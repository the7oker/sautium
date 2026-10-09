"""The HQPlayer benchmark (backend/playback/hqp_benchmark.py).

The grid is built from the lists a real HQPlayer Desktop 6 answered
(tests/fixtures/hqp_lists, names and indices only); the settling rule runs
on synthetic traces. Whole runs — their rows and the ledger a later run
skips by, the probe row, a combination HQPlayer stops at Play, cancel, a
connection HQPlayer drops under the run (and a stop as it goes away, which
is no dropout), a run cut short and put back when HQPlayer returns, the hold
of the output, the app stopping — run against the fake control port of
test_hqp_backend taught the DSP commands, a real PostgreSQL (a throwaway
database built from the migrations, the pool pointed at it) and real test
signals from ffmpeg. HQPlayer's silence while it builds a setting is waited
out — a build past the wait is a setting this host does not start in time,
not asked again. Never a real HQPlayer: a transport command on the owner's
output is an audible action, not a test.
"""

import json
import random
import sys
import time
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
from playback import hqp_load  # noqa: E402
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

def _grid(name, *, covered=frozenset(), keeps_up=None, seed=3, **kw):
    d, cur = _lists(name)
    args = dict(filters=d["filters"], shapers=d["shapers"], rates=d["rates"], current=cur,
                srcs=SRC44, covered=set(covered), keeps_up=keeps_up or {},
                rng=random.Random(seed))
    args.update(kw)
    return d, cur, bench.build_grid("sdm", **args)


def test_sdm_grid_from_a_real_hqplayers_lists():
    d, cur, grid = _grid("desktop_sdm.json")
    pts = grid.points
    # the owner's own setting first and last; every modulator's ladder at
    # DSD256 (the owner's rate here) with every filter at the owner's rate,
    # then the step up to 512× — less the two points the own setting is
    starts = len(d["shapers"]) - 1 + len(d["filters"]) - 1
    assert len(pts) == 1 + starts + len(d["shapers"]) + 1
    assert pts[0].own and pts[-1].own and pts[0] == pts[-1]
    assert [p for p in pts[1:-1] if p.own] == []
    keys = [p.key for p in pts[1:-1]]
    assert len(keys) == len(set(keys))
    assert {p.rate_hz for p in pts[1:1 + starts]} == {11289600}
    assert {(p.rate_hz, p.step) for p in pts[1 + starts:-1]} == {(22579200, 1)}
    # every ladder keeps its rungs under the start for a host that needs them
    assert {tuple(p.step for p in r) for r in grid.rungs.values()} == {(-2, -1, 0, 1)}
    # the owner's setting is the start of its modulator's ladder, its 1x
    # filter what runs for a 44.1 kHz source
    assert pts[0].ladder is not None and pts[0].step == 0
    names = {f["index"]: f["name"] for f in d["filters"]}
    assert pts[0].key[1] == names[cur["filter1x"]]
    # a seeded shuffle is the same shuffle
    assert _grid("desktop_sdm.json")[2].points == pts


def test_a_later_run_fills_only_the_gaps():
    _, _, first = _grid("desktop_sdm.json", seed=1)
    covered = {p.key for p in first.points[1:-11]}   # all but ten points asked since
    _, _, later = _grid("desktop_sdm.json", seed=1, covered=covered | {first.points[0].key})
    # the ten gaps, and the owner's setting first and last all the same
    assert len(later.points) == 12
    assert later.points[0].own and later.points[-1].own
    assert {p.key for p in later.points[1:-1]} == {p.key for p in first.points[-11:-1]}


def test_a_ladder_climbs_from_dsd256_while_its_setting_keeps_up():
    def key(rate, shaper):
        return (rate, "poly-sinc-gauss-xla", shaper, 44100, 2)
    known = {key(11289600, "ASDM7EC"): False,       # tight at DSD256: down, not up
             key(11289600, "ASDM5"): True,          # keeps up: up, not down
             key(22579200, "DSD7"): True,           # keeps up at 512× (listened): so does 256×
             key(5644800, "ASDM7EC-light"): False}  # too slow at 128×: nothing above, 64× next
    _, _, grid = _grid("desktop_sdm.json", covered=set(known), keeps_up=known, seed=6)
    asked = {(p.rate_hz, p.key[2]) for p in grid.points}
    assert (5644800, "ASDM7EC") in asked and (22579200, "ASDM7EC") not in asked
    assert (2822400, "ASDM7EC") not in asked                  # one rung down at a time
    assert (22579200, "ASDM5") in asked
    assert not {(5644800, "ASDM5"), (2822400, "ASDM5")} & asked
    assert not {(11289600, "DSD7"), (22579200, "DSD7")} & asked
    assert not {(11289600, "ASDM7EC-light"), (22579200, "ASDM7EC-light")} & asked
    assert (2822400, "ASDM7EC-light") in asked
    # the starts, then each step by its distance from the start, down first
    steps = [p.step for p in grid.points[1:-1] if p.ladder is not None]
    assert steps == sorted(steps, key=lambda n: (abs(n), n > 0))


def test_pcm_ladders_climb_every_filter_from_8x_and_an_automatic_rate_is_keyed_by_the_ask():
    d, cur = _lists("desktop_sdm.json")            # PCM lists hold the same filter names
    shapers = [{"index": 0, "name": "none", "value": 0}, {"index": 1, "name": "LNS15", "value": 1}]
    rates = [{"index": 0, "rate": 0}, {"index": 1, "rate": 352800}, {"index": 2, "rate": 384000},
             {"index": 3, "rate": 705600}, {"index": 4, "rate": 768000}]
    current = {"rate": 0, "filterNx": cur["filterNx"], "filter1x": cur["filter1x"], "shaper": 1}
    srcs = {**SRC44, "pcm96": {"rate": 96000, "channels": 2}}
    pts = bench.build_grid("pcm", filters=d["filters"], shapers=shapers, rates=rates, current=current,
                           srcs=srcs, covered=set(), keeps_up={}, rng=random.Random(2)).points
    # every filter on each source a ladder over its family from 8× — 352.8
    # and 384 kHz — and a step up; the owner's own setting first and last
    assert len(pts) == len(d["filters"]) * 2 * 2 + 2
    assert {p.shaper for p in pts} == {1}
    assert {(p.rate_hz, p.step) for p in pts[1:-1] if p.source == "pcm44"} == \
        {(352800, 0), (705600, 1)}
    assert {(p.rate_hz, p.step) for p in pts[1:-1] if p.source == "pcm96"} == \
        {(384000, 0), (768000, 1)}
    # an automatic rate plays another rate than the one asked: the owner's
    # point is keyed by the ask (0), which a run's ledger row covers
    assert pts[0].own and pts[0].key[0] == 0 and pts[0].ladder is None
    again = bench.build_grid("pcm", filters=d["filters"], shapers=shapers, rates=rates,
                             current=current, srcs=srcs, covered={p.key for p in pts},
                             keeps_up={}, rng=random.Random(2)).points
    assert [p.own for p in again] == [True, True]


