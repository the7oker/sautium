"""Support diagnostics — the launcher's bundle collector
(desktop/p2p/diag_protocol.py, P2P_NETWORK.md § "Support diagnostics").

A warrant names scopes; every scope below is a fixed collector over fixed
sources, and settings / config.json pass through ALLOWLISTS. Nothing here
takes a path, a table or a query from the wire.

Never in a bundle: p2p_messages (friends' private E2E chat — the master
already has its own thread with this node), credentials of any kind
(ai.api_key, the Last.fm session, config.json api_keys / postgres_password,
node keys, TLS keys, .api_secret, the agents' auth files). Log tails go
through diag_protocol.scrub_secrets.
"""

import io
import json
import logging
import platform
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

import psycopg2

from desktop import os_locale
from desktop.api_client import BackendAPIClient
from desktop.p2p import contact_log, diag_events
from desktop.p2p.diag_protocol import (
    BUNDLE_MAX_BYTES, CHAT_DEFAULT_DAYS, EVENTS_MAX_ROWS, LOG_TAIL_BYTES,
    PROTOCOL_VERSION, scrub_secrets,
)
from desktop.p2p.master_node import MASTER_INVITE_CODE, MASTER_PUBKEY_HEX

logger = logging.getLogger(__name__)

SETTINGS_KEY_PREFIXES = ("sync.", "p2p.", "enrichment.", "output.", "albums.",
                         "musicbrainz.", "library.", "hardware.", "support.")
SETTINGS_KEYS = ("ai.provider", "ai.model", "ai.canonization_enabled",
                 "hqplayer.host", "hqplayer.port", "ui.language")
CONFIG_KEYS = ("version", "music_path", "provider", "hqplayer", "ports",
               "claude_code_available", "codex_available", "first_run_complete",
               "sync", "mb_slice")
CONFIG_P2P_KEYS = ("node_name", "listen_port", "docker_ports", "chat_enabled")
LOG_FILES = ("launcher.log", "backend.log", "backend.log.1", "pgdata/server.log")
CHAT_MAX_MESSAGES = 5000
SYSTEM_SETTINGS_KEYS = ("p2p.reachability", "p2p.reachability_detail", "p2p.identity",
                        "sync.last_at", "sync.last_items_received", "hardware.lite_streak",
                        "ui.language")


def _json(obj) -> str:
    return json.dumps(obj, indent=1, ensure_ascii=False, default=str)


def _iso(value) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _backend_client(config: dict) -> BackendAPIClient:
    client = BackendAPIClient()
    client.set_port(config.get("ports", {}).get("web", 18000))
    return client


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

