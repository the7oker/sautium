"""Peer-chat service for the Docker peer surface — the master node's half
of the chat protocol.

A trimmed mirror of desktop/p2p/chat_service.ChatService against the shared
PostgreSQL: friend upsert, NaCl Box encrypt/decrypt, incoming-message
handling and history export. Deliberately NOT mirrored: outgoing delivery
loops, pending resolution, history import — this node is a PASSIVE peer.
Guests push messages inbound and pull replies with the existing history
sync; the relay wake stream tells them when to pull. Most guests sit
behind NAT where an outbound push could never land anyway.

Keep in step with desktop/p2p/chat_service.py for the mirrored parts.
"""

import logging
import uuid as uuid_mod
from datetime import datetime, timezone
from typing import Optional

from db_pool import get_conn

logger = logging.getLogger(__name__)

# Wire cap on one chat message: ciphertext as received (base64 text) and
# plaintext after decryption. The UI caps composition at 10k chars; the peer
# surface must enforce the same bound against hand-rolled clients.
MAX_ENCRYPTED_CHARS = 90_000
MAX_PLAINTEXT_CHARS = 10_000


def _sender_timestamp(timestamp_iso: str) -> datetime:
    """The send time a peer claims, made safe to sort a thread by.

    The thread is ordered by this value, so a peer whose clock runs fast —
    or who simply lies — would otherwise pin its message above everything
    that follows, forever. A future stamp collapses to now; running behind
    is left alone, because that is exactly what delayed delivery and
    history pulls legitimately look like."""
    try:
        ts = datetime.fromisoformat(timestamp_iso)
    except ValueError:
        return datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return min(ts, datetime.now(timezone.utc))


