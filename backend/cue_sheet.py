"""CUE sheet parsing and resolution for image-based rips.

A classic EAC/XLD rip stores a whole disc as ONE audio image (.ape/.flac/
.wav/.mp3) plus a .cue sheet carrying per-track boundaries and titles. The
scanner imports such an image as N virtual media_files rows sharing one
file_path, sliced by [cue_start_seconds, cue_end_seconds).

Track span rule: track N runs from its INDEX 01 to the next track's INDEX 01
(the pregap between INDEX 00 and INDEX 01 rides at the previous track's tail),
which reproduces CD-linear gapless playback. The last track's end is None =
"to EOF". seconds = MM*60 + SS + FF/75 (MM:SS:FF, 75 frames per second).

Real-world sheets are sloppy; resolution is deliberately tolerant:
- FILE often names a .wav that was later converted (.ape on disk), with
  arbitrary casing — matched case-insensitively, then by stem with any
  supported audio extension, then "the only audio file in the folder".
- Encodings vary (EAC on Windows) — utf-8-sig, then cp1251, then cp1252.
- A multi-FILE sheet describes an already-split rip whose per-track files are
  self-describing — such cues are skipped entirely.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_QUOTED = re.compile(r'^"(.*)"$')
_TRACK_RE = re.compile(r"^TRACK\s+(\d+)\s+(\S+)", re.IGNORECASE)
_INDEX_RE = re.compile(r"^INDEX\s+(\d+)\s+(\d+):(\d{1,2}):(\d{1,2})", re.IGNORECASE)
_FILE_RE = re.compile(r'^FILE\s+(".*?"|\S+)', re.IGNORECASE)


@dataclass
class CueTrack:
    number: int
    title: Optional[str]
    performer: Optional[str]
    isrc: Optional[str]
    start_seconds: float
    end_seconds: Optional[float]  # None = to EOF (last track)


@dataclass
class CueSheet:
    cue_path: Path
    audio_path: Path              # resolved on-disk image, true casing
    title: Optional[str]          # album
    performer: Optional[str]      # album artist
    date: Optional[str]
    genre: Optional[str]
    catalog: Optional[str]
    tracks: List[CueTrack]


def read_cue_text(path: Path) -> str:
    """Decode a cue file: utf-8-sig strict, then cp1251, then cp1252.

    cp1251 before cp1252 because a Cyrillic sheet decodes "successfully" as
    cp1252 mojibake, while real cp1252 text almost never survives strict
    cp1251 either — and this library's non-ASCII rips are Cyrillic-first.
    cp1252 falls back to latin-1 semantics via errors="replace", so the chain
    never raises.
    """
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp1252", errors="replace")


def _unquote(value: str) -> str:
    m = _QUOTED.match(value)
    return m.group(1) if m else value


def _msf_to_seconds(mm: str, ss: str, ff: str) -> float:
    return int(mm) * 60 + int(ss) + int(ff) / 75.0


def parse_cue(text: str) -> Optional[Dict[str, Any]]:
    """Parse cue text into sheet fields + per-FILE track blocks.

    Returns ``{title, performer, genre, date, catalog,
    files: [(filename, [track dicts])]}`` preserving the multi-FILE structure,
    or None when the sheet yields no audio tracks. Track dicts carry
    ``{number, title, performer, isrc, indexes: {nn: seconds}}``. Non-AUDIO
    (data) tracks are dropped. TITLE/PERFORMER/ISRC bind to the current TRACK
    when inside one, else to the sheet.
    """
    sheet: Dict[str, Any] = {"title": None, "performer": None, "genre": None,
                             "date": None, "catalog": None, "files": []}
    cur_file: Optional[Tuple[str, List[dict]]] = None
    cur_track: Optional[dict] = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        upper = line.upper()

        if upper.startswith("REM "):
            rest = line[4:].strip()
            rest_u = rest.upper()
            if rest_u.startswith("GENRE "):
                sheet["genre"] = _unquote(rest[6:].strip()) or None
            elif rest_u.startswith("DATE "):
                sheet["date"] = _unquote(rest[5:].strip()) or None
            continue

        if upper.startswith("CATALOG "):
            sheet["catalog"] = _unquote(line[8:].strip()) or None
            continue

        m = _FILE_RE.match(line)
        if m:
            cur_file = (_unquote(m.group(1)), [])
            sheet["files"].append(cur_file)
            cur_track = None
            continue

        m = _TRACK_RE.match(line)
        if m:
            if cur_file is None:
                return None  # TRACK before any FILE — malformed
            if m.group(2).upper() == "AUDIO":
                cur_track = {"number": int(m.group(1)), "title": None,
                             "performer": None, "isrc": None, "indexes": {}}
                cur_file[1].append(cur_track)
            else:
                cur_track = None  # data track: swallow its lines
            continue

        m = _INDEX_RE.match(line)
        if m:
            if cur_track is not None:
                cur_track["indexes"][int(m.group(1))] = _msf_to_seconds(
                    m.group(2), m.group(3), m.group(4))
            continue

        if upper.startswith("TITLE "):
            value = _unquote(line[6:].strip()) or None
            if cur_track is not None:
                cur_track["title"] = value
            else:
                sheet["title"] = value
            continue

        if upper.startswith("PERFORMER "):
            value = _unquote(line[10:].strip()) or None
            if cur_track is not None:
                cur_track["performer"] = value
            else:
                sheet["performer"] = value
            continue

        if upper.startswith("ISRC ") and cur_track is not None:
            cur_track["isrc"] = _unquote(line[5:].strip()) or None
            continue
        # FLAGS / PREGAP / POSTGAP / SONGWRITER / unknown lines: ignored

    sheet["files"] = [f for f in sheet["files"] if f[1]]
    if not sheet["files"]:
        return None
    return sheet


def _resolve_audio_file(cue_path: Path, referenced: str,
                        audio_extensions: Set[str]) -> Optional[Path]:
    """Find the on-disk image the sheet's FILE line means, tolerating the
    usual defects. Matches against the real directory listing so the returned
    Path carries true on-disk casing (must equal the scanner walk's spelling).
    """
    folder = cue_path.parent
    try:
        entries = [e for e in folder.iterdir() if e.is_file()]
    except OSError as e:
        logger.warning("cue %s: cannot list folder: %s", cue_path, e)
        return None

    ref_name = Path(referenced).name  # drop any path component the ripper left
    by_lower = {e.name.lower(): e for e in entries}
    hit = by_lower.get(ref_name.lower())
    if hit is not None:
        return hit

    stem = Path(ref_name).stem.lower()
    stem_hits = sorted(
        (e for e in entries
         if e.stem.lower() == stem and e.suffix.lower() in audio_extensions),
        key=lambda e: e.name)
    if stem_hits:
        if len(stem_hits) > 1:
            logger.warning("cue %s: several images share stem %r, using %s",
                           cue_path, stem, stem_hits[0].name)
        return stem_hits[0]

    audio_entries = [e for e in entries if e.suffix.lower() in audio_extensions]
    if len(audio_entries) == 1:
        return audio_entries[0]
    return None


def resolve_cue(cue_path: Path, audio_extensions: Set[str]) -> Optional[CueSheet]:
    """Parse + validate one cue and resolve its image. None = ignore this cue
    (reason logged); a governed image is all-or-nothing — a sheet with any
    unreliable boundary is rejected whole rather than half-applied.
    """
    try:
        parsed = parse_cue(read_cue_text(cue_path))
    except OSError as e:
        logger.warning("cue %s: unreadable: %s", cue_path, e)
        return None
    if parsed is None:
        logger.warning("cue %s: no audio tracks, ignoring", cue_path)
        return None

    if len(parsed["files"]) > 1:
        logger.debug("cue %s: %d FILE entries (split-file rip, files "
                     "self-describing) — ignoring", cue_path, len(parsed["files"]))
        return None

    referenced, raw_tracks = parsed["files"][0]
    audio_path = _resolve_audio_file(cue_path, referenced, audio_extensions)
    if audio_path is None:
        logger.warning("cue %s: referenced file %r not found in folder, ignoring",
                       cue_path, referenced)
        return None

    starts: List[float] = []
    for t in raw_tracks:
        start = t["indexes"].get(1, t["indexes"].get(0))
        if start is None:
            logger.warning("cue %s: track %d has no INDEX, ignoring sheet",
                           cue_path, t["number"])
            return None
        starts.append(start)
    if any(b <= a for a, b in zip(starts, starts[1:])):
        logger.warning("cue %s: track starts not strictly increasing, ignoring",
                       cue_path)
        return None

    tracks = [
        CueTrack(number=t["number"], title=t["title"], performer=t["performer"],
                 isrc=t["isrc"], start_seconds=start,
                 end_seconds=starts[i + 1] if i + 1 < len(starts) else None)
        for i, (t, start) in enumerate(zip(raw_tracks, starts))
    ]
    return CueSheet(cue_path=cue_path, audio_path=audio_path,
                    title=parsed["title"], performer=parsed["performer"],
                    date=parsed["date"], genre=parsed["genre"],
                    catalog=parsed["catalog"], tracks=tracks)


def build_cue_map(cue_paths: List[Path],
                  audio_extensions: Set[str]) -> Dict[str, "CueSheet"]:
    """{str(image path as walked) -> CueSheet} for every usable cue.
    Two cues claiming one image: first by cue name wins."""
    cue_map: Dict[str, CueSheet] = {}
    for cue_path in sorted(cue_paths):
        sheet = resolve_cue(cue_path, audio_extensions)
        if sheet is None:
            continue
        key = str(sheet.audio_path)
        if key in cue_map:
            logger.warning("cue %s: image already governed by %s, ignoring",
                           cue_path, cue_map[key].cue_path.name)
            continue
        cue_map[key] = sheet
        logger.info("cue %s governs %s (%d tracks)",
                    cue_path.name, sheet.audio_path.name, len(sheet.tracks))
    return cue_map


def synthesize_metadata(image_md: Dict[str, Any], sheet: CueSheet,
                        track: CueTrack,
                        image_length_seconds: Optional[float]) -> Dict[str, Any]:
    """Per-track metadata dict for one cue slice, shaped exactly like
    LibraryScanner.extract_metadata output: the image's technical props ride
    through, tag fields come cue-first with image-tag fallback."""
    md = dict(image_md)
    title = track.title or f"Track {track.number:02d}"
    artist = track.performer or sheet.performer or image_md.get("artist")
    md["title"] = title
    md["artist"] = artist
    md["album"] = sheet.title or image_md.get("album")
    md["album_artist"] = sheet.performer or image_md.get("album_artist") or artist
    md["genre"] = sheet.genre or image_md.get("genre")
    date = sheet.date or image_md.get("date")
    md["date"] = date
    year_match = re.search(r"\d{4}", str(date)) if date else None
    md["release_year"] = int(year_match.group()) if year_match else None
    md["track_number"] = track.number
    md["disc_number"] = 1
    md["isrc"] = track.isrc
    if sheet.catalog and any(c != "0" for c in sheet.catalog if c.isdigit()):
        md["catalog_number"] = sheet.catalog

    end = track.end_seconds if track.end_seconds is not None else image_length_seconds
    duration = round(float(end) - track.start_seconds, 2) if end is not None else None
    md["duration_seconds"] = duration if duration is None or duration > 0 else None
    md["cue_start_seconds"] = track.start_seconds
    md["cue_end_seconds"] = track.end_seconds
    return md
