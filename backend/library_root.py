"""The library's folders: every folder the person has given it, several at
once (LIBRARY-FOLDERS-1, 2026-11-26; before it, one chosen folder —
PI-NODE-2B §4.3).

A folder is a row of `library_folders`, its `path` as media_files.file_path
spells it: the prefix of every file it holds, and its identity. It is given
three ways, by the kind of node (`kind`):

- "pick" — a node its console setup made is given ROOTS: the folders it may
  read music from — the folder the machine mounts music disks in (its
  automount, or the setup's line of /etc/fstab for one disk: the same point
  for a disk whoever mounts it, named by the filesystem's UUID), a folder
  named at setup — each bound into the container read-only at its own host
  path (`SAUTIUM_LIBRARY_ROOTS`, from the setup's compose override, which no
  container can write). The path the library stores is then the path an
  HQPlayer on the same machine opens, with no translation. The owner adds
  folders under those roots in the Web UI (`add`); a disk plugged in that the
  library never had is offered for one tap (`new_disks`), never joined by
  itself;
- "launcher" — the launcher's window adds them (the OS's own folder dialog,
  the person at the machine: the route admits a host-signed call alone); its
  one folder of before is the first row (db_migrate's library_folders_v1);
- "setup" — a Docker node whose .env names its folder (MUSIC_LIBRARY_PATH
  bound at /music, MUSIC_HOST_PATH the stored prefix): that folder, made sure
  of at every start (`apply`); another is another bind in its compose file.

No folder lies inside another: the one writer (`add`) refuses the inner and
merges the outer, under a lock of the table. A folder leaves only by the
owner's confirmed removal of what it alone held (scanner.plan_folder_removal,
CLAUDE.md's retention rule). Whether a folder answers now is the process's
look (scanner), never stored.

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
"""

import json
import logging
import os
import re
import shlex
import stat
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import settings
from db_pool import db_execute, db_query, db_query_one, transaction

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
# filesystem's type as blkid names it, the label, the driver the mount was
# tried with ("-": none, the type is one the script never mounts) — four lines.
# Where blkid names no filesystem the type line is one of the script's own two
# words (since 2026-11-24, PI-NODE-2D §5): the marks of more than one
# filesystem, or none found
UNREAD_DIR = ".unread"
UNREAD_BYTES_MAX = 256
BOOT_ID = "/proc/sys/kernel/random/boot_id"
MOUNTINFO = "/proc/self/mountinfo"
_KERNEL_NAME = re.compile(r"[A-Za-z0-9]+\Z")
_FS_TYPE = re.compile(r"[a-z0-9_]{1,16}\Z")
# The drivers the automount mounts with; anything else on the fourth line is none
_DRIVERS = frozenset({"vfat", "exfat", "ntfs3", "ntfs-3g", "ext4"})
# blkid's type → (the name a person knows the format by, whether the
# automount mounts that type — so the volume is here because a mount FAILED,
# not because the format is one Sautium does not read). Any other type is
# "an unknown format": the page never shows a stranger's bytes as a format.
_FORMATS = {"vfat": ("FAT", True), "exfat": ("exFAT", True), "ntfs": ("NTFS", True), "ext4": ("ext4", True),
            "apfs": ("APFS", False), "hfsplus": ("HFS+", False), "hfs": ("HFS+", False),
            "btrfs": ("Btrfs", False), "xfs": ("XFS", False), "ext2": ("ext2/3", False),
            "ext3": ("ext2/3", False)}
_NO_ONE_FILESYSTEM = frozenset({"ambivalent", "unknown"})
# Where the setup's line of /etc/fstab mounted a music disk until 2026-11-24:
# a point of its own, which was the root (scripts/setup.py's
# OLD_BOOT_MOUNT_POINT); since then the disks' folder's point for it,
# `<disks' folder>/<UUID>`. Both name the filesystem's UUID, written by root's
# own lines — the proof that the two are one disk, never a label or a name.
OLD_BOOT_MOUNT_POINT = re.compile(r"/mnt/sautium-([0-9A-Za-z-]{4,40})")


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
    """This node's owner adds the library's folders in the Web UI, under
    the roots its setup gave it. Read off the setting alone: no root is
    resolved here (a reader on the play path never waits on a mount)."""
    return any((settings.sautium_library_roots or "").split(":"))


