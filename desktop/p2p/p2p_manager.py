"""
P2P Manager for Sautium.

Orchestrates the aiohttp sync server and libtorrent DHT service.
Runs in a background asyncio event loop thread, separate from the
tkinter GUI main thread.

Provides P2P sync: find peers via DHT, sync enrichment data via HTTP.
"""

import asyncio
import json
import logging
import select
import threading
import time
from datetime import datetime, timezone
from functools import partial
from typing import Callable, Optional

import psycopg2
import psycopg2.extensions

from desktop.api_client import BackendAPIClient
from desktop.mb_slice_client import MBSliceClient
from desktop.p2p import mb_slice_queries, sync_queries
from desktop.p2p.chat_service import ChatService
from desktop.p2p.dht_service import DHTService
from desktop.p2p.lan_discovery import LANDiscovery
from desktop.p2p.sync_server import SyncServer
from desktop.p2p.upnp_service import UPnPService
from desktop.sync_client import SyncClient

logger = logging.getLogger(__name__)

# How many residual artists get a targeted per-artist DHT lookup after node
# discovery has been drained. Each lookup costs a get_peers timeout, and only
# the rare tail is announced by key at all — so this stays a probe, not a
# sweep. Sized ~2 waves of the DHT batch concurrency (20).
_DHT_TAIL_PROBE = 50


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
        self._chat_notify: Optional[asyncio.Event] = None
        self._reannounce_task: Optional[asyncio.Task] = None
        self._upnp_renewal_task: Optional[asyncio.Task] = None
        self._pending_retry_task: Optional[asyncio.Task] = None
        self._resolve_friends_task: Optional[asyncio.Task] = None
        self._pending_accepts_task: Optional[asyncio.Task] = None
        self._lan_discovery_task: Optional[asyncio.Task] = None
        self._db_listen_task: Optional[asyncio.Task] = None
        self._sync_request_task: Optional[asyncio.Task] = None
        self._sync_request_listen_task: Optional[asyncio.Task] = None
        self._auto_sync_task: Optional[asyncio.Task] = None
        self._sync_request_notify: Optional[asyncio.Event] = None
        self._sync_lock: Optional[asyncio.Lock] = None
        self._mb_slice_task: Optional[asyncio.Task] = None
        self._mb_slice_lock: Optional[asyncio.Lock] = None
        self._mb_dump_version: Optional[str] = None
        self._running = False
        self._on_message_cb: Optional[Callable] = None
        # Peer address cache: friend_id -> peers_list
        # Persists until connection failure triggers refresh.
        self._friend_peer_cache: dict[int, list[tuple]] = {}
        # Friends whose history has been pulled this session (skip re-pull)
        self._history_synced_friends: set[int] = set()
        self._master_wake_task: Optional[asyncio.Task] = None
        self._reachability_task: Optional[asyncio.Task] = None
        # unknown | reachable | cgnat | unreachable — see _reachability_loop
        self._reachability_status = "unknown"

    @property
    def is_running(self) -> bool:
        return self._running

    def set_on_message_callback(self, cb: Callable):
        """Set callback for incoming chat messages (called from P2P thread)."""
        self._on_message_cb = cb

    def notify_new_message(self):
        """Wake up the chat delivery loop immediately (thread-safe)."""
        if self._loop and self._chat_notify and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._chat_notify.set)

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

        # Birth certificate: silent fetch when missing (idempotent issuance —
        # a re-fetch after moving devices returns the original birth date;
        # network failures degrade to None and are logged inside).
        if account_info:
            from desktop.p2p.birth_cert import ensure_certificate
            ensure_certificate()

        backend_port = self.config.get("ports", {}).get("web", 0)
        # MB dump capability: serve slices only with a completed FULL dump
        # (VERSION marker + mb_artist rows on this DSN) — partial slice
        # holders must never advertise, they'd hand out incomplete worlds.
        self._mb_dump_version = None
        if self.config.get("mb_slice", {}).get("serve", True):
            try:
                conn = psycopg2.connect(self.db_dsn)
                try:
                    self._mb_dump_version = (
                        mb_slice_queries.local_dump_available(conn))
                finally:
                    conn.close()
            except Exception as e:
                logger.debug(f"MB dump capability check failed: {e}")
        self._sync_server = SyncServer(
            db_dsn=self.db_dsn,
            port=http_port,
            node_id=node_id,
            account_info=account_info,
            backend_port=backend_port,
            mb_dump_version=self._mb_dump_version,
        )
        self._dht_service = DHTService(
            listen_port=dht_port,
            http_port=http_port,
        )
        docker_ports = p2p_cfg.get("docker_ports", [])
        invite_code = account_info.get("invite_code", "") if account_info else ""
        self._lan_discovery = LANDiscovery(
            sync_port=http_port,
            node_id=node_id,
            invite_code=invite_code,
            localhost_probe_ports=docker_ports,
        )
        # UPnP: map ONLY the P2P sync port. Never include backend / docker
        # ports here — the backend has no app-level auth (see CLAUDE.md
        # "Security Posture"). docker_ports above is for LAN-discovery
        # probing of localhost only.
        self._upnp = UPnPService(ports=[http_port])

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
                self._sync_server.set_delivery_trigger(
                    self._deliver_pending_fast
                )
                self._sync_server.set_inbound_cb(
                    self._note_inbound_reachable
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
        # Suppress transient Windows network errors (WinError 64, etc.)
        # that occur when a peer disconnects during TLS accept handshake.
        # These fire inside the proactor's own connection_lost path, where
        # there is nothing left to recover — the awaited call already saw
        # the disconnect and handled it. Long-lived relay wake streams and
        # probe connect-backs make abrupt closes routine, so the noise
        # would otherwise drown the log.
        _default_handler = self._loop.get_exception_handler()

        def _exception_handler(loop, context):
            exc = context.get("exception")
            if isinstance(exc, OSError) and getattr(exc, "winerror", None) in (
                64,     # network name no longer available
                121,    # semaphore timeout
                10053,  # connection aborted by local host
                10054,  # connection reset by peer
            ):
                return  # suppress
            if _default_handler:
                _default_handler(loop, context)
            else:
                loop.default_exception_handler(context)

        self._loop.set_exception_handler(_exception_handler)
        try:
            self._loop.run_until_complete(
                self._async_main(node_id, progress_cb)
            )
        except Exception as e:
            logger.error(f"P2P event loop error: {e}")
        finally:
            self._running = False
            # Cancel any tasks still pending (defensive — _cleanup() should
            # have got them all, but better than leaking on close).
            try:
                pending = [
                    t for t in asyncio.all_tasks(self._loop)
                    if not t.done()
                ]
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception as e:
                logger.debug(f"Pending task drain failed: {e}")
            try:
                self._loop.close()
            except Exception as e:
                logger.debug(f"Event loop close failed: {e}")

    async def _async_main(self, node_id: str, progress_cb: Callable = None):
        """Main async routine: start services, announce, wait."""
        self._stop_event = asyncio.Event()
        self._chat_notify = asyncio.Event()
        self._sync_request_notify = asyncio.Event()
        self._sync_lock = asyncio.Lock()
        self._mb_slice_lock = asyncio.Lock()

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
                # Renewal loop regardless of the first attempt's outcome —
                # renew_ports() retries a full open when nothing is mapped,
                # so a router that appears later still gets picked up.
                self._upnp_renewal_task = asyncio.create_task(
                    self._upnp_renewal_loop()
                )

            # Cheap local CGNAT tells gate the very first announces — an
            # unreachable node's announces are dead-address DHT pollution.
            # The probe/passive signals refine this in _reachability_loop.
            heuristic = self._reachability_heuristic()
            if heuristic:
                self._dht_service.set_announces_enabled(False)
                _progress(f"Reachability: {heuristic[0]} — "
                          "DHT announces suppressed")

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

                # The discovery key — how peers find this node at all.
                await self._dht_service.announce_node()

                # Rare-artist tail. Paced (dht_service.ANNOUNCE_CHUNK), so it
                # runs as a background task; awaiting it would stall startup.
                _progress("Querying enriched artists...")
                enriched = await self._get_enriched_artists()
                self._lan_discovery.update_enriched_count(len(enriched))
                tail = await self._get_announce_tail()
                if tail:
                    asyncio.create_task(
                        self._dht_service.announce_artists(tail)
                    )
                    _progress(
                        f"P2P online: node announced, {len(tail)} rare "
                        f"artists queued"
                    )
                else:
                    _progress("P2P online: node announced")

                # Advertise the MB dump capability so dump-less nodes can
                # find this node beyond LAN/manual peers
                if self._mb_dump_version:
                    await self._dht_service.announce_capability("mbdump")

                # Start periodic re-announce
                self._reannounce_task = asyncio.create_task(
                    self._dht_service.periodic_reannounce()
                )
            else:
                _progress(
                    "P2P online (LAN only, DHT disabled — "
                    "install libtorrent for internet discovery)"
                )

            # Start chat history sync + friend resolution
            # (works via LAN even without DHT)
            if self._chat_service:
                try:
                    await self._ensure_master_contact()
                except Exception as e:
                    # A shipped-contact hiccup must never take P2P down —
                    # everything below (chat, sync, discovery) is the real
                    # product; the master contact is a convenience.
                    logger.error(f"Master contact seeding failed: {e}")
                self._pending_retry_task = asyncio.create_task(
                    self._sync_chat_histories()
                )
                self._resolve_friends_task = asyncio.create_task(
                    self._resolve_pending_friends()
                )
                self._db_listen_task = asyncio.create_task(
                    self._listen_for_db_notifications()
                )
                self._pending_accepts_task = asyncio.create_task(
                    self._poll_pending_accepts()
                )
                self._master_wake_task = asyncio.create_task(
                    self._master_wake_loop()
                )
            self._reachability_task = asyncio.create_task(
                self._reachability_loop()
            )

            # Sync triggers: NOTIFY sautium_sync_request (Web UI Force
            # sync now → backend → here) + auto-sync timer
            # (sync.auto_interval_min). Both serialise through
            # self._sync_lock so concurrent triggers merge to one run.
            self._sync_request_listen_task = asyncio.create_task(
                self._sync_request_listener_thread()
            )
            self._sync_request_task = asyncio.create_task(
                self._sync_request_loop()
            )
            self._auto_sync_task = asyncio.create_task(
                self._auto_sync_loop()
            )
            self._mb_slice_task = asyncio.create_task(
                self._mb_slice_loop()
            )

            self._running = True

            # Monitor sync server — restart if it crashes.
            # Check every 10 seconds with a self-health probe.
            self._health_fail_count = 0
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=10
                    )
                    break  # stop_event was set
                except asyncio.TimeoutError:
                    pass  # check sync server health

                needs_restart = await self._check_sync_server_health()

                if needs_restart:
                    logger.warning(
                        "Sync server not responding, restarting..."
                    )
                    try:
                        await self._sync_server.stop()
                        await self._sync_server.start()
                        self._health_fail_count = 0
                        _progress("Sync server restarted")
                    except Exception as e:
                        logger.error(f"Sync server restart failed: {e}")

        except Exception as e:
            logger.error(f"P2P startup failed: {e}")
            _progress(f"P2P error: {e}")
        finally:
            await self._cleanup()

    async def _check_sync_server_health(self) -> bool:
        """Check if the sync server is actually accepting connections.

        Returns True if the server needs to be restarted.

        First checks socket-level state (fast), then falls back to a
        self-connection probe every 3 consecutive failures to avoid
        false positives from transient network hiccups.
        """
        if not self._sync_server or not self._sync_server._site:
            return True

        site = self._sync_server._site
        # Fast checks: server object or socket clearly dead
        if site._server is None:
            return True
        if site._server.sockets is not None and len(
            site._server.sockets
        ) == 0:
            return True
        try:
            if (site._server.sockets
                    and site._server.sockets[0].fileno() == -1):
                return True
        except Exception:
            return True

        # Self-connection probe: catches the Python 3.13 proactor bug
        # where the socket looks alive but the accept loop is dead.
        # Plain TCP connect is enough — if the accept loop is dead the OS
        # won't complete the handshake regardless of TLS.
        import socket

        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: sock.connect(("127.0.0.1", self._http_port)),
            )
            self._health_fail_count = 0
            return False  # server is healthy
        except Exception as exc:
            self._health_fail_count += 1
            if self._health_fail_count >= 3:
                logger.warning(
                    "Sync server health check failed "
                    f"{self._health_fail_count} times: {exc}"
                )
                return True  # needs restart
            return False  # might be transient
        finally:
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass

    async def _upnp_renewal_loop(self):
        """Re-map before the 1-hour UPnP lease expires (at 75% of
        LEASE_DURATION). Lease renewal is a timer-based protocol
        requirement, not state polling: without it the router silently
        drops the mapping after an hour and the node loses internet
        reachability until relaunch (outbound still works, so it just
        looks like 'the friend is offline'). renew_ports() also recovers
        a rebooted router via full re-discover; a changed external port
        is pushed into the DHT announce state, which the 15-min periodic
        re-announce then publishes."""
        from desktop.p2p.upnp_service import LEASE_DURATION
        interval = LEASE_DURATION * 0.75
        while True:
            await asyncio.sleep(interval)
            try:
                ok = await asyncio.get_event_loop().run_in_executor(
                    None, self._upnp.renew_ports
                )
                if ok:
                    ext_port = self._upnp.get_external_port(self._http_port)
                    if ext_port:
                        self._dht_service.set_announce_port(ext_port)
                else:
                    logger.info("UPnP: no active mapping after renewal attempt")
            except Exception as e:
                logger.warning(f"UPnP renewal loop error: {e}")

    async def _cleanup(self):
        """Clean shutdown of all services."""
        for task in (self._reannounce_task, self._pending_retry_task,
                     self._resolve_friends_task, self._db_listen_task,
                     self._pending_accepts_task, self._lan_discovery_task,
                     self._sync_request_listen_task, self._sync_request_task,
                     self._auto_sync_task, self._mb_slice_task,
                     self._upnp_renewal_task, self._master_wake_task,
                     self._reachability_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.debug(f"Background task error during cleanup: {e}")

        if self._lan_discovery:
            await self._lan_discovery.stop()
        if self._upnp:
            self._upnp.close_ports()
        if self._dht_service:
            await self._dht_service.stop()
        if self._sync_server:
            await self._sync_server.stop()

    def stop(self):
        """Stop all P2P services.

        Called from the launcher thread — must NOT touch the event loop
        directly (`run_until_complete`, `close`, etc.). Anything async
        is scheduled inside the loop thread via `run_coroutine_threadsafe`,
        so `_async_main`'s `finally` block runs `_cleanup()` to completion
        and the aiohttp socket is fully released before this returns.
        """
        if not self._running and not self._loop:
            return

        logger.info("Stopping P2P manager...")

        # Break out of DHT bootstrap wait (sync flag, safe from any thread)
        if self._dht_service:
            self._dht_service._running = False

        # Signal _async_main to exit its wait loop. The loop thread itself
        # awaits the event and runs `_cleanup()` in its `finally` block.
        if self._loop and not self._loop.is_closed():
            async def _signal():
                if self._stop_event:
                    self._stop_event.set()
                if self._chat_notify:
                    self._chat_notify.set()

            try:
                fut = asyncio.run_coroutine_threadsafe(_signal(), self._loop)
                fut.result(timeout=2)
            except Exception as e:
                logger.warning(f"P2P stop signal failed: {e}")

        # Wait for the event-loop thread to actually exit. _async_main runs
        # `_cleanup()` (which awaits sync_server.stop / dht.stop / lan.stop)
        # before returning, so by the time the thread is dead, all sockets
        # are released.
        if self._thread:
            self._thread.join(timeout=15)
            if self._thread.is_alive():
                logger.error(
                    "P2P loop thread did not exit within 15s — "
                    "sync server socket may leak"
                )

        self._running = False
        self._loop = None
        self._thread = None
        logger.info("P2P manager stopped")

    # psycopg2.connect() is a sync C-call: invoking it directly from a
    # coroutine blocks the event loop until the TCP/auth handshake returns
    # (seconds, especially during a backend restart). While blocked, every
    # `loop.call_soon_threadsafe(...)` from another thread is queued but
    # never runs — `stop()` then times out and force-cleanup leaks the
    # aiohttp socket. Always do the full connect+query+close inside the
    # executor.

    async def _get_enriched_artists(self) -> list[str]:
        """Query local DB for enriched artist UUIDs."""
        def _blocking() -> list[str]:
            conn = psycopg2.connect(self.db_dsn)
            conn.autocommit = True
            try:
                return sync_queries.get_enriched_artist_uuids(conn)
            finally:
                conn.close()
        return await asyncio.get_event_loop().run_in_executor(None, _blocking)

    async def _get_announce_tail(self) -> list[str]:
        """Rare-artist tail for DHT announcing, sized by sync.announce_limit
        (0/null = node key only)."""
        def _blocking() -> list[str]:
            conn = psycopg2.connect(self.db_dsn)
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT value FROM user_settings WHERE key = %s",
                        ("sync.announce_limit",))
                    row = cur.fetchone()
                limit = int(row[0]) if row and row[0] else 0
                return sync_queries.get_announce_tail_uuids(conn, limit)
            finally:
                conn.close()
        return await asyncio.get_event_loop().run_in_executor(None, _blocking)

    async def _get_unenriched_artists(self) -> list[str]:
        """Audio-only-empty artists — DHT lookup candidates (cheap to
        skip when we have any audio data; partial gaps are filled by
        _get_incomplete_artists in the manual/LAN sync path)."""
        def _blocking() -> list[str]:
            conn = psycopg2.connect(self.db_dsn)
            conn.autocommit = True
            try:
                return sync_queries.get_unenriched_artist_uuids(conn)
            finally:
                conn.close()
        return await asyncio.get_event_loop().run_in_executor(None, _blocking)

    async def _get_incomplete_artists(self) -> list[str]:
        """Artists missing data in any sync category — manual/LAN trigger
        set. Catches partial-sync states (audio landed but Last.fm bio
        didn't, etc.) that the audio-only AND-logic in
        _get_unenriched_artists silently skipped."""
        def _blocking() -> list[str]:
            conn = psycopg2.connect(self.db_dsn)
            conn.autocommit = True
            try:
                return sync_queries.get_incomplete_artist_uuids(conn)
            finally:
                conn.close()
        return await asyncio.get_event_loop().run_in_executor(None, _blocking)

    async def _get_tracks_for_artists(
        self, artist_uuids: list[str]
    ) -> list[str]:
        """Get all track UUIDs for a list of artists (single query)."""
        if not artist_uuids:
            return []

        def _blocking() -> list[str]:
            conn = psycopg2.connect(self.db_dsn)
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT DISTINCT track_id::text FROM track_artists "
                        "WHERE artist_id = ANY(%s::uuid[])",
                        [artist_uuids],
                    )
                    return [r[0] for r in cur.fetchall()]
            finally:
                conn.close()
        return await asyncio.get_event_loop().run_in_executor(None, _blocking)

    async def _get_track_uuids_for_artist(
        self, artist_uuid: str
    ) -> list[str]:
        """Get track UUIDs for a specific artist."""
        def _blocking() -> list[str]:
            conn = psycopg2.connect(self.db_dsn)
            conn.autocommit = True
            try:
                return sync_queries.get_track_uuids_for_artist(
                    conn, artist_uuid
                )
            finally:
                conn.close()
        return await asyncio.get_event_loop().run_in_executor(None, _blocking)

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

        # Step 1: Find artists missing data in any sync category.
        # `incomplete` is broader than `unenriched` (which is audio-only,
        # AND-logic) — incomplete catches partial states like
        # "audio landed, Last.fm bio never came through" that the old
        # gate silently skipped. Inventory + _compute_needed inside
        # the peer sync handles per-category filtering so a wide
        # trigger here costs only one inventory round-trip per peer
        # when nothing new exists.
        _progress("Finding artists needing sync...")
        incomplete = await self._get_incomplete_artists()
        if not incomplete:
            _progress("All artists fully synced!")
            return {"status": "all_synced"}

        _progress(f"Found {len(incomplete)} artists with missing data")

        # Step 2: Collect track UUIDs for all incomplete artists (single query)
        track_uuids = await self._get_tracks_for_artists(incomplete)

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
                        is_lan=True,
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

        # Build a set of DHT addresses to skip (only for our external IP):
        # - our own node (external_ip:announce_port)
        # - LAN peers via external IP (already synced above)
        # - UPnP-mapped Docker ports (not Sautium sync servers)
        skip_dht_addrs: set[str] = set()
        ext_ip = self._upnp.external_ip if self._upnp else None
        if ext_ip:
            # Skip self
            if self._dht_service:
                skip_dht_addrs.add(
                    f"{ext_ip}:{self._dht_service._announce_port}"
                )
            # Skip LAN peers (same router = same external IP)
            if self._lan_discovery:
                for lip, lport in self._lan_discovery.peers:
                    skip_dht_addrs.add(f"{ext_ip}:{lport}")
            # Skip UPnP-mapped Docker ports
            if self._upnp:
                for ext_port, int_port in self._upnp._mapped:
                    skip_dht_addrs.add(f"{ext_ip}:{ext_port}")
                    skip_dht_addrs.add(f"{ext_ip}:{int_port}")
        if skip_dht_addrs:
            logger.debug(f"DHT skip list: {skip_dht_addrs}")

        if self._dht_service and self._dht_service.is_available:
            dht_seen: set[str] = set()

            async def _drain_peer(peer_addr: str):
                """Ask one peer about everything we're still missing. The
                inventory call does the matching, so there is no reason to
                ask about a subset — this is what made per-artist discovery
                pointless in the first place."""
                remaining = await self._get_unenriched_artists()
                if not remaining:
                    return
                tracks = await self._get_tracks_for_artists(remaining)
                if not tracks:
                    return
                _progress(
                    f"Asking {peer_addr} about {len(tracks)} tracks..."
                )
                synced = await self._sync_from_peer(
                    peer_addr, tracks, _progress, progress_cb,
                )
                items = 0
                for k, v in synced.items():
                    if isinstance(v, int):
                        total_stats[k] = total_stats.get(k, 0) + v
                        items += v
                if items:
                    synced_peers.add(peer_addr)

            async def _drain_new(peers) -> None:
                for ip, port in peers:
                    addr = f"{ip}:{port}"
                    if (addr in dht_seen or addr in synced_peers
                            or addr in skip_dht_addrs):
                        continue
                    dht_seen.add(addr)
                    await _drain_peer(addr)

            # Step A: node discovery — ONE lookup yields nodes, and each
            # node's inventory answers for the whole library at once.
            _progress("Searching DHT for nodes...")
            await _drain_new(await self._dht_service.lookup_nodes())

            # Step B: residual artists — targeted keys against the rare-artist
            # tail peers announce. Small by construction: whatever is common
            # enough to sit on a random node was already drained in Step A.
            residual = await self._get_unenriched_artists()
            if residual:
                probe = residual[:_DHT_TAIL_PROBE]
                _progress(
                    f"Searching DHT for {len(probe)} of {len(residual)} "
                    f"rare artists..."
                )
                peer_map = await self._dht_service.lookup_artists_batch(probe)
                for peers in peer_map.values():
                    await _drain_new(peers)

        # Sync may have enriched artists that now belong in the rare tail
        # (paced — background task, the sync result must not wait for it).
        if total_stats and self._dht_service:
            tail = await self._get_announce_tail()
            if tail:
                _progress("Re-announcing the rare-artist tail...")
                asyncio.create_task(self._dht_service.announce_artists(tail))
            self._lan_discovery.update_enriched_count(
                len(await self._get_enriched_artists()))

        total_items = sum(
            v for v in total_stats.values() if isinstance(v, int)
        )
        _progress(f"P2P sync complete: {total_items} items synced")
        return total_stats

    async def _try_connect_peer(
        self, peer_addr: str, is_lan: bool = False,
    ) -> Optional[BackendAPIClient]:
        """Try to connect to a peer, testing HTTPS then HTTP.

        Returns a working BackendAPIClient or None.
        For LAN peers (is_lan=True), retries once — the first connection
        from a fresh process can fail due to OS-level cold-start overhead.
        """
        loop = asyncio.get_event_loop()

        if "://" in peer_addr:
            # Explicit scheme — try as-is
            api = BackendAPIClient(peer_addr)
            attempts = 2 if is_lan else 1
            for attempt in range(attempts):
                health = await loop.run_in_executor(
                    None, api.get_health
                )
                if health:
                    return api
                if attempt == 0 and is_lan:
                    logger.info(
                        f"  LAN peer {peer_addr} not reachable, "
                        f"retrying in 5s..."
                    )
                    await asyncio.sleep(5)
            return None

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
        is_lan: bool = False,
    ) -> dict:
        """Sync enrichment data from a single peer. Returns stats dict."""
        _progress(f"Connecting to {peer_addr}...")

        peer_api = await self._try_connect_peer(peer_addr, is_lan=is_lan)
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
        # Check friend has a real public key before encrypting
        friends = self._chat_service.get_friends()
        friend = next((f for f in friends if f["id"] == friend_id), None)
        if not friend:
            return False

        if friend["public_key_hex"].startswith("pending:"):
            logger.info(
                f"Friend {friend.get('username', '?')} not yet resolved, "
                f"message queued"
            )
            # Store undelivered message even though we can't encrypt yet
            self._chat_service.store_message(
                friend_id, "out", content, delivered=False
            )
            return False

        msg = self._chat_service.prepare_outgoing(friend_id, content)
        if not msg:
            return False

        msg_uuid = msg.get("message_uuid")

        # Find friend's address via LAN + DHT
        peers = await self._find_friend_peers(friend)

        if not peers:
            logger.info(
                f"Friend {friend.get('username', '?')} offline, "
                f"message queued for history sync"
            )
            return False

        # Try each peer address; on total failure refresh cache and retry once
        import aiohttp

        async def _try_send(targets: list[tuple]) -> bool:
            for ip, port in targets:
                url = f"https://{ip}:{port}/api/chat/message"
                try:
                    async with aiohttp.ClientSession(
                        connector=aiohttp.TCPConnector(ssl=False)
                    ) as session:
                        async with session.post(
                            url, json=msg,
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp:
                            if resp.status == 200:
                                if msg_uuid:
                                    self._chat_service.mark_delivered_by_uuid(
                                        msg_uuid
                                    )
                                return True
                except Exception as e:
                    logger.debug(f"Send to {ip}:{port} failed: {e}")
            return False

        if await _try_send(peers):
            return True

        # All cached peers failed — refresh and retry once
        fresh = await self._find_friend_peers(friend, refresh=True)
        if fresh and fresh != peers:
            if await _try_send(fresh):
                return True

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
                                    # Receiving "accepted" means the
                                    # remote launcher is reachable; mark
                                    # the friend live so the UI shows
                                    # them online without waiting for a
                                    # message exchange.
                                    self._chat_service.update_friend_last_seen(
                                        result["public_key_hex"]
                                    )
                                    # Sync chat history after handshake
                                    friend = (
                                        self._chat_service
                                        .get_friend_by_public_key(
                                            result["public_key_hex"]
                                        )
                                    )
                                    if friend:
                                        await self._sync_chat_history(
                                            friend, [(ip, port)]
                                        )
                                return result
            except Exception as e:
                logger.debug(f"Handshake with {ip}:{port} failed: {e}")

        return None

    def _note_peer_alive(self, friend_id: Optional[int],
                         pubkey: Optional[str], ip: str, port: int) -> None:
        """One event, two facts: this friend answered us AT this address,
        just now.

        The address goes to the head of the candidate list — a DHT hit only
        proves someone announced an address, never that it answers, so the
        proven one must outrank it.

        `last_seen` moves too, because an answered request is presence.
        Without this a passive peer — the master never pushes, by design —
        goes "offline" five minutes after the handshake while its wake
        stream is still connected and its history pulls still succeed.
        """
        if friend_id:
            peer = (ip, port)
            cached = [p for p in self._friend_peer_cache.get(friend_id, [])
                      if p != peer]
            self._friend_peer_cache[friend_id] = [peer] + cached
        if pubkey and not pubkey.startswith("pending:"):
            self._chat_service.update_friend_last_seen(pubkey)

    async def _find_friend_peers(
        self, friend: dict, refresh: bool = False,
    ) -> list[tuple]:
        """Candidate addresses for a friend — the SINGLE source every path
        uses (resolve, push, history pull, wake subscribe). Order: LAN by
        identity, then last-known-good cache, then DHT, then every other
        LAN peer as a final tier.

        That last tier matters on a peer's own LAN: the address a node
        announces in the DHT is its router's public IP, which answers only
        with NAT hairpinning and usually doesn't — while the same node sits
        one hop away on the LAN (or on 127.0.0.1 as a localhost-probed
        Docker node).

        Args:
            friend: friend dict with id, invite_code, public_key_hex.
            refresh: if True, skip cache and do fresh DHT lookup.
                     If fresh lookup finds nothing, the old cached
                     address is kept (peer may come back at same addr).
        """
        fid = friend.get("id")
        invite_code = friend.get("invite_code", "")
        pubkey = friend.get("public_key_hex", "")

        peers: list[tuple] = []

        def add(candidates):
            for p in candidates:
                if p and p not in peers:
                    peers.append(p)

        # 1. LAN by identity — instant and always right when it hits.
        if self._lan_discovery:
            lan = self._lan_discovery.find_peer_by_invite_code(invite_code)
            if not lan and pubkey and not pubkey.startswith("pending:"):
                lan = self._lan_discovery.find_peer_by_node_id(pubkey)
            add([lan])

        # 2. Last known good (_note_peer_alive puts the winner first).
        if fid and not refresh:
            add(self._friend_peer_cache.get(fid, []))

        # 3. DHT — an announcement, not a promise of reachability.
        if self._dht_service and self._dht_service.is_available:
            add(await self._dht_service.lookup_user(invite_code))

        if fid and peers:
            self._friend_peer_cache[fid] = list(peers)
        elif fid:
            add(self._friend_peer_cache.get(fid, []))

        # 4. Every other live LAN peer. A handful of addresses, and the
        # reason a same-LAN or localhost-probed node stays reachable when
        # its DHT address is an unhairpinnable public IP.
        if self._lan_discovery:
            add(self._lan_discovery.peers)

        return peers

    async def _sync_chat_history(
        self, friend: dict, peers: list[tuple],
    ) -> dict:
        """Request chat history from a friend's peer and import it.

        Args:
            friend: friend dict with id, public_key_hex, username, etc.
            peers: list of (ip, port) tuples for the friend's peer.

        Returns: import stats {imported, skipped, delivered} or {}.
        """
        if not self._chat_service or not peers:
            return {}

        friend_pubkey = friend.get("public_key_hex", "")
        if not friend_pubkey or friend_pubkey.startswith("pending:"):
            return {}

        from desktop.node_identity import get_account_info, sign_message
        account = get_account_info()
        if not account:
            return {}

        # Sign request to prove key ownership (prevents metadata leakage).
        # Timestamp-bound (±60s server-side) so a captured signature cannot
        # replay forever — mirrors the HMAC window.
        import uuid as _uuid
        nonce = str(_uuid.uuid4())
        ts = int(time.time())
        sig_bytes = sign_message(
            f"history_request:{ts}:{nonce}".encode("utf-8"))

        payload = {
            "public_key_hex": account["public_key_hex"],
            "nonce": nonce,
            "ts": ts,
            "signature": sig_bytes.hex(),
        }

        import aiohttp
        for ip, port in peers:
            url = f"https://{ip}:{port}/api/chat/history"
            try:
                async with aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(ssl=False)
                ) as session:
                    async with session.post(
                        url, json=payload,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                        self._note_peer_alive(
                            friend.get("id"), friend_pubkey, ip, port)
            except Exception as e:
                logger.debug(f"History request to {ip}:{port} failed: {e}")
                continue

            # Decrypt each message and prepare for import
            raw_messages = data.get("messages", [])
            if not raw_messages:
                logger.debug(
                    f"No history from {ip}:{port} for "
                    f"{friend.get('username', '?')}"
                )
                return {"imported": 0, "skipped": 0, "delivered": 0}

            decrypted = []
            for msg in raw_messages:
                try:
                    content = self._chat_service.decrypt_message(
                        msg["encrypted"], friend_pubkey
                    )
                    decrypted.append({
                        "message_uuid": msg.get("message_uuid"),
                        "direction": msg["direction"],
                        "content": content,
                        "timestamp": msg["timestamp"],
                    })
                except Exception as e:
                    logger.debug(f"Failed to decrypt history msg: {e}")

            if not decrypted:
                return {"imported": 0, "skipped": 0, "delivered": 0}

            stats = self._chat_service.import_history(
                friend["id"], decrypted
            )
            logger.info(
                f"History sync with {friend.get('username', '?')}: "
                f"{stats}"
            )

            # Notify UI if new messages were imported
            if stats.get("imported", 0) > 0 and self._on_message_cb:
                try:
                    self._on_message_cb({
                        "friend_id": friend["id"],
                        "content": "",
                        "timestamp": None,
                        "history_sync": True,
                    })
                except Exception:
                    pass

            return stats

        return {}

    async def _resolve_pending_friends(self):
        """Periodically resolve pending friends via LAN handshake."""
        await asyncio.sleep(5)  # initial delay for LAN beacons to arrive
        while self._running:
            try:
                if self._chat_service and self._lan_discovery:
                    await self._do_resolve_pending()
            except Exception as e:
                logger.debug(f"Resolve pending friends error: {e}")
            await asyncio.sleep(15)  # check every 15 seconds

    async def _do_resolve_pending(self):
        """Check LAN/DHT peers for pending friends and do handshake."""
        friends = self._chat_service.get_friends()

        # Collect invite codes that are already resolved (have real public key)
        resolved_invites = {
            f["invite_code"] for f in friends
            if not f["public_key_hex"].startswith("pending:")
        }

        pending = []
        for f in friends:
            if not f["public_key_hex"].startswith("pending:"):
                continue
            if f["invite_code"] in resolved_invites:
                # Stale duplicate — real entry already exists, remove this one
                self._chat_service.remove_friend(f["id"])
                logger.info(
                    f"Removed stale pending entry for {f['invite_code']}"
                )
                continue
            pending.append(f)

        if not pending:
            return

        from desktop.node_identity import get_account_info
        account = get_account_info()
        if not account:
            return

        import aiohttp
        handshake_data = {
            "public_key_hex": account["public_key_hex"],
            "username": account["username"],
            "invite_code": account["invite_code"],
        }

        for friend in pending:
            invite = friend.get("invite_code", "")
            if not invite:
                continue

            # Token invites auto-accept: attach the token + a fresh signed
            # timestamp (the issuer skips its mutual-add check on this
            # path, so it demands proof of key possession). Birth cert
            # rides along whenever we have one — the master token requires
            # it as its anti-sybil gate.
            per_friend = dict(handshake_data)
            join_token = friend.get("join_token_id")
            if join_token:
                import time as _time

                from desktop.node_identity import sign_message
                ts = int(_time.time())
                per_friend["token_id"] = join_token
                per_friend["ts"] = ts
                per_friend["signature"] = sign_message(
                    f"token_handshake:{ts}:{join_token}:{invite}"
                    .encode("utf-8")).hex()
                from desktop.p2p.birth_cert import load_certificate
                cert = load_certificate()
                if cert:
                    per_friend["birth_cert"] = cert

            peers = await self._find_friend_peers(friend)

            if not peers:
                continue

            resolved = False
            for ip, port in peers:
                url = f"https://{ip}:{port}/api/chat/handshake"
                try:
                    async with aiohttp.ClientSession(
                        connector=aiohttp.TCPConnector(ssl=False)
                    ) as session:
                        async with session.post(
                            url, json=per_friend,
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp:
                            if resp.status == 200:
                                result = await resp.json()
                                if result.get("accepted"):
                                    from desktop.p2p.master_node import (
                                        MASTER_PUBKEY_HEX,
                                    )
                                    if (friend.get("source") == "master"
                                            and result["public_key_hex"]
                                            != MASTER_PUBKEY_HEX):
                                        # 48-bit invite fingerprints are
                                        # guessable; the master is pinned
                                        # by its full key — a DHT
                                        # impersonator stops here.
                                        logger.warning(
                                            "Master handshake from %s:%s "
                                            "returned a foreign pubkey — "
                                            "ignoring", ip, port)
                                        continue
                                    self._chat_service.add_friend(
                                        public_key_hex=result[
                                            "public_key_hex"
                                        ],
                                        invite_code=result.get(
                                            "invite_code", invite
                                        ),
                                        username=result.get(
                                            "username", ""
                                        ),
                                    )
                                    self._store_incoming_grant(
                                        result, account)
                                    # Mirror the recipient-side
                                    # presence bump: a 200 "accepted"
                                    # is proof the remote launcher is
                                    # alive. Without this update one
                                    # side of a freshly-paired duo
                                    # always shows the other as
                                    # offline because only the
                                    # accept-handler bumps last_seen.
                                    self._chat_service.update_friend_last_seen(
                                        result["public_key_hex"]
                                    )
                                    logger.info(
                                        f"Pending friend resolved via "
                                        f"handshake: {invite} "
                                        f"({ip}:{port})"
                                    )
                                    resolved = True
                                    # Sync chat history after handshake
                                    resolved_friend = (
                                        self._chat_service
                                        .get_friend_by_public_key(
                                            result["public_key_hex"]
                                        )
                                    )
                                    self._note_peer_alive(
                                        (resolved_friend or {}).get("id"),
                                        result["public_key_hex"], ip, port)
                                    if resolved_friend:
                                        await self._sync_chat_history(
                                            resolved_friend,
                                            [(ip, port)],
                                        )
                                    break

                            # Handshake rejected — try nudge (auto-reciprocate
                            # via Worker). Peer checks their pending accepts
                            # and adds us if found.
                            if resp.status == 403:
                                resolved = await self._try_nudge(
                                    ip, port, invite, handshake_data,
                                    session,
                                )
                                if resolved:
                                    break
                except Exception as e:
                    logger.debug(
                        f"Handshake to {ip}:{port} for {invite} "
                        f"failed: {e}"
                    )
            if resolved:
                continue

    async def _try_nudge(
        self,
        ip: str,
        port: int,
        invite: str,
        handshake_data: dict,
        session,
    ) -> bool:
        """Send invite-accepted nudge to a peer after handshake rejection.

        The peer checks the Worker for pending accepts and auto-adds us.
        Returns True if the nudge succeeded and the friend was resolved.
        """
        url = f"https://{ip}:{port}/api/chat/invite-accepted"
        try:
            import aiohttp
            async with session.post(
                url, json=handshake_data,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get("accepted"):
                        self._chat_service.add_friend(
                            public_key_hex=result["public_key_hex"],
                            invite_code=result.get("invite_code", invite),
                            username=result.get("username", ""),
                        )
                        # Mirror the bump in the regular handshake
                        # initiator path: a successful nudge proves
                        # the peer is alive, so record presence now
                        # — the friend would otherwise read "offline"
                        # in the UI right after pairing.
                        self._chat_service.update_friend_last_seen(
                            result["public_key_hex"]
                        )
                        logger.info(
                            f"Pending friend resolved via nudge: "
                            f"{invite} ({ip}:{port})"
                        )
                        resolved_friend = (
                            self._chat_service.get_friend_by_public_key(
                                result["public_key_hex"]
                            )
                        )
                        if resolved_friend:
                            await self._sync_chat_history(
                                resolved_friend, [(ip, port)],
                            )
                        return True
        except Exception as e:
            logger.debug(f"Nudge to {ip}:{port} for {invite} failed: {e}")
        return False

    # ------------------------------------------------------------------
    # Master contact, relay wake stream, reachability
    # ------------------------------------------------------------------

    def _read_setting(self, key: str):
        """One user_settings value (JSONB) via the chat DB connection."""
        if not self._chat_service:
            return None
        conn = self._chat_service.db_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM user_settings WHERE key = %s",
                        (key,))
            row = cur.fetchone()
            return row[0] if row else None

    def _write_settings_blocking(self, values: dict) -> None:
        """Upsert user_settings keys on a short-lived connection — safe to
        call from an executor thread (the chat connection is not)."""
        conn = psycopg2.connect(self.db_dsn)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                for key, value in values.items():
                    cur.execute("""
                        INSERT INTO user_settings (key, value, updated_at)
                        VALUES (%s, %s::jsonb, NOW())
                        ON CONFLICT (key) DO UPDATE
                            SET value = EXCLUDED.value, updated_at = NOW()
                    """, (key, json.dumps(value)))
        finally:
            conn.close()

    def _store_incoming_grant(self, result: dict, account: dict) -> None:
        """Persist the issuer-signed grant from an accepted handshake — the
        contact-recovery document (re-presented when the issuer's device,
        and friends table, changes)."""
        grant = result.get("grant")
        if not grant:
            return
        from desktop.p2p import invite_tokens
        issuer_pubkey = result.get("public_key_hex", "")
        if not invite_tokens.verify_grant(grant, issuer_pubkey):
            logger.warning("Grant from %s failed verification — discarded",
                           result.get("username", "?"))
            return
        if (grant.get("guest_pubkey", "").lower()
                != account["public_key_hex"].lower()):
            return
        friend = self._chat_service.get_friend_by_public_key(issuer_pubkey)
        if friend:
            invite_tokens.store_grant(self._chat_service.db_conn(),
                                      friend["id"], grant)
            logger.info("Friendship grant stored for %s",
                        result.get("username", "?"))

    async def _ensure_master_contact(self) -> None:
        """Seed the shipped master contact as a pending friend — the
        existing resolver loop does the rest (LAN/DHT lookup, token
        handshake, welcome pull). Removal is respected forever via the
        p2p.master_removed flag; a manual re-add clears it (backend)."""
        from desktop.p2p.master_node import (
            MASTER_INVITE_CODE, MASTER_PUBKEY_HEX, MASTER_TOKEN_ID,
            MASTER_USERNAME, master_configured,
        )
        if not master_configured() or not self._chat_service:
            return
        from desktop.node_identity import get_account_info
        account = get_account_info()
        if not account:
            return
        if account["public_key_hex"] == MASTER_PUBKEY_HEX:
            return  # the master must not add itself
        if self._read_setting("p2p.master_removed"):
            return
        conn = self._chat_service.db_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM friends WHERE public_key_hex IN (%s, %s)"
                " OR invite_code = %s",
                (MASTER_PUBKEY_HEX, f"pending:{MASTER_INVITE_CODE}",
                 MASTER_INVITE_CODE))
            if cur.fetchone():
                return
            cur.execute("""
                INSERT INTO friends (username, public_key_hex, invite_code,
                                     display_name, source, join_token_id)
                VALUES (%s, %s, %s, %s, 'master', %s)
                ON CONFLICT (public_key_hex) DO NOTHING
            """, (MASTER_USERNAME, f"pending:{MASTER_INVITE_CODE}",
                  MASTER_INVITE_CODE, MASTER_USERNAME, MASTER_TOKEN_ID))
        logger.info("Master contact seeded (%s) — resolver will connect",
                    MASTER_INVITE_CODE)

    async def _master_wake_loop(self) -> None:
        """Hold ONE outbound SSE connection to the master's relay wake
        stream. Outbound works from behind any NAT — this is how an
        unreachable node learns 'you have mail' the moment the maintainer
        replies, instead of at its next restart. Each wake (and each
        connect) triggers the existing history pull; uuid dedup makes the
        pull idempotent. Internally shaped as subscribe-to-one-relay so
        Phase D can run it per peer relay."""
        from desktop.p2p.master_node import MASTER_PUBKEY_HEX, master_configured
        if not master_configured():
            return
        import random

        from desktop.node_identity import get_account_info, sign_message
        import aiohttp
        backoff = 5.0
        while self._running:
            try:
                account = get_account_info()
                if (not account or not self._chat_service
                        or account["public_key_hex"] == MASTER_PUBKEY_HEX
                        or self._read_setting("p2p.master_removed")):
                    await asyncio.sleep(60)
                    continue
                master = self._chat_service.get_friend_by_public_key(
                    MASTER_PUBKEY_HEX)
                if not master:
                    await asyncio.sleep(30)  # not resolved yet
                    continue
                peers = await self._find_friend_peers(master)
                if not peers:
                    raise ConnectionError("no master address")
                ip, port = peers[0]
                ts = int(time.time())
                sig = sign_message(
                    f"wake_subscribe:{ts}:{MASTER_PUBKEY_HEX}:"
                    f"{account['public_key_hex']}".encode("utf-8")).hex()
                url = (f"https://{ip}:{port}/api/relay/wake-stream"
                       f"?pubkey={account['public_key_hex']}"
                       f"&ts={ts}&sig={sig}")
                # sock_read=45 → two missed 15s keepalives = dead stream
                async with aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(ssl=False),
                    timeout=aiohttp.ClientTimeout(
                        total=None, connect=10, sock_read=45),
                ) as session:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            raise ConnectionError(
                                f"wake subscribe: HTTP {resp.status}")
                        logger.info(
                            "Relay wake stream connected (%s:%s)", ip, port)
                        self._note_peer_alive(
                            master.get("id"),
                            master.get("public_key_hex"), ip, port)
                        backoff = 5.0
                        # Connect-time catch-up covers wakes missed offline
                        await self._sync_chat_history(master, [(ip, port)])
                        last_bump = time.time()
                        async for raw in resp.content:
                            if not self._running:
                                break
                            line = raw.decode("utf-8", "replace").strip()
                            if line.startswith("data:"):
                                await self._sync_chat_history(
                                    master, [(ip, port)])
                            elif time.time() - last_bump >= 120:
                                # Keepalives prove the relay is alive.
                                # Throttled to the cadence the relay uses
                                # for us — an UPDATE per 15s keepalive
                                # would NOTIFY the UI into a refetch loop.
                                last_bump = time.time()
                                self._chat_service.update_friend_last_seen(
                                    master["public_key_hex"])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"Master wake stream: {e}")
            if not self._running:
                break
            await asyncio.sleep(backoff + random.uniform(0, backoff / 4))
            backoff = min(backoff * 2, 300.0)

    def _note_inbound_reachable(self) -> None:
        """Passive proof from the sync server: a non-LAN peer just reached
        us — definitive 'reachable', overrides every heuristic."""
        if self._reachability_status == "reachable":
            return
        self._reachability_status = "reachable"
        asyncio.ensure_future(self._apply_reachability(
            "reachable", "inbound request observed"))

    def _reachability_heuristic(self):
        """Local CGNAT tells, no network calls: a private/RFC 6598 router
        WAN address, or a router-WAN vs DHT-observed mismatch (another NAT
        above the router). Returns (status, detail) or None."""
        import ipaddress
        cgnat_net = ipaddress.ip_network("100.64.0.0/10")
        wan = self._upnp.external_ip if self._upnp else None
        if wan:
            try:
                addr = ipaddress.ip_address(wan)
                if addr.is_private or addr in cgnat_net:
                    return ("cgnat",
                            f"router WAN {wan} is private/carrier-grade")
            except ValueError:
                wan = None
        observed = (self._dht_service.observed_external_ip
                    if self._dht_service else None)
        if wan and observed and wan != observed:
            return ("cgnat", f"router WAN {wan} != observed {observed}")
        return None

    async def _apply_reachability(self, status: str, detail: str) -> None:
        self._reachability_status = status
        if self._dht_service:
            self._dht_service.set_announces_enabled(
                status not in ("cgnat", "unreachable"))
        await asyncio.get_event_loop().run_in_executor(
            None, self._write_settings_blocking, {
                "p2p.reachability": status,
                "p2p.reachability_detail": detail,
                "p2p.reachability_checked_at":
                    datetime.now(timezone.utc).isoformat(),
            })
        logger.info(f"Reachability: {status} ({detail})")

    async def _probe_reachability_via_master(self):
        """Definitive test — ask the master to connect back to our sync
        port (BT-tracker style). None when the master is not resolved or
        not reachable itself."""
        from desktop.p2p.master_node import MASTER_PUBKEY_HEX, master_configured
        if not master_configured() or not self._chat_service:
            return None
        from desktop.node_identity import get_account_info, sign_message
        account = get_account_info()
        if not account or account["public_key_hex"] == MASTER_PUBKEY_HEX:
            return None
        master = self._chat_service.get_friend_by_public_key(
            MASTER_PUBKEY_HEX)
        if not master:
            return None
        peers = await self._find_friend_peers(master)
        if not peers:
            return None
        port = (self._upnp.get_external_port(self._http_port)
                if self._upnp else None) or self._http_port
        ts = int(time.time())
        sig = sign_message(
            f"probe_request:{ts}:{MASTER_PUBKEY_HEX}:{port}"
            .encode("utf-8")).hex()
        payload = {
            "public_key_hex": account["public_key_hex"],
            "port": port, "ts": ts, "signature": sig,
        }
        import aiohttp
        for ip, mport in peers:
            try:
                async with aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(ssl=False),
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as session:
                    async with session.post(
                            f"https://{ip}:{mport}/api/relay/probe-connect",
                            json=payload) as resp:
                        if resp.status == 429:
                            return None  # cooldown — try next cycle
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                        self._note_peer_alive(master.get("id"),
                                              MASTER_PUBKEY_HEX, ip, mport)
                        return bool(data.get("reachable"))
            except Exception as e:
                logger.debug(f"Probe via {ip}:{mport} failed: {e}")
        return None

    async def _reachability_loop(self) -> None:
        """Adaptive-cadence self-check: retry every RETRY_UNKNOWN while we
        still have no verdict — at startup the master contact is usually
        mid-resolution, so the first probe has no target — then settle into
        the infrastructure cadence (6 h, like REANNOUNCE). Passive inbound
        proof overrides in between via _note_inbound_reachable."""
        RETRY_UNKNOWN = 60
        await asyncio.sleep(20)  # let UPnP/DHT settle after startup
        while self._running:
            status = "unknown"
            try:
                probe = await self._probe_reachability_via_master()
                heuristic = self._reachability_heuristic()
                if probe is True:
                    status, detail = "reachable", "master probe connected"
                elif probe is False:
                    status, detail = ((heuristic[0], heuristic[1])
                                      if heuristic else
                                      ("unreachable", "master probe failed"))
                elif heuristic:
                    status, detail = heuristic
                elif self._reachability_status == "reachable":
                    status, detail = ("reachable",
                                      "inbound request observed")
                else:
                    status, detail = "unknown", "no probe target yet"
                await self._apply_reachability(status, detail)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"Reachability check error: {e}")
            await asyncio.sleep(RETRY_UNKNOWN if status == "unknown"
                                else 6 * 3600)

    async def _poll_pending_accepts(self):
        """One-time startup check for pending accepts from the Worker.

        Picks up accepts that arrived while we were offline.
        Only runs if we have outstanding email invites (marker file).
        After startup, accepts are handled via the nudge mechanism
        (peer sends /api/chat/invite-accepted when both are online).
        """
        await asyncio.sleep(10)  # initial delay for network setup
        try:
            loop = asyncio.get_event_loop()

            has_pending = await loop.run_in_executor(
                None, self._has_pending_invites
            )
            if not has_pending:
                return

            accepts = await loop.run_in_executor(
                None, self._fetch_pending_accepts_sync
            )
            for accept in accepts:
                inv = accept.get("invite_code", "")
                if not inv or not self._chat_service:
                    continue
                # Skip if already exists (may have been added via nudge)
                existing = self._chat_service.get_friends()
                if any(f["invite_code"] == inv for f in existing):
                    continue
                username = inv.split("#")[0]
                self._chat_service.add_friend(
                    public_key_hex=f"pending:{inv}",
                    invite_code=inv,
                    username=username,
                    display_name=username,
                )
                logger.info(f"Startup: auto-added friend from pending accept: {inv}")
            if accepts and self._sync_server:
                await self._sync_server._wake_backend_sse()
        except Exception as e:
            logger.debug(f"Startup pending accepts check error: {e}")

    def _has_pending_invites(self) -> bool:
        """Check DB for unreciprocated sent invites."""
        try:
            import psycopg2
            conn = psycopg2.connect(self.db_dsn)
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM sent_invites "
                        "WHERE sent_at > NOW() - INTERVAL '30 days' "
                        "LIMIT 1"
                    )
                    return cur.fetchone() is not None
            finally:
                conn.close()
        except Exception:
            return False

    @staticmethod
    def _fetch_pending_accepts_sync() -> list[dict]:
        """Blocking: fetch pending accepts from Worker."""
        from desktop.p2p.email_verify import check_pending_accepts
        return check_pending_accepts()

    async def _deliver_pending_fast(self):
        """Fast path: push pending messages first, pull history in background.

        Called directly from sync_server trigger-send endpoint.
        Always pushes immediately. Schedules a one-time history pull per
        friend per session (for identity restoration / multi-device sync).
        """
        if not self._chat_service:
            return
        pending = self._chat_service.get_pending_messages()
        if not pending:
            return

        seen_friends: set[int] = set()
        for msg in pending:
            fid = msg["friend_id"]
            if fid in seen_friends:
                continue
            seen_friends.add(fid)

            pubkey = msg["public_key_hex"]
            if pubkey.startswith("pending:"):
                continue

            friend_info = {
                "id": fid,
                "invite_code": msg["invite_code"],
                "public_key_hex": pubkey,
                "username": "",
            }
            peers = await self._find_friend_peers(friend_info)
            if not peers:
                continue

            # Push first (fast) — don't block on history pull
            friend_pending = [m for m in pending if m["friend_id"] == fid]
            await self._push_pending_messages(
                fid, pubkey, friend_pending, peers
            )

            # Schedule background history pull once per session
            if fid not in self._history_synced_friends:
                self._history_synced_friends.add(fid)
                asyncio.ensure_future(
                    self._sync_chat_history(friend_info, peers)
                )

    async def _push_pending_messages(
        self,
        friend_id: int,
        pubkey: str,
        messages: list[dict],
        peers: list[tuple],
    ):
        """Push undelivered messages to a friend's peer.

        Stops immediately on 403 (peer doesn't have us as friend —
        they will pull history when they add us).
        """
        import aiohttp
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=False),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as session:
            for msg in messages:
                try:
                    encrypted = self._chat_service.encrypt_message(
                        msg["content"], pubkey
                    )
                except Exception as e:
                    logger.error(
                        f"Encrypt failed for friend {friend_id}: {e}"
                    )
                    break

                ts = msg["timestamp"]
                ts_str = (
                    ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
                )
                if "+" not in ts_str and not ts_str.endswith("Z"):
                    ts_str += "+00:00"

                payload = {
                    "from_public_key": self._chat_service.public_key_hex,
                    "encrypted": encrypted,
                    "timestamp": ts_str,
                    "message_uuid": msg.get("message_uuid", ""),
                }

                delivered = False
                for ip, port in peers:
                    url = f"https://{ip}:{port}/api/chat/message"
                    try:
                        async with session.post(url, json=payload) as resp:
                            if resp.status == 200:
                                self._chat_service.mark_delivered(
                                    msg["id"]
                                )
                                self._note_peer_alive(
                                    friend_id, pubkey, ip, port)
                                delivered = True
                                break
                            if resp.status == 403:
                                # This address is not our friend (a
                                # stranger on the LAN, or they haven't
                                # added us yet) — try the next candidate.
                                # Aborting here used to let any bystander
                                # answering first block delivery entirely.
                                logger.debug(
                                    f"Push rejected (403) by {ip}:{port}")
                    except Exception:
                        pass
                if not delivered:
                    # Every address refused or timed out. Later messages
                    # would fare no better; the row stays undelivered and
                    # the 60s retry loop picks it up.
                    logger.debug(
                        f"No route to friend {friend_id} "
                        f"({len(peers)} candidates tried)")
                    return

    async def _listen_for_db_notifications(self):
        """Listen for PostgreSQL NOTIFY on 'sautium_chat' channel.

        Wakes up ``_sync_chat_histories`` instantly when the backend
        inserts a new outgoing message (NOTIFY sautium_chat).
        """
        while self._running:
            conn = None
            try:
                conn = psycopg2.connect(self.db_dsn)
                conn.set_isolation_level(
                    psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT
                )
                with conn.cursor() as cur:
                    cur.execute("LISTEN sautium_chat")

                while self._running:
                    # Block up to 5 s waiting for data on the socket
                    ready = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: select.select([conn], [], [], 5),
                    )
                    if ready[0]:
                        conn.poll()
                        while conn.notifies:
                            conn.notifies.pop(0)
                        if self._chat_notify:
                            self._chat_notify.set()
            except Exception as e:
                logger.debug(f"DB LISTEN error: {e}")
                await asyncio.sleep(5)
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

    async def _sync_chat_histories(self):
        """Sync chat histories with friends who have pending messages.

        Wakes up immediately on ``_chat_notify`` event (triggered by
        PostgreSQL NOTIFY or ``notify_new_message()``) or falls back
        to a 60-second polling interval.

        Hybrid approach (pull + push):
          1. Pull history from friend's peer (handles identity
             restoration and catching up)
          2. Push remaining undelivered messages (handles normal
             offline delivery)
          3. Stop pushing on 403 (peer hasn't added us — they'll
             pull when ready)
        """
        while self._running:
            try:
                await asyncio.wait_for(
                    self._chat_notify.wait(), timeout=60
                )
                self._chat_notify.clear()
            except asyncio.TimeoutError:
                pass  # 60 s elapsed — do periodic check
            if not self._running or not self._chat_service:
                break

            pending = self._chat_service.get_pending_messages()
            if not pending:
                continue

            # Collect unique friends with undelivered messages
            seen_friends: set[int] = set()
            for msg in pending:
                fid = msg["friend_id"]
                if fid in seen_friends:
                    continue
                seen_friends.add(fid)

                pubkey = msg["public_key_hex"]
                if pubkey.startswith("pending:"):
                    continue

                friend_info = {
                    "id": fid,
                    "invite_code": msg["invite_code"],
                    "public_key_hex": pubkey,
                    "username": "",
                }
                peers = await self._find_friend_peers(friend_info)
                if not peers:
                    continue

                # Step 1: Push undelivered messages first (fast)
                still_pending = [
                    m for m in self._chat_service.get_pending_messages()
                    if m["friend_id"] == fid
                ]
                if still_pending:
                    await self._push_pending_messages(
                        fid, pubkey, still_pending, peers
                    )

                # Step 2: Pull history once per session (background)
                if fid not in self._history_synced_friends:
                    self._history_synced_friends.add(fid)
                    await self._sync_chat_history(friend_info, peers)
                    # If messages still undelivered — refresh peer
                    # cache so next cycle does a fresh LAN/DHT lookup
                    leftovers = [
                        m for m in self._chat_service.get_pending_messages()
                        if m["friend_id"] == fid
                    ]
                    if leftovers:
                        await self._find_friend_peers(
                            friend_info, refresh=True
                        )

    # -------------------------------------------------------------------
    # Sync triggers: Web UI Force sync + Auto-sync timer
    # -------------------------------------------------------------------

    async def _sync_request_listener_thread(self):
        """LISTEN on sautium_sync_request, wake _sync_request_loop.

        Mirrors _listen_for_db_notifications (chat) — same select-on-
        socket pattern with a 5s timeout so cancellation is responsive.
        """
        while self._running:
            conn = None
            try:
                conn = psycopg2.connect(self.db_dsn)
                conn.set_isolation_level(
                    psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT
                )
                with conn.cursor() as cur:
                    cur.execute("LISTEN sautium_sync_request")

                while self._running:
                    ready = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: select.select([conn], [], [], 5),
                    )
                    if ready[0]:
                        conn.poll()
                        while conn.notifies:
                            conn.notifies.pop(0)
                        if self._sync_request_notify:
                            self._sync_request_notify.set()
            except Exception as e:
                logger.debug(f"sync_request LISTEN error: {e}")
                await asyncio.sleep(5)
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

    async def _sync_request_loop(self):
        """Dispatcher: on notify, run sync_from_peers via _run_sync_with_status."""
        while self._running:
            try:
                await self._sync_request_notify.wait()
                self._sync_request_notify.clear()
            except asyncio.CancelledError:
                break
            if not self._running:
                break
            await self._run_sync_with_status(trigger="manual")

    async def _auto_sync_loop(self):
        """Periodic sync based on sync.auto_interval_min.

        Re-reads the setting each cycle so config changes apply at the
        next interval without restart. Initial delay of 60s lets the
        DHT/LAN discovery warm up before the first auto-sync.
        """
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            return

        while self._running:
            interval_min = self._read_auto_sync_interval()
            if interval_min and interval_min > 0:
                await self._run_sync_with_status(trigger="auto")
                sleep_for = interval_min * 60
            else:
                sleep_for = 60  # re-check setting every minute when disabled

            try:
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                break

    # Mirrors _DEFAULTS["sync.auto_interval_min"] in
    # backend/routers/settings.py — keep in sync so the UI's displayed
    # default and the launcher's actual cadence match on a fresh
    # install (no user_settings row yet).
    _AUTO_SYNC_INTERVAL_DEFAULT_MIN = 30

    def _read_auto_sync_interval(self) -> Optional[int]:
        """Read sync.auto_interval_min from user_settings (None = disabled)."""
        try:
            conn = psycopg2.connect(self.db_dsn)
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT value FROM user_settings WHERE key = %s",
                        ("sync.auto_interval_min",),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return self._AUTO_SYNC_INTERVAL_DEFAULT_MIN
                    if row[0] is None:
                        return None  # explicitly disabled
                    return int(row[0]) if row[0] else None
            finally:
                conn.close()
        except Exception as e:
            logger.debug(f"Failed to read sync.auto_interval_min: {e}")
        return None

    async def _run_sync_with_status(self, trigger: str):
        """Run sync_from_peers, persist results to user_settings, NOTIFY UI.

        Serialised via self._sync_lock so manual + auto triggers can't
        run concurrently. Writes sync.last_at and sync.last_items_received
        on completion (success or failure), then NOTIFY sautium_sync_done
        wakes the backend SSE bridge.
        """
        if self._sync_lock.locked():
            logger.debug(
                f"P2P sync already in progress, skipping {trigger} trigger"
            )
            return

        async with self._sync_lock:
            started = datetime.now(timezone.utc)
            logger.info(f"P2P sync starting (trigger={trigger})")
            try:
                stats = await self._async_sync_from_peers(progress_cb=None)
            except Exception as e:
                logger.error(f"P2P sync failed: {e}", exc_info=True)
                stats = {"error": str(e)}

            items = sum(v for v in stats.values() if isinstance(v, int))
            await asyncio.get_event_loop().run_in_executor(
                None, self._write_sync_status, started, items
            )
            logger.info(
                f"P2P sync complete (trigger={trigger}): "
                f"{items} items, stats={stats}"
            )

        # A sync may have imported new artists/phantoms whose canon is now
        # blocked on missing MB facts — fetch their slices right away instead
        # of waiting for the periodic loop (fire-and-forget; merges with a
        # concurrent run via _mb_slice_lock).
        asyncio.create_task(self._request_mb_slices_safe())

    async def _request_mb_slices_safe(self):
        try:
            await self._request_mb_slices()
        except Exception:
            logger.exception("post-sync MB slice fetch failed")

    # -------------------------------------------------------------------
    # MB dump slices (P2P canonicalization for dump-less nodes)
    # -------------------------------------------------------------------

    # Fallback default when user_settings has no mb_slice.auto_interval_min
    # row and the config block is absent.
    _MB_SLICE_INTERVAL_DEFAULT_MIN = 360

    async def _mb_slice_loop(self):
        """Periodic MB slice fetch for dump-less nodes. The post-sync trigger
        covers the common case (new content arrives via sync/scan); this loop
        is the fallback cadence and the retry path after peer failures."""
        try:
            await asyncio.sleep(120)
        except asyncio.CancelledError:
            return

        while self._running:
            interval_min = self._read_mb_slice_interval()
            if interval_min and interval_min > 0:
                try:
                    await self._request_mb_slices()
                except Exception:
                    logger.exception("MB slice cycle failed")
                sleep_for = interval_min * 60
            else:
                sleep_for = 300  # disabled — re-check the setting later
            try:
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                break

    def _read_mb_slice_interval(self) -> Optional[int]:
        """mb_slice.auto_interval_min from user_settings (None = disabled),
        falling back to the config block on a fresh install."""
        default = self.config.get("mb_slice", {}).get(
            "auto_interval_min", self._MB_SLICE_INTERVAL_DEFAULT_MIN)
        try:
            conn = psycopg2.connect(self.db_dsn)
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT value FROM user_settings WHERE key = %s",
                        ("mb_slice.auto_interval_min",),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return default
                    if row[0] is None:
                        return None  # explicitly disabled
                    return int(row[0]) if row[0] else None
            finally:
                conn.close()
        except Exception as e:
            logger.debug(f"Failed to read mb_slice.auto_interval_min: {e}")
        return default

    def request_mb_slices(self) -> bool:
        """Manual trigger from the launcher/UI thread (thread-safe)."""
        if not self._running or not self._loop:
            return False
        asyncio.run_coroutine_threadsafe(
            self._request_mb_slices(), self._loop)
        return True

    def _pending_slice_names_sync(self) -> list[str]:
        conn = psycopg2.connect(self.db_dsn)
        try:
            return mb_slice_queries.pending_slice_names(conn, limit=200)
        finally:
            conn.close()

    def _local_dump_available_sync(self):
        """Full dump in THIS node's DB (VERSION marker + mb_artist rows on
        our DSN). The file alone is not enough: on a dev host the Docker
        loader stamps VERSION through the repo bind-mount while the
        launcher's embedded PG has empty mb_* — fetch must not be fooled."""
        conn = psycopg2.connect(self.db_dsn)
        try:
            return mb_slice_queries.local_dump_available(conn)
        finally:
            conn.close()

    def _local_backend_api(self) -> Optional[BackendAPIClient]:
        port = self.config.get("ports", {}).get("web", 0)
        if not port:
            return None
        return BackendAPIClient(f"https://127.0.0.1:{port}")

    def _load_p2p_bans(self) -> tuple[set, set]:
        """Local ban list: (pubkeys, addr uuids). The pubkey ban is the
        anchor (proven by slice receipts); the address id catches a banned
        key returning under a fresh identity from the same place."""
        try:
            conn = psycopg2.connect(self.db_dsn)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pubkey, addr::text FROM p2p_node_bans")
                    rows = cur.fetchall()
                return ({r[0] for r in rows if r[0]},
                        {r[1] for r in rows if r[1]})
            finally:
                conn.close()
        except Exception as e:
            logger.debug(f"Failed to load p2p bans: {e}")
            return set(), set()

    async def _find_dump_peers(self) -> list[tuple[BackendAPIClient, str]]:
        """Reachable peers advertising the MB dump: manual peers and LAN
        first (cheap, likely friends), then a DHT capability lookup.
        Locally-banned nodes are skipped before connecting (by address) and
        after health (by pubkey)."""
        loop = asyncio.get_event_loop()
        banned_keys, banned_addrs = await loop.run_in_executor(
            None, self._load_p2p_bans)
        found: list[tuple[BackendAPIClient, str]] = []
        seen_addrs: set[str] = set()

        candidates: list[str] = list(
            self.config.get("p2p", {}).get("manual_peers", []))
        if self._lan_discovery:
            for ip, port in self._lan_discovery.peers:
                info = self._lan_discovery.get_peer_info(ip, port) or {}
                scheme = info.get("scheme", "https")
                candidates.append(f"{scheme}://{ip}:{port}")
        if self._dht_service:
            for ip, port in await self._dht_service.lookup_capability("mbdump"):
                candidates.append(f"{ip}:{port}")

        for addr in candidates:
            if addr in seen_addrs:
                continue
            seen_addrs.add(addr)
            if mb_slice_queries.addr_uuid(addr) in banned_addrs:
                logger.info(f"MB slice: skipping banned address {addr}")
                continue
            api = await self._try_connect_peer(addr)
            if not api:
                continue
            health = await loop.run_in_executor(None, api.get_health)
            if not health or not health.get("mb_dump"):
                continue
            node_id = health.get("node_id", "")
            if node_id and node_id in banned_keys:
                logger.info(f"MB slice: skipping banned node "
                            f"{node_id[:16]}… at {addr}")
                continue
            found.append((api, node_id))
        return found

    async def _request_mb_slices(self) -> dict:
        """Fetch MB slices for every canon-pending artist name and hand the
        imported facts to the backend canon. Serialised via _mb_slice_lock so
        the periodic loop, post-sync trigger and manual runs merge."""
        cfg = self.config.get("mb_slice", {})
        if not cfg.get("fetch", True):
            logger.debug("MB slice: fetch disabled in config")
            return {}
        loop = asyncio.get_event_loop()
        if await loop.run_in_executor(None, self._local_dump_available_sync):
            logger.debug("MB slice: full local dump — nothing to fetch")
            return {}
        if self._mb_slice_lock.locked():
            return {}

        async with self._mb_slice_lock:
            names = await loop.run_in_executor(
                None, self._pending_slice_names_sync)
            if not names:
                logger.info("MB slice: no pending names — all canon inputs "
                            "already fetched")
                return {}

            peers = await self._find_dump_peers()
            if not peers:
                logger.info("MB slice: no dump-holding peers reachable")
                return {}

            batch_size = max(1, min(int(cfg.get("batch_size", 20)),
                                    mb_slice_queries.MAX_NAMES_PER_REQUEST))
            batches = [names[i:i + batch_size]
                       for i in range(0, len(names), batch_size)]
            logger.info(f"MB slice: {len(names)} pending names, "
                        f"{len(batches)} batches, {len(peers)} dump peers")

            backend_api = self._local_backend_api()
            peer_iter = iter(peers)
            api, node = next(peer_iter)
            client = MBSliceClient(api, db_dsn=self.db_dsn, source_node=node,
                                   backend_api=backend_api)
            total = {"names": 0, "matched": 0, "rows_inserted": 0}
            imported_any = False
            i = 0
            while i < len(batches):
                stats = await loop.run_in_executor(None, client.run, batches[i])
                if "error" in stats:
                    # Provenance is written per successful batch, so retrying
                    # this batch against the next peer is idempotent.
                    nxt = next(peer_iter, None)
                    if nxt is None:
                        logger.warning("MB slice: all dump peers failed — "
                                       "remaining names retry next cycle")
                        break
                    client.close()
                    api, node = nxt
                    client = MBSliceClient(api, db_dsn=self.db_dsn,
                                           source_node=node,
                                           backend_api=backend_api)
                    continue
                imported_any = True
                for k in total:
                    total[k] += stats.get(k, 0)
                i += 1

            if imported_any:
                # ANALYZE + backend POST /canonicalize — once per run
                await loop.run_in_executor(None, client.finalize)
                logger.info(f"MB slice run done: {total}")
            else:
                client.close()
            return total

    def _write_sync_status(self, started: datetime, items: int) -> None:
        """Persist sync.last_at + items_received, fire sautium_sync_done."""
        try:
            conn = psycopg2.connect(self.db_dsn)
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO user_settings (key, value)
                        VALUES (%s, %s::jsonb)
                        ON CONFLICT (key) DO UPDATE
                            SET value = EXCLUDED.value,
                                updated_at = CURRENT_TIMESTAMP
                        """,
                        ("sync.last_at", json.dumps(started.isoformat())),
                    )
                    cur.execute(
                        """
                        INSERT INTO user_settings (key, value)
                        VALUES (%s, %s::jsonb)
                        ON CONFLICT (key) DO UPDATE
                            SET value = EXCLUDED.value,
                                updated_at = CURRENT_TIMESTAMP
                        """,
                        ("sync.last_items_received", json.dumps(int(items))),
                    )
                    cur.execute("NOTIFY sautium_sync_done")
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"Failed to write sync status: {e}")

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
