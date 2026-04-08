"""
P2P API routes for Friends / Chat in web UI.

Provides endpoints for account info, friend management, and messaging.
"""

import asyncio
import json
import logging
import select
import ssl
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import psycopg2
import psycopg2.extensions
from config import settings
from db_pool import db_query as _db_query, db_execute as _db_execute, get_conn as _get_conn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/p2p", tags=["p2p"])


class AddFriendRequest(BaseModel):
    invite_code: str = Field(..., min_length=3, pattern=r".+#.+")


class SendMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=10000)


class InviteByEmailRequest(BaseModel):
    to_email: str = Field(..., min_length=5)
    message: str = Field(default="", max_length=500)


# -- Chat SSE infrastructure --------------------------------------------------

_chat_sse_clients: list = []  # list of (asyncio.Event, asyncio.AbstractEventLoop)
_chat_sse_lock = threading.Lock()
_chat_listener_thread: Optional[threading.Thread] = None
_chat_listener_running = False


def _wake_chat_sse_clients():
    """Thread-safe: signal all chat SSE generators to push updates."""
    with _chat_sse_lock:
        for evt, loop in _chat_sse_clients:
            loop.call_soon_threadsafe(evt.set)


def _chat_db_listener():
    """Background thread: LISTEN for PostgreSQL chat notifications."""
    while _chat_listener_running:
        conn = None
        try:
            conn = psycopg2.connect(settings.database_url)
            conn.set_isolation_level(
                psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT
            )
            with conn.cursor() as cur:
                cur.execute("LISTEN sautium_chat")

            while _chat_listener_running:
                ready = select.select([conn], [], [], 1)
                if ready[0]:
                    conn.poll()
                    if conn.notifies:
                        while conn.notifies:
                            conn.notifies.pop(0)
                        _wake_chat_sse_clients()
        except Exception as e:
            logger.debug(f"Chat DB listener error: {e}")
            if _chat_listener_running:
                import time
                time.sleep(1)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass


def start_chat_listener():
    """Start the background DB listener thread for chat SSE."""
    global _chat_listener_thread, _chat_listener_running
    if _chat_listener_thread and _chat_listener_thread.is_alive():
        return
    _chat_listener_running = True
    _chat_listener_thread = threading.Thread(
        target=_chat_db_listener, daemon=True, name="chat-sse-listener"
    )
    _chat_listener_thread.start()


def stop_chat_listener():
    """Stop the background DB listener thread."""
    global _chat_listener_running
    _chat_listener_running = False
    if _chat_listener_thread:
        _chat_listener_thread.join(timeout=3)




# Cache identity to avoid re-deriving (Argon2id is slow)
_cached_identity = None


def _get_identity():
    global _cached_identity
    if _cached_identity is not None:
        return _cached_identity
    # Try reading pre-derived identity from node_info.json (desktop mode)
    if settings.p2p_identity_dir:
        import json
        from pathlib import Path
        info_path = Path(settings.p2p_identity_dir) / "node_info.json"
        if info_path.exists():
            try:
                data = json.loads(info_path.read_text(encoding="utf-8"))
                if data.get("username"):
                    _cached_identity = {
                        "node_id": data["node_id"],
                        "public_key_hex": data["public_key_hex"],
                        "username": data["username"],
                        "invite_code": data["invite_code"],
                        "email": data.get("email", ""),
                    }
                    return _cached_identity
            except Exception:
                pass
    # Fallback: derive from username+password (Docker mode)
    if settings.p2p_username:
        from p2p_identity import derive_identity
        _cached_identity = derive_identity(
            settings.p2p_username,
            settings.p2p_password,
            settings.p2p_email,
        )
    return _cached_identity


VERIFY_WORKER_URL = "https://sautium-verify.sautium.workers.dev"

# Cached private key for signing Worker requests
_cached_private_key = None


