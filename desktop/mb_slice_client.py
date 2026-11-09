"""
MB dump slice importer — the requester half of P2P canonicalization.

Fetches raw mb_* rows for a batch of artist names from a peer that holds the
dump or re-serves slices (POST /api/mb/slice) and inserts them into the local
mb_* tables. Facts only: all matching/canon logic runs locally afterwards —
the caller triggers the backend's POST /canonicalize once per run, which flips
mb_backend.LOCAL_DUMP on the first imported slice and feeds the canon, whose
irreversible decisions are re-made on MusicBrainz's own answers
(backend/canon/confirm.py).

Any node holding a dump may author a slice, so each one is taken on its
author's certificate and on its content, never on the key alone
(desktop/p2p/slice_trust.py): a convicted author's slices, or a quarantined
peer's answers, are refused; a slice whose rows reach outside its own subtree
is refused whole and proves its author false; a slice one of whose artist,
group, release or recording rows carries another MBID on an id held here — or
whose artist or group row carries, under another id, an MBID held here — lands
none of its rows until MusicBrainz's answers settle the clash (in the other
tables the held row stays and the rest lands). What was imported is indexed —
its author, the peer that passed it (by the key its TLS channel proved), the
MBIDs it is about.

Idempotent: every insert is ON CONFLICT DO NOTHING (001_initial.sql guarantees
a unique index on each shipped table), and mb_slice_fetches records every
queried name — including zero-match, which is a closed-world answer, not a
retry. A later FULL dump load TRUNCATEs the slices away and supersedes them.
"""

import hashlib
import logging
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple
from uuid import UUID

import psycopg2
import psycopg2.extras

from desktop.api_client import BackendAPIClient
from desktop.p2p import mb_asks, slice_trust, wire
from desktop.p2p.mb_slice_queries import (MB_LOAD_LOCK_KEY, PROTOCOL_VERSION,
                                          SLICE_TABLES, TABLE_ROW_LIMITS, addr_uuid,
                                          bandcamp_page, name_key as lower_key,
                                          store_slice_blob,
                                          verify_slice_entry)

_URL_COL = SLICE_TABLES["mb_url"].index("url")
_URL_ID_COL = SLICE_TABLES["mb_url"].index("id")
_LINK_URL_COL = SLICE_TABLES["mb_l_release_url"].index("entity1")   # both link tables: one column list

# The tables whose row ids a clash is judged in, and the kind MusicBrainz
# answers their MBIDs by: a row whose id is held here under another MBID —
# MusicBrainz never reuses an id, so one of the two is false, and its web
# service tells which MBID it knows. An artist's or a group's MBID held here
# under another id is one too (one MBID has one row): MusicBrainz's answer for
# the MBID tells by the rows' names. In every other table (dictionaries,
# credits, media, tracks, urls) there is nothing it can be asked about: the
# held row stays and the rest of a slice lands.
_CLASHING = {"mb_artist": "artist", "mb_release_group": "release_group",
             "mb_release": "release", "mb_recording": "recording"}
_NAMED = ("mb_artist", "mb_release_group")
# An honest dump never clashes: a blob with more clashes than this loses
# them all without a question — a forger's rows are no reason for MusicBrainz's
# questions, nor for a rebuild's.
CLASHES_ASKED = 3

# The column kinds of the slice tables (001_initial.sql), what a cell must be
# before it reaches SQL: an MBID in its canonical form, a string PostgreSQL
# stores, a timestamp it parses, a boolean, or an integer its column holds.
_TEXT_COLUMNS = frozenset({"name", "sort_name", "comment", "locale", "join_phrase", "description",
                           "number", "barcode", "url", "entity0_credit", "entity1_credit"})
_TIME_COLUMNS = frozenset({"created", "last_updated"})
_BOOL_COLUMNS = frozenset({"ended", "primary_for_locale", "is_data_track", "video"})
_SMALLINT_COLUMNS = frozenset({"begin_date_year", "begin_date_month", "begin_date_day",
                               "end_date_year", "end_date_month", "end_date_day",
                               "date_year", "date_month", "date_day", "artist_count", "quality",
                               ("mb_artist_credit_name", "position")})


