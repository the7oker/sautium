"""
P2P MB dump slice queries — facts, not verdicts.

A dump-holding node answers a batch of artist names with raw mb_* table rows:
every exact/alias/unaccent name match (all namesakes — the slice is a CLOSED
WORLD per queried name) plus each matched artist's full discography subtree.
The requester inserts the rows into its own mb_* tables, after which the
existing canon pipeline (content resolve, editions, phantom, AI tier) runs
unchanged against the partial local dump. No trigram fuzzy on the serve path —
recall lost to variant spellings is the accepted cost of keeping volunteer
dump nodes cheap (every probe here is an indexed exact lookup).

Framework-agnostic like sync_queries: functions take a psycopg2 connection.
"""

import hashlib
import json
import logging
import os
import re
from datetime import date, datetime
from uuid import UUID

logger = logging.getLogger(__name__)

# Mirrors backend/mb_dump_load.py MB_LOAD_LOCK_KEY — the full-dump loader holds
# this advisory lock for its whole TRUNCATE+COPY pass; slice reads/writes must
# not interleave with it.
MB_LOAD_LOCK_KEY = 0x6D626C64

MAX_NAMES_PER_REQUEST = 50

# Oversized-artist guard: a prolific matched set (think "Mozart" namesakes) can
# explode the release/track walk. Responses carry a `truncated` list so the
# requester logs the gap; imperfect canon is an accepted compromise.
TABLE_ROW_LIMITS = {
    "mb_release": 100_000,
    "mb_medium": 150_000,
    "mb_track": 300_000,
    "mb_recording": 300_000,
}

# Wire-format source of truth, shared by the serving side (SELECT column list)
# and the importer (INSERT column list + response validation). Column order
# mirrors desktop/migrations/001_initial.sql / MB CreateTables.sql. The
# importer must never build SQL from server-sent identifiers — only from this
# constant.
SLICE_TABLES = {
    "mb_area": [
        "id", "gid", "name", "type", "edits_pending", "last_updated",
        "begin_date_year", "begin_date_month", "begin_date_day",
        "end_date_year", "end_date_month", "end_date_day", "ended", "comment",
    ],
    "mb_artist": [
        "id", "gid", "name", "sort_name",
        "begin_date_year", "begin_date_month", "begin_date_day",
        "end_date_year", "end_date_month", "end_date_day",
        "type", "area", "gender", "comment", "edits_pending", "last_updated",
        "ended", "begin_area", "end_area",
    ],
    "mb_artist_alias": [
        "id", "artist", "name", "locale", "edits_pending", "last_updated",
        "type", "sort_name",
        "begin_date_year", "begin_date_month", "begin_date_day",
        "end_date_year", "end_date_month", "end_date_day",
        "primary_for_locale", "ended",
    ],
    "mb_artist_credit": [
        "id", "name", "artist_count", "ref_count", "created",
        "edits_pending", "gid",
    ],
    "mb_artist_credit_name": [
        "artist_credit", "position", "artist", "name", "join_phrase",
    ],
    "mb_release_group_primary_type": [
        "id", "name", "parent", "child_order", "description", "gid",
    ],
    "mb_release_group_secondary_type": [
        "id", "name", "parent", "child_order", "description", "gid",
    ],
    "mb_release_group": [
        "id", "gid", "name", "artist_credit", "type", "comment",
        "edits_pending", "last_updated",
    ],
    "mb_release_group_secondary_type_join": [
        "release_group", "secondary_type", "created",
    ],
    "mb_release": [
        "id", "gid", "name", "artist_credit", "release_group", "status",
        "packaging", "language", "script", "barcode", "comment",
        "edits_pending", "quality", "last_updated",
    ],
    "mb_release_country": [
        "release", "country", "date_year", "date_month", "date_day",
    ],
    "mb_release_unknown_country": [
        "release", "date_year", "date_month", "date_day",
    ],
    "mb_medium": [
        "id", "release", "position", "format", "name", "edits_pending",
        "last_updated", "track_count", "gid",
    ],
    "mb_track": [
        "id", "gid", "recording", "medium", "position", "number", "name",
        "artist_credit", "length", "edits_pending", "last_updated",
        "is_data_track",
    ],
    "mb_recording": [
        "id", "gid", "name", "artist_credit", "length", "comment",
        "edits_pending", "last_updated", "video",
    ],
    "mb_tag": ["id", "name", "ref_count"],
    "mb_artist_tag": ["artist", "tag", "count", "last_updated"],
    "mb_release_group_tag": ["release_group", "tag", "count", "last_updated"],
}

# Type dictionaries: tiny, shipped whole in every response (idempotent on import).
_DICT_TABLES = ("mb_release_group_primary_type", "mb_release_group_secondary_type")


