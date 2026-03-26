"""
P2P Manager for Sautium.

Orchestrates the aiohttp sync server and libtorrent DHT service.
Runs in a background asyncio event loop thread, separate from the
tkinter GUI main thread.

Provides P2P sync: find peers via DHT, sync enrichment data via HTTP.
"""

import asyncio
import logging
import threading
from functools import partial
from typing import Callable, Optional

import psycopg2

from desktop.api_client import BackendAPIClient
from desktop.p2p import sync_queries
from desktop.p2p.chat_service import ChatService
from desktop.p2p.dht_service import DHTService
from desktop.p2p.lan_discovery import LANDiscovery
from desktop.p2p.sync_server import SyncServer
from desktop.p2p.upnp_service import UPnPService
from desktop.sync_client import SyncClient

logger = logging.getLogger(__name__)


class P2PManager:
    """Orchestrates P2P sync server + DHT + LAN discovery + UPnP."""

    def __init__(self, db_dsn: str, config: dict):
        self.db_dsn = db_dsn
        self.config = config
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._sync_server: Optional[SyncServer] = None
        self._dht_service: Optional[DHTService] = None
        self._lan_discovery: Optional[LANDiscovery] = None
        self._upnp: Optional[UPnPService] = None
        self._chat_service: Optional[ChatService] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._reannounce_task: Optional[asyncio.Task] = None
        self._pending_retry_task: Optional[asyncio.Task] = None
        self._lan_discovery_task: Optional[asyncio.Task] = None
        self._running = False
        self._on_message_cb: Optional[Callable] = None

    @property
    def is_running(self) -> bool:
        return self._running

    def set_on_message_callback(self, cb: Callable):
        """Set callback for incoming chat messages (called from P2P thread)."""
        self._on_message_cb = cb

    @property
    def chat_service(self) -> Optional[ChatService]:
        return self._chat_service

    def start(self, node_id: str = "", progress_cb: Callable = None):
        """Start P2P services in a background thread."""
        if self._running:
            logger.warning("P2P manager already running")
            return

        p2p_cfg = self.config.get("p2p", {})
        self._http_port = p2p_cfg.get("listen_port", 19000)
        http_port = self._http_port
        dht_port = http_port + 1  # DHT on next port

        # Load account info for chat
        from desktop.node_identity import get_account_info
        account_info = get_account_info()

        self._sync_server = SyncServer(
            db_dsn=self.db_dsn,
            port=http_port,
            node_id=node_id,
            account_info=account_info,
        )
        self._dht_service = DHTService(
            listen_port=dht_port,
            http_port=http_port,
        )
        docker_ports = p2p_cfg.get("docker_ports", [])
        self._lan_discovery = LANDiscovery(
            sync_port=http_port,
            node_id=node_id,
            localhost_probe_ports=docker_ports,
        )
        # UPnP: map sync port + any docker ports found on localhost
        upnp_ports = [http_port] + docker_ports
        self._upnp = UPnPService(ports=upnp_ports)

        # Initialize chat service if account exists
        if account_info:
            try:
                from desktop.node_identity import get_private_key_raw
                self._chat_service = ChatService(
                    db_dsn=self.db_dsn,
                    private_key_raw=get_private_key_raw(),
                    public_key_hex=account_info["public_key_hex"],
                )
                self._sync_server.set_chat_service(
                    self._chat_service, self._on_message_cb
                )
            except Exception as e:
                logger.warning(f"Chat service init failed: {e}")

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            args=(node_id, progress_cb),
            daemon=True,
            name="p2p-event-loop",
        )
        self._thread.start()

    def _run_loop(self, node_id: str, progress_cb: Callable = None):
        """Background thread: run asyncio event loop."""
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(
                self._async_main(node_id, progress_cb)
            )
        except Exception as e:
            logger.error(f"P2P event loop error: {e}")
        finally:
            self._running = False

    async def _async_main(self, node_id: str, progress_cb: Callable = None):
        """Main async routine: start services, announce, wait."""
        self._stop_event = asyncio.Event()

        def _progress(msg):
            logger.info(msg)
            if progress_cb:
                progress_cb(msg)

        try:
            # Start HTTP sync server
            _progress("Starting sync server...")
            await self._sync_server.start()

            # Start LAN discovery (always — works behind any NAT)
            _progress("Starting LAN discovery...")
            await self._lan_discovery.start()

            # Try UPnP port mapping (best-effort)
            if self._upnp.is_available:
                _progress("Trying UPnP port mapping...")
                ext_ip = await asyncio.get_event_loop().run_in_executor(
                    None, self._upnp.open_ports
                )
                if ext_ip:
                    # Use UPnP external port for DHT announce
                    ext_port = self._upnp.get_external_port(self._http_port)
                    if ext_port:
                        self._dht_service.set_announce_port(ext_port)
                    _progress(f"UPnP: {ext_ip}")

            # Start DHT
            if self._dht_service.is_available:
                _progress("Starting DHT...")
                await self._dht_service.start()

                # Announce user identity in DHT
                from desktop.node_identity import get_account_info
                account_info = get_account_info()
                if account_info and account_info.get("invite_code"):
                    await self._dht_service.announce_user(
                        account_info["invite_code"]
                    )
                    _progress(
                        f"User announced: {account_info['invite_code']}"
                    )

                # Announce enriched artists
                _progress("Querying enriched artists...")
                artist_uuids = await self._get_enriched_artists()
                if artist_uuids:
                    _progress(
                        f"Announcing {len(artist_uuids)} artists in DHT..."
                    )
                    await self._dht_service.announce_artists(artist_uuids)
                    self._lan_discovery.update_enriched_count(
                        len(artist_uuids)
                    )
                    _progress(
                        f"P2P online: {len(artist_uuids)} artists announced"
                    )
                else:
                    _progress("P2P online: no enriched artists yet")

                # Start periodic re-announce
                self._reannounce_task = asyncio.create_task(
                    self._dht_service.periodic_reannounce()
                )

                # Start pending message retry loop
                if self._chat_service:
                    self._pending_retry_task = asyncio.create_task(
                        self._retry_pending_messages()
                    )
            else:
                _progress(
                    "P2P online (LAN only, DHT disabled — "
                    "install libtorrent for internet discovery)"
                )

            self._running = True

            # Wait until stopped
            await self._stop_event.wait()

        except Exception as e:
            logger.error(f"P2P startup failed: {e}")
            _progress(f"P2P error: {e}")
        finally:
            await self._cleanup()

    async def _cleanup(self):
        """Clean shutdown of all services."""
        for task in (self._reannounce_task, self._pending_retry_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._lan_discovery:
            await self._lan_discovery.stop()
        if self._upnp:
            self._upnp.close_ports()
        if self._dht_service:
            await self._dht_service.stop()
        if self._sync_server:
            await self._sync_server.stop()

    def stop(self):
        """Stop all P2P services."""
        if not self._running and not self._loop:
            return

        logger.info("Stopping P2P manager...")

        # Signal DHT to stop waiting (breaks _wait_for_dht_ready)
        if self._dht_service:
            self._dht_service._running = False

        # Signal the event loop to exit
        if self._stop_event and self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._stop_event.set)

        if self._thread:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                logger.warning("P2P thread did not stop in 5s, "
                               "forcing cleanup...")
                # Force-close the loop to unblock everything
                if self._loop and not self._loop.is_closed():
                    self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join(timeout=5)

        if self._loop and not self._loop.is_closed():
            self._loop.close()

        self._running = False
        self._loop = None
        self._thread = None
        logger.info("P2P manager stopped")

    async def _get_enriched_artists(self) -> list[str]:
        """Query local DB for enriched artist UUIDs."""
        loop = asyncio.get_event_loop()
        conn = psycopg2.connect(self.db_dsn)
        conn.autocommit = True
        try:
            return await loop.run_in_executor(
                None,
                sync_queries.get_enriched_artist_uuids,
                conn,
            )
        finally:
            conn.close()

    async def _get_unenriched_artists(self) -> list[str]:
        """Query local DB for artists needing enrichment."""
        loop = asyncio.get_event_loop()
        conn = psycopg2.connect(self.db_dsn)
        conn.autocommit = True
        try:
            return await loop.run_in_executor(
                None,
                sync_queries.get_unenriched_artist_uuids,
                conn,
            )
        finally:
            conn.close()

    async def _get_tracks_for_artists(
        self, artist_uuids: list[str]
    ) -> list[str]:
        """Get all track UUIDs for a list of artists (single query)."""
        if not artist_uuids:
            return []
        loop = asyncio.get_event_loop()
        conn = psycopg2.connect(self.db_dsn)
        conn.autocommit = True
        try:
            def _query(c):
                with c.cursor() as cur:
                    cur.execute(
                        "SELECT DISTINCT track_id::text FROM track_artists "
                        "WHERE artist_id = ANY(%s::uuid[])",
                        [artist_uuids],
                    )
                    return [r[0] for r in cur.fetchall()]
            return await loop.run_in_executor(None, _query, conn)
        finally:
            conn.close()

    async def _get_track_uuids_for_artist(
        self, artist_uuid: str
    ) -> list[str]:
        """Get track UUIDs for a specific artist."""
        loop = asyncio.get_event_loop()
        conn = psycopg2.connect(self.db_dsn)
        conn.autocommit = True
        try:
            return await loop.run_in_executor(
                None,
                sync_queries.get_track_uuids_for_artist,
                conn,
                artist_uuid,
            )
        finally:
            conn.close()

    # -------------------------------------------------------------------
    # P2P Sync (called from launcher thread)
    # -------------------------------------------------------------------

    def sync_from_peers(
        self,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """
        Sync enrichment data from P2P network.

        Called from the launcher thread (blocking).
        Tries manual peers first, then DHT discovery.
        """
        if not self._running:
            if progress_cb:
                progress_cb("P2P not running")
            return {"error": "p2p_not_running"}

        # Run the async sync in the P2P event loop
        future = asyncio.run_coroutine_threadsafe(
            self._async_sync_from_peers(progress_cb),
            self._loop,
        )
        try:
            return future.result(timeout=600)  # 10 min max
        except Exception as e:
            logger.error(f"P2P sync failed: {e}")
            if progress_cb:
                progress_cb(f"P2P sync error: {e}")
            return {"error": str(e)}

    async def _async_sync_from_peers(
        self,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """Async implementation of P2P sync."""

        def _progress(msg):
            logger.info(msg)
            if progress_cb:
                progress_cb(msg)

        # Step 1: Find artists needing enrichment
        _progress("Finding artists without enrichment...")
        unenriched = await self._get_unenriched_artists()
        if not unenriched:
            _progress("All artists already enriched!")
            return {"status": "all_enriched"}

        _progress(f"Found {len(unenriched)} artists needing enrichment")

        # Step 2: Collect track UUIDs for all unenriched artists (single query)
        track_uuids = await self._get_tracks_for_artists(unenriched)

        if not track_uuids:
            _progress("No tracks found for unenriched artists")
            return {"status": "no_tracks"}

        _progress(f"Need enrichment for {len(track_uuids)} tracks")

        # Step 3: Try manual peers first (e.g., Docker backend)
        total_stats = {}
        manual_peers = self.config.get("p2p", {}).get("manual_peers", [])
        for peer_addr in manual_peers:
            synced = await self._sync_from_peer(
                peer_addr, track_uuids, _progress, progress_cb
            )
            for k, v in synced.items():
                if isinstance(v, int):
                    total_stats[k] = total_stats.get(k, 0) + v

        # Step 4: LAN peers (fast, works behind any NAT)
        if self._lan_discovery:
            lan_peers = self._lan_discovery.peers
            if lan_peers:
                _progress(f"Found {len(lan_peers)} LAN peers")
                # Sort by artist count descending (prefer richer peers)
                lan_peers_sorted = sorted(
                    lan_peers,
                    key=lambda p: (
                        self._lan_discovery.get_peer_info(*p) or {}
                    ).get("artists", 0),
                    reverse=True,
                )
                for ip, port in lan_peers_sorted:
                    info = self._lan_discovery.get_peer_info(ip, port)
                    artist_count = (info or {}).get("artists", "?")
                    scheme = (info or {}).get("scheme", "https")
                    peer_url = f"{scheme}://{ip}:{port}"
                    _progress(
                        f"LAN peer {peer_url} "
                        f"({artist_count} artists)..."
                    )
                    synced = await self._sync_from_peer(
                        peer_url,
                        track_uuids,
                        _progress,
                        progress_cb,
                    )
                    for k, v in synced.items():
                        if isinstance(v, int):
                            total_stats[k] = total_stats.get(k, 0) + v

        # Step 5: DHT lookup for remaining unenriched artists (internet)
        #
        # Flow: search DHT by artist → find seed → sync artist tracks →
        # ask same seed about ALL remaining unenriched tracks (high
        # probability it has more) → continue searching if gaps remain.
        synced_peers: set[str] = set()  # peers already fully queried

        if self._dht_service and self._dht_service.is_available:
            while True:
                still_unenriched = await self._get_unenriched_artists()
                if not still_unenriched:
                    break

                _progress(
                    f"Searching DHT for {len(still_unenriched)} "
                    f"remaining artists..."
                )
                peer_map = await self._dht_service.lookup_artists_batch(
                    still_unenriched
                )
                if not peer_map:
                    _progress("No peers found in DHT")
                    break

                # Collect unique NEW peers (skip already-queried)
                new_peers: dict[tuple, list[str]] = {}
                for artist_uuid, peers in peer_map.items():
                    for ip, port in peers:
                        key = f"{ip}:{port}"
                        if key not in synced_peers:
                            new_peers.setdefault(
                                (ip, port), []
                            ).append(artist_uuid)

                if not new_peers:
                    _progress("No new DHT peers to try")
                    break

                _progress(
                    f"Found {len(new_peers)} new DHT peers "
                    f"for {len(peer_map)} artists"
                )

                found_any = False
                for (ip, port), artist_uuids in new_peers.items():
                    peer_addr = f"{ip}:{port}"

                    # Step A: sync tracks for the artists DHT told us about
                    peer_tracks = await self._get_tracks_for_artists(
                        artist_uuids
                    )
                    if not peer_tracks:
                        continue

                    synced = await self._sync_from_peer(
                        peer_addr, peer_tracks, _progress, progress_cb,
                    )
                    peer_items = sum(
                        v for v in synced.values() if isinstance(v, int)
                    )
                    for k, v in synced.items():
                        if isinstance(v, int):
                            total_stats[k] = total_stats.get(k, 0) + v

                    if peer_items == 0:
                        continue  # not a Sautium peer, skip

                    found_any = True
                    synced_peers.add(peer_addr)

                    # Step B: this peer has data — ask it about ALL
                    # remaining unenriched tracks too
                    remaining = await self._get_unenriched_artists()
                    if not remaining:
                        break
                    all_remaining_tracks = (
                        await self._get_tracks_for_artists(remaining)
                    )
                    if all_remaining_tracks:
                        _progress(
                            f"Peer {peer_addr} has data, asking about "
                            f"{len(all_remaining_tracks)} more tracks..."
                        )
                        synced2 = await self._sync_from_peer(
                            peer_addr, all_remaining_tracks,
                            _progress, progress_cb,
                        )
                        for k, v in synced2.items():
                            if isinstance(v, int):
                                total_stats[k] = (
                                    total_stats.get(k, 0) + v
                                )

                if not found_any:
                    break  # no new data from any peer, stop

        # Re-announce newly enriched artists
        if total_stats and self._dht_service:
            _progress("Re-announcing newly enriched artists...")
            new_enriched = await self._get_enriched_artists()
            await self._dht_service.announce_artists(new_enriched)

        total_items = sum(
            v for v in total_stats.values() if isinstance(v, int)
        )
        peers_used = len(manual_peers) + len(
            peer_to_artists if 'peer_to_artists' in dir() else {}
        )
        _progress(f"P2P sync complete: {total_items} items synced")
        return total_stats

    async def _try_connect_peer(
        self, peer_addr: str
    ) -> Optional[BackendAPIClient]:
        """Try to connect to a peer, testing HTTPS then HTTP.

        Returns a working BackendAPIClient or None.
        """
        loop = asyncio.get_event_loop()

        if "://" in peer_addr:
            # Explicit scheme — try as-is
            api = BackendAPIClient(peer_addr)
            health = await loop.run_in_executor(None, api.get_health)
            return api if health else None

        # No scheme — try HTTPS (desktop peers) then HTTP (Docker)
        for scheme in ("https", "http"):
            url = f"{scheme}://{peer_addr}"
            api = BackendAPIClient(url)
            health = await loop.run_in_executor(None, api.get_health)
            if health:
                return api
        return None

    async def _sync_from_peer(
        self,
        peer_addr: str,
        track_uuids: list[str],
        _progress,
        progress_cb,
    ) -> dict:
        """Sync enrichment data from a single peer. Returns stats dict."""
        _progress(f"Connecting to {peer_addr}...")

        peer_api = await self._try_connect_peer(peer_addr)
        if not peer_api:
            _progress(f"  {peer_addr} not reachable, skipping")
            return {}

        _progress(
            f"Syncing from {peer_addr} ({len(track_uuids)} tracks)..."
        )

        sync_client = SyncClient(
            api_client=peer_api,
            db_dsn=self.db_dsn,
            batch_size=500,
            progress_cb=progress_cb,
        )

        try:
            stats = await asyncio.get_event_loop().run_in_executor(
                None,
                partial(sync_client.run_sync, track_uuids),
            )
            return stats
        except Exception as e:
            logger.error(f"Sync from {peer_addr} failed: {e}")
            _progress(f"  Sync from {peer_addr} failed: {e}")
            return {}

    # -------------------------------------------------------------------
    # Chat operations (called from launcher thread)
    # -------------------------------------------------------------------

    def send_message(self, friend_id: int, content: str) -> bool:
        """Send an encrypted message to a friend. Returns True if delivered."""
        if not self._chat_service or not self._running:
            return False

        future = asyncio.run_coroutine_threadsafe(
            self._async_send_message(friend_id, content),
            self._loop,
        )
        try:
            return future.result(timeout=30)
        except Exception as e:
            logger.error(f"Send message failed: {e}")
            return False

    async def _async_send_message(self, friend_id: int, content: str) -> bool:
        """Async: encrypt and send message to a friend."""
        msg = self._chat_service.prepare_outgoing(friend_id, content)
        if not msg:
            return False

        # Get friend's invite code for DHT lookup
        friends = self._chat_service.get_friends()
        friend = next((f for f in friends if f["id"] == friend_id), None)
        if not friend:
            return False

        # Find friend's address via DHT
        peers = []
        if self._dht_service and self._dht_service.is_available:
            peers = await self._dht_service.lookup_user(
                friend["invite_code"]
            )

        if not peers:
            logger.info(
                f"Friend {friend.get('username', '?')} offline, "
                f"message queued"
            )
            return False

        # Try each peer address
        import aiohttp
        for ip, port in peers:
            url = f"https://{ip}:{port}/api/chat/message"
            try:
                async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                    async with session.post(
                        url, json=msg, timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status == 200:
                            # Mark as delivered
                            pending = self._chat_service.get_pending_messages()
                            for pm in pending:
                                if pm["friend_id"] == friend_id:
                                    self._chat_service.mark_delivered(pm["id"])
                            return True
            except Exception as e:
                logger.debug(f"Send to {ip}:{port} failed: {e}")

        logger.info(f"Could not deliver to {friend.get('username', '?')}")
        return False

    def add_friend_by_invite(self, invite_code: str) -> Optional[dict]:
        """
        Add a friend by invite code: DHT lookup → handshake → add.

        Returns friend info dict or None if failed.
        Called from launcher thread (blocking).
        """
        if not self._running:
            return None

        future = asyncio.run_coroutine_threadsafe(
            self._async_add_friend(invite_code),
            self._loop,
        )
        try:
            return future.result(timeout=30)
        except Exception as e:
            logger.error(f"Add friend failed: {e}")
            return None

    async def _async_add_friend(self, invite_code: str) -> Optional[dict]:
        """Async: lookup invite code in DHT, handshake, add friend."""
        if not self._dht_service:
            return None

        from desktop.node_identity import get_account_info, parse_invite_code
        account = get_account_info()
        if not account:
            return None

        # DHT lookup
        peers = await self._dht_service.lookup_user(invite_code)
        if not peers:
            logger.info(f"User {invite_code} not found in DHT")
            return None

        # Try handshake with each peer
        import aiohttp
        handshake_data = {
            "public_key_hex": account["public_key_hex"],
            "username": account["username"],
            "invite_code": account["invite_code"],
        }

        for ip, port in peers:
            url = f"https://{ip}:{port}/api/chat/handshake"
            try:
                async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                    async with session.post(
                        url, json=handshake_data,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status == 200:
                            result = await resp.json()
                            if result.get("accepted"):
                                # Add friend locally
                                if self._chat_service:
                                    self._chat_service.add_friend(
                                        public_key_hex=result["public_key_hex"],
                                        invite_code=result["invite_code"],
                                        username=result.get("username", ""),
                                    )
                                return result
            except Exception as e:
                logger.debug(f"Handshake with {ip}:{port} failed: {e}")

        return None

    async def _retry_pending_messages(self):
        """Periodically retry sending undelivered messages."""
        while self._running:
            await asyncio.sleep(60)  # check every minute
            if not self._running or not self._chat_service:
                break

            pending = self._chat_service.get_pending_messages()
            if not pending:
                continue

            # Group by friend
            by_friend: dict[int, list[dict]] = {}
            for msg in pending:
                by_friend.setdefault(msg["friend_id"], []).append(msg)

            for friend_id, messages in by_friend.items():
                invite_code = messages[0]["invite_code"]
                pubkey = messages[0]["public_key_hex"]

                # Find friend via DHT
                peers = []
                if self._dht_service and self._dht_service.is_available:
                    peers = await self._dht_service.lookup_user(invite_code)

                if not peers:
                    continue

                # Try to deliver each message
                import aiohttp
                for msg in messages:
                    encrypted = self._chat_service.encrypt_message(
                        msg["content"], pubkey
                    )
                    payload = {
                        "from_public_key": self._chat_service.public_key_hex,
                        "encrypted": encrypted,
                        "timestamp": msg["timestamp"].isoformat()
                        if hasattr(msg["timestamp"], "isoformat")
                        else str(msg["timestamp"]),
                    }

                    delivered = False
                    for ip, port in peers:
                        url = f"https://{ip}:{port}/api/chat/message"
                        try:
                            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                                async with session.post(
                                    url, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=10),
                                ) as resp:
                                    if resp.status == 200:
                                        delivered = True
                                        break
                        except Exception:
                            pass

                    if delivered:
                        self._chat_service.mark_delivered(msg["id"])

    def get_status(self) -> dict:
        """Get P2P status for UI display."""
        status = {
            "running": self._running,
            "http_port": self.config.get("p2p", {}).get("listen_port", 19000),
            "chat_available": self._chat_service is not None,
        }
        if self._dht_service:
            status.update(self._dht_service.get_dht_stats())
        return status
