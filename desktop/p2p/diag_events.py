"""Node-local diagnostic event log — the durable record behind support
reports and the `events` bundle scope (desktop/p2p/diag_protocol.py).

Every node records; only a launcher node ships (P2PManager's diag
worker). Writers hand in an open psycopg2 connection; the insert trigger
(004_support_diagnostics.sql) NOTIFYs `sautium_diag`, which wakes the
shipper. Before the database exists — the first-run wizard, a PostgreSQL
start failure — events go to a spool file under <data_dir>/diag/ that the
launcher drains at its next successful start.

Also here: the per-warrant state machine on the receiving node
(receive_warrant → mark_collected → mark_uploaded), whose PRIMARY KEY is
what makes a warrant single-use.

python -m desktop.p2p.diag_events --selftest [--dsn …]
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from desktop.p2p.diag_protocol import REPORT_MAX_AGE_DAYS, REPORT_MAX_EVENTS

logger = logging.getLogger(__name__)

DIAG_DIRNAME = "diag"
SPOOL_FILENAME = "spool.jsonl"
SESSION_FILENAME = "session.json"
LOCAL_RETENTION_DAYS = 90

_SETTINGS_UPSERT = """
    INSERT INTO user_settings (key, value, updated_at)
    VALUES (%s, %s::jsonb, NOW())
    ON CONFLICT (key) DO UPDATE
        SET value = EXCLUDED.value, updated_at = NOW()
"""


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _commit(conn) -> None:
    if not conn.autocommit:
        conn.commit()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def record(conn, kind: str, detail: Optional[dict] = None,
           ts: Optional[str] = None) -> int:
    """One event row; the insert trigger NOTIFYs sautium_diag."""
    with conn.cursor() as cur:
        if ts:
            cur.execute(
                "INSERT INTO diag_events (ts, kind, detail) VALUES (%s, %s, %s::jsonb) RETURNING id",
                (ts, kind, json.dumps(detail or {}, default=str)))
        else:
            cur.execute(
                "INSERT INTO diag_events (kind, detail) VALUES (%s, %s::jsonb) RETURNING id",
                (kind, json.dumps(detail or {}, default=str)))
        event_id = cur.fetchone()[0]
    _commit(conn)
    return event_id


def record_or_spool(dsn: str, data_dir: Path, kind: str,
                    detail: Optional[dict] = None) -> bool:
    """Record through a short-lived connection; when the database is not
    there (PostgreSQL down, tables not yet migrated, first run) the event
    waits in the spool instead. True when it reached the database."""
    import psycopg2
    try:
        conn = psycopg2.connect(dsn, connect_timeout=3)
        try:
            conn.autocommit = True
            record(conn, kind, detail)
        finally:
            conn.close()
        return True
    except psycopg2.Error as e:
        logger.debug("diag: database unavailable, spooling %s (%s)", kind, e)
        spool(data_dir, kind, detail)
        return False


def drain_unreported(conn, max_age_days: int = REPORT_MAX_AGE_DAYS,
                     limit: int = REPORT_MAX_EVENTS) -> list:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, kind, ts, detail
              FROM diag_events
             WHERE reported_at IS NULL
               AND ts > now() - make_interval(days => %s)
             ORDER BY id
             LIMIT %s
        """, (max_age_days, limit))
        return [{"id": r[0], "kind": r[1], "ts": r[2].isoformat(), "detail": r[3] or {}}
                for r in cur.fetchall()]


def mark_reported(conn, ids: Iterable[int]) -> None:
    ids = list(ids)
    if not ids:
        return
    with conn.cursor() as cur:
        cur.execute("UPDATE diag_events SET reported_at = now() WHERE id = ANY(%s)", (ids,))
    _commit(conn)


