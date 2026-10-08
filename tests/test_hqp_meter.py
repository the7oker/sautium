"""HQPlayer's meter stream (backend/playback/hqp_meter.py) against real
sockets: the frame layout of Signalyst's SDK, pinned on live captures (per
channel: the levels, then the Re and Im of the block's spectrum); the source
rebuilt from those spectra and its true peak, past 0 dBTP where HQPlayer's
own levels stop; the one socket the pages' interest opens and closes; the
edges it connects on — never a timer; and the page's event stream that
carries it (routers/player.py)."""

import asyncio
import json
import socket
import struct
import sys
import threading
import time
from pathlib import Path

import numpy as np

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

from playback import hqp_meter  # noqa: E402
from playback.hqp_meter import (HqpMeter, MeterProtocolError, SourceRebuild,  # noqa: E402
                                parse_frames)

N, HOP, BINS = 2048, 1024, 1025
WINDOW = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N) / N)      # periodic Hann


def header(channels, *, bins=BINS, version=1, hop=HOP):
    return struct.pack("<IIIiffff", version, channels, bins, 16, 22050.0, hop / 44100, 2.0, 0.0)


def frames_of(x):
    """HQPlayer's frames for the signal x (samples × channels), as measured
    against the FLAC: each the 2/N-scaled FFT of a periodic-Hann block, Im
    negated, a hop apart. Its own levels (after its limiter) carry nothing
    the meter reads — a constant here."""
    channels = x.shape[1]
    padded = np.concatenate([np.zeros((HOP, channels)), x, np.zeros((N, channels))])
    out = []
    for k in range((len(padded) - N) // HOP + 1):
        spec = np.fft.rfft(padded[k * HOP:k * HOP + N] * WINDOW[:, None], axis=0) * (2 / N)
        body = b""
        for c in range(channels):
            body += struct.pack("<ffff", 0.0, 0.0, -3.0, -3.0)
            body += spec[:, c].real.astype("<f4").tobytes()
            body += (-spec[:, c].imag).astype("<f4").tobytes()
        out.append(header(channels) + body)
    return out


def over_sine(seconds=1.0, peak_db=2.0, channels=2):
    """A sine at fs/4 a quarter turn off its samples: every sample sits 3 dB
    under the true peak — +2 dBTP from samples no higher than −1.01 dBFS,
    the over a sample-peak meter never sees. Faded in and out: an abrupt
    start rings past the peak in any band-limited reconstruction."""
    n = np.arange(int(44100 * seconds))
    x = 10 ** (peak_db / 20) * np.sin(np.pi / 2 * n + np.pi / 4)
    ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(2048) / 2048)
    x[:2048] *= ramp
    x[-2048:] *= ramp[::-1]
    return np.repeat(x[:, None], channels, axis=1)


# -- the frame (pure) -----------------------------------------------------------------

def test_a_stereo_frame_is_16464_bytes_and_its_spectra_come_back_per_channel():
    x = np.random.default_rng(1).normal(0, 0.1, (4096, 2))
    f = frames_of(x)[2]
    assert len(f) == 16464
    frames, used, need = parse_frames(memoryview(f))
    rate, spectra = frames[0]
    assert used == len(f) and need == 0 and rate == 44100 and spectra.shape == (2, BINS)
    block = x[HOP:HOP + N] * WINDOW[:, None]                      # the frame's block of x
    want = np.fft.rfft(block, axis=0).T * (2 / N)
    assert np.allclose(spectra, want, atol=1e-6)                 # Im negated back


def test_an_incomplete_frame_waits_for_its_body():
    a, b, c = frames_of(np.zeros((3 * HOP, 2)))[:3]
    frames, used, need = parse_frames(memoryview(a + b + c[:100]))
    assert len(frames) == 2 and used == len(a) + len(b) and need == len(c)
    assert parse_frames(memoryview(a[:31])) == ([], 0, 0)         # no header yet
    assert parse_frames(memoryview(a[:32])) == ([], 0, len(a))    # a header: its frame's size


@pytest.mark.parametrize("head", [dict(version=2), dict(bins=70000), dict(bins=1),
                                  dict(channels=0), dict(channels=33),
                                  dict(hop=2048)])                # a hop that is not half the FFT
def test_a_foreign_header_fails_before_its_body(head):
    channels = head.pop("channels", 2)
    with pytest.raises(MeterProtocolError):
        parse_frames(memoryview(header(channels, **head)))