def _get_private_key():
    """Load or derive the Ed25519 private key for signing."""
    global _cached_private_key
    if _cached_private_key is not None:
        return _cached_private_key

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    from pathlib import Path

    # Desktop mode: read PEM from identity dir
    if settings.p2p_identity_dir:
        key_path = Path(settings.p2p_identity_dir) / "node_ed25519.key"
        if key_path.exists():
            pem = key_path.read_bytes()
            _cached_private_key = serialization.load_pem_private_key(pem, password=None)
            return _cached_private_key

    # Docker mode: derive from username + password
    if settings.p2p_username and settings.p2p_password:
        from p2p_identity import ARGON2_TIME_COST, ARGON2_MEMORY_COST, ARGON2_PARALLELISM, ARGON2_HASH_LEN
        from argon2.low_level import hash_secret_raw, Type
        salt = f"{settings.p2p_username}:sautium".encode("utf-8")
        seed = hash_secret_raw(
            secret=settings.p2p_password.encode("utf-8"),
            salt=salt,
            time_cost=ARGON2_TIME_COST,
            memory_cost=ARGON2_MEMORY_COST,
            parallelism=ARGON2_PARALLELISM,
            hash_len=ARGON2_HASH_LEN,
            type=Type.ID,
        )
        _cached_private_key = Ed25519PrivateKey.from_private_bytes(seed)
        return _cached_private_key

    return None


def _sign_message(message: str) -> Optional[str]:
    """Sign a message with our Ed25519 key. Returns hex signature or None."""
    key = _get_private_key()
    if not key:
        return None
    sig = key.sign(message.encode("utf-8"))
    return sig.hex()


async def _worker_post(path: str, payload: dict) -> Optional[dict]:
    """POST to the Cloudflare Worker. Returns response dict or None."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            resp = await client.post(
                f"{VERIFY_WORKER_URL}{path}",
                json=payload,
            )
            if resp.status_code == 200:
                return resp.json()
            logger.warning(f"Worker {path} returned {resp.status_code}: {resp.text}")
            return None
    except Exception as e:
        logger.error(f"Worker {path} failed: {e}")
        return None


async def _worker_get(path: str, params: dict) -> Optional[dict]:
    """GET from the Cloudflare Worker. Returns response dict or None."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            resp = await client.get(
                f"{VERIFY_WORKER_URL}{path}",
                params=params,
            )
            if resp.status_code == 200:
                return resp.json()
            logger.warning(f"Worker GET {path} returned {resp.status_code}: {resp.text}")
            return None
    except Exception as e:
        logger.error(f"Worker GET {path} failed: {e}")
        return None


@router.get("/account")
async def get_account() -> Dict[str, Any]:
    """Get current P2P account info (invite code, username)."""
    identity = _get_identity()
    if not identity:
        return {"invite_code": None, "username": None}
    return {
        "invite_code": identity["invite_code"],
        "username": identity["username"],
        "email": identity.get("email", ""),
        "public_key_hex": identity["public_key_hex"],
    }


@router.get("/friends")
async def list_friends() -> List[Dict[str, Any]]:
    """List all friends with unread counts."""
    rows = _db_query("""
        SELECT f.id, f.username, f.public_key_hex, f.invite_code,
               f.display_name, f.added_at, f.last_seen, f.is_blocked,
               COALESCE(u.unread, 0) as unread_count
        FROM friends f
        LEFT JOIN (
            SELECT friend_id, COUNT(*) as unread
            FROM p2p_messages
            WHERE direction = 'in' AND read = FALSE
            GROUP BY friend_id
        ) u ON u.friend_id = f.id
        ORDER BY f.display_name, f.username
    """)
    for row in rows:
        for k in ("added_at", "last_seen"):
            if row.get(k):
                row[k] = row[k].isoformat()
    return rows


@router.post("/friends/add")
async def add_friend(req: AddFriendRequest) -> Dict[str, Any]:
    """Add a friend by invite code. Also notifies the Worker for auto-reciprocate."""
    invite_code = req.invite_code.strip()
    username = invite_code.split("#")[0]

    try:
        row = _db_execute("""
            INSERT INTO friends (username, public_key_hex, invite_code, display_name)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (public_key_hex) DO UPDATE
                SET invite_code = EXCLUDED.invite_code,
                    username = EXCLUDED.username
            RETURNING id
        """, (username, f"pending:{invite_code}", invite_code, username))

        # Fire-and-forget: notify Worker that we accepted this invite
        asyncio.create_task(_accept_invite_on_worker(invite_code))

        return {"id": row["id"], "username": username, "invite_code": invite_code}
    except Exception as e:
        logger.error(f"Failed to add friend: {e}")
        raise HTTPException(status_code=500, detail="Failed to add friend")


