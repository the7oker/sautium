"""
Discovery screen endpoints.

Each search endpoint is an independent block — the frontend dispatches
all of them in parallel and renders blocks as they resolve. No single
aggregator: blocks must not block each other on slow signals (CLAP /
BGE-M3 cold start, etc.).

Per-block contract:
    GET /api/discovery/<block>?q=<query>&limit=<N>
    →
    { "status": "ok"     , "results": [...] }   # search ran
    { "status": "loading", "model": "<key>" }   # model not yet warm
    { "status": "empty"  , "results": [] }      # query too short / no model

When a block returns "loading" it has already kicked off a background
model load via `model_cache.kick_load`. The user sees an inline notice
and can retry within a few seconds — by then `is_loaded()` is True
and the next request returns real results.
"""

import random
from typing import Any, Optional
from fastapi import APIRouter, Query
from sqlalchemy import text

import model_cache
from database import get_db_context
from db_pool import db_query


router = APIRouter(prefix="/api/discovery", tags=["discovery"])


# Broad-instrument chip → underlying AST/PaSST tag set. The Discovery
# UI surfaces 10 categories; SQL expands the user's selection to all
# matching raw tags via JSONB `?|` (any-of) on `audio_features.instruments`.
INSTRUMENT_GROUPS: dict[str, list[str]] = {
    "piano":           ["piano", "electric piano", "keyboard (musical)"],
    "guitar":          ["guitar", "acoustic guitar", "plucked string instrument"],
    "electric guitar": ["electric guitar"],
    "bass":            ["bass guitar", "electric bass", "double bass"],
    "drums":           ["drum", "drum kit", "drum machine", "percussion",
                        "bass drum", "snare drum", "cymbal", "hi-hat",
                        "tabla", "tambourine"],
    "strings":         ["violin, fiddle", "cello", "bowed string instrument"],
    "orchestra":       ["orchestra"],
    "synth":           ["synthesizer", "sampler"],
    "brass":           ["brass instrument", "trumpet", "trombone", "french horn",
                        "tuba"],
    "saxophone":       ["saxophone"],
}


def _expand_instruments(broad: list[str]) -> list[str]:
    out: list[str] = []
    for b in broad:
        out.extend(INSTRUMENT_GROUPS.get(b.lower(), [b.lower()]))
    return list(dict.fromkeys(out))  # dedup, preserve order


# Energy bucket → energy_db window. -35..-5 dB is the practical range
# of the PaSST energy estimate; bucket boundaries chosen to roughly
# trisect it.
ENERGY_BUCKETS: dict[str, tuple[Optional[float], Optional[float]]] = {
    "low":  (None,   -25.0),
    "mid":  (-25.0,  -15.0),
    "high": (-15.0,  None),
}


def _build_filter_params(
    bpm_min: Optional[float],
    bpm_max: Optional[float],
    key: Optional[str],
    mode: Optional[str],
    vocalist: Optional[str],
    gender: Optional[str],
    danceable: Optional[str],
    energy: Optional[str],
    instruments: list[str],
    quality: list[str],
) -> dict:
    """Translate raw query params into the filter dict shape that
    `search._apply_filters` and our local SQL helpers consume.

    "any" / empty values are stripped so they don't generate SQL
    fragments. Multi-select fields (instruments, quality) become
    expanded lists; energy buckets translate to numeric ranges.
    """
    f: dict[str, Any] = {}
    if bpm_min is not None: f["bpm_min"] = bpm_min
    if bpm_max is not None: f["bpm_max"] = bpm_max
    if key: f["key"] = key
    if mode and mode != "any": f["mode"] = mode
    if vocalist and vocalist != "any": f["vocalist"] = vocalist
    if gender and gender != "any": f["gender"] = gender
    if danceable == "yes": f["danceable"] = True
    elif danceable == "no": f["not_danceable"] = True
    if energy in ENERGY_BUCKETS:
        lo, hi = ENERGY_BUCKETS[energy]
        if lo is not None: f["energy_min"] = lo
        if hi is not None: f["energy_max"] = hi
    if instruments:
        f["instruments_expanded"] = _expand_instruments(instruments)
    if quality:
        f["quality"] = [q.lower() for q in quality]
    return f


