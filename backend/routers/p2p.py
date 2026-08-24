"""
P2P API routes for Friends / Chat in web UI.

Provides endpoints for account info, friend management, and messaging.
"""

import asyncio
import base64
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
from db_pool import db_query as _db_query, db_query_one as _db_query_one, db_execute as _db_execute, get_conn as _get_conn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/p2p", tags=["p2p"])


class AddFriendRequest(BaseModel):
    invite_code: str = Field(..., min_length=3, pattern=r".+#.+")


class SendMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=10000)


class InviteByEmailRequest(BaseModel):
    to_email: str = Field(..., min_length=5)
    # Optional intro line forwarded to the Worker — recipient sees it
    # under the invite block. Empty default keeps callers that only
    # pass `to_email` working unchanged.
    message: str = Field(default="", max_length=500)


class VerifyCodeRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=10)
    message: str = Field(default="", max_length=500)


class SetEmailRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)


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
                        out_friend_ids = set()
                        while conn.notifies:
                            note = conn.notifies.pop(0)
                            # trg_p2p_messages_notify payload:
                            # msg:{friend_id}:{direction}. An 'out' insert
                            # means a reply for that friend — ping their
                            # relay wake stream so they pull immediately.
                            parts = (note.payload or "").split(":")
                            if (len(parts) == 3 and parts[0] == "msg"
                                    and parts[2] == "out"):
                                try:
                                    out_friend_ids.add(int(parts[1]))
                                except ValueError:
                                    pass
                        _wake_chat_sse_clients()
                        if out_friend_ids:
                            from routers.peer_chat import (
                                ping_wake_by_friend_id,
                            )
                            for fid in out_friend_ids:
                                ping_wake_by_friend_id(fid)
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
def _get_identity():
    """One resolution point (p2p_identity.resolve_identity — cached there,
    keyed on node_info.json's mtime in desktop mode); no second cache here."""
    from p2p_identity import resolve_identity
    return resolve_identity(settings)


VERIFY_WORKER_URL = "https://sautium-verify.sautium.workers.dev"
# Descriptive UA — Cloudflare challenges default python-httpx/urllib agents,
# which would surface as spurious Worker failures. Matches sign_audio.py /
# desktop email_verify.py so every Worker call speaks with the same identity.
_WORKER_HEADERS = {"User-Agent": "Sautium/1.0"}

# Cached private key for signing Worker requests
_cached_private_key = None


def _get_private_key():
    """Load or derive the Ed25519 private key for signing."""
    global _cached_private_key
    if _cached_private_key is None:
        from p2p_identity import load_signing_key
        _cached_private_key = load_signing_key(settings)
    return _cached_private_key


def _sign_message(message: str) -> Optional[str]:
    """Sign a message with our Ed25519 key. Returns hex signature or None."""
    key = _get_private_key()
    if not key:
        return None
    sig = key.sign(message.encode("utf-8"))
    return sig.hex()


async def _worker_post(path: str, payload: dict) -> Optional[dict]:
    """POST to the Cloudflare Worker. Returns the response dict, or None on a
    non-200. A 429 (Worker anti-spam) surfaces as HTTPException so the calling
    endpoint returns a clear 'service busy, retry' instead of a misleading
    result (e.g. register-email would otherwise read as 'invalid code')."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            resp = await client.post(
                f"{VERIFY_WORKER_URL}{path}",
                json=payload,
                headers=_WORKER_HEADERS,
            )
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                logger.warning(f"Worker {path} rate-limited (429): {resp.text}")
                raise HTTPException(429, "The verification service is busy — please wait a minute and try again.")
            logger.warning(f"Worker {path} returned {resp.status_code}: {resp.text}")
            return None
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Worker {path} failed: {e}")
        return None


async def _worker_get(path: str, params: dict) -> Optional[dict]:
    """GET from the Cloudflare Worker. Returns the response dict, or None on a
    non-200. A 429 (Worker anti-spam) surfaces as HTTPException — see
    _worker_post."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            resp = await client.get(
                f"{VERIFY_WORKER_URL}{path}",
                params=params,
                headers=_WORKER_HEADERS,
            )
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                logger.warning(f"Worker GET {path} rate-limited (429): {resp.text}")
                raise HTTPException(429, "The verification service is busy — please wait a minute and try again.")
            logger.warning(f"Worker GET {path} returned {resp.status_code}: {resp.text}")
            return None
    except HTTPException:
        raise
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


