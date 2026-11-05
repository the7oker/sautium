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
  * possession of a pairing PIN the launcher hands over as a QR code or a
    link (it is never shown as text, and a Docker node has no surface for
    one), for the anonymous accounts the wizard creates with a random
    password its owner has never seen.

The token is a pure function of a server-side EPOCH, so "log out everywhere"
is `epoch += 1` — one integer, no device table. That is a deliberate trade:
individual devices cannot be revoked separately, which for a single-user
appliance is worth the simplicity. Changing the password bumps the epoch too
(a password change must cut off whoever knew the old one).

Storing the token client-side has a second effect worth noting: localStorage
is bound to an origin, so a rebinding attacker on evil.com opens *their own*
empty storage. The vector that made the inlined secret reachable from the
internet closes on its own.

THE WIRE. The Web UI rides plain HTTP on the LAN (PROGRESS.md "HTTP on the
LAN"), so the transport hides nothing. Every exchange that carries a
credential — a password or PIN in, a device token out — is boxed end to end
instead (NaCl box: X25519 + XSalsa20-Poly1305) between the browser and a
per-exchange server key that the node's identity key signs. See "Credential
channel" below.

BRUTE FORCE. The two doors need different locks:
  * PIN — 40 bits, short-lived: it expires in minutes, dies after
    MAX_PIN_ATTEMPTS, and every check passes through a lock. Sleeping
    between attempts would be useless: a thousand parallel requests sleep
    concurrently. Serialising them is what makes attempts countable.
  * password — Argon2id at 256 MB already makes guessing slow (a thousand
    parallel attempts would need 250 GB). The danger runs the other way: that
    same cost turns an unbounded login endpoint into a memory DoS, so
    derivations run under a semaphore, and past DERIVE_QUEUE of them in
    flight a request is turned away (Busy) instead of waiting — the last
    place kept for a signed caller — and a derivation runs to its end even
    when its caller has gone. Wrong passwords are counted, at the login
    door and, on a door of their own, at an identity change: past
    LOGIN_FREE_FAILURES in a row a door stays shut for a delay that doubles
    with each further one (Shut, before any derivation) and one try at a
    time is let in, and the owner is told (login_refused). An anonymous account has
    no password anyone knows: its door is the PIN, and a password login there
    is refused before any derivation.
"""

import asyncio
import base64
import hmac
import json
import logging
import math
import re
import secrets
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_EPOCH_KEY = "auth.token_epoch"

# Unambiguous alphabet (no O/0, I/1) — the PIN is read off a screen and typed
# on a phone. Its length is the defence, 40 bits: a code dies after
# MAX_PIN_ATTEMPTS, but the launcher shows the next one at once, so the cap
# bounds one code, not the guesses over time.
_PIN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PIN_GROUPS = 2
PIN_GROUP_LEN = 4
PIN_TTL_SECONDS = 5 * 60
MAX_PIN_ATTEMPTS = 5
# How long a just-rotated code stays acceptable. Covers the gap between a
# camera reading the QR and the phone finishing the request.
PIN_ROTATE_GRACE = 30

# One Argon2id derivation is ~256 MB; two concurrent is already half a gig.
# Everything else waits rather than racing the container into an OOM kill —
# up to DERIVE_QUEUE in flight, running or waiting; past that, Busy.
_derive_semaphore = asyncio.Semaphore(2)
DERIVE_QUEUE = 4
_derive_in_flight = 0
_pin_lock = asyncio.Lock()
# First-run setup is check-then-write around a ~1s Argon2id derivation — a
# wide enough window for two concurrent requests to both pass the "no account
# yet" test and for the second to overwrite the first's identity. Serialise
# the whole operation, and re-check inside it.
_create_lock = asyncio.Lock()

# First-run setup's code. While a node has no account anyone who reaches its
# port could make one — and own the node — so the account is made only with
# a code the node writes where its owner alone reads: its log, and
# SETUP_CODE_FILE in the identity folder (a Docker node's bind-mounted
# ./data/node_identity). One per start of a node with no account; it dies
# with the account's creation. 60 bits: nothing counts the attempts.
SETUP_CODE_FILE = "setup_code.txt"
SETUP_CODE_GROUPS = 3
_setup_code: Optional[str] = None
# The code within whatever was pasted around it (a log line, a space)
_SETUP_CODE_FORM = re.compile("[%s]{4}(?:-[%s]{4}){%d}" % (
    "A-HJ-NP-Z2-9", "A-HJ-NP-Z2-9", SETUP_CODE_GROUPS - 1))

# The password doors' wrong guesses. Two doors, one rule: the login's, which
# anyone on the LAN reaches — one count for the node, as behind Docker every
# request comes from the bridge's address — and the signed callers' (an
# identity change's current password), so a crowd at the login door never
# turns a browser signed in already away. Past LOGIN_FREE_FAILURES in a row a
# door stays shut for a delay that doubles with each further one, up to
# LOGIN_DELAY_MAX, and lets a try in only when none is in flight; a right
# password clears the count. Process memory: a restart opens
# the doors, and the owner's record (LOGIN_FAILURES_KEY, a notice) stays.
LOGIN_FREE_FAILURES = 5
LOGIN_DELAY_MAX = 15 * 60
LOGIN_FAILURES_KEY = "auth.login_failures"
_doors = {name: {"failures": 0, "shut_until": 0.0, "in_flight": 0} for name in ("login", "signed")}
_failures_lock = threading.Lock()

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

# Read on EVERY signed request — by the auth middleware, on the event loop —
# so it is process memory, not a query. The only writer is bump_epoch()
# below, in this process, so the cache cannot go stale; and a slow database
# no longer freezes the loop for every caller (measured: one 2 s connect per
# concurrent request, the Web UI stalling in 2-second steps, 2026-09-17).
_epoch_cache: Optional[int] = None


def _epoch() -> int:
    global _epoch_cache
    if _epoch_cache is None:
        from routers.settings import _read
        try:
            _epoch_cache = int(_read(_EPOCH_KEY) or 0)
        except (TypeError, ValueError):
            _epoch_cache = 0
    return _epoch_cache


def prime() -> None:
    """Load what every signed request derives its token from — the epoch (a
    DB read) and the node key (a file parse) — before the first request
    lands on the event loop."""
    _epoch()
    _expected_pubkey()


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
    global _epoch_cache
    from routers.settings import _write
    nxt = _epoch() + 1
    _write(_EPOCH_KEY, nxt)
    _epoch_cache = nxt
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


def account_anonymous() -> bool:
    """True when nobody knows this node's password.

    The setup wizard turns a skipped account form into a real identity —
    `anonymous-<4 hex>` plus a 32-byte random password that is derived,
    used and dropped, never shown. Such a node HAS an account and still
    cannot be signed into by password: the PIN is its only door. Reading
    that off the username shape is the fallback for identities minted
    before the flag was written."""
    import json
    import re

    d = _identity_dir()
    if d is None:
        return False
    try:
        info = json.loads((d / "node_info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if "anonymous" in info:
        return bool(info["anonymous"])
    return bool(re.fullmatch(r"anonymous-[0-9a-f]{4}", info.get("username") or ""))


def identity_key_on_disk() -> bool:
    """Is there a key in the identity folder — an identity, named or not?"""
    d = _identity_dir()
    return d is not None and (d / "node_ed25519.key").exists()


def pin_door() -> bool:
    """Does a host show this node's pairing code — the launcher, which gives
    the backend its sync server's port? A Docker node shows none: its doors
    are the account password and the first-run account."""
    from config import settings
    return bool(settings.p2p_listen_port)


class AccountExists(Exception):
    """Raised when setup is attempted on a node that already has an identity."""


class WrongSetupCode(Exception):
    """The setup code given is not the one this node wrote at its start."""


def _code(groups: int) -> str:
    return "-".join("".join(secrets.choice(_PIN_ALPHABET) for _ in range(PIN_GROUP_LEN))
                    for _ in range(groups))


def prepare_setup() -> None:
    """At start, on a node with no account: its setup code, minted and
    written to SETUP_CODE_FILE (readable by the node's user alone) and to
    the log. A node with an account leaves no code behind."""
    import os
    global _setup_code
    d = _identity_dir()
    if d is None:
        return
    path = d / SETUP_CODE_FILE
    # A key on disk is an identity, named or not: creation could only refuse
    if account_configured() or identity_key_on_disk():
        path.unlink(missing_ok=True)
        if not account_configured():
            logger.warning("The identity folder holds a key but no account name (node_info.json): "
                           "no account can be created over it")
        return
    _setup_code = _code(SETUP_CODE_GROUPS)
    d.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(_setup_code + "\n")
    # A warning: the owner has something to do, whatever the log level
    logger.warning("No account yet. The setup code to create it is %s (also in %s in the identity folder; "
                   "a new one at each start)", _setup_code, SETUP_CODE_FILE)


class Busy(Exception):
    """DERIVE_QUEUE derivations are already in flight."""


async def create_account(username: str, password: str, setup_code: str) -> dict:
    """Create the node's identity from username+password and persist it.

    Mirrors desktop/node_identity._save_keypair so a Docker node and a
    launcher node are the same thing on disk: the private key as PKCS8 PEM
    plus node_info.json. Nothing about the password is written — the key IS
    the derivation, and losing the password means a new identity.

    Single-shot by construction: the whole check-derive-write runs under a
    lock, the account test is repeated inside it, and the key file is opened
    O_EXCL. Setup is the one moment this node has no owner, and it must
    produce exactly one — its owner's, who alone reads the setup code
    (prepare_setup; WrongSetupCode, checked before anything is derived)."""
    global _setup_code
    d = _identity_dir()
    if d is None:
        raise RuntimeError("no identity directory configured")

    async with _create_lock:
        if account_configured() or identity_key_on_disk():
            raise AccountExists()
        found = _SETUP_CODE_FORM.search((setup_code or "").upper())
        if _setup_code is None or found is None or not hmac.compare_digest(
                _setup_code.encode(), found.group().encode()):
            raise WrongSetupCode()
        info = await asyncio.to_thread(_write_identity, d, username, password)
        _setup_code = None
        (d / SETUP_CODE_FILE).unlink(missing_ok=True)
        return info


class WrongPassword(Exception):
    """The current password given is not this account's."""


class WrongPin(Exception):
    """The pairing code given is not the one the host shows now."""


async def rotate_identity(username: str, password: str, current_password: str = "",
                          pin: str = "") -> dict:
    """A new name and/or password for the node's identity — in a
    deterministic KDF that is a new key (desktop/node_identity.rotate_identity
    keeps the old one in the archive). An empty password is minted, as the
    wizard mints it: `anonymous` stays the property that the PIN is the only
    door. Same lock as create_account — two rotations racing would archive
    the same key twice.

    An account with a password changes only with it: `current_password` is
    checked under the lock, after the refusals that cost nothing and before
    anything else is derived — a rotation that raced another would
    otherwise change the identity the other just made, on the password of
    the one before (WrongPassword, counted at the signed door under the
    login's rule — Shut; Busy past the derivation bound). An
    anonymous account has no password anyone has seen: its host vouches
    for the change instead, by the pairing code it shows now — `pin`, the
    code a page opened from the host carries (WrongPin; a wrong one costs
    one of MAX_PIN_ATTEMPTS, as at the pairing door).

    Everything in this process that cached the old key is told; the P2P
    layer learns through NOTIFY (the launcher restarts its manager on it —
    a Docker node restarts the backend, its peer surface is bound to the
    key at start)."""
    d = _identity_dir()
    if d is None:
        raise RuntimeError("no identity directory configured")
    async with _create_lock:
        if not account_configured():
            raise RuntimeError("no account to change")
        if not (d / "node_info.json").exists():
            raise RuntimeError("This identity comes from the node's environment "
                               "(P2P_USERNAME, P2P_PASSWORD): change it there")
        if account_anonymous():
            # No code at all is a page that was not opened from the host: it
            # costs the code the host shows nothing
            if not pin or not await redeem_pin(pin):
                raise WrongPin()
        elif not await password_tried(current_password, signed=True):
            raise WrongPassword()
        result = await _rotate_locked(d, username, password)
    _after_rotation()
    return result["info"]


async def _rotate_locked(d, username: str, password: str) -> dict:
    """The rotation itself, under _create_lock. The old key first signs every
    witness record still waiting for its signature (notary.drain_witness): a
    sighting stays signed by the key that saw it, never by one issued after
    it."""
    import notary
    from desktop.node_identity import rotate_identity as _rotate
    await asyncio.to_thread(notary.drain_witness)
    return await asyncio.to_thread(
        _rotate, d, username, password or secrets.token_urlsafe(32), not password)


def _after_rotation() -> None:
    import p2p_chat
    import p2p_identity
    from db_pool import db_execute
    from routers import p2p as p2p_router
    p2p_identity.forget_cached()
    p2p_router.forget_identity()
    p2p_chat.forget_service()
    # The Worker's invite code → mailbox mapping named the old code.
    db_execute("UPDATE user_profile SET email_verified = FALSE WHERE id = 1")
    # Tokens are bound to the public key, so every paired browser is out
    # already; the epoch bump makes that an explicit, logged event.
    bump_epoch()
    db_execute("NOTIFY sautium_identity_rotated")


def _write_identity(d, username: str, password: str) -> dict:
    import json
    import os
    import stat

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from p2p_identity import derive_identity, derive_private_key

    info = derive_identity(username, password)          # validates the name
    # Said, not left to account_anonymous's reading of the name's shape: the
    # owner chose this password, whatever the nickname looks like
    info["anonymous"] = False
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


async def verify_password(password: str, *, signed: bool = False) -> bool:
    """Check the account password by DERIVING the identity and comparing
    public keys.

    Only the password is asked for: a node has exactly one account, so its
    username is not a choice the person signing in makes — the server reads
    it from its own settings. The account is deterministic
    (username+password -> Argon2id -> Ed25519), so the derivation itself is
    the check; there is no password hash to store, leak or rotate. An
    anonymous account is refused before any derivation. Raises Busy
    (derive_guarded; `signed` for a caller a request signature vouches
    for) — and for a derivation that failed: the node could not check the
    password, which says nothing of the password."""
    expected = _expected_pubkey()
    username = account_username()
    if not expected or not username or not password or account_anonymous():
        return False
    from p2p_identity import derive_identity
    try:
        got = await derive_guarded(derive_identity, username, password, signed=signed)
    except Busy:
        raise
    except Exception as e:
        logger.warning("a password could not be checked: the derivation failed: %s", e)
        raise Busy() from e
    return hmac.compare_digest(got.get("public_key_hex", ""), expected)


async def derive_guarded(fn, *args, signed: bool = False):
    """Run one Argon2id derivation of a password check off the loop, under
    the memory semaphore (the module doc's two doors): the login door's,
    change-identity's and the backup CLI's (in its own process). Raises Busy
    when DERIVE_QUEUE are in flight already —
    one fewer for a caller no request signature vouches for, so a crowd at
    the login door never turns the owner's own change away. A caller that
    goes leaves its derivation counted, and holding its semaphore slot,
    until its thread ends — a thread cannot be stopped, so it is what the
    bound counts — and a failure of it is logged, not lost."""
    global _derive_in_flight
    if _derive_in_flight >= DERIVE_QUEUE - (0 if signed else 1):
        raise Busy()
    _derive_in_flight += 1

    async def run():
        global _derive_in_flight
        try:
            async with _derive_semaphore:
                return await asyncio.to_thread(fn, *args)
        finally:
            _derive_in_flight -= 1
    task = asyncio.ensure_future(run())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(_abandoned_derivation)
        raise


class Shut(Exception):
    """The password door waits out its delay: `seconds` left."""

    def __init__(self, seconds: int):
        super().__init__(f"the login door opens in {seconds} s")
        self.seconds = seconds


def _delay(failures: int) -> int:
    """How long the door stays shut after `failures` wrong passwords in a row."""
    over = failures - LOGIN_FREE_FAILURES
    return min(LOGIN_DELAY_MAX, 2 ** min(over - 1, 20)) if over > 0 else 0


async def password_tried(password: str, *, signed: bool = False) -> bool:
    """The account password checked at the door every typed password passes
    (the login, an identity change): Shut while the door waits out a delay;
    past the free failures a try is let in only when none is in flight, and
    holds its door until its answer — the wait it gives meanwhile is the
    delay its failure would cost; a wrong one is counted and told to the
    owner (login_refused), a right one clears the count. Only a derivation
    that ran and did not match counts — an empty password, or a node with
    no password door, is refused for nothing. A `signed` caller passes the
    signed door, the login's crowd never shuts it. Busy as verify_password."""
    door = _doors["signed" if signed else "login"]
    left = door["shut_until"] - time.time()
    if left > 0:
        raise Shut(math.ceil(left))
    held = _delay(door["failures"] + 1)
    if held and door["in_flight"]:
        raise Shut(held)
    if not password or account_anonymous() or not account_configured():
        return False
    door["in_flight"] += 1
    try:
        ok = await verify_password(password, signed=signed)
    finally:
        door["in_flight"] -= 1
    if ok:
        door["failures"] = 0
        door["shut_until"] = 0.0
    else:
        await login_refused("signed" if signed else "login")
    return ok


async def login_refused(name: str) -> None:
    """A wrong password at the door `name`: counted, the door shut past the
    free ones, and the owner told — the record of wrong passwords since the
    notice was last seen, which wakes the notices channel."""
    door = _doors[name]
    door["failures"] += 1
    delay = _delay(door["failures"])
    if delay:
        door["shut_until"] = time.time() + delay
        logger.warning("%d wrong passwords in a row at the %s door: the next is taken in %d s",
                       door["failures"], name, delay)
    await asyncio.to_thread(_record_failure)


def _record_failure() -> None:
    from datetime import datetime, timezone

    from db_pool import db_execute
    from routers.settings import _read, _write
    now = datetime.now(timezone.utc).isoformat()
    with _failures_lock:
        record = _read(LOGIN_FAILURES_KEY) or {}
        # The notice is marked seen by its latest failure: one recorded
        # after the owner looked is news, and starts a new count
        seen = (_read("notice.seen") or {}).get("auth.login_failures")
        if not record or seen == record.get("last"):
            record = {"count": 0, "since": now}
        _write(LOGIN_FAILURES_KEY, {**record, "count": record["count"] + 1, "last": now})
    db_execute("NOTIFY sautium_notices")


def _abandoned_derivation(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.warning("a derivation whose caller went failed: %s", task.exception())


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
    code = _code(PIN_GROUPS)
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
        given = (code or "").strip().upper().encode()
        if (_pin_prev and time.time() <= _pin_prev["valid_until"]
                and hmac.compare_digest(_pin_prev["code"].encode(), given)):
            return True
        if not _pin or time.time() > _pin["expires_at"]:
            return False
        if not hmac.compare_digest(_pin["code"].encode(), given):
            _pin["attempts"] += 1
            if _pin["attempts"] >= MAX_PIN_ATTEMPTS:
                logger.warning("pairing PIN burned after %d failed attempts",
                               _pin["attempts"])
                _pin = None
                _pin_prev = None
                notify_pin_subscribers()
            return False
        # Only wrong codes count toward the burn. Guessing is what the limit is
        # for, and a correct code proves the caller is not guessing — while
        # ordinary use redeems the same code repeatedly (a second device, the
        # same one on another address, every load of a URL that carries it),
        # which used to walk the counter to the edge of the limit for free.
        _pin["attempts"] = 0
        return True


# ---------------------------------------------------------------------------
# Credential channel
# ---------------------------------------------------------------------------
#
# A browser and this node agree on a box key for ONE exchange: /handshake
# mints an X25519 key, the credential request quotes its public half and
# consumes it. A recorded exchange therefore cannot be replayed and no key
# outlives the exchange it was minted for. The handshake carries the node's
# identity signature over the key, so a browser that already knows this node
# (auth.js pins the identity key on first sign-in) can tell the node from
# something answering in its place — trust on first use, as SSH does it.
# Without an identity (first-run setup) the handshake goes out unsigned: the
# node has no owner yet, and whoever sets it up becomes one.

CHANNEL_TTL_SECONDS = 120
# Outstanding handshakes. A LAN can fill this, never memory; the oldest goes.
CHANNEL_CAP = 64
_CHANNEL_CONTEXT = b"sautium-pair:v1"

_channels: dict = {}                 # eph pubkey bytes → (PrivateKey, expires_at)
_channels_lock = threading.Lock()


class ChannelError(Exception):
    """The envelope does not open: unknown or expired handshake, a box that
    fails to authenticate, or a payload that is not a JSON object."""


def open_channel() -> dict:
    """Mint the server half of one exchange. The browser answers with a box
    to `eph`; `sig` is the identity key's signature over
    context || eph || expires (big-endian seconds), empty without identity."""
    from nacl.public import PrivateKey

    from config import settings
    from p2p_identity import load_signing_key

    key = PrivateKey.generate()
    pub = bytes(key.public_key)
    expires = int(time.time()) + CHANNEL_TTL_SECONDS
    with _channels_lock:
        now = time.time()
        for stale in [k for k, (_, exp) in _channels.items() if exp < now]:
            del _channels[stale]
        while len(_channels) >= CHANNEL_CAP:
            del _channels[next(iter(_channels))]        # insertion order = age
        _channels[pub] = (key, expires)
    signing_key = load_signing_key(settings)
    signed = _CHANNEL_CONTEXT + pub + expires.to_bytes(8, "big")
    return {
        "eph": base64.b64encode(pub).decode("ascii"),
        "expires": expires,
        "node_pubkey": _expected_pubkey() or "",
        "sig": signing_key.sign(signed).hex() if signing_key is not None else "",
    }


def unbox(eph: str, client: str, nonce: str, box: str
          ) -> tuple[dict, Callable[[dict], dict]]:
    """Open a boxed credential request. Returns (payload, reply): the JSON
    object the browser sealed, and a function that seals an answer to the
    same client key. The server key is consumed here, whatever the request
    then turns out to be worth — a wrong password costs a new handshake."""
    from nacl.exceptions import CryptoError
    from nacl.public import Box, PublicKey
    from nacl.utils import random as nacl_random

    try:
        eph_raw = base64.b64decode(eph, validate=True)
        client_key = PublicKey(base64.b64decode(client, validate=True))
        nonce_raw = base64.b64decode(nonce, validate=True)
        sealed = base64.b64decode(box, validate=True)
    except (ValueError, TypeError):
        raise ChannelError("malformed envelope")
    with _channels_lock:
        entry = _channels.pop(eph_raw, None)
    if entry is None or entry[1] < time.time():
        raise ChannelError("unknown or expired handshake")
    channel = Box(entry[0], client_key)
    try:
        plain = channel.decrypt(sealed, nonce_raw)
    except CryptoError:
        raise ChannelError("box does not open")
    try:
        payload = json.loads(plain.decode("utf-8"))
    except ValueError:
        raise ChannelError("payload is not JSON")
    if not isinstance(payload, dict):
        raise ChannelError("payload is not an object")

    def reply(obj: dict) -> dict:
        reply_nonce = nacl_random(Box.NONCE_SIZE)
        sealed_reply = channel.encrypt(
            json.dumps(obj).encode("utf-8"), reply_nonce).ciphertext
        return {"nonce": base64.b64encode(reply_nonce).decode("ascii"),
                "box": base64.b64encode(sealed_reply).decode("ascii")}

    return payload, reply
