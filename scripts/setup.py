#!/usr/bin/env python3
"""Sautium on a Linux machine, from a clone: the console setup (`./sautium`).

    ./sautium            set the node up, or go on where a run stopped
    ./sautium status     what is installed, what runs, what is mounted
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
import stat
import subprocess
import sys
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
SETUP_CODE = re.compile(r"[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}")
SMALL_FILE = 4096


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


def device_removable(source: str, lsblk_json: str) -> bool:
    """Whether the block device `source` (a mount's source, as findmnt names
    it) sits on a card or a removable disk: lsblk's own tree, read from its
    JSON — the device itself or the disk it is a partition of is `mmcblk*`,
    removable, hot-pluggable or on USB."""
    name = os.path.basename(source)

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
        chain = walk(json.loads(lsblk_json).get("blockdevices"), [])
    except ValueError:
        return False
    for node in chain or []:
        if (str(node.get("name", "")).startswith("mmcblk") or node.get("rm") in (True, "1", 1)
                or node.get("hotplug") in (True, "1", 1) or node.get("tran") == "usb"):
            return True
    return False


def mount_of(path: str) -> Dict[str, str]:
    """{source, fstype, target} of the mount holding `path` (findmnt's JSON)."""
    result = run(["findmnt", "-J", "-o", "SOURCE,FSTYPE,TARGET", "-T", path])
    try:
        return json.loads(out(result))["filesystems"][0]
    except (ValueError, KeyError, IndexError):
        return {}


def machine() -> dict:
    """What the setup reads of this machine, once."""
    with open("/proc/meminfo") as f:
        ram_gib = mem_total_kib(f.read()) / 1024 ** 2
    docker_root = out(run(["docker", "info", "--format", "{{.DockerRootDir}}"])) or "/var/lib/docker"
    where = docker_root if os.path.isdir(docker_root) else os.path.dirname(docker_root) or "/"
    holding = mount_of(where)
    removable = device_removable(holding.get("source", ""),
                                 out(run(["lsblk", "-J", "-o", "NAME,RM,HOTPLUG,TRAN"])))
    try:
        free_gb = shutil.disk_usage(where).free / 1e9
    except OSError:
        free_gb = None
    return {"ram_gib": ram_gib, "cores": os.cpu_count() or 1, "arch": platform.machine(),
            "docker_root": docker_root, "docker_root_removable": removable,
            "docker_root_free_gb": free_gb}


def machine_line(m: dict, preset: Optional[str]) -> str:
    disk = "a card or removable disk" if m["docker_root_removable"] else "a fixed disk"
    return (f"{round(m['ram_gib'])} GB RAM, {m['cores']} cores, Docker's data on {disk} → "
            f"{'Nano' if preset == 'nano' else 'the full node'}")


def pg_settings(ram_gib: float) -> Dict[str, str]:
    """PostgreSQL's memory for this board (the compose file's defaults are a
    16 GB board's); nothing is written where the defaults fit."""
    if ram_gib >= 12:
        return {}
    if ram_gib >= 6:
        return {"PG_SHARED_BUFFERS": "512MB", "PG_EFFECTIVE_CACHE_SIZE": "3GB",
                "PG_MAINTENANCE_WORK_MEM": "256MB", "PG_MAX_WAL_SIZE": "2GB"}
    if ram_gib >= 3:
        return {"PG_SHARED_BUFFERS": "256MB", "PG_EFFECTIVE_CACHE_SIZE": "1GB",
                "PG_MAINTENANCE_WORK_MEM": "128MB", "PG_MAX_WAL_SIZE": "1GB"}
    return {"PG_SHARED_BUFFERS": "128MB", "PG_EFFECTIVE_CACHE_SIZE": "512MB",
            "PG_MAINTENANCE_WORK_MEM": "64MB", "PG_MAX_WAL_SIZE": "512MB", "PG_WORK_MEM": "4MB"}


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
# and anything already mounted alone.
ACTION=="add", SUBSYSTEM=="block", SUBSYSTEMS=="usb", ENV{ID_FS_USAGE}=="filesystem", TAG+="systemd", ENV{SYSTEMD_WANTS}+="sautium-automount@%k.service"
ACTION=="add", SUBSYSTEM=="block", KERNEL=="mmcblk[0-9]*", ENV{ID_FS_USAGE}=="filesystem", TAG+="systemd", ENV{SYSTEMD_WANTS}+="sautium-automount@%k.service"
"""

AUTOMOUNT_UNIT = """\
# Sautium: one plugged disk, mounted read-only while it is there
# (written by ./sautium; installed by the person, as root).
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
verb="${{1:-}}"
name="${{2:-}}"
case "$name" in ""|*[!A-Za-z0-9]*) exit 64 ;; esac     # a kernel name, nothing else
dev="/dev/$name"

