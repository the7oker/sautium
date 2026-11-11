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
node fetches its own from the public sources. Nor is anything Last.fm
answered — bios, tags, similars, track stats, genre descriptions (since
2026-09-19): its API terms do not allow redistribution, so that layer is
node-local and every node fetches its own by name.
"""

import base64
import logging
import math
import struct
import time
import unicodedata
import uuid
from functools import partial
from typing import Optional, Callable

import psycopg2
import psycopg2.extras

from desktop.api_client import BackendAPIClient
from desktop.p2p import material
from desktop.p2p import record_sig as rs
from desktop.p2p.birth_cert import TRUSTED_AUTHORITIES
from desktop.p2p.bloom import BloomFilter
from desktop.p2p import sync_queries
from desktop.p2p.sync_queries import CARRY_CATEGORIES, EMPTY_INVENTORY

logger = logging.getLogger(__name__)

# Categories and their pull endpoint names. Audio embeddings are pulled as
# sealed `segments` only, from a peer that advertises the capability; a peer
# without it is asked for none (the unsealed mean-vector pull is gone,
# 2026-11-03: its rows took no seal, so a peer chose the vectors a track
# got and nothing named who) — its sealed audio features and instruments
# still.
CATEGORIES = [
    # (category_key_in_inventory, pull_endpoint, uuid_column_hint)
    ("segments", "segments", "track"),
    ("audio_features", "audio-features", "track"),
    ("instruments", "instruments", "track"),
]

# Pull category -> record kind. Membership in this map is what makes a category
# verified; a new enrichment category is unverified until it appears here, which
# is the failure mode we want (visible, not silent).
_ENRICHMENT_KIND_BY_CATEGORY = {"track_mbids": "track_mbid"}

DEFAULT_BATCH_SIZE = 500
# A segments batch is K=12..24 vectors + proofs per track (~50KB) — keep the
# per-request payload in the single-digit-MB range.
SEGMENT_PULL_BATCH = 100
# Both server implementations reject inventory requests above 10k UUIDs —
# the client slices its library and merges the responses.
INVENTORY_CHUNK = 10_000

# The canonical grid's window (mirrors backend/embeddings.WINDOW_SECONDS) and
# the most windows a bundle may hold — 24 h of track: the importer stores
# every segment it is given, so a bundle is bounded by its track's length.
GRID_WINDOW_S = 10
MAX_GRID_WINDOWS = 24 * 3600 // GRID_WINDOW_S
# What the columns a bundle lands in hold: vector(512) for the mean and each
# segment, VARCHAR(100) for its model's name. No node stores another, so
# only a crafted answer carries one, and it drops at the gate rather than
# failing the page in the database.
VECTOR_DIM = 512
MODEL_NAME_CHARS = 100


def _on_the_grid(segments, duration) -> bool:
    """A bundle's segments fit the canonical grid of the duration its seals
    declare: indices unique, each inside it, no more of them than it has
    windows. One window over: the declared duration is the file's tag,
    rounded, and the grid is cut from the decoded audio, which can run past
    a ten-second boundary."""
    if not isinstance(segments, list) or not segments:
        return False
    if duration is None:
        windows = MAX_GRID_WINDOWS
    elif isinstance(duration, int) and not isinstance(duration, bool) and duration > 0:
        windows = min(max(1, duration // GRID_WINDOW_S) + 1, MAX_GRID_WINDOWS)
    else:
        return False
    idx = [s.get("i") if isinstance(s, dict) else None for s in segments]
    return (len(idx) <= windows and len(set(idx)) == len(idx)
            and all(isinstance(i, int) and not isinstance(i, bool) and 0 <= i < windows
                    for i in idx))


# A segment is a unit vector: what the scores derived from it assume
# (zero_shot_derive in 001_initial.sql, whose band this mirrors — a bundle
# outside it derives none there). Every analyser normalizes in float32,
# within 1.5e-7 of 1 as measured on CUDA bf16, x86 and ARM; one normalizing
# in bf16 would stay within 0.8 %. A seal says who made a vector, not that
# it is one a node can use. The node's own passes hold what they save to
# the same band (backend/embeddings.py _persist_analysis), so a node whose
# analysis went wrong saves no embedding there and never seals what every
# peer refuses.
UNIT_NORM = (0.99, 1.01)


def is_unit(values: list) -> bool:
    return UNIT_NORM[0] <= math.sqrt(math.fsum(x * x for x in values)) <= UNIT_NORM[1]


# Mirrors backend/uuid_utils.embedding_model_uuid (desktop must not import
# the backend package): a model's id is the uuid5 of its name alone.
_NAMESPACE = uuid.UUID("adc1ec0b-2c81-5e26-9938-a369c6f7a5e1")


def model_uuid(name: str) -> str:
    key = " ".join(unicodedata.normalize("NFC", name.strip().lower()).split())
    return str(uuid.uuid5(_NAMESPACE, f"embedding_model:{key}"))


def _names_its_model(item: dict) -> bool:
    """The bundle's model id is its model name's: the importer makes the
    embedding_models row from the pair, and a name under another id would
    hold that name against the node's own model."""
    name, given = item.get("model_name"), item.get("model_uuid")
    return (isinstance(name, str) and isinstance(given, str)
            and given.lower() == model_uuid(name))


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_uuid(value) -> bool:
    """A uuid in its one canonical text, lower case: every lookup the import
    makes by it is by that text."""
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


# The fields of a features record and the kind of value each holds: the
# gate's one table of them, in the order the record's seal hashes them
# (record_sig.FEATURE_ORDER), which the features import also writes its
# columns from. A field of another kind drops its item at the gate. A number
# is a float: its column is float8, which hands every value back as one, so
# a record sealed over an int (120 where the column gives 120.0) would verify
# here and be re-served under a hash its seal does not bind.
_FLOAT, _TEXT, _SCORES = "float", "text", "scores"
FEATURE_FIELDS = {
    "bpm": _FLOAT, "key": _TEXT, "mode": _TEXT, "key_confidence": _FLOAT,
    "energy": _FLOAT, "energy_db": _FLOAT, "brightness": _FLOAT,
    "dynamic_range_db": _FLOAT, "zero_crossing_rate": _FLOAT,
}
# The instruments record's one field, the same way: the tagger's raw score
# per label (record_sig.canonical_instruments_blob). The readers threshold
# it with `->>` and a cast, so a label is a short text and a score a number
# in 0..1 — anything else would fail a reader's query, not its own import —
# and there are at most INSTRUMENT_LABELS of them (the tagger knows fewer
# than a hundred and stores its top twenty). A negative zero is no score:
# JSONB's numeric keeps no sign of zero, so the row would be re-served under
# a hash its seal does not bind. An empty map is a record.
INSTRUMENT_FIELDS = {"instruments": _SCORES}
INSTRUMENT_LABELS = 128
INSTRUMENT_LABEL_CHARS = 64
_RECORD_FIELDS = {"features": FEATURE_FIELDS, "instruments": INSTRUMENT_FIELDS}
# The newest methodology a record of each kind may carry: the one this
# release computes (record_sig).
_NEWEST_ANALYSIS = {"features": rs.FEATURES_ANALYSIS_VERSION,
                    "instruments": rs.INSTRUMENTS_ANALYSIS_VERSION}
