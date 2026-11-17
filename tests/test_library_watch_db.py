"""New music appears by itself (LIBRARY-WATCH-DESIGN §1–§5, §10): the
folder watcher (backend/library_watch.py) over inotify, on real files in a
temporary music folder, feeding the one scan queue (main.request_scan) and
its worker, on the module's scratch database.

What a mount delivers is the mount's kind, read from /proc/self/mountinfo;
the suite's temporary folder sits on the container's own overlay, so the
tests that need events say the folder is a local disk."""

import shutil
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

pytest.importorskip("psycopg2")
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="inotify")

import library_watch  # noqa: E402
import scanner  # noqa: E402
from tests.test_library_reread_db import _clean, _flac, _rows, template  # noqa: E402,F401
from tests.test_scan_unreachable import _wait_until, cur, library, main_module  # noqa: E402,F401

_MOUNTS = """\
22 1 8:2 / / rw,relatime shared:1 - ext4 /dev/sda2 rw
45 22 8:17 / /srv/music rw,relatime shared:30 - ext4 /dev/sdb1 rw
46 45 0:50 / /srv/music/nas rw,relatime shared:31 - cifs //nas/music rw,vers=3.0
61 22 0:60 /D:\\134 /music rw,noatime - 9p D:\\134 rw,aname=drvfs;path=D:\\
"""


def test_the_mount_says_whether_events_come():
    assert library_watch.linux_kind("/srv/music/Rock", _MOUNTS) is None
    assert library_watch.linux_kind("/srv/music", _MOUNTS) == "cifs"     # a share mounted inside
    assert library_watch.linux_kind("/music", _MOUNTS) == "9p"           # Docker Desktop's bind
    assert library_watch.linux_kind("/home/x", _MOUNTS) is None          # the root's ext4


def test_a_bind_of_docker_desktop_is_not_watched(library, monkeypatch, cur):
    asked = []
    monkeypatch.setattr(library_watch, "mount_kind", lambda root: "9p")
    library_watch.start(lambda scopes, **kw: asked.append(scopes))
    try:
        assert library_watch.state() == {"state": library_watch.NO_EVENTS, "why": "9p"}
        assert asked == []
    finally:
        library_watch.stop()


@pytest.fixture
def watching(library, monkeypatch, cur, main_module):
    """The watcher armed on the music folder (a local disk, as the mount
    would say on a Pi), feeding the real queue; every read the scanner makes
    counted by path."""
    from canon import post_import
    monkeypatch.setattr(post_import, "run", lambda *a, **kw: None)
    monkeypatch.setattr(library_watch, "mount_kind", lambda root: None)
    reads = []
    real = scanner.LibraryScanner.extract_metadata

    def counted(path):
        reads.append(str(path))
        return real(path)
    monkeypatch.setattr(scanner.LibraryScanner, "extract_metadata", staticmethod(counted))
    library_watch.start(main_module.request_scan)
    assert library_watch.state()["state"] == library_watch.WATCHING
    yield reads
    library_watch.stop()
    _wait_until(lambda: not main_module._scan_state["running"], timeout=60)


def _settled(main_module, check, timeout=60):
    return _wait_until(lambda: check() and not main_module._scan_state["running"]
                       and main_module._scan_pending is None, timeout=timeout)


def test_a_file_that_appears_is_imported_with_no_request(cur, library, template, watching,
                                                         main_module):
    _clean(cur)
    _flac(template, library / "A" / "LP" / "01.flac", "T1", "Aurora", "LP")
    assert _settled(main_module, lambda: len(_rows(cur)) == 1)
    assert [r[2] for r in _rows(cur)] == ["T1"]


def test_a_folder_removed_is_away_not_gone(cur, library, template, watching, main_module):
    _clean(cur)
    for i in (1, 2):
        _flac(template, library / "A" / "LP" / f"{i:02d}.flac", f"T{i}", "Aurora", "LP")
    assert _settled(main_module, lambda: len(_rows(cur)) == 2)
    shutil.rmtree(library / "A" / "LP")

    def away():
        cur.execute("SELECT count(*) FROM album_variants WHERE absent_since IS NOT NULL")
        return cur.fetchone()[0] == 1
    assert _settled(main_module, away)
    assert len(_rows(cur)) == 2                   # nothing removed


def _sources(template, tmp_path):
    """Ten tagged files to copy from, a title each."""
    out = []
    for t in range(10):
        out.append(_flac(template, tmp_path / "src" / f"{t:02d}.flac", f"T{t}", "Aurora", "Storm",
                         track=t + 1).read_bytes())
    return out


def _copy_in(sources, folder: Path, paths):
    for t, data in enumerate(sources):
        p = folder / f"{t:02d}.flac"
        with open(p, "wb") as dst:
            dst.write(data[: len(data) // 2])      # written in two goes:
            dst.flush()
            dst.write(data[len(data) // 2:])       # a scan between them leaves it
        paths.append(str(p))


def test_a_storm_of_a_thousand_files_reads_each_once(cur, library, template, watching,
                                                     main_module, tmp_path):
    """1 000 files copied into 100 folders the watch holds: each is read
    once — at its close; a scan that meets it still open leaves it."""
    _clean(cur)
    sources = _sources(template, tmp_path)
    folders = [library / "Storm" / f"Album {d:03d}" for d in range(100)]
    for f in folders:
        f.mkdir(parents=True)
    assert _settled(main_module, lambda: True)
    paths = []
    for f in folders:
        _copy_in(sources, f, paths)
    assert _settled(main_module, lambda: len(_rows(cur)) == 1000, timeout=300)
    assert sorted(watching) == sorted(paths)


def test_folders_copied_in_whole_are_imported_whole(cur, library, template, watching,
                                                    main_module, tmp_path):
    """A folder written before its watch exists is walked when the watch is
    placed; a file it meets half-written is read again at its close — read
    at most twice, never missed."""
    _clean(cur)
    sources = _sources(template, tmp_path)
    paths = []
    for d in range(20):
        folder = library / "Copied" / f"Album {d:03d}"
        folder.mkdir(parents=True)
        _copy_in(sources, folder, paths)
    assert _settled(main_module, lambda: len(_rows(cur)) == 200, timeout=300)
    counts = {p: watching.count(p) for p in paths}
    assert set(counts.values()) <= {1, 2}


def test_an_overflow_scans_the_whole_library(library, monkeypatch, cur):
    asked = []

    def request(scopes, **kw):
        asked.append(scopes)
    monkeypatch.setattr(library_watch, "mount_kind", lambda root: None)
    library_watch.start(request)
    try:
        asked.clear()                                   # the start's own walk
        library_watch._source._event(-1, library_watch._Inotify.IN_Q_OVERFLOW, "")
        assert asked == [{"": True}]
    finally:
        library_watch.stop()
