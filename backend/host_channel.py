"""The node's side of the host agent (scripts/host_agent.py; PI-NODE-1 F2):
what the node may ask of the machine it runs on, and how the answer comes
back.

A request is the NAME of an empty file in the folder the agent watches — one
of VERBS, and nothing else: the agent reads no content, so there is no parser
of the node's words on the host's side. The node writes it there through a
bind of that folder alone (docker-compose.pi.yml); the agent empties the
folder by handles, never following what the node put in it.

The answer comes back through this node's own API, signed with the CHANNEL'S
key (`SAUTIUM_HOST_CHANNEL_KEY`, from the agent's own folder `./host/`, which
no container mounts) — never with `.api_secret`, which lives where the node
writes: the middleware admits that key on ANSWER_PATH alone
(auth_hmac.HMACAuthMiddleware). A node controlled by an attacker knows the
key too, and gains by it what it has already: it can tell its own page
anything.

The node knows from its environment whether the channel exists: no key, no
agent installed — a page then says the command to run on the machine
instead (PI-NODE-2B).

The diagnostics report is JSON by section (format 2, since 2026-11-21,
B-99): the machine's facts and the containers' logs apart by NAME, so a
support bundle takes the facts as structural (`system`) and the logs only in
a ticked `logs` — no line a container logged can move into the facts. An
answer wakes whoever waits for it on this process's loop (`expect`,
`answered`): the support worker asks and waits for the event, never polls.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

VERBS = ("check", "update", "diagnostics")
# What each verb's answer may say (the host_outcome ENUM, 001_initial.sql)
OUTCOMES = {
    "check": {"current", "offered", "rewrite", "refused", "floor", "failed"},
    "update": {"current", "updated", "undone", "refused", "floor", "failed"},
    "diagnostics": {"done", "floor", "failed"},
}
ANSWER_PATH = "/api/host/answer"
REQUESTS_DIR = Path(os.getenv("SAUTIUM_HOST_REQUESTS", "/run/sautium/requests"))
# An answer's text fields, as the agent bounds them (scripts/host_agent.py)
DETAIL_MAX = 2000
RELEASE_MAX = 64
REPORT_MAX = 4 * 1024 * 1024
# The diagnostics report's form and sections (scripts/host_agent.py says the
# same; a test holds them together), and the agent's floor on making one
REPORT_FORMAT = 2
MACHINE_SECTIONS = ("versions", "board", "power", "disk", "containers", "restarts")
LOG_SECTIONS = {"log: backend": "backend.log",
                "log: backend before the last update": "backend-before-update.log",
                "log: postgres": "postgres.log", "log: the host agent": "host-agent.log"}
DIAGNOSTICS_FLOOR = 600


def _read_key() -> Optional[bytes]:
    key = os.getenv("SAUTIUM_HOST_CHANNEL_KEY", "").strip()
    return key.encode("ascii") if len(key) == 64 and all(c in "0123456789abcdef" for c in key) else None


# Read at import, before the lifespan drops the credentials from the
# environment (serving.hold_secrets: config.SECRET_ENV names it), so no child
# of the node — an agent CLI, ffmpeg — inherits it
_KEY = _read_key()


def channel_key() -> Optional[bytes]:
    """The channel's key as the agent signs with it, or None when this node
    has no agent. The key is the hex text of 32 random bytes."""
    return _KEY


def available() -> bool:
    return channel_key() is not None


class NoChannel(RuntimeError):
    """This node has no host agent, or its requests folder is not mounted."""


def ask(verb: str) -> None:
    """Leave the request for `verb`: an empty file of that name in the
    requests folder. A request already waiting is the same request. The
    folder is the bind's, and whatever is in it is the node's own; the file
    is made without following a link the name may already be."""
    if verb not in VERBS:
        raise ValueError(f"no such request: {verb}")
    if not available():
        raise NoChannel("this node has no host agent")
    try:
        folder = os.open(REQUESTS_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as e:
        raise NoChannel(f"the requests folder {REQUESTS_DIR} cannot be opened: {e.strerror}") from e
    try:
        os.close(os.open(verb, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=folder))
    finally:
        os.close(folder)
    logger.info("asked the host agent: %s", verb)


def read_report(text: Optional[str]) -> Optional[dict]:
    """A kept diagnostics report as {"made_at": epoch, "sections": {name:
    text}} — the known sections alone, each a text — or None: no report, or
    one in a form this release does not read (an agent's body of an older
    release wrote one text)."""
    try:
        data = json.loads(text or "")
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("v") != REPORT_FORMAT \
            or not isinstance(data.get("made_at"), int) or not isinstance(data.get("sections"), dict):
        return None
    known = (*MACHINE_SECTIONS, *LOG_SECTIONS)
    return {"made_at": data["made_at"],
            "sections": {name: value for name, value in data["sections"].items()
                         if name in known and isinstance(value, str)}}


def report_page(text: str) -> dict:
    """The report as the page shows it — text, read by a person and never
    parsed — and when it was made."""
    report = read_report(text)
    if report is None:
        return {"report": text, "made_at": None}
    body = "".join(f"== {name}\n{value}\n" for name, value in report["sections"].items())
    return {"report": body,
            "made_at": datetime.fromtimestamp(report["made_at"], tz=timezone.utc).isoformat()}


# Who waits for an answer of a verb, on the loop the answer route runs on
_waiters: dict = {}


def expect(verb: str) -> "asyncio.Future":
    """A future the next answer of `verb` completes with its outcome — made
    BEFORE the request is left, so an answer as quick as the agent can be
    is not missed. On this process's loop; the caller drops it with
    `forget` when it stops waiting."""
    future = asyncio.get_running_loop().create_future()
    _waiters.setdefault(verb, set()).add(future)
    return future


def forget(verb: str, future: "asyncio.Future") -> None:
    _waiters.get(verb, set()).discard(future)


def answered(verb: str, outcome: str) -> None:
    """The answer route took an answer of `verb`: whoever waits is woken."""
    for future in _waiters.pop(verb, set()):
        if not future.done():
            future.set_result(outcome)
