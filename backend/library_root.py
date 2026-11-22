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

What the machine's root leaves beside the disks is read here too, and
believed only as far as it goes: a volume's label (`.names/`), and — since
2026-11-22, PI-NODE-2C §2.3 — a volume that was plugged in and that nothing
mounted (`.unread/`, `unread`): one small file a volume, written by the
automount's script, whose label and filesystem type a stranger's disk chose.
Each is read bounded, never through a link, and no path is built from what a
file says.

Every other node — a launcher's, a Docker node whose folder is named in .env
— has no roots, and this module changes nothing there (`picks` is False).
"""

import json
import logging
import os
import re
import stat
from datetime import datetime, timezone
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
# A volume no driver mounted, as the automount's script leaves it beside the
# disks (`<root>/.unread/<kernel name>`): the boot it belongs to, the
# filesystem's type as blkid names it, the label — three lines
UNREAD_DIR = ".unread"
UNREAD_BYTES_MAX = 256
BOOT_ID = "/proc/sys/kernel/random/boot_id"
MOUNTINFO = "/proc/self/mountinfo"
_KERNEL_NAME = re.compile(r"[A-Za-z0-9]+\Z")
_FS_TYPE = re.compile(r"[a-z0-9_]{1,16}\Z")
# blkid's type → (the name a person knows the format by, whether the
# automount mounts that type — so the volume is here because a mount FAILED,
# not because the format is one Sautium does not read). Any other type is
# "an unknown format": the page never shows a stranger's bytes as a format.
_FORMATS = {"vfat": ("FAT", True), "exfat": ("exFAT", True), "ntfs": ("NTFS", True), "ext4": ("ext4", True),
            "apfs": ("APFS", False), "hfsplus": ("HFS+", False), "hfs": ("HFS+", False),
            "btrfs": ("Btrfs", False), "xfs": ("XFS", False), "ext2": ("ext2/3", False),
            "ext3": ("ext2/3", False)}


class Refused(ValueError):
    """A folder that is not this node's to read, in words for the page."""


def roots() -> List[str]:
    """The roots as the kernel names them, in the order given. Each as it
    was written: a folder's name may begin or end with a space."""
    found = []
    for raw in (settings.sautium_library_roots or "").split(":"):
        if raw:
            real = os.path.realpath(raw)
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
    mounts = _mounts()
    for root in roots():
        if _gone(root, mounts):
            logger.warning("music root %s is empty: its disk is not mounted, or the folder was removed; "
                           "its albums are away. Forget it on the machine: ./sautium roots --forget %s",
                           root, root)


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
    # The newest choice is the folder (chosen): later than every stored one
    # whatever the clock says — a board with no battery starts in the past
    # until the network sets its time.
    db_execute("""
        INSERT INTO library_roots (mount, subpath, chosen_at)
        SELECT %s, %s, GREATEST(now(), COALESCE(max(chosen_at) + interval '1 microsecond', now()))
        FROM library_roots
        ON CONFLICT (mount, subpath) DO UPDATE SET chosen_at = EXCLUDED.chosen_at""", (mount, subpath))
    settings.music_library_path = real
    logger.info("music folder chosen: %s", real)
    return real


def _read_small(path: str, most: int) -> Optional[tuple]:
    """(the bytes, the mtime) of a small regular file root left for the node:
    never through a link, never waiting on a FIFO, never more than `most`
    bytes — a longer file is not one the automount wrote."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        raw = os.read(fd, most + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    return raw, st.st_mtime


def _one_line(raw: bytes) -> Optional[str]:
    """A stranger's label as the page gets it: its first line, bounded."""
    return raw[:LABEL_BYTES_MAX].decode("utf-8", "ignore").split("\n", 1)[0].strip() or None


def _label(root: str, name: str) -> Optional[str]:
    """The volume label the automount left for the mount point `name` under
    `root`, if any: read bounded, one line, never through a link."""
    read = _read_small(os.path.join(root, NAMES_DIR, name), LABEL_BYTES_MAX)
    if read is None:
        return None
    return _one_line(read[0])


def unread_folders() -> List[str]:
    """Where the automount says which volumes it could not mount: the folder
    beside the disks, under each root that is the automount's."""
    return [folder for folder in (os.path.join(root, UNREAD_DIR) for root in roots() if _disks_folder(root))
            if os.path.isdir(folder)]


def unread() -> List[Dict[str, Any]]:
    """The volumes plugged into this machine that nothing mounted, as the
    automount left them: {device (the kernel's name), format (a name from
    the fixed map, None for a type outside it), mounts (the automount mounts
    that type: this one failed), label, since}. A file of another boot (a
    power cut left it) says nothing about now and is not listed; neither is
    anything that is not the automount's small regular file."""
    try:
        with open(BOOT_ID) as f:
            boot = f.read().strip()
    except OSError:
        return []
    found = []
    for folder in unread_folders():
        try:
            names = sorted(name for name in os.listdir(folder) if _KERNEL_NAME.match(name))
        except OSError:
            continue
        for name in names:
            read = _read_small(os.path.join(folder, name), UNREAD_BYTES_MAX)
            if read is None or len(read[0]) > UNREAD_BYTES_MAX:
                continue
            lines = read[0].decode("utf-8", "replace").split("\n")
            if not boot or lines[0].strip() != boot:
                continue
            fs = lines[1].strip() if len(lines) > 1 else ""
            fmt, mounts = _FORMATS.get(fs if _FS_TYPE.match(fs) else "", (None, False))
            label = _one_line(lines[2].encode("utf-8")) if len(lines) > 2 else None
            found.append({"device": name, "format": fmt, "mounts": mounts, "label": label,
                          "since": datetime.fromtimestamp(read[1], timezone.utc).isoformat()})
    return found


