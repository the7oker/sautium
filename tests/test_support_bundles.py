"""How the support desk unpacks a bundle a node sent (routers/support.py
_unpacked_tar, _bounded_headers, _safe_extract): a tar stream of so many
bytes whose headers are walked before tar reads one — only those of the kinds
a bundle holds — so many entries and so many bytes in them, judged before a
byte is written, and no mode bit beyond plain permissions."""

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


def _refused_before_tar_reads_it(blob: bytes, dest: Path, monkeypatch) -> None:
    opened = []
    tar_open = support.tarfile.open
    monkeypatch.setattr(support.tarfile, "open", lambda *a, **k: opened.append(1) or tar_open(*a, **k))
    with pytest.raises(HTTPException) as refused:
        _extract(blob, dest)
    assert refused.value.status_code == 422 and not opened
    assert list(dest.iterdir()) == []


def _with(kind: str) -> bytes:
    """A bundle whose first entry is a header of `kind`, then a plain file."""
    buf = io.BytesIO()
    form = tarfile.GNU_FORMAT if kind in ("long-name", "long-link", "sparse") else tarfile.PAX_FORMAT
    extra = {"pax_headers": {"comment": "a"}} if kind == "global" else {}
    with tarfile.open(fileobj=buf, mode="w:gz", format=form, **extra) as tar:
        info, data = tarfile.TarInfo("logs/a.log"), b"x"
        if kind in ("long-name", "extended"):
            info.name = "logs/" + "n" * 200 + ".log"
        elif kind in ("long-link", "link"):
            info.type, info.linkname, data = tarfile.SYMTYPE, "t" * (200 if kind == "long-link" else 1), b""
        elif kind == "solaris":
            info.type, data = tarfile.SOLARIS_XHDTYPE, b"13 comment=a\n"
        elif kind == "sparse":
            info.type, data = tarfile.GNUTYPE_SPARSE, b""
        elif kind == "folder-with-data":
            info.type, data = tarfile.DIRTYPE, b"x" * 10
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
        plain = tarfile.TarInfo("manifest.json")
        plain.size = 2
        tar.addfile(plain, io.BytesIO(b"{}"))
    return buf.getvalue()


@pytest.mark.parametrize("kind", ["long-name", "extended", "global", "long-link", "solaris", "sparse",
                                  "link", "folder-with-data"])
def test_a_header_of_a_kind_a_bundle_does_not_hold_is_refused_before_tar_reads_one(tmp_path, monkeypatch, kind):
    _refused_before_tar_reads_it(_with(kind), tmp_path, monkeypatch)


def _tar(*entries) -> bytearray:
    """A plain tar stream of (name, bytes) files."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return bytearray(buf.getvalue())


def _resealed(raw: bytearray, at: int) -> bytearray:
    """The header at `at` with its checksum made to hold again."""
    raw[at + 148:at + 156] = b" " * 8
    raw[at + 148:at + 155] = b"%06o\0" % tarfile.calc_chksums(bytes(raw[at:at + 512]))[0]
    return raw


def test_a_header_that_does_not_read_is_refused_before_tar_reads_one(tmp_path, monkeypatch):
    import gzip
    raw = _tar(("a.log", b"x"), ("b.log", b"y"))
    raw[1024 + 148:1024 + 156] = b"0000000\0"                  # the second header's checksum
    _refused_before_tar_reads_it(gzip.compress(bytes(raw)), tmp_path, monkeypatch)


def test_an_entry_running_past_the_archives_end_is_refused_before_tar_reads_one(tmp_path, monkeypatch):
    import gzip
    raw = _tar(("a.log", b"x" * 2048))
    _refused_before_tar_reads_it(gzip.compress(bytes(raw[:1536])), tmp_path, monkeypatch)


def test_a_size_that_is_no_size_is_refused_before_tar_reads_one(tmp_path, monkeypatch):
    import gzip
    raw = _tar(("a.log", b"x"))
    raw[124:136] = b"\xff" * 12                                  # base-256: below zero
    _refused_before_tar_reads_it(gzip.compress(bytes(_resealed(raw, 0))), tmp_path, monkeypatch)


def test_what_is_no_tar_archive_is_refused(tmp_path):
    import gzip
    with pytest.raises(HTTPException) as refused:
        _extract(gzip.compress(b"not a tar archive"), tmp_path)
    assert refused.value.status_code == 422
