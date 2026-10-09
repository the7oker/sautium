"""A scan never reports a walk over an unreachable music folder as a scan
(scanner.library_unreachable — the rule the library.mount_missing notice and
the prune read): every entry point refuses it and wakes the notices, a walk
that found nothing records nothing, and a prune deletes nothing from a
partial tree — the folder gone, or a subfolder the walk could not read. On
2026-10-09 a Docker bind that came up on an empty directory turned "Scan for
new" into "Scan complete" and a fresh "Last scan" while the new album sat on
the drive nothing had mounted.

On a real PostgreSQL — the module's scratch database (conftest.scratch_dsn),
the connection pool AND the SQLAlchemy sessions pointed at it, so not even a
regressed prune can reach a catalog. Skipped where there is no cluster.
"""

import asyncio
import errno
import os
import select
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
settings_router = pytest.importorskip("routers.settings")

import scanner  # noqa: E402


@pytest.fixture
def library(tmp_path, monkeypatch):
    """The music folder the node walks — an empty directory, as the
    2026-10-09 bind was — and the host's own name for it, the one the catalog
    stores and the owner knows (a Docker node walks its bind)."""
    from config import settings
    root = tmp_path / "music"
    root.mkdir()
    monkeypatch.setattr(settings, "music_library_path", str(root))
    monkeypatch.setattr(settings, "music_host_path", (tmp_path / "host" / "Music").as_posix())
    return root


@pytest.fixture
def cur(scratch_dsn, monkeypatch):
    import database
    import db_pool
    import psycopg2.pool
    from sqlalchemy.orm import sessionmaker
    pool = psycopg2.pool.ThreadedConnectionPool(1, 2, dsn=scratch_dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    engine = database.make_engine(scratch_dsn)
    monkeypatch.setattr(database, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    # The notice's onset and the folder's last look are process state: each
    # test starts with none armed and a folder last seen in place.
    monkeypatch.setattr(settings_router, "_derived_since", {})
    monkeypatch.setattr(scanner, "_folder_unreachable", False)
    monkeypatch.setattr(scanner, "_recorded", 0)
    monkeypatch.setattr(scanner, "_looking", False)
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    with conn.cursor() as c:
        for table in ("user_settings", "media_files", "album_variants", "albums", "tracks"):
            c.execute(f"DELETE FROM {table}")
        yield c
    conn.close()
    engine.dispose()
    pool.closeall()


@pytest.fixture
def heard(scratch_dsn):
    """What the notices channel hears from the backend."""
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute("LISTEN sautium_notices")

    def wakes(timeout=2.0):
        got = []
        while select.select([conn], [], [], timeout)[0]:
            conn.poll()
            got += [n.channel for n in conn.notifies]
            conn.notifies.clear()
            timeout = 0.2                         # drain what follows closely
        return got

    yield wakes
    conn.close()


@pytest.fixture
def main_module(monkeypatch):
    monkeypatch.chdir(BACKEND)   # main mounts static/ relative to the server's directory
    import main
    saved = dict(main._scan_state)
    yield main
    main._scan_state.clear()
    main._scan_state.update(saved)


def _owned_file(cur, album="Solar Fields/Shaped By Time"):
    from config import settings
    album_dir = f"{settings.library_db_root()}/{album}"
    track, album = str(uuid.uuid4()), str(uuid.uuid4())
    cur.execute("INSERT INTO tracks (id, title) VALUES (%s, 'Silent Walking')", (track,))
    cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'Shaped By Time')", (album,))
    cur.execute("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, %s) "
                "RETURNING id", (album, album_dir))
    cur.execute("INSERT INTO media_files (track_id, album_variant_id, file_path) "
                "VALUES (%s, %s, %s)", (track, cur.fetchone()[0], f"{album_dir}/01.flac"))
    return album_dir


def _last_scan(cur):
    cur.execute("SELECT value FROM user_settings WHERE key = 'library.last_scan_at'")
    return cur.fetchone()


def test_an_empty_folder_is_unreachable_only_while_the_catalog_knows_files(
        cur, library, monkeypatch):
    from config import settings
    assert not scanner.library_unreachable()      # a fresh node's empty folder

    _owned_file(cur)
    assert scanner.library_unreachable()

    (library / "Electronic").mkdir()
    assert not scanner.library_unreachable()

    library.joinpath("Electronic").rmdir()
    library.rmdir()                               # the drive itself gone
    assert scanner.library_unreachable()

    monkeypatch.setattr(settings, "music_library_path", "")
    assert not scanner.library_unreachable()      # no folder chosen yet is not "unmounted"


def test_the_notice_names_the_folder_the_owner_knows(cur, library):
    _owned_file(cur)

    notice = {n["key"]: n for n in settings_router._notices_state()["items"]}

    # the host's name for the folder, never the path the node walks
    assert notice["library.mount_missing"]["data"]["path"] == (
        library.parent / "host" / "Music").as_posix()


def _keys():
    return {n["key"] for n in settings_router._notices_state()["items"]}


def test_the_notices_look_and_never_wake_the_channel(cur, library, heard):
    _owned_file(cur)                              # the folder is empty

    assert "library.mount_missing" in _keys()     # the derivation looked
    assert heard(timeout=0.5) == []               # and woke nobody: on a flapping share it
                                                  # would wake itself without end


def _hang_the_folder(monkeypatch, library):
    """scandir of the library root blocks, as on a dead network mount, until
    the returned event is set; `blocked` says a look is stuck in it."""
    real, release, blocked = os.scandir, threading.Event(), threading.Event()

    def scandir(path="."):
        if os.fspath(path) == str(library):
            blocked.set()
            release.wait(10)
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)
    return release, blocked


