"""
Music library scanner for extracting metadata from audio files.

Creates canonical entities (Artist, Track, Album) with deterministic UUIDs
and physical entities (AlbumVariant, MediaFile) per file on disk.
"""

import logging
import os
import re
import sys
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
import file_stat
from album_identity import assign_dir_albums, dir_disc, group_key
from canon.identity import ORPHAN_TRACK_SQL, elect_analysis_source
from provenance import ANALYSIS_KINDS, relink_returned_files
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
        back = _folder_unreachable and not unreachable
        _folder_unreachable = unreachable
        wake = wake and unreachable != _shown
        if wake:
            _shown = unreachable
    if wake:
        db_execute("NOTIFY sautium_notices")
    if back:
        # The folder answers again: a watch it lost is placed again
        import library_watch
        threading.Thread(target=library_watch.folder_back, daemon=True,
                         name="library-watch-rearm").start()


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


def _own_assignment(album: Optional[str], title: Optional[str], album_artist: Optional[str],
                    artist: Optional[str]) -> Tuple[str, str]:
    """A file's album as its own tags name it: the album tag, else the title
    (a single); the album artist, else the track artist."""
    return (album or title), (album_artist or artist)


def _classify_dirs(entries, sink: FileSink):
    """How each directory's files map to albums (album_identity.
    assign_dir_albums), over the WHOLE folder: this batch's files and the
    rows its folders already hold. ``entries`` = ``(dir_path, metadata)``
    pairs, the directory in DB form (the variant key).

    Returns ``(batch, existing)``:
    - ``batch``: ``{file_path: (album_title, album_artist, track_override,
      disc_override)}`` for this batch's files — a box set or singles
      collection is one album per release group, a mix or a loose per-track
      dump one folder album; a plain single album is each file's own tags.
      A ``None`` track/disc override keeps the file's own value;
    - ``existing``: ``{file id: (album_title, album_artist)}`` for the rows of
      the same folders that are not in the batch — what the folder now makes
      of them. A folder's shape can change as its files arrive (a compilation
      whose first file alone read as its artist's album), so the scan moves a
      row whose assignment changed (LIBRARY-WATCH-DESIGN §3.4 b).

    Cue slices take their sheet's album and are left out: the sheet is the
    album authority for its image, and the renumber-fold is keyed by path."""
    by_dir = defaultdict(list)
    batch: Dict[str, Tuple] = {}
    for dir_path, md in entries:
        title, credit = _own_assignment(md.get("album"), md.get("title"),
                                        md.get("album_artist"), md.get("artist"))
        batch[md.get("file_path")] = (title, credit, None, None)
        if md.get("cue_start_seconds") is not None:
            continue
        by_dir[dir_path].append(md)
    existing_rows = defaultdict(list)
    if by_dir:
        local = sink.location == "local"
        path_col = "file_path" if local else "hqp_path"
        cue_filter = "AND f.cue_start_seconds IS NULL" if local else ""
        for r in db_query(f"""
            SELECT f.id, av.directory_path AS d, f.raw_album, f.disc_number, f.track_number,
                   f.raw_album_artist, f.raw_artist, f.raw_track_name, f.{path_col} AS file_path
            FROM album_variants av JOIN {sink.files_table} f ON f.album_variant_id = av.id
            WHERE av.directory_path = ANY(%(d)s)
              AND av.hqp_endpoint_id IS NOT DISTINCT FROM %(e)s {cue_filter}
        """, {"d": list(by_dir), "e": sink.hqp_endpoint_id}):
            existing_rows[r["d"]].append(r)
    existing: Dict[int, Tuple[str, str]] = {}
    for hd, mds in by_dir.items():
        batch_paths = {md.get("file_path") for md in mds}
        others = [e for e in existing_rows[hd] if e["file_path"] not in batch_paths]
        files = [{"album": md.get("album"), "disc": md.get("disc_number"),
                  "track": md.get("track_number"),
                  "artist_key": md.get("album_artist") or md.get("artist"),
                  "path": md.get("file_path")} for md in mds]
        files += [{"album": e["raw_album"], "disc": e["disc_number"],
                   "track": e["track_number"],
                   "artist_key": e["raw_album_artist"] or e["raw_artist"],
                   "path": e["file_path"]} for e in others]
        folder = os.path.basename(hd.replace("\\", "/").rstrip("/"))
        assignment = assign_dir_albums(folder, files)
        for e in others:
            if assignment is None:
                existing[e["id"]] = _own_assignment(e["raw_album"], e["raw_track_name"],
                                                    e["raw_album_artist"], e["raw_artist"])
            else:
                existing[e["id"]] = assignment[e["file_path"]][:2]
        if assignment is not None:
            for p in batch_paths:
                batch[p] = assignment[p]
    return batch, existing


class _Albums:
    """Which album a file's assignment (title, credit) belongs to — the
    folder rule (LIBRARY-WATCH-DESIGN §3.4 a, §15.1). The folder, not the
    file, is the unit of album identity: a file joins the copy its folder
    already holds for its raw group (album_identity.group_key), wherever the
    canon has moved that copy's album since (an edition renamed or split, a
    twin folded); a file of a disc folder joins the album a sibling disc
    folder under the same parent holds for its group — a folder name of a
    disc's shape, or a disc number the sibling does not hold. Only when
    neither holds one is the album minted from the tags. Without it, a canon
    move of an album's first files sent the later ones to a new album row in
    the same folder, and the edition rules never folded the two halves."""

    def __init__(self, db: Session, sink: FileSink):
        self.db, self.sink = db, sink
        self._held: Dict[str, list] = {}

    def _variants_in(self, dirs: List[str]) -> None:
        want = [d for d in dirs if d not in self._held]
        if not want:
            return
        for d in want:
            self._held[d] = []
        rows = self.db.execute(text(f"""
            SELECT av.id, av.album_id::text AS album_id, av.directory_path, av.raw_title,
                   av.raw_artist, count(f.id) AS files,
                   array_remove(array_agg(DISTINCT COALESCE(f.disc_number, 1)), NULL) AS discs
              FROM album_variants av LEFT JOIN {self.sink.files_table} f ON f.album_variant_id = av.id
             WHERE av.directory_path = ANY(:d)
               AND av.hqp_endpoint_id IS NOT DISTINCT FROM :e
             GROUP BY av.id"""), {"d": want, "e": self.sink.hqp_endpoint_id}).fetchall()
        for r in rows:
            self._held[r.directory_path].append(r)

    def _siblings(self, dir_path: str) -> List:
        parent = dir_path.rstrip("/").rpartition("/")[0]
        if not parent:
            return []
        key = "sib:" + parent
        if key not in self._held:
            rows = self.db.execute(text(f"""
                SELECT av.id, av.album_id::text AS album_id, av.directory_path, av.raw_title,
                       av.raw_artist, count(f.id) AS files,
                       array_remove(array_agg(DISTINCT COALESCE(f.disc_number, 1)), NULL) AS discs
                  FROM album_variants av LEFT JOIN {self.sink.files_table} f ON f.album_variant_id = av.id
                 WHERE starts_with(av.directory_path, :p)
                   AND av.hqp_endpoint_id IS NOT DISTINCT FROM :e
                 GROUP BY av.id"""), {"p": parent + "/", "e": self.sink.hqp_endpoint_id}).fetchall()
            self._held[key] = [r for r in rows if "/" not in r.directory_path[len(parent) + 1:]]
        return [r for r in self._held[key] if r.directory_path != dir_path]

    def forget(self, dir_path: str) -> None:
        """What the folder holds changed (a copy made or moved): read again."""
        self._held.pop(dir_path, None)
        parent = dir_path.rstrip("/").rpartition("/")[0]
        self._held.pop("sib:" + parent, None)

    def resolve(self, dir_path: str, title: str, credit: str, discs: Set[int],
                exclude_variant: Optional[int] = None) -> Tuple[str, bool]:
        """(album id, minted): the album of a copy this folder or a sibling
        disc folder holds for the group, else the one the tags mint. A
        sibling is a disc of the same album when a folder name says so, or
        when this folder's discs — `discs`, the files at hand, with every
        copy the folder already holds — share none with it: another edition
        beside it (one disc against a deluxe two) is no disc of it."""
        key = group_key(title, credit)
        self._variants_in([dir_path])
        here = [v for v in self._held[dir_path]
                if v.id != exclude_variant and v.raw_artist is not None
                and group_key(v.raw_title, v.raw_artist) == key]
        if here:
            return max(here, key=lambda v: (v.files, -v.id)).album_id, False
        folder_discs = set(discs or {1})
        for v in self._held[dir_path]:
            folder_discs.update(v.discs or [])
        for v in sorted(self._siblings(dir_path), key=lambda v: -v.files):
            if (v.id != exclude_variant and v.raw_artist is not None
                    and group_key(v.raw_title, v.raw_artist) == key
                    and (dir_disc(dir_path) or dir_disc(v.directory_path)
                         or folder_discs.isdisjoint(v.discs or []))):
                return v.album_id, False
        return str(album_uuid(title, credit)), True


