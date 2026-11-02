"""What a dump node builds and keeps for strangers (desktop/p2p/
mb_slice_queries.serve_slices, desktop/p2p/lb_slice_queries.serve_slices),
on a real PostgreSQL built from the migrations — skipped without a cluster:
one subtree per match set, whatever the spelling asked, and only a few
spellings kept; a zero-match served signed and never kept; a name past the
rules or the build budget is `missing`, with no build; a blob cut from an
earlier dump is rebuilt; LB builds share the loader's lock among
themselves."""

import base64
import os
import sys
import threading
import uuid
from pathlib import Path

import psycopg2
import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from desktop.p2p import lb_slice_queries as lb  # noqa: E402
from desktop.p2p import mb_slice_queries as q  # noqa: E402

ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")

PG = dict(host=os.environ.get("SAUTIUM_TEST_PGHOST", "postgres"),
          port=int(os.environ.get("SAUTIUM_TEST_PGPORT", "5432")),
          user=os.environ.get("SAUTIUM_TEST_PGUSER", "musicai"),
          password=os.environ.get("SAUTIUM_TEST_PGPASSWORD", "supervisor"))
DBNAME = "sautium_slice_serving_test"
KEY = ed25519.Ed25519PrivateKey.generate()
PUB = KEY.public_key().public_bytes_raw().hex()
VERSION = "20261101-001500"


@pytest.fixture(scope="module")
def dsn():
    try:
        admin = psycopg2.connect(dbname="postgres", **PG)
    except psycopg2.OperationalError as e:
        pytest.skip(f"no PostgreSQL for the slice serving test: {e}")
    admin.autocommit = True
    from desktop import db_init, node_backup as nb
    nb._drop_database(admin, DBNAME)
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {DBNAME}")
    c = psycopg2.connect(dbname=DBNAME, **PG)
    db_init.apply_migrations(c)
    c.commit()
    with c.cursor() as cur:
        cur.execute("""INSERT INTO mb_artist (id, gid, name, sort_name)
                       VALUES (1, %s, 'Sigur Rós', 'Sigur Rós')""", (str(uuid.uuid4()),))
        cur.execute("""INSERT INTO mb_artist (id, gid, name, sort_name)
                       VALUES (2, %s, 'Simon & Garfunkel', 'Simon & Garfunkel')""", (str(uuid.uuid4()),))
        cur.execute("""INSERT INTO user_settings (key, value)
                       VALUES ('listenbrainz.db_version', %s::jsonb)""", (f'"{VERSION}"',))
        cur.execute("""INSERT INTO lb_recording (recording_mbid, listen_count, user_count,
                                                 artist_mbids, dump_version)
                       VALUES (%s, 10, 3, ARRAY[%s::uuid], %s)""",
                    (str(uuid.uuid4()), str(uuid.UUID(int=7)), VERSION))
    c.commit()
    c.close()
    yield f"postgresql://{PG['user']}:{PG['password']}@{PG['host']}:{PG['port']}/{DBNAME}"
    nb._drop_database(admin, DBNAME)
    admin.close()


@pytest.fixture
def conn(dsn):
    c = psycopg2.connect(dsn)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("DELETE FROM mb_slice_blobs")
        cur.execute("DELETE FROM lb_slice_blobs")
    yield c
    c.close()


@pytest.fixture
def dump_node(monkeypatch):
    """A node holding the MB dump at VERSION, its builds counted."""
    monkeypatch.setattr(q, "local_dump_available", lambda conn: VERSION)
    monkeypatch.setattr(q, "dump_version_file", lambda: VERSION)
    monkeypatch.setattr(q, "_builds", q.BuildBudget(q.BUILD_CONCURRENCY, q.BUILDS_PER_MINUTE))
    builds = []
    real = q.get_slice_one
    monkeypatch.setattr(q, "get_slice_one",
                        lambda conn, name, ids=None: builds.append(name) or real(conn, name, ids))
    return builds


def _serve(conn, names):
    return q.serve_slices(conn, names, KEY.sign, PUB)


def _kept(conn) -> list:
    with conn.cursor() as cur:
        cur.execute("SELECT name_key FROM mb_slice_blobs ORDER BY name_key")
        return [r[0] for r in cur.fetchall()]


