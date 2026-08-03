"""
Music library scanner for extracting metadata from audio files.

Creates canonical entities (Artist, Track, Album) with deterministic UUIDs
and physical entities (AlbumVariant, MediaFile) per file on disk.
"""

import logging
import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

import mutagen
from mutagen.flac import FLAC
from mutagen import MutagenError
from sqlalchemy import text
from sqlalchemy.orm import Session
from tqdm import tqdm

from config import settings
from models import (
    Artist, Album, Track, TrackArtist, AlbumArtist,
    AlbumVariant, MediaFile, Genre,
)
from database import get_db_context
from db_pool import db_query
from uuid_utils import artist_uuid, track_uuid, album_uuid, genre_uuid, is_lossless as check_lossless
from album_identity import assign_dir_albums
from canon.identity import elect_analysis_source

logger = logging.getLogger(__name__)

# Genre is album-grain: each imported media file adds +1 to its album's count
# for every distinct genre in its file tag. A file is imported at most once
# (skip_existing + file_path UNIQUE) and the whole file's writes share one
# savepoint, so this never double-counts on re-scan.
_ALBUM_GENRE_UPSERT = text("""
    INSERT INTO album_genres (album_id, genre_id, source, count)
    VALUES (:album_id, :genre_id, 'filetag', 1)
    ON CONFLICT (album_id, genre_id, source)
    DO UPDATE SET count = album_genres.count + 1
""")

# Supported audio extensions
AUDIO_EXTENSIONS = {'.flac', '.ape', '.wav', '.aiff', '.wv', '.tta', '.dsf', '.dff', '.mp3', '.ogg', '.m4a'}


