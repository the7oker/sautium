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
from sqlalchemy import text

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
    # The notice's onset and the folder's state are process state: each test
    # starts with none armed and the folder answering, once the look worker
    # is idle — a look still running would land in this test.
    assert _wait_until(_worker_idle)
    monkeypatch.setattr(settings_router, "_derived_since", {})
    for name, value in (("_folder_unreachable", False), ("_shown", False), ("_recorded", 0),
                        ("_look_given_up", False), ("_look_wakes_wanted", False)):
        monkeypatch.setattr(scanner, name, value)
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    with conn.cursor() as c:
        for table in ("user_settings", "media_files", "album_variants", "albums", "tracks"):
            c.execute(f"DELETE FROM {table}")
        yield c
    assert _wait_until(_worker_idle)
    conn.close()
    engine.dispose()
    pool.closeall()


def _worker_idle():
    return not scanner._looking and not scanner._look_wanted


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
        cur, library, monkeypatch, heard):
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
    # the look answers after all — a disk spinning up, not a dead mount: the
    # tabs that read "unreachable" on its behalf are woken
    assert heard() == ["sautium_notices"]
    assert "library.mount_missing" not in _keys()


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
        media._serve_disk(owned, "audio/flac", _NoRange(), library=True)
    assert heard() == ["sautium_notices"]         # the library's own file: the folder is gone
    with pytest.raises(HTTPException):
        media._serve_disk(owned, "audio/flac", _NoRange(), library=True)
    assert heard(timeout=0.5) == []               # a retry wakes nobody again

    (library / "A").mkdir()
    Path(owned).write_bytes(b"x" * 16)
    media._serve_disk(owned, "audio/flac", _NoRange(), library=True)
    assert heard() == ["sautium_notices"]         # served: the folder answers again
    media._serve_disk(owned, "audio/flac", _NoRange(), library=True)
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
    plan = scanner.plan_prune(disk_paths={f"{album_dir}/02.flac"})

    assert (plan["pruned"], plan["file_ids"]) == (0, [])
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


def _rescan(main_module, monkeypatch):
    """The Library's Rescan up to its question: the scan, then the plan."""
    from canon import post_import
    monkeypatch.setattr(post_import, "run", lambda *a: None)
    main_module._scan_state.update(running=True, cancel_requested=False,
                                   progress="Starting scan...", stats=None, result=None)
    main_module._scan_worker(None, True, None, True)
    return main_module._scan_state


def _answer(main_module, plan_id, remove):
    """The owner's answer, as the Library screen sends it; a yes runs the
    removal in a worker of its own, waited for here."""
    out = asyncio.run(main_module.scan_prune(plan_id=plan_id, remove=remove))
    assert _wait_until(lambda: not main_module._scan_state["running"], timeout=60)
    return out


def test_a_rescan_keeps_what_it_could_not_read_and_removes_the_rest_at_the_yes(
        cur, library, main_module, monkeypatch):
    _albums_one_unreadable_one_gone(cur, library, monkeypatch)

    state = _rescan(main_module, monkeypatch)

    prune = state["result"]["prune"]
    assert (prune["pruned"], prune["kept_unread"]) == (1, 1)
    assert state["progress"] == (
        "Scan complete — 1 folder(s) could not be read; the prune keeps 1 file(s) there"
        " — 1 missing file(s) wait for your answer")
    assert _catalog(cur) == ["A", "B", "C"]       # nothing removed before the yes

    _answer(main_module, state["prune_plan"]["id"], True)

    assert state["result"]["prune"]["pruned"] == 1
    assert state["progress"] == "Removal complete — 1 missing file(s) removed"
    assert _catalog(cur) == ["A", "B"]            # C removed; B unread, so kept


def test_a_prune_of_its_own_keeps_what_it_could_not_read(cur, library, monkeypatch):
    _albums_one_unreadable_one_gone(cur, library, monkeypatch)

    plan = scanner.plan_prune()                   # its own walk

    assert (plan["pruned"], plan["kept_unread"]) == (1, 1)
    assert _catalog(cur) == ["A", "B", "C"]
    scanner.prune_missing_files(plan["file_ids"])
    assert _catalog(cur) == ["A", "B"]


# ── ROOT-1: a part of the library that goes missing loses nothing ──────────
# PI5-4A § Q2's demonstration on the Pi: a Rescan with a folder moved out
# deleted, unasked, every album whose files were all there and the tracks
# nobody had played or analysed, and when the folder came back its analysis
# stayed unlinked, so the next pass recomputed it in full.

