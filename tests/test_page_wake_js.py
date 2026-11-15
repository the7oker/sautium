"""A phone tab away and back, under Node: backend/static/auth.js and player.js
run as written, against a network that does what a phone's does when it puts
the browser away — the streams' sockets die without a word (or are cut), what
the page sends meanwhile is held and sent when it comes back, past the replay
window.

The page that comes back re-opens both streams with fresh signatures, every
read the node refused as stale is answered once more and no more, and what it
shows is the node's state — never what was true when it left (B-81, the owner's
phone on 2026-11-11). Skipped where Node is not.
"""

import json
import shutil
import subprocess

import pytest

from tests.conftest import REPO_ROOT

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

STATIC = REPO_ROOT / "backend" / "static"

_DRIVER = r"""
'use strict';
const fs = require('node:fs'), vm = require('node:vm');
const [shaPath, authPath, playerPath, scenario] = process.argv.slice(-4);
const ORIGIN = 'http://node.test:8800';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const t0 = Date.now();
const log = [];                       // what reached the node, and its answer

// -- the node --------------------------------------------------------------
const node = { song: 'A', pv: 1, hangPlaylist: false };
const status = () => ({ state: 'playing', song: node.song, playlist_version: node.pv });
const live = new Set();               // stream connections the node writes to
const enc = new TextEncoder();
function stream(kind, signal) {
  let conn;
  const body = new ReadableStream({ start(c) { conn = { kind, c, dead: false }; } });
  live.add(conn);
  const say = (obj) => conn.c.enqueue(enc.encode('data: ' + JSON.stringify(obj) + '\n\n'));
  if (kind === 'events') { say({ t: 'hello' }); say({ t: 'status', d: status() }); }
  else say({});
  conn.say = say;
  signal && signal.addEventListener('abort', () => {
    live.delete(conn);
    try { conn.c.error(new DOMException('aborted', 'AbortError')); } catch (_) {}
  });
  return body;
}
function push() {
  for (const conn of live) if (conn.kind === 'events' && !conn.dead) conn.say({ t: 'status', d: status() });
}
const answer = (status, headers = {}, body = '{}') => new Response(body, { status, headers });
function serve(method, path, init) {
  const signal = init.signal;
  if (path.startsWith('/api/events')) {
    return new Response(stream('events', signal), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  }
  if (path.startsWith('/api/player/browser/channel')) {
    return new Response(stream('channel', signal), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  }
  if (path === '/api/settings/library/stream') {
    return new Response(stream('library', signal), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  }
  if (path === '/api/player/playlist') {
    if (node.hangPlaylist) {
      return new Promise((_, reject) => signal && signal.addEventListener('abort',
        () => reject(new DOMException('aborted', 'AbortError'))));
    }
    return answer(200, {}, JSON.stringify({ tracks: Array.from({ length: node.pv }, (_, i) => ({ id: i })) }));
  }
  return answer(200);
}

// -- the phone's network ---------------------------------------------------
let away = false;
let held = [];
let staleAgain = false;             // the page's next sends of the events stream are held past the window too
let staleFor = 0;
function network(input, init) {
  const url = new URL(String(input), ORIGIN);
  const path = url.pathname + url.search;
  const method = (init.method || 'GET').toUpperCase();
  // A fetch whose signal is already aborted is never sent
  if (init.signal && init.signal.aborted) return Promise.reject(new DOMException('aborted', 'AbortError'));
  const entry = { t: Date.now() - t0, method, path: url.pathname };
  log.push(entry);
  if (staleFor > 0 && url.pathname === '/api/events') {
    staleFor -= 1;
    entry.status = 401; entry.auth = 'stale-ts'; entry.sent = entry.t;
    return Promise.resolve(answer(401, { 'X-Sautium-Auth-Error': 'stale-ts' }));
  }
  if (away) {
    return new Promise((resolve, reject) => {
      held.push({ entry, resolve });
      init.signal && init.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
    });
  }
  return Promise.resolve(serve(method, path, init)).then((r) => { entry.status = r.status; return r; });
}
function goAway(kind) {
  away = true;
  for (const conn of live) {
    if (kind === 'cut') { live.delete(conn); conn.c.error(new TypeError('network connection was lost')); }
    else conn.dead = true;          // open, silent: no FIN ever comes
  }
}
function flushStale() {
  // What the browser held was signed before: the node refuses it as stale.
  const out = held; held = [];
  for (const { entry, resolve } of out) {
    entry.status = 401; entry.auth = 'stale-ts'; entry.sent = Date.now() - t0;
    resolve(answer(401, { 'X-Sautium-Auth-Error': 'stale-ts' }));
  }
}

// -- the page --------------------------------------------------------------
const store = new Map([['sautium.device_token', 'page token']]);
const winEvents = new EventTarget(), docEvents = new EventTarget();
let hidden = false;
const document = {
  readyState: 'complete', getElementById: () => null,
  get hidden() { return hidden; }, get visibilityState() { return hidden ? 'hidden' : 'visible'; },
  addEventListener: (...a) => docEvents.addEventListener(...a),
  removeEventListener: (...a) => docEvents.removeEventListener(...a),
  dispatchEvent: (e) => docEvents.dispatchEvent(e),
};
const g = {
  fetch: network, document,
  addEventListener: (...a) => winEvents.addEventListener(...a),
  removeEventListener: (...a) => winEvents.removeEventListener(...a),
  dispatchEvent: (e) => winEvents.dispatchEvent(e),
  localStorage: { getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: k => store.delete(k) },
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  location: { origin: ORIGIN, hash: '', pathname: '/', search: '', reload() {} },
  history: { replaceState() {} },
  crypto: globalThis.crypto, Headers, URL, Request: class extends Request {
    constructor(input, init) { super(new URL(String(input), ORIGIN), init); } },
  Response, TextEncoder, TextDecoder, CustomEvent, Event, DOMException, AbortController,
  setTimeout, clearTimeout, Promise,
  console: { log() {}, debug() {}, warn() {}, error() {} },
};
g.window = g;
g.Sautium = {};
vm.createContext(g);
const links = [];
winEvents.addEventListener('sautium:link', (e) => links.push(e.detail.up));
let wakes = 0;
winEvents.addEventListener('sautium:wake', () => { wakes += 1; });
for (const p of [shaPath, authPath, playerPath]) {
  // The link's grace is the page's to say; shortened here so a test waits it out
  vm.runInContext(fs.readFileSync(p, 'utf8').replace('DISCONNECT_GRACE_MS = 10000', 'DISCONNECT_GRACE_MS = 300'), g);
}
// The library wake channel's stream (app-shell.js), as the shell opens it.
vm.runInContext("window.sseStream('/api/settings/library/stream', () => {}, () => {})", g);
// The browser renderer's command channel (player.js), a claim that does not re-open on a wake.
vm.runInContext("window.sseStream('/api/player/browser/channel?tab=t', () => {}, () => {}, { reopenOnWake: false })", g);

const leave = (kind) => { hidden = true; docEvents.dispatchEvent(new Event('visibilitychange')); goAway(kind); };
const comeBack = () => {
  away = false;
  flushStale();
  if (staleAgain) staleFor = 2;
  hidden = false;
  docEvents.dispatchEvent(new Event('visibilitychange'));
};

(async () => {
  await sleep(150);
  if (scenario === 'lifecycle') {
    // Chrome: hidden, frozen, thawed while hidden, shown; the back/forward cache: hidden, shown again
    const fire = (target, type) => target.dispatchEvent(new Event(type));
    const counts = [];
    hidden = true; fire(docEvents, 'visibilitychange'); fire(docEvents, 'freeze'); fire(docEvents, 'resume');
    counts.push(wakes);
    hidden = false; fire(docEvents, 'visibilitychange');
    counts.push(wakes);
    fire(winEvents, 'pagehide'); hidden = true; fire(docEvents, 'visibilitychange');
    hidden = false; fire(docEvents, 'visibilitychange'); fire(winEvents, 'pageshow');
    counts.push(wakes);
    fire(docEvents, 'visibilitychange'); fire(winEvents, 'pageshow');    // shown, never away: no trip
    counts.push(wakes);
    process.stdout.write(JSON.stringify({ counts }));
    process.exit(0);
  }
  const before = g.currentStatus && g.currentStatus.song;
  let backAt;
  if (scenario === 'hung') {
    // A playlist read the network never answers, and the status after it
    node.hangPlaylist = true; node.pv = 2; push();
    await sleep(100);
    node.hangPlaylist = false;
  }
  const silent = scenario !== 'cut';
  leave(silent ? 'silent' : 'cut');
  await sleep(silent ? 100 : 1500);   // a cut stream retries after 1 s
  node.song = 'B'; node.pv += 1; push();
  backAt = Date.now() - t0;
  if (scenario === 'stuck') { hidden = false; docEvents.dispatchEvent(new Event('visibilitychange')); }
  else comeBack();
  await sleep(staleAgain ? 1800 : 400);
  const s = g.currentStatus || {};
  process.stdout.write(JSON.stringify({
    before, song: s.song, pv: s.playlist_version, queue: (g.currentPlaylist || []).length, node,
    backAt, links, log, live: [...live].filter(c => !c.dead).map(c => c.kind),
  }));
  process.exit(0);
})();
"""


