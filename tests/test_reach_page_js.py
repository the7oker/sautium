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


LINE = "\n".join([
    _block("const STREAM_REASONS = {", "\n  };\n"),
    _block("function streamReason(code) {"),
    _block("function streamLine(st) {"),
    WORDS,
])


def test_a_queue_row_of_his_copy_that_is_not_here_says_the_album_pages_words():
    """H1: a queued slot of his own copy that nothing here can play says what
    the album page says of it — never words of a stream; a not-owned slot
    where nothing streams says streaming's own; a fill's gap with no `why`
    keeps its line."""
    lines = _node(LINE + """
console.log(JSON.stringify([
  { state: 'failed', reason: 'not_here', why: 'away' },
  { state: 'failed', reason: 'not_here', why: 'disk' },
  { state: 'failed', reason: 'not_here' },
  { state: 'failed', reason: 'off' },
  { state: 'waiting' },
].map(streamLine)));
""")
    assert lines == ["Your copy is away", "On a disk that is not connected", "Its file is not here now",
                     "Doesn't stream here — streaming is off on this node", "Waiting"]


ROWS = WORDS + "\n" + _block("function albumOutRow(t, rowKind, albumId, lines, attrs = '') {") + r"""
const escapeHtml = s => String(s).replace(/</g, '&lt;').replace(/"/g, '&quot;');
const fmtDuration = s => `${s}s`;
const SVG_PLUS = '+';
const d = %(payload)s;
const out = albumOut(d);
const tag = html => html && html.replace(/\s+/g, ' ').trim();
console.log(JSON.stringify(d.tracks.map(t => {
  const html = tag(albumOutRow(t, albumRowKind(t, out), d.id, '<i>lines</i>'));
  if (!html) return null;
  const open = html.slice(0, html.indexOf('>') + 1);
  return { open, tap: /data-(track|media-file)-id=/.test(open), lines: html.includes('<i>lines</i>'),
           title: (html.match(/track-title-line">([^<]*)</) || [])[1],
           dur: (html.match(/track-dur-num">([^<]*)</) || html.match(/track-dur"[^>]*>([^<]*)</) || [])[1],
           add: html.includes('aria-label="Add to queue"'),
           state: html.includes('class="track-buffering"') }; })));
"""


def _rows(payload: dict) -> list:
    return _node(ROWS % {"payload": json.dumps(payload)})


def test_the_album_screen_draws_a_row_that_streams_as_a_not_owned_tracks_and_one_that_waits_with_no_tap():
    """The album screen's rows for a track with no file here now, drawn by
    the page's own function from a payload: where this node streams, the
    not-owned row's markup — tapped by track uuid under the album's id, its
    stream's line and its [+] there; where nothing streams, a row with no
    tap, no [+] and no stream line. A track that is here is the screen's
    own row (null)."""
    track = {"track_id": "t-1", "title": 'One "quoted"', "track_number": 1, "duration": 200}
    payload = {"id": "album-1", "is_owned": True, "away": None,
               "tracks": [{**track, "here": False}, {**track, "here": True, "media_file_id": 7}]}
    streams = _rows({**payload, "away_part": {"why": "disk", "streams": True}})
    assert streams[1] is None
    assert streams[0] == {
        "open": '<div class="track-row is-phantom-track" data-track-id="t-1" data-album-id="album-1">',
        "tap": True, "lines": True, "title": "One &quot;quoted&quot;", "dur": "200s", "add": True,
        "state": True}
    waits = _rows({**payload, "away_part": {"why": "away", "streams": False}})
    assert waits[1] is None
    assert waits[0] == {"open": '<div class="track-row is-out-track">', "tap": False, "lines": True,
                        "title": "One &quot;quoted&quot;", "dur": "200s", "add": False, "state": False}
    assert _rows({**payload, "away_part": None, "tracks": [{**track, "here": True}]}) == [None]


VARIANTS = WORDS + "\n" + _block("function variantLabel(v) {") + r"""
console.log(JSON.stringify(VARIANTS.map(variantLabel)));
"""


def test_the_variant_picker_names_a_copy_that_is_away_or_not_connected():
    """PI-NODE-2E H2: two copies a move left with the same specs and the
    same folder names read apart by their state, in the album page's
    words; a copy that is here reads as before."""
    rip = {"variant_id": 1, "sample_rate": 44100, "bit_depth": 16, "file_format": "FLAC",
           "location": "local", "directory_path": "/music/Mamanet/Frisson"}
    said = _node(VARIANTS.replace("VARIANTS.map", json.dumps([
        {**rip, "away": False, "here": True},
        {**rip, "away": True, "here": False},
        {**rip, "away": False, "here": False},
        rip,                                                   # a payload of before the fields
        {**rip, "location": "hqplayer", "hqp_label": "Box", "away": False, "here": True},
    ]) + ".map"))
    base = "44.1 kHz · 16-bit · FLAC · Mamanet/Frisson"
    assert said == [base, f"{base} · away", f"{base} · not connected", base, f"{base} · at Box"]


