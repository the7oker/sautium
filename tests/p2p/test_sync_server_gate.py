"""The launcher's peer gate (desktop/p2p/sync_server.py, _inbound_middleware)
judges a request by the route aiohttp matched — the path the handler is
reached by — never by the raw request-target, which another spelling of the
same path makes differ from it."""

import asyncio

from aiohttp import web
from aiohttp.test_utils import TestServer

from desktop.p2p.sync_server import SyncServer


class _Log:
    def __init__(self):
        self.endpoints = []

    def record(self, *, endpoint, **_):
        self.endpoints.append(endpoint)


class _Pricer:
    def __init__(self):
        self.actions = []

    def would_be(self, client_pubkey, action, lane):
        self.actions.append(action)
        return 0

    def mode(self):
        return "shadow"


async def _lane(request):
    return web.json_response({"lane": request.get("lane")})


async def _exchange(raw_target: str):
    """One request with exactly this request-target, through the gate, to a
    handler that reports the lane the gate gave it."""
    server = SyncServer("postgresql://unused")       # no account: the anonymous lane
    log, pricer = _Log(), _Pricer()
    server._contact_log = log
    server._price = lambda: pricer
    app = web.Application(middlewares=[server._inbound_middleware])
    app.router.add_get("/api/sync/holdings", _lane)
    app.router.add_get("/health", _lane)
    async with TestServer(app) as ts:
        reader, writer = await asyncio.open_connection(ts.host, ts.port)
        writer.write(f"GET {raw_target} HTTP/1.1\r\nHost: peer\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        reply = (await reader.read()).decode()
        writer.close()
    status = int(reply.split(" ", 2)[1])
    return status, reply.rsplit("\r\n\r\n", 1)[1], log.endpoints, pricer.actions


def test_a_percent_encoded_target_is_gated_as_the_route_it_reaches():
    status, body, endpoints, actions = asyncio.run(_exchange("/api/%73ync/holdings"))
    assert status == 200
    assert '"lane": "anonymous"' in body                  # the gate ran and set the lane
    assert actions == ["sync.holdings"]                   # priced as the route it reached
    assert endpoints == ["sync.holdings"]


def test_an_absolute_form_target_is_gated_as_the_route_it_reaches():
    status, body, endpoints, actions = asyncio.run(_exchange("http://peer/api/sync/holdings"))
    assert status == 200
    assert '"lane": "anonymous"' in body
    assert actions == ["sync.holdings"]


def test_an_unbound_route_and_an_unknown_target_pass_ungated():
    status, body, endpoints, actions = asyncio.run(_exchange("/health"))
    assert status == 200 and '"lane": null' in body and actions == []
    assert endpoints == ["health"]
    status, _, endpoints, actions = asyncio.run(_exchange("/api/sync/nowhere"))
    assert status == 404 and actions == [] and endpoints == ["other"]
