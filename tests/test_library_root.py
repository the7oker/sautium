"""The library's folders under mounted roots (backend/library_root.py,
mount_watch.py; PI-NODE-2B §4.3; several folders since LIBRARY-FOLDERS-1).

- The roots are the boundary: a path a page names is resolved and must stay
  under one — `..`, a link out, a folder beside a root are refused, by the
  picker and by the choice alike.
- The picker lists places (a folder named at setup; each disk mounted in the
  automount's folder, with the label left for it) and a folder's own folders
  with a bounded count of its audio files: no link, no hidden name, no
  AppleDouble file.
- A folder added is a row, one of the library's folders at once and after a
  restart; one whose root is gone stays a folder, not connected, and nothing
  stored goes. A folder inside one is refused, one around folders takes them
  in, the disks' folder itself is no folder of music.
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
- Since PI-NODE-2D (§2, §5, §9.3): a disk the setup's fstab line mounts lies
  in the disks' folder and is listed as the automount's own; the automount's
  two words for a volume no one filesystem is found on; an empty root is
  lost only where the library holds copies from it, else new; a folder's
  listing names the disk it lies on. Real mounts: test_roots_real_mounts.py.
- Since PI-NODE-2E (§2): the disk the setup's line of before mounted at the
  root /mnt/sautium-<id> is the disk mounted at <the disks' folder>/<id>; at
  the node's start every stored path moves with it, the folder with them,
  the scan reads nothing again and sets nothing away.
- Since LIBRARY-FOLDERS-1: the one folder of before becomes the first rows
  (db_migrate's library_folders_v1), and a disk plugged in that the library
  never had is counted once and offered, never joined by itself.
On a real PostgreSQL where a row is read or written; skipped without one.
"""

import json
import os
import re
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
def tree(tmp_path, monkeypatch, db):
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
        c.execute("TRUNCATE library_folders")
        library_root.invalidate()
        yield c
        library_root.invalidate()
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
    assert library_root.kind() == "setup"
    with pytest.raises(library_root.Refused, match="not added here"):
        library_root.places()
    with pytest.raises(library_root.Refused, match="named where the node was set up"):
        library_root.add("/music")


# -- the picker ------------------------------------------------------------------------------

def test_the_places_are_the_named_folder_and_each_disk_that_is_there(tree):
    listing = library_root.places()
    assert listing["under"] is None
    places = {p["path"]: p for p in listing["places"]}
    # the named folder itself; the disk mounted in the automount's folder —
    # never the automount's folder, its labels' folder, or a pulled disk's stub
    assert list(places) == [str(tree.music), str(tree.disks / "4A21-0000")]
    assert places[str(tree.music)]["kind"] == "folder"
    disk = places[str(tree.disks / "4A21-0000")]
    assert (disk["kind"], disk["name"]) == ("disk", "4A21-0000")
    assert disk["label"] == "Elements <b>"            # one line, a stranger's text as it is
    assert disk["size_bytes"] > 0
    assert places[str(tree.music)]["in_library"] is None
    library_root.add(str(tree.disks / "4A21-0000" / "Rock"))
    places = {p["path"]: p for p in library_root.places()["places"]}
    # a disk one of whose folders is in the library: not the disk itself
    assert places[str(tree.disks / "4A21-0000")]["in_library"] is None
    library_root.add(str(tree.music))
    assert {p["path"]: p for p in library_root.places()["places"]}[str(tree.music)]["in_library"] == str(tree.music)


def test_a_disk_the_setups_fstab_line_mounts_is_listed_as_the_automounts_own(tmp_path, monkeypatch, db):
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
    (place,) = library_root.places()["places"]
    assert (place["path"], place["kind"], place["name"], place["label"], place["read_only"]) == (
        str(disks / "4A21-0000"), "disk", "4A21-0000", "Elements", True)
    assert library_root.pullable(str(disks / "4A21-0000" / "Music"))
    assert not library_root._gone(str(disks), {})                  # every disk out: not a library gone
    assert library_root.places(str(disks / "4A21-0000" / "Music"))["parent"] == str(disks / "4A21-0000")
    assert library_root.places(str(disks / "4A21-0000"))["parent"] is None        # up from a disk: the places


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
    top = library_root.places(str(tree.music))
    # no hidden folder; the way up from a root is the places
    assert [f["name"] for f in top["folders"]] == ["Jazz"] and top["parent"] is None
    assert (top["audio_files"], top["capped"]) == (2, False)   # not ._01.flac, not the links, not the jpg
    jazz = library_root.places(top["folders"][0]["path"])
    assert [f["name"] for f in jazz["folders"]] == ["Album"]   # way-out is a link: not listed
    assert jazz["parent"] == str(tree.music)
    disk = library_root.places(str(tree.disks / "4A21-0000"))
    assert disk["parent"] is None and disk["audio_files"] == 1
    # the disk a folder lies on, for a page to name the place by: the mount point's own name is a UUID
    assert top["disk"] is None and jazz["disk"] is None
    on = library_root.places(str(tree.disks / "4A21-0000" / "Rock"))["disk"]
    assert (on["path"], on["name"], on["label"]) == (str(tree.disks / "4A21-0000"), "4A21-0000", "Elements <b>")
    assert on["size_bytes"] > 0 and disk["disk"] == on
    for refused in (str(tree.outside), str(tree.music / "Jazz" / "way-out"),
                    str(tree.music / "none"), str(tree.music / "Jazz" / "Album" / "01.flac")):
        with pytest.raises(library_root.Refused):
            library_root.places(refused)


