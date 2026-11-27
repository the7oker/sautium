"""The page's side of the Library stream, under Node: backend/static/auth.js
as written, and app-shell.js's own `libraryWake` and `guide`, against a node
that says what the backend's stream says (B-138).

The page asks GET /api/settings/guidance once, for its first paint, and never
at a wake: the trail comes as the stream's `guidance` event. A named event is
no wake — a `load` reaches only who asked for it by name, and nobody fetches
anything. Answers are applied in the order the node computed them, whichever
arrives first. Skipped where Node is not.
"""

import json
import shutil
import subprocess

import pytest

from tests.conftest import REPO_ROOT

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

STATIC = REPO_ROOT / "backend" / "static"
SHELL = (STATIC / "app-shell.js").read_text(encoding="utf-8")


def _object(name: str) -> str:
    start = SHELL.index(f"\n  const {name} = {{\n") + 1
    return SHELL[start:SHELL.index("\n  };\n", start) + len("\n  };")]


_DRIVER = r"""
'use strict';
const fs = require('node:fs'), vm = require('node:vm');
const [shaPath, authPath, scenario] = process.argv.slice(-3);
const ORIGIN = 'http://node.test:8800';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const log = [];                       // what reached the node

// -- the node --------------------------------------------------------------
const node = { tasks: ['audio_output'], seq: 0, run: 'r1', holdRead: scenario === 'late-read', release: null };
const answer = () => ({ tasks: [...node.tasks], seq: ++node.seq, run: node.run });
const live = new Set();
const enc = new TextEncoder();
// As the node's stream: each connection is named at connect, and `load` goes to the ones whose page asked
let connections = 0;
const shows = new Map();              // a connection's name → { on, seq }
node.load = { load: { headroom: 1 }, gate: null };
function stream(signal) {
  let conn;
  const body = new ReadableStream({ start(c) { conn = c; } });
  conn.name = (++connections).toString(16).padStart(16, '0');
  live.add(conn);
  conn.enqueue(enc.encode('data: {}\n\n'));
  say('stream', { id: conn.name }, conn);
  if (!node.holdRead) say('guidance', answer(), conn);
  signal && signal.addEventListener('abort', () => { live.delete(conn); shows.delete(conn.name); });
  return body;
}
function say(event, payload, only) {
  const text = (event ? `event: ${event}\n` : '') + `data: ${JSON.stringify(payload)}\n\n`;
  for (const conn of only ? [only] : live) conn.enqueue(enc.encode(text));
}
function band(load) {                 // the meter's band change
  node.load = load;
  for (const conn of live) if ((shows.get(conn.name) || {}).on) say('load', load, conn);
}
function drop() {                     // the node ends every connection: its interest goes with it
  for (const conn of live) { shows.delete(conn.name); conn.close(); }
  live.clear();
}
const json = (body) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
function network(input, init = {}) {
  const url = new URL(String(input), ORIGIN);
  const method = (init.method || 'GET').toUpperCase();
  log.push(`${method} ${url.pathname}`);
  if (url.pathname === '/api/settings/library/stream') {
    return Promise.resolve(new Response(stream(init.signal), { status: 200, headers: { 'Content-Type': 'text/event-stream' } }));
  }
  if (url.pathname === '/api/settings/library/stream/load') {
    const word = JSON.parse(init.body);
    log.push(`load ${word.stream.slice(-2)} ${word.on ? 'on' : 'off'} #${word.seq}`);
    const conn = [...live].find(c => c.name === word.stream);
    if (!conn) return Promise.resolve(new Response('{}', { status: 404 }));
    if (word.seq > ((shows.get(conn.name) || {}).seq || 0)) {
      shows.set(conn.name, { on: word.on, seq: word.seq });
      if (word.on) say('load', node.load, conn);          // the snapshot at once
    }
    return Promise.resolve(json({ on: shows.get(conn.name).on }));
  }
  if (url.pathname === '/api/settings/guidance') {
    const computed = answer();        // computed now, answered when the network lets it
    if (node.holdRead) return new Promise(resolve => { node.release = () => resolve(json(computed)); });
    return Promise.resolve(json(computed));
  }
  if (url.pathname === '/api/settings/guidance/audio_output/seen') {
    node.tasks = node.tasks.filter(t => t !== 'audio_output');
    const computed = answer();
    say('', {});                       // the write's own wake,
    say('guidance', computed);         // and the trail it changed
    return Promise.resolve(json(computed));
  }
  return Promise.resolve(json({}));
}

// -- the page --------------------------------------------------------------
const store = new Map([['sautium.device_token', 'page token']]);
const winEvents = new EventTarget(), docEvents = new EventTarget();
const document = {
  readyState: 'complete', getElementById: () => null, hidden: false, visibilityState: 'visible',
  querySelectorAll: () => [],
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
  setTimeout, clearTimeout, Promise, JSON, Set, Map,
  console: { log() {}, debug() {}, warn() {}, error() {} },
};
g.window = g;
g.Sautium = {};
vm.createContext(g);
for (const p of [shaPath, authPath]) vm.runInContext(fs.readFileSync(p, 'utf8'), g);
// app-shell.js's own objects; the fan-out's 200 ms is the page's to say, shortened so ten wakes are ten
vm.runInContext(%(objects)s.replace('}, 200);', '}, 20);') + '\nthis.libraryWake = libraryWake; this.guide = guide;', g);

(async () => {
  const heard = { wakes: 0, loads: [] };
  g.libraryWake.on(() => { heard.wakes += 1; });
  const words = () => log.filter(l => l.startsWith('load '));
  if (scenario === 'interest') {
    // A page on any screen but the one that shows the load: it says nothing, and is sent nothing
    g.guide.start();
    await sleep(120);
    band({ load: { headroom: 0.9 }, gate: null });
    band({ load: { headroom: 0.8 }, gate: null });
    await sleep(60);
    const out = { elsewhere: { words: words(), wakes: heard.wakes } };
    // the Sync screen is drawn: the snapshot at once, then each change
    const screen = g.libraryWake.onEvent('load', (d) => heard.loads.push(d.load.headroom));
    await sleep(60);
    band({ load: { headroom: 0.7 }, gate: null });
    await sleep(60);
    out.shown = { words: words(), loads: [...heard.loads], wakes: heard.wakes };
    // the screen leaves: the word is taken back, and nothing more comes
    screen.abort();
    await sleep(60);
    band({ load: { headroom: 0.6 }, gate: null });
    await sleep(60);
    out.left = { words: words(), loads: [...heard.loads] };
    // back on it, and the connection drops: the next connection is told what this page shows
    g.libraryWake.onEvent('load', (d) => heard.loads.push(d.load.headroom));
    await sleep(60);
    drop();
    await sleep(1400);                // the stream's reconnect after a close: one second
    band({ load: { headroom: 0.5 }, gate: null });
    await sleep(60);
    out.again = { words: words(), loads: [...heard.loads] };
    out.streams = log.filter(l => l === 'GET /api/settings/library/stream').length;
    out.fetched = log.filter(l => l.startsWith('GET ') && l !== 'GET /api/settings/library/stream');
    out.said = log.filter(l => l.startsWith('PUT ')).length;
    process.stdout.write(JSON.stringify(out));
    process.exit(0);
  }
  g.libraryWake.onEvent('load', (d) => heard.loads.push(d));
  g.guide.start();
  await sleep(120);
  const out = { first: [...g.guide.tasks] };
  if (scenario === 'wakes') {
    for (let i = 0; i < 10; i++) { say('', {}); await sleep(50); }
    out.afterWakes = { gets: log.filter(l => l === 'GET /api/settings/guidance').length, wakes: heard.wakes };
    // a task appears: said on the stream, painted, nothing asked
    node.tasks.push('support_request');
    say('guidance', answer());
    await sleep(60);
    out.appeared = [...g.guide.tasks];
    // the load: its own subscriber, no wake of anyone
    const wakes = heard.wakes;
    band({ load: { headroom: 0.7 }, gate: null });
    await sleep(60);
    out.load = { heard: heard.loads, wakes: heard.wakes - wakes };
    // a visit retires a task: the route's answer and the stream's word say the same computation
    await g.guide.seen('audio_output');
    await sleep(60);
    out.seen = [...g.guide.tasks];
  } else if (scenario === 'late-read') {
    // The first read is computed, and held by the network; the trail changes and the stream says so
    node.tasks = ['support_request'];
    say('guidance', answer());
    await sleep(60);
    out.streamed = [...g.guide.tasks];
    node.release();                   // the older answer arrives last
    await sleep(60);
    out.afterLate = [...g.guide.tasks];
    // the node starts again and counts from one: its first word is taken
    node.run = 'r2'; node.seq = 0; node.tasks = ['music_folder'];
    say('guidance', answer());
    await sleep(60);
    out.restarted = [...g.guide.tasks];
  }
  out.log = log;
  process.stdout.write(JSON.stringify(out));
  process.exit(0);
})();
"""


