"""
A provider's own live state in the ranking (streaming/base.py SteppedAside,
service.providers_preferred / providers_aside) — pure logic: providers are
objects in a registry, the wake is recorded. Run inside the backend container:

    python -m pytest test_stream_provider_aside.py -q
"""

import logging

import pytest

from streaming import base, service
from streaming.base import (CONTRACT_VERSION, FetchedAudio, ProviderError, ProviderManifest,
                            ProviderRegistry, SteppedAside, StreamProvider, TrackQuery)

ASIDE = SteppedAside(code="no_tier", since="2026-11-14T09:00:00+00:00",
                     title="Lossless-X: this account no longer has the tier",
                     text="Tracks stream from another provider.", detail="")


class _Plain(StreamProvider):
    """A provider built before the live state: it implements none of it."""

    def __init__(self, pid, **flags):
        flags.setdefault("lossless", False)
        self.manifest = ProviderManifest(id=pid, name=pid.title(), kind="direct_url", **flags)

    def fetch(self, query):
        raise NotImplementedError


class _Stateful(_Plain):
    def __init__(self, pid, **flags):
        super().__init__(pid, **flags)
        self.aside = None
        self.checks = 0

    def stepped_aside(self):
        return self.aside

    def check_again(self):
        self.checks += 1


@pytest.fixture
def registry(monkeypatch):
    woke = []
    monkeypatch.setattr(base, "_aside_listeners", [woke.append])
    reg = ProviderRegistry()
    for prov in (_Plain("demo", demo_limited=True), _Plain("clip", excerpt=True),
                 _Stateful("lossless-x", lossless=True), _Plain("lossless-a", lossless=True)):
        reg.register(prov)
    monkeypatch.setattr(service, "_registry", reg)
    return reg, woke


def _order():
    return [p.manifest.id for p in service.providers_preferred()]


def test_the_ranking_is_unchanged_for_providers_without_the_state(registry):
    reg, _ = registry
    assert _order() == ["lossless-a", "lossless-x", "demo", "clip"]
    assert service.providers_aside() == []
    for pid in ("demo", "clip", "lossless-a"):
        plain = reg.get(pid)
        assert plain.stepped_aside() is None and plain.check_again() is None


def test_a_provider_that_stepped_aside_is_registered_and_not_offered(registry):
    reg, _ = registry
    prov = reg.get("lossless-x")
    prov.aside = ASIDE
    assert _order() == ["lossless-a", "demo", "clip"]
    assert service.providers_aside() == [(prov, ASIDE)]
    other = reg.get("lossless-a")
    other.stepped_aside = lambda: ASIDE
    assert service.providers_aside() == [(other, ASIDE), (prov, ASIDE)]     # by id
    del other.stepped_aside
    assert service.get_provider("lossless-x") is prov          # provenance, names, Check again
    assert prov in reg.enabled()
    prov.aside = None
    assert _order() == ["lossless-a", "lossless-x", "demo", "clip"]
    assert service.providers_aside() == []


def test_the_only_lossless_provider_aside_leaves_the_next_one_first(registry):
    reg, _ = registry
    reg.get("lossless-x").aside = ASIDE
    reg._by_id.pop("lossless-a")
    assert _order() == ["demo", "clip"]
    assert service.get_provider().manifest.id == "demo"


def test_a_providers_word_of_a_change_reaches_the_hosts_listeners(registry):
    reg, woke = registry
    prov = reg.get("lossless-x")
    prov.aside = ASIDE
    prov.aside_changed()
    assert woke == [prov]


def test_the_service_listens_from_its_import():
    """Before any provider is built: a read a provider starts in its factory
    can land at any moment after. Asked of a fresh interpreter — this
    process's list is the fixtures' to patch."""
    import subprocess
    import sys
    out = subprocess.run(
        [sys.executable, "-c",
         "from streaming import base, service; "
         "print(base._aside_listeners == [service._aside_changed])"],
        capture_output=True, text=True, check=True).stdout
    assert out.strip() == "True"


def test_listeners_are_added_never_replaced(registry, monkeypatch):
    reg, woke = registry
    also = []
    base.on_aside_change(also.append)
    prov = reg.get("lossless-x")
    prov.aside_changed()
    assert woke == [prov] and also == [prov]


