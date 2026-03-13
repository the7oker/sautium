"""
Music library scanner for extracting metadata from audio files.

Creates canonical entities (Artist, Track, Album) with deterministic UUIDs
and physical entities (AlbumVariant, MediaFile) per file on disk.
"""

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

import mutagen
from mutagen.flac import FLAC
from mutagen import MutagenError
from sqlalchemy.orm import Session
from tqdm import tqdm

from config import settings
from models import (
    Artist, Album, Track, TrackArtist, TrackGenre, AlbumArtist,
    AlbumVariant, MediaFile, Genre,
)
from database import get_db_context
from uuid_utils import artist_uuid, track_uuid, album_uuid, genre_uuid, is_lossless as check_lossless

logger = logging.getLogger(__name__)

# Supported audio extensions
AUDIO_EXTENSIONS = {'.flac', '.ape', '.wav', '.aiff', '.wv', '.tta', '.dsf', '.dff', '.mp3', '.ogg', '.m4a'}


class LibraryScanner:
    """Scanner for music library audio files."""

    def __init__(self, library_path: Optional[str] = None):
        """Initialize scanner with library path."""
        self.library_path = Path(library_path or settings.music_library_path)

        if not self.library_path.exists():
            raise ValueError(f"Library path does not exist: {self.library_path}")

        logger.info(f"Initialized scanner for: {self.library_path}")

    @staticmethod
    def extract_metadata(file_path: Path) -> Optional[Dict[str, Any]]:
        """
        Extract metadata from audio file.

        Returns:
            Dictionary with extracted metadata or None if failed.
        """
        try:
            audio = mutagen.File(file_path)
            if audio is None:
                logger.warning(f"Unsupported format: {file_path}")
                return None

            # Extract basic tags
            file_stat = file_path.stat()
            file_format = file_path.suffix.lstrip('.').upper()

            # Audio properties — not all formats expose all fields
            info = audio.info if hasattr(audio, 'info') and audio.info else None
            bit_depth = None
            if info and hasattr(info, 'bits_per_sample'):
                bit_depth = info.bits_per_sample

            # Universal tag getter: handles Vorbis (FLAC/OGG), ID3 (MP3/DSF),
            # MP4 (M4A/AAC), and APE (WavPack/Musepack) tag formats
            def get_tag(vorbis_key: str, id3_key: str = None,
                        mp4_key: str = None, default=None):
                """Get tag value from any supported format."""
                # Try Vorbis-style key first (works for FLAC, OGG, easy=True)
                val = audio.get(vorbis_key)
                if val:
                    return str(val[0]) if isinstance(val, list) else str(val)
                # Try ID3 frame (MP3, DSF, AIFF)
                if id3_key and audio.tags:
                    frame = audio.tags.get(id3_key)
                    if frame:
                        return str(frame)
                # Try MP4 atom (M4A, AAC, ALAC)
                if mp4_key and audio.tags:
                    val = audio.tags.get(mp4_key)
                    if val:
                        item = val[0] if isinstance(val, list) else val
                        # MP4 trkn/disk are tuples like (track_num, total)
                        if isinstance(item, tuple):
                            return str(item[0])
                        return str(item)
                return default

            def get_tag_int(vorbis_key: str, id3_key: str = None,
                           mp4_key: str = None) -> Optional[str]:
                """Get tag that should be parsed as int (track/disc number)."""
                return get_tag(vorbis_key, id3_key, mp4_key)

            metadata = {
                # File information — translate to native OS path for DB storage
                "file_path": settings.translate_to_host_path(str(file_path.absolute())),
                "file_size_bytes": file_stat.st_size,
                "file_format": file_format,
                "file_modified_at": datetime.fromtimestamp(file_stat.st_mtime),
                "is_lossless": check_lossless(file_format),

                # Audio properties
                "duration_seconds": round(info.length, 2) if info else None,
                "sample_rate": info.sample_rate if info and hasattr(info, 'sample_rate') else None,
                "bit_depth": bit_depth,
                "channels": info.channels if info and hasattr(info, 'channels') else None,
                "bitrate": int(info.bitrate / 1000) if info and hasattr(info, 'bitrate') and info.bitrate else None,

                # Metadata tags — universal across formats
                "title": get_tag("title", "TIT2", "\xa9nam"),
                "artist": get_tag("artist", "TPE1", "\xa9ART"),
                "album": get_tag("album", "TALB", "\xa9alb"),
                "album_artist": (get_tag("albumartist", "TPE2", "aART")
                                 or get_tag("album artist")),
                "genre": get_tag("genre", "TCON", "\xa9gen"),
                "date": get_tag("date", "TDRC", "\xa9day"),
                "track_number": get_tag("tracknumber", "TRCK", "trkn"),
                "disc_number": get_tag("discnumber", "TPOS", "disk") or "1",
                "label": (get_tag("label", "TPUB")
                          or get_tag("publisher", "TPUB")),
                "catalog_number": get_tag("catalognumber"),
                "isrc": get_tag("isrc", "TSRC"),
            }

            # Parse track number (handle "1/12" format)
            if metadata["track_number"]:
                track_num = str(metadata["track_number"]).split("/")[0]
                try:
                    metadata["track_number"] = int(track_num)
                except ValueError:
                    metadata["track_number"] = None

            # Parse disc number
            if metadata["disc_number"]:
                disc_num = str(metadata["disc_number"]).split("/")[0]
                try:
                    metadata["disc_number"] = int(disc_num)
                except ValueError:
                    metadata["disc_number"] = 1

            # Parse year from date
            if metadata["date"]:
                year_match = re.search(r'\d{4}', str(metadata["date"]))
                if year_match:
                    metadata["release_year"] = int(year_match.group())
                else:
                    metadata["release_year"] = None
            else:
                metadata["release_year"] = None

            return metadata

        except MutagenError as e:
            logger.error(f"Failed to read {file_path}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error reading {file_path}: {e}")
            return None

    def find_audio_files(self, limit: Optional[int] = None, subpath: Optional[str] = None) -> List[Path]:
        """
        Recursively find all audio files in library.

        Args:
            limit: Maximum number of files to return (for testing).
            subpath: Optional subdirectory within library to scan.

        Returns:
            List of Path objects for audio files.
        """
        if subpath:
            scan_path = self.library_path / subpath
            if not scan_path.exists():
                raise ValueError(f"Subpath does not exist: {scan_path}")
            logger.info(f"Searching for audio files in {scan_path} (subpath: {subpath})")
        else:
            scan_path = self.library_path
            logger.info(f"Searching for audio files in {scan_path}")

        audio_files = []
        for file_path in scan_path.rglob("*"):
            if file_path.is_file() and file_path.suffix.lower() in AUDIO_EXTENSIONS:
                audio_files.append(file_path)
                if limit and len(audio_files) >= limit:
                    break

        logger.info(f"Found {len(audio_files)} audio files")
        return audio_files

    @staticmethod
    def get_or_create_genre(db: Session, genre_name: str) -> Genre:
        """Get existing genre or create new one (deterministic UUID PK)."""
        name = genre_name.strip()
        gid = genre_uuid(name)
        genre = db.query(Genre).filter(Genre.id == gid).first()

        if not genre:
            genre = Genre(id=gid, name=name)
            db.add(genre)
            db.flush()
            logger.debug(f"Created genre: {name}")

        return genre

    @staticmethod
    def get_or_create_artist(db: Session, artist_name: str) -> Artist:
        """Get existing artist or create new one. Uses deterministic UUID."""
        uid = artist_uuid(artist_name)
        artist = db.query(Artist).filter(Artist.id == uid).first()

        if not artist:
            artist = Artist(id=uid, name=artist_name)
            db.add(artist)
            db.flush()
            logger.debug(f"Created artist: {artist_name} ({uid})")

        return artist

    @staticmethod
    def get_or_create_track(db: Session, title: str, artist_name: str) -> Track:
        """Get existing track or create new one. Uses deterministic UUID."""
        uid = track_uuid(title, artist_name)
        track = db.query(Track).filter(Track.id == uid).first()

        if not track:
            track = Track(id=uid, title=title)
            db.add(track)
            db.flush()
            logger.debug(f"Created track: {title} ({uid})")

        return track

    @staticmethod
    def get_or_create_album(
        db: Session,
        album_title: str,
        artist_name: str,
        metadata: Dict[str, Any],
    ) -> Album:
        """Get existing album or create new one. Uses deterministic UUID."""
        uid = album_uuid(album_title, artist_name)
        album = db.query(Album).filter(Album.id == uid).first()

        if not album:
            album = Album(
                id=uid,
                title=album_title,
                release_year=metadata.get("release_year"),
                label=metadata.get("label"),
                catalog_number=metadata.get("catalog_number"),
            )
            db.add(album)
            db.flush()
            logger.debug(f"Created album: {album_title} ({uid})")

        return album

    @staticmethod
    def get_or_create_album_variant(
        db: Session,
        album: Album,
        directory_path: str,
        metadata: Dict[str, Any],
    ) -> AlbumVariant:
        """Get existing album variant or create new one (identified by directory_path)."""
        variant = db.query(AlbumVariant).filter(
            AlbumVariant.directory_path == directory_path
        ).first()

        if not variant:
            variant = AlbumVariant(
                album_id=album.id,
                directory_path=directory_path,
                sample_rate=metadata.get("sample_rate"),
                bit_depth=metadata.get("bit_depth"),
                is_lossless=metadata.get("is_lossless", True),
            )
            db.add(variant)
            db.flush()
            logger.debug(f"Created album variant: {directory_path}")

        return variant

    @staticmethod
    def _update_analysis_source(db: Session, track_id):
        """Set is_analysis_source for the best quality file per track.

        Priority: CD (16bit lossless) > other lossless > lossy.
        """
        from sqlalchemy import text
        db.execute(text("""
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           ORDER BY
                               (bit_depth = 16 AND is_lossless) DESC,
                               is_lossless DESC,
                               id
                       ) as rn
                FROM media_files
                WHERE track_id = :tid
            )
            UPDATE media_files
            SET is_analysis_source = (id = (SELECT id FROM ranked WHERE rn = 1))
            WHERE track_id = :tid
        """), {"tid": track_id})

    def scan_and_import(
        self,
        limit: Optional[int] = None,
        skip_existing: bool = True,
        subpath: Optional[str] = None,
        progress_cb: Optional[callable] = None,
        cancel_check: Optional[callable] = None,
    ) -> Dict[str, int]:
        """
        Scan library and import metadata to database.

        Args:
            limit: Maximum number of files to scan (for testing).
            skip_existing: Skip files already in database.
            subpath: Optional subdirectory within library to scan.
            progress_cb: Callback(msg, stats) for progress reporting.
            cancel_check: Callback() -> bool, returns True if cancel requested.

        Returns:
            Dictionary with statistics (processed, added, skipped, errors).
        """
        stats = {
            "processed": 0,
            "added": 0,
            "skipped": 0,
            "errors": 0,
            "unique_tracks": 0,
        }
        seen_track_ids = set()

        def _report(msg: str = None):
            if progress_cb:
                progress_cb(msg or f"Scanned {stats['processed']}/{total_files}", stats)

        # Find audio files
        if progress_cb:
            progress_cb("Discovering files...", stats)
        audio_files = self.find_audio_files(limit=limit, subpath=subpath)

        if not audio_files:
            logger.warning("No audio files found")
            return stats

        total_files = len(audio_files)
        _report(f"Found {total_files} files, starting scan...")

        with get_db_context() as db:
            # Get existing file paths for skip check
            if skip_existing:
                existing_paths = set(
                    path[0] for path in db.query(MediaFile.file_path).all()
                )
                logger.info(f"Found {len(existing_paths)} existing media files in database")
            else:
                existing_paths = set()

            # Process files with progress bar
            for file_path in tqdm(audio_files, desc="Scanning files", unit="file"):
                # Check for cancellation
                if cancel_check and cancel_check():
                    logger.info("Scan cancelled by user")
                    db.commit()
                    break
                stats["processed"] += 1

                # Report progress every 50 files
                if stats["processed"] % 50 == 0:
                    _report()

                # Skip if already in database (compare translated path)
                if skip_existing and settings.translate_to_host_path(str(file_path.absolute())) in existing_paths:
                    stats["skipped"] += 1
                    continue

                # Extract metadata
                metadata = self.extract_metadata(file_path)
                if not metadata:
                    stats["errors"] += 1
                    continue

                try:
                    # Validate required fields
                    if not metadata.get("title"):
                        logger.warning(f"Missing title for {file_path}, skipping")
                        stats["errors"] += 1
                        continue

                    # Get artist name (prefer album artist, fallback to artist)
                    artist_name = metadata.get("album_artist") or metadata.get("artist")
                    if not artist_name:
                        logger.warning(f"Missing artist for {file_path}, skipping")
                        stats["errors"] += 1
                        continue

                    album_title = metadata.get("album")
                    if not album_title:
                        # Treat as a single — use track title as album name
                        album_title = metadata["title"]
                        logger.info(f"No album tag, using title as album: {album_title}")

                    # Get or create canonical entities
                    # Note: compound artist names (e.g. "Beth Hart & Joe Bonamassa")
                    # are stored as-is. Splitting into individual artists is done
                    # separately by normalize_artists.py (with Last.fm verification)
                    # to avoid incorrectly splitting band names like "Simon & Garfunkel".
                    artist = self.get_or_create_artist(db, artist_name)
                    track = self.get_or_create_track(db, metadata["title"], artist_name)
                    album = self.get_or_create_album(db, album_title, artist_name, metadata)

                    # Get or create album variant (physical edition)
                    variant = self.get_or_create_album_variant(
                        db, album, settings.translate_to_host_path(str(file_path.parent)), metadata
                    )

                    # Create track-artist association (if not exists)
                    existing_ta = db.query(TrackArtist).filter(
                        TrackArtist.track_id == track.id,
                        TrackArtist.artist_id == artist.id,
                        TrackArtist.role == "primary",
                    ).first()
                    if not existing_ta:
                        db.add(TrackArtist(
                            track_id=track.id,
                            artist_id=artist.id,
                            role="primary",
                        ))

                    # Create album-artist association (if not exists)
                    existing_aa = db.query(AlbumArtist).filter(
                        AlbumArtist.album_id == album.id,
                        AlbumArtist.artist_id == artist.id,
                        AlbumArtist.role == "primary",
                    ).first()
                    if not existing_aa:
                        db.add(AlbumArtist(
                            album_id=album.id,
                            artist_id=artist.id,
                            role="primary",
                        ))

                    # Create track-genre associations (split composite genres)
                    genre_name = metadata.get("genre")
                    if genre_name and genre_name.strip():
                        from normalize_genres import parse_genre_string, normalize_genre_name
                        genre_names = parse_genre_string(genre_name)
                        for gn in genre_names:
                            gn = normalize_genre_name(gn)
                            genre = self.get_or_create_genre(db, gn)
                            existing_tg = db.query(TrackGenre).filter(
                                TrackGenre.track_id == track.id,
                                TrackGenre.genre_id == genre.id,
                            ).first()
                            if not existing_tg:
                                db.add(TrackGenre(
                                    track_id=track.id,
                                    genre_id=genre.id,
                                ))

                    # Create media file (physical file on disk)
                    media_file = MediaFile(
                        track_id=track.id,
                        album_variant_id=variant.id,
                        file_path=metadata["file_path"],
                        file_format=metadata.get("file_format", "FLAC"),
                        is_lossless=metadata.get("is_lossless", True),
                        file_size_bytes=metadata.get("file_size_bytes"),
                        file_modified_at=metadata.get("file_modified_at"),
                        sample_rate=metadata.get("sample_rate"),
                        bit_depth=metadata.get("bit_depth"),
                        bitrate=metadata.get("bitrate"),
                        channels=metadata.get("channels"),
                        duration_seconds=metadata.get("duration_seconds"),
                        track_number=metadata.get("track_number"),
                        disc_number=metadata.get("disc_number", 1),
                        isrc=metadata.get("isrc"),
                    )
                    db.add(media_file)
                    db.flush()

                    # Update is_analysis_source: CD quality (16bit lossless) > lossless > lossy
                    self._update_analysis_source(db, track.id)

                    stats["added"] += 1
                    if track.id not in seen_track_ids:
                        seen_track_ids.add(track.id)
                        stats["unique_tracks"] += 1

                    # Commit every 100 files to avoid huge transactions
                    if stats["added"] % 100 == 0:
                        db.commit()
                        _report()
                        logger.info(f"Progress: {stats['added']} files added")

                except Exception as e:
                    logger.error(f"Error processing {file_path}: {e}")
                    stats["errors"] += 1
                    db.rollback()

            # Final commit
            db.commit()

        _report(f"Done: {stats['added']} added, {stats['skipped']} skipped, {stats['errors']} errors")
        logger.info(
            f"Scan complete: {stats['processed']} processed, "
            f"{stats['added']} added, {stats['skipped']} skipped, "
            f"{stats['errors']} errors"
        )

        return stats


def scan_library(limit: Optional[int] = None, skip_existing: bool = True, subpath: Optional[str] = None) -> Dict[str, int]:
    """
    Convenience function to scan library.

    Args:
        limit: Maximum number of files to scan.
        skip_existing: Skip files already in database.
        subpath: Optional subdirectory within library to scan.

    Returns:
        Statistics dictionary.
    """
    scanner = LibraryScanner()
    return scanner.scan_and_import(limit=limit, skip_existing=skip_existing, subpath=subpath)
