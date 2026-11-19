"""How much machine this is, read from the machine alone — the one rule the
backend's hardware profile, the launcher's wizard and the console setup all
apply (standard library only: the setup runs before anything is installed).

Two readings. `auto_tier`: the tier a running node gives itself from its
accelerator and memory (full / standard / lite; docs/design/HARDWARE-TIERS.md).
`birth_preset`: what a node should be BORN with on a machine too small to
carry the full one — the console setup's choice, written once
(SAUTIUM_BIRTH_PRESET, backend/db_migrate.py); every switch it presets is the
owner's to change afterwards.

Every figure is in GiB, the unit the box and the spec sheet use.
"""

import ipaddress
import re
from typing import Optional, Tuple

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
