"""The slice families in the active-conditions set
(backend/routers/settings._notices_state): a deferral is its cycle's status
row, dated by the onset the row keeps, and there is none on a node that
holds that family's dump, whatever the row still says.

On a real PostgreSQL — a throwaway database built from the migrations, the
connection pool pointed at it. Skipped where there is no cluster. The tab's
side — an onset toasted once — runs app-shell.js's `notices` under Node,
strict as the page is; skipped where Node is not, and with this module
where psycopg2 or the settings router is not.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
settings_router = pytest.importorskip("routers.settings")

PG = dict(host=os.environ.get("SAUTIUM_TEST_PGHOST", "postgres"),
          port=int(os.environ.get("SAUTIUM_TEST_PGPORT", "5432")),
          user=os.environ.get("SAUTIUM_TEST_PGUSER", "musicai"),
          password=os.environ.get("SAUTIUM_TEST_PGPASSWORD", "supervisor"))
DBNAME = "sautium_notices_state_test"
ONSET = "2026-10-07T11:00:00+00:00"


@pytest.fixture(scope="module")
def dsn():
    try:
        admin = psycopg2.connect(dbname="postgres", **PG)
    except psycopg2.OperationalError as e:
        pytest.skip(f"no PostgreSQL for the notices test: {e}")
    admin.autocommit = True
    from desktop import db_init, node_backup as nb
    nb._drop_database(admin, DBNAME)
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {DBNAME}")
    conn = psycopg2.connect(dbname=DBNAME, **PG)
    db_init.apply_migrations(conn)
    conn.commit()
    conn.close()
    yield f"postgresql://{PG['user']}:{PG['password']}@{PG['host']}:{PG['port']}/{DBNAME}"
    nb._drop_database(admin, DBNAME)
    admin.close()


@pytest.fixture
def cur(dsn, monkeypatch):
    import db_pool
    import psycopg2.pool
    pool = psycopg2.pool.ThreadedConnectionPool(1, 2, dsn=dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute("DELETE FROM user_settings")
        yield c
    conn.close()
    pool.closeall()


def _setting(cur, key, value):
    cur.execute("INSERT INTO user_settings (key, value) VALUES (%s, %s::jsonb)",
                (key, json.dumps(value)))


def _status(cur, family, **facts):
    _setting(cur, f"{family}.status", {
        "pending": 200, "pending_capped": True, "served": 0, "unserved": 200,
        "reason": "no_sources", "sources": 0, "next_attempt_at": None,
        "since": ONSET, **facts})


def _deferred() -> dict:
    return {n["key"]: n for n in settings_router._notices_state()["items"]
            if n["key"].endswith(".deferred")}


def test_a_deferral_is_dated_by_its_onset(cur):
    _status(cur, "lb_slice")
    _status(cur, "mb_slice", served=200, unserved=0, reason="ok", since=None)
    items = _deferred()
    assert list(items) == ["lb_slice.deferred"]
    assert items["lb_slice.deferred"]["since"] == ONSET


def test_a_dump_node_has_no_deferral_for_its_family(cur):
    # The MB cycle's last run published "no source" while the load landed.
    _status(cur, "mb_slice")
    _status(cur, "lb_slice")
    _setting(cur, "musicbrainz.db_version", "20261007-002147")
    assert list(_deferred()) == ["lb_slice.deferred"]


def test_the_rate_check_notice_is_the_hqplayer_backends_own(cur, monkeypatch):
    """A queue that meets a rate HQPlayer cannot reach: the notice the
    HQPlayer backend holds (its rate_notice), served as it gives it — the
    module's phrases in `data` — and none while another output plays."""
    import types
    from playback import manager as manager_mod
    held = {"since": ONSET, "data": {"title": [{"t": "Some tracks in the queue cannot play at 705.6 kHz"}],
                                     "text": [[{"t": "HQPlayer describes demo-sinc-a as 2^x up: …"}]]}}

    def served(active):
        monkeypatch.setattr(manager_mod, "manager", types.SimpleNamespace(active=active))
        return {i["key"]: i for i in settings_router._notice_items()}.get("hqplayer.rate_check")

    assert served(types.SimpleNamespace(id="hqplayer", rate_notice=lambda: held)) == {
        "key": "hqplayer.rate_check", "kind": "warning", "domain": "hqplayer", "since": ONSET, "until": None,
        "data": held["data"], "seen": False}
    assert served(types.SimpleNamespace(id="hqplayer", rate_notice=lambda: None)) is None
    assert served(types.SimpleNamespace(id="local")) is None
    assert served(None) is None


