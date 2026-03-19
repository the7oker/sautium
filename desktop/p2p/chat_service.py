"""
Encrypted P2P chat service for Sautium.

Uses NaCl Box (Curve25519 + XSalsa20-Poly1305) for end-to-end encryption.
Ed25519 signing keys are converted to Curve25519 for key exchange.

Message flow:
  Sender encrypts with NaCl Box(sender_curve_private, recipient_curve_public)
  → sends via HTTP to recipient's sync server
  → Recipient decrypts with NaCl Box(recipient_curve_private, sender_curve_public)
  → stores plaintext locally

Requires: PyNaCl, psycopg2.
"""

import base64
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from nacl.public import Box, PrivateKey, PublicKey
    from nacl.signing import SigningKey, VerifyKey
    from nacl.exceptions import CryptoError
    HAS_NACL = True
except ImportError:
    HAS_NACL = False
    logger.info("PyNaCl not installed — chat encryption disabled")


def ed25519_to_curve25519_private(ed25519_seed: bytes) -> "PrivateKey":
    """Convert Ed25519 seed (32 bytes) to Curve25519 private key."""
    signing_key = SigningKey(ed25519_seed)
    return signing_key.to_curve25519_private_key()


def ed25519_to_curve25519_public(ed25519_public_hex: str) -> "PublicKey":
    """Convert Ed25519 public key (hex) to Curve25519 public key."""
    pub_bytes = bytes.fromhex(ed25519_public_hex)
    verify_key = VerifyKey(pub_bytes)
    return verify_key.to_curve25519_public_key()


