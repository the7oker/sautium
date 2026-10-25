"""The backend's event streams end as its server's stop begins
(backend/serving.py, EventStream and Server.shutdown).

The Web UI's streams never end by themselves. uvicorn waited out its whole
graceful window on them before the lifespan's shutdown, then cancelled them
— a traceback for each open tab in backend.log, and five of the ten seconds
the launcher gives a stop (2026-10-25). Here a real serving.Server serves an
app shaped as the backend's — a BaseHTTPMiddleware in front, as the HMAC
check is — and its streams wait as the backend's do, on an event with a
keepalive timeout. The grace is long, so a stop that waited on a stream
would show; the stop must not.
"""

import asyncio
import logging
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware

import serving

GRACE_S = 30


class Passing(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        return await call_next(request)


def _app(state: dict) -> FastAPI:
    app = FastAPI()
    app.add_middleware(Passing)

    def stream(name: str):
        async def gen():
            evt = asyncio.Event()
            try:
                yield "data: {}\n\n"
                while True:
                    try:
                        await asyncio.wait_for(evt.wait(), timeout=20.0)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                state.setdefault("closed", []).append(name)
        return serving.EventStream(gen())

    @app.get("/events")
    async def events():
        return stream("events")

    @app.get("/flood")
    async def flood():
        async def gen():
            try:
                while True:
                    yield "data: " + "x" * 65536 + "\n\n"
            finally:
                state.setdefault("closed", []).append("flood")
        # Held, so no collection of it closes it in the stream's place
        state["flood"] = gen()
        return serving.EventStream(state["flood"])

    @app.get("/late")
    async def late():
        # Still on its way to the stream when the stop begins
        state["late_asked"].set()
        await state["late_go"].wait()
        return stream("late")

    return app


async def _open(port: int, path: str):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: test\r\n\r\n".encode())
    await writer.drain()
    return reader, writer


async def _rest(reader) -> bytes:
    """What the stream sends until it ends — in far less than the grace."""
    return await asyncio.wait_for(reader.read(), timeout=5)


async def _until(cond, within: float = 5) -> None:
    deadline = time.monotonic() + within
    while not cond():
        assert time.monotonic() < deadline, "it never came"
        await asyncio.sleep(0.01)


@pytest.fixture
def served():
    """A serving.Server on a loopback port: (run, state). `run(scenario)`
    starts it, runs `scenario(port, server)` and returns what the scenario
    returns with the seconds the stop took."""
    state: dict = {}

    def run(scenario):
        async def main():
            state["late_asked"], state["late_go"] = asyncio.Event(), asyncio.Event()
            server = serving.Server(uvicorn.Config(
                _app(state), host="127.0.0.1", port=0, log_config=None, lifespan="off",
                timeout_graceful_shutdown=GRACE_S))
            serve = asyncio.create_task(server.serve())
            await _until(lambda: server.started)
            port = server.servers[0].sockets[0].getsockname()[1]
            outcome = await scenario(port, server)
            await serve
            return outcome
        return asyncio.run(main())
    return run, state


def test_an_open_stream_ends_complete_as_the_stop_begins(served, caplog):
    run, state = served

    async def scenario(port, server):
        streams = [await _open(port, "/events") for _ in range(2)]
        for reader, _ in streams:
            head = await reader.readuntil(b"data: {}\n\n")
            assert head.startswith(b"HTTP/1.1 200")
        began = time.monotonic()
        server.should_exit = True
        rests = [await _rest(reader) for reader, _ in streams]
        for _, writer in streams:
            writer.close()
        return began, rests

    with caplog.at_level(logging.INFO):
        began, rests = run(scenario)
    took = time.monotonic() - began
    # Each a completed chunked response, ended in order, its generator closed
    assert [rest.endswith(b"0\r\n\r\n") for rest in rests] == [True, True]
    assert state["closed"] == ["events", "events"]
    assert took < 2, took
    said = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert said == [], said


def test_a_stop_that_meets_a_stream_mid_send_closes_its_generator(served):
    """A generator that never waits but in its yield: every cancel lands in
    the stream's send, the generator left at its yield. The stream's end
    closes it — its cleanup runs before the response ends, not whenever,
    if ever, the generator is collected."""
    run, state = served

    async def scenario(port, server):
        reader, writer = await _open(port, "/flood")
        await reader.readuntil(b"data: ")
        server.should_exit = True
        await _until(lambda: "flood" in state.get("closed", []))
        rest = await _rest(reader)
        writer.close()
        return rest

    assert run(scenario).endswith(b"0\r\n\r\n")


def test_a_stream_that_starts_once_the_stop_began_ends_at_once(served, caplog):
    run, state = served

    async def scenario(port, server):
        loop = asyncio.get_running_loop()
        reader, writer = await _open(port, "/late")
        await state["late_asked"].wait()
        began = time.monotonic()
        server.should_exit = True
        await _until(lambda: loop in serving._stopping)    # the stop began on this loop
        state["late_go"].set()
        rest = await _rest(reader)
        writer.close()
        return began, rest

    with caplog.at_level(logging.INFO):
        began, rest = run(scenario)
    assert rest.startswith(b"HTTP/1.1 200") and rest.endswith(b"0\r\n\r\n"), rest
    assert state["closed"] == ["late"]
    assert time.monotonic() - began < 2
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


# -- A chat generation's stream (routers/chat.py, _run_event_stream) ----------
# Its subscriber was a queue.Queue its stream waited on in a worker thread, for
# up to the keepalive's 15 s: a stop's cancel waited for that wait to end.


async def _drain(stream, got: list) -> None:
    async for item in stream:
        got.append(item)


def test_a_chat_stream_replays_follows_its_run_and_ends_with_it():
    from routers import chat
    run = chat._LiveRun()
    run.publish("meta")

    async def main():
        got = []
        task = asyncio.create_task(_drain(chat._run_event_stream(run), got))
        await _until(lambda: run._subscribers)
        producer = threading.Thread(target=lambda: (run.publish("delta"), run.close()))
        producer.start()
        await asyncio.wait_for(task, 5)
        producer.join()
        return got

    assert asyncio.run(main()) == ["meta", "delta"]
    assert run._subscribers == []


def test_a_chat_stream_keeps_its_connection_alive_while_its_run_is_quiet(monkeypatch):
    from routers import chat
    monkeypatch.setattr(chat, "KEEPALIVE_INTERVAL_SEC", 0.05)
    run = chat._LiveRun()

    async def main():
        stream = chat._run_event_stream(run)
        first = await asyncio.wait_for(stream.__anext__(), 5)
        await stream.aclose()
        return first

    assert asyncio.run(main()) == ": keepalive\n\n"
    assert run._subscribers == []


def test_a_chat_stream_waiting_on_its_run_ends_at_once_when_cancelled():
    from routers import chat
    run = chat._LiveRun()

    async def main():
        task = asyncio.create_task(_drain(chat._run_event_stream(run), []))
        await _until(lambda: run._subscribers)
        began = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return time.monotonic() - began

    assert asyncio.run(main()) < 0.5
    assert run._subscribers == []