def kind() -> str:
    """Who gives this node its folders: "pick" (the Web UI, under the
    roots), "launcher" (the launcher's window), "setup" (the node's .env)."""
    if picks():
        return "pick"
    from claude_code import is_launcher_mode
    return "launcher" if is_launcher_mode() else "setup"


def within(path: str) -> Optional[str]:
    """`path` as the kernel resolves it, if that stays under a root; None
    for one that leaves them (a link out, `..`) or where no root is."""
    real = os.path.realpath(path)
    for root in roots():
        if real == root or real.startswith(root.rstrip("/") + "/"):
            return real
    return None


def _norm(path: str) -> str:
    """Forward slashes, no trailing separator — but a drive's root keeps
    its own (`F:/`: `F:` alone is that drive's current folder)."""
    p = path.replace("\\", "/").rstrip("/")
    return p + "/" if re.fullmatch(r"[A-Za-z]:", p) else (p or "/")


def stored(path: str) -> str:
    """A folder or a file as media_files.file_path spells it: forward
    slashes, no trailing separator (the launcher's E:\\Music is E:/Music; a
    Docker node of Windows or macOS stores its bind's host path)."""
    return _norm(settings.translate_to_host_path(path))


def local(path: str) -> str:
    """A stored path as this process opens it."""
    return settings.translate_to_local_path(path)


def _key(path: str) -> str:
    """A stored path as two are compared: Windows' names ignore case."""
    return path.casefold() if os.name == "nt" else path


def _under(path: str, folder: str) -> bool:
    p, f = _key(path), _key(folder)
    return p == f or p.startswith(f.rstrip("/") + "/")


def env_folder() -> Optional[str]:
    """The folder a setup node's .env names, stored form: MUSIC_HOST_PATH
    where the node translates (a Docker node of Windows or macOS), else
    MUSIC_LIBRARY_PATH. None on a node of another kind."""
    if kind() != "setup" or not settings.music_library_path:
        return None
    return _norm(settings.music_host_path or settings.music_library_path)


# The table's rows as this process last read them. The backend is the one
# writer (add, the forget's yes, the start's apply and its re-point), and each
# write drops them (invalidate): a reader on the play path — a file served to a
# renderer, a slot's way in — asks the database nothing.
_rows: Optional[List[Dict[str, Any]]] = None
_rows_lock = threading.Lock()


def invalidate() -> None:
    """The folders changed (a write of the table): read again at next use."""
    global _rows
    with _rows_lock:
        _rows = None


def given() -> List[Dict[str, Any]]:
    """The library's folders, oldest first: {id, path, label}. On a setup
    node the folder its .env names is one, row or not yet (the start writes
    its row)."""
    global _rows
    with _rows_lock:
        if _rows is None:
            _rows = [dict(r) for r in db_query("SELECT id, path, label FROM library_folders ORDER BY added_at, id")]
        rows = [dict(r) for r in _rows]
    env = env_folder()
    if env is not None and not any(_key(r["path"]) == _key(env) for r in rows):
        rows.append({"id": None, "path": env, "label": None})
    return rows


def paths() -> List[str]:
    return [f["path"] for f in given()]


def folder_of(path: str, folders: Optional[List[str]] = None) -> Optional[str]:
    """The folder (its stored path) a stored path lies in, None outside
    every folder of the library — the deepest, should two ever nest."""
    return max((f for f in (paths() if folders is None else folders) if _under(path, f)), key=len, default=None)