def test_the_count_stops_at_its_bound(tree, monkeypatch):
    monkeypatch.setattr(library_root, "COUNT_ENTRIES_MAX", 3)
    counted = library_root.places(str(tree.music))
    assert counted["capped"] and counted["audio_files"] <= 3


# -- a folder added ---------------------------------------------------------------------------

def _rows(db):
    db.execute("SELECT path, label FROM library_folders ORDER BY added_at, id")
    return db.fetchall()


def test_a_folder_added_is_a_row_and_a_folder_of_the_library_at_once(tree, db):
    assert library_root.paths() == []
    rock = str(tree.disks / "4A21-0000" / "Rock")
    added = library_root.add(rock)
    assert (added["folder"]["path"], added["merged"]) == (rock, [])
    # the disk's label is kept with the row: the automount removes its own copy with the disk
    assert _rows(db) == [(rock, "Elements <b>")]
    assert library_root.paths() == [rock]
    # a page names it by its disk and the folder under it, never the mount point's UUID
    assert library_root.folder_name(added["folder"]) == "Elements <b> · Rock"
    # a second folder beside it: several at once
    library_root.add(str(tree.music))
    assert library_root.paths() == [rock, str(tree.music)]
    assert library_root.folder_of(rock + "/LP/01.flac") == rock
    assert library_root.folder_of(rock + "-beside/01.flac") is None       # a name that only begins alike
    assert library_root.folder_of(str(tree.music / "Jazz" / "Album" / "01.flac")) == str(tree.music)


def test_a_folder_inside_one_is_refused_and_one_around_takes_them_in(tree, db):
    rock = tree.disks / "4A21-0000" / "Rock"
    (rock / "Live").mkdir()
    library_root.add(str(rock))
    with pytest.raises(library_root.Refused, match=r"already in your library, as part of Elements <b> · Rock"):
        library_root.add(str(rock / "Live"))
    with pytest.raises(library_root.Refused, match="already in your library"):
        library_root.add(str(rock))
    assert library_root.standing(str(rock / "Live")) == {"inside": "Elements <b> · Rock", "holds": []}
    disk = str(tree.disks / "4A21-0000")
    assert library_root.standing(disk) == {"inside": None, "holds": ["Elements <b> · Rock"]}
    added = library_root.add(disk)
    assert added["merged"] == [str(rock)]
    assert library_root.paths() == [disk]


def test_where_disks_appear_is_no_folder_of_music(tree, db):
    with pytest.raises(library_root.Refused, match="where disks appear"):
        library_root.add(str(tree.disks))
    assert library_root.paths() == []


def test_a_folder_outside_the_roots_is_never_added(tree, db):
    for refused in (tree.outside, tree.music / "Jazz" / "way-out", tree.music / ".." / "outside",
                    tree.music / "Jazz" / "Album" / "01.flac", tree.music / "gone"):
        with pytest.raises(library_root.Refused, match="not one this node may read"):
            library_root.add(str(refused))
    assert library_root.paths() == []


def test_a_folder_stays_a_folder_after_a_restart_and_under_a_root_no_longer_given(tree, db, monkeypatch):
    library_root.apply()
    assert library_root.paths() == []                         # none added yet
    jazz = library_root.add(str(tree.music / "Jazz"))["folder"]["path"]
    monkeypatch.setattr(settings, "music_library_path", "/music")   # the setup's empty folder: no folder
    library_root.apply()
    assert library_root.paths() == [jazz]
    # The setup ran again with other folders: the folder lies under no root — it stays, not
    # connected, its albums listed, forgotten only by the owner (LIBRARY-FOLDERS-1 §2.2)
    monkeypatch.setattr(settings, "sautium_library_roots", str(tree.disks))
    library_root.apply()
    assert library_root.paths() == [jazz]


def test_a_node_with_no_roots_has_the_folder_its_env_names(db, monkeypatch):
    monkeypatch.setattr(settings, "sautium_library_roots", "")
    monkeypatch.setattr(settings, "music_library_path", "/music")
    monkeypatch.setattr(settings, "music_host_path", "E:\\Music\\")
    assert library_root.given() == [{"id": None, "path": "E:/Music", "label": None}]
    library_root.apply()                                      # the start writes its row
    assert _rows(db) == [("E:/Music", None)]
    library_root.apply()
    assert _rows(db) == [("E:/Music", None)]
    # the .env changed: its folder is added, the old one stays a folder of the library
    monkeypatch.setattr(settings, "music_host_path", "F:/Music")
    library_root.apply()
    assert library_root.paths() == ["E:/Music", "F:/Music"]
    assert library_root.env_folder() == "F:/Music"


