"""The Last.fm history walk (backend/lastfm_history.py).

The page parser and the per-name key are pure. The walk itself — pages into
the waiting room, this node's own scrobbles left out, the cursor surviving a
restart, a second sync reading nothing twice — runs against a real PostgreSQL
(a throwaway database built from the migrations, the connection pool pointed
at it), with Last.fm replaced by a list of scrobbles. Skipped where there is
no cluster.
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.dom.minidom import parseString

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
lastfm = pytest.importorskip("lastfm")
lastfm_history = pytest.importorskip("lastfm_history")

T0 = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)

PAGE = """<?xml version="1.0" encoding="utf-8"?>
<lfm status="ok"><recenttracks user="vale" page="1" perPage="200" totalPages="1" total="3">
  <track nowplaying="true"><artist mbid="">Now Playing</artist><name>Live</name>
    <mbid></mbid><album mbid=""></album></track>
  <track><artist mbid="2f9ecbed-27be-40e6-abca-6de49d50299e">Sade</artist><name>Kiss of Life</name>
    <mbid>not-a-uuid</mbid><album mbid="0d2cb66c-3a32-4f81-b977-9231c461c34a">Love Deluxe</album>
    <date uts="1758391200">20 Sep 2025, 18:00</date></track>
  <track><artist mbid="">Radio Host</artist><name>Station ID</name>
    <mbid></mbid><album mbid=""></album><date uts="1758391000">20 Sep 2025, 17:56</date></track>
