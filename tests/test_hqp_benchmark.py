"""The HQPlayer benchmark (backend/playback/hqp_benchmark.py).

The grid is built from the lists a real HQPlayer Desktop 6 answered
(tests/fixtures/hqp_lists, names and indices only); the settling rule runs
on synthetic traces. Whole runs — their rows and the ledger a later run
skips by, the probe row of a first run, cancel, a connection HQPlayer drops
under the run, a run cut short and put back when HQPlayer returns, the hold
of the output, the app stopping — run against the fake control port of
test_hqp_backend taught the DSP commands, a real PostgreSQL (a throwaway
database built from the migrations, the pool pointed at it) and real test
signals from ffmpeg. Never a real HQPlayer: a transport command on the
owner's output is an audible action, not a test.
"""

import json
import random
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

psycopg2 = pytest.importorskip("psycopg2")

from config import settings  # noqa: E402
from hqplayer_client import HQPlayerClient, PlaybackState, TrackStatus  # noqa: E402
from playback import hqp_backend as hb  # noqa: E402
from playback import hqp_benchmark as bench  # noqa: E402
from playback.hqp_backend import HqpBackend  # noqa: E402
from playback.manager import OutputHeld, PlaybackManager  # noqa: E402
from streaming import service as streaming_service  # noqa: E402
from streaming.proxy import MediaProxy  # noqa: E402

from test_hqp_backend import FakeHqp, _item, _wait  # noqa: E402
from test_hqp_load import PG, _make_db  # noqa: E402

LISTS = Path(__file__).resolve().parent / "fixtures" / "hqp_lists"
DBNAME = "sautium_hqp_bench_test"


def _lists(name):
    d = json.loads((LISTS / name).read_text())
    st = d["state"]
    current = {"rate": st["rate"], "filterNx": st["filterNx"], "filter1x": st["filter1x"],
               "shaper": st["shaper"]}
    return d, current


SRC44 = {"pcm44": {"rate": 44100, "channels": 2}}


# -- the grid ---------------------------------------------------------------------

def test_sdm_grid_from_a_real_hqplayers_lists():
    d, cur = _lists("desktop_sdm.json")
    pts = bench.build_grid("sdm", filters=d["filters"], shapers=d["shapers"], rates=d["rates"],
                           current=cur, srcs=SRC44, covered=set(), first_run=True,
                           rng=random.Random(3))
    real = [r for r in d["rates"] if r["rate"] > 0]
    # every modulator at every DSD rate with the owner's filter, every filter
    # at the owner's rate with the owner's modulator, the owner's own setting
    # twice — less the two points the own setting already is
    assert len(pts) == len(d["shapers"]) * len(real) + len(d["filters"]) - 2 + 2
    assert pts[0].own and pts[-1].own and pts[0] == pts[-1]
    assert [p for p in pts[1:-1] if p.own] == []
    keys = [p.key for p in pts[1:-1]]
    assert len(keys) == len(set(keys))
    # the first run probes the highest rate first: the host's boundary is
    # there (the owner listens at 256×, so all of 512× is the probe row)
    top = max(r["rate"] for r in real)
    probe = pts[1:1 + len(d["shapers"])]
    assert {p.rate_hz for p in probe} == {top}
    assert {p.shaper for p in probe} == {s["index"] for s in d["shapers"]}
    # the owner's own filter pair runs the 1x slot for a 44.1 kHz source
    names = {f["index"]: f["name"] for f in d["filters"]}
    assert pts[0].key[1] == names[cur["filter1x"]]
    # a seeded shuffle is the same shuffle
    again = bench.build_grid("sdm", filters=d["filters"], shapers=d["shapers"], rates=d["rates"],
                             current=cur, srcs=SRC44, covered=set(), first_run=True,
                             rng=random.Random(3))
    assert again == pts


def test_a_later_run_fills_only_the_gaps():
    d, cur = _lists("desktop_sdm.json")
    first = bench.build_grid("sdm", filters=d["filters"], shapers=d["shapers"], rates=d["rates"],
                             current=cur, srcs=SRC44, covered=set(), first_run=True,
                             rng=random.Random(1))
    covered = {p.key for p in first[1:-11]}          # all but ten points asked since
    later = bench.build_grid("sdm", filters=d["filters"], shapers=d["shapers"], rates=d["rates"],
                             current=cur, srcs=SRC44, covered=covered | {first[0].key},
                             first_run=False, rng=random.Random(1))
    # the ten gaps, and the owner's setting first and last all the same
    assert len(later) == 12
    assert later[0].own and later[-1].own
    assert {p.key for p in later[1:-1]} == {p.key for p in first[-11:-1]}


