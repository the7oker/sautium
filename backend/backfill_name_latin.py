"""Backfill name_latin / title_latin for rows minted before Phase 0a.

Idempotent: only touches rows whose Latin form is still NULL, so re-running is
cheap and resumes after interruption. Write-time population (the ORM event in
models.py + raw choke-points in canon/lastfm/discography) keeps new rows filled;
this is the one-time catch-up.

`backfill_owned` runs in a single pass — owned counts are small (~25k artists,
~3.7k albums, ~37k tracks) and immediately useful for cross-script /artists.
`backfill_phantom` drains the millions of phantom rows in bounded id-cursor
batches, for the background loop (it never re-scans, so blank-name rows that
latinize to NULL don't wedge it).
"""
import logging
from typing import Optional

from psycopg2.extras import execute_values

from db_pool import get_conn, db_query
from transliterate import latinize

logger = logging.getLogger(__name__)

# table → (source column, latin column, "owned" predicate on alias t)
_SPEC = {
    "artists": ("name", "name_latin",
                "EXISTS (SELECT 1 FROM track_artists ta JOIN media_files mf "
                "ON mf.track_id = ta.track_id WHERE ta.artist_id = t.id)"),
    "albums":  ("title", "title_latin",
                "EXISTS (SELECT 1 FROM album_variants av WHERE av.album_id = t.id)"),
    "tracks":  ("title", "title_latin",
                "EXISTS (SELECT 1 FROM media_files mf WHERE mf.track_id = t.id)"),
}


def _write_batch(table: str, latin_col: str, pairs: list[tuple]) -> None:
    """UPDATE a batch via VALUES join. pairs = [(id_text, latin_or_None), ...]."""
    if not pairs:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            execute_values(
                cur,
                f"UPDATE {table} SET {latin_col} = v.nl "
                f"FROM (VALUES %s) AS v(id, nl) WHERE {table}.id = v.id::uuid",
                pairs, template="(%s, %s)",
            )
        conn.commit()


def backfill_owned(table: str) -> int:
    """Fill the Latin column for every owned row that lacks one. Single pass."""
    src, latin_col, owned = _SPEC[table]
    rows = db_query(
        f"SELECT t.id::text AS id, t.{src} AS nm FROM {table} t "
        f"WHERE t.{latin_col} IS NULL AND t.{src} IS NOT NULL AND {owned}"
    )
    pairs = [(r["id"], latinize(r["nm"])) for r in rows]
    for i in range(0, len(pairs), 2000):
        _write_batch(table, latin_col, pairs[i:i + 2000])
    logger.info("backfilled %s.%s for %d owned rows", table, latin_col, len(pairs))
    return len(pairs)


def backfill_phantom(table: str, limit: Optional[int] = None, batch: int = 2000) -> int:
    """Fill the Latin column for phantom (non-owned) rows in id-cursor batches.

    Cursors on id (not the NULL predicate) so rows whose name latinizes to NULL
    don't re-appear and wedge the loop. `limit` caps a single background tick.
    """
    src, latin_col, owned = _SPEC[table]
    done, cursor = 0, "00000000-0000-0000-0000-000000000000"
    while True:
        rows = db_query(
            f"SELECT t.id::text AS id, t.{src} AS nm FROM {table} t "
            f"WHERE t.id > %(c)s::uuid AND t.{latin_col} IS NULL "
            f"AND t.{src} IS NOT NULL AND NOT {owned} "
            f"ORDER BY t.id LIMIT %(b)s",
            {"c": cursor, "b": batch},
        )
        if not rows:
            break
        _write_batch(table, latin_col, [(r["id"], latinize(r["nm"])) for r in rows])
        done += len(rows)
        cursor = rows[-1]["id"]
        if limit and done >= limit:
            break
    logger.info("backfilled %s.%s for %d phantom rows", table, latin_col, done)
    return done


