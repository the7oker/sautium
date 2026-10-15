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
import os
import random
import socket
import sys
import threading
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
from playback import hqp_link as hl  # noqa: E402
from playback import hqp_benchmark as bench  # noqa: E402
from playback import hqp_load  # noqa: E402
from playback.hqp_backend import HqpBackend  # noqa: E402
from playback.manager import OutputHeld, PlaybackManager  # noqa: E402
from streaming import service as streaming_service  # noqa: E402
from streaming.proxy import MediaProxy  # noqa: E402

from test_hqp_backend import FakeHqp, _fresh_links, _item, _wait  # noqa: E402
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
    _fresh_links()
    monkeypatch.setattr(hl, "CIRCUIT_COOLDOWN_S", 0.2)
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
    # What the module keeps of runs cut short is the test's own
    monkeypatch.setattr(bench, "_owed", {})
    monkeypatch.setattr(bench, "_left", {})
    b = HqpBackend(emit=mgr._on_backend_status, queue=mgr.queue)
    mgr._active = b
    b.start()
    yield Run(fake, mgr, conn, endpoint_id)
    if bench.running():
        bench.cancel()
        _wait(lambda: not bench.running(), 20)
    # The job's thread gives the output back after running() turned False:
    # left alive it attaches HQPlayer again on the next test's fake
    job = bench._thread
    if job is not None:
        job.join(20)
        assert not job.is_alive(), "the benchmark's thread outlived its test"
    if mgr.active is not None:
        mgr.active.shutdown()
    _fresh_links()
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


def test_a_put_back_owed_gives_way_to_the_owners_newer_change(run, monkeypatch, request):
    """A run HQPlayer left in the middle — nobody could put it back — owes
    its put-back as a DSP item as of the run's end: the owner's filter
    change, made once HQPlayer answers again and before the put-back goes,
    supersedes it — HQPlayer keeps the owner's filter, and the run is closed
    as left to the owner."""
    fake, mgr = run.fake, run.mgr
    fake.jitter = 0.2
    monkeypatch.setattr(bench, "SETTLE_CAP_S", 1.0)
    # The poller's recovery, once HQPlayer answers again, waits for the
    # owner's change to go first
    asked, go = threading.Event(), threading.Event()
    request.addfinalizer(go.set)        # a failure before it frees the poller before the teardown
    recover = hb.HqpBackend._recover
    monkeypatch.setattr(hb.HqpBackend, "_recover",
                        lambda self: (asked.set(), go.wait(15), recover(self))[2])
    bench.start()
    assert _wait(lambda: run.point() >= 3, 20)
    fake.close()                        # powered off in the middle of a point
    assert _wait(lambda: not bench.running(), 30)
    assert run.q("SELECT finished_at IS NULL FROM hqp_benchmark_runs") == [(True,)]
    fake.come_back()                    # the run's last selection, its lowered volume
    assert _wait(lambda: (mgr.active.poke(), asked.is_set())[1], 20)
    out = hb.change_dsp({"filter": 0, "filter1x": 0, "shaper": 1})    # the owner's
    assert out["ok"], out
    owners = fake.settings()
    go.set()                                         # the put-back may go now
    assert _wait(lambda: (mgr.active.poke(), run.q(
        "SELECT finished_at IS NOT NULL FROM hqp_benchmark_runs") == [(True,)])[1], 20)
    assert fake.settings() == owners
    assert "left as it was set since" in run.q("SELECT note FROM hqp_benchmark_runs")[0][0]


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
    bench.recover(run.endpoint_id, hl.selected())
    # PCM's own selection back, then SDM and its selection, the volume, no signal
    assert fake.settings() == (2, (2, 1, 1, 1), (2, 0, 0, 0), -3.0)
    assert fake.playlist == []
    assert run.q("SELECT outcome::text FROM hqp_benchmark_runs") == [("interrupted",)]


def _run_left():
    """The notice of a run left to the owner, as the notices channel derives
    it for the view layer."""
    from routers import settings as settings_router
    return next((n for n in settings_router._notices_state()["items"]
                 if n["key"] == "hqplayer.run_left"), None)


def _run_left_items():
    left = _run_left()
    return {it["what"]: it for it in ((left or {}).get("data") or {}).get("items") or []}


def _closed_as_left(r: Run, run_id: int) -> bool:
    return r.q("SELECT finished_at IS NOT NULL, left_at IS NOT NULL FROM hqp_benchmark_runs "
               "WHERE id = %s", run_id) == [(True, True)]


# What a PCM run from SDM, left to the owner on its last point, still holds of its
# test settings while HQPlayer is in PCM: each as now → before the run
_LEFT_IN_PCM = {
    "volume": {"what": "volume", "now": -60.0, "before": -3.0},
    "mode": {"what": "mode", "now": "PCM", "before": "SDM (DSD)"},
    "rate": {"what": "rate", "now": 352800, "before": 705600},
    "shaper": {"what": "shaper", "now": "none", "before": "LNS15"}}


def _owed_pcm_run(r: Run) -> int:
    """A "Measure PCM" run from SDM cut short, its put-back owed on the
    selected link: HQPlayer in PCM on the run's last point, lowered."""
    run_id = _cut_short_run(r, mode="PCM", own_mode=1, own_rate=2, own_filter=1, own_filter1x=1,
                            own_shaper=1, last_rate=1, last_filter=0, last_filter1x=0, last_shaper=0)
    with r.fake._lock:
        r.fake.mode, r.fake.sel[1], r.fake.volume = 1, [1, 0, 0, 0], -60.0
    bench._owe(run_id, hl.selected())
    return run_id


def test_a_put_back_a_person_superseded_leaves_one_notice_of_what_the_run_left(run):
    """The owner changes the filter before a run's owed put-back goes: their
    filter stays, the volume stays at the bottom of the range — nothing
    raises it for them — and one notice names what still holds the run's
    test values, each as now → before the run, the mode and the rate among
    them; it ends for good once they changed it all, the selection PCM
    keeps while HQPlayer is in SDM included."""
    fake, mgr = run.fake, run.mgr
    run_id = _owed_pcm_run(run)
    out = hb.change_dsp({"filter": 1, "filter1x": 1})          # the owner's, first
    assert out["ok"], out
    bench.recover(run.endpoint_id, hl.selected())               # HQPlayer answers
    assert _wait(lambda: run.q("SELECT finished_at IS NOT NULL, left_at IS NOT NULL "
                               "FROM hqp_benchmark_runs WHERE id = %s", run_id) == [(True, True)])
    assert fake.settings() == (1, (1, 1, 1, 0), (2, 0, 0, 0), -60.0)

    def notice():
        mgr.active.poke()           # the poller reads what is left at its next answer
        return _run_left_items()
    assert _wait(lambda: notice() == _LEFT_IN_PCM, 10)
    left = _LEFT_IN_PCM
    from routers import hqplayer as hqp_router
    screen = hqp_router.get_state(dsp=True)["benchmark"]["left"]     # the HQPlayer screen too
    assert {it["what"]: it for it in screen} == left
    assert fake.volume == -60.0                                 # never raised for them
    assert hb.change_dsp({"mode": 2})["ok"]                     # they go back to SDM...
    assert mgr.active.set_volume(-12.0)                         # ...and raise the volume:
    # PCM keeps the run's rate and shaper — HQPlayer keeps one selection per mode
    elsewhere = {"what": "elsewhere", "mode": "PCM", "knobs": ["rate", "shaper"]}
    assert _wait(lambda: notice() == {"elsewhere": elsewhere}, 10), notice()
    assert hb.change_dsp({"mode": 1, "rate": 2, "shaper": 1})["ok"]     # theirs in PCM again...
    assert _wait(lambda: set(notice()) == {"mode"}, 10), notice()
    assert hb.change_dsp({"mode": 2})["ok"]                     # ...and SDM
    assert _wait(lambda: (mgr.active.poke(), run.q(
        "SELECT left_at IS NULL FROM hqp_benchmark_runs WHERE id = %s", run_id) == [(True,)])[1])
    assert _run_left() is None
    assert not bench.left_pending(run.endpoint_id)              # the poller asks no more for it
    assert hb.change_dsp({"mode": 1})["ok"]                     # the run's mode and volume again,
    assert mgr.active.set_volume(-60.0)                         # by their own hand...
    assert hqp_router.get_state(dsp=True)["benchmark"]["left"] is None   # ...are no run's


def test_the_play_gates_reattach_leaves_an_owed_put_back_owed(run, monkeypatch):
    """The play gate attaching the same HQPlayer again — it was believed
    gone a moment ago — is no switch away: the put-back a run cut short
    owes goes at the attach, never closed as left to the owner."""
    fake, mgr = run.fake, run.mgr
    from routers import settings as settings_router
    monkeypatch.setattr(settings_router, "_read", lambda key: {
        "output.type": "hqplayer", "hqplayer.host": "127.0.0.1",
        "hqplayer.port": fake.port}.get(key))
    run_id = _owed_pcm_run(run)
    old = mgr.active
    monkeypatch.setattr(old, "_recover", lambda: None)          # its poller puts back nothing
    old._gone = True                                            # the gate found it offline
    assert mgr.ensure_active() is not old                       # back: attached again
    assert _wait(lambda: run.q("SELECT finished_at IS NOT NULL FROM hqp_benchmark_runs "
                               "WHERE id = %s", run_id) == [(True,)])
    assert fake.settings() == (2, (2, 1, 1, 1), (2, 0, 0, 0), -3.0)
    assert "put back when it answered again" in run.q(
        "SELECT note FROM hqp_benchmark_runs WHERE id = %s", run_id)[0][0]


def test_a_new_run_takes_the_put_back_still_owed_first(run):
    """A run cut short still owes its put-back when the owner starts the
    next: it goes first — the new run reads the owner's settings, not the
    old run's test point and lowered volume — and the new run puts those
    back at its end."""
    fake = run.fake
    run_id = _owed_pcm_run(run)
    bench.start()
    assert _wait(lambda: not bench.running(), 60)
    st = bench.state()
    assert st["outcome"] == "done", st["note"]
    assert run.q("SELECT note FROM hqp_benchmark_runs WHERE id = %s", run_id)[0][0].endswith(
        "put back when it answered again")
    assert fake.settings() == (2, (2, 1, 1, 1), (2, 0, 0, 0), -3.0)
    assert run.q("SELECT pre_mode, pre_volume FROM hqp_benchmark_runs WHERE id > %s",
                 run_id) == [(2, -3.0)]


def test_a_put_back_still_owed_once_a_run_began_goes_first_on_its_connection(run, monkeypatch):
    """The put-back a run cut short owes has not gone when the next run
    begins — its try at the start met HQPlayer away — and the owner
    confirmed a fixed volume: the run takes it first, on its own
    connection, so it reads and puts back at its end the owner's settings,
    never the old run's test point."""
    fake = run.fake
    run_id = _owed_pcm_run(run)
    monkeypatch.setattr(bench, "recover", lambda endpoint_id, link, **kw: None)
    bench.start(fixed_volume_ok=True)
    assert _wait(lambda: not bench.running(), 60)
    st = bench.state()
    assert st["outcome"] == "done", st["note"]
    assert run.q("SELECT note FROM hqp_benchmark_runs WHERE id = %s", run_id)[0][0].endswith(
        "put back when it answered again")
    assert fake.settings() == (2, (2, 1, 1, 1), (2, 0, 0, 0), -3.0)
    assert run.q("SELECT pre_mode, pre_volume FROM hqp_benchmark_runs WHERE id > %s",
                 run_id) == [(2, -3.0)]


def test_a_new_run_ends_the_notice_of_one_left_to_the_owner(run):
    """A run left to the owner is named until every setting it left was
    changed — or until the owner starts the next run, which reads what
    HQPlayer holds as theirs."""
    mgr = run.mgr
    run_id = _owed_pcm_run(run)
    assert hb.change_dsp({"filter": 1, "filter1x": 1})["ok"]       # the owner's, first
    bench.recover(run.endpoint_id, hl.selected())
    assert _wait(lambda: (mgr.active.poke(), _run_left())[1] is not None, 10)
    bench.start(fixed_volume_ok=True)                   # their volume is the range's bottom now
    assert _wait(lambda: not bench.running(), 60)
    st = bench.state()
    assert st["outcome"] == "done", st["note"]
    assert _run_left() is None
    assert run.q("SELECT left_at FROM hqp_benchmark_runs WHERE id = %s", run_id) == [(None,)]


def test_recover_leaves_an_hqplayer_the_owner_changed_since(run, library):
    """The owner's own album queued, their volume, a modulator of theirs: what
    they set in HQPlayer's window since the run is theirs — nothing is put
    back, and the run is left to them."""
    fake = run.fake
    run_id = _cut_short_run(run, last_rate=1, last_filter=1, last_filter1x=1, last_shaper=1)
    owners = (library / "01.flac").as_uri()
    with fake._lock:
        fake.volume, fake.sel[2] = -10.0, [2, 1, 1, 2]
        fake.playlist = [owners]
    bench.recover(run.endpoint_id, hl.selected())
    assert fake.settings() == (2, (2, 1, 1, 1), (2, 1, 1, 2), -10.0)
    assert fake.playlist == [owners]
    assert _closed_as_left(run, run_id)
    assert "shaper, volume changed in HQPlayer since" in run.q(
        "SELECT note FROM hqp_benchmark_runs")[0][0]


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
    # HQPlayer restarted: the backend knows its mirror is gone until HQPlayer
    # gets the queue back — the run needs none of it (2026-10-03: "not the
    # attached output" right after an Embedded restart). Its playlist empty
    # and the loss a refusal's, the mirror stays lost until a play press: a
    # loss through a silence is given back at the poller's next answer, and a
    # playlist that is the queue reads as in step — either maybe before the
    # next line.
    b = run.mgr.active
    with run.fake._lock:
        run.fake.playlist = []
    b.lose_mirror("HQPlayer came back with a different playlist (restarted?)", by="refusal")
    assert b._mirror == "lost"
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


def test_a_list_hqplayer_did_not_give_refuses_a_run_or_ends_it_put_back(run):
    # a refused GetModes read as no modes said "HQPlayer's mode is [source]"
    fake = run.fake
    fake.MINE = DspFake.MINE - {"GetModes"}
    fake.replies["GetModes"] = '<GetModes result="Error"/>'
    with pytest.raises(bench.BenchmarkRefused, match="HQPlayer did not list its modes"):
        bench.start()
    fake.MINE = DspFake.MINE - {"GetShapers"}
    fake.replies["GetShapers"] = '<GetShapers result="Error"/>'
    before = fake.settings()
    bench.start()
    assert _wait(lambda: not bench.running(), 20)
    st = bench.state()
    assert (st["outcome"], st["note"]) == ("failed", "HQPlayer did not list its shapers")
    assert fake.settings() == before


def test_a_cancel_never_closes_the_socket_the_run_reads(run, monkeypatch):
    """A cancel wakes the run's read of an answer HQPlayer holds through a
    build, and the run's own thread closes its socket: one closed by the
    cancelling thread raced the read, which then waited its whole wait now
    and then."""
    fake = run.fake
    monkeypatch.setattr(bench, "ANSWER_WAIT_S", 30.0)
    monkeypatch.setattr(bench, "RESTORE_ANSWER_S", 0.3)
    fake.builds[("sinc-L", 11289600)] = 6.0
    bench.start()
    assert _wait(lambda: fake._silent_until > time.monotonic(), 30)
    held = bench._live.c.socket
    made = []
    close = socket.socket.close

    def recorded(sock):
        made.append((threading.current_thread().name, sock))
        close(sock)
    monkeypatch.setattr(socket.socket, "close", recorded)
    bench.cancel()
    assert _wait(lambda: not bench.running(), 10)
    assert [who for who, sock in made if sock is held] == ["hqp-benchmark"]


