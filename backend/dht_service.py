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


# Prefix for infohash computation
INFOHASH_PREFIX = "Sautium-artist:"
INFOHASH_PREFIX_USER = "Sautium-user:"

# dht_announce initiations per pacing pause. Announcing thousands of
# infohashes in a tight loop floods the docker-bridge/WSL2 NAT with UDP
# (each announce fans out into a get_peers traversal) and starves every
# other network flow in the container — measured 2026-07-10: each 15-min
# re-announce burst lined up with an HQPlayer control-socket timeout
# cluster, while a probe from the WSL host stayed clean. Pacing spreads
# ~3.2k announces over ~2 min; DHT entries live 15-30 min, so staggered
# refresh costs nothing in discoverability.
# 5/s: initiation must stay under the DHT's traversal-completion rate or the
# backlog of concurrent traversals accumulates through the window and
# saturates the path anyway (measured: 25/s still produced timeout bursts
# near the END of the paced window and ~1 min past it). 3173 announces at
# 5/s = ~10.6 min, still inside the 15-min re-announce cycle.
ANNOUNCE_CHUNK = 5
ANNOUNCE_CHUNK_PAUSE = 1.0

# DHT re-announce interval (seconds)
REANNOUNCE_INTERVAL = 15 * 60  # 15 minutes

# Yield-to-foreground announce scheduling (HARDWARE-TIERS / announce-storm
# history): even the paced sweep degrades the container->host path for its
# whole ~10-min window — HQPlayer connects time out and the Web UI starves.
# Discoverability is a *background* value; local playback/UI always wins.
# The announcer therefore HOLDS between chunks while the node is busy
# (playback active on any output, or an authenticated UI request within
# ACTIVITY_WINDOW). To keep an always-listening node from vanishing off the
# DHT (entries live 15-30 min), a hold longer than MAX_DEFER grants a
# FORCED_TRICKLE allowance of announces even under activity.
BUSY_CHECK_INTERVAL = 15.0
ACTIVITY_WINDOW = 5 * 60
MAX_DEFER_SECONDS = 30 * 60
FORCED_TRICKLE = 50

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