def test_a_pcm_source_is_never_asked_below_its_own_rate():
    # HQPlayer does not downsample PCM: "clHQPlayerEngine::Execute(): lInRate
    # > lOutRate", then it stops (Embedded 6.2.3, 96 kHz at 48 kHz, 2026-10-03)
    d, cur = _lists("desktop_sdm.json")
    shapers = [{"index": 0, "name": "none", "value": 0}, {"index": 1, "name": "TPDF", "value": 1}]
    rates = [{"index": 0, "rate": 0}] + [{"index": i + 1, "rate": r} for i, r in enumerate(
        (44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000))]
    # the owner at 48 kHz: another family than 44.1 kHz, under 96 kHz
    current = {"rate": 2, "filterNx": cur["filterNx"], "filter1x": cur["filter1x"], "shaper": 1}
    srcs = {**SRC44, "pcm96": {"rate": 96000, "channels": 2}}
    plan = bench.build_grid("pcm", filters=d["filters"], shapers=shapers, rates=rates,
                            current=current, srcs=srcs, covered=set(), keeps_up={},
                            rng=random.Random(3))

    def rungs(src):
        return {p.rate_hz for lid, r in plan.rungs.items() if lid[3] == src for p in r}
    assert rungs("pcm96") == {96000, 192000, 384000}
    assert rungs("pcm44") == {44100, 88200, 176400, 352800}       # its 1× stays
    assert not any(p.source == "pcm96" and p.rate_hz < 96000 for p in plan.points)
    assert any(p.source == "pcm44" and p.rate_hz == 48000 for p in plan.points)


def test_a_source_plays_at_its_own_family_and_the_owners_rate():
    assert bench.same_family(11289600, 44100) and bench.same_family(768000, 96000)
    assert bench.same_family(352800, 2822400)              # a DSD source down to PCM
    assert not bench.same_family(12288000, 44100) and not bench.same_family(705600, 96000)
    assert not bench.same_family(12000000, 44100) and not bench.same_family(0, 44100)
    rates = [{"index": 0, "rate": 0}] + [{"index": i + 1, "rate": r} for i, r in enumerate(
        (2822400, 3072000, 5644800, 6144000, 11289600, 12288000, 45158400, 49152000))]
    # the owner on 48k × 128 — another family than the 44.1 kHz signal's
    current = {"rate": 4, "filterNx": 53, "filter1x": 49, "shaper": 21}
    _, _, grid = _grid("desktop_sdm.json", rates=rates, current=current, seed=5)
    modulator_rows = {p.rate_hz for p in grid.points if p.filter == 53 and p.filter1x == 49}
    assert modulator_rows == {11289600, 45158400, 6144000}
    # the ladders keep to the family — DSD256 up to 44.1k × 1024; the
    # owner's 48k × 128 is a point of its own, and no other 48k rate is asked
    assert {p.rate_hz for r in grid.rungs.values() for p in r} == \
        {2822400, 5644800, 11289600, 45158400}
    assert not any(p.rate_hz in (3072000, 12288000, 49152000) for p in grid.points)


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
    # a rebuild: HQPlayer reports on, the position does not move
    trace += [(4, _st(position=3.0, speed=1.9)), (5, _st(position=3.0, speed=1.8))]
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
        # refreshed (the output buffer drains), the position not moving
        trace += [(t, _st(position=8.0, speed=speed, out=1.7 - 0.1 * t)) for t in range(9, 12)]
        _, v = _feed(bench.Settle(0.0), trace)
        assert v.get("dropout", False) is dropout, (speed, v)
        if not dropout:
            assert "froze" in v["failed"]


def test_a_setting_that_never_starts_fails_at_the_deadline():
    trace = [(t, _st(position=0.0)) for t in range(1, int(bench.START_DEADLINE_S) + 3)]
    t, v = _feed(bench.Settle(0.0), trace)
    assert "failed" in v and t == int(bench.START_DEADLINE_S) + 1


def test_a_build_hqplayer_answers_all_through_is_not_held_against_the_start():
    # Embedded on a Pi 5: playing, position 0, speed 0, nothing out — for
    # longer than a start may take — then it plays
    building = [(t, _st(position=0.0, speed=0.0, out=0.0)) for t in range(1, 80)]
    trace = building + [(80 + t, _st(position=float(t), speed=2.0)) for t in range(1, 30)]
    t, v = _feed(bench.Settle(0.0), trace)
    assert "speed" in v and v["settled"], v
    assert v["init_s"] >= 80


def test_a_build_past_the_limit_never_started():
    limit = int(bench.BUILD_LIMIT_S)
    trace = [(t, _st(position=0.0, speed=0.0, out=0.0)) for t in range(1, limit + 3)]
    t, v = _feed(bench.Settle(0.0), trace)
    assert "unstarted" in v and t == limit + 1


def test_a_point_without_a_verdict_fails_at_its_deadline():
    # moving and stalling over and over before the readings, at a speed that
    # is no dropout: no verdict ever comes — the point gives up
    trace, pos = [], 0.0
    for t in range(1, 200):
        pos += 1.0 if t % 4 in (1, 2) else 0.0
        trace.append((t, _st(position=pos, speed=1.2 + 0.001 * t)))
    t, v = _feed(bench.Settle(0.0), trace)
    assert "failed" in v and "no verdict" in v["failed"]
    assert t == int(bench.POINT_DEADLINE_S) + 1


