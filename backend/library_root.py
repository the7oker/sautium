"""The library's folder as the owner's choice under mounted roots
(PI-NODE-2B §4.3).

A node its console setup made is given ROOTS: the folders it may read music
from — where the machine's automount puts plugged disks, a folder named at
setup — each bound into the container read-only at its own host path
(`SAUTIUM_LIBRARY_ROOTS`, from the setup's compose override, which no
container can write). The path the library stores is then the path an
HQPlayer on the same machine opens, with no translation. Under those roots
the owner picks the library's folder in the Web UI; the choice is a row
(`library_roots`), and `settings.music_library_path` is that row for the life
of the process (`apply`, `choose`). With no row the node has no music folder
yet, which the scanner, the watcher and the notices already know as "not
set".

The roots are the mount, and the mount is the boundary: everything a page
names is resolved by the kernel's own view (`realpath`) and must stay under a
root; a link is never followed out of one, by the picker or by the scanner
(`within`). A volume's label and a folder's name are a stranger's text: data
for the page to escape, never part of a decision here.

Every other node — a launcher's, a Docker node whose folder is named in .env
— has no roots, and this module changes nothing there (`picks` is False).
"""

import logging
import os
from typing import Any, Dict, List, Optional

from config import settings
from db_pool import db_execute, db_query_one

logger = logging.getLogger(__name__)

# The picker counts audio files under the folder it shows, to say what a
# choice would bring: a bounded walk, so a tap never waits on a whole disk.
COUNT_ENTRIES_MAX = 20_000
# One listing's folders: a disk's top level, never a tree
FOLDERS_MAX = 2_000
# A volume's label as the automount leaves it beside its mount point
# (`<root>/.names/<mount point's name>`): one line, a stranger's text
LABEL_BYTES_MAX = 128
NAMES_DIR = ".names"


class Refused(ValueError):
    """A folder that is not this node's to read, in words for the page."""


def roots() -> List[str]:
    """The roots as the kernel names them, in the order given."""
    found = []
    for raw in (settings.sautium_library_roots or "").split(":"):
        if raw.strip():
            real = os.path.realpath(raw.strip())
            if real not in found:
                found.append(real)
    return found


def picks() -> bool:
    """This node's owner chooses the library's folder in the Web UI."""
    return bool(roots())


def within(path: str) -> Optional[str]:
    """`path` as the kernel resolves it, if that stays under a root; None
    for one that leaves them (a link out, `..`) or where no root is."""
    real = os.path.realpath(path)
    for root in roots():
        if real == root or real.startswith(root.rstrip("/") + "/"):
            return real
    return None


def chosen() -> Optional[str]:
    """The folder the owner chose, as stored; None before any choice."""
    row = db_query_one("SELECT mount, subpath FROM library_roots ORDER BY chosen_at DESC, id DESC LIMIT 1")
    if row is None:
        return None
    return row["mount"] if not row["subpath"] else f"{row['mount'].rstrip('/')}/{row['subpath']}"


def apply() -> None:
    """At the node's start, after the migrations: the stored choice becomes
    the library's folder. A choice whose root is no longer mounted (the setup
    ran again with other folders) is no folder: the Library row says "Not
    chosen" again, and nothing stored is touched."""
    if not picks():
        return
    path = chosen()
    if path is not None and within(path) is None:
        logger.warning("the chosen music folder %s lies under no root this node is given now", path)
        path = None
    settings.music_library_path = path or ""
    # A root is mounted at its own host path: what the library stores is what
    # this process reads
    settings.music_host_path = None
    logger.info("music folder: %s (roots: %s)", path or "not chosen", ", ".join(roots()))


def choose(path: str) -> str:
    """The owner's choice: stored, and the library's folder from now on. The
    caller re-arms the folder watcher and asks for a scan; what the library
    held under another folder stays until a Rescan's question."""
    if not picks():
        raise Refused("This node's music folder is named where the node was set up")
    real = within(path)
    if real is None or not os.path.isdir(real):
        raise Refused("That folder is not one this node may read")
    mount = next(r for r in roots() if real == r or real.startswith(r.rstrip("/") + "/"))
    subpath = real[len(mount):].strip("/")
    db_execute("""
        INSERT INTO library_roots (mount, subpath) VALUES (%s, %s)
        ON CONFLICT (mount, subpath) DO UPDATE SET chosen_at = now()""", (mount, subpath))
    settings.music_library_path = real
    logger.info("music folder chosen: %s", real)
    return real


