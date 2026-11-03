"""The GENA callback server (dlna_backend._ensure_notify): an event whose
subscription is not known is kept only while a SUBSCRIBE of the node's is in
flight or just answered, so many and so long, and played to its subscription
when the answer names it; any other is refused. Its body is bounded."""

import asyncio
import socket
import sys
from pathlib import Path

import pytest
from aiohttp import ClientSession

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from config import settings  # noqa: E402
from playback import dlna_backend  # noqa: E402

pytestmark = pytest.mark.skipif(not dlna_backend.HAS_UPNP, reason="async-upnp-client not installed")

EVENT = ('<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0"><e:property>'
         '<LastChange>x</LastChange></e:property></e:propertyset>')


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def notify(monkeypatch):
    """The process's notify server, on a free port of its own, gone after."""
    monkeypatch.setattr(settings, "dlna_gena_port", _free_port())
    monkeypatch.setattr(settings, "media_proxy_advertised_host", "127.0.0.1")
    monkeypatch.setattr(dlna_backend, "_shared_notify", None)
    monkeypatch.setattr(dlna_backend, "_shared_notify_host", None)
    return settings.dlna_gena_port


async def _post(port: int, sid: str, body: str = EVENT) -> int:
    async with ClientSession() as session:
        async with session.request("NOTIFY", f"http://127.0.0.1:{port}/notify", data=body, headers={
                "NT": "upnp:event", "NTS": "upnp:propchange", "SID": sid, "SEQ": "0",
                "Content-Type": 'text/xml; charset="utf-8"'}) as r:
            return r.status


def _kept(handler) -> int:
    return len(getattr(handler, "_backlog", {})) + len(getattr(handler, "_early", {}))


def test_an_event_no_subscription_expects_is_refused_and_not_kept(notify):
    async def scenario():
        server = await dlna_backend._ensure_notify(dlna_backend.AiohttpRequester(), None)
        try:
            statuses = [await _post(notify, f"uuid:nobody-{i}") for i in range(40)]
            return statuses, _kept(server.event_handler)
        finally:
            await server.async_stop_server()
    statuses, kept = asyncio.run(scenario())
    assert set(statuses) == {412} and kept == 0


class _Service:
    """What a subscription is to the handler: an event URL, and the state
    variables it is told."""
    event_sub_url = "http://127.0.0.1:9/event"

    def __init__(self):
        self.changes = []

    def notify_changed_state_variables(self, changes):
        self.changes.append(changes)


def _subscribe_meeting(notify, strangers: int, wait: float = 0.0):
    """A SUBSCRIBE whose renderer sends its first event before it answers —
    and `strangers` events of other SIDs beside it — then, `wait` seconds
    later, answers with its SID: (the SID, what the subscription was told,
    the statuses, what is kept)."""
    from async_upnp_client.const import HttpResponse

    async def scenario():
        early_sid = "uuid:the-renderer-1"
        server = await dlna_backend._ensure_notify(dlna_backend.AiohttpRequester(), None)
        handler = server.event_handler

        class Renderer:
            async def async_http_request(self, request):
                statuses = [await _post(notify, early_sid)]
                statuses += [await _post(notify, f"uuid:stranger-{i}") for i in range(strangers)]
                Renderer.statuses = statuses
                await asyncio.sleep(wait)
                return HttpResponse(200, {"sid": early_sid, "timeout": "Second-1800"}, "")
        handler._requester = Renderer()
        service = _Service()
        try:
            sid, _ = await handler.async_subscribe(service)
            return sid, service.changes, Renderer.statuses, _kept(handler)
        finally:
            handler._requester = dlna_backend.AiohttpRequester()
            await server.async_stop_server()
            dlna_backend._shared_notify = None
    return asyncio.run(scenario())


def test_an_early_event_reaches_its_subscription_and_the_early_ones_are_bounded(notify):
    sid, changes, statuses, kept = _subscribe_meeting(notify, strangers=5)
    assert sid == "uuid:the-renderer-1"
    assert changes == [{"LastChange": "x"}]
    assert set(statuses) == {200} and kept == 5
    _, _, statuses, kept = _subscribe_meeting(notify, strangers=3 * dlna_backend._EARLY_EVENTS)
    assert set(statuses) == {200} and kept == dlna_backend._EARLY_EVENTS


def test_a_notify_body_is_bounded(notify):
    async def scenario():
        server = await dlna_backend._ensure_notify(dlna_backend.AiohttpRequester(), None)
        try:
            return await _post(notify, "uuid:nobody", "x" * (dlna_backend._NOTIFY_BODY_MAX + 1))
        finally:
            await server.async_stop_server()
    assert asyncio.run(scenario()) == 413


def test_an_early_event_older_than_its_window_is_not_played(notify, monkeypatch):
    monkeypatch.setattr(dlna_backend, "_EARLY_EVENT_SECONDS", 0.2)
    sid, changes, statuses, _kept_now = _subscribe_meeting(notify, strangers=0, wait=0.4)
    assert sid == "uuid:the-renderer-1" and statuses == [200] and changes == []


def test_an_event_without_a_sid_is_refused_and_kept_nowhere(notify):
    async def scenario():
        server = await dlna_backend._ensure_notify(dlna_backend.AiohttpRequester(), None)
        try:
            return await _post(notify, ""), _kept(server.event_handler)
        finally:
            await server.async_stop_server()
    assert asyncio.run(scenario()) == (412, 0)
