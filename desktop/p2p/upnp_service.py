"""
UPnP / NAT-PMP port mapping for Sautium.

Opens an external port on the router so this node is reachable from
the internet.  Uses miniupnpc (optional dependency).

Usage:
    upnp = UPnPService(internal_port=19000)
    result = upnp.open_port()
    if result:
        external_ip, external_port = result
        # Use in DHT announce
    upnp.close_port()   # on shutdown
"""

import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import miniupnpc
    HAS_UPNP = True
except ImportError:
    HAS_UPNP = False

# Lease duration in seconds (0 = permanent until reboot/close)
LEASE_DURATION = 3600  # 1 hour, re-map periodically


class UPnPService:
    """Manages a single UPnP port mapping for the sync server."""

    def __init__(self, internal_port: int = 19000, description: str = "Sautium P2P"):
        self.internal_port = internal_port
        self.description = description
        self._upnp = None
        self._external_port: Optional[int] = None
        self._external_ip: Optional[str] = None

    @property
    def is_available(self) -> bool:
        return HAS_UPNP

    @property
    def external_address(self) -> Optional[Tuple[str, int]]:
        """Return (external_ip, external_port) if mapped, else None."""
        if self._external_ip and self._external_port:
            return (self._external_ip, self._external_port)
        return None

    def open_port(self) -> Optional[Tuple[str, int]]:
        """Try to open external port via UPnP. Returns (ip, port) or None."""
        if not HAS_UPNP:
            logger.info("miniupnpc not installed — UPnP disabled")
            return None

        try:
            u = miniupnpc.UPnP()
            u.discoverdelay = 2000  # ms
            devices = u.discover()
            if devices == 0:
                logger.info("No UPnP devices found on network")
                return None

            u.selectigd()
            external_ip = u.externalipaddress()
            if not external_ip:
                logger.warning("UPnP: could not determine external IP")
                return None

            # Try to map our internal port to the same external port
            port = self.internal_port
            for attempt in range(5):
                try:
                    result = u.addportmapping(
                        port,           # external port
                        "TCP",
                        u.lanaddr,      # internal IP
                        self.internal_port,
                        self.description,
                        "",             # remote host (any)
                        LEASE_DURATION,
                    )
                    if result:
                        self._upnp = u
                        self._external_ip = external_ip
                        self._external_port = port
                        logger.info(
                            f"UPnP mapped {external_ip}:{port} -> "
                            f"{u.lanaddr}:{self.internal_port}"
                        )
                        return (external_ip, port)
                except Exception:
                    pass
                # Port taken, try next
                port += 1

            logger.warning("UPnP: could not map any port after 5 attempts")
            return None

        except Exception as e:
            logger.info(f"UPnP failed: {e}")
            return None

    def close_port(self):
        """Remove the UPnP port mapping."""
        if self._upnp and self._external_port:
            try:
                self._upnp.deleteportmapping(self._external_port, "TCP")
                logger.info(f"UPnP mapping removed (port {self._external_port})")
            except Exception as e:
                logger.debug(f"UPnP close failed: {e}")
            self._external_port = None
            self._external_ip = None

    def get_lan_ip(self) -> Optional[str]:
        """Return LAN IP from UPnP discovery (reliable, no guessing)."""
        if self._upnp:
            return self._upnp.lanaddr
        # Fallback: quick UPnP discover just for LAN IP
        if not HAS_UPNP:
            return None
        try:
            u = miniupnpc.UPnP()
            u.discoverdelay = 1000
            if u.discover() > 0:
                u.selectigd()
                return u.lanaddr
        except Exception:
            pass
        return None