def _filter_clauses(
    f: dict, mf_alias: str = "mf"
) -> tuple[list[str], dict]:
    """Build SQL WHERE fragments and params for filters that apply
    to a track-row query (used by /discovery/titles directly and by
    the Stage-A track_ids pre-filter for sound/lyrics).

    Aliases assumed in caller's FROM:
      - tracks        AS t
      - audio_features AS af  (LEFT JOIN preferred)
      - artists       AS a
      - media_files   AS <mf_alias>
    """
    where: list[str] = []
    params: dict[str, Any] = {}

    if f.get("bpm_min") is not None:
        where.append("af.bpm >= :f_bpm_min")
        params["f_bpm_min"] = f["bpm_min"]
    if f.get("bpm_max") is not None:
        where.append("af.bpm <= :f_bpm_max")
        params["f_bpm_max"] = f["bpm_max"]
    if f.get("key"):
        where.append("af.key = :f_key")
        params["f_key"] = f["key"]
    if f.get("mode"):
        where.append("af.mode = :f_mode")
        params["f_mode"] = f["mode"]
    if f.get("vocalist"):
        where.append("a.is_vocalist = :f_vocalist")
        params["f_vocalist"] = f["vocalist"]
    if f.get("gender"):
        where.append("a.gender = :f_gender")
        params["f_gender"] = f["gender"]
    if f.get("danceable"):
        where.append("af.danceability >= 0.5")
    if f.get("not_danceable"):
        where.append("af.danceability < 0.5")
    if f.get("energy_min") is not None:
        where.append("af.energy_db >= :f_energy_min")
        params["f_energy_min"] = f["energy_min"]
    if f.get("energy_max") is not None:
        where.append("af.energy_db <= :f_energy_max")
        params["f_energy_max"] = f["energy_max"]
    if f.get("instruments_expanded"):
        where.append("af.instruments ?| :f_instruments")
        params["f_instruments"] = f["instruments_expanded"]
    if f.get("quality"):
        sub: list[str] = []
        for q in f["quality"]:
            if q == "lossy":
                sub.append(f"{mf_alias}.is_lossless = false")
            elif q == "lossless":
                sub.append(
                    f"({mf_alias}.is_lossless = true AND NOT "
                    f"({mf_alias}.sample_rate >= 48000 "
                    f"AND {mf_alias}.bit_depth >= 24))")
            elif q == "hi-res":
                sub.append(
                    f"({mf_alias}.is_lossless = true "
                    f"AND {mf_alias}.sample_rate >= 48000 "
                    f"AND {mf_alias}.bit_depth >= 24)")
        if sub:
            where.append("(" + " OR ".join(sub) + ")")
    return where, params


def _filter_needs_audio_features(f: dict) -> bool:
    af_keys = {"bpm_min", "bpm_max", "key", "mode", "danceable",
               "not_danceable", "energy_min", "energy_max",
               "instruments_expanded"}
    return bool(af_keys & set(f.keys()))


def _filtered_track_ids(f: dict, cap: int = 5000) -> Optional[list[str]]:
    """Stage-A query: returns the set of track UUIDs that pass the
    filter. Used to scope subsequent embedding searches without
    rebuilding their SQL. Returns None when no filter is active
    (caller skips Stage A entirely). Capped so the IN-clause stays
    bounded; if the cap is hit, results are still soundly filtered
    but might not represent the entire corpus — acceptable for
    "narrow then rank" Discovery semantics.
    """
    where, params = _filter_clauses(f, mf_alias="mf")
    if not where:
        return None
    af_join = "JOIN audio_features af ON af.track_id = t.id" \
        if _filter_needs_audio_features(f) else ""
    sql = f"""
        SELECT DISTINCT t.id::text AS id
        FROM tracks t
        JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
        JOIN artists a ON a.id = ta.artist_id
        JOIN media_files mf ON mf.track_id = t.id
        {af_join}
        WHERE {' AND '.join(where)}
        LIMIT %(cap)s
    """
    # Adapt :name → %(name)s for db_query (psycopg2 driver).
    py_params = {**{k: v for k, v in params.items()}, "cap": cap}
    py_sql = sql
    for k in params:
        py_sql = py_sql.replace(f":{k}", f"%({k})s")
    rows = db_query(py_sql, py_params)
    return [r["id"] for r in rows]


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


