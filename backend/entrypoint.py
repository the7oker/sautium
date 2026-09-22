"""Docker entrypoint: wait for the database, then start uvicorn on plain HTTP.

The Web UI rides HTTP on the LAN (PROGRESS.md "HTTP on the LAN"): a
certificate a phone would trust cannot exist for a private address, and
every credential exchange is boxed end to end instead. The peer surface
(p2p_app.py, port 8801) keeps its own TLS, pinned to the node key.
"""

import os
import sys
import time
from pathlib import Path

import psycopg2
import uvicorn

from config import settings

HOST = os.getenv("UVICORN_HOST", "0.0.0.0")
PORT = int(os.getenv("UVICORN_PORT", "8000"))
RELOAD = os.getenv("UVICORN_RELOAD", "true").lower() in ("1", "true", "yes")

# HuggingFace model snapshots required by the pre-warm pipeline.
HF_REQUIRED_MODELS = (
    "models--laion--clap-htsat-unfused",
    "models--BAAI--bge-m3",
    "models--MIT--ast-finetuned-audioset-10-10-0.4593",
)


def _enable_hf_offline_if_cached() -> None:
    """Skip HF Hub eTag HEAD checks when all models are already cached.

    On bad network, each `from_pretrained` blocks ~50s on retries before
    falling back to the local snapshot. With every required model present
    on disk, the network check is wasted work — switch to offline mode so
    Discovery + enrichment start immediately. First install (empty cache)
    still gets online mode and downloads normally.
    """
    cache_root = Path(os.getenv("HF_HOME", "/root/.cache/huggingface")) / "hub"
    if not cache_root.is_dir():
        return
    for name in HF_REQUIRED_MODELS:
        snapshots = cache_root / name / "snapshots"
        if not snapshots.is_dir() or not any(snapshots.iterdir()):
            print(f"[entrypoint] HF cache miss for {name}; staying online", flush=True)
            return
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    print("[entrypoint] All HF model snapshots cached → offline mode", flush=True)


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
    _enable_hf_offline_if_cached()
    _wait_for_database()
    print(f"[entrypoint] uvicorn HTTP on {HOST}:{PORT} (reload={RELOAD})", flush=True)
    uvicorn.run(
        "main:app",
        host=HOST,
        port=PORT,
        reload=RELOAD,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
