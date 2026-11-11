"""backend/static/app-shell.js's "Recognise instruments" line under Node:
what the line says on and off, measuring or with an estimate, with nothing
left, with tracks past what the machine holds, on a node with no tracks of
its own (no track at all: the analysable count leaves out copies away and
tracks under the floor), and nothing when the estimate could not be read; and
a payload asked for before a tap never repaints the switch. app-shell.js's own
functions, run as written; skipped where Node is not."""

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
const fmtNum = n => String(n);
%(time)s
%(line)s
const cases = %(cases)s;
process.stdout.write(JSON.stringify(cases.map(([ins, owned]) => _instrumentsLine(ins, owned))));
"""

CASES = [
    ({"enabled": True, "tracks": 4812, "seconds": 5 * 86400}, "4812 tracks to go: about 5 days of analysis on this machine."),
    ({"enabled": True, "tracks": 1, "seconds": 7200}, "1 track to go: about 2 hours of analysis on this machine."),
    ({"enabled": True, "tracks": 93, "seconds": None},
     "93 tracks to go. Measuring this machine's pace: the estimate follows the first tracks."),
    ({"enabled": True, "tracks": 0, "seconds": 0}, "Every track has its instruments."),
    ({"enabled": False, "tracks": 93, "seconds": 1096},
     "Off: instruments come only from peers. Here, 93 tracks would take less than an hour of analysis."),
    ({"enabled": False, "tracks": 93, "seconds": None},
     "Off: instruments come only from peers that recognised them."),
    ({"enabled": True, "tracks": None, "seconds": None}, ""),
    ({"enabled": True, "tracks": 3, "seconds": 7200, "too_long": 2},
     "3 tracks to go: about 2 hours of analysis on this machine. 2 longer tracks are past what "
     "this machine can hold now: their instruments can come from peers."),
    ({"enabled": False, "tracks": 0, "seconds": 0, "too_long": 1},
     "1 longer track is past what this machine can hold now: their instruments can come from peers."),
]
NO_TRACKS = ({"enabled": True, "tracks": 0, "seconds": 0},
             "No tracks of your own yet. The switch also decides whether streamed tracks are tagged.")


def test_the_line_says_what_the_instrument_pass_has_left_here():
    driver = DRIVER % {"time": _function("_analysisTime(seconds)"),
                       "line": _function("_instrumentsLine(ins, owned)"),
                       "cases": json.dumps([[c, 93] for c, _ in CASES] + [[NO_TRACKS[0], 0]])}
    proc = subprocess.run([NODE, "-e", driver], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [line for _, line in CASES] + [NO_TRACKS[1]]


PAINT_DRIVER = r"""
'use strict';
let _instrumentsWrites = 0;
let _instrumentsTaps = 0;
%(paintable)s
const out = [];
const askedBefore = _instrumentsTaps;          // a refresh begins
_instrumentsWrites += 1; _instrumentsTaps += 1;  // the tap
_instrumentsWrites -= 1;                        // its write lands
out.push(_instrumentsPaintable(askedBefore));   // the refresh's payload lands
out.push(_instrumentsPaintable(_instrumentsTaps));  // the write's own event
_instrumentsWrites += 1; _instrumentsTaps += 1;
out.push(_instrumentsPaintable(_instrumentsTaps));  // asked during a write
process.stdout.write(JSON.stringify(out));
"""


def test_a_payload_asked_for_before_a_tap_never_repaints_the_switch():
    """Round 3: the repaint checked for a write in flight when the payload
    landed, so a refresh begun before the tap painted the old state once the
    write had landed."""
    driver = PAINT_DRIVER % {"paintable": _function("_instrumentsPaintable(tapsAtAsk)")}
    proc = subprocess.run([NODE, "-e", driver], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [False, True, False]
