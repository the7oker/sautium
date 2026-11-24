"""The Recommendations ranking's log line (backend/routers/home.py).

Every write of listening_history rebuilds the ranking, and on the 4 GB box a
two-second test tone was a listen: "Recommendations ranking rebuilt … cold
start" at INFO every two seconds, all evening (PI-NODE-2C §11.14). The
trigger is right; the level was not. News is the first build, the ranking
leaving or reaching a cold start, and a build that took over a second.
"""

import logging
import sys

import pytest

from tests.conftest import REPO_ROOT

if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

home = pytest.importorskip("routers.home")


def test_a_rebuild_is_news_when_its_kind_changes_or_it_was_slow(monkeypatch, caplog):
    answers = iter([None, None, ["a", "b"], ["a", "b", "c"], ["a"], None, None])
    clock = iter([0.0, 0.2,   10.0, 10.3,   20.0, 20.4,   30.0, 30.1,   40.0, 41.5,   50.0, 50.2,   60.0, 62.0])
    monkeypatch.setattr(home, "_rank_recommendations", lambda: next(answers))
    monkeypatch.setattr(home.time, "monotonic", lambda: next(clock))
    ranking = home._Ranking()
    with caplog.at_level(logging.DEBUG, logger=home.__name__):
        for _ in range(7):
            ranking.rebuild()
    said = [(r.levelname, r.getMessage()) for r in caplog.records if "ranking rebuilt" in r.getMessage()]
    assert said == [
        ("INFO", "Recommendations ranking rebuilt in 200 ms: cold start"),      # the first build
        ("DEBUG", "Recommendations ranking rebuilt in 300 ms: cold start"),     # a listen, nothing new
        ("INFO", "Recommendations ranking rebuilt in 400 ms: 2 albums"),        # the cold start is over
        ("DEBUG", "Recommendations ranking rebuilt in 100 ms: 3 albums"),
        ("INFO", "Recommendations ranking rebuilt in 1500 ms: 1 albums"),       # slow
        ("INFO", "Recommendations ranking rebuilt in 200 ms: cold start"),      # back to a cold start
        ("INFO", "Recommendations ranking rebuilt in 2000 ms: cold start"),     # slow, whatever the kind
    ]
    assert ranking.album_ids() is None