# The per-track record kinds the import gate takes: their table, their
# fields, the bytes their seal hashes and the payload it signs.
_RECORD_KINDS = {
    "features": ("audio_features", FEATURE_FIELDS, rs.canonical_features_blob,
                 rs.features_payload),
    "instruments": ("track_instruments", INSTRUMENT_FIELDS,
                    lambda item: rs.canonical_instruments_blob(item["instruments"]),
                    rs.instruments_payload),
}


def _is_number(value) -> bool:
    return _is_int(value) or (isinstance(value, float) and math.isfinite(value))


def _of_kind(kind: str, value) -> bool:
    """A value the column of its field's kind takes: a finite float, a text
    with no NUL, or none; for the scores, a map of label to score."""
    if kind == _SCORES:
        return (isinstance(value, dict) and len(value) <= INSTRUMENT_LABELS
                and all(isinstance(label, str) and 0 < len(label) <= INSTRUMENT_LABEL_CHARS
                        and "\x00" not in label and _is_number(score) and 0 <= score <= 1
                        and math.copysign(1, score) > 0
                        for label, score in value.items()))
    if value is None:
        return True
    if kind == _FLOAT:
        return isinstance(value, float) and math.isfinite(value)
    return isinstance(value, str) and "\x00" not in value


def _well_formed(item: dict, kind: str) -> Optional[str]:
    """Why an analysis item of `kind` (segments — a bundle —, features,
    instruments) is not one this node stores, or None — and then its
    provenance is normalised in place to the declaration its row is keyed
    by, exactly as the seal binds it: the fingerprint a string no longer
    than fpcalc's window can make (material.MAX_FINGERPRINT_CHARS; every
    later registration on its track compares with it), the duration whole
    seconds within a day or none, the grid the one this build makes — a
    bundle of another is refused (every segment seal binds it), a record's
    is ignored (no features or instruments seal binds it, so it must not
    move the row). `is_lossless` is the author's unsigned claim, kept when
    it is one. A record's methodology is no newer than this release computes
    (_NEWEST_ANALYSIS), and its fields are typed by its table
    (FEATURE_FIELDS, INSTRUMENT_FIELDS). A field of another type drops its
    item, never the batch."""
    bundle = kind == "segments"
    if not _is_uuid(item.get("track_uuid")):
        return "no track uuid"
    prov = item.get("provenance")
    if not isinstance(prov, dict):
        return "no material declaration"
    fp, duration = prov.get("chromaprint"), prov.get("duration_seconds")
    grid = prov.get("grid_version")
    if not isinstance(fp, str) or not fp:
        return "no fingerprint"
    if len(fp) > material.MAX_FINGERPRINT_CHARS:
        return "a fingerprint longer than fpcalc's window can make"
    if duration is not None and not (_is_int(duration) and 0 < duration <= 24 * 3600):
        return "a duration that is not whole seconds"
    if bundle and grid is not None and not (_is_int(grid) and grid == rs.GRID_VERSION):
        return "a grid this build does not make"
    version = item.get("analysis_version", 1)
    if not (_is_int(version) and 0 < version < 2 ** 15):
        return "an analysis version that is not one"
    if version > _NEWEST_ANALYSIS.get(kind, version):
        return "a methodology this release does not compute"
    if bundle and not (_is_uuid(item.get("model_uuid"))
                       and isinstance(item.get("model_name", ""), str)
                       and len(item.get("model_name", "")) <= MODEL_NAME_CHARS):
        return "no model"
    for field, of in _RECORD_FIELDS.get(kind, {}).items():
        if not _of_kind(of, item.get(field)):
            return f"a {field} that is not one"
    lossless = prov.get("is_lossless")
    item["provenance"] = {"chromaprint": fp, "duration_seconds": duration,
                          "grid_version": rs.GRID_VERSION,
                          "is_lossless": lossless if isinstance(lossless, bool) else None}
    return None


def _sealed(payload: bytes, signature, author, proof, root, batch_ok) -> bool:
    """A record's seal checks out end to end — author signature, Merkle
    proof, Worker timestamp — and every field of it is of its type."""
    try:
        return bool(rs.verify(payload, signature, author)
                    and rs.verify_proof(rs.record_leaf(signature), proof or [], root)
                    and batch_ok(root))
    except (TypeError, ValueError, AttributeError):
        return False


def _item_write(cur, what: str, track: str, write: Callable[[], None]) -> bool:
    """Run one item's writes under a savepoint: a value the database refuses
    (an enum, a check, a range) drops that item, logged, never its batch."""
    cur.execute("SAVEPOINT import_item")
    try:
        write()
    except (psycopg2.DataError, psycopg2.IntegrityError) as e:
        cur.execute("ROLLBACK TO SAVEPOINT import_item")
        cur.execute("RELEASE SAVEPOINT import_item")
        logger.warning("sync import: dropping %s %s — %s", what, track[:8],
                       str(e).splitlines()[0])
        return False
    cur.execute("RELEASE SAVEPOINT import_item")
    return True


def _page_write(cur, what: str, items: list, page: Callable[[], None],
                one: Callable[[dict], None]) -> int:
    """Write a page of items under one savepoint; only when the database
    refuses a value in it, write the page again item by item, each under its
    own (_item_write). A page is one subtransaction, not one per item —
    beyond 64 in one transaction PostgreSQL's per-session cache of them
    overflows and every concurrent snapshot pays. Returns the items written."""
    cur.execute("SAVEPOINT import_page")
    try:
        page()
    except (psycopg2.DataError, psycopg2.IntegrityError):
        cur.execute("ROLLBACK TO SAVEPOINT import_page")
        cur.execute("RELEASE SAVEPOINT import_page")
        return sum(_item_write(cur, what, it["track_uuid"], lambda it=it: one(it))
                   for it in items)
    cur.execute("RELEASE SAVEPOINT import_page")
    return len(items)


