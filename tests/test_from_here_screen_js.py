"""backend/static/app-shell.js under Node, PLAYBACK-UI-1: the album page's
"▶ From here" is its own Play all, started at the row (B-156) — the same
route and copies, the start named by the row's track; Listening history's
dots (B-158, the one-dot rule of PROPOSAL §11) — what the page draws from
the live queue, the status and the stop it was given, and what Play sends.
app-shell.js's own functions, run as written; skipped where Node is not."""

import json
import shutil
import subprocess

import pytest

from tests.conftest import REPO_ROOT

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

SOURCE = (REPO_ROOT / "backend" / "static" / "app-shell.js").read_text(encoding="utf-8")


def _function(head: str) -> str:
    start = SOURCE.index(f"\n  function {head} {{\n") + 1
    return SOURCE[start:SOURCE.index("\n  }\n", start) + len("\n  }")]


def _node(script: str):
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# -- B-156: the album's Play all, started at a row ----------------------------------

PLAY_ALL = r"""
'use strict';
const sent = [];
const fetch = (url, opts) => { sent.push([url, JSON.parse(opts.body)]); return Promise.resolve({}); };
%(fn)s
const tracks = [{ track_id: 'a', media_file_id: 1 }, { track_id: 'b', media_file_id: 2 },
                { track_id: 'c', media_file_id: 3, location: 'local' }];
const held = [{ track_id: 'a', media_file_id: 1 }, { track_id: 'b', media_file_id: null, location: 'hqplayer' }];
const page = { albumId: 'ALB', tracks, playOrigin: 'album', originAlbumId: 'ALB' };
playAllRequest(page, null);
playAllRequest(page, 'b');
playAllRequest({ ...page, byAlbum: true }, 'c');
playAllRequest({ ...page, tracks: held }, 'b');
console.log(JSON.stringify(sent));
"""


def test_from_here_is_play_all_of_the_same_page_with_the_rows_track():
    sent = _node(PLAY_ALL % {"fn": _function("playAllRequest(ctx, startTrackId)")})
    base = {"track_ids": [1, 2, 3], "origin": "album", "origin_album_id": "ALB"}
    assert sent == [
        ["/api/player/play-tracks", base],                                    # Play all
        ["/api/player/play-tracks", {**base, "start_track_id": "b"}],         # from track b
        # a file not here now: the album by its uuid, from track c
        ["/api/player/play-entities", {"items": [{"kind": "album", "id": "ALB"}],
                                       "start_track_id": "c"}],
        # a pick held at the HQPlayer: the same
        ["/api/player/play-entities", {"items": [{"kind": "album", "id": "ALB"}],
                                       "start_track_id": "b"}],
    ]


def test_the_button_is_on_the_album_pages_bars_alone_with_its_name():
    """ctx.albumId is the album page's alone (renderAlbum); its three bars
    — owned, streamed, held — take the action, and the button names itself."""
    wire = _function("wireDetailHandlers(screen, ctx = {})")
    assert "const fromHere = ctx.albumId" in wire
    for call in ("openQueueConfirm(row, fromHere)", "openPhantomQueueConfirm(screen, row, fromHere)",
                 "openHeldQueueConfirm(row, fromHere)"):
        assert call in wire
    add = _function("addFromHere(bar, row, fromHere)")
    assert "'Play the album from this track'" in add and "From here</button>" in add
    assert SOURCE.count("wireDetailHandlers(screen, {\n        albumId,") == 1


# -- B-158: the dots ---------------------------------------------------------------

DOTS = r"""
'use strict';
%(fn)s
const slots = ['a', 'b', 'c', 'b'], positions = [0, 1, 2, 3];
const q = slots.map(t => ({ track_id: t }));
const stop = { position: 2, seconds: 75 };
const cases = %(cases)s;
console.log(JSON.stringify(cases.map(c => sessionDotView(
  slots, positions, c.live ? q : [{ track_id: 'x' }], c.status, c.stop === null ? null : stop, c.chosen || null))));
"""


def _dots(cases):
    return _node(DOTS % {"fn": _function(
        "sessionDotView(slots, positions, playlist, status, stop, chosen)"),
        "cases": json.dumps(cases)})


def test_the_stop_kept_is_the_blue_dot_and_play_resumes_it():
    [v] = _dots([{"live": False, "status": {"state": "stopped"}}])
    assert (v["playing"], v["dot"], v["resume"]) == (False, {"position": 2, "seconds": 75}, True)


def test_a_tap_moves_the_dot_and_play_starts_there_from_the_top():
    [v, back] = _dots([{"live": False, "status": {}, "chosen": {"position": 1, "seconds": 0}},
                       {"live": False, "status": {}, "chosen": {"position": 2, "seconds": 75}}])
    assert (v["dot"], v["resume"]) == ({"position": 1, "seconds": 0}, False)
    assert back["resume"] is True                       # back on the stop: Resume


def test_while_the_session_plays_the_amber_dot_alone_by_slot():
    """A track the session holds twice is two slots: the status's slot is
    the row."""
    views = _dots([{"live": True, "status": {"state": s, "track_index": 4}}
                   for s in ("playing", "paused", "loading")])
    assert all(v == {"playing": True, "at": 4, "stop": None, "dot": None, "resume": False}
               for v in views)


def test_another_queue_playing_leaves_the_blue_dot():
    [v] = _dots([{"live": False, "status": {"state": "playing", "track_index": 1}}])
    assert v["playing"] is False and v["dot"] == {"position": 2, "seconds": 75}


