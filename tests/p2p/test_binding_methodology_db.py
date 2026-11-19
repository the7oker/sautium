"""A track_mbid binding carries the matcher's methodology that made it
(record_sig track_mbid v3, MATCHER_VERSION; CANON-CLASSICAL-DESIGN §5.4):
the import gate refuses a binding of a methodology this release does not
compute, an imported binding of an older methodology is replaced by a newer
one — never this node's own — and the cutover at the first start deletes
every imported binding and sheds the own ones' seals for the notary.

Before this slice the importer was ON CONFLICT DO NOTHING and the planner
asked only for a track with no binding at all, so an author's corrected
binding never reached a peer that held the old one."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
if str(REPO / "backend") not in sys.path:
    sys.path.insert(0, str(REPO / "backend"))

from desktop.p2p import record_sig as rs, sync_queries  # noqa: E402
from tests.p2p.sealed_records import pub  # noqa: E402
from tests.p2p.test_witness_carried_db import (  # noqa: E402,F401
    _Peer, _key, _pull, _served, _witnessed, conn, no_evaluation, pool, recorder, trusted)


def _row(conn, recording):
    with conn.cursor() as cur:
        cur.execute("SELECT track_id::text, matcher_version, imported, signature IS NOT NULL "
                    "FROM track_mbids WHERE recording_mbid = %s", (recording,))
        return cur.fetchone()


def test_a_binding_of_a_methodology_no_release_computes_is_refused(conn, recorder, scratch_dsn):
    author = _key()
    _witnessed(recorder, author)
    track, recording, payload = _served(conn, author, "track_mbids")
    assert payload["items"][0]["matcher_version"] == rs.MATCHER_VERSION
    payload["items"][0]["matcher_version"] = rs.MATCHER_VERSION + 1
    assert _pull(scratch_dsn, _Peer(pub(_key()), recorder), "track_mbids", track, payload) == 0
    del payload["items"][0]["matcher_version"]                    # the format before v3
    assert _pull(scratch_dsn, _Peer(pub(_key()), recorder), "track_mbids", track, payload) == 0
    assert _row(conn, recording) is None


def test_a_newer_methodology_replaces_an_imported_older_one_never_an_own(conn, recorder, scratch_dsn):
    author = _key()
    _witnessed(recorder, author)
    track, recording, payload = _served(conn, author, "track_mbids")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO tracks (id, title) VALUES (gen_random_uuid(), 'stale') RETURNING id::text")
        stale = cur.fetchone()[0]
        cur.execute("INSERT INTO track_mbids (recording_mbid, track_id, confidence, matcher_version, imported, "
                    "signature) VALUES (%s, %s, 'overlap_verified', 1, TRUE, repeat('b', 128))",
                    (recording, stale))
        # the planner: an imported binding of an older matcher is a gap
        cur.execute(f"SELECT {sync_queries._LACKS['track_mbids'].format(t='%s::uuid')}", (stale,))
        assert cur.fetchone() == (True,)
    assert _pull(scratch_dsn, _Peer(pub(_key()), recorder), "track_mbids", track, payload) == 1
    assert _row(conn, recording) == (track, rs.MATCHER_VERSION, True, True)
    with conn.cursor() as cur:
        cur.execute("UPDATE track_mbids SET track_id = %s, matcher_version = 1, imported = FALSE "
                    "WHERE recording_mbid = %s", (stale, recording))
    _pull(scratch_dsn, _Peer(pub(_key()), recorder), "track_mbids", track, payload)
    assert _row(conn, recording)[:3] == (stale, 1, False)        # this node's own stays


def test_the_cutover_deletes_imported_bindings_and_sheds_own_seals(conn):
    import db_migrate
    with conn.cursor() as cur:
        cur.execute("DELETE FROM track_mbids")
        cur.execute("INSERT INTO tracks (id, title) VALUES (gen_random_uuid(), 'x') RETURNING id::text")
        t = cur.fetchone()[0]
        cur.execute("""INSERT INTO track_mbids (recording_mbid, track_id, confidence, imported, signature)
                       VALUES (gen_random_uuid(), %s, 'overlap_verified', TRUE, repeat('a', 128)),
                              (gen_random_uuid(), %s, 'overlap_verified', FALSE, repeat('c', 128))""", (t, t))
    assert db_migrate._cut_over_bindings(conn) == {"imported_deleted": 1, "own_seals_shed": 1}
    with conn.cursor() as cur:
        cur.execute("SELECT imported, signature FROM track_mbids WHERE track_id = %s", (t,))
        assert cur.fetchall() == [(False, None)]