_MAX_UTC_OFFSET = timedelta(hours=16)
_INT4 = 2 ** 31


def canonical_mbid(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _text(value) -> bool:
    if not isinstance(value, str) or "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _cell_ok(table: str, column: str, value) -> bool:
    if value is None:
        return True
    if column == "gid":
        return canonical_mbid(value)
    if column in _TEXT_COLUMNS:
        return _text(value)
    if column in _TIME_COLUMNS:
        if not _text(value):
            return False
        try:
            offset = datetime.fromisoformat(value).utcoffset()
        except ValueError:
            return False
        # Python takes offsets up to a day; PostgreSQL refuses one past 15:59.
        return offset is None or abs(offset) < _MAX_UTC_OFFSET
    if column in _BOOL_COLUMNS:
        return isinstance(value, bool)
    bound = 2 ** 15 if column in _SMALLINT_COLUMNS or (table, column) in _SMALLINT_COLUMNS else 2 ** 31
    return isinstance(value, int) and not isinstance(value, bool) and -bound <= value < bound


def _core_ok(core: dict) -> bool:
    """The core fields the import and the check read, of their kind: the
    names matched, each to a list of artist ids; the tables the build cut —
    only those it caps (TABLE_ROW_LIMITS); the dump version."""
    matched = core.get("artists_matched")
    if not isinstance(matched, dict) or not all(
            isinstance(name, str) and isinstance(ids, list)
            and all(isinstance(i, int) and not isinstance(i, bool) and -_INT4 <= i < _INT4 for i in ids)
            for name, ids in matched.items()):
        return False
    truncated = core.get("truncated")
    if truncated is not None and not (isinstance(truncated, list)
                                      and all(isinstance(t, str) and t in TABLE_ROW_LIMITS
                                              for t in truncated)):
        return False
    version = core.get("dump_version")
    return version is None or isinstance(version, str)


def _rows(core: dict):
    """{table: decoded rows} of a verified slice in its wire shape — the core
    fields of their kind (_core_ok), each table one of SLICE_TABLES, each row
    a list of its columns, each cell of its column's kind — or None for
    anything else: a malformed entry is dropped, never raised on, and nothing
    of it reaches a set, a sort or a cast that would raise."""
    if not _core_ok(core):
        return None
    tables = core.get("tables")
    if not isinstance(tables, dict) or not set(tables) <= set(SLICE_TABLES):
        return None
    out = {}
    for t, cols in SLICE_TABLES.items():
        raw = tables.get(t) or []
        if not isinstance(raw, list) or not all(isinstance(r, str) for r in raw):
            return None
        try:
            rows = [wire.loads(r) for r in raw]
        except ValueError:
            return None
        if not all(isinstance(r, list) and len(r) == len(cols)
                   and all(_cell_ok(t, c, v) for c, v in zip(cols, r)) for r in rows):
            return None
        out[t] = rows
    return out


def carried(rows: Dict[str, list]) -> List[str]:
    """The MBIDs of a slice's artist, group, release and recording rows — what
    a clash MusicBrainz settles can find the kept blobs that carry by."""
    out = set()
    for t in _CLASHING:
        g = SLICE_TABLES[t].index("gid")
        out.update(r[g] for r in rows.get(t) or [] if r[g])
    return sorted(out)


def _shop_pages_only(tables: dict) -> dict:
    """A slice's url subtree cut to Bandcamp shop pages, as the dump loader
    keeps them (mb_slice_queries.bandcamp_page): the Buy button opens what
    these rows name, and a slice is the author's word, not a store's."""
    urls = [r for r in tables.get("mb_url") or [] if bandcamp_page(r[_URL_COL])]
    kept = {r[_URL_ID_COL] for r in urls}
    out = dict(tables, mb_url=urls)
    for t in ("mb_l_artist_url", "mb_l_release_url"):
        out[t] = [r for r in tables.get(t) or [] if r[_LINK_URL_COL] in kept]
    return out


Clash = Tuple[str, str, str, Optional[str], Optional[str]]


def clashes(conn, tables: Dict[str, list]) -> List[Clash]:
    """(table, the MBID a row of `tables` carries, the MBID held, the two
    rows' names) for every artist, group, release or recording row whose id
    is held here under another MBID — names None — and every artist or group
    row whose MBID is held here under another id — the MBID twice, the names
    for MusicBrainz's answer to tell apart (_CLASHING). An honest dump never
    yields one: MusicBrainz never reuses an id, and one MBID has one row."""
    out: List[Clash] = []
    with conn.cursor() as cur:
        for t in _CLASHING:
            rows = tables.get(t) or []
            if not rows:
                continue
            cols = SLICE_TABLES[t]
            i, g = cols.index("id"), cols.index("gid")
            incoming = {r[i]: r[g] for r in rows if r[g]}
            if not incoming:
                continue
            cur.execute(f"SELECT id, gid::text FROM {t} WHERE id = ANY(%s)", (list(incoming),))
            for rid, held in cur.fetchall():
                if held and held != incoming[rid]:
                    out.append((t, incoming[rid], held, None, None))
            if t not in _NAMED:
                continue
            n = cols.index("name")
            by_gid = {r[g]: (r[i], r[n]) for r in rows if r[g]}
            cur.execute(f"SELECT gid::text, id, name FROM {t} WHERE gid = ANY(%s::uuid[])",
                        (list(by_gid),))
            for gid, held_id, held_name in cur.fetchall():
                if by_gid[gid][0] != held_id:
                    out.append((t, gid, gid, by_gid[gid][1], held_name))
    return out


def insert_rows(conn, nk: str, tables: Dict[str, list]) -> Tuple[int, int, List[Clash]]:
    """A slice's rows into the mb_* tables — none of them when one clashes
    (`clashes`): a clashing row's neighbours would otherwise attach to the
    entity held under that id. Returns (received, inserted, clashes). Rows
    travel as canonical JSON inside the signed blob, decoded and checked by
    the caller; identifiers never come from the wire: only SLICE_TABLES
    names and columns build SQL."""
    found = clashes(conn, tables)
    received = sum(len(r) for r in tables.values())
    if found:
        return received, 0, found
    inserted = 0
    with conn.cursor() as cur:
        for t, cols in SLICE_TABLES.items():
            rows = tables.get(t) or []
            if not rows:
                continue
            done = psycopg2.extras.execute_values(
                cur, f"INSERT INTO {t} ({', '.join(cols)}) VALUES %s "
                     f"ON CONFLICT DO NOTHING RETURNING 1",
                rows, page_size=1000, fetch=True)
            inserted += len(done)
    return received, inserted, []


logger = logging.getLogger(__name__)

# Post-import ANALYZE targets — the tables whose planner stats shift from
# "empty" to "populated" and sit on canon's hot join paths.
_ANALYZE_TABLES = (
    "mb_artist", "mb_artist_alias", "mb_artist_credit", "mb_artist_credit_name",
    "mb_release_group", "mb_release", "mb_medium", "mb_track", "mb_recording",
    "mb_url", "mb_l_artist_url", "mb_l_release_url",
)


class MBSliceClient:
    """Imports MB slices from one peer into the local DB."""

    def __init__(
        self,
        peer_api: BackendAPIClient,
        db_dsn: str,
        source_node: str = "",
        backend_api: Optional[BackendAPIClient] = None,
        progress_cb: Optional[Callable[[str], None]] = None,
    ):
        self.peer_api = peer_api
        self.db_dsn = db_dsn
        self.source_node = source_node
        self.source_addr = addr_uuid(getattr(peer_api, "base_url", ""))
        self.backend_api = backend_api
        self.progress_cb = progress_cb
        self._conn: Optional[psycopg2.extensions.connection] = None
        self._idconn: Optional[psycopg2.extensions.connection] = None

    def _progress(self, msg: str):
        logger.info(msg)
        if self.progress_cb:
            self.progress_cb(msg)

    def _get_conn(self) -> psycopg2.extensions.connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self.db_dsn)
            self._conn.autocommit = False
        return self._conn

    def _get_idconn(self) -> psycopg2.extensions.connection:
        """The autocommit connection the identity and penalty checks of every
        batch share — one for the client's life, not one a batch."""
        if self._idconn is None or self._idconn.closed:
            self._idconn = psycopg2.connect(self.db_dsn)
            self._idconn.autocommit = True
        return self._idconn

    def close(self):
        for attr in ("_conn", "_idconn"):
            conn = getattr(self, attr)
            if conn and not conn.closed:
                conn.close()
            setattr(self, attr, None)

    def _relay(self) -> str:
        """The key the peer's TLS channel proved — who passed the slices on."""
        return (getattr(self.peer_api, "peer_pubkey", None) or self.source_node or "").lower()

    def run(self, names: list[str], avoid: Optional[Dict[str, str]] = None) -> dict:
        """Fetch + import one batch of names (per-name signed blobs).

        Each name verifies INDEPENDENTLY against its ORIGINAL author's
        signature and certificate — the peer asked may be a replica
        re-serving someone else's blobs, and that is the design. Verified
        names are imported and closed (mb_slice_fetches); names the peer does
        not hold come back in `missing` and stay pending for the next
        candidate — only a SIGNED empty slice closes a name as a zero-match.
        `avoid` maps a re-opened name (a zero-match, or an answer a clash or a
        rebuild voided) to the author whose answer closed it: that author's
        answer is no answer for it here, from whichever peer it comes — the
        name stays `missing`. The very blob that lost a clash, served again,
        closes the name like a zero-match (re-asked after a month, of another
        author first) and lands nothing. A dump version
        later than the newest MetaBrainz has published (slice_checks.
        version_bound) is no answer either. Returns per-batch stats; an
        'error' dict means the whole exchange failed — `refused` then carries
        what the peer says this node did."""
        resp = self.peer_api.mb_slice(names)
        if not resp or "error" in resp or "detail" in resp:
            err = (resp or {}).get("error") or (resp or {}).get("detail") or "no response"
            logger.warning(f"MB slice fetch failed ({self.source_node}): {err}")
            out = {"error": err}
            if err == "slice_refused":
                refused = slice_trust.refusal_of(resp)
                if refused is None:
                    # Not a refusal this node can read: the source is dropped,
                    # and nothing of what it said is recorded.
                    return {"error": "malformed refusal"}
                out["refused"] = refused
            if "retry_after" in (resp or {}):
                # A 429: the peer's per-IP window is full. The caller waits
                # it out and re-asks instead of parking the batch.
                out["retry_after"] = resp["retry_after"]
            return out
        if resp.get("v") != PROTOCOL_VERSION:
            logger.warning(f"MB slice from {self.source_node}: incompatible "
                           f"protocol (no v{PROTOCOL_VERSION}) — rejected")
            return {"error": "incompatible slice protocol"}

        slices = resp.get("slices") or {}
        missing_here = resp.get("missing") or []
        if not isinstance(slices, dict) or not isinstance(missing_here, list):
            return {"error": "malformed slice answer"}
        relay = self._relay()
        signed: Dict[str, tuple] = {}
        dropped = 0
        for name in names:
            entry = slices.get(name)
            if not isinstance(entry, dict):
                continue                      # missing at this peer
            out = verify_slice_entry(name, entry)
            rows = _rows(out[0]) if out is not None else None
            if rows is None:
                dropped += 1
                logger.warning(f"MB slice for {name!r}: signature/identity/shape "
                               f"check failed — dropped")
                continue
            signed[name] = (out, entry, rows)
        if dropped and not signed:
            return {"error": "all slices failed verification"}
        verified: Dict[str, tuple] = {}
        penalized: List[str] = []
        passed: List[str] = []   # no answer here: asked of the next source
        if signed:
            from desktop.p2p import slice_checks, slice_rebuild
            idconn = self._get_idconn()
            if slice_checks.source_refused(idconn, relay):
                return {"error": "source carries a penalty"}
            bound = slice_checks.version_bound(idconn, "mb")
            # Who brought a proof, for its verifier: only the key the channel
            # proved — the source list's claim could name the author itself.
            proved = (getattr(self.peer_api, "peer_pubkey", None) or "").lower()
            for name, ((core, blob_gz, cert, proof), entry, rows) in signed.items():
                author = entry["author_pubkey"].lower()
                if not slice_checks.version_ok(core.get("dump_version"), bound):
                    # No dump MetaBrainz has published (or not yet, as this
                    # node read its listing): nothing an honest node holds.
                    passed.append(name)
                    logger.warning("MB slice for %r: dump version %r past the newest "
                                   "published — not taken", name[:64],
                                   str(core.get("dump_version"))[:32])
                    continue
                if avoid and avoid.get(name) == author:
                    passed.append(name)
                    continue
                if slice_trust.refused_author(idconn, cert) \
                        or slice_trust.observe_author(idconn, cert, proof, proved) \
                        in (None, "failed"):
                    dropped += 1
                    continue
                if not slice_trust.subtree_ok(core, rows):
                    # Rows the protocol's own walk does not reach: put there by
                    # the author, and passed on by a peer whose check would have
                    # seen it. The conviction and the drop commit together.
                    dropped += 1
                    with slice_rebuild.verdict(idconn):
                        found = slice_trust.proven(
                            idconn, "mb", lower_key(name), author, relay or author, "P2",
                            "subtree", None, core.get("dump_version") or "",
                            hashlib.sha256(blob_gz).hexdigest())
                        if found:
                            slice_rebuild.drop(idconn, found)
                    penalized += found
                    continue
                verified[name] = (core, blob_gz, entry, _shop_pages_only(rows), rows)
            if penalized:
                # What the answer brought before the conviction counts no more
                # than what came after it: nothing of a source quarantined
                # meanwhile, nothing of an author convicted meanwhile.
                if relay and relay in penalized:
                    return {"error": "source carries a penalty"}
                verified = {n: v for n, v in verified.items()
                            if v[2]["author_pubkey"].lower() not in penalized}
        if dropped and not verified and not passed:
            return {"error": "all slices failed verification"}

        stats = {"names": len(names), "matched": 0, "rows_received": 0,
                 "rows_inserted": 0, "truncated": [], "contested": 0,
                 "missing": [n for n in names
                             if n not in verified and (n in missing_here or n in passed)
                             or (n not in slices and n not in verified)]}

        conn = self._get_conn()
        with conn.cursor() as cur:
            # Serialize against a concurrent full dump load (its TRUNCATE+COPY
            # holds the same lock exclusive). Shared: canon runs reading mb_*
            # meanwhile are no conflict — this batch lands in one transaction.
            # Session-scoped, released in finally.
            cur.execute("SELECT pg_advisory_lock_shared(%s)", (MB_LOAD_LOCK_KEY,))
        try:
            from desktop.p2p import slice_checks
            # A verdict that committed while this answer was verified or the
            # lock awaited (a rebuild that ran meanwhile has already cleaned
            # up): nothing of a source or an author penalized since lands.
            if slice_checks.source_refused(conn, relay):
                return {"error": "source carries a penalty"}
            penalized_now = slice_trust.penalized_keys(conn)
            for name, (core, blob_gz, entry, tables, full) in verified.items():
                if entry["author_pubkey"].lower() in penalized_now:
                    stats["missing"].append(name)
                    continue
                with conn.cursor() as cur:
                    cur.execute("SAVEPOINT slice_name")
                try:
                    self._import_one(conn, name, core, blob_gz, entry, tables, full, relay, stats)
                except psycopg2.Error:
                    # One name's rows the database refuses lose that name
                    # alone; it stays pending for another source.
                    with conn.cursor() as cur:
                        cur.execute("ROLLBACK TO SAVEPOINT slice_name")
                    logger.warning("MB slice for %r could not be imported — that name alone is "
                                   "left out", name[:64], exc_info=True)
                    stats["missing"].append(name)
                    continue
                with conn.cursor() as cur:
                    cur.execute("RELEASE SAVEPOINT slice_name")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock_shared(%s)", (MB_LOAD_LOCK_KEY,))
            conn.commit()

        if stats["truncated"]:
            logger.warning(f"MB slice truncated tables {stats['truncated']} "
                           f"for batch starting {names[0]!r} — canon coverage "
                           f"for oversized artists will be partial")
        self._progress(f"MB slice: {stats['names']} names, "
                       f"{stats['matched']} matches, "
                       f"+{stats['rows_inserted']} rows"
                       + (f", {len(stats['missing'])} missing here"
                          if stats["missing"] else ""))
        return stats

    def _import_one(self, conn, name: str, core: dict, blob_gz: bytes, entry: dict,
                    tables: Dict[str, list], full: Dict[str, list], relay: str, stats: dict) -> None:
        """One verified name into the catalogue, its ledger row, the subject
        index and the replica inventory, in the caller's transaction."""
        author = entry["author_pubkey"].lower()
        payload = hashlib.sha256(blob_gz).hexdigest()
        nk = lower_key(name)
        if _lost_again(conn, nk, payload):
            # The very blob that lost a clash here: the name's answer is no
            # better than none — closed like a zero-match, re-asked in a month
            # of another author first.
            self._ledger(conn, name, core, author, entry, payload, relay, matched=0)
            return
        matched = core.get("artists_matched") or {}
        n_matched = sum(len(v) for v in matched.values())
        stats["matched"] += n_matched
        for t in core.get("truncated") or []:
            if t not in stats["truncated"]:
                stats["truncated"].append(t)
        received, inserted, clashed = insert_rows(conn, nk, tables)
        stats["rows_received"] += received
        stats["rows_inserted"] += inserted
        if not clashed and n_matched and not _matched_landed(conn, core, full):
            # A matched artist that is not here under its own (id, MBID) after
            # the import: the name's answer shows nothing a reader can see, so
            # it closes nothing for good — re-asked in a month, of another
            # author first.
            n_matched = 0
        self._ledger(conn, name, core, author, entry, payload, relay, matched=n_matched)
        artists, groups = slice_trust.subjects(core, full)
        slice_trust.index(conn, "mb", nk, author, relay or author, self.source_addr,
                          payload, core.get("dump_version") or "", artists, groups)
        # Replica inventory: keep the verified blob verbatim so this node can
        # re-serve it with the author's signature once it has checked it. A
        # blob too large to keep removes the name's older one — dump nodes
        # serve those from their own cache.
        kept = store_slice_blob(conn, nk, core.get("dump_version") or "", author,
                                entry["sig"], blob_gz, cap=True, relay=relay or author,
                                contested=bool(clashed), mbids=carried(full))
        if clashed:
            stats["contested"] += 1
            logger.warning("MB slice for %r: %d row(s) clash with rows held here — none "
                           "imported until MusicBrainz settles them", name[:64], len(clashed))
            if kept:
                _note_clashes(conn, nk, payload, clashed, author)
            else:
                # Nothing landed and nothing is kept to settle: the name is not
                # closed for good — re-asked in a month, of another author first.
                self._ledger(conn, name, core, author, entry, payload, relay, matched=0)

    def _ledger(self, conn, name: str, core: dict, author: str, entry: dict, payload: str,
                relay: str, matched: int) -> None:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO mb_slice_fetches
                    (name_key, source_node, dump_version, matched_ids,
                     source_pubkey, receipt, payload_sha256,
                     source_addr, relay)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (name_key) DO UPDATE SET
                    source_node = EXCLUDED.source_node,
                    dump_version = EXCLUDED.dump_version,
                    matched_ids = EXCLUDED.matched_ids,
                    source_pubkey = EXCLUDED.source_pubkey,
                    receipt = EXCLUDED.receipt,
                    payload_sha256 = EXCLUDED.payload_sha256,
                    source_addr = EXCLUDED.source_addr,
                    relay = EXCLUDED.relay,
                    void = FALSE,
                    fetched_at = now()
            """, (lower_key(name), self.source_node or author,
                  core.get("dump_version"), matched,
                  author, entry["sig"], payload,
                  self.source_addr, relay or author))

    def finalize(self):
        """After the last batch of a run: refresh planner stats and hand the
        freshly-imported facts to the backend canon pipeline."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("ANALYZE " + ", ".join(_ANALYZE_TABLES))
            conn.commit()
        finally:
            self.close()
        if self.backend_api:
            result = self.backend_api.canonicalize()
            if not result or not (result.get("started") or result.get("running")):
                logger.warning(f"backend canonicalize trigger failed: {result}")


