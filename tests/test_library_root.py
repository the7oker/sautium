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
- Since PI-NODE-2C (§2.3, §6.3, §9, §13): a volume plugged in that nothing
  mounted is the automount's small file, believed only as far as it goes
  (its boot, a type from a fixed map, a bounded label; never a link, a FIFO
  or a long file), a place that cannot be opened and a notice that leaves
  with the disk; its folder is watched by inotify, on a real folder here. A
  disk is a point a WHOLE filesystem is mounted at — in a container every
  root is a mount point, its bind — and "safe to pull" is the superblock's
  read-only word. A root that is a disk goes by the label the setup read; a
  root that is an empty folder and no disk is said at the start and listed
  as empty.
On a real PostgreSQL where a row is read or written; skipped without one.
"""

import json
import os
import select
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
    monkeypatch.setattr(library_root, "_mounts", lambda: {str(disks / "4A21-0000"): True})
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


def test_a_disk_the_setups_fstab_line_mounts_is_listed_as_the_automounts_own(tmp_path, monkeypatch):
    """Since 2026-11-24 the line mounts at the automount's point for its disk
    (PI-NODE-2D §2). On a machine with no automount the line's block made the
    folder and its labels' folder, and wrote the label: the one root is the
    disks' folder, the disk a row like any other — and no root is the disk."""
    disks = tmp_path / "media" / "sautium"
    (disks / ".names").mkdir(parents=True)                         # what the block makes: no .unread, no rule
    (disks / "4A21-0000" / "Music").mkdir(parents=True)
    (disks / ".names" / "4A21-0000").write_text("Elements\n")
    (disks / "0398-3084").mkdir()                                  # a line's mount point whose disk is out
    monkeypatch.setattr(settings, "sautium_library_roots", str(disks))
    monkeypatch.setattr(settings, "sautium_library_root_labels", "")
    monkeypatch.setattr(settings, "music_library_path", "")
    monkeypatch.setattr(library_root, "_mounts", lambda: {str(disks / "4A21-0000"): True})
    assert library_root._disks_folder(str(disks)) and library_root.unread_folders() == []
    (place,) = library_root.folders()["places"]
    assert (place["path"], place["kind"], place["name"], place["label"], place["read_only"]) == (
        str(disks / "4A21-0000"), "disk", "4A21-0000", "Elements", True)
    assert library_root.pullable(str(disks / "4A21-0000" / "Music"))
    assert not library_root._gone(str(disks), {})                  # every disk out: not a library gone
    assert library_root.folders(str(disks / "4A21-0000" / "Music"))["parent"] == str(disks / "4A21-0000")
    assert library_root.folders(str(disks / "4A21-0000"))["parent"] is None        # up from a disk: the places


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
    # the disk a folder lies on, for a page to name the place by: the mount point's own name is a UUID
    assert top["disk"] is None and jazz["disk"] is None
    on = library_root.folders(str(tree.disks / "4A21-0000" / "Rock"))["disk"]
    assert (on["path"], on["name"], on["label"]) == (str(tree.disks / "4A21-0000"), "4A21-0000", "Elements <b>")
    assert on["size_bytes"] > 0 and disk["disk"] == on
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
    # A scope with a root of its own would replace the library's path when joined to it
    assert not scanner.scope_admitted(tree.music, str(tree.outside))
    # … and on a launcher under Windows, one with a drive (a name such as "B:Side" is a folder elsewhere)
    assert all(scanner.scope_has_drive(s) for s in ("C:/Windows", "C:Windows", "\\\\server\\share\\x", "\\Windows"))
    assert not any(scanner.scope_has_drive(s) for s in ("Jazz/Album", "Vol..2", "AC:DC/Back in Black", "Jazz"))
    assert scanner.scope_admitted(tree.music, "Vol..2")           # two dots inside a name are a name


def test_a_root_is_read_as_it_was_written(tmp_path, monkeypatch):
    """A folder's name may end with a space; the setup carries it whole and
    the node reads the list the same way."""
    spaced = tmp_path / "Music "
    (spaced / "Jazz").mkdir(parents=True)
    monkeypatch.setattr(settings, "sautium_library_roots", f"{spaced}::{tmp_path / 'other'}")
    assert library_root.roots() == [str(spaced), str(tmp_path / "other")]
    assert library_root.within(str(spaced / "Jazz")) == str(spaced / "Jazz")


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
    points = library_root._mounts()
    assert "/" in points and "/proc" in points
    assert library_watch._unescape_mount("/media/My\\040Disk") == "/media/My Disk"
    with open(mount_watch.MOUNTINFO, "rb", buffering=0) as f:
        select.poll().register(f, select.POLLERR | select.POLLPRI)


