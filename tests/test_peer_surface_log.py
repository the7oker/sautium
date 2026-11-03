"""The Docker peer surface writes no access line — a peer's address and the
text of a search it sent are not the node's log —, takes no WebSocket
upgrade (uvicorn's WebSocket protocol logs each handshake with both), and
leaves the process's logging as its start made it: the Web UI's access log,
which shares uvicorn's access logger in the same process, keeps its handler
and its query filter (serving.PeerSurfaceConfig, used for both of
main._serve_p2p's servers).
A real uvicorn server on loopback, in a process of its own."""

import ast
import subprocess
import sys

from tests.conftest import REPO_ROOT

BACKEND = REPO_ROOT / "backend"

DRIVER = r"""
import asyncio, base64, json, logging, logging.config, os, socket, sys, urllib.request
sys.path.insert(0, %(backend)r)
from config import LOGGING_CONFIG
logging.config.dictConfig(LOGGING_CONFIG)
import uvicorn
from serving import PeerSurfaceConfig

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

def upgrade():
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        nonce = base64.b64encode(os.urandom(16))
        s.sendall(b"GET /api/mb/search?q=a+private+upgrade HTTP/1.1\r\nHost: x\r\n"
                  b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                  b"Sec-WebSocket-Key: " + nonce + b"\r\nSec-WebSocket-Version: 13\r\n\r\n")
        return s.recv(4096)
    finally:
        s.close()

probe = socket.socket()
probe.bind(("127.0.0.1", 0))
port = probe.getsockname()[1]
probe.close()
config = PeerSurfaceConfig(app, host="127.0.0.1", port=port)
kept = (list(access.handlers), list(access.filters)) == before
server = uvicorn.Server(config)

async def main():
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    url = f"http://127.0.0.1:{port}/api/mb/search?q=a+private+search"
    body = await asyncio.get_event_loop().run_in_executor(
        None, lambda: urllib.request.urlopen(url, timeout=5).read())
    await asyncio.get_event_loop().run_in_executor(None, upgrade)
    server.should_exit = True
    await task
    return body

body = asyncio.run(main())
print(json.dumps({"body": body.decode(), "kept": kept, "access_lines": lines,
                  "upgrade_said": [l for l in said if "private" in l or '"WebSocket ' in l]}))
"""


def test_the_peer_surface_logs_no_access_line_and_leaves_the_web_uis_log_alone():
    proc = subprocess.run([sys.executable, "-c", DRIVER % {"backend": str(BACKEND)}],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    import json
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out == {"body": "ok", "kept": True, "access_lines": [], "upgrade_said": []}


def test_both_peer_surface_servers_use_it():
    tree = ast.parse((BACKEND / "main.py").read_text(encoding="utf-8"))
    serve = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == "_serve_p2p")
    built = [c.func for c in ast.walk(serve) if isinstance(c, ast.Call)
             and isinstance(c.func, (ast.Name, ast.Attribute))
             and (getattr(c.func, "id", None) or getattr(c.func, "attr", None))
             in ("Config", "PeerSurfaceConfig")]
    assert [getattr(f, "id", None) or f.attr for f in built] == ["PeerSurfaceConfig"] * 2
