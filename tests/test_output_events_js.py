"""What another app did to the output, as the Web UI tells it (ROON-COEX
§12.6): backend/static/app-shell.js's output-events block, cut from the file
between its banners with the generic diagnosis toast beside it, runs under
Node against stubs while statuses are replayed — one toast per event and
page, none for an event minted before the page's connection (its floor),
the words with the action first — and player.js's status path hands the
floor on to the status the page really paints. Skipped where Node is not."""

import json
import shutil
import subprocess

import pytest

from tests.conftest import REPO_ROOT

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

STATIC = REPO_ROOT / "backend" / "static"

_SHELL = r"""
const fs = require('node:fs'), vm = require('node:vm');
const shell = fs.readFileSync(process.argv[process.argv.length - 1], 'utf8');
const start = shell.indexOf('  /* ---- Output events: what another app did to the output');
const end = shell.indexOf('  /* ---- Output events: end ---- */', start);
if (start < 0 || end < 0) throw new Error('the output-events block was not found');
// ...and the toast of a play the output judged failed, which stands down for these events
const dStart = shell.indexOf('  let lastDiagnosisSeen;');
const dEnd = shell.indexOf('  // A DSP change that went through a build answered its caller', dStart);
if (dStart < 0 || dEnd < 0) throw new Error('the diagnosis toast was not found');

function page() {
  const toasts = [], opened = [], on = {};
  const escape = s => String(s).replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);
  const ctx = {
    notices: { toast: t => toasts.push(t) },
    saidAttempts: new Set(), metDiagnoses: new Set(), escapeHtml: escape,
    fmtDuration: s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`,
    escapeProfileHtml: escape,
    openHqpDiagnosis: id => opened.push(['attempt', id]),
    openModal: html => { opened.push(['sheet', html.replace(/\s+/g, ' ')]);
                         return { el: { querySelector: () => ({ addEventListener() {}, focus() {} }) },
                                  close() {} }; },
    document: { addEventListener: (t, f) => { (on[t] = on[t] || []).push(f); } },
  };
  vm.runInNewContext(shell.slice(start, end) + shell.slice(dStart, dEnd).replace('const metDiagnoses = new Set();', '')
                     + '\n;this.outputLine = outputLine;', ctx);
  return { ctx, toasts, opened, status: d => on['np-update'].forEach(f => f({ detail: d })) };
}

const spec = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = {};
for (const [name, statuses] of Object.entries(spec.pages)) {
  const p = page();
  out[name] = statuses.map(st => {
    const before = p.toasts.length;
    p.status(st);
    return p.toasts.slice(before).map(t => ({ kind: t.kind, key: t.key, title: t.title, text: t.text,
                                             label: t.action && t.action.label }));
  });
  if (spec.press && spec.press[name] != null) {
    p.toasts[spec.press[name]].action.run();
    out[name + ':opened'] = p.opened;
    out[name + ':said'] = [...p.ctx.saidAttempts];
  }
}
const p = page();
out.lines = spec.lines.map(([st, diag]) => p.ctx.outputLine(st, diag));
process.stdout.write(JSON.stringify(out));
"""

OUT = {"type": "hqplayer", "label": "HQPlayer Desktop · study-pc"}
SPOT = {"index": 3, "position": 182}


def _st(event=None, **more):
    st = {"state": "stopped", "output": OUT, "resume": SPOT, **more}
    if event is not None:
        st["output_event"] = event
    return st


def _run(pages, lines=(), press=None):
    proc = subprocess.run([NODE, "-e", _SHELL, "--", str(STATIC / "app-shell.js")],
                          input=json.dumps({"pages": pages, "lines": list(lines), "press": press or {}}).encode(),
                          capture_output=True, check=True)
    return json.loads(proc.stdout)


TOOK = {"id": 11, "kind": "took"}
REFUSED = {"id": 12, "kind": "refused", "attempt": 12, "reason": "no output rate for 96 kHz in its output family"}
STOPPED = {"id": 13, "kind": "stopped", "attempt": 13}
PAUSED = {"id": 14, "kind": "paused"}


