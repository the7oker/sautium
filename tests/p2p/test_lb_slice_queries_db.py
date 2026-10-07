"""desktop/p2p/lb_slice_queries against a real PostgreSQL built from the
migrations (the test_life_merge pattern): the pending tiers and the version
rules, the serve matrix against min_version, the forward-only import,
backend/lb_dump_load's staging aggregation, and the status row both slice
cycles publish (lb_slice_cycle.publish_status). Skipped without a cluster."""

import base64
import gzip
import hashlib
import io
import json
import os
import select
import sys
import uuid
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

psycopg2.extras.register_uuid()

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from desktop.p2p import lb_slice_cycle  # noqa: E402
from desktop.p2p import lb_slice_queries as q  # noqa: E402

ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")

PG = dict(host=os.environ.get("SAUTIUM_TEST_PGHOST", "postgres"),
          port=int(os.environ.get("SAUTIUM_TEST_PGPORT", "5432")),
          user=os.environ.get("SAUTIUM_TEST_PGUSER", "musicai"),
          password=os.environ.get("SAUTIUM_TEST_PGPASSWORD", "supervisor"))
DBNAME = "sautium_lb_slice_test"

KEY = ed25519.Ed25519PrivateKey.generate()
PUB = KEY.public_key().public_bytes_raw().hex()

A_OWNED, A_ENGAGED, A_PHANTOM, A_ASKED = (uuid.UUID(int=n) for n in (1, 2, 3, 4))
M_OWNED, M_ENGAGED, M_PHANTOM, M_ASKED = (str(uuid.UUID(int=n)) for n in (11, 12, 13, 14))
T_OWNED, T_ENGAGED = uuid.UUID(int=21), uuid.UUID(int=22)
REC_1, REC_2 = str(uuid.UUID(int=31)), str(uuid.UUID(int=32))
V1, V2 = "20260901-000002", "20260915-000002"


@pytest.fixture(scope="module")
def conn():
    try:
        admin = psycopg2.connect(dbname="postgres", **PG)
    except psycopg2.OperationalError as e:
        pytest.skip(f"no PostgreSQL for the slice test: {e}")
    admin.autocommit = True
    from desktop import db_init, node_backup as nb
    nb._drop_database(admin, DBNAME)
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {DBNAME}")
    c = psycopg2.connect(dbname=DBNAME, **PG)
    db_init.apply_migrations(c)
    c.commit()
    c.autocommit = True
    _seed(c)
    yield c
    c.close()
    nb._drop_database(admin, DBNAME)
    admin.close()


def _seed(c):
    with c.cursor() as cur:
        for aid, name in ((A_OWNED, "Owned"), (A_ENGAGED, "Engaged"),
                          (A_PHANTOM, "Phantom"), (A_ASKED, "Asked")):
            cur.execute("INSERT INTO artists (id, name) VALUES (%s, %s)", (aid, name))
        for aid, mbid in ((A_OWNED, M_OWNED), (A_ENGAGED, M_ENGAGED),
                          (A_PHANTOM, M_PHANTOM), (A_ASKED, M_ASKED)):
            cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) "
                        "VALUES (%s, %s, 'phantom')", (mbid, aid))
        for tid, aid, title in ((T_OWNED, A_OWNED, "one"), (T_ENGAGED, A_ENGAGED, "two")):
            cur.execute("INSERT INTO tracks (id, title) VALUES (%s, %s)", (tid, title))
            cur.execute("INSERT INTO track_artists (track_id, artist_id, role) "
                        "VALUES (%s, %s, 'primary')", (tid, aid))
        cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'LP')", (uuid.UUID(int=41),))
        cur.execute("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, '/m/lp') "
                    "RETURNING id", (uuid.UUID(int=41),))
        variant = cur.fetchone()[0]
        cur.execute("""INSERT INTO media_files (track_id, album_variant_id, file_path)
                       VALUES (%s, %s, '/m/lp/one.flac')""", (T_OWNED, variant))
        cur.execute("""INSERT INTO listening_history (track_id, started_at, completed, skipped)
                       VALUES (%s, now(), true, false)""", (T_ENGAGED,))
        cur.execute("INSERT INTO lb_slice_requests (artist_id) VALUES (%s)", (A_ASKED,))


