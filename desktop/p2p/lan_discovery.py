"""
LAN peer discovery for Sautium via UDP broadcast.

Complements DHT (which works over the internet but fails for peers
behind the same NAT/router).  Each node periodically broadcasts a
small beacon on the LAN; other nodes collect beacons and expose
discovered peers for sync.

Protocol (JSON over UDP broadcast, port 19002):
  {
    "sautium": 1,           # protocol version
    "node_id": "...",        # Ed25519 public key hex (or empty)
    "sync_port": 19000,      # HTTP sync server port
    "artists": 42            # enriched artist count (hint)
  }

Security: beacons are unsigned discovery hints only.  The actual
sync uses HTTPS with certificate pinning.
"""

import asyncio
import json
import logging
import socket
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

LAN_DISCOVERY_PORT = 19002
BEACON_INTERVAL = 30          # seconds between broadcasts
PEER_EXPIRY = BEACON_INTERVAL * 4  # remove peer after 4 missed beacons
PROTOCOL_VERSION = 1



class LANDiscovery:
    """Broadcast/listen for Sautium peers on the local network."""

    def __init__(
        self,
        sync_port: int = 19000,
        node_id: str = "",
        discovery_port: int = LAN_DISCOVERY_PORT,
        localhost_probe_ports: list[int] | None = None,
    ):
        self.sync_port = sync_port
        self.node_id = node_id
        self.discovery_port = discovery_port
        self._localhost_probe_ports = localhost_probe_ports or []

        # {(ip, sync_port): {"node_id": ..., "artists": ..., "last_seen": ...}}
        self._peers: Dict[Tuple[str, int], dict] = {}
        self._running = False
        self._sock: Optional[socket.socket] = None

    @property
    def peers(self) -> list[Tuple[str, int]]:
        """Return list of (ip, sync_port) for live LAN peers."""
        now = time.time()
        alive = []
        expired = []
        for key, info in self._peers.items():
            if now - info["last_seen"] < PEER_EXPIRY:
                alive.append(key)
            else:
                expired.append(key)
        for key in expired:
            del self._peers[key]
        return alive

    def get_peer_info(self, ip: str, port: int) -> Optional[dict]:
        """Get metadata for a specific LAN peer."""
        return self._peers.get((ip, port))

    def _make_beacon(self, enriched_artists: int = 0) -> bytes:
        """Build beacon payload."""
        return json.dumps({
            "sautium": PROTOCOL_VERSION,
            "node_id": self.node_id,
            "sync_port": self.sync_port,
            "artists": enriched_artists,
        }, separators=(",", ":")).encode("utf-8")

    def _parse_beacon(self, data: bytes, sender_ip: str) -> Optional[Tuple[str, int, dict]]:
        """Parse beacon, return (ip, sync_port, info) or None."""
        try:
            msg = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

        if msg.get("sautium") != PROTOCOL_VERSION:
            return None

        sync_port = msg.get("sync_port")
        if not isinstance(sync_port, int):
            return None

        # Ignore our own beacons
        if msg.get("node_id") and msg["node_id"] == self.node_id:
            return None

        return (sender_ip, sync_port, {
            "node_id": msg.get("node_id", ""),
            "artists": msg.get("artists", 0),
        })

    async def start(self):
        """Start broadcasting and listening."""
        if self._running:
            return

        self._running = True
        loop = asyncio.get_event_loop()

        # Create UDP socket for both sending and receiving
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # SO_REUSEPORT allows multiple processes on same machine (Mac Docker + Desktop)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass  # Not available on all platforms
        sock.setblocking(False)
        sock.bind(("", self.discovery_port))
        self._sock = sock

        # Start listener, broadcaster, and localhost prober
        self._listen_task = asyncio.create_task(self._listen_loop(loop))
        self._broadcast_task = asyncio.create_task(self._broadcast_loop(loop))
        self._probe_task = asyncio.create_task(self._localhost_probe_loop())

        logger.info(
            f"LAN discovery started on UDP port {self.discovery_port} "
            f"(sync port {self.sync_port})"
        )

    async def stop(self):
        """Stop discovery."""
        self._running = False
        for task in (
            getattr(self, "_listen_task", None),
            getattr(self, "_broadcast_task", None),
            getattr(self, "_probe_task", None),
        ):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._sock:
            self._sock.close()
            self._sock = None

        logger.info("LAN discovery stopped")

    async def _broadcast_loop(self, loop: asyncio.AbstractEventLoop):
        """Periodically broadcast beacon."""
        while self._running:
            try:
                beacon = self._make_beacon(enriched_artists=0)
                await loop.run_in_executor(
                    None,
                    lambda: self._sock.sendto(
                        beacon, ("255.255.255.255", self.discovery_port)
                    ),
                )
            except OSError as e:
                logger.debug(f"LAN broadcast failed: {e}")
            await asyncio.sleep(BEACON_INTERVAL)

    async def _listen_loop(self, loop: asyncio.AbstractEventLoop):
        """Listen for beacons from other peers."""
        while self._running:
            try:
                data, addr = await loop.run_in_executor(
                    None, lambda: self._receive_with_timeout()
                )
                if data is None:
                    continue

                sender_ip = addr[0]
                parsed = self._parse_beacon(data, sender_ip)
                if parsed:
                    ip, port, info = parsed
                    key = (ip, port)
                    is_new = key not in self._peers
                    info["last_seen"] = time.time()
                    self._peers[key] = info
                    if is_new:
                        logger.info(
                            f"LAN peer discovered: {ip}:{port} "
                            f"({info['artists']} artists)"
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"LAN listen error: {e}")
                await asyncio.sleep(1)

    def _receive_with_timeout(self, timeout: float = 2.0):
        """Blocking receive with timeout (run in executor)."""
        import select
        if self._sock is None:
            return None, None
        ready, _, _ = select.select([self._sock], [], [], timeout)
        if ready:
            try:
                return self._sock.recvfrom(1024)
            except OSError:
                return None, None
        return None, None

    async def _localhost_probe_loop(self):
        """Probe localhost ports for Docker/other backends that can't broadcast.

        Checks /health for a Sautium peer signature, then registers it
        as a discovered peer.  Skips our own sync_port.
        """
        import urllib.request
        import ssl

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        while self._running:
            for port in self._localhost_probe_ports:
                if port == self.sync_port:
                    continue  # skip ourselves
                # Try HTTPS first (desktop sync servers), then HTTP (Docker)
                for scheme in ("https", "http"):
                    try:
                        url = f"{scheme}://127.0.0.1:{port}/health"
                        kw = {"timeout": 2}
                        if scheme == "https":
                            kw["context"] = ctx
                        req = urllib.request.urlopen(url, **kw)
                        data = json.loads(req.read().decode())
                        if data.get("type") == "sautium-peer":
                            key = ("127.0.0.1", port)
                            is_new = key not in self._peers
                            self._peers[key] = {
                                "node_id": "",
                                "artists": 0,
                                "last_seen": time.time(),
                                "localhost": True,
                                "scheme": scheme,
                            }
                            if is_new:
                                logger.info(
                                    f"Localhost peer discovered: "
                                    f"{scheme}://127.0.0.1:{port}"
                                )
                        break  # found it, don't try next scheme
                    except Exception:
                        continue
            await asyncio.sleep(BEACON_INTERVAL)

    def update_enriched_count(self, count: int):
        """Update the artist count sent in beacons."""
        # The broadcast loop reads this on next iteration
        self._enriched_count = count

    def _make_beacon(self, enriched_artists: int = 0) -> bytes:
        """Build beacon payload."""
        count = getattr(self, "_enriched_count", enriched_artists)
        return json.dumps({
            "sautium": PROTOCOL_VERSION,
            "node_id": self.node_id,
            "sync_port": self.sync_port,
            "artists": count,
        }, separators=(",", ":")).encode("utf-8")
