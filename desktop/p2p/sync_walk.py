"""The sync walk — the PULL side of the P2P protocol, one implementation for
both runtimes.

The launcher's P2PManager ran this walk from the start; the Docker backend
only served pulls and accepted carry, so the master's phantom layer could
receive what peers held first-hand only when a peer chose to carry it
(found 2026-09-08). The walk lives here and is IMPORTED by both — never
copied (the mirror rule in CLAUDE.md) — so the two surfaces walk the network
identically.

What a run does, in order (`SyncWalk.run`):

1. the gap set — artists missing data in any category — split into the
   CORE (engaged artists, asked of every peer in full through the exact
   inventory) and the phantom BULK (asked through the peer's holdings
   filter; sync_queries.split_engaged);
2. manual peers, then LAN peers (the launcher's beacon; a Docker node has
   no LAN tier — containers cannot broadcast);
3. tier A: ONE DHT node-key lookup plus the Worker directory's volunteers,
   probed concurrently, drained one at a time (each node's inventory
   answers for the whole library at once); the reply doubles as this run's
   network-size sample (network_size);
4. tier B: engaged artists still missing analysis, through their own DHT
   keys — only under the rare-mode verdict;
5. a reachable peer that speaks `carry` is offered our first-hand canon
   analysis (push-seeding — nobody can pull from a node behind CGNAT).

Runtime differences are injected, never branched on: the DHT service (both
runtimes have one with the same surface), LAN discovery, manual peers, the
address skip list, the sharing switch, diagnostics, the post-run hook.
Peer-search memory is address-keyed and in-process — never per-peer sync
state (a peer already drained is the wrong place to look for the
remainder; a "known peers book" would collect dead nodes).

psycopg2.connect() is a sync C-call: invoked from a coroutine it blocks the
event loop until the TCP/auth handshake returns (seconds during a backend
restart), and every `call_soon_threadsafe` from another thread queues
behind it. Every DB helper below is blocking by design and runs in the
executor (`SyncWalk._db`).
"""

import asyncio
import json
import logging
import select
import time
from datetime import datetime, timezone
from functools import partial
from typing import Awaitable, Callable, Optional
from urllib.parse import urlsplit

import psycopg2
import psycopg2.extensions

from desktop.api_client import BackendAPIClient
from desktop.p2p import master_hint, network_size, node_hints, sync_queries
from desktop.p2p.addrs import canon_host, fmt_addr
from desktop.sync_client import SEGMENT_PULL_BATCH, SyncClient

logger = logging.getLogger(__name__)

# How many residual artists get a targeted per-artist DHT lookup after node
# discovery has been drained. Each lookup costs a get_peers timeout, and only
# the rare tail is announced by key at all — so this stays a probe, not a
# sweep. Sized ~2 waves of the DHT batch concurrency (20).
_DHT_TAIL_PROBE = 50

# The first sync waits for a SOURCE — a LAN peer's beacon or the DHT
# bootstrap — not for a fixed delay; this caps the wait for a node with
# neither (the directory tier needs no local state at all).
FIRST_SYNC_MAX_DELAY = 60

# Peer-search memory (process lifetime, address-keyed, never per-peer sync
# state). A dead address backs off — a DHT entry outlives its node by up to
# 30 min, so every lookup keeps returning it — and a peer that answered
# with nothing for us is left alone until our gaps change: the residue is
# elsewhere by definition.
UNREACHABLE_BACKOFF_BASE = 30 * 60
UNREACHABLE_BACKOFF_MAX = 24 * 3600
EMPTY_PEER_TTL = 6 * 3600
PROBE_CONCURRENCY = 8

# Mirrors _DEFAULTS["sync.auto_interval_min"] in backend/routers/settings.py
# — keep in sync so the UI's displayed default and the actual cadence match
# on a fresh install (no user_settings row yet).
AUTO_SYNC_INTERVAL_DEFAULT_MIN = 30


# ---------------------------------------------------------------------------
# Blocking DB helpers — one short-lived autocommit connection each, always
# called through SyncWalk._db (the executor) from coroutine code.
# ---------------------------------------------------------------------------

def _connect(dsn: str):
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    return conn


