"""
Last.fm API integration for Sautium.
Fetches artist bios, tags, and similar artists, storing in external_metadata table.
"""

import logging
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Any
from xml.dom.minidom import Document

import pylast

import api_cooldown
from sqlalchemy import text
from sqlalchemy.orm import Session
from decimal import Decimal

from config import settings
from photo_fetch import TransientFetchError
from sql_queries import ARTIST_ENGAGED
from models import (
    ExternalMetadata, Artist, SimilarArtist, Genre, GenreDescription,
    ArtistBio, Tag, ArtistTag, Album, TrackArtist
)
from uuid_utils import tag_uuid

logger = logging.getLogger(__name__)

# One pace for every Last.fm request this process makes — enrichment,
# covers, scrobbles, the authorization flow. pylast's own limiter lives on
# each network object (every LastFmService built its own, so parallel callers
# never saw each other), holds no lock, and stamps the time BEFORE its sleep,
# so back-to-back calls left in pairs. 0.34 s (~3 req/s) keeps clear of the
# published 5 req/s per address; the lock is held through the sleep, which is
# what queues the callers onto the pace.
_PACE_S = 0.34
_pace_lock = threading.Lock()
_last_call = 0.0


class _PacedNetwork(pylast.LastFMNetwork):
    """pylast calls `_delay_call` before every request once `limit_rate` is
    set (`_Request._download_response`); this one waits for the process-wide
    slot instead of the instance's own clock."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.limit_rate = True

    def _delay_call(self) -> None:
        global _last_call
        with _pace_lock:
            wait = _last_call + _PACE_S - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last_call = time.monotonic()


def lastfm_network(session_key: str = "", username: str = "") -> pylast.LastFMNetwork:
    """The only way this codebase builds a Last.fm network: behind the pace,
    signing every call when a session key is given."""
    return _PacedNetwork(api_key=settings.lastfm_api_key,
                         api_secret=settings.lastfm_api_secret,
                         session_key=session_key or "", username=username or "")


# A failed fetch means one of three things, and each has its own handling —
# reading them as one "error" is how a ban once marked thirty innocent
# artists per pass, and how one bug of ours held a queue for weeks.
#
#   1. The SOURCE'S VERDICT about the entity: a pylast.WSError that reaches
#      the caller — status 6 (not found) or another per-entity refusal.
#      Cached in external_metadata so the planner backs off for the window
#      (not_found 90 days, error 7 days).
#   2. The SOURCE IS UNAVAILABLE to us: a transport failure, a 5xx, its own
#      "offline / try again" statuses, or a refusal — the rate limit (29),
#      a dead key, an HTML challenge page instead of XML. _with_retry turns
#      these into SourceUnavailable (SourceRefused for the refusals, which
#      also arm the persistent cooldown). Says nothing about the entity:
#      the batch ends, the entity stays unmarked, the next pass retries.
#   3. OUR OWN FAILURE: anything else — a UniqueViolation, a value too
#      long, a TypeError. Nothing here is a statement about the artist, and
#      recording it as a verdict turned a transient bug into permanent
#      data loss: measured on the master before this existed, 26 of 49
#      cached "errors" were our own database errors, one captioned
#      "(transient)" and five months old. Never cached. But the same code
#      fed the same row fails the same way, and an unmarked row is
#      re-selected at the head of every pass — so it is registered here
#      (internal_failures) and skipped until this process is replaced,
#      because a fix is a restart.

class SourceUnavailable(Exception):
    """Last.fm gave no answer about the entity asked for, and every retry
    in _with_retry failed. Callers end the batch and leave the entity
    unmarked; the loop's interval is the backoff."""


class SourceRefused(SourceUnavailable):
    """The refusal variant — rate limit (29), invalid or suspended key
    (10, 26), or a non-API answer such as an HTML challenge page — and the
    persistent 'lastfm' cooldown has been armed (api_cooldown)."""


