"""Comparing filters by ear (backend/playback/hqp_ab.py) against a fake
HQPlayer that keeps time: each filter builds after a Play, plays its entry
at the wall clock's pace, pauses and resumes with a latency of its own, and
its sound leaves the "output" its output delay after the meter shows it —
the meter a port of its own sending frames a hop apart in 160 ms bursts,
all-zero through the lead, a pause and the tail. The tests read the
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

BINS = 129
FRAME_RATE = 5512.5
HOP = (BINS - 1) / FRAME_RATE          # 23.2 ms, HQPlayer's at 44.1 kHz
BURST = 0.16
METER_LAG = 0.02


def _frame(sound: bool) -> bytes:
    re = np.zeros(BINS, "<f4")
    if sound:
        re[3] = 0.1
    return (struct.pack("<IIIiffff", 1, 1, BINS, 16, FRAME_RATE / 2, HOP, 2.0, 0.0)
            + struct.pack("<ffff", 0.0, 0.0, -3.0, -3.0) + re.tobytes() + np.zeros(BINS, "<f4").tobytes())


ZERO, SOUND = _frame(False), _frame(True)


@dataclass
class Params:
    b: float = 0.05            # Play → the first frame (the build)
    d: float = 0.3             # the output delay
    rl: float = 0.03           # a resume's latency
    rate: int = 705600
    extra: float = 0.0         # frames past the delay after the end (sinc-M's 2 s in the lab)


class TimedHqp(FakeHqp):
    """FakeHqp taught time (the module docstring). Segments of what its
    engine does, by wall time: (t0, kind, position at t0, the filter's
    params, the entry: (uri, lead, total)) — kind 'build', 'play', 'pause',
    'hold' (a resume's latency), 'tail' (all-zero frames after the end or a
    Stop), 'off'. Kept whole: the output is read off them after the test."""

    FILTERS = ["xla", "sinc-M", "ext2", "hires", "long-lp", "mp", "ringy"]
    STREAMING = ("play", "pause", "hold", "tail")

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
        self.adds = []                 # times of each cut's PlaylistAdd
        self.stream0 = None            # when the meter's stream of the entry began
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
            self.seg += [(now, "build", 0.0, f, self.entry), (start, "play", 0.0, f, self.entry)]
            self._ends(start, 0.0, f)
        elif kind == "pause":
            self._future_cut(now)
            self.seg += [(now, "hold", pos, f, self.entry), (now + f.rl, "play", pos, f, self.entry)]
            self._ends(now + f.rl, pos, f)

    def _pause(self, now):
        _, kind, _, f, _ = self._now_seg(now)
        if kind == "play":
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
        """None (no frame), or whether the frame at `t` sounds. Under the lock."""
        _, kind, _, _, entry = self._now_seg(t)
        if entry is None or kind not in self.STREAMING:
            return None
        return kind == "play" and entry[1] <= self.pos_at(t) < entry[2]

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
            on = t0 + max(0.0, entry[1] - pos0)
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
                self._pause(now)
                return '<Pause result="OK"/>'
            if cmd == "Stop":
                self._stop(now)
                return '<Stop result="OK"/>'
            _, kind, _, f, entry = self._now_seg(now)
            state = {"build": 2, "play": 2, "hold": 2, "pause": 1}.get(kind, 0)
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
        s0, j, over = None, 1, None
        while not self._done.wait(self.TICK):
            now = time.time()
            data = []
            with self.fake._lock:
                if self.fake.stream0 not in (s0, over):
                    s0, j = self.fake.stream0, 1
                while s0 is not None:
                    t = s0 + j * HOP
                    due = s0 + math.ceil(j * HOP / BURST) * BURST + METER_LAG
                    if due > now:
                        break
                    sound = self.fake.frame_at(t)
                    if sound is None:
                        if self.fake._now_seg(t)[1] == "off":
                            s0, over = None, s0     # the stream is over: its frames stop
                        break
                    data.append(SOUND if sound else ZERO)
                    j += 1
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
        "offset": 0.0, "length": 120.0, "rate": 44100, "bits": 24, "channels": 2})

    def cut(path, start, length, *, lead, fade, bits):
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


# -- the warm-up and the silence (tests 1, 11) ------------------------------------------------

def test_a_chain_plays_every_fragment_after_the_same_silence(ab):
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
    p = v["silence_s"]
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
    # again and counted, P grown for it; none was heard
    assert row[0][0] == "done" and row[0][1] == hqp_ab.view()["slipped"]
    assert row[0][2] == pytest.approx(p, abs=0.01) if row[0][1] == 0 else row[0][2] > p


def test_a_build_slower_than_its_warm_up_is_played_again_with_the_silence_grown(ab):
    fake = ab.fake
    hqp_ab.open_sitting("pair", "sinc-M")
    v = ab.wait_phase("ready")
    p = v["silence_s"]
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
    """The sitting's thread woken late on a fragment's first frame: its pause
    lands further into the lead, and its resume is timed from there."""
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
    assert ab.q("SELECT slipped FROM hqp_ab_sittings")[0][0] == hqp_ab.view()["slipped"]


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


def test_a_warm_up_pause_in_the_music_plays_it_again_on_a_longer_lead(ab, monkeypatch):
    """Review round 1, M1 (§16): the warm-up's pause landing too near the
    music lengthens the lead and plays that filter again — never a slip,
    never the sitting's end."""
    monkeypatch.setattr(S, "LEAD_MIN_S", 0.2)
    hqp_ab.open_sitting("pair", "ext2")
    v = ab.wait_phase("ready")
    s = hqp_ab._sitting
    assert v["ended"] is None and s.warm_lead > 0.2
    assert s.lead_s >= max(t.p for t in s.timings.values()) + S.LEAD_ABOVE_S - 1e-9


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


