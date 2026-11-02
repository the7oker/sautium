"""What the Output scan takes from a device that answers it (routers/player.py
_scan_run, hqp_library, hqplayer_client): a multicast LOCATION only as plain
HTTP on the address the answer came from; a description fetched with no
redirect, nothing compressed, so many bytes and documents, all of them within
one deadline and parsed off the server's loop; so many addresses per host and
in all; and whatever one HQPlayer answers is that box's failure alone."""

import asyncio
import gzip
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
from aiohttp import web

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import hqp_library  # noqa: E402
import hqplayer_client  # noqa: E402
from routers import player  # noqa: E402

pytest.importorskip("async_upnp_client")

SCPD = ('<?xml version="1.0"?><scpd xmlns="urn:schemas-upnp-org:service-1-0"><specVersion>'
        '<major>1</major><minor>0</minor></specVersion><actionList/><serviceStateTable/></scpd>')


def _description(services: int = 0, padding: int = 0) -> str:
    listed = "".join(
        f"<service><serviceType>urn:schemas-upnp-org:service:S{i}:1</serviceType>"
        f"<serviceId>urn:upnp-org:serviceId:S{i}</serviceId><SCPDURL>/scpd{i}.xml</SCPDURL>"
        f"<controlURL>/control{i}</controlURL><eventSubURL>/event{i}</eventSubURL></service>"
        for i in range(services))
    return ('<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><specVersion>'
            '<major>1</major><minor>0</minor></specVersion><device>'
            '<deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>'
            '<friendlyName>Shelf</friendlyName><manufacturer>Test</manufacturer><modelName>T1</modelName>'
            f'<UDN>uuid:shelf-1</UDN><serviceList>{listed}</serviceList></device>'
            f'<!--{"p" * padding}--></root>')


@pytest.fixture
def device():
    """An HTTP server on loopback that describes a renderer in each of the
    shapes the tests ask for, counting what is fetched; its base URL."""
    hits: dict = {}

    def counted(path, make):
        async def handler(request):
            hits[path] = hits.get(path, 0) + 1
            return await make(request)
        return handler

    async def text(body, **kw):
        return web.Response(text=body, content_type="text/xml", **kw)

    async def slow(_request):
        await asyncio.sleep(3)
        return await text(_description())

    routes = {
        "/plain.xml": lambda r: text(_description()),
        "/moved.xml": lambda r: asyncio.sleep(0, web.HTTPFound("/plain.xml")),
        "/packed.xml": lambda r: asyncio.sleep(0, web.Response(
            body=gzip.compress(_description().encode()), content_type="text/xml",
            headers={"Content-Encoding": "gzip"})),
        "/large.xml": lambda r: text(_description(padding=300 * 1024)),
        "/many.xml": lambda r: text(_description(services=24)),
        "/slow.xml": slow,
    }
    app = web.Application()
    for path, make in routes.items():
        app.router.add_get(path, counted(path, make))
    for i in range(24):
        app.router.add_get(f"/scpd{i}.xml", counted(f"/scpd{i}.xml", lambda r: text(SCPD)))
    loop = asyncio.new_event_loop()
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "127.0.0.1", 0)
    loop.run_until_complete(site.start())
    port = site._server.sockets[0].getsockname()[1]
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", hits
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)


def _describe(url):
    return asyncio.run(player._dlna_describe(url))


def test_a_description_is_fetched_plainly_and_bounded(device):
    base, hits = device
    assert _describe(f"{base}/plain.xml")["udn"] == "uuid:shelf-1"
    for shape in ("moved", "packed", "large", "many"):
        with pytest.raises(Exception):
            _describe(f"{base}/{shape}.xml")
    assert hits["/plain.xml"] == 1                     # the redirect was not followed
    assert sum(n for p, n in hits.items() if p.startswith("/scpd")) < player._DESCRIBE_DOCUMENTS


def test_the_descriptions_answer_within_one_deadline_off_the_servers_loop(device, monkeypatch):
    base, _ = device
    monkeypatch.setattr(player, "_DESCRIBE_DEADLINE_S", 0.5)
    started = time.monotonic()
    described = player._describe_all([f"{base}/slow.xml", f"{base}/plain.xml", f"{base}/large.xml"])
    assert time.monotonic() - started < 2.5
    assert list(described) == [f"{base}/plain.xml"]


def test_a_multicast_location_is_taken_only_as_plain_http_from_its_source():
    assert player._from_source("http://192.0.2.5:1400/d.xml", "192.0.2.5")
    assert not player._from_source("http://192.0.2.6:1400/d.xml", "192.0.2.5")
    assert not player._from_source("https://192.0.2.5/d.xml", "192.0.2.5")
    assert not player._from_source("file:///d.xml", "192.0.2.5")
    assert not player._from_source("http://192.0.2.5/d.xml", None)