_REFUSED_STATUSES = {"29", "10", "26"}
# 8 "operation failed, try again", 11 "service offline", 16 "temporary
# error"; pylast reports an HTTP 5xx as a WSError carrying the HTTP code.
_TRANSIENT_STATUSES = {"8", "11", "16", "500", "502", "503", "504"}


def _failure_class(exc: BaseException) -> Optional[str]:
    """'refused' or 'transient' for a failure of the source; None for its
    verdict about the entity (any other WSError) and for our own errors —
    neither is retried."""
    if isinstance(exc, pylast.WSError):
        status = str(exc.status)
        if status in _REFUSED_STATUSES:
            return "refused"
        if status in _TRANSIENT_STATUSES:
            return "transient"
        return None
    if isinstance(exc, pylast.MalformedResponseError):
        return "refused"
    if isinstance(exc, (pylast.NetworkError, ConnectionError, TimeoutError, OSError)):
        return "transient"
    return None


# Entities this process's own code failed on, by entity type ('artist',
# 'genre'). Read by every candidate query as an exclusion list; forgotten
# with the process.
_internal_failures: Dict[str, set] = {}


def note_internal_failure(entity_type: str, entity_id) -> None:
    _internal_failures.setdefault(entity_type, set()).add(str(entity_id))


def internal_failures(entity_type: str) -> List[str]:
    """For `<> ALL(CAST(:skip AS uuid[]))` in a candidate query."""
    return sorted(_internal_failures.get(entity_type, ()))


def _first_text(doc: Document, tag: str) -> Optional[str]:
    """Text of the first `tag` element (pylast's own reading: the artist's
    fields come before the similar-artist block in getInfo), or None."""
    nodes = doc.getElementsByTagName(tag)
    if not nodes or nodes[0].firstChild is None:
        return None
    return nodes[0].firstChild.wholeText.strip() or None


def _artist_info(doc: Document) -> Dict[str, Any]:
    """The fields enrichment keeps from one artist.getInfo answer."""
    return {
        "bio": {"summary": _first_text(doc, "summary"),
                "content": _first_text(doc, "content")},
        "stats": {"listeners": int(_first_text(doc, "listeners") or 0),
                  "playcount": int(_first_text(doc, "playcount") or 0)},
        # Last.fm's canonical MBID for this name — disambiguates namesakes
        # (one display name → several real MB artists).
        "mbid": _first_text(doc, "mbid"),
    }


