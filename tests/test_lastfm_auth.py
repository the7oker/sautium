"""The Last.fm authorization flow (backend/lastfm_auth.py): the callback is
the completion event, the nonce is the admission, the minted token is the
manual fallback. pylast and the database are stubbed — the exchange is one
network call, the persistence one upsert per key."""

import pytest

import auth_hmac
import lastfm_auth

ORIGIN = "http://127.0.0.1:18000"
DESKTOP_URL = "https://www.last.fm/api/auth/?api_key=k&token=minted"


class FakeGenerator:
    refused: set = set()
    exchanged: list = []

    def __init__(self, network):
        self.web_auth_tokens = {}

    def get_web_auth_url(self):
        self.web_auth_tokens[DESKTOP_URL] = "minted"
        return DESKTOP_URL

    def get_web_auth_session_key_username(self, url, token=""):
        FakeGenerator.exchanged.append(token)
        if token in FakeGenerator.refused:
            raise RuntimeError("Unauthorized Token - This token has not been authorized")
        return "sk-" + token, "listener"


@pytest.fixture(autouse=True)
def flow(monkeypatch):
    """A fresh module: no flow, no session, the network and the database
    replaced, every wake counted."""
    persisted, wakes = [], []
    FakeGenerator.refused = set()
    FakeGenerator.exchanged = []
    monkeypatch.setattr(lastfm_auth.pylast, "SessionKeyGenerator", FakeGenerator)
    monkeypatch.setattr(lastfm_auth, "_network", lambda: None)
    monkeypatch.setattr(lastfm_auth, "_upsert", lambda key, value: persisted.append((key, value)))
    monkeypatch.setattr(lastfm_auth, "_notify", lambda: wakes.append(1))
    monkeypatch.setattr(auth_hmac, "host_allowed", lambda host: host == "127.0.0.1:18000")
    monkeypatch.setattr(lastfm_auth, "_flow", None)
    monkeypatch.setattr(lastfm_auth, "_last_error", None)
    monkeypatch.setattr(lastfm_auth.settings, "lastfm_session_key", None)
    monkeypatch.setattr(lastfm_auth.settings, "lastfm_username", None)
    return persisted, wakes


def _nonce() -> str:
    return lastfm_auth._flow.nonce


def test_start_names_this_node_as_the_callback():
    url = lastfm_auth.start(ORIGIN)
    nonce = _nonce()
    assert url.startswith(DESKTOP_URL + "&cb=")
    assert url.endswith("http%3A%2F%2F127.0.0.1%3A18000%2Flastfm%2Fauth%2Fcallback%2F" + nonce)
    assert len(nonce) >= 21          # 16 random bytes, urlsafe
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
    assert FakeGenerator.exchanged == []


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
    persisted, wakes = flow
    lastfm_auth.start(ORIGIN)
    nonce = _nonce()

    assert lastfm_auth.callback(nonce, "granted") == "listener"

    assert FakeGenerator.exchanged == ["granted"]
    assert persisted == [("lastfm.session_key", "sk-granted"), ("lastfm.username", "listener")]
    assert lastfm_auth.status() == {
        "authorized": True, "username": "listener", "pending": False, "auth_url": None, "error": None}
    assert wakes == [1]
    # Single use: the same link a second time earns nothing.
    with pytest.raises(lastfm_auth.FlowError, match="expired"):
        lastfm_auth.callback(nonce, "granted")
    assert FakeGenerator.exchanged == ["granted"]


def test_a_foreign_nonce_changes_nothing(flow):
    persisted, wakes = flow
    lastfm_auth.start(ORIGIN)
    nonce = _nonce()
    with pytest.raises(lastfm_auth.FlowError, match="expired"):
        lastfm_auth.callback("not-the-nonce", "granted")
    with pytest.raises(lastfm_auth.FlowError, match="without a token"):
        lastfm_auth.callback(nonce, "")
    assert FakeGenerator.exchanged == [] and persisted == [] and wakes == []
    assert _nonce() == nonce and lastfm_auth.status()["pending"] is True


def test_a_refused_callback_token_ends_the_flow_and_says_why(flow):
    persisted, wakes = flow
    lastfm_auth.start(ORIGIN)
    nonce = _nonce()
    FakeGenerator.refused = {"granted"}

    with pytest.raises(lastfm_auth.FlowError, match="Unauthorized Token"):
        lastfm_auth.callback(nonce, "granted")

    assert persisted == [] and wakes == [1]
    status = lastfm_auth.status()
    assert status["pending"] is False and "Unauthorized Token" in status["error"]
    # The next start mints a fresh flow and forgets the refusal.
    lastfm_auth.start(ORIGIN)
    assert _nonce() != nonce
    assert lastfm_auth.status()["error"] is None


def test_manual_completion_exchanges_the_minted_token(flow):
    persisted, wakes = flow
    with pytest.raises(lastfm_auth.FlowError, match="not started"):
        lastfm_auth.complete()
    lastfm_auth.start(ORIGIN)

    assert lastfm_auth.complete() == "listener"

    assert FakeGenerator.exchanged == ["minted"]
    assert persisted[0] == ("lastfm.session_key", "sk-minted")
    assert lastfm_auth.status()["authorized"] is True and wakes == [1]


def test_manual_completion_keeps_the_flow_while_access_is_not_granted(flow):
    persisted, wakes = flow
    lastfm_auth.start(ORIGIN)
    nonce = _nonce()
    FakeGenerator.refused = {"minted"}

    with pytest.raises(lastfm_auth.FlowError, match="Unauthorized Token"):
        lastfm_auth.complete()

    # The user has not clicked "allow" yet: the flow, the token and the
    # page stay, and nobody is woken for a non-event.
    assert _nonce() == nonce and lastfm_auth.status()["pending"] is True
    assert lastfm_auth.status()["error"] is None and wakes == []

    FakeGenerator.refused = set()
    assert lastfm_auth.complete() == "listener"
    assert FakeGenerator.exchanged == ["minted", "minted"] and wakes == [1]
