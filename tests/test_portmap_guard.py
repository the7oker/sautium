"""What `python -m desktop.portmap map` agrees to put on the router: a TCP
port only when it answers /health over TLS as the peer surface — never the
Web UI, the media proxy, the DLNA event port or a port with nothing behind
it — and never the LAN beacon's UDP port. The router is a stub here."""

import argparse
import datetime
import json
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from desktop import portmap
from desktop.p2p.lan_discovery import LAN_DISCOVERY_PORT


def _serve(health, *, tls_dir=None, status=200):
    """A loopback server answering /health with `health` (or `status`), over
    TLS when `tls_dir` is given; returns the running server."""
    body = json.dumps(health).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    if tls_dir is not None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*_self_signed(tls_dir))
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _self_signed(directory):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "node")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return str(cert_path), str(key_path)


@pytest.fixture
def router(monkeypatch):
    """The mappings asked of a stub router."""
    asked = []

    class Igd:
        lanaddr = "127.0.0.1"

        def externalipaddress(self):
            return "192.0.2.1"
    monkeypatch.setattr(portmap, "_igd_or_exit", lambda: Igd())
    monkeypatch.setattr(portmap, "add_mapping", lambda _u, ext, port, proto, *_a: asked.append(
        (ext, port, proto)) or True)
    return asked


def _map(port, *, udp=False):
    portmap.cmd_map(argparse.Namespace(port=port, external=None, udp=udp, lease=0, name="",
                                       keep=False, pcp=False))


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_peer_surface_is_mapped(router, tmp_path):
    server = _serve({"type": "sautium-peer"}, tls_dir=tmp_path)
    port = server.server_address[1]
    _map(port)
    server.shutdown()
    assert router == [(port, port, "TCP")]


@pytest.mark.parametrize("health,status", [({"type": "sautium-webui"}, 200), ({}, 404)])
def test_a_plain_http_surface_is_never_mapped(router, health, status):
    server = _serve(health, status=status)
    with pytest.raises(SystemExit):
        _map(server.server_address[1])
    server.shutdown()
    assert router == []


def test_a_port_with_nothing_behind_it_is_not_mapped(router):
    with pytest.raises(SystemExit):
        _map(_free_port())
    assert router == []


def test_the_lan_beacons_port_is_not_mapped(router):
    with pytest.raises(SystemExit):
        _map(LAN_DISCOVERY_PORT, udp=True)
    assert router == []
