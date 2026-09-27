"""The address a remote media consumer is handed so it can pull media back
from this node — a DLNA renderer, an HQPlayer on another machine.

A host has several addresses and which one is right depends on who is
asking: a device on the LAN must be told the LAN address, one joined over a
tunnel the tunnel address. Inside Docker none of this is discoverable (every
socket reports the bridge), so the candidates are configured
(MEDIA_PROXY_ADVERTISED_HOST, SAUTIUM_HOST_IPS) and the peer's own address
picks among them. Shared by every output that serves URLs; a backend never
carries its own copy of this rule.
"""
import logging
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)


def _same_network(a: str, b: str) -> bool:
    """Would a device at `b` reach a server at `a` without leaving its own
    network? CGNAT is treated whole because a tailnet hands out /32s from
    100.64/10 with no subnet structure — two peers there reach each other
    regardless of how far apart the addresses look."""
    import ipaddress
    try:
        ia, ib = ipaddress.ip_address(a), ipaddress.ip_address(b)
    except ValueError:
        return False
    cgnat = ipaddress.ip_network("100.64.0.0/10")
    if ia in cgnat or ib in cgnat:
        return ia in cgnat and ib in cgnat
    return (ipaddress.ip_network(f"{a}/24", strict=False)
            == ipaddress.ip_network(f"{b}/24", strict=False))


def _resolve_candidates(entries: list) -> list:
    """Turn configured entries into addresses, accepting names as well.

    Writing our tunnel address into a config file is the wrong shape: it is a
    lease, not a property. The machine's NAME is the stable thing, and on a
    tunnel with MagicDNS it resolves to the current address from anywhere —
    including from inside the container, which cannot ask Tailscale directly
    (no CLI, and the local API is on the host). So `SAUTIUM_HOST_IPS=vh11`
    keeps working after the address changes, where a literal would rot
    silently and hand renderers an address nobody answers on."""
    import ipaddress
    import socket
    out = []
    for entry in entries:
        if not entry:
            continue
        try:
            ipaddress.ip_address(entry)
            resolved = [entry]
        except ValueError:
            try:
                resolved = [socket.gethostbyname(entry)]
            except OSError as e:
                logger.warning("host candidate %r does not resolve (%s)", entry, e)
                resolved = []
        for ip in resolved:
            if ip not in out:
                out.append(ip)
    return out


def _source_address_toward(peer: str) -> Optional[str]:
    """Which of our addresses the kernel would speak from to reach `peer`.

    Costs nothing and sends nothing — connecting a UDP socket only resolves a
    route. This is the authoritative answer wherever the process runs on the
    real host: it needs no configuration and gets tunnels right for free,
    because the route to a tailnet peer leaves by the tailnet interface and
    the kernel says so. It is exactly wrong inside a container, where every
    route ends at the bridge; the caller checks for that."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((peer, 9))
            return s.getsockname()[0]
    except OSError:
        return None


def media_host(peer: Optional[str] = None) -> str:
    """The address to hand a renderer so it can pull media back from us.

    A host has several addresses and which one is correct depends entirely on
    who is asking: a renderer on the LAN must be told the LAN address, one
    joined over a tunnel must be told the tunnel address, and giving either
    the other's is a request that goes nowhere. So `peer` — the renderer's own
    address — picks among our candidates by which network it shares.

    Inside Docker none of this is discoverable: every socket reports the
    bridge address, and what the outside world sees is a NAT the container
    cannot look through. The candidates therefore have to be told to us, via
    MEDIA_PROXY_ADVERTISED_HOST and SAUTIUM_HOST_IPS."""
    import os
    from tls_gen import detect_private_host_ips
    configured = settings.media_proxy_advertised_host
    candidates = _resolve_candidates(
        ([configured] if configured and configured != "127.0.0.1" else [])
        + [s.strip() for s in os.getenv("SAUTIUM_HOST_IPS", "").split(",")])
    if peer:
        match = next((ip for ip in candidates if _same_network(ip, peer)), None)
        if match:
            return match
        routed = _source_address_toward(peer)
        # Accept the kernel's answer only if it lands on the renderer's own
        # network. That is the whole requirement, and it doubles as the
        # container check for free: a bridge address shares a network with
        # nothing outside, so inside Docker this rejects itself and the answer
        # falls back to configuration, where it belongs.
        if routed and _same_network(routed, peer):
            return routed
        logger.warning(
            "no local address on %s's network — media URLs will point at %s "
            "and the renderer will not reach them; add its address to "
            "SAUTIUM_HOST_IPS", peer, candidates[0] if candidates else configured)
    if candidates:
        return candidates[0]
    ips = detect_private_host_ips()
    lan = [ip for ip in ips if not ip.startswith("172.")]
    if lan or ips:
        return (lan or ips)[0]
    return configured


def media_host_for_name(name: str) -> str:
    """`media_host` for a consumer configured by NAME (the HQPlayer host
    setting). A name for this very machine — loopback, or Docker Desktop's
    alias for the host — keeps the configured advertised host verbatim: the
    consumer reaches the published port the same way a browser on the host
    does, and no route lookup could improve on that (inside the container it
    would answer with a bridge address). Any other name is resolved and
    treated as a peer; one that does not resolve gets the configured host,
    the same fallback `media_host` ends on."""
    import socket
    lowered = (name or "").strip().lower()
    if lowered in ("", "localhost", "127.0.0.1", "::1") or lowered.endswith(".docker.internal"):
        return settings.media_proxy_advertised_host
    try:
        ip = socket.gethostbyname(lowered)
    except OSError as e:
        logger.warning("media consumer %r does not resolve (%s) — media URLs "
                       "will name %s", name, e, settings.media_proxy_advertised_host)
        return settings.media_proxy_advertised_host
    if ip.startswith("127."):
        return settings.media_proxy_advertised_host
    return media_host(ip)
