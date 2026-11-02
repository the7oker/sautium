"""The address a remote media consumer is handed — chosen by which network
it shares with us, never a fixed one."""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from config import settings  # noqa: E402
from streaming.media_host import (  # noqa: E402
    _same_network, media_host, media_host_for_name, through_tunnel)


def test_a_tunnels_address_is_cgnats_shared_space_and_nothing_else():
    """A renderer reached through a tunnel is one at a tailnet's address:
    the play gate takes a LAN address that answers over it."""
    assert through_tunnel("100.64.0.1") and through_tunnel("100.127.255.1")
    assert not through_tunnel("192.168.1.1") and not through_tunnel("phone.local")
    assert not through_tunnel(None)


def test_same_network_is_a_slash_24_except_cgnat_which_is_one():
    assert _same_network("192.168.1.1", "192.168.1.200")
    assert not _same_network("192.168.1.1", "192.168.2.1")
    assert _same_network("100.64.0.1", "100.127.255.1")
    assert not _same_network("100.64.0.1", "192.168.1.1")
    assert not _same_network("not-an-ip", "192.168.1.1")


def test_candidate_on_the_peers_network_wins(monkeypatch):
    monkeypatch.setattr(settings, "media_proxy_advertised_host", "192.168.1.88")
    monkeypatch.setenv("SAUTIUM_HOST_IPS", "100.101.102.103")
    assert media_host("192.168.1.53") == "192.168.1.88"
    assert media_host("100.64.5.5") == "100.101.102.103"


def test_a_name_for_this_machine_keeps_the_advertised_host(monkeypatch):
    monkeypatch.setattr(settings, "media_proxy_advertised_host", "127.0.0.1")
    for name in ("localhost", "127.0.0.1", "::1", "host.docker.internal",
                 "gateway.docker.internal", ""):
        assert media_host_for_name(name) == "127.0.0.1", name


def test_a_lan_address_picks_the_lan_candidate(monkeypatch):
    monkeypatch.setattr(settings, "media_proxy_advertised_host", "192.168.1.88")
    monkeypatch.delenv("SAUTIUM_HOST_IPS", raising=False)
    assert media_host_for_name("192.168.1.53") == "192.168.1.88"


def test_an_unresolvable_name_falls_back_to_the_advertised_host(monkeypatch):
    monkeypatch.setattr(settings, "media_proxy_advertised_host", "192.168.1.88")
    assert media_host_for_name("no-such-host.invalid") == "192.168.1.88"
