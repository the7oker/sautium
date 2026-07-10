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
            u = miniupnpc.UPnP()
            u.discoverdelay = 2000
            if u.discover() == 0:
                logger.info("No UPnP devices found")
                return None

            u.selectigd()
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
            try:
                result = u.addportmapping(
                    port, "TCP", u.lanaddr, internal_port,
                    f"Sautium ({internal_port})", "", LEASE_DURATION,
                )
                if result:
                    logger.info(
                        f"UPnP: {u.externalipaddress()}:{port} -> "
                        f"{u.lanaddr}:{internal_port}"
                    )
                    return port
            except Exception:
                pass
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
                self._upnp.addportmapping(
                    ext, "TCP", self._upnp.lanaddr, intn,
                    f"Sautium ({intn})", "", LEASE_DURATION,
                )
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
