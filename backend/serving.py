"""The backend's uvicorn server, as a node runs it — the Docker entrypoint
(not its development reload mode), the launcher's (desktop/backend_serve.py)
and the peer surface inside the app (main._serve_p2p): one that says the
signal stopping it. uvicorn stops on SIGINT and SIGTERM without a word of
which; a hangup, which it does not handle, ended the process at Python's
default — no word in the log, no lifespan shutdown (2026-10-18).

Light at import: the entrypoint imports it before the database is up.
"""

import logging
import signal

import uvicorn

logger = logging.getLogger(__name__)


class Server(uvicorn.Server):
    """uvicorn's server, saying the first signal that stops it and what it
    stops (`what`). A signal while it stops changes nothing: uvicorn would
    cut the lifespan's shutdown short at a SIGINT after the first signal
    (its force exit), and one arrives whenever a session's end signals each
    of its processes and the launcher, still quitting, stops the backend
    with SIGINT. So uvicorn's "(CTRL+C to force quit)" does not hold here: a
    stop that hangs is the launcher's or Docker's to kill."""

    def __init__(self, config: uvicorn.Config, what: str = "the backend") -> None:
        super().__init__(config)
        self.what = what

    def handle_exit(self, sig, frame) -> None:
        if self.should_exit:
            return
        logger.info("%s received: %s shuts down", signal.Signals(sig).name, self.what)
        super().handle_exit(sig, frame)


def heard_until_served() -> None:
    """Until a server holds them: SIGTERM, SIGINT and a hangup end the
    process at once, said first. PID 1 in a container has no default action
    for SIGTERM, so a stop during the wait for the database was not heard
    at all and waited out the grace's SIGKILL."""
    for sig in _STOPS:
        signal.signal(sig, _before_served)


def served() -> None:
    """The signals handed to the server about to start: SIGTERM and SIGINT
    back at their defaults — the server takes them, and restores and raises
    again what it found when it stops — and a hangup stopping it as SIGTERM
    does, which uvicorn does not handle. From the main thread, as every
    signal handler."""
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _hangup)


# The stops a node hears before it serves; Windows has no SIGHUP
_STOPS = tuple(getattr(signal, name) for name in ("SIGTERM", "SIGINT", "SIGHUP")
               if hasattr(signal, name))


def _before_served(sig, frame) -> None:
    logger.info("%s received before the server started: the backend stops",
                signal.Signals(sig).name)
    raise SystemExit(0)


def _hangup(sig, frame) -> None:
    logger.info("SIGHUP received: taken as SIGTERM")
    signal.raise_signal(signal.SIGTERM)
