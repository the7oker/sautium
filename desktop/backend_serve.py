"""How the launcher runs the backend: uvicorn, as `python -m uvicorn main:app`
would, plus the one thing the CLI cannot do — say when the server is bound
and serving. The launcher LISTENs on READY_CHANNEL (service_manager,
_await_ready) instead of polling /health; the token it passes in
SAUTIUM_START_TOKEN tells this start from an earlier one's. Docker runs
uvicorn directly and needs none of it.

Both ends of the signal live here, so the channel is written once. Light at
import: the launcher's interpreter imports this module and has no uvicorn.

    python -m desktop.backend_serve <port>     (cwd: backend/)
"""

import os
import sys

READY_CHANNEL = "sautium_backend"


def listen_for_ready(dsn: str):
    """The launcher's end: a connection LISTENing on READY_CHANNEL. Open it
    before the spawn — a backend that comes up fast must not NOTIFY into
    nobody."""
    import psycopg2
    conn = psycopg2.connect(dsn, connect_timeout=5)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"LISTEN {READY_CHANNEL}")
    return conn


def announce_ready(dsn: str, token: str) -> None:
    """The backend's end, once uvicorn is bound."""
    import psycopg2
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT pg_notify(%s, %s)", (READY_CHANNEL, token))
    finally:
        conn.close()


def main() -> None:
    import uvicorn

    token = os.environ["SAUTIUM_START_TOKEN"]

    class AnnouncingServer(uvicorn.Server):
        async def startup(self, sockets=None):
            # Lifespan first (migrations, the model imports a pre-warming
            # profile holds the loop for), then the bind: when this returns
            # with `started`, a request is answered, not refused.
            await super().startup(sockets=sockets)
            if self.started:
                from config import settings
                announce_ready(settings.database_url, token)

    config = uvicorn.Config("main:app", host="0.0.0.0", port=int(sys.argv[1]),
                            timeout_graceful_shutdown=5)
    AnnouncingServer(config).run()


if __name__ == "__main__":
    main()
