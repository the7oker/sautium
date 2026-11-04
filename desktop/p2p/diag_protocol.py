"""Support diagnostics — the wire protocol (P2P_NETWORK.md § "Support
diagnostics"). Shared by the launcher and the Docker backend.

A WARRANT is the master's signed request for a diagnostic bundle. It is
bound to ONE target node, expires, and names only SCOPES from the fixed
enum below — never a path, a table or a query: each scope is a fixed
collector with its own allowlist (desktop/diag_bundle.py). A node acts on
a warrant only when it arrives on the node's own wake stream to the master
AND verify_warrant() passes; the first receipt is decided by the
diag_warrants primary key (diag_events.receive_warrant), so a re-delivery
resumes an unfinished upload and a finished one is a no-op. A warrant that
names a CONTENT scope waits on the node until the person decides on the
request sheet (diag_events.decide): what they leave unticked never leaves,
and the structural part goes either way. The answer
goes to the pinned master, never to an address the warrant could name —
a captured warrant buys an attacker nothing, and a warrant for node A is
inert on node B.

REPORTS are a node's proactive event notes (EVENT_KINDS): states, counters
and error strings, never content. Fields in LOCAL_ONLY_FIELDS (log tails)
stay in the node's diag_events and travel only inside a bundle the master
asked for.

Both payloads are NaCl Box between the Curve25519 keys derived from the
Ed25519 node keys — the chat construction — so only the master reads
them. Depends on `cryptography` + PyNaCl.
"""

import ipaddress
import json
import re
import time
import uuid
from datetime import datetime
from typing import Callable, Iterable, Optional, Tuple

from desktop.p2p.master_node import MASTER_PUBKEY_HEX

PROTOCOL_VERSION = 1
FRAME_TYPE = "diag_warrant"          # SSE frame type on the master's wake stream

SCOPES = ("system", "settings", "p2p", "jobs", "events", "logs", "chat", "playback")
# What a warrant asks for when the maintainer names no scopes: the ones every
# deployed node has known since the protocol shipped. A node refuses a warrant
# that names a scope it does not know, so a scope added later — `playback`,
# the HQPlayer playback traces (2026-10-02) — is asked for by name.
DEFAULT_SCOPES = ("system", "settings", "p2p", "jobs", "events", "logs", "chat")
# What the person said, played and met — their conversations with the
# assistant, log excerpts (the events' log tails ride with them), HQPlayer's
# playback traces: these leave only with the person's own tick on the request
# sheet their node shows, per warrant. The other scopes are facts about the
# node, answered while the switch is on.
CONTENT_SCOPES = ("chat", "logs", "playback")

EVENT_KINDS = frozenset({
    "node.started",
    "service.start_failed",
    "p2p.start_failed",
    "backend.crashed",
    "backend.restarted",
    "backend.gave_up",
    "agent.signin_opened",
    "agent.signin_failed",
    "agent.signin_timeout",
    "agent.state_changed",
    "chat.error",
    "sync.failed",
    "sync.import_failed",
    "update.failed",
})
# What a report may call an event: `family.name`. EVENT_KINDS is the
# vocabulary nodes emit today; the master accepts any well-formed kind so a
# node one release ahead never has a whole report refused over one new name.
_KIND_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
KIND_MAX_CHARS = 64

BUNDLE_MAX_BYTES = 32 * 1024 * 1024   # boxed tar.gz the master accepts
REPORT_MAX_BYTES = 256 * 1024         # boxed report the master accepts
REPORT_MAX_EVENTS = 50                # events per report
EVENT_DETAIL_MAX_CHARS = 4096         # one event's detail as JSON (node truncates, master refuses)
LOG_TAIL_BYTES = 2 * 1024 * 1024      # per log file inside a bundle
EVENTS_MAX_ROWS = 500                 # diag_events rows inside a bundle
CHAT_DEFAULT_DAYS = 14                # chat scope window when `since` is absent
WARRANT_TTL_DEFAULT_S = 7 * 86400     # parked warrants wait this long for the node
WARRANT_ISSUE_SKEW_S = 60             # issued_at may lead the node's clock by this much
BUNDLE_ACCEPT_GRACE_S = 3600          # the master still takes a bundle this long past expiry
REPORT_MAX_AGE_DAYS = 7               # older unreported events are never shipped
REPORT_MAX_PER_HOUR = 30              # node-side send cap and master-side per-node backstop
LOCAL_ONLY_FIELDS = frozenset({"log_tail"})


