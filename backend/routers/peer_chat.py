"""Peer-facing chat + relay endpoints for the Docker peer surface (p2p_app).

MIRRORS the chat handlers in desktop/p2p/sync_server.py — keep the wire
contracts in step. The /api/relay/* namespace is the proxy-agnostic relay
protocol: today this node (the master) is relay #0; Phase D lets any
reachable peer serve the identical contract.

Auth model per endpoint:
- handshake: invite-code<->pubkey binding always; the token/grant paths add
  a MANDATORY timestamp-bound guest signature, because they bypass the
  mutual-add consent check and must prove key possession instead.
- message:  sender must be a known, non-blocked friend with can_message.
- history:  timestamp-bound signature + friend + can_message.
- wake-stream / probe-connect: timestamp-bound signature + friend.
Timestamps are unix seconds within ±TS_WINDOW (mirrors the HMAC replay
window on 8800). This router runs behind p2p_app's per-IP rate limiter.
"""

import asyncio
import hashlib
import json
import logging
import threading
import time
from collections import deque
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

import invite_tokens
from config import settings
from db_pool import db_query_one, get_conn
from p2p_chat import MAX_ENCRYPTED_CHARS, get_peer_chat
from p2p_identity import load_signing_key, resolve_identity, verify_invite_code

logger = logging.getLogger(__name__)

chat_router = APIRouter(prefix="/api/chat", tags=["peer-chat"])
relay_router = APIRouter(prefix="/api/relay", tags=["peer-relay"])

TS_WINDOW = 60                 # seconds, mirrors auth_hmac's replay window
TOKEN_FRIENDS_PER_HOUR = 30    # soft cap on token auto-accepts
WAKE_MAX_PER_IP = 20
PROBE_COOLDOWN = 60            # seconds per pubkey
# Forwarding: how long a sender's request waits for the recipient's signed
# receipt, how many envelopes may queue for one recipient, and how many
# forwards one sender may have in flight. The queue is the ONLY resource a
# relay spends per client — it stores nothing.
FORWARD_ACK_TIMEOUT = 10
FORWARD_QUEUE_MAX = 100
FORWARD_INFLIGHT_PER_SENDER = 10
# Peer-relay caps (Phase D) — mirror desktop/p2p/sync_server.py, where the
# adaptive-cap rationale lives.
RELAY_CAP_BASE = 20
RELAY_CAP_MAX = 100
RELAY_CAP_STEP = 20
VOUCHER_TTL = 24 * 3600


def voucher_payload(client_pubkey: str, relay_pubkey: str, until: int) -> str:
    """Canonical relay-voucher string the CLIENT signs — mirrored in
    desktop/p2p/sync_server.py. The relay pubkey inside the payload means a
    voucher issued to one relay cannot be presented by another."""
    return (f"sautium-relay-voucher:v1:{client_pubkey.lower()}"
            f":{relay_pubkey.lower()}:{int(until)}")


def _err(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


_gate = None


def _identity_gate():
    """Lazy: the asyncio semaphore inside is born on the serving loop.
    Note: behind the Docker bridge every peer shares one client address, so
    the per-address failure backstop acts as a global one here — bounded
    verifier spend on forged proofs, at the price of a shared bucket."""
    global _gate
    if _gate is None:
        import birth_authority
        from desktop.p2p import identity_registry
        _gate = identity_registry.IdentityGate(get_conn, birth_authority.verify_certificate)
    return _gate


def _ts_ok(ts) -> bool:
    try:
        return abs(time.time() - int(ts)) <= TS_WINDOW
    except (TypeError, ValueError):
        return False


def _verify_ed25519(pubkey_hex: str, message: str, sig_hex: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex)).verify(
            bytes.fromhex(sig_hex), message.encode("utf-8"))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def _find_friend_for_handshake(invite_code: str, pubkey: str) -> Optional[dict]:
    """Match by real pubkey, rotated-away pubkey OR invite code — the last
    catches `pending:` stubs this node's operator pre-added manually."""
    return db_query_one("""
        SELECT id, username, public_key_hex, invite_code, is_blocked,
               source::text AS source
          FROM friends
         WHERE public_key_hex = %s OR previous_public_key_hex = %s
            OR invite_code = %s
    """, (pubkey, pubkey, invite_code))


