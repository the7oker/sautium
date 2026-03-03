"""
Genius API integration for Music AI DJ.
Fetches plain lyrics from Genius via the lyricsgenius library.
"""

import logging
import re
from datetime import datetime
from typing import Dict, Optional, Any

from sqlalchemy.orm import Session

from models import TrackLyrics, ExternalMetadata

logger = logging.getLogger(__name__)


def clean_genius_lyrics(raw_lyrics: str) -> Optional[str]:
    """
    Remove artifacts from lyricsgenius-scraped text:
    - "Song Title Lyrics" header (first line)
    - "NNNEmbed" footer
    - "You might also like" injections
    - Ticket ad injections
    """
    if not raw_lyrics:
        return None

    text = raw_lyrics.strip()

    # Remove header line ending with "Lyrics"
    lines = text.split("\n")
    if lines and lines[0].rstrip().endswith("Lyrics"):
        lines = lines[1:]
    text = "\n".join(lines)

    # Remove "You might also like" injections
    text = re.sub(r"You might also like", "", text)

    # Remove "NNNEmbed" footer
    text = re.sub(r"\d*Embed$", "", text, flags=re.MULTILINE)

    # Remove ticket ads
    text = re.sub(r"See .* Live.*Get tickets as low as \$\d+", "", text)

    # Collapse excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip() or None


class GeniusService:
    """Service for fetching and storing lyrics from Genius."""

    def __init__(self, access_token: str):
        import lyricsgenius
        self.genius = lyricsgenius.Genius(
            access_token,
            verbose=False,
            remove_section_headers=False,  # keep [Chorus] etc.
            skip_non_songs=True,
            retries=2,
            timeout=10,
        )
        logger.info("Genius service initialized")

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def search_song(
        self, title: str, artist: str
    ) -> Optional[Dict[str, Any]]:
        """
        Search Genius for a song and return cleaned lyrics + metadata.
        Returns None if not found.
        """
        try:
            song = self.genius.search_song(
                title=title, artist=artist, get_full_info=False
            )
            if song is None:
                return None

            cleaned = clean_genius_lyrics(song.lyrics)
            if not cleaned:
                return None

            # lyricsgenius 3.x stores attributes in _body dict
            song_id = getattr(song, 'song_id', None) or getattr(song, 'id', None)
            if song_id is None and hasattr(song, '_body'):
                song_id = song._body.get('id')

            return {
                "id": song_id,
                "title": song.title,
                "artist": song.artist,
                "lyrics": cleaned,
            }
        except Exception as e:
            logger.error(f"Genius search failed for {artist} - {title}: {e}")
            return None

    def fetch_and_store(
        self,
        db: Session,
        track_id: str,
        track_name: str,
        artist_name: str,
        album_name: Optional[str] = None,
        duration: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Fetch lyrics from Genius and store in track_lyrics + external_metadata.

        Returns dict with:
          - status: 'plain' | 'not_found' | 'error'
          - data: the lyrics dict (if found)
        """
        track_id_str = str(track_id)

        try:
            data = self.search_song(title=track_name, artist=artist_name)
        except Exception as e:
            logger.error(f"Genius error for {artist_name} - {track_name}: {e}")
            self._store_external_metadata(db, track_id_str, "error", None)
            return {"status": "error"}

        if data is None:
            self._store_external_metadata(db, track_id_str, "not_found", None)
            return {"status": "not_found"}

        # Genius only provides plain lyrics (no synced/instrumental)
        existing = db.query(TrackLyrics).filter_by(
            track_id=track_id, source="genius"
        ).first()

        if existing:
            existing.plain_lyrics = data["lyrics"]
            existing.instrumental = False
            existing.external_id = data.get("id")
            existing.updated_at = datetime.utcnow()
        else:
            lyrics_record = TrackLyrics(
                track_id=track_id,
                source="genius",
                plain_lyrics=data["lyrics"],
                synced_lyrics=None,
                instrumental=False,
                external_id=data.get("id"),
            )
            db.add(lyrics_record)

        self._store_external_metadata(db, track_id_str, "success", data.get("id"))

        db.commit()
        return {"status": "plain", "data": data}

    def _store_external_metadata(
        self, db: Session, track_id_str: str, fetch_status: str, external_id: Optional[int]
    ):
        """Store or update fetch status in external_metadata table."""
        existing = db.query(ExternalMetadata).filter_by(
            entity_type="track",
            entity_id=track_id_str,
            source="genius",
            metadata_type="lyrics",
        ).first()

        meta_data = {"external_id": external_id} if external_id else {}

        if existing:
            existing.fetch_status = fetch_status
            existing.data = meta_data or {}
            existing.updated_at = datetime.utcnow()
        else:
            record = ExternalMetadata(
                entity_type="track",
                entity_id=track_id_str,
                source="genius",
                metadata_type="lyrics",
                data=meta_data or {},
                fetch_status=fetch_status,
            )
            db.add(record)

        db.commit()