def test_pcm_grid_keys_an_automatic_rate_by_what_was_asked():
    d, cur = _lists("desktop_sdm.json")            # PCM lists hold the same filter names
    shapers = [{"index": 0, "name": "none", "value": 0}, {"index": 1, "name": "LNS15", "value": 1}]
    rates = [{"index": 0, "rate": 0}, {"index": 1, "rate": 352800}, {"index": 2, "rate": 705600}]
    current = {"rate": 0, "filterNx": cur["filterNx"], "filter1x": cur["filter1x"], "shaper": 1}
    srcs = {**SRC44, "pcm96": {"rate": 96000, "channels": 2}}
    pts = bench.build_grid("pcm", filters=d["filters"], shapers=shapers, rates=rates, current=current,
                           srcs=srcs, covered=set(), first_run=False, rng=random.Random(2))
    # every filter × {the owner's rate (auto here), the highest} × {44.1, 96 kHz},
    # the owner's own setting twice — less the point it already is (its 1x
    # filter at the owner's automatic rate from a 44.1 kHz source)
    assert len(pts) == len(d["filters"]) * 2 * 2 + 2 - 1
    assert {p.shaper for p in pts} == {1}
    # an automatic rate plays another rate than the one asked: its point is
    # keyed by the ask (0), which a run's ledger row covers like any other
    auto = [p for p in pts if p.rate_hz == 0]
    assert auto and all(p.key[0] == 0 for p in auto)
    again = bench.build_grid("pcm", filters=d["filters"], shapers=shapers, rates=rates,
                             current=current, srcs=srcs, covered={p.key for p in pts},
                             first_run=False, rng=random.Random(2))
    assert [p.own for p in again] == [True, True]


def test_rates_are_named_as_the_hqplayer_screen_names_them():
    # app-shell.js fmtRateLabel, the 32k and 22.05k families included
    assert [bench._fmt_rate(hz) for hz in (44100, 48000, 705600, 768000, 352800)] == \
        ["44.1 kHz", "48 kHz", "705.6 kHz", "768 kHz", "352.8 kHz"]
    assert [bench._fmt_rate(hz) for hz in (11289600, 12288000, 2048000, 1411200, 3000000)] == \
        ["44.1k × 256", "48k × 256", "32k × 64", "22.05k × 64", "3 MHz"]


# -- settling ----------------------------------------------------------------------

def _st(state=PlaybackState.PLAYING, position=0.0, speed=2.0, out=0.99, inp=-1.0):
    return TrackStatus(state=state, track_index=1, track_id="", position=position, length=120.0,
                       volume=-60.0, process_speed=speed, input_fill=inp, output_fill=out,
                       active_mode="SDM (DSD)", active_filter="f", active_shaper="s",
                       active_rate=11289600, src_rate=44100, src_bits=24, src_channels=2,
                       src_sdm=False)


def _feed(settle, trace, t0=0.0):
    """trace: (seconds after Play, status); the verdict and when it came."""
    for t, st in trace:
        v = settle.feed(t0 + t, st)
        if v is not None:
            return t, v
    return None, None


def test_a_flat_average_settles_after_the_stable_seconds():
    trace = [(1, _st(position=0.0)), (2, _st(position=0.0))]          # initialising
    trace += [(t, _st(position=t - 2.5, speed=2.0 + 0.01 * (t % 2))) for t in range(3, 30)]
    t, v = _feed(bench.Settle(0.0), trace)
    assert v["settled"] and not v["dropout"]
    assert v["init_s"] == 3                                              # Play until it moved
    assert t == 3 + bench.STABLE_AFTER_S + bench.SETTLE_WINDOW - 1
    assert v["settle_s"] == t - 3
    assert v["speed"] == pytest.approx(2.0, abs=0.011)


def test_a_lagging_average_is_not_taken_for_settled():
    # a running average still climbing: inside the ±2 % band, but trending
    trace = [(t, _st(position=float(t), speed=2.0 + 0.008 * t)) for t in range(1, 40)]
    t, v = _feed(bench.Settle(0.0), trace)
    assert v["settled"] is False                                         # recorded at the cap
    assert t == 1 + bench.SETTLE_CAP_S
    assert v["speed"] == pytest.approx(bench._p10(v["readings"][-bench.SETTLE_WINDOW:]))


def test_the_count_starts_again_when_the_position_stalls_before_the_readings():
    trace = [(t, _st(position=float(t))) for t in range(1, 4)]
    trace += [(4, _st(position=3.0)), (5, _st(position=3.0))]          # a rebuild: no movement
    trace += [(t, _st(position=float(t - 2))) for t in range(6, 30)]
    t, v = _feed(bench.Settle(0.0), trace)
    assert v["settled"]
    assert t == 6 + bench.STABLE_AFTER_S + bench.SETTLE_WINDOW - 1


def test_one_tick_without_movement_is_no_stall():
    # a position HQPlayer updates a little slower than the ticks: one tick
    # sees no movement now and then, and the count goes on
    trace = [(t, _st(position=float(t))) for t in range(1, 4)]
    trace += [(4, _st(position=3.0))]
    trace += [(t, _st(position=float(t - 1))) for t in range(5, 30)]
    t, v = _feed(bench.Settle(0.0), trace)
    assert v["settled"]
    assert t == 1 + bench.STABLE_AFTER_S + bench.SETTLE_WINDOW - 1


