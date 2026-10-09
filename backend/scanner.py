"""
Music library scanner for extracting metadata from audio files.

Creates canonical entities (Artist, Track, Album) with deterministic UUIDs
and physical entities (AlbumVariant, MediaFile) per file on disk.
"""

import logging
import os
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Sequence, Set, Tuple

import mutagen
from mutagen.flac import FLAC
from mutagen import MutagenError
from sqlalchemy import text
from sqlalchemy.orm import Session
from tqdm import tqdm

from config import settings
from models import (
    Artist, Album, Track, TrackArtist, AlbumArtist,
    AlbumVariant, MediaFile, HqpLibraryFile, Genre,
)
from database import get_db_context
from db_pool import db_execute, db_query, db_query_one
from uuid_utils import artist_uuid, track_uuid, album_uuid, genre_uuid, is_lossless as check_lossless
from album_identity import assign_dir_albums
from canon.identity import ORPHAN_TRACK_SQL, elect_analysis_source
import cue_sheet

logger = logging.getLogger(__name__)

# Genre is album-grain: each imported media file adds +1 to its album's count
# for every distinct genre in its file tag. A file is imported at most once
# (skip_existing + the (file_path, cue_start_seconds) UNIQUE) and the whole
# file's writes share one savepoint, so this never double-counts on re-scan.
_ALBUM_GENRE_UPSERT = text("""
    INSERT INTO album_genres (album_id, genre_id, source, count)
    VALUES (:album_id, :genre_id, 'filetag', 1)
    ON CONFLICT (album_id, genre_id, source)
    DO UPDATE SET count = album_genres.count + 1
""")

# Supported audio extensions
AUDIO_EXTENSIONS = {'.flac', '.ape', '.wav', '.aiff', '.wv', '.tta', '.dsf', '.dff', '.mp3', '.ogg', '.m4a'}

# A Docker bind is fixed when its container starts: a drive reconnected under
# a running node stays invisible to it until the restart.
UNREACHABLE = ("The music folder is empty or not mounted, so nothing was scanned. "
               "Reconnect it and scan again — a Docker node sees it again only "
               "after a restart.")


def library_unreachable(wake: bool = True) -> bool:
    """The library path is set but holds nothing while the catalog knows
    owned files: a drvfs mount that dropped under a running node, a forgotten
    drive, a Docker bind that came up on an empty directory. An unreadable
    path counts as empty; no folder chosen yet is not this. The one rule
    behind the library.mount_missing notice, Library's Music path row, the
    scan's refusal and the prune's last check — a walk over such a folder
    finds nothing and reads as a scan, or as every file deleted.

    A look, taken where it is called (a scan's refusal, the prune): its
    answer is recorded unless a look that started later recorded first, and
    `wake` wakes the notices when it differs from what they show."""
    return _look(_next_ticket(), wake)


def _look(ticket: int, wake: bool) -> bool:
    root = settings.music_library_path
    if not root or not db_query_one("SELECT 1 AS x FROM media_files LIMIT 1"):
        answer = False
    else:
        try:
            with os.scandir(root) as it:
                answer = next(it, None) is None
        except OSError:
            answer = True
    _record(answer, ticket, wake)
    return answer


# The folder's state, two ways: the last answer recorded (looks and
# observations, ordered by when they started) and what the notices were last
# given. The notices derive on every wake of every stream, so they share one
# look, run by a worker thread, and wait for it a few seconds at most — a look
# that takes longer reads as a dead mount. Their look never wakes the channel:
# on a share that flaps it would wake itself without end, so a change only a
# request's derivation saw reaches the tabs already open with the next wake.
# Producers do wake it, when their answer differs from what the notices show:
# a scan's refusal, the prune, Library opened, a library file served or found
# missing. And a look the readers gave up on wakes them if the folder answered
# after all: a disk spinning up is not a dead mount.
LOOK_PATIENCE_S = 3.0
_folder = threading.Condition()
_folder_unreachable = False
_shown = False
_ticket = 0
_recorded = 0                 # the ticket the recorded answer is from
_look_wanted = False          # the worker's queue of one,
_look_wakes_wanted = False    # asked for by a producer
_looking = False              # the worker's look under way:
_look_ticket = 0              #   its ticket,
_look_started = 0.0           #   its start (monotonic),
_look_wakes = False           #   a producer's,
_look_given_up = False        #   a reader read "unreachable" rather than wait longer
_looks_done = 0
_worker: Optional[threading.Thread] = None


