"""Why nothing connects while the node has no identity it can use
(PI-NODE-2B Q21–Q22): app-shell.js's own `nodeIdentity`, `identityLabel` and
`identityDetail`, run as written under Node — the line Friends and every chat
header carry, by the worker's state; the two toasts (the identity ready, the
first message written while it is not); the Identity row's states. And the
state the page is sent, on a real PostgreSQL (skipped without a cluster)."""

import json
import shutil
import subprocess
import sys

import pytest

from tests.conftest import REPO_ROOT

if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

NODE = shutil.which("node")
SOURCE = (REPO_ROOT / "backend" / "static" / "app-shell.js").read_text(encoding="utf-8")


def _block(head: str, end: str = "\n  }\n") -> str:
    start = SOURCE.index(f"\n  {head}") + 1
    return SOURCE[start:SOURCE.index(end, start) + len(end)]


DRIVER = r"""
'use strict';
const toasts = [];
const notices = { toast: t => toasts.push(t.key + ': ' + t.title + ' — ' + t.text) };
const fmtNum = n => String(n);
%(fns)s
const out = { lines: {}, offers: {}, labels: {}, details: {}, emails: {} };
for (const [name, st] of Object.entries(%(states)s)) {
  nodeIdentity.state = null;
  nodeIdentity.apply(st);
  out.lines[name] = nodeIdentity.line();
  out.offers[name] = nodeIdentity.offersEmail();
  out.labels[name] = identityLabel(st);
  out.details[name] = identityDetail(st);
  out.emails[name] = [emailLineWords(true), emailLineWords(false)];
}
// The events, in the order a tab sees them
toasts.length = 0;
nodeIdentity.state = null;
nodeIdentity._saidWaiting = false;
nodeIdentity.apply({status: 'ready', detail: 'proof', initial: true});      // connect: state, no news
nodeIdentity.wroteWhileWaiting();
nodeIdentity.apply({status: 'mining', p_done: 0.1});
nodeIdentity.wroteWhileWaiting();
nodeIdentity.wroteWhileWaiting();                                          // once a waiting period
nodeIdentity.apply({status: 'paused', detail: 'memory'});
nodeIdentity.apply({status: 'ready', detail: 'proof'});
nodeIdentity.apply({status: 'ready', detail: 'proof'});                    // already ready: said once
nodeIdentity.apply({status: 'needs_email'});
nodeIdentity.wroteWhileWaiting();
nodeIdentity.apply({status: 'ready', detail: 'email'});
// A node Support knows: what is written to Support goes at once, to a friend it waits
nodeIdentity.apply({status: 'needs_email', support_known: true});
nodeIdentity.wroteWhileWaiting(true);
out.toasts_support = toasts.length;
nodeIdentity.wroteWhileWaiting(false);
out.toasts = toasts;
process.stdout.write(JSON.stringify(out));
"""

