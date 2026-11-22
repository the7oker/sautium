"""Support diagnostics on a Docker node (backend/diag_node.py, B-99) on a real
PostgreSQL built from the migrations — the shared worker
(desktop/p2p/diag_worker.py) as the backend builds it, the host agent's own
body (scripts/host_agent.py) making its report with compose faked and
answering through the node's own answer route, the master a test key and its
own ingress (backend/routers/peer_diag.py: the signature checked, the box
opened, support_reports and support_bundles written) on the same database —
the node's diag_* tables and the master's support_* ones are apart. Skipped
without a cluster.

Checked: a warrant for facts about the node alone is answered at once, its
system facts naming the node's kind and the machine's; one that names `logs`
waits for the person and the host agent is not asked before them, then its
log sections ride scrubbed — and a log line shaped as a section header stays
in the log; unticked, the machine's facts go without the logs; no host agent,
an agent that does not answer and a floor's answer each say so in the
manifest; the switch, the Support contact and the master's own key close the
gate, and a warrant that comes while the switch is off collects, asks and
sends nothing; nothing is read of the node before its Web UI listens;
node.started (with the node's kind, an unclean end, then a clean one), a
chat error and an update the host agent took back reach the master; the desk
reads the kind; the backend builds one worker where it owns the peer
surface, none in launcher mode or on the master, and a new one on a rotated
key, which refuses a warrant to the old; the worker's connection survives a
PostgreSQL restart; the collectors read the node's API by one list of
routes, signed with the node's own secret, which its door takes."""

import asyncio
import importlib.util
import io
import json
import socket
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import psycopg2
import psycopg2.pool
import pytest

