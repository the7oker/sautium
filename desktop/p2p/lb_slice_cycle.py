"""The ListenBrainz slice cycle — the requester side of the second dump
family, one implementation for both runtimes.

The launcher's P2PManager runs it beside the sync walk; the Docker backend
runs the same object with its own services injected (the walk's
`connect_peer`, its DHT, no LAN tier). The MB family's cycle
(desktop/p2p/mb_slice_cycle.py) has had the same shape since 2026-09-24, and
both find their sources through desktop/p2p/slice_sources.py (CLAUDE.md: what
both surfaces must agree on exactly lives in desktop/p2p and is imported,
never copied).

What a run does (`LbSliceCycle.run`):

1. nothing, when this node holds the dump (it serves, it does not ask);
2. sources (desktop/p2p/slice_sources.py): manual peers, LAN peers, every
   holder of the DHT `lbdump` key, and — when those yield no usable source —
   the Worker directory's `lbslices` / `lbdump` volunteers with the master
   hint LAST (Ф16c de-specialization); each probed once through the walk's
   connect (health + pinned key + own-address guard), banned addresses and
   keys skipped, REPLICAS BEFORE DUMP NODES, the verified set kept between
   runs until a source fails. Every source's `/health` says which dump
   version it serves or re-serves, and the newest of those is what the
   ledger is measured against;
3. the pending set (lb_slice_queries.pending_slice_mbids): the on-demand
   lane whole, and a random draw from the owned and engaged artists, minus
   ledger rows at the newest version — a row older than that is STALE and
   asked again with `min_version`, so counts refresh and signed zero-matches
   re-open; each request mixes them in random order, so it does not tell
   an opened page from an owned or a listened artist;
4. an opened artist page's MBIDs first, at once and of the dump nodes before
   the replicas, mixed into PAGE_MBIDS; then source by source, one request
   every PACKET_GAP at most, of up to 50 MBIDs (the protocol's maximum) — a
   page opened while a run waits out the pace ends that run for the next:
   every entry verified against the
   ORIGINAL author's key, imported in one transaction per batch under the
   loader's lock; `missing` (the peer does not hold it) and DROPPED (it sent
   something that does not verify) both carry to the next source — two
   explicit sets, never one expression (desktop/mb_slice_client.py's
   precedence bug is what that invites);
5. `NOTIFY sautium_lb_done` after every importing batch — the artist page
   that asked re-fetches its own snapshot; `lb_slice.status` published on
   every run, including an all-clear, so the derived notice ends the moment
   it stops being true.

Blocking DB work runs in the executor (`psycopg2.connect` blocks the loop
for the whole handshake — see sync_walk).
"""

import asyncio
import json
import logging
import random
import threading
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

import psycopg2

from desktop.api_client import PEER_SESSION_S, BackendAPIClient
from desktop.p2p import lb_slice_queries
from desktop.p2p.slice_sources import SourceFinder
from desktop.p2p.sync_walk import DeadAddresses

logger = logging.getLogger(__name__)

PENDING_PER_CYCLE = 200
INTERVAL_DEFAULT_MIN = 360           # mirrors config_manager DEFAULT_CONFIG["lb_slice"]
INTERVAL_KEY = "lb_slice.auto_interval_min"
STATUS_KEY = "lb_slice.status"
FIRST_SOURCE_MAX_DELAY = 60          # like the walk: wait for a source, not a clock
FIRST_RUN_DELAY = 120                # let the first sync walk spend the peers' windows first
# The stream's one pace, as in the MB family (mb_slice_cycle.PACKET_GAP): a
# request of at most MAX_MBIDS_PER_REQUEST (50) MBIDs to a source every
# PACKET_GAP — two thirds of what a dump node builds for one requester in a
# minute (lb_slice_queries.BuildBudget: 300 node-wide, 75 each), so no burst
# spends the budget and comes back `missing`, and no event's artists leave
# as a burst of their own. An artist page opened is the person's act: its
# MBIDs go at once, the first time a run asks for them, mixed into a request
# of PAGE_MBIDS, in the third left.
PACKET_GAP = 60.0
PAGE_MBIDS = 25


