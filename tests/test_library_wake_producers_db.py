"""A producer that changes what the Library shows says so, on a real
PostgreSQL (PI-NODE-2D, B-138's second part).

While the load meter spoke on sautium_identity every two seconds, every page
read its state again that often, and state with no event of its own looked
live. With that gone each producer says its own change:

- records an import lands — a pull's batch, a peer's push, a share file —
  change the Library's coverage and what the guidance trail asks to analyse:
  the import gate says them with the commit that makes them visible, once a
  batch (NOTIFY sautium_library), in whichever process it runs;
- a file's structural rows (the streaming library's footprint) the same way;
- a rotation clears the email's verification, which the identity line and the
  trail read: the write says it (NOTIFY sautium_identity), not the workers'
  restarts, which publish when they get to it, or never.
"""

import select
import sys
import uuid

import pytest

from tests.conftest import REPO_ROOT

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.pool  # noqa: E402

BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from desktop import sync_client  # noqa: E402
from tests.test_records_v4_wire_db import (AUTHOR, _import, _track, _v4_features,  # noqa: E402,F401
                                           conn, trusted)


@pytest.fixture
def heard(scratch_dsn):
    """What a backend's listener would hear, channel by channel."""
    listener = psycopg2.connect(scratch_dsn)
    listener.autocommit = True
    with listener.cursor() as c:
        for channel in ("sautium_library", "sautium_sync_done", "sautium_identity",
                        "sautium_identity_rotated", "sautium_notices"):
            c.execute(f"LISTEN {channel}")

    def notifies() -> list:
        while select.select([listener], [], [], 0.5)[0]:
            listener.poll()
        listener.poll()
        said, listener.notifies[:] = [n.channel for n in listener.notifies], []
        return said
    yield notifies
    listener.close()


def _on_a_tracklist(cur, *tracks) -> None:
    album = str(uuid.uuid4())
    cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'LP')", (album,))
    for n, track in enumerate(tracks, 1):
        cur.execute("""INSERT INTO album_tracks (album_id, track_id, position, recording_mbid)
                       VALUES (%s, %s, %s, %s)""", (album, track, n, str(uuid.uuid4())))


def test_records_a_pull_lands_are_said_once_a_batch(conn, scratch_dsn, heard):
    with conn.cursor() as cur:
        one, two = _track(cur), _track(cur)
    payloads = [_v4_features(AUTHOR, track) for track in (one, two)]
    batch = {"items": [i for p in payloads for i in p["items"]], "authors": {},
             "batches": {r: b for p in payloads for r, b in p["batches"].items()}}
    assert _import(scratch_dsn, "audio_features", batch) == 2
    assert heard() == ["sautium_library"]                  # two records, one batch, one word
    # the same batch again lands nothing (the node holds them): nothing is said
    assert _import(scratch_dsn, "audio_features", batch) == 0
    assert heard() == []


def test_records_a_peer_pushed_are_said_and_a_push_that_lands_nothing_is_not(conn, scratch_dsn, heard):
    with conn.cursor() as cur:
        wanted, unasked = _track(cur), _track(cur)
        _on_a_tracklist(cur, wanted)
    assert sync_client.import_pushed(scratch_dsn, "audio_features", _v4_features(AUTHOR, wanted), 100) == 1
    assert heard() == ["sautium_library"]
    # off this node's tracklists: the push is cut to nothing before the gate
    assert sync_client.import_pushed(scratch_dsn, "audio_features", _v4_features(AUTHOR, unasked), 100) == 0
    # a record whose seal does not hold: the gate drops it
    forged = _v4_features(AUTHOR, wanted, version=2, bpm=99.0)
    forged["items"][0]["bpm"] = 100.0
    with conn.cursor() as cur:
        cur.execute("DELETE FROM audio_features")
    assert sync_client.import_pushed(scratch_dsn, "audio_features", forged, 100) == 0
    assert heard() == []


def test_a_files_structural_rows_are_said_with_their_commit(conn, scratch_dsn, heard):
    import seed_import
    artist = {"id": str(uuid.uuid4()), "name": "An Artist"}
    writer = psycopg2.connect(scratch_dsn)
    try:
        seed_import.insert_structural(writer, {"artists": [artist]})
    finally:
        writer.close()
    assert heard() == ["sautium_library"]
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM artists WHERE id = %s", (artist["id"],))
        assert cur.fetchone() == ("An Artist",)


def test_a_rotation_says_the_emails_verification_is_gone(conn, scratch_dsn, heard, monkeypatch):
    """Whatever the workers' restarts do, and when: the page that made the
    rotation holds its stream, and its trail leads to the verification."""
    import db_pool
    import device_auth
    import p2p_chat
    import p2p_identity
    from desktop.p2p import master_node
    from routers import p2p as p2p_router
    pool = psycopg2.pool.ThreadedConnectionPool(1, 3, dsn=scratch_dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    restarts = []
    monkeypatch.setattr(p2p_chat, "soon", lambda coro: (restarts.append(coro.__name__), coro.close()))
    monkeypatch.setattr(p2p_chat, "forget_service", lambda: None)
    monkeypatch.setattr(p2p_identity, "forget_cached", lambda: None)
    monkeypatch.setattr(p2p_router, "forget_identity", lambda: None)
    monkeypatch.setattr(master_node, "decide", lambda pubkey: None)
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO user_profile (id, email_verified) VALUES (1, TRUE)
                           ON CONFLICT (id) DO UPDATE SET email_verified = TRUE""")
        device_auth._after_rotation("ab" * 32)
        assert heard() == ["sautium_identity", "sautium_identity_rotated"]
        assert restarts == ["restart_client", "restart_identity_worker", "changed"]
        with conn.cursor() as cur:
            cur.execute("SELECT email_verified FROM user_profile WHERE id = 1")
            assert cur.fetchone() == (False,)
    finally:
        pool.closeall()
