"""What a node asks of its router, and what it believes of the answer
(PI-NODE-2C DESIGN §5.3, R1).

A second node behind one router asked for the external port its neighbour
held: the router said yes — over its own static forward of that port, which
its mapping table does not list — and the node announced a port that led to
the neighbour (PI-NODE-2B §8.3). The mapper now looks before it asks, checks
a new mapping from inside, and counts a refused renewal as a lost mapping.

The router is a fake with miniupnpc's five calls; the nodes that answer on
the "public" address are loopback TLS servers bound to a chosen key."""

import asyncio
import datetime
import socket
import ssl
import sys
import threading
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import p2p_identity  # noqa: E402
import tls_gen  # noqa: E402
from desktop import portmap
from desktop.p2p import peer_auth, upnp_service
from desktop.p2p.upnp_service import UPnPService, leads_here

LAN, NEIGHBOUR = "192.168.7.20", "192.168.7.30"


class FakeIgd:
    """A router that says yes to anyone, as §8.3's did: an add over another
    device's mapping takes it, and an add over one of its own static
    forwards (`static`) is accepted and listed, and forwards nothing."""
    lanaddr = LAN

    def __init__(self):
        self.table = {}           # (external, protocol) -> (host, port, description)
        self.static = set()
        self.refuses = set()
        self.asked = []

    def externalipaddress(self):
        return "127.0.0.1"        # where the loopback "public" servers listen

    def getspecificportmapping(self, external, protocol):
        held = self.table.get((external, protocol))
        return held and (held[0], held[1], held[2], True, 3600)

    def addportmapping(self, external, protocol, host, port, description, _remote, _lease):
        self.asked.append(external)
        if external in self.refuses:
            return False
        self.table[(external, protocol)] = (host, port, description)
        return True

    def deleteportmapping(self, external, protocol):
        return self.table.pop((external, protocol), None) is not None


@pytest.fixture
def igd(monkeypatch):
    router = FakeIgd()
    monkeypatch.setattr(upnp_service, "HAS_UPNP", True)
    monkeypatch.setattr(upnp_service, "discover_igd", lambda: router)
    return router


def _key():
    key = Ed25519PrivateKey.generate()
    return key, key.public_key().public_bytes_raw().hex()


