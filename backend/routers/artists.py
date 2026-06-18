"""
Artist detail endpoint.

Aggregates everything the Artist screen needs in a single roundtrip:
identity, Last.fm bio + tags, the artist's albums in user's library,
popular tracks (by local play count), and similar artists with one
representative cover for each. Photo URL is reserved for future
Last.fm artist-image enrichment — returned as null until Step 1.7.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from db_pool import db_query, db_query_one
from discography import fetch_new_albums, sync_artist_discography
from release_groups import collapse_to_groups


router = APIRouter(prefix="/api/artists", tags=["artists"])

# Fetch-on-view gate: the artist screen triggers a missing-album reconcile
# (local MB dump) at most once a day per artist; the monthly background
# sync handles the rest.
_DISCOGRAPHY_VIEW_STALE_HOURS = 24


def _is_discography_stale(last_sync) -> bool:
    """True if the artist's new-album data should be refreshed on view."""
    if last_sync is None:
        return True
    if last_sync.tzinfo is None:
        last_sync = last_sync.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last_sync > timedelta(
        hours=_DISCOGRAPHY_VIEW_STALE_HOURS
    )


# Supported album-sort modes. Each one corresponds to an ORDER BY
# expression injected below the role_priority grouping, plus a
# metric-line formatter that the tile renders under the title.
_ALBUM_SORTS = {
    "release_year",   # default
    "time_listened",
    "popularity",
    "recently_added",
    "a_z",
}


def _fmt_seconds(total: int) -> str:
    """Compact listening-time label, '12h 4m' / '42m' / '—' for nothing."""
    if total is None or total <= 0:
        return ""
    minutes = int(total) // 60
    if minutes < 1:
        return ""
    if minutes < 60:
        return f"{minutes}m"
    hours, rem = divmod(minutes, 60)
    if rem == 0:
        return f"{hours}h"
    return f"{hours}h {rem}m"


def _fmt_plays(count: int) -> str:
    """Human plays — 1.2M / 280k / 850. Last.fm scrobble counts can be
    huge; tile space is tight, so we compact above 1k."""
    if count is None or count <= 0:
        return ""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M".replace(".0M", "M")
    if count >= 1000:
        return f"{count / 1000:.0f}k"
    return str(count)


def _fmt_added(added_at) -> str:
    """'3d ago' / '12h ago' / 'today'. The tile prefixes a small `+`
    glyph so the unit (when the album was added) is unambiguous."""
    if added_at is None:
        return ""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    if added_at.tzinfo is None:
        added_at = added_at.replace(tzinfo=timezone.utc)
    delta = now - added_at
    secs = int(delta.total_seconds())
    if secs < 3600:
        return "today"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    days = secs // 86400
    if days < 14:
        return f"{days}d ago"
    if days < 60:
        return f"{days // 7}w ago"
    if days < 365:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"