def test_a_look_a_dead_mount_holds_reads_as_unreachable_and_holds_no_reader(
        cur, library, monkeypatch):
    _owned_file(cur)
    (library / "Electronic").mkdir()              # the folder is there...
    monkeypatch.setattr(scanner, "LOOK_PATIENCE_S", 0.3)
    release, blocked = _hang_the_folder(monkeypatch, library)   # ...and stops answering
    try:
        t = time.monotonic()
        assert "library.mount_missing" in _keys()  # a look stuck past patience: a dead mount
        assert "library.mount_missing" in _keys()  # the next reader does not wait again
        assert time.monotonic() - t < 2
        assert blocked.is_set()
    finally:
        release.set()
    assert _wait_until(lambda: not scanner._looking)
    assert "library.mount_missing" not in _keys()  # the look came back: the folder answers


def _wait_until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_an_older_look_that_lands_late_records_nothing(cur, library, monkeypatch):
    _owned_file(cur)                              # the folder is empty: a look says gone
    release, blocked = _hang_the_folder(monkeypatch, library)
    old = threading.Thread(target=scanner.library_unreachable, kwargs={"wake": False})
    old.start()
    assert blocked.wait(5)

    scanner.folder_seen(False)                    # a newer word: a library file served
    release.set()
    old.join(5)

    assert scanner._folder_unreachable is False   # the stale "gone" landed last and lost


class _NoRange:
    headers: dict = {}


def test_only_the_librarys_own_files_speak_for_the_folder(cur, library, tmp_path, heard):
    from fastapi import HTTPException
    from routers import media
    _owned_file(cur)                              # the folder is empty: gone
    owned = str(library / "A" / "01.flac")

    with pytest.raises(HTTPException):
        media._serve_disk(str(tmp_path / "transcode_cache" / "x.opus"), "audio/ogg", _NoRange())
    assert heard(timeout=0.5) == []               # a cache miss says nothing of the folder

    with pytest.raises(HTTPException):
        media._serve_disk(owned, "audio/flac", _NoRange())
    assert heard() == ["sautium_notices"]         # the library's own file: the folder is gone
    with pytest.raises(HTTPException):
        media._serve_disk(owned, "audio/flac", _NoRange())
    assert heard(timeout=0.5) == []               # a retry wakes nobody again

    (library / "A").mkdir()
    Path(owned).write_bytes(b"x" * 16)
    media._serve_disk(owned, "audio/flac", _NoRange())
    assert heard() == ["sautium_notices"]         # served: the folder answers again
    media._serve_disk(owned, "audio/flac", _NoRange())
    assert heard(timeout=0.5) == []               # never once per request


