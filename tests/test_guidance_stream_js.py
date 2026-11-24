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
function stream(signal) {
  let conn;
  const body = new ReadableStream({ start(c) { conn = c; } });
  live.add(conn);
  conn.enqueue(enc.encode('data: {}\n\n'));
  if (!node.holdRead) say('guidance', answer(), conn);
  signal && signal.addEventListener('abort', () => live.delete(conn));
  return body;
}
function say(event, payload, only) {
  const text = (event ? `event: ${event}\n` : '') + `data: ${JSON.stringify(payload)}\n\n`;
  for (const conn of only ? [only] : live) conn.enqueue(enc.encode(text));
}
const json = (body) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
function network(input, init = {}) {
  const url = new URL(String(input), ORIGIN);
  const method = (init.method || 'GET').toUpperCase();
  log.push(`${method} ${url.pathname}`);
  if (url.pathname === '/api/settings/library/stream') {
    return Promise.resolve(new Response(stream(init.signal), { status: 200, headers: { 'Content-Type': 'text/event-stream' } }));
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
  g.libraryWake.onEvent('load', (d) => heard.loads.push(d));
  g.guide.start();
  await sleep(120);
  const out = { first: [...g.guide.tasks] };
  if (scenario === 'wakes') {
    for (let i = 0; i < 10; i++) { say('', {}); await sleep(50); }
    out.afterWakes = { gets: log.filter(l => l === 'GET /api/settings/guidance').length, wakes: heard.wakes };
    // a task appears: said on the stream, painted, nothing asked
    node.tasks.push('notices');
    say('guidance', answer());
    await sleep(60);
    out.appeared = [...g.guide.tasks];
    // the load: its own subscriber, no wake of anyone
    const wakes = heard.wakes;
    say('load', { load: { headroom: 0.7 }, gate: null });
    await sleep(60);
    out.load = { heard: heard.loads, wakes: heard.wakes - wakes };
    // a visit retires a task: the route's answer and the stream's word say the same computation
    await g.guide.seen('audio_output');
    await sleep(60);
    out.seen = [...g.guide.tasks];
  } else if (scenario === 'late-read') {
    // The first read is computed, and held by the network; the trail changes and the stream says so
    node.tasks = ['notices'];
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
    assert out["appeared"] == ["audio_output", "notices"]
    assert out["load"] == {"heard": [{"load": {"headroom": 0.7}, "gate": None}], "wakes": 0}
    assert out["seen"] == ["notices"]
    assert out["log"].count("GET /api/settings/guidance") == 1
    assert out["log"].count("GET /api/settings/library/stream") == 1


def test_an_answer_computed_earlier_never_paints_over_a_later_one():
    out = _run("late-read")
    assert out["first"] == []                       # nothing said yet: the read is held
    assert out["streamed"] == ["notices"]
    assert out["afterLate"] == ["notices"]          # the held read was computed before it
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