def test_the_rebuilt_source_is_the_source_and_its_peak_the_true_peak():
    x = over_sine()
    rebuild = SourceRebuild(2, BINS)
    for f in frames_of(x):
        frames, _, _ = parse_frames(memoryview(f))
        rebuild.add(frames[0][1])
    peak_db = 20 * np.log10(rebuild.peaks())
    # +2 dBTP from samples at −1.01 dBFS: the true peak, not the sample peak
    assert np.allclose(peak_db, 2.0, atol=0.05)
    assert 20 * np.log10(np.abs(x).max()) == pytest.approx(-1.01, abs=0.01)


def test_a_peak_waits_for_the_samples_after_it():
    rebuild = SourceRebuild(1, BINS)
    assert rebuild.peaks() is None                                # nothing yet
    frames, _, _ = parse_frames(memoryview(frames_of(np.zeros((HOP, 1)))[0]))
    rebuild.add(frames[0][1])
    assert rebuild.peaks() is not None                            # a hop, minus the context


# -- the socket -------------------------------------------------------------------------

class FakeMeterPort:
    """HQPlayer's meter port: counts what connects and what ends; the test
    pushes the frames itself."""

    def __init__(self):
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self.accepts = 0
        self.ends = 0
        self._conns = []
        self._cv = threading.Condition()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                c, _ = self._srv.accept()
            except OSError:
                return
            with self._cv:
                self.accepts += 1
                self._conns.append(c)
                self._cv.notify_all()
            threading.Thread(target=self._watch, args=(c,), daemon=True).start()

    def _watch(self, c):
        try:
            while c.recv(4096):
                pass
        except OSError:
            pass
        with self._cv:
            self.ends += 1
            self._cv.notify_all()

    def wait(self, pred, timeout=5.0):
        with self._cv:
            assert self._cv.wait_for(pred, timeout), f"accepts={self.accepts} ends={self.ends}"

    def send(self, data, chunk=4096) -> bool:
        """False when the client closed before all of it went — what HQPlayer
        sees when a meter goes away mid-burst."""
        with self._cv:
            c = self._conns[-1]
        try:
            for i in range(0, len(data), chunk):
                c.sendall(data[i:i + chunk])
        except (BrokenPipeError, ConnectionResetError):
            return False
        return True

    def reset(self):
        """The newest connection reset (RST): an error, not an end of stream."""
        with self._cv:
            c = self._conns.pop()
        c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        c.close()

    def drop(self):
        """HQPlayer ends the newest connection (its meter server restarting)."""
        with self._cv:
            c = self._conns.pop()
        c.shutdown(socket.SHUT_RDWR)
        c.close()

    def close(self):
        self._srv.close()
        with self._cv:
            conns, self._conns = self._conns, []
        for c in conns:
            c.close()


@pytest.fixture
def loop():
    lp = asyncio.new_event_loop()
    t = threading.Thread(target=lp.run_forever, daemon=True)
    t.start()
    yield lp
    lp.call_soon_threadsafe(lp.stop)
    t.join(2)
    lp.close()


@pytest.fixture
def meter(loop):
    # torn down before the pages' loop: a detach tells the pages
    m = HqpMeter()
    yield m
    m.detach()


@pytest.fixture
def port():
    p = FakeMeterPort()
    yield p
    p.close()


class Page:
    """One /api/events connection of a page, read as the generator reads it."""

    def __init__(self, meter, loop, tab):
        self.meter, self.loop, self.tab = meter, loop, tab
        self.evt = asyncio.Event()
        self.conn = meter.register(tab, self.evt, loop)
        self.seq = 0

    def want(self, on):
        self.seq += 1
        return self.meter.want(self.tab, on, self.seq)

    def take(self, timeout=3.0):
        """What arrived since the last take, waiting up to `timeout` for anything."""
        async def wait():
            try:
                await asyncio.wait_for(self.evt.wait(), timeout)
            except asyncio.TimeoutError:
                return []
            self.evt.clear()
            return self.conn.drain()
        return asyncio.run_coroutine_threadsafe(wait(), self.loop).result(timeout + 2)

    def until(self, pred, timeout=5.0):
        got, deadline = [], time.monotonic() + timeout
        while time.monotonic() < deadline:
            msgs = self.take(max(0.0, deadline - time.monotonic()))
            got += msgs
            if any(pred(m) for m in msgs):
                return got
        raise AssertionError(f"nothing matched in {got}")

    def settle(self, quiet=0.3):
        """Everything until the stream has been quiet for `quiet` seconds."""
        got = []
        while True:
            msgs = self.take(quiet)
            if not msgs:
                return got
            got += msgs

    def gone(self):
        self.meter.unregister(self.conn)


