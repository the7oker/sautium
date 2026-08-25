"""
Catalog queries + result formatting shared by the two assistant tool surfaces.

The MCP server (`mcp/assistant_server.py`, driving Claude Code / Codex) and the
API-provider tool registry (`backend/tools/definitions.py`, driving Anthropic /
OpenAI) expose the SAME tools to the model and used to carry two copies of every
query — which is how one surface could drift owned-only while the other learned
about streaming. They now share this module; each passes its own query executor
(the MCP server has a psycopg2 connection of its own, the backend has db_pool).

Everything here speaks the CANONICAL identity: `tracks.id` / `albums.id` UUIDs,
which owned rows and not-owned (streamable) rows both carry. `media_files.id` is
the id of a FILE and appears only where a file is the subject.
"""

import uuid as _uuid

from ensemble_instruments import present_instruments
from hqplayer_client import format_time

# Lexical candidate sources, each behind its trigram GIN index (`%` — a bare
# `similarity(col, q) > x` cannot use one and seq-scans 3M rows).
_ART_MATCH = """
    SELECT a.id, GREATEST(similarity(a.name_latin, %(a_ql)s),
                          CASE WHEN a.name_latin LIKE %(a_pfx)s THEN 0.85 ELSE 0 END,
                          similarity(a.name, %(a_q)s)) AS s
    FROM artists a
    WHERE a.name_latin %% %(a_ql)s OR a.name_latin LIKE %(a_pfx)s OR a.name %% %(a_q)s
    ORDER BY s DESC LIMIT 25"""

_ALB_MATCH = """
    SELECT al.id, GREATEST(similarity(al.title_latin, %(b_ql)s),
                           CASE WHEN al.title_latin LIKE %(b_pfx)s THEN 0.85 ELSE 0 END) AS s
    FROM albums al
    WHERE al.title_latin %% %(b_ql)s OR al.title_latin LIKE %(b_pfx)s
    ORDER BY s DESC LIMIT 25"""

_TRK_MATCH = """
    SELECT t.id, GREATEST(similarity(t.title_latin, %(t_ql)s),
                          CASE WHEN t.title_latin LIKE %(t_pfx)s THEN 0.85 ELSE 0 END) AS s
    FROM tracks t
    WHERE t.title_latin %% %(t_ql)s OR t.title_latin LIKE %(t_pfx)s
    ORDER BY s DESC LIMIT 300"""

# Tracks OF a matched album, both layers: an owned album reaches them through its
# files, a not-owned one through its MusicBrainz tracklist.
_ALB_TRACKS = """
    SELECT atr.track_id, alb.s FROM alb JOIN album_tracks atr ON atr.album_id = alb.id
    UNION ALL
    SELECT mf.track_id, alb.s FROM alb
      JOIN album_variants av ON av.album_id = alb.id
      JOIN media_files mf ON mf.album_variant_id = av.id"""

# One row per track with both layers resolved: the owned file (if any) and the
# tracklist row that carries a not-owned track's album, length and cover.
_OWN_LATERAL = """
    LEFT JOIN LATERAL (
        SELECT mf.id, mf.cover_id, mf.duration_seconds, mf.is_lossless,
               mf.track_number, mf.disc_number, mf.sample_rate, mf.bit_depth,
               mf.file_path, al.id AS album_id, al.title AS album, al.release_year
        FROM media_files mf
        JOIN album_variants av ON av.id = mf.album_variant_id
        JOIN albums al ON al.id = av.album_id
        WHERE mf.track_id = t.id
        ORDER BY mf.is_analysis_source DESC, mf.id LIMIT 1) own ON true"""

_PH_LATERAL = """
    LEFT JOIN LATERAL (
        SELECT atr.length_ms, al.id AS album_id, al.title AS album,
               al.release_year, al.cover_url
        FROM album_tracks atr JOIN albums al ON al.id = atr.album_id
        WHERE atr.track_id = t.id
        ORDER BY (al.cover_url IS NOT NULL) DESC, al.id LIMIT 1) ph ON true"""


def valid_uuids(ids: list) -> list[str]:
    """Keep the well-formed UUIDs — an id the model invented must not reach a
    ::uuid cast and blow up the whole call."""
    out = []
    for i in ids:
        try:
            out.append(str(_uuid.UUID(str(i))))
        except (ValueError, AttributeError, TypeError):
            continue
    return out


def entity_kinds(q, ids: list[str]) -> dict:
    """Classify canonical UUIDs as 'track' or 'album'. v5 ids are namespaced per
    entity type, so an id belongs to exactly one of the two tables."""
    if not ids:
        return {}
    rows = q("""
        SELECT id::text AS id, 'track' AS kind FROM tracks
         WHERE id = ANY(CAST(%(ids)s AS uuid[]))
        UNION ALL
        SELECT id::text, 'album' FROM albums
         WHERE id = ANY(CAST(%(ids)s AS uuid[]))
    """, {"ids": list(ids)})
    return {r["id"]: r["kind"] for r in rows}


