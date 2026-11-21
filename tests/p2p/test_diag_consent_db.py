"""A support request that names the person's conversations, logs or playback
traces waits for their own decision on the request sheet, against a real
PostgreSQL built from the migrations (desktop/p2p/diag_events): nothing of it
is collected or leaves before they decide; their decision keeps what support
asked for and they ticked, and wakes the node's support worker
(desktop/p2p/diag_worker.py, both runtimes') and the notices;
the bundle carries only that, naming what was left out; a request the switch
withdrew or that expired cannot be sent; a request for facts about the node
alone is answered as before. Skipped without a cluster."""

import asyncio
import io
import json
import select
import sys
import tarfile
import time
import uuid
from datetime import datetime
from pathlib import Path

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from desktop import diag_bundle  # noqa: E402
from desktop.p2p import diag_events  # noqa: E402

CONTENT = ["system", "events", "logs", "chat"]


@pytest.fixture
def conn(scratch_dsn):
    c = psycopg2.connect(scratch_dsn)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("DELETE FROM diag_events")
        cur.execute("DELETE FROM diag_warrants")
        cur.execute("DELETE FROM user_settings WHERE key = 'support.diagnostics_enabled'")
    yield c
    c.close()


def _warrant(scopes, ttl=600, since=None) -> dict:
    now = int(time.time())
    return {"id": str(uuid.uuid4()), "issuer": "ab" * 32, "issued_at": now,
            "expires_at": now + ttl, "scopes": list(scopes), "since": since}


def _switch(conn, on: bool) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO user_settings (key, value) VALUES ('support.diagnostics_enabled', %s::jsonb)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (json.dumps(on),))


def _db_now(conn) -> datetime:
    with conn.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        return cur.fetchone()[0]


def _waiting_ids(conn) -> list:
    return [r["id"] for r in diag_events.waiting_warrants(conn)]


def test_a_request_for_content_waits_for_the_person_and_nothing_of_it_leaves(conn):
    w = _warrant(CONTENT)
    state = diag_events.receive_warrant(conn, w)
    assert state["fresh"] and state["awaits"] and state["granted"] == []
    assert _waiting_ids(conn) == [w["id"]]
    # a bundle an earlier release collected before asking counts for nothing
    diag_events.mark_collected(conn, w["id"], 100, _db_now(conn))
    assert not diag_events.may_upload(conn, w["id"])
    assert w["id"] not in diag_events.resumable_warrants(conn)
    assert diag_events.receive_warrant(conn, w)["awaits"]


def test_a_request_for_facts_about_the_node_alone_is_answered_without_asking(conn):
    w = _warrant(["system", "settings", "p2p", "jobs", "events"])
    assert not diag_events.receive_warrant(conn, w)["awaits"]
    assert _waiting_ids(conn) == []
    diag_events.mark_collected(conn, w["id"], 100, _db_now(conn))
    assert diag_events.may_upload(conn, w["id"])
    assert w["id"] in diag_events.resumable_warrants(conn)


def test_the_decision_keeps_what_was_asked_and_ticked_and_wakes_the_worker_and_the_notices(
        conn, scratch_dsn):
    w = _warrant(CONTENT)
    diag_events.receive_warrant(conn, w)
    diag_events.mark_collected(conn, w["id"], 100, _db_now(conn))     # an earlier release's
    listener = psycopg2.connect(scratch_dsn)
    listener.autocommit = True
    try:
        with listener.cursor() as cur:
            cur.execute("LISTEN sautium_diag_consent")
            cur.execute("LISTEN sautium_notices")

        assert diag_events.decide(conn, w["id"], ["chat", "playback"])

        heard = set()
        deadline = time.time() + 5
        while heard != {"sautium_diag_consent", "sautium_notices"} and time.time() < deadline:
            if select.select([listener], [], [], 1)[0]:
                listener.poll()
                heard |= {n.channel for n in listener.notifies}
                listener.notifies.clear()
        assert heard == {"sautium_diag_consent", "sautium_notices"}
    finally:
        listener.close()
    assert _waiting_ids(conn) == []
    state = diag_events.receive_warrant(conn, w)
    assert not state["awaits"] and state["granted"] == ["chat"]   # playback was not asked for
    assert state["collected_at"] is None                          # the collection that counts follows
    decided = diag_events.decided_uncollected(conn)
    assert [(d[0]["id"], d[0]["scopes"], d[1]) for d in decided] == [(w["id"], CONTENT, ["chat"])]
    assert not diag_events.decide(conn, w["id"], ["logs"])        # decided once
    diag_events.mark_collected(conn, w["id"], 100, _db_now(conn))
    assert diag_events.may_upload(conn, w["id"]) and diag_events.decided_uncollected(conn) == []