def test_an_event_is_one_toast_for_a_page_that_was_connected_when_it_happened():
    """The page is connected (its floor under the event's id); Roon takes
    HQPlayer: the status that first carries the event toasts — once. The
    statuses after it (a poll, a second read of the state: the same id while
    the state stands), and the status a reconnect of the stream opens with
    (a new floor, above the event), say nothing more."""
    out = _run({"p": [_st(event_floor=10, state="playing", resume=None),
                      _st(TOOK, source="external"), _st(TOOK, source="external"), _st(TOOK),
                      _st(TOOK, event_floor=20), _st(TOOK)]})
    said = out["p"]
    assert [len(t) for t in said] == [0, 1, 0, 0, 0, 0]
    assert said[1][0] == {"kind": "info", "key": "player.other_app", "label": "Why?",
                          "title": "Another app took HQPlayer Desktop · study-pc",
                          "text": 'Play takes it back and resumes at <span class="num">3:02</span>.'}


def test_a_page_opened_after_the_event_sees_the_state_and_no_stale_toast():
    """A page opened later — a reload, a second device — and a stream that
    reconnects after the event (a phone tab back from the background): an
    event minted before the connection (at or under its floor) is state,
    whichever status carries it — the one the connection opens with, or a
    later one that brings it back: the takeover's, when the refusal that
    stood over it at the connection ends; any, after an outage the page
    connected in (review round 1's finding 2). An event that happens after
    the connection toasts, once."""
    out = _run({"opened_later": [_st(TOOK, event_floor=11, source="external"), _st(TOOK, source="external"),
                                 _st(REFUSED), _st(REFUSED), _st(TOOK)],
                "opened_during_the_refusal": [_st(REFUSED, event_floor=12), _st(REFUSED), _st(TOOK), _st(TOOK)],
                "opened_during_an_outage": [{"state": "disconnected", "event_floor": 30}, _st(TOOK), _st(STOPPED),
                                            _st({"id": 31, "kind": "paused"}, state="paused")],
                "slept_through_it": [_st(event_floor=5, state="playing", resume=None),
                                     _st(STOPPED, event_floor=13), _st(STOPPED), _st(PAUSED, state="paused")]})
    assert [len(t) for t in out["opened_later"]] == [0, 0, 1, 0, 0]
    assert out["opened_later"][2][0]["title"] == "HQPlayer refused the other app's track"
    # Each connection's floor is its own: a backend back with its ids counting lower (its clock set back)
    # is not held to the floor of the connection before it
    back = _run({"p": [_st(event_floor=1_000_000), _st(event_floor=940_000),
                       _st({"id": 950_000, "kind": "took"}), _st({"id": 930_000, "kind": "paused"})]})
    assert [len(t) for t in back["p"]] == [0, 0, 1, 0]
    assert [len(t) for t in out["opened_during_the_refusal"]] == [0, 0, 0, 0]
    assert [len(t) for t in out["opened_during_an_outage"]] == [0, 0, 0, 1]
    assert [len(t) for t in out["slept_through_it"]] == [0, 0, 0, 1]


def test_an_event_with_a_diagnosis_is_one_toast_not_two():
    """The other app's stop of a take-back and its refused track ride the
    status with a diagnosis too (Now Playing's Why button): the event's
    toast says them, and the toast of a play the output judged failed stands
    down for those two codes — one toast per event, from one place. A failed
    play of the node's own keeps its toast."""
    stopped = {"id": 9, "code": "other_stopped", "title": "Another app stopped HQPlayer",
               "next": "Press Play to resume where it was.", "track_id": "t", "failing": False}
    refused = {"id": 12, "code": "other_refused", "title": "HQPlayer refused the other app's track", "next": "",
               "track_id": None, "failing": False}
    own = {"id": 40, "code": "not_played", "title": "HQPlayer could not play it", "next": "", "track_id": "t",
           "failing": False}
    out = _run({"p": [_st(), _st({"id": 13, "kind": "stopped", "attempt": 9}, diagnosis=stopped),
                      _st(REFUSED, diagnosis=refused), _st(diagnosis=own)]})
    assert [[t["key"] for t in said] for said in out["p"]] == [
        [], ["player.other_app"], ["player.other_app"], ["hqplayer.diagnosis"]]