@pytest.mark.parametrize("again", range(int(os.environ.get("SAUTIUM_LOOP_RUNS", "3"))))
def test_a_cancel_in_a_silent_build_ends_at_once_again_and_again(run, monkeypatch, again):
    """The cancel of the test above, run again and again: a socket the
    cancel closed under the read cost the run its whole wait now and then
    (SAUTIUM_LOOP_RUNS=40 for a long run)."""
    test_a_cancel_in_a_silent_build_does_not_wait_out_the_answer(run, monkeypatch)


def test_a_benchmarks_hold_is_no_switch_away(run):
    """A run's hold detaches the output, and is no switch away: what was
    made for the attachment it lends — a put-back owed, the rest of a DSP
    change — is superseded by nothing it does."""
    link = hl.selected()
    item = link.item("dsp_knob", link.accept("dsp"))
    bench.start()
    assert _wait(lambda: run.point() >= 1, 20)
    assert link.superseded(item) is None
    bench.cancel()
    assert _wait(lambda: not bench.running(), 30)


def test_a_put_back_a_switch_superseded_stays_superseded_on_a_new_link(run):
    """A person's switch away supersedes a run's owed put-back on the link it
    was owed on; HQPlayer picked again on a link made afresh owes it no more
    — the run is closed as left to the owner."""
    run_id = _owed_pcm_run(run)
    old = hl.selected()
    old.accept("switch")                        # a person switched away from it
    _fresh_links()                              # ...and its link went with another pick
    bench.recover(run.endpoint_id, hl.selected())
    assert _wait(lambda: run.q("SELECT finished_at IS NOT NULL, left_at IS NOT NULL "
                               "FROM hqp_benchmark_runs WHERE id = %s", run_id) == [(True, True)])
    assert run.fake.settings() == (1, (1, 0, 0, 0), (2, 0, 0, 0), -60.0)


# -- A run left to the owner: written where it happens, judged knob by knob, named
# from the measured mode's lists, its notice the run row's (2026-10-13) ----------

def test_a_switch_away_leaves_an_owed_put_back_to_the_owner_for_good(run, monkeypatch):
    """A person switches the output away while a run cut short owes its
    put-back: the run is closed as left to them where the switch happens —
    a restart, which forgets what the process held, and HQPlayer picked and
    answering again put nothing back and raise no volume."""
    fake, mgr = run.fake, run.mgr
    run_id = _owed_pcm_run(run)
    mgr.active.release()                                    # the person's switch away
    assert _closed_as_left(run, run_id)
    monkeypatch.setattr(bench, "_owed", {})                 # a restart
    _fresh_links()
    bench.recover(run.endpoint_id, hl.selected())           # picked, and answering again
    assert fake.settings() == (1, (1, 0, 0, 0), (2, 0, 0, 0), -60.0)


def test_a_dsp_change_leaves_an_owed_put_back_to_the_owner_for_good(run, monkeypatch):
    """The owner's DSP change while a run cut short owes its put-back closes
    the run as left to them at once, in its row: after a restart, HQPlayer
    answering puts nothing back over their change."""
    fake = run.fake
    run_id = _owed_pcm_run(run)
    assert hb.change_dsp({"filter": 1, "filter1x": 1})["ok"]     # the owner's
    assert _closed_as_left(run, run_id)
    monkeypatch.setattr(bench, "_owed", {})                 # a restart
    _fresh_links()
    bench.recover(run.endpoint_id, hl.selected())
    assert fake.settings() == (1, (1, 1, 1, 0), (2, 0, 0, 0), -60.0)


def test_a_dsp_change_the_database_cannot_record_goes_all_the_same(run, monkeypatch):
    """The owner's DSP change while a run cut short owes its put-back, and
    the database refuses to record the run as left to them: the change goes
    all the same, and in this process the put-back stays superseded — no
    answer of HQPlayer puts it back over their change."""
    import psycopg2
    fake = run.fake
    run_id = _owed_pcm_run(run)
    read = bench.db_query

    def refusing(sql, params=None):
        raise psycopg2.OperationalError("the database is away")
    monkeypatch.setattr(bench, "db_query", refusing)
    assert hb.change_dsp({"filter": 1, "filter1x": 1})["ok"]
    monkeypatch.setattr(bench, "db_query", read)
    bench.recover(run.endpoint_id, hl.selected())
    assert _wait(lambda: _closed_as_left(run, run_id))
    assert fake.settings() == (1, (1, 1, 1, 0), (2, 0, 0, 0), -60.0)


def test_another_hqplayer_chosen_leaves_an_owed_put_back_to_the_owner(run, monkeypatch):
    """The owner picks another HQPlayer while a run cut short on this one
    owes its put-back: the run is closed as left to them — picked again
    later, this HQPlayer keeps what it holds."""
    from routers import settings as settings_router
    run_id = _owed_pcm_run(run)
    monkeypatch.setattr(settings_router, "_write", lambda key, value: None)
    settings_router._apply_hqplayer_address("127.0.0.1", 1)    # another HQPlayer's address
    assert _closed_as_left(run, run_id)


def test_a_setting_changed_in_hqplayers_own_window_leaves_the_run_to_the_owner(run):
    """A run cut short owes its put-back; the owner changes only the filter,
    in HQPlayer's own window. HQPlayer answers: that knob is neither the
    run's last point nor what was there before the run — the owner's — so
    the put-back is theirs to forgo: nothing more is sent — the volume, still
    at the run's level, goes back to theirs — and the notice names what
    still holds the run's test values."""
    fake, mgr = run.fake, run.mgr
    run_id = _cut_short_run(run, mode="PCM", own_mode=1, own_rate=2, own_filter=1,
                            own_filter1x=1, own_shaper=1, last_rate=1, last_filter=1,
                            last_filter1x=1, last_shaper=0)
    with fake._lock:                    # on the run's last point but the filter: the owner's
        fake.mode, fake.sel[1], fake.volume = 1, [1, 0, 1, 0], -60.0
    bench._owe(run_id, hl.selected())
    bench.recover(run.endpoint_id, hl.selected())           # HQPlayer answers
    assert _wait(lambda: _closed_as_left(run, run_id))
    assert fake.settings() == (1, (1, 0, 1, 0), (2, 0, 0, 0), -3.0)
    left = {k: v for k, v in _LEFT_IN_PCM.items() if k != "volume"}
    assert _wait(lambda: (mgr.active.poke(), _run_left_items())[1] == left, 10)


def test_a_setting_of_the_owners_in_the_mode_the_run_found_leaves_it_to_them(run):
    """A PCM run from SDM cut short; the owner puts HQPlayer back in SDM and
    changes its filter there, in HQPlayer's own window. The run set nothing
    in SDM: that filter is theirs — nothing is put back but the volume, still
    at the run's level."""
    fake = run.fake
    run_id = _owed_pcm_run(run)
    with fake._lock:
        fake.mode, fake.sel[2] = 2, [2, 1, 1, 0]
    bench.recover(run.endpoint_id, hl.selected())
    assert _wait(lambda: _closed_as_left(run, run_id))
    assert fake.settings() == (2, (1, 0, 0, 0), (2, 1, 1, 0), -3.0)


def test_the_notice_names_what_is_left_from_the_measured_modes_lists(run):
    """A PCM run left to the owner while they put HQPlayer in SDM: the
    poller's first read of it is in SDM, which names the knobs PCM keeps as
    the run left them. Back in PCM by HQPlayer's own window, the notice
    names the rate and the shaper still on the run's test values from PCM's
    lists — never SDM's."""
    fake, mgr = run.fake, run.mgr
    run_id = _owed_pcm_run(run)
    assert hb.change_dsp({"mode": 2})["ok"]                 # the owner's: SDM
    bench.recover(run.endpoint_id, hl.selected())
    assert _wait(lambda: _closed_as_left(run, run_id))
    assert _wait(lambda: (mgr.active.poke(), _run_left_items())[1] == {
        "volume": {"what": "volume", "now": -60.0, "before": -3.0},
        "elsewhere": {"what": "elsewhere", "mode": "PCM",
                      "knobs": ["rate", "filter", "filter1x", "shaper"]}}, 10)
    with fake._lock:
        fake.mode = 1                                       # PCM again, in HQPlayer's window
    filters = {"now": "poly-sinc-gauss-long", "before": "sinc-L"}     # the run's last point's
    assert _wait(lambda: (mgr.active.poke(), _run_left_items())[1] == {
        **_LEFT_IN_PCM, "filter": {"what": "filter", **filters},
        "filter1x": {"what": "filter1x", **filters}}, 10)


def test_a_notice_write_the_database_refuses_costs_the_notice_never_the_poll(run, monkeypatch):
    """Every setting a run left was changed back, and the database refuses
    the write that ends its notice: the poll goes on as if nothing happened
    — no miss, no back-off — and the notice ends at a later answer, once the
    database takes it."""
    import psycopg2
    fake, mgr = run.fake, run.mgr
    run_id = _owed_pcm_run(run)
    assert hb.change_dsp({"filter": 1, "filter1x": 1})["ok"]
    bench.recover(run.endpoint_id, hl.selected())
    assert _wait(lambda: (mgr.active.poke(), _run_left())[1] is not None, 10)
    write = bench.db_execute
    refused = threading.Event()

    def refusing(sql, params=None):
        if "left_at = NULL" in sql and not refused.is_set():
            refused.set()
            raise psycopg2.OperationalError("the database is away")
        return write(sql, params)
    monkeypatch.setattr(bench, "db_execute", refusing)
    with fake._lock:                                        # the owner's: SDM, their volume...
        fake.mode, fake.volume = 2, -12.0
    # ... while PCM keeps the run's test values: the notice stands
    assert _wait(lambda: (mgr.active.poke(), set(_run_left_items()))[1] == {"elsewhere"}, 10)
    with fake._lock:                                        # PCM's own selection theirs again...
        fake.mode, fake.sel[1] = 1, [2, 1, 1, 1]
    assert _wait(lambda: (mgr.active.poke(), set(_run_left_items()))[1] == {"mode"}, 10)
    with fake._lock:
        fake.mode = 2                                       # ... and SDM: nothing left
    assert _wait(lambda: (mgr.active.poke(), refused.is_set())[1], 10)
    assert mgr.active._failures == 0
    assert _wait(lambda: (mgr.active.poke(), run.q(
        "SELECT left_at IS NULL FROM hqp_benchmark_runs WHERE id = %s", run_id) == [(True,)])[1],
        10)
    assert _run_left() is None


def test_a_left_run_the_database_did_not_give_is_asked_again(run, monkeypatch):
    """The poller asks whether a run was left to the owner on this HQPlayer,
    and the database fails that read: it is asked again at the next answer,
    never taken for "none" for the life of the process."""
    import psycopg2
    run_id = _owed_pcm_run(run)
    assert hb.change_dsp({"filter": 1, "filter1x": 1})["ok"]
    bench.recover(run.endpoint_id, hl.selected())
    assert _wait(lambda: _closed_as_left(run, run_id))
    monkeypatch.setattr(bench, "_left", {})                 # not read yet
    read = bench.db_query_one

    def failing(sql, params=None):
        raise psycopg2.OperationalError("the database is away")
    monkeypatch.setattr(bench, "db_query_one", failing)
    assert not bench.left_pending(run.endpoint_id)
    monkeypatch.setattr(bench, "db_query_one", read)
    assert bench.left_pending(run.endpoint_id)


def test_a_run_that_never_opened_its_row_leaves_the_notice_standing(run, monkeypatch):
    """A run left to the owner is named until a new run reads what HQPlayer
    holds as theirs — one that never got as far as its own row read nothing,
    and the notice stands."""
    mgr = run.mgr
    run_id = _owed_pcm_run(run)
    assert hb.change_dsp({"filter": 1, "filter1x": 1})["ok"]
    bench.recover(run.endpoint_id, hl.selected())
    assert _wait(lambda: (mgr.active.poke(), _run_left())[1] is not None, 10)

    def no_row(*args):
        raise bench._Abort("This HQPlayer is no longer registered")
    monkeypatch.setattr(bench, "_open_run", no_row)
    bench.start(fixed_volume_ok=True)
    assert _wait(lambda: not bench.running(), 60)
    assert bench.state()["outcome"] == "failed"
    assert run.q("SELECT left_at IS NOT NULL FROM hqp_benchmark_runs WHERE id = %s",
                 run_id) == [(True,)]
    assert _wait(lambda: _run_left() is not None, 10)


def test_the_notice_of_a_run_left_stands_on_its_row(run, monkeypatch):
    """The notice of a run left to the owner is its row's: a restart — what
    the process held, gone, nothing read of HQPlayer yet — and another
    output picked keep it in the channel, its onset the same: never gone and
    back, to be toasted again."""
    fake, mgr = run.fake, run.mgr
    run_id = _owed_pcm_run(run)
    assert hb.change_dsp({"filter": 1, "filter1x": 1})["ok"]
    bench.recover(run.endpoint_id, hl.selected())
    assert _wait(lambda: (mgr.active.poke(), _run_left_items())[1] == _LEFT_IN_PCM, 10)
    since = _run_left()["since"]
    monkeypatch.setattr(bench, "_left", {})                 # a restart
    assert (_run_left() or {}).get("since") == since
    mgr.active.shutdown()
    mgr._active = None                                      # another output
    assert (_run_left() or {}).get("since") == since


# -- A run HQPlayer went away under; the notice's own bookkeeping --------------------


def _gone_mid_run(run, monkeypatch, **boot) -> tuple:
    """A run HQPlayer went away under — powered off in the middle of a point
    — and HQPlayer back, booted with `boot` (its saved mode, its saved
    volume): (the settings before the run, the closed run's note)."""
    fake, mgr = run.fake, run.mgr
    fake.jitter = 0.2
    monkeypatch.setattr(bench, "SETTLE_CAP_S", 1.0)
    before = fake.settings()
    for name, value in boot.items():
        setattr(fake, name, value)
    bench.start()
    assert _wait(lambda: run.point() >= 3, 20)
    fake.close()
    assert _wait(lambda: not bench.running(), 30)
    assert run.q("SELECT finished_at IS NULL, lost_at IS NOT NULL FROM hqp_benchmark_runs") == \
        [(True, True)]
    fake.come_back()

    def closed():
        mgr.active.poke()
        return run.q("SELECT finished_at IS NOT NULL FROM hqp_benchmark_runs") == [(True,)]
    assert _wait(closed, 30)
    return before, run.q("SELECT note FROM hqp_benchmark_runs")[0][0]


def _run_restored():
    from routers import settings as settings_router
    return next((n for n in settings_router._notices_state()["items"]
                 if n["key"] == "hqplayer.run_restored"), None)


def test_a_run_hqplayer_restarted_under_is_put_back_whole_its_volume_with_it(run, monkeypatch):
    """HQPlayer went away mid-run and boots back in the mode it saved (PCM),
    its volume where the run lowered it: no one changed a knob — what it
    shows is what it booted with — so the run is put back whole, the volume
    raised from the run's own level, and a notice says why."""
    before, note = _gone_mid_run(run, monkeypatch, boot_mode=1)
    assert run.fake.settings() == before, (note, run.fake.settings())
    assert "HQPlayer went away under the run" in note
    assert run.q("SELECT left_at IS NULL FROM hqp_benchmark_runs") == [(True,)]
    restored = _run_restored()
    assert restored is not None and restored["data"]["volume_left"] is None, restored