def folder_name(folder: Dict[str, Any]) -> str:
    """A folder as a page and a log line name it: under the disks' folder
    its disk's label and the folder beneath it (the mount point's own name
    is a UUID), else its path."""
    path = folder["path"]
    for root in roots():
        if _disks_folder(root) and _under(path, root) and path != root:
            point, _, under = path[len(root.rstrip("/")) + 1:].partition("/")
            disk = folder.get("label") or "USB disk"
            return f"{disk} · {under}" if under else disk
    return path


def apply() -> None:
    """At the node's start, after the migrations. A setup node's .env folder
    has its row; a disk the setup's old line mounted at a root of its own,
    now mounted in the disks' folder, takes its files, copies, covers and
    folder along (_follow_moved_disks). A folder under a root the node is
    no longer given stays a folder of the library: not connected, its
    albums listed, forgotten only by the owner. What each folder holds is
    said."""
    env = env_folder()
    with _write, transaction() as cur:
        cur.execute("LOCK TABLE library_folders IN SHARE ROW EXCLUSIVE MODE")
        if env is not None:
            # the .env's word: its folder is a folder of the library; one of
            # before that holds it is narrowed to it (the copies outside it
            # are "Elsewhere"), those it holds are taken in
            _place(cur, env, None, narrow=True)
    if picks():
        _follow_moved_disks()
    with _write, transaction() as cur:
        cur.execute("LOCK TABLE library_folders IN SHARE ROW EXCLUSIVE MODE")
        _unnest(cur)
    invalidate()
    folders = given()
    logger.info("music folders: %s%s", ", ".join(f["path"] for f in folders) or "none",
                f" (roots: {', '.join(roots())})" if picks() else "")
    if not picks():
        return
    mounts = _mounts()
    for root in roots():
        if not _gone(root, mounts):
            continue
        if _held(root):
            logger.warning("music root %s is empty: its disk is not mounted, or the folder was removed; "
                           "its albums are not connected. Forget it on the machine: ./sautium roots --forget %s",
                           root, shlex.quote(root))
        else:
            logger.info("music root %s is empty: nothing is in it yet", root)


