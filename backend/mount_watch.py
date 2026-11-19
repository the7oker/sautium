"""A disk that arrives after the node's start (PI-NODE-2B §4.3).

The roots a node reads its music under are bound `rslave`: a volume the host
mounts beneath one reaches the container's own mount table. The kernel says
when that table changes — a `poll()` on /proc/self/mountinfo wakes with
POLLERR|POLLPRI — so one thread blocks there, with no timer and no door of
the host channel. On each change it runs what depends on the table: the
folder sheet's list (the Library stream), the music folder's look (a card
pulled or put back: the notice, and through it the folder watcher's re-arm).

Only on a node that reads under roots (library_root.picks), and only where
the kernel offers the file (Linux).
"""

import logging
import select
import threading
from typing import Callable

logger = logging.getLogger(__name__)

MOUNTINFO = "/proc/self/mountinfo"


def watch(wait: Callable[[], bool], changed: Callable[[], None]) -> None:
    """`changed()` after every wake of `wait()`, until it says False (the
    watch was closed). `wait` is the file's poll — a mount table signals by
    POLLPRI, which no pipe can stand in for: tests drive this seam."""
    while wait():
        try:
            changed()
        except Exception:
            logger.exception("mount watch: a reader of the mount table failed")


def _changed() -> None:
    logger.info("mount watch: the mount table changed")
    from routers.settings import notify_library_subscribers
    from scanner import library_unreachable
    import library_watch
    # The folder's state, recorded and said where it changed (the notice; a
    # folder that is back re-arms its watch through the look's own rule)
    library_unreachable(wake=True)
    # A card put back at another moment than a look: the watch that waits for
    # the folder is asked directly as well
    library_watch.folder_back()
    notify_library_subscribers()


def _run() -> None:
    with open(MOUNTINFO, "rb", buffering=0) as f:
        poller = select.poll()
        poller.register(f, select.POLLERR | select.POLLPRI)

        def wait() -> bool:
            poller.poll()
            return True

        watch(wait, _changed)


def start() -> bool:
    """Watch the mount table on a node that reads its music under roots."""
    import library_root
    if not library_root.picks():
        return False
    try:
        open(MOUNTINFO, "rb").close()
    except OSError:
        return False
    threading.Thread(target=_run, name="mount-watch", daemon=True).start()
    logger.info("watching the mount table for disks under %s", ", ".join(library_root.roots()))
    return True