def test_opening_the_library_looks_and_wakes_the_notices_on_a_change(
        cur, library, main_module, heard):
    _owned_file(cur)

    asyncio.run(settings_router._library_state())
    assert heard() == ["sautium_notices"]         # the folder gone: news

    asyncio.run(settings_router._library_state())
    assert heard(timeout=0.5) == []               # still gone: no wake per read

    (library / "Electronic").mkdir()              # the folder is back
    asyncio.run(settings_router._library_state())
    assert heard() == ["sautium_notices"]


def test_scan_start_refuses_an_unreachable_folder_and_wakes_the_notices(
        cur, library, main_module, heard):
    from fastapi import HTTPException
    _owned_file(cur)

    with pytest.raises(HTTPException) as refused:
        asyncio.run(main_module.scan_start())

    assert refused.value.status_code == 409
    assert refused.value.detail == scanner.UNREACHABLE
    assert main_module._scan_state["running"] is False
    assert heard() == ["sautium_notices"]         # every open tab learns the onset

    with pytest.raises(HTTPException):
        asyncio.run(main_module.scan_start())
    assert heard(timeout=0.5) == []               # a second tap wakes nobody again


def test_the_cli_and_the_legacy_endpoint_refuse_as_well(cur, library, main_module, heard):
    from fastapi import HTTPException
    _owned_file(cur)

    with pytest.raises(scanner.LibraryUnreachable):
        scanner.scan_library()                    # what cli.py scan runs
    with pytest.raises(HTTPException) as refused:
        asyncio.run(main_module.scan_library_endpoint())

    assert refused.value.status_code == 409
    assert heard() == ["sautium_notices"]         # the first look's news; the second's is not


def _empty_the_folder_during_the_walk(monkeypatch, library):
    (library / "Electronic").mkdir()

    def walk(self, **kw):
        library.joinpath("Electronic").rmdir()
        return [], [], []

    monkeypatch.setattr(scanner.LibraryScanner, "find_audio_files", walk)


@pytest.mark.parametrize("case, progress, wakes", [
    ("gone before the walk", f"Scan failed: {scanner.UNREACHABLE}", ["sautium_notices"]),
    ("gone during the walk", f"Scan failed: {scanner.UNREACHABLE}", ["sautium_notices"]),
    ("a fresh node's empty folder", "No audio files found", []),
], ids=["gone-before", "gone-during", "fresh-node"])
def test_an_empty_walk_records_no_scan(cur, library, main_module, heard, monkeypatch,
                                       case, progress, wakes):
    from canon import post_import
    if case != "a fresh node's empty folder":
        _owned_file(cur)
    if case == "gone during the walk":
        _empty_the_folder_during_the_walk(monkeypatch, library)
    ran = []
    monkeypatch.setattr(post_import, "run", lambda *a: ran.append("post_import"))
    monkeypatch.setattr(scanner, "prune_missing_files", lambda **kw: ran.append("prune"))
    main_module._scan_state.update(running=True, cancel_requested=False,
                                   progress="Starting scan...", stats=None, result=None)

    main_module._scan_worker(None, True, None, True)

    assert main_module._scan_state["running"] is False
    assert main_module._scan_state["progress"] == progress
    assert ran == []                              # no post-import pass, no prune
    assert _last_scan(cur) is None
    assert heard(timeout=0.5) == wakes


def test_a_prune_handed_a_partial_tree_deletes_nothing_once_the_folder_is_gone(cur, library):
    album_dir = _owned_file(cur)

    # a walk that saw one file before the drive left
    stats = scanner.prune_missing_files(disk_paths={f"{album_dir}/02.flac"})

    assert stats["pruned"] == 0
    cur.execute("SELECT count(*) FROM media_files")
    assert cur.fetchone()[0] == 1