def _ensure_album(db: Session, album_id: str, title: str, credit: str,
                  md: Optional[Dict[str, Any]] = None) -> None:
    """The album row a minted id names, with the credit it was minted under."""
    from canon.identity import _ensure_artist
    from transliterate import latinize
    db.execute(text("""
        INSERT INTO albums (id, title, title_latin, release_year, label, catalog_number)
        VALUES (:id, :t, :tl, :y, :l, :c) ON CONFLICT (id) DO NOTHING"""),
        {"id": album_id, "t": title, "tl": latinize(title),
         "y": (md or {}).get("release_year"), "l": (md or {}).get("label"),
         "c": (md or {}).get("catalog_number")})
    artist = str(_ensure_artist(db, credit))
    db.execute(text("""INSERT INTO album_artists (album_id, artist_id, role)
                       VALUES (:a, :r, 'primary') ON CONFLICT DO NOTHING"""),
               {"a": album_id, "r": artist})


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
    # The folder → albums pre-pass over each whole folder (box sets,
    # singles, mixes); the moves it asks of rows already held are the
    # caller's (LibraryScanner.scan_and_import → move_albums).
    dir_albums, _existing = _classify_dirs(entries, sink)
    batch_discs: Dict[str, Set[int]] = defaultdict(set)
    for dir_path, md in entries:
        ov = dir_albums.get(md.get("file_path"), (None, None, None, None))[3]
        batch_discs[dir_path].add(ov if ov is not None else (md.get("disc_number") or 1))

    with get_db_context() as db:
        albums = _Albums(db, sink)
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
                if not track_artist_name:
                    logger.warning(f"Missing artist for {file_path}, skipping")
                    stats["errors"] += 1
                    continue
                if not metadata.get("album"):
                    logger.info(f"No album tag, using title as album: {metadata['title']}")
                # The folder's assignment (box set / singles / mix folder →
                # per-group title + credit). The per-track artist is untouched,
                # so track identity stands. Cue slices keep their sheet's.
                album_title, album_artist_name, track_override, disc_override = \
                    dir_albums[file_path]
                disc_number = (disc_override if disc_override is not None
                               else metadata.get("disc_number", 1))
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
                    # ── Album: the folder rule, else minted from the tags ──
                    vkey = (dir_path, group_key(album_title, album_artist_name))
                    if vkey in caches["variant"]:
                        variant = caches["variant"][vkey]
                        album = variant.album
                    else:
                        al_id, minted = albums.resolve(dir_path, album_title, album_artist_name,
                                                       batch_discs[dir_path])
                        album = db.query(Album).filter(Album.id == al_id).first()
                        if not album:
                            album = Album(
                                id=al_id,
                                title=album_title,
                                release_year=metadata.get("release_year"),
                                label=metadata.get("label"),
                                catalog_number=metadata.get("catalog_number"),
                            )
                            db.add(album)
                            db.flush()
                        if minted:
                            # The credit the id was minted from. A copy that
                            # joined an album keeps that album's credits: the
                            # canon may have renamed them.
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
                            aa_key = (album.id, album_artist.id, "primary")
                            if aa_key not in assoc_aa:
                                if not db.query(AlbumArtist).filter(
                                        AlbumArtist.album_id == album.id,
                                        AlbumArtist.artist_id == album_artist.id,
                                        AlbumArtist.role == "primary").first():
                                    db.add(AlbumArtist(album_id=album.id,
                                                       artist_id=album_artist.id, role="primary"))
                                assoc_aa.add(aa_key)
                        # ── Album variant: one physical copy per (dir, album)
                        # at the sink's location — a box set is several albums
                        # in one folder ──
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
                                raw_artist=album_artist_name,
                                sample_rate=metadata.get("sample_rate"),
                                bit_depth=metadata.get("bit_depth"),
                                is_lossless=metadata.get("is_lossless", True),
                                location=sink.location,
                                hqp_endpoint_id=sink.hqp_endpoint_id,
                            )
                            db.add(variant)
                            db.flush()
                            albums.forget(dir_path)
                        elif variant.raw_artist is None:
                            variant.raw_artist = album_artist_name
                        pending_cache.append(("variant", vkey, variant))
                    # A file read here makes its copy present: the walk's own
                    # look (_mark_present) knows only the rows it had before.
                    if variant.absent_since is not None:
                        variant.absent_since = None
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
                        for g in _genres_of(genre_name, caches, pending_cache, db):
                            db.execute(_ALBUM_GENRE_UPSERT, {
                                "album_id": album.id,
                                "genre_id": g.id,
                            })
                    # ── File row ──
                    track_number = (track_override if track_override is not None
                                    else metadata.get("track_number"))
                    if sink.location == "local":
                        st = metadata.get("stat")
                        row = MediaFile(
                            track_id=track.id,
                            album_variant_id=variant.id,
                            file_path=metadata["file_path"],
                            file_format=metadata.get("file_format", "FLAC"),
                            is_lossless=metadata.get("is_lossless", True),
                            file_size_bytes=metadata.get("file_size_bytes"),
                            file_modified_at=metadata.get("file_modified_at"),
                            stat_mtime_ns=st.mtime_ns if st else None,
                            stat_ctime_ns=st.ctime_ns if st else None,
                            cue_stat=metadata.get("cue_stat"),
                            sample_rate=metadata.get("sample_rate"),
                            bit_depth=metadata.get("bit_depth"),
                            bitrate=metadata.get("bitrate"),
                            channels=metadata.get("channels"),
                            duration_seconds=metadata.get("duration_seconds"),
                            cue_start_seconds=metadata.get("cue_start_seconds"),
                            cue_end_seconds=metadata.get("cue_end_seconds"),
                            track_number=track_number,
                            disc_number=disc_number,
                            track_total=metadata.get("track_total"),
                            disc_total=metadata.get("disc_total"),
                            isrc=metadata.get("isrc"),
                            # Original tags — ground truth for re-normalization / correction
                            raw_track_name=metadata.get("title"),
                            raw_artist=metadata.get("artist"),
                            raw_album_artist=metadata.get("album_artist"),
                            raw_album=metadata.get("album"),
                            raw_year=metadata.get("date"),
                            raw_genre=metadata.get("genre"),
                        )
                        if metadata.get("arrived_at") is not None:
                            row.arrived_at = metadata["arrived_at"]
                        db.add(row)
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


def _genres_of(genre_tag: str, caches, pending_cache, db: Session) -> List[Genre]:
    """The distinct genres a file's genre tag names (one +1 per genre per
    file: a tag may normalize two parts to the same genre)."""
    from normalize_genres import parse_genre_string, normalize_genre_name
    out: Dict[Any, Genre] = {}
    for gn in parse_genre_string(genre_tag):
        gn = normalize_genre_name(gn)
        g_uid = genre_uuid(gn)
        genre = caches["genre"].get(g_uid)
        if genre is None:
            genre = db.query(Genre).filter(Genre.id == g_uid).first()
            if not genre:
                genre = Genre(id=g_uid, name=gn)
                db.add(genre)
                db.flush()
            pending_cache.append(("genre", g_uid, genre))
        out[genre.id] = genre
    return list(out.values())


def derive_filetag_genres(db: Session, album_ids) -> None:
    """An album's filetag genre counts as its local files' genre tags make
    them — one per genre per file, derived, never incremented in place (the
    local_play_stats pattern): a file read again with another genre moves
    its album's counts, a file moved to another album moves them between the
    two. An album some of whose files were never read by a release that
    keeps the tag (raw_genre) keeps the counts its imports added: its first
    read by such a release, which every such row gets once, derives them."""
    caches: Dict[str, dict] = {"genre": {}}
    for album in {str(a) for a in album_ids}:
        rows = db.execute(text("""
            SELECT mf.raw_genre, mf.stat_mtime_ns FROM media_files mf
              JOIN album_variants av ON av.id = mf.album_variant_id
             WHERE av.album_id = :a"""), {"a": album}).fetchall()
        if not rows or any(r.stat_mtime_ns is None for r in rows):
            continue
        counts: Dict[Any, int] = defaultdict(int)
        for r in rows:
            if r.raw_genre and r.raw_genre.strip():
                for g in _genres_of(r.raw_genre, caches, [], db):
                    counts[g.id] += 1
        db.execute(text("DELETE FROM album_genres WHERE album_id = :a AND source = 'filetag'"),
                   {"a": album})
        for genre_id, n in counts.items():
            db.execute(text("""INSERT INTO album_genres (album_id, genre_id, source, count)
                               VALUES (:a, :g, 'filetag', :n)"""),
                       {"a": album, "g": str(genre_id), "n": n})


