"""A queued file that leaves the library (PlaybackManager.rebind_files).

The canonical queue binds each owned slot to a media_files row. A row deleted
under it — a rescan pruning a moved or deleted file — must not stay named
there: the next Play archived the old queue into session_tracks and died on
the foreign key, Now Playing's detail 404'd, and the path no longer opened.
Runs against a real PostgreSQL — a throwaway database built from the
migrations, the delete trigger included — skipped where there is none.
"""

import os
import select
import sys
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("sqlalchemy")

import psycopg2.extensions  # noqa: E402
import psycopg2.pool  # noqa: E402

PG = dict(host=os.environ.get("SAUTIUM_TEST_PGHOST", "postgres"),
          port=int(os.environ.get("SAUTIUM_TEST_PGPORT", "5432")),
          user=os.environ.get("SAUTIUM_TEST_PGUSER", "musicai"),
          password=os.environ.get("SAUTIUM_TEST_PGPASSWORD", "supervisor"))
DBNAME = "sautium_queue_rebind_test"

OLD = "E:/Music/Nu jazz/Hidden Orchestra/Night Walks/01. Antiphon.flac"
NEW = "E:/Music/[bandcamp]/Hidden Orchestra/Night Walks/01. Antiphon.flac"


@pytest.fixture(scope="module")
def dsn():
    try:
        admin = psycopg2.connect(dbname="postgres", **PG)
    except psycopg2.OperationalError as e:
        pytest.skip(f"no PostgreSQL for the queue re-bind test: {e}")
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
def conn(dsn, monkeypatch):
    import db_pool
    pool = psycopg2.pool.ThreadedConnectionPool(1, 4, dsn=dsn, options="-c timezone=UTC")
    monkeypatch.setattr(db_pool, "_pool", pool)
    c = psycopg2.connect(dsn, options="-c timezone=UTC")
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("TRUNCATE tracks, albums, artists, listening_sessions CASCADE")
    yield c
    c.close()
    pool.closeall()


@pytest.fixture
def mgr(conn):
    """A manager whose queue persistence is recorded, not written: the
    debounced write lands a second later, when this test's database may
    already be the live one again."""
    from playback.manager import PlaybackManager
    m = PlaybackManager()
    m.persisted = 0

    def persist():
        m.persisted += 1
    m._schedule_persist = persist
    return m


class Library:
    """The rows a queue slot binds to: one artist, its albums and files."""

    def __init__(self, cur):
        self.cur = cur
        self.artist = uuid.uuid4()
        cur.execute("INSERT INTO artists (id, name) VALUES (%s, %s)",
                    (str(self.artist), f"Hidden Orchestra {self.artist.hex[:6]}"))

    def album(self) -> uuid.UUID:
        a = uuid.uuid4()
        self.cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'Night Walks')", (str(a),))
        return a

    def track(self) -> uuid.UUID:
        t = uuid.uuid4()
        self.cur.execute("INSERT INTO tracks (id, title) VALUES (%s, 'Antiphon')", (str(t),))
        self.cur.execute("INSERT INTO track_artists (track_id, artist_id) VALUES (%s, %s)",
                         (str(t), str(self.artist)))
        return t

    def file(self, track, album, path, *, sample_rate=44100) -> int:
        self.cur.execute("INSERT INTO album_variants (album_id, directory_path) "
                         "VALUES (%s, %s) RETURNING id", (str(album), path.rsplit("/", 1)[0]))
        variant = self.cur.fetchone()[0]
        self.cur.execute("INSERT INTO media_files (track_id, album_variant_id, file_path, "
                         "  sample_rate, bit_depth, duration_seconds) "
                         "VALUES (%s, %s, %s, %s, 16, 365.97) RETURNING id",
                         (str(track), variant, path, sample_rate))
        return self.cur.fetchone()[0]


class StubOutput:
    """The active output as rebind_files sees it: an id, maybe an endpoint,
    and the hook it is told through."""

    def __init__(self, output_id, endpoint_id=None):
        self.id, self.endpoint_id = output_id, endpoint_id
        self.changed = []

    def queue_changed(self, kind, *, play=False):
        self.changed.append(kind)


def _notified(listener, timeout) -> bool:
    if select.select([listener], [], [], timeout)[0]:
        listener.poll()
    got = bool(listener.notifies)
    listener.notifies.clear()
    return got


