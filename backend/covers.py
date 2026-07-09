"""
Cover art ingestion (lazy on-demand).

Resolution order for a single media_file:
  1. embedded FLAC PICTURE type 3 (front cover) — preferred source
  2. external file in album directory (priority: cover > folder > front > ...)
  3. Last.fm cover URL (size=mega) for the album

Each source is hashed (BLAKE2b-256) for dedup, resized to 1024px WebP q=85 and
stored as a blob. Perceptual hash (imagehash.phash) is computed for future
near-duplicate detection (different encodings / re-rips of same art).

When a folder is being resolved and yields a cover, all media_files in that
folder get the same cover_id assigned in one pass — visiting one track of an
album warms covers for the whole album.

When all sources fail, every media_file in the folder is pinned to
SENTINEL_COVER_ID — a singleton row in `covers` whose UUID is
`00000000-0000-0000-0000-000000000000`. The HTTP endpoint sees the sentinel
and returns a 404 with a short Cache-Control, blocking further extraction
attempts but allowing manual retry after a re-scan / metadata fix.

Public entry points:
  resolve_cover_for_media_file(db, media_file_id) -> Optional[UUID]
  resolve_cover_for_folder(db, media_file_id) -> Optional[UUID]
  process_pending(db, media_file_id) -> Optional[UUID]   # + marks row processed
"""

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional, Tuple

import httpx
import imagehash
from mutagen import File as MutagenFile
from PIL import Image, UnidentifiedImageError
from sqlalchemy import text
from sqlalchemy.orm import Session

import api_cooldown
from config import settings
from uuid_utils import NAMESPACE


SENTINEL_COVER_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")

logger = logging.getLogger(__name__)

# Artist-photo rate-limit cooldown now lives in the persistent, cross-source
# `api_cooldown` module (source 'deezer') — armed here in resolve_artist_photo,
# honored by the endpoint in routers/covers.py, and it survives restarts.

# Album booklets scanned for print can reach ~50 MP — raise default bomb guard.
Image.MAX_IMAGE_PIXELS = 200_000_000

TARGET_MAX_DIM = 1024
WEBP_QUALITY = 85

# Filename priority (higher rank = earlier in tuple).
_NAME_PRIORITY: Tuple[str, ...] = ("cover", "folder", "front", "albumart", "album")

# Extension priority — prefer lossless masters since we re-encode to WebP anyway.
_EXT_PRIORITY = {
    ".tiff": 5, ".tif": 5,
    ".png": 4,
    ".webp": 3,
    ".jpg": 2, ".jpeg": 2,
    ".bmp": 1,
}


def _cover_uuid(content_hash: bytes) -> uuid.UUID:
    """Deterministic UUID v5 from content hash."""
    return uuid.uuid5(NAMESPACE, f"cover:{content_hash.hex()}")


def _phash_to_signed_int64(unsigned: int) -> int:
    """Reinterpret an unsigned 64-bit bit pattern as signed int64 for BIGINT storage.

    imagehash returns 0..2^64-1; PostgreSQL BIGINT is signed (-2^63..2^63-1).
    Bit pattern is preserved — equality and bit-level ops (xor / popcount) still work.
    """
    if unsigned >= (1 << 63):
        return unsigned - (1 << 64)
    return unsigned


def find_external_cover(album_dir: Path) -> Optional[Path]:
    """Return best-ranked cover file in the directory, or None.

    Name rank dominates extension rank: a `front.png` beats `cover.jpg` only if
    `cover` is absent. TIFF/PNG beats JPEG at equal name rank.
    """
    best: Optional[Path] = None
    best_score = -1
    try:
        entries = list(album_dir.iterdir())
    except (PermissionError, OSError, FileNotFoundError) as e:
        logger.debug(f"find_external_cover({album_dir}): {e}")
        return None

    for entry in entries:
        if not entry.is_file():
            continue
        ext = entry.suffix.lower()
        ext_rank = _EXT_PRIORITY.get(ext)
        if ext_rank is None:
            continue
        stem = entry.stem.lower()
        for name_idx, name in enumerate(_NAME_PRIORITY):
            if stem == name or stem.startswith(name):
                name_rank = len(_NAME_PRIORITY) - name_idx
                score = name_rank * 10 + ext_rank
                if score > best_score:
                    best_score = score
                    best = entry
                break
    return best