def test_a_scan_describes_so_many_addresses_per_host_and_the_remembered_first():
    found = {f"usn{i}": f"http://192.0.2.9:{1000 + i}/d.xml" for i in range(10)}
    found.update({"known": "http://192.0.2.200:80/d.xml", "secure": "https://192.0.2.7/d.xml"})
    chosen = player._scan_locations(found, {"http://192.0.2.200:81/old.xml"})
    assert chosen[0] == "http://192.0.2.200:80/d.xml"
    assert len([loc for loc in chosen if "192.0.2.9" in loc]) == player._LOCATIONS_PER_HOST
    assert all(loc.startswith("http://") for loc in chosen)


@pytest.fixture
def quiet_scan(monkeypatch):
    """_scan_run with nothing of the node's own state in the way."""
    import async_upnp_client.search as search
    import tls_gen
    from routers import settings as settings_router
    monkeypatch.setattr(tls_gen, "detect_private_host_ips", lambda: [])
    monkeypatch.setattr(player, "_configured_hqp", lambda: None)
    monkeypatch.setattr(player, "_dlna_pins", lambda: {})
    monkeypatch.setattr(player, "_hqplayer_entries", lambda: [])
    monkeypatch.setattr(player, "_hqp_discovered", {})
    monkeypatch.setattr(player, "_dlna_discovered", {})
    monkeypatch.setattr(settings_router, "_read", lambda key, *a, **k: None)
    return search


def test_a_multicast_answer_naming_another_host_is_not_described(device, quiet_scan, monkeypatch):
    base, hits = device

    async def answers(callback, **_kw):
        await callback({"location": f"{base}/plain.xml", "usn": "uuid:a", "_host": "192.0.2.9"})
        await callback({"location": f"{base}/plain.xml?honest", "usn": "uuid:b", "_host": "127.0.0.1"})

    async def no_sweep(found, found_hqp):
        return None
    monkeypatch.setattr(quiet_scan, "async_search", answers)
    monkeypatch.setattr(player, "_unicast_sweep", no_sweep)
    result = asyncio.run(player._scan_run())
    assert [r["location"] for r in result["renderers"]] == [f"{base}/plain.xml?honest"]
    assert hits["/plain.xml"] == 1


def test_what_one_hqplayer_answers_is_its_failure_alone(quiet_scan, monkeypatch):
    async def no_search(*_a, **_k):
        return None

    async def sweep(found, found_hqp):
        found_hqp["192.0.2.7"] = {"name": "box", "product": "Signalyst HQPlayer Embedded 6"}
        found_hqp["192.0.2.8"] = {"name": "other", "product": "Signalyst HQPlayer Desktop 6"}

    def resolve(host, port):
        if host == "192.0.2.7":
            raise ValueError("an answer of no shape")
        return None, None, {}

    class Link:
        def refuse(self):
            return None
    monkeypatch.setattr(quiet_scan, "async_search", no_search)
    monkeypatch.setattr(player, "_unicast_sweep", sweep)
    monkeypatch.setattr(player.hqp_link, "link_for", lambda host, port: Link())
    monkeypatch.setattr(hqp_library, "resolve_endpoint", resolve)
    asyncio.run(player._scan_run())
    assert player._hqp_discovered[("192.0.2.7", 4321)]["control"] is False
    assert player._hqp_discovered[("192.0.2.8", 4321)]["control"] is True


def test_an_hqplayers_facts_are_plain_and_its_hash_a_token(monkeypatch):
    answers = {"<GetInfo/>": b'<GetInfo name="Stu\x00dio\x07" product="Signalyst HQPlayer Desktop 6"/>',
               "<LibraryGetHash/>": '<LibraryGetHash value="été"/>'.encode()}
    monkeypatch.setattr(hqp_library, "_command", lambda host, port, xml, timeout, limit=None: answers[xml])
    assert hqp_library.get_info("192.0.2.7", 4321)["name"] == "Studio"
    with pytest.raises(ConnectionError):
        hqp_library.library_hash("192.0.2.7", 4321)
    answers["<LibraryGetHash/>"] = b'<LibraryGetHash value="9f86d081884c7d65"/>'
    assert hqp_library.library_hash("192.0.2.7", 4321) == "9f86d081884c7d65"
    assert hqp_library.parse_discover(b'<discover name="Box\x00\x01" version="Signalyst HQPlayer Embedded 6"/>') \
        == {"name": "Box", "product": "Signalyst HQPlayer Embedded 6"}


def test_an_answer_past_its_limit_ends_the_connection():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def flood():
        conn, _ = server.accept()
        conn.recv(1024)
        try:
            conn.sendall(b"x" * (256 * 1024))
            time.sleep(1)
        except OSError:
            pass
        conn.close()
    threading.Thread(target=flood, daemon=True).start()
    client = hqplayer_client.HQPlayerClient("127.0.0.1", port, timeout=5, ring=False)
    assert client.connect()
    with pytest.raises(ConnectionError, match="longer than"):
        client.raw("<GetInfo/>", 5, limit=64 * 1024)
    assert not client.is_connected()
    server.close()
