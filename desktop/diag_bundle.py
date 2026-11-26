"""Support diagnostics — the bundle collector both runtimes use
(desktop/p2p/diag_protocol.py, P2P_NETWORK.md § "Support diagnostics").

A warrant names scopes; every scope below is a fixed collector over fixed
sources, and settings / the config view pass through ALLOWLISTS. Nothing
here takes a path, a table or a query from the wire. What a collector reads
FROM is the runtime's `Sources` (since 2026-11-21, B-99): the launcher's
(`launcher_sources`: config.json, its backend, the data folder's logs) or a
Docker node's (backend/diag_node.py: its settings as a config view, its own
API on loopback, the host agent's report). Both read the node's API through
`BackendReads`, over the one list of routes BACKEND_READS.

Never in a bundle: p2p_messages (friends' private E2E chat — the master
already has its own thread with this node), credentials of any kind
(ai.api_key, the Last.fm session, config.json api_keys / postgres_password,
node keys, TLS keys, .api_secret, the agents' auth files). Log tails go
through diag_protocol.scrub_personal: no credential, email, address or
absolute path. The content scopes — `chat`, `logs` (with the events' log
tails) and `playback` — go only with the person's own tick on the request
sheet (diag_protocol.CONTENT_SCOPES). The `playback` scope — the HQPlayer
output's playback traces and HQPlayer's own log — comes
from the backend with each track named by its ids, format and length and
every path cut to its last two components; a library file's name leaves as
its extension (scrub_detail), even in a scope the person sends.
"""

import io
import json
import logging
import platform
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

import psycopg2

from desktop import os_locale
from desktop.api_client import BackendAPIClient
from desktop.p2p import contact_log, diag_events
from desktop.p2p.diag_protocol import (
    BUNDLE_MAX_BYTES, CHAT_DEFAULT_DAYS, CONTENT_SCOPES, EVENTS_MAX_ROWS, LOG_TAIL_BYTES,
    PROTOCOL_VERSION, scrub_detail, scrub_personal, strip_local_only,
)
from desktop.p2p.master_node import MASTER_INVITE_CODE, MASTER_PUBKEY_HEX

logger = logging.getLogger(__name__)

SETTINGS_KEY_PREFIXES = ("sync.", "p2p.", "enrichment.", "output.", "albums.",
                         "musicbrainz.", "library.", "discovery.", "support.")
SETTINGS_KEYS = ("ai.provider", "ai.model", "ai.canonization_enabled",
                 "hqplayer.host", "hqplayer.port", "ui.language")
CONFIG_KEYS = ("version", "music_path", "provider", "hqplayer", "ports",
               "claude_code_available", "codex_available", "first_run_complete",
               "sync", "mb_slice")
CONFIG_P2P_KEYS = ("listen_port", "docker_ports", "chat_enabled")
LOG_FILES = ("launcher.log", "backend.log", "backend.log.1", "pgdata/server.log",
             "bootstrap.log")
# Every route of the node's own API a collector reads, on either runtime —
# and nothing else: BackendReads refuses any other.
BACKEND_READS = ("/config", "/health", "/stats", "/api/settings/ai", "/api/settings/hardware",
                 "/api/settings/ai/claude/state", "/api/settings/ai/codex/state",
                 "/api/settings/sync", "/api/settings/library", "/scan/status", "/enrich/status",
                 "/api/settings/musicbrainz/status", "/api/player/diagnostics/hqplayer/bundle")
REPORT_LOG_TAIL_BYTES = 256 * 1024     # per log in a report sent by hand
CHAT_MAX_MESSAGES = 5000
SYSTEM_SETTINGS_KEYS = ("p2p.reachability", "p2p.identity",
                        "sync.last_at", "sync.last_items_received", "ui.language")


def _json(obj) -> str:
    return json.dumps(obj, indent=1, ensure_ascii=False, default=str)


def _iso(value) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else value


class BackendReads:
    """The node's own API as the collectors read it: BACKEND_READS alone,
    GETs, None for a route that did not answer."""

    routes = BACKEND_READS

    def __init__(self, client: BackendAPIClient):
        self._client = client

    def get(self, path: str, timeout: int = 5) -> Optional[dict]:
        if path not in self.routes:
            raise ValueError(f"not a route the collectors read: {path}")
        return self._client._get_json(path, timeout=timeout)