def test_a_run_hqplayer_restarted_under_at_its_own_level_leaves_that_level(run, monkeypatch):
    """HQPlayer went away mid-run and boots back at a level it saved (-20 dB,
    neither the run's -60 nor the owner's -3): the rest is put back, the
    volume is not raised — from any level but the run's own it is the
    owner's to raise — and the notice says it was left as it is."""
    before, note = _gone_mid_run(run, monkeypatch, boot_volume=-20.0)
    after = run.fake.settings()
    assert after[:3] == before[:3] and after[3] == -20.0, (note, after)
    started = run.q("SELECT started_at FROM hqp_benchmark_runs")[0][0]
    restored = _run_restored()
    assert restored["data"] == {"run_at": started.isoformat(), "volume_left": -20.0,
                                "volume_before": -3.0}, restored


def test_a_run_hqplayer_restarted_under_louder_is_lowered_to_the_owners_level(run, monkeypatch):
    """HQPlayer boots back louder than the owner's level: lowering is always
    allowed — the put-back takes it down to it."""
    before, note = _gone_mid_run(run, monkeypatch, boot_volume=0.0)
    assert run.fake.settings() == before, (note, run.fake.settings())


def test_the_restored_notice_is_gone_once_seen(run, monkeypatch):
    """The notice of a run put back after a restart is a report: once the
    owner saw it — the rows of notices visited — it is gone."""
    from routers import settings as settings_router
    _gone_mid_run(run, monkeypatch, boot_mode=1)
    store = {"output.type": "hqplayer"}
    monkeypatch.setattr(settings_router, "_read", store.get)
    monkeypatch.setattr(settings_router, "_write", store.__setitem__)
    monkeypatch.setattr(settings_router, "_guidance_state", lambda: {})
    monkeypatch.setattr(settings_router, "notify_library_subscribers", lambda: None)
    assert _run_restored() is not None
    settings_router.post_guidance_seen("notices")
    assert _run_restored() is None


def test_a_new_run_ends_the_restored_notice(run, monkeypatch):
    """... and so does the next run, once it opened its row: it reads what
    HQPlayer holds as the owner's."""
    _gone_mid_run(run, monkeypatch, boot_mode=1)
    assert _run_restored() is not None
    bench.start()
    assert _wait(lambda: not bench.running(), 60)
    assert bench.state()["outcome"] == "done", bench.state()["note"]
    assert _run_restored() is None


def test_a_notify_the_database_refuses_costs_the_notice_never_the_poll(run, monkeypatch):
    """The NOTIFY that wakes the channel when what a run left changed is a
    write too: the database refusing it costs the notice — sent again at a
    later answer — never the poller's tick."""
    import psycopg2
    fake, mgr = run.fake, run.mgr
    write = bench.db_execute
    refused = threading.Event()

    def refusing(sql, params=None):
        if (sql.startswith("NOTIFY") and threading.current_thread().name == "hqp-status-poller"
                and not refused.is_set()):
            refused.set()
            raise psycopg2.OperationalError("the database is away")
        return write(sql, params)
    monkeypatch.setattr(bench, "db_execute", refusing)
    missed = threading.Event()
    register = mgr.active._register_failure

    def counting(*, silent):
        missed.set()
        return register(silent=silent)
    monkeypatch.setattr(mgr.active, "_register_failure", counting)
    run_id = _owed_pcm_run(run)
    assert hb.change_dsp({"filter": 1, "filter1x": 1})["ok"]
    bench.recover(run.endpoint_id, hl.selected())
    assert _wait(lambda: _closed_as_left(run, run_id))
    assert _wait(lambda: (mgr.active.poke(), refused.is_set())[1], 10)
    assert _wait(lambda: (mgr.active.poke(), _run_left_items())[1] != {}, 10)
    assert not missed.is_set(), "a NOTIFY the database refused cost the poll a miss"


def test_a_list_hqplayer_did_not_give_is_read_again(run, monkeypatch):
    """The notice's lists of the measured mode: one HQPlayer did not give at
    the first read is asked again at the next answer, until it gives it."""
    fake, mgr = run.fake, run.mgr
    dispatch = fake._dispatch
    failing = threading.Event()
    failing.set()

    def rates_refused(cmd, attrs):
        if cmd == "GetRates" and failing.is_set():
            return "<Nope/>"
        return dispatch(cmd, attrs)
    monkeypatch.setattr(fake, "_dispatch", rates_refused)
    run_id = _owed_pcm_run(run)
    assert hb.change_dsp({"filter": 1, "filter1x": 1})["ok"]
    bench.recover(run.endpoint_id, hl.selected())
    assert _wait(lambda: _closed_as_left(run, run_id))
    assert _wait(lambda: (mgr.active.poke(), _run_left_items().get("rate"))[1]
                 == {"what": "rate", "now": None, "before": None}, 10)
    failing.clear()                     # HQPlayer gives its rates from now on
    assert _wait(lambda: (mgr.active.poke(), _run_left_items().get("rate"))[1]
                 == _LEFT_IN_PCM["rate"], 6)


def test_a_run_left_in_another_mode_names_what_the_measured_mode_holds(run):
    """A run measuring SDM cut short, the owner puts HQPlayer in PCM in its
    own window — the run is left to them. HQPlayer keeps one selection per
    mode and State shows PCM's: the notice names what SDM still holds from
    the run's row — the volume, at the run's level, went back to theirs —
    and it stands whatever the volume does next, until HQPlayer is read back
    in SDM."""
    fake, mgr = run.fake, run.mgr
    run_id = _cut_short_run(run, last_rate=1, last_filter=1, last_filter1x=1, last_shaper=1)
    with fake._lock:                    # SDM on the run's last point; PCM chosen in its window
        fake.mode, fake.sel[2], fake.volume = 1, [1, 1, 1, 1], -60.0
    bench._owe(run_id, hl.selected())
    bench.recover(run.endpoint_id, hl.selected())
    assert _wait(lambda: _closed_as_left(run, run_id))
    elsewhere = {"what": "elsewhere", "mode": "SDM (DSD)",
                 "knobs": ["rate", "filter", "filter1x", "shaper"]}
    assert _wait(lambda: (mgr.active.poke(), _run_left_items())[1] == {
        "elsewhere": elsewhere}, 10), _run_left_items()
    assert fake.volume == -3.0
    with fake._lock:
        fake.volume = -12.0             # the owner sets the volume
    assert _wait(lambda: (mgr.active.poke(), _run_left_items())[1] == {
        "elsewhere": elsewhere}, 10), _run_left_items()
    assert run.q("SELECT left_at IS NOT NULL FROM hqp_benchmark_runs WHERE id = %s",
                 run_id) == [(True,)]


def test_a_close_while_the_left_run_is_first_read_is_read_again(run, monkeypatch):
    """The poller's first read of a run left to the owner is on its way when
    a person's change closes the run as left: what it read is not kept as
    "no run left" — the next answer reads it again, and the notice is named."""
    fake, mgr = run.fake, run.mgr
    run_id = _owed_pcm_run(run)
    read = bench._left_run
    reading, closed = threading.Event(), threading.Event()

    def slow(endpoint_id):
        row = read(endpoint_id)
        reading.set()
        closed.wait(10)
        return row
    # The slow read first; the cache emptied as a change does it — a read
    # the poller had on its way is not kept (left_pending's generation)
    monkeypatch.setattr(bench, "_left_run", slow)
    with bench._lock:
        monkeypatch.setattr(bench, "_left", {})         # not read yet
        bench._bump()
    mgr.active.poke()
    assert reading.wait(10)                             # the poller's read is on its way
    bench.left_by_person("127.0.0.1", fake.port, "a DSP change came first")
    closed.set()
    assert _closed_as_left(run, run_id)
    monkeypatch.setattr(bench, "_left_run", read)
    assert _wait(lambda: (mgr.active.poke(), _run_left_items())[1] != {}, 5), _run_left()


def test_a_put_back_that_went_is_never_recorded_as_left(run, monkeypatch):
    """The put-back goes and its row is closed while the command lock is
    still the put-back's: a person's DSP change asked for meanwhile finds the
    run closed — put back, no notice — never closes it as left to them."""
    fake = run.fake
    run_id = _owed_pcm_run(run)
    close, by_person = bench._close_owed, bench.left_by_person
    holding, go, asked = threading.Event(), threading.Event(), threading.Event()

    def held(row, note, *args, **kw):
        if threading.current_thread().name == "hqp-benchmark-recover":
            holding.set()
            go.wait(10)
        return close(row, note, *args, **kw)

    def person(*args):
        asked.set()
        return by_person(*args)
    monkeypatch.setattr(bench, "_close_owed", held)
    monkeypatch.setattr(bench, "left_by_person", person)
    t = threading.Thread(target=lambda: bench.recover(run.endpoint_id, hl.selected()))
    t.start()
    assert holding.wait(10)                             # put back; its row not closed yet
    assert fake.settings() == (2, (2, 1, 1, 1), (2, 0, 0, 0), -3.0)
    out = {}
    changing = threading.Thread(target=lambda: out.update(hb.change_dsp(
        {"filter": 1, "filter1x": 1})))                 # the owner's, after it
    changing.start()
    lock = hl.selected().lock
    assert _wait(lambda: len(lock._waiting) > 0 or asked.is_set(), 5)
    go.set()
    t.join(10)
    changing.join(10)
    assert out.get("ok"), out
    row = run.q("SELECT note, left_at IS NULL FROM hqp_benchmark_runs WHERE id = %s", run_id)
    assert row[0][1], row


def test_a_run_the_owner_changed_is_closed_with_no_notice_as_a_new_run_starts(run, monkeypatch):
    """A run cut short whose put-back a knob the owner set in HQPlayer's
    window supersedes, taken by a new run's first step (its try at the start
    met HQPlayer away): closed as theirs with no notice — the channel never
    woken for it — as the new run reads what HQPlayer holds as the owner's
    now."""
    fake = run.fake
    run_id = _cut_short_run(run, last_rate=1, last_filter=1, last_filter1x=1, last_shaper=1)
    with fake._lock:                    # SDM on the run's last point, the rate the owner's own
        fake.sel[2] = [0, 1, 1, 1]
    monkeypatch.setattr(bench, "recover", lambda endpoint_id, link, **kw: None)
    woken, notify = [], bench._notify
    monkeypatch.setattr(bench, "_notify", lambda: (woken.append(True), notify())[1])
    bench.start()
    assert _wait(lambda: not bench.running(), 60)
    assert bench.state()["outcome"] == "done", bench.state()["note"]
    row = run.q("SELECT note, left_at IS NULL FROM hqp_benchmark_runs WHERE id = %s", run_id)
    assert "changed in HQPlayer since" in row[0][0] and row[0][1], row
    assert woken == [], "the notices channel was woken for a run the new one took"


# -- A run lost or cut short: who may put it back, and how it is said ----------------


# What a put-back changes on HQPlayer
_CHANGES = ("Stop", "SetMode", "SetRate", "SetFilter", "SetShaping", "Volume", "PlaylistClear",
            "PlaylistAdd")


def _changes(fake, mark):
    return [c for c, _ in fake.commands[mark:] if c in _CHANGES]


def _another_controller_plays(fake):
    """Another controller plays through HQPlayer, in PCM on a modulator and a
    volume of the owner's: (the settings it plays on, the command log's mark)."""
    with fake._lock:
        fake.playlist = ["http://other-controller.invalid/track.flac"]
        fake.track, fake.state, fake.position = 1, int(PlaybackState.PLAYING), 30.0
        fake.mode, fake.sel[1], fake.volume = 1, [0, 0, 0, 1], -10.0
        return fake.settings(), len(fake.commands)


def _lost_run_node_stopped(run, monkeypatch):
    """A run HQPlayer went away under (powered off in the middle of a point),
    the node then stopped — its backend detached, no switch away — before
    HQPlayer is back: the run stays owed, lost_at kept. Returns the settings
    before the run."""
    fake, mgr = run.fake, run.mgr
    fake.jitter = 0.2
    monkeypatch.setattr(bench, "SETTLE_CAP_S", 1.0)
    before = fake.settings()
    bench.start()
    assert _wait(lambda: run.point() >= 3, 20)
    fake.close()
    assert _wait(lambda: not bench.running(), 30)
    assert _wait(lambda: mgr.held is None, 20)
    assert run.q("SELECT finished_at IS NULL, lost_at IS NOT NULL FROM hqp_benchmark_runs") == \
        [(True, True)]
    mgr.active.shutdown()
    mgr._active = None
    fake.come_back()
    return before


def _node_starts(run):
    """The node starts again: its attach puts back what is owed first."""
    b = HqpBackend(emit=run.mgr._on_backend_status, queue=run.mgr.queue)
    run.mgr._active = b
    b.start()
    assert _wait(lambda: run.q(
        "SELECT finished_at IS NOT NULL FROM hqp_benchmark_runs") == [(True,)], 20)
    return b


def test_a_lost_run_met_at_the_next_attach_leaves_another_controller_playing(run, monkeypatch):
    """A run HQPlayer went away under; while the node is stopped HQPlayer
    boots and another controller plays through it on settings of the
    owner's. The node's attach finds more than a box that just booted shows
    — HQPlayer plays an entry not its own — so the run is judged as any run
    cut short: nothing stopped, nothing reset, the run left to the owner."""
    fake = run.fake
    _lost_run_node_stopped(run, monkeypatch)
    owners, mark = _another_controller_plays(fake)
    _node_starts(run)
    assert _changes(fake, mark) == []
    assert fake.settings() == owners and fake.state == int(PlaybackState.PLAYING)
    assert run.q("SELECT left_at IS NOT NULL, restored_at IS NULL FROM hqp_benchmark_runs") == \
        [(True, True)]


def test_a_lost_run_met_by_the_poller_leaves_another_controller_playing(run, monkeypatch,
                                                                        request):
    """The node stays up: HQPlayer goes away mid-run, comes back, and another
    controller plays through it before the poller's put-back — the same
    judgement: nothing stopped, nothing reset."""
    fake, mgr = run.fake, run.mgr
    fake.jitter = 0.2
    monkeypatch.setattr(bench, "SETTLE_CAP_S", 1.0)
    asked, go = threading.Event(), threading.Event()
    request.addfinalizer(go.set)
    recover = hb.HqpBackend._recover
    monkeypatch.setattr(hb.HqpBackend, "_recover",
                        lambda self: (asked.set(), go.wait(15), recover(self))[2])
    bench.start()
    assert _wait(lambda: run.point() >= 3, 20)
    fake.close()
    assert _wait(lambda: not bench.running(), 30)
    asked.clear()
    fake.come_back()
    owners, mark = _another_controller_plays(fake)
    assert _wait(lambda: (mgr.active.poke(), asked.is_set())[1], 20)   # the poller's first answer
    go.set()
    assert _wait(lambda: (mgr.active.poke(), run.q(
        "SELECT finished_at IS NOT NULL FROM hqp_benchmark_runs") == [(True,)])[1], 20)
    assert _changes(fake, mark) == []
    assert fake.settings() == owners and fake.state == int(PlaybackState.PLAYING)