def owned_media_file(q, track_uuid: str):
    """The playable file for a track UUID, or None when nothing is on disk
    (a not-owned track — it streams instead)."""
    rows = q("""
        SELECT mf.id FROM media_files mf
        WHERE mf.track_id = %(t)s::uuid
        ORDER BY mf.is_analysis_source DESC, mf.id
        LIMIT 1
    """, {"t": track_uuid})
    return rows[0]["id"] if rows else None


def search_tracks(q, query: str = "", artist: str = "", album: str = "",
                  genre: str = "", limit: int = 20, corpus: str = "owned") -> list[dict]:
    """Metadata search over the catalog. The first term given IS the candidate
    source (query > artist > album > genre); the rest narrow it. corpus='owned'
    keeps only tracks with a file, 'all' includes the streamable ones."""
    params: dict = {"limit": min(limit, 50), "pool": max(limit * 4, 40),
                    "owned_only": corpus != "all"}
    ctes: list[str] = []
    cand: list[str] = []
    filters: list[str] = []
    seed = "query" if query else "artist" if artist else "album" if album else "genre"

    if query:
        params["a_q"] = params["t_q"] = query[:255]
        params["a_ql"] = params["t_ql"] = _latin(query)
        params["a_pfx"] = params["a_ql"] + "%"
        params["t_pfx"] = params["t_ql"] + "%"
        params["b_ql"], params["b_pfx"] = params["t_ql"], params["t_pfx"]
        ctes += [f"art AS ({_ART_MATCH})", f"alb AS ({_ALB_MATCH})", f"trk AS ({_TRK_MATCH})"]
        cand += ["SELECT id AS track_id, s FROM trk",
                 "SELECT ta.track_id, art.s FROM art "
                 "JOIN track_artists ta ON ta.artist_id = art.id AND ta.role = 'primary'",
                 _ALB_TRACKS]

    if artist:
        params["a_q"], params["a_ql"] = artist[:255], _latin(artist)
        params["a_pfx"] = params["a_ql"] + "%"
        if seed == "artist":
            ctes.append(f"art AS ({_ART_MATCH})")
            cand.append("SELECT ta.track_id, art.s FROM art "
                        "JOIN track_artists ta ON ta.artist_id = art.id AND ta.role = 'primary'")
        else:
            filters.append("""EXISTS (SELECT 1 FROM track_artists ta JOIN artists a ON a.id = ta.artist_id
                WHERE ta.track_id = best.track_id AND ta.role = 'primary'
                  AND (a.name_latin %% %(a_ql)s OR a.name_latin LIKE %(a_pfx)s
                       OR a.name %% %(a_q)s))""")

    if album:
        params["b_ql"] = _latin(album)
        params["b_pfx"] = params["b_ql"] + "%"
        if seed == "album":
            ctes.append(f"alb AS ({_ALB_MATCH})")
            cand.append(_ALB_TRACKS)
        else:
            filters.append("""EXISTS (SELECT 1 FROM albums al
                WHERE (al.title_latin %% %(b_ql)s OR al.title_latin LIKE %(b_pfx)s)
                  AND (EXISTS (SELECT 1 FROM album_tracks atr
                               WHERE atr.album_id = al.id AND atr.track_id = best.track_id)
                    OR EXISTS (SELECT 1 FROM album_variants av JOIN media_files mf
                                 ON mf.album_variant_id = av.id
                               WHERE av.album_id = al.id AND mf.track_id = best.track_id)))""")

    if genre:
        params["genre_like"] = f"%{genre}%"
        genre_albums = """SELECT ag.album_id FROM album_genres ag JOIN genres g ON g.id = ag.genre_id
                          WHERE g.name ILIKE %(genre_like)s"""
        if seed == "genre":
            ctes.append(f"alb AS (SELECT album_id AS id, 0.5::float AS s "
                        f"FROM ({genre_albums}) ga LIMIT 200)")
            cand.append(_ALB_TRACKS)
        else:
            filters.append(f"""EXISTS (SELECT 1 FROM ({genre_albums}) ga
                WHERE EXISTS (SELECT 1 FROM album_tracks atr
                              WHERE atr.album_id = ga.album_id AND atr.track_id = best.track_id)
                   OR EXISTS (SELECT 1 FROM album_variants av JOIN media_files mf
                                ON mf.album_variant_id = av.id
                              WHERE av.album_id = ga.album_id AND mf.track_id = best.track_id))""")

    where_extra = "".join(f" AND {f}" for f in filters)
    sql = f"""
        WITH {', '.join(ctes)},
        cand AS ({' UNION ALL '.join(cand)}),
        best AS (
            SELECT cand.track_id, MAX(cand.s) AS score,
                   EXISTS (SELECT 1 FROM media_files mf
                           WHERE mf.track_id = cand.track_id) AS is_owned
            FROM cand GROUP BY cand.track_id
        ),
        top AS (
            SELECT * FROM best
            WHERE (NOT %(owned_only)s OR is_owned){where_extra}
            ORDER BY score DESC, is_owned DESC
            LIMIT %(pool)s
        )
        SELECT t.id::text AS track_id, t.title, top.is_owned,
               (SELECT a.name FROM track_artists ta JOIN artists a ON a.id = ta.artist_id
                WHERE ta.track_id = t.id AND ta.role = 'primary' LIMIT 1) AS artist,
               COALESCE(own.album, ph.album) AS album,
               COALESCE(own.duration_seconds, ph.length_ms / 1000.0) AS duration_seconds,
               own.is_lossless,
               (SELECT g.name FROM album_genres ag JOIN genres g ON g.id = ag.genre_id
                WHERE ag.album_id = COALESCE(own.album_id, ph.album_id)
                ORDER BY ag.count DESC NULLS LAST LIMIT 1) AS genre
        FROM top
        JOIN tracks t ON t.id = top.track_id
        {_OWN_LATERAL}
        {_PH_LATERAL}
        ORDER BY top.score DESC, top.is_owned DESC, artist, album
        LIMIT %(limit)s
    """
    return q(sql, params)


