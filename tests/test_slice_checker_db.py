"""The backend's slice checker (backend/main.py, `_start_slice_checker`) as it
runs, on a real PostgreSQL built from the migrations — skipped without a
cluster: an answer kept unknown takes the check away from the kept slices
about it on the checker's own thread, after the answer's commit — taken by an
idle checker, and never stamped over by a verdict that read the answers
before it (desktop/p2p/slice_checks.uncheck_subject, rule 0's condition 4).

The checker is started once for the module, with the answers' wakers
registered as at startup; its own thread evaluates every check it is told of,
so the module holds no other test."""

import sys
import threading
from pathlib import Path

import pytest

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("sqlalchemy")
import psycopg2.pool  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
for _p in (REPO, BACKEND):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from desktop.p2p import mb_slice_queries, slice_checks  # noqa: E402
from tests.p2p.test_slice_checks_db import (OWN, _answer, _authority, _browse, _key, _offer,  # noqa: E402,F401
                                            conn)
from tests.slice_certs import Subtree  # noqa: E402

WAIT_S = 30


@pytest.fixture(scope="module")
def checker(scratch_dsn):
    """The checker for the module. Its wakers are unregistered at the
    module's end, so the thread, which outlives it, is never woken by
    another module's answers."""
    import db_pool
    import mb_answers
    with pytest.MonkeyPatch.context() as mp:
        pool = psycopg2.pool.ThreadedConnectionPool(1, 8, dsn=scratch_dsn, options="-c timezone=UTC")
        mp.setattr(db_pool, "_pool", pool)
        mp.setattr(mb_answers, "_unknown_waker", None)
        mp.setattr(mb_answers, "_wakers", {})
        mp.chdir(BACKEND)
        import main
        main._register_mb_wakers()
        main._start_slice_checker()
        yield main


@pytest.fixture
def unchecked(monkeypatch):
    """unchecked(kind, mbid): an event set once the checker un-checked the
    slices about that answer — that one, not a stray one's."""
    events: dict = {}
    real = slice_checks.uncheck_subject

    def uncheck(c, kind, mbid):
        out = real(c, kind, mbid)
        events.setdefault((kind, mbid), threading.Event()).set()
        return out
    monkeypatch.setattr(slice_checks, "uncheck_subject", uncheck)
    return lambda kind, mbid: events.setdefault((kind, mbid), threading.Event())


def _kept(conn, scratch_dsn, name):
    """A kept slice of another author, asked for once (its check planned),
    MusicBrainz's answers for it in."""
    s = Subtree(name)
    group, (release,) = s.group()
    _offer(scratch_dsn, _key(), s, relay=_key())
    assert mb_slice_queries.serve_slices(conn, [s.name], own_pubkey=OWN)["missing"] == [s.name]
    _answer(conn, "artist", s.gid)
    _browse(conn, s.gid, [group])
    _answer(conn, "release", release)
    return s, s.name.lower()


def test_an_idle_checker_takes_the_check_away_once_an_answer_is_kept_unknown(
        conn, scratch_dsn, checker, unchecked):
    import mb_answers
    s, key = _kept(conn, scratch_dsn, "Checker Idle")
    assert slice_checks.evaluate(conn, "mb", key) is True
    assert slice_checks.servable(conn, "mb", key, OWN) is True
    mb_answers.store("artist", s.gid, None)              # the consumer's step: the commit, then the waker
    assert unchecked("artist", s.gid).wait(WAIT_S), "the checker never un-checked the slice"
    assert slice_checks.servable(conn, "mb", key, OWN) is None


def test_a_verdict_that_read_the_answers_before_one_kept_unknown_never_stamps_over_it(
        conn, scratch_dsn, checker, unchecked, monkeypatch):
    import mb_answers
    s, key = _kept(conn, scratch_dsn, "Checker Race")
    real = slice_checks._finish
    stamped = threading.Event()

    def finish(c, family, k, b, *a, **kw):
        if k == key and not stamped.is_set():
            # The answer commits after the verdict read the answers, before its stamp.
            mb_answers.store("artist", s.gid, None)
        real(c, family, k, b, *a, **kw)
        if k == key:
            stamped.set()
    monkeypatch.setattr(slice_checks, "_finish", finish)
    checker._evaluate_slice_checks([slice_checks.subject_key("mb", key)])
    assert stamped.wait(WAIT_S), "the checker ran no verdict"
    assert unchecked("artist", s.gid).wait(WAIT_S), "the checker never un-checked the slice"
    assert slice_checks.servable(conn, "mb", key, OWN) is None