SESSION_ROWS = r"""
const escapeHtml = s => String(s).replace(/</g, '&lt;').replace(/"/g, '&quot;');
const fmtDuration = s => `${s}s`;
const SVG_PLUS = '+';
const d = %(payload)s;
const out = sessionOut(d);
console.log(JSON.stringify(d.tracks.map((t, i) => {
  const html = sessionTrackRow(t, i + 1, out, '<i>lines</i>').replace(/\s+/g, ' ').trim();
  return { open: html.slice(0, html.indexOf('>') + 1), lines: html.includes('<i>lines</i>'),
           rank: (html.match(/track-rank">([^<]*)</) || [])[1],
           dur: (html.match(/track-dur-num">([^<]*)</) || html.match(/track-dur"[^>]*>([^<]*)</) || [])[1],
           add: html.includes('aria-label="Add to queue"') }; })));
"""


def _session_rows(payload: dict) -> list:
    fns = "\n".join([WORDS] + [_block(head) for head in (
        "function albumOutRow(t, rowKind, albumId, lines, attrs = '') {", "function sessionOut(d) {",
        "function sessionTrackRow(t, n, out, lines) {")])
    return _node(fns + SESSION_ROWS.replace("%(payload)s", json.dumps(payload)))


def test_a_sessions_rows_draw_his_track_with_no_file_here_as_the_album_page_does():
    """PI-NODE-2E H5: in a Listening history session his own track with no
    file here now is the album page's out row — dimmed, no tap, no [+]
    where nothing streams; by track uuid under the slot's album where the
    node streams — and every other row is drawn as before."""
    owned = {"position": 1, "track_id": "t-1", "media_file_id": 7, "album_id": "al-1", "title": "One",
             "duration_seconds": 200, "is_phantom": False}
    phantom = {"position": 3, "track_id": "t-9", "media_file_id": None, "album_id": "al-2", "title": "Nine",
               "duration_seconds": 180, "is_phantom": True, "here": None, "why": None}
    here = {**owned, "here": True, "why": None}
    out = {**owned, "position": 2, "here": False, "why": "disk"}
    waits = _session_rows({"streams": False, "tracks": [here, out, phantom]})
    assert waits == [
        {"open": '<button class="track-row" data-media-file-id="7" data-position="1" type="button">',
         "lines": True, "rank": "1", "dur": "200s", "add": True},
        # every row carries its slot, an out row too: a tap chooses where Play starts (PLAYBACK-UI-1 B-158)
        {"open": '<div class="track-row is-out-track" data-position="2">', "lines": True, "rank": "2",
         "dur": "200s", "add": False},
        {"open": '<button class="track-row is-phantom-track" data-track-id="t-9" data-album-id="al-2" '
                 'data-position="3" type="button">', "lines": True, "rank": "3", "dur": "180s", "add": True},
    ]
    streams = _session_rows({"streams": True, "tracks": [here, out]})
    assert streams[0] == waits[0]
    assert streams[1] == {
        "open": '<div class="track-row is-phantom-track" data-track-id="t-1" data-album-id="al-1" data-position="2">',
        "lines": True, "rank": "2", "dur": "200s", "add": True}
    # every row here, or a payload of before the fields: the session as it was
    assert _session_rows({"tracks": [{**owned}, phantom]})[0] == waits[0]
    assert _session_rows({"streams": True, "tracks": [here]}) == [waits[0]]
    # the screen draws its rows by the function
    assert "trackParts.push(sessionTrackRow(t, n, out," in SOURCE


TOAST = WORDS + "\n" + _block("async function reportPlaybackResult(resp, body, word) {") + r"""
const toasts = [];
const notices = { toast: t => toasts.push(t) };
const escapeProfileHtml = s => String(s).replace(/</g, '&lt;');
const answerSays = () => true;
const TRANSPORT_WORDS = {};
const window = {};
const refused = detail => ({ ok: false, status: 409, headers: { get: () => null },
                             json: () => Promise.resolve({ detail }) });
(async () => {
  const said = [];
  for (const detail of %(details)s) {
    said.push(await reportPlaybackResult(refused(detail), { detail }, 'play'));
  }
  // a caller that has not read the body: the function reads it
  said.push(await reportPlaybackResult(refused(%(details)s[0]), undefined, 'play'));
  console.log(JSON.stringify({ said, toasts }));
})();
"""


