"""What a dump node builds and keeps for strangers (desktop/p2p/
mb_slice_queries.serve_slices, desktop/p2p/lb_slice_queries.serve_slices),
on a real PostgreSQL built from the migrations — skipped without a cluster:
one subtree per match set, whatever the spelling asked, and only a few
spellings kept, each the blob its own build would make; a zero-match served
signed and never kept; a name past the rules or the build budget is
`missing`, with no build — only a build that found something counts, and a
requester has a share of the minute; a name_key is answered once and an
answer is bounded; a blob cut from an earlier dump is rebuilt and replaced;
LB builds share the loader's lock among themselves."""

import base64
import os
import sys
import threading
import uuid
from pathlib import Path

import psycopg2
import pytest

REPO = Path(__file__).resolve().parents[2]
for _p in (REPO, REPO / "backend"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from desktop.p2p import lb_slice_queries as lb  # noqa: E402
from desktop.p2p import mb_slice_queries as q  # noqa: E402

BUILD = q.get_slice_one

ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")

PG = dict(host=os.environ.get("SAUTIUM_TEST_PGHOST", "postgres"),
          port=int(os.environ.get("SAUTIUM_TEST_PGPORT", "5432")),
          user=os.environ.get("SAUTIUM_TEST_PGUSER", "musicai"),
          password=os.environ.get("SAUTIUM_TEST_PGPASSWORD", "supervisor"))
DBNAME = "sautium_slice_serving_test"
KEY = ed25519.Ed25519PrivateKey.generate()
PUB = KEY.public_key().public_bytes_raw().hex()


@pytest.fixture(autouse=True)
def _author(monkeypatch):
    """The test key is the slice author the release names."""
    from desktop.p2p import master_node
    monkeypatch.setattr(master_node, "SLICE_AUTHORS", (PUB,))
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


def _serve(conn, names, requester=""):
    return q.serve_slices(conn, names, KEY.sign, PUB, requester)


def _blob(entry) -> bytes:
    import gzip
    return gzip.decompress(base64.b64decode(entry["blob_gz"]))


def _kept(conn) -> list:
    with conn.cursor() as cur:
        cur.execute("SELECT name_key FROM mb_slice_blobs ORDER BY name_key")
        return [r[0] for r in cur.fetchall()]


def _verifies(name, entry) -> bool:
    return q.verify_slice_entry(name, entry) is not None


def test_one_subtree_per_match_set_and_a_few_spellings_kept(conn, dump_node, monkeypatch):
    monkeypatch.setattr(q, "VARIANTS_PER_MATCH", 2)
    spellings = ["Sigur Rós", "sigur ros", "SIGUR  RÓS"]              # three name_keys, one match set
    out = _serve(conn, spellings)
    assert out["missing"] == []
    assert all(_verifies(n, out["slices"][n]) for n in spellings)
    assert dump_node == ["Sigur Rós"]                     # one build for the three
    assert len(_kept(conn)) == 2
    for name in spellings:                                # a spelling cut from the stored subtree
        assert _blob(out["slices"][name]) == q.name_blob(name, BUILD(conn, name))
    again = _serve(conn, ["sigur ros"])                     # a kept spelling: no work
    assert _verifies("sigur ros", again["slices"]["sigur ros"]) and dump_node == ["Sigur Rós"]


def test_a_name_key_is_answered_once_and_an_answer_is_bounded(conn, dump_node, monkeypatch):
    out = _serve(conn, ["Sigur Rós", "sigur rós", "Simon & Garfunkel"])
    assert sorted(out["slices"]) == ["Sigur Rós", "Simon & Garfunkel"]
    assert out["missing"] == ["sigur rós"]                # one name_key, asked again later
    monkeypatch.setattr(q, "ANSWER_MAX_BYTES", 1)
    out = _serve(conn, ["Sigur Rós", "Simon & Garfunkel"])
    assert list(out["slices"]) == ["Sigur Rós"] and out["missing"] == ["Simon & Garfunkel"]


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
    monkeypatch.setattr(q, "_builds", q.BuildBudget(1, 4))          # one a minute for any one requester
    nobody = [f"Unknown Name {i}" for i in range(5)]
    out = _serve(conn, nobody + ["Sigur Rós", "Simon & Garfunkel"], "a")
    assert sorted(out["slices"]) == sorted(nobody + ["Sigur Rós"])     # a zero-match costs no build
    assert out["missing"] == ["Simon & Garfunkel"]                    # a's share is spent
    assert list(_serve(conn, ["Simon & Garfunkel"], "b")["slices"]) == ["Simon & Garfunkel"]
    with conn.cursor() as cur:
        cur.execute("DELETE FROM mb_slice_blobs")
    monkeypatch.setattr(q, "_builds", q.BuildBudget(1, 1))
    assert list(_serve(conn, ["Sigur Rós"], "a")["slices"]) == ["Sigur Rós"]
    assert _serve(conn, ["Simon & Garfunkel"], "b")["missing"] == ["Simon & Garfunkel"]   # the minute is spent


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


def test_an_earlier_dumps_blob_is_replaced_when_its_match_set_is_full(conn, dump_node, monkeypatch):
    import gzip
    monkeypatch.setattr(q, "VARIANTS_PER_MATCH", 1)
    _serve(conn, ["Sigur Rós"])                                          # the one spelling kept
    one = BUILD(conn, "sigur ros")
    one["dump_version"] = "20200101-000000"
    blob = q.name_blob("sigur ros", one)
    q.store_slice_blob(conn, q.name_key("sigur ros"), "20200101-000000", PUB,
                       KEY.sign(q.receipt_message_for(blob)).hex(), gzip.compress(blob), cap=False)
    dump_node.clear()
    assert _serve(conn, ["sigur ros"])["slices"]["sigur ros"]["dump_version"] == VERSION
    assert dump_node == []                                               # cut from the kept subtree
    with conn.cursor() as cur:
        cur.execute("SELECT dump_version FROM mb_slice_blobs WHERE name_key = 'sigur ros'")
        assert cur.fetchall() == [(VERSION,)]


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


def test_what_slices_of_an_unnamed_key_left_is_dropped(conn, monkeypatch):
    import db_migrate
    import master_node as backend_master
    import p2p_identity
    own, stranger = "cd" * 32, "ef" * 32
    monkeypatch.setattr(backend_master, "SLICE_AUTHORS", (PUB,))
    monkeypatch.setattr(p2p_identity, "resolve_identity", lambda s: {"public_key_hex": own})
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO mb_slice_fetches (name_key, source_pubkey) VALUES
                       ('by author', %s), ('by stranger', %s), ('before keys', NULL)""",
                    (PUB.upper(), stranger))
        cur.execute("""INSERT INTO lb_slice_fetches (artist_mbid, dump_version, source_pubkey) VALUES
                       (%s, %s, %s), (%s, %s, %s), (%s, '29991231-000000', %s)""",
                    (str(uuid.UUID(int=31)), VERSION, PUB, str(uuid.UUID(int=32)), VERSION, stranger,
                     str(uuid.UUID(int=33)), PUB))
        for key, signer in (("a", PUB), ("o", own), ("s", stranger)):
            cur.execute("""INSERT INTO mb_slice_blobs (name_key, dump_version, author_pubkey, sig, blob_gz)
                           VALUES (%s, %s, %s, %s, '\\x00')""", (key, VERSION, signer, "00" * 64))
        cur.execute("""INSERT INTO lb_recording (recording_mbid, listen_count, user_count, artist_mbids,
                                                 dump_version)
                       VALUES (%s, 1, 1, ARRAY[%s::uuid], '29991231-000000')""",
                    (str(uuid.UUID(int=34)), str(uuid.UUID(int=7))))
        # What the stranger's slice for artist 32 gave, under a plausible version:
        # its own recording goes, one credited to artist 31 as well stays.
        cur.execute("""INSERT INTO lb_artist (artist_mbid, listen_count, user_count, dump_version)
                       VALUES (%s, 9, 9, %s), (%s, 1, 1, %s)""",
                    (str(uuid.UUID(int=32)), VERSION, str(uuid.UUID(int=31)), VERSION))
        cur.execute("""INSERT INTO lb_recording (recording_mbid, listen_count, user_count, artist_mbids,
                                                 dump_version)
                       VALUES (%s, 9, 9, ARRAY[%s::uuid], %s), (%s, 9, 9, ARRAY[%s::uuid, %s::uuid], %s)""",
                    (str(uuid.UUID(int=35)), str(uuid.UUID(int=32)), VERSION,
                     str(uuid.UUID(int=36)), str(uuid.UUID(int=32)), str(uuid.UUID(int=31)), VERSION))
        cur.execute("""INSERT INTO lb_slice_blobs (artist_mbid, dump_version, author_pubkey, sig, blob_gz)
                       VALUES (%s, %s, %s, %s, '\\x00'), (%s, '29991231-000000', %s, %s, '\\x00')""",
                    (str(uuid.UUID(int=37)), VERSION, stranger, "00" * 64,
                     str(uuid.UUID(int=38)), PUB, "00" * 64))
    db_migrate._drop_unauthored_slices(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT name_key FROM mb_slice_fetches ORDER BY name_key")
        assert [r[0] for r in cur.fetchall()] == ["before keys", "by author"]
        cur.execute("SELECT artist_mbid::text FROM lb_slice_fetches")
        assert [r[0] for r in cur.fetchall()] == [str(uuid.UUID(int=31))]
        cur.execute("SELECT name_key FROM mb_slice_blobs ORDER BY name_key")
        assert [r[0] for r in cur.fetchall()] == ["a", "o"]        # the author's and this node's own
        cur.execute("SELECT count(*) FROM lb_recording WHERE dump_version > '2999'")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT artist_mbid::text FROM lb_artist")
        assert [r[0] for r in cur.fetchall()] == [str(uuid.UUID(int=31))]
        cur.execute("SELECT recording_mbid::text FROM lb_recording WHERE recording_mbid IN (%s, %s)",
                    (str(uuid.UUID(int=35)), str(uuid.UUID(int=36))))
        assert [r[0] for r in cur.fetchall()] == [str(uuid.UUID(int=36))]
        cur.execute("SELECT count(*) FROM lb_slice_blobs")
        assert cur.fetchone()[0] == 0                     # a stranger's, and one a day ahead
        cur.execute("DELETE FROM mb_slice_fetches")
        cur.execute("DELETE FROM lb_slice_fetches")
        cur.execute("DELETE FROM lb_artist")
        cur.execute("DELETE FROM lb_recording WHERE recording_mbid = %s", (str(uuid.UUID(int=36)),))
