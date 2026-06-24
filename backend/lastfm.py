"""
Last.fm API integration for Sautium.
Fetches artist bios, tags, and similar artists, storing in external_metadata table.
"""

import logging
import re
import time
from typing import Dict, List, Optional, Any

import pylast
from sqlalchemy import text
from sqlalchemy.orm import Session
from decimal import Decimal

from config import settings
from lastfm_photos import TransientFetchError
from models import (
    ExternalMetadata, Artist, SimilarArtist, Genre, GenreDescription,
    ArtistBio, Tag, ArtistTag, Album, TrackArtist
)
from uuid_utils import tag_uuid

logger = logging.getLogger(__name__)


class LastFmService:
    """Service for fetching and storing Last.fm metadata."""

    def __init__(self):
        """Initialize Last.fm network connection."""
        if not settings.lastfm_api_key:
            raise ValueError("LASTFM_API_KEY is not configured")

        self.network = pylast.LastFMNetwork(
            api_key=settings.lastfm_api_key,
            api_secret=None,  # Not needed for read-only access
        )
        logger.info("Last.fm service initialized")

    @staticmethod
    def _with_retry(fn, max_retries=3, base_delay=2.0):
        """Call fn() with exponential backoff on rate limit / transient errors.

        Last.fm error code 29 = "Rate limit exceeded".
        Also retries on network-level failures (timeout, connection reset).
        """
        for attempt in range(max_retries + 1):
            try:
                return fn()
            except pylast.WSError as e:
                err_str = str(e).lower()
                if ('rate limit' in err_str or 'try again' in err_str) and attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(f"Rate limited, retry {attempt + 1}/{max_retries} in {delay:.0f}s")
                    time.sleep(delay)
                    continue
                raise
            except (pylast.NetworkError, ConnectionError, TimeoutError, OSError) as e:
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(f"Network error ({type(e).__name__}), retry {attempt + 1}/{max_retries} in {delay:.0f}s")
                    time.sleep(delay)
                    continue
                raise

    @staticmethod
    def _is_genuine_not_found(e: pylast.WSError) -> bool:
        """True only for Last.fm status 6 ("... could not be found") — the sole
        permanent miss. Rate-limit (29), malformed/empty bodies and server errors
        carry misleading messages, so classifying by error code (not a substring
        of the message) is what keeps transient failures out of the negative cache
        and stops real entities being recorded as missing forever."""
        return str(getattr(e, "status", "")) == str(pylast.STATUS_INVALID_PARAMS)

    def get_artist_info(self, artist_name: str, fetch_similar: bool = True) -> Optional[Dict[str, Any]]:
        """
        Fetch artist info from Last.fm.

        Returns dict with:
        - bio: {summary, content, published, url}
        - tags: [{name, count}, ...]
        - stats: {listeners, playcount}
        - similar: [{name, match, mbid}, ...] (only if fetch_similar=True)
        """
        try:
            artist = self.network.get_artist(artist_name)

            # Get bio — first API call, also validates artist exists
            bio_data = None
            try:
                bio = artist.get_bio_summary()
                content = artist.get_bio_content()
                bio_data = {
                    "summary": bio,
                    "content": content,
                    "url": artist.get_url(),
                }
            except pylast.WSError:
                raise  # re-raise so outer handler catches "Artist not found"
            except Exception as e:
                logger.debug(f"No bio for {artist_name}: {e}")

            # Get tags
            tags_data = []
            try:
                top_tags = artist.get_top_tags(limit=30)
                tags_data = [
                    {"name": tag.item.get_name(), "count": int(tag.weight)}
                    for tag in top_tags
                ]
            except Exception as e:
                logger.debug(f"No tags for {artist_name}: {e}")

            # Get stats
            stats_data = {}
            try:
                stats_data = {
                    "listeners": int(artist.get_listener_count()),
                    "playcount": int(artist.get_playcount()),
                }
            except pylast.WSError:
                raise
            except Exception as e:
                logger.debug(f"No stats for {artist_name}: {e}")

            # Get similar artists (skip for non-library artists to save API calls)
            similar_data = []
            if fetch_similar:
                try:
                    similar = artist.get_similar(limit=20)
                    similar_data = [
                        {
                            "name": similar_artist.item.get_name(),
                            "match": float(similar_artist.match),
                        }
                        for similar_artist in similar
                    ]
                except pylast.WSError:
                    raise
                except Exception as e:
                    logger.debug(f"No similar artists for {artist_name}: {e}")

            # Last.fm's canonical MBID for this name — disambiguates namesakes
            # (one display name → several real MB artists). getInfo is already
            # cached by the calls above, so this reads it for free.
            try:
                lastfm_mbid = artist.get_mbid() or None
            except Exception:
                lastfm_mbid = None

            # Reaching here with every section empty but no WSError means the
            # per-section handlers swallowed a transient failure (network/throttle):
            # a genuinely missing artist would have raised status-6 on the bio call
            # above. Raise so it's recorded as a retryable 'error', never not_found.
            if not bio_data and not stats_data and not tags_data and not similar_data:
                raise RuntimeError(f"Empty Last.fm response for {artist_name} (transient)")

            return {
                "bio": bio_data,
                "tags": tags_data,
                "stats": stats_data,
                "similar": similar_data,
                "lastfm_mbid": lastfm_mbid,
            }

        except pylast.WSError as e:
            if self._is_genuine_not_found(e):
                logger.info(f"Artist not found on Last.fm: {artist_name}")
                return None
            logger.error(f"Last.fm API error for {artist_name}: {e}")
            raise
        except Exception as e:
            logger.error(f"Error fetching Last.fm data for {artist_name}: {e}")
            raise

    def store_artist_metadata(
        self, db: Session, artist_id: int, artist_name: str, data: Dict[str, Any],
        store_similar: bool = True,
    ) -> Dict[str, bool]:
        """
        Store Last.fm data in external_metadata table.

        Args:
            store_similar: If False, skip storing similar artists (prevents recursion
                          for artists not in the music library).

        Returns dict indicating what was stored: {bio: True, tags: True, similar: False, ...}
        """
        stored = {}

        # Persist Last.fm's canonical MBID for this name onto the artist row.
        # A name-UUID may conflate namesakes; this records which real MB artist
        # Last.fm's name-based bio/photo/similar actually describe, so the UI can
        # gate the photo/similar block to the matching artist_mbids row. NULL
        # (Last.fm returned no/empty mbid) overwrites stale values — idempotent.
        db.execute(
            text("UPDATE artists SET lastfm_mbid = :m WHERE id = :a"),
            {"m": data.get("lastfm_mbid"), "a": str(artist_id)},
        )

        # Store bio in normalized table
        if data.get("bio"):
            existing = db.query(ArtistBio).filter(
                ArtistBio.artist_id == artist_id,
                ArtistBio.source == "lastfm"
            ).first()

            stats = data.get("stats", {})

            if existing:
                # Update existing
                existing.summary = data["bio"].get("summary")
                existing.content = data["bio"].get("content")
                existing.url = data["bio"].get("url")
                existing.listeners = stats.get("listeners")
                existing.playcount = stats.get("playcount")
                logger.debug(f"Updated bio for artist {artist_id} ({artist_name})")
            else:
                # Create new
                bio = ArtistBio(
                    artist_id=artist_id,
                    source="lastfm",
                    summary=data["bio"].get("summary"),
                    content=data["bio"].get("content"),
                    url=data["bio"].get("url"),
                    listeners=stats.get("listeners"),
                    playcount=stats.get("playcount")
                )
                db.add(bio)
                logger.debug(f"Created bio for artist {artist_id} ({artist_name})")

            stored["bio"] = True
        else:
            # Artist found but no bio — record in external_metadata to avoid re-processing
            self._upsert_metadata(
                db,
                entity_type="artist",
                entity_id=artist_id,
                source="lastfm",
                metadata_type="bio",
                data={"found": True, "no_bio": True},
                fetch_status="success",
            )
            logger.debug(f"Artist {artist_id} ({artist_name}) found on Last.fm but has no bio")
            stored["bio"] = False

        # Store tags in normalized tables
        if data.get("tags"):
            tags_count = self._store_artist_tags(db, artist_id, artist_name, data["tags"])
            stored["tags"] = tags_count > 0
            logger.debug(f"Stored {tags_count} tags for artist {artist_id} ({artist_name})")
        else:
            stored["tags"] = False

        # Store similar artists (fetched only when the SEED is a library artist;
        # the stored set now includes out-of-catalog phantoms — see _store_similar_artists)
        if store_similar and data.get("similar"):
            similar_count = self._store_similar_artists(db, artist_id, artist_name, data["similar"])
            stored["similar_artists"] = similar_count > 0
            logger.debug(
                f"Stored {similar_count}/{len(data['similar'])} similar artists for artist {artist_id} ({artist_name})"
            )
        else:
            stored["similar_artists"] = False

        # Update artist gender from bio pronouns
        if stored.get("bio") and data.get("bio", {}).get("content"):
            self._update_artist_gender(db, artist_id, data["bio"]["content"])
            self._update_artist_is_vocalist(db, artist_id, data["bio"]["content"])

        db.commit()
        return stored

    @staticmethod
    def _update_artist_gender(db: Session, artist_id, bio_content: str) -> None:
        """Classify artist gender from bio pronouns and update the artists row."""
        if not bio_content or len(bio_content) < 200:
            return
        text_lower = bio_content.lower()
        female_score = len(re.findall(r'\bshe\b', text_lower)) + len(re.findall(r'\bher\b', text_lower))
        male_score = len(re.findall(r'\bhe\b', text_lower)) + len(re.findall(r'\bhis\b', text_lower))
        group_score = len(re.findall(r'\bthey\b', text_lower))

        if female_score >= 2 and female_score > male_score * 2 and (male_score == 0 or (female_score - male_score) >= 4):
            gender = 'female'
        elif male_score >= 2 and male_score > female_score * 2 and (female_score == 0 or (male_score - female_score) >= 4):
            gender = 'male'
        elif group_score > max(female_score, male_score) and group_score >= 3:
            gender = 'mixed'
        else:
            gender = 'unknown'

        db.execute(
            text("UPDATE artists SET gender = :gender, updated_at = NOW() WHERE id = :id"),
            {"gender": gender, "id": artist_id},
        )

    # Strong: unambiguous vocal/singer terms (each occurrence is a high-confidence signal).
    _VOCAL_STRONG_PATTERNS = (
        r'\bsinger\b', r'\bsingers\b',
        r'\bvocalist\b', r'\bvocalists\b',
        r'\bfrontman\b', r'\bfrontwoman\b',
        r'\bcrooner\b', r'\bchanteuse\b',
        r'\bsoprano\b', r'\btenor\b', r'\bbaritone\b', r'\bcontralto\b',
        r'\brapper\b',
    )
    # Medium: weaker terms, sometimes used metaphorically ("voice of a generation").
    _VOCAL_MEDIUM_PATTERNS = (
        r'\bvocal\b', r'\bvocals\b',
        r'\bsinging\b', r'\bsings\b', r'\bsang\b',
        r'\brapping\b',
    )
    _INSTRUMENTAL_PATTERNS = (
        r'\binstrumental\b', r'\binstrumentals\b', r'\binstrumentalist\b',
    )

    @staticmethod
    def _update_artist_is_vocalist(db: Session, artist_id, bio_content: str) -> None:
        """Classify artist as vocal/instrumental from bio keywords.

        Rules:
            - Short bios (< 200 chars) stay 'unknown'.
            - Any strong or medium vocal keyword → 'vocal'.
            - Only instrumental keywords, no vocal ones → 'instrumental'.
            - Otherwise → 'unknown'.
        """
        if not bio_content or len(bio_content) < 200:
            return
        text_lower = bio_content.lower()

        vocal_hits = sum(
            len(re.findall(p, text_lower))
            for p in LastFmService._VOCAL_STRONG_PATTERNS + LastFmService._VOCAL_MEDIUM_PATTERNS
        )
        instrumental_hits = sum(
            len(re.findall(p, text_lower))
            for p in LastFmService._INSTRUMENTAL_PATTERNS
        )

        if vocal_hits >= 1:
            is_vocalist = 'vocal'
        elif instrumental_hits >= 1:
            is_vocalist = 'instrumental'
        else:
            is_vocalist = 'unknown'

        db.execute(
            text("UPDATE artists SET is_vocalist = :v, updated_at = NOW() WHERE id = :id"),
            {"v": is_vocalist, "id": artist_id},
        )

    def _store_similar_artists(
        self, db: Session, artist_id, artist_name: str, similar_data: List[Dict[str, Any]]
    ) -> int:
        """Store Last.fm similar artists as edges, minting a phantom artist row
        (no media_files) for any not yet in the library — those become the
        out-of-catalog recommendations and accumulate enrichment to share.

        A guaranteed-collaboration name (feat./vs./pres./…) is split via
        detect_compound_type and each member becomes its own edge; '&'/',' duos
        stay whole — they are real entities with their own discography.

        Phantom artists are never enriched themselves (enrichment only targets
        artists with tracks), so this never cascades into similar-of-similar.
        Returns the number of new edges stored.
        """
        from uuid_utils import artist_uuid
        from canon.split import detect_compound_type

        seed = str(artist_id)
        seen: set = set()   # sids already handled this batch — pending db.add()s
                            # aren't visible to the existence query, so without this
                            # a name that recurs (collab split, spelling variant)
                            # would double-insert and trip uq_similar_artists.
        stored_count = 0

        for similar in similar_data:
            raw_name = (similar.get("name") or "").strip()
            if not raw_name:
                continue
            match_score = similar.get("match", 0.0)

            detected = detect_compound_type(raw_name)
            names = detected[2] if detected else [raw_name]

            for name in names:
                name = name.strip()
                if not name:
                    continue
                sid = artist_uuid(name)
                if str(sid) == seed or sid in seen:
                    continue   # self-loop (chk_not_self_similar) or already this batch
                seen.add(sid)

                # Mint the phantom row if absent. id derives from normalize(name),
                # so a namesake collapses to the same bucket — intended.
                db.execute(text(
                    "INSERT INTO artists (id, name) VALUES (:id, :name) "
                    "ON CONFLICT (id) DO NOTHING"
                ), {"id": str(sid), "name": name})

                existing = db.query(SimilarArtist).filter(
                    SimilarArtist.artist_id == artist_id,
                    SimilarArtist.similar_artist_id == sid,
                    SimilarArtist.source == "lastfm",
                ).first()
                if existing:
                    if abs(float(existing.match_score) - match_score) > 0.0001:
                        existing.match_score = Decimal(str(match_score))
                else:
                    db.add(SimilarArtist(
                        artist_id=artist_id,
                        similar_artist_id=sid,
                        match_score=Decimal(str(match_score)),
                        source="lastfm",
                    ))
                    stored_count += 1

        return stored_count

    def fetch_and_store_similar(self, db: Session, artist_id, artist_name: str) -> Dict[str, Any]:
        """Re-fetch ONLY Last.fm similar artists for a library artist and store
        them (minting phantoms, splitting collaborations), then stamp
        `last_similar_sync`. The backfill primitive: bio/tags are already cached,
        so this is a getSimilar-only call. Idempotent."""
        result = {"status": "success", "stored": 0}
        try:
            artist = self.network.get_artist(artist_name)
            similar = self._with_retry(lambda: artist.get_similar(limit=20))
            similar_data = [
                {"name": s.item.get_name(), "match": float(s.match)} for s in similar
            ]
        except pylast.WSError as e:
            if self._is_genuine_not_found(e):
                result["status"] = "not_found"
                similar_data = []
            else:
                raise
        result["stored"] = self._store_similar_artists(db, artist_id, artist_name, similar_data)
        # Stamp even on not_found so we don't re-query a dead name every batch.
        db.execute(text("UPDATE artists SET last_similar_sync = NOW() WHERE id = :id"),
                   {"id": str(artist_id)})
        db.commit()
        return result

    def _store_artist_tags(
        self, db: Session, artist_id: int, artist_name: str, tags_data: List[Dict[str, Any]]
    ) -> int:
        """
        Store artist tags in normalized tags/artist_tags tables.
        Creates tag records as needed.

        Returns number of tags stored.
        """
        stored_count = 0

        # Track tag IDs processed in this batch to avoid duplicates in same transaction
        processed_tag_ids = set()

        for tag_item in tags_data:
            raw_tag_name = tag_item.get("name")
            tag_weight = tag_item.get("count", 50)  # Default weight if missing

            if not raw_tag_name or not raw_tag_name.strip():
                continue

            # Split compound tags like "Rock/Pop/Indie" into separate tags
            sub_tags = [t.strip() for t in raw_tag_name.split("/") if t.strip()]
            if not sub_tags:
                continue

            for tag_name in sub_tags:
                # Truncate to 100 chars max
                tag_name = tag_name[:100]

                # Get or create tag (deterministic UUID PK)
                tid = tag_uuid(tag_name)

                tag = db.query(Tag).filter(Tag.id == tid).first()

                if not tag:
                    tag = Tag(id=tid, name=tag_name)
                    db.add(tag)
                    db.flush()
                    logger.debug(f"Created new tag: {tag_name} (ID: {tag.id})")

                # Check if we already processed this tag in current batch
                if tag.id in processed_tag_ids:
                    logger.debug(f"Skipping duplicate tag in batch: {artist_name} - {tag_name}")
                    continue

                # Check if artist_tag relationship already exists in database
                existing = db.query(ArtistTag).filter(
                    ArtistTag.artist_id == artist_id,
                    ArtistTag.tag_id == tag.id,
                    ArtistTag.source == "lastfm"
                ).first()

                if existing:
                    # Update weight if changed
                    if existing.weight != tag_weight:
                        existing.weight = tag_weight
                        logger.debug(f"Updated tag weight for {artist_name} - {tag_name}: {tag_weight}")
                else:
                    # Create new relationship
                    artist_tag = ArtistTag(
                        artist_id=artist_id,
                        tag_id=tag.id,
                        weight=tag_weight,
                        source="lastfm"
                    )
                    db.add(artist_tag)
                    processed_tag_ids.add(tag.id)  # Mark as processed
                    stored_count += 1
                    logger.debug(f"Added tag: {artist_name} - {tag_name} (weight: {tag_weight})")

        return stored_count

    def _upsert_metadata(
        self,
        db: Session,
        entity_type: str,
        entity_id,
        source: str,
        metadata_type: str,
        data: Dict[str, Any],
        fetch_status: str = "success",
        error_message: Optional[str] = None,
    ):
        """Insert or update metadata record."""
        # entity_id column is Text — ensure we pass a string, not UUID object
        entity_id = str(entity_id)

        # Check if record exists
        existing = (
            db.query(ExternalMetadata)
            .filter_by(
                entity_type=entity_type,
                entity_id=entity_id,
                source=source,
                metadata_type=metadata_type,
            )
            .first()
        )

        if existing:
            # Update
            existing.data = data
            existing.fetch_status = fetch_status
            existing.error_message = error_message
        else:
            # Insert
            record = ExternalMetadata(
                entity_type=entity_type,
                entity_id=entity_id,
                source=source,
                metadata_type=metadata_type,
                data=data,
                fetch_status=fetch_status,
                error_message=error_message,
            )
            db.add(record)

    def enrich_artist(
        self, db: Session, artist_id: int, artist_name: str,
    ) -> Dict[str, Any]:
        """
        Fetch Last.fm data for an artist and store in database.

        Bio/tags are always fetched. Similar artists are only fetched for
        OWNED artists (with a physical file) — prevents similar-of-similar.

        Returns summary dict with status and stored flags.
        """
        logger.info(f"Enriching artist: {artist_name} (ID: {artist_id})")

        # Gate similars on OWNED (a physical file), NOT EXISTS(track_artists):
        # phantom artists gained track_artists from materialized phantom
        # tracklists, so track_artists no longer means "in catalog". Without
        # the media_files join, ~16k phantoms would fetch similars -> blowup.
        is_owned = db.execute(text("""
            SELECT 1 FROM track_artists ta
            JOIN media_files mf ON mf.track_id = ta.track_id
            WHERE ta.artist_id = :id LIMIT 1
        """), {"id": str(artist_id)}).first() is not None
        fetch_similar = is_owned

        try:
            data = self._with_retry(
                lambda: self.get_artist_info(artist_name, fetch_similar=fetch_similar)
            )

            if data is None:
                # Artist not found
                self._upsert_metadata(
                    db,
                    entity_type="artist",
                    entity_id=artist_id,
                    source="lastfm",
                    metadata_type="bio",
                    data={},
                    fetch_status="not_found",
                    error_message="Artist not found on Last.fm",
                )
                db.commit()
                return {
                    "status": "not_found",
                    "artist_id": artist_id,
                    "artist_name": artist_name,
                    "stored": {},
                }

            # Store bio/tags/similar in normalized tables
            stored = self.store_artist_metadata(
                db, artist_id, artist_name, data, store_similar=fetch_similar
            )

            return {
                "status": "success",
                "artist_id": artist_id,
                "artist_name": artist_name,
                "stored": stored,
                "tags_count": len(data.get("tags", [])),
                "similar_count": len(data.get("similar", [])),
            }

        except Exception as e:
            logger.error(f"Failed to enrich artist {artist_name}: {e}")
            db.rollback()

            # Store error in external_metadata to avoid re-processing
            try:
                self._upsert_metadata(
                    db,
                    entity_type="artist",
                    entity_id=artist_id,
                    source="lastfm",
                    metadata_type="bio",
                    data={},
                    fetch_status="error",
                    error_message=str(e)[:500],
                )
                db.commit()
            except Exception:
                db.rollback()

            return {
                "status": "error",
                "artist_id": artist_id,
                "artist_name": artist_name,
                "error": str(e),
            }

    def get_tag_info(self, tag_name: str) -> Optional[Dict[str, Any]]:
        """
        Fetch tag/genre info from Last.fm.

        Returns dict with:
        - summary: Short description
        - content: Full wiki text
        - url: Last.fm tag page URL
        """
        try:
            tag = self.network.get_tag(tag_name)

            # Get wiki info
            summary = None
            content = None
            try:
                summary = tag.get_wiki_summary()
                content = tag.get_wiki_content()
            except Exception as e:
                logger.debug(f"No wiki for tag {tag_name}: {e}")

            if not summary and not content:
                return None

            return {
                "summary": summary,
                "content": content,
                "url": tag.get_url(),
            }

        except pylast.WSError as e:
            if self._is_genuine_not_found(e):
                logger.info(f"Tag not found on Last.fm: {tag_name}")
                return None
            else:
                logger.error(f"Last.fm API error for tag {tag_name}: {e}")
                raise
        except Exception as e:
            logger.error(f"Error fetching Last.fm data for tag {tag_name}: {e}")
            raise

    def enrich_genre(self, db: Session, genre_id: int, genre_name: str) -> Dict[str, Any]:
        """
        Fetch Last.fm data for a genre/tag and store in normalized genre_descriptions table.

        Returns summary dict with status.
        """
        logger.info(f"Enriching genre: {genre_name} (ID: {genre_id})")

        try:
            # Fetch from Last.fm (with retry on rate limit / network errors)
            data = self._with_retry(lambda: self.get_tag_info(genre_name))

            if data is None:
                # Tag not found
                logger.warning(f"Genre not found on Last.fm: {genre_name}")
                return {
                    "status": "not_found",
                    "genre_id": genre_id,
                    "genre_name": genre_name,
                }

            # Store in normalized table
            existing = db.query(GenreDescription).filter(
                GenreDescription.genre_id == genre_id,
                GenreDescription.source == "lastfm"
            ).first()

            if existing:
                # Update existing
                existing.summary = data.get("summary")
                existing.content = data.get("content")
                existing.url = data.get("url")
                logger.debug(f"Updated description for genre {genre_id} ({genre_name})")
            else:
                # Create new
                description = GenreDescription(
                    genre_id=genre_id,
                    source="lastfm",
                    summary=data.get("summary"),
                    content=data.get("content"),
                    url=data.get("url"),
                )
                db.add(description)
                logger.debug(f"Created description for genre {genre_id} ({genre_name})")

            db.commit()

            return {
                "status": "success",
                "genre_id": genre_id,
                "genre_name": genre_name,
                "has_description": bool(data.get("summary") or data.get("content")),
                "summary_length": len(data.get("summary") or ""),
                "content_length": len(data.get("content") or ""),
            }

        except Exception as e:
            logger.error(f"Failed to enrich genre {genre_name}: {e}")
            db.rollback()

            return {
                "status": "error",
                "genre_id": genre_id,
                "genre_name": genre_name,
                "error": str(e),
            }

    def enrich_genres_batch(
        self,
        db: Session,
        limit: Optional[int] = None,
        skip_existing: bool = True,
        rate_limit_delay: float = 0.2,
    ) -> Dict[str, Any]:
        """
        Enrich multiple genres with Last.fm tag data.

        Args:
            db: Database session
            limit: Max number of genres to process
            skip_existing: Skip genres that already have Last.fm data
            rate_limit_delay: Delay between requests (seconds)

        Returns:
            Statistics dict
        """
        # Get genres to enrich
        if skip_existing:
            query = text("""
                SELECT DISTINCT g.id, g.name
                FROM genres g
                WHERE NOT EXISTS (
                    SELECT 1 FROM genre_descriptions gd
                    WHERE gd.genre_id = g.id
                      AND gd.source = 'lastfm'
                )
                ORDER BY g.name
            """)
        else:
            query = text("SELECT id, name FROM genres ORDER BY name")

        if limit:
            query = text(str(query) + f" LIMIT {limit}")

        genres = db.execute(query).fetchall()

        if not genres:
            logger.info("No genres to enrich")
            return {"processed": 0, "success": 0, "not_found": 0, "errors": 0}

        logger.info(f"Enriching {len(genres)} genres from Last.fm")

        stats = {"processed": 0, "success": 0, "not_found": 0, "errors": 0}

        for genre_id, genre_name in genres:
            result = self.enrich_genre(db, genre_id, genre_name)

            stats["processed"] += 1

            if result["status"] == "success":
                stats["success"] += 1
            elif result["status"] == "not_found":
                stats["not_found"] += 1
            elif result["status"] == "error":
                stats["errors"] += 1

            # Rate limiting
            if rate_limit_delay > 0:
                time.sleep(rate_limit_delay)

        logger.info(
            f"Last.fm genre enrichment complete: {stats['success']} success, "
            f"{stats['not_found']} not found, {stats['errors']} errors"
        )

        return stats

    def enrich_artists_batch(
        self,
        db: Session,
        limit: Optional[int] = None,
        skip_existing: bool = True,
        rate_limit_delay: float = 0.2,
    ) -> Dict[str, Any]:
        """
        Enrich multiple artists with Last.fm data.

        Args:
            db: Database session
            limit: Max number of artists to process
            skip_existing: Skip artists that already have Last.fm data
            rate_limit_delay: Delay between requests (seconds)

        Returns:
            Statistics dict
        """
        # Get artists to enrich (only those with tracks in library)
        if skip_existing:
            # Find library artists without Last.fm bio
            query = text("""
                SELECT DISTINCT a.id, a.name
                FROM artists a
                JOIN track_artists ta ON ta.artist_id = a.id
                WHERE NOT EXISTS (
                    SELECT 1 FROM external_metadata em
                    WHERE em.entity_type = 'artist'
                      AND em.entity_id = a.id::text
                      AND em.source = 'lastfm'
                      AND em.metadata_type = 'bio'
                )
                ORDER BY a.name
            """)
        else:
            query = text("""
                SELECT DISTINCT a.id, a.name
                FROM artists a
                JOIN track_artists ta ON ta.artist_id = a.id
                ORDER BY a.name
            """)

        if limit:
            query = text(str(query) + f" LIMIT {limit}")

        artists = db.execute(query).fetchall()

        if not artists:
            logger.info("No artists to enrich")
            return {"processed": 0, "success": 0, "not_found": 0, "errors": 0}

        logger.info(f"Enriching {len(artists)} artists from Last.fm")

        stats = {"processed": 0, "success": 0, "not_found": 0, "errors": 0}

        for artist_id, artist_name in artists:
            result = self.enrich_artist(db, artist_id, artist_name)

            stats["processed"] += 1

            if result["status"] == "success":
                stats["success"] += 1
            elif result["status"] == "not_found":
                stats["not_found"] += 1
            elif result["status"] == "error":
                stats["errors"] += 1

            # Rate limiting
            if rate_limit_delay > 0:
                time.sleep(rate_limit_delay)

        logger.info(
            f"Last.fm enrichment complete: {stats['success']} success, "
            f"{stats['not_found']} not found, {stats['errors']} errors"
        )

        return stats

    def get_album_cover_url(
        self,
        artist_name: str,
        album_title: str,
        size: int = pylast.SIZE_MEGA,
    ) -> Optional[str]:
        """Resolve a direct Last.fm cover-art URL for an album, or None.

        Last.fm's `album.getInfo` returns image URLs at sizes
        small/medium/large/extralarge/mega — pylast addresses them by
        integer index (pylast.SIZE_*). We default to SIZE_MEGA so the
        downstream WebP encoder (1024px max) gets the highest-detail
        source available.

        Returns None only when the album is genuinely unknown to Last.fm
        or has no cover registered — the caller pins SENTINEL on None, so
        a transient failure must NOT collapse to None. Network errors and
        rate limits (which `_with_retry` already retries with backoff)
        raise TransientFetchError after exhausting retries, so the caller
        leaves the cover unresolved and retries on a later request rather
        than permanently marking the album cover-less.
        """
        try:
            url = self._with_retry(
                lambda: self.network.get_album(artist_name, album_title).get_cover_image(size=size)
            )
        except pylast.WSError as e:
            if self._is_genuine_not_found(e):
                return None
            # Rate limit (code 29) survived the backoff, or some other
            # API-level error — transient, not "no cover".
            raise TransientFetchError(f"WSError: {e}") from e
        except Exception as e:
            # NetworkError / timeout / connection reset that outlived
            # _with_retry's backoff.
            raise TransientFetchError(f"{type(e).__name__}: {e}") from e

        if not url:
            return None
        url = str(url).strip()
        return url or None

    def get_track_stats(self, artist_name: str, track_title: str) -> Optional[Dict[str, Any]]:
        """
        Fetch track statistics from Last.fm (listeners and playcount only).

        Returns dict with:
        - listeners: int
        - playcount: int
        - mbid: str (optional)

        Returns None if track not found.
        """
        try:
            track = self.network.get_track(artist_name, track_title)

            # Get statistics
            listeners = None
            playcount = None

            try:
                listeners = int(track.get_listener_count())
            except Exception as e:
                logger.debug(f"No listeners for {artist_name} - {track_title}: {e}")

            try:
                playcount = int(track.get_playcount())
            except Exception as e:
                logger.debug(f"No playcount for {artist_name} - {track_title}: {e}")

            # Get MBID (optional)
            mbid = None
            try:
                mbid = track.get_mbid()
            except Exception as e:
                logger.debug(f"No MBID for {artist_name} - {track_title}: {e}")

            # Return None if no stats available
            if listeners is None and playcount is None:
                return None

            return {
                "listeners": listeners,
                "playcount": playcount,
                "mbid": mbid,
            }

        except pylast.WSError as e:
            if self._is_genuine_not_found(e):
                logger.info(f"Track not found on Last.fm: {artist_name} - {track_title}")
                return None
            else:
                logger.error(f"Last.fm API error for {artist_name} - {track_title}: {e}")
                raise
        except Exception as e:
            logger.error(f"Error fetching Last.fm data for {artist_name} - {track_title}: {e}")
            raise

    def enrich_track(self, db: Session, track_id, artist_name: str, track_title: str) -> Dict[str, Any]:
        """
        Fetch Last.fm statistics for a track and store in database.

        Args:
            track_id: UUID of the track.
            artist_name: Primary artist name.
            track_title: Track title.

        Returns summary dict with status.
        """
        logger.info(f"Enriching track: {artist_name} - {track_title} (ID: {track_id})")

        try:
            # Fetch from Last.fm (with retry on rate limit / network errors)
            stats = self._with_retry(lambda: self.get_track_stats(artist_name, track_title))

            if stats is None:
                # Track not found or no stats — record in external_metadata to avoid re-processing
                logger.warning(f"No stats found for track: {artist_name} - {track_title}")
                self._upsert_metadata(
                    db,
                    entity_type="track",
                    entity_id=str(track_id),
                    source="lastfm",
                    metadata_type="stats",
                    data={},
                    fetch_status="not_found",
                    error_message=f"No stats found: {artist_name} - {track_title}",
                )
                db.commit()
                return {
                    "status": "not_found",
                    "track_id": track_id,
                    "artist_name": artist_name,
                    "track_title": track_title,
                }

            # Store track stats
            self._store_track_stats(db, track_id, stats)

            db.commit()

            return {
                "status": "success",
                "track_id": track_id,
                "artist_name": artist_name,
                "track_title": track_title,
                "listeners": stats.get("listeners"),
                "playcount": stats.get("playcount"),
            }

        except Exception as e:
            logger.error(f"Failed to enrich track {artist_name} - {track_title}: {e}")
            db.rollback()

            # Record error in external_metadata to avoid re-processing
            try:
                self._upsert_metadata(
                    db,
                    entity_type="track",
                    entity_id=str(track_id),
                    source="lastfm",
                    metadata_type="stats",
                    data={},
                    fetch_status="error",
                    error_message=str(e),
                )
                db.commit()
            except Exception:
                db.rollback()

            return {
                "status": "error",
                "track_id": track_id,
                "artist_name": artist_name,
                "track_title": track_title,
                "error": str(e),
            }

    def _store_track_stats(self, db: Session, track_id, stats: Dict[str, Any]):
        """Store track statistics in track_stats table."""
        from models import TrackStats

        listeners = stats.get("listeners")
        playcount = stats.get("playcount")

        # Check if already exists
        existing = db.query(TrackStats).filter(
            TrackStats.track_id == track_id,
            TrackStats.source == "lastfm"
        ).first()

        if existing:
            # Update existing
            existing.listeners = listeners
            existing.playcount = playcount
            logger.debug(f"Updated track stats for track {track_id}")
        else:
            # Create new
            track_stats = TrackStats(
                track_id=track_id,
                source="lastfm",
                listeners=listeners,
                playcount=playcount
            )
            db.add(track_stats)
            logger.debug(f"Created track stats for track {track_id}")


