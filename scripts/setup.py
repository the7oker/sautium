#!/usr/bin/env python3
"""Sautium on a Linux machine, from a clone: the console setup (`./sautium`).

    ./sautium            set the node up, or go on where a run stopped (it moves
                         the folder to a release only before the host agent is
                         installed, and never to one older than its own; after
                         that, releases are the host agent's: ./sautium update)
    ./sautium status     what is installed, what runs, what is mounted
    ./sautium roots      the folders it may read music from; --forget <folder>
    ./sautium pair       a code and its QR to sign another device in
    ./sautium update     ask the host agent for the newest signed release
    ./sautium remove     take Sautium off this machine

One file, the standard library, Python 3.9's language, run as the person who
owns the checkout — never root, and it never calls sudo: a step that needs
root is PRINTED, whole, with this machine's own values in it, for the person
to paste into a terminal that may; the setup then checks the result and goes
on. It asks nothing about the node: how much the machine does is read off the
machine and shown, and everything else — the account, the music folder, the
assistant, the output — is the Web UI's (PI-NODE-2B §5).

What it reads that a container wrote (the setup code, the API secret) it
reads as a stranger's bytes: without following a link, a regular file only,
bounded, and used only in the form it must have.

The .env's reading and writing are the host agent's (scripts/host_agent.py,
loaded from beside this file): one parser of that file, not two.
"""

import sys

if sys.version_info < (3, 9):       # before anything a later Python alone has (os.pidfd_open)
    sys.exit("./sautium needs Python 3.9 or newer; this is " + sys.version.split()[0])

import argparse
import hashlib
import hmac
import importlib.util
import ipaddress
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = "docker-compose.pi.yml"
ROOTS_OVERRIDE = "host/compose.roots.yml"
AUTOMOUNT_BASE = "/media/sautium"
NO_LIBRARY = "./data/no-library"
NODE_API = "http://127.0.0.1:8800"
WEB_PORT = 8800
# What the node binds on the host's network (docker-compose.pi.yml): the Web
# UI, the media proxy, the DLNA events, the peer surface, the DHT
TCP_PORTS = (8800, 8830, 8831, 8801)
UDP_PORTS = (19001,)
DOCKER_MIN, COMPOSE_MIN = (25, 0), (2, 24)
# A checkout on these cannot be private to its owner, and the node's user is
# taken from the checkout's owner
FOREIGN_FILESYSTEMS = {"exfat", "vfat", "msdos", "ntfs", "ntfs3", "fuseblk"}
# Free space Docker's data root needs for what the preset builds and keeps
NEED_GB = {"nano": 8, None: 25}
SMALL_FILE = 4096
# The version a tree states, as desktop/updater.py reads it
STATED_VERSION = re.compile(r'^VERSION = "(\d+)\.(\d+)\.(\d+)"$', re.M)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agent = _load("sautium_host_agent", ROOT / "scripts" / "host_agent.py")
machine_tier = _load("sautium_machine_tier", ROOT / "desktop" / "machine_tier.py")


class Stop(Exception):
    """The setup cannot go on: one sentence, and what to do."""


def say(text: str = "") -> None:
    print(text, flush=True)


def run(args: List[str], *, timeout: int = 120, check: bool = False, cwd: Path = None,
        stdin: bytes = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, cwd=str(cwd or ROOT), input=stdin, capture_output=True,
                              timeout=timeout, check=check)
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, 127, b"", f"{args[0]}: not found".encode())
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, b"", f"{args[0]}: no answer in {timeout} s".encode())


def printable(text: str) -> str:
    """A tool's line as the terminal may show it: no control character."""
    return "".join(c if c.isprintable() else " " for c in text)[:400]


def out(result: subprocess.CompletedProcess) -> str:
    return result.stdout.decode("utf-8", "replace").strip()


# -- the machine -----------------------------------------------------------------------

def mem_total_kib(meminfo: str) -> int:
    for line in meminfo.splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1])
    raise Stop("this machine's memory could not be read (/proc/meminfo has no MemTotal)")


def version_tuple(text: str) -> Tuple[int, ...]:
    """The leading numbers of a version, as far as they go."""
    match = re.match(r"v?(\d+)(?:\.(\d+))?", text.strip())
    return tuple(int(g) for g in match.groups(default="0")) if match else (0, 0)


def device_chain(source: str, lsblk_json: str) -> List[dict]:
    """lsblk's own tree (its JSON) walked from the whole disk down to the
    block device `source` — a mount's source as findmnt names it, a swap's
    device; empty where it is no block device lsblk lists."""
    name = os.path.basename(re.sub(r"\[.*\]$", "", source))     # btrfs names a subvolume after it

    def walk(nodes, above):
        for node in nodes or []:
            here = above + [node]
            if node.get("name") == name:
                return here
            found = walk(node.get("children"), here)
            if found:
                return found
        return None
    try:
        return walk(json.loads(lsblk_json).get("blockdevices"), []) or []
    except (ValueError, AttributeError):
        return []


def mount_of(path: str) -> Dict[str, str]:
    """{source, fstype, target} of the mount holding `path` (findmnt's JSON)."""
    result = run(["findmnt", "-J", "-o", "SOURCE,FSTYPE,TARGET", "-T", path])
    try:
        return json.loads(out(result))["filesystems"][0]
    except (ValueError, KeyError, IndexError):
        return {}


def read_line(path: str) -> str:
    """A small file of the kernel's (sysfs, procfs): its text, "" where it
    cannot be read."""
    try:
        with open(path, "rb") as f:
            return f.read(SMALL_FILE).decode("utf-8", "replace").strip()
    except OSError:
        return ""


def file_size(path: str) -> Optional[int]:
    try:
        return os.stat(path).st_size
    except OSError:
        return None


def swaps(proc_swaps: str, lsblk_json: str, sysfs: str = "/sys/block",
          source_of=lambda path: mount_of(path).get("source", ""), size_of=file_size) -> List[dict]:
    """Where this machine swaps to, from /proc/swaps and sysfs alone: each
    swap as {path, kind: file | partition | zram, medium} — and for a zram
    device with a backing file (Raspberry Pi OS writes idle pages of its
    compressed swap to one, which /proc/swaps does not show) `writeback`,
    the file's path, its `medium`, its `size_bytes` (None where it cannot be
    read) and `written_mb`, what the kernel counted as written there since
    the machine started."""
    def medium(device: str) -> str:
        return machine_tier.storage_medium(device_chain(device, lsblk_json), sysfs)

    found = []
    for line in proc_swaps.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 2:
            continue
        # The kernel writes a space in a path as \040
        path = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[0])
        name = os.path.basename(path)
        if re.fullmatch(r"zram\d+", name):
            swap = {"path": path, "kind": "zram", "medium": "memory"}
            backing = read_line(f"{sysfs}/{name}/backing_dev")
            if backing not in ("", "none"):
                loop = os.path.basename(backing)
                file = read_line(f"{sysfs}/{loop}/loop/backing_file") if loop.startswith("loop") else ""
                stat = read_line(f"{sysfs}/{name}/bd_stat").split()
                swap["writeback"] = {
                    "path": file or backing, "medium": medium(source_of(file) if file else backing),
                    "size_bytes": size_of(file) if file else None,
                    # bd_stat's third figure: 4 KiB units written to the backing device
                    "written_mb": int(stat[2]) * 4 / 1024 if len(stat) == 3 and stat[2].isdigit() else None}
            found.append(swap)
        elif fields[1] == "file":
            found.append({"path": path, "kind": "file", "medium": medium(source_of(path))})
        else:
            found.append({"path": path, "kind": "partition", "medium": medium(path)})
    return found


def machine() -> dict:
    """What the setup reads of this machine, once."""
    with open("/proc/meminfo") as f:
        ram_gib = mem_total_kib(f.read()) / 1024 ** 2
    docker_root = out(run(["docker", "info", "--format", "{{.DockerRootDir}}"])) or "/var/lib/docker"
    where = docker_root if os.path.isdir(docker_root) else os.path.dirname(docker_root) or "/"
    holding = mount_of(where)
    lsblk = out(run(["lsblk", "-J", "-o", "NAME,RM,TRAN,ROTA"]))
    try:
        free_gb = shutil.disk_usage(where).free / 1e9
    except OSError:
        free_gb = None
    return {"ram_gib": ram_gib, "cores": machine_tier.machine_cores(), "arch": platform.machine(),
            "docker_root": docker_root, "docker_root_free_gb": free_gb,
            "docker_root_medium": machine_tier.storage_medium(device_chain(holding.get("source", ""), lsblk)),
            "swaps": swaps(read_line("/proc/swaps"), lsblk)}


def machine_line(m: dict, preset: Optional[str]) -> str:
    """The machine as read, and what the node is. The arrow is the machine's
    own verdict; a node that is something else — asked for with --preset, or
    born before the machine changed — is said as that, not as read. The
    medium is Docker's data root's, named as read: a USB bridge that reports
    a stick as a fixed disk is read wrong, and --preset is the answer."""
    read = machine_tier.birth_preset(m["ram_gib"], m["docker_root_medium"], m["docker_root_free_gb"])[0]
    name = "Nano" if preset == "nano" else "the full node"
    said = f" → {name}" if read == preset else f"; set up as {name}"
    return (f"{round(m['ram_gib'])} GB RAM, {m['cores']} cores, "
            f"{machine_tier.MEDIA[m['docker_root_medium']]}{said}")


# -- the checks ------------------------------------------------------------------------

def refuse_root() -> None:
    if os.geteuid() == 0:
        raise Stop("run ./sautium as the person who owns this folder, not as root: the node runs as "
                   "that person, and the setup prints every step that needs root for you to run")


def check_checkout() -> None:
    if ROOT.stat().st_uid != os.getuid():
        raise Stop(f"{ROOT} is not this user's folder: run ./sautium as its owner")
    if agent._UNSAFE_PATH.search(str(ROOT)):
        raise Stop(f"the folder's path {ROOT} has a character the setup cannot carry: put Sautium at "
                   "a plain path (letters, digits, . _ - /)")
    fstype = mount_of(str(ROOT)).get("fstype", "")
    if fstype in FOREIGN_FILESYSTEMS:
        raise Stop(f"this folder is on a {fstype} disk, where files cannot be private to you: put the "
                   "Sautium folder on the system disk")
    if run(["git", "--version"]).returncode != 0:
        raise Stop("git is not installed: install it (sudo apt install git) and run ./sautium again")
    changed = out(run(["git", "status", "--porcelain", "--untracked-files=no"]))
    if changed:
        raise Stop("this folder has local changes to Sautium's own files, and an update never moves a "
                   "tree someone edits: undo them (git checkout -- .) and run ./sautium again")


def check_machine() -> None:
    arch = platform.machine()
    if arch not in ("aarch64", "arm64", "x86_64", "amd64"):
        raise Stop(f"this machine is {arch}: Sautium's image is built for 64-bit ARM and x86-64")
    if out(run(["getconf", "LONG_BIT"])) == "32":
        raise Stop("this system is 32-bit: install the 64-bit system (Raspberry Pi OS 64-bit) and "
                   "run ./sautium again")


def docker_state() -> Tuple[str, str]:
    """("ok" | "absent" | "denied" | "old" | "compose", a sentence)"""
    if shutil.which("docker") is None:
        return "absent", ("Docker is not installed. Install Docker Engine from docs.docker.com/engine/"
                          "install (the page for your system), then run ./sautium again")
    server = run(["docker", "version", "--format", "{{.Server.Version}}"])
    if server.returncode != 0:
        why = server.stderr.decode("utf-8", "replace")
        if "permission denied" in why.lower():
            return "denied", "Docker is installed, and this user may not use it yet"
        return "absent", f"Docker does not answer ({why.strip().splitlines()[-1][:160] if why.strip() else 'no answer'}): start it and run ./sautium again"
    if version_tuple(out(server)) < DOCKER_MIN:
        return "old", f"Docker Engine {out(server)} is older than 25: update Docker and run ./sautium again"
    compose = run(["docker", "compose", "version", "--short"])
    if compose.returncode != 0 or version_tuple(out(compose)) < COMPOSE_MIN:
        return "compose", ("Docker's compose plugin is missing or older than 2.24: install "
                           "docker-compose-plugin and run ./sautium again")
    return "ok", f"Docker {out(server)}, Compose {out(compose)}"


def node_running() -> bool:
    return bool(out(run(agent.compose_cmd(ROOT) + ["ps", "-q", "--status", "running", "backend"])))


def ports_taken() -> List[str]:
    """The node's ports another program holds now."""
    taken = []
    for kind, ports in ((socket.SOCK_STREAM, TCP_PORTS), (socket.SOCK_DGRAM, UDP_PORTS)):
        for port in ports:
            with socket.socket(socket.AF_INET, kind) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    s.bind(("0.0.0.0", port))
                except OSError:
                    taken.append(f"{port}/{'tcp' if kind == socket.SOCK_STREAM else 'udp'}")
    return taken


def undervoltage_now() -> Optional[bool]:
    """A Raspberry Pi's supply under its voltage at this moment; None where
    the kernel has no such flag."""
    for device in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        try:
            if (device / "name").read_text().strip() == "rpi_volt":
                return (device / "in0_lcrit_alarm").read_text().strip() == "1"
        except OSError:
            continue
    return None


# -- root steps: printed, never run ----------------------------------------------------

def block(title: str, why: str, commands: List[str], undo: List[str]) -> None:
    """One root step as the person pastes it: commands only, whole, this
    machine's values in them — and its undo beside it."""
    say()
    say(f"── {title} " + "─" * max(4, 70 - len(title)))
    say(why)
    say()
    say("Paste into a terminal that may use sudo:")
    say()
    for line in commands:
        say(f"    {line}")
    say()
    say("To undo it later:")
    say()
    for line in undo:
        say(f"    {line}")
    say()


def lingering() -> bool:
    user = os.environ.get("USER") or str(os.getuid())
    return out(run(["loginctl", "show-user", user, "-p", "Linger"])).endswith("=yes")