def test_an_env_folder_inside_one_of_before_narrows_it_and_no_folder_ever_nests(db, monkeypatch):
    """Review round 1 (M2): a setup node whose .env moved into a folder of
    before (E:/Music → E:/Music/Library) — the old row would hold the new,
    every file read under the outer, unreachable one, and its Forget would
    take the whole library. The .env's word narrows it; a folder the
    re-point or any writer left inside another is taken in; folder_of
    takes the deepest."""
    monkeypatch.setattr(settings, "sautium_library_roots", "")
    monkeypatch.setattr(settings, "music_library_path", "/music")
    monkeypatch.setattr(settings, "music_host_path", "E:/Music")
    library_root.apply()
    monkeypatch.setattr(settings, "music_host_path", "E:/Music/Library")
    library_root.apply()
    assert _rows(db) == [("E:/Music/Library", None)]
    monkeypatch.setattr(settings, "music_host_path", "E:/")               # a drive's root keeps its slash
    library_root.apply()
    assert _rows(db) == [("E:/", None)] and library_root.folder_of("E:/Album/01.flac") == "E:/"
    db.execute("INSERT INTO library_folders (path) VALUES ('E:/Inner')")   # as a writer outside add() would
    library_root.invalidate()
    assert library_root.folder_of("E:/Inner/01.flac") == "E:/Inner"         # the deepest
    library_root.apply()
    assert _rows(db) == [("E:/", None)]                                     # taken in


def test_the_env_folder_of_a_setup_node_is_not_forgotten_here(db, monkeypatch):
    import asyncio
    from fastapi import HTTPException
    monkeypatch.chdir(BACKEND)
    main = pytest.importorskip("main")
    monkeypatch.setattr(settings, "sautium_library_roots", "")
    monkeypatch.setattr(settings, "music_library_path", "/music")
    monkeypatch.setattr(settings, "music_host_path", "E:/Music")
    library_root.apply()
    (row,) = [f for f in library_root.given() if f["path"] == "E:/Music"]
    with pytest.raises(HTTPException) as no:
        asyncio.run(main.scan_forget(row["id"]))
    assert no.value.status_code == 409 and "MUSIC_LIBRARY_PATH" in no.value.detail


def test_the_route_adds_arms_the_watch_and_scans_the_folder_alone(tree, db, monkeypatch):
    from fastapi import HTTPException
    settings_router = pytest.importorskip("routers.settings")
    monkeypatch.chdir(BACKEND)
    import library_watch
    import main
    import scanner
    done = []
    monkeypatch.setattr(library_watch, "add", lambda path: done.append(("watch", path)))
    monkeypatch.setattr(library_watch, "forget", lambda path: done.append(("unwatch", path)))
    monkeypatch.setattr(scanner, "drop_look", lambda path: done.append(("unlook", path)))
    monkeypatch.setattr(main, "request_scan", lambda scopes, **kw: done.append((scopes, kw)))
    monkeypatch.setattr(settings_router, "notify_library_subscribers", lambda: done.append("wake"))
    page = type("R", (), {"state": type("S", (), {"signer": "device"})()})()
    jazz = str(tree.music / "Jazz")
    answer = settings_router.add_library_folder(settings_router.LibraryFolderAdd(path=jazz), page)
    assert answer == {"folder": {"id": answer["folder"]["id"], "path": jazz, "label": None, "name": jazz},
                      "merged": []}
    assert done == [("watch", jazz), ({jazz: True}, {"explicit": True}), "wake"]
    done.clear()
    settings_router.add_library_folder(settings_router.LibraryFolderAdd(path=str(tree.music)), page)
    assert done[:3] == [("unwatch", jazz), ("unlook", jazz), ("watch", str(tree.music))]
    for refused in (str(tree.outside), jazz):
        with pytest.raises(HTTPException) as no:
            settings_router.add_library_folder(settings_router.LibraryFolderAdd(path=refused), page)
        assert no.value.status_code == 400
    with pytest.raises(HTTPException) as no:
        settings_router.get_library_places(under=str(tree.outside))
    assert no.value.status_code == 400
    # On a launcher a folder is any the OS gives: only the launcher's own (host-signed) call adds one
    monkeypatch.setattr(settings, "sautium_library_roots", "")
    monkeypatch.setattr(library_root, "kind", lambda: "launcher")
    with pytest.raises(HTTPException) as no:
        settings_router.add_library_folder(settings_router.LibraryFolderAdd(path=str(tree.outside)), page)
    assert no.value.status_code == 403
    host = type("R", (), {"state": type("S", (), {"signer": "host"})()})()
    settings_router.add_library_folder(settings_router.LibraryFolderAdd(path=str(tree.outside)), host)
    assert str(tree.outside) in library_root.paths()
    with pytest.raises(HTTPException) as no:
        settings_router.get_library_places(under=None)
    assert no.value.status_code == 409
    monkeypatch.setattr(library_root, "kind", lambda: "setup")
    with pytest.raises(HTTPException) as no:
        settings_router.add_library_folder(settings_router.LibraryFolderAdd(path=str(tree.music)), host)
    assert no.value.status_code == 409


