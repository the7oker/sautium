"""
MediaProxy — its fetch lane, its entries, its serving — pure logic, a stub
provider, no network. Run inside the backend container:

    python -m pytest test_streaming_proxy.py -q
"""

import threading
import types

import pytest

from playback.dlna_backend import DlnaBackend
from playback.queue import CanonicalQueue, QueueItem
from streaming.base import FetchedAudio, ProviderManifest, StreamProvider, TrackQuery
from streaming.proxy import MediaProxy


class _GatedProvider(StreamProvider):
    """Every fetch blocks until its title is released — the pipe's order and
    what is in flight become observable."""
    manifest = ProviderManifest(id="stub", name="Stub", kind="direct_url", lossless=True)

    def __init__(self):
        self.gates: dict = {}
        self.started: list = []
        self.started_ev = threading.Condition()

    def fetch(self, query: TrackQuery) -> FetchedAudio:
        with self.started_ev:
            self.started.append(query.title)
            self.started_ev.notify_all()
        self.gates.setdefault(query.title, threading.Event()).wait(5)
        return FetchedAudio(data=query.title.encode() * 8, mime="audio/flac", lossless=True)

    def release(self, *titles):
        for t in titles:
            self.gates.setdefault(t, threading.Event()).set()

    def wait_started(self, title):
        with self.started_ev:
            assert self.started_ev.wait_for(lambda: title in self.started, timeout=5)


class _Instant(StreamProvider):
    """Answers at once; the manifest flags and what it serves are the test's."""

    def __init__(self, pid, *, excerpt=False, demo_limited=False, seconds=None):
        self.manifest = ProviderManifest(id=pid, name=pid, kind="direct_url", lossless=False,
                                         excerpt=excerpt, demo_limited=demo_limited)
        self._seconds = seconds
        self.fetched: list = []

    def fetch(self, query: TrackQuery) -> FetchedAudio:
        self.fetched.append(query.title)
        return FetchedAudio(data=b"x" * 16, mime="audio/mpeg", lossless=False,
                            excerpt=self.manifest.excerpt, seconds=self._seconds)


def _q(title):
    return TrackQuery(artist="A", title=title, album="Alb", duration=100.0,
                      track_id=f"id-{title}")


def _mint(proxy, prov, *titles):
    return [proxy.mint(_q(t), [(prov, None)]) for t in titles]


def _proxy():
    return MediaProxy(port=0, advertised_host="127.0.0.1", file_token_key=b"test-key")


def test_a_minted_token_fetches_nothing_until_it_is_wanted():
    """The lane has no list of its own: a token is fetched when the queue's
    picker hands it over or a consumer asks for it — never at its minting."""
    prov = _Instant("p")
    proxy = _proxy()
    tok = proxy.mint(_q("a1"), [(prov, None)])
    proxy.wake()
    assert proxy.state(tok) == ("waiting", None, None) and prov.fetched == []
    assert proxy.wait_ready(tok, timeout=5).audio is not None
    assert proxy.state(tok) == ("ready", None, None) and prov.fetched == ["a1"]


def test_the_lane_takes_the_pickers_wants_in_its_order_and_a_demand_first():
    """Each time it is free the worker takes an on-demand read first, then
    the picker's next want — so an order the picker changes is the order the
    fetches go in."""
    prov = _GatedProvider()
    proxy = _proxy()
    toks = _mint(proxy, prov, "a1", "a2", "a3", "a4")
    order = [toks[0], toks[2], toks[1]]
    proxy.picker = lambda: next((t for t in order if proxy.wanted(t)), None)
    proxy.wake()
    prov.wait_started("a1")
    order[:] = [toks[1], toks[2]]           # a2 moved nearer while a1 fetches
    proxy.demand(toks[3])                   # a consumer waits on a4
    prov.release("a1", "a2", "a3", "a4")
    for t in toks:
        assert proxy.wait_ready(t, timeout=5).audio is not None
    assert prov.started == ["a1", "a4", "a2", "a3"]


