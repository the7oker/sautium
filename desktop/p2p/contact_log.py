"""Contact log — the measurement layer of the defense strategy
(P2P-SYNC-INTEGRITY.md § "Phased rollout — measure before you arm", Ф7).

Every request a peer surface serves becomes one row in
`p2p_contact_events` (who: pubkey when signed, pseudonymised address +
/24 subnet; what: endpoint family, lane, status, request items /
target names; how much: bytes in/out, wall and CPU milliseconds) and —
for served (2xx) responses only — one sample in the per-endpoint cost
EMA (`p2p_action_costs`, the `base(action)` of the pricing formula,
calibrated per node). Raw rows live 30 days under a row cap; aggregates
are the report's business.

Local by construction — this history is never shared and never becomes a
banlist. Recording is off the request path: `record()` appends to an
in-memory queue, a daemon thread batch-inserts (execute_values, 500 rows)
when the queue fills or every few seconds, whichever comes first.

CPU attribution is process-wide (`time.process_time()` around the
handler), so under concurrency a request is charged for its neighbours'
work too; the EMA over many samples converges on a usable per-endpoint
cost and the report shows wall vs CPU side by side. Good enough for a
price scale, honest about what it is.

`python -m desktop.p2p.contact_log --report [--dsn …]` prints the
distributions the later phases need: endpoint × lane volumes, the cost
table, the repeat-contact ratio ("how likely is a second contact from the
same identity/address?"), contacts-per-identity buckets, births per day
and hour-of-day / subnet spreads (the similarity axes, Ф14).
"""

import ipaddress
import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid5

logger = logging.getLogger(__name__)

RAW_RETENTION_DAYS = 30
RAW_ROWS_CAP = 1_000_000
FLUSH_INTERVAL_SECONDS = 5.0
FLUSH_BATCH = 200
EMA_ALPHA = 0.05
MAX_TARGETS = 8

_SAUTIUM_NAMESPACE = UUID("adc1ec0b-2c81-5e26-9938-a369c6f7a5e1")

SCHEMA_SQL = (
    """DO $$ BEGIN
         CREATE TYPE p2p_lane AS ENUM ('anonymous', 'stranger', 'identity');
       EXCEPTION WHEN duplicate_object THEN NULL; END $$""",
    """CREATE TABLE IF NOT EXISTS p2p_contact_events (
         id          BIGSERIAL PRIMARY KEY,
         ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
         pubkey      TEXT,
         addr        UUID,
         subnet      UUID,
         endpoint    TEXT NOT NULL,
         lane        p2p_lane,
         status      SMALLINT NOT NULL,
         bytes_in    INTEGER NOT NULL DEFAULT 0,
         bytes_out   INTEGER NOT NULL DEFAULT 0,
         wall_ms     REAL NOT NULL,
         cpu_ms      REAL NOT NULL,
         items       INTEGER,
         targets     TEXT[]
       )""",
    """CREATE INDEX IF NOT EXISTS idx_p2p_contact_events_ts ON p2p_contact_events (ts)""",
    """CREATE INDEX IF NOT EXISTS idx_p2p_contact_events_pubkey_ts
         ON p2p_contact_events (pubkey, ts) WHERE pubkey IS NOT NULL""",
    """CREATE INDEX IF NOT EXISTS idx_p2p_contact_events_addr_ts ON p2p_contact_events (addr, ts)""",
    """CREATE INDEX IF NOT EXISTS idx_p2p_contact_events_endpoint_ts ON p2p_contact_events (endpoint, ts)""",
    """ALTER TABLE p2p_contact_events ADD COLUMN IF NOT EXISTS gate_price SMALLINT""",
    """ALTER TABLE p2p_contact_events ADD COLUMN IF NOT EXISTS gate_status TEXT""",
    """CREATE TABLE IF NOT EXISTS p2p_action_costs (
         endpoint      TEXT PRIMARY KEY,
         ema_cpu_ms    REAL NOT NULL,
         ema_wall_ms   REAL NOT NULL,
         ema_bytes_out REAL NOT NULL,
         samples       BIGINT NOT NULL,
         updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
       )""",
)


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        for stmt in SCHEMA_SQL:
            cur.execute(stmt)
    conn.commit()