def _label(root: str, name: str) -> Optional[str]:
    """The volume label the automount left for the mount point `name` under
    `root`, if any: read bounded, one line, never through a link."""
    try:
        fd = os.open(os.path.join(root, NAMES_DIR, name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not os.path.isfile(f"/proc/self/fd/{fd}"):
            return None
        raw = os.read(fd, LABEL_BYTES_MAX)
    except OSError:
        return None
    finally:
        os.close(fd)
    text = raw.decode("utf-8", "replace").split("\n", 1)[0].strip()
    return text or None


def _size(path: str) -> Optional[int]:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return st.f_frsize * st.f_blocks


def _mount_points() -> set:
    """Every mount point this process sees (the mount table's fifth field)."""
    from library_watch import _unescape_mount
    points = set()
    try:
        with open("/proc/self/mountinfo") as f:
            for line in f:
                fields = line.split()
                if len(fields) > 4:
                    points.add(_unescape_mount(fields[4]).rstrip("/") or "/")
    except OSError:
        pass
    return points


def _subfolders(folder: str) -> List[str]:
    """The names of the real folders in `folder`: no link, no hidden name."""
    names = []
    with os.scandir(folder) as it:
        for entry in it:
            if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                continue
            names.append(entry.name)
            if len(names) >= FOLDERS_MAX:
                break
    return sorted(names, key=str.casefold)


def _count_audio(folder: str) -> Dict[str, Any]:
    """Audio files under `folder`, by a walk bounded in entries: `capped`
    when the bound ended it (the page then says "N+")."""
    from scanner import AUDIO_EXTENSIONS, hidden_name
    files = seen = 0
    stack = [folder]
    while stack and seen < COUNT_ENTRIES_MAX:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    seen += 1
                    if hidden_name(entry.name):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif (os.path.splitext(entry.name)[1].lower() in AUDIO_EXTENSIONS
                          and entry.is_file(follow_symlinks=False)):
                        files += 1
                    if seen >= COUNT_ENTRIES_MAX:
                        break
        except OSError:
            continue
    return {"audio_files": files, "capped": bool(stack) or seen >= COUNT_ENTRIES_MAX}


def folders(under: Optional[str] = None) -> Dict[str, Any]:
    """The picker's listing. With no `under`: where a choice can start — each
    root that is a folder of music itself, and each disk mounted beneath a
    root (the automount's folder), with its label and size. With `under`:
    that folder's own folders, its audio count, and the way back up."""
    if not picks():
        raise Refused("This node's music folder is named where the node was set up")
    if under is None:
        points = _mount_points()
        places = []
        for root in roots():
            if not os.path.isdir(root):
                continue
            if not _disks_folder(root):
                # a folder named at setup: itself the place
                places.append({"path": root, "name": root, "kind": "folder", "label": None,
                               "size_bytes": None})
                continue
            for name in _subfolders(root):
                path = os.path.join(root, name)
                # a mount point: a disk that is there now (an unplugged one
                # leaves at most an empty folder)
                if path in points:
                    places.append({"path": path, "name": name, "kind": "disk",
                                   "label": _label(root, name), "size_bytes": _size(path)})
        return {"under": None, "places": places, "chosen": settings.music_library_path or None}
    real = within(under)
    if real is None or not os.path.isdir(real):
        raise Refused("That folder is not one this node may read")
    parent = os.path.dirname(real)
    try:
        names = _subfolders(real)
    except OSError as e:
        raise Refused(f"That folder could not be read ({e.strerror or e})")
    # Up from a root, or from a disk in the automount's folder, is the list of places
    top = real in roots() or (parent in roots() and _disks_folder(parent))
    return {"under": real,
            "parent": None if top else parent,
            "folders": [{"name": name, "path": os.path.join(real, name)} for name in names],
            "chosen": settings.music_library_path or None,
            **_count_audio(real)}


def _disks_folder(root: str) -> bool:
    """`root` is the automount's folder (it carries the labels' folder): the
    place disks appear in, not itself a folder of music."""
    return os.path.isdir(os.path.join(root, NAMES_DIR))