def test_when_the_play_stops_its_stop_is_the_new_blue_dot():
    [v] = _dots([{"live": True, "status": {"state": "stopped",
                                           "heard": {"index": 2, "track_id": "b", "position": 31}}}])
    assert (v["dot"], v["resume"]) == ({"position": 1, "seconds": 31}, True)


def test_a_session_with_no_stop_draws_no_dot_and_plays_from_the_top():
    [v] = _dots([{"live": False, "status": {}, "stop": None}])
    assert (v["dot"], v["resume"]) == (None, False)


def test_play_sends_the_dot_drawn_and_the_session_rows_choose():
    wire = _function("wireDetailHandlers(screen, ctx = {})")
    assert "body: JSON.stringify({ session_id: ctx.sessionId, ...(start ? { start } : {}) })" in wire
    # the generic taps (play the one track) stay off a page whose rows choose
    assert wire.count("if (!ctx.rowsChoose) screen.querySelectorAll(") == 3
    assert "sessionId, rowsChoose: true," in SOURCE
    # and the generic amber pass leaves the session page to its own
    hl = _function("updatePlayingHighlight()")
    assert hl.count(".detail-screen:not([data-session-id])") == 3


# -- B-158: the page's own wiring, on a DOM of stand-ins ---------------------------

WIRE = r"""
'use strict';
const listeners = { doc: {}, win: {} };
const on = (where) => (type, fn) => ((listeners[where][type] = listeners[where][type] || []).push(fn));
const off = (where) => (type, fn) => {
  listeners[where][type] = (listeners[where][type] || []).filter(f => f !== fn); };
const document = { addEventListener: on('doc'), removeEventListener: off('doc') };
const window = { addEventListener: on('win'), removeEventListener: off('win'),
                 currentPlaylist: [], currentStatus: { state: 'stopped' },
                 togglePlayPause: () => sent.push(['toggle']) };
const fire = (where, type) => (listeners[where][type] || []).slice().forEach(f => f());
const sent = [];
const fetch = (url, opts) => { sent.push([url, JSON.parse(opts.body)]);
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true }) }); };
const onceInFlight = (el, fn) => fn();
const reportPlaybackResult = async () => true;
const answerSays = () => false;
const notices = { toast() {} };
const escapeHtml = s => String(s);
const SVG_PLAY = '<svg/>';
const fmtDuration = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
%(view)s
%(wire)s
const classes = () => { const set = new Set(); return {
  toggle: (c, on) => (on ? set.add(c) : set.delete(c)), contains: c => set.has(c), has: c => set.has(c) }; };
const ids = ['a', 'b', 'c', 'd'];
const rows = ids.map((t, i) => {
  const dur = { dataset: { dur: '3:20' }, innerHTML: '3:20' };
  const row = { dataset: { position: String(i), trackId: t }, classList: classes(), handlers: [],
                querySelector: () => dur, dur,
                addEventListener: (type, fn) => row.handlers.push(fn) };
  return row;
});
const label = { textContent: 'Play' };
const screen = { isConnected: true, querySelectorAll: () => rows,
                 querySelector: () => ({ querySelector: () => label }) };
const tap = (i) => rows[i].handlers.forEach(f => f({ target: { closest: () => null }, stopPropagation() {} }));
const look = () => ({ start: rows.findIndex(r => r.classList.has('is-start')),
                      playing: rows.findIndex(r => r.classList.has('is-playing')),
                      dur: rows.map(r => r.dur.innerHTML.replace(/<[^>]+>/g, '')).find(h => h !== '3:20') || null,
                      label: label.textContent });
const out = [];
(async () => {
  wireSessionDot(screen, { tracks: ids.map((t, i) => ({ track_id: t, position: i })),
                           stop: { position: 2, seconds: 75 } });
  out.push(look());                                   // 0: the stop
  tap(0); out.push(look()); out.push(sent.length);    // 1: a tap moves it, 2: nothing sent
  window.currentPlaylist = ids.map(t => ({ track_id: t }));
  window.currentStatus = { state: 'playing', track_index: 2 };
  fire('doc', 'np-update'); out.push(look());         // 3: playing: the amber dot alone
  tap(3); await new Promise(r => setTimeout(r, 0));
  tap(1); out.push(sent.slice());                     // 4: a jump, then its own row toggles
  window.currentPlaylist = [{ track_id: 'x' }];
  window.currentStatus = { state: 'stopped' };
  fire('doc', 'np-update'); out.push(look());         // 5: the choice gave way: the stop again
  screen.isConnected = false;
  fire('win', 'sautium:route');
  out.push([(listeners.doc['np-update'] || []).length, (listeners.doc['playlist-loaded'] || []).length,
            (listeners.win['sautium:route'] || []).length]);   // 6: let go with its screen
  console.log(JSON.stringify(out));
})();
"""


def test_the_session_page_draws_one_dot_and_its_taps_choose_or_jump():
    out = _node(WIRE.replace("%(view)s", _function(
        "sessionDotView(slots, positions, playlist, status, stop, chosen)"))
        .replace("%(wire)s", _function("wireSessionDot(screen, d)")))
    stop, moved, nothing_sent, playing, sent, back, let_go = out
    assert stop == {"start": 2, "playing": -1, "dur": "1:15 / 3:20", "label": "Resume"}
    assert moved == {"start": 0, "playing": -1, "dur": None, "label": "Play"} and nothing_sent == 0
    assert playing == {"start": -1, "playing": 1, "dur": None, "label": "Play"}
    assert sent == [["/api/player/jump", {"index": 4, "track_id": "d"}], ["toggle"]]
    assert back == stop
    assert let_go == [0, 0, 0]