# ---------------------------------------------------------------------------
# Blocking DB helpers — one short-lived autocommit connection each, always
# called through the executor from coroutine code.
# ---------------------------------------------------------------------------

def _connect(dsn: str):
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    return conn


_checks_conn: dict = {}
_checks_lock = threading.Lock()


def _checks(dsn: str):
    """The autocommit connection the per-batch author and source checks
    share, for the process's life — not one a batch. Serialized: the
    executor may run two batches' checks at once."""
    conn = _checks_conn.get(dsn)
    if conn is None or conn.closed:
        conn = _checks_conn[dsn] = _connect(dsn)
    return conn


def read_interval(dsn: str, default: int, key: str = INTERVAL_KEY) -> Optional[int]:
    """A slice cycle's auto_interval_min (this family's by default): the
    default without a row, None when the row says null (explicitly
    disabled)."""
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM user_settings WHERE key = %s", (key,))
            row = cur.fetchone()
        if row is None:
            return default
        if row[0] is None:
            return None
        return int(row[0]) if row[0] else None
    finally:
        conn.close()


def load_bans(dsn: str) -> tuple[set, set]:
    """The keys and addresses a slice cycle does not ask: the local ban list
    (pubkeys, addr uuids — the pubkey is the anchor, the address id catches a
    banned key returning under a fresh identity), the keys whose proof failed
    here (their ban rows may be evicted past the cap) and the slice lanes' own
    penalties, which bind keys only (desktop/p2p/slice_trust.py)."""
    from desktop.p2p import slice_trust
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT pubkey, addr::text FROM p2p_node_bans
                           UNION ALL SELECT pubkey, NULL FROM p2p_identities WHERE status = 'failed'""")
            rows = cur.fetchall()
        keys = {r[0] for r in rows if r[0]} | slice_trust.penalized_keys(conn)
        return keys, {r[1] for r in rows if r[1]}
    finally:
        conn.close()


def published_newest(dsn: str, family: str):
    """MetaBrainz's newest published version of `family` and when its listing
    was read (desktop/p2p/published.py, at most once a day), with every kept
    blob from a later version judged a falsification (P1)."""
    from desktop.p2p import published, slice_checks
    conn = _connect(dsn)
    try:
        newest = published.newest(conn, family)
        if newest is not None:
            slice_checks.published_check(conn, family, newest[0], newest[1])
        return newest
    finally:
        conn.close()


def vet_authors(dsn: str, verified: list, relay: str) -> list:
    """The verified entries whose author this node takes: no penalty, mailbox
    not burned, identity recorded (and a `pow` proof that `relay`, the key the
    channel proved, passed on evaluated when the node can afford it). `verified` items end with
    (certificate, proof)."""
    from desktop.p2p import slice_trust
    with _checks_lock:
        conn = _checks(dsn)
        kept = []
        for item in verified:
            cert, proof = item[-2], item[-1]
            if slice_trust.refused_author(conn, cert):
                continue
            if slice_trust.observe_author(conn, cert, proof, relay) in (None, "failed"):
                continue
            kept.append(item)
        return kept


def source_refused(dsn: str, relay: str) -> bool:
    from desktop.p2p import slice_checks
    with _checks_lock:
        return slice_checks.source_refused(_checks(dsn), relay)


def local_dump_available(dsn: str) -> Optional[str]:
    conn = _connect(dsn)
    try:
        return lb_slice_queries.local_dump_available(conn)
    finally:
        conn.close()


def _requests(remaining: dict, opened: set, newest: Optional[str]) -> list:
    """(lane, min_version, MBIDs) for one source's paced requests, in the
    order asked: the fresh lane first — never-fetched artists have no counts
    at all, and an opened page's MBIDs no source answered yet lead it —, then
    the stale, in requests of MAX_MBIDS_PER_REQUEST, each in random order."""
    size = lb_slice_queries.MAX_MBIDS_PER_REQUEST
    fresh = ([m for m in remaining["fresh"] if m in opened]
             + [m for m in remaining["fresh"] if m not in opened])
    out = []
    for lane, min_version, group in (("fresh", None, fresh), ("stale", newest, remaining["stale"])):
        for i in range(0, len(group), size):
            batch = group[i:i + size]
            random.shuffle(batch)
            out.append((lane, min_version, batch))
    return out


def page_requests_waiting(dsn: str) -> set:
    """The MBIDs of the artist pages that asked and wait, none fetched yet."""
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT DISTINCT am.mbid::text FROM lb_slice_requests r
                             JOIN artist_mbids am ON am.artist_id = r.artist_id
                            WHERE NOT EXISTS (SELECT 1 FROM lb_slice_fetches f
                                               WHERE f.artist_mbid = am.mbid)""")
            return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def pending_slice_mbids(dsn: str, limit: int, newest: Optional[str]) -> list:
    conn = _connect(dsn)
    try:
        return lb_slice_queries.pending_slice_mbids(conn, limit, newest)
    finally:
        conn.close()


