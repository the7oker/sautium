"""Browser authentication: how a device earns the right to sign requests.

Before this, `GET /` inlined the shared HMAC secret into the page, so the key
was handed to anyone who could load it — every device on the LAN, and via DNS
rebinding any page the owner opened. A key that is published is not
authentication; it is obfuscation with extra steps. It also had no TTL, no
revocation and no notion of a device.

Now the page carries nothing. A browser signs with a DEVICE TOKEN it obtains
once, by proving one of two things:

  * knowledge of the account password — checked by the account's sign-in
    hash (desktop/node_identity: account.json's `signin`, Argon2id at t=8
    over 128 MiB with a salt of its own), never by deriving the key again:
    the key costs minutes (SEC-S5), and a stolen disk holds the key or the
    password already, so the hash makes the password no cheaper to find.
  * possession of a pairing code its host hands over as a QR code or a link
    — the launcher's window, a Docker node's console (`./sautium pair`,
    `python -m pairing`), the QR's value printed under it — for the
    accounts with no password, whose key is random.

THE PASSWORD ITSELF. The key is derived from the nickname and the password,
and an invite code fixes it: whoever holds one tests guesses at home, unseen
and without a limit, so no lock here bounds them. Every door that makes or
changes an account therefore estimates the new password
(desktop/password_strength: the time one GPU would take at the key's cost)
and refuses one under an hour, takes one under a year only on the person's
own word (`keep`), and an existing account is told its time once at a
sign-in (told_once). Nothing of a password or of its estimate is kept but
the public key whose password was estimated.

The token is a pure function of a server-side EPOCH and the account's random
id (account.json's `account_id`, born with the account — before its key), so
"log out everywhere" is `epoch += 1` — one integer, no device table. That is a deliberate trade:
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
channel" below. So does every other secret the Web UI sends (an AI key, a
sign-in's code, the email's code — routers/auth.boxed), and the changes a
device token alone does not make take the password through it (step_up).
A box's length would say its payload's, a password's among it: both sides
pad what they seal to a whole bucket (BOX_BUCKET).

BRUTE FORCE. The two doors need different locks:
  * pairing code — nobody types it (it rides in the QR and in "Open Web
    UI"'s link), so it is long instead of guarded: 140 bits, out of reach of
    any count of guesses. It is never burned — a burn was a lever a LAN
    neighbour pulled to kill the code being scanned — and a wrong one is
    told to the owner (pin_refused), a code this process minted and let
    expire excepted.
  * password — the sign-in hash's 128 MiB makes each try a memory cost, so
    an unbounded login endpoint would be a memory DoS: checks run in one of
    MEMORY_SLOTS (the key's derivation holds one for its whole run), and past
    DERIVE_QUEUE of them in flight a request is turned away (Busy) instead of
    waiting — the last place kept for a signed caller — and a check runs to
    its end even when its caller has gone. Wrong passwords are counted, at the login
    door and, on a door of their own, at an identity change: past
    LOGIN_FREE_FAILURES in a row a door stays shut for a delay that doubles
    with each further one (Shut, before any derivation) and one try at a
    time is let in, and the owner is told (login_refused). An anonymous account has
    no password anyone knows: its door is the PIN, and a password login there
    is refused before any derivation.
"""

import asyncio
import base64
import collections
import hashlib
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

# Unambiguous alphabet (no O/0, I/1): the setup code is typed by hand.
_PIN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PIN_GROUP_LEN = 4
# The pairing code: 7 groups of 4 of 32 characters, 140 bits. Its length is
# the whole defence (SEC-S2 A3): it is never typed, so nothing is lost by it.
PIN_GROUPS = 7
PIN_TTL_SECONDS = 5 * 60
# How long a just-rotated code stays acceptable. Covers the gap between a
# camera reading the QR and the phone finishing the request.
PIN_ROTATE_GRACE = 30
# The codes this process minted, by digest: a wrong code among them is one
# that expired (a QR in a photo, an old tab) — not a guess, not told.
_MINTED_KEEP = 1024

# Every Argon2 call over a password here is 128 MiB — a sign-in's check, the
# account's key (desktop/identity_birth, for its whole run); two at once is
# what a 1 GB board has room for beside the node (PI-NODE-2B §8.15).
# Everything else waits rather than racing the container into an OOM kill —
# up to DERIVE_QUEUE checks in flight, running or waiting; past that, Busy.
MEMORY_SLOTS = 2
_memory_slots = threading.BoundedSemaphore(MEMORY_SLOTS)
DERIVE_QUEUE = 4
_derive_in_flight = 0
# First-run setup is check-then-write around a second's hash — a
# wide enough window for two concurrent requests to both pass the "no account
# yet" test and for the second to overwrite the first's identity. Serialise
# the whole operation, and re-check inside it.
_create_lock = asyncio.Lock()

# First-run setup's code. While a node has no account anyone who reaches its
# port could make one — and own the node — so the account is made only with
# a code the node writes where its owner alone reads: its log, and
# SETUP_CODE_FILE in the identity folder (a Docker node's bind-mounted
# ./data/node_identity). One per start of a node with no account; it dies
# with the account's creation, and a wrong try never changes it.
#
# It is typed by hand, so it is short and forgiving (since 2026-11-19,
# PI-NODE-2B Q20): 8 characters, 40 bits, read out of whatever was typed or
# pasted around it — any case, with or without its hyphen, a space in its
# place, a whole log line (setup_candidates). 40 bits stand behind the
# setup's own door, the login door's rule: past LOGIN_FREE_FAILURES wrong
# tries a delay that doubles to LOGIN_DELAY_MAX. What that costs is a LAN
# neighbour delaying the first run by a quarter of an hour.
SETUP_CODE_FILE = "setup_code.txt"
SETUP_CODE_GROUPS = 2
_setup_code: Optional[str] = None
# The code as the node writes it (its log, its file): what a diagnostics
# report scrubs (scripts/host_agent.SETUP_CODE, held to this by a test)
_SETUP_CODE_FORM = re.compile("[%s]{4}(?:-[%s]{4}){%d}" % (
    "A-HJ-NP-Z2-9", "A-HJ-NP-Z2-9", SETUP_CODE_GROUPS - 1))
