"""Comparing filters by ear (backend/playback/hqp_ab.py) against a fake
HQPlayer that keeps time: each filter builds after a Play, plays its entry
at the wall clock's pace, pauses and resumes with a latency of its own, and
its sound leaves the "output" its output delay after the meter shows it —
the meter a port of its own sending frames a hop apart in 160 ms bursts:
the lead's mark (a frame far below any sound, not zero), the music, and
all-zero frames through a pause and the tail (AB-2 F12). The tests read the
silences off the fake's own output clock, never off Sautium's estimate
(AB-1-DESIGN §9: tests 1, 3, 11, 13–17; the logic's own in
test_hqp_ab_search.py).

The clock is shrunk — a lead of 0.4 s, a fragment of 0.5 s, delays of
0.3–0.9 s — and every rule is the product's."""

import math
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

psycopg2 = pytest.importorskip("psycopg2")
np = pytest.importorskip("numpy")

from config import settings  # noqa: E402
from playback import hqp_ab, hqp_meter  # noqa: E402
from playback import hqp_ab_search as S  # noqa: E402
from playback import hqp_backend as hb  # noqa: E402
from playback import hqp_link as hl  # noqa: E402
from playback.hqp_backend import HqpBackend  # noqa: E402
from playback.manager import PlaybackManager  # noqa: E402
from streaming import service as streaming_service  # noqa: E402
from streaming.proxy import MediaProxy  # noqa: E402

from test_hqp_backend import FakeHqp, _fresh_links, _item, _wait  # noqa: E402
from test_hqp_load import PG, _make_db  # noqa: E402

DBNAME = "sautium_pytest_hqp_ab"
# The product's own read of the queue's entry, before the fixture stands a
# stub in for it (its own test reads the database)
_REAL_TRACK = hqp_ab._track

BINS = 129
FRAME_RATE = 5512.5
HOP = (BINS - 1) / FRAME_RATE          # 23.2 ms, HQPlayer's at 44.1 kHz
BURST = 0.16
METER_LAG = 0.02
# HQPlayer's own zeros on its meter before the source reaches it, from the
# build's end: a Pause sent at Play's answer, or at the first frame on an
# idle host, took hold there, before position 0 (the lab run of 2026-11-23,
# 32 of 32 pauses each way; AB-2 §12) — after a resume a few come again (the hold)
PRE_S = 0.25
# Through a pause HQPlayer sends its zeros at a cadence of their own: 61-80 a
# second against 41-46 while it plays (the same run)
PAUSE_HOP = 1 / 70


def _frame(level: float) -> bytes:
    re = np.zeros(BINS, "<f4")
    re[3] = level
    return (struct.pack("<IIIiffff", 1, 1, BINS, 16, FRAME_RATE / 2, HOP, 2.0, 0.0)
            + struct.pack("<ffff", 0.0, 0.0, -3.0, -3.0) + re.tobytes() + np.zeros(BINS, "<f4").tobytes())


# The mark's frame: a lead's tone at -126 dBFS is ~2.4e-7 on the meter
ZERO, MARK, SOUND = _frame(0.0), _frame(2.4e-7), _frame(0.1)
# A 16-bit TPDF dither's frame (its bins peak ~1.3e-6): sound, not the mark
QUIET = _frame(1.3e-6)
FRAMES = {"z": ZERO, "m": MARK, "s": SOUND, "q": QUIET}


@dataclass
class Params:
    b: float = 0.05            # Play → the first frame (the build)
    d: float = 0.3             # the output delay
    rl: float = 0.03           # a resume's latency
    rate: int = 705600
    extra: float = 0.0         # frames past the delay after the end (sinc-M's 2 s in the lab)
    hold_pause: bool = False   # a Pause during the build answered only at its end (the port busy)
    # A HQPlayer on which a Pause sent before the source takes hold this far
    # into it — inside the lead (the premise of AB-2 §13 Q7 not holding)
    pause_late: float = 0.0


class TimedHqp(FakeHqp):
    """FakeHqp taught time (the module docstring). Segments of what its
    engine does, by wall time: (t0, kind, position at t0, the filter's
    params, the entry: (uri, lead, total)) — kind 'build', 'preroll' (its
    zeros before the source), 'play', 'pause', 'hold' (a resume's latency),
    'tail' (all-zero frames after the end or a Stop), 'off'. Kept whole: the
    output is read off them after the test."""

    FILTERS = ["xla", "sinc-M", "ext2", "hires", "long-lp", "mp", "ringy"]
    STREAMING = ("preroll", "play", "pause", "hold", "tail")

    def __init__(self):
        super().__init__()
        self.filter_names = list(self.FILTERS)
        self.slots = {"nx": 0, "x1": 0, "running": 0}
        self.params = {n: Params() for n in self.FILTERS}
        self.volume = -10.0
        self.volume_range = '<VolumeRange min="-60" max="0" enabled="1" adaptive="0"/>'
        self.seg = [(0.0, "off", 0.0, None, None)]
        self.entry = None              # (uri, lead, total) of the cut it holds
        self.cuts = {}                 # proxy token → (lead, length)
        self.sets = []                 # (time, nx, x1) of each SetFilter
        self.refuse = set()            # filters SetFilter refuses in the 1x slot
        self.refused = []              # the names of each SetFilter it refused
        self.refuse_volume = False     # Volume refused (a fixed volume)
        self.refuse_pause = False      # Pause refused
        self.adds = []                 # times of each cut's PlaylistAdd
        self.stream0 = None            # when the meter's stream of the entry began
        # The passage's own head: ('z', s) seconds of digital silence or
        # ('q', s) of dither at its start, after the lead (review round 1)
        self.quiet_head = None
        try:
            self.meter = _MeterPort(self)
        except OSError:
            FakeHqp.close(self)
            raise

    # -- the engine's timeline -------------------------------------------------

    def _now_seg(self, t):
        return next(s for s in reversed(self.seg) if s[0] <= t)

    def pos_at(self, t):
        t0, kind, pos0, _, _ = self._now_seg(t)
        return pos0 + (t - t0) if kind == "play" else pos0

    def _future_cut(self, now):
        self.seg = [s for s in self.seg if s[0] <= now]

    def _running(self):
        return self.params[self.FILTERS[self.slots["x1"]]]

    def _ends(self, t0, pos0, f):
        total = self.entry[2]
        t_end = t0 + (total - pos0)
        self.seg += [(t_end, "tail", total, f, self.entry),
                     (t_end + f.d + f.extra, "off", total, f, self.entry)]

    def _play(self, now):
        _, kind, pos, f, _ = self._now_seg(now)
        if kind in ("off", "tail"):
            f = self._running()
            self._future_cut(now)
            start = now + f.b
            self.stream0 = start
            self.seg += [(now, "build", 0.0, f, self.entry), (start, "preroll", 0.0, f, self.entry),
                         (start + PRE_S, "play", 0.0, f, self.entry)]
            self._ends(start + PRE_S, 0.0, f)
        elif kind == "pause":
            self._future_cut(now)
            self.seg += [(now, "hold", pos, f, self.entry), (now + f.rl, "play", pos, f, self.entry)]
            self._ends(now + f.rl, pos, f)
        else:
            # A resume before a pause that takes hold later (pause_late): the
            # pause comes first, as HQPlayer takes its commands in order, and
            # the resume after its first zeros
            late = next((x for x in self.seg if x[1] == "pause" and x[0] > now), None)
            if late is not None:
                t0, _, pos, f, entry = late
                self._future_cut(t0)
                back = t0 + 2 * PAUSE_HOP
                self.seg += [(back, "hold", pos, f, entry), (back + f.rl, "play", pos, f, entry)]
                self._ends(back + f.rl, pos, f)

    def _source_at(self, now):
        """When the source reaches the meter in the play under way."""
        return next(t0 for t0, kind, *_ in self.seg if kind == "play" and t0 >= now)

    def build_end(self, now):
        _, kind, _, f, _ = self._now_seg(now)
        return self._source_at(now) - PRE_S if kind == "build" else None

    def _pause(self, now):
        _, kind, _, f, _ = self._now_seg(now)
        if kind in ("build", "preroll"):
            if f.pause_late:
                at = self._source_at(now) + f.pause_late
                self._future_cut(at)
                self.seg.append((at, "pause", f.pause_late, f, self.entry))
                return
            # before the source: it holds at position 0, from the build's end
            at = max(now, self._source_at(now) - PRE_S)
            self._future_cut(at)
            self.seg.append((at, "pause", 0.0, f, self.entry))
        elif kind == "play":
            pos = self.pos_at(now)
            self._future_cut(now)
            self.seg.append((now, "pause", pos, f, self.entry))

    def _stop(self, now):
        _, kind, _, f, _ = self._now_seg(now)
        if kind in ("off", "tail") or f is None:
            return
        pos = self.pos_at(now)
        self._future_cut(now)
        self.seg += [(now, "tail", pos, f, self.entry), (now + f.d, "off", pos, f, self.entry)]

    def frame_at(self, t):
        """None (no frame), or the frame at `t`: 's' the music, 'm' the lead's
        mark, 'z' all-zero (a pause, a resume's latency, the tail). Under the
        lock."""
        _, kind, _, _, entry = self._now_seg(t)
        if entry is None or kind not in self.STREAMING:
            return None
        if kind != "play" or self.pos_at(t) >= entry[2]:
            return "z"                  # HQPlayer's own zeros: before the source, a pause, the tail
        if self.pos_at(t) < entry[1]:
            return "m"
        if self.quiet_head is not None and self.pos_at(t) < entry[1] + self.quiet_head[1]:
            return self.quiet_head[0]
        return "s"

    def output(self, after=0.0):
        """[(on, off, filter params)]: the sound as it left the output, every
        play of a cut since `after`."""
        with self._lock:
            seg = list(self.seg)
        out = []
        for i, (t0, kind, pos0, f, entry) in enumerate(seg):
            if kind != "play" or entry is None or t0 < after:
                continue
            t1 = seg[i + 1][0] if i + 1 < len(seg) else float("inf")
            # the first sound: after the lead, and after a silent head of the passage
            head = self.quiet_head[1] if self.quiet_head is not None and self.quiet_head[0] == "z" else 0.0
            on = t0 + max(0.0, entry[1] + head - pos0)
            off = min(t1, t0 + (entry[2] - pos0))
            if on < off:
                out.append((on + f.d, off + f.d, f))
        return out

    # -- the control port ------------------------------------------------------

    MINE = {"State", "SetFilter", "GetModes", "GetRates", "VolumeRange", "Volume", "PlaylistAdd",
            "Play", "Pause", "Stop", "Status", "GetInfo"}

    def _dispatch(self, cmd, attrs):
        if cmd not in self.MINE:
            return super()._dispatch(cmd, attrs)
        now = time.time()
        if cmd == "Pause":
            with self._lock:
                _, kind, _, f, _ = self._now_seg(now)
                held = self.build_end(now) if f is not None and f.hold_pause else None
            if held is not None:
                time.sleep(max(0.0, held - now))       # the port answers once the build is over
                now = time.time()
        if cmd == "PlaylistAdd":
            token = attrs["uri"].rsplit("/", 1)[-1]
            with self._lock:
                cut = self.cuts.get(token)
                if cut is not None:
                    self.commands.append((cmd, attrs))
                    self.adds.append(now)
                    self.entry = (attrs["uri"], cut[0], cut[0] + cut[1])
                    return '<PlaylistAdd result="OK"/>'
                if attrs.get("clear") == "1":
                    self.entry = None
                    self._future_cut(now)
                    self.seg.append((now, "off", 0.0, None, None))
            return super()._dispatch(cmd, attrs)
        with self._lock:
            ours = self.entry is not None
        if not ours and cmd in ("Play", "Pause", "Stop", "Status"):
            return super()._dispatch(cmd, attrs)
        with self._lock:
            self.commands.append((cmd, attrs))
            if cmd == "State":
                sl = self.slots
                return (f'<State mode="1" active_mode="0" rate="0" active_rate="705600" '
                        f'filter="{sl["x1"]}" filter1x="{sl["x1"]}" filterNx="{sl["nx"]}" shaper="0" '
                        f'volume="{self.volume}" convolution="0" adaptive="0" invert="0" matrix_profile=""/>')
            if cmd == "SetFilter":
                x1 = int(attrs.get("value1x", self.slots["x1"]))
                if self.FILTERS[x1] in self.refuse:
                    self.refused.append(self.FILTERS[x1])
                    return '<SetFilter result="Error">Filter not available in this mode</SetFilter>'
                self.slots["nx"] = int(attrs["value"])
                self.slots["x1"] = int(attrs.get("value1x", self.slots["x1"]))
                self.sets.append((now, self.slots["nx"], self.slots["x1"]))
                return '<SetFilter result="OK"/>'
            if cmd == "GetModes":
                return ('<GetModes><ModesItem index="0" name="[source]" value="-1"/>'
                        '<ModesItem index="1" name="PCM" value="0"/></GetModes>')
            if cmd == "GetRates":
                return '<GetRates><RatesItem index="0" rate="705600"/></GetRates>'
            if cmd == "VolumeRange":
                return self.volume_range
            if cmd == "Volume":
                if self.refuse_volume:
                    self.refused.append("volume")
                    return '<Volume result="Error"/>'
                self.volume = float(attrs["value"])
                return '<Volume result="OK"/>'
            if cmd == "GetInfo":
                return ('<GetInfo name="fake" product="Signalyst HQPlayer Fake" '
                        'version="6" platform="Linux" engine="6.3.3"/>')
            if cmd == "Play":
                self._play(now)
                return '<Play result="OK"/>'
            if cmd == "Pause":
                if self.refuse_pause:
                    return '<Pause result="Error">not now</Pause>'
                self._pause(now)
                return '<Pause result="OK"/>'
            if cmd == "Stop":
                self._stop(now)
                return '<Stop result="OK"/>'
            _, kind, _, f, entry = self._now_seg(now)
            state = {"build": 2, "preroll": 2, "play": 2, "hold": 2, "pause": 1}.get(kind, 0)
            uri = entry[0] if entry is not None and kind != "off" else ""
            delay = f' output_delay="{int(f.d * 1e6)}"' if f is not None and kind != "off" else ""
            rate = f.rate if f is not None else 705600
            length = entry[2] if entry is not None else 0
            return (f'<Status state="{state}" track="{1 if uri else 0}" position="{round(self.pos_at(now), 2)}" '
                    f'length="{length}" volume="{self.volume}" tracks_total="1" process_speed="2.0" '
                    f'input_fill="0.9" output_fill="0.9" active_mode="PCM" '
                    f'active_filter="{self.FILTERS[self.slots["x1"]]}" active_shaper="none" '
                    f'active_rate="{rate}"{delay}><metadata artist="" album="" song="" genre="" '
                    f'uri="{self._uri_out(uri)}" samplerate="44100" bits="24" channels="2" sdm="0"/>'
                    f'</Status>')

    def close(self):
        self.meter.close()
        super().close()


