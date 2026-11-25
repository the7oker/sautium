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
%(away)s
%(numbers)s
const made = sel => ({ textContent: 'old', innerHTML: 'old', dataset: {}, sel });
const els = {};
const root = { querySelector: sel => (els[sel] = els[sel] || made(sel)) };
const lib = { total_tracks: 8187, total_artists: 58, total_albums: 223, total_genres: 13, total_size_bytes: 4096,
              last_scan_at: 'T1', away: { albums: 180, copies: 200 } };
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
    script = NUMBERS % {"cells": _function("_libraryStatCells(lib)"), "away": _function("_awayCount(away)"),
                        "numbers": _function("_refreshLibraryNumbers(root, lib, scanRunning)")}
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {
        "first": {"stats": "[Tracks=8187][Artists=58][Albums=223][Genres=13]", "size": "4096 B",
                  "scan": "rel:T1", "at": "abs:T1",
                  "away": "200 copies away — 180 albums have no other copy"},
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
%(crumb)s
%(paint)s
const sub = { textContent: '', hidden: true };
const pathRow = pullable => ({ dataset: { musicFolder: 'pick', musicPullable: pullable ? '1' : '' },
  classList: { toggle() {} }, querySelector: () => sub });
const painted = (pullable, missing) => { paintMusicPathRow(pathRow(pullable), missing);
                                         return [sub.textContent, sub.hidden]; };
console.log(JSON.stringify({
  copies: %(volumes)s.map(unreadDiskCopy),
  rows: %(places)s.map(_folderPlaceRow),
  crumbs: %(crumbs)s.map(c => _folderCrumb(c[0], c[1])),
  path: [painted(true, false), painted(false, false), painted(true, true)],
  safe: SAFE_TO_PULL,
}));
"""

SAFE = ("Sautium only reads this disk and writes nothing to it. You can unplug it at any time: "
        "its albums are away until it is back.")
READS = "Sautium reads FAT, exFAT, NTFS and ext4 disks."


def _disks(volumes=(), places=(), crumbs=()):
    start = SOURCE.index("\n  const SAFE_TO_PULL = ") + 1
    script = DISKS % {"safe": SOURCE[start:SOURCE.index(";\n", start) + 1],
                      "copy": _function("unreadDiskCopy(d)"), "row": _function("_folderPlaceRow(p)"),
                      "crumb": _function("_folderCrumb(at, disk)"),
                      "paint": _function("paintMusicPathRow(row, missing)"),
                      "volumes": json.dumps(list(volumes)), "places": json.dumps(list(places)),
                      "crumbs": json.dumps(list(crumbs))}
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


def test_a_disk_no_one_format_is_found_on_is_said_by_which_of_the_two_it_is():
    """The automount's two words (PI-NODE-2D §5, §9.4): the marks of two
    formats, or none found. No label came with either; the toast and the
    sheet's row say the same sentence."""
    told = "Sautium cannot tell what is on it."
    two = ("It carries the marks of two formats (a quick format over an older one); formatting it again in full "
           "on your computer clears them.")
    none = "It may be empty, or in a format this machine does not know."
    volume = {"label": None, "format": None, "mounts": False, "driver": None}
    out = _disks(volumes=[dict(volume, unclear="ambivalent"), dict(volume, unclear="unknown")],
                 places=[dict(volume, path=None, name="sda1", kind="unread", size_bytes=None, unclear="ambivalent"),
                         dict(volume, path=None, name="sdb", kind="unread", size_bytes=None, unclear="unknown")])
    assert [c["text"] for c in out["copies"]] == [f"A disk — {told} {two}", f"A disk — {told} {none}"]
    assert {c["title"] for c in out["copies"]} == {"A disk could not be read"}
    for row, sub in zip(out["rows"], (two, none)):
        assert "A disk <span" in row and f"— {told}</span>" in row and sub in row
        assert "data-path" not in row and "<button" not in row and "unknown format" not in row
        assert "sda1" not in row and "sdb" not in row               # the kernel's name is nothing to a person


def test_the_folder_sheets_rows_a_disk_to_open_with_its_line_and_a_volume_only_to_read():
    out = _disks(places=[
        {"path": "/media/sautium/4A21", "name": "4A21", "kind": "disk", "label": "Elements <b>",
         "size_bytes": 512, "read_only": True},
        {"path": "/srv/music", "name": "/srv/music", "kind": "disk", "label": None, "size_bytes": 9,
         "read_only": False},
        {"path": "/mnt/sautium-5678", "name": "/mnt/sautium-5678", "kind": "folder", "label": None,
         "size_bytes": None, "empty": True, "held": True},
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
    assert ">/srv/music <" in written and "— 9 B" in written and SAFE not in written
    assert "Empty — its disk is not mounted, or the folder was removed." in gone
    assert "Empty" not in folder and SAFE not in folder and 'data-path="/home/pi/Music"' in folder
    # no path, no button: a row to read
    assert "data-path" not in unread and "<button" not in unread and "data-unread" in unread
    assert "&lt;i&gt;Win&lt;/i&gt;" in unread and "— NTFS, could not be read" in unread
    assert "<i>" not in unread


def test_a_disk_with_no_label_is_named_by_what_it_is_and_its_uuid_is_nowhere():
    """PI-NODE-2C F9: the mount point's name is the disk's UUID, which is
    nothing to a person — in the list and in the line that says where the
    sheet stands."""
    disk = {"path": "/media/sautium/01DD7A027355D0B0", "name": "01DD7A027355D0B0", "kind": "disk", "label": None,
            "size_bytes": 15, "read_only": True}
    out = _disks(places=[disk, dict(disk, size_bytes=None), dict(disk, label="Elements")],
                 crumbs=[["/media/sautium/01DD7A027355D0B0/Music/Jazz", dict(disk)],
                         ["/media/sautium/01DD7A027355D0B0", dict(disk)],
                         ["/media/sautium/4A21-0000/Music", {"path": "/media/sautium/4A21-0000", "name": "4A21-0000",
                                                             "label": "Elements <b>"}],
                         ["/mnt/sautium-5678/Music", {"path": "/mnt/sautium-5678", "name": None, "label": "Old line"}],
                         ["/srv/music/Jazz", {"path": "/srv/music", "name": None, "label": None}],
                         ["/home/pi/Music/Jazz", None]])
    unnamed, sizeless, named = out["rows"]
    assert ">USB disk · 15 B<" in unnamed and ">USB disk<" in sizeless
    assert ">Elements <span" in named and "— 15 B" in named
    for row in (unnamed, sizeless):
        assert "01DD7A027355D0B0" not in row.replace('data-path="/media/sautium/01DD7A027355D0B0"', "")
    assert out["crumbs"] == ["USB disk › Music › Jazz", "USB disk", "Elements <b> › Music", "Old line › Music",
                             "/srv/music/Jazz", "/home/pi/Music/Jazz"]


def test_an_empty_folder_the_library_never_read_is_new_not_lost():
    """PI-NODE-2C F2: a folder named a moment ago with nothing in it was said
    as a root whose disk is not mounted."""
    folder = {"path": "/home/pi/New", "name": "/home/pi/New", "kind": "folder", "label": None, "size_bytes": None}
    new, lost, full = _disks(places=[dict(folder, empty=True, held=False), dict(folder, empty=True, held=True),
                                     dict(folder, empty=False, held=None)])["rows"]
    assert "Empty — nothing is in it yet." in new and "not mounted" not in new
    assert "Empty — its disk is not mounted, or the folder was removed." in lost
    assert "Empty" not in full


def test_the_sheets_foot_promises_only_what_the_sheet_shows():
    """It said "can read every disk plugged into this machine" under a row
    for a disk it could not read (PI-NODE-2C F13)."""
    assert "Sautium reads the disks listed here and writes to none of them." in SOURCE
    assert "can read every disk plugged into this machine" not in SOURCE


def test_the_chosen_folders_row_says_it_can_be_pulled_while_it_is_on_a_plugged_disk():
    path = _disks()["path"]
    assert path == [[SAFE, False], ["", True],
                    ["Not connected — your own tracks cannot play or be scanned until it is back.", False]]


# -- the Away row while the music folder does not answer (PI-NODE-2E F23, H3) ----------------------

def _block(start: str, end: str) -> str:
    at = SOURCE.index(start)
    return SOURCE[at:SOURCE.index(end, at) + len(end)]


MOUNT_MISSING = _block("  NOTICE_COPY['library.mount_missing'] = n => ({", "\n  });")

FOLDER = r"""
'use strict';
const escapeHtml = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;');
const fmtPathForDisplay = p => p;
const NOTICE_COPY = {};
%(copy)s
let _nodeStreams = null, _nodeStreamsAsked = false, _prunePlanHeld = null;
let shown = null;
const notices = { get: k => (k === 'library.mount_missing' ? shown : null) };
const buttons = ['scan', 'scan-prune', 'remove-away'].map(action => ({ action, disabled: false }));
const why = { innerHTML: '', hidden: true, dataset: {} };
const document = {
  querySelector: sel => (sel === '[data-away-why]' ? why : null),
  querySelectorAll: sel => buttons.filter(b => sel.includes(`[data-action="${b.action}"]`)),
};
%(paint)s
const seen = () => ({ disabled: buttons.map(b => b.disabled), why: why.innerHTML, hidden: why.hidden });
shown = { data: { path: '/media/sautium/X/<Music>' } };
paintMusicFolder(true);
const out = seen();
shown = null;
paintMusicFolder(false);
console.log(JSON.stringify({ out, back: seen(),
                             copy: NOTICE_COPY['library.mount_missing']({ data: { path: '/media/sautium/X/<Music>' } }).text }));
"""


def test_the_away_rows_remove_waits_for_the_folder_with_the_notices_sentence():
    script = FOLDER % {"copy": MOUNT_MISSING, "paint": _function("paintMusicFolder(missing)")}
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got["out"] == {"disabled": [True, True, True], "why": got["copy"], "hidden": False}
    assert "/media/sautium/X/&lt;Music>" in got["copy"]           # the notice's own words, escaped
    assert got["back"] == {"disabled": [False, False, False], "why": "", "hidden": True}


def test_the_away_rows_refusal_is_a_toast_as_the_yes_is():
    handler = _block("onAction('[data-action=\"remove-away\"]'", "\n    }));")
    assert "notices.toast({ kind: 'error', key: 'library.prune'" in handler
    assert "notifyDialog" not in handler
    assert 'data-away-why hidden' in SOURCE


ASK = r"""
'use strict';
const escapeProfileHtml = s => String(s);
const _prunePlansAsked = new Set();
let _prunePlanOpen = false, _prunePlanHeld = null;
const _prunePlanTitle = p => 't', _prunePlanMessage = p => 'm';
const parseHash = () => 'home', render = () => {};
const toasts = [];
let missing = null;
const notices = { toast: t => toasts.push(t), get: key => (key === 'library.mount_missing' ? missing : null) };
let confirms = 0;
const window = { confirmDestructive: async () => { confirms += 1; return true; } };
%(sent)s
const atSend = [];
let answer = { ok: true, status: 200 };
const fetch = async url => { atSend.push([..._pruneYesSent.keys()]);
                             return Object.assign({ json: async () => ({ detail: 'refused here' }) }, answer); };
%(ask)s
%(say)s
(async () => {
  await _askPrunePlan({ id: 'p1', kind: 'away' });
  // the run ends refused; the snapshot is read twice (two wakes), and once by a tab that never asked
  _sayPruneRefused({ running: false, result: { plan_id: 'p1', prune: { pruned: 0, refused: 'Not connected.' } } });
  _sayPruneRefused({ running: false, result: { plan_id: 'p1', prune: { pruned: 0, refused: 'Not connected.' } } });
  _sayPruneRefused({ running: false, result: { plan_id: 'p9', prune: { pruned: 0, refused: 'Not connected.' } } });
  // a yes the node takes and runs to the end: nothing to say, nothing kept
  await _askPrunePlan({ id: 'p2', kind: 'rescan' });
  _sayPruneRefused({ running: false, result: { plan_id: 'p2', prune: { pruned: 3 } } });
  // a yes refused at the route: its own toast, nothing kept for the run
  answer = { ok: false, status: 409 };
  await _askPrunePlan({ id: 'p3', kind: 'rescan' });
  // the folder out: a waiting question is not asked (its yes would only be refused) — asked once it is back
  answer = { ok: true, status: 200 };
  missing = { key: 'library.mount_missing' };
  const before = confirms;
  await _askPrunePlan({ id: 'p4', kind: 'away' });
  await _askPrunePlan({ id: 'p4', kind: 'away' });
  const whileOut = confirms - before;
  missing = null;
  await _askPrunePlan(_prunePlanHeld);                      // what paintMusicFolder(false) asks at the notice's end
  console.log(JSON.stringify({ atSend, toasts, kept: [..._pruneYesSent.keys()], whileOut,
                               afterBack: confirms - before }));
})();
"""


def test_a_removal_refused_when_it_looked_is_said_once_to_the_tab_that_said_yes():
    start = SOURCE.index("\n  async function _askPrunePlan(plan) {\n") + 1
    ask = SOURCE[start:SOURCE.index("\n  }\n", start) + len("\n  }")]
    sent = _block("  const _pruneYesSent = ", ";")
    script = ASK % {"sent": sent, "ask": ask, "say": _function("_sayPruneRefused(scan)")}
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got["atSend"] == [["p1"], ["p2"], ["p3"], ["p4"]]       # known before the run can end
    assert got["toasts"] == [
        {"kind": "error", "key": "library.prune", "title": "Albums away", "text": "Not connected."},
        {"kind": "error", "key": "library.prune", "title": "Rescan", "text": "refused here"}]
    assert got["kept"] == ["p4"]
    assert got["whileOut"] == 0 and got["afterBack"] == 1          # PI-NODE-2E Q6: waits, asked when back


AWAY = r"""
'use strict';
const fmtNum = n => String(n);
const _count = (n, one, many) => `${fmtNum(n)} ${n === 1 ? one : many}`;
%(away)s
console.log(JSON.stringify(%(cases)s.map(_awayCount)));
"""


def test_the_away_count_says_the_copies_and_the_albums_no_other_copy_holds():
    cases = [{"copies": 27, "albums": 18}, {"copies": 1, "albums": 1}, {"copies": 3, "albums": 0}]
    script = AWAY % {"away": _function("_awayCount(away)"), "cases": json.dumps(cases)}
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == ["27 copies away — 18 albums have no other copy",
                                      "1 copy away — 1 album has no other copy",
                                      "3 copies away — each album has another copy"]