def test_an_fpcalc_that_fails_the_self_test_is_a_notice_and_a_missing_one_is_tools_missing(
        cur, monkeypatch):
    import provenance

    def served(verdict, on_path):
        monkeypatch.setattr(provenance, "fingerprinter", lambda: verdict)
        monkeypatch.setattr(settings_router.shutil, "which",
                            lambda tool: f"/usr/bin/{tool}" if tool in on_path else None)
        items = {i["key"]: i for i in settings_router._notice_items()}
        return items.get("tools.fingerprint"), items.get("tools.missing")

    failed = provenance.Fingerprinter("failed", "/usr/bin/fpcalc", "exit 3: ERROR: Error decoding audio frame")
    notice, missing = served(failed, ("ffmpeg", "fpcalc"))
    assert missing is None and (notice["kind"], notice["until"], notice["data"]) == (
        "warning", None, {"state": "failed", "detail": "exit 3: ERROR: Error decoding audio frame",
                          "path": "/usr/bin/fpcalc"})
    mismatch = provenance.Fingerprinter("mismatch", "/usr/bin/fpcalc", "another fingerprint than the release's")
    assert served(mismatch, ("ffmpeg", "fpcalc"))[0]["since"] == notice["since"]   # one condition
    notice, missing = served(provenance.Fingerprinter("missing", None, "not on PATH"), ("ffmpeg",))
    assert notice is None and missing["data"] == {"tools": ["fpcalc"]}
    assert served(provenance.Fingerprinter("ok", "/usr/bin/fpcalc"), ("ffmpeg", "fpcalc")) == (None, None)


_TABS = r"""
const fs = require('node:fs'), vm = require('node:vm');
const src = fs.readFileSync(process.argv[process.argv.length - 1], 'utf8');
const literal = src.slice(src.indexOf('  const notices = {'), src.indexOf('  window.notices = notices;'));
function storage(kind) {
  const junk = { number: '5', array: '["x"]' }[kind];
  const kept = new Map(junk ? [['sautiumNoticeOnsets', junk]] : []);
  return kind === 'refused'
    ? { getItem() { throw new Error('refused'); }, setItem() { throw new Error('refused'); } }
    : { getItem: (k) => (kept.has(k) ? kept.get(k) : null), setItem: (k, v) => kept.set(k, String(v)) };
}
function tab(sessionStorage) {
  const toasted = [];
  const context = { sessionStorage, window: { dispatchEvent() {} }, CustomEvent: class {},
                    noticeCopy: () => ({ title: '', text: '' }) };
  // strict, as the page's own code is
  vm.runInNewContext(`'use strict';\n${literal}\nthis.notices = notices;`, context);
  context.notices.toast = (t) => toasted.push(t.key);
  return { apply: (items, initial) => context.notices.apply({ items, initial }), toasted };
}
const out = {};
for (const kind of ['kept', 'refused', 'number', 'array']) {
  const kept = storage(kind), rate = { key: 'hqplayer.rate_check', since: '2001-05-06T09:00:00+00:00' };
  const first = tab(kept);
  first.apply([], true);
  first.apply([rate]);                              // the onset
  first.apply([]);                                  // a gap: an advance's tail
  first.apply([rate]);                              // back with the same onset
  const reloaded = tab(kept);                       // the tab reloaded
  reloaded.apply([], true);
  reloaded.apply([rate]);
  reloaded.apply([{ ...rate, since: '2001-05-06T09:30:00+00:00' }]);   // a new onset
  for (const items of [[], [{ key: 'x', since: null }], [], [{ key: 'x', since: null }]]) reloaded.apply(items);
  // a tab that connects while an onset already stands: painted, never toasted, a gap included
  const late = tab(storage(kind));
  late.apply([rate], true);
  late.apply([]);
  late.apply([rate]);
  late.apply([{ ...rate, since: '2001-05-06T10:00:00+00:00' }]);       // a new onset
  out[kind] = { first: first.toasted, reloaded: reloaded.toasted, late: late.toasted };
}
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_tab_toasts_an_onset_once_through_a_gap_and_a_reload():
    """A notice that goes and comes back with the same onset — an advance's
    tail, a Stop — toasts once in a tab, and once only across a reload while
    sessionStorage keeps the onsets; refused, memory keeps them for the page,
    and junk there (a number, an array) starts afresh. An onset the
    connect-time snapshot carried never toasts, a gap included; one the tab
    first meets after it toasts once, however old. A new onset toasts; a
    notice without an onset, whenever it appears."""
    proc = subprocess.run([shutil.which("node"), "-e", _TABS, "--", str(BACKEND / "static" / "app-shell.js")],
                          capture_output=True, check=True)
    once = ["hqplayer.rate_check"]
    assert json.loads(proc.stdout) == {
        "kept": {"first": once, "reloaded": ["hqplayer.rate_check", "x", "x"], "late": once},
        "refused": {"first": once, "reloaded": ["hqplayer.rate_check", "hqplayer.rate_check", "x", "x"],
                    "late": once},
        "number": {"first": once, "reloaded": ["hqplayer.rate_check", "x", "x"], "late": once},
        "array": {"first": once, "reloaded": ["hqplayer.rate_check", "x", "x"], "late": once}}, proc.stdout


_ROWS = r"""
const notices = { list: () => [{ key: 'hqplayer.rate_check', kind: 'warning', domain: 'network' },
                               { key: 'x"<y', kind: 'info', domain: 'network' },
                               { key: 'cooldown.lastfm', kind: 'info', domain: 'network' },
                               { key: 'both', kind: 'error', domain: 'network' }] };