def _entry(mbid, version, recordings, artist=(10, 3)):
    one = {"dump_version": version, "artist": list(artist) if artist else None,
           "recordings": recordings, "truncated": False}
    blob = q.slice_blob(mbid, one)
    sig = KEY.sign(q.receipt_message_for(blob)).hex()
    return {"dump_version": version, "author_pubkey": PUB, "sig": sig,
            "blob_gz": base64.b64encode(gzip.compress(blob)).decode("ascii")}


def _import(c, mbid, entry, node="peer-a"):
    core, blob_gz = q.verify_slice_entry(mbid, entry)
    return q.import_slice(c, mbid, core, blob_gz, entry, node, None)


def test_pending_tiers_on_demand_then_owned_then_engaged_never_phantoms(conn):
    pending = q.pending_slice_mbids(conn, 200, None)
    assert [m for m, _ in pending] == [M_ASKED, M_OWNED, M_ENGAGED]
    assert all(v is None for _, v in pending)


def test_a_ledger_row_closes_an_mbid_only_at_its_version(conn):
    n = _import(conn, M_OWNED, _entry(M_OWNED, V1, [[REC_1, 5, 2, [M_OWNED]]]))
    assert n == 1
    assert [m for m, _ in q.pending_slice_mbids(conn, 200, None)] == [M_ASKED, M_ENGAGED]
    assert [m for m, _ in q.pending_slice_mbids(conn, 200, V1)] == [M_ASKED, M_ENGAGED]
    stale = q.pending_slice_mbids(conn, 200, V2)
    assert (M_OWNED, V1) in stale


def test_a_signed_zero_match_is_closed_and_reopened_by_a_newer_version(conn):
    _import(conn, M_ENGAGED, _entry(M_ENGAGED, V1, [], artist=None))
    with conn.cursor() as cur:
        cur.execute("SELECT recordings FROM lb_slice_fetches WHERE artist_mbid = %s", (M_ENGAGED,))
        assert cur.fetchone()[0] == 0
    assert M_ENGAGED not in [m for m, _ in q.pending_slice_mbids(conn, 200, V1)]
    assert (M_ENGAGED, V1) in q.pending_slice_mbids(conn, 200, V2)


def test_import_answers_the_on_demand_request(conn):
    _import(conn, M_ASKED, _entry(M_ASKED, V1, [[REC_2, 7, 4, [M_ASKED]]]))
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM lb_slice_requests")
        assert cur.fetchone()[0] == 0
    assert M_ASKED not in [m for m, _ in q.pending_slice_mbids(conn, 200, V1)]


def test_imports_move_rows_forward_only_in_any_arrival_order(conn):
    # V2 first, then V1 for the same recording (credited to a second artist)
    _import(conn, M_OWNED, _entry(M_OWNED, V2, [[REC_1, 50, 20, [M_OWNED, M_PHANTOM]]]))
    _import(conn, M_PHANTOM, _entry(M_PHANTOM, V1, [[REC_1, 5, 2, [M_OWNED, M_PHANTOM]]]))
    with conn.cursor() as cur:
        cur.execute("SELECT listen_count, user_count, dump_version FROM lb_recording "
                    "WHERE recording_mbid = %s", (REC_1,))
        assert cur.fetchone() == (50, 20, V2)
        cur.execute("SELECT dump_version FROM lb_slice_fetches WHERE artist_mbid = %s", (M_OWNED,))
        assert cur.fetchone()[0] == V2
        cur.execute("SELECT dump_version FROM lb_slice_blobs WHERE artist_mbid = %s", (M_OWNED,))
        assert cur.fetchone()[0] == V2