</recenttracks></lfm>"""


def test_a_page_reads_dated_scrobbles_and_keeps_mbids_as_hints():
    page = lastfm._recent_tracks(parseString(PAGE))
    assert page["total"] == 3
    assert page["items"] == [
        {"played_at": datetime.fromtimestamp(1758391200, tz=timezone.utc), "artist": "Sade",
         "title": "Kiss of Life", "album": "Love Deluxe",
         "artist_mbid": "2f9ecbed-27be-40e6-abca-6de49d50299e",
         "album_mbid": "0d2cb66c-3a32-4f81-b977-9231c461c34a", "track_mbid": None},
        {"played_at": datetime.fromtimestamp(1758391000, tz=timezone.utc), "artist": "Radio Host",
         "title": "Station ID", "album": None,
         "artist_mbid": None, "album_mbid": None, "track_mbid": None},
    ]


def test_a_name_waits_under_the_head_of_its_credit():
    assert lastfm_history.name_key("Sade feat. Someone Else") == "sade"
    assert lastfm_history.name_key("  Beth Hart & Joe Bonamassa ") == "beth hart & joe bonamassa"


# ---------------------------------------------------------------------------
# The walk, on a real cluster
# ---------------------------------------------------------------------------

PG = dict(host=os.environ.get("SAUTIUM_TEST_PGHOST", "postgres"),
          port=int(os.environ.get("SAUTIUM_TEST_PGPORT", "5432")),
          user=os.environ.get("SAUTIUM_TEST_PGUSER", "musicai"),
          password=os.environ.get("SAUTIUM_TEST_PGPASSWORD", "supervisor"))
DBNAME = "sautium_lastfm_history_test"


@pytest.fixture(scope="module")
def dsn():
    try:
        admin = psycopg2.connect(dbname="postgres", **PG)
    except psycopg2.OperationalError as e:
        pytest.skip(f"no PostgreSQL for the walk test: {e}")
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


def _scrobble(minutes, artist="Sade", title=None):
    return {"played_at": T0 + timedelta(minutes=minutes), "artist": artist,
            "title": title or f"Song {minutes}", "album": "Love Deluxe",
            "artist_mbid": None, "album_mbid": None, "track_mbid": None}


class _FakeLastFm:
    """user.getRecentTracks over a fixed history: newest first, strictly
    before `before`, not before `after`, pages of lastfm_history.PAGE_SIZE."""
    history = []
    calls = []

    def __init__(self, session_key=""):
        pass

    def recent_tracks_page(self, user, before, after):
        self.calls.append((before, after))
        rows = sorted((s for s in self.history
                       if s["played_at"] < before and (after is None or s["played_at"] >= after)),
                      key=lambda s: s["played_at"], reverse=True)
        return {"total": len(rows), "items": rows[:lastfm_history.PAGE_SIZE]}


@pytest.fixture
def walk(dsn, monkeypatch):
    import db_pool
    from config import settings
    pool = psycopg2.pool.ThreadedConnectionPool(1, 3, dsn=dsn, options="-c timezone=UTC")
    monkeypatch.setattr(db_pool, "_pool", pool)
    monkeypatch.setattr(lastfm, "LastFmService", _FakeLastFm)
    monkeypatch.setattr(lastfm_history, "_notify", lambda: None)
    monkeypatch.setattr(lastfm_history, "PAGE_SIZE", 3)
    monkeypatch.setattr(settings, "lastfm_session_key", "sk")
    monkeypatch.setattr(settings, "lastfm_username", "Vale")
    _FakeLastFm.calls = []
    conn = psycopg2.connect(dsn, options="-c timezone=UTC")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("TRUNCATE lastfm_import, pending_scrobble_artists, pending_scrobbles, "
                    "tracks CASCADE")
    yield conn
    conn.close()
    pool.closeall()


def test_the_walk_fills_the_waiting_room_without_this_nodes_own_listens(walk):
    own = uuid.uuid4()
    with walk.cursor() as cur:
        cur.execute("INSERT INTO tracks (id, title) VALUES (%s, 'Song 30')", (str(own),))
        # This node played minute 30 and scrobbled it; Last.fm has it 1 s off.
        cur.execute("""INSERT INTO listening_history (track_id, started_at, ended_at,
                                                      duration_listened, completed, skipped)
                       VALUES (%s, %s, %s, 200, TRUE, FALSE)""",
                    (str(own), T0 + timedelta(minutes=30, seconds=-1), T0 + timedelta(minutes=34)))
    _FakeLastFm.history = [_scrobble(m) for m in (0, 10, 20, 30, 40, 50, 60)]

    lastfm_history._run("vale", "connected")

    with walk.cursor() as cur:
        cur.execute("SELECT played_at FROM pending_scrobbles ORDER BY played_at")
        waiting = [r[0] for r in cur.fetchall()]
        assert waiting == [T0 + timedelta(minutes=m) for m in (0, 10, 20, 40, 50, 60)]
        cur.execute("SELECT name_key FROM pending_scrobble_artists")
        assert cur.fetchall() == [("sade",)]
        cur.execute("SELECT watermark_at IS NOT NULL, walk_top_at, walk_cursor_at, walk_fetched "
                    "FROM lastfm_import WHERE username = 'vale'")
        assert cur.fetchone() == (True, None, None, 7)
    # Pages of three walk down by their `to` bound, each re-reading the
    # second its predecessor ended on: 60-50-40, 40-30-20, 20-10-0, 0.
    assert len(_FakeLastFm.calls) == 4 and all(a is None for _, a in _FakeLastFm.calls)
    assert lastfm_history.status()["waiting"] == 6


def test_a_second_sync_reads_behind_the_watermark_and_adds_nothing_twice(walk):
    _FakeLastFm.history = [_scrobble(m) for m in (0, 10, 20)]
    lastfm_history._run("vale", "connected")
    _FakeLastFm.calls = []
    _FakeLastFm.history.append(_scrobble(90, title="Played on the phone"))

    lastfm_history._run("vale", "sync")

    with walk.cursor() as cur:
        cur.execute("SELECT count(*) FROM pending_scrobbles")
        assert cur.fetchone() == (4,)
    after = _FakeLastFm.calls[0][1]
    assert after is not None and after < T0   # two weeks behind the watermark


def test_a_walk_interrupted_by_a_restart_resumes_from_its_cursor(walk):
    _FakeLastFm.history = [_scrobble(m) for m in range(0, 70, 10)]

    class _Dies(_FakeLastFm):
        def recent_tracks_page(self, user, before, after):
            if len(self.calls) == 1:
                raise RuntimeError("the process went away")
            return super().recent_tracks_page(user, before, after)

    import lastfm as lastfm_module
    lastfm_module.LastFmService = _Dies
    try:
        lastfm_history._run("vale", "connected")
    finally:
        lastfm_module.LastFmService = _FakeLastFm
    with walk.cursor() as cur:
        cur.execute("SELECT walk_top_at IS NOT NULL, walk_cursor_at FROM lastfm_import")
        in_progress, cursor = cur.fetchone()
        assert in_progress and cursor == T0 + timedelta(minutes=40, seconds=1)
    _FakeLastFm.calls = []

    lastfm_history._run("vale", "resume")

    assert _FakeLastFm.calls[0][0] == T0 + timedelta(minutes=40, seconds=1)
    with walk.cursor() as cur:
        cur.execute("SELECT count(*) FROM pending_scrobbles")
        assert cur.fetchone() == (7,)
        cur.execute("SELECT walk_top_at FROM lastfm_import")
        assert cur.fetchone() == (None,)


def test_remove_takes_every_imported_listen_and_the_waiting_room(walk):
    track = uuid.uuid4()
    with walk.cursor() as cur:
        cur.execute("INSERT INTO tracks (id, title) VALUES (%s, 't')", (str(track),))
        cur.execute("""INSERT INTO listening_history (track_id, started_at, completed, source)
                       VALUES (%s, %s, TRUE, 'lastfm'), (%s, %s, TRUE, 'sautium')""",
                    (str(track), T0 - timedelta(days=1), str(track), T0 + timedelta(hours=1)))
    _FakeLastFm.history = [_scrobble(m) for m in (0, 10)]
    lastfm_history._run("vale", "connected")

    removed = lastfm_history.remove_imported()

    assert removed == {"listens": 1, "sessions": 0, "waiting": 2}
    with walk.cursor() as cur:
        cur.execute("SELECT source FROM listening_history")
        assert cur.fetchall() == [("sautium",)]
        cur.execute("SELECT (SELECT count(*) FROM lastfm_import), (SELECT count(*) FROM pending_scrobble_artists)")
        assert cur.fetchone() == (0, 0)
