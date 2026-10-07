"""The Last.fm authorization flow (backend/lastfm_auth.py): Last.fm's web
flow — the page names this node as the callback, the redirect is the
completion event, the nonce is the admission, the page's deadline an event
of its own. Last.fm, the database, the history walk and the deadline's
timer are stubbed — the exchange is one call, the persistence one upsert
per key."""

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pylast
import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import auth_hmac
import lastfm
import lastfm_auth
import lastfm_history

ORIGIN = "http://127.0.0.1:18000"
PHONE = "http://192.168.1.5:18000"


class FakeTimer:
    """The deadline, fired by the test instead of the clock."""
    armed: list = []

    def __init__(self, interval, function, args=()):
        self.interval, self.function, self.args = interval, function, args
        self.cancelled = False
        FakeTimer.armed.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.function(*self.args)


class FakeService:
    """auth.getSession: a session for every token, unless the token is
    refused (Last.fm's verdict) or the source stays silent."""
    refused: set = set()
    silent = 0                       # exchanges that get no answer
    during = None                    # runs inside the exchange
    exchanged: list = []

    def auth_session(self, token):
        FakeService.exchanged.append(token)
        if FakeService.during:
            FakeService.during()
        if FakeService.silent:
            FakeService.silent -= 1
            raise lastfm.SourceUnavailable("Last.fm is offline")
        if token in FakeService.refused:
            raise pylast.WSError(None, "14", "Unauthorized Token - This token has not been authorized")
        return "sk-" + token, "listener"


@pytest.fixture(autouse=True)
def flow(monkeypatch):
    """A fresh module: no flow, no session, Last.fm, the database, the
    history walk and the timer replaced, every wake counted."""
    persisted, wakes, walks = [], [], []
    FakeTimer.armed = []
    FakeService.refused, FakeService.silent, FakeService.during = set(), 0, None
    FakeService.exchanged = []
    monkeypatch.setattr(lastfm, "LastFmService", FakeService)
    monkeypatch.setattr(lastfm_auth, "threading", SimpleNamespace(Timer=FakeTimer))
    monkeypatch.setattr(lastfm_auth, "_upsert", lambda key, value: persisted.append((key, value)))
    monkeypatch.setattr(lastfm_auth, "_notify", lambda: wakes.append(1))
    monkeypatch.setattr(lastfm_history, "start", walks.append)
    monkeypatch.setattr(auth_hmac, "host_allowed",
                        lambda host: host in ("127.0.0.1:18000", "192.168.1.5:18000"))
    monkeypatch.setattr(lastfm_auth, "_flow", None)
    monkeypatch.setattr(lastfm_auth, "_last_error", None)
    monkeypatch.setattr(lastfm_auth.settings, "lastfm_api_key", "k")
    monkeypatch.setattr(lastfm_auth.settings, "lastfm_session_key", None)
    monkeypatch.setattr(lastfm_auth.settings, "lastfm_username", None)
    return persisted, wakes, walks


def _nonce() -> str:
    return lastfm_auth._flow.nonce


def test_start_opens_the_web_flow_with_this_node_as_the_callback():
    url = lastfm_auth.start(ORIGIN)
    nonce = _nonce()
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://www.last.fm/api/auth/"
    # Exactly the key and the callback: a `token` would make the page the
    # desktop flow, which ends on Last.fm's own page and never redirects.
    assert parse_qs(parts.query) == {
        "api_key": ["k"],
        "cb": ["http://127.0.0.1:18000/lastfm/auth/callback/" + nonce],
    }
    assert len(nonce) >= 21          # 16 random bytes, urlsafe
    assert [t.interval for t in FakeTimer.armed] == [lastfm_auth.FLOW_TTL_SECONDS]
    assert FakeService.exchanged == []
    # The redirect cannot sign: the callback path is admitted unsigned, the
    # rest of the flow is not.
    assert auth_hmac._is_whitelisted(lastfm_auth.CALLBACK_PREFIX + nonce)
    assert not auth_hmac._is_whitelisted("/lastfm/auth/start")
    assert not auth_hmac._is_whitelisted("/lastfm/auth/status")
    assert lastfm_auth.status() == {
        "authorized": False, "username": "", "pending": True, "auth_url": url, "error": None}


def test_a_live_flow_is_handed_back_to_the_address_that_started_it():
    first = lastfm_auth.start(ORIGIN)
    nonce = _nonce()
    assert lastfm_auth.start(ORIGIN) == first
    assert _nonce() == nonce and len(FakeTimer.armed) == 1


