"""
Genius API integration for Music AI DJ.
Fetches plain lyrics from Genius via the lyricsgenius library.

Known issue: Genius.com hosts non-lyrics content (novels, legislation,
release calendars, essays) using the same page format as lyrics.
Their search API can return these pages for instrumental/electronic tracks.
We validate results with fuzzy title matching and structural heuristics.
"""

import logging
import re
from datetime import datetime
from typing import Dict, Optional, Any

from sqlalchemy.orm import Session

from models import TrackLyrics, ExternalMetadata

logger = logging.getLogger(__name__)

# --- Lyrics validation constants ---
# Max characters for valid lyrics. Longest legitimate songs rarely exceed 5000 chars.
# Genius junk can be 100K-2.5M (novels, legislation, release calendars).
MAX_LYRICS_CHARS = 5000

# Minimum fuzzy match score (0-100) between requested and returned title/artist.
MIN_FUZZY_MATCH_SCORE = 50

# Average words-per-line threshold. Real lyrics: 3-12 words/line.
# Prose (novels, essays): 15-30+ words/line.
MAX_AVG_WORDS_PER_LINE = 16

# Minimum number of lines to apply structural checks (very short lyrics skip these).
MIN_LINES_FOR_STRUCTURE_CHECK = 5


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


def _fuzzy_match(a: str, b: str) -> int:
    """Simple fuzzy match score (0-100) without external dependencies."""
    a = re.sub(r"[^\w\s]", "", a.lower()).strip()
    b = re.sub(r"[^\w\s]", "", b.lower()).strip()
    if not a or not b:
        return 0
    if a == b:
        return 100
    # Check if one contains the other
    if a in b or b in a:
        return 80
    # Token overlap ratio
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    if not tokens_a or not tokens_b:
        return 0
    overlap = len(tokens_a & tokens_b)
    total = max(len(tokens_a), len(tokens_b))
    return int(overlap / total * 100)


def validate_lyrics(
    lyrics: str,
    requested_title: str,
    requested_artist: str,
    returned_title: Optional[str] = None,
    returned_artist: Optional[str] = None,
) -> Optional[str]:
    """
    Validate that scraped text is actually song lyrics, not junk.

    Returns None if valid, or a rejection reason string if invalid.
    """
    if not lyrics or not lyrics.strip():
        return "empty"

    # 1. Length check — novels/legislation are 100K-2.5M chars
    if len(lyrics) > MAX_LYRICS_CHARS:
        return f"too_long ({len(lyrics)} chars)"

    # 2. Fuzzy title match — catch completely wrong pages
    if returned_title:
        title_score = _fuzzy_match(requested_title, returned_title)
        if title_score < MIN_FUZZY_MATCH_SCORE:
            # Also check artist to be lenient (compilation pages etc.)
            artist_score = _fuzzy_match(requested_artist, returned_artist or "")
            if artist_score < MIN_FUZZY_MATCH_SCORE:
                return f"title_mismatch (req='{requested_title}' got='{returned_title}' score={title_score})"

    # 3. Structural analysis — lyrics have short lines, prose has long paragraphs
    lines = [line for line in lyrics.split("\n") if line.strip()]
    if len(lines) >= MIN_LINES_FOR_STRUCTURE_CHECK:
        words_per_line = [len(line.split()) for line in lines]
        avg_wpl = sum(words_per_line) / len(words_per_line)
        if avg_wpl > MAX_AVG_WORDS_PER_LINE:
            return f"prose_structure (avg {avg_wpl:.1f} words/line)"

    return None


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
        Returns None if not found or if validation fails (junk content).
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

            # Validate lyrics aren't junk (novels, legislation, etc.)
            rejection = validate_lyrics(
                cleaned,
                requested_title=title,
                requested_artist=artist,
                returned_title=song.title,
                returned_artist=song.artist,
            )
            if rejection:
                logger.warning(
                    f"Genius lyrics rejected for {artist} - {title}: {rejection}"
                )
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