async def _accept_invite_on_worker(their_invite_code: str):
    """Notify the Worker that we accepted someone's invite (auto-reciprocate)."""
    identity = _get_identity()
    if not identity:
        return

    my_invite = identity["invite_code"]
    public_key_hex = identity["public_key_hex"]

    message = f"accept:{my_invite}:{their_invite_code}"
    signature = _sign_message(message)
    if not signature:
        logger.warning("Cannot sign accept-invite — no private key")
        return

    result = await _worker_post("/accept-invite", {
        "my_invite_code": my_invite,
        "their_invite_code": their_invite_code,
        "public_key_hex": public_key_hex,
        "signature": signature,
    })
    if result and result.get("status") == "accepted":
        notified = result.get("notified", False)
        logger.info(f"Accept-invite sent for {their_invite_code} (notified={notified})")


@router.get("/friends/{friend_id}/messages")
async def get_messages(friend_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    """Get chat messages with a friend."""
    limit = min(limit, 500)
    rows = _db_query("""
        SELECT id, direction, content, timestamp, delivered, read,
               message_uuid
        FROM p2p_messages
        WHERE friend_id = %s
        ORDER BY id DESC LIMIT %s
    """, (friend_id, limit))
    rows.reverse()
    for row in rows:
        if row.get("timestamp"):
            ts = row["timestamp"]
            row["timestamp"] = ts.isoformat() if ts.tzinfo else ts.isoformat() + "+00:00"
    return rows


@router.post("/friends/{friend_id}/messages/read")
async def mark_messages_read(friend_id: int) -> Dict[str, Any]:
    """Mark all incoming messages from a friend as read."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE p2p_messages
                SET read = TRUE
                WHERE friend_id = %s AND direction = 'in' AND read = FALSE
            """, (friend_id,))
            return {"ok": True, "updated": cur.rowcount}


