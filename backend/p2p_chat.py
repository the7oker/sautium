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
from desktop.p2p import chat_wire

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

    def __init__(self, private_seed_raw: bytes, public_key_hex: str,
                 previous_seeds: tuple = ()):
        from nacl.public import Box  # noqa: F401 — fail fast if PyNaCl absent
        from nacl.signing import SigningKey

        self.public_key_hex = public_key_hex
        self._signing_key = SigningKey(private_seed_raw)
        self._curve_private = self._signing_key.to_curve25519_private_key()
        # Keys this node retired (node_identity rotation archive), newest
        # first — a friend who has not applied the notice yet still encrypts
        # to one of them, and names one in what it signs.
        self._previous_curve = [SigningKey(s).to_curve25519_private_key()
                                for s in previous_seeds]
        self.own_keys = (public_key_hex,) + tuple(
            SigningKey(s).verify_key.encode().hex() for s in previous_seeds)
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

    def encrypt_envelope(self, text: str, message_uuid: str, ts_iso: str,
                         friend_public_key_hex: str) -> str:
        """Mirror of chat_service.encrypt_envelope: the uuid, the time and
        our signature boxed with the text (chat_wire.envelope)."""
        sig = self._signing_key.sign(chat_wire.message_statement(
            self.public_key_hex, friend_public_key_hex, message_uuid,
            chat_wire.sent_time(ts_iso), text)).signature.hex()
        return self._box(friend_public_key_hex, chat_wire.envelope(text, message_uuid, ts_iso, sig))

    def seal_exported(self, row: dict, friend_public_key_hex: str) -> str:
        """Mirror of chat_service.seal_exported."""
        if row["direction"] == "out":
            return self.encrypt_envelope(row["content"], str(row["message_uuid"]),
                                         row["timestamp"].isoformat(), friend_public_key_hex)
        sig = bytes(row["sender_sig"]).hex() if row["sender_sig"] is not None else None
        sent = row["sent_at"] or row["timestamp"]
        return self._box(friend_public_key_hex, chat_wire.envelope(
            row["content"], str(row["message_uuid"]), sent.isoformat(), sig))

    def _box(self, friend_public_key_hex: str, plaintext: bytes) -> str:
        import base64
        return base64.b64encode(self._get_box(friend_public_key_hex).encrypt(plaintext)).decode("ascii")

    def decrypt_envelope(self, encrypted_b64: str, sender_public_key_hex: str) -> dict:
        """Mirror of chat_service.decrypt_envelope."""
        return chat_wire.open_envelope(
            self.decrypt_message(encrypted_b64, sender_public_key_hex).encode("utf-8"))

    def decrypt_message(self, encrypted_b64: str, sender_public_key_hex: str) -> str:
        import base64
        from nacl.exceptions import CryptoError
        from nacl.public import Box
        from nacl.signing import VerifyKey
        encrypted = base64.b64decode(encrypted_b64)
        try:
            return self._get_box(sender_public_key_hex).decrypt(encrypted).decode("utf-8")
        except CryptoError:
            if not self._previous_curve:
                raise
        friend_curve_pub = VerifyKey(
            bytes.fromhex(sender_public_key_hex)).to_curve25519_public_key()
        for index, curve_private in enumerate(self._previous_curve):
            try:
                plaintext = Box(curve_private, friend_curve_pub).decrypt(encrypted)
            except CryptoError:
                if index == len(self._previous_curve) - 1:
                    raise
                continue
            logger.info("Message from %s… was encrypted to a retired key — "
                        "they have not applied the rotation yet",
                        sender_public_key_hex[:16])
            return plaintext.decode("utf-8")

    def apply_key_rotation(self, friend_id: int, old_public_key_hex: str,
                           new_public_key_hex: str, new_invite_code: str) -> None:
        """Mirror of chat_service.store_key_rotation + apply_key_rotation in
        one step: the friend row moves to the new key, the old one stays on
        it as previous_public_key_hex (a record: nothing answers to it), and
        the Box cached for the old key is dropped."""
        with get_conn() as conn, conn.cursor() as cur:
            # The friend may already be here under the new key: a node that
            # could not deliver its notice re-introduced itself by token, and
            # that made a second friendship. Fold it into the original — the
            # history lives there — before the key moves.
            cur.execute(
                "SELECT id FROM friends WHERE public_key_hex = %s AND id <> %s",
                (new_public_key_hex, friend_id))
            dup = cur.fetchone()
            if dup:
                cur.execute("UPDATE p2p_messages SET friend_id = %s WHERE friend_id = %s",
                            (friend_id, dup[0]))
                cur.execute("DELETE FROM friends WHERE id = %s", (dup[0],))
                logger.info("Key rotation: merged duplicate friend row %s into %s",
                            dup[0], friend_id)
            cur.execute("""
                UPDATE friends
                   SET previous_public_key_hex = public_key_hex,
                       public_key_hex = %s,
                       invite_code = %s
                 WHERE id = %s
            """, (new_public_key_hex, new_invite_code, friend_id))
        self._boxes.pop(old_public_key_hex, None)

    # -- friends ------------------------------------------------------------

    def get_friend_by_public_key(self, public_key_hex: str) -> Optional[dict]:
        """By the friend's current key only (mirror of chat_service): a key
        the friend rotated away from — leaked, perhaps — speaks for nobody."""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT id, username, public_key_hex, invite_code, display_name,
                       is_blocked, source::text AS source
                  FROM friends
                 WHERE public_key_hex = %s
            """, (public_key_hex,))
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))

    def add_friend(self, public_key_hex: str, invite_code: str,
                   username: str = "", source: str = "manual",
                   source_token_id: Optional[str] = None) -> Optional[int]:
        """Two-phase upsert mirroring chat_service.add_friend: resolve a
        pending: stub for this invite first — one bound to a fingerprint
        only for the key it names, else nothing (None) —, insert
        otherwise."""
        with get_conn() as conn, conn.cursor() as cur:
            # Either way the peer accepted the key we hold NOW — that is
            # what bound_identity records (mirror of chat_service.add_friend).
            cur.execute("""
                UPDATE friends
                   SET public_key_hex = %s,
                       username = COALESCE(NULLIF(%s, ''), username),
                       source = %s::friend_source,
                       source_token_id = %s,
                       bound_identity = %s
                 WHERE invite_code = %s
                   AND public_key_hex LIKE 'pending:%%'
                   AND (key_fingerprint IS NULL
                        OR key_fingerprint = substring(sha256(decode(%s, 'hex')) FROM 1 FOR 16))
             RETURNING id
            """, (public_key_hex, username, source, source_token_id,
                  self.public_key_hex, invite_code, public_key_hex))
            row = cur.fetchone()
            if row:
                return row[0]
            cur.execute("SELECT 1 FROM friends WHERE invite_code = %s"
                        " AND public_key_hex LIKE 'pending:%%'", (invite_code,))
            if cur.fetchone() is not None:
                logger.warning("A key %s… that the contact %s is not bound to — nothing added",
                               public_key_hex[:16], invite_code)
                return None
            cur.execute("""
                INSERT INTO friends (public_key_hex, invite_code, username,
                                     source, source_token_id, bound_identity)
                VALUES (%s, %s, %s, %s::friend_source, %s, %s)
                ON CONFLICT (public_key_hex) DO UPDATE SET
                    invite_code = EXCLUDED.invite_code,
                    username = COALESCE(NULLIF(EXCLUDED.username, ''),
                                        friends.username),
                    bound_identity = EXCLUDED.bound_identity
             RETURNING id
            """, (public_key_hex, invite_code, username, source,
                  source_token_id, self.public_key_hex))
            return cur.fetchone()[0]

    def friend_for_key(self, public_key_hex: str) -> Optional[dict]:
        """Mirror of chat_service.friend_for_key: the resolved row, else a
        `pending:` stub whose invite code binds this key, resolved here —
        the operator's consent plus the key the digest names is all the
        classic handshake establishes, and a peer that already holds us as
        a friend never sends one. Token stubs stay pending: those resolve
        through our own token handshake."""
        friend = self.get_friend_by_public_key(public_key_hex)
        if friend is not None:
            return friend
        from p2p_identity import verify_invite_code
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT invite_code, upper(encode(key_fingerprint, 'hex')) FROM friends
                 WHERE public_key_hex LIKE 'pending:%'
                   AND join_token_id IS NULL
            """)
            stubs = cur.fetchall()
        invite = next((code for code, fingerprint in stubs
                       if verify_invite_code(code, public_key_hex)
                       and chat_wire.binds(fingerprint, public_key_hex)), None)
        if invite is None:
            return None
        self.add_friend(public_key_hex, invite)
        logger.info("Pending friend %s resolved by the peer's own request "
                    "(%s...)", invite, public_key_hex[:16])
        return self.get_friend_by_public_key(public_key_hex)

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

    def _stored_message(self, message_uuid: str) -> Optional[tuple]:
        """(friend_id, content) of the message stored under this uuid."""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT friend_id, content FROM p2p_messages WHERE message_uuid = %s",
                        (message_uuid,))
            return cur.fetchone()

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
        try:
            env = self.decrypt_envelope(encrypted_b64, sender_public_key)
        except (CryptoError, ValueError):
            logger.error("Failed to open a message from %s...",
                         sender_public_key[:16])
            return None
        content = env["text"]
        if message_uuid and message_uuid != env["uuid"]:
            logger.warning("Message from %s names another uuid than its box — rejected",
                           friend.get("username", "?"))
            return None
        if len(content) > MAX_PLAINTEXT_CHARS:
            logger.warning("Oversized plaintext from %s rejected",
                           friend.get("username", "?"))
            return None
        try:
            sent_at = chat_wire.sent_time(env["ts"])
        except ValueError:
            sent_at = None
        if sent_at is None or not chat_wire.signed_by(
                env["sig"], (sender_public_key,), self.own_keys, env["uuid"], sent_at, content):
            logger.warning("Message from %s without its sender's signature — rejected",
                           friend.get("username", "?"))
            return None
        ts = _sender_timestamp(env["ts"])
        stored = self._stored_message(env["uuid"])
        if stored is None:
            if self._store_incoming(friend["id"], content, ts, env["uuid"], env["sig"], sent_at):
                self.update_friend_last_seen(sender_public_key)
                return {"friend_id": friend["id"], "content": content, "timestamp": ts}
            stored = self._stored_message(env["uuid"])      # another copy of it landed first
        if stored[0] == friend["id"] and stored[1] == content:
            return {"friend_id": friend["id"], "content": "",
                    "timestamp": None, "duplicate": True}
        logger.warning("Message %s from %s reuses a uuid — rejected",
                       env["uuid"][:8], friend.get("username", "?"))
        return None

    def _store_incoming(self, friend_id: int, content: str, ts: datetime,
                        message_uuid: str, sig: str, sent_at: datetime) -> bool:
        """An incoming message stored with its sender's signature and the
        time it signed, False when its uuid already is: two copies of one
        message (a direct push and a relay's, or a push and its retry)
        arrive side by side on two threads."""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO p2p_messages
                    (friend_id, direction, content, timestamp, delivered, message_uuid,
                     sender_sig, sent_at)
                VALUES (%s, 'in', %s, %s, TRUE, %s, %s, %s)
                ON CONFLICT (message_uuid) WHERE message_uuid IS NOT NULL DO NOTHING
                RETURNING id
            """, (friend_id, content, ts, message_uuid, bytes.fromhex(sig), sent_at))
            return cur.fetchone() is not None


_service: Optional[PeerChatService] = None


def forget_service() -> None:
    """After a key rotation: the next get_peer_chat() builds on the new key."""
    global _service
    _service = None


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
    from desktop.node_identity import load_previous_seeds
    from p2p_identity import identity_dir
    d = identity_dir(settings)
    previous = load_previous_seeds(d) if d else []
    _service = PeerChatService(seed, identity["public_key_hex"], tuple(previous))
    return _service