def test_a_silent_build_is_held_off_the_start_once_not_twice():
    # Desktop silent 100 s building, then answering that it plays with
    # nothing out yet: the silence is the build's (paused), and the first
    # answer after it must not count the same gap again from the tick before
    s = bench.Settle(0.0)
    assert s.feed(1.0, _st(position=0.0, speed=0.0, out=0.0)) is None
    s.paused(100.0)                                       # _outlast, after the silence
    assert s.feed(102.0, _st(position=0.0, speed=0.0, out=0.0)) is None
    # it says it plays and does not move: the start deadline runs from here
    t, v = _feed(s, [(102.0 + i, _st(position=0.0, speed=1.0, out=0.5)) for i in range(1, 400)])
    assert "failed" in v and "did not move" in v["failed"], v
    assert t <= 102.0 + bench.START_DEADLINE_S + 2         # not the silence's length later


def test_a_signal_played_out_faster_than_real_time_is_read_not_failed():
    # HQPlayer's ALSA null device keeps no time: a Pi 5 played the 120 s
    # signal out at the DSP's speed, ~24 audio seconds a second, and
    # stopped (2026-10-04); the readings it gave on the way are the point
    trace = [(1, _st(position=0.0, speed=0.0, out=0.0))]
    trace += [(1 + t, _st(position=24.0 * t, speed=24.0 + 0.1 * t)) for t in range(1, 6)]
    trace += [(7, _st(state=PlaybackState.STOPPED, position=0.0, speed=0.0, out=0.0))]
    t, v = _feed(bench.Settle(0.0), trace)
    assert "speed" in v and not v["dropout"] and v["settled"] is False, v
    assert v["readings"] == pytest.approx([24.2, 24.3, 24.4, 24.5])   # from five audio seconds on
    assert v["speed"] == pytest.approx(bench._p10(v["readings"]))
    # out within one tick (`none`, ~100×): only the first jump tells it
    once = [(1, _st(position=0.0, speed=0.0, out=0.0)), (2, _st(position=100.0, speed=100.0)),
            (3, _st(state=PlaybackState.STOPPED, position=0.0, speed=0.0, out=0.0))]
    _, v = _feed(bench.Settle(0.0), once)
    assert "keeps no time" in v["failed"] and v["outran"], v
    # a stop at real time stays what it was
    real = [(t, _st(position=float(t), speed=2.5)) for t in range(1, 8)]
    real += [(8, _st(state=PlaybackState.STOPPED, position=0.0, speed=0.0))]
    _, v = _feed(bench.Settle(0.0), real)
    assert "failed" in v and "not the DSP" in v["failed"]


def test_a_state_hqplayer_does_not_name_is_no_stop():
    # Embedded said 5 while it started a play: once the point moved, no verdict
    trace = [(t, _st(position=float(t), speed=2.0 + 0.001 * t)) for t in range(1, 4)]
    trace += [(4, _st(state=PlaybackState(5), position=3.5, speed=2.0))]
    trace += [(t, _st(position=float(t - 1), speed=2.0 + 0.001 * t)) for t in range(5, 30)]
    _, v = _feed(bench.Settle(0.0), trace)
    assert "speed" in v and not v["dropout"], v


def test_a_silence_mid_play_is_read_afresh_not_closed_on_one_reading():
    # the control port silent a minute while it played: the first answer
    # after it must not close the point "unsettled" from one reading — the
    # drained output it shows is the dropout
    s = bench.Settle(0.0)
    t, v = _feed(s, [(t, _st(position=float(t), speed=1.5 + 0.01 * t)) for t in range(1, 9)])
    assert v is None
    s.paused(60.0)
    after = [(70 + t, _st(position=8.0 + t, speed=0.8, out=0.02, inp=0.9 + 0.001 * t))
             for t in range(1, 10)]
    _, v = _feed(s, after)
    assert v["dropout"] and v["speed"] == pytest.approx(0.8), v


def test_a_refused_start_leaves_the_rungs_above_to_send_the_ladder_down():
    def rung(hz, step):
        return bench.Point(0, hz, 0, 0, 0, "pcm44", str(hz), (hz, "f", "s", 44100, 2),
                           ladder=("f", "f", "s", "pcm44"), step=step)
    rungs = [rung(2822400, -2), rung(5644800, -1), rung(11289600, 0), rung(22579200, 1)]
    known = {rungs[3].key: False}             # DSD512 too slow, DSD256 refused: said nothing
    passed = {r.key for r in rungs[2:]}
    assert bench._descend(rungs, known, passed) is rungs[1]
    # one at or under the start that keeps up: nothing to go down for
    assert bench._descend(rungs, {**known, rungs[1].key: True}, passed) is None


def test_a_status_refreshed_per_output_block_is_read_not_taken_for_frozen():
    # HQPlayer Embedded on a Pi 5 (engine 6.2.3, 2026-10-03), poly-sinc-ext2-
    # hires-ip at 384 kHz from a 96 kHz source, read once a second: building
    # with nothing out, then the whole <Status/> refreshed every ~3 s — read
    # as a position frozen for two ticks, it "froze at 9.27×"
    building = [(t, _st(position=0.0, speed=0.0, out=0.0, inp=0.0003)) for t in range(1, 8)]
    refreshed = [(8, 2.560, 9.56, 0.61, 0.1429), (9, 2.560, 9.56, 0.61, 0.1429),
                 (10, 3.584, 7.55, 0.67, 0.1429), (11, 3.584, 7.55, 0.67, 0.1429),
                 (12, 3.584, 7.55, 0.67, 0.1429), (13, 4.523, 7.94, 0.20, 0.1579),
                 (14, 5.547, 10.96, 0.36, 0.1579), (15, 5.547, 10.96, 0.36, 0.1579),
                 (16, 5.547, 10.96, 0.36, 0.1579), (17, 6.571, 8.81, 0.14, 0.1729),
                 (18, 6.571, 8.81, 0.14, 0.1729), (19, 6.571, 8.81, 0.14, 0.1729),
                 (20, 7.509, 8.14, 0.61, 0.1879), (21, 7.509, 8.14, 0.61, 0.1879),
                 (22, 7.509, 8.14, 0.61, 0.1879), (23, 8.533, 8.57, 0.87, 0.1879),
                 (24, 8.533, 8.57, 0.87, 0.1879), (25, 8.533, 8.57, 0.87, 0.1879),
                 (26, 9.557, 8.17, 0.68, 0.2029), (27, 10.581, 9.27, 0.62, 0.2029)]
    trace = building + [(t, _st(position=p, speed=s, out=o, inp=i)) for t, p, s, o, i in refreshed]
    t, v = _feed(bench.Settle(0.0), trace)
    assert "speed" in v and not v["dropout"], v
    assert v["init_s"] == 8
    # one reading per refresh, from STABLE_AFTER_S after it moved
    assert v["readings"] == [7.94, 10.96, 8.81, 8.14, 8.57]
    assert v["settled"] is False and t == 8 + bench.SETTLE_CAP_S
    assert v["speed"] == pytest.approx(bench._p10(v["readings"]))


