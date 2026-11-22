"""How much machine this is, read from the machine alone — the one rule the
backend's hardware profile, the launcher's wizard and the console setup all
apply (standard library only: the setup runs before anything is installed).

Two readings. `auto_tier`: the tier a running node gives itself from its
accelerator and memory (full / standard / lite; docs/design/HARDWARE-TIERS.md).
`birth_preset`: what a node should be BORN with on a machine too small to
carry the full one — the console setup's choice, written once
(SAUTIUM_BIRTH_PRESET, backend/db_migrate.py); every switch it presets is the
owner's to change afterwards.

And what of the machine this PROCESS may use — `usable_cores`,
`memory_limit_kib`: a container's quota, a slice's limit or a narrowed
affinity (a core kept for an audio daemon beside the node) is the size
every pool and every tier is cut to; `os.cpu_count()` is the machine's and
is read only through `machine_cores`, by the lines that describe the machine.

Every tier figure is in GiB, the unit the box and the spec sheet use.
"""

import ipaddress
import math
import os
import re
from typing import List, Optional, Tuple

TIERS = ("full", "standard", "lite")

# Under this much memory a machine is born Nano: lite's models resident, the
# database and the identity's 2 GiB proof do not fit beside one another
NANO_BELOW_RAM_GIB = 7.5
# Docker's data root must hold the full image (5.5 GB), the models (4 GB) and
# a database that grows: less free than this, and the node is born small
NANO_BELOW_FREE_GB = 25


def auto_tier(device: str, accel_gb: float, ram_gb: float) -> str:
    """Total accelerator memory, not free: a tier describes the machine, not
    the moment. A machine with under 12 GiB of system RAM is demoted one
    tier: on CUDA the whole-track audio buffers live in system RAM."""
    if device == "cuda":
        tier = "full" if accel_gb >= 7.5 else "standard" if accel_gb >= 5.5 else "lite"
    elif device == "mps":
        tier = "full" if accel_gb >= 23 else "standard" if accel_gb >= 15 else "lite"
    else:
        tier = "lite"
    if ram_gb < 12 and tier != "lite":
        tier = TIERS[TIERS.index(tier) + 1]
    return tier


def birth_preset(ram_gib: float, data_root_removable: bool,
                 data_root_free_gb: Optional[float]) -> Tuple[Optional[str], str]:
    """(the preset — "nano" or None for the full node —, why in a few words)
    for a machine with `ram_gib` of memory whose Docker data root is on a
    card or a removable disk or not, with that much free on it."""
    if ram_gib < NANO_BELOW_RAM_GIB:
        return "nano", "under 8 GB of memory"
    if data_root_removable:
        return "nano", "Docker's data is on a card or a removable disk"
    if data_root_free_gb is not None and data_root_free_gb < NANO_BELOW_FREE_GB:
        return "nano", f"under {NANO_BELOW_FREE_GB} GB free for Docker's data"
    return None, "enough memory and disk for the full node"


CGROUP_ROOT = "/sys/fs/cgroup"
CGROUP_MEMBERSHIP = "/proc/self/cgroup"
# cgroup v1 writes "no limit" as the largest page-aligned 64-bit figure
_NO_LIMIT = 1 << 60


def _cgroup_dirs(controller: str, root: Optional[str], membership: Optional[str]) -> List[Tuple[str, int]]:
    """The process's control group for `controller` and every group above it,
    nearest first, each as (directory, hierarchy version). The group is the
    process's own, not the mount's top: a launcher under a systemd slice is
    read as rightly as a container, whose own group IS the top of what it
    sees (a private cgroup namespace, or v1's bind of the group over the
    mount) — there the named path does not exist and the top alone is read."""
    root = root or CGROUP_ROOT
    try:
        with open(membership or CGROUP_MEMBERSHIP) as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    unified = legacy = None
    for line in lines:
        hierarchy, _, rest = line.partition(":")
        controllers, _, path = rest.partition(":")
        if hierarchy == "0" and not controllers:
            unified = path
        elif controller in controllers.split(","):
            legacy = path
    if legacy is not None:
        top, path, version = os.path.join(root, controller), legacy, 1
    elif unified is not None:
        top, path, version = root, unified, 2
    else:
        return []
    parts = [part for part in path.split("/") if part]
    if ".." in parts:           # a group outside this namespace's root
        parts = []
    dirs = [os.path.join(top, *parts[:depth]) for depth in range(len(parts), 0, -1)] + [top]
    return [(d, version) for d in dirs if os.path.isdir(d)]


