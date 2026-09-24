"""The wake-stream registry both relay surfaces share
(desktop/p2p/wake_registry.py): one stream per NODE of a key, so two
machines signed into one account stop taking each other's stream."""

import asyncio

from desktop.p2p import wake_registry
from desktop.p2p.wake_registry import WakeRegistry, valid_instance

KEY = "ab" * 32


def test_two_nodes_of_one_account_both_hold_a_stream():
    async def go():
        reg = WakeRegistry()
        stand = reg.register(KEY, "a1", "198.51.100.1")
        mac = reg.register(KEY, "b2", "198.51.100.1")
        assert not stand.closed and not mac.closed

        reg.ping(KEY, "message")
        assert reg.push_frame(KEY, {"type": "diag_warrant"})
        assert reg.queue_envelope(KEY, {"message_uuid": "m1"}) is None
        await asyncio.sleep(0)
        for sub in (stand, mac):
            assert sub.evt.is_set()
            assert reg.drain(sub) == ([{"type": "diag_warrant"}],
                                      [{"message_uuid": "m1"}], ["message"])
            assert reg.drain(sub) == ([], [], [])
    asyncio.run(go())


def test_a_node_resubscribing_supersedes_only_its_own_stream():
    async def go():
        reg = WakeRegistry()
        first = reg.register(KEY, "a1", "198.51.100.1")
        other = reg.register(KEY, "b2", "198.51.100.1")
        again = reg.register(KEY, "a1", "198.51.100.1")
        assert first.closed and not other.closed and not again.closed
        # The superseded stream's teardown must leave its successor alone.
        assert reg.unregister(KEY, "a1", first) is False
        reg.ping(KEY)
        assert again.kinds == {"message"} and other.kinds == {"message"}
        assert first.kinds == set()
    asyncio.run(go())


def test_the_key_is_gone_only_with_its_last_stream():
    async def go():
        reg = WakeRegistry()
        a = reg.register(KEY, "a1", "198.51.100.1")
        b = reg.register(KEY, "b2", "198.51.100.2")
        assert reg.unregister(KEY, "a1", a) is False
        assert reg.keys() == [KEY]
        assert reg.unregister(KEY, "b2", b) is True
        assert reg.keys() == []
        assert reg.queue_envelope(KEY, {}) == "not connected"
        assert reg.push_frame(KEY, {}) is False
    asyncio.run(go())


def test_past_the_key_cap_the_oldest_stream_yields(monkeypatch):
    monkeypatch.setattr(wake_registry, "MAX_PER_KEY", 2)

    async def go():
        reg = WakeRegistry()
        oldest = reg.register(KEY, "a1", "198.51.100.1")
        middle = reg.register(KEY, "b2", "198.51.100.1")
        newest = reg.register(KEY, "c3", "198.51.100.1")
        assert oldest.closed and not middle.closed and not newest.closed
        assert reg.unregister(KEY, "a1", oldest) is False
    asyncio.run(go())


def test_the_address_cap_refuses_a_new_stream_but_not_a_resubscription(monkeypatch):
    monkeypatch.setattr(wake_registry, "MAX_PER_IP", 2)

    async def go():
        reg = WakeRegistry()
        reg.register(KEY, "a1", "198.51.100.1")
        reg.register("cd" * 32, "", "198.51.100.1")
        assert reg.register("ef" * 32, "", "198.51.100.1") is None
        assert reg.register(KEY, "b2", "198.51.100.1") is None
        assert reg.register(KEY, "a1", "198.51.100.1") is not None
        assert reg.register("ef" * 32, "", "198.51.100.9") is not None
    asyncio.run(go())


def test_an_envelope_skips_a_full_stream_and_is_refused_when_all_are(monkeypatch):
    monkeypatch.setattr(wake_registry, "QUEUE_MAX", 1)

    async def go():
        reg = WakeRegistry()
        dead = reg.register(KEY, "a1", "198.51.100.1")     # never drained
        live = reg.register(KEY, "b2", "198.51.100.1")
        assert reg.queue_envelope(KEY, {"message_uuid": "m1"}) is None
        reg.drain(live)
        assert reg.queue_envelope(KEY, {"message_uuid": "m2"}) is None
        assert [e["message_uuid"] for e in dead.envelopes] == ["m1"]
        assert [e["message_uuid"] for e in live.envelopes] == ["m2"]
        assert reg.queue_envelope(KEY, {"message_uuid": "m3"}) == "busy"
    asyncio.run(go())


def test_close_ends_every_stream_of_a_key():
    async def go():
        reg = WakeRegistry()
        a = reg.register(KEY, "a1", "198.51.100.1")
        b = reg.register(KEY, "b2", "198.51.100.1")
        other = reg.register("cd" * 32, "", "198.51.100.1")
        reg.close(KEY)
        assert a.closed and b.closed and not other.closed
        assert reg.queue_envelope(KEY, {}) == "not connected"
    asyncio.run(go())


def test_instance_tokens():
    assert valid_instance("")                  # a node from before 2026-09-24
    assert valid_instance("0123456789abcdef")
    assert not valid_instance("0123456789ABCDEF")
    assert not valid_instance("a" * 33)
    assert not valid_instance("../x")