# -- the fake HQPlayer ----------------------------------------------------------------

class DspFake(FakeHqp):
    """The fake control port taught HQPlayer's DSP: per-mode lists and one
    selection per mode (as HQPlayer keeps them), State, the Set* commands,
    Volume/VolumeRange, and a Status that reports the source and a speed per
    setting. A modulator named "… 512+fs" is refused below 512×. A stopped
    HQPlayer plays the slot a SelectTrack gives it, as Desktop 5 and 6 do.
    Starting a setting in `builds` answers, then the port answers nothing on
    any connection until the build is done — and then carries out what it
    was sent meanwhile. A restart keeps the selections (HQPlayer saves them) and
    brings the volume back to `boot_volume` when one is set."""

    MODES = [("[source]", -1), ("PCM", 0), ("SDM (DSD)", 1)]
    RATES = {1: [0, 352800, 705600], 2: [0, 5644800, 11289600]}
    FILTERS = ["poly-sinc-gauss-long", "sinc-L"]
    SHAPERS = {1: ["none", "LNS15"], 2: ["ASDM7EC", "ASDM7EC-super", "ASDM7EC-light 512+fs"]}
    MINE = {"Status", "State", "GetModes", "GetRates", "GetFilters", "GetShapers", "SetMode",
            "SetRate", "SetFilter", "SetShaping", "Volume", "VolumeRange", "Play", "SelectTrack"}

    def __init__(self):
        super().__init__()
        self.mode = 2
        self.sel = {1: [2, 1, 1, 1], 2: [2, 0, 0, 0]}   # [rate, filterNx, filter1x, shaper]
        self.volume = -3.0
        self.boot_volume = None
        self.boot_mode = None            # the mode it saved, which a restart brings back
        self.speeds = {}             # (filter, shaper, rate Hz) → speed; 2.0 otherwise
        self.jitter = 0.0
        self.volume_range = '<VolumeRange min="-60" max="0" enabled="1" adaptive="0"/>'
        self._polls = 0
        # (running filter, output rate) it takes a Play for and stops at once:
        # "Requested filter not possible with this rate combination …, stop"
        self.unplayable = set()
        # (filter, shaper, rate) whose play it stops 10 s in and then drops
        # every connection — a trial-mode Embedded's half hour running out
        self.vanish_at = None
        self._vanish = False
        # ... or whose play the box loses to a reset: no FIN, no RST — the
        # read goes unanswered, and it comes back a second later, stopped
        self.resets_at = None
        # (running filter, output rate) → seconds HQPlayer builds it after a
        # Play, its control port silent (FakeHqp.silence) — or, `starts`,
        # answering all through: playing, position 0, speed 0, nothing out
        self.builds = {}
        self.starts = {}
        self._starting_until = 0.0
        # (running filter, output rate) from whose Play on it starts nothing
        # at all — an output engine that no longer comes up
        self.sticks_at = None
        self._stuck = False
        # ... or stops every Play at once — an output that went away
        self.dies_at = None
        self._dead = False
        # (running filter, output rate) → audio seconds a Status poll: an
        # output that keeps no time (the ALSA null device) plays it out fast
        self.races = {}

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
            src = 44100
            running = self.FILTERS[x1 if src in (44100, 48000) else nx]
            key = (running, self.SHAPERS[self.mode][sh], self._played_rate(src))
            if cmd in ("Play", "SelectTrack"):
                if cmd == "SelectTrack":
                    self.track, self.position = int(attrs["index"]), 0.0
                    if self.state == int(PlaybackState.PLAYING):
                        return '<SelectTrack result="OK"/>'
                self._dead = self._dead or (running, key[2]) == self.dies_at
                if self.playlist and not self._dead and (running, key[2]) not in self.unplayable:
                    if self.state != int(PlaybackState.PLAYING):
                        self.position = 0.0
                        if (running, key[2]) in self.builds:
                            self.silence(self.builds[(running, key[2])])
                        self._stuck = self._stuck or (running, key[2]) == self.sticks_at
                        self._starting_until = (
                            float("inf") if self._stuck
                            else time.monotonic() + self.starts.get((running, key[2]), 0.0))
                    self.state = int(PlaybackState.PLAYING)
                    self.track = self.track or 1
                return f'<{cmd} result="OK"/>'
            if cmd == "Status":
                if self._vanish:
                    self._vanish = False
                    return None
                playing = self.state == int(PlaybackState.PLAYING)
                starting = playing and time.monotonic() < self._starting_until
                if playing and not starting:
                    self.position += self.races.get((running, key[2]), 1.0)
                    if (running, key[2]) in self.races and self.position >= 120.0:
                        # the signal ran out: on to the next entry, else stop
                        if self.track < len(self.playlist):
                            self.track, self.position = self.track + 1, 0.0
                        else:
                            self.state, self.position = int(PlaybackState.STOPPED), 0.0
                            playing = False
                if playing and key == self.vanish_at and self.position >= 10:
                    self.vanish_at, self._vanish = None, True
                    self.state, playing = int(PlaybackState.STOPPED), False
                if playing and key == self.resets_at and self.position >= 10:
                    # this read goes unanswered (no FIN, no RST — its answer
                    # comes past the client's wait), and the box is back a
                    # second later, stopped
                    self.resets_at = None
                    self.state, self.position, self.track = int(PlaybackState.STOPPED), 0.0, 0
                    playing = False
                    self.silence(1.0)
                    time.sleep(0.5)
                speed = 0.0
                if playing and not starting:
                    self._polls += 1
                    speed = self.speeds.get(key, 2.0) + (self.jitter if self._polls % 2 else -self.jitter)
                out = (0.0 if starting else 0.99 if speed >= 1.0 or not playing
                       else max(0.0, 0.99 - 0.3 * self.position))
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
        with self._lock:
            if self.boot_volume is not None:
                self.volume = self.boot_volume
            if self.boot_mode is not None:
                self.mode = self.boot_mode

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