# ---------------------------------------------------------------------------
# Wake registry — who holds a live wake stream to this relay
# ---------------------------------------------------------------------------

class _WakeSub:
    __slots__ = ("evt", "loop", "kinds", "envelopes", "frames", "closed", "ip")

    def __init__(self, evt, loop, ip):
        self.evt = evt
        self.loop = loop
        self.kinds: set = set()
        # Envelopes to push down this stream. A queue, not a set: each one
        # is a distinct message, and order is the sender's.
        self.envelopes: deque = deque()
        # Typed frames beyond deliver/wake — today the support warrant
        # (routers/peer_diag). Queued by push_frame, sent first by gen().
        self.frames: deque = deque()
        self.closed = False
        self.ip = ip


_wake_subs: dict = {}          # subscriber pubkey -> _WakeSub
_wake_lock = threading.Lock()

# Peer-relay clients (Phase D): pubkey -> {invite_code, until, signature}.
# In-memory on purpose — a relay restart drops the registry and every
# client re-issues its voucher on reconnect. The announce hooks are wired
# by main.py to DHTService.announce_user_for / withdraw_user_for.
_relay_clients: dict = {}
_relay_cap = RELAY_CAP_BASE
_client_announce_cb = None
_client_withdraw_cb = None


def set_client_announce_cbs(announce, withdraw) -> None:
    global _client_announce_cb, _client_withdraw_cb
    _client_announce_cb = announce
    _client_withdraw_cb = withdraw


def relay_client_count() -> int:
    with _wake_lock:
        return sum(1 for k in _wake_subs if k in _relay_clients)


def relay_has_room() -> bool:
    return relay_client_count() < _relay_cap


def adapt_relay_cap(other_relays_visible: bool) -> bool:
    """One adaptive-cap step — mirror of SyncServer.adapt_relay_cap, see the
    rationale there. Returns True when the cap changed."""
    global _relay_cap
    clients = relay_client_count()
    if clients >= _relay_cap and not other_relays_visible \
            and _relay_cap < RELAY_CAP_MAX:
        _relay_cap = min(_relay_cap + RELAY_CAP_STEP, RELAY_CAP_MAX)
        logger.info("relay cap raised to %d (no other relays visible)",
                    _relay_cap)
        return True
    if clients < RELAY_CAP_BASE and other_relays_visible \
            and _relay_cap > RELAY_CAP_BASE:
        _relay_cap = max(RELAY_CAP_BASE, clients)
        logger.info("relay cap decayed to %d", _relay_cap)
        return True
    return False


def ping_wake(pubkey: str, kind: str = "message") -> None:
    """Thread-safe: signal one subscriber's stream. No-op when offline —
    they catch up with the connect-time history sync."""
    with _wake_lock:
        sub = _wake_subs.get(pubkey)
        if sub is None:
            return
        sub.kinds.add(kind)
        sub.loop.call_soon_threadsafe(sub.evt.set)


def ping_wake_by_friend_id(friend_id: int, kind: str = "message") -> None:
    """For the LISTEN thread: resolve the friend's pubkey only when someone
    is actually subscribed."""
    with _wake_lock:
        if not _wake_subs:
            return
    row = db_query_one(
        "SELECT public_key_hex FROM friends WHERE id = %s", (friend_id,))
    if row:
        ping_wake(row["public_key_hex"], kind)


def push_frame(pubkey: str, frame: dict) -> bool:
    """Thread-safe: queue one typed frame for a subscriber's stream. False
    when nobody with that key holds a stream right now — the caller keeps
    the payload and pushes again when the node subscribes
    (set_subscribe_hook)."""
    with _wake_lock:
        sub = _wake_subs.get(pubkey)
        if sub is None or sub.closed:
            return False
        sub.frames.append(frame)
        sub.loop.call_soon_threadsafe(sub.evt.set)
    return True


_subscribe_hook = None


def set_subscribe_hook(cb) -> None:
    """`await cb(pubkey)` runs on the serving loop right after a wake
    stream is registered — the moment payloads parked for that node can
    be pushed (routers/peer_diag.on_wake_subscribed)."""
    global _subscribe_hook
    _subscribe_hook = cb