class _MeterPort:
    """HQPlayer's meter port at the control port + 1: every connection gets
    the stream's frames, a hop apart from the moment the stream starts (the
    build's end), sent in bursts of BURST from that moment, METER_LAG after
    the burst's last position — as the lab's captures showed HQPlayer's: a
    resume from a pause at the first frame spread by 69 ms in PCM, which a
    burst clock of its own would not give."""

    TICK = 0.01

    def __init__(self, fake):
        self.fake = fake
        self._srv = socket.socket()
        self._srv.bind(("127.0.0.1", fake.port + 1))
        self._srv.listen(8)
        self._conns = []
        self._lock = threading.Lock()
        self._done = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()
        threading.Thread(target=self._send, daemon=True).start()

    def _accept(self):
        while not self._done.is_set():
            try:
                c, _ = self._srv.accept()
            except OSError:
                return
            with self._lock:
                self._conns.append(c)

    def _send(self):
        s0, t, over = None, None, None
        while not self._done.wait(self.TICK):
            now = time.time()
            data = []
            with self.fake._lock:
                if self.fake.stream0 not in (s0, over):
                    s0 = self.fake.stream0
                    t = None if s0 is None else s0 + HOP
                while s0 is not None:
                    due = s0 + math.ceil((t - s0) / BURST - 1e-9) * BURST + METER_LAG
                    if due > now:
                        break
                    frame = self.fake.frame_at(t)
                    if frame is None:
                        if self.fake._now_seg(t)[1] == "off":
                            s0, over = None, s0     # the stream is over: its frames stop
                        break
                    data.append(FRAMES[frame])
                    t += PAUSE_HOP if self.fake._now_seg(t)[1] == "pause" else HOP
            if not data:
                continue
            blob = b"".join(data)
            with self._lock:
                for c in list(self._conns):
                    try:
                        c.sendall(blob)
                    except OSError:
                        self._conns.remove(c)

    def close(self):
        self._done.set()
        self._srv.close()
        with self._lock:
            for c in self._conns:
                c.close()


def _timed_fake():
    for _ in range(20):
        try:
            return TimedHqp()
        except OSError:
            continue        # its port + 1 was taken: another pair
    raise RuntimeError("no free port pair for the fake HQPlayer and its meter")


# -- the atlas, the speeds and the track, as the sitting reads them ------------------------

# name → (phase, cutoff over Nyquist, ringing in source samples): each far
# from xla on one axis
AXES = {"xla": ("linear", 0.95, 300), "sinc-M": ("linear", 0.99, 9000),
        "ext2": ("minimum", 0.95, 320), "hires": ("linear", 0.80, 280),
        "long-lp": ("intermediate", 0.94, 2000), "mp": ("minimum", 0.90, 600),
        "ringy": ("linear", 0.96, 30000)}


class _Found:
    def __init__(self, name):
        self.name = name
        phase, cut, ring = AXES[name]
        self.state = "measured"
        f3 = cut * 22.05
        self.entry = {"name": name, "contexts": {"1x-44.1k-705.6k": {
            "phase": phase, "f_3db_khz": f3, "pre_ring_ms_100": ring / 2 / 44.1,
            "post_ring_ms_100": ring / 2 / 44.1}}}


class StubAtlas:
    twins = {}

    def engine_for(self, engine, kind=None, **kw):
        return "6.3.3"

    def speaks_of(self, engine, kind, **kw):
        return True

    def lookup(self, kind, name, engine, mode, **kw):
        return _Found(name) if name in AXES else None

    def identical(self, found):
        gid = self.twins.get(found.name)
        return ({"id": gid, "members": []}, {}) if gid else None


@dataclass
class Ab:
    fake: TimedHqp
    mgr: PlaybackManager
    conn: object
    endpoint_id: int
    taps: list

    def q(self, sql, *params):
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def phase(self):
        return hqp_ab.view()["phase"]

    def wait_phase(self, *phases, timeout=60.0):
        assert _wait(lambda: self.phase() in phases, timeout), hqp_ab.view()
        return hqp_ab.view()

    def answer(self, a, timeout=60.0):
        assert _wait(lambda: (hqp_ab.view().get("step") or {}).get("answerable")
                     or hqp_ab.view()["phase"] not in ("testing",), timeout), hqp_ab.view()
        self.taps.append(time.time())
        hqp_ab.command("answer", answer=a)
        assert _wait(lambda: not (hqp_ab.view().get("step") or {}).get("answerable")
                     or hqp_ab.view()["phase"] != "testing", 5)

    def begin(self):
        self.taps.append(time.time())
        hqp_ab.command("begin")
        self.wait_phase("testing", "result", "ended", timeout=10)