@router.get("/{artist_id}")
def get_artist(
    artist_id: str,
    sort: str = Query(default="release_year"),
) -> dict:
    if sort not in _ALBUM_SORTS:
        sort = "release_year"
    artist = db_query_one("""
        SELECT a.id::text AS id,
               a.name,
               NULL::text AS photo_url,
               a.last_album_sync
        FROM artists a
        WHERE a.id = %(id)s::uuid
    """, {"id": artist_id})

    if not artist:
        raise HTTPException(status_code=404, detail="artist not found")

    # last_album_sync drives the fetch-on-view refresh decision; it's not
    # part of the response shape the screen consumes.
    last_album_sync = artist.pop("last_album_sync", None)

    bio_row = db_query_one("""
        SELECT content, summary
        FROM artist_bios
        WHERE artist_id = %(id)s::uuid
    """, {"id": artist_id})
    artist["bio"] = bio_row["content"] if bio_row else None
    artist["bio_summary"] = bio_row["summary"] if bio_row else None

    # Genres for this artist — same evidence rule as the genre detail
    # page (so artist-X-on-genre-Y and genre-Y-on-artist-X stay in
    # sync). An artist is "in" a genre if either the Last.fm artist-
    # tag for it carries weight >= 10 OR the artist has at least 5
    # primary tracks on albums tagged with that genre (album_genres).
    # Last.fm weight wins on sort; track count is the supplementary
    # signal that surfaces niche artists whose Last.fm tagging is
    # weak but whose tracks are clearly genre-tagged in the library.
    # The non-alphanumeric-strip lets "nu-jazz" resolve to "Nu Jazz".
    artist["tags"] = db_query("""
        WITH via_tag AS (
            SELECT g.id::text AS genre_id, g.name,
                   COALESCE(at.weight, 0)::int AS lastfm_weight,
                   0::int AS track_count
            FROM artist_tags at
            JOIN tags tg ON tg.id = at.tag_id
            JOIN genres g
              ON regexp_replace(LOWER(g.name), '[^a-z0-9]', '', 'g')
               = regexp_replace(LOWER(tg.name), '[^a-z0-9]', '', 'g')
            WHERE at.artist_id = %(id)s::uuid
              AND COALESCE(at.weight, 0) >= 10
        ),
        via_track AS (
            SELECT g.id::text AS genre_id, g.name,
                   0::int AS lastfm_weight,
                   COUNT(DISTINCT t.id)::int AS track_count
            FROM tracks t
            JOIN track_artists ta
              ON ta.track_id = t.id AND ta.role = 'primary'
            JOIN media_files mf ON mf.track_id = t.id
            JOIN album_variants av ON av.id = mf.album_variant_id
            JOIN album_genres ag ON ag.album_id = av.album_id
            JOIN genres g ON g.id = ag.genre_id
            WHERE ta.artist_id = %(id)s::uuid
            GROUP BY g.id, g.name
            HAVING COUNT(DISTINCT t.id) >= 5
        )
        SELECT genre_id, name,
               MAX(lastfm_weight)::int AS weight,
               MAX(track_count)::int  AS track_count
        FROM (SELECT * FROM via_tag UNION ALL SELECT * FROM via_track) u
        GROUP BY genre_id, name
        ORDER BY weight DESC, track_count DESC, name
    """, {"id": artist_id})

    # Discography: every album where the artist appears in any role.
    # role_priority sorts the list so genuine solo records lead, true
    # collabs (multiple primary artists) follow, and featured-only
    # appearances close out the grid. is_primary feeds a small
    # "feat." badge on tiles where the artist isn't the headline.
    #
    # Per-album metrics (listening time, scrobble count, added-at) are
    # always computed so the UI can switch sort without an extra
    # round-trip. role_priority is the primary ORDER BY column for
    # every sort — solo work stays grouped, feat. credits stay in
    # their own block — and the user-picked column controls the order
    # within each group.
    sort_expr = {
        "release_year":   "al.release_year DESC NULLS LAST, al.title",
        "time_listened":  "time_listened_seconds DESC NULLS LAST, al.title",
        "popularity":     "popularity DESC NULLS LAST, al.title",
        "recently_added": "al.created_at DESC NULLS LAST, al.title",
        "a_z":            ("regexp_replace(LOWER(al.title), "
                           "'^(the|a|an)\\s+', '', 'i')"),
    }[sort]

    rows = db_query(f"""
        WITH metrics AS (
            SELECT al.id AS album_id,
                   COALESCE(SUM(lh.duration_listened), 0)::bigint
                       AS time_listened_seconds,
                   COALESCE(SUM(ts.playcount), 0)::bigint
                       AS popularity
            FROM albums al
            JOIN album_variants av ON av.album_id = al.id
            JOIN media_files mf ON mf.album_variant_id = av.id
            JOIN tracks t ON t.id = mf.track_id
            LEFT JOIN listening_history lh ON lh.track_id = t.id
            LEFT JOIN track_stats ts
                   ON ts.track_id = t.id AND ts.source = 'lastfm'
            WHERE al.id IN (
                SELECT DISTINCT av2.album_id
                FROM album_variants av2
                JOIN media_files mf2 ON mf2.album_variant_id = av2.id
                JOIN tracks t2 ON t2.id = mf2.track_id
                JOIN track_artists ta2 ON ta2.track_id = t2.id
                WHERE ta2.artist_id = %(id)s::uuid
            )
            GROUP BY al.id
        )
        SELECT al.id::text AS id,
               al.title,
               al.musicbrainz_id::text AS musicbrainz_id,
               al.release_year AS year,
               al.created_at AS added_at,
               (SELECT mf.cover_id::text
                FROM media_files mf
                JOIN album_variants av ON av.id = mf.album_variant_id
                WHERE av.album_id = al.id AND mf.cover_id IS NOT NULL
                LIMIT 1) AS cover_id,
               (SELECT mf2.id
                FROM media_files mf2
                JOIN album_variants av2 ON av2.id = mf2.album_variant_id
                WHERE av2.album_id = al.id
                ORDER BY mf2.disc_number, mf2.track_number
                LIMIT 1) AS media_file_id,
               BOOL_OR(ta.role = 'primary' AND ta.artist_id = %(id)s::uuid)
                   AS is_primary,
               CASE
                 WHEN BOOL_OR(ta.role = 'primary' AND ta.artist_id = %(id)s::uuid)
                      AND NOT BOOL_OR(ta.role = 'primary' AND ta.artist_id <> %(id)s::uuid)
                   THEN 1
                 WHEN BOOL_OR(ta.role = 'primary' AND ta.artist_id = %(id)s::uuid)
                   THEN 2
                 ELSE 3
               END AS role_priority,
               m.time_listened_seconds,
               m.popularity
        FROM albums al
        JOIN album_variants av ON av.album_id = al.id
        JOIN media_files mf ON mf.album_variant_id = av.id
        JOIN tracks t ON t.id = mf.track_id
        JOIN track_artists ta ON ta.track_id = t.id
        JOIN metrics m ON m.album_id = al.id
        WHERE al.id IN (
            SELECT DISTINCT av2.album_id
            FROM album_variants av2
            JOIN media_files mf2 ON mf2.album_variant_id = av2.id
            JOIN tracks t2 ON t2.id = mf2.track_id
            JOIN track_artists ta2 ON ta2.track_id = t2.id
            WHERE ta2.artist_id = %(id)s::uuid
        )
        GROUP BY al.id, al.title, al.musicbrainz_id, al.release_year, al.created_at,
                 m.time_listened_seconds, m.popularity
        ORDER BY role_priority, {sort_expr}
    """, {"id": artist_id})

    # Pre-format the metric so the UI doesn't have to mirror server
    # formatting rules. Empty string == unavailable; the tile renders
    # it as '—' in the dimmed style.
    metric_for = {
        "release_year":   lambda r: (str(r["year"]) if r["year"] else ""),
        "time_listened":  lambda r: _fmt_seconds(r["time_listened_seconds"]),
        "popularity":     lambda r: _fmt_plays(r["popularity"]),
        "recently_added": lambda r: _fmt_added(r["added_at"]),
        "a_z":            lambda r: (str(r["year"]) if r["year"] else ""),
    }[sort]

    for r in rows:
        r["metric"] = metric_for(r)
        # Drop the raw metric columns from the response — they are
        # implementation detail of the sort; the tile only uses
        # `metric` + the screen-wide `sort` value.
        r.pop("time_listened_seconds", None)
        r.pop("popularity", None)
        r.pop("added_at", None)

    # Collapse multi-edition release groups to one tile (the primary edition),
    # tagged with edition_count + group_id so the UI routes a group through the
    # release-group screen and a single album straight to the album.
    artist["albums"] = collapse_to_groups(rows, artist_id=str(artist_id))
    artist["albums_sort"] = sort

    # Popular tracks — hybrid rank:
    #   1. Tracks that are popular on Last.fm come first, ordered by
    #      Last.fm playcount. This is the "general / cultural" rank —
    #      what the world considers the artist's hits, useful when
    #      the user hasn't streamed this artist yet.
    #   2. Tracks with no Last.fm data but local plays > 0 fall back
    #      after, ordered by personal play_count. Keeps the block
    #      useful for niche library-only material the user has
    #      actually listened to.
    #   3. Tracks with neither signal are dropped — a tile under a
    #      "Popular" header that is actually random would lie about
    #      the data. If the filter empties the list, the section
    #      hides entirely (see renderArtist on the frontend).
    #
    # Tier ordering and the 5-row cap happen entirely in SQL. The
    # `candidates` CTE dedups media_file variants per track id; the
    # outer SELECT filters tracks with no signal, applies the two
    # tiers and LIMITs to 5.
    artist["popular_tracks"] = db_query("""
        WITH candidates AS (
            SELECT DISTINCT ON (t.id)
                   t.id::text AS track_id,
                   mf.id AS media_file_id,
                   t.title,
                   al.title AS album,
                   mf.duration_seconds AS duration,
                   COALESCE(lps.play_count, 0)::int AS local_plays,
                   COALESCE(ts.playcount, 0)::bigint AS lastfm_playcount
            FROM tracks t
            JOIN track_artists ta ON ta.track_id = t.id
            JOIN media_files mf ON mf.track_id = t.id AND mf.is_analysis_source = true
            JOIN album_variants av ON av.id = mf.album_variant_id
            JOIN albums al ON al.id = av.album_id
            LEFT JOIN local_play_stats lps ON lps.track_id = t.id
            LEFT JOIN track_stats ts
                   ON ts.track_id = t.id AND ts.source = 'lastfm'
            WHERE ta.artist_id = %(id)s::uuid
            ORDER BY t.id, COALESCE(ts.playcount, 0) DESC,
                           COALESCE(lps.play_count, 0) DESC
        )
        SELECT track_id, media_file_id, title, album, duration
        FROM candidates
        WHERE lastfm_playcount > 0 OR local_plays > 0
        ORDER BY
            CASE WHEN lastfm_playcount > 0 THEN 0 ELSE 1 END,
            lastfm_playcount DESC,
            local_plays DESC,
            title
        LIMIT 5
    """, {"id": artist_id})

    # Whole similar-artists list, ordered by the Last.fm match score
    # (0..1). No cap — the row is a horizontal scroll, so showing the
    # full set lets the user explore beyond the obvious top-5 without
    # a separate "see all" screen.
    #
    # SYMMETRIC read: edges are stored directed (Last.fm getSimilar is a
    # per-artist top-20, so A→B often exists while B's own list truncates
    # A away — e.g. Submotion Orchestra→Hidden Orchestra at 0.95 with no
    # reverse edge). Similarity is mutual, so both screens must surface
    # the pair; max(score) wins when both directions exist.
    artist["similar_artists"] = db_query("""
        WITH edges AS (
            SELECT sa.similar_artist_id AS other_id, sa.match_score
            FROM similar_artists sa
            WHERE sa.artist_id = %(id)s::uuid
            UNION ALL
            SELECT sa.artist_id, sa.match_score
            FROM similar_artists sa
            WHERE sa.similar_artist_id = %(id)s::uuid
        )
        SELECT a.id::text AS id,
               a.name,
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
                LIMIT 1) AS media_file_id,
               EXISTS (SELECT 1 FROM media_files mf3
                       JOIN track_artists ta3 ON ta3.track_id = mf3.track_id
                       WHERE ta3.artist_id = a.id) AS is_owned
        FROM (SELECT other_id, MAX(match_score) AS match_score
              FROM edges GROUP BY other_id) e
        JOIN artists a ON a.id = e.other_id
        -- Show only owned or phantom-CANONIZED similars; hide bare phantom-
        -- uncanonized rows (no tracks, no MBID — just a Last.fm name).
        WHERE EXISTS (SELECT 1 FROM track_artists ta4 WHERE ta4.artist_id = a.id)
           OR EXISTS (SELECT 1 FROM artist_mbids am WHERE am.artist_id = a.id)
        ORDER BY e.match_score DESC NULLS LAST, a.name
    """, {"id": artist_id})

    # New albums the user doesn't own — phantom albums derived from the
    # MB-dump discography (canonized artists only). `stale` lets the screen
    # fire a fetch-on-view refresh (once a day) after rendering what's cached.
    artist["new_albums"] = fetch_new_albums(artist_id)
    artist["new_albums_stale"] = _is_discography_stale(last_album_sync)

    return artist


@router.post("/{artist_id}/sync-discography")
def sync_discography(artist_id: str) -> dict:
    """Fetch-on-view refresh of an artist's new-album discovery.

    Gated to once a day per artist (`last_album_sync`): if fresh, returns
    the cached phantom albums as-is. Otherwise runs the MB-dump reconcile
    inline — pure local-DB work, a few indexed joins — and returns the
    refreshed list for an in-place shelf update on the client (no full
    re-render). Non-canonized artists reconcile to an empty shelf and stay
    unstamped, so the first view after canonization syncs immediately."""
    row = db_query_one("""
        SELECT a.name, a.last_album_sync
        FROM artists a
        WHERE a.id = %(id)s::uuid
    """, {"id": artist_id})
    if not row:
        raise HTTPException(status_code=404, detail="artist not found")

    if not _is_discography_stale(row["last_album_sync"]):
        return {"new_albums": fetch_new_albums(artist_id), "synced": False}

    result = sync_artist_discography(artist_id, row["name"])
    return {
        "new_albums": fetch_new_albums(artist_id),
        "synced": result.get("status") == "success",
        "status": result.get("status"),
    }