def _register_wake(pubkey: str, ip: str) -> Optional[_WakeSub]:
    """A new subscription supersedes the old one for the same pubkey
    (cap 1/pubkey); per-IP subscriptions are capped."""
    sub = _WakeSub(asyncio.Event(), asyncio.get_running_loop(), ip)
    with _wake_lock:
        ip_count = sum(1 for s in _wake_subs.values() if s.ip == ip)
        old = _wake_subs.get(pubkey)
        if old is None and ip_count >= WAKE_MAX_PER_IP:
            return None
        if old is not None:
            old.closed = True
            old.loop.call_soon_threadsafe(old.evt.set)
        _wake_subs[pubkey] = sub
    return sub


def _unregister_wake(pubkey: str, sub: _WakeSub) -> None:
    with _wake_lock:
        if _wake_subs.get(pubkey) is sub:
            del _wake_subs[pubkey]


# ---------------------------------------------------------------------------
# Handshake (classic + token + grant-recovery paths)
# ---------------------------------------------------------------------------

@chat_router.post("/handshake")
async def chat_handshake(request: Request):
    svc = get_peer_chat()
    ident = resolve_identity(settings)
    if svc is None or not ident:
        return _err("no account configured", 503)

    try:
        body = await request.json()
        peer_pubkey = body.get("public_key_hex", "")
        peer_username = body.get("username", "")
        peer_invite = body.get("invite_code", "")
    except Exception:
        return _err("invalid JSON", 400)
    if not peer_pubkey or not peer_invite:
        return _err("missing public_key_hex or invite_code", 400)
    if not verify_invite_code(peer_invite, peer_pubkey):
        return _err("invite code mismatch", 403)

    token_id = body.get("token_id") or None
    grant_in = body.get("grant") or None

    # Token/grant paths bypass mutual-add — require a fresh guest signature.
    if token_id or grant_in:
        ts = body.get("ts")
        if not _ts_ok(ts):
            return _err("stale timestamp", 403)
        label = "token_handshake" if token_id else "grant_handshake"
        tid = token_id or (grant_in or {}).get("token_id", "")
        signed = f"{label}:{int(ts)}:{tid}:{ident['invite_code']}"
        if not _verify_ed25519(peer_pubkey, signed, body.get("signature", "")):
            return _err("invalid signature", 403)

    match = _find_friend_for_handshake(peer_invite, peer_pubkey)
    if match and match["is_blocked"]:
        return _err("blocked", 403)
    resolved = (match if match
                and match["public_key_hex"] == peer_pubkey else None)

    def _accept(grant_out=None):
        payload = {
            "accepted": True,
            "public_key_hex": ident["public_key_hex"],
            "username": ident["username"],
            "invite_code": ident["invite_code"],
        }
        if grant_out:
            payload["grant"] = grant_out
        return JSONResponse(payload)

    def _mint(tid: str, rights: list):
        return invite_tokens.sign_grant(
            load_signing_key(settings), tid, rights, peer_pubkey,
            ident["public_key_hex"])

    # -- idempotent re-accept: friendship already established ---------------
    if resolved is not None and (token_id or grant_in):
        tid = token_id or grant_in["token_id"]
        with get_conn() as conn:
            rights = invite_tokens.friend_rights(conn, resolved["id"])
            if not rights and resolved["source"] == "token":
                # First accept was interrupted between add_friend and the
                # snapshot — the guest's 15s retry loop lands here to heal
                # it. The use was already burned, so read rights directly.
                rights = (invite_tokens.token_rights(conn, token_id)
                          if token_id else grant_in.get("rights", []))
                invite_tokens.snapshot_rights(conn, resolved["id"], rights)
        svc.update_friend_last_seen(peer_pubkey)
        return _accept(_mint(tid, rights))

    if token_id:
        with get_conn() as conn:
            requires_cert = invite_tokens.token_requires_cert(conn, token_id)
        if requires_cert is None:
            return _err("token invalid", 403)
        if requires_cert:
            # Identity gate BEFORE the use is burned (mirror of sync_server):
            # certificate v2 + the mined proof for method:pow, verified once
            # and cached in p2p_identities; "busy" → 503 with Retry-After.
            admission = await _identity_gate().admit(
                peer_pubkey, body.get("birth_cert") or {},
                body.get("identity_proof"),
                request.client.host if request.client else None)
            if admission.status != "verified":
                headers = ({"Retry-After": str(admission.retry_after)}
                           if admission.retry_after else None)
                return JSONResponse({"error": admission.detail},
                                    status_code=admission.http_status,
                                    headers=headers)
        with get_conn() as conn:
            tok = invite_tokens.consume_token(conn, token_id)
        if tok is None:
            return _err("token invalid", 403)
        recent = db_query_one(
            "SELECT count(*) AS c FROM friends"
            " WHERE source = 'token' AND added_at > NOW() - INTERVAL '1 hour'")
        if recent and recent["c"] >= TOKEN_FRIENDS_PER_HOUR:
            return _err("token accept rate exceeded", 429)

        fid = svc.add_friend(peer_pubkey, peer_invite, peer_username,
                             source="token", source_token_id=token_id)
        with get_conn() as conn:
            invite_tokens.snapshot_rights(conn, fid, tok["rights"])
        if tok["welcome_message"]:
            svc.store_message(fid, "out", tok["welcome_message"],
                              delivered=True)
        svc.update_friend_last_seen(peer_pubkey)
        logger.info("Token handshake accepted from %s (%s...)",
                    peer_username, peer_pubkey[:16])
        return _accept(_mint(token_id, tok["rights"]))

    if grant_in:
        # Recovery: our own signature over their grant replaces the friends
        # row this device never had (or lost with the previous device).
        if not invite_tokens.verify_grant(grant_in, ident["public_key_hex"]):
            return _err("invalid grant", 403)
        if grant_in.get("guest_pubkey", "").lower() != peer_pubkey.lower():
            return _err("grant subject mismatch", 403)
        with get_conn() as conn:
            revoked = invite_tokens.token_revoked(conn, grant_in["token_id"])
        if revoked is True:
            return _err("token revoked", 403)
        # FK-safe: reference the token row only when it exists locally.
        src_tid = grant_in["token_id"] if revoked is not None else None
        fid = svc.add_friend(peer_pubkey, peer_invite, peer_username,
                             source="token", source_token_id=src_tid)
        with get_conn() as conn:
            invite_tokens.snapshot_rights(conn, fid,
                                          grant_in.get("rights", []))
        svc.update_friend_last_seen(peer_pubkey)
        logger.info("Grant recovery accepted from %s (%s...)",
                    peer_username, peer_pubkey[:16])
        return _accept(_mint(grant_in["token_id"],
                             grant_in.get("rights", [])))

    # -- classic path: mutual-add consent (mirror of sync_server) -----------
    if match is None:
        logger.info("Handshake rejected from %s (%s...) — not in our"
                    " friends list", peer_username, peer_pubkey[:16])
        return JSONResponse(
            {"accepted": False, "error": "not in friends list"},
            status_code=403)
    svc.add_friend(peer_pubkey, peer_invite, peer_username)
    svc.update_friend_last_seen(peer_pubkey)
    logger.info("Handshake accepted from %s (%s...)",
                peer_username, peer_pubkey[:16])
    return _accept()