def _root_labels() -> Dict[str, str]:
    """The labels the setup read for the roots that are disks themselves (a
    disk its fstab line mounts: no automount leaves a `.names` entry for it,
    and a container cannot ask blkid): root → label, a stranger's text."""
    try:
        named = json.loads(settings.sautium_library_root_labels or "{}")
    except ValueError:
        logger.warning("SAUTIUM_LIBRARY_ROOT_LABELS is not JSON: the roots are listed without labels")
        return {}
    if not isinstance(named, dict):
        return {}
    labels = {}
    for root, label in named.items():
        text = _one_line(label.encode("utf-8")) if isinstance(label, str) else None
        if text:
            labels[os.path.realpath(root)] = text
    return labels


def _size(path: str) -> Optional[int]:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return st.f_frsize * st.f_blocks


def _mounts() -> Dict[str, bool]:
    """Every point this process sees a WHOLE filesystem mounted at → whether
    that filesystem is mounted read-only. Whole: the mount shows the
    filesystem's own root — in a container every root is a mount point (its
    bind), and only a disk mounted there shows its filesystem's root. The
    read-only word is the superblock's, the same in every namespace; the
    mount's own flags are the bind's."""
    from library_watch import _unescape_mount
    mounts = {}
    try:
        with open(MOUNTINFO) as f:
            for line in f:
                fields, _, rest = line.partition(" - ")
                fields, rest = fields.split(), rest.split()
                if len(fields) > 4 and fields[3] == "/":
                    point = _unescape_mount(fields[4]).rstrip("/") or "/"
                    mounts[point] = len(rest) > 2 and "ro" in rest[2].split(",")
    except OSError:
        pass
    return mounts


def _gone(root: str, mounts: Dict[str, bool]) -> bool:
    """A root that is there, empty, and no disk: the folder Docker made for a
    root whose own was removed, or a mount point whose disk is out."""
    if root in mounts or _disks_folder(root):
        return False
    try:
        with os.scandir(root) as it:
            return next(it, None) is None
    except OSError:
        return False


def _disk_points(mounts: Dict[str, bool]) -> List[tuple]:
    """(path, root, name) of each disk a choice can start at: every one
    mounted in the automount's folder, and every root that is a disk itself
    (name None: no automount named it)."""
    points = []
    for root in roots():
        if not os.path.isdir(root):
            continue
        if root in mounts:
            points.append((root, root, None))
        elif _disks_folder(root):
            # a mount point: a disk that is there now (an unplugged one
            # leaves at most an empty folder)
            points.extend((path, root, name) for name in _subfolders(root)
                          for path in [os.path.join(root, name)] if path in mounts)
    return points


def pullable(path: Optional[str]) -> bool:
    """Whether `path` lies on a plugged disk nothing writes to (the machine
    mounted it read-only): pulling it cuts nothing short."""
    if not path or not picks():
        return False
    mounts = _mounts()
    return any(mounts[disk] and (path == disk or path.startswith(disk.rstrip("/") + "/"))
               for disk, _root, _name in _disk_points(mounts))


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
    root that is a folder of music itself (`empty` where its disk is out or
    the folder was removed), each disk mounted beneath a root (the
    automount's folder) or as a root, with its label and size — and, last,
    each volume plugged in that nothing mounted (kind "unread": no path, a
    row to read, never to open). With `under`: that folder's own folders,
    its audio count, and the way back up."""
    if not picks():
        raise Refused("This node's music folder is named where the node was set up")
    if under is None:
        mounts = _mounts()
        labels = _root_labels()
        points = _disk_points(mounts)
        places = []
        for root in roots():
            if not os.path.isdir(root):
                continue
            if root in mounts or _disks_folder(root):
                places.extend({"path": path, "name": name or path, "kind": "disk",
                               "label": _label(root, name) if name else labels.get(path),
                               "size_bytes": _size(path), "read_only": mounts[path]}
                              for path, of, name in points if of == root)
            else:
                # a folder named at setup: itself the place
                places.append({"path": root, "name": root, "kind": "folder", "label": None,
                               "size_bytes": None, "empty": _gone(root, mounts)})
        # A volume nothing mounted is a place that cannot be opened: no path
        places.extend({"path": None, "name": volume["device"], "kind": "unread", "label": volume["label"],
                       "size_bytes": None, "format": volume["format"], "mounts": volume["mounts"]}
                      for volume in unread())
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
