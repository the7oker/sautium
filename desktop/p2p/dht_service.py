"""
libtorrent DHT service for Sautium.

Two announce layers, by design (2026-07-11):

  1. NODE — one key, SHA1("Sautium-node"), announced by every node. This is
     the discovery highway: a peer finds nodes with ONE lookup and then asks
     each for an inventory, which answers "what do you have of mine?" for a
     whole library in a single HTTP call. Announcing every artist instead
     never paid for itself — the sync flow only ever used those per-artist
     announcements as a generator of *some* productive peer, and a library
     of N artists cost N announcements to answer a question one answers.
  2. TAIL — targeted SHA1("Sautium-artist:" + uuid) keys for a bounded set
     of the node's RAREST owned artists. Announcing a popular artist is
     wasted traffic (every second node has them, inventory finds them
     anyway); a rare one is invisible to a random peer sample, so it earns
     an exact key. Budget ~300 keys instead of thousands.

Bucketing artists into a fixed number of slots was considered and rejected:
slot occupancy is driven by the ANNOUNCER's library size, so with thousands
of artists every slot is occupied on every node — a slot lookup returns
~the whole network, i.e. exactly what the node key returns, for a thousand
times the announce cost.

The node key deliberately has no shard suffix yet. When the network outgrows
one key (hot-key eviction on the 8 nodes storing it), nodes start ALSO
announcing SHA1("Sautium-node" + prefix) levels; the global key stays, so
old and new clients keep seeing each other.

MIRRORS backend/dht_service.py (the launcher build cannot import backend
modules) — the announce/lookup key formats are the wire contract.
"""

import asyncio
import hashlib
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import libtorrent as lt
    HAS_LIBTORRENT = True
except ImportError:
    HAS_LIBTORRENT = False
    logger.warning("libtorrent not installed — DHT disabled")


# Prefix for artist infohash computation
INFOHASH_PREFIX = "Sautium-artist:"
INFOHASH_PREFIX_USER = "Sautium-user:"
INFOHASH_PREFIX_CAP = "Sautium-cap:"
INFOHASH_PREFIX_NODE = "Sautium-node"

# DHT re-announce interval (seconds)
REANNOUNCE_INTERVAL = 15 * 60  # 15 minutes

# dht_announce initiations per pacing pause. Each announce fans out into a
# get_peers traversal, so a tight loop floods the host NAT with UDP and
# starves concurrent flows — on the Docker master it measurably knocked out
# the HQPlayer control socket every 15 minutes.
# 5/s: initiation must stay under the DHT's traversal-completion rate or the
# backlog of concurrent traversals accumulates through the window and
# saturates the path anyway (measured: 25/s still produced timeout bursts
# near the END of the paced window and ~1 min past it). The mass per-artist
# sweep this pacing was built for is gone (node + tail, see module
# docstring): a ~300-key tail takes ~60s of the 15-min cycle.
ANNOUNCE_CHUNK = 5
ANNOUNCE_CHUNK_PAUSE = 1.0

# How long to wait for DHT bootstrap (seconds)
DHT_BOOTSTRAP_TIMEOUT = 30

# How long to wait for get_peers results (seconds)
GET_PEERS_TIMEOUT = 30

# Peer cache TTL (seconds)
PEER_CACHE_TTL = 30 * 60  # 30 minutes

# Bootstrap DHT routers
DHT_ROUTERS = [
    ("router.bittorrent.com", 6881),
    ("dht.transmissionbt.com", 6881),
    ("router.utorrent.com", 6881),
]


def artist_infohash(artist_uuid: str) -> bytes:
    """Compute SHA1 infohash for an artist UUID."""
    return hashlib.sha1(
        f"{INFOHASH_PREFIX}{artist_uuid}".encode()
    ).digest()


def user_infohash(invite_code: str) -> bytes:
    """Compute SHA1 infohash for a user invite code (for DHT announce/lookup)."""
    return hashlib.sha1(
        f"{INFOHASH_PREFIX_USER}{invite_code}".encode()
    ).digest()


def capability_infohash(capability: str) -> bytes:
    """Compute SHA1 infohash for a node capability (e.g. 'mbdump' — the node
    serves MB dump slices). One well-known infohash per capability lets
    dump-less nodes discover volunteer dump holders beyond known peers."""
    return hashlib.sha1(
        f"{INFOHASH_PREFIX_CAP}{capability}".encode()
    ).digest()