function noticeCopy(n) {
  if (n.key === 'cooldown.lastfm') return { title: 'Last.fm paused', text: 'Rate-limited' };
  const copy = { title: 'A title', text: 'Its text' };
  if (n.key !== 'x"<y') copy.why = () => {};
  if (n.key !== 'hqplayer.rate_check') copy.action = { label: n.key === 'both' ? 'Settings' : 'Go <on>', run() {} };
  return copy;
}
process.stdout.write(_renderNoticeRows());
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_notice_row_draws_its_copys_action_as_a_button():
    """A copy's buttons are its row's too, as on its toast and in its order
    — Why?, then the one action under the toast's label — beside the title,
    keyed by their notice, both escaped. A copy without one draws none.
    app-shell.js's own functions, run as written."""
    src = (BACKEND / "static" / "app-shell.js").read_text(encoding="utf-8")
    fns = [re.search(rf"\n  function {name}\(.*?\) \{{\n.*?\n  \}}\n", src, re.S).group(0)
           for name in ("escapeHtml", "_renderNoticeRows")]
    out = subprocess.run([shutil.which("node"), "-e", "'use strict';" + "".join(fns) + _ROWS],
                         capture_output=True, text=True, check=True).stdout
    button = '<button class="btn-link" type="button" data-notice-action="%s" data-which="%s">%s</button>'
    assert re.findall(r"<button[^>]*>[^<]*</button>", out) == [
        button % ("hqplayer.rate_check", "why", "Why?"), button % ("x&quot;&lt;y", "action", "Go &lt;on&gt;"),
        button % ("both", "why", "Why?"), button % ("both", "action", "Settings")]
    assert out.count('class="form-row stacked notice-row') == 4 and out.count("<span></span>") == 1