def test_a_wake_while_the_picker_runs_is_not_lost():
    prov = _Instant("p")
    proxy = _proxy()
    tok = proxy.mint(_q("a1"), [(prov, None)])
    asked = threading.Event()
    wanted = []

    def picker():
        if not asked.is_set():
            asked.set()
            proxy.wake()            # the queue moved as the picker read it
            return None
        return next((t for t in wanted if proxy.wanted(t)), None)
    proxy.picker = picker
    proxy.wake()
    assert asked.wait(5)
    wanted.append(tok)
    proxy.wake()
    assert proxy._peek(tok).ready.wait(5) and prov.fetched == ["a1"]


class _Cooperative(StreamProvider):
    """Reads its fetch's context: reports progress, stops on the cancel."""
    manifest = ProviderManifest(id="coop", name="Coop", kind="direct_url", lossless=False)

    def __init__(self):
        self.began = threading.Event()
        self.stopped = threading.Event()

    def fetch(self, query):
        from streaming.base import FetchCancelled, fetching
        ctx = fetching()
        ctx.progress(0.42)
        self.began.set()
        ctx.cancelled.wait(5)
        self.stopped.set()
        raise FetchCancelled("stopped")


def test_a_cancel_stops_a_running_fetch_at_the_source_and_the_lane_goes_on():
    coop, nxt = _Cooperative(), _Instant("next")
    proxy = _proxy()
    tok = proxy.mint(_q("a1"), [(coop, None), (nxt, None)])
    other = proxy.mint(_q("a2"), [(nxt, None)])
    proxy.demand(tok)
    assert coop.began.wait(5)
    assert proxy.state(tok) == ("buffering", 0.42, None)
    e = proxy._peek(tok)
    assert proxy.cancel(tok) is True
    assert coop.stopped.wait(5) and e.ready.wait(5)
    assert proxy._peek(tok) is None and nxt.fetched == []     # the chain does not cascade
    with pytest.raises(KeyError):
        proxy.wait_ready(tok, timeout=1)
    assert proxy.wait_ready(other, timeout=5).audio is not None


def test_a_provider_that_ignores_the_cancel_is_waited_out_and_its_bytes_discarded():
    prov = _GatedProvider()
    landed = []
    proxy = _proxy()
    proxy.track_ready_hooks.append(lambda e: landed.append(e.token))
    tok, after = _mint(proxy, prov, "a1", "a2")
    proxy.demand(tok)
    prov.wait_started("a1")
    e = proxy._peek(tok)
    assert proxy.cancel(tok) is True and e.ready.is_set()   # its waiters woken at once
    proxy.demand(after)
    prov.release("a1", "a2")                                # a1's download returns
    assert proxy.wait_ready(after, timeout=5).audio is not None
    assert landed == [after] and e.audio is None            # a1 never landed
    assert prov.started == ["a1", "a2"]                     # never run beside another


def test_a_cancel_of_a_fetch_not_begun_takes_it_out_of_the_lane():
    prov = _Instant("p")
    proxy = _proxy()
    tok = proxy.mint(_q("a1"), [(prov, None)])
    assert proxy.cancel(tok) is True and proxy._peek(tok) is None
    assert proxy.cancel(tok) is False                       # nothing left to cancel
    ready = proxy.mint(_q("a2"), [(prov, None)])
    proxy.wait_ready(ready, timeout=5)
    assert proxy.cancel(ready) is False and proxy._peek(ready).audio is not None


class _Refusing(StreamProvider):
    def __init__(self, pid, code):
        self.manifest = ProviderManifest(id=pid, name=pid, kind="direct_url", lossless=False)
        self._code = code

    def fetch(self, query):
        from streaming.base import Refused
        raise Refused(self._code, f"{self.manifest.id}: {self._code}")


def test_a_refusal_with_a_reason_is_told_and_the_failure_carries_the_heads_code():
    """Every link that refused with a reason is told (link_refused, the
    resolve's memory), and the failure is shown with the chain head's code;
    a plain failure carries none."""
    from streaming.base import ProviderError
    region, clip = _Refusing("lossless", "region"), _Refusing("clip", "no_clip")
    told = []
    proxy = _proxy()
    proxy.link_refused = lambda q, p, code: told.append((q.track_id, p.manifest.id, code))
    tok = proxy.mint(_q("a1"), [(region, None), (clip, None)])
    e = proxy.wait_ready(tok, timeout=5)
    assert e.audio is None and e.code == "region"
    assert proxy.state(tok) == ("failed", None, "region")
    assert told == [("id-a1", "lossless", "region"), ("id-a1", "clip", "no_clip")]

    class _Broken(_Instant):
        def fetch(self, query):
            raise ProviderError("a cut connection")
    plain = proxy.mint(_q("a2"), [(_Broken("x"), None)])
    assert proxy.wait_ready(plain, timeout=5).code is None
    assert proxy.state(plain) == ("failed", None, None)