_HELD_SQL = text("""
    WITH w(path, size, mtime, ctime) AS (
        SELECT * FROM unnest(CAST(:paths AS text[]), CAST(:sizes AS bigint[]),
                             CAST(:mtimes AS bigint[]), CAST(:ctimes AS bigint[])))
    SELECT w.path, array_agg(mf.cue_start_seconds) AS starts,
           bool_or(mf.stat_mtime_ns IS NULL OR mf.file_size_bytes IS DISTINCT FROM w.size
                   OR mf.stat_mtime_ns <> w.mtime OR mf.stat_ctime_ns IS DISTINCT FROM w.ctime)
               AS changed,
           min(mf.cue_stat) AS cue_stat        -- one sheet stat for all of an image's slices
      FROM w JOIN media_files mf ON mf.file_path = w.path
     GROUP BY w.path
""")

_REREAD_ROWS_SQL = text("""
    SELECT mf.id, mf.track_id::text AS track_id, mf.album_variant_id, mf.file_path,
           mf.cue_start_seconds, mf.raw_track_name, mf.raw_artist, mf.raw_album_artist,
           mf.raw_album, mf.raw_genre, mf.duration_seconds, mf.sample_rate, mf.bit_depth,
           mf.stat_mtime_ns, mf.file_size_bytes, av.raw_title AS v_title, av.raw_artist AS v_artist, av.album_id::text AS album_id
      FROM media_files mf JOIN album_variants av ON av.id = mf.album_variant_id
     WHERE mf.file_path = ANY(CAST(:paths AS text[]))
""")

_REREAD_SQL = text("""
    UPDATE media_files SET
        file_format = :file_format, is_lossless = :is_lossless,
        file_size_bytes = :file_size_bytes, file_modified_at = :file_modified_at,
        arrived_at = CASE WHEN stat_mtime_ns IS NULL AND CAST(:arrived_at AS timestamptz) IS NOT NULL
                          THEN CAST(:arrived_at AS timestamptz) ELSE arrived_at END,
        stat_mtime_ns = :stat_mtime_ns, stat_ctime_ns = :stat_ctime_ns, cue_stat = :cue_stat,
        sample_rate = :sample_rate, bit_depth = :bit_depth, bitrate = :bitrate,
        channels = :channels, duration_seconds = :duration_seconds,
        cue_end_seconds = :cue_end_seconds, track_number = :track_number,
        disc_number = :disc_number, track_total = :track_total, disc_total = :disc_total,
        isrc = :isrc, raw_track_name = :raw_track_name, raw_artist = :raw_artist,
        raw_album_artist = :raw_album_artist, raw_album = :raw_album, raw_year = :raw_year,
        raw_genre = :raw_genre, decode_refused = false, cover_processed_at = NULL,
        -- the canon reads the file again when what it decides on moved: a
        -- name, a number, a total (the right-hand columns are the old row's)
        content_at = CASE WHEN stat_mtime_ns IS NOT NULL AND (
                -- the bytes (a rip replaced in place: the length the canon
                -- matches recordings by), or a name, a number, a total
                stat_mtime_ns IS DISTINCT FROM CAST(:stat_mtime_ns AS bigint)
             OR file_size_bytes IS DISTINCT FROM CAST(:file_size_bytes AS bigint)
             OR raw_track_name IS DISTINCT FROM CAST(:raw_track_name AS text)
             OR raw_artist IS DISTINCT FROM CAST(:raw_artist AS text)
             OR raw_album_artist IS DISTINCT FROM CAST(:raw_album_artist AS text)
             OR raw_album IS DISTINCT FROM CAST(:raw_album AS text)
             OR raw_year IS DISTINCT FROM CAST(:raw_year AS text)
             OR track_number IS DISTINCT FROM CAST(:track_number AS integer)
             OR disc_number IS DISTINCT FROM CAST(:disc_number AS integer)
             OR track_total IS DISTINCT FROM CAST(:track_total AS integer)
             OR disc_total IS DISTINCT FROM CAST(:disc_total AS integer))
            THEN now() ELSE content_at END
     WHERE id = :id
""")

# A file replaced at its path by other material (another rip, a re-encode)
# declares another length or format: the analysis made from the old bytes
# no longer describes it — unlinked, the track is pending and is analysed
# again or relinked by its fingerprint. The same length and format is a
# re-tag, and the analysis stays.
_MATERIAL_LENGTH_TOLERANCE = 0.5


def reread_metadata(entries: List[Tuple[str, Dict[str, Any], Any]], assignments: Dict[str, Tuple],
                    stats: Dict[str, int], moves: Dict[int, List[Tuple[int, str, str, Optional[int]]]],
                    touched_albums: Set[str]) -> None:
    """Rows read again in place — `media_files.id` kept: listens, sessions,
    the queue and the analysis link name it (LIBRARY-WATCH-DESIGN §6.2).
    ``entries`` = ``(dir_path, metadata, row)``. A changed title or artist
    moves the file to the track the tags name (canon.identity.
    rehome_file_track); a changed album or credit is recorded in ``moves``
    ({variant id: [(file id, title, credit, disc)]}) for move_albums, which
    moves a copy whole when all its files go. Identity is compared raw
    against raw — the key the tags mint now against the one the row's stored
    tags minted — so whatever the canon renamed since is no change."""
    from canon.identity import rehome_file_track
    stats.setdefault("reread", 0)
    with get_db_context() as db:
        for dir_path, md, row in entries:
            if not md.get("title") or not (md.get("artist") or md.get("album_artist")):
                logger.warning(f"Missing title or artist for {md['file_path']}, its row kept as it was")
                stats["errors"] += 1
                continue
            title, credit, track_ov, disc_ov = assignments[md["file_path"]]
            disc = disc_ov if disc_ov is not None else md.get("disc_number", 1)
            st = md.get("stat")
            try:
                with db.begin_nested():
                    db.execute(_REREAD_SQL, {
                        "id": row.id, "file_format": md.get("file_format", "FLAC"),
                        "is_lossless": md.get("is_lossless", True),
                        "file_size_bytes": md.get("file_size_bytes"),
                        "file_modified_at": md.get("file_modified_at"),
                        "arrived_at": md.get("arrived_at"),
                        "stat_mtime_ns": st.mtime_ns if st else None,
                        "stat_ctime_ns": st.ctime_ns if st else None,
                        "cue_stat": md.get("cue_stat"),
                        "sample_rate": md.get("sample_rate"), "bit_depth": md.get("bit_depth"),
                        "bitrate": md.get("bitrate"), "channels": md.get("channels"),
                        "duration_seconds": md.get("duration_seconds"),
                        "cue_end_seconds": md.get("cue_end_seconds"),
                        "track_number": track_ov if track_ov is not None else md.get("track_number"),
                        "disc_number": disc, "track_total": md.get("track_total"),
                        "disc_total": md.get("disc_total"), "isrc": md.get("isrc"),
                        "raw_track_name": md.get("title"), "raw_artist": md.get("artist"),
                        "raw_album_artist": md.get("album_artist"), "raw_album": md.get("album"),
                        "raw_year": md.get("date"), "raw_genre": md.get("genre")})
                    track_artist = md.get("artist") or md.get("album_artist")
                    old_key = (track_uuid(row.raw_track_name, row.raw_artist or row.raw_album_artist)
                               if row.raw_track_name else None)
                    track = row.track_id
                    if old_key != track_uuid(md["title"], track_artist):
                        # first: the analysis of the file's material moves with it
                        track = rehome_file_track(db, row.id, row.track_id, md["title"],
                                                  track_artist)
                        db.execute(text("UPDATE media_files SET content_at = now() WHERE id = :id"),
                                   {"id": row.id})
                        stats["moved"] = stats.get("moved", 0) + (track != row.track_id)
                    if row.stat_mtime_ns is not None and (
                            row.stat_mtime_ns != (st.mtime_ns if st else None)
                            or row.file_size_bytes != md.get("file_size_bytes")):
                        # The bytes changed since the last read (a ctime alone —
                        # a chmod, a hard-link backup, an archive bit — is no
                        # write): what was registered of the file (a print
                        # taken mid-copy, an analysis of its old bytes) no
                        # longer names it. A source holding no record goes;
                        # one holding records is unlinked, and the fingerprint
                        # relinks it if the material is the same.
                        db.execute(text("""
                            DELETE FROM analysis_sources s WHERE s.media_file_id = :id
                               AND NOT EXISTS (SELECT 1 FROM embeddings x WHERE x.analysis_source_id = s.id)
                               AND NOT EXISTS (SELECT 1 FROM audio_features x WHERE x.analysis_source_id = s.id)
                               AND NOT EXISTS (SELECT 1 FROM track_instruments x WHERE x.analysis_source_id = s.id)
                               AND NOT EXISTS (SELECT 1 FROM analysis_sources m WHERE m.material_of = s.id)"""),
                                   {"id": row.id})
                        db.execute(text("UPDATE analysis_sources SET media_file_id = NULL "
                                        "WHERE media_file_id = :id"), {"id": row.id})
                        elect_analysis_source(db, track)
                    old_dur = float(row.duration_seconds) if row.duration_seconds is not None else None
                    new_dur = md.get("duration_seconds")
                    if ((old_dur is None) != (new_dur is None)
                            or (old_dur is not None and abs(old_dur - float(new_dur)) > _MATERIAL_LENGTH_TOLERANCE)
                            or row.sample_rate != md.get("sample_rate")
                            or row.bit_depth != md.get("bit_depth")):
                        db.execute(text("UPDATE analysis_sources SET media_file_id = NULL "
                                        "WHERE media_file_id = :id"), {"id": row.id})
                        elect_analysis_source(db, track)
                    if row.v_title is None or row.v_artist is None:
                        # a copy from before its group was kept: filled, never
                        # moved — the canon may have re-keyed its album since
                        db.execute(text("""UPDATE album_variants
                                              SET raw_title = COALESCE(raw_title, :t),
                                                  raw_artist = COALESCE(raw_artist, :c)
                                            WHERE id = :v"""),
                                   {"t": title, "c": credit, "v": row.album_variant_id})
                    elif group_key(row.v_title, row.v_artist) != group_key(title, credit):
                        moves.setdefault(row.album_variant_id, []).append((row.id, title, credit, disc))
                    if (row.raw_genre or None) != (md.get("genre") or None):
                        touched_albums.add(row.album_id)
                stats["reread"] += 1
                if stats["reread"] % 100 == 0:
                    db.commit()         # the rows read so far: their locks go
            except Exception as e:
                logger.error(f"Error reading {md['file_path']} again: {e}", exc_info=True)
                stats["errors"] += 1
        db.commit()