@pytest.fixture(scope="module")
def dsn():
    admin, nb = _make_db(DBNAME)
    yield f"postgresql://{PG['user']}:{PG['password']}@{PG['host']}:{PG['port']}/{DBNAME}"
    nb._drop_database(admin, DBNAME)
    admin.close()


@pytest.fixture
def ab(dsn, tmp_path, monkeypatch):
    import db_pool
    import hqp_atlas
    import hqp_library
    from playback import hqp_load
    from playback import manager as manager_mod
    from playback import substitute
    from routers import settings as settings_router
    from streaming import transcode
    pool = psycopg2.pool.ThreadedConnectionPool(1, 8, dsn=dsn, options="-c timezone=UTC")
    monkeypatch.setattr(db_pool, "_pool", pool)
    conn = psycopg2.connect(dsn, options="-c timezone=UTC")
    conn.autocommit = True
    fake = _timed_fake()
    with conn.cursor() as cur:
        cur.execute("TRUNCATE hqp_endpoints CASCADE")
        cur.execute("""INSERT INTO hqp_endpoints (name, host, port, product, hqp_name, hqp_engine, cuda)
                       VALUES ('fake', '127.0.0.1', %s, 'Signalyst HQPlayer Fake', 'fake', '6.3.3', 'full')
                       RETURNING id""", (fake.port,))
        endpoint_id = cur.fetchone()[0]
    monkeypatch.setattr(settings, "hqplayer_host", "127.0.0.1")
    monkeypatch.setattr(settings, "hqplayer_port", fake.port)
    monkeypatch.setattr(settings, "media_proxy_advertised_host", "127.0.0.1")
    proxy = MediaProxy(port=0, advertised_host="127.0.0.1", file_token_key=b"k")
    monkeypatch.setattr(streaming_service, "_proxy", proxy)
    _fresh_links()
    monkeypatch.setattr(hl, "CIRCUIT_COOLDOWN_S", 0.2)
    monkeypatch.setattr(hb, "_notify_notices", lambda: None)
    monkeypatch.setattr(hb, "_stream_mode", lambda: True)
    monkeypatch.setattr(hqp_library, "request_sync", lambda host, port: None)
    monkeypatch.setattr(substitute, "native_plays", lambda items, output_id, endpoint_id: [None] * len(items))
    monkeypatch.setattr(settings_router, "_read", lambda key: "hqplayer" if key == "output.type" else None)
    # The clock shrunk, every rule the product's
    monkeypatch.setattr(S, "LEAD_MIN_S", 0.4)
    monkeypatch.setattr(S, "MARGIN_S", 0.2)
    monkeypatch.setattr(hqp_ab, "QUIET_S", 0.2)
    monkeypatch.setattr(hqp_ab, "LENGTH_MIN_S", 0.2)
    monkeypatch.setattr(hqp_ab, "LENGTH_DEFAULT_S", 0.5)
    monkeypatch.setattr(hqp_ab, "WARM_S", 0.3)
    # The track: an owned file here (its own test reads the database)
    monkeypatch.setattr(hqp_ab, "_track", lambda mgr: {
        "track_id": None, "title": "Song 1", "artist": "Artist", "path": str(tmp_path / "song.flac"),
        "offset": 0.0, "length": 120.0, "rate": 44100, "channels": 2})

    def cut(path, start, length, *, lead, fade):
        out = tmp_path / f"cut-{start:.3f}-{length:.3f}-{lead:.3f}.flac"
        out.write_bytes(b"fLaC")
        fake.cuts[proxy.file_token(str(out))] = (lead, length)
        return str(out)
    monkeypatch.setattr(transcode, "flac_fragment_path_for_file", cut)
    monkeypatch.setattr(hqp_atlas, "read", lambda shipped, overlay: StubAtlas())
    speeds = {n: {"class": "ok", "speed": 2.0} for n in TimedHqp.FILTERS}
    monkeypatch.setattr(hqp_load, "headroom", lambda ctx, **kw: {"filters": speeds})
    mgr = PlaybackManager()
    monkeypatch.setattr(mgr, "_schedule_persist", lambda: None)
    mgr.queue.replace([_item((tmp_path / "song.flac").as_posix(), 1)])
    monkeypatch.setattr(manager_mod, "manager", mgr)
    b = HqpBackend(emit=mgr._on_backend_status, queue=mgr.queue)
    mgr._active = b
    b.start()
    r = Ab(fake, mgr, conn, endpoint_id, [])
    r.speeds = speeds
    yield r
    hqp_ab.close()
    s = hqp_ab._sitting
    if s is not None and s.thread is not None:
        s.thread.join(30)
        assert not s.thread.is_alive(), "the sitting's thread outlived its test"
    hqp_ab._sitting = None
    if mgr.active is not None:
        mgr.active.shutdown()
    _fresh_links()
    hqp_meter.meter.detach()
    fake.close()
    conn.close()
    pool.closeall()


def _silences(r: Ab, after: float):
    """Each fragment's silence as the fake's output sounded it, measured
    from the sitting's own anchor for it — the end of the sound before as
    the sitting read it, or the tap as it took it — beside the P it was
    played at: [(silence, P)]. A fragment the sitting caught as slipped is
    left out: it plays that step again (B-119: measured from the test's own
    tap, stamped before the sitting took the answer, and from the fake's own
    end, the two clocks parted by up to 0.46 s under the lab's load — the
    sitting's resume had not)."""
    out = [on for on, _, _ in r.fake.output(after)]
    frags = [f for f in hqp_ab._sitting.fragments if f["anchor"] >= after]
    assert len(out) == len(frags), (out, frags)
    return [(on - f["anchor"], f["silence_s"]) for on, f in zip(out, frags) if not f["slipped"]]


def _said(gaps):
    return ", ".join(f"{g:.3f} (P {p:.2f})" for g, p in gaps)


def _p(*names, ref=None):
    """The P a step playing `names` (compared with `ref`) gets from the
    sitting's timings now."""
    s = hqp_ab._sitting
    return S.silence([s.timings[n] for n in names], s.lead_s,
                     s.timings[ref] if ref is not None else None)


# -- the warm-up and the silence (tests 1, 11) ------------------------------------------------

def test_a_chain_plays_every_fragment_after_its_steps_silence(ab):
    fake = ab.fake
    fake.params["xla"] = Params(b=0.05, d=0.3, rl=0.02)
    fake.params["sinc-M"] = Params(b=0.25, d=0.9, rl=0.05, extra=0.3)
    before = fake.volume
    hqp_ab.open_sitting("chain")
    v = ab.wait_phase("ready")
    assert v["warm"]["done"] == v["warm"]["of"] == 1 + len(hqp_ab._sitting.pool)
    # the warm-up ran at the bottom of the range, their volume read back after it
    vols = [a["value"] for c, a in fake.commands if c == "Volume"]
    assert vols == ["-60.0", str(before)] and fake.volume == before
    assert v["silence_s"] is None             # no step yet: P is a step's (AB-2)
    t0 = time.time()
    ab.begin()
    for a in ("same", "same", "same", "same", "same", "same"):
        if ab.phase() != "testing":
            break
        ab.answer(a)
    ab.wait_phase("result", "ended")
    gaps = _silences(ab, t0)
    assert len(gaps) >= 4
    assert all(abs(g - at) <= 0.15 for g, at in gaps), _said(gaps)
    # no volume change between the warm-up and the end
    assert [a["value"] for c, a in fake.commands if c == "Volume"] == vols
    row = ab.q("SELECT ended::text, slipped, silence_s::float, lead_s::float FROM hqp_ab_sittings")
    # a slip the sitting caught (the lab's load can make one) is played
    # again and counted; none was heard
    assert row[0][0] == "done"
    # each duel keeps the P it was played at; the sitting the longest
    duels = [r[0] for r in ab.q("SELECT silence_s::float FROM hqp_ab_duels ORDER BY n")]
    assert duels and None not in duels
    assert row[0][2] >= max(duels) - 0.01


def test_a_build_slower_than_its_warm_up_is_played_again_with_the_silence_grown(ab):
    fake = ab.fake
    hqp_ab.open_sitting("pair", "sinc-M")
    ab.wait_phase("ready")
    p = _p(hqp_ab._sitting.yours, "sinc-M")
    fake.params["sinc-M"] = Params(b=p + 0.5)          # its pause lands after its resume was due
    ab.begin()
    ab.answer("1")
    ab.answer("same")
    ab.answer("same")
    ab.wait_phase("result")
    rows = ab.q("SELECT slipped, silence_s::float FROM hqp_ab_sittings")
    assert rows[0][0] >= 1 and rows[0][1] > p
    # a slip is never a duel: the answers given are the duels
    assert ab.q("SELECT count(*) FROM hqp_ab_duels")[0][0] == 3


def test_a_pause_landing_late_moves_the_resume_and_never_the_sound(ab, monkeypatch):
    """The sitting's thread woken late on a fragment's first frame: its pause,
    sent at Play's answer, held before the source all the same (§13 Q7), and
    the resume, timed from the later of that and the first frame, keeps the
    silence."""
    real = hqp_ab.Sitting._wait
    late = []

    def woken_late(self, pred, limit, what):
        got = real(self, pred, limit, what)
        if what == "frame of the passage" and self.phase == "testing" and len(late) < 2:
            late.append(what)
            time.sleep(0.15)        # the lead played on further
        return got
    monkeypatch.setattr(hqp_ab.Sitting, "_wait", woken_late)
    monkeypatch.setattr(S, "MARGIN_S", 0.4)     # the late wake well inside it: no slip
    hqp_ab.open_sitting("pair", "ext2")
    ab.wait_phase("ready")
    t0 = time.time()
    ab.begin()
    for a in ("1", "2"):
        if ab.phase() != "testing":
            break
        ab.answer(a)
    hqp_ab.command("stop")
    ab.wait_phase("result", "ended")
    gaps = _silences(ab, t0)
    assert len(late) == 2, _said(gaps)
    # timed from the warm-up's pause, each late one would be 0.15 s short
    assert all(abs(g - at) <= 0.08 for g, at in gaps), _said(gaps)


