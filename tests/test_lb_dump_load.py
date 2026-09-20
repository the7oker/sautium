"""backend/lb_dump_load — the pure half: the mirror listing (no LATEST file,
the newest COMPLETE export wins), the fail-closed checksum, the JSONL →
COPY reader (unmapped items dropped, credits normalised) and the member
short-circuit over a synthetic archive."""

import io
import json
import os
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
L = pytest.importorskip("lb_dump_load")

LISTING = """<html><body><pre><a href="../">../</a>
<a href="listenbrainz-dump-2647-20260901-000002-full/">listenbrainz-dump-2647-20260901-000002-full/</a>       03-Sep-2026 13:02       -
<a href="listenbrainz-dump-2663-20260915-000002-full/">listenbrainz-dump-2663-20260915-000002-full/</a>       17-Sep-2026 16:59       -
</pre></body></html>"""

COMPLETE = """<a href="listenbrainz-listens-dump-2663-20260915-000002-full.tar.zst">l</a>
<a href="listenbrainz-statistics-dump-20260915-000002.tar.zst">s</a>
<a href="listenbrainz-statistics-dump-20260915-000002.tar.zst.md5">m</a>
<a href="listenbrainz-statistics-dump-20260915-000002.tar.zst.sha256">h</a>"""


def test_listing_orders_exports_newest_first_by_dump_id():
    assert L.parse_listing(LISTING) == [
        (2663, "listenbrainz-dump-2663-20260915-000002-full"),
        (2647, "listenbrainz-dump-2647-20260901-000002-full"),
    ]
    assert L.parse_listing("<pre></pre>") == []


def test_a_directory_counts_only_with_its_archive_and_checksum():
    assert L.parse_directory(COMPLETE) == (
        "20260915-000002", "listenbrainz-statistics-dump-20260915-000002.tar.zst")
    assert L.parse_directory(COMPLETE.replace(".tar.zst.sha256", ".tar.zst.sha256.tmp")) is None
    assert L.parse_directory("<a href=\"listenbrainz-listens-dump-1-2-full.tar.zst\">l</a>") is None


def test_recording_rows_drop_unmapped_items_and_normalise_credits():
    assert L.recording_row({"recording_mbid": None, "listen_count": 9}) is None
    assert L.recording_row({"recording_mbid": "bad", "listen_count": 9}) is None
    assert L.recording_row({"recording_mbid": "11111111-1111-4111-8111-111111111111",
                            "listen_count": 0}) is None
    row = L.recording_row({"recording_mbid": "8A5F0F63-0D3B-4C4E-9B1D-1B2C3D4E5F60",
                           "listen_count": "3",
                           "artist_mbids": ["11111111-1111-4111-8111-111111111111", "nope", None]})
    assert row == (b"8a5f0f63-0d3b-4c4e-9b1d-1b2c3d4e5f60\t3\t"
                   b"{11111111-1111-4111-8111-111111111111}\n")
    assert L.recording_row({"recording_mbid": "8a5f0f63-0d3b-4c4e-9b1d-1b2c3d4e5f61",
                            "listen_count": 2, "artist_mbids": None}).endswith(b"\t{}\n")


def test_artist_rows():
    assert L.artist_row({"artist_mbid": "11111111-1111-4111-8111-111111111111", "listen_count": 5}) \
        == b"11111111-1111-4111-8111-111111111111\t5\n"
    assert L.artist_row({"artist_mbid": None, "listen_count": 5}) is None
    assert L.artist_row({"artist_mbid": "11111111-1111-4111-8111-111111111111", "listen_count": "x"}) is None


def _jsonl(docs):
    return b"".join(json.dumps(d).encode() + b"\n" for d in docs)


