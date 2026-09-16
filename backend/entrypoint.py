"""Docker entrypoint: start uvicorn on plain HTTP.

The Web UI rides HTTP on the LAN (PROGRESS.md "HTTP on the LAN"): a
certificate a phone would trust cannot exist for a private address, and
every credential exchange is boxed end to end instead. The peer surface
(p2p_app.py, port 8801) keeps its own TLS, pinned to the node key.
"""

import os
import sys
from pathlib import Path

import uvicorn

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


def main() -> int:
    _enable_hf_offline_if_cached()
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
