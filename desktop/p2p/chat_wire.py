"""The chat protocol's signed statements and its history pages — one
definition for both peer surfaces (the launcher: sync_server, chat_service,
p2p_manager; Docker: backend/routers/peer_chat, backend/p2p_chat), which
import it.

Every statement starts with its own context string, so a signature made for
one can never be read as another, nor as any request, receipt or seal of
the rest of the network. A request a responder acts on names that responder
and a time, so it cannot be replayed at another node or later than
TS_WINDOW.
"""

import hashlib
import json
import time
from datetime import datetime
from typing import Optional
from uuid import UUID

TS_WINDOW = 60

_HANDSHAKE = "sautium-chat-handshake:v1"
_HISTORY = "sautium-chat-history:v1"
ENVELOPE_VERSION = 2
# A history export is a page of a thread, oldest first after the cursor it
# is asked from: at most HISTORY_EXPORT_MAX messages and
# HISTORY_EXPORT_BYTES of encrypted text, with the cursor of its last row
# (`next`) and whether the thread goes on (`more`). The exporter marks the
# outgoing messages a page carries delivered, so a page a requester refused
# would be lost: it stays well under the requester's bound
# (p2p_manager.PEER_ANSWER_CAP). One pull reads at most
# HISTORY_PAGES_PER_PULL pages; the next one goes on from its cursor.
HISTORY_EXPORT_MAX = 2000
HISTORY_EXPORT_BYTES = 4 * 1024 * 1024
HISTORY_PAGES_PER_PULL = 32


def key_digest(pubkey_hex: str) -> str:
    """The 12-hex fingerprint an invite code carries (node_identity
    .make_invite_code), upper case."""
    return hashlib.sha256(bytes.fromhex(pubkey_hex)).digest()[:6].hex().upper()


def invite_digest(invite_code: str) -> Optional[str]:
    """The fingerprint part of an invite code, canonical, or None."""
    if not isinstance(invite_code, str) or "#" not in invite_code:
        return None
    clean = invite_code.split("#", 1)[1].replace("-", "").upper()
    return clean if len(clean) == 12 else None


def fresh(ts, now: Optional[float] = None) -> bool:
    try:
        return abs((time.time() if now is None else now) - int(ts)) <= TS_WINDOW
    except (TypeError, ValueError):
        return False


def handshake_payload(ts: int, responder_digest: str, pubkey: str, username: str) -> bytes:
    """What the initiator of a classic handshake signs: the responder it is
    for (its invite fingerprint — the initiator may know no more of it
    yet), its own key and name, the time."""
    return (f"{_HANDSHAKE}:{int(ts)}:{responder_digest.upper()}:{pubkey.lower()}:{username}"
            .encode("utf-8"))


def sign_handshake(sign, body: dict, responder_invite: str, now: Optional[float] = None) -> dict:
    """`body` ({public_key_hex, username, invite_code}) with the classic
    handshake's ts and signature added, for the node `responder_invite`
    names."""
    ts = int(time.time() if now is None else now)
    digest = invite_digest(responder_invite) or ""
    sig = sign(handshake_payload(ts, digest, body["public_key_hex"], body["username"]))
    return {**body, "handshake_ts": ts, "handshake_sig": sig.hex()}


def handshake_signed(body: dict, responder_pubkey: str, verify) -> bool:
    """True when `body` carries a fresh classic-handshake signature by the
    key it names, made for this responder, under the name its invite code
    carries. `verify(pubkey_hex, message_bytes, sig_hex) -> bool`."""
    pubkey, username = body.get("public_key_hex"), body.get("username")
    invite, ts, sig = body.get("invite_code"), body.get("handshake_ts"), body.get("handshake_sig")
    if not all(isinstance(v, str) for v in (pubkey, username, invite, sig)) or not fresh(ts):
        return False
    if invite.split("#", 1)[0] != username:
        return False
    return verify(pubkey, handshake_payload(int(ts), key_digest(responder_pubkey), pubkey, username), sig)


