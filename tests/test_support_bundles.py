"""How the support desk unpacks a bundle a node sent (routers/support.py
_unpacked_tar, _safe_extract): a tar stream of so many bytes, so many entries
and so many bytes in them, judged before a byte is written, and no mode bit
beyond plain permissions."""

import io
import stat
import sys
import tarfile
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "backend", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from routers import support  # noqa: E402


def _bundle(entries, form=tarfile.PAX_FORMAT) -> bytes:
    """A tar.gz of (name, bytes, mode) entries."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=form) as tar:
        for name, data, mode in entries:
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _extract(blob: bytes, dest: Path) -> None:
    """As the desk opens a bundle (support._unpack)."""
    support._unpack(blob, dest)


def test_a_bundle_unpacks_as_its_files(tmp_path):
    _extract(_bundle([("manifest.json", b"{}", 0o644), ("logs/backend.log", b"line\n", 0o644)]), tmp_path)
    assert (tmp_path / "manifest.json").read_bytes() == b"{}"
    assert (tmp_path / "logs" / "backend.log").read_bytes() == b"line\n"


def test_an_entry_keeps_no_mode_bit_beyond_plain_permissions(tmp_path):
    _extract(_bundle([("manifest.json", b"{}", 0o666)]), tmp_path)
    assert stat.S_IMODE((tmp_path / "manifest.json").stat().st_mode) == 0o644


def test_more_entries_than_a_bundle_holds_are_refused_before_a_byte_is_written(tmp_path, monkeypatch):
    monkeypatch.setattr(support, "_BUNDLE_ENTRIES", 3)
    with pytest.raises(HTTPException) as refused:
        _extract(_bundle([(f"logs/{i}.log", b"x", 0o644) for i in range(4)]), tmp_path)
    assert refused.value.status_code == 422
    assert list(tmp_path.iterdir()) == []


def test_a_bundle_that_unpacks_past_its_cap_is_refused_before_a_byte_is_written(tmp_path, monkeypatch):
    monkeypatch.setattr(support, "_BUNDLE_UNPACKED_BYTES", 10)
    with pytest.raises(HTTPException) as refused:
        _extract(_bundle([("a.log", b"x" * 8, 0o644), ("b.log", b"x" * 8, 0o644)]), tmp_path)
    assert refused.value.status_code == 422
    assert list(tmp_path.iterdir()) == []


def test_a_bundle_whose_tar_stream_passes_its_cap_is_refused_before_a_byte_is_written(tmp_path, monkeypatch):
    monkeypatch.setattr(support, "_BUNDLE_STREAM_BYTES", 8 * 1024)
    with pytest.raises(HTTPException) as refused:
        _extract(_bundle([("logs/backend.log", b"line\n" * 4096, 0o644)]), tmp_path)
    assert refused.value.status_code == 422
    assert list(tmp_path.iterdir()) == []


def test_what_is_no_gzip_stream_is_refused(tmp_path):
    with pytest.raises(HTTPException) as refused:
        _extract(b"not a bundle", tmp_path)
    assert refused.value.status_code == 422


def test_a_long_name_past_what_a_bundles_names_take_is_refused_before_a_byte_is_written(tmp_path, monkeypatch):
    monkeypatch.setattr(support, "_HEADER_DATA_BYTES", 64)
    with pytest.raises(HTTPException) as refused:
        _extract(_bundle([("logs/" + "n" * 200 + ".log", b"x", 0o644)], form=tarfile.GNU_FORMAT), tmp_path)
    assert refused.value.status_code == 422
    assert list(tmp_path.iterdir()) == []


def test_what_is_no_tar_archive_is_refused(tmp_path):
    import gzip
    with pytest.raises(HTTPException) as refused:
        _extract(gzip.compress(b"not a tar archive"), tmp_path)
    assert refused.value.status_code == 422
