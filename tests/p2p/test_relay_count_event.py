"""The Sync screen's "Relaying for N nodes" row hears its number move
(backend/routers/peer_chat.py, a Docker node's relay registry).

The count had no event of its own: the row changed at whatever woke the page
next — every two seconds under any activity while the load meter spoke on
sautium_identity, and at nothing once that ended (PI-NODE-2D, B-138). A
voucher's stream that comes or goes now wakes the readers of the node's
network state, when the number the row shows moved and only then. The relay's
own route is driven as the peer surface drives it.
"""

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parent.parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from desktop.p2p.chat_wire import voucher_payload  # noqa: E402
from desktop.p2p.wake_registry import WakeRegistry  # noqa: E402
from tests.p2p.test_chat_binding import FRIEND, HOST, STRANGER, _Svc, _friend  # noqa: E402

peer_chat = pytest.importorskip("routers.peer_chat")
settings_router = pytest.importorskip("routers.settings")


@pytest.fixture
def relay(monkeypatch):
    """A relay with its switch on and room, whose gate knows every caller;
    `relay.woken` counts the wakes of the identity subscribers, `relay.trail`
    those of the guidance trail, `relay.library` those of the Library stream."""
    svc = _Svc()
    svc.friend_for_key = lambda pk: _friend(FRIEND) if pk == FRIEND.pub else None
    monkeypatch.setattr(peer_chat, "get_peer_chat", lambda: svc)
    monkeypatch.setattr(peer_chat, "resolve_identity", lambda s: {
        "public_key_hex": HOST.pub, "username": HOST.name, "invite_code": HOST.invite})
    monkeypatch.setattr(peer_chat, "_wake", WakeRegistry())
    monkeypatch.setattr(peer_chat, "_relay_clients", {})
    monkeypatch.setattr(peer_chat, "_relay_count_said", 0)
    monkeypatch.setattr(peer_chat, "_subscribe_hook", None)
    monkeypatch.setattr(peer_chat, "_client_announce_cb", None)
    monkeypatch.setattr(peer_chat, "_client_withdraw_cb", None)
    monkeypatch.setattr(settings_router, "_read", lambda key: key == "p2p.relay_enabled")

    class _Gate:
        async def admit(self, pk, cert, proof, addr):
            return SimpleNamespace(status="verified")
    monkeypatch.setattr(peer_chat, "_identity_gate", lambda: _Gate())
    from desktop.p2p import peer_auth
    monkeypatch.setattr(peer_auth, "decode_cert_bundle", lambda header: {"cert": {}, "proof": None})

    out = SimpleNamespace(woken=0, trail=0, library=0)
    monkeypatch.setattr(settings_router, "notify_identity_subscribers",
                        lambda: setattr(out, "woken", out.woken + 1))
    monkeypatch.setattr(settings_router._guidance, "moved", lambda: setattr(out, "trail", out.trail + 1))
    monkeypatch.setattr(settings_router, "notify_library_subscribers",
                        lambda: setattr(out, "library", out.library + 1))
    return out


async def _stream(who, instance: str, *, voucher: bool = True):
    """`who`'s wake stream on this relay, open: its first frame read."""
    ts, until = int(time.time()), int(time.time()) + 3600
    request = SimpleNamespace(client=SimpleNamespace(host="198.51.100.8"), headers={})
    response = await peer_chat.wake_stream(
        request, pubkey=who.pub, ts=str(ts), instance=instance,
        sig=who.key.sign(f"wake_subscribe:{ts}:{HOST.pub}:{who.pub}".encode()).hex(),
        invite=who.invite if voucher else "", voucher_until=str(until) if voucher else "",
        voucher_sig=(who.key.sign(voucher_payload(who.pub, HOST.pub, until).encode()).hex()
                     if voucher else ""))
    assert getattr(response, "status_code", 200) == 200, getattr(response, "body", b"")
    frames = response.body_iterator
    assert await asyncio.wait_for(frames.__anext__(), 5) == ": connected\n\n"
    return frames


def test_the_row_is_told_when_the_number_it_shows_moves_and_only_then(relay):
    async def scenario():
        said = []
        first = await _stream(STRANGER, "a" * 16)
        said.append(("a relayed node came", peer_chat.relay_client_count(), relay.woken))
        # the same node's next stream over its last (a reconnect, a second machine of the account)
        second = await _stream(STRANGER, "b" * 16)
        said.append(("its second stream", peer_chat.relay_client_count(), relay.woken))
        # a friend's bare subscription is its chat stream: no slot, no number
        friend = await _stream(FRIEND, "c" * 16, voucher=False)
        said.append(("a friend's chat stream", peer_chat.relay_client_count(), relay.woken))
        await first.aclose()                                # one of its two streams leaves
        said.append(("one of two left", peer_chat.relay_client_count(), relay.woken))
        await friend.aclose()
        said.append(("the friend left", peer_chat.relay_client_count(), relay.woken))
        await second.aclose()                               # the node is gone
        said.append(("the node left", peer_chat.relay_client_count(), relay.woken))
        return said
    assert asyncio.run(scenario()) == [
        ("a relayed node came", 1, 1),
        ("its second stream", 1, 1),
        ("a friend's chat stream", 1, 1),
        ("one of two left", 1, 1),
        ("the friend left", 1, 1),
        ("the node left", 0, 2),
    ]
    # the node's network state, as the Sync screen's other rows: no Library wake, no trail computed
    assert (relay.library, relay.trail) == (0, 0)


def test_the_switch_going_off_ends_the_slots_and_says_the_number(relay):
    async def scenario():
        one = await _stream(STRANGER, "a" * 16)
        two = await _stream(FRIEND, "b" * 16)               # a friend that asked to be relayed for
        assert (peer_chat.relay_client_count(), relay.woken) == (2, 2)
        peer_chat.close_relay_clients()
        for frames in (one, two):                           # each stream ends where it waited
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(frames.__anext__(), 5)
        return peer_chat.relay_client_count(), relay.woken
    count, woken = asyncio.run(scenario())
    assert count == 0 and woken >= 3                        # 2 → 0, by one stream's end or two
