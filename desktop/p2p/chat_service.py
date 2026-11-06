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
import uuid
from datetime import datetime, timezone
from typing import Optional

from desktop.p2p import chat_wire

logger = logging.getLogger(__name__)

try:
    from nacl.public import Box, PrivateKey, PublicKey
    from nacl.signing import SigningKey, VerifyKey
    from nacl.exceptions import CryptoError
    HAS_NACL = True
except ImportError:
    HAS_NACL = False
    logger.info("PyNaCl not installed — chat encryption disabled")


# Wire caps on one chat message (mirror backend/p2p_chat.py — keep in step):
# ciphertext as received (base64 text) and plaintext after decryption. The UI
# caps composition at 10k chars; the peer surface enforces the same bound
# against hand-rolled clients.
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

    def __init__(self, db_dsn: str, private_key_raw: bytes, public_key_hex: str,
                 previous_seeds: tuple = ()):
        """
        Args:
            db_dsn: PostgreSQL connection string
            private_key_raw: 32-byte Ed25519 seed
            public_key_hex: hex-encoded Ed25519 public key
            previous_seeds: seeds of keys this node retired (node_identity
                rotation archive), newest first — a friend who has not
                applied the rotation notice yet still encrypts to one of
                them, and that message is not lost.
        """
        if not HAS_NACL:
            raise RuntimeError("PyNaCl required for chat")

        self.db_dsn = db_dsn
        self.public_key_hex = public_key_hex
        self._signing_key = SigningKey(private_key_raw)
        # This node's key and the ones it retired: a message a friend wrote
        # before applying our rotation names one of the retired ones.
        self.own_keys = (public_key_hex,) + tuple(
            SigningKey(s).verify_key.encode().hex() for s in previous_seeds)
        self._curve_private = ed25519_to_curve25519_private(private_key_raw)
        self._previous_curve = [ed25519_to_curve25519_private(s) for s in previous_seeds]
        # Cache Box instances per friend public key
        self._boxes: dict[str, Box] = {}
        # Persistent DB connection (avoids 2s connect overhead per call)
        self._conn = None

    def _get_box(self, friend_public_key_hex: str) -> Box:
        """Get or create NaCl Box for a friend."""
        if friend_public_key_hex not in self._boxes:
            friend_curve_pub = ed25519_to_curve25519_public(friend_public_key_hex)
            self._boxes[friend_public_key_hex] = Box(
                self._curve_private, friend_curve_pub
            )
        return self._boxes[friend_public_key_hex]

    def encrypt_envelope(self, text: str, message_uuid: str, ts_iso: str,
                         friend_public_key_hex: str) -> str:
        """One message of ours boxed for a friend with its uuid, its time and
        our signature over both, the text and the pair inside
        (chat_wire.envelope) — every message this node sends, any path."""
        sig = self._signing_key.sign(chat_wire.message_statement(
            self.public_key_hex, friend_public_key_hex, message_uuid,
            chat_wire.sent_time(ts_iso), text)).signature.hex()
        return self._box(friend_public_key_hex, chat_wire.envelope(text, message_uuid, ts_iso, sig))

    def seal_exported(self, row: dict, friend_public_key_hex: str) -> str:
        """A row of a history export (chat_wire.export_rows), boxed for the
        friend asking: ours signed as we send them, theirs with the
        signature they came with (none on a row stored before messages
        carried one), each under the time it was signed."""
        if row["direction"] == "out":
            return self.encrypt_envelope(row["content"], str(row["message_uuid"]),
                                         row["timestamp"].isoformat(), friend_public_key_hex)
        sig = bytes(row["sender_sig"]).hex() if row["sender_sig"] is not None else None
        sent = row["sent_at"] or row["timestamp"]
        return self._box(friend_public_key_hex, chat_wire.envelope(
            row["content"], str(row["message_uuid"]), sent.isoformat(), sig))

    def _box(self, friend_public_key_hex: str, plaintext: bytes) -> str:
        return base64.b64encode(self._get_box(friend_public_key_hex).encrypt(plaintext)).decode("ascii")

    def decrypt_envelope(self, encrypted_b64: str, sender_public_key_hex: str) -> dict:
        """{uuid, ts, text, sig} of a message a friend boxed — CryptoError for
        a box that does not open, ValueError for one that holds no envelope."""
        return chat_wire.open_envelope(self._open(encrypted_b64, sender_public_key_hex))

    def _open(self, encrypted_b64: str, sender_public_key_hex: str) -> bytes:
        """Tries the live key first, then every retired one: the sender may
        still hold the key this node rotated away from."""
        encrypted = base64.b64decode(encrypted_b64)
        try:
            return self._get_box(sender_public_key_hex).decrypt(encrypted)
        except CryptoError:
            if not self._previous_curve:
                raise
        friend_curve_pub = ed25519_to_curve25519_public(sender_public_key_hex)
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
            return plaintext

    # -------------------------------------------------------------------
    # Friend management (DB operations)
    # -------------------------------------------------------------------

    def _get_db(self):
        import psycopg2
        # Reuse persistent connection (psycopg2.connect takes ~2s on Windows)
        if self._conn is not None:
            try:
                with self._conn.cursor() as cur:
                    cur.execute("SELECT 1")
                return self._conn
            except Exception:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
        conn = psycopg2.connect(self.db_dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET timezone = 'UTC'")
        self._conn = conn
        return conn

    def _return_db(self, conn):
        """No-op: connection is kept alive for reuse."""
        pass

    def add_friend(
        self,
        public_key_hex: str,
        invite_code: str,
        username: str = "",
        display_name: str = "",
        source: Optional[str] = None,
        source_token_id: Optional[str] = None,
    ) -> Optional[int]:
        """Add a friend. Resolves pending entries by invite_code. Returns
        friend ID. `source`/`source_token_id` are set only when passed
        (token-handshake accepts) — None preserves whatever the row has, so
        resolving a guest-side 'master' stub never relabels it. A pending
        entry bound to a fingerprint resolves only for the key it names;
        for any other, nothing is added (None) — the callers check first
        (chat_wire.binds), this holds whoever calls."""
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                # First, try to resolve a pending entry with matching invite_code
                # Either way the peer accepted the key we hold NOW — that is
                # what bound_identity records (see friends in 001).
                cur.execute("""
                    UPDATE friends
                    SET public_key_hex = %s,
                        username = COALESCE(NULLIF(%s, ''), username),
                        display_name = COALESCE(NULLIF(%s, ''), display_name),
                        source = COALESCE(%s::friend_source, source),
                        source_token_id = COALESCE(%s, source_token_id),
                        bound_identity = %s
                    WHERE invite_code = %s AND public_key_hex LIKE 'pending:%%'
                      AND (key_fingerprint IS NULL
                           OR key_fingerprint = substring(sha256(decode(%s, 'hex')) FROM 1 FOR 16))
                    RETURNING id
                """, (public_key_hex, username, display_name, source,
                      source_token_id, self.public_key_hex, invite_code, public_key_hex))
                row = cur.fetchone()
                if row:
                    logger.info(
                        f"Resolved pending friend {invite_code} → "
                        f"{public_key_hex[:16]}..."
                    )
                    return row[0]
                cur.execute("SELECT 1 FROM friends WHERE invite_code = %s"
                            " AND public_key_hex LIKE 'pending:%%'", (invite_code,))
                if cur.fetchone() is not None:
                    logger.warning("A key %s… that the contact %s is not bound to — nothing added",
                                   public_key_hex[:16], invite_code)
                    return None

                # No pending entry — regular upsert by public_key_hex
                cur.execute("""
                    INSERT INTO friends (public_key_hex, invite_code, username,
                                         display_name, source, source_token_id,
                                         bound_identity)
                    VALUES (%s, %s, %s, %s,
                            COALESCE(%s::friend_source, 'manual'), %s, %s)
                    ON CONFLICT (public_key_hex) DO UPDATE
                        SET invite_code = EXCLUDED.invite_code,
                            username = EXCLUDED.username,
                            display_name = COALESCE(NULLIF(EXCLUDED.display_name, ''), friends.display_name),
                            bound_identity = EXCLUDED.bound_identity
                    RETURNING id
                """, (public_key_hex, invite_code, username, display_name,
                      source, source_token_id, self.public_key_hex))
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            self._return_db(conn)

    def remove_friend(self, friend_id: int) -> bool:
        """Remove a friend and all their messages."""
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM friends WHERE id = %s", (friend_id,))
                return cur.rowcount > 0
        finally:
            self._return_db(conn)

    def unbind_friendships(self) -> int:
        """Put every resolved friendship back to `pending:<invite_code>` and
        return how many were reset.

        For when THIS node's own key changed: the peer still binds our invite
        code to the key it accepted, so the friendship is one-sided until we
        introduce ourselves again. `pending:` is the state the resolver
        already knows how to repair, and reclaiming a row is not
        trust-on-first-use — each row is bound to the fingerprint of the key
        it held, so only that peer can resolve it back."""
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE friends
                       SET public_key_hex = 'pending:' || invite_code,
                           key_fingerprint = substring(sha256(decode(public_key_hex, 'hex')) FROM 1 FOR 16)
                     WHERE public_key_hex NOT LIKE 'pending:%'
                       AND invite_code <> ''
                """)
                return cur.rowcount
        finally:
            self._return_db(conn)

    def friends_needing_notice(self) -> list[dict]:
        """Resolved, unblocked friends not known to bind our CURRENT key —
        the rotation notice goes to each until one is accepted. NULL is a
        row from before bound_identity existed: which of our keys it knows
        is found out by offering the chain."""
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, username, public_key_hex, invite_code, bound_identity
                      FROM friends
                     WHERE public_key_hex NOT LIKE 'pending:%%'
                       AND NOT is_blocked
                       AND bound_identity IS DISTINCT FROM %s
                """, (self.public_key_hex,))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            self._return_db(conn)

    def set_bound_identity(self, friend_id: int, pubkey_hex: Optional[str]) -> None:
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE friends SET bound_identity = %s WHERE id = %s",
                            (pubkey_hex, friend_id))
        finally:
            self._return_db(conn)

    def unbind_friend(self, friend_id: int) -> None:
        """One friendship back to `pending:<invite_code>` — for a friend we
        hold no notice for; see unbind_friendships."""
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE friends
                       SET public_key_hex = 'pending:' || invite_code,
                           key_fingerprint = substring(sha256(decode(public_key_hex, 'hex')) FROM 1 FOR 16)
                     WHERE id = %s AND public_key_hex NOT LIKE 'pending:%%'
                       AND invite_code <> ''
                """, (friend_id,))
        finally:
            self._return_db(conn)

    def get_friends(self) -> list[dict]:
        """Get all friends."""
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, username, public_key_hex, invite_code,
                           display_name, added_at, last_seen, is_blocked,
                           source::text AS source,
                           join_token_id::text AS join_token_id, favorite,
                           upper(encode(key_fingerprint, 'hex')) AS key_fingerprint
                    FROM friends
                    ORDER BY display_name, username
                """)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            self._return_db(conn)

    def db_conn(self):
        """The service's persistent DB connection — for invite_tokens calls
        from the sync server (same pool-of-one the chat queries use)."""
        return self._get_db()

    def get_friend_by_public_key(self, public_key_hex: str) -> Optional[dict]:
        """Find a friend by their public key."""
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, username, public_key_hex, invite_code,
                           display_name, is_blocked, source::text AS source
                    FROM friends WHERE public_key_hex = %s
                """, (public_key_hex,))
                row = cur.fetchone()
                if not row:
                    return None
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))
        finally:
            self._return_db(conn)

    def friend_for_key(self, public_key_hex: str) -> Optional[dict]:
        """The friend row a peer speaks for: the resolved row, else a
        `pending:` stub whose invite code binds this key — resolved here.

        The stub is the operator's consent, and the digest inside the
        invite code names the one key that can ever claim it, so a request
        from that key carries everything the classic handshake establishes.
        Waiting for the handshake instead deadlocks a friendship deleted
        and re-added on one side: the peer still holds us resolved and
        never handshakes again, and a node that never initiates (the
        master) keeps the stub forever. A stub carrying a join token stays
        pending — that one resolves through OUR token handshake, which
        burns the use and mints the rights."""
        friend = self.get_friend_by_public_key(public_key_hex)
        if friend is not None:
            return friend
        from desktop.node_identity import verify_invite_code
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT invite_code, upper(encode(key_fingerprint, 'hex')) FROM friends
                    WHERE public_key_hex LIKE 'pending:%'
                      AND join_token_id IS NULL
                """)
                stubs = cur.fetchall()
        finally:
            self._return_db(conn)
        invite = next((code for code, fingerprint in stubs
                       if verify_invite_code(code, public_key_hex)
                       and chat_wire.binds(fingerprint, public_key_hex)), None)
        if invite is None:
            return None
        self.add_friend(public_key_hex=public_key_hex, invite_code=invite)
        logger.info(f"Pending friend {invite} resolved by the peer's own "
                    f"request ({public_key_hex[:16]}...)")
        return self.get_friend_by_public_key(public_key_hex)

    def update_friend_last_seen(self, public_key_hex: str):
        """Update last_seen timestamp for a friend.

        Issues NOTIFY sautium_chat on success so the backend's chat
        SSE wakes its subscribers — the Friends screen otherwise
        only learns about presence changes on its next manual
        refresh."""
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE friends SET last_seen = NOW()
                    WHERE public_key_hex = %s
                """, (public_key_hex,))
                if cur.rowcount > 0:
                    cur.execute("NOTIFY sautium_chat")
        finally:
            self._return_db(conn)

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
        message_uuid: Optional[str] = None,
    ) -> int:
        """Store a chat message (plaintext). Returns message ID."""
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        if message_uuid is None:
            message_uuid = str(uuid.uuid4())
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO p2p_messages
                        (friend_id, direction, content, timestamp, delivered,
                         message_uuid)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (friend_id, direction, content, timestamp, delivered,
                      message_uuid))
                msg_id = cur.fetchone()[0]
                # NOTIFY comes from the trg_p2p_messages_notify trigger —
                # the single wake source for ALL p2p_messages writers.
                return msg_id
        finally:
            self._return_db(conn)

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
                        SELECT id, direction, content, timestamp, delivered,
                               read, message_uuid
                        FROM p2p_messages
                        WHERE friend_id = %s AND id < %s
                        ORDER BY id DESC LIMIT %s
                    """, (friend_id, before_id, limit))
                else:
                    cur.execute("""
                        SELECT id, direction, content, timestamp, delivered,
                               read, message_uuid
                        FROM p2p_messages
                        WHERE friend_id = %s
                        ORDER BY id DESC LIMIT %s
                    """, (friend_id, limit))
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
                rows.reverse()  # chronological order
                return rows
        finally:
            self._return_db(conn)

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
            self._return_db(conn)

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
            self._return_db(conn)

    def get_pending_messages(self) -> list[dict]:
        """Get all undelivered outgoing messages."""
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT m.id, m.friend_id, m.content, m.timestamp,
                           m.message_uuid, f.public_key_hex, f.invite_code
                    FROM p2p_messages m
                    JOIN friends f ON f.id = m.friend_id
                    WHERE m.direction = 'out' AND m.delivered = FALSE
                    ORDER BY m.timestamp
                """)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            self._return_db(conn)

    def mark_delivered(self, message_id: int):
        """Mark an outgoing message as delivered."""
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE p2p_messages SET delivered = TRUE WHERE id = %s
                """, (message_id,))
                if cur.rowcount:
                    cur.execute("NOTIFY sautium_chat")
        finally:
            self._return_db(conn)

    def mark_delivered_by_uuid(self, message_uuid: str):
        """Mark an outgoing message as delivered by its UUID."""
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE p2p_messages SET delivered = TRUE
                    WHERE message_uuid = %s AND direction = 'out'
                """, (message_uuid,))
                if cur.rowcount:
                    cur.execute("NOTIFY sautium_chat")
        finally:
            self._return_db(conn)

    def _stored_message(self, message_uuid: str) -> Optional[tuple]:
        """(friend_id, content) of the message stored under this uuid."""
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT friend_id, content FROM p2p_messages WHERE message_uuid = %s",
                    (message_uuid,),
                )
                return cur.fetchone()
        finally:
            self._return_db(conn)

    # -------------------------------------------------------------------
    # History sync
    # -------------------------------------------------------------------

    def import_history(
        self,
        friend_id: int,
        messages: list[dict],
    ) -> dict:
        """Import messages from a peer's history response.

        Messages have direction from the PEER's perspective, so we flip:
          peer's "in"  → our "out" (we sent it, they received it)
          peer's "out" → our "in"  (they sent it to us)

        A line the export claims as ours is taken only under our own valid
        signature — a friend can write nothing into the thread as written
        by us; one claimed as theirs, under theirs when it carries one (a
        row of theirs from before signatures carries none). Deduplicates by
        message_uuid; marks our outgoing messages as delivered (the peer
        has them).

        Returns: {imported: int, skipped: int, delivered: int}
        """
        imported = 0
        skipped = 0
        delivered = 0

        conn = self._get_db()
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT public_key_hex, previous_public_key_hex FROM friends WHERE id = %s",
                            (friend_id,))
                theirs = tuple(k for k in (cur.fetchone() or ()) if k and not k.startswith("pending:"))
                for msg in messages:
                    msg_uuid = msg.get("message_uuid")
                    if not msg_uuid:
                        skipped += 1
                        continue

                    # Check if we already have this message
                    cur.execute(
                        "SELECT id, direction, delivered, friend_id FROM p2p_messages "
                        "WHERE message_uuid = %s",
                        (msg_uuid,),
                    )
                    existing = cur.fetchone()

                    if existing and existing[3] != friend_id:
                        # A uuid of another thread: this export says
                        # nothing about it, and nothing of it is touched.
                        skipped += 1
                        continue
                    if existing:
                        # We already have it — but if it's our outgoing
                        # message and not yet marked delivered, mark it now
                        # (the peer confirmed they have it)
                        ex_id, ex_dir, ex_delivered, _ = existing
                        if ex_dir == "out" and not ex_delivered:
                            cur.execute(
                                "UPDATE p2p_messages SET delivered = TRUE "
                                "WHERE id = %s",
                                (ex_id,),
                            )
                            delivered += 1
                        skipped += 1
                        continue

                    # Flip direction: peer's perspective → our perspective
                    peer_dir = msg["direction"]
                    our_dir = "out" if peer_dir == "in" else "in"
                    try:
                        sent_at = chat_wire.sent_time(msg["timestamp"])
                    except (TypeError, ValueError):
                        skipped += 1
                        continue
                    sig = msg.get("sig")
                    if our_dir == "out":
                        signed = chat_wire.signed_by(sig, self.own_keys, theirs, msg_uuid,
                                                     sent_at, msg["content"])
                    else:
                        signed = sig is None or chat_wire.signed_by(
                            sig, theirs, self.own_keys, msg_uuid, sent_at, msg["content"])
                    if not signed:
                        skipped += 1
                        continue
                    ts = min(sent_at, datetime.now(timezone.utc))

                    # Fallback dedup for old messages (pre-UUID migration):
                    # check by timestamp + direction + content
                    cur.execute(
                        "SELECT 1 FROM p2p_messages "
                        "WHERE friend_id = %s AND timestamp = %s "
                        "AND direction = %s AND content = %s",
                        (friend_id, ts, our_dir, msg["content"]),
                    )
                    if cur.fetchone():
                        skipped += 1
                        continue

                    is_delivered = True  # both in & out are confirmed
                    kept = our_dir == "in" and sig is not None

                    cur.execute("""
                        INSERT INTO p2p_messages
                            (friend_id, direction, content, timestamp,
                             delivered, message_uuid, sender_sig, sent_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, (friend_id, our_dir, msg["content"], ts,
                          is_delivered, msg_uuid, bytes.fromhex(sig) if kept else None,
                          sent_at if kept else None))
                    imported += 1

            conn.commit()
            return {"imported": imported, "skipped": skipped,
                    "delivered": delivered}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True
            self._return_db(conn)

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
            self._return_db(conn)

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

                # Read OLD public key before overwriting
                cur.execute(
                    "SELECT public_key_hex FROM friends WHERE id = %s",
                    (friend_id,),
                )
                old_row = cur.fetchone()
                old_pubkey = old_row[0] if old_row else None

                # The friend may already be here under the new key: a node
                # that could not deliver its notice re-introduced itself by
                # token, and that made a second friendship. Fold it into the
                # original — the history lives there — before the key moves.
                cur.execute(
                    "SELECT id FROM friends WHERE public_key_hex = %s AND id <> %s",
                    (new_pubkey, friend_id),
                )
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
                """, (new_pubkey, new_invite, friend_id))

                cur.execute("""
                    DELETE FROM pending_key_rotations WHERE friend_id = %s
                """, (friend_id,))

                # Invalidate cached Box for the OLD key (now stale)
                if old_pubkey and old_pubkey in self._boxes:
                    del self._boxes[old_pubkey]

                return True
        finally:
            self._return_db(conn)

    # -------------------------------------------------------------------
    # High-level send/receive
    # -------------------------------------------------------------------

    def prepare_outgoing(self, friend_id: int, content: str) -> Optional[dict]:
        """
        Prepare an outgoing message: encrypt + store locally.

        Returns dict for HTTP sending:
            {from_public_key, encrypted, timestamp, message_uuid}
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
            self._return_db(conn)

        ts = datetime.now(timezone.utc)
        msg_uuid = str(uuid.uuid4())
        encrypted = self.encrypt_envelope(content, msg_uuid, ts.isoformat(), friend_pubkey)

        # Store plaintext locally (not yet delivered)
        self.store_message(
            friend_id, "out", content, ts,
            delivered=False, message_uuid=msg_uuid,
        )

        return {
            "from_public_key": self.public_key_hex,
            "encrypted": encrypted,
            "timestamp": ts.isoformat(),
            "message_uuid": msg_uuid,
        }

    def handle_incoming(self, sender_public_key: str, encrypted_b64: str,
                        timestamp_iso: str,
                        message_uuid: Optional[str] = None) -> Optional[dict]:
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
        if len(encrypted_b64) > MAX_ENCRYPTED_CHARS:
            logger.warning(f"Oversized ciphertext from {friend.get('username', '?')} rejected")
            return None

        try:
            env = self.decrypt_envelope(encrypted_b64, sender_public_key)
        except (CryptoError, ValueError):
            logger.error(f"Failed to open a message from {sender_public_key[:16]}...")
            return None
        content = env["text"]
        # The uuid and the time are the box's: the one the request names
        # must agree, and neither can be another's (chat_wire.envelope).
        if message_uuid and message_uuid != env["uuid"]:
            logger.warning(f"Message from {friend.get('username', '?')} names another "
                           f"uuid than its box — rejected")
            return None
        if len(content) > MAX_PLAINTEXT_CHARS:
            logger.warning(f"Oversized plaintext from {friend.get('username', '?')} rejected")
            return None
        # Its sender signed it, for this node, as boxed — what lets the row
        # go back to them in a history export and stand there as theirs.
        try:
            sent_at = chat_wire.sent_time(env["ts"])
        except ValueError:
            sent_at = None
        if sent_at is None or not chat_wire.signed_by(
                env["sig"], (sender_public_key,), self.own_keys, env["uuid"], sent_at, content):
            logger.warning(f"Message from {friend.get('username', '?')} without its sender's "
                           f"signature — rejected")
            return None

        # One message per uuid in a thread: the same message again is a
        # duplicate (and answered so); anything else under its uuid — or a
        # uuid another thread holds — is refused.
        ts = _sender_timestamp(env["ts"])
        stored = self._stored_message(env["uuid"])
        if stored is None:
            if self._store_incoming(friend["id"], content, ts, env["uuid"], env["sig"], sent_at):
                self.update_friend_last_seen(sender_public_key)
                return {"friend_id": friend["id"], "content": content, "timestamp": ts}
            stored = self._stored_message(env["uuid"])      # another copy of it landed first
        if stored[0] == friend["id"] and stored[1] == content:
            logger.debug(f"Duplicate message {env['uuid'][:8]}, skipping")
            return {"friend_id": friend["id"], "content": "", "timestamp": None,
                    "duplicate": True}
        logger.warning(f"Message {env['uuid'][:8]} from {friend.get('username', '?')} "
                       f"reuses a uuid — rejected")
        return None

    def _store_incoming(self, friend_id: int, content: str, ts: datetime,
                        message_uuid: str, sig: str, sent_at: datetime) -> bool:
        """An incoming message stored with its sender's signature and the
        time it signed, False when its uuid already is — two copies of one
        message side by side (mirrors backend/p2p_chat)."""
        conn = self._get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO p2p_messages
                        (friend_id, direction, content, timestamp, delivered, message_uuid,
                         sender_sig, sent_at)
                    VALUES (%s, 'in', %s, %s, TRUE, %s, %s, %s)
                    ON CONFLICT (message_uuid) WHERE message_uuid IS NOT NULL DO NOTHING
                    RETURNING id
                """, (friend_id, content, ts, message_uuid, bytes.fromhex(sig), sent_at))
                return cur.fetchone() is not None
        finally:
            self._return_db(conn)