def import_batch(dsn: str, source_node: str, source_addr: Optional[str],
                 verified: list, relay: Optional[str] = None) -> int:
    """One transaction for a batch of verified slices, under the loader's
    lock (a full load must never interleave with an import). The penalties
    are read again inside it: a verdict that committed while the batch was
    verified, or the lock awaited, lands nothing of the key it penalized."""
    from desktop.p2p import slice_checks, slice_trust
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (lb_slice_queries.LB_LOAD_LOCK_KEY,))
        try:
            with conn.cursor() as cur:
                cur.execute("BEGIN")
            if slice_checks.source_refused(conn, relay):
                with conn.cursor() as cur:
                    cur.execute("ROLLBACK")
                return 0
            penalized = slice_trust.penalized_keys(conn)
            recordings = 0
            for mbid, core, blob_gz, entry, *_cert in verified:
                if (entry.get("author_pubkey") or "").lower() in penalized:
                    continue
                recordings += lb_slice_queries.import_slice(
                    conn, mbid, core, blob_gz, entry, source_node, source_addr, relay)
            with conn.cursor() as cur:
                cur.execute("COMMIT")
            return recordings
        except Exception:
            with conn.cursor() as cur:
                cur.execute("ROLLBACK")
            raise
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (lb_slice_queries.LB_LOAD_LOCK_KEY,))
    finally:
        conn.close()


def analyze_tables(dsn: str) -> None:
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("ANALYZE lb_recording")
            cur.execute("ANALYZE lb_artist")
    finally:
        conn.close()


def notify(dsn: str, channel: str) -> None:
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(f"NOTIFY {channel}")
    finally:
        conn.close()


def clear_status(dsn: str, key: str) -> None:
    """Drop a slice cycle's status row on a node that now holds the dump. The
    notice is gone already — routers/settings._notices_state ignores the row
    once the family's dump marker is set — but a status published before the
    load would come back as a deferral the day the dump is deleted."""
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_settings WHERE key = %s", (key,))
    finally:
        conn.close()


def publish_status(dsn: str, key: str, state: dict) -> None:
    """A slice cycle's status row (`mb_slice.status` / `lb_slice.status`),
    shared by both families. `since` is when the current deferral BEGAN —
    kept while artists stay unserved, new once they start waiting again —
    because the derived notice toasts whenever its `since` moves: stamped
    per run, the same "looking for a node" toast came back on every wake,
    every few seconds while a Last.fm history import grew the MBID set
    (2026-10-07). An unchanged row is not rewritten and wakes nobody — the
    NOTIFY says a condition moved, not that a run happened. The cycle is the
    row's only writer, one run at a time (its dispatcher's): a dump loader
    just wakes the notices channel, since a node holding a family's dump shows
    no deferral for it (routers/settings._notices_state)."""
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM user_settings WHERE key = %s", (key,))
            row = cur.fetchone()
            prev = row[0] if row else None
            since = None
            if state["unserved"]:
                since = (prev or {}).get("since") or datetime.now(timezone.utc).isoformat()
            value = {**state, "since": since}
            if value == prev:
                return
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
            cur.execute("NOTIFY sautium_notices")
    finally:
        conn.close()