def batch_roots(item: dict) -> set:
    """The Merkle roots an item's seals name — a bundle's per segment."""
    segments = item.get("segments")
    named = ([s.get("batch_root") for s in segments if isinstance(s, dict)]
             if isinstance(segments, list) else [item.get("batch_root")])
    return {r for r in named if isinstance(r, str)}


def seal_authors(item: dict) -> set:
    """The authors an item's seals name — a bundle's per segment."""
    segments = item.get("segments")
    named = ([s.get("author_pubkey") for s in segments if isinstance(s, dict)]
             if isinstance(segments, list) else [item.get("author_pubkey")])
    return {a.lower() for a in named if isinstance(a, str)}


def _track_of(row) -> Optional[str]:
    track = row if isinstance(row, str) else row[0] if isinstance(row, list) and row else None
    return track if isinstance(track, str) else None


def _asked(rows, asked: set) -> list:
    """The inventory rows that name a track asked about: a plain uuid, or a
    row whose first field is one. A peer's word about any other track is no
    answer to the question."""
    return [r for r in rows if _track_of(r) in asked]


def answer_to(result: dict, asked: set) -> dict:
    """The part of a pull answer about the tracks asked, with the Worker
    timestamps those items' seals name and no others."""
    items = [it for it in result.get("items") or []
             if isinstance(it, dict) and _track_of(it.get("track_uuid")) in asked]
    roots = set().union(*(batch_roots(it) for it in items))
    batches = result.get("batches")
    batches = batches if isinstance(batches, dict) else {}
    return {**result, "items": items,
            "batches": {r: batches[r] for r in roots if r in batches}}


