"""
P2P API routes for Friends / Chat in web UI.

Provides endpoints for account info, friend management, and messaging.
"""

import asyncio
import base64
import json
import logging
import re
import ssl
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

import httpx

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import psycopg2
import psycopg2.extensions
from config import settings
import device_auth
from routers.auth import StepUp, boxed, step_up
from db_pool import Listener, db_query as _db_query, db_query_one as _db_query_one, db_execute as _db_execute, get_conn as _get_conn

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


# The account email acts at the Worker and outlasts "Sign out everywhere": a
# token holder's own mailbox there would take the node's invite and
# acceptance mails. So the flow — the address set, its code sent, the code
# verified — takes the account's password at its first step (routers/auth.
# step_up), whose sealed reply carries the grant the later steps quote; the
# grant lives as long as the Worker's code does.
EMAIL_FLOW_SECONDS = 15 * 60


class SetEmailRequest(StepUp):
    email: str = Field(..., min_length=3, max_length=320)


class SendCodeRequest(StepUp):
    grant: str = Field(default="", max_length=64)


class VerifyCodeRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=10)
    grant: str = Field(default="", max_length=64)


# -- Chat SSE infrastructure --------------------------------------------------

_chat_sse_clients: list = []  # list of (asyncio.Event, asyncio.AbstractEventLoop)
_chat_sse_lock = threading.Lock()


def _wake_chat_sse_clients():
    """Thread-safe: signal all chat SSE generators to push updates."""
    with _chat_sse_lock:
        for evt, loop in _chat_sse_clients:
            loop.call_soon_threadsafe(evt.set)


def chat_sse_register(evt: asyncio.Event, loop) -> None:
    """Chat wakeups for the multiplexed /api/events channel — the standalone
    /chat/stream endpoint is gone (a browser caps an origin at 6 connections
    for the whole browser, and the web UI holds exactly one)."""
    with _chat_sse_lock:
        _chat_sse_clients.append((evt, loop))


def chat_sse_unregister(evt: asyncio.Event, loop) -> None:
    with _chat_sse_lock:
        try:
            _chat_sse_clients.remove((evt, loop))
        except ValueError:
            pass


def _on_chat_notify(notifies: list) -> None:
    """sautium_chat: every chat stream re-fetches. An 'out' insert — the
    trg_p2p_messages_notify payload msg:{friend_id}:{direction} — is a reply
    for that friend: their relay wake stream is pinged so they pull
    immediately, and this node's chat client pushes it at once (the one
    LISTEN for both: the client takes the notes from here)."""
    from desktop.p2p.chat_client import replied_friends
    from p2p_chat import chat_client
    _wake_chat_sse_clients()
    client = chat_client()
    if client is not None:
        client.chat_notes(notifies)
    out_friend_ids = replied_friends(notifies)
    if out_friend_ids:
        from routers.peer_chat import ping_wake_by_friend_id
        for fid in out_friend_ids:
            ping_wake_by_friend_id(fid)


_chat_listener = Listener("chat-sse-listener", ("sautium_chat",), _on_chat_notify)


def start_chat_listener():
    """Start the background DB listener thread for chat SSE."""
    _chat_listener.start()


def stop_chat_listener():
    """Stop the background DB listener thread."""
    _chat_listener.stop()


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


def forget_identity() -> None:
    """After a key rotation: the next Worker request signs with the new key."""
    global _cached_private_key
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


def _busy(resp) -> HTTPException:
    """A Worker refusal for volume (429) as the person reads it, with the
    wait the Worker gave in Retry-After: a minute behind its per-minute
    brakes, hours once a daily cap is reached."""
    retry = resp.headers.get("Retry-After", "")
    wait = int(retry) if retry.isdigit() else 0
    if wait <= 90:
        when = "in a minute"
    elif wait < 90 * 60:
        when = f"in about {round(wait / 60)} minutes"
    else:
        when = f"in about {round(wait / 3600)} hours"
    return HTTPException(429, f"The verification service is busy — please try again {when}.",
                         headers={"Retry-After": str(wait)} if wait else None)


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
                raise _busy(resp)
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
                raise _busy(resp)
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
        return {"invite_code": None, "share_code": None, "username": None}
    from desktop.p2p import chat_wire
    return {
        "invite_code": identity["invite_code"],
        # What a person copies: the long form, which binds a contact made
        # from it to this key, not to the 48 bits of the short one.
        "share_code": chat_wire.long_code(identity["username"], identity["public_key_hex"]),
        "username": identity["username"],
        "email": identity.get("email", ""),
        "public_key_hex": identity["public_key_hex"],
    }


def _record_email_verified(verified: bool) -> None:
    """`user_profile.email_verified`, and its word to the pages: the identity
    line offers the verification while it is missing and the guidance trail
    leads to it (routers.settings.identity_event, _guidance_state). Said by
    the writer — until 2026-11-24 a page saw it at whatever woke it next."""
    _db_execute("UPDATE user_profile SET email_verified = %s WHERE id = 1", (verified,))
    _db_execute("NOTIFY sautium_identity")


@router.put("/account/email")
async def set_account_email(opened=boxed(SetEmailRequest)) -> Dict[str, Any]:
    """Set or change the account email, on the account's password; the
    sealed answer carries the grant its code steps quote. Persisted to
    node_info.json (desktop mode only) and resets user_profile.email_verified
    — both a new email and changing an existing one invalidate the Worker's
    `invite_code → email` mapping for this identity."""
    req, reply = opened
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
    await step_up(req)
    try:
        data = json.loads(info_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"Cannot read node_info.json: {e}")

    data["email"] = email
    data["email_verified"] = False
    info_path.write_text(json.dumps(data, indent=2), encoding="utf-8")   # resolve_identity re-reads on mtime change

    _record_email_verified(False)

    # The step the wizard takes before it sends a code: a mailbox this
    # identity verified before — a reinstall with the same pair, an address
    # removed and re-added — is still mapped on the Worker, and asking is
    # what keeps the code dialog from opening for nothing. The mtime bump
    # above is what makes _get_identity() read the new address. A Worker
    # that is busy leaves the flag False: /email/status re-derives it on
    # the next Profile render, and the Verify row stays.
    try:
        result = await _check_email_upgrade(_get_identity())
    except HTTPException as e:
        logger.warning(f"check-email after email change: {e.detail}")
        result = None
    verified = _verified_for_this_key(result)
    if verified:
        data["email_verified"] = True
        info_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        _record_email_verified(True)

    return reply({"email": email, "email_verified": verified,
                  "grant": None if verified else device_auth.grant("email", EMAIL_FLOW_SECONDS)})