# -- a volume nothing mounted (PI-NODE-2C §2.3) -------------------------------------------------

BOOT = "0b5c7a1e-3f0d-4c55-9a53-6f5a4f0f2a11"


@pytest.fixture
def unread(tree, tmp_path, monkeypatch):
    """The automount's folder of volumes it could not mount, and this boot."""
    folder = tree.disks / ".unread"
    folder.mkdir()
    boot = tmp_path / "boot_id"
    boot.write_text(BOOT + "\n")
    monkeypatch.setattr(library_root, "BOOT_ID", str(boot))
    return folder


def _volume(folder: Path, name: str, fs: str, label: str = "", boot: str = BOOT, driver: str = "-") -> Path:
    """As the automount writes it: beside, then renamed in."""
    tmp = folder / f".{name}.tmp"
    tmp.write_text(f"{boot}\n{fs}\n{label}\n{driver}\n")
    tmp.rename(folder / name)
    return folder / name


def test_a_volume_nothing_mounted_is_read_from_the_automounts_file(tree, unread):
    _volume(unread, "sda1", "ntfs", "Elements <b>", driver="ntfs3")
    _volume(unread, "sdb1", "apfs", "Mac disk")
    _volume(unread, "mmcblk0p3", "ext3", driver="rm -rf /")
    found = {v["device"]: v for v in library_root.unread()}
    assert list(found) == ["mmcblk0p3", "sda1", "sdb1"]
    # the driver its mount was tried with, from the automount's own five; anything else is none
    assert [found[d]["driver"] for d in ("sda1", "sdb1", "mmcblk0p3")] == ["ntfs3", None, None]
    # a type the automount mounts: this mount failed; one it never mounts
    assert (found["sda1"]["format"], found["sda1"]["mounts"], found["sda1"]["label"]) == (
        "NTFS", True, "Elements <b>")
    assert (found["sdb1"]["format"], found["sdb1"]["mounts"]) == ("APFS", False)
    assert (found["mmcblk0p3"]["format"], found["mmcblk0p3"]["mounts"], found["mmcblk0p3"]["label"]) == (
        "ext2/3", False, None)
    # the onset is the file's own time
    assert found["sda1"]["since"].startswith("20") and found["sda1"]["since"].endswith("+00:00")


@pytest.mark.parametrize("fs", ["zfs_member", "NTFS", "ntfs; rm -rf /", "<script>", "", "x" * 17, "ntfs\x00"])
def test_a_type_outside_the_map_is_never_shown_as_the_stick_wrote_it(tree, unread, fs):
    (unread / "sda1").write_bytes(f"{BOOT}\n{fs}\nLabel\n".encode())
    (volume,) = library_root.unread()
    assert (volume["format"], volume["mounts"], volume["label"]) == (None, False, "Label")


