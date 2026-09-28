"""HQPlayer's own discovery datagram (backend/hqp_library.discover): a
`<discover/>` to the control port over UDP is answered with the HQPlayer's
name and product. A fake responder on loopback stands in for the box; no
network beyond that.
"""

import socket
import sys
import threading
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

hqp_library = pytest.importorskip("hqp_library")

PI_REPLY = (b'<?xml version="1.0" encoding="utf-8"?>'
            b'<discover name="HQPlayerEmbedded" result="NA" version="Signalyst HQPlayer Embedded 6"/>')
DESKTOP_REPLY = (b'<?xml version="1.0" encoding="utf-8"?>'
                 b'<discover name="STUDIO-PC" result="NA" version="Signalyst HQPlayer Desktop 6"/>')


def test_the_reply_names_the_hqplayer_and_its_product():
    assert hqp_library.parse_discover(PI_REPLY) == {"name": "HQPlayerEmbedded",
                                                    "product": "Signalyst HQPlayer Embedded 6"}
    desk = hqp_library.parse_discover(DESKTOP_REPLY)
    assert (desk["name"], hqp_library.is_embedded(desk["product"])) == ("STUDIO-PC", False)
    assert hqp_library.is_embedded(hqp_library.parse_discover(PI_REPLY)["product"])
    # anything else on that port is not an HQPlayer
    assert hqp_library.parse_discover(b"HTTP/1.1 200 OK\r\n\r\n") is None
    assert hqp_library.parse_discover(b"") is None


@pytest.fixture
def responder():
    """A box on loopback that answers `<discover/>` the way HQPlayer does
    and ignores everything else."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.2)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            if data.strip() == b"<discover/>":
                sock.sendto(DESKTOP_REPLY, addr)
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    yield sock.getsockname()[1]
    stop.set()
    t.join(1)
    sock.close()


def test_discover_finds_the_box_on_its_control_port(responder):
    assert hqp_library.discover("127.0.0.1", responder, timeout=1.0) == {
        "name": "STUDIO-PC", "product": "Signalyst HQPlayer Desktop 6"}


def test_discover_is_none_where_nothing_answers():
    quiet = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    quiet.bind(("127.0.0.1", 0))
    try:
        assert hqp_library.discover("127.0.0.1", quiet.getsockname()[1], timeout=0.3) is None
    finally:
        quiet.close()


def test_the_label_names_the_product_and_what_tells_two_apart():
    """A Desktop is told apart by its machine's name; a box's generic
    self-name adds nothing to the product and is dropped."""
    assert hqp_library.label("STUDIO-PC", "Signalyst HQPlayer Desktop") == "HQPlayer Desktop · STUDIO-PC"
    assert hqp_library.label("HQPlayerEmbedded", "Signalyst HQPlayer Embedded 6") == "HQPlayer Embedded"
    assert hqp_library.label("Living room", "Signalyst HQPlayer Embedded") == "HQPlayer Embedded · Living room"
    assert hqp_library.label("192.168.1.53", None) == "HQPlayer · 192.168.1.53"
    assert hqp_library.label(None, None) == "HQPlayer"


def test_a_renderer_at_an_alias_of_a_found_hqplayer_is_that_hqplayer(monkeypatch):
    """A Desktop's UPnP renderer answers the interface-bound search on a
    virtual adapter's address while its datagram was answered on the LAN
    one; both are this machine, so the renderer is not a second device. A
    Signalyst renderer where no HQPlayer answered stays a renderer, and any
    other maker's renderer is never an HQPlayer."""
    import auth_hmac
    import routers.player as player
    own = {"192.168.1.88", "172.26.80.1", "localhost"}
    monkeypatch.setattr(auth_hmac, "is_own_address", lambda h: (h or "").lower() in own)
    found = {"192.168.1.88": {"name": "STUDIO-PC", "product": "Signalyst HQPlayer Desktop 6"},
             "192.168.1.53": {"name": "HQPlayerEmbedded", "product": "Signalyst HQPlayer Embedded 6"}}

    def renderer(host, maker="Signalyst"):
        return {"manufacturer": maker, "location": f"http://{host}:2870/desc.xml"}

    assert player._is_hqplayer_renderer(renderer("172.26.80.1"), found)
    assert player._is_hqplayer_renderer(renderer("192.168.1.53"), found)
    assert not player._is_hqplayer_renderer(renderer("192.168.1.60"), found)
    assert not player._is_hqplayer_renderer(renderer("192.168.1.53", "Astell&Kern"), found)