def _classify_dirs(metadata_results):
    """Resolve how each directory's files map to albums (album_identity.
    assign_dir_albums).

    Returns ``{host_dir: {file_path: (album_title, album_artist, track_override,
    disc_override)}}`` for directories that need reshaping — box sets and singles
    collections (one album per release group) and mixes / loose per-track dumps
    (one folder album). A plain single-album directory is absent, so the import
    loop keeps each file's own tags. A ``None`` track/disc override means "keep
    the file's own value".

    Classification unions this batch with the directory's existing media_files,
    so an incremental scan that sees only part of a folder still classifies the
    whole folder; only this batch's files are returned (existing rows are already
    imported).
    """
    by_dir = defaultdict(list)
    for fp, md in metadata_results:
        by_dir[settings.translate_to_host_path(str(fp.parent))].append(md)

    existing = defaultdict(list)
    if by_dir:
        for r in db_query("""
            SELECT av.directory_path AS d, mf.raw_album, mf.disc_number, mf.track_number,
                   mf.raw_album_artist, mf.raw_artist, mf.file_path
            FROM album_variants av JOIN media_files mf ON mf.album_variant_id = av.id
            WHERE av.directory_path = ANY(%(d)s)
        """, {"d": list(by_dir)}):
            existing[r["d"]].append(r)

    dir_albums = {}
    for hd, mds in by_dir.items():
        batch_paths = {md.get("file_path") for md in mds}
        files = [{"album": md.get("album"), "disc": md.get("disc_number"),
                  "track": md.get("track_number"),
                  "artist_key": md.get("album_artist") or md.get("artist"),
                  "path": md.get("file_path")} for md in mds]
        files += [{"album": e["raw_album"], "disc": e["disc_number"],
                   "track": e["track_number"],
                   "artist_key": e["raw_album_artist"] or e["raw_artist"],
                   "path": e["file_path"]} for e in existing[hd]]
        folder = os.path.basename(hd.replace("\\", "/").rstrip("/"))
        assignment = assign_dir_albums(folder, files)
        if assignment is None:
            continue
        dir_albums[hd] = {p: a for p, a in assignment.items() if p in batch_paths}
    return dir_albums


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
                # Note: VorbisComment (FLAC/OGG) raises ValueError for non-ASCII
                # keys like ©nam, so we guard with try/except.
                if mp4_key and audio.tags:
                    try:
                        val = audio.tags.get(mp4_key)
                    except (ValueError, KeyError):
                        val = None
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

            # Parse track number (handle "1/12" format and vinyl "A1"/"B02")
            if metadata["track_number"]:
                track_num = str(metadata["track_number"]).split("/")[0]
                try:
                    metadata["track_number"] = int(track_num)
                except ValueError:
                    # Vinyl side notation: A=side1, B=side2, C=side3, D=side4, ...
                    # e.g. A1->track 1 disc 1, B02->track 2 disc 2, C03->track 3 disc 3
                    vinyl_match = re.match(r'^([A-Za-z])0*(\d+)$', track_num)
                    if vinyl_match:
                        side = vinyl_match.group(1).upper()
                        metadata["track_number"] = int(vinyl_match.group(2))
                        metadata["disc_number"] = ord(side) - ord('A') + 1
                    else:
                        metadata["track_number"] = None

            # Parse disc number (skip if already set by vinyl notation above)
            if metadata["disc_number"] and not isinstance(metadata["disc_number"], int):
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

    def find_audio_files(
        self,
        limit: Optional[int] = None,
        subpath: Optional[str] = None,
        cancel_check: Optional[callable] = None,
    ) -> List[Path]:
        """
        Recursively find all audio files in library.

        Args:
            limit: Maximum number of files to return (for testing).
            subpath: Optional subdirectory within library to scan.
            cancel_check: Optional callable returning True if the
                          enclosing scan was cancelled. Discovery can
                          take many seconds on a large library (rglob
                          stats every node); checking inside the walk
                          lets a Cancel tap take effect immediately
                          instead of waiting until extraction phase.

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
        for i, file_path in enumerate(scan_path.rglob("*")):
            if cancel_check and (i & 0xFF) == 0 and cancel_check():
                logger.info("Discovery cancelled by user")
                break
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
        """Get existing artist or create new one (deterministic name-UUID).

        Non-pure variants ("H. Mancini" → "Henry Mancini") may fragment on
        ingestion; the idempotent MB canon pass re-merges them (MB aliases +
        merge-on-MBID-collision), so no per-ingestion alias lookup is kept."""
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

        Two-phase pipeline:
          Phase 1 — extract metadata in parallel (ThreadPoolExecutor)
          Phase 2 — import to DB single-threaded with entity caching

        Disk reads stay roughly sequential (OS scheduler + small header
        reads), so this is safe for both SSD and HDD.

        Args:
            limit: Maximum number of files to scan (for testing).
            skip_existing: Skip files already in database.
            subpath: Optional subdirectory within library to scan.
            progress_cb: Callback(msg, stats) for progress reporting.
            cancel_check: Callback() -> bool, returns True if cancel requested.

        Returns:
            Dictionary with statistics (processed, added, skipped, errors).
        """
        from normalize_genres import parse_genre_string, normalize_genre_name

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

        def _cancelled() -> bool:
            return cancel_check and cancel_check()

        # ── Discover files ──────────────────────────────────────────
        if progress_cb:
            progress_cb("Discovering files...", stats)
        audio_files = self.find_audio_files(limit=limit, subpath=subpath, cancel_check=cancel_check)
        if _cancelled():
            logger.info("Scan cancelled during discovery")
            return stats

        if not audio_files:
            logger.warning("No audio files found")
            return stats

        total_files = len(audio_files)

        # ── Filter already-imported files ───────────────────────────
        existing_paths: set = set()
        if skip_existing:
            with get_db_context() as db:
                existing_paths = set(
                    row[0] for row in db.query(MediaFile.file_path).all()
                )
            logger.info(f"Found {len(existing_paths)} existing media files in database")

        files_to_process: List[Path] = []
        for fp in audio_files:
            if settings.translate_to_host_path(str(fp.absolute())) in existing_paths:
                stats["skipped"] += 1
            else:
                files_to_process.append(fp)

        stats["processed"] = stats["skipped"]

        if not files_to_process:
            stats["processed"] = total_files
            _report(f"All {total_files} files already in database")
            logger.info("No new files to process")
            return stats

        _report(f"Found {total_files} files, {len(files_to_process)} new")

        # ── Phase 1: extract metadata in parallel ───────────────────
        metadata_results: List[Tuple[Path, Dict[str, Any]]] = []
        num_workers = min(4, os.cpu_count() or 4)
        extract_done = 0
        cancelled_in_phase1 = False

        with ThreadPoolExecutor(max_workers=num_workers) as pool:
            future_to_path = {
                pool.submit(self.extract_metadata, fp): fp
                for fp in files_to_process
            }

            for future in tqdm(
                as_completed(future_to_path),
                total=len(future_to_path),
                desc="Extracting metadata",
                unit="file",
            ):
                if _cancelled():
                    cancelled_in_phase1 = True
                    for f in future_to_path:
                        f.cancel()
                    break

                fp = future_to_path[future]
                extract_done += 1
                stats["processed"] += 1

                try:
                    meta = future.result()
                    if meta:
                        metadata_results.append((fp, meta))
                    else:
                        stats["errors"] += 1
                except Exception as e:
                    logger.error(f"Error extracting metadata from {fp}: {e}")
                    stats["errors"] += 1

                if extract_done % 100 == 0:
                    _report(f"Extracting metadata: {extract_done}/{len(files_to_process)}")

        logger.info(f"Metadata extracted for {len(metadata_results)} files "
                     f"({stats['errors']} errors)")

        if not metadata_results:
            _report(f"Done: {stats['added']} added, {stats['skipped']} skipped, {stats['errors']} errors")
            return stats

        # ── Phase 2: import to database (single-threaded, cached) ───
        # If cancel was pressed during Phase 1, we still import whatever
        # metadata we managed to extract — cancel means "stop extracting",
        # not "discard collected data". A second cancel during Phase 2
        # will stop the import (keeping what was already committed).
        _report(f"Importing {len(metadata_results)} files to database...")

        # Entity caches — populated on demand, survive across files.
        # Key = deterministic UUID (or directory_path for variants).
        caches: Dict[str, dict] = {
            "artist": {},
            "track": {},
            "album": {},
            "genre": {},
            "variant": {},     # (dir_path, album_id) -> AlbumVariant
        }
        # Association caches — avoid repeated DB existence checks.
        assoc_ta: set = set()   # (track_id, artist_id, role)
        assoc_aa: set = set()   # (album_id, artist_id, role)

        # Folder→albums pre-pass: resolve box-set / singles / mix directories
        # once (album_identity.assign_dir_albums).
        dir_albums = _classify_dirs(metadata_results)

        with get_db_context() as db:
            for file_path, metadata in tqdm(
                metadata_results, desc="Importing", unit="file"
            ):
                if not cancelled_in_phase1 and _cancelled():
                    logger.info("Scan cancelled by user during import")
                    db.commit()
                    break

                try:
                    # Validate required fields
                    if not metadata.get("title"):
                        logger.warning(f"Missing title for {file_path}, skipping")
                        stats["errors"] += 1
                        continue

                    # Per-track artist owns the TRACK identity — compilation cuts
                    # belong to their real artists, not 'Various Artists'; the
                    # album_artist owns the ALBUM identity, so the compilation
                    # still groups as one album (Album has no artist_id — both
                    # credits coexist via track_artists/album_artists).
                    track_artist_name = metadata.get("artist") or metadata.get("album_artist")
                    album_artist_name = metadata.get("album_artist") or metadata.get("artist")
                    if not track_artist_name:
                        logger.warning(f"Missing artist for {file_path}, skipping")
                        stats["errors"] += 1
                        continue

                    album_title = metadata.get("album")
                    if not album_title:
                        album_title = metadata["title"]
                        logger.info(f"No album tag, using title as album: {album_title}")

                    # Box set / singles / mix folder → album identity comes from
                    # the directory pre-pass (per-group title + credit). The
                    # per-track artist (track_artist_name) is untouched, so track
                    # identity stands.
                    asg = dir_albums.get(
                        settings.translate_to_host_path(str(file_path.parent)), {}
                    ).get(metadata["file_path"])
                    if asg:
                        album_title, album_artist_name = asg[0], asg[1]

                    # Collect cache entries created inside the savepoint;
                    # only commit them to the long-lived caches after the
                    # savepoint succeeds (rollback safety).
                    pending_cache: List[Tuple[str, Any, Any]] = []

                    savepoint = db.begin_nested()
                    try:
                        # ── Artist (track credit) ──
                        a_uid = artist_uuid(track_artist_name)
                        if a_uid in caches["artist"]:
                            artist = caches["artist"][a_uid]
                        else:
                            artist = db.query(Artist).filter(Artist.id == a_uid).first()
                            if not artist:
                                artist = Artist(id=a_uid, name=track_artist_name)
                                db.add(artist)
                                db.flush()
                            pending_cache.append(("artist", a_uid, artist))

                        # ── Artist (album credit — e.g. 'Various Artists' on comps) ──
                        aa_uid = artist_uuid(album_artist_name)
                        if aa_uid == a_uid:
                            album_artist = artist
                        elif aa_uid in caches["artist"]:
                            album_artist = caches["artist"][aa_uid]
                        else:
                            album_artist = db.query(Artist).filter(Artist.id == aa_uid).first()
                            if not album_artist:
                                album_artist = Artist(id=aa_uid, name=album_artist_name)
                                db.add(album_artist)
                                db.flush()
                            pending_cache.append(("artist", aa_uid, album_artist))

                        # ── Track ──
                        t_uid = track_uuid(metadata["title"], track_artist_name)
                        if t_uid in caches["track"]:
                            track = caches["track"][t_uid]
                        else:
                            track = db.query(Track).filter(Track.id == t_uid).first()
                            if not track:
                                track = Track(id=t_uid, title=metadata["title"])
                                db.add(track)
                                db.flush()
                            pending_cache.append(("track", t_uid, track))

                        # ── Album ──
                        al_uid = album_uuid(album_title, album_artist_name)
                        if al_uid in caches["album"]:
                            album = caches["album"][al_uid]
                        else:
                            album = db.query(Album).filter(Album.id == al_uid).first()
                            if not album:
                                album = Album(
                                    id=al_uid,
                                    title=album_title,
                                    release_year=metadata.get("release_year"),
                                    label=metadata.get("label"),
                                    catalog_number=metadata.get("catalog_number"),
                                )
                                db.add(album)
                                db.flush()
                            pending_cache.append(("album", al_uid, album))

                        # ── Album variant (one physical edition per (dir, album)
                        # — a box set is several albums in one folder) ──
                        dir_path = settings.translate_to_host_path(str(file_path.parent))
                        vkey = (dir_path, str(album.id))
                        if vkey in caches["variant"]:
                            variant = caches["variant"][vkey]
                        else:
                            variant = db.query(AlbumVariant).filter(
                                AlbumVariant.directory_path == dir_path,
                                AlbumVariant.album_id == album.id,
                            ).first()
                            if not variant:
                                variant = AlbumVariant(
                                    album_id=album.id,
                                    directory_path=dir_path,
                                    sample_rate=metadata.get("sample_rate"),
                                    bit_depth=metadata.get("bit_depth"),
                                    is_lossless=metadata.get("is_lossless", True),
                                )
                                db.add(variant)
                                db.flush()
                            pending_cache.append(("variant", vkey, variant))

                        # ── Track-Artist association ──
                        ta_key = (track.id, artist.id, "primary")
                        if ta_key not in assoc_ta:
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
                            assoc_ta.add(ta_key)

                        # ── Album-Artist association (album credit) ──
                        aa_key = (album.id, album_artist.id, "primary")
                        if aa_key not in assoc_aa:
                            existing_aa = db.query(AlbumArtist).filter(
                                AlbumArtist.album_id == album.id,
                                AlbumArtist.artist_id == album_artist.id,
                                AlbumArtist.role == "primary",
                            ).first()
                            if not existing_aa:
                                db.add(AlbumArtist(
                                    album_id=album.id,
                                    artist_id=album_artist.id,
                                    role="primary",
                                ))
                            assoc_aa.add(aa_key)

                        # ── Album-Genre associations (album grain) ──
                        genre_name = metadata.get("genre")
                        if genre_name and genre_name.strip():
                            file_genre_ids: set = set()
                            for gn in parse_genre_string(genre_name):
                                gn = normalize_genre_name(gn)
                                g_uid = genre_uuid(gn)

                                if g_uid in caches["genre"]:
                                    genre = caches["genre"][g_uid]
                                else:
                                    genre = db.query(Genre).filter(Genre.id == g_uid).first()
                                    if not genre:
                                        genre = Genre(id=g_uid, name=gn)
                                        db.add(genre)
                                        db.flush()
                                    pending_cache.append(("genre", g_uid, genre))

                                # One +1 per genre per file (a tag may normalize
                                # two parts to the same genre).
                                if genre.id in file_genre_ids:
                                    continue
                                file_genre_ids.add(genre.id)
                                db.execute(_ALBUM_GENRE_UPSERT, {
                                    "album_id": album.id,
                                    "genre_id": genre.id,
                                })

                        # ── Media file ──
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
                            track_number=(asg[2] if asg and asg[2] is not None
                                          else metadata.get("track_number")),
                            disc_number=(asg[3] if asg and asg[3] is not None
                                         else metadata.get("disc_number", 1)),
                            isrc=metadata.get("isrc"),
                            # Original tags — ground truth for re-normalization / correction
                            raw_track_name=metadata.get("title"),
                            raw_artist=metadata.get("artist"),
                            raw_album_artist=metadata.get("album_artist"),
                            raw_album=metadata.get("album"),
                            raw_year=metadata.get("date"),
                        )
                        db.add(media_file)
                        db.flush()

                        elect_analysis_source(db, track.id)

                        savepoint.commit()
                    except Exception as e:
                        savepoint.rollback()
                        raise

                    # Promote pending entries to long-lived caches
                    for cache_name, key, obj in pending_cache:
                        caches[cache_name][key] = obj

                    stats["added"] += 1
                    if track.id not in seen_track_ids:
                        seen_track_ids.add(track.id)
                        stats["unique_tracks"] += 1

                    if stats["added"] % 100 == 0:
                        db.commit()
                        _report(f"Importing: {stats['added']}/{len(metadata_results)}")
                        logger.info(f"Progress: {stats['added']} files added")

                except Exception as e:
                    logger.error(f"Error processing {file_path}: {e}")
                    stats["errors"] += 1

            # Final commit
            db.commit()

        if _cancelled():
            _report(f"Cancelled: {stats['added']} added before cancel")
            logger.info(f"Scan cancelled: {stats['added']} added, "
                        f"{stats['skipped']} skipped, {stats['errors']} errors")
        else:
            stats["processed"] = total_files
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