# ---------------------------------------------------------------------------
# Message + history (guest-initiated; this node never pushes outbound)
# ---------------------------------------------------------------------------

@chat_router.post("/message")
async def chat_message(request: Request):
    svc = get_peer_chat()
    if svc is None:
        return _err("chat not available", 503)
    try:
        body = await request.json()
        sender_pubkey = body.get("from_public_key", "")
        encrypted = body.get("encrypted", "")
        timestamp = body.get("timestamp", "")
        message_uuid = body.get("message_uuid", "")
    except Exception:
        return _err("invalid JSON", 400)
    if not sender_pubkey or not encrypted or not timestamp:
        return _err("missing fields", 400)
    if len(encrypted) > MAX_ENCRYPTED_CHARS:
        return _err("message too large", 413)

    with get_conn() as conn:
        if not invite_tokens.friend_has_right(conn, sender_pubkey,
                                              "can_message"):
            return _err("not permitted", 403)

    result = svc.handle_incoming(sender_pubkey, encrypted, timestamp,
                                 message_uuid=message_uuid or None)
    if result is None:
        return _err("rejected", 403)
    # The insert trigger NOTIFYs sautium_chat → the 8800 UI SSE wakes.
    return JSONResponse({"status": "delivered"})


def mailbox_import(m: dict) -> None:
    """One message drained from the master's Worker mailbox (Ф16) — the same
    checks as /message above, minus the transport: friendship right, size,
    handle_incoming (dedup by message_uuid, decrypt, store → the insert
    trigger wakes the UI). A refused message is dropped, not retried: the
    mailbox is a parking lot for the direct path, not a second protocol."""
    svc = get_peer_chat()
    if svc is None:
        raise RuntimeError("chat not available")          # skips the ack → served again later
    sender_pubkey = m.get("from_public_key", "")
    encrypted = m.get("encrypted", "")
    if len(encrypted) > MAX_ENCRYPTED_CHARS:
        logger.warning("mailbox: oversized message from %s dropped", sender_pubkey[:8])
        return
    with get_conn() as conn:
        if not invite_tokens.friend_has_right(conn, sender_pubkey, "can_message"):
            logger.info("mailbox: message from %s without can_message dropped", sender_pubkey[:8])
            return
    result = svc.handle_incoming(sender_pubkey, encrypted, m.get("timestamp", ""),
                                 message_uuid=m.get("message_uuid") or None)
    if result is None:
        logger.info("mailbox: message %s from %s rejected", (m.get("message_uuid") or "")[:8], sender_pubkey[:8])


