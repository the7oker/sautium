"""backend/static/player.js's transport primitives under Node: a word the
output did not take is said to the person through the shell's one reporter
(reportPlaybackResult). Skipped where Node is not."""

import json
import shutil
import subprocess

import pytest

from tests.conftest import REPO_ROOT

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

_DRIVER = r"""
const fs = require('node:fs'), vm = require('node:vm');
const reported = [];
const window = {
  addEventListener() {}, dispatchEvent() {},
  reportPlaybackResult: async (resp) => { reported.push(resp ? resp.status : null); return false; },
};
const document = { readyState: 'loading', addEventListener() {}, visibilityState: 'visible' };
const fetch = async () => ({ ok: false, status: 409,
                             json: async () => ({ detail: 'HQPlayer is not answering' }) });
vm.runInNewContext(fs.readFileSync(process.argv[process.argv.length - 1], 'utf8'), {
  window, document, fetch, crypto: require('node:crypto').webcrypto, CustomEvent: class {},
  console: { warn() {}, log() {}, error() {} }, setTimeout, clearTimeout,
});
(async () => {
  const out = {};
  for (const word of JSON.parse(fs.readFileSync(0, 'utf8'))) {
    reported.length = 0;
    await (word === 'seek' ? window.playerSeek(60) : window.playerCmd(word));
    out[word] = reported.slice();
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def test_a_pause_a_stop_or_a_seek_the_output_did_not_take_is_said():
    """A Pause, a Stop or a Seek the route answers it did not take —
    HQPlayer building, or not answering past a short silence — is said, as a
    Play is: the music goes on, and the person must know their word did not
    go."""
    words = ["pause", "stop", "seek", "play"]
    proc = subprocess.run(
        [NODE, "-e", _DRIVER, "--", str(REPO_ROOT / "backend" / "static" / "player.js")],
        input=json.dumps(words).encode(), capture_output=True, check=True)
    assert json.loads(proc.stdout) == {w: [409] for w in words}, proc.stdout


_TAKEN = r"""
const fs = require('node:fs'), vm = require('node:vm');
const reported = [];
const window = {
  addEventListener() {}, dispatchEvent() {},
  reportPlaybackResult: async (resp, body, word) => { reported.push([resp.status, body, word]); return false; },
};
const document = { readyState: 'loading', addEventListener() {}, visibilityState: 'visible' };
const spec = JSON.parse(fs.readFileSync(0, 'utf8'));
const fetch = async () => ({ ok: true, status: 200, json: async () => spec.answer });
vm.runInNewContext(fs.readFileSync(process.argv[process.argv.length - 1], 'utf8'), {
  window, document, fetch, crypto: require('node:crypto').webcrypto, CustomEvent: class {},
  console: { warn() {}, log() {}, error() {} }, setTimeout, clearTimeout,
});
(async () => {
  const out = {};
  for (const word of spec.words) {
    reported.length = 0;
    await (word === 'seek' ? window.playerSeek(60) : window.playerCmd(word));
    out[word] = reported.slice();
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def _taken(answer, words):
    proc = subprocess.run(
        [NODE, "-e", _TAKEN, "--", str(REPO_ROOT / "backend" / "static" / "player.js")],
        input=json.dumps({"answer": answer, "words": words}).encode(), capture_output=True, check=True)
    return json.loads(proc.stdout)


def test_a_word_the_output_refused_in_its_own_words_is_said():
    """The route took the word to the output, and the output refused it in
    its own words (HQPlayer's `result="Error"`): HTTP 200 with ok false and
    the refusal. Said, as a word the route refused is — the music goes on."""
    words = ["pause", "stop", "seek", "play", "next", "previous"]
    refused = {"ok": False, "refusal": "HQPlayer refused Pause"}
    assert _taken(refused, words) == {w: [[200, refused, w]] for w in words}


def test_a_word_the_output_took_is_said_to_nobody():
    words = ["pause", "seek", "play"]
    assert _taken({"ok": True}, words) == {w: [] for w in words}
    assert _taken({"ok": True, "starts": {"index": 2}}, words) == {w: [] for w in words}


_REPORT = r"""
const fs = require('node:fs'), vm = require('node:vm');
const src = fs.readFileSync(process.argv[process.argv.length - 1], 'utf8');
const start = src.indexOf('  const TRANSPORT_WORDS = {');
const body = src.slice(start, src.indexOf('\n  window.reportPlaybackResult = reportPlaybackResult;', start));
const toasts = [];
const spec = JSON.parse(fs.readFileSync(0, 'utf8'));
const context = {
  window: { currentStatus: spec.status }, notices: { toast: t => toasts.push(t) },
  escapeProfileHtml: s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'),
};
vm.runInNewContext(`'use strict';\n${body}\nthis.report = reportPlaybackResult;`, context);
(async () => {
  const back = await context.report({ ok: true, status: 200 }, spec.body, spec.word);
  process.stdout.write(JSON.stringify({ back, toasts }));
})();
"""


def _report(status, body, word):
    proc = subprocess.run(
        [NODE, "-e", _REPORT, "--", str(REPO_ROOT / "backend" / "static" / "app-shell.js")],
        input=json.dumps({"status": status, "body": body, "word": word}).encode(), capture_output=True, check=True)
    return json.loads(proc.stdout)


def test_the_shell_says_an_outputs_refusal_in_its_words_and_names_the_output():
    out = _report({"output": {"label": "HQPlayer"}}, {"ok": False, "refusal": "Busy <now>"}, "pause")
    assert out == {"back": False, "toasts": [{"kind": "error", "key": "player.refused",
                                              "title": "HQPlayer did not take the Pause",
                                              "text": "Busy &lt;now&gt;"}]}
    # a renderer names itself: escaped; an output that gave no reason says so
    out = _report({"output": {"label": "<b>Kitchen</b>"}}, {"ok": False, "refusal": ""}, "seek")
    assert out["toasts"][0]["title"] == "&lt;b&gt;Kitchen&lt;/b&gt; did not take the Seek"
    assert out["toasts"][0]["text"] == "It gave no reason."
    assert _report(None, {"ok": True}, "pause") == {"back": True, "toasts": []}
