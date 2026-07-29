#!/usr/bin/env python3
"""Print UPnP device-description URLs for DLNA renderers.

A companion to the Output picker's scan, for the addresses a scan cannot
reach: a renderer on another subnet, or one joined to the same VPN rather
than the same LAN.

    python3 backend/dlna_locate.py 192.168.1.235      # one device
    python3 backend/dlna_locate.py 100.66.130.110     # one over the tunnel
    python3 backend/dlna_locate.py                    # sweep the local /24
    python3 backend/dlna_locate.py 192.168.7          # sweep another /24

Wake the device first (a dozing KANN or phone stops answering SSDP), then
paste the [RENDERER] location into the Output picker. Media-server entries
of the same device are marked [server] — the picker rejects those.

Printed locations are rehosted onto the address that answered. Devices build
their LOCATION from their own interface address, so one reached across a
tunnel names an address that means nothing here — a phone on mobile data
advertises its carrier-side 10.x while answering perfectly well on the
tunnel. Port and path stay as the device chose them.
"""

import ipaddress
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, urlunparse

MSEARCH = ("M-SEARCH * HTTP/1.1\r\nHOST: {ip}:1900\r\n"
           'MAN: "ssdp:discover"\r\nMX: 2\r\nST: ssdp:all\r\n\r\n')


def rehost(location: str, ip: str) -> str:
    """Point the description URL at the address that answered."""
    parts = urlparse(location)
    if not parts.hostname or parts.hostname == ip:
        return location
    netloc = f"{ip}:{parts.port}" if parts.port else ip
    return urlunparse(parts._replace(netloc=netloc))


def probe(ip: str, wait: float = 2.5) -> set:
    found = set()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.5)
    try:
        s.sendto(MSEARCH.format(ip=ip).encode(), (ip, 1900))
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                data, _ = s.recvfrom(4096)
            except socket.timeout:
                continue
            loc = st = usn = None
            for line in data.decode(errors="replace").splitlines():
                low = line.lower()
                if low.startswith("location:"):
                    loc = line.split(":", 1)[1].strip()
                elif low.startswith("st:"):
                    st = line.split(":", 1)[1].strip()
                elif low.startswith("usn:"):
                    usn = line.split(":", 1)[1].strip()
            if loc:
                found.add((usn or "?", st or "?", rehost(loc, ip)))
    except OSError:
        pass
    finally:
        s.close()
    return found


def local_ip() -> str:
    """The address this host would use to reach the internet.

    Not gethostbyname(gethostname()), which on a multi-homed box answers with
    whichever address the resolver happens to name first — a Hyper-V adapter,
    the docker bridge, or a VPN's own address. Sweeping the /24 around any of
    those finds nothing and reports it as "no devices"."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg.count(".") == 3:
        targets = [arg]
    else:
        net = ipaddress.ip_network(f"{arg or local_ip()}.0/24"
                                   if arg else f"{local_ip()}/24", strict=False)
        targets = [str(h) for h in net.hosts()]
        print(f"sweeping {net} …", file=sys.stderr)

    results = set()
    with ThreadPoolExecutor(max_workers=64) as pool:
        for chunk in pool.map(probe, targets):
            results |= chunk

    if not results:
        print("no UPnP devices answered — wake the device (screen on / "
              "start its network mode) and retry")
        return
    renderers, others = [], []
    for usn, st, loc in sorted(results):
        (renderers if "MediaRenderer" in usn + st else others).append((usn, loc))
    seen = set()
    for tag, rows in (("RENDERER", renderers), ("server", others)):
        for usn, loc in rows:
            if loc in seen:
                continue
            seen.add(loc)
            print(f"[{tag}] {loc}")
            print(f"          {usn}")


if __name__ == "__main__":
    main()