def test_only_the_automounts_own_small_file_of_this_boot_is_believed(tree, unread):
    _volume(unread, "sda1", "ntfs", "Here")
    _volume(unread, "sdb1", "ntfs", "A power cut left this", boot="another-boot")
    (unread / "sdc1").symlink_to(tree.outside / "secret.flac")
    os.mkfifo(unread / "sdd1")                       # a read that waited here would hang the test
    (unread / "sde1").write_text(f"{BOOT}\nntfs\n" + "L" * 400)
    (unread / "sdf1").mkdir()
    (unread / ".sdg1.tmp").write_text(f"{BOOT}\nntfs\nhalf written\n")
    (unread / "sd-h1").write_text(f"{BOOT}\nntfs\nnot a kernel name\n")
    (unread / "sdi1").write_text("")
    assert [v["device"] for v in library_root.unread()] == ["sda1"]
    # a label is cut where the automount would have cut it, on a character
    (unread / "sdj1").write_text(f"{BOOT}\nexfat\n" + "é" * 100)
    labels = {v["device"]: v["label"] for v in library_root.unread()}
    assert labels["sdj1"] == "é" * (library_root.LABEL_BYTES_MAX // 2)


def test_no_boot_to_compare_with_believes_nothing(tree, unread, monkeypatch):
    _volume(unread, "sda1", "ntfs")
    monkeypatch.setattr(library_root, "BOOT_ID", str(tree.top / "absent"))
    assert library_root.unread() == []


def test_a_node_with_no_automount_folder_has_no_unread_volume(tree):
    assert library_root.unread_folders() == [] and library_root.unread() == []


def test_an_unread_volume_is_a_place_that_cannot_be_opened(tree, unread):
    _volume(unread, "sda1", "ntfs", "Elements")
    places = library_root.folders()["places"]
    assert [p["kind"] for p in places] == ["folder", "disk", "unread"]
    assert places[-1] == {"path": None, "name": "sda1", "kind": "unread", "label": "Elements",
                          "size_bytes": None, "format": "NTFS", "mounts": True, "driver": None, "unclear": None}


@pytest.mark.parametrize("word", ["ambivalent", "unknown"])
def test_a_volume_no_one_filesystem_is_found_on_is_read_by_the_automounts_own_word(tree, unread, word):
    """The automount's two words where blkid names no filesystem (PI-NODE-2D
    §5): the marks of more than one, or none. The file keeps its four lines —
    no label, no driver — and the word, never a format, chooses the sentence."""
    _volume(unread, "sda1", word)
    (volume,) = library_root.unread()
    assert (volume["unclear"], volume["format"], volume["mounts"], volume["label"], volume["driver"]) == (
        word, None, False, None, None)
    place = library_root.folders()["places"][-1]
    assert (place["kind"], place["path"], place["unclear"]) == ("unread", None, word)
    # a type blkid did name is never one of the two, whatever a stick calls itself
    for named in ("ntfs", "apfs", "zfs_member", "Unknown", "ambivalent2", "unknown\x00"):
        (unread / "sda1").write_bytes(f"{BOOT}\n{named}\nLabel\n-\n".encode())
        assert library_root.unread()[0]["unclear"] is None, named


def _readable(fd: int, seconds: float) -> bool:
    return bool(select.select([fd], [], [], seconds)[0])


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="inotify is Linux's")
def test_the_unread_folder_wakes_on_a_file_moved_in_and_on_one_removed(tree, unread):
    import mount_watch
    fd = mount_watch.unread_watch(library_root.unread_folders())
    try:
        assert not _readable(fd, 0.2)
        tmp = unread / ".sda1.tmp"
        tmp.write_text(f"{BOOT}\nntfs\nElements\n")
        assert not _readable(fd, 0.2)                # a file still being written is no event
        tmp.rename(unread / "sda1")
        assert _readable(fd, 5) and os.read(fd, 65536)
        assert [v["device"] for v in library_root.unread()] == ["sda1"]
        assert not _readable(fd, 0.2)                # one read took every queued event
        (unread / "sda1").unlink()
        assert _readable(fd, 5) and os.read(fd, 65536)
        assert library_root.unread() == []
    finally:
        os.close(fd)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="inotify is Linux's")
def test_the_watch_is_armed_at_the_start_only_where_the_folder_is(tree, monkeypatch, caplog):
    import logging

    import mount_watch
    started = []
    monkeypatch.setattr(mount_watch.threading, "Thread", lambda **kw: started.append(kw) or type(
        "T", (), {"start": lambda self: None})())
    with caplog.at_level(logging.INFO, logger="mount_watch"):
        assert mount_watch.start() is True
    assert [kw["name"] for kw in started] == ["mount-watch"]
    (tree.disks / ".unread").mkdir()
    started.clear()
    assert mount_watch.start() is True
    assert [kw["name"] for kw in started] == ["unread-watch", "mount-watch"]
    os.close(started[0]["args"][0])
    assert f"watching {tree.disks / '.unread'} for disks nothing mounted" in caplog.text


def test_a_change_of_the_unread_volumes_wakes_the_notices_and_the_library_stream(db, scratch_dsn, monkeypatch):
    import mount_watch
    from routers import settings as settings_router
    woken = []
    monkeypatch.setattr(settings_router, "notify_library_subscribers", lambda: woken.append(1))
    listener = psycopg2.connect(scratch_dsn)
    listener.autocommit = True
    with listener.cursor() as c:
        c.execute("LISTEN sautium_notices")
    mount_watch._unread_changed()
    assert select.select([listener], [], [], 5)[0]
    listener.poll()
    assert [n.channel for n in listener.notifies] == ["sautium_notices"]
    assert woken == [1]
    listener.close()