def test_a_refused_tap_says_the_albums_words_as_a_toast():
    """A route's 409 `not_here`, as the page's own function reports it: a
    warning toast under one key, titled as the album titles that state,
    with the node's sentence — or, where it sent none, the album's own."""
    details = [
        {"reason": "not_here", "why": "disk",
         "message": "Night <Walks> cannot play until its disk is connected."},
        {"reason": "not_here", "why": "away"},
    ]
    out = _node(TOAST % {"details": json.dumps(details)})
    assert out["said"] == [False, False, False]
    assert out["toasts"] == [
        {"kind": "warning", "key": "player.not_here", "title": "On a disk that is not connected",
         "text": "Night &lt;Walks> cannot play until its disk is connected."},
        {"kind": "warning", "key": "player.not_here", "title": "Your copy is away",
         "text": "It cannot play until its files are back."},
        {"kind": "warning", "key": "player.not_here", "title": "On a disk that is not connected",
         "text": "Night &lt;Walks> cannot play until its disk is connected."},
    ]


PATH_ROW = "\n".join([
    "const SAFE_TO_PULL = 'safe';",
    "const MUSIC_FOLDER_LINES = { pick: '', launcher: 'launcher', setup: 'setup' };",
    _block("function paintMusicPathRow(row, missing) {"),
    "let _nodeStreams = null;",
    "let _nodeStreamsAsked = false;",
    "let _prunePlanHeld = null;",
    _block("function paintMusicFolder(missing) {"),
])

PATH_ROW_DRIVER = PATH_ROW + r"""
const classes = new Set();
const sub = { textContent: '', hidden: true };
const row = { classList: { toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)) },
              dataset: { musicFolder: 'setup' }, querySelector: () => sub };
const buttons = [{ disabled: false }, { disabled: false }, { disabled: false }];
let notice = null, asked = 0, answer;
const notices = { get: () => notice };
const document = { querySelectorAll: sel => {
    if (sel !== '[data-action="scan"], [data-action="scan-prune"], [data-action="remove-away"]') throw new Error(sel);
    return buttons; },
  querySelector: sel => {
    if (sel === '[data-away-why]') return null;          // the Away row is not on this screen's stub
    if (sel !== '[data-music-path-row]') throw new Error(sel); return row; } };
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
    """F13: "Scan for new", "Rescan" and the Away row's Remove (2E F23) are
    disabled while the notice stands, and back with its end; the row says
    whether the albums stream meanwhile, asked once."""
    steps = _node(PATH_ROW_DRIVER % {"streams": json.dumps(streams)})
    assert steps[0] == {"line": "setup", "disabled": [False] * 3, "error": False, "asked": 0}
    assert steps[1] == {"line": WAITS, "disabled": [True] * 3, "error": True, "asked": 1}
    assert steps[2] == {"line": said, "disabled": [True] * 3, "error": True, "asked": 1}
    assert steps[3] == steps[2]
    assert steps[4] == {"line": "setup", "disabled": [False] * 3, "error": False, "asked": 1}
    # the three places that paint the row paint the buttons with it
    assert SOURCE.count("paintMusicFolder(!!notices.get('library.mount_missing'));") == 3


REACH = "\n".join([
    "const _reachScreens = new Set();",
    _block("function followReach(screen, ask, text, rerender) {"),
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
  return { classList: { add: c => classes.push(c) }, classes, on, innerHTML: '', dataset: {},
           addEventListener: (name, fn) => { on[name] = fn; } }; } };
const draw = item => { const t = renderAlbumTile(item); t.on.click();
  if (t.dataset.albumId !== item.id) throw new Error('a tile without its album id');
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


SHELF_DRIVER = WORDS + "\n" + "\n".join([
    "const _reachScreens = new Set();",
    _block("function followReach(screen, ask, text, rerender) {"),
    _block("async function refreshReach(screen) {"),
    _block("function markAlbumTile(tile, why) {"),
    _block("async function homeShelf(sec, url, fill) {"),
]) + r"""
// A row of album tiles as the page holds it: each tile its id, its class
// and its mark's word — set in place, never made again.
const made = [];
const tile = (id, word = null) => {
  const t = { dataset: { albumId: id }, marked: word !== null, mark: null };
  made.push(id);
  t.classList = { toggle: (c, on) => { if (c !== 'is-not-here') throw new Error(c); t.marked = on; } };
  const put = el => { t.mark = el; el.remove = () => { t.mark = null; }; };
  const cover = { querySelector: sel => { if (sel !== '.album-tile-out') throw new Error(sel); return t.mark; },
                  appendChild: put };
  if (word !== null) put({ className: 'album-tile-feat album-tile-out', textContent: word });
  t.querySelector = sel => { if (sel !== '.album-cover') throw new Error(sel); return cover; };
  return t;
};
const shown = () => tiles.map(t => [t.dataset.albumId, t.marked, t.mark && t.mark.textContent]);
const document = { createElement: tag => { if (tag !== 'span') throw new Error(tag); return {}; } };
const tiles = [];
const row = { isConnected: true,
              querySelectorAll: sel => { if (sel !== '.album-tile[data-album-id]') throw new Error(sel); return tiles; },
              replaceChildren: () => { throw new Error('the row was emptied'); } };