# The move of a disk from its old point to its new one, in one statement. A
# copy moves whole with its files, or stays as it was where a row holds its
# new place already (a scan read the disk there first): away, for the owner's
# Away row (PI-NODE-2E Q5). The stat columns stay, so the start's scan finds
# each file unchanged at its new path; no id changes, so tracks, analysis,
# listens and the queue are untouched. A folder at the new place already is
# the same folder: one row, the earlier.
_MOVE_SQL = """
    WITH moves AS (
        SELECT old, uuid, %(disks)s::text || '/' || uuid AS new
          FROM unnest(%(olds)s::text[], %(uuids)s::text[]) AS m(old, uuid)),
    old_variants AS (
        SELECT v.id, v.album_id, m.new || substr(v.directory_path, length(m.old) + 1) AS path
          FROM album_variants v JOIN moves m
            ON v.directory_path = m.old OR starts_with(v.directory_path, m.old || '/')
         WHERE v.location = 'local'),
    old_files AS (
        SELECT f.id, f.album_variant_id, f.cue_start_seconds, m.old,
               m.new || substr(f.file_path, length(m.old) + 1) AS path
          FROM media_files f JOIN moves m ON starts_with(f.file_path, m.old || '/')),
    taken AS (
        SELECT f.id, f.album_variant_id FROM old_files f
         WHERE EXISTS (SELECT 1 FROM media_files o
                        WHERE o.file_path = f.path AND o.cue_start_seconds IS NOT DISTINCT FROM f.cue_start_seconds)),
    stays AS (
        SELECT v.id FROM old_variants v
         WHERE EXISTS (SELECT 1 FROM album_variants o
                        WHERE o.directory_path = v.path AND o.album_id = v.album_id AND o.hqp_endpoint_id IS NULL)
            OR EXISTS (SELECT 1 FROM taken t WHERE t.album_variant_id = v.id)),
    files AS (
        UPDATE media_files mf SET file_path = f.path
          FROM old_files f
         WHERE mf.id = f.id
           AND f.id NOT IN (SELECT id FROM taken)
           AND f.album_variant_id NOT IN (SELECT id FROM stays)
        RETURNING f.old),
    variants AS (
        UPDATE album_variants av SET directory_path = v.path
          FROM old_variants v
         WHERE av.id = v.id AND v.id NOT IN (SELECT id FROM stays)),
    -- a file's own path, or an embedded picture's `<suffix>:<path>#<index>`
    pictures AS (
        UPDATE covers c
           SET source_path = CASE WHEN starts_with(c.source_path, m.old || '/')
                                  THEN m.new || substr(c.source_path, length(m.old) + 1)
                                  ELSE split_part(c.source_path, ':', 1) || ':' || m.new
                                       || substr(c.source_path, length(split_part(c.source_path, ':', 1))
                                                                + length(m.old) + 2) END
          FROM moves m
         WHERE starts_with(c.source_path, m.old || '/')
            OR (c.source_type = 'embedded'
                AND starts_with(c.source_path, split_part(c.source_path, ':', 1) || ':' || m.old || '/'))),
    places AS (
        SELECT r.id, m.new || substr(r.path, length(m.old) + 1) AS path,
               EXISTS (SELECT 1 FROM library_folders o
                        WHERE o.path = m.new || substr(r.path, length(m.old) + 1)) AS there
          FROM library_folders r JOIN moves m ON r.path = m.old OR starts_with(r.path, m.old || '/')),
    places_moved AS (
        UPDATE library_folders r SET path = p.path FROM places p WHERE r.id = p.id AND NOT p.there),
    places_dropped AS (
        DELETE FROM library_folders r USING places p WHERE r.id = p.id AND p.there)
    SELECT m.old, m.new, count(f.old) AS files
      FROM moves m LEFT JOIN files f ON f.old = m.old
     GROUP BY m.old, m.new
"""


def _follow_moved_disks() -> None:
    """A folder under an old-shape point that is no root now, while a root
    is the folder disks are mounted in: the disk is mounted there, at its
    UUID (the move block of PI-NODE-2D, then `roots --forget`), and every
    path stored under the old point is rewritten to the new one. Nothing is
    read from the disk, so a disk that is out moves too — its folder is
    then "not connected", which is true. A second start finds no folder
    under the old point: nothing to do."""
    disks = next((root for root in roots() if _disks_folder(root)), None)
    if disks is None:
        return
    given_roots = roots()
    moves = {}
    for row in db_query("SELECT path FROM library_folders"):
        match = OLD_BOOT_MOUNT_POINT.match(row["path"])
        if match and row["path"][match.end():][:1] in ("", "/") and match.group(0) not in given_roots:
            moves[match.group(0)] = match.group(1)
    if not moves:
        return
    for row in db_query(_MOVE_SQL, {"olds": list(moves), "uuids": list(moves.values()), "disks": disks}):
        logger.info("music folder moved with its disk: %d files from %s to %s", row["files"], row["old"], row["new"])


def standing(path: str) -> Dict[str, Any]:
    """Where a folder a page is about to add stands against the library:
    `inside` — the name of the folder it lies in (it is read already),
    `holds` — the names of the folders it would take in."""
    real = stored(path)
    rows = [r for r in given() if r["id"] is not None]
    inside = next((r for r in rows if _under(real, r["path"])), None)
    holds = [r for r in rows if _under(r["path"], real) and _key(r["path"]) != _key(real)]
    return {"inside": folder_name(inside) if inside else None, "holds": [folder_name(r) for r in holds]}


