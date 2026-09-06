"""
P2P Sync client for Sautium.

Orchestrates enrichment data synchronization from a remote source
(backend API or future P2P peer) into the local database.

Protocol:
  1. Capabilities — GET /health, pick the audio category the peer speaks
  2. Inventory — ask source what enrichment data it has for our tracks
  3. Plan — determine what we need, split into batches
  4. Pull — fetch data batch by batch
  5. Import — verify seals, insert into local PostgreSQL

Audio analysis arrives as SEGMENTS with their seals (author signature →
Merkle proof → Worker timestamp, verified here before any write); the
track-level mean is derived locally — it is a local aggregate, never wire
data (see desktop/p2p/record_sig.py). Lyrics are deliberately NOT synced
(2026-07-11): the one category that is verbatim copyrighted text — every
node fetches its own from the public sources.
"""

import base64
import logging
import math
import struct
import time
from typing import Optional, Callable

import psycopg2
import psycopg2.extras

from desktop.api_client import BackendAPIClient
from desktop.p2p import record_sig as rs
from desktop.p2p.birth_cert import TRUSTED_AUTHORITIES
from desktop.p2p.bloom import BloomFilter
from desktop.p2p.sync_queries import CARRY_CATEGORIES, EMPTY_INVENTORY

logger = logging.getLogger(__name__)

# Categories and their pull endpoint names. Exactly one of segments/embeddings
# runs per sync: `segments` when the peer advertises the capability,
# `embeddings` as the legacy mean-vector fallback.
CATEGORIES = [
    # (category_key_in_inventory, pull_endpoint, uuid_column_hint)
    ("segments", "segments", "track"),
    ("embeddings", "embeddings", "track"),
    ("audio_features", "audio-features", "track"),
    ("track_stats", "track-stats", "track"),
    ("artist_bios", "artist-bios", "artist"),
    ("artist_tags", "artist-tags", "artist"),
    ("similar_artists", "similar-artists", "artist"),
    ("genre_descriptions", "genre-descriptions", "genre"),
]

# When an incoming enrichment record may replace the local one. The rules, in
# the order they matter:
#
#   1. Never overwrite first-hand data. What this node fetched itself outranks
#      anything a peer says, always — the alternative is a network where the
#      loudest relay rewrites your own observations.
#   2. A signed record beats an unsigned one. Attribution is the point; a claim
#      nobody stands behind should not displace one somebody does.
#   3. Otherwise the fresher fetch wins, on fetched_at — which is inside the
#      signed payload precisely so this comparison cannot be gamed.
#
# COALESCE on the local side so a legacy row with no fetched_at loses to a
# stamped one rather than making the comparison NULL and blocking every update.
_ENRICHMENT_PRECEDENCE = """
    {t}.imported
    AND (
        (EXCLUDED.signature IS NOT NULL AND {t}.signature IS NULL)
        OR EXCLUDED.fetched_at > COALESCE({t}.fetched_at, '-infinity'::timestamptz)
    )
"""

# Pull category -> record kind. Membership in this map is what makes a category
# verified; a new enrichment category is unverified until it appears here, which
# is the failure mode we want (visible, not silent).
_ENRICHMENT_KIND_BY_CATEGORY = {
    "track_stats": "track_stat",
    "artist_bios": "artist_bio",
    "artist_tags": "artist_tag",
    "similar_artists": "similar_artist",
    "genre_descriptions": "genre_description",
    "track_mbids": "track_mbid",
}

DEFAULT_BATCH_SIZE = 500
# A segments batch is K=12..24 vectors + proofs per track (~50KB) — keep the
# per-request payload in the single-digit-MB range.
SEGMENT_PULL_BATCH = 100
# Both server implementations reject inventory requests above 10k UUIDs —
# the client slices its library and merges the responses.
INVENTORY_CHUNK = 10_000

# A fetched holdings filter is used as-is for this long before the peer's
# version is even compared: a peer's version moves with every enrichment
# pass it runs (the counts of six tables are in it), and the syncs that
# follow a minting burst come minutes apart — refetching a filter that is
# 99.9% the same for each of them is the waste the filter exists to avoid.
# Staleness costs nothing but a delay: an element the peer gained since
# the fetch is missed until the next refresh, never answered wrongly.
HOLDINGS_MAX_AGE_S = 15 * 60