def extract_embedded_cover(audio_path: Path) -> Optional[bytes]:
    """Return raw bytes of an embedded front-cover image, or None.

    Format-agnostic via mutagen.File auto-detection. Supports:
      - FLAC PICTURE blocks       (type 3 preferred, else first)
      - MP3 ID3v2 APIC frames     (type 3 preferred, else first)
      - MP4 / M4A 'covr' atom     (first cover)

    Returns None when the file is unreadable, has no tags, or stores
    no embedded picture. Picture type 3 is "Cover (front)" per the
    ID3v2/FLAC spec; we fall back to whatever picture exists when the
    encoder didn't tag a type, which is common in older rips.
    """
    try:
        audio = MutagenFile(str(audio_path))
    except Exception as e:
        logger.debug(f"audio read failed {audio_path}: {e}")
        return None
    if audio is None:
        return None

    # FLAC (and any format exposing .pictures: also Vorbis comments
    # in Ogg containers when mutagen surfaces them this way).
    pics = getattr(audio, "pictures", None)
    if pics:
        for pic in pics:
            if getattr(pic, "type", None) == 3:
                return bytes(pic.data)
        return bytes(pics[0].data)

    tags = getattr(audio, "tags", None)
    if tags is None:
        return None

    # ID3v2 APIC frames (MP3, also WAV/AIFF when ID3-tagged).
    apic_frames = []
    try:
        for key in tags.keys():
            if isinstance(key, str) and key.startswith("APIC"):
                apic_frames.append(tags[key])
    except Exception:
        pass
    if apic_frames:
        for frame in apic_frames:
            if getattr(frame, "type", None) == 3:
                return bytes(frame.data)
        return bytes(apic_frames[0].data)

    # MP4 / M4A 'covr' atom.
    try:
        covers = tags.get("covr")
        if covers:
            return bytes(covers[0])
    except Exception:
        pass

    return None


def _encode_and_hash(data: bytes):
    """Decode source bytes → (webp_bytes, w, h, orig_w, orig_h, orig_format, phash).

    phash is computed from the original (resize-invariant anyway).
    Returns None if PIL cannot identify the format.
    """
    try:
        img = Image.open(BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError) as e:
        logger.debug(f"PIL decode failed: {e}")
        return None

    orig_w, orig_h = img.size
    orig_format = (img.format or "").lower()

    phash_int: Optional[int] = None
    try:
        phash_int = _phash_to_signed_int64(int(str(imagehash.phash(img)), 16))
    except Exception as e:
        logger.debug(f"phash failed: {e}")

    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")

    if max(orig_w, orig_h) > TARGET_MAX_DIM:
        img.thumbnail((TARGET_MAX_DIM, TARGET_MAX_DIM), Image.LANCZOS)

    buf = BytesIO()
    img.save(buf, "WEBP", quality=WEBP_QUALITY, method=6)
    webp_bytes = buf.getvalue()
    webp_w, webp_h = img.size

    return webp_bytes, webp_w, webp_h, orig_w, orig_h, orig_format, phash_int


