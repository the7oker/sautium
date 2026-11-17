"""A file's stat as the library compares it: size, modification time and
change time in ns, and its birth on this disk when asked.

One definition, so what a scan records and what the analysis passes compare
are the same numbers on every platform:

- the change time is the one no tool sets back — POSIX `st_ctime`, NTFS's
  ChangeTime — so a tag editor that restores the modification time is still
  seen (LIBRARY-WATCH-DESIGN M3, M4). On Windows `os.stat`'s `st_ctime` is
  the creation time, so both come from GetFileInformationByHandleEx;
- the birth time is when the file was written to this disk: Windows'
  CreationTime, macOS's `st_birthtime`, Linux's `statx` btime (ext4, btrfs,
  xfs). A copy keeps its source's modification time; the birth is the copy's
  own. None where the file system keeps none.
"""

import ctypes
import os
import sys
from typing import NamedTuple, Optional


class FileStat(NamedTuple):
    size: int
    mtime_ns: int
    ctime_ns: int


def from_posix(st: os.stat_result) -> FileStat:
    return FileStat(st.st_size, st.st_mtime_ns, st.st_ctime_ns)


if sys.platform == "win32":
    import ctypes.wintypes as _wt

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.restype = _wt.HANDLE
    _k32.CreateFileW.argtypes = [_wt.LPCWSTR, _wt.DWORD, _wt.DWORD, ctypes.c_void_p,
                                 _wt.DWORD, _wt.DWORD, _wt.HANDLE]
    _k32.GetFileInformationByHandleEx.argtypes = [_wt.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                  _wt.DWORD]
    _k32.CloseHandle.argtypes = [_wt.HANDLE]
    _INVALID = ctypes.c_void_p(-1).value
    _FILE_READ_ATTRIBUTES = 0x0080
    _GENERIC_READ = 0x80000000
    _SHARE_ALL = 0x1 | 0x2 | 0x4
    _OPEN_EXISTING = 3
    _BACKUP_SEMANTICS = 0x02000000
    _EPOCH_AS_FILETIME = 116444736000000000
    _SHARING_VIOLATION = 32

    class _BasicInfo(ctypes.Structure):
        _fields_ = [("CreationTime", ctypes.c_longlong), ("LastAccessTime", ctypes.c_longlong),
                    ("LastWriteTime", ctypes.c_longlong), ("ChangeTime", ctypes.c_longlong),
                    ("FileAttributes", _wt.DWORD)]

    class _StandardInfo(ctypes.Structure):
        _fields_ = [("AllocationSize", ctypes.c_longlong), ("EndOfFile", ctypes.c_longlong),
                    ("NumberOfLinks", _wt.DWORD), ("DeletePending", ctypes.c_ubyte),
                    ("Directory", ctypes.c_ubyte)]

    def _ns(filetime: int) -> int:
        return (filetime - _EPOCH_AS_FILETIME) * 100

    def _info(path: str):
        h = _k32.CreateFileW(path, _FILE_READ_ATTRIBUTES, _SHARE_ALL, None, _OPEN_EXISTING,
                             _BACKUP_SEMANTICS, None)
        if h in (_INVALID, None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            basic, std = _BasicInfo(), _StandardInfo()
            if not (_k32.GetFileInformationByHandleEx(h, 0, ctypes.byref(basic), ctypes.sizeof(basic))
                    and _k32.GetFileInformationByHandleEx(h, 1, ctypes.byref(std),
                                                          ctypes.sizeof(std))):
                raise ctypes.WinError(ctypes.get_last_error())
            return basic, std
        finally:
            _k32.CloseHandle(h)

    def stat_of(path: str) -> FileStat:
        basic, std = _info(path)
        return FileStat(std.EndOfFile, _ns(basic.LastWriteTime), _ns(basic.ChangeTime))

    def birth_ns(path: str) -> Optional[int]:
        return _ns(_info(path)[0].CreationTime)

    def writer_holds(path: str) -> bool:
        """Whether a handle with write access holds the file: an open that
        denies writers fails with a sharing violation while one does. Every
        copier measured is notified again after its close (M3), so a file
        held now is read at that notification."""
        h = _k32.CreateFileW(path, _GENERIC_READ, 0x1, None, _OPEN_EXISTING, 0, None)
        if h in (_INVALID, None):
            return ctypes.get_last_error() == _SHARING_VIOLATION
        _k32.CloseHandle(h)
        return False

else:
    def stat_of(path: str) -> FileStat:
        return from_posix(os.stat(path))

    def writer_holds(path: str) -> bool:
        """POSIX has no share modes: a writer is known by its close event
        (library_watch) or not at all."""
        return False

    if sys.platform == "darwin":
        def birth_ns(path: str) -> Optional[int]:
            return round(os.stat(path).st_birthtime * 1e9)
    else:
        _STATX_BTIME = 0x800
        _AT_FDCWD = -100
        try:
            _statx = ctypes.CDLL(None, use_errno=True).statx
            _statx.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint,
                               ctypes.c_void_p]
        except AttributeError:       # a libc without statx (glibc < 2.28, musl < 1.2.5)
            _statx = None

        def birth_ns(path: str) -> Optional[int]:
            """statx's btime, when the file system keeps one (`stx_mask`)."""
            if _statx is None:
                return None
            buf = ctypes.create_string_buffer(256)
            if _statx(_AT_FDCWD, os.fsencode(path), 0, _STATX_BTIME, buf) != 0:
                err = ctypes.get_errno()
                raise OSError(err, os.strerror(err), path)
            raw = buf.raw
            if not int.from_bytes(raw[0:4], "little") & _STATX_BTIME:
                return None
            sec = int.from_bytes(raw[80:88], "little", signed=True)
            nsec = int.from_bytes(raw[88:92], "little")
            return sec * 1_000_000_000 + nsec
