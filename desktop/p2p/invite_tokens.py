"""Invite tokens + signed friendship grants.

An invite token is a bearer secret (a random UUID) minted by this node and
shared as the third segment of `username#XXXX-XXXX-XXXX#<token>`. Presenting
a live token in a chat handshake auto-adds the presenter as a friend: the
token AUTHORIZES the friendship, while the invite-code<->pubkey binding still
IDENTIFIES the presenter — a stolen share string cannot impersonate anyone.

Every accept mints a GRANT signed by the issuer's account key. The guest
stores it and re-presents it to recover the friendship after the issuer
moves devices: the issuer's friends table is gone, but its deterministic
identity still verifies its own past signature — the grant replaces the
lost database row as proof.

Timestamps in the signed payload are unix epoch seconds (ints), never ISO
strings: TIMESTAMPTZ round-trips through arbitrary session timezones do not
reproduce ISO text byte-for-byte, and signatures need determinism.

MIRRORED: desktop/p2p/invite_tokens.py <-> backend/invite_tokens.py (the
Docker image builds from backend/ only). Update both together.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

GRANT_VERSION = 1
ALL_RIGHTS = ("can_message", "can_search")


# ---------------------------------------------------------------------------
# Grant: canonical payload + sign/verify (birth-certificate pattern)
# ---------------------------------------------------------------------------

def grant_payload(token_id: str, rights: list, guest_pubkey_hex: str,
                  issued_at: int, expires_at: Optional[int]) -> bytes:
    rights_csv = ",".join(sorted(rights))
    return (f"sautium-grant:v{GRANT_VERSION}:{token_id}:{rights_csv}:"
            f"{guest_pubkey_hex}:{issued_at}:{expires_at or ''}"
            ).encode("utf-8")


def sign_grant(private_key, token_id: str, rights: list,
               guest_pubkey_hex: str, issuer_pubkey_hex: str,
               expires_at: Optional[int] = None) -> dict:
    """Mint the wire-format grant for one accepted guest."""
    import time
    issued_at = int(time.time())
    sig = private_key.sign(grant_payload(
        token_id, rights, guest_pubkey_hex, issued_at, expires_at))
    return {
        "v": GRANT_VERSION,
        "token_id": token_id,
        "rights": sorted(rights),
        "guest_pubkey": guest_pubkey_hex,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "issuer_pubkey": issuer_pubkey_hex,
        "sig": sig.hex(),
    }


def verify_grant(grant: dict, issuer_pubkey_hex: str) -> bool:
    """Offline check that a grant was signed by `issuer_pubkey_hex` and has
    not expired. The caller decides WHOSE key that must be: its own on the
    recovery path, the responder's on the guest path."""
    import time

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        if grant.get("v") != GRANT_VERSION:
            return False
        if grant.get("issuer_pubkey", "").lower() != issuer_pubkey_hex.lower():
            return False
        expires_at = grant.get("expires_at")
        if expires_at and int(expires_at) < time.time():
            return False
        authority = Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(issuer_pubkey_hex))
        authority.verify(
            bytes.fromhex(grant["sig"]),
            grant_payload(grant["token_id"], grant["rights"],
                          grant["guest_pubkey"], int(grant["issued_at"]),
                          expires_at and int(expires_at)))
        return True
    except (InvalidSignature, KeyError, ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Issuer side (psycopg2 connection, autocommit)
# ---------------------------------------------------------------------------

def consume_token(conn, token_id: str) -> Optional[dict]:
    """Atomically burn one use of a live token.

    Expiry, revocation and max_uses are all decided by the single UPDATE, so
    concurrent handshakes cannot oversubscribe. Returns {rights,
    welcome_message, require_birth_cert} or None for a dead/unknown token.
    """
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE invite_tokens
               SET use_count = use_count + 1
             WHERE id = %s
               AND revoked_at IS NULL
               AND (expires_at IS NULL OR expires_at > NOW())
               AND (max_uses IS NULL OR use_count < max_uses)
         RETURNING welcome_message, require_birth_cert
        """, (token_id,))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute("SELECT p2p_right FROM invite_token_rights"
                    " WHERE token_id = %s", (token_id,))
        return {
            "welcome_message": row[0],
            "require_birth_cert": row[1],
            "rights": sorted(r[0] for r in cur.fetchall()),
        }


def token_rights(conn, token_id: str) -> list:
    """Current rights of a token WITHOUT burning a use — the retry path of
    an interrupted accept heals the snapshot from here (the use was already
    burned by the interrupted attempt)."""
    with conn.cursor() as cur:
        cur.execute("SELECT p2p_right FROM invite_token_rights"
                    " WHERE token_id = %s", (token_id,))
        return sorted(r[0] for r in cur.fetchall())


def token_revoked(conn, token_id: str) -> Optional[bool]:
    """None = no such token locally (fresh device), else its revoked state.
    The grant-recovery path honors a same-device revocation record but
    accepts on signature alone when the row never existed here."""
    with conn.cursor() as cur:
        cur.execute("SELECT revoked_at IS NOT NULL FROM invite_tokens"
                    " WHERE id = %s", (token_id,))
        row = cur.fetchone()
        return None if row is None else row[0]


def snapshot_rights(conn, friend_id: int, rights: list) -> None:
    """Pin what this friendship was granted; token edits affect future uses
    only."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM friend_rights WHERE friend_id = %s",
                    (friend_id,))
        for r in rights:
            cur.execute(
                "INSERT INTO friend_rights (friend_id, p2p_right)"
                " VALUES (%s, %s) ON CONFLICT DO NOTHING", (friend_id, r))