# One server-side truth for "online" — imported by routers/settings.py's
# sync state; the client renders row.is_online and computes nothing.
ONLINE_WINDOW_MIN = 5

# Chat-recency order: whoever wrote (or was written to) last comes first,
# so a new message lifts its row to the top. friends.last_activity_at is
# maintained by the p2p_messages trigger and seeded from added_at. Blocked
# contacts sink below everyone else — written as `NOT is_blocked DESC` so
# all three keys share a direction and the keyset cursor stays one tuple
# comparison, which is also the shape idx_friends_recent has.
_FRIEND_SORT = "(NOT f.is_blocked) DESC, f.last_activity_at DESC, f.id DESC"

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
    """A friend row as the client renders it. `reach`, on the Support
    contact's row alone: this node can deliver to Support or deposit for it
    (its Worker mailbox, B-92) — the row and the chat header say what becomes
    of a message, never a presence: a person reads "offline" as "nobody will
    get this" (PI-NODE-2B Q21). It takes P2P on and a certificate the Worker
    issued (it takes mail from no other key); a `pending:` Support contact
    is not resolved yet, and what is written to it waits on this node.
    Accepted gaps: on a launcher-mode backend the P2P switch read is the
    backend's, not whether the launcher's P2P runs; and a deposit the
    mailbox refused (full, a cap) leaves this line while that bubble says
    "Not sent yet"."""
    import master_node
    from p2p_identity import served_certificate
    for k in ("added_at", "last_seen", "last_activity_at"):
        if row.get(k):
            row[k] = row[k].isoformat()
    if "public_key_hex" in row:
        if row.get("source") == "master":
            row["reach"] = bool(row["public_key_hex"] == master_node.MASTER_PUBKEY_HEX
                                and settings.p2p_enabled
                                and served_certificate(settings) is not None)
    return row


def support_known() -> bool:
    """Support answers this node: its contact is resolved to the master's key
    and holds the grant the master signed for it. On a node with no identity
    of its own that is one peer's memory of the account — the master met this
    key on another machine and re-accepts a friend with no identity gate —
    and nothing any other node can check (PI-NODE-2C §10)."""
    import master_node
    row = _db_query_one(
        """SELECT EXISTS (SELECT 1 FROM friends f JOIN friend_grants g ON g.friend_id = f.id
                           WHERE f.source = 'master' AND f.public_key_hex = %(key)s) AS known""",
        {"key": master_node.MASTER_PUBKEY_HEX})
    return bool(row and row["known"])


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
            assert isinstance(after, list) and len(after) == 3
        except Exception:
            raise HTTPException(status_code=400, detail="Bad cursor")

    cur_clause = ""
    cur_params: list = []
    if after:
        cur_clause = (" AND ((NOT f.is_blocked), f.last_activity_at, f.id)"
                      " < (%s, %s::timestamptz, %s)")
        cur_params = [bool(after[0]), after[1], int(after[2])]

    items = _db_query(
        _FRIENDS_SELECT + f" WHERE f.favorite = FALSE{q_clause}{cur_clause}"
        f" ORDER BY {_FRIEND_SORT} LIMIT %s",
        q_params + cur_params + [limit + 1])

    next_cursor = None
    if len(items) > limit:
        items = items[:limit]
        last = items[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps(
            [not last["is_blocked"], last["last_activity_at"].isoformat(),
             last["id"]]).encode()).decode()

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
async def add_friend(opened=boxed(AddFriendRequest)) -> Dict[str, Any]:
    """The pasted share string rides the credential box: an invite link or a
    mailed invite carries another node's bearer token, which a LAN reader
    could spend first; the answer comes back sealed (add_share_string)."""
    req, reply = opened
    return reply(await add_share_string(req.invite_code))


async def add_share_string(string: str) -> Dict[str, Any]:
    """Add a friend by share string (chat_wire.parse_share): a code, short
    or long, an invite link (`…#<token-uuid>`) or a mailed invite
    (`…#i-<token>`).

    The contact waits for its handshake, bound to the long code's
    fingerprint when the string carried one. A mailed invite is spent on
    the Worker before the answer (`invite` says what came of it — the
    person reads it); an accepted one binds the contact to the inviter's
    own key and arms its nudge (accepted_invite_id). An add by plain code,
    or with an invite the Worker turned down (used, expired, another code's
    — _ADD_AFTER), tells the code's owner, who adds back; one the Worker
    refused this node for, or did not answer, tells nobody — the same string
    again tries again. A new fingerprint drops the arm of the one before. An
    invite link skips the Worker: its token handshake replaces mutual add.
    The master's code, in any form, waits for the master's own key; a manual
    re-add of it is consent: it clears the removal flag and re-arms the
    auto-contact. A request from that code (its owner added ours first) is
    answered by the add."""
    from desktop.p2p import chat_wire

    try:
        share = chat_wire.parse_share(string)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid invite code")
    return await _add_share(share, tell=True)


