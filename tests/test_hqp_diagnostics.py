"""The HQPlayer playback trace's rules (backend/playback/hqp_diagnostics.py):
the verdict on recorded traces — every cause, and the normal plays that a
naive order would misread — the failing run behind the notice, the rings'
sizes, redaction on the way out, and HQPlayer's own log read and classified
(lines as the maintainer's Desktop 5/6 and Embedded installations print them)."""

import json
import logging
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

from config import settings  # noqa: E402
from hqplayer_client import HQPlayerClient, redact_path, redact_uri  # noqa: E402
from playback import hqp_diagnostics as diag  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TRACES = sorted((FIXTURES / "hqp_traces").glob("*.json"))


@pytest.fixture
def fresh():
    with diag._lock:
        diag._ring.clear()
        diag._ledger.clear()
        diag._by_slot.clear()
    diag._run_key = None
    yield
    with diag._lock:
        diag._ring.clear()
    diag._run_key = None


# -- the verdict ----------------------------------------------------------------------

@pytest.mark.parametrize("path", TRACES, ids=lambda p: p.stem)
def test_recorded_traces_get_their_verdict(path):
    case = json.loads(path.read_text(encoding="utf-8"))
    v = diag.verdict(case["facts"], case.get("hqp_log"))
    assert v["code"] == case["expect"], case["note"]
    assert v["title"] and v["sentence"]


def _trace(name):
    return json.loads((FIXTURES / "hqp_traces" / f"{name}.json").read_text(encoding="utf-8"))["facts"]


def test_the_stop_after_a_dsp_change_elsewhere_is_the_owners():
    assert diag._dsp_changed(_trace("played_then_dsp_change_elsewhere")["ticks"])
    # Before a track loads, the previous one's rate shows: not a change.
    ticks = _trace("played_stream")["ticks"]
    first = {**ticks[0], "track": 0, "length": 0.0, "rate": 24576000}
    assert not diag._dsp_changed([first, *ticks])


def test_every_cause_is_recorded():
    codes = {json.loads(p.read_text(encoding="utf-8"))["expect"] for p in TRACES}
    assert codes == {"unreachable", "played", "too_slow", "proxy_error", "no_fetch",
                     "rejected", "external", "not_played", "rate_refused", "unknown", "interrupted"}


def _case(name):
    return json.loads((FIXTURES / "hqp_traces" / f"{name}.json").read_text(encoding="utf-8"))


def test_a_refused_rate_names_the_filter_or_the_modulator_with_their_fixes():
    v = diag.verdict(_trace("rate_refused_filter"))
    assert (v["title"], v["cause"]) == ("HQPlayer refused the rate combination", "filter")
    assert v["sentence"] == ("HQPlayer describes demo-sinc-a as 2^x up: 768 kHz is no power-of-two multiple "
                             "of 44.1 kHz, and HQPlayer stopped on this track.")
    assert v["next"] == ("Pick a filter HQPlayer describes as Any, or turn on adaptive output rate in "
                         "HQPlayer's own settings (Output), or pick 705.6 kHz.")
    assert v["dsp"] == "PCM · demo-sinc-a · TPDF · 768 kHz"
    m = diag.verdict(_trace("rate_refused_modulator"))
    assert (m["cause"], m["next"]) == ("modulator", "Pick another modulator, or another output rate.")
    assert m["sentence"] == ("HQPlayer refused DMX7 512+fs at DSD256 (48k) on engine 9.4.1, and it stopped on "
                             "this 48 kHz track.")


def test_the_verdict_names_a_rate_to_pick_only_when_every_track_still_to_play_is_refused():
    facts = _trace("rate_refused_filter")                   # 44.1 kHz tracks only, from the refused one on
    fix = ("Pick a filter HQPlayer describes as Any, or turn on adaptive output rate in HQPlayer's own "
           "settings (Output)")
    assert diag.verdict(facts)["next"] == fix + ", or pick 705.6 kHz."
    # 48 kHz tracks after it reach 768 kHz, and would not reach 705.6 kHz: no rate named
    mixed = {**facts, "rate": {**facts["rate"], "queue": {"44100": 1, "48000": 2}}}
    assert diag.verdict(mixed)["next"] == fix + "."
    # the queue's rates not read: none named either
    unread = {**facts, "rate": {**facts["rate"], "queue": None}}
    assert diag.verdict(unread)["next"] == fix + "."


def test_only_a_claim_without_a_hedge_closes_an_attempt_at_its_first_stop():
    rate = _trace("rate_refused_filter")["rate"]              # the adaptive output rate read: not checked
    assert diag.names_cause(rate)
    # not known, as for an HQPlayer elsewhere: HQPlayer may yet pick a rate the filter reaches
    assert not diag.names_cause({**rate, "inputs": {**rate["inputs"], "adaptive": None}})


def test_hqplayers_line_is_quoted_and_alone_names_nothing():
    line_only = _case("rate_refused_line_only")
    # without the log the rule names nothing: no cause, as before
    assert diag.verdict(line_only["facts"])["code"] == "not_played"
    v = diag.verdict(line_only["facts"], line_only["hqp_log"])
    assert (v["code"], v["cause"]) == ("rate_refused", "line")
    assert v["sentence"] == ("HQPlayer stopped on it: “Requested filter not possible with this rate combination "
                             "48000/12288000, stop”.")
    assert v["next"] == "Pick another filter, modulator or output rate."
    # the rule's cause with the log: the line beside it
    v = diag.verdict(_trace("rate_refused_filter"), line_only["hqp_log"])
    assert v["sentence"].endswith("HQPlayer's log: “Requested filter not possible with this rate combination "
                                  "48000/12288000, stop”.")
    # a source no file row gave: the line's
    facts = {**line_only["facts"], "rate": {**line_only["facts"]["rate"], "source": None}}
    assert diag.verdict(facts, line_only["hqp_log"])["code"] == "rate_refused"