# The code as a person gives it back: its two groups with anything that is
# not a letter or a digit between them, standing as a word of their own —
# so a pasted line offers the few words of that shape it holds, never every
# eight-character window of a long string
_SETUP_CODE_TYPED = re.compile("(?<![A-Z0-9])([%s]{4})[^A-Z0-9]{0,3}([%s]{4})(?![A-Z0-9])" % (
    "A-HJ-NP-Z2-9", "A-HJ-NP-Z2-9"))
# One request tries at most this many words of the code's shape — the last
# ones: a pasted log ends on its newest line, and each start wrote a code
SETUP_CANDIDATES_MAX = 4


def setup_candidates(typed: str) -> list:
    """The setup codes `typed` could mean, in the node's own spelling."""
    found = _SETUP_CODE_TYPED.findall((typed or "").upper())
    return ["-".join(groups) for groups in found[-SETUP_CANDIDATES_MAX:]]

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
PIN_FAILURES_KEY = "auth.pin_failures"
# The public key whose password a sign-in estimated (told_once): a new
# password is a new key, so a rotation asks again by itself
PASSWORD_ESTIMATED_KEY = "auth.password_estimated_for"
_doors = {name: {"failures": 0, "shut_until": 0.0, "in_flight": 0}
          for name in ("login", "signed", "setup")}
_failures_lock = threading.Lock()