# A fetched holdings filter is used as-is for this long before the peer's
# version is even compared: a peer's version moves with every analysis
# pass it runs (the counts of both analysis tables are in it), and the syncs that
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
        deliverer: Optional[str] = None,
        carried: Optional[Callable[[dict, str, str], None]] = None,
        run_cache: Optional[dict] = None,
    ):
        # api_client is None on the receiving side of a push: the payload
        # arrives instead of being fetched, but it goes through the same
        # verify-and-import gate (see import_pushed). Who delivered a
        # payload, and where its authors' certificates go as `carried`
        # sightings: on a pull the peer's TLS-bound key and this node's
        # PeerIdentity sink; on a push what the server passes (_deliverer).
        self.api = api_client
        self.db_dsn = db_dsn
        self.batch_size = batch_size
        self.progress_cb = progress_cb
        self._delivered_by = (deliverer.lower(), carried) if deliverer and carried else None
        # peer pubkey -> {"version", "tracks": BloomFilter}: the peers'
        # published holdings, kept by the manager across runs (process
        # lifetime) and refreshed by version.
        self.holdings_cache = holdings_cache if holdings_cache is not None else {}
        # What a walk run reads of this node's holdings once for all the
        # peers it asks (the walk passes one per run): the peers are asked
        # one at a time, and a read gone stale meanwhile only routes a track
        # by the broader test.
        self.run_cache = run_cache if run_cache is not None else {}
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

    # The existence check is bounded by the INVENTORY, never by the
    # table: a filled node holds millions of rows per category and a run
    # asks about a few thousand — reading the whole table per category,
    # per peer, per run was seconds of pure waste on every sync.

    def _held_records(self, table: str, candidates) -> dict[str, tuple[int, bool]]:
        """Of `candidates`, uuid -> (local analysis_version, first-hand) of
        the record of `table` (audio_features, track_instruments) this node
        holds (sync_queries.HELD_RECORD): its own, or sealed. An imported
        row whose seal a shed removed is a gap."""
        candidates = list(candidates)
        if not candidates:
            return {}
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(f"""SELECT a.track_id::text, a.analysis_version,
                                   s.id IS NOT NULL AND NOT s.imported
                              FROM {table} a
                              LEFT JOIN analysis_sources s ON s.id = a.analysis_source_id
                             WHERE a.track_id = ANY(%s::uuid[])
                               AND {sync_queries.HELD_RECORD}""", [candidates])
            return {row[0]: (row[1], row[2]) for row in cur.fetchall()}

    def _lacking_only_instruments(self) -> set:
        """The tracks whose one want from a peer is their instruments: their
        segments held (a held grid is never replaced), a features record no
        pull would replace — first-hand, or sealed at the newest methodology
        (sync_queries.KEPT_RECORD) — and no instruments. Read from what is
        held, never from an asked list the phantom bulk makes millions long,
        once per walk run (run_cache)."""
        if "lacking_only_instruments" not in self.run_cache:
            conn = self._get_conn()
            with conn.cursor() as cur:
                # HOLDS_INSTRUMENTS names its own `a` and `s`: the track is
                # read from the CTE, never from an alias the subquery shadows.
                kept = sync_queries.KEPT_RECORD.format(v=_NEWEST_ANALYSIS["features"])
                cur.execute(f"""WITH sonic AS (
                                    SELECT a.track_id FROM audio_features a
                                      LEFT JOIN analysis_sources s ON s.id = a.analysis_source_id
                                     WHERE {kept}
                                       AND {sync_queries.HOLDS_SEGMENTS.format(t="a.track_id")})
                                SELECT x.track_id::text FROM sonic x
                                 WHERE NOT {sync_queries.HOLDS_INSTRUMENTS.format(t="x.track_id")}""")
                self.run_cache["lacking_only_instruments"] = {row[0] for row in cur.fetchall()}
        return self.run_cache["lacking_only_instruments"]

    def run_sync(self, track_uuids: list[str] = None,
                 bulk_track_uuids: Optional[list[str]] = None) -> dict:
        """
        Run full synchronization.

        Args:
            track_uuids: the CORE — tracks of engaged artists; None = every
                local track.
            bulk_track_uuids: the phantom BULK.

        Core and bulk are priced the same way against the peer's holdings
        filter: one filter download and a handful of exact questions about
        the hits when the ask is bigger than the filter, the exact
        inventory otherwise.

        Returns:
            dict with sync statistics per category.
        """
        try:
            return self._run_sync_inner(track_uuids, bulk_track_uuids or [])
        finally:
            self._close_conn()

    def _run_sync_inner(self, track_uuids: list[str],
                        bulk_track_uuids: list[str]) -> dict:
        # Step 1: Get track UUIDs
        if track_uuids is None:
            self._progress("Getting local track UUIDs...")
            track_uuids = self._get_local_track_uuids()

        if not track_uuids and not bulk_track_uuids:
            self._progress("No tracks to sync.")
            return {"total_tracks": 0}

        self._progress(
            f"Syncing analysis for {len(track_uuids)} tracks"
            + (f" + {len(bulk_track_uuids)} phantom-bulk tracks"
               if bulk_track_uuids else "") + "...")

        # Step 2: Capability probe — does the peer serve sealed segments?
        health = self.api.get_health() or {}
        self.peer_capabilities = set(health.get("capabilities") or [])
        use_segments = "segments" in self.peer_capabilities
        if not use_segments:
            self._progress(
                "  peer has no `segments` capability — no embeddings from it")

        # Step 3: Inventory — what the peer holds of our gaps, core and bulk
        # alike: the exact inventory when our ask weighs less than the
        # peer's holdings filter (or the peer publishes none), otherwise
        # the filter — fetched once per version, tested locally — and the
        # exact inventory only about the hits. Until 2026-09-08 the core
        # was always asked in full: a 3M-track master paid 48 exact
        # requests per peer per run for what the filter answers in a
        # second.
        inventory = self._holdings_inventory(
            health, list(track_uuids) + list(bulk_track_uuids))
        if inventory is None:
            self._progress("Inventory request failed.")
            return {"error": "inventory_failed"}

        # Step 4: Filter out what we already have locally
        needed = self._compute_needed(inventory, use_segments)

        # Step 5: Pull and import each category in batches
        stats = {}
        for cat_key, pull_endpoint, _ in CATEGORIES:
            if cat_key == "segments" and not use_segments:
                continue
            uuids_to_pull = needed.get(cat_key, [])
            if not uuids_to_pull:
                stats[cat_key] = 0
                continue

            imported = self._pull_and_import_category(
                cat_key, pull_endpoint, uuids_to_pull
            )
            stats[cat_key] = imported

        total = sum(stats.values())
        self._progress(f"Sync complete. Imported {total} items across {len(stats)} categories.")
        return stats

    def _ask(self, ask: Callable[[], Optional[dict]], key: str, what: str) -> Optional[dict]:
        """`ask()`'s answer when it holds `key`, asked at most twice: again
        after the wait a busy or rate-limited peer names (its Retry-After,
        120 s at most — retrying sooner lands inside the same window), else
        after 2 s. A refusal under load is a wait, and a failure is mostly
        transient — a TLS handshake interrupted, an HMAC timestamp drifting
        in a CPU stall, a keep-alive race —: neither ends the run's step."""
        for attempt in (1, 2):
            answer = ask()
            if answer and key in answer:
                return answer
            if attempt == 1:
                wait = 2
                if answer and answer.get("retry_after"):
                    wait = min(int(answer["retry_after"]), 120)
                    logger.info(f"{what}: the peer is busy, re-asking in {wait}s")
                else:
                    logger.warning(f"{what} failed (attempt 1), retrying in {wait}s")
                time.sleep(wait)
        return None

    def _fetch_inventory(self, track_uuids: list[str]) -> Optional[dict]:
        """Inventory in ≤INVENTORY_CHUNK slices, merged: chunks are disjoint
        track sets, so list values concatenate."""
        requests = [track_uuids[i:i + INVENTORY_CHUNK]
                    for i in range(0, len(track_uuids), INVENTORY_CHUNK)]
        merged: dict = {k: [] for k in EMPTY_INVENTORY}
        for i, tracks in enumerate(requests, 1):
            t0 = time.monotonic()
            part = self._ask(lambda: self.api.sync_inventory(tracks), "tracks",
                             f"Inventory {i}/{len(requests)}")
            if part is None:
                return None
            asked = set(tracks)
            for k, v in part.items():
                if isinstance(v, list):
                    merged.setdefault(k, []).extend(_asked(v, asked))
            self._progress(
                f"  inventory {i}/{len(requests)}: {len(tracks)} tracks "
                f"in {time.monotonic() - t0:.1f}s")
        return merged

    def _holdings_inventory(self, health: dict, tracks: list[str]) -> Optional[dict]:
        """Inventory for our gaps by the cheaper direction: the peer's
        holdings filters (one download, cached by version, tested locally)
        when the ask is bigger than the filters, the exact inventory
        otherwise or when the peer has none. A track whose one want is its
        instruments (_lacking_only_instruments) is tested against the peer's
        instruments filter alone — the peer holding its segments says nothing
        of its instruments, and such a gap no peer can fill would otherwise
        be an inventory request every run; every other track against the
        tracks filter, as before: a peer that holds its segments or features
        is asked about it, and its instruments come in the same inventory —
        so a features record a newer methodology would replace keeps its
        track on the tracks filter. A peer that publishes no instruments
        filter holds no instruments this node can verify. An empty inventory
        when nothing is worth asking; None when a request failed."""
        if "holdings" not in self.peer_capabilities:
            self._progress(
                f"  peer has no holdings filter — asking about "
                f"{len(tracks)} tracks directly")
            return self._fetch_inventory(tracks)

        key = self.api.peer_pubkey or self.api.base_url
        cached = self.holdings_cache.get(key)
        summary = health.get("holdings")
        if not (isinstance(summary, dict)
                and all(_is_int(summary.get(k, 0)) for k in ("tracks", "instruments"))):
            summary = None
        fresh = cached and time.time() - cached["fetched_at"] < HOLDINGS_MAX_AGE_S
        if fresh or (cached and summary
                     and summary.get("version") == cached["version"]):
            filters = cached
        else:
            # Price the two directions: ~40 bytes per uuid on the wire
            # against ~1.2 bytes per held element in the filter.
            ask_cost = len(tracks) * 40
            if summary:
                fetch_cost = (summary.get("tracks", 0) + summary.get("instruments", 0)) * 1.2
                if fetch_cost > ask_cost:
                    self._progress(
                        f"  {len(tracks)} track gaps weigh less than the "
                        f"peer's filter — asking directly")
                    return self._fetch_inventory(tracks)
            payload = self._ask(
                lambda: self.api.sync_holdings(cached["version"] if cached else None),
                "version", "Holdings")
            if payload is None:
                self._progress("  holdings request failed")
                return None
            if payload.get("unchanged") and cached:
                filters = cached
                filters["fetched_at"] = time.time()
            else:
                try:
                    held = BloomFilter.from_dict(payload.get("tracks"))
                    instruments = (BloomFilter.from_dict(payload["instruments"])
                                   if "instruments" in payload else None)
                except ValueError as e:
                    self._progress(f"  holdings filter refused: {e}")
                    return None
                filters = {"version": payload["version"],
                           "fetched_at": time.time(),
                           "tracks": held, "instruments": instruments}
                self.holdings_cache[key] = filters
                self._progress(
                    f"  holdings filter fetched: {filters['tracks'].n} tracks "
                    f"held by the peer"
                    + (f", {instruments.n} with instruments" if instruments else ""))

        only_instruments = self._lacking_only_instruments()
        hits = filters["tracks"].hits([t for t in tracks if t not in only_instruments])
        if filters.get("instruments") is not None:
            hits += filters["instruments"].hits([t for t in tracks if t in only_instruments])
        self._progress(
            f"  holdings filter: {len(hits)} of {len(tracks)} tracks worth asking")
        if not hits:
            return dict(EMPTY_INVENTORY)
        return self._fetch_inventory(hits)

    def _protected_analysis_tracks(self, uuids: list[str]) -> set:
        """Tracks whose local analysis a peer import must never replace: what
        this node holds (sync_queries.HELD_EMBEDDING) — first-hand (my own
        decode) or a sealed grid. Gap-fill only — densification / version
        upgrades of held grids are the deferred "deepen analysis" path."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT e.track_id::text FROM embeddings e
                    LEFT JOIN analysis_sources s ON s.id = e.analysis_source_id
                    WHERE e.track_id = ANY(%s::uuid[])
                      AND {sync_queries.HELD_EMBEDDING}""",
                [uuids],
            )
            return {row[0] for row in cur.fetchall()}

    def _local_segment_versions(self, candidates) -> dict[str, int]:
        """Of `candidates`, uuid -> local analysis_version for the tracks
        whose analysis this node holds (sync_queries.HELD_EMBEDDING). A mean
        without segments, or an imported grid a shed unsealed, is a gap:
        nothing seals it, so sealed segments are pulled to replace it."""
        candidates = list(candidates)
        if not candidates:
            return {}
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(f"""SELECT e.track_id::text, MAX(e.analysis_version)
                              FROM embeddings e
                              LEFT JOIN analysis_sources s ON s.id = e.analysis_source_id
                             WHERE e.track_id = ANY(%s::uuid[])
                               AND {sync_queries.HELD_EMBEDDING}
                             GROUP BY e.track_id""", [candidates])
            return {row[0]: row[1] for row in cur.fetchall()}

    def _compute_needed(self, inventory: dict, use_segments: bool) -> dict:
        """Compare inventory with local data to find what's missing.

        Segments are planned from the `embeddings` inventory key, whose rows
        are [uuid, analysis_version, sealed segment count] — a row with no
        sealed segment offers nothing to pull — against the local rows this
        node holds (sync_queries.HELD_*: its own, or sealed); a held grid is
        never replaced (_protected_analysis_tracks). The features and the
        instruments, [uuid, analysis_version], against the records held: a
        held record is still pulled when the source's methodology is newer
        and the record is not this node's own — an imported record of a
        newer methodology replaces an imported one of an older, never a
        first-hand one (sync_queries.KEPT_RECORD, which the importers keep
        by), so nothing is pulled only to be dropped; and a methodology newer
        than this release computes is none at all (_NEWEST_ANALYSIS: the gate
        refuses it). Existence alone never delivers upgrades."""
        needed = {}
        checks = [("audio_features", "audio_features",
                   partial(self._held_records, "audio_features"), _NEWEST_ANALYSIS["features"]),
                  ("instruments", "instruments", partial(self._held_records, "track_instruments"),
                   _NEWEST_ANALYSIS["instruments"])]
        if use_segments:
            checks.insert(0, ("segments", "embeddings", self._local_segment_versions,
                              2 ** 15 - 1))
        for cat_key, inv_key, local_versions, newest in checks:
            t0 = time.monotonic()
            rows = inventory.get(inv_key) or []
            available_versions = {
                row[0]: row[1] for row in rows
                if isinstance(row, list) and len(row) >= 2 and _is_int(row[1])
                and 0 < row[1] <= newest
                and (cat_key != "segments" or (len(row) > 2 and row[2]))}
            if not available_versions:
                continue
            local = local_versions(available_versions)
            new = [u for u in available_versions if u not in local]
            if cat_key == "segments":
                outdated = [u for u, v in available_versions.items()
                            if u in local and local[u] < v]
            else:
                outdated = [u for u, v in available_versions.items()
                            if u in local and not local[u][1] and local[u][0] < v]
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
        return needed

    def _note_slow(self, cat_key: str, t0: float) -> None:
        """A local existence check is a handful of indexed lookups; when it
        takes seconds the node's database is the bottleneck (a sync went
        silent for six minutes here on 2026-09-06) — say so, with the time."""
        dt = time.monotonic() - t0
        if dt > 2.0:
            self._progress(f"  {cat_key}: local check took {dt:.1f}s")

    def _pull_and_import_category(
        self, cat_key: str, pull_endpoint: str, uuids: list[str]
    ) -> int:
        """Pull and import a single category in batches, each asked as
        `_ask` asks; a batch that fails twice logs a warning, and the
        broader incomplete-artist sync flow picks up the gap on the next
        pass."""
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

            result = self._ask(lambda: self.api.sync_pull(pull_endpoint, batch_uuids),
                               "items", f"Pull {cat_key} batch {batch_idx}")
            if result is None:
                logger.warning(
                    f"Pull {cat_key} batch {batch_idx} failed after retry, "
                    f"skipping {len(batch_uuids)} uuids"
                )
                continue

            result = answer_to(result, set(batch_uuids))
            if not result["items"]:
                continue

            imported = self._import_items(cat_key, result)
            total_imported += imported

        return total_imported

    def _import_items(self, category: str, result: dict) -> int:
        """Import a pull response into the local database — a category of
        _IMPORTERS only, whatever a payload names itself (a pull, a push,
        a share file)."""
        importer = self._IMPORTERS.get(category)
        if importer is None:
            logger.warning(f"No importer for category: {category}")
            return 0
        importer = importer.__get__(self)

        authors: set = set()          # of the records whose seal verified
        conn = self._get_conn()
        try:
            # Sealed categories also need the response-level Worker-timestamp
            # map to verify (and re-serve) the full chain.
            if category in ("segments", "audio_features", "instruments"):
                count = importer(conn, result["items"],
                                 result.get("batches") or {}, authors)
            else:
                items = result["items"]
                # Sealed plain rows verify here rather than inside the
                # importer: one gate, and no way to add a category that
                # quietly skips it.
                kind = _ENRICHMENT_KIND_BY_CATEGORY.get(category)
                if kind:
                    items = self._verify_enrichment(
                        kind, items, result.get("batches") or {})
                    if not items:
                        return 0
                    authors.update(*(seal_authors(it) for it in items))
                    self._insert_batches_for(conn, result.get("batches") or {},
                                             items)
                count = importer(conn, items)
            conn.commit()
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
        self._note_carried(result.get("authors"), authors)
        return count

    # -- Witnessing the authors a payload carried (BIRTH-SYNC slice 2) -------

    def _deliverer(self) -> Optional[tuple]:
        """(the deliverer's key, the `carried` sink), or None when nobody
        delivered the payload: a share file, an unsigned push."""
        if self._delivered_by is not None:
            return self._delivered_by
        sink = getattr(getattr(self.api, "peer", None), "carried", None)
        via = getattr(self.api, "peer_pubkey", None)
        return (via.lower(), sink) if sink is not None and via else None

    def _note_carried(self, entries, authors: set) -> None:
        """The certificate the payload's `authors` map carried for each author
        of a record whose seal verified goes on this node's witness record as
        a `carried` sighting delivered by the peer. The record's own check
        decides, as on every channel (desktop/p2p/witness.py): a certificate
        valid for that author, no proof read. The deliverer is no carried
        author — it introduced itself, or this node dialed it — and a payload
        nobody delivered is no sighting at all. The sink never raises."""
        delivered = self._deliverer()
        if delivered is None or not isinstance(entries, dict):
            return
        via, sink = delivered
        by_key = {k.lower(): v for k, v in entries.items() if isinstance(k, str)}
        for author in sorted(authors - {via}):
            entry = by_key.get(author)
            cert = entry.get("author_cert") if isinstance(entry, dict) else None
            if isinstance(cert, dict):
                sink(cert, author, via)

    # -- Seal verification (verify-on-import) -------------------------------

    # Which wire field names the entity a record hangs off.
    _ENRICHMENT_ENTITY = {"track_mbid": "track_uuid"}

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
                try:
                    cache[root] = bool(
                        isinstance(b, dict) and b.get("authority") in TRUSTED_AUTHORITIES
                        and rs.verify_timestamp(
                            root, b["worker_date"], b["ip_hash"],
                            b["worker_sig"], b["authority"],
                            int(b.get("timestamp_version") or rs.TIMESTAMP_VERSION)))
                except (KeyError, TypeError, ValueError, AttributeError):
                    cache[root] = False     # a malformed entry is no valid batch
            return cache[root]

        return ok

    @staticmethod
    def _insert_batches(cur, batches: dict, roots: set):
        """Store the Worker-timestamp rows referenced by verified seals, so
        this node re-serves the full chain (relay keeps authorship intact).
        A peer on the wire format before timestamp_version sends none — its
        batches are the one format that ever existed."""
        if not roots:
            return
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO signing_batches
               (batch_root, author_pubkey, worker_date, ip_hash,
                worker_sig, authority, timestamp_version)
               VALUES %s ON CONFLICT (batch_root) DO NOTHING""",
            [(r, batches[r]["author_pubkey"], batches[r]["worker_date"],
              batches[r]["ip_hash"], batches[r]["worker_sig"],
              batches[r]["authority"],
              int(batches[r].get("timestamp_version") or rs.TIMESTAMP_VERSION))
             for r in roots],
            template="(%s, %s, %s::timestamptz, %s::uuid, %s, %s, %s)",
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

    def _import_segments(self, conn, items: list[dict], batches: dict, authors: set) -> int:
        """Import per-track segment bundles: verify every seal, store the
        segments seal-intact, derive the track mean locally. A bundle with ANY
        invalid OR MISSING seal is dropped whole — a broken seal on
        TLS-protected wire is evidence of tampering, and an unsigned one is a
        claim this node would go on to redistribute unattributably. Returns
        the count of imported tracks; `authors` gains the author of every
        bundle whose seals all verified under one key."""
        batch_ok = self._batch_verifier(batches)

        # Decode + verify outside the write path: (item, [(idx, floats, seal)])
        decoded = []
        for item in items:
            why = _well_formed(item, "segments")
            if why:
                logger.warning("sync import: dropping track %s — %s",
                               str(item.get("track_uuid", "?"))[:8], why)
                continue
            prov = item["provenance"]
            if not _on_the_grid(item.get("segments"), prov["duration_seconds"]):
                logger.warning("sync import: dropping track %s — segments off the grid",
                               str(item.get("track_uuid", "?"))[:8])
                continue
            if not _names_its_model(item):
                logger.warning("sync import: dropping track %s — its model id is not its "
                               "model name's", str(item.get("track_uuid", "?"))[:8])
                continue
            segs, bad = [], None
            for s in item.get("segments", []):
                try:
                    raw = base64.b64decode(s["v"], validate=True)
                    floats = rs.vector_from_bytes(raw)
                except (KeyError, ValueError, TypeError, struct.error):
                    bad = f"segment {s.get('i')} undecodable"
                    break
                if len(floats) != VECTOR_DIM:
                    bad = f"segment {s.get('i')} of {len(floats)} dimensions"
                    break
                if not is_unit(floats):
                    bad = f"segment {s.get('i')} not a unit vector"
                    break
                if not s.get("signature"):
                    bad = f"segment {s.get('i')} unsigned"
                    break
                try:
                    payload = rs.segment_payload(
                        s.get("author_pubkey") or "", item["track_uuid"],
                        prov["chromaprint"], prov["duration_seconds"],
                        item["model_uuid"], s["i"], rs.vector_hash(raw),
                        grid_version=prov["grid_version"])
                except (ValueError, TypeError, AttributeError):
                    payload = None
                if not (payload
                        and _sealed(payload, s.get("signature"), s.get("author_pubkey"),
                                    s.get("proof"), s.get("batch_root"), batch_ok)):
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
                # An honest bundle is one author's analysis, sealed by one
                # key; one that mixes keys imports all the same, but credits
                # no author: one bundle must not carry a certificate per key.
                sealed_by = {seal[0].lower() for _i, _f, seal in segs}
                if len(sealed_by) == 1:
                    authors.update(sealed_by)

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

            # Import-side guard (the planner prefilters, the importer
            # enforces): never replace what this node holds — first-hand
            # analysis or a sealed grid. Dropped first, so provenance is
            # registered only for what is kept: a dropped item leaves no
            # imported row that nothing links to.
            kept = self._drop_protected(
                cur, [item for item, _ in decoded],
                f"""SELECT e.track_id::text FROM embeddings e
                    LEFT JOIN analysis_sources s ON s.id = e.analysis_source_id
                    WHERE e.track_id = ANY(%s::uuid[])
                      AND {sync_queries.HELD_EMBEDDING}""",
                "segments")
            kept_uuids = {i["track_uuid"] for i in kept}
            if not kept_uuids:
                return 0
            source_map = self._upsert_analysis_sources(cur, kept)

            self._insert_batches(cur, batches, {
                seal[2]
                for item, segs in decoded if item["track_uuid"] in kept_uuids
                for _, _, seal in segs if seal})

            # one bundle per track, the first: a track's grid is one author's
            bundles: dict = {}
            for item, segs in decoded:
                if item["track_uuid"] in kept_uuids:
                    bundles.setdefault(item["track_uuid"], (item, segs))
            writing = [item for item, _segs in bundles.values()]

            def one(item):
                self._write_bundle(cur, item, bundles[item["track_uuid"]][1], source_map)

            def page():
                for item in writing:
                    one(item)
            return _page_write(cur, "track", writing, page, one)

    def _write_bundle(self, cur, item: dict, segs: list, source_map: dict) -> None:
        """One bundle's mean and segments, replacing the track's grid."""
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

    @staticmethod
    def _upsert_analysis_sources(cur, items: list[dict]) -> dict:
        """Upsert the distinct provenance rows carried by analysis items and
        return {(track_uuid, chromaprint, duration): analysis_sources.id}. media_file_id
        is always NULL and imported=true — the sender's material is not ours,
        so these sources are never signable here (sign_audio excludes
        imported).

        Each item is linked to the IMPORTED row of its own declaration only —
        the print and the duration its seal binds: its own twin when this
        node registered the declaration first-hand too (an author's records
        under this node's own row would read as this node's, and be signed
        by it after the next shed of their seals), and a row of its own when
        another author declared another duration of the print (its records,
        under that row, would no longer verify). A row new here joins its
        track's material group (material.root_among: an equal print within
        the duration bound with no comparison, else the first row the rule
        matches); one seen before is never compared again.

        `provider_id` stays NULL for imported rows, and honestly so: the wire
        does not say whether the author analysed a file or a stream (see
        _provenance_item), and the column exists only to rank OUR OWN
        analysis. Every item here passed its seal check, so the fingerprint
        is in the alphabet the row key's decode() expects."""
        prov = {}
        for item in items:
            p = item["provenance"]
            prov[(item["track_uuid"], p["chromaprint"], p["duration_seconds"])] = p
        if not prov:
            return {}
        values = [
            (tid, fp, duration, p["grid_version"], p["is_lossless"])
            for (tid, fp, duration), p in prov.items()
        ]
        rows = psycopg2.extras.execute_values(
            cur,
            """INSERT INTO analysis_sources
               (track_id, chromaprint, duration_seconds,
                grid_version, is_lossless, imported)
               VALUES %s
               ON CONFLICT (track_id, chromaprint_key, duration_seconds, imported, copy_rank)
                   -- a write to no key column: the row lock it takes does not
                   -- wait on a registration's hold of the row as a root
                   DO UPDATE SET grid_version = analysis_sources.grid_version
               RETURNING id, track_id::text, chromaprint, duration_seconds, xmax = 0""",
            values,
            template="(%s::uuid, %s, %s, %s, %s, true)",
            fetch=True,
        )
        SyncClient._join_groups(cur, sorted((r[0], r[1]) for r in rows if r[4]))
        return {(r[1], r[2], r[3]): r[0] for r in rows}

    @staticmethod
    def _join_groups(cur, new_rows: list) -> None:
        """Put each new row — (id, track uuid), in id order — in its track's
        material group."""
        if not new_rows:
            return
        cur.execute("""SELECT id, track_id::text, material_of, chromaprint, duration_seconds
                         FROM analysis_sources WHERE track_id = ANY(%s::uuid[])
                        ORDER BY id""", [sorted({t for _i, t in new_rows})])
        by_track: dict = {}
        for r in cur.fetchall():
            by_track.setdefault(r[1], []).append([r[0], r[2], r[3], r[4]])
        new_ids = {sid for sid, _t in new_rows}
        joined = []
        for sid, track in new_rows:
            rows = by_track[track]
            me = next(r for r in rows if r[0] == sid)
            # among the rows before it: those here already, and the new
            # ones that joined before it
            root = material.root_among(me[2], me[3], [r for r in rows if r is not me and (
                r[0] not in new_ids or r[0] < sid)])
            if root is not None:
                me[1] = root
                joined.append((sid, root))
        if joined:
            # held while the rows join them: a root a merge deleted meanwhile
            # leaves its joiner a root of its own, one it re-rooted hands
            # over its new root
            cur.execute("SELECT id, material_of FROM analysis_sources WHERE id = ANY(%s) "
                        "FOR KEY SHARE", [sorted({r for _s, r in joined})])
            held = dict(cur.fetchall())
            joined = [(sid, held[root] or root) for sid, root in joined if root in held]
        if joined:
            psycopg2.extras.execute_values(
                cur, """UPDATE analysis_sources s SET material_of = v.root
                          FROM (VALUES %s) AS v(id, root) WHERE s.id = v.id""",
                joined, template="(%s, %s)")

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
    def _source_id(source_map: dict, item: dict) -> int:
        p = item["provenance"]
        return source_map[(item["track_uuid"], p["chromaprint"], p["duration_seconds"])]

    @staticmethod
    def _drop_kept(cur, items: list[dict], table: str, what: str) -> list[dict]:
        """Filter out the items — one per track — whose track's record of
        `table` this node keeps against them (sync_queries.KEPT_RECORD): its
        own first-hand record, or a sealed one of a methodology no older than
        the item's — an imported record of an older methodology is replaced
        by a newer one, as the planner asked for it."""
        if not items:
            return items
        cur.execute(f"""SELECT i.track_id::text
                          FROM unnest(%s::uuid[], %s::int[]) AS i(track_id, version)
                          JOIN {table} a ON a.track_id = i.track_id
                          LEFT JOIN analysis_sources s ON s.id = a.analysis_source_id
                         WHERE {sync_queries.KEPT_RECORD.format(v="i.version")}""",
                    ([i["track_uuid"] for i in items],
                     [i.get("analysis_version", 1) for i in items]))
        kept = {r[0] for r in cur.fetchall()}
        if kept:
            logger.debug("sync import: %d %s items kept out (own or no older)", len(kept), what)
        return [i for i in items if i["track_uuid"] not in kept]

    def _import_audio_features(self, conn, items: list[dict],
                               batches: dict, authors: set) -> int:
        return self._import_records(conn, items, batches, authors, "features")

    def _import_instruments(self, conn, items: list[dict],
                            batches: dict, authors: set) -> int:
        return self._import_records(conn, items, batches, authors, "instruments")

    def _import_records(self, conn, items: list[dict], batches: dict, authors: set,
                        kind: str) -> int:
        """Import the per-track records of `kind` (_RECORD_KINDS: features,
        instruments) whose seal verifies; `authors` gains the author of
        each."""
        table, fields, digest, payload_of = _RECORD_KINDS[kind]
        batch_ok = self._batch_verifier(batches)

        # Verify-on-import: a row must verify end-to-end (author sig → Merkle
        # proof → Worker timestamp over the transmitted values) or it drops.
        # Unsigned rows drop too — see _verify_enrichment for why: we would
        # redistribute them, and an unattributable claim must not travel with
        # our address on it. A record of an older format of its kind (a v3
        # features record) is rebuilt in this one and fails here.
        verified = []
        for item in items:
            if not item.get("signature"):
                logger.warning(
                    "sync import: dropping unsigned %s %s", kind,
                    item.get("track_uuid", "?")[:8])
                continue
            why = _well_formed(item, kind)
            if why:
                logger.warning("sync import: dropping %s %s — %s",
                               kind, str(item.get("track_uuid", "?"))[:8], why)
                continue
            prov = item["provenance"]
            try:
                payload = payload_of(
                    item.get("author_pubkey") or "", item["track_uuid"],
                    prov["chromaprint"], prov["duration_seconds"],
                    item.get("analysis_version", 1), rs.blake2b_hex(digest(item)))
            except (KeyError, ValueError, TypeError, AttributeError):
                payload = None
            if not (payload
                    and _sealed(payload, item["signature"], item.get("author_pubkey"),
                                item.get("merkle_proof"), item.get("batch_root"), batch_ok)):
                logger.warning(
                    "sync import: dropping %s %s — seal invalid", kind,
                    item.get("track_uuid", "?")[:8])
                continue
            verified.append(item)
            authors.add(item["author_pubkey"].lower())
        # One record per track: of a track named twice, its newest
        # methodology (the first of equals), as a later pull of it would land.
        newest: dict = {}
        for item in verified:
            held = newest.get(item["track_uuid"])
            if held is None or item.get("analysis_version", 1) > held.get("analysis_version", 1):
                newest[item["track_uuid"]] = item
        items = list(newest.values())
        if not items:
            return 0

        with conn.cursor() as cur:
            # The data write never replaces what this node keeps: its own
            # first-hand analysis — file or stream, the line the segments
            # importer draws — or a SEALED row of a methodology no older
            # (overwriting would strip its seal via the guard trigger).
            # Provenance registers for the kept items only.
            items = self._drop_kept(cur, items, table, kind)
            if not items:
                return 0
            source_map = self._upsert_analysis_sources(cur, items)
            self._insert_batches(
                cur, batches,
                {i["batch_root"] for i in items if i.get("signature")})
            columns = list(fields) + ["analysis_source_id", "analysis_version",
                                      "author_pubkey", "signature", "batch_root", "merkle_proof"]
            upsert = f"""INSERT INTO {table} (track_id, {", ".join(columns)})
                   VALUES %s
                   ON CONFLICT (track_id) DO UPDATE SET
                       {", ".join(f"{c} = EXCLUDED.{c}" for c in columns)},
                       updated_at = CURRENT_TIMESTAMP"""
            template = "(" + ", ".join(["%s"] * (len(columns) + 1)) + ")"
            values = {
                item["track_uuid"]: (
                    item["track_uuid"],
                    *(psycopg2.extras.Json(item[f]) if of == _SCORES else item.get(f)
                      for f, of in fields.items()),
                    self._source_id(source_map, item), item.get("analysis_version", 1),
                    item["author_pubkey"], item["signature"], item["batch_root"],
                    psycopg2.extras.Json(item["merkle_proof"]))
                for item in items
            }
            return _page_write(
                cur, kind, items,
                lambda: psycopg2.extras.execute_values(cur, upsert, list(values.values()),
                                                       template=template, page_size=500),
                lambda item: psycopg2.extras.execute_values(cur, upsert, [values[item["track_uuid"]]],
                                                            template=template))

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

    # The categories an import takes, and their importers. Every one of them
    # verifies seals; nothing outside this map is imported.
    _IMPORTERS = {
        "segments": _import_segments,
        "audio_features": _import_audio_features,
        "instruments": _import_instruments,
        "track_mbids": _import_track_mbids,
    }


