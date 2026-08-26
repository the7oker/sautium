"""The support desk — the master's Web-UI-surface API over what user nodes
sent (P2P_NETWORK.md § "Support diagnostics"): a per-node overview, event
reports, warrant issue + dispatch, bundle open / delete. HMAC like
everything on 8800, and 404 unless this node IS the shipped master, so
the routes are inert on every other node. Consumed by
mcp/support_server.py — the maintainer works the desk from Claude Code.

A bundle is stored as received (boxed); `open` decrypts it with the
master's own key and unpacks it under SUPPORT_DIR — a bind mount, so the
files are readable from the host. Plaintext leaves the database only on
that explicit call.
"""

import io
import json
import logging
import os
import shutil
import tarfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from config import settings
from db_pool import db_execute, db_query, db_query_one
from desktop.p2p import diag_protocol
from master_node import MASTER_PUBKEY_HEX
from p2p_identity import load_signing_key, resolve_identity

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/support", tags=["support"])

SUPPORT_DIR = Path(os.getenv("SAUTIUM_SUPPORT_DIR", "/app/data/support"))
REPORT_KEEP_DAYS = 180
BUNDLE_KEEP_DAYS = 30
PROBLEM_KINDS = ("service.start_failed", "p2p.start_failed", "backend.crashed",
                 "backend.gave_up", "agent.signin_timeout", "chat.error",
                 "sync.failed", "sync.import_failed", "update.failed")


def _require_master() -> None:
    ident = resolve_identity(settings)
    if not ident or ident["public_key_hex"].lower() != MASTER_PUBKEY_HEX:
        raise HTTPException(status_code=404, detail="Not found")


def _resolve_node(node: str) -> str:
    """A username or a pubkey prefix → the node's pubkey; 404 unknown,
    409 ambiguous."""
    node = node.strip()
    if not node:
        raise HTTPException(status_code=400, detail="node is required")
    rows = db_query("""
        SELECT pubkey FROM (
            SELECT public_key_hex AS pubkey FROM friends
             WHERE username = %(n)s OR public_key_hex LIKE %(p)s
            UNION
            SELECT node_pubkey FROM support_reports WHERE node_pubkey LIKE %(p)s
        ) x LIMIT 2
    """, {"n": node, "p": node.lower() + "%"})
    if not rows:
        raise HTTPException(status_code=404, detail=f"no node matches {node!r}")
    if len(rows) > 1:
        raise HTTPException(status_code=409, detail=f"{node!r} matches more than one node")
    return rows[0]["pubkey"]


def _out(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}


# ---------------------------------------------------------------------------
# Overview + reports
# ---------------------------------------------------------------------------

_NODES_SQL = """
WITH nodes AS (
    SELECT node_pubkey FROM support_reports
    UNION
    SELECT node_pubkey FROM support_warrants
),
started AS (
    SELECT DISTINCT ON (node_pubkey) node_pubkey, ts, detail
      FROM support_reports WHERE kind = 'node.started'
     ORDER BY node_pubkey, ts DESC
),
last_report AS (
    SELECT DISTINCT ON (node_pubkey) node_pubkey, ts, kind
      FROM support_reports ORDER BY node_pubkey, ts DESC
),
signins AS (
    SELECT DISTINCT ON (node_pubkey, detail->>'agent')
           node_pubkey, detail->>'agent' AS agent, ts
      FROM support_reports WHERE kind = 'agent.signin_opened'
     ORDER BY node_pubkey, detail->>'agent', ts DESC
),
unresolved AS (
    SELECT s.node_pubkey,
           jsonb_agg(jsonb_build_object('agent', s.agent, 'opened_at', s.ts)
                     ORDER BY s.ts) AS items
      FROM signins s
     WHERE NOT EXISTS (
        SELECT 1 FROM support_reports r
         WHERE r.node_pubkey = s.node_pubkey
           AND r.kind = 'agent.state_changed'
           AND r.detail->>'agent' = s.agent
           AND r.detail->>'to' = 'ready'
           AND r.ts > s.ts)
     GROUP BY s.node_pubkey
)
SELECT n.node_pubkey AS pubkey,
       f.username, f.display_name, f.last_seen AS friend_last_seen, f.is_blocked,
       st.ts AS last_started_at, st.detail AS started,
       lr.ts AS last_report_at, lr.kind AS last_report_kind,
       COALESCE(u.items, '[]'::jsonb) AS unresolved_signins,
       (SELECT count(*) FROM support_warrants w
         WHERE w.node_pubkey = n.node_pubkey AND w.fulfilled_at IS NULL
           AND w.expires_at > now()) AS open_warrants,
       (SELECT count(*) FROM support_bundles b
         WHERE b.node_pubkey = n.node_pubkey) AS bundles,
       (SELECT count(*) FROM support_reports r
         WHERE r.node_pubkey = n.node_pubkey
           AND r.ts > now() - interval '7 days'
           AND r.kind = ANY(%(problems)s)) AS problems_7d
  FROM nodes n
  LEFT JOIN friends f ON f.public_key_hex = n.node_pubkey
  LEFT JOIN started st ON st.node_pubkey = n.node_pubkey
  LEFT JOIN last_report lr ON lr.node_pubkey = n.node_pubkey
  LEFT JOIN unresolved u ON u.node_pubkey = n.node_pubkey
 ORDER BY lr.ts DESC NULLS LAST
"""


