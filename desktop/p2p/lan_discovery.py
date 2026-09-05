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
from typing import Callable, Dict, Optional, Tuple

from desktop.p2p.addrs import fmt_addr

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
        invite_code: str = "",
        discovery_port: int = LAN_DISCOVERY_PORT,
        localhost_probe_ports: list[int] | None = None,
        on_new_peer: Optional[Callable[[str, int], None]] = None,
    ):
        self.sync_port = sync_port
        self.node_id = node_id
        self.invite_code = invite_code
        self.discovery_port = discovery_port
        self._localhost_probe_ports = localhost_probe_ports or []
        # Fired (on the event loop) the first time an address is seen this
        # session — a beacon, a sibling node on the sender's host, or the
        # localhost Docker probe. The manager syncs on it: a node one hop
        # away is the cheapest source there is.
        self._on_new_peer = on_new_peer

        # {(ip, sync_port): {"node_id": ..., "artists": ..., "invite_code": ..., "last_seen": ...}}
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

    def _local_node_ports(self) -> list[int]:
        """Ports of sibling nodes found on THIS host by the localhost probe
        (a Docker backend). They are announced so LAN peers can reach them
        at OUR address: a container cannot broadcast for itself, and its DHT
        announcement carries the router's external address — which nothing on
        the LAN can connect back to (the backend port is never forwarded)."""
        return sorted({port for (ip, port), info in self._peers.items()
                       if info.get("localhost")})

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

        nodes = [p for p in (msg.get("nodes") or [])
                 if isinstance(p, int) and 0 < p < 65536]
        return (sender_ip, sync_port, {
            "node_id": msg.get("node_id", ""),
            "artists": msg.get("artists", 0),
            "invite_code": msg.get("invite_code", ""),
            "nodes": nodes,
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
        """Periodically broadcast beacon on all network interfaces."""
        while self._running:
            try:
                beacon = self._make_beacon(enriched_artists=0)
                # Send to both global and subnet broadcast addresses.
                # 255.255.255.255 may not cross interfaces on multi-NIC
                # machines (Windows with VMware/VPN adapters).
                targets = {"255.255.255.255"}
                for bcast in self._get_broadcast_addresses():
                    targets.add(bcast)
                for addr in targets:
                    try:
                        await loop.run_in_executor(
                            None,
                            lambda a=addr: self._sock.sendto(
                                beacon, (a, self.discovery_port)
                            ),
                        )
                    except OSError:
                        pass
            except OSError as e:
                logger.debug(f"LAN broadcast failed: {e}")
            await asyncio.sleep(BEACON_INTERVAL)

    @staticmethod
    def _get_broadcast_addresses() -> list[str]:
        """Get subnet broadcast addresses for all network interfaces."""
        import ipaddress
        results = []
        try:
            import psutil
            for name, addrs in psutil.net_if_addrs().items():
                for addr in addrs:
                    if addr.family == socket.AF_INET and addr.netmask:
                        try:
                            net = ipaddress.IPv4Network(
                                f"{addr.address}/{addr.netmask}", strict=False
                            )
                            bcast = str(net.broadcast_address)
                            if bcast != addr.address:
                                results.append(bcast)
                        except ValueError:
                            pass
        except ImportError:
            # psutil not available — fallback to common subnets
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
                s.close()
                # Assume /24
                parts = ip.split(".")
                parts[3] = "255"
                results.append(".".join(parts))
            except Exception:
                pass
        return results

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
                        # Warm up TLS connection in background so first
                        # sync doesn't hit cold-start latency
                        asyncio.ensure_future(
                            self._warmup_peer(loop, ip, port)
                        )
                        if self._on_new_peer:
                            self._on_new_peer(ip, port)
                    # Sibling nodes on the sender's host (its Docker backend):
                    # reachable at the sender's address, invisible otherwise.
                    for extra in info.get("nodes", ()):
                        sib = (ip, extra)
                        if sib == (ip, port):
                            continue
                        sib_new = sib not in self._peers
                        self._peers[sib] = {
                            "node_id": "", "artists": 0,
                            "invite_code": "",
                            "last_seen": info["last_seen"],
                            "sibling_of": info["node_id"],
                        }
                        if sib_new:
                            logger.info(
                                f"LAN peer discovered: {ip}:{extra} "
                                f"(node on peer host)"
                            )
                            asyncio.ensure_future(
                                self._warmup_peer(loop, ip, extra)
                            )
                            if self._on_new_peer:
                                self._on_new_peer(ip, extra)
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

    async def _warmup_peer(
        self, loop: asyncio.AbstractEventLoop, ip: str, port: int
    ):
        """Do a background health check to warm up TLS/ARP/routing state."""
        import ssl
        import urllib.request
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        def _probe():
            try:
                urllib.request.urlopen(
                    f"https://{fmt_addr(ip, port)}/health",
                    timeout=5, context=ctx,
                )
            except Exception:
                pass

        try:
            await loop.run_in_executor(None, _probe)
            logger.debug(f"LAN peer warmup done: {ip}:{port}")
        except Exception:
            pass

    async def _localhost_probe_loop(self):
        """Probe docker_ports on localhost for Docker backends.

        Docker containers can't do UDP broadcast, so we actively probe
        known ports on localhost. Checks /health for a sautium-peer signature.
        """
        import urllib.request
        import ssl

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        while self._running:
            for port in self._localhost_probe_ports:
                if port == self.sync_port:
                    continue
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
                                if self._on_new_peer:
                                    self._on_new_peer("127.0.0.1", port)
                        break
                    except Exception:
                        continue
            await asyncio.sleep(BEACON_INTERVAL)

    def update_enriched_count(self, count: int):
        """Update the artist count sent in beacons."""
        # The broadcast loop reads this on next iteration
        self._enriched_count = count

    def find_peer_by_invite_code(self, invite_code: str) -> Optional[Tuple[str, int]]:
        """Find a LAN peer by invite code. Returns (ip, port) or None."""
        now = time.time()
        for (ip, port), info in self._peers.items():
            if now - info["last_seen"] >= PEER_EXPIRY:
                continue
            if info.get("invite_code") == invite_code:
                return (ip, port)
        return None

    def find_peer_by_node_id(self, node_id: str) -> Optional[Tuple[str, int]]:
        """Find a LAN peer by public key hex. Returns (ip, port) or None."""
        if not node_id:
            return None
        now = time.time()
        for (ip, port), info in self._peers.items():
            if now - info["last_seen"] >= PEER_EXPIRY:
                continue
            if info.get("node_id") == node_id:
                return (ip, port)
        return None

    def _make_beacon(self, enriched_artists: int = 0) -> bytes:
        """Build beacon payload. `nodes` is optional — older peers ignore it,
        so it needs no protocol bump."""
        count = getattr(self, "_enriched_count", enriched_artists)
        payload = {
            "sautium": PROTOCOL_VERSION,
            "node_id": self.node_id,
            "sync_port": self.sync_port,
            "artists": count,
            "invite_code": self.invite_code,
        }
        local_nodes = self._local_node_ports()
        if local_nodes:
            payload["nodes"] = local_nodes
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")