def move_albums(moves: Dict[int, List[Tuple[int, str, str, Optional[int]]]], sink: FileSink,
                touched_albums: Set[str]) -> int:
    """Rows whose folder now assigns them another album (a tag changed, or
    the folder's shape did) move to the album of their new group, by the
    folder rule (_Albums.resolve). A copy all of whose files move to one
    group moves whole (_merge_album_variants), and an album left with no
    other copy follows it (_update_album_uuid: sessions' album origins,
    descriptions, genres and its seal with it) — so a title fixed in all an
    album's files renames the album rather than leaving an empty one. A copy
    or an album left holding nothing goes, as the CUE supersede drops what
    it leaves: nothing the owner sees is removed — the files are there under
    their new album. Returns how many files moved."""
    from canon.identity import _merge_album_variants, _update_album_uuid
    table = sink.files_table
    moved = 0
    with get_db_context() as db:
        albums = _Albums(db, sink)
        for vid, files in moves.items():
            v = db.execute(text("SELECT id, album_id::text AS album_id, directory_path "
                                "FROM album_variants WHERE id = :v"), {"v": vid}).first()
            if v is None:
                continue
            held = set(db.execute(text(f"SELECT id FROM {table} WHERE album_variant_id = :v"),
                                  {"v": vid}).scalars())
            groups = {group_key(t, c) for _f, t, c, _d in files}
            _f0, title, credit, _disc = files[0]
            with db.begin_nested():
                if held <= {f for f, *_ in files} and len(groups) == 1:
                    target, minted = albums.resolve(v.directory_path, title, credit,
                                                    {d or 1 for *_x, d in files}, exclude_variant=vid)
                    if minted:
                        _ensure_album(db, target, title, credit)
                    if target != v.album_id:
                        alone = not db.execute(text("""
                            SELECT EXISTS (SELECT 1 FROM album_variants WHERE album_id = :a AND id <> :v)
                                OR EXISTS (SELECT 1 FROM album_tracks WHERE album_id = :a)"""),
                            {"a": v.album_id, "v": vid}).scalar()
                        if alone:
                            # the old album's credits are its raw tags' (or the
                            # canon's of them): the target keeps its own
                            db.execute(text("DELETE FROM album_artists WHERE album_id = :a"),
                                       {"a": v.album_id})
                            _update_album_uuid(db, v.album_id, target)
                        else:
                            _merge_album_variants(db, target, variant_ids=[vid])
                        if minted:
                            # minted from (title, credit): its credit is that one, not
                            # the one the merge brought along from the old album
                            from canon.identity import _ensure_artist
                            db.execute(text("""DELETE FROM album_artists
                                                WHERE album_id = :a AND role = 'primary'
                                                  AND artist_id <> :r"""),
                                       {"a": target, "r": str(_ensure_artist(db, credit))})
                        touched_albums.update({v.album_id, target})
                    db.execute(text("""UPDATE album_variants SET raw_title = :t, raw_artist = :c
                                        WHERE album_id = :a AND directory_path = :d
                                          AND hqp_endpoint_id IS NOT DISTINCT FROM :e"""),
                               {"t": title, "c": credit, "a": target, "d": v.directory_path,
                                "e": sink.hqp_endpoint_id})
                else:
                    for fid, t, c, d in files:
                        target, minted = albums.resolve(v.directory_path, t, c,
                                                        {d or 1 for *_x, d in files}, exclude_variant=vid)
                        if minted:
                            _ensure_album(db, target, t, c)
                        dest = db.execute(text("""
                            SELECT id FROM album_variants
                             WHERE directory_path = :d AND album_id = :a
                               AND hqp_endpoint_id IS NOT DISTINCT FROM :e"""),
                            {"d": v.directory_path, "a": target, "e": sink.hqp_endpoint_id}).scalar()
                        if dest is None:
                            dest = db.execute(text("""
                                INSERT INTO album_variants (album_id, directory_path, raw_title, raw_artist,
                                                            location, hqp_endpoint_id, sample_rate,
                                                            bit_depth, is_lossless)
                                SELECT :a, :d, :t, :c, location, hqp_endpoint_id, sample_rate,
                                       bit_depth, is_lossless
                                  FROM album_variants WHERE id = :v RETURNING id"""),
                                {"a": target, "d": v.directory_path, "t": t, "c": c, "v": vid}).scalar()
                            albums.forget(v.directory_path)
                        db.execute(text(f"UPDATE {table} SET album_variant_id = :dest WHERE id = :f"),
                                   {"dest": dest, "f": fid})
                        touched_albums.update({v.album_id, target})
                    db.execute(text("""DELETE FROM album_variants av WHERE av.id = :v
                                         AND NOT EXISTS (SELECT 1 FROM media_files WHERE album_variant_id = :v)
                                         AND NOT EXISTS (SELECT 1 FROM hqp_library_files
                                                          WHERE album_variant_id = :v)"""), {"v": vid})
                    db.execute(text("""DELETE FROM albums a WHERE a.id = :a
                                         AND NOT EXISTS (SELECT 1 FROM album_variants WHERE album_id = :a)
                                         AND NOT EXISTS (SELECT 1 FROM album_tracks WHERE album_id = :a)"""),
                               {"a": v.album_id})
                if table == "media_files":
                    db.execute(text("UPDATE media_files SET content_at = now() WHERE id = ANY(:f)"),
                               {"f": [f for f, *_ in files]})
            albums.forget(v.directory_path)
            moved += len(files)
        db.commit()
    return moved


