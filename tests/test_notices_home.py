"""Each notice in its own domain (NOTICES-HOME-1).

The server names a notice's domain where it derives it
(backend/routers/settings._notice_items): Sync & P2P draws the network's
lines alone, every other state is a lasting toast at its onset and its
standing state on its own domain's screen, and a report is seen at its toast.
The tab's side runs app-shell.js's own functions under Node, strict as the
page is; skipped where Node is not.
"""

import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
SHELL = BACKEND / "static" / "app-shell.js"
NODE = shutil.which("node")
DOMAINS = {"network", "enrichment", "library", "streaming", "hqplayer", "assistant", "support",
           "account", "node"}


def _dict_literals(tree: ast.AST, names: set):
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name in names:
            for node in ast.walk(fn):
                if isinstance(node, ast.Dict):
                    keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
                    if "key" in keys and "since" in keys or "key" in keys and "kind" in keys:
                        yield fn.name, node, dict(zip(keys, node.values))


def test_every_notice_names_its_domain_where_it_is_derived():
    """Every item a derivation builds carries `domain`, a literal of the set
    (or the cooldowns' own map, whose default is the enrichment's) — a notice
    without one would land on no screen, and the page keeps no list of keys."""
    found = []
    for path, names in ((BACKEND / "routers" / "settings.py", {"_notice_items", "_failures_notice"}),
                        (BACKEND / "power_watch.py", {"notice"})):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn, node, fields in _dict_literals(tree, names):
            assert "domain" in fields, (path.name, fn, ast.unparse(node)[:120])
            dom = fields["domain"]
            if isinstance(dom, ast.Constant):
                assert dom.value in DOMAINS, dom.value
            else:
                assert ast.unparse(dom) == "_COOLDOWN_DOMAIN.get(row['source'], 'enrichment')"
            found.append(fn)
    assert found.count("_notice_items") >= 22 and "notice" in found and "_failures_notice" in found


def test_musicbrainz_alone_of_the_cooldowns_is_the_networks():
    settings_router = pytest.importorskip("routers.settings")
    assert settings_router._COOLDOWN_DOMAIN == {"musicbrainz": "network"}


def _functions(*names: str) -> str:
    src = SHELL.read_text(encoding="utf-8")
    return "".join(re.search(rf"\n  function {name}\(.*?\) \{{\n.*?\n  \}}\n", src, re.S).group(0)
                   for name in names)


ONE_OF_EACH = [
    {"key": "mb_slice.deferred", "domain": "network", "kind": "info", "since": "a"},
    {"key": "cooldown.musicbrainz", "domain": "network", "kind": "warning", "since": "a"},
    {"key": "cooldown.lastfm", "domain": "enrichment", "kind": "warning", "since": "a",
     "until": None, "data": {"source": "lastfm", "strikes": 1}},
    {"key": "cooldown.deezer", "domain": "enrichment", "kind": "warning", "since": "a",
     "until": None, "data": {"source": "deezer", "strikes": 2}},
    {"key": "disk.new.U", "domain": "library", "kind": "info", "since": "a"},
    {"key": "library.folder_out.3", "domain": "library", "kind": "warning", "since": "a"},
    {"key": "disk.unread.sdb1", "domain": "library", "kind": "warning", "since": "a",
     "data": {"label": "STICK", "format": "HFS+", "mounts": False}},
    {"key": "tools.missing", "domain": "library", "kind": "warning", "since": "a", "data": {"tools": ["fpcalc"]}},
    {"key": "streaming.silent.yt", "domain": "streaming", "kind": "warning", "since": "a",
     "data": {"provider": "yt", "name": "Demo channel", "reason": "timeout"}},
    {"key": "streaming.aside.byo", "domain": "streaming", "kind": "warning", "since": "a",
     "data": {"provider": "byo", "name": "BYO", "title": "BYO stepped aside", "text": "Its key <expired>"}},
    {"key": "hqplayer.run_unchecked", "domain": "hqplayer", "kind": "info", "since": "a",
     "data": {"mode": "PCM", "run": 7, "refused": True}},
    {"key": "claude_code.signed_out", "domain": "assistant", "kind": "error", "since": "a"},
    {"key": "support.request.9", "domain": "support", "kind": "info", "since": "a"},
    {"key": "auth.login_failures", "domain": "account", "kind": "warning", "since": "a", "report": True},
    {"key": "update.result", "domain": "node", "kind": "info", "since": "a", "report": True},
]

