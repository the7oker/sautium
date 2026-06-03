"""New-album discovery for local artists (Phantom Discovery, Phase 1).

Fetches a *local* artist's full Deezer discography, drops releases the
user already owns, collapses reissues/editions of the same album, and
persists the rest as **phantom album rows** — `albums` + `album_artists`
with an external `cover_url` and NO `album_variants`/`media_files`. The
artist screen surfaces them in a "New albums" shelf.

Two callers share `sync_artist_discography`:
  - the background enrichment step (monthly per artist), and
  - the fetch-on-view endpoint (daily gate on `artists.last_album_sync`).

Both go through `db_pool` (own connection per call) so neither needs to
thread a session in. Deezer rate-limits arm the shared cooldown in
`covers.py` so the photo and discography consumers back off together.

Matching is heuristic, by design (Deezer-only first iteration). The
deterministic `album_uuid` can't be the match key: Deezer titles and
local tags differ in articles, punctuation and edition suffixes, so the
same album yields different UUIDs on each side. `release_match_key`
canonicalises both sides for the own-check and the reissue-collapse;
the stored UUID stays `album_uuid(title, artist)` so a later local rip
can still collapse onto it.
"""

import logging
import re
import unicodedata
from typing import Dict, List, Optional

import covers
import deezer_discography
from db_pool import db_execute, db_query, db_query_one
from lastfm_photos import RateLimitError, TransientFetchError
from uuid_utils import album_uuid

logger = logging.getLogger(__name__)

# Deezer record_type values we treat as albums for the "new albums"
# shelf. Singles/EPs/compilations add too much noise in Phase 1.
_ALBUM_RECORD_TYPES = {"album"}