def test_a_lost_run_back_playing_the_nodes_own_queue_is_judged_as_any(run):
    """HQPlayer back and already playing an entry this node gave it — the
    owner pressed Play before the put-back went: more than a box that just
    booted shows, so the run is judged as any run cut short — the rate and
    the volume it plays at are the owner's, and the music plays on."""
    import datetime
    fake = run.fake
    assert _wait(lambda: len(fake.playlist) == 2, 10)
    run_id = _cut_short_run(run, last_rate=1, last_filter=1, last_filter1x=1, last_shaper=1,
                            lost_at=datetime.datetime.now(datetime.timezone.utc))
    with fake._lock:
        fake.track, fake.state, fake.position = 1, int(PlaybackState.PLAYING), 30.0
        fake.sel[2], fake.volume = [0, 1, 1, 1], -20.0
        owners, mark = fake.settings(), len(fake.commands)
    bench._owe(run_id, hl.selected())
    bench.recover(run.endpoint_id, hl.selected(), gave=run.mgr.active.gave)
    assert _closed_as_left(run, run_id)
    assert _changes(fake, mark) == [] and fake.state == int(PlaybackState.PLAYING)
    assert fake.settings() == owners


def test_a_lost_run_back_stopped_on_another_controllers_entry_is_judged_as_any(run):
    """HQPlayer back stopped, reading an entry another controller loaded:
    more than a box that just booted shows. The run is judged as any run cut
    short — the modulator the owner chose stands, nothing is sent but the
    volume, still at the run's level."""
    import datetime
    fake = run.fake
    run_id = _cut_short_run(run, last_rate=1, last_filter=1, last_filter1x=1, last_shaper=1,
                            lost_at=datetime.datetime.now(datetime.timezone.utc))
    with fake._lock:
        fake.playlist = ["http://other-controller.invalid/track.flac"]
        fake.track, fake.state = 1, int(PlaybackState.STOPPED)
        fake.sel[2], fake.volume = [1, 1, 1, 2], -60.0
        owners, mark = fake.settings(), len(fake.commands)
    bench._owe(run_id, hl.selected())
    bench.recover(run.endpoint_id, hl.selected(), gave=run.mgr.active.gave)
    assert _closed_as_left(run, run_id)
    assert fake.settings() == owners[:3] + (-3.0,) and _changes(fake, mark) == ["Volume"]


def test_a_lost_run_back_stopped_on_an_entry_it_does_not_list_is_judged_as_any(run, monkeypatch):
    """HQPlayer back stopped, its Status naming an entry, its playlist read
    refused: an entry the read does not show is no sign of a boot — the run
    is judged as any run cut short: the owner's modulator stands, and only
    the volume, still at the run's level, goes back."""
    import datetime
    fake = run.fake
    run_id = _cut_short_run(run, last_rate=1, last_filter=1, last_filter1x=1, last_shaper=1,
                            lost_at=datetime.datetime.now(datetime.timezone.utc))
    dispatch = fake._dispatch
    monkeypatch.setattr(fake, "_dispatch", lambda cmd, attrs: (
        '<PlaylistGet result="Error">busy</PlaylistGet>' if cmd == "PlaylistGet"
        else dispatch(cmd, attrs)))
    with fake._lock:
        fake.track, fake.state = 1, int(PlaybackState.STOPPED)
        fake.sel[2], fake.volume = [1, 1, 1, 2], -60.0
        owners, mark = fake.settings(), len(fake.commands)
    bench._owe(run_id, hl.selected())
    bench.recover(run.endpoint_id, hl.selected(), gave=run.mgr.active.gave)
    assert _closed_as_left(run, run_id)
    assert fake.settings() == owners[:3] + (-3.0,) and _changes(fake, mark) == ["Volume"]


def test_a_lost_run_back_stopped_on_the_nodes_own_entry_is_put_back_whole(run):
    """HQPlayer back stopped on an entry this node gave it (the queue was
    handed to it again before the put-back went): no other controller is
    there — the run is put back whole, over what HQPlayer booted with."""
    import datetime
    fake = run.fake
    assert _wait(lambda: len(fake.playlist) == 2, 10)              # the queue as the attach handed it
    run_id = _cut_short_run(run, last_rate=1, last_filter=1, last_filter1x=1, last_shaper=1,
                            lost_at=datetime.datetime.now(datetime.timezone.utc))
    with fake._lock:
        fake.track, fake.state = 1, int(PlaybackState.STOPPED)
        fake.sel[2], fake.volume = [0, 1, 1, 1], -60.0             # booted with a rate of no one's
    bench._owe(run_id, hl.selected())
    bench.recover(run.endpoint_id, hl.selected(), gave=run.mgr.active.gave)
    assert run.q("SELECT restored_at IS NOT NULL FROM hqp_benchmark_runs WHERE id = %s",
                 run_id) == [(True,)]
    assert fake.settings() == (2, (2, 1, 1, 1), (2, 0, 0, 0), -3.0)


def test_a_lost_run_back_on_the_queue_it_saved_is_put_back_whole_by_the_poller(run, monkeypatch):
    """HQPlayer went away mid-run and boots stopped on a playlist it saved —
    the queue this node gave it — with a rate of its own: no other
    controller's entry, so the poller's put-back goes whole."""
    fake, mgr = run.fake, run.mgr
    assert _wait(lambda: len(fake.playlist) == 2, 10)
    handed = list(fake.playlist)
    fake.jitter = 0.2
    monkeypatch.setattr(bench, "SETTLE_CAP_S", 1.0)
    before = fake.settings()
    bench.start()
    assert _wait(lambda: run.point() >= 3, 20)
    fake.close()
    assert _wait(lambda: not bench.running(), 30)
    with fake._lock:                    # what it boots with
        fake.playlist, fake.track = handed, 1
        fake.sel[2][0] = 0
    fake.come_back()
    assert _wait(lambda: (mgr.active.poke(), run.q(
        "SELECT finished_at IS NOT NULL FROM hqp_benchmark_runs") == [(True,)])[1], 30)
    assert fake.settings() == before


def test_a_lost_run_back_stopped_after_the_node_restarted_is_put_back_whole_and_dated(
        run, monkeypatch):
    """The node stops while HQPlayer is away; HQPlayer boots in the mode it
    saved and is back stopped, its playlist empty, when the node starts
    again: what a box that just booted shows — the run is put back whole, and
    the notice says so with the run's date: a put-back days later must not
    read as a change made on its own."""
    fake = run.fake
    fake.boot_mode = 1
    before = _lost_run_node_stopped(run, monkeypatch)
    _node_starts(run)
    assert fake.settings() == before
    started = run.q("SELECT started_at FROM hqp_benchmark_runs")[0][0]
    restored = _run_restored()
    assert restored["data"] == {"run_at": started.isoformat(), "volume_left": None,
                                "volume_before": -3.0}, restored


def test_a_run_cancelled_in_a_silent_build_is_no_run_lost(run, monkeypatch, request):
    """A cancel lands in a build HQPlayer is silent through: its put-back
    goes unanswered — HQPlayer busy, not gone (it closed, reset or refused
    nothing) — so the run is no run lost. The owner then sets a filter in
    HQPlayer's own window: their change stands, and no notice speaks of a
    lost connection; the volume, still at the run's level, goes back, and
    with it the last of the run the notice would name."""
    fake, mgr = run.fake, run.mgr
    monkeypatch.setattr(bench, "ANSWER_WAIT_S", 30.0)
    monkeypatch.setattr(bench, "RESTORE_ANSWER_S", 0.3)
    asked, go = threading.Event(), threading.Event()
    request.addfinalizer(go.set)
    recover = hb.HqpBackend._recover
    monkeypatch.setattr(hb.HqpBackend, "_recover",
                        lambda self: (asked.set(), go.wait(15), recover(self))[2])
    monkeypatch.setattr(DspFake, "FILTERS", DspFake.FILTERS + ["poly-sinc-ext2"])
    fake.builds[("sinc-L", 11289600)] = 3.0
    bench.start()
    assert _wait(lambda: fake._silent_until > time.monotonic(), 30)
    bench.cancel()
    assert _wait(lambda: not bench.running(), 10)
    assert run.q("SELECT finished_at IS NULL, lost_at IS NULL FROM hqp_benchmark_runs") == \
        [(True, True)]
    assert _wait(lambda: fake._silent_until <= time.monotonic(), 10)
    with fake._lock:                                # the owner's filter, in HQPlayer's window
        fake.sel[2][1] = fake.sel[2][2] = 2
        owners = fake.settings()
    assert _wait(lambda: asked.is_set(), 20)
    go.set()
    assert _wait(lambda: (mgr.active.poke(), run.q(
        "SELECT finished_at IS NOT NULL FROM hqp_benchmark_runs") == [(True,)])[1], 20)
    assert fake.settings()[:3] == owners[:3] and fake.volume == -3.0
    assert run.q("SELECT note LIKE '%%changed in HQPlayer since%%' FROM hqp_benchmark_runs") \
        == [(True,)]
    assert _wait(lambda: (mgr.active.poke(), run.q(
        "SELECT left_at IS NULL FROM hqp_benchmark_runs") == [(True,)])[1], 10)
    assert _run_restored() is None


def test_a_lost_run_that_switched_modes_back_in_the_mode_it_found_is_put_back_whole(run):
    """A "Measure PCM" run from SDM lost, its PCM selection on the run's last
    point; HQPlayer boots in the mode the run found (SDM). HQPlayer keeps one
    selection per mode: the whole put-back goes into PCM for its own
    selection, then back to SDM and its own — as the run's own end does."""
    import datetime
    fake = run.fake
    _cut_short_run(run, mode="PCM", own_mode=1, own_rate=2, own_filter=1, own_filter1x=1,
                   own_shaper=1, last_rate=1, last_filter=0, last_filter1x=0, last_shaper=0,
                   lost_at=datetime.datetime.now(datetime.timezone.utc))
    with fake._lock:
        fake.mode, fake.sel[1], fake.volume = 2, [1, 0, 0, 0], -60.0
        fake.state, fake.track = int(PlaybackState.STOPPED), 0
    bench.recover(run.endpoint_id, hl.selected(), gave=run.mgr.active.gave)
    assert fake.settings() == (2, (2, 1, 1, 1), (2, 0, 0, 0), -3.0)


def test_a_restored_notice_once_seen_stays_gone_at_a_later_visit(run, monkeypatch):
    """The restored notice is seen and dropped; later another notice arrives
    and the owner visits the rows again: the report stays seen — the seen
    map is written from what the notices derive, not from what is served."""
    from routers import settings as settings_router
    _gone_mid_run(run, monkeypatch, boot_mode=1)
    store = {"output.type": "hqplayer"}
    monkeypatch.setattr(settings_router, "_read", store.get)
    monkeypatch.setattr(settings_router, "_write", store.__setitem__)
    monkeypatch.setattr(settings_router, "_guidance_state", lambda: {})
    monkeypatch.setattr(settings_router, "notify_library_subscribers", lambda: None)
    settings_router.post_guidance_seen("notices")
    assert _run_restored() is None
    run.q("""INSERT INTO external_api_cooldown (source, strikes, updated_at, cooldown_until, reason)
             VALUES ('lastfm', 1, now(), now() + interval '1 hour', 'test') RETURNING source""")
    settings_router.post_guidance_seen("notices")
    assert _run_restored() is None


def test_a_run_put_back_at_a_new_runs_start_is_closed_with_no_notice(run, monkeypatch):
    """A run cut short whose put-back a knob the owner set in HQPlayer's
    window supersedes, met by a new run's start (the poller's put-back not
    there yet): closed as theirs with no notice — the new run reads what
    HQPlayer holds as the owner's from now."""
    fake = run.fake
    run_id = _cut_short_run(run, last_rate=1, last_filter=1, last_filter1x=1, last_shaper=1)
    with fake._lock:
        fake.sel[2] = [0, 1, 1, 1]
    monkeypatch.setattr(hb.HqpBackend, "_recover", lambda self: None)
    woken, notify = [], bench._notify
    monkeypatch.setattr(bench, "_notify", lambda: (woken.append(True), notify())[1])
    bench.start()
    assert _wait(lambda: not bench.running(), 60)
    assert bench.state()["outcome"] == "done", bench.state()["note"]
    row = run.q("SELECT note, left_at IS NULL FROM hqp_benchmark_runs WHERE id = %s", run_id)
    assert "changed in HQPlayer since" in row[0][0] and row[0][1], row
    assert woken == [], "the notices channel was woken for a run the new one took"


def test_a_lost_run_put_back_at_a_new_runs_start_is_closed_with_no_notice(run, monkeypatch):
    """... and a lost run put back whole there: no notice raised for the new
    run to end a moment later."""
    import datetime
    fake = run.fake
    _cut_short_run(run, last_rate=1, last_filter=1, last_filter1x=1, last_shaper=1,
                   lost_at=datetime.datetime.now(datetime.timezone.utc))
    with fake._lock:
        fake.sel[2], fake.volume = [1, 1, 1, 1], -20.0
    monkeypatch.setattr(hb.HqpBackend, "_recover", lambda self: None)
    woken, notify = [], bench._notify
    monkeypatch.setattr(bench, "_notify", lambda: (woken.append(True), notify())[1])
    bench.start()
    assert _wait(lambda: not bench.running(), 60)
    assert bench.state()["outcome"] == "done", bench.state()["note"]
    assert woken == [], "the notices channel was woken for a run the new one took"


def test_a_switch_after_a_put_back_that_went_never_records_it_as_left(run, monkeypatch):
    """A switch away is accepted at once, without the command lock: one that
    comes after a put-back went, its row not closed yet, leaves the run to
    that put-back's own end — put back, never left."""
    fake, mgr = run.fake, run.mgr
    run_id = _owed_pcm_run(run)
    close = bench._close_owed
    holding, go = threading.Event(), threading.Event()

    def held(row, note, *args, **kw):
        if threading.current_thread().name == "hqp-benchmark-recover":
            holding.set()
            go.wait(10)
        return close(row, note, *args, **kw)
    monkeypatch.setattr(bench, "_close_owed", held)
    t = threading.Thread(target=lambda: bench.recover(run.endpoint_id, hl.selected()))
    t.start()
    assert holding.wait(10)
    assert fake.settings() == (2, (2, 1, 1, 1), (2, 0, 0, 0), -3.0)
    mgr.active.release()
    go.set()
    t.join(10)
    assert run.q("SELECT left_at IS NULL FROM hqp_benchmark_runs WHERE id = %s", run_id) == [(True,)]
    assert _run_left() is None


def test_another_hqplayer_chosen_while_a_put_back_goes_leaves_the_run_to_the_owner(
        run, monkeypatch):
    """The owner picks another HQPlayer while the put-back holds the command
    lock, between two of its requests: the put-back's own end decides — its
    next request is dropped, and the run is left to the owner."""
    fake = run.fake
    run_id = _owed_pcm_run(run)
    call = bench._OnLink.__call__
    holding, go = threading.Event(), threading.Event()

    def held(self, method, *args, **kwargs):
        if method == "apply_settings" and not holding.is_set():
            holding.set()
            go.wait(10)
        return call(self, method, *args, **kwargs)
    monkeypatch.setattr(bench._OnLink, "__call__", held)
    t = threading.Thread(target=lambda: bench.recover(run.endpoint_id, hl.selected()))
    t.start()
    assert holding.wait(10)
    link = hl.selected()
    link.retire("another HQPlayer was chosen")             # as _apply_hqplayer_address does
    bench.left_by_person("127.0.0.1", fake.port, "another HQPlayer was chosen")
    go.set()
    t.join(10)
    assert _closed_as_left(run, run_id)


def _recover_threads_done():
    return not any(t.name == "hqp-benchmark-recover" and t.is_alive()
                   for t in threading.enumerate())


def _another_hqplayer_chosen(run, monkeypatch):
    """The owner picks another HQPlayer: the old link retires, the backend
    attached to it is detached (as PUT /hqplayer does)."""
    from routers import settings as settings_router
    monkeypatch.setattr(settings_router, "_write", lambda key, value: None)
    settings_router._apply_hqplayer_address("127.0.0.1", 1)
    run.mgr.activate(None, stop_old=False)


