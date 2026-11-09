"""Sealed analysis records for tests that move them between nodes: one
author's segments, audio features and track binding for a track, signed as
backend/sign_audio.py signs them, in one Merkle batch, its root stamped by
the test authority (tests/p2p/test_witness.AUTHORITY) — what the import gate
verifies end to end. Not a test module."""

import uuid
from datetime import datetime, timezone

import psycopg2.extras

from desktop.p2p import record_sig as rs
from tests.p2p.test_witness import AUTHORITY, ISSUER

DIM = 512
STAMPED_AT = "2026-11-09T10:00:00Z"
BOUND_AT = datetime(2026, 11, 9, 9, 30, tzinfo=timezone.utc)


def pub(key) -> str:
    return key.public_key().public_bytes_raw().hex()


def vector(seed: int) -> list:
    """Values float32 keeps exactly, so the vector's hash survives the
    database's real[] and the wire's float32 bytes."""
    return [((seed * 37 + i) % 997 + 1) / 1024 for i in range(DIM)]


def _vec(values: list) -> str:
    return "[" + ",".join(repr(v) for v in values) + "]"


def seal_track(cur, author_key, track: str, model: str, *, recording: str,
               first_hand: bool = False, chromaprint: str = "AQAAsealed",
               duration: int = 95, segments: int = 3, analysis_version: int = 2) -> str:
    """An author's whole analysis of `track` on this node: its source (the
    author's own file when `first_hand`, else imported), the mean and
    `segments` sealed segments, sealed features and a sealed binding to
    `recording`. Returns the batch root."""
    author = pub(author_key)
    cur.execute("""INSERT INTO analysis_sources (track_id, chromaprint, duration_seconds,
                                                 grid_version, imported)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (track, chromaprint, duration, rs.GRID_VERSION, not first_hand))
    source = cur.fetchone()[0]
    cur.execute("""INSERT INTO embeddings (track_id, model_id, vector, analysis_source_id,
                                           analysis_version)
                   VALUES (%s, %s, %s::vector, %s, %s) RETURNING id""",
                (track, model, _vec(vector(99)), source, analysis_version))
    embedding = cur.fetchone()[0]

    signed = []                                   # (kind, key, signature)
    for i in range(segments):
        raw = rs.vector_to_bytes(vector(i))
        payload = rs.segment_payload(author, track, chromaprint, duration, model, i,
                                     rs.vector_hash(raw), grid_version=rs.GRID_VERSION)
        signed.append(("segment", i, rs.sign(payload, author_key)))
    features = {c: None for c in rs.FEATURE_ORDER}
    features.update(bpm=120.5, energy=0.25)
    signed.append(("features", None, rs.sign(rs.features_payload(
        author, track, chromaprint, duration, analysis_version,
        rs.blake2b_hex(rs.canonical_features_blob(features))), author_key)))
    binding = {"recording_mbid": recording, "confidence": "name_exact"}
    signed.append(("binding", None, rs.sign(rs.enrichment_payload(
        author, "track_mbid", track, "",
        rs.blake2b_hex(rs.canonical_enrichment_blob("track_mbid", binding)), BOUND_AT), author_key)))

    root, proofs = rs.merkle_tree([rs.record_leaf(sig) for _k, _i, sig in signed])
    ip_hash = str(uuid.uuid4())
    cur.execute("""INSERT INTO signing_batches (batch_root, author_pubkey, worker_date, ip_hash,
                                                worker_sig, authority, timestamp_version)
                   VALUES (%s, %s, %s, %s, %s, %s, 2)""",
                (root, author, STAMPED_AT, ip_hash,
                 AUTHORITY.sign(rs.timestamp_payload(root, STAMPED_AT, ip_hash)).hex(), ISSUER))
    for (kind, i, sig), proof in zip(signed, proofs):
        seal = (author, sig, root, psycopg2.extras.Json(proof))
        if kind == "segment":
            cur.execute("""INSERT INTO embedding_segments (embedding_id, segment_index, vector,
                                                           author_pubkey, signature, batch_root,
                                                           merkle_proof)
                           VALUES (%s, %s, %s::vector, %s, %s, %s, %s)""",
                        (embedding, i, _vec(vector(i)), *seal))
        elif kind == "features":
            cur.execute("""INSERT INTO audio_features (track_id, bpm, energy, analysis_source_id,
                                                       analysis_version, author_pubkey, signature,
                                                       batch_root, merkle_proof)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (track, features["bpm"], features["energy"], source, analysis_version,
                         *seal))
        else:
            cur.execute("""INSERT INTO track_mbids (recording_mbid, track_id, confidence, created_at,
                                                    author_pubkey, signature, batch_root,
                                                    merkle_proof, imported)
                           VALUES (%s, %s, 'name_exact', %s, %s, %s, %s, %s, %s)""",
                        (recording, track, BOUND_AT, *seal, not first_hand))
    return root