# Edition/reissue markers — same album, different packaging; stripped so
# variants collapse. Deliberately EXCLUDES 'live', 'remix', 'acoustic',
# 'demo', 'instrumental' — those are genuinely distinct releases and must
# survive as separate albums.
_EDITION_WORDS = (
    "deluxe", "remaster", "remastered", "anniversary", "expanded",
    "super deluxe", "bonus", "reissue", "collector", "collectors",
    "edition", "mono", "stereo", "redux",
)
_EDITION_BRACKET_RE = re.compile(
    r"\s*[\(\[][^\)\]]*\b(?:" + "|".join(_EDITION_WORDS) + r")\b[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
_EDITION_SUFFIX_RE = re.compile(
    r"\s*[-–—:]\s*[^-–—:]*\b(?:" + "|".join(_EDITION_WORDS) + r")\b.*$",
    re.IGNORECASE,
)
_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+")
_NONALNUM_RE = re.compile(r"[^a-z0-9]+")
# Multi-disc rip suffixes — "disc 1", "(Disc 2)", "cd1", "CD 2". A per-disc
# directory split is the same album: without this, "Catch a Fire … disc 1"
# never match-keys onto MB's "Catch a Fire" and overlap-verification silently
# misses every multi-disc release. 'disc one' (word) is rarer; digits only.
_DISC_RE = re.compile(
    r"\s*[\(\[]?\s*\b(?:disc|disk|cd)\s*\.?\s*\d+\s*[\)\]]?",
    re.IGNORECASE,
)


def release_match_key(title: str) -> str:
    """Canonical comparison key for collapsing reissues and for matching
    a Deezer release against an owned album.

    lowercase + NFC → drop bracketed/trailing edition markers → drop multi-disc
    suffixes → strip a leading article → collapse punctuation to spaces.
    'Live'/'remix'/etc. are not edition markers, so they survive and stay
    distinct.
    """
    s = unicodedata.normalize("NFC", (title or "").strip().lower())
    s = _EDITION_BRACKET_RE.sub("", s)
    s = _EDITION_SUFFIX_RE.sub("", s)
    s = _DISC_RE.sub(" ", s)
    # "&" vs "and": dirty tags use "Hunting High & Low", MB stores "… and …".
    # Normalise to the spelled-out form on both sides so they overlap-match.
    s = s.replace("&", " and ")
    s = _ARTICLE_RE.sub("", s)
    s = _NONALNUM_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _earlier(a: dict, b: dict) -> bool:
    """True if release `a` is older than `b` (None year sorts last)."""
    return (a.get("year") or 99999) < (b.get("year") or 99999)


def _stamp_sync(artist_id: str) -> None:
    db_execute(
        "UPDATE artists SET last_album_sync = now() WHERE id = %(id)s::uuid",
        {"id": artist_id},
    )


def _upsert_phantom_album(artist_id: str, artist_name: str, rel: dict) -> bool:
    """Persist one release as a phantom album + artist link. Returns True
    if a new album row was created (vs refreshing an existing phantom).

    The ON CONFLICT guard (`WHERE NOT EXISTS album_variants`) makes the
    upsert a no-op on an owned album, so an owned record's data can never
    be clobbered even if a UUID somehow collides.
    """
    aid = str(album_uuid(rel["title"], artist_name))
    res = db_execute("""
        WITH up AS (
            INSERT INTO albums (id, title, release_year, cover_url)
            VALUES (%(id)s::uuid, %(title)s, %(year)s, %(cover)s)
            ON CONFLICT (id) DO UPDATE
                SET cover_url = EXCLUDED.cover_url,
                    release_year = COALESCE(albums.release_year, EXCLUDED.release_year),
                    updated_at = now()
                WHERE NOT EXISTS (
                    SELECT 1 FROM album_variants av WHERE av.album_id = albums.id
                )
            RETURNING id, (xmax = 0) AS inserted
        ),
        link AS (
            INSERT INTO album_artists (album_id, artist_id, role)
            SELECT id, %(ar)s::uuid, 'primary' FROM up
            ON CONFLICT DO NOTHING
        )
        SELECT inserted FROM up
    """, {
        "id": aid,
        "title": rel["title"],
        "year": rel["year"],
        "cover": rel["cover_url"],
        "ar": artist_id,
    })
    return bool(res and res.get("inserted"))


def sync_artist_discography(artist_id, artist_name: str) -> Dict[str, int]:
    """Fetch `artist_name`'s Deezer discography and persist the albums the
    user doesn't own as phantom rows. Idempotent; stamps `last_album_sync`.

    Returns a stats dict: ``{status, found, new, skipped_owned}``.
    `status` is success / not_found (artist not on Deezer) / rate_limited
    / transient / error.
    """
    artist_id = str(artist_id)
    stats = {"status": "success", "found": 0, "new": 0, "skipped_owned": 0}

    row = db_query_one(
        "SELECT deezer_id FROM artists WHERE id = %(id)s::uuid",
        {"id": artist_id},
    )
    if row is None:
        return {"status": "error", "found": 0, "new": 0, "skipped_owned": 0}
    deezer_id = row.get("deezer_id")

    try:
        if not deezer_id:
            deezer_id = deezer_discography.resolve_deezer_artist_id(artist_name)
            if deezer_id:
                db_execute(
                    "UPDATE artists SET deezer_id = %(d)s WHERE id = %(id)s::uuid",
                    {"d": deezer_id, "id": artist_id},
                )
        if not deezer_id:
            # Not on Deezer — stamp so the gate doesn't retry every batch.
            _stamp_sync(artist_id)
            stats["status"] = "not_found"
            return stats
        releases = deezer_discography.fetch_artist_albums(deezer_id)
    except RateLimitError as e:
        covers.note_photo_rate_limit()
        logger.warning(f"Deezer rate-limited on discography for {artist_name}: {e}")
        stats["status"] = "rate_limited"
        return stats
    except TransientFetchError as e:
        logger.info(f"Deezer transient failure on discography for {artist_name}: {e}")
        stats["status"] = "transient"
        return stats

    owned = db_query("""
        SELECT DISTINCT al.title
        FROM albums al
        JOIN album_variants av ON av.album_id = al.id
        JOIN media_files mf ON mf.album_variant_id = av.id
        JOIN tracks t ON t.id = mf.track_id
        JOIN track_artists ta ON ta.track_id = t.id
        WHERE ta.artist_id = %(id)s::uuid
    """, {"id": artist_id})
    owned_keys = {release_match_key(r["title"]) for r in owned}

    # Collapse reissues by match-key (earliest year is the canonical
    # release; carry a cover from whichever variant has one), drop
    # non-albums and anything already owned.
    best: Dict[str, dict] = {}
    for rel in releases:
        if rel["record_type"] not in _ALBUM_RECORD_TYPES:
            continue
        key = release_match_key(rel["title"])
        if not key:
            continue
        if key in owned_keys:
            stats["skipped_owned"] += 1
            continue
        cur = best.get(key)
        if cur is None:
            best[key] = rel
        elif _earlier(rel, cur):
            if not rel.get("cover_url"):
                rel["cover_url"] = cur.get("cover_url")
            best[key] = rel
        elif not cur.get("cover_url") and rel.get("cover_url"):
            cur["cover_url"] = rel["cover_url"]

    stats["found"] = len(best)
    for rel in best.values():
        if _upsert_phantom_album(artist_id, artist_name, rel):
            stats["new"] += 1

    _stamp_sync(artist_id)
    return stats


def fetch_new_albums(artist_id) -> List[dict]:
    """The phantom albums for an artist — those linked via `album_artists`
    with no local files — newest first. Tile shape for the artist screen.
    """
    return db_query("""
        SELECT al.id::text AS id,
               al.title,
               al.release_year AS year,
               al.cover_url
        FROM albums al
        JOIN album_artists aa ON aa.album_id = al.id
        WHERE aa.artist_id = %(id)s::uuid
          AND NOT EXISTS (
              SELECT 1 FROM album_variants av WHERE av.album_id = al.id
          )
        ORDER BY al.release_year DESC NULLS LAST, al.title
    """, {"id": str(artist_id)})