@router.put("/account/email")
async def set_account_email(req: SetEmailRequest) -> Dict[str, Any]:
    """Set or change the account email. Persisted to node_info.json
    (desktop mode only) and resets user_profile.email_verified — both
    a new email and changing an existing one invalidate the Worker's
    `invite_code → email` mapping for this identity."""
    email = req.email.strip()
    if "@" not in email or "." not in email.split("@", 1)[1]:
        raise HTTPException(400, "Invalid email address")

    if not settings.p2p_identity_dir:
        raise HTTPException(
            400,
            "Email is configured via the P2P_EMAIL environment variable "
            "in this deployment — edit the .env and restart the backend.",
        )

    import json
    from pathlib import Path
    info_path = Path(settings.p2p_identity_dir) / "node_info.json"
    if not info_path.exists():
        raise HTTPException(500, "node_info.json is missing")
    try:
        data = json.loads(info_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"Cannot read node_info.json: {e}")

    data["email"] = email
    data["email_verified"] = False
    info_path.write_text(json.dumps(data, indent=2), encoding="utf-8")   # resolve_identity re-reads on mtime change

    _db_execute("UPDATE user_profile SET email_verified = FALSE WHERE id = 1")

    return {"email": email, "email_verified": False}


# One server-side truth for "online" — imported by routers/settings.py's
# sync state; the client renders row.is_online and computes nothing.
ONLINE_WINDOW_MIN = 5

# Chat-recency order: whoever wrote (or was written to) last comes first,
# so a new message lifts its row to the top. friends.last_activity_at is
# maintained by the p2p_messages trigger and seeded from added_at.
_FRIEND_SORT = "f.last_activity_at DESC, f.id DESC"

_FRIENDS_SELECT = f"""
    SELECT f.id, f.username, f.public_key_hex, f.invite_code,
           f.display_name, f.added_at, f.last_seen, f.is_blocked,
           f.favorite, f.source::text AS source, f.last_activity_at,
           COALESCE(f.last_seen > NOW() - make_interval(
               mins => {ONLINE_WINDOW_MIN}), FALSE) AS is_online,
           COALESCE(u.unread, 0) AS unread_count,
           COALESCE(r.rights, '{{}}') AS rights_granted,
           COALESCE(g.rights, '{{}}') AS grant_rights,
           EXISTS (SELECT 1 FROM friend_grants fg
                   WHERE fg.friend_id = f.id) AS has_grant
    FROM friends f
    LEFT JOIN (
        SELECT friend_id, COUNT(*) AS unread
        FROM p2p_messages
        WHERE direction = 'in' AND read = FALSE
        GROUP BY friend_id
    ) u ON u.friend_id = f.id
    LEFT JOIN (
        SELECT friend_id,
               array_agg(p2p_right::text ORDER BY p2p_right) AS rights
        FROM friend_rights GROUP BY friend_id
    ) r ON r.friend_id = f.id
    LEFT JOIN (
        SELECT friend_id,
               array_agg(p2p_right::text ORDER BY p2p_right) AS rights
        FROM friend_grant_rights GROUP BY friend_id
    ) g ON g.friend_id = f.id
"""


def _friend_row_out(row: dict) -> dict:
    for k in ("added_at", "last_seen", "last_activity_at"):
        if row.get(k):
            row[k] = row[k].isoformat()
    return row


