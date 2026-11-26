"""A library of several folders (LIBRARY-FOLDERS-1): two disks in the
automount's folder, each a folder of the library, real FLAC files scanned
through the one queue on the module's scratch database.

- One pulled: its albums are not connected (album_away 'disk'), the other's
  play; the queue hears each change once; back, nothing is lost.
- A Rescan with one out judges the folder that answers alone: a file
  deleted there is missing, nothing of the folder out is away (F14's case:
  a second disk's albums are never away while it is in).
- "Forget this disk and its albums", out or in: the plan's counts are what
  the yes removes; the listens and a heard track stay; an album held by a
  copy in the other folder stays; the row goes with the files.
- One album in two folders is one album with two copies.
"""

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

pytest.importorskip("psycopg2")
settings_router = pytest.importorskip("routers.settings")

import library_root  # noqa: E402
import scanner  # noqa: E402
from config import settings  # noqa: E402
from tests.test_library_reread_db import _flac, template  # noqa: E402,F401
from tests.test_scan_unreachable import (_answer, _post_import, _wait_until, cur, heard,  # noqa: E402,F401
                                         main_module)


@pytest.fixture
def disks(tmp_path, monkeypatch, cur, template, main_module):
    """The automount's folder with two disks plugged in — A holding "First
    Light" by Aurora, B holding "Night Walks" by Hidden Orchestra and a copy
    of "First Light" — each disk's Music a folder of the library, scanned.
    `pull(name)` takes a disk out (its mount point goes with it),
    `plug(name)` puts it back."""
    from canon import post_import
    root = tmp_path / "media" / "sautium"
    (root / ".names").mkdir(parents=True)
    (root / ".names" / "AAAA-1111").write_text("STICK A\n")
    (root / ".names" / "BBBB-2222").write_text("STICK B\n")
    a, b = root / "AAAA-1111", root / "BBBB-2222"
    _flac(template, a / "Music" / "Aurora" / "First Light" / "01.flac", "Dawn", "Aurora", "First Light", track=1)
    _flac(template, a / "Music" / "Aurora" / "First Light" / "02.flac", "Noon", "Aurora", "First Light", track=2)
    _flac(template, b / "Music" / "HO" / "Night Walks" / "01.flac", "Dusk", "Hidden Orchestra", "Night Walks",
          track=1)
    _flac(template, b / "Music" / "Aurora" / "First Light" / "01.flac", "Dawn", "Aurora", "First Light", track=1)
    mounted = {str(a): True, str(b): True}
    monkeypatch.setattr(settings, "sautium_library_roots", str(root))
    monkeypatch.setattr(settings, "music_library_path", "")
    monkeypatch.setattr(settings, "music_host_path", None)
    monkeypatch.setattr(library_root, "_mounts", lambda: dict(mounted))
    monkeypatch.setattr(post_import, "run", _post_import)
    folders = {name: library_root.add(str(root / f"{uuid}/Music"))["folder"]
               for name, uuid in (("A", "AAAA-1111"), ("B", "BBBB-2222"))}
    main_module.request_scan()
    main_module._scan_thread.join()
    away = tmp_path / "pulled"
    away.mkdir()

    def pull(name):
        disk = (a if name == "A" else b)
        mounted.pop(str(disk))
        shutil.move(str(disk), str(away / disk.name))
        scanner.look_all()                         # the mount table's wake

    def plug(name):
        disk = (a if name == "A" else b)
        shutil.move(str(away / disk.name), str(disk))
        mounted[str(disk)] = True
        scanner.look_all()
    return type("Disks", (), {"root": root, "folders": folders, "pull": staticmethod(pull),
                              "plug": staticmethod(plug), "a": a, "b": b})


def _album(cur, title):
    cur.execute("SELECT id::text FROM albums WHERE title = %s", (title,))
    return cur.fetchone()[0]


def _away_of(cur, album):
    from sql_queries import album_away
    from db_pool import db_query_one
    return db_query_one(f"SELECT {album_away('%(a)s::uuid')} AS why", {"a": album, "gone": scanner.gone_prefixes()})["why"]


def _variants(cur, title):
    cur.execute("""SELECT av.directory_path, av.absent_since IS NOT NULL FROM album_variants av
                   JOIN albums al ON al.id = av.album_id WHERE al.title = %s ORDER BY 1""", (title,))
    return cur.fetchall()


def test_one_album_in_two_folders_is_one_album_with_two_copies(cur, disks):
    assert [Path(p).parts[-4] for p, _away in _variants(cur, "First Light")] == ["AAAA-1111", "BBBB-2222"]
    cur.execute("SELECT count(*) FROM albums WHERE title = 'First Light'")
    assert cur.fetchone()[0] == 1


