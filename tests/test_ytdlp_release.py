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


def test_a_signature_number_outside_the_keys_range_is_refused(pinned):
    sums = b"0" * 64 + b"  yt-dlp_linux.zip\n"
    subpackets = b"\x05\x02" + struct.pack(">I", int(time.time()))
    hashed = b"\x04\x00\x01\x0a" + struct.pack(">H", len(subpackets)) + subpackets
    wider = 1 << (pinned.private.key_size + 8)
    body = hashed + b"\x00\x00" + b"\x00\x00" + _mpi(wider)
    packet = b"\xc2" + bytes([192 + ((len(body) - 192) >> 8), (len(body) - 192) & 0xFF]) + body
    with pytest.raises(ytdlp_release.Refused):
        ytdlp_release.verify_signature(sums, packet)


def test_the_signature_parser_reads_both_packet_formats_and_refuses_a_header_cut_short(pinned):
    sums = b"0" * 64 + b"  yt-dlp_linux.zip\n"
    body = pinned.sign(sums)[3:]
    old_format = bytes([0x80 | (2 << 2) | 1]) + len(body).to_bytes(2, "big") + body
    ytdlp_release.verify_signature(sums, old_format)
    for cut in (b"\xc2", b"\xc2\xc0", b"\xc2\xff\x00\x00", b"\x89", b"\x89\x01", b"\x8a\x00\x00"):
        with pytest.raises(ytdlp_release.Refused):
            ytdlp_release.verify_signature(sums, cut)


# -- a pass against a fake GitHub -------------------------------------------------------

def _build(version: str, extra: dict = None, broken: bool = False) -> bytes:
    """A release archive: the executable for this platform and a folder."""
    name, exe = ytdlp_release._asset()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        script = zipfile.ZipInfo(exe)
        script.external_attr = (0o100755 << 16)
        zf.writestr(script, "\x00\x01 not a program" if broken else f"#!/bin/sh\necho {version}\n")
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


def _held() -> list:
    """What the cache holds: each build folder by its release's tag."""
    return sorted(p.name.rsplit("-", 1)[0] if p.is_dir() else p.name
                  for p in ytdlp_release.root().iterdir())


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
    assert exe.parent.parent == ytdlp_release.root() and exe.parent.name.startswith(f"{TAG}-")
    assert exe.name == ytdlp_release._asset()[1]
    assert os.access(exe, os.X_OK)
    github.asked.clear()
    assert ytdlp_release.refresh() == TAG
    assert github.asked == ["/repos/yt-dlp/yt-dlp/releases/latest"]     # nothing fetched again


def test_an_archive_its_signed_sums_do_not_name_is_never_unpacked(github):
    github.publish(TAG, _build(TAG))
    ytdlp_release.refresh()
    github.publish("2026.11.02", _build("2026.11.02"), sums_digest="0" * 64)
    assert ytdlp_release.refresh() == TAG
    assert _held() == [TAG, "current", "failed"]


def test_sums_another_key_signed_leave_the_build_in_place(github):
    github.publish(TAG, _build(TAG))
    ytdlp_release.refresh()
    github.publish("2026.11.02", _build("2026.11.02"), signer=SigningKey())
    assert ytdlp_release.refresh() == TAG
    assert _held() == [TAG, "current", "failed"]
    name, _ = ytdlp_release._asset()
    assert not [p for p in github.asked if p.endswith(f"/2026.11.02/{name}")]   # never fetched


def test_a_release_older_than_the_build_in_place_is_refused(github):
    github.publish("2026.11.02", _build("2026.11.02"))
    ytdlp_release.refresh()
    github.publish(TAG, _build(TAG))                    # validly signed, and older
    assert ytdlp_release.refresh() == "2026.11.02"
    assert _held() == ["2026.11.02", "current"]


def test_a_build_that_does_not_name_its_own_release_never_replaces_the_one_in_place(github):
    github.publish(TAG, _build(TAG))
    ytdlp_release.refresh()
    github.publish("2026.11.02", _build("2026.10.01"))
    assert ytdlp_release.refresh() == TAG
    assert _held() == [TAG, "current", "failed"]