@router.get("/friends")
async def list_friends(cursor: str = "", limit: int = 50,
                       q: str = "") -> Dict[str, Any]:
    """Friends list built for scale: pinned favorites fetched whole
    (user-curated, small by definition), the rest keyset-paginated over
    (last_activity_at, id) — the home.py cursor pattern on
    idx_friends_recent."""
    limit = max(1, min(int(limit), 200))
    search = q.strip()
    q_clause = ""
    q_params: list = []
    if search:
        q_clause = " AND (f.display_name ILIKE %s OR f.username ILIKE %s)"
        q_params = [f"%{search}%", f"%{search}%"]

    pinned = _db_query(
        _FRIENDS_SELECT + f" WHERE f.favorite = TRUE{q_clause}"
        f" ORDER BY {_FRIEND_SORT}", q_params)

    after = None
    if cursor:
        try:
            after = json.loads(
                base64.urlsafe_b64decode(cursor.encode()).decode())
            assert isinstance(after, list) and len(after) == 2
        except Exception:
            raise HTTPException(status_code=400, detail="Bad cursor")

    cur_clause = ""
    cur_params: list = []
    if after:
        cur_clause = " AND (f.last_activity_at, f.id) < (%s::timestamptz, %s)"
        cur_params = [after[0], int(after[1])]

    items = _db_query(
        _FRIENDS_SELECT + f" WHERE f.favorite = FALSE{q_clause}{cur_clause}"
        f" ORDER BY {_FRIEND_SORT} LIMIT %s",
        q_params + cur_params + [limit + 1])

    next_cursor = None
    if len(items) > limit:
        items = items[:limit]
        last = items[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps(
            [last["last_activity_at"].isoformat(), last["id"]]).encode()).decode()

    total = _db_query_one(
        "SELECT count(*) AS c FROM friends f WHERE TRUE" + q_clause,
        q_params)["c"]

    return {
        "pinned": [_friend_row_out(r) for r in pinned],
        "items": [_friend_row_out(r) for r in items],
        "next_cursor": next_cursor,
        "total": total,
    }