def import_pushed(db_dsn: str, category: str, payload: dict, budget: int,
                  deliverer: Optional[str] = None,
                  carried: Optional[Callable[[dict, str, str], None]] = None) -> int:
    """Verify and import a payload a peer PUSHED to us.

    The receiving side of push-seeding deliberately reuses the pull path's
    import gate rather than growing a second one: same seal verification,
    same first-hand protection, same precedence rules. A pushed record gets
    exactly the scrutiny a pulled one does — the only difference is who
    started the conversation. Before that, the push is cut to what this
    node's own answer to an offer would have asked for
    (sync_queries.wanted_for_push): its tracklist, the gaps, the carry
    limit's room, the phantom switch — a pusher chooses nothing else.

    `deliverer` is the key that signed the push (None for an unsigned one),
    `carried` the server's witness sink: the authors' certificates the push
    carried go on record as delivered by that key (SyncClient._note_carried).
    """
    if category not in CARRY_CATEGORIES:
        raise ValueError(f"category not carryable: {category}")
    client = SyncClient(api_client=None, db_dsn=db_dsn, deliverer=deliverer, carried=carried)
    try:
        items = [it for it in payload.get("items") or [] if isinstance(it, dict)]
        kept = sync_queries.wanted_for_push(client._get_conn(), category, items, budget)
        if len(kept) < len(items):
            logger.info("carry push: %d of %d %s item(s) not asked for — dropped",
                        len(items) - len(kept), len(items), category)
        if not kept:
            return 0
        return client._import_items(category, answer_to(
            {**payload, "items": kept}, {it["track_uuid"] for it in kept}))
    finally:
        client._close_conn()