# -- Per-block search endpoints ----------------------------------------------

def _loading(model_key: str, model_label: str, factory) -> dict:
    """Kick a background load and return the loading marker."""
    model_cache.kick_load(model_key, factory)
    return {"status": "loading", "model": model_label, "results": []}


# Loader callables mirror the per-key factories in search.py — they
# must produce instances compatible with the corresponding cache key
# so a `kick_load`-warmed entry is interchangeable with one created
# lazily by `model_cache.get_model` inside the search functions.

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


def _attach_artist_covers(artists: list[dict]) -> list[dict]:
    """Batch-fetch cover_id / media_file_id for each artist tile."""
    if not artists:
        return artists
    ids = [r["artist_id"] for r in artists]
    cover_rows = db_query("""
        SELECT a.id::text AS artist_id,
               (SELECT mf.cover_id::text
                FROM media_files mf
                JOIN tracks t ON t.id = mf.track_id
                JOIN track_artists ta ON ta.track_id = t.id
                WHERE ta.artist_id = a.id AND mf.cover_id IS NOT NULL
                LIMIT 1) AS cover_id,
               (SELECT mf2.id
                FROM media_files mf2
                JOIN tracks t2 ON t2.id = mf2.track_id
                JOIN track_artists ta2 ON ta2.track_id = t2.id AND ta2.role = 'primary'
                WHERE ta2.artist_id = a.id
                LIMIT 1) AS media_file_id
        FROM artists a
        WHERE a.id = ANY(%(ids)s::uuid[])
    """, {"ids": ids})
    cov = {r["artist_id"]: r for r in cover_rows}
    for r in artists:
        c = cov.get(r["artist_id"]) or {}
        r["cover_id"] = c.get("cover_id")
        r["media_file_id"] = c.get("media_file_id")
    return artists