@router.post("/friends/add")
async def add_friend(req: AddFriendRequest) -> Dict[str, Any]:
    """Add a friend by share string (`user#XXXX-XXXX-XXXX[#token]`).

    Token invites skip the Worker entirely — the token handshake replaces
    Worker reciprocation. A manual re-add of the master invite is consent:
    it clears the removal flag and re-arms the auto-contact."""
    from master_node import MASTER_INVITE_CODE, MASTER_TOKEN_ID
    from p2p_identity import parse_share_string

    try:
        invite_code, token_id = parse_share_string(req.invite_code)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid invite code")
    username = invite_code.split("#")[0]

    source = "manual"
    if MASTER_INVITE_CODE and invite_code == MASTER_INVITE_CODE:
        source = "master"
        token_id = token_id or MASTER_TOKEN_ID or None
        _db_execute(
            "DELETE FROM user_settings WHERE key = 'p2p.master_removed'")

    try:
        row = _db_execute("""
            INSERT INTO friends (username, public_key_hex, invite_code,
                                 display_name, source, join_token_id)
            VALUES (%s, %s, %s, %s, %s::friend_source, %s)
            ON CONFLICT (public_key_hex) DO UPDATE
                SET invite_code = EXCLUDED.invite_code,
                    username = EXCLUDED.username,
                    join_token_id = COALESCE(EXCLUDED.join_token_id,
                                             friends.join_token_id)
            RETURNING id
        """, (username, f"pending:{invite_code}", invite_code, username,
              source, token_id))

        if not token_id:
            # Fire-and-forget: notify Worker that we accepted this invite
            asyncio.create_task(_accept_invite_on_worker(invite_code))

        _wake_chat_sse_clients()
        return {"id": row["id"], "username": username,
                "invite_code": invite_code, "token_id": token_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to add friend: {e}")
        raise HTTPException(status_code=500, detail="Failed to add friend")


class FriendPatch(BaseModel):
    display_name: Optional[str] = Field(None, max_length=128)
    favorite: Optional[bool] = None
    is_blocked: Optional[bool] = None


@router.patch("/friends/{friend_id}")
async def patch_friend(friend_id: int, req: FriendPatch) -> Dict[str, Any]:
    """Rename, pin/unpin, block/unblock — one edit endpoint."""
    sets, params = [], []
    if req.display_name is not None:
        sets.append("display_name = %s")
        params.append(req.display_name.strip())
    if req.favorite is not None:
        sets.append("favorite = %s")
        params.append(req.favorite)
    if req.is_blocked is not None:
        sets.append("is_blocked = %s")
        params.append(req.is_blocked)
    if not sets:
        raise HTTPException(status_code=400, detail="Nothing to update")
    row = _db_execute(
        f"UPDATE friends SET {', '.join(sets)} WHERE id = %s"
        " RETURNING id, display_name, favorite, is_blocked",
        params + [friend_id])
    if not row:
        raise HTTPException(status_code=404, detail="Friend not found")
    _wake_chat_sse_clients()
    return row


@router.delete("/friends/{friend_id}")
async def delete_friend(friend_id: int) -> Dict[str, Any]:
    """Delete a friend (messages CASCADE). Deleting the master contact sets
    the removal flag so the auto-add never resurrects it — a later manual
    re-add of the master invite clears the flag (consent)."""
    row = _db_execute(
        "DELETE FROM friends WHERE id = %s RETURNING source::text AS source",
        (friend_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Friend not found")
    if row["source"] == "master":
        _db_execute("""
            INSERT INTO user_settings (key, value, updated_at)
            VALUES ('p2p.master_removed', 'true'::jsonb, NOW())
            ON CONFLICT (key) DO UPDATE
                SET value = 'true'::jsonb, updated_at = NOW()
        """)
    _wake_chat_sse_clients()
    return {"ok": True}


# -- Invite tokens (issuer-side management) -----------------------------------

class TokenCreate(BaseModel):
    label: str = Field(default="", max_length=128)
    rights: List[str] = Field(default_factory=lambda: ["can_message"])
    max_uses: Optional[int] = Field(default=None, ge=1)
    expires_at: Optional[str] = None          # ISO timestamp
    welcome_message: Optional[str] = Field(default=None, max_length=2000)
    # Custom id = the device-transfer path: re-create the token with the
    # UUID from your old share string and existing grants keep working.
    id: Optional[str] = None


class TokenPatch(BaseModel):
    label: Optional[str] = Field(default=None, max_length=128)
    rights: Optional[List[str]] = None
    max_uses: Optional[int] = Field(default=None, ge=1)
    expires_at: Optional[str] = None
    welcome_message: Optional[str] = Field(default=None, max_length=2000)


def _validate_rights(rights: List[str]) -> List[str]:
    from invite_tokens import ALL_RIGHTS
    bad = [r for r in rights if r not in ALL_RIGHTS]
    if bad:
        raise HTTPException(status_code=400,
                            detail=f"Unknown rights: {', '.join(bad)}")
    return sorted(set(rights))


@router.get("/tokens")
async def list_tokens() -> List[Dict[str, Any]]:
    rows = _db_query("""
        SELECT t.id::text AS id, t.label, t.max_uses, t.use_count,
               t.expires_at, t.revoked_at, t.require_birth_cert,
               t.welcome_message, t.created_at,
               COALESCE(r.rights, '{}') AS rights
        FROM invite_tokens t
        LEFT JOIN (
            SELECT token_id,
                   array_agg(p2p_right::text ORDER BY p2p_right) AS rights
            FROM invite_token_rights GROUP BY token_id
        ) r ON r.token_id = t.id
        ORDER BY t.created_at DESC
    """)
    for row in rows:
        for k in ("expires_at", "revoked_at", "created_at"):
            if row.get(k):
                row[k] = row[k].isoformat()
    return rows


@router.post("/tokens")
async def create_token(req: TokenCreate) -> Dict[str, Any]:
    rights = _validate_rights(req.rights)
    token_id = str(uuid.uuid4())
    if req.id:
        try:
            token_id = str(uuid.UUID(req.id.strip()))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid token id")
    try:
        _db_execute("""
            INSERT INTO invite_tokens (id, label, max_uses, expires_at,
                                       welcome_message)
            VALUES (%s, %s, %s, %s::timestamptz, %s)
        """, (token_id, req.label.strip(), req.max_uses, req.expires_at,
              req.welcome_message))
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="Token id already exists")
    for r in rights:
        _db_execute(
            "INSERT INTO invite_token_rights (token_id, p2p_right)"
            " VALUES (%s, %s) ON CONFLICT DO NOTHING", (token_id, r))
    identity = _get_identity()
    share = (f"{identity['invite_code']}#{token_id}"
             if identity else token_id)
    return {"id": token_id, "share_string": share, "rights": rights}


@router.patch("/tokens/{token_id}")
async def patch_token(token_id: str, req: TokenPatch) -> Dict[str, Any]:
    """Edits affect FUTURE uses only — existing friendships keep their
    rights snapshot."""
    sets, params = [], []
    if req.label is not None:
        sets.append("label = %s")
        params.append(req.label.strip())
    if req.max_uses is not None:
        sets.append("max_uses = %s")
        params.append(req.max_uses)
    if req.expires_at is not None:
        sets.append("expires_at = %s::timestamptz")
        params.append(req.expires_at or None)
    if req.welcome_message is not None:
        sets.append("welcome_message = %s")
        params.append(req.welcome_message)
    if sets:
        row = _db_execute(
            f"UPDATE invite_tokens SET {', '.join(sets)}"
            " WHERE id = %s RETURNING id", params + [token_id])
        if not row:
            raise HTTPException(status_code=404, detail="Token not found")
    if req.rights is not None:
        rights = _validate_rights(req.rights)
        _db_execute("DELETE FROM invite_token_rights WHERE token_id = %s",
                    (token_id,))
        for r in rights:
            _db_execute(
                "INSERT INTO invite_token_rights (token_id, p2p_right)"
                " VALUES (%s, %s) ON CONFLICT DO NOTHING", (token_id, r))
    return {"ok": True}


@router.post("/tokens/{token_id}/revoke")
async def revoke_token(token_id: str) -> Dict[str, Any]:
    """No DELETE by design: the revocation record is what makes
    grant-recovery honour a same-device revocation."""
    row = _db_execute("""
        UPDATE invite_tokens SET revoked_at = NOW()
        WHERE id = %s AND revoked_at IS NULL
        RETURNING id
    """, (token_id,))
    if not row:
        raise HTTPException(status_code=404,
                            detail="Token not found or already revoked")
    return {"ok": True}


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

    # Wake SSE if anything was actually added so connected Friends
    # screens refresh without a manual reload — the auto-adds above
    # bypass NOTIFY sautium_chat (no chat_service involvement).
    if added:
        _wake_chat_sse_clients()

    return {"accepts": accepts, "added": added}


# -- Email verification -------------------------------------------------------

async def _get_own_birth_cert(refresh: bool = False) -> Optional[dict]:
    """This node's identity certificate: the disk cache first (verified on
    load), else the Worker — public read, then self-signed issuance when not
    yet issued — persisted for next time. `refresh` skips the cache (after
    an email upgrade the Worker holds a newer certificate)."""
    from birth_authority import verify_certificate
    import p2p_identity

    identity = _get_identity()
    if not identity:
        return None
    pubkey = identity["public_key_hex"].lower()

    if not refresh:
        cached = p2p_identity.load_certificate(settings)
        if cached is not None:
            return cached

    cert = await _worker_get("/birth-certificate", {"pubkey": pubkey})
    if not cert:
        signature = _sign_message(f"birth:{pubkey}")
        if not signature:
            return None
        cert = await _worker_post("/birth-certificate", {
            "pubkey_hex": pubkey,
            "signature": signature,
        })
    if cert and cert.get("pubkey") == pubkey and verify_certificate(cert):
        p2p_identity.save_certificate(settings, cert)
        return cert
    return None


async def _worker_check_email(identity: dict) -> Optional[dict]:
    """GET /check-email for this identity's configured email: {verified,
    birth_cert?}. A returned certificate is the method:email upgrade — the
    Worker issues it once the verified record is bound to this pubkey."""
    import p2p_identity

    email = identity.get("email")
    if not email:
        return None
    message = f"check-email:{identity['invite_code']}:{email}"
    signature = _sign_message(message)
    if not signature:
        return None
    result = await _worker_get("/check-email", {
        "invite_code": identity["invite_code"],
        "email": email,
        "public_key_hex": identity["public_key_hex"],
        "signature": signature,
    })
    if result and result.get("birth_cert"):
        p2p_identity.save_certificate(settings, result["birth_cert"])
    return result


async def identity_proof_task(stop: threading.Event) -> None:
    """Startup task: make sure this node holds its identity certificate and,
    for method:pow, a verified proof (desktop/p2p/identity_proof.py — the
    same policy the launcher runs). Progress is published to
    user_settings['p2p.identity'] + NOTIFY sautium_identity for the Web UI.
    A node whose email is verified but whose certificate predates v2 gets
    the method:email certificate from /check-email here — no work needed."""
    from desktop.p2p import identity_proof
    import p2p_identity

    def publish(state: dict) -> None:
        try:
            from routers.settings import _write
            _write("p2p.identity", state)
            _db_execute("NOTIFY sautium_identity")
        except Exception as e:
            logger.debug(f"identity state publish failed: {e}")

    backoff = 300
    while not stop.is_set():
        cert = await _get_own_birth_cert()
        if cert is not None and cert["method"] == "pow":
            # Portable identity: the email upgrade may have happened on
            # another device holding the same key — one Worker read before
            # committing minutes of mining to a possibly superseded cert.
            cert = await _get_own_birth_cert(refresh=True) or cert
        if cert is None:
            # Worker unreachable / not configured: retry with backoff — the
            # certificate is a network fact, nothing local can replace it.
            logger.warning(f"identity certificate unavailable — retry in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 3600)
            continue
        if cert["method"] != "email":
            row = _db_query_one("SELECT email_verified FROM user_profile WHERE id = 1")
            if row and row.get("email_verified"):
                result = await _worker_check_email(_get_identity() or {})
                if result and result.get("birth_cert"):
                    cert = result["birth_cert"]
        path = p2p_identity.proof_path(settings)
        if path is None:
            logger.warning("p2p_identity_dir unset — identity proof cannot be stored")
            return
        from desktop.p2p import load_meter
        meter = load_meter.current()
        await asyncio.to_thread(identity_proof.ensure_identity_proof, cert, path,
                                stop=stop, on_state=publish,
                                hold=meter.mining_hold if meter else (lambda: None))
        return


@router.get("/email/status")
async def email_status() -> Dict[str, Any]:
    """Check if the configured P2P email is verified on the Worker.

    Persistent state: user_profile.email_verified (boolean). When
    True, the Profile screen renders "verified" without hitting the
    Worker. Reset to False on email change or password change — both
    flows must clear the flag because the new state invalidates the
    Worker's stored mapping (invite_code derives from the password)."""
    identity = _get_identity()
    if not identity or not identity.get("email"):
        return {"email": "", "verified": False}

    email = identity["email"]
    row = _db_query_one("SELECT email_verified FROM user_profile WHERE id = 1")
    if row and row.get("email_verified"):
        return {"email": email, "verified": True}

    result = await _worker_check_email(identity)
    verified = bool(result and result.get("verified"))
    if verified:
        _db_execute(
            "UPDATE user_profile SET email_verified = TRUE WHERE id = 1"
        )
    return {"email": email, "verified": verified}


@router.post("/email/send-code")
async def email_send_code() -> Dict[str, Any]:
    """Ask the Worker to generate and email a verification code.

    The code exists only on the Worker (hash, 15 min TTL) — mailbox
    ownership is proven server-side at /email/verify-code."""
    identity = _get_identity()
    if not identity or not identity.get("email"):
        raise HTTPException(400, "No email configured (set P2P_EMAIL in .env)")

    email = identity["email"]
    invite_code = identity["invite_code"]
    signature = _sign_message(f"sendcode:{invite_code}:{email}")
    if not signature:
        raise HTTPException(500, "Cannot sign request")

    result = await _worker_post("/send-verification", {
        "to": email,
        "invite_code": invite_code,
        "public_key_hex": identity["public_key_hex"],
        "signature": signature,
        "from_username": identity["username"],
    })

    if not result or result.get("status") != "sent":
        raise HTTPException(502, "Failed to send verification email")

    return {"status": "sent", "email": email}


@router.post("/email/verify-code")
async def email_verify_code(req: VerifyCodeRequest) -> Dict[str, Any]:
    """Register the email on the Worker: it checks the entered code against
    its stored hash and requires this node's birth certificate (the verified
    record then carries born_at)."""
    identity = _get_identity()
    if not identity or not identity.get("email"):
        raise HTTPException(400, "No email configured")

    cert = await _get_own_birth_cert()
    if cert is None:
        raise HTTPException(502, "Birth certificate unavailable")

    email = identity["email"]
    invite_code = identity["invite_code"]
    signature = _sign_message(f"register:{invite_code}:{email}")
    if not signature:
        raise HTTPException(500, "Cannot sign request")

    result = await _worker_post("/register-email", {
        "invite_code": invite_code,
        "email": email,
        "public_key_hex": identity["public_key_hex"],
        "signature": signature,
        "code": req.code.strip().upper(),
        "birth_cert": cert,
    })

    if not result or result.get("status") != "registered":
        return {"verified": False, "error": "Invalid or expired code"}

    if result.get("birth_cert"):
        import p2p_identity
        p2p_identity.save_certificate(settings, result["birth_cert"])

    # Persist so future /email/status checks return verified without a
    # Worker round-trip. Email-change and password-change flows (when
    # they land) must clear this flag.
    _db_execute("UPDATE user_profile SET email_verified = TRUE WHERE id = 1")
    return {"verified": True, "email": email}


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