def _started_summary(detail: Optional[dict]) -> Optional[dict]:
    if not detail:
        return None
    hardware = detail.get("hardware") or {}
    return {
        "commit": detail.get("commit"),
        "build": detail.get("build"),
        "app_version": detail.get("app_version"),
        "os": detail.get("os"),
        "profile": hardware.get("profile"),
        "gpu": (detail.get("gpu") or {}).get("name"),
        "agents": detail.get("agents"),
        "unclean_shutdown": detail.get("unclean_shutdown"),
        "reachability": (detail.get("settings") or {}).get("p2p.reachability"),
    }


@router.get("/nodes")
def support_nodes() -> List[Dict[str, Any]]:
    _require_master()
    rows = db_query(_NODES_SQL, {"problems": list(PROBLEM_KINDS)})
    out = []
    for row in rows:
        row["started"] = _started_summary(row.pop("started"))
        out.append(_out(row))
    return out


@router.get("/reports")
def support_reports(node: str = "", kind: str = "", since: str = "",
                    limit: int = 50) -> List[Dict[str, Any]]:
    _require_master()
    clauses, params = [], {}
    if node:
        clauses.append("node_pubkey = %(node)s")
        params["node"] = _resolve_node(node)
    if kind:
        clauses.append("kind = %(kind)s")
        params["kind"] = kind
    if since:
        try:
            params["since"] = datetime.fromisoformat(since)
        except ValueError:
            raise HTTPException(status_code=400, detail="since must be ISO 8601")
        clauses.append("ts >= %(since)s")
    params["limit"] = max(1, min(int(limit), 500))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = db_query(f"""
        SELECT id, node_pubkey, kind, ts, received_at, detail
          FROM support_reports{where}
         ORDER BY ts DESC LIMIT %(limit)s
    """, params)
    return [_out(r) for r in rows]


# ---------------------------------------------------------------------------
# Warrants
# ---------------------------------------------------------------------------

class WarrantRequest(BaseModel):
    node: str = Field(..., max_length=128)
    scopes: List[str] = Field(default_factory=lambda: list(diag_protocol.SCOPES))
    since: Optional[str] = None            # ISO 8601 — chat/events window
    note: Optional[str] = Field(None, max_length=500)
    expires_hours: int = Field(168, ge=1, le=24 * 30)


@router.post("/warrants")
def issue_warrant(req: WarrantRequest) -> Dict[str, Any]:
    _require_master()
    pubkey = _resolve_node(req.node)
    since = None
    if req.since:
        try:
            since = int(datetime.fromisoformat(req.since).timestamp())
        except ValueError:
            raise HTTPException(status_code=400, detail="since must be ISO 8601")
    try:
        warrant = diag_protocol.sign_warrant(
            load_signing_key(settings).sign, target=pubkey, scopes=req.scopes,
            since=since, ttl_s=req.expires_hours * 3600)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db_execute("""
        INSERT INTO support_warrants (id, node_pubkey, scopes, since, note, issued_at, expires_at)
        VALUES (%s, %s, %s, to_timestamp(%s), %s, to_timestamp(%s), to_timestamp(%s))
    """, (warrant["id"], pubkey, warrant["scopes"], since, req.note,
          warrant["issued_at"], warrant["expires_at"]))
    from routers.peer_diag import dispatch_pending
    dispatched = dispatch_pending(pubkey) > 0
    logger.info("support: warrant %s… for %s… scopes=%s %s", warrant["id"][:8], pubkey[:8],
                ",".join(warrant["scopes"]), "pushed" if dispatched else "parked")
    return {"id": warrant["id"], "node_pubkey": pubkey, "scopes": warrant["scopes"],
            "dispatched": dispatched, "expires_at": warrant["expires_at"]}


