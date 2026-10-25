"""backend/static/app-shell.js's chat stream under Node: a reply is finished
by its `done` event, never by its stream's close.

A backend that stops ends its event streams in order (serving.EventStream),
so a reply cut short by a stop arrives as a stream closed cleanly before its
`done` — read as a finished reply, its bubble stayed half-written and no
reattach followed. It is a drop: the reattach finds the run gone, and the
messages say whether the reply was kept. app-shell.js's own functions, run as
written; skipped where Node is not.
"""

import json
import shutil
import subprocess

import pytest

from tests.conftest import REPO_ROOT

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

SOURCE = (REPO_ROOT / "backend" / "static" / "app-shell.js").read_text(encoding="utf-8")


def _method(head: str) -> str:
    """An `ai` method as written: from its head to its closing `},`."""
    start = SOURCE.index(f"\n    {head} {{\n") + 1
    return SOURCE[start:SOURCE.index("\n    },\n", start) + len("\n    },")]


def _function(head: str) -> str:
    start = SOURCE.index(f"\n  function {head} {{\n") + 1
    return SOURCE[start:SOURCE.index("\n  }\n", start) + len("\n  }")]


DRIVER = r"""
'use strict';
%(parse)s
const STREAM_RECONNECT_DELAYS_MS = [0, 0, 0, 0];
const toolPipLabel = name => name;
const window = { awaitReconnectWindow: async () => 'timer' };
const log = [];
const ai = {
  activeSessionId: 7,
  chatTitle: {},
  sessions: [],
%(consume)s
%(pump)s
  _newStreamCtx() {
    return {
      lastEventAt: 0,
      finish() { log.push('finish'); },
      fail(err) { log.push('fail: ' + err.message); },
      reset() { log.push('reset'); },
      showPip(text) { log.push('pip: ' + text); },
      onModel() {},
      onDelta(text) { log.push('delta: ' + text); },
      onBlocks() {},
    };
  },
  async loadSessions() {},
  scrollToBottom() {},
  async _openReattach() { log.push('reattach'); return 'gone'; },
  async _finishFromDb() { log.push('the messages'); },
};
const body = text => {
  const chunks = [new TextEncoder().encode(text)];
  return { body: { getReader: () => ({
    read: async () => chunks.length ? { value: chunks.shift(), done: false } : { value: undefined, done: true },
  }) } };
};
console.warn = () => {};
(async () => {
  const out = {};
  for (const [name, text] of Object.entries(JSON.parse(require('node:fs').readFileSync(0, 'utf8')))) {
    log.length = 0;
    await ai.consumeStream(body(text), {});
    out[name] = log.slice();
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def _sse(*events) -> str:
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)


def test_a_reply_is_finished_by_its_done_and_a_close_before_it_is_a_drop():
    script = DRIVER % {"parse": _function("parseSseMessage(raw)"),
                       "consume": _method("async consumeStream(resp, typing)"),
                       "pump": _method("async _pumpStream(resp, ctx)")}
    cases = {
        "cut short": _sse(("meta", {"model": "m"}), ("delta", {"text": "Hel"})),
        "replied": _sse(("meta", {"model": "m"}), ("delta", {"text": "Hello"}), ("done", {"model": "m"})),
        "failed": _sse(("delta", {"text": "Hel"}), ("error", {"message": "the provider refused"})),
    }
    proc = subprocess.run([NODE, "-e", script], input=json.dumps(cases).encode(), capture_output=True,
                          check=True)
    assert json.loads(proc.stdout) == {
        "cut short": ["delta: Hel", "pip: reconnecting…", "reset", "reattach", "the messages"],
        "replied": ["delta: Hello", "finish"],
        "failed": ["delta: Hel", "fail: the provider refused"],
    }, proc.stdout
