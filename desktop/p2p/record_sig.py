"""Signed audio-analysis records — the attribution + content-address layer.

Phase 1 of enrichment signing (see docs/design/P2P-SYNC-INTEGRITY.md). A
signature proves *who* produced the data and *from what material*; it is
weight, never proof of truth — truth is established by a verifier who owns
the same material and recomputes.

THE SIGNED UNIT IS THE SEGMENT, NOT THE MEAN. CLAP analysis is windowed: a
track is a canonical 10s grid (window i = [i*10s, i*10s+10s) from the track
start), and embedding_segments stores a position-indexed SUBSET (K by
duration). The track-level embedding is the mean of that subset — and the
mean is NOT verifiable, because two honest nodes that sampled different K
produce different means from the same audio. Each segment, by contrast, is
CLAP(PCM[i*10s:(i+1)*10s], model): fully deterministic given the whole-track
PCM, the index and the model. So we sign per segment; a peer recomputes any
segment and confirms it. Each node computes its own mean locally from the
segments it holds.

CONTENT-ADDRESS STAYS WHOLE-TRACK: pcm_hash = BLAKE2b of the NATIVELY-decoded
PCM (source rate & channels, f32le — before the -ac1 -ar48000 analysis
conversion; lossless decode is deterministic across ffmpeg builds where the
48k resample is not). Whole-track because segment_index is only definable in
the whole-track frame; the 48k-mono analysis frame is a derivation defined by
grid_version. Per-segment signing needs no Merkle root — nodes hold different
subsets, so each segment is signed independently and travels self-contained.

Only hashes and IDs enter the signed string — never raw floats — so the
payload is byte-stable across signer and verifier. Float determinism lives
only in vector_hash / features_hash, taken over fixed-layout bytes.

Producer (signing) AND verifier (import) contract in one file. MIRRORED at
desktop/p2p/record_sig.py — the launcher build cannot import backend modules;
keep both copies in step, byte for byte below the header.
"""

import datetime
import hashlib
import json
import struct
from typing import Optional

# Record payload format. v2 (2026-07-06): the source material's
# duration_seconds joins the content-address section (after chromaprint) —
# it feeds the receiver's cheap import gate, so it must be tamper-evident.
# v1 records were re-signed as v2 pre-launch; no verifier supports v1.
RECORD_VERSION = 2

# The Worker timestamp string format. v2 (2026-07-10) appends the submitter's
# ip_hash (uuid5(NAMESPACE, "ip:"+ip), computed by the Worker) after the date —
# accountability for who notarized a batch. MIRRORS worker/verify.js
# TIMESTAMP_VERSION (they must match); decoupled from BIRTH_CERT_VERSION and
# from the record format above, so neither a birth-cert nor a record-format
# bump desyncs the notary.
TIMESTAMP_VERSION = 2

# Canonical grid identity: WINDOW_SECONDS=10, evenly-spaced-subset strategy
# (embeddings.segment_grid_indices). Bump when the grid definition changes so
# a verifier knows which windowing a signature refers to.
GRID_VERSION = 1


def blake2b_hex(data: bytes) -> str:
    """32-byte BLAKE2b digest, hex. The content-hash primitive throughout."""
    return hashlib.blake2b(data, digest_size=32).hexdigest()


def pcm_hash(pcm_bytes: bytes) -> str:
    """Content-address of the material: hash of the NATIVELY-decoded PCM bytes
    (source rate & channels, f32le) — NOT the file bytes and NOT the resampled
    48k analysis frame. Lossless decode is deterministic across ffmpeg builds,
    so this is stable where the resampled form is not (see the doc). The 48k
    analysis derivation is defined by grid_version, tolerance-verified."""
    return blake2b_hex(pcm_bytes)


def vector_hash(vector_bytes: bytes) -> str:
    """Hash of a stored vector's raw float32 bytes (pgvector round-trips them
    exactly), binding the segment's actual values into its signature."""
    return blake2b_hex(vector_bytes)


def vector_to_bytes(values) -> bytes:
    """Canonical byte layout of a vector: float32 little-endian. These are the
    exact bytes vector_hash covers and the sync wire carries (base64) — one
    layout on both ends, so a verifier never re-derives floats from text."""
    return struct.pack(f"<{len(values)}f", *values)


def vector_from_bytes(raw: bytes) -> list:
    """Inverse of vector_to_bytes."""
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