def node_infohash(prefix: str = "") -> bytes:
    """Compute SHA1 infohash of the node-discovery key. `prefix` is the
    (currently unused) shard level — empty means the global key every node
    announces; see the module docstring for the growth path."""
    return hashlib.sha1(
        f"{INFOHASH_PREFIX_NODE}{prefix}".encode()
    ).digest()


class DHTService:
    """libtorrent-based DHT service for per-artist peer discovery."""

    def __init__(self, listen_port: int = 19001, http_port: int = 19000):
        """
        Args:
            listen_port: UDP port for libtorrent DHT operations
            http_port: TCP port of the HTTP sync server (announced to peers)
        """
        self.listen_port = listen_port
        self.http_port = http_port
        self._announce_port = http_port  # may differ if UPnP maps to another port
        self._session: Optional[object] = None  # lt.session
        self._announced: set[str] = set()  # artist UUIDs currently announced
        self._user_invite_code: Optional[str] = None  # user's invite code
        self._capabilities: set[str] = set()  # announced node capabilities
        self._peer_cache: dict[str, list[tuple[str, int, float]]] = {}
        self._running = False
        self._alert_task: Optional[asyncio.Task] = None
        # Pending lookups: infohash_hex -> list of futures
        self._pending_lookups: dict[str, list[asyncio.Future]] = {}

    @property
    def is_available(self) -> bool:
        return HAS_LIBTORRENT

    @property
    def announced_count(self) -> int:
        return len(self._announced)

    def set_announce_port(self, port: int):
        """Update the port used in DHT announcements (e.g., from UPnP)."""
        if port != self._announce_port:
            logger.info(
                f"DHT announce port changed: {self._announce_port} -> {port}"
            )
            self._announce_port = port

    async def start(self):
        """Create libtorrent session and bootstrap DHT."""
        if not HAS_LIBTORRENT:
            logger.warning("libtorrent not available, DHT service disabled")
            return

        settings = {
            "listen_interfaces": f"0.0.0.0:{self.listen_port}",
            "enable_dht": True,
            "enable_lsd": False,
            "enable_upnp": False,   # Phase P3
            "enable_natpmp": False,  # Phase P3
            # Cap aggregate DHT UDP egress — a get_peers traversal fans out
            # exponentially and bursts above the paced initiation rate.
            "dht_upload_rate_limit": 16000,
            "alert_mask": int(
                lt.alert.category_t.dht_notification
                | lt.alert.category_t.dht_operation_notification
            ),
        }
        self._session = lt.session(settings)

        for host, port in DHT_ROUTERS:
            self._session.add_dht_router(host, port)

        self._running = True
        self._alert_task = asyncio.create_task(self._poll_alerts())

        # Wait for DHT to bootstrap
        logger.info(
            f"DHT bootstrapping on UDP port {self.listen_port}..."
        )
        await self._wait_for_dht_ready()
        logger.info("DHT bootstrap complete")

    async def _wait_for_dht_ready(self, min_nodes: int = 20):
        """Wait until DHT has enough nodes for reliable lookups."""
        deadline = time.time() + DHT_BOOTSTRAP_TIMEOUT
        while time.time() < deadline and self._running:
            if self._session:
                nodes = self._session.status().dht_nodes
                if nodes >= min_nodes:
                    logger.info(f"DHT has {nodes} nodes")
                    return
            await asyncio.sleep(1)
        if self._session:
            nodes = self._session.status().dht_nodes
            logger.warning(
                f"DHT bootstrap: {nodes} nodes "
                f"(wanted {min_nodes}, timeout {DHT_BOOTSTRAP_TIMEOUT}s)"
            )

    async def stop(self):
        """Shut down libtorrent session."""
        self._running = False
        if self._alert_task:
            self._alert_task.cancel()
            try:
                await self._alert_task
            except asyncio.CancelledError:
                pass
        if self._session:
            self._session.pause()
            del self._session
            self._session = None
        self._announced.clear()
        self._peer_cache.clear()
        logger.info("DHT service stopped")

    async def announce_user(self, invite_code: str):
        """Announce user's invite code in DHT for friend discovery."""
        if not self._session:
            return
        self._user_invite_code = invite_code
        ih = user_infohash(invite_code)
        sha1 = lt.sha1_hash(ih)
        self._session.dht_announce(sha1, self._announce_port, 0)
        logger.info(f"DHT: user announced ({invite_code})")

    async def lookup_user(self, invite_code: str) -> list[tuple[str, int]]:
        """Find a user by their invite code. Returns list of (ip, port)."""
        return await self._lookup(user_infohash(invite_code),
                                  f"user:{invite_code}")

    async def announce_node(self):
        """Announce this node on the discovery key — the highway a peer
        finds us by (one lookup, then inventory). Unconditional and cheap:
        one announce regardless of library size."""
        if not self._session:
            return
        sha1 = lt.sha1_hash(node_infohash())
        self._session.dht_announce(sha1, self._announce_port, 0)
        logger.info(f"DHT: node announced (port {self._announce_port})")

    async def lookup_nodes(self) -> list[tuple[str, int]]:
        """Find Sautium nodes on the discovery key. Returns (ip, port)."""
        return await self._lookup(node_infohash(), "node")

    async def announce_capability(self, capability: str):
        """Announce a node capability (e.g. 'mbdump') on its well-known infohash."""
        if not self._session:
            return
        self._capabilities.add(capability)
        sha1 = lt.sha1_hash(capability_infohash(capability))
        self._session.dht_announce(sha1, self._announce_port, 0)
        logger.info(f"DHT: capability announced ({capability})")

    async def lookup_capability(self, capability: str) -> list[tuple[str, int]]:
        """Find nodes announcing a capability. Returns list of (ip, port)."""
        return await self._lookup(capability_infohash(capability),
                                  f"cap:{capability}")

    async def announce_artists(self, artist_uuids: list[str]):
        """Announce the rare-artist tail (see module docstring). The caller
        decides WHICH artists — this is a bounded set, not the library."""
        if not self._session:
            return

        new_uuids = set(artist_uuids) - self._announced
        if not new_uuids:
            logger.info("No new tail artists to announce")
            return

        logger.info(
            f"Announcing {len(new_uuids)} rare artists in DHT "
            f"(announce port {self._announce_port})"
        )
        for i, uuid in enumerate(new_uuids):
            ih = artist_infohash(uuid)
            sha1 = lt.sha1_hash(ih)
            self._session.dht_announce(sha1, self._announce_port, 0)
            self._announced.add(uuid)
            if (i + 1) % ANNOUNCE_CHUNK == 0:
                await asyncio.sleep(ANNOUNCE_CHUNK_PAUSE)
                if not self._running or not self._session:
                    return

        logger.info(
            f"DHT: {len(self._announced)} rare artists announced total"
        )

    async def _lookup(self, infohash: bytes, cache_key: str
                      ) -> list[tuple[str, int]]:
        """get_peers on an infohash, with the shared peer cache."""
        cached = self._get_cached_peers(cache_key)
        if cached is not None:
            return cached

        if not self._session:
            return []

        sha1 = lt.sha1_hash(infohash)
        ih_hex = sha1.to_string().hex()

        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._pending_lookups.setdefault(ih_hex, []).append(future)

        self._session.dht_get_peers(sha1)

        try:
            peers = await asyncio.wait_for(future, timeout=GET_PEERS_TIMEOUT)
        except asyncio.TimeoutError:
            if ih_hex in self._pending_lookups:
                futures = self._pending_lookups[ih_hex]
                if future in futures:
                    futures.remove(future)
                if not futures:
                    del self._pending_lookups[ih_hex]
            logger.debug(f"DHT lookup timeout for {cache_key}")
            return []

        now = time.time()
        self._peer_cache[cache_key] = [(ip, port, now) for ip, port in peers]
        return peers

    async def lookup_artist(self, artist_uuid: str) -> list[tuple[str, int]]:
        """Find peers announcing this specific artist — the targeted path for
        residual rare artists a node-discovery sweep failed to cover."""
        return await self._lookup(artist_infohash(artist_uuid), artist_uuid)

    async def lookup_artists_batch(
        self, artist_uuids: list[str], max_concurrent: int = 20
    ) -> dict[str, list[tuple[str, int]]]:
        """
        Batch lookup for multiple artists.

        Returns: {artist_uuid: [(ip, port), ...]}
        """
        results = {}
        sem = asyncio.Semaphore(max_concurrent)

        async def _lookup(uuid):
            async with sem:
                peers = await self.lookup_artist(uuid)
                if peers:
                    results[uuid] = peers

        tasks = [_lookup(uuid) for uuid in artist_uuids]
        await asyncio.gather(*tasks, return_exceptions=True)
        return results

    def _get_cached_peers(
        self, artist_uuid: str
    ) -> Optional[list[tuple[str, int]]]:
        """Get cached peers if not expired."""
        entries = self._peer_cache.get(artist_uuid)
        if entries is None:
            return None
        now = time.time()
        valid = [(ip, port) for ip, port, ts in entries
                 if now - ts < PEER_CACHE_TTL]
        if not valid:
            del self._peer_cache[artist_uuid]
            return None
        return valid

    async def periodic_reannounce(self):
        """Refresh node key, user key, capabilities and the rare-artist tail
        every REANNOUNCE_INTERVAL seconds (DHT entries live 15-30 min)."""
        while self._running:
            await asyncio.sleep(REANNOUNCE_INTERVAL)
            if not self._running or not self._session:
                break

            # The discovery key first — it is what every peer looks up.
            self._session.dht_announce(
                lt.sha1_hash(node_infohash()), self._announce_port, 0)

            # Re-announce user
            if self._user_invite_code:
                ih = user_infohash(self._user_invite_code)
                sha1 = lt.sha1_hash(ih)
                self._session.dht_announce(sha1, self._announce_port, 0)

            # Re-announce capabilities
            for cap in list(self._capabilities):
                sha1 = lt.sha1_hash(capability_infohash(cap))
                self._session.dht_announce(sha1, self._announce_port, 0)

            # Re-announce artists
            logger.info(
                f"Re-announcing {len(self._announced)} artists "
                f"+ user in DHT"
            )
            for i, uuid in enumerate(list(self._announced)):
                ih = artist_infohash(uuid)
                sha1 = lt.sha1_hash(ih)
                self._session.dht_announce(sha1, self._announce_port, 0)
                if (i + 1) % ANNOUNCE_CHUNK == 0:
                    await asyncio.sleep(ANNOUNCE_CHUNK_PAUSE)
                    if not self._running or not self._session:
                        return
            logger.info("DHT re-announce complete")

    async def _poll_alerts(self):
        """Poll libtorrent alerts and dispatch to handlers."""
        while self._running:
            try:
                if self._session:
                    alerts = self._session.pop_alerts()
                    for alert in alerts:
                        self._handle_alert(alert)
            except Exception as e:
                logger.debug(f"Alert polling error: {e}")
            await asyncio.sleep(0.5)

    def _handle_alert(self, alert):
        """Handle a libtorrent alert."""
        if isinstance(alert, lt.dht_get_peers_reply_alert):
            ih_hex = alert.info_hash.to_string().hex()
            raw_peers = alert.peers()
            # libtorrent >= 2.1 returns list[tuple[str,int]],
            # older versions return list[tcp_endpoint]
            peers = []
            for p in raw_peers:
                if isinstance(p, tuple):
                    peers.append((str(p[0]), int(p[1])))
                else:
                    peers.append((p.address().to_string(), p.port()))
            logger.debug(
                f"DHT get_peers reply: {ih_hex[:16]}... "
                f"→ {len(peers)} peers"
            )
            # Resolve pending futures
            futures = self._pending_lookups.pop(ih_hex, [])
            for fut in futures:
                if not fut.done():
                    fut.set_result(peers)

    def get_dht_stats(self) -> dict:
        """Return DHT statistics for UI display."""
        if not self._session:
            return {
                "available": HAS_LIBTORRENT,
                "running": False,
                "nodes": 0,
                "announced_artists": 0,
                "user_announced": False,
                "cached_peers": 0,
            }
        status = self._session.status()
        return {
            "available": True,
            "running": self._running,
            "nodes": status.dht_nodes,
            "announced_artists": len(self._announced),
            "user_announced": self._user_invite_code is not None,
            "cached_peers": sum(
                len(v) for v in self._peer_cache.values()
            ),
        }
