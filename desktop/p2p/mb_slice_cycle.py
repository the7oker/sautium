"""The MusicBrainz slice cycle — the requester side of the MB dump family, one
implementation for both runtimes.

Until 2026-09-24 only the launcher's P2PManager asked for MB slices, so a
dump-less Docker node had no MB data but what an artist page's click-to-mint
fetched for one artist: its canon could not run, `mb.search_sources` was never
written (remote MB search and click-to-mint had no source), and the Last.fm
history import could not place a scrobble whose artist it had never seen. The
cycle now lives here, like the ListenBrainz one (desktop/p2p/lb_slice_cycle.py),
and each runtime injects its services: the walk's connect, its DHT, the LAN
tier where there is one, and what "hand the facts to the canon" means for it —
the backend's POST /canonicalize from the launcher, the in-process trigger in
the backend itself.

What a run does (`MbSliceCycle.run`):

1. nothing, when this node holds the full dump (it serves, it does not ask);
2. sources (desktop/p2p/slice_sources.py): manual peers, LAN peers, every
   holder of the DHT `mbdump` key, and — when those yield no usable source —
   the Worker directory's `mbslices` / `mbdump` volunteers with the master
   hint LAST (Ф16c); each probed once through the walk's connect, banned
   addresses and keys skipped, REPLICAS BEFORE DUMP NODES, the verified set
   kept between runs until a source fails; a new map is persisted as
   `mb.search_sources` for the backend's request-time consumers and
   announced with NOTIFY sautium_mb_sources;
3. the pending names in priority order (mb_slice_queries.pending_slice_names:
   owned artists, then the names imported scrobbles wait on, then the rest);
4. source by source, batches through MBSliceClient (verified against the
   original author's key); a replica's `missing` and outright failures carry
   to the next source; a rate-limited source is waited out once;
5. after an importing run: ANALYZE, then `after_import` (the canon trigger);
   `mb_slice.status` published on every run, an all-clear included;
6. a run that filled its cap with names of the first two tiers — the owner's
   files, the imported scrobbles — asks for the next run at once, while the
   phantom-stub tiers keep the timed pace so dump nodes are not hammered for
   discovery stubs.

Blocking DB work runs in the executor (`psycopg2.connect` blocks the loop for
the whole handshake — see sync_walk).
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

import psycopg2

from desktop.api_client import BackendAPIClient
from desktop.mb_slice_client import MBSliceClient
from desktop.p2p import mb_slice_queries
from desktop.p2p.lb_slice_cycle import load_bans, notify, read_interval
from desktop.p2p.slice_sources import SourceFinder
from desktop.p2p.sync_walk import write_settings

logger = logging.getLogger(__name__)

PENDING_PER_CYCLE = 200
INTERVAL_DEFAULT_MIN = 360           # mirrors config_manager DEFAULT_CONFIG["mb_slice"]
INTERVAL_KEY = "mb_slice.auto_interval_min"
STATUS_KEY = "mb_slice.status"
SOURCES_KEY = "mb.search_sources"
FIRST_SOURCE_MAX_DELAY = 60          # like the walk: wait for a source, not a clock
FIRST_RUN_DELAY = 120                # let the first sync walk spend the peers' windows first
_DRAIN_TIER = 1                      # tiers 0-1: owned files, imported scrobbles


def _connect(dsn: str):
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    return conn


def local_dump_available(dsn: str):
    """Full dump in THIS node's DB (the VERSION marker AND mb_artist rows on
    our DSN) — on a dev host the Docker loader stamps VERSION through the repo
    bind-mount while the launcher's embedded PG has empty mb_*."""
    conn = _connect(dsn)
    try:
        return mb_slice_queries.local_dump_available(conn)
    finally:
        conn.close()


def pending_names(dsn: str, limit: int) -> list:
    conn = _connect(dsn)
    try:
        return mb_slice_queries.pending_slice_names(conn, limit=limit)
    finally:
        conn.close()


