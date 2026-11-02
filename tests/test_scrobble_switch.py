"""The Scrobbling switch (Profile) holds back every play report to Last.fm —
the live now-playing as well as the scrobble — and a switch that cannot be
read sends nothing (playback/tracker._scrobble_async, the one gate).

On a real PostgreSQL: the module's scratch database (conftest.scratch_dsn)
holds the switch and takes the listens. Last.fm itself is a recorder in place
of the network object the tracker would build for the owner's session.
"""

import json
import sys
import threading
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")

from conftest import PG, SESSION_DATABASE  # noqa: E402
from playback import tracker  # noqa: E402
from playback.queue import QueueItem  # noqa: E402


class _LastFm:
    """Records each call the tracker makes of the Last.fm network."""

    def __init__(self):
        self.calls: list[str] = []
        self.called = threading.Event()

    def __getattr__(self, method):
        def call(**kwargs):
            self.calls.append(method)
            self.called.set()
        return call


def _pool(**connect):
    import psycopg2.pool
    return psycopg2.pool.ThreadedConnectionPool(1, 2, **connect)


@pytest.fixture
def node(scratch_dsn, monkeypatch):
    import db_pool
    pool = _pool(dsn=scratch_dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    lastfm = _LastFm()
    built: list[int] = []

    def scrobbler():
        built.append(1)
        return lastfm

    monkeypatch.setattr(tracker, "_get_scrobbler", scrobbler)
    monkeypatch.setattr(tracker, "_play_session", None)
    monkeypatch.setattr(tracker, "_carried", False)
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    yield conn, lastfm, built
    conn.close()
    pool.closeall()


def _switch(conn, on: bool) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO user_settings (key, value) VALUES ('lastfm.scrobbling_enabled', %s::jsonb)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (json.dumps(on),))


def _item(track_id: str) -> QueueItem:
    return QueueItem(track_id=track_id, media_file_id=None,
                     source={"kind": "proxy", "token": "tok" + track_id},
                     title="A Title", artist="An Artist", duration_seconds=200.0,
                     preview=True, provider="youtube", excerpt=False)


def _listen(track_id: str, positions) -> None:
    for pos in positions:
        tracker.track_play_event("playing", pos, 200.0, _item(track_id))


def test_with_scrobbling_off_last_fm_hears_nothing_not_even_now_playing(node):
    conn, lastfm, built = node
    _switch(conn, False)
    _listen(str(uuid.uuid4()), (0.0, 1.0, 150.0, 199.0))
    tracker.track_play_event("stopped", 0.0, 0.0, None)
    assert built == [] and lastfm.calls == []


def test_a_switch_that_cannot_be_read_sends_nothing_and_says_so(node, monkeypatch, caplog):
    _, lastfm, built = node
    import db_pool
    # The run's session database is empty: the switch's table is not there.
    pool = _pool(dbname=SESSION_DATABASE, **PG)
    monkeypatch.setattr(db_pool, "_pool", pool)
    try:
        with caplog.at_level("ERROR", logger=tracker.logger.name):
            _listen(str(uuid.uuid4()), (0.0, 150.0))
    finally:
        pool.closeall()
    assert built == [] and lastfm.calls == []
    assert any("Scrobbling" in r.getMessage() for r in caplog.records
               if r.name == tracker.logger.name and r.levelname == "ERROR")


def test_a_listen_the_switch_held_back_scrobbles_once_it_is_on_before_the_threshold(node):
    conn, lastfm, built = node
    _switch(conn, False)
    track = str(uuid.uuid4())
    _listen(track, (0.0, 20.0))           # its start: now-playing held back
    _switch(conn, True)
    _listen(track, (120.0,))              # past half of it: the scrobble goes
    assert lastfm.called.wait(5)
    assert lastfm.calls == ["scrobble"]