def _lost_again(conn, nk: str, payload: str) -> bool:
    """The kept blob of the name with this payload lost a clash here."""
    with conn.cursor() as cur:
        cur.execute("""SELECT 1 FROM mb_slice_blobs
                        WHERE name_key = %s AND suspect AND contested
                          AND coalesce(payload_sha256, encode(sha256(blob_gz), 'hex')) = %s""",
                    (nk, payload))
        return cur.fetchone() is not None


def _matched_landed(conn, core: dict, rows: Dict[str, list]) -> bool:
    """Every artist the blob matched is here under its own (id, MBID)."""
    cols = SLICE_TABLES["mb_artist"]
    i, g = cols.index("id"), cols.index("gid")
    ids = {a for v in (core.get("artists_matched") or {}).values() for a in v}
    pairs = sorted({(r[i], r[g]) for r in rows.get("mb_artist") or [] if r[i] in ids and r[g]})
    if len(pairs) < len(ids):
        return False
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM mb_artist a
                         JOIN unnest(%s::int[], %s::uuid[]) AS p(id, gid)
                           ON a.id = p.id AND a.gid = p.gid""",
                    ([p[0] for p in pairs], [p[1] for p in pairs]))
        return cur.fetchone()[0] == len(pairs)


def _note_clashes(conn, nk: str, payload: str, clashed: list, author: str) -> None:
    """The clashes of the kept blob `payload`, for its check (desktop/p2p/
    slice_checks.py) to settle on MusicBrainz's answers — against this blob
    alone, never a later one of the name — one at a time, at the check's
    priority. The name is not closed meanwhile: asked again at once, of
    another author first (slice_checks.reopen). A blob with more than
    CLASHES_ASKED clashes loses at once, nothing asked."""
    from desktop.p2p import slice_checks
    if len(clashed) > CLASHES_ASKED:
        slice_checks.lose(conn, nk, payload)
        logger.warning("MB slice for %r: %d clashes — lost without a question", nk[:64],
                       len(clashed))
        return
    with conn.cursor() as cur:
        for table, incoming, held, incoming_name, held_name in clashed:
            cur.execute("""INSERT INTO slice_clashes (family, key, payload_sha256, entity, incoming, held,
                                                      incoming_name, held_name)
                           VALUES ('mb', %s, %s, %s, %s::uuid, %s::uuid, %s, %s) ON CONFLICT DO NOTHING""",
                        (nk, payload, table, incoming, held, incoming_name, held_name))
    slice_checks.reopen(conn, [nk])
    # Only a node that asks MusicBrainz settles a clash; elsewhere the blob
    # stays contested and none of its rows land.
    if mb_asks.node_asks(conn):
        need: list = []
        slice_checks.clash_questions(conn, nk, payload, need)
        if need:
            mb_asks.enqueue(conn, need, slice_checks.PRIORITY, slice_checks.SUBJECT,
                            slice_checks.subject_key("mb", nk), author)
