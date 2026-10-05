"""The master address hint cache: TTL, failure backoff, stale-while-error."""

import asyncio
import importlib
import types

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


# ---------------------------------------------------------------------------
# _find_friend_peers: who gets the hint
# ---------------------------------------------------------------------------

HINT = ("198.51.100.77", 8801)


class _Lan:
    """LANDiscovery's lookups over live peers, none of them the master."""

    def __init__(self, *peers):
        self.peers = list(peers)

    def find_peer_by_invite_code(self, invite_code):
        return None

    def find_peer_by_node_id(self, node_id):
        return None


def _node(lan=None):
    return types.SimpleNamespace(_lan_discovery=lan, _friend_peer_cache={},
                                 _dht_service=None, _dht_refresh_inflight=set())


def _peers_of(node, friend):
    from desktop.p2p.p2p_manager import P2PManager
    return asyncio.run(P2PManager._find_friend_peers(node, friend))


def test_a_newborns_pending_master_contact_gets_the_hint(monkeypatch):
    """The first handshake is the call the hint exists for — while the
    contact is still `pending:` it has no pinned key to be matched by."""
    from desktop.p2p.master_node import MASTER_INVITE_CODE
    monkeypatch.setattr(mh, "fetch", lambda force=False: HINT)
    master = {"id": 1, "invite_code": MASTER_INVITE_CODE,
              "public_key_hex": f"pending:{MASTER_INVITE_CODE}"}
    assert _peers_of(_node(), master) == [HINT]

    stranger = {"id": 2, "invite_code": "someone#0000-0000-0000",
                "public_key_hex": "pending:someone#0000-0000-0000"}
    assert _peers_of(_node(), stranger) == []


def test_other_lan_nodes_do_not_stand_in_for_the_master(monkeypatch):
    from desktop.p2p.master_node import MASTER_INVITE_CODE
    monkeypatch.setattr(mh, "fetch", lambda force=False: HINT)
    master = {"id": 1, "invite_code": MASTER_INVITE_CODE,
              "public_key_hex": f"pending:{MASTER_INVITE_CODE}"}
    lan_node = ("192.0.2.10", 24001)
    assert _peers_of(_node(_Lan(lan_node)), master) == [HINT, lan_node]
