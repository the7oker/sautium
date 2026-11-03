"""What a signed request to the Web UI port is judged on (auth_hmac): the
path the router routes and the request-target as the client sent it, never
the URL Starlette builds again from the Host header — and the same for the
peer surface's limiter and its diagnostics signature. A signature is taken
once within the window, and every signer of the tree signs with a nonce."""

import asyncio
import collections
import hashlib
import hmac
import json
import secrets
import shutil
import subprocess
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
    monkeypatch.setattr(auth_hmac, "_seen", type(auth_hmac._seen)(), raising=False)
    monkeypatch.setattr(auth_hmac, "_seen_wall", [], raising=False)
    monkeypatch.setattr(auth_hmac, "_seen_order", collections.deque(), raising=False)

    async def echo(request):
        return JSONResponse({"path": request.scope["path"],
                             "query": request.scope["query_string"].decode()})

    app = Starlette(routes=[Route("/api/auth/status", echo),
                            Route("/api/secret", echo, methods=["GET", "POST"]),
                            Route("/api/item/{name}", echo)])
    app.add_middleware(auth_hmac.HMACAuthMiddleware, secret_path=tmp_path / ".api_secret")
    return TestClient(app)


def signed(method: str, target: str, body: bytes = b"", *, nonce: str | None = "draw",
           key: bytes = TOKEN.encode(), ts: str | None = None, digest: bool = True) -> dict:
    """The headers a signer sends, the canonical string written out here as
    the wire defines it; `nonce=None, digest=False` signs as a page from
    before the nonce and the declared digest."""
    ts = ts or str(int(time.time()))
    if nonce == "draw":
        nonce = secrets.token_hex(16)
    body_digest = hashlib.sha256(body).hexdigest()
    lines = [method, target, ts] + ([nonce] if nonce is not None else []) + [body_digest]
    headers = {"X-Sautium-Ts": ts,
               "X-Sautium-Sig": hmac.new(key, "\n".join(lines).encode(), hashlib.sha256).hexdigest()}
    if nonce is not None:
        headers["X-Sautium-Nonce"] = nonce
    if digest:
        headers["X-Sautium-Body-Sha256"] = body_digest
    return headers


def _url_says(monkeypatch, url: str) -> None:
    """Every Request's `url` names `url`, whatever was routed — what an
    affected Starlette made of a Host header carrying a path."""
    monkeypatch.setattr(Request, "url", property(lambda self: URL(url)))


def test_the_whitelist_is_judged_on_the_routed_path(client, monkeypatch):
    _url_says(monkeypatch, "http://testserver/api/auth/status")
    assert client.get("/api/secret").status_code == 401
    assert client.get("/api/auth/status").status_code == 200


def test_a_prefix_no_route_serves_is_no_whitelist_entry(client):
    assert client.get("/sync/inventory").status_code == 401


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


def test_a_signature_is_taken_once_within_the_window(client):
    headers = signed("POST", "/api/secret", b"{}")
    assert client.post("/api/secret", content=b"{}", headers=headers).status_code == 200
    again = client.post("/api/secret", content=b"{}", headers=headers)
    assert again.status_code == 401 and again.headers["x-sautium-auth-error"] == "replayed"


def test_the_same_request_twice_in_a_second_is_two_requests_with_two_nonces(client):
    ts = str(int(time.time()))
    for _ in range(2):
        assert client.post("/api/secret", content=b"{}",
                           headers=signed("POST", "/api/secret", b"{}", ts=ts)).status_code == 200


def test_a_signature_without_a_nonce_is_taken_once_too(client):
    """A page loaded before its signer drew nonces and declared digests keeps
    working until it reloads, and its signatures are taken once like any."""
    headers = signed("GET", "/api/secret", nonce=None, digest=False)
    assert client.get("/api/secret", headers=headers).status_code == 200
    assert client.get("/api/secret", headers=headers).headers["x-sautium-auth-error"] == "replayed"
    headers = signed("POST", "/api/secret", b"{}", nonce=None, digest=False)
    assert client.post("/api/secret", content=b"{}", headers=headers).status_code == 200


