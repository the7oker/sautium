#!/usr/bin/env python3
"""MCP server for the Sautium support desk — the maintainer's side of
support diagnostics (P2P_NETWORK.md § "Support diagnostics"), driven from
Claude Code on the master's host.

Everything goes through the master backend's /api/support/* and
/api/p2p/* routes over the same HMAC-signed HTTPS mcp/assistant_server.py
uses — this process holds no key of its own. A bundle is opened ON the
master (decrypted + unpacked under data/support/, a bind mount) and the
tool returns the HOST path, so Claude Code reads the files directly.

All logging goes to stderr (stdout is the STDIO MCP transport).
"""

import hashlib
import hmac
import json
import logging
import os
import sys
import time
import urllib.parse
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("support-mcp")

backend_path = os.environ.get("BACKEND_PATH", os.path.join(os.path.dirname(__file__), "..", "backend"))
BACKEND_URL = os.getenv("BACKEND_URL", "https://localhost:8800")
# The master runs in Docker: /app/data is the repo's data/ on the host.
CONTAINER_DATA = "/app/data"
HOST_DATA = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))

SCOPES = ("system", "settings", "p2p", "jobs", "events", "logs", "chat")


# -- Signed backend HTTP (mirror of mcp/assistant_server.py) ------------------

_API_SECRET: bytes | None = None


def _api_secret() -> bytes:
    global _API_SECRET
    if _API_SECRET is None:
        identity_dir = os.environ.get("P2P_IDENTITY_DIR") or os.path.join(
            backend_path, "data", "node_identity")
        with open(os.path.join(identity_dir, ".api_secret"), encoding="ascii") as f:
            _API_SECRET = f.read().strip().encode("ascii")
    return _API_SECRET


def _signed(method: str, path_q: str, payload: bytes) -> dict:
    ts = str(int(time.time()))
    canonical = f"{method}\n{path_q}\n{ts}\n{hashlib.sha256(payload).hexdigest()}"
    sig = hmac.new(_api_secret(), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return {"x-sautium-ts": ts, "x-sautium-sig": sig}


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = None
        raise RuntimeError(detail or f"backend returned {resp.status_code}")


def _backend_get(path: str, params: Optional[dict] = None):
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "", [], ())}
    qs = urllib.parse.urlencode(clean, doseq=True)
    path_q = f"{path}?{qs}" if qs else path
    with httpx.Client(base_url=BACKEND_URL, timeout=60.0, verify=False) as client:
        resp = client.get(path_q, headers=_signed("GET", path_q, b""))
        _raise_for(resp)
        return resp.json()


def _backend_post(path: str, body: Optional[dict] = None, timeout: float = 120.0):
    payload = json.dumps(body or {}).encode("utf-8")
    with httpx.Client(base_url=BACKEND_URL, timeout=timeout, verify=False) as client:
        resp = client.post(path, content=payload, headers={
            **_signed("POST", path, payload), "content-type": "application/json"})
        _raise_for(resp)
        return resp.json()


def _backend_delete(path: str):
    with httpx.Client(base_url=BACKEND_URL, timeout=60.0, verify=False) as client:
        resp = client.delete(path, headers=_signed("DELETE", path, b""))
        _raise_for(resp)
        return resp.json()


def _host_path(container_path: str) -> str:
    if container_path.startswith(CONTAINER_DATA):
        return HOST_DATA + container_path[len(CONTAINER_DATA):]
    return container_path


def _dump(obj) -> str:
    return json.dumps(obj, indent=1, ensure_ascii=False, default=str)


def _friends() -> list:
    data = _backend_get("/api/p2p/friends", {"limit": 200})
    return list(data.get("pinned", [])) + list(data.get("items", []))


def _friend(node: str) -> dict:
    node = node.strip()
    matches = [f for f in _friends()
               if f.get("username") == node or str(f.get("public_key_hex", "")).startswith(node.lower())]
    if not matches:
        raise RuntimeError(f"no friend matches {node!r}")
    if len(matches) > 1:
        raise RuntimeError(f"{node!r} matches {len(matches)} friends — use a longer pubkey prefix")
    return matches[0]


# -- MCP server ---------------------------------------------------------------

mcp = FastMCP(
    "Sautium Support",
    instructions="The Sautium master's support desk: which user nodes are alive "
                 "and how, their event reports, support chat threads, and the "
                 "signed diagnostic warrants that fetch logs/dialogs from a node.",
)


