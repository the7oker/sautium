"""backend/static/app-shell.js's Library screen under Node: when a wake of
the Library stream asks for the screen to be drawn again. A scan the screen
did not start — the folder's choice, the folder watcher — must show, and an
empty library's first scan must end in the library, not in "Run first scan"
(PI-NODE-2B item 8, U1). app-shell.js's own function, run as written; skipped
where Node is not."""

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


DRIVER = r"""
'use strict';
%(drawn)s
%(stale)s
const SCAN = '[data-cancel-scan]', ENRICH = '[data-cancel-enrich]', EMPTY = '.empty-library';
const lib = (tracks, analysis = true, more = {}) => Object.assign(
  { total_tracks: tracks, analysis_available: analysis, last_scan_at: 'then' }, more);
// The screen as drawn: which marks it shows, and the numbers it was drawn with
const drawn = (shown, was) => ({ querySelector: sel =>
  sel === '[data-library-drawn]' ? { getAttribute: () => _libraryDrawn(was) }
  : shown.includes(sel) ? {} : null });
const cases = %(cases)s;
console.log(JSON.stringify(cases.map(c => {
  const now = lib(c.tracks, c.analysis !== false, c.now || {});
  const was = c.was === undefined ? now : lib(c.was, true, {});
  return _libraryStructureStale(drawn(c.shown.map(s => ({ SCAN, ENRICH, EMPTY })[s]), was), now, !!c.scan, !!c.enrich);
})));
"""


def _run(cases):
    script = DRIVER % {"drawn": _function("_libraryDrawn(lib)"),
                       "stale": _function("_libraryStructureStale(root, lib, scanRunning, enrichRunning)"),
                       "cases": json.dumps(cases)}
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


CASES = [
    # (what the screen shows, the library's tracks, scan running, analysis running, stale?)
    (["EMPTY"], 0, False, False, False),            # an empty library at rest
    (["EMPTY"], 0, True, False, True),              # the folder was chosen: its scan runs unseen
    (["EMPTY", "SCAN"], 0, True, False, False),     # the first scan, shown, nothing indexed yet
    (["EMPTY", "SCAN"], 40, True, False, True),     # its first tracks: no longer an empty library
    (["EMPTY"], 93, False, False, True),            # a scan too short to be seen running
    (["SCAN"], 93, False, False, True),             # a scan finished
    ([], 93, True, False, True),                    # the folder watcher started one
    ([], 93, False, False, False),                  # a library at rest
    ([], 0, False, False, True),                    # the library was emptied
    (["ENRICH"], 93, False, False, True),           # an analysis finished
    ([], 93, False, True, True),                    # an analysis started elsewhere
    (["ENRICH"], 93, False, True, False),
]


def test_the_library_screen_is_drawn_again_only_where_its_structure_is_stale():
    cases = [{"shown": shown, "tracks": tracks, "scan": scan, "enrich": enrich,
              # the screen was drawn with whatever made it show what it shows
              "was": 0 if "EMPTY" in shown else (tracks or 93)}
             for shown, tracks, scan, enrich, _ in CASES]
    assert _run(cases) == [stale for *_, stale in CASES]


def test_an_analysis_no_row_can_show_never_asks_for_a_redraw():
    """A node with no analysis of its own, or an empty library, draws no
    analysis row: a redraw could not show the run, and would be asked for at
    every wake."""
    assert _run([{"shown": [], "tracks": 93, "enrich": True, "analysis": False},
                 {"shown": ["EMPTY"], "tracks": 0, "enrich": True}]) == [False, False]


def test_a_redraw_follows_structure_and_never_the_numbers():
    """What moves the screen's structure with no worker running: the folder,
    an Away block to add or take away, a first scan's time. The counts, the
    size and a later scan's time are written in place — they also move under
    an HQPlayer library's import and a background pass, whose wakes must not
    redraw the screen (review round 3)."""
    assert _run([
        {"shown": [], "tracks": 93, "now": {"away": {"albums": 200, "copies": 200}}, "was": 93},   # the Away block comes
        {"shown": [], "tracks": 93, "now": {"music_path": "/media/sautium/X/Music"}, "was": 93},
        {"shown": [], "tracks": 93, "now": {"last_scan_at": ""}, "was": 93},                        # no scan on record
        {"shown": [], "tracks": 8187, "was": 10187},                              # only the counts moved
        {"shown": [], "tracks": 93, "now": {"last_scan_at": "now"}, "was": 93},   # a later scan's time
        {"shown": [], "tracks": 93, "now": {"total_size_bytes": "7"}, "was": 93},
        {"shown": [], "tracks": 93, "was": 93},
        {"shown": ["SCAN"], "tracks": 2093, "scan": True, "now": {"away": {"albums": 1, "copies": 1}}, "was": 93},
    ]) == [True, True, True, False, False, False, False, False]


