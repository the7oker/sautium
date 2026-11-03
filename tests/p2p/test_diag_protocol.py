"""Support diagnostics wire protocol — warrant signing/verification, the
Box payloads, report encoding and log scrubbing (desktop/p2p/diag_protocol.py)."""

import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from nacl.exceptions import CryptoError

from desktop.p2p import diag_protocol as dp

MASTER_SEED = bytes.fromhex("11" * 32)
NODE_SEED = bytes.fromhex("22" * 32)
OTHER_SEED = bytes.fromhex("33" * 32)


def _pub(seed: bytes) -> str:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw).hex()


MASTER = Ed25519PrivateKey.from_private_bytes(MASTER_SEED)
MASTER_PUB = _pub(MASTER_SEED)
NODE_PUB = _pub(NODE_SEED)
OTHER_PUB = _pub(OTHER_SEED)
NOW = 1_800_000_000


def _warrant(**kw):
    args = dict(target=NODE_PUB, scopes=["logs", "system"], since=NOW - 86400,
                issuer=MASTER_PUB, now=NOW, warrant_id="0d9a5f4e-7e2b-4c3b-9a0e-1f2c3d4e5f60")
    args.update(kw)
    return dp.sign_warrant(MASTER.sign, **args)


def test_payload_is_canonical_and_deterministic():
    p = dp.warrant_payload("id-1", MASTER_PUB.upper(), NODE_PUB, NOW, NOW + 10,
                           ["system", "logs"], None)
    assert p == (f"sautium-diag-warrant:v1:id-1:{MASTER_PUB}:{NODE_PUB}:{NOW}:{NOW + 10}:"
                 f"logs,system:").encode()
    w1, w2 = _warrant(), _warrant()
    assert w1 == w2                                  # Ed25519 is deterministic
    assert w1["scopes"] == ["logs", "system"] and w1["expires_at"] == NOW + dp.WARRANT_TTL_DEFAULT_S


def test_verify_accepts_and_rejects():
    w = _warrant()
    ok = lambda ww, **kw: dp.verify_warrant(ww, kw.pop("target", NODE_PUB),
                                            now=kw.pop("now", NOW + 5), issuer=MASTER_PUB)
    assert ok(w) == (True, None)
    assert ok(w, now=w["expires_at"] - 1) == (True, None)
    assert ok(w, now=w["expires_at"])[1] == "expired"
    assert ok(w, now=NOW - dp.WARRANT_ISSUE_SKEW_S - 1)[1] == "issued in the future"
    assert ok(w, target=OTHER_PUB)[1] == "addressed to another node"
    assert dp.verify_warrant(w, NODE_PUB, now=NOW, issuer=OTHER_PUB)[1] == "not from the master"
    assert ok(dict(w, scopes=["logs", "everything"]))[1] == "unknown scope"
    assert ok(dict(w, scopes=[]))[1] == "unknown scope"
    assert ok(dict(w, sig="00" * 64))[1] == "bad signature"
    assert ok(dict(w, since=None))[1] == "bad signature"          # since is signed
    assert ok(dict(w, v=2))[1] == "unsupported version"
    assert ok(dict(w, id="not-a-uuid"))[1] == "malformed"
    assert ok("junk")[1] == "unsupported version"
    forged = dp.sign_warrant(Ed25519PrivateKey.from_private_bytes(OTHER_SEED).sign,
                             target=NODE_PUB, scopes=["logs"], issuer=MASTER_PUB, now=NOW)
    assert ok(forged)[1] == "bad signature"
    with pytest.raises(ValueError):
        dp.sign_warrant(MASTER.sign, target=NODE_PUB, scopes=["nope"], issuer=MASTER_PUB)


