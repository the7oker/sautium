"""The Docker peer surface writes no access line — a peer's address and the
text of a search it sent are not the node's log —, takes no WebSocket
upgrade (uvicorn's WebSocket protocol logs each handshake with both), and
leaves the process's logging as its start made it: the Web UI's access log,
which shares uvicorn's access logger in the same process, keeps its handler
and its query filter (serving.PeerSurfaceConfig with serving.peer_protocol,
as main._serve_p2p builds both of its servers: plain behind a trusted front,
TLS otherwise).
A real uvicorn server on loopback, in a process of its own."""

import ast
import subprocess
import sys

from tests.conftest import REPO_ROOT

BACKEND = REPO_ROOT / "backend"

DRIVER = r"""
import asyncio, base64, json, logging, logging.config, os, socket, ssl, sys, tempfile, urllib.request
from pathlib import Path
sys.path.insert(0, %(backend)r)
from config import LOGGING_CONFIG
logging.config.dictConfig(LOGGING_CONFIG)
import uvicorn
from uvicorn.protocols.http.auto import AutoHTTPProtocol
import tls_gen
from serving import PeerSurfaceConfig, peer_protocol

lines, said = [], []
class Catch(logging.Handler):
    def __init__(self, into):
        super().__init__()
        self.into = into
    def emit(self, record):
        self.into.append(record.getMessage())
access = logging.getLogger("uvicorn.access")
access.addHandler(Catch(lines))
logging.getLogger("uvicorn.error").addHandler(Catch(said))
before = (list(access.handlers), list(access.filters))

async def app(scope, receive, send):
    if scope["type"] == "websocket":            # what an app with no WebSocket route answers
        await send({"type": "websocket.close", "code": 1000})
        return
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})

cert, key = tls_gen.ensure_cert(Path(tempfile.mkdtemp()))
served = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
served.load_cert_chain(str(cert), str(key))
asking = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
asking.check_hostname = False
asking.verify_mode = ssl.CERT_NONE

def upgrade(port, tls):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    if tls:
        s = asking.wrap_socket(s)
    try:
        nonce = base64.b64encode(os.urandom(16))
        s.sendall(b"GET /api/mb/search?q=a+private+upgrade HTTP/1.1\r\nHost: x\r\n"
                  b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                  b"Sec-WebSocket-Key: " + nonce + b"\r\nSec-WebSocket-Version: 13\r\n\r\n")
        return s.recv(4096)
    finally:
        s.close()

kept = True
def serve(tls):
    global kept
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    http = peer_protocol(AutoHTTPProtocol, head_s=10, head_max=64 * 1024, stall_s=30,
                         max_connections=64, per_address=None, tls=served if tls else None)
    config = PeerSurfaceConfig(app, host="127.0.0.1", port=port, http=http)
    kept = kept and (list(access.handlers), list(access.filters)) == before
    server = uvicorn.Server(config)

    async def main():
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(server.serve())
        while not server.started and not task.done():
            await asyncio.sleep(0.01)
        if task.done():
            task.result()
        url = f"{'https' if tls else 'http'}://127.0.0.1:{port}/api/mb/search?q=a+private+search"
        body = await loop.run_in_executor(None, lambda: urllib.request.urlopen(
            url, timeout=5, context=asking if tls else None).read())
        answer = await loop.run_in_executor(None, upgrade, port, tls)
        server.should_exit = True
        await task
        return {"body": body.decode(), "upgrade": answer.split(b"\r\n", 1)[0].decode()}

    return asyncio.run(main())

out = {"plain": serve(False), "tls": serve(True)}
print(json.dumps({**out, "kept": kept, "access_lines": lines,
                  "upgrade_said": [l for l in said if "private" in l or "WebSocket" in l
                                   or "upgrade" in l]}))
"""


def test_the_peer_surface_logs_no_access_line_and_leaves_the_web_uis_log_alone():
    proc = subprocess.run([sys.executable, "-c", DRIVER % {"backend": str(BACKEND)}],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    import json
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    served = {"body": "ok", "upgrade": "HTTP/1.1 200 OK"}
    assert out == {"plain": served, "tls": served, "kept": True,
                   "access_lines": [], "upgrade_said": []}


def _name(func) -> str:
    return getattr(func, "id", None) or getattr(func, "attr", None)


def test_both_peer_surface_servers_use_it():
    tree = ast.parse((BACKEND / "main.py").read_text(encoding="utf-8"))
    serve = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == "_serve_p2p")
    built = [c for c in ast.walk(serve) if isinstance(c, ast.Call)
             and isinstance(c.func, (ast.Name, ast.Attribute))
             and _name(c.func) in ("Config", "PeerSurfaceConfig")]
    assert [_name(c.func) for c in built] == ["PeerSurfaceConfig"] * 2
    for call in built:
        http = next(k.value for k in call.keywords if k.arg == "http")
        assert isinstance(http, ast.Call) and _name(http.func) == "peer_protocol"
