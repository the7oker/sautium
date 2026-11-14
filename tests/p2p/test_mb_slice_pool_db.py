"""Which names the MB slice cycle asks for (desktop/p2p/mb_slice_queries.
pending_slice_names), on a real PostgreSQL — a throwaway database built from
the migrations — skipped where there is none.

Every reason the node asks is one pool: the owner's files, the names imported
Last.fm scrobbles wait on, canonized artists never shelved, the similar-artist
stubs and the canonized names the ledger does not vouch for; each name in one
form (lower(btrim())), the draw random and unranked, so a request does not tell
why a name is in it. A name a peer already answered is not asked again, and the
waiting room's names wait while an import's walk is reading pages.
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from desktop.p2p import mb_slice_queries as q  # noqa: E402

PG = dict(host=os.environ.get("SAUTIUM_TEST_PGHOST", "postgres"),
          port=int(os.environ.get("SAUTIUM_TEST_PGPORT", "5432")),
          user=os.environ.get("SAUTIUM_TEST_PGUSER", "musicai"),
          password=os.environ.get("SAUTIUM_TEST_PGPASSWORD", "supervisor"))
DBNAME = "sautium_mb_slice_pool_test"
T0 = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def conn():
    try:
        admin = psycopg2.connect(dbname="postgres", **PG)
    except psycopg2.OperationalError as e:
        pytest.skip(f"no PostgreSQL for the tier test: {e}")
    admin.autocommit = True
    from desktop import db_init, node_backup as nb
    nb._drop_database(admin, DBNAME)
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {DBNAME}")
    c = psycopg2.connect(dbname=DBNAME, **PG)
    db_init.apply_migrations(c)
    c.commit()
    c.autocommit = True
    yield c
    c.close()
    nb._drop_database(admin, DBNAME)
    admin.close()


def _waiting(cur, name_key, scrobbles):
    cur.execute("INSERT INTO pending_scrobble_artists (name_key) VALUES (%s)", (name_key,))
    for n in range(scrobbles):
        cur.execute("""INSERT INTO pending_scrobbles (played_at, artist, title, name_key)
                       VALUES (%s, %s, %s, %s)""",
                    (T0 + timedelta(minutes=n), name_key, f"song {n}", name_key))


def _artist(cur, name, mbid=False, shelved=False, owned=False, similar_of=None):
    aid = str(uuid.uuid4())
    cur.execute("INSERT INTO artists (id, name, last_album_sync) VALUES (%s, %s, %s)",
                (aid, name, T0 if shelved else None))
    if mbid:
        cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) VALUES (%s, %s, 'phantom')",
                    (str(uuid.uuid4()), aid))
    if owned:
        track, album = str(uuid.uuid4()), str(uuid.uuid4())
        cur.execute("INSERT INTO tracks (id, title) VALUES (%s, 't')", (track,))
        cur.execute("INSERT INTO track_artists (track_id, artist_id) VALUES (%s, %s)", (track, aid))
        cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'A')", (album,))
        cur.execute("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, %s) RETURNING id",
                    (album, f"/music/{aid}"))
        cur.execute("""INSERT INTO media_files (track_id, album_variant_id, file_path, file_format)
                       VALUES (%s, %s, %s, 'FLAC')""", (track, cur.fetchone()[0], f"/music/{aid}/1.flac"))
    if similar_of:
        cur.execute("INSERT INTO similar_artists (artist_id, similar_artist_id, match_score, source) "
                    "VALUES (%s, %s, 0.5, 'lastfm')", (similar_of, aid))
    return aid


@pytest.fixture
def pool(conn):
    with conn.cursor() as cur:
        owned = _artist(cur, "Owned One", owned=True)
        _artist(cur, "Never Shelved", mbid=True)
        _artist(cur, "  Canon From Peers ", mbid=True, shelved=True)
        _artist(cur, "Similar Stub", similar_of=owned)
        _waiting(cur, "rarely heard", 1)
        _waiting(cur, "often heard", 50)
        _waiting(cur, "already answered", 9)
        cur.execute("INSERT INTO mb_slice_fetches (name_key) VALUES ('already answered')")
        _waiting(cur, "various artists", 20)
    yield conn
    with conn.cursor() as cur:
        cur.execute("""TRUNCATE artists, tracks, albums, pending_scrobble_artists, pending_scrobbles,
                                mb_slice_fetches, lastfm_import CASCADE""")


EVERY_REASON = ["canon from peers", "never shelved", "often heard", "owned one", "rarely heard",
                "similar stub"]


def test_every_reason_is_one_pool_in_one_form(pool):
    assert sorted(q.pending_slice_names(pool, limit=10)) == EVERY_REASON


def test_the_draw_is_random_and_unranked(pool):
    # Until the mixed stream the owner's files came first and the waiting
    # room by its scrobble count: a request's head told both.
    heads, often = set(), 0
    for _ in range(200):
        drawn = q.pending_slice_names(pool, limit=3)
        assert len(drawn) == 3
        heads.add(drawn[0])
        often += "often heard" in drawn
    assert heads == set(EVERY_REASON)
    assert often < 160                       # one in two draws, not every one


def test_the_waiting_room_waits_while_an_import_is_reading(pool):
    with pool.cursor() as cur:
        cur.execute("INSERT INTO lastfm_import (username, reading_since) VALUES ('someone', now())")
    assert sorted(q.pending_slice_names(pool, limit=10)) == [
        "canon from peers", "never shelved", "owned one", "similar stub"]
    with pool.cursor() as cur:
        cur.execute("UPDATE lastfm_import SET reading_since = NULL")
    assert sorted(q.pending_slice_names(pool, limit=10)) == EVERY_REASON
