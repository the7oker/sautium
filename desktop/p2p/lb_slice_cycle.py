"""The ListenBrainz slice cycle — the requester side of the second dump
family, one implementation for both runtimes.

The launcher's P2PManager runs it beside the sync walk; the Docker backend
runs the same object with its own services injected (the walk's
`connect_peer`, its DHT, no LAN tier). The MB family's cycle
(desktop/p2p/mb_slice_cycle.py) has had the same shape since 2026-09-24, and
both find their sources through desktop/p2p/slice_sources.py (CLAUDE.md: what
both surfaces must agree on exactly lives in desktop/p2p and is imported,
never copied).

What a run does (`LbSliceCycle.run`):

1. nothing, when this node holds the dump (it serves, it does not ask);
2. sources (desktop/p2p/slice_sources.py): manual peers, LAN peers, every
   holder of the DHT `lbdump` key, and — when those yield no usable source —
   the Worker directory's `lbslices` / `lbdump` volunteers with the master
   hint LAST (Ф16c de-specialization); each probed once through the walk's
   connect (health + pinned key + own-address guard), banned addresses and
   keys skipped, REPLICAS BEFORE DUMP NODES, the verified set kept between
   runs until a source fails. Every source's `/health` says which dump
   version it serves or re-serves, and the newest of those is what the
   ledger is measured against;
3. the pending set (lb_slice_queries.pending_slice_mbids): the on-demand
   lane first, then owned, then engaged artists, minus ledger rows at the
   newest version — a row older than that is STALE and asked again with
   `min_version`, so counts refresh and signed zero-matches re-open;
4. source by source, batches of 50 (the protocol's maximum): every entry verified against the
   ORIGINAL author's key, imported in one transaction per batch under the
   loader's lock; `missing` (the peer does not hold it) and DROPPED (it sent
   something that does not verify) both carry to the next source — two
   explicit sets, never one expression (desktop/mb_slice_client.py's
   precedence bug is what that invites);
5. `NOTIFY sautium_lb_done` after every importing batch — the artist page
   that asked re-fetches its own snapshot; `lb_slice.status` published on
   every run, including an all-clear, so the derived notice ends the moment
   it stops being true.

Blocking DB work runs in the executor (`psycopg2.connect` blocks the loop
for the whole handshake — see sync_walk).
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

import psycopg2

from desktop.api_client import BackendAPIClient
from desktop.p2p import lb_slice_queries
from desktop.p2p.slice_sources import SourceFinder
from desktop.p2p.sync_walk import write_settings

logger = logging.getLogger(__name__)

PENDING_PER_CYCLE = 200
INTERVAL_DEFAULT_MIN = 360           # mirrors config_manager DEFAULT_CONFIG["lb_slice"]
INTERVAL_KEY = "lb_slice.auto_interval_min"
STATUS_KEY = "lb_slice.status"
FIRST_SOURCE_MAX_DELAY = 60          # like the walk: wait for a source, not a clock
FIRST_RUN_DELAY = 120                # let the first sync walk spend the peers' windows first


# ---------------------------------------------------------------------------
# Blocking DB helpers — one short-lived autocommit connection each, always
# called through the executor from coroutine code.
# ---------------------------------------------------------------------------

def _connect(dsn: str):
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    return conn


def read_interval(dsn: str, default: int, key: str = INTERVAL_KEY) -> Optional[int]:
    """A slice cycle's auto_interval_min (this family's by default): the
    default without a row, None when the row says null (explicitly
    disabled)."""
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM user_settings WHERE key = %s", (key,))
            row = cur.fetchone()
        if row is None:
            return default
        if row[0] is None:
            return None
        return int(row[0]) if row[0] else None
    finally:
        conn.close()


def load_bans(dsn: str) -> tuple[set, set]:
    """Local ban list: (pubkeys, addr uuids) — the pubkey is the anchor,
    the address id catches a banned key returning under a fresh identity."""
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pubkey, addr::text FROM p2p_node_bans")
            rows = cur.fetchall()
        return {r[0] for r in rows if r[0]}, {r[1] for r in rows if r[1]}
    finally:
        conn.close()


def local_dump_available(dsn: str) -> Optional[str]:
    conn = _connect(dsn)
    try:
        return lb_slice_queries.local_dump_available(conn)
    finally:
        conn.close()


def pending_slice_mbids(dsn: str, limit: int, newest: Optional[str]) -> list:
    conn = _connect(dsn)
    try:
        return lb_slice_queries.pending_slice_mbids(conn, limit, newest)
    finally:
        conn.close()


def import_batch(dsn: str, source_node: str, source_addr: Optional[str],
                 verified: list) -> int:
    """One transaction for a batch of verified slices, under the loader's
    lock (a full load must never interleave with an import)."""
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (lb_slice_queries.LB_LOAD_LOCK_KEY,))
        try:
            with conn.cursor() as cur:
                cur.execute("BEGIN")
            recordings = 0
            for mbid, core, blob_gz, entry in verified:
                recordings += lb_slice_queries.import_slice(
                    conn, mbid, core, blob_gz, entry, source_node, source_addr)
            with conn.cursor() as cur:
                cur.execute("COMMIT")
            return recordings
        except Exception:
            with conn.cursor() as cur:
                cur.execute("ROLLBACK")
            raise
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (lb_slice_queries.LB_LOAD_LOCK_KEY,))
    finally:
        conn.close()


def analyze_tables(dsn: str) -> None:
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("ANALYZE lb_recording")
            cur.execute("ANALYZE lb_artist")
    finally:
        conn.close()


def notify(dsn: str, channel: str) -> None:
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(f"NOTIFY {channel}")
    finally:
        conn.close()


def clear_status(dsn: str) -> bool:
    """Drop the cycle's status row — a dump node asks nobody, so a status
    written before its load landed ("no source reachable") would otherwise
    keep the derived notice alive for good. True when a row was dropped."""
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_settings WHERE key = %s", (STATUS_KEY,))
            dropped = cur.rowcount > 0
            if dropped:
                cur.execute("NOTIFY sautium_notices")
        return dropped
    finally:
        conn.close()


class LbSliceCycle:
    """One node's LB slice requester. Construct once per process, `bind()`
    on the event loop that will run `dispatch_loop`/`interval_loop`, then
    `request(reason)` from anywhere on that loop."""

    def __init__(
        self,
        db_dsn: str,
        *,
        connect: Callable[[str], Awaitable[Optional[BackendAPIClient]]],
        config: Optional[dict] = None,
        dht=None,
        lan=None,
        manual_peers: Optional[list[str]] = None,
        load_bans_fn: Optional[Callable[[], tuple[set, set]]] = None,
        diag_record: Optional[Callable[[str, dict], None]] = None,
        first_source: Optional[Callable[[], Optional[asyncio.Event]]] = None,
    ):
        """
        connect: async (addr) -> BackendAPIClient | None — the walk's
            connect_peer: health, the TLS-pinned key, the own-address guard.
        config: the `lb_slice` block (fetch / auto_interval_min).
        dht / lan: the runtime's DHTService / LANDiscovery (either None).
        manual_peers: explicit `scheme://host:port` entries, tried first.
        load_bans_fn: () -> (pubkeys, addr uuids); the DB list by default.
        diag_record: (kind, detail) -> None, executor-safe.
        first_source: () -> the walk's first-source event (created when the
            walk binds), so the first timed run waits for a source.
        """
        self.db_dsn = db_dsn
        self.config = dict(config or {})
        self._sources = SourceFinder(
            "LB slice", capability="lbdump", directory=("lbslices", "lbdump"),
            usable=lambda h: bool(h.get("lb_dump") or h.get("lb_slices")),
            connect=connect, dht=dht, lan=lan, manual_peers=list(manual_peers or []),
            load_bans=load_bans_fn or (lambda: load_bans(self.db_dsn)),
            addr_uuid=lb_slice_queries.addr_uuid)
        self._diag_record = diag_record
        self._first_source = first_source
        self._reasons: set[str] = set()
        self._notify: Optional[asyncio.Event] = None
        self._lock: Optional[asyncio.Lock] = None
        self._next_at: Optional[float] = None
        self._running = False

    # ------------------------------------------------------------ lifecycle

    def bind(self) -> None:
        self._notify = asyncio.Event()
        self._lock = asyncio.Lock()
        self._running = True
        if self._reasons:
            self._notify.set()

    def stop(self) -> None:
        self._running = False
        if self._notify:
            self._notify.set()

    @property
    def running(self) -> bool:
        return self._running

    @property
    def dht(self):
        return self._sources.dht

    @dht.setter
    def dht(self, dht) -> None:
        """The backend starts its DHT after the cycle exists."""
        self._sources.dht = dht

    # ------------------------------------------------------------- triggers

    def request(self, reason: str) -> None:
        """Queue a run and remember why (loop thread). Concurrent reasons
        merge into the one run the dispatcher starts next."""
        self._reasons.add(reason)
        if self._notify:
            self._notify.set()

    async def request_async(self, reason: str) -> None:
        """The walk's `after_run` shape."""
        self.request(reason)

    async def dispatch_loop(self) -> None:
        while self._running:
            try:
                await self._notify.wait()
                self._notify.clear()
            except asyncio.CancelledError:
                break
            if not self._running:
                break
            reasons = "+".join(sorted(self._reasons)) or "request"
            self._reasons.clear()
            await self.run_once(trigger=reasons)

    async def interval_loop(self) -> None:
        """Periodic runs on lb_slice.auto_interval_min, re-read each cycle.
        The first run waits for a source (the walk's event) and then the
        first sync walk's head start."""
        evt = self._first_source() if self._first_source else None
        if evt is not None:
            try:
                await asyncio.wait_for(evt.wait(), timeout=FIRST_SOURCE_MAX_DELAY)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                return
        self._next_at = time.time() + FIRST_RUN_DELAY
        try:
            await asyncio.sleep(FIRST_RUN_DELAY)
        except asyncio.CancelledError:
            return
        default = int(self.config.get("auto_interval_min", INTERVAL_DEFAULT_MIN))
        loop = asyncio.get_event_loop()
        while self._running:
            interval_min = await loop.run_in_executor(None, read_interval, self.db_dsn, default)
            if interval_min and interval_min > 0:
                self._next_at = time.time() + interval_min * 60
                await self.run_once(trigger="auto")
                sleep_for = max(1.0, self._next_at - time.time())
            else:
                self._next_at = None
                sleep_for = 300   # disabled — re-check the setting later
            try:
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                break

    async def run_once(self, trigger: str) -> None:
        """One run, serialised: concurrent triggers merge into the run in
        progress rather than queueing behind it."""
        if self._lock.locked():
            logger.debug(f"LB slice cycle already in progress, skipping {trigger}")
            return
        loop = asyncio.get_event_loop()
        async with self._lock:
            try:
                stats = await self.run(trigger)
                if stats:
                    logger.info(f"LB slice run done (trigger={trigger}): {stats}")
            except Exception as e:
                logger.error(f"LB slice cycle failed: {e}", exc_info=True)
                if self._diag_record:
                    await loop.run_in_executor(
                        None, self._diag_record, "lb_slice.failed",
                        {"trigger": trigger, "error": str(e)[:500]})

    # -------------------------------------------------------------- sources

    async def find_sources(self) -> tuple[list[tuple[BackendAPIClient, str, Optional[str]]],
                                          Optional[str]]:
        """Reachable slice sources as (client, node_id, version), REPLICAS
        FIRST, and the newest dump version any of them holds — read from the
        /health each answered when it was probed."""
        sources, _ = await self._sources.find()
        replicas: list = []
        dumps: list = []
        newest: Optional[str] = None
        for api, node_id, health in sources:
            dump_version = health.get("lb_dump") or None
            held_version = health.get("lb_slices_version") or None
            for v in (dump_version, held_version):
                if isinstance(v, str) and v and (newest is None or v > newest):
                    newest = v
            if dump_version:
                dumps.append((api, node_id, dump_version))
            else:
                replicas.append((api, node_id, held_version))
        return replicas + dumps, newest

    # ------------------------------------------------------------- the run

    async def run(self, trigger: str = "auto") -> dict:
        loop = asyncio.get_event_loop()
        if not self.config.get("fetch", True):
            return {}
        if await loop.run_in_executor(None, local_dump_available, self.db_dsn):
            # A dump node serves; it has nothing to ask for — and nothing to
            # report: a status left from before its load must not outlive it.
            await loop.run_in_executor(None, clear_status, self.db_dsn)
            return {}

        sources, newest = await self.find_sources()
        pending = await loop.run_in_executor(
            None, pending_slice_mbids, self.db_dsn, PENDING_PER_CYCLE, newest)
        if not pending:
            await self._publish(pending=0, served=0, unserved=0, reason="idle",
                                sources=len(sources), newest=newest)
            return {}
        if not sources:
            logger.info(f"LB slice: {len(pending)} pending, no source reachable")
            await self._publish(pending=len(pending), served=0, unserved=len(pending),
                                reason="no_sources", sources=0, newest=newest)
            return {}

        batch_size = lb_slice_queries.MAX_MBIDS_PER_REQUEST
        # Stale rows (a ledger version older than the newest reachable) are
        # asked with min_version, so a replica's older cache is a miss, not
        # an answer; never-fetched artists take the first data they can get
        # and are re-asked as stale next run if it was older.
        remaining = {"stale": [m for m, v in pending if v is not None],
                     "fresh": [m for m, v in pending if v is None]}
        logger.info(f"LB slice: {len(pending)} pending ({len(remaining['stale'])} stale), "
                    f"{len(sources)} source(s), newest {newest} (trigger={trigger})")

        total = {"artists": 0, "recordings": 0}
        reasons: set[str] = set()
        imported_any = False
        for api, node, _ in sources:
            if not (remaining["stale"] or remaining["fresh"]):
                break
            source_addr = lb_slice_queries.addr_uuid(api.base_url)
            leftovers = {"stale": [], "fresh": []}
            waited = limited = False
            for lane, min_version in (("stale", newest), ("fresh", None)):
                group = remaining[lane]
                for i in range(0, len(group), batch_size):
                    batch = group[i:i + batch_size]
                    if limited:
                        leftovers[lane].extend(batch)
                        continue
                    result = await self._exchange(api, node, source_addr, batch, min_version)
                    if result.get("retry_after") is not None and not waited:
                        # The peer's per-IP window is full — usually our own
                        # sync walk just spent it. Wait it out ONCE per
                        # source and re-ask instead of parking the batch.
                        wait = min(int(result.get("retry_after") or 60), 120)
                        logger.info(f"LB slice: {node[:16]} rate-limited — re-asking in {wait}s")
                        waited = True
                        await asyncio.sleep(wait)
                        result = await self._exchange(api, node, source_addr, batch, min_version)
                    if "error" in result:
                        leftovers[lane].extend(batch)
                        if result.get("retry_after") is not None:
                            reasons.add("rate_limited")
                            limited = True
                        else:
                            reasons.add("error")
                            self._sources.drop(node)
                        continue
                    if result["imported"]:
                        imported_any = True
                    total["artists"] += result["imported"]
                    total["recordings"] += result["recordings"]
                    leftovers[lane].extend(result["missing"])
            remaining = leftovers

        unserved = len(remaining["stale"]) + len(remaining["fresh"])
        if unserved:
            logger.info(f"LB slice: {unserved} artist(s) not served this cycle — they stay pending")
        if imported_any:
            await loop.run_in_executor(None, analyze_tables, self.db_dsn)
        await self._publish(
            pending=len(pending), served=len(pending) - unserved, unserved=unserved,
            reason=("rate_limited" if "rate_limited" in reasons
                    else "error" if "error" in reasons
                    else "missing" if unserved else "ok"),
            sources=len(sources), newest=newest)
        return total

    async def _exchange(self, api: BackendAPIClient, node: str, source_addr: Optional[str],
                        batch: list[str], min_version: Optional[str]) -> dict:
        """One request to one source: verify every entry against the
        ORIGINAL author's key, import the verified ones in one transaction.
        `missing` carries what the peer does not hold AND what it sent that
        does not verify; an all-dropped answer is an error (the source is
        skipped for the rest of this run)."""
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, api.lb_slice, batch, min_version)
        if not resp or "error" in resp or "detail" in resp:
            err = (resp or {}).get("error") or (resp or {}).get("detail") or "no response"
            logger.warning(f"LB slice fetch failed ({node[:16]}): {err}")
            out = {"error": err}
            if "retry_after" in (resp or {}):
                out["retry_after"] = resp["retry_after"]
            return out
        if resp.get("v") != lb_slice_queries.PROTOCOL_VERSION:
            logger.warning(f"LB slice from {node[:16]}: incompatible protocol — rejected")
            return {"error": "incompatible slice protocol"}
        slices = resp.get("slices") or {}
        verified: list = []
        missing: list = []
        dropped = 0
        for mbid in batch:
            entry = slices.get(mbid)
            if entry is None:
                missing.append(mbid)
                continue
            out = lb_slice_queries.verify_slice_entry(mbid, entry, min_version)
            if out is None:
                dropped += 1
                missing.append(mbid)
                logger.warning(f"LB slice for {mbid} from {node[:16]}: "
                               f"signature/identity/version check failed — dropped")
                continue
            verified.append((mbid, out[0], out[1], entry))
        if dropped and not verified:
            return {"error": "all slices failed verification"}
        recordings = 0
        if verified:
            recordings = await loop.run_in_executor(
                None, import_batch, self.db_dsn, node, source_addr, verified)
            await loop.run_in_executor(None, notify, self.db_dsn, "sautium_lb_done")
        return {"imported": len(verified), "recordings": recordings,
                "missing": missing, "dropped": dropped}

    async def _publish(self, *, pending: int, served: int, unserved: int,
                       reason: str, sources: int, newest: Optional[str]) -> None:
        """One row, `lb_slice.status`, is the whole of what the UI knows
        about this cycle; written on every run — an all-clear included — so
        the derived notice (`lb_slice.deferred`) ends the moment it stops
        being true. The NOTIFY wakes the backend's notices channel."""
        next_at = self._next_at
        state = {
            "at": datetime.now(timezone.utc).isoformat(),
            "pending": pending,
            "pending_capped": pending >= PENDING_PER_CYCLE,
            "served": served,
            "unserved": unserved,
            "reason": reason,
            "sources": sources,
            "newest_version": newest,
            "next_attempt_at": (datetime.fromtimestamp(next_at, timezone.utc).isoformat()
                                if next_at else None),
        }
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, write_settings, self.db_dsn, {STATUS_KEY: state})
            await loop.run_in_executor(None, notify, self.db_dsn, "sautium_notices")
        except Exception as e:
            logger.warning(f"LB slice: status publish failed: {e}")
