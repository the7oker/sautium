"""
libtorrent DHT service for Sautium.

Announces enriched artists in the BitTorrent DHT so other nodes
can find this node and sync enrichment data via HTTP.

Each enriched artist is announced under a unique infohash:
    SHA1("Sautium-artist:" + artist_uuid)

Other nodes search for the same infohash to discover peers
that have enrichment data for a specific artist.

Shared by both Docker backend (FastAPI) and desktop launcher (aiohttp).
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

# DHT re-announce interval (seconds)
REANNOUNCE_INTERVAL = 15 * 60  # 15 minutes

# How long to wait for DHT bootstrap (seconds)
DHT_BOOTSTRAP_TIMEOUT = 30

# How long to wait for get_peers results (seconds)
GET_PEERS_TIMEOUT = 15

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
        self._session: Optional[object] = None  # lt.session
        self._announced: set[str] = set()  # artist UUIDs currently announced
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
            "alert_mask": lt.alert.category_t.dht_notification,
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

    async def _wait_for_dht_ready(self):
        """Wait until DHT has enough nodes."""
        deadline = time.time() + DHT_BOOTSTRAP_TIMEOUT
        while time.time() < deadline and self._running:
            if self._session and self._session.status().dht_nodes > 0:
                nodes = self._session.status().dht_nodes
                logger.info(f"DHT has {nodes} nodes")
                return
            await asyncio.sleep(1)
        if self._session:
            nodes = self._session.status().dht_nodes
            logger.warning(
                f"DHT bootstrap timeout ({DHT_BOOTSTRAP_TIMEOUT}s), "
                f"nodes: {nodes}"
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

    async def announce_artists(self, artist_uuids: list[str]):
        """Announce all enriched artists in DHT."""
        if not self._session:
            return

        new_uuids = set(artist_uuids) - self._announced
        if not new_uuids:
            logger.info("No new artists to announce")
            return

        logger.info(
            f"Announcing {len(new_uuids)} artists in DHT "
            f"(HTTP port {self.http_port})"
        )
        for uuid in new_uuids:
            ih = artist_infohash(uuid)
            sha1 = lt.sha1_hash(ih)
            self._session.dht_announce(sha1, self.http_port, 0)
            self._announced.add(uuid)

        logger.info(
            f"DHT: {len(self._announced)} artists announced total"
        )

    async def lookup_artist(self, artist_uuid: str) -> list[tuple[str, int]]:
        """
        Find peers that have enrichment for this artist.

        Returns list of (ip, port) tuples.
        """
        # Check cache first
        cached = self._get_cached_peers(artist_uuid)
        if cached is not None:
            return cached

        if not self._session:
            return []

        ih = artist_infohash(artist_uuid)
        sha1 = lt.sha1_hash(ih)
        ih_hex = sha1.to_string().hex()

        # Create a future for this lookup
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._pending_lookups.setdefault(ih_hex, []).append(future)

        # Initiate DHT get_peers
        self._session.dht_get_peers(sha1)

        # Wait for result with timeout
        try:
            peers = await asyncio.wait_for(future, timeout=GET_PEERS_TIMEOUT)
        except asyncio.TimeoutError:
            # Remove pending future
            if ih_hex in self._pending_lookups:
                futures = self._pending_lookups[ih_hex]
                if future in futures:
                    futures.remove(future)
                if not futures:
                    del self._pending_lookups[ih_hex]
            logger.debug(f"DHT lookup timeout for artist {artist_uuid[:8]}...")
            return []

        # Cache results
        now = time.time()
        self._peer_cache[artist_uuid] = [
            (ip, port, now) for ip, port in peers
        ]
        return peers

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
        """Re-announce all artists every REANNOUNCE_INTERVAL seconds."""
        while self._running:
            await asyncio.sleep(REANNOUNCE_INTERVAL)
            if not self._running or not self._session:
                break
            logger.info(
                f"Re-announcing {len(self._announced)} artists in DHT"
            )
            for uuid in list(self._announced):
                ih = artist_infohash(uuid)
                sha1 = lt.sha1_hash(ih)
                self._session.dht_announce(sha1, self.http_port, 0)
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
            peers = [(p.address().to_string(), p.port())
                     for p in alert.peers()]
            logger.debug(
                f"DHT get_peers reply: {ih_hex[:16]}... "
                f"-> {len(peers)} peers"
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
                "cached_peers": 0,
            }
        status = self._session.status()
        return {
            "available": True,
            "running": self._running,
            "nodes": status.dht_nodes,
            "announced_artists": len(self._announced),
            "cached_peers": sum(
                len(v) for v in self._peer_cache.values()
            ),
        }
