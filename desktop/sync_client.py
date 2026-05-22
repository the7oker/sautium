"""
P2P Sync client for Sautium.

Orchestrates enrichment data synchronization from a remote source
(backend API or future P2P peer) into the local database.

Protocol:
  1. Inventory — ask source what enrichment data it has for our tracks
  2. Plan — determine what we need, split into batches
  3. Pull — fetch data batch by batch
  4. Import — insert into local PostgreSQL
"""

import logging
from typing import Optional, Callable

import psycopg2
import psycopg2.extras

from desktop.api_client import BackendAPIClient

logger = logging.getLogger(__name__)

# Categories and their pull endpoint names
CATEGORIES = [
    # (category_key_in_inventory, pull_endpoint, uuid_column_hint)
    ("lyrics", "lyrics", "track"),
    ("embeddings", "embeddings", "track"),
    ("audio_features", "audio-features", "track"),
    ("track_stats", "track-stats", "track"),
    ("artist_bios", "artist-bios", "artist"),
    ("artist_tags", "artist-tags", "artist"),
    ("similar_artists", "similar-artists", "artist"),
    ("artist_members", "artist-members", "artist"),
    ("album_info", "album-info", "album"),
    ("album_tags", "album-tags", "album"),
    ("genre_descriptions", "genre-descriptions", "genre"),
]

DEFAULT_BATCH_SIZE = 500


