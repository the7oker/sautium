"""What a node's logs keep — and so what a support bundle's log excerpt or a
saved report can carry: no query string of a request (a search, a signed
media URL, a one-use token), no request URL httpx would name at INFO, no
title of a track played or skipped, no text of a message to the assistant, no
email address; a saved report scrubbed of addresses and paths; and on
Docker, a bounded log per container.
"""

import ast
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT

BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# The backend's logging as every runtime applies it (config.LOGGING_CONFIG),
# in a process of its own: an access line as uvicorn writes it, and httpx's
# request line.
DRIVER = r"""
import logging, logging.config, sys
sys.path.insert(0, %(backend)r)
from config import LOGGING_CONFIG
logging.config.dictConfig(LOGGING_CONFIG)
logging.getLogger("uvicorn.access").info(
    '%%s - "%%s %%s HTTP/%%s" %%d', "192.0.2.5:51000", "GET",
    "/api/discovery/search?q=a+private+search&sig=abc123", "1.1", 200)
logging.getLogger("httpx").info(
    'HTTP Request: GET https://example.org/check-email?email=someone%%40example.com "HTTP/1.1 200 OK"')
logging.getLogger("httpx").warning("httpx warns: kept")
"""


def test_the_access_line_keeps_no_query_and_httpx_names_no_url_at_info():
    proc = subprocess.run([sys.executable, "-c", DRIVER % {"backend": str(BACKEND)}],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert '"GET /api/discovery/search HTTP/1.1" 200' in out
    assert "private" not in out and "abc123" not in out
    assert "someone" not in out and "httpx warns: kept" in out


def test_a_listen_logs_no_title_at_info(monkeypatch, caplog):
    from playback import tracker
    from playback.queue import QueueItem
    monkeypatch.setattr(tracker, "_db_execute", lambda sql, params=None: None)
    monkeypatch.setattr(tracker, "_scrobble_async", lambda method, **kw: True)
    monkeypatch.setattr(tracker, "_play_session", None)
    item = QueueItem(track_id="0f0e0d0c-0b0a-4908-8706-050403020100", media_file_id=None,
                     source={"kind": "proxy", "token": "t"}, title="A Private Title",
                     artist="A Private Artist", duration_seconds=200.0, preview=True,
                     provider="youtube", excerpt=False)
    with caplog.at_level(logging.INFO):
        for pos in (0.0, 150.0, 199.0):
            tracker.track_play_event("playing", pos, 200.0, item)
        tracker.track_play_event("stopped", 0.0, 0.0, None)
    assert not [r for r in caplog.records
                if r.levelno >= logging.INFO and "Private" in r.getMessage()]


def _message_text_logged(path: Path) -> list:
    """Logger calls that format `message` itself — its length is allowed."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "logger"):
            continue
        lengths = {id(arg) for call in ast.walk(node) if isinstance(call, ast.Call)
                   and isinstance(call.func, ast.Name) and call.func.id == "len"
                   for arg in call.args}
        for name in ast.walk(node):
            if isinstance(name, ast.Name) and name.id == "message" and id(name) not in lengths:
                found.append(f"{path.name}:{node.lineno}")
    return found


# What a node plays, searches for, and who its owner is: names these modules
# must not put in a line logged at INFO or above (their DEBUG lines may) — nor
# in the text of an exception they raise, which a caller logs or a playback
# trace keeps, nor in what they hand to a call that logs it, nor through a
# variable the function built from them. A path may appear in such a line —
# what ships is scrubbed, and a library file's name is cut to its extension
# (diag_protocol.scrub_personal) — but a bare file name may not: nothing
# marks it as one.
_CONTENT_ATTRS = {"title", "artist", "album", "username"}
_CONTENT_NAMES = {"search", "username", "artist", "title", "album", "artist_name", "track_name",
                  "album_name"}
_BARE_NAME_CALLS = {"basename"}
_LOGGED_LEVELS = {"info", "warning", "error", "exception", "critical"}
_LOGGING_CALLS = {"background", "lose_mirror"}      # each logs its first argument
_PLAY_MODULES = sorted([*BACKEND.glob("playback/**/*.py"), *BACKEND.glob("streaming/**/*.py"),
                        BACKEND / "routers" / "player.py", BACKEND / "lastfm_auth.py",
                        BACKEND / "auth_hmac.py", BACKEND / "genius.py", BACKEND / "lrclib.py"])


def _names_content(node: ast.AST, fmt: ast.AST = None, built: frozenset = frozenset()) -> bool:
    return any(isinstance(sub, ast.Attribute) and sub.attr in _CONTENT_ATTRS
               or isinstance(sub, ast.Name) and (sub.id in _CONTENT_NAMES or sub.id in built)
               or isinstance(sub, ast.Constant) and sub is not fmt and sub.value in _CONTENT_ATTRS
               or isinstance(sub, ast.Call) and getattr(sub.func, "attr", getattr(
                   sub.func, "id", None)) in _BARE_NAME_CALLS
               for sub in ast.walk(node))


def _built_from_content(fn: ast.AST) -> frozenset:
    """The variables a function assigns an f-string naming content."""
    return frozenset(t.id for node in ast.walk(fn) if isinstance(node, ast.Assign)
                     and any(isinstance(s, ast.JoinedStr) and _names_content(s)
                             for s in ast.walk(node.value))
                     for t in node.targets if isinstance(t, ast.Name))


def _content_logged(path: Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    built = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = _built_from_content(fn)
            for node in ast.walk(fn):
                built[id(node)] = built.get(id(node), frozenset()) | names
    found = []
    for node in ast.walk(tree):
        where = f"{path.relative_to(BACKEND)}:{getattr(node, 'lineno', 0)}"
        if isinstance(node, ast.Raise) and node.exc is not None:
            if any(isinstance(s, ast.JoinedStr) and _names_content(s) for s in ast.walk(node.exc)):
                found.append(where)
            continue
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if (name in _LOGGED_LEVELS and isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name) and func.value.id == "logger"):
            fmt = node.args[0] if node.args else None
            if _names_content(node, fmt, built.get(id(node), frozenset())) or (
                    isinstance(fmt, ast.Constant)
                    and str(fmt.value).startswith("Host guard accepts")):
                found.append(where)
        elif name in _LOGGING_CALLS and node.args and _names_content(node.args[0]):
            found.append(where)
    return found


def test_no_logged_line_names_what_is_played_searched_or_who_listens():
    assert [hit for path in _PLAY_MODULES for hit in _content_logged(path)] == []


def test_a_played_files_path_leaves_the_node_without_its_name(tmp_path, monkeypatch, caplog):
    """What the local outputs log and raise about a library file — its
    transcode, a file gone since the scan, the URI HQPlayer is handed — names
    neither the file nor its folders once scrubbed for support, and a bare
    file name is not logged at INFO at all."""
    import subprocess as sp

    from desktop.p2p.diag_protocol import scrub_personal
    from hqplayer_client import file_path_to_uri, redact_uri
    from streaming import local
    from streaming.base import ProviderError

    folder = tmp_path / "Some Artist" / "Some Album"
    folder.mkdir(parents=True)
    played = folder / "05 Some Song Title.m4a"
    played.write_bytes(b"\0" * 64)

    def ffmpeg(cmd, **kw):
        Path(cmd[-1]).write_bytes(b"fLaC" + b"\0" * 64)
        return sp.CompletedProcess(cmd, 0)

    monkeypatch.setattr(local.subprocess, "run", ffmpeg)
    provider = local.LocalTranscodeProvider()
    with caplog.at_level(logging.INFO):
        provider._download(str(played))
        with pytest.raises(ProviderError) as gone:
            provider._download(str(folder / "06 Another Title.m4a"))
    said = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    said += [str(gone.value), f"[Errno 2] No such file or directory: '{played}'",
             f"HQPlayer answered the add of {redact_uri(file_path_to_uri(str(played)))}: taken"]
    for line in said:
        shipped = scrub_personal(line)
        assert "Some" not in shipped and "Title" not in shipped, shipped


def test_no_agent_runner_logs_the_text_of_a_message():
    runners = [BACKEND / "claude_code_runner.py", BACKEND / "codex_runner.py"]
    assert [hit for path in runners for hit in _message_text_logged(path)] == []


def test_verifying_an_email_logs_no_address(monkeypatch, caplog):
    from desktop.p2p import email_verify
    monkeypatch.setattr(email_verify, "_identity_ctx", lambda username, password: {
        "invite_code": "someone#AAAA-BBBB-CCCC", "public_key_hex": "ab" * 32,
        "sign": lambda data: b"\x00" * 64})
    monkeypatch.setattr(email_verify, "_post_worker",
                        lambda path, payload: {"status": "sent", "verified": True})
    monkeypatch.setattr(email_verify, "_get_worker",
                        lambda path, params: {"verified": True})
    with caplog.at_level(logging.INFO, logger=email_verify.logger.name):
        assert email_verify.send_verification_email("someone.private@example.com")
        assert email_verify.is_email_already_verified("someone.private@example.com")
    assert caplog.records and not [r for r in caplog.records if "private" in r.getMessage()]


def test_a_saved_report_names_no_address_or_path(tmp_path, monkeypatch):
    from desktop import diag_bundle
    monkeypatch.setattr(diag_bundle, "system_facts", lambda config, **kw: {})
    (tmp_path / "launcher.log").write_text(
        "INFO: peer 192.0.2.7:24001 answered for someone@example.com\n"
        "ERROR: [Errno 2] No such file or directory: '/home/someone/Music/x.flac'\n",
        encoding="utf-8")
    text = diag_bundle.save_report(data_dir=tmp_path, config={}, state="Failed",
                                   detail="").read_text(encoding="utf-8")
    assert "someone" not in text and "192.0.2" not in text
    assert "[private address]:24001" in text and "'…/[name].flac'" in text and "[email]" in text


@pytest.mark.parametrize("name", ["docker-compose.yml", "docker-compose.mac.yml",
                                  "docker-compose.wsl.yml"])
def test_every_container_keeps_a_bounded_log(name):
    yaml = pytest.importorskip("yaml")
    services = yaml.safe_load((REPO_ROOT / name).read_text(encoding="utf-8"))["services"]
    assert services
    for service, spec in services.items():
        log = spec.get("logging") or {}
        assert log.get("driver") == "json-file", service
        assert log.get("options", {}).get("max-size") and log["options"].get("max-file"), service
