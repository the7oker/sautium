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
    assert "[private address]:24001" in text and "'…/x.flac'" in text and "[email]" in text


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
