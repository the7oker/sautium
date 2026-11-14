"""The output's words in the Web UI (ROON-COEX §12.6, §12.7; BACKLOG B-87,
B-89): what another app did to the output, a play HQPlayer did not carry out
and a condition the output is in for a while are toasts — lasting ones, on
screen until their close or until the status says the condition no more —
and Now Playing shows the player alone.

backend/static/app-shell.js's `notices` object, its block "The output's
words" and the diagnosis toast beside it are cut from the file between their
banners and run under Node over a DOM of stubs and a clock the test turns,
while statuses are replayed: what is on screen after each, and what a press
does. player.js's status path hands a connection's floor on to the status
the page really paints. Skipped where Node is not."""

import json
import re
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
const cut = (from, to) => {
  const a = shell.indexOf(from), b = shell.indexOf(to, a);
  if (a < 0 || b < 0) throw new Error('marker not found: ' + (a < 0 ? from : to));
  return shell.slice(a, b);
};

function page(cap) {
  const opened = [], on = {}, stack = [];
  let now = 0, timers = [], seq = 0;
  const escape = s => String(s).replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);
  // An element as the toasts use one: its HTML kept as text, a button found
  // by the marker its selector names, its listeners kept to be pressed
  const element = () => {
    const classes = new Set(), listeners = {}, buttons = {};
    const el = {
      innerHTML: '', attrs: {},
      set className(v) { classes.clear(); String(v).split(/\s+/).filter(Boolean).forEach(c => classes.add(c)); },
      get className() { return [...classes].join(' '); },
      classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c) },
      setAttribute(k, v) { el.attrs[k] = v; },
      addEventListener(type, fn) { listeners[type] = fn; },
      getBoundingClientRect() { return {}; },
      remove() { const i = stack.indexOf(el); if (i >= 0) stack.splice(i, 1); },
      querySelector(sel) {
        const marker = sel === '.toast-close' ? 'class="toast-close"' : sel.slice(1, -1);
        if (!el.innerHTML.includes(marker)) return null;
        return { addEventListener(type, fn) { buttons[sel] = fn; } };
      },
      press(sel) { buttons[sel](); },
    };
    return el;
  };
  const ctx = {
    saidAttempts: new Set(), escapeHtml: escape, escapeProfileHtml: escape,
    fmtDuration: s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`,
    openHqpDiagnosis: id => opened.push(['attempt', id]),
    openHqpSettingsAt: picker => opened.push(['settings', picker]),
    openModal: html => { opened.push(['sheet', html.replace(/\s+/g, ' ')]);
                         return { el: { querySelector: () => ({ addEventListener() {}, focus() {} }) },
                                  close() {} }; },
    modalIsOpen: () => false, layoutMode: () => (cap === 3 ? 'tablet' : 'compact'),
    noticeCopy: () => ({}), guide: {}, fetch: async () => ({ ok: false }),
    sessionStorage: { getItem: () => null, setItem() {} },
    window: { dispatchEvent() {} }, CustomEvent: class {},
    document: {
      addEventListener: (t, f) => { (on[t] = on[t] || []).push(f); },
      createElement: element,
      getElementById: () => ({ appendChild: el => stack.push(el) }),
    },
    setTimeout: (fn, ms) => { timers.push({ id: ++seq, at: now + ms, fn }); return seq; },
    clearTimeout: id => { timers = timers.filter(t => t.id !== id); },
  };
  vm.runInNewContext([
    cut('  const notices = {', '\n  window.notices = notices;'),
    cut("  /* ---- The output's words: what happened is a toast", "\n  /* ---- The output's words: end"),
    cut('  let diagnosisSeen = false;', '\n  // A DSP change that went through a build answered its caller'),
    'this.notices = notices;',
  ].join('\n'), ctx);
  const buttons = html => [...html.matchAll(/<button class="toast-(action|close)"[^>]*>([^<]*)</g)]
    .map(m => (m[1] === 'close' ? '×' : m[2]));
  return {
    ctx, opened,
    status: d => on['np-update'].forEach(f => f({ detail: d })),
    // The clock moves: every timer due by then fires, in its order
    pass(ms) {
      const until = now + ms;
      for (;;) {
        const due = timers.filter(t => t.at <= until).sort((a, b) => a.at - b.at || a.id - b.id)[0];
        if (!due) break;
        timers = timers.filter(t => t !== due);
        now = due.at;
        due.fn();
      }
      now = until;
    },
    shown: () => ctx.notices._shown.map(t => ({
      key: t.key, kind: t.kind, title: t.title, text: t.text, lasting: !!t.lasting,
      taps: t.el.classList.contains('is-actionable'), buttons: buttons(t.el.innerHTML) })),
    waiting: () => ctx.notices._queue.map(t => t.key),
    press(key, sel) { ctx.notices._shown.find(t => t.key === key).el.press(sel); },
    // The two taps of a double-tap: the second lands on the toast as it leaves
    double(key, sel) { const el = ctx.notices._shown.find(t => t.key === key).el; el.press(sel); el.press(sel); },
  };
}

const spec = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = {};
for (const [name, steps] of Object.entries(spec.pages)) {
  const p = page(spec.cap || 1);
  out[name] = steps.map(step => {
    if (step.pass != null) p.pass(step.pass);
    else if (step.press) p.press(step.press[0], step.press[1]);
    else if (step.double) p.double(step.double[0], step.double[1]);
    else if (step.toast) p.ctx.notices.toast(step.toast);
    else p.status(step);
    p.pass(0);
    return { shown: p.shown(), waiting: p.waiting() };
  });
  out[name + ':opened'] = p.opened;
  out[name + ':said'] = [...p.ctx.saidAttempts];
}
process.stdout.write(JSON.stringify(out));
"""

OUT = {"type": "hqplayer", "label": "HQPlayer Desktop · study-pc"}
SPOT = {"index": 3, "position": 182}
WHY, ACTION, CLOSE = '[data-toast="why"]', '[data-toast="action"]', ".toast-close"


def _st(event=None, **more):
    st = {"state": "stopped", "output": OUT, "resume": SPOT, **more}
    if event is not None:
        st["output_event"] = event
    return st


def _run(pages, cap=1):
    proc = subprocess.run([NODE, "-e", _SHELL, "--", str(STATIC / "app-shell.js")],
                          input=json.dumps({"pages": pages, "cap": cap}).encode(), capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()
    return json.loads(proc.stdout)


def _keys(steps):
    return [[t["key"] for t in step["shown"]] for step in steps]


TOOK = {"id": 11, "kind": "took"}
REFUSED = {"id": 12, "kind": "refused", "attempt": 12, "reason": "no output rate for 96 kHz in its output family",
           "cause": "rate"}
STOPPED = {"id": 13, "kind": "stopped", "attempt": 13}
PAUSED = {"id": 14, "kind": "paused"}
PLAYING = {"state": "playing", "output": OUT}


def test_an_event_is_one_toast_that_stays_until_play():
    """The page is connected (its floor under the event's id); Roon takes
    HQPlayer: the status that first carries the event raises its toast —
    one, with Why? and a close, on screen through the statuses after it and
    past any timer (the addendum of 2026-11-13: the explanation lives in the
    toast, so it stays until dismissed or until Play). The node's own Play
    ends the state, and the toast with it."""
    out = _run({"p": [_st(event_floor=10, state="playing", resume=None),
                      _st(TOOK, source="external"), _st(TOOK, source="external"), {"pass": 60_000}, _st(TOOK),
                      PLAYING, {"pass": 1_000}]})
    assert _keys(out["p"]) == [[], ["player.other_app"], ["player.other_app"], ["player.other_app"],
                               ["player.other_app"], [], []]
    assert out["p"][1]["shown"][0] == {
        "key": "player.other_app", "kind": "info", "lasting": True, "taps": True, "buttons": ["Why?", "×"],
        "title": "Another app took HQPlayer Desktop · study-pc",
        "text": 'Play takes it back and resumes at <span class="num">3:02</span>.'}


def test_a_toast_the_person_closed_stays_closed_and_its_words_follow_the_spot():
    """The same event on every later status raises nothing again once the
    person closed its toast; while it is up, a Seek at the spot (the bar on
    Now Playing is the spot's) redraws the second in place."""
    moved = {"index": 3, "position": 200}
    out = _run({"closed": [_st(event_floor=10), _st(TOOK), {"press": ["player.other_app", CLOSE]}, {"pass": 500},
                           _st(TOOK), _st(TOOK, resume=moved)],
                "moved": [_st(event_floor=10), _st(TOOK), _st(TOOK, resume=moved)]})
    assert _keys(out["closed"]) == [[], ["player.other_app"], [], [], [], []]
    assert out["moved"][2]["shown"][0]["text"] == 'Play takes it back and resumes at <span class="num">3:20</span>.'
    assert out["moved"][2]["shown"][0]["buttons"] == ["Why?", "×"]


def test_a_page_opened_after_the_event_sees_no_message():
    """A page opened later — a reload, a second device — and a stream that
    reconnects after the event (a phone tab back from the background): an
    event minted before the connection (at or under its floor) is state for
    Now Playing's bar, whichever status carries it — the one the connection
    opens with, or a later one that brings it back: the takeover's, when the
    refusal that stood over it at the connection ends; any, after an outage
    the page connected in. An event that happens after the connection is
    told, once."""
    out = _run({"opened_later": [_st(TOOK, event_floor=11, source="external"), _st(TOOK, source="external"),
                                 _st(REFUSED), _st(REFUSED), _st(TOOK)],
                "opened_during_the_refusal": [_st(REFUSED, event_floor=12), _st(REFUSED), _st(TOOK), _st(TOOK)],
                "opened_during_an_outage": [{"state": "disconnected", "event_floor": 30}, _st(TOOK), _st(STOPPED),
                                            _st({"id": 31, "kind": "paused"}, state="paused")],
                "slept_through_it": [_st(event_floor=5, state="playing", resume=None),
                                     _st(STOPPED, event_floor=13), _st(STOPPED), _st(PAUSED, state="paused")],
                # Each connection's floor is its own: a backend back with its ids counting lower (its
                # clock set back) is not held to the floor of the connection before it
                "clock_set_back": [_st(event_floor=1_000_000), _st(event_floor=940_000),
                                   _st({"id": 950_000, "kind": "took"}), _st({"id": 930_000, "kind": "paused"})]})
    # the refusal told goes with its state; the takeover under it, from before the connection, says nothing
    assert _keys(out["opened_later"]) == [[], [], ["player.other_app"], ["player.other_app"], []]
    assert out["opened_later"][2]["shown"][0]["title"] == "HQPlayer refused the other app's track"
    assert _keys(out["opened_during_the_refusal"]) == [[], [], [], []]
    assert _keys(out["opened_during_an_outage"]) == [[], [], [], ["player.other_app"]]
    assert _keys(out["slept_through_it"]) == [[], [], [], ["player.other_app"]]
    assert _keys(out["clock_set_back"]) == [[], [], ["player.other_app"], []]


def test_an_event_gone_from_the_status_and_back_is_no_news():
    """A status that says the event no more ends its toast — an outage
    between two statuses among them — and the event's return is no news to
    the page that told it: the toast is not raised a second time."""
    out = _run({"p": [_st(event_floor=10), _st(TOOK), {"state": "disconnected", "output": OUT}, _st(TOOK), _st(TOOK)]})
    assert _keys(out["p"]) == [[], ["player.other_app"], [], [], []]


def test_a_takeover_stands_in_the_toast_again_once_the_refusal_over_it_ended():
    """Roon takes HQPlayer (told), HQPlayer refuses Roon's track (told: the
    toast redrawn in place), then Roon plays a track that works: the status
    says the takeover again, under its old id. That app holds the output
    still and nobody pressed Play — the toast stands as the takeover again,
    in place (review round 1's high: it left, and Now Playing says nothing
    by design). A toast the person closed is not raised for it; and its
    words still follow the spot."""
    start = _st(event_floor=10)
    out = _run({"stands": [start, _st(TOOK), _st(REFUSED), _st(TOOK), _st(TOOK, resume={"index": 3, "position": 200}),
                           PLAYING],
                "closed": [start, _st(TOOK), _st(REFUSED), {"press": ["player.other_app", CLOSE]}, {"pass": 500},
                           _st(TOOK), _st(TOOK)],
                "closed_before": [start, _st(TOOK), {"press": ["player.other_app", CLOSE]}, {"pass": 500},
                                  _st(REFUSED), _st(TOOK)]})
    assert [[t["title"] for t in step["shown"]] for step in out["stands"]] == [
        [], ["Another app took HQPlayer Desktop · study-pc"], ["HQPlayer refused the other app's track"],
        ["Another app took HQPlayer Desktop · study-pc"], ["Another app took HQPlayer Desktop · study-pc"], []]
    assert out["stands"][3]["shown"][0]["buttons"] == ["Why?", "×"]
    assert out["stands"][4]["shown"][0]["text"].endswith('<span class="num">3:20</span>.')
    assert _keys(out["closed"]) == [[], ["player.other_app"], ["player.other_app"], [], [], [], []]
    # the refusal is news of its own after a closed takeover; the takeover's return rides in its toast
    assert [[t["title"] for t in step["shown"]] for step in out["closed_before"]][4:] == [
        ["HQPlayer refused the other app's track"], ["Another app took HQPlayer Desktop · study-pc"]]


def test_each_event_says_what_happened_and_what_play_does_and_opens_its_sheet():
    """The four events' words (drafts): what happened in the title, what Play
    does in the text; the other app's stop of the person's own Play is the
    one outcome (error). Why? opens the attempt's sheet where the event has
    an attempt — said then, so the play route's answer for it says nothing
    more — else a sheet with the node's two sentences. A device's own name
    and HQPlayer's reason are escaped."""
    kann = {"type": "dlna", "label": "KANN <ULTRA>"}
    pages = {"took": [_st(event_floor=1), _st(TOOK, source="external")],
             "refused": [_st(event_floor=1), _st(REFUSED)],
             "stopped": [_st(event_floor=1), _st(STOPPED, resume={"index": 1, "position": 47})],
             "paused": [_st(event_floor=1, output=kann), _st(PAUSED, state="paused", resume=None, output=kann)]}
    out = _run({k: v + [{"press": ["player.other_app", WHY]}] for k, v in pages.items()})
    toast = {k: out[k][1]["shown"][0] for k in pages}
    assert (toast["refused"]["kind"], toast["refused"]["text"]) == (
        "info", "No output rate for 96 kHz in its output family. "
                'Play takes HQPlayer Desktop · study-pc back at <span class="num">3:02</span>.')
    assert (toast["stopped"]["kind"], toast["stopped"]["title"], toast["stopped"]["text"]) == (
        "error", "Another app stopped HQPlayer Desktop · study-pc", 'Play resumes at <span class="num">0:47</span>.')
    assert (toast["paused"]["kind"], toast["paused"]["title"]) == ("info", "Paused on KANN &#60;ULTRA&#62;")
    assert toast["paused"]["text"].endswith("Play resumes.")
    assert all(t["lasting"] and t["buttons"][0] == "Why?" and t["buttons"][-1] == "×" for t in toast.values())
    assert out["refused:opened"] == [["attempt", 12]] and out["refused:said"] == [12]
    assert out["stopped:opened"] == [["attempt", 13]] and out["stopped:said"] == [13]
    (kind, sheet), = out["took:opened"]
    assert kind == "sheet" and "Another app took HQPlayer Desktop · study-pc" in sheet
    assert "Sautium sends HQPlayer Desktop · study-pc nothing while that app plays" in sheet
    assert "Press Play to take HQPlayer Desktop · study-pc back" in sheet and out["took:said"] == []
    (kind, sheet), = out["paused:opened"]
    assert kind == "sheet" and "KANN &#60;ULTRA&#62; paused this track by itself" in sheet
    assert "<ULTRA>" not in sheet
    # a press on Why? takes the toast with it, as a press on any toast's button does
    assert all(out[k][2]["shown"] == [] for k in pages)


def test_a_refused_setting_offers_hqplayers_settings_at_its_picker():
    """BACKLOG B-89: HQPlayer refused a track because the chosen filter, the
    modulator or the output rate family does not fit its rate — the other
    app's (the event's `cause`) or the node's own (a rate_refused
    diagnosis's): the toast carries one action after Why?, "HQPlayer
    settings", which opens the picker that needs changing. Two buttons at
    most, in one order. A refusal that names no setting offers none."""
    filt = {**REFUSED, "id": 15, "attempt": 15, "reason": "its filter is not possible at 96 kHz", "cause": "filter"}
    bare = {**REFUSED, "id": 16, "attempt": 16, "cause": None}

    def own(i, code, cause):
        return {"id": i, "code": code, "title": "HQPlayer refused the rate combination", "next": "Pick another.",
                "track_id": "t", "failing": False, "cause": cause}

    start = _st(event_floor=1)
    pages = {"rate": [start, _st(REFUSED)], "filter": [start, _st(filt)], "bare": [start, _st(bare)],
             "own_filter": [start, _st(diagnosis=own(40, "rate_refused", "filter"))],
             "own_line": [start, _st(diagnosis=own(41, "rate_refused", "line"))],
             "own_modulator": [start, _st(diagnosis=own(42, "rate_refused", "modulator"))],
             "own_other": [start, _st(diagnosis=own(43, "not_played", "filter"))]}
    key = {k: ("hqplayer.diagnosis" if k.startswith("own") else "player.other_app") for k in pages}
    with_action = [k for k in pages if k not in ("bare", "own_other")]
    out = _run({k: v + ([{"press": [key[k], ACTION]}] if k in with_action else []) for k, v in pages.items()})
    for k in with_action:
        assert out[k][1]["shown"][0]["buttons"] == ["Why?", "HQPlayer settings", "×"], k
    assert out["bare"][1]["shown"][0]["buttons"] == out["own_other"][1]["shown"][0]["buttons"] == ["Why?", "×"]
    assert [out[k + ":opened"] for k in with_action] == [
        [["settings", "rate"]], [["settings", "filter"]], [["settings", "filter"]], [["settings", "filter"]],
        [["settings", "modulator"]]]


def test_a_failed_plays_toast_stays_until_the_status_offers_it_no_more():
    """A play HQPlayer did not carry out: one lasting toast with Why? — Now
    Playing has no button for it — until the status offers the diagnosis no
    more (a new start, the slot playing after all). The other app's stop and
    its refused track ride the status with a diagnosis too: the event's
    toast says them, and this one stands down — one toast per event, from
    one place. The status a page connects with says nothing."""
    stopped = {"id": 9, "code": "other_stopped", "title": "Another app stopped HQPlayer",
               "next": "Press Play to resume where it was.", "track_id": "t", "failing": False}
    refused = {"id": 12, "code": "other_refused", "title": "HQPlayer refused the other app's track", "next": "",
               "track_id": None, "failing": False}
    own = {"id": 40, "code": "not_played", "title": "HQPlayer could not play it", "next": "Play starts it again.",
           "track_id": "t", "failing": False}
    out = _run({"events": [_st(event_floor=1), _st({"id": 13, "kind": "stopped", "attempt": 9}, diagnosis=stopped),
                           _st(REFUSED, diagnosis=refused)],
                "own": [_st(event_floor=1), _st(diagnosis=own), {"pass": 60_000}, _st(diagnosis=own), PLAYING],
                "opened_later": [_st(event_floor=1, diagnosis=own), _st(diagnosis=own), _st(), _st(diagnosis=own)]},
               cap=3)
    assert _keys(out["events"]) == [[], ["player.other_app"], ["player.other_app"]]
    assert _keys(out["own"]) == [[], ["hqplayer.diagnosis"], ["hqplayer.diagnosis"], ["hqplayer.diagnosis"], []]
    assert out["own"][1]["shown"][0] == {
        "key": "hqplayer.diagnosis", "kind": "error", "lasting": True, "taps": True, "buttons": ["Why?", "×"],
        "title": "HQPlayer could not play it", "text": "Play starts it again."}
    assert _keys(out["opened_later"]) == [[], [], [], []]


def test_the_route_answers_toast_under_the_diagnosis_key_is_not_ended_for_it():
    """The play route's answer says a refusal under the diagnosis's key, and
    the diagnosis takes it over in place when it comes. A status that offers
    no diagnosis ends the diagnosis's own toast — never the answer's toast
    for a newer attempt that took the place meanwhile."""
    own = {"id": 40, "code": "not_played", "title": "HQPlayer could not play it", "next": "", "track_id": "t",
           "failing": False}
    answer = {"toast": {"kind": "error", "key": "hqplayer.diagnosis", "title": "HQPlayer did not take the Play",
                        "text": "Empty transport"}}
    out = _run({"p": [_st(event_floor=1), _st(diagnosis=own), answer, _st(), {"pass": 9_000}]})
    assert [[t["title"] for t in step["shown"]] for step in out["p"]] == [
        [], ["HQPlayer could not play it"], ["HQPlayer did not take the Play"], ["HQPlayer did not take the Play"],
        []]


def test_a_condition_the_output_is_in_is_a_toast_while_it_lasts():
    """What stood beside the track on Now Playing while it lasted (B-87): a
    build HQPlayer answers nothing in, the queue it takes again, a renderer
    being attached again — each a toast that takes no taps and goes with the
    condition; a build HQPlayer did not finish and a silence no build
    explains carry a close. A new build is a new toast."""
    base = {"state": "stopped", "output": OUT}
    build = {"since": 100.0, "command": "Play", "silent": True}
    out = _run({"build": [base, {**base, "building": build}, {"pass": 120_000}, {**base, "building": build}, base,
                          {**base, "building": {**build, "since": 200.0}},
                          {**base, "building": {**build, "since": 200.0, "unfinished": True}}, base],
                "embedded": [base, {**base, "building": {"since": 5.0, "command": None, "silent": False}}],
                "silent": [base, {**base, "not_answering": "HQPlayer is not answering"}, base],
                "rebuilding": [base, {**base, "rebuilding": True}, base],
                "connecting": [base, {**base, "connecting": "KANN <ULTRA>"}, base]}, cap=3)
    assert _keys(out["build"]) == [[], ["hqplayer.building"], ["hqplayer.building"], ["hqplayer.building"], [],
                                   ["hqplayer.building"], ["hqplayer.building"], []]
    assert out["build"][1]["shown"][0] == {
        "key": "hqplayer.building", "kind": "info", "lasting": True, "taps": False, "buttons": [],
        "title": "HQPlayer is building the filter", "text": "It takes commands again once it is done."}
    assert out["build"][6]["shown"][0] == {
        "key": "hqplayer.building", "kind": "error", "lasting": True, "taps": True, "buttons": ["×"],
        "title": "HQPlayer did not finish building the filter", "text": "Restart HQPlayer."}
    assert out["embedded"][1]["shown"][0]["text"] == ""         # an Embedded box answers through its build
    assert out["silent"][1]["shown"][0]["buttons"] == ["×"] and _keys(out["silent"])[2] == []
    assert out["rebuilding"][1]["shown"][0]["title"] == "HQPlayer is taking the queue again"
    assert out["connecting"][1]["shown"][0] == {
        "key": "player.connecting", "kind": "info", "lasting": True, "taps": False, "buttons": [],
        "title": "Connecting to KANN &#60;ULTRA&#62;…", "text": ""}
    assert _keys(out["rebuilding"])[2] == _keys(out["connecting"])[2] == []


def test_news_never_waits_behind_a_lasting_toast():
    """A phone shows one toast. A lasting one on screen steps aside for
    news — the news shows at once, the lasting toast waits behind it and is
    back when the news' time is up — unless its condition ended meanwhile.
    One that stepped aside displaces nothing itself."""
    news = {"toast": {"kind": "info", "key": "queue", "title": "Added to the queue"}}
    later = {"toast": {"kind": "info", "key": "copy", "title": "Copied"}}
    out = _run({"back": [_st(event_floor=1), _st(TOOK), news, {"pass": 9_000}, later, {"pass": 9_000}],
                "ended": [_st(event_floor=1), _st(TOOK), news, PLAYING, {"pass": 9_000}],
                "two_lasting": [_st(event_floor=1), _st(TOOK),
                                _st(TOOK, building={"since": 1.0, "command": "Play", "silent": True}),
                                {"pass": 60_000}, _st(TOOK), {"pass": 1_000}]})
    assert _keys(out["back"]) == [[], ["player.other_app"], ["queue"], ["player.other_app"], ["copy"],
                                  ["player.other_app"]]
    assert out["back"][2]["waiting"] == ["player.other_app"] and out["back"][3]["waiting"] == []
    assert _keys(out["ended"]) == [[], ["player.other_app"], ["queue"], ["queue"], []]
    assert out["ended"][3]["waiting"] == []
    # a newer condition shows; the older waits behind it and returns once the newer ended and left
    assert _keys(out["two_lasting"]) == [[], ["player.other_app"], ["hqplayer.building"], ["hqplayer.building"],
                                         [], ["player.other_app"]]
    assert [step["waiting"] for step in out["two_lasting"]][2:] == [["player.other_app"]] * 3 + [[]]


def test_a_double_tap_on_a_toasts_button_is_one_press():
    """§ "A double-tap is one tap": the second tap lands on the toast as it
    leaves (its element keeps taking taps through the leave) and does
    nothing — one sheet, one route."""
    out = _run({"why": [_st(event_floor=1), _st(REFUSED), {"double": ["player.other_app", WHY]}],
                "action": [_st(event_floor=1), _st(REFUSED), {"double": ["player.other_app", ACTION]}]})
    assert out["why:opened"] == [["attempt", 12]] and out["action:opened"] == [["settings", "rate"]]


def test_what_waits_for_its_turn_keeps_its_order_and_its_words():
    """The queue behind a phone's one place: news goes before a lasting
    toast that stepped aside, however many come; a repeat for a key that
    waits redraws the one that waits; a lasting toast's words changed while
    it waits are the ones shown when it is back; a toast without buttons
    steps aside as any lasting one; and a new instance for a key whose
    toast stepped aside is news itself."""
    def toast(key, title, **more):
        return {"toast": {"kind": "info", "key": key, "title": title, **more}}
    build = {"since": 1.0, "command": "Play", "silent": True}
    out = _run({
        # two pieces of news over a lasting toast: both before it comes back
        "order": [_st(event_floor=1), _st(TOOK), toast("a", "First"), toast("b", "Second"), {"pass": 5_000},
                  {"pass": 5_000}],
        # a repeat for the key that waits
        "repeat": [toast("a", "First"), toast("b", "Second"), toast("b", "Second, again"), {"pass": 9_000}],
        # the spot moved while the takeover's toast waited behind news
        "amended": [_st(event_floor=1), _st(TOOK), toast("a", "First"), _st(TOOK, resume={"index": 3, "position": 200}),
                    {"pass": 9_000}],
        # a build's toast (no buttons) steps aside too
        "passive": [{"state": "playing", "output": OUT, "building": build}, toast("a", "First")],
        # a refusal while the takeover's toast waits behind news: news itself, ahead of what else waits
        "parked_key": [_st(event_floor=1), _st(diagnosis={"id": 40, "code": "not_played", "title": "Not played",
                                                         "next": "", "track_id": "t", "failing": False}),
                       _st(TOOK, diagnosis={"id": 40, "code": "not_played", "title": "Not played", "next": "",
                                            "track_id": "t", "failing": False}),
                       _st(REFUSED, diagnosis={"id": 40, "code": "not_played", "title": "Not played", "next": "",
                                               "track_id": "t", "failing": False})]})
    assert _keys(out["order"]) == [[], ["player.other_app"], ["a"], ["a"], ["b"], ["player.other_app"]]
    assert out["order"][3]["waiting"] == ["b", "player.other_app"]
    assert [[t["title"] for t in step["shown"]] for step in out["repeat"]] == [["First"]] * 3 + [["Second, again"]]
    assert _keys(out["amended"])[-1] == ["player.other_app"]
    assert out["amended"][-1]["shown"][0]["text"].endswith('<span class="num">3:20</span>.')
    assert _keys(out["passive"]) == [["hqplayer.building"], ["a"]] and out["passive"][1]["waiting"] == [
        "hqplayer.building"]
    # the diagnosis's toast stepped aside for the takeover's; the refusal — a new instance under the takeover's
    # key, which was on screen — redraws it in place
    assert _keys(out["parked_key"]) == [[], ["hqplayer.diagnosis"], ["player.other_app"], ["player.other_app"]]
    assert out["parked_key"][3]["shown"][0]["title"] == "HQPlayer refused the other app's track"


def test_a_new_instance_for_a_toast_that_stepped_aside_is_news():
    """A takeover's toast stepped aside for a failed play's (news, and
    lasting itself); then another takeover happens. The toast that waits is
    redrawn as the new one and is news: it shows, the failed play's steps
    aside for it — it does not wait, unseen, until that one is closed
    (review round 1, L3)."""
    took2 = {"id": 21, "kind": "took"}
    own = {"id": 40, "code": "not_played", "title": "Not played", "next": "", "track_id": "t", "failing": False}
    out = _run({"p": [_st(event_floor=1), _st(TOOK), _st(TOOK, diagnosis=own), _st(took2, diagnosis=own)]})
    assert _keys(out["p"]) == [[], ["player.other_app"], ["hqplayer.diagnosis"], ["player.other_app"]]
    assert out["p"][3]["waiting"] == ["hqplayer.diagnosis"]


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


# What Now Playing's status handlers — the sheet's and the mini-player's — may
# read of the status: the player's state alone. A key that carries a notice
# (what happened to the output, a diagnosis, a build, a spot's words) is a
# toast's to read (the block "The output's words"), never these screens'.
NOW_PLAYING_READS = {
    "excerpt", "length", "progress_percent", "position_formatted", "length_formatted", "state", "source",
    "radio_mode", "song", "artist", "album", "cover_id", "media_file_id", "cover_url", "provider_cover_url",
    "preview", "preview_track_id", "provider", "album_id", "track_id",
}
# ...and everything they write as text or show and hide, statement by statement
NOW_PLAYING_WRITES = {
    "sheet": ["this.excerptBadge.hidden = !data.excerpt", "this.timeCurrent.textContent = data.position_formatted || '0:00'",
              "this.timeTotal.textContent = data.length_formatted || '0:00'", "this.title.textContent = data.song || '—'",
              "this.artist.textContent = data.artist || ''", "this.albumText.textContent = data.album || ''",
              "this.year.textContent = ''"],
    "mini": ["this.el.hidden = true", "this.el.hidden = false", "this.title.textContent = data.song || '—'",
             "this.artist.textContent = album ? `${artist} · ${album}` : artist"],
}
# The helpers they hand the status to: the transport's own (a spinner while a start is in flight)
NOW_PLAYING_CALLS = {"transportBusy", "_paintProgress", "coverPlaceholderColors", "onStatus", "update"}
# Every element of the two, by id: the meta row under the album line holds the
# format badges and the track's own readings — nothing that says what happened
NOW_PLAYING_META_ROW = ["npQBadge", "npExcerptBadge", "npKeyPill", "npBpm", "npEnergy"]
NOW_PLAYING_IDS = {
    "npSheet", "npClose", "npCoverImg", "npCoverFallback", "npLyricsBtn", "npTitleLine", "npArtistLine",
    "npAlbumText", "npYearText", "npQBadge", "npExcerptBadge", "npKeyPill", "npBpm", "npBpmNum", "npEnergy",
    "npEnergyDots", "npProgressTrack", "npProgressFill", "npProgressHead", "npTimeCurrent", "npTimeTotal",
    "npQueueBtn", "npPrev", "npPlayPauseBtn", "npPlayPauseSvg", "npPlayPauseIcon", "npNextBtn", "npRadioBtn",
    "npSimilar", "npSimilarList", "npSimilarCount",
    "mpCover", "mpTitle", "mpArtist", "mpPlayPause", "mpPlayPauseIcon", "mpNext",
}


def _now_playing_handlers(shell: str) -> dict:
    return {"sheet": shell[shell.index("    onStatus(data) {"):shell.index("    // One similar path for owned AND preview")],
            "mini": shell[shell.index("    update(data) {"):shell.index("\n    setCover(detail) {")]}


def now_playing_says_a_notice(html: str, css: str, shell: str) -> list:
    """Why Now Playing — the sheet or the mini-player — could render a
    notice, as the page's three files stand: each reason a sentence, none
    when it shows the player alone."""
    why = []
    row = html[html.index('<div class="np-meta-row">'):html.index('<div class="np-progress-wrap">')]
    depth, children = 0, []
    for closing, _tag, attrs in re.findall(r"<(/?)(\w+)([^>]*)>", row):
        if closing:
            depth -= 1
            continue
        if depth == 1:
            ident = re.search(r'id="(\w+)"', attrs)
            children.append(ident.group(1) if ident else "(no id)")
        depth += 1
    if children != NOW_PLAYING_META_ROW:
        why.append(f"the meta row holds {children}")
    if re.search(r"<(button|a)\b", row):
        why.append("a control in the meta row")
    ids = set(re.findall(r'id="((?:np|mp)[A-Z]\w*)"', html)) | set(re.findall(r"getElementById\('((?:np|mp)[A-Z]\w*)'\)", shell))
    if ids - NOW_PLAYING_IDS:
        why.append(f"elements not of the player: {sorted(ids - NOW_PLAYING_IDS)}")
    for name, handler in _now_playing_handlers(shell).items():
        reads = set(re.findall(r"\bdata\.(\w+)", handler))
        if reads - NOW_PLAYING_READS:
            why.append(f"the {name}'s handler reads {sorted(reads - NOW_PLAYING_READS)} from the status")
        # `source` tells one thing here: whose playback it is, for the transport's icon
        if re.findall(r"data\.source\b(?! !== 'external')", handler):
            why.append(f"the {name}'s handler reads `source` for more than the transport's icon")
        writes = [f"{target}.{prop} = {' '.join(value.split())}" for target, prop, value in
                  re.findall(r"([\w.]+)\.(textContent|innerHTML|innerText|hidden|title)\s*=\s*([^;]+);", handler)]
        if writes != NOW_PLAYING_WRITES[name]:
            why.append(f"the {name}'s handler writes {[w for w in writes if w not in NOW_PLAYING_WRITES[name]]}")
        if re.search(r"insertAdjacent|appendChild|createElement|\.append\(|\.prepend\(|notices\.", handler):
            why.append(f"the {name}'s handler builds elements or raises a notice of its own")
        calls = set(re.findall(r"\b(\w+)\(data\b", handler))
        if calls - NOW_PLAYING_CALLS:
            why.append(f"the {name}'s handler hands the status to {sorted(calls - NOW_PLAYING_CALLS)}")
    for gone in ("npHqpTag", "npWhy", "np-output-line", "np-why", "outputLine"):
        if gone in html or gone in css or gone in shell:
            why.append(f"{gone} is back")
    return why


def test_now_playing_renders_no_notice():
    """The owner's rule (2026-11-11, restated 2026-11-14; BACKLOG B-87): Now
    Playing shows the player alone — the track, its badges, the bar, the
    controls — and so does the mini-player. Pinned on the page's three files
    as they stand: the elements the two have, by id; the meta row's children;
    what their status handlers read of the status, write as text, show and
    hide, and whom they hand the status to. A new notice goes through
    notices.toast — adding it here fails this test, in each of the shapes
    the old box could come back in."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    shell = (STATIC / "app-shell.js").read_text(encoding="utf-8")
    assert now_playing_says_a_notice(html, css, shell) == []

    def swap(text, old, new):
        assert text.count(old) == 1, old
        return text.replace(old, new)
    meta_end = '        <div class="np-progress-wrap">'
    read_excerpt = "      if (this.excerptBadge) this.excerptBadge.hidden = !data.excerpt;\n"
    mini_title = "      this.title.textContent = data.song || '—';\n      const artist = data.artist || '';"
    mutants = {
        # the old mechanism's shape: an element under the badges, filled by a helper from the status
        "a line under the meta row": (swap(html, meta_end, '        <div id="npStateNote" hidden></div>\n' + meta_end),
                                      swap(shell, read_excerpt, read_excerpt + "      playerNote(data);\n")),
        "a button in the meta row": (swap(html, '<div class="np-key-pill" id="npKeyPill" hidden></div>',
                                          '<button type="button">Why?</button>'
                                          '<div class="np-key-pill" id="npKeyPill" hidden></div>'), shell),
        "the mini-player says a build": (html, swap(shell, mini_title, "      if (data.building) this.artist.textContent"
                                                                      " = 'Building the filter…';\n" + mini_title)),
        "a sentence where the year stands": (html, swap(shell, "        if (this.year) this.year.textContent = '';", (
            "        if (this.year) this.year.textContent = data.source === 'external' ? 'Another app has it' : '';"))),
        "a toast raised from the handler": (html, swap(shell, read_excerpt, read_excerpt + (
            "      if (data.state === 'stopped') notices.toast({ title: 'Stopped' });\n"))),
        "a diagnosis read again": (html, swap(shell, read_excerpt, read_excerpt + "      const diag = data.diagnosis;\n")),
    }
    for name, (mutant_html, mutant_shell) in mutants.items():
        assert now_playing_says_a_notice(mutant_html, css, mutant_shell), f"not caught: {name}"


def test_a_toast_has_two_buttons_at_most_in_one_order():
    """§ Notices: Why? (the explanation), then one action, then the close —
    the notices object draws no third, and every caller that offers an
    explanation gives it as `why`, never as an action labelled so."""
    shell = (STATIC / "app-shell.js").read_text(encoding="utf-8")
    fill = shell[shell.index("    _fill(t, {"):shell.index("    _show(t) {")]
    order = [m for m in re.findall(r'data-toast="(\w+)"|class="toast-(close)"', fill)]
    assert [a or b for a, b in order][:3] == ["why", "action", "close"]
    assert fill.count('class="toast-action"') == 2
    assert not re.search(r"action:\s*\{\s*label:\s*'Why\?'", shell)