if [ "$verb" = unmount ]; then
    [ -r "$STATE/$name" ] || exit 0
    point=$(cat "$STATE/$name")
    case "$point" in ""|*[!A-Za-z0-9-]*) exit 64 ;; esac
    umount -l "$BASE/$point" 2>/dev/null || true
    rmdir "$BASE/$point" 2>/dev/null || true
    rm -f "$BASE/.names/$point" "$STATE/$name"
    exit 0
fi
[ "$verb" = mount ] || exit 64
[ -b "$dev" ] || exit 0

# Never the medium the system runs from, never what something else mounted
root_src=$(findmnt -n -o SOURCE / || true)
root_disk=$(lsblk -n -o PKNAME "$root_src" 2>/dev/null | head -n 1 || true)
dev_disk=$(lsblk -n -o PKNAME "$dev" 2>/dev/null | head -n 1 || true)
[ -n "$dev_disk" ] || dev_disk="$name"
if [ -n "$root_disk" ] && [ "$dev_disk" = "$root_disk" ]; then exit 0; fi
if [ "$dev" = "$root_src" ]; then exit 0; fi
if findmnt -n -S "$dev" >/dev/null 2>&1; then exit 0; fi

type=$(blkid -o value -s TYPE "$dev" || true)
uuid=$(blkid -o value -s UUID "$dev" || true)
case "$uuid" in ""|*[!A-Za-z0-9-]*) exit 0 ;; esac    # no UUID, or not one: left alone
safe="ro,nosuid,nodev,noexec"
owned="uid=${{OWNER%%:*}},gid=${{OWNER##*:}},fmask=0133,dmask=0022"
# FAT keeps its stamps in local time: pinned to UTC, a file's stamp is the
# same number at every mount, whatever this machine's zone or summer time
case "$type" in
    vfat)  fs=vfat;  opts="$safe,$owned,tz=UTC" ;;
    exfat) fs=exfat; opts="$safe,$owned,time_offset=0" ;;
    ntfs)  fs=ntfs3; opts="$safe,$owned" ;;
    ext4)  fs=ext4;  opts="$safe,noload" ;;
    *) exit 0 ;;
esac

point="$uuid"
if findmnt -n "$BASE/$point" >/dev/null 2>&1; then point="$uuid-$name"; fi   # a second volume claiming the UUID
mkdir -p "$BASE/.names" "$STATE"
mkdir -p "$BASE/$point"
if ! mount -t "$fs" -o "$opts" "$dev" "$BASE/$point"; then
    # An exFAT driver that does not know time_offset: mounted without it, said
    if [ "$fs" = exfat ] && mount -t exfat -o "$safe,$owned" "$dev" "$BASE/$point"; then
        echo "sautium-automount: $name mounted without time_offset (this kernel's exfat refused it)" >&2
    else
        rmdir "$BASE/$point" 2>/dev/null || true
        exit 1
    fi