def ingest_cover(
    db: Session,
    data: bytes,
    source_type: str,
    source_path: str,
    source_mtime: Optional[float] = None,
) -> Optional[uuid.UUID]:
    """Hash, dedup, encode + insert cover. Returns cover UUID or None on failure."""
    content_hash = hashlib.blake2b(data, digest_size=32).digest()

    existing = db.execute(
        text("SELECT id FROM covers WHERE content_hash = :h"),
        {"h": content_hash},
    ).first()
    if existing:
        return existing[0]

    encoded = _encode_and_hash(data)
    if encoded is None:
        return None
    webp_bytes, w, h, orig_w, orig_h, orig_format, phash_int = encoded

    cover_id = _cover_uuid(content_hash)
    mtime_dt = (
        datetime.fromtimestamp(source_mtime, tz=timezone.utc)
        if source_mtime is not None
        else None
    )

    db.execute(
        text(
            """
            INSERT INTO covers (
                id, content_hash, perceptual_hash,
                source_type, source_path, source_mtime,
                orig_width, orig_height, orig_format, orig_bytes,
                width, height, data, bytes
            ) VALUES (
                :id, :ch, :ph,
                :st, :sp, :smt,
                :ow, :oh, :of, :ob,
                :w, :h, :d, :b
            )
            ON CONFLICT (content_hash) DO NOTHING
            """
        ),
        {
            "id": cover_id,
            "ch": content_hash,
            "ph": phash_int,
            "st": source_type,
            "sp": source_path,
            "smt": mtime_dt,
            "ow": orig_w,
            "oh": orig_h,
            "of": orig_format or None,
            "ob": len(data),
            "w": w,
            "h": h,
            "d": webp_bytes,
            "b": len(webp_bytes),
        },
    )
    return cover_id


def resolve_cover_for_media_file(db: Session, media_file_id: int) -> Optional[uuid.UUID]:
    """Locate the cover for one media_file and ingest it. Returns cover_id or None."""
    row = db.execute(
        text("SELECT file_path FROM media_files WHERE id = :id"),
        {"id": media_file_id},
    ).first()
    if not row:
        return None
    db_path = row[0]
    local_path = Path(settings.translate_to_local_path(db_path))

    if not local_path.exists():
        logger.debug(f"media_file {media_file_id}: path missing {local_path}")
        return None

    # 1. Embedded front cover — format-agnostic (FLAC PICTURE, ID3v2
    #    APIC, MP4 'covr'). extract_embedded_cover returns None on
    #    formats without embedded art so we silently fall through.
    data = extract_embedded_cover(local_path)
    if data:
        suffix = local_path.suffix.lower().lstrip('.') or 'audio'
        return ingest_cover(
            db,
            data,
            source_type="embedded",
            source_path=f"{suffix}:{db_path}#0",
            source_mtime=local_path.stat().st_mtime,
        )

    # 2. External file in the album directory
    ext = find_external_cover(local_path.parent)
    if ext is None:
        return None
    try:
        data = ext.read_bytes()
    except (OSError, PermissionError) as e:
        logger.debug(f"read external cover failed {ext}: {e}")
        return None
    return ingest_cover(
        db,
        data,
        source_type="external",
        source_path=settings.translate_to_host_path(str(ext)),
        source_mtime=ext.stat().st_mtime,
    )


def process_pending(db: Session, media_file_id: int) -> Optional[uuid.UUID]:
    """Resolve cover for one pending media_file and mark it processed.

    Caller is responsible for the outer transaction (commit/rollback).
    """
    cover_id = resolve_cover_for_media_file(db, media_file_id)
    db.execute(
        text(
            """
            UPDATE media_files
            SET cover_id = :cid, cover_processed_at = now()
            WHERE id = :mid
            """
        ),
        {"cid": cover_id, "mid": media_file_id},
    )
    return cover_id


# -- Lazy on-demand resolution -----------------------------------------------

def _split_folder(file_path: str) -> tuple[str, str]:
    """Split a path into (parent_with_separator, basename), separator-agnostic.

    `os.path.dirname` is platform-bound: it can't parse Windows paths on a
    Linux container. We rfind both separators ourselves so a Windows-style
    file_path (`E:\\Music\\Album\\track.flac`) parses correctly inside the
    Linux backend. Returns the folder prefix WITH trailing separator.
    """
    idx = max(file_path.rfind('/'), file_path.rfind('\\'))
    if idx < 0:
        return ('', file_path)
    return (file_path[:idx + 1], file_path[idx + 1:])