def track_info(q, track_uuid: str):
    """Everything the assistant shows for one track: identity, album context,
    file facts when a file exists, and the analysis (which is keyed on the
    track, so a not-owned track carried in over P2P has it too)."""
    rows = q(f"""
        SELECT t.id::text AS track_id, t.title,
               (own.id IS NOT NULL) AS is_owned,
               own.id AS media_file_id, own.track_number, own.disc_number,
               own.sample_rate, own.bit_depth, own.is_lossless,
               COALESCE(own.duration_seconds, ph.length_ms / 1000.0) AS duration_seconds,
               COALESCE(own.album, ph.album) AS album,
               COALESCE(own.release_year, ph.release_year) AS release_year,
               (SELECT a.name FROM track_artists ta JOIN artists a ON a.id = ta.artist_id
                WHERE ta.track_id = t.id AND ta.role = 'primary' LIMIT 1) AS artist,
               (SELECT g.name FROM album_genres ag JOIN genres g ON g.id = ag.genre_id
                WHERE ag.album_id = COALESCE(own.album_id, ph.album_id)
                ORDER BY ag.count DESC NULLS LAST LIMIT 1) AS genre
        FROM tracks t
        {_OWN_LATERAL}
        {_PH_LATERAL}
        WHERE t.id = %(track_id)s::uuid
    """, {"track_id": track_uuid})
    return rows[0] if rows else None


def track_features(q, track_uuid: str):
    rows = q("""
        SELECT bpm, key, mode, energy_db, danceability, vocal_instrumental, instruments
        FROM audio_features WHERE track_id = %(track_id)s::uuid
    """, {"track_id": track_uuid})
    return rows[0] if rows else None


def track_lyrics(q, track_uuid: str):
    rows = q("""
        SELECT t.title, a.name AS artist, tl.source, tl.plain_lyrics, tl.instrumental
        FROM tracks t
        JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
        JOIN artists a ON a.id = ta.artist_id
        LEFT JOIN track_lyrics tl ON tl.track_id = t.id
        WHERE t.id = %(track_id)s::uuid
        ORDER BY CASE tl.source WHEN 'lrclib' THEN 1 WHEN 'genius' THEN 2 ELSE 3 END
        LIMIT 1
    """, {"track_id": track_uuid})
    return rows[0] if rows else None


def format_track(row: dict) -> str:
    """One track as the model reads it — always with the canonical ID, and with
    the not-owned rows marked so a reply can say how they will play."""
    parts = [p for p in (row.get("artist"), row.get("title")) if p]
    line = " - ".join(parts) if parts else "Track"

    extras = []
    if row.get("album"):
        extras.append(f"Album: {row['album']}")
    if row.get("genre"):
        extras.append(f"Genre: {row['genre']}")
    if row.get("duration_seconds"):
        extras.append(f"Duration: {format_time(float(row['duration_seconds']))}")
    if row.get("is_lossless") is not None:
        extras.append(f"Quality: {'Lossless' if row['is_lossless'] else 'Lossy'}")
    if row.get("similarity") is not None:
        extras.append(f"Similarity: {float(row['similarity']):.2%}")
    if row.get("is_owned") is False:
        extras.append("NOT in library (streams)")
    if row.get("track_id"):
        extras.append(f"ID: {row['track_id']}")

    return line + ("\n  " + " | ".join(extras) if extras else "")


