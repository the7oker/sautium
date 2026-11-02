"""serving.BodyLimit — the ceiling ahead of everything on the Web UI port: a
body is read whole before the app runs, within a size, a deadline and a
budget the bodies in flight share. Driven with raw ASGI events: a test client
always sends a whole body at once."""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from serving import BodyLimit  # noqa: E402


class App:
    """The app behind the limit: records what reached it, and can hold a
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


def _scope(length=None):
    headers = [(b"host", b"node")]
    if length is not None:
        headers.append((b"content-length", str(length).encode()))
    return {"type": "http", "method": "POST", "path": "/api/x", "headers": headers}


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


def _run(limit, scope, receive):
    sent = []

    async def send(message):
        sent.append(message)
    asyncio.run(limit(scope, receive, send))
    return sent


def _status(sent):
    starts = [m for m in sent if m["type"] == "http.response.start"]
    return starts[0]["status"] if starts else None


def test_a_declared_length_over_the_cap_is_refused_before_a_byte_is_read():
    app = App()
    receive = _feed(b"x" * 10)
    sent = _run(BodyLimit(app, cap=100, budget=1000, deadline=5), _scope(length=101), receive)
    assert _status(sent) == 413 and not receive.asked and not app.calls


def test_a_body_that_grows_past_the_cap_is_refused_there():
    app = App()
    receive = _feed(b"x" * 60, b"x" * 60, b"x" * 60)
    sent = _run(BodyLimit(app, cap=100, budget=1000, deadline=5), _scope(), receive)
    assert _status(sent) == 413 and len(receive.asked) == 2 and not app.calls


def test_a_body_still_arriving_at_the_deadline_is_refused():
    app = App()
    sent = _run(BodyLimit(app, cap=100, budget=1000, deadline=0.2), _scope(),
                _feed(b"a", b"b", b"c", b"d", gap=0.1))
    assert _status(sent) == 408 and not app.calls


def test_a_body_within_bounds_reaches_the_app_whole_and_then_the_clients_own_messages():
    seen = []

    async def app(scope, receive, send):
        seen.append(await receive())
        seen.append(await receive())
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})
    limit = BodyLimit(app, cap=100, budget=1000, deadline=5)
    sent = _run(limit, _scope(), _feed(b"ab", b"cd", b"ef"))
    assert _status(sent) == 204
    assert seen == [{"type": "http.request", "body": b"abcdef", "more_body": False},
                    {"type": "http.disconnect"}]
    assert limit.held == 0


def test_a_client_that_leaves_mid_body_is_answered_400_and_the_app_never_runs():
    app = App()
    sent = _run(BodyLimit(app, cap=100, budget=1000, deadline=5), _scope(),
                _feed(b"ab", then_disconnect=True))
    assert _status(sent) == 400 and not app.calls


def test_the_bodies_in_flight_share_the_budget():
    async def scenario():
        app = App()
        app.release = asyncio.Event()
        limit = BodyLimit(app, cap=100, budget=150, deadline=5)
        sent_a, sent_b, sent_c = [], [], []

        def send_to(box):
            async def send(message):
                box.append(message)
            return send
        held = asyncio.ensure_future(limit(_scope(), _feed(b"x" * 100), send_to(sent_a)))
        while not app.calls:
            await asyncio.sleep(0.01)
        assert limit.held == 100                          # the first body is held while its request runs
        await limit(_scope(), _feed(b"y" * 60), send_to(sent_b))
        app.release.set()
        await held
        assert limit.held == 0
        await limit(_scope(), _feed(b"z" * 60), send_to(sent_c))
        return _status(sent_a), _status(sent_b), _status(sent_c)
    assert asyncio.run(scenario()) == (200, 503, 200)


def test_what_is_not_http_passes_through():
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])
    asyncio.run(BodyLimit(app, cap=1, budget=1, deadline=1)({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


def test_the_web_ui_reads_no_body_past_the_limit(monkeypatch):
    """The whole app: the limit is its outermost middleware, so a body over
    the cap is refused before the signature check reads it."""
    import pytest
    from fastapi.testclient import TestClient
    monkeypatch.chdir(ROOT / "backend")            # main mounts static/ relative to its directory
    main = pytest.importorskip("main")
    assert main.app.user_middleware[0].cls is BodyLimit
    big = b"x" * (12 * 1024 * 1024 + 1)
    r = TestClient(main.app, base_url="http://localhost").post("/api/player/next", content=big)
    assert r.status_code == 413