def test_nothing_ticked_sends_the_facts_about_the_node_alone(conn):
    w = _warrant(CONTENT)
    diag_events.receive_warrant(conn, w)
    assert diag_events.decide(conn, w["id"], [])
    assert diag_events.decided_uncollected(conn)[0][1] == []


def test_a_request_the_switch_withdrew_is_not_asked_about_and_cannot_be_sent(conn):
    w = _warrant(CONTENT)
    diag_events.receive_warrant(conn, w)
    _switch(conn, False)
    assert _waiting_ids(conn) == []
    _switch(conn, True)
    assert _waiting_ids(conn) == []          # back on: still withdrawn
    assert not diag_events.decide(conn, w["id"], ["chat"])


def test_an_expired_request_cannot_be_sent(conn):
    w = _warrant(CONTENT, ttl=-1)
    diag_events.receive_warrant(conn, w)
    assert _waiting_ids(conn) == []
    assert not diag_events.decide(conn, w["id"], ["chat"])


def test_a_decision_the_switch_changed_after_is_not_collected_and_does_not_leave(conn):
    w = _warrant(CONTENT)
    diag_events.receive_warrant(conn, w)
    assert diag_events.decide(conn, w["id"], ["chat"])
    _switch(conn, False)
    _switch(conn, True)
    assert diag_events.decided_uncollected(conn) == []
    assert diag_events.receive_warrant(conn, w)["withdrawn"]
    diag_events.mark_collected(conn, w["id"], 100, _db_now(conn))
    assert not diag_events.may_upload(conn, w["id"])


def test_a_bundle_staged_before_the_person_decided_on_content_counts_for_nothing(
        conn, settings_router):
    """Before this rule, a request for facts about the node alone carried
    the events' log tails too: whatever was staged and not sent is collected
    again on the warrant's next delivery (the startup step)."""
    from db_migrate import _recollect_staged_bundles

    withdrawn = _warrant(["system"])
    diag_events.receive_warrant(conn, withdrawn)
    diag_events.mark_collected(conn, withdrawn["id"], 100, _db_now(conn))
    _switch(conn, False)
    _switch(conn, True)
    staged, sent = _warrant(["system", "events"]), _warrant(["system"])
    for w in (staged, sent):
        diag_events.receive_warrant(conn, w)
        diag_events.mark_collected(conn, w["id"], 100, _db_now(conn))
    diag_events.mark_uploaded(conn, sent["id"])

    assert _recollect_staged_bundles(conn) == {"withdrawn": 1, "recollected": 1}
    assert diag_events.resumable_warrants(conn) == []
    assert diag_events.receive_warrant(conn, staged)["collected_at"] is None
    assert diag_events.receive_warrant(conn, sent)["uploaded_at"] is not None
    # collected before the switch moved: closed, never collected again
    assert diag_events.receive_warrant(conn, withdrawn)["error"] == diag_events.WITHDRAWN


def _members(data: bytes) -> dict:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return {m.name: tar.extractfile(m).read().decode("utf-8") for m in tar.getmembers()}


