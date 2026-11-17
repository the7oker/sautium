"""New music appears by itself: a folder watcher over the OS's own calls
(LIBRARY-WATCH-DESIGN §1–§4, the design session's answers §15).

The watcher only says which folder changed; the one scan queue
(main.request_scan) does every read and write. It runs where the library's
mount delivers events for what other programs write — known by the mount,
never by writing into the library (Docker binds it read-only, and a write
through the node's own mount raises events even where nobody else's do):

- Linux, a local file system (the Pi's disk, a Linux Docker bind of one):
  inotify, a watch per folder, close-write as a file's end; a server process
  writing for another machine (smbd) raises the same events (M2);
- Windows, a local volume: ReadDirectoryChangesW, one handle on the root;
- macOS, a local volume: FSEvents.

Not armed: Docker Desktop's bind of a Windows or macOS path (9p, virtiofs:
no event for anything the host writes, M1), a NAS (CIFS, NFS, SMB — a
remote write raises none), any type not known to deliver them, and a
library past the inotify watch limit. There "Scan for new" is the way.

No lost event: the OS's own word that events were lost — inotify's queue
overflow, ReadDirectoryChangesW's empty answer, FSEvents' MustScanSubDirs
and dropped events — queues the whole library. `watchdog` drops all three,
which is why this is not it (§2.1). Nothing waits on a clock: a folder that
changes while it is scanned is scanned again after.
"""

import ctypes
import logging
import os
import select
import struct
import sys
import threading
from typing import Callable, Dict, Optional, Set

logger = logging.getLogger(__name__)

WATCHING, NO_EVENTS, UNREACHABLE, NOT_SET = "watching", "no_events", "unreachable", "not_set"

# The file systems a watch is armed on: local, every write through this
# kernel. ntfs-3g mounts as fuseblk.
_ARMED_FSTYPES = {"ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "zfs", "vfat", "exfat",
                  "ntfs3", "fuseblk", "hfsplus", "apfs", "hfs", "msdos"}
_WATCHED_SUFFIXES = None     # the scanner's audio extensions and ".cue", read at arm