def test_a_silent_hqplayer_is_told_from_one_that_closed():
    fake = DspFake()
    try:
        c = HQPlayerClient(host="127.0.0.1", port=fake.port, timeout=0.3, ring=False)
        assert c.connect()
        fake.silence(0.8)                                # building: nothing answers
        assert c.get_status() is None
        assert c.timed_out and not c.is_connected()
        assert c.refusal() == "Status: no answer within 0.3 s"
        fake.mute = True                                 # a connection it closes at once
        assert c.connect()
        assert c.get_status() is None and not c.timed_out
    finally:
        fake.close()


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
def library(tmp_path):
    """The album the owner has queued when a run starts — files of the test's
    own, not a path of any machine's library."""
    album = tmp_path / "library" / "A"
    album.mkdir(parents=True)
    for name in ("01.flac", "02.flac"):
        (album / name).touch()
    return album


@pytest.fixture
def run(dsn, signals, library, monkeypatch):
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
    # Its persist timer would outlive this scratch pool, and db_pool then opens
    # the configured database — the node's own player.queue (2026-10-09).
    monkeypatch.setattr(mgr, "_schedule_persist", lambda: None)
    mgr.queue.replace([_item((library / "01.flac").as_posix(), 1),
                       _item((library / "02.flac").as_posix(), 2)])
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
                      shapers=shapers, rates=rates).points


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
    # own setting first and last, the ladders' starts at DSD256 (the top
    # here) and the filters at the owner's rate: five points — the 512+fs
    # modulator refused, sinc-L dropped out, nothing under DSD256 asked
    # where DSD256 keeps up
    assert run.q("""SELECT outcome::text, points_planned, points_measured, round(dropout_speed::numeric, 2)::float,
                           first_speed IS NOT NULL, last_speed IS NOT NULL, cuda::text, convolution,
                           own_mode, last_rate IS NOT NULL, note
                      FROM hqp_benchmark_runs""") == \
        [("done", 5, 4, 0.6, True, True, "full", False, None, True, "HQPlayer refused 1 combination")]
    ledger = run.q("SELECT rate_hz, filter, shaper, result::text, sample_id IS NOT NULL "
                   "FROM hqp_benchmark_points ORDER BY id")
    assert len(ledger) == 5
    assert sorted(r[3] for r in ledger) == ["dropout"] + ["measured"] * 3 + ["refused"]
    assert all(r[4] == (r[3] != "refused") for r in ledger)
    assert {(r[0], r[2]) for r in ledger if r[3] == "refused"} == \
        {(11289600, "ASDM7EC-light 512+fs")}
    assert ledger[0][:3] == ledger[-1][:3] == (11289600, "poly-sinc-gauss-long", "ASDM7EC")
    assert {r[0] for r in ledger} == {11289600}
    # every sample came from the benchmark, played as asked, the source reported
    assert run.q("SELECT count(*), bool_and(source = 'benchmark'), bool_and(src_rate = 44100) "
                 "FROM hqp_dsp_samples") == [(4, True, True)]
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


def test_a_combination_hqplayer_stops_at_play_is_refused_in_seconds(run):
    fake = run.fake
    fake.unplayable.add(("sinc-L", 11289600))       # "Requested filter not possible …, stop"
    before = fake.settings()
    bench.start()
    assert _wait(lambda: not bench.running(), 60)
    st = bench.state()
    assert st["outcome"] == "done", st["note"]
    assert run.q("SELECT result::text, note FROM hqp_benchmark_points WHERE filter = 'sinc-L'") == \
        [("refused", "HQPlayer stopped it again without playing — it does not run this combination "
                     "(its log says why)")]
    assert run.q("SELECT note FROM hqp_benchmark_runs") == [("HQPlayer refused 2 combinations",)]
    assert fake.settings() == before
    # asked and answered: a later run does not ask it again
    assert not any(p.key[1] == "sinc-L" for p in _plan_now(run))


def test_a_ladder_steps_up_while_it_keeps_up_and_down_while_its_start_does_not(run):
    fake = run.fake
    fake.RATES = {1: DspFake.RATES[1], 2: [0, 2822400, 5644800, 11289600, 22579200]}
    fake.sel[2][0] = 3                                       # the owner at DSD256
    fake.speeds[("poly-sinc-gauss-long", "ASDM7EC-super", 11289600)] = 1.1   # tight there
    bench.start()
    assert _wait(lambda: not bench.running(), 60)
    st = bench.state()
    assert st["outcome"] == "done", st["note"]
    asked = {(r[0], r[1]): r[2] for r in run.q(
        "SELECT rate_hz, shaper, result::text FROM hqp_benchmark_points "
        "WHERE filter = 'poly-sinc-gauss-long'")}
    # the owner's modulator keeps up at DSD256: a step up, none down
    assert (22579200, "ASDM7EC") in asked and (5644800, "ASDM7EC") not in asked
    # tight at DSD256: nothing above it — down a rung, which keeps up
    assert (22579200, "ASDM7EC-super") not in asked
    assert asked[(5644800, "ASDM7EC-super")] in ("measured", "unsettled")
    assert (2822400, "ASDM7EC-super") not in asked
    # a refusal stops no climb: the 512+fs modulator plays from 512×
    assert asked[(11289600, "ASDM7EC-light 512+fs")] == "refused"
    assert asked[(22579200, "ASDM7EC-light 512+fs")] in ("measured", "unsettled")
    assert "1 rate not measured — the rate next to it on its ladder answers for it" in st["note"]