def test_the_table_is_in_the_baseline_and_its_delta_alike(db):
    baseline = (REPO / "desktop" / "migrations" / "001_initial.sql").read_text(encoding="utf-8")
    delta = (REPO / "desktop" / "migrations" / "079_library_folders.sql").read_text(encoding="utf-8")
    ddl = delta[delta.index("CREATE TABLE IF NOT EXISTS library_folders"):].strip()
    assert ddl in baseline
    db.execute(delta)                                         # again, on the schema that has it
    db.execute("""SELECT conname FROM pg_constraint
                  WHERE conrelid = 'library_folders'::regclass AND contype IN ('p', 'u')
                  ORDER BY conname""")
    assert [r[0] for r in db.fetchall()] == ["library_folders_path", "library_folders_pkey"]


# -- the one folder of before (db_migrate's library_folders_v1) -----------------------------------

@pytest.fixture
def before(db, scratch_dsn, monkeypatch):
    """A database as the release before left it: library_roots, its rows,
    no library_folders row, the step not yet run."""
    import psycopg2 as pg
    db_migrate = pytest.importorskip("db_migrate")
    db.execute("""CREATE TABLE IF NOT EXISTS library_roots (id SERIAL PRIMARY KEY, mount TEXT NOT NULL,
                  subpath TEXT NOT NULL DEFAULT '', chosen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  CONSTRAINT library_roots_place UNIQUE (mount, subpath))""")
    db.execute("TRUNCATE library_roots")
    for table in ("media_files", "album_variants", "albums"):
        db.execute(f"DELETE FROM {table}")

    def step():
        conn = pg.connect(scratch_dsn)
        try:
            return db_migrate._library_folders(conn)
        finally:
            conn.close()
    yield type("Before", (), {"step": staticmethod(step)})
    db.execute("DROP TABLE IF EXISTS library_roots")
    for table in ("album_variants", "albums"):
        db.execute(f"DELETE FROM {table}")


def _choice(db, mount, subpath, days_ago):
    db.execute("INSERT INTO library_roots (mount, subpath, chosen_at) VALUES (%s, %s, now() - %s * interval '1 day')",
               (mount, subpath, days_ago))


def test_every_choice_of_before_that_holds_copies_is_a_folder(tree, db, before):
    """Two sticks chosen in turn (F14: the first one's albums were away while
    it was in) are two folders; a choice inside the newest is part of it; a
    choice around the newest is the owner's narrowing — not read again, its
    other copies "Elsewhere"; one under a root no longer given is a folder,
    not connected."""
    import uuid
    disks = str(tree.disks)
    for path in (f"{disks}/1111-AAAA/Music/LP", f"{disks}/2222-BBBB/Audiobooks/Book",
                 "/mnt/sautium-3333-CCCC/Old/LP", f"{disks}/1111-AAAA/Podcasts/Ep"):
        album = str(uuid.uuid4())
        db.execute("INSERT INTO albums (id, title) VALUES (%s, 'LP')", (album,))
        db.execute("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, %s)", (album, path))
    _choice(db, disks, "1111-AAAA/Music", 1)                  # the newest: the second stick's
    _choice(db, disks, "1111-AAAA/Music/LP", 2)                # inside the newest: part of it
    _choice(db, disks, "2222-BBBB", 3)                         # the first stick: its copies
    _choice(db, disks, "2222-BBBB/Audiobooks/Empty", 4)        # inside a kept one
    _choice(db, disks, "1111-AAAA", 5)                         # around the newest: narrowed away
    _choice(db, "/mnt/sautium-3333-CCCC", "Old", 6)            # a root no longer given: not connected
    _choice(db, disks, "4444-DDDD", 7)                         # holds nothing: never read
    out = before.step()
    assert _rows(db) == [("/mnt/sautium-3333-CCCC/Old", None), (f"{disks}/2222-BBBB", None),
                         (f"{disks}/1111-AAAA/Music", None)]
    assert out["elsewhere"] == 1                               # the narrowed disk's other copy
    db.execute("SELECT to_regclass('library_roots')")
    assert db.fetchone()[0] is None


def test_a_launchers_folder_of_before_is_its_first_row(db, before, monkeypatch):
    monkeypatch.setattr(settings, "sautium_library_roots", "")
    monkeypatch.setattr(library_root, "kind", lambda: "launcher")
    monkeypatch.setattr(settings, "music_library_path", "E:\\Music\\")
    monkeypatch.setattr(settings, "music_host_path", None)
    out = before.step()
    assert _rows(db) == [("E:/Music", None)] and out["folders"] == ["E:/Music"]
    assert before.step()["folders"] == ["E:/Music"]           # again: the same row


