"""
Email verification for P2P friend discovery.

Proves email ownership by exchanging verification codes via email.
Uses the Sautium verification worker (Cloudflare) to send emails.

Flow (both online):
  1. User1 wants to add User2 by email
  2. Both apps generate random 6-char codes
  3. Each app sends their code to the OTHER person's email via worker
  4. Each user reads their email, enters the code in the app
  5. Apps exchange entered codes via P2P connection
  6. Each app verifies the code matches what they generated
  7. Verified → add as friends

Flow (invitee offline):
  1. User1 sends an invite email to user2@email.com via worker
  2. Email contains project description + invite code + download link
  3. User2 installs Sautium, adds User1 by invite code
"""

import logging
import secrets
import string
import urllib.error
import urllib.request
import json
from typing import Optional

logger = logging.getLogger(__name__)

VERIFY_WORKER_URL = "https://sautium-verify.sautium.workers.dev"


def generate_code(length: int = 6) -> str:
    """Generate a random alphanumeric verification code."""
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def send_verification_email(
    to_email: str,
    code: str,
    from_username: str = "",
    invite_code: str = "",
) -> bool:
    """
    Send a verification code email via the Sautium worker.

    Returns True if sent successfully.
    """
    payload = {
        "to": to_email,
        "code": code,
        "from_username": from_username,
        "invite_code": invite_code,
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{VERIFY_WORKER_URL}/send-verification",
            method="POST",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read().decode("utf-8"))
        if result.get("status") == "sent":
            logger.info(f"Verification email sent to {to_email}")
            return True
        logger.warning(f"Verification send unexpected: {result}")
        return False
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error(f"Verification email failed ({e.code}): {body}")
        return False
    except Exception as e:
        logger.error(f"Verification email failed: {e}")
        return False


def send_invite_email(
    to_email: str,
    from_username: str,
    invite_code: str,
    message: str = "",
) -> bool:
    """
    Send an invite email to someone not yet on Sautium.

    Returns True if sent successfully.
    """
    payload = {
        "to": to_email,
        "from_username": from_username,
        "invite_code": invite_code,
        "message": message,
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{VERIFY_WORKER_URL}/send-invite",
            method="POST",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read().decode("utf-8"))
        if result.get("status") == "sent":
            logger.info(f"Invite email sent to {to_email}")
            return True
        return False
    except Exception as e:
        logger.error(f"Invite email failed: {e}")
        return False


class EmailVerification:
    """Manages the email verification handshake between two peers."""

    def __init__(self, my_username: str, my_invite_code: str):
        self.my_username = my_username
        self.my_invite_code = my_invite_code
        # Code I generated and sent to the other party
        self._my_code: Optional[str] = None
        # Code the other party sent to me (entered by user from email)
        self._their_code: Optional[str] = None
        self.verified = False

    def start(self, their_email: str) -> bool:
        """
        Start verification: generate code and send to their email.

        Returns True if email was sent.
        """
        self._my_code = generate_code()
        return send_verification_email(
            to_email=their_email,
            code=self._my_code,
            from_username=self.my_username,
            invite_code=self.my_invite_code,
        )

    def enter_code(self, code: str):
        """User enters the code they received in their email."""
        self._their_code = code.strip().upper()

    def get_code_for_peer(self) -> Optional[str]:
        """Get the code entered by user (to send back to peer for verification)."""
        return self._their_code

    def verify_returned_code(self, returned_code: str) -> bool:
        """
        Verify the code returned by the peer matches what we sent.

        If it matches, the peer proved they have access to their email.
        """
        if not self._my_code:
            return False
        self.verified = returned_code.strip().upper() == self._my_code
        return self.verified