def endpoint_family(path: str) -> str:
    """Route → stable family name (the cost table's key)."""
    p = path.split("?", 1)[0].rstrip("/")
    if p == "/health":
        return "health"
    parts = p.strip("/").split("/")
    if len(parts) >= 3 and parts[0] == "api":
        return f"{parts[1]}.{parts[2].replace('-', '_')}"
    return "other"


def addr_ids(host: Optional[str]):
    """(addr uuid, subnet uuid) — the project's node_addr formula plus a
    /24 (IPv6 /48) sibling for the subnet axis. Both None without a host."""
    if not host:
        return None, None
    host = host.lower()
    addr = str(uuid5(_SAUTIUM_NAMESPACE, f"node_addr:{host}"))
    try:
        ip = ipaddress.ip_address(host)
        if ip.version == 4:
            net = ipaddress.ip_network(f"{host}/24", strict=False)
        else:
            net = ipaddress.ip_network(f"{host}/48", strict=False)
        subnet = str(uuid5(_SAUTIUM_NAMESPACE, f"node_subnet:{net}"))
    except ValueError:
        subnet = None
    return addr, subnet


def extract_request_shape(path: str, body: bytes):
    """(items, targets) for the request families that carry names or ids:
    mb.search → the query as one target; mb.slice → up to MAX_TARGETS
    name keys + count; sync.* → item count only. Never raises."""
    family = endpoint_family(path)
    try:
        if family == "mb.search":
            q = parse_qs(urlsplit(path).query).get("q", [""])[0].strip().lower()
            return (1, [q[:200]]) if q else (None, None)
        if not body:
            return None, None
        data = json.loads(body)
        if not isinstance(data, dict):
            return None, None
        if family == "mb.slice":
            names = data.get("names") or []
            keys = [k for k in (str(n).strip().lower()[:200] for n in names) if k][:MAX_TARGETS]
            return len(names), keys or None
        for key in ("track_uuids", "uuids", "recordings", "items"):
            if isinstance(data.get(key), list):
                return len(data[key]), None
    except (ValueError, TypeError):
        pass
    return None, None