def backfill_variant_groups(cur) -> int:
    """The raw group every copy was minted under — its title
    (album_variants.raw_title, not written before 2026-09-28) and its album
    credit (raw_artist, new) — for copies made before they were kept: what
    the folder rule makes of each folder's rows by their stored tags (the
    classification an import runs), the most common among a copy's files.
    Without it a copy the canon has moved since would not be found by its
    group, and the next file of its folder would mint a second album beside
    it — or its first re-read would move it back to the raw key. Idempotent:
    only a missing title or credit is written. Returns how many copies."""
    out = 0
    for table, path_col, cue in (("media_files", "file_path", "f.cue_start_seconds"),
                                 ("hqp_library_files", "hqp_path", "NULL::float")):
        cur.execute(f"""
            SELECT av.id, av.directory_path, f.{path_col}, f.raw_album, f.disc_number,
                   f.track_number, f.raw_album_artist, f.raw_artist, f.raw_track_name, {cue},
                   av.hqp_endpoint_id
              FROM album_variants av JOIN {table} f ON f.album_variant_id = av.id
             WHERE av.raw_artist IS NULL OR av.raw_title IS NULL
             ORDER BY av.directory_path""")
        # one folder of one library: two HQPlayers' alike layouts are two
        by_dir: Dict[Tuple[str, Optional[int]], list] = defaultdict(list)
        for row in cur.fetchall():
            by_dir[(row[1], row[10])].append(row)
        groups: Dict[int, Dict[Tuple[str, str], int]] = defaultdict(lambda: defaultdict(int))
        for (d, _endpoint), rows in by_dir.items():
            plain = [r for r in rows if r[9] is None]
            files = [{"album": r[3], "disc": r[4], "track": r[5], "artist_key": r[6] or r[7],
                      "path": r[2]} for r in plain]
            folder = os.path.basename(d.replace("\\", "/").rstrip("/"))
            assignment = assign_dir_albums(folder, files) if files else None
            for r in rows:
                if r[9] is None and assignment is not None:
                    title, credit = assignment[r[2]][:2]
                else:
                    title, credit = _own_assignment(r[3], r[8], r[6], r[7])
                if title and credit:
                    groups[r[0]][(title, credit)] += 1
        for vid, counted in groups.items():
            title, credit = sorted(counted.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            cur.execute("""UPDATE album_variants SET raw_title = COALESCE(raw_title, %s),
                                                     raw_artist = COALESCE(raw_artist, %s)
                            WHERE id = %s AND (raw_title IS NULL OR raw_artist IS NULL)""",
                        (title, credit, vid))
            out += cur.rowcount
    return out


def _int_or_none(value) -> Optional[int]:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _declared_totals(audio, get_tag) -> Tuple[Optional[int], Optional[int]]:
    """The track and disc totals the file's own tags declare — "n/N" in the
    number tags (Vorbis, ID3 TRCK/TPOS, MP4 trkn/disk), or TRACKTOTAL /
    TOTALTRACKS and DISCTOTAL / TOTALDISCS. None where nothing declares one:
    an album whose files declare nothing is complete as soon as it is read
    (few tracks is no incomplete album)."""
    def total_of(vorbis_key, id3_key, mp4_key, *total_keys):
        for key in total_keys:
            n = _int_or_none(get_tag(key))
            if n:
                return n
        number = get_tag(vorbis_key, id3_key)
        if number and "/" in str(number):
            return _int_or_none(str(number).split("/", 1)[1])
        if audio.tags is not None:
            try:
                pair = audio.tags.get(mp4_key)
            except (ValueError, KeyError):
                pair = None
            if pair and isinstance(pair[0], tuple) and len(pair[0]) > 1:
                return _int_or_none(pair[0][1])
        return None
    return (total_of("tracknumber", "TRCK", "trkn", "tracktotal", "totaltracks"),
            total_of("discnumber", "TPOS", "disk", "disctotal", "totaldiscs"))


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
        # The scopes whose folder was gone at the last walk (find_audio_files).
        self.last_gone: List[str] = []

    @staticmethod
    def extract_metadata(file_path: Path) -> Optional[Dict[str, Any]]:
        """
        Extract metadata from audio file.

        Returns:
            Dictionary with extracted metadata or None if failed.
        """
        try:
            # The stat before the read: a write during the read moves the
            # file's stat away from the one recorded, so the next comparison
            # reads it again — taken after, it could name a version the tags
            # were not read from (LIBRARY-WATCH-DESIGN §6.1).
            stat = file_stat.stat_of(str(file_path))
            birth_ns = file_stat.birth_ns(str(file_path))
            audio = mutagen.File(file_path)
            if audio is None:
                logger.warning(f"Unsupported format: {file_path}")
                return None

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
                "file_size_bytes": stat.size,
                "stat": stat,
                # when the file came onto this disk; None where the disk keeps
                # no birth (a 9p or drvfs bind): the first read stands in
                "arrived_at": (datetime.fromtimestamp(birth_ns / 1e9, tz=timezone.utc)
                               if birth_ns is not None else None),
                "file_format": file_format,
                "file_modified_at": datetime.fromtimestamp(stat.mtime_ns / 1e9, tz=timezone.utc),
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

            metadata["track_total"], metadata["disc_total"] = _declared_totals(audio, get_tag)

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
        scopes: Optional[Sequence[Tuple[str, bool]]] = None,
        stats: Optional[Dict[str, "file_stat.FileStat"]] = None,
        gone: Optional[List[str]] = None,
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
            scopes: (folder relative to the library, recursive) pairs to
                    walk instead of `subpath` — the folders a watcher
                    found changed; a folder not recursive is listed alone.
            stats: filled with each audio file's and cue sheet's stat by
                   its walk path (file_stat: from the listing on POSIX, the
                   one handle Windows needs for the change time).
            gone: filled with the scopes whose folder no longer exists —
                  every file the catalogue holds under it is missing.

        Returns:
            (audio file Paths, .cue file Paths, folders that could not be
            read) — one walk collects all three. A folder it could not list
            whole (no permission, an entry whose type it could not read, a
            nested mount gone, removed mid-walk) is reported, never skipped
            in silence: nothing beneath it is in the lists, and a prune must
            not read it as deleted.
        """
        if scopes is None:
            if subpath:
                scan_path = self.library_path / subpath
                if not scan_path.exists():
                    raise ValueError(f"Subpath does not exist: {scan_path}")
                logger.info(f"Searching for audio files in {scan_path} (subpath: {subpath})")
            else:
                logger.info(f"Searching for audio files in {self.library_path}")
            scopes = [(subpath or "", True)]

        audio_files: List[Path] = []
        cue_files: List[Path] = []
        unread: List[str] = []

        def stat_entry(entry) -> bool:
            if stats is None:
                return True
            try:
                stats[entry.path] = (file_stat.stat_of(entry.path) if sys.platform == "win32"
                                     else file_stat.from_posix(entry.stat()))
            except FileNotFoundError:
                return False        # gone between the listing and its stat
            return True

        # A scandir walk of its own: Path.rglob drops a folder it may not list
        # without a word, and os.walk files a folder whose type it could not
        # read (a stale nested mount) as a plain name. Entry types come from
        # the directory listing, so a regular file costs no stat; a symlink is
        # followed to its file (rglob's is_file did the same) but never into
        # a folder. Absolute from the root, as every path the prune compares.
        folders = []
        for rel, recursive in scopes:
            root = (self.library_path / rel) if rel else self.library_path
            if rel and not root.is_dir():
                if gone is not None:
                    gone.append(rel)
                continue
            folders.append((str(root.absolute()), recursive))
        while folders:
            if cancel_check and cancel_check():
                logger.info("Discovery cancelled by user")
                break
            folder, recursive = folders.pop()
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
                                if stat_entry(entry):
                                    here_audio.append(Path(entry.path))
                            elif suffix == ".cue":
                                if stat_entry(entry):
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
            if recursive:
                folders.extend((f, True) for f in subfolders)
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
        scopes: Optional[Sequence[Tuple[str, bool]]] = None,
        leave: Set[str] = frozenset(),
    ) -> Dict[str, int]:
        """
        Scan library and import metadata to database.

        Two-phase pipeline:
          Phase 1 — extract metadata in parallel (ThreadPoolExecutor)
          Phase 2 — import to DB single-threaded with entity caching

        Disk reads stay roughly sequential (OS scheduler + small header
        reads), so this is safe for both SSD and HDD.

        A file the catalogue holds is read again when its stat (size,
        mtime, ctime — file_stat) differs from the one its last read
        recorded, or its cue sheet's does: a re-tagged file reaches the
        catalogue, and a file read while it was still being written is read
        once more when it is done (LIBRARY-WATCH-DESIGN §6). The row is
        updated in place.

        Args:
            limit: Maximum number of files to scan (for testing).
            skip_existing: Skip files whose stat did not move; False reads
                           every file again.
            subpath: Optional subdirectory within library to scan.
            progress_cb: Callback(msg, stats) for progress reporting.
            cancel_check: Callback() -> bool, returns True if cancel requested.
            scopes: (folder, recursive) pairs to walk instead of `subpath`.
            leave: walk paths a watcher knows a writer still holds: their
                   close brings them back.

        Returns:
            Dictionary with statistics (processed, added, skipped, reread,
            errors).
        """
        stats = {
            "processed": 0,
            "added": 0,
            "skipped": 0,
            "reread": 0,
            "moved": 0,
            "held": 0,
            "errors": 0,
            "unique_tracks": 0,
            "superseded": 0,
            "unread": 0,
            "back": 0,
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
        disk_stats: Dict[str, file_stat.FileStat] = {}
        gone: List[str] = []
        audio_files, cue_files, unread = self.find_audio_files(
            limit=limit, subpath=subpath, cancel_check=cancel_check, scopes=scopes,
            stats=disk_stats, gone=gone)
        stats["unread"] = len(unread)
        self.last_unread = unread
        self.last_gone = gone
        if audio_files or cue_files:
            # music found in the folder: a watch waiting for it is armed — the
            # look cannot say so while the catalogue is empty (a first scan)
            import library_watch
            threading.Thread(target=library_watch.folder_back, daemon=True,
                             name="library-watch-rearm").start()
        if _cancelled():
            logger.info("Scan cancelled during discovery")
            return stats

        # The prune that closes a scan run reconciles this very tree. Hand it
        # this walk, with the paths it could not read — the prune keeps what
        # lies under them. A limited scan truncates the list, so it never
        # becomes a prune input — everything outside the limit would read as
        # deleted.
        walked = {settings.translate_to_host_path(str(fp.absolute())): fp for fp in audio_files}
        self.last_disk_paths = None if limit else set(walked)
        if not audio_files:
            logger.warning("No audio files found")
            return stats
        # A copy that was away is present again as soon as any walk sees one
        # of its files — a limited or partial one too: seeing is proof.
        stats["back"] = _mark_present(set(walked))

        total_files = len(audio_files)

        # str(walk path) -> CueSheet for every image a usable cue governs.
        cue_map = cue_sheet.build_cue_map(cue_files, AUDIO_EXTENSIONS)

        def sheet_stat(fp: Path) -> Optional[List[int]]:
            sheet = cue_map.get(str(fp))
            st = disk_stats.get(str(sheet.cue_path)) if sheet is not None else None
            return list(st) if st is not None else None

        # ── What the catalogue holds of the walked files ────────────
        # Per path: its rows' cue starts (a plain file's is {None}), whether
        # any row's stat differs from the walk's, and the sheet stat its
        # slices recorded. One statement; a path it does not return is new.
        hosts = list(walked)
        held: Dict[str, Any] = {}
        if hosts:
            with get_db_context() as db:
                for r in db.execute(_HELD_SQL, {
                        "paths": hosts,
                        "sizes": [disk_stats[str(walked[h])].size for h in hosts],
                        "mtimes": [disk_stats[str(walked[h])].mtime_ns for h in hosts],
                        "ctimes": [disk_stats[str(walked[h])].ctime_ns for h in hosts]}):
                    held[r.path] = r
        logger.info(f"Found {len(held)} of the walk's {len(hosts)} files in the database")

        files_to_process: List[Path] = []
        for host, fp in walked.items():
            sheet = cue_map.get(str(fp))
            want = ({t.start_seconds for t in sheet.tracks} if sheet is not None else {None})
            h = held.get(host)
            if (h is not None and skip_existing and set(h.starts) == want and not h.changed
                    and (sheet is None or list(h.cue_stat or []) == (sheet_stat(fp) or []))):
                stats["skipped"] += 1
            elif str(fp) in leave or file_stat.writer_holds(str(fp)):
                # a writer holds it: its close notifies again, and this
                # stat would be the copy's middle
                stats["held"] += 1
            else:
                files_to_process.append(fp)

        stats["processed"] = stats["skipped"] + stats["held"]

        if not files_to_process:
            stats["processed"] = total_files
            _report(f"All {total_files} files already in database")
            logger.info("No new or changed files to process")
            return stats

        _report(f"Found {total_files} files, {len(files_to_process)} new or changed")

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
            had = set(held[host_path].starts) if host_path in held else set()
            if sheet is None:
                expanded.append((fp, md))
                if had - {None}:
                    recon_plans[host_path] = {None}
                continue
            starts = {t.start_seconds for t in sheet.tracks}
            stat = sheet_stat(fp)
            for tr in sheet.tracks:
                slice_md = cue_sheet.synthesize_metadata(md, sheet, tr, md.get("duration_seconds"))
                slice_md["cue_stat"] = stat
                expanded.append((fp, slice_md))
            if had - starts:
                recon_plans[host_path] = starts
        metadata_results = expanded

        # ── Phase 2: the database ───────────────────────────────────
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

        # Each entry: a row the catalogue holds at its (path, cue start) is
        # read again in place; one it does not is imported. The folder's
        # assignment covers both, and the rows of the same folders that are
        # neither (§3.4 b).
        held_rows: Dict[Tuple[str, Any], Any] = {}
        reread_paths = [md["file_path"] for _d, md in entries if md["file_path"] in held]
        if reread_paths:
            with get_db_context() as db:
                for r in db.execute(_REREAD_ROWS_SQL, {"paths": list(set(reread_paths))}):
                    held_rows[(r.file_path, r.cue_start_seconds)] = r
        assignments, existing = _classify_dirs(entries, LOCAL_FILES)
        rereads, new_entries = [], []
        for dir_path, md in entries:
            row = held_rows.get((md["file_path"], md.get("cue_start_seconds")))
            if row is None:
                new_entries.append((dir_path, md))
            else:
                rereads.append((dir_path, md, row))
        moves: Dict[int, List[Tuple[int, str, str, Optional[int]]]] = {}
        touched_albums: Set[str] = set()
        if rereads:
            reread_metadata(rereads, assignments, stats, moves, touched_albums)
        import_metadata(new_entries, sink=LOCAL_FILES, stats=stats, progress_cb=progress_cb,
                        cancel_check=None if cancelled_in_phase1 else cancel_check)
        # The rows of the same folders whose folder now makes another album
        # of them: the shape changed as files arrived or were re-tagged.
        if existing:
            with get_db_context() as db:
                for r in db.execute(text("""
                    SELECT mf.id, mf.album_variant_id, mf.disc_number, av.raw_title, av.raw_artist
                      FROM media_files mf JOIN album_variants av ON av.id = mf.album_variant_id
                     WHERE mf.id = ANY(:ids)"""), {"ids": list(existing)}):
                    title, credit = existing[r.id]
                    if (r.raw_title is not None and r.raw_artist is not None
                            and group_key(r.raw_title, r.raw_artist) != group_key(title, credit)):
                        moves.setdefault(r.album_variant_id, []).append(
                            (r.id, title, credit, r.disc_number))
        if moves:
            stats["moved"] += move_albums(moves, LOCAL_FILES, touched_albums)
        if touched_albums:
            with get_db_context() as db:
                derive_filetag_genres(db, touched_albums)
                db.commit()
        # A slot queued on a file of a copy that is away moves to a copy of
        # its track as soon as one is here: the queue's rebind looks again.
        if stats["added"]:
            with get_db_context() as db:
                db.execute(text("""
                    SELECT pg_notify('sautium_files_removed', '')
                    WHERE EXISTS (SELECT 1 FROM album_variants WHERE absent_since IS NOT NULL)"""))
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
                for table in (*ANALYSIS_KINDS, "analysis_sources"):
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


def prune_counts(stats: Dict[str, Any]) -> Dict[str, int]:
    """What a plan asks about, from a prune's stats: the files that go
    (`files`, a copy found again elsewhere counted apart as `copies`), what
    only they held (tracks, albums, artists), and the copies a Rescan just
    set aside as away (`away`, nothing removed)."""
    return {"files": stats["pruned"] - stats["copy_files"], "copies": stats["copies"],
            "tracks": stats["orphan_tracks"], "albums": stats["orphan_albums"],
            "artists": stats["orphan_artists"], "away": stats["away"]}


# What a yes is held to: no more of what only the removed files held than the
# question named. Files and copies cannot grow — the yes takes a subset of the
# plan's files.
_BOUND = ("tracks", "albums", "artists")


def _mark_present(paths: Set[str]) -> int:
    """Mark present again the away copies one of whose files a walk saw.
    Returns how many came back."""
    with get_db_context() as db:
        if db.execute(text("SELECT 1 FROM album_variants WHERE absent_since IS NOT NULL "
                           "LIMIT 1")).first() is None:
            return 0
        back_ids = list(db.execute(text("""
            UPDATE album_variants av SET absent_since = NULL
            WHERE av.absent_since IS NOT NULL
              AND EXISTS (SELECT 1 FROM media_files mf
                          WHERE mf.album_variant_id = av.id
                            AND mf.file_path = ANY(CAST(:p AS text[])))
            RETURNING av.id
        """), {"p": list(paths)}).scalars())
        # The rule picks again now that the copy is readable: a better rip
        # back is an upgrade, as a better rip added is — the passes re-analyse
        # from it; an equal one leaves the analysis where it is.
        if back_ids:
            for tid in db.execute(text("""
                SELECT DISTINCT mf.track_id FROM media_files mf
                WHERE mf.album_variant_id = ANY(CAST(:v AS int[]))
                  AND EXISTS (SELECT 1 FROM media_files o
                              WHERE o.track_id = mf.track_id
                                AND o.album_variant_id <> mf.album_variant_id)
            """), {"v": back_ids}).scalars():
                elect_analysis_source(db, tid)
        back = len(back_ids)
    if back:
        logger.info(f"{back} cop(ies) away are present again: the walk saw their files")
    return back


def _prune_stats() -> Dict[str, Any]:
    return {"checked": 0, "pruned": 0, "kept_unread": 0, "orphan_tracks": 0,
            "orphan_variants": 0, "orphan_albums": 0, "orphan_artists": 0,
            "copies": 0, "copy_files": 0, "away": 0, "back": 0, "relinked": 0}


# The local copies whose files are ALL among a walk's missing ones, and
# whether ONE other present copy of the same album holds every one of their
# tracks — a folder moved within the library, re-ripped elsewhere, the card's
# copy away while the disk holds the album, the disk moved to the HQPlayer's
# box. Present means seen: a local copy's file this very walk found
# (`seen`, its paths — a copy outside a --path walk or under an unread folder
# was not looked at), or a copy in an HQPlayer's library, which its own sync
# keeps. One statement over the missing set.
_GONE_COPIES_SQL = text("""
    WITH missing AS (SELECT unnest(CAST(:ids AS int[])) AS id),
    gone AS (
        SELECT mf.album_variant_id AS vid
        FROM media_files mf LEFT JOIN missing m ON m.id = mf.id
        WHERE mf.album_variant_id IN (SELECT f.album_variant_id FROM media_files f
                                      JOIN missing USING (id))
        GROUP BY mf.album_variant_id
        HAVING count(m.id) = count(*)
    )
    SELECT g.vid, av.absent_since IS NOT NULL AS away,
           EXISTS (
               SELECT 1 FROM album_variants ov
               WHERE ov.album_id = av.album_id AND ov.id <> g.vid AND ov.absent_since IS NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM media_files mf WHERE mf.album_variant_id = g.vid
                       AND NOT EXISTS (SELECT 1 FROM media_files o
                                       WHERE o.album_variant_id = ov.id
                                         AND o.track_id = mf.track_id
                                         AND o.file_path = ANY(CAST(:seen AS text[])))
                       AND NOT EXISTS (SELECT 1 FROM hqp_library_files h
                                       WHERE h.album_variant_id = ov.id
                                         AND h.track_id = mf.track_id))
           ) AS found_elsewhere
    FROM gone g JOIN album_variants av ON av.id = g.vid