def test_a_disk_pulled_is_not_connected_and_the_other_plays(cur, disks, monkeypatch):
    import playback.manager as manager_mod
    changes = []
    monkeypatch.setattr(manager_mod, "reach_changed", lambda: changes.append(scanner.gone_prefixes()))
    b_music = disks.folders["B"]["path"]
    disks.pull("B")
    assert scanner.gone_prefixes() == [b_music + "/"]
    assert _wait_until(lambda: changes == [[b_music + "/"]])      # the queue hears it once
    assert _away_of(cur, _album(cur, "Night Walks")) == "disk"     # not connected, never away
    assert _away_of(cur, _album(cur, "First Light")) is None       # its copy on A opens
    lib = asyncio.run(settings_router._library_state())
    state = {f["name"]: (f["connected"], f["albums"], f["tracks"]) for f in lib["folders"]}
    assert state == {"STICK A · Music": (True, 1, 2), "STICK B · Music": (False, 2, 2)}
    assert lib["away"] == {"albums": 0, "copies": 0, "waiting": 0}
    scanner.look_all()
    assert len(changes) == 1                                       # looked again, nothing changed
    disks.plug("B")
    assert scanner.gone_prefixes() == []
    assert _wait_until(lambda: len(changes) == 2)
    assert _away_of(cur, _album(cur, "Night Walks")) is None


def test_a_rescan_with_a_disk_out_judges_the_folder_that_answers_alone(cur, disks, main_module, monkeypatch):
    (disks.a / "Music" / "Aurora" / "First Light" / "02.flac").unlink()
    disks.pull("B")
    main_module.request_scan(prune=True)
    main_module._scan_thread.join()
    state = main_module._scan_state
    assert state["progress"].startswith("Scan complete")
    assert "STICK B · Music was not scanned: it is not connected" in state["progress"]
    plan = state["prune_plan"]
    assert (plan["counts"]["files"], plan["counts"]["away"]) == (1, 0)
    assert plan["folders"] == [disks.folders["A"]["path"]]
    assert not any(away for _p, away in _variants(cur, "Night Walks") + _variants(cur, "First Light"))
    # "Scan for new" with B out sets nothing of B aside either
    main_module.request_scan()
    main_module._scan_thread.join()
    assert not any(away for _p, away in _variants(cur, "Night Walks"))


def _listen(cur, title):
    cur.execute("""INSERT INTO listening_history (track_id, started_at, duration_listened, completed, skipped)
                   SELECT id, now(), 200, TRUE, FALSE FROM tracks WHERE title = %s""", (title,))


def test_a_disk_out_is_forgotten_with_what_it_alone_held(cur, disks, main_module):
    _listen(cur, "Dusk")
    disks.pull("B")
    asyncio.run(main_module.scan_forget(disks.folders["B"]["id"]))
    assert _wait_until(lambda: not main_module._scan_state["running"])
    asked = main_module._prune_plan_counts(main_module._scan_state["prune_plan"])
    assert (asked["kind"], asked["name"], asked["connected"], asked["disk"], asked["elsewhere"]) == (
        "folder", "STICK B · Music", False, True, False)
    # B's two files; "Night Walks" only it held; "First Light" stays, held by A; "Dusk" was heard
    assert (asked["files"], asked["copies"], asked["albums"], asked["tracks"]) == (1, 1, 1, 0)
    _answer(main_module, asked["id"], True)
    assert main_module._scan_state["result"]["prune"]["pruned"] == 2
    assert library_root.paths() == [disks.folders["A"]["path"]]
    cur.execute("SELECT count(*) FROM albums WHERE title = 'Night Walks'")
    assert cur.fetchone()[0] == 0
    assert [Path(p).parts[-4] for p, _a in _variants(cur, "First Light")] == ["AAAA-1111"]
    cur.execute("SELECT count(*) FROM tracks WHERE title = 'Dusk'")
    assert cur.fetchone()[0] == 1                                  # a heard track stays, to stream
    cur.execute("SELECT count(*) FROM listening_history")
    assert cur.fetchone()[0] == 1
    assert scanner.gone_prefixes() == []                           # its look went with it
    # plugged in again, it is a disk the library never had: offered, not joined
    disks.plug("B")
    library_root.count_new_disks()
    assert [d["uuid"] for d in library_root.new_disks()] == ["BBBB-2222"]