def test_serve_replica_matrix_against_min_version(conn):
    # This node holds no dump: it re-serves M_OWNED at V2 and M_PHANTOM at V1.
    out = q.serve_slices(conn, [M_OWNED, M_PHANTOM, M_ENGAGED], None, None, "")
    assert set(out["slices"]) == {M_OWNED, M_PHANTOM, M_ENGAGED}
    out = q.serve_slices(conn, [M_OWNED, M_PHANTOM], V2, None, "")
    assert set(out["slices"]) == {M_OWNED} and out["missing"] == [M_PHANTOM]
    assert q.verify_slice_entry(M_OWNED, out["slices"][M_OWNED], V2) is not None
    with pytest.raises(ValueError):
        q.serve_slices(conn, ["nope"], None, None, "")
    with pytest.raises(ValueError):
        q.serve_slices(conn, [], None, None, "")


def test_a_dump_node_builds_signs_and_never_serves_a_cache_older_than_its_dump(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO user_settings (key, value) VALUES ('listenbrainz.db_version', %s::jsonb)",
                    (json.dumps(V2),))
        cur.execute("""INSERT INTO lb_recording (recording_mbid, listen_count, user_count, artist_mbids, dump_version)
                       VALUES (%s, 9, 9, %s::uuid[], %s)""",
                    (str(uuid.UUID(int=33)), [M_PHANTOM], V2))
        cur.execute("""INSERT INTO lb_artist (artist_mbid, listen_count, user_count, dump_version)
                       VALUES (%s, 100, 40, %s)
                       ON CONFLICT (artist_mbid) DO UPDATE SET listen_count = 100, user_count = 40,
                                                              dump_version = EXCLUDED.dump_version""",
                    (M_PHANTOM, V2))
    assert q.local_dump_available(conn) == V2
    # M_PHANTOM's cache is at V1 (a replica import) — rebuilt at V2 from the dump.
    out = q.serve_slices(conn, [M_PHANTOM], None, KEY.sign, PUB)
    entry = out["slices"][M_PHANTOM]
    assert entry["dump_version"] == V2 and entry["author_pubkey"] == PUB
    core, _ = q.verify_slice_entry(M_PHANTOM, entry, V2)
    assert core["artist"] == [100, 40]
    assert {r[0] for r in core["recordings"]} == {REC_1, str(uuid.UUID(int=33))}
    # A version above the dump's is a miss even on a dump node.
    out = q.serve_slices(conn, [M_PHANTOM], "20261001-000000", KEY.sign, PUB)
    assert out["missing"] == [M_PHANTOM]