def backfill_similar(limit: Optional[int] = None, delay: float = 0.2,
                     force: bool = False) -> Dict[str, int]:
    """One-shot backfill of Last.fm similar artists for library artists.

    The pre-phantom code discarded out-of-catalog similars without caching them,
    so they must be re-fetched once after deploying the phantom-creating
    ``_store_similar_artists``. Processes library artists (those with track
    credits) whose ``last_similar_sync`` is unset — oldest/never first — minting
    phantom rows and splitting collaborations, one commit per artist for
    resumability. Incremental + idempotent; ``force=True`` re-fetches everyone.
    New artists need no backfill — their similars are stored on first bio
    enrichment.
    """
    from database import get_db_context

    svc = LastFmService()
    where = "" if force else "AND a.last_similar_sync IS NULL"
    lim = "LIMIT :lim" if limit else ""
    sql = text(f"""
        SELECT a.id, a.name
        FROM artists a
        WHERE EXISTS (SELECT 1 FROM track_artists ta
                      JOIN media_files mf ON mf.track_id = ta.track_id
                      WHERE ta.artist_id = a.id)
          {where}
        ORDER BY a.last_similar_sync NULLS FIRST, a.name
        {lim}
    """)
    stats = {"processed": 0, "stored": 0, "not_found": 0, "errors": 0}
    with get_db_context() as db:
        rows = db.execute(sql, {"lim": limit} if limit else {}).fetchall()
    logger.info(f"backfill_similar: {len(rows)} library artists queued")
    for row in rows:
        try:
            with get_db_context() as db:
                r = svc.fetch_and_store_similar(db, row.id, row.name)
            stats["processed"] += 1
            stats["stored"] += r["stored"]
            if r["status"] == "not_found":
                stats["not_found"] += 1
        except Exception as e:
            stats["errors"] += 1
            logger.error(f"backfill_similar failed for {row.name}: {e}")
        time.sleep(delay)
    return stats


