"""
P2P ListenBrainz statistics slices — the second dump family (2026-09-20).

A node holding the ListenBrainz statistics dump (backend/lb_dump_load.py)
answers a batch of ARTIST MBIDs with one signed blob each: the artist's own
totals plus every recording credited to it, straight from lb_recording /
lb_artist. The requester upserts the rows into the same two tables, and the
artist page / genre page rank by them exactly as a dump node would. Keyed by
MBID, not name: ListenBrainz speaks MBIDs, the requester already holds its
artists' MBIDs (canon, carry, seed), and namesakes cannot collide.

Separate from the MusicBrainz slices in every artefact — context prefix,
protocol version, ledgers, capabilities, route family — because a node may
hold either dump and one ledger cannot track two families' holes.

VERSIONED from the start (what the MB family lacks): ``dump_version`` rides
inside the signed bytes and in every ledger row; a request carries
``min_version`` (the newest dump the requester learnt a source holds) and a
cached blob older than that is ``missing``, not an answer; a dump node never
serves a cache older than its own dump; the requester re-asks any artist
whose ledger row is older than the newest version seen — a signed
zero-match ("unknown to ListenBrainz at version X") included. Imports move
a row forward only, so slices of one recording credited to two artists may
arrive at different versions in any order.

Framework-agnostic like mb_slice_queries: functions take a psycopg2
connection; both peer surfaces (desktop/p2p/sync_server.py and
backend/routers/sync.py) import this module as-is.
"""

import base64
import gzip
import hashlib
import json
import logging
from typing import Dict, List, Optional, Tuple
from uuid import UUID

from desktop.p2p.mb_slice_queries import addr_uuid  # noqa: F401 — the one node_addr formula

logger = logging.getLogger(__name__)

# Mirrors backend/lb_dump_load.LB_LOAD_LOCK_KEY — the loader holds this
# advisory lock for its whole stage+aggregate+swap; a slice cut meanwhile
# would be signed from a half-built table and close an MBID for good.
LB_LOAD_LOCK_KEY = 0x6C626C64
DB_VERSION_KEY = "listenbrainz.db_version"

MAX_MBIDS_PER_REQUEST = 50
# The dump's per-user top-1000 lists leave a prolific artist with a few
# thousand credited recordings at most; the cap is a guard, `truncated`
# rides INSIDE the signed blob so the requester knows the slice is a head.
MAX_RECORDINGS_PER_SLICE = 5000
# Replicas keep blobs below this — LB blobs are tens of KB, so in practice
# everything; the cap is the mb_slice_blobs posture kept for symmetry.
RESERVE_BLOB_MAX_GZ = 2 * 1024 * 1024

# Domain separation: a receipt can never be replayed as a chat/sync/birth
# or MB-slice signature. Widening the payload later means bumping this AND
# wiping every node's ledgers — a signed hole closes an MBID for good.
RECEIPT_CONTEXT = b"sautium-lb-slice-v1:"
PROTOCOL_VERSION = 1


class DumpBusy(Exception):
    """A full dump load is in progress on the serving node — retry later."""


def mbid_key(value) -> Optional[str]:
    """The canonical (lowercase, hyphenated) form of an MBID string, or None
    when it is not one — the requester's key, the blob's key, the ledger's key."""
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value.strip()))
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Authorship receipt — per artist MBID
# ---------------------------------------------------------------------------

