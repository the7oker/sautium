"""
Email verification and signed invite delivery for Sautium.

Uses the Cloudflare Worker as a trusted CA:
- Verification: code sent to email → user enters code → email registered on Worker
- Invite: signed request → Worker verifies signature → sends email with verified badge

All state-changing requests are signed with the user's Ed25519 private key.
The Worker verifies the signature, so a modified client can't impersonate others.
"""

import json
import logging
import secrets
import string
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

VERIFY_WORKER_URL = "https://sautium-verify.sautium.workers.dev"

_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Sautium/1.0",
}


def generate_code(length: int = 6) -> str:
    """Generate a random alphanumeric verification code."""
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


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


# -------------------------------------------------------------------
# Verification (prove email ownership)
# -------------------------------------------------------------------

def send_verification_email(
    to_email: str,
    code: str,
    from_username: str = "",
    invite_code: str = "",
) -> bool:
    """Send a verification code email. Returns True if sent."""
    result = _post_worker("/send-verification", {
        "to": to_email,
        "code": code,
        "from_username": from_username,
        "invite_code": invite_code,
    })
    if result and result.get("status") == "sent":
        logger.info(f"Verification email sent to {to_email}")
        return True
    return False


def register_verified_email(email: str) -> bool:
    """
    Register verified email mapping on the Worker (after code confirmed).

    Signs the request so the Worker can verify we own this invite code.
    Returns True if registered.
    """
    identity = _get_identity()
    if not identity:
        logger.error("No account — can't register email")
        return False

    invite_code = identity["invite_code"]
    public_key_hex = identity["public_key_hex"]

    message = f"register:{invite_code}:{email}"
    signature = _sign_message(message)

    result = _post_worker("/register-email", {
        "invite_code": invite_code,
        "email": email,
        "public_key_hex": public_key_hex,
        "signature": signature,
    })
    if result and result.get("status") == "registered":
        logger.info(f"Email {email} registered for {invite_code}")
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
# Verification handshake (for wizard)
# -------------------------------------------------------------------

class EmailVerification:
    """Manages email verification flow in the wizard."""

    def __init__(self, my_username: str, my_invite_code: str):
        self.my_username = my_username
        self.my_invite_code = my_invite_code
        self._my_code: Optional[str] = None
        self.verified = False

    def start(self, email: str) -> bool:
        """Send verification code to email. Returns True if sent."""
        self._my_code = generate_code()
        return send_verification_email(
            to_email=email,
            code=self._my_code,
            from_username=self.my_username,
            invite_code=self.my_invite_code,
        )

    def verify_code(self, entered_code: str) -> bool:
        """Check if entered code matches. Returns True if valid."""
        if not self._my_code:
            return False
        self.verified = entered_code.strip().upper() == self._my_code
        return self.verified