class LbSliceCycle:
    """One node's LB slice requester. Construct once per process, `bind()`
    on the event loop that will run `dispatch_loop`/`interval_loop`, then
    `request(reason)` from anywhere on that loop."""

    def __init__(
        self,
        db_dsn: str,
        *,
        connect: Callable[[str], Awaitable[Optional[BackendAPIClient]]],
        dead: DeadAddresses,
        sharing_enabled: Callable[[], bool],
        has_identity: Callable[[], bool],
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
        dead: the walk's record of dead discovered addresses (`walk.dead`).
        sharing_enabled: the "P2P sharing" switch — off, the cycle asks no
            peer and probes no source, as the walk does.
        has_identity: the walk's has_identity — while this node has no
            identity the cycle probes no source and asks no peer, as the
            walk does not (sync_walk.SyncWalk.has_identity says why).
        config: the `lb_slice` block (fetch / auto_interval_min).
        dht / lan: the runtime's DHTService / LANDiscovery (either None).
        manual_peers: explicit `scheme://host:port` entries, tried first.
        load_bans_fn: () -> (pubkeys, addr uuids); the DB list by default.
        diag_record: (kind, detail) -> None, executor-safe.
        first_source: () -> the walk's first-source event (created when the
            walk binds), so the first timed run waits for a source.
        """
        self.db_dsn = db_dsn
        self.config = dict(config or {})
        self._sharing_enabled = sharing_enabled
        self._has_identity = has_identity
        self._sources = SourceFinder(
            "LB slice", capability="lbdump", directory=("lbslices", "lbdump"),
            # A node whose owner turned sharing off answers no slice request:
            # it is never told the MBIDs.
            usable=lambda h: bool(h.get("lb_dump") or h.get("lb_slices"))
                             and h.get("sharing") is not False,
            connect=connect, dead=dead, dht=dht, lan=lan,
            manual_peers=list(manual_peers or []),
            load_bans=load_bans_fn or (lambda: load_bans(self.db_dsn)),
            addr_uuid=lb_slice_queries.addr_uuid)
        self._diag_record = diag_record
        self._first_source = first_source
        self._reasons: set[str] = set()
        self._notify: Optional[asyncio.Event] = None
        self._next_at: Optional[float] = None
        self._status_cleared = False       # a dump node dropped its row this process
        self._published: Optional[str] = None   # MetaBrainz's newest LB dump, per run
        self._running = False
        self._asked_at: dict[str, float] = {}   # source -> when this node last asked it
        self._page = asyncio.Event()            # an artist page asked since the run began
        self._paged: set[str] = set()           # page MBIDs already asked at once
        self._timed = False                     # the timer's first run was asked for

    # ------------------------------------------------------------ lifecycle

    def bind(self) -> None:
        self._notify = asyncio.Event()
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
        merge into the one run the dispatcher starts next; an artist page's
        (`request`) also ends a run that waits out the pace, so the page is
        asked at once."""
        self._reasons.add(reason)
        if reason == "request":
            self._page.set()
        if self._notify:
            self._notify.set()

    async def pages_waiting(self) -> None:
        """Whenever the notifications are listened for again: an artist page
        that asked while no one listened, and was not asked at once yet, is
        asked now — a page wakes the cycle once, for its new request alone.
        Not before the timer's first run, which reads every waiting page
        itself once a source can be found (a run at the first LISTEN would
        find none but the fallback's)."""
        if not self._timed:
            return
        loop = asyncio.get_event_loop()
        if await loop.run_in_executor(None, page_requests_waiting, self.db_dsn) - self._paged:
            self.request("request")

    async def request_async(self, reason: str) -> None:
        """The walk's `after_run` shape."""
        self.request(reason)

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
        """Periodic runs on lb_slice.auto_interval_min, re-read each cycle.
        The first run waits for a source (the walk's event) and then the
        first sync walk's head start."""
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
            interval_min = await loop.run_in_executor(None, read_interval, self.db_dsn, default)
            if interval_min and interval_min > 0:
                self._next_at = time.time() + interval_min * 60
                # Through the dispatcher like every other trigger: one run at a
                # time, and a trigger during a run — an opened page's — is never
                # dropped for it.
                self._timed = True
                self.request("auto")
                sleep_for = interval_min * 60
            else:
                self._next_at = None
                sleep_for = 300   # disabled — re-check the setting later
            try:
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                break

    async def run_once(self, trigger: str) -> None:
        """One run — the dispatcher's alone, one at a time: the triggers that
        come during it merge into the one run after it (before 2026-11-14 the
        interval loop ran its own and a trigger meanwhile was dropped)."""
        loop = asyncio.get_event_loop()
        try:
            stats = await self.run(trigger)
            if stats:
                logger.info(f"LB slice run done (trigger={trigger}): {stats}")
        except Exception as e:
            logger.error(f"LB slice cycle failed: {e}", exc_info=True)
            if self._diag_record:
                await loop.run_in_executor(
                    None, self._diag_record, "lb_slice.failed",
                    {"trigger": trigger, "error": str(e)[:500]})

    # -------------------------------------------------------------- sources

    async def find_sources(self, published: Optional[str] = None
                           ) -> tuple[list[tuple[BackendAPIClient, str, Optional[str]]],
                                      Optional[str]]:
        """Reachable slice sources as (client, node_id, version), REPLICAS
        FIRST, and the newest dump version: the newest a reachable source
        serves that is not later than what MetaBrainz has published
        (`published`, its own listing — before the listing was read, no
        later than a day ahead). The newest version decides what every node
        asks again, so it is never a stranger's word alone: a version past
        MetaBrainz's is a falsification (P1), not news."""
        sources, _ = await self._sources.find()
        replicas: list = []
        dumps: list = []
        newest: Optional[str] = None
        for api, node_id, health in sources:
            dump_version = health.get("lb_dump") or None
            held_version = health.get("lb_slices_version") or None
            for v in (dump_version, held_version):
                if (lb_slice_queries.plausible_version(v) and (published is None or v <= published)
                        and (newest is None or v > newest)):
                    newest = v
            if dump_version:
                dumps.append((api, node_id, dump_version))
            else:
                replicas.append((api, node_id, held_version))
        return replicas + dumps, newest

    # ------------------------------------------------------------- the run

    async def run(self, trigger: str = "auto") -> dict:
        loop = asyncio.get_event_loop()
        if not self.config.get("fetch", True):
            return {}
        if not self._has_identity():
            return {}
        if await loop.run_in_executor(None, local_dump_available, self.db_dsn):
            # A dump node serves; it has nothing to ask for — and nothing to
            # report: the row from before its load goes, once per process,
            # not on every wake.
            if not self._status_cleared:
                await loop.run_in_executor(None, clear_status, self.db_dsn, STATUS_KEY)
                self._status_cleared = True
            return {}
        if not self._sharing_enabled():
            # A node that does not give does not take: no source is probed
            # and no MBID leaves; the status says how many wait, and why.
            pending = await loop.run_in_executor(
                None, pending_slice_mbids, self.db_dsn, PENDING_PER_CYCLE, None)
            await self._publish(pending=len(pending), served=0, unserved=len(pending),
                                reason="sharing_off" if pending else "idle", sources=0,
                                newest=None)
            return {}

        self._page.clear()
        listing = await loop.run_in_executor(None, published_newest, self.db_dsn, "lb")
        self._published = listing[0] if listing else None
        sources, newest = await self.find_sources(self._published)
        pending = await loop.run_in_executor(
            None, pending_slice_mbids, self.db_dsn, PENDING_PER_CYCLE, newest)
        if not pending:
            await self._publish(pending=0, served=0, unserved=0, reason="idle",
                                sources=len(sources), newest=newest)
            return {}
        if not sources:
            logger.info(f"LB slice: {len(pending)} pending, no source reachable")
            await self._publish(pending=len(pending), served=0, unserved=len(pending),
                                reason="no_sources", sources=0, newest=newest)
            return {}

        # Stale rows (a ledger version older than the newest reachable) are
        # asked with min_version, so a replica's older cache is a miss, not
        # an answer; never-fetched artists take the first data they can get
        # and are re-asked as stale next run if it was older.
        remaining = {"stale": [m for m, v, _ in pending if v is not None],
                     "fresh": [m for m, v, _ in pending if v is None]}
        for group in remaining.values():
            random.shuffle(group)
        # An opened page's MBIDs go at once the first time a run asks for
        # them; asked again (no source had them), they ride the paced
        # requests like the rest.
        opened = {m for m, _, o in pending if o}
        self._paged &= opened
        page = opened - self._paged
        self._paged |= page
        logger.info(f"LB slice: {len(pending)} pending ({len(remaining['stale'])} stale), "
                    f"{len(sources)} source(s), newest {newest} (trigger={trigger})")

        total = {"artists": 0, "recordings": 0}
        reasons: set[str] = set()
        imported_any, lacked = await self._ask_page(sources, remaining, page, total)
        for api, node, _ in sources:
            if not (remaining["stale"] or remaining["fresh"]) or reasons & {"sharing_off", "page"}:
                break
            source_addr = lb_slice_queries.addr_uuid(api.base_url)
            api.session_deadline = time.monotonic() + PEER_SESSION_S
            # What this source said it lacks when the page was asked goes on to
            # the next untold.
            skip = lacked.get(node, set())
            leftovers = {"stale": [], "fresh": [m for m in remaining["fresh"] if m in skip]}
            here = {"stale": remaining["stale"], "fresh": [m for m in remaining["fresh"] if m not in skip]}
            waited = limited = failed = False
            for lane, min_version, batch in _requests(here, opened, newest):
                if not reasons & {"sharing_off", "page"} and await self._paced(node):
                    # An artist page opened meanwhile: the run that follows
                    # asks for it at once; what this one had left waits for it.
                    reasons.add("page")
                if not self._sharing_enabled():
                    # Turned off while the run went on: nothing more leaves.
                    reasons.add("sharing_off")
                # A session that is over has not failed: what is left
                # stays pending for the next source or cycle.
                if (limited or failed or reasons & {"sharing_off", "page"}
                        or time.monotonic() >= api.session_deadline):
                    leftovers[lane].extend(batch)
                    continue
                self._asked_at[node] = time.monotonic()
                result = await self._isolated(api, node, source_addr, batch, min_version)
                if result is None:
                    reasons.add("error")
                    failed = True
                    leftovers[lane].extend(batch)
                    continue
                if result.get("retry_after") is not None and not waited:
                    # The peer's per-IP window is full — usually our own
                    # sync walk just spent it. Wait it out ONCE per
                    # source and re-ask instead of parking the batch.
                    wait = min(int(result.get("retry_after") or 60), 120)
                    logger.info(f"LB slice: {node[:16]} rate-limited — re-asking in {wait}s")
                    waited = True
                    await asyncio.sleep(wait)
                    if not self._sharing_enabled():
                        reasons.add("sharing_off")
                    if "sharing_off" in reasons or time.monotonic() >= api.session_deadline:
                        leftovers[lane].extend(batch)
                        continue
                    self._asked_at[node] = time.monotonic()
                    result = await self._isolated(api, node, source_addr, batch, min_version)
                    if result is None:
                        reasons.add("error")
                        failed = True
                        leftovers[lane].extend(batch)
                        continue
                if "error" in result and time.monotonic() >= api.session_deadline:
                    # The session ended under this batch, not the source.
                    leftovers[lane].extend(batch)
                    continue
                if "error" in result:
                    leftovers[lane].extend(batch)
                    if result.get("retry_after") is not None:
                        reasons.add("rate_limited")
                        limited = True
                    else:
                        reasons.add("error")
                        self._sources.drop(node)
                    continue
                if result["imported"]:
                    imported_any = True
                total["artists"] += result["imported"]
                total["recordings"] += result["recordings"]
                leftovers[lane].extend(result["missing"])
            remaining = leftovers

        unserved = len(remaining["stale"]) + len(remaining["fresh"])
        if imported_any:
            await loop.run_in_executor(None, analyze_tables, self.db_dsn)
        if "page" in reasons:
            # The run asking for the page follows at once and says how things
            # stand; what this one left is no miss.
            return total
        if unserved:
            logger.info(f"LB slice: {unserved} artist(s) not served this cycle — they stay pending")
        await self._publish(
            pending=len(pending), served=len(pending) - unserved, unserved=unserved,
            reason=("sharing_off" if "sharing_off" in reasons
                    else "rate_limited" if "rate_limited" in reasons
                    else "error" if "error" in reasons
                    else "missing" if unserved else "ok"),
            sources=len(sources), newest=newest)
        return total

    async def _ask_page(self, sources: list, remaining: dict, page: set,
                        total: dict) -> tuple[bool, dict]:
        """An opened page's MBIDs, asked at once, of the dump nodes before the
        replicas — a replica rarely holds the artist a person just opened —,
        each source once until every one is answered, mixed with fresh MBIDs
        into PAGE_MBIDS where the backlog has them. What a source lacks goes on
        to the next; what none answered, and what a failure left (the paced
        requests judge the source), leads the paced requests. Returns whether
        anything was imported, and what each replica said it lacks (the paced
        requests do not ask it again; a dump node is)."""
        size = lb_slice_queries.MAX_MBIDS_PER_REQUEST
        unserved = [m for m in remaining["fresh"] if m in page][:size]
        filled: set = set()
        imported = False
        lacked: dict = {}
        if not unserved:
            return imported, lacked

        def is_dump(api) -> bool:
            return bool((api.last_health or {}).get("lb_dump"))

        # The dump nodes first, each tier in the order it was found (the
        # fallback's master hint last among them).
        for api, node, _ in ([s for s in sources if is_dump(s[0])]
                             + [s for s in sources if not is_dump(s[0])]):
            if not unserved or not self._sharing_enabled():
                break
            fill = [m for m in remaining["fresh"]
                    if m not in page and m not in filled][:max(0, PAGE_MBIDS - len(unserved))]
            filled.update(fill)
            batch = unserved + fill
            random.shuffle(batch)
            api.session_deadline = time.monotonic() + PEER_SESSION_S
            self._asked_at[node] = time.monotonic()
            result = await self._isolated(api, node, lb_slice_queries.addr_uuid(api.base_url), batch, None)
            if result is None or "error" in result:
                continue
            missing = set(result["missing"])
            if not is_dump(api):
                # A replica lacks what it never kept; a dump node's `missing` is
                # its budget's of the moment, and the paced requests ask again.
                lacked[node] = missing
            answered = set(batch) - missing
            remaining["fresh"] = [m for m in remaining["fresh"] if m not in answered]
            unserved = [m for m in unserved if m not in answered]
            imported = imported or bool(result["imported"])
            total["artists"] += result["imported"]
            total["recordings"] += result["recordings"]
        return imported, lacked

    async def _paced(self, node: str) -> bool:
        """Wait until this node may ask `node` again — PACKET_GAP after its
        last request there, across runs — or until an artist page asks first
        (True)."""
        wait = self._asked_at.get(node, float("-inf")) + PACKET_GAP - time.monotonic()
        if wait <= 0:
            return False
        try:
            await asyncio.wait_for(self._page.wait(), timeout=wait)
        except asyncio.TimeoutError:
            return False
        return True

    async def _isolated(self, api: BackendAPIClient, node: str, source_addr: Optional[str],
                        batch: list[str], min_version: Optional[str]) -> Optional[dict]:
        """`_exchange`, or None when it raised: one source's answer must not
        end the run for the others — the source is skipped for the rest of it."""
        try:
            return await self._exchange(api, node, source_addr, batch, min_version)
        except Exception as e:
            logger.warning("LB slice: source %s failed (%s) — skipped this run", node[:16], e,
                           exc_info=True)
            self._sources.drop(node)
            return None

    async def _exchange(self, api: BackendAPIClient, node: str, source_addr: Optional[str],
                        batch: list[str], min_version: Optional[str]) -> dict:
        """One request to one source: verify every entry against the
        ORIGINAL author's key, import the verified ones in one transaction.
        `missing` carries what the peer does not hold AND what it sent that
        does not verify; an all-dropped answer is an error (the source is
        skipped for the rest of this run)."""
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, api.lb_slice, batch, min_version)
        if not resp or "error" in resp or "detail" in resp:
            err = (resp or {}).get("error") or (resp or {}).get("detail") or "no response"
            logger.warning(f"LB slice fetch failed ({node[:16]}): {err}")
            out = {"error": err}
            if err == "slice_refused":
                # What the peer says this node did, each field of its kind: the
                # evidence in the node's log, its kind for support. A body that
                # is no refusal is not recorded; the source is dropped anyway.
                from desktop.p2p.slice_trust import refusal_of, refusal_report
                refused = refusal_of(resp)
                if refused is None:
                    return {"error": "malformed refusal"}
                logger.warning("LB slice: %s refuses this node's requests: %s", node[:16], refused)
                if self._diag_record:
                    await loop.run_in_executor(None, self._diag_record, "lb_slice.refused",
                                               refusal_report(refused))
            if "retry_after" in (resp or {}):
                out["retry_after"] = resp["retry_after"]
            return out
        if resp.get("v") != lb_slice_queries.PROTOCOL_VERSION:
            logger.warning(f"LB slice from {node[:16]}: incompatible protocol — rejected")
            return {"error": "incompatible slice protocol"}
        slices = resp.get("slices") or {}
        if not isinstance(slices, dict):
            return {"error": "malformed slice answer"}
        verified: list = []
        missing: list = []
        dropped = 0
        for mbid in batch:
            entry = slices.get(mbid)
            if not isinstance(entry, dict):
                missing.append(mbid)
                continue
            out = lb_slice_queries.verify_slice_entry(mbid, entry, min_version, self._published)
            if out is None:
                dropped += 1
                missing.append(mbid)
                logger.warning(f"LB slice for {mbid} from {node[:16]}: "
                               f"signature/identity/version check failed — dropped")
                continue
            verified.append((mbid, out[0], out[1], entry, out[2], out[3]))
        relay = (api.peer_pubkey or node or "").lower()
        if verified:
            # A refused source's proofs are not worth an evaluation.
            if await loop.run_in_executor(None, source_refused, self.db_dsn, relay):
                return {"error": "source carries a penalty"}
            # The verifier counts a proof under the key the channel proved
            # alone: the source list's claim could name the author itself.
            kept = await loop.run_in_executor(None, vet_authors, self.db_dsn, verified,
                                              (api.peer_pubkey or "").lower())
            dropped += len(verified) - len(kept)
            missing += [v[0] for v in verified if v not in kept]
            verified = kept
        if dropped and not verified:
            return {"error": "all slices failed verification"}
        recordings = 0
        if verified:
            recordings = await loop.run_in_executor(
                None, import_batch, self.db_dsn, node, source_addr, verified, relay)
            await loop.run_in_executor(None, notify, self.db_dsn, "sautium_lb_done")
        return {"imported": len(verified), "recordings": recordings,
                "missing": missing, "dropped": dropped}

    async def _publish(self, *, pending: int, served: int, unserved: int,
                       reason: str, sources: int, newest: Optional[str]) -> None:
        """One row, `lb_slice.status`, is the whole of what the UI knows
        about this cycle; published on every run — an all-clear included —
        so the derived notice (`lb_slice.deferred`) ends the moment it stops
        being true."""
        self._status_cleared = False
        next_at = self._next_at
        state = {
            "pending": pending,
            "pending_capped": pending >= PENDING_PER_CYCLE,
            "served": served,
            "unserved": unserved,
            "reason": reason,
            "sources": sources,
            "newest_version": newest,
            "next_attempt_at": (datetime.fromtimestamp(next_at, timezone.utc).isoformat()
                                if next_at else None),
        }
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, publish_status, self.db_dsn, STATUS_KEY, state)
        except Exception as e:
            logger.warning(f"LB slice: status publish failed: {e}")
