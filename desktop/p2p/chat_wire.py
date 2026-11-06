"""The chat protocol's signed statements and its history pages — one
definition for both peer surfaces (the launcher: sync_server, chat_service,
p2p_manager; Docker: backend/routers/peer_chat, backend/p2p_chat), which
import it. The invite wire lives here too: what is pasted into Add, the four
texts the Worker's invite routes take (worker/verify.js speaks the same
shape; tests/p2p/vectors/invite_wire_v1.json pins the two), and what an
inviter's node does with an acceptance its pickup brings.

Every statement starts with its own context string, so a signature made for
one can never be read as another, nor as any request, receipt or seal of
the rest of the network. A request a responder acts on names that responder
and a time, so it cannot be replayed at another node or later than
TS_WINDOW.
"""

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

logger = logging.getLogger(__name__)

TS_WINDOW = 60

_HANDSHAKE = "sautium-chat-handshake:v1"
_HISTORY = "sautium-chat-history:v1"
_MESSAGE = "sautium-chat-message:v1"
ENVELOPE_VERSION = 3
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
        at, row = datetime.fromisoformat(ts), int(row_id)
    except ValueError:
        return None
    return (at, row) if at.tzinfo is not None else None      # the cursors given carry their offset


def export_rows(conn, friend_id: int, after: Optional[tuple]) -> list[dict]:
    """A page's rows of a thread, oldest first after the cursor `after`
    (history_cursor), one past a page's count — with row ids, so the
    exporter can mark the outgoing rows a page carries delivered. Its own
    undelivered outgoing rows come whatever the cursor: one taken from
    another node of this account, or before this node's clock stepped back,
    stands past rows the requester never had."""
    ts, row_id = after or (None, None)
    with conn.cursor() as cur:
        # A row's text comes only while the texts before it are under the
        # page's bytes (its ciphertext is larger still, so export_page stops
        # first); the rest come as ids, to say the thread goes on.
        # Two index paths, each cut to a page — the thread after the cursor
        # (idx_p2p_messages_thread) and the pending outgoing rows
        # (idx_p2p_messages_pending) —, so a page costs a page whatever the
        # planner would make of an OR across them.
        cur.execute("""
            SELECT id, message_uuid, direction, timestamp, sender_sig, sent_at,
                   CASE WHEN before < %(bytes)s THEN content END AS content
              FROM (SELECT id, message_uuid, direction, content, timestamp, sender_sig, sent_at,
                           sum(octet_length(content)) OVER (ORDER BY timestamp, id)
                               - octet_length(content) AS before
                      FROM (SELECT * FROM (
                                (SELECT id, message_uuid, direction, content, timestamp, sender_sig, sent_at
                                   FROM p2p_messages
                                  WHERE friend_id = %(f)s
                                    AND (%(ts)s::timestamptz IS NULL
                                         OR (timestamp, id) > (%(ts)s::timestamptz, %(id)s))
                                  ORDER BY timestamp, id LIMIT %(n)s)
                                UNION
                                (SELECT id, message_uuid, direction, content, timestamp, sender_sig, sent_at
                                   FROM p2p_messages
                                  WHERE friend_id = %(f)s AND direction = 'out' AND delivered = FALSE
                                  ORDER BY timestamp, id LIMIT %(n)s)) both_paths
                             ORDER BY timestamp, id LIMIT %(n)s) thread) page
             ORDER BY timestamp, id
        """, {"f": friend_id, "ts": ts, "id": row_id, "n": HISTORY_EXPORT_MAX + 1,
              "bytes": HISTORY_EXPORT_BYTES})
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


def export_page(rows: list, seal, after: Optional[tuple] = None
                ) -> tuple[list, list, Optional[str], bool]:
    """One page of a thread from `rows` (export_rows) after the cursor
    `after`, each row boxed by `seal(row)` (exported_envelope's row): (the
    page, the ids of the outgoing rows it carries — what the exporter marks
    delivered, and nothing else —, the cursor the next page starts after —
    never behind `after`; None while there is none —, whether the thread
    goes on past the page)."""
    page, out_ids, size, last = [], [], 0, after
    for n, row in enumerate(rows[:HISTORY_EXPORT_MAX], 1):
        ts_iso = row["timestamp"].isoformat()
        message_uuid = str(row["message_uuid"])
        encrypted = seal(row)
        page.append({"message_uuid": message_uuid, "direction": row["direction"],
                     "encrypted": encrypted, "timestamp": ts_iso})
        size += len(encrypted)
        if row["direction"] == "out":
            out_ids.append(row["id"])
        if last is None or (row["timestamp"], row["id"]) > last:
            last = (row["timestamp"], row["id"])
        if size >= HISTORY_EXPORT_BYTES:
            break
    else:
        n = min(len(rows), HISTORY_EXPORT_MAX)
    cursor = f"{last[0].isoformat()}|{last[1]}" if last is not None else None
    return page, out_ids, cursor, n < len(rows)