class DumpBusy(Exception):
    """A full dump load is in progress on the serving node — retry later."""


# ---------------------------------------------------------------------------
# Authorship receipt
# ---------------------------------------------------------------------------
# The slice content is public MB data — it is NOT signed per row. What IS
# signed is the whole response: one Ed25519 signature by the serving node
# over the canonical payload hash. That binds authorship cryptographically
# ("node with key K produced answer H for dump V") without any per-row keys,
# timestamps or Worker involvement. The requester verifies before importing
# and stores (pubkey, receipt, hash) in mb_slice_fetches as durable evidence.

# Domain-separation prefix — a receipt signature can never be replayed as a
# chat/sync/birth signature and vice versa.
RECEIPT_CONTEXT = b"sautium-mb-slice-v1:"

_SIGNED_FIELDS = ("dump_version", "artists_matched", "columns", "tables",
                  "truncated")


def payload_hash(resp: dict) -> bytes:
    """Deterministic sha256 over the signable core of a slice response.
    The server computes it from the dict it built, the requester from the
    parsed JSON — canonical serialization (sorted keys, compact separators,
    ensure_ascii=False) makes both byte-identical."""
    core = {k: resp.get(k) for k in _SIGNED_FIELDS}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).digest()


def receipt_message(resp: dict) -> bytes:
    """The exact bytes the serving node signs / the requester verifies."""
    return RECEIPT_CONTEXT + payload_hash(resp)


# ---------------------------------------------------------------------------
# Capability (is THIS node a dump holder?)
# ---------------------------------------------------------------------------

