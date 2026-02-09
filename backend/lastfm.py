"""
Last.fm API integration for Music AI DJ.
Fetches artist bios, tags, and similar artists, storing in external_metadata table.
"""

import logging
import time
from typing import Dict, List, Optional, Any

import pylast
from sqlalchemy import text
from sqlalchemy.orm import Session
from decimal import Decimal

from config import settings
from models import (
    ExternalMetadata, Artist, SimilarArtist, Genre, GenreDescription,
    ArtistBio, Tag, ArtistTag, Album, AlbumInfo, AlbumTag
)

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

    def get_artist_info(self, artist_name: str) -> Optional[Dict[str, Any]]:
        """
        Fetch artist info from Last.fm.

        Returns dict with:
        - bio: {summary, content, published, url}
        - tags: [{name, count}, ...]
        - stats: {listeners, playcount}
        - similar: [{name, match, mbid}, ...]
        """
        try:
            artist = self.network.get_artist(artist_name)

            # Get MBID (MusicBrainz ID)
            mbid = None
            try:
                mbid = artist.get_mbid()
            except Exception as e:
                logger.debug(f"No MBID for {artist_name}: {e}")

            # Get bio
            bio_data = None
            try:
                bio = artist.get_bio_summary()
                content = artist.get_bio_content()
                bio_data = {
                    "summary": bio,
                    "content": content,
                    "url": artist.get_url(),
                }
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
            except Exception as e:
                logger.debug(f"No stats for {artist_name}: {e}")

            # Get similar artists
            similar_data = []
            try:
                similar = artist.get_similar(limit=20)
                similar_data = [
                    {
                        "name": similar_artist.item.get_name(),
                        "match": float(similar_artist.match),
                        "mbid": similar_artist.item.get_mbid() or None,
                    }
                    for similar_artist in similar
                ]
            except Exception as e:
                logger.debug(f"No similar artists for {artist_name}: {e}")

            return {
                "mbid": mbid,
                "bio": bio_data,
                "tags": tags_data,
                "stats": stats_data,
                "similar": similar_data,
            }

        except pylast.WSError as e:
            if "Artist not found" in str(e):
                logger.info(f"Artist not found on Last.fm: {artist_name}")
                return None
            else:
                logger.error(f"Last.fm API error for {artist_name}: {e}")
                raise
        except Exception as e:
            logger.error(f"Error fetching Last.fm data for {artist_name}: {e}")
            raise

    def store_artist_metadata(
        self, db: Session, artist_id: int, artist_name: str, data: Dict[str, Any]
    ) -> Dict[str, bool]:
        """
        Store Last.fm data in external_metadata table.

        Returns dict indicating what was stored: {bio: True, tags: True, similar: False, ...}
        """
        stored = {}

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
            stored["bio"] = False

        # Store tags in normalized tables
        if data.get("tags"):
            tags_count = self._store_artist_tags(db, artist_id, artist_name, data["tags"])
            stored["tags"] = tags_count > 0
            logger.debug(f"Stored {tags_count} tags for artist {artist_id} ({artist_name})")
        else:
            stored["tags"] = False

        # Store similar artists in normalized table
        if data.get("similar"):
            similar_count = self._store_similar_artists(db, artist_id, artist_name, data["similar"])
            stored["similar_artists"] = similar_count > 0
            logger.debug(
                f"Stored {similar_count}/{len(data['similar'])} similar artists for artist {artist_id} ({artist_name})"
            )
        else:
            stored["similar_artists"] = False

        # Update artist.lastfm_id with MBID if available
        if data.get("mbid"):
            artist_record = db.query(Artist).filter(Artist.id == artist_id).first()
            if artist_record:
                artist_record.lastfm_id = data["mbid"]
                logger.debug(f"Updated lastfm_id for artist {artist_id}: {data['mbid']}")

        db.commit()
        return stored

    def _store_similar_artists(
        self, db: Session, artist_id: int, artist_name: str, similar_data: List[Dict[str, Any]]
    ) -> int:
        """
        Store similar artists in normalized similar_artists table.
        Filters out compound artists and creates artist records as needed.

        Returns number of similar artists stored.
        """
        # Import here to avoid circular dependency
        from normalize_artists import is_compound_artist, normalize_artist_name

        stored_count = 0

        for similar in similar_data:
            similar_name = similar.get("name")
            match_score = similar.get("match", 0.0)

            if not similar_name:
                continue

            # Skip compound artists (e.g., "Pete Namlook & Klaus Schulze")
            if is_compound_artist(similar_name):
                logger.debug(f"Skipping compound similar artist: {similar_name}")
                continue

            # Get or create similar artist
            normalized_name = normalize_artist_name(similar_name)
            similar_artist = db.query(Artist).filter(
                Artist.name.ilike(normalized_name)
            ).first()

            if not similar_artist:
                # Create new artist
                similar_artist = Artist(name=normalized_name)
                db.add(similar_artist)
                db.flush()  # Get ID without committing
                logger.info(f"Created new artist from similar: {normalized_name} (ID: {similar_artist.id})")

            # Check if relationship already exists
            existing = db.query(SimilarArtist).filter(
                SimilarArtist.artist_id == artist_id,
                SimilarArtist.similar_artist_id == similar_artist.id,
                SimilarArtist.source == "lastfm"
            ).first()

            if existing:
                # Update match score if changed
                if abs(float(existing.match_score) - match_score) > 0.0001:
                    existing.match_score = Decimal(str(match_score))
                    logger.debug(f"Updated match score for {artist_name} -> {similar_name}: {match_score}")
            else:
                # Create new relationship
                similar_rel = SimilarArtist(
                    artist_id=artist_id,
                    similar_artist_id=similar_artist.id,
                    match_score=Decimal(str(match_score)),
                    source="lastfm"
                )
                db.add(similar_rel)
                stored_count += 1
                logger.debug(f"Added similar artist: {artist_name} -> {similar_name} (match: {match_score})")

        return stored_count

    def _store_artist_tags(
        self, db: Session, artist_id: int, artist_name: str, tags_data: List[Dict[str, Any]]
    ) -> int:
        """
        Store artist tags in normalized tags/artist_tags tables.
        Creates tag records as needed.

        Returns number of tags stored.
        """
        stored_count = 0

        for tag_item in tags_data:
            tag_name = tag_item.get("name")
            tag_weight = tag_item.get("count", 50)  # Default weight if missing

            if not tag_name or not tag_name.strip():
                continue

            # Normalize tag name
            tag_name = tag_name.strip()

            # Get or create tag
            tag = db.query(Tag).filter(
                Tag.name.ilike(tag_name)
            ).first()

            if not tag:
                # Create new tag
                tag = Tag(name=tag_name)
                db.add(tag)
                db.flush()  # Get ID without committing
                logger.debug(f"Created new tag: {tag_name} (ID: {tag.id})")

            # Check if artist_tag relationship already exists
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
                stored_count += 1
                logger.debug(f"Added tag: {artist_name} - {tag_name} (weight: {tag_weight})")

        return stored_count

    def _upsert_metadata(
        self,
        db: Session,
        entity_type: str,
        entity_id: int,
        source: str,
        metadata_type: str,
        data: Dict[str, Any],
        fetch_status: str = "success",
        error_message: Optional[str] = None,
    ):
        """Insert or update metadata record."""
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

    def enrich_artist(self, db: Session, artist_id: int, artist_name: str) -> Dict[str, Any]:
        """
        Fetch Last.fm data for an artist and store in database.

        Returns summary dict with status and stored flags.
        """
        logger.info(f"Enriching artist: {artist_name} (ID: {artist_id})")

        try:
            # Fetch from Last.fm
            data = self.get_artist_info(artist_name)

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

            # Store in database (also updates lastfm_id)
            stored = self.store_artist_metadata(db, artist_id, artist_name, data)

            return {
                "status": "success",
                "artist_id": artist_id,
                "artist_name": artist_name,
                "stored": stored,
                "mbid": data.get("mbid"),
                "tags_count": len(data.get("tags", [])),
                "similar_count": len(data.get("similar", [])),
            }

        except Exception as e:
            logger.error(f"Failed to enrich artist {artist_name}: {e}")

            # Store error
            self._upsert_metadata(
                db,
                entity_type="artist",
                entity_id=artist_id,
                source="lastfm",
                metadata_type="bio",
                data={},
                fetch_status="error",
                error_message=str(e),
            )
            db.commit()

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
        - reach: How many items have this tag
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

            # Get reach (popularity)
            reach = None
            try:
                reach = int(tag.get_reach())
            except Exception as e:
                logger.debug(f"No reach for tag {tag_name}: {e}")

            if not summary and not content:
                return None

            return {
                "summary": summary,
                "content": content,
                "reach": reach,
                "url": tag.get_url(),
            }

        except pylast.WSError as e:
            if "Tag not found" in str(e):
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
            # Fetch from Last.fm
            data = self.get_tag_info(genre_name)

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
                existing.reach = data.get("reach")
                logger.debug(f"Updated description for genre {genre_id} ({genre_name})")
            else:
                # Create new
                description = GenreDescription(
                    genre_id=genre_id,
                    source="lastfm",
                    summary=data.get("summary"),
                    content=data.get("content"),
                    url=data.get("url"),
                    reach=data.get("reach")
                )
                db.add(description)
                logger.debug(f"Created description for genre {genre_id} ({genre_name})")

            db.commit()

            return {
                "status": "success",
                "genre_id": genre_id,
                "genre_name": genre_name,
                "has_description": bool(data.get("summary") or data.get("content")),
                "reach": data.get("reach"),
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
                    SELECT 1 FROM external_metadata em
                    WHERE em.entity_type = 'genre'
                      AND em.entity_id = g.id
                      AND em.source = 'lastfm'
                      AND em.metadata_type = 'description'
                      AND em.fetch_status = 'success'
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
        # Get artists to enrich
        if skip_existing:
            # Find artists without Last.fm bio
            query = text("""
                SELECT DISTINCT a.id, a.name
                FROM artists a
                WHERE NOT EXISTS (
                    SELECT 1 FROM external_metadata em
                    WHERE em.entity_type = 'artist'
                      AND em.entity_id = a.id
                      AND em.source = 'lastfm'
                      AND em.metadata_type = 'bio'
                      AND em.fetch_status = 'success'
                )
                ORDER BY a.name
            """)
        else:
            query = text("SELECT id, name FROM artists ORDER BY name")

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

    def get_album_info(self, artist_name: str, album_title: str) -> Optional[Dict[str, Any]]:
        """
        Fetch album info from Last.fm.

        Returns dict with:
        - mbid: MusicBrainz ID
        - wiki: {summary, content, url}
        - tags: [{name, count}, ...]
        - stats: {listeners, playcount}
        - tracks: [{name, duration}, ...] (optional)
        """
        try:
            album = self.network.get_album(artist_name, album_title)

            # Get MBID
            mbid = None
            try:
                mbid = album.get_mbid()
            except Exception as e:
                logger.debug(f"No MBID for {artist_name} - {album_title}: {e}")

            # Get wiki
            wiki_data = None
            try:
                summary = album.get_wiki_summary()
                content = album.get_wiki_content()
                if summary or content:
                    wiki_data = {
                        "summary": summary,
                        "content": content,
                        "url": album.get_url(),
                    }
            except Exception as e:
                logger.debug(f"No wiki for {artist_name} - {album_title}: {e}")

            # Get stats
            stats_data = {}
            try:
                stats_data = {
                    "listeners": int(album.get_listener_count()),
                    "playcount": int(album.get_playcount()),
                }
            except Exception as e:
                logger.debug(f"No stats for {artist_name} - {album_title}: {e}")

            # Get tags
            tags_data = []
            try:
                top_tags = album.get_top_tags(limit=30)
                tags_data = [
                    {"name": tag.item.get_name(), "count": int(tag.weight)}
                    for tag in top_tags
                ]
            except Exception as e:
                logger.debug(f"No tags for {artist_name} - {album_title}: {e}")

            # Get tracks (optional, can fail)
            tracks_data = []
            try:
                tracks = album.get_tracks()
                tracks_data = [
                    {
                        "name": track.get_name(),
                        "duration": track.get_duration() // 1000 if track.get_duration() else None
                    }
                    for track in tracks
                ]
            except Exception as e:
                logger.debug(f"No tracks for {artist_name} - {album_title}: {e}")

            return {
                "mbid": mbid,
                "wiki": wiki_data,
                "tags": tags_data,
                "stats": stats_data,
                "tracks": tracks_data,
            }

        except pylast.WSError as e:
            if "Album not found" in str(e):
                logger.info(f"Album not found on Last.fm: {artist_name} - {album_title}")
                return None
            else:
                logger.error(f"Last.fm API error for {artist_name} - {album_title}: {e}")
                raise
        except Exception as e:
            logger.error(f"Error fetching Last.fm data for {artist_name} - {album_title}: {e}")
            raise

    def enrich_album(self, db: Session, album_id: int, artist_name: str, album_title: str) -> Dict[str, Any]:
        """
        Fetch Last.fm data for an album and store in database.

        Returns summary dict with status and stored flags.
        """
        logger.info(f"Enriching album: {artist_name} - {album_title} (ID: {album_id})")

        try:
            # Fetch from Last.fm
            data = self.get_album_info(artist_name, album_title)

            if data is None:
                # Album not found
                logger.warning(f"Album not found on Last.fm: {artist_name} - {album_title}")
                return {
                    "status": "not_found",
                    "album_id": album_id,
                    "artist_name": artist_name,
                    "album_title": album_title,
                }

            # Update album.lastfm_id with MBID
            if data.get("mbid"):
                album_record = db.query(Album).filter(Album.id == album_id).first()
                if album_record:
                    album_record.lastfm_id = data["mbid"]
                    logger.debug(f"Updated lastfm_id for album {album_id}: {data['mbid']}")

            # Store album info (wiki + stats)
            if data.get("wiki") or data.get("stats"):
                self._store_album_info(db, album_id, artist_name, album_title, data)

            # Store album tags
            tags_count = 0
            if data.get("tags"):
                tags_count = self._store_album_tags(db, album_id, album_title, data["tags"])

            db.commit()

            return {
                "status": "success",
                "album_id": album_id,
                "artist_name": artist_name,
                "album_title": album_title,
                "mbid": data.get("mbid"),
                "has_wiki": bool(data.get("wiki")),
                "tags_count": tags_count,
                "listeners": data.get("stats", {}).get("listeners"),
                "playcount": data.get("stats", {}).get("playcount"),
            }

        except Exception as e:
            logger.error(f"Failed to enrich album {artist_name} - {album_title}: {e}")
            db.rollback()

            return {
                "status": "error",
                "album_id": album_id,
                "artist_name": artist_name,
                "album_title": album_title,
                "error": str(e),
            }

    def _store_album_info(
        self, db: Session, album_id: int, artist_name: str, album_title: str, data: Dict[str, Any]
    ):
        """Store album info (wiki + stats) in album_info table."""
        wiki = data.get("wiki") or {}
        stats = data.get("stats") or {}

        existing = db.query(AlbumInfo).filter(
            AlbumInfo.album_id == album_id,
            AlbumInfo.source == "lastfm"
        ).first()

        if existing:
            # Update existing
            existing.summary = wiki.get("summary")
            existing.content = wiki.get("content")
            existing.url = wiki.get("url")
            existing.listeners = stats.get("listeners")
            existing.playcount = stats.get("playcount")
            logger.debug(f"Updated album info for album {album_id} ({album_title})")
        else:
            # Create new
            album_info = AlbumInfo(
                album_id=album_id,
                source="lastfm",
                summary=wiki.get("summary"),
                content=wiki.get("content"),
                url=wiki.get("url"),
                listeners=stats.get("listeners"),
                playcount=stats.get("playcount")
            )
            db.add(album_info)
            logger.debug(f"Created album info for album {album_id} ({album_title})")

    def _store_album_tags(
        self, db: Session, album_id: int, album_title: str, tags_data: List[Dict[str, Any]]
    ) -> int:
        """
        Store album tags in normalized tags/album_tags tables.
        Creates tag records as needed.

        Returns number of tags stored.
        """
        stored_count = 0

        for tag_item in tags_data:
            tag_name = tag_item.get("name")
            tag_weight = tag_item.get("count", 50)

            if not tag_name or not tag_name.strip():
                continue

            tag_name = tag_name.strip()

            # Get or create tag
            tag = db.query(Tag).filter(
                Tag.name.ilike(tag_name)
            ).first()

            if not tag:
                tag = Tag(name=tag_name)
                db.add(tag)
                db.flush()
                logger.debug(f"Created new tag: {tag_name} (ID: {tag.id})")

            # Check if album_tag relationship already exists
            existing = db.query(AlbumTag).filter(
                AlbumTag.album_id == album_id,
                AlbumTag.tag_id == tag.id,
                AlbumTag.source == "lastfm"
            ).first()

            if existing:
                # Update weight if changed
                if existing.weight != tag_weight:
                    existing.weight = tag_weight
                    logger.debug(f"Updated tag weight for album {album_id} - {tag_name}: {tag_weight}")
            else:
                # Create new relationship
                album_tag = AlbumTag(
                    album_id=album_id,
                    tag_id=tag.id,
                    weight=tag_weight,
                    source="lastfm"
                )
                db.add(album_tag)
                stored_count += 1
                logger.debug(f"Added tag: album {album_id} - {tag_name} (weight: {tag_weight})")

        return stored_count
