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
