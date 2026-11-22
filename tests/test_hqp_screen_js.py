"""backend/static/app-shell.js's HQPlayer screen under Node (HQP-WORDS-S):
the "Profile" row from the status's `configuration` — drawn hidden while
HQPlayer names none, kept in place by each status, left as it is by a
disconnected one — and the Library block's scan line, by the HQPlayer's
product. app-shell.js's own functions, run as written; skipped where Node is
not."""

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


def _const(name: str) -> str:
    start = SOURCE.index(f"\n  const {name} = ") + 1
    return SOURCE[start:SOURCE.index(";\n", start) + 1]


def _run(driver: str):
    proc = subprocess.run([NODE, "-e", driver], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


PROFILE_DRIVER = r"""
'use strict';
%(escape)s
%(row)s
%(update)s
// One row on the page, as the screen draws it; its value and `hidden` are
// all the update touches
const value = { textContent: '' };
const row = { hidden: true, querySelector: () => value };
const document = { querySelector: () => row };
const read = () => ({ hidden: row.hidden, value: value.textContent });
const out = {};
out.none = hqpProfileRow({ state: 'stopped' });
out.named = hqpProfileRow({ state: 'playing', configuration: 'Room <A>' });
out.offline = hqpProfileRow(false);
updateHqpProfile({ state: 'playing', configuration: 'Desk' });
out.shown = read();
updateHqpProfile({ state: 'disconnected' });
out.disconnected = read();
updateHqpProfile({ state: 'stopped' });
out.gone = read();
process.stdout.write(JSON.stringify(out));
"""


def test_the_profile_row_says_hqplayers_active_configuration_and_follows_the_status():
    out = _run(PROFILE_DRIVER % {"escape": _function("escapeHtml(s)"),
                                 "row": _function("hqpProfileRow(status)"),
                                 "update": _function("updateHqpProfile(status)")})
    assert "data-hqp-profile hidden" in out["none"] and "data-hqp-profile hidden" in out["offline"]
    assert "data-hqp-profile>" in out["named"]
    assert '<span class="hqp-row-label">Profile</span>' in out["named"]
    assert '<span class="hqp-row-value">Room &lt;A&gt;</span>' in out["named"]
    assert out["shown"] == {"hidden": False, "value": "Desk"}
    assert out["disconnected"] == out["shown"]          # the screen says offline; the row waits
    assert out["gone"] == {"hidden": True, "value": ""}


def test_the_library_block_says_where_hqplayer_scans_before_a_sync():
    out = _run("'use strict';\n" + _const("_hqpLibScanHint") + """
process.stdout.write(JSON.stringify([
  _hqpLibScanHint(true), _hqpLibScanHint(false), _hqpLibScanHint(undefined)]));""")
    assert out == ["Added music? Scan in HQPlayer's web interface first, then Sync library.",
                   "Added music? Scan in HQPlayer first, then Sync library.",
                   "Added music? Scan in HQPlayer first, then Sync library."]


DIAG_DRIVER = r"""
'use strict';
%(escape)s
%(consts)s
%(block)s
const a = (id, code, title) => ({ id, started: '2026-11-22T10:00:00Z', artist: 'A', title: 'T' + id,
                                  intent: 'play', verdict: { code, title } });
process.stdout.write(JSON.stringify({
  failed: hqpDiagnosticsBlock({ attempts: [a(3, 'played', 'Played'), a(2, 'refused_rate', 'Refused'),
                                           a(1, 'interrupted', 'Stopped')], log_source: { kind: 'local' } }),
  played: hqpDiagnosticsBlock({ attempts: [a(2, 'played', 'Played'), a(1, 'interrupted', 'Stopped')],
                                log_source: { kind: 'local' } }),
  none: hqpDiagnosticsBlock({ attempts: [], log_source: { kind: 'none' } }),
}));
"""


def test_diagnostics_lists_the_plays_that_did_not_play_and_folds_the_rest():
    """B-125: the block's rows are the attempts that did not play; with none,
    one line; every attempt folded under one quiet "All plays" link."""
    consts = "\n".join(_const(n) for n in ("HQP_INTENTS", "HQP_LOG_SOURCES", "HQP_CHEVRON", "fmtClock"))
    out = _run(DIAG_DRIVER % {"escape": _function("escapeHtml(s)"), "consts": consts,
                              "block": _function("hqpDiagnosticsBlock(d)")})
    head, _, folded = out["failed"].partition('<details class="hqp-diag-all">')
    assert head.count('data-attempt=') == 1 and 'data-attempt="2"' in head
    assert folded.count('data-attempt=') == 3 and "<summary>All plays ›</summary>" in folded
    head, _, folded = out["played"].partition('<details class="hqp-diag-all">')
    assert 'data-attempt=' not in head
    assert "Every play since the node started reached HQPlayer." in head
    assert folded.count('data-attempt=') == 2
    assert "hqp-diag-all" not in out["none"]
    assert "none yet since the node started" in out["none"]


MARK_DRIVER = r"""
'use strict';
%(escape)s
%(range)s
const fmtRateLabel = (hz) => (hz / 1000) + ' kHz';
%(mark)s
process.stdout.write(JSON.stringify({
  swung: hqpHeadroomMark({ class: 'ok', speed: 21.29, n: 1, settled: true, lo: 21.0, hi: 33.46 }),
  steady: hqpHeadroomMark({ class: 'ok', speed: 10.49, n: 1, settled: true, lo: 10.41, hi: 10.6 }),
  near: hqpHeadroomMark({ class: 'tight', speed: 1.28, n: 1, settled: false, lo: 1.08, hi: 1.71 }),
  implied: hqpHeadroomMark({ class: 'no', speed: null, n: 0, settled: true, implied_from: 352800 }),
}));
"""


def test_a_mark_shows_its_range_where_the_readings_swung_and_a_word_where_a_neighbour_answered():
    out = _run(MARK_DRIVER % {"escape": _function("escapeHtml(s)"),
                              "range": _function("hqpSpeedRange(lo, hi)"),
                              "mark": _function("hqpHeadroomMark(entry, { onlyShort = false } = {})")})
    assert out["swung"].endswith("21–33×</span>")
    assert out["steady"].endswith("10.5×</span>")
    assert out["near"].endswith("~1.1–1.7×</span>")
    assert "too slow</span>" in out["implied"] and "slower than at 352.8 kHz" in out["implied"]
