"""Outside covers — a phantom album's Cover Art Archive front, a streamed
track's provider art — fetched by the node (covers.fetch_art, by
covers.ART_SOURCES) and served by it (routers/covers.py). The image hosts are
scripted: httpx.MockTransport behind the routes, a stub of fetch_art for the
breaker. No DB, no network. Run:

    python -m pytest test_covers_outside_art.py -q
"""

import asyncio
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from covers import _ART_MAX_BYTES, fetch_art
from routers import covers

RG = "f5093c06-23e3-404f-aeaa-40f72885ee3a"
CAA = f"https://coverartarchive.org/release-group/{RG}/front-500"
MD5 = "5718f7c81c27e0b2417e2a4c45224f8a"
DEEZER = f"https://cdn-images.dzcdn.net/images/cover/{MD5}/1000x1000-000000-80-0-0.jpg"
YOUTUBE = "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
MONTH = "public, max-age=2592000"
DAY = "public, max-age=86400"
DEFERRED = "public, max-age=60"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    covers._art_cache.clear()
    covers._art_misses.clear()
    covers._art_inflight.clear()
    covers._refused_hosts.clear()
    monkeypatch.setattr(covers, "_art_cache_bytes", 0)
    monkeypatch.setattr(covers, "_art_failures", {})
    monkeypatch.setattr(covers, "_art_open", set())


@pytest.fixture
def hosts(monkeypatch):
    """The image hosts, scripted: `answer[url]` is the response (or a list,
    one per ask) for that URL — an unscripted URL fails the test.
    `asked` lists every URL fetched, in order; `headers` what each carried."""
    class Hosts:
        answer, asked, headers = {}, [], []

    def handle(request):
        Hosts.asked.append(str(request.url))
        Hosts.headers.append(dict(request.headers))
        reply = Hosts.answer[str(request.url)]
        return reply.pop(0) if isinstance(reply, list) else reply

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handle), **kw))
    return Hosts


@pytest.fixture
def node():
    app = FastAPI()
    app.include_router(covers.router)
    with TestClient(app) as client:
        yield client


def _image(content=JPEG, mime="image/jpeg"):
    return httpx.Response(200, headers={"content-type": mime}, content=content)


def _art(node, url):
    return node.get("/api/covers/art", params={"url": url})


@pytest.mark.parametrize("url", [DEEZER, YOUTUBE], ids=["deezer", "youtube"])
def test_a_provider_cover_comes_through_the_node_and_keeps_a_month(node, hosts, url):
    hosts.answer[url] = _image()
    browser = {"Cookie": "session=1", "Referer": "http://192.0.2.7:8800/",
               "User-Agent": "Browser/1", "X-Forwarded-For": "192.0.2.7"}
    first = node.get("/api/covers/art", params={"url": url}, headers=browser)
    again = _art(node, url)
    assert first.status_code == again.status_code == 200
    assert first.content == again.content == JPEG
    assert first.headers["content-type"] == "image/jpeg"
    assert first.headers["cache-control"] == MONTH
    assert first.headers["x-content-type-options"] == "nosniff"
    assert hosts.asked == [url]                     # the second came from the node's cache
    sent = hosts.headers[0]
    assert not {"cookie", "referer", "x-forwarded-for"} & set(sent)
    assert "Browser/1" not in sent.get("user-agent", "")


@pytest.mark.parametrize("url", [
    "https://images.example.com/cover.jpg",
    DEEZER.replace("https://", "http://"),
    "https://cdnt-preview.dzcdn.net/api/1/1/d/6/b/0/d6bc80aadfa1d7625d59a6620f229371.mp3",
    DEEZER.replace("dzcdn.net", "dzcdn.net.example.com"),
    DEEZER.replace("1000x1000", "١٠٠٠x١٠٠٠"),
    YOUTUBE.replace("i.ytimg.com", "i.ytimg.com@example.com"),
    YOUTUBE + "?w=1",
    f"https://coverartarchive.org/release/{RG}/front",
    "http://localhost:8800/api/settings",
    "/\\images.example.com/pixel.gif",
], ids=["another host", "not https", "the provider's audio", "a lookalike host", "other digits",
        "userinfo", "a query", "another caa form", "the node itself", "a backslash"])
