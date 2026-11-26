"""Support diagnostics on a Docker node (since 2026-11-21, B-99): the shared
worker (desktop/p2p/diag_worker.py) as this backend builds it beside its chat
client (p2p_chat.start_client — the same gate: this backend owns the peer
surface, an account, not the master), its LISTEN, the sources its bundles
are collected from, and the node's start-of-life note.

The sources (desktop/diag_bundle.Sources): the node's settings as a config
view; its own API on loopback, signed with its own .api_secret, through the
collectors' one list of routes (diag_bundle.BACKEND_READS); the build from
config.build_identity(); the machine's facts and the containers' logs from
the host agent's `diagnostics` (scripts/host_agent.py) — asked only for a
bundle that carries `system` or a ticked `logs`, waited for as an event
(host_channel.expect), at most HOST_WAIT_S. The container gains no way to
read the Docker socket, the journal or a host path: what it knows of the
host comes through the agent's channel alone. A node with no agent (a
hand-run compose, a bridge-mode node) says so in the bundle's manifest
(`unavailable`), and the backend keeps no log file or ring of its own.

node.started is recorded once the Web UI's server listens
(serving.WEB_LISTENING) — its facts are read over that server — with
`unclean_shutdown` from a session marker under <data_dir>/diag/: written at
the lifespan's start, removed at its clean end (a SIGTERM from `docker stop`
or the host agent's update); the OOM killer, a crash or a pulled plug leave
it behind. A Docker backend starts after PostgreSQL is healthy: it spools
nothing and drains no spool.
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional, Tuple

import psycopg2

import host_channel
import library_root
import serving
from config import build_identity, settings
from db_pool import Listener, db_query
from desktop import diag_bundle
from desktop.api_client import BackendAPIClient
from desktop.p2p import diag_events
from desktop.p2p.diag_protocol import CONTENT_SCOPES
from desktop.p2p.diag_worker import DiagIdentity, DiagWorker, seal_with

logger = logging.getLogger(__name__)

# How long a bundle waits for the host agent's report: the measured round
# trip is 0.44 s; the agent's run takes a check first, whose fetch is
# bounded far below this. A bound on one wait, never a timer that re-asks.
HOST_WAIT_S = 120
NO_AGENT = "no host agent on this node"

_worker: Optional[DiagWorker] = None
_listener: Optional[Listener] = None
_starting: Optional[asyncio.Task] = None
_started_task: Optional[asyncio.Task] = None


def data_dir() -> Path:
    return Path(settings.data_dir)


# ---------------------------------------------------------------------------
# The sources
# ---------------------------------------------------------------------------

def config_view() -> dict:
    """The keys of a launcher's config.json this node has a counterpart for
    (diag_bundle.config_allowlisted keeps what it keeps of them); a key with
    none is absent."""
    return {
        "music_path": library_root.env_folder(),
        "provider": settings.default_provider,
        "hqplayer": {"enabled": settings.hqplayer_enabled, "host": settings.hqplayer_host,
                     "port": settings.hqplayer_port},
        "ports": {"web": serving.WEB_PORT, "p2p_sync": settings.p2p_sync_port,
                  "announce": settings.p2p_announce_port or settings.p2p_sync_port,
                  "media": settings.media_proxy_port, "gena": settings.dlna_gena_port},
        "claude_code_available": settings.claude_code_enabled,
        "codex_available": settings.codex_cli_enabled,
        "p2p": {"listen_port": settings.p2p_sync_port},
        "lastfm": {"username": settings.lastfm_username},
    }


def runtime() -> dict:
    return {"kind": "docker", "host_network": settings.sautium_host_network,
            "host_agent": host_channel.available()}


def backend_reads() -> diag_bundle.BackendReads:
    """This node's API on loopback, signed with its own secret — the
    launcher's path to its backend. The secret never leaves the process."""
    import auth_hmac
    host = "127.0.0.1" if serving.WEB_HOST in ("0.0.0.0", "::", "") else serving.WEB_HOST
    return diag_bundle.BackendReads(BackendAPIClient(
        f"http://{host}:{serving.WEB_PORT}", secret=auth_hmac.ensure_secret(auth_hmac.secret_path())))