def sent_time(ts_iso: str) -> datetime:
    """The instant a message's sender signed, from the time its box carries
    — ValueError for a time with no offset, which names no instant."""
    at = datetime.fromisoformat(ts_iso)
    if at.tzinfo is None:
        raise ValueError("a send time without an offset")
    return at


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def message_statement(sender: str, recipient: str, message_uuid: str, sent_at: datetime,
                      text: str) -> bytes:
    """What the sender of a message signs inside its box: who wrote it to
    whom, which message, when and what — so a line a friend's history
    export claims as ours stands only under our own signature, and nobody
    gives a message another author, recipient, uuid, time or text. The time
    is the instant in microseconds since the epoch and the uuid its
    canonical form: the one spelling every serialisation and every stored
    row of each shares."""
    micros = (sent_at - _EPOCH) // datetime.resolution
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return (f"{_MESSAGE}:{sender.lower()}:{recipient.lower()}:{UUID(message_uuid)}:{micros}:{digest}"
            .encode("utf-8"))


def envelope(text: str, message_uuid: str, ts_iso: str, sig: Optional[str]) -> bytes:
    """A message's plaintext as it is boxed: its uuid and time travel inside
    the box with the text, so nobody between the pair can give a ciphertext
    another uuid or another time, and so does its sender's signature over
    message_statement — None only for a history row stored before messages
    carried one."""
    return json.dumps({"v": ENVELOPE_VERSION, "uuid": message_uuid, "ts": ts_iso, "text": text,
                       "sig": sig}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


_SIG_RE = re.compile(r"[0-9a-f]{128}")


def open_envelope(plaintext: bytes) -> dict:
    """{uuid, ts, text, sig} of a boxed message — ValueError for anything
    that is not an envelope of this version. `sig` is 128 lower-case hex or
    None; whether a None is taken is the receiving path's to say."""
    try:
        env = json.loads(plaintext)
    except (ValueError, RecursionError):
        raise ValueError("not an envelope") from None
    if not isinstance(env, dict) or env.get("v") != ENVELOPE_VERSION:
        raise ValueError("not an envelope")
    if not all(isinstance(env.get(k), str) for k in ("uuid", "ts", "text")):
        raise ValueError("envelope fields")
    sig = env.get("sig")
    if sig is not None and not (isinstance(sig, str) and _SIG_RE.fullmatch(sig)):
        raise ValueError("envelope signature")
    UUID(env["uuid"])
    return {"uuid": env["uuid"], "ts": env["ts"], "text": env["text"], "sig": sig}


def verify(pubkey_hex: str, message: bytes, sig_hex: str) -> bool:
    """An Ed25519 signature checked by libsodium, which refuses a
    small-order key — a key anyone can sign for."""
    from nacl.exceptions import BadSignatureError
    from nacl.signing import VerifyKey
    try:
        VerifyKey(bytes.fromhex(pubkey_hex)).verify(message, bytes.fromhex(sig_hex))
        return True
    except (BadSignatureError, ValueError, TypeError):
        return False


def signed_by(sig_hex: Optional[str], senders, recipients, message_uuid: str, sent_at: datetime,
              text: str) -> bool:
    """True when `sig_hex` is a signature over this message by one of
    `senders` for one of `recipients` — the keys each side held while it
    could have been sent (a rotation leaves the earlier ones)."""
    if not sig_hex:
        return False
    return any(verify(sender, message_statement(sender, recipient, message_uuid, sent_at, text), sig_hex)
               for sender in senders if sender for recipient in recipients if recipient)


# ---------------------------------------------------------------------------
# The invite wire
# ---------------------------------------------------------------------------

_INVITE_SEND = "sautium-invite-send:v1"
_INVITE_ACCEPT = "sautium-invite-accept:v1"
_INVITE_ADD = "sautium-invite-add:v1"
_INVITE_PICKUP = "sautium-invite-pickup:v1"
_INVITE_ID = "sautium-invite-id:v1"

_USERNAME_RE = re.compile(r"[A-Za-z0-9_-]{3,32}")
_HEX_RE = re.compile(r"[0-9A-F]+")
_TOKEN_RE = re.compile(r"[0-9a-f]{32}")
_KEY_RE = re.compile(r"[0-9a-f]{64}")
_LINK_TOKEN_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
# What a mail client or a page may put inside a pasted string: whitespace,
# and the soft hyphen, the zero-width characters and the byte-order mark.
_WRAPPING_RE = re.compile(r"[\s\u00ad\u200b-\u200d\u2060\ufeff]+")
# An open invite lives 30 days on the Worker, an accepted one 30 more for its
# inviter's pickup: a node asks while an untaken invite of its own is younger
# than both together.
INVITE_PICKUP_DAYS = 60


def key_fingerprint(pubkey_hex: str) -> str:
    """The first 16 bytes of the key's SHA-256, 32 upper-case hex — what a
    long code carries."""
    return hashlib.sha256(bytes.fromhex(pubkey_hex)).digest()[:16].hex().upper()


def _groups(hex_text: str) -> str:
    return "-".join(hex_text[i:i + 4] for i in range(0, len(hex_text), 4))


def long_code(username: str, pubkey_hex: str) -> str:
    """`name#` and 128 bits of the key's SHA-256 in eight groups of four:
    the form a person copies. A node holding it binds the contact to the
    key itself, not to the 48 bits of the short code, which anyone could
    grind a key for. It starts with the short code."""
    return f"{username}#{_groups(key_fingerprint(pubkey_hex))}"


def binds(fingerprint: Optional[str], pubkey_hex: str) -> bool:
    """True when `fingerprint` (32 upper-case hex, a contact's bound
    fingerprint) names `pubkey_hex`; a contact made from a short code has
    none, and its invite code alone decides."""
    if fingerprint is None:
        return True
    try:
        return key_fingerprint(pubkey_hex) == fingerprint
    except ValueError:
        return False


@dataclass(frozen=True)
class Share:
    """What a share string pasted into Add says: `code`, the canonical short
    code every consumer downstream keys by; `fingerprint`, 32 upper-case hex
    when the string carried a long code (else None); and at most one of
    `link_token` (an invite link's join token, a UUID) and `invite_token` (a
    mailed invite's single-use token, 32 lower-case hex)."""
    code: str
    fingerprint: Optional[str] = None
    link_token: Optional[str] = None
    invite_token: Optional[str] = None


def parse_share(text: str) -> Share:
    """A share string as a person pastes it: `name#fingerprint` (12 or 32
    hex, any case, dashes anywhere) and optionally `#<uuid>` (an invite
    link, in its dashed form only) or `#i-<token>` (a mailed invite — `i`,
    `I` and the dash before the hex are one spelling). Every whitespace and
    invisible character is dropped first, a mail client's wrapping included.
    ValueError for anything else."""
    if not isinstance(text, str):
        raise ValueError("not a share string")
    parts = _WRAPPING_RE.sub("", text).split("#")
    if len(parts) not in (2, 3) or not _USERNAME_RE.fullmatch(parts[0]):
        raise ValueError("not a share string")
    fingerprint = parts[1].replace("-", "").upper()
    if len(fingerprint) not in (12, 32) or not _HEX_RE.fullmatch(fingerprint):
        raise ValueError("not a share string")
    code = f"{parts[0]}#{_groups(fingerprint[:12])}"
    bound = fingerprint if len(fingerprint) == 32 else None
    if len(parts) == 2:
        return Share(code, bound)
    tail = parts[2]
    if tail[:1] in ("i", "I"):
        token = tail[1:].replace("-", "").lower()
        if not _TOKEN_RE.fullmatch(token):
            raise ValueError("not an invite token")
        return Share(code, bound, invite_token=token)
    if not _LINK_TOKEN_RE.fullmatch(tail):
        raise ValueError("not an invite link token")
    return Share(code, bound, link_token=str(UUID(tail)))


def invite_id(token: str) -> str:
    """An invite's id: the first 16 bytes of SHA-256 over its token, as a
    UUID — the Worker's key for the invite, the inviter's record of it and
    the name its acceptor signs. It gives no way back to the token."""
    return str(UUID(hashlib.sha256(f"{_INVITE_ID}:{token}".encode("utf-8")).hexdigest()[:32]))


def send_text(ts: int, invite_code: str, to: str, message: str) -> str:
    """What an inviter signs for the Worker to mail an invite: its code,
    the recipient as written and the message's SHA-256 (of "" when there
    is none)."""
    digest = hashlib.sha256((message or "").encode("utf-8")).hexdigest()
    return f"{_INVITE_SEND}:{int(ts)}:{invite_code}:{to}:{digest}"


def accept_text(ts: int, inviteid: str, their_code: str, my_code: str, pubkey_hex: str) -> str:
    """What an acceptor signs to spend an invite: the Worker checks it, and
    the inviter's node again when its pickup brings it."""
    return f"{_INVITE_ACCEPT}:{int(ts)}:{inviteid}:{their_code}:{my_code}:{pubkey_hex.lower()}"


def add_text(ts: int, their_code: str, my_code: str, pubkey_hex: str) -> str:
    """What a node signs to tell a code's owner it added the code."""
    return f"{_INVITE_ADD}:{int(ts)}:{their_code}:{my_code}:{pubkey_hex.lower()}"


def pickup_text(ts: int, invite_code: str, pubkey_hex: str) -> str:
    """What an inviter signs to read its accepted invites."""
    return f"{_INVITE_PICKUP}:{int(ts)}:{invite_code}:{pubkey_hex.lower()}"


def take_acceptance(conn, accept: dict, own_code: str, own_pubkey: str) -> str:
    """One acceptance a pickup brought, on the inviter's node → what came of
    it: "added" (the acceptor is now a contact waiting for its handshake,
    bound to its key), "held" (a friend under that key already, or blocked:
    nothing added, the invite taken all the same), "taken" (another pickup
    took it first), or why it was skipped. All must hold: the invite is one
    this node sent and has not taken; the acceptor's statement verifies
    under the acceptor's key, for this node's code and that invite; the
    acceptor's code binds the key; the key is not this node's own. The
    invite is marked taken in the statement that wins it, and only that
    caller goes on to add: run it in one transaction, committed after."""
    try:
        inviteid = str(UUID(str(accept["invite_id"])))
        acceptor = accept["acceptor"]
        key = str(acceptor["public_key_hex"]).lower()
        code = str(acceptor["invite_code"])
        ts, sig = int(acceptor["ts"]), str(acceptor["signature"]).lower()
    except (KeyError, TypeError, ValueError, AttributeError):
        return "malformed"
    if not _KEY_RE.fullmatch(key):
        return "malformed"
    if key == own_pubkey.lower():
        return "own key"
    username = code.split("#", 1)[0]
    if not _USERNAME_RE.fullmatch(username) or code != f"{username}#{_groups(key_digest(key))}":
        return "code does not bind the key"
    if not verify(key, accept_text(ts, inviteid, own_code, code, key).encode("utf-8"), sig):
        return "statement does not verify"
    with conn.cursor() as cur:
        cur.execute("SELECT taken_at IS NOT NULL FROM sent_invites WHERE invite_id = %s", (inviteid,))
        row = cur.fetchone()
        if row is None:
            return "not an invite of this node"
        if row[0]:
            return "taken"
        cur.execute("UPDATE sent_invites SET taken_at = now() WHERE invite_id = %s AND taken_at IS NULL"
                    " RETURNING id", (inviteid,))
        if cur.fetchone() is None:
            return "taken"
        cur.execute("SELECT public_key_hex FROM friends WHERE public_key_hex = %s"
                    " OR (invite_code = %s AND public_key_hex NOT LIKE 'pending:%%')", (key, code))
        held = [row[0] for row in cur.fetchall()]
        if held:
            if key not in held:
                logger.warning("An acceptance of an invite for %s names another key than the contact "
                               "already held with that code — nothing added", code)
            return "held"
        fingerprint = bytes.fromhex(key_fingerprint(key))
        cur.execute("""
            INSERT INTO friends (username, public_key_hex, invite_code, display_name, key_fingerprint)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (public_key_hex) DO UPDATE
                SET key_fingerprint = EXCLUDED.key_fingerprint
              WHERE friends.key_fingerprint IS NULL OR friends.key_fingerprint = EXCLUDED.key_fingerprint
            RETURNING id
        """, (username, f"pending:{code}", code, username, fingerprint))
        if cur.fetchone() is None:
            logger.warning("An acceptance of an invite for %s names another key than the contact "
                           "already made with that code — the contact is left as it was", code)
            return "held"
    return "added"
