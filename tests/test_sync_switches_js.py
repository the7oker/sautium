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


SUPPORT_DRIVER = r"""
'use strict';
const OPTION = [{ id: 0, label: 'Off' }];
const AUTO_SYNC_OPTIONS = OPTION, ANNOUNCE_LIMIT_OPTIONS = OPTION, CARRY_LIMIT_OPTIONS = OPTION;
const SETTINGS_ICONS = { chev: '' };
const escapeProfileHtml = s => String(s ?? '');
const fmtRelative = () => 'now', fmtNum = n => String(n);
const _bgEnrichStatusLine = () => '', _syncCadenceLine = () => '';
const identityLabel = () => '', identityDetail = () => '';
const loadLabel = () => '', loadDetail = () => '', gateLabel = () => '', gateDetail = () => '';
%(label)s
%(detail)s
%(render)s
const request = { id: 'r1', scopes: ['system', 'chat'], structural: ['system'], content: ['chat'] };
const out = {};
for (const sharing of [true, false]) {
  for (const enabled of [true, false]) {
    out[`sharing ${sharing} enabled ${enabled}`] = _renderSyncSettings(
      { p2p_enabled: sharing, support: { enabled, requests: [request] } });
  }
}
out.none = _renderSyncSettings({ p2p_enabled: true, support: { enabled: true, requests: [] } });
process.stdout.write(JSON.stringify(out));
"""


def test_a_support_request_waiting_makes_the_support_row_its_way_in():
    """A request that waits for the person's decision: the Support row says
    so and is the button that opens the request sheet, whatever P2P sharing
    says — and only while diagnostics are on."""
    proc = subprocess.run([NODE, "-e", SUPPORT_DRIVER % {
        "label": _function("supportLabel(s)"), "detail": _function("supportDetail(s)"),
        "render": _function("_renderSyncSettings(sync)")}],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    for sharing in ("true", "false"):
        html = out[f"sharing {sharing} enabled true"]
        assert html.count('data-action="review-support"') == 1, sharing
        assert "request waiting — review" in html
        assert 'data-action="review-support"' not in out[f"sharing {sharing} enabled false"]
    assert 'data-action="review-support"' not in out["none"]
