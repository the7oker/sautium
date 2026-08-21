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
  * PIN — small enough to guess, so it expires in minutes, dies after
    MAX_PIN_ATTEMPTS, and every check passes through a lock. Sleeping
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
import threading
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
# How long a just-rotated code stays acceptable. Covers the gap between a
# camera reading the QR and the phone finishing the request.
PIN_ROTATE_GRACE = 30

# One Argon2id derivation is ~256 MB; two concurrent is already half a gig.
# Everything else waits rather than racing the container into an OOM kill.
_derive_semaphore = asyncio.Semaphore(2)
_pin_lock = asyncio.Lock()
# First-run setup is check-then-write around a ~1s Argon2id derivation — a
# wide enough window for two concurrent requests to both pass the "no account
# yet" test and for the second to overwrite the first's identity. Serialise
# the whole operation, and re-check inside it.
_create_lock = asyncio.Lock()

_pin: Optional[dict] = None          # {"code", "expires_at", "attempts"}
_pin_prev: Optional[dict] = None     # {"code", "valid_until"} — rotation grace

# Hosts watching the pairing code. Wake-event, not payload: the client pulls
# the fresh code over the signed API, so the code itself never rides an SSE
# frame that a proxy might buffer or log.
_pin_sse_clients: list = []
_pin_sse_lock = threading.Lock()


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
    copy in existence at once.

    The node's own public key is part of the derivation, because a credential
    has to name what it grants access to. Without it the token was a function
    of (secret file, epoch) alone, and neither belongs to the node: deleting a
    node and creating a new one — new account, new identity, new database —
    handed the browsers paired with the OLD one full access to the new one,
    silently, on a page refresh. An empty key (no account yet) is a value like
    any other: pairing before the node has an owner ends when it gets one."""
    pubkey = _expected_pubkey() or ""
    return hmac.new(server_secret,
                    f"sautium-device:v1:{_epoch()}:{pubkey}".encode(),
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

def _identity_dir():
    from pathlib import Path

    from config import settings
    return Path(settings.p2p_identity_dir) if settings.p2p_identity_dir else None


def account_username() -> Optional[str]:
    """The account this node signs in as, from the stored identity or from
    env credentials. Onboarding writes the former; a hand-configured Docker
    node has the latter."""
    import json

    from config import settings
    d = _identity_dir()
    if d:
        info = d / "node_info.json"
        try:
            return json.loads(info.read_text(encoding="utf-8")).get("username")
        except (OSError, ValueError):
            pass
    return settings.p2p_username or None


def account_configured() -> bool:
    """True when this node has an account to log into at all."""
    return bool(account_username()) and _expected_pubkey() is not None


class AccountExists(Exception):
    """Raised when setup is attempted on a node that already has an identity."""


async def create_account(username: str, password: str) -> dict:
    """Create the node's identity from username+password and persist it.

    Mirrors desktop/node_identity._save_keypair so a Docker node and a
    launcher node are the same thing on disk: the private key as PKCS8 PEM
    plus node_info.json. Nothing about the password is written — the key IS
    the derivation, and losing the password means a new identity.

    Single-shot by construction: the whole check-derive-write runs under a
    lock, the account test is repeated inside it, and the key file is opened
    O_EXCL. Setup is the one moment this node has no owner, and it must
    produce exactly one."""
    d = _identity_dir()
    if d is None:
        raise RuntimeError("no identity directory configured")

    async with _create_lock:
        if account_configured():
            raise AccountExists()
        return await asyncio.to_thread(_write_identity, d, username, password)


def _write_identity(d, username: str, password: str) -> dict:
    import json
    import os
    import stat

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from p2p_identity import derive_identity, derive_private_key

    info = derive_identity(username, password)          # validates the name
    key: Ed25519PrivateKey = derive_private_key(username, password)

    d.mkdir(parents=True, exist_ok=True)
    key_path = d / "node_ed25519.key"
    # O_EXCL, not "check then write": an identity already on disk must never
    # be overwritten, and the filesystem enforces that even against a second
    # process that never saw our lock. A half-written node_info.json would
    # slip past account_configured(), but the key file still exists — and
    # overwriting it would silently change who this node is.
    try:
        fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise AccountExists()
    with os.fdopen(fd, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()))
    try:
        os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    (d / "node_ed25519.pub").write_bytes(key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo))
    (d / "node_info.json").write_text(json.dumps(info, indent=2),
                                      encoding="utf-8")
    logger.info("account created: %s (invite %s)",
                info.get("username"), info.get("invite_code"))
    return info


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


async def verify_password(password: str) -> bool:
    """Check the account password by DERIVING the identity and comparing
    public keys.

    Only the password is asked for: a node has exactly one account, so its
    username is not a choice the person signing in makes — the server reads
    it from its own settings. The account is deterministic
    (username+password -> Argon2id -> Ed25519), so the derivation itself is
    the check; there is no password hash to store, leak or rotate."""
    expected = _expected_pubkey()
    username = account_username()
    if not expected or not username or not password:
        return False
    from p2p_identity import derive_identity
    async with _derive_semaphore:
        try:
            got = await asyncio.to_thread(derive_identity, username, password)
        except Exception as e:
            logger.info("password verification failed: %s", e)
            return False
    return hmac.compare_digest(got.get("public_key_hex", ""), expected)


# ---------------------------------------------------------------------------
# PIN pairing (for accounts whose password the owner does not know)
# ---------------------------------------------------------------------------

def notify_pin_subscribers() -> None:
    """Wake every client watching the pairing code. Called when the code
    changes under the host's feet — i.e. burned by failed attempts, which is
    the one transition the host cannot predict from the deadline it was
    given."""
    with _pin_sse_lock:
        for evt, loop in list(_pin_sse_clients):
            try:
                loop.call_soon_threadsafe(evt.set)
            except RuntimeError:
                continue          # loop closed; generator cleans itself up


def pin_subscribe() -> tuple:
    evt = asyncio.Event()
    entry = (evt, asyncio.get_event_loop())
    with _pin_sse_lock:
        _pin_sse_clients.append(entry)
    return entry


def pin_unsubscribe(entry: tuple) -> None:
    with _pin_sse_lock:
        _pin_sse_clients[:] = [e for e in _pin_sse_clients if e[0] is not entry[0]]


def issue_pin() -> str:
    """Mint a fresh pairing PIN, demoting the outstanding one to a short
    grace period rather than killing it outright.

    Rotation is otherwise a race the scanner cannot win: a camera reads the
    QR a moment before the code turns over, and the phone submits a code that
    stopped existing in between. Keeping the previous one briefly acceptable
    makes that impossible rather than unlikely."""
    global _pin, _pin_prev
    if _pin and time.time() <= _pin["expires_at"]:
        _pin_prev = {"code": _pin["code"],
                     "valid_until": time.time() + PIN_ROTATE_GRACE}
    code = "-".join(
        "".join(secrets.choice(_PIN_ALPHABET) for _ in range(PIN_GROUP_LEN))
        for _ in range(PIN_GROUPS))
    _pin = {"code": code, "expires_at": time.time() + PIN_TTL_SECONDS,
            "attempts": 0}
    logger.info("pairing PIN issued (valid %d min)", PIN_TTL_SECONDS // 60)
    return code


def current_pin() -> dict:
    """The code to put on screen, plus how long it is good for.

    Idempotent within a code's life: the QR and the "Open Web UI" button must
    show the same code, and they used to disagree because merely asking for
    one replaced it. But not idempotent forever — a code past its half-life
    is replaced here, so what the host draws always has minutes left on it.
    Rotating on read rather than on a clock is what keeps the two in step:
    the host redraws, and the redraw is itself the rotation."""
    state = pin_state()
    if state and state["expires_in"] > PIN_TTL_SECONDS // 2:
        return state
    issue_pin()
    return pin_state()


def pin_state() -> Optional[dict]:
    """Outstanding PIN for display on the host, or None."""
    if not _pin or time.time() > _pin["expires_at"]:
        return None
    return {"code": _pin["code"],
            "expires_in": int(_pin["expires_at"] - time.time()),
            "attempts_left": MAX_PIN_ATTEMPTS - _pin["attempts"]}


async def redeem_pin(code: str) -> bool:
    """Check the PIN. Valid until it expires or the attempt limit kills it —
    a successful pairing does NOT consume it.

    One-shot bought nothing here and cost real usability: the code lives for
    minutes on the owner's own screen, so whoever can read it can pair either
    way, while burning it on first use meant a second device (or the same
    device on the node's other address, since a token is bound to one origin)
    silently failed until the code rotated. Serialised, because parallel
    guesses are the whole attack — a delay would just make a thousand
    requests wait together."""
    global _pin, _pin_prev
    async with _pin_lock:
        given = (code or "").strip().upper()
        if (_pin_prev and time.time() <= _pin_prev["valid_until"]
                and hmac.compare_digest(_pin_prev["code"], given)):
            return True
        if not _pin or time.time() > _pin["expires_at"]:
            return False
        _pin["attempts"] += 1
        if not hmac.compare_digest(_pin["code"], given):
            if _pin["attempts"] >= MAX_PIN_ATTEMPTS:
                logger.warning("pairing PIN burned after %d failed attempts",
                               _pin["attempts"])
                _pin = None
                _pin_prev = None
                notify_pin_subscribers()
            return False
        return True
