"""
Configuration manager for Sautium desktop launcher.

Reads/writes config from %APPDATA%/Sautium/config.json.
Generates .env file for the backend process.
"""

import json
import logging
import os
import random
import secrets
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Default config schema version
CONFIG_VERSION = 1

DEFAULT_CONFIG = {
    "version": CONFIG_VERSION,
    "music_path": "",
    "provider": "none",
    "api_keys": {
        "anthropic": None,
        "openai": None,
    },
    "openai_compat": {
        "base_url": None,
        "api_key": None,
        "model": None,
        "name": None,
    },
    "hqplayer": {
        # Off until the owner picks HQPlayer in the Output picker. Setup used to
        # ask with the box pre-ticked, which made _hqp_configured() true and let
        # manager's legacy branch hand a brand-new node to HQPlayer — an output
        # most people do not own. Existing installs keep their saved value.
        "enabled": False,
        "host": "localhost",
        "port": 4321,
    },
    "ports": {
        "postgres": 15432,
        "web": 18000,
        "tracker": 18765,
        # Plain-http media surfaces, offset from the Docker node's 8830/8831
        # (held by its netsh portproxy even while the container is down) so
        # a launcher test node can drive DLNA on the same host.
        "media": 8832,
        "gena": 8833,
        # Peer surface of the BACKEND (backend/p2p_app.py) — off in launcher
        # mode. A launcher already serves the peer protocol from its own
        # aiohttp sync server on p2p.listen_port, which is randomised per
        # install precisely so several Sautium instances can share a network;
        # a second surface on a fixed port would both duplicate it and
        # reintroduce that collision. Docker has no launcher, which is the
        # only reason the backend grew this surface at all.
        "p2p_sync": 0,
    },
    "claude_code_available": False,
    "first_run_complete": False,
    "postgres_password": None,  # auto-generated on first run
    "lastfm": {
        "username": None,
        "session_key": None,
    },
    "p2p": {
        "node_name": None,
        "listen_port": None,  # auto-generated on first run (random port)
        # Localhost ports probed for a Docker node on this host — its peer
        # surface only. 8800 must never appear here: it serves the Web UI,
        # whose page carries the API secret, and a probed port becomes a
        # peer address we hand out in LAN beacons and dial for chat. It
        # answers /health with type "sautium-peer" (one health route for
        # both apps), so nothing downstream would catch the mistake.
        "docker_ports": [8801],
        "manual_peers": [],
        "chat_enabled": True,
    },
    "sync": {},
    "mb_slice": {
        "serve": True,   # answer /api/mb/slice (effective only with a full local dump)
        "fetch": True,   # request slices from dump peers when we have no dump
        "batch_size": 20,
        "auto_interval_min": 360,
    },
}


def get_config_dir() -> Path:
    """Get the config directory path (%APPDATA%/Sautium on Windows)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    config_dir = base / "Sautium"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_config_path() -> Path:
    """Get the config file path."""
    return get_config_dir() / "config.json"


def get_data_dir() -> Path:
    """Get the data directory (for PostgreSQL data, cache, etc.)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    data_dir = base / "Sautium"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def load_config() -> dict:
    """Load config from disk, merging with defaults for any missing keys."""
    config_path = get_config_path()

    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            config = _deep_merge(DEFAULT_CONFIG.copy(), saved)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Failed to load config: {e}")
            config = DEFAULT_CONFIG.copy()
    else:
        config = DEFAULT_CONFIG.copy()

    # Auto-generate postgres password if not set
    changed = False
    if not config.get("postgres_password"):
        config["postgres_password"] = secrets.token_urlsafe(16)
        changed = True

    # Auto-generate random P2P listen port (unique per instance)
    # Range 20000-29999 avoids common service ports
    p2p = config.get("p2p", {})
    if not p2p.get("listen_port"):
        p2p["listen_port"] = random.randint(20000, 29999)
        config["p2p"] = p2p
        changed = True
        logger.info(f"Generated random P2P port: {p2p['listen_port']}")

    # Migrate configs written before the peer-port split: 8800 is the Web
    # UI, never a peer address (see docker_ports in DEFAULT_CONFIG).
    docker_ports = p2p.get("docker_ports") or []
    if 8800 in docker_ports:
        p2p["docker_ports"] = [p for p in docker_ports if p != 8800]
        config["p2p"] = p2p
        changed = True
        logger.info("Dropped 8800 from p2p.docker_ports (Web UI port)")

    if changed:
        save_config(config)

    return config