def test_a_url_off_the_list_is_no_art_and_nothing_is_fetched(node, hosts, url):
    r = _art(node, url)
    assert r.status_code == 404
    assert r.headers["cache-control"] == DAY
    assert hosts.asked == []


def test_a_caa_front_follows_its_own_chain_through_archive_org(node, hosts):
    store = "https://archive.org/download/mbid-1/mbid-1-2_thumb500.jpg"
    data_node = "https://dn000000.ca.archive.org/0/items/mbid-1/mbid-1-2_thumb500.jpg"
    hosts.answer[CAA] = httpx.Response(307, headers={"location": store})
    hosts.answer[store] = httpx.Response(302, headers={"location": data_node})
    hosts.answer[data_node] = _image()
    r = node.get(f"/api/covers/caa/{RG}")
    assert (r.status_code, r.content, r.headers["cache-control"]) == (200, JPEG, MONTH)
    assert hosts.asked == [CAA, store, data_node]
    assert _art(node, CAA).content == JPEG and len(hosts.asked) == 3    # one cover, one entry


def test_a_caa_redirect_within_cover_art_archive_is_followed(node, hosts):
    release = "https://coverartarchive.org/release/0f0e0d0c-0000-4000-8000-000000000001/1-500.jpg"
    hosts.answer[CAA] = httpx.Response(307, headers={"location": release.removeprefix(
        "https://coverartarchive.org")})
    hosts.answer[release] = _image()
    assert _art(node, CAA).content == JPEG
    assert hosts.asked == [CAA, release]


def test_a_redirect_off_the_sources_hosts_is_no_art(node, hosts):
    """Deezer redirects a cover it has not to its own blank placeholder; a
    CAA redirect may lead only to Cover Art Archive or the Internet Archive."""
    blank = "/images/cover/d41d8cd98f00b204e9800998ecf8427e/1000x1000-000000-80-0-0.jpg"
    hosts.answer[DEEZER] = httpx.Response(302, headers={"location": blank})
    hosts.answer[CAA] = httpx.Response(307, headers={"location": "https://images.example.com/x.jpg"})
    for url in (DEEZER, CAA):
        r = _art(node, url)
        assert (r.status_code, r.headers["cache-control"]) == (404, DAY)
    assert hosts.asked == [DEEZER, CAA]           # neither redirect was followed


@pytest.mark.parametrize("reply, kept", [
    (_image(b"<script>alert(1)</script>", "text/html"), DEFERRED),
    (_image(b"<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'/>", "image/svg+xml"), DEFERRED),
    (_image(b""), DEFERRED),
    (_image(b"\xff" * (_ART_MAX_BYTES + 1)), DAY),
], ids=["html", "svg", "empty", "over the cap"])
def test_only_a_raster_image_within_the_cap_is_served(node, hosts, reply, kept):
    """Anything but an image is the host's trouble (an interstitial, a
    captive portal) and is asked again; an image over the cap is no art."""
    hosts.answer[DEEZER] = reply
    r = _art(node, DEEZER)
    assert (r.status_code, r.content, r.headers["cache-control"]) == (404, b"", kept)


def test_a_cover_the_host_has_not_is_no_art_for_a_day(node, hosts):
    hosts.answer[YOUTUBE] = httpx.Response(404, headers={"content-type": "image/jpeg"}, content=b"grey")
    for _ in range(2):
        r = _art(node, YOUTUBE)
        assert (r.status_code, r.content, r.headers["cache-control"]) == (404, b"", DAY)
    assert hosts.asked == [YOUTUBE]


def test_upstream_trouble_defers_a_minute_and_is_asked_again(node, hosts):
    hosts.answer[YOUTUBE] = [httpx.Response(503), _image()]
    r = _art(node, YOUTUBE)
    assert (r.status_code, r.headers["cache-control"]) == (404, DEFERRED)
    assert _art(node, YOUTUBE).status_code == 200
    assert hosts.asked == [YOUTUBE, YOUTUBE]


