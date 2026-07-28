"""Browser authentication: how a device earns the right to sign requests.

Before this, `GET /` inlined the shared HMAC secret into the page, so the key
was handed to anyone who could load it — every device on the LAN, and via DNS
rebinding any page the owner opened. A key that is published is not
authentication; it is obfuscation with extra steps. It also had no TTL, no
revocation and no notion of a device.

Now the page carries nothing. A browser signs with a DEVICE TOKEN it obtains
once, by proving one of two things:

  * knowledge of the account password — re-deriving the Argon2id identity and
    checking that it yields this node's public key. Nothing about the password
    is stored anywhere, not even a hash: the account IS the derivation.
  * possession of a pairing PIN shown on the host (launcher Settings), for
    the anonymous accounts the wizard creates with a random password its owner
    has never seen.

The token is a pure function of a server-side EPOCH, so "log out everywhere"
is `epoch += 1` — one integer, no device table. That is a deliberate trade:
individual devices cannot be revoked separately, which for a single-user
appliance is worth the simplicity. Changing the password bumps the epoch too
(a password change must cut off whoever knew the old one).

Storing the token client-side has a second effect worth noting: localStorage
is bound to an origin, so a rebinding attacker on evil.com opens *their own*
empty storage. The vector that made the inlined secret reachable from the
internet closes on its own.

BRUTE FORCE. The two doors need different locks:
  * PIN — small enough to guess, so it is one-shot, expires in minutes, dies
    after MAX_PIN_ATTEMPTS, and every check passes through a lock. Sleeping
    between attempts would be useless: a thousand parallel requests sleep
    concurrently. Serialising them is what makes attempts countable.
  * password — Argon2id at 256 MB already makes guessing hopeless (a thousand
    parallel attempts would need 250 GB). The danger runs the other way: that
    same cost turns an unbounded login endpoint into a memory DoS, so
    derivations run under a semaphore.
"""

import asyncio
import hmac
import logging
import secrets
import time
from typing import Optional

logger = logging.getLogger(__name__)

_EPOCH_KEY = "auth.token_epoch"

# Unambiguous alphabet (no O/0, I/1) — the PIN is read off a screen and typed
# on a phone. Length is insurance, not the defence: MAX_PIN_ATTEMPTS is.
_PIN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PIN_GROUPS = 2
PIN_GROUP_LEN = 4
PIN_TTL_SECONDS = 5 * 60
MAX_PIN_ATTEMPTS = 5

# One Argon2id derivation is ~256 MB; two concurrent is already half a gig.
# Everything else waits rather than racing the container into an OOM kill.
_derive_semaphore = asyncio.Semaphore(2)
_pin_lock = asyncio.Lock()

_pin: Optional[dict] = None          # {"code", "expires_at", "attempts"}


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------

def _epoch() -> int:
    from routers.settings import _read
    try:
        return int(_read(_EPOCH_KEY) or 0)
    except (TypeError, ValueError):
        return 0


def current_token(server_secret: bytes) -> str:
    """The token every paired browser signs with. Derived, not stored — the
    server can always recompute it, and bumping the epoch invalidates every
    copy in existence at once."""
    return hmac.new(server_secret, f"sautium-device:v1:{_epoch()}".encode(),
                    "sha256").hexdigest()


def bump_epoch() -> int:
    """Invalidate every issued token. Returns the new epoch."""
    from routers.settings import _read, _write
    nxt = _epoch() + 1
    _write(_EPOCH_KEY, nxt)
    logger.info("device tokens invalidated (epoch %d)", nxt)
    return nxt


# ---------------------------------------------------------------------------
# Password login
# ---------------------------------------------------------------------------

def account_configured() -> bool:
    """True when this node has an account whose password its owner knows —
    i.e. a password login can succeed at all."""
    from config import settings
    return bool(settings.p2p_username and settings.p2p_password)


def _expected_pubkey() -> Optional[str]:
    from config import settings
    from cryptography.hazmat.primitives import serialization
    from p2p_identity import load_signing_key
    key = load_signing_key(settings)
    if key is None:
        return None
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw).hex()


async def verify_password(username: str, password: str) -> bool:
    """Check credentials by DERIVING the identity and comparing public keys.

    The account is deterministic (username+password -> Argon2id -> Ed25519),
    so the derivation itself is the check — there is no password hash to
    store, leak or rotate."""
    expected = _expected_pubkey()
    if not expected or not username or not password:
        return False
    from p2p_identity import derive_identity
    async with _derive_semaphore:
        try:
            got = await asyncio.to_thread(derive_identity, username, password)
        except Exception as e:                      # bad username shape, etc.
            logger.info("password verification failed: %s", e)
            return False
    return hmac.compare_digest(got.get("public_key_hex", ""), expected)


# ---------------------------------------------------------------------------
# PIN pairing (for accounts whose password the owner does not know)
# ---------------------------------------------------------------------------

def issue_pin() -> str:
    """Mint a fresh pairing PIN, replacing any outstanding one."""
    global _pin
    code = "-".join(
        "".join(secrets.choice(_PIN_ALPHABET) for _ in range(PIN_GROUP_LEN))
        for _ in range(PIN_GROUPS))
    _pin = {"code": code, "expires_at": time.time() + PIN_TTL_SECONDS,
            "attempts": 0}
    logger.info("pairing PIN issued (valid %d min)", PIN_TTL_SECONDS // 60)
    return code


def pin_state() -> Optional[dict]:
    """Outstanding PIN for display on the host, or None."""
    if not _pin or time.time() > _pin["expires_at"]:
        return None
    return {"code": _pin["code"],
            "expires_in": int(_pin["expires_at"] - time.time()),
            "attempts_left": MAX_PIN_ATTEMPTS - _pin["attempts"]}


async def redeem_pin(code: str) -> bool:
    """Consume the PIN. One-shot: a correct code burns it, and so does the
    attempt limit. Serialised, because parallel guesses are the whole attack —
    a delay would just make a thousand requests wait together."""
    global _pin
    async with _pin_lock:
        if not _pin or time.time() > _pin["expires_at"]:
            return False
        _pin["attempts"] += 1
        if not hmac.compare_digest(_pin["code"], (code or "").strip().upper()):
            if _pin["attempts"] >= MAX_PIN_ATTEMPTS:
                logger.warning("pairing PIN burned after %d failed attempts",
                               _pin["attempts"])
                _pin = None
            return False
        _pin = None
        return True