def _guard_chromaprint(chromaprint: Optional[str]) -> str:
    if chromaprint is None:
        return "-"
    if ":" in chromaprint:                       # AcoustID base64 is ':'-free
        raise ValueError("chromaprint must not contain ':'")
    return chromaprint


def _fmt_duration(duration_seconds: Optional[int]) -> str:
    """Whole seconds as a byte-stable string ('-' when unknown). Integer by
    design — the field feeds a ±5% import gate, and floats are a determinism
    hazard in signed strings."""
    return "-" if duration_seconds is None else str(int(duration_seconds))


def segment_payload(
    author_pubkey_hex: str,
    track_uuid: str,
    pcm_hash_hex: str,
    chromaprint: Optional[str],
    duration_seconds: Optional[int],
    model_uuid: str,
    segment_index: int,
    vector_hash_hex: str,
    grid_version: int = GRID_VERSION,
) -> bytes:
    """The exact bytes the author signs for one CLAP segment. author_pubkey is
    bound INTO the payload so the signature is an intrinsic statement by a named
    identity — evidence in a flag report names its author without relying on an
    external column. (It does not, and cannot, prevent re-signing deterministic
    public content; authorship theft is caught by timestamp priority.)
    pcm_hash + chromaprint + duration form the material declaration."""
    return ":".join([
        "sautium-record", f"v{RECORD_VERSION}", "segment", author_pubkey_hex.lower(),
        track_uuid.lower(), pcm_hash_hex.lower(), _guard_chromaprint(chromaprint),
        _fmt_duration(duration_seconds),
        model_uuid.lower(), str(grid_version), str(segment_index),
        vector_hash_hex.lower(),
    ]).encode("utf-8")


def features_payload(
    author_pubkey_hex: str,
    track_uuid: str,
    pcm_hash_hex: str,
    chromaprint: Optional[str],
    duration_seconds: Optional[int],
    analysis_version: int,
    features_hash_hex: str,
) -> bytes:
    """The bytes the author signs for a track's audio_features row — a
    parallel per-track record under the same whole-track content-address."""
    return ":".join([
        "sautium-record", f"v{RECORD_VERSION}", "features", author_pubkey_hex.lower(),
        track_uuid.lower(), pcm_hash_hex.lower(), _guard_chromaprint(chromaprint),
        _fmt_duration(duration_seconds),
        str(analysis_version), features_hash_hex.lower(),
    ]).encode("utf-8")


# Fixed order for the audio_features content hash — signer and verifier must
# agree byte-for-byte. analysis_version rides in the payload, not the blob.
FEATURE_ORDER = [
    "bpm", "key", "mode", "key_confidence", "energy", "energy_db", "brightness",
    "dynamic_range_db", "zero_crossing_rate", "danceability",
    "vocal_instrumental", "vocal_score", "instruments", "moods",
]


def _fmt_feature(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True, separators=(",", ":"))
    return str(v)


def canonical_features_blob(row: dict) -> bytes:
    """Byte-stable serialization whose hash enters the features payload. `row`
    may be a DB row or a sync wire item — FEATURE_ORDER values survive the
    JSON round-trip exactly (floats keep their shortest repr, JSONB dicts are
    re-dumped with sorted keys), so signer and verifier hash the same bytes."""
    return "|".join(_fmt_feature(row[c]) for c in FEATURE_ORDER).encode("utf-8")


# -- Enrichment records --------------------------------------------------------
#
# These say something DIFFERENT from the audio records above, and the difference
# is the whole design. A segment signature is a derivation claim: "I computed
# this vector from this exact PCM", and a verifier with the same file can redo
# it. Nobody derives a Last.fm biography. The node fetched it, and all it can
# honestly assert is "at this instant, this source told ME this".
#
# So the payload binds three things and no material hash: WHO relayed it, WHAT
# they relayed (content hash), and WHEN they were told. fetched_at lives INSIDE
# the signed bytes rather than beside them, because freshness is the entire
# basis on which one copy beats another — a timestamp an importer could edit
# would decide precedence while the signature went on looking valid.
#
# What this buys is attribution, not verifiability: a fabricated bio is
# indistinguishable from a real one until someone checks the source, but it is
# never anonymous, and one DELETE by author_pubkey removes everything a bad
# relay ever introduced.

ENRICHMENT_KINDS = ("artist_bio", "artist_tag", "similar_artist",
                    "track_stat", "genre_description",
                    "album", "album_track", "track_mbid")