def test_a_stop_once_the_readings_run_is_a_dropout_at_the_last_speed():
    trace = [(t, _st(position=float(t), speed=0.9 + 0.05 * (t % 3))) for t in range(1, 9)]
    trace += [(9, _st(state=PlaybackState.STOPPED, position=0.0, speed=0.0))]
    t, v = _feed(bench.Settle(0.0), trace)
    assert v["dropout"] and not v["settled"]
    assert v["speed"] == pytest.approx(0.9 + 0.05 * (8 % 3))


def test_a_stop_at_a_healthy_speed_fails_the_point():
    trace = [(t, _st(position=float(t), speed=2.5)) for t in range(1, 8)]
    trace += [(8, _st(state=PlaybackState.STOPPED, position=0.0, speed=0.0))]
    t, v = _feed(bench.Settle(0.0), trace)
    assert "failed" in v and "2.50×" in v["failed"] and "not the DSP" in v["failed"]


def test_before_the_readings_a_stop_counts_only_under_one():
    for speed, dropout in ((0.8, True), (1.2, False)):
        trace = [(t, _st(position=float(t), speed=speed)) for t in range(1, 4)]
        trace += [(4, _st(state=PlaybackState.STOPPED, position=0.0, speed=0.0))]
        _, v = _feed(bench.Settle(0.0), trace)
        assert ("dropout" in v and v["dropout"]) is dropout, (speed, v)


def test_a_buffer_still_filling_at_a_healthy_speed_is_no_dropout():
    # the output buffer low a second after the start, the DSP at 2.5×: filling
    trace = [(1, _st(position=1.0, speed=2.5, out=0.4)), (2, _st(position=2.0, speed=2.5, out=0.08))]
    trace += [(t, _st(position=float(t), speed=2.5)) for t in range(3, 30)]
    _, v = _feed(bench.Settle(0.0), trace)
    assert v["settled"] and not v["dropout"]
    assert v["speed"] == pytest.approx(2.5)


def test_a_drained_output_while_the_input_holds_is_a_dropout():
    trace = [(t, _st(position=float(t), speed=0.7, out=0.9 - 0.2 * t, inp=0.95)) for t in range(1, 12)]
    _, v = _feed(bench.Settle(0.0), trace)
    assert v["dropout"] and v["speed"] == pytest.approx(0.7)
    # one tick under the line once the readings run is no dropout yet
    dip = [(t, _st(position=float(t), speed=1.2, out=0.05 if t == 7 else 0.99)) for t in range(1, 30)]
    _, v = _feed(bench.Settle(0.0), dip)
    assert v["settled"] and not v["dropout"]
    # a stream that starves the input is the proxy's failure, not the DSP's
    starved = [(t, _st(position=float(t), speed=2.0, out=0.9 - 0.2 * t, inp=0.1)) for t in range(1, 12)]
    _, v = _feed(bench.Settle(0.0), starved)
    assert "failed" in v and "starved" in v["failed"]


def test_a_frozen_position_once_the_readings_run():
    for speed, dropout in ((0.9, True), (2.5, False)):
        trace = [(t, _st(position=float(t), speed=speed)) for t in range(1, 9)]
        trace += [(t, _st(position=8.0, speed=speed)) for t in range(9, 12)]
        _, v = _feed(bench.Settle(0.0), trace)
        assert v.get("dropout", False) is dropout, (speed, v)
        if not dropout:
            assert "froze" in v["failed"]


def test_a_setting_that_never_starts_fails_at_the_deadline():
    trace = [(t, _st(position=0.0)) for t in range(1, int(bench.START_DEADLINE_S) + 3)]
    t, v = _feed(bench.Settle(0.0), trace)
    assert "failed" in v and t == int(bench.START_DEADLINE_S) + 1


def test_a_point_without_a_verdict_fails_at_its_deadline():
    # moving and stalling over and over before the readings, at a speed that
    # is no dropout: no verdict ever comes — the point gives up
    trace, pos = [], 0.0
    for t in range(1, 200):
        pos += 1.0 if t % 4 in (1, 2) else 0.0
        trace.append((t, _st(position=pos, speed=1.2)))
    t, v = _feed(bench.Settle(0.0), trace)
    assert "failed" in v and "no verdict" in v["failed"]
    assert t == int(bench.POINT_DEADLINE_S) + 1


# -- the fake HQPlayer ----------------------------------------------------------------