def test_the_node_keeps_a_miss_a_day_and_a_cover_a_month(node, hosts):
    hosts.answer[YOUTUBE] = [httpx.Response(404), _image()]
    hosts.answer[DEEZER] = [_image(), _image(b"\xff\xd8 new")]
    assert _art(node, YOUTUBE).status_code == 404
    assert _art(node, DEEZER).content == JPEG
    covers._art_misses[YOUTUBE] = time.monotonic() - 1           # its day is over
    until, mime, body = covers._art_cache[DEEZER]
    covers._art_cache[DEEZER] = (time.monotonic() - 1, mime, body)   # and its month
    assert _art(node, YOUTUBE).content == JPEG
    assert _art(node, DEEZER).content == b"\xff\xd8 new"
    assert hosts.asked == [YOUTUBE, DEEZER, YOUTUBE, DEEZER]
    assert covers._art_cache_bytes == len(JPEG) + len(b"\xff\xd8 new")


def test_the_cache_keeps_to_its_byte_budget_and_misses_apart(monkeypatch):
    monkeypatch.setattr(covers, "_ART_CACHE_BYTES", 25)
    monkeypatch.setattr(covers, "_ART_MISS_ENTRIES", 3)
    for url in "abc":
        covers._art_remember(url, (200, "image/jpeg", b"x" * 10))
    for n in range(10):
        covers._art_remember(f"miss{n}", (404, "", b""))
    assert list(covers._art_cache) == ["b", "c"]          # the budget, not the misses, evicted "a"
    assert covers._art_cache_bytes == 20
    assert list(covers._art_misses) == ["miss7", "miss8", "miss9"]


