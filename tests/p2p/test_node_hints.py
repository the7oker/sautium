"""The capability-directory client: cache discipline and the registration
signature contract (the Worker side is exercised by the harness)."""

import importlib

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from desktop.p2p import node_hints as nh


def _reset(monkeypatch):
    importlib.reload(nh)
    import desktop.p2p.node_hints as fresh
    monkeypatch.setattr("desktop.p2p.master_node.master_configured", lambda: True)
    return fresh


def test_fetch_caches_per_cap_and_survives_errors(monkeypatch):
    m = _reset(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(m.time, "monotonic", lambda: clock[0])
    calls = []

    def get_ok(url):
        calls.append(url)
        return {"nodes": [{"pubkey": "ab" * 32, "host": "198.51.100.50", "host6": None, "port": 20246},
                          {"pubkey": "cd" * 32, "host": None, "host6": "2a00:db8::77", "port": 20300},
                          {"pubkey": "ef" * 32, "host": "", "port": 1},
                          {"pubkey": "01" * 32, "host": "1.2.3.4", "port": 0}]}

    nodes = m.fetch("mbdump", _get=get_ok)
    assert nodes == [("198.51.100.50", 20246, "ab" * 32), ("2a00:db8::77", 20300, "cd" * 32)]
    assert m.fetch("mbdump", _get=get_ok) == nodes and len(calls) == 1            # cached
    assert m.fetch("relay", _get=get_ok) == nodes and len(calls) == 2             # per-cap cache
    clock[0] += m.HINTS_TTL_S + 1

    def get_fail(url):
        calls.append(url)
        raise OSError("offline")

    assert m.fetch("mbdump", _get=get_fail) == nodes                              # stale beats nothing
    assert m.fetch("mbdump", _get=get_fail) == nodes and len(calls) == 3          # backoff
    assert m.fetch("nonsense", _get=get_ok) == []                                 # unknown cap: never fetched
    monkeypatch.setattr("desktop.p2p.master_node.master_configured", lambda: False)
    assert m.fetch("mbdump", _get=get_ok) == []                                   # dev build: no worker pinned


def test_registration_signature_contract():
    key = Ed25519PrivateKey.generate()
    sig, caps = nh.registration_signature(key.sign, 20246, ["mbdump", "sync", "mbdump"], 1_800_000_000)
    assert caps == ["mbdump", "sync"]
    Ed25519PublicKey.from_public_bytes(key.public_key().public_bytes_raw()).verify(
        bytes.fromhex(sig), b"sautium-directory:v1:20246:mbdump,sync:1800000000")