def _trigram_artists(q: str, limit: int) -> list[dict]:
    """Pure SQL fuzzy match on artists.name. No model dependency.

    Three complementary signals merged into one effective score:
      - prefix `lower(name) LIKE lower(q || '%')` — autocomplete
        (score 0.85). "sa" → "Sade", "Santana".
      - levenshtein <= 1 on lowercase — typo tolerance (score 0.70).
        "sode" → "Sade", "metalica" → "Metallica". Uses
        `levenshtein_less_equal(s1, s2, 1)` which short-circuits when
        the distance exceeds the cap, so the full-table scan stays
        fast even for thousands of artists.
      - trigram similarity(name, q) >= 0.5 — substring fuzzy.
        Threshold filters incidental matches like "Crusade" vs "sade"
        (sits at ~0.3) without blocking real ones.

    Tiebreaker by primary-track count so prominent artists rise to
    the top when many candidates share the same effective score.
    Artists with zero primary tracks (orphan import rows) are
    filtered out.
    """
    from transliterate import latinize
    q = q[:255]                 # postgres levenshtein() rejects args > 255 chars
    ql = (latinize(q) or q)[:255]   # query in Latin; name_latin is pre-lowered, so no lower()
    rows = db_query("""
        SELECT a.id::text AS artist_id,
               a.name AS artist,
               GREATEST(
                   similarity(a.name, %(q)s),
                   CASE WHEN lower(a.name) LIKE lower(%(prefix)s)
                        THEN 0.85 ELSE 0 END,
                   CASE WHEN levenshtein_less_equal(
                            lower(a.name), lower(%(q)s), 1) <= 1
                        THEN 0.70 ELSE 0 END,
                   similarity(a.name_latin, %(ql)s),
                   CASE WHEN a.name_latin LIKE %(qlprefix)s
                        THEN 0.85 ELSE 0 END,
                   CASE WHEN levenshtein_less_equal(
                            a.name_latin, %(ql)s, 1) <= 1
                        THEN 0.70 ELSE 0 END,
                   COALESCE((SELECT MAX(GREATEST(
                       similarity(al.alias_latin, %(ql)s),
                       CASE WHEN al.alias_latin LIKE %(qlprefix)s THEN 0.85 ELSE 0 END,
                       CASE WHEN levenshtein_less_equal(al.alias_latin, %(ql)s, 1) <= 1
                            THEN 0.70 ELSE 0 END))
                       FROM artist_name_aliases al WHERE al.artist_id = a.id), 0)
               ) AS sim,
               a.gender AS gender,
               a.is_vocalist AS is_vocalist,
               (SELECT COUNT(*) FROM track_artists ta
                WHERE ta.artist_id = a.id AND ta.role = 'primary') AS track_count,
               EXISTS (SELECT 1 FROM media_files mf
                       JOIN track_artists ta2 ON ta2.track_id = mf.track_id
                       WHERE ta2.artist_id = a.id) AS is_owned
        FROM artists a
        WHERE (similarity(a.name, %(q)s) >= 0.5
               OR lower(a.name) LIKE lower(%(prefix)s)
               OR levenshtein_less_equal(
                    lower(a.name), lower(%(q)s), 1) <= 1
               OR similarity(a.name_latin, %(ql)s) >= 0.5
               OR a.name_latin LIKE %(qlprefix)s
               OR levenshtein_less_equal(a.name_latin, %(ql)s, 1) <= 1
               OR EXISTS (SELECT 1 FROM artist_name_aliases al
                          WHERE al.artist_id = a.id
                            AND (similarity(al.alias_latin, %(ql)s) >= 0.5
                                 OR al.alias_latin LIKE %(qlprefix)s
                                 OR levenshtein_less_equal(al.alias_latin, %(ql)s, 1) <= 1)))
          AND EXISTS (
            SELECT 1 FROM track_artists ta
            WHERE ta.artist_id = a.id AND ta.role = 'primary'
          )
        ORDER BY sim DESC,
                 (SELECT COUNT(*) FROM track_artists ta
                  WHERE ta.artist_id = a.id AND ta.role = 'primary') DESC,
                 a.name
        LIMIT %(limit)s
    """, {"q": q, "prefix": q + "%", "ql": ql, "qlprefix": ql + "%", "limit": limit})
    return [{
        "artist_id": r["artist_id"],
        "artist": r["artist"],
        "gender": r.get("gender"),
        "is_vocalist": r.get("is_vocalist"),
        "track_count": r.get("track_count"),
        "similarity": round(float(r["sim"] or 0), 4),
        "match_source": "name",
    } for r in rows]


@router.get("/artists")
def discovery_artists(
    q: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=30),
    corpus: str = Query("all"),
):
    """Artist matches via the discovery engine: name_latin trigram + BGE-M3 bio
    (when warm). The engine gracefully degrades to name-only while the bio model
    loads (kicking the load), and surfaces is_owned / cover / media_file per tile."""
    from discovery_engine import build

    built = build({"text": q}, {}, corpus=corpus, limit=limit)
    if "artist" not in built:
        return {"status": "ok", "results": []}
    sql, params = built["artist"]
    with get_db_context() as db:
        rows = db.execute(text(sql), params).fetchall()

    results = [{
        "artist_id": str(r.id),
        "artist": r.name,
        "similarity": round(float(r.score), 4) if r.score is not None else None,
        "gender": r.gender,
        "is_vocalist": r.is_vocalist,
        "is_owned": bool(r.is_owned),
        "cover_id": r.cover_id,
        "media_file_id": r.media_file_id,
        "match_source": "engine",
    } for r in rows]
    return {"status": "ok", "results": results}


