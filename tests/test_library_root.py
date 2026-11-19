"""The library's folder chosen under mounted roots (backend/library_root.py,
mount_watch.py; PI-NODE-2B §4.3).

- The roots are the boundary: a path a page names is resolved and must stay
  under one — `..`, a link out, a folder beside a root are refused, by the
  picker and by the choice alike.
- The picker lists places (a folder named at setup; each disk mounted in the
  automount's folder, with the label left for it) and a folder's own folders
  with a bounded count of its audio files: no link, no hidden name, no
  AppleDouble file.
- The choice is a row; it is the library's folder at once and after a
  restart; a choice whose root is gone is no folder, and nothing stored goes.
- The scanner's two rules: a file named `._*` is never music; on a node that
  reads under roots a link that leaves them is not followed. A scanner with
  no folder refuses rather than walk the working directory.
- The mount table's wake, through its seam (a POLLPRI no pipe can fake).
On a real PostgreSQL where a row is read or written; skipped without one.
"""

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.pool  # noqa: E402

library_root = pytest.importorskip("library_root")
from config import settings  # noqa: E402


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """Two roots — a folder named at setup and the automount's folder with
    one disk in it — and what lies outside them."""
    music = tmp_path / "home" / "Music"
    (music / "Jazz" / "Album").mkdir(parents=True)
    (music / "Jazz" / "Album" / "01.flac").write_bytes(b"x")
    (music / "Jazz" / "Album" / "02.FLAC").write_bytes(b"x")
    (music / "Jazz" / "Album" / "._01.flac").write_bytes(b"resource fork")
    (music / "Jazz" / "Album" / "cover.jpg").write_bytes(b"x")
    (music / ".hidden").mkdir()
    disks = tmp_path / "media"
    (disks / ".names").mkdir(parents=True)
    (disks / "4A21-0000" / "Rock").mkdir(parents=True)
    (disks / "4A21-0000" / "Rock" / "a.mp3").write_bytes(b"x")
    (disks / "stub-of-a-disk-pulled").mkdir()
    (disks / ".names" / "4A21-0000").write_text("Elements <b>\nsecond line\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.flac").write_bytes(b"the node's key")
    (music / "Jazz" / "way-out").symlink_to(outside, target_is_directory=True)
    (music / "Jazz" / "Album" / "03.flac").symlink_to(outside / "secret.flac")
    (music / "Jazz" / "Album" / "04.flac").symlink_to(music / "Jazz" / "Album" / "01.flac")
    monkeypatch.setattr(settings, "sautium_library_roots", f"{music}:{disks}")
    monkeypatch.setattr(settings, "music_library_path", "")
    monkeypatch.setattr(settings, "music_host_path", None)
    monkeypatch.setattr(library_root, "_mount_points", lambda: {str(disks / "4A21-0000")})
    return type("Tree", (), {"music": music, "disks": disks, "outside": outside, "top": tmp_path})


