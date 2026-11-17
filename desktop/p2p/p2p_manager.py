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
import threading
import time
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Callable, Optional

import psycopg2
import psycopg2.extensions

from desktop.api_client import BackendAPIClient
from desktop.p2p import diag_protocol, lb_slice_cycle, mb_slice_queries, sync_walk
from desktop.p2p.addrs import fmt_addr
from desktop.p2p.peer_auth import pinned_ssl_context
from desktop.p2p.chat_client import (ChatClient, ClientIdentity, master_runs_none,
                                     replied_friends, upnp_heuristic)
from desktop.p2p.chat_service import ChatService
from desktop.p2p.dht_service import DHTService
from desktop.p2p.dht_state import DhtStateStore
from desktop.p2p.lan_discovery import LANDiscovery
from desktop.p2p.lb_slice_cycle import LbSliceCycle
from desktop.p2p.mb_slice_cycle import MbSliceCycle
from desktop.p2p.sync_walk import SyncWalk
from desktop.p2p.sync_server import SyncServer
from desktop.p2p.upnp_service import UPnPService

logger = logging.getLogger(__name__)


class P2PManager:
    """Orchestrates P2P sync server + DHT + LAN discovery + UPnP."""

    def __init__(self, db_dsn: str, config: dict):
        self.db_dsn = db_dsn
        self.config = config
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._proof_thread: Optional[threading.Thread] = None
        self._proof_stop: Optional[threading.Event] = None
        self._load_meter = None
        self._sync_server: Optional[SyncServer] = None
        self._dht_service: Optional[DHTService] = None
        self._lan_discovery: Optional[LANDiscovery] = None
        self._upnp: Optional[UPnPService] = None
        self._chat_service: Optional[ChatService] = None   # the sync server's
        # The client half of chat (desktop/p2p/chat_client.py, shared with the
        # Docker backend) — built when the account exists.
        self._chat_client: Optional[ChatClient] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._reannounce_task: Optional[asyncio.Task] = None
        self._dht_online_task: Optional[asyncio.Task] = None
        self._upnp_renewal_task: Optional[asyncio.Task] = None
        self._pending_accepts_task: Optional[asyncio.Task] = None
        # One pickup at a time (_pick_up); created on the loop at start,
        # before the sync server can take a nudge. `_pickup_waiting`: the
        # pickup waiting for its turn, which a later request joins.
        self._pickup_lock: Optional[asyncio.Lock] = None
        self._pickup_waiting: Optional[asyncio.Task] = None
        self._lan_discovery_task: Optional[asyncio.Task] = None
        self._db_listen_task: Optional[asyncio.Task] = None
        self._sync_request_task: Optional[asyncio.Task] = None
        self._sync_request_listen_task: Optional[asyncio.Task] = None
        self._auto_sync_task: Optional[asyncio.Task] = None
        self._walk: Optional[SyncWalk] = None   # the pull side (desktop/p2p/sync_walk.py)
        self._mb_cycle_tasks: list = []
        self._lb_cycle_tasks: list = []
        self._mb_dump_version: Optional[str] = None
        self._running = False
        self._on_message_cb: Optional[Callable] = None
        self._on_identity_rotated_cb: Optional[Callable] = None
        self._diag_task: Optional[asyncio.Task] = None
        self._relay_cap_task: Optional[asyncio.Task] = None

    @property
    def is_running(self) -> bool:
        return self._running

    def set_on_message_callback(self, cb: Callable):
        """Set callback for incoming chat messages (called from P2P thread)."""
        self._on_message_cb = cb

    def set_identity_rotated_callback(self, cb: Callable):
        """Set the callback for a key rotation done by the backend (Profile →
        name / password); called from the P2P thread. Everything this
        manager holds is built on the old key, so the launcher answers by
        bringing it down and up again."""
        self._on_identity_rotated_cb = cb

    @property
    def _reachability_status(self) -> str:
        """The verdict the chat client holds (unknown without an account)."""
        return self._chat_client.reachability if self._chat_client else "unknown"

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

        # Identity certificate: silent fetch when missing (idempotent issuance —
        # a re-fetch after moving devices returns the original birth date;
        # network failures degrade to None and are logged inside). A pow
        # certificate then needs its proof: minutes of memory-hard work in a
        # below-normal-priority thread, gated on free memory / mains power,
        # published to user_settings for the Web UI (desktop/p2p/identity_proof.py).
        # Load meter: this process tree's CPU against the profile ceiling —
        # headroom/dormancy for the miner (hold), the DHT announce sweep
        # (pace) and later the gate; published to user_settings['p2p.load'].
        from desktop.p2p import load_meter
        self._load_meter = load_meter.install(load_meter.LoadMeter())

        def _publish_load(snap: dict) -> None:
            try:
                from desktop.p2p import pricing
                values = {"p2p.load": snap}
                if pricing.current() is not None:
                    values["p2p.gate"] = pricing.current().snapshot()
                self._write_settings_blocking(values)
                self._notify_blocking("sautium_identity")
            except Exception as e:
                logger.debug("load publish failed: %s", e)
        self._load_meter.subscribe(_publish_load)
        self._load_meter.start()

        if account_info:
            from desktop.p2p.birth_cert import ensure_certificate
            cert = ensure_certificate()
            if cert is not None:
                self._start_identity_proof_worker(cert)

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
            sharing_enabled=self._sharing_on,
            state_store=DhtStateStore(self.db_dsn),
            listen_port=dht_port,
            http_port=http_port,
        )
        if self._load_meter is not None:
            self._dht_service.set_pace_provider(self._load_meter.announce_pace)
        docker_ports = p2p_cfg.get("docker_ports", [])
        invite_code = account_info.get("invite_code", "") if account_info else ""
        self._lan_discovery = LANDiscovery(
            sync_port=http_port,
            node_id=node_id,
            invite_code=invite_code,
            localhost_probe_ports=docker_ports,
            on_new_peer=self._on_lan_peer,
        )
        # UPnP: map ONLY the P2P sync port. Never include backend / docker
        # ports here — the backend has no app-level auth (see CLAUDE.md
        # "Security Posture"). docker_ports above is for LAN-discovery
        # probing of localhost only.
        self._upnp = UPnPService(ports=[http_port])
        # The pull side, shared with the Docker backend: this runtime's
        # services go in as dependencies (desktop/p2p/sync_walk.py).
        self._walk = SyncWalk(
            self.db_dsn,
            identity=self._peer_identity,
            sharing_enabled=self._sharing_on,
            dht=self._dht_service,
            lan=self._lan_discovery,
            manual_peers=p2p_cfg.get("manual_peers", []),
            skip_addrs=self._dht_skip_addrs,
            diag_record=self._diag_record,
            after_run=self._after_walk,
            on_enriched_count=self._lan_discovery.update_enriched_count,
        )
        # The ListenBrainz slice cycle — the same shape, its own family
        # (desktop/p2p/lb_slice_cycle.py, shared with the Docker backend).
        self._lb_cycle = LbSliceCycle(
            self.db_dsn,
            connect=self._walk.connect_peer,
            dead=self._walk.dead,
            sharing_enabled=self._sharing_on,
            config=self.config.get("lb_slice", {}),
            dht=self._dht_service,
            lan=self._lan_discovery,
            manual_peers=p2p_cfg.get("manual_peers", []),
            load_bans_fn=self._load_p2p_bans,
            diag_record=self._diag_record,
            first_source=lambda: self._walk.first_source,
        )
        # The MusicBrainz slice cycle — shared with the Docker backend since
        # 2026-09-24 (desktop/p2p/mb_slice_cycle.py); the facts it imports go
        # to the backend canon over its API.
        self._mb_cycle = MbSliceCycle(
            self.db_dsn,
            connect=self._walk.connect_peer,
            dead=self._walk.dead,
            after_import=self._canonicalize_locally,
            sharing_enabled=self._sharing_on,
            config=self.config.get("mb_slice", {}),
            dht=self._dht_service,
            lan=self._lan_discovery,
            manual_peers=p2p_cfg.get("manual_peers", []),
            load_bans_fn=self._load_p2p_bans,
            diag_record=self._diag_record,
            first_source=lambda: self._walk.first_source,
        )

        # Initialize chat service if account exists
        if account_info:
            try:
                from desktop.node_identity import (
                    get_private_key_raw, load_previous_seeds,
                )
                def chat_service() -> ChatService:
                    return ChatService(
                        db_dsn=self.db_dsn,
                        private_key_raw=get_private_key_raw(),
                        public_key_hex=account_info["public_key_hex"],
                        previous_seeds=tuple(load_previous_seeds()),
                    )
                self._chat_service = chat_service()
                self._sync_server.set_chat_service(
                    self._chat_service, self._on_message_cb
                )
                self._sync_server.set_pickup(self._pick_up)
                # The client works through a store of its own: its calls run
                # on the client's store thread, the sync server's on this loop.
                # The master runs no client half (chat_client.master_runs_none).
                if not master_runs_none(account_info["public_key_hex"]):
                    self._chat_client = ChatClient(
                        chat_service(),
                        identity=self._client_identity,
                        announce_port=self._announce_port,
                        dht=self._dht_service,
                        lan=self._lan_discovery,
                        heuristic=lambda: upnp_heuristic(self._upnp, self._dht_service),
                        on_reachability=self._apply_reachability,
                        on_incoming=self._chat_incoming,
                        on_warrant=lambda warrant, addr: self._diag_queue.put_nowait(
                            ("warrant", warrant, addr)),
                        on_master_reachable=self._master_reachable,
                    )
                    self._sync_server.set_inbound_cb(self._chat_client.note_inbound_reachable)
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
        self._sharing_lock = asyncio.Lock()
        self._sharing_read: Optional[bool] = None   # the switch as _sharing_changed last read it
        self._pickup_lock = asyncio.Lock()
        self._walk.bind()
        # Support diagnostics: one sequential worker fed by the master's
        # wake stream (warrants), its reconnects (resume) and LISTEN
        # sautium_diag (reports) — see _diag_worker.
        self._diag_queue: asyncio.Queue = asyncio.Queue()
        self._master_addr = None
        self._diag_sent: list = []

        def _progress(msg):
            logger.info(msg)
            if progress_cb:
                progress_cb(msg)

        try:
            # Start HTTP sync server
            _progress("Starting sync server...")
            await self._sync_server.start()

            # Start LAN discovery (always — works behind any NAT); it beacons
            # only while P2P sharing is on (_sharing_changed follows the switch)
            _progress("Starting LAN discovery...")
            await self._sharing_changed()
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
            # The probe/passive signals refine this in the chat client's
            # reachability loop.
            heuristic = upnp_heuristic(self._upnp, self._dht_service)
            if heuristic:
                self._dht_service.set_announces_enabled(False)
                _progress(f"Reachability: {heuristic[0]} — "
                          "DHT announces suppressed")

            # Start DHT — returns at once. The routing table warms up in
            # the background and _dht_online announces once it is usable,
            # so chat, friends and the sync loops start without waiting
            # out a cold bootstrap.
            if self._dht_service.is_available:
                _progress("Starting DHT...")
                await self._dht_service.start()
                self._dht_online_task = asyncio.create_task(
                    self._dht_online(_progress)
                )
            else:
                _progress(
                    "P2P online (LAN only, DHT disabled — "
                    "install libtorrent for internet discovery)"
                )

            # Start chat history sync + friend resolution
            # (works via LAN even without DHT)
            if self._chat_service:
                if self._chat_client:
                    await self._chat_client.start()
                self._db_listen_task = asyncio.create_task(
                    self._listen_for_db_notifications()
                )
                self._pending_accepts_task = asyncio.create_task(
                    self._poll_pending_accepts()
                )
                self._diag_task = asyncio.create_task(self._diag_worker())
                self._diag_queue.put_nowait(("report",))
            self._relay_cap_task = asyncio.create_task(self._relay_cap_loop())
            self._directory_register_task = asyncio.create_task(
                self._directory_register_loop()
            )

            # Sync triggers: NOTIFY sautium_sync_request (the backend
            # after a scan that added files), a LAN peer appearing
            # (_on_lan_peer) and the auto-sync timer (sync.auto_interval_min).
            # All serialise inside the walk, so concurrent triggers merge
            # to one run (desktop/p2p/sync_walk.py).
            self._sync_request_listen_task = asyncio.create_task(
                self._sync_notification_listener()
            )
            self._sync_request_task = asyncio.create_task(
                self._walk.dispatch_loop()
            )
            self._auto_sync_task = asyncio.create_task(
                self._walk.interval_loop()
            )
            self._mb_cycle.bind()
            self._mb_cycle_tasks = [
                asyncio.create_task(self._mb_cycle.dispatch_loop()),
                asyncio.create_task(self._mb_cycle.interval_loop()),
            ]
            self._lb_cycle.bind()
            self._lb_cycle_tasks = [
                asyncio.create_task(self._lb_cycle.dispatch_loop()),
                asyncio.create_task(self._lb_cycle.interval_loop()),
            ]

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
                        f"Sync server not responding ({needs_restart}), "
                        "restarting..."
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

    async def _dht_online(self, _progress) -> None:
        """Everything that needs a bootstrapped routing table: the identity
        and discovery keys, the rare-artist tail, capabilities, the
        re-announce cycles — and the sync loop's first source."""
        await self._dht_service.wait_ready()

        from desktop.node_identity import get_account_info
        account_info = get_account_info()
        if account_info and account_info.get("invite_code"):
            await self._dht_service.announce_user(account_info["invite_code"])
            _progress(f"User announced: {account_info['invite_code']}")

        # The discovery key — how peers find this node at all.
        await self._dht_service.announce_node()

        # Rare-artist tail — registration only; the drip loop in
        # dht_service announces it at its own spacing, and only under the
        # network-size verdict, which starts from the last run's so a
        # restart neither waits for a sync run nor announces blind.
        previous = await self._walk.db(sync_walk.read_setting, "p2p.rare_mode")
        self._dht_service.set_tail_enabled(bool(previous))
        _progress("Querying enriched artists...")
        enriched = await self._walk.db(sync_walk.enriched_artist_uuids)
        self._lan_discovery.update_enriched_count(len(enriched))
        tail = await self._walk.db(sync_walk.announce_tail_uuids)
        if tail:
            asyncio.create_task(self._dht_service.announce_artists(tail))
            _progress(
                f"P2P online: node announced, {len(tail)} rare artists queued"
            )
        else:
            _progress("P2P online: node announced")

        # Advertise the MB dump capability so dump-less nodes can find
        # this node beyond LAN/manual peers
        if self._mb_dump_version:
            await self._dht_service.announce_capability("mbdump")
        if await self._lb_dump_local():
            await self._dht_service.announce_capability("lbdump")

        self._reannounce_task = asyncio.create_task(
            self._dht_service.periodic_reannounce()
        )
        self._walk.first_source.set()

    async def _check_sync_server_health(self) -> bool:
        """Check if the sync server is actually accepting connections.

        Returns the reason the server needs a restart (a short string),
        or None when it is healthy — the reason is logged, so a restart
        can be told apart from a probe hiccup after the fact.

        First checks socket-level state (fast), then falls back to a
        self-connection probe every 3 consecutive failures to avoid
        false positives from transient network hiccups.
        """
        if not self._sync_server or not self._sync_server._site:
            return "no site"

        site = self._sync_server._site
        # Fast checks: server object or socket clearly dead
        if site._server is None:
            return "server object gone"
        if site._server.sockets is not None and len(
            site._server.sockets
        ) == 0:
            return "no listening sockets"
        try:
            if (site._server.sockets
                    and site._server.sockets[0].fileno() == -1):
                return "listening socket closed"
        except Exception as e:
            return f"socket state unreadable: {e!r}"

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
            return None  # server is healthy
        except Exception as exc:
            self._health_fail_count += 1
            if self._health_fail_count >= 3:
                return (f"self-connect failed {self._health_fail_count} "
                        f"times: {exc}")
            return None  # might be transient
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
        if self._chat_client:
            await self._chat_client.stop()
        for task in (self._dht_online_task, self._reannounce_task,
                     self._db_listen_task,
                     self._pending_accepts_task, self._lan_discovery_task,
                     self._sync_request_listen_task, self._sync_request_task,
                     self._auto_sync_task,
                     *self._mb_cycle_tasks, *self._lb_cycle_tasks,
                     self._upnp_renewal_task, self._diag_task,
                     self._relay_cap_task):
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

    def _start_identity_proof_worker(self, cert: dict) -> None:
        if self._proof_thread is not None and self._proof_thread.is_alive():
            return
        from desktop.p2p import birth_cert, identity_proof

        def publish(state: dict) -> None:
            try:
                self._write_settings_blocking({"p2p.identity": state})
                self._notify_blocking("sautium_identity")
            except Exception as e:
                logger.debug("identity state publish failed: %s", e)
            if state.get("status") == "ready" and self._chat_client:
                # The freshly mined proof is the event the parked cert-gated
                # handshakes wait for — wake the resolver now, not in ≤15 s.
                self._chat_client.resolve_soon()

        hold = self._load_meter.mining_hold if self._load_meter else (lambda: None)
        self._proof_stop = threading.Event()
        self._proof_thread = threading.Thread(
            target=identity_proof.ensure_identity_proof,
            args=(cert, birth_cert.proof_path()),
            kwargs={"stop": self._proof_stop, "on_state": publish, "hold": hold},
            daemon=True, name="identity-proof")
        self._proof_thread.start()

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

        # The proof worker exits between attempts (one ~2 GiB call at most).
        if self._proof_stop is not None:
            self._proof_stop.set()
        if self._load_meter is not None:
            self._load_meter.stop()

        # Break out of DHT bootstrap wait (sync flag, safe from any thread)
        if self._dht_service:
            self._dht_service._running = False

        # Signal _async_main to exit its wait loop. The loop thread itself
        # awaits the event and runs `_cleanup()` in its `finally` block.
        if self._loop and not self._loop.is_closed():
            async def _signal():
                if self._stop_event:
                    self._stop_event.set()

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

        if self._proof_thread is not None:
            self._proof_thread.join(timeout=10)
            self._proof_thread = None

        self._running = False
        self._loop = None
        self._thread = None
        logger.info("P2P manager stopped")

    # -------------------------------------------------------------------
    # P2P Sync — the walk lives in desktop/p2p/sync_walk.py (shared with
    # the Docker backend); this side only feeds it triggers.
    # -------------------------------------------------------------------

    def _request_sync(self, reason: str) -> None:
        """Queue a sync run and remember why (loop thread). Concurrent
        reasons merge into the one run the dispatcher starts next."""
        self._walk.request(reason)

    def _on_lan_peer(self, ip: str, port: int) -> None:
        """A LAN beacon (or the localhost Docker probe) found a node that
        was not there a moment ago — the cheapest source there is, one hop
        away. Sync now rather than at the next interval."""
        if self._walk.first_source:
            self._walk.first_source.set()
        self._request_sync("lan-peer")
        # A friend one hop away may be the one still waiting on a rotation
        # notice or a handshake — no reason to sit out the resolver's tick.
        if self._chat_client:
            self._chat_client.resolve_soon()

    def _dht_skip_addrs(self) -> set[str]:
        """DHT addresses that are not peers worth probing, all on our own
        external IP: this node (external_ip:announce_port), LAN peers
        through the router (same external IP — already synced one hop
        away) and UPnP-mapped Docker ports (not Sautium sync servers)."""
        skip: set[str] = set()
        ext_ip = self._upnp.external_ip if self._upnp else None
        if not ext_ip:
            return skip
        if self._dht_service:
            skip.add(f"{ext_ip}:{self._dht_service._announce_port}")
        if self._lan_discovery:
            for _lip, lport in self._lan_discovery.peers:
                skip.add(f"{ext_ip}:{lport}")
        if self._upnp:
            for ext_port, int_port in self._upnp._mapped:
                skip.add(f"{ext_ip}:{ext_port}")
                skip.add(f"{ext_ip}:{int_port}")
        return skip

    def _peer_identity(self):
        """This node as a peer CLIENT (wire format v1, desktop/p2p/peer_auth.py):
        signer, pubkey and a lazy {cert, proof} loader — the certificate and
        the mined proof can land after the client object exists. A peer's
        certificate on its /health goes to the sync server's witness record
        as a `probe` sighting, and an author's certificate a pulled payload
        carried as a `carried` one (desktop/p2p/witness.py)."""
        from desktop.node_identity import get_account_info, sign_message
        from desktop.p2p import peer_auth
        account = get_account_info()
        if not account:
            return None

        def witness(cert: dict, pubkey: str) -> None:
            server = self._sync_server
            if server is not None:
                server.witness.note(cert, "probe", pubkey)

        def carried(cert: dict, author: str, via: str) -> None:
            server = self._sync_server
            if server is not None:
                server.witness.note(cert, "carried", author, via)

        return peer_auth.PeerIdentity(pubkey=account["public_key_hex"].lower(),
                                      sign=sign_message, cert_bundle=self._cert_bundle,
                                      witness=witness, carried=carried)

    @staticmethod
    def _cert_bundle() -> Optional[dict]:
        from desktop.p2p import birth_cert
        cert = birth_cert.load_certificate()
        return {"cert": cert, "proof": birth_cert.load_proof()} if cert else None

    def _client_identity(self) -> Optional[ClientIdentity]:
        """This node's account as the chat client speaks it — read on every
        call, as the moved code read get_account_info."""
        from desktop.node_identity import get_account_info, sign_message
        account = get_account_info()
        if not account:
            return None
        return ClientIdentity(public_key_hex=account["public_key_hex"],
                              username=account["username"],
                              invite_code=account["invite_code"],
                              sign=sign_message, cert_bundle=self._cert_bundle)

    def _announce_port(self) -> int:
        """The port peers dial: the UPnP external one when mapped."""
        return ((self._upnp.get_external_port(self._http_port) if self._upnp else None)
                or self._http_port)

    def _chat_incoming(self, result: dict) -> None:
        """A message the client stored from a relay, or a history pull that
        imported: the tray hears of it; the Web UI of a relayed one."""
        if self._on_message_cb:
            self._on_message_cb(result)
        if not result.get("history_sync") and self._sync_server:
            asyncio.ensure_future(self._sync_server._wake_backend_sse())

    def _master_reachable(self, addr: tuple) -> None:
        """The master's stream connected: retry staged support bundles and
        unshipped reports."""
        self._master_addr = addr
        self._diag_queue.put_nowait(("resume", addr))

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

    def _notify_blocking(self, channel: str) -> None:
        """Fire one NOTIFY on a short-lived connection (executor-safe)."""
        conn = psycopg2.connect(self.db_dsn)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f"NOTIFY {channel}")
        finally:
            conn.close()

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

    async def _apply_reachability(self, status: str, _detail: str) -> None:
        """The verdict's consumers here (the chat client holds and records
        it): the DHT's own announces and the relay role."""
        if self._dht_service:
            self._dht_service.set_announces_enabled(
                status not in ("cgnat", "unreachable"))
        await self._apply_relay_role(status)

    async def _relay_cap_loop(self) -> None:
        """The relay's capacity on the re-announce cadence (mirror of the
        Docker node's main._relay_cap_loop): a full relay that sees no
        other relay grows its cap, one with room in a network that has
        relays decays back, and the capability is announced only while a
        stranger could still get a slot — a full relay withdraws it."""
        from desktop.p2p.dht_service import REANNOUNCE_INTERVAL
        while self._running:
            await asyncio.sleep(REANNOUNCE_INTERVAL)
            if self._sync_server and self._read_setting("p2p.relay_enabled") is False:
                # Off: no slot outlives the switch — one admitted while the
                # off notice was on its way included (as the Docker loop does).
                self._sync_server.close_relay_clients()
                continue
            if not (self._dht_service and self._sync_server
                    and self._reachability_status == "reachable"):
                continue
            try:
                peers = await self._dht_service.lookup_capability("relay")
                self._sync_server.adapt_relay_cap(len(set(peers)) > 1)
                if self._sync_server.relay_has_room():
                    await self._dht_service.announce_capability("relay")
                else:
                    self._dht_service.withdraw_capability("relay")
            except Exception as e:
                logger.warning(f"relay cap step failed: {e}")

    async def _apply_relay_role(self, status: str) -> None:
        """Relay role (Phase D): reachable + non-lite + enabled → announce
        Sautium-cap:relay and serve strangers; anything else → withdraw and
        close foreign subscriptions (their clients re-register elsewhere —
        graceful degradation, no global reputation)."""
        if not self._dht_service or not self._sync_server:
            return
        # No hardware-profile gate here: the profile lives in the backend
        # process and relaying is byte-shuffling (SSE + small forwards), not
        # ML work — a torch-less node relays fine. The plan's "non-lite"
        # condition is about capability the role does not actually need.
        want = (status == "reachable"
                and self._read_setting("p2p.relay_enabled") is not False)
        if want:
            self._sync_server.set_client_announce_cbs(
                lambda code: asyncio.get_event_loop().create_task(
                    self._dht_service.announce_user_for(code)),
                self._dht_service.withdraw_user_for)
            if self._sync_server.relay_has_room():
                await self._dht_service.announce_capability("relay")
        else:
            self._dht_service.withdraw_capability("relay")
            for pk in list(self._sync_server._relay_clients):
                self._sync_server._wake.close(pk)

    async def _directory_register_loop(self) -> None:
        """The volunteer side of the capability directory (Ф16c): while this
        node is REACHABLE (and not the master — it publishes /master-hint),
        re-register hourly so DHT-less clients can find sync/slices/relay
        beyond the master. The Worker's TTL (2 h) makes a vanished node
        disappear on its own."""
        from desktop.node_identity import get_account_info, sign_message
        from desktop.p2p import node_hints
        from desktop.p2p.master_node import MASTER_PUBKEY_HEX
        while self._running:
            try:
                account = get_account_info()
                if (account and self._reachability_status == "reachable"
                        and account["public_key_hex"] != MASTER_PUBKEY_HEX):
                    caps = ["sync"]
                    if self._mb_dump_version:
                        caps.append("mbdump")
                    elif self.config.get("mb_slice", {}).get("serve", True):
                        caps.append("mbslices")
                    if await self._lb_dump_local():
                        caps.append("lbdump")
                    elif self.config.get("lb_slice", {}).get("serve", True):
                        caps.append("lbslices")
                    if self._read_setting("p2p.relay_enabled") is not False:
                        caps.append("relay")
                    port = ((self._upnp.get_external_port(self._http_port)
                             if self._upnp else None) or self._http_port)
                    await asyncio.get_event_loop().run_in_executor(
                        None, node_hints.register,
                        account["public_key_hex"], sign_message, port, caps)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"directory registration cycle: {e}")
            await asyncio.sleep(3600)

    async def _poll_pending_accepts(self):
        """The start's pickup: the acceptances of our invites, and the adds
        of our code, that came while this node was off (email_verify.pick_up
        — the same function the Friends screen and a peer's nudge run).
        Later ones come with Friends opening, or a nudge."""
        await asyncio.sleep(10)  # initial delay for network setup
        try:
            if await self._pick_up():
                self._resolve_soon()
        except Exception as e:
            logger.warning(f"Startup pickup failed: {e}")

    async def _pick_up(self) -> list:
        """One pickup at a time on this node (the start, a nudge): the
        contacts it added. A request that comes while one waits for its turn
        joins that one — it begins after the request came all the same — so
        nudges at once cost one pickup, not one each."""
        if self._pickup_waiting is None:
            self._pickup_waiting = asyncio.ensure_future(self._pickup_turn())
        return await asyncio.shield(self._pickup_waiting)

    async def _pickup_turn(self) -> list:
        try:
            async with self._pickup_lock:
                self._pickup_waiting = None        # a request from now on waits for the next turn
                return await asyncio.get_running_loop().run_in_executor(None, self._pick_up_blocking)
        finally:
            if self._pickup_waiting is asyncio.current_task():   # cancelled while it waited
                self._pickup_waiting = None

    def _pick_up_blocking(self) -> list:
        from desktop.node_identity import get_account_info, sign_message
        from desktop.p2p import birth_cert, email_verify
        account = get_account_info()
        # The Worker answers only a key it issued a certificate to.
        if not account or birth_cert.load_certificate() is None:
            return []
        import psycopg2
        conn = psycopg2.connect(self.db_dsn)
        conn.autocommit = True
        try:
            picked = email_verify.pick_up(
                conn, account["invite_code"], account["public_key_hex"], sign_message)
        finally:
            conn.close()
        if (picked["added"] or picked["requested"]) and self._sync_server:
            asyncio.run_coroutine_threadsafe(self._sync_server._wake_backend_sse(), self._loop)
        return picked["added"]

    def _resolve_soon(self) -> None:
        if self._chat_client:
            self._chat_client.resolve_soon()

                # 409 / no receipt from THIS relay — the friend may hold a
                # live stream to the next one (K relays announce it in
                # parallel), so a dead candidate is a reason to move on,
                # not to give up the batch.

    async def _listen_for_db_notifications(self):
        """Listen for PostgreSQL NOTIFY on the 'sautium_chat' and
        'sautium_diag' channels.

        sautium_chat wakes the chat client's pass instantly when the
        backend inserts a new outgoing message (ChatClient.chat_notes); sautium_diag (the
        diag_events insert trigger) wakes the support-report shipper, and
        sautium_diag_consent (diag_events.decide) the collection of a
        support request the person decided on.
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
                    cur.execute("LISTEN sautium_diag")
                    cur.execute("LISTEN sautium_diag_consent")
                    cur.execute("LISTEN sautium_identity_rotated")
                    cur.execute("LISTEN sautium_relay_off")
                # A decision made while this connection was not listening
                # woke nobody: the worker looks once on every (re)connect
                self._diag_queue.put_nowait(("decided",))

                while self._running:
                    # Notices a query on this connection collected wait in
                    # conn.notifies with nothing more to read on the socket.
                    if not conn.notifies:
                        if not await asyncio.get_event_loop().run_in_executor(
                                None, sync_walk.readable, conn, 5):
                            continue
                        conn.poll()
                    notes = []
                    while conn.notifies:
                        notes.append(conn.notifies.pop(0))
                    channels = {note.channel for note in notes}
                    if self._chat_client:
                        self._chat_client.chat_notes(notes)
                    replies = replied_friends(notes)
                    if replies:
                        await self._ping_wake_for(conn, replies)
                    if "sautium_diag" in channels:
                        self._diag_queue.put_nowait(("report",))
                    if "sautium_diag_consent" in channels:
                        self._diag_queue.put_nowait(("decided",))
                    if "sautium_relay_off" in channels and self._sync_server:
                        # The owner switched relaying off: the slots held end
                        # now, and the capability goes with them.
                        self._sync_server.close_relay_clients()
                        if self._dht_service:
                            self._dht_service.withdraw_capability("relay")
                    if ("sautium_identity_rotated" in channels
                            and self._on_identity_rotated_cb):
                        self._on_identity_rotated_cb()
            except Exception as e:
                logger.debug(f"DB LISTEN error: {e}")
                await asyncio.sleep(5)
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

    async def _ping_wake_for(self, conn, friend_ids: set) -> None:
        """Signal the wake streams of friends a reply was just stored for —
        the ones that hold a stream here pull at once. `conn` is the LISTEN
        loop's own, idle between its waits."""
        if not self._sync_server or not self._sync_server.has_wake_streams():
            return

        def keys() -> list:
            with conn.cursor() as cur:
                cur.execute("SELECT public_key_hex FROM friends WHERE id = ANY(%s)",
                            (list(friend_ids),))
                return [r[0] for r in cur.fetchall()]

        for pubkey in await asyncio.get_event_loop().run_in_executor(None, keys):
            self._sync_server.ping_wake(pubkey)

    # -------------------------------------------------------------------
    # Sync triggers — the walk itself lives in desktop/p2p/sync_walk.py
    # -------------------------------------------------------------------

    async def _sync_notification_listener(self):
        """LISTEN for the walk's request channel plus the launcher's two
        MB-slice wakes (desktop/p2p/sync_walk.listen_notifications)."""
        await sync_walk.listen_notifications(self.db_dsn, {
            # The backend after a scan that added files, a canon run, a
            # discography/similars growth — sync now, not at the interval.
            "sautium_sync_request": lambda: self._walk.request("request"),
            # Enrichment is what MINTS phantom similars, and it runs in the
            # backend — without this the freshly minted names wait out the
            # 6-hour slice timer even with a dump peer online right now.
            "sautium_enrich_done": lambda: self._mb_cycle.request("enrich"),
            # Backend fires this when a remote MB search exhausted every
            # known source — re-probe now instead of waiting out the slice
            # timer, so the chip flips honestly.
            "sautium_mb_sources_request": lambda: asyncio.create_task(
                self._mb_cycle.refresh_sources()),
            # Imported Last.fm scrobbles wait on names only a slice can
            # place (backend/canon/scrobbles.py).
            "sautium_mb_pending": lambda: self._mb_cycle.request("scrobbles"),
            # The ListenBrainz family: the MBID set grew (a trigger on
            # artist_mbids), an artist page asked for one artist now, or
            # the backend finished/deleted the local statistics dump.
            "sautium_lb_pending": lambda: self._lb_cycle.request("pending"),
            "sautium_lb_request": lambda: self._lb_cycle.request("request"),
            "sautium_lb_sources": lambda: asyncio.create_task(
                self._lb_sources_changed()),
            # The owner moved P2P sharing (routers/settings.put_sync_prefs).
            "sautium_sharing": lambda: asyncio.create_task(self._sharing_changed()),
        }, lambda: self._running, on_listening=self._listening_again)

    def _listening_again(self) -> None:
        """The LISTENs are up — the first time, or after a reconnect: what was
        said while none was would be lost, so the switch is read again and an
        artist page's request made meanwhile is asked now."""
        asyncio.create_task(self._sharing_changed())
        asyncio.create_task(self._lb_cycle.pages_waiting())

    def _sharing_on(self) -> bool:
        """The "P2P sharing" switch as this process reads it — the sync
        server's cached read, which the owner's move of the switch (NOTIFY
        sautium_sharing) refreshes at once: serving and every requester here
        (the walk, both slice cycles, the DHT tail) follow the one value."""
        return self._sync_server._sharing_enabled()

    async def _sharing_changed(self) -> None:
        """P2P sharing as it is now — at start, whenever the owner moves it,
        and whenever the notifications are listened for again: the LAN
        beacon names this node on every network it joins only while it is
        on (LANDiscovery.set_beaconing). One read at a time, each applied
        before the next starts: an earlier read finishing later would put
        back what the switch said before. Turned back on, the walk is asked
        for here: the one the owner's move requested (NOTIFY
        sautium_sync_request) may have read this process's cached "off"
        before this read refreshed it."""
        async with self._sharing_lock:
            on = await asyncio.get_running_loop().run_in_executor(
                None, self._sync_server.sharing_changed)
            self._lan_discovery.set_beaconing(on)
            if on and self._sharing_read is False:
                self._walk.request("sharing")
            self._sharing_read = on

    async def _after_walk(self):
        """What the walk just carried in may name artists canon waits on
        (MB slices) and may have brought new artist MBIDs (LB slices)."""
        self._mb_cycle.request("sync")
        self._lb_cycle.request("sync")

    async def _lb_dump_local(self) -> Optional[str]:
        """The ListenBrainz dump version served from this database, read
        live: the backend finishes a load while this process runs."""
        try:
            return await asyncio.get_event_loop().run_in_executor(
                None, lb_slice_cycle.local_dump_available, self.db_dsn)
        except Exception as e:
            logger.debug(f"LB dump capability check failed: {e}")
            return None

    async def _lb_sources_changed(self) -> None:
        """A local statistics load finished (announce the capability, no
        restart needed) or the dump was deleted (this node asks again)."""
        if self._dht_service and await self._lb_dump_local():
            try:
                await self._dht_service.announce_capability("lbdump")
            except Exception as e:
                logger.debug(f"lbdump announce failed: {e}")
        self._lb_cycle.request("sources")

    # -------------------------------------------------------------------
    # MB dump slices (desktop/p2p/mb_slice_cycle.py) — the launcher's side
    # -------------------------------------------------------------------

    def _canonicalize_locally(self) -> None:
        """The MB cycle's after-import hook (executor thread): hand the
        freshly imported facts to the backend canon."""
        api = self._local_backend_api()
        result = api.canonicalize() if api else None
        if not result or not (result.get("started") or result.get("running")):
            logger.warning(f"backend canonicalize trigger failed: {result}")

    def _local_backend_api(self) -> Optional[BackendAPIClient]:
        port = self.config.get("ports", {}).get("web", 0)
        if not port:
            return None
        return BackendAPIClient(f"http://127.0.0.1:{port}")

    def _load_p2p_bans(self) -> tuple[set, set]:
        """Local ban list: (pubkeys, addr uuids). The pubkey ban is the
        anchor (proven by slice receipts); the address id catches a banned
        key returning under a fresh identity from the same place."""
        try:
            conn = psycopg2.connect(self.db_dsn)
            try:
                with conn.cursor() as cur:
                    # A key whose proof failed here is refused as a ban row
                    # is, after its row was evicted past the cap.
                    cur.execute("""SELECT pubkey, addr::text FROM p2p_node_bans
                                   UNION ALL SELECT pubkey, NULL FROM p2p_identities
                                              WHERE status = 'failed'""")
                    rows = cur.fetchall()
                return ({r[0] for r in rows if r[0]},
                        {r[1] for r in rows if r[1]})
            finally:
                conn.close()
        except Exception as e:
            logger.debug(f"Failed to load p2p bans: {e}")
            return set(), set()

    # ------------------------------------------------------------------
    # Support diagnostics (desktop/p2p/diag_protocol.py)
    # ------------------------------------------------------------------

    def _diag_record(self, kind: str, detail: dict) -> None:
        """Executor-safe: one event on a short-lived connection."""
        from desktop.config_manager import get_data_dir
        from desktop.p2p import diag_events
        diag_events.record_or_spool(self.db_dsn, get_data_dir(), kind, detail)

    def _diag_db(self, fn, *args):
        """Run fn(conn, *args) on a short-lived autocommit connection —
        the diag_events primitives from an executor thread."""
        conn = psycopg2.connect(self.db_dsn)
        try:
            conn.autocommit = True
            return fn(conn, *args)
        finally:
            conn.close()

    def _diag_allowed(self) -> bool:
        """The node speaks to support only while the user lets it: the
        toggle, the master contact still present and not blocked — and
        never when this node IS the master."""
        from desktop.node_identity import get_account_info
        from desktop.p2p.master_node import MASTER_PUBKEY_HEX, master_configured
        if not master_configured() or not self._chat_service:
            return False
        account = get_account_info()
        if not account or account["public_key_hex"] == MASTER_PUBKEY_HEX:
            return False
        if (self._read_setting("support.diagnostics_enabled") is False
                or self._read_setting("p2p.master_removed")):
            return False
        master = self._chat_service.get_friend_by_public_key(MASTER_PUBKEY_HEX)
        return bool(master) and not master.get("is_blocked")

    async def _diag_worker(self) -> None:
        """One sequential worker for everything support-related: warrants
        from the master's wake stream, upload/report retries when the
        master is reachable again, event reports woken by LISTEN
        sautium_diag. Sequential on purpose — a warrant re-delivered while
        its first copy is still collecting lands behind it and becomes
        the no-op the state machine makes of it. A job that fails leaves
        its state in the database (diag_warrants.error, unreported rows);
        the worker itself lives on."""
        while self._running:
            job = await self._diag_queue.get()
            try:
                if not self._diag_allowed():
                    continue
                if job[0] == "warrant":
                    await self._diag_handle_warrant(job[1], job[2])
                elif job[0] == "resume":
                    await self._diag_decided()
                    await self._diag_resume(job[1])
                    await self._diag_report()
                elif job[0] == "report":
                    await self._diag_report()
                elif job[0] == "decided":
                    await self._diag_decided()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("diag worker: %s job failed: %s", job[0], e,
                               exc_info=True)

    async def _diag_handle_warrant(self, warrant: dict, addr) -> None:
        """A verified warrant: answered at once when it names structural
        scopes only; one that names a content scope waits for the person's
        decision on the request sheet (diag_events.decide → _diag_decided) —
        the notices tell the Web UI it is there."""
        from desktop.config_manager import get_data_dir
        from desktop.node_identity import get_account_info
        from desktop.p2p import diag_events

        own = get_account_info()["public_key_hex"]
        ok, reason = diag_protocol.verify_warrant(warrant, own)
        if not ok:
            logger.warning("Support warrant rejected: %s", reason)
            return
        loop = asyncio.get_event_loop()
        short = warrant["id"][:8]
        state = await loop.run_in_executor(
            None, self._diag_db, diag_events.receive_warrant, warrant)
        if state["uploaded_at"] or state["error"]:
            logger.info("Support warrant %s… already %s", short,
                        "fulfilled" if state["uploaded_at"] else "failed")
            return
        stage = diag_events.diag_dir(get_data_dir()) / f"{warrant['id']}.box"
        if state["withdrawn"]:
            # The person decided, and the switch moved after: nothing of it
            # is gathered
            stage.unlink(missing_ok=True)
            await loop.run_in_executor(
                None, self._diag_db, diag_events.mark_failed, warrant["id"], diag_events.WITHDRAWN)
            logger.info("Support warrant %s… %s", short, diag_events.WITHDRAWN)
            return
        if state["awaits"]:
            # A bundle an earlier release staged before asking is not the
            # person's to send: it goes, and the request waits like any other
            stage.unlink(missing_ok=True)
            if state["fresh"]:
                logger.info("Support warrant %s… waits for the owner's decision: scopes=%s",
                            short, ",".join(warrant["scopes"]))
                await loop.run_in_executor(None, self._notify_blocking, "sautium_notices")
            return
        if state["collected_at"] is None or not stage.exists():
            if not await self._diag_collect(warrant, state["granted"], stage):
                return
        await self._diag_upload(warrant["id"], stage, addr)

    async def _diag_collect(self, warrant: dict, granted: list, stage: Path) -> bool:
        """Collect a warrant's bundle — its content scopes as far as the
        person granted them — box it to the master and stage it. False when
        the collection failed; its warrant is closed with the error."""
        from desktop import diag_bundle
        from desktop.config_manager import get_data_dir
        from desktop.node_identity import get_account_info, get_private_key_raw
        from desktop.p2p import diag_events
        from desktop.p2p.master_node import MASTER_PUBKEY_HEX

        loop = asyncio.get_event_loop()
        short = warrant["id"][:8]
        logger.info("Support warrant %s… accepted: scopes=%s granted=%s", short,
                    ",".join(warrant["scopes"]), ",".join(granted) or "-")
        extra = {"p2p_status": self.get_status(),
                 "reachability": self._reachability_status}
        began = datetime.now(timezone.utc)
        try:
            tar = await loop.run_in_executor(None, partial(
                diag_bundle.collect, db_dsn=self.db_dsn, data_dir=get_data_dir(),
                config=self.config, warrant=warrant, granted=granted,
                node_pubkey=get_account_info()["public_key_hex"], extra=extra))
        except Exception as e:
            logger.error("Support bundle %s… collection failed: %s", short, e,
                         exc_info=True)
            await loop.run_in_executor(
                None, self._diag_db, diag_events.mark_failed, warrant["id"],
                f"collect: {e}")
            return False
        boxed = diag_protocol.encrypt_to(get_private_key_raw(), MASTER_PUBKEY_HEX, tar)
        stage.write_bytes(boxed)
        await loop.run_in_executor(
            None, self._diag_db, diag_events.mark_collected, warrant["id"], len(boxed), began)
        return True

    async def _diag_decided(self) -> None:
        """The person decided on waiting requests (sautium_diag_consent): each
        is collected with what they ticked and sent at once when the master's
        stream holds an address, else staged for its next reconnect."""
        from desktop.config_manager import get_data_dir
        from desktop.p2p import diag_events

        loop = asyncio.get_event_loop()
        decided = await loop.run_in_executor(
            None, self._diag_db, diag_events.decided_uncollected)
        for warrant, granted in decided:
            stage = diag_events.diag_dir(get_data_dir()) / f"{warrant['id']}.box"
            if not await self._diag_collect(warrant, granted, stage):
                continue
            if self._master_addr is not None:
                await self._diag_upload(warrant["id"], stage, self._master_addr)

    async def _diag_upload(self, warrant_id: str, stage: Path, addr) -> bool:
        """POST the staged box to the master's peer surface. A refusal
        that cannot change (403/404/409/410/413) closes the warrant; a
        network failure leaves the stage for the next reconnect. A bundle
        the owner withdrew — diagnostics switched off since its collection
        began, whatever they say now — is deleted and its warrant closed,
        decided right before it would leave."""
        import aiohttp
        from desktop.node_identity import get_account_info, sign_message
        from desktop.p2p import diag_events, peer_auth
        from desktop.p2p.master_node import MASTER_PUBKEY_HEX

        loop = asyncio.get_event_loop()
        if not await loop.run_in_executor(None, self._diag_db, diag_events.may_upload, warrant_id):
            stage.unlink(missing_ok=True)
            await loop.run_in_executor(
                None, self._diag_db, diag_events.mark_failed, warrant_id, diag_events.WITHDRAWN)
            logger.info("Support bundle %s… %s", warrant_id[:8], diag_events.WITHDRAWN)
            return False
        body = stage.read_bytes()
        own = get_account_info()["public_key_hex"]
        target = f"/api/diag/bundle?warrant={warrant_id}"
        headers = peer_auth.sign_headers(sign_message, own, MASTER_PUBKEY_HEX,
                                         "POST", target, body)
        headers["Content-Type"] = "application/octet-stream"
        ip, port = addr
        try:
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=pinned_ssl_context(MASTER_PUBKEY_HEX)),
                timeout=aiohttp.ClientTimeout(total=300, connect=10),
            ) as session:
                async with session.post(f"https://{fmt_addr(ip, port)}{target}",
                                        data=body, headers=headers) as resp:
                    status = resp.status
                    text = await resp.text()
        except Exception as e:
            logger.info("Support bundle %s… upload deferred: %s", warrant_id[:8], e)
            return False
        if status == 200:
            await loop.run_in_executor(
                None, self._diag_db, diag_events.mark_uploaded, warrant_id)
            stage.unlink(missing_ok=True)
            await loop.run_in_executor(None, self._write_settings_blocking, {
                "support.last_bundle_at": datetime.now(timezone.utc).isoformat()})
            await loop.run_in_executor(None, self._notify_blocking, "sautium_identity")
            logger.info("Support bundle %s… delivered (%d bytes)", warrant_id[:8], len(body))
            return True
        if status in (403, 404, 409, 410, 413):
            await loop.run_in_executor(
                None, self._diag_db, diag_events.mark_failed, warrant_id,
                f"HTTP {status}: {text[:200]}")
            stage.unlink(missing_ok=True)
            logger.warning("Support bundle %s… refused by the master: HTTP %s %s",
                           warrant_id[:8], status, text[:200])
            return False
        logger.info("Support bundle %s… upload deferred: HTTP %s", warrant_id[:8], status)
        return False

    async def _diag_resume(self, addr) -> None:
        from desktop.config_manager import get_data_dir
        from desktop.p2p import diag_events

        loop = asyncio.get_event_loop()
        pending = await loop.run_in_executor(
            None, self._diag_db, diag_events.resumable_warrants)
        stage_dir = diag_events.diag_dir(get_data_dir())
        for warrant_id in pending:
            stage = stage_dir / f"{warrant_id}.box"
            if not stage.exists():
                await loop.run_in_executor(
                    None, self._diag_db, diag_events.mark_failed, warrant_id,
                    "staged bundle missing")
                continue
            await self._diag_upload(warrant_id, stage, addr)

    async def _diag_report(self) -> None:
        """Ship unreported events to the master — content-free (encode_report
        strips the local-only fields), boxed, capped per hour. Whatever
        stays behind rides the next wake. The events are read after the
        master's address is found, just before they leave: the switch's
        rule (diag_events.SWITCH_ALLOWS) decides at that moment, not before a
        lookup that may take a DHT's timeout."""
        import aiohttp
        from desktop.node_identity import (get_account_info, get_private_key_raw,
                                           sign_message)
        from desktop.p2p import diag_events, peer_auth
        from desktop.p2p.master_node import MASTER_PUBKEY_HEX

        loop = asyncio.get_event_loop()
        now = time.time()
        self._diag_sent = [t for t in self._diag_sent if now - t < 3600]
        if len(self._diag_sent) >= diag_protocol.REPORT_MAX_PER_HOUR:
            return
        if not await loop.run_in_executor(None, self._diag_db, diag_events.pending_count):
            return
        addr = self._master_addr
        if addr is None:
            master = self._chat_service.get_friend_by_public_key(MASTER_PUBKEY_HEX)
            peers = await self._chat_client.find_friend_peers(master) if master else []
            if not peers:
                return
            addr = peers[0]
        events = await loop.run_in_executor(
            None, self._diag_db, diag_events.drain_unreported)
        if not events:
            return
        own = get_account_info()["public_key_hex"]
        body = diag_protocol.encrypt_to(get_private_key_raw(), MASTER_PUBKEY_HEX,
                                        diag_protocol.encode_report(events))
        headers = peer_auth.sign_headers(sign_message, own, MASTER_PUBKEY_HEX,
                                         "POST", "/api/diag/report", body)
        headers["Content-Type"] = "application/octet-stream"
        ip, port = addr
        try:
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=pinned_ssl_context(MASTER_PUBKEY_HEX)),
                timeout=aiohttp.ClientTimeout(total=30, connect=10),
            ) as session:
                async with session.post(f"https://{fmt_addr(ip, port)}/api/diag/report",
                                        data=body, headers=headers) as resp:
                    status = resp.status
        except Exception as e:
            logger.debug("diag report deferred: %s", e)
            return
        self._diag_sent.append(now)
        if status != 200:
            logger.info("diag report refused by the master: HTTP %s", status)
            return
        await loop.run_in_executor(
            None, self._diag_db, diag_events.mark_reported, [e["id"] for e in events])
        await loop.run_in_executor(None, self._write_settings_blocking, {
            "support.last_report_at": datetime.now(timezone.utc).isoformat()})
        await loop.run_in_executor(None, self._notify_blocking, "sautium_identity")
        logger.info("diag report delivered: %d event(s)", len(events))
        if len(events) >= diag_protocol.REPORT_MAX_EVENTS:
            self._diag_queue.put_nowait(("report",))     # more is waiting

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
