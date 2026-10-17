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
