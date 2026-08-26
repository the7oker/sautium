"""Support diagnostics — the collector's pure parts (desktop/diag_bundle.py)
and the node-local spool / session marker (desktop/p2p/diag_events.py).
The SQL paths run in `python -m desktop.p2p.diag_events --selftest`."""

import json

from desktop import diag_bundle
from desktop.p2p import diag_events


def test_config_allowlist_drops_every_credential():
    config = {
        "version": 3, "music_path": "E:/Music", "provider": "claude_code",
        "api_keys": {"anthropic": "sk-ant-secret", "openai": "sk-openai-secret"},
        "openai_compat": {"base_url": "http://x", "api_key": "compat-secret", "model": "m", "name": "n"},
        "hqplayer": {"enabled": True, "host": "h", "port": 4321},
        "ports": {"postgres": 15432, "web": 18000},
        "postgres_password": "pg-secret",
        "lastfm": {"username": "vale", "session_key": "lastfm-secret", "pending_auth": False},
        "p2p": {"node_name": "n", "listen_port": 20123, "docker_ports": [8801],
                "manual_peers": ["1.2.3.4:5"], "chat_enabled": True},
        "sync": {}, "mb_slice": {"serve": True},
        "claude_code_available": True, "first_run_complete": True,
    }
    out = diag_bundle.config_allowlisted(config)
    flat = json.dumps(out)
    for secret in ("sk-ant-secret", "sk-openai-secret", "compat-secret", "pg-secret",
                   "lastfm-secret", "api_keys", "postgres_password", "session_key",
                   "manual_peers"):
        assert secret not in flat
    assert out["p2p"] == {"node_name": "n", "listen_port": 20123, "docker_ports": [8801],
                          "chat_enabled": True}
    assert out["openai_compat"] == {"base_url": "http://x", "model": "m", "name": "n"}
    assert out["lastfm"] == {"username": "vale", "authorized": True}
    assert out["ports"]["web"] == 18000 and out["hqplayer"]["host"] == "h"


def test_settings_allowlist_never_names_a_credential_key():
    for key in ("ai.api_key", "lastfm.session_key", "auth.token_epoch"):
        assert key not in diag_bundle.SETTINGS_KEYS
        assert not key.startswith(diag_bundle.SETTINGS_KEY_PREFIXES)
    assert "ai.provider" in diag_bundle.SETTINGS_KEYS
    assert "sync.last_at".startswith(diag_bundle.SETTINGS_KEY_PREFIXES)


def test_tail_cuts_at_a_line_boundary(tmp_path):
    path = tmp_path / "backend.log"
    lines = [f"line {i:04d} " + "x" * 40 for i in range(200)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    whole, truncated = diag_bundle.tail(path, limit=10 ** 6)
    assert not truncated and whole.startswith("line 0000")
    part, truncated = diag_bundle.tail(path, limit=1000)
    assert truncated
    assert part.startswith("line ")                 # never a half line
    assert part.endswith(lines[-1] + "\n") and len(part.encode()) <= 1000


def test_spool_and_session_marker(tmp_path):
    assert diag_events.write_session_marker(tmp_path, build="abc") is None
    marker = json.loads((tmp_path / "diag" / "session.json").read_text())
    assert marker["build"] == "abc" and marker["started_at"]
    previous = diag_events.write_session_marker(tmp_path)     # unclean: still there
    assert previous["build"] == "abc"
    diag_events.clear_session_marker(tmp_path)
    assert diag_events.write_session_marker(tmp_path) is None
    diag_events.clear_session_marker(tmp_path)
    diag_events.clear_session_marker(tmp_path)                 # idempotent

    diag_events.spool(tmp_path, "agent.signin_timeout", {"agent": "claude", "source": "wizard"})
    diag_events.spool(tmp_path, "service.start_failed", {"step": "postgres"})
    rows = [json.loads(l) for l in (tmp_path / "diag" / "spool.jsonl").read_text().splitlines()]
    assert [r["kind"] for r in rows] == ["agent.signin_timeout", "service.start_failed"]
    assert rows[0]["detail"]["agent"] == "claude" and rows[0]["ts"]