def dump_dir() -> str:
    """Replicates backend/mb_dump_load.py path resolution (desktop must not
    import the backend package)."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.environ.get("MB_DUMP_DIR") or os.path.normpath(
        os.path.join(here, "..", "..", "data", "mbdump"))


def dump_version_file():
    """The VERSION marker content, or None. Stamped only by a successful FULL
    dump load — slice imports never create it, so a slice-holding node can
    never advertise itself as a dump holder."""
    try:
        with open(os.path.join(dump_dir(), "VERSION")) as f:
            return f.read().strip() or None
    except OSError:
        return None


def local_dump_available(conn):
    """Dump version string if this node can serve slices, else None. Requires
    BOTH the VERSION marker (full load completed) AND mb_artist rows on the
    serve DSN — the marker is filesystem-local while the DB is per-DSN, and
    the two can diverge (e.g. dump loaded into a different database)."""
    version = dump_version_file()
    if not version:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM mb_artist LIMIT 1")
        if cur.fetchone() is None:
            return None
    return version


# ---------------------------------------------------------------------------
# Serving side
# ---------------------------------------------------------------------------

def _ser(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, UUID):
        return str(v)
    return v


# MB's style guide stores apostrophes as U+2019 and hyphens as U+2010 while
# library tags use ASCII — mirror of canon/match.py's _mb_spelling (desktop
# must not import the backend package).
_MB_PUNCT = str.maketrans({"'": "’", "`": "’", "´": "’",
                           "-": "‐"})


def _name_variants(name: str) -> list:
    """`name` plus deterministic same-entity spelling variants (mirror of
    canon/match.py _whole_variants + _mb_spelling): ' & '<->' and ' separator
    swaps and MB punctuation respelling. Every variant is exact-probed on the
    same indexes — the requester's canon probes these variants locally, so a
    variant the server never matched would be a permanent hole in the slice."""
    n = " ".join(name.split())
    out = [n]
    amp = re.sub(r"\s+&\s+", " and ", n)
    if amp != n:
        out.append(amp)
    a_nd = re.sub(r"\s+and\s+", " & ", n, flags=re.IGNORECASE)
    if a_nd != n:
        out.append(a_nd)
    respelled = n.translate(_MB_PUNCT)
    if respelled != n:
        out.append(respelled)
    return out


def _match_artist_ids(cur, name: str) -> list:
    """All mb_artist ids whose name/alias equals `name` or a deterministic
    spelling variant of it — lower-exact and f_unaccent-exact, every probe on
    an existing index. No trigram."""
    cur.execute("""
        WITH vars AS (
            SELECT lower(x) AS l, f_unaccent(x) AS f
            FROM unnest(%(vs)s::text[]) x
        )
        SELECT a.id FROM mb_artist a JOIN vars v ON lower(a.name) = v.l
        UNION
        SELECT al.artist FROM mb_artist_alias al JOIN vars v ON lower(al.name) = v.l
        UNION
        SELECT a.id FROM mb_artist a JOIN vars v ON f_unaccent(a.name) = v.f
        UNION
        SELECT al.artist FROM mb_artist_alias al JOIN vars v ON f_unaccent(al.name) = v.f
    """, {"vs": _name_variants(name)})
    return [r[0] for r in cur.fetchall()]


def _fetch(cur, table: str, where_sql: str, params, truncated: list):
    """Rows of `table` (SLICE_TABLES column order) matching `where_sql`.
    Applies the per-table row cap and records overflow in `truncated`."""
    cols = ", ".join(SLICE_TABLES[table])
    limit = TABLE_ROW_LIMITS.get(table)
    sql = f"SELECT {cols} FROM {table} WHERE {where_sql}"
    if limit:
        sql += f" LIMIT {limit + 1}"
    cur.execute(sql, params)
    rows = cur.fetchall()
    if limit and len(rows) > limit:
        truncated.append(table)
        rows = rows[:limit]
    return [[_ser(v) for v in row] for row in rows]


def get_slice(conn, names: list) -> dict:
    """The /api/mb/slice payload for a batch of artist names."""
    if not isinstance(names, list) or not names or len(names) > MAX_NAMES_PER_REQUEST:
        raise ValueError(f"names must be a list of 1..{MAX_NAMES_PER_REQUEST}")
    if not all(isinstance(n, str) and n.strip() for n in names):
        raise ValueError("names must be non-empty strings")

    with conn.cursor() as cur:
        # The loader holds this lock for its whole TRUNCATE+COPY pass — serving
        # half-truncated tables would poison the requester, so answer 503.
        cur.execute("SELECT pg_try_advisory_lock(%s)", (MB_LOAD_LOCK_KEY,))
        if not cur.fetchone()[0]:
            raise DumpBusy()
        cur.execute("SELECT pg_advisory_unlock(%s)", (MB_LOAD_LOCK_KEY,))

        matched, all_ids = {}, set()
        for name in names:
            ids = _match_artist_ids(cur, name.strip())
            matched[name] = ids
            all_ids.update(ids)

        version = dump_version_file()
        if not all_ids:
            return {"dump_version": version, "artists_matched": matched,
                    "columns": {}, "tables": {}, "truncated": []}

        truncated: list = []
        tables: dict = {}
        a_ids = sorted(all_ids)

        tables["mb_artist"] = _fetch(cur, "mb_artist", "id = ANY(%s)", (a_ids,), truncated)
        tables["mb_artist_alias"] = _fetch(
            cur, "mb_artist_alias", "artist = ANY(%s)", (a_ids,), truncated)

        cur.execute("SELECT DISTINCT artist_credit FROM mb_artist_credit_name"
                    " WHERE artist = ANY(%s)", (a_ids,))
        ac_ids = sorted(r[0] for r in cur.fetchall())

        tables["mb_artist_credit"] = _fetch(
            cur, "mb_artist_credit", "id = ANY(%s)", (ac_ids,), truncated)
        # ALL members of every touched credit, not just the queried artists —
        # multi-artist credits must arrive whole for credit→artist joins.
        tables["mb_artist_credit_name"] = _fetch(
            cur, "mb_artist_credit_name", "artist_credit = ANY(%s)", (ac_ids,), truncated)

        # Bare mb_artist rows for co-credited collaborators (no subtree): cheap
        # PK lookups that keep acn.artist joins and the artist_mbids meta
        # trigger working. Their own slices are still requested normally — the
        # provenance log only records the names actually queried.
        acn_artist_col = SLICE_TABLES["mb_artist_credit_name"].index("artist")
        collab_ids = sorted({row[acn_artist_col]
                             for row in tables["mb_artist_credit_name"]} - all_ids)
        if collab_ids:
            tables["mb_artist"] += _fetch(
                cur, "mb_artist", "id = ANY(%s)", (collab_ids,), truncated)

        tables["mb_release_group"] = _fetch(
            cur, "mb_release_group", "artist_credit = ANY(%s)", (ac_ids,), truncated)
        rg_id_col = SLICE_TABLES["mb_release_group"].index("id")
        rg_ids = sorted(row[rg_id_col] for row in tables["mb_release_group"])

        tables["mb_release_group_secondary_type_join"] = _fetch(
            cur, "mb_release_group_secondary_type_join",
            "release_group = ANY(%s)", (rg_ids,), truncated)

        tables["mb_release"] = _fetch(
            cur, "mb_release", "release_group = ANY(%s)", (rg_ids,), truncated)
        r_id_col = SLICE_TABLES["mb_release"].index("id")
        r_ids = sorted(row[r_id_col] for row in tables["mb_release"])

        tables["mb_release_country"] = _fetch(
            cur, "mb_release_country", "release = ANY(%s)", (r_ids,), truncated)
        tables["mb_release_unknown_country"] = _fetch(
            cur, "mb_release_unknown_country", "release = ANY(%s)", (r_ids,), truncated)

        tables["mb_medium"] = _fetch(
            cur, "mb_medium", "release = ANY(%s)", (r_ids,), truncated)
        m_id_col = SLICE_TABLES["mb_medium"].index("id")
        m_ids = sorted(row[m_id_col] for row in tables["mb_medium"])

        tables["mb_track"] = _fetch(
            cur, "mb_track", "medium = ANY(%s)", (m_ids,), truncated)
        rec_col = SLICE_TABLES["mb_track"].index("recording")
        rec_ids = sorted({row[rec_col] for row in tables["mb_track"]
                          if row[rec_col] is not None})

        # Track-referenced recordings PLUS standalone recordings credited to
        # the matched artists — the content-resolve candidate pool matches
        # recordings by artist credit, not only via release tracklists.
        recordings = _fetch(cur, "mb_recording", "id = ANY(%s)", (rec_ids,), truncated)
        rec_id_col = SLICE_TABLES["mb_recording"].index("id")
        seen_rec = {row[rec_id_col] for row in recordings}
        for row in _fetch(cur, "mb_recording", "artist_credit = ANY(%s)",
                          (ac_ids,), truncated):
            if row[rec_id_col] not in seen_rec:
                seen_rec.add(row[rec_id_col])
                recordings.append(row)
        tables["mb_recording"] = recordings

        tables["mb_artist_tag"] = _fetch(
            cur, "mb_artist_tag", "artist = ANY(%s) AND count > 0", (a_ids,), truncated)
        tables["mb_release_group_tag"] = _fetch(
            cur, "mb_release_group_tag", "release_group = ANY(%s)", (rg_ids,), truncated)
        tag_ids = sorted(
            {row[SLICE_TABLES["mb_artist_tag"].index("tag")]
             for row in tables["mb_artist_tag"]} |
            {row[SLICE_TABLES["mb_release_group_tag"].index("tag")]
             for row in tables["mb_release_group_tag"]})
        tables["mb_tag"] = _fetch(cur, "mb_tag", "id = ANY(%s)", (tag_ids,), truncated)

        area_col = SLICE_TABLES["mb_artist"].index("area")
        area_ids = sorted({row[area_col] for row in tables["mb_artist"]
                           if row[area_col] is not None})
        tables["mb_area"] = _fetch(cur, "mb_area", "id = ANY(%s)", (area_ids,), truncated)

        for t in _DICT_TABLES:
            tables[t] = _fetch(cur, t, "TRUE", None, truncated)

    return {
        "dump_version": version,
        "artists_matched": matched,
        "columns": {t: SLICE_TABLES[t] for t in tables},
        "tables": tables,
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# Requester side — which names still need a slice
# ---------------------------------------------------------------------------

def pending_slice_names(conn, limit: int = 200) -> list:
    """Artist names the local canon pipeline is waiting on: owned artists past
    their last_mb_sync watermark (canonicalize_pending's feed) plus phantom
    stubs without an artist_mbids row (phantom canon's feed), minus names
    already answered by a peer (mb_slice_fetches — zero-match included, the
    slice is a closed world per name)."""
    with conn.cursor() as cur:
        cur.execute("""
            WITH pending AS (
                SELECT ar.name
                FROM artists ar
                WHERE EXISTS (
                    SELECT 1 FROM track_artists ta
                    JOIN media_files mf ON mf.track_id = ta.track_id
                    WHERE ta.artist_id = ar.id AND ta.role = 'primary'
                      AND (ar.last_mb_sync IS NULL OR mf.created_at > ar.last_mb_sync))
                UNION
                SELECT a.name
                FROM artists a
                WHERE ((EXISTS (SELECT 1 FROM similar_artists sa
                                WHERE sa.similar_artist_id = a.id)
                        AND NOT EXISTS (SELECT 1 FROM track_artists ta
                                        WHERE ta.artist_id = a.id))
                       OR EXISTS (SELECT 1 FROM streaming_mints sm
                                  WHERE sm.artist_id = a.id))
                  AND NOT EXISTS (SELECT 1 FROM artist_mbids am
                                  WHERE am.artist_id = a.id)
            )
            SELECT DISTINCT p.name FROM pending p
            WHERE p.name IS NOT NULL AND btrim(p.name) <> ''
              AND NOT EXISTS (SELECT 1 FROM mb_slice_fetches f
                              WHERE f.name_key = lower(btrim(p.name)))
            ORDER BY p.name
            LIMIT %(lim)s
        """, {"lim": limit})
        return [r[0] for r in cur.fetchall()]