def _next_ticket() -> int:
    global _ticket
    with _folder:
        _ticket += 1
        return _ticket


def _record(unreachable: bool, ticket: int, wake: bool) -> None:
    global _folder_unreachable, _recorded, _shown
    with _folder:
        if ticket < _recorded:
            return
        _recorded = ticket
        _folder_unreachable = unreachable
        wake = wake and unreachable != _shown
        if wake:
            _shown = unreachable
    if wake:
        db_execute("NOTIFY sautium_notices")


def folder_seen(unreachable: bool) -> None:
    """A producer's observation that needs no look (a library file served:
    the folder answers), recorded like a look that started now."""
    _record(unreachable, _next_ticket(), wake=True)


def check_folder() -> None:
    """A producer's look that nobody waits for (Library opened, a library
    file found missing): the worker looks and wakes the notices if the
    answer differs from what they show."""
    global _look_wanted, _look_wakes_wanted
    _ensure_worker()
    with _folder:
        _look_wanted = _look_wakes_wanted = True
        _folder.notify_all()


def folder_unreachable_now() -> bool:
    """The folder's state for the notices: they share the worker's look and
    wait for it no longer than LOOK_PATIENCE_S from its start. A look that
    takes longer reads as a dead mount, unless a word newer than it says the
    folder answers (a library file served)."""
    global _look_wanted, _look_given_up, _shown
    _ensure_worker()
    with _folder:
        if not _looking and not _look_wanted:
            _look_wanted = True
            _folder.notify_all()
        done = _looks_done
        started = _look_started if _looking else time.monotonic()
        remaining = started + LOOK_PATIENCE_S - time.monotonic()
        if remaining > 0 and _folder.wait_for(lambda: _looks_done != done, timeout=remaining):
            answer = _folder_unreachable
        elif _recorded > _look_ticket:
            answer = _folder_unreachable
        else:
            _look_given_up = True
            answer = True
        _shown = answer
    return answer


def _ensure_worker() -> None:
    global _worker
    with _folder:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_look_worker, daemon=True,
                                       name="music-folder-look")
            _worker.start()


def _look_worker() -> None:
    global _look_wanted, _look_wakes_wanted, _looking, _look_ticket, _look_started
    global _look_wakes, _look_given_up, _looks_done, _shown, _ticket
    while True:
        with _folder:
            _folder.wait_for(lambda: _look_wanted)
            _look_wanted = False
            _look_wakes, _look_wakes_wanted = _look_wakes_wanted, False
            _look_given_up = False
            _ticket += 1
            _looking, _look_ticket, _look_started = True, _ticket, time.monotonic()
        try:
            answer = _look(_look_ticket, wake=False)
        except Exception as e:
            logger.error(f"Music folder look failed: {e}", exc_info=True)
            answer = None
        with _folder:
            wake = (answer is not None and _recorded == _look_ticket
                    and answer != _shown
                    and (_look_wakes or (_look_given_up and not answer)))
            if wake:
                _shown = answer
            _looking = False
            _looks_done += 1
            _folder.notify_all()
        if wake:
            db_execute("NOTIFY sautium_notices")


class LibraryUnreachable(RuntimeError):
    """A scan refused: the folder library_unreachable() names. An expected
    condition, not a failure of the scan."""


def refuse_if_unreachable() -> None:
    """Every scan's refusal, whoever starts it (the Library button, the
    launcher, cli.py, a walk that came back empty). Its look at the folder
    wakes the notices when the answer changed, so every open tab learns."""
    if library_unreachable():
        raise LibraryUnreachable(UNREACHABLE)


@dataclass(frozen=True)
class FileSink:
    """Where an import run's file rows land. Local files become media_files
    under variants located here; an HQPlayer's library (hqp_library.sync)
    becomes hqp_library_files under variants located at that endpoint. The
    canonical entities are the same either way, so a copy at the HQPlayer
    lands on the tracks a local scan minted — one album, one more variant."""
    location: str = "local"
    hqp_endpoint_id: Optional[int] = None

    @property
    def files_table(self) -> str:
        return "media_files" if self.location == "local" else "hqp_library_files"


LOCAL_FILES = FileSink()


