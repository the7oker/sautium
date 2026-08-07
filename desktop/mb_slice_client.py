"""
MB dump slice importer — the requester half of P2P canonicalization.

Fetches raw mb_* rows for a batch of artist names from a dump-holding peer
(POST /api/mb/slice) and inserts them into the local mb_* tables. Facts only:
all matching/canon logic runs locally afterwards — the caller triggers the
backend's POST /canonicalize once per run, which flips mb_backend.LOCAL_DUMP
on the first imported slice and feeds the unchanged canon pipeline.

Idempotent: every insert is ON CONFLICT DO NOTHING (001_initial.sql guarantees
a unique index on each shipped table), and mb_slice_fetches records every
queried name — including zero-match, which is a closed-world answer, not a
retry. A later FULL dump load TRUNCATEs the slices away and supersedes them.
"""

import logging
from typing import Callable, Optional

import psycopg2
import psycopg2.extras

from desktop.api_client import BackendAPIClient
from desktop.p2p.mb_slice_queries import (MB_LOAD_LOCK_KEY, SLICE_TABLES,
                                          addr_uuid, name_key as lower_key,
                                          store_slice_blob,
                                          verify_slice_entry)

logger = logging.getLogger(__name__)

# Post-import ANALYZE targets — the tables whose planner stats shift from
# "empty" to "populated" and sit on canon's hot join paths.
_ANALYZE_TABLES = (
    "mb_artist", "mb_artist_alias", "mb_artist_credit", "mb_artist_credit_name",
    "mb_release_group", "mb_release", "mb_medium", "mb_track", "mb_recording",
)


class MBSliceClient:
    """Imports MB slices from one dump-holding peer into the local DB."""

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

    def _progress(self, msg: str):
        logger.info(msg)
        if self.progress_cb:
            self.progress_cb(msg)

    def _get_conn(self) -> psycopg2.extensions.connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self.db_dsn)
            self._conn.autocommit = False
        return self._conn

    def close(self):
        if self._conn and not self._conn.closed:
            self._conn.close()
            self._conn = None

    def run(self, names: list[str]) -> dict:
        """Fetch + import one batch of names (v2: per-name signed blobs).

        Each name verifies INDEPENDENTLY against the ORIGINAL dump node's
        signature — the peer we asked may be a replica re-serving someone
        else's blobs, and that is the design. Verified names are imported
        and closed (mb_slice_fetches); names the peer does not hold come
        back in `missing` and stay pending for the next candidate — only a
        SIGNED empty slice closes a name as a zero-match (closed world is
        an author's statement, not a transport hiccup). Returns per-batch
        stats; an 'error' dict means the whole exchange failed."""
        import hashlib as _hashlib
        import json as _json

        resp = self.peer_api.mb_slice(names)
        if not resp or "error" in resp or "detail" in resp:
            err = (resp or {}).get("error") or (resp or {}).get("detail") or "no response"
            logger.warning(f"MB slice fetch failed ({self.source_node}): {err}")
            return {"error": err}
        if resp.get("v") != 2:
            logger.warning(f"MB slice from {self.source_node}: incompatible "
                           f"protocol (no v2) — rejected")
            return {"error": "incompatible slice protocol"}

        slices = resp.get("slices") or {}
        verified: dict[str, tuple] = {}
        dropped = 0
        for name in names:
            entry = slices.get(name)
            if entry is None:
                continue                      # missing at this peer
            out = verify_slice_entry(name, entry)
            if out is None:
                dropped += 1
                logger.warning(f"MB slice for {name!r}: signature/identity "
                               f"check failed — dropped")
                continue
            verified[name] = (out[0], out[1], entry)
        if dropped and not verified:
            return {"error": "all slices failed verification"}

        stats = {"names": len(names), "matched": 0, "rows_received": 0,
                 "rows_inserted": 0, "truncated": [],
                 "missing": [n for n in names
                             if n not in verified and n in (resp.get("missing") or [])
                             or (n not in slices and n not in verified)]}

        conn = self._get_conn()
        with conn.cursor() as cur:
            # Serialize against a concurrent full dump load (its TRUNCATE+COPY
            # holds the same lock); session-scoped, released in finally.
            cur.execute("SELECT pg_advisory_lock(%s)", (MB_LOAD_LOCK_KEY,))
        try:
            with conn.cursor() as cur:
                for name, (core, blob_gz, entry) in verified.items():
                    matched = core.get("artists_matched") or {}
                    n_matched = sum(len(v) for v in matched.values())
                    stats["matched"] += n_matched
                    for t in core.get("truncated") or []:
                        if t not in stats["truncated"]:
                            stats["truncated"].append(t)
                    # Rows travel as canonical JSON strings inside the
                    # signed blob (deterministic sort) — decode here, and
                    # identifiers still never come from the wire: only
                    # SLICE_TABLES names/columns are used for SQL.
                    for t, cols in SLICE_TABLES.items():
                        raw = (core.get("tables") or {}).get(t)
                        if not raw:
                            continue
                        rows = [_json.loads(r) for r in raw]
                        stats["rows_received"] += len(rows)
                        inserted = psycopg2.extras.execute_values(
                            cur,
                            f"INSERT INTO {t} ({', '.join(cols)}) VALUES %s "
                            f"ON CONFLICT DO NOTHING RETURNING 1",
                            rows, page_size=1000, fetch=True,
                        )
                        stats["rows_inserted"] += len(inserted)

                    author = entry["author_pubkey"]
                    cur.execute("""
                        INSERT INTO mb_slice_fetches
                            (name_key, source_node, dump_version, matched_ids,
                             source_pubkey, receipt, payload_sha256,
                             source_addr)
                        VALUES (lower(btrim(%s)), %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (name_key) DO UPDATE SET
                            source_node = EXCLUDED.source_node,
                            dump_version = EXCLUDED.dump_version,
                            matched_ids = EXCLUDED.matched_ids,
                            source_pubkey = EXCLUDED.source_pubkey,
                            receipt = EXCLUDED.receipt,
                            payload_sha256 = EXCLUDED.payload_sha256,
                            source_addr = EXCLUDED.source_addr,
                            fetched_at = now()
                    """, (name, self.source_node or author,
                          core.get("dump_version"), n_matched,
                          author, entry["sig"],
                          _hashlib.sha256(blob_gz).hexdigest(),
                          self.source_addr))
                    # Replica inventory: keep the verified blob verbatim so
                    # this node can re-serve it with the author's signature.
                    # Oversized blobs are skipped (cap) — dump nodes serve
                    # those from their own cache.
                    store_slice_blob(
                        cur.connection, lower_key(name),
                        core.get("dump_version") or "", author,
                        entry["sig"], blob_gz, cap=True)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (MB_LOAD_LOCK_KEY,))
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
