"""Streaming-provider metadata search + phantom minting (Discovery supplement).

Search hits Deezer's PUBLIC metadata API (plain JSON, no auth — the closed BYO
module is only needed to STREAM; §1201 concerns decryption, not metadata).
Availability is still gated on the Deezer provider being installed: without it
a minted phantom would only have the YouTube fallback and the product story is
"you searched YOUR streaming service".

A tile the user clicks is MINTED as an ordinary phantom row with the same
deterministic UUID v5 identity the MB pipeline uses — a Deezer mint therefore
COLLAPSES onto the row an MB-dump phantom (or a later rip) would occupy; no
duplicate entities by construction. Every mint is recorded in streaming_mints:
provenance (explicit user intent) the phantom-canon discard pass must respect,
plus provider ids for future exact playback resolution.
"""
import logging
import time
from typing import Optional

import httpx

from db_pool import db_query_one, db_execute, get_conn
from transliterate import latinize
from uuid_utils import artist_uuid, album_uuid, track_uuid

logger = logging.getLogger(__name__)

_API = "https://api.deezer.com"
_TTL_SECONDS = 300.0          # absorb retypes without re-hitting the rate limit (~50 req/5s)
_MAX_TRACKLIST = 200
_cache: dict = {}


def available() -> bool:
    from streaming import service
    return any(p.manifest.id == "deezer" for p in service.providers_preferred())


def _get(path: str, params: Optional[dict] = None) -> dict:
    with httpx.Client(timeout=8.0) as client:
        resp = client.get(f"{_API}{path}", params=params or {})
        resp.raise_for_status()
        data = resp.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"deezer API error: {data['error']}")
    return data


def search(q: str, limit: int = 6) -> dict:
    """Provider rows for the Discovery streaming tail, deduped against the local
    DB via deterministic UUIDs — an entity we already have (owned OR phantom) is
    hidden, the local engine already surfaced it. A pleasant side effect: Deezer's
    empty namesake clones collapse onto the same UUID and vanish with it."""
    key = (q.lower(), limit)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _TTL_SECONDS:
        return hit[1]

    artists = []
    for r in _get("/search/artist", {"q": q, "limit": limit}).get("data", []):
        name = (r.get("name") or "").strip()
        if not name:
            continue
        if db_query_one("SELECT 1 AS x FROM artists WHERE id = %(id)s::uuid",
                        {"id": str(artist_uuid(name))}):
            continue
        artists.append({"provider": "deezer", "provider_id": str(r["id"]),
                        "name": name, "picture": r.get("picture_medium"),
                        "nb_fan": r.get("nb_fan") or 0})
    artists.sort(key=lambda a: -a["nb_fan"])   # fan count sinks empty clone profiles

    albums = []
    for r in _get("/search/album", {"q": q, "limit": limit}).get("data", []):
        title = (r.get("title") or "").strip()
        artist_name = ((r.get("artist") or {}).get("name") or "").strip()
        if not title or not artist_name:
            continue
        if db_query_one("SELECT 1 AS x FROM albums WHERE id = %(id)s::uuid",
                        {"id": str(album_uuid(title, artist_name))}):
            continue
        albums.append({"provider": "deezer", "provider_id": str(r["id"]),
                       "title": title, "artist": artist_name,
                       "cover": r.get("cover_medium")})

    out = {"artists": artists, "albums": albums}
    _cache[key] = (time.time(), out)
    if len(_cache) > 200:
        _cache.pop(next(iter(_cache)))
    return out


def _record_mint(provider_id: str, artist_id: Optional[str] = None,
                 album_id: Optional[str] = None) -> None:
    db_execute("""
        INSERT INTO streaming_mints (artist_id, album_id, provider, provider_id)
        VALUES (%(ar)s::uuid, %(al)s::uuid, 'deezer', %(pid)s)
        ON CONFLICT DO NOTHING
    """, {"ar": artist_id, "al": album_id, "pid": provider_id})


def _mint_artist_row(name: str, provider_id: str) -> str:
    aid = str(artist_uuid(name))
    db_execute("""
        INSERT INTO artists (id, name, name_latin)
        VALUES (%(id)s::uuid, %(n)s, %(nl)s)
        ON CONFLICT (id) DO NOTHING
    """, {"id": aid, "n": name, "nl": latinize(name)})
    _record_mint(provider_id, artist_id=aid)
    return aid


