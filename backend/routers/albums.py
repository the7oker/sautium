"""
Album detail endpoint.

Aggregates album-level metadata (title, year, primary artist, format
quality), top genre chips, and the full tracklist with per-track
audio features (key, mode, BPM) into a single roundtrip.

When an album has more than one variant (multiple rips/encodings/
masters of the same logical release), callers can pick a specific
variant via ``?variant_id=<id>``. Without it, the response uses
DISTINCT ON across all variants of the album, falling back to the
"best" media_file per track (analysis-source first, lowest id as
deterministic tiebreaker). The full variant list is always returned
so the UI can render a selector without a second roundtrip.
"""

from fastapi import APIRouter, HTTPException, Query

from db_pool import db_query, db_query_one
from genre_queries import album_genre_chips


router = APIRouter(prefix="/api/albums", tags=["albums"])


def _phantom_album(album_id: str) -> dict:
    """Album-detail payload for a PHANTOM (not-owned) album. Owned albums are
    served from media_files/album_variants; a phantom has neither, so the
    tracklist comes from `album_tracks` (Phantom Discovery) with its MB length,
    the cover from `albums.cover_url` (Cover Art Archive hotlink), and the
    primary artist from the track credits. No quality / variant data — a phantom
    is not a rip. `is_owned=False` lets the UI swap Play/Queue for Listen/Buy."""
    album = db_query_one("""
        SELECT al.id::text AS id,
               al.title,
               al.release_year AS year,
               al.cover_url
        FROM albums al
        WHERE al.id = %(id)s::uuid
    """, {"id": album_id})
    if not album:
        raise HTTPException(status_code=404, detail="album not found")

    album["is_owned"] = False
    album["cover_id"] = None
    album["media_file_id"] = None
    album["quality"] = None
    # Streaming quality is PER-TRACK (lossless where Deezer has it, lossy via the
    # YouTube fallback), so it can't be known without resolving the tracklist.
    # Left null; the album page fills the badge from /phantom-availability once it
    # knows the real mix — avoids briefly claiming "Lossless" for a mostly-lossy album.
    album["stream_quality"] = None
    album["variants"] = []
    album["selected_variant_id"] = None

    # Most-frequent primary credit across the phantom tracklist (album has no
    # artist_id; phantom tracks carry track_artists from the MB release).
    album["primary_artist"] = db_query_one("""
        SELECT a.id::text AS id, a.name
        FROM artists a
        JOIN track_artists ta ON ta.artist_id = a.id AND ta.role = 'primary'
        JOIN album_tracks atr ON atr.track_id = ta.track_id
        WHERE atr.album_id = %(id)s::uuid
        GROUP BY a.id, a.name
        ORDER BY COUNT(*) DESC
        LIMIT 1
    """, {"id": album_id})

    # Album-grain genres, falling back to the primary artist's genres when the
    # phantom album has none (same shared rule as the owned-album page).
    album["genres"] = album_genre_chips(
        album_id,
        album["primary_artist"]["id"] if album["primary_artist"] else None)

    # Tracklist from album_tracks. No media_file_id (no local audio → rows are
    # display-only, playback via play-phantom-album), but bpm/key/mode DO appear
    # once a preview has streamed and the enricher analysed the track
    # (audio_features keyed by track_id, provenance = a stream-origin
    # analysis_sources row).
    album["tracks"] = db_query("""
        SELECT t.id::text AS track_id,
               NULL::int    AS media_file_id,
               t.title,
               atr.disc     AS disc_number,
               atr.position AS track_number,
               atr.length_ms / 1000.0 AS duration,
               af.bpm,
               af.key,
               af.mode
        FROM album_tracks atr
        JOIN tracks t ON t.id = atr.track_id
        LEFT JOIN audio_features af ON af.track_id = t.id
        WHERE atr.album_id = %(id)s::uuid
        ORDER BY atr.disc, atr.position
    """, {"id": album_id})

    # Per-track buffering flag from the live preview proxy (in-memory, transient).
    # Folding it into THIS payload means one re-fetch carries both the enriched
    # features (DB) AND the buffering state (proxy) — a single consistent snapshot,
    # so there's no second source for the UI to race against (preview-events SSE
    # just pings 'refresh'; the page re-reads everything here).
    try:
        from streaming import service as _streaming
        _proxy = _streaming.get_proxy() if _streaming.is_enabled() else None
    except Exception:
        _proxy = None
    for t in album["tracks"]:
        t["buffering"] = bool(_proxy and _proxy.is_buffering(t["track_id"]))

    album["total_duration"] = float(sum(t["duration"] or 0 for t in album["tracks"]))
    return album