@chat_router.post("/history")
async def chat_history(request: Request):
    svc = get_peer_chat()
    if svc is None:
        return _err("chat not available", 503)
    try:
        body = await request.json()
        requester_pubkey = body.get("public_key_hex", "")
        since_iso = body.get("since")
        signature_hex = body.get("signature", "")
        nonce = body.get("nonce", "")
        ts = body.get("ts")
    except Exception:
        return _err("invalid JSON", 400)
    if not requester_pubkey:
        return _err("missing public_key_hex", 400)
    if not _ts_ok(ts):
        return _err("stale timestamp", 403)
    if not _verify_ed25519(requester_pubkey,
                           f"history_request:{int(ts)}:{nonce}",
                           signature_hex):
        return _err("invalid signature", 403)

    friend = svc.get_friend_by_public_key(requester_pubkey)
    if not friend:
        return _err("not a friend", 403)
    if friend.get("is_blocked"):
        return _err("blocked", 403)
    with get_conn() as conn:
        if not invite_tokens.friend_has_right(conn, requester_pubkey,
                                              "can_message"):
            return _err("not permitted", 403)

    since = None
    if since_iso:
        try:
            since = datetime.fromisoformat(since_iso)
        except (ValueError, TypeError):
            pass

    messages = svc.get_history_for_export(friend["id"], since=since)
    result_messages = []
    max_out_id = 0
    for msg in messages:
        if msg["direction"] == "out":
            max_out_id = max(max_out_id, msg["id"])
        ts_val = msg["timestamp"]
        ts_str = (ts_val.isoformat() if hasattr(ts_val, "isoformat")
                  else str(ts_val))
        if "+" not in ts_str and not ts_str.endswith("Z"):
            ts_str += "+00:00"
        result_messages.append({
            # NULL-safe: str(None) would ship the literal string "None"
            # and crash the importer's uuid lookup.
            "message_uuid": (str(msg["message_uuid"])
                             if msg["message_uuid"] else None),
            "direction": msg["direction"],
            "encrypted": svc.encrypt_message(msg["content"],
                                             requester_pubkey),
            "timestamp": ts_str,
        })
    if max_out_id:
        # The requester proved key ownership and has the export now —
        # export IS delivery (this node has no push loop).
        svc.mark_exported_delivered(friend["id"], max_out_id)

    logger.info("History export for %s: %d messages",
                friend.get("username", "?"), len(result_messages))
    return JSONResponse({"messages": result_messages})


# ---------------------------------------------------------------------------
# Relay protocol: wake stream + reachability probe
# ---------------------------------------------------------------------------