def test_the_nonce_is_covered_by_the_signature(client):
    assert client.get("/api/secret", headers=signed("GET", "/api/secret")).status_code == 200
    headers = signed("GET", "/api/secret")
    headers["X-Sautium-Nonce"] = secrets.token_hex(16)
    r = client.get("/api/secret", headers=headers)
    assert r.status_code == 401 and r.headers["x-sautium-auth-error"] == "bad-sig"
    stripped = {k: v for k, v in signed("GET", "/api/secret").items() if k != "X-Sautium-Nonce"}
    assert client.get("/api/secret", headers=stripped).headers["x-sautium-auth-error"] == "bad-sig"


def test_a_declared_digest_is_64_lowercase_hex_digits(client):
    for bad in ("", "ab", "A" * 64, "0" * 63):
        headers = {**signed("POST", "/api/secret", b"{}"), "X-Sautium-Body-Sha256": bad}
        r = client.post("/api/secret", content=b"{}", headers=headers)
        assert r.status_code == 401 and r.headers["x-sautium-auth-error"] == "bad-digest", bad


def test_a_nonce_is_32_lowercase_hex_digits(client):
    for bad in ("", "abc", "G" * 32, "A" * 32, "0" * 31, "0" * 33):
        r = client.get("/api/secret", headers=signed("GET", "/api/secret", nonce=bad))
        assert r.status_code == 401 and r.headers["x-sautium-auth-error"] == "bad-nonce", bad


def test_a_refused_signature_is_not_remembered(client):
    headers = signed("GET", "/api/secret", key=b"another key")
    assert client.get("/api/secret", headers=headers).headers["x-sautium-auth-error"] == "bad-sig"
    assert not auth_hmac._seen


def _clocks(monkeypatch):
    """The monotonic and the wall clock, both the signer's and the node's,
    moved by hand."""
    clock = {"mono": 1000.0, "wall": float(int(time.time()))}
    monkeypatch.setattr(auth_hmac.time, "monotonic", lambda: clock["mono"])
    monkeypatch.setattr(auth_hmac.time, "time", lambda: clock["wall"])
    return clock


def test_what_is_remembered_is_forgotten_after_two_windows(client, monkeypatch):
    clock = _clocks(monkeypatch)
    for _ in range(3):
        assert client.get("/api/secret", headers=signed("GET", "/api/secret")).status_code == 200
    assert len(auth_hmac._seen) == 3
    clock["mono"] += 2 * auth_hmac.REPLAY_WINDOW_SECONDS
    clock["wall"] += 2 * auth_hmac.REPLAY_WINDOW_SECONDS
    assert client.get("/api/secret", headers=signed("GET", "/api/secret")).status_code == 200
    assert len(auth_hmac._seen) == 1


def test_a_step_back_of_the_wall_clock_reopens_no_signature_still_kept(client, monkeypatch):
    clock = _clocks(monkeypatch)
    headers = signed("GET", "/api/secret")
    assert client.get("/api/secret", headers=headers).status_code == 200
    # the wall clock set back 100 s, then 130 s go by: two windows are over on
    # the monotonic clock, and the request's own time is inside the window again
    clock["mono"] += 130
    clock["wall"] += 30
    assert client.get("/api/secret", headers=headers).headers["x-sautium-auth-error"] == "replayed"


def test_a_signature_the_wall_clock_keeps_holds_back_no_other(client, monkeypatch):
    clock = _clocks(monkeypatch)
    first = signed("GET", "/api/secret")
    assert client.get("/api/secret", headers=first).status_code == 200
    clock["wall"] -= 1000                                       # the wall clock set back
    second = signed("GET", "/api/secret")
    assert client.get("/api/secret", headers=second).status_code == 200
    clock["mono"] += 130
    clock["wall"] += 130
    assert client.get("/api/secret", headers=signed("GET", "/api/secret")).status_code == 200
    # the first is kept until the wall clock is past it again; the second, which
    # it no longer is, is let go however long the first stays
    assert first["X-Sautium-Sig"] in auth_hmac._seen and second["X-Sautium-Sig"] not in auth_hmac._seen


def test_a_signature_or_a_time_of_another_shape_is_refused_as_such(client):
    headers = {**signed("GET", "/api/secret"), "X-Sautium-Sig": "\u00e9" * 64}
    r = client.get("/api/secret", headers={k: v.encode("latin-1") for k, v in headers.items()})
    assert r.status_code == 401 and r.headers["x-sautium-auth-error"] == "bad-sig"
    r = client.get("/api/secret", headers={**signed("GET", "/api/secret"), "X-Sautium-Ts": "1" * 400})
    assert r.status_code == 401 and r.headers["x-sautium-auth-error"] == "bad-ts"