@router.get("/warrants")
def support_warrants(node: str = "", open_only: bool = False) -> List[Dict[str, Any]]:
    _require_master()
    clauses, params = [], {}
    if node:
        clauses.append("node_pubkey = %(node)s")
        params["node"] = _resolve_node(node)
    if open_only:
        clauses.append("fulfilled_at IS NULL AND expires_at > now()")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = db_query(f"""
        SELECT id::text AS id, node_pubkey, scopes, since, note, issued_at, expires_at,
               dispatched_at, fulfilled_at, bundle_id::text AS bundle_id
          FROM support_warrants{where}
         ORDER BY issued_at DESC LIMIT 200
    """, params)
    return [_out(r) for r in rows]


# ---------------------------------------------------------------------------
# Bundles
# ---------------------------------------------------------------------------

@router.get("/bundles")
def support_bundles(node: str = "") -> List[Dict[str, Any]]:
    _require_master()
    params = {}
    where = ""
    if node:
        where = " WHERE b.node_pubkey = %(node)s"
        params["node"] = _resolve_node(node)
    rows = db_query(f"""
        SELECT b.id::text AS id, b.warrant_id::text AS warrant_id, b.node_pubkey,
               b.received_at, b.size_bytes, b.sha256, b.opened_at, b.extract_path,
               w.scopes, w.note
          FROM support_bundles b
          JOIN support_warrants w ON w.id = b.warrant_id{where}
         ORDER BY b.received_at DESC LIMIT 200
    """, params)
    return [_out(r) for r in rows]


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Regular files and directories under `dest` only — the archive is a
    friend's, still untrusted input."""
    root = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if not (member.isfile() or member.isdir()) or root not in target.parents and target != root:
            raise HTTPException(status_code=422, detail=f"bundle entry refused: {member.name}")
    tar.extractall(dest, members=[m for m in tar.getmembers() if m.isfile() or m.isdir()])


@router.post("/bundles/{bundle_id}/open")
def open_bundle(bundle_id: str) -> Dict[str, Any]:
    _require_master()
    row = db_query_one("""
        SELECT id::text AS id, node_pubkey, ciphertext, extract_path
          FROM support_bundles WHERE id = %s
    """, (bundle_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="unknown bundle")
    dest = SUPPORT_DIR / row["node_pubkey"][:16] / row["id"]
    if not (row["extract_path"] and Path(row["extract_path"]).exists()):
        from nacl.exceptions import CryptoError
        try:
            plain = diag_protocol.decrypt_from(
                load_signing_key(settings).private_bytes_raw(), row["node_pubkey"],
                bytes(row["ciphertext"]))
        except CryptoError:
            raise HTTPException(status_code=422, detail="bundle does not decrypt with this node's key")
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tar:
            _safe_extract(tar, dest)
        db_execute("UPDATE support_bundles SET opened_at = now(), extract_path = %s WHERE id = %s",
                   (str(dest), bundle_id))
    manifest_path = dest / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
    return {"bundle_id": bundle_id, "node_pubkey": row["node_pubkey"],
            "path": str(dest), "manifest": manifest}


@router.delete("/bundles/{bundle_id}")
def delete_bundle(bundle_id: str) -> Dict[str, Any]:
    _require_master()
    row = db_query_one("SELECT extract_path FROM support_bundles WHERE id = %s", (bundle_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="unknown bundle")
    if row["extract_path"] and Path(row["extract_path"]).exists():
        shutil.rmtree(row["extract_path"])
    db_execute("DELETE FROM support_bundles WHERE id = %s", (bundle_id,))
    return {"deleted": bundle_id}


def sweep_retention() -> None:
    """Master startup: reports past REPORT_KEEP_DAYS, bundles past
    BUNDLE_KEEP_DAYS together with their unpacked files."""
    old = db_query("""
        SELECT id::text AS id, extract_path FROM support_bundles
         WHERE received_at < now() - make_interval(days => %s)
    """, (BUNDLE_KEEP_DAYS,))
    for bundle in old:
        if bundle["extract_path"] and Path(bundle["extract_path"]).exists():
            shutil.rmtree(bundle["extract_path"])
        db_execute("DELETE FROM support_bundles WHERE id = %s", (bundle["id"],))
    db_execute("DELETE FROM support_reports WHERE received_at < now() - make_interval(days => %s)",
               (REPORT_KEEP_DAYS,))
    if old:
        logger.info("support: %d bundle(s) past retention removed", len(old))
