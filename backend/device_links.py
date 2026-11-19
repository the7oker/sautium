""""Add a device" (PI-NODE-2B §4.2): this node's own addresses as links, each
with its QR code, for the device that is not signed in yet.

The link is the address alone: the other device signs in with the account's
nickname and password, as any browser does. No code is minted here — a
second way to make devices belongs with per-device tokens.

An address is one of this machine's own, read from its interfaces: where
the backend runs on the host's network (the Linux compose file) or under the
launcher. A bridged container sees only its bridge, so there the host's
addresses are the ones its compose file was told (SAUTIUM_HOST_IPS). A
tailnet address is named as such only on an interface called `tailscale*`:
its range, 100.64.0.0/10, is also a phone tether's and a carrier's.
"""

import ipaddress
import os
import socket
from typing import Dict, List

_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _own_addresses() -> List[Dict[str, str]]:
    """{address, kind} for each IPv4 address a device could reach this node
    at: `lan` (a private address) or `tailnet`."""
    import psutil
    from claude_code_runner import IN_DOCKER
    from config import settings
    found: Dict[str, str] = {}
    if IN_DOCKER and not settings.sautium_host_network:
        # A bridge's own address is no door from outside: the host's are told
        for raw in os.getenv("SAUTIUM_HOST_IPS", "").split(","):
            try:
                addr = ipaddress.ip_address(raw.strip())
            except ValueError:
                continue                         # a name: never resolved here
            if isinstance(addr, ipaddress.IPv4Address) and addr.is_private and not addr.is_loopback:
                found[str(addr)] = "lan"
    else:
        for name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family != socket.AF_INET:
                    continue
                addr = ipaddress.ip_address(a.address)
                if addr.is_loopback or addr.is_link_local or addr.is_unspecified:
                    continue
                if addr in _CGNAT:
                    if name.lower().startswith("tailscale"):
                        found[a.address] = "tailnet"
                elif addr.is_private:
                    found[a.address] = "lan"
    return [{"address": address, "kind": kind}
            for address, kind in sorted(found.items(), key=lambda item: (item[1], item[0]))]


def qr_rows(text: str) -> List[str]:
    """`text` as a QR code: its modules row by row, '1' dark, with no quiet
    zone (the page draws its own margin). The page renders it as inline SVG —
    no image is fetched, so the page's content policy needs no exception."""
    import qrcode
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
    code.add_data(text)
    code.make(fit=True)
    return ["".join("1" if dark else "0" for dark in row) for row in code.get_matrix()]


def links(port: int) -> List[Dict[str, object]]:
    """A link per own address, on the port the page itself reached the node
    on (what a bridge publishes is not the port the node listens on)."""
    out = []
    for item in _own_addresses():
        url = f"http://{item['address']}:{port}/"
        out.append({"url": url, "kind": item["kind"], "qr": qr_rows(url)})
    return out