def _watched(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in _WATCHED_SUFFIXES


# ── The mount's kind ─────────────────────────────────────────────────────

def _unescape_mount(field: str) -> str:
    return field.encode().decode("unicode_escape") if "\\" in field else field


def linux_kind(root: str, mountinfo: str) -> Optional[str]:
    """None when every mount holding the library — its own and each below
    it — is a local file system; else the first type that is not."""
    root = os.path.realpath(root).rstrip("/") or "/"
    holding, below = None, []
    for line in mountinfo.splitlines():
        left, _, right = line.partition(" - ")
        fields = left.split()
        if len(fields) < 5 or not right:
            continue
        point = _unescape_mount(fields[4]).rstrip("/") or "/"
        fstype = right.split()[0]
        if root == point or root.startswith(point.rstrip("/") + "/") or point == "/":
            if holding is None or len(point) >= len(holding[0]):
                holding = (point, fstype)
        elif point.startswith(root + "/"):
            below.append(fstype)
    for fstype in ([holding[1]] if holding else []) + below:
        if fstype not in _ARMED_FSTYPES:
            return fstype
    return None if holding else "unknown"


def _windows_kind(root: str) -> Optional[str]:
    drive = os.path.splitdrive(os.path.abspath(root))[0]
    if not drive or drive.startswith("\\\\"):
        return "network path"
    kind = ctypes.windll.kernel32.GetDriveTypeW(drive + "\\")
    return None if kind in (2, 3) else f"drive type {kind}"      # removable, fixed


def _macos_kind(root: str) -> Optional[str]:
    libc = ctypes.CDLL("/usr/lib/libc.dylib", use_errno=True)
    buf = ctypes.create_string_buffer(2168)                          # struct statfs (64-bit inodes)
    if libc.statfs(os.fsencode(root), buf) != 0:
        raise OSError(ctypes.get_errno(), "statfs failed", root)
    fstype = buf.raw[72:88].split(b"\0", 1)[0].decode()
    return None if fstype in _ARMED_FSTYPES else fstype


def mount_kind(root: str) -> Optional[str]:
    """Why the library's mount delivers no events for other programs'
    writes — its file system's type — or None where it does."""
    if sys.platform == "win32":
        return _windows_kind(root)
    if sys.platform == "darwin":
        return _macos_kind(root)
    with open("/proc/self/mountinfo") as f:
        return linux_kind(root, f.read())


# ── Linux: inotify ───────────────────────────────────────────────────────

class _Inotify:
    IN_MODIFY, IN_ATTRIB, IN_CLOSE_WRITE = 0x2, 0x4, 0x8
    IN_MOVED_FROM, IN_MOVED_TO, IN_CREATE, IN_DELETE = 0x40, 0x80, 0x100, 0x200
    IN_DELETE_SELF, IN_MOVE_SELF = 0x400, 0x800
    IN_UNMOUNT, IN_Q_OVERFLOW, IN_IGNORED, IN_ISDIR = 0x2000, 0x4000, 0x8000, 0x40000000
    IN_ONLYDIR = 0x01000000
    MASK = (IN_MODIFY | IN_ATTRIB | IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE
            | IN_DELETE | IN_DELETE_SELF | IN_MOVE_SELF | IN_ONLYDIR)

    def __init__(self, root: str, on_dirty, on_lost):
        self.root, self.on_dirty, self.on_lost = root, on_dirty, on_lost
        self._libc = ctypes.CDLL(None, use_errno=True)
        self.fd = self._libc.inotify_init1(os.O_CLOEXEC | os.O_NONBLOCK)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self._stop_r, self._stop_w = os.pipe()
        self.paths: Dict[int, str] = {}
        self.writing: Set[str] = set()
        self._lock = threading.Lock()
        try:
            self.add_tree(root)
        except OSError:
            self.close()                # the watches placed so far go with the fd
            raise

    def _add(self, path: str) -> None:
        wd = self._libc.inotify_add_watch(self.fd, os.fsencode(path), self.MASK)
        if wd < 0:
            err = ctypes.get_errno()
            if err in (2, 20):          # gone, or no longer a folder, since the listing
                return
            if err == 13:               # a folder the node may not read (lost+found):
                logger.info(f"Library watch: {path} is not readable, so not watched")
                return                  # the walk names it unread, as it does today
            raise OSError(err, os.strerror(err), path)
        self.paths[wd] = path

    def add_tree(self, top: str) -> None:
        """A watch on `top` and every folder below it, before its walk: what
        was written before a watch existed is found by the walk that
        follows, what is written after raises events."""
        stack = [top]
        while stack:
            folder = stack.pop()
            self._add(folder)
            try:
                with os.scandir(folder) as it:
                    stack.extend(e.path for e in it if e.is_dir(follow_symlinks=False))
            except OSError:
                pass        # the walk names a folder it cannot read

    def _drop_under(self, top: str) -> None:
        for wd, path in list(self.paths.items()):
            if path == top or path.startswith(top + "/"):
                self._libc.inotify_rm_watch(self.fd, wd)
                self.paths.pop(wd, None)

    def run(self) -> None:
        while True:
            ready = select.select([self.fd, self._stop_r], [], [])[0]
            if self._stop_r in ready:
                return
            try:
                data = os.read(self.fd, 64 * 1024)
            except BlockingIOError:
                continue
            offset = 0
            while offset + 16 <= len(data):
                wd, mask, _cookie, length = struct.unpack_from("iIII", data, offset)
                name = data[offset + 16:offset + 16 + length].split(b"\0", 1)[0].decode(
                    "utf-8", "surrogateescape")
                offset += 16 + length
                self._event(wd, mask, name)

    def _event(self, wd: int, mask: int, name: str) -> None:
        if mask & self.IN_Q_OVERFLOW:
            logger.warning("Library watch: the kernel's event queue overflowed — the whole "
                           "library is scanned")
            with self._lock:
                self.writing.clear()    # their closes may be among the events lost
            self.on_dirty(self.root, True)
            return
        folder = self.paths.get(wd)
        if mask & self.IN_IGNORED:
            self.paths.pop(wd, None)
            return
        if folder is None:
            return
        if mask & self.IN_UNMOUNT:
            if folder == self.root:
                self.on_lost("unmounted")
            else:                       # a mount inside the library went: judged whole
                self._drop_under(folder)
                self.on_dirty(folder, True)
            return
        if mask & (self.IN_DELETE_SELF | self.IN_MOVE_SELF) and folder == self.root:
            self.on_lost("the music folder went away")
            return
        if not name:
            return
        path = f"{folder}/{name}"
        if mask & self.IN_ISDIR:
            if mask & (self.IN_CREATE | self.IN_MOVED_TO):
                try:
                    self.add_tree(path)
                except OSError as e:
                    # past the watch limit: what this watch cannot see is no
                    # watch at all — disarmed, "Scan for new" again
                    self.on_lost(f"a watch could not be placed ({e})", NO_EVENTS)
                self.on_dirty(path, True)
            elif mask & (self.IN_DELETE | self.IN_MOVED_FROM):
                self._drop_under(path)
                self.on_dirty(path, True)
            return
        if not _watched(name):
            return
        with self._lock:
            if mask & self.IN_MODIFY or (mask & self.IN_CREATE and not self._whole(path)):
                self.writing.add(path)
                return
            self.writing.discard(path)
        self.on_dirty(folder, False)

    @staticmethod
    def _whole(path: str) -> bool:
        """A file created whole: a hard link to one (Lidarr's import, `cp -l`)
        or a symlink — no write and no close follow its IN_CREATE."""
        try:
            st = os.lstat(path)
        except OSError:
            return False
        import stat as stat_mod
        return stat_mod.S_ISLNK(st.st_mode) or st.st_nlink > 1

    def being_written(self) -> Set[str]:
        with self._lock:
            return set(self.writing)

    def stop(self) -> None:
        os.write(self._stop_w, b"x")

    def close(self) -> None:
        for fd in (self.fd, self._stop_r, self._stop_w):
            os.close(fd)


# ── Windows: ReadDirectoryChangesW ───────────────────────────────────────

class _ReadDirectoryChanges:
    FILE_ACTION_REMOVED, FILE_ACTION_RENAMED_OLD = 2, 4
    NOTIFY = 0x1 | 0x2 | 0x4 | 0x8 | 0x10 | 0x40      # names, dirs, attributes, size, write, creation

    def __init__(self, root: str, on_dirty, on_lost):
        import ctypes.wintypes as wt
        self.root, self.on_dirty, self.on_lost = root, on_dirty, on_lost
        k32 = self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wt.HANDLE
        k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                    wt.DWORD, wt.HANDLE]
        k32.ReadDirectoryChangesW.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, wt.BOOL,
                                              wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p,
                                              ctypes.c_void_p]
        k32.CancelIoEx.argtypes = [wt.HANDLE, ctypes.c_void_p]
        k32.CloseHandle.argtypes = [wt.HANDLE]
        self._wt = wt
        self.handle = k32.CreateFileW(root, 0x0001, 0x1 | 0x2 | 0x4, None, 3, 0x02000000, None)
        if self.handle in (ctypes.c_void_p(-1).value, None):
            raise ctypes.WinError(ctypes.get_last_error())
        self._stopping = False

    def run(self) -> None:
        buf = ctypes.create_string_buffer(64 * 1024)
        got = self._wt.DWORD()
        while not self._stopping:
            if not self._k32.ReadDirectoryChangesW(self.handle, buf, len(buf), True, self.NOTIFY,
                                                   ctypes.byref(got), None, None):
                if not self._stopping:
                    self.on_lost(f"ReadDirectoryChangesW failed ({ctypes.get_last_error()})")
                return
            if got.value == 0:
                logger.warning("Library watch: more changed than one answer holds — the whole "
                               "library is scanned")
                self.on_dirty(self.root, True)
                continue
            offset = 0
            while True:
                nxt, action, length = struct.unpack_from("III", buf.raw, offset)
                name = buf.raw[offset + 12:offset + 12 + length].decode("utf-16-le")
                self._event(action, os.path.join(self.root, name))
                if not nxt:
                    break
                offset += nxt

    def _event(self, action: int, path: str) -> None:
        parent = os.path.dirname(path)
        if action in (self.FILE_ACTION_REMOVED, self.FILE_ACTION_RENAMED_OLD):
            # gone: a folder's rows are judged under it, a file's in its folder
            self.on_dirty(path, True)
            self.on_dirty(parent, False)
        elif os.path.isdir(path):
            self.on_dirty(path, True)        # a folder added is named alone (M3)
        elif _watched(path):
            self.on_dirty(parent, False)

    def being_written(self) -> Set[str]:
        return set()         # the scan's deny-write open (file_stat.writer_holds) tells

    def stop(self) -> None:
        self._stopping = True
        self._k32.CancelIoEx(self.handle, None)

    def close(self) -> None:
        self._k32.CloseHandle(self.handle)


