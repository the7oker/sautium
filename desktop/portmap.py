"""Open a port on the router from the command line (Windows / macOS / Linux).

    python -m desktop.portmap status
    python -m desktop.portmap map 27846
    python -m desktop.portmap list
    python -m desktop.portmap unmap 27846

Why this exists: a Sautium node in Docker cannot map its own port. UPnP
discovery is SSDP multicast, which does not survive the container's bridge
network (nor, on Windows, the WSL VM behind it), so the mapping has to be
requested from the host — that is what this does.

Mappings are attempted as permanent first; routers that reject a zero lease
get LEASE_DURATION and the command says so, because such a mapping silently
expires and has to be refreshed (`--keep` does that in the foreground).

SAFETY: this refuses to map a port that answers with the Sautium Web UI.
That page carries the API secret inline (window.__SAUTIUM_SECRET), so
exposing it to the internet hands over the whole API — see the Security
Posture section in CLAUDE.md. The P2P sync surface is the one that may
face the internet; the Web UI never is.
"""

import argparse
import socket
import ssl
import sys
import time
import urllib.request

from desktop.p2p.upnp_service import (
    HAS_UPNP, LEASE_DURATION, add_mapping, discover_igd, list_mappings,
)

# Refresh at 75% of the lease — the same margin the launcher's renewal uses.
RENEW_RATIO = 0.75
# A permanent mapping still dies with a router reboot; --keep re-asserts it
# on this cadence (add_mapping is idempotent) so the dead window after a
# reboot is minutes, not "until someone notices" — the master's 8801 was
# down for days exactly that way (2026-08-20, found by an external probe).
PERMANENT_REASSERT_SECONDS = 15 * 60


def _igd_or_exit():
    if not HAS_UPNP:
        sys.exit("miniupnpc is not installed - run: pip install miniupnpc")
    u = discover_igd()
    if u is None:
        sys.exit("No UPnP router found. Check that UPnP is enabled on the "
                 "router, and that this machine is on the same network.")
    return u