def save_config(config: dict) -> None:
    """Save config to disk."""
    config_path = get_config_path()
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        logger.info(f"Config saved to {config_path}")
    except OSError as e:
        logger.error(f"Failed to save config: {e}")
        raise


def update_config(updates: dict) -> dict:
    """Load config, apply updates, save, and return the updated config."""
    config = load_config()
    config = _deep_merge(config, updates)
    save_config(config)
    return config


def generate_env_file(config: dict, env_path: Path) -> None:
    """
    Generate a .env file for the backend based on current config.

    Key differences from Docker:
    - POSTGRES_HOST = localhost (not 'postgres')
    - MUSIC_LIBRARY_PATH = native Windows path (not '/music')
    - HQPLAYER_HOST = localhost (not 'host.docker.internal')
    - TRACKER_URL = http://localhost:{port} (not 'http://playback-tracker:8765')
    """
    hqp = config.get("hqplayer", {})
    ports = config.get("ports", {})
    lastfm = config.get("lastfm", {})
    api_keys = config.get("api_keys", {})
    compat = config.get("openai_compat", {})

    # Resolve P2P identity directory
    identity_dir = get_config_dir() / "node_identity"

    lines = [
        "# Auto-generated by Sautium Launcher",
        "# Do not edit manually — changes will be overwritten",
        "",
        "# Database",
        f"POSTGRES_HOST=localhost",
        f"POSTGRES_PORT={ports.get('postgres', 5432)}",
        f"POSTGRES_DB=sautium",
        f"POSTGRES_USER=sautium",
        f"POSTGRES_PASSWORD={config.get('postgres_password', 'changeme')}",
        "",
        "# Music Library",
        f"MUSIC_LIBRARY_PATH={config.get('music_path', '')}",
        "",
        "# HQPlayer",
        f"HQPLAYER_HOST={hqp.get('host', 'localhost')}",
        f"HQPLAYER_PORT={hqp.get('port', 4321)}",
        f"HQPLAYER_ENABLED={'true' if hqp.get('enabled') else 'false'}",
        f"TRACKER_URL=http://localhost:{ports.get('tracker', 8765)}",
        "",
        "# AI Providers",
        f"DEFAULT_PROVIDER={config.get('provider', 'anthropic')}",
        f"ANTHROPIC_API_KEY={api_keys.get('anthropic') or ''}",
        f"OPENAI_API_KEY={api_keys.get('openai') or ''}",
        "",
        "# OpenAI-compatible endpoint",
        f"OPENAI_COMPAT_BASE_URL={compat.get('base_url') or ''}",
        f"OPENAI_COMPAT_API_KEY={compat.get('api_key') or ''}",
        f"OPENAI_COMPAT_MODEL={compat.get('model') or ''}",
        f"OPENAI_COMPAT_NAME={compat.get('name') or ''}",
        "",
        "# Claude Code",
        f"CLAUDE_CODE_ENABLED={'true' if config.get('claude_code_available') and config.get('provider') == 'claude_code' else 'false'}",
        "",
        "# OpenAI Codex CLI",
        f"CODEX_CLI_ENABLED={'true' if config.get('codex_available') and config.get('provider') == 'codex' else 'false'}",
        "",
        "# Last.fm (API key/secret are built into the app)",
        f"LASTFM_USERNAME={lastfm.get('username') or ''}",
        f"LASTFM_SESSION_KEY={lastfm.get('session_key') or ''}",
        "",
        "# P2P Identity",
        f"P2P_IDENTITY_DIR={identity_dir}",
        f"P2P_LISTEN_PORT={config.get('p2p', {}).get('listen_port', 0)}",
        "",
        "# Application",
        "LOG_LEVEL=INFO",
        "CUDA_VISIBLE_DEVICES=0",
        "",
        "# Built-in player: load the ASIO-enabled PortAudio DLL. Must be in",
        "# the environment BEFORE sounddevice is first imported; harmless on",
        "# macOS/Linux and for users without ASIO drivers.",
        "SD_ENABLE_ASIO=1",
        "",
        "# Media surfaces (plain-http, LAN-only) — distinct from the Docker",
        "# node's 8830/8831 so both nodes coexist on one host.",
        f"MEDIA_PROXY_PORT={ports.get('media', 8832)}",
        f"DLNA_GENA_PORT={ports.get('gena', 8833)}",
        "",
        "# Backend peer surface: 0 = off, because the launcher's own sync",
        "# server (random port, see p2p.listen_port) already serves peers.",
        f"P2P_SYNC_PORT={ports.get('p2p_sync', 0)}",
        "",
    ]

    try:
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        logger.info(f"Generated .env at {env_path}")
    except OSError as e:
        logger.error(f"Failed to generate .env: {e}")
        raise


