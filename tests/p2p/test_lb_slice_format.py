"""The per-artist ListenBrainz slice blob (desktop/p2p/lb_slice_queries):
canonical bytes, the receipt context, and what the receiving side rejects —
another artist's blob, a flipped byte, a foreign protocol version, and a
version older than the requester's floor."""

import base64
import gzip
import hashlib
import json

import pytest

from desktop.p2p import lb_slice_queries as q

ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")

KEY = ed25519.Ed25519PrivateKey.generate()
PUB = KEY.public_key().public_bytes_raw().hex()

ARTIST = "0b0c25f4-f31c-46a5-a4fb-ccbf53d663bd"
OTHER = "8e4f0f9a-1c0b-4b1e-9b0a-0b0c25f4f31c"
REC_A = "11111111-1111-4111-8111-111111111111"
REC_B = "22222222-2222-4222-8222-222222222222"
VERSION = "20260915-000002"


def _slice(version=VERSION, recordings=None):
    return {
        "dump_version": version,
        "artist": [1200, 340],
        "recordings": recordings if recordings is not None else [
            [REC_A, 900, 250, [ARTIST]],
            [REC_B, 300, 90, [ARTIST, OTHER]],
        ],
        "truncated": False,
    }


def _entry(mbid, one, context=q.RECEIPT_CONTEXT):
    blob = q.slice_blob(mbid, one)
    sig = KEY.sign(context + hashlib.sha256(blob).digest()).hex()
    return {"dump_version": one["dump_version"], "author_pubkey": PUB, "sig": sig,
            "blob_gz": base64.b64encode(gzip.compress(blob)).decode("ascii")}


def test_the_family_is_separate_from_mb_slices():
    assert q.PROTOCOL_VERSION == 1
    assert q.RECEIPT_CONTEXT == b"sautium-lb-slice-v1:"
    assert q.LB_LOAD_LOCK_KEY != 0x6D626C64      # never the MB loader's lock
    assert q.DB_VERSION_KEY == "listenbrainz.db_version"


def test_mbid_key_canonicalises_and_rejects_non_uuids():
    assert q.mbid_key(" 0B0C25F4-F31C-46A5-A4FB-CCBF53D663BD ") == ARTIST
    assert q.mbid_key("not-a-uuid") is None
    assert q.mbid_key(None) is None
    assert q.mbid_key(42) is None


def test_a_blob_round_trips_with_its_version_inside():
    out = q.verify_slice_entry(ARTIST, _entry(ARTIST, _slice()))
    assert out is not None
    core, blob_gz = out
    assert core["v"] == 1
    assert core["artist_mbid"] == ARTIST
    assert core["dump_version"] == VERSION
    assert core["artist"] == [1200, 340]
    assert [r[0] for r in core["recordings"]] == [REC_A, REC_B]
    assert gzip.decompress(blob_gz) == q.slice_blob(ARTIST, _slice())


def test_a_blob_served_under_another_artist_is_rejected():
    assert q.verify_slice_entry(OTHER, _entry(ARTIST, _slice())) is None


def test_a_flipped_byte_is_rejected():
    entry = _entry(ARTIST, _slice())
    raw = bytearray(base64.b64decode(entry["blob_gz"]))
    raw[-1] ^= 0x01
    entry["blob_gz"] = base64.b64encode(bytes(raw)).decode("ascii")
    assert q.verify_slice_entry(ARTIST, entry) is None


def test_a_foreign_context_does_not_verify():
    entry = _entry(ARTIST, _slice(), context=b"sautium-mb-slice-v3:")
    assert q.verify_slice_entry(ARTIST, entry) is None


def test_a_version_below_the_floor_is_rejected():
    entry = _entry(ARTIST, _slice(version="20260901-000002"))
    assert q.verify_slice_entry(ARTIST, entry) is not None
    assert q.verify_slice_entry(ARTIST, entry, min_version="20260915-000002") is None
    assert q.verify_slice_entry(ARTIST, entry, min_version="20260901-000002") is not None


def test_a_signed_zero_match_verifies():
    one = {"dump_version": VERSION, "artist": None, "recordings": [], "truncated": False}
    core, _ = q.verify_slice_entry(ARTIST, _entry(ARTIST, one))
    assert core["recordings"] == [] and core["artist"] is None


def test_row_order_never_reaches_the_signature():
    a = _slice(recordings=[[REC_A, 900, 250, [ARTIST]], [REC_B, 300, 90, [OTHER, ARTIST]]])
    b = _slice(recordings=[[REC_B, 300, 90, [OTHER, ARTIST]], [REC_A, 900, 250, [ARTIST]]])
    assert q.slice_blob(ARTIST, a) == q.slice_blob(ARTIST, b)


def test_malformed_wire_entries_are_rejections_not_exceptions():
    assert q.verify_slice_entry(ARTIST, {}) is None
    assert q.verify_slice_entry(ARTIST, {"blob_gz": "%%%", "sig": "zz", "author_pubkey": "zz"}) is None
    good = _entry(ARTIST, _slice())
    assert q.verify_slice_entry(ARTIST, {**good, "author_pubkey": "ab" * 32}) is None
    assert q.verify_slice_entry(ARTIST, {**good, "blob_gz": base64.b64encode(
        gzip.compress(json.dumps([1, 2]).encode())).decode()}) is None