def _serves_web_ui(port: int) -> bool:
    """True if the local port answers with the secret-carrying Web UI."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for scheme in ("https", "http"):
        try:
            kw = {"timeout": 3}
            if scheme == "https":
                kw["context"] = ctx
            body = urllib.request.urlopen(
                f"{scheme}://127.0.0.1:{port}/", **kw).read(65536)
            if b"__SAUTIUM_SECRET" in body:
                return True
        except Exception:
            continue
    return False


def _port_is_listening(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        return s.connect_ex((host, port)) == 0


def cmd_status(args) -> None:
    u = _igd_or_exit()
    print(f"router          : UPnP available")
    print(f"external ip     : {u.externalipaddress()}")
    print(f"this machine    : {u.lanaddr}")
    mine = [m for m in list_mappings(u) if m["internal_host"] == u.lanaddr]
    print(f"mappings (mine) : {len(mine)}")
    for m in mine:
        print(f"  {m['external']}/{m['protocol']} -> {m['internal_port']}"
              f"  {m['description']}")


def cmd_list(args) -> None:
    u = _igd_or_exit()
    rows = list_mappings(u)
    if not rows:
        print("no port mappings on the router")
        return
    print(f"{'EXT':>6} {'PROTO':<5} {'-> INTERNAL':<24} DESCRIPTION")
    for m in rows:
        target = f"{m['internal_host']}:{m['internal_port']}"
        print(f"{m['external']:>6} {m['protocol']:<5} {target:<24} "
              f"{m['description']}")


def cmd_map(args) -> None:
    port = args.port
    external = args.external or port
    proto = "UDP" if args.udp else "TCP"

    if proto == "TCP" and _serves_web_ui(port):
        sys.exit(
            f"Refusing to map {port}: it serves the Sautium Web UI, whose "
            f"page contains the API secret. Exposing it would hand over the "
            f"whole API. Map the P2P sync port instead.")

    u = _igd_or_exit()
    if proto == "TCP" and not _port_is_listening(port):
        print(f"warning: nothing is listening on 127.0.0.1:{port} - mapping "
              f"it anyway, but peers will get a closed port until it is up")

    lease = args.lease
    permanent = lease == 0
    if not add_mapping(u, external, port, proto, lease, args.name):
        if permanent:
            # Many routers reject a zero (indefinite) lease outright.
            lease = LEASE_DURATION
            permanent = False
            if not add_mapping(u, external, port, proto, lease, args.name):
                sys.exit(f"Router refused to map {external}/{proto}. Another "
                         f"device may already hold that external port - try "
                         f"--external with a different number.")
        else:
            sys.exit(f"Router refused to map {external}/{proto}.")

    print(f"mapped {u.externalipaddress()}:{external}/{proto} -> "
          f"{u.lanaddr}:{port}")

    # A router mapping is only half the path: the port must also answer on
    # this machine's LAN address. A published Docker port that listens
    # IPv6-only (Docker Desktop does this) passes a loopback check and still
    # refuses every LAN and internet connection.
    if proto == "TCP" and not _port_is_listening(port, u.lanaddr):
        print(f"warning: {u.lanaddr}:{port} does not accept IPv4 connections, "
              f"so the mapping cannot work yet")
        if sys.platform == "win32":
            print(f"         on Windows a published Docker port is often "
                  f"IPv6-only; it needs (as admin):")
            print(f"           netsh interface portproxy add v4tov4 "
                  f"listenaddress=0.0.0.0 listenport={port} "
                  f"connectaddress=<WSL-IP> connectport={port}")
            print(f"           netsh advfirewall firewall add rule "
                  f'name="Sautium P2P (TCP {port})" dir=in action=allow '
                  f"protocol=TCP localport={port} profile=any")
    if permanent:
        print("lease: permanent (survives until removed or the router reboots)")
        if not args.keep:
            return
        every = PERMANENT_REASSERT_SECONDS
        print(f"       holding it open anyway, re-asserting every {every}s so "
              f"a router reboot costs minutes, not days (Ctrl-C to stop)")
    else:
        print(f"lease: {lease}s - this router refused a permanent mapping, "
              f"so it expires")
        if not args.keep:
            print(f"       re-run this command before it lapses, or use "
                  f"--keep to hold it open")
            return
        every = max(60, int(lease * RENEW_RATIO))
        print(f"       holding it open, refreshing every {every}s "
              f"(Ctrl-C to stop)")
    try:
        while True:
            time.sleep(every)
            if add_mapping(u, external, port, proto, lease, args.name):
                continue
            # Router rebooted or dropped the IGD session — rediscover once.
            u = discover_igd()
            if u is None or not add_mapping(u, external, port, proto, lease,
                                            args.name):
                sys.exit("lost the mapping and could not restore it")
    except KeyboardInterrupt:
        print("\nstopped refreshing (the mapping stays until its lease ends)")


def cmd_unmap(args) -> None:
    u = _igd_or_exit()
    proto = "UDP" if args.udp else "TCP"
    try:
        ok = u.deleteportmapping(args.port, proto)
    except Exception:
        ok = False
    print(f"removed {args.port}/{proto}" if ok
          else f"no mapping for {args.port}/{proto} (already gone?)")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m desktop.portmap",
        description="Open/close ports on the router via UPnP.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="router, external IP, this machine's maps"
                   ).set_defaults(func=cmd_status)
    sub.add_parser("list", help="every mapping the router holds"
                   ).set_defaults(func=cmd_list)

    m = sub.add_parser("map", help="open a port")
    m.add_argument("port", type=int, help="local port to expose")
    m.add_argument("--external", type=int,
                   help="external port (default: same as local)")
    m.add_argument("--udp", action="store_true", help="map UDP instead of TCP")
    m.add_argument("--lease", type=int, default=0,
                   help="lease seconds; 0 = permanent (default)")
    m.add_argument("--name", default="", help="description shown on the router")
    m.add_argument("--keep", action="store_true",
                   help="stay running and refresh a non-permanent lease")
    m.set_defaults(func=cmd_map)

    un = sub.add_parser("unmap", help="remove a mapping")
    un.add_argument("port", type=int)
    un.add_argument("--udp", action="store_true")
    un.set_defaults(func=cmd_unmap)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