def user_infohash(invite_code: str) -> bytes:
    """Compute SHA1 infohash for a user invite code."""
    return hashlib.sha1(
        f"{INFOHASH_PREFIX_USER}{invite_code}".encode()
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
        self._announced: set[str] = set()  # artist UUIDs announced at least once
        self._artist_uuids: set[str] = set()  # full announce target set
        self._rotation_cursor = 0  # round-robin position for budgeted re-announces
        self._user_invite_code: Optional[str] = None
        self._peer_cache: dict[str, list[tuple[str, int, float]]] = {}
        self._running = False
        self._alert_task: Optional[asyncio.Task] = None
        # Pending lookups: infohash_hex -> list of futures
        self._pending_lookups: dict[str, list[asyncio.Future]] = {}
        # Yield-to-foreground state (see module constants). The probe is
        # injected by main.py after startup — returns True while the node
        # has foreground activity; None = never busy (announce freely).
        self._activity_probe: Optional[callable] = None
        self._deferred_s = 0.0
        self._trickle_allowance = 0
        # Serialize sweeps: the startup announce and the 15-min cycle must
        # not interleave (double traffic on the same choked path).
        self._announce_lock = asyncio.Lock()

    def set_activity_probe(self, probe: callable) -> None:
        self._activity_probe = probe

    def _is_busy(self) -> bool:
        if self._activity_probe is None:
            return False
        try:
            return bool(self._activity_probe())
        except Exception:
            return False

    async def _hold_while_busy(self) -> None:
        """Block between announce chunks while foreground activity is on.
        Coarse checks of an in-process flag (BUSY_CHECK_INTERVAL) — this is
        backpressure scheduling, not state polling of another component.
        After MAX_DEFER_SECONDS of continuous busy, grants FORCED_TRICKLE
        announces so DHT presence survives an all-day listening session."""
        if not self._is_busy():
            self._deferred_s = 0.0
            return
        if self._trickle_allowance > 0:
            self._trickle_allowance -= 1
            return
        held = False
        while self._running and self._is_busy():
            held = True
            await asyncio.sleep(BUSY_CHECK_INTERVAL)
            self._deferred_s += BUSY_CHECK_INTERVAL
            if self._deferred_s >= MAX_DEFER_SECONDS:
                self._deferred_s = 0.0
                self._trickle_allowance = FORCED_TRICKLE - 1
                logger.info(
                    "DHT: announce deferred %d min under activity — pushing "
                    "a %d-announce trickle", MAX_DEFER_SECONDS // 60,
                    FORCED_TRICKLE)
                return
        if held:
            logger.info("DHT: node idle — announcing resumes")

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

    async def announce_user(self, invite_code: str):
        """Announce user's invite code in DHT for friend discovery."""
        if not self._session:
            return
        self._user_invite_code = invite_code
        ih = user_infohash(invite_code)
        sha1 = lt.sha1_hash(ih)
        self._session.dht_announce(sha1, self.http_port, 0)
        logger.info(f"DHT: user announced ({invite_code})")

    @staticmethod
    def _announce_budget() -> Optional[int]:
        """Per-cycle announce budget from user settings (sync.announce_limit;
        null = announce everything). Even the PACED full sweep degrades the
        container↔host path for its whole ~10-min window — a budget keeps the
        window short and rotation still refreshes every artist over a few
        cycles."""
        from routers.settings import _read
        limit = _read("sync.announce_limit")
        return int(limit) if limit else None

    async def announce_artists(self, artist_uuids: list[str]):
        """Register artists for DHT announcing and announce up to the budget
        now; the rest are covered by the rotating periodic re-announce."""
        if not self._session:
            return

        self._artist_uuids.update(artist_uuids)
        new_uuids = list(set(artist_uuids) - self._announced)
        if not new_uuids:
            logger.info("No new artists to announce")
            return

        try:
            budget = await asyncio.to_thread(self._announce_budget)
        except Exception as e:
            logger.warning(f"announce budget read failed: {e}")
            budget = None
        if budget and len(new_uuids) > budget:
            logger.info(
                f"Announcing {budget}/{len(new_uuids)} new artists in DHT "
                f"(budget; rotation covers the rest, HTTP port {self.http_port})"
            )
            new_uuids = new_uuids[:budget]
        else:
            logger.info(
                f"Announcing {len(new_uuids)} artists in DHT "
                f"(HTTP port {self.http_port})"
            )
        async with self._announce_lock:
            for i, uuid in enumerate(new_uuids):
                await self._hold_while_busy()
                if not self._running or not self._session:
                    return
                ih = artist_infohash(uuid)
                sha1 = lt.sha1_hash(ih)
                self._session.dht_announce(sha1, self.http_port, 0)
                self._announced.add(uuid)
                if (i + 1) % ANNOUNCE_CHUNK == 0:
                    await asyncio.sleep(ANNOUNCE_CHUNK_PAUSE)
                    if not self._running or not self._session:
                        return

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
        """Re-announce artists and user every REANNOUNCE_INTERVAL seconds.
        With an announce budget (sync.announce_limit) each cycle refreshes a
        rotating window of the full set — never-announced artists included —
        so every artist comes around every ceil(N/budget) cycles while the
        per-cycle network window stays short."""
        while self._running:
            await asyncio.sleep(REANNOUNCE_INTERVAL)
            if not self._running or not self._session:
                break

            # Re-announce user
            if self._user_invite_code:
                ih = user_infohash(self._user_invite_code)
                sha1 = lt.sha1_hash(ih)
                self._session.dht_announce(sha1, self.http_port, 0)

            try:
                budget = await asyncio.to_thread(self._announce_budget)
            except Exception as e:
                logger.warning(f"announce budget read failed: {e}")
                budget = None
            uuids = sorted(self._artist_uuids or self._announced)
            if budget and len(uuids) > budget:
                start = self._rotation_cursor % len(uuids)
                uuids = [uuids[(start + k) % len(uuids)] for k in range(budget)]
                self._rotation_cursor = (start + budget) % len(self._artist_uuids)
                logger.info(
                    f"Re-announcing {len(uuids)}/{len(self._artist_uuids)} artists "
                    f"(rotating budget) + user in DHT"
                )
            else:
                logger.info(
                    f"Re-announcing {len(uuids)} artists + user in DHT"
                )
            async with self._announce_lock:
                for i, uuid in enumerate(uuids):
                    await self._hold_while_busy()
                    if not self._running or not self._session:
                        return
                    ih = artist_infohash(uuid)
                    sha1 = lt.sha1_hash(ih)
                    self._session.dht_announce(sha1, self.http_port, 0)
                    self._announced.add(uuid)
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
            peers = []
            for p in raw_peers:
                if isinstance(p, tuple):
                    peers.append((str(p[0]), int(p[1])))
                else:
                    peers.append((p.address().to_string(), p.port()))
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