def test_a_start_that_does_not_keep_up_steps_down_at_once_and_holds_the_filters_back(run):
    fake = run.fake
    fake.RATES = {1: DspFake.RATES[1], 2: [0, 2822400, 5644800, 11289600]}
    fake.sel[2][0] = 3                                       # the owner at DSD256 ...
    fake.speeds[("poly-sinc-gauss-long", "ASDM7EC", 11289600)] = 0.6   # ... which drops out
    bench.start()
    assert _wait(lambda: not bench.running(), 60)
    st = bench.state()
    assert st["outcome"] == "done", st["note"]
    ledger = run.q("SELECT rate_hz, filter, shaper, result::text FROM hqp_benchmark_points "
                   "ORDER BY id")
    # the owner's setting first, the rung under it right after — what this
    # host does run comes before anything else
    assert ledger[0] == (11289600, "poly-sinc-gauss-long", "ASDM7EC", "dropout")
    assert ledger[1][:3] == (5644800, "poly-sinc-gauss-long", "ASDM7EC")
    assert ledger[1][3] in ("measured", "unsettled")
    assert not [r for r in ledger if r[0] == 2822400]        # 128× keeps up: no further
    # every filter with the owner's modulator at the owner's rate would
    # measure the modulator: left for a setting that keeps up
    assert not [r for r in ledger if r[1] == "sinc-L"]
    assert "1 filter left for later — your own setting does not keep up here" in st["note"]


def test_a_stop_as_hqplayer_goes_away_is_no_dropout(run, monkeypatch):
    fake = run.fake
    fake.jitter = 0.2                                 # the points run to the cap ...
    monkeypatch.setattr(bench, "SETTLE_CAP_S", 0.5)  # ... past position 10
    key = ("poly-sinc-gauss-long", "ASDM7EC-super", 11289600)
    fake.speeds[key] = 1.2                            # slow enough that a stop would read as one
    fake.vanish_at = key                              # its play stopped, every connection dropped
    bench.start()
    assert _wait(lambda: not bench.running(), 60)
    assert bench.state()["outcome"] == "done", bench.state()["note"]
    assert fake.vanish_at is None
    assert run.q("SELECT count(*) FROM hqp_dsp_samples WHERE dropout") == [(0,)]
    assert run.q("""SELECT result::text FROM hqp_benchmark_points
                     WHERE shaper = 'ASDM7EC-super' AND rate_hz = 11289600""") == [("unsettled",)]


def test_an_hqplayer_reset_mid_point_has_the_point_made_again(run, monkeypatch):
    # its stop after the silence is no dropout at the speed it showed last
    fake = run.fake
    fake.jitter = 0.2                                 # the points run to the cap ...
    monkeypatch.setattr(bench, "SETTLE_CAP_S", 0.5)  # ... past position 10
    monkeypatch.setattr(bench, "ANSWER_WAIT_S", 0.2)
    key = ("poly-sinc-gauss-long", "ASDM7EC-super", 11289600)
    fake.speeds[key] = 1.2                            # slow enough that a stop would read as one
    fake.resets_at = key
    bench.start()
    assert _wait(lambda: not bench.running(), 60)
    assert bench.state()["outcome"] == "done", bench.state()["note"]
    assert fake.resets_at is None
    assert run.q("SELECT count(*) FROM hqp_dsp_samples WHERE dropout") == [(0,)]
    assert run.q("""SELECT result::text FROM hqp_benchmark_points
                     WHERE shaper = 'ASDM7EC-super' AND rate_hz = 11289600""") == [("unsettled",)]


def _loads(fake) -> int:
    """How often the run loaded its sources into HQPlayer — once, unless it
    had to make what HQPlayer holds again (the queue's mirror is not it)."""
    tokens = bench._signal_tokens()
    return sum(1 for cmd, attrs in fake.commands
               if cmd == "PlaylistAdd" and attrs.get("clear") == "1"
               and any(t in attrs.get("uri", "") for t in tokens))


def test_hqplayers_silence_while_it_builds_a_setting_is_waited_out(run, monkeypatch):
    fake = run.fake
    monkeypatch.setattr(bench, "ANSWER_WAIT_S", 0.3)
    # longer than several waits and than the point's start deadline (2 s
    # here): the silence is the build, not the start, and HQPlayer is asked
    # again until it answers
    fake.builds[("sinc-L", 11289600)] = 2.6
    bench.start()
    assert _wait(lambda: not bench.running(), 60)
    assert bench.state()["outcome"] == "done", bench.state()["note"]
    # measured, the build counted into its start — nothing stopped it
    # mid-build, nothing was loaded again
    rows = run.q("""SELECT p.result::text, s.init_s FROM hqp_benchmark_points p
                      JOIN hqp_dsp_samples s ON s.id = p.sample_id
                     WHERE p.filter = 'sinc-L' AND p.rate_hz = 11289600""")
    assert len(rows) == 1 and rows[0][0] in ("measured", "unsettled") and rows[0][1] >= 2.5, rows
    assert _loads(fake) == 1


def test_a_build_hqplayer_answers_through_is_waited_out_and_measured(run):
    fake = run.fake
    fake.starts[("sinc-L", 11289600)] = 3.0      # past the start deadline (2 s here)
    bench.start()
    assert _wait(lambda: not bench.running(), 60)
    assert bench.state()["outcome"] == "done", bench.state()["note"]
    rows = run.q("""SELECT p.result::text, s.init_s FROM hqp_benchmark_points p
                      JOIN hqp_dsp_samples s ON s.id = p.sample_id
                     WHERE p.filter = 'sinc-L' AND p.rate_hz = 11289600""")
    assert len(rows) == 1 and rows[0][0] in ("measured", "unsettled") and rows[0][1] >= 3.0, rows


