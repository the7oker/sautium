"""backend/static/app-shell.js's Sync & P2P settings under Node: the switches
for work that goes on whatever P2P sharing says — relaying, support
diagnostics — and the Support row, which says what left for support, show in
both states of P2P sharing. app-shell.js's own function, run as written;
skipped where Node is not."""

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
const OPTION = [{ id: 0, label: 'Off' }];
const AUTO_SYNC_OPTIONS = OPTION, ANNOUNCE_LIMIT_OPTIONS = OPTION, CARRY_LIMIT_OPTIONS = OPTION;
const SETTINGS_ICONS = { chev: '' };
const escapeProfileHtml = s => String(s ?? '');
const fmtRelative = () => 'now', fmtNum = n => String(n);
const _bgEnrichStatusLine = () => '', _syncCadenceLine = () => '';
const identityLabel = () => '', identityDetail = () => '';
const loadLabel = () => '', loadDetail = () => '', gateLabel = () => '', gateDetail = () => '';
const supportLabel = s => 'support-row:' + (s && s.enabled ? 'on' : 'off'), supportDetail = () => '';
%(render)s
const out = {};
for (const sharing of [true, false]) {
  out[sharing ? 'sharing on' : 'sharing off'] = _renderSyncSettings(
    { p2p_enabled: sharing, relay_enabled: true, support: { enabled: true } });
}
process.stdout.write(JSON.stringify(out));
"""


def test_relaying_and_diagnostics_keep_their_switch_and_row_whatever_sharing_says():
    proc = subprocess.run([NODE, "-e", DRIVER % {"render": _function("_renderSyncSettings(sync)")}],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    for state, html in json.loads(proc.stdout).items():
        assert html.count('data-action="toggle-relay"') == 1, state
        assert html.count('data-action="toggle-support"') == 1, state
        assert html.count("support-row:on") == 1, state