@mcp.tool()
def support_nodes() -> str:
    """Every node the master has heard from: username, last report, last
    start (build, OS, language, hardware profile, agent auth states, unclean shutdown),
    unresolved AI-agent sign-ins, open warrants, stored bundles and how many
    problem events came in the last 7 days. Start here."""
    try:
        return _dump(_backend_get("/api/support/nodes"))
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def support_reports(node: str = "", kind: str = "", since: str = "", limit: int = 50) -> str:
    """Event reports nodes sent on their own (newest first). `node` is a
    username or pubkey prefix; `kind` one of node.started, service.start_failed,
    p2p.start_failed, backend.crashed, backend.restarted, backend.gave_up,
    agent.signin_opened, agent.signin_timeout, agent.state_changed, chat.error,
    sync.failed, sync.import_failed, update.failed; `since` ISO 8601. Reports carry states,
    counters and error strings — never logs or dialogs (those need a warrant)."""
    try:
        return _dump(_backend_get("/api/support/reports",
                                  {"node": node, "kind": kind, "since": since, "limit": limit}))
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def support_threads(unread_only: bool = False) -> str:
    """Support chat threads: one per user node (every node auto-friends the
    master). Shows unread counts, last activity and online state."""
    try:
        rows = []
        for f in _friends():
            if unread_only and not f.get("unread_count"):
                continue
            rows.append({
                "friend_id": f.get("id"), "username": f.get("username"),
                "display_name": f.get("display_name"),
                "pubkey": str(f.get("public_key_hex", ""))[:16],
                "unread": f.get("unread_count"), "online": f.get("is_online"),
                "last_activity_at": f.get("last_activity_at"),
                "last_seen": f.get("last_seen"), "blocked": f.get("is_blocked"),
            })
        return _dump(rows)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def support_messages(node: str, limit: int = 50) -> str:
    """The support chat with one node (oldest first): direction 'in' is the
    user, 'out' is the master. `node` = username or pubkey prefix."""
    try:
        friend = _friend(node)
        msgs = _backend_get(f"/api/p2p/friends/{friend['id']}/messages", {"limit": limit})
        return _dump({"friend": friend.get("username"), "messages": msgs})
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def support_reply(node: str, text: str) -> str:
    """Send a support chat message to a node as the master. Delivery is the
    normal chat path (direct, relay or the node's next history pull)."""
    try:
        friend = _friend(node)
        res = _backend_post(f"/api/p2p/friends/{friend['id']}/send", {"content": text})
        return f"Queued for {friend.get('username')}: message {res.get('message_uuid')}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def support_issue_warrant(node: str, scopes: Optional[list] = None, since: str = "",
                          note: str = "", expires_hours: int = 168) -> str:
    """Ask a node for a diagnostic bundle. `scopes` ⊆ system, settings, p2p,
    jobs, events, logs, chat (default: all); `since` (ISO 8601) bounds the
    chat/events window (default 14 days). The warrant is signed by the
    master, bound to that node, single-use there, and rides the node's own
    wake stream — `dispatched: true` means the node is connected now,
    otherwise it is parked until the node next subscribes (up to
    expires_hours). The bundle arrives encrypted; see support_bundles /
    support_open_bundle."""
    try:
        body = {"node": node, "scopes": list(scopes or SCOPES), "since": since or None,
                "note": note or None, "expires_hours": expires_hours}
        return _dump(_backend_post("/api/support/warrants", body))
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def support_warrants(node: str = "", open_only: bool = False) -> str:
    """Issued warrants with their dispatch / fulfilment state."""
    try:
        return _dump(_backend_get("/api/support/warrants",
                                  {"node": node, "open_only": open_only}))
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def support_bundles(node: str = "") -> str:
    """Bundles received (still encrypted until opened): id, node, size,
    scopes, when, and the unpack path if already opened."""
    try:
        return _dump(_backend_get("/api/support/bundles", {"node": node}))
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def support_open_bundle(bundle_id: str) -> str:
    """Decrypt and unpack a bundle on the master; returns the manifest and
    the HOST directory holding system.json, settings.json, p2p.json,
    jobs.json, events.json, logs/*.log and chat/sessions.json — Read/Grep
    them from there."""
    try:
        res = _backend_post(f"/api/support/bundles/{bundle_id}/open")
        res["host_path"] = _host_path(res.get("path", ""))
        return _dump(res)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def support_delete_bundle(bundle_id: str) -> str:
    """Remove a bundle and its unpacked files from the master."""
    try:
        return _dump(_backend_delete(f"/api/support/bundles/{bundle_id}"))
    except Exception as e:
        return f"Error: {e}"


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
