"""backend/static/player.js's transport primitives under Node: a word the
output did not take is said to the person through the shell's one reporter
(reportPlaybackResult) — once, and only in the output's own words. Skipped
where Node is not."""

import json
import shutil
import subprocess
import sys
import types

import pytest

from tests.conftest import REPO_ROOT

if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

from hqplayer_client import PlaybackState, TrackStatus  # noqa: E402
from playback import hqp_diagnostics as diag  # noqa: E402
from playback.manager import PlaybackManager  # noqa: E402
from playback.queue import QueueItem  # noqa: E402
from tests.test_hqp_backend import (_closed, _queue_stays_in_memory, _wait, fake,  # noqa: E402,F401
                                    listens, output, traced)

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

_DRIVER = r"""
const fs = require('node:fs'), vm = require('node:vm');
const reported = [];
const window = {
  addEventListener() {}, dispatchEvent() {},
  reportPlaybackResult: async (resp) => { reported.push(resp ? resp.status : null); return false; },
};
const document = { readyState: 'loading', addEventListener() {}, visibilityState: 'visible' };
const fetch = async () => ({ ok: false, status: 409,
                             json: async () => ({ detail: 'HQPlayer is not answering' }) });
vm.runInNewContext(fs.readFileSync(process.argv[process.argv.length - 1], 'utf8'), {
  window, document, fetch, crypto: require('node:crypto').webcrypto, CustomEvent: class {},
  console: { warn() {}, log() {}, error() {} }, setTimeout, clearTimeout,
});
(async () => {
  const out = {};
  for (const word of JSON.parse(fs.readFileSync(0, 'utf8'))) {
    reported.length = 0;
    await (word === 'seek' ? window.playerSeek(60) : window.playerCmd(word));
    out[word] = reported.slice();
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def test_a_pause_a_stop_or_a_seek_the_output_did_not_take_is_said():
    """A Pause, a Stop or a Seek the route answers it did not take —
    HQPlayer building, or not answering past a short silence — is said, as a
    Play is: the music goes on, and the person must know their word did not
    go."""
    words = ["pause", "stop", "seek", "play"]
    proc = subprocess.run(
        [NODE, "-e", _DRIVER, "--", str(REPO_ROOT / "backend" / "static" / "player.js")],
        input=json.dumps(words).encode(), capture_output=True, check=True)
    assert json.loads(proc.stdout) == {w: [409] for w in words}, proc.stdout


_TAKEN = r"""
const fs = require('node:fs'), vm = require('node:vm');
const reported = [];
const window = {
  addEventListener() {}, dispatchEvent() {},
  reportPlaybackResult: async (resp, body, word) => { reported.push([resp.status, body, word]); return false; },
};
const document = { readyState: 'loading', addEventListener() {}, visibilityState: 'visible' };
const spec = JSON.parse(fs.readFileSync(0, 'utf8'));
const fetch = async () => ({ ok: true, status: 200, json: async () => spec.answer });
vm.runInNewContext(fs.readFileSync(process.argv[process.argv.length - 1], 'utf8'), {
  window, document, fetch, crypto: require('node:crypto').webcrypto, CustomEvent: class {},
  console: { warn() {}, log() {}, error() {} }, setTimeout, clearTimeout,
});
(async () => {
  const out = {};
  for (const word of spec.words) {
    reported.length = 0;
    await (word === 'seek' ? window.playerSeek(60) : window.playerCmd(word));
    out[word] = reported.slice();
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def _taken(answer, words):
    proc = subprocess.run(
        [NODE, "-e", _TAKEN, "--", str(REPO_ROOT / "backend" / "static" / "player.js")],
        input=json.dumps({"answer": answer, "words": words}).encode(), capture_output=True, check=True)
    return json.loads(proc.stdout)


def test_a_word_the_output_refused_in_its_own_words_is_said():
    """The route took the word to the output, and the output refused it in
    its own words (HQPlayer's `result="Error"`): HTTP 200 with ok false and
    the refusal. Said, as a word the route refused is — the music goes on."""
    words = ["pause", "stop", "seek", "play", "next", "previous"]
    refused = {"ok": False, "refusal": "HQPlayer refused Pause"}
    assert _taken(refused, words) == {w: [[200, refused, w]] for w in words}


def test_a_word_the_output_took_is_said_to_nobody():
    words = ["pause", "seek", "play"]
    assert _taken({"ok": True}, words) == {w: [] for w in words}
    assert _taken({"ok": True, "starts": {"index": 2}}, words) == {w: [] for w in words}


# The person's side, end to end: the route's answer and the status the
# real backend gives, fed to the real player.js and the real reporter and
# status listeners of app-shell.js; the toasts a person sees are counted

_SHELL = r"""
const fs = require('node:fs'), vm = require('node:vm');
const [playerPath, shellPath] = process.argv.slice(-2);
const shell = fs.readFileSync(shellPath, 'utf8');
const cut = (from, to) => {
  const a = shell.indexOf(from), b = shell.indexOf(to, a);
  if (a < 0 || b < 0) throw new Error('marker not found: ' + (a < 0 ? from : to));
  return shell.slice(a, b);
};
const spec = JSON.parse(fs.readFileSync(0, 'utf8'));
const npHandlers = [], toasts = [];
let calls = 0;
// notices.toast as it shows them: one with the key of a toast still up
// takes it over in place (`calls` counts every one asked for)
const toast = t => {
  calls += 1;
  const i = t.key ? toasts.findIndex(x => x.key === t.key) : -1;
  if (i >= 0) toasts[i] = t; else toasts.push(t);
};
const ctx = {
  window: { addEventListener() {}, dispatchEvent() {} },
  document: { readyState: 'loading', visibilityState: 'visible',
              addEventListener(type, fn) { if (type === 'np-update') npHandlers.push(fn); } },
  fetch: async (url) => {
    const a = spec.answers[url];
    return { ok: a.status >= 200 && a.status < 300, status: a.status, json: async () => a.body };
  },
  crypto: require('node:crypto').webcrypto, CustomEvent: class {},
  console: { warn() {}, log() {}, error() {}, debug() {} }, setTimeout, clearTimeout,
  notices: { toast }, __tracks: spec.tracks || [],
  openHqpDiagnosis() {}, navigate() {},
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(playerPath, 'utf8'), ctx);
vm.runInContext([
  cut('  function escapeHtml(s) {', '\n  /* ---------- Now-playing track highlight'),
  cut('  function escapeProfileHtml(s) {', '\n  function statusBadgeHTML'),
  cut('  // Playback 503 = the configured output', '\n  // Backend-async playback failures ride'),
  cut('  // Backend-async playback failures ride', '\n  // A DSP change that went through a build'),
  cut('  // Surface a queue/play failure as a toast', '\n  // Grey out + disable the phantom track rows'),
  // the queue sheet's tap, as the sheet calls it
  'const __sheet = { tracks: __tracks, ' + cut('    async jumpTo(index) {', '\n    // Pointer-events drag') + ' };',
  'this.__sheet = __sheet;',
].join('\n'), ctx);
(async () => {
  for (const ev of spec.events) {
    if (ev.np) { ctx.window.currentStatus = ev.np; npHandlers.forEach(h => h({ detail: ev.np })); }
    else if (ev.cmd) await ctx.window.playerCmd(ev.cmd);
    else if (ev.seek != null) await ctx.window.playerSeek(ev.seek);
    else if (ev.jump != null) await ctx.__sheet.jumpTo(ev.jump);
  }
  process.stdout.write(JSON.stringify({ calls, toasts: toasts.map(t => ({
    key: t.key, kind: t.kind, title: t.title, text: t.text, action: t.action ? t.action.label : null })) }));
})().catch(e => { process.stderr.write(String(e && e.stack)); process.exit(1); });
"""


def _ui(events, answers, tracks=None):
    return _ui_run(events, answers, tracks)["toasts"]


def _ui_run(events, answers, tracks=None):
    proc = subprocess.run(
        [NODE, "-e", _SHELL, "--", str(REPO_ROOT / "backend" / "static" / "player.js"),
         str(REPO_ROOT / "backend" / "static" / "app-shell.js")],
        input=json.dumps({"events": events, "answers": answers, "tracks": tracks or []},
                         default=str).encode(),
        capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()
    return json.loads(proc.stdout)


def test_a_pause_and_a_seek_hqplayer_refused_are_each_said_once_in_its_words(traced, fake, monkeypatch):
    import routers.player as player

    fake.replies["Pause"] = '<Pause result="Error"/>'
    fake.replies["Seek"] = '<Seek result="Error">Not seekable</Seek>'
    monkeypatch.setattr(player, "manager", types.SimpleNamespace(
        ensure_active=lambda: traced, backend=lambda: traced))
    paused = player.pause()
    sought = player.seek(player.SeekRequest(position=60))
    assert paused == {"ok": False, "refusal": "HQPlayer refused Pause"}, paused
    assert sought == {"ok": False, "refusal": "Not seekable"}, sought
    out = {"type": "hqplayer", "label": "Study <HQP>", "attached": True}     # a name of its own: escaped
    np = {"np": {"state": "playing", "output": out}}
    assert _ui([np, {"cmd": "pause"}], {"/api/player/pause": {"status": 200, "body": paused}}) == [
        {"key": "player.refused", "kind": "error", "title": "Study &lt;HQP&gt; did not take the Pause",
         "text": "HQPlayer refused Pause", "action": None}]
    assert _ui([np, {"seek": 60}], {"/api/player/seek": {"status": 200, "body": sought}}) == [
        {"key": "player.refused", "kind": "error", "title": "Study &lt;HQP&gt; did not take the Seek",
         "text": "Not seekable", "action": None}]


@pytest.mark.parametrize("diagnosis_first", [False, True])
def test_a_play_hqplayer_refused_is_said_once_with_its_diagnosis(traced, fake, monkeypatch,
                                                                 diagnosis_first):
    """A Play HQPlayer refuses in its words closes its attempt rejected, which
    the status offers as a diagnosis: one failure, said once in the tab that
    asked, as the route's answer or the diagnosis comes first."""
    import routers.player as player

    fake.replies["Play"] = '<Play result="Error">Empty transport</Play>'
    monkeypatch.setattr(player, "manager", types.SimpleNamespace(
        ensure_active=lambda: traced, backend=lambda: traced))
    answer = player.play()
    a = _closed(timeout=5.0)
    assert answer == {"ok": False, "refusal": "Empty transport", "attempt": a.id}, answer
    assert _wait(lambda: traced._last_closed is a, 5.0)
    stopped = TrackStatus(state=PlaybackState.STOPPED, track_index=1, track_id="", position=0.0,
                          length=300.0, volume=-3.0)
    d = traced._diagnosis(stopped)
    assert d is not None and d["code"] == "rejected" and d["id"] == a.id and not d["failing"], d
    out = {"type": "hqplayer", "label": "HQPlayer", "attached": True}
    diagnosed = {"np": {"state": "stopped", "output": out, "diagnosis": d}}
    events = [{"np": {"state": "stopped", "output": out}}, {"cmd": "play"}, diagnosed]
    if diagnosis_first:
        events = [events[0], diagnosed, events[1]]
    toasts = _ui(events, {"/api/player/play": {"status": 200, "body": answer}})
    assert len(toasts) == 1 and toasts[0]["action"] == "Why?", toasts


def test_a_failure_said_is_not_said_again_when_its_diagnosis_comes_back(traced, fake, monkeypatch):
    """A refused Play said with its Why?; the status then loses the
    diagnosis — HQPlayer away a while, or another entry played and stopped —
    and gets it back: no new failure, nothing said again."""
    import routers.player as player

    fake.replies["Play"] = '<Play result="Error">Empty transport</Play>'
    monkeypatch.setattr(player, "manager", types.SimpleNamespace(
        ensure_active=lambda: traced, backend=lambda: traced))
    answer = player.play()
    a = _closed(timeout=5.0)
    assert _wait(lambda: traced._last_closed is a, 5.0)
    stopped = TrackStatus(state=PlaybackState.STOPPED, track_index=1, track_id="", position=0.0,
                          length=300.0, volume=-3.0)
    d = traced._diagnosis(stopped)
    out = {"type": "hqplayer", "label": "HQPlayer", "attached": True}
    diagnosed = {"np": {"state": "stopped", "output": out, "diagnosis": d}}
    run = _ui_run([{"np": {"state": "stopped", "output": out}}, {"cmd": "play"}, diagnosed,
                   {"np": {"state": "stopped", "output": out}}, diagnosed],
                  {"/api/player/play": {"status": 200, "body": answer}})
    # the answer, then the diagnosis taking it over: two asked, none after
    assert run["calls"] == 2 and run["toasts"][0]["action"] == "Why?", run


def test_a_tab_opened_after_a_failure_never_says_it(traced, fake, monkeypatch):
    """The tab's first status already carries a diagnosis: it paints the
    tag and says nothing — nor when the status loses it and gets it back."""
    import routers.player as player

    fake.replies["Play"] = '<Play result="Error">Empty transport</Play>'
    monkeypatch.setattr(player, "manager", types.SimpleNamespace(
        ensure_active=lambda: traced, backend=lambda: traced))
    player.play()
    a = _closed(timeout=5.0)
    assert _wait(lambda: traced._last_closed is a, 5.0)
    stopped = TrackStatus(state=PlaybackState.STOPPED, track_index=1, track_id="", position=0.0,
                          length=300.0, volume=-3.0)
    d = traced._diagnosis(stopped)
    out = {"type": "hqplayer", "label": "HQPlayer", "attached": True}
    diagnosed = {"np": {"state": "stopped", "output": out, "diagnosis": d}}
    run = _ui_run([diagnosed, {"np": {"state": "stopped", "output": out}}, diagnosed], {})
    assert run["calls"] == 0, run


def test_a_failure_met_in_a_failing_run_is_not_said_when_the_run_ends(traced, fake):
    """Three taps HQPlayer refuses, on slots 1, 2 and 1: the first two are
    said with their Why?, the third is the failing run's — its notice
    speaks. HQPlayer then plays slot 2 from its own window and stops: the
    run is over, and the third attempt comes back without `failing` —
    met already, it is no news."""
    fake.replies["SelectTrack"] = '<SelectTrack result="Error">No such track</SelectTrack>'
    stopped = TrackStatus(state=PlaybackState.STOPPED, track_index=1, track_id="", position=0.0,
                          length=300.0, volume=-3.0)
    seen = []
    for slot in (1, 2, 1):
        assert traced.select(slot) is False
        attempt = traced.refused_attempt()
        assert _wait(lambda: traced._last_closed is not None and traced._last_closed.id == attempt, 5.0)
        seen.append(traced._diagnosis(stopped))
    assert [d["failing"] for d in seen] == [False, False, True], seen
    assert diag.note_playing(2, traced._queue.generation)
    ended = traced._diagnosis(stopped)
    assert ended["id"] == seen[2]["id"] and not ended["failing"], ended
    out = {"type": "hqplayer", "label": "HQPlayer", "attached": True}
    bare = {"np": {"state": "stopped", "output": out}}
    run = _ui_run([bare] + [{"np": {"state": "stopped", "output": out, "diagnosis": d}} for d in seen]
                  + [{"np": {"state": "playing", "output": out}},
                     {"np": {"state": "stopped", "output": out, "diagnosis": ended}}], {})
    assert run["calls"] == 2 and [t["action"] for t in run["toasts"]] == ["Why?"], run


@pytest.mark.parametrize("diagnosis_first", [False, True])
def test_a_tap_hqplayer_refused_is_said_once_with_its_why(traced, fake, monkeypatch, diagnosis_first):
    """A queue tap HQPlayer refuses: its answer comes before the attempt's
    diagnosis, which names the tapped track while Now Playing stands on
    another — the one toast keeps the way to Why?."""
    import routers.player as player

    fake.replies["SelectTrack"] = '<SelectTrack result="Error">No such track</SelectTrack>'
    monkeypatch.setattr(player, "manager", types.SimpleNamespace(
        ensure_active=lambda: traced, backend=lambda: traced, active=traced, queue=traced._queue))
    answer = player.jump(player.JumpRequest(index=2, track_id="track-2"))
    assert answer["ok"] is False and answer["refusal"] == "No such track" and answer["attempt"], answer
    assert _wait(lambda: traced._last_closed is not None
                 and traced._last_closed.id == answer["attempt"], 5.0)
    stopped = TrackStatus(state=PlaybackState.STOPPED, track_index=1, track_id="", position=0.0,
                          length=300.0, volume=-3.0)
    d = traced._diagnosis(stopped)
    assert d is not None and d["id"] == answer["attempt"] and not d["failing"], d
    out = {"type": "hqplayer", "label": "HQPlayer", "attached": True}
    diagnosed = {"np": {"state": "stopped", "output": out, "diagnosis": d}}
    events = [{"np": {"state": "stopped", "output": out}}, {"jump": 2}, diagnosed]
    if diagnosis_first:
        events = [events[0], diagnosed, events[1]]
    toasts = _ui(events, {"/api/player/jump": {"status": 200, "body": answer}},
                 [{"track_id": "track-1"}, {"track_id": "track-2"}])
    assert len(toasts) == 1 and toasts[0]["action"] == "Why?", toasts


def test_a_next_hqplayer_refused_while_it_plays_is_said_in_its_words(fake, output, monkeypatch):
    """HQPlayer plays on after a Next it refused: no diagnosis shows, and the
    answer's toast stands, in HQPlayer's words."""
    import routers.player as player

    mgr, b = output
    monkeypatch.setattr(player, "manager", mgr)
    fake.replies["Next"] = '<Next result="Error">No next track</Next>'
    before = dict(mgr.latest_status)
    answer = player.next_track()
    a = _closed(timeout=5.0)
    assert answer == {"ok": False, "refusal": "No next track", "attempt": a.id}, answer
    assert _wait(lambda: b._last_closed is a, 5.0)
    after = dict(mgr.latest_status)
    assert after["state"] == "playing" and "diagnosis" not in after, after
    toasts = _ui([{"np": before}, {"cmd": "next"}, {"np": after}],
                 {"/api/player/next": {"status": 200, "body": answer}})
    assert [(t["key"], t["text"]) for t in toasts] == [("hqplayer.diagnosis", "No next track")], toasts


def test_a_pause_a_dlna_renderer_failed_is_said_once(monkeypatch):
    """A Pause the renderer fails (a SOAP fault): its words ride the status
    (`error`), and the route's answer carries none — said once."""
    import routers.player as player
    from playback import dlna_backend
    from playback.dlna_backend import DlnaBackend

    mgr = PlaybackManager()
    b = DlnaBackend(emit=mgr._on_backend_status, queue=mgr.queue,
                    renderer={"udn": "uuid:stub", "name": "Kitchen",
                              "location": "http://127.0.0.1:9/desc.xml"})

    class Refusing:
        async def async_call(self, **kwargs):
            raise RuntimeError("UPnP error 701: Transition not available")

    b._dmr = types.SimpleNamespace(_action=lambda service, name: Refusing(), is_subscribed=True,
                                   transport_state=None, volume_level=None)
    b._loop = dlna_backend._ensure_loop()
    mgr._active = b
    monkeypatch.setattr(player, "manager", mgr)
    b._emit_now()
    before = dict(mgr.latest_status)
    answer = player.pause()
    after = dict(mgr.latest_status)
    assert answer == {"ok": False, "refusal": ""}
    assert after["error"] == "'Kitchen' pause failed (UPnP error 701: Transition not available)"
    toasts = _ui([{"np": before}, {"np": after}, {"cmd": "pause"}],
                 {"/api/player/pause": {"status": 200, "body": answer}})
    assert [t["key"] for t in toasts] == ["player.error"], toasts


def test_a_next_the_browser_cannot_play_is_said_once(monkeypatch):
    """The browser output, the slots after the playing one in an HQPlayer's
    own library: it skips them and says why on its status — said once."""
    import routers.player as player
    import routers.settings as settings_router
    from playback.browser_backend import BrowserBackend

    monkeypatch.setattr(settings_router, "_read", lambda key: None)
    mgr = PlaybackManager()
    mgr.queue.replace(
        [QueueItem(track_id="t1", media_file_id=1,
                   source={"kind": "file", "path": "/music/1.flac", "format": "FLAC"},
                   title="T1", artist="A")]
        + [QueueItem(track_id=f"t{i}", media_file_id=None,
                     source={"kind": "hqp", "path": f"/hqp/{i}.flac", "format": "FLAC",
                             "endpoint": 7},
                     title=f"T{i}", artist="A") for i in (2, 3)])
    b = BrowserBackend(emit=mgr._on_backend_status, queue=mgr.queue)
    mgr._active = b
    b.resume_at(1)
    monkeypatch.setattr(player, "manager", mgr)
    before = dict(mgr.latest_status)
    answer = player.next_track()
    after = dict(mgr.latest_status)
    assert answer == {"ok": False, "refusal": ""}
    assert "live in the HQPlayer's own library" in after["error"]
    toasts = _ui([{"np": before}, {"np": after}, {"cmd": "next"}],
                 {"/api/player/next": {"status": 200, "body": answer}})
    assert [t["key"] for t in toasts] == ["player.error"], toasts


def test_a_next_with_nothing_after_it_is_no_refusal(monkeypatch):
    """The browser output on the queue's last track: Next has nowhere to go,
    and says nothing — no words, no toast."""
    import routers.player as player
    from playback.browser_backend import BrowserBackend

    mgr = PlaybackManager()
    mgr.queue.replace([QueueItem(track_id=f"t{i}", media_file_id=i,
                                 source={"kind": "file", "path": f"/music/{i}.flac",
                                         "format": "FLAC"},
                                 title=f"T{i}", artist="A") for i in (1, 2)])
    b = BrowserBackend(emit=mgr._on_backend_status, queue=mgr.queue)
    mgr._active = b
    b.resume_at(2)
    monkeypatch.setattr(player, "manager", mgr)
    answer = player.next_track()
    assert answer == {"ok": False, "refusal": ""}
    assert _ui([{"np": dict(mgr.latest_status)}, {"cmd": "next"}],
               {"/api/player/next": {"status": 200, "body": answer}}) == []