def _p2p_extra() -> dict:
    """The p2p scope's runtime facts, by the launcher's keys."""
    import p2p_chat
    client = p2p_chat.chat_client()
    status = {"running": client is not None, "http_port": settings.p2p_sync_port,
              "chat_available": client is not None}
    if client is not None and client.dht is not None:
        status.update(client.dht.get_dht_stats())
    return {"p2p_status": status, "reachability": client.reachability if client else "unknown"}


def sources(host: Optional[dict] = None, unavailable: Optional[dict] = None,
            extra: Optional[dict] = None) -> diag_bundle.Sources:
    """`host`: the agent's report as host_channel.read_report gives it;
    `extra`: the p2p scope's runtime facts, read on the loop that owns them."""
    sections = (host or {}).get("sections") or {}
    return diag_bundle.Sources(
        config=config_view(), backend=backend_reads(), build=build_identity(), runtime=runtime(),
        # a tail of each log by construction (the agent's --tail and its cut)
        logs=lambda: [(name, sections[section], True)
                      for section, name in host_channel.LOG_SECTIONS.items() if section in sections],
        host=({name: sections[name] for name in host_channel.MACHINE_SECTIONS if name in sections}
              if host is not None else None),
        unavailable=unavailable or {}, extra=extra or {})


async def host_report() -> Tuple[Optional[dict], Optional[str]]:
    """(the agent's report, None) or (None, why there is none): the request
    left and its answer awaited as an event, at most HOST_WAIT_S — an answer
    of an earlier run the agent says again first wakes no one (the answer
    route's `replayed`). A floor's answer keeps the report made under ten
    minutes ago: used when it is that fresh, else named as missing. A
    database that fails meanwhile costs the host's part, never the bundle."""
    from routers.host import leave_request
    if not host_channel.available():
        return None, NO_AGENT
    answer = host_channel.expect("diagnostics")
    try:
        try:
            await asyncio.to_thread(leave_request, "diagnostics")
        except (host_channel.NoChannel, OSError, psycopg2.Error) as e:
            return None, f"the host agent could not be asked: {e}"
        try:
            outcome = await asyncio.wait_for(answer, HOST_WAIT_S)
        except asyncio.TimeoutError:
            return None, f"the host agent did not answer within {HOST_WAIT_S} s"
    finally:
        host_channel.forget("diagnostics", answer)
    try:
        row = (await asyncio.to_thread(
            db_query, "SELECT report, detail FROM host_requests WHERE verb = 'diagnostics'"))[0]
    except psycopg2.Error as e:
        return None, f"the host agent's answer could not be read: {e}"
    report = host_channel.read_report(row["report"])
    if outcome == "failed":
        return None, f"the host agent's diagnostics failed: {row['detail']}"
    if row["report"] is None:
        return None, "no report of the host agent reached the node"
    if report is None:
        return None, "the host agent's report is in a form this release does not read"
    age = int(time.time() - report["made_at"])
    if age > host_channel.DIAGNOSTICS_FLOOR:
        return None, (f"the host agent made a report {age} s ago that did not reach the node; "
                      "asked again it answers after ten minutes")
    return report, None


async def collect(warrant: dict, granted: list, node_pubkey: str) -> bytes:
    """A bundle over this node's sources: the host agent asked only when the
    bundle carries `system` or a ticked `logs` — after the person's decision
    for a warrant that waits for one, since nothing of it is collected
    before."""
    scopes = {s for s in warrant["scopes"] if s not in CONTENT_SCOPES or s in granted}
    host, why, unavailable = None, None, {}
    if scopes & {"system", "logs"}:
        host, why = await host_report()
        if host is None:
            unavailable = {"system.host": why, "logs": why}
    extra = _p2p_extra()            # the DHT's and the client's state: the loop's
    return await asyncio.to_thread(lambda: diag_bundle.collect(
        db_dsn=settings.database_url, sources=sources(host, unavailable, extra),
        warrant=warrant, granted=granted, node_pubkey=node_pubkey))


