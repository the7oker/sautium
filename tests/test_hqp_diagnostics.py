"""The HQPlayer playback trace's rules (backend/playback/hqp_diagnostics.py):
the verdict on recorded traces — every cause, and the normal plays that a
naive order would misread — the failing run behind the notice, the rings'
sizes, redaction on the way out, and HQPlayer's own log read and classified
(lines as the maintainer's Desktop 5/6 and Embedded installations print them)."""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
    v = diag.verdict(case["facts"])
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
                     "rejected", "external", "not_played", "unknown", "interrupted"}


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
    assert "poly-sinc-gauss-xla" in v["sentence"] and "22.5792 MHz" in v["sentence"]
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