def _trigram_albums(q: str, limit: int) -> list[dict]:
    """Pure SQL fuzzy match on albums.title. No model dependency.

    Same prefix + trigram + levenshtein strategy as _trigram_artists —
    see comments there for the score rationale.
    """
    q = q[:255]                 # postgres levenshtein() rejects args > 255 chars
    rows = db_query("""
        SELECT al.id::text AS album_id,
               al.title AS album,
               al.release_year AS year,
               GREATEST(
                   similarity(al.title, %(q)s),
                   CASE WHEN lower(al.title) LIKE lower(%(prefix)s)
                        THEN 0.85 ELSE 0 END,
                   CASE WHEN levenshtein_less_equal(
                            lower(al.title), lower(%(q)s), 1) <= 1
                        THEN 0.70 ELSE 0 END
               ) AS sim,
               (SELECT a.name
                FROM artists a
                JOIN track_artists ta ON ta.artist_id = a.id AND ta.role = 'primary'
                JOIN tracks t ON t.id = ta.track_id
                JOIN media_files mf ON mf.track_id = t.id
                JOIN album_variants av ON av.id = mf.album_variant_id
                WHERE av.album_id = al.id
                GROUP BY a.id, a.name
                ORDER BY COUNT(*) DESC
                LIMIT 1) AS artist,
               (SELECT mf2.cover_id::text
                FROM media_files mf2
                JOIN album_variants av2 ON av2.id = mf2.album_variant_id
                WHERE av2.album_id = al.id AND mf2.cover_id IS NOT NULL
                LIMIT 1) AS cover_id,
               (SELECT mf3.id
                FROM media_files mf3
                JOIN album_variants av3 ON av3.id = mf3.album_variant_id
                WHERE av3.album_id = al.id
                ORDER BY mf3.disc_number, mf3.track_number
                LIMIT 1) AS media_file_id
        FROM albums al
        WHERE (similarity(al.title, %(q)s) >= 0.5
           OR lower(al.title) LIKE lower(%(prefix)s)
           OR levenshtein_less_equal(
                lower(al.title), lower(%(q)s), 1) <= 1)
          -- owned only: phantom rows (MB missing-album discovery) have no
          -- variants/files and would render as dead tiles here
          AND EXISTS (SELECT 1 FROM album_variants av WHERE av.album_id = al.id)
        ORDER BY sim DESC, al.title
        LIMIT %(limit)s
    """, {"q": q, "prefix": q + "%", "limit": limit})
    return [{
        "album_id": r["album_id"],
        "album": r["album"],
        "artist": r.get("artist"),
        "year": r.get("year"),
        "cover_id": r.get("cover_id"),
        "media_file_id": r.get("media_file_id"),
        "similarity": round(float(r["sim"] or 0), 4),
        "match_source": "title",
    } for r in rows]


@router.get("/albums")
def discovery_albums(
    q: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=30),
    corpus: str = Query("all"),
):
    """Album matches by title via the discovery engine (title_latin trigram),
    surfacing artist / year / cover / is_owned per tile."""
    from discovery_engine import build

    built = build({"text": q}, {}, corpus=corpus, limit=limit)
    if "album" not in built:
        return {"status": "ok", "results": []}
    sql, params = built["album"]
    with get_db_context() as db:
        rows = db.execute(text(sql), params).fetchall()

    results = [{
        "album_id": str(r.id),
        "album": r.name,
        "artist": r.artist,
        "year": r.year,
        "cover_id": r.cover_id,
        "cover_url": r.cover_url,
        "media_file_id": r.media_file_id,
        "is_owned": bool(r.is_owned),
        "similarity": round(float(r.score), 4) if r.score is not None else None,
        "match_source": "engine",
    } for r in rows]
    return {"status": "ok", "results": results}


