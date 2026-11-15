"""The node's side of the host agent (backend/host_channel.py,
backend/routers/host.py) on a real database: a request is an empty file of
the verb's name and a row's asked_at; the answer is taken only under the
channel's key, behind the node's own door, on its one path; a page reads it
and is woken."""

import hashlib
import json
import secrets
import sys
import time
from pathlib import Path

import psycopg2
import psycopg2.pool
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

import auth_hmac  # noqa: E402
import device_auth  # noqa: E402
import host_channel  # noqa: E402

KEY = "cd" * 32
SECRET = b"s" * 64
TOKEN = "t" * 64


@pytest.fixture
def node(scratch_dsn, tmp_path, monkeypatch):
    import db_pool
    from routers import host, settings as settings_router
    pool = psycopg2.pool.ThreadedConnectionPool(1, 3, dsn=scratch_dsn, options="-c timezone=UTC")
    monkeypatch.setattr(db_pool, "_pool", pool)
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    conn.cursor().execute("TRUNCATE host_requests")
    requests = tmp_path / "requests"
    requests.mkdir()
    monkeypatch.setattr(host_channel, "REQUESTS_DIR", requests)
    monkeypatch.setenv("SAUTIUM_HOST_CHANNEL_KEY", KEY)
    woken = []
    monkeypatch.setattr(settings_router, "notify_library_subscribers", lambda: woken.append(1))
    monkeypatch.setattr(device_auth, "current_token", lambda secret: TOKEN)
    app = FastAPI()
    app.include_router(host.router)
    door = auth_hmac.HMACAuthMiddleware(app, tmp_path / ".api_secret")
    monkeypatch.setattr(door, "_get_secret", lambda: SECRET)
    client = TestClient(door, base_url="http://127.0.0.1:8800")
    yield client, requests, conn, woken
    conn.close()
    pool.closeall()


def _signed(client, path: str, body: dict, key: bytes):
    raw = json.dumps(body).encode()
    ts, nonce = str(int(time.time())), secrets.token_hex(16)
    sig = auth_hmac.sign(key, "POST", path, ts, raw, nonce)
    return client.post(path, content=raw, headers={
        "Content-Type": "application/json", "X-Sautium-Ts": ts, "X-Sautium-Nonce": nonce,
        "X-Sautium-Body-Sha256": hashlib.sha256(raw).hexdigest(), "X-Sautium-Sig": sig})


def test_a_request_is_an_empty_file_of_the_verbs_name_and_a_row(node):
    client, requests, conn, woken = node
    response = _signed(client, "/api/host/update", {}, TOKEN.encode())
    assert response.status_code == 200, response.text
    assert [(p.name, p.read_bytes()) for p in requests.iterdir()] == [("update", b"")]
    assert _signed(client, "/api/host/update", {}, TOKEN.encode()).status_code == 200
    assert [p.name for p in requests.iterdir()] == ["update"]
    with conn.cursor() as cur:
        cur.execute("SELECT verb::text, asked_at IS NOT NULL, outcome FROM host_requests")
        assert cur.fetchall() == [("update", True, None)]
    assert woken
    assert _signed(client, "/api/host/reboot", {}, TOKEN.encode()).status_code == 404
    assert [p.name for p in requests.iterdir()] == ["update"]


def test_a_node_without_an_agent_says_so_and_writes_nothing(node, monkeypatch):
    client, requests, conn, _ = node
    monkeypatch.delenv("SAUTIUM_HOST_CHANNEL_KEY")
    assert _signed(client, "/api/host/check", {}, TOKEN.encode()).status_code == 409
    assert list(requests.iterdir()) == []
    state = client.get("/api/host", headers=_get_headers("/api/host"))
    assert state.json() == {"available": False, "requests": {}}


def _get_headers(path: str) -> dict:
    ts, nonce = str(int(time.time())), secrets.token_hex(16)
    return {"X-Sautium-Ts": ts, "X-Sautium-Nonce": nonce,
            "X-Sautium-Sig": auth_hmac.sign(TOKEN.encode(), "GET", path, ts, b"", nonce)}


def test_the_answer_is_taken_under_the_channels_key_alone(node):
    client, _, conn, woken = node
    answer = {"verb": "check", "outcome": "offered", "detail": "Sautium 0.3.0 is available",
              "release": "v0.3.0", "report": None}
    for key in (TOKEN.encode(), SECRET, b"e" * 64):
        assert _signed(client, host_channel.ANSWER_PATH, answer, key).status_code == 401
    assert _signed(client, host_channel.ANSWER_PATH, answer, KEY.encode()).status_code == 200
    state = client.get("/api/host", headers=_get_headers("/api/host")).json()
    assert state["available"] is True
    check = state["requests"]["check"]
    assert (check["outcome"], check["release"], check["has_report"]) == ("offered", "v0.3.0", False)
    assert woken
    # the channel's key signs nothing else
    assert _signed(client, "/api/host/update", {}, KEY.encode()).status_code == 401


def test_an_answer_with_no_such_outcome_for_its_verb_is_refused(node):
    client, _, _, _ = node
    for verb, outcome in (("check", "updated"), ("diagnostics", "offered"), ("reboot", "done")):
        response = _signed(client, host_channel.ANSWER_PATH,
                           {"verb": verb, "outcome": outcome, "detail": ""}, KEY.encode())
        assert response.status_code == 422, (verb, outcome)


def test_the_diagnostics_report_stays_until_a_new_one_is_made(node):
    client, _, _, _ = node
    report = "== versions\nLinux 6.18\n"
    assert _signed(client, host_channel.ANSWER_PATH, {"verb": "diagnostics", "outcome": "done",
                                                      "detail": "made", "report": report},
                   KEY.encode()).status_code == 200
    assert _signed(client, host_channel.ANSWER_PATH, {"verb": "diagnostics", "outcome": "floor",
                                                      "detail": "made 3 s ago"}, KEY.encode()).status_code == 200
    got = client.get("/api/host/diagnostics/report", headers=_get_headers("/api/host/diagnostics/report"))
    assert got.json()["report"] == report
    state = client.get("/api/host", headers=_get_headers("/api/host")).json()["requests"]["diagnostics"]
    assert (state["outcome"], state["has_report"]) == ("floor", True)


def test_a_request_never_follows_a_link_the_folder_holds(node, tmp_path):
    client, requests, _, _ = node
    target = tmp_path / "elsewhere"
    target.write_text("kept\n")
    (requests / "check").symlink_to(target)
    with pytest.raises(OSError):
        host_channel.ask("check")
    assert target.read_text() == "kept\n"