def test_the_plan_names_what_the_removal_would_delete_and_deletes_nothing(
        cur, library, main_module, monkeypatch):
    _owned_file(cur)                              # one file, its track, album, variant
    (library / "Other").mkdir()
    (library / "Other" / "01.flac").touch()
    _owned_file(cur, "Other")
    cur.execute("SELECT count(*) FROM tracks")
    tracks = cur.fetchone()[0]

    state = _rescan(main_module, monkeypatch)

    plan = state["prune_plan"]
    assert plan["counts"] == {"files": 1, "tracks": 1, "albums": 1, "artists": 0}
    # the Library screen and /scan/status show it without the file ids
    lib = asyncio.run(settings_router._library_state())
    assert lib["scan"]["prune_plan"] == {"id": plan["id"], "files": 1, "tracks": 1,
                                         "albums": 1, "artists": 0}
    assert asyncio.run(main_module.scan_status())["prune_plan"] == lib["scan"]["prune_plan"]
    cur.execute("SELECT count(*) FROM tracks")
    assert cur.fetchone()[0] == tracks
    assert _catalog(cur) == ["Other", "Shaped By Time"]


def test_a_no_keeps_everything_and_an_answer_is_taken_once(
        cur, library, main_module, monkeypatch):
    from fastapi import HTTPException
    (library / "A").mkdir()
    (library / "A" / "01.flac").touch()
    _owned_file(cur, "A")
    _owned_file(cur, "Gone")
    state = _rescan(main_module, monkeypatch)
    plan_id = state["prune_plan"]["id"]

    assert _answer(main_module, plan_id, False) == {"success": True, "removed": False}

    assert state["prune_plan"] is None
    assert state["progress"] == "Scan complete — 1 missing file(s) kept"
    assert _catalog(cur) == ["A", "Gone"]
    with pytest.raises(HTTPException) as again:   # a second device's yes
        asyncio.run(main_module.scan_prune(plan_id=plan_id, remove=True))
    assert again.value.status_code == 409
    assert _catalog(cur) == ["A", "Gone"]


def test_a_rescan_with_nothing_missing_asks_nothing(cur, library, main_module, monkeypatch):
    (library / "A").mkdir()
    (library / "A" / "01.flac").touch()
    _owned_file(cur, "A")

    state = _rescan(main_module, monkeypatch)

    assert state["prune_plan"] is None
    assert "prune_plan" not in state["result"]
    assert state["progress"] == "Scan complete"


def test_a_file_back_before_the_yes_keeps_its_rows(cur, library, main_module, monkeypatch):
    (library / "A").mkdir()
    (library / "A" / "01.flac").touch()
    _owned_file(cur, "A")
    _owned_file(cur, "Back")
    _owned_file(cur, "Gone")
    state = _rescan(main_module, monkeypatch)
    assert state["prune_plan"]["counts"]["files"] == 2
    (library / "Back").mkdir()
    (library / "Back" / "01.flac").touch()        # reconnected while the dialog was open

    _answer(main_module, state["prune_plan"]["id"], True)

    assert state["result"]["prune"]["pruned"] == 1
    assert _catalog(cur) == ["A", "Back"]


def test_a_yes_with_the_folder_gone_removes_nothing(cur, library, main_module, monkeypatch):
    (library / "A").mkdir()
    (library / "A" / "01.flac").touch()
    _owned_file(cur, "A")
    _owned_file(cur, "Gone")
    state = _rescan(main_module, monkeypatch)
    (library / "A" / "01.flac").unlink()
    (library / "A").rmdir()                       # the drive left with the dialog open

    _answer(main_module, state["prune_plan"]["id"], True)

    assert state["result"]["prune"]["pruned"] == 0
    assert _catalog(cur) == ["A", "Gone"]


def test_a_new_scan_replaces_the_plan(cur, library, main_module, monkeypatch):
    from fastapi import HTTPException
    from canon import post_import
    (library / "A").mkdir()
    (library / "A" / "01.flac").touch()
    _owned_file(cur, "A")
    _owned_file(cur, "Gone")
    state = _rescan(main_module, monkeypatch)
    stale = state["prune_plan"]["id"]
    monkeypatch.setattr(post_import, "run", lambda *a: None)

    asyncio.run(main_module.scan_start())         # "Scan for new"
    assert _wait_until(lambda: not state["running"], timeout=60)

    assert state["prune_plan"] is None
    with pytest.raises(HTTPException) as refused:
        asyncio.run(main_module.scan_prune(plan_id=stale, remove=True))
    assert refused.value.status_code == 409
    assert _catalog(cur) == ["A", "Gone"]


