"""B-92's surface: backend/static/app-shell.js's own functions, run as written
under Node — the Support contact's line (the friend row and the chat header
say the same: what becomes of a message, never a presence — PI-NODE-2B Q21)
and a message's state under its bubble (every waiting and kept one,
`Delivered` under the newest delivered one only, nothing under a friend's);
and the API's words they render — `state` on an outgoing message, `reach` on
the Support contact's row only, true while this node can deliver or deposit
— on a real PostgreSQL (skipped without a cluster)."""

import asyncio
import json
import shutil
import subprocess
import sys
from urllib.parse import urlparse

import pytest

from tests.conftest import REPO_ROOT

if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

NODE = shutil.which("node")
SOURCE = (REPO_ROOT / "backend" / "static" / "app-shell.js").read_text(encoding="utf-8")


def _function(head: str) -> str:
    start = SOURCE.index(f"\n  function {head} {{\n") + 1
    return SOURCE[start:SOURCE.index("\n  }\n", start) + len("\n  }")]


def _const(name: str) -> str:
    start = SOURCE.index(f"\n  const {name} = ") + 1
    return SOURCE[start:SOURCE.index(";\n", start) + 1]


DRIVER = r"""
'use strict';
const escapeHtml = s => String(s);
const tsStampLabel = d => 'TS';
const avatarPlaceholder = n => ({bg: '#000', initials: 'X'});
const SVG_CHAT = '';
%(fns)s
const out = {
  rows: %(friends)s.map(f => friendRowInner(f).match(/friend-status[^>]*>(.*?)<\/div>/)[1]),
  header: %(friends)s.map(f => {
    const el = {style: {}, set textContent(t) { this.text = t; }, set innerHTML(h) { this.text = h; }};
    paintChatStatus(el, f);
    return el.text.replace(/<[^>]*>/g, '');
  }),
  thread: [...renderThreadHTML(%(messages)s, %(friends)s[0]).matchAll(/<div class="(bubble [a-z]+|msg-state)">([^<]*)</g)]
            .map(m => m[1] === 'msg-state' ? '  ' + m[2] : m[2]),
  waits: %(waiting)s.map(f => [...renderThreadHTML(%(messages)s, f).matchAll(/<div class="msg-state">([^<]*)</g)]
            .map(m => m[1])),
  keys: %(friends)s.map(f => friendUrlKey(f)),
};
process.stdout.write(JSON.stringify(out));
"""