class LastFmService:
    """Service for fetching and storing Last.fm metadata."""

    def __init__(self):
        """Initialize Last.fm network connection."""
        if not settings.lastfm_api_key:
            raise ValueError("LASTFM_API_KEY is not configured")

        self.network = lastfm_network()
        logger.debug("Last.fm service initialized")

    @staticmethod
    def _with_retry(fn, max_retries=3, base_delay=2.0):
        """Call fn(), retrying a failure of the SOURCE (see _failure_class)
        with exponential backoff. One that survives every retry becomes
        SourceUnavailable — SourceRefused, with the persistent 'lastfm'
        cooldown armed, when the source is refusing us rather than merely
        failing. A verdict about the entity and our own errors are raised
        untouched, at once. A successful call clears the cooldown's strikes:
        this is the one place that arms it, so it is the one place that
        resets it."""
        for attempt in range(max_retries + 1):
            try:
                result = fn()
                api_cooldown.clear('lastfm')
                return result
            except Exception as e:
                kind = _failure_class(e)
                if kind is None:
                    raise
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(f"Last.fm {kind} ({type(e).__name__}: {e}), "
                                   f"retry {attempt + 1}/{max_retries} in {delay:.0f}s")
                    time.sleep(delay)
                    continue
                if kind == "refused":
                    api_cooldown.arm('lastfm', str(e))
                    raise SourceRefused(str(e)) from e
                raise SourceUnavailable(str(e)) from e

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
            # One getInfo read whole (pylast's per-field getters each send
            # their own — five requests for one answer), getTopTags and — for
            # a library artist — getSimilar. Nothing is caught per section:
            # a missing section reads as None or [], and an exception here is
            # the source failing or refusing, which _with_retry classifies for
            # the caller. Per-section swallowing once turned a ban into
            # "empty artist" verdicts cached for a week.
            info = _artist_info(artist._request(artist.ws_prefix + ".getInfo", True))
            tags_data = [
                {"name": tag.item.get_name(), "count": int(tag.weight)}
                for tag in artist.get_top_tags(limit=30)
            ]
            similar_data = []
            if fetch_similar:
                similar_data = [
                    {"name": s.item.get_name(), "match": float(s.match)}
                    for s in artist.get_similar(limit=20)
                ]

            return {
                "bio": {**info["bio"], "url": artist.get_url()},
                "tags": tags_data,
                "stats": info["stats"],
                "similar": similar_data,
                "lastfm_mbid": info["mbid"],
            }

        except pylast.WSError as e:
            if self._is_genuine_not_found(e):
                logger.info(f"Artist not found on Last.fm: {artist_name}")
                return None
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
            # Stamp so the engagement-gated backfill (backfill_similar) doesn't
            # re-ask getSimilar for an artist whose similars just landed here.
            # Only on a non-empty list: a transient getSimilar failure arrives
            # as [] (get_artist_info swallows per-section errors), and leaving
            # it unstamped lets the backfill self-heal it next cycle.
            db.execute(text("UPDATE artists SET last_similar_sync = NOW() WHERE id = :id"),
                       {"id": str(artist_id)})
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

        A minted stub only becomes a similar-seed once a human completes a
        listen on it (backfill_similar's engagement gate), so expansion is
        linear in listening — no unconditional similar-of-similar cascade.
        Returns the number of new edges stored.
        """
        from uuid_utils import artist_uuid
        from canon.split import detect_compound_type
        from transliterate import latinize
        from routers.settings import _read as _read_setting

        # The phantom layer is the owner's switch (discovery.phantom_layer),
        # never the hardware profile's. Off: edges are stored only between
        # artists that already exist; unknown similars are dropped instead
        # of minted (FK requires the row).
        mint_phantoms = bool(_read_setting("discovery.phantom_layer"))

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
                if mint_phantoms:
                    db.execute(text(
                        "INSERT INTO artists (id, name, name_latin) VALUES (:id, :name, :nl) "
                        "ON CONFLICT (id) DO NOTHING"
                    ), {"id": str(sid), "name": name, "nl": latinize(name)})
                elif not db.execute(text(
                    "SELECT 1 FROM artists WHERE id = :id"
                ), {"id": str(sid)}).first():
                    continue

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
            # The source's verdict about this name — not found, or another
            # per-entity refusal (its own failures became SourceUnavailable
            # in _with_retry). Nothing to store either way, and the stamp
            # below keeps the backfill from asking again.
            if self._is_genuine_not_found(e):
                result["status"] = "not_found"
            else:
                logger.warning(f"Last.fm error for similars of {artist_name}: {e}")
                result["status"] = "error"
            similar_data = []
        result["stored"] = self._store_similar_artists(db, artist_id, artist_name, similar_data)
        # Stamped on every verdict, not only on a list: a dead name is asked once.
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
        OWNED artists (with a physical file); listened-but-unowned phantoms
        get theirs from the engagement-gated backfill_similar step instead.

        Returns summary dict with status and stored flags.
        """
        logger.info(f"Enriching artist: {artist_name} (ID: {artist_id})")

        # Gate similars on OWNED (a physical file), NOT EXISTS(track_artists):
        # phantom artists gained track_artists from materialized phantom
        # tracklists, so track_artists no longer means "in catalog". Without
        # the media_files join, ~16k phantoms would fetch similars -> blowup.
        # Deliberately NOT widened to the listened-phantom predicate: the
        # engagement-gated step (backfill_similar, run by background_enrichment)
        # owns that rule as the single choke point, and picks such artists up
        # within one cycle regardless of bio state.
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

        except SourceUnavailable as e:
            # Not a statement about this artist: no marker, and a status
            # that ends the batch.
            db.rollback()
            return {
                "status": "unavailable",
                "artist_id": artist_id,
                "artist_name": artist_name,
                "error": str(e),
            }
        except pylast.WSError as e:
            # The source's verdict about this name (status 6 was answered
            # as not_found above). Cached so the planner backs off for the
            # window.
            logger.warning(f"Last.fm error for artist {artist_name}: {e}")
            db.rollback()
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
            return {
                "status": "error",
                "artist_id": artist_id,
                "artist_name": artist_name,
                "error": str(e),
            }
        except Exception as e:
            # Ours (see the taxonomy at the top): logged with its trace,
            # registered for this process, never cached.
            logger.error(f"Enriching artist {artist_name} failed in our code: {e}",
                         exc_info=True)
            db.rollback()
            note_internal_failure("artist", artist_id)
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
            # Not caught per call: pylast answers a missing wiki with None,
            # and an exception is the source failing, for _with_retry.
            summary = tag.get_wiki_summary()
            content = tag.get_wiki_content()
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
            raise

    def enrich_genre(self, db: Session, genre_id: int, genre_name: str) -> Dict[str, Any]:
        """
        Fetch Last.fm data for a genre/tag and store in normalized genre_descriptions table.

        Returns summary dict with status.
        """
        logger.info(f"Enriching genre: {genre_name} (ID: {genre_id})")

        try:
            data = self._with_retry(lambda: self.get_tag_info(genre_name))

            if data is None:
                # No wiki (or no such tag): marked here, not by the caller,
                # so every batch — background or CLI — stops asking.
                logger.info(f"Genre not found on Last.fm: {genre_name}")
                self._upsert_metadata(
                    db,
                    entity_type="genre",
                    entity_id=genre_id,
                    source="lastfm",
                    metadata_type="description",
                    data={},
                    fetch_status="not_found",
                    error_message="No wiki on Last.fm",
                )
                db.commit()
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

        except SourceUnavailable as e:
            db.rollback()
            return {
                "status": "unavailable",
                "genre_id": genre_id,
                "genre_name": genre_name,
                "error": str(e),
            }
        except pylast.WSError as e:
            logger.warning(f"Last.fm error for genre {genre_name}: {e}")
            db.rollback()
            self._upsert_metadata(
                db,
                entity_type="genre",
                entity_id=genre_id,
                source="lastfm",
                metadata_type="description",
                data={},
                fetch_status="error",
                error_message=str(e)[:500],
            )
            db.commit()
            return {
                "status": "error",
                "genre_id": genre_id,
                "genre_name": genre_name,
                "error": str(e),
            }
        except Exception as e:
            logger.error(f"Enriching genre {genre_name} failed in our code: {e}",
                         exc_info=True)
            db.rollback()
            note_internal_failure("genre", genre_id)
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
    ) -> Dict[str, Any]:
        """
        Enrich multiple genres with Last.fm tag data.

        Args:
            db: Database session
            limit: Max number of genres to process
            skip_existing: Skip genres that already have Last.fm data

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
            if result["status"] == "unavailable":
                logger.info("Last.fm unavailable — ending genre batch")
                break

            stats["processed"] += 1

            if result["status"] == "success":
                stats["success"] += 1
            elif result["status"] == "not_found":
                stats["not_found"] += 1
            elif result["status"] == "error":
                stats["errors"] += 1

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
    ) -> Dict[str, Any]:
        """
        Enrich multiple artists with Last.fm data.

        Args:
            db: Database session
            limit: Max number of artists to process
            skip_existing: Skip artists that already have Last.fm data

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
            if result["status"] == "unavailable":
                logger.info("Last.fm unavailable — ending artist batch")
                break

            stats["processed"] += 1

            if result["status"] == "success":
                stats["success"] += 1
            elif result["status"] == "not_found":
                stats["not_found"] += 1
            elif result["status"] == "error":
                stats["errors"] += 1

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
            # Another per-entity verdict — left unresolved, not "no cover".
            raise TransientFetchError(f"WSError: {e}") from e
        except Exception as e:
            # SourceUnavailable after _with_retry's backoff, or our own error.
            raise TransientFetchError(f"{type(e).__name__}: {e}") from e

        if not url:
            return None
        url = str(url).strip()
        return url or None


