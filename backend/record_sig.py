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

CONTENT-ADDRESS STAYS WHOLE-TRACK: pcm_hash = BLAKE2b of the decoded
48kHz-mono PCM (load_full_track_48k), because segment_index is only definable
in the whole-track frame. Per-segment signing needs no Merkle root — nodes
hold different subsets, so each segment is signed independently and travels
self-contained.

Only hashes and IDs enter the signed string — never raw floats — so the
payload is byte-stable across signer and verifier. Float determinism lives
only in vector_hash / features_hash, taken over fixed-layout bytes.

This is the producer (signing) side. The importer's verify side lands with
the P2P sync refactor; when it does, mirror this file to desktop/p2p/ (the
launcher build cannot import backend modules) and keep both in step.
"""

import hashlib
from typing import Optional

RECORD_VERSION = 1

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


def _guard_chromaprint(chromaprint: Optional[str]) -> str:
    if chromaprint is None:
        return "-"
    if ":" in chromaprint:                       # AcoustID base64 is ':'-free
        raise ValueError("chromaprint must not contain ':'")
    return chromaprint


def segment_payload(
    author_pubkey_hex: str,
    track_uuid: str,
    pcm_hash_hex: str,
    chromaprint: Optional[str],
    model_uuid: str,
    segment_index: int,
    vector_hash_hex: str,
    grid_version: int = GRID_VERSION,
) -> bytes:
    """The exact bytes the author signs for one CLAP segment. author_pubkey is
    bound INTO the payload so the signature is an intrinsic statement by a named
    identity — evidence in a flag report names its author without relying on an
    external column. (It does not, and cannot, prevent re-signing deterministic
    public content; authorship theft is caught by timestamp priority.)"""
    return ":".join([
        "sautium-record", f"v{RECORD_VERSION}", "segment", author_pubkey_hex.lower(),
        track_uuid.lower(), pcm_hash_hex.lower(), _guard_chromaprint(chromaprint),
        model_uuid.lower(), str(grid_version), str(segment_index),
        vector_hash_hex.lower(),
    ]).encode("utf-8")


def features_payload(
    author_pubkey_hex: str,
    track_uuid: str,
    pcm_hash_hex: str,
    chromaprint: Optional[str],
    analysis_version: int,
    features_hash_hex: str,
) -> bytes:
    """The bytes the author signs for a track's audio_features row — a
    parallel per-track record under the same whole-track content-address."""
    return ":".join([
        "sautium-record", f"v{RECORD_VERSION}", "features", author_pubkey_hex.lower(),
        track_uuid.lower(), pcm_hash_hex.lower(), _guard_chromaprint(chromaprint),
        str(analysis_version), features_hash_hex.lower(),
    ]).encode("utf-8")


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


def timestamp_payload(root_hex: str, date_iso: str) -> bytes:
    """The bytes the Worker (master authority) signs to notarize a batch
    root at a date — domain-separated from birth certs by the prefix."""
    return f"sautium-timestamp:v{RECORD_VERSION}:{root_hex.lower()}:{date_iso}".encode("utf-8")


def verify_timestamp(root_hex: str, date_iso: str, signature_hex: str,
                     authority_pubkey_hex: str) -> bool:
    """Check the Worker's countersignature over {root, date}."""
    return verify(timestamp_payload(root_hex, date_iso), signature_hex,
                  authority_pubkey_hex)


def verify_seal(payload: bytes, signature_hex: str, author_pubkey_hex: str,
                merkle_proof: list, batch_root_hex: str,
                worker_date: str, worker_sig_hex: str, worker_authority_hex: str,
                trusted_authorities: list) -> bool:
    """Full seal check for one record, in order:
      1. the author signed this exact content (author_pubkey is bound in the
         payload, so this also names the author),
      2. the record is included in its batch (Merkle proof → batch_root),
      3. that batch root was timestamped by a TRUSTED authority at worker_date.
    Together they establish the priority claim: 'author_pubkey analyzed this
    content, sealed no later than worker_date.' A re-signer of the same public
    content necessarily lands in a different batch with a later stamp — the
    seal is what a verifier checks to know who was first."""
    return bool(
        worker_authority_hex in trusted_authorities
        and verify(payload, signature_hex, author_pubkey_hex)
        and verify_proof(record_leaf(signature_hex), merkle_proof, batch_root_hex)
        and verify_timestamp(batch_root_hex, worker_date, worker_sig_hex,
                             worker_authority_hex))


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