def test_the_cli_asks_before_it_removes(cur, library, monkeypatch):
    from click.testing import CliRunner
    import cli
    (library / "A").mkdir()
    (library / "A" / "01.flac").touch()
    _owned_file(cur, "A")
    _owned_file(cur, "Gone")
    monkeypatch.setattr(cli, "scan_library", lambda **kw: {
        "processed": 1, "added": 0, "skipped": 1, "errors": 0, "unread": 0})

    no = CliRunner().invoke(cli.cli, ["scan", "--prune"], input="n\n")
    assert "Nothing removed" in no.output, no.output
    assert _catalog(cur) == ["A", "Gone"]

    yes = CliRunner().invoke(cli.cli, ["scan", "--prune"], input="y\n")
    assert "Pruned: 1 missing files" in yes.output, yes.output
    assert _catalog(cur) == ["A"]


def _flac(path: Path, seed: int, title: str, artist: str = "Ostrava Unit",
          album: str = "Pulse Archive", track: int = 1):
    """A real FLAC: 15 s of noise from `seed` — one master per seed, so the
    fingerprint tells two seeds apart and one seed's copies alike."""
    import subprocess
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", f"anoisesrc=d=15:c=pink:r=44100:a=0.3:s={seed}",
                    "-sample_fmt", "s16",
                    "-metadata", f"title={title}", "-metadata", f"artist={artist}",
                    "-metadata", f"album={album}", "-metadata", f"tracknumber={track}",
                    str(path)], check=True)


def _analyse(cur, title):
    """The analysis passes' record of a track, as they write it: the source
    registered by its fingerprint (provenance.get_or_create_local), then an
    embedding and the features linked to it."""
    import provenance
    from database import get_db_context
    cur.execute("""
        SELECT mf.id, mf.track_id, mf.file_path, mf.sample_rate, mf.bit_depth,
               mf.is_lossless, mf.duration_seconds
        FROM media_files mf JOIN tracks t ON t.id = mf.track_id
        WHERE t.title = %s AND mf.is_analysis_source""", (title,))
    mf_id, track_id, path, sr, bd, ll, dur = cur.fetchone()
    with get_db_context() as db:
        sid = provenance.get_or_create_local(db, track_id, mf_id, path, sr, bd, ll, dur)
    assert sid is not None
    cur.execute("INSERT INTO embedding_models (id, name, dimension) "
                "VALUES (%s, 'root-1 test model', 512) ON CONFLICT (name) DO NOTHING",
                (str(uuid.uuid4()),))
    cur.execute("INSERT INTO embeddings (vector, model_id, track_id, analysis_source_id) "
                "SELECT %s::vector, id, %s, %s FROM embedding_models "
                "WHERE name = 'root-1 test model'",
                ("[" + ",".join(["0.1"] * 512) + "]", track_id, sid))
    cur.execute("INSERT INTO audio_features (track_id, analysis_source_id) VALUES (%s, %s)",
                (track_id, sid))
    return str(track_id), sid


def _queued(cur, title):
    """Is the track in the queue of either analysis pass — their own SQL."""
    from database import get_db_context
    import audio_analysis
    import embeddings
    params = {"min_seconds": 10, "reanalyze": False}
    out = {}
    with get_db_context() as db:
        for name, sql in (("embeddings", embeddings.PENDING_SQL),
                          ("features", audio_analysis.PENDING_SQL)):
            rows = db.execute(text(f"SELECT 1 FROM ({sql}) q JOIN tracks tt "
                                   f"ON tt.id = q.track_id WHERE tt.title = :title"),
                              {**params, "title": title}).fetchall()
            out[name] = bool(rows)
    return out


def _sources(cur, track_id):
    cur.execute("SELECT s.media_file_id, mf.file_path FROM analysis_sources s "
                "LEFT JOIN media_files mf ON mf.id = s.media_file_id WHERE s.track_id = %s",
                (track_id,))
    return cur.fetchall()


@pytest.fixture
def scanned_library(cur, library, main_module, monkeypatch):
    """PI5-4A § Q2's set-up on a real scan: an analysed album (Pulse
    Archive, two tracks) and a probe EP nobody analysed or played."""
    _flac(library / "Ostrava Unit" / "Pulse Archive" / "01.flac", 1, "Carrier", track=1)
    _flac(library / "Ostrava Unit" / "Pulse Archive" / "02.flac", 2, "Relay", track=2)
    _flac(library / "Q2 Probe" / "Probe EP" / "01.flac", 3, "Probe B",
          artist="Q2 Probe", album="Probe EP")
    from canon import post_import
    monkeypatch.setattr(post_import, "run", lambda *a: None)
    main_module._scan_state.update(running=True, cancel_requested=False,
                                   progress="Starting scan...", stats=None, result=None)
    main_module._scan_worker(None, True, None, False)
    analysed = {t: _analyse(cur, t) for t in ("Carrier", "Relay")}
    for t in ("Carrier", "Relay"):
        assert _queued(cur, t) == {"embeddings": False, "features": False}
    assert _queued(cur, "Probe B") == {"embeddings": True, "features": True}
    return analysed