class _Upstream:
    """Scripted stand-in for fetch_art: `outcomes` is consumed per call (an
    int status, or (asyncio.Event, status) to hold the call until released)."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def __call__(self, source, url, quiet=False):
        self.calls.append(url)
        step = self.outcomes.pop(0)
        if isinstance(step, tuple):
            gate, status = step
            await gate.wait()
        else:
            status = step
        if status == 200:
            return 200, "image/jpeg", b"jpeg"
        return status, "", b""


def _run(coro):
    return asyncio.run(coro)


def test_trips_after_consecutive_failures_then_probes_one_at_a_time(monkeypatch):
    async def scenario():
        gate = asyncio.Event()
        up = _Upstream(502, 502, 502, (gate, 502))
        monkeypatch.setattr(covers, "fetch_art", up)

        for url in ("a", "b"):
            assert (await covers._art_lookup("caa", url))[0] == 502
        assert not covers._art_open
        assert (await covers._art_lookup("caa", "c"))[0] == 502
        assert covers._art_open == {"caa"}

        probe = asyncio.create_task(covers._art_lookup("caa", "d"))
        await asyncio.sleep(0)               # the probe is in flight
        deferred = await asyncio.gather(*(covers._art_lookup("caa", url) for url in "efg"))
        assert [d[0] for d in deferred] == [503, 503, 503]
        assert up.calls == ["a", "b", "c", "d"]   # nobody but the probe reached upstream

        gate.set()
        assert (await probe)[0] == 502
        assert covers._art_open == {"caa"}

    _run(scenario())


def test_burst_settles_then_first_definite_answer_closes(monkeypatch):
    async def scenario():
        gate = asyncio.Event()
        up = _Upstream(*[(gate, 502)] * 6, (asyncio.Event(), 200), 404)
        monkeypatch.setattr(covers, "fetch_art", up)

        burst = [asyncio.create_task(covers._art_lookup("caa", url)) for url in "abcdef"]
        await asyncio.sleep(0)
        assert len(covers._art_inflight) == 6    # closed breaker: the burst went out
        gate.set()
        assert {r[0] for r in await asyncio.gather(*burst)} == {502}
        assert covers._art_open == {"caa"}

        probe_gate = up.outcomes[0][0]
        probe = asyncio.create_task(covers._art_lookup("caa", "g"))
        await asyncio.sleep(0)
        assert (await covers._art_lookup("caa", "h"))[0] == 503
        probe_gate.set()
        assert (await probe)[0] == 200
        assert not covers._art_open
        assert "g" in covers._art_cache

        assert (await covers._art_lookup("caa", "h"))[0] == 404   # closed again: fetched
        assert up.calls[-1] == "h"

    _run(scenario())


def test_sporadic_failures_do_not_trip(monkeypatch):
    async def scenario():
        up = _Upstream(502, 200, 502, 502, 404, 502, 502)
        monkeypatch.setattr(covers, "fetch_art", up)
        for url in "abcdefg":
            await covers._art_lookup("caa", url)
        assert not covers._art_open
        assert covers._art_failures["caa"] == 2

    _run(scenario())


def test_cached_answers_are_served_while_open(monkeypatch):
    async def scenario():
        gate = asyncio.Event()
        up = _Upstream(200, 502, 502, 502, (gate, 502))
        monkeypatch.setattr(covers, "fetch_art", up)
        assert (await covers._art_lookup("caa", "cached"))[0] == 200
        for url in "abc":
            await covers._art_lookup("caa", url)
        assert covers._art_open == {"caa"}
        probe = asyncio.create_task(covers._art_lookup("caa", "d"))
        await asyncio.sleep(0)
        assert (await covers._art_lookup("caa", "cached"))[0] == 200
        gate.set()
        await probe

    _run(scenario())


def test_a_source_down_holds_back_no_other(monkeypatch):
    async def scenario():
        gate = asyncio.Event()
        up = _Upstream(502, 502, 502, (gate, 502), 200)
        monkeypatch.setattr(covers, "fetch_art", up)
        for url in "abc":
            await covers._art_lookup("caa", url)
        probe = asyncio.create_task(covers._art_lookup("caa", "d"))
        await asyncio.sleep(0)
        assert (await covers._art_lookup("caa", "e"))[0] == 503       # CAA's probe is out
        assert (await covers._art_lookup("deezer", "f"))[0] == 200    # Deezer is asked all the same
        assert covers._art_open == {"caa"}
        gate.set()
        await probe

    _run(scenario())


def test_the_node_asks_a_source_so_much_at_once_and_defers_the_rest(monkeypatch):
    """A source that stalls before its breaker has heard enough to trip holds
    its own share of the fetches, never the others'."""
    async def scenario():
        gate = asyncio.Event()
        up = _Upstream(*[(gate, 200)] * covers._ART_MAX_FETCHES, 200)
        monkeypatch.setattr(covers, "fetch_art", up)
        held = [asyncio.create_task(covers._art_lookup("caa", f"rg{n}"))
                for n in range(covers._ART_MAX_FETCHES)]
        await asyncio.sleep(0)
        assert (await covers._art_lookup("caa", "one more"))[0] == 503
        await asyncio.sleep(0)                   # the held fetches reach the host
        assert len(up.calls) == covers._ART_MAX_FETCHES
        assert (await covers._art_lookup("deezer", "art"))[0] == 200     # another source's share
        gate.set()
        assert {r[0] for r in await asyncio.gather(*held)} == {200}
        assert not covers._art_failures.get("caa")           # deferring is no failure of the host

    _run(scenario())


def test_a_chain_that_outlasts_its_deadline_is_trouble(monkeypatch):
    """The deadline is the whole chain's, whatever each hop's own timeout."""
    async def stall(request):
        await asyncio.sleep(5)
        return _image()

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(stall), **kw))
    started = time.monotonic()
    assert asyncio.run(fetch_art("youtube", YOUTUBE, deadline=0.2)) == (502, "", b"")
    assert time.monotonic() - started < 2


def test_a_browser_that_leaves_mid_fetch_strands_no_one(monkeypatch):
    async def scenario():
        gate = asyncio.Event()
        up = _Upstream((gate, 200))
        monkeypatch.setattr(covers, "fetch_art", up)
        first = asyncio.create_task(covers._art_lookup("deezer", "a"))
        await asyncio.sleep(0)
        second = asyncio.create_task(covers._art_lookup("deezer", "a"))
        await asyncio.sleep(0)
        first.cancel()
        gate.set()
        assert (await asyncio.wait_for(second, 2))[0] == 200
        assert "a" in covers._art_cache
        assert up.calls == ["a"]
        with pytest.raises(asyncio.CancelledError):
            await first

    _run(scenario())