def test_a_slow_build_falls_inside_the_silence_and_the_pair_hears_it_equal(ab):
    """§9 test 1's slow side: a filter that takes 1.2 s from Play to its
    first frame (an SDM build's place) against one that takes 0.05 s — the
    build is paid in the paused lead, so both sides get the same silence."""
    fake = ab.fake
    fake.params["ext2"] = Params(b=1.2, d=0.6, rl=0.04)
    hqp_ab.open_sitting("pair", "ext2")
    ab.wait_phase("ready")
    t0 = time.time()
    ab.begin()
    for a in ("1", "2", "1", "2"):
        if ab.phase() != "testing":
            break
        ab.answer(a)
    hqp_ab.command("stop")
    ab.wait_phase("result", "ended")
    gaps = _silences(ab, t0)
    assert len(gaps) >= 6, _said(gaps)
    assert all(abs(g - at) <= 0.15 for g, at in gaps), _said(gaps)


# -- AB-2: the silence per step, the 10 s cap, the pause's true position -------------------

def _run_pair(ab, answers=("1", "2")):
    t0 = time.time()
    ab.begin()
    for a in answers:
        if ab.phase() != "testing":
            break
        ab.answer(a)
    hqp_ab.command("stop")
    ab.wait_phase("result", "ended")
    return t0


def test_a_second_tried_and_changed_stretches_no_round(ab):
    """B-131: P per step from its own filters — a slow second the person
    tried and then changed keeps no round waiting (one P a sitting took
    every filter warmed)."""
    ab.fake.params["sinc-M"] = Params(b=1.5)
    hqp_ab.open_sitting("pair", "sinc-M")
    ab.wait_phase("ready")
    hqp_ab.command("form", form="pair", second="ext2")
    assert _wait(lambda: hqp_ab.view()["second"] == "ext2" and hqp_ab.view()["phase"] == "ready", 60)
    s = hqp_ab._sitting
    slow = _p(s.yours, "sinc-M")
    gaps = _silences(ab, _run_pair(ab))
    assert gaps, _said(gaps)
    assert all(at < slow - 1.0 for _, at in gaps), _said(gaps)
    assert all(abs(g - at) <= 0.15 for g, at in gaps), _said(gaps)
    assert all(p < slow - 1.0 for (p,) in ab.q("SELECT silence_s::float FROM hqp_ab_duels"))


def test_a_slow_probe_stretches_only_the_steps_it_is_in(ab):
    """B-131 in the chain (Q1 (a)): a candidate that builds 0.8 s against
    0.05 s waits in the steps that play it or are compared with it, and in
    no other; every silence its own step's."""
    ab.fake.params["hires"] = Params(b=0.8)
    hqp_ab.open_sitting("chain")
    ab.wait_phase("ready")
    assert "hires" in hqp_ab._sitting.pool
    t0 = time.time()
    ab.begin()
    for _ in range(S.CHAIN_STEPS):
        if ab.phase() != "testing":
            break
        ab.answer("same")
    ab.wait_phase("result", "ended")
    gaps = _silences(ab, t0)
    assert all(abs(g - at) <= 0.15 for g, at in gaps), _said(gaps)
    # like against like: the one-fragment steps after the first (the first
    # plays two, so its P holds a tail the others have not)
    rows = ab.q("SELECT first_name, second_name, silence_s::float FROM hqp_ab_duels WHERE n > 1 ORDER BY n")
    with_slow = [p for a, b, p in rows if "hires" in (a, b)]
    without = [p for a, b, p in rows if "hires" not in (a, b)]
    assert with_slow and without, rows          # the search's order is its own: the same each run
    assert max(without) < min(with_slow) - 0.5, rows


def test_a_candidate_over_the_start_cap_leaves_with_its_reason(ab, monkeypatch):
    """B-131: a probe whose start (Play to its first frame, the build in it)
    is over START_MAX_S leaves the sitting with its reason; the rest goes on."""
    monkeypatch.setattr(S, "START_MAX_S", 0.8)
    ab.fake.params["hires"] = Params(b=1.0)
    hqp_ab.open_sitting("chain")
    v = ab.wait_phase("ready")
    s = hqp_ab._sitting
    assert "hires" not in s.pool and "hires" not in s.search.pool and "hires" not in s.timings
    why = {r["name"]: r["why"] for r in v["chain"]["left_out"]}
    assert why["hires"] == "Takes too long to start here to compare it"
    assert v["own_slow"] is None


def test_the_pairs_second_over_the_cap_is_cleared_with_its_reason(ab, monkeypatch):
    monkeypatch.setattr(S, "START_MAX_S", 0.8)
    ab.fake.params["sinc-M"] = Params(b=1.0)
    hqp_ab.open_sitting("pair", "sinc-M")
    v = ab.wait_phase("ready")
    assert v["second"] is None and v["say"]["line"] == "Takes too long to start here to compare it"
    assert "sinc-M" in {r["name"] for r in v["seconds"]["left_out"]}


def test_their_own_filter_over_the_cap_keeps_the_sitting_and_says_its_pauses_are_long(ab, monkeypatch):
    monkeypatch.setattr(S, "START_MAX_S", 0.8)
    ab.fake.params["xla"] = Params(b=1.0)          # theirs: slot 0
    hqp_ab.open_sitting("pair", "ext2")
    v = ab.wait_phase("ready")
    s = hqp_ab._sitting
    assert v["ended"] is None and v["second"] == "ext2"
    assert v["own_slow"] is not None and v["own_slow"] >= 0.9
    gaps = _silences(ab, _run_pair(ab))
    assert gaps and all(at > 1.0 + S.LEAD_MIN_S for _, at in gaps), _said(gaps)
    assert all(abs(g - at) <= 0.15 for g, at in gaps), _said(gaps)


def test_their_filters_slow_first_play_is_measured_again_after_another(ab, monkeypatch):
    """§3.2, Q2: the warm-up's first play follows whatever HQPlayer held —
    a build across rate families no fragment pays. Over the cap, their
    start is taken again after another filter's play."""
    monkeypatch.setattr(S, "START_MAX_S", 0.8)
    fake = ab.fake
    real = fake._play
    first = []

    def slow_first(now):
        _, kind, _, _, _ = fake._now_seg(now)
        if kind in ("off", "tail") and not first:
            first.append(now)
            fake.params["xla"] = Params(b=1.2)
            real(now)
            fake.params["xla"] = Params()
            return
        real(now)
    fake._play = slow_first
    hqp_ab.open_sitting("pair", "ext2")
    v = ab.wait_phase("ready")
    s = hqp_ab._sitting
    assert first and v["own_slow"] is None, s.timings
    assert s.timings[s.yours].start < 0.8
    # played again: theirs, ext2, theirs
    assert v["warm"]["of"] == 3


def _held_at_the_first_frame(monkeypatch, times=2, hold=0.15):
    """F12 as HQP-BENCH-63 §14.5 saw it under load, made exact (B-119's
    method): the sitting's thread woken late on a test fragment's first frame
    AND the meter's reader held there — the frames wait in the kernel's
    buffer, then come in one late read. The first `times` test fragments."""
    held = []
    real_wait = hqp_ab.Sitting._wait

    def late_wait(self, pred, limit, what):
        got = real_wait(self, pred, limit, what)
        if what == "frame of the passage" and self.phase == "testing" and len(held) < times:
            held.append(time.time())
            time.sleep(hold)
        return got
    monkeypatch.setattr(hqp_ab.Sitting, "_wait", late_wait)
    real_read = hqp_ab.Sitting._on_read

    def held_read(self, at, hop, levels):
        first = not self._reads
        real_read(self, at, hop, levels)
        if first and self.phase == "testing" and len(held) < times:
            time.sleep(hold)                  # on the meter's thread: the reads behind it wait
    monkeypatch.setattr(hqp_ab.Sitting, "_on_read", held_read)
    return held


def test_a_sitting_and_reader_held_at_the_first_frame_move_no_silence(ab, monkeypatch):
    """F12 (§13 Q7): the Pause goes out at Play's answer, so it takes hold
    before the source whatever the sitting's thread and the meter's reader
    then do — no silence short or long, no slip. At the first frame (the
    trigger before) a pause sent that late takes hold inside the lead."""
    held = _held_at_the_first_frame(monkeypatch)
    hqp_ab.open_sitting("pair", "ext2")
    ab.wait_phase("ready")
    gaps = _silences(ab, _run_pair(ab))
    assert len(held) == 2
    assert hqp_ab._sitting.slipped == 0, _said(gaps)
    assert all(abs(g - at) <= 0.08 for g, at in gaps), _said(gaps)
    # the pause took hold at the lead's start: every pause segment at position 0
    assert {round(pos, 6) for _, kind, pos, _, _ in ab.fake.seg if kind == "pause"} == {0.0}


def test_a_pause_whose_answer_a_build_holds_still_holds_before_the_source(ab):
    """The lab saw HQPlayer's answer to a Pause sent during a build come up
    to 4.2 s later (7 of 32): the pause still held at position 0, and the
    resume waits for both the answer and the build's end."""
    ab.fake.params["ext2"] = Params(b=0.6, hold_pause=True)
    hqp_ab.open_sitting("pair", "ext2")
    ab.wait_phase("ready")
    gaps = _silences(ab, _run_pair(ab))
    assert gaps and all(abs(g - at) <= 0.15 for g, at in gaps), _said(gaps)