fi
printf '%s\\n' "$point" > "$STATE/$name"
# The volume's label, a stranger's text, as a file the node reads as text
blkid -o value -s LABEL "$dev" 2>/dev/null | head -c 120 > "$BASE/.names/$point" || true
"""


def automount_files(folder: Path, uid: int, gid: int, base: str = AUTOMOUNT_BASE,
                    state: str = "/run/sautium-automount") -> Dict[str, Path]:
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
                                                 state=shlex.quote(state)).encode(), 0o755)
    return files


def automount_installed() -> bool:
    return (os.path.exists("/etc/udev/rules.d/99-sautium-automount.rules")
            and os.path.exists("/usr/local/lib/sautium/automount") and os.path.isdir(AUTOMOUNT_BASE))


def automount_block(files: Dict[str, Path]) -> None:
    q = shlex.quote
    block("Plugged disks (optional)",
          "So that a USB disk or a card with music appears in Sautium when it is plugged in, three small\n"
          "files go into the system. They mount a plugged disk read-only and never write to it.\n"
          f"Read them first: {files['rule'].parent}/",
          [f"sudo install -d -m 0755 {AUTOMOUNT_BASE} {AUTOMOUNT_BASE}/.names /usr/local/lib/sautium",
           f"sudo install -m 0755 {q(str(files['script']))} /usr/local/lib/sautium/automount",
           f"sudo install -m 0644 {q(str(files['rule']))} /etc/udev/rules.d/99-sautium-automount.rules",
           f"sudo install -m 0644 {q(str(files['unit']))} /etc/systemd/system/sautium-automount@.service",
           "sudo systemctl daemon-reload",
           "sudo udevadm control --reload"],
          ["sudo rm -f /etc/udev/rules.d/99-sautium-automount.rules /etc/systemd/system/sautium-automount@.service /usr/local/lib/sautium/automount",
           "sudo systemctl daemon-reload",
           "sudo udevadm control --reload"])


# -- the roots -------------------------------------------------------------------------

# What a container has of its own: a root that is one of these, or holds one
OWN_OF_THE_SYSTEM = ("/", "/app", "/home", "/usr", "/var", "/run", "/etc", "/proc", "/sys", "/dev",
                     "/bin", "/sbin", "/lib", "/lib64", "/boot", "/root", "/opt", "/srv", "/tmp")
# Broader than a folder a person would call music
TOO_BROAD = ("/mnt", "/media")


def root_refusal(path: str, checkout: Path = ROOT) -> Optional[str]:
    """Why `path` may not be a root the node reads music under, or None. The
    roots are the mount, and the mount is the boundary: nothing that is, or
    holds, a part of the system or this checkout, and nothing as broad as a
    whole home."""
    if not os.path.isabs(path):
        return "give the folder's whole path, from /"
    if re.search(r"[\s:,$\"'\\]|[^\x20-\x7e]", path):
        return "the path has a character the setup cannot carry (a space, : , $ a quote, or a non-Latin letter): name the folder plainly"
    real = os.path.realpath(path)
    if not os.path.isdir(real):
        return "there is no such folder"
    parts = Path(real).parts
    for own in OWN_OF_THE_SYSTEM + (str(checkout),):
        own_real = os.path.realpath(own)
        if real == own_real or (own_real + "/").startswith(real.rstrip("/") + "/"):
            return f"it is, or holds, {own}: a folder of the system or of Sautium itself, not a folder of music"
    if real == os.path.realpath(str(checkout)) or (real + "/").startswith(os.path.realpath(str(checkout)) + "/"):
        return "it lies inside the Sautium folder"
    if real in TOO_BROAD or (len(parts) == 3 and parts[1] == "home"):
        return "it is a whole home or every mounted disk at once: name the folder the music is in"
    for system in ("/proc", "/sys", "/dev", "/run", "/etc", "/usr", "/boot", "/bin", "/sbin", "/lib"):
        if (real + "/").startswith(system + "/"):
            return f"it lies inside {system}"
    return None


def roots_override(roots: List[str]) -> str:
    """The compose override that gives the node its roots: each bound
    read-only at its own host path, `rslave` so a disk mounted beneath one
    later reaches the container, and their list in the node's environment.
    Lives in ./host/, which no container mounts: the node cannot widen it."""
    lines = ["# Written by ./sautium: the folders this node may read music from.",
             "# Each is mounted read-only at its own path; change them with",
             "# ./sautium --music <folder>, never by hand.",
             "services:", "  backend:", "    environment:",
             f"      SAUTIUM_LIBRARY_ROOTS: {json.dumps(':'.join(roots))}"]
    if any(os.path.isdir(r) for r in roots):
        lines.append("    volumes:")
    # A root that is not there yet (the automount's folder before the
    # automount is installed) is named and not bound: Docker would make it
    for root in [r for r in roots if os.path.isdir(r)]:
        lines += ["      - type: bind", f"        source: {json.dumps(root)}",
                  f"        target: {json.dumps(root)}", "        read_only: true",
                  "        bind:", "          propagation: rslave"]
    return "\n".join(lines) + "\n"


def held_roots(override: Path) -> List[str]:
    """The roots an earlier run wrote: the override file is their record."""
    raw = agent._read_bounded(override, 65536) or b""
    match = re.search(r'^\s*SAUTIUM_LIBRARY_ROOTS: (".*")\s*$', raw.decode("utf-8", "replace"), re.M)
    try:
        return [r for r in json.loads(match.group(1)).split(":") if r] if match else []
    except ValueError:
        return []


def chosen_roots(music: Optional[str], held: List[str]) -> List[str]:
    """The roots of this install: the folders an earlier run named, a folder
    named now, and — always — the automount's folder, where plugged disks
    appear once the person installed the automount."""
    roots = [r for r in held if r != AUTOMOUNT_BASE]
    if music:
        why = root_refusal(music)
        if why:
            raise Stop(f"--music {music}: {why}")
        real = os.path.realpath(music)
        if real not in roots:
            roots.append(real)
    return roots + [AUTOMOUNT_BASE]


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
             ram_gib: float, assistant: str, volume_exists: bool) -> Dict[str, str]:
    """The keys the setup owns and their values, given what .env holds. A key
    it does not own is never in the plan. The database's password is made
    only at the volume's birth, and a node that already lives is not born
    again: `preset` (read off the machine) is written only before that
    birth. What the IMAGE carries follows the birth at first, what .env
    holds afterwards, and `asked_preset` (--preset) whenever it is given —
    the way an install set up without the models gets them."""
    plan: Dict[str, str] = {}
    if "POSTGRES_PASSWORD" not in held and not volume_exists:
        plan["POSTGRES_PASSWORD"] = secrets.token_urlsafe(24)
    plan.update(pg_settings(ram_gib))
    if not volume_exists and "SAUTIUM_BIRTH_PRESET" not in held and preset:
        plan["SAUTIUM_BIRTH_PRESET"] = preset
    if asked_preset is not None:
        small = asked_preset == "nano"
    elif "SAUTIUM_IMAGE_MODELS" in held:
        small = held["SAUTIUM_IMAGE_MODELS"] == "0"
    else:
        small = preset == "nano"
    plan["SAUTIUM_IMAGE_MODELS"] = plan["SAUTIUM_IMAGE_STREAMING"] = "0" if small else "1"
    plan["SAUTIUM_IMAGE_ASSISTANT_CLI"] = "1" if assistant == "claude" else "0"
    plan["CLAUDE_CODE_ENABLED"] = "true" if assistant == "claude" else "false"
    if "DEFAULT_PROVIDER" not in held:
        plan["DEFAULT_PROVIDER"] = "none"
    plan["COMPOSE_FILE"] = f"{COMPOSE_FILE}:{ROOTS_OVERRIDE}"
    if "MUSIC_LIBRARY_PATH" not in held:
        # The base file's own mount needs a folder: an empty one of the
        # node's, while the node reads its music under the roots
        plan["MUSIC_LIBRARY_PATH"] = NO_LIBRARY
    return plan


def write_env(plan: Dict[str, str]) -> List[str]:
    path = ROOT / ".env"
    if not path.exists():
        agent._write_private(path, b"# Sautium on this machine. Written by ./sautium; your own lines are kept.\n")
    held = agent.read_env(path)
    changed = [key for key, value in plan.items() if held.get(key) != value]
    for key in changed:
        agent.set_env(path, key, plan[key])
    os.chmod(path, 0o600)
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


def build(state: dict, env: Dict[str, str]) -> None:
    """The image, built detached with its log in ./host/: a dropped terminal
    does not end it, and a second run attaches to the same build."""
    key = image_key(env)
    if state.get("built") == key and out(run(["docker", "image", "inspect", "--format", "{{.Id}}",
                                             agent.image_of(ROOT)])):
        return
    host = agent.host_dir(ROOT)
    log, code = host / "setup-build.log", host / "setup-build.exit"
    running = state.get("build") or {}
    if not (running.get("key") == key and running.get("pid") and os.path.exists(f"/proc/{running['pid']}")):
        code.unlink(missing_ok=True)
        command = " ".join(shlex.quote(part) for part in agent.compose_cmd(ROOT) + ["build", "backend"])
        # One retry: a build lost to a passing download failure is common
        script = (f"{command} > {shlex.quote(str(log))} 2>&1 || {command} >> {shlex.quote(str(log))} 2>&1; "
                  f"echo $? > {shlex.quote(str(code))}")
        child = subprocess.Popen(["sh", "-c", script], cwd=str(ROOT), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        running = {"key": key, "pid": child.pid}
        state["build"] = running
        save_state(state)
        say(f"Building Sautium's image (several minutes; its log: {log}) …")
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


def start() -> None:
    say("Starting Sautium (its first start prepares the database: a few minutes) …")
    result = run(agent.compose_cmd(ROOT) + ["up", "-d", "--no-build", "--wait", "--wait-timeout", "2400"],
                 timeout=2500)
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
    found = []
    for line in out(run(["ip", "-o", "-4", "addr", "show"])).splitlines():
        match = re.search(r"\binet (\d+\.\d+\.\d+\.\d+)/", line)
        if match:
            found.append(match.group(1))
    usable = [a for a in ([first] if first else []) + found
              if not ipaddress.ip_address(a).is_loopback and not ipaddress.ip_address(a).is_link_local]
    return list(dict.fromkeys(usable))


def tailnet_address() -> Optional[str]:
    for line in out(run(["ip", "-o", "-4", "addr", "show"])).splitlines():
        match = re.search(r"^\d+:\s+(tailscale\S*)\s+inet (\d+\.\d+\.\d+\.\d+)/", line)
        if match and ipaddress.ip_address(match.group(2)) in ipaddress.ip_network("100.64.0.0/10"):
            return match.group(2)
    return None


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
    return text if SETUP_CODE.fullmatch(text) else None


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


def last_screen(m: dict, preset: Optional[str], passes_on: bool) -> None:
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
        say("  arrive from other Sautium nodes. Change it later in Sautium: More › Profile.")
        say("  Your node asks other Sautium nodes about your artists and recordings and takes the sound")
        say("  features they hold" + (", and answers them in turn." if passes_on else
                                      ", and passes nothing on unless you switch that on") + (
            "" if passes_on else " (More › Sync & P2P)."))
    else:
        say(f"  This machine: {machine_line(m, preset)}.")
    say("  This node answers on your home network. Never forward ports 8800, 8830 or 8831 on your router.")
    say("  Stop: docker compose stop     Start: docker compose up -d     Another device: ./sautium pair")
    say()


# -- the commands ----------------------------------------------------------------------

def release_step() -> None:
    """Born on a release where the clone holds one whose tree has this setup;
    said plainly otherwise. The setup does not pretend to verify its own
    clone: the check that means something is the person's, against the key
    the site publishes."""
    tags = [t for t in out(run(["git", "tag", "--list", "v*", "--sort=-v:refname"])).split()
            if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    newest = tags[0] if tags else None
    if newest is not None and out(run(["git", "rev-parse", f"{newest}^{{commit}}"])) != agent.head(ROOT):
        # Moved to it only where that release carries this setup: an older
        # one would take the setup away from under its own run
        if run(["git", "cat-file", "-e", f"{newest}:scripts/setup.py"]).returncode == 0:
            moved = run(["git", "-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", newest])
            if moved.returncode != 0:
                raise Stop(f"the release {newest} could not be checked out: git status says why")
            say(f"Moved this folder to the release {newest}; its own setup goes on from here.")
            os.execv(sys.executable, [sys.executable, "-I", str(ROOT / "scripts" / "setup.py")] + sys.argv[1:])
        newest = None
    if newest is None:
        say("This folder is not on a signed release (it holds none, or none that carries this setup):")
        say("updates begin with the next release.")
        return
    say(f"This folder is on the release {newest}. To check it against the key Sautium publishes")
    say("(sautium.net › Download), save the key line from that page as release_signers, then:")
    say(f"    git -c gpg.ssh.allowedSignersFile=release_signers verify-tag {newest}")


def root_steps(args, state: dict) -> None:
    """The optional root steps: each printed once as a whole block, then —
    where a person is at the terminal — waited for and checked. Declined,
    each leaves a working node. `--yes` never waits."""
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
    if not automount_installed():
        automount_block(automount_files(agent.host_dir(ROOT) / "automount", uid, gid))
        shown.append("automount")
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
    preset = {"nano": "nano", "full": None}.get(args.preset) if args.preset else machine_tier.birth_preset(
        m["ram_gib"], m["docker_root_removable"], m["docker_root_free_gb"])[0]
    if volume or "SAUTIUM_BIRTH_PRESET" in held:
        preset = held.get("SAUTIUM_BIRTH_PRESET") or None      # a node that lives is not born again
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
    host = agent.host_dir(ROOT)
    host.mkdir(mode=0o700, exist_ok=True)
    root_steps(args, state)

    roots = chosen_roots(args.music, held_roots(ROOT / ROOTS_OVERRIDE))
    agent._write_private(ROOT / ROOTS_OVERRIDE, roots_override(roots).encode(), 0o644)
    (ROOT / "data" / "no-library").mkdir(parents=True, exist_ok=True)
    assistant = args.assistant or ("claude" if held.get("SAUTIUM_IMAGE_ASSISTANT_CLI") == "1" else "none")
    plan = env_plan(held, preset=preset, asked_preset=args.preset, ram_gib=m["ram_gib"],
                    assistant=assistant, volume_exists=volume)
    changed = write_env(plan)
    if changed:
        say(".env: " + ", ".join(k for k in changed if k != "POSTGRES_PASSWORD")
            + (" (and the database's password)" if "POSTGRES_PASSWORD" in changed else ""))

    env = agent.read_env(ROOT / ".env")
    build(state, env)
    if agent.install(ROOT) != 0:
        raise Stop("the host agent could not be installed (its own lines above say why)")
    start()
    state["done"] = True
    save_state(state)
    last_screen(m, preset, passes_on=preset != "nano")
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


def cmd_pair(args) -> int:
    refuse_root()
    answer = signed_get("/api/auth/pin")
    code = str((answer or {}).get("code", ""))
    if not SETUP_CODE.fullmatch(code):
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
    if not agent.read_env(agent.host_dir(ROOT) / "agent.env").get("SAUTIUM_HOST_CHANNEL_KEY"):
        raise Stop("this node has no host agent: run ./sautium to install it")
    requests = ROOT / "run" / "requests"
    try:
        fd = os.open(requests, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        raise Stop(f"the requests folder cannot be opened ({e.strerror}): run ./sautium")
    try:
        os.close(os.open("update", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=fd))
    finally:
        os.close(fd)
    say("Asked for the newest signed release; the host agent is working (an update takes minutes) …")
    log = agent.host_dir(ROOT) / "agent.log"
    before = log.stat().st_size if log.exists() else 0
    # The agent's own unit, waited for: it takes the request just left
    run(["systemctl", "--user", "start", "--wait", "sautium-host-agent.service"], timeout=4 * 3600)
    with open(log, "rb") as f:
        f.seek(before)
        for line in f.read(65536).decode("utf-8", "replace").splitlines():
            say("  " + line)
    return 0


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
    env = agent.read_env(ROOT / ".env")
    roots = [r for r in env.get("SAUTIUM_SETUP_ROOTS", "").split(":") if r] + (
        [AUTOMOUNT_BASE] if automount_installed() else [])
    for root in roots:
        say(f"Music root: {root}  {'there' if os.path.isdir(root) else 'NOT THERE'}")
    for line in out(run(["findmnt", "-rn", "-o", "TARGET,FSTYPE,OPTIONS", "-R", AUTOMOUNT_BASE])).splitlines():
        say("  mounted: " + line)
    tailnet = tailnet_address()
    if tailnet:
        say(f"Tailscale: this machine is {tailnet} on a tailnet; Sautium answers there too.")
        if "Funnel on" in out(run(["tailscale", "funnel", "status"])):
            say("  WARNING: Tailscale Funnel is on. Sautium must never be reachable from the open "
                "internet: turn Funnel off (tailscale funnel reset).")
    low = undervoltage_now()
    if low:
        say("Power: the supply is under its voltage right now.")
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
    # The node stopped first: its own stop removes its port mapping at the router
    if run(agent.compose_cmd(ROOT) + ["stop"], timeout=300).returncode == 0:
        done.append("the node stopped")
    down = ["down", "--remove-orphans", "--rmi", "local"] + (["--volumes"] if args.data else [])
    if run(agent.compose_cmd(ROOT) + down, timeout=600).returncode == 0:
        done.append("containers, networks and images removed" + (", and the database" if args.data else ""))
    for image in {env.get("SAUTIUM_BACKEND_IMAGE"), agent.DEFAULT_IMAGE} - {None, ""}:
        run(["docker", "image", "rm", image])
    if (agent.host_dir(ROOT) / "agent.env").exists() or (agent.unit_dir() / "sautium-host-agent.path").exists():
        agent.uninstall(ROOT)
        done.append("the host agent's units and starter removed")
    for path in (agent.host_dir(ROOT) / "bin", agent.host_dir(ROOT) / "automount"):
        shutil.rmtree(path, ignore_errors=True)
    for name in (ROOTS_OVERRIDE, "host/setup.state", "host/setup-build.log", "host/setup-build.exit"):
        (ROOT / name).unlink(missing_ok=True)
    if args.data:
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
        say("    sudo rm -f /etc/udev/rules.d/99-sautium-automount.rules /etc/systemd/system/sautium-automount@.service /usr/local/lib/sautium/automount")
        say("    sudo systemctl daemon-reload")
        say("    sudo udevadm control --reload")
    say()
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
    setup_flags(sub.add_parser("setup", help="set the node up (the default)"))
    sub.add_parser("status", help="what is installed, what runs")
    sub.add_parser("pair", help="a code and its QR for another device")
    sub.add_parser("update", help="move to the newest signed release")
    remove = sub.add_parser("remove", help="take Sautium off this machine")
    remove.add_argument("--data", action="store_true", help="the database, the identity and the backups too")
    args = parser.parse_args(argv)
    command = {None: cmd_setup, "setup": cmd_setup, "status": cmd_status, "pair": cmd_pair,
               "update": cmd_update, "remove": cmd_remove}[args.command]
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