class ContactLog:
    """Per-process recorder: queue → daemon flusher → batch INSERT + EMA."""

    def __init__(self, conn_factory: Callable):
        self._conn_factory = conn_factory
        self._queue: deque = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._costs: dict = {}
        self._flushes = 0
        self._loaded = False

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="p2p-contact-log")
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self._flush()

    def record(self, *, endpoint: str, status: int, wall_ms: float, cpu_ms: float,
               pubkey: Optional[str] = None, addr: Optional[str] = None,
               lane: Optional[str] = None, bytes_in: int = 0, bytes_out: int = 0,
               items: Optional[int] = None, targets: Optional[list] = None,
               gate_price: Optional[int] = None, gate_status: Optional[str] = None) -> None:
        addr_id, subnet_id = addr_ids(addr)
        row = (datetime.now(timezone.utc), pubkey.lower() if pubkey else None, addr_id, subnet_id,
               endpoint, lane, int(status), int(bytes_in or 0), int(bytes_out or 0),
               float(wall_ms), float(cpu_ms), items, targets, gate_price, gate_status)
        with self._lock:
            self._queue.append(row)
            if 200 <= int(status) < 300:
                # base(action) is what a SERVED request costs us; refusals
                # (402/403/429, ~0 ms, 0 bytes) would drag the EMA toward
                # zero and price the real work away — measured on the
                # master: mb.slice EMA 3 ms next to a 1046 ms real slice.
                self._bump_cost(endpoint, cpu_ms, wall_ms, bytes_out)
            if len(self._queue) >= FLUSH_BATCH:
                self._wake.set()

    def costs(self) -> dict:
        """{endpoint: (ema_cpu_ms, ema_bytes_out)} — the pricer's base(action)."""
        with self._lock:
            return {e: (c["cpu"], c["bytes"]) for e, c in self._costs.items()}

    def _bump_cost(self, endpoint, cpu_ms, wall_ms, bytes_out) -> None:
        c = self._costs.get(endpoint)
        if c is None:
            self._costs[endpoint] = {"cpu": float(cpu_ms), "wall": float(wall_ms),
                                     "bytes": float(bytes_out), "n": 1, "dirty": True}
            return
        c["cpu"] += EMA_ALPHA * (cpu_ms - c["cpu"])
        c["wall"] += EMA_ALPHA * (wall_ms - c["wall"])
        c["bytes"] += EMA_ALPHA * (bytes_out - c["bytes"])
        c["n"] += 1
        c["dirty"] = True

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(FLUSH_INTERVAL_SECONDS)
            self._wake.clear()
            try:
                self._flush()
            except Exception as e:                       # the log must never take the server down
                logger.warning("contact log flush failed: %s", e)

    def _flush(self) -> None:
        with self._lock:
            rows = list(self._queue)
            self._queue.clear()
            dirty = {k: dict(v) for k, v in self._costs.items() if v["dirty"]}
            for v in self._costs.values():
                v["dirty"] = False
        if not rows and not dirty:
            return
        from psycopg2.extras import execute_values
        with self._conn_factory() as conn:
            if not self._loaded:
                self._load_costs(conn)
            with conn.cursor() as cur:
                if rows:
                    execute_values(cur, """
                        INSERT INTO p2p_contact_events
                            (ts, pubkey, addr, subnet, endpoint, lane, status,
                             bytes_in, bytes_out, wall_ms, cpu_ms, items, targets,
                             gate_price, gate_status)
                        VALUES %s
                    """, rows, page_size=500)
                for endpoint, c in dirty.items():
                    cur.execute("""
                        INSERT INTO p2p_action_costs
                            (endpoint, ema_cpu_ms, ema_wall_ms, ema_bytes_out, samples, updated_at)
                        VALUES (%s, %s, %s, %s, %s, now())
                        ON CONFLICT (endpoint) DO UPDATE SET
                            ema_cpu_ms = EXCLUDED.ema_cpu_ms,
                            ema_wall_ms = EXCLUDED.ema_wall_ms,
                            ema_bytes_out = EXCLUDED.ema_bytes_out,
                            samples = EXCLUDED.samples,
                            updated_at = now()
                    """, (endpoint, c["cpu"], c["wall"], c["bytes"], c["n"]))
                self._flushes += 1
                if self._flushes % 50 == 1:
                    cur.execute("DELETE FROM p2p_contact_events WHERE ts < now() - make_interval(days => %s)",
                                (RAW_RETENTION_DAYS,))
                    cur.execute("SELECT count(*) FROM p2p_contact_events")
                    if cur.fetchone()[0] > RAW_ROWS_CAP:
                        cur.execute("""
                            DELETE FROM p2p_contact_events WHERE id IN (
                                SELECT id FROM p2p_contact_events ORDER BY ts ASC LIMIT 10000)
                        """)
            conn.commit()

    def _load_costs(self, conn) -> None:
        """Continue the EMA across restarts; in-memory samples seeded from
        the persisted counters so a fresh process does not restart at 1."""
        with conn.cursor() as cur:
            cur.execute("SELECT endpoint, ema_cpu_ms, ema_wall_ms, ema_bytes_out, samples FROM p2p_action_costs")
            for endpoint, cpu, wall, b, n in cur.fetchall():
                mine = self._costs.get(endpoint)
                if mine is None:
                    self._costs[endpoint] = {"cpu": cpu, "wall": wall, "bytes": b, "n": n, "dirty": False}
                else:
                    # blend the persisted EMA with what this process saw before its first flush
                    mine["cpu"] = cpu + EMA_ALPHA * (mine["cpu"] - cpu)
                    mine["wall"] = wall + EMA_ALPHA * (mine["wall"] - wall)
                    mine["bytes"] = b + EMA_ALPHA * (mine["bytes"] - b)
                    mine["n"] += n
                    mine["dirty"] = True
        self._loaded = True


