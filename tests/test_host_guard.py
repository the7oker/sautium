"""The Host guard (auth_hmac._host_allowed): a closed set of this node's own
addresses, re-read from the interfaces for an IP literal it has not seen — a
tunnel that came up after startup — and never a name the client supplied."""

import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import auth_hmac  # noqa: E402
import tls_gen  # noqa: E402

LAN = "192.168.1.10"
TUNNEL = "100.101.102.103"


@pytest.fixture
def interfaces(monkeypatch):
    """The addresses `tls_gen.detect_own_ipv4s` reports, mutable per test, with
    the guard's caches reset and no operator configuration in the way."""
    state = SimpleNamespace(own=[LAN], scans=0)

    def own_ipv4s():
        state.scans += 1
        return list(state.own)

    monkeypatch.setattr(tls_gen, "detect_own_ipv4s", own_ipv4s)
    monkeypatch.setattr(auth_hmac, "_allowed_hosts", None)
    monkeypatch.setattr(auth_hmac, "_host_misses", {})
    monkeypatch.delenv("SAUTIUM_HOST_IPS", raising=False)
    monkeypatch.delenv("SAUTIUM_ALLOWED_HOSTS", raising=False)
    return state


def test_tunnel_address_bound_at_startup_is_accepted(interfaces):
    interfaces.own.append(TUNNEL)
    assert auth_hmac._host_allowed(f"{TUNNEL}:18000")
    assert auth_hmac._host_allowed(f"{LAN}:8800")


def test_address_that_comes_up_after_startup_is_learned_once(interfaces):
    assert auth_hmac._host_allowed(f"{LAN}:8800")
    assert interfaces.scans == 1
    assert not auth_hmac._host_allowed(f"{TUNNEL}:18000")

    interfaces.own.append(TUNNEL)
    assert not auth_hmac._host_allowed(f"{TUNNEL}:18000")   # the refusal is still cached
    auth_hmac._host_misses.clear()
    assert auth_hmac._host_allowed(f"{TUNNEL}:18000")
    scans = interfaces.scans
    assert auth_hmac._host_allowed(f"{TUNNEL}:18000")
    assert interfaces.scans == scans                          # in the set now — no rescan


def test_unbound_literal_is_refused_and_the_miss_expires(interfaces, monkeypatch):
    assert not auth_hmac._host_allowed(f"{TUNNEL}:18000")
    scans = interfaces.scans
    assert not auth_hmac._host_allowed(f"{TUNNEL}:18000")
    assert not auth_hmac._host_allowed(TUNNEL)
    assert interfaces.scans == scans                          # one enumeration per literal

    later = auth_hmac.time.monotonic() + auth_hmac._MISS_TTL_S + 1
    monkeypatch.setattr(auth_hmac.time, "monotonic", lambda: later)
    assert not auth_hmac._host_allowed(TUNNEL)
    assert interfaces.scans == scans + 1


def test_miss_cache_stays_bounded(interfaces):
    for i in range(auth_hmac._MISS_CAP + 40):
        assert not auth_hmac._host_allowed(f"10.9.{i // 256}.{i % 256}")
    assert len(auth_hmac._host_misses) <= auth_hmac._MISS_CAP


def test_a_name_is_never_resolved_nor_rescanned(interfaces, monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("client-supplied name was resolved")

    monkeypatch.setattr(socket, "gethostbyname", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    assert not auth_hmac._host_allowed("evil.example.com")
    assert not auth_hmac._host_allowed("evil.example.com:8800")
    assert not auth_hmac._host_allowed("[fd00::1]:8800")
    assert interfaces.scans == 1                              # the startup set only
    assert not auth_hmac._host_misses


def test_loopback_forms_and_no_header(interfaces):
    assert auth_hmac._host_allowed("[::1]:8000")
    assert auth_hmac._host_allowed("localhost:8800")
    assert auth_hmac._host_allowed("127.0.0.1")
    assert auth_hmac._host_allowed("host.docker.internal:8800")
    assert auth_hmac._host_allowed("")


def test_operator_configuration_is_honoured(interfaces, monkeypatch):
    monkeypatch.setenv("SAUTIUM_HOST_IPS", "100.64.0.9")
    monkeypatch.setenv("SAUTIUM_ALLOWED_HOSTS", "Node.tail1234.ts.net")
    assert auth_hmac._host_allowed("100.64.0.9:8800")
    assert auth_hmac._host_allowed("node.tail1234.ts.net")
    assert not auth_hmac._host_allowed("other.tail1234.ts.net")


def test_detect_own_ipv4s_keeps_every_bound_address_but_loopback_and_link_local(monkeypatch):
    def addr(family, address):
        return SimpleNamespace(family=family, address=address)

    monkeypatch.setattr(tls_gen.psutil, "net_if_addrs", lambda: {
        "lo": [addr(socket.AF_INET, "127.0.0.1"), addr(socket.AF_INET6, "::1")],
        "eth0": [addr(socket.AF_INET, LAN), addr(socket.AF_INET6, "fe80::1%eth0")],
        "tailscale0": [addr(socket.AF_INET, TUNNEL)],
        "vEthernet": [addr(socket.AF_INET, "169.254.3.4")],
        "docker0": [addr(socket.AF_INET, "172.17.0.1")],
    })
    assert tls_gen.detect_own_ipv4s() == [TUNNEL, "172.17.0.1", LAN]
