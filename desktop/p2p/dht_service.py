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

# DHT re-announce interval (seconds). Also the length of one tail pass:
# entries live 15-30 min, so refreshing every key once per interval is
# exactly enough and nothing is gained by going faster.
REANNOUNCE_INTERVAL = 15 * 60  # 15 minutes

# Announce-storm history (e0d409b): each announce fans out into a get_peers
# traversal, so a tight loop floods the host NAT with UDP and starves
# concurrent flows — on the Docker master it measurably knocked out the
# HQPlayer control socket every 15 minutes. That was answered with a brake
# (hold while the node is busy), which had the announcer alternate between
# storming and standing still, and standing still is what dropped the node
# out of the DHT. The rate is now safe BY CONSTRUCTION: the tail drips one
# key at a time, spaced so a full pass takes exactly one entry lifetime, and
# only ONE traversal is ever in flight.
#
# A tail bigger than REANNOUNCE_INTERVAL/MIN_ANNOUNCE_SPACING keys cannot
# refresh inside one lifetime, and that is fine: its oldest keys expire and
# reappear on the next pass, so presence degrades smoothly with tail size
# instead of collapsing. At the shipped sync.announce_limit of 300 the pass
# is 3 s per key, well inside the window.
MIN_ANNOUNCE_SPACING = 0.5
TAIL_IDLE_RECHECK = 60.0

# How long to wait for DHT bootstrap (seconds)
DHT_BOOTSTRAP_TIMEOUT = 30