def test_the_fragment_is_the_passage_faded_after_its_lead_of_digital_silence(tmp_path):
    import shutil
    import subprocess
    if shutil.which("ffmpeg") is None:
        pytest.skip("no ffmpeg")
    from streaming import transcode
    src = tmp_path / "tone.flac"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=1000:sample_rate=44100:duration=5", "-ac", "2", "-sample_fmt", "s32",
                    "-bits_per_raw_sample", "24", str(src)], check=True)
    old = transcode._CACHE_DIR
    transcode._CACHE_DIR = tmp_path / "cache"
    try:
        out = transcode.flac_fragment_path_for_file(str(src), 1.0, 2.0, lead=1.0, fade=0.02, bits=24)
    finally:
        transcode._CACHE_DIR = old
    raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", out, "-f", "s32le",
                          "-ac", "1", "-"], check=True, capture_output=True).stdout
    x = np.frombuffer(raw, "<i4").astype(float) / 2 ** 31
    assert len(x) == pytest.approx(3 * 44100, abs=64)
    assert not x[:44100].any()                                  # the lead: digital silence
    assert abs(x[44100 + 10]) < 0.05 and np.abs(x[44100 + 2000:44100 + 4000]).max() > 0.05   # faded in (the sine at 1/8)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=bits_per_raw_sample",
                            "-of", "csv=p=0", out], capture_output=True, text=True).stdout.strip()
    assert probe == "24"


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


def test_a_filter_that_stopped_hqplayer_is_left_out_with_its_reason(ab, monkeypatch):
    """HQP-BENCH-63 §3.2: a combination that stopped HQPlayer here is left
    out of both forms, said as that — never "not measured"; a rung its
    ladder answered for reads as its class (too slow: slower than at a
    rate)."""
    marks = {n: {"class": "ok", "speed": 2.0} for n in TimedHqp.FILTERS if n not in ("ringy", "mp")}
    marks["mp"] = {"class": "no", "speed": None, "implied_from": 352800}
    _marks(monkeypatch, marks, refused={"ringy": "hung"})
    v = hqp_ab.open_sitting("chain")
    why = {r["name"]: r["why"] for r in v["chain"]["left_out"]}
    assert why["ringy"] == "Stops HQPlayer here: it stopped responding while preparing it"
    assert why["mp"] == "Too slow here: slower than at 352.8 kHz"
    assert {r["name"] for r in v["seconds"]["left_out"]} >= {"ringy", "mp"}