def _fmt_fetched_at(fetched_at) -> str:
    """Whole seconds, UTC, RFC3339-ish. Sub-second precision would survive the
    signer and not the JSON round-trip through the wire, and a signature that
    depends on what a serialiser did to a microsecond is not a signature."""
    if fetched_at is None:
        return ""
    if isinstance(fetched_at, str):
        # The wire carries whatever the serialiser produced — isoformat gives
        # "+00:00" where the signer wrote "Z". Re-render rather than trust the
        # spelling: the format belongs to this contract, not to whichever JSON
        # encoder the row passed through, and a signature that depends on that
        # would verify on one path and fail on the other.
        try:
            fetched_at = datetime.datetime.fromisoformat(
                fetched_at.replace("Z", "+00:00"))
        except ValueError:
            return fetched_at
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=datetime.timezone.utc)
    return fetched_at.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def enrichment_payload(
    author_pubkey_hex: str,
    kind: str,
    entity_uuid: str,
    source: str,
    content_hash_hex: str,
    fetched_at,
) -> bytes:
    """The bytes an author signs for one enrichment row.

    `entity_uuid` is the artist/track/genre the row hangs off; `source` is the
    provider name as stored, so the same fact from Last.fm and from MusicBrainz
    are separate claims rather than one contested one."""
    if kind not in ENRICHMENT_KINDS:
        raise ValueError(f"unknown enrichment kind: {kind}")
    return ":".join([
        "sautium-record", f"v{RECORD_VERSION}", kind, author_pubkey_hex.lower(),
        entity_uuid.lower(), (source or "").lower(),
        content_hash_hex.lower(), _fmt_fetched_at(fetched_at),
    ]).encode("utf-8")


# Per-kind field order for the content hash. Signer and verifier must agree
# byte-for-byte, so this is a contract, not a convenience — appending a field
# invalidates every existing signature of that kind and needs a version bump.
# Field names are the WIRE names, not the column names, and deliberately so:
# a peer verifies what it received. tag_name and similar_artist_uuid travel
# where the local rows hold tag_id and similar_artist_id — both are UUIDv5 of
# the name, so the two forms are interchangeable, and the portable one is what
# the signature should cover.
ENRICHMENT_ORDER = {
    "artist_bio":        ["summary", "content", "url", "listeners", "playcount"],
    "artist_tag":        ["tag_name", "weight"],
    "similar_artist":    ["similar_artist_uuid", "match_score"],
    "track_stat":        ["listeners", "playcount"],
    "genre_description": ["summary", "content", "url"],
    # Carry — the sealed canon layer around pushed analysis. entity:
    # album → album_uuid; album_track / track_mbid → track_uuid. `source`
    # is "" and fetched_at = created_at for all three. album/album_track
    # seals feed the pusher's full-snapshot gate; track_mbid also travels
    # (v4). confidence is inside the signed bytes — an unsigned tier could
    # be inflated in transit.
    # cover_url travels but is NOT sealed: a CAA front-image URL is a
    # deterministic derivative of rg_mbid (re-derivable by anyone), and an
    # owned album's cover lives in its files anyway — sealing it would
    # forbid the importer from filling the blank with the CAA form.
    "album":             ["rg_mbid", "title", "release_year", "confidence"],
    "album_track":       ["album_uuid", "disc", "position", "length_ms",
                          "recording_mbid"],
    "track_mbid":        ["recording_mbid", "confidence"],
}


def canonical_enrichment_blob(kind: str, row: dict) -> bytes:
    """Byte-stable serialization of an enrichment row's payload fields.

    Reuses _fmt_feature: the same repr rules that survived the audio blob's
    JSON round-trip apply here for exactly the same reason."""
    order = ENRICHMENT_ORDER.get(kind)
    if order is None:
        raise ValueError(f"unknown enrichment kind: {kind}")
    return "|".join(_fmt_feature(row.get(c)) for c in order).encode("utf-8")


def record_leaf(signature_hex: str) -> str:
    """Merkle leaf for a signed record: hash of its author signature (which
    already commits to the payload and the key). The batch tree is built over
    these leaves; its root is what the Worker timestamps."""
    return blake2b_hex(bytes.fromhex(signature_hex))