def history_payload(ts: int, nonce: str, responder_pubkey: str) -> bytes:
    """What a history request signs: the responder it is for, so a node a
    friend pulls from cannot present the request to another node."""
    return f"{_HISTORY}:{int(ts)}:{nonce}:{responder_pubkey.lower()}".encode("utf-8")


def history_cursor(value) -> Optional[tuple]:
    """(timestamp, id) a page starts after, from a request's `after` —
    None for the thread's start, which anything that is no cursor reads as."""
    if not isinstance(value, str):
        return None
    ts, _, row_id = value.partition("|")
    try:
        return datetime.fromisoformat(ts), int(row_id)
    except ValueError:
        return None


def export_rows(conn, friend_id: int, after: Optional[tuple]) -> list[dict]:
    """A page's rows of a thread, oldest first after the cursor `after`
    (history_cursor), one past a page's count — with row ids, so the
    exporter can mark the outgoing rows a page carries delivered."""
    ts, row_id = after or (None, None)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, message_uuid, direction, content, timestamp
              FROM p2p_messages
             WHERE friend_id = %s
               AND (%s::timestamptz IS NULL OR (timestamp, id) > (%s::timestamptz, %s))
             ORDER BY timestamp, id
             LIMIT %s
        """, (friend_id, ts, ts, row_id, HISTORY_EXPORT_MAX + 1))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def mark_exported(conn, friend_id: int, ids: list) -> int:
    """The outgoing rows a page carried, delivered — a requester that
    proved its key HAS the page: export is delivery, for what it carried
    and never for a row it did not."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE p2p_messages SET delivered = TRUE
             WHERE friend_id = %s AND direction = 'out'
               AND delivered = FALSE AND id = ANY(%s)
        """, (friend_id, ids))
        return cur.rowcount


def export_page(rows: list, seal) -> tuple[list, list, Optional[str], bool]:
    """One page of a thread from `rows` — oldest first, read one past
    HISTORY_EXPORT_MAX, each {id, message_uuid, direction, content,
    timestamp} — boxed by `seal(text, uuid, ts_iso)`: (the page, the ids of
    the outgoing rows it carries — what the exporter marks delivered, and
    nothing else —, the cursor of its last row or None for an empty page,
    whether the thread goes on past it)."""
    page, out_ids, size, cursor = [], [], 0, None
    for n, row in enumerate(rows[:HISTORY_EXPORT_MAX], 1):
        ts_iso = row["timestamp"].isoformat()
        message_uuid = str(row["message_uuid"])
        encrypted = seal(row["content"], message_uuid, ts_iso)
        page.append({"message_uuid": message_uuid, "direction": row["direction"],
                     "encrypted": encrypted, "timestamp": ts_iso})
        size += len(encrypted)
        if row["direction"] == "out":
            out_ids.append(row["id"])
        cursor = f"{ts_iso}|{row['id']}"
        if size >= HISTORY_EXPORT_BYTES:
            return page, out_ids, cursor, n < len(rows)
    return page, out_ids, cursor, len(rows) > HISTORY_EXPORT_MAX


def envelope(text: str, message_uuid: str, ts_iso: str) -> bytes:
    """A message's plaintext as it is boxed: its uuid and time travel inside
    the box with the text, so nobody between the pair can give a ciphertext
    another uuid or another time."""
    return json.dumps({"v": ENVELOPE_VERSION, "uuid": message_uuid, "ts": ts_iso, "text": text},
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def open_envelope(plaintext: bytes) -> dict:
    """{uuid, ts, text} of a boxed message — ValueError for anything that is
    not an envelope of this version."""
    try:
        env = json.loads(plaintext)
    except (ValueError, RecursionError):
        raise ValueError("not an envelope") from None
    if not isinstance(env, dict) or env.get("v") != ENVELOPE_VERSION:
        raise ValueError("not an envelope")
    if not all(isinstance(env.get(k), str) for k in ("uuid", "ts", "text")):
        raise ValueError("envelope fields")
    UUID(env["uuid"])
    return {"uuid": env["uuid"], "ts": env["ts"], "text": env["text"]}