NUMBERS = r"""
'use strict';
const fmtNum = n => String(n), fmtBytes = n => n + ' B', fmtRelative = t => 'rel:' + t, fmtAbs = t => 'abs:' + t;
const escapeProfileHtml = s => String(s);
const _count = (n, one, many) => `${n || 0} ${n === 1 ? one : many}`;
const _statCell = (label, value) => `[${label}=${value}]`;
%(cells)s
%(numbers)s
const made = sel => ({ textContent: 'old', innerHTML: 'old', dataset: {}, sel });
const els = {};
const root = { querySelector: sel => (els[sel] = els[sel] || made(sel)) };
const lib = { total_tracks: 8187, total_artists: 58, total_albums: 223, total_genres: 13, total_size_bytes: 4096,
              last_scan_at: 'T1', away: { albums: 200, copies: 200 } };
_refreshLibraryNumbers(root, lib, false);
const first = { stats: els['[data-library-stats]'].innerHTML, size: els['[data-library-size]'].textContent,
                scan: els['[data-library-last-scan]'].textContent, at: els['[data-library-last-scan-at]'].textContent,
                away: els['[data-library-away]'].textContent };
// the same numbers again: nothing is written
els['[data-library-stats]'].innerHTML = 'untouched';
_refreshLibraryNumbers(root, lib, false);
// a scan runs: its "running…" stays
els['[data-library-last-scan]'].textContent = 'running…';
_refreshLibraryNumbers(root, Object.assign({}, lib, { last_scan_at: 'T2' }), true);
console.log(JSON.stringify({ first, again: els['[data-library-stats]'].innerHTML,
                             running: els['[data-library-last-scan]'].textContent }));
"""


def test_the_numbers_are_written_where_they_stand_and_only_when_they_moved():
    script = NUMBERS % {"cells": _function("_libraryStatCells(lib)"),
                        "numbers": _function("_refreshLibraryNumbers(root, lib, scanRunning)")}
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {
        "first": {"stats": "[Tracks=8187][Artists=58][Albums=223][Genres=13]", "size": "4096 B",
                  "scan": "rel:T1", "at": "abs:T1", "away": "200 albums"},
        "again": "untouched", "running": "running…"}


def test_an_empty_librarys_first_scan_is_shown_with_its_cancel_and_no_run_first_scan():
    start = SOURCE.index("const emptyState = ")
    block = SOURCE[start:SOURCE.index("const actions = ''", start)]
    assert "</div>`) + (scanRunning ? scanActions : '');" in block
    assert "${scanRunning ? '' : `<button class=\"empty-cta\" data-action=\"scan\">" in block


# -- plugged disks in the folder sheet and under the folder's row (PI-NODE-2C §2.3, §9) ---------

DISKS = r"""
'use strict';
const escapeHtml = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
const fmtBytes = n => n + ' B', fmtPathForDisplay = p => p;
const MUSIC_FOLDER_LINES = { pick: '', setup: 'Named where this node was set up.' };
%(safe)s
%(copy)s
%(row)s
%(paint)s
const sub = { textContent: '', hidden: true };
const pathRow = pullable => ({ dataset: { musicFolder: 'pick', musicPullable: pullable ? '1' : '' },
  classList: { toggle() {} }, querySelector: () => sub });
const painted = (pullable, missing) => { paintMusicPathRow(pathRow(pullable), missing);
                                         return [sub.textContent, sub.hidden]; };
console.log(JSON.stringify({
  copies: %(volumes)s.map(unreadDiskCopy),
  rows: %(places)s.map(_folderPlaceRow),
  path: [painted(true, false), painted(false, false), painted(true, true)],
  safe: SAFE_TO_PULL,
}));
"""

SAFE = ("Sautium only reads this disk and writes nothing to it. You can unplug it at any time: "
        "its albums are away until it is back.")
