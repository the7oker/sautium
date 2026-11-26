"""The pairing code and its QR, from inside a Docker node's container (SEC-S5 §18).

    docker compose exec backend python -m pairing

For the nodes the console (`./sautium pair`) does not serve — Docker Desktop
on Windows or macOS, a Linux node set up by hand: the code an anonymous
node's browsers sign in with (the master's included — one logic for every
node), asked of the node's own API on loopback and signed as the host with
the node's API secret, as the launcher and the console sign.

`present` is the one presentation of a printed QR: the code first, its value
as text under it — the QR is for a phone, the text for a browser on the same
machine. This command uses it, and the console draws its QRs with this
same function, read from the checkout and run in a container of the image
that sees no network (scripts/setup.py `qr`). So this module imports the
standard library and `qrcode` alone at its top.
"""

import io

# The Web UI's port as a device reaches it: every compose file publishes it
# there (the Linux file's node binds it itself), whatever the container
# listens on behind a bridge
PUBLISHED_WEB_PORT = 8800


def present(text: str) -> str:
    """`text` as a QR of block characters, then `text` itself on a line of
    its own after a blank one."""
    import qrcode
    code = qrcode.QRCode(border=2)
    code.add_data(text)
    code.make(fit=True)
    drawn = io.StringIO()
    code.print_ascii(out=drawn, invert=True)
    return drawn.getvalue().rstrip("\n") + "\n\n" + text + "\n"


def main() -> int:
    import hashlib
    import json
    import os
    import secrets
    import sys
    import time
    import urllib.error
    import urllib.request
    from pathlib import Path

    import device_links
    from auth_hmac import sign
    from config import settings
    try:
        secret = (Path(settings.p2p_identity_dir) / ".api_secret").read_bytes().strip()
    except FileNotFoundError:
        print("This node has no API secret yet: start it first (docker compose up -d).", file=sys.stderr)
        return 1
    path = "/api/auth/pin"
    ts, nonce, digest = str(int(time.time())), secrets.token_hex(16), hashlib.sha256(b"").hexdigest()
    signature = sign(secret, "GET", path, ts, b"", nonce)
    request = urllib.request.Request(
        f"http://127.0.0.1:{os.getenv('UVICORN_PORT', '8000')}{path}",
        headers={"X-Sautium-Ts": ts, "X-Sautium-Nonce": nonce, "X-Sautium-Body-Sha256": digest,
                 "X-Sautium-Sig": signature})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            code = json.loads(response.read(65536))["code"]
    except urllib.error.HTTPError as e:
        print(f"The node gave no pairing code ({e.code}): {e.read(400).decode('utf-8', 'replace')}",
              file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"The node does not answer on its own port: {e}. Is it running?", file=sys.stderr)
        return 1
    addresses = device_links._own_addresses()
    host = addresses[0]["address"] if addresses else "127.0.0.1"
    print("Open on the other device within a few minutes:\n")
    print(present(f"http://{host}:{PUBLISHED_WEB_PORT}/#pair={code}"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