def _admitted(path: str) -> str:
    """The real folder a request names, if this node may add it; else
    Refused, in words for the page."""
    k = kind()
    if k == "setup":
        raise Refused("This node's music folder is named where the node was set up")
    if k == "pick":
        real = within(path)
        if real is None or not os.path.isdir(real):
            raise Refused("That folder is not one this node may read")
        if any(real == root and _disks_folder(root) for root in roots()):
            # where disks appear, not a folder of music: adding it would join every stick by itself
            raise Refused("That is where disks appear, not a folder of music: add a disk, or a folder on one")
        return real
    if not os.path.isabs(path):
        raise Refused("That folder is not one this node may read")
    real = os.path.realpath(path)
    if not os.path.isdir(real):
        raise Refused("That folder is not one this node may read")
    return real


_write = threading.Lock()


def add(path: str) -> Dict[str, Any]:
    """The owner gives the library a folder: stored, its disk's label read.
    A folder inside one of the library's is refused (it is read already); one
    that holds folders of the library takes them in (their rows go, their
    files' paths are absolute and stay). Checked and written under a lock of
    the table. Returns {folder, merged}. The caller arms the folder's watch
    and queues its scan."""
    real = _admitted(path)
    folder = stored(real)
    label = _label_of(real)
    with _write, transaction() as cur:
        cur.execute("LOCK TABLE library_folders IN SHARE ROW EXCLUSIVE MODE")
        row, merged = _place(cur, folder, label)
    invalidate()
    logger.info("music folder added: %s%s", folder,
                f" (it takes in {', '.join(merged)})" if merged else "")
    return {"folder": row, "merged": merged}


def _place(cur, folder: str, label: Optional[str], narrow: bool = False) -> tuple:
    """The one rule a folder enters the table by (the caller holds the
    table's lock): one inside a folder of the library is refused — or, with
    `narrow` (a setup node's .env, which names its folder), that outer row
    becomes this folder —; folders inside it are taken in. Returns (its row,
    the paths taken in)."""
    cur.execute("SELECT id, path, label FROM library_folders")
    rows = [dict(zip(("id", "path", "label"), r)) for r in cur.fetchall()]
    same = next((r for r in rows if _key(r["path"]) == _key(folder)), None)
    if same is not None:
        if narrow:
            return same, []
        raise Refused(f"It is already in your library, as part of {folder_name(same)}.")
    inside = next((r for r in rows if _under(folder, r["path"])), None)
    if inside is not None and not narrow:
        raise Refused(f"It is already in your library, as part of {folder_name(inside)}.")
    gone = [r for r in rows if _under(r["path"], folder) or (narrow and r is inside)]
    if gone:
        cur.execute("DELETE FROM library_folders WHERE id = ANY(%s)", ([r["id"] for r in gone],))
    cur.execute("INSERT INTO library_folders (path, label) VALUES (%s, %s) RETURNING id, path, label",
                (folder, label))
    return dict(zip(("id", "path", "label"), cur.fetchone())), [r["path"] for r in gone]


def _unnest(cur) -> None:
    """No folder inside another, whatever wrote the rows (the re-point of a
    moved disk): the inner goes, the outer reads it."""
    cur.execute("""DELETE FROM library_folders i USING library_folders o
                    WHERE i.id <> o.id AND starts_with(i.path, rtrim(o.path, '/') || '/')
                    RETURNING i.path, o.path""")
    for inner, outer in cur.fetchall():
        logger.info("music folder %s is part of %s: one folder from now on", inner, outer)


def _label_of(real: str) -> Optional[str]:
    """The label of the disk `real` lies on: in the disks' folder its
    automount's (or the setup block's) word, a root that is a disk itself
    the setup's."""
    for root in roots():
        if _disks_folder(root) and real.startswith(root.rstrip("/") + "/"):
            return _label(root, real[len(root.rstrip("/")) + 1:].split("/", 1)[0])
        if real == root or real.startswith(root.rstrip("/") + "/"):
            return _root_labels().get(root)
    return None


