"""Compare by ear's lines under a step (AB-2): their own filter's slow start
said (B-131, C3), and the long-pause warning (W20) read off the step's own
silence. backend/static/app-shell.js's `abNum` and its block from `abOwnSlow`
to `abAnswers` are cut from the file and run under Node. Skipped where Node
is not."""

import json
import shutil
import subprocess

import pytest

from tests.conftest import REPO_ROOT

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

_RUN = r"""
const fs = require('node:fs');
const shell = fs.readFileSync(process.argv[process.argv.length - 2], 'utf8');
const cut = (from, to) => {
  const a = shell.indexOf(from), b = shell.indexOf(to, a);
  if (a < 0 || b < 0) throw new Error('marker not found: ' + (a < 0 ? from : to));
  return shell.slice(a, b);
};
const escapeHtml = s => String(s).replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);
const line = shell.split('\n').find(l => l.includes('const abNum = '));
eval(line + cut('function abOwnSlow', 'function abAnswers'));
const views = JSON.parse(process.argv[process.argv.length - 1]);
console.log(JSON.stringify(views.map(v => abWarnings(v))));
"""


def _warnings(*views):
    out = subprocess.run([NODE, "-e", _RUN, str(REPO_ROOT / "backend" / "static" / "app-shell.js"),
                          json.dumps(views)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_their_slow_filter_and_a_steps_long_silence_are_said_on_their_own():
    plain, slow, long_step, both = _warnings(
        {"own_slow": None, "long_silence": False, "sdm": False},
        {"own_slow": 12.3, "long_silence": False, "sdm": False},
        {"own_slow": None, "long_silence": True, "sdm": False},
        {"own_slow": 12.3, "long_silence": True, "sdm": True})
    assert plain == ""
    assert slow == ('<p class="ab-warn">Your filter takes <span class="ab-num">12.3 s</span> to start here, '
                    'so the pause before it is long.</p>')
    assert long_step == '<p class="ab-warn">Long pauses make small differences harder to hear.</p>'
    assert both.startswith(slow) and "Long pauses" in both and "click between fragments" in both
