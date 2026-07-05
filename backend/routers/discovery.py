"""
Discovery screen endpoints.

ONE search endpoint backed by the discovery engine (discovery_engine.build).
The frontend fires it once per target block (artist / album / track) in
parallel; the engine composes the SAME matched set for every call — text
sources OR within the tool, filter chips AND across tools — and the target
decides the grain: the atom entity ranks by relevance, higher entities
aggregate (AVG of matched-atom scores + own-level identity matches).

A target below the atom (e.g. tracks for a Gender-only browse) is
structurally absent — the endpoint answers `{"results": []}` and the block
hides. Cold vector models are gracefully skipped (and kick-loaded) inside
the engine, so a search never blocks on a model load; `warming: true`
tells the client the semantic channels weren't all live yet.
"""

import logging
import random
from typing import Any, Optional
from fastapi import APIRouter, Body, HTTPException, Query
from sqlalchemy import text

import model_cache
from database import get_db_context
from db_pool import db_query

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/discovery", tags=["discovery"])


# -- Shuffle (Step 1.5a, kept) ------------------------------------------------

@router.get("/shuffle")
def shuffle_albums(
    limit: int = Query(14, ge=1, le=48),
    seed: int | None = Query(None),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Random album sample for the Discovery shuffle mosaic.

    Pagination uses a per-session seed and an offset so successive
    pages stay consistent with the first one (a fresh ORDER BY RANDOM()
    on each request would reshuffle and duplicate tiles the user has
    already scrolled past). The seed is generated server-side on the
    first call and returned in next_cursor; clients echo it back for
    subsequent pages.
    """

    if seed is None:
        seed = random.randint(0, 2**31 - 1)

    # md5(album_id || seed) is a deterministic pseudo-random ordering
    # over the album set: cheap (no plpgsql call, no setseed transaction
    # state), monotonic for a fixed seed, and uniformly distributed for
    # UUID inputs. Postgres can't index this expression usefully, but a
    # 3.5k-row seq scan + sort costs <50ms and avoids the seed-bleed
    # issues setseed has with pooled connections.
    albums = db_query("""
        SELECT al.id::text AS id,
               al.title,
               al.release_year AS year,
               (SELECT a.name
                FROM artists a
                JOIN track_artists ta ON ta.artist_id = a.id AND ta.role = 'primary'
                JOIN tracks t ON t.id = ta.track_id
                JOIN media_files mf2 ON mf2.track_id = t.id
                JOIN album_variants av2 ON av2.id = mf2.album_variant_id
                WHERE av2.album_id = al.id
                GROUP BY a.id, a.name
                ORDER BY COUNT(*) DESC
                LIMIT 1) AS artist,
               (SELECT mf3.cover_id::text
                FROM media_files mf3
                JOIN album_variants av3 ON av3.id = mf3.album_variant_id
                WHERE av3.album_id = al.id AND mf3.cover_id IS NOT NULL
                LIMIT 1) AS cover_id,
               (SELECT mf4.id
                FROM media_files mf4
                JOIN album_variants av4 ON av4.id = mf4.album_variant_id
                WHERE av4.album_id = al.id
                ORDER BY mf4.disc_number, mf4.track_number
                LIMIT 1) AS media_file_id
        FROM albums al
        WHERE EXISTS (
            SELECT 1 FROM album_variants av
            JOIN media_files mf ON mf.album_variant_id = av.id
            WHERE av.album_id = al.id
        )
        ORDER BY md5(al.id::text || %(seed)s::text)
        OFFSET %(offset)s
        LIMIT %(limit)s
    """, {"limit": limit, "seed": seed, "offset": offset})

    next_cursor = None
    if len(albums) == limit:
        next_cursor = {"seed": seed, "offset": offset + limit}

    return {"albums": albums, "next_cursor": next_cursor}


# -- Unified search -----------------------------------------------------------

# Loader callables mirror the per-key factories in search.py — they
# must produce instances compatible with the corresponding cache key
# so a `kick_load`-warmed entry is interchangeable with one created
# lazily by `model_cache.get_model` inside the search functions.
# discovery_engine._kick_model dispatches to these.

def _enrichment_loader():
    from enrichment_embeddings import _BaseEnrichmentGenerator
    gen = _BaseEnrichmentGenerator()
    gen.load_model()
    return gen


def _lyrics_loader():
    from lyrics_embeddings import LyricsEmbeddingGenerator
    gen = LyricsEmbeddingGenerator()
    gen.load_model()
    return gen


def _clap_loader():
    from embeddings import AudioEmbeddingGenerator
    gen = AudioEmbeddingGenerator()
    gen.load_model()
    return gen


def _engine_filters(bpm_min, bpm_max, key, mode, vocalist, gender,
                    danceable, energy, instruments, genres=()) -> dict:
    """Map the Discovery filter chips to engine tool values."""
    a: dict = {}
    if genres:
        a["genre"] = list(genres)
    if bpm_min is not None or bpm_max is not None:
        a["bpm"] = (bpm_min if bpm_min is not None else 0.0,
                    bpm_max if bpm_max is not None else 9999.0)
    if key:
        a["key"] = key
    if mode and mode != "any":
        a["mode"] = mode
    if vocalist and vocalist != "any":
        a["vocalist"] = vocalist
    if gender and gender != "any":
        a["gender"] = gender
    if danceable == "yes":
        a["danceable"] = True
    if energy in ("low", "mid", "high"):
        a["energy"] = energy
    if instruments:
        a["instruments"] = instruments
    return a


def _artist_results(rows) -> list:
    return [{
        "artist_id": str(r.id),
        "artist": r.name,
        "similarity": round(float(r.score), 4) if r.score is not None else None,
        "gender": r.gender,
        "is_vocalist": r.is_vocalist,
        "is_owned": bool(r.is_owned),
        "cover_id": r.cover_id,
        "media_file_id": r.media_file_id,
    } for r in rows]


def _album_results(rows) -> list:
    return [{
        "album_id": str(r.id),
        "album": r.name,
        "artist": r.artist,
        "year": r.year,
        "cover_id": r.cover_id,
        "cover_url": r.cover_url,
        "media_file_id": r.media_file_id,
        "is_owned": bool(r.is_owned),
        "similarity": round(float(r.score), 4) if r.score is not None else None,
    } for r in rows]


def _track_results(rows) -> list:
    return [{
        "id": r.media_file_id,          # owned playback id (None for a phantom track)
        "track_id": str(r.id),
        "title": r.name,
        "artist": r.artist,
        "album": r.album,
        "year": r.year,
        "duration_seconds": float(r.duration_seconds) if r.duration_seconds else None,
        "cover_id": r.cover_id,
        "cover_url": r.cover_url,       # phantom rows: album art (no media_files)
        "is_owned": bool(r.is_owned),
        "similarity": round(float(r.score), 4) if r.score is not None else None,
    } for r in rows]


def _genre_results(rows) -> list:
    return [{
        "genre_id": str(r.id),
        "genre": r.name,
        "album_count": r.album_count,
        "is_owned": bool(r.is_owned),
        "similarity": round(float(r.score), 4) if r.score is not None else None,
    } for r in rows]


_SHAPERS = {"artist": _artist_results, "album": _album_results,
            "track": _track_results, "genre": _genre_results}


@router.get("/streaming-search")
def streaming_search(q: str = Query(..., min_length=2, max_length=100),
                     limit: int = Query(6, ge=1, le=12)):
    """Streaming-provider supplement for a text search (no advanced filters —
    the provider can't honour our gates). Rows are deduped against the local DB;
    a tile the user clicks gets minted via /streaming-mint. Provider errors are
    the tail's problem, not the search's — the endpoint answers empty."""
    from streaming import search as provider_search
    if not provider_search.available():
        return {"available": False, "albums": []}
    try:
        res = provider_search.search(q.strip(), limit=limit)
    except Exception as e:
        logger.warning("streaming search failed: %s", e)
        return {"available": True, "albums": []}
    return {"available": True, **res}


@router.post("/streaming-mint")
def streaming_mint(body: dict = Body(...)):
    """Mint a clicked streaming album tile as a phantom (artist + album + full
    tracklist) and answer the local UUID to navigate to. Deterministic identity:
    re-minting is a no-op, an existing MB phantom is reused."""
    from streaming import search as provider_search
    provider_id = str(body.get("provider_id") or "")
    if body.get("type") != "album" or not provider_id:
        raise HTTPException(status_code=422, detail="type must be album with provider_id")
    try:
        return {"album_id": provider_search.mint_album(provider_id)}
    except Exception as e:
        logger.error("streaming mint failed (album %s): %s", provider_id, e)
        raise HTTPException(status_code=502, detail=f"mint failed: {e}")


@router.get("/instrument-options")
def instrument_options(q: str = Query("", max_length=50), limit: int = Query(8, ge=1, le=20)):
    """Instrument labels present in the corpus (raw AST/PaSST tags) matching a
    substring — the Instruments filter typeahead behind the 10 broad chips. The
    engine's expand passes an unknown label through verbatim, so any suggested
    tag gates correctly. Counts honour the tagger's threshold so a label only
    suggests where the gate would actually match."""
    from ensemble_instruments import DEFAULT_THRESHOLD
    rows = db_query("""
        SELECT e.k AS name, COUNT(*) AS track_count
        FROM audio_features af, LATERAL jsonb_each_text(af.instruments) AS e(k, v)
        WHERE (e.v)::float >= %(thr)s AND e.k ILIKE %(pat)s
        GROUP BY e.k ORDER BY COUNT(*) DESC
        LIMIT %(limit)s
    """, {"thr": DEFAULT_THRESHOLD, "pat": f"%{q}%", "limit": limit})
    return {"instruments": rows}


@router.get("/search")
def discovery_search(
    target: str = Query(..., pattern="^(artist|album|track|genre)$"),
    q: Optional[str] = Query(None),
    sound: Optional[str] = Query(None),   # semantic-only channels (MCP/AI-chat):
    lyrics: Optional[str] = Query(None),  # CLAP text→audio / lyrics vectors, no lexical arm
    limit: int = Query(10, ge=1, le=30),
    bpm_min: Optional[float] = Query(None),
    bpm_max: Optional[float] = Query(None),
    key: Optional[str] = Query(None),
    mode: Optional[str] = Query(None),
    vocalist: Optional[str] = Query(None),
    gender: Optional[str] = Query(None),
    danceable: Optional[str] = Query(None),
    energy: Optional[str] = Query(None),
    instruments: list[str] = Query(default_factory=list),
    genres: list[str] = Query(default_factory=list),
    seed_track_id: Optional[str] = Query(None),
    artists: list[str] = Query(default_factory=list),
    corpus: str = Query("all"),
):
    """One composite query → one target block. Text + chips build the engine's
    `active` dict; the engine decides whether this target is reachable (atom and
    up) and how it's scored (direct relevance vs roll-up aggregation).

    seed_track_id = the now-playing track's UUID: its CLAP vector becomes a
    relevance source, so every block answers "similar to this" — AND-composable
    with the gates. `artists` = typeahead-picked artist UUIDs (hard filter).

    `quality` is not an engine tool (owned-only file tiers were dropped; returns
    later as a two-source file+stream tool), so the chip row is gone from the UI.
    """
    from discovery_engine import build

    active: dict = {}
    if q and q.strip():
        active["text"] = q.strip()[:255]
    if sound and sound.strip():
        active["sound"] = sound.strip()[:255]
    if lyrics and lyrics.strip():
        active["lyrics"] = lyrics.strip()[:255]
    if seed_track_id:
        active["seed"] = seed_track_id
    if artists:
        active["artist"] = artists
    active.update(_engine_filters(bpm_min, bpm_max, key, mode, vocalist,
                                  gender, danceable, energy, instruments, genres))
    warming = any(k in active for k in ("text", "sound", "lyrics")) and not all(
        model_cache.is_loaded(k) for k in ("clap", "lyrics", "enrichment"))
    if not active:
        return {"status": "ok", "results": [], "warming": warming}

    built = build(active, {}, corpus=corpus, limit=limit)
    if target not in built:                    # target below the atom — structurally absent
        return {"status": "ok", "results": [], "warming": warming}
    sql, params = built[target]
    with get_db_context() as db:
        # The segment-KNN retrieve branch overscans to 1000 rows (see Source.knn):
        # ef_search must cover the LIMIT, and iterative_scan (pgvector 0.8+) keeps
        # walking the graph when gates/floors filter the first wave — the exact
        # ORDER BY in the branch re-sorts relaxed_order's stream anyway.
        db.execute(text("SET LOCAL hnsw.ef_search = 1000"))
        db.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        rows = db.execute(text(sql), params).fetchall()
    return {"status": "ok", "results": _SHAPERS[target](rows), "warming": warming}