def test_a_pause_taking_hold_inside_the_lead_is_a_slip_said_by_the_mark(ab):
    """§13 Q8: a marked frame before the resume — the pause took hold inside
    the lead — is a slip, played again, counted with its cause."""
    hqp_ab.open_sitting("pair", "ext2")
    ab.wait_phase("ready")
    once = _late_pause_once(ab, 0.1)
    gaps = _silences(ab, _run_pair(ab))
    assert once and hqp_ab._sitting.slipped == 1
    assert ab.q("SELECT cause::text, warm FROM hqp_ab_slips") == [("mark", False)]
    assert all(abs(g - at) <= 0.15 for g, at in gaps), _said(gaps)
    assert ab.q("SELECT count(*) FROM hqp_ab_duels")[0][0] == 2


def test_a_hqplayer_whose_pause_never_holds_before_the_source_stops_in_words(ab):
    """§13 Q8's addition: on a HQPlayer where every pause takes hold inside
    the lead, the test's first step slips by the mark at every try and the
    sitting stops in words blaming HQPlayer — no filter left out as late. (The
    warm-up resumes at once, before the source is due: only the test's
    fragments can see it.)"""
    for n in TimedHqp.FILTERS:
        ab.fake.params[n] = Params(pause_late=0.1)
    hqp_ab.open_sitting("pair", "ext2")
    ab.wait_phase("ready")
    ab.begin()
    v = ab.wait_phase("ended", timeout=90)
    assert v["ended"] == "failed" and v["note"] == hqp_ab.NEVER_HOLDS
    assert "late" not in hqp_ab._sitting.screened.left_out
    assert ab.q("SELECT count(*), bool_and(NOT warm), min(cause::text), max(cause::text) FROM hqp_ab_slips") == [
        (hqp_ab.SLIPS_A_STEP, True, "mark", "mark")]
    assert ab.q("SELECT count(*) FROM hqp_ab_duels")[0][0] == 0


def test_a_passage_that_starts_in_digital_silence_or_dither_holds_and_sounds(ab):
    """Review round 1, H1 and M3: the passage's own digital silence after the
    lead is never taken for a pause held inside it, and a dithered quiet
    start (16-bit TPDF, bins ~1.3e-6) is sound, not the mark."""
    for head in ("z", "q"):
        ab.fake.quiet_head = (head, 0.2)
        hqp_ab.open_sitting("pair", "ext2")
        ab.wait_phase("ready")
        gaps = _silences(ab, _run_pair(ab))
        assert hqp_ab._sitting.slipped == 0 and gaps, (head, _said(gaps))
        assert all(abs(g - at) <= 0.15 for g, at in gaps), (head, _said(gaps))
        hqp_ab.close()


def test_a_warm_up_after_a_filter_at_another_rate_settles_on_theirs_first(ab, monkeypatch):
    """Review round 1, M4: a play after one at another output rate would pay
    the switch back in its start — their filter plays once between,
    unmeasured, so nothing after is left out as slow or measured long."""
    monkeypatch.setattr(S, "START_MAX_S", 0.8)
    fake = ab.fake
    fake.params["ringy"] = Params(rate=768000)
    real = fake._play
    last = []

    def switch_back(now):
        _, kind, _, _, _ = fake._now_seg(now)
        if kind in ("off", "tail"):
            f = fake._running()
            back = bool(last) and last[-1] != f.rate
            last.append(f.rate)
            if back:                              # a rate change paid at this Play
                fake.params[fake.FILTERS[fake.slots["x1"]]] = Params(**{**f.__dict__, "b": 1.0})
                real(now)
                fake.params[fake.FILTERS[fake.slots["x1"]]] = f
                return
        real(now)
    fake._play = switch_back
    hqp_ab.open_sitting("chain")
    v = ab.wait_phase("ready")
    s = hqp_ab._sitting
    assert "ringy" not in s.pool and s.left_rates.get("ringy") == 768000
    assert "long_start" not in s.screened.left_out, s.screened.left_out
    assert all(t.start < 0.8 for t in s.timings.values()), s.timings
    assert v["own_slow"] is None


def _late_pause_once(ab, late):
    """The next test fragment's pause takes hold `late` s into the source."""
    fake = ab.fake
    once = []
    real = fake._pause

    def late_once(now):
        _, kind, _, f, _ = fake._now_seg(now)
        if kind in ("build", "preroll") and not once and hqp_ab._sitting.phase == "testing":
            once.append(now)
            f2 = Params(**{**f.__dict__, "pause_late": late})
            fake.seg = [x if x[3] is not f else (x[0], x[1], x[2], f2, x[4]) for x in fake.seg]
        real(now)
    fake._pause = late_once
    return once


def test_a_pause_that_does_not_hold_is_stopped_at_its_first_frame_not_heard_through(ab, monkeypatch):
    """Review round 2, M1: a pause that did not hold before the source — here
    it would take hold only in the music — is seen at the first frame of the
    source the meter shows during the silence (the lead's mark), and the
    fragment is stopped then: its music never reaches the output, nor plays
    through the silence to its resume."""
    monkeypatch.setattr(S, "MARGIN_S", 1.5)               # a silence long enough to tell
    hqp_ab.open_sitting("pair", "ext2")
    ab.wait_phase("ready")
    fake = ab.fake
    stops = []
    real_stop = fake._stop
    fake._stop = lambda now: (stops.append(now), real_stop(now))[1]
    t0 = time.time()
    once = _late_pause_once(ab, S.LEAD_MIN_S + 0.1)       # past the lead: in the music
    _run_pair(ab)
    assert once and ab.q("SELECT cause::text FROM hqp_ab_slips WHERE NOT warm") == [("mark",)]
    source = once[0] + Params().b + PRE_S                  # when that fragment's source began
    # stopped before the music would have left HQPlayer, and before anything
    # of that fragment was heard
    assert any(once[0] < t < source + S.LEAD_MIN_S for t in stops), (once, stops, source)
    heard = [(on, off) for on, off, _ in fake.output(t0) if once[0] < on < source + S.LEAD_MIN_S + 1.0]
    assert heard == [], heard


def test_the_track_s_own_channels_are_what_the_screen_reads_speeds_by(ab, monkeypatch, tmp_path):
    """Review round 3: the speeds measured here are by source channels — the
    queue's entry read from the database carries a 5.1 file's six, and the
    screen asks for the speeds of six."""
    from playback.queue import QueueItem
    from playback import hqp_load
    song = tmp_path / "six.flac"
    song.write_bytes(b"fLaC")
    with ab.conn.cursor() as cur:
        cur.execute("INSERT INTO tracks (id, title) VALUES (gen_random_uuid(), 'Six') RETURNING id")
        track = cur.fetchone()[0]
        cur.execute("INSERT INTO albums (id, title) VALUES (gen_random_uuid(), 'Six') RETURNING id")
        album = cur.fetchone()[0]
        cur.execute("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, %s) RETURNING id",
                    (album, str(tmp_path)))
        cur.execute("INSERT INTO media_files (track_id, album_variant_id, file_path, file_format, sample_rate, "
                    "bit_depth, channels, duration_seconds) VALUES (%s, %s, %s, 'FLAC', 44100, 24, 6, 120) "
                    "RETURNING id", (track, cur.fetchone()[0], str(song)))
        mf = cur.fetchone()[0]
    ab.conn.commit()

    class M:
        latest_status = {"track_index": 1}
        spot = staticmethod(lambda: None)

        class queue:
            @staticmethod
            def item_at(i):
                return QueueItem(track_id=str(track), media_file_id=mf,
                                 source={"kind": "file", "path": str(song), "format": "FLAC"})
    got = _REAL_TRACK(M)
    assert got["channels"] == 6 and got["rate"] == 44100
    asked = []
    real = hqp_load.headroom
    monkeypatch.setattr(hqp_load, "headroom", lambda ctx, **kw: (asked.append(kw), real(ctx, **kw))[1])
    monkeypatch.setattr(hqp_ab, "_track", lambda mgr: {**got, "title": "Six", "artist": "A"})
    hqp_ab.open_sitting("pair", "ext2")
    ab.wait_phase("ready")
    assert asked and all(kw.get("src_channels") == 6 for kw in asked), asked


def test_a_pause_hqplayer_refuses_ends_the_sitting_in_words(ab):
    ab.fake.refuse_pause = True
    hqp_ab.open_sitting("pair", "ext2")
    v = ab.wait_phase("ended", timeout=60)
    assert v["ended"] == "failed" and v["note"] == "HQPlayer did not pause the passage (not now)", v["note"]


def test_the_chain_s_step_says_whether_their_filter_is_in_it(ab):
    """Review round 2, L4: their filter is in the first step, in a check while
    it is the champion, and in the next step after such a check (the fragment
    it is compared with) — never in a next step after another's."""
    hqp_ab.open_sitting("chain")
    ab.wait_phase("ready")
    ab.begin()
    seen = []
    for a in ("same", "worse", "better", "same"):                 # the check passed: theirs stays
        if ab.phase() != "testing":
            break
        assert _wait(lambda: (hqp_ab.view().get("step") or {}).get("answerable"), 60)
        st = hqp_ab.view()["step"]
        seen.append((st["kind"], st["yours"]))
        ab.answer(a)
    hqp_ab.command("stop")
    ab.wait_phase("result", "ended")
    # first (theirs + a probe), next (a probe after a probe), check after
    # "worse" (theirs again), next (a probe against theirs)
    assert seen == [("first", True), ("next", False), ("check", True), ("next", True)], seen


