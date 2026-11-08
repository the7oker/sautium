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

import gzip
import hashlib
import ipaddress
import json
import logging
import os
import re
import threading
import time
import unicodedata
from collections import deque
from datetime import date, datetime
from typing import Optional
from urllib.parse import urlsplit
from uuid import UUID, uuid5

logger = logging.getLogger(__name__)

# Mirrors backend/mb_dump_load.py MB_LOAD_LOCK_KEY — the full-dump loader holds
# this advisory lock EXCLUSIVE for its whole TRUNCATE+COPY pass; slice reads and
# writes take it SHARED — never beside a load, never in a canon run's way.
MB_LOAD_LOCK_KEY = 0x6D626C64

MAX_NAMES_PER_REQUEST = 50

# What one received blob may inflate to: TABLE_ROW_LIMITS keep an honest
# subtree near 30 MB raw at its largest (one prolific artist, measured), and
# the author's bytes are inflated before their signature can be checked. A
# dump node builds nothing larger: no requester would take it.
BLOB_RAW_MAX = 64 * 1024 * 1024

# What a dump node builds a slice for: a name of at most NAME_MAX_CHARS with
# no control character, and not one of MB's special-purpose artists
# ([unknown], [traditional], Various Artists…), whose subtree is every record
# on Earth — the requester never asks for those (pending_slice_names). Any
# other name in a request is `missing` in the answer.
NAME_MAX_CHARS = 255
_SPECIAL_PURPOSE = re.compile(r"^\[.*\]$|^various artists$")

# A dump node's builds, node-wide: BUILD_CONCURRENCY at once and
# BUILDS_PER_MINUTE in any minute, a quarter of those for any one requester;
# a miss past either is `missing` for that answer, and the requester asks
# elsewhere or later. A stored blob is served whatever the budget. Per match
# set (the matched artist ids, `match_key`) one subtree is built and at most
# VARIANTS_PER_MATCH spellings are kept; any other spelling is cut from that
# stored subtree and served unkept, and a zero-match is served signed and
# never kept.
BUILD_CONCURRENCY = 2
BUILDS_PER_MINUTE = 120
VARIANTS_PER_MATCH = 4

# The blobs one answer carries, in their base64 bytes: under the 128 MiB a
# requester reads (desktop/api_client._PEER_BOUNDS) with room for one blob
# at the largest a build makes, so an honest batch of prolific names is cut,
# never refused whole. One name_key is answered once per request. What is cut
# is `missing`, and the requester asks for it again.
ANSWER_MAX_BYTES = 16 * 1024 * 1024

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
    # URL relationships, Bandcamp only (mb_dump_load._ROW_FILTERS) — the
    # phantom album's Buy link, resolved offline on the requester. v3.
    "mb_url": ["id", "gid", "url", "edits_pending", "last_updated"],
    "mb_l_artist_url": [
        "id", "link", "entity0", "entity1", "edits_pending", "last_updated",
        "link_order", "entity0_credit", "entity1_credit",
    ],
    "mb_l_release_url": [
        "id", "link", "entity0", "entity1", "edits_pending", "last_updated",
        "link_order", "entity0_credit", "entity1_credit",
    ],
    "mb_tag": ["id", "name", "ref_count"],
    "mb_artist_tag": ["artist", "tag", "count", "last_updated"],
    "mb_release_group_tag": ["release_group", "tag", "count", "last_updated"],
}

# Type dictionaries: tiny, shipped whole in every response (idempotent on import).
_DICT_TABLES = ("mb_release_group_primary_type", "mb_release_group_secondary_type")

# The one store a Buy button opens (docs/design/PHANTOM-DISCOVERY.md D5): a
# Bandcamp shop's own page. daily.bandcamp.com is editorial, bare
# bandcamp.com is the site itself. The rule for every mb_url row this node
# keeps — the dump loader's (backend/mb_dump_load), a slice's import, the
# Buy resolver's (backend/routers/albums._buy_link).
BANDCAMP_PAGE = r"^https?://(?!daily\.)[a-z0-9-]+\.bandcamp\.com/"
_BANDCAMP_PAGE = re.compile(BANDCAMP_PAGE, re.IGNORECASE)