def _albums_one_unreadable_one_gone(cur, library, monkeypatch):
    """A/ readable, B/ a folder the node may not list, C/ in the catalog but
    deleted from the disk. The suite runs as root, which reads through any
    mode bits, so the refusal comes from scandir itself."""
    for album in ("A", "B", "C"):
        if album != "C":
            (library / album).mkdir()
            (library / album / "01.flac").touch()
        _owned_file(cur, album)
    denied, real = str(library / "B"), os.scandir

    def scandir(path="."):
        if os.fspath(path) == denied:
            raise PermissionError(errno.EACCES, "Permission denied", denied)
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)
    return denied


def _catalog(cur):
    """The album folders the catalog still has files in."""
    cur.execute("SELECT file_path FROM media_files ORDER BY file_path")
    return [Path(p).parent.name for (p,) in cur.fetchall()]


def test_the_walk_names_what_it_could_not_read_and_takes_regular_files_only(
        cur, library, monkeypatch):
    denied = _albums_one_unreadable_one_gone(cur, library, monkeypatch)
    os.mkfifo(library / "A" / "a pipe.flac")      # opening it would block a reader for good
    os.symlink(library / "moved away.flac", library / "A" / "dangling.flac")

    audio, _cues, unread = scanner.LibraryScanner().find_audio_files()

    assert audio == [library / "A" / "01.flac"]
    assert unread == [denied]


def test_a_folder_with_an_entry_it_cannot_type_is_left_out_whole(library, monkeypatch):
    album = library / "CUE"
    album.mkdir()
    (album / "image.flac").touch()
    (album / "image.cue").touch()
    real = os.scandir

    class Untypable:
        """The cue as a stale mount lists it: a name whose type is EIO."""
        def __init__(self, entry):
            self._entry = entry

        def __getattr__(self, attr):
            return getattr(self._entry, attr)

        def is_dir(self, follow_symlinks=True):
            raise OSError(errno.EIO, "Input/output error", self._entry.path)

        is_file = is_dir

    class Listing:
        def __init__(self, path):
            self._it = real(path)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._it.close()

        def __iter__(self):
            return (Untypable(e) if e.name == "image.cue" else e for e in self._it)

    monkeypatch.setattr(os, "scandir",
                        lambda path=".": Listing(path) if os.fspath(path) == str(album) else real(path))

    audio, cues, unread = scanner.LibraryScanner().find_audio_files()

    # never the image without its cue: imported whole, it would supersede its slices
    assert (audio, cues, unread) == ([], [], [str(album)])


def test_a_rescan_keeps_what_it_could_not_read_and_prunes_the_rest(
        cur, library, main_module, monkeypatch):
    from canon import post_import
    _albums_one_unreadable_one_gone(cur, library, monkeypatch)
    monkeypatch.setattr(post_import, "run", lambda *a: None)
    main_module._scan_state.update(running=True, cancel_requested=False,
                                   progress="Starting scan...", stats=None, result=None)

    main_module._scan_worker(None, True, None, True)   # Rescan: the scan, then the prune

    prune = main_module._scan_state["result"]["prune"]
    assert (prune["pruned"], prune["kept_unread"]) == (1, 1)
    assert main_module._scan_state["progress"] == (
        "Scan complete — 1 folder(s) could not be read; the prune kept 1 file(s) there")
    assert _catalog(cur) == ["A", "B"]            # C pruned; B unread, so kept


def test_a_prune_of_its_own_keeps_what_it_could_not_read(cur, library, monkeypatch):
    _albums_one_unreadable_one_gone(cur, library, monkeypatch)

    stats = scanner.prune_missing_files()         # its own walk

    assert (stats["pruned"], stats["kept_unread"]) == (1, 1)
    assert _catalog(cur) == ["A", "B"]