def slice_blob(artist_mbid: str, one: dict) -> bytes:
    """Canonical bytes of ONE artist's slice — what the dump node signs and
    every recipient hashes to verify. Rows are positional arrays sorted by
    recording MBID: Postgres row order never reaches the signature. Stored
    VERBATIM (gzipped) by recipients — re-serving hands back these exact
    bytes."""
    core = {
        "v": PROTOCOL_VERSION,
        "artist_mbid": mbid_key(artist_mbid),
        "dump_version": one["dump_version"],
        "artist": one.get("artist"),
        "recordings": sorted(one.get("recordings") or []),
        "truncated": bool(one.get("truncated")),
    }
    return json.dumps(core, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def receipt_message_for(blob: bytes) -> bytes:
    """The exact bytes the dump node signs / every recipient verifies."""
    return RECEIPT_CONTEXT + hashlib.sha256(blob).digest()


# ---------------------------------------------------------------------------
# Capability (is THIS node a dump holder?)
# ---------------------------------------------------------------------------

def local_dump_available(conn) -> Optional[str]:
    """Dump version string if a FULL load finished on this DSN (the one
    marker the loader writes) and the table still holds rows, else None. A
    slice node's lb_recording has rows too — its artists' slices — and no
    marker, so it never advertises a dump."""
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM user_settings WHERE key = %s", (DB_VERSION_KEY,))
        row = cur.fetchone()
        version = row[0] if row else None
        if not version:
            return None
        cur.execute("SELECT EXISTS (SELECT 1 FROM lb_recording)")
        if not cur.fetchone()[0]:
            return None
    return str(version)


# ---------------------------------------------------------------------------
# Serving side
# ---------------------------------------------------------------------------

def get_slice_one(conn, artist_mbid: str, version: str) -> dict:
    """One artist's totals + credited recordings from the local dump. Raises
    DumpBusy while the loader holds its lock."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (LB_LOAD_LOCK_KEY,))
        if not cur.fetchone()[0]:
            raise DumpBusy()
        try:
            cur.execute("""
                SELECT recording_mbid::text, listen_count, user_count, artist_mbids::text[]
                  FROM lb_recording
                 WHERE artist_mbids @> ARRAY[%s::uuid]
                 ORDER BY listen_count DESC, recording_mbid
                 LIMIT %s""", (artist_mbid, MAX_RECORDINGS_PER_SLICE + 1))
            rows = cur.fetchall()
            cur.execute("SELECT listen_count, user_count FROM lb_artist WHERE artist_mbid = %s",
                        (artist_mbid,))
            artist = cur.fetchone()
        finally:
            cur.execute("SELECT pg_advisory_unlock(%s)", (LB_LOAD_LOCK_KEY,))
    truncated = len(rows) > MAX_RECORDINGS_PER_SLICE
    return {
        "dump_version": version,
        "artist": [int(artist[0]), int(artist[1])] if artist else None,
        "recordings": [[r[0], int(r[1]), int(r[2]), sorted(x.lower() for x in (r[3] or []))]
                       for r in rows[:MAX_RECORDINGS_PER_SLICE]],
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# Blob cache + replication (one table, three roles — see mb_slice_blobs)
# ---------------------------------------------------------------------------

def cached_slice(conn, mbid: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT dump_version, author_pubkey, sig, blob_gz"
                    "  FROM lb_slice_blobs WHERE artist_mbid = %s", (mbid,))
        row = cur.fetchone()
    if not row:
        return None
    return {"dump_version": row[0], "author_pubkey": row[1].strip(),
            "sig": row[2].strip(), "blob_gz": bytes(row[3])}


def store_slice_blob(conn, mbid: str, dump_version: str, author_pubkey: str,
                     sig: str, blob_gz: bytes, cap: bool = True) -> bool:
    """Upsert one verified blob, forward only by dump version. `cap=True` is
    the replica posture (skip oversized); a dump node stores its own
    regardless — that is the cache."""
    if cap and len(blob_gz) > RESERVE_BLOB_MAX_GZ:
        return False
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO lb_slice_blobs
                   (artist_mbid, dump_version, author_pubkey, sig, blob_gz)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (artist_mbid) DO UPDATE SET
                dump_version = EXCLUDED.dump_version,
                author_pubkey = EXCLUDED.author_pubkey,
                sig = EXCLUDED.sig,
                blob_gz = EXCLUDED.blob_gz,
                created_at = now()
            WHERE EXCLUDED.dump_version >= lb_slice_blobs.dump_version""",
            (mbid, dump_version, author_pubkey, sig, blob_gz))
    return True