def _verify(headers: dict, key: bytes, method: str, target: str, body: bytes) -> None:
    h = {k.lower(): v for k, v in headers.items()}
    assert auth_hmac.NONCE_FORM.fullmatch(h["x-sautium-nonce"])
    assert h["x-sautium-body-sha256"] == hashlib.sha256(body).hexdigest()
    assert h["x-sautium-sig"] == auth_hmac.sign(key, method, target, h["x-sautium-ts"], body,
                                                h["x-sautium-nonce"])


def test_the_host_signers_sign_with_a_nonce(monkeypatch):
    import importlib.util

    from desktop import api_client
    monkeypatch.setattr(api_client, "_cached_secret", b"host secret")
    first, second = (api_client._sign_headers("POST", "/api/player/next", b"{}") for _ in range(2))
    _verify(first, b"host secret", "POST", "/api/player/next", b"{}")
    assert first["X-Sautium-Nonce"] != second["X-Sautium-Nonce"]
    for name in ("assistant_server", "support_server"):
        spec = importlib.util.spec_from_file_location(f"_signer_{name}", ROOT / "mcp" / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "_API_SECRET", b"host secret")
        _verify(module._signed("GET", "/api/x?q=1", b""), b"host secret", "GET", "/api/x?q=1", b"")


_PAGE_DRIVER = r"""
const fs = require('node:fs'), vm = require('node:vm');
const [shaPath, authPath, origin, mode] = process.argv.slice(-4);
const sent = [];
const store = new Map([['sautium.device_token', 'page token']]);
// mode "replayed": the node answers each first send of a request "replayed"
const answer = () => mode === 'replayed' && sent.length % 2
  ? { status: 401, headers: new Headers({ 'X-Sautium-Auth-Error': 'replayed' }) }
  : { status: 200, headers: new Headers() };
const ctx = {
  window: { fetch: async (input, init) => { sent.push({ url: String(input), method: init.method || 'GET',
                                                        headers: [...new Headers(init.headers)] });
                                            return answer(); },
            addEventListener() {}, dispatchEvent() {} },
  localStorage: { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k) },
  location: { origin, hash: '', pathname: '/', search: '' },
  document: { readyState: 'complete', addEventListener() {}, getElementById: () => null },
  history: { replaceState() {} },
  crypto: globalThis.crypto, Headers, URL, TextEncoder, TextDecoder, console, setTimeout, clearTimeout,
  Request: class extends Request { constructor(input, init) { super(new URL(String(input), origin), init); } },
};
ctx.window.Sautium = {};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(shaPath, 'utf8'), ctx);
vm.runInContext(fs.readFileSync(authPath, 'utf8'), ctx);
(async () => {
  if (mode === 'replayed') {
    await ctx.window.fetch('/api/status');
    await ctx.window.fetch('/api/player/next', { method: 'POST', body: '{}' });
  } else {
    await ctx.window.fetch('/api/item/café?q=a b', { method: 'POST', body: '{"x":1}' });
    await ctx.window.fetch('/api/item/café?q=a b', { method: 'POST', body: '{"x":1}' });
  }
  process.stdout.write(JSON.stringify(sent));
})();
"""


def _page_sends(mode: str) -> list:
    static = ROOT / "backend" / "static"
    proc = subprocess.run(["node", "-e", _PAGE_DRIVER, "--", str(static / "sha256.js"),
                           str(static / "auth.js"), "http://node.test:8800", mode],
                          capture_output=True, check=True, timeout=30)
    return json.loads(proc.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_page_signs_the_target_as_it_sends_it_with_a_nonce_each_time():
    sent = _page_sends("plain")
    assert len(sent) == 2
    for request in sent:
        _verify(dict(request["headers"]), b"page token", "POST", "/api/item/caf%C3%A9?q=a%20b", b'{"x":1}')
    nonces = {dict(r["headers"])["x-sautium-nonce"] for r in sent}
    assert len(nonces) == 2


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_page_signs_a_read_the_node_called_replayed_again_and_never_sends_a_change_twice():
    sent = _page_sends("replayed")
    assert [r["method"] for r in sent] == ["GET", "GET", "POST"]
    first, again = (dict(r["headers"]) for r in sent[:2])
    assert first["x-sautium-nonce"] != again["x-sautium-nonce"]
    _verify(again, b"page token", "GET", "/api/status", b"")
