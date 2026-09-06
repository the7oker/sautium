#!/usr/bin/env python3
"""Sign a node's audio-analysis records and Worker-timestamp the batch.

Phase 1 of enrichment signing (docs/design/P2P-SYNC-INTEGRITY.md). A record is
signable when its LINKED analysis_sources row (registered at analysis time by
the scanner / stream enricher — never recomputed here) is signable material:

  - origin='local' — ALL first-hand local analysis signs (the per-album
    signing_whitelist gate was dropped 2026-07-07: an unsigned network breaks
    integrity testing and the sync verify chain; selective privacy policy is
    off), or
  - origin='deezer' / 'youtube' — tier 3: a streamed source signs against
    the STREAM's pcm_hash, claiming no possession of any local rip. Until
    2026-09-02 only a lossless Deezer fetch qualified (decode-varying
    pcm_hash, lossy master); lossy streams sign too now — the analysis
    loses nothing measurable at lossy rates, the Deezer provider ships
    outside the distribution so YouTube is the only stream most nodes
    ever analyse, and a network where none of that signs carries nothing.
    The seal binds the author's OWN decode; is_lossless rides in the
    provenance so a peer can still rank sources.

Each unsigned CLAP segment (via its embeddings row) and audio_features row is
author-signed against that source's content-address; all new signatures batch
into one Merkle tree and the Worker timestamps the root. Authorship priority =
the Worker date. Records not yet linked to a source are skipped, not hashed
lazily — the link is the statement of WHAT was analyzed, and only the analysis
pass itself can make it.

Two stages, each incremental & idempotent (2026-09-06):

  sign()  — author-signs every record whose signature IS NULL. Local and
            instant; the row is final the moment it commits.
  stamp() — one Merkle batch over everything signed but not yet stamped,
            one Worker timestamp, proofs + root written back. The only
            stage on the network, so the only one that can fail — and a
            failure loses nothing, the signatures wait for the next call.

They used to be one atomic step, which coupled a free local operation to a
rate-limited remote one: a Worker 429 threw the whole batch away, and the
Worker round trip sat inside the window in which a canon pass could move
rows from under the batch (52 segments, 2026-08-25). The backend never calls
either directly — backend/notary.py owns them: it signs on every producer's
wake and stamps on its own cadence. A batch is capped at
MAX_RECORDS_PER_BATCH: every pending record, its Merkle proof and the JSON of
that proof sit in memory until the batch commits, and a full re-seal after an
identity migration (930k records on the master, 2026-08-25) took the whole
VM down with them. A backlog beyond the cap simply takes several calls —
`--until-done` loops them.

    docker exec sautium-backend python /app/sign_audio.py [--stage sign|stamp|both] [--limit N] [--dry-run] [--until-done]
"""

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg2
import psycopg2.extras

import record_sig as rs
from config import settings
from p2p_identity import load_signing_key
from record_sig import FEATURE_ORDER, canonical_features_blob

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sign_audio")

WORKER_URL = "https://sautium-verify.sautium.workers.dev"

# Records per batch (audio + enrichment). ~1 GB peak at this size.
MAX_RECORDS_PER_BATCH = 250_000

# A record signs only when its linked source is signable-classed AND
# first-hand (imported sources arrived over sync — signing analysis this node
# never computed would be authorship theft in reverse).
_SIGNABLE_SRC = "(NOT src.imported AND src.origin IS NOT NULL)"


def _pubkey_hex(key) -> str:
    from cryptography.hazmat.primitives import serialization
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw).hex()


def _vector_bytes(text: str) -> bytes:
    """pgvector text → the canonical float32-LE bytes vector_hash covers."""
    return rs.vector_to_bytes([float(x) for x in text.strip("[]").split(",")])


class NotaryThrottled(Exception):
    """The Worker answered 429 — its per-IP budget for this hour is spent, by
    this node or by whoever shares the address. `retry_after` is the Worker's
    Retry-After in seconds when it sent one, else None."""

    def __init__(self, retry_after=None):
        super().__init__("Worker throttled the timestamp request")
        self.retry_after = retry_after