def bandcamp_page(url) -> bool:
    return isinstance(url, str) and _BANDCAMP_PAGE.match(url) is not None


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
# a chat/sync/birth signature and vice versa. v2 = per-name grain; v3
# (2026-09-17) = the url subtree rides in the blob (SLICE_TABLES mb_url,
# mb_l_artist_url, mb_l_release_url). The bump is deliberate: a v2 blob is
# a signed statement WITHOUT those tables, and a signed hole closes a name
# for good on the requester — so a v3 requester must not verify one.
# Migration 016 empties every node's blob cache and provenance log.
RECEIPT_CONTEXT = b"sautium-mb-slice-v3:"
# v4 (2026-11-07): any dump holder may author slices, so an entry carries its
# author's birth certificate (author_cert, and author_proof for `pow`); the
# signed bytes are v3's. A node passes a blob on only once its own check
# against MusicBrainz ran (desktop/p2p/slice_checks.py).
PROTOCOL_VERSION = 4


def name_key(name: str) -> str:
    """The per-name identity key of a slice: its blob, its ledger row
    (mb_slice_fetches), its index rows and clashes are all keyed by it."""
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


def servable_name(name: str) -> bool:
    """A name a slice is served for (NAME_MAX_CHARS, no control character
    nor lone surrogate — no text an answer can carry —, not a
    special-purpose artist)."""
    n = name.strip()
    return (0 < len(n) <= NAME_MAX_CHARS
            and not any(unicodedata.category(c) in ("Cc", "Cs") for c in n)
            and not _SPECIAL_PURPOSE.match(n.lower()))


def match_key(artist_ids) -> str:
    """The key of what a slice covers — its matched artist ids. Names that
    fold to one match set (case, spacing, accents, "&"/"and", MB's
    punctuation) share it, whatever their spelling."""
    return hashlib.sha256(",".join(str(i) for i in sorted(artist_ids)).encode()).hexdigest()


