"""
Music library scanner for extracting metadata from FLAC files.
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from mutagen.flac import FLAC
from mutagen import MutagenError
from sqlalchemy.orm import Session
from tqdm import tqdm

from config import settings
from models import Artist, Album, Track, TrackArtist, TrackGenre, Genre, QualitySource
from database import get_db_context

logger = logging.getLogger(__name__)


class LibraryScanner:
    """Scanner for music library FLAC files."""

    def __init__(self, library_path: Optional[str] = None):
        """Initialize scanner with library path."""
        self.library_path = Path(library_path or settings.music_library_path)

        if not self.library_path.exists():
            raise ValueError(f"Library path does not exist: {self.library_path}")

        logger.info(f"Initialized scanner for: {self.library_path}")

    @staticmethod
    def detect_quality_source(file_path: Path) -> QualitySource:
        """
        Detect quality source from folder structure.

        Rules:
        - [Vinyl] folder → Vinyl
        - [TR24] folder → Hi-Res
        - [MP3] folder → MP3
        - Otherwise → CD
        """
        path_str = str(file_path)

        if "[Vinyl]" in path_str:
            return QualitySource.VINYL
        elif "[TR24]" in path_str:
            return QualitySource.HI_RES
        elif "[MP3]" in path_str:
            return QualitySource.MP3
        else:
            return QualitySource.CD

    @staticmethod
    def extract_metadata(file_path: Path) -> Optional[Dict[str, Any]]:
        """
        Extract metadata from FLAC file.

        Returns:
            Dictionary with extracted metadata or None if failed.
        """
        try:
            audio = FLAC(file_path)

            # Extract basic tags
            metadata = {
                # File information
                "file_path": str(file_path.absolute()),
                "file_size_bytes": file_path.stat().st_size,
                "file_format": "FLAC",

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

                # Quality detection
                "quality_source": LibraryScanner.detect_quality_source(file_path),
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

    def find_flac_files(self, limit: Optional[int] = None) -> List[Path]:
        """
        Recursively find all FLAC files in library.

        Args:
            limit: Maximum number of files to return (for testing).

        Returns:
            List of Path objects for FLAC files.
        """
        logger.info(f"Searching for FLAC files in {self.library_path}")

        flac_files = []
        for file_path in self.library_path.rglob("*.flac"):
            if file_path.is_file():
                flac_files.append(file_path)
                if limit and len(flac_files) >= limit:
                    break

        logger.info(f"Found {len(flac_files)} FLAC files")
        return flac_files

    @staticmethod
    def get_or_create_genre(db: Session, genre_name: str) -> Genre:
        """Get existing genre or create new one."""
        name = genre_name.strip()
        genre = db.query(Genre).filter(Genre.name == name).first()

        if not genre:
            genre = Genre(name=name)
            db.add(genre)
            db.flush()
            logger.debug(f"Created genre: {name}")

        return genre

    @staticmethod
    def get_or_create_artist(db: Session, artist_name: str) -> Artist:
        """Get existing artist or create new one."""
        artist = db.query(Artist).filter(Artist.name == artist_name).first()

        if not artist:
            artist = Artist(name=artist_name)
            db.add(artist)
            db.flush()
            logger.debug(f"Created artist: {artist_name}")

        return artist

    @staticmethod
    def get_or_create_album(
        db: Session,
        album_title: str,
        directory_path: str,
        metadata: Dict[str, Any]
    ) -> Album:
        """Get existing album or create new one (identified by directory_path)."""
        album = db.query(Album).filter(
            Album.directory_path == directory_path
        ).first()

        if not album:
            album = Album(
                title=album_title,
                directory_path=directory_path,
                release_year=metadata.get("release_year"),
                label=metadata.get("label"),
                catalog_number=metadata.get("catalog_number"),
                quality_source=metadata.get("quality_source", QualitySource.CD),
                sample_rate=metadata.get("sample_rate"),
                bit_depth=metadata.get("bit_depth"),
            )
            db.add(album)
            db.flush()
            logger.debug(f"Created album: {album_title}")

        return album

    def scan_and_import(self, limit: Optional[int] = None, skip_existing: bool = True) -> Dict[str, int]:
        """
        Scan library and import metadata to database.

        Args:
            limit: Maximum number of files to scan (for testing).
            skip_existing: Skip files already in database.

        Returns:
            Dictionary with statistics (processed, added, skipped, errors).
        """
        stats = {
            "processed": 0,
            "added": 0,
            "skipped": 0,
            "errors": 0,
        }

        # Find FLAC files
        flac_files = self.find_flac_files(limit=limit)

        if not flac_files:
            logger.warning("No FLAC files found")
            return stats

        with get_db_context() as db:
            # Get existing file paths for skip check
            if skip_existing:
                existing_paths = set(
                    path[0] for path in db.query(Track.file_path).all()
                )
                logger.info(f"Found {len(existing_paths)} existing tracks in database")
            else:
                existing_paths = set()

            # Process files with progress bar
            for file_path in tqdm(flac_files, desc="Scanning files", unit="file"):
                stats["processed"] += 1

                # Skip if already in database
                if skip_existing and str(file_path.absolute()) in existing_paths:
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
                        logger.warning(f"Missing album for {file_path}, skipping")
                        stats["errors"] += 1
                        continue

                    # Get or create artist
                    artist = self.get_or_create_artist(db, artist_name)

                    # Get or create album
                    album = self.get_or_create_album(
                        db,
                        album_title,
                        directory_path=str(file_path.parent),
                        metadata=metadata
                    )

                    # Create track
                    track = Track(
                        title=metadata["title"],
                        album_id=album.id,
                        track_number=metadata.get("track_number"),
                        disc_number=metadata.get("disc_number", 1),
                        duration_seconds=metadata.get("duration_seconds"),
                        sample_rate=metadata.get("sample_rate"),
                        bit_depth=metadata.get("bit_depth"),
                        bitrate=metadata.get("bitrate"),
                        channels=metadata.get("channels"),
                        file_path=metadata["file_path"],
                        file_size_bytes=metadata.get("file_size_bytes"),
                        file_format=metadata.get("file_format", "FLAC"),
                        isrc=metadata.get("isrc"),
                    )
                    db.add(track)
                    db.flush()

                    # Create track-artist association
                    track_artist = TrackArtist(
                        track_id=track.id,
                        artist_id=artist.id,
                        role="primary"
                    )
                    db.add(track_artist)

                    # Create track-genre association
                    genre_name = metadata.get("genre")
                    if genre_name and genre_name.strip():
                        genre = self.get_or_create_genre(db, genre_name)
                        track_genre = TrackGenre(
                            track_id=track.id,
                            genre_id=genre.id
                        )
                        db.add(track_genre)

                    stats["added"] += 1

                    # Commit every 100 tracks to avoid huge transactions
                    if stats["added"] % 100 == 0:
                        db.commit()
                        logger.info(f"Progress: {stats['added']} tracks added")

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


def scan_library(limit: Optional[int] = None, skip_existing: bool = True) -> Dict[str, int]:
    """
    Convenience function to scan library.

    Args:
        limit: Maximum number of files to scan.
        skip_existing: Skip files already in database.

    Returns:
        Statistics dictionary.
    """
    scanner = LibraryScanner()
    return scanner.scan_and_import(limit=limit, skip_existing=skip_existing)