def backfill_aliases(limit: Optional[int] = None) -> int:
    """Populate artist_name_aliases (Phase 0b) with alternate Latin readings.

    Alt readings only apply to kana-less Han (中森明菜 = Nakamori Akina in
    Japanese vs a Chinese reading), so scan Han-containing artist names and store
    the Japanese one alongside the pinyin name_latin. Idempotent: skips artists
    that already have an alias row. Han names are few, so a single pass — no cursor.
    """
    from transliterate import latin_alt_forms

    rows = db_query(
        "SELECT a.id::text AS id, a.name AS nm FROM artists a "
        "WHERE a.name ~ '[一-鿿㐀-䶿]' "
        "AND NOT EXISTS (SELECT 1 FROM artist_name_aliases al WHERE al.artist_id = a.id)"
        + (f" LIMIT {int(limit)}" if limit else "")
    )
    pairs = [(r["id"], alt) for r in rows for alt in latin_alt_forms(r["nm"])]
    if pairs:
        with get_conn() as conn:
            with conn.cursor() as cur:
                execute_values(
                    cur,
                    "INSERT INTO artist_name_aliases (artist_id, alias_latin) "
                    "VALUES %s ON CONFLICT DO NOTHING",
                    pairs, template="(%s::uuid, %s)",
                )
            conn.commit()
    logger.info("backfilled %d aliases from %d Han artists", len(pairs), len(rows))
    return len(pairs)


_JUNK_TAGS = {"unknown", "unknown artist", "various", "various artists",
              "va", "artist", "traditional", "soundtrack"}


def _is_junk_tag(s: str) -> bool:
    return (s in _JUNK_TAGS or s.isdigit()
            or any(sep in s for sep in (" feat ", " ft ", " vs ")))


def backfill_filetag_aliases(limit: Optional[int] = None) -> int:
    """Human-tagged Latin readings from owned file tags (media_files.raw_artist).

    Collectors often tag a non-Latin artist in Latin (三宅純 tagged "Jun Miyake"),
    which beats machine transliteration — especially CJK name order. Kept only when
    latinize(tag) differs from name_latin: accented-Latin tags just duplicate
    anyascii, so the diff filter retains a tag only where the human adds real value.
    Owned-only (phantoms have no file tags). Stored with source='filetag' so its
    provenance stays separable from the cutlet aliases."""
    from transliterate import latinize

    rows = db_query(
        "SELECT DISTINCT a.id::text AS id, a.name_latin AS nl, mf.raw_artist AS tag "
        "FROM artists a "
        "JOIN track_artists ta ON ta.artist_id = a.id AND ta.role = 'primary' "
        "JOIN media_files mf ON mf.track_id = ta.track_id "
        "WHERE octet_length(a.name) <> length(a.name) "
        "AND mf.raw_artist IS NOT NULL "
        "AND octet_length(mf.raw_artist) = length(mf.raw_artist) "
        "AND length(mf.raw_artist) BETWEEN 2 AND 200"
        + (f" LIMIT {int(limit)}" if limit else "")
    )
    pairs = []
    for r in rows:
        tag_latin = latinize(r["tag"])
        if not tag_latin or tag_latin == r["nl"] or _is_junk_tag(tag_latin):
            continue
        pairs.append((r["id"], tag_latin))
    if pairs:
        with get_conn() as conn:
            with conn.cursor() as cur:
                execute_values(
                    cur,
                    "INSERT INTO artist_name_aliases (artist_id, alias_latin, source) "
                    "VALUES %s ON CONFLICT DO NOTHING",
                    pairs, template="(%s::uuid, %s, 'filetag')",
                )
            conn.commit()
    logger.info("backfilled %d filetag aliases", len(pairs))
    return len(pairs)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for tbl in ("artists", "albums", "tracks"):
        print(f"{tbl}: {backfill_owned(tbl)} owned rows")
    print(f"cutlet aliases: {backfill_aliases()} rows")
    print(f"filetag aliases: {backfill_filetag_aliases()} rows")
