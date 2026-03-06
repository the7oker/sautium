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
            audio = FLAC(file_path)

            # Extract basic tags
            file_stat = file_path.stat()
            file_format = file_path.suffix.lstrip('.').upper()

            metadata = {
                # File information — translate to native OS path for DB storage
                "file_path": settings.translate_to_host_path(str(file_path.absolute())),
                "file_size_bytes": file_stat.st_size,
                "file_format": file_format,
                "file_modified_at": datetime.fromtimestamp(file_stat.st_mtime),
                "is_lossless": check_lossless(file_format),

                # Audio properties
                "duration_seconds": round(audio.info.length, 2) if audio.info else None,
                "sample_rate": audio.info.sample_rate if audio.info else None,
                "bit_depth": audio.info.bits_per_sample if audio.info else None,
                "channels": audio.info.channels if audio.info else None,
                "bitrate": int(audio.info.bitrate / 1000) if audio.info and audio.info.bitrate else None,

                # Metadata tags (with fallbacks)
                "title": audio.get("title", [None])[0],
                "artist": audio.get("artist", [None])[0],
                "album": audio.get("album", [None])[0],
                "album_artist": audio.get("albumartist", [None])[0] or audio.get("album artist", [None])[0],
                "genre": audio.get("genre", [None])[0],
                "date": audio.get("date", [None])[0],
                "track_number": audio.get("tracknumber", [None])[0],
                "disc_number": audio.get("discnumber", [None])[0] or "1",
                "label": audio.get("label", [None])[0] or audio.get("publisher", [None])[0],
                "catalog_number": audio.get("catalognumber", [None])[0],
                "isrc": audio.get("isrc", [None])[0],
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

    def find_flac_files(self, limit: Optional[int] = None, subpath: Optional[str] = None) -> List[Path]:
        """
        Recursively find all FLAC files in library.

        Args:
            limit: Maximum number of files to return (for testing).
            subpath: Optional subdirectory within library to scan.

        Returns:
            List of Path objects for FLAC files.
        """
        if subpath:
            scan_path = self.library_path / subpath
            if not scan_path.exists():
                raise ValueError(f"Subpath does not exist: {scan_path}")
            logger.info(f"Searching for FLAC files in {scan_path} (subpath: {subpath})")
        else:
            scan_path = self.library_path
            logger.info(f"Searching for FLAC files in {scan_path}")

        flac_files = []
        for file_path in scan_path.rglob("*.flac"):
            if file_path.is_file():
                flac_files.append(file_path)
                if limit and len(flac_files) >= limit:
                    break

        logger.info(f"Found {len(flac_files)} FLAC files")
        return flac_files

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

    def scan_and_import(self, limit: Optional[int] = None, skip_existing: bool = True, subpath: Optional[str] = None) -> Dict[str, int]:
        """
        Scan library and import metadata to database.

        Args:
            limit: Maximum number of files to scan (for testing).
            skip_existing: Skip files already in database.
            subpath: Optional subdirectory within library to scan.

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

        # Find FLAC files
        flac_files = self.find_flac_files(limit=limit, subpath=subpath)

        if not flac_files:
            logger.warning("No FLAC files found")
            return stats

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
            for file_path in tqdm(flac_files, desc="Scanning files", unit="file"):
                stats["processed"] += 1

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

                    # Create track-genre association
                    genre_name = metadata.get("genre")
                    if genre_name and genre_name.strip():
                        genre = self.get_or_create_genre(db, genre_name)
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

                    stats["added"] += 1
                    if track.id not in seen_track_ids:
                        seen_track_ids.add(track.id)
                        stats["unique_tracks"] += 1

                    # Commit every 100 files to avoid huge transactions
                    if stats["added"] % 100 == 0:
                        db.commit()
                        logger.info(f"Progress: {stats['added']} files added")

                except Exception as e:
                    logger.error(f"Error processing {file_path}: {e}")
                    stats["errors"] += 1
                    db.rollback()

            # Final commit
            db.commit()

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