def _siblings_in_folder(db: Session, file_path: str) -> list[tuple[int, str]]:
    """All media_files whose file_path is a direct child of the same folder."""
    folder_prefix, _ = _split_folder(file_path)
    if not folder_prefix:
        return []
    rows = db.execute(
        text("SELECT id, file_path FROM media_files WHERE file_path LIKE :p"),
        {"p": folder_prefix + "%"},
    ).all()
    out: list[tuple[int, str]] = []
    for mid, fp in rows:
        rest = fp[len(folder_prefix):]
        if '/' in rest or '\\' in rest:
            continue  # nested subfolder, not a direct sibling
        out.append((mid, fp))
    return out


def ingest_cover_from_url(
    db: Session,
    url: str,
    source_path: str = "",
) -> Optional[uuid.UUID]:
    """Fetch image bytes from URL and ingest. Returns cover_id or None."""
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.content
    except Exception as e:
        logger.warning(f"cover URL fetch failed {url}: {e}")
        return None
    if not data:
        return None
    return ingest_cover(
        db,
        data,
        source_type="external",
        source_path=source_path or url,
        source_mtime=None,
    )


def resolve_artist_photo(db: Session, artist_id: str) -> Optional[uuid.UUID]:
    """Resolve and cache an artist photo, returning the cover_id.

    Fast path is owned by the endpoint (it checks artists.photo_cover_id
    first). This runs only when no photo has been resolved yet. Resolves
    via Deezer's JSON API (the Last.fm HTML scrape that used to back this
    up was retired — it was the sole cause of the 406 IP-bans, and Deezer
    covers every artist it did); downloads the image and ingests it
    through the covers pipeline (BLAKE2 dedup + WebP encode + max-1024
    resize, AR preserved).

    Three outcomes:
      - photo found  → cache the cover_id.
      - Deezer definitively reports "no photo" → pin SENTINEL_COVER_ID
        so we don't re-fetch on every page load.
      - the fetch errored (rate-limit, timeout, 5xx) → leave
        photo_cover_id NULL and return None so a later request retries.
        Rate-limit errors additionally arm the global cooldown. Without
        this distinction one bad moment would permanently mark a real
        artist as photo-less.

    The endpoint serializes calls to this function behind a global
    throttle — see `routers/covers.py`. Do not call it in a tight loop.
    """
    from photo_fetch import TransientFetchError, RateLimitError
    from deezer_photos import fetch_deezer_photo_url

    row = db.execute(
        text("SELECT id, name, photo_cover_id FROM artists WHERE id = :id"),
        {"id": artist_id},
    ).first()
    if not row:
        return None

    a_id, name, existing = row[0], row[1], row[2]
    if existing is not None:
        return existing

    photo_url: Optional[str] = None
    any_source_errored = False
    try:
        photo_url = fetch_deezer_photo_url(name)
        api_cooldown.clear('deezer')   # a clean response clears any prior cooldown
    except RateLimitError as e:
        logger.warning(f"artist photo rate-limited via Deezer for {name!r}: {e}")
        api_cooldown.arm('deezer', str(e))
        any_source_errored = True
    except TransientFetchError as e:
        logger.warning(f"artist photo transient via Deezer for {name!r}: {e}")
        any_source_errored = True

    cover_id: Optional[uuid.UUID] = None
    if photo_url:
        cover_id = ingest_cover_from_url(db, photo_url, source_path=f"artist-photo:{name}")

    if cover_id is not None:
        final = cover_id
    elif any_source_errored:
        # Don't pin SENTINEL on a transient/rate-limit error — leave NULL
        # so a later request retries once the cooldown clears.
        return None
    else:
        final = SENTINEL_COVER_ID

    db.execute(
        text("UPDATE artists SET photo_cover_id = :c WHERE id = :a"),
        {"c": final, "a": a_id},
    )
    return final


