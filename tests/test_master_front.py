"""The master's trusted front (scripts/master-front/Caddyfile) writes no log
line that names a peer or what it asked: no site has an access log, and the
default log — which takes the entries Caddy writes for a request a handler
failed, with the request in them — drops the client's address and port, the
URI with its query and the headers from every entry."""

import re
from pathlib import Path

CADDYFILE = Path(__file__).resolve().parent.parent / "scripts" / "master-front" / "Caddyfile"


def _global_block(text: str) -> str:
    """The global options block: the file's first top-level { … }."""
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    start = body.index("{")
    depth = 0
    for i, ch in enumerate(body[start:], start):
        depth += {"{": 1, "}": -1}.get(ch, 0)
        if depth == 0:
            return body[start:i + 1]
    raise AssertionError("unbalanced Caddyfile")


def test_no_site_keeps_an_access_log():
    body = "\n".join(l for l in CADDYFILE.read_text().splitlines() if not l.lstrip().startswith("#"))
    after_global = body[body.index(_global_block(body)) + len(_global_block(body)):]
    assert not re.search(r"^\s*log\b", after_global, re.MULTILINE)


def test_the_default_log_drops_the_request_s_address_and_uri():
    block = _global_block(CADDYFILE.read_text())
    log = block[block.index("log default"):]
    assert "format filter" in log and "wrap json" in log
    for field in ("request>remote_ip", "request>remote_port", "request>client_ip",
                  "request>uri", "request>headers"):
        assert re.search(rf"^\s*{re.escape(field)}\s+delete\s*$", log, re.MULTILINE), field