def test_a_setup_nodes_folder_of_before_is_its_first_row(db, before, monkeypatch):
    monkeypatch.setattr(settings, "sautium_library_roots", "")
    monkeypatch.setattr(settings, "music_library_path", "/music")
    monkeypatch.setattr(settings, "music_host_path", "E:/Music")
    before.step()
    assert _rows(db) == [("E:/Music", None)]


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
    names, is judged here: inside a folder of the library, no `..`
    anywhere, no link out of the roots."""
    scanner = pytest.importorskip("scanner")
    music = str(tree.music)
    walker = scanner.LibraryScanner(music)
    assert scanner.scope_admitted(f"{music}/Jazz/Album", [music]) == music
    assert scanner.scope_admitted(f"{music}/Jazz", [music]) == music
    for outside in (f"{music}/../outside", f"{music}/Jazz/../../../outside", f"{music}/..",
                    f"{music}\\Jazz\\..\\..", f"{music}/Jazz/way-out", str(tree.outside), music + "-beside"):
        assert scanner.scope_admitted(outside, [music]) is None, outside
        audio, _cues, _unread = walker.find_audio_files(scopes=[(outside, True)])
        assert audio == [], outside
    assert "is not inside the library — not walked" in caplog.text
    assert [p.name for p in walker.find_audio_files(scopes=[(f"{music}/Jazz", True)])[0]] != []
    assert scanner.scope_admitted(f"{music}/Vol..2", [music]) == music      # two dots inside a name are a name
    # With no roots a folder link inside the library is a scope as before; `..` is one nowhere
    monkeypatch.setattr(settings, "sautium_library_roots", "")
    assert scanner.scope_admitted(f"{music}/Jazz/way-out", [music]) == music
    assert scanner.scope_admitted(f"{music}/Jazz/../../outside", [music]) is None


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
    library_root.add(str(tree.music))
    monkeypatch.setattr(hqp_benchmark, "measures_here", lambda: False)
    asked = []
    monkeypatch.setattr(main, "request_scan", lambda scopes, **kw: asked.append(scopes) or False)
    for outside in (f"{tree.music}/../outside", f"{tree.music}/Jazz/way-out", str(tree.outside)):
        with pytest.raises(HTTPException) as refused:
            asyncio.run(main.scan_start(scope=outside))
        assert refused.value.status_code == 400
    assert asked == []
    asyncio.run(main.scan_start(scope=f"{tree.music}/Jazz/Album/"))
    assert asked == [{f"{tree.music}/Jazz/Album": True}]
    asyncio.run(main.scan_start())
    assert asked[-1] == {"": True}


def test_a_node_with_no_roots_follows_a_link_as_before(tree, monkeypatch):
    scanner = pytest.importorskip("scanner")
    monkeypatch.setattr(settings, "sautium_library_roots", "")
    assert _found(scanner, tree.music) == ["01.flac", "02.FLAC", "03.flac", "04.flac"]


def test_a_scanner_with_no_folder_refuses_rather_than_walk_where_it_stands(tree):
    scanner = pytest.importorskip("scanner")
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


# -- a disk plugged in later (LIBRARY-FOLDERS-1 §3.2) -------------------------------------------

def test_a_disk_the_library_never_had_is_counted_once_and_offered_for_one_tap(tree, db, monkeypatch):
    from routers import settings as settings_router
    monkeypatch.setattr(library_root, "_counted", {})
    monkeypatch.setattr(settings_router, "_derived_since", {})
    disk = str(tree.disks / "4A21-0000")
    assert library_root.new_disks() == []                    # never counted on a page's read
    assert library_root.count_new_disks() is True             # the mount watch's own thread counts
    assert library_root.new_disks() == [{"path": disk, "uuid": "4A21-0000", "label": "Elements <b>",
                                         "size_bytes": library_root._size(disk), "audio_files": 1,
                                         "capped": False}]
    walked = []
    real = library_root._count_audio
    monkeypatch.setattr(library_root, "_count_audio", lambda f: walked.append(f) or real(f))
    assert library_root.count_new_disks() is False and walked == []     # once a mount
    notice = {n["key"]: n for n in settings_router._notice_items()}["disk.new.4A21-0000"]
    assert (notice["kind"], notice["data"]) == ("info", {"path": disk, "label": "Elements <b>",
                                                         "audio_files": 1, "capped": False})
    # a disk that holds a folder of the library comes back with no tap: not offered
    library_root.add(str(tree.disks / "4A21-0000" / "Rock"))
    assert library_root.new_disks() == []
    assert not any(k.startswith("disk.new.") for k in (n["key"] for n in settings_router._notice_items()))


def test_a_disk_with_no_music_is_not_offered_and_a_pulled_one_is_forgotten(tree, db, monkeypatch):
    monkeypatch.setattr(library_root, "_counted", {})
    (tree.disks / "4A21-0000" / "Rock" / "a.mp3").unlink()
    (tree.disks / "4A21-0000" / "Rock" / "notes.txt").write_text("x")
    library_root.count_new_disks()
    assert library_root.new_disks() == []                     # a stick of documents says nothing
    (tree.disks / "4A21-0000" / "Rock" / "b.flac").write_bytes(b"x")
    assert library_root.count_new_disks() is False            # counted already: this mount
    monkeypatch.setattr(library_root, "_mounts", lambda: {})  # pulled
    library_root.count_new_disks()
    assert library_root._counted == {}                        # nothing kept of a disk passed over (Q8)
    monkeypatch.setattr(library_root, "_mounts", lambda: {str(tree.disks / "4A21-0000"): True})
    assert library_root.count_new_disks() is True             # plugged in again: counted afresh
    assert library_root.new_disks()[0]["audio_files"] == 1


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
    places = library_root.places()["places"]
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
    place = library_root.places()["places"][-1]
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
    # the disks plugged in before the start are counted once, on a thread of their own
    assert [kw["name"] for kw in started] == ["new-disks", "mount-watch"]
    (tree.disks / ".unread").mkdir()
    started.clear()
    assert mount_watch.start() is True
    assert [kw["name"] for kw in started] == ["unread-watch", "new-disks", "mount-watch"]
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
    places = {p["path"]: p for p in library_root.places()["places"]}
    root = places[str(tree.music)]
    assert (root["kind"], root["name"], root["label"], root["read_only"]) == (
        "disk", str(tree.music), "My <Music>", True)
    assert root["size_bytes"] > 0
    # the automount's disk keeps the automount's label
    assert (places[disk]["label"], places[disk]["read_only"]) == ("Elements <b>", False)
    monkeypatch.setattr(settings, "sautium_library_root_labels", json.dumps({str(tree.music): "L" * 5000}))
    assert library_root.places()["places"][0]["label"] == "L" * library_root.LABEL_BYTES_MAX
    for broken in ("{not json", json.dumps(["a list"]), ""):
        monkeypatch.setattr(settings, "sautium_library_root_labels", broken)
        assert library_root.places()["places"][0]["label"] is None
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
    places = {p["path"]: p for p in library_root.places()["places"]}
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
        assert {p["path"]: p for p in library_root.places()["places"]}[str(gone)]["held"] is True
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
    assert library_root.places()["places"][-1]["kind"] == "disk"
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
                                f"its albums are not connected. Forget it on the machine: ./sautium roots --forget {gone}")]


# -- the move re-points its files (PI-NODE-2E §2) ---------------------------------------------------

OLD = "/mnt/sautium-B269-E281"


@pytest.fixture
def moved(tmp_path, db, monkeypatch):
    """A node whose disk the setup's line of before 2026-11-24 mounted at the
    root /mnt/sautium-B269-E281 — its files, a CUE image's two slices, its
    copy, a cover of each source form, the library's folder there — after
    the move block and the forget: the one root is the disks' folder."""
    import uuid
    disks = tmp_path / "media" / "sautium"
    (disks / ".names").mkdir(parents=True)
    for table in ("media_files", "album_variants", "albums", "tracks"):
        db.execute(f"DELETE FROM {table}")
    db.execute("DELETE FROM covers WHERE source_type <> 'sentinel'")
    album, track, image = (str(uuid.uuid4()) for _ in range(3))
    db.execute("INSERT INTO albums (id, title) VALUES (%s, 'LP')", (album,))
    db.execute("INSERT INTO tracks (id, title) VALUES (%s, 'One'), (%s, 'Two')", (track, image))
    db.execute("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, %s) RETURNING id",
               (album, f"{OLD}/Music/A/LP"))
    variant = db.fetchone()[0]
    db.execute("""INSERT INTO media_files (track_id, album_variant_id, file_path, cue_start_seconds, cue_end_seconds,
                                           stat_mtime_ns, stat_ctime_ns)
                  VALUES (%(t)s, %(v)s, %(p)s || '/Music/A/LP/01.flac', NULL, NULL, 1, 2),
                         (%(i)s, %(v)s, %(p)s || '/Music/A/LP/image.flac', 0, 180, 3, 4),
                         (%(i)s, %(v)s, %(p)s || '/Music/A/LP/image.flac', 180, NULL, 3, 4)""",
               {"t": track, "i": image, "v": variant, "p": OLD})
    db.execute("""INSERT INTO covers (id, content_hash, source_type, source_path, width, height, data, bytes)
                  VALUES (gen_random_uuid(), '\\x01', 'external', %(p)s || '/Music/A/LP/cover.jpg', 1, 1, '', 0),
                         (gen_random_uuid(), '\\x02', 'embedded', 'flac:' || %(p)s || '/Music/A/LP/01.flac#0',
                          1, 1, '', 0),
                         (gen_random_uuid(), '\\x03', 'embedded', 'mp3:/srv/other/a.mp3#0', 1, 1, '', 0)""",
               {"p": OLD})
    db.execute("INSERT INTO library_folders (path, added_at) VALUES (%s, now() - interval '1 day')",
               (f"{OLD}/Music",))
    monkeypatch.setattr(settings, "sautium_library_roots", str(disks))
    monkeypatch.setattr(settings, "music_library_path", "")
    monkeypatch.setattr(library_root, "_mounts", lambda: {})
    return type("Moved", (), {"disks": disks, "new": f"{disks}/B269-E281", "album": album, "variant": variant})