@relay_router.get("/wake-stream")
async def wake_stream(request: Request, pubkey: str = "", ts: str = "",
                      sig: str = "", invite: str = "",
                      voucher_until: str = "", voucher_sig: str = ""):
    svc = get_peer_chat()
    ident = resolve_identity(settings)
    if svc is None or not ident:
        return _err("relay not available", 503)
    if not _ts_ok(ts):
        return _err("stale timestamp", 403)
    signed = f"wake_subscribe:{int(ts)}:{ident['public_key_hex']}:{pubkey}"
    if not _verify_ed25519(pubkey, signed, sig):
        return _err("invalid signature", 403)

    # Admission (Phase D). A presented voucher is ALWAYS processed —
    # friendship only waives its NECESSITY (master-path legacy): a
    # friend-client that wants to be announced still sends one. Gating on
    # friendship first silently dropped exactly that — two friendly
    # launchers could never relay for each other. Mirror of
    # desktop/p2p/sync_server.py.
    friend = svc.get_friend_by_public_key(pubkey)
    if friend and friend.get("is_blocked"):
        return _err("not a friend", 403)
    voucher = None
    if invite and voucher_sig:
        from p2p_identity import verify_invite_code
        try:
            v_until = int(voucher_until)
        except ValueError:
            v_until = 0
        if v_until <= int(time.time()):
            return _err("voucher required", 403)
        if not verify_invite_code(invite, pubkey):
            return _err("invite does not match key", 403)
        if not _verify_ed25519(
                pubkey,
                voucher_payload(pubkey, ident["public_key_hex"], v_until),
                voucher_sig):
            return _err("invalid voucher", 403)
        if pubkey not in _relay_clients and not relay_has_room():
            return _err("relay full", 429)
        voucher = {"invite_code": invite, "until": v_until,
                   "signature": voucher_sig}
    elif not friend:
        return _err("voucher required", 403)

    ip = request.client.host if request.client else "unknown"
    sub = _register_wake(pubkey, ip)
    if sub is None:
        return _err("too many subscriptions", 429)
    is_client = voucher is not None
    bump_presence = friend is not None
    if is_client:
        with _wake_lock:
            _relay_clients[pubkey] = voucher
        if _client_announce_cb:
            try:
                _client_announce_cb(voucher["invite_code"])
            except Exception as e:
                logger.warning("client announce failed: %s", e)

    async def gen():
        try:
            yield ": connected\n\n"
            if bump_presence:
                svc.update_friend_last_seen(pubkey)
            if _subscribe_hook is not None:
                try:
                    await _subscribe_hook(pubkey)
                except Exception as e:
                    logger.warning("subscribe hook failed for %s…: %s", pubkey[:8], e)
            cycles = 0
            while not sub.closed:
                try:
                    await asyncio.wait_for(sub.evt.wait(), timeout=15.0)
                    sub.evt.clear()
                    if sub.closed:
                        break
                    with _wake_lock:
                        kinds, sub.kinds = sub.kinds, set()
                        envelopes = list(sub.envelopes)
                        sub.envelopes.clear()
                        frames = list(sub.frames)
                        sub.frames.clear()
                    # Typed frames first (an instruction), then envelopes
                    # (the payload), then wakes (only a hint to go looking).
                    for frame in frames:
                        yield "data: %s\n\n" % json.dumps(frame, ensure_ascii=False)
                    for envelope in envelopes:
                        yield "data: %s\n\n" % json.dumps(
                            {"type": "deliver", "envelope": envelope},
                            ensure_ascii=False)
                    for kind in sorted(kinds):
                        yield ('data: {"type": "wake", "kind": "%s"}\n\n'
                               % kind)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                cycles += 1
                if cycles >= 8:          # ~2 min — passive presence bump
                    if bump_presence:
                        svc.update_friend_last_seen(pubkey)
                    cycles = 0
        finally:
            with _wake_lock:
                still_ours = _wake_subs.get(pubkey) is sub
            _unregister_wake(pubkey, sub)
            # Announce lives exactly as long as the subscription; only when
            # THIS sub is the registered one — a re-subscribe supersession
            # must not withdraw the successor's announce.
            if is_client and still_ours:
                with _wake_lock:
                    rec = _relay_clients.pop(pubkey, None)
                if rec and _client_withdraw_cb:
                    try:
                        _client_withdraw_cb(rec["invite_code"])
                    except Exception as e:
                        logger.warning("client withdraw failed: %s", e)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })


@relay_router.get("/voucher")
async def relay_voucher(invite: str = ""):
    """Prove our authority to relay for a client — mirrors
    desktop/p2p/sync_server.handle_relay_voucher. A black-hole impostor
    announcing someone else's invite has nothing to answer with here."""
    ident = resolve_identity(settings)
    with _wake_lock:
        for pubkey, rec in _relay_clients.items():
            if rec["invite_code"] == invite:
                return {
                    "client_pubkey": pubkey,
                    "invite_code": rec["invite_code"],
                    "relay_pubkey": (ident or {}).get("public_key_hex", ""),
                    "until": rec["until"],
                    "signature": rec["signature"],
                }
    return _err("no such client", 404)