# How long to wait for get_peers results, and the collection window a
# want_all lookup spends gathering replies. 15 s because a traversal runs
# ~19 s but every reply arrives in its first seconds (measured 2026-08-24:
# three holders at 0.4, 0.4 and 1.4 s), so the tail of the traversal only
# costs latency — on an empty key it is pure dead time, which is what the
# sync walk's rare-artist step used to burn. Matches backend/dht_service.py.
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
        # Reachability gate: an unreachable node's announces carry a dead
        # address — they pollute lookups for everyone and burn traffic.
        # Lookups and LAN discovery are unaffected by this flag.
        self._announces_enabled = True
        self._pace = lambda: 1.0     # load_meter.announce_pace when installed
        # External IP as DHT peers see us (external_ip_alert) — the
        # router-WAN vs world-view mismatch is a CGNAT tell.
        self.observed_external_ip: Optional[str] = None
        self._session: Optional[object] = None  # lt.session
        self._announced: set[str] = set()  # artist UUIDs currently announced
        self._user_invite_code: Optional[str] = None  # user's invite code
        self._capabilities: set[str] = set()  # announced node capabilities
        # Invite codes announced ON BEHALF of relay clients (Phase D). BT DHT
        # does not verify infohash ownership — that is the feature: a CGNAT
        # node cannot announce itself, so its relay does, and a sender's
        # ordinary lookup_user() finds the relay's address. Withdrawn the
        # moment the client's wake subscription closes; the voucher (held by
        # the sync server) is what makes the claim provable to senders.
        self._client_invites: set[str] = set()
        self._peer_cache: dict[str, list[tuple[str, int, float]]] = {}
        self._running = False
        self._alert_task: Optional[asyncio.Task] = None
        # Pending lookups: infohash_hex -> list of futures
        # ih_hex -> [(future, want_all)]. want_all waits out the collection
        # window instead of returning on the first fragment; see _lookup.
        self._pending_lookups: dict[str, list[tuple[asyncio.Future, bool]]] = {}
        # ih_hex -> peers seen so far in the traversal currently in flight.
        self._lookup_buffers: dict[str, set[tuple[str, int]]] = {}

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

    def set_pace_provider(self, fn) -> None:
        """Multiplier for the announce chunk pause (desktop/p2p/load_meter.py:
        1× when the node is idle, up to 8× under load or playback) — the
        playback-aware throttling of the announce sweep."""
        self._pace = fn

    def set_announces_enabled(self, enabled: bool):
        """Gate ALL announces by the reachability verdict (see __init__)."""
        if enabled != self._announces_enabled:
            logger.info(
                "DHT announces %s (reachability verdict)",
                "enabled" if enabled else "suppressed",
            )
            self._announces_enabled = enabled

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
        if not self._announces_enabled:
            return
        ih = user_infohash(invite_code)
        sha1 = lt.sha1_hash(ih)
        self._session.dht_announce(sha1, self._announce_port, 0)
        logger.info(f"DHT: user announced ({invite_code})")

    async def lookup_user(self, invite_code: str) -> list[tuple[str, int]]:
        """Find a user by their invite code. Returns list of (ip, port)."""
        return await self._lookup(user_infohash(invite_code),
                                  f"user:{invite_code}")

    async def announce_user_for(self, invite_code: str):
        """Announce a relay CLIENT's invite code from this node (Phase D).
        Idempotent; joins the periodic re-announce cycle until withdrawn."""
        if invite_code in self._client_invites:
            return
        self._client_invites.add(invite_code)
        if not self._session or not self._announces_enabled:
            return
        sha1 = lt.sha1_hash(user_infohash(invite_code))
        self._session.dht_announce(sha1, self._announce_port, 0)
        logger.info(f"DHT: client announced on behalf ({invite_code})")

    def withdraw_user_for(self, invite_code: str):
        """Stop re-announcing a client. The already-published DHT entry ages
        out on its own (15-30 min) — that decay window is the same one every
        dead announce has, and senders survive it by trying the next
        candidate."""
        self._client_invites.discard(invite_code)

    async def announce_node(self):
        """Announce this node on the discovery key — the highway a peer
        finds us by (one lookup, then inventory). Cheap: one announce
        regardless of library size."""
        if not self._session or not self._announces_enabled:
            return
        sha1 = lt.sha1_hash(node_infohash())
        self._session.dht_announce(sha1, self._announce_port, 0)
        logger.info(f"DHT: node announced (port {self._announce_port})")

    async def lookup_nodes(self) -> list[tuple[str, int]]:
        """Find Sautium nodes on the discovery key. Returns (ip, port).

        want_all: this is the sync walk's only organic source of peers, and
        one holder's fragment of the busiest key in the network is not a
        sample worth acting on."""
        return await self._lookup(node_infohash(), "node", want_all=True)

    async def announce_capability(self, capability: str):
        """Announce a node capability (e.g. 'mbdump') on its well-known infohash."""
        if not self._session:
            return
        self._capabilities.add(capability)
        if not self._announces_enabled:
            return
        sha1 = lt.sha1_hash(capability_infohash(capability))
        self._session.dht_announce(sha1, self._announce_port, 0)
        logger.info(f"DHT: capability announced ({capability})")

    def withdraw_capability(self, capability: str):
        """Drop a capability from the re-announce cycle (e.g. a full relay
        stops advertising). The published entry ages out on its own."""
        self._capabilities.discard(capability)

    async def lookup_capability(self, capability: str) -> list[tuple[str, int]]:
        """Find nodes announcing a capability. Returns list of (ip, port)."""
        return await self._lookup(capability_infohash(capability),
                                  f"cap:{capability}")

    async def announce_artists(self, artist_uuids: list[str]):
        """Register the rare-artist tail (see module docstring). The caller
        decides WHICH artists — this is a bounded set, not the library.

        Registration only: the drip loop does the announcing. A caller
        handing us 300 keys must not become 300 traversals right now, which
        is exactly what the old startup sweep did on every boot."""
        new_uuids = set(artist_uuids) - self._announced
        if not new_uuids:
            return

        self._announced.update(new_uuids)
        logger.info(
            f"DHT tail: +{len(new_uuids)} rare artists "
            f"({len(self._announced)} total)"
        )

    async def _lookup(self, infohash: bytes, cache_key: str,
                      want_all: bool = False) -> list[tuple[str, int]]:
        """get_peers on an infohash, with the shared peer cache.

        One traversal answers with one alert PER responding node, so the
        first reply carries one holder's view, not the set (measured
        2026-08-24 on Sautium-cap:mbdump: three replies, three different
        holders, 0.4-1.4 s apart). `want_all` therefore waits out the
        collection window and returns the union — node discovery needs it,
        because a partial answer there means a peer that exists is simply
        never contacted, and that path has no second tier to catch it.

        The default stays first-reply: the chat paths run this on the
        critical path of every message and must not wait, and they do have
        further tiers. They still profit — every reply lands in the buffer
        and the cache, so their NEXT call sees the fuller set.
        """
        cached = self._get_cached_peers(cache_key)
        if cached is not None:
            return cached

        if not self._session:
            return []

        sha1 = lt.sha1_hash(infohash)
        ih_hex = sha1.to_string().hex()

        loop = asyncio.get_event_loop()
        future = loop.create_future()
        waiter = (future, want_all)
        self._pending_lookups.setdefault(ih_hex, []).append(waiter)

        self._session.dht_get_peers(sha1)

        try:
            peers = await asyncio.wait_for(future, timeout=GET_PEERS_TIMEOUT)
        except asyncio.TimeoutError:
            # want_all always lands here — nothing resolves it early, the
            # timeout IS its collection window.
            peers = sorted(self._lookup_buffers.get(ih_hex, ()))
        finally:
            waiters = self._pending_lookups.get(ih_hex)
            if waiters and waiter in waiters:
                waiters.remove(waiter)
            if waiters is not None and not waiters:
                del self._pending_lookups[ih_hex]
            if ih_hex not in self._pending_lookups:
                self._lookup_buffers.pop(ih_hex, None)

        if not peers:
            logger.debug(f"DHT lookup found nothing for {cache_key}")
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
        """Keep this node's DHT presence alive — two INDEPENDENT cycles.

        They shared one loop until 2026-08-24, and the tail starved the
        keys: on the master a 300-key pass ran 36-59 minutes while the node
        key — three announces, nothing to pace — waited behind it. A DHT
        entry lives 15-30 min, so the node dropped out of discovery between
        passes and the sync walk could not find it at all.
        """
        await asyncio.gather(self._reannounce_keys(), self._reannounce_tail())

    async def _reannounce_keys(self):
        """Node, user, relay clients and capabilities: a handful of
        announces regardless of library size, so no pacing applies."""
        while self._running:
            await asyncio.sleep(REANNOUNCE_INTERVAL)
            if not self._running or not self._session:
                break
            if not self._announces_enabled:
                continue

            # The discovery key first — it is what every peer looks up.
            self._session.dht_announce(
                lt.sha1_hash(node_infohash()), self._announce_port, 0)

            if self._user_invite_code:
                ih = user_infohash(self._user_invite_code)
                sha1 = lt.sha1_hash(ih)
                self._session.dht_announce(sha1, self._announce_port, 0)

            # Relay clients (announce-on-behalf, Phase D) — bounded by the
            # relay cap, so at most a few dozen keys.
            for code in list(self._client_invites):
                sha1 = lt.sha1_hash(user_infohash(code))
                self._session.dht_announce(sha1, self._announce_port, 0)

            for cap in list(self._capabilities):
                sha1 = lt.sha1_hash(capability_infohash(cap))
                self._session.dht_announce(sha1, self._announce_port, 0)

            logger.info(
                f"DHT keys re-announced: node + user + "
                f"{len(self._client_invites)} clients + "
                f"{len(self._capabilities)} capabilities"
            )

    async def _reannounce_tail(self):
        """The rare-artist tail as a continuous drip — no sweep, no sleep
        between passes: the spacing IS the cadence. One key every
        REANNOUNCE_INTERVAL/len(tail) seconds means a pass lasts exactly one
        entry lifetime, so every key is refreshed just as it would expire,
        and the instantaneous rate never exceeds one announce.

        The tail is re-read each pass, so a set that grew or shrank between
        passes re-spaces itself without any bookkeeping."""
        while self._running:
            uuids = sorted(self._announced)
            if not uuids or not self._session or not self._announces_enabled:
                await asyncio.sleep(TAIL_IDLE_RECHECK)
                continue

            spacing = max(MIN_ANNOUNCE_SPACING, REANNOUNCE_INTERVAL / len(uuids))
            logger.info(
                f"DHT tail pass: {len(uuids)} rare artists, "
                f"{spacing:.1f}s apart"
            )
            for uuid in uuids:
                if not self._running or not self._session:
                    return
                if not self._announces_enabled:
                    break
                self._session.dht_announce(
                    lt.sha1_hash(artist_infohash(uuid)), self._announce_port, 0)
                await asyncio.sleep(spacing * self._pace())

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
        if isinstance(alert, lt.external_ip_alert):
            self.observed_external_ip = str(alert.external_address)
            return
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
            buffered = self._lookup_buffers.setdefault(ih_hex, set())
            buffered.update(peers)

            waiters = self._pending_lookups.get(ih_hex)
            if not waiters:
                return
            snapshot = sorted(buffered)
            still_waiting = [(fut, want_all) for fut, want_all in waiters
                             if want_all]
            for fut, want_all in waiters:
                if not want_all and not fut.done():
                    fut.set_result(snapshot)
            if still_waiting:
                self._pending_lookups[ih_hex] = still_waiting
            else:
                del self._pending_lookups[ih_hex]

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
