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
    # A queue that never settles (two toasts trading places) hangs under Node: a failure, by the clock
    proc = subprocess.run([NODE, "-e", _SHELL, "--", str(STATIC / "app-shell.js")],
                          input=json.dumps({"pages": pages, "cap": cap}).encode(), capture_output=True, timeout=60)
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
    by design), and its words still follow the spot. A toast the person
    closed is not raised for it — nor is the takeover they closed brought
    back by the refusal's toast: that one goes with its refusal (round 2).
    A refusal the page has no word for (the stream reconnected while it
    stood) leaves the takeover's toast up: that app holds the output still.
    And a toast that waits its turn is redrawn where it waits, not moved
    ahead of what the person has not seen."""
    start = _st(event_floor=10)
    out = _run({"stands": [start, _st(TOOK), _st(REFUSED), _st(TOOK), _st(TOOK, resume={"index": 3, "position": 200}),
                           PLAYING],
                "closed": [start, _st(TOOK), _st(REFUSED), {"press": ["player.other_app", CLOSE]}, {"pass": 500},
                           _st(TOOK), _st(TOOK)],
                "closed_before": [start, _st(TOOK), {"press": ["player.other_app", CLOSE]}, {"pass": 500},
                                  _st(REFUSED), _st(TOOK), {"pass": 500}, _st(TOOK)],
                "reconnected": [start, _st(TOOK), _st(REFUSED, event_floor=50), _st(REFUSED), _st(TOOK), PLAYING],
                "waiting": [start, _st(TOOK), _st(REFUSED),
                            {"toast": {"kind": "info", "key": "a", "title": "First"}}, _st(TOOK), {"pass": 5_000}],
                # the takeover's toast waited behind news when the refusal came: waiting is not closed
                "waited_then_refused": [start, _st(TOOK), {"toast": {"kind": "info", "key": "a", "title": "First"}},
                                        _st(REFUSED), _st(TOOK), {"pass": 5_000}]})
    assert [[t["title"] for t in step["shown"]] for step in out["stands"]] == [
        [], ["Another app took HQPlayer Desktop · study-pc"], ["HQPlayer refused the other app's track"],
        ["Another app took HQPlayer Desktop · study-pc"], ["Another app took HQPlayer Desktop · study-pc"], []]
    assert out["stands"][3]["shown"][0]["buttons"] == ["Why?", "×"]
    assert out["stands"][4]["shown"][0]["text"].endswith('<span class="num">3:20</span>.')
    assert _keys(out["closed"]) == [[], ["player.other_app"], ["player.other_app"], [], [], [], []]
    # the refusal is news of its own after a closed takeover, and goes when it is over
    assert [[t["title"] for t in step["shown"]] for step in out["closed_before"]][4:] == [
        ["HQPlayer refused the other app's track"], [], [], []]
    assert [[t["title"] for t in step["shown"]] for step in out["reconnected"]] == [
        []] + [["Another app took HQPlayer Desktop · study-pc"]] * 4 + [[]]
    # behind news: redrawn as the takeover where it waits, and back in its turn
    assert _keys(out["waiting"])[3:] == [["a"], ["a"], ["player.other_app"]]
    assert out["waiting"][4]["waiting"] == ["player.other_app"]
    assert out["waiting"][5]["shown"][0]["title"] == "Another app took HQPlayer Desktop · study-pc"
    assert [[t["title"] for t in step["shown"]] for step in out["waited_then_refused"]][3:] == [
        ["First"], ["First"], ["Another app took HQPlayer Desktop · study-pc"]]


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
        "repeat": [toast("a", "First"), toast("b", "Second"), toast("c", "Third"),
                   toast("b", "Second, again", tag="other"), {"pass": 5_000}],
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
    # ...in its place, whatever it is raised for now: not behind what came after it
    assert [[t["title"] for t in step["shown"]] for step in out["repeat"]] == [["First"]] * 4 + [["Second, again"]]
    assert [step["waiting"] for step in out["repeat"]] == [[], ["b"], ["b", "c"], ["b", "c"], ["c"]]
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


# ---- Now Playing shows the player alone ---------------------------------------------------------
# The pin is on the whole of it — the sheet and the mini-player — not on one place a notice came
# back through: every element the two have in the page's markup; who in the script may touch them
# (their two objects, `sheet` and `mp`, and nobody else); and, in those objects, what the status
# handlers read of the status, write, set and call. Each list is the code as it stands: a change to
# the player changes its list here, on purpose, and a notice has no list to join.

# The keys of the status a notice rides on: a toast's to read ("The output's words"), never these
# objects' — in any spelling (a dot, a bracket, a destructuring)
NOTICE_KEYS = ("diagnosis", "output_event", "event_floor", "building", "not_answering", "rebuilding", "resume",
               "paused_on_device", "dsp_change", "seek_missed", "other_refused", "hold")
NOW_PLAYING_ELEMENTS = [
    "div#miniPlayer.mini-player", "div#mpCover.mp-cover", "div.mp-info", "div#mpTitle.mp-title",
    "div#mpArtist.mp-artist", "button#mpPlayPause.mp-action", "svg", "button#mpNext.mp-action", "svg",
    "div#npSheet.np-sheet", "div.np-screen", "div.np-cover-block", "div.np-cover-overlay", "div.np-control-bar",
    "button#npClose.np-icon-btn", "svg", "div.np-cover-wrap", "img#npCoverImg",
    "div#npCoverFallback.np-cover-fallback", "div.np-cover-scrim", "button#npLyricsBtn.np-lyrics-btn", "svg",
    "div.np-below", "h1#npTitleLine.np-title", "div#npArtistLine.np-artist", "div.np-album", "span#npAlbumText",
    "span#npYearText.np-year", "div.np-meta-row", "span#npQBadge.np-q-badge",
    "span#npExcerptBadge.np-q-badge.is-neutral", "div#npKeyPill.np-key-pill", "div#npBpm.np-bpm",
    "span#npBpmNum.np-bpm-num", "span.np-bpm-label", "div#npEnergy.np-energy", "span#npEnergyDots.np-energy-dots",
    "div.np-progress-wrap", "div#npProgressTrack.np-progress-track", "div#npProgressFill.np-progress-fill",
    "div#npProgressHead.np-progress-head", "div.np-progress-meta", "span#npTimeCurrent",
    "span#npTimeTotal.np-time-total", "div.np-transport", "button#npQueueBtn.np-tbtn-secondary", "svg",
    "button#npPrev.np-tbtn", "svg", "button#npPlayPauseBtn.np-play", "svg#npPlayPauseSvg",
    "button#npNextBtn.np-tbtn", "svg", "button#npRadioBtn.np-tbtn-secondary.np-radio", "svg",
    "div#npSimilar.np-similar", "div.np-similar-head", "h3.np-similar-title", "span.np-similar-sub", "svg",
    "span#npSimilarCount", "div#npSimilarList.np-similar-list", "div.np-spacer",
]
# What the script outside the two objects says of them: these members (the other `sheet`s are local
# sheets of other screens), and one selector of the Similar rows' add button
NOW_PLAYING_OUTSIDE = {
    "sheet": {"addEventListener", "className", "hide", "html", "init", "innerHTML", "isOpen", "onStatus",
              "querySelector", "querySelectorAll", "refreshPreviewDetail", "remove"},
    "mp": {"init", "setCover", "update"},
    "selectors": {".track-add, .np-sim-add"},
}
# The two status handlers, statement by statement
NOW_PLAYING_HANDLERS = {
    "sheet": {
        "reads": {"excerpt", "length", "progress_percent", "position_formatted", "length_formatted", "state",
                  "source", "radio_mode", "song", "artist", "album", "cover_id", "media_file_id", "cover_url",
                  "provider_cover_url", "preview", "preview_track_id", "provider", "album_id", "track_id"},
        "writes": ["this.excerptBadge.hidden = !data.excerpt",
                   "this.timeCurrent.textContent = data.position_formatted || '0:00'",
                   "this.timeTotal.textContent = data.length_formatted || '0:00'",
                   "this.title.textContent = data.song || '—'", "this.artist.textContent = data.artist || ''",
                   "this.albumText.textContent = data.album || ''", "this.year.textContent = ''"],
        "calls": {"Date.now", "btn.classList.toggle", "coverPlaceholderColors", "onStatus", "paintCoverImg",
                  "this._paintProgress", "this.coverFallback.style.setProperty", "this.playPauseIcon.closest",
                  "this.playPauseIcon.setAttribute", "this.radioBtn.setAttribute", "this.tryFetchDetail",
                  "transportBusy"},
        "attributes": {"d", "data-state"}, "classes": {"is-loading"},
        "assigned": {"_identity", "_npAlbumId", "_npMfId", "_npPreviewTid", "_npProvider", "_trackLength", "hidden",
                     "lastTrackKey", "textContent"},
    },
    "mp": {
        "reads": {"album", "artist", "cover_id", "cover_url", "media_file_id", "provider_cover_url", "song", "source",
                  "state"},
        "writes": ["this.el.hidden = true", "this.el.hidden = false", "this.title.textContent = data.song || '—'",
                   "this.artist.textContent = album ? `${artist} · ${album}` : artist"],
        "calls": {"btn.classList.toggle", "coverPlaceholderColors", "document.body.classList.add",
                  "document.body.classList.remove", "gradient", "this.playPauseIcon.closest",
                  "this.playPauseIcon.setAttribute", "this.setCover", "transportBusy", "update"},
        "attributes": {"d"}, "classes": {"has-miniplayer", "is-loading"},
        "assigned": {"backgroundImage", "hidden", "lastSongKey", "textContent"},
    },
}
_VOID = {"img", "input", "br", "hr", "line", "rect", "circle", "path", "meta", "link"}


def _uncommented(js: str) -> str:
    return re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", js, flags=re.S))


def _now_playing_elements(html: str) -> list:
    """Every element of the mini-player and the Now Playing sheet in the
    page's markup, in order, as tag#id.classes (an icon's own shapes aside)."""
    from html.parser import HTMLParser

    class Walk(HTMLParser):
        def __init__(self):
            super().__init__()
            self.depth, self.root, self.svg, self.out = 0, None, 0, []

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if self.root is None and a.get("id") in ("npSheet", "miniPlayer"):
                self.root = self.depth
            if self.root is not None and not self.svg:
                self.out.append(tag + (f"#{a['id']}" if a.get("id") else "")
                                + "".join(f".{c}" for c in (a.get("class") or "").split()))
            if self.root is not None and tag == "svg":
                self.svg += 1
            if tag not in _VOID:
                self.depth += 1

        def handle_endtag(self, tag):
            if tag in _VOID:
                return
            self.depth -= 1
            if tag == "svg" and self.svg:
                self.svg -= 1
            if self.root is not None and self.depth == self.root:
                self.root = None

    walk = Walk()
    walk.feed(html)
    return walk.out


def _now_playing_objects(shell: str) -> dict:
    """The source of the two objects that own Now Playing — `sheet` and the
    mini-player's `mp` — and of the script around them."""
    def span(name):
        start = shell.index(f"\n  const {name} = {{")
        return start, shell.index("\n  };", start) + 5
    (s0, s1), (m0, m1) = span("sheet"), span("mp")
    assert s1 <= m0
    return {"sheet": shell[s0:s1], "mp": shell[m0:m1], "outside": shell[:s0] + shell[s1:m0] + shell[m1:]}