def state(name):
    return lambda m: m.get("state") == name


def burst(x):
    """One burst: the frames of x back to back."""
    return b"".join(frames_of(x))


def readings(msgs):
    return [m["p"] for m in msgs if "p" in m]


def loudest(peaks):
    """The highest reading per channel — the settled peak of a passage."""
    return [max(p[c] for p in peaks if p[c] is not None) for c in range(len(peaks[0]))]


def test_no_interest_no_connection(meter, loop, port):
    meter.attach("127.0.0.1", port.port)
    page = Page(meter, loop, "a")
    assert page.take(0.3) == []
    assert port.accepts == 0 and not meter.metering()


def test_two_pages_share_one_socket_and_only_interested_pages_hear_it(meter, loop, port):
    meter.attach("127.0.0.1", port.port)
    meter.set_gain(0.0)                          # volume 0 dB, no adaptive gain
    a, b = Page(meter, loop, "a"), Page(meter, loop, "b")
    assert a.want(True)
    a.until(state("open"))
    port.wait(lambda: port.accepts == 1)
    port.send(burst(over_sine(0.5)), chunk=1000)
    got = readings(a.until(lambda m: "p" in m) + a.settle())
    # the over in dB: +2 dBTP, where HQPlayer's own peak would read 0.00
    assert loudest(got) == pytest.approx([2.0, 2.0], abs=0.1)
    last = got[-1]
    assert b.take(0.3) == []
    assert b.want(True)
    # what a newly interested page is told at once: the state and the last reading
    assert b.until(lambda m: "p" in m) == [{"state": "open"}, {"p": last, "g": 0.0}]
    assert port.accepts == 1


def test_the_reading_is_after_the_volume_and_the_tracks_gain(meter, loop, port):
    meter.attach("127.0.0.1", port.port)
    page = Page(meter, loop, "a")
    page.want(True)
    page.until(state("open"))
    port.send(burst(over_sine(0.3)))
    assert readings(page.take(0.5)) == []        # no gain known yet: no reading
    meter.set_gain(-3.0 - 1.5)                   # the poller: volume + the track's adaptive gain
    port.send(burst(over_sine(0.5)))
    got = [m for m in page.until(lambda m: "p" in m) + page.settle() if "p" in m]
    assert loudest([m["p"] for m in got]) == pytest.approx([-2.5, -2.5], abs=0.1)
    assert {m["g"] for m in got} == {-4.5}       # the gain it was made with: a step shows in it
    port.send(burst(np.zeros((22050, 2))))       # paused: silence
    got = readings(page.until(lambda m: "p" in m and m["p"][0] is None))
    assert got[-1] == [None, None]



def test_the_last_interest_closes_the_socket_at_once(meter, loop, port):
    meter.attach("127.0.0.1", port.port)
    a, b = Page(meter, loop, "a"), Page(meter, loop, "b")
    a.want(True)
    b.want(True)
    a.until(state("open"))
    a.want(False)
    time.sleep(0.3)
    assert port.ends == 0 and meter.metering()
    # a stopped HQPlayer sends nothing: the read blocks, and is woken at once
    t = time.monotonic()
    b.gone()                                    # its stream ended, and the interest with it
    port.wait(lambda: port.ends == 1)
    assert time.monotonic() - t < 1.0 and not meter.metering()


def test_a_reconnected_page_takes_its_interest_over(meter, loop, port):
    meter.attach("127.0.0.1", port.port)
    first = Page(meter, loop, "a")
    first.want(True)
    first.until(state("open"))
    meter.set_gain(-5.0)
    port.send(burst(over_sine(0.5)))
    got = readings(first.until(lambda m: "p" in m) + first.settle())
    assert loudest(got) == pytest.approx([-3.0, -3.0], abs=0.1)   # +2 dBTP at −5 dB
    last = got[-1]
    again = Page(meter, loop, "a")               # the same page on a new stream; the old one lingers
    assert again.until(lambda m: "p" in m) == [{"state": "open"}, {"p": last, "g": -5.0}]
    first.gone()                                 # the old stream's end takes nothing with it
    time.sleep(0.3)
    assert port.ends == 0 and meter.metering()
    again.gone()
    port.wait(lambda: port.ends == 1)