def count_slice_blobs(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM lb_slice_blobs")
        return int(cur.fetchone()[0])


def max_blob_version(conn) -> Optional[str]:
    """The newest dump version this node's inventory carries — what a
    replica-only network still learns a "newest known" from."""
    with conn.cursor() as cur:
        cur.execute("SELECT max(dump_version) FROM lb_slice_blobs")
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def serve_slices(conn, artist_mbids: list, min_version: Optional[str] = None,
                 sign_fn=None, author_pubkey: str = "") -> dict:
    """The shared v1 server body for both surfaces.

    Per MBID: a cached blob answers when it is at least ``min_version`` AND
    not older than this node's own dump (a dump node never re-serves a cache
    its loader has superseded); a dump node computes+signs+caches otherwise
    (`sign_fn` present, a local dump at least ``min_version``); everything
    else lands in `missing` — the requester takes misses to the next
    candidate. The response entry carries the ORIGINAL author's pubkey+sig,
    which on a replica is not this node's identity — and that is the point."""
    if not isinstance(artist_mbids, list) or not artist_mbids \
            or len(artist_mbids) > MAX_MBIDS_PER_REQUEST:
        raise ValueError(f"artist_mbids must be a list of 1..{MAX_MBIDS_PER_REQUEST}")
    keys = [mbid_key(m) for m in artist_mbids]
    if any(k is None for k in keys):
        raise ValueError("artist_mbids must be UUID strings")
    if min_version is not None and not isinstance(min_version, str):
        raise ValueError("min_version must be a string")

    local = local_dump_available(conn)
    can_build = (sign_fn is not None and local is not None
                 and (min_version is None or local >= min_version))
    slices, missing = {}, []
    for asked, key in zip(artist_mbids, keys):
        entry = cached_slice(conn, key)
        if entry is not None and ((min_version is not None and entry["dump_version"] < min_version)
                                  or (local is not None and entry["dump_version"] < local)):
            entry = None
        if entry is None and can_build:
            one = get_slice_one(conn, key, local)          # may raise DumpBusy
            blob = slice_blob(key, one)
            sig = sign_fn(receipt_message_for(blob)).hex()
            blob_gz = gzip.compress(blob)
            store_slice_blob(conn, key, local, author_pubkey, sig, blob_gz, cap=False)
            entry = {"dump_version": local, "author_pubkey": author_pubkey,
                     "sig": sig, "blob_gz": blob_gz}
        if entry is None:
            missing.append(asked)
            continue
        slices[asked] = {
            "dump_version": entry["dump_version"],
            "author_pubkey": entry["author_pubkey"],
            "sig": entry["sig"],
            "blob_gz": base64.b64encode(entry["blob_gz"]).decode("ascii"),
        }
    return {"v": PROTOCOL_VERSION, "slices": slices, "missing": missing}


# ---------------------------------------------------------------------------
# Requester side
# ---------------------------------------------------------------------------

def verify_slice_entry(artist_mbid: str, entry: dict,
                       min_version: Optional[str] = None) -> Optional[Tuple[dict, bytes]]:
    """Full per-artist verification on the receiving side: gunzip → hash →
    author signature → the blob's own version, artist and protocol match
    what was asked. Returns (core, blob_gz) or None. Every hop runs exactly
    this, against the ORIGINAL author's key. Anything malformed on the wire
    is a rejection, never an exception — the peer is untrusted."""
    from desktop.node_identity import verify_signature
    try:
        blob_gz = base64.b64decode(entry.get("blob_gz") or "")
        blob = gzip.decompress(blob_gz)
        author = entry.get("author_pubkey") or ""
        sig = entry.get("sig") or ""
        if not verify_signature(receipt_message_for(blob), bytes.fromhex(sig), author):
            return None
        core = json.loads(blob)
        if not isinstance(core, dict) or core.get("v") != PROTOCOL_VERSION:
            return None
        if core.get("artist_mbid") != mbid_key(artist_mbid):
            return None
        version = core.get("dump_version")
        if not isinstance(version, str) or not version:
            return None
        if min_version is not None and version < min_version:
            return None
        return core, blob_gz
    except (ValueError, TypeError, KeyError, OSError, EOFError):
        return None


def pending_slice_mbids(conn, limit: int = 200,
                        newest_version: Optional[str] = None) -> List[Tuple[str, Optional[str]]]:
    """(artist MBID, ledger version | None) the cycle should ask for, in
    priority order: the on-demand lane first (an artist page opened —
    lb_slice_requests), then OWNED artists, then ENGAGED ones (a completed,
    unskipped listen — the same rule that gates every per-artist fan-out).
    Phantom artists nobody opened are never asked in bulk.

    A ledger row closes an MBID only at its version: with a newer version
    reachable (``newest_version``) every older row — a signed zero-match
    included — is asked again."""
    with conn.cursor() as cur:
        cur.execute("""
            WITH cand AS (
                SELECT am.mbid, -1 AS tier, r.requested_at AS ts
                  FROM lb_slice_requests r
                  JOIN artist_mbids am ON am.artist_id = r.artist_id
                UNION ALL
                SELECT am.mbid, 0, NULL::timestamptz
                  FROM artist_mbids am
                 WHERE EXISTS (SELECT 1 FROM track_artists ta
                               JOIN media_files mf ON mf.track_id = ta.track_id
                               WHERE ta.artist_id = am.artist_id)
                UNION ALL
                SELECT am.mbid, 1, NULL::timestamptz
                  FROM artist_mbids am
                 WHERE EXISTS (SELECT 1 FROM track_artists ta
                               JOIN listening_history lh ON lh.track_id = ta.track_id
                               WHERE ta.artist_id = am.artist_id
                                 AND lh.completed AND NOT lh.skipped)
            )
            SELECT c.mbid::text, f.dump_version
              FROM cand c
              LEFT JOIN lb_slice_fetches f ON f.artist_mbid = c.mbid
             WHERE f.artist_mbid IS NULL
                OR (%(newest)s IS NOT NULL AND f.dump_version < %(newest)s)
             GROUP BY c.mbid, f.dump_version
             ORDER BY MIN(c.tier), MAX(c.ts) DESC NULLS LAST, c.mbid
             LIMIT %(lim)s
        """, {"newest": newest_version, "lim": limit})
        return [(r[0], r[1]) for r in cur.fetchall()]


def import_slice(conn, artist_mbid: str, core: dict, blob_gz: bytes, entry: dict,
                 source_node: str, source_addr: Optional[str]) -> int:
    """A verified slice into the local tables, in the caller's transaction:
    the rows (forward-only by dump version — a recording credited to two
    artists arrives from two slices, in any order), the ledger row, the
    re-serve blob, and the on-demand request it answers. Returns the number
    of recordings carried."""
    from psycopg2.extras import execute_values
    mbid = mbid_key(artist_mbid)
    version = core["dump_version"]
    recordings = core.get("recordings") or []
    with conn.cursor() as cur:
        if recordings:
            execute_values(cur, """
                INSERT INTO lb_recording
                       (recording_mbid, listen_count, user_count, artist_mbids, dump_version)
                VALUES %s
                ON CONFLICT (recording_mbid) DO UPDATE SET
                    listen_count = EXCLUDED.listen_count,
                    user_count   = EXCLUDED.user_count,
                    artist_mbids = EXCLUDED.artist_mbids,
                    dump_version = EXCLUDED.dump_version
                WHERE EXCLUDED.dump_version >= lb_recording.dump_version""",
                [(r[0], int(r[1]), int(r[2]), [str(x) for x in (r[3] or [])], version)
                 for r in recordings],
                template="(%s::uuid, %s, %s, %s::uuid[], %s)", page_size=500)
        artist = core.get("artist")
        if artist:
            cur.execute("""
                INSERT INTO lb_artist (artist_mbid, listen_count, user_count, dump_version)
                VALUES (%s::uuid, %s, %s, %s)
                ON CONFLICT (artist_mbid) DO UPDATE SET
                    listen_count = EXCLUDED.listen_count,
                    user_count   = EXCLUDED.user_count,
                    dump_version = EXCLUDED.dump_version
                WHERE EXCLUDED.dump_version >= lb_artist.dump_version""",
                (mbid, int(artist[0]), int(artist[1]), version))
        cur.execute("""
            INSERT INTO lb_slice_fetches
                   (artist_mbid, dump_version, recordings, source_node, source_pubkey,
                    receipt, payload_sha256, source_addr)
            VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s::uuid)
            ON CONFLICT (artist_mbid) DO UPDATE SET
                dump_version   = EXCLUDED.dump_version,
                recordings     = EXCLUDED.recordings,
                source_node    = EXCLUDED.source_node,
                source_pubkey  = EXCLUDED.source_pubkey,
                receipt        = EXCLUDED.receipt,
                payload_sha256 = EXCLUDED.payload_sha256,
                source_addr    = EXCLUDED.source_addr,
                fetched_at     = now()
            WHERE EXCLUDED.dump_version >= lb_slice_fetches.dump_version""",
            (mbid, version, len(recordings), source_node, entry.get("author_pubkey"),
             entry.get("sig"), hashlib.sha256(blob_gz).hexdigest(), source_addr))
        cur.execute("""
            DELETE FROM lb_slice_requests r
             USING artist_mbids am
             WHERE am.artist_id = r.artist_id AND am.mbid = %s::uuid""", (mbid,))
    store_slice_blob(conn, mbid, version, entry.get("author_pubkey") or "",
                     entry.get("sig") or "", blob_gz, cap=True)
    return len(recordings)