def test_a_release_that_failed_to_install_is_fetched_again_only_after_a_day_or_a_newer_one(
        github, monkeypatch):
    """A build that will not start where the node keeps it (a noexec mount,
    application control) costs one download a day, not one a pass."""
    github.publish(TAG, _build(TAG, broken=True))
    assert ytdlp_release.refresh() is None
    name, _ = ytdlp_release._asset()
    fetched = lambda tag: [p for p in github.asked if p.endswith(f"/{tag}/{name}")]  # noqa: E731
    assert len(fetched(TAG)) == 1 and ytdlp_release.failed_tag() == TAG
    assert ytdlp_release.refresh() is None and len(fetched(TAG)) == 1
    later = time.time() + ytdlp_release._RETRY_FAILED_S + 1
    monkeypatch.setattr(ytdlp_release.time, "time", lambda: later)
    assert ytdlp_release.refresh() is None and len(fetched(TAG)) == 2
    github.publish("2026.11.02", _build("2026.11.02"))
    assert ytdlp_release.refresh() == "2026.11.02" and ytdlp_release.failed_tag() is None


def test_a_failure_of_the_nodes_own_is_not_held_against_the_release(github, monkeypatch):
    """A full disk, a folder held a moment: the next pass tries again."""
    import errno
    github.publish(TAG, _build(TAG))
    real = ytdlp_release._unpack

    def full(archive, into):
        raise OSError(errno.ENOSPC, "No space left on device", str(into))

    monkeypatch.setattr(ytdlp_release, "_unpack", full)
    assert ytdlp_release.refresh() is None and ytdlp_release.failed_tag() is None
    monkeypatch.setattr(ytdlp_release, "_unpack", real)
    assert ytdlp_release.refresh() == TAG


def test_the_build_in_place_stays_until_its_replacement_runs(github, monkeypatch):
    """A check that times out under load says nothing works; the folder in
    place is not taken before a replacement proves itself."""
    github.publish(TAG, _build(TAG))
    ytdlp_release.refresh()
    exe = ytdlp_release.executable()
    real, quiet = ytdlp_release._version, []
    monkeypatch.setattr(ytdlp_release, "_version", lambda path: quiet.append(path))
    assert ytdlp_release.refresh() is None and len(quiet) == 2      # in place, the replacement
    assert ytdlp_release.executable() == exe and exe.is_file()
    monkeypatch.setattr(ytdlp_release, "_version", real)
    monkeypatch.setattr(ytdlp_release, "failed_tag", lambda: None)  # a day on
    assert ytdlp_release.refresh() == TAG and ytdlp_release.executable() == exe


def test_a_failure_record_from_the_future_holds_nothing(github, monkeypatch):
    github.publish(TAG, _build(TAG, broken=True))
    ytdlp_release.refresh()
    assert ytdlp_release.failed_tag() == TAG
    earlier = time.time() - 3600                      # a clock set back an hour
    monkeypatch.setattr(ytdlp_release.time, "time", lambda: earlier)
    assert ytdlp_release.failed_tag() is None


def test_a_build_in_place_that_stopped_starting_is_installed_again(github):
    github.publish(TAG, _build(TAG))
    ytdlp_release.refresh()
    ytdlp_release.executable().write_bytes(b"\x00\x01 not a program")
    assert ytdlp_release.in_place() is None
    assert ytdlp_release.refresh() == TAG and ytdlp_release.in_place() == TAG


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
    assert _held() == ["2026.11.02", "2026.11.03", "current"]


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