AUTOMOUNT_RULE = """\
# Sautium: a plugged disk is mounted read-only for the music library
# (written by ./sautium; installed by the person, as root).
# USB storage and the card slot; the script leaves the system's own medium
# and anything already mounted alone. Every event but a removal: a card put
# into a reader that was already plugged in is a "change", never an "add".
ACTION!="remove", SUBSYSTEM=="block", SUBSYSTEMS=="usb", ENV{ID_FS_USAGE}=="filesystem", TAG+="systemd", ENV{SYSTEMD_WANTS}+="sautium-automount@%k.service"
ACTION!="remove", SUBSYSTEM=="block", KERNEL=="mmcblk[0-9]*", ENV{ID_FS_USAGE}=="filesystem", TAG+="systemd", ENV{SYSTEMD_WANTS}+="sautium-automount@%k.service"
# A volume the system names no filesystem on at all (none found, or the marks
# of two: a quick format over an older one) is the script's to say; one that
# is named and is no filesystem of files (swap, an encrypted volume) is not.
# No size: a card reader's empty slot. Never an optical drive: an empty tray
# and an audio CD are no volume a format was lost on.
ACTION!="remove", SUBSYSTEM=="block", SUBSYSTEMS=="usb", KERNEL!="sr[0-9]*", ENV{ID_FS_USAGE}!="?*", ATTR{size}!="0", TAG+="systemd", ENV{SYSTEMD_WANTS}+="sautium-automount@%k.service"
ACTION!="remove", SUBSYSTEM=="block", KERNEL=="mmcblk[0-9]*", ENV{ID_FS_USAGE}!="?*", ATTR{size}!="0", TAG+="systemd", ENV{SYSTEMD_WANTS}+="sautium-automount@%k.service"
# A USB card reader keeps its device when the card is pulled, with no size.
# systemd is told the device is not there then (as it is told of an optical
# drive with no disc), so the unit bound to it stops with the card — what it
# mounted is unmounted, what it said of a card is taken back — and the next
# card starts it afresh. The state follows the medium, not the reader.
# Only a device whose medium can leave it, by the kernel's own word
# (removable): a fixed disk that reports no size for a moment — a
# revalidation after a reset — is a disk that is still there, and nothing
# mounted from it is to be taken away. An optical drive is systemd's own.
ACTION!="remove", SUBSYSTEM=="block", SUBSYSTEMS=="usb", KERNEL!="sr[0-9]*", ATTR{removable}=="1", ATTR{size}=="0", ENV{SYSTEMD_READY}="0"
"""

AUTOMOUNT_UNIT = """\
# Sautium: one plugged disk, mounted read-only while it is there
# (written by ./sautium; installed by the person, as root). Bound to the
# disk's device: it stops when the device goes — a disk pulled, or a card
# pulled from a reader that stays (the rule says such a device is not there).
[Unit]
Description=Sautium: a plugged disk, mounted read-only (%I)
BindsTo=dev-%i.device
After=dev-%i.device

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/lib/sautium/automount mount %I
ExecStop=/usr/local/lib/sautium/automount unmount %I
"""

# The one program of Sautium's that runs as root, and only when a disk is
# plugged in or pulled. It mounts read-only, never writes to the disk, and
# takes nothing from the disk into a command but its UUID, checked first.
# What it writes for the node is under its base folder, which the node has
# read-only: a mounted volume's label (.names/<mount point>) and, for a
# volume it did not mount, .unread/<kernel name> — the boot's id, the
# filesystem's type as blkid names it, the label, the driver that was tried
# (so that "NTFS, and no ntfs-3g here" is told from "ntfs-3g refused it"),
# each cut here and read as a stranger's text there
# (backend/library_root.py). The unit stays active for such a volume, so
# that its stop at the pull takes the file away. Where blkid names no
# filesystem the type line is one of the script's own two words:
# `ambivalent` (wipefs lists the marks of more than one), `unknown` (it lists
# none; said only of a volume that can be one of files, see the script).
# Never by `blkid -p`'s exit: 8 for two marks on an older util-linux, 2 for
# the same disk on 2.41 (Debian trixie), as for a blank one.
AUTOMOUNT_SCRIPT = """\
#!/bin/sh
# Sautium: mount a plugged disk read-only under {base}/<its UUID>, for the
# music library to read (written by ./sautium; installed by the person, as
# root, at /usr/local/lib/sautium/automount). Called by the unit
# sautium-automount@<kernel name>.service with "mount" or "unmount".
set -eu
BASE={base}
OWNER={uid}:{gid}
STATE={state}
SYSFS={sysfs}
verb="${{1:-}}"
name="${{2:-}}"
case "$name" in ""|*[!A-Za-z0-9]*) exit 64 ;; esac     # a kernel name, nothing else
dev="/dev/$name"

# A volume that is not mounted here is said to Sautium, and why to the
# journal: $1 the reason, $2 the driver that was tried ("-": none was). Four
# lines — the boot's id, the filesystem as blkid names it, the label, the
# driver — each one line whatever the disk says of itself.
unread() {{
    mkdir -p "$BASE/.unread"
    said=$(printf '%s' "$type" | head -c 16)
    case "$said" in ""|*[!a-z0-9_]*) said=unknown ;; esac
    label=$(blkid -o value -s LABEL "$dev" 2>/dev/null | head -c 120 | tr -d '\\n' || true)
    {{
        cat /proc/sys/kernel/random/boot_id
        printf '%s\\n%s\\n%s\\n' "$said" "$label" "$2"
    }} > "$BASE/.unread/.$name.tmp"
    mv -f "$BASE/.unread/.$name.tmp" "$BASE/.unread/$name"
    echo "sautium-automount: $name ($said) is not mounted: $1" >&2
    exit 0
}}

if [ "$verb" = unmount ]; then
    rm -f "${{BASE:?}}/.unread/$name" "${{BASE:?}}/.unread/.$name.tmp"
    [ -r "$STATE/$name" ] || exit 0
    point=$(cat "$STATE/$name")
    case "$point" in ""|*[!A-Za-z0-9-]*) exit 64 ;; esac
    umount -l "${{BASE:?}}/${{point:?}}" 2>/dev/null || true
    rmdir "${{BASE:?}}/${{point:?}}" 2>/dev/null || true
    rm -f "${{BASE:?}}/.names/${{point:?}}" "${{STATE:?}}/$name"
    exit 0
fi
[ "$verb" = mount ] || exit 64
[ -b "$dev" ] || exit 0
rm -f "${{BASE:?}}/.unread/$name"      # what a start cut short left under this name

# Never the medium the system runs from, never what something else mounted
# or a line of /etc/fstab mounts (the setup's own line mounts its disk at the plug)
root_src=$(findmnt -n -o SOURCE / || true)
root_disk=$(lsblk -n -o PKNAME "$root_src" 2>/dev/null | head -n 1 || true)
dev_disk=$(lsblk -n -o PKNAME "$dev" 2>/dev/null | head -n 1 || true)
[ -n "$dev_disk" ] || dev_disk="$name"
if [ -n "$root_disk" ] && [ "$dev_disk" = "$root_disk" ]; then exit 0; fi
if [ "$dev" = "$root_src" ]; then exit 0; fi
if findmnt -n -S "$dev" >/dev/null 2>&1; then exit 0; fi
if findmnt --fstab -n -S "$dev" >/dev/null 2>&1; then exit 0; fi
# A whole disk that holds partitions is not a volume, whatever signature an
# earlier format left on it (lsblk's own tree: blkid reads a FAT volume's
# boot sector as a partition table)
if [ "$(lsblk -n -o NAME "$dev" 2>/dev/null | wc -l)" -gt 1 ]; then exit 0; fi

type=$(blkid -o value -s TYPE "$dev" || true)
uuid=$(blkid -o value -s UUID "$dev" || true)
if [ -z "$type" ]; then
    # No filesystem the system names. The person plugged a disk in and is
    # owed why it shows nothing — but never for what is no volume of files:
    # an eMMC's own areas; an empty reader slot and an extended partition's
    # container (under 1 MiB)
    case "$name" in mmcblk*boot*|mmcblk*rpmb) exit 0 ;; esac
    size=$(lsblk -n -d -b -o SIZE "$dev" 2>/dev/null | head -n 1 | tr -d ' ' || true)
    case "$size" in ""|*[!0-9]*) exit 0 ;; esac
    [ "$size" -ge 1048576 ] || exit 0
    # What is on it, as wipefs lists it (read only): a filesystem once for
    # each offset it is found at, and a partition table's signature, which a
    # FAT or NTFS boot sector carries too — no filesystem (libblkid's
    # partition probers; `minix` names a filesystem as well, so it counts)
    if ! listed=$(wipefs --no-act --noheadings --parsable --output TYPE "$dev"); then
        echo "sautium-automount: $name: no filesystem named, and wipefs could not list its signatures" >&2
        exit 0
    fi
    marks=$(printf '%s\\n' "$listed" | grep -v -x -e '' -e aix -e atari -e bsd -e dos -e gpt -e mac -e PMBR \\
            -e sgi -e solaris -e sun -e ultrix -e unixware | sort -u)
    count=0
    [ -z "$marks" ] || count=$(printf '%s\\n' "$marks" | wc -l)
    case "$count" in
        0)  ;;
        1)  echo "sautium-automount: $name: no filesystem named, and wipefs finds $marks" >&2
            exit 0 ;;
        *)  type=ambivalent
            unread "it carries the marks of more than one filesystem (wipefs lists them)" - ;;
    esac
    # None found. Beside other partitions, only one whose type is for data:
    # a disk Windows partitioned carries a reserved partition with nothing on
    # it next to its NTFS one, and others a boot or recovery partition —
    # nobody put music there. A disk's only volume is said whatever its type.
    # The type is the partition table's, by partx: udev exports none for a
    # partition its probe found no filesystem on, which this one is.
    if [ "$dev_disk" != "$name" ] && [ "$(lsblk -n -o NAME "/dev/$dev_disk" 2>/dev/null | wc -l)" -gt 2 ]; then
        number=$(cat "$SYSFS/class/block/$name/partition" 2>/dev/null || true)
        case "$number" in ""|*[!0-9]*) exit 0 ;; esac
        if ! parttype=$(partx -g -o TYPE -n "$number" "/dev/$dev_disk"); then
            echo "sautium-automount: $name: no filesystem named, and partx could not read its partition type" >&2
            exit 0
        fi
        parttype=$(printf '%s' "$parttype" | head -n 1 | tr -d ' ' | tr 'A-F' 'a-f')
        case "$parttype" in
            ebd0a0a2-b9e5-4433-87c0-68b6b72699c7) ;;    # GPT: Microsoft basic data (FAT, exFAT, NTFS)
            0fc63daf-8483-4772-8e79-3d69d8477de4) ;;    # Linux filesystem
            933ac7e1-2eb4-4f13-b844-0e14e2aef915) ;;    # Linux /home
            48465300-0000-11aa-aa11-00306543ecac) ;;    # Apple HFS+
            7c3457ef-0000-11aa-aa11-00306543ecac) ;;    # Apple APFS
            0x1|0x4|0x6|0x7|0xb|0xc|0xe|0x83|0xaf) ;;   # MBR: FAT, NTFS and exFAT, Linux, HFS
            *) exit 0 ;;
        esac
    fi
    unread "no filesystem is found on it" -
fi
safe="ro,nosuid,nodev,noexec"
owned="uid=${{OWNER%%:*}},gid=${{OWNER##*:}},fmask=0133,dmask=0022"
# FAT keeps its stamps in local time: pinned to UTC, a file's stamp is the
# same number at every mount, whatever this machine's zone or summer time.
# NTFS by ntfs-3g where it is installed: the kernel's ntfs3 refuses a stick
# as Windows formats one (seen on 6.12 and later), and what ntfs-3g refuses
# is said, never handed to a second parser
case "$type" in
    vfat)  fs=vfat;  opts="$safe,$owned,tz=UTC" ;;
    exfat) fs=exfat; opts="$safe,$owned,time_offset=0" ;;
    ntfs)  if command -v ntfs-3g >/dev/null 2>&1; then fs=ntfs-3g; else fs=ntfs3; fi
           opts="$safe,$owned" ;;
    ext4)  fs=ext4;  opts="$safe,noload" ;;
    swap|crypto_LUKS|LVM2_member|linux_raid_member|zfs_member) exit 0 ;;   # no filesystem of files
    *) unread "not a format this script mounts (FAT, exFAT, NTFS, ext4)" - ;;
esac
case "$uuid" in ""|*[!A-Za-z0-9-]*) unread "it has no UUID to name its mount point by" - ;; esac

point="$uuid"
if findmnt -n "$BASE/$point" >/dev/null 2>&1; then point="$uuid-$name"; fi   # a second volume claiming the UUID
mkdir -p "$BASE/.names" "$STATE"
mkdir -p "$BASE/$point"
# The volume's label, a stranger's text, as a file the node reads as text —
# before the mount, whose change of the mount table is what wakes the node
blkid -o value -s LABEL "$dev" 2>/dev/null | head -c 120 > "$BASE/.names/$point" || true
if ! mount -t "$fs" -o "$opts" "$dev" "$BASE/$point"; then
    # An exFAT driver that does not know time_offset: mounted without it, said
    if [ "$fs" = exfat ] && mount -t exfat -o "$safe,$owned" "$dev" "$BASE/$point"; then
        echo "sautium-automount: $name mounted without time_offset (this kernel's exfat refused it)" >&2
    else
        rm -f "${{BASE:?}}/.names/${{point:?}}"
        rmdir "${{BASE:?}}/${{point:?}}" 2>/dev/null || true
        unread "mount -t $fs refused it" "$fs"
    fi
fi
printf '%s\\n' "$point" > "$STATE/$name"
"""


