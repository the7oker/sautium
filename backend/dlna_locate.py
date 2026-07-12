#!/usr/bin/env python3
"""Print UPnP device-description URLs for DLNA renderers on the LAN.

The Docker backend cannot discover renderers itself (multicast dies at the
docker bridge and the NAT drops unicast M-SEARCH replies), so its
"Add renderer by address" field needs a full description URL. This tool
runs where UDP works — the WSL shell or any host on the LAN:

    python3 backend/dlna_locate.py 192.168.1.235      # one device
    python3 backend/dlna_locate.py                    # sweep the /24

Wake the device first (a dozing KANN or phone stops answering SSDP), then
paste the [RENDERER] location into the Output picker. Media-server entries
of the same device are marked [server] — the picker rejects those.
"""

import ipaddress
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor

MSEARCH = ("M-SEARCH * HTTP/1.1\r\nHOST: {ip}:1900\r\n"
           'MAN: "ssdp:discover"\r\nMX: 2\r\nST: ssdp:all\r\n\r\n')


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
                found.add((usn or "?", st or "?", loc))
    except OSError:
        pass
    finally:
        s.close()
    return found


def main() -> None:
    if len(sys.argv) > 1:
        targets = [sys.argv[1]]
    else:
        local = socket.gethostbyname(socket.gethostname())
        net = ipaddress.ip_network(f"{local}/24", strict=False)
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