def _classify_dirs(entries, sink: FileSink):
    """Resolve how each directory's files map to albums (album_identity.
    assign_dir_albums). ``entries`` = ``(dir_path, metadata)`` pairs, the
    directory in DB form (the variant key).

    Returns ``{dir_path: {file_path: (album_title, album_artist, track_override,
    disc_override)}}`` for directories that need reshaping — box sets and singles
    collections (one album per release group) and mixes / loose per-track dumps
    (one folder album). A plain single-album directory is absent, so the import
    loop keeps each file's own tags. A ``None`` track/disc override means "keep
    the file's own value".

    Classification unions this batch with the directory's existing rows in the
    sink's file table, so an incremental scan that sees only part of a folder
    still classifies the whole folder; only this batch's files are returned
    (existing rows are already imported).
    """
    by_dir = defaultdict(list)
    for dir_path, md in entries:
        # Cue slices are excluded: the sheet is the album authority for its
        # folder, and the renumber-fold below is keyed by file path — N
        # virtual entries sharing one image path would collapse onto one
        # track number.
        if md.get("cue_start_seconds") is not None:
            continue
        by_dir[dir_path].append(md)
    existing = defaultdict(list)
    if by_dir:
        path_col = "file_path" if sink.location == "local" else "hqp_path"
        for r in db_query(f"""
            SELECT av.directory_path AS d, f.raw_album, f.disc_number, f.track_number,
                   f.raw_album_artist, f.raw_artist, f.{path_col} AS file_path
            FROM album_variants av JOIN {sink.files_table} f ON f.album_variant_id = av.id
            WHERE av.directory_path = ANY(%(d)s)
              AND av.hqp_endpoint_id IS NOT DISTINCT FROM %(e)s
        """, {"d": list(by_dir), "e": sink.hqp_endpoint_id}):
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