def test_another_hqplayer_chosen_in_a_put_backs_build_leaves_the_run_for_good(run, monkeypatch):
    """The owed put-back sends a setting HQPlayer builds in silence; the owner
    picks another HQPlayer meanwhile. The switch wakes the put-back's request
    on its way — it ends on a connection let go, not on a next request
    dropped — and the run is left to the owner in its row: after a restart,
    picked and answering again, HQPlayer keeps what it holds, its volume
    never raised for them."""
    fake = run.fake
    run_id = _owed_pcm_run(run)
    fake.build("SetMode", 3.0, answer="last")       # the way back into SDM builds
    link = hl.selected()
    bench.recover(run.endpoint_id, link)            # returns once the build began
    assert link.build.silent() is not None
    _another_hqplayer_chosen(run, monkeypatch)
    assert _wait(_recover_threads_done, 10)
    assert _closed_as_left(run, run_id)
    assert _wait(lambda: fake._silent_until <= time.monotonic(), 10)
    monkeypatch.setattr(bench, "_owed", {})         # a restart
    _fresh_links()
    monkeypatch.setattr(settings, "hqplayer_host", "127.0.0.1")
    monkeypatch.setattr(settings, "hqplayer_port", fake.port)          # picked again
    held = fake.settings()
    bench.recover(run.endpoint_id, hl.selected())
    assert fake.settings() == held


def test_another_hqplayer_chosen_in_a_put_backs_stop_leaves_the_run(run, monkeypatch):
    """The same with no build: the put-back's Stop is answered only once
    HQPlayer's output is torn down, and the owner picks another HQPlayer
    meanwhile — the run is left to them in its row."""
    fake = run.fake
    run_id = _owed_pcm_run(run)
    fake.service["Stop"] = 2.0
    link = hl.selected()
    t = threading.Thread(target=lambda: bench.recover(run.endpoint_id, link))
    t.start()
    assert _wait(lambda: link.teardown == "Stop", 10)
    _another_hqplayer_chosen(run, monkeypatch)
    t.join(10)
    assert _wait(_recover_threads_done, 10)
    assert _closed_as_left(run, run_id)


def test_another_hqplayer_chosen_while_a_put_back_reads_no_state_leaves_the_run(run, monkeypatch):
    """The owed put-back's reads come back with no state of HQPlayer's (an
    answer it could not read) after the owner picked another HQPlayer: that
    end too is the switch's — the run is left to them in its row."""
    run_id = _owed_pcm_run(run)
    link = hl.selected()
    owed_on, item = bench._owed[run_id]
    entered = threading.Event()

    def reads_nothing(on, r, gave=None):
        entered.set()
        assert _wait(lambda: owed_on.superseded(item) is not None, 10)
        return None
    monkeypatch.setattr(bench, "_recover", reads_nothing)
    t = threading.Thread(target=lambda: bench.recover(run.endpoint_id, link))
    t.start()
    assert entered.wait(10)
    _another_hqplayer_chosen(run, monkeypatch)
    t.join(10)
    assert _wait(_recover_threads_done, 10)
    assert _closed_as_left(run, run_id)


def test_a_put_back_of_a_run_cut_short_never_resets_a_mode_it_did_not_read(run):
    """A "Measure PCM" run from SDM cut short by the node's own stop. In
    HQPlayer's window the owner set PCM's rate to their own and went back to
    SDM, the volume still at the run's level. HQPlayer answers in SDM, at
    rest: the put-back reads PCM before it judges it, finds the owner's rate
    there and leaves the run to them — PCM keeps their rate, and the volume,
    at the run's level, goes back."""
    fake = run.fake
    run_id = _cut_short_run(run, mode="PCM", own_mode=1, own_rate=2, own_filter=1,
                            own_filter1x=1, own_shaper=1, last_rate=1, last_filter=0,
                            last_filter1x=0, last_shaper=0)
    with fake._lock:
        fake.mode, fake.sel[1], fake.volume = 2, [0, 0, 0, 0], -60.0
    bench._owe(run_id, hl.selected())
    bench.recover(run.endpoint_id, hl.selected())
    assert _wait(lambda: run.q("SELECT finished_at IS NOT NULL FROM hqp_benchmark_runs "
                               "WHERE id = %s", run_id) == [(True,)])
    assert fake.sel[1][0] == 0 and fake.volume == -3.0, fake.settings()
    assert _closed_as_left(run, run_id)


def test_a_new_runs_first_step_puts_a_lost_run_back_whole_on_the_nodes_entry(run, monkeypatch):
    """A lost run; HQPlayer back stopped on an entry of the node's own queue,
    booted with a rate of no one's — what a box that just booted shows. The
    start's own try met HQPlayer away, so the new run's first step takes it:
    whole, as the attach would — the new run then reads the owner's rate and
    volume, never the run's leftovers."""
    import datetime
    fake = run.fake
    assert _wait(lambda: len(fake.playlist) == 2, 10)
    run_id = _cut_short_run(run, last_rate=1, last_filter=1, last_filter1x=1, last_shaper=1,
                            lost_at=datetime.datetime.now(datetime.timezone.utc))
    with fake._lock:
        fake.track, fake.state = 1, int(PlaybackState.STOPPED)
        fake.sel[2], fake.volume = [0, 1, 1, 1], -60.0
    monkeypatch.setattr(bench, "recover", lambda endpoint_id, link, **kw: None)
    bench.start(fixed_volume_ok=True)
    assert _wait(lambda: not bench.running(), 60)
    assert run.q("SELECT pre_rate, pre_volume FROM hqp_benchmark_runs WHERE id > %s",
                 run_id) == [(2, -3.0)]


def _restarted_then_silent_as_the_put_back_begins(monkeypatch, fake):
    """HQPlayer restarts as the run's put-back begins (the connection closed)
    and then answers nothing for a while."""
    put_back, began = bench._put_back, threading.Event()

    def restarted_then_silent(link, **kw):
        if not began.is_set():
            began.set()
            fake.restart()
            fake.silence(2.0)
        return put_back(link, **kw)
    monkeypatch.setattr(bench, "_put_back", restarted_then_silent)


def test_a_put_back_that_met_a_restart_then_a_silence_marks_the_run_lost(run, monkeypatch):
    """The run's put-back finds the connection closed by HQPlayer (it
    restarted), connects again and then meets a silence: HQPlayer went away
    under the put-back all the same — the run is lost, as for the closed
    connection alone."""
    fake = run.fake
    fake.jitter = 0.2
    monkeypatch.setattr(bench, "SETTLE_CAP_S", 2.0)
    monkeypatch.setattr(bench, "RESTORE_ANSWER_S", 0.3)
    _restarted_then_silent_as_the_put_back_begins(monkeypatch, fake)
    monkeypatch.setattr(hb.HqpBackend, "_recover", lambda self: None)  # nothing at the re-attach
    bench.start()
    assert _wait(lambda: run.point() >= 2, 20)
    bench.cancel()
    assert _wait(lambda: not bench.running(), 30)
    assert run.q("SELECT lost_at IS NOT NULL FROM hqp_benchmark_runs") == [(True,)]


def test_a_restart_the_put_back_met_puts_the_run_back_whole(run, monkeypatch):
    """... and what follows: HQPlayer restarted into the mode it saved as the
    put-back began, then was silent a moment. Answering again it shows what
    it booted with — the run goes back whole, never left to the owner over a
    mode no one chose."""
    fake = run.fake
    fake.jitter = 0.2
    monkeypatch.setattr(bench, "SETTLE_CAP_S", 2.0)
    monkeypatch.setattr(bench, "RESTORE_ANSWER_S", 0.3)
    before = fake.settings()
    fake.boot_mode = 1
    _restarted_then_silent_as_the_put_back_begins(monkeypatch, fake)
    bench.start()
    assert _wait(lambda: run.point() >= 2, 20)
    bench.cancel()
    assert _wait(lambda: not bench.running(), 30)
    assert _wait(lambda: (run.mgr.active is not None and run.mgr.active.poke(), run.q(
        "SELECT finished_at IS NOT NULL FROM hqp_benchmark_runs") == [(True,)])[1], 30)
    assert fake.settings() == before


def test_a_lost_run_back_stopped_on_its_own_dsd_source_is_put_back_whole(run, tmp_path,
                                                                        monkeypatch):
    """A run that measured the owner's DSD file, lost; HQPlayer boots stopped
    on the playlist the run gave it, on the DSD entry, with a rate of no
    one's. That entry is the run's own, no other controller's: whole."""
    import datetime
    fake = run.fake
    dsd = tmp_path / "owner.dsf"
    dsd.touch()
    f = {"file_path": dsd.as_posix(), "format": "DSF", "sample_rate": 2822400, "channels": 2}
    monkeypatch.setattr(bench, "_dsd_file", lambda: f)
    signal = bench._signal_uri(bench.ensure_signal(44100))
    source = bench._dsd_uri(f)
    run_id = _cut_short_run(run, last_rate=1, last_filter=1, last_filter1x=1, last_shaper=1,
                            lost_at=datetime.datetime.now(datetime.timezone.utc))
    with fake._lock:
        fake.playlist, fake.track, fake.state = [signal, source], 2, int(PlaybackState.STOPPED)
        fake.sel[2], fake.volume = [0, 1, 1, 1], -60.0
    bench._owe(run_id, hl.selected())
    bench.recover(run.endpoint_id, hl.selected())
    assert run.q("SELECT restored_at IS NOT NULL FROM hqp_benchmark_runs WHERE id = %s",
                 run_id) == [(True,)]
    assert fake.settings() == (2, (2, 1, 1, 1), (2, 0, 0, 0), -3.0)


def test_a_streamed_hqplayer_back_on_the_queue_after_the_node_restarted_goes_back_whole(
        run, monkeypatch):
    """An HQPlayer that gets streams, lost under a run while the node was
    down; it boots stopped on the queue this node had handed it (its saved
    playlist) with a rate of no one's. The node starts again: its attach puts
    back before it mirrors — no file registered with the media proxy yet, no
    hand-over in the trace — and the entries at the node's own media proxy
    are the node's all the same: whole."""
    from test_hqp_backend import _fresh_trace
    fake = run.fake
    assert _wait(lambda: len(fake.playlist) == 2, 10)
    handed = list(fake.playlist)                    # the queue as the attach handed it: streams
    before = _lost_run_node_stopped(run, monkeypatch)
    with fake._lock:                                # what it boots with: its saved playlist
        fake.playlist, fake.track, fake.state = handed, 1, int(PlaybackState.STOPPED)
        fake.sel[2][0] = 0
    proxy = streaming_service.get_proxy()           # the node's process starts afresh
    with proxy._lock:
        proxy._files.clear()
    _fresh_trace()
    _node_starts(run)
    assert fake.settings() == before


@pytest.mark.parametrize("unserved", ["accepted", "queued"])
def test_a_restart_drops_the_put_backs_connection_it_has_not_served(run, monkeypatch, unserved):
    """The cancel woke the run's read, the run connected again at once, and
    HQPlayer restarted before it served that connection — accepted, its
    handler not run yet, or still in the kernel's queue: a restart resets
    every connection the box had, so the put-back meets the loss and the
    run is marked lost, never judged by the silence after."""
    fake = run.fake
    fake.jitter = 0.2
    monkeypatch.setattr(bench, "SETTLE_CAP_S", 2.0)
    monkeypatch.setattr(bench, "RESTORE_ANSWER_S", 0.3)
    hold, held = threading.Event(), threading.Event()

    def later(step):
        def waits(*a):
            if hold.is_set():
                hold.clear()
                held.wait(5)
            return step(*a)
        return waits
    if unserved == "accepted":
        monkeypatch.setattr(fake._handler, "handle", later(fake._handler.handle))
    else:
        monkeypatch.setattr(fake._srv, "get_request", later(fake._srv.get_request))
    put_back, began = bench._put_back, threading.Event()

    def new_connection_then_restart(link, **kw):
        if not began.is_set():
            began.set()
            hold.set()
            link.c.disconnect()
            assert link.c.connect()         # as the cancel's reconnect leaves it
            fake.restart()
            fake.silence(2.0)
            held.set()
        return put_back(link, **kw)
    monkeypatch.setattr(bench, "_put_back", new_connection_then_restart)
    monkeypatch.setattr(hb.HqpBackend, "_recover", lambda self: None)
    bench.start()
    assert _wait(lambda: run.point() >= 2, 20)
    bench.cancel()
    assert _wait(lambda: not bench.running(), 30)
    assert run.q("SELECT lost_at IS NOT NULL FROM hqp_benchmark_runs") == [(True,)]


def _a_dsd_listen(run, path, *, source: str, at) -> list:
    """A DSD file of the owner's library and a listen of it — one the node
    made ('sautium'), or one a Last.fm import placed ('lastfm'). Returns
    what to delete again: the module's database outlives the test."""
    import uuid
    album, track = str(uuid.uuid4()), str(uuid.uuid4())
    run.q("INSERT INTO albums (id, title) VALUES (%s, 'D') RETURNING id", album)
    variant = run.q("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, %s) "
                    "RETURNING id", album, path.parent.as_posix())[0][0]
    run.q("INSERT INTO tracks (id, title) VALUES (%s, 'd') RETURNING id", track)
    mf = run.q("""INSERT INTO media_files (track_id, album_variant_id, file_path, file_format,
                                          sample_rate, channels)
                  VALUES (%s, %s, %s, 'DSF', 2822400, 2) RETURNING id""",
               track, variant, path.as_posix())[0][0]
    run.q("INSERT INTO listening_history (track_id, media_file_id, started_at, source) "
          "VALUES (%s, %s, %s, %s) RETURNING id", track, mf, at, source)
    return [album, track]


def _forget(run, made: list) -> None:
    for album, track in made:
        run.q("DELETE FROM tracks WHERE id = %s RETURNING id", track)
        run.q("DELETE FROM albums WHERE id = %s RETURNING id", album)


@pytest.mark.parametrize("source", ["sautium", "lastfm"])
def test_a_lost_run_back_on_its_dsd_source_after_a_newer_dsd_listen_goes_back_whole(
        run, tmp_path, monkeypatch, source):
    """An HQPlayer on this machine (it opens files by path). A run that
    measured the owner's DSD file is lost; before HQPlayer answers again the
    owner's newest DSD listen is another file — a listen of the node's, or
    one a Last.fm import placed. HQPlayer boots stopped on the playlist the
    run gave it, on the run's DSD entry: that entry is the run's own, known
    by the URI the run handed — the run goes back whole."""
    import datetime
    fake = run.fake
    measured, later = tmp_path / "measured.dsf", tmp_path / "later.dsf"
    measured.touch()
    later.touch()
    monkeypatch.setattr(hb, "_stream_mode", lambda: False)      # HQPlayer on this machine
    now = datetime.datetime.now(datetime.timezone.utc)
    made = [_a_dsd_listen(run, measured, source="sautium", at=now - datetime.timedelta(hours=2))]
    try:
        f = bench._dsd_file()
        assert f is not None and f["file_path"] == measured.as_posix(), f
        signal = bench._signal_uri(bench.ensure_signal(44100))
        handed = bench._dsd_uri(f)                     # what the run handed HQPlayer: a path
        run_id = _cut_short_run(run, last_rate=1, last_filter=1, last_filter1x=1, last_shaper=1,
                                lost_at=now - datetime.timedelta(hours=1), dsd_uri=handed)
        made.append(_a_dsd_listen(run, later, source=source, at=now))     # newest now
        assert bench._dsd_file()["file_path"] == later.as_posix()
        with fake._lock:
            fake.playlist, fake.track, fake.state = [signal, handed], 2, int(PlaybackState.STOPPED)
            fake.sel[2], fake.volume = [0, 1, 1, 1], -60.0
        bench._owe(run_id, hl.selected())
        bench.recover(run.endpoint_id, hl.selected(), gave=run.mgr.active.gave)
        row = run.q("SELECT restored_at IS NOT NULL, left_at IS NOT NULL, note "
                    "FROM hqp_benchmark_runs WHERE id = %s", run_id)
        assert row[0][:2] == (True, False), row
        assert fake.settings() == (2, (2, 1, 1, 1), (2, 0, 0, 0), -3.0), fake.settings()
    finally:
        _forget(run, made)