def test_wishes_apply_in_the_pages_order(meter, loop, port):
    meter.attach("127.0.0.1", port.port)
    page = Page(meter, loop, "a")
    assert meter.want("a", True, 2)
    assert meter.want("a", False, 1)             # sent before, landed after
    page.until(state("open"))
    time.sleep(0.3)
    assert port.ends == 0 and meter.metering()
    assert not meter.want("nobody", True, 1)     # no event stream: the route says 404


def test_a_detach_closes_the_socket_and_the_next_attach_reopens_it(meter, loop, port):
    meter.attach("127.0.0.1", port.port)
    page = Page(meter, loop, "a")
    page.want(True)
    page.until(state("open"))
    meter.detach()                               # an output switch, a benchmark's hold
    page.until(state("waiting"))
    port.wait(lambda: port.ends == 1)
    meter.attach("127.0.0.1", port.port)         # the page's interest stayed
    page.until(state("open"))
    port.wait(lambda: port.accepts == 2)


def test_a_refusing_port_is_reported_and_left_alone_until_an_edge(meter, loop):
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    nobody = closed.getsockname()[1]
    closed.close()
    meter.attach("127.0.0.1", nobody)
    page = Page(meter, loop, "a")
    page.want(True)
    got = page.until(state("unavailable"))
    said = next(m for m in got if m.get("state") == "unavailable")
    assert said["reason"] == "refused" and said["port"] == nobody
    assert page.take(0.5) == []                  # no retry of its own
    meter.hqplayer_back()                        # the status poller found HQPlayer again
    page.until(state("unavailable"))
    page.want(True)                              # Retry: the same wish, a newer seq
    page.until(state("unavailable"))


def test_a_stream_that_delivered_is_retried_once_when_it_drops(meter, loop, port):
    meter.attach("127.0.0.1", port.port)
    page = Page(meter, loop, "a")
    page.want(True)
    page.until(state("open"))
    meter.set_gain(0.0)
    port.send(burst(over_sine(0.3)))
    page.until(lambda m: "p" in m)
    port.drop()
    port.wait(lambda: port.accepts == 2)         # at once, no timer
    page.until(state("open"))
    port.drop()                                  # gone again before a frame: no second try
    got = page.until(state("unavailable"))
    assert next(m for m in got if m.get("state") == "unavailable")["reason"] == "closed"
    time.sleep(0.3)
    assert port.accepts == 2


def test_a_foreign_stream_is_never_read_as_a_meter(meter, loop, port):
    meter.attach("127.0.0.1", port.port)
    page = Page(meter, loop, "a")
    page.want(True)
    page.until(state("open"))
    port.send(b"HTTP/1.1 400 Bad Request\r\n\r\n" + bytes(32))
    got = page.until(state("unavailable"))
    assert next(m for m in got if m.get("state") == "unavailable")["reason"] == "protocol"
    port.wait(lambda: port.ends == 1)
    time.sleep(0.3)
    assert port.accepts == 1


def test_a_page_that_stops_reading_loses_its_interest(meter, loop, port):
    meter.attach("127.0.0.1", port.port)
    page = Page(meter, loop, "a")
    page.want(True)
    page.until(state("open"))
    meter.set_gain(0.0)
    # the page reads nothing more while readings pile up past its backlog:
    # one per 0.16 s of music, so a few seconds of it
    port.send(burst(over_sine((hqp_meter.PAGE_BACKLOG + 5) * hqp_meter.READING_S)))
    port.wait(lambda: port.ends == 1)            # its interest was the only one
    assert page.take()[0] == {"state": "dropped"}
    assert not meter.metering()


def test_a_reading_per_160_ms_of_music_however_tcp_cuts_it(meter, loop, port):
    meter.attach("127.0.0.1", port.port)
    meter.set_gain(0.0)
    page = Page(meter, loop, "a")
    page.want(True)
    page.until(state("open"))
    port.send(burst(over_sine(1.0)), chunk=100)          # a burst in a thousand segments
    n = len(readings(page.settle()))
    # 1 s of music and the padding around it: about seven, never one per segment
    assert 6 <= n <= 9