async def _add_share(share, *, tell: bool) -> Dict[str, Any]:
    """The add of a parsed share string: add_friend's, and Add back's —
    `tell` False there, the adder having added our code already."""
    from desktop.p2p import chat_wire
    from master_node import MASTER_INVITE_CODE, MASTER_PUBKEY_HEX, MASTER_TOKEN_ID

    invite_code, token_id = share.code, share.link_token
    username = invite_code.split("#")[0]
    identity = _get_identity()
    if identity and invite_code == identity["invite_code"]:
        raise HTTPException(status_code=400, detail="That is your own invite code.")

    source = "manual"
    fingerprint = bytes.fromhex(share.fingerprint) if share.fingerprint else None
    if MASTER_INVITE_CODE and invite_code == MASTER_INVITE_CODE:
        source = "master"
        token_id = token_id or MASTER_TOKEN_ID or None
        if MASTER_PUBKEY_HEX:
            fingerprint = bytes.fromhex(chat_wire.key_fingerprint(MASTER_PUBKEY_HEX))
        _db_execute(
            "DELETE FROM user_settings WHERE key = 'p2p.master_removed'")

    row = _wait_for(username, invite_code, source, token_id, fingerprint)

    invite = None
    if share.invite_token:
        invite = await _accept_invite(share, row["id"], row["key_fingerprint"])
    if tell and not token_id and (share.invite_token is None or invite in _ADD_AFTER):
        asyncio.create_task(_tell_code_added(invite_code))

    _wake_chat_sse_clients()
    return {"id": row["id"], "username": username,
            "invite_code": invite_code, "token_id": token_id, "invite": invite}


def _wait_for(username: str, invite_code: str, source: str, token_id: Optional[str],
              fingerprint: Optional[bytes]) -> dict:
    """The contact an add makes — waiting for its handshake under its code,
    bound to `fingerprint` when there is one — and the request from that
    code answered (an add is the person's yes, over a dismissal before), in
    one statement → {id, key_fingerprint}."""
    try:
        return _db_execute("""
            WITH contact AS (
                INSERT INTO friends (username, public_key_hex, invite_code,
                                     display_name, source, join_token_id, key_fingerprint)
                VALUES (%s, %s, %s, %s, %s::friend_source, %s, %s)
                ON CONFLICT (public_key_hex) DO UPDATE
                    SET invite_code = EXCLUDED.invite_code,
                        username = EXCLUDED.username,
                        join_token_id = COALESCE(EXCLUDED.join_token_id,
                                                 friends.join_token_id),
                        key_fingerprint = COALESCE(EXCLUDED.key_fingerprint,
                                                   friends.key_fingerprint),
                        accepted_invite_id = CASE
                            WHEN EXCLUDED.key_fingerprint IS DISTINCT FROM friends.key_fingerprint
                                 AND EXCLUDED.key_fingerprint IS NOT NULL THEN NULL
                            ELSE friends.accepted_invite_id END
                RETURNING id, upper(encode(key_fingerprint, 'hex')) AS key_fingerprint
            ), answered AS (
                UPDATE friend_requests SET answered_at = requested_at, ignored_at = NULL
                 WHERE lower(invite_code) = lower(%s)
            )
            SELECT * FROM contact
        """, (username, f"pending:{invite_code}", invite_code, username,
              source, token_id, fingerprint, invite_code))
    except Exception as e:
        logger.error(f"Failed to add friend: {e}")
        raise HTTPException(status_code=500, detail="Failed to add friend")


# The Worker's refusals of an invite (INVITE_REFUSALS there) after which the
# contact is an add by plain code, and its owner is told so.
_ADD_AFTER = ("used", "not_found", "not_this_code", "bad_token")
# The Worker's window for a signed time (tsFresh there).
_WORKER_TS_WINDOW_S = 300


def _clock_off(resp, ts: int) -> bool:
    """This node's clock farther from the Worker's than the Worker takes a
    signed time from — read off its answer's Date."""
    try:
        theirs = parsedate_to_datetime(resp.headers.get("date", "")).timestamp()
    except (TypeError, ValueError):
        return False
    return abs(theirs - ts) > _WORKER_TS_WINDOW_S


async def _accept_invite(share, friend_id: int, bound: Optional[str]) -> str:
    """Spend a mailed invite's token on the Worker → what came of it, for
    the person: "accepted" (the contact is bound to the inviter's own key —
    checked against `bound`, the contact's fingerprint, when it has one,
    else the short code's 48 bits — and its nudge armed), a refusal's reason
    (_ADD_AFTER, or "own"), "clock" (the Worker refused this node's time:
    its answer's Date is more than the Worker's five minutes away),
    "refused" (the Worker refused this node itself: its key, its name), or
    "unreachable" (the same invite string again tries again). The token is a
    secret until it is spent: never logged."""
    from desktop.p2p import chat_wire
    identity = _get_identity()
    if not identity or not await _born_quietly():
        return "unreachable"
    ts = int(datetime.now(timezone.utc).timestamp())
    inviteid = chat_wire.invite_id(share.invite_token)
    signature = _sign_message(chat_wire.accept_text(
        ts, inviteid, share.code, identity["invite_code"], identity["public_key_hex"]))
    if not signature:
        return "unreachable"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            resp = await client.post(f"{VERIFY_WORKER_URL}/accept-invite", headers=_WORKER_HEADERS, json={
                "my_invite_code": identity["invite_code"],
                "their_invite_code": share.code,
                "public_key_hex": identity["public_key_hex"],
                "ts": ts,
                "invite_token": share.invite_token,
                "signature": signature,
            })
        answer = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    except Exception as e:
        logger.warning(f"Accept-invite for {share.code} failed: {type(e).__name__}")
        return "unreachable"
    if not isinstance(answer, dict):
        answer = {}
    if resp.status_code != 200:
        reason = answer.get("reason")
        if reason in _ADD_AFTER or reason == "own":
            logger.info(f"Accept-invite for {share.code} refused: {reason}")
            return reason
        if resp.status_code == 403:
            logger.warning(f"Accept-invite for {share.code} refused this node: {answer.get('error')}")
            return "clock" if _clock_off(resp, ts) else "refused"
        logger.warning(f"Accept-invite for {share.code} answered {resp.status_code}")
        return "unreachable"
    inviter = answer.get("inviter")
    inviter = inviter.get("public_key_hex", "") if isinstance(inviter, dict) else ""
    known = bound or chat_wire.invite_digest(share.code)
    if not (isinstance(inviter, str) and re.fullmatch(r"[0-9a-f]{64}", inviter)
            and chat_wire.key_fingerprint(inviter).startswith(known)):
        logger.warning(f"Accept-invite for {share.code} answered with a key the invite does not name")
        return "unreachable"
    _db_execute("UPDATE friends SET key_fingerprint = %s, accepted_invite_id = %s"
                " WHERE id = %s AND public_key_hex LIKE 'pending:%%'",
                (bytes.fromhex(chat_wire.key_fingerprint(inviter)), chat_wire.invite_id(share.invite_token),
                 friend_id))
    logger.info(f"Invite from {share.code} accepted (notified={answer.get('notified')})")
    return "accepted"


