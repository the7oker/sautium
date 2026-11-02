"""Media tools from upstream's zip (desktop/db_init._download_tool).

Every backend start installs a missing tool, and the next start only asks
whether the file exists — so an installed binary must be a whole one, and
executable: zipfile drops the archive's mode bits. The archive is a local
file:// URL, pinned with its digest as desktop/downloads pins upstream's;
the download path is the same urlopen."""

import hashlib
import os
import zipfile

from desktop import db_init, downloads, utils


def _pin(monkeypatch, name, archive, digest=None):
    monkeypatch.setitem(downloads.ARTIFACTS, name, downloads.Artifact(
        archive.as_uri(), digest or hashlib.sha256(archive.read_bytes()).hexdigest()))


def test_a_downloaded_tool_lands_whole_and_executable(tmp_path, monkeypatch):
    archive = tmp_path / "deno-x86_64-apple-darwin.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("deno", "#!/bin/sh\necho deno\n")
    root = tmp_path / "app"
    root.mkdir()
    monkeypatch.setattr(utils, "get_project_root", lambda: root)
    monkeypatch.setattr(db_init, "IS_WINDOWS", False)

    _pin(monkeypatch, "deno-test", archive)
    assert db_init._download_tool("deno", "deno-test")
    bin_dir = root / "deno" / "bin"
    assert os.access(bin_dir / "deno", os.X_OK)
    assert (bin_dir / "deno").read_text() == "#!/bin/sh\necho deno\n"
    assert not list(bin_dir.glob("*.part"))
    assert not (root / "_deno_download.zip").exists()
    monkeypatch.setattr(db_init, "IS_MACOS", True)
    assert db_init.media_tool_dirs()[0] == str(bin_dir)        # ahead of Homebrew's


def test_an_archive_that_is_not_the_pinned_file_is_never_opened(tmp_path, monkeypatch):
    archive = tmp_path / "deno-x86_64-apple-darwin.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("deno", "#!/bin/sh\necho someone else\n")
    root = tmp_path / "app"
    root.mkdir()
    monkeypatch.setattr(utils, "get_project_root", lambda: root)
    monkeypatch.setattr(db_init, "IS_WINDOWS", False)
    _pin(monkeypatch, "deno-test", archive, digest="0" * 64)

    assert not db_init._download_tool("deno", "deno-test")
    assert not (root / "deno" / "bin" / "deno").exists()
    assert sorted(p.name for p in root.rglob("*") if p.is_file()) == []