@dataclass
class Sources:
    """What a runtime's collectors read from.

    `config` is the config view (config.json on a launcher); `backend` the
    node's API, None where it is not asked (a launcher that knows its
    backend is down); `build` {"commit", "build"}; `runtime` the node's kind;
    `logs` the log tails, raw — scrubbed here; `host` the machine's facts by
    section (a Docker node's host agent), None where there is none;
    `unavailable` what this kind of node cannot give, and why — the
    manifest's, never a refusal; `extra` the p2p scope's runtime facts."""
    config: dict
    backend: Optional[BackendReads]
    build: dict
    runtime: dict
    logs: Callable[[], List[Tuple[str, str, bool]]] = lambda: []
    host: Optional[dict] = None
    unavailable: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


def launcher_logs(data_dir: Path) -> Callable[[], List[Tuple[str, str, bool]]]:
    def read() -> List[Tuple[str, str, bool]]:
        out = []
        for name in LOG_FILES:
            path = Path(data_dir) / name
            if path.exists():
                text, truncated = tail(path)
                out.append((Path(name).name, text, truncated))
        return out
    return read


def launcher_sources(config: dict, data_dir: Path, probe_backend: bool = True,
                     extra: Optional[dict] = None) -> Sources:
    """The launcher's: config.json, its backend on config.json's port with
    the identity folder's .api_secret, the data folder's logs."""
    from desktop import updater
    backend = None
    if probe_backend:
        backend = BackendReads(BackendAPIClient(
            f"http://127.0.0.1:{config.get('ports', {}).get('web', 18000)}"))
    return Sources(config=config, backend=backend,
                   build={"commit": updater.current_commit(), "build": updater.installed_build()},
                   runtime={"kind": "launcher"}, logs=launcher_logs(data_dir), extra=extra or {})


def _settings_values(conn, keys) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM user_settings WHERE key = ANY(%s)", (list(keys),))
        return dict(cur.fetchall())


def _schema_head(conn) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT filename FROM _schema_migrations ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# system — also node.started's detail
# ---------------------------------------------------------------------------

def system_facts(sources: Sources, conn=None) -> dict:
    """Build, machine, tools and agent states, and the node's kind.
    Backend-owned facts come over the signed local API and read None while
    the backend is not answering — or without asking, when the caller knows
    it is down (`sources.backend` None): six probes at a dead port cost ~2 s
    each on Windows, at a hung one 5 s each.

    An allow-list, field by field, because node.started sends these at every
    start without a warrant: of the backend's health checks only their states
    — the music folder as there or not, never its path; the reachability as
    its kind, never the addresses behind it."""
    from desktop.utils import (detect_claude_cli, detect_codex_cli, detect_gpu,
                               detect_node_version)

    gpu_ok, gpu_name, vram_gb = detect_gpu()
    node_ver = detect_node_version()

    def backend(path: str) -> Optional[dict]:
        return sources.backend.get(path) if sources.backend else None

    cfg = backend("/config") or {}
    ai = backend("/api/settings/ai") or {}
    health = backend("/health") or {}
    checks = health.get("checks") or {}
    database = checks.get("database")
    facts = {
        "commit": sources.build.get("commit"),
        "build": sources.build.get("build"),
        "runtime": sources.runtime,
        "app_version": cfg.get("app_version"),
        "os": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "gpu": {"available": gpu_ok, "name": gpu_name, "vram_gb": vram_gb},
        "tools": {
            "node": ".".join(str(p) for p in node_ver) if node_ver else None,
            "claude_cli": detect_claude_cli(),
            "codex_cli": detect_codex_cli(),
        },
        "hardware": backend("/api/settings/hardware"),
        "agents": {
            "provider": ai.get("provider"),
            "model": ai.get("model"),
            "auth_state": ai.get("auth_state"),
            "claude": (backend("/api/settings/ai/claude/state") or {}).get("state"),
            "codex": (backend("/api/settings/ai/codex/state") or {}).get("state"),
        },
        "backend": {
            "status": health.get("status"),
            "database": scrub_personal(str(database)) if database is not None else None,
            "music_folders": checks.get("music_library"),
            "dht": checks.get("dht"),
        },
        "ports": sources.config.get("ports"),
    }
    if conn is not None:
        facts["settings"] = _settings_values(conn, SYSTEM_SETTINGS_KEYS)
        facts["schema_head"] = _schema_head(conn)
    facts["locale"] = os_locale.describe(
        (facts.get("settings") or {}).get("ui.language"))
    return facts