def test_the_trigger_speaks_only_when_rows_went(conn, dsn):
    listener = psycopg2.connect(dsn)
    listener.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        with listener.cursor() as cur:
            cur.execute("LISTEN sautium_files_removed")
        with conn.cursor() as cur:
            cur.execute("DELETE FROM media_files WHERE id = -1")
            assert not _notified(listener, 0.5)
            lib = Library(cur)
            f = lib.file(lib.track(), lib.album(), OLD)
            cur.execute("DELETE FROM media_files WHERE id = %s", (f,))
        assert _notified(listener, 5)
    finally:
        listener.close()


def test_a_moved_file_rebinds_to_the_copy_on_its_album(conn, mgr):
    from playback.queue import items_for_media_ids
    with conn.cursor() as cur:
        lib = Library(cur)
        album, other = lib.album(), lib.album()
        t = lib.track()
        old = lib.file(t, album, OLD)
        mgr.queue.replace(items_for_media_ids([old]))
        # The move, as the rescan does it: the new path imported, the old pruned.
        lib.file(t, other, "E:/Music/Hi-res/Night Walks/01. Antiphon.flac", sample_rate=96000)
        moved = lib.file(t, album, NEW)
        cur.execute("DELETE FROM media_files WHERE id = %s", (old,))
    v = mgr.queue.version
    mgr.rebind_files()
    it = mgr.queue.item_at(1)
    # the album it was queued from outranks a better rip on another one
    assert (it.media_file_id, it.source) == (moved, {"kind": "file", "path": NEW, "format": "FLAC"})
    assert (it.track_id, it.album_id, it.title, it.play) == (str(t), str(album), "Antiphon", None)
    assert (mgr.queue.version, mgr.persisted) == (v + 1, 1)
    mgr.rebind_files()                       # nothing left to move: nothing changes
    assert (mgr.queue.version, mgr.persisted) == (v + 1, 1)


def test_no_copy_left_makes_the_slot_the_track_itself(conn, mgr):
    from playback.queue import items_for_media_ids
    from playback.substitute import native_plays
    with conn.cursor() as cur:
        lib = Library(cur)
        t = lib.track()
        f = lib.file(t, lib.album(), OLD)
        mgr.queue.replace(items_for_media_ids([f]))
        cur.execute("DELETE FROM media_files WHERE id = %s", (f,))
    browser = StubOutput("browser")
    mgr._active = browser
    mgr.rebind_files()
    it = mgr.queue.item_at(1)
    assert (it.media_file_id, it.source, it.track_id) == (None, {"kind": "track"}, str(t))
    assert it.play == {"kind": "pending"}             # streamed, like any phantom
    assert browser.changed == ["rebind"]
    assert mgr.queue.payload()["tracks"][0]["id"] is None
    # HQPlayer opens native copies only
    assert native_plays([it], "hqplayer", None) == [
        {"kind": "unplayable", "reason": "its file left the library"}]


def test_a_track_gone_with_its_file_leaves_an_inert_entry(conn, mgr):
    from playback.queue import items_for_media_ids
    with conn.cursor() as cur:
        lib = Library(cur)
        t = lib.track()
        f = lib.file(t, lib.album(), OLD)
        mgr.queue.replace(items_for_media_ids([f]))
        cur.execute("DELETE FROM tracks WHERE id = %s", (str(t),))   # the file cascades
    mgr.rebind_files()
    it = mgr.queue.item_at(1)
    assert (it.track_id, it.media_file_id, it.title) == (None, None, "Antiphon")
    assert it.source == {"kind": "uri", "uri": "file:///" + OLD}


def test_a_way_in_that_named_a_gone_file_is_read_again(conn, mgr):
    from playback.queue import QueueItem
    with conn.cursor() as cur:
        lib = Library(cur)
        album = lib.album()
        t = lib.track()
        rip = lib.file(t, album, OLD)
    held = QueueItem(track_id=str(t), media_file_id=None,
                     source={"kind": "hqp", "path": "/media/FLASH/Night Walks/01.flac",
                             "format": "FLAC", "endpoint": 99},
                     title="Antiphon", artist="Hidden Orchestra", album_id=str(album))
    mgr.queue.replace([held])
    browser = StubOutput("browser")
    mgr._active = browser
    mgr._reresolve(browser)
    assert held.play["media_file_id"] == rip    # the rip here stands in for the copy there
    with conn.cursor() as cur:
        cur.execute("DELETE FROM media_files WHERE id = %s", (rip,))
    mgr.rebind_files()
    assert held.source["kind"] == "hqp"         # its origin was never the rip
    assert held.play == {"kind": "pending"}