def test_an_hqplayer_silent_past_the_build_limit_is_unstarted_and_ends_the_run(run, monkeypatch):
    fake = run.fake
    monkeypatch.setattr(bench, "ANSWER_WAIT_S", 0.2)
    monkeypatch.setattr(bench, "BUILD_LIMIT_S", 0.6)
    fake.builds[("sinc-L", 11289600)] = 3.0
    bench.start()
    assert _wait(lambda: not bench.running(), 60)
    st = bench.state()
    assert st["outcome"] == "failed" and "stayed silent" in st["note"], st
    (note,), = run.q("""SELECT note FROM hqp_benchmark_points
                         WHERE filter = 'sinc-L' AND rate_hz = 11289600 AND result = 'unstarted'""")
    assert "does not get it going" in note, note
    assert run.q("SELECT count(*) FROM hqp_dsp_samples WHERE filter = 'sinc-L'") == [(0,)]
    ctx = {"endpoint_id": run.endpoint_id, "engine": "6.2.3", "cuda": "full",
           "mode": "SDM (DSD)", "matrix_profile": None, "convolution": False}
    assert (11289600, "sinc-L", "ASDM7EC", 44100, 2) in hqp_load.covered(ctx)  # not asked again
    assert [(r["filter"], r["rate_out"]) for r in hqp_load.results(ctx)
            if r["class"] == "unstarted"] == [("sinc-L", 11289600)]


def test_a_setting_that_never_starts_is_kept_once_hqplayer_starts_the_control(run, monkeypatch):
    fake = run.fake
    monkeypatch.setattr(bench, "BUILD_LIMIT_S", 0.6)
    fake.starts[("sinc-L", 11289600)] = 999.0          # this one never gets going, the rest do
    bench.start()
    assert _wait(lambda: not bench.running(), 60)
    assert bench.state()["outcome"] == "done", bench.state()
    assert run.q("""SELECT count(*) FROM hqp_benchmark_points WHERE filter = 'sinc-L'
                     AND rate_hz = 11289600 AND result = 'unstarted'""") == [(1,)]


def test_an_hqplayer_that_starts_nothing_any_more_marks_nothing_never_started(run, monkeypatch):
    fake = run.fake
    monkeypatch.setattr(bench, "BUILD_LIMIT_S", 0.6)
    fake.sticks_at = ("sinc-L", 11289600)              # from its Play on, nothing comes up
    bench.start()
    assert _wait(lambda: not bench.running(), 60)
    st = bench.state()
    assert st["outcome"] == "failed" and "stopped starting anything" in st["note"], st
    assert run.q("SELECT count(*) FROM hqp_benchmark_points WHERE result = 'unstarted'") == [(0,)]


def test_a_point_an_output_plays_out_faster_than_real_time_is_measured(run):
    fake = run.fake
    fake.races[("sinc-L", 11289600)] = 30.0            # 30 audio seconds a poll: out in four
    bench.start()
    assert _wait(lambda: not bench.running(), 60)
    assert bench.state()["outcome"] == "done", bench.state()
    rows = run.q("""SELECT result::text FROM hqp_benchmark_points
                     WHERE filter = 'sinc-L' AND rate_hz = 11289600""")
    assert rows == [("unsettled",)], rows


def test_an_hqplayer_that_starts_nothing_from_the_first_point_marks_nothing(run, monkeypatch):
    # its output engine hung before the run: the owner's own setting, first,
    # never starts, nothing earlier in the run tells the two apart — failed,
    # asked again next run, and three in a row stop the run
    fake = run.fake
    monkeypatch.setattr(bench, "BUILD_LIMIT_S", 0.6)
    fake.sticks_at = ("poly-sinc-gauss-long", 11289600)
    bench.start()
    assert _wait(lambda: not bench.running(), 60)
    # nothing kept for good; failed points are asked again (three in a row
    # stop the run, unless one HQPlayer refuses at its Set* comes between)
    assert run.q("SELECT count(*) FROM hqp_benchmark_points WHERE result = 'unstarted'") == [(0,)]
    assert run.q("SELECT result::text FROM hqp_benchmark_points ORDER BY id")[0] == ("failed",)


def test_an_owners_setting_refused_from_the_44k_signal_does_not_stop_the_run(run):
    # the owner's own setting is first; HQPlayer stops it at Play from the
    # 44.1 kHz signal (a filter that takes no asynchronous ratio): nothing to
    # check HQPlayer against yet — failed, and the rest is measured
    fake = run.fake
    fake.unplayable.add(("poly-sinc-gauss-long", 11289600))
    bench.start()
    assert _wait(lambda: not bench.running(), 60)
    assert bench.state()["outcome"] == "done", bench.state()
    first, *_ = run.q("SELECT result::text, note FROM hqp_benchmark_points ORDER BY id")
    assert first[0] == "failed" and "not kept" in first[1], first
    # once a setting has started, the same stop at Play is HQPlayer's answer
    assert run.q("""SELECT count(*) FROM hqp_benchmark_points WHERE result = 'measured'""") \
        != [(0,)]


def test_a_rung_the_plan_holds_further_on_is_asked_next_when_the_start_is_too_slow(run):
    # three listens put the owner's setting (DSD256, the ladder's start) under
    # "ok", so the plan holds the rung below for later; measured tight again,
    # that rung is asked next — not taken for asked and stepped past
    fake = run.fake
    fake.speeds[("poly-sinc-gauss-long", "ASDM7EC", 11289600)] = 1.1
    for _ in range(3):
        hqp_load.write(hqp_load.Sample(
            endpoint_id=run.endpoint_id, source="listen", mode="SDM (DSD)", rate_out=11289600,
            filter="poly-sinc-gauss-long", shaper="ASDM7EC", matrix_profile=None,
            convolution=False, adaptive=False, src_rate=44100, src_bits=24, src_channels=2,
            src_sdm=False, process_speed=1.1))
    bench.start()
    assert _wait(lambda: not bench.running(), 60)
    assert bench.state()["outcome"] == "done", bench.state()
    rows = run.q("SELECT rate_hz, shaper FROM hqp_benchmark_points ORDER BY id")
    assert rows[:2] == [(11289600, "ASDM7EC"), (5644800, "ASDM7EC")], rows


