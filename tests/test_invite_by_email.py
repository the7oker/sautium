"""The Web UI's calls to the Worker (backend/routers/p2p.py): the Worker sends
invites only from an account with a verified email, so an account without one
is told so before any request leaves the node, and the Friends screen says
where to verify; a refusal for volume tells the person how long to wait, as
the Worker said it; the Friends screen's pickups keep to their share of
the Worker's day; a request waits there for one click, Add back; and a
profile's header shows the long code to copy, never the short tail."""

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


class _Answering(_Refusing):
    """httpx.AsyncClient answering the accept with what a test sets."""
    status, body, date = 200, {}, None

    def _answer(self):
        import httpx
        headers = {"date": self.date} if self.date else {}
        return httpx.Response(self.status, headers=headers, json=self.body)


def _accept(monkeypatch, status, body, date=None) -> str:
    from desktop.p2p import chat_wire

    async def born():
        return True
    monkeypatch.setattr(p2p, "_get_identity", lambda: {
        "invite_code": "alice#ABCD-EF01-2345", "public_key_hex": "ab" * 32})
    monkeypatch.setattr(p2p, "_born", born)
    monkeypatch.setattr(p2p, "_sign_message", lambda message: "cd" * 64)
    monkeypatch.setattr(p2p, "_db_execute", lambda *a, **k: pytest.fail("no answer to bind"))
    monkeypatch.setattr(p2p.httpx, "AsyncClient", _Answering)
    monkeypatch.setattr(_Answering, "status", status)
    monkeypatch.setattr(_Answering, "body", body)
    monkeypatch.setattr(_Answering, "date", date)
    share = chat_wire.parse_share("bob#1234-5678-9ABC#i-" + "0" * 32)
    return asyncio.run(p2p._accept_invite(share, 1, None))


@pytest.mark.parametrize("body", [[], "accepted", {"inviter": "ab" * 32}, {"inviter": {"public_key_hex": 7}}])
def test_an_accepts_answer_of_another_shape_is_no_answer(monkeypatch, body):
    assert _accept(monkeypatch, 200, body) == "unreachable"


@pytest.mark.parametrize("worker_minus_node_s, outcome", [
    (3600, "clock"), (-3600, "clock"),                                  # this node behind, or ahead
    (310, "clock"), (-310, "clock"), (290, "refused"), (-290, "refused"), (0, "refused")])
def test_a_refusal_of_this_node_names_the_clock_only_past_the_workers_five_minutes(monkeypatch, worker_minus_node_s,
                                                                                outcome):
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone
    at = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=worker_minus_node_s), usegmt=True)
    assert _accept(monkeypatch, 403, {"error": "stale timestamp"}, at) == outcome


def test_a_refusal_of_this_node_without_a_date_is_no_word_on_the_clock(monkeypatch):
    assert _accept(monkeypatch, 403, {"error": "no certificate for this key"}) == "refused"
    assert _accept(monkeypatch, 403, {"error": "no certificate for this key"}, "soon") == "refused"
    assert _accept(monkeypatch, 404, {"reason": "not_found", "error": "no such invite"}) == "not_found"


async def _busy_cert():
    raise HTTPException(429, "The verification service is busy — please try again in a minute.")


def test_a_key_is_born_when_its_certificate_is_held_and_a_busy_worker_says_so_where_a_person_waits(monkeypatch):
    async def held():
        return {"pubkey": "ab" * 32}

    async def none():
        return None
    for cert, born in ((held, True), (none, False)):
        monkeypatch.setattr(p2p, "_get_own_birth_cert", cert)
        assert asyncio.run(p2p._born()) is born and asyncio.run(p2p._born_quietly()) is born
    monkeypatch.setattr(p2p, "_get_own_birth_cert", _busy_cert)
    with pytest.raises(HTTPException) as busy:
        asyncio.run(p2p._born())
    assert busy.value.status_code == 429
    assert asyncio.run(p2p._born_quietly()) is False                  # no person waiting: a no, in the log


def test_a_busy_worker_is_busy_to_the_person_and_no_answer_to_a_background_call(monkeypatch):
    from desktop.p2p import chat_wire
    monkeypatch.setattr(p2p, "_get_own_birth_cert", _busy_cert)
    monkeypatch.setattr(p2p, "_get_identity", lambda: {
        "invite_code": "alice#ABCD-EF01-2345", "public_key_hex": "ab" * 32,
        "email": "alice@example.org", "username": "alice"})
    for route in (p2p.email_send_code(), p2p._worker_check_email(p2p._get_identity())):
        with pytest.raises(HTTPException) as busy:
            asyncio.run(route)
        assert busy.value.status_code == 429
    share = chat_wire.parse_share("bob#1234-5678-9ABC#i-" + "0" * 32)
    assert asyncio.run(p2p._accept_invite(share, 1, None)) == "unreachable"
    assert asyncio.run(p2p._tell_code_added("bob#1234-5678-9ABC")) is None


_SHELL = (BACKEND / "static" / "app-shell.js").read_text(encoding="utf-8")


def _function(name: str) -> str:
    return re.search(rf"\n  function {name}\(.*?\) \{{\n.*?\n  \}}\n", _SHELL, re.S).group(0)


def _node(body: str) -> str:
    stubs = r"""
const escapeHtml = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
const escapeProfileHtml = escapeHtml;
const avatarPlaceholder = name => ({ bg: '#123', initials: String(name).slice(0, 1).toUpperCase() });
const SVG_COPY = '<svg data-copy-icon></svg>';
"""
    return subprocess.run([shutil.which("node"), "-e", "'use strict';" + stubs + body],
                          capture_output=True, text=True, check=True).stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_request_row_offers_add_back_and_dismiss_under_the_adders_name():
    code = "sasha#FBE3-7BA9-57CE-0123-4567-89AB-CDEF-0011"
    out = _node(_function("renderRequestRow")
                + f"process.stdout.write(renderRequestRow({{id: 7, username: 'sa<sha', share_code: {code!r}}}));"
                + f"process.stdout.write(renderRequestRow({{id: 8, username: 'x', share_code: 'y', name_taken: true}}));")
    first, second = out.split('data-request-id="8"')
    assert 'data-request-id="7"' in first and "sa&lt;sha" in first and "sa<sha" not in first
    assert re.search(r"<button[^>]*data-request-add>Add back</button>", first)
    assert "data-request-dismiss" in first and "data-menu" not in first   # no chat, no contact menu
    assert "friend-row" not in out           # the friends list's in-place patch counts its own rows alone
    assert code in first                     # the key, not only a name anybody may choose
    assert "a friend of yours has this name" in second and "a friend of yours has this name" not in first


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_profiles_header_shows_the_long_code_with_its_copy_button():
    code = "masha#FBE3-7BA9-57CE-0123-4567-89AB-CDEF-0011"
    out = _node(_function("identityCodeHTML") + f"process.stdout.write(identityCodeHTML({code!r}));")
    assert f'<span class="invite">{code}</span>' in out
    assert re.search(r"<button[^>]*data-copy-code>\s*<svg data-copy-icon></svg></button>", out)


def test_no_screen_shows_the_short_tail_of_an_invite_code():
    # a code's tail cut off and shown ("· FBE3-7BA9-57CE"): gone; both
    # profile headers render the long share code instead
    assert ".split('#').pop()" not in _SHELL
    assert _SHELL.count("identityCodeHTML(shareCode)") == 2
    assert "(account && account.share_code)" in _SHELL and "profile.share_code" in _SHELL
