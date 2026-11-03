"""What a request may weigh on the Web UI port before it is judged: a body is
read whole before the app runs, within a size, a deadline and a budget the
bodies in flight share (serving.BodyBudget) — and the port's door
(auth_hmac.HMACAuthMiddleware) decides all its headers decide before a byte
of a body is read, so the large budget is a key holder's alone. Driven with
raw ASGI events: a test client always sends a whole body at once."""

import asyncio
import collections
import hashlib
import secrets
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

import auth_hmac  # noqa: E402
import device_auth  # noqa: E402
from serving import BodyBudget, replaying  # noqa: E402

TOKEN = "d" * 64


class App:
    """What is behind the bounds: records what reached it, and can hold a
    request open until released."""

    def __init__(self):
        self.calls = []
        self.release = None

    async def __call__(self, scope, receive, send):
        first = await receive()
        self.calls.append(first)
        if self.release is not None:
            await self.release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _gate(budget, app):
    """The smallest gate over a budget: the body read whole, then the app."""
    async def gate(scope, receive, send):
        async with budget.reading(scope, receive, send) as body:
            if body is not None:
                await app(scope, replaying(body, receive), send)
    return gate


def _scope(length=None, path="/api/x", headers=()):
    head = [(b"host", b"localhost"), *headers]
    if length is not None:
        head.append((b"content-length", str(length).encode()))
    return {"type": "http", "method": "POST", "path": path, "raw_path": path.encode(),
            "query_string": b"", "headers": head}


def _feed(*chunks, gap=0.0, then_disconnect=False):
    """A receive that hands `chunks` out one per call, `gap` seconds apart."""
    queue = list(chunks)
    asked = []

    async def receive():
        asked.append(1)
        if gap:
            await asyncio.sleep(gap)
        if queue:
            chunk = queue.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(queue) or then_disconnect}
        return {"type": "http.disconnect"}
    receive.asked = asked
    return receive


def _run(asgi, scope, receive):
    sent = []

    async def send(message):
        sent.append(message)
    asyncio.run(asgi(scope, receive, send))
    return sent


def _status(sent):
    starts = [m for m in sent if m["type"] == "http.response.start"]
    return starts[0]["status"] if starts else None


def _auth_error(sent):
    start = next(m for m in sent if m["type"] == "http.response.start")
    return dict(start["headers"]).get(b"x-sautium-auth-error", b"").decode()


# -- the budget ---------------------------------------------------------------

def test_a_declared_length_over_the_cap_is_refused_before_a_byte_is_read():
    app, receive = App(), _feed(b"x" * 10)
    sent = _run(_gate(BodyBudget(cap=100, budget=1000, deadline=5), app), _scope(length=101), receive)
    assert _status(sent) == 413 and not receive.asked and not app.calls


def test_a_body_that_grows_past_the_cap_is_refused_there():
    app, receive = App(), _feed(b"x" * 60, b"x" * 60, b"x" * 60)
    sent = _run(_gate(BodyBudget(cap=100, budget=1000, deadline=5), app), _scope(), receive)
    assert _status(sent) == 413 and len(receive.asked) == 2 and not app.calls


def test_a_body_still_arriving_at_the_deadline_is_refused():
    app = App()
    sent = _run(_gate(BodyBudget(cap=100, budget=1000, deadline=0.2), app), _scope(),
                _feed(b"a", b"b", b"c", b"d", gap=0.1))
    assert _status(sent) == 408 and not app.calls


def test_a_body_within_bounds_reaches_the_app_whole_and_then_the_clients_own_messages():
    seen = []

    async def app(scope, receive, send):
        seen.append(await receive())
        seen.append(await receive())
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})
    budget = BodyBudget(cap=100, budget=1000, deadline=5)
    sent = _run(_gate(budget, app), _scope(), _feed(b"ab", b"cd", b"ef"))
    assert _status(sent) == 204
    assert seen == [{"type": "http.request", "body": b"abcdef", "more_body": False},
                    {"type": "http.disconnect"}]
    assert budget.held == 0


def test_a_client_that_leaves_mid_body_is_answered_400_and_the_app_never_runs():
    app, budget = App(), BodyBudget(cap=100, budget=1000, deadline=5)
    sent = _run(_gate(budget, app), _scope(), _feed(b"ab", then_disconnect=True))
    assert _status(sent) == 400 and not app.calls and budget.held == 0


def test_the_bodies_in_flight_share_the_budget():
    async def scenario():
        app = App()
        app.release = asyncio.Event()
        budget = BodyBudget(cap=100, budget=150, deadline=5)
        gate = _gate(budget, app)
        sent_a, sent_b, sent_c = [], [], []

        def send_to(box):
            async def send(message):
                box.append(message)
            return send
        held = asyncio.ensure_future(gate(_scope(), _feed(b"x" * 100), send_to(sent_a)))
        while not app.calls:
            await asyncio.sleep(0.01)
        assert budget.held == 100                         # the first body is held while its request runs
        await gate(_scope(), _feed(b"y" * 60), send_to(sent_b))
        app.release.set()
        await held
        assert budget.held == 0
        await gate(_scope(), _feed(b"z" * 60), send_to(sent_c))
        return _status(sent_a), _status(sent_b), _status(sent_c)
    assert asyncio.run(scenario()) == (200, 503, 200)


# -- the door -----------------------------------------------------------------