def refresh_labels() -> None:
    """A disk plugged in: its folders' rows take the label it carries now
    (renamed on another computer); a disk out keeps the one last read."""
    for folder in given():
        if folder["id"] is None:
            continue
        real = local(folder["path"])
        if not os.path.isdir(real):
            continue
        label = _label_of(real)
        if label is not None and label != folder["label"]:
            db_execute("UPDATE library_folders SET label = %s WHERE id = %s", (label, folder["id"]))
            invalidate()


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
    that type: this one failed), driver (what its mount was tried with, None
    where none was), unclear (`ambivalent` or `unknown` where the automount
    found no one filesystem on the volume — the marks of two, or none —,
    else None), label, since}. A file of another boot (a
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
            driver = lines[3].strip() if len(lines) > 3 else ""
            found.append({"device": name, "format": fmt, "mounts": mounts, "label": label,
                          "driver": driver if driver in _DRIVERS else None,
                          "unclear": fs if fs in _NO_ONE_FILESYSTEM else None,
                          "since": datetime.fromtimestamp(read[1], timezone.utc).isoformat()})
    return found


def _root_labels() -> Dict[str, str]:
    """The labels the setup read for the roots that are disks themselves — a
    mount point a person named, or the setup's own fstab line of before
    2026-11-24, which mounted its disk at the root (nothing leaves a `.names`
    entry for such a disk, and a container cannot ask blkid): root → label, a
    stranger's text. A disk mounted in the disks' folder has its label there."""
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


def _held(root: str) -> bool:
    """Whether the library holds a copy from under `root`: what tells a root
    that lost its music (its disk out, its folder removed) from a folder
    named a moment ago with nothing in it yet. Under roots a copy's stored
    folder is the path this process reads."""
    row = db_query_one("""
        SELECT EXISTS (SELECT 1 FROM album_variants
                        WHERE location = 'local'
                          AND (directory_path = %s OR starts_with(directory_path, %s))) AS held""",
                       (root, root.rstrip("/") + "/"))
    return bool(row["held"])


def _disk_points(mounts: Dict[str, bool]) -> List[tuple]:
    """(path, root, name) of each disk a choice can start at: every one
    mounted in the disks' folder — by the automount or by a line of
    /etc/fstab, alike — and every root that is a disk itself (name None:
    nothing named it)."""
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


def places(under: Optional[str] = None) -> Dict[str, Any]:
    """The folder sheet's listing. With no `under`: where an added folder
    can start — each root that is a folder of music itself (`empty` where
    nothing is in it, and then `held`: the library holds copies from it, so
    its disk is out or the folder was removed — else it is new), each disk
    mounted beneath a root (the disks' folder) or as a root, with its label
    and size — and, last, each volume plugged in that nothing mounted (kind
    "unread": no path, a row to read, never to open). Each place says the
    folder of the library it is or lies in (`in_library`). With `under`:
    that folder's own folders, its audio count, the way back up, the disk
    it lies on (`disk`: its path, label and size; None under a folder named
    at setup) — what a page names the place by, where the path holds a UUID
    — and where it stands against the library (`standing`: the folder it
    lies in, the folders it would take in)."""
    if not picks():
        raise Refused("This node's music folders are not added here")
    if under is None:
        mounts = _mounts()
        labels = _root_labels()
        points = _disk_points(mounts)
        folders_ = given()
        places_ = []

        def in_library(path):
            inside = next((f for f in folders_ if _under(path, f["path"])), None)
            return folder_name(inside) if inside else None
        for root in roots():
            if not os.path.isdir(root):
                continue
            if root in mounts or _disks_folder(root):
                places_.extend({"path": path, "name": point or path, "kind": "disk",
                                "label": _label(root, point) if point else labels.get(path),
                                "size_bytes": _size(path), "read_only": mounts[path],
                                "in_library": in_library(path)}
                               for path, of, point in points if of == root)
            else:
                # a folder named at setup: itself the place
                empty = _gone(root, mounts)
                places_.append({"path": root, "name": root, "kind": "folder", "label": None,
                                "size_bytes": None, "empty": empty, "held": _held(root) if empty else None,
                                "in_library": in_library(root)})
        # A volume nothing mounted is a place that cannot be opened: no path
        places_.extend({"path": None, "name": volume["device"], "kind": "unread", "label": volume["label"],
                        "size_bytes": None, "format": volume["format"], "mounts": volume["mounts"],
                        "driver": volume["driver"], "unclear": volume["unclear"]}
                       for volume in unread())
        return {"under": None, "places": places_}
    real = within(under)
    if real is None or not os.path.isdir(real):
        raise Refused("That folder is not one this node may read")
    parent = os.path.dirname(real)
    try:
        names = _subfolders(real)
    except OSError as e:
        raise Refused(f"That folder could not be read ({e.strerror or e})")
    # Up from a root, or from a disk in the disks' folder, is the list of places
    top = real in roots() or (parent in roots() and _disks_folder(parent))
    on = next(((path, root, point) for path, root, point in _disk_points(_mounts())
               if real == path or real.startswith(path.rstrip("/") + "/")), None)
    return {"under": real,
            "parent": None if top else parent,
            "disk": on and {"path": on[0], "name": on[2], "size_bytes": _size(on[0]),
                            "label": _label(on[1], on[2]) if on[2] else _root_labels().get(on[0])},
            "folders": [{"name": n, "path": os.path.join(real, n)} for n in names],
            "standing": standing(real),
            **_count_audio(real)}


# A disk plugged in that the library never had (LIBRARY-FOLDERS-1 §3.2): its
# audio counted once a mount — keyed by the point and the device the mount
# gave it, so a disk plugged in again is counted afresh — on the mount
# watch's own thread, never on a page's read.
_counted: Dict[tuple, Dict[str, Any]] = {}
_counted_lock = threading.Lock()


def _device(path: str) -> Optional[int]:
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def _new_points() -> List[tuple]:
    """(path, root, point) of each disk mounted in the disks' folder that is
    no folder of the library, lies in none and holds none."""
    folders_ = paths()
    return [(path, root, point) for path, root, point in _disk_points(_mounts())
            if point is not None and not any(_under(path, f) or _under(f, path) for f in folders_)]


def count_new_disks() -> bool:
    """Count the audio of every disk plugged in that the library never had,
    once a mount. Returns whether a count changed what new_disks says."""
    if not picks():
        return False
    with _counting:
        return _count_new_disks()


# one count at a time: the start's and the mount watch's never cross
_counting = threading.Lock()


def _count_new_disks() -> bool:
    changed = False
    points = _new_points()
    keys = {(path, _device(path)) for path, _root, _point in points}
    for path, _root, _point in points:
        key = (path, _device(path))
        with _counted_lock:
            if key in _counted:
                continue
        count = _count_audio(path)
        with _counted_lock:
            _counted[key] = count
        changed = changed or count["audio_files"] > 0
    with _counted_lock:
        for key in [k for k in _counted if k not in keys]:
            changed = changed or _counted.pop(key)["audio_files"] > 0
    return changed


def new_disks() -> List[Dict[str, Any]]:
    """The disks plugged in that the library never had and that hold music
    (the counts count_new_disks made): {path, uuid, label, size_bytes,
    audio_files, capped}. A disk with no audio file is not offered: a stick
    of documents says nothing. Nothing of a disk passed over is stored — it
    is offered again at its next plug (the owner, Q8)."""
    if not picks():
        return []
    found = []
    for path, root, point in _new_points():
        with _counted_lock:
            count = _counted.get((path, _device(path)))
        if count and count["audio_files"]:
            found.append({"path": path, "uuid": point, "label": _label(root, point),
                          "size_bytes": _size(path), **count})
    return found


def _disks_folder(root: str) -> bool:
    """`root` is the folder disks are mounted in (it carries the labels'
    folder, which the automount's install and the setup's fstab block both
    make): the place disks appear in, not itself a folder of music."""
    return os.path.isdir(os.path.join(root, NAMES_DIR))