# ── macOS: FSEvents ──────────────────────────────────────────────────────

class _FSEvents:
    MUST_SCAN, USER_DROPPED, KERNEL_DROPPED, ROOT_CHANGED = 0x1, 0x2, 0x4, 0x20
    CREATED, REMOVED, RENAMED, IS_DIR = 0x100, 0x200, 0x800, 0x20000
    CREATE_FLAGS = 0x2 | 0x4 | 0x10                   # NoDefer, WatchRoot, FileEvents

    def __init__(self, root: str, on_dirty, on_lost):
        self.root, self.on_dirty, self.on_lost = root, on_dirty, on_lost
        cs = self._cs = ctypes.CDLL("/System/Library/Frameworks/CoreServices.framework/CoreServices")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        system = ctypes.CDLL("/usr/lib/libSystem.dylib")
        vp = ctypes.c_void_p
        self._callback_type = ctypes.CFUNCTYPE(None, vp, vp, ctypes.c_size_t,
                                               ctypes.POINTER(ctypes.c_char_p),
                                               ctypes.POINTER(ctypes.c_uint32),
                                               ctypes.POINTER(ctypes.c_uint64))
        self._callback = self._callback_type(self._events)
        cf.CFStringCreateWithCString.restype = vp
        cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFArrayCreate.restype = vp
        cf.CFArrayCreate.argtypes = [vp, ctypes.POINTER(vp), ctypes.c_long, vp]
        cs.FSEventStreamCreate.restype = vp
        cs.FSEventStreamCreate.argtypes = [vp, self._callback_type, vp, vp, ctypes.c_uint64,
                                           ctypes.c_double, ctypes.c_uint32]
        for fn in ("FSEventStreamStart", "FSEventStreamStop", "FSEventStreamInvalidate",
                   "FSEventStreamRelease"):
            getattr(cs, fn).argtypes = [vp]
        cs.FSEventStreamSetDispatchQueue.argtypes = [vp, vp]
        system.dispatch_queue_create.restype = vp
        system.dispatch_queue_create.argtypes = [ctypes.c_char_p, vp]
        path = cf.CFStringCreateWithCString(None, os.fsencode(root), 0x08000100)   # UTF-8
        paths = (vp * 1)(path)
        callbacks = ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeArrayCallBacks"))
        array = cf.CFArrayCreate(None, paths, 1, callbacks)
        self.stream = cs.FSEventStreamCreate(None, self._callback, None, array,
                                             0xFFFFFFFFFFFFFFFF, 0.0, self.CREATE_FLAGS)
        if not self.stream:
            raise OSError("FSEventStreamCreate failed")
        self._queue = system.dispatch_queue_create(b"sautium.library-watch", None)
        cs.FSEventStreamSetDispatchQueue(self.stream, self._queue)
        self._done = threading.Event()

    def _events(self, _stream, _info, count, paths, flags, _ids):
        for i in range(count):
            path = paths[i].decode("utf-8", "surrogateescape").rstrip("/")
            flag = flags[i]
            if flag & (self.MUST_SCAN | self.USER_DROPPED | self.KERNEL_DROPPED):
                logger.warning("Library watch: FSEvents dropped or coalesced events — scanning")
                self.on_dirty(path if flag & self.MUST_SCAN and not flag & (
                    self.USER_DROPPED | self.KERNEL_DROPPED) else self.root, True)
            elif flag & self.ROOT_CHANGED:
                self.on_dirty(self.root, True)
            elif flag & self.IS_DIR:
                if flag & (self.CREATED | self.REMOVED | self.RENAMED):
                    self.on_dirty(path, True)
            elif _watched(path):
                self.on_dirty(os.path.dirname(path), False)

    def run(self) -> None:
        if not self._cs.FSEventStreamStart(self.stream):
            self.on_lost("FSEventStreamStart failed")
            return
        self._done.wait()

    def being_written(self) -> Set[str]:
        return set()

    def stop(self) -> None:
        self._cs.FSEventStreamStop(self.stream)
        self._done.set()

    def close(self) -> None:
        self._cs.FSEventStreamInvalidate(self.stream)
        self._cs.FSEventStreamRelease(self.stream)


