"""The yt-dlp a node runs (backend/streaming/ytdlp_release.py): yt-dlp's
latest stable release, unpacked only when its SHA2-256SUMS carries a valid
signature of the pinned release key and the archive the digest those sums
name; run with no configuration file, no plugin and none of the node's
credentials. GitHub is a fake here, and the release key a key of the test's
own, put in the pinned key's place."""

import hashlib
import io
import json
import os
import struct
import sys
import time
import zipfile
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "backend") not in sys.path:
    sys.path.insert(0, str(REPO / "backend"))

from streaming import ytdlp_release  # noqa: E402

TAG = "2026.11.01"


def _mpi(value: int) -> bytes:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return struct.pack(">H", value.bit_length()) + raw


class SigningKey:
    """An RSA key as an OpenPGP v4 key packet, and v4 signatures by it."""

    def __init__(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = self.private.public_key().public_numbers()
        self.packet = b"\x04" + struct.pack(">I", 1_700_000_000) + b"\x01" + _mpi(numbers.n) + _mpi(numbers.e)
        self.fingerprint = hashlib.sha1(
            b"\x99" + len(self.packet).to_bytes(2, "big") + self.packet).hexdigest().upper()

    def sign(self, document: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        subpackets = b"\x05\x02" + struct.pack(">I", int(time.time()))
        hashed = b"\x04\x00\x01\x0a" + struct.pack(">H", len(subpackets)) + subpackets
        data = document + hashed + b"\x04\xff" + len(hashed).to_bytes(4, "big")
        value = int.from_bytes(self.private.sign(data, padding.PKCS1v15(), hashes.SHA512()), "big")
        body = hashed + b"\x00\x00" + hashlib.sha512(data).digest()[:2] + _mpi(value)
        return b"\xc2" + bytes([192 + ((len(body) - 192) >> 8), (len(body) - 192) & 0xFF]) + body


@pytest.fixture(scope="module")
def release_key():
    return SigningKey()


@pytest.fixture
def pinned(release_key, monkeypatch):
    monkeypatch.setattr(ytdlp_release, "SIGNER_PACKET", release_key.packet)
    monkeypatch.setattr(ytdlp_release, "SIGNER_FINGERPRINT", release_key.fingerprint)
    return release_key


def test_the_pinned_release_key_is_the_one_its_fingerprint_names():
    assert ytdlp_release._release_key().key_size == 4096


def test_sums_signed_by_the_release_key_are_taken_and_nothing_else_is(pinned):
    sums = b"0" * 64 + b"  yt-dlp_linux.zip\n"
    ytdlp_release.verify_signature(sums, pinned.sign(sums))
    with pytest.raises(ytdlp_release.Refused):
        ytdlp_release.verify_signature(sums.replace(b"0", b"1", 1), pinned.sign(sums))
    with pytest.raises(ytdlp_release.Refused):
        ytdlp_release.verify_signature(sums, SigningKey().sign(sums))
    with pytest.raises(ytdlp_release.Refused):
        ytdlp_release.verify_signature(sums, b"\xc2\x02\x04\x00")


def test_the_signature_parser_reads_both_packet_formats_and_refuses_a_header_cut_short(pinned):
    sums = b"0" * 64 + b"  yt-dlp_linux.zip\n"
    body = pinned.sign(sums)[3:]
    old_format = bytes([0x80 | (2 << 2) | 1]) + len(body).to_bytes(2, "big") + body
    ytdlp_release.verify_signature(sums, old_format)
    for cut in (b"\xc2", b"\xc2\xc0", b"\xc2\xff\x00\x00", b"\x89", b"\x89\x01", b"\x8a\x00\x00"):
        with pytest.raises(ytdlp_release.Refused):
            ytdlp_release.verify_signature(sums, cut)


# -- a pass against a fake GitHub -------------------------------------------------------

def _build(version: str, extra: dict = None) -> bytes:
    """A release archive: the executable for this platform and a folder."""
    name, exe = ytdlp_release._asset()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        script = zipfile.ZipInfo(exe)
        script.external_attr = (0o100755 << 16)
        zf.writestr(script, f"#!/bin/sh\necho {version}\n")
        zf.writestr("_internal/yt_dlp_ejs/core.min.js", "// solver")
        for member, data in (extra or {}).items():
            zf.writestr(member, data)
    return out.getvalue()


class FakeGitHub:
    def __init__(self, key):
        self.key, self.releases, self.asked, self.answer = key, {}, [], None

    def publish(self, tag, archive, sums_digest=None, signer=None):
        name, _ = ytdlp_release._asset()
        sums = f"{sums_digest or hashlib.sha256(archive).hexdigest()}  {name}\n".encode()
        self.releases[tag] = {name: archive, "SHA2-256SUMS": sums,
                              "SHA2-256SUMS.sig": (signer or self.key).sign(sums)}
        self.latest = tag

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(request.url.path)
        if request.url.path == "/repos/yt-dlp/yt-dlp/releases/latest":
            return httpx.Response(200, json=self.answer if self.answer is not None
                                  else {"tag_name": self.latest})
        _, tag, name = request.url.path.rsplit("/", 2)
        return httpx.Response(200, content=self.releases[tag][name])


@pytest.fixture
def github(pinned, tmp_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("the fake build is a shell script")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    fake = FakeGitHub(pinned)
    monkeypatch.setattr(ytdlp_release, "_client", lambda: httpx.Client(
        transport=httpx.MockTransport(fake.handler), follow_redirects=True))
    return fake


def test_a_pass_puts_the_signed_release_in_place_and_runs_it(github):
    github.publish(TAG, _build(TAG))
    assert ytdlp_release.refresh() == TAG
    exe = ytdlp_release.executable()
    assert exe == ytdlp_release.root() / TAG / ytdlp_release._asset()[1]
    assert os.access(exe, os.X_OK)
    github.asked.clear()
    assert ytdlp_release.refresh() == TAG
    assert github.asked == ["/repos/yt-dlp/yt-dlp/releases/latest"]     # nothing fetched again


def test_an_archive_its_signed_sums_do_not_name_is_never_unpacked(github):
    github.publish(TAG, _build(TAG))
    ytdlp_release.refresh()
    github.publish("2026.11.02", _build("2026.11.02"), sums_digest="0" * 64)
    assert ytdlp_release.refresh() == TAG
    assert sorted(p.name for p in ytdlp_release.root().iterdir()) == [TAG, "current"]


def test_sums_another_key_signed_leave_the_build_in_place(github):
    github.publish(TAG, _build(TAG))
    ytdlp_release.refresh()
    github.publish("2026.11.02", _build("2026.11.02"), signer=SigningKey())
    assert ytdlp_release.refresh() == TAG
    assert sorted(p.name for p in ytdlp_release.root().iterdir()) == [TAG, "current"]
    name, _ = ytdlp_release._asset()
    assert not [p for p in github.asked if p.endswith(f"/2026.11.02/{name}")]   # never fetched


def test_a_release_older_than_the_build_in_place_is_refused(github):
    github.publish("2026.11.02", _build("2026.11.02"))
    ytdlp_release.refresh()
    github.publish(TAG, _build(TAG))                    # validly signed, and older
    assert ytdlp_release.refresh() == "2026.11.02"
    assert sorted(p.name for p in ytdlp_release.root().iterdir()) == ["2026.11.02", "current"]


def test_a_build_that_does_not_name_its_own_release_never_replaces_the_one_in_place(github):
    github.publish(TAG, _build(TAG))
    ytdlp_release.refresh()
    github.publish("2026.11.02", _build("2026.10.01"))
    assert ytdlp_release.refresh() == TAG
    assert sorted(p.name for p in ytdlp_release.root().iterdir()) == [TAG, "current"]


def test_an_answer_that_names_no_release_leaves_the_build_in_place(github):
    github.publish(TAG, _build(TAG))
    ytdlp_release.refresh()
    for answer in ([{"tag_name": "2026.11.02"}], {"tag_name": 20261102}, {}):
        github.answer = answer
        assert ytdlp_release.refresh() == TAG


def test_a_release_whose_name_is_not_a_date_is_refused(github):
    github.publish("../x", _build("x"))
    assert ytdlp_release.refresh() is None
    assert not ytdlp_release.root().exists() or list(ytdlp_release.root().iterdir()) == []


def test_an_archive_that_names_a_path_outside_its_folder_is_refused(github, tmp_path):
    github.publish(TAG, _build(TAG, {"../../outside.txt": "x"}))
    assert ytdlp_release.refresh() is None
    assert not (tmp_path / "outside.txt").exists() and not (tmp_path / "cache" / "outside.txt").exists()
    assert ytdlp_release.executable() is None


def test_a_newer_release_replaces_the_build_and_keeps_the_one_before(github):
    github.publish(TAG, _build(TAG))
    ytdlp_release.refresh()
    github.publish("2026.11.02", _build("2026.11.02"))
    assert ytdlp_release.refresh() == "2026.11.02"
    github.publish("2026.11.03", _build("2026.11.03"))
    assert ytdlp_release.refresh() == "2026.11.03"
    assert sorted(p.name for p in ytdlp_release.root().iterdir()) == ["2026.11.02", "2026.11.03",
                                                                       "current"]


# -- how the provider runs it -----------------------------------------------------------------

def test_a_yt_dlp_child_carries_none_of_the_nodes_credentials(monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "pg")
    monkeypatch.setenv("P2P_PASSWORD", "p2p")
    monkeypatch.setenv("LASTFM_SESSION_KEY", "s")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    env = ytdlp_release.child_env()
    assert not {"POSTGRES_PASSWORD", "P2P_PASSWORD", "LASTFM_SESSION_KEY", "ANTHROPIC_API_KEY"} & set(env)
    assert env["PATH"] == os.environ["PATH"]


def test_the_provider_runs_the_build_in_place_reading_no_config_and_no_plugin(github, monkeypatch):
    from streaming import youtube
    github.publish(TAG, _build(TAG))
    ytdlp_release.refresh()
    seen = []
    monkeypatch.setattr(youtube.subprocess, "run", lambda cmd, **kw: seen.append((cmd, kw)) or
                        type("Done", (), {"stdout": ""})())
    youtube.YouTubeProvider()._flat_search("an artist a title")
    cmd, kw = seen[0]
    assert cmd[:5] == [str(ytdlp_release.executable()), "--ignore-config", "--no-plugin-dirs",
                       "--encoding", "utf-8"]
    assert kw["env"] == ytdlp_release.child_env()
    assert (kw["encoding"], kw["errors"]) == ("utf-8", "replace")


def test_a_build_in_place_that_does_not_start_makes_the_provider_unavailable(github):
    from streaming import youtube
    from streaming.base import ProviderUnavailable
    github.publish(TAG, _build(TAG))
    ytdlp_release.refresh()
    ytdlp_release.executable().write_bytes(b"\x00\x01 not a program")
    with pytest.raises(ProviderUnavailable):
        youtube.YouTubeProvider()._flat_search("an artist a title")


def test_before_the_first_build_lands_the_provider_is_unavailable(tmp_path, monkeypatch):
    from streaming import youtube
    from streaming.base import ProviderUnavailable
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    with pytest.raises(ProviderUnavailable):
        youtube.YouTubeProvider()._flat_search("an artist a title")


def test_yt_dlp_is_not_part_of_the_backends_interpreter_or_image():
    lines = [line.split("#", 1)[0] for line in
             (REPO / "backend" / "requirements.txt").read_text(encoding="utf-8").splitlines()]
    assert not [line for line in lines if "yt-dlp" in line or "yt_dlp" in line]
    for name in ("Dockerfile", "Dockerfile.mac"):
        assert "yt-dlp[" not in (REPO / "backend" / name).read_text(encoding="utf-8")


# -- when a pass runs (streaming/service.py) -----------------------------------------------

def _loop(monkeypatch, supported=True):
    import asyncio
    from streaming import service
    passes = []
    monkeypatch.setattr(service, "_REFRESH_MIN_GAP_S", 0.3)
    monkeypatch.setattr(service, "_REFRESH_INTERVAL_S", 30)
    monkeypatch.setattr(ytdlp_release, "supported", lambda: supported)
    monkeypatch.setattr(ytdlp_release, "installed_tag", lambda: TAG)
    monkeypatch.setattr(ytdlp_release, "refresh", lambda: passes.append(time.monotonic()))

    async def run():
        task = asyncio.create_task(service.ytdlp_refresh_loop())
        for _ in range(100):
            await asyncio.sleep(0.02)
            if passes:
                break
        if passes:
            service.request_ytdlp_refresh()          # inside the gap
            for _ in range(100):
                await asyncio.sleep(0.02)
                if len(passes) > 1:
                    break
        done = task.done()
        task.cancel()
        return done

    return passes, asyncio.run(run())


def test_a_refresh_asked_for_inside_the_gap_runs_as_the_gap_ends(monkeypatch):
    passes, _ = _loop(monkeypatch)
    assert len(passes) == 2 and passes[1] - passes[0] >= 0.25


def test_a_platform_yt_dlp_builds_nothing_for_runs_no_pass(monkeypatch):
    passes, ended = _loop(monkeypatch, supported=False)
    assert passes == [] and ended