def test_a_folder_that_answers_is_forgotten_and_its_files_stay(cur, disks, main_module, monkeypatch):
    monkeypatch.setattr(library_root, "_counted", {})
    asyncio.run(main_module.scan_forget(disks.folders["A"]["id"]))
    assert _wait_until(lambda: not main_module._scan_state["running"])
    asked = main_module._prune_plan_counts(main_module._scan_state["prune_plan"])
    # A's copy of "First Light" goes, the album stays with B's; "Noon" only A held, unheard
    assert (asked["connected"], asked["files"], asked["copies"], asked["tracks"]) == (True, 0, 1, 1)
    _answer(main_module, asked["id"], True)
    assert library_root.paths() == [disks.folders["B"]["path"]]
    assert (disks.a / "Music" / "Aurora" / "First Light" / "02.flac").exists()   # nothing on the disk
    # still plugged in, the disk is one the library never had: offered at once, with no plug
    # to wake a count (the box, 2026-11-26)
    assert _wait_until(lambda: [d["uuid"] for d in library_root.new_disks()] == ["AAAA-1111"])
    assert [Path(p).parts[-4] for p, _a in _variants(cur, "First Light")] == ["BBBB-2222"]


def test_a_no_to_the_forget_keeps_everything_and_a_scan_meanwhile_is_refused(cur, disks, main_module):
    from fastapi import HTTPException
    asyncio.run(main_module.scan_forget(disks.folders["B"]["id"]))
    assert _wait_until(lambda: not main_module._scan_state["running"])
    plan = main_module._scan_state["prune_plan"]
    with pytest.raises(HTTPException) as busy:
        asyncio.run(main_module.scan_forget(disks.folders["A"]["id"]))
    assert busy.value.status_code == 409                           # one question at a time
    _answer(main_module, plan["id"], False)
    assert library_root.paths() == [disks.folders["A"]["path"], disks.folders["B"]["path"]]
    cur.execute("SELECT count(*) FROM media_files")
    assert cur.fetchone()[0] == 4
    with pytest.raises(HTTPException) as no:
        asyncio.run(main_module.scan_forget(10 ** 6))
    assert no.value.status_code == 404


def test_the_notice_of_a_disk_out_is_a_warning_and_leaves_when_it_is_back(cur, disks, monkeypatch):
    monkeypatch.setattr(settings_router, "_derived_since", {})
    key = f"library.folder_out.{disks.folders['B']['id']}"
    disks.pull("B")
    items = {n["key"]: n for n in settings_router._notice_items()}
    assert items[key]["kind"] == "warning"
    assert items[key]["data"]["name"] == "STICK B · Music" and items[key]["data"]["albums"] == 2
    assert not any(k.startswith("library.folder_out.") and k != key for k in items)
    disks.plug("B")
    assert not any(k.startswith("library.folder_out.") for k in (n["key"] for n in settings_router._notice_items()))


def test_every_tile_of_an_album_out_says_so(cur, disks, monkeypatch):
    """B-152: the album tiles beyond Home's shelves — the shared album
    hydrator (Similar albums, the assistant's blocks), Discovery's shuffle
    mosaic, the artist page, a release group's editions, a Listening
    history card of one album — carry the mark by the one definition
    (sql_queries.album_away): 'disk' while its folder is out, none when it
    is back or a copy elsewhere opens."""
    from entity_hydration import hydrate_albums
    from routers import artists, discovery, home, release_groups
    import routers.artists
    monkeypatch.setattr(routers.artists, "_is_discography_stale", lambda last: False, raising=False)
    night, first = _album(cur, "Night Walks"), _album(cur, "First Light")
    cur.execute("""INSERT INTO listening_sessions (origin, title, track_count, ended_at)
                   VALUES ('album', 'Night Walks', 1, now()) RETURNING id""")
    session = cur.fetchone()[0]
    cur.execute("""INSERT INTO session_tracks (session_id, position, track_id, album_id)
                   SELECT %s, 1, t.id, %s FROM tracks t WHERE t.title = 'Dusk'""", (session, night))
    cur.execute("SELECT ta.artist_id::text FROM track_artists ta JOIN tracks t ON t.id = ta.track_id "
                "WHERE t.title = 'Dusk'")
    artist = cur.fetchone()[0]

    def marks():
        tiles = {t["album_id"]: t["not_here"] for t in hydrate_albums([night, first])}
        shuffled = {a["id"]: a["not_here"] for a in discovery.shuffle_albums(limit=10, seed=1, offset=0)["albums"]}
        page = {a["id"]: a.get("not_here") for a in artists.get_artist(artist, sort="release_year", mbid=None)["albums"]}
        editions = {e["album_id"]: e["not_here"] for e in release_groups.get_release_group(night)["editions"]}
        cards = {c["album_id"]: c["not_here"] for c in home.get_listening_history(limit=5)["sessions"]}
        return tiles, shuffled, page, editions, cards

    for got in marks():
        assert got.get(night) is None and got.get(first) is None
    disks.pull("B")
    tiles, shuffled, page, editions, cards = marks()
    assert tiles == {night: "disk", first: None}                  # First Light opens from A
    assert shuffled[night] == "disk" and shuffled[first] is None
    assert page[night] == "disk" and editions[night] == "disk" and cards[night] == "disk"
    disks.plug("B")
    assert all(got.get(night) is None for got in marks())