async def _tell_code_added(their_invite_code: str) -> None:
    """Tell a code's owner, through the Worker, that we added it (an add by
    plain code): the Worker keeps the add for the owner's pickup, which
    offers Add back in Friends, and mails the owner."""
    from desktop.p2p import chat_wire
    identity = _get_identity()
    if not identity or not await _born_quietly():
        return
    ts = int(datetime.now(timezone.utc).timestamp())
    signature = _sign_message(chat_wire.add_text(
        ts, their_invite_code, identity["invite_code"], identity["public_key_hex"]))
    if not signature:
        logger.warning("Cannot sign accept-invite — no private key")
        return
    try:
        result = await _worker_post("/accept-invite", {
            "my_invite_code": identity["invite_code"],
            "their_invite_code": their_invite_code,
            "public_key_hex": identity["public_key_hex"],
            "ts": ts,
            "signature": signature,
        })
    except HTTPException as e:
        logger.info(f"Add of {their_invite_code} not told: {e.detail}")
        return
    if result and result.get("status") == "added":
        logger.info(f"Add of {their_invite_code} told (notified={result.get('notified')})")


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

class TokenCreate(StepUp):
    label: str = Field(default="", max_length=128)
    rights: List[str] = Field(default_factory=lambda: ["can_message"])
    max_uses: Optional[int] = Field(default=None, ge=1)
    expires_at: Optional[str] = None          # ISO timestamp
    welcome_message: Optional[str] = Field(default=None, max_length=2000)
    # Custom id = the device-transfer path: re-create the token with the
    # UUID from your old share string and existing grants keep working.
    id: Optional[str] = None


class TokenPatch(StepUp):
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


# A token with rights is spent later by whoever holds its link, and outlasts
# "Sign out everywhere": minting one, or widening any link — a right added,
# more uses, a later expiry or none — takes the account's password
# (routers/auth.step_up): a link widened without rights now would carry the
# reach nobody approved into the rights its owner adds later. Minting a link
# without rights is a friend's add, which stays on the device token;
# narrowing is never asked.
_TOKEN_TERMS = """
    SELECT t.max_uses, t.expires_at,
           ARRAY(SELECT p2p_right::text FROM invite_token_rights
                 WHERE token_id = t.id ORDER BY 1) AS rights,
           (%(expires)s IS NOT NULL AND t.expires_at IS NOT NULL
            AND (NULLIF(%(expires)s, '') IS NULL
                 OR NULLIF(%(expires)s, '')::timestamptz > t.expires_at)) AS extends
    FROM invite_tokens t WHERE t.id = %(id)s
"""


def _widens(terms: Dict[str, Any], req: "TokenPatch", rights: Optional[List[str]]) -> bool:
    """Whether the patch leaves the token reaching further than before: a
    right added, more uses, a later expiry or none."""
    before = set(terms["rights"])
    after = before if rights is None else set(rights)
    more_uses = (req.max_uses is not None and terms["max_uses"] is not None
                 and req.max_uses > terms["max_uses"])
    return bool(after - before) or more_uses or bool(terms["extends"])


@router.post("/tokens")
async def create_token(opened=boxed(TokenCreate)) -> Dict[str, Any]:
    """Mint an invite link; with any right, on the account's password. The
    body rides the box (a custom id is typed from an old share string) and
    the share string comes back sealed."""
    req, reply = opened
    rights = _validate_rights(req.rights)
    token_id = str(uuid.uuid4())
    if req.id:
        try:
            token_id = str(uuid.UUID(req.id.strip()))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid token id")
    if rights:
        await step_up(req)
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
    if identity:
        from desktop.p2p import chat_wire
        share = f"{chat_wire.long_code(identity['username'], identity['public_key_hex'])}#{token_id}"
    else:
        share = token_id
    return reply({"id": token_id, "share_string": share, "rights": rights})


@router.patch("/tokens/{token_id}")
async def patch_token(token_id: str, opened=boxed(TokenPatch)) -> Dict[str, Any]:
    """Edits affect FUTURE uses only — existing friendships keep their
    rights snapshot. A patch that widens the token (_widens) takes the
    account's password, and lands only on the token as it was when the
    password was asked for: under the row's lock, with the edit, a vouched
    patch finds the terms it was judged on (another edit during the
    derivation is a 409, asked again), an unvouched one is judged again."""
    import psycopg2.extras
    from db_pool import transaction
    req, reply = opened
    rights = _validate_rights(req.rights) if req.rights is not None else None
    terms_args = {"id": token_id, "expires": req.expires_at}
    terms = _db_query_one(_TOKEN_TERMS, terms_args)
    if not terms:
        raise HTTPException(status_code=404, detail="Token not found")
    judged, vouched = terms, _widens(terms, req, rights)
    if vouched:
        await step_up(req)
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
    with transaction(psycopg2.extras.RealDictCursor) as cur:
        cur.execute(_TOKEN_TERMS + " FOR UPDATE OF t", terms_args)
        terms = cur.fetchone()
        if not terms:
            raise HTTPException(status_code=404, detail="Token not found")
        if vouched and terms != judged:
            raise HTTPException(status_code=409,
                                detail="This link changed meanwhile: open it again and save")
        if not vouched and _widens(terms, req, rights):
            raise HTTPException(status_code=403,
                                detail="This link changed meanwhile, and the change now widens it: "
                                       "type the account's password",
                                headers={"X-Sautium-Auth-Error": "step-up"})
        if sets:
            cur.execute(f"UPDATE invite_tokens SET {', '.join(sets)} WHERE id = %s",
                        params + [token_id])
        if rights is not None:
            cur.execute("DELETE FROM invite_token_rights WHERE token_id = %s", (token_id,))
            for r in rights:
                cur.execute("INSERT INTO invite_token_rights (token_id, p2p_right)"
                            " VALUES (%s, %s) ON CONFLICT DO NOTHING", (token_id, r))
    return reply({"ok": True})


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


