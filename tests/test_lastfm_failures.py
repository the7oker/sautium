"""The three meanings of a failed Last.fm fetch (backend/lastfm.py) and the
drain rule that depends on them (backend/background_enrichment.py). Pure
logic: the classifier, the retry envelope with a zero backoff, the
process-local failure registry, and _wants_more. No network, no database —
the cooldown's arm() is replaced where a refusal would reach it."""

import pylast
import pytest

import background_enrichment as loop
import lastfm
from lastfm import LastFmService, SourceRefused, SourceUnavailable


class _Net:
    """pylast's exceptions read the network's name when stringified."""
    name = "Last.fm"


def _ws(status):
    return pylast.WSError(_Net(), status, f"status {status}")


@pytest.mark.parametrize("exc, kind", [
    (_ws("29"), "refused"),                                       # rate limit
    (_ws("26"), "refused"),                                       # suspended key
    (pylast.MalformedResponseError(_Net(), ValueError("<html>")), "refused"),
    (_ws(503), "transient"),                                      # pylast: HTTP 5xx as the code
    (_ws("16"), "transient"),                                     # "try again"
    (pylast.NetworkError(_Net(), TimeoutError("read")), "transient"),
    (_ws("6"), None),                                             # not found: a verdict
    (_ws("7"), None),                                             # any other verdict
    (TypeError("ours"), None),
])
def test_failure_class(exc, kind):
    assert lastfm._failure_class(exc) == kind


def test_a_verdict_is_raised_at_once():
    calls = []

    def fn():
        calls.append(1)
        raise _ws("6")

    with pytest.raises(pylast.WSError):
        LastFmService._with_retry(fn, base_delay=0)
    assert len(calls) == 1


def test_our_own_error_is_raised_at_once():
    def fn():
        raise TypeError("ours")

    with pytest.raises(TypeError):
        LastFmService._with_retry(fn, base_delay=0)


def test_transient_failure_outlives_retries_as_unavailable(monkeypatch):
    armed = []
    monkeypatch.setattr("api_cooldown.arm", lambda source, reason="": armed.append(source))
    calls = []

    def fn():
        calls.append(1)
        raise pylast.NetworkError(_Net(), ConnectionError("reset"))

    with pytest.raises(SourceUnavailable) as info:
        LastFmService._with_retry(fn, max_retries=2, base_delay=0)
    assert not isinstance(info.value, SourceRefused)
    assert len(calls) == 3
    assert armed == []


def test_a_refusal_arms_the_cooldown(monkeypatch):
    armed = []
    monkeypatch.setattr("api_cooldown.arm", lambda source, reason="": armed.append(source))

    def fn():
        raise pylast.MalformedResponseError(_Net(), ValueError("<html>"))

    with pytest.raises(SourceRefused):
        LastFmService._with_retry(fn, max_retries=1, base_delay=0)
    assert armed == ["lastfm"]


def test_transient_then_success_returns_the_value():
    attempts = iter([_ws(502), _ws("8"), "ok"])

    def fn():
        value = next(attempts)
        if isinstance(value, Exception):
            raise value
        return value

    assert LastFmService._with_retry(fn, base_delay=0) == "ok"


def test_internal_failure_registry_is_per_entity_type():
    lastfm._internal_failures.clear()
    assert lastfm.internal_failures("artist") == []
    lastfm.note_internal_failure("artist", "0f7b0d5e-5c1e-5a1e-9d0a-000000000001")
    lastfm.note_internal_failure("artist", "0f7b0d5e-5c1e-5a1e-9d0a-000000000001")
    lastfm.note_internal_failure("genre", "0f7b0d5e-5c1e-5a1e-9d0a-000000000002")
    assert lastfm.internal_failures("artist") == ["0f7b0d5e-5c1e-5a1e-9d0a-000000000001"]
    assert lastfm.internal_failures("genre") == ["0f7b0d5e-5c1e-5a1e-9d0a-000000000002"]
    lastfm._internal_failures.clear()


def test_wants_more_reads_only_the_stuck_keys():
    # A Last.fm batch: its errors left the queue (marker or registry).
    assert loop._wants_more({"processed": 30, "errors": 3}, 30) is True
    # The lyrics step: a failed track keeps its place.
    assert loop._wants_more({"processed": 50, "errors": 1}, 50, ("errors",)) is False
    # The model steps: an unembedded track likewise.
    assert loop._wants_more({"processed": 500, "failed": 1}, 500, ("failed",)) is False
    assert loop._wants_more({"processed": 500, "failed": 0}, 500, ("failed",)) is True
    # A short batch never drains.
    assert loop._wants_more({"processed": 29}, 30) is False
    assert loop._wants_more({}, 30) is False
