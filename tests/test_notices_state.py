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
        "key": "hqplayer.rate_check", "kind": "warning", "since": ONSET, "until": None,
        "data": held["data"], "seen": False}
    assert served(types.SimpleNamespace(id="hqplayer", rate_notice=lambda: None)) is None
    assert served(types.SimpleNamespace(id="local")) is None
    assert served(None) is None


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
