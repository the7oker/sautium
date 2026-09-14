"""The share export file (backend/share.py): JSON lines under a signed
digest. Round trip, and every defect that must be refused before anything
is applied — a cut file, an edited byte, a foreign signature, a stranger's
format or identity rule. The database half runs as the two-node acceptance
(Docker export → launcher import)."""

import gzip
import io
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

share = pytest.importorskip("share")
ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")

KEY = ed25519.Ed25519PrivateKey.generate()
PUB = KEY.public_key().public_bytes_raw().hex()
HEADER = {"exporter": {"pubkey": PUB, "username": "vale", "created_at": "2026-09-14T00:00:00Z"},
          "scope": {"kind": "owned"}}
ROWS = [{"id": f"{i:032x}", "name": f"artist {i}"} for i in range(1234)]
ENVELOPE = {"category": "artist_bios", "items": [{"artist_uuid": "a", "source": "lastfm"}],
            "batches": {"root": {"worker_date": "2026-09-14T00:00:00Z"}}}
SUMMARY = {"albums": 3, "tracks": 30, "artists": 5, "analysed_tracks": 12, "items": {"artist_bios": 1}}


def _write(path, sign=KEY.sign, header=HEADER, tail=True):
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as fp:
        w = share.ExportWriter(fp, header, sign)
        w.section("batches", {"root": {"author_pubkey": PUB}})
        w.section("artists", ROWS)
        w.section("tracks", [])
        w.envelope("enrichment", "artist_bios", ENVELOPE)
        if tail:
            w.finish(SUMMARY)
    return path


def test_round_trip_lines_and_chunking(tmp_path):
    path = _write(tmp_path / "x.jsonl.gz")
    lines = list(share.read_export(path))
    header, body, summary = lines[0], lines[1:-1], lines[-1]
    assert header["format"] == share.FORMAT and header["version"] == share.VERSION
    assert header["identity_rule"] == share.IDENTITY_RULE
    assert header["exporter"]["pubkey"] == PUB and header["scope"] == {"kind": "owned"}
    sections = [(o["section"], len(o["rows"])) for o in body if "section" in o]
    assert sections == [("batches", 1), ("artists", 500), ("artists", 500), ("artists", 234), ("tracks", 0)]
    assert [r for o in body if o.get("section") == "artists" for r in o["rows"]] == ROWS
    env = [o for o in body if "envelope" in o]
    assert env == [{"envelope": "artist_bios", "group": "enrichment", "data": ENVELOPE}]
    assert summary == {"summary": SUMMARY}
    info = share.verify_export(path)
    assert info["summary"] == SUMMARY and info["header"]["exporter"]["username"] == "vale"


def _rewrite(path, mutate):
    raw = gzip.open(path, "rb").read()
    with gzip.open(path, "wb") as fp:
        fp.write(mutate(raw))


def test_truncated_file_is_refused(tmp_path):
    path = _write(tmp_path / "x.jsonl.gz", tail=False)
    with pytest.raises(share.ShareError, match="truncated"):
        list(share.read_export(path))
    path2 = _write(tmp_path / "y.jsonl.gz")
    _rewrite(path2, lambda raw: raw[:raw.rindex(b'{"end"')])     # trailer cut off
    with pytest.raises(share.ShareError, match="truncated"):
        share.verify_export(path2)


def test_edited_byte_is_refused(tmp_path):
    path = _write(tmp_path / "x.jsonl.gz")

    def flip(raw):
        i = raw.index(b"artist 7")
        return raw[:i] + b"artist 8" + raw[i + 8:]
    _rewrite(path, flip)
    with pytest.raises(share.ShareError, match="edited or damaged"):
        share.verify_export(path)


def test_foreign_signature_is_refused(tmp_path):
    other = ed25519.Ed25519PrivateKey.generate()
    path = _write(tmp_path / "x.jsonl.gz", sign=other.sign)     # header still names KEY
    with pytest.raises(share.ShareError, match="signature"):
        share.verify_export(path)


def test_header_defects_are_refused(tmp_path):
    path = _write(tmp_path / "x.jsonl.gz")
    _rewrite(path, lambda raw: raw.replace(b'"format":"sautium-export"', b'"format":"something"'))
    with pytest.raises(share.ShareError, match="not a Sautium export"):
        list(share.read_export(path))
    path = _write(tmp_path / "y.jsonl.gz")
    _rewrite(path, lambda raw: raw.replace(
        f'"identity_rule":{share.IDENTITY_RULE}'.encode(), f'"identity_rule":{share.IDENTITY_RULE + 1}'.encode(), 1))
    with pytest.raises(share.ShareError, match="identity rule"):
        list(share.read_export(path))
    with pytest.raises(share.ShareError):
        list(share.read_export(_write(tmp_path / "z.jsonl.gz", header={"scope": {}})))   # no exporter key


def test_data_after_trailer_is_refused(tmp_path):
    path = _write(tmp_path / "x.jsonl.gz")
    _rewrite(path, lambda raw: raw + b'{"section":"tracks","rows":[]}\n')
    with pytest.raises(share.ShareError, match="after the trailer"):
        list(share.read_export(path))


def test_export_filename():
    from datetime import datetime, timezone
    when = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    assert share.export_filename("ab" * 32, when) == "sautium-export-abababababab-2026-09-14.jsonl.gz"