def generate_mcp_config(config: dict, output_path: Path) -> None:
    """Generate MCP config (mcp-windows.json) for Claude Code with localhost values."""
    ports = config.get("ports", {})
    hqp = config.get("hqplayer", {})

    # The assistant subprocess spawns the hqplayer server itself, so the command
    # must be a Python that actually has the backend deps (mcp, httpx,
    # psycopg2) — the launcher-provisioned interpreter, the same one uvicorn
    # runs on. A bare "python" from PATH (the original value) either doesn't
    # exist on a fresh machine or lacks the deps, and the server died on
    # spawn SILENTLY: Claude Code just proceeds without that server's tools,
    # so the agent saw prompt descriptions of search/playback/MB tools it
    # did not have. Same class of bug for the script path: assistant_server.py
    # lives in <project_root>/mcp/, not <backend>/mcp/.
    from desktop.python_env import get_backend_python
    backend_dir = output_path.parent          # the runner reads the file from backend/
    project_root = backend_dir.parent

    mcp_config = {
        "mcpServers": {
            "postgres": {
                "command": "npx",
                "args": ["-y", "postgres-mcp-server"],
                "env": {
                    "DB_HOST": "localhost",
                    "DB_PORT": str(ports.get("postgres", 5432)),
                    "DB_USER": "sautium",
                    "DB_PASSWORD": config.get("postgres_password", "changeme"),
                    "DB_NAME": "sautium",
                    "DB_SSL": "false",
                },
            },
            "assistant": {
                "command": get_backend_python(),
                "args": [str(project_root / "mcp" / "assistant_server.py")],
                "env": {
                    "DB_HOST": "localhost",
                    "DB_PORT": str(ports.get("postgres", 5432)),
                    "DB_USER": "sautium",
                    "DB_PASSWORD": config.get("postgres_password", "changeme"),
                    "DB_NAME": "sautium",
                    "HQPLAYER_HOST": hqp.get("host", "localhost"),
                    "HQPLAYER_PORT": str(hqp.get("port", 4321)),
                    "BACKEND_URL": f"https://localhost:{ports.get('web', 8000)}",
                    # Where the server finds shared backend code — explicit,
                    # not derived from the script's own location.
                    "BACKEND_PATH": str(backend_dir),
                    # Where .api_secret lives for signing backend requests.
                    # Must be explicit: codex spawns MCP servers with ONLY
                    # the env configured here, so the launcher backend's
                    # own P2P_IDENTITY_DIR never reaches the server by
                    # inheritance the way it does under claude — without
                    # this line every signed assistant tool (mb_resolve,
                    # playback, …) dies with "missing .api_secret" on
                    # codex while claude looks fine.
                    "P2P_IDENTITY_DIR": str(get_config_dir() / "node_identity"),
                },
            },
        }
    }

    try:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(mcp_config, f, indent=2)
        logger.info(f"Generated MCP config at {output_path}")
    except OSError as e:
        logger.error(f"Failed to generate MCP config: {e}")
        raise


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge override into base, returning a new dict."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