def test_the_session_snapshot_keeps_what_its_keys_allow(conn, mgr):
    from playback import sessions
    from playback.queue import QueueItem
    with conn.cursor() as cur:
        lib = Library(cur)
        album, gone_album = lib.album(), lib.album()
        kept, gone = lib.track(), lib.track()
        f = lib.file(kept, album, OLD)
        cur.execute("INSERT INTO listening_sessions (origin, seed_track_id) VALUES ('radio', %s)",
                    (str(kept),))
    # Rows went a moment ago and the re-bind has not landed yet.
    mgr.queue.replace([
        QueueItem(track_id=str(kept), media_file_id=f, album_id=str(gone_album),
                  source={"kind": "file", "path": OLD, "format": "FLAC"}),
        QueueItem(track_id=str(gone), media_file_id=None, source={"kind": "track"}),
    ])
    with conn.cursor() as cur:
        cur.execute("DELETE FROM media_files WHERE id = %s", (f,))
        cur.execute("DELETE FROM albums WHERE id = %s", (str(gone_album),))
        cur.execute("DELETE FROM tracks WHERE id = %s", (str(gone),))
    sessions.rotate_session(mgr.queue, "track", seed_track_id=str(kept))
    with conn.cursor() as cur:
        cur.execute("SELECT position, track_id::text, media_file_id, album_id "
                    "FROM session_tracks ORDER BY position")
        assert cur.fetchall() == [(0, str(kept), None, None)]
        cur.execute("SELECT origin::text, ended_at IS NULL FROM listening_sessions ORDER BY started_at")
        assert cur.fetchall() == [("radio", False), ("track", True)]


def test_a_listen_keeps_what_its_keys_allow(conn, monkeypatch):
    from playback import tracker
    scrobbles = []
    monkeypatch.setattr(tracker, "_scrobble_async", lambda method, **kw: scrobbles.append(method))
    with conn.cursor() as cur:
        lib = Library(cur)
        t = lib.track()
        f = lib.file(t, lib.album(), OLD)
        cur.execute("DELETE FROM media_files WHERE id = %s", (f,))
    s = tracker._PlaySession({"track_id": str(t), "media_file_id": f,
                              "artist": "Hidden Orchestra", "title": "Antiphon"},
                             datetime.now(timezone.utc), 365.0)
    s.update_position(364.0)
    tracker._save_play_session(s)
    with conn.cursor() as cur:
        cur.execute("SELECT media_file_id, track_id::text, completed FROM listening_history")
        assert cur.fetchall() == [(None, str(t), True)]


def test_the_restore_rebinds_a_persisted_queue(conn, mgr, monkeypatch):
    from playback.queue import items_for_media_ids
    from routers import settings as settings_router
    with conn.cursor() as cur:
        lib = Library(cur)
        album = lib.album()
        t = lib.track()
        old = lib.file(t, album, OLD)
        stored = {"items": [asdict(it) for it in items_for_media_ids([old])]}
        moved = lib.file(t, album, NEW)
        cur.execute("DELETE FROM media_files WHERE id = %s", (old,))
    monkeypatch.setattr(settings_router, "_read",
                        lambda key: stored if key == "player.queue" else None)
    mgr._restore_persisted_queue()
    assert (mgr.queue.item_at(1).media_file_id, mgr.persisted) == (moved, 1)


def test_the_listener_rebinds_on_the_delete(conn, dsn, mgr, monkeypatch):
    import playback.manager as manager_mod
    from config import settings
    from playback.queue import items_for_media_ids
    for key, value in dict(postgres_host=PG["host"], postgres_port=PG["port"],
                           postgres_user=PG["user"], postgres_password=PG["password"],
                           postgres_db=DBNAME).items():
        monkeypatch.setattr(settings, key, value)
    assert settings.database_url == dsn
    monkeypatch.setattr(manager_mod, "manager", mgr)
    passes = []
    rebind = mgr.rebind_files

    def counted():
        rebind()
        passes.append(mgr.queue.item_at(1).media_file_id)
    mgr.rebind_files = counted
    with conn.cursor() as cur:
        lib = Library(cur)
        album = lib.album()
        t = lib.track()
        old = lib.file(t, album, OLD)
        moved = lib.file(t, album, NEW)
    mgr.queue.replace(items_for_media_ids([old]))
    manager_mod.start_files_listener()
    try:
        deadline = time.monotonic() + 10
        while not passes and time.monotonic() < deadline:   # the (re)connect pass
            time.sleep(0.05)
        assert passes == [old]
        with conn.cursor() as cur:
            cur.execute("DELETE FROM media_files WHERE id = %s", (old,))
        deadline = time.monotonic() + 10
        while len(passes) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert passes == [old, moved]
    finally:
        manager_mod.stop_files_listener()
        manager_mod._files_listener.join(timeout=10)