def mint_artist(provider_id: str) -> str:
    """Phantom artist from a Deezer artist tile. The page starts sparse; the
    existing enrichment pipelines (canon → missing-albums, Last.fm) fill it —
    streaming_mints keeps the row alive even when MB can't resolve it."""
    d = _get(f"/artist/{provider_id}")
    name = (d.get("name") or "").strip()
    if not name:
        raise ValueError("deezer artist payload has no name")
    return _mint_artist_row(name, provider_id)


def mint_album(provider_id: str) -> str:
    """Phantom album + artist + full tracklist from a Deezer album tile — the
    album page is immediately streamable. Same insert discipline as the MB
    phantom path: owned-guard upsert (an owned row can never be clobbered),
    slot upserts on (album, disc, position), NO media_files (the owned/phantom
    discriminator — a later rip collapses onto the same track_uuid rows)."""
    d = _get(f"/album/{provider_id}")
    title = (d.get("title") or "").strip()[:500]
    artist = d.get("artist") or {}
    artist_name = (artist.get("name") or "").strip()
    if not title or not artist_name:
        raise ValueError("deezer album payload incomplete")

    artist_id = _mint_artist_row(artist_name, str(artist.get("id") or f"album:{provider_id}"))

    album_id = str(album_uuid(title, artist_name))
    year = None
    if d.get("release_date"):
        try:
            year = int(str(d["release_date"])[:4])
        except ValueError:
            pass
    db_execute("""
        WITH up AS (
            INSERT INTO albums (id, title, title_latin, release_year, cover_url)
            VALUES (%(id)s::uuid, %(t)s, %(tl)s, %(y)s, %(c)s)
            ON CONFLICT (id) DO UPDATE SET
                cover_url = COALESCE(albums.cover_url, EXCLUDED.cover_url),
                release_year = COALESCE(albums.release_year, EXCLUDED.release_year),
                updated_at = now()
                WHERE NOT EXISTS (
                    SELECT 1 FROM album_variants av WHERE av.album_id = albums.id
                )
            RETURNING id
        )
        INSERT INTO album_artists (album_id, artist_id, role)
        SELECT id, %(ar)s::uuid, 'primary' FROM up
        ON CONFLICT DO NOTHING
    """, {"id": album_id, "t": title, "tl": latinize(title), "y": year,
          "c": d.get("cover_xl") or d.get("cover_big") or d.get("cover_medium"),
          "ar": artist_id})
    _record_mint(provider_id, album_id=album_id)

    tracks = list((d.get("tracks") or {}).get("data") or [])
    nb_tracks = d.get("nb_tracks") or len(tracks)
    while len(tracks) < min(nb_tracks, _MAX_TRACKLIST):
        page = _get(f"/album/{provider_id}/tracks", {"index": len(tracks)})
        data = page.get("data") or []
        if not data:
            break
        tracks.extend(data)

    track_rows, artist_rows, slot_rows = [], [], []
    for i, tr in enumerate(tracks[:_MAX_TRACKLIST], 1):
        t_title = (tr.get("title") or "").strip()[:500]
        if not t_title:
            continue
        tid = str(track_uuid(t_title, artist_name))
        track_rows.append((tid, t_title, latinize(t_title)))
        artist_rows.append((tid, artist_id))
        slot_rows.append((album_id, tid, tr.get("disk_number") or 1,
                          tr.get("track_position") or i,
                          (tr.get("duration") or 0) * 1000 or None))
    if slot_rows:
        from psycopg2.extras import execute_values
        with get_conn() as conn:
            with conn.cursor() as cur:
                execute_values(cur, """
                    INSERT INTO tracks (id, title, title_latin) VALUES %s
                    ON CONFLICT (id) DO NOTHING
                """, track_rows, template="(%s::uuid, %s, %s)")
                execute_values(cur, """
                    INSERT INTO track_artists (track_id, role, artist_id) VALUES %s
                    ON CONFLICT DO NOTHING
                """, artist_rows, template="(%s::uuid, 'primary', %s::uuid)")
                execute_values(cur, """
                    INSERT INTO album_tracks (album_id, track_id, disc, position, length_ms)
                    VALUES %s
                    ON CONFLICT (album_id, disc, position)
                    DO UPDATE SET track_id = EXCLUDED.track_id,
                                  length_ms = EXCLUDED.length_ms,
                                  updated_at = now()
                """, slot_rows, template="(%s::uuid, %s::uuid, %s, %s, %s)")
            conn.commit()
    logger.info("minted deezer album %s: %s — %s (%d tracks)",
                provider_id, artist_name, title, len(slot_rows))
    return album_id