def _run(scenario: str) -> dict:
    objects = json.dumps(_object("libraryWake") + "\n" + _object("guide"))
    proc = subprocess.run([NODE, "-e", _DRIVER % {"objects": objects}, "--", str(STATIC / "sha256.js"),
                           str(STATIC / "auth.js"), scenario], capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr.decode()
    return json.loads(proc.stdout)


def test_the_page_asks_for_the_trail_once_and_never_at_a_wake():
    out = _run("wakes")
    assert out["first"] == ["audio_output"]
    # ten wakes after the connect's own: every one reached the screens, none asked for the trail
    assert out["afterWakes"] == {"gets": 1, "wakes": 11}
    assert out["appeared"] == ["audio_output", "support_request"]
    # the page said its interest once the stream named its connection, and was sent the snapshot at once
    assert out["load"] == {"heard": [{"load": {"headroom": 1}, "gate": None},
                                     {"load": {"headroom": 0.7}, "gate": None}], "wakes": 0}
    assert out["seen"] == ["support_request"]
    assert out["log"].count("GET /api/settings/guidance") == 1
    assert out["log"].count("GET /api/settings/library/stream") == 1


def test_the_page_says_which_connection_shows_the_load_and_no_other_is_sent_it():
    """The node sends `load` to the connection whose page shows it: the page
    says so when something on it listens for the load, takes it back when
    nothing does, and says it again to a connection that replaced a dropped
    one. No timer, and nothing fetched per event."""
    out = _run("interest")
    assert out["elsewhere"] == {"words": [], "wakes": 1}            # the connect's own wake alone
    assert out["shown"] == {"words": ["load 01 on #1"], "loads": [0.8, 0.7], "wakes": 1}
    assert out["left"] == {"words": ["load 01 on #1", "load 01 off #2"], "loads": [0.8, 0.7]}
    assert out["again"]["words"] == ["load 01 on #1", "load 01 off #2", "load 01 on #3", "load 02 on #4"]
    assert out["again"]["loads"] == [0.8, 0.7, 0.6, 0.6, 0.5]       # each connection's snapshot, then the change
    # four words in all, and nothing read: the trail's first paint is the one GET
    assert out["streams"] == 2 and out["said"] == 4 and out["fetched"] == ["GET /api/settings/guidance"]


def test_an_answer_computed_earlier_never_paints_over_a_later_one():
    out = _run("late-read")
    assert out["first"] == []                       # nothing said yet: the read is held
    assert out["streamed"] == ["support_request"]
    assert out["afterLate"] == ["support_request"]          # the held read was computed before it
    assert out["restarted"] == ["music_folder"]


# -- the Sync screen: each part hears its own change ------------------------------

def _function(head: str) -> str:
    start = SHELL.index(f"\n  function {head} {{\n") + 1
    return SHELL[start:SHELL.index("\n  }\n", start) + len("\n  }")]


_SYNC_DRIVER = r"""
'use strict';
const PROFILE_DISPLAY_NAMES = { lite: 'Lite' };
let hash = 'more/sync';
const parseHash = () => hash;
let claims = 0;
const claimFresh = () => { const mine = ++claims; return () => mine === claims; };
const fetched = [];
const _fetchSyncState = async () => { fetched.push(hash); return { load: null }; };
const _renderSync = () => 'the block, drawn again';
const _wireSyncActions = () => {};
const guide = { paint() {} };
const window = new EventTarget();
const libraryWake = {
  wakes: new Set(), named: new Map(),
  on(fn) { this.wakes.add(fn); return { abort: () => this.wakes.delete(fn) }; },
  onEvent(name, fn) {
    if (!this.named.has(name)) this.named.set(name, new Set());
    this.named.get(name).add(fn);
    return { abort: () => this.named.get(name).delete(fn) };
  },
  say(name, payload) { (this.named.get(name) || []).forEach(f => f(payload)); },
};
let _syncStreamCtrl = null;
%(functions)s
const cell = () => ({ textContent: '—', title: '' });
const els = { '[data-sync-content]': { innerHTML: 'as first drawn' }, '[data-sync-load]': cell(), '[data-sync-gate]': cell() };
const root = { querySelector: sel => els[sel] || null };
const settle = () => new Promise(r => setTimeout(r, 10));
(async () => {
  const out = {};
  _subscribeSyncStream(root);
  libraryWake.say('load', { load: { headroom: 0.7, dormant: false, playback: true, profile: 'lite', ceiling: 0.15,
                                    cpu_frac: 0.045, pace: 2.6, mem_available_mib: 812 }, gate: null });
  await settle();
  out.load = { ...els['[data-sync-load]'] };
  out.gateLeft = { ...els['[data-sync-gate]'] };
  out.afterLoad = { fetched: fetched.length, block: els['[data-sync-content]'].innerHTML };
  libraryWake.say('load', { load: { headroom: 1, dormant: true }, gate: { mode: 'shadow', dormant: true } });
  out.gate = els['[data-sync-gate]'].textContent;
  libraryWake.wakes.forEach(f => f());
  await settle();
  window.dispatchEvent(new Event('sautium:identity'));
  await settle();
  window.dispatchEvent(new Event('sautium:notice'));
  await settle();
  window.dispatchEvent(new Event('sautium:chat-changed'));
  await settle();
  out.reads = fetched.length;
  out.block = els['[data-sync-content]'].innerHTML;
  // the person left the screen: the next word finds nobody
  hash = 'home';
  libraryWake.say('load', { load: { headroom: 0.2 }, gate: null });
  for (const name of ['sautium:identity', 'sautium:notice', 'sautium:chat-changed']) window.dispatchEvent(new Event(name));
  await settle();
  out.left = { reads: fetched.length, load: els['[data-sync-load]'].textContent,
               wakes: libraryWake.wakes.size, named: libraryWake.named.get('load').size, ctrl: _syncStreamCtrl };
  process.stdout.write(JSON.stringify(out));
})();
"""


def test_the_sync_screen_paints_the_load_in_place_and_reads_again_for_the_rest():
    functions = "\n".join(_function(head) for head in (
        "_subscribeSyncStream(root)", "_paintSyncLoad(root, said)", "loadLabel(ld)", "loadDetail(ld)",
        "gateLabel(g)", "gateDetail(g)"))
    proc = subprocess.run([NODE, "-e", _SYNC_DRIVER % {"functions": functions}],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["load"] == {"textContent": "70% · playing · active",
                           "title": "profile Lite · ceiling 15% CPU · using 4.5% · announce pace ×2.6 · 812 MiB free"}
    assert out["gateLeft"] == {"textContent": "—", "title": ""}      # no pricer in the process that measured
    assert out["afterLoad"] == {"fetched": 0, "block": "as first drawn"}
    assert out["gate"] == "shadow · dormant"
    # a Library wake, the identity's state, the notices, a friend online: each reads the screen's state again
    assert out["reads"] == 4 and out["block"] == "the block, drawn again"
    assert out["left"] == {"reads": 4, "load": "100% · dormant", "wakes": 0, "named": 0, "ctrl": None}