def test_a_run_a_knob_of_the_owners_leaves_to_them_gives_back_a_volume_at_its_own_level(run):
    """A run the node's own stop cut short (not lost): HQPlayer shows a rate
    the owner set in its own window since, so the run is left to them — but
    its volume still stands at the level the run lowered it to. That level is
    the run's: it goes back to the owner's; their rate is left."""
    fake = run.fake
    run_id = _cut_short_run(run, mode="PCM", own_mode=1, own_rate=2, own_filter=1, own_filter1x=1,
                            own_shaper=1, last_rate=1, last_filter=0, last_filter1x=0,
                            last_shaper=0)
    with fake._lock:
        fake.mode, fake.sel[1], fake.volume = 1, [0, 0, 0, 0], -60.0   # the owner's rate
    bench._owe(run_id, hl.selected())
    bench.recover(run.endpoint_id, hl.selected())
    assert _wait(lambda: run.q("SELECT left_at IS NOT NULL FROM hqp_benchmark_runs "
                               "WHERE id = %s", run_id) == [(True,)])
    mode, pcm, sdm, volume = fake.settings()
    assert (mode, pcm, volume) == (1, (0, 0, 0, 0), -3.0), fake.settings()


def test_a_run_measuring_the_owners_dsd_file_keeps_the_uri_it_hands(run, tmp_path, monkeypatch):
    """A run that measures the owner's DSD file keeps, on its row, the URI it
    hands HQPlayer for it: what a later put-back knows the run's own entry
    by."""
    owner = tmp_path / "owner.dsf"
    owner.touch()
    f = {"file_path": owner.as_posix(), "format": "DSF", "sample_rate": 2822400, "channels": 2}
    monkeypatch.setattr(bench, "_dsd_file", lambda: f)
    bench.start("pcm")
    assert _wait(lambda: run.q("SELECT dsd_uri FROM hqp_benchmark_runs ORDER BY id DESC "
                               "LIMIT 1") == [(bench._dsd_uri(f),)], 20)
    bench.cancel()
    assert _wait(lambda: not bench.running(), 30)


def _pcm_run_found_in_sdm(r: Run, pcm: list, **kw) -> int:
    """A "Measure PCM" run from SDM that the node's own stop cut short:
    HQPlayer back in SDM, the mode the run found, on the owner's selection
    there, lowered — while PCM, which State does not show in SDM, holds
    `pcm`. The run's last point in PCM is (1, 0, 0, 0), the owner's own
    there (2, 1, 1, 1)."""
    run_id = _cut_short_run(r, mode="PCM", own_mode=1, own_rate=2, own_filter=1, own_filter1x=1,
                            own_shaper=1, last_rate=1, last_filter=0, last_filter1x=0,
                            last_shaper=0, **kw)
    with r.fake._lock:
        r.fake.mode, r.fake.sel[1], r.fake.sel[2] = 2, list(pcm), [2, 0, 0, 0]
        r.fake.volume = -60.0
    return run_id


def _unchecked(r: Run, run_id: int) -> bool:
    return r.q("SELECT measured_unchecked_at IS NOT NULL FROM hqp_benchmark_runs WHERE id = %s",
               run_id) == [(True,)]


def test_a_put_back_on_a_resting_hqplayer_puts_the_measured_modes_test_point_back(run):
    """HQPlayer stopped, reading nothing of anyone's: the put-back enters
    PCM, finds the run's last point there and puts the owner's selection
    back, then returns to SDM, whose selection the round trip left as it
    was — and the rest goes back as before."""
    fake = run.fake
    run_id = _pcm_run_found_in_sdm(run, [1, 0, 0, 0])
    bench._owe(run_id, hl.selected())
    bench.recover(run.endpoint_id, hl.selected())
    assert _wait(lambda: run.q("SELECT finished_at IS NOT NULL FROM hqp_benchmark_runs "
                               "WHERE id = %s", run_id) == [(True,)])
    assert fake.settings() == (2, (2, 1, 1, 1), (2, 0, 0, 0), -3.0), fake.settings()
    assert not _closed_as_left(run, run_id) and not _unchecked(run, run_id)


def test_a_put_back_on_a_resting_hqplayer_leaves_a_measured_mode_the_owner_changed(run):
    """The same, PCM's rate the owner's since: the run is left to them —
    PCM as it is, HQPlayer back in SDM — and the notice names what PCM still
    holds of the run's test point, as read there; the volume, still at the
    run's level, goes back."""
    fake = run.fake
    run_id = _pcm_run_found_in_sdm(run, [0, 0, 0, 0])
    bench._owe(run_id, hl.selected())
    bench.recover(run.endpoint_id, hl.selected())
    assert _wait(lambda: _closed_as_left(run, run_id))
    assert fake.settings() == (2, (0, 0, 0, 0), (2, 0, 0, 0), -3.0), fake.settings()

    def notice():
        run.mgr.active.poke()
        return _run_left_items()
    elsewhere = {"what": "elsewhere", "mode": "PCM", "knobs": ["filter", "filter1x", "shaper"]}
    assert _wait(lambda: notice() == {"elsewhere": elsewhere}, 10), notice()


def test_a_put_back_on_a_busy_hqplayer_leaves_the_measured_mode_unchecked_until_it_rests(
        run, monkeypatch):
    """HQPlayer plays another controller's entry: the put-back sends no
    SetMode — it would restart HQPlayer's output — marks the run, and the
    notice says the mode is not checked yet. Stopped on that entry it is
    still another's; once HQPlayer rests on nothing of anyone's, the mode is
    judged: the run's last point goes back to the owner's, and the notice
    ends."""
    fake, mgr = run.fake, run.mgr
    run_id = _pcm_run_found_in_sdm(run, [1, 0, 0, 0])
    with fake._lock:
        fake.playlist, fake.track = ["file:///Elsewhere/theirs.flac"], 1
        fake.state = int(PlaybackState.PLAYING)
    n = len(fake.commands)
    bench._owe(run_id, hl.selected())
    bench.recover(run.endpoint_id, hl.selected())
    assert _wait(lambda: _unchecked(run, run_id))
    assert "SetMode" not in [c for c, _ in fake.commands[n:]], fake.commands[n:]
    assert fake.settings() == (2, (1, 0, 0, 0), (2, 0, 0, 0), -3.0), fake.settings()
    assert bench.unchecked_notice() is not None
    assert _wait(lambda: run.fake.state == int(PlaybackState.STOPPED))
    assert _wait(lambda: run.endpoint_id in bench._busy_at, 10)   # stopped on another's entry
    judged = []
    booted = bench._booted
    monkeypatch.setattr(bench, "_booted", lambda *a: (judged.append(a), booted(*a))[1])
    k = len(fake.arrivals)
    for _ in range(3):
        mgr.active.poke()
        assert _wait(lambda: [c for _, c in fake.arrivals[k:]].count("Status") >= 1)
        k = len(fake.arrivals)
    assert not _wait(lambda: judged, timeout=1.0), judged   # the same rest, not judged again
    assert _unchecked(run, run_id)
    with fake._lock:
        fake.playlist, fake.track = [], 0       # the other controller's entry gone
    assert _wait(lambda: (mgr.active.poke(), not _unchecked(run, run_id))[1], 10)
    assert fake.settings() == (2, (2, 1, 1, 1), (2, 0, 0, 0), -3.0), fake.settings()
    assert bench.unchecked_notice() is None
    assert _wait(lambda: not bench._checking, 5)
    assert run.endpoint_id not in bench._busy_at        # a verdict leaves no try behind


def _set_modes(monkeypatch, fake) -> list:
    """(value, the transport HQPlayer was in) of every SetMode it carries out."""
    seen, dispatch = [], fake._dispatch

    def log(cmd, attrs):
        if cmd == "SetMode":
            seen.append((attrs.get("value"), fake.state))
        return dispatch(cmd, attrs)
    monkeypatch.setattr(fake, "_dispatch", log)
    return seen


def _end_the_run() -> None:
    if bench.running():
        bench.cancel()
    assert _wait(lambda: not bench.running(), 30)


def test_a_new_run_judges_an_unchecked_measured_mode_before_it_reads_the_owners(
        run, monkeypatch):
    """A run marked unchecked — PCM still on its last point — and the owner,
    their album playing through Sautium, measures PCM again: before the new
    run reads PCM's selection as theirs, the mode is judged and the owner's
    selection put back, never while the album plays (a mode switched under
    a track restarts the output): at the rest the run's own Stop makes. The
    test point is never what the new run puts back at its end."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    fake = run.fake
    assert _wait(lambda: len(fake.playlist) == 2, 10)             # the queue mirrored
    run_id = _pcm_run_found_in_sdm(run, [1, 0, 0, 0], finished_at=now, outcome="interrupted",
                                   measured_unchecked_at=now)
    with fake._lock:
        fake.volume = -3.0
        fake.track, fake.state = 1, int(PlaybackState.PLAYING)     # the owner's album plays
    seen = _set_modes(monkeypatch, fake)
    bench.start("pcm")
    assert _wait(lambda: run.q("SELECT own_rate, own_filter, own_filter1x, own_shaper "
                               "FROM hqp_benchmark_runs WHERE id > %s", run_id)
                 == [(2, 1, 1, 1)], 20), run.q("SELECT id, own_rate, own_filter FROM "
                                              "hqp_benchmark_runs")
    assert not _unchecked(run, run_id)
    _end_the_run()
    assert seen and [v for v, state in seen if state == int(PlaybackState.PLAYING)] == [], seen


def test_a_new_run_that_finds_another_controllers_entry_leaves_the_measured_mode_marked(
        run, monkeypatch):
    """A run marked unchecked, and the next run starts while HQPlayer plays
    an entry another controller loaded. The run's own Stop makes no rest
    while that entry stays: the measured mode is not entered, the run ends
    there, the mark stands and the notice is said."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    fake = run.fake
    run_id = _pcm_run_found_in_sdm(run, [1, 0, 0, 0], finished_at=now, outcome="interrupted",
                                   measured_unchecked_at=now)
    with fake._lock:
        fake.volume = -3.0
        fake.playlist, fake.track = ["http://other-controller.invalid/track.flac"], 1
        fake.state = int(PlaybackState.PLAYING)
    seen = _set_modes(monkeypatch, fake)
    said, notify = [], bench._notify
    monkeypatch.setattr(bench, "_notify", lambda: (said.append(bench.running()), notify())[1])
    bench.start("pcm")
    assert _wait(lambda: not bench.running(), 30)
    assert "not at rest" in (bench.state()["note"] or ""), bench.state()
    assert seen == [], seen
    assert _unchecked(run, run_id) and True in said, said


def test_a_put_back_owed_at_a_new_runs_start_never_switches_modes_under_a_playing_track(
        run, monkeypatch):
    """A run the node's own stop cut short still owes its put-back once the
    next run holds HQPlayer (start()'s own put-back did not reach it);
    HQPlayer is back in SDM, PCM on the run's test point, and the owner's
    album plays. The put-back marks PCM — HQPlayer is not at rest — and the
    new run judges it at the rest its own Stop makes: no SetMode while the
    album plays, the owner's selection is what the new run reads, and the
    notices are never woken over a mark the run judges a moment later."""
    fake = run.fake
    assert _wait(lambda: len(fake.playlist) == 2, 10)
    run_id = _pcm_run_found_in_sdm(run, [1, 0, 0, 0])
    with fake._lock:
        fake.volume = -3.0
        fake.track, fake.state = 1, int(PlaybackState.PLAYING)
    seen = _set_modes(monkeypatch, fake)
    monkeypatch.setattr(bench, "recover", lambda endpoint_id, link, **kw: None)
    marked, notify = [], bench._notify
    monkeypatch.setattr(bench, "_notify", lambda: (marked.append(_unchecked(run, run_id)),
                                                   notify())[1])
    bench.start("pcm")
    assert _wait(lambda: run.q("SELECT own_rate, own_filter, own_filter1x, own_shaper "
                               "FROM hqp_benchmark_runs WHERE id > %s", run_id)
                 == [(2, 1, 1, 1)], 20), run.q("SELECT id, note FROM hqp_benchmark_runs")
    assert not _unchecked(run, run_id)
    _end_the_run()
    assert seen and [v for v, state in seen if state == int(PlaybackState.PLAYING)] == [], seen
    assert True not in marked, marked


def test_starts_put_back_never_switches_modes_under_a_playing_track(run, monkeypatch):
    """A run the node's own stop cut short still owes its put-back when the
    owner presses Start, HQPlayer back in SDM and playing another
    controller's entry. start() puts the run back first, quiet — before it
    holds HQPlayer and before it asks whether another controller plays: no
    SetMode under the playing track, the run closed and marked, for a run
    to judge at the rest its own Stop makes, or HQPlayer's nearest rest."""
    fake = run.fake
    run_id = _pcm_run_found_in_sdm(run, [1, 0, 0, 0])
    with fake._lock:
        fake.volume = -3.0
        fake.playlist, fake.track = ["http://other-controller.invalid/track.flac"], 1
        fake.state, fake.position = int(PlaybackState.PLAYING), 30.0
    seen = _set_modes(monkeypatch, fake)
    bench.recover(run.endpoint_id, hl.selected(), gave=run.mgr.active.gave, quiet=True)
    assert _wait(lambda: run.q("SELECT finished_at IS NOT NULL FROM hqp_benchmark_runs "
                               "WHERE id = %s", run_id) == [(True,)], 15)
    assert _unchecked(run, run_id)
    assert seen == [] and fake.state == int(PlaybackState.PLAYING), (seen, fake.state)


def test_a_mode_starts_put_back_could_not_read_stays_marked_for_the_new_run(run, monkeypatch):
    """start()'s own put-back on an HQPlayer at rest, and HQPlayer refuses
    the SetMode into PCM this once: PCM is not read, and the run stays
    marked — closed with no notice, but marked. The new run of PCM judges it
    before it reads PCM's selection: the test point goes back to the
    owner's, never taken for theirs."""
    fake = run.fake
    run_id = _pcm_run_found_in_sdm(run, [1, 0, 0, 0])
    refused, dispatch = [], fake._dispatch

    def refuses_once(cmd, attrs):
        if cmd == "SetMode" and attrs.get("value") == "1" and not refused:
            refused.append(True)
            return '<SetMode result="Error">Mode not available</SetMode>'
        return dispatch(cmd, attrs)
    monkeypatch.setattr(fake, "_dispatch", refuses_once)
    bench.start("pcm")
    assert _wait(lambda: run.q("SELECT own_rate, own_filter, own_filter1x, own_shaper "
                               "FROM hqp_benchmark_runs WHERE id > %s", run_id)
                 == [(2, 1, 1, 1)], 30), run.q("SELECT id, note, measured_unchecked_at "
                                              "FROM hqp_benchmark_runs")
    _end_the_run()
    assert refused
    assert not _unchecked(run, run_id)


