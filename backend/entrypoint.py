"""Docker entrypoint: wait for the database, then start uvicorn on plain HTTP.

It runs as the node's user: the image's own entrypoint (node_user.py, root's,
outside /app) hands the node's folders to that user and drops root before it
starts this one.

The Web UI rides HTTP on the LAN (PROGRESS.md "HTTP on the LAN"): a
certificate a phone would trust cannot exist for a private address, and
every credential exchange is boxed end to end instead. The peer surface
(p2p_app.py, port 8801) keeps its own TLS, pinned to the node key.
"""

import logging.config
import os
import sys
import time

import psycopg2
import uvicorn
from uvicorn.main import STARTUP_FAILURE

from config import LOGGING_CONFIG, settings
from serving import Server, heard_until_served, served, undumpable

HOST = os.getenv("UVICORN_HOST", "0.0.0.0")
PORT = int(os.getenv("UVICORN_PORT", "8000"))
RELOAD = os.getenv("UVICORN_RELOAD", "true").lower() in ("1", "true", "yes")


def _wait_for_database() -> None:
    """Block until PostgreSQL accepts a connection.

    Docker's restart policy restores containers without the `depends_on`
    ordering `compose up` honours, so after a daemon restart the backend
    comes up before postgres — seconds before it normally, hours before it
    when the postgres restore itself failed and a human has to press start
    (2026-09-22). Nothing past this point works without the database:
    mb_backend reads it at import, the lifespan migrates it before anything
    serves. So the process waits here, as the launcher's
    service_manager._wait_for_postgres does before it spawns uvicorn, instead
    of starting half-initialized and dying on the first unguarded query.
    Before a connection exists there is no event to subscribe to; the probe
    is the only signal, the same one the healthcheck uses.
    """
    started = time.monotonic()
    delay = 1.0
    last_report = None
    while True:
        try:
            psycopg2.connect(settings.database_url, connect_timeout=5).close()
        except psycopg2.OperationalError as e:
            waited = time.monotonic() - started
            if last_report is None or waited - last_report >= 30:
                reason = str(e).strip().splitlines()[0]
                print(f"[entrypoint] database unreachable, waiting ({waited:.0f}s): {reason}",
                      flush=True)
                last_report = waited
            time.sleep(delay)
            delay = min(delay * 2, 10.0)
            continue
        if last_report is not None:
            print(f"[entrypoint] database reachable after {time.monotonic() - started:.0f}s",
                  flush=True)
        return


def main() -> int:
    undumpable()
    # The app's own logging from the first line, as main.py sets it at its
    # import: a stop heard before then is said too
    logging.config.dictConfig(LOGGING_CONFIG)
    if os.geteuid() == 0:
        # An image built before the node ran as its own user: its entrypoint
        # starts this one as root
        logging.getLogger("entrypoint").warning(
            "the backend runs as root: this container's image predates the node's own user; "
            "rebuild it (docker compose up -d --build)")
    heard_until_served()
    _wait_for_database()
    print(f"[entrypoint] uvicorn HTTP on {HOST}:{PORT} (reload={RELOAD})", flush=True)
    served()
    # Without a limit uvicorn waits on open requests until Docker kills the
    # process, and the lifespan's shutdown — a benchmark run putting the
    # owner's HQPlayer back — never runs (exit 137, 2026-10-03: the Web UI's
    # event streams, which serving.Server ends as its stop begins since
    # 2026-10-25; the reload path's own server does not). The compose files
    # give the stop 10 s (stop_grace_period); these 2 are the open requests'
    # share, after the peer surface's 1 (main._PEER_GRACE_S: SIGTERM reaches
    # it first).
    if RELOAD:
        # The development path: the reload supervisor runs the server in a
        # process of its own
        uvicorn.run("main:app", host=HOST, port=PORT, reload=True, timeout_graceful_shutdown=2)
        return 0
    # Built here, not by uvicorn.run, which imports the app before its server
    # holds the signals: PID 1 has no default action for SIGTERM, so a stop
    # during the import was not heard at all
    server = Server(uvicorn.Config("main:app", host=HOST, port=PORT, timeout_graceful_shutdown=2,
                                   log_config=LOGGING_CONFIG))
    try:
        server.run()
    except KeyboardInterrupt:
        pass            # SIGINT, raised again as the server leaves, once stopped in order
    return 0 if server.started else STARTUP_FAILURE


if __name__ == "__main__":
    sys.exit(main())
