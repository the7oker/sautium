"""
Shared track filtering for batch processing commands.

Provides:
- get_filtered_track_ids(): SQL-based filtering returning matching track IDs
- track_filter_options: Click decorator adding filter options to commands
- describe_filters(): Human-readable description of active filters

Uses the canonical schema: tracks → media_files → album_variants → albums.
Returns track IDs (UUIDs) since batch operations (embeddings, analysis) work on tracks.
"""

import functools
from typing import Dict, List, Optional

import click
from sqlalchemy import text
from sqlalchemy.orm import Session


def get_filtered_track_ids(
    db: Session,
    *,
    artist: Optional[str] = None,
    album: Optional[str] = None,
    genre: Optional[str] = None,
    path: Optional[str] = None,
    tag: Optional[str] = None,
    track_number: Optional[int] = None,
    lossless: Optional[bool] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
) -> Optional[List]:
    """
    Return track IDs matching the given filters.

    Returns None if no filters are active (meaning "all tracks").
    Returns List (possibly empty) if any filter is active.

    All string filters use ILIKE for case-insensitive partial matching.
    JOINs are added only when needed by the active filters.
    """
    # Check if any filter is active
    has_filter = any([
        artist, album, genre, path, tag,
        track_number is not None, lossless is not None,
        year_from is not None, year_to is not None,
    ])

    if not has_filter:
        return None

    joins = []
    where_clauses = []
    params: Dict[str, object] = {}

    # Artist filter: JOIN track_artists + artists
    if artist:
        joins.append("JOIN track_artists ta ON t.id = ta.track_id")
        joins.append("JOIN artists a ON ta.artist_id = a.id")
        where_clauses.append("a.name ILIKE :artist")
        params["artist"] = f"%{artist}%"

    # Album/year/lossless filters: need media_files + album_variants + albums
    need_album_join = any([album, year_from is not None, year_to is not None])
    need_mf_join = any([path, track_number is not None, lossless is not None]) or need_album_join

    if need_mf_join:
        joins.append("JOIN media_files mf ON mf.track_id = t.id")

    if need_album_join:
        joins.append("JOIN album_variants av ON mf.album_variant_id = av.id")
        joins.append("JOIN albums al ON av.album_id = al.id")

    if album:
        where_clauses.append("al.title ILIKE :album")
        params["album"] = f"%{album}%"

    if lossless is not None:
        where_clauses.append("mf.is_lossless = :lossless")
        params["lossless"] = lossless

    if year_from is not None:
        where_clauses.append("al.release_year >= :year_from")
        params["year_from"] = year_from

    if year_to is not None:
        where_clauses.append("al.release_year <= :year_to")
        params["year_to"] = year_to

    # Genre filter: JOIN track_genres + genres
    if genre:
        joins.append("JOIN track_genres tg ON t.id = tg.track_id")
        joins.append("JOIN genres g ON tg.genre_id = g.id")
        where_clauses.append("g.name ILIKE :genre")
        params["genre"] = f"%{genre}%"

    # Path filter: on media_files.file_path
    if path:
        where_clauses.append("mf.file_path ILIKE :path")
        params["path"] = f"%{path}%"

    # Tag filter: search artist_tags and album_tags via tags table
    if tag:
        # Need album join for album_tags if not already joined
        if not need_album_join and not need_mf_join:
            joins.append("JOIN media_files mf ON mf.track_id = t.id")
            joins.append("JOIN album_variants av ON mf.album_variant_id = av.id")
            joins.append("JOIN albums al ON av.album_id = al.id")
        elif not need_album_join:
            joins.append("JOIN album_variants av ON mf.album_variant_id = av.id")
            joins.append("JOIN albums al ON av.album_id = al.id")

        # Need artist join for artist_tags if not already joined
        if not artist:
            joins.append("JOIN track_artists ta ON t.id = ta.track_id")

        where_clauses.append("""(
            EXISTS (
                SELECT 1 FROM artist_tags at2
                JOIN tags tg2 ON at2.tag_id = tg2.id
                WHERE at2.artist_id = ta.artist_id AND tg2.name ILIKE :tag
            )
            OR EXISTS (
                SELECT 1 FROM album_tags alt2
                JOIN tags tg3 ON alt2.tag_id = tg3.id
                WHERE alt2.album_id = al.id AND tg3.name ILIKE :tag
            )
        )""")
        params["tag"] = f"%{tag}%"

    # Track number filter
    if track_number is not None:
        where_clauses.append("mf.track_number = :track_number")
        params["track_number"] = track_number

    joins_sql = "\n".join(joins)
    where_sql = " AND ".join(where_clauses)

    sql = f"""
        SELECT DISTINCT t.id
        FROM tracks t
        {joins_sql}
        WHERE {where_sql}
    """

    rows = db.execute(text(sql), params).fetchall()
    return [r[0] for r in rows]


def describe_filters(**kwargs) -> str:
    """
    Return a human-readable description of active filters.

    Returns empty string if no filters are active.
    """
    parts = []

    if kwargs.get("artist"):
        parts.append(f"artist~'{kwargs['artist']}'")
    if kwargs.get("album"):
        parts.append(f"album~'{kwargs['album']}'")
    if kwargs.get("genre"):
        parts.append(f"genre~'{kwargs['genre']}'")
    if kwargs.get("path"):
        parts.append(f"path~'{kwargs['path']}'")
    if kwargs.get("tag"):
        parts.append(f"tag~'{kwargs['tag']}'")
    if kwargs.get("track_number") is not None:
        parts.append(f"track#{kwargs['track_number']}")
    if kwargs.get("lossless") is not None:
        parts.append(f"lossless={kwargs['lossless']}")
    if kwargs.get("year_from") is not None:
        parts.append(f"year>={kwargs['year_from']}")
    if kwargs.get("year_to") is not None:
        parts.append(f"year<={kwargs['year_to']}")

    return ", ".join(parts)


def track_filter_options(func):
    """Click decorator that adds track filter options to a command."""
    @click.option("--artist", "filter_artist", type=str, default=None,
                  help="Filter by artist name (partial match)")
    @click.option("--album", "filter_album", type=str, default=None,
                  help="Filter by album title (partial match)")
    @click.option("--genre", "filter_genre", type=str, default=None,
                  help="Filter by genre name (partial match)")
    @click.option("--path", "filter_path", type=str, default=None,
                  help="Filter by file path (e.g. 'Electronic/Berlin School')")
    @click.option("--tag", "filter_tag", type=str, default=None,
                  help="Filter by Last.fm tag (e.g. 'idm', 'psychill')")
    @click.option("--track-number", "-n", "filter_track_number", type=int, default=None,
                  help="Filter by track number (e.g. 1 for first tracks)")
    @click.option("--lossless/--lossy", "filter_lossless", default=None,
                  help="Filter by lossless/lossy format")
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)
    return wrapper