def test_progress_is_told_in_tenths():
    """A row hears a fetch's progress at each tenth, never at every step a
    provider reports."""
    class _Steps(_Instant):
        def fetch(self, query):
            from streaming.base import fetching
            for i in range(1, 101):
                fetching().progress(i / 100)
            return super().fetch(query)
    proxy = _proxy()
    said = []
    proxy.state_hooks.append(lambda e: said.append(e.progress))
    tok = proxy.mint(_q("a1"), [(_Steps("p"), None)])
    proxy.wait_ready(tok, timeout=5)
    assert len(said) <= 13 and said[-1] == 1.0              # begin, ten tenths, the end


def test_an_excerpt_reports_the_clips_own_length():
    clip = _Instant("clip", excerpt=True, seconds=30.0)
    proxy = _proxy()
    tok = proxy.mint(_q("a1"), [(clip, None)])
    proxy.wait_ready(tok, timeout=5)
    meta = proxy.preview_meta(tok)
    assert meta["excerpt"] is True and meta["duration"] == 30.0     # not the 100 s catalog length
    assert meta["provider"] == "clip"


def test_the_link_gate_skips_refused_links_and_the_chain_cascades():
    demo, clip = _Instant("demo", demo_limited=True), _Instant("clip", excerpt=True, seconds=30.0)
    proxy = _proxy()
    proxy.link_admissible = lambda provider, query: not provider.manifest.demo_limited
    tok = proxy.mint(_q("a1"), [(demo, None), (clip, None)])
    e = proxy.wait_ready(tok, timeout=5)
    assert e.audio is not None and e.provider is clip and demo.fetched == []
    # Every link refused: a fetch failure, not a hang.
    proxy.link_admissible = lambda provider, query: False
    tok2 = proxy.mint(_q("a2"), [(demo, None), (clip, None)])
    e2 = proxy.wait_ready(tok2, timeout=5)
    assert e2.audio is None and "demo listen spent" in (e2.error or "")


def test_ram_audio_is_adopted_only_from_a_provider_the_new_chain_names():
    demo, clip = _Instant("demo", demo_limited=True), _Instant("clip", excerpt=True, seconds=30.0)
    proxy = _proxy()
    first = proxy.mint(_q("a1"), [(demo, None), (clip, None)])
    assert proxy.wait_ready(first, timeout=5).provider is demo
    # Same track, same providers: the bytes in RAM are reused, nothing fetched.
    again = proxy.mint(_q("a1"), [(demo, None), (clip, None)])
    assert proxy._peek(again).ready.is_set() and demo.fetched == ["a1"]
    # Same track, a chain without the demo channel (its listen spent): the
    # demo channel's bytes must not come along — the excerpt is fetched.
    spent = proxy.mint(_q("a1"), [(clip, None)])
    e = proxy.wait_ready(spent, timeout=5)
    assert e.provider is clip and clip.fetched == ["a1"]


def test_drop_audio_rearms_the_demo_channels_entry_and_a_wait_refetches():
    demo, clip = _Instant("demo", demo_limited=True), _Instant("clip", excerpt=True, seconds=30.0)
    proxy = _proxy()
    tok = proxy.mint(_q("a1"), [(demo, None), (clip, None)])
    proxy.wait_ready(tok, timeout=5)
    assert proxy.drop_audio("id-a1") == 1
    assert proxy.drop_audio("id-a1") == 0               # nothing left to drop
    assert not proxy._peek(tok).ready.is_set() and proxy._peek(tok).evicted
    # The gate now refuses the demo channel: the refetch lands on the excerpt
    # and preview_meta reads the clip.
    proxy.link_admissible = lambda provider, query: not provider.manifest.demo_limited
    e = proxy.wait_ready(tok, timeout=5)
    assert e.provider is clip and e.audio.excerpt
    assert proxy.preview_meta(tok)["excerpt"] is True
    # An excerpt entry is never dropped: only the demo channel's bytes are.
    assert proxy.drop_audio("id-a1") == 0