# ── The watcher ──────────────────────────────────────────────────────────

_SOURCE = {"win32": _ReadDirectoryChanges, "darwin": _FSEvents}.get(sys.platform, _Inotify)

_lock = threading.Lock()
_state = {"state": NOT_SET, "why": None}
_source = None
_thread: Optional[threading.Thread] = None
_request: Optional[Callable] = None


def state() -> Dict[str, Optional[str]]:
    """The watch as the node knows it: watching, no_events (and why),
    unreachable, not_set."""
    with _lock:
        return {"state": _state["state"], "why": _state["why"]}


def being_written() -> Set[str]:
    """The files the watcher saw written and not yet closed (inotify): a
    scan leaves them for their own close-write."""
    with _lock:
        source = _source
    return source.being_written() if source is not None else set()


def _rel(path: str, root: str) -> str:
    return os.path.relpath(path, root).replace("\\", "/") if path != root else ""


def start(request: Callable) -> None:
    """Arm the watcher on the music folder, then queue the whole library:
    what changed while the node was down is found by that walk, and what
    changes during it is in the queue (the watch is armed first). Called at
    the backend's start and again when the folder is back."""
    global _request, _WATCHED_SUFFIXES
    from config import settings
    from scanner import AUDIO_EXTENSIONS, library_unreachable
    _request = request
    _WATCHED_SUFFIXES = set(AUDIO_EXTENSIONS) | {".cue"}
    root = settings.music_library_path
    with _lock:
        if _source is not None:
            return
        if not root:
            _state.update(state=NOT_SET, why=None)
            return
        if not os.path.isdir(root):         # a drive not plugged in: armed when it is
            _state.update(state=UNREACHABLE, why="the music folder is not there")
            return
        if _state.get("arming"):
            return
        _state["arming"] = True
    try:
        if library_unreachable(wake=False):
            with _lock:
                _state.update(state=UNREACHABLE, why=None)
            return
        root = os.path.abspath(root)
        try:
            why = mount_kind(root)
        except OSError as e:
            why = f"its mount could not be read ({e})"
        if why is not None:
            with _lock:
                _state.update(state=NO_EVENTS, why=why)
            logger.info(f"Library watch: not armed — the music folder's mount is {why}, which "
                        "delivers no events for other programs' writes; 'Scan for new' finds new music")
            return
        _arm(root)
    finally:
        with _lock:
            _state.pop("arming", None)