_ROWS = r"""
const notices = { list: () => %s };
const noticeCopy = n => ({ title: `T:${n.key}`, text: '' });
process.stdout.write(_renderNoticeRows());
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_sync_and_p2p_draws_no_notice_of_another_domain():
    """Sync & P2P's rows are the network's lines alone: a notice of any other
    domain — a disk, an update, HQPlayer, a sign-in — comes back there and
    this fails (the owner's shots of 2026-11-26)."""
    out = subprocess.run([NODE, "-e", "'use strict';" + _functions("escapeHtml", "_renderNoticeRows")
                          + _ROWS % json.dumps(ONE_OF_EACH)],
                         capture_output=True, text=True, check=True).stdout
    assert re.findall(r"T:([\w.]+)", out) == ["mb_slice.deferred", "cooldown.musicbrainz"]
    # ... and the screen draws the rows through that one function, and no
    # other notice but the enrichment's pauses on its own line
    src = SHELL.read_text(encoding="utf-8")
    sync = re.search(r"\n  function _renderSync\(sync\) \{\n(.*?)\n  \}\n", src, re.S).group(1)
    assert sync.strip() == "return _renderNoticeRows() + _renderSyncSettings(sync);"
    settings = re.search(r"\n  function _renderSyncSettings\(sync\) \{\n.*?\n  \}\n", src, re.S).group(0)
    assert "notices." not in settings and "noticeCopy" not in settings


_ENRICHMENT = r"""
const notices = { list: () => ITEMS, get: k => ITEMS.find(n => n.key === k) || null };
let ITEMS = %s;
const fmtRelative = () => '3 min ago';
const fmtUntil = () => 'at <span class="num">14:20</span>';
const escapeProfileHtml = escapeHtml;
const out = {
  on: _bgEnrichStatusLine({ current_step: 'idle', last_run_at: 'x', totals: {} }, true),
  off: _bgEnrichStatusLine({ current_step: 'idle', last_run_at: 'x', totals: {} }, false),
};
ITEMS = [];
out.none = _bgEnrichStatusLine({ current_step: 'idle', last_run_at: 'x', totals: {} }, true);
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_enrichment_line_says_its_services_pauses():
    """Last.fm's cooldown stands in place of "Idle" while the switch is on;
    Deezer's — the photos and the 30-second excerpts — is said whatever the
    switch; with neither, "Idle" as before."""
    js = _functions("escapeHtml", "noticeStrikes", "_cooldownLine", "_bgPauseLines", "_bgEnrichStatusLine")
    out = json.loads(subprocess.run([NODE, "-e", "'use strict';" + js + _ENRICHMENT % json.dumps(ONE_OF_EACH)],
                                    capture_output=True, text=True, check=True).stdout)
    text = {k: re.sub(r"<[^>]+>", "", v) for k, v in out.items()}
    assert "Paused · Last.fm asked to slow down — resumes at 14:20" in text["on"]
    assert "Idle" not in text["on"]
    deezer = "Artist photos and 30-second excerpts paused · Deezer asked to slow down (2× in a row) — resume at 14:20"
    assert deezer in text["on"] and deezer in text["off"]
    assert "Last.fm" not in text["off"]
    assert "Idle · last batch 3 min ago" in text["none"] and "paused" not in text["none"]


_STATES = r"""
const ITEMS = %s;
const notices = { list: () => ITEMS, get: k => ITEMS.find(n => n.key === k) || null };
const noticeCopy = n => n.key === 'hqplayer.run_unchecked'
  ? { text: 'The PCM settings are not checked yet.', action: { label: 'Leave it as it is', run() {} } }
  : { title: '', text: '' };
const fmtNum = String;
%s
process.stdout.write(JSON.stringify(Object.fromEntries(Object.keys(NOTICE_STATES).map(k => [k, NOTICE_STATES[k]()]))));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_each_state_shows_on_its_own_screen():
    """The states that had no screen of their own: an unread disk beside the
    Library's folders, the media tools under Analyse library, the stream
    providers on the Streaming library with their Check again, a benchmark
    mark on HQPlayer's Benchmark with its Leave — each drawn from the channel,
    and nothing where the channel holds none."""
    src = SHELL.read_text(encoding="utf-8")
    states = src[src.index("  const NOTICE_STATES = {"):src.index("  function noticeState(name)")]
    js = _functions("escapeHtml", "unreadDiskCopy", "asideNoticeCopy")
    out = json.loads(subprocess.run([NODE, "-e", "'use strict';" + js + _STATES % (json.dumps(ONE_OF_EACH), states)],
                                    capture_output=True, text=True, check=True).stdout)
    text = {k: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", v)).strip() for k, v in out.items()}
    assert "STICK" in text["unread-disks"] and "Sautium reads FAT, exFAT, NTFS and ext4 disks." in text["unread-disks"]
    assert text["tools"].startswith("fpcalc not found — audio analysis and fingerprinting are skipped")
    assert "Providers" in text["providers"] and "Demo channel not answering" in text["providers"]
    assert "BYO stepped aside" in text["providers"] and "Its key &lt;expired&gt;" in out["providers"]
    assert 'data-action="provider-check" data-provider="byo"' in out["providers"]
    assert 'data-action="bench-leave" data-run="7"' in out["benchmark-marks"]
    empty = json.loads(subprocess.run([NODE, "-e", "'use strict';" + js + _STATES % ("[]", states)],
                                      capture_output=True, text=True, check=True).stdout)
    assert empty == {k: "" for k in out}


_APPLY = r"""
const fs = require('node:fs'), vm = require('node:vm');
const src = fs.readFileSync(process.argv[process.argv.length - 1], 'utf8');
const literal = src.slice(src.indexOf('  const notices = {'), src.indexOf('  window.notices = notices;'));
const kept = new Map();
const posted = [];
const context = {
  sessionStorage: { getItem: k => kept.get(k) || null, setItem: (k, v) => kept.set(k, v) },
  window: { dispatchEvent() {} }, CustomEvent: class {}, console,
  noticeCopy: n => ({ title: n.key, text: '' }),
  fetch: async (url, opts) => { posted.push([url, JSON.parse(opts.body)]); return { ok: true }; },
};
vm.runInNewContext(`'use strict';\n${literal}\nthis.notices = notices;`, context);
const n = context.notices;
const raised = [], resolved = [];
n.toast = t => raised.push(t);
n.resolve = (key, tag) => resolved.push([key, tag]);
const net = { key: 'mb_slice.deferred', domain: 'network', since: 'n1' };
const out = (folder, rep, upd) => [folder, rep, upd].filter(Boolean);
const folder = { key: 'library.folder_out.3', domain: 'library', since: 'f1' };
const report = { key: 'auth.login_failures', domain: 'account', since: 'r1', report: true, seen: false };
const update = { key: 'update.result', domain: 'node', since: 'u1', report: true, seen: false };
// a page opened while the folder is out and two reports stand
n.apply({ items: [folder, report, update], initial: true });
const atConnect = raised.map(t => [t.key, !!t.lasting, t.tag || null]);
// the same snapshot again: nothing new
n.apply({ items: [folder, report, update] });
const again = raised.length;
// the network line and a new folder onset arrive
n.apply({ items: [net, { ...folder, since: 'f2' }, report] });
const later = raised.slice(again).map(t => [t.key, !!t.lasting, t.tag || null]);
// the person closes the report's toast: its seen, on every page
raised.find(t => t.key === 'auth.login_failures').heard();
// the folder is back, the report seen: their toasts go
n.apply({ items: [net] });
setTimeout(() => process.stdout.write(JSON.stringify({ atConnect, again, later, resolved, posted })), 10);
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_state_is_a_lasting_toast_and_a_report_is_seen_at_its_toast():
    """The connect-time snapshot toasts no state (a page opened later shows
    the state on its screen) but does tell an unseen report, lasting until
    its close — and "Updated to …", an event that is over, as a timed toast
    seen as it is told. A state's toast is lasting, tagged by its onset, and
    resolved by the snapshot that no longer carries it, or carries it at
    another onset; a network line's is timed, as before. A report's close
    posts its seen."""
    proc = subprocess.run([NODE, "-e", _APPLY, "--", str(SHELL)], capture_output=True, text=True, check=True)
    out = json.loads(proc.stdout)
    assert out["atConnect"] == [["auth.login_failures", True, "r1"], ["update.result", False, None]]
    assert out["again"] == 2
    assert out["later"] == [["mb_slice.deferred", False, None], ["library.folder_out.3", True, "f2"]]
    assert ["library.folder_out.3", "f1"] in out["resolved"] and ["library.folder_out.3", "f2"] in out["resolved"]
    assert ["auth.login_failures", "r1"] in out["resolved"]
    assert ["mb_slice.deferred", "n1"] not in out["resolved"]
    assert out["posted"] == [["/api/settings/notices/update.result/seen", {"since": "u1"}],
                             ["/api/settings/notices/auth.login_failures/seen", {"since": "r1"}]]


def test_the_trail_names_no_notices_task():
    """The trail lights for no notice as such: `notices` is no task, on the
    server or on any element of the page."""
    src = SHELL.read_text(encoding="utf-8")
    assert not re.search(r'data-guide="[^"]*\bnotices\b', src)
    assert "guidance/notices/seen" not in src and "markSeen" not in src
    settings = (BACKEND / "routers" / "settings.py").read_text(encoding="utf-8")
    assert 'tasks.append("notices")' not in settings
    assert re.search(r'data-go="more/profile" data-guide="[^"]*\bupdate\b', src)
    assert re.search(r'data-go="more/sync" data-guide="support_request"', src)
    assert 'data-action="host-update" data-guide="update"' in src
    assert 'data-action="review-support" data-guide="support_request"' in src


def test_the_other_surfaces_use_the_channel_the_design_gives():
    """§4's deviations (NOTICES-HOME-1): what happened is a toast, a form's
    result stays under its button, a state is the screen's own — each site
    put back the old way fails here."""
    src = SHELL.read_text(encoding="utf-8")
    for title in ("'Nothing playing'", "title: 'Done'", "title: 'Add device'", "'Scan not started', kind: 'error',\n          message",
                  "title: 'MusicBrainz search', kind: 'info',\n                message",
                  "title: 'Could not send verification code'", "title: 'Could not delete'"):
        assert title not in src, title
    dialogs = re.findall(r"notifyDialog\(\{\s*title: '([^']+)'", src)
    assert set(dialogs) == {"Your new key is being made", "Restart the backend", "Restart Sautium",
                            "Restart Sautium to finish", "This password can be found"}, dialogs
    _cc = re.search(r"const _ccError = async \(title, r, err\) => \{\n(.*?)\n    \};", src, re.S).group(1)
    assert "notices.toast" in _cc and "notifyDialog" not in _cc
    hint = re.search(r"\n    function showHint\(text\) \{\n.*?\n    \}\n", src, re.S).group(0)
    assert "setTimeout" not in hint
    assert "Phase 2" not in src and "Your audio chain is not shared." in src
    assert "notices.toast({ key: 'hqp-forget', text" not in src