STATES = {
    "none": {"status": None},
    "ready": {"status": "ready", "detail": "email"},
    "checking": {"status": "checking"},
    "mining": {"status": "mining", "p_done": 0.4321, "attempts": 18, "email_verified": False},
    "mining_verified": {"status": "mining", "p_done": 0.1, "attempts": 3, "email_verified": True},
    "paused_memory": {"status": "paused", "detail": "memory", "email_verified": False},
    "paused_playback": {"status": "paused", "detail": "playback", "email_verified": True},
    "needs_email": {"status": "needs_email", "email_verified": False},
    "needs_email_support": {"status": "needs_email", "email_verified": False, "support_known": True},
    "mining_support": {"status": "mining", "p_done": 0.1, "email_verified": False, "support_known": True},
    "stopped": {"status": "stopped"},
}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_line_the_toasts_and_the_identity_rows_states():
    fns = "\n".join([_block("const nodeIdentity = {", "\n  };\n"),
                     _block("function identityLabel(st) {"), _block("function identityDetail(st) {"),
                     _block("function emailLineWords(nano) {")])
    proc = subprocess.run([NODE, "-e", DRIVER % {"fns": fns, "states": json.dumps(STATES)}],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    lines = out["lines"]
    # Nothing to say with no worker, at `ready`, or in the seconds of a proof's check
    assert [lines[k] for k in ("none", "ready", "checking", "stopped")] == ["", "", "", ""]
    assert lines["mining"].startswith("Your node is proving it is a real new node: a one-time calculation")
    assert "So far: 43% of the usual odds. Or verify your email — either is enough." in lines["mining"]
    assert lines["mining_verified"].endswith("So far: 10% of the usual odds.")
    assert "paused: not enough free memory. It goes on by itself." in lines["paused_memory"]
    assert lines["paused_memory"].endswith("Or verify your email — either is enough.")
    assert "paused: music is playing." in lines["paused_playback"]
    assert "verify your email" not in lines["paused_playback"]
    assert lines["needs_email"] == ("This node has no network identity yet. Verify your email to "
                                    "connect with friends and Support.")
    # PI-NODE-2C §10: Support met this account on another machine — the
    # state is the same, the words say what the node reaches (§18.3's drafts)
    known = ("This node has no network identity of its own yet. Support answers, because it met this "
             "account on another machine. Other Sautium nodes will not know this one until you verify "
             "your email (More › Profile).")
    assert lines["needs_email_support"] == known
    assert out["details"]["needs_email_support"] == known
    assert out["labels"]["needs_email_support"] == "needs a verified email · Support knows this account"
    assert out["emails"]["needs_email_support"] == [
        "Not verified. Support works; connections from outside your network and invites by email "
        "wait for a verified email."] * 2
    assert out["emails"]["needs_email"] == [
        "Not verified. This node has no network identity yet: no Support, no connections from "
        "outside your network, no invites by email.",
        "Not verified. Needed to invite friends by email."]
    # … and only of a node that waits for the email: a mining one says its own line
    assert lines["mining_support"].startswith("Your node is proving it is a real new node")
    assert out["emails"]["mining_support"] == out["emails"]["needs_email"]
    assert out["offers"] == {"none": False, "ready": False, "checking": False, "mining": True,
                             "mining_verified": False, "paused_memory": True,
                             "paused_playback": False, "needs_email": True,
                             "needs_email_support": True, "mining_support": True, "stopped": False}
    assert out["labels"]["needs_email"] == "needs a verified email"
    assert out["labels"]["paused_memory"] == "paused · not enough free memory — or verify your email"
    assert out["labels"]["paused_playback"] == "paused · while music plays"     # was "low memory"
    assert out["labels"]["ready"] == "email-verified"
    assert "More › Profile" in out["details"]["needs_email"]
    assert out["toasts"] == [
        "identity.waiting: Saved — It will be sent once your node's identity is ready.",
        "identity.ready: Your node is ready — Friends and Support can connect now.",
        "identity.waiting: Saved — It will be sent once your node's identity is ready.",
        "identity.ready: Your node is ready — Friends and Support can connect now.",
        "identity.waiting: Saved — It will be sent once your node's identity is ready."]
    assert out["toasts_support"] == 4                # the message to Support said nothing


def test_the_page_is_sent_the_workers_state_and_the_emails(scratch_dsn, monkeypatch):
    psycopg2 = pytest.importorskip("psycopg2")
    import psycopg2.pool
    import db_pool
    settings_router = pytest.importorskip("routers.settings")
    pool = psycopg2.pool.ThreadedConnectionPool(1, 2, dsn=scratch_dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_settings")
            assert settings_router.identity_event() == {
                "status": None, "detail": None, "attempts": None, "p_done": None,
                "email_verified": False, "support_known": False}
            cur.execute("INSERT INTO user_settings (key, value) VALUES ('p2p.identity', %s::jsonb)",
                        (json.dumps({"status": "mining", "detail": None, "attempts": 7,
                                     "p_done": 0.2, "difficulty": 32, "method": "pow"}),))
            cur.execute("""INSERT INTO user_profile (id, email_verified) VALUES (1, TRUE)
                           ON CONFLICT (id) DO UPDATE SET email_verified = TRUE""")
            assert settings_router.identity_event() == {
                "status": "mining", "detail": None, "attempts": 7, "p_done": 0.2,
                "email_verified": True, "support_known": False}
            cur.execute("UPDATE user_profile SET email_verified = FALSE WHERE id = 1")
    finally:
        conn.close()
        pool.closeall()

    # A change of the worker's state wakes the page's stream
    import asyncio
    woken = []

    class Loop:
        def call_soon_threadsafe(self, fn):
            woken.append(fn)
    evt, loop = asyncio.Event(), Loop()
    settings_router.identity_sse_register(evt, loop)
    try:
        note = type("Note", (), {"channel": "sautium_identity"})()
        monkeypatch.setattr(settings_router, "notify_library_subscribers", lambda: None)
        settings_router._on_sync_notify([note])
        assert woken == [evt.set]
    finally:
        settings_router.identity_sse_unregister(evt, loop)