def _engine_filters(bpm_min, bpm_max, key, mode, vocalist, gender,
                    danceable, energy, instruments) -> dict:
    """Map the shared Discovery filter chips to engine tool values (used by
    /titles, /sound, /lyrics)."""
    a: dict = {}
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


def _track_results(rows) -> list:
    """Engine track rows → the /titles /sound /lyrics result shape (owned playback
    id + phantom track_id + is_owned + tile fields)."""
    return [{
        "id": r.media_file_id,
        "track_id": str(r.id),
        "title": r.name,
        "artist": r.artist,
        "album": r.album,
        "year": r.year,
        "duration_seconds": float(r.duration_seconds) if r.duration_seconds else None,
        "cover_id": r.cover_id,
        "is_owned": bool(r.is_owned),
        "similarity": round(float(r.score), 4) if r.score is not None else None,
    } for r in rows]


@router.get("/sound")
def discovery_sound(
    q: str = Query(..., min_length=1),
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
    quality: list[str] = Query(default_factory=list),
    corpus: str = Query("all"),
):
    """Tracks closest in CLAP audio embedding to the text query, via the engine
    (sound tool + filter chips). Loading marker until CLAP is warm."""
    if not model_cache.is_loaded("clap"):
        return _loading("clap", "audio", _clap_loader)

    from discovery_engine import build
    active = {"sound": q, **_engine_filters(bpm_min, bpm_max, key, mode,
                                            vocalist, gender, danceable, energy, instruments)}
    built = build(active, {}, corpus=corpus, limit=limit)
    if "track" not in built:
        return {"status": "ok", "results": []}
    sql, params = built["track"]
    with get_db_context() as db:
        rows = db.execute(text(sql), params).fetchall()
    return {"status": "ok", "results": _track_results(rows)}


@router.get("/lyrics")
def discovery_lyrics(
    q: str = Query(..., min_length=1),
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
    quality: list[str] = Query(default_factory=list),
    corpus: str = Query("all"),
):
    """Tracks whose lyrics match the query semantically, via the engine (lyrics
    tool + filter chips). Loading marker until the lyrics model is warm."""
    if not model_cache.is_loaded("lyrics"):
        return _loading("lyrics", "lyrics", _lyrics_loader)

    from discovery_engine import build
    active = {"lyrics": q, **_engine_filters(bpm_min, bpm_max, key, mode,
                                             vocalist, gender, danceable, energy, instruments)}
    built = build(active, {}, corpus=corpus, limit=limit)
    if "track" not in built:
        return {"status": "ok", "results": []}
    sql, params = built["track"]
    with get_db_context() as db:
        rows = db.execute(text(sql), params).fetchall()
    return {"status": "ok", "results": _track_results(rows)}


@router.get("/titles")
def discovery_titles(
    q: Optional[str] = Query(None),
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
    quality: list[str] = Query(default_factory=list),
    corpus: str = Query("all"),
):
    """Track matches by title and/or audio filters, via the discovery engine.
    Maps the Discovery filter chips to engine tools; browse mode (no text) returns
    filter-only matches. (quality isn't an engine tool yet — noted below.)"""
    from discovery_engine import build

    active: dict = {}
    if q and q.strip():
        active["text"] = q.strip()[:255]
    active.update(_engine_filters(bpm_min, bpm_max, key, mode, vocalist,
                                  gender, danceable, energy, instruments))
    # NOTE: `quality` is not an engine tool yet (owned-only file tiers dropped;
    # returns as a two-source file+stream tool later).
    if not active:
        return {"status": "ok", "results": []}

    built = build(active, {}, corpus=corpus, limit=limit)
    if "track" not in built:
        return {"status": "ok", "results": []}
    sql, params = built["track"]
    with get_db_context() as db:
        rows = db.execute(text(sql), params).fetchall()
    return {"status": "ok", "results": _track_results(rows)}
