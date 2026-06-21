"""
LRCLIB API integration for Sautium.
Fetches plain and synced (LRC) lyrics from lrclib.net.
"""

import logging
import re
import time
from datetime import datetime
from typing import Dict, List, Optional, Any

import httpx
from sqlalchemy.orm import Session

from models import TrackLyrics, ExternalMetadata

logger = logging.getLogger(__name__)

LRCLIB_BASE_URL = "https://lrclib.net"
USER_AGENT = "Sautium/1.0 (https://github.com/sautium-dj)"


class LrclibService:
    """Service for fetching and storing lyrics from LRCLIB."""

    def __init__(self):
        self.client = httpx.Client(
            base_url=LRCLIB_BASE_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=10.0,
        )
        logger.info("LRCLIB service initialized")

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _get_with_retry(self, path: str, params: dict, max_retries: int = 3, base_delay: float = 1.0) -> "httpx.Response":
        """GET with exponential backoff on transient failures (timeouts, network
        errors, 429, 5xx). A 404 returns immediately (genuine miss). After retries
        are exhausted the error propagates, so the caller records a retryable
        'error' rather than poisoning the negative cache with a permanent
        not_found for what was only a transient blip (LRClib read-times-out on
        niche/Cyrillic queries — those must be retried, not cached as missing)."""
        for attempt in range(max_retries + 1):
            try:
                resp = self.client.get(path, params=params)
                if resp.status_code == 404:
                    return resp
                if (resp.status_code == 429 or resp.status_code >= 500) and attempt < max_retries:
                    time.sleep(base_delay * (2 ** attempt))
                    continue
                resp.raise_for_status()
                return resp
            except httpx.TransportError:
                if attempt < max_retries:
                    time.sleep(base_delay * (2 ** attempt))
                    continue
                raise

    def get_lyrics(
        self,
        track_name: str,
        artist_name: str,
        album_name: Optional[str] = None,
        duration: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch lyrics from LRCLIB via exact match (/api/get).

        Returns dict with id, plainLyrics, syncedLyrics, instrumental, etc.
        Returns None if not found.
        """
        params = {
            "track_name": track_name,
            "artist_name": artist_name,
        }
        if album_name:
            params["album_name"] = album_name
        if duration is not None:
            params["duration"] = duration

        # Only a genuine 404 is a real miss and returns None. Rate-limit (429),
        # 5xx, timeouts and network errors must propagate so the caller records a
        # retryable 'error' — swallowing them as None used to poison the negative
        # cache, marking tracks lyric-less forever after a transient blip.
        resp = self._get_with_retry("/api/get", params)
        if resp.status_code == 404:
            return None
        return resp.json()

    def search_lyrics(
        self,
        track_name: str,
        artist_name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Fallback search via /api/search. Returns best match or None.
        """
        params = {"q": track_name}
        if artist_name:
            params["artist_name"] = artist_name

        resp = self._get_with_retry("/api/search", params)
        if resp.status_code == 404:
            return None
        results = resp.json()
        # Return the first (best) match; transient errors propagate to the caller.
        return results[0] if results else None

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
        Fetch lyrics from LRCLIB and store in track_lyrics + external_metadata.

        Returns dict with:
          - status: 'synced' | 'plain' | 'instrumental' | 'not_found' | 'error'
          - data: the lyrics dict (if found)
        """
        track_id_str = str(track_id)

        # Try exact match first, then the search fallback. A transient failure
        # (429/5xx/network/timeout) propagates out of get_lyrics/search_lyrics and
        # is recorded as a retryable 'error'; only a genuine 404/empty result below
        # becomes not_found. This keeps the negative cache free of transient blips.
        try:
            data = self.get_lyrics(track_name, artist_name, album_name, duration)
            if data is None:
                data = self.search_lyrics(track_name, artist_name)
        except Exception as e:
            logger.warning(f"LRCLIB transient failure for {artist_name} - {track_name}: {e}")
            self._store_external_metadata(db, track_id_str, "error", None)
            return {"status": "error", "error": str(e)}

        if data is None:
            # Genuine miss: LRClib has no match for this exact track
            self._store_external_metadata(db, track_id_str, "not_found", None)
            return {"status": "not_found"}

        # Determine status
        is_instrumental = data.get("instrumental", False)
        has_synced = bool(data.get("syncedLyrics"))
        has_plain = bool(data.get("plainLyrics"))

        if is_instrumental:
            status = "instrumental"
        elif has_synced:
            status = "synced"
        elif has_plain:
            status = "plain"
        else:
            # Empty response — treat as not found
            self._store_external_metadata(db, track_id_str, "not_found", None)
            return {"status": "not_found"}

        # Upsert into track_lyrics
        existing = db.query(TrackLyrics).filter_by(
            track_id=track_id, source="lrclib"
        ).first()

        if existing:
            existing.plain_lyrics = data.get("plainLyrics")
            existing.synced_lyrics = data.get("syncedLyrics")
            existing.instrumental = is_instrumental
            existing.external_id = data.get("id")
            existing.updated_at = datetime.utcnow()
        else:
            lyrics_record = TrackLyrics(
                track_id=track_id,
                source="lrclib",
                plain_lyrics=data.get("plainLyrics"),
                synced_lyrics=data.get("syncedLyrics"),
                instrumental=is_instrumental,
                external_id=data.get("id"),
            )
            db.add(lyrics_record)

        # Store success in external_metadata
        self._store_external_metadata(db, track_id_str, "success", data.get("id"))

        db.commit()
        return {"status": status, "data": data}

    def _store_external_metadata(
        self, db: Session, track_id_str: str, fetch_status: str, external_id: Optional[int]
    ):
        """Store or update fetch status in external_metadata table."""
        existing = db.query(ExternalMetadata).filter_by(
            entity_type="track",
            entity_id=track_id_str,
            source="lrclib",
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
                source="lrclib",
                metadata_type="lyrics",
                data=meta_data or {},
                fetch_status=fetch_status,
            )
            db.add(record)

        db.commit()

    @staticmethod
    def parse_lrc(lrc_text: str) -> List[Dict[str, Any]]:
        """
        Parse LRC format text into a list of {time_ms, text} objects.

        LRC format: [mm:ss.xx] lyrics text
        """
        if not lrc_text:
            return []

        lines = []
        pattern = re.compile(r"\[(\d{2}):(\d{2})\.(\d{2,3})\]\s*(.*)")

        for line in lrc_text.strip().split("\n"):
            match = pattern.match(line.strip())
            if match:
                minutes = int(match.group(1))
                seconds = int(match.group(2))
                centis = match.group(3)
                # Handle both .xx (centiseconds) and .xxx (milliseconds)
                if len(centis) == 2:
                    ms = int(centis) * 10
                else:
                    ms = int(centis)
                time_ms = (minutes * 60 + seconds) * 1000 + ms
                text = match.group(4)
                lines.append({"time_ms": time_ms, "text": text})

        return lines