def test_another_address_gets_a_flow_of_its_own():
    # The launcher's page names 127.0.0.1: a phone handed it would be sent
    # back to itself once access is granted.
    lastfm_auth.start(ORIGIN)
    launcher = _nonce()
    url = lastfm_auth.start(PHONE)
    assert parse_qs(urlsplit(url).query)["cb"] == [
        "http://192.168.1.5:18000/lastfm/auth/callback/" + _nonce()]
    assert _nonce() != launcher
    assert FakeTimer.armed[0].cancelled and not FakeTimer.armed[1].cancelled
    # The newest flow wins: the launcher's page lands on "expired".
    with pytest.raises(lastfm_auth.FlowError, match="expired"):
        lastfm_auth.callback(launcher, "granted")
    assert lastfm_auth.callback(_nonce(), "granted") == "listener"


def test_the_deadline_ends_the_flow_and_wakes_the_clients(flow):
    persisted, wakes, walks = flow
    lastfm_auth.start(ORIGIN)
    old = _nonce()

    FakeTimer.armed[0].fire()

    # The window waiting on the page hears that it can no longer finish.
    assert wakes == [1]
    status = lastfm_auth.status()
    assert status["pending"] is False and status["error"] == "The Last.fm page expired."
    with pytest.raises(lastfm_auth.FlowError, match="expired"):
        lastfm_auth.callback(old, "granted")
    lastfm_auth.start(ORIGIN)
    assert _nonce() != old and lastfm_auth.status()["error"] is None


@pytest.mark.parametrize("origin", [
    "http://evil.example",           # not one of this node's addresses
    "127.0.0.1:18000",               # no scheme
    "http://127.0.0.1:18000/profile", # a path, not an origin
    "ftp://127.0.0.1:18000",
    "",
])
def test_an_origin_that_is_not_this_node_is_refused(origin):
    with pytest.raises(lastfm_auth.FlowError):
        lastfm_auth.start(origin)
    assert lastfm_auth._flow is None


def test_the_callback_finishes_the_flow(flow):
    persisted, wakes, walks = flow
    lastfm_auth.start(ORIGIN)
    nonce = _nonce()

    assert lastfm_auth.callback(nonce, "granted") == "listener"

    assert FakeService.exchanged == ["granted"]
    assert persisted == [("lastfm.session_key", "sk-granted"), ("lastfm.username", "listener")]
    assert lastfm_auth.status() == {
        "authorized": True, "username": "listener", "pending": False, "auth_url": None, "error": None}
    assert wakes == [1] and walks == ["connected"]
    assert FakeTimer.armed[0].cancelled
    # One connection per flow: the same link a second time earns nothing.
    with pytest.raises(lastfm_auth.FlowError, match="expired"):
        lastfm_auth.callback(nonce, "granted")
    assert FakeService.exchanged == ["granted"]


def test_a_foreign_nonce_changes_nothing(flow):
    persisted, wakes, walks = flow
    lastfm_auth.start(ORIGIN)
    nonce = _nonce()
    with pytest.raises(lastfm_auth.FlowError, match="expired"):
        lastfm_auth.callback("not-the-nonce", "granted")
    with pytest.raises(lastfm_auth.FlowError, match="expired"):
        lastfm_auth.callback("é", "granted")            # the path is anyone's to type
    with pytest.raises(lastfm_auth.FlowError, match="without a token"):
        lastfm_auth.callback(nonce, "")
    assert FakeService.exchanged == [] and persisted == [] and wakes == [] and walks == []
    assert _nonce() == nonce and lastfm_auth.status()["pending"] is True


def test_a_second_callback_during_the_exchange_is_refused(flow):
    persisted, wakes, walks = flow
    url = lastfm_auth.start(ORIGIN)
    nonce = _nonce()
    started_meanwhile = []

    def meanwhile():
        # A doubled redirect, and a start from the same address, while the
        # first callback is still talking to Last.fm.
        with pytest.raises(lastfm_auth.FlowError, match="already finishing"):
            lastfm_auth.callback(nonce, "granted")
        started_meanwhile.append(lastfm_auth.start(ORIGIN))
    FakeService.during = meanwhile

    assert lastfm_auth.callback(nonce, "granted") == "listener"

    assert started_meanwhile == [url] and len(FakeTimer.armed) == 1
    assert FakeService.exchanged == ["granted"] and wakes == [1]
    assert lastfm_auth.status()["pending"] is False


