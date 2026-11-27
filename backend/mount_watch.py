"""A disk that arrives after the node's start (PI-NODE-2B §4.3).

The roots a node reads its music under are bound `rslave`: a volume the host
mounts beneath one reaches the container's own mount table. The kernel says
when that table changes — a `poll()` on /proc/self/mountinfo wakes with
POLLERR|POLLPRI — so one thread blocks there, with no timer and no door of
the host channel. On each change it runs what depends on the table: the
folder sheet's list (the Library stream), each folder's look (a card pulled
or put back: the notice, and through it the folder watcher's re-arm), and a
disk the library never had, counted once to be offered for one tap
(library_root.count_new_disks, LIBRARY-FOLDERS-1 §3.2).

A disk that arrives and that nothing mounts changes no mount table. Root's
automount says so in a file beside the disks (library_root.unread, PI-NODE-2C
§2.3), and the folder of those files is watched the same way: one inotify
descriptor, one thread blocked in its read, for a file moved in (a volume no
driver mounted) and a file removed (the volume pulled). The watch is armed at
the node's start where the folder is; an automount installed later is met at
the next start, which the setup that installed it makes.

Only on a node that reads under roots (library_root.picks), and only where
the kernel offers the file (Linux).
"""

import ctypes
import logging
import os
import select
import threading
from typing import Callable, List

logger = logging.getLogger(__name__)

MOUNTINFO = "/proc/self/mountinfo"


# inotify(7): a file renamed into the folder, a file unlinked from it; the
# path must be a folder
_IN_MOVED_TO, _IN_DELETE, _IN_ONLYDIR = 0x80, 0x200, 0x01000000


def watch(wait: Callable[[], bool], changed: Callable[[], None], of: str = "the mount table") -> None:
    """`changed()` after every wake of `wait()`, until it says False (the
    watch was closed). `wait` is the file's poll — a mount table signals by
    POLLPRI, which no pipe can stand in for: tests drive this seam."""
    while wait():
        try:
            changed()
        except Exception:
            logger.exception("mount watch: a reader of %s failed", of)


def _changed() -> None:
    logger.info("mount watch: the mount table changed")
    from db_pool import db_execute
    from routers.settings import notify_library_subscribers
    from scanner import look_all
    import library_root
    import library_watch
    # Each folder's state, recorded and said where it changed (the notice; a
    # folder that is back re-arms its watch through the look's own rule)
    look_all(wake=True)
    # A card put back at another moment than a look: the watches that wait
    # for their folder are asked directly as well
    library_watch.folder_back()
    # A disk plugged in: its folders take the label it carries now, and a
    # disk the library never had is counted, to be offered for one tap
    library_root.refresh_labels()
    if library_root.count_new_disks(plugged=True):
        db_execute("NOTIFY sautium_notices")
    notify_library_subscribers()


def _count_at_start() -> None:
    """The disks plugged in before the start, counted once: the tabs
    already open hear of one that holds music."""
    import library_root
    from db_pool import db_execute
    from routers.settings import notify_library_subscribers
    if library_root.count_new_disks():
        db_execute("NOTIFY sautium_notices")
        notify_library_subscribers()


def _unread_changed() -> None:
    logger.info("mount watch: the volumes nothing mounted changed")
    from db_pool import db_execute
    from routers.settings import notify_library_subscribers
    # The notice (a toast as it happens; gone with the volume) and the folder
    # sheet's list
    db_execute("NOTIFY sautium_notices")
    notify_library_subscribers()


def unread_watch(folders: List[str]) -> int:
    """An inotify descriptor that reads whenever a file is moved into one of
    `folders` or removed from it — the automount writes beside and renames,
    so a file is whole when it is seen."""
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.inotify_init1(os.O_CLOEXEC)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "inotify_init1 failed")
    for folder in folders:
        if libc.inotify_add_watch(fd, os.fsencode(folder), _IN_MOVED_TO | _IN_DELETE | _IN_ONLYDIR) < 0:
            err = ctypes.get_errno()
            os.close(fd)
            raise OSError(err, os.strerror(err), folder)
    return fd


def _run_unread(fd: int) -> None:
    # Every event queued by then leaves in one read: one wake for them all
    watch(lambda: bool(os.read(fd, 65536)), _unread_changed, of="the volumes nothing mounted")


def _run() -> None:
    with open(MOUNTINFO, "rb", buffering=0) as f:
        poller = select.poll()
        poller.register(f, select.POLLERR | select.POLLPRI)

        def wait() -> bool:
            poller.poll()
            return True

        watch(wait, _changed)


def start() -> bool:
    """Watch the mount table, and the automount's word of what it could not
    mount, on a node that reads its music under roots."""
    import library_root
    if not library_root.picks():
        return False
    try:
        open(MOUNTINFO, "rb").close()
    except OSError:
        return False
    folders = library_root.unread_folders()
    if folders:
        try:
            fd = unread_watch(folders)
        except OSError as e:
            logger.error("mount watch: %s cannot be watched (%s): a disk nothing mounts will be said only "
                         "when the folder sheet is opened", ", ".join(folders), e)
        else:
            threading.Thread(target=_run_unread, args=(fd,), name="unread-watch", daemon=True).start()
            logger.info("watching %s for disks nothing mounted", ", ".join(folders))
    threading.Thread(target=_count_at_start, name="new-disks", daemon=True).start()
    threading.Thread(target=_run, name="mount-watch", daemon=True).start()
    logger.info("watching the mount table for disks under %s", ", ".join(library_root.roots()))
    return True