def read_setting(dsn: str, key: str):
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM user_settings WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def write_settings(dsn: str, values: dict) -> None:
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            for key, value in values.items():
                cur.execute(
                    """
                    INSERT INTO user_settings (key, value)
                    VALUES (%s, %s::jsonb)
                    ON CONFLICT (key) DO UPDATE
                        SET value = EXCLUDED.value,
                            updated_at = CURRENT_TIMESTAMP
                    """,
                    (key, json.dumps(value)),
                )
    finally:
        conn.close()


def read_auto_sync_interval(dsn: str) -> Optional[int]:
    """sync.auto_interval_min: the default without a row, None when the
    row says null (explicitly disabled)."""
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM user_settings WHERE key = %s",
                        ("sync.auto_interval_min",))
            row = cur.fetchone()
        if row is None:
            return AUTO_SYNC_INTERVAL_DEFAULT_MIN
        if row[0] is None:
            return None
        return int(row[0]) if row[0] else None
    finally:
        conn.close()


def write_sync_status(dsn: str, started: datetime, items: int) -> None:
    """Persist sync.last_at + items_received, fire sautium_sync_done (the
    backend's SSE bridge and background enrichment's wake)."""
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            for key, value in (("sync.last_at", started.isoformat()),
                               ("sync.last_items_received", int(items))):
                cur.execute(
                    """
                    INSERT INTO user_settings (key, value)
                    VALUES (%s, %s::jsonb)
                    ON CONFLICT (key) DO UPDATE
                        SET value = EXCLUDED.value,
                            updated_at = CURRENT_TIMESTAMP
                    """,
                    (key, json.dumps(value)),
                )
            cur.execute("NOTIFY sautium_sync_done")
    finally:
        conn.close()


def enriched_artist_uuids(dsn: str) -> list[str]:
    conn = _connect(dsn)
    try:
        return sync_queries.get_enriched_artist_uuids(conn)
    finally:
        conn.close()


def announce_tail_uuids(dsn: str) -> list[str]:
    """Rare-artist tail for DHT announcing, sized by sync.announce_limit
    (0/null = node key only)."""
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM user_settings WHERE key = %s",
                        ("sync.announce_limit",))
            row = cur.fetchone()
        limit = int(row[0]) if row and row[0] else 0
        return sync_queries.get_announce_tail_uuids(conn, limit)
    finally:
        conn.close()


def unenriched_artist_uuids(dsn: str) -> list[str]:
    """The audio gap set between drains (partial gaps are
    incomplete_artist_uuids' job at the start of a run)."""
    conn = _connect(dsn)
    try:
        return sync_queries.get_unenriched_artist_uuids(conn)
    finally:
        conn.close()


def incomplete_artist_uuids(dsn: str) -> list[str]:
    """Artists missing data in ANY sync category — the run's trigger set.
    Catches partial states (audio landed but the Last.fm bio didn't) that
    the audio-only AND-logic of unenriched_artist_uuids skips."""
    conn = _connect(dsn)
    try:
        return sync_queries.get_incomplete_artist_uuids(conn)
    finally:
        conn.close()


def rare_search_uuids(dsn: str) -> list[str]:
    """Engaged artists with an audio gap, rarest first — the ones worth an
    exact DHT key (sync_queries.get_rare_search_uuids)."""
    conn = _connect(dsn)
    try:
        return sync_queries.get_rare_search_uuids(conn)
    finally:
        conn.close()


def tracks_for_artists(dsn: str, artist_uuids: list[str]) -> list[str]:
    if not artist_uuids:
        return []
    conn = _connect(dsn)
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


def core_and_bulk(dsn: str, artist_uuids: list[str],
                  ) -> tuple[list[str], list[str], list[str], list[str]]:
    """(core tracks, core artists, bulk tracks, bulk artists): the engaged
    artists and their tracks are the core, the rest the phantom bulk
    (sync_queries.split_engaged). Both are priced against a peer's
    holdings filter the same way; the split keeps the logs honest about
    what a run is made of."""
    conn = _connect(dsn)
    try:
        engaged, rest = sync_queries.split_engaged(conn, artist_uuids)
    finally:
        conn.close()
    return (tracks_for_artists(dsn, engaged), engaged,
            tracks_for_artists(dsn, rest), rest)