def _paths(db) -> dict:
    db.execute("SELECT file_path, cue_start_seconds FROM media_files ORDER BY file_path, cue_start_seconds")
    files = db.fetchall()
    db.execute("SELECT directory_path, absent_since FROM album_variants ORDER BY directory_path")
    variants = db.fetchall()
    db.execute("SELECT source_path FROM covers WHERE source_type <> 'sentinel' ORDER BY content_hash")
    covers = [r[0] for r in db.fetchall()]
    db.execute("SELECT path, added_at FROM library_folders ORDER BY path")
    return {"files": files, "variants": variants, "covers": covers, "roots": db.fetchall()}


def test_the_move_re_points_its_files_and_its_folder(moved, db, caplog):
    """The disk the old line mounted at /mnt/sautium-<id> is the disk the
    new line mounts at /media/sautium/<id>: the UUID both names carry. At
    the node's start every stored path moves with it, the library's folder
    too, nothing is away — and a second start finds nothing to do."""
    before = _paths(db)
    caplog.set_level("INFO")
    library_root.apply()
    after = _paths(db)
    new = moved.new
    assert after["files"] == [(f"{new}/Music/A/LP/01.flac", None), (f"{new}/Music/A/LP/image.flac", 0.0),
                              (f"{new}/Music/A/LP/image.flac", 180.0)]
    assert after["variants"] == [(f"{new}/Music/A/LP", None)]
    assert after["covers"] == [f"{new}/Music/A/LP/cover.jpg", f"flac:{new}/Music/A/LP/01.flac#0",
                               "mp3:/srv/other/a.mp3#0"]
    assert after["roots"] == [(f"{new}/Music", before["roots"][0][1])]
    assert library_root.paths() == [f"{new}/Music"]
    db.execute("SELECT stat_mtime_ns, stat_ctime_ns FROM media_files ORDER BY id")
    assert db.fetchall() == [(1, 2), (3, 4), (3, 4)]                 # the stat stays: nothing is read again
    assert f"music folder moved with its disk: 3 files from {OLD} to {new}" in caplog.text
    caplog.clear()
    library_root.apply()
    assert _paths(db) == after and "moved with its disk" not in caplog.text