@pytest.mark.parametrize("granted", [[], ["logs"], ["chat"]])
def test_the_bundle_carries_only_the_content_the_person_ticked(conn, scratch_dsn, tmp_path, granted):
    (tmp_path / "backend.log").write_text("a line of the log\n", encoding="utf-8")
    diag_events.record(conn, "backend.crashed", {"rc": 1, "log_tail": "the crash's tail"})
    w = _warrant(["events", "logs", "chat"])

    sources = diag_bundle.launcher_sources({}, tmp_path, probe_backend=False)
    members = _members(diag_bundle.collect(db_dsn=scratch_dsn, sources=sources,
                                           warrant=w, granted=granted, node_pubkey="ab" * 32))

    manifest = json.loads(members["manifest.json"])
    assert manifest["scopes"] == sorted(["events", *granted])
    assert manifest["withheld"] == sorted({"logs", "chat"} - set(granted))
    assert ("logs/backend.log" in members) == ("logs" in granted)
    assert ("chat/sessions.json" in members) == ("chat" in granted)
    assert ("the crash's tail" in members["events.json"]) == ("logs" in granted)
    assert '"rc": 1' in members["events.json"]


def _node(scratch_dsn, tmp_path, monkeypatch, master_addr=None):
    """The support worker both runtimes run (desktop/p2p/diag_worker.py): the
    warrant's signature is the protocol's own tests'; the box a marker; the
    bundle a recorder; the upload what it was handed. `node.handle(w, addr)`
    and `node.decided()` run its two entries."""
    from desktop.p2p import diag_protocol
    from desktop.p2p.diag_worker import DiagIdentity, DiagWorker

    collected, uploaded = [], []

    async def collect(warrant, granted, node_pubkey):
        collected.append((warrant["id"], granted))
        return b"tar"

    async def upload(ident, warrant_id, stage, addr):
        uploaded.append((warrant_id, stage.read_bytes(), addr))
        conn = psycopg2.connect(scratch_dsn)
        conn.autocommit = True
        try:
            diag_events.mark_uploaded(conn, warrant_id)
        finally:
            conn.close()
        return True

    async def find_master(master):
        return []

    monkeypatch.setattr(diag_protocol, "verify_warrant", lambda warrant, own, **kw: (True, None))
    ident = DiagIdentity("cd" * 32, lambda message: b"sig", lambda data: b"boxed:" + data)
    worker = DiagWorker(scratch_dsn, identity=lambda: ident, stage_dir=tmp_path / "diag",
                        find_master=find_master, collect=collect)
    worker._master_addr = master_addr
    worker._upload = upload
    worker.handle = lambda w, addr: worker._handle_warrant(ident, w, addr)
    worker.decided = lambda: worker._decided(ident)
    worker.stage = worker._stage
    return worker, collected, uploaded


def test_the_worker_waits_for_the_person_then_sends_what_they_ticked(
        conn, scratch_dsn, tmp_path, monkeypatch):
    addr = ("192.0.2.1", 20000)
    node, collected, uploaded = _node(scratch_dsn, tmp_path, monkeypatch, master_addr=addr)
    w = _warrant(CONTENT)
    stage = node.stage(w["id"])
    stage.write_bytes(b"an earlier release's bundle")
    listener = psycopg2.connect(scratch_dsn)
    listener.autocommit = True
    with listener.cursor() as cur:
        cur.execute("LISTEN sautium_notices")
    try:
        asyncio.run(node.handle(w, addr))
        assert select.select([listener], [], [], 5)[0], "the notices were not woken"
    finally:
        listener.close()
    assert collected == [] and uploaded == [] and not stage.exists()
    asyncio.run(node.handle(w, addr))           # re-delivered: still waits
    assert collected == [] and uploaded == []

    assert diag_events.decide(conn, w["id"], ["logs"])
    asyncio.run(node.decided())

    assert collected == [(w["id"], ["logs"])]
    assert uploaded == [(w["id"], b"boxed:tar", addr)]
    asyncio.run(node.handle(w, addr))           # re-delivered: sent, not collected again
    assert len(collected) == 1