def test_the_log_read_after_the_close_judges_the_attempt_again(fresh):
    line_only = _case("rate_refused_line_only")
    a = diag.Attempt(intent="replace", slot=1)
    diag.close(a, line_only["facts"])
    assert a.verdict["code"] == "not_played"
    diag.attach_log(a, line_only["hqp_log"])
    assert a.verdict["code"] == "rate_refused" and a.hqp_log is line_only["hqp_log"]
    # a verdict with a cause of its own is left as it was
    played = diag.Attempt(intent="select", slot=3)
    diag.close(played, _trace("played_ran_out"))
    diag.attach_log(played, line_only["hqp_log"])
    assert played.verdict["code"] == "played"


def test_the_logs_cause_names_neither_the_filter_nor_the_modulator():
    line = "E 2001/05/06 10:00:01 Requested filter not possible with this rate combination 48000/12288000, stop"
    assert diag.classify([line])["sentence"] == "HQPlayer refused this rate combination."


# -- HQPlayer's own advance -----------------------------------------------------------

def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _stopped_status(serial=5):
    return SimpleNamespace(track_index=0, position=0.0, length=0.0, process_speed=0.0, input_fill=0.0,
                           output_fill=0.0, tracks_total=0, active_mode="PCM", active_filter="demo-sinc-a",
                           active_shaper="TPDF", active_rate=768000, src_rate=None, track_serial=serial)


def test_the_start_signal_counts_an_advances_own_take_only():
    def tick(state, position, serial):
        return {"state": state, "position": position, "serial": serial}
    assert not diag.started([tick("stopped", 0.0, 5)])
    assert diag.started([tick("playing", 1.0, 5)], 5) and diag.started([tick("playing", 1.0, None)])
    assert diag.started([tick("playing", 0.2, 5), tick("playing", 0.6, 5)], 5)
    # a Play of that entry afterwards takes it again, one more: not this advance's start
    assert not diag.started([tick("stopped", 0.0, 5), tick("playing", 2.0, 6)], 5)


def test_an_advance_is_watched_from_its_first_stop_to_its_deadline():
    att = diag.advance(1, 5, 1.04, 1000.0)
    assert (att.intent, att.slot, att.t0) == ("advance", 2, 1000.0)
    assert att.advance["deadline"] == pytest.approx(1000.0 + 2 * 1.04 + 11.0)
    att.tick(_stopped_status(), "stopped", 1000.0)          # the first stopped tick, asked for at t0
    assert [t["ts"] for t in att.snapshot()["ticks"]] == [1000.0]
    # a stop the rule may explain, at its first tick: the close there is a Play's, not an advance's
    assert not att.stopped_at_start()
    assert att.due(1013.0) is None and att.due(1013.1) == "window"
    assumed = diag.advance(1, 5, None, 1000.0)
    assert (assumed.advance["output_delay"], assumed.advance["delay_assumed"]) == (3.5, True)
    assert assumed.advance["deadline"] == pytest.approx(1000.0 + 2 * 3.5 + 11.0)


# -- the start on the meter stream ------------------------------------------------------

def _tick(ts, state="playing", track=1, position=0.0, serial=None):
    return {"ts": ts, "state": state, "track": track, "position": position, "serial": serial}


def _seen(*runs, opened=None):
    """What the meter showed: runs as (since, start, last), one session from `opened`."""
    first = min((r[1] for r in runs), default=None)
    return {"sessions": [{"open": opened if opened is not None else min(r[0] for r in runs), "first": first,
                          "closed": None}],
            "runs": [{"since": a, "start": b, "last": c, "frames": int((c - b) / 0.16) + 1} for a, b, c in runs]}


def test_frames_running_while_a_status_names_the_entry_it_plays_are_its_start():
    # its Status names the entry and keeps the position at 0 for seconds while the meter shows sound
    lagging = [_tick(10.1, track=0), _tick(11.0), _tick(12.0)]
    assert not diag.started(lagging)
    assert diag.started(lagging, meter=_seen((5.0, 10.6, 14.0)))
    # the frames came seconds after the status, or ended seconds before it: nothing beside it
    assert not diag.started([_tick(11.0)], meter=_seen((5.0, 13.0, 14.0)))
    assert not diag.started([_tick(11.0)], meter=_seen((5.0, 6.0, 9.9)))
    # an opening status names no entry yet
    assert not diag.started([_tick(11.0, track=0)], meter=_seen((5.0, 10.6, 14.0)))


def test_sound_after_the_take_and_a_quiet_the_meter_watched_is_its_start_though_no_status_names_it():
    stale = [_tick(10.1, track=0), _tick(11.0, track=0)]
    # the meter watched, quiet, through the take (a Play from stopped): the sound is the take's
    assert diag.started(stale, meter=_seen((9.0, 10.6, 20.0)), take=10.0)
    # ... but sound that began before the take is not
    assert not diag.started(stale, meter=_seen((5.0, 9.0, 20.0)), take=10.0)
    # a session opened after the take whose sound came at once may have caught what played before it
    assert not diag.started(stale, meter=_seen((10.3, 10.4, 20.0)), take=10.0)
    # ... one quiet for a while first did not
    assert diag.started(stale, meter=_seen((10.3, 11.0, 20.0)), take=10.0)
    # HQPlayer's own advance onto a refused entry, live: the window opened at its first stopped tick, and the entry
    # before sounded on for 80 ms (2026-10-22); a jump's window opened a moment before its SelectTrack went
    assert not diag.started(stale, meter=_seen((10.0, 10.02, 10.08)), take=10.0)
    assert not diag.started(stale, meter=_seen((9.995, 10.1, 10.4)), take=10.0)