def _move(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)


def test_a_folder_that_comes_back_gets_its_analysis_back_not_a_new_pass(
        cur, library, main_module, monkeypatch, tmp_path, scanned_library):
    """Q2's steps 5b–6: two folders moved out, Rescan, the yes; the folders
    back, "Scan for new"."""
    away = tmp_path / "away"
    _move(library / "Ostrava Unit", away / "Ostrava Unit")
    _move(library / "Q2 Probe", away / "Q2 Probe")
    (library / "Keep").mkdir()                    # the root itself stays reachable
    _flac(library / "Keep" / "01.flac", 9, "Anchor", artist="Keeper", album="Keep")

    state = _rescan(main_module, monkeypatch)
    _answer(main_module, state["prune_plan"]["id"], True)

    for title, (track_id, _sid) in scanned_library.items():
        cur.execute("SELECT count(*) FROM media_files WHERE track_id = %s", (track_id,))
        assert cur.fetchone()[0] == 0
        assert _sources(cur, track_id) == [(None, None)]   # kept, unlinked
    cur.execute("SELECT count(*) FROM tracks WHERE title = 'Probe B'")
    assert cur.fetchone()[0] == 0                 # nothing to keep it: an orphan, as before

    _move(away / "Ostrava Unit", library / "Ostrava Unit")
    _move(away / "Q2 Probe", library / "Q2 Probe")
    main_module._scan_state.update(running=True, cancel_requested=False,
                                   progress="Starting scan...", stats=None, result=None)
    main_module._scan_worker(None, True, None, False)        # "Scan for new"

    assert state["result"]["relinked"] == 2
    for title, (track_id, sid) in scanned_library.items():
        [(mf_id, path)] = _sources(cur, track_id)
        assert path.endswith(f"Pulse Archive/0{1 if title == 'Carrier' else 2}.flac")
        assert _queued(cur, title) == {"embeddings": False, "features": False}
    assert _queued(cur, "Probe B") == {"embeddings": True, "features": True}


def test_a_folder_moved_within_the_library_takes_its_analysis_along(
        cur, library, main_module, monkeypatch, scanned_library):
    """The same Rescan sees the old path missing and the new one: the new
    file is added beside the old row, the yes removes the old one, and the
    analysis follows the file the track is left with."""
    _move(library / "Ostrava Unit" / "Pulse Archive",
          library / "Ostrava Unit" / "Pulse Archive (2019)")

    state = _rescan(main_module, monkeypatch)
    assert state["prune_plan"]["counts"] == {"files": 2, "tracks": 0, "albums": 0,
                                             "artists": 0}
    _answer(main_module, state["prune_plan"]["id"], True)

    assert state["result"]["prune"]["relinked"] == 2
    for title, (track_id, _sid) in scanned_library.items():
        [(mf_id, path)] = _sources(cur, track_id)
        assert "Pulse Archive (2019)/" in path
        cur.execute("SELECT is_analysis_source FROM media_files WHERE id = %s", (mf_id,))
        assert cur.fetchone()[0] is True          # elected again once the old row went
        assert _queued(cur, title) == {"embeddings": False, "features": False}


def test_another_master_at_the_old_place_is_analysed_as_new(
        cur, library, main_module, monkeypatch, tmp_path, scanned_library):
    """A file that returns with other audio under the same tags is another
    master: its fingerprint matches nothing kept, so it is analysed."""
    away = tmp_path / "away"
    _move(library / "Ostrava Unit", away / "Ostrava Unit")
    state = _rescan(main_module, monkeypatch)
    _answer(main_module, state["prune_plan"]["id"], True)
    _flac(library / "Ostrava Unit" / "Pulse Archive" / "01.flac", 41, "Carrier", track=1)
    _move(away / "Ostrava Unit" / "Pulse Archive" / "02.flac",
          library / "Ostrava Unit" / "Pulse Archive" / "02.flac")

    main_module._scan_state.update(running=True, cancel_requested=False,
                                   progress="Starting scan...", stats=None, result=None)
    main_module._scan_worker(None, True, None, False)

    assert state["result"]["relinked"] == 1
    assert _queued(cur, "Carrier") == {"embeddings": True, "features": True}
    assert _queued(cur, "Relay") == {"embeddings": False, "features": False}
