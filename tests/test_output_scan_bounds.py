"""What the Output scan takes from a device that answers it (routers/player.py
_scan_run, hqp_library, hqplayer_client): a multicast LOCATION only as plain
HTTP, described at the address the answer came from; a description read whole
and alone — none of its services' documents — with no redirect, nothing
compressed, so many bytes, all of them within one deadline and parsed off the
server's loop; so many addresses per host and in all; and whatever one
HQPlayer answers is that box's failure alone. The attach reads a renderer's
every answer by the same rule (playback/dlna_backend.py _BoundedRequester)."""

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
from playback import dlna_backend  # noqa: E402
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


def _nested(depth: int, *, namespace: bool = True, name: str = "Shelf") -> str:
    """A media server whose renderer sits `depth` levels below it."""
    renderer = ('<device><deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>'
                f'<friendlyName>{name}</friendlyName><UDN>uuid:shelf-1</UDN></device>')
    for _ in range(depth):
        renderer = ('<device><deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>'
                    f'<friendlyName>Box</friendlyName><UDN>uuid:box</UDN><deviceList>{renderer}</deviceList></device>')
    xmlns = ' xmlns="urn:schemas-upnp-org:device-1-0"' if namespace else ""
    return f'<?xml version="1.0"?><root{xmlns}>{renderer}</root>'


def _declared(encoding, name: str = "Caf\xe9") -> str:
    """A renderer's description under an XML declaration of `encoding` (None:
    no declaration)."""
    body = _nested(0, name=name).split("?>", 1)[1]
    return (f'<?xml version="1.0" encoding="{encoding}"?>' if encoding else "") + body