class RequestTimer:
    """Wall + process-CPU delta around one request."""

    def __init__(self):
        self._t0 = time.perf_counter()
        self._c0 = time.process_time()

    def elapsed_ms(self):
        return ((time.perf_counter() - self._t0) * 1000.0,
                (time.process_time() - self._c0) * 1000.0)


# ----------------------------------------------------------------------------
# python -m desktop.p2p.contact_log --report [--dsn …] [--days 7]
# ----------------------------------------------------------------------------

def report(conn, days: int = 7) -> str:
    out = []
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), min(ts), max(ts) FROM p2p_contact_events WHERE ts > now() - make_interval(days => %s)", (days,))
        n, lo, hi = cur.fetchone()
        out.append(f"contact events, last {days} d: {n} ({lo} … {hi})")
        if not n:
            return "\n".join(out)

        out.append("\nby endpoint × lane:")
        cur.execute("""
            SELECT endpoint, coalesce(lane::text, '-') AS lane, count(*),
                   count(DISTINCT pubkey) FILTER (WHERE pubkey IS NOT NULL) AS identities,
                   count(DISTINCT addr) AS addrs,
                   round(avg(wall_ms)::numeric, 1), round(avg(cpu_ms)::numeric, 1),
                   round(avg(bytes_out)::numeric, 0)
              FROM p2p_contact_events
             WHERE ts > now() - make_interval(days => %s)
             GROUP BY 1, 2 ORDER BY 3 DESC
        """, (days,))
        out.append(f"  {'endpoint':<22}{'lane':<11}{'events':>8}{'ids':>6}{'addrs':>7}{'wall ms':>9}{'cpu ms':>8}{'bytes':>9}")
        for r in cur.fetchall():
            out.append(f"  {r[0]:<22}{r[1]:<11}{r[2]:>8}{r[3]:>6}{r[4]:>7}{r[5]:>9}{r[6]:>8}{r[7]:>9}")

        cur.execute("""
            SELECT coalesce(gate_status, '-'), count(*), round(avg(gate_price)::numeric, 1), max(gate_price)
              FROM p2p_contact_events
             WHERE ts > now() - make_interval(days => %s) AND lane IS NOT NULL
             GROUP BY 1 ORDER BY 2 DESC
        """, (days,))
        gate_rows = cur.fetchall()
        if gate_rows:
            out.append("\ngate (shadow/enforce): status · events · avg would-be price · max")
            for st, n, avg, mx in gate_rows:
                out.append(f"  {st:<10} {n:>7}   {avg if avg is not None else '-':>6}   {mx if mx is not None else '-'}")

        out.append("\naction costs (EMA, per node):")
        cur.execute("SELECT endpoint, ema_cpu_ms, ema_wall_ms, ema_bytes_out, samples FROM p2p_action_costs ORDER BY ema_cpu_ms DESC")
        for e, cpu, wall, b, s in cur.fetchall():
            out.append(f"  {e:<22} cpu {cpu:7.1f} ms  wall {wall:8.1f} ms  out {b:9.0f} B  (n={s})")

        out.append("\nrepeat contacts (an event whose identity/address was already seen in the window):")
        cur.execute("""
            WITH e AS (
              SELECT endpoint, pubkey, addr,
                     row_number() OVER (PARTITION BY coalesce(pubkey, addr::text) ORDER BY ts) AS rn
                FROM p2p_contact_events
               WHERE ts > now() - make_interval(days => %s) AND (pubkey IS NOT NULL OR addr IS NOT NULL))
            SELECT split_part(endpoint, '.', 1) AS family,
                   count(*) AS events,
                   round(100.0 * count(*) FILTER (WHERE rn > 1) / count(*), 1) AS repeat_pct
              FROM e GROUP BY 1 ORDER BY 2 DESC
        """, (days,))
        for fam, ev, pct in cur.fetchall():
            out.append(f"  {fam:<10} events {ev:>7}  repeat {pct:>5}%")
        cur.execute("""
            WITH per AS (
              SELECT coalesce(pubkey, addr::text) AS who, count(*) AS c
                FROM p2p_contact_events
               WHERE ts > now() - make_interval(days => %s) AND (pubkey IS NOT NULL OR addr IS NOT NULL)
               GROUP BY 1)
            SELECT count(*) FILTER (WHERE c = 1), count(*) FILTER (WHERE c BETWEEN 2 AND 5),
                   count(*) FILTER (WHERE c BETWEEN 6 AND 20), count(*) FILTER (WHERE c > 20)
              FROM per
        """, (days,))
        b1, b2, b3, b4 = cur.fetchone()
        out.append(f"  contacts per identity/address: once {b1} · 2–5 {b2} · 6–20 {b3} · >20 {b4}")

        out.append("\nsimilarity axes (registry + events):")
        cur.execute("SELECT date_trunc('day', issued_at)::date, count(*) FROM p2p_identities GROUP BY 1 ORDER BY 1")
        births = cur.fetchall()
        out.append("  births per day (issued_at): " + ", ".join(f"{d}:{c}" for d, c in births[-14:]))
        cur.execute("""
            SELECT subnet, count(DISTINCT pubkey) FROM p2p_contact_events
             WHERE pubkey IS NOT NULL AND subnet IS NOT NULL AND ts > now() - make_interval(days => %s)
             GROUP BY 1 HAVING count(DISTINCT pubkey) > 1 ORDER BY 2 DESC LIMIT 5
        """, (days,))
        multi = cur.fetchall()
        out.append(f"  subnets with >1 identity: {len(multi)}" + (" — top " + ", ".join(f"{str(s)[:8]}:{c}" for s, c in multi) if multi else ""))
        cur.execute("""
            SELECT extract(hour from ts AT TIME ZONE 'UTC')::int AS h, count(*)
              FROM p2p_contact_events WHERE ts > now() - make_interval(days => %s) GROUP BY 1 ORDER BY 1
        """, (days,))
        hours = dict(cur.fetchall())
        out.append("  hour of day (UTC): " + " ".join(f"{h:02d}:{hours.get(h, 0)}" for h in range(24)))
        cur.execute("SELECT method, status, count(*) FROM p2p_identities GROUP BY 1, 2 ORDER BY 1, 2")
        out.append("  registry: " + ", ".join(f"{m}/{s}={c}" for m, s, c in cur.fetchall()))
        cur.execute("""
            SELECT count(*) FILTER (WHERE succeeded_by IS NOT NULL),
                   count(*) FILTER (WHERE predecessor IS NOT NULL),
                   (SELECT count(*) FROM p2p_node_bans WHERE reason = 'succession')
              FROM p2p_identities
        """)
        succeeded, heirs, inherited = cur.fetchone()
        out.append(f"  succession: {succeeded} rows succeeded, {heirs} heirs, {inherited} inherited bans")
    from desktop.p2p import similarity
    out.append(similarity.report(conn))
    return "\n".join(out)


if __name__ == "__main__":
    import argparse
    import psycopg2
    ap = argparse.ArgumentParser(description="P2P contact log")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dsn", default="postgresql://musicai:supervisor@localhost:5432/music_ai")
    args = ap.parse_args()
    if args.report:
        conn = psycopg2.connect(args.dsn)
        try:
            ensure_schema(conn)
            print(report(conn, args.days))
        finally:
            conn.close()
    else:
        ap.print_help()