class DspFake(FakeHqp):
    """The fake control port taught HQPlayer's DSP: per-mode lists and one
    selection per mode (as HQPlayer keeps them), State, the Set* commands,
    Volume/VolumeRange, and a Status that reports the source and a speed per
    setting. A modulator named "… 512+fs" is refused below 512×. A restart
    keeps the selections (HQPlayer saves them) and brings the volume back
    to `boot_volume` when one is set."""

    MODES = [("[source]", -1), ("PCM", 0), ("SDM (DSD)", 1)]
    RATES = {1: [0, 352800, 705600], 2: [0, 5644800, 11289600]}
    FILTERS = ["poly-sinc-gauss-long", "sinc-L"]
    SHAPERS = {1: ["none", "LNS15"], 2: ["ASDM7EC", "ASDM7EC-super", "ASDM7EC-light 512+fs"]}
    MINE = {"Status", "State", "GetModes", "GetRates", "GetFilters", "GetShapers", "SetMode",
            "SetRate", "SetFilter", "SetShaping", "Volume", "VolumeRange"}

    def __init__(self):
        super().__init__()
        self.mode = 2
        self.sel = {1: [2, 1, 1, 1], 2: [2, 0, 0, 0]}   # [rate, filterNx, filter1x, shaper]
        self.volume = -3.0
        self.boot_volume = None
        self.speeds = {}             # (filter, shaper, rate Hz) → speed; 2.0 otherwise
        self.jitter = 0.0
        self.volume_range = '<VolumeRange min="-60" max="0" enabled="1" adaptive="0"/>'
        self._polls = 0

    def _played_rate(self, src: int) -> int:
        rate = self.RATES[self.mode][self.sel[self.mode][0]]
        if rate:
            return rate
        return 11289600 if self.mode == 2 else (768000 if src % 48000 == 0 else 705600)

    def _dispatch(self, cmd, attrs):
        if cmd not in self.MINE:
            return super()._dispatch(cmd, attrs)
        with self._lock:
            self.commands.append((cmd, attrs))
            rate, nx, x1, sh = self.sel.get(self.mode, [0, 0, 0, 0])
            if cmd == "Status":
                playing = self.state == int(PlaybackState.PLAYING)
                if playing:
                    self.position += 1.0
                src = 44100
                running = self.FILTERS[x1 if src in (44100, 48000) else nx]
                key = (running, self.SHAPERS[self.mode][sh], self._played_rate(src))
                speed = 0.0
                if playing:
                    self._polls += 1
                    speed = self.speeds.get(key, 2.0) + (self.jitter if self._polls % 2 else -self.jitter)
                out = 0.99 if speed >= 1.0 or not playing else max(0.0, 0.99 - 0.3 * self.position)
                return (f'<Status state="{self.state}" track="{self.track}" position="{self.position}" '
                        f'length="120" volume="{self.volume}" tracks_total="{len(self.playlist)}" '
                        f'process_speed="{speed}" input_fill="-1" output_fill="{out}" '
                        f'active_mode="{self.MODES[self.mode][0]}" active_filter="{running}" '
                        f'active_shaper="{self.SHAPERS[self.mode][sh]}" active_rate="{key[2]}">'
                        + (f'<metadata samplerate="{src}" bits="24" channels="2" sdm="0"/>' if playing else '')
                        + '</Status>')
            if cmd == "State":
                return (f'<State mode="{self.mode}" active_mode="{self.MODES[self.mode][1]}" rate="{rate}" '
                        f'active_rate="{self.RATES[self.mode][rate]}" filter="{x1}" filter1x="{x1}" '
                        f'filterNx="{nx}" shaper="{sh}" volume="{self.volume}" convolution="0" '
                        f'adaptive="0" invert="0" matrix_profile=""/>')
            if cmd == "GetModes":
                return "<GetModes>" + "".join(f'<ModesItem index="{i}" name="{n}" value="{v}"/>'
                                              for i, (n, v) in enumerate(self.MODES)) + "</GetModes>"
            if cmd == "GetRates":
                return "<GetRates>" + "".join(f'<RatesItem index="{i}" rate="{r}"/>'
                                              for i, r in enumerate(self.RATES[self.mode])) + "</GetRates>"
            if cmd == "GetFilters":
                return "<GetFilters>" + "".join(f'<FiltersItem index="{i}" name="{n}" value="{i}" arg="1"/>'
                                                for i, n in enumerate(self.FILTERS)) + "</GetFilters>"
            if cmd == "GetShapers":
                return "<GetShapers>" + "".join(f'<ShapersItem index="{i}" name="{n}" value="{i}"/>'
                                                for i, n in enumerate(self.SHAPERS[self.mode])) + "</GetShapers>"
            if cmd == "VolumeRange":
                return self.volume_range
            if cmd == "SetMode":
                self.mode = int(attrs["value"])
            elif cmd == "SetRate":
                self.sel[self.mode][0] = int(attrs["value"])
            elif cmd == "SetFilter":
                self.sel[self.mode][1] = int(attrs["value"])
                self.sel[self.mode][2] = int(attrs.get("value1x", attrs["value"]))
            elif cmd == "SetShaping":
                wanted = int(attrs["value"])
                if ("512+fs" in self.SHAPERS[self.mode][wanted]
                        and self.RATES[self.mode][rate] < 512 * 44100):
                    return '<SetShaping result="Error">Modulator needs 512x rate</SetShaping>'
                self.sel[self.mode][3] = wanted
            elif cmd == "Volume":
                self.volume = float(attrs["value"])
            return f'<{cmd} result="OK"/>'

    def restart(self):
        with self._lock:
            self.restarted_at = len(self.commands)
        super().restart()
        if self.boot_volume is not None:
            with self._lock:
                self.volume = self.boot_volume

    def settings(self):
        return (self.mode, tuple(self.sel[1]), tuple(self.sel[2]), self.volume)


