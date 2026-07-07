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
from desktop.node_identity import verify_signature
from desktop.p2p.mb_slice_queries import (MB_LOAD_LOCK_KEY, SLICE_TABLES,
                                          payload_hash, receipt_message)

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
        """Fetch + import one batch of names. Returns per-batch stats; a dict
        with an 'error' key means the batch failed and may be retried against
        another peer (nothing was recorded in mb_slice_fetches)."""
        resp = self.peer_api.mb_slice(names)
        if not resp or "error" in resp or "detail" in resp:
            err = (resp or {}).get("error") or (resp or {}).get("detail") or "no response"
            logger.warning(f"MB slice fetch failed ({self.source_node}): {err}")
            return {"error": err}

        tables = resp.get("tables", {})
        for t, cols in resp.get("columns", {}).items():
            # Identifiers never come from the wire: unknown table or reordered
            # columns = version skew — refuse the whole batch.
            if t not in SLICE_TABLES or cols != SLICE_TABLES[t]:
                logger.warning(f"MB slice column mismatch for {t!r} — peer "
                               f"{self.source_node} runs an incompatible version")
                return {"error": f"column mismatch: {t}"}

        # Authorship receipt — strict: an unsigned or mis-signed response is
        # unattributable, and attribution is the whole trust model here
        # (content is public MB data, verified by spot-checks against
        # musicbrainz.org; the receipt pins WHO answered).
        pubkey = resp.get("author_pubkey")
        receipt = resp.get("receipt")
        if not pubkey or not receipt:
            logger.warning(f"MB slice from {self.source_node}: unsigned "
                           f"response — rejected")
            return {"error": "unsigned response"}
        try:
            valid = verify_signature(receipt_message(resp),
                                     bytes.fromhex(receipt), pubkey)
        except Exception as e:
            logger.warning(f"MB slice receipt malformed: {e}")
            return {"error": "malformed receipt"}
        if not valid:
            logger.warning(f"MB slice from {self.source_node}: receipt does "
                           f"not verify against {pubkey[:16]}… — rejected")
            return {"error": "receipt verification failed"}

        matched = resp.get("artists_matched", {})
        stats = {"names": len(names),
                 "matched": sum(len(v) for v in matched.values()),
                 "rows_received": sum(len(r) for r in tables.values()),
                 "rows_inserted": 0,
                 "truncated": resp.get("truncated", [])}

        conn = self._get_conn()
        with conn.cursor() as cur:
            # Serialize against a concurrent full dump load (its TRUNCATE+COPY
            # holds the same lock); session-scoped, released in finally.
            cur.execute("SELECT pg_advisory_lock(%s)", (MB_LOAD_LOCK_KEY,))
        try:
            with conn.cursor() as cur:
                for t, cols in SLICE_TABLES.items():
                    rows = tables.get(t)
                    if not rows:
                        continue
                    inserted = psycopg2.extras.execute_values(
                        cur,
                        f"INSERT INTO {t} ({', '.join(cols)}) VALUES %s "
                        f"ON CONFLICT DO NOTHING RETURNING 1",
                        rows, page_size=1000, fetch=True,
                    )
                    stats["rows_inserted"] += len(inserted)

                sha_hex = payload_hash(resp).hex()
                for name in names:
                    cur.execute("""
                        INSERT INTO mb_slice_fetches
                            (name_key, source_node, dump_version, matched_ids,
                             source_pubkey, receipt, payload_sha256)
                        VALUES (lower(btrim(%s)), %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (name_key) DO UPDATE SET
                            source_node = EXCLUDED.source_node,
                            dump_version = EXCLUDED.dump_version,
                            matched_ids = EXCLUDED.matched_ids,
                            source_pubkey = EXCLUDED.source_pubkey,
                            receipt = EXCLUDED.receipt,
                            payload_sha256 = EXCLUDED.payload_sha256,
                            fetched_at = now()
                    """, (name, self.source_node or pubkey,
                          resp.get("dump_version"),
                          len(matched.get(name, [])),
                          pubkey, receipt, sha_hex))
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
                       f"+{stats['rows_inserted']} rows")
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
