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
%(stale)s
const drawn = shown => ({ querySelector: sel => shown.includes(sel) ? {} : null });
const SCAN = '[data-cancel-scan]', ENRICH = '[data-cancel-enrich]', EMPTY = '.empty-library';
const lib = (tracks, analysis = true) => ({ total_tracks: tracks, analysis_available: analysis });
const cases = %(cases)s;
console.log(JSON.stringify(cases.map(c =>
  _libraryStructureStale(drawn(c.shown.map(s => ({ SCAN, ENRICH, EMPTY })[s])), lib(c.tracks, c.analysis !== false),
                         !!c.scan, !!c.enrich))));
"""

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
    cases = [{"shown": shown, "tracks": tracks, "scan": scan, "enrich": enrich}
             for shown, tracks, scan, enrich, _ in CASES]
    script = DRIVER % {"stale": _function("_libraryStructureStale(root, lib, scanRunning, enrichRunning)"),
                       "cases": json.dumps(cases)}
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [stale for *_, stale in CASES]


def test_an_analysis_no_row_can_show_never_asks_for_a_redraw():
    """A node with no analysis of its own, or an empty library, draws no
    analysis row: a redraw could not show the run, and would be asked for at
    every wake."""
    cases = [{"shown": [], "tracks": 93, "enrich": True, "analysis": False},
             {"shown": ["EMPTY"], "tracks": 0, "enrich": True}]
    script = DRIVER % {"stale": _function("_libraryStructureStale(root, lib, scanRunning, enrichRunning)"),
                       "cases": json.dumps(cases)}
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [False, False]


def test_an_empty_librarys_first_scan_is_shown_with_its_cancel_and_no_run_first_scan():
    start = SOURCE.index("const emptyState = ")
    block = SOURCE[start:SOURCE.index("const actions = ''", start)]
    assert "</div>`) + (scanRunning ? scanActions : '');" in block
    assert "${scanRunning ? '' : `<button class=\"empty-cta\" data-action=\"scan\">" in block
