"""Record payload grammar — the bytes an author signs and every import gate
rebuilds. Pure logic: exact bytes for fixed inputs, one version per record
kind, the features blob of v4 and the instruments blob, the fingerprint
guard, the two copies in step."""

import hashlib
import json
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
INSTRUMENTS_HASH = "12" * 32


def test_copies_are_byte_identical():
    backend_copy = (REPO / "backend" / "record_sig.py").read_bytes()
    launcher_copy = (REPO / "desktop" / "p2p" / "record_sig.py").read_bytes()
    assert backend_copy == launcher_copy


def test_each_kind_has_a_version_of_its_own():
    assert (rs.SEGMENT_RECORD_VERSION, rs.FEATURES_RECORD_VERSION,
            rs.INSTRUMENTS_RECORD_VERSION, rs.ENRICHMENT_RECORD_VERSION,
            rs.TRACK_MBID_RECORD_VERSION, rs.MATCHER_VERSION) == (3, 4, 1, 2, 3, 2)
    assert not hasattr(rs, "AUDIO_RECORD_VERSION")


def test_segment_payload_v3_bytes():
    payload = rs.segment_payload(AUTHOR.upper(), TRACK, FINGERPRINT, 189, MODEL, 7,
                                 VECTOR_HASH)
    assert payload == (
        f"sautium-record:v3:segment:{AUTHOR}:{TRACK.lower()}:{FINGERPRINT}:189:"
        f"{MODEL}:{rs.GRID_VERSION}:7:{VECTOR_HASH}").encode()


def test_features_payload_v4_bytes():
    payload = rs.features_payload(AUTHOR, TRACK, FINGERPRINT, None, 2, FEATURES_HASH)
    assert payload == (
        f"sautium-record:v4:features:{AUTHOR}:{TRACK.lower()}:{FINGERPRINT}:-:2:"
        f"{FEATURES_HASH}").encode()


def test_instruments_payload_v1_bytes():
    payload = rs.instruments_payload(AUTHOR.upper(), TRACK, FINGERPRINT, 241, 2,
                                     INSTRUMENTS_HASH.upper())
    assert payload == (
        f"sautium-record:v1:instruments:{AUTHOR}:{TRACK.lower()}:{FINGERPRINT}:241:2:"
        f"{INSTRUMENTS_HASH}").encode()


def test_the_features_blob_is_the_nine_fields_in_their_order():
    assert rs.FEATURE_ORDER == ["bpm", "key", "mode", "key_confidence", "energy", "energy_db",
                                "brightness", "dynamic_range_db", "zero_crossing_rate"]
    row = {"bpm": 120.5, "key": "A", "mode": "minor", "key_confidence": 0.81,
           "energy": 0.25, "energy_db": -14.2, "brightness": 0.1234,
           "dynamic_range_db": 11.0, "zero_crossing_rate": 0.051234,
           # what v3 hashed besides: no part of a v4 record
           "danceability": 0.7, "moods": {"calm": 0.4}, "instruments": {"piano": 0.6}}
    assert rs.canonical_features_blob(row) == (
        b"120.5|A|minor|0.81|0.25|-14.2|0.1234|11.0|0.051234")


def test_the_instruments_blob_is_the_map_as_sorted_compact_json():
    scores = {"piano": 0.62, "drum kit": 0.45, "violin, fiddle": 0.011}
    blob = rs.canonical_instruments_blob(scores)
    assert blob == b'{"drum kit":0.45,"piano":0.62,"violin, fiddle":0.011}'
    # the JSONB round trip a stored row takes: the same bytes
    assert rs.canonical_instruments_blob(json.loads(json.dumps(scores))) == blob
    assert rs.canonical_instruments_blob({}) == b"{}"
    for not_a_map in (None, [], "piano", [["piano", 0.6]]):
        with pytest.raises(ValueError):
            rs.canonical_instruments_blob(not_a_map)


@pytest.mark.parametrize("bad", [None, "", "a:b", "a\\b", "with space", "fp\n"])
def test_chromaprint_is_required_and_in_alphabet(bad):
    with pytest.raises(ValueError):
        rs.segment_payload(AUTHOR, TRACK, bad, 189, MODEL, 0, VECTOR_HASH)
    with pytest.raises(ValueError):
        rs.features_payload(AUTHOR, TRACK, bad, 189, 2, FEATURES_HASH)
    with pytest.raises(ValueError):
        rs.instruments_payload(AUTHOR, TRACK, bad, 189, 2, INSTRUMENTS_HASH)


def test_enrichment_payload_stays_v2_and_a_binding_is_v3_with_its_methodology():
    assert rs.enrichment_payload(AUTHOR, "album", TRACK, "", FEATURES_HASH,
                                 "2026-09-18T10:00:00Z").startswith(b"sautium-record:v2:album:")
    payload = rs.enrichment_payload(AUTHOR, "track_mbid", TRACK, "",
                                    FEATURES_HASH, "2026-09-18T10:00:00Z")
    assert payload.startswith(b"sautium-record:v3:track_mbid:")
    # the matcher's methodology is inside the hashed fields: a stale binding
    # cannot pass for a newer one
    one = {"recording_mbid": TRACK, "confidence": "positional", "matcher_version": 1}
    assert rs.canonical_enrichment_blob("track_mbid", one) != rs.canonical_enrichment_blob(
        "track_mbid", {**one, "matcher_version": 2})
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


def test_a_v3_features_seal_does_not_verify_as_v4():
    """No compatibility with the old format: the bytes a v3 author signed
    are not the bytes any v4 verifier rebuilds."""
    key = Ed25519PrivateKey.generate()
    author = key.public_key().public_bytes_raw().hex()
    v4 = rs.features_payload(author, TRACK, FINGERPRINT, 189, 2, FEATURES_HASH)
    v3 = v4.replace(b":v4:features:", b":v3:features:")
    assert not rs.verify(v4, rs.sign(v3, key), author)
    assert hashlib.blake2b(v3).digest() != hashlib.blake2b(v4).digest()