def _run(scenario: str, *, stale_again: bool = False) -> dict:
    driver = _DRIVER.replace("let staleAgain = false;", f"let staleAgain = {str(stale_again).lower()};")
    proc = subprocess.run([NODE, "-e", driver, "--", str(STATIC / "sha256.js"), str(STATIC / "auth.js"),
                           str(STATIC / "player.js"), scenario],
                          capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr.decode()
    return json.loads(proc.stdout)


def _after(out: dict, path: str) -> list:
    return [r for r in out["log"] if r["path"] == path and r["t"] >= out["backAt"]]


def test_a_page_back_from_a_trip_whose_streams_died_in_silence_shows_the_node_now():
    """The sockets stay open and say nothing (no FIN: the phone slept, a NAT
    forgot). The page that comes back re-opens both streams at once with
    fresh signatures and paints the node's state — before, it showed what was
    true when it left until the 45 s idle check, with nothing said."""
    out = _run("silent")
    assert out["before"] == "A"
    assert (out["song"], out["pv"], out["queue"]) == ("B", 2, 2)
    assert sorted(out["live"]) == ["events", "library"]
    for path in ("/api/events", "/api/settings/library/stream"):
        assert [r["status"] for r in _after(out, path)] == [200]
    # The renderer's command channel is a claim on the output: a wake does not re-open it (its dead
    # socket is found by the idle check, as before)
    assert _after(out, "/api/player/browser/channel") == []


def test_reads_held_past_the_window_are_answered_once_more_and_the_page_is_current():
    """The owner's log: the streams cut as the phone leaves, their reconnects
    signed then and sent when it comes back, past the window — refused as
    stale, each answered by one fresh signature, and the page current."""
    out = _run("cut")
    assert (out["song"], out["pv"]) == ("B", 2)
    for path in ("/api/events", "/api/settings/library/stream"):
        statuses = [(r.get("status"), r.get("auth")) for r in out["log"] if r["path"] == path and r["t"] > 100]
        assert (401, "stale-ts") in statuses
        refused = statuses.index((401, "stale-ts"))
        assert (200, None) in statuses[refused + 1:], statuses
    assert sorted(out["live"]) == ["channel", "events", "library"]


def test_a_second_staleness_is_not_signed_again_at_once():
    """The fresh signature is held past the window too (the phone put the
    browser away again at once): the refusal goes back to the stream, which
    reconnects on its own schedule — no third send in the same breath, no
    loop."""
    out = _run("cut", stale_again=True)
    events = [r for r in out["log"] if r["path"] == "/api/events" and r["t"] > 100]
    after = [r for r in events if r["t"] >= out["backAt"]]
    assert [r.get("auth") for r in after[:2]] == ["stale-ts", "stale-ts"], events
    second = after[1]
    following = after[2:]
    assert following, events
    assert following[0]["t"] - second["sent"] >= 900, events     # the stream's backoff, not a retry loop
    assert (out["song"], out["pv"]) == ("B", 2)


def test_a_playlist_read_that_is_never_answered_does_not_hold_the_status_for_good():
    """A playlist read in flight on a socket that died answers never; the
    status chain waited on it, and every later status behind it — the page
    stayed on the old state until a fresh tab. The re-opened connection's
    `hello` lets it go."""
    out = _run("hung")
    assert (out["song"], out["pv"], out["queue"]) == ("B", 3, 3)


def test_a_page_back_whose_streams_cannot_get_through_says_so():
    """Back, and the network still holds everything the page sends (the
    phone's Wi-Fi is not up yet): the re-opened stream says nothing, so the
    page does — the link strip ("reconnecting") once the grace, counted from
    the return, has run out — never the old state in silence."""
    out = _run("stuck")
    assert out["song"] == "A"
    assert out["links"] == [False]


def test_one_trip_away_is_one_wake_whatever_the_browser_says_of_it():
    """Hidden, frozen and thawed while still hidden, then shown: one wake, at
    the showing. A bfcache trip (pagehide, then pageshow) is one more. A page
    shown that was never away raises none."""
    proc = subprocess.run([NODE, "-e", _DRIVER, "--", str(STATIC / "sha256.js"), str(STATIC / "auth.js"),
                           str(STATIC / "player.js"), "lifecycle"], capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr.decode()
    assert json.loads(proc.stdout)["counts"] == [0, 1, 2, 2]