def now_playing_says_a_notice(html: str, css: str, shell: str) -> list:
    """Why Now Playing — the sheet or the mini-player — could render a
    notice, as the page's three files stand: each reason a sentence, none
    when the two show the player alone."""
    why = []
    elements = _now_playing_elements(html)
    if elements != NOW_PLAYING_ELEMENTS:
        added = [e for e in elements if e not in NOW_PLAYING_ELEMENTS]
        why.append(f"the markup of the two changed: {added or 'an element moved or went'}")
    row = html[html.index('<div class="np-meta-row">'):html.index('<div class="np-progress-wrap">')]
    if re.search(r"<(button|a)\b", row):
        why.append("a control in the meta row")

    parts = _now_playing_objects(shell)
    outside = _uncommented(parts["outside"])
    if re.search(r"getElementById\(\s*['\"`](?:np|mp)[A-Z]|getElementById\(\s*['\"`]miniPlayer", outside):
        why.append("an element of the two is taken by its id outside their objects")
    selectors = set(re.findall(r"['\"`]([^'\"`\n]*(?:\.np-|#np[A-Z]|\.mp-|#mp[A-Z]|mini-player|#miniPlayer)[^'\"`\n]*)['\"`]",
                               outside))
    if selectors - NOW_PLAYING_OUTSIDE["selectors"]:
        why.append(f"the script outside their objects selects into them: {sorted(selectors - NOW_PLAYING_OUTSIDE['selectors'])}")
    for name in ("sheet", "mp"):
        members = set(re.findall(r"(?<![\w.$])" + name + r"\.(\w+)", outside))
        if members - NOW_PLAYING_OUTSIDE[name]:
            why.append(f"the script outside reaches into `{name}`: {sorted(members - NOW_PLAYING_OUTSIDE[name])}")
        named = [key for key in NOTICE_KEYS if re.search(r"\b" + key + r"\b", _uncommented(parts[name]))]
        if named:
            why.append(f"`{name}` names {named}, which a notice rides on")

    handlers = {"sheet": shell[shell.index("    onStatus(data) {"):shell.index("    // One similar path for owned AND preview")],
                "mp": shell[shell.index("    update(data) {"):shell.index("\n    setCover(detail) {")]}
    for name, source in handlers.items():
        handler, pinned = _uncommented(source), NOW_PLAYING_HANDLERS[name]
        reads = set(re.findall(r"\bdata\.(\w+)", handler))
        if reads - pinned["reads"]:
            why.append(f"`{name}`'s handler reads {sorted(reads - pinned['reads'])} from the status")
        if re.search(r"\bdata\s*\[|\bdata\?\.|\}\s*=\s*data\b|=\s*data\s*[;,)]|\.\.\.data\b|currentStatus", handler):
            why.append(f"`{name}`'s handler reads the status by another road than `data.<key>`")
        # `source` tells one thing here: whose playback it is, for the transport's icon
        if re.findall(r"data\.source\b(?! !== 'external')", handler):
            why.append(f"`{name}`'s handler reads `source` for more than the transport's icon")
        writes = [f"{target}.{prop} {op} {' '.join(value.split())}" for target, prop, op, value in re.findall(
            r"([\w.]+)\.(textContent|innerHTML|innerText|outerHTML|hidden|title|value|className)\s*([-+*]?=)(?!=)\s*([^;]+);",
            handler)]
        if writes != pinned["writes"]:
            why.append(f"`{name}`'s handler writes {[w for w in writes if w not in pinned['writes']] or 'other than pinned'}")
        calls = set(re.findall(r"([\w.]+)\(", handler)) - {"if", "function"}
        if calls - pinned["calls"]:
            why.append(f"`{name}`'s handler calls {sorted(calls - pinned['calls'])}")
        attributes = set(re.findall(r"setAttribute\(\s*['\"`]([^'\"`]+)", handler))
        classes = set(re.findall(r"classList\.\w+\(\s*['\"`]([^'\"`]+)", handler))
        assigned = set(re.findall(r"\.(\w+)\s*[-+*]?=(?!=)", handler))
        for what, found in (("attributes", attributes), ("classes", classes), ("assigned", assigned)):
            if found - pinned[what]:
                why.append(f"`{name}`'s handler sets {what} {sorted(found - pinned[what])}")
        if re.search(r"setAttribute\(\s*[^'\"`\s]|classList\.\w+\(\s*[^'\"`\s]|notices\b", handler):
            why.append(f"`{name}`'s handler sets a name it computes, or raises a notice of its own")

    # ...and no words a stylesheet could put there: generated content of the two is an empty box at most
    plain = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for selector, body in re.findall(r"([^{}]*(?:\.np-|#np[A-Z]|\.mini-player|\.mp-|#mp[A-Z])[^{}]*)\{([^{}]*)\}", plain):
        for value in re.findall(r"(?<![-\w])content\s*:\s*([^;]+)", body):
            if value.strip() not in ("''", '""', "none"):
                why.append(f"the stylesheet writes {value.strip()} into {selector.strip()}")
    for gone in ("npHqpTag", "npWhy", "np-output-line", "np-why", "outputLine"):
        if gone in html or gone in css or gone in shell:
            why.append(f"{gone} is back")
    return why