def test_track_ready_hooks_all_run_before_the_entry_is_ready():
    prov = _Instant("p")
    proxy = _proxy()
    seen = []
    proxy.track_ready_hooks.append(lambda e: seen.append(("first", e.ready.is_set())))
    proxy.track_ready_hooks.append(lambda e: 1 / 0)      # a failing hook stops nothing
    proxy.track_ready_hooks.append(lambda e: seen.append(("third", e.ready.is_set())))
    tok = proxy.mint(_q("a1"), [(prov, None)])
    assert proxy.wait_ready(tok, timeout=5).audio is not None
    assert seen == [("first", False), ("third", False)]


def _streamed(i):
    return QueueItem(track_id=f"t{i}", media_file_id=None,
                     source={"kind": "proxy", "token": f"tok{i}"},
                     title=f"T{i}", artist="A", album="B", track_number=i,
                     duration_seconds=100.0, cover_id=None, preview=True,
                     provider="stub")


def test_unplayable_reason_reads_the_skipped_slots():
    q = CanonicalQueue()
    q.replace([_streamed(i) for i in range(1, 6)])
    stub = types.SimpleNamespace(_queue=q)
    # Ran off the end at slot 6 having skipped slots 2..5 — the tail of a
    # streamed album; the reason must be about those slots, not about the
    # empty space past the end of the queue.
    assert DlnaBackend._unplayable_reason(stub, 2, 4).startswith("4 streamed track(s)")


def test_a_file_token_is_checked_when_served_not_when_minted(tmp_path):
    proxy = _proxy()
    # Minting is bookkeeping: a path the disk does not have still gets a
    # token, and nothing raises into the caller that builds the URL.
    tok = proxy.register_file(str(tmp_path / "gone.flac"), "audio/flac")
    with pytest.raises(OSError):
        proxy.materialize_file(tok, None)
    track = tmp_path / "track.flac"
    track.write_bytes(b"x" * 10)
    tok = proxy.register_file(str(track), "audio/flac")
    assert proxy.materialize_file(tok, None) == (str(track), "audio/flac", 10)
    # The library's drive goes away under a running node: the token minted
    # while the file was there must not vouch for it any more.
    track.unlink()
    with pytest.raises(OSError):
        proxy.materialize_file(tok, None)


def test_unplayable_reason_names_owned_files_the_disk_refused():
    q = CanonicalQueue()
    q.replace([QueueItem(track_id=f"t{i}", media_file_id=i,
                         source={"kind": "file", "path": f"E:/Music/A/B/{i}.flac",
                                 "format": "FLAC"},
                         title=f"T{i}", artist="A", album="B")
               for i in range(1, 4)])
    stub = types.SimpleNamespace(_queue=q)
    # Catalogued files are neither "not scanned yet" nor "outside the
    # library": only the disk can have refused them.
    assert DlnaBackend._unplayable_reason(stub, 1, 3).startswith(
        "3 queued track(s) could not be read from the library")


def test_file_tokens_are_deterministic_per_node_key_and_span():
    import string
    a = MediaProxy(port=0, advertised_host="127.0.0.1", file_token_key=b"k1")
    b = MediaProxy(port=0, advertised_host="127.0.0.1", file_token_key=b"k1")
    c = MediaProxy(port=0, advertised_host="127.0.0.1", file_token_key=b"k2")
    t = a.register_file("/music/a.flac", "audio/flac")
    assert t == b.register_file("/music/a.flac", "audio/flac")     # a restart hands out the same one
    assert t == a.register_file("/music/a.flac", "audio/flac")     # idempotent
    assert t != c.register_file("/music/a.flac", "audio/flac")     # another node's key
    assert t != a.register_file("/music/a.flac", "audio/flac", start=10.0, end=20.0)
    assert len(t) == 20 and set(t) <= set(string.ascii_letters + string.digits + "-_")
    assert a.file_url(t, host="192.168.1.5") == f"http://192.168.1.5:0/file/{t}"
    assert a.file_url(t) == f"http://127.0.0.1:0/file/{t}"