def test_a_queued_slot_follows_its_file_to_the_new_place(moved, db):
    """The queue keeps a slot's path beside its media_files id (an output
    opens the path), and a queue restored at the same start would still
    name the old point: the re-bind reads the row's path again. A slot whose
    way in alone names a moved file has its way in re-read."""
    from playback.queue import QueueItem, file_source, rebind_origins
    db.execute("SELECT id, track_id::text, file_path, file_format::text AS file_format, cue_start_seconds,"
               " cue_end_seconds FROM media_files ORDER BY file_path, cue_start_seconds")
    cols = [c.name for c in db.description]
    one, image = (dict(zip(cols, r)) for r in db.fetchall()[:2])
    slots = [QueueItem(track_id=one["track_id"], media_file_id=one["id"], title="One",
                       source=file_source(one)),
             QueueItem(track_id=image["track_id"], media_file_id=None, title="Two",
                       source={"kind": "track"}, play={**file_source(image), "media_file_id": image["id"]})]
    assert rebind_origins(slots) == [None, None]
    library_root.apply()
    first, second = rebind_origins(slots)
    assert first["source"]["path"] == f"{moved.new}/Music/A/LP/01.flac" and "media_file_id" not in first
    assert second == {}                                   # its way in is re-read
    assert rebind_origins([QueueItem(**{**slots[0].__dict__, **first})]) == [None]


def test_a_copy_whose_new_place_is_taken_stays_as_it_was(moved, db):
    """An install a scan read at the new place before this release (Q5): the
    old copy stays where it was, away, for its Away row; a copy moves whole
    or not at all. The chosen folder moves all the same."""
    db.execute("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, %s) RETURNING id",
               (moved.album, f"{moved.new}/Music/A/LP"))
    there = db.fetchone()[0]
    db.execute("""INSERT INTO media_files (track_id, album_variant_id, file_path)
                  SELECT track_id, %s, %s FROM media_files WHERE cue_start_seconds IS NULL""",
               (there, f"{moved.new}/Music/A/LP/01.flac"))
    db.execute("UPDATE album_variants SET absent_since = now() WHERE id = %s", (moved.variant,))
    before = _paths(db)
    library_root.apply()
    after = _paths(db)
    assert after["files"] == before["files"] and after["variants"] == before["variants"]
    assert after["roots"][0][0] == f"{moved.new}/Music"
    # … and the copy's place alone taken (its files' are free): it stays whole too
    db.execute("DELETE FROM media_files WHERE album_variant_id = %s", (there,))
    db.execute("INSERT INTO library_folders (path) VALUES (%s)", (f"{OLD}/Music",))
    library_root.apply()
    assert _paths(db)["files"] == [f for f in before["files"] if f[0].startswith(OLD + "/")]


