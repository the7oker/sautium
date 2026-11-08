"""MusicBrainz's web service as the node asks it (backend/musicbrainz.py): by
MBID only, one request per MIN_INTERVAL per process, the project's
User-Agent with the project's site as its contact, a 404 as a definitive
answer, and every other failure as no answer of one of three kinds — the pace
rule (a short pause that grows), MusicBrainz out of reach or not itself (the
persistent cooldown), or one entity's request gone wrong (that item alone
backs off)."""

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

httpx = pytest.importorskip("httpx")


class _Reply:
    def __init__(self, status, body=None, headers=None, text=""):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.fixture
def mb(monkeypatch):
    import api_cooldown
    import musicbrainz
    calls, cooldown = [], {"armed": [], "cleared": 0, "until": None}
    monkeypatch.setattr(api_cooldown, "arm", lambda source, reason="": cooldown["armed"].append((source, reason)))
    monkeypatch.setattr(api_cooldown, "clear",
                        lambda source: cooldown.__setitem__("cleared", cooldown["cleared"] + 1))
    monkeypatch.setattr(api_cooldown, "cooling_down", lambda source: cooldown["until"])
    replies = []

    def get(url, params=None, headers=None, timeout=None, follow_redirects=None):
        calls.append({"url": url, "params": params, "headers": headers,
                      "follow_redirects": follow_redirects})
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(httpx, "get", get)
    clock = {"t": 1000.0, "slept": []}
    monkeypatch.setattr(musicbrainz.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(musicbrainz.time, "sleep", lambda s: clock["slept"].append(s))
    monkeypatch.setattr(musicbrainz.random, "uniform", lambda a, b: 1.0)
    monkeypatch.setattr(musicbrainz, "_last_call", 0.0)
    monkeypatch.setattr(musicbrainz, "_refusals", 0)
    monkeypatch.setattr(musicbrainz, "_paused_until", 0.0)
    return musicbrainz, calls, replies, cooldown, clock


def test_a_lookup_is_by_mbid_with_the_projects_user_agent(mb):
    musicbrainz, calls, replies, cooldown, _ = mb
    replies.append(_Reply(200, {"id": "a"}))
    assert musicbrainz.lookup("artist", "a") == {"id": "a"}
    call = calls[0]
    assert call["url"] == "https://musicbrainz.org/ws/2/artist/a"
    assert call["params"] == {"inc": "aliases+genres+artist-rels+url-rels", "fmt": "json"}
    assert call["headers"]["User-Agent"] == "Sautium/1.0 ( https://sautium.net )"
    assert "@" not in call["headers"]["User-Agent"]
    assert call["follow_redirects"] is True
    assert cooldown["cleared"] == 1


def test_nothing_in_the_client_asks_by_name():
    import musicbrainz
    assert not hasattr(musicbrainz, "search_artist")
    assert not hasattr(musicbrainz, "fetch_album_release_groups")


def test_a_404_is_an_answer_and_clears_the_cooldown(mb):
    musicbrainz, calls, replies, cooldown, _ = mb
    replies.append(_Reply(404, {"error": "Not Found", "help": "For usage, please see: …"}))
    with pytest.raises(musicbrainz.NotFound):
        musicbrainz.lookup("recording", "r")
    assert cooldown["armed"] == [] and cooldown["cleared"] == 1


@pytest.mark.parametrize("body", [ValueError("an HTML page"), {"detail": "gone"}, []])
def test_a_404_that_is_not_musicbrainzs_says_nothing_about_the_entity(mb, body):
    """A proxy's or a portal's 404 is no "unknown": MusicBrainz is out of
    reach, the cooldown is armed, and nothing is stored about the MBID."""
    musicbrainz, calls, replies, cooldown, _ = mb
    replies.append(_Reply(404, body))
    with pytest.raises(musicbrainz.Unavailable):
        musicbrainz.lookup("release", "x")
    assert [s for s, _ in cooldown["armed"]] == [musicbrainz.COOLDOWN_SOURCE]


def test_the_genre_list_is_asked_at_the_one_pace_as_plain_text(mb):
    musicbrainz, calls, replies, cooldown, clock = mb
    replies.append(_Reply(200, text="rock\npost-rock\n\nrock\n"))
    assert musicbrainz.genre_names() == ["post-rock", "rock"]
    call = calls[0]
    assert call["url"] == "https://musicbrainz.org/ws/2/genre/all"
    assert call["params"] == {"fmt": "txt"}
    assert call["headers"]["User-Agent"] == "Sautium/1.0 ( https://sautium.net )"
    replies.append(_Reply(503))
    with pytest.raises(musicbrainz.RateLimited):
        musicbrainz.genre_names()
    replies.append(_Reply(200, text="  \n"))
    clock["t"] += 3600
    with pytest.raises(musicbrainz.Unavailable):
        musicbrainz.genre_names()


def test_the_pace_rule_pauses_briefly_and_longer_while_it_goes_on(mb):
    musicbrainz, calls, replies, cooldown, clock = mb
    replies.append(_Reply(503))
    with pytest.raises(musicbrainz.RateLimited) as first:
        musicbrainz.lookup("release", "x")
    assert first.value.retry_after == pytest.approx(musicbrainz.RATE_PAUSE)
    # nothing is asked while the pause runs
    with pytest.raises(musicbrainz.RateLimited):
        musicbrainz.lookup("release", "x")
    assert len(calls) == 1
    clock["t"] += first.value.retry_after
    replies.append(_Reply(429, headers={"Retry-After": "1"}))
    with pytest.raises(musicbrainz.RateLimited) as second:
        musicbrainz.lookup("release", "x")
    assert second.value.retry_after == pytest.approx(2 * musicbrainz.RATE_PAUSE)
    assert second.value.retry_after < 60 and cooldown["armed"] == []
    # an answer ends the run of refusals
    clock["t"] += second.value.retry_after
    replies.extend([_Reply(200, {"id": "x"}), _Reply(503)])
    musicbrainz.lookup("release", "x")
    with pytest.raises(musicbrainz.RateLimited) as again:
        musicbrainz.lookup("release", "x")
    assert again.value.retry_after == pytest.approx(musicbrainz.RATE_PAUSE)


def test_one_entitys_failure_is_that_items_alone(mb):
    musicbrainz, calls, replies, cooldown, clock = mb
    for reply in (_Reply(500), _Reply(502), _Reply(400), httpx.ReadTimeout("slow"),
                  httpx.RemoteProtocolError("cut off")):
        replies.append(reply)
        with pytest.raises(musicbrainz.Failed):
            musicbrainz.lookup("release", "x")
        clock["t"] += musicbrainz.MIN_INTERVAL
    assert cooldown["armed"] == []


def test_out_of_reach_or_not_musicbrainz_arms_the_cooldown(mb):
    musicbrainz, calls, replies, cooldown, clock = mb
    for reply in (httpx.ConnectError("refused"), httpx.ConnectTimeout("no route"), _Reply(403),
                  _Reply(200, ValueError("<html>portal</html>")), _Reply(200, ["not", "a", "map"]),
                  _Reply(200, {"error": "no id"})):
        replies.append(reply)
        with pytest.raises(musicbrainz.Unavailable) as e:
            musicbrainz.lookup("release", "x")
        assert not isinstance(e.value, musicbrainz.RateLimited)
        clock["t"] += musicbrainz.MIN_INTERVAL
    assert [s for s, _ in cooldown["armed"]] == ["musicbrainz"] * 6
    assert cooldown["cleared"] == 0


def test_while_the_cooldown_runs_no_request_is_made(mb):
    musicbrainz, calls, replies, cooldown, _ = mb
    cooldown["until"] = object()
    with pytest.raises(musicbrainz.Unavailable):
        musicbrainz.lookup("artist", "a")
    assert calls == []


def test_two_requests_are_at_least_the_interval_apart(mb):
    musicbrainz, calls, replies, cooldown, clock = mb
    replies.extend([_Reply(200, {"id": "a"}), _Reply(200, {"id": "b"})])
    musicbrainz.lookup("artist", "a")
    clock["t"] += 0.25
    musicbrainz.lookup("artist", "b")
    assert clock["slept"] == [pytest.approx(musicbrainz.MIN_INTERVAL - 0.25)]


def test_an_artists_groups_are_browsed_a_page_at_a_time_of_every_status(mb):
    musicbrainz, calls, replies, cooldown, clock = mb
    first = [{"id": str(i)} for i in range(100)]
    replies.extend([_Reply(200, {"release-group-count": 130, "release-groups": first}),
                    _Reply(200, {"release-group-count": 130,
                                 "release-groups": [{"id": "x"}] * 30}),
                    _Reply(200, {"release-group-count": 130})])
    assert musicbrainz.browse_release_groups_page("a", 0) == (first, 130)
    groups, count = musicbrainz.browse_release_groups_page("a", 100)
    assert (len(groups), count) == (30, 130)
    assert [c["params"]["offset"] for c in calls] == ["0", "100"]
    assert all(c["params"]["release-group-status"] == "all" and c["params"]["artist"] == "a"
               and c["params"]["limit"] == "100" for c in calls)
    with pytest.raises(musicbrainz.Unavailable):
        musicbrainz.browse_release_groups_page("a", 130)