class SyncClient:
    """Synchronizes enrichment data from a remote source into local DB."""

    def __init__(
        self,
        api_client: BackendAPIClient,
        db_dsn: str,
        batch_size: int = DEFAULT_BATCH_SIZE,
        progress_cb: Optional[Callable[[str], None]] = None,
    ):
        self.api = api_client
        self.db_dsn = db_dsn
        self.batch_size = batch_size
        self.progress_cb = progress_cb
        self._conn: Optional[psycopg2.extensions.connection] = None

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

    def _log_missing_tracks(self, missing_uuids: set[str]):
        """Log details of tracks not found at source."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT t.id::text, t.title, a.name AS artist
                   FROM tracks t
                   LEFT JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
                   LEFT JOIN artists a ON a.id = ta.artist_id
                   WHERE t.id = ANY(%s::uuid[])""",
                [list(missing_uuids)],
            )
            for row in cur.fetchall():
                artist = row[2] or "Unknown"
                logger.warning(
                    f"  Not at source: {artist} - {row[1]} ({row[0][:8]}...)"
                )

    def _get_local_track_uuids(self) -> list[str]:
        """Get all track UUIDs from the local database."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT id::text FROM tracks ORDER BY id")
            return [row[0] for row in cur.fetchall()]

    def _get_existing_uuids(self, table: str, uuid_col: str) -> set[str]:
        """Get UUIDs that already have data in a local enrichment table."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT DISTINCT {uuid_col}::text FROM {table}")
                return {row[0] for row in cur.fetchall()}
        except psycopg2.errors.UndefinedTable:
            conn.rollback()
            return set()

    def run_sync(self, track_uuids: list[str] = None) -> dict:
        """
        Run full synchronization.

        Args:
            track_uuids: specific track UUIDs to sync, or None for all local tracks.

        Returns:
            dict with sync statistics per category.
        """
        try:
            return self._run_sync_inner(track_uuids)
        finally:
            self._close_conn()

    def _run_sync_inner(self, track_uuids: list[str] = None) -> dict:
        # Step 1: Get track UUIDs
        if track_uuids is None:
            self._progress("Getting local track UUIDs...")
            track_uuids = self._get_local_track_uuids()

        if not track_uuids:
            self._progress("No tracks to sync.")
            return {"total_tracks": 0}

        self._progress(f"Syncing enrichment for {len(track_uuids)} tracks...")

        # Step 2: Inventory — ask source what it has
        self._progress("Requesting inventory...")
        inventory = self.api.sync_inventory(track_uuids)
        if not inventory:
            self._progress("Inventory request failed.")
            return {"error": "inventory_failed"}

        # Log tracks not found at source
        source_tracks = set(inventory.get("tracks", []))
        not_found = set(track_uuids) - source_tracks
        if not_found:
            self._progress(
                f"  {len(not_found)} tracks not found at source"
            )
            self._log_missing_tracks(not_found)

        # Step 3: Filter out what we already have locally
        needed = self._compute_needed(inventory)

        # Step 4: Pull and import each category in batches
        stats = {}
        for cat_key, pull_endpoint, _ in CATEGORIES:
            uuids_to_pull = needed.get(cat_key, [])
            if not uuids_to_pull:
                stats[cat_key] = 0
                continue

            imported = self._pull_and_import_category(
                cat_key, pull_endpoint, uuids_to_pull
            )
            stats[cat_key] = imported

        # Step 5: Recompute artist gender and vocalist status from imported bios
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

    def _compute_needed(self, inventory: dict) -> dict:
        """Compare inventory with local data to find what's missing."""
        # Map category to (table, uuid_column) for local existence check
        local_check = {
            "lyrics": ("track_lyrics", "track_id"),
            "embeddings": ("embeddings", "track_id"),
            "audio_features": ("audio_features", "track_id"),
            "track_stats": ("track_stats", "track_id"),
            "artist_bios": ("artist_bios", "artist_id"),
            "artist_tags": ("artist_tags", "artist_id"),
            "similar_artists": ("similar_artists", "artist_id"),
            "artist_members": ("artist_members", "compound_artist_id"),
            "album_info": ("album_info", "album_id"),
            "album_tags": ("album_tags", "album_id"),
            "genre_descriptions": ("genre_descriptions", "genre_id"),
        }

        # Parent entity filters: only request enrichment for entities
        # that actually exist in our library (prevents orphaned records)
        parent_filters = {
            "album_info": ("albums", "id"),
            "album_tags": ("albums", "id"),
        }

        needed = {}
        for cat_key, (table, uuid_col) in local_check.items():
            available = set(inventory.get(cat_key, []))
            if not available:
                continue

            # Filter by parent entity existence (e.g., only albums we own)
            if cat_key in parent_filters:
                parent_table, parent_col = parent_filters[cat_key]
                local_parents = self._get_existing_uuids(parent_table, parent_col)
                skipped = available - local_parents
                available = available & local_parents
                if skipped:
                    self._progress(
                        f"  {cat_key}: skipped {len(skipped)} "
                        f"(albums not in local library)"
                    )
                if not available:
                    continue

            existing = self._get_existing_uuids(table, uuid_col)
            missing = available - existing

            if missing:
                needed[cat_key] = list(missing)
                self._progress(
                    f"  {cat_key}: {len(missing)} new / {len(available)} available"
                )

        return needed

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
        batches = [
            uuids[i: i + self.batch_size]
            for i in range(0, len(uuids), self.batch_size)
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

            imported = self._import_items(cat_key, items)
            total_imported += imported

        return total_imported

    def _import_items(self, category: str, items: list[dict]) -> int:
        """Import items into local database. Returns count of imported items."""
        importer = getattr(self, f"_import_{category}", None)
        if not importer:
            logger.warning(f"No importer for category: {category}")
            return 0

        conn = self._get_conn()
        try:
            count = importer(conn, items)
            conn.commit()
            return count
        except Exception as e:
            conn.rollback()
            logger.error(f"Import {category} failed: {e}")
            return 0

    # -- Category importers (batch) ----------------------------------------

    def _import_lyrics(self, conn, items: list[dict]) -> int:
        with conn.cursor() as cur:
            values = [
                (
                    item["track_uuid"], item.get("source", "sync"),
                    item.get("plain_lyrics"), item.get("synced_lyrics"),
                    item.get("instrumental", False),
                )
                for item in items
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO track_lyrics
                   (track_id, source, plain_lyrics, synced_lyrics, instrumental)
                   VALUES %s
                   ON CONFLICT (track_id, source) DO UPDATE SET
                       plain_lyrics = EXCLUDED.plain_lyrics,
                       synced_lyrics = EXCLUDED.synced_lyrics,
                       instrumental = EXCLUDED.instrumental,
                       updated_at = CURRENT_TIMESTAMP""",
                values,
                template="(%s, %s, %s, %s, %s)",
                page_size=500,
            )
        return len(items)

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

            # Batch upsert embeddings
            values = [
                (
                    item["track_uuid"], item.get("model_uuid"),
                    str(item.get("vector", [])),
                    item.get("source_bit_depth"),
                    item.get("source_sample_rate"),
                    item.get("source_is_lossless"),
                )
                for item in items
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO embeddings
                   (track_id, model_id, vector,
                    source_bit_depth, source_sample_rate, source_is_lossless)
                   VALUES %s
                   ON CONFLICT (track_id, model_id) DO UPDATE SET
                       vector = EXCLUDED.vector,
                       updated_at = CURRENT_TIMESTAMP""",
                values,
                template="(%s, %s, %s::vector, %s, %s, %s)",
                page_size=200,
            )
        return len(items)

    def _import_audio_features(self, conn, items: list[dict]) -> int:
        with conn.cursor() as cur:
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
                    item.get("source_bit_depth"), item.get("source_sample_rate"),
                    item.get("source_is_lossless"),
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
                    source_bit_depth, source_sample_rate, source_is_lossless)
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
                       updated_at = CURRENT_TIMESTAMP""",
                values,
                template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                page_size=500,
            )
        return len(items)

    def _import_track_stats(self, conn, items: list[dict]) -> int:
        with conn.cursor() as cur:
            values = [
                (
                    item["track_uuid"], item.get("source", "sync"),
                    item.get("listeners"), item.get("playcount"),
                )
                for item in items
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO track_stats (track_id, source, listeners, playcount)
                   VALUES %s
                   ON CONFLICT (track_id, source) DO UPDATE SET
                       listeners = EXCLUDED.listeners,
                       playcount = EXCLUDED.playcount,
                       updated_at = CURRENT_TIMESTAMP""",
                values,
                template="(%s, %s, %s, %s)",
                page_size=500,
            )
        return len(items)

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
                )
                for item in unique_items
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO artist_bios
                   (artist_id, source, summary, content, url, listeners, playcount)
                   VALUES %s
                   ON CONFLICT (artist_id, source) DO UPDATE SET
                       summary = EXCLUDED.summary,
                       content = EXCLUDED.content,
                       url = EXCLUDED.url,
                       listeners = EXCLUDED.listeners,
                       playcount = EXCLUDED.playcount,
                       updated_at = CURRENT_TIMESTAMP""",
                values,
                template="(%s, %s, %s, %s, %s, %s, %s)",
                page_size=500,
            )
        return len(items)

    def _import_artist_tags(self, conn, items: list[dict]) -> int:
        with conn.cursor() as cur:
            # Batch upsert tags (deduplicated)
            tags_seen = {}
            for item in items:
                tid = item["tag_uuid"]
                if tid not in tags_seen:
                    tags_seen[tid] = (tid, item["tag_name"])
            if tags_seen:
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO tags (id, name)
                       VALUES %s ON CONFLICT (id) DO NOTHING""",
                    list(tags_seen.values()),
                    template="(%s, %s)",
                )

            # Batch upsert artist_tags
            values = [
                (
                    item["artist_uuid"], item["tag_uuid"],
                    item["weight"], item.get("source", "sync"),
                )
                for item in items
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO artist_tags
                   (artist_id, tag_id, weight, source)
                   VALUES %s
                   ON CONFLICT (artist_id, tag_id, source) DO UPDATE SET
                       weight = EXCLUDED.weight,
                       updated_at = CURRENT_TIMESTAMP""",
                values,
                template="(%s, %s, %s, %s)",
                page_size=500,
            )
        return len(items)

    def _import_similar_artists(self, conn, items: list[dict]) -> int:
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
                )
                for item in items
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO similar_artists
                   (artist_id, similar_artist_id, match_score, source)
                   VALUES %s
                   ON CONFLICT (artist_id, similar_artist_id, source) DO UPDATE SET
                       match_score = EXCLUDED.match_score,
                       updated_at = CURRENT_TIMESTAMP""",
                values,
                template="(%s, %s, %s, %s)",
                page_size=500,
            )
        return len(items)

    def _import_artist_members(self, conn, items: list[dict]) -> int:
        with conn.cursor() as cur:
            # Ensure member artists exist
            member_values = list({
                item["member_artist_uuid"]: (
                    item["member_artist_uuid"], item["member_artist_name"]
                )
                for item in items if item.get("member_artist_name")
            }.values())
            if member_values:
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO artists (id, name)
                       VALUES %s ON CONFLICT (id) DO NOTHING""",
                    member_values,
                    template="(%s, %s)",
                )

            # Update compound artist metadata
            compound_updates = list({
                item["compound_artist_uuid"]: (
                    item["compound_artist_uuid"],
                    item.get("artist_type", "collaboration"),
                    item.get("verification_status", "verified_split"),
                )
                for item in items
            }.values())
            if compound_updates:
                psycopg2.extras.execute_values(
                    cur,
                    """UPDATE artists SET
                           artist_type = data.artist_type,
                           verification_status = data.verification_status
                       FROM (VALUES %s) AS data(id, artist_type, verification_status)
                       WHERE artists.id = data.id::uuid""",
                    compound_updates,
                    template="(%s, %s, %s)",
                )

            # Upsert artist_members
            values = [
                (
                    item["compound_artist_uuid"],
                    item["member_artist_uuid"],
                    item.get("role", "member"),
                )
                for item in items
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO artist_members
                   (compound_artist_id, member_artist_id, role)
                   VALUES %s
                   ON CONFLICT (compound_artist_id, member_artist_id) DO UPDATE SET
                       role = EXCLUDED.role""",
                values,
                template="(%s, %s, %s)",
                page_size=500,
            )
        return len(items)

    def _import_album_info(self, conn, items: list[dict]) -> int:
        with conn.cursor() as cur:
            values = [
                (
                    item["album_uuid"], item.get("source", "sync"),
                    item.get("summary"), item.get("content"),
                    item.get("url"), item.get("listeners"),
                    item.get("playcount"),
                )
                for item in items
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO album_info
                   (album_id, source, summary, content, url, listeners, playcount)
                   VALUES %s
                   ON CONFLICT (album_id, source) DO UPDATE SET
                       summary = EXCLUDED.summary,
                       content = EXCLUDED.content,
                       url = EXCLUDED.url,
                       listeners = EXCLUDED.listeners,
                       playcount = EXCLUDED.playcount,
                       updated_at = CURRENT_TIMESTAMP""",
                values,
                template="(%s, %s, %s, %s, %s, %s, %s)",
                page_size=500,
            )
        return len(items)

    def _import_album_tags(self, conn, items: list[dict]) -> int:
        with conn.cursor() as cur:
            # Batch upsert tags (deduplicated)
            tags_seen = {}
            for item in items:
                tid = item["tag_uuid"]
                if tid not in tags_seen:
                    tags_seen[tid] = (tid, item["tag_name"])
            if tags_seen:
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO tags (id, name)
                       VALUES %s ON CONFLICT (id) DO NOTHING""",
                    list(tags_seen.values()),
                    template="(%s, %s)",
                )

            # Batch upsert album_tags
            values = [
                (
                    item["album_uuid"], item["tag_uuid"],
                    item["weight"], item.get("source", "sync"),
                )
                for item in items
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO album_tags
                   (album_id, tag_id, weight, source)
                   VALUES %s
                   ON CONFLICT (album_id, tag_id, source) DO UPDATE SET
                       weight = EXCLUDED.weight,
                       updated_at = CURRENT_TIMESTAMP""",
                values,
                template="(%s, %s, %s, %s)",
                page_size=500,
            )
        return len(items)

    def _import_genre_descriptions(self, conn, items: list[dict]) -> int:
        with conn.cursor() as cur:
            # Batch upsert genres
            genre_values = list({
                item["genre_uuid"]: (item["genre_uuid"], item["genre_name"])
                for item in items if item.get("genre_name")
            }.values())
            if genre_values:
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO genres (id, name)
                       VALUES %s ON CONFLICT (id) DO NOTHING""",
                    genre_values,
                    template="(%s, %s)",
                )

            # Batch upsert genre_descriptions
            values = [
                (
                    item["genre_uuid"], item.get("source", "sync"),
                    item.get("summary"), item.get("content"),
                    item.get("url"),
                )
                for item in items
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO genre_descriptions
                   (genre_id, source, summary, content, url)
                   VALUES %s
                   ON CONFLICT (genre_id, source) DO UPDATE SET
                       summary = EXCLUDED.summary,
                       content = EXCLUDED.content,
                       url = EXCLUDED.url,
                       updated_at = CURRENT_TIMESTAMP""",
                values,
                template="(%s, %s, %s, %s, %s)",
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