class MbSliceCycle:
    """One node's MB slice requester. Construct once per process, `bind()` on
    the event loop that will run `dispatch_loop`/`interval_loop`, then
    `request(reason)` from anywhere on that loop."""

    def __init__(
        self,
        db_dsn: str,
        *,
        connect: Callable[[str], Awaitable[Optional[BackendAPIClient]]],
        after_import: Callable[[], None],
        config: Optional[dict] = None,
        dht=None,
        lan=None,
        manual_peers: Optional[list[str]] = None,
        load_bans_fn: Optional[Callable[[], tuple[set, set]]] = None,
        diag_record: Optional[Callable[[str, dict], None]] = None,
        first_source: Optional[Callable[[], Optional[asyncio.Event]]] = None,
    ):
        """
        connect: async (addr) -> BackendAPIClient | None — the walk's
            connect_peer: health, the TLS-pinned key, the own-address guard.
        after_import: () -> None, executor-safe — hand the freshly imported
            facts to the canon (the launcher's POST /canonicalize, the
            backend's in-process trigger).
        config: the `mb_slice` block (fetch / auto_interval_min).
        dht / lan: the runtime's DHTService / LANDiscovery (either None).
        manual_peers: explicit `scheme://host:port` entries, tried first.
        load_bans_fn: () -> (pubkeys, addr uuids); the DB list by default.
        diag_record: (kind, detail) -> None, executor-safe.
        first_source: () -> the walk's first-source event, so the first
            timed run waits for a source.
        """
        self.db_dsn = db_dsn
        self.config = dict(config or {})
        self._sources = SourceFinder(
            "MB slice", capability="mbdump", directory=("mbslices", "mbdump"),
            usable=lambda h: bool(h.get("mb_dump") or h.get("mb_slices")),
            connect=connect, dht=dht, lan=lan, manual_peers=list(manual_peers or []),
            load_bans=load_bans_fn or (lambda: load_bans(self.db_dsn)),
            addr_uuid=mb_slice_queries.addr_uuid)
        self._after_import = after_import
        self._diag_record = diag_record
        self._first_source = first_source
        self._reasons: set[str] = set()
        self._notify: Optional[asyncio.Event] = None
        self._lock: Optional[asyncio.Lock] = None
        self._probe_lock: Optional[asyncio.Lock] = None
        self._next_at: Optional[float] = None
        self._running = False

    # ------------------------------------------------------------ lifecycle

    def bind(self) -> None:
        self._notify = asyncio.Event()
        self._lock = asyncio.Lock()
        self._probe_lock = asyncio.Lock()
        self._running = True
        if self._reasons:
            self._notify.set()

    def stop(self) -> None:
        self._running = False
        if self._notify:
            self._notify.set()

    @property
    def running(self) -> bool:
        return self._running

    @property
    def dht(self):
        return self._sources.dht

    @dht.setter
    def dht(self, dht) -> None:
        """The backend starts its DHT after the cycle exists."""
        self._sources.dht = dht

    # ------------------------------------------------------------- triggers

    def request(self, reason: str) -> None:
        """Queue a run and remember why (loop thread). Concurrent reasons
        merge into the one run the dispatcher starts next."""
        self._reasons.add(reason)
        if self._notify:
            self._notify.set()

    async def dispatch_loop(self) -> None:
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
        """The fallback cadence and the retry path after peer failures, on
        mb_slice.auto_interval_min re-read each cycle. The first run waits for
        a source (the walk's event) and then the first sync walk's head start.
        The deadline of the next timed run is published with every status: it
        is the one honest answer to "when will the albums appear"."""
        evt = self._first_source() if self._first_source else None
        if evt is not None:
            try:
                await asyncio.wait_for(evt.wait(), timeout=FIRST_SOURCE_MAX_DELAY)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                return
        self._next_at = time.time() + FIRST_RUN_DELAY
        try:
            await asyncio.sleep(FIRST_RUN_DELAY)
        except asyncio.CancelledError:
            return
        default = int(self.config.get("auto_interval_min", INTERVAL_DEFAULT_MIN))
        loop = asyncio.get_event_loop()
        while self._running:
            interval_min = await loop.run_in_executor(
                None, read_interval, self.db_dsn, default, INTERVAL_KEY)
            if interval_min and interval_min > 0:
                self._next_at = time.time() + interval_min * 60
                await self.run_once(trigger="auto")
                sleep_for = max(1.0, self._next_at - time.time())
            else:
                self._next_at = None
                sleep_for = 300   # disabled — re-check the setting later
            try:
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                break

    async def run_once(self, trigger: str) -> None:
        """One run, serialised: concurrent triggers merge into the run in
        progress rather than queueing behind it."""
        if self._lock.locked():
            logger.debug(f"MB slice cycle already in progress, skipping {trigger}")
            return
        loop = asyncio.get_event_loop()
        async with self._lock:
            try:
                stats = await self.run(trigger)
                if stats:
                    logger.info(f"MB slice run done (trigger={trigger}): {stats}")
            except Exception as e:
                logger.error(f"MB slice cycle failed: {e}", exc_info=True)
                if self._diag_record:
                    await loop.run_in_executor(
                        None, self._diag_record, "mb_slice.failed",
                        {"trigger": trigger, "error": str(e)[:500]})

    async def refresh_sources(self) -> None:
        """On-demand source re-probe (the backend NOTIFYs when a remote search
        exhausted every known source — the 'dump node disappeared' moment);
        a full-dump node has nothing to probe."""
        if self._probe_lock.locked():
            return
        async with self._probe_lock:
            loop = asyncio.get_event_loop()
            if await loop.run_in_executor(None, local_dump_available, self.db_dsn):
                return
            self._sources.forget()
            await self.find_sources()

    # -------------------------------------------------------------- sources

    async def find_sources(self) -> list[tuple[BackendAPIClient, str]]:
        """Reachable slice sources as (client, node_id), REPLICAS FIRST: a
        dump-less peer with mb_slices > 0 re-serves the blobs it verified —
        asking those first spreads the load off the few dump nodes; misses
        fall through to a dump holder. A newly probed set is persisted for
        the backend's request-time consumers (remote MB search,
        click-to-mint); desktop/p2p/slice_sources.py keeps it between runs."""
        sources, probed = await self._sources.find()
        replicas = [(api, node) for api, node, health in sources if not health.get("mb_dump")]
        dumps = [(api, node) for api, node, health in sources if health.get("mb_dump")]
        if probed:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, write_settings, self.db_dsn, {
                SOURCES_KEY: [{"url": api.base_url, "kind": "replica"} for api, _ in replicas]
                             + [{"url": api.base_url, "kind": "dump"} for api, _ in dumps]})
            # The backend's mb-sources listener → {"t": "mb"} → the Discovery
            # chip flips live (searching → online/disabled).
            await loop.run_in_executor(None, notify, self.db_dsn, "sautium_mb_sources")
        return replicas + dumps

    # -------------------------------------------------------------- the run

    async def run(self, trigger: str = "auto") -> dict:
        loop = asyncio.get_event_loop()
        if not self.config.get("fetch", True):
            return {}
        if await loop.run_in_executor(None, local_dump_available, self.db_dsn):
            return {}
        # Source discovery runs BEFORE the pending-names early return: it also
        # persists mb.search_sources for the backend's remote MB search, which
        # a dump-less node needs even when canon has no work (2026-08-10).
        peers = await self.find_sources()
        pending = await loop.run_in_executor(None, pending_names, self.db_dsn, PENDING_PER_CYCLE)
        if not pending:
            await self._publish(pending=0, served=0, unserved=0, reason="idle", sources=len(peers))
            return {}
        if not peers:
            logger.info(f"MB slice: {len(pending)} pending names, no source reachable")
            await self._publish(pending=len(pending), served=0, unserved=len(pending),
                                reason="no_sources", sources=0)
            return {}

        names = [name for name, _ in pending]
        batch_size = mb_slice_queries.MAX_NAMES_PER_REQUEST
        logger.info(f"MB slice: {len(names)} pending names, {len(peers)} source(s) (replicas first)")
        total = {"names": 0, "matched": 0, "rows_inserted": 0}
        imported_any = False
        last_client = None
        remaining = list(names)
        reasons: set[str] = set()
        for api, node in peers:
            if not remaining:
                break
            client = MBSliceClient(api, db_dsn=self.db_dsn, source_node=node)
            leftovers: list[str] = []
            waited = False
            for i in range(0, len(remaining), batch_size):
                batch = remaining[i:i + batch_size]
                stats = await loop.run_in_executor(None, client.run, batch)
                if "retry_after" in stats and not waited:
                    # The peer's per-IP window is full — usually our own sync
                    # walk just spent it. Wait that out ONCE per source and
                    # re-ask; parking the batch meant the next timed cycle.
                    wait = min(int(stats.get("retry_after") or 60), 120)
                    logger.info(f"MB slice: {node} rate-limited — re-asking in {wait}s")
                    waited = True
                    await asyncio.sleep(wait)
                    stats = await loop.run_in_executor(None, client.run, batch)
                if "error" in stats:
                    leftovers.extend(batch)
                    if "retry_after" in stats:
                        # Still limited after the wait: every further batch
                        # on this source would 429 too.
                        reasons.add("rate_limited")
                        leftovers.extend(remaining[i + batch_size:])
                        break
                    reasons.add("error")
                    self._sources.drop(node)
                    continue
                imported_any = True
                for k in total:
                    total[k] += stats.get(k, 0)
                leftovers.extend(stats.get("missing") or [])
            if imported_any and last_client is not client:
                if last_client is not None:
                    last_client.close()
                last_client = client
            else:
                client.close()
            remaining = leftovers
        if remaining:
            logger.info(f"MB slice: {len(remaining)} name(s) not served this cycle — they stay pending")
        if imported_any and last_client is not None:
            await loop.run_in_executor(None, last_client.finalize)
            await loop.run_in_executor(None, self._after_import)
        served = len(names) - len(remaining)
        await self._publish(
            pending=len(names), served=served, unserved=len(remaining),
            reason=("rate_limited" if "rate_limited" in reasons
                    else "error" if "error" in reasons
                    else "missing" if remaining else "ok"),
            sources=len(peers))
        if (len(pending) >= PENDING_PER_CYCLE and served
                and any(tier <= _DRAIN_TIER for _, tier in pending)):
            self.request("drain")
        return total

    async def _publish(self, *, pending: int, served: int, unserved: int, reason: str,
                       sources: int) -> None:
        """One row, `mb_slice.status`, is the whole of what the UI knows about
        this cycle: how many names canon waits on, how many a peer served,
        why the rest stayed pending and when the timed loop asks again.
        Written on every cycle — an all-clear included — so the condition the
        backend derives from it (`mb_slice.deferred`) ends the moment it stops
        being true."""
        state = {
            "at": datetime.now(timezone.utc).isoformat(),
            "pending": pending,
            # the pending set is capped per cycle: at the cap the true
            # backlog is unknown, and the copy says "200+".
            "pending_capped": pending >= PENDING_PER_CYCLE,
            "served": served,
            "unserved": unserved,
            "reason": reason,
            "sources": sources,
            "next_attempt_at": (datetime.fromtimestamp(self._next_at, timezone.utc).isoformat()
                                if self._next_at else None),
        }
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, write_settings, self.db_dsn, {STATUS_KEY: state})
            await loop.run_in_executor(None, notify, self.db_dsn, "sautium_notices")
        except Exception as e:
            logger.warning(f"MB slice: status publish failed: {e}")
