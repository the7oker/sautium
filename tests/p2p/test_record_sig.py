"""Record payload grammar — the bytes an author signs and every import gate
rebuilds. Pure logic: exact bytes for fixed inputs, the fingerprint guard,
the two copies in step."""

from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import backend.record_sig as rs
from desktop.p2p import record_sig as launcher_mirror

REPO = Path(__file__).resolve().parents[2]
AUTHOR = "ab" * 32
TRACK = "551248F6-64F6-539D-A4E1-840581D55266"
MODEL = "0f3d8c1e-2a4b-5c6d-8e7f-90a1b2c3d4e5"
FINGERPRINT = "AQADtEmSJEmS_5EOzo-Q7f8Hw1-gj0EbZKLA"
VECTOR_HASH = "cd" * 32
FEATURES_HASH = "ef" * 32


def test_copies_are_byte_identical():
    backend_copy = (REPO / "backend" / "record_sig.py").read_bytes()
    launcher_copy = (REPO / "desktop" / "p2p" / "record_sig.py").read_bytes()
    assert backend_copy == launcher_copy


def test_segment_payload_v3_bytes():
    payload = rs.segment_payload(AUTHOR.upper(), TRACK, FINGERPRINT, 189, MODEL, 7,
                                 VECTOR_HASH)
    assert payload == (
        f"sautium-record:v3:segment:{AUTHOR}:{TRACK.lower()}:{FINGERPRINT}:189:"
        f"{MODEL}:{rs.GRID_VERSION}:7:{VECTOR_HASH}").encode()


def test_features_payload_v3_bytes():
    payload = rs.features_payload(AUTHOR, TRACK, FINGERPRINT, None, 2, FEATURES_HASH)
    assert payload == (
        f"sautium-record:v3:features:{AUTHOR}:{TRACK.lower()}:{FINGERPRINT}:-:2:"
        f"{FEATURES_HASH}").encode()


@pytest.mark.parametrize("bad", [None, "", "a:b", "a\\b", "with space", "fp\n"])
def test_chromaprint_is_required_and_in_alphabet(bad):
    with pytest.raises(ValueError):
        rs.segment_payload(AUTHOR, TRACK, bad, 189, MODEL, 0, VECTOR_HASH)
    with pytest.raises(ValueError):
        rs.features_payload(AUTHOR, TRACK, bad, 189, 2, FEATURES_HASH)


def test_enrichment_payload_stays_v2():
    payload = rs.enrichment_payload(AUTHOR, "track_mbid", TRACK, "",
                                    FEATURES_HASH, "2026-09-18T10:00:00Z")
    assert payload.startswith(b"sautium-record:v2:track_mbid:")
    # The Last.fm kinds left the grammar 2026-09-19 — node-local, never sealed.
    with pytest.raises(ValueError):
        rs.enrichment_payload(AUTHOR, "artist_bio", TRACK, "lastfm",
                              FEATURES_HASH, "2026-09-18T10:00:00Z")


def test_sign_verify_round_trip_across_copies():
    key = Ed25519PrivateKey.generate()
    author = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw).hex()
    payload = rs.segment_payload(author, TRACK, FINGERPRINT, 189, MODEL, 3, VECTOR_HASH)
    signature = rs.sign(payload, key)
    assert rs.verify(payload, signature, author)
    assert launcher_mirror.verify(payload, signature, author)
    assert not rs.verify(payload + b"x", signature, author)