def test_a_slow_build_s_slip_is_recorded_as_late_and_a_resume_that_moved_as_the_check(ab):
    """§13 Q9: each slip says what read it — a build slower than its warm-up
    ('late'), a resume's way to the output that changed ('check')."""
    hqp_ab.open_sitting("pair", "sinc-M")
    ab.wait_phase("ready")
    s = hqp_ab._sitting
    ab.fake.params["sinc-M"] = Params(b=_p(s.yours, "sinc-M") + 0.5)
    ab.fake.params[s.yours] = Params(rl=0.4)
    _run_pair(ab, ("1", "same", "same"))
    causes = {c for (c,) in ab.q("SELECT cause::text FROM hqp_ab_slips WHERE NOT warm")}
    assert {"late", "check"} <= causes, causes


def test_the_app_stopping_mid_sitting_puts_hqplayer_back(ab):
    fake = ab.fake
    before = fake.volume
    hqp_ab.open_sitting("chain")
    ab.wait_phase("ready")
    ab.begin()
    hqp_ab.shutdown(20)
    assert not hqp_ab.running()
    assert fake.FILTERS[fake.slots["x1"]] == "xla" and fake.volume == before
    assert ab.q("SELECT ended::text, owed FROM hqp_ab_sittings") == [("failed", False)]


def test_the_track_the_person_was_hearing_tails_out_before_the_warm_up(ab):
    """Review round 1, H1: the person taps Compare while listening. HQPlayer
    sends all-zero frames through its output delay after the Stop; taken
    for the first warm-up play's own, they would pause it before it began."""
    fake = ab.fake
    assert _wait(lambda: fake.playlist, 10)                 # the queue mirrored at the attach
    with fake._lock:
        now = time.time()
        f = Params(d=1.5, extra=1.0)
        fake.entry = (fake.playlist[0], 0.0, 300.0)
        fake.stream0 = now
        fake.seg.append((now, "play", 10.0, f, fake.entry))
        fake._ends(now, 10.0, f)
    hqp_ab.open_sitting("pair", "ext2")
    ab.wait_phase("ready")
    s = hqp_ab._sitting
    assert s.slipped == 0 and s.lead_s < 0.8, (s.lead_s, s.timings)


def test_a_pair_switched_to_find_my_filter_leaves_out_one_at_another_rate(ab):
    """Review round 1, M2: the chain's pool warmed up from a pair loses a
    filter at another output rate, whichever form asked."""
    ab.fake.params["ringy"] = Params(rate=768000)
    hqp_ab.open_sitting("pair", "ext2")
    ab.wait_phase("ready")
    hqp_ab.command("form", form="chain")
    assert _wait(lambda: hqp_ab.view()["form"] == "chain" and hqp_ab.view()["phase"] == "ready", 60)
    s = hqp_ab._sitting
    assert "ringy" not in s.pool and "ringy" not in s.search.pool
    ab.begin()
    for a in ("same", "same", "same"):
        if ab.phase() != "testing":
            break
        ab.answer(a)
    v = ab.wait_phase("result", "ended")
    assert v["ended"] in ("done", None) or v["phase"] == "result", v


def test_an_answer_is_taken_once_while_one_is_awaited_and_only_in_its_form(ab):
    """Review round 1, M8: an answer before the step is heard, a second one
    for the same step, and another form's answer are refused."""
    hqp_ab.open_sitting("chain")
    ab.wait_phase("ready")
    ab.begin()
    with pytest.raises(hqp_ab.Refused):
        hqp_ab.command("answer", answer="same")             # the fragments still play
    with pytest.raises(hqp_ab.Refused):
        hqp_ab.command("answer", answer="1")                # the pair's
    assert _wait(lambda: (hqp_ab.view().get("step") or {}).get("answerable"), 60)
    hqp_ab.command("answer", answer="same")
    with pytest.raises(hqp_ab.Refused):
        hqp_ab.command("answer", answer="same")             # a second device's, a duplicate
    assert _wait(lambda: ab.q("SELECT count(*) FROM hqp_ab_duels")[0][0] == 1, 10)
    hqp_ab.command("stop")
    ab.wait_phase("result", "ended")
    assert ab.q("SELECT count(*) FROM hqp_ab_duels")[0][0] == 1


# -- swaps and put back (tests 6, 13) --------------------------------------------------------------

def test_a_better_writes_the_slot_at_once_and_stop_puts_back_the_champion(ab):
    fake = ab.fake
    hqp_ab.open_sitting("chain")
    ab.wait_phase("ready")
    ab.begin()
    tap = time.time()
    ab.answer("better")
    champ = hqp_ab._sitting.chain.champion
    assert champ != "xla"
    # the slot holds the challenger from the answer on, before the next
    # fragment is handed over (1x: the source is 44.1k; Nx as it was)
    assert _wait(lambda: any(t > tap for t in fake.adds), 30)
    nxt = min(t for t in fake.adds if t > tap)
    assert any(tap < t < nxt and x1 == fake.FILTERS.index(champ) and nx == 0
               for t, nx, x1 in fake.sets)
    hqp_ab.command("stop")
    ab.wait_phase("result", "ended")
    assert _wait(lambda: ab.mgr.held is None, 20)
    assert fake.FILTERS[fake.slots["x1"]] == champ and fake.slots["nx"] == 0
    row = ab.q("SELECT ended::text, owed, champion, picked FROM hqp_ab_sittings")
    assert row == [("stopped", False, champ, champ)]


def test_the_pair_puts_back_his_own_filter_and_the_volume(ab):
    fake = ab.fake
    before = fake.volume
    hqp_ab.open_sitting("pair", "ext2")
    ab.wait_phase("ready")
    hqp_ab.command("learn")
    assert _wait(lambda: hqp_ab.view()["learned"] == 1, 30)
    ab.begin()
    ab.answer("2")
    hqp_ab.command("stop")
    ab.wait_phase("result", "ended")
    assert _wait(lambda: ab.mgr.held is None, 20)
    assert fake.FILTERS[fake.slots["x1"]] == "xla" and fake.volume == before
    assert ab.q("SELECT ended::text, learn_plays, form::text FROM hqp_ab_sittings") == [("stopped", 1, "pair")]
    # the output came back to the queue
    assert _wait(lambda: isinstance(ab.mgr.active, HqpBackend) and ab.mgr.held is None, 10)


def test_a_sitting_the_process_died_in_is_put_back_at_the_next_answer(ab):
    fake = ab.fake
    with ab.conn.cursor() as cur:
        cur.execute("""INSERT INTO hqp_ab_sittings (hqp_endpoint_id, engine, mode, slot, fractional, form,
                                                    from_s, length_s, tolerance_s, before_filter_nx,
                                                    before_filter_1x, before_volume, muted_to, champion)
                       VALUES (%s, '6.3.3', 'pcm', '1x', FALSE, 'chain', 0, 10, 0.15, 'xla', 'xla',
                               -10, -60, 'hires')""", (ab.endpoint_id,))
    fake.slots["x1"] = TimedHqp.FILTERS.index("ringy")       # a candidate, at the warm-up's level
    fake.volume = -60.0
    hqp_ab.recover(ab.endpoint_id, hl.selected())
    assert _wait(lambda: ab.q("SELECT ended::text FROM hqp_ab_sittings")[0][0] == "failed", 10)
    assert fake.FILTERS[fake.slots["x1"]] == "hires" and fake.volume == -10.0


def test_a_put_back_hqplayer_refuses_twice_is_left_to_the_person_and_said_once(ab):
    # AB-1-DESIGN §17: the same refusal twice closes the row as left — no
    # third SetFilter, nothing owed — and one report names what to set:
    # the filter and the volume both, when HQPlayer took neither
    from routers import settings as settings_router
    fake = ab.fake
    with ab.conn.cursor() as cur:
        cur.execute("""INSERT INTO hqp_ab_sittings (hqp_endpoint_id, engine, mode, slot, fractional, form,
                                                    from_s, length_s, tolerance_s, before_filter_nx,
                                                    before_filter_1x, before_volume, muted_to, champion)
                       VALUES (%s, '6.3.3', 'pcm', '1x', FALSE, 'chain', 0, 10, 0.15, 'xla', 'xla',
                               -10, -60, 'hires')""", (ab.endpoint_id,))
    fake.slots["x1"] = TimedHqp.FILTERS.index("ringy")
    fake.volume = -60.0
    fake.refuse, fake.refuse_volume = {"hires"}, True
    tries = []

    def again():                    # the poller's next answer
        tries.append(time.time())
        hqp_ab.recover(ab.endpoint_id, hl.selected(), again=again)
    hqp_ab.recover(ab.endpoint_id, hl.selected(), again=again)
    assert _wait(lambda: ab.q("SELECT ended::text, owed, refused_filter, refused_volume FROM hqp_ab_sittings")
                 == [("left", False, "hires", -10)], 10)
    hqp_ab.recover(ab.endpoint_id, hl.selected(), again=again)     # nothing owed: nothing sent
    time.sleep(0.5)
    assert fake.refused == ["volume", "hires", "volume", "hires"] and len(tries) == 1
    assert fake.FILTERS[fake.slots["x1"]] == "ringy"
    notice = hqp_ab.refused_notice()
    assert notice["data"] == {"filter": "hires", "volume": -10.0, "mode": "pcm", "slot": "1x",
                              "hqplayer": "fake"}
    assert "hqplayer.ab_refused" in settings_router._REPORTS      # gone once seen: said once