def _timestamp_root(root: str) -> dict:
    req = urllib.request.Request(
        f"{WORKER_URL}/timestamp", method="POST",
        data=json.dumps({"root": root}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "Sautium/1.0"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            retry = e.headers.get("Retry-After")
            raise NotaryThrottled(int(retry) if retry and retry.isdigit() else None) from e
        raise


def connect():
    conn = psycopg2.connect(settings.database_url)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


def _pending_tracks(cur) -> list:
    """Tracks with an unsigned analysis row under a first-hand signable
    source. Driven from the unsigned rows (partial indexes), not from the
    tens of thousands of signable tracks — this runs on every wake."""
    cur.execute(f"""
        SELECT DISTINCT e.track_id::text AS tid
          FROM embedding_segments s
          JOIN embeddings e         ON e.id = s.embedding_id
          JOIN analysis_sources src ON src.id = e.analysis_source_id
         WHERE s.signature IS NULL AND {_SIGNABLE_SRC}
        UNION
        SELECT a.track_id::text
          FROM audio_features a
          JOIN analysis_sources src ON src.id = a.analysis_source_id
         WHERE a.signature IS NULL AND {_SIGNABLE_SRC}""")
    return [r["tid"] for r in cur.fetchall()]


# Enrichment records join the SAME batch as the audio ones: one Merkle root,
# one Worker timestamp, one round trip. They are cheap to sign and there is no
# reason to pay for a second notarisation.
#
# Only NOT imported rows. A signature here says "this source told ME this" —
# signing a row a peer sent us would restate their observation as our own, and
# the network would lose the one thing these signatures are for.
_ENRICHMENT_SOURCES = {
    "artist_bio": ("""
        SELECT b.id, b.artist_id::text AS entity, b.source, b.fetched_at,
               b.summary, b.content, b.url, b.listeners, b.playcount
        FROM artist_bios b
        WHERE b.signature IS NULL AND NOT b.imported""", "artist_bios"),
    "artist_tag": ("""
        SELECT at2.id, at2.artist_id::text AS entity, at2.source, at2.fetched_at,
               t.name AS tag_name, at2.weight
        FROM artist_tags at2 JOIN tags t ON t.id = at2.tag_id
        WHERE at2.signature IS NULL AND NOT at2.imported""", "artist_tags"),
    "similar_artist": ("""
        SELECT sa.id, sa.artist_id::text AS entity, sa.source, sa.fetched_at,
               sa.similar_artist_id::text AS similar_artist_uuid,
               sa.match_score::float AS match_score
        FROM similar_artists sa
        WHERE sa.signature IS NULL AND NOT sa.imported""", "similar_artists"),
    "track_stat": ("""
        SELECT ts.id, ts.track_id::text AS entity, ts.source, ts.fetched_at,
               ts.listeners, ts.playcount
        FROM track_stats ts
        WHERE ts.signature IS NULL AND NOT ts.imported""", "track_stats"),
    "genre_description": ("""
        SELECT gd.id, gd.genre_id::text AS entity, gd.source, gd.fetched_at,
               gd.summary, gd.content, gd.url
        FROM genre_descriptions gd
        WHERE gd.signature IS NULL AND NOT gd.imported""", "genre_descriptions"),
    # Carry v3 — the album layer. Only what stands on this node's OWN
    # signable analysis signs (_SIGNABLE_SRC: a local rip or a first-hand
    # stream): the ~3M MB-minted phantom tracklist rows nobody analyzed are
    # derived from the dump, not observed, and attesting them would sign a
    # copy of MusicBrainz. The gate used to be an owned FILE, which left every
    # stream-analyzed phantom's tracklist row unsealed — and the carry offer
    # reads exactly these seals, so first-hand analysis of 1659 phantoms never
    # left the node (2026-08-25). RG-anchored albums only — an unanchored
    # album is exactly the non-canon residue the snapshot must not carry.
    "album": ("""
        SELECT al.id, al.id::text AS entity, '' AS source,
               al.created_at AS fetched_at,
               al.musicbrainz_id::text AS rg_mbid, al.title,
               al.release_year,
               al.mb_match_confidence::text AS confidence
        FROM albums al
        WHERE al.signature IS NULL AND NOT al.imported
          AND al.musicbrainz_id IS NOT NULL
          AND EXISTS (SELECT 1 FROM album_tracks at2
                       WHERE at2.album_id = al.id AND {track_pred})""",
        "albums"),
    "album_track": ("""
        SELECT at2.id, at2.track_id::text AS entity, '' AS source,
               at2.created_at AS fetched_at,
               at2.album_id::text AS album_uuid, at2.disc, at2.position,
               at2.length_ms, at2.recording_mbid::text AS recording_mbid
        FROM album_tracks at2
        WHERE at2.signature IS NULL AND NOT at2.imported
          AND {track_pred}""", "album_tracks"),
    "track_mbid": ("""
        SELECT tm.recording_mbid AS id, tm.track_id::text AS entity,
               '' AS source, tm.created_at AS fetched_at,
               tm.recording_mbid::text AS recording_mbid,
               tm.confidence::text AS confidence
        FROM track_mbids tm
        WHERE tm.signature IS NULL AND NOT tm.imported""", "track_mbids"),
}

# The album layer's track predicate: whose tracklist rows (and the albums
# above them) are candidates. Full = every first-hand analyzed track — walks
# the 3.5 M-row phantom tracklist layer (~1 s), needed only when structure
# moved (a canon pass shed seals, a reconcile minted tracklists under
# already-signed tracks). Scoped = the tracks this pass signed — the wakes
# that only produced analysis.
_TRACK_PRED_FULL = f"""EXISTS (SELECT 1 FROM analysis_sources src
                              WHERE src.track_id = at2.track_id
                                AND {_SIGNABLE_SRC})"""
_TRACK_PRED_SCOPED = "at2.track_id = ANY(%(tids)s::uuid[])"
_ALBUM_LAYER = ("album", "album_track")

# UPDATE-phase PK column and cast per table; everything not listed uses the
# SERIAL `id`. track_mbids is keyed by the MBID itself.
_TABLE_PK = {"track_mbids": ("recording_mbid", "uuid"),
             "albums": ("id", "uuid"),
             "album_tracks": ("id", "bigint")}

# The entity uuid each seal's payload binds, as a predicate on the row being
# signed. A canon pass (artist split, merge, re-key) can move a row to
# another uuid between the SELECT and the UPDATE; the guard triggers cannot
# catch a signature that arrives AFTER the change, so the UPDATE asserts what
# was signed and skips rows that moved — they stay unsigned and the next pass
# signs them under their new uuid. The window used to hold a Worker round
# trip (52 segments of tracks split mid-batch verified invalid, 2026-08-25);
# signing no longer waits on the network, so it is the seconds of one pass.
_TABLE_ENTITY = {"embedding_segments": None,       # via embeddings.track_id, see below
                 "audio_features": "track_id", "track_stats": "track_id",
                 "album_tracks": "track_id", "track_mbids": "track_id",
                 "artist_bios": "artist_id", "artist_tags": "artist_id",
                 "similar_artists": "artist_id", "genre_descriptions": "genre_id",
                 "albums": "id"}


def _materialize_owned_tracklists(cur) -> int:
    """album_tracks rows for owned tracks, from their files' tags.

    The owned layer lives in albums → album_variants → media_files, so most
    owned tracks have no album_tracks row at all — that junction was built
    for PHANTOM tracklists. The carry snapshot needs one sealed tracklist
    shape for both, and the file's own tags (disc, track number, duration,
    recording mbid) are this node's first-hand observation of it. Every
    tagged file counts, not only the analysis source (the 2026-09-03
    change): an album's tracklist is a property of its files, not of which
    copy the GPU happened to read — with the source-only rule a rip whose
    tracks were analysed from a best-of or a box set had no tracklist at
    all (922 of 3 747 owned albums on the reference library). Idempotent
    and incremental: ON CONFLICT keeps whatever row already claims the slot
    (an MB-minted one outranks a re-derivation). Tracks with no tag number
    are skipped, not invented — a made-up position is not an observation.
    Runs before every signing pass, so a fresh scan's rows are sealed by the
    same pass that notices them; returns the track ids of the rows it minted
    so a scoped pass seals them too. Phantom rows need no materializing — the
    MB mint wrote them — they seal once the track carries signable analysis."""
    cur.execute("""
        INSERT INTO album_tracks
            (album_id, track_id, disc, position, recording_mbid, length_ms)
        SELECT av.album_id, mf.track_id, COALESCE(mf.disc_number, 1),
               mf.track_number, mf.recording_mbid,
               (mf.duration_seconds * 1000)::int
        FROM media_files mf
        JOIN album_variants av ON av.id = mf.album_variant_id
        WHERE mf.track_number IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM album_tracks at2
                           WHERE at2.album_id = av.album_id
                             AND at2.track_id = mf.track_id)
        ON CONFLICT (album_id, disc, position) DO NOTHING
        RETURNING track_id::text""")
    return [r["track_id"] for r in cur.fetchall()]


def _collect_enrichment(cur, author, key, pending, chunk=20000,
                        budget=None, track_ids=None) -> int:
    """Append signed enrichment records to `pending`, at most `budget` of
    them (None = all). The album layer is scoped to `track_ids` when given
    (None = full scan; an empty list = nothing to seal there). Returns how
    many."""
    added = 0
    for kind, (sql, table) in _ENRICHMENT_SOURCES.items():
        if budget is not None and added >= budget:
            break
        params = None
        if kind in _ALBUM_LAYER:
            if track_ids is None:
                sql = sql.format(track_pred=_TRACK_PRED_FULL)
            elif not track_ids:
                continue
            else:
                sql = sql.format(track_pred=_TRACK_PRED_SCOPED)
                params = {"tids": track_ids}
        cur.execute(sql, params)
        while True:
            rows = cur.fetchmany(chunk)
            if not rows:
                break
            for r in rows:
                if budget is not None and added >= budget:
                    break
                content = rs.blake2b_hex(rs.canonical_enrichment_blob(kind, r))
                payload = rs.enrichment_payload(author, kind, r["entity"],
                                                r["source"], content,
                                                r["fetched_at"])
                pending.append((table, r["id"], rs.sign(payload, key), r["entity"]))
                added += 1
            if budget is not None and added >= budget:
                break
    return added


def _signer():
    """(key, author pubkey hex), or None for a node without an identity —
    sealing is skipped there, never fatal to the caller."""
    key = load_signing_key(settings)
    if key is None:
        logger.info("no signing identity (P2P_USERNAME/P2P_PASSWORD) — "
                    "sealing skipped")
        return None
    return key, _pubkey_hex(key)


def sign(conn, full=False, limit=None, dry_run=False,
         max_records=MAX_RECORDS_PER_BATCH) -> int:
    """Stage 1 — author-sign every record that became signable. Local and
    instant: no Worker, no batch. Returns the number of records signed;
    a call that fills the cap means more are waiting, so loop until 0 to
    drain a backlog.

    The analysis layer is found from its unsigned rows (cheap on every
    wake); the album layer above it is scoped to those tracks unless
    `full` — see _TRACK_PRED_FULL for when the end-to-end scan is due."""
    signer = _signer()
    if signer is None:
        return 0
    key, author = signer
    cur = conn.cursor()

    minted = _materialize_owned_tracklists(cur)
    if minted:
        logger.info("materialized %d owned tracklist row(s)", len(minted))

    track_ids = sorted(_pending_tracks(cur))
    if limit:
        track_ids = track_ids[:limit]
    if track_ids:
        logger.info("tracks with unsigned analysis: %d", len(track_ids))

    # pending: (table, pk, signature, entity uuid) across every table
    pending, capped = [], False
    for tid in track_ids:
        if max_records and len(pending) >= max_records:
            capped = True
            break
        params = {"tid": tid}

        cur.execute(f"""
            SELECT s.id, s.segment_index, s.vector::text AS vec,
                   e.model_id::text AS model,
                   src.pcm_hash, src.chromaprint, src.duration_seconds,
                   src.grid_version
            FROM embedding_segments s
            JOIN embeddings e         ON e.id = s.embedding_id
            JOIN analysis_sources src ON src.id = e.analysis_source_id
            WHERE e.track_id = %(tid)s AND s.signature IS NULL
              AND {_SIGNABLE_SRC}
            ORDER BY s.segment_index""", params)
        for seg in cur.fetchall():
            vh = rs.vector_hash(_vector_bytes(seg["vec"]))
            payload = rs.segment_payload(
                author, tid, seg["pcm_hash"], seg["chromaprint"],
                seg["duration_seconds"],
                seg["model"], seg["segment_index"], vh,
                grid_version=seg["grid_version"])
            pending.append(("embedding_segments", seg["id"], rs.sign(payload, key), tid))

        cur.execute(f"""
            SELECT a.id, a.analysis_version, {', '.join(FEATURE_ORDER)},
                   src.pcm_hash, src.chromaprint, src.duration_seconds
            FROM audio_features a
            JOIN analysis_sources src ON src.id = a.analysis_source_id
            WHERE a.track_id = %(tid)s AND a.signature IS NULL
              AND {_SIGNABLE_SRC}""", params)
        feat = cur.fetchone()
        if feat:
            fh = rs.blake2b_hex(canonical_features_blob(feat))
            payload = rs.features_payload(author, tid, feat["pcm_hash"],
                                          feat["chromaprint"],
                                          feat["duration_seconds"],
                                          feat["analysis_version"], fh)
            pending.append(("audio_features", feat["id"], rs.sign(payload, key), tid))

    budget = max(0, max_records - len(pending)) if max_records else None
    scope = None if full else sorted(set(track_ids) | set(minted))
    enriched = _collect_enrichment(cur, author, key, pending, budget=budget,
                                   track_ids=scope)
    if enriched:
        logger.info("enrichment records to sign: %d", enriched)
    if max_records and len(pending) >= max_records:
        capped = True

    if dry_run:
        conn.rollback()
        logger.info("dry-run — %d record(s) would sign, nothing stored",
                    len(pending))
        return len(pending)
    if not pending:
        conn.commit()          # the materialized tracklist rows, if any
        return 0

    # Grouped and batched: a statement per record was fine at audio scale and
    # is not at enrichment scale — the first unified pass signed 300k+ rows.
    by_table: dict = {}
    for table, pk, sig, entity in pending:
        by_table.setdefault(table, []).append((pk, author, sig, entity))
    for table, rows in by_table.items():
        pk_col, pk_cast = _TABLE_PK.get(table, ("id", "int"))
        entity_col = _TABLE_ENTITY[table]
        if entity_col is None:
            entity_sql = ("FROM (VALUES %s) AS v(pk, author, sig, entity), "
                          "embeddings e "
                          "WHERE t.id = v.pk AND e.id = t.embedding_id "
                          "AND e.track_id = v.entity")
        else:
            entity_sql = (f"FROM (VALUES %s) AS v(pk, author, sig, entity) "
                          f"WHERE t.{pk_col} = v.pk AND t.{entity_col} = v.entity")
        signed = 0
        for i in range(0, len(rows), 500):
            page = rows[i:i + 500]
            psycopg2.extras.execute_values(
                cur,
                f"""UPDATE {table} AS t
                       SET author_pubkey = v.author, signature = v.sig
                    {entity_sql} AND t.signature IS NULL""",
                page,
                template=f"(%s::{pk_cast}, %s, %s, %s::uuid)",
                page_size=500,
            )
            signed += cur.rowcount
        if signed < len(rows):
            logger.warning("%d of %d %s rows moved to another uuid while the pass "
                           "was open — left unsigned for the next pass",
                           len(rows) - signed, len(rows), table)
        logger.info("signed %d rows in %s", signed, table)
    conn.commit()
    logger.info("signed %d record(s) over %d track(s)", len(pending), len(track_ids))
    if capped:
        logger.info("pass capped at %d records — run again to continue",
                    max_records)
    return len(pending)


# Stage 2 finds "signed, awaiting stamp" by scan; the partial indexes of
# migration 011 make that O(pending) on the tables that are gigabytes.
_STAMP_TABLES = ("embedding_segments", "audio_features", "artist_bios",
                 "artist_tags", "similar_artists", "track_stats",
                 "genre_descriptions", "albums", "album_tracks", "track_mbids")


def stamp(conn, dry_run=False, max_records=MAX_RECORDS_PER_BATCH) -> int:
    """Stage 2 — one Merkle batch over everything signed but not yet
    stamped, one Worker call, root + proofs written back. The only stage
    on the network, so the only one that can fail or be throttled
    (NotaryThrottled) — and failing loses nothing: the signatures stay and
    the next call finds them again. One batch per author key, so a rotated
    identity's leftovers stamp under their own author. Returns the number
    of records stamped; a call that fills the cap means more are waiting."""
    cur = conn.cursor()
    pending = []
    for table in _STAMP_TABLES:
        room = max_records - len(pending) if max_records else None
        if room is not None and room <= 0:
            break
        pk_col, _ = _TABLE_PK.get(table, ("id", "int"))
        cur.execute(f"""SELECT {pk_col} AS pk, author_pubkey, signature
                          FROM {table}
                         WHERE signature IS NOT NULL AND batch_root IS NULL
                         ORDER BY {pk_col}"""
                    + (f" LIMIT {int(room)}" if room is not None else ""))
        pending.extend((table, r["pk"], r["author_pubkey"], r["signature"])
                       for r in cur.fetchall())
    if not pending:
        conn.rollback()
        return 0

    by_author: dict = {}
    for rec in pending:
        by_author.setdefault(rec[2], []).append(rec)

    stamped = 0
    for author, recs in by_author.items():
        leaves = [rs.record_leaf(sig) for _, _, _, sig in recs]
        root, proofs = rs.merkle_tree(leaves)
        logger.info("batch: %d records by %s…, root %s",
                    len(recs), author[:8], root[:16])
        if dry_run:
            continue

        ts = _timestamp_root(root)
        if not rs.verify_timestamp(root, ts["date"], ts["ip_hash"], ts["sig"],
                                   ts["authority"]):
            conn.rollback()
            raise RuntimeError("Worker timestamp failed verification — aborting")

        cur.execute("""INSERT INTO signing_batches
                         (batch_root, author_pubkey, worker_date, ip_hash,
                          worker_sig, authority)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (root, author, ts["date"], ts["ip_hash"], ts["sig"],
                     ts["authority"]))
        by_table: dict = {}
        for (table, pk, _, sig), proof in zip(recs, proofs):
            by_table.setdefault(table, []).append((pk, sig, root, json.dumps(proof)))
        for table, rows in by_table.items():
            pk_col, pk_cast = _TABLE_PK.get(table, ("id", "int"))
            done = 0
            for i in range(0, len(rows), 500):
                page = rows[i:i + 500]
                # The signature predicate is the seal: a row re-signed while
                # the batch was open holds a leaf this tree never had, so it
                # stays unstamped for the next batch instead of carrying a
                # proof that would never verify.
                psycopg2.extras.execute_values(
                    cur,
                    f"""UPDATE {table} AS t
                           SET batch_root = v.root, merkle_proof = v.proof::jsonb
                          FROM (VALUES %s) AS v(pk, sig, root, proof)
                         WHERE t.{pk_col} = v.pk AND t.signature = v.sig
                           AND t.batch_root IS NULL""",
                    page,
                    template=f"(%s::{pk_cast}, %s, %s, %s)",
                    page_size=500,
                )
                done += cur.rowcount
            if done < len(rows):
                logger.warning("%d of %d %s rows re-signed while the batch was "
                               "open — left for the next stamp",
                               len(rows) - done, len(rows), table)
            logger.info("stamped %d rows in %s", done, table)
        conn.commit()
        logger.info("stamped %d record(s) in batch %s @ %s",
                    len(recs), root[:16], ts["date"])
        stamped += len(recs)

    if dry_run:
        conn.rollback()
        logger.info("dry-run — %d record(s) would stamp, not timestamped or stored",
                    len(pending))
        return len(pending)
    if max_records and len(pending) >= max_records:
        logger.info("batch capped at %d records — run again to continue",
                    max_records)
    return stamped


def run(stage="both", limit=None, dry_run=False, until_done=False,
        max_records=MAX_RECORDS_PER_BATCH) -> int:
    """The CLI shape: the stages back to back on one connection. The backend
    does not call this — backend/notary.py drives sign() and stamp() on the
    producers' wakes and its own stamp cadence. Returns records touched."""
    conn = connect()
    total = 0
    if stage in ("sign", "both"):
        while True:
            n = sign(conn, full=True, limit=limit, dry_run=dry_run,
                     max_records=max_records)
            total += n
            if not until_done or dry_run or n < max_records or not max_records:
                break
    if stage in ("stamp", "both"):
        while True:
            n = stamp(conn, dry_run=dry_run, max_records=max_records)
            total += n
            if not until_done or dry_run or n < max_records or not max_records:
                break
    conn.close()
    return total


def verify_all() -> bool:
    """Re-verify the seal of every signed record against the stored content.

    For each signed segment / audio_features row it rebuilds the exact signed
    payload FROM the stored data — the content hashes are recomputed from the
    actual vector / feature values, so this also proves the stored data is what
    was sealed — then runs the full chain (author signature → Merkle inclusion
    → Worker timestamp by a trusted authority). A signed row whose provenance
    link is missing counts as INVALID: the seal asserts a content-address the
    row no longer carries. Reports valid/invalid counts; returns True iff every
    seal holds. (This is the seal check; a --deep audit that also re-decodes
    the audio to confirm pcm_hash against the file is a separate, heavier pass.)"""
    from birth_authority import TRUSTED_AUTHORITIES

    conn = psycopg2.connect(settings.database_url)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    cur = conn.cursor()

    cur.execute("SELECT batch_root, worker_date, ip_hash, worker_sig, authority "
                "FROM signing_batches")
    batches = {
        b["batch_root"]: (
            b["worker_date"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            str(b["ip_hash"]), b["worker_sig"], b["authority"])
        for b in cur.fetchall()
    }

    ok, bad, bad_samples = 0, 0, []

    def _seal_ok(payload, r):
        b = batches.get(r["batch_root"])
        return bool(b and rs.verify_seal(
            payload, r["signature"], r["author_pubkey"], r["merkle_proof"],
            r["batch_root"], b[0], b[1], b[2], b[3], TRUSTED_AUTHORITIES))

    # Server-side cursor: a signed library is hundreds of thousands of
    # segments, each row carrying a 512-float vector as text — fetchall()
    # here was an OOM kill waiting for the library to grow (it did: the tool
    # worked at 3.7k records and was killed at 452k).
    seg_cur = conn.cursor(name="verify_segments",
                          cursor_factory=psycopg2.extras.RealDictCursor)
    seg_cur.itersize = 2000
    seg_cur.execute("""SELECT s.author_pubkey, s.signature, s.merkle_proof, s.batch_root,
                          e.track_id::text tid, e.model_id::text model,
                          s.segment_index idx, s.vector::text vec,
                          p.pcm_hash, p.chromaprint, p.duration_seconds,
                          p.grid_version
                   FROM embedding_segments s
                   JOIN embeddings e ON e.id = s.embedding_id
                   LEFT JOIN analysis_sources p ON p.id = e.analysis_source_id
                   WHERE s.signature IS NOT NULL""")
    for r in seg_cur:
        if r["pcm_hash"] is None:
            bad += 1
            if len(bad_samples) < 5:
                bad_samples.append(f"segment {r['tid'][:8]}#{r['idx']} UNLINKED")
            continue
        vh = rs.vector_hash(_vector_bytes(r["vec"]))
        payload = rs.segment_payload(r["author_pubkey"], r["tid"], r["pcm_hash"],
                                     r["chromaprint"], r["duration_seconds"],
                                     r["model"], r["idx"], vh,
                                     grid_version=r["grid_version"])
        if _seal_ok(payload, r):
            ok += 1
        else:
            bad += 1
            if len(bad_samples) < 5:
                bad_samples.append(f"segment {r['tid'][:8]}#{r['idx']}")
    seg_cur.close()
    logger.info("segments checked: %d valid, %d invalid", ok, bad)

    feat_cur = conn.cursor(name="verify_features",
                           cursor_factory=psycopg2.extras.RealDictCursor)
    feat_cur.itersize = 2000
    feat_cur.execute(f"""SELECT a.author_pubkey, a.signature, a.merkle_proof, a.batch_root,
                           a.track_id::text tid, a.analysis_version,
                           {', '.join('a.' + c for c in FEATURE_ORDER)},
                           p.pcm_hash, p.chromaprint, p.duration_seconds
                    FROM audio_features a
                    LEFT JOIN analysis_sources p ON p.id = a.analysis_source_id
                    WHERE a.signature IS NOT NULL""")
    for r in feat_cur:
        if r["pcm_hash"] is None:
            bad += 1
            if len(bad_samples) < 5:
                bad_samples.append(f"features {r['tid'][:8]} UNLINKED")
            continue
        fh = rs.blake2b_hex(canonical_features_blob(r))
        payload = rs.features_payload(r["author_pubkey"], r["tid"], r["pcm_hash"],
                                      r["chromaprint"], r["duration_seconds"],
                                      r["analysis_version"], fh)
        if _seal_ok(payload, r):
            ok += 1
        else:
            bad += 1
            if len(bad_samples) < 5:
                bad_samples.append(f"features {r['tid'][:8]}")
    feat_cur.close()

    logger.info("seal verification: %d valid, %d invalid", ok, bad)
    if bad_samples:
        logger.warning("invalid: %s", bad_samples)
    return bad == 0


def resign_timestamps():
    """Re-timestamp existing batches under the Worker's current format.

    Author signatures and Merkle roots depend on neither the IP nor the date,
    so re-submitting a root refreshes ONLY the Worker countersignature — the
    per-record seals (488k rows) are untouched, and the Worker preserves each
    root's original date, so authorship priority never regresses.

    Every batch is re-submitted and the WORKER decides what to refresh (it
    versions both the timestamp payload and the ip_hash derivation): a root
    already in the current format comes back byte-identical, so this stays
    idempotent without the client having to know which formats exist.

    One Worker request per root, committed as it lands: the Worker's per-IP
    budget is an hour's worth of requests, and a run over more batches than
    that stops at the throttle with its progress kept — re-run later for the
    rest (a root already refreshed comes back byte-identical)."""
    conn = connect()
    cur = conn.cursor()

    cur.execute("SELECT batch_root FROM signing_batches ORDER BY created_at")
    roots = [r["batch_root"] for r in cur.fetchall()]
    if not roots:
        logger.info("no batches to re-timestamp")
        return
    logger.info("re-timestamping %d batch(es)", len(roots))

    for i, root in enumerate(roots):
        try:
            ts = _timestamp_root(root)
        except NotaryThrottled as e:
            logger.warning("Worker throttled after %d of %d batch(es) — re-run "
                           "in %s to continue", i, len(roots),
                           f"{e.retry_after}s" if e.retry_after else "an hour")
            return
        if not rs.verify_timestamp(root, ts["date"], ts["ip_hash"], ts["sig"],
                                   ts["authority"]):
            conn.rollback()
            raise RuntimeError(f"re-timestamp failed verification: {root[:16]}")
        cur.execute("""UPDATE signing_batches
                       SET worker_date=%s, ip_hash=%s, worker_sig=%s, authority=%s
                       WHERE batch_root=%s""",
                    (ts["date"], ts["ip_hash"], ts["sig"], ts["authority"], root))
        conn.commit()
    logger.info("re-timestamped %d batch(es)", len(roots))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true",
                    help="re-verify the seal of every signed record; sign nothing")
    ap.add_argument("--resign", action="store_true",
                    help="re-timestamp existing batches under the current format "
                         "(adds ip_hash); signs no new records")
    ap.add_argument("--until-done", action="store_true",
                    help="keep running capped batches until nothing is left")
    ap.add_argument("--max-records", type=int, default=MAX_RECORDS_PER_BATCH,
                    help="records per batch (0 = uncapped)")
    ap.add_argument("--stage", choices=("sign", "stamp", "both"), default="both",
                    help="author-sign only, Worker-stamp only, or both (default)")
    a = ap.parse_args()
    if a.verify:
        sys.exit(0 if verify_all() else 1)
    if a.resign:
        resign_timestamps()
        sys.exit(0)
    run(stage=a.stage, limit=a.limit, dry_run=a.dry_run,
        until_done=a.until_done, max_records=a.max_records)