class PeerChatService:
    """Chat crypto + DB operations bound to this node's account identity."""

    def __init__(self, private_seed_raw: bytes, public_key_hex: str):
        from nacl.public import Box  # noqa: F401 — fail fast if PyNaCl absent
        from nacl.signing import SigningKey

        self.public_key_hex = public_key_hex
        self._curve_private = SigningKey(
            private_seed_raw).to_curve25519_private_key()
        self._boxes: dict = {}

    # -- crypto (mirror of chat_service.py) --------------------------------

    def _get_box(self, friend_public_key_hex: str):
        from nacl.public import Box
        from nacl.signing import VerifyKey
        if friend_public_key_hex not in self._boxes:
            friend_curve_pub = VerifyKey(
                bytes.fromhex(friend_public_key_hex)
            ).to_curve25519_public_key()
            self._boxes[friend_public_key_hex] = Box(
                self._curve_private, friend_curve_pub)
        return self._boxes[friend_public_key_hex]

    def encrypt_message(self, plaintext: str, friend_public_key_hex: str) -> str:
        import base64
        box = self._get_box(friend_public_key_hex)
        return base64.b64encode(
            box.encrypt(plaintext.encode("utf-8"))).decode("ascii")

    def decrypt_message(self, encrypted_b64: str, sender_public_key_hex: str) -> str:
        import base64
        box = self._get_box(sender_public_key_hex)
        return box.decrypt(base64.b64decode(encrypted_b64)).decode("utf-8")

    # -- friends ------------------------------------------------------------

    def get_friend_by_public_key(self, public_key_hex: str) -> Optional[dict]:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT id, username, public_key_hex, invite_code, display_name,
                       is_blocked, source::text AS source
                  FROM friends
                 WHERE public_key_hex = %s OR previous_public_key_hex = %s
            """, (public_key_hex, public_key_hex))
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))

    def add_friend(self, public_key_hex: str, invite_code: str,
                   username: str = "", source: str = "manual",
                   source_token_id: Optional[str] = None) -> int:
        """Two-phase upsert mirroring chat_service.add_friend: resolve a
        pending: stub for this invite first, insert otherwise."""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                UPDATE friends
                   SET public_key_hex = %s,
                       username = COALESCE(NULLIF(%s, ''), username),
                       source = %s::friend_source,
                       source_token_id = %s
                 WHERE invite_code = %s
                   AND public_key_hex LIKE 'pending:%%'
             RETURNING id
            """, (public_key_hex, username, source, source_token_id,
                  invite_code))
            row = cur.fetchone()
            if row:
                return row[0]
            cur.execute("""
                INSERT INTO friends (public_key_hex, invite_code, username,
                                     source, source_token_id)
                VALUES (%s, %s, %s, %s::friend_source, %s)
                ON CONFLICT (public_key_hex) DO UPDATE SET
                    invite_code = EXCLUDED.invite_code,
                    username = COALESCE(NULLIF(EXCLUDED.username, ''),
                                        friends.username)
             RETURNING id
            """, (public_key_hex, invite_code, username, source,
                  source_token_id))
            return cur.fetchone()[0]

    def update_friend_last_seen(self, public_key_hex: str) -> None:
        """Mirror of chat_service.update_friend_last_seen — NOTIFY only on a
        real row (the UPDATE event has no trigger, unlike message inserts)."""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE friends SET last_seen = NOW()"
                        " WHERE public_key_hex = %s", (public_key_hex,))
            if cur.rowcount:
                cur.execute("NOTIFY sautium_chat")

    # -- messages -----------------------------------------------------------

    def store_message(self, friend_id: int, direction: str, content: str,
                      timestamp: Optional[datetime] = None,
                      delivered: bool = True,
                      message_uuid: Optional[str] = None) -> int:
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        if message_uuid is None:
            message_uuid = str(uuid_mod.uuid4())
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO p2p_messages
                    (friend_id, direction, content, timestamp, delivered,
                     message_uuid)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (friend_id, direction, content, timestamp, delivered,
                  message_uuid))
            # NOTIFY comes from the trg_p2p_messages_notify trigger.
            return cur.fetchone()[0]

    def _has_message_uuid(self, message_uuid: str) -> bool:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM p2p_messages WHERE message_uuid = %s",
                        (message_uuid,))
            return cur.fetchone() is not None

    def handle_incoming(self, sender_public_key: str, encrypted_b64: str,
                        timestamp_iso: str,
                        message_uuid: Optional[str] = None) -> Optional[dict]:
        """Mirror of chat_service.handle_incoming + the wire caps."""
        from nacl.exceptions import CryptoError

        friend = self.get_friend_by_public_key(sender_public_key)
        if not friend:
            logger.warning("Message from unknown sender: %s...",
                           sender_public_key[:16])
            return None
        if friend.get("is_blocked"):
            return None
        if len(encrypted_b64) > MAX_ENCRYPTED_CHARS:
            logger.warning("Oversized ciphertext from %s rejected",
                           friend.get("username", "?"))
            return None
        if message_uuid and self._has_message_uuid(message_uuid):
            return {"friend_id": friend["id"], "content": "",
                    "timestamp": None, "duplicate": True}
        try:
            content = self.decrypt_message(encrypted_b64, sender_public_key)
        except CryptoError:
            logger.error("Failed to decrypt message from %s...",
                         sender_public_key[:16])
            return None
        if len(content) > MAX_PLAINTEXT_CHARS:
            logger.warning("Oversized plaintext from %s rejected",
                           friend.get("username", "?"))
            return None

        ts = _sender_timestamp(timestamp_iso)
        self.store_message(friend["id"], "in", content, ts,
                           delivered=True, message_uuid=message_uuid)
        self.update_friend_last_seen(sender_public_key)
        return {"friend_id": friend["id"], "content": content, "timestamp": ts}

    def get_history_for_export(self, friend_id: int,
                               since: Optional[datetime] = None) -> list:
        """Messages for history sync, oldest first — includes row ids so the
        caller can mark exported outbound rows delivered (a requester that
        proved key ownership HAS the export: export is delivery)."""
        with get_conn() as conn, conn.cursor() as cur:
            if since:
                cur.execute("""
                    SELECT id, message_uuid, direction, content, timestamp
                      FROM p2p_messages
                     WHERE friend_id = %s AND timestamp > %s
                     ORDER BY timestamp
                """, (friend_id, since))
            else:
                cur.execute("""
                    SELECT id, message_uuid, direction, content, timestamp
                      FROM p2p_messages
                     WHERE friend_id = %s
                     ORDER BY timestamp
                """, (friend_id,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def mark_exported_delivered(self, friend_id: int, max_id: int) -> int:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                UPDATE p2p_messages SET delivered = TRUE
                 WHERE friend_id = %s AND direction = 'out'
                   AND delivered = FALSE AND id <= %s
            """, (friend_id, max_id))
            return cur.rowcount


_service: Optional[PeerChatService] = None


def get_peer_chat() -> Optional[PeerChatService]:
    """Lazy singleton bound to the account identity; None when the node has
    no account (chat endpoints then answer 503)."""
    global _service
    if _service is not None:
        return _service
    from config import settings
    from p2p_identity import load_signing_key, resolve_identity

    identity = resolve_identity(settings)
    key = load_signing_key(settings)
    if not identity or key is None:
        return None
    from cryptography.hazmat.primitives import serialization
    seed = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    _service = PeerChatService(seed, identity["public_key_hex"])
    return _service