def test_a_library_file_asked_for_here_speaks_for_the_music_folder(tmp_path, monkeypatch):
    """/file/{token} — what a DLNA renderer and an HQPlayer on another
    machine read — is a producer of the music folder's state as the
    browser's route is: a file found missing has the folder looked at (the
    moment a dropped mount becomes visible), one served as it lies says the
    folder answers. Before, a missing file here was a 500 and a log line:
    nothing told the node its disk was out."""
    import sys
    import types
    import urllib.error
    import urllib.request
    said = []
    monkeypatch.setitem(sys.modules, "scanner", types.SimpleNamespace(
        check_folder=lambda wait=False: said.append("looked at"),
        folder_seen=lambda unreachable: said.append(("answers", not unreachable)),
        of_music_folder=lambda path: str(path).startswith(str(tmp_path / "music") + "/")))
    proxy = MediaProxy(port=0, advertised_host="127.0.0.1", file_token_key=b"k",
                       bind_host="127.0.0.1")
    proxy.start()
    port = proxy._httpd.server_address[1]
    (tmp_path / "music").mkdir()
    track = tmp_path / "music" / "t.flac"
    track.write_bytes(b"x" * 1000)
    tok = proxy.register_file(str(track), "audio/flac")
    # a row of a folder the library has left: served or missing, it says
    # nothing of the music folder
    old = tmp_path / "old-library" / "t.flac"
    old.parent.mkdir()
    old.write_bytes(b"x" * 10)
    old_tok = proxy.register_file(str(old), "audio/flac")
    assert proxy.materialize_file(old_tok, None)[0] == str(old)
    old.unlink()
    with pytest.raises(OSError):
        proxy.materialize_file(old_tok, None)
    assert said == []
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/file/{tok}", timeout=5) as r:
            assert r.status == 200 and len(r.read()) == 1000
        assert said == [("answers", True)]
        track.unlink()                              # the disk pulled under a running node
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/file/{tok}", timeout=5)
        assert e.value.code == 500 and said == [("answers", True), "looked at"]
        # the DLNA backend's own read before a URL is handed to a renderer says it too
        with pytest.raises(OSError):
            proxy.materialize_file(tok, None)
        assert said[2:] == ["looked at"]
    finally:
        proxy._httpd.shutdown()
    # a process that never loaded the scanner looks at no folder
    monkeypatch.delitem(sys.modules, "scanner")
    with pytest.raises(OSError):
        proxy.materialize_file(tok, None)