def test_another_address_mid_exchange_gets_its_own_flow(flow):
    # The launcher's flow is exchanging slowly and a phone taps Connect:
    # handed the launcher's page, the phone would be sent back to 127.0.0.1
    # — itself — should that exchange get no answer and the flow stay.
    persisted, wakes, walks = flow
    lastfm_auth.start(ORIGIN)
    nonce = _nonce()
    phone = []
    FakeService.during = lambda: phone.append(lastfm_auth.start(PHONE))
    FakeService.refused = {"granted"}

    with pytest.raises(lastfm_auth.FlowError, match="Unauthorized Token"):
        lastfm_auth.callback(nonce, "granted")

    assert parse_qs(urlsplit(phone[0]).query)["cb"] == [
        "http://192.168.1.5:18000/lastfm/auth/callback/" + _nonce()]
    # The launcher's refusal is not the phone's: its flow is still open.
    status = lastfm_auth.status()
    assert status["pending"] is True and status["auth_url"] == phone[0]
    assert status["error"] is None


def test_last_fm_not_answering_keeps_the_flow_for_a_reload(flow):
    persisted, wakes, walks = flow
    lastfm_auth.start(ORIGIN)
    nonce = _nonce()
    FakeService.silent = 1

    with pytest.raises(lastfm_auth.FlowError, match="Reload this page"):
        lastfm_auth.callback(nonce, "granted")

    # The token Last.fm sent is still good: nothing ended, nobody was woken,
    # and the same redirect, reloaded, finishes the flow.
    assert _nonce() == nonce and lastfm_auth.status()["pending"] is True
    assert lastfm_auth.status()["error"] is None and wakes == [] and persisted == []
    assert lastfm_auth.callback(nonce, "granted") == "listener"
    assert FakeService.exchanged == ["granted", "granted"] and wakes == [1]


def test_a_refused_callback_token_ends_the_flow_and_says_why(flow):
    persisted, wakes, walks = flow
    lastfm_auth.start(ORIGIN)
    nonce = _nonce()
    FakeService.refused = {"granted"}

    with pytest.raises(lastfm_auth.FlowError, match="Unauthorized Token"):
        lastfm_auth.callback(nonce, "granted")

    assert persisted == [] and wakes == [1] and walks == []
    status = lastfm_auth.status()
    assert status["pending"] is False and "Unauthorized Token" in status["error"]
    assert FakeTimer.armed[0].cancelled
    # The next start mints a fresh flow and forgets the refusal.
    lastfm_auth.start(ORIGIN)
    assert _nonce() != nonce
    assert lastfm_auth.status()["error"] is None


def test_a_failure_of_our_own_ends_the_flow_loudly(flow, monkeypatch, caplog):
    persisted, wakes, walks = flow

    def gone(key, value):
        raise RuntimeError("the database is gone")
    monkeypatch.setattr(lastfm_auth, "_upsert", gone)
    lastfm_auth.start(ORIGIN)

    with caplog.at_level(logging.ERROR, logger="lastfm_auth"):
        with pytest.raises(lastfm_auth.FlowError, match="database is gone"):
            lastfm_auth.callback(_nonce(), "granted")

    # The waiting window hears the reason instead of waiting out the
    # deadline, and the log keeps the traceback.
    assert wakes == [1] and walks == []
    status = lastfm_auth.status()
    assert status["authorized"] is False and status["pending"] is False
    assert "database is gone" in status["error"]
    assert any(r.levelno == logging.ERROR and r.exc_info for r in caplog.records)


def test_the_deadline_does_not_undo_a_grant_mid_exchange(flow):
    persisted, wakes, walks = flow
    lastfm_auth.start(ORIGIN)
    FakeService.during = FakeTimer.armed[0].fire

    assert lastfm_auth.callback(_nonce(), "granted") == "listener"

    status = lastfm_auth.status()
    assert status["authorized"] is True and status["error"] is None
    assert walks == ["connected"]
    assert wakes == [1]                  # the outcome alone, no "expired" before it


def test_last_fm_silent_past_the_deadline_ends_the_flow_as_expired(flow):
    # "Reload this page" would land on a flow the deadline has ended.
    persisted, wakes, walks = flow
    lastfm_auth.start(ORIGIN)
    nonce = _nonce()
    FakeService.during = FakeTimer.armed[0].fire
    FakeService.silent = 1

    with pytest.raises(lastfm_auth.FlowError, match="expired"):
        lastfm_auth.callback(nonce, "granted")

    status = lastfm_auth.status()
    assert status["pending"] is False and status["error"] == "The Last.fm page expired."
    assert wakes == [1] and persisted == []