def test_no_reading_while_hqplayer_names_no_track(meter, loop, port):
    meter.attach("127.0.0.1", port.port)
    meter.set_gain(0.0)
    page = Page(meter, loop, "a")
    page.want(True)
    page.until(state("open"))
    port.send(burst(over_sine(0.5)))
    assert readings(page.settle())
    meter.set_gain(None)                        # stopped: the next track's gain is not known
    port.send(burst(over_sine(0.5)))
    assert readings(page.settle()) == []
    other = Page(meter, loop, "b")
    other.want(True)
    assert other.settle() == [{"state": "open"}]   # nothing from before the stop is shown


def test_a_failed_session_leaves_the_thread_to_the_next_wish(meter, loop, port, monkeypatch):
    def broken(sock):
        raise ValueError("a failure nothing expected")
    monkeypatch.setattr(hqp_meter, "_keepalive", broken)
    meter.attach("127.0.0.1", port.port)
    page = Page(meter, loop, "a")
    page.want(True)
    said = next(m for m in page.until(state("unavailable")) if m.get("state") == "unavailable")
    assert said["reason"] == "error" and "ValueError" in said["detail"]
    monkeypatch.undo()
    assert not meter.metering()
    page.want(True)                              # Retry
    page.until(state("open"))
    port.wait(lambda: port.accepts == 2)


def test_a_name_that_cannot_be_a_host_is_unreachable(meter, loop):
    meter.attach("host..docker.internal", 4322)  # IDNA refuses the empty label
    page = Page(meter, loop, "a")
    page.want(True)
    said = next(m for m in page.until(state("unavailable")) if m.get("state") == "unavailable")
    assert said["reason"] == "unreachable"


def test_a_connection_that_fails_is_lost_with_its_error(meter, loop, port):
    meter.attach("127.0.0.1", port.port)
    page = Page(meter, loop, "a")
    page.want(True)
    page.until(state("open"))
    port.reset()                                 # a reset, no frame ever came
    said = next(m for m in page.until(state("unavailable")) if m.get("state") == "unavailable")
    assert said["reason"] == "lost" and said["detail"]


def test_after_a_failure_a_new_wish_hears_the_new_attempt_first(meter, loop):
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    nobody = closed.getsockname()[1]
    closed.close()
    meter.attach("127.0.0.1", nobody)
    page = Page(meter, loop, "a")
    page.want(True)
    page.until(state("unavailable"))
    page.want(False)
    page.want(True)
    assert page.take()[0] == {"state": "connecting"}


def test_hqplayer_back_while_connecting_adds_no_attempt(meter):
    meter.attach("127.0.0.1", 9)
    with meter._cond:
        meter._due = False
        meter._connecting = True
    meter.hqplayer_back()
    assert meter._due is False


# -- the page's event stream --------------------------------------------------------------

def test_the_event_stream_says_hello_first_and_carries_the_meter_to_its_page(monkeypatch):
    from routers import player as player_router
    from routers import settings as settings_router
    monkeypatch.setattr(settings_router, "_notices_state", lambda: {})
    hqp_meter.meter.detach()                     # no HQPlayer attached in this process

    async def run():
        stream = (await player_router.events_stream(tab="page-1")).body_iterator
        assert await stream.__anext__() == 'data: {"t": "hello"}\n\n'
        assert json.loads((await stream.__anext__())[6:])["t"] == "status"
        assert json.loads((await stream.__anext__())[6:])["t"] == "notice"
        assert hqp_meter.meter.want("page-1", True, 1)
        msg = json.loads((await asyncio.wait_for(stream.__anext__(), 3))[6:])
        assert msg == {"t": "meter", "d": {"state": "waiting"}}     # no HQPlayer attached here
        # The page leaves while the stream waits (the server cancels it, as on
        # a disconnect): nothing it waited on is left pending.
        reading = asyncio.ensure_future(stream.__anext__())
        await asyncio.sleep(0.05)
        reading.cancel()
        with pytest.raises((StopAsyncIteration, asyncio.CancelledError)):
            await reading
        for _ in range(3):
            await asyncio.sleep(0)
        me = asyncio.current_task()
        assert [t for t in asyncio.all_tasks() if t is not me and not t.done()] == []
        assert not hqp_meter.meter.want("page-1", True, 2)          # the page went with its stream

    asyncio.run(run())