class SyncClient:
    """Synchronizes enrichment data from a remote source into local DB."""

    def __init__(
        self,
        api_client: Optional[BackendAPIClient],
        db_dsn: str,
        batch_size: int = DEFAULT_BATCH_SIZE,
        progress_cb: Optional[Callable[[str], None]] = None,
        holdings_cache: Optional[dict] = None,
    ):
        # api_client is None on the receiving side of a push: the payload
        # arrives instead of being fetched, but it goes through the same
        # verify-and-import gate (see import_pushed).
        self.api = api_client
        self.db_dsn = db_dsn
        self.batch_size = batch_size
        self.progress_cb = progress_cb
        # peer pubkey -> {"version", "tracks": BloomFilter, "artists":
        # BloomFilter}: the peers' published holdings, kept by the manager
        # across runs (process lifetime) and refreshed by version.
        self.holdings_cache = holdings_cache if holdings_cache is not None else {}
        self._conn: Optional[psycopg2.extensions.connection] = None
        # Filled by run_sync's capability probe; the caller reads it to
        # decide whether this peer can also be pushed to.
        self.peer_capabilities: set[str] = set()

    def _progress(self, msg: str):
        logger.info(msg)
        if self.progress_cb:
            self.progress_cb(msg)

    def _get_conn(self) -> psycopg2.extensions.connection:
        """Get or reuse a single DB connection."""
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self.db_dsn)
            self._conn.autocommit = False
        return self._conn

    def _close_conn(self):
        if self._conn and not self._conn.closed:
            self._conn.close()
            self._conn = None

    def _get_local_track_uuids(self) -> list[str]:
        """Get all track UUIDs from the local database."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT id::text FROM tracks ORDER BY id")
            return [row[0] for row in cur.fetchall()]

    # Both existence checks are bounded by the INVENTORY, never by the
    # table: a filled node holds millions of rows per category and a run
    # asks about a few thousand — reading the whole table per category,
    # per peer, per run was seconds of pure waste on every sync.

    def _get_existing_uuids(self, table: str, uuid_col: str,
                            candidates) -> set[str]:
        """Of `candidates`, the uuids that already have a local row."""
        candidates = list(candidates)
        if not candidates:
            return set()
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT DISTINCT {uuid_col}::text FROM {table} "
                            f"WHERE {uuid_col} = ANY(%s::uuid[])", [candidates])
                return {row[0] for row in cur.fetchall()}
        except psycopg2.errors.UndefinedTable:
            conn.rollback()
            return set()

    def _get_existing_versions(self, table: str, uuid_col: str,
                               candidates) -> dict[str, int]:
        """Of `candidates`, uuid -> local analysis_version."""
        candidates = list(candidates)
        if not candidates:
            return {}
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {uuid_col}::text, MAX(analysis_version) "
                            f"FROM {table} WHERE {uuid_col} = ANY(%s::uuid[]) "
                            f"GROUP BY {uuid_col}", [candidates])
                return {row[0]: row[1] for row in cur.fetchall()}
        except psycopg2.errors.UndefinedTable:
            conn.rollback()
            return {}

    def run_sync(self, track_uuids: list[str] = None,
                 bulk_track_uuids: Optional[list[str]] = None,
                 bulk_artist_uuids: Optional[list[str]] = None) -> dict:
        """
        Run full synchronization.

        Args:
            track_uuids: the CORE — tracks of engaged artists, asked of the
                peer in full through the exact inventory; None = every
                local track.
            bulk_track_uuids / bulk_artist_uuids: the phantom BULK — asked
                through the peer's holdings filter when that is the cheaper
                direction, so millions of gaps cost one filter download and
                a handful of exact questions about the hits.

        Returns:
            dict with sync statistics per category.
        """
        try:
            return self._run_sync_inner(track_uuids, bulk_track_uuids or [],
                                        bulk_artist_uuids or [])
        finally:
            self._close_conn()

    def _run_sync_inner(self, track_uuids: list[str],
                        bulk_track_uuids: list[str],
                        bulk_artist_uuids: list[str]) -> dict:
        # Step 1: Get track UUIDs
        if track_uuids is None:
            self._progress("Getting local track UUIDs...")
            track_uuids = self._get_local_track_uuids()

        if not track_uuids and not bulk_track_uuids and not bulk_artist_uuids:
            self._progress("No tracks to sync.")
            return {"total_tracks": 0}

        self._progress(
            f"Syncing enrichment for {len(track_uuids)} tracks"
            + (f" + {len(bulk_track_uuids)} phantom-bulk tracks"
               if bulk_track_uuids else "") + "...")

        # Step 2: Capability probe — which audio category does the peer speak?
        health = self.api.get_health() or {}
        self.peer_capabilities = set(health.get("capabilities") or [])
        use_segments = "segments" in self.peer_capabilities
        if not use_segments:
            self._progress(
                "  peer has no `segments` capability — legacy mean-vector pull")

        # Step 3: Inventory — ask source what it has (chunked + merged)
        inventory = dict(EMPTY_INVENTORY)
        if track_uuids:
            self._progress("Requesting inventory...")
            inventory = self._fetch_inventory(track_uuids)
            if not inventory:
                self._progress("Inventory request failed.")
                return {"error": "inventory_failed"}

            # A track the source does not hold is the normal case, not an
            # anomaly — on a node with the phantom layer it is millions of
            # rows per peer. One count is all the log needs.
            source_tracks = set(inventory.get("tracks", []))
            not_found = set(track_uuids) - source_tracks
            if not_found:
                self._progress(
                    f"  {len(not_found)} tracks not found at source"
                )

        if bulk_track_uuids or bulk_artist_uuids:
            bulk = self._bulk_inventory(health, bulk_track_uuids, bulk_artist_uuids)
            if bulk:
                for key, value in bulk.items():
                    if isinstance(value, list):
                        inventory.setdefault(key, []).extend(value)

        # Step 4: Filter out what we already have locally
        needed = self._compute_needed(inventory, use_segments)

        # Step 5: Pull and import each category in batches
        stats = {}
        inactive = {"embeddings"} if use_segments else {"segments"}
        for cat_key, pull_endpoint, _ in CATEGORIES:
            if cat_key in inactive:
                continue
            uuids_to_pull = needed.get(cat_key, [])
            if not uuids_to_pull:
                stats[cat_key] = 0
                continue

            imported = self._pull_and_import_category(
                cat_key, pull_endpoint, uuids_to_pull
            )
            stats[cat_key] = imported

        # Step 6: Recompute artist gender and vocalist status from imported bios
        if stats.get("artist_bios", 0) > 0:
            gender_updated = self._update_artist_gender(needed.get("artist_bios", []))
            if gender_updated:
                self._progress(f"Updated gender for {gender_updated} artists.")
            vocal_updated = self._update_artist_is_vocalist(needed.get("artist_bios", []))
            if vocal_updated:
                self._progress(f"Updated vocalist status for {vocal_updated} artists.")

        total = sum(stats.values())
        self._progress(f"Sync complete. Imported {total} items across {len(stats)} categories.")
        return stats

    def _fetch_inventory(self, track_uuids: list[str],
                         artist_uuids: Optional[list[str]] = None) -> Optional[dict]:
        """Inventory in ≤INVENTORY_CHUNK slices, merged. Chunks are disjoint
        track sets, so list values concatenate; artist/genre categories may
        repeat across chunks and are consumed as sets downstream. Artists
        named outright travel in their own chunks."""
        requests = [(track_uuids[i:i + INVENTORY_CHUNK], None)
                    for i in range(0, len(track_uuids), INVENTORY_CHUNK)]
        artist_uuids = artist_uuids or []
        requests += [([], artist_uuids[i:i + INVENTORY_CHUNK])
                     for i in range(0, len(artist_uuids), INVENTORY_CHUNK)]
        merged: dict = dict(EMPTY_INVENTORY)
        for i, (tracks, artists) in enumerate(requests, 1):
            t0 = time.monotonic()
            part = self.api.sync_inventory(tracks, artists)
            if not part or "tracks" not in part:
                return None
            for k, v in part.items():
                if isinstance(v, list):
                    merged.setdefault(k, []).extend(v)
            self._progress(
                f"  inventory {i}/{len(requests)}: {len(tracks)} tracks, "
                f"{len(artists or [])} artists in {time.monotonic() - t0:.1f}s")
        return merged

    def _bulk_inventory(self, health: dict, bulk_tracks: list[str],
                        bulk_artists: list[str]) -> Optional[dict]:
        """Inventory for the phantom bulk by the cheaper direction: the
        peer's holdings filter (one download, cached by version, tested
        locally) when the bulk is bigger than the filter, the exact
        inventory otherwise or when the peer has no filter. Returns the
        inventory for what is worth asking, None when nothing is."""
        if "holdings" not in self.peer_capabilities:
            self._progress(
                f"  bulk: peer has no holdings filter — asking about "
                f"{len(bulk_tracks)} tracks directly")
            return self._fetch_inventory(bulk_tracks, bulk_artists)

        key = self.api.peer_pubkey or self.api.base_url
        cached = self.holdings_cache.get(key)
        summary = health.get("holdings") or None
        fresh = cached and time.time() - cached["fetched_at"] < HOLDINGS_MAX_AGE_S
        if fresh or (cached and summary
                     and summary.get("version") == cached["version"]):
            filters = cached
        else:
            # Price the two directions: ~40 bytes per uuid on the wire
            # against ~1.2 bytes per held element in the filter.
            ask_cost = (len(bulk_tracks) + len(bulk_artists)) * 40
            if summary:
                fetch_cost = (summary.get("tracks", 0) + summary.get("artists", 0)) * 1.2
                if fetch_cost > ask_cost:
                    self._progress(
                        f"  bulk: {len(bulk_tracks)} gaps weigh less than the "
                        f"peer's filter — asking directly")
                    return self._fetch_inventory(bulk_tracks, bulk_artists)
            payload = self.api.sync_holdings(cached["version"] if cached else None)
            if not payload or "version" not in payload:
                self._progress("  holdings request failed — bulk skipped this run")
                return None
            if payload.get("unchanged") and cached:
                filters = cached
                filters["fetched_at"] = time.time()
            else:
                filters = {"version": payload["version"],
                           "fetched_at": time.time(),
                           "tracks": BloomFilter.from_dict(payload["tracks"]),
                           "artists": BloomFilter.from_dict(payload["artists"])}
                self.holdings_cache[key] = filters
                self._progress(
                    f"  holdings filter fetched: {filters['tracks'].n} tracks + "
                    f"{filters['artists'].n} artists held by the peer")

        hits_t = filters["tracks"].hits(bulk_tracks)
        hits_a = filters["artists"].hits(bulk_artists)
        self._progress(
            f"  holdings filter: {len(hits_t)} of {len(bulk_tracks)} tracks and "
            f"{len(hits_a)} of {len(bulk_artists)} artists worth asking")
        if not hits_t and not hits_a:
            return None
        return self._fetch_inventory(hits_t, hits_a)

    def _protected_analysis_tracks(self, uuids: list[str]) -> set:
        """Tracks whose local analysis a peer import must never replace:
        first-hand (my own decode) or already carrying segments. Gap-fill
        only — densification / version upgrades of imported grids are the
        deferred "deepen analysis" path."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT e.track_id::text FROM embeddings e
                   LEFT JOIN analysis_sources s ON s.id = e.analysis_source_id
                   WHERE e.track_id = ANY(%s::uuid[])
                     AND (EXISTS (SELECT 1 FROM embedding_segments es
                                  WHERE es.embedding_id = e.id)
                          OR (s.id IS NOT NULL AND NOT s.imported))""",
                [uuids],
            )
            return {row[0] for row in cur.fetchall()}

    def _compute_needed(self, inventory: dict, use_segments: bool) -> dict:
        """Compare inventory with local data to find what's missing."""
        emb_cat = "segments" if use_segments else "embeddings"
        # Map category to (table, uuid_column) for local existence check.
        # Both audio categories check the local `embeddings` table and read
        # the `embeddings` inventory key.
        local_check = {
            emb_cat: ("embeddings", "track_id"),
            "audio_features": ("audio_features", "track_id"),
            "track_stats": ("track_stats", "track_id"),
            "artist_bios": ("artist_bios", "artist_id"),
            "artist_tags": ("artist_tags", "artist_id"),
            "similar_artists": ("similar_artists", "artist_id"),
            "genre_descriptions": ("genre_descriptions", "genre_id"),
        }

        # Versioned categories: the inventory carries [uuid, analysis_version]
        # pairs — [uuid, analysis_version, segment_count] triples from
        # segments-capable peers — and a locally-present row is still pulled
        # when the source's methodology is newer: existence alone never
        # delivers upgrades.
        versioned = {emb_cat, "audio_features"}

        needed = {}
        for cat_key, (table, uuid_col) in local_check.items():
            t0 = time.monotonic()
            inv_key = "embeddings" if cat_key == "segments" else cat_key
            if cat_key in versioned:
                available_versions = {row[0]: row[1]
                                      for row in inventory.get(inv_key, [])}
                if not available_versions:
                    continue
                local = self._get_existing_versions(
                    table, uuid_col, available_versions)
                new = [u for u in available_versions if u not in local]
                outdated = [u for u, v in available_versions.items()
                            if u in local and local[u] < v]
                candidates = new + outdated
                if cat_key == "segments" and candidates:
                    protected = self._protected_analysis_tracks(candidates)
                    if protected:
                        self._progress(
                            f"  segments: {len(protected)} protected "
                            f"(own analysis) — not pulled")
                        candidates = [u for u in candidates
                                      if u not in protected]
                if candidates:
                    needed[cat_key] = candidates
                    self._progress(
                        f"  {cat_key}: {len(new)} new + {len(outdated)} outdated "
                        f"/ {len(available_versions)} available"
                    )
                self._note_slow(cat_key, t0)
                continue

            available = set(inventory.get(cat_key, []))
            if not available:
                continue

            existing = self._get_existing_uuids(table, uuid_col, available)
            missing = available - existing

            # Similars are ENGAGED-ONLY, mirroring the local enrichment rule
            # (lastfm.backfill_similar): an owned file OR a completed listen.
            # Importing an artist's similars mints a stub row for every
            # target, those stubs get canonized, their discographies mint
            # phantom tracks — and the next sync would then ask for THEIR
            # similars. Each hop multiplies by the ~50 entries of a Last.fm
            # list, so an ungated pull is a breadth-first walk of all
            # recorded music through this node's disk. Engagement keeps the
            # bound: both signals are linear in human behavior, and a minted
            # stub only becomes a seed via a new human listen.
            if cat_key == "similar_artists" and missing:
                engaged = self._engaged_artist_uuids(missing)
                skipped = len(missing) - len(engaged)
                missing = engaged
                if skipped:
                    self._progress(
                        f"  similar_artists: {skipped} skipped "
                        f"(no engagement — blowup guard)")

            if missing:
                needed[cat_key] = list(missing)
                self._progress(
                    f"  {cat_key}: {len(missing)} new / {len(available)} available"
                )
            self._note_slow(cat_key, t0)

        return needed

    def _note_slow(self, cat_key: str, t0: float) -> None:
        """A local existence check is a handful of indexed lookups; when it
        takes seconds the node's database is the bottleneck (a sync went
        silent for six minutes here on 2026-09-06) — say so, with the time."""
        dt = time.monotonic() - t0
        if dt > 2.0:
            self._progress(f"  {cat_key}: local check took {dt:.1f}s")

    def _engaged_artist_uuids(self, artist_uuids) -> set:
        """Of these artists, the ones this node owns a file by OR has a
        completed, unskipped listen of (streamed phantoms count)."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT ta.artist_id::text
                     FROM track_artists ta
                    WHERE ta.artist_id = ANY(%s::uuid[])
                      AND (EXISTS (SELECT 1 FROM media_files mf
                                    WHERE mf.track_id = ta.track_id)
                           OR EXISTS (SELECT 1 FROM listening_history lh
                                       WHERE lh.track_id = ta.track_id
                                         AND lh.completed AND NOT lh.skipped))""",
                [list(artist_uuids)])
            return {row[0] for row in cur.fetchall()}

    def _pull_and_import_category(
        self, cat_key: str, pull_endpoint: str, uuids: list[str]
    ) -> int:
        """Pull and import a single category in batches.

        Each batch is retried once after a 2s backoff. Empirically pull
        failures on localhost are transient — TLS handshake interrupts,
        gzip-middleware boundary cases on large audio_features payloads
        (the instruments+moods JSONB columns blow up the wire size),
        HMAC timestamp drift during a CPU stall, or uvicorn keepalive
        races. A single retry catches the bulk of them; persistent
        failure logs a warning and the broader incomplete-artist sync
        flow will pick up the gap on the next pass.
        """
        import time
        total_imported = 0
        batch_size = (SEGMENT_PULL_BATCH if cat_key == "segments"
                      else self.batch_size)
        batches = [
            uuids[i: i + batch_size]
            for i in range(0, len(uuids), batch_size)
        ]

        for batch_idx, batch_uuids in enumerate(batches, 1):
            self._progress(
                f"  {cat_key}: batch {batch_idx}/{len(batches)} "
                f"({len(batch_uuids)} items)..."
            )

            result = None
            for attempt in (1, 2):
                result = self.api.sync_pull(pull_endpoint, batch_uuids)
                if result and "items" in result:
                    break
                if attempt == 1:
                    logger.warning(
                        f"Pull {cat_key} batch {batch_idx} failed "
                        f"(attempt 1), retrying in 2s"
                    )
                    time.sleep(2)
            if not result or "items" not in result:
                logger.warning(
                    f"Pull {cat_key} batch {batch_idx} failed after retry, "
                    f"skipping {len(batch_uuids)} uuids"
                )
                continue

            items = result["items"]
            if not items:
                continue

            imported = self._import_items(cat_key, result)
            total_imported += imported

        return total_imported

    def _import_items(self, category: str, result: dict) -> int:
        """Import a pull response into the local database."""
        importer = getattr(self, f"_import_{category}", None)
        if not importer:
            logger.warning(f"No importer for category: {category}")
            return 0

        conn = self._get_conn()
        try:
            # Sealed categories also need the response-level Worker-timestamp
            # map to verify (and re-serve) the full chain.
            if category in ("segments", "audio_features"):
                count = importer(conn, result["items"],
                                 result.get("batches") or {})
            else:
                items = result["items"]
                # Enrichment verifies here rather than inside each importer:
                # one gate, five categories, and no way to add a sixth that
                # quietly skips it.
                kind = _ENRICHMENT_KIND_BY_CATEGORY.get(category)
                if kind:
                    items = self._verify_enrichment(
                        kind, items, result.get("batches") or {})
                    if not items:
                        return 0
                    self._insert_batches_for(conn, result.get("batches") or {},
                                             items)
                count = importer(conn, items)
            conn.commit()
            return count
        except Exception as e:
            conn.rollback()
            logger.error(f"Import {category} failed: {e}")
            # Support diagnostics: a category that never lands is invisible in
            # the run's totals ("0 items" reads as "nothing new"), so the
            # failure is recorded here, where it is caught.
            from desktop.config_manager import get_data_dir
            from desktop.p2p import diag_events
            diag_events.record_or_spool(
                self.db_dsn, get_data_dir(), "sync.import_failed",
                {"category": category, "error": str(e)[:500]})
            return 0

    # -- Seal verification (verify-on-import) -------------------------------

    # Which wire field names the entity a record hangs off.
    _ENRICHMENT_ENTITY = {
        "artist_bio": "artist_uuid",
        "artist_tag": "artist_uuid",
        "similar_artist": "artist_uuid",
        "track_stat": "track_uuid",
        "genre_description": "genre_uuid",
        "track_mbid": "track_uuid",
    }

    def _verify_enrichment(self, kind: str, items: list, batches: dict) -> list:
        """Keep only the records whose seal checks out.

        Unsigned records used to be accepted here on the grounds that a peer
        which has not signed yet is not a liar. That was wrong at the network
        level rather than the peer level: an unsigned record is one nobody can
        be held to, and this node would go on to REDISTRIBUTE it — so an
        unattributable claim would spread with our address on it. Enrichment
        the network carries has to be attributable end to end, which means a
        record with no seal is a record we do not take. Nodes sign their own
        material on the next sign_audio run, so this costs a peer nothing but
        a delay.

        A signature that does not check out was always dropped and still is:
        corrupt or forged, and there is no version of "accept it anyway" that
        leaves the seal meaning anything."""
        batch_ok = self._batch_verifier(batches or {})
        key = self._ENRICHMENT_ENTITY[kind]
        kept, dropped, unsigned = [], 0, 0
        for it in items:
            if not it.get("signature"):
                unsigned += 1
                continue
            try:
                content = rs.blake2b_hex(rs.canonical_enrichment_blob(kind, it))
                payload = rs.enrichment_payload(
                    it.get("author_pubkey") or "", kind, it.get(key) or "",
                    it.get("source") or "", content, it.get("fetched_at"))
            except (ValueError, KeyError):
                payload = None
            if not (payload
                    and rs.verify(payload, it["signature"],
                                  it.get("author_pubkey") or "")
                    and rs.verify_proof(rs.record_leaf(it["signature"]),
                                        it.get("merkle_proof") or [],
                                        it.get("batch_root") or "")
                    and batch_ok(it.get("batch_root"))):
                dropped += 1
                continue
            kept.append(it)
        if dropped:
            logger.warning("sync import: dropped %d %s record(s) — seal invalid",
                           dropped, kind)
        if unsigned:
            logger.warning("sync import: dropped %d unsigned %s record(s) — "
                           "the peer has not signed this material yet",
                           unsigned, kind)
        return kept

    def _insert_batches_for(self, conn, batches: dict, items: list) -> None:
        """Store the Worker timestamps the surviving seals reference, so this
        node can re-serve the whole chain instead of relaying orphaned seals."""
        roots = {i["batch_root"] for i in items if i.get("batch_root")}
        if not roots:
            return
        with conn.cursor() as cur:
            self._insert_batches(cur, batches, roots)

    @staticmethod
    def _seal_values(item: dict) -> tuple:
        """The five seal columns in a fixed order, for the INSERT templates."""
        return (item.get("fetched_at"), item.get("author_pubkey"),
                item.get("signature"), item.get("batch_root"),
                psycopg2.extras.Json(item.get("merkle_proof"))
                if item.get("merkle_proof") is not None else None)

    @staticmethod
    def _batch_verifier(batches: dict):
        """Per-import cache: batch_root -> Worker-timestamp chain verdict
        (authority trusted + countersignature over {root, date, ip_hash})."""
        cache: dict = {}

        def ok(root) -> bool:
            if not root:
                return False
            if root not in cache:
                b = batches.get(root)
                cache[root] = bool(
                    b and b.get("authority") in TRUSTED_AUTHORITIES
                    and rs.verify_timestamp(root, b["worker_date"], b["ip_hash"],
                                            b["worker_sig"], b["authority"]))
            return cache[root]

        return ok

    @staticmethod
    def _insert_batches(cur, batches: dict, roots: set):
        """Store the Worker-timestamp rows referenced by verified seals, so
        this node re-serves the full chain (relay keeps authorship intact)."""
        if not roots:
            return
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO signing_batches
               (batch_root, author_pubkey, worker_date, ip_hash,
                worker_sig, authority)
               VALUES %s ON CONFLICT (batch_root) DO NOTHING""",
            [(r, batches[r]["author_pubkey"], batches[r]["worker_date"],
              batches[r]["ip_hash"], batches[r]["worker_sig"],
              batches[r]["authority"]) for r in roots],
            template="(%s, %s, %s::timestamptz, %s::uuid, %s, %s)",
        )

    @staticmethod
    def _portrait(vectors: list) -> list:
        """normalize(mean(segment vectors)) — the local mean derivation,
        mirroring backend/embeddings.py _compute_segments. The mean is a
        derived aggregate, never wire data (see desktop/p2p/record_sig.py)."""
        dim = len(vectors[0])
        n = len(vectors)
        mean = [sum(v[i] for v in vectors) / n for i in range(dim)]
        norm = math.sqrt(sum(x * x for x in mean)) or 1.0
        return [x / norm for x in mean]

    @staticmethod
    def _vec_text(values: list) -> str:
        """pgvector text literal; repr keeps the shortest float round-trip."""
        return "[" + ",".join(repr(float(x)) for x in values) + "]"

    # -- Category importers (batch) ----------------------------------------

    def _import_segments(self, conn, items: list[dict], batches: dict) -> int:
        """Import per-track segment bundles: verify every seal, store the
        segments seal-intact, derive the track mean locally. A bundle with ANY
        invalid OR MISSING seal is dropped whole — a broken seal on
        TLS-protected wire is evidence of tampering, and an unsigned one is a
        claim this node would go on to redistribute unattributably. Returns
        the count of imported tracks."""
        batch_ok = self._batch_verifier(batches)

        # Decode + verify outside the write path: (item, [(idx, floats, seal)])
        decoded = []
        for item in items:
            prov = item.get("provenance") or {}
            segs, bad = [], None
            for s in item.get("segments", []):
                try:
                    raw = base64.b64decode(s["v"], validate=True)
                    floats = rs.vector_from_bytes(raw)
                except (KeyError, ValueError, struct.error):
                    bad = f"segment {s.get('i')} undecodable"
                    break
                if not s.get("signature"):
                    bad = f"segment {s.get('i')} unsigned"
                    break
                try:
                    payload = rs.segment_payload(
                        s.get("author_pubkey") or "", item["track_uuid"],
                        prov.get("pcm_hash") or "", prov.get("chromaprint"),
                        prov.get("duration_seconds"),
                        item.get("model_uuid") or "", s["i"],
                        rs.vector_hash(raw),
                        grid_version=prov.get("grid_version")
                                     or rs.GRID_VERSION)
                except ValueError:
                    payload = None
                if not (payload
                        and rs.verify(payload, s["signature"],
                                      s.get("author_pubkey") or "")
                        and rs.verify_proof(rs.record_leaf(s["signature"]),
                                            s.get("proof") or [],
                                            s.get("batch_root") or "")
                        and batch_ok(s.get("batch_root"))):
                    bad = f"segment {s['i']} seal invalid"
                    break
                seal = (s["author_pubkey"], s["signature"], s["batch_root"],
                        psycopg2.extras.Json(s.get("proof") or []))
                segs.append((s["i"], floats, seal))
            if bad:
                logger.warning("sync import: dropping track %s — %s",
                               item.get("track_uuid", "?")[:8], bad)
            elif segs:
                decoded.append((item, segs))

        if not decoded:
            return 0

        with conn.cursor() as cur:
            # Batch upsert embedding models (deduplicated)
            models_seen = {}
            for item, segs in decoded:
                mid = item.get("model_uuid")
                if mid and mid not in models_seen:
                    models_seen[mid] = (mid, item.get("model_name", "unknown"),
                                        len(segs[0][1]))
            if models_seen:
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO embedding_models (id, name, dimension)
                       VALUES %s ON CONFLICT (id) DO NOTHING""",
                    list(models_seen.values()),
                    template="(%s, %s, %s)",
                )

            source_items = [item for item, _ in decoded]
            source_map = self._upsert_analysis_sources(cur, source_items)

            # Import-side guard (the planner prefilters, the importer
            # enforces): never replace first-hand analysis or a grid already
            # present locally.
            kept = self._drop_protected(
                cur, source_items,
                """SELECT e.track_id::text FROM embeddings e
                   LEFT JOIN analysis_sources s ON s.id = e.analysis_source_id
                   WHERE e.track_id = ANY(%s::uuid[])
                     AND (EXISTS (SELECT 1 FROM embedding_segments es
                                  WHERE es.embedding_id = e.id)
                          OR (s.id IS NOT NULL AND NOT s.imported))""",
                "segments")
            kept_uuids = {i["track_uuid"] for i in kept}
            if not kept_uuids:
                return 0

            self._insert_batches(cur, batches, {
                seal[2]
                for item, segs in decoded if item["track_uuid"] in kept_uuids
                for _, _, seal in segs if seal})

            imported = 0
            for item, segs in decoded:
                if item["track_uuid"] not in kept_uuids:
                    continue
                mean = self._portrait([floats for _, floats, _ in segs])
                cur.execute(
                    """INSERT INTO embeddings
                       (track_id, model_id, vector, analysis_source_id,
                        analysis_version)
                       VALUES (%s, %s, %s::vector, %s, %s)
                       ON CONFLICT (track_id, model_id) DO UPDATE SET
                           vector = EXCLUDED.vector,
                           analysis_source_id = EXCLUDED.analysis_source_id,
                           analysis_version = EXCLUDED.analysis_version,
                           updated_at = CURRENT_TIMESTAMP
                       RETURNING id""",
                    (item["track_uuid"], item["model_uuid"],
                     self._vec_text(mean), self._source_id(source_map, item),
                     item.get("analysis_version", 1)))
                emb_id = cur.fetchone()[0]
                # The grid is atomic — an upgraded imported-mean row never
                # mixes leftover segments from another pull.
                cur.execute(
                    "DELETE FROM embedding_segments WHERE embedding_id = %s",
                    (emb_id,))
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO embedding_segments
                       (embedding_id, segment_index, vector,
                        author_pubkey, signature, batch_root, merkle_proof)
                       VALUES %s""",
                    [(emb_id, idx, self._vec_text(floats),
                      *(seal or (None, None, None, None)))
                     for idx, floats, seal in segs],
                    template="(%s, %s, %s::vector, %s, %s, %s, %s)",
                    page_size=200,
                )
                imported += 1
        return imported

    @staticmethod
    def _upsert_analysis_sources(cur, items: list[dict]) -> dict:
        """Upsert the distinct provenance rows carried by analysis items and
        return {(track_uuid, pcm_hash): analysis_sources.id}. media_file_id is
        always NULL and imported=true — the sender's material is not ours, so
        these sources are never signable here (sign_audio excludes imported).

        `origin` is NULL for imported rows, and honestly so: the wire no
        longer says whether the author analysed a file or a stream (see
        _provenance_item), and the field exists only to rank OUR OWN
        analysis. Local knowledge always wins on conflict; only missing
        chromaprint/duration are filled in."""
        prov = {}
        for item in items:
            p = item.get("provenance")
            if p and p.get("pcm_hash"):
                prov[(item["track_uuid"], p["pcm_hash"])] = p
        if not prov:
            return {}
        values = [
            (tid, p["pcm_hash"], p.get("chromaprint"),
             p.get("duration_seconds"),
             p.get("grid_version", 1), p.get("is_lossless"))
            for (tid, _), p in prov.items()
        ]
        rows = psycopg2.extras.execute_values(
            cur,
            """INSERT INTO analysis_sources
               (track_id, pcm_hash, chromaprint, duration_seconds,
                grid_version, is_lossless, imported)
               VALUES %s
               ON CONFLICT (track_id, pcm_hash) DO UPDATE SET
                   chromaprint = COALESCE(analysis_sources.chromaprint,
                                          EXCLUDED.chromaprint),
                   duration_seconds = COALESCE(analysis_sources.duration_seconds,
                                               EXCLUDED.duration_seconds)
               RETURNING id, track_id::text, pcm_hash""",
            values,
            template="(%s::uuid, %s, %s, %s, %s, %s, true)",
            fetch=True,
        )
        return {(r[1], r[2]): r[0] for r in rows}

    @staticmethod
    def _drop_protected(cur, items: list[dict], protected_sql: str,
                        what: str) -> list[dict]:
        """Filter out items whose EXISTING local row must not be replaced by
        an unsigned import. Tier-0 lite: my own complete/signed analysis
        always outranks a peer's copy of anything."""
        if not items:
            return items
        cur.execute(protected_sql, ([i["track_uuid"] for i in items],))
        protected = {r[0] for r in cur.fetchall()}
        if protected:
            logger.debug("sync import: %d %s rows protected (own analysis)",
                         len(protected), what)
        return [i for i in items if i["track_uuid"] not in protected]

    @staticmethod
    def _source_id(source_map: dict, item: dict):
        p = item.get("provenance")
        if not p or not p.get("pcm_hash"):
            return None
        return source_map.get((item["track_uuid"], p["pcm_hash"]))

    def _import_embeddings(self, conn, items: list[dict]) -> int:
        with conn.cursor() as cur:
            # Batch upsert embedding models (deduplicated)
            models_seen = {}
            for item in items:
                mid = item.get("model_uuid")
                if mid and mid not in models_seen:
                    models_seen[mid] = (
                        mid, item.get("model_name", "unknown"),
                        len(item.get("vector", [])),
                    )
            if models_seen:
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO embedding_models (id, name, dimension)
                       VALUES %s ON CONFLICT (id) DO NOTHING""",
                    list(models_seen.values()),
                    template="(%s, %s, %s)",
                )

            # Provenance registers for ALL items (universal material facts);
            # the data write skips rows that carry MY OWN segments — importing
            # only the mean would re-point the provenance those segments
            # inherit through their embeddings row (lying segments, broken
            # segment seals). Peer-imported rows have no segments, so imports
            # keep refreshing each other freely.
            source_map = self._upsert_analysis_sources(cur, items)
            items = self._drop_protected(
                cur, items,
                """SELECT e.track_id::text FROM embeddings e
                   WHERE e.track_id = ANY(%s::uuid[])
                     AND EXISTS (SELECT 1 FROM embedding_segments es
                                 WHERE es.embedding_id = e.id)""",
                "embeddings")
            if not items:
                return 0

            # Batch upsert embeddings
            values = [
                (
                    item["track_uuid"], item.get("model_uuid"),
                    str(item.get("vector", [])),
                    self._source_id(source_map, item),
                    item.get("analysis_version", 1),
                )
                for item in items
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO embeddings
                   (track_id, model_id, vector, analysis_source_id,
                    analysis_version)
                   VALUES %s
                   ON CONFLICT (track_id, model_id) DO UPDATE SET
                       vector = EXCLUDED.vector,
                       analysis_source_id = EXCLUDED.analysis_source_id,
                       analysis_version = EXCLUDED.analysis_version,
                       updated_at = CURRENT_TIMESTAMP""",
                values,
                template="(%s, %s, %s::vector, %s, %s)",
                page_size=200,
            )
        return len(items)

    def _import_audio_features(self, conn, items: list[dict],
                               batches: dict) -> int:
        batch_ok = self._batch_verifier(batches)

        # Verify-on-import: a row must verify end-to-end (author sig → Merkle
        # proof → Worker timestamp over the transmitted values) or it drops.
        # Unsigned rows drop too — see _verify_enrichment for why: we would
        # redistribute them, and an unattributable claim must not travel with
        # our address on it.
        verified = []
        for item in items:
            if not item.get("signature"):
                logger.warning(
                    "sync import: dropping unsigned features %s",
                    item.get("track_uuid", "?")[:8])
                continue
            prov = item.get("provenance") or {}
            try:
                payload = rs.features_payload(
                    item.get("author_pubkey") or "", item["track_uuid"],
                    prov.get("pcm_hash") or "", prov.get("chromaprint"),
                    prov.get("duration_seconds"),
                    item.get("analysis_version", 1),
                    rs.blake2b_hex(rs.canonical_features_blob(item)))
            except (KeyError, ValueError):
                payload = None
            if not (payload
                    and rs.verify(payload, item["signature"],
                                  item.get("author_pubkey") or "")
                    and rs.verify_proof(rs.record_leaf(item["signature"]),
                                        item.get("merkle_proof") or [],
                                        item.get("batch_root") or "")
                    and batch_ok(item.get("batch_root"))):
                logger.warning(
                    "sync import: dropping features %s — seal invalid",
                    item.get("track_uuid", "?")[:8])
                continue
            verified.append(item)
        items = verified
        if not items:
            return 0

        with conn.cursor() as cur:
            # Provenance registers for ALL items; the data write never
            # replaces a SIGNED row (overwriting would strip my seal via the
            # guard trigger) nor my own first-hand local analysis.
            source_map = self._upsert_analysis_sources(cur, items)
            items = self._drop_protected(
                cur, items,
                """SELECT a.track_id::text FROM audio_features a
                   LEFT JOIN analysis_sources s ON s.id = a.analysis_source_id
                   WHERE a.track_id = ANY(%s::uuid[])
                     AND (a.signature IS NOT NULL
                          OR (s.origin = 'local' AND NOT s.imported))""",
                "audio_features")
            if not items:
                return 0
            self._insert_batches(
                cur, batches,
                {i["batch_root"] for i in items if i.get("signature")})
            values = [
                (
                    item["track_uuid"], item.get("bpm"), item.get("key"),
                    item.get("mode"), item.get("key_confidence"),
                    item.get("energy"), item.get("energy_db"),
                    item.get("brightness"), item.get("dynamic_range_db"),
                    item.get("zero_crossing_rate"),
                    psycopg2.extras.Json(item.get("instruments")),
                    psycopg2.extras.Json(item.get("moods")),
                    item.get("vocal_instrumental"), item.get("vocal_score"),
                    item.get("danceability"),
                    self._source_id(source_map, item),
                    item.get("analysis_version", 1),
                    item.get("author_pubkey"), item.get("signature"),
                    item.get("batch_root"),
                    psycopg2.extras.Json(item["merkle_proof"])
                    if item.get("signature") else None,
                )
                for item in items
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO audio_features
                   (track_id, bpm, key, mode, key_confidence,
                    energy, energy_db, brightness, dynamic_range_db,
                    zero_crossing_rate, instruments, moods,
                    vocal_instrumental, vocal_score, danceability,
                    analysis_source_id, analysis_version,
                    author_pubkey, signature, batch_root, merkle_proof)
                   VALUES %s
                   ON CONFLICT (track_id) DO UPDATE SET
                       bpm = EXCLUDED.bpm, key = EXCLUDED.key,
                       mode = EXCLUDED.mode, key_confidence = EXCLUDED.key_confidence,
                       energy = EXCLUDED.energy, energy_db = EXCLUDED.energy_db,
                       brightness = EXCLUDED.brightness,
                       dynamic_range_db = EXCLUDED.dynamic_range_db,
                       zero_crossing_rate = EXCLUDED.zero_crossing_rate,
                       instruments = EXCLUDED.instruments, moods = EXCLUDED.moods,
                       vocal_instrumental = EXCLUDED.vocal_instrumental,
                       vocal_score = EXCLUDED.vocal_score,
                       danceability = EXCLUDED.danceability,
                       analysis_source_id = EXCLUDED.analysis_source_id,
                       analysis_version = EXCLUDED.analysis_version,
                       author_pubkey = EXCLUDED.author_pubkey,
                       signature = EXCLUDED.signature,
                       batch_root = EXCLUDED.batch_root,
                       merkle_proof = EXCLUDED.merkle_proof,
                       updated_at = CURRENT_TIMESTAMP""",
                values,
                template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                page_size=500,
            )
        return len(items)

    def _import_track_stats(self, conn, items: list[dict]) -> int:
        with conn.cursor() as cur:
            values = [
                (
                    item["track_uuid"], item.get("source", "sync"),
                    item.get("listeners"), item.get("playcount"),
                    *self._seal_values(item),
                )
                for item in items
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO track_stats
                       (track_id, source, listeners, playcount, fetched_at,
                        author_pubkey, signature, batch_root, merkle_proof,
                        imported)
                   VALUES %s
                   ON CONFLICT (track_id, source) DO UPDATE SET
                       listeners = EXCLUDED.listeners,
                       playcount = EXCLUDED.playcount,
                       fetched_at = EXCLUDED.fetched_at,
                       author_pubkey = EXCLUDED.author_pubkey,
                       signature = EXCLUDED.signature,
                       batch_root = EXCLUDED.batch_root,
                       merkle_proof = EXCLUDED.merkle_proof,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE """ + _ENRICHMENT_PRECEDENCE.format(t="track_stats"),
                values,
                template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)",
                page_size=500,
            )
        return len(items)

    def _import_track_mbids(self, conn, items: list[dict]) -> int:
        """Sealed track↔recording bindings — same shape as artist_mbids:
        the track must exist, an existing binding outranks a restatement."""
        if not items:
            return 0
        uuids = list({item["track_uuid"] for item in items})
        with conn.cursor() as cur:
            cur.execute("SELECT id::text FROM tracks WHERE id = ANY(%s::uuid[])",
                        (uuids,))
            known = {r[0] for r in cur.fetchall()}
            values = [
                (item["recording_mbid"], item["track_uuid"],
                 item.get("confidence"), item.get("fetched_at"),
                 *self._seal_values(item)[1:])
                for item in items if item["track_uuid"] in known
            ]
            if not values:
                return 0
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO track_mbids
                       (recording_mbid, track_id, confidence, created_at,
                        author_pubkey, signature, batch_root, merkle_proof,
                        imported)
                   VALUES %s
                   ON CONFLICT (recording_mbid) DO NOTHING""",
                values,
                template="(%s::uuid, %s::uuid, %s::mb_match_confidence,"
                         " %s::timestamptz, %s, %s, %s, %s, TRUE)")
        return len(values)

    def _import_artist_bios(self, conn, items: list[dict]) -> int:
        with conn.cursor() as cur:
            # Deduplicate items by (artist_uuid, source) — last wins
            deduped = {
                (item["artist_uuid"], item.get("source", "sync")): item
                for item in items
            }
            unique_items = list(deduped.values())

            # Batch upsert artists (deduplicated by uuid)
            artist_values = list({
                item["artist_uuid"]: (item["artist_uuid"], item["artist_name"])
                for item in unique_items if item.get("artist_name")
            }.values())
            if artist_values:
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO artists (id, name)
                       VALUES %s ON CONFLICT (id) DO NOTHING""",
                    artist_values,
                    template="(%s, %s)",
                )

            # Batch upsert bios
            values = [
                (
                    item["artist_uuid"], item.get("source", "sync"),
                    item.get("summary"), item.get("content"),
                    item.get("url"), item.get("listeners"),
                    item.get("playcount"),
                    *self._seal_values(item),
                )
                for item in unique_items
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO artist_bios
                   (artist_id, source, summary, content, url, listeners, playcount,
                    fetched_at,
                        author_pubkey, signature, batch_root, merkle_proof,
                        imported)
                   VALUES %s
                   ON CONFLICT (artist_id, source) DO UPDATE SET
                       summary = EXCLUDED.summary,
                       content = EXCLUDED.content,
                       url = EXCLUDED.url,
                       listeners = EXCLUDED.listeners,
                       playcount = EXCLUDED.playcount,
                       fetched_at = EXCLUDED.fetched_at,
                       author_pubkey = EXCLUDED.author_pubkey,
                       signature = EXCLUDED.signature,
                       batch_root = EXCLUDED.batch_root,
                       merkle_proof = EXCLUDED.merkle_proof,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE """ + _ENRICHMENT_PRECEDENCE.format(t="artist_bios"),
                values,
                template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)",
                page_size=500,
            )
        return len(items)

    def _reconcile_named(self, cur, table: str, pairs: list[tuple]) -> dict:
        """Get-or-create content-addressed (id, name) rows; return wire name -> local id.

        tags/genres are unique on BOTH axes — id (uuid5 of the folded name)
        and name (the raw spelling) — and a peer can differ from us on either:
        the same name under a different id (legacy pre-v5 rows, a divergent
        enrichment history), or — since identity normalize v2 folds case and
        punctuation — the same id under a different spelling ("hip hop" here,
        "Hip-Hop" there). An INSERT that guards one axis crashes the whole
        batch on the other (tags_pkey, launcher stand 2026-08-26), and even
        skipping it would leave the child row (artist_tags /
        genre_descriptions) pointing at an id this node lacks. So: resolve by
        id first (the folded identity — the local row wins, however it is
        spelled), then by name, and insert only what neither axis knows, ON
        CONFLICT DO NOTHING covering a race on either constraint. `table` is
        a trusted literal from the caller, never user input.
        """
        by_name = {name: wid for wid, name in pairs if name}   # dedup, last wins
        if not by_name:
            return {}
        resolved: dict = {}

        def lookup(subset: dict) -> None:
            cur.execute(
                f"SELECT id::text, name FROM {table} "
                f"WHERE id = ANY(%s::uuid[]) OR name = ANY(%s)",
                (list(set(subset.values())), list(subset)))
            rows = cur.fetchall()
            local_by_id = {i: n for i, n in rows}
            local_by_name = {n: i for i, n in rows}
            for name, wid in subset.items():
                if wid in local_by_id:
                    resolved[name] = wid
                elif name in local_by_name:
                    resolved[name] = local_by_name[name]

        lookup(by_name)
        missing = {n: w for n, w in by_name.items() if n not in resolved}
        if missing:
            psycopg2.extras.execute_values(
                cur,
                f"INSERT INTO {table} (id, name) VALUES %s ON CONFLICT DO NOTHING",
                [(w, n) for n, w in missing.items()], template="(%s, %s)",
            )
            lookup(missing)
        return resolved

    def _import_artist_tags(self, conn, items: list[dict]) -> int:
        with conn.cursor() as cur:
            local = self._reconcile_named(
                cur, "tags",
                [(item["tag_uuid"], item["tag_name"]) for item in items])
            values = [
                (item["artist_uuid"], local[item["tag_name"]],
                 item["weight"], item.get("source", "sync"),
                 *self._seal_values(item))
                for item in items if item["tag_name"] in local
            ]
            if values:
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO artist_tags
                       (artist_id, tag_id, weight, source, fetched_at,
                        author_pubkey, signature, batch_root, merkle_proof, imported)
                       VALUES %s
                       ON CONFLICT (artist_id, tag_id, source) DO UPDATE SET
                           weight = EXCLUDED.weight,
                           fetched_at = EXCLUDED.fetched_at,
                           author_pubkey = EXCLUDED.author_pubkey,
                           signature = EXCLUDED.signature,
                           batch_root = EXCLUDED.batch_root,
                           merkle_proof = EXCLUDED.merkle_proof,
                           updated_at = CURRENT_TIMESTAMP
                       WHERE """ + _ENRICHMENT_PRECEDENCE.format(t="artist_tags"),
                    values,
                    template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)",
                    page_size=500,
                )
        return len(items)

    def _import_similar_artists(self, conn, items: list[dict]) -> int:
        # Boundary filter: a self-edge is always garbage (namesake-collapse
        # era rows on peers that predate chk_not_self_similar) and would kill
        # the whole batch against the constraint here.
        self_edges = [i for i in items
                      if i["artist_uuid"] == i["similar_artist_uuid"]]
        if self_edges:
            logger.warning("sync import: dropping %d self-similar edge(s)",
                           len(self_edges))
            items = [i for i in items
                     if i["artist_uuid"] != i["similar_artist_uuid"]]
            if not items:
                return 0
        with conn.cursor() as cur:
            # Batch upsert similar artists
            artist_values = list({
                item["similar_artist_uuid"]: (
                    item["similar_artist_uuid"], item["similar_artist_name"]
                )
                for item in items if item.get("similar_artist_name")
            }.values())
            if artist_values:
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO artists (id, name)
                       VALUES %s ON CONFLICT (id) DO NOTHING""",
                    artist_values,
                    template="(%s, %s)",
                )

            # Batch upsert similar_artists
            values = [
                (
                    item["artist_uuid"], item["similar_artist_uuid"],
                    item["match_score"], item.get("source", "sync"),
                    *self._seal_values(item),
                )
                for item in items
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO similar_artists
                   (artist_id, similar_artist_id, match_score, source, fetched_at,
                        author_pubkey, signature, batch_root, merkle_proof, imported)
                   VALUES %s
                   ON CONFLICT (artist_id, similar_artist_id, source) DO UPDATE SET
                       match_score = EXCLUDED.match_score,
                       fetched_at = EXCLUDED.fetched_at,
                       author_pubkey = EXCLUDED.author_pubkey,
                       signature = EXCLUDED.signature,
                       batch_root = EXCLUDED.batch_root,
                       merkle_proof = EXCLUDED.merkle_proof,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE """ + _ENRICHMENT_PRECEDENCE.format(t="similar_artists"),
                values,
                template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)",
                page_size=500,
            )
        return len(items)

    def _import_genre_descriptions(self, conn, items: list[dict]) -> int:
        with conn.cursor() as cur:
            local = self._reconcile_named(
                cur, "genres",
                [(item["genre_uuid"], item.get("genre_name")) for item in items])

            # Batch upsert genre_descriptions (genre_id resolved to the local row)
            values = [
                (
                    local[item["genre_name"]], item.get("source", "sync"),
                    item.get("summary"), item.get("content"),
                    item.get("url"), *self._seal_values(item),
                )
                for item in items if item.get("genre_name") in local
            ]
            if values:
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO genre_descriptions
                       (genre_id, source, summary, content, url, fetched_at,
                        author_pubkey, signature, batch_root, merkle_proof, imported)
                       VALUES %s
                       ON CONFLICT (genre_id, source) DO UPDATE SET
                           summary = EXCLUDED.summary,
                           content = EXCLUDED.content,
                           url = EXCLUDED.url,
                           fetched_at = EXCLUDED.fetched_at,
                           author_pubkey = EXCLUDED.author_pubkey,
                           signature = EXCLUDED.signature,
                           batch_root = EXCLUDED.batch_root,
                           merkle_proof = EXCLUDED.merkle_proof,
                           updated_at = CURRENT_TIMESTAMP
                       WHERE """ + _ENRICHMENT_PRECEDENCE.format(t="genre_descriptions"),
                    values,
                    template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)",
                    page_size=500,
                )
        return len(items)

    # -- Post-import enrichment -----------------------------------------------

    def _update_artist_gender(self, artist_uuids: list[str]) -> int:
        """Classify artist gender from bio pronouns (she/he/her/his/they).

        Uses regexp_count with \\y word boundaries for accurate matching.
        Only updates artists whose bios were just imported.
        """
        if not artist_uuids:
            return 0
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """WITH pronoun_analysis AS (
                        SELECT ab.artist_id,
                            regexp_count(LOWER(ab.content), '\\yshe\\y')
                                + regexp_count(LOWER(ab.content), '\\yher\\y') AS female_score,
                            regexp_count(LOWER(ab.content), '\\yhe\\y')
                                + regexp_count(LOWER(ab.content), '\\yhis\\y') AS male_score,
                            regexp_count(LOWER(ab.content), '\\ythey\\y') AS group_score
                        FROM artist_bios ab
                        WHERE ab.artist_id = ANY(%s::uuid[])
                          AND LENGTH(ab.content) > 200
                    )
                    UPDATE artists a
                    SET gender = (CASE
                        WHEN pa.female_score >= 2 AND pa.female_score > pa.male_score * 2
                             AND (pa.male_score = 0 OR (pa.female_score - pa.male_score) >= 4)
                            THEN 'female'
                        WHEN pa.male_score >= 2 AND pa.male_score > pa.female_score * 2
                             AND (pa.female_score = 0 OR (pa.male_score - pa.female_score) >= 4)
                            THEN 'male'
                        WHEN pa.group_score > GREATEST(pa.female_score, pa.male_score)
                             AND pa.group_score >= 3
                            THEN 'mixed'
                        ELSE 'unknown'
                    END)::artist_gender,
                    updated_at = NOW()
                    FROM pronoun_analysis pa
                    WHERE a.id = pa.artist_id""",
                    [artist_uuids],
                )
                updated = cur.rowcount
            conn.commit()
            return updated
        except Exception as e:
            conn.rollback()
            logger.error(f"Gender classification failed: {e}")
            return 0

    def _update_artist_is_vocalist(self, artist_uuids: list[str]) -> int:
        """Classify artists as vocal/instrumental from bio keywords.

        Rules mirror LastFmService._update_artist_is_vocalist:
            - Any vocal keyword (strong or medium) → 'vocal'.
            - Only instrumental keywords → 'instrumental'.
            - Otherwise → 'unknown'.
        Only updates artists whose bios were just imported.
        """
        if not artist_uuids:
            return 0
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    r"""WITH vocal_analysis AS (
                        SELECT ab.artist_id,
                            regexp_count(LOWER(ab.content), '\ysinger\y')
                            + regexp_count(LOWER(ab.content), '\ysingers\y')
                            + regexp_count(LOWER(ab.content), '\yvocalist\y')
                            + regexp_count(LOWER(ab.content), '\yvocalists\y')
                            + regexp_count(LOWER(ab.content), '\yfrontman\y')
                            + regexp_count(LOWER(ab.content), '\yfrontwoman\y')
                            + regexp_count(LOWER(ab.content), '\ycrooner\y')
                            + regexp_count(LOWER(ab.content), '\ychanteuse\y')
                            + regexp_count(LOWER(ab.content), '\ysoprano\y')
                            + regexp_count(LOWER(ab.content), '\ytenor\y')
                            + regexp_count(LOWER(ab.content), '\ybaritone\y')
                            + regexp_count(LOWER(ab.content), '\ycontralto\y')
                            + regexp_count(LOWER(ab.content), '\yrapper\y')
                            + regexp_count(LOWER(ab.content), '\yvocal\y')
                            + regexp_count(LOWER(ab.content), '\yvocals\y')
                            + regexp_count(LOWER(ab.content), '\ysinging\y')
                            + regexp_count(LOWER(ab.content), '\ysings\y')
                            + regexp_count(LOWER(ab.content), '\ysang\y')
                            + regexp_count(LOWER(ab.content), '\yrapping\y') AS vocal_hits,
                            regexp_count(LOWER(ab.content), '\yinstrumental\y')
                            + regexp_count(LOWER(ab.content), '\yinstrumentals\y')
                            + regexp_count(LOWER(ab.content), '\yinstrumentalist\y') AS instr_hits
                        FROM artist_bios ab
                        WHERE ab.artist_id = ANY(%s::uuid[])
                          AND LENGTH(ab.content) > 200
                    )
                    UPDATE artists a
                    SET is_vocalist = (CASE
                        WHEN va.vocal_hits >= 1 THEN 'vocal'
                        WHEN va.instr_hits >= 1 THEN 'instrumental'
                        ELSE 'unknown'
                    END)::artist_vocalist,
                    updated_at = NOW()
                    FROM vocal_analysis va
                    WHERE a.id = va.artist_id""",
                    [artist_uuids],
                )
                updated = cur.rowcount
            conn.commit()
            return updated
        except Exception as e:
            conn.rollback()
            logger.error(f"Vocalist classification failed: {e}")
            return 0


def import_pushed(db_dsn: str, category: str, payload: dict) -> int:
    """Verify and import a payload a peer PUSHED to us.

    The receiving side of push-seeding deliberately reuses the pull path's
    import gate rather than growing a second one: same seal verification,
    same first-hand protection, same precedence rules. A pushed record gets
    exactly the scrutiny a pulled one does — the only difference is who
    started the conversation.
    """
    if category not in CARRY_CATEGORIES:
        raise ValueError(f"category not carryable: {category}")
    client = SyncClient(api_client=None, db_dsn=db_dsn)
    try:
        return client._import_items(category, payload)
    finally:
        client._close_conn()