def test_the_playback_scope_is_signed_and_verified_but_only_asked_for_by_name():
    w = _warrant(scopes=["playback", "logs"])
    assert w["scopes"] == ["logs", "playback"]
    assert dp.verify_warrant(w, NODE_PUB, now=NOW + 5, issuer=MASTER_PUB) == (True, None)
    # A node that has not updated refuses a warrant naming a scope it does not
    # know, so the default stays the seven every deployed node has.
    assert "playback" in dp.SCOPES and "playback" not in dp.DEFAULT_SCOPES
    assert set(dp.DEFAULT_SCOPES) == set(dp.SCOPES) - {"playback"}


def test_frame_shape():
    frame = dp.warrant_frame(_warrant())
    assert frame["type"] == dp.FRAME_TYPE == "diag_warrant"
    assert frame["warrant"]["target"] == NODE_PUB


def test_box_round_trip_and_wrong_party():
    data = b"\x00tar.gz bytes" * 1000
    boxed = dp.encrypt_to(NODE_SEED, MASTER_PUB, data)
    assert boxed != data and len(boxed) == len(data) + 24 + 16
    assert dp.decrypt_from(MASTER_SEED, NODE_PUB, boxed) == data
    with pytest.raises(CryptoError):
        dp.decrypt_from(MASTER_SEED, OTHER_PUB, boxed)          # wrong sender
    with pytest.raises(CryptoError):
        dp.decrypt_from(OTHER_SEED, NODE_PUB, boxed)            # wrong recipient
    with pytest.raises(CryptoError):
        dp.decrypt_from(MASTER_SEED, NODE_PUB, boxed[:-1])      # tampered


def test_report_strips_local_only_and_bounds_detail():
    events = [
        {"kind": "backend.crashed", "ts": "2026-08-26T10:00:00+00:00",
         "detail": {"rc": 1, "log_tail": "Traceback…", "uptime_s": 3}},
        {"kind": "node.started", "ts": "2026-08-26T10:01:00+00:00",
         "detail": {"blob": "x" * (dp.EVENT_DETAIL_MAX_CHARS + 1)}},
    ]
    decoded = dp.decode_report(dp.encode_report(events))
    assert decoded[0]["detail"] == {"rc": 1, "uptime_s": 3}
    assert decoded[1]["detail"] == {"truncated": True, "keys": ["blob"]}
    assert "log_tail" not in dp.encode_report(events).decode()


def test_decode_report_rejects_bad_input():
    good = {"v": 1, "events": [{"kind": "sync.failed", "ts": "2026-08-26T10:00:00+00:00",
                                "detail": {"error": "boom"}}]}
    assert dp.decode_report(json.dumps(good).encode())[0]["kind"] == "sync.failed"
    # A kind this build has never heard of still lands — forward compatibility
    # for a node one release ahead of the master.
    future = json.dumps({"v": 1, "events": [dict(good["events"][0], kind="future.kind_2")]})
    assert dp.decode_report(future.encode())[0]["kind"] == "future.kind_2"
    bad = [
        b"not json",
        json.dumps({"v": 2, "events": []}).encode(),
        json.dumps({"v": 1, "events": []}).encode(),
        json.dumps({"v": 1, "events": [dict(good["events"][0], kind="not a kind")]}).encode(),
        json.dumps({"v": 1, "events": [dict(good["events"][0], kind="a.b.c")]}).encode(),
        json.dumps({"v": 1, "events": [dict(good["events"][0], kind="x." + "y" * 70)]}).encode(),
        json.dumps({"v": 1, "events": [dict(good["events"][0], ts="yesterday")]}).encode(),
        json.dumps({"v": 1, "events": [dict(good["events"][0], detail="str")]}).encode(),
        json.dumps({"v": 1, "events": good["events"] * (dp.REPORT_MAX_EVENTS + 1)}).encode(),
        json.dumps({"v": 1, "events": [dict(good["events"][0],
                                            detail={"x": "y" * dp.EVENT_DETAIL_MAX_CHARS})]}).encode(),
    ]
    for raw in bad:
        with pytest.raises(ValueError):
            dp.decode_report(raw)


