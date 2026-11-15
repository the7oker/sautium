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
"""

import logging
import os
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