_probe_last: dict = {}         # pubkey -> monotonic seconds
_probe_lock = threading.Lock()


@relay_router.post("/probe-connect")
async def probe_connect(request: Request):
    """Connectability check, BT-tracker style: connect BACK to the request's
    source address (never a caller-supplied IP — no reflector) on the port
    the caller claims to serve, and confirm the /health identity matches."""
    svc = get_peer_chat()
    ident = resolve_identity(settings)
    if svc is None or not ident:
        return _err("relay not available", 503)
    try:
        body = await request.json()
        pubkey = body.get("public_key_hex", "")
        port = int(body.get("port", 0))
        ts = body.get("ts")
        sig = body.get("signature", "")
    except Exception:
        return _err("invalid JSON", 400)
    if not pubkey or not (0 < port < 65536):
        return _err("missing fields", 400)
    if not _ts_ok(ts):
        return _err("stale timestamp", 403)
    signed = f"probe_request:{int(ts)}:{ident['public_key_hex']}:{port}"
    if not _verify_ed25519(pubkey, signed, sig):
        return _err("invalid signature", 403)
    friend = svc.get_friend_by_public_key(pubkey)
    if not friend or friend.get("is_blocked"):
        return _err("not a friend", 403)

    now = time.monotonic()
    with _probe_lock:
        last = _probe_last.get(pubkey, 0)
        if now - last < PROBE_COOLDOWN:
            return _err("probe cooldown", 429)
        _probe_last[pubkey] = now
        if len(_probe_last) > 10_000:     # bound the table under a flood
            for k in [k for k, v in _probe_last.items()
                      if now - v > PROBE_COOLDOWN]:
                _probe_last.pop(k, None)

    source_ip = request.client.host if request.client else ""
    host = f"[{source_ip}]" if ":" in source_ip else source_ip
    reachable, error = False, None
    try:
        import httpx
        async with httpx.AsyncClient(verify=False, timeout=3.0) as client:
            resp = await client.get(f"https://{host}:{port}/health")
            reachable = (resp.status_code == 200
                         and resp.json().get("node_id") == pubkey)
            if not reachable:
                error = "identity mismatch"
    except Exception as e:
        error = type(e).__name__

    payload = {"reachable": reachable, "observed_ip": source_ip,
               "tested_port": port}
    if error and not reachable:
        payload["error"] = error
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# Relay protocol: forwarding
#
# The relay is a pure forwarder — it stores NOTHING. A sender that cannot
# reach the recipient directly hands us the E2E envelope; we push it down
# the recipient's already-open wake stream and hold the sender's request
# until the recipient signs a receipt. No ack, no delivery: the message
# stays queued at the sender and is retried.
#
# The receipt is the recipient's Ed25519 signature over the message uuid
# and the ciphertext hash, so the SENDER verifies delivery — a relay cannot
# forge it, and a relay that fabricates an envelope gets no receipt because
# the forgery will not decrypt.
# ---------------------------------------------------------------------------

def delivery_payload(message_uuid: str, ciphertext_sha256: str) -> str:
    """Canonical receipt string — mirrored in desktop/p2p/sync_server.py and
    signed/verified by the two endpoints below. No timestamp: a receipt is a
    permanent fact the sender must be able to re-verify from what it has."""
    return f"sautium-delivery:v1:{message_uuid}:{ciphertext_sha256}"


_pending_acks: dict = {}       # message_uuid -> (recipient_pubkey, Future)
_ack_lock = threading.Lock()