def test_a_decision_the_switch_withdrew_is_closed_without_collecting(
        conn, scratch_dsn, tmp_path, monkeypatch):
    addr = ("192.0.2.1", 20000)
    node, collected, uploaded = _node(scratch_dsn, tmp_path, monkeypatch, master_addr=addr)
    w = _warrant(CONTENT)
    diag_events.receive_warrant(conn, w)
    assert diag_events.decide(conn, w["id"], ["chat", "logs"])
    _switch(conn, False)
    _switch(conn, True)
    asyncio.run(node.decided())
    asyncio.run(node.handle(w, addr))           # re-delivered
    assert collected == [] and uploaded == []
    assert diag_events.receive_warrant(conn, w)["error"] == diag_events.WITHDRAWN


def test_the_masters_reconnect_collects_a_decision_whose_wake_was_missed(
        conn, scratch_dsn, tmp_path, monkeypatch):
    """The decision's NOTIFY reached no LISTEN (the connection was down):
    the master's reconnect looks for decisions too, before it resumes."""
    addr = ("192.0.2.1", 20000)
    node, collected, uploaded = _node(scratch_dsn, tmp_path, monkeypatch)
    w = _warrant(CONTENT)
    diag_events.receive_warrant(conn, w)
    assert diag_events.decide(conn, w["id"], ["chat"])

    async def run():
        reported = asyncio.Event()

        async def allowed():
            return node._identity()

        async def report(ident):
            if collected:              # the start's own report comes before the reconnect
                reported.set()
        node._allowed = allowed
        node._report = report
        await node.start()
        node.master_reachable(addr)
        await asyncio.wait_for(reported.wait(), 10)
        await node.stop()
    asyncio.run(run())
    assert collected == [(w["id"], ["chat"])]
    assert [u[0] for u in uploaded] == [w["id"]]


def test_a_decision_without_the_masters_address_waits_staged_for_its_reconnect(
        conn, scratch_dsn, tmp_path, monkeypatch):
    node, collected, uploaded = _node(scratch_dsn, tmp_path, monkeypatch)
    w = _warrant(CONTENT)
    diag_events.receive_warrant(conn, w)
    assert diag_events.decide(conn, w["id"], [])
    asyncio.run(node.decided())
    assert collected == [(w["id"], [])] and uploaded == []
    assert diag_events.resumable_warrants(conn) == [w["id"]]


def test_a_request_for_facts_alone_is_collected_and_sent_at_once(
        conn, scratch_dsn, tmp_path, monkeypatch):
    addr = ("192.0.2.1", 20000)
    node, collected, uploaded = _node(scratch_dsn, tmp_path, monkeypatch, master_addr=addr)
    w = _warrant(["system", "events"])
    asyncio.run(node.handle(w, addr))
    assert collected == [(w["id"], [])] and [u[0] for u in uploaded] == [w["id"]]


@pytest.fixture
def settings_router(scratch_dsn, monkeypatch):
    backend = str(Path(__file__).resolve().parents[2] / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    import psycopg2.pool

    import db_pool
    from routers import settings as router
    pool = psycopg2.pool.ThreadedConnectionPool(1, 2, dsn=scratch_dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    yield router
    pool.closeall()


def test_the_web_ui_shows_a_waiting_request_and_takes_the_persons_answer(conn, settings_router):
    from fastapi import HTTPException

    w = _warrant(CONTENT)
    diag_events.receive_warrant(conn, w)
    requests = settings_router._support_state()["requests"]
    assert [(r["id"], r["structural"], r["content"]) for r in requests] == [
        (w["id"], ["system", "events"], ["chat", "logs"])]

    with pytest.raises(HTTPException) as refused:
        settings_router.decide_support_request(
            uuid.UUID(w["id"]), settings_router.SupportDecision(send=["system"]))
    assert refused.value.status_code == 400
    assert settings_router.decide_support_request(
        uuid.UUID(w["id"]), settings_router.SupportDecision(send=["chat"])) == {"ok": True}
    assert settings_router._support_state()["requests"] == []
    with pytest.raises(HTTPException) as again:
        settings_router.decide_support_request(
            uuid.UUID(w["id"]), settings_router.SupportDecision(send=[]))
    assert again.value.status_code == 409
    assert diag_events.decided_uncollected(conn)[0][1] == ["chat"]