def friend_rights(conn, friend_id: int) -> list:
    with conn.cursor() as cur:
        cur.execute("SELECT p2p_right FROM friend_rights WHERE friend_id = %s",
                    (friend_id,))
        return sorted(r[0] for r in cur.fetchall())


def friend_has_right(conn, friend_pubkey_hex: str, right: str) -> bool:
    """The enforcement query: token-sourced friends need an explicit granted
    right; mutual-consent friendships keep full implicit rights."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT f.source != 'token'
                   OR EXISTS (SELECT 1 FROM friend_rights fr
                              WHERE fr.friend_id = f.id AND fr.p2p_right = %s)
              FROM friends f
             WHERE f.public_key_hex = %s
        """, (right, friend_pubkey_hex))
        row = cur.fetchone()
        return bool(row and row[0])


# ---------------------------------------------------------------------------
# Guest side (psycopg2 connection, autocommit)
# ---------------------------------------------------------------------------

def store_grant(conn, friend_id: int, grant: dict) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO friend_grants (friend_id, token_id, issuer_pubkey_hex,
                                       issued_at, expires_at, signature)
            VALUES (%s, %s, %s, to_timestamp(%s), to_timestamp(%s), %s)
            ON CONFLICT (friend_id) DO UPDATE SET
                token_id = EXCLUDED.token_id,
                issuer_pubkey_hex = EXCLUDED.issuer_pubkey_hex,
                issued_at = EXCLUDED.issued_at,
                expires_at = EXCLUDED.expires_at,
                signature = EXCLUDED.signature
        """, (friend_id, grant["token_id"], grant["issuer_pubkey"],
              int(grant["issued_at"]), grant.get("expires_at"),
              bytes.fromhex(grant["sig"])))
        cur.execute("DELETE FROM friend_grant_rights WHERE friend_id = %s",
                    (friend_id,))
        for r in grant.get("rights", []):
            cur.execute(
                "INSERT INTO friend_grant_rights (friend_id, p2p_right)"
                " VALUES (%s, %s) ON CONFLICT DO NOTHING", (friend_id, r))


def load_grant(conn, friend_id: int, own_pubkey_hex: str) -> Optional[dict]:
    """Rebuild the wire-format grant from stored fields. `own_pubkey_hex` is
    the grant subject — on the guest side the grant is always for this node,
    so the subject is not stored."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT g.token_id, g.issuer_pubkey_hex,
                   EXTRACT(EPOCH FROM g.issued_at)::bigint,
                   EXTRACT(EPOCH FROM g.expires_at)::bigint,
                   g.signature,
                   COALESCE(array_agg(r.p2p_right::text ORDER BY r.p2p_right)
                            FILTER (WHERE r.p2p_right IS NOT NULL), '{}')
              FROM friend_grants g
              LEFT JOIN friend_grant_rights r USING (friend_id)
             WHERE g.friend_id = %s
             GROUP BY g.friend_id, g.token_id, g.issuer_pubkey_hex,
                      g.issued_at, g.expires_at, g.signature
        """, (friend_id,))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "v": GRANT_VERSION,
            "token_id": str(row[0]),
            "rights": list(row[5]),
            "guest_pubkey": own_pubkey_hex,
            "issued_at": int(row[2]),
            "expires_at": row[3] and int(row[3]),
            "issuer_pubkey": row[1],
            "sig": bytes(row[4]).hex(),
        }