@relay_router.post("/forward")
async def relay_forward(request: Request):
    """Forward one envelope to a connected recipient and return their receipt."""
    svc = get_peer_chat()
    ident = resolve_identity(settings)
    if svc is None or not ident:
        return _err("relay not available", 503)
    try:
        body = await request.json()
        sender = body.get("public_key_hex", "")
        recipient = body.get("to_public_key", "")
        envelope = body.get("envelope") or {}
        ts = body.get("ts")
        sig = body.get("signature", "")
        message_uuid = envelope.get("message_uuid", "")
        encrypted = envelope.get("encrypted", "")
    except Exception:
        return _err("invalid JSON", 400)
    if not (sender and recipient and message_uuid and encrypted
            and envelope.get("timestamp")):
        return _err("missing fields", 400)
    if len(encrypted) > MAX_ENCRYPTED_CHARS:
        return _err("message too large", 413)
    if not _ts_ok(ts):
        return _err("stale timestamp", 403)
    ct_hash = hashlib.sha256(encrypted.encode("utf-8")).hexdigest()
    signed = (f"relay_forward:{int(ts)}:{ident['public_key_hex']}"
              f":{message_uuid}:{ct_hash}")
    if not _verify_ed25519(sender, signed, sig):
        return _err("invalid signature", 403)
    # Admission (Phase D): a friend may forward to anyone subscribed here;
    # a STRANGER may forward only to a voucher-registered client — that is
    # the point of being someone's relay, and the client's E2E decrypt is
    # what actually rejects mail from non-friends.
    friend = svc.get_friend_by_public_key(sender)
    if friend and friend.get("is_blocked"):
        return _err("not a friend", 403)
    if not friend:
        with _wake_lock:
            registered = recipient in _relay_clients
        if not registered:
            return _err("not a friend", 403)
    # The relay stamps the sender from the signature — a forwarded envelope
    # must never claim an authorship the signature does not back.
    envelope = dict(envelope, from_public_key=sender)

    loop = asyncio.get_running_loop()
    future = loop.create_future()
    with _wake_lock:
        sub = _wake_subs.get(recipient)
        if sub is None:
            # Tell the sender immediately rather than burning the timeout:
            # "not connected" is an answer, and it keeps the message queued.
            return _err("recipient not connected", 409)
        if len(sub.envelopes) >= FORWARD_QUEUE_MAX:
            return _err("recipient busy", 429)
        with _ack_lock:
            inflight = sum(1 for r, _ in _pending_acks.values() if r == sender)
            if inflight >= FORWARD_INFLIGHT_PER_SENDER:
                return _err("too many forwards in flight", 429)
            _pending_acks[message_uuid] = (recipient, future)
        sub.envelopes.append(envelope)
        sub.loop.call_soon_threadsafe(sub.evt.set)

    try:
        ack = await asyncio.wait_for(future, timeout=FORWARD_ACK_TIMEOUT)
    except asyncio.TimeoutError:
        return JSONResponse({"delivered": False, "reason": "no ack"})
    finally:
        with _ack_lock:
            if _pending_acks.get(message_uuid, (None, None))[1] is future:
                del _pending_acks[message_uuid]

    logger.info("Relayed %s… from %s… to %s…", message_uuid[:8],
                sender[:8], recipient[:8])
    return JSONResponse({"delivered": True, "ack": ack})


@relay_router.post("/ack")
async def relay_ack(request: Request):
    """The recipient's signed receipt for a forwarded envelope."""
    svc = get_peer_chat()
    if svc is None:
        return _err("relay not available", 503)
    try:
        body = await request.json()
        pubkey = body.get("public_key_hex", "")
        message_uuid = body.get("message_uuid", "")
        ct_hash = body.get("ciphertext_sha256", "")
        sig = body.get("signature", "")
    except Exception:
        return _err("invalid JSON", 400)
    if not (pubkey and message_uuid and ct_hash and sig):
        return _err("missing fields", 400)
    if not _verify_ed25519(pubkey, delivery_payload(message_uuid, ct_hash),
                           sig):
        return _err("invalid signature", 403)
    # A receipt may come from a friend OR a voucher-registered client
    # (Phase D) — the real gate is below: the uuid must match a pending
    # forward addressed to exactly this pubkey.
    friend = svc.get_friend_by_public_key(pubkey)
    if friend and friend.get("is_blocked"):
        return _err("not a friend", 403)
    if not friend:
        with _wake_lock:
            registered = pubkey in _relay_clients
        if not registered:
            return _err("not a friend", 403)

    with _ack_lock:
        entry = _pending_acks.get(message_uuid)
        # The uuid must be one WE are waiting on, for THIS recipient — so a
        # receipt cannot be planted for someone else's forward.
        if entry is None or entry[0] != pubkey:
            return _err("no such forward", 404)
        future = entry[1]
    if not future.done():
        future.get_loop().call_soon_threadsafe(
            future.set_result,
            {"public_key_hex": pubkey, "message_uuid": message_uuid,
             "ciphertext_sha256": ct_hash, "signature": sig})
    return JSONResponse({"ok": True})
