"""
Email verification and signed invite delivery for Sautium.

Uses the Cloudflare Worker as a trusted CA:
- Verification: the WORKER generates the code, emails it and remembers its
  hash; the client sends the user-entered code back at registration. Mailbox
  ownership is proven to the verifier, never asserted by the client.
- Registration requires a birth certificate for the same identity — the
  Worker stores {email, pubkey, born_at, verified_at}, so a verified badge
  can cite identity age.
- Invite: the Web UI's route (backend/routers/p2p.py) signs the request;
  the Worker mails a single-use token under the verified badge. The
  inviter's pickup (pick_up_acceptances) adds whoever accepted.
- Every route is a key's own code's, and the Worker answers only a key it
  issued a certificate to: the wizard asks for it before its first request.

All state-changing requests are signed with the user's Ed25519 private key.
The Worker verifies the signature, so a modified client can't impersonate
others.
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from desktop.p2p import chat_wire

logger = logging.getLogger(__name__)

VERIFY_WORKER_URL = "https://sautium-verify.sautium.workers.dev"

_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Sautium/1.0",
}


def _post_worker(path: str, payload: dict) -> Optional[dict]:
    """POST to the Worker API. Returns response dict or None on failure."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{VERIFY_WORKER_URL}{path}",
            method="POST",
            data=data,
            headers=_HEADERS,
        )
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error(f"Worker {path} failed ({e.code}): {body}")
        return None
    except Exception as e:
        logger.error(f"Worker {path} failed: {e}")
        return None


def _get_identity() -> Optional[dict]:
    """Get account info (invite_code, public_key_hex)."""
    from desktop.node_identity import get_account_info
    return get_account_info()


def _get_worker(path: str, params: dict) -> Optional[dict]:
    """GET from the Worker API with query params. Returns response dict or None."""
    try:
        query = urllib.parse.urlencode(params)
        url = f"{VERIFY_WORKER_URL}{path}?{query}"
        req = urllib.request.Request(url, method="GET", headers=_HEADERS)
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error(f"Worker GET {path} failed ({e.code}): {body}")
        return None
    except Exception as e:
        logger.error(f"Worker GET {path} failed: {e}")
        return None


# -------------------------------------------------------------------
# Identity context (saved account OR wizard-derived credentials)
# -------------------------------------------------------------------

def _identity_ctx(username: str = "", password: str = "") -> Optional[dict]:
    """Signing context {invite_code, public_key_hex, sign(bytes)->bytes}.

    With username+password, derives the identity in memory (wizard context —
    account not saved yet); otherwise uses the saved account. The derivation
    is deterministic, so both paths speak for the same identity.
    """
    if username and password:
        from desktop.node_identity import derive_account_identity
        private_key, public_key_hex, invite_code = derive_account_identity(
            username, password)
        return {
            "invite_code": invite_code,
            "public_key_hex": public_key_hex,
            "sign": private_key.sign,
        }

    identity = _get_identity()
    if not identity:
        return None
    from desktop.node_identity import sign_message
    return {
        "invite_code": identity["invite_code"],
        "public_key_hex": identity["public_key_hex"],
        "sign": sign_message,
    }


def _born(ctx: dict) -> Optional[dict]:
    """This identity's certificate — the stored one, else asked for: a new
    account's wizard verifies its email before the launcher's first start,
    and the Worker answers only a key it issued a certificate to."""
    from desktop.p2p import birth_cert
    cert = birth_cert.load_certificate()
    if cert is None or cert["pubkey"] != ctx["public_key_hex"].lower():
        cert = birth_cert.request_certificate(ctx["public_key_hex"], ctx["sign"])
    return cert


# -------------------------------------------------------------------
# Check if email already verified
# -------------------------------------------------------------------

def is_email_already_verified(
    email: str,
    username: str = "",
    password: str = "",
) -> bool:
    """
    Check if this email is already verified on the Worker for our invite code.

    If the user reinstalls with the same username+password, the Worker
    still has the mapping — no need to re-verify.
    """
    ctx = _identity_ctx(username, password)
    if not ctx or not _born(ctx):
        return False

    message = f"check-email:{ctx['invite_code']}:{email}"
    result = _get_worker("/check-email", {
        "invite_code": ctx["invite_code"],
        "email": email,
        "public_key_hex": ctx["public_key_hex"],
        "signature": ctx["sign"](message.encode("utf-8")).hex(),
    })
    if result and result.get("verified"):
        logger.info("Email already verified for this identity")
        if result.get("birth_cert"):
            from desktop.p2p import birth_cert
            birth_cert.save_certificate(result["birth_cert"])
        return True
    return False


# -------------------------------------------------------------------
# Verification (prove email ownership — code lives on the Worker)
# -------------------------------------------------------------------

