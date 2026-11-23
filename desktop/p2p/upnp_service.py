"""
UPnP / NAT-PMP port mapping for Sautium.

Opens external ports on the router so this node is reachable from
the internet.  Uses miniupnpc (optional dependency).
"""

import asyncio
import http.client
import ipaddress
import json
import logging
import socket
import ssl
import threading
import urllib.request
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import miniupnpc
    HAS_UPNP = True
except ImportError:
    HAS_UPNP = False

LEASE_DURATION = 3600  # 1 hour


def discover_igd():
    """Find the router's IGD and return a ready miniupnpc handle, or None.

    Shared by the service and the `portmap` CLI — every UPnP operation
    starts here, and a router that answers SSDP is the precondition for
    all of them."""
    if not HAS_UPNP:
        return None
    try:
        u = miniupnpc.UPnP()
        u.discoverdelay = 2000
        if u.discover() == 0:
            return None
        u.selectigd()
        return u
    except Exception as e:
        logger.info(f"UPnP discovery failed: {e}")
        return None


def held_elsewhere(u, external: int, internal: int,
                   protocol: str = "TCP") -> Optional[Tuple[str, int]]:
    """(LAN address, port) the router's mapping of `external` leads to where
    that is not this machine's `internal` port — another device's, or
    another port of this machine. None where the router holds no mapping of
    it, holds this very one, or does not say in a way that can be compared
    (an error, a host name for the client): the mapper then asks as it did
    before it looked, and the check from inside is what is left."""
    try:
        mapping = u.getspecificportmapping(external, protocol)
    except Exception as e:      # miniupnpc raises bare Exception for a router's error
        logger.debug("UPnP: the router did not say who holds port %s: %s", external, e)
        return None
    if not mapping:
        return None
    try:
        held = (ipaddress.ip_address(mapping[0]).compressed, int(mapping[1]))
        here = (ipaddress.ip_address(u.lanaddr).compressed, internal)
    except (ValueError, TypeError) as e:
        logger.debug("UPnP: who holds port %s cannot be compared (the router names %r, this machine %r): %s",
                     external, mapping[0], getattr(u, "lanaddr", None), e)
        return None
    return None if held == here else held


def add_mapping(u, external: int, internal: int, protocol: str = "TCP",
                lease: int = LEASE_DURATION, description: str = "") -> bool:
    """Add one port mapping to the IGD, or renew this machine's own of that
    very port. Never asked over a mapping that leads elsewhere: a router
    that says yes to anyone would hand a neighbour's port over (a second
    node behind one router asked for the first one's 8801, PI-NODE-2B §8.3).
    Returns True on success."""
    if held_elsewhere(u, external, internal, protocol) is not None:
        return False
    try:
        return bool(u.addportmapping(
            external, protocol, u.lanaddr, internal,
            description or f"Sautium ({internal})", "", lease))
    except Exception:
        return False


def answers_as_peer_surface(port: int) -> bool:
    """Whether the local port answers /health over TLS saying `sautium-peer`
    — the peer surface, and nothing else is (the Web UI says `sautium-webui`,
    over plain HTTP; the media proxy and the DLNA event port are plain HTTP
    too). The one gate before any TCP mapping, the CLI's and a node's own:
    only the peer surface may face the internet. A probe that fails is a no."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE      # telling surfaces apart, not trusting one
    try:
        with urllib.request.urlopen(f"https://127.0.0.1:{port}/health",
                                    context=ctx, timeout=3) as resp:
            health = json.loads(resp.read(65536))
    except (OSError, ValueError, http.client.HTTPException):
        return False
    return isinstance(health, dict) and health.get("type") == "sautium-peer"


def leads_here(pubkey: Callable[[], Optional[str]]) -> Callable[[str, int], Optional[bool]]:
    """The check of a mapping from inside, for the node whose key `pubkey()`
    names: dial the router's public address on the mapped port and see whose
    channel answers. True — this node's (the mapping leads here, as far as
    the router's hairpin tells); False — another node's: the port is a
    neighbour's by a forward of the router's own, which its mapping table
    does not list and its yes did not mention; None — nothing learnt (no
    hairpin, no answer, no key yet). One TLS handshake to the node's own
    public address, pinned as any peer's dial is; nothing is sent over it."""
    def check(external_ip: str, external_port: int) -> Optional[bool]:
        from desktop.p2p import peer_auth
        own = pubkey()
        if not own:
            return None
        channel = peer_auth.pinned_ssl_context(own)
        try:
            with socket.create_connection((external_ip, external_port), timeout=3) as raw:
                with channel.wrap_socket(raw):
                    return True
        except OSError:
            return False if peer_auth.seen_pubkey(channel) else None
    return check