def backfill_similar(limit: Optional[int] = None, force: bool = False,
                     cancel_flag: Optional[Callable[[], bool]] = None) -> Dict[str, int]:
    """Fetch Last.fm similars for ENGAGED artists that never had them.

    Engaged = an owned file (track_artists JOIN media_files) OR at least one
    completed, unskipped listen (listening_history — covers streamed phantoms,
    the catalog-less mode). Both signals are linear in human behavior, so the
    fan-out stays bounded: a minted similar-stub only becomes a seed via a new
    human listen — no unconditional similar-of-similar recursion.

    This is the shared candidate rule for the background `similar` step and
    manual runs. Recently-listened first; one commit per artist for
    resumability; incremental + idempotent. ``force=True`` re-fetches the whole
    engaged set (also the escape hatch for the permanent not_found stamp —
    fetch_and_store_similar stamps a dead name once, forever).

    Every row counted in ``processed`` left the queue — stamped, or registered
    as this process's own failure (internal_failures). An unavailable source
    ends the batch (``unavailable``) with the row unstamped for the next pass.
    """
    from database import get_db_context

    svc = LastFmService()
    where = "" if force else "AND a.last_similar_sync IS NULL"
    lim = "LIMIT :lim" if limit else ""
    sql = text(f"""
        SELECT a.id, a.name
        FROM artists a
        WHERE {ARTIST_ENGAGED}
        {where}
        AND a.id <> ALL(CAST(:skip AS uuid[]))
        ORDER BY a.last_similar_sync NULLS FIRST,
                 (SELECT MAX(lh.started_at)
                  FROM listening_history lh
                  JOIN track_artists ta ON ta.track_id = lh.track_id
                  WHERE ta.artist_id = a.id
                    AND lh.completed AND NOT lh.skipped) DESC NULLS LAST,
                 a.name
        {lim}
    """)
    params: Dict[str, Any] = {"skip": internal_failures("artist")}
    if limit:
        params["lim"] = limit
    stats = {"processed": 0, "stored": 0, "not_found": 0, "errors": 0}
    with get_db_context() as db:
        rows = db.execute(sql, params).fetchall()
    logger.info(f"backfill_similar: {len(rows)} engaged artists queued")
    for row in rows:
        if cancel_flag and cancel_flag():
            break
        try:
            with get_db_context() as db:
                r = svc.fetch_and_store_similar(db, row.id, row.name)
        except SourceUnavailable:
            logger.info("backfill_similar: Last.fm unavailable — ending batch")
            stats["unavailable"] = True
            break
        except Exception as e:
            stats["errors"] += 1
            note_internal_failure("artist", row.id)
            logger.error(f"backfill_similar failed for {row.name} in our code: {e}",
                         exc_info=True)
        else:
            stats["stored"] += r["stored"]
            if r["status"] == "not_found":
                stats["not_found"] += 1
        stats["processed"] += 1
    return stats


def backfill_lastfm_mbid(limit: Optional[int] = None, force: bool = False,
                         namesakes_only: bool = True) -> Dict[str, int]:
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
        except SourceUnavailable:
            logger.info("backfill_lastfm_mbid: Last.fm unavailable — ending batch")
            break
        except Exception as e:
            stats["errors"] += 1
            logger.error(f"backfill_lastfm_mbid failed for {row.name}: {e}")
    return stats