def automount_files(folder: Path, uid: int, gid: int, base: str = AUTOMOUNT_BASE,
                    state: str = "/run/sautium-automount", sysfs: str = "/sys") -> Dict[str, Path]:
    """The automount's three files, written into ./host/automount for the
    person to read and install."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    files = {"rule": folder / "99-sautium-automount.rules",
             "unit": folder / "sautium-automount@.service",
             "script": folder / "automount"}
    agent._write_private(files["rule"], AUTOMOUNT_RULE.encode(), 0o644)
    agent._write_private(files["unit"], AUTOMOUNT_UNIT.encode(), 0o644)
    agent._write_private(files["script"],
                         AUTOMOUNT_SCRIPT.format(base=shlex.quote(base), uid=uid, gid=gid,
                                                 state=shlex.quote(state),
                                                 sysfs=shlex.quote(sysfs)).encode(), 0o755)
    return files


# Where the person's paste puts the three files; world-readable there
AUTOMOUNT_PLACES = {"rule": "/etc/udev/rules.d/99-sautium-automount.rules",
                    "unit": "/etc/systemd/system/sautium-automount@.service",
                    "script": "/usr/local/lib/sautium/automount"}


def automount_installed() -> bool:
    return (os.path.exists(AUTOMOUNT_PLACES["rule"]) and os.path.exists(AUTOMOUNT_PLACES["script"])
            and os.path.isdir(AUTOMOUNT_BASE))


def automount_stale(files: Dict[str, Path], places: Dict[str, str] = None) -> bool:
    """Whether an installed automount is not the one this setup writes: any
    of the three files differs from its bytes here (an earlier release's
    script knows no ntfs-3g, or says nothing of a disk it could not mount or
    of one it finds no one filesystem on)."""
    def same(kind: str) -> bool:
        try:
            with open((places or AUTOMOUNT_PLACES)[kind], "rb") as f:
                return f.read(65536) == files[kind].read_bytes()
        except OSError:
            return False
    return not all(same(kind) for kind in files)


# What takes the automount back: one list, printed beside the block and by `remove`
AUTOMOUNT_UNDO = ["sudo rm -f /etc/udev/rules.d/99-sautium-automount.rules",
                  "sudo rm -f /etc/systemd/system/sautium-automount@.service",
                  "sudo rm -f /usr/local/lib/sautium/automount",
                  "sudo systemctl daemon-reload",
                  "sudo udevadm control --reload"]


def automount_block(files: Dict[str, Path], update: bool = False) -> None:
    # Short lines: a line a terminal or a chat wrapped arrives as two commands
    # (seen: `install` without its destination). The folders go in variables.
    title, why = ("Plugged disks (optional)",
                  "So that a USB disk or a card with music appears in Sautium when it is plugged in, three small\n"
                  "files go into the system. They mount a plugged disk read-only and never write to it.\n")
    if update:
        title, why = ("Plugged disks: an update of the automount",
                      "The three small files that mount a plugged disk are an earlier release's. This release's\n"
                      "open a disk formatted by Windows where the old ones could not, and tell Sautium of a disk\n"
                      "they could not open or tell the format of. A disk that is plugged in now stays as it is\n"
                      "until it is plugged in again.\n")
    block(title, why + f"Read them first: {files['rule'].parent}/",
          [f"A={shlex.quote(str(files['rule'].parent))}",
           f"B={AUTOMOUNT_BASE}",
           'sudo install -d -m 0755 "$B" "$B/.names" "$B/.unread"',
           "sudo install -d -m 0755 /usr/local/lib/sautium",
           f'sudo install -m 0755 "$A/{files["script"].name}" /usr/local/lib/sautium/',
           f'sudo install -m 0644 "$A/{files["rule"].name}" /etc/udev/rules.d/',
           f'sudo install -m 0644 "$A/{files["unit"].name}" /etc/systemd/system/',
           "sudo systemctl daemon-reload",
           "sudo udevadm control --reload"],
          AUTOMOUNT_UNDO)


def ntfs_3g() -> bool:
    """Whether the userspace NTFS driver is installed — looked for where
    root's mount finds it, which a person's own PATH may leave out."""
    return shutil.which("ntfs-3g", path=os.environ.get("PATH", "") + ":/usr/sbin:/sbin:/usr/bin:/bin") is not None


# -- the roots -------------------------------------------------------------------------

# What a container has of its own: a root that is one of these, or holds one
OWN_OF_THE_SYSTEM = ("/", "/app", "/home", "/usr", "/var", "/run", "/etc", "/proc", "/sys", "/dev",
                     "/bin", "/sbin", "/lib", "/lib64", "/boot", "/root", "/opt", "/srv", "/tmp")
# Broader than a folder a person would call music
TOO_BROAD = ("/mnt", "/media", "/run/media")


def root_refusal(path: str, checkout: Path = ROOT, docker_root: Optional[str] = None) -> Optional[str]:
    """Why `path` may not be a root the node reads music under, or None. The
    roots are the mount, and the mount is the boundary: nothing that is, or
    holds, a part of the system, Docker's own data or this checkout, and
    nothing as broad as a whole home. The name itself may be any a folder
    has — a space, any alphabet — but for the two characters the roots' list
    and the compose file read as their own (`:` and `$`)."""
    if not os.path.isabs(path):
        return "give the folder's whole path, from /"
    if re.search(r"[:$]", path) or not path.isprintable():
        return "the path has a character the setup cannot carry (a colon, a $ or a control character)"
    real = os.path.realpath(path)
    if re.search(r"[:$]", real) or not real.isprintable():
        return "the folder it leads to has a character the setup cannot carry (a colon, a $ or a control character)"
    if not os.path.isdir(real):
        return "there is no such folder"
    parts = Path(real).parts

    def holds_or_is(own: str) -> bool:
        own_real = os.path.realpath(own)
        return real == own_real or (own_real + "/").startswith(real.rstrip("/") + "/")

    def inside(own: str) -> bool:
        return (real + "/").startswith(os.path.realpath(own).rstrip("/") + "/")

    for own in OWN_OF_THE_SYSTEM + (str(checkout),):
        if holds_or_is(own):
            return f"it is, or holds, {own}: a folder of the system or of Sautium itself, not a folder of music"
    if inside(str(checkout)):
        return "it lies inside the Sautium folder"
    if docker_root and (holds_or_is(docker_root) or inside(docker_root)):
        return (f"it is, holds or lies inside Docker's own data ({docker_root}): every container's files "
                "would be readable there. Name the folder the music is in")
    # /home/<user>; /media/<user> and /run/media/<user>, where a desktop
    # mounts a person's disks — but a disk mounted AT /media/<name> is a disk
    if real in TOO_BROAD or (len(parts) == 3 and parts[1] == "home") or (
            len(parts) == 3 and parts[1] == "media" and real != AUTOMOUNT_BASE and not os.path.ismount(real)) or (
            len(parts) == 4 and parts[1:3] == ("run", "media")):
        return "it is a whole home or every mounted disk at once: name the folder the music is in"
    for system in ("/proc", "/sys", "/dev", "/run", "/etc", "/usr", "/boot", "/bin", "/sbin", "/lib"):
        # A desktop mounts a signed-in person's disks under /run/media/<user>/<disk>
        if inside(system) and not (system == "/run" and len(parts) >= 5 and parts[1:3] == ("run", "media")):
            return f"it lies inside {system}"
    return None


def quoted(text: str) -> str:
    """A string as YAML's double-quoted scalar and JSON both read it: the
    letters themselves, never an escape a YAML reader would not join."""
    return json.dumps(text, ensure_ascii=False)


def cut_label(text: str) -> str:
    """A volume's label as the setup carries it: a stranger's text, its
    first 120 bytes, nothing a terminal or a file would read as control."""
    cut = text.encode("utf-8", "replace")[:120].decode("utf-8", "ignore")
    return "".join(c for c in cut if c.isprintable()).strip()


def root_label(root: str) -> str:
    """The label of the volume mounted AT `root` (a disk a line of /etc/fstab
    mounts there), "" where the root is a plain folder or the volume has
    none. findmnt reads it without root; the node in its container cannot."""
    try:
        mount = json.loads(out(run(["findmnt", "-J", "-o", "TARGET,LABEL", "-T", root])))["filesystems"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return ""
    return cut_label(str(mount.get("label") or "")) if mount.get("target") == root else ""


def roots_override(roots: List[str], labels: Optional[Dict[str, str]] = None) -> str:
    """The compose override that gives the node its roots: each bound
    read-only at its own host path, `rslave` so a disk mounted beneath one
    later reaches the container, and their list in the node's environment.
    Lives in ./host/, which no container mounts: the node cannot widen it.
    A root whose folder is gone must never stop the node's start (Docker
    refuses a bind whose source is not there — at an update, at the machine's
    own boot): `create_host_path` makes an empty folder in its place, and an
    empty root is a library that is away. `labels`: a root's volume label
    where it is a mount point, for the node to show (it cannot ask)."""
    lines = ["# Written by ./sautium: the folders this node may read music from.",
             "# Each is mounted read-only at its own path; change them with",
             "# ./sautium --music <folder> and ./sautium roots --forget <folder>,",
             "# never by hand.",
             "services:", "  backend:", "    environment:",
             f"      SAUTIUM_LIBRARY_ROOTS: {quoted(':'.join(roots))}"]
    known = {root: label for root, label in (labels or {}).items() if root in roots and label}
    if known:
        # JSON inside a YAML string; a `$` is compose's own letter, doubled to mean itself
        text = quoted(json.dumps(known, ensure_ascii=False, sort_keys=True)).replace("$", "$$")
        lines.append(f"      SAUTIUM_LIBRARY_ROOT_LABELS: {text}")
    # The automount's folder before the automount is installed is named and
    # not bound: made by Docker, it would read as an automount half installed
    bound = [r for r in roots if r != AUTOMOUNT_BASE or os.path.isdir(r)]
    if bound:
        lines.append("    volumes:")
    for root in bound:
        lines += ["      - type: bind", f"        source: {quoted(root)}",
                  f"        target: {quoted(root)}", "        read_only: true",
                  "        bind:", "          propagation: rslave", "          create_host_path: true"]
    return "\n".join(lines) + "\n"


def _override_value(override: Path, key: str):
    """One environment value of the override an earlier run wrote, as the
    JSON string it was written as; None where the file or the line is not there."""
    raw = agent._read_bounded(override, 65536) or b""
    match = re.search(rf'^\s*{key}: (".*")\s*$', raw.decode("utf-8", "replace"), re.M)
    try:
        return json.loads(match.group(1)) if match else None
    except ValueError:
        return None


def held_roots(override: Path) -> List[str]:
    """The roots an earlier run wrote: the override file is their record."""
    return [r for r in (_override_value(override, "SAUTIUM_LIBRARY_ROOTS") or "").split(":") if r]


def held_labels(override: Path) -> Dict[str, str]:
    """The labels an earlier run read: kept for a root whose disk is not
    plugged in at this run."""
    try:
        found = json.loads((_override_value(override, "SAUTIUM_LIBRARY_ROOT_LABELS") or "{}").replace("$$", "$"))
    except ValueError:
        return {}
    return {str(k): cut_label(str(v)) for k, v in found.items()} if isinstance(found, dict) else {}


def write_roots(roots: List[str]) -> None:
    """The override for `roots`, each root's label read now or kept from the
    run before."""
    path = ROOT / ROOTS_OVERRIDE
    labels = held_labels(path)
    for root in roots:
        label = root_label(root)
        if label:
            labels[root] = label
    agent._write_private(path, roots_override(roots, labels).encode(), 0o644)


def root_state(root: str, mount_points: List[str]) -> str:
    """"there", "empty" or "gone": a root that is not a folder is gone (the
    node's next start makes an empty one in its place); a folder with
    nothing in it and no disk mounted on it is empty — its disk is not
    plugged in, or what was there was removed. The automount's folder holds
    plugged disks, and none is not a library gone."""
    if not os.path.isdir(root):
        return "gone"
    if root == AUTOMOUNT_BASE or root in mount_points:
        return "there"
    try:
        with os.scandir(root) as entries:
            return "there" if next(entries, None) is not None else "empty"
    except OSError:
        return "there"


def root_lines(roots: List[str], findmnt_rn: str, labels: Dict[str, str],
               stepped: Optional[List[str]] = None, held: Optional[Dict[str, bool]] = None,
               forgotten: Optional[List[str]] = None) -> List[str]:
    """What `status` and `roots` say of each root: there, empty or gone, what
    is mounted on or under it (each mount under the root it lies in — the
    nearest, where one root lies in another), its label. A root that is itself a mount point
    is named as that — the node holds what was mounted there at its start —,
    and `stepped` are the roots the setup has a step for (the line of before
    2026-11-24, whose mount point is the root whether its disk is in or out;
    a disk someone mounted by hand). `held`: for each empty root, whether the
    node's library holds copies from it (held_by_the_node) — one it never
    read is a new folder, not a library that is away; where the node gave no
    answer the root is said as one that may have lost its music. `forgotten`:
    the mount points of the setup's old lines no root names
    (forgotten_old_lines), whose disks never appear in Sautium."""
    mounted = mounts_under(roots, findmnt_rn)
    points = [mount_target(line) for line in mounted]
    owner = [max((r for r in roots if p == r or p.startswith(r.rstrip("/") + "/")), key=len) for p in points]
    lines = []
    for root in roots:
        state = root_state(root, points)
        if root == AUTOMOUNT_BASE and state == "gone":
            lines.append(f"Music root {root}: plugged disks — the automount is not installed (./sautium shows how)")
        elif root != AUTOMOUNT_BASE and (root in points or (OLD_BOOT_MOUNT_POINT.fullmatch(root)
                                                            and root in (stepped or []))):
            lines.append(f"Music root {root}: a disk mounted at the root itself — Sautium sees it only as it was "
                         "when Sautium started" + (" (./sautium shows the step)" if root in (stepped or []) else ""))
        elif state == "there":
            label = f'  "{printable(labels[root])}"' if labels.get(root) else ""
            lines.append(f"Music root {root}: there{label}" + ("  (plugged disks)" if root == AUTOMOUNT_BASE else ""))
        elif state == "empty" and (held or {}).get(root) is False:
            lines.append(f"Music root {root}: empty — nothing is in it yet")
        else:
            lines.append(f"Music root {root}: {state} — its disk is not mounted, or the folder was removed. "
                         f"Its albums show as away. To forget it: ./sautium roots --forget {shlex.quote(root)}")
        lines += ["  mounted: " + printable(line) for line, of in zip(mounted, owner) if of == root]
    return lines + [f"A line of /etc/fstab mounts the disk {OLD_BOOT_MOUNT_POINT.fullmatch(point).group(1)} at "
                    f"{point}, where Sautium does not look: it never appears in Sautium (./sautium shows the step)"
                    for point in forgotten or []]


def held_by_the_node() -> Optional[Dict[str, bool]]:
    """For each root the node lists as an empty folder: whether its library
    holds copies from under it. The node's own answer (the folder sheet's
    listing), read as a stranger's; None where it gave none."""
    asked = signed_get("/api/settings/library/folders")
    places = asked.get("places") if isinstance(asked, dict) else None
    if not isinstance(places, list):
        return None
    return {p["path"]: p["held"] for p in places
            if isinstance(p, dict) and isinstance(p.get("path"), str) and isinstance(p.get("held"), bool)}


def chosen_roots(music: Optional[str], held: List[str], docker_root: Optional[str] = None) -> List[str]:
    """The roots of this install: the folders an earlier run named, a folder
    named now, and — always — the automount's folder, where plugged disks
    appear once the person installed the automount."""
    roots = [r for r in held if r != AUTOMOUNT_BASE]
    if music:
        why = root_refusal(music, docker_root=docker_root)
        if why:
            raise Stop(f"--music {music}: {why}")
        real = os.path.realpath(music)
        inside = next((r for r in roots + [AUTOMOUNT_BASE] if real == r or real.startswith(r.rstrip("/") + "/")), None)
        if inside is None:
            roots.append(real)
        elif real != inside:
            say(f"{music} is inside {inside}, which this node already reads: choose it as the music folder "
                "in Sautium (More › Library).")
    return roots + [AUTOMOUNT_BASE]


def keeps_music_line(held: Dict[str, str]) -> bool:
    """An install whose .env names its music folder (made by hand, before the
    setup) keeps reading it there: no roots, no folder to pick in the Web UI.
    Roots would move every file to another path, and the library would be
    read again from its first file."""
    return held.get("MUSIC_LIBRARY_PATH", "") not in ("", NO_LIBRARY)


def birth_of(held: Dict[str, str], asked: Optional[str], read: Optional[str], volume_exists: bool) -> Optional[str]:
    """The preset this node is, or will be, born with: what its .env holds
    once it lives; before that, --preset when given, else an earlier run's
    line, else what was read off the machine."""
    line = held.get("SAUTIUM_BIRTH_PRESET", "").strip().lower()
    if line not in ("", "nano"):
        raise Stop(f".env's SAUTIUM_BIRTH_PRESET is {printable(line)!r}: the setup knows nano, or no such line")
    if volume_exists:
        return line or None
    if asked:
        return "nano" if asked == "nano" else None
    if "SAUTIUM_BIRTH_PRESET" in held:
        return line or None
    return read


# -- .env ------------------------------------------------------------------------------