def test_item_rows_read_one_document_per_line_and_end_on_row_boundaries():
    docs = [
        {"user_id": 1, "data": [
            {"recording_mbid": "8a5f0f63-0d3b-4c4e-9b1d-1b2c3d4e5f60", "listen_count": 3,
             "artist_mbids": ["11111111-1111-4111-8111-111111111111"]},
            {"recording_mbid": None, "listen_count": 9},
            {"recording_mbid": "8a5f0f63-0d3b-4c4e-9b1d-1b2c3d4e5f61", "listen_count": 2}]},
        {"user_id": 2, "data": []},
        {"user_id": 3},
    ]
    rows = L.ItemRows(io.BytesIO(_jsonl(docs)), L.recording_row)
    out = b""
    while True:
        chunk = rows.read(16)
        if not chunk:
            break
        assert chunk.endswith(b"\n")
        out += chunk
    assert out.count(b"\n") == 2
    assert (rows.docs, rows.seen, rows.kept) == (3, 3, 2)


def test_checksum_verification_fails_closed(tmp_path):
    from dump_common import sha256_file, verify_sha256
    p = tmp_path / "a.bin"
    p.write_bytes(b"listenbrainz")
    digest = sha256_file(str(p))
    verify_sha256(str(p), digest.upper())
    with pytest.raises(RuntimeError):
        verify_sha256(str(p), "0" * 64)


def _archive(tmp_path, members):
    """A statistics archive shaped like the dump tool's: lbdump/statistics/
    <stat>_<range>.jsonl in the tool's order, one zstd stream."""
    zstandard = pytest.importorskip("zstandard")
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, payload in members:
            info = tarfile.TarInfo(f"listenbrainz-statistics-dump-x/lbdump/statistics/{name}")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    path = tmp_path / "listenbrainz-statistics-dump-20260915-000002.tar.zst"
    path.write_bytes(zstandard.ZstdCompressor().compress(raw.getvalue()))
    return path


def test_stage_reads_the_two_members_and_stops_after_recordings(tmp_path, monkeypatch):
    """The DB is stubbed with a recording cursor: what matters here is which
    members get COPYed and that reading stops once recordings_all_time is
    in — everything after it is never asked for."""
    artists = _jsonl([{"user_id": 1, "data": [
        {"artist_mbid": "11111111-1111-4111-8111-111111111111", "listen_count": 7}]}])
    recordings = _jsonl([{"user_id": 1, "data": [
        {"recording_mbid": "8a5f0f63-0d3b-4c4e-9b1d-1b2c3d4e5f60", "listen_count": 3,
         "artist_mbids": ["11111111-1111-4111-8111-111111111111"]}]}])
    poison = b'{"user_id": 1, "data": [{"release_mbid": "x"}]}\n'
    path = _archive(tmp_path, [
        ("artists_this_week.jsonl", artists), ("artists_all_time.jsonl", artists),
        ("recordings_this_week.jsonl", recordings), ("recordings_all_time.jsonl", recordings),
        ("releases_all_time.jsonl", poison), ("daily_activity_all_time.jsonl", poison),
    ])

    copied = {}
    executed = []

    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, *a): executed.append(sql)
        def copy_expert(self, sql, fh):
            copied[sql] = fh.read(-1)

    class Conn:
        def cursor(self): return Cur()

    kept = L._stage(Conn(), str(path), lambda u: None)
    assert kept == {"artists_all_time.jsonl": 1, "recordings_all_time.jsonl": 1}
    assert copied["COPY lb_stage_artist FROM STDIN"] == b"11111111-1111-4111-8111-111111111111\t7\n"
    assert copied["COPY lb_stage_recording FROM STDIN"].startswith(b"8a5f0f63-0d3b-4c4e-9b1d-1b2c3d4e5f60\t3\t")
    assert any("CREATE UNLOGGED TABLE lb_stage_recording" in e for e in executed)


def test_stage_fails_loudly_when_a_member_is_missing(tmp_path):
    path = _archive(tmp_path, [("artists_all_time.jsonl", b"")])

    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a): pass
        def copy_expert(self, sql, fh): fh.read(-1)

    class Conn:
        def cursor(self): return Cur()

    with pytest.raises(RuntimeError, match="recordings_all_time"):
        L._stage(Conn(), str(path), lambda u: None)


def test_disk_budget_shape():
    b = L.disk_budget()
    assert set(b) == {"download_gb", "required_gb", "free_gb", "can_fit"}
    assert b["required_gb"] >= L.STAGING_GB + L.TABLES_GB + L.MARGIN_GB
