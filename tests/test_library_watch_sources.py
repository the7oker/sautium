"""The folder watcher's event source on the platform it runs on — inotify,
ReadDirectoryChangesW, FSEvents (backend/library_watch.py) — on real files
in a temporary folder, with no database: a file written makes its folder
dirty, a folder copied in is walked whole, a folder removed is judged whole.
On Windows a file a copier still holds is not read: the scan's deny-write
probe (file_stat.writer_holds) says so at every notification but the one
after the close, which is the copy's whole (M3).

Runs on Linux in the suite; on Windows and macOS by hand (LIBRARY-WATCH-
PROPOSAL, the platform checks)."""

import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import file_stat  # noqa: E402
import library_watch  # noqa: E402


class _Heard:
    def __init__(self):
        self.events, self._cond = [], threading.Condition()

    def __call__(self, path, recursive):
        with self._cond:
            self.events.append((os.path.normcase(os.path.realpath(path)), recursive))
            self._cond.notify_all()

    def wait_for(self, pred, timeout=10.0):
        with self._cond:
            return self._cond.wait_for(lambda: pred(self.events), timeout)


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr(library_watch, "_WATCHED_SUFFIXES", {".flac", ".cue"})
    root = tmp_path / "music"
    root.mkdir()
    heard, lost = _Heard(), []
    src = library_watch._SOURCE(str(root), heard, lambda *a: lost.append(a))
    thread = threading.Thread(target=src.run, daemon=True)
    thread.start()
    # The source's own start, by an event: a write it reports is a write
    # after its first read began (ReadDirectoryChangesW reports nothing
    # from before; the product's start walk covers that moment)
    warm = root / "warm.flac"
    deadline = time.monotonic() + 10
    while not heard.wait_for(lambda ev: (_key(root), False) in ev, timeout=0.2):
        assert time.monotonic() < deadline, "the source reported no write"
        warm.write_bytes(b"fLaC")
    warm.unlink()
    heard.wait_for(lambda ev: False, timeout=0.3)    # its own removal's events
    heard.events.clear()
    yield root, heard, src, lost
    src.stop()
    thread.join(5)
    src.close()


def _key(path):
    return os.path.normcase(os.path.realpath(path))


def test_a_file_written_makes_its_folder_dirty(source):
    root, heard, _src, lost = source
    album = root / "A" / "LP"
    album.mkdir(parents=True)
    assert heard.wait_for(lambda ev: (_key(album), True) in ev or (_key(root / "A"), True) in ev)
    (album / "01.flac").write_bytes(b"fLaC" + b"\0" * 1000)
    assert heard.wait_for(lambda ev: (_key(album), False) in ev)
    assert lost == []


def test_a_folder_moved_in_is_walked_and_one_removed_is_judged_whole(source, tmp_path):
    root, heard, _src, _lost = source
    outside = tmp_path / "outside" / "Album X"
    outside.mkdir(parents=True)
    (outside / "01.flac").write_bytes(b"fLaC")
    shutil.move(str(outside), str(root / "Album X"))
    assert heard.wait_for(lambda ev: (_key(root / "Album X"), True) in ev)
    shutil.rmtree(root / "Album X")
    assert heard.wait_for(lambda ev: ev.count((_key(root / "Album X"), True)) >= 2
                          or any(p == _key(root / "Album X") and r for p, r in ev[-3:]))


def test_other_files_raise_nothing(source):
    root, heard, _src, _lost = source
    (root / "cover.jpg").write_bytes(b"\xff\xd8")
    (root / "01.flac").write_bytes(b"fLaC")
    assert heard.wait_for(lambda ev: (_key(root), False) in ev)
    assert all(not p.endswith("cover.jpg") for p, _r in heard.events)


@pytest.mark.skipif(sys.platform != "win32", reason="CopyFileEx and share modes are Windows'")
def test_a_copy_is_read_only_after_its_writer_lets_go(source, tmp_path):
    import ctypes
    import ctypes.wintypes as wt
    root, heard, _src, _lost = source
    src = tmp_path / "big.flac"
    with open(src, "wb") as f:
        for _ in range(64):
            f.write(os.urandom(2 ** 20))
    want = src.stat()
    dst = root / "LP" / "big.flac"
    dst.parent.mkdir()
    held_at = []

    def probe(path, recursive):
        heard(path, recursive)
        if _key(path) == _key(dst.parent) and dst.exists():
            held_at.append(file_stat.writer_holds(str(dst)))
    _src.on_dirty = probe
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CopyFileExW.argtypes = [wt.LPCWSTR, wt.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.c_void_p, wt.DWORD]
    assert k32.CopyFileExW(str(src), str(dst), None, None, None, 0)
    assert heard.wait_for(lambda ev: held_at and held_at[-1] is False)
    assert held_at[-1] is False                       # the last notification: the copy whole
    assert file_stat.stat_of(str(dst)).mtime_ns == want.st_mtime_ns


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS's ChangeTime")
def test_a_tag_edit_that_keeps_the_mtime_moves_the_change_time(tmp_path):
    f = tmp_path / "a.flac"
    f.write_bytes(b"fLaC" + b"\0" * 4096)
    before = file_stat.stat_of(str(f))
    time.sleep(0.05)
    with open(f, "r+b") as h:
        h.seek(100)
        h.write(b"TITLE=x")
    os.utime(f, ns=(before.mtime_ns, before.mtime_ns))
    after = file_stat.stat_of(str(f))
    assert (after.size, after.mtime_ns) == (before.size, before.mtime_ns)
    assert after.ctime_ns != before.ctime_ns
    assert file_stat.birth_ns(str(f)) is not None


@pytest.mark.skipif(sys.platform == "linux", reason="the suite's folder is the container's overlay")
def test_a_local_folder_s_mount_delivers_events(tmp_path):
    assert library_watch.mount_kind(str(tmp_path)) is None