def database_volume_exists() -> bool:
    """Whether this node's database was ever born: its volume is there. The
    project's name as compose itself derives it, where the files resolve;
    the folder's name before the first .env exists."""
    project = None
    resolved = run(agent.compose_cmd(ROOT) + ["config", "--format", "json"])
    if resolved.returncode == 0:
        try:
            project = json.loads(out(resolved)).get("name")
        except ValueError:
            project = None
    project = project or re.sub(r"[^a-z0-9_-]", "", ROOT.name.lower())
    names = out(run(["docker", "volume", "ls", "--format", "{{.Name}}",
                     "--filter", f"label=com.docker.compose.project={project}"]))
    return any("postgres" in name for name in names.split())


def env_plan(held: Dict[str, str], *, preset: Optional[str], asked_preset: Optional[str],
             ram_gib: float, assistant: str, volume_exists: bool,
             roots: bool = True) -> Dict[str, Optional[str]]:
    """The keys the setup owns and their values, given what .env holds — None
    for a key that is to have no line (PostgreSQL's, where the compose
    file's own defaults fit this machine's memory). A key
    it does not own is never in the plan. The database's password is made
    only at the volume's birth, and a node that already lives is not born
    again: `preset` (birth_of) is written only before that birth. What the
    IMAGE carries follows the birth at first, what .env holds afterwards,
    and `asked_preset` (--preset) whenever it is given — the way an install
    set up without the models gets them. `roots` false (keeps_music_line):
    the music line and the compose files stay as they are."""
    plan: Dict[str, Optional[str]] = {}
    if "POSTGRES_PASSWORD" not in held and not volume_exists:
        plan["POSTGRES_PASSWORD"] = secrets.token_urlsafe(24)
    plan.update(machine_tier.pg_settings(ram_gib))
    if not volume_exists and (preset or "SAUTIUM_BIRTH_PRESET" in held):
        plan["SAUTIUM_BIRTH_PRESET"] = preset or ""
    plan.update(image_choice(held, preset=preset, asked_preset=asked_preset, assistant=assistant))
    plan["CLAUDE_CODE_ENABLED"] = "true" if assistant == "claude" else "false"
    if "DEFAULT_PROVIDER" not in held:
        plan["DEFAULT_PROVIDER"] = "none"
    if roots:
        plan["COMPOSE_FILE"] = f"{COMPOSE_FILE}:{ROOTS_OVERRIDE}"
        # The base file's own mount needs a folder: an empty one of the
        # node's, while the node reads its music under the roots
        plan["MUSIC_LIBRARY_PATH"] = NO_LIBRARY
    elif "COMPOSE_FILE" not in held:
        plan["COMPOSE_FILE"] = COMPOSE_FILE
    return plan


def image_choice(held: Dict[str, str], *, preset: Optional[str], asked_preset: Optional[str],
                 assistant: str) -> Dict[str, str]:
    """What the image carries, as .env records it: the birth's at first,
    what .env holds afterwards, `asked_preset` (--preset) whenever given."""
    if asked_preset is not None:
        small = asked_preset == "nano"
    elif "SAUTIUM_IMAGE_MODELS" in held:
        small = held["SAUTIUM_IMAGE_MODELS"] == "0"
    else:
        small = preset == "nano"
    return {"SAUTIUM_IMAGE_MODELS": "0" if small else "1", "SAUTIUM_IMAGE_STREAMING": "0" if small else "1",
            "SAUTIUM_IMAGE_ASSISTANT_CLI": "1" if assistant == "claude" else "0"}


def write_env(plan: Dict[str, Optional[str]]) -> List[str]:
    """.env brought to the plan; the keys set. A key the plan gives no line
    (None) is removed where it has one, and that is said here: compose then
    reads its file's own default."""
    path = ROOT / ".env"
    if not path.exists():
        agent._write_private(path, b"# Sautium on this machine. Written by ./sautium; your own lines are kept.\n")
    changed, removed = agent.apply_env(path, plan)
    os.chmod(path, 0o600)
    if removed:
        say(f".env: {', '.join(removed)} removed: this machine's memory fits the compose file's "
            f"default{'s' if len(removed) > 1 else ''}")
    return changed


# -- state, build, start ---------------------------------------------------------------

def state_path() -> Path:
    return agent.host_dir(ROOT) / "setup.state"


def load_state() -> dict:
    raw = agent._read_bounded(state_path(), 65536)
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def save_state(state: dict) -> None:
    agent.host_dir(ROOT).mkdir(mode=0o700, exist_ok=True)
    agent._write_private(state_path(), json.dumps(state, indent=1, sort_keys=True).encode())


def image_key(env: Dict[str, str]) -> str:
    """What decides the image: the commit and the three arguments."""
    args = ",".join(env.get(key, "1") for _, key in agent.IMAGE_ARGS)
    return f"{agent.head(ROOT)}:{args}"


def wait_for(pid: int) -> None:
    """Block until `pid` ends — by its own descriptor, never by looking for
    its name in a process list."""
    try:
        fd = os.pidfd_open(pid)
    except (ProcessLookupError, OSError):
        return
    try:
        import select
        select.select([fd], [], [])
    finally:
        os.close(fd)


def born(pid: int) -> Optional[str]:
    """When the process `pid` started, in the kernel's own ticks since boot
    (/proc/<pid>/stat, the field after the name's last bracket + 20) — what
    tells a build this setup started from another process that has since
    been given its number."""
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as f:
            return f.read(4096).rsplit(b")", 1)[1].split()[19].decode()
    except (OSError, ValueError, IndexError):
        return None


def image_built(state: dict, env: Dict[str, str]) -> bool:
    """Whether the image this commit and .env's choices make is built and
    still there: a run again builds nothing."""
    return state.get("built") == image_key(env) and bool(out(run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", agent.image_of(ROOT)])))


def build_refusal(state: dict, env: Dict[str, str]) -> Optional[str]:
    """Why this machine cannot build the image the run needs; None where it
    can, or where nothing is to be built. Asked before the setup asks the
    person anything of root's."""
    refusal = machine_tier.image_build_refusal(machine_tier.machine_memory_kib())
    if refusal is None or image_built(state, env):
        return None
    return refusal + ": Sautium cannot be set up on it yet"


def build(state: dict, env: Dict[str, str]) -> None:
    """The image, built detached with its log in ./host/: a dropped terminal
    does not end it, and a second run attaches to the same build."""
    key = image_key(env)
    if image_built(state, env):
        return
    host = agent.host_dir(ROOT)
    log, code = host / "setup-build.log", host / "setup-build.exit"
    running = state.get("build") or {}
    if not (running.get("key") == key and running.get("pid") and running.get("born")
            and born(running["pid"]) == running["born"]):
        code.unlink(missing_ok=True)
        command = " ".join(shlex.quote(part) for part in agent.compose_cmd(ROOT) + ["build", "backend"])
        # One retry: a build lost to a passing download failure is common
        script = (f"{command} > {shlex.quote(str(log))} 2>&1 || {command} >> {shlex.quote(str(log))} 2>&1; "
                  f"echo $? > {shlex.quote(str(code))}")
        child = subprocess.Popen(["sh", "-c", script], cwd=str(ROOT), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        running = {"key": key, "pid": child.pid, "born": born(child.pid)}
        state["build"] = running
        save_state(state)
        say(f"Building Sautium's image (its first build compiles Python: most of an hour on a small board; "
            f"its log: {log}) …")
    else:
        say(f"A build is under way (its log: {log}) …")
    wait_for(running["pid"])
    result = (agent._read_bounded(code, 16) or b"").decode().strip()
    state.pop("build", None)
    if result != "0":
        save_state(state)
        tail = (agent._read_bounded(log, 50 * 1024 * 1024) or b"").decode("utf-8", "replace").splitlines()[-12:]
        raise Stop("the image did not build. The last lines of its log:\n    " + "\n    ".join(tail)
                   + f"\nThe whole log: {log}. Run ./sautium again to try once more.")
    state["built"] = key
    save_state(state)


def backend_container() -> str:
    """The backend container's id, running or not; "" where there is none."""
    return out(run(agent.compose_cmd(ROOT) + ["ps", "--all", "-q", "backend"]))


def postgres_remade() -> bool:
    """Whether `up` would make PostgreSQL's container again: its service, as
    the compose files and .env define it now, is not the one its running
    container was made from — by compose's own hash of the service, which it
    keeps as a label on the container. (A PG_* line of .env that moved with
    the machine's memory; a release that changed the service's lines.)"""
    held = out(run(agent.compose_cmd(ROOT) + ["ps", "-q", "--status", "running", "postgres"])).split()
    if not held:
        return False
    wanted = out(run(agent.compose_cmd(ROOT) + ["config", "--hash", "postgres"])).split()
    made_from = out(run(["docker", "inspect", "--format",
                         '{{index .Config.Labels "com.docker.compose.config-hash"}}', held[0]]))
    return len(wanted) == 2 and bool(made_from) and made_from != wanted[1]


def start() -> None:
    say("Starting Sautium (its first start prepares the database: a few minutes) …")
    # `up` makes the backend's container again where its image or its lines
    # changed, and the old one's log goes with it: taken first, and kept only
    # if the container did go — a run that changed nothing must not push an
    # update's kept log out
    before = backend_container()
    kept = agent.take_log(ROOT, "setup") if before else None
    if before and postgres_remade() and node_running():
        # PostgreSQL is stopped by its smart shutdown, which waits for its
        # clients to leave: made again under a running backend it would wait
        # its whole grace out and be killed. The backend goes first, as at
        # the host agent's update, and `up` starts it again.
        stopped = run(agent.compose_cmd(ROOT) + ["stop", "backend"], timeout=120)
        if stopped.returncode != 0:
            said = stopped.stderr.decode("utf-8", "replace").strip().splitlines()
            say("  the node could not be stopped before its database is made again: "
                + (printable(said[-1]) if said else "docker compose said nothing"))
    result = run(agent.compose_cmd(ROOT) + ["up", "-d", "--no-build", "--wait", "--wait-timeout", "2400"],
                 timeout=2500)
    if kept is not None:
        if backend_container() == before:
            kept.unlink()
        else:
            agent.trim_logs(ROOT)
    if result.returncode != 0:
        why = result.stderr.decode("utf-8", "replace").strip().splitlines()[-6:]
        raise Stop("Sautium did not start:\n    " + "\n    ".join(why)
                   + "\nIts log: docker compose logs backend")


# -- the last screen -------------------------------------------------------------------

def own_addresses() -> List[str]:
    """This machine's IPv4 addresses a device on the home network could open:
    the one the default route leaves by first, then every other private one."""
    first = None
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))            # no packet is sent: the kernel names the source
            first = s.getsockname()[0]
        except OSError:
            pass
    return lan_addresses(first, out(run(["ip", "-o", "-4", "addr", "show"])))


def lan_addresses(first: Optional[str], ip_addr: str) -> List[str]:
    """The private addresses of `ip -o -4 addr show` that are a door from
    another device — never a container runtime's bridge, never a tailnet's
    (named apart) — the default route's own first."""
    found = []
    for line in ip_addr.splitlines():
        match = re.search(r"^\d+:\s+(\S+)\s+inet (\d+\.\d+\.\d+\.\d+)/", line)
        if match and not machine_tier.container_bridge(match.group(1)):
            found.append(match.group(2))
    usable = [a for a in found if machine_tier.home_address(a)]
    return sorted(dict.fromkeys(usable), key=lambda a: a != first)


def tailnet_address() -> Optional[str]:
    for line in out(run(["ip", "-o", "-4", "addr", "show"])).splitlines():
        match = re.search(r"^\d+:\s+(tailscale\S*)\s+inet (\d+\.\d+\.\d+\.\d+)/", line)
        if match and ipaddress.ip_address(match.group(2)) in ipaddress.ip_network("100.64.0.0/10"):
            return match.group(2)
    return None


def funnel_on(serve_json: str, status_text: str) -> bool:
    """Whether Tailscale Funnel publishes anything of this machine: its
    serve configuration's AllowFunnel map, else the words of its status."""
    try:
        allowed = json.loads(serve_json).get("AllowFunnel")
    except (ValueError, AttributeError):
        return "Funnel on" in status_text
    return isinstance(allowed, dict) and any(allowed.values())


def key_expiry(status_json: str) -> Optional[str]:
    """The day this machine's tailnet key expires, where Tailscale names one
    (a key with expiry disabled has none)."""
    try:
        expiry = json.loads(status_json)["Self"]["KeyExpiry"]
    except (ValueError, KeyError, TypeError):
        return None
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})T[0-9:.]+(?:Z|[+-]\d{2}:\d{2})", str(expiry))
    return match.group(1) if match else None


def read_small(path: Path) -> Optional[bytes]:
    """A file a container wrote, as a stranger's bytes: no link followed, a
    regular file only, bounded; None for anything else."""
    try:
        return agent._read_bounded(path, SMALL_FILE)
    except OSError:
        return None


def setup_code() -> Optional[str]:
    """The node's setup code, printed only in the form it must have."""
    raw = read_small(ROOT / "data" / "node_identity" / "setup_code.txt")
    if raw is None or len(raw) > 64:
        return None
    text = raw.decode("ascii", "replace").strip()
    return text if agent.SETUP_CODE.fullmatch(text) else None


def qr(text: str) -> Optional[str]:
    """`text` as a QR of block characters, drawn by the image's encoder in a
    container that sees no network, no mount and no capability; the text goes
    in on stdin, never on a command line a process list would show."""
    code = ("import sys, qrcode; q = qrcode.QRCode(border=2); q.add_data(sys.stdin.read()); "
            "q.make(fit=True); q.print_ascii(invert=True)")
    result = run(["docker", "run", "--rm", "-i", "--network", "none", "--read-only", "--cap-drop", "ALL",
                  "--security-opt", "no-new-privileges:true", "--user", f"{os.getuid()}:{os.getgid()}",
                  "--label", "com.docker.compose.project=sautium-setup",
                  "--entrypoint", "python3", agent.image_of(ROOT), "-I", "-c", code],
                 stdin=text.encode(), timeout=60)
    drawn = result.stdout.decode("utf-8", "replace")
    # Only block characters and spaces are a picture; anything else is not shown
    if result.returncode != 0 or not drawn.strip() or re.search(r"[^\s\u2580\u2584\u2588\u00a0]", drawn):
        return None
    return drawn


RPI_SWAP_CONF = "/etc/rpi/swap.conf"


