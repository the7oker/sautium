"""
A provider's own live state in the ranking (streaming/base.py SteppedAside,
service.providers_preferred / providers_aside) — pure logic: providers are
objects in a registry, the wake is recorded. Run inside the backend container:

    python -m pytest test_stream_provider_aside.py -q
"""

import logging

import pytest

from streaming import service
from streaming.base import (CONTRACT_VERSION, ProviderManifest, ProviderRegistry,
                            SteppedAside, StreamProvider)

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
    reg = ProviderRegistry(on_aside_change=woke.append)
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


def test_a_providers_word_of_a_change_reaches_the_hosts_listener(registry):
    reg, woke = registry
    prov = reg.get("lossless-x")
    prov.aside = ASIDE
    prov.aside_changed()
    assert woke == [prov]
    unregistered = _Stateful("elsewhere")
    unregistered.aside_changed()                               # no host listens: nothing to do
    assert woke == [prov]


def test_the_wake_is_the_notices_channel(registry, monkeypatch):
    reg, _ = registry
    import db_pool
    sent = []
    monkeypatch.setattr(db_pool, "db_execute", lambda sql, *a: sent.append(sql))
    prov = reg.get("lossless-x")
    prov.aside = ASIDE
    service._aside_changed(prov)
    prov.aside = None
    service._aside_changed(prov)
    assert sent == ["NOTIFY sautium_notices"] * 2


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