let away = {};
const asked = [];
const fetch = (url, opts) => {
  if (!opts) return Promise.resolve({ ok: true, json: () => Promise.resolve({ albums: [{ id: 'a' }, { id: 'b' }] }) });
  if (url !== '/api/home/albums-away' || opts.method !== 'POST') throw new Error(url);
  const ids = JSON.parse(opts.body).ids;
  asked.push(ids);
  const not_here = {};
  ids.forEach(id => { if (away[id]) not_here[id] = away[id]; });
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ not_here }) });
};
(async () => {
  const steps = [];
  await homeShelf({ row }, '/api/home/new-in-library?limit=20',
                  data => data.albums.forEach(a => tiles.push(tile(a.id))));
  const step = async () => { const drew = await refreshReach(row); steps.push({ drew, tiles: shown() }); };
  tiles.push(tile('c'));                        // a page scrolled in
  away = { c: 'disk', a: 'away' };
  // a page that lands while the answer is on its way keeps its own marks
  const landing = refreshReach(row);
  tiles.push(tile('d', 'not connected'));
  steps.push({ drew: await landing, tiles: shown() });
  await step();                                 // asked again, the new tile among them: 'd' is here
  await step();                                 // the same answer: nothing set
  away = {};
  await step();                                 // the disk is back
  console.log(JSON.stringify({ steps, asked, made }));
})();
"""


def test_a_home_shelf_sets_the_marks_of_the_tiles_it_shows_in_place_whatever_page_brought_them():
    """What is reachable changed: the shelf asks about the albums of the
    tiles on the page — the first page's and every page scrolled in — and
    sets each one's mark in place. No tile is made again and the row is
    never emptied, so nothing flickers and paging is untouched; a page that
    landed while the answer was on its way keeps the marks it came with."""
    out = _node(SHELF_DRIVER)
    assert out["made"] == ["a", "b", "c", "d"]                      # each tile made once
    assert out["asked"] == [["a", "b", "c"], ["a", "b", "c", "d"], ["a", "b", "c", "d"],
                            ["a", "b", "c", "d"]]
    first, second, third, last = out["steps"]
    assert first == {"drew": True, "tiles": [["a", True, "away"], ["b", False, None],
                                             ["c", True, "not connected"], ["d", True, "not connected"]]}
    assert second == {"drew": True, "tiles": [["a", True, "away"], ["b", False, None],
                                              ["c", True, "not connected"], ["d", False, None]]}
    assert third == {"drew": False, "tiles": second["tiles"]}
    assert last == {"drew": True, "tiles": [[i, False, None] for i in "abcd"]}
    home = _block("async function renderHome(root) {")
    assert "homeShelf(newSec, '/api/home/new-in-library?limit=20'," in home
    assert "homeShelf(recSec, '/api/home/recommendations?limit=20'," in home


LEFT_DRIVER = REACH + r"""
const fetch = () => { throw new Error('a screen that left was asked about'); };
(async () => {
  let screen = { isConnected: false };              // a screen the person left
  followReach(screen, '/api/albums/a', 'x', () => {});
  const probe = new WeakRef(screen);
  screen = null;
  await new Promise(r => setTimeout(r, 0));
  global.gc();
  await new Promise(r => setTimeout(r, 0));
  const held = probe.deref() !== undefined;
  reachChanged({ key: '' });
  reachChanged({ key: '9f2c' });                    // the next word drops what was collected
  console.log(JSON.stringify({ held, followers: _reachScreens.size }));
})();
"""


def test_a_screen_the_person_left_is_let_go_whatever_the_node_says_later():
    """The followers are held weakly: a screen that left the page is the
    collector's at once — not kept, with its payload and its closures, until
    the folder's answer next moves."""
    out = subprocess.run([NODE, "--expose-gc", "-e", "'use strict';\n" + LEFT_DRIVER],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"held": False, "followers": 0}