# ---------------------------------------------------------------------------
# Warrant: canonical payload + sign/verify (grant / voucher pattern)
# ---------------------------------------------------------------------------

def warrant_payload(warrant_id: str, issuer: str, target: str, issued_at: int,
                    expires_at: int, scopes: Iterable[str],
                    since: Optional[int]) -> bytes:
    return (f"sautium-diag-warrant:v{PROTOCOL_VERSION}:{warrant_id}:{issuer.lower()}:"
            f"{target.lower()}:{int(issued_at)}:{int(expires_at)}:"
            f"{','.join(sorted(scopes))}:{int(since) if since else ''}"
            ).encode("utf-8")


def sign_warrant(sign: Callable[[bytes], bytes], *, target: str, scopes: Iterable[str],
                 since: Optional[int] = None, issuer: str = MASTER_PUBKEY_HEX,
                 ttl_s: int = WARRANT_TTL_DEFAULT_S, now: Optional[float] = None,
                 warrant_id: Optional[str] = None) -> dict:
    issued_at = int(time.time() if now is None else now)
    warrant_id = warrant_id or str(uuid.uuid4())
    scopes = sorted(set(scopes))
    if not scopes or not set(scopes) <= set(SCOPES):
        raise ValueError(f"unknown scopes: {sorted(set(scopes) - set(SCOPES))}")
    expires_at = issued_at + int(ttl_s)
    since = int(since) if since else None
    sig = sign(warrant_payload(warrant_id, issuer, target, issued_at, expires_at,
                               scopes, since))
    return {
        "v": PROTOCOL_VERSION,
        "id": warrant_id,
        "issuer": issuer.lower(),
        "target": target.lower(),
        "issued_at": issued_at,
        "expires_at": expires_at,
        "scopes": scopes,
        "since": since,
        "sig": sig.hex(),
    }


def verify_warrant(warrant: dict, expected_target: str, now: Optional[float] = None,
                   issuer: str = MASTER_PUBKEY_HEX) -> Tuple[bool, Optional[str]]:
    """(True, None) for a warrant this node must act on; (False, reason)
    otherwise. `issuer` is the pinned master in production — a parameter
    only so tests can sign with a throwaway key."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    now = time.time() if now is None else now
    if not isinstance(warrant, dict) or warrant.get("v") != PROTOCOL_VERSION:
        return False, "unsupported version"
    try:
        warrant_id = str(uuid.UUID(str(warrant.get("id"))))
        issued_at = int(warrant["issued_at"])
        expires_at = int(warrant["expires_at"])
        scopes = list(warrant["scopes"])
        since = warrant.get("since")
        since = int(since) if since else None
        sig = bytes.fromhex(warrant["sig"])
    except (KeyError, TypeError, ValueError):
        return False, "malformed"
    if str(warrant.get("issuer", "")).lower() != issuer.lower():
        return False, "not from the master"
    if str(warrant.get("target", "")).lower() != expected_target.lower():
        return False, "addressed to another node"
    if not scopes or not set(scopes) <= set(SCOPES):
        return False, "unknown scope"
    if issued_at > now + WARRANT_ISSUE_SKEW_S:
        return False, "issued in the future"
    if now >= expires_at:
        return False, "expired"
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(issuer)).verify(
            sig, warrant_payload(warrant_id, issuer, expected_target, issued_at,
                                 expires_at, scopes, since))
    except (InvalidSignature, ValueError):
        return False, "bad signature"
    return True, None


def warrant_frame(warrant: dict) -> dict:
    return {"type": FRAME_TYPE, "warrant": warrant}


def asks_the_person(scopes: Iterable[str]) -> bool:
    """Whether a warrant waits for the person's decision: it names a
    content scope."""
    return not set(CONTENT_SCOPES).isdisjoint(scopes)


# ---------------------------------------------------------------------------
# Payload encryption: NaCl Box over the node keys (the chat construction)
# ---------------------------------------------------------------------------

def _curve_private(seed: bytes):
    from nacl.signing import SigningKey
    return SigningKey(seed).to_curve25519_private_key()


def _curve_public(pubkey_hex: str):
    from nacl.signing import VerifyKey
    return VerifyKey(bytes.fromhex(pubkey_hex)).to_curve25519_public_key()


def encrypt_to(seed: bytes, recipient_pubkey_hex: str, data: bytes) -> bytes:
    """nonce ‖ ciphertext, readable by `recipient` from `seed`'s owner."""
    from nacl.public import Box
    return bytes(Box(_curve_private(seed), _curve_public(recipient_pubkey_hex)).encrypt(data))