# ---------------------------------------------------------------------------
# settings — allowlists, never the credential rows
# ---------------------------------------------------------------------------

_THIS_MACHINE = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}


def _host_kind(host):
    """Where an HQPlayer is, never its name — a machine's name is often its
    owner's: this machine, or another one (an address, the scrubber's kind)."""
    if not host:
        return host
    if str(host).lower() in _THIS_MACHINE:
        return "this machine"
    scrubbed = scrub_personal(str(host))
    return scrubbed if scrubbed != str(host) else "another machine"


def _without_names(key: str, value):
    """The outputs a node chose, by kind: a renderer's or a sound device's own
    name is often its owner's ("…'s iPhone", "…'s AirPods")."""
    if key in ("output.dlna_renderer", "output.dlna_pinned"):
        if isinstance(value, dict):
            return {k: v for k, v in value.items() if k != "name"}
        if isinstance(value, list):
            return [{k: v for k, v in r.items() if k != "name"} if isinstance(r, dict) else r
                    for r in value]
    if key == "output.local_device" and isinstance(value, str):
        return value.split("::", 1)[0]
    if key == "hqplayer.host":
        return _host_kind(value)
    return value


def settings_allowlisted(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT key, value, updated_at FROM user_settings
             WHERE key = ANY(%s) OR key LIKE ANY(%s)
             ORDER BY key
        """, (list(SETTINGS_KEYS), [p + "%" for p in SETTINGS_KEY_PREFIXES]))
        return {k: {"value": _without_names(k, v), "updated_at": _iso(u)}
                for k, v, u in cur.fetchall()}


def config_allowlisted(config: dict) -> dict:
    """config.json's facts, names left out: a node's name is often its
    owner's, a Last.fm username always is, an HQPlayer's host is a machine's."""
    out = {k: config.get(k) for k in CONFIG_KEYS if k in config}
    if isinstance(out.get("hqplayer"), dict):
        out["hqplayer"] = {**out["hqplayer"], "host": _host_kind(out["hqplayer"].get("host"))}
    p2p = config.get("p2p") or {}
    out["p2p"] = {k: p2p.get(k) for k in CONFIG_P2P_KEYS if k in p2p}
    if "openai_compat" in config:
        compat = config["openai_compat"] or {}
        out["openai_compat"] = {k: compat.get(k) for k in ("base_url", "model", "name") if k in compat}
    if "lastfm" in config:
        out["lastfm"] = {"authorized": bool((config["lastfm"] or {}).get("username"))}
    return out


# ---------------------------------------------------------------------------
# p2p / jobs / chat / logs
# ---------------------------------------------------------------------------

def p2p_facts(conn, sources: Sources) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*),
                   count(*) FILTER (WHERE is_blocked),
                   count(*) FILTER (WHERE public_key_hex LIKE 'pending:%'),
                   max(last_seen)
              FROM friends
        """)
        total, blocked, pending, last_seen = cur.fetchone()
        cur.execute("""
            SELECT public_key_hex, added_at, last_seen, is_blocked, source::text
              FROM friends WHERE public_key_hex = %s OR invite_code = %s
        """, (MASTER_PUBKEY_HEX, MASTER_INVITE_CODE))
        master = cur.fetchone()
        cur.execute("SELECT status::text, count(*) FROM p2p_identities GROUP BY 1")
        identities = dict(cur.fetchall())
        cur.execute("SELECT source, cooldown_until, reason, strikes FROM external_api_cooldown")
        cooldowns = [{"source": s, "until": _iso(u), "reason": r, "strikes": n}
                     for s, u, r, n in cur.fetchall()]
        cur.execute("SELECT count(*) FROM mb_slice_fetches")
        slice_fetches = cur.fetchone()[0]
    return {
        "friends": {"total": total, "blocked": blocked, "pending": pending,
                    "last_seen": _iso(last_seen)},
        "master_contact": {
            "resolved": not master[0].startswith("pending:"), "added_at": _iso(master[1]),
            "last_seen": _iso(master[2]), "blocked": master[3], "source": master[4],
        } if master else None,
        "identities": identities,
        "api_cooldowns": cooldowns,
        "mb_slice_fetches": slice_fetches,
        "settings": _settings_values(conn, (
            "p2p.reachability", "p2p.reachability_detail", "p2p.reachability_checked_at",
            "p2p.identity", "p2p.bound_identity", "p2p.load", "p2p.gate",
            "p2p.gate_mode", "p2p.relay_enabled",
            "p2p.relay_pubkeys", "p2p.master_removed", "sync.last_at",
            "sync.last_items_received", "sync.announce_limit", "sync.carry_limit")),
        "sync": sources.backend.get("/api/settings/sync") if sources.backend else None,
        **sources.extra,
    }


def _without_folder_names(library: Optional[dict], scan: Optional[dict]) -> tuple:
    """The Library's state as a support bundle carries it without the
    person's tick: each folder its state and counts, never its name or a
    disk's label (a stranger's text, a person's own words), a new disk only
    counted; a scan's lines with the folders' names cut, a plan without its
    folder's name (LIBRARY-FOLDERS-1)."""
    names = sorted(({f.get("name") or "" for f in (library or {}).get("folders") or []}
                    | {d.get("label") or "" for d in (library or {}).get("new_disks") or []}) - {""},
                   key=len, reverse=True)

    def cut(job: Optional[dict]) -> Optional[dict]:
        if not job:
            return job
        job = dict(job)
        for name in names:
            if isinstance(job.get("progress"), str):
                job["progress"] = job["progress"].replace(name, "<folder>")
        if isinstance(job.get("prune_plan"), dict):
            job["prune_plan"] = {k: v for k, v in job["prune_plan"].items() if k != "name"}
        if isinstance(job.get("result"), dict) and isinstance(job["result"].get("prune_plan"), dict):
            job["result"] = {**job["result"], "prune_plan": {k: v for k, v in job["result"]["prune_plan"].items()
                                                             if k != "name"}}
        return job
    if library:
        library = {**library,
                   "folders": [{k: f.get(k) for k in ("connected", "disk", "albums", "tracks", "away_copies")}
                               for f in library.get("folders") or []],
                   "new_disks": len(library.get("new_disks") or []),
                   "scan": cut(library.get("scan"))}
    return library, cut(scan)