def list_mappings(u, limit: int = 200) -> list[dict]:
    """Enumerate the router's port-mapping table."""
    out = []
    for i in range(limit):
        try:
            m = u.getgenericportmapping(i)
        except Exception:
            break
        if m is None:
            break
        out.append({
            "external": m[0], "protocol": m[1],
            "internal_host": m[2][0], "internal_port": m[2][1],
            "description": m[3],
        })
    return out


class UPnPService:
    """Manages UPnP port mappings for P2P connectivity."""

    def __init__(self, ports: list[int] = None, gate: Optional[Callable[[], bool]] = None,
                 leads_here: Optional[Callable[[str, int], Optional[bool]]] = None):
        """
        Args:
            ports: list of internal TCP ports to map (sync server, Docker, etc.)
            gate: asked before every (re)map — a Docker node's check that the
                port answers as the peer surface (answers_as_peer_surface):
                behind a front another process owns it.
            leads_here: (external ip, external port) -> the module's
                leads_here() check, asked of each mapping as it is made and again
                at recheck().
        """
        self._ports = ports or []
        self._gate = gate
        self._leads_here = leads_here
        self._gate_misses = 0
        self._upnp = None
        self._mapped: list[Tuple[int, int]] = []  # (external, internal)
        # One caller of the router at a time: the renewal runs in the loop's
        # executor, and the identity's event asks its recheck from a thread
        self._lock = threading.RLock()
        self._external_ip: Optional[str] = None

    @property
    def is_available(self) -> bool:
        return HAS_UPNP

    @property
    def external_ip(self) -> Optional[str]:
        return self._external_ip

    def get_external_port(self, internal_port: int) -> Optional[int]:
        """Get the external port mapped for a given internal port."""
        for ext, intn in self._mapped:
            if intn == internal_port:
                return ext
        return None

    def open_ports(self) -> Optional[str]:
        """Map all configured ports via UPnP.

        Returns external IP if successful, None otherwise.
        """
        with self._lock:
            return self._open_ports()

    def _open_ports(self) -> Optional[str]:
        if not HAS_UPNP:
            logger.info("miniupnpc not installed — UPnP disabled")
            return None

        if not self._ports:
            return None
        if self._gate is not None and not self._gate():
            logger.warning("UPnP: %s does not answer as the peer surface — not mapped", self._ports)
            return None

        try:
            u = discover_igd()
            if u is None:
                logger.info("No UPnP devices found")
                return None

            external_ip = u.externalipaddress()
            if not external_ip:
                logger.warning("UPnP: could not determine external IP")
                return None

            self._upnp = u
            self._external_ip = external_ip

            for internal_port in self._ports:
                ext = self._map_port(u, internal_port)
                if ext:
                    self._mapped.append((ext, internal_port))

            if self._mapped:
                mapped_str = ", ".join(
                    f"{ext}->{intn}" for ext, intn in self._mapped
                )
                logger.info(f"UPnP: {external_ip} [{mapped_str}]")
            return external_ip

        except Exception as e:
            logger.info(f"UPnP failed: {e}")
            return None

    def _map_port(self, u, internal_port: int) -> Optional[int]:
        """Map a single port, trying the same external port first. A port
        the router maps and another node answers on is given back: the
        announce would name a neighbour."""
        port = internal_port
        for _ in range(5):
            if add_mapping(u, port, internal_port):
                if self._leads_here is None or self._leads_here(self._external_ip, port) is not False:
                    logger.info(
                        f"UPnP: {self._external_ip}:{port} -> "
                        f"{u.lanaddr}:{internal_port}"
                    )
                    return port
                logger.warning("UPnP: the router mapped %d, but another node answers there "
                               "(a forward of the router's own) — mapping removed", port)
                try:
                    u.deleteportmapping(port, "TCP")
                except Exception as e:
                    logger.info(f"UPnP: the mapping of {port} could not be removed: {e}")
            port += 1
        logger.warning(f"UPnP: could not map port {internal_port}")
        return None

    def renew_ports(self) -> bool:
        """Refresh the lease on existing mappings. Routers drop a mapping
        after LEASE_DURATION; re-adding the same (ext, int) pair resets the
        timer. Without this the node silently loses internet reachability
        one hour after launch — outbound still works, so it looks like
        'the friend is offline'. A mapping the router will not renew — or
        that now leads elsewhere — is lost and forgotten: the node stops
        announcing its port. With none left (router rebooted, IGD gone)
        falls back to a full re-discover + re-map. Returns True while at
        least one mapping is active."""
        if not HAS_UPNP:
            return False
        with self._lock:
            if self._gate is not None and self._mapped:
                # Two renewals in a row: one probe the node's own busy loop was
                # slow to answer is no reason to drop the mapping for a lease.
                self._gate_misses = 0 if self._gate() else self._gate_misses + 1
                if self._gate_misses >= 2:
                    logger.warning("UPnP: %s no longer answers as the peer surface — mapping removed",
                                   self._ports)
                    self.close_ports()
                    return False
            if self._upnp and self._mapped:
                for ext, intn in list(self._mapped):
                    if not add_mapping(self._upnp, ext, intn):
                        logger.warning("UPnP: the mapping %d -> %d was not renewed — lost", ext, intn)
                        self._mapped.remove((ext, intn))
                if self._mapped:
                    logger.debug("UPnP: %d mappings renewed", len(self._mapped))
                    return True
                logger.info("UPnP: no mapping left after renewal — re-discovering")
                self._upnp = None
                self._external_ip = None
            self.open_ports()
            return bool(self._mapped)

    def recheck(self) -> bool:
        """Ask the check from inside again of every mapping held — for the
        moment the key it is pinned to appears or changes after the mapping
        was made (an account created under a running node: its first check
        had no key and learnt nothing). A mapping another node answers on is
        given back and the next port taken, as at the first map. True where
        a mapping changed."""
        with self._lock:
            if not (self._upnp and self._mapped and self._leads_here):
                return False
            changed = False
            for ext, intn in list(self._mapped):
                if self._leads_here(self._external_ip, ext) is not False:
                    continue
                logger.warning("UPnP: another node answers on the mapped port %d "
                               "(a forward of the router's own) — mapping removed", ext)
                try:
                    self._upnp.deleteportmapping(ext, "TCP")
                except Exception as e:      # miniupnpc raises bare Exception for a router's error
                    logger.info(f"UPnP: the mapping of {ext} could not be removed: {e}")
                self._mapped.remove((ext, intn))
                changed = True
                if self._gate is not None and not self._gate():
                    logger.warning("UPnP: %d does not answer as the peer surface — not mapped again", intn)
                    continue
                taken = self._map_port(self._upnp, intn)
                if taken:
                    self._mapped.append((taken, intn))
            return changed

    async def keep_mapped(self, internal_port: int, on_external: Callable[[int], None]) -> None:
        """Re-map before the 1-hour lease expires (at 75% of LEASE_DURATION)
        — both runtimes' renewal. Lease renewal is a timer-based protocol
        requirement, not state polling: without it the router silently drops
        the mapping after an hour and the node loses internet reachability
        (outbound still works, so it just looks like 'the friend is
        offline'). renew_ports() also recovers a rebooted router via full
        re-discover; an external port that changed goes to `on_external`
        (the DHT's announce port, which the 15-min re-announce publishes) —
        None when the mapping is gone."""
        interval = LEASE_DURATION * 0.75
        loop = asyncio.get_running_loop()
        last = self.get_external_port(internal_port)
        while True:
            await asyncio.sleep(interval)
            try:
                if not await loop.run_in_executor(None, self.renew_ports):
                    logger.info("UPnP: no active mapping after renewal attempt")
                ext_port = self.get_external_port(internal_port)
                if ext_port != last:
                    on_external(ext_port)
                last = ext_port
            except Exception as e:
                logger.warning(f"UPnP renewal loop error: {e}")

    def close_ports(self):
        """Remove all UPnP port mappings."""
        with self._lock:
            if not self._upnp:
                return
            for ext, _ in self._mapped:
                try:
                    self._upnp.deleteportmapping(ext, "TCP")
                except Exception:
                    pass
            if self._mapped:
                logger.info(f"UPnP: {len(self._mapped)} mappings removed")
            self._mapped.clear()
            self._external_ip = None
