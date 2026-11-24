"""backend/static/app-shell.js under Node: what the pages say of the owner's
music with no file that opens here now (PI-NODE-2D §3.4, §9.1) — the album
screen's states (everything here; none here and this node streams; none
here and nothing streams; the mix), the Library screen's scan buttons and
path row while the music folder does not answer (F13), and the open
screen's payload asked again when the notice library.mount_missing begins
or ends, repainted only when it says otherwise. app-shell.js's own
functions, run as written against stand-ins for the DOM; skipped where Node
is not. No browser renders the page here."""

import json
import shutil
import subprocess

import pytest

from tests.conftest import REPO_ROOT

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

SOURCE = (REPO_ROOT / "backend" / "static" / "app-shell.js").read_text(encoding="utf-8")


def _block(start: str, end: str = "\n  }\n") -> str:
    """The source from the line that begins with `start` through `end`."""
    at = SOURCE.index("\n  " + start) + 1
    return SOURCE[at:SOURCE.index(end, at) + len(end)]


WORDS = "\n".join([
    _block("const NOT_HERE = {", "\n  };\n"),
    _block("function notHereWords(why) {"),
    _block("function albumOut(d) {"),
    _block("function albumRowKind(t, out) {"),
])


def _node(script: str):
    out = subprocess.run([NODE, "-e", "'use strict';\n" + script], capture_output=True, text=True,
                         timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _album(payload: dict) -> dict:
    return _node(WORDS + f"""
const d = {json.dumps(payload)};
const out = albumOut(d);
console.log(JSON.stringify({{ out, rows: d.tracks.map(t => albumRowKind(t, out)) }}));
""")


HERE, OUT = {"here": True}, {"here": False}


def test_an_album_all_here_is_the_page_as_it_was():
    assert _album({"is_owned": True, "away": None, "away_part": None, "tracks": [HERE, HERE]}) == {
        "out": None, "rows": ["file", "file"]}
    # a payload of before the field, and a not-owned album, read the same
    assert _album({"is_owned": True, "tracks": [{}]}) == {"out": None, "rows": ["file"]}
    assert _album({"is_owned": False, "away": None, "tracks": [{}]})["out"] is None


def test_an_album_all_out_on_a_node_that_streams_says_so_and_its_rows_stream():
    page = _album({"is_owned": True, "away": {"why": "disk", "streams": True}, "away_part": None,
                   "tracks": [OUT, OUT]})
    assert page["rows"] == ["stream", "stream"]
    assert page["out"] == {
        "all": True, "streams": True, "noPlay": False,
        "title": "On a disk that is not connected",
        "text": "Playing it now streams it: each track once in full, then 30-second excerpts — "
                "until the disk is back.",
        "button": "Its disk is not connected"}
    assert _node(WORDS + "console.log(JSON.stringify([notHereWords('disk').mark, "
                 "notHereWords('away').mark]));") == ["not connected", "away"]


def test_an_album_all_out_where_nothing_streams_refuses_play_on_the_page_with_the_sentence():
    page = _album({"is_owned": True, "away": {"why": "disk", "streams": False}, "away_part": None,
                   "tracks": [OUT, OUT]})
    assert page["rows"] == ["waits", "waits"]
    assert (page["out"]["noPlay"], page["out"]["title"], page["out"]["text"], page["out"]["button"]) == (
        True, "On a disk that is not connected", "It cannot play until the disk is connected.",
        "Its disk is not connected")
    away = _album({"is_owned": True, "away": {"why": "away", "streams": False}, "away_part": None,
                   "tracks": [OUT]})["out"]
    assert (away["noPlay"], away["title"], away["text"]) == (
        True, "Your copy is away", "It cannot play until its files are back.")


def test_an_album_partly_out_reads_each_row_by_its_own_file_and_keeps_play():
    page = _album({"is_owned": True, "away": None, "away_part": {"why": "disk", "streams": False},
                   "tracks": [HERE, OUT]})
    assert page["rows"] == ["file", "waits"]
    assert (page["out"]["all"], page["out"]["noPlay"]) == (False, False)
    assert page["out"]["text"] == "They cannot play until the disk is connected."
    streams = _album({"is_owned": True, "away": None, "away_part": {"why": "away", "streams": True},
                      "tracks": [OUT, HERE]})
    assert streams["rows"] == ["stream", "file"] and streams["out"]["noPlay"] is False


def test_the_album_screen_draws_those_states():
    """The renderer reads the two functions and nothing else for it: the
    stream rows are the phantom rows' (by track uuid, under the album's id),
    the waiting rows have no tap, the buttons carry the refusal, Play and
    Queue go by the album's uuid while anything is out."""
    album = _block("async function renderAlbum(root, albumId) {", "\n  }\n\n  // Release-group screen")
    assert "const out = albumOut(d);" in album and "albumRowKind(t, out)" in album
    stream_row = album[album.index("if (rowKind === 'stream') {"):album.index("if (rowKind === 'waits') {")]
    assert 'class="track-row is-phantom-track" data-track-id=' in stream_row
    assert 'data-album-id="${escapeHtml(albumId)}"' in stream_row
    waits_row = album[album.index("if (rowKind === 'waits') {"):album.index("if (t.location === 'hqplayer') {")]
    assert 'class="track-row is-out-track"' in waits_row and "data-media-file-id" not in waits_row
    assert 'data-action="play-all"${noPlayAttr}' in album and 'data-action="queue-album"${noPlayAttr}' in album
    assert "out && out.noPlay ? ` disabled title=" in album
    assert "byAlbum: !!out," in album
    assert '<span class="am-demo am-yours">Your copy</span>' in album
    handlers = _block("function wireDetailHandlers(screen, ctx = {}) {", "\n  }\n\n  /* Inline")
    assert handlers.count("ctx.byAlbum") == 3            # Play all, and Queue's two branches


def test_a_refused_tap_says_the_same_words_as_a_toast():
    report = _block("async function reportPlaybackResult(resp, body, word) {")
    at = report.index("if (detail.reason === 'not_here') {")
    assert "notHereWords(detail.why).title" in report[at:at + 400]
    assert "key: 'player.not_here'" in report[at:at + 400]


PATH_ROW = "\n".join([
    "const SAFE_TO_PULL = 'safe';",
    "const MUSIC_FOLDER_LINES = { pick: '', launcher: 'launcher', setup: 'setup' };",
    _block("function paintMusicPathRow(row, missing) {"),
    "let _nodeStreams = null;",
    "let _nodeStreamsAsked = false;",
    _block("function paintMusicFolder(missing) {"),
])

PATH_ROW_DRIVER = PATH_ROW + r"""
const classes = new Set();
const sub = { textContent: '', hidden: true };
const row = { classList: { toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)) },
              dataset: { musicFolder: 'setup' }, querySelector: () => sub };
const buttons = [{ disabled: false }, { disabled: false }];
let notice = null, asked = 0, answer;
const notices = { get: () => notice };
const document = { querySelectorAll: sel => {
    if (sel !== '[data-action="scan"], [data-action="scan-prune"]') throw new Error(sel);
    return buttons; },
  querySelector: sel => { if (sel !== '[data-music-path-row]') throw new Error(sel); return row; } };
const fetch = url => { asked += 1; if (url !== '/api/player/reach') throw new Error(url);
  return Promise.resolve({ ok: true, json: () => Promise.resolve(answer) }); };
const seen = () => ({ line: sub.textContent, disabled: buttons.map(b => b.disabled),
                      error: classes.has('kind-error'), asked });
(async () => {
  const steps = [];
  paintMusicFolder(false); steps.push(seen());
  notice = { key: 'library.mount_missing' }; answer = { streams: %(streams)s };
  paintMusicFolder(true); steps.push(seen());
  await new Promise(r => setTimeout(r, 0)); await new Promise(r => setTimeout(r, 0));
  steps.push(seen());
  paintMusicFolder(true); steps.push(seen());             // another snapshot: not asked again
  notice = null;
  paintMusicFolder(false); steps.push(seen());
  console.log(JSON.stringify(steps));
})();
"""

WAITS = "Not connected — your own tracks cannot play or be scanned until it is back."
STREAMS = ("Not connected — your own tracks stream or wait, and nothing can be scanned, "
           "until it is back.")


@pytest.mark.parametrize("streams, said", [(True, STREAMS), (False, WAITS)])
def test_the_scan_buttons_wait_for_the_folder_under_the_rows_sentence(streams, said):
    """F13: "Scan for new" and "Rescan" are disabled while the notice
    stands, and back with its end; the row says whether the albums stream
    meanwhile, asked once."""
    steps = _node(PATH_ROW_DRIVER % {"streams": json.dumps(streams)})
    assert steps[0] == {"line": "setup", "disabled": [False, False], "error": False, "asked": 0}
    assert steps[1] == {"line": WAITS, "disabled": [True, True], "error": True, "asked": 1}
    assert steps[2] == {"line": said, "disabled": [True, True], "error": True, "asked": 1}
    assert steps[3] == steps[2]
    assert steps[4] == {"line": "setup", "disabled": [False, False], "error": False, "asked": 1}
    # the three places that paint the row paint the buttons with it
    assert SOURCE.count("paintMusicFolder(!!notices.get('library.mount_missing'));") == 3


REACH = "\n".join([
    "const _reachScreens = new Set();",
    _block("function followReach(screen, url, text, rerender) {"),
    _block("async function refreshReach(screen) {"),
    "let _reachKey = null;",
    _block("function reachChanged(said) {"),
])

REACH_DRIVER = REACH + r"""
let served = 'payload-1';
const fetched = [];
const fetch = url => { fetched.push(url); return Promise.resolve({ ok: true, text: () => Promise.resolve(served) }); };
const screen = { isConnected: true };
const left = { isConnected: false };                 // a screen the person left
followReach(left, '/api/albums/left', 'x', () => { throw new Error('a screen that left was drawn'); });
let painted = 0;
const paint = () => { painted += 1; followReach(screen, '/api/albums/a', served, paint); };
followReach(screen, '/api/albums/a', served, paint);
const settle = async () => { for (let i = 0; i < 4; i++) await new Promise(r => setTimeout(r, 0)); };
(async () => {
  const steps = [];
  const said = async key => { reachChanged({ key }); await settle(); steps.push([fetched.length, painted]); };
  await said('');                                     // the connect's word: the state the page loaded in
  await said('');                                     // a reconnect, nothing moved
  await said('9f2c');                                 // the folder went; the payload says the same
  await said('9f2c');
  served = 'payload-2'; await said('');               // it is back; the payload differs
  served = 'payload-3'; await said('9f2c');
  console.log(JSON.stringify(steps));
})();
"""


def test_a_new_word_of_what_is_reachable_asks_the_open_screens_payload_again_and_repaints_only_a_change():
    """The page follows the node's own word of what is reachable (`reach`
    on its stream — the recorded answer the payloads read), never the
    notice: a notice shown by a reader that gave up on a slow look flips
    nothing the payload reads, and the look landing later flipped no notice."""
    assert _node(REACH_DRIVER) == [[0, 0], [0, 0], [1, 0], [1, 0], [2, 1], [3, 2]]     # [asked, drawn]
    # the album and the artist screens name their payload
    assert "followReach(screen, url, text, loadAndRender);" in SOURCE
    assert "followReach(screen, url, text, () => renderArtist(root, artistId, selectedMbid));" in SOURCE
    assert "window.addEventListener('sautium:reach', e => reachChanged(e.detail));" in SOURCE
    assert "reachNoticeChanged" not in SOURCE
    player = (REPO_ROOT / "backend" / "static" / "player.js").read_text(encoding="utf-8")
    assert "window.dispatchEvent(new CustomEvent('sautium:reach', { detail: msg.d }));" in player


TILE_DRIVER = WORDS + "\n" + _block("function renderAlbumTile(item) {") + r"""
const escapeHtml = s => String(s == null ? '' : s);
const coverPlaceholderColors = () => ({ bg1: '#000', bg2: '#111' });
const coverUrl = () => '';
const opened = [];
const navigateToEntity = (kind, id) => opened.push([kind, id]);
const document = { createElement: () => {
  const classes = [], on = {};
  return { classList: { add: c => classes.push(c) }, classes, on, innerHTML: '',
           addEventListener: (name, fn) => { on[name] = fn; } }; } };
const draw = item => { const t = renderAlbumTile(item); t.on.click();
  return { marked: t.classes.includes('is-not-here'),
           word: (t.innerHTML.match(/album-tile-out">([^<]*)</) || [])[1] || null,
           held: t.innerHTML.includes('album-tile-held') }; };
console.log(JSON.stringify({
  here: draw({ id: 'a', title: 'Here', artist: 'A', not_here: null }),
  disk: draw({ id: 'b', title: 'Out', artist: 'A', not_here: 'disk' }),
  away: draw({ id: 'c', title: 'Away', artist: 'A', not_here: 'away' }),
  held: draw({ id: 'd', title: 'Held', artist: 'A', location: 'hqplayer' }),
  opened,
}));
"""


def test_a_tile_of_an_album_that_is_out_keeps_its_place_dimmed_with_the_one_word():
    """PI-NODE-2D §3.4, §4.5, Q5: on "New in my collection" and the
    Recommendations rows an album whose disk is out, or whose copy is away,
    stays, marked; the tap opens its page as any tile's does."""
    out = _node(TILE_DRIVER)
    assert out["here"] == {"marked": False, "word": None, "held": False}
    assert out["disk"] == {"marked": True, "word": "not connected", "held": False}
    assert out["away"] == {"marked": True, "word": "away", "held": False}
    assert out["held"] == {"marked": False, "word": None, "held": True}
    assert out["opened"] == [["album", "a"], ["album", "b"], ["album", "c"], ["album", "d"]]


SHELF_DRIVER = "\n".join([
    "const _reachScreens = new Set();",
    _block("function followReach(screen, url, text, rerender) {"),
    _block("async function refreshReach(screen) {"),
    _block("async function homeShelf(sec, url, fill) {"),
]) + r"""
let served = JSON.stringify({ albums: [{ id: 'a', not_here: null }] });
const fetched = [];
const fetch = url => { fetched.push(url);
  return Promise.resolve({ ok: true, text: () => Promise.resolve(served) }); };
const log = [];
const row = { isConnected: true, replaceChildren: () => log.push('emptied') };
const fill = data => { log.push('drawn ' + data.albums.map(a => a.id + ':' + a.not_here).join(','));
  return { disconnect: () => log.push('paging ended') }; };
(async () => {
  await homeShelf({ row }, '/api/home/new-in-library?limit=20', fill);
  const again = [await refreshReach(row)];                       // the same payload: no tile touched
  served = JSON.stringify({ albums: [{ id: 'a', not_here: 'disk' }] });
  again.push(await refreshReach(row));                           // its disk went: drawn again, once
  again.push(await refreshReach(row));
  console.log(JSON.stringify({ log, again, fetched: fetched.length }));
})();
"""


def test_a_home_shelf_is_drawn_again_only_from_a_payload_that_says_otherwise():
    out = _node(SHELF_DRIVER)
    assert out["again"] == [False, True, False] and out["fetched"] == 4
    assert out["log"] == ["drawn a:null", "paging ended", "emptied", "drawn a:disk"]
    home = _block("async function renderHome(root) {")
    assert "homeShelf(newSec, '/api/home/new-in-library?limit=20'," in home
    assert "homeShelf(recSec, '/api/home/recommendations?limit=20'," in home