@router.get("/{album_id}")
def get_album(
    album_id: str,
    variant_id: int | None = Query(None),
) -> dict:
    # A phantom (not-owned) album has no album_variants/media_files; serve its
    # tracklist from album_tracks instead of the rip-based queries below.
    if not db_query_one(
        "SELECT 1 AS ok FROM album_variants WHERE album_id = %(id)s::uuid LIMIT 1",
        {"id": album_id},
    ):
        return _phantom_album(album_id)

    if variant_id is not None:
        owner = db_query_one("""
            SELECT 1 AS ok FROM album_variants
            WHERE id = %(vid)s AND album_id = %(aid)s::uuid
        """, {"vid": variant_id, "aid": album_id})
        if not owner:
            raise HTTPException(
                status_code=404,
                detail="variant not found for this album",
            )

    params = {"id": album_id, "vid": variant_id}

    album = db_query_one("""
        SELECT al.id::text AS id,
               al.title,
               al.release_year AS year,
               (SELECT mf.cover_id::text
                FROM media_files mf
                JOIN album_variants av ON av.id = mf.album_variant_id
                WHERE av.album_id = al.id
                  AND (%(vid)s::int IS NULL OR av.id = %(vid)s::int)
                  AND mf.cover_id IS NOT NULL
                LIMIT 1) AS cover_id,
               (SELECT mf2.id
                FROM media_files mf2
                JOIN album_variants av2 ON av2.id = mf2.album_variant_id
                WHERE av2.album_id = al.id
                  AND (%(vid)s::int IS NULL OR av2.id = %(vid)s::int)
                ORDER BY mf2.disc_number, mf2.track_number
                LIMIT 1) AS media_file_id
        FROM albums al
        WHERE al.id = %(id)s::uuid
    """, params)

    if not album:
        raise HTTPException(status_code=404, detail="album not found")

    # Most-frequent primary artist for this album (always album-level,
    # independent of variant — composers don't change per remaster).
    primary = db_query_one("""
        SELECT a.id::text AS id, a.name
        FROM artists a
        JOIN track_artists ta ON ta.artist_id = a.id AND ta.role = 'primary'
        JOIN tracks t ON t.id = ta.track_id
        JOIN media_files mf ON mf.track_id = t.id
        JOIN album_variants av ON av.id = mf.album_variant_id
        WHERE av.album_id = %(id)s::uuid
        GROUP BY a.id, a.name
        ORDER BY COUNT(*) DESC
        LIMIT 1
    """, {"id": album_id})
    album["primary_artist"] = primary

    # Quality + total duration. When no variant_id is requested, the same
    # DISTINCT ON pattern as the tracklist below — pick one media_file per
    # track (analysis-source preferred, lowest id as fallback) so multi-
    # variant albums don't double-count duration. When a specific variant
    # is requested, restrict to that variant before DISTINCT ON; tracks
    # without a media_file in that variant simply don't contribute.
    qrow = db_query_one("""
        SELECT BOOL_OR(is_lossless)          AS lossless,
               MAX(sample_rate)              AS sr_max,
               MAX(bit_depth)                AS bd_max,
               SUM(duration_seconds)         AS total_duration
        FROM (
            SELECT DISTINCT ON (mf.track_id)
                   mf.track_id,
                   mf.is_lossless,
                   mf.sample_rate,
                   mf.bit_depth,
                   mf.duration_seconds
            FROM media_files mf
            JOIN album_variants av ON av.id = mf.album_variant_id
            WHERE av.album_id = %(id)s::uuid
              AND (%(vid)s::int IS NULL OR av.id = %(vid)s::int)
            ORDER BY mf.track_id, mf.is_analysis_source DESC, mf.id
        ) per_track
    """, params)
    sr = qrow["sr_max"] or 0
    bd = qrow["bd_max"] or 0
    if qrow["lossless"] and sr >= 48000 and bd >= 24:
        album["quality"] = "hi-res"
    elif qrow["lossless"]:
        album["quality"] = "lossless"
    else:
        album["quality"] = "lossy"
    album["total_duration"] = float(qrow["total_duration"] or 0)

    # Album-grain genres (deduped across filetag/mb, ranked by count), falling
    # back to the primary artist's genres when the album has none — the same
    # rule the phantom-album page uses, so both render identically.
    album["genres"] = album_genre_chips(
        album_id, primary["id"] if primary else None)

    # Tracklist ordered by disc / track number. `is_analysis_source`
    # is a *preference* — it marks the media_file we chose for audio
    # analysis when a track has multiple variants — not a "show this
    # in the UI" flag. Filtering on it strictly hides tracks whose
    # variants weren't picked yet (newly imported files, edge cases
    # like Wingbeats where two media_files share disc/track and
    # neither is flagged). Use DISTINCT ON instead: one row per
    # track, picking analysis-source first and the lowest media_file
    # id as the deterministic fallback. When a variant is pinned, the
    # WHERE clause narrows the pool to that variant's media_files only.
    album["tracks"] = db_query("""
        SELECT DISTINCT ON (t.id)
               t.id::text AS track_id,
               mf.id AS media_file_id,
               t.title,
               mf.disc_number,
               mf.track_number,
               mf.duration_seconds AS duration,
               af.bpm,
               af.key,
               af.mode
        FROM media_files mf
        JOIN tracks t ON t.id = mf.track_id
        JOIN album_variants av ON av.id = mf.album_variant_id
        LEFT JOIN audio_features af ON af.track_id = t.id
        WHERE av.album_id = %(id)s::uuid
          AND (%(vid)s::int IS NULL OR av.id = %(vid)s::int)
        ORDER BY t.id,
                 mf.is_analysis_source DESC,
                 mf.id
    """, params)
    album["tracks"].sort(key=lambda r: (
        r.get("disc_number") if r.get("disc_number") is not None else 99,
        r.get("track_number") if r.get("track_number") is not None else 999,
        r.get("title") or "",
    ))

    # Full variant list, always returned (UI hides the selector when
    # len == 1). Ordered "best first" so a UI defaulting to variants[0]
    # picks the lossless/highest-resolution rip without further logic.
    album["variants"] = db_query("""
        SELECT av.id AS variant_id,
               av.sample_rate,
               av.bit_depth,
               av.is_lossless,
               av.directory_path,
               av.file_modified_at,
               (SELECT mf.id FROM media_files mf
                WHERE mf.album_variant_id = av.id
                ORDER BY mf.disc_number, mf.track_number
                LIMIT 1) AS first_media_file_id,
               (SELECT mf.file_format FROM media_files mf
                WHERE mf.album_variant_id = av.id
                LIMIT 1) AS file_format
        FROM album_variants av
        WHERE av.album_id = %(id)s::uuid
        ORDER BY av.is_lossless DESC NULLS LAST,
                 av.sample_rate DESC NULLS LAST,
                 av.bit_depth DESC NULLS LAST,
                 av.id
    """, {"id": album_id})
    album["selected_variant_id"] = variant_id
    album["is_owned"] = True

    return album