""")


def _set_aside(missing_ids: List[int], seen: Set[str], stats: Dict[str, Any]) -> List[int]:
    """A copy whose files are all missing is away, not gone: it is marked
    absent (nothing removed, nothing asked — a walk that sees one of its
    files marks it present again) and its files leave the removal. A copy
    found again elsewhere is marked absent too — nothing can read it while
    the question waits, or after a Keep — and stays in the removal, a copy
    to remove. Returns the files the removal is left with."""
    if not missing_ids:
        return missing_ids
    with get_db_context() as db:
        copies = db.execute(_GONE_COPIES_SQL, {"ids": missing_ids, "seen": list(seen)}).fetchall()
        away = [c.vid for c in copies if not c.found_elsewhere]
        newly = [c.vid for c in copies if not c.away]
        if newly:
            db.execute(text("UPDATE album_variants SET absent_since = now() "
                            "WHERE id = ANY(CAST(:v AS int[]))"), {"v": newly})
            # The queue moves a slot on a file of this copy to a present copy
            # of its track (playback.queue.rebind_origins), as when files go.
            db.execute(text("SELECT pg_notify('sautium_files_removed', '')"))
            # A track another copy here also holds (a compilation, a second
            # rip) is elected again by the rule: one with no analysis yet is
            # analysed from that copy while this one is away; one analysed
            # keeps its source (canon.identity.ANALYSIS_SOURCE_ORDER).
            for tid in db.execute(text("""
                SELECT DISTINCT mf.track_id FROM media_files mf
                WHERE mf.album_variant_id = ANY(CAST(:v AS int[]))
                  AND EXISTS (SELECT 1 FROM media_files o
                              WHERE o.track_id = mf.track_id
                                AND o.album_variant_id <> mf.album_variant_id)
            """), {"v": newly}).scalars():
                elect_analysis_source(db, tid)
        kept = set(db.execute(text(
            "SELECT id FROM media_files WHERE album_variant_id = ANY(CAST(:v AS int[]))"),
            {"v": away}).scalars()) if away else set()
    stats["away"] = sum(1 for c in copies if not c.away and not c.found_elsewhere)
    if newly:
        logger.info(f"Rescan set {len(newly)} cop(ies) aside as away: none of their files was found")
    return [i for i in missing_ids if i not in kept]


def set_aside_missing(scopes: Optional[Sequence[Tuple[str, bool]]], walked: Set[str],
                      unread: Sequence[str], gone: Sequence[str]) -> int:
    """After a walk that asks no question ("Scan for new", a watcher's
    folders): a copy none of whose files it found in the folders it walked
    is away — the Rescan's own rule (_set_aside), reversible, nothing
    removed and nothing asked; the Rescan's question and the Away row stay
    the only removals. A folder walked alone judges only its own files; a
    folder that is gone judges everything under it; what lies under a
    folder the walk could not read keeps its state. Returns how many copies
    were set aside."""
    if library_unreachable():
        return 0
    root = settings.library_db_root()
    unverified = {settings.translate_to_host_path(p) for p in unread}

    def judged(path: str) -> bool:
        folder = path.rpartition("/")[0]
        while folder:
            if folder in unverified:
                return False
            folder = folder.rpartition("/")[0]
        if scopes is None:
            return True
        for rel, recursive in scopes:
            base = f"{root}/{rel}" if rel else root
            if (recursive or rel in gone) and path.startswith(base + "/"):
                return True
            if path.rpartition("/")[0] == base:
                return True
        return False

    with get_db_context() as db:
        if scopes is None:
            rows = db.execute(text("SELECT id, file_path FROM media_files")).fetchall()
        else:
            prefixes = [(f"{root}/{rel}" if rel else root) + "/" for rel, _r in scopes]
            rows = db.execute(text("""SELECT id, file_path FROM media_files
                                       WHERE file_path ^@ ANY(CAST(:p AS text[]))"""),
                              {"p": prefixes}).fetchall()
    missing = [r.id for r in rows if r.file_path not in walked and judged(r.file_path)]
    stats = _prune_stats()
    _set_aside(missing, walked, stats)
    return stats["away"]


def plan_prune(
    progress_cb: Optional[callable] = None,
    subpath: Optional[str] = None,
    cancel_check: Optional[callable] = None,
    disk_paths: Optional[Set[str]] = None,
    unread: Sequence[str] = (),
) -> Dict[str, Any]:
    """What removing the DB records of files no longer on disk would delete.

    Removing them deletes rows the owner sees, which goes only at the owner's
    yes (CLAUDE.md), so the walk never deletes: it takes every step of the
    removal in a transaction it rolls back and returns the counts with the
    files it would remove (`file_ids`) — the plan a person says yes to, its
    counts read off the very statements that would delete. The yes is
    prune_missing_files(file_ids).

    Uses a single directory scan + set difference instead of per-file exists()
    calls, which is orders of magnitude faster on WSL2/network mounts.

    disk_paths lets a caller that has just walked the tree hand its result in
    (see LibraryScanner.scan_and_import) instead of paying for a second walk,
    with `unread`, the paths that walk could not read: whatever lies under
    them keeps its rows, whoever walked the tree.

    cancel_check is forwarded to find_audio_files so a Cancel tap takes
    effect during the slow disk-discovery phase.
    """
    stats = _prune_stats()
    stats["file_ids"] = []

    if disk_paths is None:
        if progress_cb:
            progress_cb("Discovering files on disk...")

        scanner = LibraryScanner()
        disk_files, _cues, unread = scanner.find_audio_files(subpath=subpath, cancel_check=cancel_check)
        if cancel_check and cancel_check():
            logger.info("Prune cancelled during discovery")
            return stats
        disk_paths = {settings.translate_to_host_path(str(fp.absolute())) for fp in disk_files}
        stats["back"] = _mark_present(disk_paths)

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
            # the walk covered <root>/<subpath> alone: rows elsewhere are not its to judge
            prefix = f"{settings.library_db_root()}/{subpath.replace(chr(92), '/').strip('/')}/"
            query = query.filter(MediaFile.file_path.startswith(prefix, autoescape=True))
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

    return _prune_files(_set_aside(missing_ids, disk_paths, stats), stats, dry_run=True,
                        progress_cb=progress_cb, cancel_check=cancel_check)


def plan_away_removal() -> Dict[str, Any]:
    """The owner's removal of the copies that are away: every file of every
    absent copy, planned as a Rescan's removal is (rolled back, its counts)
    and removed only at the yes — as the prune removed them before copies
    could be away: the files, the tracks only they held, the copy, the album
    and its own rows when nothing else holds it."""
    stats = _prune_stats()
    with get_db_context() as db:
        file_ids = list(db.execute(text("""
            SELECT mf.id FROM media_files mf
            JOIN album_variants av ON av.id = mf.album_variant_id
            WHERE av.absent_since IS NOT NULL ORDER BY mf.id""")).scalars())
    stats["checked"] = len(file_ids)
    return _prune_files(file_ids, stats, dry_run=True, progress_cb=None, cancel_check=None)


def prune_missing_files(file_ids: Sequence[int], counts: Dict[str, int],
                        progress_cb: Optional[callable] = None,
                        cancel_check: Optional[callable] = None) -> Dict[str, Any]:
    """Remove the DB records of a plan's files (plan_prune) — of them only
    those still missing: a file that came back since keeps its rows.

    The yes covers what the question named (`counts`, the plan's). When the
    removal would now take more tracks, albums or artists — the catalogue
    changed while the question waited (an HQPlayer library forgotten, a
    phantom slot dropped) — nothing is removed: the stats come back with
    `grew` and the new `file_ids`, a plan to ask again.

    Deletion order: MediaFile → Track → AlbumVariant → Album → Artist, each
    step scoped to the rows the removed files justified — the phantom layer
    is not this function's business. DB-level ON DELETE CASCADE handles child
    tables (embeddings, stats, etc)."""
    stats = _prune_stats()
    return _prune_files(_still_missing(file_ids, stats), stats, dry_run=False, bound=counts,
                        progress_cb=progress_cb, cancel_check=cancel_check)


def _still_missing(file_ids: Sequence[int], stats: Dict[str, Any]) -> List[int]:
    """Of a plan's files, those still in the catalog and still missing now,
    judged as the plan's walk judged them: from the music folder down, every
    folder of the path listed by its exact name as a folder (not a link to
    one, which the walk never enters) and the file by its exact name. A path
    outside the current root is missing; a case-only rename on a
    case-insensitive disk, or a folder swapped for a link, leaves the old
    spelling openable but not walked, so it is missing too. A folder that
    cannot be listed keeps what lies under it, as the walk keeps unread
    folders. The music folder is asked first: gone as a whole, nothing may
    go."""
    if library_unreachable():
        logger.error("Prune aborted at the yes: the music folder is unreachable")
        stats["unreachable"] = True
        return []
    with get_db_context() as db:
        rows = db.query(MediaFile.id, MediaFile.file_path).filter(
            MediaFile.id.in_(list(file_ids))).all()
    stats["checked"] = len(rows)
    root = settings.library_db_root() + "/"
    local_root = str(Path(settings.music_library_path).absolute())
    listings: Dict[Tuple[str, ...], Optional[Dict[str, str]]] = {}

    def listing(parts: Tuple[str, ...]) -> Optional[Dict[str, str]]:
        if parts not in listings:
            kinds: Optional[Dict[str, str]] = {}
            try:
                with os.scandir(os.path.join(local_root, *parts)) as it:
                    for entry in it:
                        kinds[entry.name] = ("dir" if entry.is_dir(follow_symlinks=False)
                                             else "file" if entry.is_file() else "other")
            except (FileNotFoundError, NotADirectoryError):
                kinds = {}
            except OSError as e:
                logger.warning(f"Prune at the yes keeps what {os.path.join(*parts or ('.',))} "
                               f"holds: {e}")
                kinds = None
            listings[parts] = kinds
        return listings[parts]

    def walked(path: str) -> Optional[bool]:
        if not path.startswith(root):
            return False
        *folders, name = path[len(root):].split("/")
        for depth in range(len(folders) + 1):
            kinds = listing(tuple(folders[:depth]))
            if kinds is None:
                return None
            want = folders[depth] if depth < len(folders) else name
            if kinds.get(want) != ("dir" if depth < len(folders) else "file"):
                return False
        return True

    missing = []
    for mf_id, path in rows:
        there = walked(path)
        if there is None:
            stats["kept_unread"] += 1
        elif not there:
            missing.append(mf_id)
    if len(missing) < len(file_ids):
        logger.info(f"Prune at the yes: {len(file_ids) - len(missing)} of the plan's "
                    f"{len(file_ids)} files are back, unread or already gone, and are left")
    return missing


def _prune_files(missing_ids: List[int], stats: Dict[str, Any], *, dry_run: bool,
                 progress_cb: Optional[callable], cancel_check: Optional[callable],
                 bound: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    if dry_run:
        stats["file_ids"] = missing_ids
    if not missing_ids:
        logger.info("Prune: no missing files found")
        return stats

    stats["pruned"] = len(missing_ids)
    with get_db_context() as db:
        logger.info(f"{'Planning the removal of' if dry_run else 'Pruning'} "
                    f"{len(missing_ids)} missing media files")

        if progress_cb and not dry_run:
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
        files_of = dict(db.execute(text("""
            SELECT album_variant_id, count(*) FROM media_files
            WHERE id = ANY(CAST(:ids AS int[])) GROUP BY album_variant_id
        """), {"ids": missing_ids}).fetchall())

        album_ids = [r[0] for r in db.execute(text("""
            SELECT DISTINCT album_id FROM album_variants
            WHERE id = ANY(CAST(:ids AS int[])) AND album_id IS NOT NULL
        """), {"ids": variant_ids})]

        # Artist links and credits cascade with the track/album rows, so the
        # candidates have to be read before the deletes, not after.
        artist_ids = [r[0] for r in db.execute(text("""
            SELECT artist_id FROM track_artists WHERE track_id = ANY(CAST(:t AS uuid[]))
            UNION
            SELECT artist_id FROM track_credits WHERE track_id = ANY(CAST(:t AS uuid[]))
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

        gone_variants = db.execute(text("""
            DELETE FROM album_variants av WHERE av.id = ANY(CAST(:ids AS int[]))
              AND NOT EXISTS (SELECT 1 FROM media_files mf WHERE mf.album_variant_id = av.id)
            RETURNING av.id, av.album_id
        """), {"ids": variant_ids}).fetchall()
        stats["orphan_variants"] = len(gone_variants)

        gone_albums = set(db.execute(text("""
            DELETE FROM albums a WHERE a.id = ANY(CAST(:ids AS uuid[]))
              AND NOT EXISTS (SELECT 1 FROM album_variants av WHERE av.album_id = a.id)
              AND NOT EXISTS (SELECT 1 FROM album_tracks at WHERE at.album_id = a.id)
            RETURNING a.id
        """), {"ids": [str(a) for a in album_ids]}).scalars())
        stats["orphan_albums"] = len(gone_albums)
        # A copy that goes while its album stays is a copy, not music: the
        # question names it apart from the files.
        copies = [vid for vid, album in gone_variants if album not in gone_albums]
        stats["copies"] = len(copies)
        stats["copy_files"] = sum(files_of.get(v, 0) for v in copies)

        r = db.execute(text("""
            DELETE FROM artists ar WHERE ar.id = ANY(CAST(:ids AS uuid[]))
              AND NOT EXISTS (SELECT 1 FROM track_artists ta WHERE ta.artist_id = ar.id)
              AND NOT EXISTS (SELECT 1 FROM album_artists aa WHERE aa.artist_id = ar.id)
              AND NOT EXISTS (SELECT 1 FROM track_credits tc WHERE tc.artist_id = ar.id)
        """), {"ids": [str(a) for a in artist_ids]})
        stats["orphan_artists"] = r.rowcount

        if dry_run:
            db.rollback()
            logger.info("Prune plan: %s", {k: v for k, v in stats.items() if k != "file_ids"})
            return stats
        if bound is not None and any(prune_counts(stats)[k] > bound[k] for k in _BOUND):
            db.rollback()
            stats["grew"], stats["file_ids"] = True, missing_ids
            logger.info("Prune at the yes would remove more than it asked about "
                        f"({prune_counts(stats)} > {bound}): nothing removed, asked again")
            return stats

        # A track whose analysis-source file was among the removed keeps its
        # other files with none of them elected: elect again, or no pending
        # query would ever see the track.
        still_held = [str(t) for t in db.execute(text("""
            SELECT DISTINCT track_id FROM media_files WHERE track_id = ANY(CAST(:ids AS uuid[]))
        """), {"ids": [str(t) for t in track_ids]}).scalars()]
        for tid in still_held:
            elect_analysis_source(db, tid)
        db.commit()

    # The elected file may be the analysed material at another path (a folder
    # the same Rescan found moved): its analysis follows it. The removal is
    # committed by now, so a failure here is the re-link's alone — the next
    # import or analysis pass looks again.
    def relink_progress(done, total):
        if progress_cb:
            progress_cb(f"Linking returned files to their analysis: {done}/{total}")
    try:
        stats["relinked"] = relink_returned_files(still_held, cancel_check, relink_progress)
    except Exception as e:
        logger.error(f"Re-link after the prune failed: {e}", exc_info=True)
    logger.info(f"Prune complete: {stats}")
    return stats