def test_the_proxy_remembers_what_each_token_was_asked_for(tmp_path):
    """The consumer's side of a hand-over, for the HQPlayer playback trace:
    each request's status, range and the bytes that actually went out."""
    import time
    import urllib.error
    import urllib.request
    proxy = MediaProxy(port=0, advertised_host="127.0.0.1", file_token_key=b"k",
                       bind_host="127.0.0.1")
    proxy.start()
    port = proxy._httpd.server_address[1]
    track = tmp_path / "t.flac"
    track.write_bytes(b"x" * 600_000)
    tok = proxy.register_file(str(track), "audio/flac")
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/file/{tok}",
                                     headers={"Range": "bytes=100-"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 206 and len(r.read()) == 599_900
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/file/NotRegistered0000000", timeout=5)
        assert e.value.code == 404
    finally:
        proxy._httpd.shutdown()
    deadline = time.monotonic() + 5
    while proxy.hits(tok)[-1]["bytes"] < 599_900 and time.monotonic() < deadline:
        time.sleep(0.05)      # the handler counts a chunk after its write returns
    hit = proxy.hits(tok)[-1]
    assert (hit["method"], hit["status"], hit["range"], hit["kind"], hit["client"]) == \
        ("GET", 206, "bytes=100-", "file", "127.0.0.1")
    assert hit["bytes"] == 599_900
    assert [h["status"] for h in proxy.hits("NotRegistered0000000")] == [404]


def test_a_range_past_the_end_is_416_never_the_whole_file(tmp_path):
    """HQPlayer resumes a stream cut short with "bytes=<offset>-" and appends
    what comes back: the whole file for an offset at its end fed it the file
    again and again (Embedded 6.2.3, 2026-10-03)."""
    import urllib.error
    import urllib.request
    proxy = MediaProxy(port=0, advertised_host="127.0.0.1", file_token_key=b"k",
                       bind_host="127.0.0.1")
    proxy.start()
    port = proxy._httpd.server_address[1]
    track = tmp_path / "t.flac"
    track.write_bytes(bytes(range(256)) * 400)            # 102 400 bytes
    tok = proxy.register_file(str(track), "audio/flac")
    url = f"http://127.0.0.1:{port}/file/{tok}"

    def get(rng):
        req = urllib.request.Request(url, headers={"Range": rng} if rng else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.headers.get("Content-Range"), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Range"), e.read()
    try:
        data = track.read_bytes()
        assert get("bytes=102400-") == (416, "bytes */102400", b"")
        assert get("bytes=200000-300000") == (416, "bytes */102400", b"")
        assert get("bytes=-1000") == (206, "bytes 101400-102399/102400", data[-1000:])
        assert get("bytes=100-199") == (206, "bytes 100-199/102400", data[100:200])
        assert get("bytes=102399-") == (206, "bytes 102399-102399/102400", data[-1:])
        assert get("bytes=abc-") == (200, None, data)               # ignored, not refused
        assert get("bytes=0-1,5-6") == (200, None, data)            # multi-range: the whole body
        assert get(None) == (200, None, data)
    finally:
        proxy._httpd.shutdown()


def test_one_range_parser_answers_the_proxy_and_the_browser_route():
    from routers import media
    from streaming.proxy import UNSATISFIABLE, parse_byte_range
    assert parse_byte_range("bytes=100-", 1000) == (100, 999)
    assert parse_byte_range("bytes=1000-", 1000) is UNSATISFIABLE
    assert parse_byte_range("bytes=-100", 1000) == (900, 999)
    assert parse_byte_range("bytes=x-", 1000) is None
    assert media.parse_byte_range is parse_byte_range


def test_the_hit_record_keeps_the_newest_tokens():
    proxy = _proxy()
    for i in range(300):
        proxy.record_hit(f"tok{i}", "127.0.0.1", "GET", None, "file")
    assert proxy.hits("tok0") == [] and proxy.hits("tok299")
    for _ in range(20):
        proxy.record_hit("tok299", "127.0.0.1", "GET", None, "file")
    assert len(proxy.hits("tok299")) == 16


def test_a_preview_counts_the_bytes_it_sends_as_they_go(tmp_path):
    """A whole-track buffer goes out in slices, so the HQPlayer trace sees a
    slow reader already being served, not 0 until the last byte."""
    import time
    import urllib.request

    class _Big(StreamProvider):
        manifest = ProviderManifest(id="big", name="Big", kind="direct_url", lossless=True)

        def fetch(self, query):
            return FetchedAudio(data=b"f" * 1_000_000, mime="audio/flac", lossless=True)

    proxy = MediaProxy(port=0, advertised_host="127.0.0.1", file_token_key=b"k",
                       bind_host="127.0.0.1")
    proxy.start()
    port = proxy._httpd.server_address[1]
    tok = _mint(proxy, _Big(), "a")[0]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/preview/{tok}", timeout=10) as r:
            assert len(r.read()) == 1_000_000
    finally:
        proxy._httpd.shutdown()
    deadline = time.monotonic() + 5
    while proxy.hits(tok)[-1]["bytes"] < 1_000_000 and time.monotonic() < deadline:
        time.sleep(0.05)
    hit = proxy.hits(tok)[-1]
    assert (hit["status"], hit["bytes"], hit["kind"]) == (200, 1_000_000, "preview")


def test_a_head_for_bytes_the_budget_dropped_answers_at_once_from_what_it_told():
    """HQPlayer probes an entry when it is added and keeps its length. Once
    the RAM budget dropped the bytes, a probe — a tail repair's, a
    reconverge's add — answered only after a whole refetch, on HQPlayer's
    control thread (live, 45 s). It answers at once now, from the size and
    type the entry was served with, and fetches nothing: the bytes come back
    with the next GET or the window ahead of the playhead."""
    import urllib.request
    prov = _GatedProvider()
    prov.release("a1")
    proxy = MediaProxy(port=0, advertised_host="127.0.0.1", file_token_key=b"k", bind_host="127.0.0.1")
    proxy.start()
    try:
        tok = _mint(proxy, prov, "a1")[0]
        size = len(proxy.wait_ready(tok, timeout=5).audio.data)
        proxy.enforce_budget(set(), [tok], 0)             # every byte over the budget: dropped
        assert proxy._peek(tok).audio is None
        prov.gates["a1"] = threading.Event()              # a refetch would hang on its gate
        req = urllib.request.Request(f"http://127.0.0.1:{proxy._httpd.server_address[1]}/preview/{tok}",
                                     method="HEAD")
        with urllib.request.urlopen(req, timeout=2) as r:
            assert (r.status, r.headers["Content-Length"], r.headers["Content-Type"]) == \
                (200, str(size), "audio/flac")
        assert prov.started == ["a1"] and proxy._peek(tok).audio is None   # no refetch began
    finally:
        prov.release("a1")
        proxy._httpd.shutdown()


def _refetch_fails(proxy, prov, tok):
    """The bytes of `tok` dropped by the budget, and their refetch failing."""
    from streaming.base import ProviderError
    proxy.enforce_budget(set(), [tok], 0)

    def gone(query):
        raise ProviderError("the provider has it no more")
    prov.fetch = gone
    proxy.demand(tok)
    assert proxy.wait_ready(tok, timeout=5).error


def test_a_head_for_a_stream_whose_refetch_failed_answers_the_error():
    """A HEAD for a stream whose refetch failed answers the error, as its GET
    does — never the size and type it was served with before: HQPlayer would
    take an add of it and fail it only when it plays it."""
    import urllib.error
    import urllib.request
    prov = _GatedProvider()
    prov.release("a1")
    proxy = MediaProxy(port=0, advertised_host="127.0.0.1", file_token_key=b"k", bind_host="127.0.0.1")
    proxy.start()
    try:
        tok = _mint(proxy, prov, "a1")[0]
        proxy.wait_ready(tok, timeout=5)
        _refetch_fails(proxy, prov, tok)
        req = urllib.request.Request(f"http://127.0.0.1:{proxy._httpd.server_address[1]}/preview/{tok}",
                                     method="HEAD")
        try:
            urllib.request.urlopen(req, timeout=2)
            status = 200
        except urllib.error.HTTPError as e:
            status = e.code
        assert status == 502
    finally:
        proxy._httpd.shutdown()


def test_a_refetch_that_fails_is_told_and_comes_back_with_no_fetch():
    """Whoever waits in the node for a refetch hears it failed
    (track_failed_hooks), and the stream is no longer one whose bytes come
    back with a fetch (bytes_away): nothing waits on it for good."""
    prov = _GatedProvider()
    prov.release("a1")
    proxy = _proxy()
    failed = []
    proxy.track_failed_hooks.append(lambda e: failed.append(e.token))
    tok = _mint(proxy, prov, "a1")[0]
    proxy.wait_ready(tok, timeout=5)
    assert not proxy.bytes_away(tok)
    proxy.enforce_budget(set(), [tok], 0)
    assert proxy.bytes_away(tok)
    _refetch_fails(proxy, prov, tok)
    assert failed == [tok] and not proxy.bytes_away(tok)


def test_a_refetch_that_lands_in_another_shape_says_so():
    """The demo channel's bytes dropped (its listen spent), the refetch
    lands on the excerpt: another size and type than the consumers were
    told — `reshaped`, for the output that holds the entry's length."""
    demo, clip = _Instant("demo", demo_limited=True), _Instant("clip", excerpt=True, seconds=30.0)
    clip.fetch = lambda query: FetchedAudio(data=b"y" * 9, mime="audio/ogg", lossless=False,
                                           excerpt=True, seconds=30.0)
    proxy = _proxy()
    tok = proxy.mint(_q("a1"), [(demo, None), (clip, None)])
    e = proxy.wait_ready(tok, timeout=5)
    assert (e.held, e.reshaped) == ((16, "audio/mpeg"), False)
    proxy.drop_audio("id-a1")
    proxy.link_admissible = lambda provider, query: not provider.manifest.demo_limited
    e = proxy.wait_ready(tok, timeout=5)
    assert (e.held, e.reshaped) == ((9, "audio/ogg"), True)