def test_scrub_secrets():
    text = ("key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123 used\n"
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig\n"
            'api_key="abc123def456" password=hunter2 token: tkn_9\n'
            "dsn postgresql://sautium:s3cret@localhost:15432/sautium\n"
            f"sig={'ab' * 64} pubkey={'cd' * 32}\n")
    out = dp.scrub_secrets(text)
    for secret in ("sk-ant-api03", "eyJhbGci", "abc123def456", "hunter2", "tkn_9", "s3cret", "ab" * 64):
        assert secret not in out
    assert "cd" * 32 in out                     # a node pubkey is not a secret
    assert "postgresql://sautium:[redacted]@localhost" in out
    assert 'api_key="[redacted]' in out


def test_scrub_personal_leaves_no_email_address_or_path():
    text = ("No such file or directory: '/home/someone/Music/A Band/01 Song.flac'\n"
            "Permission denied: 'C:\\\\Users\\\\Some One\\\\AppData\\\\Local\\\\Sautium'\n"
            r"cannot open C:\Users\Some One\Music\x y\track.flac now" "\n"
            "url file:///home/someone/x.txt and ~/Music/a.flac and \\\\nas\\share\\x.flac\n"
            "sent to someone.else+tag@example.co.uk\n"
            "peers 192.0.2.20:8800 8.8.8.8 100.64.0.1 127.0.0.1 ::1 fe80::1 "
            "2001:4860:4860::8888\n"
            'File "/app/backend/main.py", line 12, in run\n'
            "GET /api/settings/sync HTTP/1.1 at 12:34:56 bind 0.0.0.0:8800\n"
            "dsn postgresql://sautium:s3cret@localhost:15432/sautium\n")
    out = dp.scrub_personal(text)
    for gone in ("someone", "Some One", "Music", "example.co.uk", "192.0.2", "8.8.8.8",
                 "100.64", "2001:4860", "fe80", "s3cret", "nas", "AppData"):
        assert gone not in out, gone
    for kept in ("'…/01 Song.flac'", "…/track.flac", "file://[path]", "…/a.flac", "…/x.flac",
                 "'[path]'", "[email]", "[private address]:8800", "[public address]",
                 "[carrier-grade NAT address]", "[loopback address]", "[link-local address]",
                 '"…/main.py", line 12', "GET /api/settings/sync", "12:34:56", "0.0.0.0:8800"):
        assert kept in out, kept


def test_a_report_is_kept_scrubbed_on_the_master():
    events = [{"kind": "sync.failed", "ts": "2026-11-03T01:00:00+00:00",
               "detail": {"error": "peer 192.0.2.20 refused '/home/someone/x.box'",
                          "nested": [{"who": "someone@example.com"}], "count": 3}}]
    [kept] = dp.decode_report(json.dumps({"v": 1, "events": events}).encode())
    assert kept["detail"] == {"error": "peer [private address] refused '…/x.box'",
                              "nested": [{"who": "[email]"}], "count": 3}