def _marked_while_busy(r: Run) -> int:
    """A "Measure PCM" run from SDM whose put-back found HQPlayer busy:
    closed and marked, PCM still on its last point, HQPlayer back on the
    owner's volume and resting on the queue it was handed."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    assert _wait(lambda: len(r.fake.playlist) == 2, 10)
    run_id = _pcm_run_found_in_sdm(r, [1, 0, 0, 0], finished_at=now, outcome="interrupted",
                                   measured_unchecked_at=now)
    with r.fake._lock:
        r.fake.volume = -3.0
    with bench._lock:               # marked as a close marks it: read again
        bench._unchecked.pop(r.endpoint_id, None)
        bench._bump()
    return run_id


def _refuses_pcm(monkeypatch, fake) -> list:
    """HQPlayer refuses every SetMode into PCM: when each was asked."""
    refused, dispatch = [], fake._dispatch

    def refuses(cmd, attrs):
        if cmd == "SetMode" and attrs.get("value") == "1":
            refused.append(time.monotonic())
            return '<SetMode result="Error">Mode not available</SetMode>'
        return dispatch(cmd, attrs)
    monkeypatch.setattr(fake, "_dispatch", refuses)
    return refused


def _ticks(r: Run, n: int = 3) -> None:
    """`n` more answers of the poller's."""
    for _ in range(n):
        k = len(r.fake.arrivals)
        r.mgr.active.poke()
        assert _wait(lambda: [c for _, c in r.fake.arrivals[k:]].count("Status") >= 1)


def _refused_at_rest(r: Run, run_id: int) -> bool:
    return r.q("SELECT measured_refused_at IS NOT NULL FROM hqp_benchmark_runs WHERE id = %s",
               run_id) == [(True,)]


def test_a_mode_hqplayer_at_rest_will_not_switch_to_is_tried_once_then_before_the_next_run(
        run, monkeypatch):
    """A run marked while HQPlayer was busy; HQPlayer rests now and refuses
    the SetMode into PCM, every time. Its nearest rest tries once — the
    notice then says the next run checks it — and no later rest tries
    again; the next run tries once more, and a run of PCM, which would read
    PCM's selection as the owner's, is refused."""
    fake = run.fake
    run_id = _marked_while_busy(run)
    refused = _refuses_pcm(monkeypatch, fake)
    with fake._lock:
        fake.track, fake.state = 1, int(PlaybackState.STOPPED)
    assert _wait(lambda: (run.mgr.active.poke(), _refused_at_rest(run, run_id))[1], 10)
    assert len(refused) == 1 and bench.unchecked_notice()["refused"]
    with fake._lock:                    # a later rest, on another entry
        fake.track = 2
    _ticks(run)
    assert len(refused) == 1, refused
    bench.start("pcm")
    assert _wait(lambda: not bench.running(), 30)
    assert "not checked yet" in (bench.state()["note"] or ""), bench.state()
    assert len(refused) == 2 and _unchecked(run, run_id)
    assert run.q("SELECT count(*) FROM hqp_benchmark_runs WHERE id > %s", run_id) == [(0,)]


@pytest.mark.parametrize("quiet", [False, True])
def test_a_put_back_at_rest_whose_mode_hqplayer_will_not_switch_to_waits_for_the_next_run(
        run, monkeypatch, quiet):
    """The put-back itself finds HQPlayer at rest — at its answer, or at a
    new run's start (`quiet`) — and HQPlayer refuses the SetMode into PCM:
    the run is closed and marked, and no later rest tries the mode again;
    the next run's start does."""
    run_id = _pcm_run_found_in_sdm(run, [1, 0, 0, 0])
    refused = _refuses_pcm(monkeypatch, run.fake)
    bench.recover(run.endpoint_id, hl.selected(), gave=run.mgr.active.gave, quiet=quiet)
    assert _wait(lambda: run.q("SELECT finished_at IS NOT NULL FROM hqp_benchmark_runs "
                               "WHERE id = %s", run_id) == [(True,)])
    assert _unchecked(run, run_id) and _refused_at_rest(run, run_id)
    _ticks(run)
    assert len(refused) == 1, refused


def test_a_mode_hqplayer_at_rest_does_not_show_after_its_setmode_is_tried_once(
        run, monkeypatch):
    """The same with a SetMode HQPlayer takes but State does not show: one
    round trip at the rest, none at a later one."""
    fake = run.fake
    run_id = _marked_while_busy(run)
    seen, dispatch = [], fake._dispatch

    def stays_in_sdm(cmd, attrs):
        if cmd == "SetMode" and attrs.get("value") == "1":
            seen.append(attrs)
            return '<SetMode result="OK"/>'
        return dispatch(cmd, attrs)
    monkeypatch.setattr(fake, "_dispatch", stays_in_sdm)
    with fake._lock:
        fake.track, fake.state = 1, int(PlaybackState.STOPPED)
    assert _wait(lambda: (run.mgr.active.poke(), _refused_at_rest(run, run_id))[1], 10)
    with fake._lock:
        fake.track = 2
    _ticks(run)
    assert len(seen) == 1, seen
    assert "did not switch to PCM" in run.q("SELECT note FROM hqp_benchmark_runs WHERE id = %s",
                                            run_id)[0][0]


def test_a_verdict_the_database_does_not_take_is_tried_once_per_rest(run, monkeypatch):
    """HQPlayer rests and the check reaches its verdict, which the database
    does not take (an outage): the round trip is not made again at that
    rest — only at the next one, on another entry."""
    import psycopg2
    fake = run.fake
    run_id = _marked_while_busy(run)
    seen = _set_modes(monkeypatch, fake)

    def outage(*a, **kw):
        raise psycopg2.OperationalError("the database is gone")
    monkeypatch.setattr(bench, "_measured_checked", outage)
    with fake._lock:
        fake.track, fake.state = 1, int(PlaybackState.STOPPED)
    assert _wait(lambda: (run.mgr.active.poke(), [v for v, _ in seen if v == "1"])[1], 10)
    _ticks(run)
    assert [v for v, _ in seen].count("1") == 1, seen
    with fake._lock:                    # the next rest
        fake.track = 2
    assert _wait(lambda: (run.mgr.active.poke(), [v for v, _ in seen].count("1") == 2)[1], 10)
    _ticks(run)
    assert [v for v, _ in seen].count("1") == 2 and _unchecked(run, run_id), seen


def test_a_run_of_another_mode_goes_while_the_measured_mode_stays_unchecked(run, monkeypatch):
    """HQPlayer, in SDM, refuses the SetMode into PCM, the mode a run before
    left unchecked. A run of SDM reads neither PCM's selection nor anything
    PCM holds: it goes, and the mark stands for a later look."""
    run_id = _marked_while_busy(run)
    refused = _refuses_pcm(monkeypatch, run.fake)
    monkeypatch.setattr(bench, "check_measured", lambda *a, **kw: None)   # no rest tries it
    bench.start("sdm")
    assert _wait(lambda: run.q("SELECT count(*) FROM hqp_benchmark_runs WHERE id > %s", run_id)
                 == [(1,)], 30), bench.state()
    _end_the_run()
    assert refused and _unchecked(run, run_id)


def _left_in_pcm(r: Run, refused: bool = False) -> int:
    """A "Measure PCM" run from SDM whose check left HQPlayer in PCM — on the
    run's last point there — its way back not taken: marked (at rest and
    `refused`, or busy so far), the return point on its row naming SDM and
    the owner's selection there (11.2 MHz, poly-sinc-gauss-long, ASDM7EC).
    Read afresh, as a process that just started."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    assert _wait(lambda: len(r.fake.playlist) == 2, 10)
    run_id = _pcm_run_found_in_sdm(r, [1, 0, 0, 0], finished_at=now, outcome="interrupted",
                                   measured_unchecked_at=now,
                                   measured_refused_at=now if refused else None,
                                   measured_from_mode=2, measured_from_mode_name="SDM (DSD)",
                                   measured_from_rate=11289600,
                                   measured_from_filter="poly-sinc-gauss-long",
                                   measured_from_filter1x="poly-sinc-gauss-long",
                                   measured_from_shaper="ASDM7EC")
    with r.fake._lock:
        r.fake.mode, r.fake.volume = 1, -3.0
        r.fake.sel[2] = [1, 1, 1, 1]            # SDM's own selection moved meanwhile
    with bench._lock:
        bench._unchecked.pop(r.endpoint_id, None)
        bench._point_rows.pop(r.endpoint_id, None)
        bench._bump()
    return run_id


def _point(r: Run, run_id: int) -> bool:
    return r.q("SELECT measured_from_mode IS NOT NULL FROM hqp_benchmark_runs WHERE id = %s",
               run_id) == [(True,)]


def test_a_run_after_a_restart_goes_back_to_where_a_check_found_hqplayer_first(
        run, monkeypatch):
    """The node restarted while a check had left HQPlayer in PCM. The next
    run, at the rest its own Stop makes, first takes HQPlayer back to SDM and
    the owner's selection there, by name, then judges PCM — and reads SDM as
    the owner's: never the mode Sautium left HQPlayer in."""
    fake = run.fake
    run_id = _left_in_pcm(run)
    seen = _set_modes(monkeypatch, fake)
    bench.start("pcm")
    assert _wait(lambda: run.q("SELECT pre_mode, pre_rate, pre_filter, pre_filter1x, pre_shaper "
                               "FROM hqp_benchmark_runs WHERE id > %s", run_id)
                 == [(2, 2, 0, 0, 0)], 30), (run.q("SELECT id, note FROM hqp_benchmark_runs"),
                                             fake.settings())
    _end_the_run()
    assert [v for v, _ in seen][:3] == ["2", "1", "2"], seen       # back, then the check
    assert not _point(run, run_id) and not _unchecked(run, run_id)


def test_hqplayer_switched_back_by_the_owner_ends_the_notice_without_a_setmode(
        run, monkeypatch):
    """A check left HQPlayer in PCM and its way back was refused at rest; the
    owner switches HQPlayer back to SDM themselves. The poller sees it: the
    return point goes and the notice no longer says HQPlayer stayed in PCM,
    no SetMode of Sautium's sent."""
    fake = run.fake
    run_id = _left_in_pcm(run, refused=True)
    seen = _set_modes(monkeypatch, fake)
    _ticks(run)
    assert bench.stayed_notice()["back_to"]["mode"] == "SDM (DSD)"
    with fake._lock:
        fake.mode = 2                           # the owner's own switch, in HQPlayer
    assert _wait(lambda: (run.mgr.active.poke(), not _point(run, run_id))[1], 10)
    assert bench.stayed_notice() is None
    assert seen == [], seen


def test_a_way_back_refused_keeps_the_return_point_and_sets_nothing_in_the_wrong_mode(
        run, monkeypatch):
    """At rest, PCM the run's only mark: the put-back enters PCM and puts the
    owner's selection back there, then HQPlayer refuses the SetMode back
    into SDM. Nothing of SDM's is set in PCM; the run is marked, the return
    point names SDM and the notice says HQPlayer stayed in PCM."""
    fake = run.fake
    run_id = _pcm_run_found_in_sdm(run, [1, 0, 0, 0])
    with fake._lock:
        fake.volume = -3.0
    refused, dispatch = [], fake._dispatch

    def refuses_sdm(cmd, attrs):
        if cmd == "SetMode" and attrs.get("value") == "2":
            refused.append(True)
            return '<SetMode result="Error">Mode not available</SetMode>'
        return dispatch(cmd, attrs)
    monkeypatch.setattr(fake, "_dispatch", refuses_sdm)
    bench.recover(run.endpoint_id, hl.selected(), gave=run.mgr.active.gave)
    assert _wait(lambda: run.q("SELECT finished_at IS NOT NULL FROM hqp_benchmark_runs "
                               "WHERE id = %s", run_id) == [(True,)])
    assert refused and fake.mode == 1 and fake.sel[1] == [2, 1, 1, 1], fake.settings()
    assert _unchecked(run, run_id) and _refused_at_rest(run, run_id) and _point(run, run_id)
    notice = bench.stayed_notice()
    assert notice["back_to"] == {"mode": "SDM (DSD)", "rate": 11289600,
                                 "filter": "poly-sinc-gauss-long",
                                 "filter1x": "poly-sinc-gauss-long", "shaper": "ASDM7EC"}, notice


def test_the_next_check_at_rest_goes_back_to_where_hqplayer_was_found_first(run, monkeypatch):
    """A check left HQPlayer in PCM, cut short between its two SetModes; the
    mark busy so far. At HQPlayer's next rest the check takes it back to SDM
    first — the owner's selection there by name — and only then judges PCM."""
    fake = run.fake
    run_id = _left_in_pcm(run)
    seen = _set_modes(monkeypatch, fake)
    with fake._lock:
        fake.track, fake.state = 1, int(PlaybackState.STOPPED)
    assert _wait(lambda: (run.mgr.active.poke(), not _unchecked(run, run_id))[1], 10)
    assert [v for v, _ in seen][:3] == ["2", "1", "2"], seen
    assert fake.settings() == (2, (2, 1, 1, 1), (2, 0, 0, 0), -3.0), fake.settings()
    assert not _point(run, run_id)


def test_a_run_whose_hqplayer_does_not_go_back_is_refused(run, monkeypatch):
    """A check left HQPlayer in PCM, and at the next run's start HQPlayer
    refuses the SetMode back into SDM: the run is refused — it would read
    PCM, where Sautium left HQPlayer, as the owner's — a run of SDM too; the
    mark and the return point stand."""
    fake = run.fake
    run_id = _left_in_pcm(run)
    dispatch = fake._dispatch

    def refuses_sdm(cmd, attrs):
        if cmd == "SetMode" and attrs.get("value") == "2":
            return '<SetMode result="Error">Mode not available</SetMode>'
        return dispatch(cmd, attrs)
    monkeypatch.setattr(fake, "_dispatch", refuses_sdm)
    monkeypatch.setattr(bench, "check_measured", lambda *a, **kw: None)   # no rest tries it
    bench.start("sdm")
    assert _wait(lambda: not bench.running(), 30)
    assert "the mode the last check left it in" in (bench.state()["note"] or ""), bench.state()
    assert run.q("SELECT count(*) FROM hqp_benchmark_runs WHERE id > %s", run_id) == [(0,)]
    assert _unchecked(run, run_id) and _point(run, run_id) and _refused_at_rest(run, run_id)


def test_a_persons_dsp_change_leaves_the_mode_a_check_left_to_them(run):
    """A check left HQPlayer in PCM; the person changes a DSP setting: HQPlayer
    is theirs now — the return point goes, said in the run's note, and
    nothing takes HQPlayer back to SDM."""
    run_id = _left_in_pcm(run)
    bench.left_by_person("127.0.0.1", run.fake.port, "a DSP change came first")
    assert not _point(run, run_id)
    note = run.q("SELECT note FROM hqp_benchmark_runs WHERE id = %s", run_id)[0][0]
    assert "left to the owner" in note, note