def test_now_playing_renders_no_notice():
    """The owner's rule (2026-11-11, restated 2026-11-14; BACKLOG B-87): Now
    Playing shows the player alone — the track, its badges, the bar, the
    controls — and so does the mini-player. Pinned on the page's three files
    as they stand, on the whole of the two: their elements in the markup;
    that nobody but their two objects touches them; and what those objects'
    status handlers read, write, set and call. A new notice goes through
    notices.toast: put on Now Playing, in any of the shapes below — the old
    box's own and the ones two review rounds brought — it fails this test."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    shell = (STATIC / "app-shell.js").read_text(encoding="utf-8")
    assert now_playing_says_a_notice(html, css, shell) == []

    def swap(text, old, new):
        assert text.count(old) == 1, old
        return text.replace(old, new)

    def after(text, old, new):
        return swap(text, old, old + new)
    meta_end = '        <div class="np-progress-wrap">'
    sheet_at = "      if (this.excerptBadge) this.excerptBadge.hidden = !data.excerpt;\n"
    mini_at = "      this.title.textContent = data.song || '—';\n      const artist = data.artist || '';\n"
    tail = "\n  window.notices = notices;\n"                 # the script outside the two objects
    listener = "  document.addEventListener('np-update', (e) => { %s });\n"
    mutants = {
        # the old mechanism's shape: an element under the badges, filled by a helper from the status
        "a line under the meta row": (swap(html, meta_end, '        <div id="npStateNote" hidden></div>\n' + meta_end),
                                      css, after(shell, sheet_at, "      playerNote(data);\n")),
        "a button in the meta row": (swap(html, '<div class="np-key-pill" id="npKeyPill" hidden></div>',
                                          '<button type="button">Why?</button>'
                                          '<div class="np-key-pill" id="npKeyPill" hidden></div>'), css, shell),
        "the mini-player says a build": (html, css, swap(shell, mini_at, (
            "      if (data.building) this.artist.textContent = 'Building the filter…';\n") + mini_at)),
        "a sentence where the year stands": (html, css, swap(
            shell, "        if (this.year) this.year.textContent = '';",
            "        if (this.year) this.year.textContent = data.source === 'external' ? 'Another app has it' : '';")),
        "a toast raised from the handler": (html, css, after(shell, sheet_at, (
            "      if (data.state === 'stopped') notices.toast({ title: 'Stopped' });\n"))),
        "a diagnosis read again": (html, css, after(shell, sheet_at, "      const diag = data.diagnosis;\n")),
        # round 2's: every way around the two handlers
        "a second listener writes where the year stands": (html, css, after(shell, tail, listener % (
            "document.getElementById('npYearText').textContent = e.detail.source ? 'Another app has it' : '';"))),
        "an element by its class, filled by a listener": (
            swap(html, meta_end, '        <div class="np-note"></div>\n' + meta_end), css,
            after(shell, tail, listener % "document.querySelector('.np-note').textContent = 'Paused elsewhere';")),
        "an element of another id in the sheet": (
            swap(html, meta_end, '        <div id="stateNote"></div>\n' + meta_end), css,
            after(shell, tail, listener % "document.getElementById('stateNote').textContent = 'Paused elsewhere';")),
        "the status taken apart": (html, css, after(shell, sheet_at, (
            "      const { building } = data;\n      this.year.replaceChildren(building ? 'Building…' : '');\n"))),
        "text added to, in the mini-player": (html, css, swap(shell, mini_at, mini_at + (
            "      this.artist.textContent += data.state === 'stopped' ? ' · stopped by another app' : '';\n"))),
        "a helper that reads the status itself": (html, css, after(shell, sheet_at, "      playerNote();\n")),
        "an attribute a stylesheet shows": (html, css, after(shell, sheet_at, (
            "      this.year.setAttribute('data-note', data['diagnosis'] ? 'Did not play' : '');\n"))),
        "a class and the stylesheet's words": (
            html, css + "\n.np-play.is-held::after { content: 'Another app has it'; }\n",
            after(shell, sheet_at, "      this.year.classList.toggle('is-held', data.state === 'stopped');\n")),
        "the status under another name": (html, css, after(shell, sheet_at, (
            "      const st = data;\n      this.tryFetchDetail(st);\n"))),
        "an element a listener makes in the sheet": (html, css, after(shell, tail, listener % (
            "sheet.el.querySelector('div').append('Another app has it');"))),
        "a title on the track's name": (html, css, after(shell, sheet_at, (
            "      this.title.setAttribute('title', data.state === 'stopped' ? 'Stopped by another app' : '');\n"))),
        "a key read with a question mark": (html, css, after(shell, sheet_at, (
            "      this.year.hidden = !!data?.building;\n"))),
        "a notice key kept for later in the object": (html, css, swap(
            shell, "    onStatus(data) {\n      if (!data) return;\n",
            "    onStatus(data) {\n      if (!data) return;\n      this._last = data;\n"
            "      this._note = () => this._last['rebuilding'];\n")),
    }
    for name, (mutant_html, mutant_css, mutant_shell) in mutants.items():
        assert now_playing_says_a_notice(mutant_html, mutant_css, mutant_shell), f"not caught: {name}"


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