def backfill_lastfm_mbid(limit: Optional[int] = None, delay: float = 0.2,
                         force: bool = False, namesakes_only: bool = True) -> Dict[str, int]:
    """Populate ``artists.lastfm_mbid`` — the MB artist Last.fm treats as canonical
    for the name. Used to decide which namesake owns the (name-based) photo/similar
    block when one display name maps to several real MB artists.

    Defaults to namesake artists only (>=2 ``artist_mbids`` rows) — the immediate
    consumers. ``namesakes_only=False`` covers every Last.fm-known artist, owned
    AND phantom: phantoms are first-class enrichment carriers (they accumulate
    metadata for the P2P network's coverage), so lastfm_mbid is filled for them
    too — even though the artist screen only reads it for owned namesake splits.
    Incremental (skips rows already set) unless ``force``; one lightweight getInfo
    call each. New artists need no backfill — captured on first bio enrichment.
    """
    from database import get_db_context

    svc = LastFmService()
    scope = ("(SELECT count(*) FROM artist_mbids am WHERE am.artist_id = a.id) >= 2"
             if namesakes_only else
             "EXISTS (SELECT 1 FROM artist_bios b WHERE b.artist_id = a.id AND b.source = 'lastfm')")
    fresh = "" if force else "AND a.lastfm_mbid IS NULL"
    lim = "LIMIT :lim" if limit else ""
    sql = text(f"""
        SELECT a.id, a.name FROM artists a
        WHERE {scope} {fresh}
        ORDER BY a.name {lim}
    """)
    stats = {"processed": 0, "set": 0, "null": 0, "errors": 0}
    with get_db_context() as db:
        rows = db.execute(sql, {"lim": limit} if limit else {}).fetchall()
    logger.info(f"backfill_lastfm_mbid: {len(rows)} artists queued")
    for row in rows:
        try:
            artist = svc.network.get_artist(row.name)

            def _mbid():
                try:
                    return artist.get_mbid() or None
                except pylast.WSError:
                    raise
                except Exception:
                    return None

            mbid = svc._with_retry(_mbid)
            with get_db_context() as db:
                db.execute(text("UPDATE artists SET lastfm_mbid = :m WHERE id = :a"),
                           {"m": mbid, "a": str(row.id)})
            stats["processed"] += 1
            stats["set" if mbid else "null"] += 1
            logger.info(f"  {row.name}: lastfm_mbid={mbid}")
        except Exception as e:
            stats["errors"] += 1
            logger.error(f"backfill_lastfm_mbid failed for {row.name}: {e}")
        time.sleep(delay)
    return stats