# -- runs against it ---------------------------------------------------------------------

def test_the_volume_range_is_trusted_only_as_hqplayer_gave_it():
    fake = DspFake()
    try:
        c = HQPlayerClient(host="127.0.0.1", port=fake.port, timeout=2.0, ring=False)
        assert c.connect()
        assert c.volume_range() == {"min": -60.0, "max": 0.0, "enabled": True, "adaptive": False}
        for reply in ('<VolumeRange/>', '<VolumeRange max="0" enabled="1"/>',
                      '<VolumeRange result="Error">No output</VolumeRange>'):
            fake.volume_range = reply
            assert c.volume_range() is None, reply
        fake.volume_range = '<VolumeRange min="-60" max="0"/>'      # no `enabled`: a fixed volume
        assert c.volume_range()["enabled"] is False
        c.disconnect()
    finally:
        fake.close()
    vr = {"min": -60.0, "max": 0.0, "enabled": True, "adaptive": False}
    assert bench.mute_level(vr, -3.0) == -60.0
    assert bench.mute_level(vr, -60.0) is None          # already at the bottom: nothing lowers it
    assert bench.mute_level({**vr, "enabled": False}, -3.0) is None
    assert bench.mute_level(None, -3.0) is None


class Run:
    def __init__(self, fake, mgr, conn, endpoint_id):
        self.fake, self.mgr, self.conn, self.endpoint_id = fake, mgr, conn, endpoint_id

    def q(self, sql, *params):
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def point(self) -> int:
        return ((self.mgr.held or {}).get("progress") or {}).get("point", 0)


@pytest.fixture(scope="module")
def dsn():
    admin, nb = _make_db(DBNAME)
    yield f"postgresql://{PG['user']}:{PG['password']}@{PG['host']}:{PG['port']}/{DBNAME}"
    nb._drop_database(admin, DBNAME)
    admin.close()


@pytest.fixture(scope="module")
def signals(tmp_path_factory):
    """The test signals, made by ffmpeg once for the module."""
    return tmp_path_factory.mktemp("hqp_bench")


@pytest.fixture
def run(dsn, signals, monkeypatch):
    """A manager with the fake HQPlayer attached as the output, its endpoint
    registered in a scratch database, and the clock of a point shrunk."""
    import db_pool
    pool = psycopg2.pool.ThreadedConnectionPool(1, 8, dsn=dsn, options="-c timezone=UTC")
    monkeypatch.setattr(db_pool, "_pool", pool)
    conn = psycopg2.connect(dsn, options="-c timezone=UTC")
    conn.autocommit = True
    fake = DspFake()
    with conn.cursor() as cur:
        cur.execute("TRUNCATE hqp_endpoints CASCADE")
        cur.execute("""INSERT INTO hqp_endpoints (name, host, port, product, hqp_engine, cuda)
                       VALUES ('fake', '127.0.0.1', %s, 'Signalyst HQPlayer Fake', '6.2.3', 'full')
                       RETURNING id""", (fake.port,))
        endpoint_id = cur.fetchone()[0]
    monkeypatch.setattr(settings, "hqplayer_host", "127.0.0.1")
    monkeypatch.setattr(settings, "hqplayer_port", fake.port)
    monkeypatch.setattr(settings, "media_proxy_advertised_host", "127.0.0.1")
    monkeypatch.setattr(streaming_service, "_proxy",
                        MediaProxy(port=0, advertised_host="127.0.0.1", file_token_key=b"k"))
    hb.reset_all_clients()
    hb._hqp_unreachable_until = 0.0
    monkeypatch.setattr(hb, "HQP_CIRCUIT_COOLDOWN", 0.2)
    monkeypatch.setattr(hb, "_notify_notices", lambda: None)
    monkeypatch.setattr(hb, "_stream_mode", lambda: True)
    import hqp_library
    from playback import manager as manager_mod
    from playback import substitute
    from routers import settings as settings_router
    monkeypatch.setattr(hqp_library, "request_sync", lambda host, port: None)
    monkeypatch.setattr(substitute, "native_plays", lambda items, output_id, endpoint_id: [None] * len(items))
    monkeypatch.setattr(settings_router, "_read", lambda key: "hqplayer" if key == "output.type" else None)
    mgr = PlaybackManager()
    mgr.queue.replace([_item("E:/Music/A/01.flac", 1), _item("E:/Music/A/02.flac", 2)])
    monkeypatch.setattr(manager_mod, "manager", mgr)
    monkeypatch.setattr(bench, "_SIGNAL_DIR", signals)
    monkeypatch.setattr(bench, "TICK_S", 0.01)
    monkeypatch.setattr(bench, "STABLE_AFTER_S", 0.03)
    monkeypatch.setattr(bench, "SETTLE_CAP_S", 0.3)
    monkeypatch.setattr(bench, "START_DEADLINE_S", 2.0)
    monkeypatch.setattr(bench, "SELECT_POLL_S", 0.01)
    monkeypatch.setattr(bench, "_RECONNECT_PAUSES", (0.05, 0.1))
    b = HqpBackend(emit=mgr._on_backend_status, queue=mgr.queue)
    mgr._active = b
    b.start()
    yield Run(fake, mgr, conn, endpoint_id)
    if bench.running():
        bench.cancel()
        _wait(lambda: not bench.running(), 20)
    if mgr.active is not None:
        mgr.active.shutdown()
    hb.reset_all_clients()
    fake.close()
    conn.close()
    pool.closeall()


