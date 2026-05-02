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


def _parse_extra_ips() -> list[str]:
    raw = os.getenv("SAUTIUM_HOST_IPS", "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def main() -> int:
    cert_path, key_path = ensure_cert(DATA_DIR, _parse_extra_ips())
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
