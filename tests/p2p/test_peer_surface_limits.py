"""The Docker peer surface's per-IP windows (backend/p2p_app.py): a /health
probe counts in its own window, so the nodes behind one router probing a
source never spend the window their slice and sync requests need."""

import asyncio
import sys
from collections import defaultdict
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

p2p_app = pytest.importorskip("p2p_app")
from starlette.requests import Request  # noqa: E402


def _request(path: str) -> Request:
    return Request({"type": "http", "method": "GET", "scheme": "https",
                    "server": ("peer", 8801), "path": path, "query_string": b"",
                    "headers": [], "client": ("198.51.100.1", 40000)})


async def _served(request):
    return "served"


def test_probes_and_work_count_in_separate_windows(monkeypatch):
    monkeypatch.setattr(p2p_app, "_hits", defaultdict(list))
    monkeypatch.setattr(p2p_app, "_health_hits", defaultdict(list))
    limit = p2p_app.RATE_LIMIT_PER_MINUTE

    async def go():
        for _ in range(limit):
            assert await p2p_app.rate_limit(_request("/health"), _served) == "served"
        probe = await p2p_app.rate_limit(_request("/health"), _served)
        assert probe.status_code == 429 and int(probe.headers["Retry-After"]) >= 1
        for _ in range(limit):
            assert await p2p_app.rate_limit(_request("/api/mb/slice"), _served) == "served"
        work = await p2p_app.rate_limit(_request("/api/mb/slice"), _served)
        assert work.status_code == 429
    asyncio.run(go())
