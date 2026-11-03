"""What is kept and sent for support names no person, place or file, against
a real PostgreSQL built from the migrations: an event's detail is stored
scrubbed whatever its writer put in it (diag_events.record); the rows kept
before that — the node's events, the master's received reports — are scrubbed
once at startup, and a bundle staged before is withdrawn (db_migrate's
`diag_scrubbed_v1`); and the facts a node sends
at every start (diag_bundle.system_facts, node.started) carry the backend's
health as states, never the music folder's path or the reachability's
addresses. Skipped without a cluster."""

import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")

from desktop import diag_bundle  # noqa: E402
from desktop.p2p import diag_events  # noqa: E402

RAW = {"error": "open '/home/someone/Music/A Band/01 Song.flac' via 192.0.2.20 for someone@example.com",
       "step": "start_all"}
CLEAN = {"error": "open '…/01 Song.flac' via [private address] for [email]", "step": "start_all"}


@pytest.fixture
def conn(scratch_dsn):
    c = psycopg2.connect(scratch_dsn)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("DELETE FROM diag_events")
        cur.execute("DELETE FROM support_reports")
        cur.execute("DELETE FROM user_settings WHERE key IN "
                    "('support.diagnostics_enabled', 'p2p.reachability', 'p2p.reachability_detail')")
    yield c
    c.close()


def _details(conn, table: str) -> list:
    with conn.cursor() as cur:
        cur.execute(f"SELECT detail FROM {table} ORDER BY id")
        return [r[0] for r in cur.fetchall()]


def test_an_event_is_kept_scrubbed_whatever_its_writer_put_in_it(conn):
    error = FileNotFoundError(2, "No such file or directory", "/home/someone/x/y.box")
    diag_events.record(conn, "service.start_failed", {**RAW, "exc": error})
    [kept] = _details(conn, "diag_events")
    assert kept["error"] == CLEAN["error"]
    assert "someone" not in json.dumps(kept) and "…/y.box" in kept["exc"]


def test_what_was_kept_before_is_scrubbed_once_at_startup(conn):
    from db_migrate import _scrub_support_records
    with conn.cursor() as cur:
        cur.execute("INSERT INTO diag_events (kind, detail) VALUES ('sync.failed', %s)",
                    (json.dumps(RAW),))
        cur.execute("INSERT INTO diag_events (kind, detail) VALUES ('sync.failed', %s)",
                    (json.dumps({"error": "boom"}),))
        cur.execute("INSERT INTO support_reports (node_pubkey, kind, ts, detail) "
                    "VALUES (%s, 'sync.failed', now(), %s)", ("ab" * 32, json.dumps(RAW)))
        cur.execute("DELETE FROM diag_warrants")
        # a bundle collected before, staged, never uploaded
        cur.execute("INSERT INTO diag_warrants (id, issuer, issued_at, expires_at, scopes, "
                    "collected_at) VALUES (gen_random_uuid(), %s, now(), now() + interval '1 day', "
                    "'{logs}', now())", ("ab" * 32,))
    assert _scrub_support_records(conn) == {"diag_events": 1, "support_reports": 1,
                                            "staged_bundles_withdrawn": 1}
    assert diag_events.resumable_warrants(conn) == []
    assert _details(conn, "diag_events") == [CLEAN, {"error": "boom"}]
    assert _details(conn, "support_reports") == [CLEAN]


class _Backend:
    """The node's own backend API, answering what system_facts asks."""

    def _get_json(self, path, timeout=None):
        return {
            "/health": {"status": "degraded", "checks": {
                "database": "error: could not open '/home/someone/pgdata/postmaster.pid' at 192.0.2.20",
                "gpu": {"available": False},
                "music_library": {"path": "/home/someone/Music", "exists": True},
                "dht": {"available": True, "running": True, "nodes": 120},
            }},
        }.get(path)


def test_the_facts_sent_at_every_start_carry_states_not_paths_or_addresses(conn, monkeypatch):
    monkeypatch.setattr(diag_bundle, "_backend_client", lambda config: _Backend())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO user_settings (key, value) VALUES ('p2p.reachability', %s), "
                    "('p2p.reachability_detail', %s)",
                    (json.dumps("reachable"), json.dumps("wan 8.8.8.8:24001 via router 192.0.2.1")))
    facts = diag_bundle.system_facts({"ports": {"web": 18000}}, conn)
    text = json.dumps(facts)
    for gone in ("someone", "/Music", "192.0.2", "8.8.8.8", "reachability_detail"):
        assert gone not in text, gone
    assert facts["backend"] == {
        "status": "degraded",
        "database": "error: could not open '…/postmaster.pid' at [private address]",
        "music_library_exists": True,
        "dht": {"available": True, "running": True, "nodes": 120},
    }
    assert facts["settings"]["p2p.reachability"] == "reachable"