def test_a_live_put_back_refused_then_refused_at_the_attach_is_left(ab):
    # §17's own path: the sitting's put-back refused (the first), and the
    # attach that takes the output back at the hold's release refused alike
    # (the second) — the row, ended stopped, closes as left
    fake = ab.fake
    hqp_ab.open_sitting("pair", "ext2")
    ab.wait_phase("ready")
    assert fake.FILTERS[fake.slots["x1"]] != "xla"          # a candidate in the slot
    fake.refuse = {"xla"}
    hqp_ab.command("stop")
    ab.wait_phase("ended")
    assert hqp_ab.view()["ended"] == "stopped"
    assert _wait(lambda: ab.mgr.held is None, 20)
    assert _wait(lambda: ab.q("SELECT ended::text, owed, refused_filter FROM hqp_ab_sittings")
                 == [("left", False, "xla")], 10)
    hqp_ab.recover(ab.endpoint_id, hl.selected())          # nothing owed: nothing sent
    time.sleep(0.5)
    assert fake.refused == ["xla", "xla"]


def test_a_volume_refused_alike_is_the_same_within_a_reads_step():
    # The live put-back holds HQPlayer's float, the row its NUMERIC(6,2)
    hqp_ab._refusals.pop(-1, None)
    assert not hqp_ab._refused_again(-1, (None, -10.004))
    assert hqp_ab._refused_again(-1, (None, -10.0))
    assert not hqp_ab._refused_again(-1, ("hires", -10.0))
    hqp_ab._refusals.pop(-1, None)


def test_a_put_back_paused_on_the_sittings_own_fragment_is_put_back(ab):
    # The narrow pass's H1: the connection lost past the link's reconnects
    # while HQPlayer paused on the fragment, and HQPlayer then answers PAUSED
    # on it — the sitting's own entry, never another controller's: put back
    fake = ab.fake
    from streaming import service as streaming_service
    proxy = streaming_service.get_proxy()
    token = proxy.register_file("/tmp/ab-fragment.flac", "audio/flac")
    uri = proxy.file_url(token)
    with fake._lock:
        fake.entry = (uri, 0.4, 0.9)
        fake.seg.append((time.time(), "pause", 0.4, Params(), fake.entry))
    with ab.conn.cursor() as cur:
        cur.execute("""INSERT INTO hqp_ab_sittings (hqp_endpoint_id, engine, mode, slot, fractional, form,
                                                    from_s, length_s, tolerance_s, before_filter_nx,
                                                    before_filter_1x, before_volume, muted_to, champion,
                                                    last_set, ended, ended_at, owed)
                       VALUES (%s, '6.3.3', 'pcm', '1x', FALSE, 'chain', 0, 10, 0.15, 'xla', 'xla',
                               -10, -60, 'hires', 'ringy', 'stopped', now(), TRUE)""", (ab.endpoint_id,))
    fake.slots["x1"] = TimedHqp.FILTERS.index("ringy")       # the candidate, at the warm-up's level
    fake.volume = -60.0
    assert ab.mgr.active.reads_foreign(ab.mgr.active._link.client.get_status())
    with pytest.raises(hqp_ab.Refused, match="still putting HQPlayer back"):
        hqp_ab.open_sitting("pair", "ext2")
    assert _wait(lambda: ab.q("SELECT ended::text, owed FROM hqp_ab_sittings") == [("stopped", False)], 10)
    assert fake.FILTERS[fake.slots["x1"]] == "hires" and fake.volume == -10.0


def test_two_put_backs_of_one_row_send_one_setfilter_never_a_third(ab):
    # The narrow pass's M1: the live put-back refused (the first), then two
    # recoveries that both read the row owed (the attach's, a door tap's) —
    # the one sends the second and closes the row, the other, its row read
    # before that close, sends nothing
    fake = ab.fake
    with ab.conn.cursor() as cur:
        cur.execute("""INSERT INTO hqp_ab_sittings (hqp_endpoint_id, engine, mode, slot, fractional, form,
                                                    from_s, length_s, tolerance_s, before_filter_nx,
                                                    before_filter_1x, champion, last_set, ended, ended_at,
                                                    owed)
                       VALUES (%s, '6.3.3', 'pcm', '1x', FALSE, 'chain', 0, 10, 0.15, 'xla', 'xla',
                               'hires', 'ringy', 'stopped', now(), TRUE)""", (ab.endpoint_id,))
    fake.slots["x1"] = TimedHqp.FILTERS.index("ringy")
    fake.refuse = {"hires"}
    link = hl.selected()
    stale = hqp_ab.owed(ab.endpoint_id)
    hqp_ab._refusals[stale["id"]] = hqp_ab._refusal(          # the live put-back's
        {"filterNx": 0, "filter1x": TimedHqp.FILTERS.index("ringy"), "volume": -10.0},
        (0, TimedHqp.FILTERS.index("hires")), "hires", None)
    for _ in range(2):
        hqp_ab._recover(dict(stale), link, lambda: None)
    assert fake.refused == ["hires"]
    assert ab.q("SELECT ended::text, owed, refused_filter FROM hqp_ab_sittings") == [("left", False, "hires")]


def test_a_dsp_change_from_elsewhere_ends_it_as_left_and_nothing_is_put_back(ab):
    fake = ab.fake
    hqp_ab.open_sitting("pair", "ext2")
    ab.wait_phase("ready")
    fake.slots["x1"] = TimedHqp.FILTERS.index("mp")         # HQPlayer's own window
    hqp_ab.command("learn")
    ab.wait_phase("ended")
    assert hqp_ab.view()["ended"] == "left"
    assert _wait(lambda: ab.mgr.held is None, 20)
    assert fake.FILTERS[fake.slots["x1"]] == "mp"
    assert ab.q("SELECT ended::text, owed FROM hqp_ab_sittings") == [("left", False)]


def test_a_window_change_while_it_waits_is_left_and_its_stop_sends_nothing(ab, monkeypatch):
    # §2.4: the filter or the volume changed in HQPlayer's window while the
    # sheet waits — the Stop that ends the sitting writes nothing over it
    from playback import hqp_benchmark
    fake = ab.fake
    hqp_ab.open_sitting("pair", "ext2")
    ab.wait_phase("ready")
    fake.slots["x1"] = TimedHqp.FILTERS.index("mp")
    fake.volume = -20.0
    sent, real = [], hqp_benchmark._Link.__call__

    def told(self, name, *a, **k):          # the sitting's own connection, not the backend's attach
        sent.append(name)
        return real(self, name, *a, **k)
    monkeypatch.setattr(hqp_benchmark._Link, "__call__", told)
    hqp_ab.command("stop")
    ab.wait_phase("ended")
    assert hqp_ab.view()["ended"] == "left"
    assert _wait(lambda: ab.mgr.held is None, 20)
    assert [n for n in sent if n in ("stop", "set_filter", "set_volume")] == [], sent
    assert fake.FILTERS[fake.slots["x1"]] == "mp" and fake.volume == -20.0
    assert ab.q("SELECT ended::text, owed FROM hqp_ab_sittings") == [("left", False)]


def test_a_sheet_left_from_another_sitting_stops_nothing_of_a_new_one(ab):
    hqp_ab.open_sitting("pair", "ext2")
    v = ab.wait_phase("ready")
    with pytest.raises(hqp_ab.Refused):
        hqp_ab.command("stop", sitting="a sitting before")
    assert hqp_ab.close("a sitting before") == {"phase": "idle"}
    assert hqp_ab.view()["phase"] == "ready"
    hqp_ab.command("stop", sitting=v["key"])
    ab.wait_phase("ended")


def test_a_sitting_still_owed_is_put_back_before_another_reads_yours(ab):
    fake = ab.fake
    with ab.conn.cursor() as cur:
        cur.execute("""INSERT INTO hqp_ab_sittings (hqp_endpoint_id, engine, mode, slot, fractional, form,
                                                    from_s, length_s, tolerance_s, before_filter_nx,
                                                    before_filter_1x, before_volume, muted_to, champion)
                       VALUES (%s, '6.3.3', 'pcm', '1x', FALSE, 'chain', 0, 10, 0.15, 'xla', 'xla',
                               -10, -60, 'hires')""", (ab.endpoint_id,))
    fake.slots["x1"] = TimedHqp.FILTERS.index("ringy")       # the last sitting's candidate, lowered
    fake.volume = -60.0
    with pytest.raises(hqp_ab.Refused, match="still putting HQPlayer back"):
        hqp_ab.open_sitting("pair", "ext2")
    assert _wait(lambda: ab.q("SELECT ended::text FROM hqp_ab_sittings")[0][0] == "failed", 10)
    assert fake.FILTERS[fake.slots["x1"]] == "hires" and fake.volume == -10.0


def test_a_person_s_dsp_change_closes_a_sitting_cut_short_as_left(ab):
    with ab.conn.cursor() as cur:
        cur.execute("""INSERT INTO hqp_ab_sittings (hqp_endpoint_id, engine, mode, slot, fractional, form,
                                                    from_s, length_s, tolerance_s, champion)
                       VALUES (%s, '6.3.3', 'pcm', '1x', FALSE, 'chain', 0, 10, 0.15, 'hires')""",
                    (ab.endpoint_id,))
    hqp_ab.left_by_person("127.0.0.1", ab.fake.port)
    assert ab.q("SELECT ended::text, owed FROM hqp_ab_sittings") == [("left", False)]
    assert hqp_ab.owed(ab.endpoint_id) is None


def test_a_database_failing_costs_the_left_record_never_the_persons_change(ab, monkeypatch, caplog):
    # Called beside hqp_benchmark.left_by_person inside the person's DSP
    # change and their switch away: a raise there would undo what they did
    import psycopg2

    def down(*a, **k):
        raise psycopg2.OperationalError("server closed the connection unexpectedly")
    monkeypatch.setattr(hqp_ab, "db_execute", down)
    hqp_ab.left_by_person("127.0.0.1", ab.fake.port)
    assert "not recorded as left" in caplog.text