def system_facts(config: dict, conn=None) -> dict:
    """Build, machine, tools and agent states. Backend-owned facts come over
    the signed local API and read None while the backend is not answering."""
    from desktop import updater
    from desktop.utils import (detect_claude_cli, detect_codex_cli, detect_gpu,
                               detect_node_version)

    gpu_ok, gpu_name, vram_gb = detect_gpu()
    node_ver = detect_node_version()
    client = _backend_client(config)
    cfg = client._get_json("/config") or {}
    ai = client._get_json("/api/settings/ai") or {}
    health = client._get_json("/health") or {}
    facts = {
        "commit": updater.current_commit(),
        "build": updater.installed_build(),
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
        "hardware": client._get_json("/api/settings/hardware"),
        "agents": {
            "provider": ai.get("provider"),
            "model": ai.get("model"),
            "auth_state": ai.get("auth_state"),
            "claude": (client._get_json("/api/settings/ai/claude/state") or {}).get("state"),
            "codex": (client._get_json("/api/settings/ai/codex/state") or {}).get("state"),
        },
        "backend": {"status": health.get("status"), "checks": health.get("checks")},
        "ports": config.get("ports"),
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

def settings_allowlisted(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT key, value, updated_at FROM user_settings
             WHERE key = ANY(%s) OR key LIKE ANY(%s)
             ORDER BY key
        """, (list(SETTINGS_KEYS), [p + "%" for p in SETTINGS_KEY_PREFIXES]))
        return {k: {"value": v, "updated_at": _iso(u)} for k, v, u in cur.fetchall()}


def config_allowlisted(config: dict) -> dict:
    out = {k: config.get(k) for k in CONFIG_KEYS if k in config}
    p2p = config.get("p2p") or {}
    out["p2p"] = {k: p2p.get(k) for k in CONFIG_P2P_KEYS if k in p2p}
    compat = config.get("openai_compat") or {}
    out["openai_compat"] = {k: compat.get(k) for k in ("base_url", "model", "name") if k in compat}
    lastfm = config.get("lastfm") or {}
    out["lastfm"] = {"username": lastfm.get("username"),
                     "authorized": bool(lastfm.get("session_key"))}
    return out


# ---------------------------------------------------------------------------
# p2p / jobs / chat / logs
# ---------------------------------------------------------------------------

def p2p_facts(conn, config: dict, extra: Optional[dict] = None) -> dict:
    client = _backend_client(config)
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
            "p2p.identity", "p2p.load", "p2p.gate", "p2p.gate_mode", "p2p.relay_enabled",
            "p2p.relay_pubkeys", "p2p.master_removed", "sync.last_at",
            "sync.last_items_received", "sync.announce_limit", "sync.carry_limit")),
        "sync": client._get_json("/api/settings/sync"),
        **(extra or {}),
    }


def jobs_facts(conn, config: dict) -> dict:
    client = _backend_client(config)
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
    return {
        "stats": client.get_stats(),
        "library": client._get_json("/api/settings/library"),
        "scan": client.scan_status(),
        "enrich": client.enrich_status(),
        "musicbrainz": client._get_json("/api/settings/musicbrainz/status"),
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

def collect(*, db_dsn: str, data_dir: Path, config: dict, warrant: dict,
            node_pubkey: str, extra: Optional[dict] = None) -> bytes:
    """A tar.gz (in memory, bounded by BUNDLE_MAX_BYTES) with one entry per
    requested scope plus manifest.json. A scope that fails to collect is
    reported in the manifest and the rest still ships — a broken collector
    must not void the bundle the maintainer is waiting for."""
    scopes = set(warrant["scopes"])
    now = datetime.now(timezone.utc)
    since = (datetime.fromtimestamp(int(warrant["since"]), tz=timezone.utc)
             if warrant.get("since") else now - timedelta(days=CHAT_DEFAULT_DAYS))
    manifest = {"v": PROTOCOL_VERSION, "warrant_id": warrant["id"], "node": node_pubkey,
                "scopes": sorted(scopes), "since": _iso(since), "collected_at": _iso(now),
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
                    manifest["errors"][name] = str(e)[:300]

            scope("system", lambda: add("system.json", _json(system_facts(config, conn))))
            scope("settings", lambda: add("settings.json", _json({
                "user_settings": settings_allowlisted(conn),
                "config": config_allowlisted(config)})))

            def _p2p() -> None:
                add("p2p.json", _json(p2p_facts(conn, config, extra)))
                add("p2p_contact_log.txt", contact_log.report(conn, 7))
            scope("p2p", _p2p)
            scope("jobs", lambda: add("jobs.json", _json(jobs_facts(conn, config))))
            scope("events", lambda: add("events.json",
                                        _json(diag_events.recent(conn, EVENTS_MAX_ROWS))))

            def _logs() -> None:
                for name in LOG_FILES:
                    path = Path(data_dir) / name
                    if not path.exists():
                        continue
                    text, truncated = tail(path)
                    add(f"logs/{Path(name).name}", scrub_secrets(text), truncated)
            scope("logs", _logs)
            scope("chat", lambda: add("chat/sessions.json", _json(chat_export(conn, since))))
            add("manifest.json", _json(manifest))
    finally:
        conn.close()
    data = buf.getvalue()
    if len(data) > BUNDLE_MAX_BYTES:
        raise ValueError(f"bundle is {len(data)} bytes, cap is {BUNDLE_MAX_BYTES}")
    return data