def _lookup_album_artist(db: Session, media_file_id: int) -> Optional[tuple[str, str]]:
    """Return (artist_name, album_title) for a media_file, or None."""
    row = db.execute(
        text("""
            SELECT a.name, al.title
            FROM media_files mf
            JOIN tracks t ON t.id = mf.track_id
            JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
            JOIN artists a ON a.id = ta.artist_id
            JOIN album_variants av ON av.id = mf.album_variant_id
            JOIN albums al ON al.id = av.album_id
            WHERE mf.id = :id
            LIMIT 1
        """),
        {"id": media_file_id},
    ).first()
    if not row:
        return None
    return (row[0], row[1])


def resolve_cover_for_folder(db: Session, media_file_id: int) -> Optional[uuid.UUID]:
    """Resolve cover for a media_file's whole folder.

    Strategy:
      1. For every media_file in the same directory: run the standard
         per-file resolution (embedded → external) and persist cover_id.
      2. For files still without a cover, fall back to Last.fm
         (album.getInfo image at size=mega). One Last.fm call covers the
         whole folder — same album, same image.
      3. For files that still have nothing, pin SENTINEL_COVER_ID so we
         never re-scan them. The /api/covers endpoint maps the sentinel
         to a 404 with short Cache-Control so a manual rescan can clear
         the marker.

    Returns the resolved cover_id for the requested media_file (may be
    SENTINEL_COVER_ID).
    """
    src_row = db.execute(
        text("SELECT file_path FROM media_files WHERE id = :id"),
        {"id": media_file_id},
    ).first()
    if not src_row:
        return None

    siblings = _siblings_in_folder(db, src_row[0]) or [(media_file_id, src_row[0])]

    requested_cover: Optional[uuid.UUID] = None
    still_unresolved: list[int] = []

    for mid, _fp in siblings:
        cid = resolve_cover_for_media_file(db, mid)
        if cid is not None:
            db.execute(
                text("UPDATE media_files SET cover_id = :c WHERE id = :m"),
                {"c": cid, "m": mid},
            )
            if mid == media_file_id:
                requested_cover = cid
        else:
            still_unresolved.append(mid)

    # Fallback: Last.fm cover for the album (one fetch shared by siblings).
    if still_unresolved:
        meta = _lookup_album_artist(db, media_file_id)
        lastfm_cover_id: Optional[uuid.UUID] = None
        transient = False
        if meta and settings.lastfm_api_key:
            if api_cooldown.cooling_down('lastfm'):
                # Last.fm is rate-limiting us — treat as transient so the folder
                # retries after the cooldown clears instead of pinning SENTINEL.
                transient = True
            else:
                from lastfm import LastFmService
                from photo_fetch import TransientFetchError
                try:
                    url = LastFmService().get_album_cover_url(meta[0], meta[1])
                except TransientFetchError as e:
                    logger.warning(f"Last.fm album cover transient for {meta[0]} - {meta[1]}: {e}")
                    url = None
                    transient = True
            if url:
                lastfm_cover_id = ingest_cover_from_url(
                    db, url, source_path=f"lastfm:{meta[0]}/{meta[1]}",
                )

        if lastfm_cover_id is not None:
            for mid in still_unresolved:
                db.execute(
                    text("UPDATE media_files SET cover_id = :c WHERE id = :m"),
                    {"c": lastfm_cover_id, "m": mid},
                )
                if mid == media_file_id:
                    requested_cover = lastfm_cover_id
        elif transient:
            # Transient Last.fm failure — leave cover_id NULL so a later
            # request retries, instead of pinning SENTINEL permanently.
            # requested_cover stays None → endpoint serves a deferred 404.
            pass
        else:
            for mid in still_unresolved:
                db.execute(
                    text("UPDATE media_files SET cover_id = :c WHERE id = :m"),
                    {"c": SENTINEL_COVER_ID, "m": mid},
                )
                if mid == media_file_id:
                    requested_cover = SENTINEL_COVER_ID

    return requested_cover
