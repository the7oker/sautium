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
                 b'<discover name="VH11" result="NA" version="Signalyst HQPlayer Desktop 6"/>')


def test_the_reply_names_the_hqplayer_and_its_product():
    assert hqp_library.parse_discover(PI_REPLY) == {"name": "HQPlayerEmbedded",
                                                    "product": "Signalyst HQPlayer Embedded 6"}
    desk = hqp_library.parse_discover(DESKTOP_REPLY)
    assert (desk["name"], hqp_library.is_embedded(desk["product"])) == ("VH11", False)
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
        "name": "VH11", "product": "Signalyst HQPlayer Desktop 6"}


def test_discover_is_none_where_nothing_answers():
    quiet = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    quiet.bind(("127.0.0.1", 0))
    try:
        assert hqp_library.discover("127.0.0.1", quiet.getsockname()[1], timeout=0.3) is None
    finally:
        quiet.close()