def jobs_facts(conn, sources: Sources) -> dict:
    def backend(path: str) -> Optional[dict]:
        return sources.backend.get(path) if sources.backend else None

    with conn.cursor() as cur:
        cur.execute("SELECT fetch_status::text, count(*) FROM external_metadata GROUP BY 1")
        fetch_status = dict(cur.fetchall())
        cur.execute("""
            SELECT left(error_message, 200), count(*) FROM external_metadata
             WHERE error_message IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 20
        """)
        errors = [{"error": e, "count": n} for e, n in cur.fetchall()]
        cur.execute("SELECT filename, applied_at FROM _schema_migrations ORDER BY id")
        migrations = [{"filename": f, "applied_at": _iso(a)} for f, a in cur.fetchall()]
    library, scan = _without_folder_names(backend("/api/settings/library"), backend("/scan/status"))
    return {
        "stats": backend("/stats"),
        "library": library,
        "scan": scan,
        "enrich": backend("/enrich/status"),
        "musicbrainz": backend("/api/settings/musicbrainz/status"),
        "external_metadata": {"by_status": fetch_status, "top_errors": errors},
        "migrations": migrations,
    }


def chat_export(conn, since: datetime) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT s.id, s.title, s.created_at,
                   m.id, m.role::text, m.content, m.model, m.retrieval_log,
                   m.tracks_retrieved, m.is_not_relevant, m.feedback_comment, m.created_at
              FROM chat_messages m
              JOIN chat_sessions s ON s.id = m.session_id
             WHERE m.created_at >= %s
             ORDER BY s.id, m.id
             LIMIT %s
        """, (since, CHAT_MAX_MESSAGES))
        rows = cur.fetchall()
    sessions: dict = {}
    for (sid, title, s_created, mid, role, content, model, rlog,
         tracks, not_relevant, feedback, created) in rows:
        session = sessions.setdefault(sid, {
            "id": sid, "title": title, "created_at": _iso(s_created), "messages": []})
        session["messages"].append({
            "id": mid, "role": role, "content": content, "model": model,
            "retrieval_log": rlog, "tracks_retrieved": tracks,
            "not_relevant": not_relevant, "feedback": feedback, "created_at": _iso(created),
        })
    return {"since": _iso(since), "truncated": len(rows) >= CHAT_MAX_MESSAGES,
            "sessions": list(sessions.values())}


def tail(path: Path, limit: int = LOG_TAIL_BYTES) -> Tuple[str, bool]:
    """The last `limit` bytes of a text file, cut at a line boundary."""
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size > limit:
            f.seek(size - limit)
            data = f.read().split(b"\n", 1)[-1]
            truncated = True
        else:
            data = f.read()
            truncated = False
    return data.decode("utf-8", errors="replace"), truncated


# ---------------------------------------------------------------------------
# The bundle
# ---------------------------------------------------------------------------

def collect(*, db_dsn: str, sources: Sources, warrant: dict, granted: Iterable[str],
            node_pubkey: str) -> bytes:
    """A tar.gz (in memory, bounded by BUNDLE_MAX_BYTES) with one entry per
    scope it carries plus manifest.json. Of the content scopes the warrant
    asked for it carries only those the person `granted` on the request
    sheet; the manifest names the ones they left out (`withheld`), so the
    desk knows to ask in the support chat, and what this kind of node cannot
    give (`unavailable`, the sources' — no host agent, an agent that did not
    answer), so a missing part never reads as a refusal. The events' log
    tails ride with `logs`. A scope that fails to collect is reported in the
    manifest and the rest still ships — a broken collector must not void the
    bundle the maintainer is waiting for."""
    asked = set(warrant["scopes"])
    scopes = {s for s in asked if s not in CONTENT_SCOPES or s in set(granted)}
    now = datetime.now(timezone.utc)
    since = (datetime.fromtimestamp(int(warrant["since"]), tz=timezone.utc)
             if warrant.get("since") else now - timedelta(days=CHAT_DEFAULT_DAYS))
    manifest = {"v": PROTOCOL_VERSION, "warrant_id": warrant["id"], "node": node_pubkey,
                "scopes": sorted(scopes), "withheld": sorted(asked - scopes),
                "runtime": sources.runtime,
                # A reason may quote the host agent's error, a path in it
                "unavailable": {part: scrub_personal(why) for part, why in sources.unavailable.items()
                                if part.split(".", 1)[0] in scopes},
                "since": _iso(since), "collected_at": _iso(now),
                "entries": [], "errors": {}}
    buf = io.BytesIO()
    conn = psycopg2.connect(db_dsn)
    conn.autocommit = True
    try:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            def add(name: str, payload, truncated: bool = False) -> None:
                data = payload if isinstance(payload, bytes) else payload.encode("utf-8")
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mtime = int(now.timestamp())
                tar.addfile(info, io.BytesIO(data))
                manifest["entries"].append({"name": name, "bytes": len(data),
                                            "truncated": truncated})

            def scope(name: str, fn) -> None:
                if name not in scopes:
                    return
                try:
                    fn()
                except Exception as e:
                    logger.warning("diag bundle: scope %s failed: %s", name, e)
                    manifest["errors"][name] = scrub_personal(str(e))[:300]

            # The structural scopes are facts about the node, never where its
            # files are or whom it talks to: every string in them is scrubbed
            # (scrub_detail) — a folder becomes "[path]", an address its kind.
            def _system() -> None:
                facts = system_facts(sources, conn)
                if sources.host is not None:
                    facts["host"] = sources.host
                add("system.json", _json(scrub_detail(facts)))
            scope("system", _system)
            scope("settings", lambda: add("settings.json", _json(scrub_detail({
                "user_settings": settings_allowlisted(conn),
                "config": config_allowlisted(sources.config)}))))

            def _p2p() -> None:
                add("p2p.json", _json(scrub_detail(p2p_facts(conn, sources))))
                add("p2p_contact_log.txt", scrub_personal(contact_log.report(conn, 7)))
            scope("p2p", _p2p)
            scope("jobs", lambda: add("jobs.json", _json(scrub_detail(jobs_facts(conn, sources)))))
            def _events() -> None:
                rows = diag_events.recent(conn, EVENTS_MAX_ROWS)
                if "logs" not in scopes:
                    rows = [{**r, "detail": strip_local_only(r["detail"])} for r in rows]
                add("events.json", _json(scrub_detail(rows)))
            scope("events", _events)

            def _logs() -> None:
                for name, text, truncated in sources.logs():
                    add(f"logs/{name}", scrub_personal(text), truncated)
            scope("logs", _logs)
            scope("chat", lambda: add("chat/sessions.json", _json(chat_export(conn, since))))

            def _playback() -> None:
                # The traces live in the backend process's memory only. A fresh
                # tail of an Embedded box's log rides along: give it time.
                data = (sources.backend.get("/api/player/diagnostics/hqplayer/bundle", timeout=30)
                        if sources.backend else None)
                if data is None:
                    raise RuntimeError("the backend did not return its playback traces")
                add("playback/hqplayer.json", _json(scrub_detail(data)))
            scope("playback", _playback)
            add("manifest.json", _json(manifest))
    finally:
        conn.close()
    data = buf.getvalue()
    if len(data) > BUNDLE_MAX_BYTES:
        raise ValueError(f"bundle is {len(data)} bytes, cap is {BUNDLE_MAX_BYTES}")
    return data


# ---------------------------------------------------------------------------
# The report a person sends by hand
# ---------------------------------------------------------------------------

def save_report(*, data_dir: Path, config: dict, state: str, detail: str) -> Path:
    """The node's own report for when the network cannot ask for a bundle:
    services that did not start leave no backend, often no PostgreSQL, and no
    P2P for a warrant to arrive on. One text file under <data_dir>/reports
    for the user to send by mail or a messenger — the `system` facts and the
    tails of the `logs` scope, scrubbed the same way. Written in the data dir
    because Desktop and Downloads are behind a macOS privacy prompt; the
    caller reveals it in the file manager."""
    now = datetime.now().astimezone()
    try:
        system = _json(system_facts(launcher_sources(config, data_dir, probe_backend=False)))
    except Exception as e:
        # The logs are what the report is for; a fact collector that broke
        # must not take them down with it — the report says what is missing.
        logger.warning("diag report: system facts failed: %s", e)
        system = f"(not collected: {e})"
    parts = ["Sautium diagnostic report",
             f"Created: {now:%Y-%m-%d %H:%M:%S %z}",
             f"State: {state}",
             f"Detail: {detail or '-'}",
             "", "== system ==", system]
    for name in LOG_FILES:
        try:
            text, truncated = tail(data_dir / name, REPORT_LOG_TAIL_BYTES)
        except FileNotFoundError:
            continue                    # not every install writes every log
        except OSError as e:
            # One log that cannot be read must not cost the others.
            logger.warning("diag report: %s not read: %s", name, e)
            parts += ["", f"== {name} ==", f"(not read: {e})"]
            continue
        cut = f" (last {REPORT_LOG_TAIL_BYTES // 1024} KB)" if truncated else ""
        parts += ["", f"== {name}{cut} ==", text]
    out_dir = data_dir / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"sautium-report-{now:%Y%m%d-%H%M%S}.txt"
    out.write_text(scrub_personal("\n".join(parts)), encoding="utf-8")
    logger.info("Diagnostic report saved: %s", out)
    return out


def save_and_show(*, data_dir: Path, config: dict, state: str, detail: str) -> str:
    """The action behind both "Save Report for Support" buttons (launcher,
    wizard): save_report, then the file manager on the file. Returns the line
    the window shows — the path itself when the file manager would not open,
    so a report that was written is never reported lost. Raises only when the
    report could not be written."""
    from desktop.utils import show_in_file_manager
    path = save_report(data_dir=data_dir, config=config, state=state, detail=detail)
    try:
        show_in_file_manager(path)
    except OSError as e:
        logger.warning("Diagnostic report saved, file manager not opened: %s", e)
        return f"Report saved: {path}"
    return "Saved and selected in the file manager — attach it to your message"