@pytest.fixture
def door(tmp_path, monkeypatch):
    """The Web UI port's door over App, with small budgets, this test's Host
    as the node's and a fixed device token."""
    monkeypatch.setattr(device_auth, "current_token", lambda secret: TOKEN)
    monkeypatch.setattr(auth_hmac, "_allowed_hosts", {"localhost"})
    monkeypatch.setattr(auth_hmac, "_seen", set(), raising=False)
    monkeypatch.setattr(auth_hmac, "_seen_order", collections.deque(), raising=False)
    app = App()
    return app, auth_hmac.HMACAuthMiddleware(
        app, secret_path=tmp_path / ".api_secret",
        signed_bodies={"cap": 1000, "budget": 1500, "deadline": 5},
        open_bodies={"cap": 100, "budget": 150, "deadline": 5})


def _signed_headers(body: bytes, *, digest=True, key: bytes = TOKEN.encode(), path="/api/x"):
    ts, nonce = str(int(time.time())), secrets.token_hex(16)
    head = [(b"x-sautium-ts", ts.encode()), (b"x-sautium-nonce", nonce.encode()),
            (b"x-sautium-sig", auth_hmac.sign(key, "POST", path, ts, body, nonce).encode())]
    if digest:
        head.append((b"x-sautium-body-sha256", hashlib.sha256(body).hexdigest().encode()))
    return head


def test_a_request_without_a_signature_is_refused_before_a_byte_of_its_body_is_read(door):
    app, gate = door
    receive = _feed(b"x" * 500)
    sent = _run(gate, _scope(length=500), receive)
    assert _status(sent) == 401 and _auth_error(sent) == "missing"
    assert not receive.asked and not app.calls


def test_a_signature_is_judged_over_the_declared_digest_before_a_byte_is_read(door):
    app, gate = door
    body = b"x" * 500
    receive = _feed(body)
    sent = _run(gate, _scope(length=500, headers=_signed_headers(body, key=b"another key")), receive)
    assert _status(sent) == 401 and _auth_error(sent) == "bad-sig"
    assert not receive.asked and not app.calls
    assert _status(_run(gate, _scope(length=500, headers=_signed_headers(body)), _feed(body))) == 200
    assert app.calls == [{"type": "http.request", "body": body, "more_body": False}]


def test_a_body_that_is_not_the_declared_one_is_refused(door):
    app, gate = door
    sent = _run(gate, _scope(length=3, headers=_signed_headers(b"abc")), _feed(b"abd"))
    assert _status(sent) == 401 and _auth_error(sent) == "bad-sig" and not app.calls


def test_an_unknown_host_is_refused_before_a_byte_is_read(door):
    app, gate = door
    receive = _feed(b"x" * 50)
    scope = _scope(length=50, headers=_signed_headers(b"x" * 50))
    scope["headers"][0] = (b"host", b"elsewhere.example")
    assert _status(_run(gate, scope, receive)) == 421 and not receive.asked and not app.calls


def test_a_whitelisted_route_and_a_signer_without_a_digest_read_from_the_small_budget(door):
    app, gate = door
    for path in ("/api/auth/login", "/api/csp-report"):
        assert _status(_run(gate, _scope(length=101, path=path), _feed(b"x" * 101))) == 413
        assert _status(_run(gate, _scope(length=10, path=path), _feed(b"x" * 10))) == 200
    # a page from before declared digests: its body judged once read, and small
    for size, status in ((101, 413), (99, 200)):
        body = b"x" * size
        assert _status(_run(gate, _scope(length=size, headers=_signed_headers(body, digest=False)),
                            _feed(body))) == status


def _stall(first: bytes):
    """A receive that hands `first` out, then waits for as long as it is let."""
    handed = []

    async def receive():
        if not handed:
            handed.append(1)
            return {"type": "http.request", "body": first, "more_body": True}
        await asyncio.Event().wait()
    return receive


def test_the_large_budget_is_a_key_holders_alone(door):
    """Bodies nobody signed, held open as long as they like, fill the small
    budget and leave a signer's body the large one."""
    app, gate = door

    async def scenario():
        boxes = [[] for _ in range(5)]

        def send_to(box):
            async def send(message):
                box.append(message)
            return send
        stalled = [asyncio.ensure_future(gate(_scope(length=100, path="/api/auth/login"),
                                              _stall(b"x" * 50), send_to(boxes[i])))
                   for i in range(3)]
        await asyncio.sleep(0.05)
        await gate(_scope(length=10, path="/api/auth/login"), _feed(b"x" * 10), send_to(boxes[3]))
        body = b"y" * 900
        await gate(_scope(length=900, headers=_signed_headers(body)), _feed(body), send_to(boxes[4]))
        for s in stalled:
            s.cancel()
        await asyncio.gather(*stalled, return_exceptions=True)
        return _status(boxes[3]), _status(boxes[4])
    assert asyncio.run(scenario()) == (503, 200)


def test_what_is_not_http_passes_through(tmp_path):
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])
    asyncio.run(auth_hmac.HMACAuthMiddleware(app, secret_path=tmp_path / ".api_secret")(
        {"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


def test_the_web_ui_judges_a_request_before_it_reads_its_body(monkeypatch):
    """The whole app: the door is its outermost middleware, so a request
    without a signature is refused, whatever its body, before anything of
    the app reads it."""
    from fastapi.testclient import TestClient
    monkeypatch.chdir(ROOT / "backend")            # main mounts static/ relative to its directory
    main = pytest.importorskip("main")
    assert main.app.user_middleware[0].cls is auth_hmac.HMACAuthMiddleware
    big = b"x" * (12 * 1024 * 1024 + 1)
    r = TestClient(main.app, base_url="http://localhost").post("/api/player/next", content=big)
    assert r.status_code == 401 and r.headers["x-sautium-auth-error"] == "missing"