@pytest.fixture
def db(scratch_dsn, monkeypatch):
    import db_pool
    pool = psycopg2.pool.ThreadedConnectionPool(1, 3, dsn=scratch_dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute("TRUNCATE library_roots")
        yield c
    conn.close()
    pool.closeall()


# -- the boundary --------------------------------------------------------------------------

def test_a_path_stays_under_a_root_or_is_refused(tree):
    assert library_root.picks() and library_root.roots() == [str(tree.music), str(tree.disks)]
    assert library_root.within(str(tree.music / "Jazz")) == str(tree.music / "Jazz")
    assert library_root.within(str(tree.music)) == str(tree.music)
    for leaving in (tree.music / ".." / "outside", tree.music / "Jazz" / "way-out",
                    tree.outside, tree.top / "home", Path(str(tree.music) + "-beside"),
                    tree.music / "Jazz" / "Album" / "03.flac"):
        assert library_root.within(str(leaving)) is None, leaving
    # A link that stays inside is itself
    assert library_root.within(str(tree.music / "Jazz" / "Album" / "04.flac")) == str(
        tree.music / "Jazz" / "Album" / "01.flac")


def test_a_node_whose_folder_is_named_outside_it_has_no_roots(monkeypatch):
    monkeypatch.setattr(settings, "sautium_library_roots", "")
    assert not library_root.picks() and library_root.within("/music") is None
    with pytest.raises(library_root.Refused, match="named where the node was set up"):
        library_root.folders()
    with pytest.raises(library_root.Refused):
        library_root.choose("/music")


# -- the picker ------------------------------------------------------------------------------

def test_the_places_are_the_named_folder_and_each_disk_that_is_there(tree):
    listing = library_root.folders()
    assert listing["under"] is None and listing["chosen"] is None
    places = {p["path"]: p for p in listing["places"]}
    # the named folder itself; the disk mounted in the automount's folder —
    # never the automount's folder, its labels' folder, or a pulled disk's stub
    assert list(places) == [str(tree.music), str(tree.disks / "4A21-0000")]
    assert places[str(tree.music)]["kind"] == "folder"
    disk = places[str(tree.disks / "4A21-0000")]
    assert (disk["kind"], disk["name"]) == ("disk", "4A21-0000")
    assert disk["label"] == "Elements <b>"            # one line, a stranger's text as it is
    assert disk["size_bytes"] > 0


def test_a_label_is_read_bounded_and_never_through_a_link(tree):
    names = tree.disks / ".names"
    (names / "long").write_text("L" * 5000)
    assert library_root._label(str(tree.disks), "long") == "L" * library_root.LABEL_BYTES_MAX
    (names / "linked").symlink_to(tree.outside / "secret.flac")
    assert library_root._label(str(tree.disks), "linked") is None
    (names / "folder").mkdir()
    assert library_root._label(str(tree.disks), "folder") is None
    assert library_root._label(str(tree.disks), "absent") is None


def test_a_folders_listing_is_its_real_folders_and_a_count_of_its_music(tree):
    top = library_root.folders(str(tree.music))
    # no hidden folder; the way up from a root is the places
    assert [f["name"] for f in top["folders"]] == ["Jazz"] and top["parent"] is None
    assert (top["audio_files"], top["capped"]) == (2, False)   # not ._01.flac, not the links, not the jpg
    jazz = library_root.folders(top["folders"][0]["path"])
    assert [f["name"] for f in jazz["folders"]] == ["Album"]   # way-out is a link: not listed
    assert jazz["parent"] == str(tree.music)
    disk = library_root.folders(str(tree.disks / "4A21-0000"))
    assert disk["parent"] is None and disk["audio_files"] == 1
    for refused in (str(tree.outside), str(tree.music / "Jazz" / "way-out"),
                    str(tree.music / "none"), str(tree.music / "Jazz" / "Album" / "01.flac")):
        with pytest.raises(library_root.Refused):
            library_root.folders(refused)


def test_the_count_stops_at_its_bound(tree, monkeypatch):
    monkeypatch.setattr(library_root, "COUNT_ENTRIES_MAX", 3)
    counted = library_root.folders(str(tree.music))
    assert counted["capped"] and counted["audio_files"] <= 3


# -- the choice --------------------------------------------------------------------------------

def test_the_choice_is_a_row_and_the_librarys_folder_at_once(tree, db):
    assert library_root.chosen() is None
    path = library_root.choose(str(tree.disks / "4A21-0000" / "Rock"))
    assert path == str(tree.disks / "4A21-0000" / "Rock") == settings.music_library_path
    db.execute("SELECT mount, subpath FROM library_roots")
    assert db.fetchall() == [(str(tree.disks), "4A21-0000/Rock")]
    assert library_root.chosen() == path and library_root.folders()["chosen"] == path
    # A root itself, then the first again: the newest choice is the folder
    assert library_root.choose(str(tree.music)) == str(tree.music)
    db.execute("SELECT mount, subpath FROM library_roots ORDER BY chosen_at DESC, id DESC")
    assert db.fetchall()[0] == (str(tree.music), "")
    library_root.choose(path)
    assert library_root.chosen() == path
    db.execute("SELECT count(*) FROM library_roots")
    assert db.fetchone()[0] == 2
    # A choice stored while the clock ran ahead (or made now, on a board that woke in the past) does not
    # outrank a later one: the newest choice is later than every stored one
    db.execute("UPDATE library_roots SET chosen_at = now() + interval '3 years' WHERE subpath <> ''")
    assert library_root.choose(str(tree.music)) == library_root.chosen() == str(tree.music)
    assert library_root.choose(path) == library_root.chosen() == path


def test_a_folder_outside_the_roots_is_never_chosen(tree, db):
    for refused in (tree.outside, tree.music / "Jazz" / "way-out", tree.music / ".." / "outside",
                    tree.music / "Jazz" / "Album" / "01.flac", tree.music / "gone"):
        with pytest.raises(library_root.Refused, match="not one this node may read"):
            library_root.choose(str(refused))
    assert library_root.chosen() is None and settings.music_library_path == ""


def test_the_stored_choice_is_the_folder_after_a_restart(tree, db, monkeypatch):
    library_root.apply()
    assert settings.music_library_path == ""                  # not chosen yet: not set
    chosen = library_root.choose(str(tree.music / "Jazz"))
    monkeypatch.setattr(settings, "music_library_path", "/music")
    monkeypatch.setattr(settings, "music_host_path", "E:/Music")
    library_root.apply()
    assert (settings.music_library_path, settings.music_host_path) == (chosen, None)
    assert settings.library_db_root() == chosen               # stored as read: no translation
    # The setup ran again with other folders: the choice lies under no root
    monkeypatch.setattr(settings, "sautium_library_roots", str(tree.disks))
    library_root.apply()
    assert settings.music_library_path == ""
    db.execute("SELECT count(*) FROM library_roots")
    assert db.fetchone()[0] == 1                              # nothing stored is touched


def test_a_node_with_no_roots_keeps_the_folder_its_env_names(monkeypatch):
    monkeypatch.setattr(settings, "sautium_library_roots", "")
    monkeypatch.setattr(settings, "music_library_path", "/music")
    monkeypatch.setattr(settings, "music_host_path", "/srv/music")
    library_root.apply()                                      # no database is asked
    assert (settings.music_library_path, settings.music_host_path) == ("/music", "/srv/music")


def test_the_route_stores_the_choice_rearms_the_watch_and_scans(tree, db, monkeypatch):
    from fastapi import HTTPException
    settings_router = pytest.importorskip("routers.settings")
    monkeypatch.chdir(BACKEND)
    import library_watch
    import main
    done = []
    monkeypatch.setattr(library_watch, "stop", lambda: done.append("stop"))
    monkeypatch.setattr(library_watch, "start", lambda request: done.append("start"))
    monkeypatch.setattr(main, "request_scan", lambda scopes, **kw: done.append((scopes, kw)))
    monkeypatch.setattr(settings_router, "notify_library_subscribers", lambda: done.append("wake"))
    answer = settings_router.put_library_root(
        settings_router.LibraryRootChoice(path=str(tree.music / "Jazz")))
    assert answer == {"music_path": str(tree.music / "Jazz")}
    assert done == ["stop", "start", ({"": True}, {"explicit": True}), "wake"]
    with pytest.raises(HTTPException) as refused:
        settings_router.put_library_root(settings_router.LibraryRootChoice(path=str(tree.outside)))
    assert refused.value.status_code == 400
    with pytest.raises(HTTPException) as refused:
        settings_router.get_library_folders(under=str(tree.outside))
    assert refused.value.status_code == 400
    monkeypatch.setattr(settings, "sautium_library_roots", "")
    with pytest.raises(HTTPException) as refused:
        settings_router.get_library_folders(under=None)
    assert refused.value.status_code == 409


def test_the_table_is_in_the_baseline_and_its_delta_alike(db):
    baseline = (REPO / "desktop" / "migrations" / "001_initial.sql").read_text(encoding="utf-8")
    delta = (REPO / "desktop" / "migrations" / "068_library_roots.sql").read_text(encoding="utf-8")
    ddl = delta[delta.index("CREATE TABLE IF NOT EXISTS library_roots"):].strip()
    assert ddl in baseline
    db.execute(delta)                                         # again, on the schema that has it
    db.execute("""SELECT conname FROM pg_constraint
                  WHERE conrelid = 'library_roots'::regclass AND contype IN ('p', 'u')
                  ORDER BY conname""")
    assert [r[0] for r in db.fetchall()] == ["library_roots_pkey", "library_roots_place"]


# -- the scanner's rules --------------------------------------------------------------------------

def test_an_appledouble_file_is_never_music(tree, monkeypatch):
    scanner = pytest.importorskip("scanner")
    import library_watch
    assert scanner.hidden_name("._Track.flac") and not scanner.hidden_name("Track.flac")
    assert not scanner.hidden_name(".flac") and not scanner.hidden_name("_.flac")
    monkeypatch.setattr(library_watch, "_WATCHED_SUFFIXES", {".flac", ".cue"})
    assert library_watch._watched("Track.flac") and not library_watch._watched("._Track.flac")


def _found(scanner, folder) -> list:
    audio, cues, unread = scanner.LibraryScanner(str(folder)).find_audio_files()
    assert unread == []
    return sorted(p.name for p in audio)


def test_a_link_that_leaves_the_roots_is_not_scanned(tree, caplog):
    scanner = pytest.importorskip("scanner")
    # 03.flac points outside the roots; 04.flac at a file inside them
    assert _found(scanner, tree.music) == ["01.flac", "02.FLAC", "04.flac"]
    assert "03.flac is a link that leaves the folders this node reads" in caplog.text


def test_a_scans_scope_is_a_folder_inside_the_library(tree, monkeypatch, caplog):
    """The walk judges entries; the scope's own folder, which a request
    names, is judged here: no `..` anywhere, no link out of the roots."""
    scanner = pytest.importorskip("scanner")
    walker = scanner.LibraryScanner(str(tree.music))
    assert scanner.scope_admitted(tree.music, "Jazz/Album") and scanner.scope_admitted(tree.music, "Jazz")
    for outside in ("../../outside", "Jazz/../../../outside", "..", "Jazz\\..\\..", "Jazz/way-out"):
        assert not scanner.scope_admitted(tree.music, outside), outside
        with pytest.raises(ValueError, match="not inside the library"):
            walker.find_audio_files(subpath=outside)
        audio, _cues, _unread = walker.find_audio_files(scopes=[(outside, True)])
        assert audio == [], outside
    assert "is not inside the library — not walked" in caplog.text
    assert [p.name for p in walker.find_audio_files(subpath="Jazz")[0]] != []
    # With no roots a folder link inside the library is a scope as before; `..` is one nowhere
    monkeypatch.setattr(settings, "sautium_library_roots", "")
    assert scanner.scope_admitted(tree.music, "Jazz/way-out")
    assert not scanner.scope_admitted(tree.music, "Jazz/../../outside")


def test_the_scan_route_refuses_a_scope_outside_the_library(tree, monkeypatch):
    import asyncio
    monkeypatch.chdir(BACKEND)
    main = pytest.importorskip("main")
    from fastapi import HTTPException
    from playback import hqp_benchmark
    monkeypatch.setattr(settings, "music_library_path", str(tree.music))
    monkeypatch.setattr(hqp_benchmark, "measures_here", lambda: False)
    monkeypatch.setattr("scanner.refuse_if_unreachable", lambda: None)
    asked = []
    monkeypatch.setattr(main, "request_scan", lambda scopes, **kw: asked.append(scopes) or False)
    for outside in ("../../outside", "Jazz/way-out"):
        with pytest.raises(HTTPException) as refused:
            asyncio.run(main.scan_start(subpath=outside))
        assert refused.value.status_code == 400
    assert asked == []
    asyncio.run(main.scan_start(subpath="/Jazz/Album/"))
    assert asked == [{"Jazz/Album": True}]


def test_a_node_with_no_roots_follows_a_link_as_before(tree, monkeypatch):
    scanner = pytest.importorskip("scanner")
    monkeypatch.setattr(settings, "sautium_library_roots", "")
    assert _found(scanner, tree.music) == ["01.flac", "02.FLAC", "03.flac", "04.flac"]


def test_a_scanner_with_no_folder_refuses_rather_than_walk_where_it_stands(monkeypatch):
    scanner = pytest.importorskip("scanner")
    monkeypatch.setattr(settings, "music_library_path", "")
    with pytest.raises(ValueError, match="No music folder is chosen"):
        scanner.LibraryScanner()


# -- the mount table's wake ---------------------------------------------------------------------

def test_every_change_of_the_mount_table_runs_its_readers(caplog):
    import mount_watch
    wakes = iter([True, True, True, False])
    seen = []

    def changed():
        seen.append(len(seen))
        if len(seen) == 2:
            raise OSError("a reader that failed")
    mount_watch.watch(lambda: next(wakes), changed)
    assert seen == [0, 1, 2]                                 # one failure ends nothing
    assert "a reader of the mount table failed" in caplog.text


def test_only_a_node_that_reads_under_roots_watches_the_mount_table(monkeypatch):
    import mount_watch
    monkeypatch.setattr(settings, "sautium_library_roots", "")
    started = []
    monkeypatch.setattr(mount_watch.threading, "Thread", lambda **kw: started.append(kw) or type(
        "T", (), {"start": lambda self: None})())
    assert mount_watch.start() is False and started == []


@pytest.mark.skipif(not os.path.exists("/proc/self/mountinfo"), reason="no mount table here")
def test_the_mount_table_is_read_as_the_kernel_writes_it():
    """The picker's "a disk that is there now" and the watch read one file:
    its mount points, a space in one unescaped (\\040), and a poll can be
    placed on it (the wake itself needs a mount, which no test may make)."""
    import select

    import library_watch
    import mount_watch
    points = library_root._mount_points()
    assert "/" in points and "/proc" in points
    assert library_watch._unescape_mount("/media/My\\040Disk") == "/media/My Disk"
    with open(mount_watch.MOUNTINFO, "rb", buffering=0) as f:
        select.poll().register(f, select.POLLERR | select.POLLPRI)