def pending_count(conn, max_age_days: int = REPORT_MAX_AGE_DAYS) -> int:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FROM diag_events
             WHERE reported_at IS NULL
               AND ts > now() - make_interval(days => %s)
        """, (max_age_days,))
        return int(cur.fetchone()[0])


def recent(conn, limit: int) -> list:
    """Newest-first rows for the `events` bundle scope — local-only fields
    included, this is the bundle the master asked for."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, ts, kind, detail, reported_at
              FROM diag_events ORDER BY id DESC LIMIT %s
        """, (limit,))
        return [{"id": r[0], "ts": r[1].isoformat(), "kind": r[2], "detail": r[3] or {},
                 "reported_at": r[4].isoformat() if r[4] else None}
                for r in cur.fetchall()]


def prune(conn, days: int = LOCAL_RETENTION_DAYS) -> int:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM diag_events WHERE ts < now() - make_interval(days => %s)", (days,))
        removed = cur.rowcount
        cur.execute("DELETE FROM diag_warrants WHERE expires_at < now() - make_interval(days => %s)",
                    (days,))
        removed += cur.rowcount
    _commit(conn)
    return removed


# ---------------------------------------------------------------------------
# Agent auth state observer — event-driven by the reads the UI already makes
# ---------------------------------------------------------------------------

_agent_state_cache: dict = {}
_agent_state_lock = threading.Lock()


def observe_agent_state(conn, agent: str, state: str, source: str = "settings") -> bool:
    """Record `agent.state_changed` when an agent's auth state differs from
    the last one seen. The baseline lives in user_settings so a restart
    does not re-announce; the first observation only seeds it. Returns
    True when an event was recorded."""
    key = f"support.agent_state.{agent}"
    with _agent_state_lock:
        if agent not in _agent_state_cache:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM user_settings WHERE key = %s", (key,))
                row = cur.fetchone()
            _agent_state_cache[agent] = row[0] if row else None
        previous = _agent_state_cache[agent]
        if previous == state:
            return False
        _agent_state_cache[agent] = state
    with conn.cursor() as cur:
        cur.execute(_SETTINGS_UPSERT, (key, json.dumps(state)))
    _commit(conn)
    if previous is None:
        return False
    record(conn, "agent.state_changed",
           {"agent": agent, "from": previous, "to": state, "source": source})
    return True


# ---------------------------------------------------------------------------
# Spool + session marker (<data_dir>/diag/) — for the moments without a DB
# ---------------------------------------------------------------------------

def diag_dir(data_dir: Path) -> Path:
    d = Path(data_dir) / DIAG_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def spool(data_dir: Path, kind: str, detail: Optional[dict] = None) -> None:
    line = json.dumps({"ts": _iso_now(), "kind": kind, "detail": detail or {}},
                      ensure_ascii=False, default=str)
    with open(diag_dir(data_dir) / SPOOL_FILENAME, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def drain_spool(conn, data_dir: Path) -> int:
    path = diag_dir(data_dir) / SPOOL_FILENAME
    if not path.exists():
        return 0
    lines = path.read_text(encoding="utf-8").splitlines()
    count = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            record(conn, item["kind"], item.get("detail") or {}, ts=item.get("ts"))
            count += 1
        except (ValueError, KeyError) as e:
            logger.warning("diag spool: dropped unreadable line (%s)", e)
    path.write_text("", encoding="utf-8")
    return count


def write_session_marker(data_dir: Path, **extra) -> Optional[dict]:
    """Stamp this launcher session; returns the PREVIOUS marker when one was
    left behind — the definition of an unclean shutdown."""
    path = diag_dir(data_dir) / SESSION_FILENAME
    previous = None
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            previous = {"corrupt": True}
    path.write_text(json.dumps({"started_at": _iso_now(), "pid": os.getpid(), **extra}),
                    encoding="utf-8")
    return previous


def clear_session_marker(data_dir: Path) -> None:
    path = diag_dir(data_dir) / SESSION_FILENAME
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Warrant state machine (receiving node)
# ---------------------------------------------------------------------------

def receive_warrant(conn, warrant: dict) -> dict:
    """First receipt inserts the row (ON CONFLICT decides — one statement,
    no read-then-write race); either way the current state comes back:
    {"fresh", "collected_at", "uploaded_at", "error"}."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO diag_warrants (id, issuer, issued_at, expires_at, scopes, since)
            VALUES (%s, %s, to_timestamp(%s), to_timestamp(%s), %s, to_timestamp(%s))
            ON CONFLICT (id) DO NOTHING
            RETURNING id
        """, (warrant["id"], warrant["issuer"], int(warrant["issued_at"]),
              int(warrant["expires_at"]), list(warrant["scopes"]), warrant.get("since")))
        fresh = cur.fetchone() is not None
        cur.execute("SELECT collected_at, uploaded_at, error FROM diag_warrants WHERE id = %s",
                    (warrant["id"],))
        collected_at, uploaded_at, error = cur.fetchone()
    _commit(conn)
    return {"fresh": fresh, "collected_at": collected_at, "uploaded_at": uploaded_at,
            "error": error}


def mark_collected(conn, warrant_id: str, size_bytes: int) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE diag_warrants SET collected_at = now(), size_bytes = %s WHERE id = %s",
                    (int(size_bytes), warrant_id))
    _commit(conn)


def mark_uploaded(conn, warrant_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE diag_warrants SET uploaded_at = now() WHERE id = %s", (warrant_id,))
    _commit(conn)


def mark_failed(conn, warrant_id: str, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE diag_warrants SET error = %s WHERE id = %s",
                    (str(error)[:500], warrant_id))
    _commit(conn)


def resumable_warrants(conn) -> list:
    """Collected, not yet uploaded, not failed, not expired — what a master
    reconnect retries."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id::text FROM diag_warrants
             WHERE collected_at IS NOT NULL AND uploaded_at IS NULL
               AND error IS NULL AND expires_at > now()
             ORDER BY received_at
        """)
        return [r[0] for r in cur.fetchall()]


def last_warrant_at(conn) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT max(uploaded_at) FROM diag_warrants")
        row = cur.fetchone()
    return row[0].isoformat() if row and row[0] else None


# ---------------------------------------------------------------------------
# python -m desktop.p2p.diag_events --selftest
# ---------------------------------------------------------------------------

def _selftest(dsn: str) -> None:
    import select
    import uuid

    import psycopg2

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    listener = psycopg2.connect(dsn)
    listener.autocommit = True
    with listener.cursor() as cur:
        cur.execute("LISTEN sautium_diag")

    wid = str(uuid.uuid4())
    created = []
    try:
        created.append(record(conn, "sync.failed", {"trigger": "selftest", "error": "boom"}))
        assert select.select([listener], [], [], 5)[0], "no NOTIFY within 5 s"
        listener.poll()
        assert any(n.channel == "sautium_diag" for n in listener.notifies), "wrong channel"

        pending = [e for e in drain_unreported(conn) if e["id"] in created]
        assert len(pending) == 1 and pending[0]["kind"] == "sync.failed"
        mark_reported(conn, [pending[0]["id"]])
        assert not [e for e in drain_unreported(conn) if e["id"] in created]

        now = int(time.time())
        warrant = {"id": wid, "issuer": "ab" * 32, "issued_at": now,
                   "expires_at": now + 600, "scopes": ["system"], "since": None}
        first = receive_warrant(conn, warrant)
        again = receive_warrant(conn, warrant)
        assert first["fresh"] and not again["fresh"], "PK did not decide first receipt"
        mark_collected(conn, wid, 123)
        assert wid in resumable_warrants(conn)
        mark_uploaded(conn, wid)
        assert wid not in resumable_warrants(conn)
        assert receive_warrant(conn, warrant)["uploaded_at"] is not None
        print("selftest OK")
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM diag_events WHERE id = ANY(%s)", (created,))
            cur.execute("DELETE FROM diag_warrants WHERE id = %s", (wid,))
        listener.close()
        conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--dsn", default="postgresql://musicai:supervisor@localhost:5432/music_ai")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.selftest:
        _selftest(args.dsn)
    else:
        parser.print_help()
