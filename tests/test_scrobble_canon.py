"""Placing imported scrobbles on canonical tracks (backend/canon/scrobbles.py).

On a real PostgreSQL — a throwaway database built from the migrations, the
connection pool and the SQLAlchemy sessions pointed at it — with a hand-built
MusicBrainz catalogue: two namesakes called Sade, each with their own songs.
Skipped where there is no cluster.
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
sqlalchemy = pytest.importorskip("sqlalchemy")
scrobbles = pytest.importorskip("canon.scrobbles")

PG = dict(host=os.environ.get("SAUTIUM_TEST_PGHOST", "postgres"),
          port=int(os.environ.get("SAUTIUM_TEST_PGPORT", "5432")),
          user=os.environ.get("SAUTIUM_TEST_PGUSER", "musicai"),
          password=os.environ.get("SAUTIUM_TEST_PGPASSWORD", "supervisor"))
DBNAME = "sautium_scrobble_canon_test"
T0 = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)
SADE, OTHER_SADE = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
KISS_REC, SMOOTH_REC = "33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444"


@pytest.fixture(scope="module")
def dsn():
    try:
        admin = psycopg2.connect(dbname="postgres", **PG)
    except psycopg2.OperationalError as e:
        pytest.skip(f"no PostgreSQL for the scrobble canon test: {e}")
    admin.autocommit = True
    from desktop import db_init, node_backup as nb
    nb._drop_database(admin, DBNAME)
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {DBNAME}")
    conn = psycopg2.connect(dbname=DBNAME, **PG)
    db_init.apply_migrations(conn)
    conn.commit()
    conn.close()
    yield f"postgresql://{PG['user']}:{PG['password']}@{PG['host']}:{PG['port']}/{DBNAME}"
    nb._drop_database(admin, DBNAME)
    admin.close()


@pytest.fixture
def db(dsn, monkeypatch):
    import database
    import db_pool
    import mb_backend
    from sqlalchemy.orm import sessionmaker
    pool = psycopg2.pool.ThreadedConnectionPool(1, 4, dsn=dsn, options="-c timezone=UTC")
    engine = sqlalchemy.create_engine(dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(mb_backend, "LOCAL_DUMP", True)
    conn = psycopg2.connect(dsn, options="-c timezone=UTC")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("""TRUNCATE pending_scrobbles, pending_scrobble_artists, tracks, albums,
                                artists, mb_artist, mb_artist_credit_name, mb_recording, mb_track,
                                mb_release_group, mb_release CASCADE""")
        cur.execute("""INSERT INTO user_settings (key, value) VALUES ('musicbrainz.db_version', '"x"')
                       ON CONFLICT (key) DO NOTHING""")
        # Two namesakes. Sade (1) sings Kiss of Life and Smooth Operator; the
        # other Sade (2) has a song of her own.
        cur.execute("""INSERT INTO mb_artist (id, gid, name) VALUES
                         (1, %s, 'Sade'), (2, %s, 'Sade')""", (SADE, OTHER_SADE))
        cur.execute("""INSERT INTO mb_artist_credit_name (artist_credit, position, artist, name)
                       VALUES (10, 0, 1, 'Sade'), (20, 0, 2, 'Sade')""")
        cur.execute("""INSERT INTO mb_recording (id, gid, name, artist_credit, length) VALUES
                         (100, %s, 'Kiss of Life', 10, 330000),
                         (101, %s, 'Smooth Operator', 10, 298000),
                         (200, %s, 'Nothing Like Hers', 20, 200000)""",
                    (KISS_REC, SMOOTH_REC, str(uuid.uuid4())))
        cur.execute("""INSERT INTO mb_track (id, gid, recording, name, artist_credit) VALUES
                         (1000, %s, 100, 'Kiss of Life', 10),
                         (1001, %s, 101, 'Smooth Operator', 10)""",
                    (str(uuid.uuid4()), str(uuid.uuid4())))
    yield conn
    conn.close()
    pool.closeall()
    engine.dispose()


def _wait(cur, minutes, title, artist="Sade", album=None):
    import lastfm_history
    nk = lastfm_history.name_key(artist)
    cur.execute("INSERT INTO pending_scrobble_artists (name_key) VALUES (%s) ON CONFLICT DO NOTHING",
                (nk,))
    cur.execute("""INSERT INTO pending_scrobbles (played_at, artist, title, album, name_key)
                   VALUES (%s, %s, %s, %s, %s)""",
                (T0 + timedelta(minutes=minutes), artist, title, album, nk))


def _slot(cur, track_id, title, seconds, recording=None, album="Love Deluxe"):
    album_id = str(uuid.uuid4())
    cur.execute("INSERT INTO tracks (id, title) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (track_id, title))
    cur.execute("INSERT INTO albums (id, title) VALUES (%s, %s)", (album_id, album))
    cur.execute("""INSERT INTO album_tracks (album_id, track_id, position, recording_mbid, length_ms)
                   VALUES (%s, %s, 1, %s, %s)""", (album_id, track_id, recording, seconds * 1000))


def _listens(cur):
    cur.execute("SELECT track_id::text, started_at, duration_listened, source "
                "FROM listening_history ORDER BY started_at")
    return cur.fetchall()


def test_stage_a_places_known_tracks_once_and_leaves_echoes(db):
    from uuid_utils import track_uuid
    known = str(track_uuid("Kiss of Life", "Sade"))
    with db.cursor() as cur:
        _slot(cur, known, "Kiss of Life", 330)
        _wait(cur, 0, "Kiss of Life - 2011 Remaster")
        # Two scrobblers reported the same play five seconds apart.
        _wait(cur, 0.08, "Kiss of Life")
        # Played here, then read back from Last.fm a second off.
        cur.execute("""INSERT INTO listening_history (track_id, started_at, ended_at,
                                                      duration_listened, completed, skipped)
                       VALUES (%s, %s, %s, 330, TRUE, FALSE)""",
                    (known, T0 + timedelta(minutes=30), T0 + timedelta(minutes=36)))
        _wait(cur, 30.02, "Kiss of Life")

    assert scrobbles.drop_echoes() == 1
    assert scrobbles.bind_known(None) == 1

    with db.cursor() as cur:
        assert _listens(cur) == [(known, T0, 330, "lastfm"),
                                 (known, T0 + timedelta(minutes=30), 330, "sautium")]
        cur.execute("SELECT count(*) FROM pending_scrobbles")
        assert cur.fetchone() == (0,)
        cur.execute("SELECT play_count FROM local_play_stats WHERE track_id = %s", (known,))
        assert cur.fetchone() == (2,)


def test_stage_b_takes_the_namesake_the_evidence_names(db):
    with db.cursor() as cur:
        _wait(cur, 0, "Kiss of Life")
        _wait(cur, 5, "Smooth Operator - Single Version")
        _wait(cur, 10, "A Song Nobody Recorded")

    stats = scrobbles.resolve_names()

    assert stats["resolved"] == 1
    with db.cursor() as cur:
        cur.execute("""SELECT a.name, am.mbid::text, am.confidence
                         FROM pending_scrobble_artists n
                         JOIN artists a ON a.id = n.artist_id
                         JOIN artist_mbids am ON am.artist_id = a.id""")
        assert cur.fetchall() == [("Sade", SADE, "phantom")]


def test_stage_b_waits_rather_than_pour_a_namesake_onto_an_artist(db):
    from uuid_utils import artist_uuid
    with db.cursor() as cur:
        # This node's Sade row already stands for the OTHER Sade.
        cur.execute("INSERT INTO artists (id, name) VALUES (%s, 'Sade')", (str(artist_uuid("Sade")),))
        cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) VALUES (%s, %s, 'user')",
                    (OTHER_SADE, str(artist_uuid("Sade"))))
        _wait(cur, 0, "Kiss of Life")

    stats = scrobbles.resolve_names()

    assert stats["resolved"] == 0 and stats["undecided"] == 1
    with db.cursor() as cur:
        cur.execute("SELECT artist_id FROM pending_scrobble_artists")
        assert cur.fetchone() == (None,)


def test_stage_d_places_through_the_catalogue(db):
    with db.cursor() as cur:
        _wait(cur, 0, "Kiss of Life (Live at the Albert Hall)")
        _wait(cur, 5, "Smooth Operator - Single Version")
        _wait(cur, 10, "Kiss Of Life")
        _wait(cur, 15, "A Song Nobody Recorded")
    resolved = scrobbles.resolve_names()["artists"]
    with db.cursor() as cur:
        # The mint gave Kiss of Life a slot (under MB's own title); Smooth
        # Operator's album was never minted.
        _slot(cur, str(uuid.uuid4()), "Kiss of Life", 330, recording=KISS_REC)

    stats = scrobbles.bind_catalogue(resolved)
    assert scrobbles.bind_catalogue() == {"bound": 0, "not_minted": 0, "not_in_catalogue": 0}

    assert stats == {"bound": 1, "not_minted": 1, "not_in_catalogue": 2}
    with db.cursor() as cur:
        assert [(s, d) for _, s, d, _ in _listens(cur)] == [(T0 + timedelta(minutes=10), 330)]
        cur.execute("SELECT title FROM pending_scrobbles ORDER BY played_at")
        assert [r[0] for r in cur.fetchall()] == ["Kiss of Life (Live at the Albert Hall)",
                                                  "Smooth Operator - Single Version",
                                                  "A Song Nobody Recorded"]
        cur.execute("SELECT checked_at IS NOT NULL FROM pending_scrobble_artists")
        assert cur.fetchone() == (True,)


def test_a_pass_resolves_places_and_wakes_what_feeds_on_listens(db, monkeypatch):
    import background_enrichment
    from uuid_utils import artist_uuid
    woken = []
    monkeypatch.setattr(background_enrichment, "wake", lambda reason="": woken.append(reason))
    monkeypatch.setattr(background_enrichment, "wake_db_steps", lambda reason="": woken.append(reason))
    sade = str(artist_uuid("Sade"))
    with db.cursor() as cur:
        # A Sade row this node already shelved (no anchor yet); its album's
        # slot carries the recording under a track id no name derives.
        cur.execute("INSERT INTO artists (id, name, last_album_sync) VALUES (%s, 'Sade', now())",
                    (sade,))
        slot_track = str(uuid.uuid4())
        _slot(cur, slot_track, "Kiss of Life", 330, recording=KISS_REC)
        cur.execute("INSERT INTO album_artists (album_id, artist_id) SELECT album_id, %s "
                    "FROM album_tracks", (sade,))
        _wait(cur, 0, "Kiss of Life - Single Version")

    drain = scrobbles.run_pass({"full": True, "names": [], "artists": []})

    assert drain is False
    with db.cursor() as cur:
        assert [(t, s) for t, _, _, s in _listens(cur)] == [(slot_track, "lastfm")]
        cur.execute("SELECT count(*) FROM pending_scrobble_artists")
        assert cur.fetchone() == (0,)
        cur.execute("SELECT artist_id::text FROM artist_mbids WHERE mbid = %s", (SADE,))
        assert cur.fetchone() == (sade,)
    assert woken == ["lastfm import", "lastfm import"]