def _plan_now(r: Run):
    """What a run of the mode HQPlayer is in would measure now."""
    c = HQPlayerClient(host="127.0.0.1", port=r.fake.port, timeout=2.0, ring=False)
    assert c.connect()
    try:
        st, modes = c.get_state(), c.get_modes()
        filters, shapers, rates = c.get_filters(), c.get_shapers(), c.get_rates()
    finally:
        c.disconnect()
    import hqp_library
    ep = hqp_library.endpoint_by_address("127.0.0.1", r.fake.port)
    mode = next(m["name"] for m in modes if m["index"] == st["mode"])
    return bench.plan(ep, kind=bench.mode_kind(mode), mode_name=mode, state=st, filters=filters,
                      shapers=shapers, rates=rates)


def test_a_run_measures_into_the_ledger_puts_hqplayer_back_and_returns_the_output(run):
    fake, mgr = run.fake, run.mgr
    fake.speeds[("sinc-L", "ASDM7EC", 11289600)] = 0.6          # too heavy here: drops out
    before = fake.settings()
    bench.start()
    assert _wait(lambda: mgr.held is not None, 5)
    held = dict(mgr.held)
    assert _wait(lambda: not bench.running(), 60)
    st = bench.state()
    assert st["outcome"] == "done", st["note"]
    # own setting first and last, the probe row, the gaps: eight points; the
    # 512+fs modulator refused at 256× and 128×, sinc-L dropped out
    assert run.q("""SELECT outcome::text, points_planned, points_measured, round(dropout_speed::numeric, 2)::float,
                           first_speed IS NOT NULL, last_speed IS NOT NULL, cuda::text, convolution,
                           own_mode, last_rate IS NOT NULL, note
                      FROM hqp_benchmark_runs""") == \
        [("done", 8, 6, 0.6, True, True, "full", False, None, True, "HQPlayer refused 2 combinations")]
    ledger = run.q("SELECT rate_hz, filter, shaper, result::text, sample_id IS NOT NULL "
                   "FROM hqp_benchmark_points ORDER BY id")
    assert len(ledger) == 8
    assert sorted(r[3] for r in ledger) == ["dropout"] + ["measured"] * 5 + ["refused"] * 2
    assert all(r[4] == (r[3] != "refused") for r in ledger)
    assert {(r[0], r[2]) for r in ledger if r[3] == "refused"} == \
        {(5644800, "ASDM7EC-light 512+fs"), (11289600, "ASDM7EC-light 512+fs")}
    # the first run probes the highest rate right after the owner's own setting
    assert ledger[0][:3] == ledger[-1][:3] == (11289600, "poly-sinc-gauss-long", "ASDM7EC")
    assert {r[0] for r in ledger[1:3]} == {11289600}
    # every sample came from the benchmark, played as asked, the source reported
    assert run.q("SELECT count(*), bool_and(source = 'benchmark'), bool_and(src_rate = 44100) "
                 "FROM hqp_dsp_samples") == [(6, True, True)]
    # HQPlayer is put back — the selection and the volume — after a run lowered it
    assert fake.settings() == before
    assert ("Volume", {"value": "-60.0"}) in fake.commands
    # the output came back to the queue: attached again, the queue mirrored
    assert _wait(lambda: mgr.held is None and isinstance(mgr.active, HqpBackend)
                 and len(fake.playlist) == 2, 10)
    signal = streaming_service.get_proxy().file_token(str(bench.signal_path(44100)))
    assert all(signal not in u for u in fake.playlist)
    assert held["by"] == "benchmark" and held["label"] == bench.LABEL
    # a second run only checks the owner's setting again: every point was
    # asked, the refused ones included
    assert [p.own for p in _plan_now(run)] == [True, True]