class BuildBudget:
    """A dump node's slice builds, node-wide (both families keep one each):
    at most `concurrency` at once and `per_minute` in any minute, of which
    a quarter at most for any one requester — one address never spends the
    minute for the rest. Only a build that found something counts against
    the minute: a name or an id nothing matches costs a few index probes,
    and the slice request windows bound those. admit() never waits — a
    build past the budget is a miss in that answer."""

    def __init__(self, concurrency: int, per_minute: int):
        self._slots = threading.BoundedSemaphore(concurrency)
        self._lock = threading.Lock()
        self._times: deque = deque()         # (when, requester) of each counted build
        self._by: dict = {}                  # requester -> its counted builds in the minute
        self._per_minute = per_minute
        self._per_requester = max(1, per_minute // 4)

    def admit(self, requester: Optional[str]) -> bool:
        """`requester` None: one that stands for many (a shared address),
        bounded by the minute's total alone."""
        now = time.monotonic()
        with self._lock:
            while self._times and now - self._times[0][0] >= 60:
                _, who = self._times.popleft()
                if who is None:
                    continue
                if self._by[who] == 1:
                    del self._by[who]
                else:
                    self._by[who] -= 1
            if (len(self._times) >= self._per_minute
                    or (requester is not None
                        and self._by.get(requester, 0) >= self._per_requester)):
                return False
        return self._slots.acquire(blocking=False)

    def done(self, requester: Optional[str], counted: bool) -> None:
        if counted:
            with self._lock:
                self._times.append((time.monotonic(), requester))
                if requester is not None:
                    self._by[requester] = self._by.get(requester, 0) + 1
        self._slots.release()


_builds = BuildBudget(BUILD_CONCURRENCY, BUILDS_PER_MINUTE)


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
    fail open (a collided ban just skips one more peer).

    A bare IP — what scope["client"] / request.remote carry — bypasses the
    URL parser: unbracketed IPv6 reads as host "2001" ("2001:db8::1") or as
    no host at all ("::1"), which folded every IPv6 peer into one token in
    the identity registry while contact_log kept them apart. Same lowercase
    raw string as contact_log.addr_ids, so both tables agree."""
    from desktop.p2p.addrs import canon_host
    try:
        ipaddress.ip_address(url_or_addr)
        host = url_or_addr
    except ValueError:
        s = url_or_addr if "://" in url_or_addr else "//" + url_or_addr
        host = urlsplit(s).hostname or ""
    # canon_host: IPv6 has many spellings of one address (2a00:0DB8::1 =
    # 2a00:db8::1 = [2a00:db8::1]) — hashing the raw string would fan one
    # peer into several pseudonyms. v4 strings pass through unchanged.
    return str(uuid5(_SAUTIUM_NAMESPACE, f"node_addr:{canon_host(host)}"))


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


# Full-load completion marker, written by the loader INTO THE DATABASE.
# Neither the VERSION file nor mb_artist rows alone prove a full dump on
# this DSN: the file is filesystem-local and SHARED between runtimes on a
# dev host (Docker stamps it through the repo bind-mount while the
# launcher's PG stays empty), and P2P slice imports populate mb_artist on
# dump-less nodes. Either alone let a slice-partial world advertise itself
# as a full dump and answer interactive search with "not found" for every
# name it never saw (observed live: 'sade' on a slice-holding node).
DB_VERSION_KEY = "musicbrainz.db_version"

# Every wire table populated, in one round trip. A dump node serves only
# when it holds EVERY table the wire format names: a dump loaded before a
# table joined SLICE_TABLES (the url tables, 2026-09-17) would otherwise
# sign slices with a hole in them, and a signed hole closes a name for good
# on the requester. Until the loader adds the missing tables
# (mb_dump_load.download_and_load loads just those) the node is a replica:
# it re-serves what it cached and advertises no dump.
_WIRE_TABLES_POPULATED_SQL = "SELECT " + " AND ".join(
    f"EXISTS (SELECT 1 FROM {t})" for t in SLICE_TABLES)


def local_dump_available(conn):
    """Dump version string if this node holds a COMPLETED full load on this
    DSN (serve-slices / serve-search / search-locally authority), else
    None. The in-DB marker is the per-DSN truth; populated wire tables are
    the sanity check that the data is all there and was not dropped
    underneath it."""
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM user_settings WHERE key = %s",
                    (DB_VERSION_KEY,))
        row = cur.fetchone()
        version = row[0] if row else None
        if not version:
            return None
        cur.execute(_WIRE_TABLES_POPULATED_SQL)
        if not cur.fetchone()[0]:
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


def match_artist_ids(conn, name: str) -> list:
    """The mb_artist ids the slice of `name` covers, sorted. Raises DumpBusy
    while the loader runs."""
    with conn.cursor() as cur:
        # The loader holds this lock exclusive for its whole TRUNCATE+COPY pass —
        # serving half-truncated tables would poison the requester, so answer
        # 503. A shared probe: a canon run reading mb_* is no reason to refuse.
        cur.execute("SELECT pg_try_advisory_lock_shared(%s)", (MB_LOAD_LOCK_KEY,))
        if not cur.fetchone()[0]:
            raise DumpBusy()
        cur.execute("SELECT pg_advisory_unlock_shared(%s)", (MB_LOAD_LOCK_KEY,))
        return sorted(set(_match_artist_ids(cur, name.strip())))


def get_slice_one(conn, name: str, artist_ids: Optional[list] = None) -> dict:
    """ONE artist's slice subtree — the v2 signing unit. Batch requests are
    served as a loop of these on the server (with the blob cache in
    between), never as a merged payload: a merged payload cannot carry
    per-name signatures. `artist_ids`: the match already made."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")

    all_ids = set(match_artist_ids(conn, name) if artist_ids is None else artist_ids)
    with conn.cursor() as cur:
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

        # Bandcamp pages (the dump keeps no other store): release links for
        # the whole subtree, artist links for the matched artists — a
        # collaborator's own page rides in that artist's slice.
        tables["mb_l_release_url"] = _fetch(
            cur, "mb_l_release_url", "entity0 = ANY(%s)", (r_ids,), truncated)
        tables["mb_l_artist_url"] = _fetch(
            cur, "mb_l_artist_url", "entity0 = ANY(%s)", (a_ids,), truncated)
        url_ids = sorted(
            {row[SLICE_TABLES["mb_l_release_url"].index("entity1")]
             for row in tables["mb_l_release_url"]} |
            {row[SLICE_TABLES["mb_l_artist_url"].index("entity1")]
             for row in tables["mb_l_artist_url"]})
        tables["mb_url"] = _fetch(cur, "mb_url", "id = ANY(%s)", (url_ids,), truncated)

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
    """(name, tier) for the artist names the local canon pipeline is waiting
    on, IN PRIORITY ORDER: owned artists past their last_mb_sync watermark
    (canonicalize_pending's feed) first; then the names imported Last.fm
    scrobbles wait on (pending_scrobble_artists, the most scrobbled first —
    canon.scrobbles can only place them once their slice is here); then
    phantom stubs without an artist_mbids row (phantom canon's feed) —
    minus names already answered by a peer (mb_slice_fetches — zero-match
    included, the slice is a closed world per name).

    Ordering is the point. The queue drains ~200 names per cycle against
    thousands of similar-derived phantoms (4033 measured on the reference
    library), so alphabetical order — what this used to do — parked a
    Z-named OWNED artist behind four thousand discovery stubs for days.
    Owned canon is not a nicety: album canon, editions, discographies and
    carry pushability all gate on it. Next come canonized artists that
    have never been shelved (artist_mbids row, no files, last_album_sync
    NULL) — the seed picks and carried phantoms a fresh replica arrives
    with: the discography sync answers no_source for them until their
    slice lands (discography._mb_source_covers), so this queue is the only
    thing that turns a curated pick into a discography. Within the phantom
    stub layer the tiers mirror discography.stale_canonized_artists —
    listened-to first, then artists similar to those, then the rest — so
    the part of the discovery graph you actually touch resolves first.

    Last come canonized artists the ledger no longer vouches for: an
    artist_mbids row and no mb_slice_fetches row. Their facts arrived in a
    slice whose provenance row is gone (016 reset the ledger when the wire
    format grew) or through a peer's sealed canon rather than a slice of
    their own name. Canon is done for them, so no other tier lists them —
    yet discography._mb_source_covers and the Buy resolver both read a
    fetch row as "the local facts for this name are complete and current",
    and without one they answer no_source / unknown for good. Re-asking
    restores that invariant (measured on the macOS node after 016: 299 of
    547 canonized names, 6 695 of 7 280 phantom albums stuck on unknown).
    MB's special-purpose artists ([unknown], [traditional], Various
    Artists…) are left out: their subtree is every record on Earth, capped
    and useless."""
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
                SELECT ar.name, 0 AS tier, NULL::bigint AS weight
                FROM artists ar
                WHERE EXISTS (
                    SELECT 1 FROM track_artists ta
                    JOIN owned_files mf ON mf.track_id = ta.track_id
                    WHERE ta.artist_id = ar.id AND ta.role = 'primary'
                      AND (ar.last_mb_sync IS NULL OR mf.created_at > ar.last_mb_sync))
                UNION ALL
                SELECT n.name_key, 1 AS tier, count(*) AS weight
                FROM pending_scrobble_artists n
                JOIN pending_scrobbles ps ON ps.name_key = n.name_key
                WHERE n.artist_id IS NULL
                  AND n.name_key !~ '^\\[.*\\]$'
                  AND n.name_key <> 'various artists'
                GROUP BY n.name_key
                UNION ALL
                SELECT a.name, 2 AS tier, NULL
                FROM artists a
                WHERE a.last_album_sync IS NULL
                  AND EXISTS (SELECT 1 FROM artist_mbids am WHERE am.artist_id = a.id)
                  AND NOT EXISTS (SELECT 1 FROM track_artists ta
                                  JOIN owned_files mf ON mf.track_id = ta.track_id
                                  WHERE ta.artist_id = a.id)
                UNION ALL
                SELECT a.name,
                       CASE WHEN EXISTS (SELECT 1 FROM similar_artists sa
                                          JOIN listened l ON l.artist_id = sa.artist_id
                                         WHERE sa.similar_artist_id = a.id) THEN 3
                            ELSE 4 END AS tier,
                       NULL
                FROM artists a
                WHERE EXISTS (SELECT 1 FROM similar_artists sa
                              WHERE sa.similar_artist_id = a.id)
                  AND NOT EXISTS (SELECT 1 FROM track_artists ta
                                  WHERE ta.artist_id = a.id)
                  AND NOT EXISTS (SELECT 1 FROM artist_mbids am
                                  WHERE am.artist_id = a.id)
                UNION ALL
                SELECT a.name, 5 AS tier, NULL
                FROM artists a
                WHERE EXISTS (SELECT 1 FROM artist_mbids am
                              WHERE am.artist_id = a.id)
                  AND a.name !~ '^\\[.*\\]$'
                  AND lower(a.name) <> 'various artists'
            )
            SELECT p.name, MIN(p.tier) FROM pending p
            WHERE p.name IS NOT NULL AND btrim(p.name) <> ''
              -- what a dump node serves (servable_name): asked otherwise,
              -- a name would come back `missing` every cycle
              AND length(btrim(p.name)) <= %(name_max)s
              AND p.name !~ '[[:cntrl:]]'
              AND lower(btrim(p.name)) !~ '^\\[.*\\]$'
              AND lower(btrim(p.name)) <> 'various artists'
              -- a signed zero-match closes a name for 30 days, then it is asked
              -- again — of another author's source where one answers; a void
              -- answer (its blob lost a clash, a rebuild left it out) closes
              -- nothing
              AND NOT EXISTS (SELECT 1 FROM mb_slice_fetches f
                              WHERE f.name_key = lower(btrim(p.name)) AND NOT f.void
                                AND (f.matched_ids > 0 OR f.fetched_at > now() - interval '30 days'))
            GROUP BY p.name
            ORDER BY MIN(p.tier), MAX(p.weight) DESC NULLS LAST, p.name
            LIMIT %(lim)s
        """, {"lim": limit, "name_max": NAME_MAX_CHARS})
        rows = cur.fetchall()
        # The ledger is keyed by name_key, which SQL's lower(btrim()) matches
        # for every name but a few (a non-breaking space, a dotted capital I):
        # those are told by the key itself, or they would be asked every cycle.
        keys = {r[0]: name_key(r[0]) for r in rows}
        cur.execute("""SELECT name_key FROM mb_slice_fetches
                        WHERE name_key = ANY(%s) AND NOT void
                          AND (matched_ids > 0 OR fetched_at > now() - interval '30 days')""",
                    (sorted(set(keys.values())),))
        closed = {r[0] for r in cur.fetchall()}
        return [(r[0], r[1]) for r in rows if keys[r[0]] not in closed]


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
                     sig: str, blob_gz: bytes, cap: bool = True,
                     match: Optional[str] = None, relay: Optional[str] = None,
                     contested: bool = False, mbids=()) -> bool:
    """Upsert one verified blob, with its payload's hash and the MBIDs of its
    artist, group, release and recording rows (`mbids`, which a settled clash
    finds its carriers by). `cap=True` is the replica posture: a blob too
    large to keep is not stored, and the name's older blob goes with it — the
    kept blobs always say what the ledger says. A dump node stores its own
    regardless — that is the cache, each blob under the `match` key of what
    it covers, `relay` None. Another blob in its place is checked anew before
    it passes on, and what waited on the one it replaces — its clashes, its
    check's draw — goes with it; the same bytes again keep their verdict and
    their arrival."""
    payload = hashlib.sha256(blob_gz).hexdigest()
    with conn.cursor() as cur:
        cur.execute("""DELETE FROM slice_check_plans p
                        WHERE p.family = 'mb' AND p.key = %(k)s
                          AND NOT EXISTS (SELECT 1 FROM mb_slice_blobs b
                                           WHERE b.name_key = %(k)s AND b.payload_sha256 = %(p)s)""",
                    {"k": nk, "p": payload})
        cur.execute("DELETE FROM slice_clashes WHERE family = 'mb' AND key = %s AND payload_sha256 <> %s",
                    (nk, payload))
        if cap and len(blob_gz) > RESERVE_BLOB_MAX_GZ:
            cur.execute("DELETE FROM mb_slice_blobs WHERE name_key = %s", (nk,))
            cur.execute("DELETE FROM slice_clashes WHERE family = 'mb' AND key = %s", (nk,))
            return False
        cur.execute("""
            INSERT INTO mb_slice_blobs
                   (name_key, dump_version, author_pubkey, sig, blob_gz, match_key, relay,
                    contested, payload_sha256, mbids)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::uuid[])
            ON CONFLICT (name_key) DO UPDATE SET
                dump_version = EXCLUDED.dump_version,
                author_pubkey = EXCLUDED.author_pubkey,
                sig = EXCLUDED.sig,
                blob_gz = EXCLUDED.blob_gz,
                match_key = EXCLUDED.match_key,
                relay = EXCLUDED.relay,
                contested = EXCLUDED.contested,
                mbids = EXCLUDED.mbids,
                checked_at = CASE WHEN mb_slice_blobs.payload_sha256 = EXCLUDED.payload_sha256
                                  THEN mb_slice_blobs.checked_at END,
                suspect = coalesce(mb_slice_blobs.payload_sha256 = EXCLUDED.payload_sha256, FALSE)
                          AND mb_slice_blobs.suspect,
                created_at = CASE WHEN mb_slice_blobs.payload_sha256 = EXCLUDED.payload_sha256
                                  THEN mb_slice_blobs.created_at ELSE now() END,
                payload_sha256 = EXCLUDED.payload_sha256""",
            (nk, dump_version, author_pubkey, sig, blob_gz, match, relay, contested, payload,
             list(mbids)))
    return True


def _stored_match(conn, match: str, dump_version: str):
    """(spellings kept for `match` at `dump_version`, one blob of them) —
    one statement, so a sweep or an upsert between two reads cannot part
    the count from the blob."""
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) OVER (), blob_gz FROM mb_slice_blobs
                        WHERE match_key = %s AND dump_version = %s
                        ORDER BY name_key LIMIT 1""", (match, dump_version))
        row = cur.fetchone()
    return (int(row[0]), bytes(row[1])) if row else (0, None)


