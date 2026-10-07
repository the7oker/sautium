"""The Last.fm authorization flow (backend/lastfm_auth.py): Last.fm's web
flow — the page names this node as the callback, the redirect is the
completion event, the nonce is the admission. pylast, the database and the
history walk are stubbed — the exchange is one network call, the
persistence one upsert per key."""

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import auth_hmac
import lastfm_auth
import lastfm_history

ORIGIN = "http://127.0.0.1:18000"


class FakeGenerator:
    refused: set = set()
    exchanged: list = []

    def __init__(self, network):
        pass

    def get_web_auth_session_key_username(self, url, token=""):
        FakeGenerator.exchanged.append(token)
        if token in FakeGenerator.refused:
            raise RuntimeError("Unauthorized Token - This token has not been authorized")
        return "sk-" + token, "listener"


@pytest.fixture(autouse=True)
def flow(monkeypatch):
    """A fresh module: no flow, no session, the network, the database and the
    history walk replaced, every wake counted."""
    persisted, wakes, walks = [], [], []
    FakeGenerator.refused = set()
    FakeGenerator.exchanged = []
    monkeypatch.setattr(lastfm_auth.pylast, "SessionKeyGenerator", FakeGenerator)
    monkeypatch.setattr(lastfm_auth, "_network", lambda: None)
    monkeypatch.setattr(lastfm_auth, "_upsert", lambda key, value: persisted.append((key, value)))
    monkeypatch.setattr(lastfm_auth, "_notify", lambda: wakes.append(1))
    monkeypatch.setattr(lastfm_history, "start", walks.append)
    monkeypatch.setattr(auth_hmac, "host_allowed", lambda host: host == "127.0.0.1:18000")
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
    assert FakeGenerator.exchanged == []
    # The redirect cannot sign: the callback path is admitted unsigned, the
    # rest of the flow is not.
    assert auth_hmac._is_whitelisted(lastfm_auth.CALLBACK_PREFIX + nonce)
    assert not auth_hmac._is_whitelisted("/lastfm/auth/start")
    assert not auth_hmac._is_whitelisted("/lastfm/auth/status")
    assert lastfm_auth.status() == {
        "authorized": False, "username": "", "pending": True, "auth_url": url, "error": None}


def test_a_live_flow_is_handed_back():
    first = lastfm_auth.start(ORIGIN)
    nonce = _nonce()
    assert lastfm_auth.start(ORIGIN) == first
    assert _nonce() == nonce


def test_an_expired_flow_is_replaced():
    first = lastfm_auth.start(ORIGIN)
    old = _nonce()
    lastfm_auth._flow.started_at -= lastfm_auth.FLOW_TTL_SECONDS + 1
    assert lastfm_auth.status()["pending"] is False
    assert lastfm_auth.start(ORIGIN) != first
    assert _nonce() != old
    with pytest.raises(lastfm_auth.FlowError, match="expired"):
        lastfm_auth.callback(old, "granted")


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

    assert FakeGenerator.exchanged == ["granted"]
    assert persisted == [("lastfm.session_key", "sk-granted"), ("lastfm.username", "listener")]
    assert lastfm_auth.status() == {
        "authorized": True, "username": "listener", "pending": False, "auth_url": None, "error": None}
    assert wakes == [1]
    assert walks == ["connected"]
    # Single use: the same link a second time earns nothing.
    with pytest.raises(lastfm_auth.FlowError, match="expired"):
        lastfm_auth.callback(nonce, "granted")
    assert FakeGenerator.exchanged == ["granted"]


def test_a_foreign_nonce_changes_nothing(flow):
    persisted, wakes, walks = flow
    lastfm_auth.start(ORIGIN)
    nonce = _nonce()
    with pytest.raises(lastfm_auth.FlowError, match="expired"):
        lastfm_auth.callback("not-the-nonce", "granted")
    with pytest.raises(lastfm_auth.FlowError, match="without a token"):
        lastfm_auth.callback(nonce, "")
    assert FakeGenerator.exchanged == [] and persisted == [] and wakes == [] and walks == []
    assert _nonce() == nonce and lastfm_auth.status()["pending"] is True


def test_a_refused_callback_token_ends_the_flow_and_says_why(flow):
    persisted, wakes, walks = flow
    lastfm_auth.start(ORIGIN)
    nonce = _nonce()
    FakeGenerator.refused = {"granted"}

    with pytest.raises(lastfm_auth.FlowError, match="Unauthorized Token"):
        lastfm_auth.callback(nonce, "granted")

    assert persisted == [] and wakes == [1] and walks == []
    status = lastfm_auth.status()
    assert status["pending"] is False and "Unauthorized Token" in status["error"]
    # The next start mints a fresh flow and forgets the refusal.
    lastfm_auth.start(ORIGIN)
    assert _nonce() != nonce
    assert lastfm_auth.status()["error"] is None