def test_an_unread_volume_is_a_notice_that_leaves_with_its_file(tree, unread, db):
    from routers import settings as settings_router

    def notices():
        return {n["key"]: n for n in settings_router._notice_items() if n["key"].startswith("disk.unread.")}
    assert notices() == {}
    path = _volume(unread, "sda1", "ntfs", "Elements <b>", driver="ntfs-3g")
    (item,) = notices().values()
    assert (item["key"], item["kind"], item["until"]) == ("disk.unread.sda1", "warning", None)
    assert item["data"] == {"label": "Elements <b>", "format": "NTFS", "mounts": True, "driver": "ntfs-3g",
                            "unclear": None}
    onset = item["since"]
    # plugged in again: another file, another onset — news for the toast
    path.unlink()
    assert notices() == {}
    os.utime(_volume(unread, "sda1", "ntfs", "Elements <b>"), (1_800_000_000, 1_800_000_000))
    assert notices()["disk.unread.sda1"]["since"] != onset
    # a volume the automount found no one filesystem on: the notice carries its word for the page's sentence
    _volume(unread, "sdb1", "ambivalent")
    assert notices()["disk.unread.sdb1"]["data"] == {"label": None, "format": None, "mounts": False, "driver": None,
                                                     "unclear": "ambivalent"}


# -- what a disk is, and what can be pulled (§9, §13) ---------------------------------------------

MOUNTINFO = """\
22 1 259:2 / / rw,relatime shared:1 - ext4 /dev/root rw
61 22 259:2 /home/pi/Music /home/pi/Music ro,relatime master:1 - ext4 /dev/root rw
62 22 259:2 /media/sautium /media/sautium ro,relatime master:1 - ext4 /dev/root rw
63 62 8:1 / /media/sautium/4A21-0000 ro,nosuid,nodev,noexec master:9 - exfat /dev/sda1 ro,uid=1000
64 62 8:17 / /media/sautium/My\\040Disk ro,nosuid,nodev,noexec master:10 - fuseblk /dev/sdb1 ro,user_id=0
65 22 259:2 /mnt/sautium-5678-EF01 /mnt/sautium-5678-EF01 ro,relatime master:1 - ext4 /dev/root rw
66 65 8:33 / /mnt/sautium-5678-EF01 ro,relatime master:11 - vfat /dev/sdc1 ro,fmask=0133
67 22 8:49 / /srv/music ro,relatime master:12 - ext4 /dev/sdd1 rw,errors=remount-ro
"""


def test_a_disk_is_a_whole_filesystem_and_read_only_is_the_superblocks_word(tmp_path, monkeypatch):
    table = tmp_path / "mountinfo"
    table.write_text(MOUNTINFO)
    monkeypatch.setattr(library_root, "MOUNTINFO", str(table))
    assert library_root._mounts() == {
        "/": False,
        # a root's bind and the automount's folder are no disks: a part of the machine's own filesystem
        "/media/sautium/4A21-0000": True, "/media/sautium/My Disk": True,
        # the fstab line's disk over the bind of its empty mount point
        "/mnt/sautium-5678-EF01": True,
        # a disk the machine mounted to write: read-only here is only the bind
        "/srv/music": False}


def test_a_root_that_is_a_disk_goes_by_the_label_the_setup_read(tree, monkeypatch, caplog):
    disk = str(tree.disks / "4A21-0000")
    monkeypatch.setattr(library_root, "_mounts", lambda: {str(tree.music): True, disk: False})
    monkeypatch.setattr(settings, "sautium_library_root_labels", json.dumps(
        {str(tree.music): "My <Music>\nsecond line", "/elsewhere": "x", disk: 7}))
    places = {p["path"]: p for p in library_root.folders()["places"]}
    root = places[str(tree.music)]
    assert (root["kind"], root["name"], root["label"], root["read_only"]) == (
        "disk", str(tree.music), "My <Music>", True)
    assert root["size_bytes"] > 0
    # the automount's disk keeps the automount's label
    assert (places[disk]["label"], places[disk]["read_only"]) == ("Elements <b>", False)
    monkeypatch.setattr(settings, "sautium_library_root_labels", json.dumps({str(tree.music): "L" * 5000}))
    assert library_root.folders()["places"][0]["label"] == "L" * library_root.LABEL_BYTES_MAX
    for broken in ("{not json", json.dumps(["a list"]), ""):
        monkeypatch.setattr(settings, "sautium_library_root_labels", broken)
        assert library_root.folders()["places"][0]["label"] is None
    assert "SAUTIUM_LIBRARY_ROOT_LABELS is not JSON" in caplog.text