def send_verification_email(
    to_email: str,
    username: str = "",
    password: str = "",
    from_username: str = "",
) -> bool:
    """Ask the Worker to generate and email a verification code.

    The code never exists client-side: the Worker stores its hash (15 min
    TTL) and checks it at register_verified_email(). Returns True if sent.
    """
    ctx = _identity_ctx(username, password)
    if not ctx:
        logger.error("No identity — can't request verification email")
        return False
    if not _born(ctx):
        logger.error("No birth certificate — can't request verification email")
        return False

    message = f"sendcode:{ctx['invite_code']}:{to_email}"
    result = _post_worker("/send-verification", {
        "to": to_email,
        "invite_code": ctx["invite_code"],
        "public_key_hex": ctx["public_key_hex"],
        "signature": ctx["sign"](message.encode("utf-8")).hex(),
        "from_username": from_username,
    })
    if result and result.get("status") == "sent":
        logger.info("Verification email sent")
        return True
    return False


def register_verified_email(
    email: str,
    code: str,
    username: str = "",
    password: str = "",
) -> bool:
    """
    Register the verified email on the Worker.

    Sends the user-entered code (the Worker compares against its stored
    hash — mailbox ownership is proven server-side) plus this identity's
    identity certificate (fetched from the Worker first when missing; the
    stored record carries born_at). Returns True if registered.
    """
    ctx = _identity_ctx(username, password)
    if not ctx:
        logger.error("No identity — can't register email")
        return False

    cert = _born(ctx)
    if cert is None:
        logger.error("No birth certificate — can't register email")
        return False

    message = f"register:{ctx['invite_code']}:{email}"
    result = _post_worker("/register-email", {
        "invite_code": ctx["invite_code"],
        "email": email,
        "public_key_hex": ctx["public_key_hex"],
        "signature": ctx["sign"](message.encode("utf-8")).hex(),
        "code": code,
        "birth_cert": cert,
    })
    if result and result.get("status") == "registered":
        logger.info("Email registered for this identity")
        # The Worker upgraded our record to method:email — persist the new
        # certificate now instead of discovering it on the next re-fetch.
        if result.get("birth_cert"):
            birth_cert.save_certificate(result["birth_cert"])
        return True
    return False


# -------------------------------------------------------------------
# The inviter's pickup (one function, three triggers: the Friends screen on
# either runtime, the launcher's start, a peer's nudge)
# -------------------------------------------------------------------

# Accepted invites one pickup takes (the Worker's page).
PICKUP_PAGE = 300


def has_open_invites(conn) -> bool:
    """An invite this node mailed whose acceptance it has not taken, young
    enough for the Worker to still hold it (chat_wire.INVITE_PICKUP_DAYS)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 1 FROM sent_invites
             WHERE invite_id IS NOT NULL AND taken_at IS NULL
               AND sent_at > now() - make_interval(days => %s)
             LIMIT 1
        """, (chat_wire.INVITE_PICKUP_DAYS,))
        return cur.fetchone() is not None


def is_open_invite(conn, invite_id: str) -> bool:
    """`invite_id` is one of this node's open invites (has_open_invites)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 1 FROM sent_invites
             WHERE invite_id = %s AND taken_at IS NULL
               AND sent_at > now() - make_interval(days => %s)
        """, (invite_id, chat_wire.INVITE_PICKUP_DAYS))
        return cur.fetchone() is not None


def pick_up_acceptances(conn, invite_code: str, public_key_hex: str, sign) -> list[str]:
    """Ask the Worker for this node's accepted invites and add every one
    that holds up (chat_wire.take_acceptance), each in a transaction of its
    own → the codes of the contacts added. Only while an invite is open;
    the Worker deletes nothing, so a node of the same account, a restore or
    a pickup lost on its way loses nothing. `sign(bytes) -> bytes` is this
    node's key; `conn` a connection the caller holds."""
    if not has_open_invites(conn):
        return []
    ts = int(time.time())
    result = _post_worker("/pending-accepts", {
        "invite_code": invite_code,
        "public_key_hex": public_key_hex,
        "ts": ts,
        "signature": sign(chat_wire.pickup_text(ts, invite_code, public_key_hex).encode("utf-8")).hex(),
    })
    accepts = result.get("accepts") if isinstance(result, dict) else None
    if not isinstance(accepts, list):
        return []
    added = []
    for accept in accepts[:PICKUP_PAGE]:
        # One acceptance's failure is its own: the rest of the page, and the
        # sweep below, go on — but not past a connection that is gone.
        try:
            outcome = _in_transaction(conn, lambda: chat_wire.take_acceptance(
                conn, accept, invite_code, public_key_hex))
        except Exception:
            if conn.closed:
                raise
            logger.exception("Pickup: an acceptance failed")
            continue
        if outcome == "added":
            added.append(accept["acceptor"]["invite_code"])
            logger.info("Pickup: the invite accepted by %s added", accept["acceptor"]["invite_code"])
        elif outcome not in ("taken", "held", "not an invite of this node"):
            logger.warning("Pickup: an acceptance skipped — %s", outcome)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM sent_invites WHERE sent_at < now() - make_interval(days => %s)",
                    (chat_wire.INVITE_PICKUP_DAYS,))
    return added


def _in_transaction(conn, step):
    """`step()` committed as one transaction, whatever mode `conn` is in."""
    autocommit = conn.autocommit
    conn.autocommit = False
    try:
        outcome = step()
        conn.commit()
        return outcome
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.autocommit = autocommit