@router.get("/friends/{friend_id}/messages")
async def get_messages(friend_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    """Get chat messages with a friend, oldest first.

    Ordered by SEND time, not insertion order: an incoming row carries the
    sender's own timestamp, so anything that arrived out of order — a relay
    catching up after an offline stretch, a history pull that lands a week
    of conversation in one batch — reads as the conversation actually
    happened. Sorting by id told you when THIS node heard about a message,
    which is not what a thread is."""
    from desktop.p2p import chat_wire
    limit = min(limit, 500)
    rows = _db_query("""
        SELECT id, direction, content, timestamp, delivered, read,
               message_uuid,
               -- an outgoing message's state (B-92): delivered is the proof;
               -- kept = handed to the master's Worker mailbox, within the
               -- time it keeps mail; waiting = here
               CASE WHEN direction = 'in' THEN NULL
                    WHEN delivered THEN 'delivered'
                    WHEN parked_at > now() - make_interval(secs => %s) THEN 'kept'
                    ELSE 'waiting' END AS state
        FROM p2p_messages
        WHERE friend_id = %s
        ORDER BY timestamp DESC, id DESC LIMIT %s
    """, (chat_wire.MAILBOX_TTL_S, friend_id, limit))
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
            updated = cur.rowcount
    # After the commit, not inside it: a woken client re-reads the count
    # and must not see the rows it is being told about as still unread.
    if updated:
        _wake_chat_sse_clients()
    return {"ok": True, "updated": updated}


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

    # Wake SSE clients immediately (sender sees own message). Delivery is
    # the insert's NOTIFY (trg_p2p_messages_notify): the launcher's delivery
    # loop and its relay pings, and this backend's own, wake on it.
    _wake_chat_sse_clients()

    return {"id": msg_id, "message_uuid": msg_uuid, "status": "queued"}


@router.post("/invite-by-email")
async def invite_by_email(req: InviteByEmailRequest) -> Dict[str, Any]:
    """Send an invite email to someone via the Cloudflare Worker. The Worker
    sends invites only from an account with a verified email, so one
    without hears why here, before any request leaves the node. The mail
    carries a single-use token; this node keeps the invite's id beside the
    address, and its pickup adds whoever accepts."""
    from desktop.p2p import chat_wire
    identity = _get_identity()
    if not identity:
        raise HTTPException(status_code=400, detail="No P2P account configured")
    if not (await email_status())["verified"]:
        raise HTTPException(
            status_code=400,
            detail="Verify your email in Profile first — an invite goes out under your verified address.")
    if not await _born():
        raise HTTPException(status_code=502, detail="Birth certificate unavailable")

    invite_code = identity["invite_code"]
    public_key_hex = identity["public_key_hex"]
    to_email = req.to_email.strip()

    ts = int(datetime.now(timezone.utc).timestamp())
    signature = _sign_message(chat_wire.send_text(ts, invite_code, to_email, req.message))
    if not signature:
        raise HTTPException(status_code=500, detail="Cannot sign request")

    result = await _worker_post("/send-invite", {
        "to": to_email,
        "invite_code": invite_code,
        "public_key_hex": public_key_hex,
        "ts": ts,
        "signature": signature,
        "message": req.message,
    })
    if not result:
        raise HTTPException(status_code=502, detail="Worker request failed")
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    try:
        inviteid = str(uuid.UUID(str(result.get("invite_id"))))
    except ValueError:
        raise HTTPException(status_code=502, detail="Worker request failed")
    _db_execute("INSERT INTO sent_invites (to_email, invite_id) VALUES (%s, %s)",
                (to_email, inviteid))

    return {
        "status": "sent",
        "to": to_email,
        "verified_sender": result.get("verified_sender", False),
    }


# The Friends screen's pickups — a Worker request each, on our key's day of
# 100 (the Worker's pending.key with an invite of ours kept, requests.key
# with none) beside the launcher's start and its nudges,
# and a second node of the same account's: at most one a minute and
# SCREEN_PICKUPS_PER_DAY in any 24 h of this run of the backend (a restart
# begins a new share). A screen past them shows what is here.
SCREEN_PICKUP_INTERVAL_S = 60
SCREEN_PICKUPS_PER_DAY = 40
_screen_pickups: deque = deque()
_screen_pickups_lock = threading.Lock()


def _screen_pickup() -> bool:
    """One of the Friends screen's pickups, if one is left now."""
    now = time.monotonic()
    with _screen_pickups_lock:
        while _screen_pickups and _screen_pickups[0] <= now - 86400:
            _screen_pickups.popleft()
        if len(_screen_pickups) >= SCREEN_PICKUPS_PER_DAY or (
                _screen_pickups and now - _screen_pickups[-1] < SCREEN_PICKUP_INTERVAL_S):
            return False
        _screen_pickups.append(now)
        return True


@router.get("/pending-accepts")
async def pending_accepts() -> Dict[str, Any]:
    """The pickup of our accepted invites and of the adds of our code, when
    Friends opens (email_verify.pick_up — the launcher's start and a peer's
    nudge run the same), while the screen's share of the Worker's day
    allows (_screen_pickup): {added: [codes], requested: [codes]} — the
    contacts it added, each waiting for its handshake, and the new requests
    waiting for Add back."""
    from desktop.p2p import email_verify
    nothing = {"added": [], "requested": []}
    identity = _get_identity()
    key = _get_private_key()
    if not identity or not key or not _screen_pickup() or not await _born_quietly():
        return nothing

    def pick_up():
        with _get_conn() as conn:
            return email_verify.pick_up(
                conn, identity["invite_code"], identity["public_key_hex"], key.sign)

    picked = await asyncio.to_thread(pick_up)
    if picked["added"] or picked["requested"]:
        _wake_chat_sse_clients()
    return {what: [{"invite_code": code} for code in codes] for what, codes in picked.items()}


# Requests shown at once: the Worker's page is 300, and a person answers a
# handful.
FRIEND_REQUESTS_SHOWN = 100


# A request Friends shows: waiting (neither dismissed nor answered by its
# latest add), and no contact of ours under its code or key, nor one whose
# key it was before a rotation.
_REQUEST_WAITING = """
    r.ignored_at IS NULL AND (r.answered_at IS NULL OR r.answered_at < r.requested_at)
    AND NOT EXISTS (SELECT 1 FROM friends f
                     WHERE f.public_key_hex = r.public_key_hex
                        OR f.previous_public_key_hex = r.public_key_hex
                        OR lower(f.invite_code) = lower(r.invite_code))
"""


@router.get("/friend-requests")
async def list_friend_requests() -> Dict[str, Any]:
    """Whoever added our code by plain code and waits for Add back, the
    latest first, each with its long code — a username is anybody's to
    choose, the code tells keys apart — and whether a contact of ours goes
    by that name already."""
    from desktop.p2p import chat_wire
    rows = _db_query(f"""
        SELECT r.id, r.username, r.invite_code, r.public_key_hex, r.requested_at,
               EXISTS (SELECT 1 FROM friends f
                        WHERE lower(f.username) = lower(r.username)
                           OR lower(f.display_name) = lower(r.username)) AS name_taken
          FROM friend_requests r
         WHERE {_REQUEST_WAITING}
         ORDER BY r.requested_at DESC, r.id DESC
         LIMIT %s
    """, (FRIEND_REQUESTS_SHOWN,))
    return {"items": [{
        "id": r["id"], "username": r["username"], "invite_code": r["invite_code"],
        "share_code": chat_wire.long_code(r["username"], r["public_key_hex"]),
        "name_taken": r["name_taken"], "requested_at": r["requested_at"].isoformat(),
    } for r in rows]}


@router.post("/friend-requests/{request_id}/add")
async def add_back(request_id: int) -> Dict[str, Any]:
    """Add back whoever added our code: the add of its code, bound to the
    key its signed statement verified under, waiting for its handshake like
    any add — the adder's own contact completes it. No Worker call: the
    adder added this code already. The request is answered. Only a request
    Friends shows: a screen gone stale meanwhile (a contact made since) adds
    no second contact."""
    from desktop.p2p import chat_wire
    req = _db_query_one(f"SELECT r.invite_code, r.public_key_hex FROM friend_requests r"
                        f" WHERE r.id = %s AND {_REQUEST_WAITING}", (request_id,))
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    return await _add_share(chat_wire.Share(req["invite_code"], chat_wire.key_fingerprint(req["public_key_hex"])),
                            tell=False)


@router.post("/friend-requests/{request_id}/dismiss")
async def dismiss_friend_request(request_id: int) -> Dict[str, Any]:
    """Hide a request; a later pickup of the same adder keeps it hidden. An
    add of that code by hand still makes the contact (and answers it)."""
    row = _db_execute("UPDATE friend_requests SET ignored_at = now()"
                      " WHERE id = %s AND ignored_at IS NULL RETURNING id", (request_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Request not found")
    _wake_chat_sse_clients()
    return {"ok": True}


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
        if p2p_identity.save_certificate(settings, cert):
            _certificate_stored()
        return cert
    return None


async def _born() -> bool:
    """This node's key born on the Worker — its certificate held, else asked
    for (_get_own_birth_cert): every invite route answers only a key it
    issued a certificate to, and a rotation's new key has none until then.
    A Worker too busy to answer raises its 429 (_busy), as every route's
    own call does; a caller with no person waiting catches it."""
    return await _get_own_birth_cert() is not None


async def _born_quietly() -> bool:
    """_born for a caller with no person waiting on a busy Worker: a no,
    told in the log."""
    try:
        return await _born()
    except HTTPException as e:
        logger.warning(f"Birth certificate refused for now: {e.detail}")
        return False


async def _worker_check_email(identity: dict) -> Optional[dict]:
    """GET /check-email for this identity's configured email: {verified,
    birth_cert?}. A returned certificate is the method:email upgrade — the
    Worker issues it once the verified record is bound to this pubkey."""
    import p2p_identity

    email = identity.get("email")
    if not email or not await _born():
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


def _verified_for_this_key(result: Optional[dict]) -> bool:
    """The Worker's /check-email says the address is verified AND bound to
    this node's key — the one meaning of `user_profile.email_verified`. It
    answers `verified` with no certificate where the address was verified
    under another key of the same code (a key rotated since): for this key
    that is an address not yet verified, and its row offers the verification
    that binds it."""
    return bool(result and result.get("verified") and result.get("birth_cert"))


# -- The identity worker -------------------------------------------------------
#
# One per node, in the process that serves the node's peer surface
# (settings.serves_peer_surface): a launcher-mode backend leaves it to the
# launcher's P2P manager, as it leaves the chat client. Started at the
# lifespan's start for a node with an account and the moment an account
# exists (routers/auth.create_account); restarted when the identity changes
# (device_auth._after_rotation) or its certificate does (_certificate_changed).
# Behind it, the identity folder's own lock (identity_proof.worker_lock): one
# worker per folder, across processes.

class _IdentityWorker:
    def __init__(self):
        self.stop = threading.Event()        # the mining thread's, between attempts
        self.certified = asyncio.Event()     # a certificate stored while it waits for one
        self.task: Optional[asyncio.Task] = None
        self.mining: Optional[asyncio.Future] = None


_identity_worker: Optional[_IdentityWorker] = None
_identity_worker_lock = asyncio.Lock()
_identity_worker_closed = False


async def start_identity_worker() -> None:
    """The worker for this node's identity, unless one ran or runs for it
    already: a worker done (ready) stays done until a restart."""
    async with _identity_worker_lock:
        await _start_worker()


async def close_identity_worker() -> None:
    """At the backend's stop: the worker stopped and waited for — its Worker
    requests cancelled (they publish nothing), its mining thread returning
    between two attempts (one Argon2 call at most) — and none started after,
    by a start or a restart that waited on the lock meanwhile."""
    global _identity_worker_closed
    async with _identity_worker_lock:
        _identity_worker_closed = True
        await _stop_worker()


async def restart_identity_worker() -> None:
    """For an identity or a certificate that changed under the worker: the
    old one stopped and waited for first — a proof it saved or a state it
    published after the new one's would be the old identity's."""
    async with _identity_worker_lock:
        await _stop_worker()
        await _start_worker()


async def _start_worker() -> None:
    global _identity_worker
    if _identity_worker is not None or _identity_worker_closed:
        return
    if not settings.serves_peer_surface:
        if settings.p2p_enabled:
            logger.info("identity worker: the launcher's process runs it (this backend serves "
                        "no peer surface)")
        return
    if _get_identity() is None:
        return
    worker = _IdentityWorker()

    async def run():
        try:
            await identity_proof_task(worker)
        except Exception:
            logger.exception("identity worker failed")
    worker.task = asyncio.create_task(run())
    _identity_worker = worker


async def _stop_worker() -> None:
    global _identity_worker
    worker, _identity_worker = _identity_worker, None
    if worker is None:
        return
    worker.stop.set()
    cancelled = worker.task.cancel()
    await asyncio.gather(worker.task, return_exceptions=True)
    if worker.mining is not None and cancelled:
        # Cancelled, the task does not see how its thread ends: said here
        try:
            await worker.mining
        except Exception:
            logger.exception("identity proof mining failed")


def _certificate_stored() -> None:
    """A certificate this node did not hold was stored (_get_own_birth_cert):
    a worker waiting out the Worker's absence goes on now, whoever asked —
    this backend's, or in launcher mode the launcher's, which started none
    without a certificate (NOTIFY sautium_identity_cert, p2p_manager)."""
    if _identity_worker is not None:
        _identity_worker.certified.set()
    if not settings.serves_peer_surface:
        _db_execute("NOTIFY sautium_identity_cert")


def _publish_identity_state(state: dict) -> None:
    """A worker's state to the Web UI (user_settings['p2p.identity'] + NOTIFY
    sautium_identity); a `ready` one also wakes the chat client, whose
    handshakes parked on our missing certificate or proof go out now — the
    launcher's worker sends the same wake (p2p_manager)."""
    try:
        from routers.settings import _write
        _write("p2p.identity", state)
        _db_execute("NOTIFY sautium_identity")
    except Exception as e:
        logger.warning(f"identity state not published: {e}")
    if state.get("status") == "ready":
        import p2p_chat
        client = p2p_chat.chat_client()
        if client is not None:
            client.resolve_soon()


async def _worker_certificate() -> tuple[Optional[dict], Optional[int]]:
    """(the certificate, None), or (None, the wait the Worker asked for) when
    it refused for volume (429) with none held: the worker's own Worker reads.
    A refusal of a read that only checks a certificate held for a newer one
    leaves the one held standing."""
    try:
        cert = await _get_own_birth_cert()
    except HTTPException as e:
        if e.status_code != 429:
            raise
        retry = (e.headers or {}).get("Retry-After", "")
        return None, int(retry) if retry.isdigit() else None
    if cert is not None and cert["method"] == "pow":
        # Portable identity: the email upgrade may have happened on
        # another device holding the same key — one Worker read before
        # committing minutes of mining to a possibly superseded cert.
        cert = await _unless_braked(_get_own_birth_cert(refresh=True)) or cert
    if cert is not None and cert["method"] != "email":
        row = _db_query_one("SELECT email_verified FROM user_profile WHERE id = 1")
        if row and row.get("email_verified"):
            result = await _unless_braked(_worker_check_email(_get_identity() or {}))
            if result and result.get("birth_cert"):
                cert = result["birth_cert"]
    return cert, None


async def _unless_braked(read):
    """A Worker read the worker can do without: None when the Worker asks it
    to wait (429)."""
    try:
        return await read
    except HTTPException as e:
        if e.status_code != 429:
            raise
        logger.info(f"identity worker: the Worker asks to wait — the certificate held stands ({e.detail})")
        return None


def _holds_identity() -> bool:
    """This node already holds what makes its identity known to others: a
    certificate verified by email, or a mined proof that binds to the
    certificate it holds. Read from the disk alone."""
    from desktop.p2p import identity_proof
    import p2p_identity
    cert = p2p_identity.load_certificate(settings)
    if cert is None:
        return False
    if cert["method"] != "pow":
        return True
    path = p2p_identity.proof_path(settings)
    return path is not None and identity_proof.proof_binds(identity_proof.load_proof(path), cert)


def _email_road() -> bool:
    """The road this node's identity takes, decided at every start of the
    worker and never stored (PI-NODE-2B §2.3): a node that runs no model —
    Nano — never mines. Its board may have no 2.5 GiB to give the miner, at
    its birth or ever, and the Worker's protocol already has the other way:
    a verified email re-signs the certificate, and every node knows that one
    by its signature. A node that already holds its identity (a proof mined
    on a bigger machine, a certificate verified elsewhere) keeps it."""
    import hardware_profile
    return not hardware_profile.resolve().ml_available and not _holds_identity()


async def _verified_email_certificate() -> bool:
    """On the email road with the account's address recorded as verified and
    no certificate of it on this disk (a restore; a verification on another
    device of this key): one read of the Worker's /check-email, which stores
    the certificate it returns — the read the proof road makes
    (_worker_certificate). True when the node then holds its identity. Where
    the Worker answers and has no certificate for this key, the record is
    not this key's: it is cleared, so the Email row offers the verification
    again instead of saying "verified" beside an identity that waits for it."""
    row = _db_query_one("SELECT email_verified FROM user_profile WHERE id = 1")
    if not (row and row.get("email_verified")):
        return False
    result = await _unless_braked(_worker_check_email(_get_identity() or {}))
    if await asyncio.to_thread(_holds_identity):
        return True
    if result is not None:
        _record_email_verified(False)
        logger.info("identity: the address was recorded as verified, and the Worker holds no "
                    "certificate of it for this key — the verification is asked for again")
    return False


async def identity_proof_task(worker: _IdentityWorker) -> None:
    """The worker's body: make sure this node holds its identity certificate
    and, for method:pow, a verified proof (desktop/p2p/identity_proof.py — the
    same policy the launcher runs), publishing every state
    (_publish_identity_state). A node whose email is verified but whose
    certificate predates v2 gets the method:email certificate from
    /check-email here — no work needed. A node on the email road
    (_email_road) mines nothing, and asks the Worker only where its address
    is already recorded as verified (_verified_email_certificate: one
    /check-email read); otherwise it says `needs_email`, and the
    verification's own event starts the worker again (_certificate_changed)."""
    from desktop.p2p import identity_proof
    import p2p_identity

    if await asyncio.to_thread(_email_road) and not await _verified_email_certificate():
        from datetime import datetime, timezone
        _publish_identity_state({"status": "needs_email", "detail": None, "method": None,
                                 "updated_at": datetime.now(timezone.utc).isoformat()})
        logger.info("identity: this node runs no model and does not mine — its identity "
                    "waits for a verified email")
        return

    backoff = 300
    while True:
        worker.certified.clear()
        cert, wait = await _worker_certificate()
        if cert is None:
            # Worker unreachable, not configured or braking: the certificate
            # is a network fact, nothing local can replace it — wait for the
            # Worker's own word, the backoff, or a certificate a route stored
            wait = max(wait or 0, backoff)
            logger.warning(f"identity certificate unavailable — retry in {wait}s")
            try:
                await asyncio.wait_for(worker.certified.wait(), wait)
            except asyncio.TimeoutError:
                backoff = min(backoff * 2, 3600)
            continue
        path = p2p_identity.proof_path(settings)
        if path is None:
            logger.warning("p2p_identity_dir unset — identity proof cannot be stored")
            return
        from desktop.p2p import load_meter
        meter = load_meter.current()
        # Shielded: a stop cancels this task, never the thread — which it
        # waits for instead (_stop_worker)
        worker.mining = asyncio.ensure_future(asyncio.to_thread(
            identity_proof.ensure_identity_proof, cert, path, stop=worker.stop,
            on_state=_publish_identity_state,
            hold=meter.mining_hold if meter else (lambda: None)))
        await asyncio.shield(worker.mining)
        return


def _certificate_changed() -> None:
    """This node's certificate changed under its worker — an email verified
    here (verify-code) or found verified on another device of the same key
    (/check-email, _check_email_upgrade): the worker starts again on it, and
    an `email` certificate is `ready` at once — the publish and the wake
    every state takes. A launcher's own worker is told by NOTIFY
    sautium_identity_cert (p2p_manager); this backend owns none there."""
    import p2p_chat
    p2p_chat.soon(restart_identity_worker())
    _db_execute("NOTIFY sautium_identity_cert")


async def _check_email_upgrade(identity: dict) -> Optional[dict]:
    """_worker_check_email for a route: a certificate it stored that this
    node did not hold is the certificate's change."""
    import p2p_identity
    held = p2p_identity.load_certificate(settings)
    result = await _worker_check_email(identity)
    if result and result.get("birth_cert") and p2p_identity.load_certificate(settings) != held:
        _certificate_changed()
    return result


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

    result = await _check_email_upgrade(identity)
    verified = _verified_for_this_key(result)
    if verified:
        _record_email_verified(True)
    return {"email": email, "verified": verified}


@router.post("/email/send-code")
async def email_send_code(opened=boxed(SendCodeRequest)) -> Dict[str, Any]:
    """Ask the Worker to generate and email a verification code — on the
    grant the address's own setting handed this browser, or the account's
    password (the Verify row of an address set earlier); the sealed answer
    carries a grant for the code's own lifetime.

    The code exists only on the Worker (hash, 15 min TTL) — mailbox
    ownership is proven server-side at /email/verify-code."""
    req, reply = opened
    identity = _get_identity()
    if not identity or not identity.get("email"):
        raise HTTPException(400, "No email configured (set P2P_EMAIL in .env)")
    if not device_auth.grant_holds("email", req.grant):
        await step_up(req)

    if not await _born():
        raise HTTPException(502, "Birth certificate unavailable")
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

    return reply({"status": "sent", "email": email,
                  "grant": device_auth.grant("email", EMAIL_FLOW_SECONDS)})


@router.post("/email/verify-code")
async def email_verify_code(opened=boxed(VerifyCodeRequest)) -> Dict[str, Any]:
    """Register the email on the Worker: it checks the entered code against
    its stored hash and requires this node's birth certificate (the verified
    record then carries born_at). The code rides the box, with the grant its
    send handed this browser; a verified address spends the grant."""
    req, reply = opened
    if not device_auth.grant_holds("email", req.grant):
        raise HTTPException(status_code=403, detail="Send the code again from here: this one was not "
                                                    "asked for by this page, or its time is up",
                            headers={"X-Sautium-Auth-Error": "grant"})
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
        return reply({"verified": False, "error": "Invalid or expired code"})

    changed = False
    if result.get("birth_cert") and result["birth_cert"] != cert:
        import p2p_identity
        changed = p2p_identity.save_certificate(settings, result["birth_cert"])

    # Persist so future /email/status checks return verified without a
    # Worker round-trip. Email-change and password-change flows (when
    # they land) must clear this flag.
    _record_email_verified(True)
    device_auth.drop_grant("email")
    if changed:
        _certificate_changed()
    return reply({"verified": True, "email": email})


@router.post("/chat/wake")
async def chat_wake() -> Dict[str, str]:
    """Wake SSE clients immediately (called by sync server on incoming message)."""
    _wake_chat_sse_clients()
    return {"ok": "true"}


@router.get("/chat/unread")
async def chat_unread() -> Dict[str, int]:
    """Unread incoming messages across every thread — what the Friends tab
    badge paints. One count over idx_p2p_messages_unread, so a repaint on
    every chat event costs nothing and needs no per-friend fan-out."""
    row = _db_query_one("SELECT count(*) AS c FROM p2p_messages"
                        " WHERE direction = 'in' AND read = FALSE")
    return {"unread": int(row["c"]) if row else 0}