def test_loader_aggregation_is_a_lower_bound_sum_over_users(conn):
    """backend/lb_dump_load: the staging COPY + GROUP BY, through the real
    aggregate (min(uuid[]) included), then the swap."""
    import lb_dump_load as L
    docs = [
        {"user_id": 1, "data": [
            {"recording_mbid": REC_1, "listen_count": 3, "artist_mbids": [M_OWNED]},
            {"recording_mbid": REC_2, "listen_count": 1, "artist_mbids": [M_ASKED]}]},
        {"user_id": 2, "data": [
            {"recording_mbid": REC_1, "listen_count": 4, "artist_mbids": [M_OWNED]},
            {"recording_mbid": None, "listen_count": 99}]},
    ]
    artists = [{"user_id": 1, "data": [{"artist_mbid": M_OWNED, "listen_count": 4}]},
               {"user_id": 2, "data": [{"artist_mbid": M_OWNED, "listen_count": 4},
                                       {"artist_mbid": None, "listen_count": 1}]}]
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS lb_stage_recording, lb_stage_artist")
        cur.execute("CREATE UNLOGGED TABLE lb_stage_recording (recording_mbid uuid NOT NULL, "
                    "listen_count bigint NOT NULL, artist_mbids uuid[] NOT NULL)")
        cur.execute("CREATE UNLOGGED TABLE lb_stage_artist (artist_mbid uuid NOT NULL, "
                    "listen_count bigint NOT NULL)")
        jsonl = lambda ds: io.BytesIO(b"".join(json.dumps(d).encode() + b"\n" for d in ds))
        cur.copy_expert("COPY lb_stage_recording FROM STDIN", L.ItemRows(jsonl(docs), L.recording_row))
        cur.copy_expert("COPY lb_stage_artist FROM STDIN", L.ItemRows(jsonl(artists), L.artist_row))
    L._aggregate(conn, "20261001-000000", lambda u: None)
    with conn.cursor() as cur:
        cur.execute("SELECT listen_count, user_count, artist_mbids::text[], dump_version "
                    "FROM lb_recording WHERE recording_mbid = %s", (REC_1,))
        assert cur.fetchone() == (7, 2, [M_OWNED], "20261001-000000")
        cur.execute("SELECT count(*) FROM lb_recording")
        assert cur.fetchone()[0] == 2            # the old rows are gone with the swap
        cur.execute("SELECT listen_count, user_count FROM lb_artist WHERE artist_mbid = %s", (M_OWNED,))
        assert cur.fetchone() == (8, 2)
        cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'lb_recording' ORDER BY 1")
        assert [r[0] for r in cur.fetchall()] == ["idx_lb_recording_artists", "lb_recording_pkey"]
        cur.execute("SELECT to_regclass('lb_stage_recording')")
        assert cur.fetchone()[0] is None


def _wakes(listener, conn, action) -> int:
    """The sautium_notices wakes `action` sent, counted up to a sentinel
    NOTIFY committed after it — an absent wake is proven, not waited out."""
    action()
    with conn.cursor() as cur:
        cur.execute("NOTIFY sautium_notices, 'sentinel'")
    count = 0
    while True:
        assert select.select([listener], [], [], 5)[0], "the sentinel never arrived"
        listener.poll()
        while listener.notifies:
            if listener.notifies.pop(0).payload == "sentinel":
                return count
            count += 1


def test_status_row_keeps_the_onset_and_wakes_only_on_change(conn):
    """The derived notice toasts when its `since` moves: a run that leaves
    the same artists waiting must neither move it nor wake the channel. A
    dump node drops the row (and the notice with it) once."""
    key = lb_slice_cycle.STATUS_KEY
    dsn = psycopg2.extensions.make_dsn(dbname=DBNAME, **PG)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM user_settings WHERE key = %s", (key,))
    listener = psycopg2.connect(dbname=DBNAME, **PG)
    listener.autocommit = True
    with listener.cursor() as cur:
        cur.execute("LISTEN sautium_notices")

    def publish(**facts) -> int:
        state = {"pending": 200, "pending_capped": True, "served": 0, "unserved": 200,
                 "reason": "no_sources", "sources": 0, "newest_version": None,
                 "next_attempt_at": None, **facts}
        return _wakes(listener, conn,
                      lambda: lb_slice_cycle.publish_status(dsn, key, state))

    def since():
        with conn.cursor() as cur:
            cur.execute("SELECT value->>'since' FROM user_settings WHERE key = %s", (key,))
            return cur.fetchone()[0]

    try:
        assert publish() == 1
        onset = since()
        assert onset
        assert publish() == 0                    # a wake that changed nothing
        assert since() == onset
        assert publish(reason="rate_limited", sources=1) == 1
        assert since() == onset                  # still waiting: the same condition
        assert publish(served=200, unserved=0, reason="ok") == 1
        assert since() is None
        assert publish() == 1
        assert since() not in (None, onset)      # waiting again is a new onset
        clear = lambda: lb_slice_cycle.clear_status(dsn, key)
        assert _wakes(listener, conn, clear) == 1
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM user_settings WHERE key = %s", (key,))
            assert cur.fetchone() is None
        assert _wakes(listener, conn, clear) == 0
    finally:
        listener.close()