@router.get("/{album_id}/similar")
def get_similar_albums(
    album_id: str,
    limit: int = Query(12, ge=1, le=50),
    exclude_same_artist: bool = Query(False),
    min_similarity: float = Query(0.6, ge=0.0, le=1.0),
) -> dict:
    """Audio-similar albums (CLAP, one-to-one assignment scoring).

    Computed per view (~0.3s in the FastAPI threadpool; the shelf loads async)
    — the old read-through cache fossilised (new rips never joined old albums'
    neighbour lists) and silently served a dead embedding space after the mean
    flip. Owned AND phantom neighbours rank together purely by similarity;
    tiles hydrate via the shared phantom-aware hydrator, so a phantom neighbour
    renders with its CAA cover and routes to the streamable album page.
    """
    from album_similarity import compute_similar

    cap = limit * 3 if exclude_same_artist else limit
    rows = [
        {"album_id": r["similar_album_id"], "sim": r["score"]}
        for r in compute_similar(album_id)
        if r["score"] >= min_similarity
    ][:cap]
    if not rows:
        return {"results": []}

    from entity_hydration import hydrate_albums
    sim = {r["album_id"]: round(r["sim"], 4) for r in rows}
    tiles = hydrate_albums([r["album_id"] for r in rows])   # phantom-aware, order-preserving

    if exclude_same_artist:
        src = db_query_one("""
            SELECT a.name
            FROM track_artists ta
            JOIN artists a ON a.id = ta.artist_id
            WHERE ta.role = 'primary' AND ta.track_id IN (
                SELECT mf.track_id FROM media_files mf
                JOIN album_variants av ON av.id = mf.album_variant_id WHERE av.album_id = %(id)s::uuid
                UNION
                SELECT atr.track_id FROM album_tracks atr WHERE atr.album_id = %(id)s::uuid)
            GROUP BY a.name ORDER BY COUNT(*) DESC LIMIT 1
        """, {"id": album_id})
        if src and src.get("name"):
            tiles = [t for t in tiles if t.get("artist") != src["name"]]

    for t in tiles:
        t["similarity"] = sim.get(t["album_id"])
    return {"results": tiles[:limit]}