def test_an_output_that_went_away_marks_nothing_refused(run):
    fake = run.fake
    fake.dies_at = ("sinc-L", 11289600)                # from its Play on, every Play stops at once
    bench.start()
    assert _wait(lambda: not bench.running(), 60)
    st = bench.state()
    assert st["outcome"] == "failed" and "stopped starting anything" in st["note"], st
    assert run.q("""SELECT count(*) FROM hqp_benchmark_points
                     WHERE result = 'refused' AND starts_with(note, 'HQPlayer stopped it again')""") \
        == [(0,)]


def test_a_cancel_in_a_silent_build_does_not_wait_out_the_answer(run, monkeypatch):
    fake = run.fake
    monkeypatch.setattr(bench, "ANSWER_WAIT_S", 30.0)     # an ask longer than the test
    monkeypatch.setattr(bench, "RESTORE_ANSWER_S", 0.3)
    fake.builds[("sinc-L", 11289600)] = 6.0
    bench.start()
    assert _wait(lambda: fake._silent_until > time.monotonic(), 30)
    t = time.monotonic()
    bench.cancel()
    assert _wait(lambda: not bench.running(), 10)
    assert time.monotonic() - t < 3.0                      # neither the ask nor the build waited out
    st = bench.state()
    assert st["outcome"] == "cancelled", st
    # still building: put back once HQPlayer answers again (recover), never resent meanwhile
    assert "put back" in (st["note"] or ""), st


def test_a_points_pace_is_its_median_not_the_run_over_its_measured_points(run):
    from datetime import datetime, timedelta, timezone
    t0 = datetime.now(timezone.utc)
    gaps = [14, 14, 600, 14, 14, 14, 14]                   # one never started: 600 s
    rid = _cut_short_run(run, outcome="cancelled", points_measured=6, started_at=t0,
                         finished_at=t0 + timedelta(seconds=sum(gaps)))
    at = t0
    with run.conn.cursor() as cur:
        for g in gaps:
            at += timedelta(seconds=g)
            cur.execute("""INSERT INTO hqp_benchmark_points (run_id, rate_hz, filter, shaper,
                             src_rate, src_channels, result, at)
                           VALUES (%s, 11289600, 'f', 's', 44100, 2, 'unsettled', %s)""", (rid, at))
    assert bench.pace(run.endpoint_id) == pytest.approx(14.0)


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
    # every point measured all the same — the refused one apart
    assert run.q("SELECT points_planned, points_measured FROM hqp_benchmark_runs") == [(5, 4)]


def test_an_hqplayer_back_in_the_mode_it_saved_is_measured_in_the_run_s_own(run, monkeypatch):
    fake = run.fake
    fake.jitter = 0.2
    monkeypatch.setattr(bench, "SETTLE_CAP_S", 1.0)
    fake.boot_mode = 1                  # restarted, it comes back in PCM
    bench.start()
    assert _wait(lambda: run.point() >= 3, 20)
    fake.restart()
    assert _wait(lambda: not bench.running(), 60)
    st = bench.state()
    assert st["outcome"] == "done", st["note"]
    assert ("SetMode", {"value": "2"}) in fake.commands[fake.restarted_at:]
    # nothing measured in the other mode, nothing named by its lists' indices
    assert run.q("SELECT count(*) FROM hqp_dsp_samples WHERE mode <> 'SDM (DSD)'") == [(0,)]
    assert run.q("SELECT count(*) FROM hqp_benchmark_points WHERE result = 'refused'") == [(1,)]


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


def test_recover_leaves_an_hqplayer_the_owner_changed_since(run, library):
    fake = run.fake
    _cut_short_run(run, last_rate=1, last_filter=1, last_filter1x=1, last_shaper=1)
    owners = (library / "01.flac").as_uri()
    with fake._lock:
        fake.volume, fake.sel[2] = -10.0, [2, 1, 1, 2]
        fake.playlist = [owners]
    bench.recover(run.endpoint_id)
    assert fake.settings() == (2, (2, 1, 1, 1), (2, 1, 1, 2), -10.0)
    assert fake.playlist == [owners]
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


def test_an_hqplayer_back_from_a_restart_is_lent_before_the_next_play(run):
    # HQPlayer restarted: the backend knows its mirror is gone until the
    # next play re-mirrors it — the run needs none of it (2026-10-03: "not
    # the attached output" right after an Embedded restart)
    b = run.mgr.active
    b._mirror_lost = True
    assert not b.healthy()
    bench.start()
    assert _wait(lambda: not bench.running(), 30)
    assert bench.state()["outcome"] == "done", bench.state()
    assert run.mgr.held is None and run.mgr.active.healthy()


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


def test_a_playlist_not_ours_holds_a_run_back_only_while_it_plays(run, monkeypatch):
    fake, b = run.fake, run.mgr.active
    monkeypatch.setattr(hb, "DRIFT_CHECK_EVERY", 1)
    with fake._lock:
        fake.playlist = ["http://elsewhere.invalid/a.flac"]       # another controller's
        fake.state, fake.track = int(PlaybackState.PLAYING), 1
    # this fake's status names no entry: the canary's drift decides
    assert _wait(lambda: b.drift, 15)
    with pytest.raises(bench.BenchmarkRefused, match="Another controller"):
        bench.start()
    # left over and stopped — the queue the attach could not mirror while
    # HQPlayer was not up yet looks the same: nobody uses it, the run goes
    with fake._lock:
        fake.state = int(PlaybackState.STOPPED)
    bench.start()
    assert _wait(lambda: not bench.running(), 60)
    assert bench.state()["outcome"] == "done", bench.state()["note"]


def test_a_run_here_waits_until_the_node_has_loaded_its_models(run, monkeypatch):
    import types
    import model_cache
    monkeypatch.setattr(hb, "_stream_mode", lambda: False)         # HQPlayer on this machine
    monkeypatch.setitem(sys.modules, "main", types.SimpleNamespace(
        _scan_state={"running": False}, _enrich_state={"running": False}))
    with model_cache.warming_up():                                 # the startup chain
        with pytest.raises(bench.BenchmarkRefused, match="still loading its models"):
            bench.start()
    assert not model_cache.loading() and not bench.running()


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