@pytest.mark.parametrize("text, scrubbed", [
    ("connect to 8.8.8.8.", "connect to [public address]."),
    ("peer 192.0.2.10.", "peer [private address]."),
    ("1.2.3.4.5 is a version", "1.2.3.4.5 is a version"),
    ("react@18.3.1 builds", "react@18.3.1 builds"),
    ("write to юзер@пошта.укр now", "write to [email] now"),
    (r"C:\Users\john.smith", "[path]"),
    ("/home/john.doe and /Users/jane.doe/", "[path] and [path]"),
    ("~someone/Music/x.flac", "…/x.flac"),
    ("share smb://nas.local/home/someone/Music", "share smb://[path]"),
    ("music_dir:/music/A/b.flac", "music_dir:…/b.flac"),
    ("/volume1/homes/someone/Music/a.flac", "…/a.flac"),
    ("guard: ['box.tail1a2b.ts.net', 'Someones-MacBook.local', 'host.docker.internal']",
     "guard: ['[host]', '[host]', 'host.docker.internal']"),
    ("GET http://pixel.lan:8200/desc.xml", "GET http://[host]:8200/desc.xml"),
    ("https://api.github.com/repos", "https://api.github.com/repos"),
    ("Phase B: failed {a: 1} ~3 s", "Phase B: failed {a: 1} ~3 s"),
    # Python's error text doubles a Windows path's backslashes
    (r"[WinError 53] The network path was not found: '\\\\SOMEONES-NAS\\Music'",
     "[WinError 53] The network path was not found: '[path]'"),
    (r"'\\\\?\\C:\\Users\\Some One\\Music'", "'[path]'"),
    ("mount //SERVER/share/Music failed", "mount [path] failed"),
    (r"unquoted C:\Users\John Smith", "unquoted [path]"),
    ("/music is not mounted", "[path] is not mounted"),
    ("copy /home/someone/Music/x.flac and http://example.org/a/b",
     "copy …/x.flac and http://example.org/a/b"),
    ("ssh someone@192.0.2.5 failed", "ssh [email] failed"),
    ("connect to box.home.arpa failed", "connect to [host] failed"),
    # A module's name is no machine's
    ("2026-11-03 [routers.home] INFO: built; [streaming.local] too",
     "2026-11-03 [routers.home] INFO: built; [streaming.local] too"),
    ("'_thread._local' object; threading.local; Path.home(); werkzeug.local.LocalProxy",
     "'_thread._local' object; threading.local; Path.home(); werkzeug.local.LocalProxy"),
])
def test_scrub_personal_cases(text, scrubbed):
    assert dp.scrub_personal(text) == scrubbed


def test_scrub_detail_takes_keys_and_a_whole_path_value():
    assert dp.scrub_detail({"/home/someone/x.flac": r"C:\Users\Some One",
                            "folder": "D:\\Music of Some One", "n": 3}) == {
        "…/x.flac": "[path]", "folder": "[path]", "n": 3}


def test_a_value_that_starts_with_a_path_is_scrubbed_whole():
    said = "/app/data/config.json: token=abcd1234efgh for someone@example.com"
    assert dp.scrub_detail({"error": said, "path": said, "file": "/music/A/a.flac from 192.0.2.5"}) == {
        "error": "[path] token=[redacted] for [email]",
        "path": "…/config.json: token=[redacted] for [email]",
        "file": "…/a.flac from [private address]"}


def test_two_keys_that_scrub_alike_keep_both_values():
    assert dp.scrub_detail({"/home/a/x.flac": 1, "/home/b/x.flac": 2, "/home/c/x.flac": 3}) == {
        "…/x.flac": 1, "…/x.flac (2)": 2, "…/x.flac (3)": 3}


@pytest.mark.parametrize("text", ["'\\\\" + "a" * (1 << 16), "'\\\\" + "a" * (1 << 16) + '"',
                                  "'\\\\" + "a" * (1 << 16) + "\n'", "\\\\" + "a" * (1 << 16) + "\n"])
def test_a_server_name_with_no_end_is_scrubbed_in_linear_time(text):
    """A run after two backslashes with no separator, quote or space to end
    it — 64 K characters — takes milliseconds, as a value and under a key
    that names a path; a search that tried every way to split it took
    seconds."""
    import time
    started = time.monotonic()
    dp.scrub_personal(text)
    dp.scrub_detail({"path": text})
    assert time.monotonic() - started < 1.0


def test_a_long_token_is_scrubbed_in_linear_time():
    """A database log's hex blob: 64 K characters take milliseconds to
    scrub; a search that tried every position in it took seconds here,
    and about an hour for a 2 MiB log tail."""
    import time
    blob = "STATEMENT: '\\x" + "ab" * (1 << 15) + "'::bytea someone@example.com"
    started = time.monotonic()
    out = dp.scrub_personal(blob)
    assert time.monotonic() - started < 1.0
    assert out.endswith("[email]")