def merkle_tree(leaves: list):
    """Build a binary Merkle tree over leaf hex-hashes. Returns
    (root_hex, proofs) where proofs[i] is the inclusion proof for leaves[i]:
    a list of [sibling_hex, side] pairs, side 'L'/'R' = sibling on the
    left/right. Odd layers duplicate the last node. Returns (None, []) empty."""
    if not leaves:
        return None, []
    layers = [[bytes.fromhex(h) for h in leaves]]
    while len(layers[-1]) > 1:
        cur = layers[-1]
        if len(cur) % 2:
            cur = cur + [cur[-1]]
            layers[-1] = cur
        layers.append([
            hashlib.blake2b(cur[i] + cur[i + 1], digest_size=32).digest()
            for i in range(0, len(cur), 2)
        ])
    root = layers[-1][0].hex()

    proofs = []
    for li in range(len(leaves)):
        proof, idx = [], li
        for depth in range(len(layers) - 1):
            sib = idx ^ 1
            side = "L" if idx % 2 else "R"      # where the SIBLING sits
            proof.append([layers[depth][sib].hex(), side])
            idx //= 2
        proofs.append(proof)
    return root, proofs


def verify_proof(leaf_hex: str, proof: list, root_hex: str) -> bool:
    """Check a Merkle inclusion proof: leaf is in the tree with this root."""
    try:
        h = bytes.fromhex(leaf_hex)
        for sib_hex, side in proof:
            sib = bytes.fromhex(sib_hex)
            pair = (sib + h) if side == "L" else (h + sib)
            h = hashlib.blake2b(pair, digest_size=32).digest()
        return h.hex() == root_hex
    except (ValueError, TypeError):
        return False


def timestamp_payload(root_hex: str, date_iso: str, ip_hash: str,
                      version: int = TIMESTAMP_VERSION) -> bytes:
    """The bytes the Worker (master authority) signs to notarize a batch root
    at a date, bound to the submitter's ip_hash — domain-separated from birth
    certs by the prefix. ip_hash is the Worker's peppered pseudonym of the
    submitter's address (the only party that sees the client IP):
    accountability for who notarized the batch, not a claim about who
    analyzed the audio. Versioned per BATCH (signing_batches.timestamp_version,
    in step with worker/verify.js TIMESTAMP_VERSION for new stamps): a bump
    adds a format here and invalidates nothing already issued. Decoupled
    from the record format — a record-format bump must not desync the Worker."""
    if version != 2:
        raise ValueError(f"unsupported timestamp payload version {version}")
    return (f"sautium-timestamp:v2:{root_hex.lower()}:"
            f"{date_iso}:{ip_hash.lower()}").encode("utf-8")


def verify_timestamp(root_hex: str, date_iso: str, ip_hash: str,
                     signature_hex: str, authority_pubkey_hex: str,
                     version: int = TIMESTAMP_VERSION) -> bool:
    """Check the Worker's countersignature over {root, date, ip_hash} in the
    payload format the batch was issued under; an unknown format verifies as
    False, never raises."""
    try:
        payload = timestamp_payload(root_hex, date_iso, ip_hash, version)
    except ValueError:
        return False
    return verify(payload, signature_hex, authority_pubkey_hex)


def verify_seal(payload: bytes, signature_hex: str, author_pubkey_hex: str,
                merkle_proof: list, batch_root_hex: str,
                worker_date: str, worker_ip_hash: str, worker_sig_hex: str,
                worker_authority_hex: str, trusted_authorities: list,
                worker_version: int = TIMESTAMP_VERSION) -> bool:
    """Full seal check for one record, in order:
      1. the author signed this exact content (author_pubkey is bound in the
         payload, so this also names the author),
      2. the record is included in its batch (Merkle proof → batch_root),
      3. that batch root was timestamped by a TRUSTED authority at worker_date,
         bound to the submitter's worker_ip_hash.
    Together they establish the priority claim: 'author_pubkey analyzed this
    content, sealed no later than worker_date.' A re-signer of the same public
    content necessarily lands in a different batch with a later stamp — the
    seal is what a verifier checks to know who was first."""
    return bool(
        worker_authority_hex in trusted_authorities
        and verify(payload, signature_hex, author_pubkey_hex)
        and verify_proof(record_leaf(signature_hex), merkle_proof, batch_root_hex)
        and verify_timestamp(batch_root_hex, worker_date, worker_ip_hash,
                             worker_sig_hex, worker_authority_hex,
                             worker_version))


def sign(payload: bytes, private_key) -> str:
    """Ed25519-sign a canonical payload; returns hex signature."""
    return private_key.sign(payload).hex()


def verify(payload: bytes, signature_hex: str, author_pubkey_hex: str) -> bool:
    """Cryptographic check that `payload` was signed by author_pubkey. Proves
    authorship only — the key's trust/weight is a separate layer."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(author_pubkey_hex))
        pub.verify(bytes.fromhex(signature_hex), payload)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