def _verifies(name, entry) -> bool:
    return q.verify_slice_entry(name, entry) is not None


def test_one_subtree_per_match_set_and_a_few_spellings_kept(conn, dump_node, monkeypatch):
    monkeypatch.setattr(q, "VARIANTS_PER_MATCH", 2)
    spellings = ["Sigur Rós", "sigur ros", "SIGUR  RÓS", "Sigur Ros", "sigur rós"]
    out = _serve(conn, spellings)
    assert out["missing"] == []
    assert all(_verifies(n, out["slices"][n]) for n in spellings)
    assert dump_node == ["Sigur Rós"]                     # one build for the five
    assert len(_kept(conn)) == 2
    again = _serve(conn, ["sigur ros"])                     # a kept spelling: no work
    assert _verifies("sigur ros", again["slices"]["sigur ros"]) and dump_node == ["Sigur Rós"]


def test_a_zero_match_is_served_signed_and_never_kept(conn, dump_node):
    out = _serve(conn, ["Nobody By This Name", "Nobody At All"])
    assert out["missing"] == []
    assert all(_verifies(n, e) for n, e in out["slices"].items())
    assert _kept(conn) == []


@pytest.mark.parametrize("name", ["x" * 256, "Bell\u0007", "[unknown]", "Various Artists"])
def test_a_name_past_the_rules_is_missing_with_no_build(conn, dump_node, name):
    out = _serve(conn, [name, "Simon & Garfunkel"])
    assert out["missing"] == [name] and "Simon & Garfunkel" in out["slices"]
    assert dump_node == ["Simon & Garfunkel"]


def test_a_build_past_the_budget_is_missing(conn, dump_node, monkeypatch):
    monkeypatch.setattr(q, "_builds", q.BuildBudget(1, 2))
    names = [f"Unknown Name {i}" for i in range(5)]
    out = _serve(conn, names)
    assert sorted(out["slices"]) == names[:2] and out["missing"] == names[2:]


def test_a_blob_cut_from_an_earlier_dump_is_rebuilt(conn, dump_node):
    one = q.get_slice_one(conn, "Sigur Rós")
    one["dump_version"] = "20200101-000000"
    blob = q.name_blob("Sigur Rós", one)
    import gzip
    q.store_slice_blob(conn, q.name_key("Sigur Rós"), "20200101-000000", PUB,
                       KEY.sign(q.receipt_message_for(blob)).hex(), gzip.compress(blob), cap=False)
    dump_node.clear()
    out = _serve(conn, ["Sigur Rós"])
    assert dump_node == ["Sigur Rós"]
    assert out["slices"]["Sigur Rós"]["dump_version"] == VERSION
    with conn.cursor() as cur:
        cur.execute("SELECT dump_version, match_key FROM mb_slice_blobs")
        assert cur.fetchall() == [(VERSION, q.match_key([1]))]


def test_an_mbid_listenbrainz_does_not_know_is_served_and_never_kept(conn):
    unknown, known = str(uuid.UUID(int=99)), str(uuid.UUID(int=7))
    out = lb.serve_slices(conn, [unknown, known], None, KEY.sign, PUB)
    assert set(out["slices"]) == {unknown, known} and out["missing"] == []
    assert lb.verify_slice_entry(unknown, out["slices"][unknown]) is not None
    with conn.cursor() as cur:
        cur.execute("SELECT artist_mbid::text FROM lb_slice_blobs")
        assert [r[0] for r in cur.fetchall()] == [known]


def test_lb_builds_share_the_loaders_lock_among_themselves(conn, dsn):
    other = psycopg2.connect(dsn)
    other.autocommit = True
    try:
        with other.cursor() as cur:                          # a build under way elsewhere
            cur.execute("SELECT pg_advisory_lock_shared(%s)", (lb.LB_LOAD_LOCK_KEY,))
        assert lb.get_slice_one(conn, str(uuid.UUID(int=7)), VERSION)["recordings"]
        with other.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock_shared(%s)", (lb.LB_LOAD_LOCK_KEY,))
            cur.execute("SELECT pg_advisory_lock(%s)", (lb.LB_LOAD_LOCK_KEY,))   # the loader
        with pytest.raises(lb.DumpBusy):
            lb.get_slice_one(conn, str(uuid.UUID(int=7)), VERSION)
    finally:
        other.close()
