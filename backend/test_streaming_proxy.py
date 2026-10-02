"""
MediaProxy session semantics — pure logic, a stub provider, no network. Run
inside the backend container:

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


def _session(proxy, prov, *titles):
    return proxy.start_session([(_q(t), [(prov, None)]) for t in titles])


def _proxy():
    return MediaProxy(port=0, advertised_host="127.0.0.1", file_token_key=b"test-key")


def test_live_queue_streams_survive_a_new_session():
    prov = _GatedProvider()
    prov.release("a1", "a2", "a3", "b1", "b2")
    proxy = _proxy()
    a = _session(proxy, prov, "a1", "a2", "a3")
    for t in a:
        proxy.wait_ready(t, timeout=5)
    proxy.retire_generation(1)          # replace_queue reports the live generation …
    proxy.bind(a, 1)                    # … then the request binds its set to it

    b = _session(proxy, prov, "b1", "b2")

    # A plays on through B's pre-buffer: every token still answers, metadata intact.
    for t, title in zip(a, ("a1", "a2", "a3")):
        e = proxy.wait_ready(t, timeout=5)
        assert e.audio is not None
        assert proxy.preview_meta(t)["title"] == title

    for t in b:
        proxy.wait_ready(t, timeout=5)
    proxy.retire_generation(2)          # the replace: A's generation retires
    proxy.bind(b, 2)
    # Fetched audio stays as a RAM-reuse candidate — the budget owns that memory.
    assert all(proxy._peek(t) is not None for t in a)


def test_new_session_supersedes_only_unbound_pending_fetches():
    prov = _GatedProvider()
    proxy = _proxy()
    a = _session(proxy, prov, "a1", "a2", "a3")
    prov.wait_started("a1")             # worker inside a1; a2, a3 queued behind
    proxy.retire_generation(1)          # replace_queue reports the live generation …
    proxy.bind(a, 1)                    # … then the request binds its set to it

    u = _session(proxy, prov, "u1", "u2")   # a tap that never reaches the queue
    u_entries = [proxy._peek(t) for t in u]
    b = _session(proxy, prov, "b1", "b2")   # the next tap supersedes it

    assert all(proxy._peek(t) is None for t in u)
    assert all(e.ready.is_set() and e.error for e in u_entries)   # waiters woken
    # B goes first; A's fills wait behind it instead of being dropped.
    assert proxy._fetch_q == [b[0], b[1], a[1], a[2]]
    assert proxy._peek(a[1]) is not None and not proxy._peek(a[1]).ready.is_set()

    prov.release("a1")
    assert proxy.wait_ready(a[0], timeout=5).audio is not None
    prov.wait_started("b1")
    assert prov.started == ["a1", "b1"]

    # The replace: A's pending fills retire, its fetched track stays.
    proxy.retire_generation(2)
    proxy.bind(b, 2)
    assert proxy._fetch_q == [b[1]]
    assert proxy._peek(a[1]) is None and proxy._peek(a[2]) is None
    assert proxy._peek(a[0]) is not None

    prov.release("b1", "b2")
    for t in b:
        assert proxy.wait_ready(t, timeout=5).audio is not None


def test_ready_run_scans_the_callers_tokens():
    prov = _GatedProvider()
    prov.release("a1", "a2")
    proxy = _proxy()
    a = _session(proxy, prov, "a1", "a2", "a3")
    proxy.wait_ready(a[1], timeout=5)
    playable, secs, used = proxy.ready_run(a)
    assert playable == [a[0], a[1]] and secs == 200.0 and used == 2


def test_an_excerpt_reports_the_clips_own_length():
    clip = _Instant("clip", excerpt=True, seconds=30.0)
    proxy = _proxy()
    tok = proxy.start_session([(_q("a1"), [(clip, None)])])[0]
    proxy.wait_ready(tok, timeout=5)
    meta = proxy.preview_meta(tok)
    assert meta["excerpt"] is True and meta["duration"] == 30.0
    assert meta["provider"] == "clip"
    assert proxy.ready_run([tok])[1] == 30.0          # not the 100 s catalog length


def test_the_link_gate_skips_refused_links_and_the_chain_cascades():
    demo, clip = _Instant("demo", demo_limited=True), _Instant("clip", excerpt=True, seconds=30.0)
    proxy = _proxy()
    proxy.link_admissible = lambda provider, query: not provider.manifest.demo_limited
    tok = proxy.start_session([(_q("a1"), [(demo, None), (clip, None)])])[0]
    e = proxy.wait_ready(tok, timeout=5)
    assert e.audio is not None and e.provider is clip and demo.fetched == []
    # Every link refused: a fetch failure, not a hang.
    proxy.link_admissible = lambda provider, query: False
    tok2 = proxy.start_session([(_q("a2"), [(demo, None), (clip, None)])])[0]
    e2 = proxy.wait_ready(tok2, timeout=5)
    assert e2.audio is None and "demo listen spent" in (e2.error or "")


def test_ram_audio_is_adopted_only_from_a_provider_the_new_chain_names():
    demo, clip = _Instant("demo", demo_limited=True), _Instant("clip", excerpt=True, seconds=30.0)
    proxy = _proxy()
    first = proxy.start_session([(_q("a1"), [(demo, None), (clip, None)])])[0]
    assert proxy.wait_ready(first, timeout=5).provider is demo
    # Same track, same providers: the bytes in RAM are reused, nothing fetched.
    again = proxy.start_session([(_q("a1"), [(demo, None), (clip, None)])])[0]
    assert proxy._peek(again).ready.is_set() and demo.fetched == ["a1"]
    # Same track, a chain without the demo channel (its listen spent): the
    # demo channel's bytes must not come along — the excerpt is fetched.
    spent = proxy.start_session([(_q("a1"), [(clip, None)])])[0]
    e = proxy.wait_ready(spent, timeout=5)
    assert e.provider is clip and clip.fetched == ["a1"]


def test_drop_audio_rearms_the_demo_channels_entry_and_a_wait_refetches():
    demo, clip = _Instant("demo", demo_limited=True), _Instant("clip", excerpt=True, seconds=30.0)
    proxy = _proxy()
    tok = proxy.start_session([(_q("a1"), [(demo, None), (clip, None)])])[0]
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
    tok = proxy.start_session([(_q("a1"), [(prov, None)])])[0]
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
    tok = _session(proxy, _Big(), "a")[0]
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