def _zones() -> str:
    """A media server embedding two renderers of one type."""
    zone = ('<device><deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>'
            '<friendlyName>Zone {0}</friendlyName><UDN>uuid:zone-{0}</UDN></device>')
    return ('<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
            '<deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType><friendlyName>Box</friendlyName>'
            f'<UDN>uuid:box</UDN><deviceList>{zone.format("a")}{zone.format("b")}</deviceList></device></root>')


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

    def raw(body: bytes, content_type: str):
        return lambda r: asyncio.sleep(0, web.Response(body=body, headers={"Content-Type": content_type}))

    async def slow(_request):
        await asyncio.sleep(3)
        return await text(_description())

    async def streamed(request, body: bytes, *, length=None, pause=0.0):
        """`body` in two writes, `pause` apart, with `length` declared or
        chunked."""
        response = web.StreamResponse(headers={"Content-Type": "text/xml"})
        if length is not None:
            response.content_length = length
        await response.prepare(request)
        half = len(body) // 2
        await response.write(body[:half])
        await asyncio.sleep(pause)
        await response.write(body[half:])
        return response

    routes = {
        "/plain.xml": lambda r: text(_description()),
        "/moved.xml": lambda r: asyncio.sleep(0, web.HTTPFound("/plain.xml")),
        "/packed.xml": lambda r: asyncio.sleep(0, web.Response(
            body=gzip.compress(_description().encode()), content_type="text/xml",
            headers={"Content-Encoding": "gzip"})),
        "/large.xml": lambda r: streamed(r, _description(padding=300 * 1024).encode()),
        "/declared.xml": lambda r: streamed(r, _description().encode(),
                                            length=dlna_backend.ANSWER_BYTES + 1),
        "/pieces.xml": lambda r: streamed(r, _description().encode(),
                                          length=len(_description().encode()), pause=0.2),
        "/embedded.xml": lambda r: text(_nested(1)),
        "/deep.xml": lambda r: text(_nested(2)),
        "/bare.xml": lambda r: text(_nested(0, namespace=False)),
        "/noisy.xml": lambda r: text(_nested(0, name="Shel\u2028f\x85\t\n" + "x" * 300)),
        "/utf8.xml": raw(_declared("UTF-8").encode(), "text/xml"),
        "/latin1.xml": raw(_declared("ISO-8859-1").encode("latin-1"), "text/xml"),
        "/labelled.xml": raw(_declared("UTF-8").encode("latin-1"), "text/xml; charset=ISO-8859-1"),
        "/undeclared.xml": raw(_declared(None).encode("latin-1"), "text/xml; charset=ISO-8859-1"),
        "/sjis.xml": raw(_declared("Shift_JIS", "\u68da").encode("shift_jis"), "text/xml; charset=Shift_JIS"),
        "/sjis-unlabelled.xml": raw(_declared("Shift_JIS", "Shelf").encode(), "text/xml"),
        "/unknown-charset.xml": raw(_declared("UTF-8").encode(), "text/xml; charset=x-none"),
        "/quoted-charset.xml": raw(_declared("UTF-8").encode("latin-1"), "text/xml; charset='latin1'"),
        "/param-charset.xml": raw(_declared("UTF-8").encode("latin-1"), 'text/xml; foo="a;charset=latin1"'),
        "/punycode.xml": raw(b"-" + b"9" * 4096, "text/xml; charset=punycode"),
        "/zlib.xml": raw(_declared("UTF-8").encode(), "text/xml; charset=zlib"),
        "/zones.xml": lambda r: text(_zones()),
        "/doubled.xml": lambda r: text(_nested(0).replace(
            "<UDN>uuid:shelf-1</UDN>", "<UDN>uuid:shelf-1</UDN><UDN>uuid:shelf-2</UDN>")),
        "/udn-tab.xml": lambda r: text(_nested(0).replace("uuid:shelf-1", "uuid:shelf\t1")),
        "/udn-long.xml": lambda r: text(_nested(0).replace("uuid:shelf-1", "uuid:" + "1" * 124)),
        "/many.xml": lambda r: text(_description(services=24)),
        "/big-service.xml": lambda r: text(_description(services=1).replace("/scpd0.xml", "/big-scpd.xml")),
        "/moved-service.xml": lambda r: text(_description(services=1).replace("/scpd0.xml", "/moved-scpd.xml")),
        "/big-scpd.xml": lambda r: text(SCPD.replace("</scpd>", f'<!--{"p" * 300 * 1024}--></scpd>')),
        "/moved-scpd.xml": lambda r: asyncio.sleep(0, web.HTTPFound("/scpd0.xml")),
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


def test_a_description_is_read_whole_plainly_and_bounded(device):
    base, hits = device
    assert _describe(f"{base}/plain.xml")["udn"] == "uuid:shelf-1"
    # a description that arrives in pieces, and one of a device of many services
    assert _describe(f"{base}/pieces.xml")["udn"] == "uuid:shelf-1"
    assert _describe(f"{base}/many.xml")["udn"] == "uuid:shelf-1"
    for shape, said in (("moved", "answered 302"), ("packed", "compressed"),
                        ("large", "past its size"), ("declared", "past its size")):
        with pytest.raises(dlna_backend.AnswerRefused, match=said):
            _describe(f"{base}/{shape}.xml")
    assert hits["/plain.xml"] == 1                     # the redirect was not followed
    assert not [p for p in hits if p.startswith("/scpd")]   # no service document is read


def test_the_descriptions_answer_within_one_deadline_off_the_servers_loop(device, monkeypatch):
    base, _ = device
    monkeypatch.setattr(player, "_DESCRIBE_DEADLINE_S", 0.5)
    started = time.monotonic()
    described = player._describe_all([f"{base}/slow.xml", f"{base}/plain.xml", f"{base}/large.xml"])
    assert time.monotonic() - started < 2.5
    assert list(described) == [f"{base}/plain.xml"]


def test_a_multicast_location_is_described_as_plain_http_at_its_source():
    assert player._multicast_location("http://192.0.2.5:1400/d.xml", "192.0.2.5") == "http://192.0.2.5:1400/d.xml"
    assert player._multicast_location("http://10.9.9.9:1400/d.xml", "192.0.2.5") == "http://192.0.2.5:1400/d.xml"
    assert player._multicast_location("http://10.9.9.9/d.xml", "fd00::5") == "http://[fd00::5]/d.xml"
    for location, source in (("https://192.0.2.5/d.xml", "192.0.2.5"), ("file:///d.xml", "192.0.2.5"),
                             ("http://192.0.2.5/d.xml", None), ("http://192.0.2.5:x/d.xml", "192.0.2.5")):
        assert player._multicast_location(location, source) is None, location


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


def test_a_multicast_answer_is_described_where_it_came_from(device, quiet_scan, monkeypatch):
    base, hits = device
    port = base.rsplit(":", 1)[1]

    async def answers(callback, **_kw):
        await callback({"location": f"http://192.0.2.9:{port}/plain.xml", "usn": "uuid:a", "_host": "127.0.0.1"})
        await callback({"location": f"https://127.0.0.1:{port}/plain.xml", "usn": "uuid:b", "_host": "127.0.0.1"})

    async def no_sweep(found, found_hqp):
        return None
    monkeypatch.setattr(quiet_scan, "async_search", answers)
    monkeypatch.setattr(player, "_unicast_sweep", no_sweep)
    result = asyncio.run(player._scan_run())
    assert [r["location"] for r in result["renderers"]] == [f"http://127.0.0.1:{port}/plain.xml"]
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
    answers = {"<GetInfo/>": b'<GetInfo name="Stu\x00d\tio\x07\xc2\x85\xe2\x80\xa8" '
                             b'product="Signalyst HQPlayer Desktop 6"/>',
               "<LibraryGetHash/>": '<LibraryGetHash value="été"/>'.encode()}
    limits = {}

    def command(host, port, xml, timeout, limit=None):
        limits[xml] = limit
        return answers[xml]
    monkeypatch.setattr(hqp_library, "_command", command)
    assert hqp_library.get_info("192.0.2.7", 4321)["name"] == "Studio"
    with pytest.raises(ConnectionError):
        hqp_library.library_hash("192.0.2.7", 4321)
    answers["<LibraryGetHash/>"] = b'<LibraryGetHash value="9f86d081884c7d65"/>'
    assert hqp_library.library_hash("192.0.2.7", 4321) == "9f86d081884c7d65"
    answers["<LibraryGetHash/>"] = b'<LibraryGetHash value=""/>'          # a Desktop without a library
    assert hqp_library.library_hash("192.0.2.7", 4321) == ""
    assert limits == {"<GetInfo/>": hqp_library._FACTS_LIMIT, "<LibraryGetHash/>": hqp_library._FACTS_LIMIT}
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


def test_an_hqplayer_without_a_library_is_known_by_no_hash(monkeypatch):
    monkeypatch.setattr(hqp_library, "get_info",
                        lambda host, port: {"name": "Desk", "product": "Signalyst HQPlayer Desktop 6"})
    monkeypatch.setattr(hqp_library, "library_hash", lambda host, port: "")
    monkeypatch.setattr(hqp_library, "endpoint_by_address", lambda host, port: None)
    asked = []
    monkeypatch.setattr(hqp_library, "db_query", lambda *a, **k: asked.append(a) or [{"id": 1}])
    assert hqp_library.resolve_endpoint("192.0.2.7", 4321)[:2] == (None, None)
    assert asked == []


def test_a_renderer_is_listed_where_the_attach_finds_one(device):
    base, _hits = device
    assert _describe(f"{base}/embedded.xml")["udn"] == "uuid:shelf-1"          # a server's own renderer
    with pytest.raises(player.HTTPException):
        _describe(f"{base}/deep.xml")                                         # deeper than the attach looks
    with pytest.raises(player.DescriptionRefused, match="no device"):
        _describe(f"{base}/bare.xml")                                         # outside the UPnP namespace


def test_a_devices_words_are_plain_and_its_udn_whole_or_refused(device):
    base, _hits = device
    assert _describe(f"{base}/noisy.xml")["name"] == "Shelf" + "x" * 195
    for shape in ("udn-tab", "udn-long"):
        with pytest.raises(player.DescriptionRefused, match="UDN"):
            _describe(f"{base}/{shape}.xml")


def test_a_description_in_a_codec_that_is_no_text_encoding_is_refused_undecoded(device):
    """The device names the codec: punycode decodes in quadratic time on the
    event loop, zlib is no text encoding at all."""
    base, _hits = device
    for shape in ("punycode", "zlib"):
        with pytest.raises(dlna_backend.AnswerRefused, match="encoding the node does not read"):
            _describe(f"{base}/{shape}.xml")


def _listed(url):
    """The UDN of the renderer the scan lists at `url`, or None."""
    from xml.etree.ElementTree import ParseError
    try:
        return player.udn_key(_describe(url)["udn"])
    except (dlna_backend.AnswerRefused, player.HTTPException, ParseError):
        return None


def _attached(url):
    """What the attach makes of the description at `url` — its requester,
    the library's factory: the UDN of the renderer among the device and
    those it embeds directly — where the attach looks for the one the scan
    listed — or None."""
    from async_upnp_client.client_factory import UpnpFactory
    from async_upnp_client.exceptions import UpnpError

    async def run():
        try:
            device = await UpnpFactory(dlna_backend._BoundedRequester(timeout=5),
                                       non_strict=True).async_create_device(url)
        except UpnpError:
            return None
        found = [d for d in (device, *device.embedded_devices.values())
                 if ":MediaRenderer:" in d.device_type.strip()]
        return player.udn_key(found[0].udn) if found else None
    return asyncio.run(run())


@pytest.mark.parametrize("shape, udn", [
    ("utf8", "uuid:shelf-1"), ("latin1", None), ("labelled", "uuid:shelf-1"),
    ("undeclared", "uuid:shelf-1"), ("sjis", "uuid:shelf-1"), ("sjis-unlabelled", "uuid:shelf-1"),
    ("unknown-charset", "uuid:shelf-1"), ("quoted-charset", "uuid:shelf-1"), ("param-charset", "uuid:shelf-1"),
    ("embedded", "uuid:shelf-1"), ("deep", None), ("bare", None),
    ("zones", "uuid:zone-b"), ("doubled", "uuid:shelf-1"),
])
def test_the_scan_reads_a_description_as_the_attach_does(device, shape, udn):
    """Its charset as the answer names it, the declaration not read; one
    embedded device of a type, the last; each word from its first element."""
    base, _hits = device
    assert (_listed(f"{base}/{shape}.xml"), _attached(f"{base}/{shape}.xml")) == (udn, udn)


def _requested(url):
    """The attach's requester asked for `url` as the library asks: its
    status and its text."""
    from async_upnp_client.const import HttpRequest

    async def run():
        answer = await dlna_backend._BoundedRequester(timeout=5).async_http_request(
            HttpRequest("GET", url, {}, None))
        return answer.status_code, answer.body
    return asyncio.run(run())


def test_the_attach_reads_a_renderers_answers_plainly_and_bounded(device):
    """Every answer the attach reads — a description, a service's document,
    a SOAP or GENA answer — as the scan reads a description: no redirect
    followed, nothing compressed, so many bytes, in a text encoding."""
    from async_upnp_client.exceptions import UpnpCommunicationError
    base, hits = device
    status, body = _requested(f"{base}/plain.xml")
    assert status == 200 and "uuid:shelf-1" in body
    assert _requested(f"{base}/moved.xml")[0] == 302
    assert hits["/plain.xml"] == 1                     # the redirect was not followed
    for shape, said in (("packed", "compressed"), ("large", "past its size"),
                        ("declared", "past its size"), ("punycode", "encoding"), ("zlib", "encoding")):
        with pytest.raises(UpnpCommunicationError, match=said):
            _requested(f"{base}/{shape}.xml")


def test_the_attach_takes_no_renderer_whose_service_document_is_past_the_bound(device):
    """A service document is an answer as any other: one past the bound, or
    one that redirects, fails the attach, and the redirect is not followed."""
    base, hits = device
    assert _attached(f"{base}/many.xml") == "uuid:shelf-1"
    assert _attached(f"{base}/big-service.xml") is None
    before = hits.get("/scpd0.xml", 0)
    assert _attached(f"{base}/moved-service.xml") is None
    assert hits.get("/scpd0.xml", 0) == before


def test_a_location_that_is_no_url_is_its_answers_failure_alone(device, quiet_scan, monkeypatch):
    for location in ("http://192.0.2.5:port/d.xml", "http://[::1/d.xml", "http:///d.xml"):
        assert player._rehost(location, "192.0.2.9") is None, location
        assert player._multicast_location(location, "192.0.2.9") is None, location
    base, _hits = device
    port = base.rsplit(":", 1)[1]

    async def answers(callback, **_kw):
        await callback({"location": "http://[::1/d.xml", "usn": "uuid:x", "_host": "127.0.0.1"})
        await callback({"location": f"http://127.0.0.1:{port}/plain.xml", "usn": "uuid:a", "_host": "127.0.0.1"})

    async def no_sweep(found, found_hqp):
        return None
    monkeypatch.setattr(quiet_scan, "async_search", answers)
    monkeypatch.setattr(player, "_unicast_sweep", no_sweep)
    result = asyncio.run(player._scan_run())
    assert [r["location"] for r in result["renderers"]] == [f"http://127.0.0.1:{port}/plain.xml"]


def test_a_probe_that_fails_is_its_addresss_failure_alone(monkeypatch):
    import ipaddress

    def probe(ip):
        if ip == "192.0.2.1":
            raise ValueError("an answer of no shape")
        return [("uuid:b", f"http://{ip}:80/d.xml")], {"name": "box", "product": "Signalyst HQPlayer Embedded 6"}
    monkeypatch.setattr(player, "_lan_subnets", lambda: [ipaddress.ip_network("192.0.2.0/30")])
    monkeypatch.setattr(player, "_tailnet_peers", lambda: [])
    monkeypatch.setattr(player, "_sweep_probe", probe)
    found, found_hqp = {}, {}
    asyncio.run(player._unicast_sweep(found, found_hqp))
    assert list(found.values()) == ["http://192.0.2.2:80/d.xml"] and list(found_hqp) == ["192.0.2.2"]