# ---------------------------------------------------------------------------
# The worker, beside the chat client
# ---------------------------------------------------------------------------

def _identity() -> Optional[DiagIdentity]:
    from p2p_identity import load_signing_key, resolve_identity
    ident = resolve_identity(settings)
    key = load_signing_key(settings)
    if not ident or key is None:
        return None
    return DiagIdentity(ident["public_key_hex"], key.sign, seal_with(key.private_bytes_raw()))


def ships() -> bool:
    """Whether this backend ships the database's reports (its worker is
    built: an account, its peer surface, not the master)."""
    return _worker is not None


def worker(find_master) -> DiagWorker:
    """The worker, built before its chat client: the client hands it the
    master's warrants and reconnects (on_warrant, on_master_reachable), and
    it finds the master through the client (`find_master`)."""
    return DiagWorker(settings.database_url, identity=_identity,
                      stage_dir=diag_events.diag_dir(data_dir()),
                      find_master=find_master, collect=collect)


async def start(built: DiagWorker) -> None:
    """With its client: the worker and its LISTEN run once the Web UI's
    server listens — a bundle reads the node over it, and the host agent
    answers through it. What the client hands over before that (a warrant
    the master pushes as the stream subscribes) waits in the worker's queue."""
    global _worker, _starting
    _worker = built
    _starting = asyncio.get_running_loop().create_task(_start_when_listening(built))


async def _start_when_listening(built: DiagWorker) -> None:
    global _listener
    await serving.WEB_LISTENING.wait()
    await built.start()
    _listener = Listener("diag-listen", ("sautium_diag", "sautium_diag_consent"),
                         on_notify=lambda notes: built.notes({n.channel for n in notes}),
                         on_connect=built.listening)
    _listener.start()


async def stop() -> None:
    """With its client — and earlier, as the Web UI's server begins to stop:
    a bundle collected after it would read a node that answers no more."""
    global _worker, _listener, _starting
    starting, _starting = _starting, None
    if starting is not None and not starting.done():
        starting.cancel()
        try:
            await starting
        except asyncio.CancelledError:
            pass
    listener, _listener = _listener, None
    built, _worker = _worker, None
    if listener is not None:
        await asyncio.to_thread(listener.stop)
    if built is not None:
        await built.stop()


# ---------------------------------------------------------------------------
# node.started and the session marker
# ---------------------------------------------------------------------------

def _record_started(previous: Optional[dict]) -> None:
    conn = psycopg2.connect(settings.database_url)
    try:
        conn.autocommit = True
        detail = diag_bundle.system_facts(sources(), conn)
        detail["unclean_shutdown"] = previous is not None
        detail["previous_session"] = previous
        diag_events.record(conn, "node.started", detail)
        diag_events.prune(conn)
    finally:
        conn.close()


async def _started(previous: Optional[dict]) -> None:
    await serving.WEB_LISTENING.wait()
    try:
        await asyncio.to_thread(_record_started, previous)
    except (psycopg2.Error, OSError) as e:
        logger.warning("diag: node.started not recorded — %s", e)


def begin_session() -> None:
    """At the lifespan's start: the marker written (the previous one, left
    behind, is an unclean end), node.started recorded once the server
    listens."""
    global _started_task
    try:
        previous = diag_events.write_session_marker(data_dir())
    except OSError as e:
        # The note is lost, never the start
        logger.warning("diag: session marker not written — %s", e)
        previous = None
    _started_task = asyncio.get_running_loop().create_task(_started(previous))


async def end_session() -> None:
    """At the lifespan's clean end."""
    if _started_task is not None and not _started_task.done():
        _started_task.cancel()
        try:
            await _started_task
        except asyncio.CancelledError:
            pass
    diag_events.clear_session_marker(data_dir())


serving.on_web_stopping(stop)