REPO = Path(__file__).resolve().parents[2]
for p in (REPO, REPO / "backend"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")

from desktop import diag_bundle, node_identity  # noqa: E402
from desktop.p2p import diag_events, diag_protocol, master_node  # noqa: E402
from desktop.p2p.diag_worker import DiagIdentity, seal_with  # noqa: E402


def _load_agent():
    spec = importlib.util.spec_from_file_location("host_agent_b99", REPO / "scripts" / "host_agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ha = _load_agent()


def _who(name):
    key = ed25519.Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes_raw()
    return SimpleNamespace(key=key, pub=raw.hex(), name=name, seed=key.private_bytes_raw(),
                           invite=node_identity.make_invite_code(name, raw))


ME, MASTER, OLD = _who("pi"), _who("Sautium"), _who("pi-before")
ADDR = ("192.0.2.1", 8801)

BACKEND_LOG = ("INFO startup complete\n"
               "== power\nfine\n"
               "WARNING peer 192.168.1.20 refused /home/someone/Music/A Band/01 Song.flac\n"
               "INFO setup code: K7QM-ZP4X\n")


def _ident(who=ME) -> DiagIdentity:
    return DiagIdentity(who.pub, who.key.sign, seal_with(who.seed))


class _Reads(diag_bundle.BackendReads):
    """The node's own API, answering by the collectors' one list of routes."""

    def __init__(self):
        self.asked = []

    def get(self, path, timeout=5):
        assert path in self.routes, path
        self.asked.append(path)
        return {"/config": {"app_version": "0.2.0"},
                "/health": {"status": "healthy", "checks": {"music_library": {"exists": True}}},
                "/api/settings/hardware": {"profile": "lite"}}.get(path)


class _Request:
    def __init__(self, signer):
        self.state = SimpleNamespace(signer=signer)


@pytest.fixture
def node(scratch_dsn, tmp_path, monkeypatch):
    """A Docker node on the scratch database: its settings and pool there,
    the master a test key and its friend, the host agent's channel in
    tmp_path, the worker built as the backend builds it, its POSTs to the
    master kept."""
    import db_pool
    import diag_node
    import host_channel
    import master_node as backend_master
    from config import settings
    from routers import settings as settings_router

    url = urlparse(scratch_dsn)
    for key, value in dict(postgres_host=url.hostname, postgres_port=url.port,
                           postgres_user=url.username, postgres_password=url.password,
                           postgres_db=url.path.lstrip("/"), data_dir=str(tmp_path / "data"),
                           p2p_enabled=True, p2p_sync_port=8801).items():
        monkeypatch.setattr(settings, key, value)
    pool = psycopg2.pool.ThreadedConnectionPool(1, 4, dsn=scratch_dsn, options="-c timezone=UTC")
    monkeypatch.setattr(db_pool, "_pool", pool)
    invite = node_identity.make_invite_code("Sautium", bytes.fromhex(MASTER.pub))
    for mod in (master_node, backend_master):
        monkeypatch.setattr(mod, "MASTER_PUBKEY_HEX", MASTER.pub)
        monkeypatch.setattr(mod, "MASTER_INVITE_CODE", invite)
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("TRUNCATE friends CASCADE")
        cur.execute("TRUNCATE host_requests")
        cur.execute("DELETE FROM diag_events")
        cur.execute("DELETE FROM diag_warrants")
        cur.execute("DELETE FROM user_settings WHERE key IN ('support.diagnostics_enabled', "
                    "'p2p.master_removed')")
        cur.execute("INSERT INTO friends (public_key_hex, invite_code, username, source) "
                    "VALUES (%s, %s, 'Sautium', 'master')", (MASTER.pub, invite))
    requests = tmp_path / "requests"
    requests.mkdir()
    monkeypatch.setattr(host_channel, "REQUESTS_DIR", requests)
    monkeypatch.setattr(host_channel, "_KEY", b"ab" * 32)
    monkeypatch.setattr(settings_router, "notify_library_subscribers", lambda: None)
    reads = _Reads()
    monkeypatch.setattr(diag_node, "backend_reads", lambda: reads)
    monkeypatch.setattr(diag_node, "_identity", lambda: _ident())

    checkout = tmp_path / "sautium"
    (checkout / "host").mkdir(parents=True)
    (checkout / "host" / "agent.log").write_text("the agent's own line\n")
    # the log the agent kept of the container an update replaced (PI-NODE-2C §12.1)
    (checkout / "host" / "logs").mkdir()
    (checkout / "host" / "logs" / "backend-20261120-101500-update.log").write_text(
        "INFO before the update\nWARNING peer 192.168.1.21 asked for /home/someone/Music/B/02 Tune.flac\n")
    board = tmp_path / "model"
    board.write_bytes(b"Raspberry Pi 5 Model B Rev 1.0\0")
    monkeypatch.setattr(ha, "BOARD_MODEL", board)

    def compose(args, cwd=None, timeout=None, check=False, **kw):
        text = " ".join(args)
        out = ("Linux 6.12.47 aarch64\ndocker 29.1\n2.40\nDebian GNU/Linux 13" if "uname" in text
               else "throttled=0x50005" if "vcgencmd" in text
               else "/dev/nvme0n1p2  234G  41G  /home/someone/sautium" if "df -h" in text
               else "sautium-backend running (healthy)" if " ps " in f" {text} "
               else "/sautium-backend restarts=3 oom_killed=true exit=137" if "inspect" in text
               else BACKEND_LOG if text.endswith(" backend")
               else "LOG: database system is ready" if text.endswith(" postgres")
               else "")
        return subprocess.CompletedProcess(args, 0, out.encode(), b"")
    monkeypatch.setattr(ha, "_run", compose)

    state = SimpleNamespace(agent={}, silent=False, floor=False, resend=False, asked=0, posts=[])
    from routers import host

    async def agent_answers():
        """The agent's body making its report, answered through the route —
        after, with `resend`, the answer of a run the node missed, sent again
        first as the agent's run sends it: without its report."""
        if state.resend:
            await host.answer(host.Answer(verb="diagnostics", outcome=state.resend, detail="earlier",
                                          replayed=True), _Request("host-agent"))
            await asyncio.sleep(0)
        if state.floor:
            outcome, detail, report = "floor", "made 30 s ago", None
        else:
            outcome, detail, _, report = await asyncio.to_thread(
                ha.verb_diagnostics, state.agent, time.time(), checkout)
        await host.answer(host.Answer(verb="diagnostics", outcome=outcome, detail=detail,
                                      report=report), _Request("host-agent"))

    real_leave = host.leave_request

    def leave_request(verb):
        real_leave(verb)
        state.asked += 1
        if not state.silent:
            state.loop.call_soon_threadsafe(lambda: state.loop.create_task(agent_answers()))
    monkeypatch.setattr(host, "leave_request", leave_request)

    from routers import peer_diag
    monkeypatch.setattr(peer_diag, "resolve_identity", lambda _s: {"public_key_hex": MASTER.pub})
    monkeypatch.setattr(peer_diag, "own_seed", lambda: MASTER.seed)
    monkeypatch.setattr(peer_diag, "get_peer_chat", lambda: SimpleNamespace(
        friend_for_key=lambda key: {"is_blocked": False} if key == ME.pub else None))

    async def send(addr, target, body, headers, total_s):
        """The master's ingress, as its peer surface runs it."""
        from starlette.requests import Request
        path, _, query = target.partition("?")
        request = Request({"type": "http", "method": "POST", "path": path,
                           "raw_path": path.encode(), "query_string": query.encode(),
                           "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]})
        if path == "/api/diag/report":
            answer = await asyncio.to_thread(peer_diag._store_report, request, body)
        else:
            answer = await asyncio.to_thread(peer_diag._store_bundle, request, body,
                                             query.removeprefix("warrant="))
        state.posts.append(path)
        if state.sent is not None:
            state.sent.set()
        return (200, "ok") if isinstance(answer, dict) else (answer.status_code, answer.body.decode())

    worker = diag_node.worker(find_master=None)
    worker._send = send
    state.worker, state.conn, state.requests, state.sent = worker, conn, requests, None
    yield state
    conn.close()
    pool.closeall()


def _warrant(scopes, target=ME, conn=None):
    """A warrant the master signed — and, given its database, issued (the
    row its ingress takes a bundle against)."""
    w = diag_protocol.sign_warrant(MASTER.key.sign, target=target.pub, scopes=scopes,
                                   issuer=MASTER.pub)
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO support_warrants (id, node_pubkey, scopes, issued_at, expires_at) "
                        "VALUES (%s, %s, %s, to_timestamp(%s), to_timestamp(%s))",
                        (w["id"], target.pub, w["scopes"], w["issued_at"], w["expires_at"]))
    return w


def _W(node, scopes, target=ME):
    return _warrant(scopes, target, node.conn)


def _members(tar: bytes) -> dict:
    with tarfile.open(fileobj=io.BytesIO(tar), mode="r:gz") as t:
        return {m.name: t.extractfile(m).read().decode("utf-8") for m in t.getmembers()}


def _bundle(node) -> dict:
    """The one bundle the master's ingress stored since the last look, opened."""
    with node.conn.cursor() as cur:
        cur.execute("SELECT node_pubkey, ciphertext FROM support_bundles")
        (pubkey, box), = cur.fetchall()
        cur.execute("DELETE FROM support_bundles")
    assert pubkey == ME.pub
    return _members(diag_protocol.decrypt_from(MASTER.seed, ME.pub, bytes(box)))


def _handle(node, w):
    async def run():
        node.loop = asyncio.get_running_loop()
        await node.worker._handle_warrant(_ident(), w, ADDR)
    asyncio.run(run())


def _decided(node):
    async def run():
        node.loop = asyncio.get_running_loop()
        node.worker._master_addr = ADDR
        await node.worker._decided(_ident())
    asyncio.run(run())


# -- warrants ------------------------------------------------------------------

def test_facts_about_the_node_alone_are_answered_at_once_with_the_machines(node):
    _handle(node, _W(node, ["system", "settings", "p2p", "events"]))
    members = _bundle(node)
    manifest = json.loads(members["manifest.json"])
    assert manifest["withheld"] == [] and manifest["unavailable"] == {} and manifest["errors"] == {}
    assert manifest["runtime"] == {"kind": "docker", "host_network": False, "host_agent": True}
    system = json.loads(members["system.json"])
    assert system["runtime"]["kind"] == "docker"
    assert system["host"]["power"] == "throttled=0x50005"
    assert system["host"]["board"] == "Raspberry Pi 5 Model B Rev 1.0"
    assert "oom_killed=true" in system["host"]["restarts"]
    # the kept log is named among the facts (a name and a size the agent gives); its lines are a log
    assert system["host"]["containers"].endswith("\nkept logs: backend-20261120-101500-update.log 0 KiB")
    assert "before the update" not in members["system.json"]
    assert "someone" not in members["system.json"]                  # df's mount point
    assert not [name for name in members if name.startswith("logs/")]
    assert node.asked == 1


def test_logs_wait_for_the_person_and_go_scrubbed_once_ticked(node):
    w = _W(node, ["system", "events", "logs"])
    _handle(node, w)
    assert node.posts == [] and node.asked == 0 and list(node.requests.iterdir()) == []

    assert diag_events.decide(node.conn, w["id"], ["logs"])
    _decided(node)
    members = _bundle(node)
    log = members["logs/backend.log"]
    assert "== power\nfine" in log                                   # a log line stays in the log
    assert json.loads(members["system.json"])["host"]["power"] == "throttled=0x50005"
    for gone in ("192.168.1.20", "someone", "Song", "K7QM-ZP4X"):
        assert gone not in log, gone
    assert "[private address]" in log and "[name].flac" in log
    assert "logs/postgres.log" in members and "logs/host-agent.log" in members
    kept = members["logs/backend-before-update.log"]
    assert kept.startswith("(host/logs/backend-20261120-101500-update.log)\nINFO before the update\n")
    assert "192.168.1.21" not in kept and "someone" not in kept and "Tune" not in kept
    assert node.asked == 1


def test_unticked_the_machines_facts_go_without_its_logs(node):
    w = _W(node, ["system", "logs"])
    _handle(node, w)
    assert diag_events.decide(node.conn, w["id"], [])
    _decided(node)
    members = _bundle(node)
    assert json.loads(members["manifest.json"])["withheld"] == ["logs"]
    assert "power" in json.loads(members["system.json"])["host"]
    assert not [name for name in members if name.startswith("logs/")]


def test_no_agent_a_silent_one_and_a_floor_say_so(node, monkeypatch):
    import diag_node
    import host_channel

    monkeypatch.setattr(host_channel, "_KEY", None)
    w = _W(node, ["system", "logs"])
    _handle(node, w)
    assert diag_events.decide(node.conn, w["id"], ["logs"])
    _decided(node)
    manifest = json.loads(_bundle(node)["manifest.json"])
    assert manifest["unavailable"] == {"system.host": diag_node.NO_AGENT, "logs": diag_node.NO_AGENT}
    assert manifest["runtime"]["host_agent"] is False and node.asked == 0

    monkeypatch.setattr(host_channel, "_KEY", b"ab" * 32)
    monkeypatch.setattr(diag_node, "HOST_WAIT_S", 0.3)
    node.silent = True
    _handle(node, _W(node, ["system"]))
    assert "did not answer within" in json.loads(_bundle(node)["manifest.json"])["unavailable"]["system.host"]

    node.silent = False
    _handle(node, _W(node, ["system"]))                       # a report is made and kept
    assert "power" in json.loads(_bundle(node)["system.json"])["host"]
    node.floor = True
    _handle(node, _W(node, ["system"]))                       # the floor's answer: the kept one, fresh
    assert "power" in json.loads(_bundle(node)["system.json"])["host"]
    with node.conn.cursor() as cur:
        cur.execute("SELECT report FROM host_requests WHERE verb = 'diagnostics'")
        report = json.loads(cur.fetchone()[0])
        report["made_at"] -= 3600
        cur.execute("UPDATE host_requests SET report = %s WHERE verb = 'diagnostics'",
                    (json.dumps(report),))
    _handle(node, _W(node, ["system"]))
    assert "did not reach the node" in json.loads(_bundle(node)["manifest.json"])["unavailable"]["system.host"]


@pytest.mark.parametrize("again", ["done", "failed", "floor"])
def test_an_answer_sent_again_is_waited_past_and_keeps_the_report(node, again):
    _handle(node, _W(node, ["system"]))
    _bundle(node)
    with node.conn.cursor() as cur:
        cur.execute("UPDATE host_requests SET report = jsonb_set(report::jsonb, '{made_at}', "
                    "to_jsonb(extract(epoch FROM now())::int - 700))::text WHERE verb = 'diagnostics'")
    node.agent, node.resend = {}, again
    _handle(node, _W(node, ["system"]))
    assert json.loads(_bundle(node)["manifest.json"])["unavailable"] == {}
    with node.conn.cursor() as cur:
        cur.execute("SELECT report IS NOT NULL FROM host_requests WHERE verb = 'diagnostics'")
        assert cur.fetchone()[0]


def test_the_gate(node, monkeypatch):
    worker = node.worker

    def allowed():
        return asyncio.run(worker._allowed())

    assert allowed() is not None
    with node.conn.cursor() as cur:
        cur.execute("INSERT INTO user_settings (key, value) VALUES ('support.diagnostics_enabled', 'false')")
        assert allowed() is None
        cur.execute("DELETE FROM user_settings WHERE key = 'support.diagnostics_enabled'")
        cur.execute("UPDATE friends SET is_blocked = TRUE")
        assert allowed() is None
        cur.execute("DELETE FROM friends")
        assert allowed() is None
        cur.execute("INSERT INTO friends (public_key_hex, invite_code, username, source) "
                    "VALUES (%s, %s, 'Sautium', 'master')", (MASTER.pub, master_node.MASTER_INVITE_CODE))
    assert allowed() is not None
    monkeypatch.setattr(worker, "_identity", lambda: _ident(MASTER))     # the master ships nothing
    assert allowed() is None


def test_a_warrant_while_the_switch_is_off_collects_asks_and_sends_nothing(node):
    with node.conn.cursor() as cur:
        cur.execute("INSERT INTO user_settings (key, value) VALUES ('support.diagnostics_enabled', 'false')")
    assert diag_events.record(node.conn, "chat.error", {"error": "boom"}) is None
    worker, gated = node.worker, []

    async def scene():
        node.loop = asyncio.get_running_loop()
        done, real = asyncio.Event(), worker._allowed

        async def allowed():
            gated.append(await real())
            if len(gated) == 2:            # the warrant handed over before the start, then the start's report
                done.set()
            return gated[-1]
        worker._allowed = allowed
        worker.warrant(_W(node, ["system", "events"]), ADDR)
        await worker.start()
        await asyncio.wait_for(done.wait(), 10)
        await worker.stop()
    asyncio.run(scene())
    assert gated == [None, None] and node.posts == [] and node.asked == 0
    assert list(node.requests.iterdir()) == []
    with node.conn.cursor() as cur:
        cur.execute("SELECT (SELECT count(*) FROM diag_warrants) + (SELECT count(*) FROM diag_events)")
        assert cur.fetchone()[0] == 0


def test_nothing_is_read_of_the_node_before_its_web_ui_listens(node, monkeypatch):
    import diag_node
    import serving
    monkeypatch.setattr(serving, "WEB_LISTENING", asyncio.Event())
    worker = node.worker

    async def scene():
        node.loop = asyncio.get_running_loop()
        node.sent = asyncio.Event()
        worker.warrant(_W(node, ["system", "jobs"]), ADDR)       # pushed as the stream subscribes
        await diag_node.start(worker)
        for _ in range(20):
            await asyncio.sleep(0)                               # everything runnable runs
        before = (len(node.posts), node.asked, worker.running)
        serving.WEB_LISTENING.set()
        await asyncio.wait_for(node.sent.wait(), 30)
        await diag_node.stop()
        return before
    assert asyncio.run(scene()) == (0, 0, False)
    assert "jobs.json" in _bundle(node) and node.asked == 1


def test_the_workers_connection_survives_a_postgresql_restart(node):
    worker = node.worker

    async def scene():
        first = await worker._db(lambda conn: conn.get_backend_pid())
        with node.conn.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(%s)", (first,))
        assert await worker._allowed() is not None
        return first, await worker._db(lambda conn: conn.get_backend_pid())
    first, again = asyncio.run(scene())
    assert first != again


def test_a_warrant_to_a_key_the_node_rotated_away_from_is_refused(node):
    _handle(node, _W(node, ["system"], target=OLD))
    with node.conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM diag_warrants")
        assert cur.fetchone()[0] == 0
    assert node.posts == []


# -- events --------------------------------------------------------------------

def test_events_reach_the_master_and_the_desk_reads_the_kind(node, monkeypatch):
    import diag_node
    import serving
    from routers import host, support

    monkeypatch.setattr(serving, "WEB_LISTENING", asyncio.Event())
    diag_events.write_session_marker(diag_node.data_dir())          # a start that never ended

    async def start_and_stop(update=False):
        diag_node.begin_session()
        serving.WEB_LISTENING.set()
        await asyncio.wait_for(diag_node._started_task, 30)
        if update:
            await host.answer(host.Answer(verb="update", outcome="undone", detail="did not start"),
                              _Request("host-agent"))
        await diag_node.end_session()
    asyncio.run(start_and_stop(update=True))
    assert not (diag_node.data_dir() / "diag" / diag_events.SESSION_FILENAME).exists()
    asyncio.run(start_and_stop())                                   # after a clean stop
    diag_events.record(node.conn, "chat.error", {"error": "boom"})

    async def report():
        node.worker._master_addr = ADDR
        await node.worker._report(_ident())
    asyncio.run(report())
    assert node.posts == ["/api/diag/report"]
    with node.conn.cursor() as cur:
        cur.execute("SELECT node_pubkey, kind, detail FROM support_reports ORDER BY id")
        rows = cur.fetchall()
    assert {r[0] for r in rows} == {ME.pub}
    kinds = [r[1] for r in rows]
    assert {"node.started", "chat.error", "update.failed"} <= set(kinds)
    started = [r[2] for r in rows if r[1] == "node.started"]
    assert [d["unclean_shutdown"] for d in started] == [True, False]
    assert started[0]["runtime"]["kind"] == "docker"
    assert [r[2] for r in rows if r[1] == "update.failed"][0]["outcome"] == "undone"
    assert support._started_summary(started[0])["runtime"]["kind"] == "docker"
    assert support._started_summary({"commit": "abc"})["runtime"] == {"kind": "launcher"}


# -- the build -----------------------------------------------------------------

def test_one_worker_where_the_backend_owns_the_peer_surface(node, monkeypatch):
    import diag_node
    import p2p_chat
    from config import settings
    from desktop.p2p import chat_client as cc

    monkeypatch.setattr(p2p_chat, "_client_lock", asyncio.Lock())
    monkeypatch.setattr(p2p_chat, "_client_deps", {"announce_port": lambda: 8801})
    keys = {"who": ME}
    monkeypatch.setattr(p2p_chat, "_keys", lambda: (
        {"public_key_hex": keys["who"].pub}, keys["who"].seed, ()))

    async def started(self):
        self.running = True
    monkeypatch.setattr(cc.ChatClient, "start", started)

    async def scene(port, who):
        monkeypatch.setattr(settings, "p2p_sync_port", port)
        keys["who"] = who
        client = await p2p_chat.start_client()
        worker = diag_node._worker
        hands = client is not None and client._on_warrant == worker.warrant
        await p2p_chat.stop_client()
        return client is not None, worker is not None, hands, diag_node._worker

    assert asyncio.run(scene(0, ME)) == (False, False, False, None)        # launcher mode
    assert asyncio.run(scene(8801, MASTER)) == (False, False, False, None)  # the master
    assert asyncio.run(scene(8801, ME)) == (True, True, True, None)
    assert diag_node.ships() is False

    async def rotated():
        keys["who"] = OLD
        await p2p_chat.start_client()
        from routers.settings import _support_state
        before, shipping = diag_node._worker, _support_state()["sends_reports"]   # the Support row
        keys["who"] = ME                    # the rotation: everything on the new key
        await p2p_chat.restart_client()
        after = diag_node._worker
        await p2p_chat.stop_client()
        return before, after, shipping
    before, after, shipping = asyncio.run(rotated())
    assert shipping and before is not None and after is not None and after is not before


# -- the node's API ------------------------------------------------------------

def test_the_collectors_read_one_list_of_routes(node, tmp_path, scratch_dsn):
    import diag_node
    launcher = diag_bundle.launcher_sources({"ports": {"web": 18000}}, tmp_path)
    assert launcher.backend.routes is diag_bundle.BACKEND_READS
    with pytest.raises(ValueError):
        launcher.backend.get("/api/settings/ai/key")
    docker, reads = diag_node.sources(), _Reads()
    docker.backend = reads
    diag_bundle.collect(db_dsn=scratch_dsn, sources=docker, node_pubkey=ME.pub, granted=["playback"],
                        warrant={"id": str(uuid.uuid4()), "since": None,
                                 "scopes": list(diag_protocol.SCOPES)})
    assert set(reads.asked) <= set(diag_bundle.BACKEND_READS)


def test_the_loopback_reader_signs_with_the_nodes_own_secret(tmp_path, monkeypatch):
    import uvicorn
    from fastapi import FastAPI

    import auth_hmac
    import device_auth
    import diag_node
    import serving
    from config import settings

    monkeypatch.setattr(settings, "p2p_identity_dir", str(tmp_path))
    monkeypatch.setattr(device_auth, "current_token", lambda secret: "t" * 64)
    secret = auth_hmac.ensure_secret(auth_hmac.secret_path())
    app = FastAPI()

    @app.get("/api/settings/hardware")             # behind the door: /health is open
    def hardware():
        return {"profile": "lite"}

    door = auth_hmac.HMACAuthMiddleware(app, auth_hmac.secret_path())
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    listening = threading.Event()
    ready = asyncio.Event()
    server = serving.Server(uvicorn.Config(door, host="127.0.0.1", port=port, log_level="warning"),
                            ready=ready)

    async def serve():
        asyncio.create_task(ready.wait()).add_done_callback(lambda _: listening.set())
        await server.serve()
    thread = threading.Thread(target=asyncio.run, args=(serve(),), daemon=True)
    thread.start()
    try:
        assert listening.wait(10)
        monkeypatch.setattr(serving, "WEB_PORT", port)
        reads = diag_node.backend_reads()
        assert reads.routes is diag_bundle.BACKEND_READS        # the launcher's list, the same object
        assert reads.get("/api/settings/hardware") == {"profile": "lite"}
        from desktop.api_client import BackendAPIClient
        stranger = diag_bundle.BackendReads(BackendAPIClient(f"http://127.0.0.1:{port}",
                                                             secret=b"x" * len(secret)))
        assert stranger.get("/api/settings/hardware") is None
    finally:
        server.should_exit = True
        thread.join(5)


# -- the host agent's report ---------------------------------------------------

def test_the_agent_and_the_node_name_the_same_sections():
    import host_channel
    assert ha.REPORT_FORMAT == host_channel.REPORT_FORMAT
    assert ha.MACHINE_SECTIONS == host_channel.MACHINE_SECTIONS
    assert ha.LOG_SECTIONS == tuple(host_channel.LOG_SECTIONS)
    assert ha.DIAGNOSTICS_FLOOR == host_channel.DIAGNOSTICS_FLOOR
    assert ha.REPORT_MAX == host_channel.REPORT_MAX


def test_a_report_is_bounded_by_its_logs_never_cut_as_json():
    import host_channel
    sections = {"power": "throttled=0x0", "log: backend": ("x" * 99 + "\n") * 60000,
                "log: postgres": ("y" * 99 + "\n") * 30000}
    report = ha.report_json(sections, 1000.0)
    assert len(report.encode()) <= ha.REPORT_MAX
    read = host_channel.read_report(report)
    assert read["made_at"] == 1000 and read["sections"]["power"] == "throttled=0x0"
    assert read["sections"]["log: backend"].startswith("x")
    assert len(read["sections"]["log: backend"].encode()) <= ha.SECTION_MAX
    assert host_channel.read_report("== versions\nLinux\n") is None
    page = host_channel.report_page(report)
    assert page["report"].startswith("== power\nthrottled=0x0\n") and page["made_at"].startswith("1970")


def test_the_worker_ends_before_the_web_ui_stops_answering(monkeypatch):
    import uvicorn

    import diag_node
    import serving
    assert diag_node.stop in serving._web_stopping
    order = []

    async def stopping():
        order.append("diag")

    async def closed(self, sockets=None):
        order.append("listener")
    monkeypatch.setattr(serving, "_web_stopping", [stopping])
    monkeypatch.setattr(uvicorn.Server, "shutdown", closed)
    web = serving.Server(uvicorn.Config(None), ready=serving.WEB_LISTENING)
    peer = serving.Server(uvicorn.Config(None), what="the peer surface", ready=asyncio.Event())
    asyncio.run(peer.shutdown())
    asyncio.run(web.shutdown())
    assert order == ["listener", "diag", "listener"]