def import_metadata(entries: List[Tuple[str, Dict[str, Any]]], *, sink: FileSink,
                    stats: Dict[str, int], progress_cb: Optional[callable] = None,
                    cancel_check: Optional[callable] = None) -> None:
    """Phase 2 of a scan: canonical entities and file rows from extracted
    metadata. ``entries`` = ``(dir_path, metadata)`` — the directory in DB
    form (the variant key) and the metadata as extract_metadata returns it
    (an HQPlayer library entry is shaped the same way by hqp_library).
    Single-threaded with entity caches, one savepoint per file, a commit
    every 100 files; counts into ``stats`` (added, errors, unique_tracks).
    cancel_check stops after the current file, keeping what was committed.
    """
    from normalize_genres import parse_genre_string, normalize_genre_name

    for key in ("added", "errors", "unique_tracks"):
        stats.setdefault(key, 0)

    def _report(msg: str):
        if progress_cb:
            progress_cb(msg, stats)

    # Entity caches — populated on demand, survive across files.
    # Key = deterministic UUID (or (dir_path, album_id) for variants).
    caches: Dict[str, dict] = {
        "artist": {},
        "track": {},
        "album": {},
        "genre": {},
        "variant": {},
    }
    # Association caches — avoid repeated DB existence checks.
    assoc_ta: set = set()   # (track_id, artist_id, role)
    assoc_aa: set = set()   # (album_id, artist_id, role)
    seen_track_ids = set()
    local_albums: Dict[Any, bool] = {}   # album_id -> has a local variant (HQP sink only)
    # Folder→albums pre-pass: resolve box-set / singles / mix directories
    # once (album_identity.assign_dir_albums).
    dir_albums = _classify_dirs(entries, sink)

    with get_db_context() as db:
        for dir_path, metadata in tqdm(entries, desc="Importing", unit="file"):
            if cancel_check and cancel_check():
                logger.info("Import cancelled by user")
                db.commit()
                break
            file_path = metadata.get("file_path")
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
                # identity stands. Cue slices bypass it — the sheet is the
                # album authority (they are excluded from _classify_dirs too).
                asg = None
                if metadata.get("cue_start_seconds") is None:
                    asg = dir_albums.get(dir_path, {}).get(file_path)
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
                    # at the sink's location — a box set is several albums in
                    # one folder) ──
                    vkey = (dir_path, str(album.id))
                    if vkey in caches["variant"]:
                        variant = caches["variant"][vkey]
                    else:
                        variant = db.query(AlbumVariant).filter(
                            AlbumVariant.directory_path == dir_path,
                            AlbumVariant.album_id == album.id,
                            AlbumVariant.hqp_endpoint_id == sink.hqp_endpoint_id,
                        ).first()
                        if not variant:
                            variant = AlbumVariant(
                                album_id=album.id,
                                directory_path=dir_path,
                                raw_title=album_title,
                                sample_rate=metadata.get("sample_rate"),
                                bit_depth=metadata.get("bit_depth"),
                                is_lossless=metadata.get("is_lossless", True),
                                location=sink.location,
                                hqp_endpoint_id=sink.hqp_endpoint_id,
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
                    # A copy at the HQPlayer of an album whose files are here
                    # adds no genre information: its tags are the same tags,
                    # counted already. Only an HQP-only album's files count.
                    genre_name = metadata.get("genre")
                    if genre_name and genre_name.strip() and sink.location != "local":
                        if album.id not in local_albums:
                            local_albums[album.id] = bool(db.execute(text(
                                "SELECT 1 FROM album_variants WHERE album_id = :a AND location = 'local' LIMIT 1"
                            ), {"a": str(album.id)}).fetchone())
                        if local_albums[album.id]:
                            genre_name = None
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
                    # ── File row ──
                    track_number = (asg[2] if asg and asg[2] is not None
                                    else metadata.get("track_number"))
                    disc_number = (asg[3] if asg and asg[3] is not None
                                   else metadata.get("disc_number", 1))
                    if sink.location == "local":
                        db.add(MediaFile(
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
                            cue_start_seconds=metadata.get("cue_start_seconds"),
                            cue_end_seconds=metadata.get("cue_end_seconds"),
                            track_number=track_number,
                            disc_number=disc_number,
                            isrc=metadata.get("isrc"),
                            # Original tags — ground truth for re-normalization / correction
                            raw_track_name=metadata.get("title"),
                            raw_artist=metadata.get("artist"),
                            raw_album_artist=metadata.get("album_artist"),
                            raw_album=metadata.get("album"),
                            raw_year=metadata.get("date"),
                        ))
                        db.flush()
                        elect_analysis_source(db, track.id)
                    else:
                        # No bytes here: nothing to elect as an analysis source.
                        db.add(HqpLibraryFile(
                            track_id=track.id,
                            album_variant_id=variant.id,
                            hqp_path=metadata["file_path"],
                            hqp_file_hash=metadata.get("hqp_file_hash"),
                            hqp_dir_hash=metadata.get("hqp_dir_hash"),
                            file_format=metadata.get("file_format", "FLAC"),
                            is_lossless=metadata.get("is_lossless", True),
                            sample_rate=metadata.get("sample_rate"),
                            bit_depth=metadata.get("bit_depth"),
                            bitrate=metadata.get("bitrate"),
                            channels=metadata.get("channels"),
                            duration_seconds=metadata.get("duration_seconds"),
                            track_number=track_number,
                            disc_number=disc_number,
                            raw_track_name=metadata.get("title"),
                            raw_artist=metadata.get("artist"),
                            raw_album_artist=metadata.get("album_artist"),
                            raw_album=metadata.get("album"),
                            raw_year=metadata.get("date"),
                        ))
                        db.flush()
                    savepoint.commit()
                except Exception:
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
                    _report(f"Importing: {stats['added']}/{len(entries)}")
                    logger.info(f"Progress: {stats['added']} files added")
            except Exception as e:
                logger.error(f"Error processing {file_path}: {e}")
                stats["errors"] += 1
        db.commit()


class LibraryScanner:
    """Scanner for music library audio files."""

    def __init__(self, library_path: Optional[str] = None):
        """Initialize scanner with library path."""
        self.library_path = Path(library_path or settings.music_library_path)

        if not self.library_path.exists():
            raise ValueError(f"Library path does not exist: {self.library_path}")

        logger.info(f"Initialized scanner for: {self.library_path}")
        # Host paths of the last FULL discovery and the folders it could not
        # read, for the prune that closes a scan run (see scan_and_import).
        self.last_disk_paths: Optional[Set[str]] = None
        self.last_unread: List[str] = []

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
                "file_modified_at": datetime.fromtimestamp(file_stat.st_mtime, tz=timezone.utc),
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
    ) -> Tuple[List[Path], List[Path], List[str]]:
        """
        Recursively find all audio files (and cue sheets) in library.

        Args:
            limit: Maximum number of audio files to return (for testing).
            subpath: Optional subdirectory within library to scan.
            cancel_check: Optional callable returning True if the
                          enclosing scan was cancelled. Discovery can
                          take minutes on a large library over drvfs;
                          checking per folder lets a Cancel tap take
                          effect immediately instead of waiting until
                          extraction phase.

        Returns:
            (audio file Paths, .cue file Paths, folders that could not be
            read) — one walk collects all three. A folder it could not list
            whole (no permission, an entry whose type it could not read, a
            nested mount gone, removed mid-walk) is reported, never skipped
            in silence: nothing beneath it is in the lists, and a prune must
            not read it as deleted.
        """
        if subpath:
            scan_path = self.library_path / subpath
            if not scan_path.exists():
                raise ValueError(f"Subpath does not exist: {scan_path}")
            logger.info(f"Searching for audio files in {scan_path} (subpath: {subpath})")
        else:
            scan_path = self.library_path
            logger.info(f"Searching for audio files in {scan_path}")

        audio_files: List[Path] = []
        cue_files: List[Path] = []
        unread: List[str] = []

        # A scandir walk of its own: Path.rglob drops a folder it may not list
        # without a word, and os.walk files a folder whose type it could not
        # read (a stale nested mount) as a plain name. Entry types come from
        # the directory listing, so a regular file costs no stat; a symlink is
        # followed to its file (rglob's is_file did the same) but never into
        # a folder. Absolute from the root, as every path the prune compares.
        folders = [str(scan_path.absolute())]
        while folders:
            if cancel_check and cancel_check():
                logger.info("Discovery cancelled by user")
                break
            folder = folders.pop()
            here_audio: List[Path] = []
            here_cues: List[Path] = []
            subfolders: List[str] = []
            try:
                with os.scandir(folder) as it:
                    for entry in it:
                        if entry.is_dir(follow_symlinks=False):
                            subfolders.append(entry.path)
                        elif entry.is_file():    # not a FIFO, a socket, a dangling link
                            suffix = os.path.splitext(entry.name)[1].lower()
                            if suffix in AUDIO_EXTENSIONS:
                                here_audio.append(Path(entry.path))
                            elif suffix == ".cue":
                                here_cues.append(Path(entry.path))
            except OSError as err:
                # A folder known in part is left out whole, with all beneath
                # it: the cue it could not see would turn its image into one
                # track and supersede the slices, and the prune keeps what
                # lies under it.
                logger.warning(f"Could not read {folder}: {err}")
                unread.append(folder)
                continue
            audio_files.extend(here_audio)
            cue_files.extend(here_cues)
            folders.extend(subfolders)
            if limit and len(audio_files) >= limit:
                del audio_files[limit:]
                break

        logger.info(f"Found {len(audio_files)} audio files, {len(cue_files)} cue sheets"
                    + (f", {len(unread)} folders unread" if unread else ""))
        return audio_files, cue_files, unread

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
            "superseded": 0,
            "unread": 0,
        }
        def _report(msg: str = None):
            if progress_cb:
                progress_cb(msg or f"Scanned {stats['processed']}/{total_files}", stats)

        def _cancelled() -> bool:
            return cancel_check and cancel_check()

        refuse_if_unreachable()

        # ── Discover files ──────────────────────────────────────────
        if progress_cb:
            progress_cb("Discovering files...", stats)
        audio_files, cue_files, unread = self.find_audio_files(
            limit=limit, subpath=subpath, cancel_check=cancel_check)
        stats["unread"] = len(unread)
        self.last_unread = unread
        if _cancelled():
            logger.info("Scan cancelled during discovery")
            return stats

        if not audio_files:
            logger.warning("No audio files found")
            return stats

        # The prune that closes a scan run reconciles this very tree, and the
        # walk costs ~6 min on the reference library over drvfs. Hand it this
        # one, with the paths it could not read — the prune keeps what lies
        # under them. A limited scan truncates the list, so it never becomes a prune
        # input — everything outside the limit would read as deleted.
        self.last_disk_paths = None if limit else {
            settings.translate_to_host_path(str(fp.absolute())) for fp in audio_files
        }

        total_files = len(audio_files)

        # str(walk path) -> CueSheet for every image a usable cue governs.
        cue_map = cue_sheet.build_cue_map(cue_files, AUDIO_EXTENSIONS)

        # ── Filter already-imported files ───────────────────────────
        # A path's DB state is its SET of cue_start_seconds values ({None} for
        # a plain whole-file row). A file is up to date only when that set
        # matches what this scan would write — so a new/edited/deleted cue
        # re-processes its image, and untouched files skip as before. Loaded
        # regardless of skip_existing: cue reconciliation needs it too.
        db_starts: Dict[str, set] = {}
        with get_db_context() as db:
            for path, start in db.query(
                    MediaFile.file_path, MediaFile.cue_start_seconds).all():
                db_starts.setdefault(path, set()).add(start)
        logger.info(f"Found {len(db_starts)} existing media file paths in database")

        files_to_process: List[Path] = []
        for fp in audio_files:
            host_path = settings.translate_to_host_path(str(fp.absolute()))
            sheet = cue_map.get(str(fp))
            want = ({t.start_seconds for t in sheet.tracks} if sheet is not None
                    else {None})
            if skip_existing and db_starts.get(host_path) == want:
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

        # ── Cue expansion + reconciliation plan ─────────────────────
        # A cue-governed image becomes N virtual entries sharing its path;
        # recon_plans records, per path, the start-set this scan is about to
        # write so every OTHER row of that path (a legacy whole-image row, a
        # stale boundary set, slices of a deleted cue) is removed first.
        # Plans are built only from files whose metadata extraction succeeded —
        # an unreadable image must not destroy its existing DB state.
        recon_plans: Dict[str, set] = {}
        expanded: List[Tuple[Path, Dict[str, Any]]] = []
        for fp, md in metadata_results:
            sheet = cue_map.get(str(fp))
            host_path = md["file_path"]
            if sheet is None:
                expanded.append((fp, md))
                if any(s is not None for s in db_starts.get(host_path, ())):
                    recon_plans[host_path] = {None}
                continue
            starts = {t.start_seconds for t in sheet.tracks}
            for tr in sheet.tracks:
                expanded.append((fp, cue_sheet.synthesize_metadata(
                    md, sheet, tr, md.get("duration_seconds"))))
            if db_starts.get(host_path, set()) - starts:
                recon_plans[host_path] = starts
        metadata_results = expanded

        # ── Phase 2: import to database (single-threaded, cached) ───
        # If cancel was pressed during Phase 1, we still import whatever
        # metadata we managed to extract — cancel means "stop extracting",
        # not "discard collected data". A second cancel during Phase 2
        # will stop the import (keeping what was already committed).
        _report(f"Importing {len(metadata_results)} files to database...")
        # The variant key is the file's directory in DB form.
        entries = [(settings.translate_to_host_path(str(fp.parent)), md)
                   for fp, md in metadata_results]
        # ── Cue reconciliation pre-pass ─────────────────────────────
        # Deletes committed before the import starts: if the scan dies in
        # between, the next scan's start-set inequality re-processes the
        # image, so the two passes stay idempotent as a pair. -1 encodes the
        # NULL start (chk_mf_cue_span keeps real starts >= 0).
        superseded_tracks: set = set()
        superseded_variants: set = set()
        if recon_plans:
            with get_db_context() as db:
                for host_path, keep in recon_plans.items():
                    keep_arr = [-1.0 if s is None else s for s in keep]
                    rows = db.execute(text("""
                        DELETE FROM media_files
                         WHERE file_path = :path
                           AND COALESCE(cue_start_seconds, -1.0) <> ALL(:keep)
                        RETURNING track_id, album_variant_id
                    """), {"path": host_path, "keep": keep_arr}).fetchall()
                    for track_id, variant_id in rows:
                        superseded_tracks.add(str(track_id))
                        superseded_variants.add(variant_id)
                        stats["superseded"] += 1
                db.commit()
            if stats["superseded"]:
                logger.info(f"Cue reconciliation: removed {stats['superseded']} "
                            f"superseded media_files rows across {len(recon_plans)} paths")

        import_metadata(entries, sink=LOCAL_FILES, stats=stats, progress_cb=progress_cb,
                        cancel_check=None if cancelled_in_phase1 else cancel_check)
        # ── Cue supersede post-pass ─────────────────────────────
        # Runs after the re-import so a track whose slices merely moved
        # keeps its row (and listening history). A track left with no
        # files loses its analysis: the cue replaces the material, a
        # whole-image embedding must not survive as a spare (contrast
        # prune_missing_files, which spares vanished-but-real rips). The
        # row itself then goes only as an orphan (ORPHAN_TRACK_SQL) — a
        # listen of the whole image is still the owner's listen.
        # Orphan cleanup is SCOPED to the affected ids — the global
        # LEFT JOIN sweeps prune uses would take phantom albums with them.
        if superseded_tracks:
            with get_db_context() as db:
                tids = list(superseded_tracks)
                for table in ("embeddings", "audio_features", "analysis_sources"):
                    db.execute(text(f"""
                        DELETE FROM {table} x WHERE x.track_id = ANY(CAST(:tids AS uuid[]))
                          AND NOT EXISTS (SELECT 1 FROM media_files mf
                                          WHERE mf.track_id = x.track_id)
                    """), {"tids": tids})
                r = db.execute(text(f"""
                    DELETE FROM tracks t WHERE t.id = ANY(CAST(:tids AS uuid[]))
                      AND {ORPHAN_TRACK_SQL.format(t='t')}
                """), {"tids": tids})
                logger.info(f"Cue supersede: removed {r.rowcount} whole-image tracks")

                for tid in db.execute(text("""
                    SELECT id FROM tracks WHERE id = ANY(CAST(:tids AS uuid[]))
                """), {"tids": tids}).scalars():
                    elect_analysis_source(db, tid)

                vids = list(superseded_variants)
                affected_albums = list(db.execute(text("""
                    SELECT DISTINCT album_id FROM album_variants
                    WHERE id = ANY(:vids)
                """), {"vids": vids}).scalars())
                db.execute(text("""
                    DELETE FROM album_variants WHERE id = ANY(:vids)
                      AND NOT EXISTS (SELECT 1 FROM media_files mf
                                      WHERE mf.album_variant_id = album_variants.id)
                """), {"vids": vids})
                if affected_albums:
                    db.execute(text("""
                        DELETE FROM albums WHERE id = ANY(CAST(:aids AS uuid[]))
                          AND NOT EXISTS (SELECT 1 FROM album_variants av
                                          WHERE av.album_id = albums.id)
                    """), {"aids": [str(a) for a in affected_albums]})

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
    disk_paths: Optional[Set[str]] = None,
    unread: Sequence[str] = (),
) -> Dict[str, int]:
    """Remove DB records for files that no longer exist on disk.

    Uses a single directory scan + set difference instead of per-file exists()
    calls, which is orders of magnitude faster on WSL2/network mounts.

    Deletion order: MediaFile → Track → AlbumVariant → Album → Artist, each
    step scoped to the rows the removed files justified — the phantom layer
    is not this function's business. DB-level ON DELETE CASCADE handles child
    tables (embeddings, stats, etc).

    disk_paths lets a caller that has just walked the tree hand its result in
    (see LibraryScanner.scan_and_import) instead of paying for a second walk,
    with `unread`, the paths that walk could not read: whatever lies under
    them keeps its rows, whoever walked the tree.

    cancel_check is forwarded to find_audio_files so a Cancel tap takes
    effect during the slow disk-discovery phase.
    """
    from sqlalchemy import text

    stats = {"checked": 0, "pruned": 0, "kept_unread": 0, "orphan_tracks": 0,
             "orphan_variants": 0, "orphan_albums": 0, "orphan_artists": 0}

    if disk_paths is None:
        if progress_cb:
            progress_cb("Discovering files on disk...")

        scanner = LibraryScanner()
        disk_files, _cues, unread = scanner.find_audio_files(subpath=subpath, cancel_check=cancel_check)
        if cancel_check and cancel_check():
            logger.info("Prune cancelled during discovery")
            return stats
        disk_paths = {settings.translate_to_host_path(str(fp.absolute())) for fp in disk_files}

    # What the walk could not read keeps its rows: a folder it could not list
    # is missing its files here, and they would read as deleted. The rest of
    # the tree is pruned as usual — a folder nobody may read (a volume's
    # System Volume Information, lost+found) must not stop the prune for good.
    unverified = {settings.translate_to_host_path(p) for p in unread}
    if unverified:
        logger.warning(f"Prune keeps what {len(unverified)} unread folder(s) hold, "
                       f"e.g. {settings.translate_to_host_path(unread[0])}")

    def under_unverified(path: str) -> bool:
        folder = path.rpartition("/")[0]
        while folder:
            if folder in unverified:
                return True
            folder = folder.rpartition("/")[0]
        return False

    # An empty tree is an unmounted library, never a library the owner emptied
    # — every DB record would read as missing. The folder is asked again right
    # before the delete: a drive that left during the walk handed in a part.
    if not disk_paths or library_unreachable():
        logger.error("Prune aborted: no audio files under "
                     f"{settings.music_library_path} — library not mounted?")
        if progress_cb:
            progress_cb("Prune skipped: no files found on disk")
        return stats

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
            if file_path in disk_paths:
                continue
            if under_unverified(file_path):
                stats["kept_unread"] += 1
                continue
            missing_ids.append(mf_id)
            logger.info(f"Missing: {file_path}")

        if not missing_ids:
            logger.info("Prune: no missing files found")
            return stats

        stats["pruned"] = len(missing_ids)
        logger.info(f"Pruning {len(missing_ids)} missing media files")

        if progress_cb:
            progress_cb(f"Removing {len(missing_ids)} missing files...")

        # Everything below is scoped to the rows THESE files justified. The
        # prune reconciles the file layer; a global "delete what nothing
        # references" sweep reads the whole phantom layer as orphans — a track
        # with no media_file IS a phantom, an album with no variant IS a
        # phantom album, an artist with no tracks IS a minted similar. On
        # 2026-09-01 such a sweep deleted 2.97M tracks, 282k albums and 311k
        # artists (with their bios, tags and similars) on a 12-file prune.
        affected = db.execute(text("""
            SELECT array_agg(DISTINCT track_id) AS tracks,
                   array_agg(DISTINCT album_variant_id) AS variants
            FROM media_files WHERE id = ANY(CAST(:ids AS int[]))
        """), {"ids": missing_ids}).one()
        track_ids = list(affected.tracks or [])
        variant_ids = list(affected.variants or [])

        album_ids = [r[0] for r in db.execute(text("""
            SELECT DISTINCT album_id FROM album_variants
            WHERE id = ANY(CAST(:ids AS int[])) AND album_id IS NOT NULL
        """), {"ids": variant_ids})]

        # Artist links cascade with the track/album rows, so the candidates
        # have to be read before the deletes, not after.
        artist_ids = [r[0] for r in db.execute(text("""
            SELECT artist_id FROM track_artists WHERE track_id = ANY(CAST(:t AS uuid[]))
            UNION
            SELECT artist_id FROM album_artists WHERE album_id = ANY(CAST(:a AS uuid[]))
        """), {"t": [str(t) for t in track_ids], "a": [str(a) for a in album_ids]})]

        db.query(MediaFile).filter(MediaFile.id.in_(missing_ids)).delete(synchronize_session=False)

        # Only true orphans go (canon.identity.ORPHAN_TRACK_SQL): embeddings +
        # analysis_sources cascade with the track, and neither the node's own
        # streamed enrichment nor rows a CGNAT peer push-seeded here (carry)
        # can be re-derived without the audio. The file is gone; the analysis
        # of it is still real. A listen is the same kind of fact, and a track
        # that is also a phantom album's slot stays that album's track.
        r = db.execute(text(f"""
            DELETE FROM tracks t WHERE t.id = ANY(CAST(:ids AS uuid[]))
              AND {ORPHAN_TRACK_SQL.format(t='t')}
        """), {"ids": [str(t) for t in track_ids]})
        stats["orphan_tracks"] = r.rowcount

        r = db.execute(text("""
            DELETE FROM album_variants av WHERE av.id = ANY(CAST(:ids AS int[]))
              AND NOT EXISTS (SELECT 1 FROM media_files mf WHERE mf.album_variant_id = av.id)
        """), {"ids": variant_ids})
        stats["orphan_variants"] = r.rowcount

        r = db.execute(text("""
            DELETE FROM albums a WHERE a.id = ANY(CAST(:ids AS uuid[]))
              AND NOT EXISTS (SELECT 1 FROM album_variants av WHERE av.album_id = a.id)
              AND NOT EXISTS (SELECT 1 FROM album_tracks at WHERE at.album_id = a.id)
        """), {"ids": [str(a) for a in album_ids]})
        stats["orphan_albums"] = r.rowcount

        r = db.execute(text("""
            DELETE FROM artists ar WHERE ar.id = ANY(CAST(:ids AS uuid[]))
              AND NOT EXISTS (SELECT 1 FROM track_artists ta WHERE ta.artist_id = ar.id)
              AND NOT EXISTS (SELECT 1 FROM album_artists aa WHERE aa.artist_id = ar.id)
        """), {"ids": [str(a) for a in artist_ids]})
        stats["orphan_artists"] = r.rowcount

        db.commit()

    logger.info(f"Prune complete: {stats}")
    return stats