def decrypt_from(seed: bytes, sender_pubkey_hex: str, blob: bytes) -> bytes:
    """Inverse of encrypt_to; raises nacl.exceptions.CryptoError when the
    blob was not boxed between exactly these two keys."""
    from nacl.public import Box
    return Box(_curve_private(seed), _curve_public(sender_pubkey_hex)).decrypt(bytes(blob))


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def strip_local_only(detail: dict) -> dict:
    return {k: v for k, v in (detail or {}).items() if k not in LOCAL_ONLY_FIELDS}


def _bounded_detail(detail: dict) -> dict:
    detail = strip_local_only(detail)
    if len(json.dumps(detail, default=str)) <= EVENT_DETAIL_MAX_CHARS:
        return detail
    return {"truncated": True, "keys": sorted(detail)}


def encode_report(events: Iterable[dict]) -> bytes:
    """The plaintext a node boxes for /api/diag/report: content-free,
    size-bounded events only — applied here, so no caller can forget."""
    out = [{"kind": e["kind"], "ts": e["ts"], "detail": _bounded_detail(e.get("detail") or {})}
           for e in events]
    return json.dumps({"v": PROTOCOL_VERSION, "events": out}, ensure_ascii=False,
                      default=str).encode("utf-8")


def decode_report(raw: bytes) -> list:
    """Validate a decrypted report on the master; ValueError on anything
    that is not a well-formed, size-bounded list of events. Each detail
    comes back scrubbed (scrub_detail) — a node that records unscrubbed
    text, a release behind, has it kept here without what names a person
    or a place."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ValueError(f"report is not JSON: {e}")
    if not isinstance(data, dict) or data.get("v") != PROTOCOL_VERSION:
        raise ValueError("unsupported report version")
    events = data.get("events")
    if not isinstance(events, list) or not events or len(events) > REPORT_MAX_EVENTS:
        raise ValueError("report event count out of range")
    out = []
    for e in events:
        kind = e.get("kind") if isinstance(e, dict) else None
        if (not isinstance(kind, str) or len(kind) > KIND_MAX_CHARS
                or not _KIND_RE.match(kind)):
            raise ValueError("malformed event kind")
        ts = e.get("ts")
        detail = e.get("detail")
        if not isinstance(ts, str) or not isinstance(detail, dict):
            raise ValueError("malformed event")
        try:
            datetime.fromisoformat(ts)
        except ValueError:
            raise ValueError("malformed event timestamp")
        if len(json.dumps(detail, default=str)) > EVENT_DETAIL_MAX_CHARS:
            raise ValueError("event detail too large")
        out.append({"kind": e["kind"], "ts": ts, "detail": scrub_detail(strip_local_only(detail))})
    return out


# ---------------------------------------------------------------------------
# Log scrubbing — logs are collected as text and MAY have caught a secret
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "[redacted]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [redacted]"),
    (re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd)(\"?\s*[:=]\s*\"?)[^\s\"',;&]+"),
     r"\1\2[redacted]"),
    (re.compile(r"(?i)\b(postgres(?:ql)?://[^:/\s@]+:)[^@\s]+@"), r"\1[redacted]@"),
    (re.compile(r"\b[0-9a-f]{128}\b"), "[redacted]"),          # signatures, key material
)


def scrub_secrets(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


# ---------------------------------------------------------------------------
# Personal scrubbing — what leaves for support names no person, place or file
# ---------------------------------------------------------------------------

# An address's start is where its run of address characters starts — a
# start inside a long token (a hex blob in a database log) would make the
# search quadratic; a top-level domain is letters, so a package's version
# (`name@18.3.1`) is no address, while a name at a numeric host
# (`someone@192.0.2.5`) is one.
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@(?:(?:[^\W_](?:[\w-]*[^\W_])?\.)+[^\W\d_]{2,}"
                    r"(?![\w-]|\.[\w-])|\d{1,3}(?:\.\d{1,3}){3}(?!\w|\.\d))")
# A sentence's full stop may follow an address; a fifth number may not (a
# version string).
_IPV4 = re.compile(r"(?<!\w)(?<!\d\.)\d{1,3}(?:\.\d{1,3}){3}(?!\w|\.\d)")
# Colon groups an IPv6 address may be; ipaddress decides (a clock time or a
# MAC address is none).
_IPV6 = re.compile(r"(?<![\w:.])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![\w:])")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
# The names a tailnet, mDNS or a home router give a machine name its owner;
# a public service's name is no one's. Under `.local` and `.home` a name is a
# machine's where a URL names it, where a port follows it, or where it looks
# like one (a hyphen, a digit, a capital: "Someones-MacBook.local") — a
# lowercase dotted word there is a module's (`streaming.local`,
# `routers.home`); so is a single label in a URL's host (`http://pixel:8200`).
# A name ends where its characters do.
_HOME_SUFFIX = r"(?i:ts\.net|home\.arpa|localdomain|fritz\.box|lan|internal)"
_MODULE_SUFFIX = r"(?i:local|home)"
_NAME_END = r"(?![\w(-]|\.\w)"
_HOST = re.compile(
    rf"(?<![\w.-])(?:[\w-]+\.)+{_HOME_SUFFIX}{_NAME_END}"
    rf"|(?<=://)(?:[\w-]+\.)+{_MODULE_SUFFIX}{_NAME_END}"
    rf"|(?<=://)[\w-]+(?=[:/\s'\"]|$)"
    rf"|(?<![\w.-])(?:[\w-]+\.)+{_MODULE_SUFFIX}(?=:\d)"
    rf"|(?<![\w.-])(?=[\w.-]*[-A-Z0-9])(?:[\w-]+\.)+{_MODULE_SUFFIX}{_NAME_END}")
_SAFE_HOSTS = {"host.docker.internal", "localhost"}
# A path's folders name an account, a person, a disk; its end is known only
# where it is quoted, so a folder may hold single spaces only where another
# separator follows it, an apostrophe (`Someone's`), and a colon but not
# before `//` — a URL later on the line.
_SEG = r"(?:[^\\/\s\"<>|*?:]|:(?!//))+(?: (?:[^\\/\s\"<>|*?:]|:(?!//))+)*"
_TAIL = r"[^\s'\"<>|]*"
# The extensions of a library's files, its cue sheets, playlists and covers:
# such a file's name is a track's or an album's title. It is cut to its
# extension, and it ends an unquoted path, spaces and all.
_MEDIA_EXT = (r"(?i:flac|mp3|m4a|m4b|mp4|aac|alac|ape|wv|wave?|aiff?|aifc|dsf|dff|ogg|oga|opus"
              r"|wma|mka|tak|tta|mpc|ac3|dts|cue|m3u8?|pls|jpe?g|png|webp|gif|bmp|tiff?)")
_MEDIA_TAIL = rf"""[^\\/\n"<>|]*?\.{_MEDIA_EXT}(?![\w])"""
_MEDIA_NAME = re.compile(rf".+?\.({_MEDIA_EXT})(?:\.[A-Za-z0-9]{{1,5}}){{0,2}}")
_ROOTS = (r"home|Users|mnt|media|Volumes|volume\d+|homes|music|Music|data|srv|net|nfs|share"
          r"|storage|app|tmp|var|opt|usr|etc|root|run|private|Library|System")
# A share URL is a path. A library file's may hold spaces up to its extension
# (a URI the node hands HQPlayer is not percent-encoded); a colon ends that
# reading — another URL's scheme — but a drive's does not.
_SHARE_URL = re.compile(
    rf"""\b(file|smb|nfs|afp|sftp)://(?:(?:[^\n"<>:]|(?<=/[A-Za-z]):)*?\.{_MEDIA_EXT}(?![\w])"""
    rf"""|[^\s'"<>]*)""", re.IGNORECASE)
# A path starts at a drive (before a separator, after `\\?\` too); at a
# server — `\\NAS`, or `\\\\NAS` as Python's error text doubles it, or
# `//NAS/share` — whose name ends where its characters do (one way to read
# it, not one per character); at `~` or `~user` before a separator; at a
# root folder.
_PATH_START = (rf"""(?:(?:(?:\\\\){{1,2}}\?(?:\\\\|[\\/]))?[A-Za-z]:(?=[\\/])"""
               rf"""|\\\\(?:\\\\)?[^\\/\s'"<>|]+(?![^\\/\s'"<>|])"""
               rf"""|(?<!:)//[^/\s'"<>|]+(?=/)|~[\w.-]*(?=[\\/])|/(?:{_ROOTS})(?=[\\/\s]|$))""")
# A Windows account's folder may hold spaces (`C:\Users\Some One`): after
# the users root — `\\?\` before the drive, separators doubled as Python's
# error text prints them, WSL's `/mnt/c` — all up to the next separator,
# quote or line end is its name. A POSIX account's name holds none.
_SEP = r"(?:\\\\|[\\/])"
_ACCOUNT_NAME = r"""[^\\/\n"<>|]*(?![^\\/\n"<>|])"""
_ACCOUNT = (rf"""(?:(?:\\\\){{1,2}}\?{_SEP})?[A-Za-z]:{_SEP}(?i:users){_SEP}{_ACCOUNT_NAME}"""
            rf"""|/mnt/[a-z]/(?i:users)/{_ACCOUNT_NAME}""")
# Quoted by either mark, a path runs to its closing one: Python quotes a
# string holding an apostrophe with `"`.
_QUOTED_PATH = re.compile(rf"""(')({_PATH_START}[^'\n]*)'|(")({_PATH_START}[^"\n]*)["]""")
# A part that holds a library file's name before a space or a mark is the
# file, never a folder: what follows it is the message (`…: Input/output
# error`), however many `/` the message holds.
_BARE_PATH = re.compile(
    rf"""(?<![\w./\\~-])(?:{_ACCOUNT}|{_PATH_START})"""
    rf"""(?:[\\/](?![^\\/\n"<>|]*?\.{_MEDIA_EXT}[^\w\\/]){_SEG}(?=[\\/]))*"""
    rf"""(?:[\\/](?:{_MEDIA_TAIL}|{_TAIL}))?""")
_WHOLE_PATH = re.compile(rf"(?:{_ACCOUNT}|{_PATH_START})[^\n]*")
# What a path was cut to before — this scrubber's `…/<file>`, the log's
# `…/<folder>/<file>` (hqplayer_client.redact_path) — still names a title.
_ELIDED = re.compile(rf"""…[\\/][^\n"<>…]*?\.({_MEDIA_EXT})(?![\w])""")
# The keys a structured detail keeps a path under: its value is a path from
# start to end, spaces and all.
_PATHISH_KEY = re.compile(r"(?i)(?:^|[_.-])(?:path|dir|directory|folder|file|root)s?$")
_FILE_NAME = re.compile(r"[^\\/]+\.[A-Za-z0-9]{1,5}")
_ACCOUNT_DIRS = {"users", "home", "homes"}


def _address(match: "re.Match") -> str:
    try:
        ip = ipaddress.ip_address(match.group(0))
    except ValueError:
        return match.group(0)
    if ip.is_unspecified or ip.is_multicast or str(ip) == "255.255.255.255":
        return match.group(0)
    if ip.is_loopback:
        return "[loopback address]"
    if ip.version == 4 and ip in _CGNAT:
        return "[carrier-grade NAT address]"
    if ip.is_link_local:
        return "[link-local address]"
    if ip.is_private:
        return "[private address]"
    return "[public address]"


def _host(match: "re.Match") -> str:
    return match.group(0) if match.group(0).lower() in _SAFE_HOSTS else "[host]"


def _cut_path(path: str) -> str:
    """A file keeps its name, unless a library's file — a title — keeps only
    its extension; anything else — a folder, which may be an account's, and
    an account's folder whatever its name looks like — is only a path."""
    parts = [p for p in re.split(r"[\\/]+", path.rstrip("\\/")) if p]
    name = parts[-1] if parts else ""
    if len(parts) >= 2 and parts[-2].lower() in _ACCOUNT_DIRS:
        return "[path]"
    media = _MEDIA_NAME.fullmatch(name)
    if media:
        return f"…/[name].{media.group(1)}"
    return "…/" + name if _FILE_NAME.fullmatch(name) else "[path]"


def _quoted(match: "re.Match") -> str:
    quote = match.group(1) or match.group(3)
    return quote + _cut_path(match.group(2) or match.group(4)) + quote


def scrub_personal(text: str) -> str:
    """scrub_secrets, then what names a person, a place or a title: email
    addresses, IP addresses (each to its kind), a home network's host names,
    absolute paths (a file to its name, a library's file to its extension)."""
    text = scrub_secrets(text)
    text = _ELIDED.sub(lambda m: f"…/[name].{m.group(1)}", text)
    text = _SHARE_URL.sub(lambda m: m.group(1) + "://[path]", text)
    text = _QUOTED_PATH.sub(_quoted, text)
    text = _BARE_PATH.sub(lambda m: _cut_path(m.group(0)), text)
    text = _EMAIL.sub("[email]", text)
    text = _HOST.sub(_host, text)
    text = _IPV4.sub(_address, text)
    return _IPV6.sub(_address, text)


def scrub_detail(value, key: str = ""):
    """scrub_personal over every string an event's detail holds, at any depth,
    keys included. A value under a key that names a path (`music_path`,
    `current_file`, `folder`…) is one path from its start to its end, spaces
    and all; every value then goes through scrub_personal whole. Two keys
    that scrub alike (two folders' `x.flac`) keep both values."""
    if isinstance(value, str):
        if _PATHISH_KEY.search(key) and _WHOLE_PATH.fullmatch(value):
            value = _cut_path(value)
        return scrub_personal(value)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            name = base = scrub_personal(k) if isinstance(k, str) else k
            n = 1
            while name in out:
                n += 1
                name = f"{base} ({n})"
            out[name] = scrub_detail(v, k if isinstance(k, str) else "")
        return out
    if isinstance(value, list):
        return [scrub_detail(v, key) for v in value]
    return value
