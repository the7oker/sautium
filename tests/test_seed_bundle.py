"""The seed bundle's download-and-check path, with a file:// URL standing in
for the release asset: the digest gate, the landing under seed_dir, the
retry semantics of a failed fetch."""

import gzip
import hashlib
import json

import pytest

import backend.seed_import as seed_import


def _write_bundle(path, payload) -> str:
    with gzip.open(path, "wb") as gz:
        gz.write(json.dumps(payload).encode("utf-8"))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_download_checks_the_digest_and_lands_under_seed_dir(tmp_path, monkeypatch):
    src = tmp_path / "asset.json.gz"
    sha = _write_bundle(src, {"format": "sautium-seed", "version": 9})
    monkeypatch.setattr(seed_import.settings, "seed_dir", str(tmp_path / "seed"))
    info = {"version": 9, "url": src.as_uri(), "sha256": sha, "size": src.stat().st_size}

    landed = seed_import.ensure_bundle(info)

    assert landed == tmp_path / "seed" / "seed_v9.json.gz"
    assert landed.read_bytes() == src.read_bytes()
    assert seed_import._load_bundle(info)["version"] == 9
    monkeypatch.setattr(seed_import.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("a present bundle was fetched again"))
    assert seed_import.ensure_bundle(info) == landed


def test_digest_mismatch_discards_the_download(tmp_path, monkeypatch):
    src = tmp_path / "asset.json.gz"
    _write_bundle(src, {"version": 9})
    monkeypatch.setattr(seed_import.settings, "seed_dir", str(tmp_path / "seed"))
    info = {"version": 9, "url": src.as_uri(), "sha256": "0" * 64, "size": 1}

    assert seed_import.ensure_bundle(info) is None
    assert list((tmp_path / "seed").iterdir()) == []


def test_unreachable_url_is_a_retry_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(seed_import.settings, "seed_dir", str(tmp_path / "seed"))
    info = {"version": 9, "url": (tmp_path / "missing.json.gz").as_uri(),
            "sha256": "0" * 64, "size": 1}

    assert seed_import.ensure_bundle(info) is None
    assert list((tmp_path / "seed").iterdir()) == []