def _arm(root: str) -> None:
    global _source, _thread

    def on_dirty(path: str, recursive: bool) -> None:
        rel = _rel(path, root)
        if not rel.startswith(".."):
            _request({rel: recursive}, explicit=False)

    holder = []

    def on_lost(why: str, as_state: str = UNREACHABLE) -> None:
        _lost(why, as_state, holder[0] if holder else None)

    try:
        source = _SOURCE(root, on_dirty, on_lost)
        holder.append(source)
    except OSError as e:
        with _lock:
            _state.update(state=NO_EVENTS, why=f"the watch could not be placed ({e})")
        hint = (" — raise fs.inotify.max_user_watches" if getattr(e, "errno", None) == 28 else "")
        logger.warning(f"Library watch: not armed: {e}{hint}")
        return
    with _lock:
        _source = source
        _state.update(state=WATCHING, why=None)
        _thread = threading.Thread(target=_run, args=(source,), daemon=True, name="library-watch")
        _thread.start()
    logger.info(f"Library watch: watching {root}")
    _request({"": True}, explicit=True)


def _run(source) -> None:
    try:
        source.run()
    except Exception as e:
        logger.error(f"Library watch failed: {e}", exc_info=True)
        _lost(f"the watch failed ({e})", UNREACHABLE, source)
    finally:
        source.close()


def _lost(why: str, as_state: str = UNREACHABLE, source=None) -> None:
    """The watch went (the drive unplugged, the folder removed): disarmed,
    nothing removed — a scan is no part of it. It is armed again when the
    folder answers (folder_back). Past the watch limit it is no_events. A
    late word from a source already replaced changes nothing."""
    global _source
    with _lock:
        if source is not None and source is not _source:
            return
        source, _source = _source, None
        _state.update(state=as_state, why=why)
    if source is not None:
        source.stop()
    logger.warning(f"Library watch: disarmed — {why}")


def folder_back() -> None:
    """The music folder answers again (scanner's look): arm once more."""
    if _request is not None and state()["state"] == UNREACHABLE:
        start(_request)


def stop() -> None:
    global _source
    with _lock:
        source, _source = _source, None
        thread = _thread
    if source is not None:
        source.stop()
    if thread is not None:
        thread.join(timeout=5)
