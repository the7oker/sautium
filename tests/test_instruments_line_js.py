"""backend/static/app-shell.js's "Recognise instruments" line under Node:
what the line says on and off, measuring or with an estimate, with nothing
left, and nothing when the estimate could not be read. app-shell.js's own
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
process.stdout.write(JSON.stringify(cases.map(_instrumentsLine)));
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
]


def test_the_line_says_what_the_instrument_pass_has_left_here():
    driver = DRIVER % {"time": _function("_analysisTime(seconds)"),
                       "line": _function("_instrumentsLine(ins)"),
                       "cases": json.dumps([c for c, _ in CASES])}
    proc = subprocess.run([NODE, "-e", driver], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [line for _, line in CASES]