_ACT = r"""
const fs = require('node:fs'), vm = require('node:vm');
const src = fs.readFileSync(process.argv[process.argv.length - 1], 'utf8');
const literal = src.slice(src.indexOf('  const notices = {'), src.indexOf('  window.notices = notices;'));
const ran = [];
const toast = key => ({ key, timer: 0, el: { classList: { remove() {} }, remove() {} } });
const context = { window: { dispatchEvent() {} }, CustomEvent: class {}, setTimeout: () => 0, clearTimeout() {},
                  modalIsOpen: () => false, layoutMode: () => 'compact',
                  noticeCopy: n => ({ title: '', text: '', why: () => ran.push('why ' + n.key),
                                      action: { label: 'Settings', run: () => ran.push(n.key) } }) };
vm.runInNewContext(`'use strict';\n${literal}\nthis.notices = notices;`, context);
const n = context.notices;
n.items = new Map(['shown', 'queued', 'other'].map(key => [key, { key }]));
n._shown = [toast('shown'), toast('other')];
n._queue = [toast('queued'), toast('waiting'), toast('asked')];
n.items.set('asked', { key: 'asked' });
n.act('shown');
n.act('queued', 'action');
n.act('asked', 'why');
process.stdout.write(JSON.stringify({ ran, shown: n._shown.map(t => t.key), queued: n._queue.map(t => t.key) }));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_rows_button_does_what_the_toasts_does():
    """A notice's button pressed on its row closes the notice's toast, shown
    or still queued, as the toast's own button closes it, and runs the
    copy's action — or its Why?, for that button; another notice's toasts
    stay."""
    proc = subprocess.run([shutil.which("node"), "-e", _ACT, "--", str(BACKEND / "static" / "app-shell.js")],
                          capture_output=True, check=True)
    assert json.loads(proc.stdout) == {"ran": ["shown", "queued", "why asked"], "shown": ["other"],
                                       "queued": ["waiting"]}


# ---- a provider that stepped aside (streaming/base.py SteppedAside) --------

def _providers(monkeypatch):
    """A lossless provider with a live state and the excerpt provider of the
    same catalog: one health key, which is also the first one's id."""
    from streaming import service
    from streaming.base import ProviderManifest, ProviderRegistry, StreamProvider

    class _P(StreamProvider):
        def __init__(self, pid, **flags):
            self.manifest = ProviderManifest(id=pid, name="Lossless-X", kind="direct_url",
                                             cooldown_source="cat", **flags)
            self.aside, self.checks, self.renewed = None, 0, False

        def fetch(self, query):
            raise NotImplementedError

        def stepped_aside(self):
            return self.aside

        def check_again(self):
            self.checks += 1
            if self.renewed:
                self.aside = None

    registry = ProviderRegistry()
    lossless, clip = _P("cat", lossless=True), _P("cat_clip", lossless=False, excerpt=True)
    registry.register(lossless)
    registry.register(clip)
    monkeypatch.setattr(service, "_registry", registry)
    monkeypatch.setattr(service, "provider_health", {})
    return service, lossless, clip


def _aside(**over):
    from streaming.base import SteppedAside
    return SteppedAside(**{"code": "no_tier", "since": ONSET,
                           "title": "Lossless-X: this account no longer has the tier",
                           "text": "Tracks stream from another provider.", "detail": "", **over})


def _streaming_items():
    return {n["key"]: n for n in settings_router._notice_items() if n["key"].startswith("streaming.")}


def test_a_provider_that_stepped_aside_is_a_notice_in_its_own_voice(cur, monkeypatch):
    """Its state as it stands, the module's words, under the provider's own
    id — and the catalog answering, which clears the shared upstream's
    silence under the same name, leaves it."""
    service, lossless, clip = _providers(monkeypatch)
    assert _streaming_items() == {}
    lossless.aside = _aside(detail="the tier ended")
    service.note_provider_health({"cat": "quota"}, set())
    items = _streaming_items()
    assert sorted(items) == ["streaming.aside.cat", "streaming.silent.cat"]
    assert items["streaming.aside.cat"] == {
        "key": "streaming.aside.cat", "kind": "warning", "domain": "streaming", "since": ONSET, "until": None,
        "seen": False,
        "data": {"provider": "cat", "name": "Lossless-X", "code": "no_tier",
                 "title": "Lossless-X: this account no longer has the tier",
                 "text": "Tracks stream from another provider.", "detail": "the tier ended"}}
    assert service.note_provider_health({}, {"cat"}) is True          # the catalog answers
    assert sorted(_streaming_items()) == ["streaming.aside.cat"]
    # its own id, where that is not the upstream's name either
    clip.aside = _aside()
    assert sorted(_streaming_items()) == ["streaming.aside.cat", "streaming.aside.cat_clip"]
    assert _streaming_items()["streaming.aside.cat_clip"]["data"]["provider"] == "cat_clip"
    lossless.aside = clip.aside = None
    assert _streaming_items() == {}


def test_a_providers_change_wakes_the_notices_channel(cur, dsn, monkeypatch):
    """The provider's own word (aside_changed) is a NOTIFY on the channel
    every open tab's snapshot waits on — either way, through the listener
    the service registered when it was imported."""
    import select
    _, lossless, _clip = _providers(monkeypatch)
    listener = psycopg2.connect(dsn)
    listener.autocommit = True
    try:
        with listener.cursor() as c:
            c.execute("LISTEN sautium_notices")
        for state in (_aside(), None):
            lossless.aside = state
            lossless.aside_changed()
            assert select.select([listener], [], [], 10)[0], "no wake"
            listener.poll()
            assert [n.channel for n in listener.notifies] == ["sautium_notices"]
            listener.notifies.clear()
    finally:
        listener.close()