def prune_missing_files(
    progress_cb: Optional[callable] = None,
    subpath: Optional[str] = None,
    cancel_check: Optional[callable] = None,
) -> Dict[str, int]:
    """Remove DB records for files that no longer exist on disk.

    Uses a single directory scan + set difference instead of per-file exists()
    calls, which is orders of magnitude faster on WSL2/network mounts.

    Deletion order: MediaFile → Track → AlbumVariant → Album → Artist.
    DB-level ON DELETE CASCADE handles child tables (embeddings, stats, etc).

    cancel_check is forwarded to find_audio_files so a Cancel tap takes
    effect during the slow disk-discovery phase.
    """
    from sqlalchemy import text

    stats = {"checked": 0, "pruned": 0, "orphan_tracks": 0,
             "orphan_variants": 0, "orphan_albums": 0, "orphan_artists": 0}

    if progress_cb:
        progress_cb("Discovering files on disk...")

    scanner = LibraryScanner()
    disk_files = scanner.find_audio_files(subpath=subpath, cancel_check=cancel_check)
    if cancel_check and cancel_check():
        logger.info("Prune cancelled during discovery")
        return stats
    disk_paths = {settings.translate_to_host_path(str(fp.absolute())) for fp in disk_files}

    with get_db_context() as db:
        query = db.query(MediaFile.id, MediaFile.file_path)
        if subpath:
            query = query.filter(MediaFile.file_path.like(f"%{subpath}%"))
        all_files = query.all()
        stats["checked"] = len(all_files)

        if progress_cb:
            progress_cb(f"Comparing {len(all_files)} DB records against {len(disk_paths)} files on disk...")

        missing_ids = []
        for mf_id, file_path in all_files:
            if file_path not in disk_paths:
                missing_ids.append(mf_id)
                logger.info(f"Missing: {file_path}")

        if not missing_ids:
            logger.info("Prune: no missing files found")
            return stats

        stats["pruned"] = len(missing_ids)
        logger.info(f"Pruning {len(missing_ids)} missing media files")

        if progress_cb:
            progress_cb(f"Removing {len(missing_ids)} missing files...")

        db.query(MediaFile).filter(MediaFile.id.in_(missing_ids)).delete(synchronize_session=False)

        # Analysis-carrying tracks are spared: embeddings + analysis_sources
        # cascade with the track, and neither the node's own streamed
        # enrichment nor rows a CGNAT peer push-seeded here (carry) can be
        # re-derived without the audio. The file is gone; the analysis of it
        # is still real.
        r = db.execute(text("""
            DELETE FROM tracks WHERE id IN (
                SELECT t.id FROM tracks t
                LEFT JOIN media_files mf ON mf.track_id = t.id
                WHERE mf.id IS NULL
                  AND NOT EXISTS (SELECT 1 FROM embeddings e WHERE e.track_id = t.id)
            )
        """))
        stats["orphan_tracks"] = r.rowcount

        r = db.execute(text("""
            DELETE FROM album_variants WHERE id IN (
                SELECT av.id FROM album_variants av
                LEFT JOIN media_files mf ON mf.album_variant_id = av.id
                WHERE mf.id IS NULL
            )
        """))
        stats["orphan_variants"] = r.rowcount

        r = db.execute(text("""
            DELETE FROM albums WHERE id IN (
                SELECT a.id FROM albums a
                LEFT JOIN album_variants av ON av.album_id = a.id
                WHERE av.id IS NULL
            )
        """))
        stats["orphan_albums"] = r.rowcount

        r = db.execute(text("""
            DELETE FROM artists WHERE id IN (
                SELECT ar.id FROM artists ar
                LEFT JOIN track_artists ta ON ta.artist_id = ar.id
                LEFT JOIN album_artists aa ON aa.artist_id = ar.id
                WHERE ta.track_id IS NULL AND aa.album_id IS NULL
            )
        """))
        stats["orphan_artists"] = r.rowcount

        db.commit()

    logger.info(f"Prune complete: {stats}")
    return stats