def test_a_return_point_is_never_cleared_under_a_round_trip_of_ours(run, monkeypatch):
    """The poller's check keeps SDM as the return point and, before its
    SetMode into PCM, the poller reads HQPlayer still in SDM: that is the
    check on its way, not the owner switching back — the point stays, and
    when HQPlayer then refuses the way back it still names SDM."""
    fake, mgr = run.fake, run.mgr
    run_id = _marked_while_busy(run)
    keep = bench._keep_point

    def then_a_tick(*a):
        name = keep(*a)
        _ticks(run, 2)                          # HQPlayer in SDM, the point just written
        return name
    monkeypatch.setattr(bench, "_keep_point", then_a_tick)
    dispatch = fake._dispatch

    def refuses_sdm(cmd, attrs):
        if cmd == "SetMode" and attrs.get("value") == "2":
            return '<SetMode result="Error">Mode not available</SetMode>'
        return dispatch(cmd, attrs)
    monkeypatch.setattr(fake, "_dispatch", refuses_sdm)
    with fake._lock:
        fake.track, fake.state = 1, int(PlaybackState.STOPPED)
    assert _wait(lambda: (mgr.active.poke(), _refused_at_rest(run, run_id))[1], 15)
    assert fake.mode == 1 and _point(run, run_id), (fake.settings(), run.q(
        "SELECT note FROM hqp_benchmark_runs WHERE id = %s", run_id))


def test_a_put_back_a_persons_change_cut_short_in_the_measured_mode_leaves_it_to_them(
        run, monkeypatch):
    """The put-back at rest enters PCM to read it, and a person's DSP change
    comes right then: the put-back's next request goes no further, the run is
    left to them, and so is the mode HQPlayer is in — no return point, no
    way back taken over their change."""
    fake = run.fake
    run_id = _pcm_run_found_in_sdm(run, [1, 0, 0, 0])
    link, dispatch, cut = hl.selected(), fake._dispatch, []

    def a_change_in_pcm(cmd, attrs):
        out = dispatch(cmd, attrs)
        if cmd == "SetMode" and attrs.get("value") == "1" and not cut:
            cut.append(True)
            link.accept("dsp")                  # the person's DSP change, as it is asked
        return out
    monkeypatch.setattr(fake, "_dispatch", a_change_in_pcm)
    bench.recover(run.endpoint_id, link, gave=run.mgr.active.gave)
    assert _wait(lambda: run.q("SELECT finished_at IS NOT NULL FROM hqp_benchmark_runs "
                               "WHERE id = %s", run_id) == [(True,)])
    assert cut and _closed_as_left(run, run_id)
    assert not _point(run, run_id)


def test_a_run_of_another_mode_is_refused_while_hqplayer_is_in_the_unchecked_mode(
        run, monkeypatch):
    """The owner switched HQPlayer to PCM themselves, the mode a run before
    left unchecked, and the check at the next run's start finds no verdict
    there: a run of SDM is refused too — it would read PCM's selection,
    still unchecked, as the owner's."""
    fake = run.fake
    run_id = _marked_while_busy(run)
    with fake._lock:
        fake.mode = 1                           # the owner's own switch to PCM
    monkeypatch.setattr(bench, "check_measured", lambda *a, **kw: None)   # no rest tries it
    monkeypatch.setattr(bench, "_check_measured",
                        lambda link, r, found: (None, "HQPlayer showed no state in PCM"))
    bench.start("sdm")
    assert _wait(lambda: not bench.running(), 30)
    assert "not checked yet" in (bench.state()["note"] or ""), bench.state()
    assert run.q("SELECT count(*) FROM hqp_benchmark_runs WHERE id > %s", run_id) == [(0,)]
    assert _unchecked(run, run_id)


def _notices() -> list:
    from routers import settings as settings_router
    return settings_router._notices_state()["items"]


def test_a_put_back_cut_in_its_round_trip_never_breaks_the_notices(run, monkeypatch):
    """A run cut short owes its put-back; HQPlayer answers at rest and the
    put-back enters PCM to read it — its return point written on the row of
    a run still owed — and HQPlayer goes away right then. The notices every
    tab's live stream derives must still answer: a run still owed is in its
    put-back's hands, no notice of it yet."""
    fake = run.fake
    run_id = _pcm_run_found_in_sdm(run, [1, 0, 0, 0])
    dispatch, gone = fake._dispatch, []

    def goes_away_in_pcm(cmd, attrs):
        out = dispatch(cmd, attrs)
        if cmd == "SetMode" and attrs.get("value") == "1" and not gone:
            gone.append(True)
            fake.mute = True                    # every later connection dropped at once
            return None                         # and this one, unanswered
        return out
    monkeypatch.setattr(fake, "_dispatch", goes_away_in_pcm)
    bench.recover(run.endpoint_id, hl.selected(), gave=run.mgr.active.gave)
    assert _wait(lambda: gone and run_id not in bench._putting, 10)
    assert run.q("SELECT finished_at IS NULL, measured_from_mode FROM hqp_benchmark_runs "
                 "WHERE id = %s", run_id) == [(True, 2)]
    keys = [n["key"] for n in _notices()]
    assert "hqplayer.run_stayed" not in keys and "hqplayer.run_unchecked" not in keys, keys


def test_a_notices_read_during_a_put_backs_round_trip_answers(run, monkeypatch):
    """The same window with nothing going wrong: a notices read made while
    the put-back is inside PCM, its return point written, answers."""
    fake = run.fake
    run_id = _pcm_run_found_in_sdm(run, [1, 0, 0, 0])
    dispatch, read = fake._dispatch, []

    def read_in_pcm(cmd, attrs):
        out = dispatch(cmd, attrs)
        if cmd == "SetMode" and attrs.get("value") == "1" and not read:
            try:
                read.append(("ok", [n["key"] for n in _notices()]))
            except Exception as e:
                read.append((type(e).__name__, str(e)))
        return out
    monkeypatch.setattr(fake, "_dispatch", read_in_pcm)
    bench.recover(run.endpoint_id, hl.selected(), gave=run.mgr.active.gave)
    assert _wait(lambda: run.q("SELECT finished_at IS NOT NULL FROM hqp_benchmark_runs "
                               "WHERE id = %s", run_id) == [(True,)])
    assert read and read[0][0] == "ok", read


def test_hqplayer_left_in_the_measured_mode_is_said_anew(run, monkeypatch):
    """A run marked while HQPlayer was busy: "Benchmark settings not checked
    yet" stands, and the owner has seen it. At HQPlayer's next rest the check
    enters PCM and HQPlayer refuses the way back: HQPlayer stays in PCM, the
    owner's SDM gone until something takes it back. That is said anew — a
    notice of its own, with its own onset — never folded into the one
    already seen."""
    fake, mgr = run.fake, run.mgr
    run_id = _marked_while_busy(run)
    before = {n["key"]: n for n in _notices()}
    assert "hqplayer.run_unchecked" in before and "hqplayer.run_stayed" not in before
    dispatch = fake._dispatch

    def refuses_sdm(cmd, attrs):
        if cmd == "SetMode" and attrs.get("value") == "2":
            return '<SetMode result="Error">Mode not available</SetMode>'
        return dispatch(cmd, attrs)
    monkeypatch.setattr(fake, "_dispatch", refuses_sdm)
    with fake._lock:
        fake.track, fake.state = 1, int(PlaybackState.STOPPED)
    assert _wait(lambda: (mgr.active.poke(), _refused_at_rest(run, run_id))[1], 15)
    after = {n["key"]: n for n in _notices()}
    assert fake.mode == 1 and "hqplayer.run_stayed" in after, (fake.settings(), list(after))
    assert "hqplayer.run_unchecked" not in after
    assert after["hqplayer.run_stayed"]["since"] != before["hqplayer.run_unchecked"]["since"]
    assert after["hqplayer.run_stayed"]["data"]["back_to"]["mode"] == "SDM (DSD)"


def _refuses_sdm(monkeypatch, fake) -> list:
    """HQPlayer refuses every SetMode into SDM: when each was asked."""
    refused, dispatch = [], fake._dispatch

    def refuses(cmd, attrs):
        if cmd == "SetMode" and attrs.get("value") == "2":
            refused.append(time.monotonic())
            return '<SetMode result="Error">Mode not available</SetMode>'
        return dispatch(cmd, attrs)
    monkeypatch.setattr(fake, "_dispatch", refuses)
    return refused


@pytest.mark.parametrize("how", ["refused", "not shown"])
def test_a_way_back_refused_in_a_put_back_sets_nothing_of_the_found_mode_in_the_measured(
        run, monkeypatch, how):
    """At rest, PCM a mark of the run, and the volume still at the run's
    level: the put-back enters PCM, puts the owner's selection back there,
    and HQPlayer refuses the way back into SDM — or takes it and stays in
    PCM — and again when the put-back goes on for the volume. Nothing of
    SDM's selection is set inside PCM; the volume goes back, and what
    HQPlayer did is said."""
    fake = run.fake
    run_id = _pcm_run_found_in_sdm(run, [1, 0, 0, 0])       # lowered: -60 dB
    if how == "refused":
        tries = _refuses_sdm(monkeypatch, fake)
    else:
        tries, dispatch = [], fake._dispatch

        def stays_in_pcm(cmd, attrs):
            if cmd == "SetMode" and attrs.get("value") == "2":
                tries.append(True)
                return '<SetMode result="OK"/>'
            return dispatch(cmd, attrs)
        monkeypatch.setattr(fake, "_dispatch", stays_in_pcm)
    bench.recover(run.endpoint_id, hl.selected(), gave=run.mgr.active.gave)
    assert _wait(lambda: run.q("SELECT finished_at IS NOT NULL FROM hqp_benchmark_runs "
                               "WHERE id = %s", run_id) == [(True,)])
    note = run.q("SELECT note FROM hqp_benchmark_runs WHERE id = %s", run_id)[0][0]
    assert len(tries) == 2 and fake.settings() == (1, (2, 1, 1, 1), (2, 0, 0, 0), -3.0), (
        fake.settings(), note)
    assert ("Mode not available" if how == "refused" else "did not show") in note, note
    assert _point(run, run_id), note


def test_a_run_whose_end_cannot_go_back_sets_nothing_of_the_mode_it_found(run, monkeypatch):
    """A run of PCM from SDM is cancelled, and HQPlayer refuses the SetMode
    back into SDM at the run's own end: PCM gets its own selection back, and
    nothing of SDM's is set inside PCM."""
    fake = run.fake
    fake.jitter = 0.2
    monkeypatch.setattr(bench, "SETTLE_CAP_S", 2.0)
    with fake._lock:
        fake.mode = 2
    before = fake.settings()
    bench.start("pcm")
    assert _wait(lambda: run.point() >= 2, 10)
    refused = _refuses_sdm(monkeypatch, fake)
    assert bench.cancel()
    assert _wait(lambda: not bench.running(), 10)
    assert refused and fake.settings() == (1, before[1], before[2], before[3]), (
        before, fake.settings(), bench.state()["note"])


def test_a_selection_hqplayer_will_not_take_back_is_never_said_put_back(run, monkeypatch):
    """At rest, PCM the run's only mark, on its last point: the check enters
    PCM to put the owner's selection back, and HQPlayer refuses the filter.
    The test point stays in PCM — no verdict: the run stays marked, the
    reason in its note, never "put back"."""
    fake = run.fake
    run_id = _pcm_run_found_in_sdm(run, [1, 0, 0, 0])
    with fake._lock:
        fake.volume = -3.0
    dispatch = fake._dispatch

    def refuses_pcms_filter(cmd, attrs):
        if cmd == "SetFilter" and fake.mode == 1:
            return '<SetFilter result="Error">Filter not available</SetFilter>'
        return dispatch(cmd, attrs)
    monkeypatch.setattr(fake, "_dispatch", refuses_pcms_filter)
    bench.recover(run.endpoint_id, hl.selected(), gave=run.mgr.active.gave)
    assert _wait(lambda: run.q("SELECT finished_at IS NOT NULL FROM hqp_benchmark_runs "
                               "WHERE id = %s", run_id) == [(True,)])
    note = run.q("SELECT note FROM hqp_benchmark_runs WHERE id = %s", run_id)[0][0]
    assert _unchecked(run, run_id) and _refused_at_rest(run, run_id), note
    assert "did not take the selection of PCM back" in note, note
    assert "the selection of PCM was put back" not in note, note


def test_a_return_whose_selection_hqplayer_refuses_says_so(run, monkeypatch):
    """A check left HQPlayer in PCM; the next run's start takes it back to
    SDM, and HQPlayer refuses the filter of the owner's selection there: the
    return point goes — HQPlayer is in SDM — and the run's note says what
    did not go back."""
    fake = run.fake
    run_id = _left_in_pcm(run)
    dispatch = fake._dispatch

    def refuses_sdms_filter(cmd, attrs):
        if cmd == "SetFilter" and fake.mode == 2:
            return '<SetFilter result="Error">Filter not available</SetFilter>'
        return dispatch(cmd, attrs)
    monkeypatch.setattr(fake, "_dispatch", refuses_sdms_filter)
    bench.start("pcm")
    assert _wait(lambda: not _point(run, run_id), 30)
    _end_the_run()
    note = run.q("SELECT note FROM hqp_benchmark_runs WHERE id = %s", run_id)[0][0]
    assert "went back to SDM (DSD) (not all" in note and "Filter not available" in note, note


def test_a_round_trip_whose_found_selection_hqplayer_refuses_says_so(run, monkeypatch):
    """The round trip through PCM leaves HQPlayer in SDM on another selection
    than it was found on, and HQPlayer refuses the filter when it is put back
    as found: the run's note says the selection of SDM did not all go back."""
    fake = run.fake
    run_id = _pcm_run_found_in_sdm(run, [1, 0, 0, 0])
    with fake._lock:
        fake.volume = -3.0
    dispatch, moved = fake._dispatch, []

    def resets_sdm_then_refuses(cmd, attrs):
        if cmd == "SetFilter" and fake.mode == 2 and moved:
            return '<SetFilter result="Error">Filter not available</SetFilter>'
        out = dispatch(cmd, attrs)
        if cmd == "SetMode" and attrs.get("value") == "2" and not moved:
            moved.append(True)
            with fake._lock:
                fake.sel[2] = [1, 1, 1, 1]          # not what the owner had there
        return out
    monkeypatch.setattr(fake, "_dispatch", resets_sdm_then_refuses)
    bench.recover(run.endpoint_id, hl.selected(), gave=run.mgr.active.gave)
    assert _wait(lambda: run.q("SELECT finished_at IS NOT NULL FROM hqp_benchmark_runs "
                               "WHERE id = %s", run_id) == [(True,)])
    note = run.q("SELECT note FROM hqp_benchmark_runs WHERE id = %s", run_id)[0][0]
    assert "the selection of SDM (DSD) not all put back" in note, note


def test_a_round_trip_through_the_measured_mode_leaves_the_found_modes_selection_as_found(
        run, monkeypatch):
    """PCM, which State does not show, is the run's only mark left (the
    owner raised the volume): the put-back enters it, puts the owner's
    selection back and returns — HQPlayer showing SDM on another selection
    than it was found on (a mode switch that reset it). Read again, SDM is
    put back as found: nothing else would put it back."""
    fake = run.fake
    run_id = _pcm_run_found_in_sdm(run, [1, 0, 0, 0])
    with fake._lock:
        fake.volume = -3.0
    dispatch, moved = fake._dispatch, []

    def resets_sdm(cmd, attrs):
        out = dispatch(cmd, attrs)
        if cmd == "SetMode" and attrs.get("value") == "2" and not moved:
            moved.append(True)
            with fake._lock:
                fake.sel[2] = [1, 1, 1, 1]          # not what the owner had there
        return out
    monkeypatch.setattr(fake, "_dispatch", resets_sdm)
    bench._owe(run_id, hl.selected())
    bench.recover(run.endpoint_id, hl.selected())
    assert _wait(lambda: run.q("SELECT finished_at IS NOT NULL FROM hqp_benchmark_runs "
                               "WHERE id = %s", run_id) == [(True,)])
    assert moved and fake.settings() == (2, (2, 1, 1, 1), (2, 0, 0, 0), -3.0), fake.settings()