def test_a_wake_that_fails_is_logged_and_the_other_listeners_still_hear(registry, monkeypatch, caplog):
    """The state is read where it lives: a failed wake loses a push, and
    must not fail the fetch or the check that found the change."""
    reg, woke = registry

    def down(prov):
        raise RuntimeError("the database is away")

    monkeypatch.setattr(base, "_aside_listeners", [down, woke.append])
    prov = reg.get("lossless-x")
    with caplog.at_level(logging.ERROR, logger="streaming.base"):
        prov.aside_changed()
    assert woke == [prov]
    assert [r.getMessage() for r in caplog.records] == [
        "stream provider lossless-x: the wake after its state changed failed"]
    assert caplog.records[0].exc_info[0] is RuntimeError


def test_the_wake_is_the_notices_channel_and_the_album_pages(registry, monkeypatch):
    reg, _ = registry
    import db_pool
    from streaming.events import preview_events
    sent, pings = [], []
    monkeypatch.setattr(db_pool, "db_execute", lambda sql, *a: sent.append(sql))
    monkeypatch.setattr(preview_events, "ping", lambda: pings.append(1))
    prov = reg.get("lossless-x")
    prov.aside = ASIDE
    service._aside_changed(prov)
    prov.aside = None
    service._aside_changed(prov)
    assert sent == ["NOTIFY sautium_notices"] * 2 and len(pings) == 2


def test_a_chain_resolved_earlier_does_not_ask_a_provider_that_stepped_aside(registry):
    """The proxy's fetch walks the chain as it was resolved: the link of a
    provider aside is skipped, unasked, and the next one serves."""
    from streaming.proxy import MediaProxy
    reg, _ = registry
    asked = []

    class _Serving(_Stateful):
        def download(self, source_id):
            asked.append((self.manifest.id, source_id))
            return FetchedAudio(data=b"audio", mime="audio/flac", lossless=self.manifest.lossless)

        def fetch(self, query):
            asked.append((self.manifest.id, None))
            return FetchedAudio(data=b"audio", mime="audio/mpeg")

    first, second = _Serving("first", lossless=True), _Serving("second")
    proxy = MediaProxy(port=0, advertised_host="127.0.0.1", file_token_key=b"test-key")
    tracks = iter(range(100))

    def served(chain):
        asked.clear()
        n = next(tracks)                # a track of its own: a held buffer is never fetched again
        query = TrackQuery(artist="A", title=f"T{n}", track_id=f"t{n}")
        token = proxy.start_session([(query, chain)])[0]
        entry = proxy.wait_ready(token, 10)
        return (entry.provider.manifest.id if entry.audio is not None else None,
                entry.error, list(asked))

    assert served([(first, "s1"), (second, None)]) == ("first", None, [("first", "s1")])
    first.aside = ASIDE
    assert served([(first, "s1"), (second, None)]) == ("second", None, [("second", None)])
    assert served([(first, None)]) == (None, "first: stepped aside (no_tier)", [])
    first.aside = None
    assert served([(first, None), (second, None)])[0] == "first"


def test_a_buffers_quality_badge_is_its_providers_whether_offered_or_not(registry):
    from routers import player
    reg, _ = registry
    assert player._stream_quality("lossless-x") == "lossless"
    reg.get("lossless-x").aside = ASIDE
    assert player._stream_quality("lossless-x") == "lossless"
    assert player._stream_quality("demo") == "lossy" and player._stream_quality(None) == "lossy"


def test_a_shared_upstream_answering_does_not_clear_a_providers_own_state(registry, monkeypatch):
    """The health key of a shared catalog may equal the provider's id: its
    silence is cleared by any provider on it answering, the account's state
    by the provider alone."""
    reg, _ = registry
    monkeypatch.setattr(service, "provider_health", {})
    prov = reg.get("lossless-x")
    prov.aside = ASIDE
    assert service.note_provider_health({"lossless-x": "quota"}, set()) is True
    assert service.note_provider_health({}, {"lossless-x"}) is True
    assert service.provider_health == {}
    assert service.providers_aside() == [(prov, ASIDE)]
    assert "lossless-x" not in _order()


def test_only_a_provider_built_for_a_newer_contract_is_warned_about(caplog):
    reg = ProviderRegistry()
    with caplog.at_level(logging.WARNING, logger="streaming.base"):
        reg.register(_Plain("older", contract_version=1))
        reg.register(_Plain("current", contract_version=CONTRACT_VERSION))
        assert caplog.records == []
        reg.register(_Plain("newer", contract_version=CONTRACT_VERSION + 1))
    assert [r.getMessage() for r in caplog.records] == [
        f"provider newer built for contract v{CONTRACT_VERSION + 1}, host is v{CONTRACT_VERSION}"]
