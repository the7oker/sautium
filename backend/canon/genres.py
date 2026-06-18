"""Genre materialization — folded into the canon layer.

MB release-group tags that are real genres → `album_genres(source='mb')`.
`refresh_album_mb_genres` is the full idempotent rebuild (used on dump reload);
`materialize_album_genres` is the targeted variant the discography path calls
when it mints new phantom albums, so a newly-canonized album gets its genres at
creation instead of waiting for the next dump reload.
"""
import logging
from typing import Callable, Dict, List

logger = logging.getLogger(__name__)

ProgressCb = Callable[[dict], None]


def _noop(_evt: dict) -> None:
    pass


def refresh_album_mb_genres(progress_cb: ProgressCb = _noop) -> int:
    """(Re)build album_genres(source='mb') from MusicBrainz release-group tags that
    are real genres (tag.name ∈ the curated genre vocabulary), for every album
    carrying a release-group MBID (albums.musicbrainz_id). count = MB community
    vote count. Genre entities are minted via the same genre_uuid/normalize path
    as file tags, so 'mb' and 'filetag' rows dedupe by genre_id in readers.
    Idempotent: replaces the whole 'mb' source in one transaction."""
    from psycopg2.extras import execute_values
    from db_pool import get_conn
    from uuid_utils import genre_uuid
    from normalize_genres import normalize_genre_name

    with get_conn() as conn:
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                # MB genre vocabulary (~2k) → our normalized genre entities. Only
                # the name→uuid step needs Python; the per-album rollup is in SQL.
                cur.execute("SELECT name FROM mb_genre WHERE name IS NOT NULL")
                tag_lc_to_gid: Dict[str, str] = {}
                genres: Dict[str, str] = {}
                for (name,) in cur.fetchall():
                    norm = normalize_genre_name(name)
                    gid = str(genre_uuid(norm))   # execute_values wants text, not uuid.UUID
                    tag_lc_to_gid[name.lower()] = gid
                    genres[gid] = norm
                if not tag_lc_to_gid:
                    logger.warning("mb_genre empty — skipping album mb-genre refresh")
                    conn.rollback()
                    return 0

                execute_values(cur,
                    "INSERT INTO genres (id, name) VALUES %s ON CONFLICT (id) DO NOTHING",
                    list(genres.items()))

                cur.execute("CREATE TEMP TABLE _mbg (tag_lc text PRIMARY KEY, "
                            "genre_id uuid NOT NULL) ON COMMIT DROP")
                execute_values(cur, "INSERT INTO _mbg (tag_lc, genre_id) VALUES %s",
                               list(tag_lc_to_gid.items()))

                cur.execute("DELETE FROM album_genres WHERE source = 'mb'")
                cur.execute("""
                    INSERT INTO album_genres (album_id, genre_id, source, count)
                    SELECT al.id, m.genre_id, 'mb', MAX(rgt.count)
                    FROM albums al
                    JOIN mb_release_group rg ON rg.gid = al.musicbrainz_id
                    JOIN mb_release_group_tag rgt
                      ON rgt.release_group = rg.id AND rgt.count > 0
                    JOIN mb_tag mt ON mt.id = rgt.tag
                    JOIN _mbg m ON m.tag_lc = lower(mt.name)
                    WHERE al.musicbrainz_id IS NOT NULL
                    GROUP BY al.id, m.genre_id
                    ON CONFLICT (album_id, genre_id, source)
                    DO UPDATE SET count = EXCLUDED.count
                """)
                n = cur.rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True
    logger.info("refreshed album_genres(mb): %d rows", n)
    progress_cb({"phase": "mb_genres", "count": n})
    return n


def materialize_album_genres(album_ids: List[str]) -> int:
    """Targeted album_genres(source='mb') materialization for a specific set of
    albums (e.g. phantom albums just minted by a discography sync). Same RG-tag ∩
    genre rollup as refresh_album_mb_genres but scoped by album id and WITHOUT the
    global 'mb' delete — idempotent ON CONFLICT upsert, safe to call per batch."""
    if not album_ids:
        return 0
    from psycopg2.extras import execute_values
    from db_pool import get_conn
    from uuid_utils import genre_uuid
    from normalize_genres import normalize_genre_name

    ids = [str(a) for a in album_ids]
    with get_conn() as conn:
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute("SELECT name FROM mb_genre WHERE name IS NOT NULL")
                tag_lc_to_gid: Dict[str, str] = {}
                genres: Dict[str, str] = {}
                for (name,) in cur.fetchall():
                    norm = normalize_genre_name(name)
                    gid = str(genre_uuid(norm))
                    tag_lc_to_gid[name.lower()] = gid
                    genres[gid] = norm
                if not tag_lc_to_gid:
                    conn.rollback()
                    return 0

                execute_values(cur,
                    "INSERT INTO genres (id, name) VALUES %s ON CONFLICT (id) DO NOTHING",
                    list(genres.items()))

                cur.execute("CREATE TEMP TABLE _mbg (tag_lc text PRIMARY KEY, "
                            "genre_id uuid NOT NULL) ON COMMIT DROP")
                execute_values(cur, "INSERT INTO _mbg (tag_lc, genre_id) VALUES %s",
                               list(tag_lc_to_gid.items()))

                cur.execute("""
                    INSERT INTO album_genres (album_id, genre_id, source, count)
                    SELECT al.id, m.genre_id, 'mb', MAX(rgt.count)
                    FROM albums al
                    JOIN mb_release_group rg ON rg.gid = al.musicbrainz_id
                    JOIN mb_release_group_tag rgt
                      ON rgt.release_group = rg.id AND rgt.count > 0
                    JOIN mb_tag mt ON mt.id = rgt.tag
                    JOIN _mbg m ON m.tag_lc = lower(mt.name)
                    WHERE al.id = ANY(%(ids)s::uuid[]) AND al.musicbrainz_id IS NOT NULL
                    GROUP BY al.id, m.genre_id
                    ON CONFLICT (album_id, genre_id, source)
                    DO UPDATE SET count = EXCLUDED.count
                """, {"ids": ids})
                n = cur.rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True
    return n