def test_nothing_is_re_pointed_while_the_old_point_is_still_a_root(moved, db):
    before = _paths(db)
    for given in (f"{moved.disks}:{OLD}", OLD):
        settings.sautium_library_roots = given
        library_root.apply()
        assert _paths(db) == before


def test_nothing_is_re_pointed_where_no_root_is_the_disks_folder(moved, db, tmp_path):
    (moved.disks / ".names").rmdir()
    before = _paths(db)
    library_root.apply()
    assert _paths(db) == before
    # … nor a folder that only looks like the old point
    (moved.disks / ".names").mkdir()
    db.execute("UPDATE library_folders SET path = '/mnt/other-B269-E281/Music'")
    library_root.apply()
    db.execute("SELECT count(*) FROM media_files WHERE file_path ^@ %s", (OLD + "/",))
    assert db.fetchone()[0] == 3


def test_a_folder_at_both_points_is_one_row(moved, db):
    db.execute("INSERT INTO library_folders (path, added_at) VALUES (%s, now() - interval '2 days'), "
               "(%s, now() - interval '3 days')", (f"{moved.new}/Music", "/srv/music"))
    library_root.apply()
    db.execute("SELECT path FROM library_folders ORDER BY added_at")
    assert db.fetchall() == [("/srv/music",), (f"{moved.new}/Music",)]


@pytest.fixture
def scanning(db, scratch_dsn, monkeypatch):
    """The scanner on this module's scratch database, its process state as at
    a start."""
    import subprocess
    if subprocess.run(["which", "ffmpeg"], capture_output=True).returncode:
        pytest.skip("no ffmpeg")
    database = pytest.importorskip("database")
    scanner = pytest.importorskip("scanner")
    from sqlalchemy.orm import sessionmaker
    engine = database.make_engine(scratch_dsn)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=engine))
    monkeypatch.setattr(scanner, "_looks", {})
    for table in ("listening_history", "media_files", "album_variants", "albums", "tracks"):
        db.execute(f"DELETE FROM {table}")
    yield scanner
    engine.dispose()


def test_the_scan_after_the_move_finds_every_file_where_it_was_left(scanning, db, tmp_path, monkeypatch, caplog):
    """The disk read at the old point, then mounted at the new one (the same
    files, the same stat): the start re-points, and its scan reads nothing
    again and sets no copy away."""
    import subprocess
    old = tmp_path / "mnt" / "sautium-B269-E281"
    disks = tmp_path / "media" / "sautium"
    (disks / ".names").mkdir(parents=True)
    for n in (1, 2):
        path = old / "Music" / "Aurora" / "LP" / f"0{n}.flac"
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                        "-ar", "44100", "-sample_fmt", "s16", "-metadata", f"title=Song {n}",
                        "-metadata", "artist=Aurora", "-metadata", "album=LP", "-metadata", f"track={n}",
                        str(path)], check=True)
    monkeypatch.setattr(library_root, "OLD_BOOT_MOUNT_POINT",
                        re.compile(re.escape(str(tmp_path / "mnt")) + r"/sautium-([0-9A-Za-z-]{4,40})"))
    monkeypatch.setattr(settings, "sautium_library_roots", f"{old}:{disks}")
    monkeypatch.setattr(settings, "music_library_path", "")
    monkeypatch.setattr(library_root, "_mounts", lambda: {})
    library_root.add(str(old / "Music"))
    library_root.apply()
    assert scanning.LibraryScanner().scan_and_import()["added"] == 2
    # the move block and the forget: the disk at the new point, the old root gone
    os.rename(old, disks / "B269-E281")
    monkeypatch.setattr(settings, "sautium_library_roots", str(disks))
    library_root.apply()
    assert library_root.paths() == [str(disks / "B269-E281" / "Music")]
    caplog.set_level("INFO")
    found = scanning.LibraryScanner()
    stats = found.scan_and_import()
    assert (stats["added"], stats["reread"], stats["skipped"]) == (0, 0, 2)
    assert "No new or changed files to process" in caplog.text
    assert scanning.set_aside_missing(found.last_scopes, found.last_disk_paths, found.last_unread,
                                      found.last_gone) == 0
    db.execute("SELECT count(*), count(*) FILTER (WHERE absent_since IS NOT NULL) FROM album_variants")
    assert db.fetchone() == (1, 0)