def format_track_list(rows: list[dict], header: str = "") -> str:
    if not rows:
        return (header + "\nNo tracks found.") if header else "No tracks found."
    lines = [header] if header else []
    lines += [f"{i}. {format_track(r)}" for i, r in enumerate(rows, 1)]
    return "\n".join(lines)


def format_artist_list(rows: list[dict], header: str) -> str:
    lines = [header]
    for r in rows:
        tags = [t for t in (r.get("gender"), r.get("is_vocalist")) if t and t != "unknown"]
        if r.get("is_owned") is False:
            tags.append("not in library")
        tag_str = f" [{'/'.join(tags)}]" if tags else ""
        sim = f"{r['similarity']:.2f}" if r.get("similarity") is not None else " —  "
        lines.append(f"  {sim} | {r['artist']}{tag_str} | ID: {r['artist_id']}")
    return "\n".join(lines) if rows else header.split(" (")[0] + " — nothing found"


def format_album_list(rows: list[dict], header: str) -> str:
    lines = [header]
    for r in rows:
        year = f" ({r['year']})" if r.get("year") else ""
        owned = "" if r.get("is_owned") is not False else " [NOT in library — streams]"
        sim = f"{r['similarity']:.2f}" if r.get("similarity") is not None else " —  "
        lines.append(f"  {sim} | {r.get('artist') or '?'} — {r['album']}{year}{owned}"
                     f" | ID: {r['album_id']}")
    return "\n".join(lines) if rows else header.split(" (")[0] + " — nothing found"


def format_genre_list(rows: list[dict], header: str) -> str:
    lines = [header]
    for r in rows:
        sim = f"{r['similarity']:.2f}" if r.get("similarity") is not None else " —  "
        lines.append(f"  {sim} | {r['genre']} ({r.get('album_count', 0)} albums)")
    return "\n".join(lines) if rows else header.split(" (")[0] + " — nothing found"


def format_track_info(row: dict, features: dict | None) -> str:
    """The get_track_info answer."""
    lines = [f"{row['artist']} - {row['title']}", f"Album: {row['album']}"]
    if row.get("release_year"):
        lines.append(f"Year: {row['release_year']}")
    if row.get("genre"):
        lines.append(f"Genre: {row['genre']}")
    if row.get("track_number"):
        disc = (f" (Disc {row['disc_number']})"
                if row.get("disc_number") and row["disc_number"] > 1 else "")
        lines.append(f"Track: #{row['track_number']}{disc}")
    if row.get("duration_seconds"):
        lines.append(f"Duration: {format_time(float(row['duration_seconds']))}")
    if row.get("is_owned"):
        lines.append(f"Quality: {'Lossless' if row.get('is_lossless') else 'Lossy'}")
        if row.get("sample_rate"):
            lines.append(f"Sample rate: {row['sample_rate']} Hz / {row.get('bit_depth', '?')}-bit")
    else:
        lines.append("NOT in the library — plays by streaming (Deezer lossless / YouTube)")
    lines.append(f"ID: {row['track_id']}")

    if features:
        lines += ["", "Audio Features:"]
        if features.get("bpm"):
            lines.append(f"  BPM: {float(features['bpm']):.1f}")
        if features.get("key"):
            lines.append(f"  Key: {features['key']} {features.get('mode', '')}")
        if features.get("energy_db") is not None:
            lines.append(f"  Energy: {float(features['energy_db']):.1f} dB")
        if features.get("danceability") is not None:
            lines.append(f"  Danceability: {float(features['danceability']):.2f}")
        if features.get("vocal_instrumental"):
            lines.append(f"  Type: {features['vocal_instrumental']}")
        if features.get("instruments"):
            # storage is the raw top-20 score distribution — show only labels
            # above their read thresholds
            top = sorted(present_instruments(features["instruments"]).items(),
                         key=lambda x: -x[1])[:5]
            if top:
                lines.append("  Instruments: "
                             + ", ".join(f"{k} ({v:.2f})" for k, v in top))
    return "\n".join(lines)


def _latin(q: str) -> str:
    """Query-side half of the symmetric normalization that fills name_latin /
    title_latin. Imported lazily: the MCP surface can run in an environment
    without anyascii, where an ASCII query still matches and a Cyrillic one
    falls back to the original-script arms (artists.name)."""
    try:
        from transliterate import latinize
    except ImportError:
        return q.lower()[:255]
    return (latinize(q) or q)[:255]
