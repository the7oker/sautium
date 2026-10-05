"""Media tools from upstream's zip (desktop/db_init._download_tool).

Every backend start installs a missing tool, and the next start only asks
whether the file exists — so an installed binary must be a whole one, and
executable: zipfile drops the archive's mode bits. The archive is a local
file:// URL; the download path is the same urlopen."""

import os
import zipfile

from desktop import db_init, utils


def test_a_downloaded_tool_lands_whole_and_executable(tmp_path, monkeypatch):
    archive = tmp_path / "deno-x86_64-apple-darwin.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("deno", "#!/bin/sh\necho deno\n")
    root = tmp_path / "app"
    root.mkdir()
    monkeypatch.setattr(utils, "get_project_root", lambda: root)
    monkeypatch.setattr(db_init, "IS_WINDOWS", False)

    assert db_init._download_tool("deno", archive.as_uri())
    bin_dir = root / "deno" / "bin"
    assert os.access(bin_dir / "deno", os.X_OK)
    assert (bin_dir / "deno").read_text() == "#!/bin/sh\necho deno\n"
    assert not list(bin_dir.glob("*.part"))
    assert not (root / "_deno_download.zip").exists()
    monkeypatch.setattr(db_init, "IS_MACOS", True)
    assert db_init.media_tool_dirs()[0] == str(bin_dir)        # ahead of Homebrew's
