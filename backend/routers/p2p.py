"""
P2P API routes for Friends / Chat in web UI.

Provides endpoints for account info, friend management, and messaging.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List
from fastapi import APIRouter

import psycopg2
from config import settings

router = APIRouter(prefix="/api/p2p", tags=["p2p"])

_db_conn = None


def _get_db():
    global _db_conn
    if _db_conn is None or _db_conn.closed:
        _db_conn = psycopg2.connect(settings.database_url)
        _db_conn.autocommit = True
        with _db_conn.cursor() as cur:
            cur.execute("SET timezone = 'UTC'")
    return _db_conn


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
    conn = _get_db()
    with conn.cursor() as cur:
        cur.execute("""
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
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        for row in rows:
            for k in ("added_at", "last_seen"):
                if row.get(k):
                    row[k] = row[k].isoformat()
        return rows


@router.post("/friends/add")
async def add_friend(body: Dict[str, str]) -> Dict[str, Any]:
    """Add a friend by invite code."""
    invite_code = body.get("invite_code", "").strip()
    if not invite_code or "#" not in invite_code:
        return {"error": "Invalid invite code format"}

    username = invite_code.split("#")[0]

    conn = _get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO friends (username, public_key_hex, invite_code, display_name)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (public_key_hex) DO UPDATE
                    SET invite_code = EXCLUDED.invite_code,
                        username = EXCLUDED.username
                RETURNING id
            """, (username, f"pending:{invite_code}", invite_code, username))
            row = cur.fetchone()
            return {"id": row[0], "username": username, "invite_code": invite_code}
    except Exception as e:
        return {"error": str(e)}


@router.get("/friends/{friend_id}/messages")
async def get_messages(friend_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    """Get chat messages with a friend."""
    conn = _get_db()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, direction, content, timestamp, delivered, read,
                   message_uuid
            FROM p2p_messages
            WHERE friend_id = %s
            ORDER BY id DESC LIMIT %s
        """, (friend_id, limit))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        rows.reverse()
        for row in rows:
            if row.get("timestamp"):
                row["timestamp"] = row["timestamp"].isoformat() + "+00:00"
        # Mark as read
        cur.execute("""
            UPDATE p2p_messages
            SET read = TRUE
            WHERE friend_id = %s AND direction = 'in' AND read = FALSE
        """, (friend_id,))
        return rows


@router.post("/friends/{friend_id}/send")
async def send_message(friend_id: int, body: Dict[str, str]) -> Dict[str, Any]:
    """Send a message to a friend (stored locally, P2P delivery via manager)."""
    content = body.get("content", "").strip()
    if not content:
        return {"error": "Empty message"}

    msg_uuid = str(uuid.uuid4())
    conn = _get_db()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO p2p_messages
                (friend_id, direction, content, timestamp, delivered,
                 message_uuid)
            VALUES (%s, 'out', %s, %s, FALSE, %s)
            RETURNING id
        """, (friend_id, content, datetime.now(timezone.utc), msg_uuid))
        msg_id = cur.fetchone()[0]
        return {"id": msg_id, "message_uuid": msg_uuid, "status": "queued"}