def test_while_held_nothing_else_drives_the_output(run, monkeypatch):
    fake, mgr = run.fake, run.mgr
    fake.jitter = 0.2                                          # never settles: points take the cap
    old = mgr.active
    bench.start()
    assert _wait(lambda: mgr.held is not None, 5)
    for attempt in (mgr.ensure_active, mgr.backend, lambda: mgr.activate("browser")):
        with pytest.raises(OutputHeld):
            attempt()
    with pytest.raises(OutputHeld):
        with mgr.output_change():
            pass
    # the backend a play intent took before the hold is detached: refused
    with pytest.raises(ConnectionError):
        old.pause()
    assert "Pause" not in [c for c, _ in fake.commands]
    assert "hold" in mgr.latest_status
    # busy, not broken: 409, never the 503 that sends the owner to the picker
    from routers import player as player_router
    monkeypatch.setattr(player_router, "manager", mgr)
    assert player_router._output_error(OutputHeld(mgr.held)).status_code == 409
    assert player_router._output_error(ConnectionError("detached")).status_code == 409
    bench.cancel()
    assert _wait(lambda: not bench.running(), 10)
    assert bench.state()["outcome"] == "cancelled"
    assert _wait(lambda: mgr.held is None and isinstance(mgr.active, HqpBackend), 10)
    assert "hold" not in mgr.latest_status
    assert player_router._output_error(ConnectionError("gone")).status_code == 503


def test_cancel_puts_hqplayer_back(run, monkeypatch):
    fake = run.fake
    fake.jitter = 0.2                                          # each point waits for the cap ...
    monkeypatch.setattr(bench, "SETTLE_CAP_S", 2.0)            # ... long enough to cancel in
    before = fake.settings()
    bench.start()
    assert _wait(lambda: run.point() >= 2, 10)
    assert bench.cancel()
    assert _wait(lambda: not bench.running(), 10)
    assert bench.state()["outcome"] == "cancelled"
    assert run.q("SELECT outcome::text, finished_at IS NOT NULL FROM hqp_benchmark_runs") == \
        [("cancelled", True)]
    assert fake.settings() == before
    assert _wait(lambda: len(fake.playlist) == 2, 10)


def test_a_dropped_connection_lowers_hqplayer_again_before_anything_plays(run, monkeypatch):
    fake = run.fake
    fake.jitter = 0.2
    monkeypatch.setattr(bench, "SETTLE_CAP_S", 1.0)
    fake.boot_volume = -3.0             # a restart brings HQPlayer back at the owner's level
    before = fake.settings()
    bench.start()
    assert _wait(lambda: run.point() >= 3, 20)
    fake.restart()                      # what a trial-mode Embedded does every 30 minutes
    assert _wait(lambda: not bench.running(), 60)
    st = bench.state()
    assert st["outcome"] == "done", st["note"]
    after = [(c, a) for c, a in fake.commands[fake.restarted_at:] if c in ("Volume", "Play")]
    assert after[0] == ("Volume", {"value": "-60.0"})          # lowered before the next Play
    assert fake.settings() == before
    assert run.q("SELECT points_measured FROM hqp_benchmark_runs") == [(6,)]


def test_a_run_hqplayer_left_is_put_back_when_it_returns(run, monkeypatch):
    fake, mgr = run.fake, run.mgr
    fake.jitter = 0.2
    monkeypatch.setattr(bench, "SETTLE_CAP_S", 1.0)
    before = fake.settings()
    bench.start()
    assert _wait(lambda: run.point() >= 3, 20)
    fake.boot_volume = -3.0
    fake.close()                        # powered off in the middle of a point
    assert _wait(lambda: not bench.running(), 30)
    st = bench.state()
    assert st["outcome"] == "failed" and "answering" in st["note"], st
    # nobody could put HQPlayer back: the run stays open, its last selection noted
    assert run.q("SELECT finished_at IS NULL, last_rate IS NOT NULL FROM hqp_benchmark_runs") == \
        [(True, True)]
    assert fake.settings() != before
    fake.come_back()                    # no playlist, the owner's volume, the run's last selection

    def put_back():
        mgr.active.poke()               # past the poller's back-off: the next tick is now
        return run.q("SELECT outcome::text FROM hqp_benchmark_runs")[0][0] == "interrupted"
    # the poller sees it return and puts it back by that selection
    assert _wait(put_back, 30)
    assert fake.settings() == before
    note = run.q("SELECT note FROM hqp_benchmark_runs")[0][0]
    assert "put back when it answered again" in note and "volume" not in note.split("; ")[-1]


