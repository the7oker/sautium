"""
Email verification and signed invite delivery for Sautium.

Uses the Cloudflare Worker as a trusted CA:
- Verification: the WORKER generates the code, emails it and remembers its
  hash; the client sends the user-entered code back at registration. Mailbox
  ownership is proven to the verifier, never asserted by the client.
- Registration requires a birth certificate for the same identity — the
  Worker stores {email, pubkey, born_at, verified_at}, so a verified badge
  can cite identity age.
- Invite: signed request → Worker verifies signature → sends email with
  verified badge.

All state-changing requests are signed with the user's Ed25519 private key.
The Worker verifies the signature, so a modified client can't impersonate
others.
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

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


def _sign_message(message: str) -> str:
    """Sign a message with the node's Ed25519 key. Returns hex signature."""
    from desktop.node_identity import sign_message
    sig_bytes = sign_message(message.encode("utf-8"))
    return sig_bytes.hex()


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
    if not ctx:
        return False

    message = f"check-email:{ctx['invite_code']}:{email}"
    result = _get_worker("/check-email", {
        "invite_code": ctx["invite_code"],
        "email": email,
        "public_key_hex": ctx["public_key_hex"],
        "signature": ctx["sign"](message.encode("utf-8")).hex(),
    })
    if result and result.get("verified"):
        logger.info(f"Email {email} already verified for {ctx['invite_code']}")
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

    message = f"sendcode:{ctx['invite_code']}:{to_email}"
    result = _post_worker("/send-verification", {
        "to": to_email,
        "invite_code": ctx["invite_code"],
        "public_key_hex": ctx["public_key_hex"],
        "signature": ctx["sign"](message.encode("utf-8")).hex(),
        "from_username": from_username,
    })
    if result and result.get("status") == "sent":
        logger.info(f"Verification email sent to {to_email}")
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

    from desktop.p2p import birth_cert
    cert = birth_cert.load_certificate()
    if cert is None or cert["pubkey"] != ctx["public_key_hex"].lower():
        cert = birth_cert.request_certificate(
            ctx["public_key_hex"], ctx["sign"])
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
        logger.info(f"Email {email} registered for {ctx['invite_code']}")
        # The Worker upgraded our record to method:email — persist the new
        # certificate now instead of discovering it on the next re-fetch.
        if result.get("birth_cert"):
            birth_cert.save_certificate(result["birth_cert"])
        return True
    return False


# -------------------------------------------------------------------
# Invite (send signed invite to someone)
# -------------------------------------------------------------------

def send_invite_email(
    to_email: str,
    message: str = "",
) -> bool:
    """
    Send a signed invite email. Worker includes verified sender badge.

    Returns True if sent.
    """
    identity = _get_identity()
    if not identity:
        logger.error("No account — can't send invite")
        return False

    invite_code = identity["invite_code"]
    public_key_hex = identity["public_key_hex"]

    sig_message = f"invite:{invite_code}:to:{to_email}"
    signature = _sign_message(sig_message)

    result = _post_worker("/send-invite", {
        "to": to_email,
        "invite_code": invite_code,
        "public_key_hex": public_key_hex,
        "signature": signature,
        "message": message,
    })
    if result and result.get("status") == "sent":
        verified = result.get("verified_sender", False)
        logger.info(
            f"Invite sent to {to_email} "
            f"(verified={verified})"
        )
        return True
    return False


# -------------------------------------------------------------------
# Accept invite (auto-reciprocate)
# -------------------------------------------------------------------

def accept_invite(their_invite_code: str) -> Optional[dict]:
    """
    Notify the Worker that we accepted someone's invite.

    The Worker stores the acceptance and sends our invite code to the
    sender's verified email (if available).

    Returns {"status": "accepted", "notified": bool} or None on failure.
    """
    identity = _get_identity()
    if not identity:
        logger.error("No account — can't accept invite")
        return None

    my_invite = identity["invite_code"]
    public_key_hex = identity["public_key_hex"]

    message = f"accept:{my_invite}:{their_invite_code}"
    signature = _sign_message(message)

    result = _post_worker("/accept-invite", {
        "my_invite_code": my_invite,
        "their_invite_code": their_invite_code,
        "public_key_hex": public_key_hex,
        "signature": signature,
    })
    if result and result.get("status") == "accepted":
        notified = result.get("notified", False)
        logger.info(
            f"Accepted invite from {their_invite_code} "
            f"(sender notified={notified})"
        )
        return result
    return None


def check_pending_accepts() -> list[dict]:
    """
    Poll the Worker for invite codes that accepted our invites.

    Returns list of {"invite_code": "...", "accepted_at": "..."}.
    Worker deletes entries after pickup (one-time).
    """
    identity = _get_identity()
    if not identity:
        return []

    my_invite = identity["invite_code"]
    public_key_hex = identity["public_key_hex"]

    message = f"pending-accepts:{my_invite}"
    signature = _sign_message(message)

    result = _get_worker("/pending-accepts", {
        "invite_code": my_invite,
        "public_key_hex": public_key_hex,
        "signature": signature,
    })
    if result and "accepts" in result:
        accepts = result["accepts"]
        if accepts:
            logger.info(f"Pending accepts: {len(accepts)} new")
        return accepts
    return []