class ChatService:
    """Manages encrypted messaging with friends."""

    def __init__(self, db_dsn: str, private_key_raw: bytes, public_key_hex: str):
        """
        Args:
            db_dsn: PostgreSQL connection string
            private_key_raw: 32-byte Ed25519 seed
            public_key_hex: hex-encoded Ed25519 public key
        """
        if not HAS_NACL:
            raise RuntimeError("PyNaCl required for chat")

        self.db_dsn = db_dsn
        self.public_key_hex = public_key_hex
        self._curve_private = ed25519_to_curve25519_private(private_key_raw)
        # Cache Box instances per friend public key
        self._boxes: dict[str, Box] = {}

    def _get_box(self, friend_public_key_hex: str) -> Box:
        """Get or create NaCl Box for a friend."""
        if friend_public_key_hex not in self._boxes:
            friend_curve_pub = ed25519_to_curve25519_public(friend_public_key_hex)
            self._boxes[friend_public_key_hex] = Box(
                self._curve_private, friend_curve_pub
            )
        return self._boxes[friend_public_key_hex]

    def encrypt_message(self, plaintext: str, friend_public_key_hex: str) -> str:
        """Encrypt a message for a friend. Returns base64(nonce + ciphertext)."""
        box = self._get_box(friend_public_key_hex)
        encrypted = box.encrypt(plaintext.encode("utf-8"))
        return base64.b64encode(encrypted).decode("ascii")

    def decrypt_message(self, encrypted_b64: str, sender_public_key_hex: str) -> str:
        """Decrypt a message from a friend. Returns plaintext string."""
        box = self._get_box(sender_public_key_hex)
        encrypted = base64.b64decode(encrypted_b64)
        plaintext = box.decrypt(encrypted)
        return plaintext.decode("utf-8")

    # -------------------------------------------------------------------
    # Friend management (DB operations)
    # -------------------------------------------------------------------

    def _get_db(self):
        import psycopg2
        conn = psycopg2.connect(self.db_dsn)
        conn.autocommit = True
        return conn

    def add_friend(
        self,
        public_key_hex: str,
        invite_code: str,
        username: str = "",
        display_name: str = "",
    ) -> Optional[int]:
        """Add a friend. Returns friend ID or None if already exists."""
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO friends (public_key_hex, invite_code, username, display_name)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (public_key_hex) DO UPDATE
                        SET invite_code = EXCLUDED.invite_code,
                            username = EXCLUDED.username,
                            display_name = COALESCE(NULLIF(EXCLUDED.display_name, ''), friends.display_name)
                    RETURNING id
                """, (public_key_hex, invite_code, username, display_name))
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()

    def remove_friend(self, friend_id: int) -> bool:
        """Remove a friend and all their messages."""
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM friends WHERE id = %s", (friend_id,))
                return cur.rowcount > 0
        finally:
            conn.close()

    def get_friends(self) -> list[dict]:
        """Get all friends."""
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, username, public_key_hex, invite_code,
                           display_name, added_at, last_seen, is_blocked
                    FROM friends
                    ORDER BY display_name, username
                """)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            conn.close()

    def get_friend_by_public_key(self, public_key_hex: str) -> Optional[dict]:
        """Find a friend by their public key."""
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, username, public_key_hex, invite_code,
                           display_name, is_blocked
                    FROM friends WHERE public_key_hex = %s
                """, (public_key_hex,))
                row = cur.fetchone()
                if not row:
                    return None
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))
        finally:
            conn.close()

    def update_friend_last_seen(self, public_key_hex: str):
        """Update last_seen timestamp for a friend."""
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE friends SET last_seen = NOW()
                    WHERE public_key_hex = %s
                """, (public_key_hex,))
        finally:
            conn.close()

    # -------------------------------------------------------------------
    # Message storage
    # -------------------------------------------------------------------

    def store_message(
        self,
        friend_id: int,
        direction: str,
        content: str,
        timestamp: Optional[datetime] = None,
        delivered: bool = True,
    ) -> int:
        """Store a chat message (plaintext). Returns message ID."""
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO p2p_messages
                        (friend_id, direction, content, timestamp, delivered)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id
                """, (friend_id, direction, content, timestamp, delivered))
                return cur.fetchone()[0]
        finally:
            conn.close()

    def get_messages(
        self,
        friend_id: int,
        limit: int = 50,
        before_id: Optional[int] = None,
    ) -> list[dict]:
        """Get chat messages with a friend, newest first."""
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                if before_id:
                    cur.execute("""
                        SELECT id, direction, content, timestamp, delivered, read
                        FROM p2p_messages
                        WHERE friend_id = %s AND id < %s
                        ORDER BY id DESC LIMIT %s
                    """, (friend_id, before_id, limit))
                else:
                    cur.execute("""
                        SELECT id, direction, content, timestamp, delivered, read
                        FROM p2p_messages
                        WHERE friend_id = %s
                        ORDER BY id DESC LIMIT %s
                    """, (friend_id, limit))
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
                rows.reverse()  # chronological order
                return rows
        finally:
            conn.close()

    def mark_messages_read(self, friend_id: int):
        """Mark all unread incoming messages from a friend as read."""
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE p2p_messages
                    SET read = TRUE
                    WHERE friend_id = %s AND direction = 'in' AND read = FALSE
                """, (friend_id,))
        finally:
            conn.close()

    def get_unread_counts(self) -> dict[int, int]:
        """Get unread message counts per friend. Returns {friend_id: count}."""
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT friend_id, COUNT(*)
                    FROM p2p_messages
                    WHERE direction = 'in' AND read = FALSE
                    GROUP BY friend_id
                """)
                return {row[0]: row[1] for row in cur.fetchall()}
        finally:
            conn.close()

    def get_pending_messages(self) -> list[dict]:
        """Get all undelivered outgoing messages (for retry when friend comes online)."""
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT m.id, m.friend_id, m.content, m.timestamp,
                           f.public_key_hex, f.invite_code
                    FROM p2p_messages m
                    JOIN friends f ON f.id = m.friend_id
                    WHERE m.direction = 'out' AND m.delivered = FALSE
                    ORDER BY m.timestamp
                """)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            conn.close()

    def mark_delivered(self, message_id: int):
        """Mark an outgoing message as delivered."""
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE p2p_messages SET delivered = TRUE WHERE id = %s
                """, (message_id,))
        finally:
            conn.close()

    # -------------------------------------------------------------------
    # Key rotation
    # -------------------------------------------------------------------

    def store_key_rotation(
        self,
        friend_id: int,
        old_public_key_hex: str,
        new_public_key_hex: str,
        new_invite_code: str,
        rotation_message: bytes,
        signature: bytes,
    ):
        """Store a pending key rotation from a friend."""
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO pending_key_rotations
                        (friend_id, old_public_key_hex, new_public_key_hex,
                         new_invite_code, rotation_message, signature)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (friend_id) DO UPDATE SET
                        old_public_key_hex = EXCLUDED.old_public_key_hex,
                        new_public_key_hex = EXCLUDED.new_public_key_hex,
                        new_invite_code = EXCLUDED.new_invite_code,
                        rotation_message = EXCLUDED.rotation_message,
                        signature = EXCLUDED.signature,
                        created_at = NOW()
                """, (
                    friend_id, old_public_key_hex, new_public_key_hex,
                    new_invite_code, rotation_message, signature,
                ))
        finally:
            conn.close()

    def apply_key_rotation(self, friend_id: int) -> bool:
        """Apply a pending key rotation: update friend's public key."""
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT new_public_key_hex, new_invite_code
                    FROM pending_key_rotations
                    WHERE friend_id = %s
                """, (friend_id,))
                row = cur.fetchone()
                if not row:
                    return False

                new_pubkey, new_invite = row
                cur.execute("""
                    UPDATE friends
                    SET previous_public_key_hex = public_key_hex,
                        public_key_hex = %s,
                        invite_code = %s
                    WHERE id = %s
                """, (new_pubkey, new_invite, friend_id))

                cur.execute("""
                    DELETE FROM pending_key_rotations WHERE friend_id = %s
                """, (friend_id,))

                # Invalidate cached Box
                if new_pubkey in self._boxes:
                    del self._boxes[new_pubkey]

                return True
        finally:
            conn.close()

    # -------------------------------------------------------------------
    # High-level send/receive
    # -------------------------------------------------------------------

    def prepare_outgoing(self, friend_id: int, content: str) -> Optional[dict]:
        """
        Prepare an outgoing message: encrypt + store locally.

        Returns dict for HTTP sending:
            {from_public_key, encrypted, timestamp}
        Or None if friend not found.
        """
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT public_key_hex FROM friends WHERE id = %s",
                    (friend_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                friend_pubkey = row[0]
        finally:
            conn.close()

        ts = datetime.now(timezone.utc)
        encrypted = self.encrypt_message(content, friend_pubkey)

        # Store plaintext locally (not yet delivered)
        self.store_message(friend_id, "out", content, ts, delivered=False)

        return {
            "from_public_key": self.public_key_hex,
            "encrypted": encrypted,
            "timestamp": ts.isoformat(),
        }

    def handle_incoming(self, sender_public_key: str, encrypted_b64: str,
                        timestamp_iso: str) -> Optional[dict]:
        """
        Handle an incoming encrypted message.

        Returns: {friend_id, content, timestamp} or None if sender unknown/blocked.
        """
        friend = self.get_friend_by_public_key(sender_public_key)
        if not friend:
            logger.warning(f"Message from unknown sender: {sender_public_key[:16]}...")
            return None
        if friend.get("is_blocked"):
            logger.info(f"Message from blocked friend: {friend.get('username', '?')}")
            return None

        try:
            content = self.decrypt_message(encrypted_b64, sender_public_key)
        except CryptoError:
            logger.error(f"Failed to decrypt message from {sender_public_key[:16]}...")
            return None

        ts = datetime.fromisoformat(timestamp_iso)
        self.store_message(friend["id"], "in", content, ts, delivered=True)
        self.update_friend_last_seen(sender_public_key)

        return {
            "friend_id": friend["id"],
            "content": content,
            "timestamp": ts,
        }