_pin: Optional[dict] = None          # {"code", "expires_at"}
_pin_prev: Optional[dict] = None     # {"code", "valid_until"} — rotation grace
_minted: "collections.OrderedDict[bytes, None]" = collections.OrderedDict()
_pin_unrecorded = 0                  # wrong codes not yet in the record (pin_refused)
_pin_recorded = 0                    # the record's count as this process last wrote it
_pin_recording = False
_pin_looks = 0                       # the owner's looks at the notices (pin_failures_seen)
_pin_counts = threading.Lock()


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
    DB read) and the account's id (a file) — before the first request lands
    on the event loop; the node key (a file parse), which the handshake
    signs with; and the password estimate, whose lists every door and
    sign-in reads: lists that are not this release's fail the start, not a
    sign-in."""
    _epoch()
    _account_id()
    _expected_pubkey()
    from desktop import password_strength  # noqa: F401


# The account's id as this process last read or wrote it ("" with no
# account). Its writers here say it (_account_written); an account made by
# another process (the launcher's wizard) is there before this one starts.
_account_id_cache: Optional[str] = None


def _account_id() -> str:
    global _account_id_cache
    if not _account_id_cache:
        account = _account()
        _account_id_cache = (account or {}).get("account_id") or ""
    return _account_id_cache


def _account_written(account: Optional[dict]) -> None:
    global _account_id_cache
    _account_id_cache = (account or {}).get("account_id") or ""


def current_token(server_secret: bytes) -> str:
    """The token every paired browser signs with. Derived, not stored — the
    server can always recompute it, and bumping the epoch invalidates every
    copy in existence at once.

    The account's random id is part of the derivation, because a credential
    has to name what it grants access to. Without it the token was a function
    of (secret file, epoch) alone, and neither belongs to the node: deleting a
    node and creating a new one — new account, new identity, new database —
    handed the browsers paired with the OLD one full access to the new one,
    silently, on a page refresh. The id (until SEC-S5 the node's public key)
    is born with the account, so the browser that made it is signed in from
    that first second, minutes before the key. An empty id (no account yet)
    is a value like any other: pairing before the node has an owner ends when
    it gets one."""
    return hmac.new(server_secret,
                    f"sautium-device:v2:{_epoch()}:{_account_id()}".encode(),
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
# The password's estimate (desktop/password_strength)
# ---------------------------------------------------------------------------

class WeakPassword(Exception):
    """A new password one GPU would find within an hour (`verdict["zone"]`
    "refused"), or within a year with no `keep` ("word"). `verdict`: the
    zone, the time in seconds and in words — said to the person through the
    box, never in the clear."""

    def __init__(self, verdict: dict):
        super().__init__(verdict["zone"])
        self.verdict = verdict


def check_new_password(password: str, username: str, email: str, keep: bool) -> None:
    """The edge every door that makes or changes an account holds: refused
    under an hour, kept under a year only on the person's word."""
    from desktop import password_strength
    verdict = password_strength.verdict(password, (username, email))
    if verdict["zone"] == "refused" or (verdict["zone"] == "word" and not keep):
        raise WeakPassword(verdict)


def account_email() -> str:
    """The email the account names, one of the words an attacker tries first
    against its password: its info's, else the one typed at its creation
    (kept in its file until the key)."""
    return _info().get("email") or (_account() or {}).get("email") or ""


def told_once(password: str) -> Optional[dict]:
    """At a sign-in that just checked `password`: the first time this key's
    password is seen here, its estimate — returned to be said once when it is
    under a year, None otherwise. What is written is the key estimated, the
    same for a strong password as for a weak one."""
    from routers.settings import _read, _write
    pubkey = _expected_pubkey()
    if pubkey is None or _read(PASSWORD_ESTIMATED_KEY) == pubkey:
        return None
    from desktop import password_strength
    verdict = password_strength.verdict(password, (account_username() or "", account_email()))
    _write(PASSWORD_ESTIMATED_KEY, pubkey)
    return None if verdict["zone"] == "clear" else verdict


# ---------------------------------------------------------------------------
# Password login
# ---------------------------------------------------------------------------

def _identity_dir():
    from pathlib import Path

    from config import settings
    return Path(settings.p2p_identity_dir) if settings.p2p_identity_dir else None


def _account() -> Optional[dict]:
    """This node's account file (desktop/node_identity.read_account), None
    without one."""
    from desktop.node_identity import read_account
    d = _identity_dir()
    return read_account(d) if d is not None else None


def _info() -> dict:
    """node_info.json, {} without one."""
    d = _identity_dir()
    if d is None:
        return {}
    try:
        return json.loads((d / "node_info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def older_identity() -> bool:
    """A key in the identity folder with an account's name beside it and no
    account file: an identity made by a Sautium before SEC-S5, whose key at
    the old cost nothing here reproduces or checks (the owner's word, SEC-S5
    §15). Its password signs nothing in; the node's account is made anew.
    One with no password anyone knows is not this: its door is the host's
    code, as before."""
    return (identity_key_on_disk() and _account() is None and bool(_info().get("username"))
            and not account_anonymous())


class OlderIdentity(Exception):
    """A password asked of an identity made before SEC-S5 (older_identity)."""

    WORDS = ("This node's identity was made by an older Sautium, and its password can no longer be "
             "checked: make this node's account anew")


def _refuse_older() -> None:
    logger.error("A password was asked of an identity made by a Sautium before this one (a key in "
                 "the identity folder with no account.json): it cannot be checked — make this "
                 "node's account anew")
    raise OlderIdentity()


def account_username() -> Optional[str]:
    """The account this node signs in as: its account file's, else an older
    identity's info."""
    account = _account()
    if account is not None:
        return account.get("username")
    return _info().get("username") or None


def account_configured() -> bool:
    """True when this node has an account at all: its account file — from
    its first second, before its key — or a key with an account's name
    beside it (an identity made before SEC-S5)."""
    return _account() is not None or (identity_key_on_disk() and bool(_info().get("username")))


def key_ready() -> bool:
    """The account's key is in place: what speaks for the node may run."""
    return _expected_pubkey() is not None


def key_state() -> Optional[str]:
    """Where the account's key stands: "ready"; "deriving" (this process
    makes it now); "another_identity" (made, and not the one this node's
    records name: the owner's question, decide_new_identity); "interrupted"
    (none, and nothing makes it: a stop came first); None with no account."""
    if key_ready():
        return "ready"
    if not account_configured():
        return None
    if _held is not None:
        return "another_identity"
    if _birth.state() is not None:
        return "deriving"
    return "interrupted"


def account_anonymous() -> bool:
    """True when nobody knows this node's password.

    An account made with no password — the setup wizard's skipped account
    form, a change to an empty password — has a random key and no sign-in
    hash. Such a node HAS an account and still cannot be signed into by
    password: the PIN is its only door. Reading that off the username shape
    is the fallback for identities minted before the flag was written."""
    import re

    account = _account()
    if account is not None:
        return bool(account.get("anonymous"))
    info = _info()
    if "anonymous" in info:
        return bool(info["anonymous"])
    return bool(re.fullmatch(r"anonymous-[0-9a-f]{4}", info.get("username") or ""))


def password_door() -> bool:
    """Whether a password signs this node in: its account file holds a
    sign-in hash. An account with no password (a random key) has none, nor
    one an identity file with no passphrase brought (the file carries no
    test of the password) — its host's pairing code is their door."""
    account = _account()
    return account is not None and bool(account.get("signin"))


def identity_key_on_disk() -> bool:
    """Is there a key in the identity folder — an identity, named or not?"""
    d = _identity_dir()
    return d is not None and (d / "node_ed25519.key").exists()


def pin_door() -> bool:
    """Does a host show this node's pairing code? Every one does since SEC-S5
    §18: the launcher's window, and on a Docker node its console —
    `./sautium pair`, `python -m pairing` in its container on any host — so
    an account with no password (the master's among them) is signed into and
    vouched for by its host's code, one logic for every node."""
    return True


def launcher_mode() -> bool:
    """This backend runs under a launcher, which gives it its sync server's
    port: the launcher's process owns the node's P2P and its identity
    folder's first key; its setup code is in that folder, not a log."""
    from config import settings
    return bool(settings.p2p_listen_port)


class AccountExists(Exception):
    """Raised when setup is attempted on a node that already has an identity."""


class WrongSetupCode(Exception):
    """The setup code given is not the one this node wrote at its start."""


# Set at start (record_identity_state): no key, while the database says this
# node had one. Nothing it reads changes while no key is in place — the
# signing key of record is written only after a key is placed, and nothing
# seals without one — and placing one ends the onboarding it is asked for in.
_identity_missing = False


def _record_signing_key(pubkey: str) -> None:
    """`pubkey` as the node's signing key of record
    (desktop/node_identity.SIGNING_KEY_SETTING)."""
    from desktop.node_identity import SIGNING_KEY_SETTING
    from routers.settings import _write
    _write(SIGNING_KEY_SETTING, pubkey.lower())


def record_identity_state() -> None:
    """At start, after the migrations: the key this node signs with, when one
    resolves (the identity folder's or the environment's), as its signing key
    of record; with none, whether the database says the node had one
    (OWN_SEALS_SQL), for the status to say once per start."""
    global _identity_missing, _followed
    pubkey = _expected_pubkey()
    _followed = pubkey
    if pubkey is not None:
        _record_signing_key(pubkey)
        return
    from db_pool import db_query_one
    from desktop.node_identity import OWN_SEALS_SQL
    _identity_missing = db_query_one(OWN_SEALS_SQL)["found"]


def identity_missing() -> bool:
    """No identity here, while the database said at start the node had one."""
    return _identity_missing


def _another_identity(pubkey: str) -> bool:
    """Whether the identity `pubkey` (lower-case hex) is not the one this
    node's database names as its own (node_identity.names_another_identity)."""
    from db_pool import get_conn
    from desktop.node_identity import names_another_identity
    with get_conn() as conn, conn.cursor() as cur:
        return names_another_identity(cur, pubkey)


def _holds_identity(pubkey: Optional[str] = None) -> bool:
    """Whether this node's database names an identity as its own — `pubkey`,
    when given (lower-case hex) — by the record _another_identity reads: the
    signing key of record, else the last P2P run's key, else the seals."""
    from db_pool import db_query_one
    from desktop.node_identity import OWN_SEALS_SQL
    if not db_query_one(OWN_SEALS_SQL)["found"]:
        return False
    return pubkey is None or not _another_identity(pubkey)


def _may_take_name(username: str, pubkey: Optional[str] = None) -> bool:
    """Whether an account may be made here under `username` (B-132): any name
    the reservation does not hold; a reserved one only by Support's own key,
    or by the identity this node's database already names (a holder bringing
    its key back). Asked before the derivation (`pubkey` None) where the key
    alone could decide — a node meant to be the master
    (MASTER-FAILCLOSED's flag) or one whose records name an identity — so any
    other refusal costs no Argon2id; asked again with the key it derived."""
    import master_node
    from config import settings
    from desktop.node_identity import is_reserved_username
    if not is_reserved_username(username):
        return True
    if pubkey is None:
        return settings.sautium_master or _holds_identity()
    return pubkey == master_node.MASTER_PUBKEY_HEX.lower() or _holds_identity(pubkey)


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
    """DERIVE_QUEUE checks are already in flight."""


class KeyBeingMade(Exception):
    """The account's key is being made (or the owner is asked about the one
    made): another derivation, a change of identity, waits for it."""


class NothingHeld(Exception):
    """decide_new_identity with no key waiting for the owner's word."""


# ---------------------------------------------------------------------------
# The account's key, made in the background (desktop/identity_birth)
# ---------------------------------------------------------------------------
# The account exists from its first second (its file: the nickname, the
# sign-in hash, the token's id); its key follows minutes later. What speaks
# for the node waits for the key, and is started when it lands (_key_arrived);
# the rest runs meanwhile. In launcher mode the launcher's process makes the
# account's key (its wizard, its window): one process makes a node's key, so
# no second derivation races the first over the folder.

from desktop.identity_birth import Birth  # noqa: E402

_birth = Birth()
_loop: Optional[asyncio.AbstractEventLoop] = None
# A key made that is not the identity this node's records name: held for the
# owner's word (decide_new_identity) — {"key", "account", "weak"} — in this
# process's memory alone; a restart derives it again at the next sign-in.
_held: Optional[dict] = None


def births_here() -> bool:
    """Whether this process makes the account's key: a Docker node's backend,
    never a launcher's (launcher_mode), whose own process does."""
    return not launcher_mode()


def _publish_birth(state: dict) -> None:
    """The key's state to the pages (user_settings 'p2p.identity' + NOTIFY
    sautium_identity, the identity line's channel): its start with the
    estimate, its end in a refusal or the owner's question. A key that lands
    is said by the identity worker it starts."""
    from routers import p2p as p2p_router
    p2p_router._publish_identity_state(state)


def _start_birth(username: str, password: str, purpose: str, done) -> bool:
    """identity_birth's worker for this account, in one of the memory slots
    for its whole run; `done(seed)` in its thread. False when one runs. A
    first key's start and failure are the identity line's (the node has no
    identity until it lands); a rotation's are not — the old identity stands
    meanwhile, and the change's own sheet said what is being made."""
    def failed(error: BaseException) -> None:
        # A first key that failed is one not made: its password makes it
        # again (the page's sheet). A rotation's failure leaves the old
        # identity whole; its error is the log's (identity_birth)
        if purpose != "rotate" and key_state() == "interrupted":
            _publish_birth({"status": "interrupted", "purpose": "resume"})

    said = _publish_birth if purpose != "rotate" else (lambda state: None)
    return _birth.start(username, password, purpose=purpose, done=done, said=said,
                        failed=failed, slot=_memory_slots)


def _on_loop(coro_fn) -> None:
    """`coro_fn()` on the backend's loop, from the worker's thread."""
    asyncio.run_coroutine_threadsafe(coro_fn(), _loop).result()


def _key_arrived(pubkey: str) -> None:
    """The account's key is in place, under a running process (a creation,
    the owner's word to another identity): every cache of "no key" dropped, the role
    decided, and what the start decided without a key decided again — the
    chat client, the identity worker (whose state replaces the key's), the
    followers (main: the peer surface, the DHT, the gate, the requesters,
    the notary)."""
    import p2p_chat
    import p2p_identity
    from routers import p2p as p2p_router
    p2p_identity.forget_cached()
    p2p_router.forget_identity()
    from desktop.p2p import master_node
    # The master's account made here builds no client and seeds no Support
    # contact to itself, and its routes answer as the master's at once; what
    # the start arms — the mailbox, the warrants' dispatch, the relay
    # announce — waits for the next start
    if master_node.decide(pubkey):
        logger.info("The master's key is in place: restart the backend to arm its mailbox, "
                    "its warrants' dispatch and its relay announce")

    async def follow():
        p2p_chat.soon(p2p_chat.start_client())
        p2p_chat.soon(p2p_router.start_identity_worker())
        p2p_chat.soon(p2p_identity.changed())
    _on_loop(follow)


# In launcher mode the launcher's process makes the account's key and says
# so (NOTIFY sautium_identity_born): this process follows it as it follows a
# key made here — its notary, its records, its role. On every (re)connect the
# folder is read once more: a key that landed while the listener was down.
_born_listener = None
_followed: Optional[str] = None


def _born_elsewhere(_notifies=None) -> None:
    global _followed
    import p2p_identity
    p2p_identity.forget_cached()
    pubkey = _expected_pubkey()
    if pubkey is None or pubkey == _followed:
        return
    _followed = pubkey
    logger.info("The account's key made by the launcher is in place: followed")
    _record_signing_key(pubkey)
    _key_arrived(pubkey)


def start_born_listener() -> None:
    """Launcher mode, at the start (on the loop): the launcher's key followed."""
    global _born_listener, _loop
    from db_pool import Listener
    _loop = asyncio.get_running_loop()
    # `_followed` is the key the start found (record_identity_state): one
    # placed since is followed at the listener's first connect
    _born_listener = Listener("identity-born", ("sautium_identity_born",),
                              on_notify=_born_elsewhere, on_connect=_born_elsewhere)
    _born_listener.start()


def stop_born_listener() -> None:
    if _born_listener is not None:
        _born_listener.stop()


def _born(account: dict, held: bool, weak: Optional[dict]):
    """The end of a creation's derivation (`seed` in the worker's thread)."""
    def done(seed: bytes) -> None:
        global _held
        from desktop.node_identity import (RESERVED_REFUSAL, key_from_seed, place_account_key,
                                           pubkey_on_disk, public_hex)
        d = _identity_dir()
        key = key_from_seed(seed)
        pubkey = public_hex(key)
        if not _may_take_name(account["username"], pubkey):
            logger.warning("The reserved nickname's account was not made: the key it derives "
                           "is not Support's")
            _undo_account(RESERVED_REFUSAL)
            return
        if held and _another_identity(pubkey):
            _held = {"key": key, "account": account, "weak": weak}
            logger.warning("The account's key is not the identity this node's records name: "
                           "the owner is asked")
            _publish_birth({"status": "another_identity", "weak": weak})
            return
        try:
            place_account_key(d, key, account)
        except FileExistsError:
            _record_signing_key(pubkey_on_disk(d))
            logger.error("A key landed in the identity folder while the account's was made: "
                         "it stays, and the one made is dropped")
            return
        except OSError:
            # A key that could not be placed (a full disk) leaves no account
            # behind: the setup is offered again, its new code in the log
            _undo_account("failed")
            raise
        _record_signing_key(pubkey)
        _key_arrived(pubkey)
    return done


def _undo_account(detail: str) -> None:
    """The account made before its key is taken back (a refusal its key
    decided, the owner's "no" to another identity): its file goes, and the
    node offers its setup again, with a new code."""
    d = _identity_dir()
    (d / "account.json").unlink(missing_ok=True)
    _account_written(None)
    prepare_setup()
    _publish_birth({"status": "refused", "detail": detail})


async def create_account(username: str, password: str, setup_code: str,
                         new_identity: bool = False, keep: bool = False,
                         email: str = "") -> dict:
    """Create the node's account from username+password: its file at once
    (desktop/node_identity.new_account — the sign-in hash, the token's id),
    its key derived in the background (identity_birth), placed as the
    launcher places its own (node_identity.place_account_key), so a Docker
    node and a launcher node are the same thing on disk.

    Single-shot by construction: the whole check-and-write runs under a
    lock, the account test is repeated inside it, and the key file is placed
    exclusively. Setup is the one moment this node has no owner, and it must
    produce exactly one — its owner's, who alone reads the setup code
    (prepare_setup; WrongSetupCode, checked before anything else).

    A node whose database says it had an identity has lost its key: the
    setup brings that identity back, from the same nickname and password —
    the info the folder kept of it stays. Whether the pair makes that
    identity is known only when the key is made: a pair that makes another
    is held for the owner's word (key_state "another_identity",
    decide_new_identity) — `new_identity` says it at once, the page's
    "make a new one" sent after he was told that a new identity makes his
    records another author's.

    A new identity's password passes the estimate's edge
    (check_new_password: WeakPassword; `keep` the person's word, `email`
    typed beside it one of the words an attacker tries first) — at once where
    the database names no identity; where it does, the identity of record
    comes back whatever its password, and another is held to the same edge
    at the owner's word."""
    global _setup_code, _loop
    from desktop.node_identity import RESERVED_REFUSAL, new_account, write_account
    d = _identity_dir()
    if d is None:
        raise RuntimeError("no identity directory configured")

    async with _create_lock:
        if account_configured() or identity_key_on_disk():
            raise AccountExists()
        # The setup's door: shut by wrong codes, it answers before a code is
        # compared; one try at a time is the lock's. The code is never
        # burned — a wrong try leaves it as it is.
        door = _doors["setup"]
        left = door["shut_until"] - time.time()
        if left > 0:
            raise Shut(math.ceil(left))
        wanted = (_setup_code or "").encode()
        if _setup_code is None or not any([hmac.compare_digest(wanted, candidate.encode())
                                           for candidate in setup_candidates(setup_code)]):
            door["failures"] += 1
            delay = _delay(door["failures"])
            if delay:
                door["shut_until"] = time.time() + delay
                logger.warning("%d wrong setup codes in a row: the next is taken in %d s",
                               door["failures"], delay)
            raise WrongSetupCode()
        # The right code clears the door, whatever the password then meets
        door["failures"], door["shut_until"] = 0, 0.0
        if not _may_take_name(username):
            raise ValueError(RESERVED_REFUSAL)
        held = await asyncio.to_thread(_holds_identity)
        if not password:
            _loop = asyncio.get_running_loop()
            made = await asyncio.to_thread(_random_account, d, username, email,
                                           held and not new_identity)
            _setup_code = None
            (d / SETUP_CODE_FILE).unlink(missing_ok=True)
            return made
        weak = None
        if not held:
            await asyncio.to_thread(check_new_password, password, username, email, keep)
        elif not new_identity:
            try:
                await asyncio.to_thread(check_new_password, password, username, email, keep)
            except WeakPassword as e:
                weak = e.verdict
        else:
            await asyncio.to_thread(check_new_password, password, username, email, keep)
        account = await asyncio.to_thread(new_account, username, password, source="form",
                                          email=email)
        # The person's word for a password under a year: a key made again
        # after a stop holds it to the same edge (resume_birth)
        account["kept"] = bool(keep)
        await asyncio.to_thread(write_account, d, account)
        _account_written(account)
        _setup_code = None
        (d / SETUP_CODE_FILE).unlink(missing_ok=True)
        _loop = asyncio.get_running_loop()
        if not births_here():
            # A launcher's node: its window makes the key, asking the
            # password once at its next start — one process makes a key
            logger.info("The account is made; the launcher makes its key at its next start")
            return {"username": username, "key": "interrupted"}
        _start_birth(username, password, "create",
                     _born(account, held and not new_identity, weak))
        return {"username": username, "key": "deriving"}


def _random_account(d, username: str, email: str, held: bool) -> dict:
    """The setup's account with no password: a random key, at once — nothing
    would derive it again (SEC-S5 §15, §20). Over records of another identity
    it is held for the owner's word like any other key that is not theirs."""
    global _held
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from desktop.node_identity import (RESERVED_REFUSAL, new_account, place_account_key,
                                       public_hex, write_account)
    account = new_account(username, "", source="form", email=email, anonymous=True)
    key = Ed25519PrivateKey.generate()
    pubkey = public_hex(key)
    if not _may_take_name(username, pubkey):
        raise ValueError(RESERVED_REFUSAL)
    write_account(d, account)
    _account_written(account)
    if not births_here():
        # A launcher's node: its window makes the key (a random one, at once)
        logger.info("The account is made; the launcher makes its random key at its next start")
        return {"username": username, "key": "interrupted"}
    if held:
        _held = {"key": key, "account": account, "weak": None}
        _publish_birth({"status": "another_identity", "weak": None})
        return {"username": username, "key": "another_identity"}
    info = place_account_key(d, key, account)
    _record_signing_key(pubkey)
    _key_arrived(pubkey)
    return {"username": username, "key": "ready", "invite_code": info["invite_code"],
            "public_key_hex": pubkey}


async def decide_new_identity(make: bool) -> dict:
    """The owner's word on a key made that is not the identity this node's
    records name (key_state "another_identity"): `make` places it — the
    records the identity of record signed are another author's from then on
    — unless its password is under the estimate's edge (WeakPassword: the
    account is made again with another); no takes the account back and the
    setup is offered again. NothingHeld with no such key."""
    global _held, _loop
    from desktop.node_identity import place_account_key, public_hex
    async with _create_lock:
        held = _held
        if held is None:
            raise NothingHeld()
        if not make:
            _held = None
            await asyncio.to_thread(_undo_account, "another_identity")
            return {"key": None}
        if held["weak"] is not None:
            raise WeakPassword(held["weak"])
        _held = None
        pubkey = public_hex(held["key"])
        info = await asyncio.to_thread(place_account_key, _identity_dir(), held["key"],
                                       held["account"])
        _record_signing_key(pubkey)
        logger.warning("A new identity was made on the owner's word (%s), over records the "
                       "identity of record signed", info["invite_code"])
    _loop = asyncio.get_running_loop()
    await asyncio.to_thread(_key_arrived, pubkey)
    return {"key": "ready", "invite_code": info["invite_code"], "public_key_hex": pubkey}


def say_key_at_start() -> None:
    """At a start, after record_identity_state: an account whose key is not
    in place and which this process makes (births_here) is said
    "interrupted" on the identity line's channel — what a stop before the
    key's end left there ("deriving", or the owner's question, whose held
    key went with the process) is not the truth any more — and the page
    asks for the password to make it again (POST /api/auth/resume-key)."""
    global _held
    if not births_here() or key_state() != "interrupted":
        return
    account = _account()
    if account.get("anonymous"):
        # A random key loses nothing by being made again: at once, held again
        # for the owner's word over records of another identity
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from desktop.node_identity import place_account_key, public_hex
        key = Ed25519PrivateKey.generate()
        if _holds_identity():
            _held = {"key": key, "account": account, "weak": None}
            _publish_birth({"status": "another_identity", "weak": None})
            return
        place_account_key(_identity_dir(), key, account)
        _record_signing_key(public_hex(key))
        import p2p_identity
        p2p_identity.forget_cached()
        logger.warning("The account's random key was not placed before the node stopped: made again")
        return
    logger.warning("The account's key was not made before the node stopped: the next "
                   "sign-in with its password makes it again")
    _publish_birth({"status": "interrupted", "purpose": "resume"})


def resume_birth(password: str) -> bool:
    """A sign-in whose password the hash just took, on an account whose key
    was not made (a stop came first): its making starts again, on this
    process's loop. False where none starts (made, making, held, or the
    launcher's to make)."""
    global _loop
    account = _account()
    if not births_here() or account is None or key_state() != "interrupted":
        return False
    _loop = asyncio.get_running_loop()
    logger.info("The account's key was not made before a stop: its making starts again")
    from desktop import password_strength
    verdict = password_strength.verdict(password, (account["username"], account.get("email") or ""))
    # A password the setup took only because the identity of record comes
    # back whatever its password: another identity made from it is refused
    weak = verdict if (verdict["zone"] == "refused"
                       or (verdict["zone"] == "word" and not account.get("kept"))) else None
    return _start_birth(account["username"], password, "resume",
                        _born(account, _holds_identity(), weak))


class WrongPassword(Exception):
    """The current password given is not this account's."""


class WrongPin(Exception):
    """The pairing code given is not the one the host shows now."""


async def rotate_identity(username: str, password: str, current_password: str = "",
                          pin: str = "", keep: bool = False) -> dict:
    """A new name and/or password for the node's identity — in a
    deterministic KDF that is a new key (desktop/node_identity.rotate_identity
    keeps the old one in the archive). An empty password is a random key at
    once (no password anyone knows, so nothing would derive it again):
    `anonymous` stays the property that the PIN is the only door. Same lock
    as create_account; one key is made at a time (KeyBeingMade).

    An account with a password changes only with it: `current_password` is
    checked under the lock, after the refusals that cost nothing and before
    anything else is made (WrongPassword, counted at the signed door under
    the login's rule — Shut; Busy past the checks' bound). An anonymous
    account has no password anyone has seen: its host vouches for the
    change instead, by the pairing code it shows now — `pin`, the code a
    page opened from the host carries (WrongPin; a wrong one is told, as at
    the pairing door). A new password passes the estimate's edge
    (check_new_password: WeakPassword, `keep` the person's word) after the
    word; the same name and password are this identity already.

    A new password's key is derived in the background (identity_birth): the
    old identity stays whole and signs in with the old password until the
    new key is placed — a stop before that leaves it so — and then
    everything in this process that cached the old key is told, and its
    chat client and identity worker start again on the new one; the P2P
    layer learns through NOTIFY (the launcher restarts its manager on it — a
    Docker node's peer surface follows the key at once, and it restarts the
    backend for its admission gate, built on the key at start). Returns the
    new info at once for an empty password, else {"key": "deriving"}."""
    global _loop
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from desktop.node_identity import check_rotation_name, key_from_seed, signin_hash, signin_matches
    d = _identity_dir()
    if d is None:
        raise RuntimeError("no identity directory configured")
    async with _create_lock:
        if not account_configured():
            raise RuntimeError("no account to change")
        if older_identity():
            _refuse_older()
        account = _account()
        if account is None:
            _refuse_older()               # an identity made before SEC-S5, anonymous: nothing here rotates it
        if key_state() != "ready" or _birth.state() is not None:
            raise KeyBeingMade()
        await step_up(current_password, pin)
        check_rotation_name(d, username)
        if not password:
            info = await asyncio.to_thread(_rotate_to, d, username, Ed25519PrivateKey.generate(),
                                           True, None)
            _after_rotation(info["public_key_hex"])
            return info
        if (username == account["username"] and account.get("signin")
                and await asyncio.to_thread(_in_slot, signin_matches, account["signin"], password)):
            raise ValueError("That name and password are this identity already")
        await asyncio.to_thread(check_new_password, password, username, account_email(), keep)
        signin = await asyncio.to_thread(_in_slot, signin_hash, password)
        _loop = asyncio.get_running_loop()

        def done(seed: bytes) -> None:
            info = _rotate_to(d, username, key_from_seed(seed), False, signin)

            async def follow():
                _after_rotation(info["public_key_hex"])
            _on_loop(follow)

        if not _start_birth(username, password, "rotate", done):
            raise KeyBeingMade()
        return {"username": username, "key": "deriving"}


async def step_up(password: str, pin: str) -> None:
    """The person's own word for a change a device token alone does not
    make: who the node is (rotate_identity), and the rare changes that act
    outside the node and outlast "Sign out everywhere" — the account email
    at the Worker, an invite token with rights, the AI provider's key, an
    agent's sign-in (routers/auth.step_up). The account's password, at the
    signed door (counted, shut past the free failures, the owner told —
    password_tried; Shut, Busy); an anonymous account has no password anyone
    has seen, so its host vouches instead, by the pairing code it shows now
    (a wrong one is told). WrongPassword / WrongPin otherwise — a node with
    no account has nobody to vouch."""
    if account_configured() and (account_anonymous() or not password_door()) and not older_identity():
        # No password signs this account in: its host vouches by the code it
        # shows. No code at all is a page that was not opened from the host:
        # it costs the code the host shows nothing
        if not pin or not await redeem_pin(pin):
            raise WrongPin()
    elif not await password_tried(password, signed=True):
        raise WrongPassword()


# A flow of several requests (an email set, its code sent and verified; an
# agent's sign-in started, its code pasted or read) takes the password at its
# first step, and that step's sealed reply carries a GRANT the later steps
# quote inside their own boxes: a device token alone never sees one, so it
# stands for the password typed in this flow. One per flow kind — a new
# step-up replaces it — in memory, for the flow's own lifetime.
_grants: dict = {}                   # kind → (value, expires_at)
_grants_lock = threading.Lock()


def grant(kind: str, ttl: float) -> str:
    value = secrets.token_urlsafe(16)
    with _grants_lock:
        _grants[kind] = (value, time.time() + ttl)
    return value


def grant_holds(kind: str, value: str) -> bool:
    with _grants_lock:
        held = _grants.get(kind)
    return (held is not None and held[1] >= time.time() and bool(value)
            and hmac.compare_digest(held[0].encode(), value.encode()))


def drop_grant(kind: str) -> None:
    with _grants_lock:
        _grants.pop(kind, None)


def _rotate_to(d, username: str, key, anonymous: bool, signin: Optional[str]) -> dict:
    """The rotation itself, in a thread. The old key first signs every
    witness record still waiting for its signature (notary.drain_witness): a
    sighting stays signed by the key that saw it, never by one issued after
    it."""
    import notary
    from desktop.node_identity import rotate_identity as _rotate
    notary.drain_witness()
    return _rotate(d, username, key, anonymous, signin)["info"]


def _after_rotation(pubkey: str) -> None:
    import p2p_chat
    import p2p_identity
    from db_pool import db_execute
    from routers import p2p as p2p_router
    global _followed
    # First: a token names the account's id, which a rotation keeps — the
    # epoch's bump is what ends every browser the old password signed in,
    # and nothing below may fail before it
    bump_epoch()
    _followed = pubkey                   # this process's own key: nothing to follow
    p2p_identity.forget_cached()
    p2p_router.forget_identity()
    p2p_chat.forget_service()
    # The chat client and the identity worker are built on the old key: they
    # start again on the new (the worker's old thread stopped first), under
    # the role the new key has.
    from desktop.p2p import master_node
    master_node.decide(pubkey)
    p2p_chat.soon(p2p_chat.restart_client())
    p2p_chat.soon(p2p_router.restart_identity_worker())
    p2p_chat.soon(p2p_identity.changed())
    _record_signing_key(pubkey)
    # The Worker's invite code → mailbox mapping named the old code. Said to
    # the pages by the write itself, as the routes' writes are
    # (routers.p2p._record_email_verified) — the identity line and the
    # guidance trail read it: the workers' restarts above publish when they
    # get to it, or never (a key with no certificate yet)
    db_execute("UPDATE user_profile SET email_verified = FALSE WHERE id = 1")
    db_execute("NOTIFY sautium_identity")
    db_execute("NOTIFY sautium_identity_rotated")


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
    """Check the account password against its sign-in hash
    (node_identity.signin_matches — never a derivation of the key).

    Only the password is asked for: a node has exactly one account, so its
    username is not a choice the person signing in makes. An anonymous
    account (no hash: no password anyone knows) is refused before any check;
    an identity made before SEC-S5 raises OlderIdentity. Raises Busy
    (derive_guarded; `signed` for a caller a request signature vouches
    for) — and for a check that failed: the node could not check the
    password, which says nothing of the password."""
    if older_identity():
        _refuse_older()
    account = _account()
    if not account or not account.get("signin") or not password or account_anonymous():
        return False
    held = _expected_pubkey()
    if account.get("pubkey") and held is not None and account["pubkey"] != held:
        logger.error("The account file's hash was made for the key %s…, and the identity folder "
                     "holds %s…: it checks no password", account["pubkey"][:16], held[:16])
        return False
    from desktop.node_identity import signin_matches
    try:
        return await derive_guarded(signin_matches, account["signin"], password, signed=signed)
    except Busy:
        raise
    except Exception as e:
        logger.warning("a password could not be checked: %s", e)
        raise Busy() from e


def _in_slot(fn, *args):
    """`fn(*args)` in one of the memory slots — in the caller's thread."""
    with _memory_slots:
        return fn(*args)


async def derive_guarded(fn, *args, signed: bool = False):
    """Run one Argon2 call of a password check off the loop, in a memory
    slot (the module doc's two doors): the login door's and the change of
    identity's. Raises Busy when DERIVE_QUEUE are in flight already —
    one fewer for a caller no request signature vouches for, so a crowd at
    the login door never turns the owner's own change away. A caller that
    goes leaves its check counted, and holding its slot, until its thread
    ends — a thread cannot be stopped, so it is what the bound counts — and
    a failure of it is logged, not lost."""
    global _derive_in_flight
    if _derive_in_flight >= DERIVE_QUEUE - (0 if signed else 1):
        raise Busy()
    _derive_in_flight += 1

    async def run():
        global _derive_in_flight
        try:
            return await asyncio.to_thread(_in_slot, fn, *args)
        finally:
            _derive_in_flight -= 1
    task = asyncio.ensure_future(run())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(_abandoned_derivation)
        raise


class Shut(Exception):
    """A door (a password's, the setup code's) waits out its delay:
    `seconds` left."""

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
    if (not password or not account_configured()
            or (not password_door() and not older_identity())):
        return False
    door["in_flight"] += 1
    try:
        ok = await verify_password(password, signed=signed)
    finally:
        door["in_flight"] -= 1
    if ok:
        door["failures"] = 0
        door["shut_until"] = 0.0
        resume_birth(password)
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
    await asyncio.to_thread(_record_failure, LOGIN_FAILURES_KEY)


def _record_failure(key: str, n: int = 1) -> int:
    """`n` more wrong credentials in the owner's record `key` — its setting
    is its notice's key too (routers/settings._REPORTS). Returns the count
    the record holds now."""
    from datetime import datetime, timezone

    from db_pool import db_execute
    from routers.settings import _read, _write
    now = datetime.now(timezone.utc).isoformat()
    with _failures_lock:
        record = _read(key) or {}
        # The notice is marked seen by its latest failure: one recorded
        # after the owner looked is news, and starts a new count
        seen = (_read("notice.seen") or {}).get(key)
        if not record or seen == record.get("last"):
            record = {"count": 0, "since": now}
        count = record["count"] + n
        _write(key, {**record, "count": count, "last": now})
    db_execute("NOTIFY sautium_notices")
    return count


def _abandoned_derivation(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.warning("a derivation whose caller went failed: %s", task.exception())


# ---------------------------------------------------------------------------
# Pairing code (for accounts whose password the owner does not know)
# ---------------------------------------------------------------------------

def _digest(code: str) -> bytes:
    return hashlib.sha256(code.encode()).digest()


def issue_pin() -> str:
    """Mint a fresh pairing code, demoting the outstanding one to a short
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
    _pin = {"code": code, "expires_at": time.time() + PIN_TTL_SECONDS}
    _minted[_digest(code)] = None
    while len(_minted) > _MINTED_KEEP:
        _minted.popitem(last=False)
    logger.info("pairing code issued (valid %d min)", PIN_TTL_SECONDS // 60)
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
    """Outstanding code for display on the host, or None."""
    if not _pin or time.time() > _pin["expires_at"]:
        return None
    return {"code": _pin["code"],
            "expires_in": int(_pin["expires_at"] - time.time())}


async def redeem_pin(code: str) -> bool:
    """Check the pairing code. Valid until it expires — a pairing does not
    consume it, and a wrong code does not burn it: the code lives for minutes
    on the owner's own screen, so whoever can read it can pair either way,
    and at 140 bits no count of guesses reaches it. A wrong code is told
    (pin_refused), unless it is one this process minted and let expire."""
    given = (code or "").strip().upper()
    if (_pin_prev and time.time() <= _pin_prev["valid_until"]
            and hmac.compare_digest(_pin_prev["code"].encode(), given.encode())):
        return True
    if _pin and time.time() <= _pin["expires_at"] and hmac.compare_digest(_pin["code"].encode(),
                                                                          given.encode()):
        return True
    if given and _digest(given) not in _minted:
        await pin_refused()
    return False


async def pin_refused() -> None:
    """A wrong pairing code, into the owner's record of them since the
    notice was last seen (_record_failure's rule). Nothing slows a wrong code
    down — it is no threat at 140 bits — so a crowd of them must not be a
    crowd of writes: the record is written when the codes not yet in it
    reach the count it holds (1, 2, 4, 8 … — a flood of N is about log₂ N
    writes, its notice saying "at least"), one write in flight at a time.
    The owner's look at the notice starts it from one again
    (pin_failures_seen, from a route's thread: the counters move under
    `_pin_counts`, never across the write's await; a look that lands while a
    write is in flight makes that write's count stale, `_pin_looks` says so)."""
    global _pin_unrecorded, _pin_recorded, _pin_recording
    with _pin_counts:
        _pin_unrecorded += 1
    if _pin_recording:
        return
    _pin_recording = True
    try:
        while True:
            with _pin_counts:
                if _pin_unrecorded < max(1, _pin_recorded):
                    break
                n, _pin_unrecorded = _pin_unrecorded, 0
                looks = _pin_looks
            count = await asyncio.to_thread(_record_failure, PIN_FAILURES_KEY, n)
            with _pin_counts:
                _pin_recorded = count if looks == _pin_looks else 0
    finally:
        _pin_recording = False


def pin_failures_seen() -> None:
    """The owner saw the notices: the next wrong code starts a new record,
    written at once; the ones not yet written came before the look."""
    global _pin_recorded, _pin_unrecorded, _pin_looks
    with _pin_counts:
        _pin_looks += 1
        _pin_recorded = _pin_unrecorded = 0


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
# What either side seals is padded to a whole number of these bytes (the
# `_pad` member, which every reader ignores): a box's length would otherwise
# be its payload's, and a password's length with it, and a meter's answer
# would say its zone. Auth.js pads its payloads to the same bucket.
BOX_BUCKET = 1024
# Outstanding handshakes. A LAN can fill this, never memory; the oldest goes.
CHANNEL_CAP = 64
_CHANNEL_CONTEXT = b"sautium-pair:v1"

_channels: dict = {}                 # eph pubkey bytes → (PrivateKey, expires_at)
_channels_lock = threading.Lock()


class ChannelError(Exception):
    """The envelope does not open: unknown or expired handshake, a box that
    fails to authenticate, or a payload that is not a JSON object."""


def padded(obj: dict) -> bytes:
    """`obj` as JSON, its length a whole number of BOX_BUCKET bytes."""
    plain = json.dumps({**obj, "_pad": ""}).encode("utf-8")
    return json.dumps({**obj, "_pad": " " * (-len(plain) % BOX_BUCKET)}).encode("utf-8")


def open_channel(server_secret: Optional[Callable[[], bytes]] = None) -> dict:
    """Mint the server half of one exchange. The browser answers with a box
    to `eph`; `sig` is the identity key's signature over
    context || eph || expires (big-endian seconds), empty without identity.
    `tag` (`server_secret` the node's secret, read only for an account):
    the same bytes and the node's public key under the device token
    (HMAC-SHA256) — what a browser that holds the token, and pinned nothing
    yet, authenticates the answer by: while the account's key is being made
    the answer is unsigned, and the first signed one is pinned only when the
    token vouches for it (SEC-S5; nobody without the token makes the tag)."""
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
    node_pubkey = _expected_pubkey() or ""
    tag = ""
    if server_secret is not None and account_configured():
        tag = hmac.new(current_token(server_secret()).encode(), signed + node_pubkey.encode(),
                       "sha256").hexdigest()
    return {
        "eph": base64.b64encode(pub).decode("ascii"),
        "expires": expires,
        "node_pubkey": node_pubkey,
        "sig": signing_key.sign(signed).hex() if signing_key is not None else "",
        "tag": tag,
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
        sealed_reply = channel.encrypt(padded(obj), reply_nonce).ciphertext
        return {"nonce": base64.b64encode(reply_nonce).decode("ascii"),
                "box": base64.b64encode(sealed_reply).decode("ascii")}

    return payload, reply