def test_each_event_says_what_happened_and_what_play_does_and_opens_its_sheet():
    """The four events' words (drafts): what happened in the title, what Play
    does in the text; the other app's stop of the person's own Play is the
    one outcome (error). Why? opens the attempt's sheet where the event has
    an attempt — said then, so the play route's answer for it says nothing
    more — else a sheet with the node's two sentences. A device's own name
    and HQPlayer's reason are escaped."""
    kann = {"type": "dlna", "label": "KANN <ULTRA>"}
    pages = {"took": [_st(), _st(TOOK, source="external")],
             "refused": [_st(), _st(REFUSED)],
             "stopped": [_st(), _st(STOPPED, resume={"index": 1, "position": 47})],
             "paused": [_st(output=kann), _st(PAUSED, state="paused", resume=None, output=kann)]}
    out = _run(pages, press={k: 0 for k in pages})
    toast = {k: out[k][1][0] for k in pages}
    assert (toast["refused"]["kind"], toast["refused"]["text"]) == (
        "info", "No output rate for 96 kHz in its output family. "
                'Play takes HQPlayer Desktop · study-pc back at <span class="num">3:02</span>.')
    assert (toast["stopped"]["kind"], toast["stopped"]["title"], toast["stopped"]["text"]) == (
        "error", "Another app stopped HQPlayer Desktop · study-pc", 'Play resumes at <span class="num">0:47</span>.')
    assert (toast["paused"]["kind"], toast["paused"]["title"]) == ("info", "Paused on KANN &#60;ULTRA&#62;")
    assert toast["paused"]["text"].endswith("Play resumes.")
    assert {t["key"] for t in toast.values()} == {"player.other_app"}
    assert {t["label"] for t in toast.values()} == {"Why?"}
    assert out["refused:opened"] == [["attempt", 12]] and out["refused:said"] == [12]
    assert out["stopped:opened"] == [["attempt", 13]] and out["stopped:said"] == [13]
    (kind, sheet), = out["took:opened"]
    assert kind == "sheet" and "Another app took HQPlayer Desktop · study-pc" in sheet
    assert "Sautium sends HQPlayer Desktop · study-pc nothing while that app plays" in sheet
    assert "Press Play to take HQPlayer Desktop · study-pc back" in sheet and out["took:said"] == []
    (kind, sheet), = out["paused:opened"]
    assert kind == "sheet" and "KANN &#60;ULTRA&#62; paused this track by itself" in sheet
    assert "<ULTRA>" not in sheet


def test_now_playings_line_says_the_action_first():
    """The line beside the track on Now Playing: what Play does first — the
    part a narrow screen must not lose — then whose the output is; the
    owner's phone cut "Another app took HQPlayer Desktop · study-pc — Play t…"
    before the action."""
    stopped_by = {"id": 13, "code": "other_stopped"}
    out = _run({}, lines=[
        (_st(source="external"), None),
        (_st(source="external", resume=None, state="playing"), None),
        (_st(resume={"index": 1, "position": 47}), stopped_by),
        (_st(resume={"index": 5, "position": 5}), None),
        (_st(resume={"index": 4, "position": 0}), None),
        (_st(state="paused", resume=None, paused_on_device=True, output={"label": "KANN_ULTRA"}), None),
        (_st(state="playing", resume=None), None),
    ])
    assert out["lines"] == [
        "Play resumes at 3:02 · another app has HQPlayer Desktop · study-pc",
        "Play takes it back · another app has HQPlayer Desktop · study-pc",
        "Play resumes at 0:47 · another app stopped HQPlayer Desktop · study-pc",
        "Play resumes at 0:05",
        "",
        "Paused on KANN_ULTRA",
        "",
    ]