def _read(path: str) -> Optional[str]:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _cpu_quota(directory: str, version: int) -> Optional[float]:
    """The CPUs' worth of time a group's CFS quota allows, None where it sets none."""
    try:
        if version == 2:
            quota, period = (_read(os.path.join(directory, "cpu.max")) or "max").split()
            if quota == "max":
                return None
        else:
            quota = _read(os.path.join(directory, "cpu.cfs_quota_us")) or "-1"
            period = _read(os.path.join(directory, "cpu.cfs_period_us")) or "0"
        quota, period = int(quota), int(period)
    except ValueError:
        return None
    return quota / period if quota > 0 and period > 0 else None


def machine_cores() -> int:
    """The machine's logical CPUs — for a line that describes the machine.
    What a process may run on is `usable_cores`."""
    return os.cpu_count() or 1


def usable_cores(root: Optional[str] = None, membership: Optional[str] = None) -> int:
    """The CPUs this process may use: its affinity where the platform has
    one, cut to the CFS quota of its control group or of any group above it;
    never under 1. Read when asked — a caller sizes a pool once at its start."""
    try:
        cores = len(os.sched_getaffinity(0))
    except AttributeError:      # macOS, Windows: no affinity to read
        cores = machine_cores()
    for directory, version in _cgroup_dirs("cpu", root, membership):
        quota = _cpu_quota(directory, version)
        if quota is not None:
            cores = min(cores, math.ceil(quota))
    return max(1, cores)


def cgroup_memory_limit(root: Optional[str] = None,
                        membership: Optional[str] = None) -> Optional[Tuple[int, str, int]]:
    """(bytes, the directory of the group that sets it, the hierarchy's
    version) for the smallest memory limit on the process's control group or
    one above it; None where none is set."""
    found = None
    for directory, version in _cgroup_dirs("memory", root, membership):
        text = _read(os.path.join(directory, "memory.max" if version == 2 else "memory.limit_in_bytes"))
        if text is None or text == "max":
            continue
        try:
            limit = int(text)
        except ValueError:
            continue
        if limit < _NO_LIMIT and (found is None or limit < found[0]):
            found = (limit, directory, version)
    return found


def machine_memory_kib() -> Optional[int]:
    """The machine's total memory, None where no reading works."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    try:                        # macOS
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // 1024
    except (ValueError, OSError, AttributeError):
        pass
    try:                        # Windows
        import ctypes

        class _MemStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        status = _MemStatus()
        status.dwLength = ctypes.sizeof(_MemStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.ullTotalPhys // 1024
    except (AttributeError, OSError):
        pass
    return None


def memory_limit_kib(root: Optional[str] = None, membership: Optional[str] = None) -> Optional[int]:
    """The most memory this process may ever have: the smallest limit of its
    control groups where one is set (a capped container is a machine of that
    size), else the machine's total; None where neither can be read. What a
    tier and "this node can never run that" are decided from — the memory
    available is one moment's reading."""
    total = machine_memory_kib()
    limit = cgroup_memory_limit(root, membership)
    if limit is None:
        return total
    return limit[0] // 1024 if total is None else min(total, limit[0] // 1024)


_CONTAINER_BRIDGE = re.compile(
    r"(docker\d*|docker_gwbridge|br-[0-9a-f]{12}|veth\w*|virbr\d+|lxcbr\d+|lxdbr\d+|incusbr\d+"
    r"|cni[\w-]*|podman\d*|vethernet \((wsl.*|default switch)\))$", re.I)


def container_bridge(interface: str) -> bool:
    """Whether a network interface is a container or VM runtime's own
    (Docker's bridge, a compose network's, a pair's end, LXD's, WSL's and
    Hyper-V's default switch on Windows — never a switch a person bridged to
    their network): its address is this machine's and no
    door from another device."""
    return _CONTAINER_BRIDGE.match(interface) is not None


_HOME_NETWORKS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def home_address(address: str) -> bool:
    """Whether an IPv4 address is one a home network gives out (RFC 1918).
    Not `is_private`: that also says yes to documentation and benchmark
    ranges, and to the shared space a carrier or a tailnet uses."""
    try:
        addr = ipaddress.ip_address(address)
    except ValueError:
        return False
    return isinstance(addr, ipaddress.IPv4Address) and any(addr in net for net in _HOME_NETWORKS)