def _loop(monkeypatch, supported=True, asked_during_the_pass=False, refused=False):
    import asyncio
    from streaming import service
    passes = []
    monkeypatch.setattr(service, "_REFRESH_MIN_GAP_S", 0.3)
    monkeypatch.setattr(service, "_FIRST_BUILD_GAP_S", 30)
    monkeypatch.setattr(service, "_REFRESH_INTERVAL_S", 30)
    monkeypatch.setattr(ytdlp_release, "supported", lambda: supported)
    monkeypatch.setattr(ytdlp_release, "failed_tag", lambda: None)

    def refresh():
        passes.append(time.monotonic())
        if asked_during_the_pass and len(passes) == 1:
            service.request_ytdlp_refresh()          # a fetch thread, while the pass runs
        return None if refused else TAG

    monkeypatch.setattr(ytdlp_release, "refresh", refresh)
    if refused:
        monkeypatch.setattr(ytdlp_release, "failed_tag", lambda: TAG)

    async def run():
        task = asyncio.create_task(service.ytdlp_refresh_loop())
        for _ in range(100):
            await asyncio.sleep(0.02)
            if passes:
                break
        if passes and not asked_during_the_pass:
            service.request_ytdlp_refresh()          # inside the gap
        for _ in range(100):
            await asyncio.sleep(0.02)
            if len(passes) > 1:
                break
        done = task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return done

    return passes, asyncio.run(run())


def test_a_refresh_asked_for_inside_the_gap_runs_as_the_gap_ends(monkeypatch):
    passes, _ = _loop(monkeypatch)
    assert len(passes) == 2 and passes[1] - passes[0] >= 0.25


def test_after_a_refused_release_the_long_gap_holds_while_nothing_is_in_place(monkeypatch):
    """Not a pass every few minutes: the first-build gap is a node's that
    started offline."""
    passes, _ = _loop(monkeypatch, refused=True)
    assert len(passes) == 2 and passes[1] - passes[0] >= 0.25


def test_a_refresh_asked_for_during_a_pass_is_kept(monkeypatch):
    passes, _ = _loop(monkeypatch, asked_during_the_pass=True)
    assert len(passes) == 2


def test_a_platform_yt_dlp_builds_nothing_for_runs_no_pass(monkeypatch):
    passes, ended = _loop(monkeypatch, supported=False)
    assert passes == [] and ended


def test_a_refresh_asked_for_once_the_loop_is_gone_is_no_error(monkeypatch):
    from streaming import service
    _loop(monkeypatch)
    service.request_ytdlp_refresh()


@pytest.mark.parametrize("platform_, machine, musl, wide, asset", [
    ("win32", "ARM64", False, True, ("yt-dlp_win_arm64.zip", "yt-dlp_arm64.exe")),
    ("win32", "AMD64", False, True, ("yt-dlp_win.zip", "yt-dlp.exe")),
    ("linux", "x86_64", True, True, ("yt-dlp_musllinux.zip", "yt-dlp_musllinux")),
    ("linux", "aarch64", True, True, ("yt-dlp_musllinux_aarch64.zip", "yt-dlp_musllinux_aarch64")),
    ("linux", "aarch64", False, True, ("yt-dlp_linux_aarch64.zip", "yt-dlp_linux_aarch64")),
    ("linux", "armv7l", False, False, ("yt-dlp_linux_armv7l.zip", "yt-dlp_linux_armv7l")),
    ("linux", "aarch64", False, False, ("yt-dlp_linux_armv7l.zip", "yt-dlp_linux_armv7l")),
])
def test_each_platform_takes_the_build_yt_dlp_makes_for_it(monkeypatch, platform_, machine, musl,
                                                             wide, asset):
    """Windows on ARM reports its own CPU to an emulated x64 Python too, and a
    32-bit userland on a 64-bit ARM kernel hears aarch64 from it."""
    monkeypatch.setattr(ytdlp_release.sys, "platform", platform_)
    monkeypatch.setattr(ytdlp_release.platform, "machine", lambda: machine)
    monkeypatch.setattr(ytdlp_release, "_musl", lambda: musl)
    monkeypatch.setattr(ytdlp_release, "_is_64bit", lambda: wide)
    assert ytdlp_release._asset() == asset
