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
from urllib.parse import urlsplit
from uuid import UUID, uuid5

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
# Authorship receipt — PER ARTIST (v2, Валерій's design)
# ---------------------------------------------------------------------------
# The slice content is public MB data — it is NOT signed per row. What IS
# signed is one artist's whole subtree: an Ed25519 signature by the DUMP
# node over the canonical per-name blob. v1 signed the whole batch
# response, which welded the signature to one transport exchange — a
# replica could not re-serve a single name without replaying the entire
# original batch byte-for-byte. Per-name signatures align the signing
# grain with the data grain (pending_slice_names, mb_slice_fetches and
# the closed-world rule are all per name already), so the ORIGINAL
# author's signature travels with each name through any number of
# replicas, verified independently by every hop. dump_version and the
# name key live inside the signed bytes — a slice of one dump version
# cannot impersonate another, and a blob signed for one name cannot be
# served under a different one.

# Domain-separation prefix — a receipt signature can never be replayed as
# a chat/sync/birth signature and vice versa. v2 = per-name grain.
RECEIPT_CONTEXT = b"sautium-mb-slice-v2:"


def name_key(name: str) -> str:
    """The per-name identity key — mirrors the mb_slice_fetches key."""
    return (name or "").strip().lower()


def name_blob(name: str, slice_one: dict) -> bytes:
    """Canonical bytes of ONE artist's slice — what the dump node signs
    and every recipient hashes to verify.

    Rows are sorted by their JSON form inside each table: _fetch carries
    no ORDER BY, and Postgres row order is an implementation detail that
    must never leak into a signature. Stored VERBATIM (gzipped) by
    recipients — re-serving hands back these exact bytes, so no
    re-serialization contract with the database is ever needed."""
    core = {
        "name_key": name_key(name),
        "dump_version": slice_one.get("dump_version"),
        "artists_matched": slice_one.get("artists_matched"),
        "truncated": sorted(slice_one.get("truncated") or []),
        "tables": {
            t: sorted(
                (json.dumps(r, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False) for r in rows),
            )
            for t, rows in (slice_one.get("tables") or {}).items()
        },
    }
    return json.dumps(core, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def receipt_message_for(blob: bytes) -> bytes:
    """The exact bytes the dump node signs / every recipient verifies."""
    return RECEIPT_CONTEXT + hashlib.sha256(blob).digest()


# Sautium UUID v5 namespace — mirrors backend/uuid_utils.py (desktop must not
# import the backend package; the constant is fixed by design).
_SAUTIUM_NAMESPACE = UUID("adc1ec0b-2c81-5e26-9938-a369c6f7a5e1")


def addr_uuid(url_or_addr: str) -> str:
    """UUID v5 of the peer HOST (scheme/port stripped), per the project
    formula uuid5(NS, "node_addr:{host}") — deterministic on every node, so
    ban/evidence correlation across nodes stays possible. Pseudonymized
    address for ban lists: the ban anchor is the pubkey (proven by receipts);
    the address id catches a banned key returning under a fresh identity from
    the same place. An IPv4 is enumerable from it by design — uniform
    storage, not privacy. Collisions are irrelevant at this population and
    fail open (a collided ban just skips one more peer)."""
    s = url_or_addr if "://" in url_or_addr else "//" + url_or_addr
    host = (urlsplit(s).hostname or "").lower()
    return str(uuid5(_SAUTIUM_NAMESPACE, f"node_addr:{host}"))


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


SEARCH_LIMIT_MAX = 10


def search_artists(conn, q: str, limit: int = SEARCH_LIMIT_MAX) -> list:
    """P2P search serve path: artist candidates for one interactive query.

    Indexed arms only — the slice matcher's lower/f_unaccent EXACT probes
    (incl. aliases, via the same deterministic variants) plus a lower()
    PREFIX arm riding the trigram GIN indexes. No similarity scan: the
    volunteer-cost rule of the slice path applies to search too, so remote
    search trades typo tolerance for a guaranteed-cheap probe. Namesakes
    all surface; disambiguation is the requester's UI job (comment and
    rg_count travel along, same fields the local scope renders)."""
    q = (q or "").strip()
    if len(q) < 2:
        return []
    limit = max(1, min(int(limit), SEARCH_LIMIT_MAX))
    with conn.cursor() as cur:
        cur.execute("""
            WITH vars AS (
                SELECT lower(x) AS l, f_unaccent(x) AS f
                FROM unnest(%(vs)s::text[]) x
            ),
            exact_ids AS (
                SELECT a.id FROM mb_artist a JOIN vars v ON lower(a.name) = v.l
                UNION
                SELECT al.artist FROM mb_artist_alias al JOIN vars v ON lower(al.name) = v.l
                UNION
                SELECT a.id FROM mb_artist a JOIN vars v ON f_unaccent(a.name) = v.f
                UNION
                SELECT al.artist FROM mb_artist_alias al JOIN vars v ON f_unaccent(al.name) = v.f
            ),
            prefix_ids AS (
                (SELECT a.id FROM mb_artist a
                 WHERE lower(a.name) LIKE lower(%(q)s) || '%%' LIMIT 100)
                UNION
                (SELECT al.artist FROM mb_artist_alias al
                 WHERE lower(al.name) LIKE lower(%(q)s) || '%%' LIMIT 100)
            ),
            best AS (
                SELECT id, MIN(rank) AS rank FROM (
                    SELECT id, 0 AS rank FROM exact_ids
                    UNION ALL
                    SELECT id, 1 FROM prefix_ids
                ) c GROUP BY id
            )
            SELECT a.gid::text, a.name, a.comment,
                   (SELECT COUNT(DISTINCT rg.id)
                    FROM mb_artist_credit_name acn
                    JOIN mb_release_group rg ON rg.artist_credit = acn.artist_credit
                    WHERE acn.artist = a.id) AS rg_count
            FROM best b JOIN mb_artist a ON a.id = b.id
            ORDER BY b.rank, rg_count DESC, a.name
            LIMIT %(limit)s
        """, {"vs": _name_variants(q), "q": q, "limit": limit})
        return [{"gid": r[0], "name": r[1], "comment": r[2],
                 "rg_count": int(r[3] or 0)} for r in cur.fetchall()]


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


def get_slice_one(conn, name: str) -> dict:
    """ONE artist's slice subtree — the v2 signing unit. Batch requests are
    served as a loop of these on the server (with the blob cache in
    between), never as a merged payload: a merged payload cannot carry
    per-name signatures."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")

    with conn.cursor() as cur:
        # The loader holds this lock for its whole TRUNCATE+COPY pass — serving
        # half-truncated tables would poison the requester, so answer 503.
        cur.execute("SELECT pg_try_advisory_lock(%s)", (MB_LOAD_LOCK_KEY,))
        if not cur.fetchone()[0]:
            raise DumpBusy()
        cur.execute("SELECT pg_advisory_unlock(%s)", (MB_LOAD_LOCK_KEY,))

        all_ids = set(_match_artist_ids(cur, name.strip()))
        matched = {name: sorted(all_ids)}

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
    """Artist names the local canon pipeline is waiting on, IN PRIORITY
    ORDER: owned artists past their last_mb_sync watermark
    (canonicalize_pending's feed) first, then phantom stubs without an
    artist_mbids row (phantom canon's feed), minus names already answered
    by a peer (mb_slice_fetches — zero-match included, the slice is a
    closed world per name).

    Ordering is the point. The queue drains ~200 names per cycle against
    thousands of similar-derived phantoms (4033 measured on the reference
    library), so alphabetical order — what this used to do — parked a
    Z-named OWNED artist behind four thousand discovery stubs for days.
    Owned canon is not a nicety: album canon, editions, discographies and
    carry pushability all gate on it. Within the phantom layer the tiers
    mirror discography.stale_canonized_artists — listened-to first, then
    artists similar to those, then the rest — so the part of the
    discovery graph you actually touch resolves first."""
    with conn.cursor() as cur:
        cur.execute("""
            WITH listened AS (
                SELECT DISTINCT ta.artist_id
                FROM track_artists ta
                JOIN listening_history lh ON lh.track_id = ta.track_id
                WHERE ta.role = 'primary'
                  AND lh.started_at > now() - interval '6 months'
            ),
            pending AS (
                SELECT ar.name, 0 AS tier
                FROM artists ar
                WHERE EXISTS (
                    SELECT 1 FROM track_artists ta
                    JOIN media_files mf ON mf.track_id = ta.track_id
                    WHERE ta.artist_id = ar.id AND ta.role = 'primary'
                      AND (ar.last_mb_sync IS NULL OR mf.created_at > ar.last_mb_sync))
                UNION ALL
                SELECT a.name,
                       CASE WHEN EXISTS (SELECT 1 FROM streaming_mints sm
                                          WHERE sm.artist_id = a.id) THEN 1
                            WHEN EXISTS (SELECT 1 FROM similar_artists sa
                                          JOIN listened l ON l.artist_id = sa.artist_id
                                         WHERE sa.similar_artist_id = a.id) THEN 2
                            ELSE 3 END AS tier
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
            SELECT p.name FROM pending p
            WHERE p.name IS NOT NULL AND btrim(p.name) <> ''
              AND NOT EXISTS (SELECT 1 FROM mb_slice_fetches f
                              WHERE f.name_key = lower(btrim(p.name)))
            GROUP BY p.name
            ORDER BY MIN(p.tier), p.name
            LIMIT %(lim)s
        """, {"lim": limit})
        return [r[0] for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# v2 blob cache + replication (the phase-E replacement)
# ---------------------------------------------------------------------------
# One table serves three roles at once. On a DUMP node it is the hot cache:
# a prolific artist (Bach: ~30 MB raw, ~8 s of query time) is computed and
# signed once per dump version, then served as stored bytes. On a REPLICA it
# is the re-serve inventory: verified blobs are kept verbatim with the
# ORIGINAL author's signature, so any number of hops can verify
# independently and no re-serialization contract with the database exists.
# On the wire it is the payload itself — gzip bytes, base64 in JSON.
#
# Replicas skip blobs above RESERVE_BLOB_MAX_GZ: the expensive names are
# exactly the ones every dump node already serves from cache for free, and
# replicating them would bloat every fan's DB with the same megabytes. The
# body of the distribution (tens of KB) is what spreads.

RESERVE_BLOB_MAX_GZ = 2 * 1024 * 1024


def ensure_blob_table(conn) -> None:
    """Idempotent DDL — mirrored in 001_initial.sql; called by both servers
    at startup so a node updated in place gains the table without a manual
    migration."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mb_slice_blobs (
                name_key      TEXT PRIMARY KEY,
                dump_version  TEXT NOT NULL,
                author_pubkey CHAR(64) NOT NULL,
                sig           CHAR(128) NOT NULL,
                blob_gz       BYTEA NOT NULL,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")


def cached_slice(conn, nk: str):
    with conn.cursor() as cur:
        cur.execute("SELECT dump_version, author_pubkey, sig, blob_gz"
                    "  FROM mb_slice_blobs WHERE name_key = %s", (nk,))
        row = cur.fetchone()
    if not row:
        return None
    return {"dump_version": row[0], "author_pubkey": row[1].strip(),
            "sig": row[2].strip(), "blob_gz": bytes(row[3])}


def store_slice_blob(conn, nk: str, dump_version: str, author_pubkey: str,
                     sig: str, blob_gz: bytes, cap: bool = True) -> bool:
    """Upsert one verified blob. `cap=True` is the replica posture (skip
    oversized); a dump node stores its own regardless — that is the cache."""
    if cap and len(blob_gz) > RESERVE_BLOB_MAX_GZ:
        return False
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO mb_slice_blobs
                   (name_key, dump_version, author_pubkey, sig, blob_gz)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (name_key) DO UPDATE SET
                dump_version = EXCLUDED.dump_version,
                author_pubkey = EXCLUDED.author_pubkey,
                sig = EXCLUDED.sig,
                blob_gz = EXCLUDED.blob_gz,
                created_at = now()""",
            (nk, dump_version, author_pubkey, sig, blob_gz))
    return True


def count_slice_blobs(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM mb_slice_blobs")
        return int(cur.fetchone()[0])


def serve_slices(conn, names: list, sign_fn=None,
                 author_pubkey: str = "") -> dict:
    """The shared v2 server body for both surfaces.

    Per name: cached blob wins (dump node's cache and replica's inventory
    are the same table); a dump node computes+signs+caches on a miss
    (`sign_fn` present AND a local dump exists); everything else lands in
    `missing` — the requester takes misses to the next candidate. The
    response entry carries the ORIGINAL author's pubkey+sig, which on a
    replica is not this node's identity — and that is the point."""
    import base64
    import gzip as _gzip

    if not isinstance(names, list) or not names \
            or len(names) > MAX_NAMES_PER_REQUEST:
        raise ValueError(f"names must be a list of 1..{MAX_NAMES_PER_REQUEST}")
    if not all(isinstance(n, str) and n.strip() for n in names):
        raise ValueError("names must be non-empty strings")

    can_build = sign_fn is not None and local_dump_available(conn)
    slices, missing = {}, []
    for name in names:
        nk = name_key(name)
        entry = cached_slice(conn, nk)
        if entry is None and can_build:
            one = get_slice_one(conn, name)          # may raise DumpBusy
            blob = name_blob(name, one)
            sig = sign_fn(receipt_message_for(blob)).hex()
            blob_gz = _gzip.compress(blob)
            store_slice_blob(conn, nk, one.get("dump_version") or "",
                             author_pubkey, sig, blob_gz, cap=False)
            entry = {"dump_version": one.get("dump_version") or "",
                     "author_pubkey": author_pubkey, "sig": sig,
                     "blob_gz": blob_gz}
        if entry is None:
            missing.append(name)
            continue
        slices[name] = {
            "dump_version": entry["dump_version"],
            "author_pubkey": entry["author_pubkey"],
            "sig": entry["sig"],
            "blob_gz": base64.b64encode(entry["blob_gz"]).decode("ascii"),
        }
    return {"v": 2, "slices": slices, "missing": missing}


def verify_slice_entry(name: str, entry: dict):
    """Full per-name verification on the receiving side: gunzip → hash →
    author signature → the blob's own name_key matches the asked name.
    Returns (core_dict, blob_gz_bytes) or None. Every hop runs exactly
    this, against the ORIGINAL author's key."""
    import base64
    import gzip as _gzip

    from desktop.node_identity import verify_signature
    try:
        blob_gz = base64.b64decode(entry.get("blob_gz") or "")
        blob = _gzip.decompress(blob_gz)
        author = entry.get("author_pubkey") or ""
        sig = entry.get("sig") or ""
        if not verify_signature(receipt_message_for(blob),
                                bytes.fromhex(sig), author):
            return None
        core = json.loads(blob)
        if core.get("name_key") != name_key(name):
            return None
        return core, blob_gz
    except Exception:
        return None
