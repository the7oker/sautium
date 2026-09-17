"""
MediaProxy session semantics — pure logic, a stub provider, no network. Run
inside the backend container:

    python -m pytest test_streaming_proxy.py -q
"""

import threading
import types

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
    return MediaProxy(port=0, advertised_host="127.0.0.1")


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
        assert proxy.preview_meta(proxy.url_for(t))["title"] == title

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
    meta = proxy.preview_meta(proxy.url_for(tok))
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
    assert proxy.preview_meta(proxy.url_for(tok))["excerpt"] is True
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
