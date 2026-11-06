"""The Web UI's calls to the Worker (backend/routers/p2p.py): the Worker sends
invites only from an account with a verified email, so an account without one
is told so before any request leaves the node, and the Friends screen says
where to verify; a refusal for volume tells the person how long to wait, as
the Worker said it; and the Friends screen's pickups keep to their share of
the Worker's day."""

import asyncio
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

p2p = pytest.importorskip("routers.p2p")


def _account(monkeypatch, verified: bool) -> list:
    sent = []

    async def status():
        return {"email": "alice@example.org", "verified": verified}

    async def post(path, payload):
        sent.append(path)
        return {"status": "sent", "verified_sender": True, "invite_id": "9e114e70-db9a-664c-1804-84e25be59859"}

    async def born():
        return True

    monkeypatch.setattr(p2p, "_get_identity", lambda: {
        "invite_code": "alice#ABCD-EF01-2345", "public_key_hex": "ab" * 32,
        "email": "alice@example.org", "username": "alice"})
    monkeypatch.setattr(p2p, "email_status", status)
    monkeypatch.setattr(p2p, "_born", born)
    monkeypatch.setattr(p2p, "_sign_message", lambda message: "cd" * 64)
    monkeypatch.setattr(p2p, "_worker_post", post)
    monkeypatch.setattr(p2p, "_db_execute", lambda *a, **k: None)
    return sent


def test_an_invite_needs_a_verified_email_before_it_leaves_the_node(monkeypatch):
    request = p2p.InviteByEmailRequest(to_email="friend@example.net")
    sent = _account(monkeypatch, verified=False)
    with pytest.raises(HTTPException) as refused:
        asyncio.run(p2p.invite_by_email(request))
    assert refused.value.status_code == 400
    assert refused.value.detail.startswith("Verify your email in Profile first")
    assert sent == []

    sent = _account(monkeypatch, verified=True)
    assert asyncio.run(p2p.invite_by_email(request))["status"] == "sent"
    assert sent == ["/send-invite"]


_HINTS = r"""
for (const status of [{ verified: true, email: 'a@example.org' }, { verified: false, email: 'a@example.org' },
                      { verified: false, email: '' }, {}]) {
  process.stdout.write(friendsInviteHint(status) + '\n');
}
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_friends_screen_tells_an_account_without_a_verified_email_where_to_verify():
    """No hint promises an invite the Worker would refuse: an address not yet
    verified, or none, is told where to verify. app-shell.js's own function,
    run as written."""
    src = (BACKEND / "static" / "app-shell.js").read_text(encoding="utf-8")
    assert "unverified badge" not in src
    fn = re.search(r"\n  function friendsInviteHint\(.*?\) \{\n.*?\n  \}\n", src, re.S).group(0)
    out = subprocess.run([shutil.which("node"), "-e", "'use strict';" + fn + _HINTS],
                         capture_output=True, text=True, check=True).stdout.splitlines()
    unverified = "Verify your email in Profile to send invites — they go out under your verified address."
    none = "Add and verify an email in Profile to send invites — they go out under your verified address."
    assert out == ["Send invite — they get a verified email with a one-time invite to paste.", unverified, none, none]


class _Refusing:
    """httpx.AsyncClient answering 429 with the Worker's Retry-After."""
    retry_after = "60"

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        return self._answer()

    async def get(self, url, params=None, headers=None):
        return self._answer()

    def _answer(self):
        import httpx
        return httpx.Response(429, headers={"Retry-After": self.retry_after},
                              json={"error": "rate limited"})


@pytest.mark.parametrize("retry_after, said", [
    ("60", "in a minute"), ("1800", "in about 30 minutes"), ("86400", "in about 24 hours")])
def test_a_refusal_for_volume_says_how_long_to_wait(monkeypatch, retry_after, said):
    monkeypatch.setattr(p2p.httpx, "AsyncClient", _Refusing)
    monkeypatch.setattr(_Refusing, "retry_after", retry_after)
    for request in (p2p._worker_post("/send-verification", {}), p2p._worker_get("/check-email", {})):
        with pytest.raises(HTTPException) as refused:
            asyncio.run(request)
        assert refused.value.status_code == 429
        assert refused.value.detail == f"The verification service is busy — please try again {said}."


def test_the_friends_screen_asks_the_worker_once_a_minute_and_forty_times_a_day(monkeypatch):
    from collections import deque
    clock = [1000.0]
    monkeypatch.setattr(p2p.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(p2p, "_screen_pickups", deque())
    assert p2p._screen_pickup() is True
    clock[0] += 30
    assert p2p._screen_pickup() is False                      # a screen opened again at once shows what is here
    taken = 1
    for _ in range(200):
        clock[0] += 61
        taken += p2p._screen_pickup()
    assert taken == p2p.SCREEN_PICKUPS_PER_DAY               # the day's share, however often it opens
    clock[0] += 86400
    assert p2p._screen_pickup() is True                       # a day later, a share again
