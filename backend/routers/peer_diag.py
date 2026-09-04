"""Support diagnostics — the master's peer-surface ingress (p2p_app only):
event reports and warrant bundles from user nodes, plus warrant dispatch
down the nodes' wake streams (P2P_NETWORK.md § "Support diagnostics";
protocol in desktop/p2p/diag_protocol.py).

Auth: every request carries a wire-format-v1 signature (peer_auth) over
its body, verified HERE and not by the identity middleware — the prefix
is deliberately not identity-bound, so the gate, the registry and the
whole-body buffering stay out of the path. The signer must be a
non-blocked friend (every node auto-friends the master), and a bundle
must answer a warrant this node issued to exactly that signer.

Bodies are NaCl boxes readable by the master's own key alone; both routes
cap what they read BEFORE reading it — nothing above this router does.
Writes go to support_* tables only.
"""

import asyncio
import hashlib
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import psycopg2
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from config import settings
from db_pool import db_execute, db_query, db_query_one, get_conn
from desktop.p2p import diag_protocol, peer_auth
from p2p_chat import get_peer_chat
from p2p_identity import load_signing_key, resolve_identity

logger = logging.getLogger(__name__)

diag_router = APIRouter(prefix="/api/diag", tags=["peer-diag"])

_report_hits: dict = {}            # node pubkey -> [monotonic send times]
_report_lock = threading.Lock()