def swap_lines(found: List[dict], step: bool) -> None:
    """What is said of this machine's swap. A file or a partition on a card
    or a stick: the sentence, and where `step` the root step for the systems
    whose swap the setup knows how to keep in memory instead (Raspberry Pi
    OS's rpi-swap; dphys-swapfile before it). A compressed swap that writes
    its idle pages back to a card: a fact, never a step, and only where
    `step` is not asked (`status`). Swap in memory alone: nothing."""
    for swap in found:
        where = {"sd_card": "its card", "usb_stick": "a USB stick"}.get(swap["medium"])
        if swap["kind"] in ("file", "partition") and where:
            why = (f"This machine's swap is a {swap['kind']} on {where} ({printable(swap['path'])}).\n"
                   "When memory runs short the system writes there: slow, and it wears the card.\n"
                   "Swap kept in compressed memory is the better choice.")
            if step and (os.path.exists(RPI_SWAP_CONF) or os.path.isdir(RPI_SWAP_CONF + ".d")):
                block("Swap in memory, not on the card (optional)",
                      why + "\nIt acts from the machine's next start.",
                      [f"F={RPI_SWAP_CONF}.d/90-sautium.conf", f"sudo mkdir -p {RPI_SWAP_CONF}.d",
                       "printf '[Main]\\nMechanism=zram\\n' | sudo tee \"$F\""],
                      [f"sudo rm -f {RPI_SWAP_CONF}.d/90-sautium.conf"])
            elif step and shutil.which("dphys-swapfile", path="/usr/sbin:/sbin:/usr/bin:/bin"):
                block("Swap in memory, not on the card (optional)", why,
                      ["sudo dphys-swapfile swapoff", "sudo dphys-swapfile uninstall",
                       "sudo systemctl disable --now dphys-swapfile", "sudo apt-get install -y zram-tools"],
                      ["sudo apt-get remove -y zram-tools", "sudo systemctl enable --now dphys-swapfile"])
            else:
                say()
                say(why)
                if not step:
                    say("./sautium setup shows the step for it, where it knows one.")
        back = swap.get("writeback")
        if back and not step and back["medium"] in machine_tier.CARD_MEDIA:
            written = "" if back["written_mb"] is None else f": {back['written_mb']:.0f} MB written there since this start"
            size = "" if back.get("size_bytes") is None else f" (a {back['size_bytes'] / 1024 ** 3:.1f} GB file)"
            say(f"Swap: compressed memory ({os.path.basename(swap['path'])}), with idle pages written back to "
                f"{printable(back['path'])}{size} on {machine_tier.MEDIA[back['medium']]}{written}.")


def last_screen(m: dict, preset: Optional[str]) -> None:
    addresses = own_addresses()
    address = addresses[0] if addresses else "127.0.0.1"
    url = f"http://{address}:{WEB_PORT}/"
    code = setup_code()
    say()
    say("Sautium is running.")
    say()
    say(f"  Open           {url:<32} on any device on your home network")
    for other in addresses[1:]:
        say(f"                 http://{other}:{WEB_PORT}/")
    tailnet = tailnet_address()
    if tailnet:
        say(f"  With Tailscale http://{tailnet}:{WEB_PORT}/")
    if code:
        say(f"  Setup code     {code:<32} asked once, when you create your account")
        picture = qr(f"{url}#setup={code}")
        if picture:
            say()
            say(picture.rstrip("\n"))
    elif (ROOT / "data" / "node_identity" / "node_info.json").exists():
        say("  This node has its account already: sign in with its nickname and password.")
    else:
        say("  Setup code     in the node's log: docker compose logs backend")
    say()
    if preset == "nano":
        say(f"  This machine: {machine_line(m, preset)}. Nothing is analysed here; sound features")
        say("  arrive from other Sautium nodes. Your node asks other Sautium nodes about your artists and")
        say("  recordings and takes the sound features they hold, and answers them in turn — you can")
        say("  switch that off in Sautium (More › Sync & P2P).")
    else:
        say(f"  This machine: {machine_line(m, preset)}.")
    say("  This node answers on your home network. Never forward ports 8800, 8830 or 8831 on your router.")
    say("  Stop: docker compose stop     Start: docker compose up -d     Another device: ./sautium pair")
    say()
    swap_lines(m.get("swaps") or [], step=True)


# -- the commands ----------------------------------------------------------------------

def agent_installed() -> bool:
    return bool(agent.read_env(agent.host_dir(ROOT) / "agent.env").get("SAUTIUM_HOST_CHANNEL_KEY"))


def stated_version(commit: str) -> Tuple[int, int, int]:
    """The version `commit`'s tree states (desktop/build_common.py, the
    updater's own reading); (0, 0, 0) when it states none."""
    match = STATED_VERSION.search(out(run(["git", "show", f"{commit}:desktop/build_common.py"])))
    return tuple(int(part) for part in match.groups()) if match else (0, 0, 0)


def head_release() -> Optional[str]:
    """The release HEAD is on: a tag of the version its tree states, naming
    HEAD's commit — the clone's own, or one the host agent fetched."""
    name = "v%d.%d.%d" % stated_version("HEAD")
    refs = out(run(["git", "for-each-ref", "--points-at", "HEAD", "--format=%(refname)",
                    f"refs/tags/{name}", f"{agent.RELEASE_REFS}{name}"]))
    return name if refs else None


