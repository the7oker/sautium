"""The peak meter sheet's SDM arc (backend/static/app-shell.js,
openPeakMeter) under Node: the sheet's block, cut from the file between its
banner and the next section's, runs against a stub DOM while status ticks
are replayed — what the published near-full-scale table says of the
modulator and rate playing (status.sdm_room) draws the amber arc or takes
it away, and words its advice. Skipped where Node is not."""

import json
import shutil
import subprocess

import pytest

from tests.conftest import REPO_ROOT

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

_DRIVER = r"""
const fs = require('node:fs'), vm = require('node:vm');
const shell = fs.readFileSync(process.argv[process.argv.length - 1], 'utf8');
const start = shell.indexOf('  /* ---- Peak meter (HQPlayer screen');
const end = shell.indexOf('  /* =====', start);
if (start < 0 || end < 0) throw new Error('the peak meter block was not found');

function el() {
  return {
    _html: '', textContent: '', hidden: false, disabled: false, style: {}, className: '',
    classList: { toggle() {} }, toggleAttribute() {}, addEventListener() {}, focus() {}, remove() {},
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = v; if (this.onset) this.onset(v); },
  };
}
const els = {};
let faces = [], facesHtml = '';
const sheet = el();
sheet.querySelector = sel => {
  if (!els[sel]) {
    els[sel] = el();
    if (sel === '[data-pm-faces]') {
      els[sel].onset = v => {
        facesHtml = v;
        faces = (v.match(/class="pm-face"/g) || []).map(() => {
          const face = el(), kids = {};
          face.querySelector = s => (kids[s] = kids[s] || el());
          return face;
        });
      };
    }
  }
  return els[sel];
};
sheet.querySelectorAll = sel => (sel === '.pm-face' ? faces : sel === '[data-vol]' ? [el(), el()] : []);
const on = { doc: {}, win: {} };
const document = { createElement: () => sheet, body: { appendChild() {} },
                   addEventListener: (t, f) => { on.doc[t] = f; }, removeEventListener() {} };
const window = { currentStatus: null, meterWanted() {}, meterRetry() {},
                 addEventListener: (t, f) => { on.win[t] = f; }, removeEventListener() {} };
const escapeHtml = s => String(s).replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);
const ctx = { window, document, performance: { now: () => 0 }, escapeHtml, fmtVolume: v => `${v} dB` };
vm.runInNewContext(shell.slice(start, end) + '\n;this.openPeakMeter = openPeakMeter;', ctx);

const seen = () => ({ arc: /class="pm-sdm"/.test(facesHtml),
                      advice: (els['[data-pm-advice]'] || el()).innerHTML.replace(/<[^>]+>/g, '') });
const [first, ...ticks] = JSON.parse(fs.readFileSync(0, 'utf8'));
window.currentStatus = first;
ctx.openPeakMeter({ getState: () => ({}), stepVolume: async () => '' });
on.win['sautium:meter']({ detail: { state: 'open' } });
on.win['sautium:meter']({ detail: { p: [-1.5, -1.6], g: -1 } });     // Max inside -3...0 dBTP
const out = [seen()];
for (const st of ticks) {
  on.doc['np-update']({ detail: st });
  out.push(seen());
}
process.stdout.write(JSON.stringify(out));
"""


def _sheet(*statuses):
    proc = subprocess.run(
        [NODE, "-e", _DRIVER, "--", str(REPO_ROOT / "backend" / "static" / "app-shell.js")],
        input=json.dumps(statuses).encode(), capture_output=True, check=True)
    return [(s["arc"], s["advice"]) for s in json.loads(proc.stdout)]


def _sdm(room=None, modulator="MOD-A", rate=None, **kw):
    """An SDM tick, its sdm_room as the backend sends it: `room` a word of the
    table or None; with no `rate` the tick names none and carries none."""
    st = {"state": "playing", "volume": -1, "active_mode": "SDM (DSD)", **kw}
    if rate:
        st["sdm_room"] = {"room": room, "modulator": modulator, "rate": rate}
    return st


ADVISED = "SDM output: −3 dB or lower recommended for {} at {}."
MANUAL = "SDM output: HQPlayer advises keeping 3 dB below full scale for the modulator."


def test_the_arc_follows_the_published_tables_word_for_the_modulator_and_rate_playing():
    assert _sheet(
        _sdm("advised", rate="DSD256"),                     # not stable at 0 dB, stable at -3 dB: the arc, named
        _sdm(state="stopped"),                              # a tick that names no rate: the last word kept
        _sdm("holds", rate="DSD512"),                       # stable to full scale at 0 dB: no arc, no advice
        _sdm(),
        {"state": "playing", "volume": -1, "active_mode": "PCM"},
        _sdm(),                                             # PCM ended the word: back in SDM, the manual's
        _sdm("holds", modulator="MOD-B", rate="DSD256"),
        _sdm(None, modulator="MOD-C", rate="DSD256"),       # the table says nothing: the manual's arc
        _sdm("advised", modulator="MOD-C", rate="DSD128"),
    ) == [(True, ADVISED.format("MOD-A", "DSD256")), (True, ADVISED.format("MOD-A", "DSD256")),
          (False, ""), (False, ""), (False, ""), (True, MANUAL), (False, ""), (True, MANUAL),
          (True, ADVISED.format("MOD-C", "DSD128"))]


def test_a_sheet_opened_while_the_table_takes_the_arc_away_draws_none():
    assert _sheet(_sdm("holds", rate="DSD256"), _sdm(state="paused")) == [(False, ""), (False, "")]
    assert _sheet({"state": "playing", "volume": -1, "active_mode": "PCM"}) == [(False, "")]