def test_the_sound_a_jump_leaves_is_no_start_beside_a_status_that_names_the_next():
    # HQPlayer names the entry tapped, at 0, while the one it left sounds on for a moment past the take
    named = [_tick(10.15, track=2)]
    assert not diag.started(named, meter=_seen((5.0, 6.0, 10.4)), take=10.0)
    # the sound goes on past the status: the next entry's
    assert diag.started(named, meter=_seen((5.0, 6.0, 12.0)), take=10.0)


def test_a_burst_a_stalled_stream_delivers_at_once_is_no_resume():
    """The sound from before the take, held up past a quiet of the stream's own and delivered in one read."""
    stale = [_tick(10.1, track=0), _tick(11.0, track=0)]
    assert not diag.started(stale, meter=_seen((9.4, 10.5, 10.5)), take=10.0)
    assert diag.started(stale, meter=_seen((9.4, 10.5, 11.2)), take=10.0)


def test_sound_after_a_status_of_another_take_is_no_start_of_the_advance():
    ticks = [_tick(100.0, "stopped", 0, serial=6), _tick(101.0, "playing", 0, serial=7)]    # a Play since, its own take
    assert not diag.started(ticks, 6, _seen((40.0, 101.5, 105.0)), take=100.0)
    assert diag.started(ticks[:1], 6, _seen((40.0, 101.5, 105.0)), take=100.0)


def test_the_tail_after_hqplayers_own_advance_is_no_start_and_what_follows_its_gap_is():
    # its own Next at 100.0 (the first stopped tick, track_serial 6): stopped on no entry while the entry before
    # plays out the tail of its output delay
    tail = [_tick(100.0, "stopped", 0, serial=6), _tick(101.0, "stopped", 0, serial=6),
            _tick(102.0, "stopped", 0, serial=6)]
    flowing = _seen((40.0, 40.2, 103.0))
    assert not diag.started(tail, 6, flowing, take=100.0)
    opened_at_the_stop = _seen((100.1, 100.2, 103.0))           # the window opened with the advance
    assert not diag.started(tail, 6, opened_at_the_stop, take=100.0)
    # the tail ends, the next entry builds, and its frames begin after a gap: its start
    after = {**flowing, "runs": flowing["runs"] + _seen((103.0, 104.4, 106.0))["runs"]}
    assert diag.started(tail, 6, after, take=100.0)
    # a status of another take beside those frames is no start of this advance
    assert not diag.started([_tick(104.5, serial=7)], 6, flowing, take=100.0)


def test_an_attempt_takes_its_entry_when_its_first_transport_command_went():
    from hqplayer_client import CommandOutcome
    att = diag.Attempt(intent="play", slot=1, started=999.5)
    att.record(CommandOutcome(1000.0, "h", 1, "Play", {}, "OK", sent=999.6))
    assert (att.t0, att.take) == (1000.0, 999.6)
    att.tick(_stopped_status(), "stopped", 1000.1, _seen((998.0, 1000.05, 1001.0)))
    assert att.moved()                          # its frames began after the Play went, before its answer
    assert diag.advance(1, 5, 1.0, 2000.0).take == 2000.0


def test_an_attempt_keeps_what_the_meter_showed_from_its_beginning_on():
    att = diag.Attempt(intent="play", slot=1, started=1000.0)
    shown = {"sessions": [{"open": 900.0, "first": 901.0, "closed": 950.0},
                          {"open": 999.0, "first": None, "closed": None}],
             "runs": [{"since": 900.0, "start": 901.0, "last": 949.0, "frames": 300}]}
    att.tick(_stopped_status(), "stopped", 1000.5, shown)
    assert att.snapshot()["meter"] == {"sessions": [{"open": 999.0, "first": None, "closed": None}], "runs": []}


def test_a_status_asked_for_before_the_transport_command_is_no_tick_of_the_attempt():
    from hqplayer_client import CommandOutcome
    att = diag.Attempt(intent="play", slot=1)
    att.record(CommandOutcome(1000.0, "h", 1, "Play", {}, "OK"))
    # asked for before the Play went: what HQPlayer held then, stopped at 0 after a replace's Stop
    att.tick(_stopped_status(), "stopped", 999.9)
    assert att.snapshot()["ticks"] == [] and not att.stopped_at_start()
    att.tick(_stopped_status(), "stopped", 1000.2)
    assert [t["ts"] for t in att.snapshot()["ticks"]] == [1000.2] and att.stopped_at_start()


def test_hqplayers_log_is_followed_from_the_stop_a_whole_line_at_a_time(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "hqplayer_data_dir", str(tmp_path))
    log = tmp_path / "HQPlayer9Desktop.log"
    # a refusal from before the stop is no refusal of this advance
    log.write_text("E 2001/05/06 09:00:00 Requested filter not possible with this rate combination "
                   "48000/705600, stop\n", encoding="utf-8")
    woke = threading.Event()
    att = diag.advance(1, 5, 1.0, time.time())
    att.follow({"version": "9"}, woke.set)
    assert _wait(lambda: att.snapshot()["advance"]["log"] == "followed")
    line = "E 2001/05/06 10:00:09 Requested filter not possible with this rate combination 48000/705600, stop"
    with open(log, "a", encoding="utf-8") as f:
        f.write(line[:40])
    att.follower.wake()
    assert _wait(lambda: att.follower.offset == log.stat().st_size)      # half a line, read: it waits
    assert att.end is None and not woke.is_set()
    with open(log, "a", encoding="utf-8") as f:
        f.write(line[40:] + "\n")
    att.follower.wake()
    assert _wait(woke.is_set)
    assert att.end == "refused" and att.snapshot()["advance"]["line"] == line


