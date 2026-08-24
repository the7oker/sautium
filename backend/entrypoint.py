"""Docker entrypoint: ensure TLS cert exists, then start uvicorn over HTTPS.

Cert is materialised in /app/data/tls (mounted from ./data/tls on the host)
so it survives container rebuilds. Extra LAN IPs to include in the SAN
are read from SAUTIUM_HOST_IPS (comma-separated).
"""

import os
import sys
from pathlib import Path

import uvicorn

from tls_gen import ensure_cert

DATA_DIR = Path("/app/data/tls")
HOST = os.getenv("UVICORN_HOST", "0.0.0.0")
PORT = int(os.getenv("UVICORN_PORT", "8000"))
RELOAD = os.getenv("UVICORN_RELOAD", "true").lower() in ("1", "true", "yes")

# HuggingFace model snapshots required by the pre-warm pipeline.
HF_REQUIRED_MODELS = (
    "models--laion--clap-htsat-unfused",
    "models--BAAI--bge-m3",
    "models--MIT--ast-finetuned-audioset-10-10-0.4593",
)


def _parse_extra_ips() -> list[str]:
    raw = os.getenv("SAUTIUM_HOST_IPS", "")
    return [s.strip() for s in raw.split(",") if s.strip()]


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
    # The peer channel binding (node key over the TLS SPKI) rides in this
    # same cert — it is what the master's Caddy front serves too. Argon2id
    # for an env-derived identity costs ~1-2 s, once per boot.
    from config import settings
    from p2p_identity import tls_binding
    cert_path, key_path = ensure_cert(DATA_DIR, _parse_extra_ips(),
                                      binding=tls_binding(settings))
    _enable_hf_offline_if_cached()
    print(f"[entrypoint] TLS cert: {cert_path}", flush=True)
    print(f"[entrypoint] uvicorn HTTPS on {HOST}:{PORT} (reload={RELOAD})", flush=True)
    uvicorn.run(
        "main:app",
        host=HOST,
        port=PORT,
        reload=RELOAD,
        ssl_keyfile=str(key_path),
        ssl_certfile=str(cert_path),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