@router.post("/friends/{friend_id}/send")
async def send_message(friend_id: int, req: SendMessageRequest) -> Dict[str, Any]:
    """Send a message to a friend (stored locally, P2P delivery via manager)."""
    content = req.content.strip()

    msg_uuid = str(uuid.uuid4())
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO p2p_messages
                    (friend_id, direction, content, timestamp, delivered,
                     message_uuid)
                VALUES (%s, 'out', %s, %s, FALSE, %s)
                RETURNING id
            """, (friend_id, content, datetime.now(timezone.utc), msg_uuid))
            msg_id = cur.fetchone()[0]

    # Wake SSE clients immediately (sender sees own message)
    _wake_chat_sse_clients()

    # Trigger P2P delivery via sync server (fire-and-forget)
    asyncio.create_task(_trigger_sync_server_delivery())

    return {"id": msg_id, "message_uuid": msg_uuid, "status": "queued"}


async def _trigger_sync_server_delivery():
    """Fire-and-forget: tell the desktop sync server to push pending messages."""
    port = settings.p2p_listen_port
    if not port:
        return
    try:
        async with httpx.AsyncClient(
            verify=False, timeout=httpx.Timeout(3.0),
        ) as client:
            await client.post(f"https://127.0.0.1:{port}/api/chat/trigger-send")
    except Exception:
        pass  # Fallback: pending loop will pick it up


@router.post("/invite-by-email")
async def invite_by_email(req: InviteByEmailRequest) -> Dict[str, Any]:
    """Send an invite email to someone via the Cloudflare Worker."""
    identity = _get_identity()
    if not identity:
        raise HTTPException(status_code=400, detail="No P2P account configured")

    invite_code = identity["invite_code"]
    public_key_hex = identity["public_key_hex"]
    to_email = req.to_email.strip()

    sig_message = f"invite:{invite_code}:to:{to_email}"
    signature = _sign_message(sig_message)
    if not signature:
        raise HTTPException(status_code=500, detail="Cannot sign request")

    result = await _worker_post("/send-invite", {
        "to": to_email,
        "invite_code": invite_code,
        "public_key_hex": public_key_hex,
        "signature": signature,
        "message": req.message,
    })
    if not result:
        raise HTTPException(status_code=502, detail="Worker request failed")
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])

    # Track sent invite in DB
    try:
        _db_execute("""
            INSERT INTO sent_invites (to_email)
            VALUES (%s)
        """, (to_email,))
    except Exception as e:
        logger.debug(f"Failed to track sent invite: {e}")

    return {
        "status": "sent",
        "to": to_email,
        "verified_sender": result.get("verified_sender", False),
    }


@router.get("/pending-accepts")
async def pending_accepts() -> Dict[str, Any]:
    """Check the Worker for invite codes that accepted our invites.

    Skips the Worker call if no unreciprocated invites exist locally.
    """
    # Early return: no pending invites → no reason to ask the Worker
    pending_count = _db_query(
        "SELECT COUNT(*) as cnt FROM sent_invites "
        "WHERE sent_at > NOW() - INTERVAL '30 days'"
    )
    if not pending_count or pending_count[0]["cnt"] == 0:
        return {"accepts": [], "skipped": True}

    identity = _get_identity()
    if not identity:
        return {"accepts": []}

    invite_code = identity["invite_code"]
    public_key_hex = identity["public_key_hex"]

    message = f"pending-accepts:{invite_code}"
    signature = _sign_message(message)
    if not signature:
        return {"accepts": []}

    result = await _worker_get("/pending-accepts", {
        "invite_code": invite_code,
        "public_key_hex": public_key_hex,
        "signature": signature,
    })
    if not result:
        return {"accepts": []}

    accepts = result.get("accepts", [])

    # Auto-add accepted friends (skip if already exists by invite_code)
    added = []
    for accept in accepts:
        acc_invite = accept.get("invite_code", "")
        if not acc_invite:
            continue

        # Check if friend already exists (may have been added via nudge)
        existing = _db_query(
            "SELECT id FROM friends WHERE invite_code = %s LIMIT 1",
            (acc_invite,),
        )
        if existing:
            continue

        acc_username = acc_invite.split("#")[0]
        try:
            row = _db_execute("""
                INSERT INTO friends (username, public_key_hex, invite_code, display_name)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (public_key_hex) DO UPDATE
                    SET invite_code = EXCLUDED.invite_code,
                        username = EXCLUDED.username
                RETURNING id
            """, (acc_username, f"pending:{acc_invite}", acc_invite, acc_username))
            added.append({"id": row["id"], "invite_code": acc_invite})
            logger.info(f"Auto-added friend from pending accept: {acc_invite}")
        except Exception as e:
            logger.error(f"Failed to auto-add {acc_invite}: {e}")

    # Clean up stale entries (Worker TTL is 30 days, no point keeping older)
    try:
        _db_execute(
            "DELETE FROM sent_invites WHERE sent_at < NOW() - INTERVAL '30 days'"
        )
    except Exception:
        pass

    return {"accepts": accepts, "added": added}


@router.post("/chat/wake")
async def chat_wake() -> Dict[str, str]:
    """Wake SSE clients immediately (called by sync server on incoming message)."""
    _wake_chat_sse_clients()
    return {"ok": "true"}


@router.get("/chat/stream")
async def chat_stream():
    """SSE endpoint: pushes chat update notifications in real-time."""
    loop = asyncio.get_event_loop()
    evt = asyncio.Event()

    async def event_generator():
        try:
            with _chat_sse_lock:
                _chat_sse_clients.append((evt, loop))

            # Initial ping so the client knows the connection is live
            yield "data: {}\n\n"

            while True:
                try:
                    await asyncio.wait_for(evt.wait(), timeout=15.0)
                    evt.clear()
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue

                yield f"data: {{\"ts\":{int(datetime.now(timezone.utc).timestamp())}}}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            with _chat_sse_lock:
                _chat_sse_clients[:] = [
                    (e, l) for e, l in _chat_sse_clients if e is not evt
                ]

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
