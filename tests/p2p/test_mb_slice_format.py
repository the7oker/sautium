"""The per-name slice blob (desktop/p2p/mb_slice_queries): v3 carries the
artist's Bandcamp url subtree, and the receipt context changed with it — a
v2 blob is a signed statement WITHOUT those tables, so it must not verify.
The v4 protocol keeps v3's signed bytes and carries the author's birth
certificate beside them: a verified entry comes back with it."""

import base64
import gzip
import hashlib
import json

import pytest

from desktop.p2p import mb_slice_queries as q
from tests.slice_certs import certificate, certified, trust

ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")

KEY = ed25519.Ed25519PrivateKey.generate()
PUB = KEY.public_key().public_bytes_raw().hex()
CERT = certificate(PUB)


@pytest.fixture(autouse=True)
def _authority(monkeypatch):
    trust(monkeypatch)


URL_TABLES = ("mb_url", "mb_l_artist_url", "mb_l_release_url")
LINK_COLUMNS = ["id", "link", "entity0", "entity1", "edits_pending",
                "last_updated", "link_order", "entity0_credit", "entity1_credit"]
PAGE = "https://jonhopkins.bandcamp.com/album/immunity"


def _slice(name="Jon Hopkins"):
    return {
        "dump_version": "20260916-002105",
        "artists_matched": {name: [1]},
        "truncated": [],
        "tables": {
            "mb_artist": [[1, "0b0c25f4-f31c-46a5-a4fb-ccbf53d663bd", name, name]
                          + [None] * 15],
            "mb_url": [[10, "8e4f0f9a-1c0b-4b1e-9b0a-0b0c25f4f31c", PAGE, 0, None]],
            "mb_l_release_url": [[100, 1, 5, 10, 0, None, 0, "", ""]],
            "mb_l_artist_url": [[200, 2, 1, 10, 0, None, 0, "", ""]],
        },
    }


def _entry(name, one, context=q.RECEIPT_CONTEXT):
    blob = q.name_blob(name, one)
    sig = KEY.sign(context + hashlib.sha256(blob).digest()).hex()
    return certified({"dump_version": one["dump_version"], "author_pubkey": PUB, "sig": sig,
                      "blob_gz": base64.b64encode(gzip.compress(blob)).decode("ascii")}, CERT)


def test_wire_format_names_the_url_tables_in_dump_column_order():
    assert q.SLICE_TABLES["mb_url"] == ["id", "gid", "url", "edits_pending", "last_updated"]
    assert q.SLICE_TABLES["mb_l_artist_url"] == LINK_COLUMNS
    assert q.SLICE_TABLES["mb_l_release_url"] == LINK_COLUMNS
    assert q.PROTOCOL_VERSION == 4
    assert q.RECEIPT_CONTEXT == b"sautium-mb-slice-v3:"          # v4 signs v3's bytes


def test_v3_blob_round_trips_with_its_url_subtree():
    out = q.verify_slice_entry("Jon Hopkins", _entry("Jon Hopkins", _slice()))
    assert out is not None
    core, blob_gz, cert, proof = out
    assert (cert, proof) == (CERT, None)
    assert core["name_key"] == "jon hopkins"
    assert core["dump_version"] == "20260916-002105"
    for t in URL_TABLES:
        assert t in core["tables"], t
    assert json.loads(core["tables"]["mb_url"][0])[2] == PAGE
    assert gzip.decompress(blob_gz) == q.name_blob("Jon Hopkins", _slice())


def test_a_v2_signature_no_longer_verifies():
    entry = _entry("Jon Hopkins", _slice(), context=b"sautium-mb-slice-v2:")
    assert q.verify_slice_entry("Jon Hopkins", entry) is None


def test_a_blob_served_under_another_name_is_rejected():
    assert q.verify_slice_entry("Four Tet", _entry("Jon Hopkins", _slice())) is None


def test_a_flipped_byte_is_rejected():
    entry = _entry("Jon Hopkins", _slice())
    raw = bytearray(base64.b64decode(entry["blob_gz"]))
    raw[-1] ^= 0x01
    entry["blob_gz"] = base64.b64encode(bytes(raw)).decode("ascii")
    assert q.verify_slice_entry("Jon Hopkins", entry) is None


def test_row_order_never_reaches_the_signature():
    second = [11, "0a4b9f3e-2c6d-4e8f-8a1b-3c5d7e9f0a1b",
              "https://jonhopkins.bandcamp.com/album/immunity-2", 0, None]
    a, b = _slice(), _slice()
    a["tables"]["mb_url"] = a["tables"]["mb_url"] + [second]
    b["tables"]["mb_url"] = [second] + b["tables"]["mb_url"]
    assert q.name_blob("Jon Hopkins", a) == q.name_blob("Jon Hopkins", b)


def _core_with(table, row):
    """A slice core carrying one `table` row — its columns from `row`, None
    elsewhere."""
    cols = q.SLICE_TABLES[table]
    return {"dump_version": "20261101-001500", "artists_matched": {}, "truncated": [],
            "tables": {table: [json.dumps([row.get(c) for c in cols])]}}


def test_every_cell_of_a_slice_is_of_its_columns_kind_before_any_of_it_is_used():
    """A row's shape alone let one certified author abort every cycle run
    that met its slice: an unhashable id in the subtree walk, a string id
    or a junk MBID in a cast. Such a slice is malformed and dropped whole."""
    from desktop import mb_slice_client
    gid = "6b0a4c8e-1c3d-4f5e-9a7b-0c1d2e3f4a5b"
    honest = {"id": 7, "gid": gid, "name": "Name", "sort_name": "Name", "ended": False,
              "begin_date_year": 1999, "last_updated": "2026-11-01T00:22:05.123456+00:00"}
    assert mb_slice_client._rows(_core_with("mb_artist", honest)) is not None
    for bad in ({"id": [7]}, {"id": "7"}, {"id": True}, {"id": 2 ** 31},
                {"gid": gid.upper()}, {"gid": "not-an-mbid"}, {"name": 42},
                {"name": "a\x00b"}, {"name": "\ud800"}, {"ended": 1},
                {"begin_date_year": 40000}, {"last_updated": "yesterday"}):
        assert mb_slice_client._rows(_core_with("mb_artist", {**honest, **bad})) is None, bad
    assert mb_slice_client._rows(_core_with("mb_artist_credit_name", {
        "artist_credit": 1, "position": 40000, "artist": 7, "name": "N", "join_phrase": ""})) is None
    assert mb_slice_client._rows(_core_with("mb_track", {
        "id": 1, "gid": gid, "recording": 2, "medium": 3, "position": 40000, "number": "A1"})) is not None