def _bound_context(tmp_path, node_key=None) -> ssl.SSLContext:
    """A server context whose certificate carries `node_key`'s binding — a
    peer surface's — or none."""
    tls_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "node")])
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
               .public_key(tls_key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - datetime.timedelta(minutes=5))
               .not_valid_after(now + datetime.timedelta(days=1)))
    if node_key is not None:
        spki = tls_key.public_key().public_bytes(serialization.Encoding.DER,
                                                 serialization.PublicFormat.SubjectPublicKeyInfo)
        pub = node_key.public_key().public_bytes_raw().hex()
        builder = builder.add_extension(x509.UnrecognizedExtension(
            x509.ObjectIdentifier(peer_auth.TLS_BINDING_OID),
            peer_auth.tls_binding_value(node_key.sign, pub, spki)), critical=False)
    cert = builder.sign(tls_key, hashes.SHA256())
    folder = tmp_path / f"tls-{x509.random_serial_number()}"
    folder.mkdir()
    (folder / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (folder / "k.pem").write_bytes(tls_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(folder / "c.pem"), str(folder / "k.pem"))
    return ctx


class Answering:
    """A loopback listener that completes each connection's TLS handshake
    (or, with no context, answers in the clear) and closes it."""

    def __init__(self, ctx=None, port=0):
        self.ctx = ctx
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                if self.ctx is None:
                    conn.sendall(b"HTTP/1.0 200 OK\r\n\r\n")
                else:
                    self.ctx.wrap_socket(conn, server_side=True).close()
            except OSError:
                pass
            finally:
                conn.close()

    def close(self):
        self.sock.close()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# -- the look ----------------------------------------------------------------------

def test_a_port_another_device_holds_is_never_asked_for(igd):
    igd.table[(8801, "TCP")] = (NEIGHBOUR, 8801, "the neighbour's")
    upnp = UPnPService(ports=[8801])
    assert upnp.open_ports() == "127.0.0.1"
    assert upnp.get_external_port(8801) == 8802
    assert igd.asked == [8802]
    assert igd.table[(8801, "TCP")] == (NEIGHBOUR, 8801, "the neighbour's")


def test_another_port_of_this_machine_is_not_taken_over_either(igd):
    igd.table[(8801, "TCP")] = (LAN, 24680, "a launcher node here")
    upnp = UPnPService(ports=[8801])
    upnp.open_ports()
    assert upnp.get_external_port(8801) == 8802
    assert igd.table[(8801, "TCP")][1] == 24680


def test_this_nodes_own_mapping_is_renewed(igd):
    igd.table[(8801, "TCP")] = (LAN, 8801, "Sautium (8801)")
    upnp = UPnPService(ports=[8801])
    upnp.open_ports()
    assert upnp.get_external_port(8801) == 8801 and igd.asked == [8801]


def test_a_router_that_will_not_say_is_asked_as_before(igd, monkeypatch):
    def refuses(*_a):
        raise Exception("Invalid Args")
    monkeypatch.setattr(igd, "getspecificportmapping", refuses)
    upnp = UPnPService(ports=[8801])
    upnp.open_ports()
    assert upnp.get_external_port(8801) == 8801


def test_a_client_named_and_not_numbered_is_asked_as_before(igd):
    """A router may name a mapping's client by host name: whose it is cannot
    be read, and taking it for another's would drop the node's own mapping
    at every renewal."""
    igd.table[(8801, "TCP")] = ("node.lan", 8801, "Sautium (8801)")
    upnp = UPnPService(ports=[8801])
    upnp.open_ports()
    assert upnp.get_external_port(8801) == 8801


def test_five_ports_held_leave_the_node_unmapped(igd):
    for port in range(8801, 8806):
        igd.table[(port, "TCP")] = (NEIGHBOUR, port, "held")
    upnp = UPnPService(ports=[8801])
    upnp.open_ports()
    assert upnp.get_external_port(8801) is None and igd.asked == []


def test_the_cli_refuses_a_port_that_leads_elsewhere(igd, monkeypatch):
    import argparse
    igd.table[(8801, "TCP")] = (NEIGHBOUR, 8801, "the neighbour's")
    monkeypatch.setattr(portmap, "_igd_or_exit", lambda: igd)
    monkeypatch.setattr(portmap, "answers_as_peer_surface", lambda port: True)
    with pytest.raises(SystemExit) as refused:
        portmap.cmd_map(argparse.Namespace(port=8801, external=None, udp=False, lease=0, name="",
                                           keep=False, pcp=False))
    assert NEIGHBOUR in str(refused.value) and igd.asked == []


# -- the check from inside -----------------------------------------------------------

def test_the_inside_check_tells_this_node_from_another_and_from_silence(tmp_path):
    mine, mine_pub = _key()
    other, _ = _key()
    check = leads_here(lambda: mine_pub)
    here, neighbour = Answering(_bound_context(tmp_path, mine)), Answering(_bound_context(tmp_path, other))
    unbound, clear = Answering(_bound_context(tmp_path)), Answering()
    try:
        assert check("127.0.0.1", here.port) is True
        assert check("127.0.0.1", neighbour.port) is False
        assert check("127.0.0.1", unbound.port) is None        # TLS, but no node's
        assert check("127.0.0.1", clear.port) is None          # something else entirely
        assert check("127.0.0.1", _free_port()) is None        # no hairpin, no answer
        assert leads_here(lambda: None)("127.0.0.1", here.port) is None     # no account yet
    finally:
        for server in (here, neighbour, unbound, clear):
            server.close()


def test_a_yes_over_the_routers_own_forward_is_given_back_for_the_next_port(igd, tmp_path):
    """§8.3 whole: the router forwards the port to the neighbour by a rule
    of its own, says yes to this node's mapping of it, and the neighbour's
    key answers on the public address."""
    mine, mine_pub = _key()
    other, _ = _key()
    neighbour = Answering(_bound_context(tmp_path, other))
    port = neighbour.port
    with socket.socket() as probe:              # nothing answers on the next port: no hairpin told
        probe.bind(("127.0.0.1", port + 1))
    igd.static.add(port)
    try:
        upnp = UPnPService(ports=[port], leads_here=leads_here(lambda: mine_pub))
        upnp.open_ports()
    finally:
        neighbour.close()
    assert igd.asked == [port, port + 1]
    assert upnp.get_external_port(port) == port + 1
    assert (port, "TCP") not in igd.table


def test_a_mapping_another_node_answers_on_is_removed(igd):
    upnp = UPnPService(ports=[8801], leads_here=lambda ip, port: port != 8801)
    upnp.open_ports()
    assert upnp.get_external_port(8801) == 8802
    assert list(igd.table) == [(8802, "TCP")]


def test_a_check_that_learns_nothing_keeps_the_mapping(igd):
    asked = []
    upnp = UPnPService(ports=[8801], leads_here=lambda ip, port: asked.append((ip, port)))
    upnp.open_ports()
    assert upnp.get_external_port(8801) == 8801
    assert asked == [("127.0.0.1", 8801)]       # once, of the router's public address


# -- the renewal ---------------------------------------------------------------------

def test_a_refused_renewal_is_a_lost_mapping(igd):
    upnp = UPnPService(ports=[8801])
    upnp.open_ports()
    assert upnp.get_external_port(8801) == 8801
    igd.refuses.update(range(8801, 8806))
    assert upnp.renew_ports() is False
    assert upnp.get_external_port(8801) is None


def test_a_mapping_that_now_leads_elsewhere_is_lost_and_the_next_port_taken(igd):
    upnp = UPnPService(ports=[8801])
    upnp.open_ports()
    igd.table[(8801, "TCP")] = (NEIGHBOUR, 8801, "taken while the lease was out")
    assert upnp.renew_ports() is True
    assert upnp.get_external_port(8801) == 8802
    assert igd.table[(8801, "TCP")][0] == NEIGHBOUR


def test_a_renewal_that_holds_keeps_the_port(igd):
    upnp = UPnPService(ports=[8801])
    upnp.open_ports()
    assert upnp.renew_ports() is True
    assert upnp.get_external_port(8801) == 8801 and igd.asked == [8801, 8801]


def test_the_announce_hears_of_a_lost_mapping(igd, monkeypatch):
    """keep_mapped tells its listener the external port is gone — the
    runtime then announces its local port and asks the verdict again."""
    monkeypatch.setattr(upnp_service, "LEASE_DURATION", 0.01)
    upnp = UPnPService(ports=[8801])
    upnp.open_ports()
    igd.refuses.update(range(8801, 8806))

    async def scene():
        heard = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(upnp.keep_mapped(8801, heard.set_result))
        try:
            return await asyncio.wait_for(heard, 10)
        finally:
            task.cancel()
    assert asyncio.run(scene()) is None


# -- a node born under a running process ------------------------------------------------
# PI-NODE-2C's evening on a 4 GB Pi (F1, F7): the node's first start has no
# account, so its peer certificate is bound to no key and the mapper's check
# from inside has none to pin; the account is then made with no restart.

def test_a_surface_started_with_no_identity_answers_under_it_once_it_has_one(tmp_path):
    key, pub = _key()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_gen.load_bound(ctx, tmp_path)                           # the start, no account yet
    surface = Answering(ctx)
    try:
        assert leads_here(lambda: pub)("127.0.0.1", surface.port) is None   # every pinned dial refused
        cert = tls_gen.load_bound(ctx, tmp_path, (pub, key.sign))            # the account is made
        assert leads_here(lambda: pub)("127.0.0.1", surface.port) is True   # the same listener
        made = cert.read_bytes()
        tls_gen.load_bound(ctx, tmp_path, (pub, key.sign))
        assert cert.read_bytes() == made                        # not minted again for the key it has

        new, new_pub = _key()                                   # a rotation
        tls_gen.load_bound(ctx, tmp_path, (new_pub, new.sign))
        assert leads_here(lambda: new_pub)("127.0.0.1", surface.port) is True
        assert leads_here(lambda: pub)("127.0.0.1", surface.port) is False
    finally:
        surface.close()


def test_a_port_taken_before_the_node_had_a_key_is_given_back_when_it_has_one(igd, tmp_path):
    """The router forwards the port to a neighbour by a rule of its own and
    says yes to this node's mapping of it; the node has no key yet, so its
    check learns nothing and the mapping stands — until the key appears."""
    mine, mine_pub = _key()
    other, _ = _key()
    neighbour = Answering(_bound_context(tmp_path, other))
    port = neighbour.port
    with socket.socket() as probe:              # nothing answers on the next port: no hairpin told
        probe.bind(("127.0.0.1", port + 1))
    igd.static.add(port)
    held = []
    try:
        upnp = UPnPService(ports=[port], leads_here=leads_here(lambda: held[0] if held else None))
        upnp.open_ports()
        assert upnp.get_external_port(port) == port
        assert upnp.recheck() is False              # still no key: nothing learnt, nothing moved
        assert upnp.get_external_port(port) == port

        held.append(mine_pub)
        assert upnp.recheck() is True
    finally:
        neighbour.close()
    assert upnp.get_external_port(port) == port + 1
    assert list(igd.table) == [(port + 1, "TCP")]
    assert upnp.recheck() is False                  # the port it holds now: nothing more to give back


def test_a_recheck_leaves_a_mapping_that_leads_here(igd, tmp_path):
    mine, mine_pub = _key()
    here = Answering(_bound_context(tmp_path, mine))
    try:
        upnp = UPnPService(ports=[here.port], leads_here=leads_here(lambda: mine_pub))
        upnp.open_ports()
        assert upnp.recheck() is False
    finally:
        here.close()
    assert upnp.get_external_port(here.port) == here.port
    assert igd.asked == [here.port]


def test_a_recheck_with_nothing_mapped_asks_nobody(igd):
    asked = []
    upnp = UPnPService(ports=[8801], leads_here=lambda ip, port: asked.append(port))
    assert upnp.recheck() is False
    assert asked == [] and igd.asked == []


def test_the_identitys_event_reaches_each_follower_once(monkeypatch):
    monkeypatch.setattr(p2p_identity, "_followers", [])
    heard = []

    async def follower():
        heard.append("peer surface")

    p2p_identity.on_change(follower)
    p2p_identity.on_change(follower)                # a second lifespan in one process
    asyncio.run(p2p_identity.changed())
    assert heard == ["peer surface"]

