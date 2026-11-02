"""What a signed request to the Web UI port is judged on (auth_hmac): the
path the router routes and the request-target as the client sent it, never
the URL Starlette builds again from the Host header — and the same for the
peer surface's limiter and its diagnostics signature."""

import asyncio
import sys
import time
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.datastructures import URL
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "backend", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import auth_hmac  # noqa: E402
import device_auth  # noqa: E402
import serving  # noqa: E402

TOKEN = "d" * 64


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The HMAC middleware over two routes — one whitelisted, one signed —
    with a fixed device token and this test client's Host as the node's."""
    monkeypatch.setattr(device_auth, "current_token", lambda secret: TOKEN)
    monkeypatch.setattr(auth_hmac, "_allowed_hosts", {"testserver"})

    async def echo(request):
        return JSONResponse({"path": request.scope["path"],
                             "query": request.scope["query_string"].decode()})

    app = Starlette(routes=[Route("/api/auth/status", echo),
                            Route("/api/secret", echo, methods=["GET", "POST"]),
                            Route("/api/item/{name}", echo)])
    app.add_middleware(auth_hmac.HMACAuthMiddleware, secret_path=tmp_path / ".api_secret")
    return TestClient(app)


def signed(method: str, target: str, body: bytes = b"") -> dict:
    ts = str(int(time.time()))
    return {"X-Sautium-Ts": ts,
            "X-Sautium-Sig": auth_hmac.sign(TOKEN.encode(), method, target, ts, body)}


def _url_says(monkeypatch, url: str) -> None:
    """Every Request's `url` names `url`, whatever was routed — what an
    affected Starlette made of a Host header carrying a path."""
    monkeypatch.setattr(Request, "url", property(lambda self: URL(url)))


def test_the_whitelist_is_judged_on_the_routed_path(client, monkeypatch):
    _url_says(monkeypatch, "http://testserver/api/auth/status")
    assert client.get("/api/secret").status_code == 401
    assert client.get("/api/auth/status").status_code == 200


def test_a_signature_covers_the_target_as_sent(client):
    target = "/api/item/caf%C3%A9?q=a%20b&x=%2F"
    r = client.get(target, headers=signed("GET", target))
    assert r.status_code == 200
    assert r.json() == {"path": "/api/item/café", "query": "q=a%20b&x=%2F"}
    # another spelling of the same route is another target
    assert client.get(target, headers=signed("GET", "/api/item/café?q=a%20b&x=%2F")).status_code == 401
    assert client.get(target, headers=signed("GET", "/api/item/caf%C3%A9")).status_code == 401


def test_a_signature_is_judged_on_the_target_whatever_request_url_says(client, monkeypatch):
    _url_says(monkeypatch, "http://testserver/api/auth/status")
    assert client.post("/api/secret", content=b"{}",
                       headers=signed("POST", "/api/secret", b"{}")).status_code == 200
    assert client.post("/api/secret", content=b"{}",
                       headers=signed("POST", "/api/auth/status", b"{}")).status_code == 401


def test_the_request_target_is_the_raw_path_and_query():
    scope = {"path": "/a b", "raw_path": b"/a%20b", "query_string": b"x=%2F"}
    assert serving.request_target(scope) == "/a%20b?x=%2F"
    assert serving.request_target({**scope, "query_string": b""}) == "/a%20b"
    assert serving.request_target({"path": "/plain", "query_string": b""}) == "/plain"


def test_the_peer_limiter_counts_by_the_routed_path(monkeypatch):
    import p2p_app
    _url_says(monkeypatch, "http://node/health")
    monkeypatch.setattr(p2p_app, "_hits", {})
    monkeypatch.setattr(p2p_app, "_health_hits", {})
    request = Request({"type": "http", "method": "GET", "path": "/api/sync/holdings",
                       "raw_path": b"/api/sync/holdings", "query_string": b"",
                       "headers": [], "client": ("192.0.2.7", 40000)})

    async def downstream(_request):
        return JSONResponse({})

    asyncio.run(p2p_app.rate_limit(request, downstream))
    assert p2p_app._hits.get("192.0.2.7") and not p2p_app._health_hits.get("192.0.2.7")


def test_the_diagnostics_signature_covers_the_target_as_sent(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from desktop.p2p import peer_auth
    from routers import peer_diag

    def hexkey(key):
        return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    server, friend = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    monkeypatch.setattr(peer_diag, "resolve_identity", lambda _s: {"public_key_hex": hexkey(server)})
    monkeypatch.setattr(peer_diag, "get_peer_chat", lambda: type(
        "Chat", (), {"friend_for_key": staticmethod(lambda _k: {"is_blocked": False})})())
    _url_says(monkeypatch, "http://master/api/diag/report")
    target = "/api/diag/bundle?warrant=0b7e"
    headers = peer_auth.sign_headers(friend.sign, hexkey(friend), hexkey(server), "POST", target, b"x")
    request = Request({"type": "http", "method": "POST", "path": "/api/diag/bundle",
                       "raw_path": b"/api/diag/bundle", "query_string": b"warrant=0b7e",
                       "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]})
    assert peer_diag._verify(request, b"x") == (hexkey(friend), None)