def _respelled(blob_gz: bytes, name: str, artist_ids: list) -> bytes:
    """The canonical blob of `name` cut from a stored blob of the same match
    set: only name_key and artists_matched differ, the rows stay as signed."""
    core = json.loads(gzip.decompress(blob_gz))
    core["name_key"] = name_key(name)
    core["artists_matched"] = {name: list(artist_ids)}
    return json.dumps(core, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _build(conn, name: str, version: str, sign_fn, author_pubkey: str,
           requester: Optional[str], replace: bool) -> Optional[dict]:
    """A dump node's signed slice of `name`, kept when it replaces this
    name's older blob or is among the first spellings of its match set —
    None past the build budget, or for a subtree larger than any requester
    takes."""
    if not _builds.admit(requester):
        return None
    ids = []
    try:
        ids = match_artist_ids(conn, name)          # may raise DumpBusy
        match = match_key(ids) if ids else None
        kept, twin = _stored_match(conn, match, version) if match else (0, None)
        blob = (_respelled(twin, name, ids) if twin is not None
                else name_blob(name, get_slice_one(conn, name, ids)))
        if len(blob) > BLOB_RAW_MAX:
            logger.warning("MB slice for %r: %d bytes, over what requesters take — not served",
                           name[:64], len(blob))
            return None
        sig = sign_fn(receipt_message_for(blob)).hex()
        blob_gz = gzip.compress(blob)
        if match and (kept < VARIANTS_PER_MATCH or replace):
            store_slice_blob(conn, name_key(name), version, author_pubkey, sig, blob_gz,
                             cap=False, match=match)
        return {"dump_version": version, "author_pubkey": author_pubkey,
                "sig": sig, "blob_gz": blob_gz}
    finally:
        _builds.done(requester, counted=bool(ids))


def count_slice_blobs(conn, own_pubkey: Optional[str] = None) -> int:
    """What /health advertises: the blobs this node may pass on, checked or
    not yet (desktop/p2p/slice_checks.checkable_count) — a node holding only
    unchecked ones must still be asked, or it would never check them."""
    from desktop.p2p import slice_checks
    return slice_checks.checkable_count(conn, "mb", own_pubkey)


def serve_slices(conn, names: list, sign_fn=None,
                 author_pubkey: str = "", requester: Optional[str] = "",
                 own_cert: Optional[tuple] = None, own_pubkey: Optional[str] = None) -> dict:
    """The shared v4 server body for both surfaces.

    Per name: a dump node answers from its own build — cached, or computed,
    signed and cached on a miss (`sign_fn` present AND a local dump exists),
    within the build budget `requester` (its address) has a share of. Any
    other kept blob answers only once this node checked it against
    MusicBrainz (desktop/p2p/slice_checks.servable); a blob not checked
    lately is `missing` this time and its check is queued. Everything else
    lands in `missing` too — the requester takes misses to the next
    candidate, and so are a name_key asked twice and what ANSWER_MAX_BYTES
    cuts. The entry carries the ORIGINAL author's pubkey, signature and
    certificate (`own_cert` — (certificate, proof) — for this node's own
    build; for a relayed blob, the author's as this node recorded it)."""
    import base64

    from desktop.p2p import slice_checks, slice_trust

    if not isinstance(names, list) or not names \
            or len(names) > MAX_NAMES_PER_REQUEST:
        raise ValueError(f"names must be a list of 1..{MAX_NAMES_PER_REQUEST}")
    if not all(isinstance(n, str) and n.strip() for n in names):
        raise ValueError("names must be non-empty strings")

    version = (dump_version_file() or "") if (
        sign_fn is not None and local_dump_available(conn)) else None
    own = (own_pubkey or author_pubkey or "").lower() or None
    slices, missing, answered, size = {}, [], set(), 0
    for name in names:
        nk = name_key(name)
        if size >= ANSWER_MAX_BYTES or nk in answered or not servable_name(name):
            missing.append(name)
            continue
        answered.add(nk)
        entry = cached_slice(conn, nk)
        if version is not None and (entry is None or entry["dump_version"] != version
                                    or entry["author_pubkey"].lower() != own):
            # A miss, a blob cut from an earlier dump or another author's: built
            # from this one when the budget allows — a stale own blob still
            # beats none.
            built = _build(conn, name, version, sign_fn, author_pubkey,
                           requester, replace=entry is not None)
            entry = built or (entry if entry and entry["author_pubkey"].lower() == own else None)
        if entry is None:
            missing.append(name)
            continue
        if entry["author_pubkey"].lower() == own:
            cert = own_cert
        else:
            ok = slice_checks.servable(conn, "mb", nk, own)
            if ok is None:
                slice_checks.request_check(conn, "mb", nk)
            cert = slice_trust.author_certificate(conn, entry["author_pubkey"]) if ok else None
        if not cert:
            missing.append(name)
            continue
        blob_b64 = base64.b64encode(entry["blob_gz"]).decode("ascii")
        if slices and size + len(blob_b64) > ANSWER_MAX_BYTES:
            missing.append(name)
            size = ANSWER_MAX_BYTES
            continue
        size += len(blob_b64)
        slices[name] = {
            "dump_version": entry["dump_version"],
            "author_pubkey": entry["author_pubkey"],
            "sig": entry["sig"],
            "blob_gz": blob_b64,
            "author_cert": cert[0],
            "author_proof": cert[1],
        }
    return {"v": PROTOCOL_VERSION, "slices": slices, "missing": missing}


def verify_slice_entry(name: str, entry: dict):
    """Full per-name verification on the receiving side: the author's birth
    certificate is the authority's and names the author (a `pow` one with its
    proof bound to it) → gunzip → hash → author signature → the blob's own
    name_key matches the asked name. Returns (core_dict, blob_gz_bytes,
    certificate, proof) or None. Every hop runs exactly this, against the
    ORIGINAL author's key; what the author is worth here (its penalties, its
    proof's evaluation) is the importer's, on its database."""
    import base64

    from desktop.node_identity import verify_signature
    from desktop.p2p import slice_trust, wire
    try:
        author = entry.get("author_pubkey")
        if not isinstance(author, str) or len(author) != 64:
            return None
        certified = slice_trust.entry_certificate(entry, author)
        if certified is None:
            return None
        blob_gz = base64.b64decode(entry.get("blob_gz") or "")
        blob = wire.gunzip(blob_gz, BLOB_RAW_MAX)
        sig = entry.get("sig") or ""
        if not verify_signature(receipt_message_for(blob),
                                bytes.fromhex(sig), author):
            return None
        core = wire.loads(blob)
        if not isinstance(core, dict) or core.get("name_key") != name_key(name):
            return None
        return core, blob_gz, certified[0], certified[1]
    except Exception:
        return None