def _cut_short_run(r: Run, **kw):
    """A run row as a run cut short leaves it."""
    row = dict(hqp_endpoint_id=r.endpoint_id, hqp_engine="6.2.3", mode="SDM (DSD)", points_planned=8,
               pre_mode=2, pre_rate=2, pre_filter=0, pre_filter1x=0, pre_shaper=0, pre_volume=-3.0,
               mute_volume=-60.0)
    row.update(kw)
    cols = ", ".join(row)
    with r.conn.cursor() as cur:
        cur.execute(f"INSERT INTO hqp_benchmark_runs ({cols}) VALUES ({', '.join(['%s'] * len(row))}) "
                    "RETURNING id", list(row.values()))
        return cur.fetchone()[0]


def test_recover_puts_back_the_measured_modes_own_selection(run):
    fake = run.fake
    # a "Measure PCM" run from SDM, cut short: HQPlayer in PCM on a test
    # point, lowered, its signal queued
    _cut_short_run(run, mode="PCM", own_mode=1, own_rate=2, own_filter=1, own_filter1x=1, own_shaper=1,
              last_rate=1, last_filter=0, last_filter1x=0, last_shaper=0)
    signal = bench.ensure_signal(44100)
    proxy = streaming_service.get_proxy()
    with fake._lock:
        fake.mode, fake.sel[1], fake.volume = 1, [1, 0, 0, 0], -60.0
        fake.playlist = [proxy.file_url(proxy.file_token(str(signal)), host="127.0.0.1")]
    bench.recover(run.endpoint_id)
    # PCM's own selection back, then SDM and its selection, the volume, no signal
    assert fake.settings() == (2, (2, 1, 1, 1), (2, 0, 0, 0), -3.0)
    assert fake.playlist == []
    assert run.q("SELECT outcome::text FROM hqp_benchmark_runs") == [("interrupted",)]


def test_recover_leaves_an_hqplayer_the_owner_changed_since(run):
    fake = run.fake
    _cut_short_run(run, last_rate=1, last_filter=1, last_filter1x=1, last_shaper=1)
    with fake._lock:
        fake.volume, fake.sel[2] = -10.0, [2, 1, 1, 2]
        fake.playlist = ["file:///E:/Music/A/01.flac"]
    bench.recover(run.endpoint_id)
    assert fake.settings() == (2, (2, 1, 1, 1), (2, 1, 1, 2), -10.0)
    assert fake.playlist == ["file:///E:/Music/A/01.flac"]
    assert "left as it is" in run.q("SELECT note FROM hqp_benchmark_runs")[0][0]


def test_a_hold_that_cannot_begin_leaves_nothing_held(run, monkeypatch):
    mgr = run.mgr
    real = mgr.activate

    def failing(output_type, **kw):
        if output_type is None and kw.get("by_hold"):
            raise RuntimeError("the database is gone")
        return real(output_type, **kw)
    monkeypatch.setattr(mgr, "activate", failing)
    bench.start()
    assert _wait(lambda: not bench.running(), 10)
    st = bench.state()
    assert st["outcome"] == "failed" and "database is gone" in st["note"]
    assert mgr.held is None
    assert mgr.ensure_active() is not None


def test_a_fixed_volume_needs_the_owners_word(run):
    fake = run.fake
    fake.volume_range = '<VolumeRange min="-60" max="0" enabled="0"/>'
    with pytest.raises(bench.BenchmarkRefused, match="amplifier"):
        bench.start()
    commands = len(fake.commands)
    bench.start(fixed_volume_ok=True)
    assert _wait(lambda: not bench.running(), 60)
    assert bench.state()["outcome"] == "done", bench.state()["note"]
    assert [a for c, a in fake.commands[commands:] if c == "Volume"] == []
    assert run.q("SELECT mute_volume FROM hqp_benchmark_runs") == [(None,)]


def test_the_app_stopping_cancels_a_run_and_waits_for_it_to_put_hqplayer_back(run, monkeypatch):
    fake = run.fake
    fake.jitter = 0.2
    monkeypatch.setattr(bench, "SETTLE_CAP_S", 2.0)
    before = fake.settings()
    bench.start()
    assert _wait(lambda: run.point() >= 2, 10)
    bench.shutdown(10.0)
    assert not bench.running()
    assert bench.state()["outcome"] == "cancelled"
    assert fake.settings() == before


def test_an_hqplayer_forgotten_mid_run_ends_it(run, monkeypatch):
    fake = run.fake
    fake.jitter = 0.2
    monkeypatch.setattr(bench, "SETTLE_CAP_S", 1.0)
    bench.start()
    assert _wait(lambda: run.point() >= 2, 10)
    run.q("DELETE FROM hqp_endpoints RETURNING id")
    assert _wait(lambda: not bench.running(), 30)
    st = bench.state()
    assert st["outcome"] == "failed" and "forgotten" in st["note"], st
    assert run.q("SELECT count(*) FROM hqp_benchmark_runs") == [(0,)]