def test_check_again_has_the_provider_read_its_state_and_answers_the_tap(cur, monkeypatch):
    from fastapi import HTTPException
    _, lossless, _clip = _providers(monkeypatch)
    lossless.aside = _aside()
    answer = settings_router.post_stream_provider_check("cat")
    assert lossless.checks == 1
    assert answer == {"provider": "cat", "name": "Lossless-X", "aside": {
        "code": "no_tier", "since": ONSET, "title": "Lossless-X: this account no longer has the tier",
        "text": "Tracks stream from another provider.", "detail": ""}}
    lossless.renewed = True
    assert settings_router.post_stream_provider_check("cat") == {
        "provider": "cat", "name": "Lossless-X", "aside": None}
    with pytest.raises(HTTPException) as refused:
        settings_router.post_stream_provider_check("nobody")
    assert refused.value.status_code == 404


_ASIDE_COPY = r"""
const toasts = [], asked = [];
let answer, release;
const notices = { toast: t => toasts.push({ ...t, action: t.action ? t.action.label : null }) };
async function fetch(url, init) {
  asked.push([url, init.method]);
  if (answer === 'held') await new Promise(done => { release = done; });
  if (answer === 'down') return { ok: false, status: 502 };
  if (answer === 'thrown') throw new Error('no <route>');
  return { ok: true, json: async () => answer };
}
(async () => {
  const data = { provider: 'cat/x', name: 'Lossless-X', title: 'Lossless-X: no <tier>', text: 'Tracks & more.',
                 detail: 'answer "0"' };
  const copy = asideNoticeCopy({ key: 'streaming.aside.cat/x', data });
  const viaNotice = noticeCopy({ key: 'streaming.aside.cat/x', data });
  answer = { provider: 'cat/x', name: 'Lossless-X', aside: { title: 'Lossless-X: no <tier>', text: 'Still.', detail: '' } };
  await copy.action.run();
  answer = { provider: 'cat/x', name: 'Lossless <X>', aside: null };
  await copy.action.run();
  answer = 'down';
  await copy.action.run();
  answer = 'thrown';
  await copy.action.run();
  const sequence = toasts.splice(0);
  // three taps while one check is in flight: one request, "Checking…" said at each tap;
  // another provider's check is its own
  const before = asked.length;
  answer = 'held';
  const taps = [copy.action.run(), copy.action.run(), copy.action.run()];
  await Promise.resolve();
  const inFlight = asked.length - before;
  const releaseFirst = release;
  const other = checkProviderAgain('other');
  await Promise.resolve();
  const withOther = asked.slice(before).map(a => a[0]);
  const releaseOther = release;
  answer = { provider: 'cat/x', name: 'Lossless-X', aside: null };
  releaseFirst(); releaseOther();
  await Promise.all([...taps, other]);
  const flight = toasts.splice(0).map(t => [t.title, t.key]);
  await copy.action.run();                      // the flight is over: a tap asks again
  process.stdout.write(JSON.stringify({ title: copy.title, text: copy.text, label: copy.action.label,
    same: viaNotice.title === copy.title && viaNotice.text === copy.text && viaNotice.action.label === 'Check again',
    bare: asideNoticeCopy({ key: 'streaming.aside.q', data: { provider: 'q', title: 'T', text: 'x' } }).text,
    asked: asked.slice(0, before), inFlight, withOther, flight, after: asked.length - before, sequence }));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_aside_notice_draws_the_modules_words_and_check_again_answers_the_tap():
    """The module's title and text as escaped text, its service's answer in
    brackets; Check again posts to the provider's check and the tap hears
    the answer: still aside (the module's words), back, or that the check
    itself failed — every toast of a check under the notice's key, each
    replacing the one before. Taps while a check is in flight join it, each
    saying "Checking…" again; another provider's check is its own.
    app-shell.js's own functions."""
    src = (BACKEND / "static" / "app-shell.js").read_text(encoding="utf-8")
    fns = [re.search(rf"\n  (?:async )?function {name}\(.*?\) \{{\n.*?\n  \}}\n", src, re.S).group(0)
           for name in ("escapeHtml", "asideNoticeCopy", "checkProviderAgain", "_checkProvider", "noticeCopy")]
    out = subprocess.run([shutil.which("node"), "-e",
                          "'use strict';const NOTICE_COPY = {};const _providerChecks = new Map();"
                          + "".join(fns) + _ASIDE_COPY],
                         capture_output=True, text=True, check=True).stdout
    url, key = "/api/settings/streaming/providers/cat%2Fx/check", "streaming.aside.cat/x"
    # no lifetime of its own: it must not hold the phone's one toast lane for the flight
    checking = {"kind": "info", "title": "Checking the provider…", "text": "", "action": None, "key": key}
    assert json.loads(out) == {
        "title": "Lossless-X: no &lt;tier&gt;", "text": "Tracks &amp; more. (answer &quot;0&quot;)",
        "label": "Check again", "same": True, "bare": "x",
        "asked": [[url, "POST"]] * 4, "inFlight": 1, "after": 3,
        "withOther": [url, "/api/settings/streaming/providers/other/check"],
        "flight": [["Checking the provider…", key]] * 3 + [
            ["Checking the provider…", "streaming.aside.other"],
            ["Lossless-X is back", key], ["Lossless-X is back", "streaming.aside.other"]],
        "sequence": [
            checking,
            {"kind": "warning", "title": "Lossless-X: no &lt;tier&gt;", "text": "Still.",
             "action": "Check again", "key": key},
            checking,
            {"kind": "success", "title": "Lossless &lt;X&gt; is back",
             "text": "Streamed tracks come from it again.", "action": None, "key": key},
            checking,
            {"kind": "error", "title": "The provider could not be checked", "text": "HTTP 502",
             "action": None, "key": key},
            checking,
            {"kind": "error", "title": "The provider could not be checked", "text": "no &lt;route&gt;",
             "action": None, "key": key}]}, out


def test_a_report_is_seen_at_its_toast_and_a_state_never_is(cur, monkeypatch):
    """A report's seen keeps that onset (the channel drops it everywhere, a
    later onset is news); two reports seen one after the other are both kept
    — one statement merges the map; a state is refused: it ends with its
    source, never by being looked at. The pairing codes' record starts again."""
    from fastapi import HTTPException
    from device_auth import PIN_FAILURES_KEY
    import device_auth
    woken, restarted = [], []
    monkeypatch.setattr(device_auth, "pin_failures_seen", lambda: restarted.append(1))
    _setting(cur, "auth.login_failures", {"count": 2, "since": ONSET, "last": ONSET})
    _setting(cur, PIN_FAILURES_KEY, {"count": 1, "since": ONSET, "last": ONSET})

    def served():
        return {n["key"]: n for n in settings_router._notices_state()["items"] if n["key"].startswith("auth.")}
    assert {k: (n["report"], n["seen"], n["domain"]) for k, n in served().items()} == {
        "auth.login_failures": (True, False, "account"), PIN_FAILURES_KEY: (True, False, "account")}
    seen = settings_router.NoticeSeen(since=ONSET)
    settings_router.post_notice_seen("auth.login_failures", seen)
    assert list(served()) == [PIN_FAILURES_KEY] and restarted == []
    settings_router.post_notice_seen(PIN_FAILURES_KEY, seen)
    assert served() == {} and restarted == [1]
    settings_router.post_notice_seen(PIN_FAILURES_KEY, seen)          # again: nothing moves
    assert restarted == [1]
    cur.execute("SELECT value FROM user_settings WHERE key = 'notice.seen'")
    assert cur.fetchone()[0] == {"auth.login_failures": ONSET, PIN_FAILURES_KEY: ONSET}
    # a later failure is news again
    cur.execute("UPDATE user_settings SET value = jsonb_set(value, '{last}', '\"2026-10-08T00:00:00+00:00\"') "
                "WHERE key = 'auth.login_failures'")
    assert list(served()) == ["auth.login_failures"]
    with pytest.raises(HTTPException) as refused:
        settings_router.post_notice_seen("tools.missing", seen)
    assert refused.value.status_code == 400


def test_the_trail_lights_for_an_offered_release_and_a_waiting_request_alone(cur, monkeypatch):
    """No notice lights the trail as such (a network line, a folder out, a
    cooldown: nothing). A release offered lights `update` until the Profile's
    visit retires it — again for a newer release; a support request waiting
    lights `support_request` while it waits, whatever is visited."""
    import types
    # The trail's other tasks read the workers' state and the hardware: not this test's
    monkeypatch.setitem(sys.modules, "main", types.SimpleNamespace(
        _scan_state={"running": True}, _enrich_state={"running": True}))
    items = []
    monkeypatch.setattr(settings_router, "_notices_state", lambda: {"items": items})
    from routers import host as host_router
    offered = {"release": "v0.3.1", "since": ONSET}
    monkeypatch.setattr(host_router, "offered_release", lambda: offered)
    monkeypatch.setattr(settings_router, "notify_library_subscribers", lambda: None)

    def tasks():
        return [t for t in settings_router._guidance_state()["tasks"] if t in ("update", "support_request", "notices")]
    items[:] = [{"key": k, "domain": d, "seen": False, "since": ONSET, "data": {}}
                for k, d in (("mb_slice.deferred", "network"), ("library.folder_out.3", "library"),
                             ("cooldown.lastfm", "enrichment"))]
    assert tasks() == []
    items.append({"key": "update.available", "domain": "node", "seen": False, "since": ONSET,
                  "data": {"release": "v0.3.1"}})
    items.append({"key": "support.request.9", "domain": "support", "seen": False, "since": ONSET, "data": {}})
    assert tasks() == ["update", "support_request"]
    settings_router.post_guidance_seen("update")
    assert tasks() == ["support_request"]
    items[-2] = {**items[-2], "data": {"release": "v0.3.2"}}
    assert tasks() == ["update", "support_request"]
    items.pop()
    assert tasks() == ["update"]
    with pytest.raises(Exception):
        settings_router.post_guidance_seen("notices")


def test_a_condition_that_ends_at_its_time_wakes_the_channel_then(monkeypatch):
    """A cooldown or a support request ends at a time its source knows, and
    nothing writes then: the channel is woken once at the earliest such end
    the derivation served — a retry's `until` (the slices') wakes nothing."""
    import threading
    from datetime import datetime, timedelta, timezone
    woke = threading.Event()
    sent = []
    monkeypatch.setattr(settings_router, "db_execute", lambda sql, *a: sent.append(sql) or woke.set())
    soon = (datetime.now(timezone.utc) + timedelta(seconds=0.2)).isoformat()
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    monkeypatch.setattr(settings_router, "_notice_items", lambda: [
        {"key": "mb_slice.deferred", "until": soon, "seen": False},
        {"key": "cooldown.deezer", "until": later, "seen": False},
        {"key": "support.request.9", "until": later, "seen": False}])
    settings_router._notices_state()
    assert settings_router._end_timer["at"] == datetime.fromisoformat(later)
    assert not woke.wait(1.5)                       # the retry woke nothing
    soon = (datetime.now(timezone.utc) + timedelta(seconds=0.2)).isoformat()
    monkeypatch.setattr(settings_router, "_notice_items", lambda: [
        {"key": "cooldown.deezer", "until": soon, "seen": False}])
    settings_router._notices_state()
    assert woke.wait(5) and sent == ["NOTIFY sautium_notices"]
    monkeypatch.setattr(settings_router, "_notice_items", lambda: [])
    settings_router._notices_state()
    assert settings_router._end_timer == {"at": None, "timer": None}


def test_a_seen_is_kept_only_for_the_reports_onset_as_it_stands(cur):
    """A page that slept through a re-arm closes the older toast: nothing is
    kept, the newer onset stays unseen. Judged by equality, never by time
    order — an onset that moved earlier (a Pi's clock set back at its boot)
    is seen at its close all the same."""
    newer, older = "2026-10-08T00:00:00+00:00", ONSET
    _setting(cur, "auth.login_failures", {"count": 1, "since": newer, "last": newer})
    out = settings_router.post_notice_seen("auth.login_failures", settings_router.NoticeSeen(since=older))
    assert out["seen"] is False
    cur.execute("SELECT count(*) FROM user_settings WHERE key = 'notice.seen'")
    assert cur.fetchone()[0] == 0
    settings_router.post_notice_seen("auth.login_failures", settings_router.NoticeSeen(since=newer))
    # the onset moves earlier: its close is its seen
    cur.execute("UPDATE user_settings SET value = jsonb_set(value, '{last}', to_jsonb(%s::text)) "
                "WHERE key = 'auth.login_failures'", (older,))
    assert any(n["key"] == "auth.login_failures" for n in settings_router._notices_state()["items"])
    assert settings_router.post_notice_seen("auth.login_failures",
                                            settings_router.NoticeSeen(since=older))["seen"] is True
    assert not any(n["key"] == "auth.login_failures" for n in settings_router._notices_state()["items"])