def test_only_a_folder_on_a_disk_mounted_read_only_can_be_pulled(tree, monkeypatch):
    disk = tree.disks / "4A21-0000"
    assert library_root.pullable(str(disk / "Rock")) and library_root.pullable(str(disk))
    assert not library_root.pullable(str(tree.music / "Jazz"))          # a folder of the machine's own
    assert not library_root.pullable(str(disk) + "-beside")
    assert not library_root.pullable("") and not library_root.pullable(None)
    monkeypatch.setattr(library_root, "_mounts", lambda: {str(disk): False})
    assert not library_root.pullable(str(disk / "Rock"))                # mounted to write by someone
    monkeypatch.setattr(settings, "sautium_library_roots", "")
    assert not library_root.pullable(str(disk / "Rock"))


# -- a root that is gone (§6.3) -------------------------------------------------------------------

def _copy_under(cursor, folder) -> str:
    """A copy the library holds from `folder`; the album's id, to remove it by."""
    import uuid
    album = str(uuid.uuid4())
    cursor.execute("INSERT INTO albums (id, title) VALUES (%s, 'Held')", (album,))
    cursor.execute("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, %s)", (album, str(folder)))
    return album


def test_a_root_that_is_an_empty_folder_and_no_disk_lists_as_empty(tree, db, monkeypatch):
    gone = tree.top / "mnt" / "sautium-5678-EF01"
    gone.mkdir(parents=True)
    monkeypatch.setattr(settings, "sautium_library_roots", f"{tree.music}:{tree.disks}:{gone}")
    places = {p["path"]: p for p in library_root.folders()["places"]}
    assert places[str(gone)]["kind"] == "folder" and places[str(gone)]["empty"] is True
    assert places[str(tree.music)]["empty"] is False
    # An empty folder the library never read a copy under is new, not lost (PI-NODE-2C F2): `held`
    # tells them apart, and is asked only of an empty one
    assert places[str(gone)]["held"] is False and places[str(tree.music)]["held"] is None
    albums = [_copy_under(db, str(gone) + "-beside" + "/Album"), _copy_under(db, tree.music / "Jazz" / "Album")]
    assert not library_root._held(str(gone))               # a folder whose name only begins like the root's
    albums.append(_copy_under(db, gone / "Rock" / "Album"))
    try:
        assert library_root._held(str(gone)) and library_root._held(str(gone) + "/")
        assert {p["path"]: p for p in library_root.folders()["places"]}[str(gone)]["held"] is True
        # a copy at an HQPlayer under the same folder's name is not this node's file
        db.execute("DELETE FROM albums WHERE id = %s", (albums.pop(),))
        db.execute("INSERT INTO hqp_endpoints (name) VALUES ('box') RETURNING id")
        endpoint = db.fetchone()[0]
        album = _copy_under(db, tree.top / "elsewhere")
        albums.append(album)
        db.execute("UPDATE album_variants SET location = 'hqplayer', hqp_endpoint_id = %s, directory_path = %s "
                   "WHERE album_id = %s", (endpoint, str(gone / "Rock" / "Album"), album))
        assert not library_root._held(str(gone))
    finally:
        db.execute("DELETE FROM albums WHERE id = ANY(%s::uuid[])", (albums,))
        db.execute("DELETE FROM hqp_endpoints WHERE name = 'box'")
    # its disk mounted: a disk, whatever it holds
    monkeypatch.setattr(library_root, "_mounts", lambda: {str(gone): True})
    assert library_root.folders()["places"][-1]["kind"] == "disk"
    assert not library_root._gone(str(gone), {str(gone): True})
    # the automount's folder with no disk in it is not a gone root
    assert not library_root._gone(str(tree.disks), {})


def test_the_start_says_which_root_is_gone_and_how_to_forget_it(tree, db, monkeypatch, caplog):
    gone = tree.top / "mnt" / "sautium-5678-EF01"
    gone.mkdir(parents=True)
    monkeypatch.setattr(settings, "sautium_library_roots", f"{tree.music}:{tree.disks}:{gone}")
    caplog.set_level("INFO")
    library_root.apply()
    said = [(r.levelname, r.getMessage()) for r in caplog.records if "is empty" in r.getMessage()]
    # a folder nothing was ever read under is new: no warning of a library away (PI-NODE-2C F2)
    assert said == [("INFO", f"music root {gone} is empty: nothing is in it yet")]
    caplog.clear()
    album = _copy_under(db, gone / "Album")
    try:
        library_root.apply()
    finally:
        db.execute("DELETE FROM albums WHERE id = %s", (album,))
    said = [(r.levelname, r.getMessage()) for r in caplog.records if "is empty" in r.getMessage()]
    assert said == [("WARNING", f"music root {gone} is empty: its disk is not mounted, or the folder was removed; "
                                f"its albums are away. Forget it on the machine: ./sautium roots --forget {gone}")]