READS = "Sautium reads FAT, exFAT, NTFS and ext4 disks."


def _disks(volumes=(), places=()):
    start = SOURCE.index("\n  const SAFE_TO_PULL = ") + 1
    script = DISKS % {"safe": SOURCE[start:SOURCE.index(";\n", start) + 1],
                      "copy": _function("unreadDiskCopy(d)"), "row": _function("_folderPlaceRow(p)"),
                      "paint": _function("paintMusicPathRow(row, missing)"),
                      "volumes": json.dumps(list(volumes)), "places": json.dumps(list(places))}
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_an_unread_disk_is_said_by_what_it_is_and_its_label_is_a_strangers_text():
    copies = _disks(volumes=[
        {"label": "Elements <img src=x onerror=alert(1)>", "format": "exFAT", "mounts": True},
        {"label": "Win", "format": "NTFS", "mounts": True, "driver": "ntfs3"},
        {"label": "Mac", "format": "APFS", "mounts": False},
        {"label": None, "format": None, "mounts": False},
        {"label": "Win", "format": "NTFS", "mounts": True, "driver": "ntfs-3g"},
    ])["copies"]
    assert {c["title"] for c in copies} == {"A disk could not be read"}
    assert copies[0]["text"] == ("Elements &lt;img src=x onerror=alert(1)&gt; is formatted as exFAT, which this "
                                 "machine could not open. It is not in your library.")
    assert copies[0]["row"] == "exFAT, could not be read"
    # NTFS the kernel's driver refused: the machine has no ntfs-3g, and the setup shows how to add it
    assert copies[1]["text"] == ("Win is an NTFS disk this machine could not open. On the machine, run "
                                 "./sautium setup: it shows how to add NTFS support. Then plug the disk in again.")
    # ...and one ntfs-3g itself refused is said as any failed mount
    assert copies[4]["text"] == ("Win is formatted as NTFS, which this machine could not open. "
                                 "It is not in your library.")
    assert copies[2]["text"] == f"Mac is formatted as APFS. {READS}"
    assert (copies[2]["row"], copies[2]["sub"]) == ("APFS", READS)
    assert copies[3]["text"] == f"This disk is in a format this machine does not know. {READS}"


def test_the_folder_sheets_rows_a_disk_to_open_with_its_line_and_a_volume_only_to_read():
    out = _disks(places=[
        {"path": "/media/sautium/4A21", "name": "4A21", "kind": "disk", "label": "Elements <b>",
         "size_bytes": 512, "read_only": True},
        {"path": "/srv/music", "name": "/srv/music", "kind": "disk", "label": None, "size_bytes": 9,
         "read_only": False},
        {"path": "/mnt/sautium-5678", "name": "/mnt/sautium-5678", "kind": "folder", "label": None,
         "size_bytes": None, "empty": True},
        {"path": "/home/pi/Music", "name": "/home/pi/Music", "kind": "folder", "label": None,
         "size_bytes": None, "empty": False},
        {"path": None, "name": "sda1", "kind": "unread", "label": "<i>Win</i>", "size_bytes": None,
         "format": "NTFS", "mounts": True},
    ])
    plugged, written, gone, folder, unread = out["rows"]
    assert out["safe"] == SAFE
    assert 'data-path="/media/sautium/4A21"' in plugged and "Elements &lt;b&gt;" in plugged
    assert "— 512 B" in plugged and SAFE in plugged
    # a disk the machine mounted to write goes by its path, and is not said to be safe to pull
    assert ">/srv/music " in written and SAFE not in written
    assert "Empty — its disk is not mounted, or the folder was removed." in gone
    assert "Empty" not in folder and SAFE not in folder and 'data-path="/home/pi/Music"' in folder
    # no path, no button: a row to read
    assert "data-path" not in unread and "<button" not in unread and "data-unread" in unread
    assert "&lt;i&gt;Win&lt;/i&gt;" in unread and "— NTFS, could not be read" in unread
    assert "<i>" not in unread


def test_the_chosen_folders_row_says_it_can_be_pulled_while_it_is_on_a_plugged_disk():
    path = _disks()["path"]
    assert path == [[SAFE, False], ["", True],
                    ["Empty or not mounted — your own tracks cannot play or be scanned until it is back.", False]]
