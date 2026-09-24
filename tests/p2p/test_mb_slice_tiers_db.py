"""Which names the MB slice cycle asks for first (desktop/p2p/mb_slice_queries.
pending_slice_names), on a real PostgreSQL — a throwaway database built from
the migrations — skipped where there is none.

The owner's files first, then the names imported Last.fm scrobbles wait on
(the most scrobbled first), then canonized artists never shelved; a name a
peer already answered is not asked again.
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
DBNAME = "sautium_mb_slice_tiers_test"
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


def test_owned_then_scrobbled_by_weight_then_never_shelved(conn):
    owned, shelved = str(uuid.uuid4()), str(uuid.uuid4())
    track, album, variant_dir = str(uuid.uuid4()), str(uuid.uuid4()), "/music/a"
    with conn.cursor() as cur:
        cur.execute("INSERT INTO artists (id, name) VALUES (%s, 'Owned One'), (%s, 'Never Shelved')",
                    (owned, shelved))
        cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) VALUES (%s, %s, 'phantom')",
                    (str(uuid.uuid4()), shelved))
        cur.execute("INSERT INTO tracks (id, title) VALUES (%s, 't')", (track,))
        cur.execute("INSERT INTO track_artists (track_id, artist_id) VALUES (%s, %s)", (track, owned))
        cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'A')", (album,))
        cur.execute("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, %s) RETURNING id",
                    (album, variant_dir))
        vid = cur.fetchone()[0]
        cur.execute("""INSERT INTO media_files (track_id, album_variant_id, file_path, file_format)
                       VALUES (%s, %s, '/music/a/1.flac', 'FLAC')""", (track, vid))
        _waiting(cur, "rarely heard", 1)
        _waiting(cur, "often heard", 5)
        _waiting(cur, "already answered", 9)
        cur.execute("INSERT INTO mb_slice_fetches (name_key) VALUES ('already answered')")
        _waiting(cur, "various artists", 20)

    assert q.pending_slice_names(conn, limit=10) == [
        ("Owned One", 0), ("often heard", 1), ("rarely heard", 1), ("Never Shelved", 2)]
