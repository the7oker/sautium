"""The master address hint cache: TTL, failure backoff, stale-while-error."""

import importlib

from desktop.p2p import master_hint as mh


def _reset():
    importlib.reload(mh)


def test_hint_caches_and_survives_transport_errors(monkeypatch):
    _reset()
    clock = [1000.0]
    monkeypatch.setattr(mh.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr("desktop.p2p.master_node.master_configured", lambda: True)
    calls = []

    def get_ok(url):
        calls.append(url)
        return {"host": "198.51.100.77", "port": 8801, "updated_at": "x"}

    assert mh.fetch(_get=get_ok) == ("198.51.100.77", 8801)
    assert mh.fetch(_get=get_ok) == ("198.51.100.77", 8801) and len(calls) == 1   # cached
    clock[0] += mh.HINT_TTL_S + 1

    def get_fail(url):
        calls.append(url)
        raise OSError("offline")

    assert mh.fetch(_get=get_fail) == ("198.51.100.77", 8801)                     # stale beats nothing
    assert mh.fetch(_get=get_fail) == ("198.51.100.77", 8801) and len(calls) == 2  # backoff: no refetch
    clock[0] += mh.NEGATIVE_TTL_S + 1
    assert mh.fetch(_get=get_ok) == ("198.51.100.77", 8801) and len(calls) == 3

    clock[0] += mh.HINT_TTL_S + 1
    assert mh.fetch(_get=lambda u: {}) is None                                     # authoritative empty clears
    assert mh.fetch(_get=get_ok) is None                                           # and is itself cached
    assert mh.fetch(_get=get_ok, force=True) == ("198.51.100.77", 8801)

    _reset()
    monkeypatch.setattr("desktop.p2p.master_node.master_configured", lambda: False)
    assert mh.fetch(_get=get_ok) is None                                           # dev build: no master pinned


def test_bad_shapes_are_rejected(monkeypatch):
    _reset()
    monkeypatch.setattr("desktop.p2p.master_node.master_configured", lambda: True)
    for bad in ({"host": "", "port": 8801}, {"host": "1.2.3.4", "port": 0},
                {"host": "1.2.3.4", "port": "8801"}, {"port": 8801}):
        _reset()
        monkeypatch.setattr("desktop.p2p.master_node.master_configured", lambda: True)
        assert mh.fetch(_get=lambda u, b=bad: b) is None
