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
from desktop.p2p.dht_service import DHTService
from desktop.p2p.sync_server import SyncServer
from desktop.sync_client import SyncClient

logger = logging.getLogger(__name__)


class P2PManager:
    """Orchestrates P2P sync server + DHT service."""

    def __init__(self, db_dsn: str, config: dict):
        self.db_dsn = db_dsn
        self.config = config
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._sync_server: Optional[SyncServer] = None
        self._dht_service: Optional[DHTService] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._reannounce_task: Optional[asyncio.Task] = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self, node_id: str = "", progress_cb: Callable = None):
        """Start P2P services in a background thread."""
        if self._running:
            logger.warning("P2P manager already running")
            return

        p2p_cfg = self.config.get("p2p", {})
        http_port = p2p_cfg.get("listen_port", 19000)
        dht_port = http_port + 1  # DHT on next port

        self._sync_server = SyncServer(
            db_dsn=self.db_dsn,
            port=http_port,
            node_id=node_id,
        )
        self._dht_service = DHTService(
            listen_port=dht_port,
            http_port=http_port,
        )

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

            # Start DHT
            if self._dht_service.is_available:
                _progress("Starting DHT...")
                await self._dht_service.start()

                # Announce enriched artists
                _progress("Querying enriched artists...")
                artist_uuids = await self._get_enriched_artists()
                if artist_uuids:
                    _progress(
                        f"Announcing {len(artist_uuids)} artists in DHT..."
                    )
                    await self._dht_service.announce_artists(artist_uuids)
                    _progress(
                        f"P2P online: {len(artist_uuids)} artists announced"
                    )
                else:
                    _progress("P2P online: no enriched artists to announce")

                # Start periodic re-announce
                self._reannounce_task = asyncio.create_task(
                    self._dht_service.periodic_reannounce()
                )
            else:
                _progress(
                    "P2P online (HTTP only, DHT disabled — "
                    "install libtorrent for peer discovery)"
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
        if self._reannounce_task:
            self._reannounce_task.cancel()
            try:
                await self._reannounce_task
            except asyncio.CancelledError:
                pass

        if self._dht_service:
            await self._dht_service.stop()
        if self._sync_server:
            await self._sync_server.stop()

    def stop(self):
        """Stop all P2P services."""
        if not self._running and not self._loop:
            return

        logger.info("Stopping P2P manager...")
        if self._stop_event and self._loop:
            self._loop.call_soon_threadsafe(self._stop_event.set)

        if self._thread:
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                logger.warning("P2P thread did not stop cleanly")

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

        # Step 2: Collect track UUIDs for unenriched artists
        all_track_uuids = set()
        for artist_uuid in unenriched:
            tracks = await self._get_track_uuids_for_artist(artist_uuid)
            all_track_uuids.update(tracks)

        if not all_track_uuids:
            _progress("No tracks found for unenriched artists")
            return {"status": "no_tracks"}

        track_uuids = list(all_track_uuids)
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

        # Step 4: DHT lookup for remaining unenriched artists
        if self._dht_service and self._dht_service.is_available:
            # Re-check what's still unenriched after manual peers
            still_unenriched = await self._get_unenriched_artists()
            if still_unenriched:
                _progress(
                    f"Searching DHT for {len(still_unenriched)} "
                    f"remaining artists..."
                )
                peer_map = await self._dht_service.lookup_artists_batch(
                    still_unenriched
                )

                if peer_map:
                    # Group by peer
                    peer_to_artists: dict[tuple, list[str]] = {}
                    for artist_uuid, peers in peer_map.items():
                        for ip, port in peers:
                            peer_key = (ip, port)
                            peer_to_artists.setdefault(
                                peer_key, []
                            ).append(artist_uuid)

                    _progress(
                        f"Found {len(peer_to_artists)} DHT peers "
                        f"for {len(peer_map)} artists"
                    )

                    for (ip, port), artist_uuids in peer_to_artists.items():
                        # Collect tracks for these artists
                        peer_tracks = set()
                        for au in artist_uuids:
                            t = await self._get_track_uuids_for_artist(au)
                            peer_tracks.update(t)

                        if peer_tracks:
                            synced = await self._sync_from_peer(
                                f"{ip}:{port}",
                                list(peer_tracks),
                                _progress,
                                progress_cb,
                            )
                            for k, v in synced.items():
                                if isinstance(v, int):
                                    total_stats[k] = (
                                        total_stats.get(k, 0) + v
                                    )
                else:
                    _progress("No peers found in DHT")

        # Step 5: Re-announce newly enriched artists
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

    async def _sync_from_peer(
        self,
        peer_addr: str,
        track_uuids: list[str],
        _progress,
        progress_cb,
    ) -> dict:
        """Sync enrichment data from a single peer. Returns stats dict."""
        # Normalize address
        if "://" not in peer_addr:
            peer_addr = f"http://{peer_addr}"

        _progress(f"Connecting to {peer_addr}...")
        peer_api = BackendAPIClient(peer_addr)

        # Verify peer is reachable
        loop = asyncio.get_event_loop()
        health = await loop.run_in_executor(None, peer_api.get_health)
        if not health:
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
            stats = await loop.run_in_executor(
                None,
                partial(sync_client.run_sync, track_uuids),
            )
            return stats
        except Exception as e:
            logger.error(f"Sync from {peer_addr} failed: {e}")
            _progress(f"  Sync from {peer_addr} failed: {e}")
            return {}

    def get_status(self) -> dict:
        """Get P2P status for UI display."""
        status = {
            "running": self._running,
            "http_port": self.config.get("p2p", {}).get("listen_port", 19000),
        }
        if self._dht_service:
            status.update(self._dht_service.get_dht_stats())
        return status
