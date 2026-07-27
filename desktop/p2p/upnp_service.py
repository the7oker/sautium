"""
UPnP / NAT-PMP port mapping for Sautium.

Opens external ports on the router so this node is reachable from
the internet.  Uses miniupnpc (optional dependency).
"""

import logging
from typing import Optional, Tuple

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


def add_mapping(u, external: int, internal: int, protocol: str = "TCP",
                lease: int = LEASE_DURATION, description: str = "") -> bool:
    """Add one port mapping to the IGD. Returns True on success."""
    try:
        return bool(u.addportmapping(
            external, protocol, u.lanaddr, internal,
            description or f"Sautium ({internal})", "", lease))
    except Exception:
        return False


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

    def __init__(self, ports: list[int] = None):
        """
        Args:
            ports: list of internal TCP ports to map (sync server, Docker, etc.)
        """
        self._ports = ports or []
        self._upnp = None
        self._mapped: list[Tuple[int, int]] = []  # (external, internal)
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
        if not HAS_UPNP:
            logger.info("miniupnpc not installed — UPnP disabled")
            return None

        if not self._ports:
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
        """Map a single port, trying the same external port first."""
        port = internal_port
        for _ in range(5):
            if add_mapping(u, port, internal_port):
                logger.info(
                    f"UPnP: {u.externalipaddress()}:{port} -> "
                    f"{u.lanaddr}:{internal_port}"
                )
                return port
            port += 1
        logger.warning(f"UPnP: could not map port {internal_port}")
        return None

    def renew_ports(self) -> bool:
        """Refresh the lease on existing mappings. Routers drop a mapping
        after LEASE_DURATION; re-adding the same (ext, int) pair resets the
        timer. Without this the node silently loses internet reachability
        one hour after launch — outbound still works, so it looks like
        'the friend is offline'. On failure (router rebooted, IGD gone)
        falls back to a full re-discover + re-map. Returns True while at
        least one mapping is active."""
        if not HAS_UPNP:
            return False
        if not self._upnp or not self._mapped:
            return self.open_ports() is not None
        try:
            for ext, intn in list(self._mapped):
                add_mapping(self._upnp, ext, intn)
            logger.debug("UPnP: %d mappings renewed", len(self._mapped))
            return True
        except Exception as e:
            logger.info(f"UPnP renewal failed ({e}) — re-discovering")
            self._mapped.clear()
            self._upnp = None
            self._external_ip = None
            return self.open_ports() is not None

    def close_ports(self):
        """Remove all UPnP port mappings."""
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