def test_a_log_gone_or_cut_leaves_an_advance_to_its_deadline_with_one_line(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(settings, "hqplayer_data_dir", str(tmp_path))
    gone = diag.advance(1, 5, 1.0, time.time())
    gone.follow({"version": "9"}, lambda: None)
    assert _wait(lambda: f"not followed for attempt {gone.id}" in caplog.text and not gone.follower.alive())
    assert gone.snapshot()["advance"]["log"] is None and gone.end is None
    log = tmp_path / "HQPlayer9Desktop.log"
    log.write_text("I 2001/05/06 10:00:00 Play (1/0)\n" * 50, encoding="utf-8")
    cut = diag.advance(1, 5, 1.0, time.time())
    cut.follow({"version": "9"}, lambda: None)
    assert _wait(lambda: cut.snapshot()["advance"]["log"] == "followed")
    log.write_text("I 2001/05/06 10:00:09 rotated\n", encoding="utf-8")     # cut, or another file there
    for _ in range(3):
        cut.follower.wake()
    assert _wait(lambda: cut.snapshot()["advance"]["log"] == "lost")
    assert cut.end is None and caplog.text.count(f"attempt {cut.id}") == 1


def test_while_a_log_read_hangs_a_newer_advance_starts_none(monkeypatch):
    release = threading.Event()

    def hanging(info):                  # an open on a mount that dropped
        release.wait(5.0)
        return None, "gone", None
    monkeypatch.setattr(diag, "_local_log", hanging)
    first = diag.advance(1, 5, 1.0, time.time())
    first.follow({"version": "9"}, lambda: None)
    second = diag.advance(2, 6, 1.0, time.time())
    second.follow({"version": "9"}, lambda: None)
    # its deadline alone, and the trace says why
    assert second.follower is None and second.snapshot()["advance"]["log"] == "hung"
    release.set()
    assert _wait(lambda: not first.follower.alive())
    third = diag.advance(3, 7, 1.0, time.time())
    third.follow({"version": "9"}, lambda: None)
    assert third.follower is not None and _wait(lambda: not third.follower.alive())


def test_the_verdict_quotes_hqplayer_and_names_the_next_step():
    v = diag.verdict(_trace("rejected_add_error"))
    assert v["quote"] == "Unable to open file" and "Unable to open file" in v["sentence"]
    assert "file exists" in v["next"]                   # a path on this machine
    assert "Rescan" in diag.verdict(_trace("rejected_held_copy_moved"))["next"]
    assert "Empty transport" in diag.verdict(_trace("rejected_empty_transport"))["quote"]


def test_no_fetch_names_the_address_and_the_port_to_open():
    v = diag.verdict(_trace("no_fetch_stream"))
    assert "192.168.1.88:8830" in v["sentence"]
    assert "TCP 8830" in v["next"]


def test_proxy_statuses_name_their_cause():
    assert diag.verdict(_trace("proxy_404"))["status"] == 404
    assert "restarts" in diag.verdict(_trace("proxy_404"))["sentence"]
    assert "missing" in diag.verdict(_trace("proxy_500"))["sentence"]
    assert "bug" in diag.verdict(_trace("proxy_403"))["sentence"]


def test_too_slow_names_the_dsp_setting():
    v = diag.verdict(_trace("too_slow_speed"))
    assert v["speed"] == 0.82
    assert "poly-sinc-gauss-xla" in v["sentence"] and "DSD512" in v["sentence"]
    assert diag.verdict(_trace("too_slow_output_draining"))["output_fill"] == 0.05


def test_a_command_that_never_left_is_unreachable_with_the_trial_hint():
    v = diag.verdict(_trace("unreachable_trial_stop"))
    assert "30 minutes" in v["next"] and "192.168.1.53:4321" in v["sentence"]


# -- the run behind the notice ---------------------------------------------------------

def _close(code_trace, **over):
    a = diag.Attempt(intent="select", slot=3)
    facts = {**_trace(code_trace), **over}
    return a, diag.close(a, facts)


def test_three_failures_in_a_row_are_a_failing_run(fresh):
    assert _close("no_fetch_stream")[1] is False
    assert _close("no_fetch_stream")[1] is False
    assert diag.failing_run() is None
    _, changed = _close("no_fetch_stream")
    run = diag.failing_run()
    assert changed and run["code"] == "no_fetch" and run["count"] == 3
    _, changed = _close("interrupted_superseded")      # not counted, not a break
    assert not changed and diag.failing_run()["count"] == 3
    _, changed = _close("played_stream")
    assert changed and diag.failing_run() is None


def test_a_different_failure_breaks_the_run(fresh):
    for _ in range(3):
        _close("proxy_404")
    _close("not_played_stream")
    assert diag.failing_run() is None


def test_a_failure_the_slot_has_played_through_is_cleared(fresh):
    # Three plays judged failed on slot 3 of queue 7 — a failing run ...
    attempts = [_close("not_played_stream", slot=3, generation=7)[0] for _ in range(3)]
    assert diag.failing_run()["count"] == 3
    # ... another slot, another queue playing: they still explain it
    assert diag.note_playing(2, 7) is False
    assert diag.note_playing(3, 8) is False
    assert diag.failing_run()["count"] == 3
    # that very slot playing: the newest failure explains nothing now, the run ends
    assert diag.note_playing(3, 7) is True
    assert attempts[-1].cleared and diag.failing_run() is None
    assert diag.note_playing(3, 7) is False
    # a played attempt has nothing to clear
    played, _ = _close("played_stream", slot=3, generation=7)
    assert diag.note_playing(3, 7) is False and not played.cleared


def test_a_failure_behind_a_later_unreachable_attempt_is_still_cleared(fresh):
    # Play pressed again while HQPlayer was still silent: the gate's GetInfo
    # timed out, an unreachable attempt is the newest — the failure on slot 3
    # must still clear once HQPlayer plays it, not come back at the album's end
    failed, _ = _close("not_played_stream", slot=3, generation=7)
    diag.record_unreachable("GetInfo: timed out", {})
    diag.note_playing(3, 7)
    assert failed.cleared


def _ticked(a, ts, state, position):
    a.ticks.append({"ts": ts, "state": state, "track": 3, "position": position, "length": 300.0,
                    "speed": 2.0, "in_fill": -1.0, "out_fill": 0.9, "tracks_total": 4,
                    "mode": "SDM (DSD)", "filter": "sinc-MGa", "shaper": "ASDM7ECv3",
                    "rate": 11289600})


def test_a_start_still_initialising_is_watched_until_the_position_moves():
    # Play, then HQPlayer says it plays while a long filter initialises —
    # the position at 0, its control port silent for a while — and the
    # audio leaves seven seconds after Play (HQPlayer's log, 2026-10-03)
    a = diag.Attempt(intent="play", slot=3)
    a.t0 = 1000.0
    for ts in (1001.0, 1002.0, 1003.0):
        _ticked(a, ts, "playing", 0.0)
    a.misses.append({"ts": 1008.0, "message": "Status: no answer within 4 s", "silent": True})
    assert a.due(1010.5) is None                       # past the window: still starting
    _ticked(a, 1011.0, "playing", 0.4)
    assert a.due(1011.5) == "window"                   # it moved: judged now, as played


def test_a_state_hqplayer_does_not_name_is_watched_like_a_start():
    # Embedded 6.2.3 said state 5 while it started a play (2026-10-03)
    a = diag.Attempt(intent="play", slot=3)
    a.t0 = 1000.0
    for ts in (1001.0, 1004.0, 1008.0):
        _ticked(a, ts, "unknown", 0.0)
    assert a.due(1010.5) is None                       # past the window: still starting
    _ticked(a, 1011.0, "playing", 1.5)
    assert a.due(1011.5) == "window"


def test_an_hqplayer_gone_mid_start_is_judged_at_the_window():
    # it refused the reads (a trial stop, a power cut): away, not building
    a = diag.Attempt(intent="play", slot=3)
    a.t0 = 1000.0
    _ticked(a, 1001.0, "playing", 0.0)
    a.misses.append({"ts": 1004.0, "message": "connect to hqp:4321: refused", "silent": False})
    assert a.due(1000.0 + diag.OBSERVE_S + 0.5) == "window"


def test_a_start_that_never_moves_is_judged_at_the_limit():
    a = diag.Attempt(intent="play", slot=3)
    a.t0 = 1000.0
    for i in range(1, 70):
        _ticked(a, 1000.0 + i, "playing", 0.0)
    assert a.due(1000.0 + diag.START_LIMIT_S - 1) is None
    assert a.due(1000.0 + diag.START_LIMIT_S) == "window"


def test_a_stopped_hqplayer_is_judged_at_the_window():
    a = diag.Attempt(intent="play", slot=3)
    a.t0 = 1000.0
    for ts in range(1001, 1010):
        _ticked(a, float(ts), "stopped", 0.0)
    assert a.due(1000.0 + diag.OBSERVE_S) == "window"


def test_only_a_silent_status_socket_is_watched_like_a_start():
    # the WSL2 hop flapping while the music plays on: no tick at all yet
    a = diag.Attempt(intent="select", slot=3)
    a.t0 = 1000.0
    a.misses.append({"ts": 1004.0, "message": "Status: no answer within 4 s", "silent": True})
    assert a.due(1010.5) is None
    _ticked(a, 1012.0, "playing", 12.0)
    assert a.due(1012.5) == "window"


def test_an_unreachable_run_ends_when_hqplayer_answers(fresh):
    for _ in range(3):
        diag.record_unreachable("GetInfo: timed out", {})
    assert diag.failing_run()["code"] == "unreachable"
    assert diag.note_answered() is True
    assert diag.failing_run() is None
    assert diag.note_answered() is False


# -- rings and attempts --------------------------------------------------------------------

def test_the_client_error_ring_keeps_the_last_50_newest_first():
    HQPlayerClient._error_ring.clear()
    client = HQPlayerClient("192.168.1.53", 4321)
    for i in range(60):
        client._outcome("PlaylistAdd", {"uri": f"file:///E:/Music/Artist/Album/{i:02d}.flac"},
                        "Error", f"refused {i}")
    errors = HQPlayerClient.last_errors()
    assert len(errors) == 50
    assert errors[0]["message"] == "refused 59" and errors[-1]["message"] == "refused 10"
    assert errors[0]["attributes"]["uri"] == "file://…/Album/59.flac"
    assert client.last_error.message == "refused 59"
    client._outcome("Play", {}, "OK")
    assert len(HQPlayerClient.last_errors()) == 50       # an accepted command is no error
    HQPlayerClient._error_ring.clear()


def test_the_attempt_ring_keeps_the_last_20(fresh):
    ids = [_close("played_stream")[0].id for _ in range(25)]
    kept = [a.id for a in diag.attempts()]
    assert len(kept) == 20 and kept == ids[:-21:-1]
    assert diag.find(ids[0]) is None and diag.find(ids[-1]) is not None
    assert len(set(ids)) == 25                            # ids never repeat


def test_a_final_refusal_settles_the_attempt_but_a_retried_one_does_not():
    from hqplayer_client import CommandOutcome
    a = diag.Attempt(intent="play")
    out = lambda result, msg="": CommandOutcome(1.0, "h", 1, "Play", {}, result, msg)  # noqa: E731
    a.record(out("Error", "not yet"))
    a.record(out("OK"))
    a.intent_done(None)
    assert a.end is None and a.t0 == 1.0                 # the second Play was accepted
    b = diag.Attempt(intent="play")
    b.record(out("Error", "Empty transport"))
    b.intent_done(None)
    assert b.end == "hard"


def test_a_select_moves_the_expected_slot():
    from hqplayer_client import CommandOutcome
    a = diag.Attempt(intent="replace", slot=1)
    a.record(CommandOutcome(1.0, "h", 1, "SelectTrack", {"index": "2"}, "OK"))
    assert a.slot == 2


# -- redaction ------------------------------------------------------------------------------

def test_paths_leave_only_their_last_two_components():
    assert redact_path("E:/Music/Genre/Artist/Album/03. Track.flac") == \
        "…/Album/03. Track.flac"
    assert redact_path(r"E:\Music\A\B\c.flac") == "…/B/c.flac"
    assert redact_uri("file:///media/usb/Music/Album/x.flac") == "file://…/Album/x.flac"
    assert redact_uri("http://192.168.1.88:8830/file/AbCdEfGhIjKlMnOpQrSt") == \
        "http://192.168.1.88:8830/file/AbCdEf…"
    assert redact_uri("Album") == "Album"


def test_paths_inside_log_lines_are_cut_wherever_they_stand():
    quoted = ('# 2026/09/30 15:16:16 clPlaylist::AddURI("file:///E:/Music/Album/03. Track.flac"): '
              'clFileIO::Open(): CreateFile("E:\\Music\\Album\\03. Track.flac"): '
              'The system cannot find the path specified.')
    out = diag.redact_text(quoted)
    assert 'AddURI("file://…/Album/03. Track.flac")' in out
    assert 'CreateFile("…/Album/03. Track.flac")' in out
    assert out.endswith("The system cannot find the path specified.")
    eol = "& 2026/06/26 13:46:22 Playlist add file: E:/Music/Genre/Artist/Other Album/07. Song.flac"
    assert diag.redact_text(eol).endswith("Playlist add file: …/Other Album/07. Song.flac")
    token = "& 2026/09/27 12:48:17 Playlist add URI: http://192.168.1.88:8830/file/Fx3kQp9TzL0aWm2Rb7Yd"
    assert diag.redact_text(token).endswith("/file/Fx3kQp…")


def test_shares_and_any_absolute_path_are_cut_too():
    assert redact_uri("\\\\NAS\\Music\\Artist\\Album\\01.flac") == "…/Album/01.flac"
    unc = ('# 2026/09/30 15:16:16 clPlaylist::AddURI("file:////NAS/Music/Artist/Album/01.flac"): '
           'CreateFile("\\\\NAS\\Music\\Artist\\Album\\01.flac"): The system cannot find the path specified.')
    out = diag.redact_text(unc)
    assert "NAS" not in out and out.count("…/Album/01.flac") == 2
    assert diag.redact_text("& 2026/10/02 12:00:00 Playlist add file: /run/media/someone/Music/Album/01.flac") \
        .endswith("…/Album/01.flac")
    win = "  2026/10/01 21:38:27 Set transport (240): C:\\Users\\someone\\AppData\\Local\\HQPlayer\\current.m3u8"
    assert "someone" not in diag.redact_text(win)
    untouched = "& 2026/10/02 12:00:00 Play (-1/0)"
    assert diag.redact_text(untouched) == untouched


def test_a_redacted_trace_names_no_full_path(fresh):
    a, _ = _close("rejected_not_kept_slots_shifted")
    diag.attach_log(a, {"lines": ["& 2026/06/26 13:46:22 Playlist add file: "
                                  "E:/Music/Genre/Artist/Album/03. Track.flac"],
                        "where": r"C:\Users\someone\AppData\Local\HQPlayer\HQPlayer6Desktop.log",
                        "note": None, "cause": None, "source": "local"})
    whole = json.dumps(diag.public(a, redact=False), ensure_ascii=False)
    assert "E:/Music/Genre" in whole
    # The media-proxy token is the capability that serves the file.
    streamed, _ = _close("played_stream")
    assert diag.public(streamed, redact=True)["facts"]["token"] == "AbCdEf…"
    cut = json.dumps(diag.public(a, redact=True), ensure_ascii=False)
    assert "E:/Music" not in cut and "Genre/" not in cut and "someone" not in cut
    assert "…/Album/03. Track.flac" in cut


# -- HQPlayer's own log ---------------------------------------------------------------------

def _log_lines():
    rows = (FIXTURES / "hqp_log_lines.txt").read_text(encoding="utf-8").splitlines()
    return [tuple(r.split("\t", 1)) for r in rows if r]


@pytest.mark.parametrize("code,line", _log_lines(), ids=lambda x: x[:40])
def test_observed_lines_name_their_cause(code, line):
    cause = diag.classify([line])
    assert (cause["code"] if cause else "-") == code


def test_without_its_file_in_the_log_the_newest_cause_answers():
    lines = ["# 2026/10/02 12:00:00 snd_pcm_open(): Device or resource busy",
             "# 2026/10/02 12:05:00 NAA output network timeout"]
    assert diag.classify(lines, "file:///E:/Music/Artist/Album/01.flac")["code"] == "naa_lost"
    # A folder named "file" is part of a path, not a media-proxy token.
    assert diag._needles("file:///E:/Music/file/01.flac") == \
        ["E:/Music/file/01.flac", "E:\\Music\\file\\01.flac"]


def test_the_cause_is_read_from_the_attempts_own_add_onward():
    uri = "http://192.168.1.88:8830/file/Fx3kQp9TzL0aWm2Rb7Yd"
    lines = ["# 2026/09/27 12:40:00 clPlaylist::AddURI(): unknown mime type: audio/mp4",
             f"& 2026/09/27 12:48:17 Playlist add URI: {uri}",
             "# 2026/09/27 12:48:18 NAA output network timeout",
             "# 2026/09/27 12:50:02 snd_pcm_open(): Device or resource busy"]
    assert diag.classify(lines, uri)["code"] == "naa_lost"      # its own add onward
    assert diag.classify(lines)["code"] == "device_busy"        # no file: the newest


NOISE = [
    "  2026/10/02 14:33:04 NAA output network Audio IPv6 support disabled",
    "  2026/10/02 14:33:04 NAA output discovery from 0.0.0.0",
    "  2026/10/02 14:33:05 NAA output discovered 0 Network Audio Adapters",
    "# 2026/09/30 15:26:00 clUPnP::OnRequest(): clString::ToUInt(): not an integer '540.0'",
    "  2026/10/01 21:38:26 Initializing processing for matrix pipeline 83",
    "  2026/10/01 21:38:26 Matrix pipeline 83: 83 -> 83 0/1",
    "  2026/10/02 12:38:36 \tNEON64",
    '# 2026/09/01 10:00:00 clReadFLAC::ProcessTag(): invalid album gain "+4.42 dB"',
    # every open and close of the peak meter (texts from the 6.2.3 binary)
    "  2026/10/07 19:40:12 Meter connection from 192.168.1.88",
    "  2026/10/07 19:40:12 Metering started from 192.168.1.88",
    "  2026/10/07 19:41:30 Metering ended from 192.168.1.88",
]


def _write_log(path, n):
    lines = []
    for i in range(n):
        lines.append(f"& 2026/10/02 14:{i // 60:02d}:{i % 60:02d} Play (1/{i})")
        lines.extend(NOISE)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_the_local_log_is_the_running_versions_tail_without_noise(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "hqplayer_data_dir", str(tmp_path))
    (tmp_path / "settings.xml").write_text('<?xml version="1.0"?><hqplayer><log enabled="1"/></hqplayer>')
    _write_log(tmp_path / "HQPlayer6Desktop.log", 300)
    (tmp_path / "HQPlayer5Desktop.log").write_text("& 2026/01/01 00:00:00 Play (5/0)\n")
    log = diag.read_log({"kind": "local", "where": str(tmp_path)}, {"version": "6"})
    assert log["note"] is None and log["where"].endswith("HQPlayer6Desktop.log")
    assert len(log["lines"]) == diag.LOG_LINES
    assert log["lines"][-1].endswith("Play (1/299)") and log["lines"][0].endswith("Play (1/100)")
    assert not any(diag._noise(line) for line in log["lines"])


def test_logging_off_and_an_empty_mount_say_so(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "hqplayer_data_dir", str(tmp_path))
    log = diag.read_log({"kind": "local", "where": str(tmp_path)}, {"version": "6"})
    assert log["lines"] == [] and "HQPLAYER_DATA_DIR" in log["note"]
    (tmp_path / "settings.xml").write_text('<hqplayer><log enabled="0"/></hqplayer>')
    log = diag.read_log({"kind": "local", "where": str(tmp_path)}, {"version": "6"})
    assert "off" in log["note"]


def test_a_desktop_elsewhere_has_no_log_here():
    log = diag.read_log({"kind": "none", "where": None}, {})
    assert log["lines"] == [] and "%LOCALAPPDATA%" in log["note"]


def test_the_embedded_log_page_is_read_and_filtered():
    body = "\n".join([*NOISE, "& 2026/10/02 12:48:17 Play (-1/0)",
                      "# 2026/10/02 12:48:18 NAA output network timeout"]).encode()

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        log = diag.read_log({"kind": "web", "where": f"http://127.0.0.1:{srv.server_port}/log"}, {})
    finally:
        srv.shutdown()
    assert log["lines"] == ["& 2026/10/02 12:48:17 Play (-1/0)",
                            "# 2026/10/02 12:48:18 NAA output network timeout"]
    assert log["cause"]["code"] == "naa_lost"


def test_a_start_the_meter_saw_and_no_status_called_playing_is_played():
    """Frames that resumed after the take while every status HQPlayer gave stayed on its opening tick, then
    stopped: no stop of its own to judge."""
    f = {"intent": "play", "started": 99.0, "t0": 100.0, "take": 99.9, "end": "window", "error": None, "slot": 1,
         "steps": [{"ts": 100.0, "command": "Play", "result": "OK", "message": "", "attributes": {}}],
         "adds": {"count": 1, "failed": []}, "misses": [], "proxy": None, "playlist": None, "handover": None,
         "mode": "path", "ticks": [_tick(100.2, "stopped", 0)], "meter": _seen((99.0, 100.5, 110.0)),
         "closed": 111.0}
    assert diag.verdict(f)["code"] == "played"
    assert diag.verdict({**f, "meter": None})["code"] == "not_played"


def test_frames_that_run_on_unnamed_keep_the_window_open_until_they_stop():
    """A session opened after the take whose frames came at once, while the status still says stopped: HQPlayer
    sounds and has yet to say what. The window waits while they run, up to its limit, and never calls it not
    played meanwhile."""
    att = diag.Attempt(intent="play", slot=2, started=99.0)
    att.t0, att.take = 100.0, 100.0
    shown = {"sessions": [{"open": 100.3, "first": 100.4, "closed": None}],
             "runs": [{"since": 100.3, "start": 100.4, "last": 110.9, "frames": 66}]}
    att.tick(_stopped_status(), "stopped", 111.0, shown)
    assert not att.moved()
    assert att.due(111.0) is None                     # past the window: the frames still ran at the last tick
    quiet = {**shown, "runs": [{**shown["runs"][0], "last": 109.0}]}
    att.tick(_stopped_status(), "stopped", 111.5, quiet)
    assert att.due(111.5) == "window"


def test_the_meter_hears_an_entry_run_out_from_its_first_sound_after_the_take():
    """A Status that keeps the position at 0: the sound from the entry's first after the take to the last that began
    by the last playing status, its silences included, against the entry's length. Sound the take stopped, the entry a
    jump left, is none of it, nor the build before the entry's first sound."""
    last = {"ts": 112.0, "position": 0.0, "length": 12.0}
    heard = dict(take=100.0, moved=False)
    assert diag._ran_out(last, _seen((99.0, 100.5, 106.0), (106.0, 106.8, 112.2)), **heard)   # a silence inside
    assert not diag._ran_out(last, _seen((99.0, 100.5, 105.0)), **heard)                      # it stopped early
    # a page's meter heard the album before this entry, gaplessly: 3 s of the entry are no 200 s
    assert not diag._ran_out({"ts": 303.0, "position": 0.0, "length": 200.0}, _seen((0.0, 0.5, 303.0)), take=300.0,
                             moved=False)
    assert not diag._ran_out(last, _seen((99.0, 100.5, 112.2)), take=None, moved=False)
    # the entry a jump left sounded 80 ms past the take, then a 3 s build: the 10 s entry stopped 6 s in
    short = {"ts": 109.0, "position": 0.0, "length": 10.0}
    assert not diag._ran_out(short, _seen((90.0, 90.5, 100.08), (100.08, 103.0, 109.0)), **heard)
    assert diag._ran_out({**short, "ts": 111.0}, _seen((90.0, 90.5, 100.08), (100.08, 103.0, 111.0)), **heard)


def _played_ticks(*rows, length=10.0):
    """Ticks as the poller records them: (ts, state, track, position); one naming no entry has no length."""
    return [{**_tick(t, state, track, position), "length": length if state == "playing" and track else 0.0, "speed": 1.0,
             "in_fill": -1.0, "out_fill": 0.9, "tracks_total": 2, "mode": "PCM", "filter": "poly-sinc-gauss-long",
             "shaper": "TPDF", "rate": 88200} for t, state, track, position in rows]


def _select(ticks, meter):
    return {"intent": "select", "started": 99.0, "t0": 100.0, "take": 100.0, "end": "stopped", "error": None,
            "slot": 2, "steps": [{"ts": 100.0, "command": "SelectTrack", "result": "OK", "message": "", "attributes": {}}],
            "adds": {"count": 1, "failed": []}, "misses": [], "proxy": None, "playlist": None, "handover": None,
            "mode": "path", "closed": 111.0, "ticks": ticks, "meter": meter}


def test_a_position_that_moved_judges_the_end_without_the_meter():
    """Where the position moves, the meter never measures the entry from the take: the stop 6 s into a 10 s entry
    is HQPlayer's, the build before its first sound in the meter's run or not."""
    assert not diag._ran_out({"ts": 112.0, "position": 5.0, "length": 12.0}, _seen((99.0, 100.5, 112.2)), take=100.0)
    stopped_short = _seen((90.0, 90.5, 103.0), (103.0, 103.6, 109.0))     # the page's run went on into the build
    ticks = _played_ticks((103.8, "playing", 2, 0.0), (106.0, "playing", 2, 2.5), (108.5, "playing", 2, 5.0),
                          (109.5, "stopped", 0, 0.0))
    assert diag.verdict(_select(ticks, stopped_short))["code"] == "not_played"


def test_an_entry_cut_short_then_hqplayers_own_next_is_judged_on_its_own_moved_position():
    """The entry played 3 s of 10 and its stream was cut; HQPlayer's own Next went on into the next entry, whose
    Status kept its position at 0 until it stopped too. The take's entry moved: its stop is HQPlayer's."""
    ticks = _played_ticks((101.0, "playing", 2, 0.5), (102.0, "playing", 2, 1.5), (103.0, "playing", 2, 2.5),
                          (104.0, "playing", 3, 0.0), (105.5, "stopped", 0, 0.0), length=6.0)
    assert diag.verdict(_select(ticks, _seen((100.0, 100.5, 105.0))))["code"] == "not_played"


def test_a_status_of_the_entry_tapped_again_read_before_its_restart_is_no_move():
    """The owner taps the entry that plays, at 4 s: a status read after the take still shows it there; the restart's
    own Status keeps 0 to its end while the meter hears it play out."""
    ticks = _played_ticks((100.1, "playing", 2, 4.1), (101.0, "playing", 2, 0.0), (103.0, "playing", 2, 0.0),
                          (106.0, "playing", 2, 0.0), (107.5, "stopped", 0, 0.0), length=6.0)
    meter = _seen((90.0, 90.5, 100.1), (100.1, 100.9, 106.9))
    assert diag.verdict(_select(ticks, meter))["code"] == "played"


def test_a_start_whose_only_playing_status_names_no_entry_has_no_stop_to_judge():
    """HQPlayer said it plays while it opened the entry (track 0), then no status until it stopped; the meter heard
    the entry from a quiet it watched."""
    ticks = _played_ticks((100.2, "playing", 0, 0.0), (107.5, "stopped", 0, 0.0))
    assert diag.verdict(_select(ticks, _seen((99.9, 100.5, 106.5))))["code"] == "played"


def test_a_status_of_the_entry_a_jump_left_is_no_move_of_the_new_one():
    """One status after the take still names the entry left behind, far into it; the new 6 s entry's own position
    stays at 0 while the meter hears it play out: played."""
    ticks = _played_ticks((100.3, "playing", 1, 120.0), (101.0, "playing", 2, 0.0), (103.0, "playing", 2, 0.0),
                          (106.0, "playing", 2, 0.0), (107.5, "stopped", 0, 0.0), length=6.0)
    meter = _seen((90.0, 90.5, 100.1), (100.1, 100.9, 107.0))
    assert diag.verdict(_select(ticks, meter))["code"] == "played"