def release_step() -> None:
    """Born on a release where the clone holds one whose tree has this setup;
    said plainly otherwise. The setup does not pretend to verify its own
    clone: the check that means something is the person's, against the key
    the site publishes.

    It moves a checkout only before the host agent exists, and only forward
    of the version HEAD's tree states: once the agent is installed the
    release is the agent's, which moves the checkout only to a release its
    chain walk verified. The releases the agent fetched (RELEASE_REFS) are
    unverified until that walk, so they are never one the setup moves to
    (PI-NODE-2E F22: a setup run again after an update went back to the
    clone's own older tag)."""
    head = agent.head(ROOT)
    if agent_installed():
        name = head_release()
        if name is None:
            say(f"This folder is at commit {head[:12]}, on no release. The host agent takes the next signed "
                "release: ./sautium update")
            return
        say(f"This folder is on the release {name}. The host agent takes the next one: ./sautium update")
        verify_hint(name)
        return
    tags = [t for t in out(run(["git", "tag", "--list", "v*", "--sort=-v:refname"])).split()
            if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    newest = tags[0] if tags else None
    # The version its tree states, not its name: a tag is no way forward to
    # a tree older than HEAD's whatever it is called
    if newest is not None and stated_version(newest) < stated_version("HEAD"):
        newest = None
    if newest is not None and out(run(["git", "rev-parse", f"{newest}^{{commit}}"])) != head:
        # Moved to it only where that release carries this setup: an older
        # one would take the setup away from under its own run
        if run(["git", "cat-file", "-e", f"{newest}:scripts/setup.py"]).returncode == 0:
            moved = run(["git", "-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", newest])
            if moved.returncode != 0:
                raise Stop(f"the release {newest} could not be checked out: git status says why")
            say(f"Moved this folder to the release {newest}; its own setup goes on from here.")
            os.execv(sys.executable, [sys.executable, "-I", str(ROOT / "scripts" / "setup.py")] + sys.argv[1:])
        newest = None
    newest = newest or head_release()
    if newest is None:
        say("This folder is not on a signed release (it holds none, or none that carries this setup):")
        say("updates begin with the next release.")
        return
    say(f"This folder is on the release {newest}.")
    verify_hint(newest)


def verify_hint(name: str) -> None:
    ref = name if run(["git", "show-ref", "--verify", "--quiet", f"refs/tags/{name}"]).returncode == 0 \
        else f"{agent.RELEASE_REFS}{name}"
    say("To check it against the key Sautium publishes")
    say("(sautium.net › Download), save the key line from that page as release_signers, then:")
    say(f"    git -c gpg.ssh.allowedSignersFile=release_signers verify-tag {ref}")


def desktop_automounter() -> bool:
    """Whether this system's desktop mounts plugged disks itself: udisks2
    under a graphical session (a Raspberry Pi OS with desktop; a Lite system
    has neither). Two automounters would mount the same disk twice."""
    return (out(run(["systemctl", "get-default"])) == "graphical.target"
            and run(["systemctl", "is-active", "--quiet", "udisks2.service"]).returncode == 0)


# A music disk mounted by a line of /etc/fstab with the automount's own
# options: by filesystem, the driver and what follows
# "ro,nofail,nosuid,nodev,noexec". exFAT without its time option: a kernel
# that refused it would refuse the whole line, and a line that fails is a
# disk that is not there. NTFS by the automount's own choice (fstab_driver).
FSTAB_FILESYSTEMS = {
    "vfat": ("vfat", ",uid={uid},gid={gid},fmask=0133,dmask=0022,tz=UTC"),
    "exfat": ("exfat", ",uid={uid},gid={gid},fmask=0133,dmask=0022"),
    "ntfs": ("ntfs3", ",uid={uid},gid={gid},fmask=0133,dmask=0022"),
    "ntfs3": ("ntfs3", ",uid={uid},gid={gid},fmask=0133,dmask=0022"),
    "ext4": ("ext4", ",noload"),
}
# Since this systemd a line of fstab can name the unit that wants its mount
WANTED_BY_SINCE = 245
# Where the setup's line mounts a music disk: the automount's own point for
# that disk, under the one root the node has for disks. A root is then never
# a mount point — bound into a container made while the disk was in, a mount
# point is the disk's filesystem itself, and the node sees neither its pull
# nor its return (PI-NODE-2D §2). Until 2026-11-24 the line mounted at
# /mnt/sautium-<UUID> and that point was a root: OLD_BOOT_MOUNT_POINT.
BOOT_MOUNT_POINT = re.compile(re.escape(AUTOMOUNT_BASE) + r"/([0-9A-Za-z-]{4,40})")
OLD_BOOT_MOUNT_POINT = re.compile(r"/mnt/sautium-([0-9A-Za-z-]{4,40})")
GENERATOR_DIR = "/run/systemd/generator"
# Where the automount's script keeps which point it mounted a device at
AUTOMOUNT_STATE = "/run/sautium-automount"
NAMES_DIR = ".names"
# What is true of a root that lies on a disk's own mount, said wherever one is met
SEEN_AT_START = "Sautium sees what is mounted there when Sautium starts."
NOT_SEEN_LATER = ("A disk unplugged or put back while Sautium runs is not seen until Sautium is started\n"
                  "again (./sautium).")


def disk_filesystem(device: str) -> str:
    """The filesystem as the disk itself says it (lsblk reads the disk, not
    the mount)."""
    lines = out(run(["lsblk", "-no", "FSTYPE", device])).splitlines()
    return lines[0].strip() if lines else ""


def systemd_version() -> int:
    """This system's systemd, 0 where it does not say."""
    match = re.match(r"systemd (\d+)", out(run(["systemctl", "--version"])))
    return int(match.group(1)) if match else 0


def unit_name(path: str, suffix: str) -> str:
    r"""systemd's unit name for a path (systemd-escape -p): `/` is `-`, and a
    letter outside [A-Za-z0-9:_.] — a UUID's own `-` above all — is \xNN."""
    escaped = "-".join("".join(c if re.fullmatch(r"[A-Za-z0-9:_.]", c) else "".join(f"\\x{b:02x}" for b in c.encode())
                               for c in part) for part in path.strip("/").split("/"))
    return f"{escaped}.{suffix}"


def plug_option(uuid: str) -> str:
    """The option that starts a line's mount whenever its disk appears, at a
    start of the machine and at a later plug alike: the disk's own device
    unit wants the mount. Since systemd 242 nothing else does — a line
    without it is acted on at boot and never again. The unit's NAME it must
    be: the generator does not convert a path given in its place."""
    return "x-systemd.wanted-by=" + unit_name(f"/dev/disk/by-uuid/{uuid}", "device")


def boot_line(uuid: str, point: str, driver: str, options: str, systemd: int) -> str:
    """The line of /etc/fstab for a music disk: read-only, by UUID, before
    Docker. `nofail` stays beside the plug option although systemd then
    ignores it: a systemd that does not know the option would otherwise hold
    the machine's start in emergency mode whenever the disk is out. Such a
    systemd (under 245) gets the line of before: mounted at a start only,
    waiting ten seconds at most for a disk that is not plugged in."""
    tail = plug_option(uuid) if systemd >= WANTED_BY_SINCE else "x-systemd.device-timeout=10s"
    return f"UUID={uuid} {point} {driver} ro,nofail,nosuid,nodev,noexec{options},x-systemd.before=docker.service,{tail} 0 0"


def fstab_driver(fstype: str) -> str:
    return "ntfs-3g" if fstype in ("ntfs", "ntfs3") and ntfs_3g() else FSTAB_FILESYSTEMS[fstype][0]


def boot_mount(real: str, findmnt_json: str, uid: int, gid: int, disk_type=disk_filesystem,
               systemd: int = WANTED_BY_SINCE) -> Optional[dict]:
    """For a folder on a disk something else mounts (a desktop, a person's
    own command): the /etc/fstab line that mounts that disk itself instead
    (boot_line) — where it then lies, the folder's path there and under the
    disk, the disk's label, and the device held now, by a name a command can
    carry. None where the mount cannot be read whole, is the system's own
    disk, or is not a filesystem the automount knows.
    `findmnt_json`: `findmnt -J -o SOURCE,TARGET,FSTYPE,UUID,LABEL -T <folder>`.
    A mount through FUSE says `fuseblk` for NTFS and exFAT alike: there
    `disk_type(device)` says which. The label is a stranger's text that the
    block carries between single quotes: one with a single quote is not
    carried, and its disk goes by its UUID."""
    try:
        mount = json.loads(findmnt_json)["filesystems"][0]
        target, fstype, uuid = str(mount["target"]), str(mount["fstype"]), str(mount["uuid"] or "")
        source = str(mount.get("source") or "")
        label = cut_label(str(mount.get("label") or ""))
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None
    if target == "/" or not re.fullmatch(r"[0-9A-Za-z-]{4,40}", uuid):
        return None
    if not (real == target or real.startswith(target.rstrip("/") + "/")):
        return None
    device = source if re.fullmatch(r"/dev/[A-Za-z0-9/_-]{1,64}", source) else f"/dev/disk/by-uuid/{uuid}"
    if fstype == "fuseblk":
        fstype = disk_type(device)
    if fstype not in FSTAB_FILESYSTEMS:
        return None
    point = f"{AUTOMOUNT_BASE}/{uuid}"
    line = boot_line(uuid, point, fstab_driver(fstype), FSTAB_FILESYSTEMS[fstype][1].format(uid=uid, gid=gid), systemd)
    under = real[len(target.rstrip("/")):].strip("/")
    return {"uuid": uuid, "point": point, "line": line, "folder": f"{point}/{under}".rstrip("/"), "under": under,
            "device": device, "label": "" if "'" in label else label, "at_plug": systemd >= WANTED_BY_SINCE}


def in_pieces(line: str, width: int = 68) -> List[str]:
    """A long line of /etc/fstab as shell lines that build it in the variable
    L, each short enough that nothing wraps it: cut at its spaces and at the
    commas of its options, each piece carrying the separator before it — and
    inside an option longer than a line (the unit's name in the plug
    option). Every piece is single-quoted, where a shell reads each letter
    as itself: the line holds a backslash (the unit's name) and never a
    single quote."""
    if "'" in line:
        raise ValueError("a line with a single quote is not carried")
    tokens = re.split(r"([, ])", line)
    pieces, piece = [], tokens[0]
    for sep, text in zip(tokens[1::2], tokens[2::2]):
        if len(piece) + 1 + len(text) > width:
            pieces.append(piece)
            piece = sep + text
        else:
            piece += sep + text
    pieces.append(piece)
    pieces = [piece[i:i + width] for piece in pieces for i in range(0, len(piece), width)]
    return [f"L='{pieces[0]}'"] + [f"L=\"$L\"'{piece}'" for piece in pieces[1:]]


def boot_mount_undo(point: str) -> List[str]:
    # The mount point in a variable: with a 36-character UUID in it the sed
    # line would be long enough to be wrapped on its way to a root terminal
    lines = [f"P={point}", 'sudo umount "$P"',
             'sudo sed -i "\\|^UUID=[^ ]* $P |d" /etc/fstab',     # sed's address for that one line
             'sudo rmdir "$P"']
    match = BOOT_MOUNT_POINT.fullmatch(point)
    if match:
        lines.append(f"sudo rm -f {AUTOMOUNT_BASE}/{NAMES_DIR}/{match.group(1)}")
    return lines + ["sudo systemctl daemon-reload"]


def boot_mounts(fstab_targets: str) -> List[str]:
    """The mount points of this setup's own fstab lines, of `findmnt --fstab
    -n -o TARGET`'s answer — where it mounts a disk now and where it did
    until 2026-11-24: what `remove` prints the undo of."""
    return [t for t in fstab_targets.split() if BOOT_MOUNT_POINT.fullmatch(t) or OLD_BOOT_MOUNT_POINT.fullmatch(t)]


def let_go(found: dict, mounted_at: List[str], state: str = AUTOMOUNT_STATE) -> List[str]:
    """The commands that let go of the disk's present holds, so that the
    line's own mount can take it: the same disk is not mounted read-only
    beside a mount for writing. Where Sautium's automount holds it (its state
    file names the point) its unit is stopped — the unit's own stop unmounts
    and removes its label file, and no later pull's stop then takes the
    line's mount point and label with it. Any other hold (a desktop's, a
    person's own mount) goes by `umount` of the device, which names no
    folder: a mount point's name may be the disk's label, a stranger's text.
    `mounted_at`: where the disk is mounted now."""
    steps, others = [], list(mounted_at)
    name = os.path.basename(os.path.realpath(found["device"]))
    if re.fullmatch(r"[A-Za-z0-9]+", name):
        raw = read_small(Path(state) / name) or b""
        own = f"{AUTOMOUNT_BASE}/{raw.decode('ascii', 'replace').strip()}"
        if raw.strip() and own in others:
            steps.append(f"sudo systemctl stop sautium-automount@{name}.service")
            others.remove(own)
    if others:
        steps.append(f"sudo umount {found['device']}")
    return steps


def line_steps(found: dict) -> List[str]:
    """What puts a music disk's line into /etc/fstab and mounts it, in order:
    the folder disks are mounted in and its labels' folder (there already
    beside the automount), the mount point (systemd makes a mount unit's
    point itself; this one is for the `mount` below), the disk's label where
    it has one — the file the automount leaves for a disk it mounts, which
    the node reads as a stranger's text —, the line, its mount. The label
    goes in as data on tee's input, between single quotes where a shell
    reads each letter as itself."""
    label = [f"T='{found['label']}'",
             'printf \'%s\\n\' "$T" | sudo tee "$B/.names/$U" >/dev/null'] if found["label"] else []
    return [f"B={AUTOMOUNT_BASE}", f"U={found['uuid']}",
            f'sudo install -d -m 0755 "$B" "$B/{NAMES_DIR}"',
            'sudo mkdir -p "$B/$U"',
            *label,
            *in_pieces(found["line"]),
            # its own line whatever the file's last one ends with
            "printf '\\n%s\\n' \"$L\" | sudo tee -a /etc/fstab",
            "sudo systemctl daemon-reload",
            'sudo mount "$B/$U"']


def named(found: dict) -> str:
    """The disk and the folder on it as the folder sheet shows them."""
    disk = f'"{printable(found["label"])}"' if found["label"] else "the disk"
    return f"{disk} › {printable(found['under'])}" if found["under"] else disk


def boot_mount_block(found: dict, why: str, mounted_at: List[str], whose: str = "its present") -> None:
    hold = let_go(found, mounted_at)
    if found["at_plug"]:
        title = "The music disk, mounted whenever it is plugged in"
        when = (f"This mounts the same disk read-only at {found['point']} whenever it is\n"
                "plugged in — at a start of the machine and later. Sautium sees it come and go there by itself.")
    else:
        title = "The music disk, mounted when the machine starts"
        when = (f"This mounts the same disk at every start, read-only, at {found['point']},\n"
                "before Sautium starts (a disk that is not plugged in holds the start back ten seconds at most).\n"
                "This system's systemd is older than 245: the disk is mounted at a start of the machine only —\n"
                "plugged in later, it waits for the next start.")
    if hold:
        when += f"\nClose anything that has a file of the disk open first: {whose} hold is let go here."
    block(title, why + "\n" + when, hold + line_steps(found), boot_mount_undo(found["point"]))


def fstab_line(point: str) -> Optional[dict]:
    """The line of /etc/fstab that mounts at `point`, as findmnt reads the
    file: {source, fstype, options}; None where no line does."""
    try:
        line = json.loads(out(run(["findmnt", "--fstab", "-J", "-o", "SOURCE,FSTYPE,OPTIONS", point])))
        line = line["filesystems"][0]
        return {key: str(line[key]) for key in ("source", "fstype", "options")}
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def mounted_safely(options: str) -> bool:
    """Whether a line's options mount its disk read-only and with nothing of
    the disk's run or trusted — as mount(8) reads them: left to right, a
    later word undoing an earlier one. `rw`, `suid`, `dev` and `exec` each
    undo one of the four; `defaults` is all of those at once; `user` and
    `users` bring nosuid, nodev and noexec with them, `owner` and `group`
    the first two."""
    safe = {"ro": False, "nosuid": False, "nodev": False, "noexec": False}
    undone_by = {"rw": "ro", "suid": "nosuid", "dev": "nodev", "exec": "noexec"}
    for word in options.split(","):
        if word in safe:
            safe[word] = True
        elif word in undone_by:
            safe[undone_by[word]] = False
        elif word == "defaults":
            safe = dict.fromkeys(safe, False)
        elif word in ("user", "users", "owner", "group"):
            safe["nosuid"] = safe["nodev"] = True
            if word in ("user", "users"):
                safe["noexec"] = True
    return all(safe.values())


def own_line(line: dict, uuid: str) -> bool:
    """Whether a line of /etc/fstab at one of the setup's mount points is
    still as the setup writes one — the disk by its UUID, read-only and with
    nothing of the disk's run or trusted in effect (the node's bind does not
    make a mount that arrives later read-only: each mount says so itself), no
    letter a shell line cannot carry. The setup builds a new line from the
    options of such a line only; any other is the person's own."""
    return line["source"] == f"UUID={uuid}" and "'" not in line["options"] and mounted_safely(line["options"])


def old_boot_mount(root: str) -> Optional[dict]:
    """The line an earlier setup wrote for `root`, where the root is that
    line's own mount point (/mnt/sautium-<UUID>) and the line is still as the
    setup writes one: {uuid, fstype, options}."""
    match = OLD_BOOT_MOUNT_POINT.fullmatch(root)
    line = fstab_line(root) if match else None
    if line is None or not own_line(line, match.group(1)):
        return None
    return {"uuid": match.group(1), "fstype": line["fstype"], "options": line["options"]}


def mount_point_of(real: str) -> Optional[str]:
    """Where the disk `real` lies on is mounted; None for the system's own."""
    target = out(run(["findmnt", "-n", "-o", "TARGET", "-T", real])).splitlines()
    return target[0] if target and target[0] not in ("", "/") else None


def someone_mounts(target: str) -> bool:
    """Whether the mount at `target` is one a person set up to be there: a
    line of /etc/fstab, a .mount or .automount unit somebody wrote (systemd
    names a unit file for it — for a mount it merely saw being made, a
    desktop's or a hand's, it names none), an automount point of any kind
    (autofs under the filesystem). Such a mount is the person's own, not the
    setup's to move."""
    if run(["findmnt", "--fstab", "-n", target]).returncode == 0:
        return True
    units = out(run(["systemctl", "show", "-p", "FragmentPath", "--value",
                     unit_name(target, "mount"), unit_name(target, "automount")]))
    if units.strip():
        return True
    return "autofs" in out(run(["findmnt", "-rn", "-o", "FSTYPE", "--mountpoint", target])).split()


def mounted_outside_fstab(real: str) -> Optional[str]:
    """The mount point of the disk `real` lies on, where that disk is not the
    system's own and neither a line of /etc/fstab nor a unit of someone's
    mounts it (someone_mounts): a desktop mounted it, or a hand."""
    target = mount_point_of(real)
    if target is None or someone_mounts(target):
        return None
    return target


# What a person plugs in and pulls, by the medium lsblk's own columns say
# (machine_tier.storage_medium — the one reader of them): what the automount's
# rule takes. A disk or an SSD in a USB case says it is not removable and is
# pulled as a stick is; a card is one in the slot. Never an NVMe, a SATA disk
# or eMMC: an internal disk is the person's to mount, and a line that mounted
# it "whenever it is plugged in" would mount it a second time at every start.
PULLED_MEDIA = ("sd_card", "usb_stick", "usb_disk")


def pulled_disk(device: str, lsblk_json: str, system_source: str, sysfs: str = "/sys/block") -> bool:
    """Whether `device` lies on a disk a person plugs in and pulls
    (PULLED_MEDIA) that is not the medium the system itself runs from.
    `lsblk_json`: `lsblk -J -o NAME,RM,TRAN,ROTA`; `system_source`: the
    device mounted at /."""
    chain = device_chain(os.path.realpath(device), lsblk_json)
    if machine_tier.storage_medium(chain, sysfs) not in PULLED_MEDIA:
        return False
    system = device_chain(system_source, lsblk_json)
    return not system or system[0].get("name") != chain[0].get("name")


def disk_of(real: str, systemd: int) -> Optional[dict]:
    """For a folder on a removable disk that nobody set up a mount for (a
    desktop mounted it, or a hand), and not under the folder Sautium's disks
    are mounted in: the setup's own line for that disk (boot_mount) and where
    the disk is mounted now (`mounted_at`). None where the setup has no line
    to offer, and only says what the mount means (on_a_mount): the system's
    disk, a disk that is not pulled (an internal one, however it is mounted),
    someone's own line or unit, a network share, a filesystem the automount
    does not know."""
    if real == AUTOMOUNT_BASE or real.startswith(AUTOMOUNT_BASE + "/") or mounted_outside_fstab(real) is None:
        return None
    found = boot_mount(real, out(run(["findmnt", "-J", "-o", "SOURCE,TARGET,FSTYPE,UUID,LABEL", "-T", real])),
                       os.getuid(), os.getgid(), systemd=systemd)
    if found is None or not pulled_disk(found["device"], out(run(["lsblk", "-J", "-o", "NAME,RM,TRAN,ROTA"])),
                                        mount_of("/").get("source", "")):
        return None
    held = out(run(["findmnt", "-rn", "-o", "TARGET", "-S", f"UUID={found['uuid']}"]))
    found["mounted_at"] = [mount_target(line) for line in held.splitlines() if line]
    return found


def desktop_mounted(real: str) -> bool:
    """A folder on a disk the desktop mounts for the person signed in to it:
    under /media/<user>/ or /run/media/<user>/."""
    parts = Path(real).parts
    return (len(parts) > 3 and parts[1] == "media" and parts[2] != "sautium") or (
        len(parts) > 4 and parts[1:3] == ("run", "media"))


def moved_block(root: str, old: dict, label: str, systemd: int, forgotten: bool = False) -> None:
    """An install whose line mounts its disk at the root itself: the old
    line's undo and the line at the new point, as one step. The line keeps
    its own options; one written before the plug option gains it. The node
    takes the files and the chosen folder along at its next start, by the
    UUID both points carry (library_root.apply). `forgotten`: the root was
    forgotten and the line left, so the disk is nowhere Sautium looks."""
    uuid = old["uuid"]
    point = f"{AUTOMOUNT_BASE}/{uuid}"
    options = old["options"].split(",")
    if systemd >= WANTED_BY_SINCE and plug_option(uuid) not in options:
        options = [o for o in options if not o.startswith("x-systemd.device-timeout=")] + [plug_option(uuid)]
    found = {"uuid": uuid, "point": point, "label": "" if "'" in label else label,
             "line": f"UUID={uuid} {point} {old['fstype']} {','.join(options)} 0 0"}
    block("The music disk, mounted " + ("whenever it is plugged in" if plug_option(uuid) in options
                                        else "when the machine starts") + " (optional)",
          (f"A line of /etc/fstab mounts the disk {uuid} at {root}, where Sautium does not\n"
           "look: it never appears in Sautium. The block below mounts it where Sautium sees it.\n"
           "Sautium finds its albums at the new place when it starts again; nothing is read or analysed again."
           if forgotten else
           f"The line of /etc/fstab that mounts the music disk at {root} gives Sautium the\n"
           "mount point itself: a disk pulled or put back while Sautium runs is not seen. This moves it to\n"
           f"{point}, where Sautium sees it come and go. Sautium finds its albums at the new\n"
           "place by itself; nothing is read or analysed again."),
          boot_mount_undo(root)[:-1] + line_steps(found), boot_mount_undo(point))


def forgotten_old_lines(roots: List[str], fstab_targets: str) -> List[Tuple[str, dict]]:
    """The setup's own lines of before 2026-11-24 (old_boot_mount) that no
    root names — the root was forgotten, the line left: the disk they mount
    never appears in Sautium. (point, the line), of `findmnt --fstab -n -o
    TARGET`'s answer."""
    return [(point, old) for point in boot_mounts(fstab_targets)
            if OLD_BOOT_MOUNT_POINT.fullmatch(point) and point not in roots
            for old in [old_boot_mount(point)] if old is not None]


def boot_mount_steps(roots: List[str], labels: Dict[str, str], shown: Optional[str] = None,
                     generator_dir: str = GENERATOR_DIR) -> None:
    """What the setup says, or prints as a root step, of the disks a line of
    /etc/fstab mounts for the node, and of a root that is a disk's own mount.

    Each of this setup's lines at the point where it mounts a disk now: that
    its disk is mounted at every plug — read off systemd's own generated
    units, which anyone may read — or what makes it so (a line written for a
    systemd older than 245 acts at a start of the machine only: its undo and
    the new line are one block).

    A root that is a mount point (the line of before 2026-11-24, a disk a
    person mounted by hand): the block that moves its disk under the folder
    disks are mounted in, and what follows it; and such a line whose root
    was forgotten, the same block with nothing after it. `shown`: the mount
    point of a disk whose block this run printed already."""
    systemd = systemd_version()
    targets = out(run(["findmnt", "--fstab", "-n", "-o", "TARGET"]))
    for point in boot_mounts(targets):
        match = BOOT_MOUNT_POINT.fullmatch(point)
        line = fstab_line(point) if match else None
        if line is None:
            continue
        uuid = match.group(1)
        wants = Path(generator_dir) / (unit_name(f"/dev/disk/by-uuid/{uuid}", "device") + ".wants") / unit_name(point, "mount")
        if plug_option(uuid) in line["options"].split(","):
            say(f"Music disk at {point}: " + ("mounted whenever it is plugged in." if wants.is_symlink() or wants.exists()
                                              else "its line is in /etc/fstab, and systemd has not read it yet — "
                                                   "run: sudo systemctl daemon-reload"))
        elif systemd < WANTED_BY_SINCE or not own_line(line, uuid):
            say(f"Music disk at {point}: mounted at a start of the machine only.")
        else:
            options = ",".join(o for o in line["options"].split(",") if not o.startswith("x-systemd.device-timeout="))
            new = f"UUID={uuid} {point} {line['fstype']} {options},{plug_option(uuid)} 0 0"
            block("The music disk, mounted whenever it is plugged in (optional)",
                  f"The line of /etc/fstab that mounts the music disk at {point} acts at a start of the\n"
                  "machine only: plugged in later, the disk waits for the next start. This puts in its place a\n"
                  "line that mounts the disk whenever it is plugged in — at a start of the machine and later.\n"
                  "It acts from the disk's next plug.",
                  [f"P={point}",
                   'sudo sed -i "\\|^UUID=[^ ]* $P |d" /etc/fstab',
                   *in_pieces(new),
                   "printf '\\n%s\\n' \"$L\" | sudo tee -a /etc/fstab",
                   "sudo systemctl daemon-reload"],
                  boot_mount_undo(point))
    for point, old in forgotten_old_lines(roots, targets):
        if fstab_line(f"{AUTOMOUNT_BASE}/{old['uuid']}") is None:
            moved_block(point, old, root_label(point), systemd, forgotten=True)
            continue
        # The disk's line at the new point is in already: the old one alone goes
        line = f"UUID={old['uuid']} {point} {old['fstype']} {old['options']} 0 0"
        block("An old line of /etc/fstab (optional)",
              f"A line of /etc/fstab still mounts the disk {old['uuid']} at {point} as well as at\n"
              f"{AUTOMOUNT_BASE}/{old['uuid']}, where Sautium sees it: the block takes the old line out.",
              boot_mount_undo(point),
              [f"sudo mkdir -p {shlex.quote(point)}", *in_pieces(line),
               "printf '\\n%s\\n' \"$L\" | sudo tee -a /etc/fstab", "sudo systemctl daemon-reload"])
    for root in roots:
        if root == AUTOMOUNT_BASE:
            continue
        old = old_boot_mount(root)
        found = None
        if old is not None:
            moved_block(root, old, labels.get(root, ""), systemd)
        else:
            found = disk_of(root, systemd)
            if found is None or found["point"] == shown:
                continue
            boot_mount_block(found, f"{root} is {on_a_mount(root)}: {SEEN_AT_START}", found["mounted_at"])
        follows(root, found)


def follows(root: str, found: Optional[dict]) -> None:
    """What follows the block that moves a root's disk under the folder disks
    are mounted in: the root, an empty folder from then on, is forgotten —
    which writes the override again (the disks' folder bound, where the block
    made it) and makes the node again — and `found`'s folder is chosen at its
    new place. None after the move of the setup's own old line: the node
    takes the chosen folder along with its files."""
    say(f"Then run:  ./sautium roots --forget {shlex.quote(root)}")
    if found is not None:
        say(f"and choose {named(found)} as the music folder in Sautium (More › Library).")
    say()


def on_a_mount(real: str) -> Optional[str]:
    """How a root that lies on a disk's own mount is said — the mount point
    itself, or a folder of a disk no line of /etc/fstab mounts. None for a
    folder of the system's own disk, of a disk a line of /etc/fstab mounts
    above it (there at every start), or under the folder disks are mounted in."""
    target = mount_point_of(real)
    if target is None or real == AUTOMOUNT_BASE or real.startswith(AUTOMOUNT_BASE + "/"):
        return None
    if target == real:
        return "a mount point"
    if someone_mounts(target):
        return None
    return f"on a disk mounted at {printable(target)}, which no line of /etc/fstab mounts"


def root_steps(args, state: dict, automount: bool = True, disk: Optional[dict] = None) -> None:
    """The optional root steps: each printed once as a whole block, then —
    where a person is at the terminal — waited for and checked. Declined,
    each leaves a working node. `--yes` never waits. No automount is offered
    to an install that reads its music by its .env line: plugged disks
    appear under a root, and it has none. An automount an earlier release
    installed is offered again as an update; `state` remembers that it was
    stale, for the run that finds it current (cmd_setup). `disk`: the line
    for the disk the --music folder is on (disk_of), printed here and checked
    by the caller."""
    uid, gid = os.getuid(), os.getgid()
    user = os.environ.get("USER") or str(uid)
    shown = []
    if not lingering():
        block("Updates while you are signed out (recommended)",
              "Sautium's updates run from your own user's services. Without this they run only\n"
              "while you are signed in to this machine.",
              [f"sudo loginctl enable-linger {shlex.quote(user)}"],
              [f"sudo loginctl disable-linger {shlex.quote(user)}"])
        shown.append("linger")
    installed = automount_installed()
    desktop = automount and not installed and desktop_automounter()
    if desktop:
        say()
        say("Plugged disks: this machine's desktop mounts them itself, under /media/<your name>, while you")
        say("are signed in to it — so Sautium's own automount is not offered here. Name such a disk's music")
        say("folder with --music: the setup then prints the step that mounts that disk for Sautium whenever it")
        say("is plugged in.")
    files = automount_files(agent.host_dir(ROOT) / "automount", uid, gid) if automount and not desktop else None
    if files and not installed:
        automount_block(files)
        shown.append("automount")
    elif files and automount_stale(files):
        automount_block(files, update=True)
        shown.append("automount update")
        if not state.get("automount_stale"):
            state["automount_stale"] = True
            save_state(state)
    if files and not ntfs_3g():
        why = ("Disks formatted by Windows (NTFS) are opened by the ntfs-3g driver where it is installed; this\n"
               "system's own NTFS driver refuses some of them, and such a disk then stays out of Sautium.")
        if shutil.which("apt-get"):
            block("Windows-formatted disks (optional)", why,
                  ["sudo apt-get install -y ntfs-3g"], ["sudo apt-get remove -y ntfs-3g"])
            shown.append("ntfs-3g")
        else:
            say()
            say(why)
            say("Install the package ntfs-3g with this system's package manager.")
    if disk is not None:
        boot_mount_block(disk, disk["why"], disk["mounted_at"], "the desktop's" if disk["desktop"] else "its present")
        if not disk["desktop"]:
            follows(disk["real"], disk)
        shown.append("music disk")
    if not shown:
        return
    if not args.yes and sys.stdin.isatty():
        try:
            input("Run the ones you want in another terminal, then press Enter here (Enter alone goes on without them): ")
        except EOFError:
            pass
    if "linger" in shown:
        say("Updates while signed out: " + ("on." if lingering() else
                                           "not set — updates run only while you are signed in."))
    if "automount" in shown:
        say("Plugged disks: " + ("will appear in Sautium." if automount_installed() else
                                 "not set — Sautium shows the folders named with --music only."))
    if "automount update" in shown:
        say("Plugged disks: " + ("the automount is this release's." if not automount_stale(files) else
                                 "the automount is an earlier release's — plugged disks appear as before."))
    if "ntfs-3g" in shown:
        say("Windows-formatted disks: " + ("ntfs-3g is installed." if ntfs_3g() else
                                           "not set — this system's own NTFS driver opens what it can."))


def automount_renewed(state: dict) -> bool:
    """Whether the automount went from an earlier release's to this one's
    since a run that offered the update: the node then starts again, to
    watch the folder the new script writes to (a node holds its watch from
    its start)."""
    if not state.get("automount_stale") or not automount_installed():
        return False
    files = automount_files(agent.host_dir(ROOT) / "automount", os.getuid(), os.getgid())
    if automount_stale(files):
        return False
    state.pop("automount_stale")
    save_state(state)
    return True


def cmd_setup(args) -> int:
    refuse_root()
    check_checkout()
    check_machine()
    kind, sentence = docker_state()
    if kind == "denied":
        user = os.environ.get("USER") or str(os.getuid())
        block("Docker for this user", sentence + ". Membership of the docker group lets this user\n"
              "start containers — which on this machine is as much as root may do.",
              [f"sudo usermod -aG docker {shlex.quote(user)}"],
              [f"sudo gpasswd -d {shlex.quote(user)} docker"])
        raise Stop("then sign out and in again (the group is read at sign-in), and run ./sautium again")
    if kind != "ok":
        raise Stop(sentence)
    say(sentence)
    release_step()

    m = machine()
    held = agent.read_env(ROOT / ".env")
    volume = database_volume_exists()
    preset = birth_of(held, args.preset, machine_tier.birth_preset(
        m["ram_gib"], m["docker_root_medium"], m["docker_root_free_gb"])[0], volume)
    by_line = keeps_music_line(held)
    if by_line and args.music:
        raise Stop("this install reads its music where MUSIC_LIBRARY_PATH in .env says, and --music is for an "
                   "install that picks its folder in Sautium. To change over, delete that line from .env and run "
                   "this again: Sautium then reads the whole library again from the folder you choose")
    music, disk = args.music, None
    if music and os.path.isdir(music):
        # A folder on a disk no line of /etc/fstab mounts (a desktop mounted
        # it, or a person by hand): the setup's own line for that disk, which
        # mounts it under the folder the node reads disks in
        real = os.path.realpath(music)
        desktop = desktop_mounted(real) and desktop_automounter() and mounted_outside_fstab(real) is not None
        disk = disk_of(real, systemd_version())
        if disk is None and desktop:
            raise Stop(f"--music {music}: it is on a disk this machine's desktop mounts only while you are "
                       "signed in, and Sautium starts with the machine. Mount the disk at every start (a line of "
                       "/etc/fstab: read-only, nofail) and name the folder where it then lies")
        if disk is not None:
            disk["desktop"], disk["real"] = desktop, real
            disk["why"] = (f"{music} is on a disk this machine's desktop mounts only while you are signed in to it.\n"
                           "Sautium starts with the machine, before that." if desktop
                           else f"{music} is {on_a_mount(real)}: {SEEN_AT_START}")
    say(f"This machine: {machine_line(m, preset)}. Change it later in Sautium: More › Profile.")
    need = NEED_GB[preset]
    if not volume and m["docker_root_free_gb"] is not None and m["docker_root_free_gb"] < need:
        raise Stop(f"{m['docker_root']} has {m['docker_root_free_gb']:.0f} GB free, and this node needs "
                   f"{need} GB there: free some space and run ./sautium again")
    if not node_running():
        taken = ports_taken()
        if taken:
            raise Stop("another program holds a port Sautium needs: " + ", ".join(taken)
                       + ". Stop it (sudo ss -ltnup shows which) and run ./sautium again")
    low = undervoltage_now()
    if low:
        say("Warning: this Pi's power supply is under its voltage right now. Under load it can switch")
        say("the Pi off. Use the official power supply.")

    state = load_state()
    assistant = args.assistant or ("claude" if held.get("SAUTIUM_IMAGE_ASSISTANT_CLI") == "1" else "none")
    refusal = build_refusal(state, {**held, **image_choice(held, preset=preset, asked_preset=args.preset,
                                                            assistant=assistant)})
    if refusal:
        raise Stop(refusal)
    host = agent.host_dir(ROOT)
    host.mkdir(mode=0o700, exist_ok=True)
    root_steps(args, state, automount=not by_line, disk=disk)

    if not by_line:
        # A folder on a disk the desktop mounts is never a root: its bind
        # would be made by Docker whenever the disk is out — a folder of
        # root's in the desktop's own place for that disk. With the line
        # pasted its disk lies under the folder the node reads disks in, and
        # the folder named is chosen there, in the Web UI. (A disk someone
        # mounted by hand stays the root it was named as until it is
        # forgotten: `follows`.)
        moved = disk is not None and disk["desktop"] and fstab_line(disk["point"]) is not None
        if disk is not None and disk["desktop"] and not moved:
            raise Stop(f"the music disk is not set, so Sautium would not read {music}: paste the block above, "
                       f"then run ./sautium again and choose {named(disk)} as the music folder in Sautium "
                       "(More › Library)")
        roots = chosen_roots(None if moved else music, held_roots(ROOT / ROOTS_OVERRIDE), m.get("docker_root"))
        write_roots(roots)
        boot_mount_steps(roots, held_labels(ROOT / ROOTS_OVERRIDE), shown=disk["point"] if disk else None)
        if moved:
            say(f"Then choose {named(disk)} as the music folder in Sautium (More › Library).")
        elif disk is None and music and os.path.realpath(music) in roots:
            # A mount the setup has no line for (someone's own line of
            # /etc/fstab, a network share): taken as it is, and said. Never
            # its parent: that would be a root the person did not name.
            said = on_a_mount(os.path.realpath(music))
            if said:
                say(f"{music} is {said}: {SEEN_AT_START}")
                say(NOT_SEEN_LATER)
        (ROOT / "data" / "no-library").mkdir(parents=True, exist_ok=True)
    plan = env_plan(held, preset=preset, asked_preset=args.preset, ram_gib=m["ram_gib"],
                    assistant=assistant, volume_exists=volume, roots=not by_line)
    changed = write_env(plan)
    if changed:
        say(".env: " + ", ".join(k for k in changed if k != "POSTGRES_PASSWORD")
            + (" (and the database's password)" if "POSTGRES_PASSWORD" in changed else ""))

    env = agent.read_env(ROOT / ".env")
    build(state, env)
    if agent.install(ROOT) != 0:
        raise Stop("the host agent could not be installed (its own lines above say why)")
    if not by_line and automount_renewed(state) and node_running():
        run(agent.compose_cmd(ROOT) + ["restart", "backend"], timeout=600)
    start()
    state["done"] = True
    save_state(state)
    last_screen(m, preset)
    return 0


def signed_get(path: str) -> Optional[dict]:
    """GET `path` on this node's own API on loopback, signed as the host
    with the node's API secret — a file the node writes, read as such."""
    raw = read_small(ROOT / "data" / "node_identity" / ".api_secret")
    if not raw:
        return None
    key = raw.strip()
    ts, nonce = str(int(time.time())), secrets.token_hex(16)
    digest = hashlib.sha256(b"").hexdigest()
    sig = hmac.new(key, f"GET\n{path}\n{ts}\n{nonce}\n{digest}".encode(), hashlib.sha256).hexdigest()
    request = urllib.request.Request(NODE_API + path, headers={
        "X-Sautium-Ts": ts, "X-Sautium-Nonce": nonce, "X-Sautium-Sig": sig})
    opener = urllib.request.build_opener(agent._NoRedirect, urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return json.loads(response.read(65536))
    except (urllib.error.URLError, OSError, ValueError):
        return None


# The pairing code the node mints (backend/device_auth.PIN_GROUPS: 7 groups
# of 4, 140 bits — nobody types it, so it is long; held to it by a test)
PAIRING_CODE = re.compile(r"[A-HJ-NP-Z2-9]{4}(?:-[A-HJ-NP-Z2-9]{4}){6}")


def cmd_pair(args) -> int:
    refuse_root()
    answer = signed_get("/api/auth/pin")
    code = str((answer or {}).get("code", ""))
    if not PAIRING_CODE.fullmatch(code):
        raise Stop("the node gave no pairing code: it must be running (docker compose up -d) and have "
                   "its account (open its address and create it first)")
    addresses = own_addresses()
    url = f"http://{addresses[0] if addresses else '127.0.0.1'}:{WEB_PORT}/#pair={code}"
    say(f"Open on the other device within a few minutes:  {url}")
    picture = qr(url)
    if picture:
        say()
        say(picture.rstrip("\n"))
    return 0


def cmd_update(args) -> int:
    refuse_root()
    if not agent_installed():
        raise Stop("this node has no host agent: run ./sautium to install it")
    requests = ROOT / "run" / "requests"
    try:
        fd = os.open(requests, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        raise Stop(f"the requests folder cannot be opened ({e.strerror}): run ./sautium")
    # A folder the node's container writes: the name is made, never opened.
    # One already there — whatever a container left under it, a FIFO or a
    # link — is the same request to the agent, which reads names alone.
    try:
        os.close(os.open("update", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK
                         | os.O_CLOEXEC, 0o600, dir_fd=fd))
    except FileExistsError:
        pass
    except OSError as e:
        raise Stop(f"the request could not be left ({e.strerror}): run ./sautium")
    finally:
        os.close(fd)
    say("Asked for the newest signed release; the host agent is working (an update takes minutes; most of an hour "
        "when it compiles Python on a small board) …")
    log = agent.host_dir(ROOT) / "agent.log"
    before = log.stat().st_size if log.exists() else 0
    # The agent's own unit, waited for: it takes the request just left
    run(["systemctl", "--user", "start", "--wait", "sautium-host-agent.service"], timeout=4 * 3600)
    with open(log, "rb") as f:
        f.seek(before)
        for line in f.read(65536).decode("utf-8", "replace").splitlines():
            say("  " + printable(line))
    return 0


def mount_target(line: str) -> str:
    """The mount point of one line of `findmnt -rn -o TARGET,…`, as its own
    letters: findmnt -r writes a space in a path as \\x20, and may write a
    letter outside ASCII a byte at a time."""
    return re.sub(rb"\\x([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]),
                  line.split(" ", 1)[0].encode("utf-8", "surrogateescape")).decode("utf-8", "replace")


def mounts_under(roots: List[str], findmnt_rn: str) -> List[str]:
    """The lines of `findmnt -rn -o TARGET,FSTYPE,OPTIONS` whose mount point
    is a root or lies under one: a plugged disk the automount mounted, a disk
    mounted at a root when the machine started. (`findmnt -R <folder>` says
    nothing of a folder that is not itself a mount point — which the
    automount's folder never is.)"""
    return [line for line in findmnt_rn.splitlines()
            if any(mount_target(line) == root or mount_target(line).startswith(root.rstrip("/") + "/")
                   for root in roots)]


def cmd_status(args) -> int:
    refuse_root()
    kind, sentence = docker_state()
    say(sentence)
    say(f"Folder: {ROOT}  commit {agent.head(ROOT)[:12]}  image {agent.image_of(ROOT)}")
    say("Containers:")
    for line in out(run(agent.compose_cmd(ROOT) + ["ps", "--format", "{{.Name}}\t{{.Status}}"])).splitlines():
        say("  " + line)
    say(f"Host agent: {'installed' if (agent.host_dir(ROOT) / 'bin' / 'sautium-host-agent').exists() else 'not installed'}"
        f" · lingering {'on' if lingering() else 'off (updates run only while you are signed in)'}")
    say_roots()
    swap_lines(swaps(read_line("/proc/swaps"), out(run(["lsblk", "-J", "-o", "NAME,RM,TRAN,ROTA"]))), step=False)
    tailnet = tailnet_address()
    if tailnet:
        say(f"Tailscale: this machine is {tailnet} on a tailnet; Sautium answers there too.")
        if funnel_on(out(run(["tailscale", "funnel", "status", "--json"])),
                     out(run(["tailscale", "funnel", "status"]))):
            say("  WARNING: Tailscale Funnel is on. Sautium must never be reachable from the open "
                "internet: turn Funnel off (tailscale funnel reset).")
        expiry = key_expiry(out(run(["tailscale", "status", "--json"])))
        if expiry:
            say(f"  This machine's Tailscale key expires {expiry}: sign it in again before then, "
                "or the address stops answering.")
    low = undervoltage_now()
    if low:
        say("Power: the supply is under its voltage right now.")
    # The last update's answer, as the host agent wrote it to its own log
    log = (agent._read_bounded(agent.host_dir(ROOT) / "agent.log", 4 * 1024 * 1024) or b"").decode("utf-8", "replace")
    last = [line for line in log.splitlines() if re.match(r"\[host-agent\] \S+ \S+ update: ", line)][-1:]
    if last:
        say("Last update: " + printable(last[0].split("] ", 1)[1]))
    return 0


def say_roots() -> List[str]:
    """The roots as `status` and `roots` say them; the roots themselves."""
    override = ROOT / ROOTS_OVERRIDE
    roots = held_roots(override)
    systemd = systemd_version()
    stepped = [r for r in roots if r != AUTOMOUNT_BASE and (old_boot_mount(r) or disk_of(r, systemd))]
    targets = out(run(["findmnt", "--fstab", "-n", "-o", "TARGET"]))
    forgotten = [point for point, old in forgotten_old_lines(roots, targets)
                 if fstab_line(f"{AUTOMOUNT_BASE}/{old['uuid']}") is None]     # else its disk is seen there
    for line in root_lines(roots, out(run(["findmnt", "-rn", "-o", "TARGET,FSTYPE,OPTIONS"])), held_labels(override),
                           stepped, held_by_the_node(), forgotten):
        say(line)
    return roots


def disks_folder_ready() -> bool:
    """The automount's folder holds the labels' folder, which the
    automount's install and the setup's fstab block both make: the node
    reads it as the folder disks are mounted in (library_root._disks_folder)."""
    return os.path.isdir(os.path.join(AUTOMOUNT_BASE, NAMES_DIR))


def cmd_roots(args) -> int:
    """The folders this node may read music from; `--forget` takes one out of
    the override — the folder itself, and whatever mounts it, untouched."""
    refuse_root()
    if keeps_music_line(agent.read_env(ROOT / ".env")):
        raise Stop("this install reads its music where MUSIC_LIBRARY_PATH in .env says: it has no roots")
    if not args.forget:
        if not say_roots():
            say("This node has no music roots yet: run ./sautium (and name a folder with --music).")
        return 0
    roots = held_roots(ROOT / ROOTS_OVERRIDE)
    asked = os.path.normpath(args.forget)
    root = next((r for r in roots if r in (asked, os.path.realpath(asked))), None)
    if root == AUTOMOUNT_BASE:
        raise Stop(f"{AUTOMOUNT_BASE} is where plugged disks appear: it leaves with the automount itself "
                   "(./sautium remove prints how)")
    if root is None:
        raise Stop(f"{printable(args.forget)} is not one of this node's music roots (./sautium roots lists them)")
    old = OLD_BOOT_MOUNT_POINT.fullmatch(root)
    moved_to = f"{AUTOMOUNT_BASE}/{old.group(1)}" if old else None
    # The node takes the albums along at its start, by the disk's UUID,
    # wherever its disks' folder is one (library_root.apply's own condition):
    # the new line, or the automount, then mounts the disk at the new point
    moved = bool(moved_to and AUTOMOUNT_BASE in roots and disks_folder_ready())
    if moved:
        say(f"Sautium finds the albums under {root} at {moved_to} when it starts again; nothing is removed.")
    else:
        say(f"Albums under {root} will show as away in Sautium; nothing is removed. Choose another folder in "
            "More › Library.")
    write_roots([r for r in roots if r != root])
    if node_running():
        start()
        say(f"Forgotten: Sautium no longer reads {root}. The folder itself is untouched.")
    else:
        say(f"Forgotten: Sautium will no longer read {root} from its next start. The folder itself is untouched.")
    if old and fstab_line(root):
        say()
        say("A line of /etc/fstab still mounts its disk there. To take that back too (paste into a terminal")
        say("that may use sudo):")
        say()
        for line in boot_mount_undo(root):
            say("    " + line)
        say()
    elif root_state(root, []) == "empty" and os.stat(root).st_uid == 0 and not os.path.ismount(root):
        say(f"The empty folder left in its place is root's: sudo rmdir {shlex.quote(root)}")
    if moved and fstab_line(moved_to) and not os.path.ismount(moved_to):
        # The node that held the old mount point held the disk with it: a
        # mount asked for while it ran may have found the disk busy
        say(f"Its disk is not mounted at {moved_to} now: plug it in again, and it appears in Sautium.")
    return 0


def cmd_remove(args) -> int:
    """Take Sautium off this machine: everything it runs and installed; the
    data only with --data, after the node's nickname is typed."""
    refuse_root()
    env = agent.read_env(ROOT / ".env")
    nickname = None
    if args.data:
        raw = read_small(ROOT / "data" / "node_identity" / "node_info.json")
        try:
            nickname = str(json.loads(raw)["username"]) if raw else None
        except (ValueError, KeyError, TypeError):
            nickname = None
        say("--data removes this node's database, its identity, its downloaded models and its backups.")
        say("A backup kept elsewhere is the way back. The identity comes back from its nickname and")
        say("password; what it earned by computing is earned again.")
        typed = input("Type the node's nickname to go on: " if nickname
                      else "This node has no account. Type REMOVE to go on: ").strip()
        if typed != (nickname or "REMOVE"):
            raise Stop("nothing was removed: that is not the node's nickname")
    done = []

    def compose(what: str, *step: str, timeout: int) -> bool:
        result = run(agent.compose_cmd(ROOT) + list(step), timeout=timeout)
        if result.returncode:
            said = result.stderr.decode("utf-8", "replace").strip().splitlines()
            say(f"  could not {what}: {printable(said[-1]) if said else 'docker compose said nothing'}")
        return result.returncode == 0

    # The node stopped first: its own stop removes its port mapping at the router
    if compose("stop the node", "stop", timeout=300):
        done.append("the node stopped")
    down = ["down", "--remove-orphans", "--rmi", "local"] + (["--volumes"] if args.data else [])
    if compose("remove the containers", *down, timeout=600):
        done.append("containers, networks and images removed" + (", and the database" if args.data else ""))
    elif args.data:
        # The database is the volume: its files are not deleted beside a volume that stayed
        raise Stop("the database's volume was not removed, so nothing of the node's data was deleted. "
                   "Mend what docker compose said above and run this again")
    for image in {env.get("SAUTIUM_BACKEND_IMAGE"), agent.DEFAULT_IMAGE} - {None, ""}:
        run(["docker", "image", "rm", image])
    if (agent.host_dir(ROOT) / "agent.env").exists() or (agent.unit_dir() / "sautium-host-agent.path").exists():
        agent.uninstall(ROOT)
        done.append("the host agent's units and starter removed")
    for path in (agent.host_dir(ROOT) / "bin", agent.host_dir(ROOT) / "automount"):
        shutil.rmtree(path, ignore_errors=True)
    for name in ("host/setup.state", "host/setup-build.log", "host/setup-build.exit"):
        (ROOT / name).unlink(missing_ok=True)
    if args.data:
        # .env names the roots override in COMPOSE_FILE: the two go together,
        # or every later compose command in this folder fails on a file gone
        (ROOT / ROOTS_OVERRIDE).unlink(missing_ok=True)
        # The node ran as this checkout's owner: its files are this user's
        left = []
        for folder in ("data", "backend/data", "run"):
            shutil.rmtree(ROOT / folder, onerror=lambda _f, path, _e: left.append(path))
        (ROOT / ".env").unlink(missing_ok=True)
        done.append("the node's data removed" + (
            f" — but for {len(left)} file(s) another user owns (the first: {left[0]})" if left else ""))
    for line in done or ["nothing of Sautium's was running or installed"]:
        say("  " + line)
    user = os.environ.get("USER") or str(os.getuid())
    say()
    say("What only root can take back, if you ran it (paste into a terminal that may use sudo):")
    say()
    if lingering():
        say(f"    sudo loginctl disable-linger {shlex.quote(user)}")
    if automount_installed() or os.path.exists("/usr/local/lib/sautium/automount"):
        for line in AUTOMOUNT_UNDO:
            say("    " + line)
    for point in boot_mounts(out(run(["findmnt", "--fstab", "-n", "-o", "TARGET"]))):
        for line in boot_mount_undo(point):
            say("    " + line)
    if os.path.isdir(AUTOMOUNT_BASE):
        # The folder disks were mounted in, which the automount's block and
        # the fstab line's block both make: last, and only while it is empty
        # (rmdir removes nothing that holds anything — a disk still mounted
        # there keeps its folder)
        inside = [f"{AUTOMOUNT_BASE}/{name}" for name in (NAMES_DIR, ".unread")
                  if os.path.isdir(f"{AUTOMOUNT_BASE}/{name}")]
        say("    sudo rmdir " + " ".join(inside + [AUTOMOUNT_BASE]))
    say()
    if args.data and sys.stdin.isatty():
        # Last, and only after the data went by the nickname: the folder itself
        try:
            typed = input(f"Delete this folder too ({ROOT})? Type yes: ").strip().lower()
        except EOFError:
            typed = ""
        if typed == "yes":
            os.chdir("/")
            shutil.rmtree(ROOT, ignore_errors=True)
            say("Nothing of Sautium is left on this machine." if not ROOT.exists()
                else f"What could not be deleted is still in {ROOT} (another user's files): sudo rm -rf it.")
            return 0
    say(f"What is left: this folder ({ROOT})" + ("" if args.data else ", with the node's data in it (./data, "
        "backend/data, the database's volume): ./sautium remove --data takes those") + ".")
    say("Delete the folder itself when you no longer want it: rm -rf " + shlex.quote(str(ROOT)))
    return 0


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="./sautium", description="Sautium on this machine.")
    sub = parser.add_subparsers(dest="command")

    def setup_flags(p):
        p.add_argument("--music", metavar="FOLDER", help="a folder of music this node may read")
        p.add_argument("--preset", choices=("nano", "full"), help="how much this machine does (default: read off the machine)")
        p.add_argument("--assistant", choices=("none", "claude"), help="what the image carries for the assistant")
        p.add_argument("--yes", action="store_true", help="never stop at a root step: print it and go on")
    setup_flags(parser)
    setup_flags(sub.add_parser("setup", help="set the node up (the default); the host agent, once installed, updates it"))
    sub.add_parser("status", help="what is installed, what runs")
    roots = sub.add_parser("roots", help="the folders this node may read music from")
    roots.add_argument("--forget", metavar="FOLDER", help="stop reading a folder: the folder itself is untouched")
    sub.add_parser("pair", help="a code and its QR for another device")
    sub.add_parser("update", help="move to the newest signed release")
    remove = sub.add_parser("remove", help="take Sautium off this machine")
    remove.add_argument("--data", action="store_true", help="the database, the identity and the backups too")
    args = parser.parse_args(argv)
    command = {None: cmd_setup, "setup": cmd_setup, "status": cmd_status, "roots": cmd_roots,
               "pair": cmd_pair, "update": cmd_update, "remove": cmd_remove}[args.command]
    try:
        return command(args)
    except Stop as e:
        say()
        say(f"./sautium: {e}")
        return 1
    except KeyboardInterrupt:
        say()
        say("./sautium: stopped. Run it again to go on where it left off.")
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