FRIENDS = [
    {"username": "Sautium", "source": "master", "reach": True, "last_seen": None, "chat_key": "ab" * 8},
    {"username": "Sautium", "source": "master", "reach": False,
     "last_seen": "2026-11-17T10:00:00+00:00"},
    {"username": "ada", "last_seen": None},
    {"username": "Sautium", "source": "master", "reach": True, "is_online": True},
    {"username": "bo", "last_seen": "2026-11-17T10:00:00+00:00"},
    {"username": "cy", "is_online": True},
    {"username": "Sautium", "source": "master", "reach": False, "support_wait": "handshake",
     "chat_key": "ab" * 8},
    {"username": "Sautium", "source": "master", "reach": False, "support_wait": "key"},
    {"username": "dee", "chat_key": ""},
]
NOT_YET = "Support isn't online yet — write now, and your message goes when it is"
NEW_KEY = "Support has not learned your new key yet — your message goes once it has"
KEPT = "Write any time — your message is kept until Support reads it"
WAITS = "Your message waits on this node and is sent once it can reach Support"
MESSAGES = [
    {"direction": "out", "content": "one", "state": "delivered"},
    {"direction": "in", "content": "hi", "state": None},
    {"direction": "out", "content": "two", "state": "delivered"},
    {"direction": "out", "content": "three", "state": "kept"},
    {"direction": "out", "content": "four", "state": "waiting"},
]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_support_line_and_each_messages_state_are_what_the_server_says(monkeypatch):
    fns = "\n".join([
        "function lastSeenLabel(iso) { return iso ? '3h ago' : ''; }",
        _function("offlineLabel(friend)"), _function("supportLine(friend)"),
        _function("paintChatStatus(statusEl, friend)"),
        _function("friendRowInner(friend)"),
        _function("friendUrlKey(friend)"),
        _const("MESSAGE_STATE_LINE"), _const("SUPPORT_WAIT_LINE"),
        _function("renderThreadHTML(messages, friend)")])
    driver = DRIVER % {"fns": fns, "friends": json.dumps(FRIENDS), "messages": json.dumps(MESSAGES),
                       "waiting": json.dumps(FRIENDS[6:8])}
    proc = subprocess.run([NODE, "-e", driver], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    # Support never reads "offline", nor "online": a friend does
    assert out["rows"][:5] == [KEPT, WAITS, "offline", KEPT, "last seen 3h ago"]
    assert out["header"] == [KEPT, WAITS, "offline", KEPT, "last seen 3h ago", "online",
                             NOT_YET, NEW_KEY, "offline"]
    assert out["thread"] == ["one", "hi", "two", "  Delivered", "three", "  Kept for Support",
                             "four", "  Not sent yet"]
    # a message to a Support contact that has not answered yet says what it waits for
    assert out["waits"] == [["Delivered", "Kept for Support", "Waiting for Support"],
                            ["Delivered", "Kept for Support", "Waiting for Support to learn your new key"]]
    # the thread is reached by the key the server names — the Support stub's too (B-116)
    assert out["keys"] == ["ab" * 8, "", "", "", "", "", "ab" * 8, "", ""]


def test_the_api_names_each_outgoing_messages_state_and_supports_kept_mail(scratch_dsn, monkeypatch):
    psycopg2 = pytest.importorskip("psycopg2")
    import db_pool
    import master_node
    from config import settings
    from routers import p2p
    url = urlparse(scratch_dsn)
    for key, value in dict(postgres_host=url.hostname, postgres_port=url.port,
                           postgres_user=url.username, postgres_password=url.password,
                           postgres_db=url.path.lstrip("/")).items():
        monkeypatch.setattr(settings, key, value)
    db_pool.close_pool()
    import p2p_identity
    master = "ab" * 32
    monkeypatch.setattr(master_node, "MASTER_PUBKEY_HEX", master)
    monkeypatch.setattr(settings, "p2p_enabled", True)
    certified = {"yes": True}
    monkeypatch.setattr(p2p_identity, "served_certificate",
                        lambda s: {"pubkey": "x"} if certified["yes"] else None)
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE friends CASCADE")
            cur.execute("""INSERT INTO friends (username, public_key_hex, invite_code, source, last_seen)
                           VALUES ('Sautium', %s, 'Sautium#AAAA-BBBB-CCCC', 'master', NULL),
                                  ('ada', %s, 'ada#AAAA-BBBB-CCCD', 'manual', NULL),
                                  ('bo', 'pending:bo#AAAA-BBBB-CCCE', 'bo#AAAA-BBBB-CCCE', 'manual', NULL)
                           RETURNING id""", (master, "cd" * 32))
            support = cur.fetchone()[0]
            for content, delivered, parked in (("a", True, "0"), ("b", False, "0"), ("c", False, None),
                                               ("e", False, "31 days")):       # past the mailbox's keep
                cur.execute("""INSERT INTO p2p_messages (friend_id, direction, content, delivered, parked_at)
                               VALUES (%s, 'out', %s, %s, now() - %s::interval)""",
                            (support, content, delivered, parked))
            cur.execute("INSERT INTO p2p_messages (friend_id, direction, content) VALUES (%s, 'in', 'd')",
                        (support,))
        msgs = asyncio.run(p2p.get_messages(support))
        assert [(m["content"], m["state"]) for m in msgs] == [
            ("a", "delivered"), ("b", "kept"), ("c", "waiting"), ("e", "waiting"), ("d", None)]
        rows = asyncio.run(p2p.list_friends())
        flags = {r["username"]: r.get("reach") for r in rows["pinned"] + rows["items"]}
        assert flags == {"Sautium": True, "ada": None, "bo": None}
        keys = {r["username"]: r["chat_key"] for r in rows["items"]}
        assert keys == {"Sautium": master[:16], "ada": "cd" * 8, "bo": ""}
        assert {r["username"]: r.get("support_wait") for r in rows["items"]}["Sautium"] is None
        assert not any("bound_identity" in r or "key_fingerprint" in r for r in rows["items"])
        certified["yes"] = False                     # the Worker takes mail from no other key
        rows = asyncio.run(p2p.list_friends())
        assert {r["username"]: r.get("reach") for r in rows["items"]}["Sautium"] is False
        certified["yes"] = True
        with conn.cursor() as cur:
            cur.execute("UPDATE friends SET last_seen = now() WHERE id = %s", (support,))
        rows = asyncio.run(p2p.list_friends())
        assert {r["username"]: r.get("reach") for r in rows["items"]}["Sautium"] is True   # online or not
    finally:
        conn.close()
        db_pool.close_pool()


def test_the_support_stub_has_a_thread_and_says_what_it_waits_for(scratch_dsn, monkeypatch):
    """B-116: the Support contact a node seeds while the master is away is
    reached by the pinned key its fingerprint binds, and says it waits for
    Support's first handshake; a resolved one that has not accepted this
    node's current key yet says that; neither reads as reachable."""
    psycopg2 = pytest.importorskip("psycopg2")
    import db_pool
    import master_node
    from config import settings
    from desktop.p2p import chat_wire
    from routers import p2p
    url = urlparse(scratch_dsn)
    for key, value in dict(postgres_host=url.hostname, postgres_port=url.port,
                           postgres_user=url.username, postgres_password=url.password,
                           postgres_db=url.path.lstrip("/")).items():
        monkeypatch.setattr(settings, key, value)
    db_pool.close_pool()
    import p2p_identity
    master, own, old = "ab" * 32, "ef" * 32, "12" * 32
    monkeypatch.setattr(master_node, "MASTER_PUBKEY_HEX", master)
    monkeypatch.setattr(settings, "p2p_enabled", True)
    monkeypatch.setattr(p2p_identity, "served_certificate", lambda s: {"pubkey": own})
    monkeypatch.setattr(p2p, "_get_identity", lambda: {"public_key_hex": own})
    fingerprint = bytes.fromhex(chat_wire.key_fingerprint(master))
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE friends CASCADE")
            cur.execute("""INSERT INTO friends (username, public_key_hex, invite_code, source,
                                                key_fingerprint)
                           VALUES ('Sautium', 'pending:Sautium#AAAA-BBBB-CCCC', 'Sautium#AAAA-BBBB-CCCC',
                                   'master', %s),
                                  ('other', 'pending:other#AAAA-BBBB-CCCD', 'other#AAAA-BBBB-CCCD',
                                   'manual', %s)""", (fingerprint, fingerprint))
        rows = {r["username"]: r for r in asyncio.run(p2p.list_friends())["items"]}
        assert rows["Sautium"]["chat_key"] == master[:16]
        assert rows["Sautium"]["support_wait"] == "handshake" and rows["Sautium"]["reach"] is False
        assert rows["other"]["chat_key"] == ""                 # a friend's stub has no thread yet
        with conn.cursor() as cur:
            cur.execute("""UPDATE friends SET public_key_hex = %s, bound_identity = %s
                           WHERE username = 'Sautium'""", (master, old))
        support = {r["username"]: r for r in asyncio.run(p2p.list_friends())["items"]}["Sautium"]
        assert support["support_wait"] == "key" and support["reach"] is False
        with conn.cursor() as cur:
            cur.execute("UPDATE friends SET bound_identity = %s WHERE username = 'Sautium'", (own,))
        support = {r["username"]: r for r in asyncio.run(p2p.list_friends())["items"]}["Sautium"]
        assert support["support_wait"] is None and support["reach"] is True
    finally:
        conn.close()
        db_pool.close_pool()