# -- hidden, no listen, refused (tests 12, 15, 16) ------------------------------------------------

def test_while_it_sits_no_surface_names_a_filter_and_nothing_is_a_listen(ab, monkeypatch):
    from routers import hqplayer as hqp_router
    monkeypatch.setattr(hqp_router, "manager", ab.mgr)
    hqp_ab.open_sitting("pair", "ext2")
    ab.wait_phase("ready")
    state = hqp_router.get_state(dsp=True)
    assert state.get("held") == hqp_ab.LABEL and "state" not in state and "filters" not in state
    assert ab.mgr.held["label"] == "Comparing filters"
    hqp_ab.command("learn")
    assert _wait(lambda: hqp_ab.view()["learned"] == 1, 30)
    hqp_ab.close()
    assert _wait(lambda: ab.mgr.held is None, 20)
    assert ab.q("SELECT count(*) FROM listening_history")[0][0] == 0


def test_a_filter_at_another_output_rate_is_left_out_with_its_reason(ab):
    ab.fake.params["ringy"] = Params(rate=768000)
    hqp_ab.open_sitting("pair", "ringy")
    v = ab.wait_phase("ready")
    assert v["second"] is None
    assert any(x["name"] == "ringy" and "plays at" in x["why"] for x in v["seconds"]["left_out"])
    # the slot held it last, left out or not: Stop puts the person's own back
    yours = hqp_ab._sitting.yours
    hqp_ab.command("stop")
    ab.wait_phase("ended")
    assert _wait(lambda: ab.mgr.held is None, 20)
    assert hqp_ab.view()["ended"] == "stopped" and ab.fake.FILTERS[ab.fake.slots["x1"]] == yours


def test_a_stop_while_a_candidate_warms_puts_the_persons_filter_back(ab):
    fake = ab.fake
    yours = fake.FILTERS[fake.slots["x1"]]
    before = fake.volume
    hqp_ab.open_sitting("chain")
    # a candidate in the slot, warming: set, not yet measured
    assert _wait(lambda: (s := hqp_ab._sitting) is not None and s.set_now not in (None, yours)
                 and s.set_now not in s.timings, 60), hqp_ab.view()
    hqp_ab.command("stop")
    ab.wait_phase("ended")
    assert _wait(lambda: ab.mgr.held is None, 20)
    assert hqp_ab.view()["ended"] == "stopped", hqp_ab.view()
    assert fake.FILTERS[fake.slots["x1"]] == yours and fake.volume == before
    assert ab.q("SELECT ended::text, owed FROM hqp_ab_sittings") == [("stopped", False)]


def test_a_track_from_a_stream_or_the_hqplayers_library_is_refused_in_words():
    from playback.queue import QueueItem
    for src, where in (({"kind": "hqp", "path": "/x.flac", "format": "FLAC", "endpoint": 1},
                        "the HQPlayer's own library"),
                       ({"kind": "proxy", "token": "t"}, "a stream")):
        class M:
            latest_status = {"track_index": 1}
            spot = staticmethod(lambda: None)

            class queue:
                @staticmethod
                def item_at(i):
                    return QueueItem(track_id=None, media_file_id=None, source=src)
        with pytest.raises(hqp_ab.Refused, match=where):
            hqp_ab._track(M)


# -- the record stays home (test 17) --------------------------------------------------------------

def test_no_path_that_leaves_the_node_names_the_record():
    root = BACKEND.parent
    # the share export, the seed, the sync's serve, pull, walk and carry, the
    # diagnostics a warrant collects and their protocol
    paths = ["backend/share.py", "backend/seed_export.py", "backend/seed_import.py",
             "backend/routers/sync.py", "backend/routers/peer_diag.py", "backend/routers/support.py",
             "desktop/p2p/sync_queries.py", "desktop/p2p/sync_server.py", "desktop/p2p/sync_walk.py",
             "desktop/sync_client.py", "desktop/p2p/diag_events.py", "desktop/p2p/diag_protocol.py",
             "desktop/p2p/diag_worker.py", "desktop/diag_bundle.py", "backend/diag_node.py"]
    for rel in paths:
        assert "hqp_ab_" not in (root / rel).read_text(encoding="utf-8"), rel


@pytest.mark.parametrize("bits,layout,ch", [(16, "stereo", 2), (24, "stereo", 2), (16, "mono", 1), (24, "5.1", 6)])
def test_the_fragment_is_the_passage_faded_after_its_lead_of_the_mark(tmp_path, bits, layout, ch):
    """AB-2 F12: the lead is the 24-bit pattern 0, +4, 0, -4 exactly (no
    dither) on every channel, the cut 24-bit whatever the source, its channel
    layout the source's (review round 1, L7), the music its samples."""
    import shutil
    import subprocess
    if shutil.which("ffmpeg") is None:
        pytest.skip("no ffmpeg")
    from streaming import transcode
    src = tmp_path / "tone.flac"
    fmt = ["-sample_fmt", "s16"] if bits == 16 else ["-sample_fmt", "s32", "-bits_per_raw_sample", "24"]
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=1000:sample_rate=44100:duration=5", "-af", f"aformat=channel_layouts={layout}",
                    *fmt, str(src)], check=True)
    old = transcode._CACHE_DIR
    transcode._CACHE_DIR = tmp_path / "cache"
    try:
        out = transcode.flac_fragment_path_for_file(str(src), 1.0, 2.0, lead=1.0, fade=0.02)
    finally:
        transcode._CACHE_DIR = old

    def pcm(path, *args):
        raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", *args, "-i", path, "-f", "s32le", "-"],
                             check=True, capture_output=True).stdout
        return np.frombuffer(raw, "<i4").reshape(-1, ch).astype(np.int64)
    x = pcm(out)
    assert len(x) == 3 * 44100
    lead = x[:44100] // 256
    assert (x[:44100] % 256 == 0).all()
    assert (lead == np.tile([0, 4, 0, -4], 44100 // 4 + 1)[:44100, None]).all()
    music = x[44100:]
    source = pcm(str(src), "-ss", "1.0", "-t", "2.0")
    fade = int(0.02 * 44100) + 2
    assert np.array_equal(music[fade:-fade], source[fade:len(music) - fade])
    c = int(np.abs(music).max(axis=0).argmax())                           # a channel the tone is in
    assert abs(music[10, c]) < abs(music[2000:4000, c]).max() / 8          # faded in
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=bits_per_raw_sample,channel_layout",
                            "-of", "csv=p=0", out], capture_output=True, text=True).stdout.strip()
    assert probe.split(",") == ["24", layout] or sorted(probe.split(",")) == sorted(["24", layout])


# -- an empty pool says why (HQP-BENCH-63 §6, B-120) ------------------------------------

NOT_TESTED = ("No filter is speed-tested on this computer for PCM on HQPlayer 6.3.3 yet: run the "
              "Benchmark, then compare.")


def _marks(monkeypatch, filters, refused=None):
    from playback import hqp_load
    monkeypatch.setattr(hqp_load, "headroom", lambda ctx, **kw: {
        "filters": filters, "refused": {"filters": refused or {}, "shapers": {}}})


def test_no_filter_speed_tested_here_is_said_with_the_benchmark_as_the_way(ab, monkeypatch):
    """B-120: no filter measured for this engine, CUDA state and mode — the
    chain's pool was empty for that reason alone, and the words blamed the
    answers. Now the cause, and the Benchmark as the way; the pair's list
    says the same in place of "one you choose"."""
    _marks(monkeypatch, {})
    with pytest.raises(hqp_ab.Refused) as e:
        hqp_ab.open_sitting("chain")
    assert (str(e.value), e.value.code, e.value.way) == (NOT_TESTED, "chain", "benchmark")
    v = hqp_ab.open_sitting("pair")
    assert v["seconds"]["list"] == [] and v["seconds"]["empty"] == {"words": NOT_TESTED, "way": "benchmark"}


def test_every_other_filter_too_slow_here_is_said_as_that(ab, monkeypatch):
    _marks(monkeypatch, {n: {"class": "no", "speed": 0.6} for n in TimedHqp.FILTERS})
    with pytest.raises(hqp_ab.Refused) as e:
        hqp_ab.open_sitting("chain")
    assert (str(e.value), e.value.way) == ("Every other filter is too slow here at 705.6 kHz.", None)


def test_the_answers_emptying_the_pool_keep_their_own_words(ab, monkeypatch):
    """Only when candidates exist and the answers left none is it the
    answers' sentence."""
    monkeypatch.setattr(S, "pool", lambda search, **kw: [])
    with pytest.raises(hqp_ab.Refused) as e:
        hqp_ab.open_sitting("chain")
    assert str(e.value).startswith("Nothing left to compare yours with here: every kind of difference "
                                   "was heard as none") and e.value.way is None


def test_a_filter_over_10_minutes_to_start_is_left_out_with_its_reason(ab, monkeypatch):
    """HQP-BENCH-63 §3.2, §12.2: a combination that took over a minute to
    start here is left out of both forms, said as that — never "not
    measured"; a rung its
    ladder answered for reads as its class (too slow: slower than at a
    rate)."""
    marks = {n: {"class": "ok", "speed": 2.0} for n in TimedHqp.FILTERS if n not in ("ringy", "mp")}
    marks["mp"] = {"class": "no", "speed": None, "implied_from": 352800}
    _marks(monkeypatch, marks, refused={"ringy": "slow_start"})
    v = hqp_ab.open_sitting("chain")
    why = {r["name"]: r["why"] for r in v["chain"]["left_out"]}
    assert why["ringy"] == "Over a minute to start here"
    assert why["mp"] == "Too slow here: slower than at 352.8 kHz"
    assert {r["name"] for r in v["seconds"]["left_out"]} >= {"ringy", "mp"}