def _err(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def own_seed() -> bytes:
    return load_signing_key(settings).private_bytes_raw()


async def _read_capped(request: Request, cap: int) -> Optional[bytes]:
    """The body, or None past `cap` — refused on Content-Length first and
    enforced while streaming (a chunked body carries no length)."""
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > cap:
        return None
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _request_target(request: Request) -> str:
    target = request.url.path
    if request.url.query:
        target += "?" + request.url.query
    return target


def _verify(request: Request, body: bytes):
    """(signer pubkey, None) for a friend's valid signature, else
    (None, response)."""
    ident = resolve_identity(settings)
    if not ident:
        return None, _err("no account configured", 503)
    pubkey, err = peer_auth.verify_request(
        request.headers, ident["public_key_hex"], request.method,
        _request_target(request), body)
    if err:
        return None, _err(err, 403)
    if not pubkey:
        return None, _err("signature required", 403)
    svc = get_peer_chat()
    friend = svc.friend_for_key(pubkey) if svc else None
    if friend is None or friend["is_blocked"]:
        return None, _err("not a friend", 403)
    return pubkey, None


def _report_allowed(pubkey: str) -> bool:
    now = time.monotonic()
    with _report_lock:
        recent = [t for t in _report_hits.get(pubkey, ()) if now - t < 3600]
        if len(recent) >= diag_protocol.REPORT_MAX_PER_HOUR:
            _report_hits[pubkey] = recent
            return False
        recent.append(now)
        _report_hits[pubkey] = recent
        if len(_report_hits) > 10_000:
            for k in [k for k, v in _report_hits.items() if not v or now - v[-1] > 3600]:
                _report_hits.pop(k, None)
    return True


# ---------------------------------------------------------------------------
# Ingress
# ---------------------------------------------------------------------------

@diag_router.post("/report")
async def diag_report(request: Request):
    from nacl.exceptions import CryptoError

    body = await _read_capped(request, diag_protocol.REPORT_MAX_BYTES)
    if body is None:
        return _err("report too large", 413)
    pubkey, failure = _verify(request, body)
    if failure:
        return failure
    if not _report_allowed(pubkey):
        return _err("too many reports", 429)
    try:
        events = diag_protocol.decode_report(
            diag_protocol.decrypt_from(own_seed(), pubkey, body))
    except (CryptoError, ValueError) as e:
        return _err(f"unreadable report: {e}", 400)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO support_reports (node_pubkey, kind, ts, detail)
                VALUES (%s, %s, %s, %s::jsonb)
            """, [(pubkey, e["kind"], e["ts"], json.dumps(e["detail"], default=str))
                  for e in events])
    logger.info("support: %d event(s) reported by %s…", len(events), pubkey[:8])
    return {"accepted": len(events)}


@diag_router.post("/bundle")
async def diag_bundle(request: Request, warrant: str = ""):
    try:
        warrant_id = str(uuid.UUID(warrant))
    except ValueError:
        return _err("warrant id required", 400)
    body = await _read_capped(request, diag_protocol.BUNDLE_MAX_BYTES)
    if body is None:
        return _err("bundle too large", 413)
    pubkey, failure = _verify(request, body)
    if failure:
        return failure
    row = db_query_one("""
        SELECT node_pubkey, expires_at, fulfilled_at
          FROM support_warrants WHERE id = %s
    """, (warrant_id,))
    if row is None:
        return _err("unknown warrant", 404)
    if row["node_pubkey"] != pubkey:
        return _err("warrant is not yours", 403)
    if row["fulfilled_at"] is not None:
        return _err("warrant already fulfilled", 409)
    grace = timedelta(seconds=diag_protocol.BUNDLE_ACCEPT_GRACE_S)
    if datetime.now(timezone.utc) > row["expires_at"] + grace:
        return _err("warrant expired", 410)

    bundle_id = str(uuid.uuid4())
    digest = hashlib.sha256(body).hexdigest()
    with get_conn() as conn:
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO support_bundles
                        (id, warrant_id, node_pubkey, size_bytes, sha256, ciphertext)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (bundle_id, warrant_id, pubkey, len(body), digest,
                      psycopg2.Binary(body)))
                cur.execute("""
                    UPDATE support_warrants
                       SET fulfilled_at = now(), bundle_id = %s
                     WHERE id = %s AND fulfilled_at IS NULL
                """, (bundle_id, warrant_id))
                if cur.rowcount != 1:           # a concurrent upload won
                    conn.rollback()
                    return _err("warrant already fulfilled", 409)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True
    logger.info("support: bundle %s… (%d bytes) for warrant %s… from %s…",
                bundle_id[:8], len(body), warrant_id[:8], pubkey[:8])
    return {"stored": True, "bundle_id": bundle_id}


# ---------------------------------------------------------------------------
# Dispatch — down the node's wake stream, now or when it next subscribes
# ---------------------------------------------------------------------------

def _signed_warrant(row: dict) -> dict:
    """Re-sign the stored warrant: the canonical payload is whole seconds
    and Ed25519 is deterministic, so this is the byte-identical warrant
    the node may already hold — its state machine dedups by id."""
    since = int(row["since"].timestamp()) if row["since"] else None
    issued_at = int(row["issued_at"].timestamp())
    ttl_s = int(row["expires_at"].timestamp()) - issued_at
    return diag_protocol.sign_warrant(
        load_signing_key(settings).sign, target=row["node_pubkey"], scopes=row["scopes"],
        since=since, now=issued_at, ttl_s=ttl_s, warrant_id=row["id"])


def dispatch_pending(pubkey: str) -> int:
    """Push every open warrant for `pubkey` down its live wake stream and
    stamp dispatched_at; returns how many went. Re-pushing on every
    subscribe until the bundle lands is the retry — the node dedups."""
    from routers.peer_chat import push_frame
    rows = db_query("""
        SELECT id::text AS id, node_pubkey, scopes, since, issued_at, expires_at
          FROM support_warrants
         WHERE node_pubkey = %s AND fulfilled_at IS NULL AND expires_at > now()
         ORDER BY issued_at
    """, (pubkey,))
    sent = 0
    for row in rows:
        if push_frame(pubkey, diag_protocol.warrant_frame(_signed_warrant(row))):
            db_execute("UPDATE support_warrants SET dispatched_at = now() WHERE id = %s",
                       (row["id"],))
            sent += 1
    return sent


async def on_wake_subscribed(pubkey: str) -> None:
    sent = await asyncio.to_thread(dispatch_pending, pubkey)
    if sent:
        logger.info("support: %d warrant(s) pushed to %s… on subscribe", sent, pubkey[:8])