async def listen_notifications(db_dsn: str,
                               handlers: dict[str, Callable[[], None]],
                               running: Callable[[], bool]) -> None:
    """LISTEN on every channel in `handlers` and call a channel's handler
    once per poll in which it fired, however many notifies arrived.
    Select-on-socket with a 5 s timeout so cancellation stays responsive; a
    dropped connection is reopened after 5 s. Handlers run on the event
    loop — schedule anything long as a task."""
    while running():
        conn = None
        try:
            conn = psycopg2.connect(db_dsn)
            conn.set_isolation_level(
                psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
            with conn.cursor() as cur:
                for channel in handlers:
                    cur.execute(f"LISTEN {channel}")
            while running():
                ready = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: select.select([conn], [], [], 5))
                if ready[0]:
                    conn.poll()
                    fired = set()
                    while conn.notifies:
                        fired.add(conn.notifies.pop(0).channel)
                    for channel in fired:
                        handler = handlers.get(channel)
                        if handler:
                            handler()
        except Exception as e:
            logger.debug(f"LISTEN {sorted(handlers)} error: {e}")
            await asyncio.sleep(5)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass


class SyncWalk:
    """One node's pull side. Construct once per process, `bind()` on the
    event loop that will run `dispatch_loop`/`interval_loop`, then
    `request(reason)` from anywhere on that loop."""

    def __init__(
        self,
        db_dsn: str,
        *,
        identity: Callable[[], Optional[object]],
        sharing_enabled: Callable[[], bool],
        dht=None,
        lan=None,
        manual_peers: Optional[list[str]] = None,
        skip_addrs: Optional[Callable[[], set[str]]] = None,
        diag_record: Optional[Callable[[str, dict], None]] = None,
        after_run: Optional[Callable[[], Awaitable[None]]] = None,
        on_enriched_count: Optional[Callable[[int], None]] = None,
    ):
        """
        identity: () -> peer_auth.PeerIdentity | None — this node as a peer
            CLIENT; also how the walk recognises its own address (a DHT
            lookup returns us too, and the Docker surface has no observed
            external IP to subtract).
        sharing_enabled: the "P2P sharing" switch — gates the carry push,
            exactly as it gates serving.
        dht / lan: the runtime's DHTService / LANDiscovery (either None).
        manual_peers: explicit `scheme://host:port` entries, tried first.
        skip_addrs: () -> addresses the DHT may list that are not peers
            worth probing (ourselves through the router, LAN peers by their
            external address, UPnP-mapped Docker ports).
        diag_record: (kind, detail) -> None, executor-safe; a failed run
            becomes a `sync.failed` incident.
        after_run: awaited as a task after every run (the launcher fetches
            MB slices for what the run minted).
        on_enriched_count: told the enriched-artist count after a run that
            imported (the launcher's LAN beacon advertises it).
        """
        self.db_dsn = db_dsn
        self.dht = dht
        self.lan = lan
        self.manual_peers = list(manual_peers or [])
        self._identity = identity
        self._sharing_enabled = sharing_enabled
        self._skip_addrs = skip_addrs
        self._diag_record = diag_record
        self._after_run = after_run
        self._on_enriched_count = on_enriched_count
        self._unreachable: dict[str, tuple[float, int]] = {}   # addr -> (retry_after, strikes)
        self._holdings_cache: dict = {}                        # peer pubkey -> published holdings filters
        self._empty_peers: dict[str, float] = {}               # addr -> ignore until
        # Reachable peers met in the current run, (pubkey, host, port): the
        # capture side of the network-size estimate (network_size).
        self._run_sightings: list[tuple[str, str, int]] = []
        self._gap_count = 0                                    # last incomplete-set size
        self._reasons: set[str] = set()                        # why the queued run was asked for
        self._notify: Optional[asyncio.Event] = None
        self._lock: Optional[asyncio.Lock] = None
        self.first_source: Optional[asyncio.Event] = None      # LAN peer seen / DHT ready
        self._running = False

    # ------------------------------------------------------------ lifecycle

    def bind(self) -> None:
        """Create the loop-bound primitives — on the loop that runs the tasks."""
        self._notify = asyncio.Event()
        self._lock = asyncio.Lock()
        self.first_source = asyncio.Event()
        self._running = True
        if self._reasons:            # a request queued before the loop existed
            self._notify.set()

    def stop(self) -> None:
        self._running = False
        if self._notify:
            self._notify.set()

    @property
    def running(self) -> bool:
        return self._running

    async def db(self, fn, *args):
        """Run a blocking helper of this module with this walk's DSN."""
        return await asyncio.get_event_loop().run_in_executor(
            None, fn, self.db_dsn, *args)

    # ------------------------------------------------------------- triggers

    def request(self, reason: str) -> None:
        """Queue a run and remember why (loop thread). Concurrent reasons
        merge into the one run the dispatcher starts next."""
        self._reasons.add(reason)
        if self._notify:
            self._notify.set()

    async def dispatch_loop(self) -> None:
        """On a queued request, run once, labelled with the merged reasons."""
        while self._running:
            try:
                await self._notify.wait()
                self._notify.clear()
            except asyncio.CancelledError:
                break
            if not self._running:
                break
            reasons = "+".join(sorted(self._reasons)) or "request"
            self._reasons.clear()
            await self.run_once(trigger=reasons)

    async def interval_loop(self) -> None:
        """Periodic runs on sync.auto_interval_min, re-read each cycle so a
        settings change applies at the next interval. The first run waits
        for the first SOURCE — a LAN peer's beacon or the DHT bootstrap sets
        `first_source` — capped by FIRST_SYNC_MAX_DELAY for a node with
        neither."""
        try:
            await asyncio.wait_for(self.first_source.wait(),
                                   timeout=FIRST_SYNC_MAX_DELAY)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            return

        while self._running:
            interval_min = await self.db(read_auto_sync_interval)
            if interval_min and interval_min > 0:
                await self.run_once(trigger="auto")
                sleep_for = interval_min * 60
            else:
                sleep_for = 60  # re-check the setting every minute when disabled
            try:
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                break

    async def run_once(self, trigger: str) -> None:
        """One run, serialised: concurrent triggers merge into the run in
        progress rather than queueing behind it. Persists sync.last_at and
        sync.last_items_received (success or failure) and fires
        sautium_sync_done; a failure is a diag incident."""
        if self._lock.locked():
            logger.debug(f"P2P sync already in progress, skipping {trigger} trigger")
            return

        loop = asyncio.get_event_loop()
        async with self._lock:
            started = datetime.now(timezone.utc)
            logger.info(f"P2P sync starting (trigger={trigger})")
            try:
                stats = await self.run(trigger=trigger)
            except Exception as e:
                logger.error(f"P2P sync failed: {e}", exc_info=True)
                stats = {"error": str(e)}
                if self._diag_record:
                    await loop.run_in_executor(
                        None, self._diag_record, "sync.failed",
                        {"trigger": trigger, "error": str(e)[:500]})

            items = sum(v for v in stats.values() if isinstance(v, int))
            await loop.run_in_executor(
                None, write_sync_status, self.db_dsn, started, items)
            logger.info(f"P2P sync complete (trigger={trigger}): "
                        f"{items} items, stats={stats}")

        if self._after_run:
            asyncio.create_task(self._after_run())

    # ------------------------------------------------------------- the walk

    async def run(self, progress_cb: Optional[Callable[[str], None]] = None,
                  trigger: str = "auto") -> dict:
        """One walk over every tier. Returns per-category import counts."""

        def _progress(msg):
            logger.info(msg)
            if progress_cb:
                progress_cb(msg)

        # Step 1: the gap set — artists missing data in ANY category.
        # Inventory + _compute_needed inside the peer sync handle the
        # per-category filtering, so a wide trigger here costs only one
        # inventory round-trip per peer when nothing new exists.
        _progress("Finding artists needing sync...")
        incomplete = await self.db(incomplete_artist_uuids)
        # New gaps make the "had nothing for us" notes stale: a scan or a
        # listen added names the same peers may well have.
        if (len(incomplete) > self._gap_count
                or "request" in trigger or "lan-peer" in trigger):
            self._empty_peers.clear()
        self._gap_count = len(incomplete)
        if not incomplete:
            _progress("All artists fully synced!")
            return {"status": "all_synced"}

        _progress(f"Found {len(incomplete)} artists with missing data")

        # Step 2: the CORE (engaged artists and their tracks) and the
        # phantom BULK — both priced against each peer's holdings filter.
        gaps = await self.db(core_and_bulk, incomplete)
        core_tracks, _core_artists, bulk_tracks, bulk_artists = gaps

        if not core_tracks and not bulk_tracks:
            _progress("No tracks found for unenriched artists")
            return {"status": "no_tracks"}

        _progress(
            f"Need enrichment for {len(core_tracks)} core tracks"
            + (f" + {len(bulk_tracks)} phantom-bulk tracks of {len(bulk_artists)} artists"
               if bulk_tracks else "")
        )

        total_stats: dict = {}

        def _add_stats(synced: dict) -> int:
            items = 0
            for k, v in synced.items():
                if isinstance(v, int):
                    total_stats[k] = total_stats.get(k, 0) + v
                    items += v
            return items

        # Internet-tier discovery starts NOW and overlaps the LAN work: the
        # node-key lookup waits out its whole collection window, and the
        # directory is one HTTPS call — by the time the LAN peers are
        # drained both answers are usually in hand.
        dht_nodes_task = None
        if self.dht and self.dht.is_available:
            dht_nodes_task = asyncio.create_task(self._lookup_nodes_safe())
        directory_task = asyncio.get_event_loop().run_in_executor(
            None, node_hints.fetch, "sync")

        # Step 3: manual peers first (an explicit address beats discovery).
        for peer_addr in self.manual_peers:
            _add_stats(await self._sync_from_peer(
                peer_addr, gaps, _progress, progress_cb))

        # Step 4: LAN peers (fast, works behind any NAT)
        if self.lan:
            lan_peers = self.lan.peers
            if lan_peers:
                _progress(f"Found {len(lan_peers)} LAN peers")
                # Richer peers first
                lan_peers_sorted = sorted(
                    lan_peers,
                    key=lambda p: (self.lan.get_peer_info(*p) or {}).get("artists", 0),
                    reverse=True,
                )
                for ip, port in lan_peers_sorted:
                    info = self.lan.get_peer_info(ip, port)
                    artist_count = (info or {}).get("artists", "?")
                    scheme = (info or {}).get("scheme", "https")
                    peer_url = f"{scheme}://{fmt_addr(ip, port)}"
                    _progress(f"LAN peer {peer_url} ({artist_count} artists)...")
                    _add_stats(await self._sync_from_peer(
                        peer_url, gaps, _progress, progress_cb, is_lan=True,
                    ))

        # Step 5: the internet tiers — only for what the LAN left behind.
        #
        # Flow: nodes on the discovery key and the directory's volunteers,
        # probed concurrently, drained one at a time (each node's inventory
        # answers for the whole library at once); then the residual rare
        # artists through their own keys.
        dht_seen: set[str] = set()       # addresses already considered this run
        skip_dht_addrs: set[str] = set(self._skip_addrs()) if self._skip_addrs else set()
        if skip_dht_addrs:
            logger.debug(f"DHT skip list: {skip_dht_addrs}")

        now = time.time()

        def _fresh(addrs) -> list[str]:
            """Addresses worth a probe: not us, not a LAN peer, not already
            considered this run, not backing off, not noted empty-handed."""
            out = []
            for ip, port in addrs:
                addr = fmt_addr(ip, port)
                if addr in dht_seen or addr in skip_dht_addrs:
                    continue
                dht_seen.add(addr)
                retry_after, _ = self._unreachable.get(addr, (0.0, 0))
                if retry_after > now or self._empty_peers.get(addr, 0.0) > now:
                    continue
                out.append(addr)
            return out

        async def _drain(reachable) -> None:
            """Ask each reachable peer about everything still missing, one
            at a time: the inventory call does the matching, and the gap set
            shrinks after every peer, so parallel pulls would only
            duplicate."""
            for addr, api in reachable:
                remaining = await self.db(unenriched_artist_uuids)
                if not remaining:
                    return
                gaps = await self.db(core_and_bulk, remaining)
                if not gaps[0] and not gaps[2]:
                    return
                _progress(f"Asking {addr} about {len(gaps[0])} core + "
                          f"{len(gaps[2])} bulk tracks...")
                synced = await self._sync_from_peer(
                    addr, gaps, _progress, progress_cb, peer_api=api,
                )
                if not _add_stats(synced) and "error" not in synced:
                    # Reachable, answered, had nothing for us — the residue
                    # is elsewhere by definition; leave it alone until our
                    # gaps change.
                    self._empty_peers[addr] = now + EMPTY_PEER_TTL

        # Tier A: ONE node-key lookup plus the directory's volunteers —
        # awaited whether or not anything is missing, because the reply is
        # also this run's network-size sample: the tail announces follow the
        # verdict, and they serve OTHER nodes.
        nodes = await dht_nodes_task if dht_nodes_task else []
        try:
            volunteers = await directory_task
        except Exception as e:
            logger.warning(f"Directory lookup failed: {e}")
            volunteers = []
        sample = [(ip, port) for ip, port in nodes
                  if fmt_addr(ip, port) not in skip_dht_addrs]
        remaining = await self.db(unenriched_artist_uuids)
        candidates = _fresh(
            list(nodes) + [(h, p) for h, p, _ in (volunteers or [])])
        if remaining and not candidates and not dht_seen:
            # Nothing anywhere (dead DHT, empty directory): the master
            # hint is the last tier, validated like any other node.
            hint = await asyncio.get_event_loop().run_in_executor(
                None, master_hint.fetch)
            if hint:
                candidates = _fresh([hint])
        if not remaining:
            _progress("Nothing left to pull — measuring the network only")
        if candidates:
            _progress(f"Probing {len(candidates)} nodes...")
            reachable = await self._probe_candidates(candidates)
            if remaining:
                await _drain(reachable)
        rare_mode = await self._measure_network(
            len(nodes), sample, node_hints.total("sync"))

        # Tier B: engaged artists still missing analysis — exact keys
        # against the tails peers announce, a rotating slice (the same
        # unfindable names must not be asked for every run while the rest
        # never is). Only once the network is too big for tier A to have
        # listed every holder: below that a per-artist key can only name a
        # node the inventory round already asked.
        if remaining and dht_nodes_task is not None:
            residual = await self.db(rare_search_uuids) if rare_mode else []
            if residual:
                probe = await self._residual_slice(residual)
                _progress(
                    f"Searching DHT for {len(probe)} of {len(residual)} "
                    f"rare artists..."
                )
                peer_map = await self.dht.lookup_artists_batch(probe)
                found: list[tuple[str, int]] = []
                for peers in peer_map.values():
                    found.extend(peer for peer in peers if peer not in found)
                candidates = _fresh(found)
                if candidates:
                    await _drain(await self._probe_candidates(candidates))
            elif not rare_mode:
                _progress("Rare-artist keys held: the node key still lists "
                          "the whole network")

        # Sync may have enriched artists that now belong in the rare tail
        # (paced — background task, the sync result must not wait for it).
        if total_stats and self.dht:
            tail = await self.db(announce_tail_uuids)
            if tail:
                _progress("Re-announcing the rare-artist tail...")
                asyncio.create_task(self.dht.announce_artists(tail))
            if self._on_enriched_count:
                self._on_enriched_count(len(await self.db(enriched_artist_uuids)))

        total_items = sum(v for v in total_stats.values() if isinstance(v, int))
        _progress(f"P2P sync complete: {total_items} items synced")
        return total_stats

    async def _lookup_nodes_safe(self) -> list[tuple[str, int]]:
        try:
            return await self.dht.lookup_nodes()
        except Exception as e:
            logger.warning(f"DHT node lookup failed: {e}")
            return []

    # ---------------------------------------------------------- peer access

    async def connect_peer(self, peer_addr: str, is_lan: bool = False,
                           ) -> Optional[BackendAPIClient]:
        """A working client for a peer, or None. `scheme://host:port` is
        tried as given; a bare `host:port` is HTTPS only — every peer
        surface serves TLS (uvicorn or the master's Caddy front), and only
        TLS can carry the node-key binding peer clients require (the old
        plain-HTTP fallback was a downgrade an on-path impostor could
        force). LAN peers get one retry: the first connection from a fresh
        process can fail on OS-level cold start. Our own address — a DHT
        lookup lists us too — answers with our own key and is never a
        peer."""
        loop = asyncio.get_event_loop()
        peer_identity = self._identity()

        async def _healthy(api: BackendAPIClient) -> bool:
            health = await loop.run_in_executor(None, api.get_health)
            if not health:
                return False
            if (peer_identity is not None and api.peer_pubkey
                    and api.peer_pubkey.lower() == peer_identity.pubkey.lower()):
                logger.debug(f"{api.base_url} is this node's own address — skipped")
                return False
            return True

        if "://" in peer_addr:
            api = BackendAPIClient(peer_addr, peer=peer_identity)
            attempts = 2 if is_lan else 1
            for attempt in range(attempts):
                if await _healthy(api):
                    return api
                if attempt == 0 and is_lan:
                    logger.info(f"  LAN peer {peer_addr} not reachable, retrying in 5s...")
                    await asyncio.sleep(5)
            return None

        api = BackendAPIClient(f"https://{peer_addr}", peer=peer_identity)
        return api if await _healthy(api) else None

    async def _probe_candidates(
        self, addrs: list[str],
    ) -> list[tuple[str, BackendAPIClient]]:
        """Health-probe candidates concurrently — sequentially every dead
        address cost its full 5 s timeout, N of them per run — and keep the
        answering ones in the order they came. Draining stays sequential."""
        sem = asyncio.Semaphore(PROBE_CONCURRENCY)

        async def probe(addr: str):
            async with sem:
                api = await self.connect_peer(addr)
            if api is None:
                self._note_unreachable(addr)
            else:
                self._unreachable.pop(addr, None)
                self._note_sighting(api)
            return addr, api

        results = await asyncio.gather(*(probe(a) for a in addrs))
        return [(addr, api) for addr, api in results if api is not None]

    def _note_unreachable(self, addr: str) -> None:
        """Back a dead address off: 30 min, doubling to a day."""
        _, strikes = self._unreachable.get(addr, (0.0, 0))
        delay = min(UNREACHABLE_BACKOFF_BASE * (2 ** strikes),
                    UNREACHABLE_BACKOFF_MAX)
        self._unreachable[addr] = (time.time() + delay, strikes + 1)

    def _note_sighting(self, api: BackendAPIClient) -> None:
        """A peer that answered /health on a verified channel: one capture
        for the network-size ledger (network_size.record_sightings)."""
        parts = urlsplit(api.base_url)
        if api.peer_pubkey and parts.hostname and parts.port:
            self._run_sightings.append(
                (api.peer_pubkey, parts.hostname, parts.port))

    async def _measure_network(
        self, reply_size: int, sample: list[tuple[str, int]],
        directory_total: Optional[int],
    ) -> bool:
        """This run's network-size measurement (desktop/p2p/network_size.py):
        the DHT sample against the ledger of nodes met before, the
        directory's count as a floor. The verdict gates the tail both ways
        — announced and searched — and is persisted so a restart starts
        from it. Returns the rare mode."""
        sightings, self._run_sightings = self._run_sightings, []
        if not sample and directory_total is None:
            return bool(self.dht and self.dht.tail_enabled)

        def _blocking() -> tuple[int, bool]:
            conn = _connect(self.db_dsn)
            try:
                marked = network_size.ledger_size(conn)
                sample_set = {(canon_host(h), int(p)) for h, p in sample}
                known_addr = network_size.known_addresses(conn, sample_set)
                probed = {}
                for pubkey, host, port in sightings:
                    key = (canon_host(host), int(port))
                    if key in sample_set:
                        probed[key] = pubkey.lower()
                # A known node at a new address is a recapture, not a
                # newcomer — matched by key once the probe named it.
                moved = network_size.known_pubkeys(
                    conn, [pk for hp, pk in probed.items()
                           if hp not in known_addr])
                live = known_addr | set(probed)
                recaptured = len(known_addr) + sum(
                    1 for hp, pk in probed.items()
                    if hp not in known_addr and pk in moved)
                network_size.record_sightings(conn, sightings)
                estimate = network_size.estimate(
                    marked, len(live), recaptured,
                    exhaustive=reply_size < network_size.DHT_REPLY_CAP)
                if directory_total is not None:
                    estimate = max(estimate, directory_total)
                with conn.cursor() as cur:
                    cur.execute("SELECT value FROM user_settings WHERE key = %s",
                                ("p2p.rare_mode",))
                    row = cur.fetchone()
                mode = network_size.rare_mode(estimate, bool(row and row[0]))
            finally:
                conn.close()
            write_settings(self.db_dsn, {
                "p2p.network_estimate": estimate,
                "p2p.rare_mode": mode,
            })
            return estimate, mode

        estimate, mode = await asyncio.get_event_loop().run_in_executor(
            None, _blocking)
        if self.dht:
            self.dht.set_tail_enabled(mode)
        logger.info(
            f"Network size: ~{estimate} nodes (DHT sample {len(sample)}, "
            f"directory {directory_total if directory_total is not None else '—'}) "
            f"— rare-artist keys {'on' if mode else 'held'}"
        )
        return mode

    async def _residual_slice(self, residual: list[str]) -> list[str]:
        """The next _DHT_TAIL_PROBE names of the (ordered) residual, from a
        cursor kept in user_settings so it survives restarts and walks the
        whole tail over successive runs."""
        cursor = await self.db(read_setting, "sync.residual_cursor")
        start = int(cursor or 0) % len(residual)
        probe = (residual[start:] + residual[:start])[:_DHT_TAIL_PROBE]
        await self.db(write_settings, {
            "sync.residual_cursor": (start + len(probe)) % len(residual)})
        return probe

    async def _sync_from_peer(
        self,
        peer_addr: str,
        gaps: tuple[list[str], list[str], list[str], list[str]],
        _progress,
        progress_cb,
        is_lan: bool = False,
        peer_api: Optional[BackendAPIClient] = None,
    ) -> dict:
        """Sync enrichment data from a single peer. Returns stats dict.
        `gaps`: (core tracks, core artists, bulk tracks, bulk artists) —
        see core_and_bulk and SyncClient.run_sync. `peer_api`: a client
        the caller already probed — skips the probe."""
        core_tracks, core_artists, bulk_tracks, bulk_artists = gaps
        if peer_api is None:
            _progress(f"Connecting to {peer_addr}...")
            peer_api = await self.connect_peer(peer_addr, is_lan=is_lan)
            if peer_api:
                self._note_sighting(peer_api)
        if not peer_api:
            _progress(f"  {peer_addr} not reachable, skipping")
            return {"error": "unreachable"}

        _progress(f"Syncing from {peer_addr} ({len(core_tracks)} core + "
                  f"{len(bulk_tracks)} bulk tracks)...")

        sync_client = SyncClient(
            api_client=peer_api,
            db_dsn=self.db_dsn,
            batch_size=500,
            progress_cb=progress_cb,
            holdings_cache=self._holdings_cache,
        )

        try:
            stats = await asyncio.get_event_loop().run_in_executor(
                None,
                partial(sync_client.run_sync, core_tracks,
                        bulk_tracks, bulk_artists, core_artists),
            )
        except Exception as e:
            logger.error(f"Sync from {peer_addr} failed: {e}")
            _progress(f"  Sync from {peer_addr} failed: {e}")
            return {"error": str(e)}

        # We just pulled from this peer, so it accepts inbound connections —
        # which is exactly what makes it a candidate carrier. Offer it our
        # own first-hand canon material: nobody can pull from a node behind
        # CGNAT, so pushing is the only way its analysis ever reaches the
        # network. Never fatal to the sync that just succeeded.
        if "carry" in sync_client.peer_capabilities and self._sharing_enabled():
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, partial(self._push_to_carrier, peer_api, _progress))
            except Exception as e:
                logger.debug(f"Carry offer to {peer_addr} failed: {e}")
        return stats

    def _push_to_carrier(self, peer_api, _progress) -> int:
        """Offer our first-hand canon audio analysis to a reachable peer and
        push whatever it asks for. Blocking — runs in the executor.

        v4: the offer speaks recording MBIDs, the answer speaks the
        CARRIER's track uuids — existing rows only, so its phantom
        catalogue acts as the taste filter and nothing is minted remotely.
        We serve only the wanted uuids we also hold (the pull handlers do
        that naturally), which is exactly the set where the seals'
        track-uuid binding survives re-serve."""
        conn = _connect(self.db_dsn)
        try:
            candidates = sync_queries.get_pushable_tracks(
                conn, sync_queries.CARRY_MAX_TRACKS)
            recordings = sorted({
                mbid
                for c in candidates
                for mbid in (c.get("recordings") or [])
            })
            if not recordings:
                return 0
            answer = peer_api.carry_offer(recordings) or {}
            wanted = answer.get("wanted") or {}
            if not any(wanted.values()):
                return 0

            pushed = 0

            def push(category: str, payload: dict) -> int:
                if not payload.get("items"):
                    return 0
                res = peer_api.carry_push(category.replace("_", "-"), payload)
                return (res or {}).get("imported") or 0

            for category, batch in (("segments", SEGMENT_PULL_BATCH),
                                    ("audio_features", 500),
                                    ("track_mbids", 500)):
                uuids = wanted.get(category) or []
                handler = sync_queries.PULL_HANDLERS[category]
                for i in range(0, len(uuids), batch):
                    took = push(category, handler(conn, uuids[i:i + batch]))
                    pushed += took
                    if took:
                        _progress(
                            f"  carried {took} {category} record(s) to peer")

            if pushed:
                logger.info("Push-seeded %d record(s) to a carrier", pushed)
            return pushed
        finally:
            conn.close()