_PLAYER = r"""
const fs = require('node:fs'), vm = require('node:vm');
const painted = [];
let onMessage, release;
const window = {
  addEventListener() {}, dispatchEvent() {}, __SAUTIUM_BUILD: 1,
  sseStream: (path, onMsg) => { onMessage = onMsg; return { abort() {} }; },
};
const document = {
  readyState: 'complete', visibilityState: 'visible', addEventListener() {},
  dispatchEvent: e => { if (e.type === 'np-update') painted.push(e.detail); },
};
// The playlist the first status waits for hangs until released
let hang = false;
const fetch = async (url) => {
  if (hang && String(url).includes('/api/player/playlist')) {
    hang = false;
    await new Promise(r => { release = r; });
  }
  return { ok: true, status: 200, json: async () => ({ tracks: [] }) };
};
class CustomEvent { constructor(type, init) { this.type = type; this.detail = init && init.detail; } }
const sessionStorage = { getItem: () => null, setItem() {} };
vm.runInNewContext(fs.readFileSync(process.argv[process.argv.length - 1], 'utf8'), {
  window, document, fetch, sessionStorage, crypto: require('node:crypto').webcrypto, CustomEvent,
  console: { warn() {}, log() {}, error() {}, debug() {} }, setTimeout, clearTimeout, location: { reload() {} },
});
const send = msg => onMessage({ data: JSON.stringify(msg) });
const tick = () => new Promise(r => setTimeout(r, 20));
(async () => {
  if (typeof onMessage !== 'function') throw new Error('the page opened no event stream');
  const steps = JSON.parse(fs.readFileSync(0, 'utf8'));
  await tick();
  hang = true;
  for (const step of steps) {
    if (step === 'release') release(); else send(step);
    await tick();
  }
  process.stdout.write(JSON.stringify(painted.map(d => [d.n, d.event_floor == null ? null : d.event_floor])));
})();
"""


def test_a_connections_floor_reaches_the_page_when_its_first_status_is_superseded():
    """player.js: the status a connection opens with waits for the playlist;
    a newer status arrives meanwhile and supersedes it (the newest speaks).
    The floor that status carried is the connection's: the status painted in
    its place hands it on, so an event from before the connection toasts
    nothing. The status after it carries none; the next connection's first
    carries its own."""
    def status(n, **more):
        return {"t": "status", "d": {"state": "stopped", "n": n, "playlist_version": 7, **more}}
    proc = subprocess.run(
        [NODE, "-e", _PLAYER, "--", str(STATIC / "player.js")],
        input=json.dumps([{"t": "hello"}, status(1, event_floor=100), status(2), "release", status(3),
                          {"t": "hello"}, status(4, event_floor=200), status(5)]).encode(),
        capture_output=True, check=True)
    assert json.loads(proc.stdout) == [[2, 100], [3, None], [4, 200], [5, None]], proc.stdout


def test_now_playings_line_may_shrink_and_wrap_inside_its_tag():
    """The line is a sentence in a badge: the badge's own rule keeps it from
    shrinking (a quality tag never wraps), so the line's class must let it —
    it ran past the phone's right edge, cut before what Play does. Rendered
    at 360 and 390 px in the slice's record; pinned here as the stylesheet's
    words."""
    import re
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    tag = re.search(r'<span class="([^"]*)" id="npHqpTag"', html).group(1).split()
    assert {"np-q-badge", "np-output-line"} <= set(tag)
    rule = re.search(r"\.np-q-badge\.np-output-line\s*\{([^}]*)\}", css).group(1)
    props = dict(p.strip().split(":", 1) for p in rule.split(";") if ":" in p)
    assert props["flex-shrink"].strip() == "1" and props["min-width"].strip() == "0"
    assert props["overflow-wrap"].strip() == "anywhere" and "white-space" not in props
    assert css.index(".np-q-badge.np-output-line") > css.index(".np-q-badge {")      # after the badge's own rule
