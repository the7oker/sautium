"""
The demo policy (streaming/demo.py) — pure logic: the ledger writes and the
proxy drop are recorded, the provider registry is a dict. Run inside the
backend container:

    python -m pytest test_demo_policy.py -q
"""

import pytest

from playback.queue import QueueItem
from streaming import demo, service
from streaming.base import ProviderManifest, StreamProvider


class _P(StreamProvider):
    def __init__(self, pid, **flags):
        flags.setdefault("lossless", False)
        self.manifest = ProviderManifest(id=pid, name=pid, kind="direct_url", **flags)

    def fetch(self, query):
        raise NotImplementedError


PROVIDERS = {"youtube": _P("youtube", demo_limited=True),
             "deezer": _P("deezer", lossless=True),
             "deezer_preview": _P("deezer_preview", excerpt=True)}


def _item(track_id, provider, *, excerpt=False):
    return QueueItem(track_id=track_id, media_file_id=None,
                     source={"kind": "proxy", "token": "tok" + track_id},
                     title=track_id, artist="A", preview=True, provider=provider, excerpt=excerpt)


def _tick(state, position, length=200.0):
    return {"state": state, "position": position, "length": length}


@pytest.fixture
def policy(monkeypatch):
    marks, drops = [], []
    monkeypatch.setattr(service, "get_provider", lambda pid: PROVIDERS.get(pid))
    monkeypatch.setattr(demo, "mark_consumed", lambda t, p: marks.append((t, p)))
    monkeypatch.setattr(demo, "_drop_buffer", lambda t: drops.append(t))
    monkeypatch.setattr(demo, "_watch", None)
    return marks, drops


def test_admits_drops_only_demo_limited_providers_for_spent_tracks():
    spent = {"t1"}
    yt, dz, clip = PROVIDERS["youtube"], PROVIDERS["deezer"], PROVIDERS["deezer_preview"]
    assert not demo.admits(yt, "t1", spent)
    assert demo.admits(yt, "t2", spent) and demo.admits(yt, None, spent)
    assert demo.admits(dz, "t1", spent) and demo.admits(clip, "t1", spent)


def test_a_demo_listen_is_spent_once_past_ninety_percent_and_the_buffer_drops_after(policy):
    marks, drops = policy
    yt = _item("t1", "youtube")
    for pos in (10.0, 100.0, 179.0):
        demo.status_observer(_tick("playing", pos), yt)
    assert marks == [] and drops == []
    demo.status_observer(_tick("playing", 181.0), yt)       # 90.5 %
    demo.status_observer(_tick("playing", 195.0), yt)
    assert marks == [("t1", "youtube")]                     # once
    demo.status_observer(_tick("paused", 195.0), yt)        # an interruption, not the end
    assert drops == []
    demo.status_observer(_tick("playing", 3.0), _item("t2", "youtube"))   # the next track
    assert drops == ["t1"]
    demo.status_observer(_tick("stopped", 0.0), _item("t2", "youtube"))   # t2 unspent: nothing
    assert drops == ["t1"] and marks == [("t1", "youtube")]


def test_a_stop_ends_the_listen_and_a_seek_into_the_last_tenth_counts(policy):
    marks, drops = policy
    yt = _item("t1", "youtube")
    demo.status_observer(_tick("playing", 5.0), yt)
    demo.status_observer(_tick("playing", 190.0), yt)       # seek → 95 %
    demo.status_observer(_tick("stopped", 0.0), yt)
    assert marks == [("t1", "youtube")] and drops == ["t1"]


def test_excerpts_and_unlimited_providers_are_never_watched(policy):
    marks, drops = policy
    for item in (_item("t1", "deezer_preview", excerpt=True), _item("t2", "deezer"),
                 QueueItem(track_id="t3", media_file_id=5, source={"kind": "file", "path": "x"})):
        demo.status_observer(_tick("playing", 199.0), item)
        demo.status_observer(_tick("stopped", 0.0), item)
    assert marks == [] and drops == []


def test_a_length_the_output_has_not_reported_yet_spends_nothing(policy):
    marks, _drops = policy
    demo.status_observer(_tick("playing", 50.0, length=0.0), _item("t1", "youtube"))
    assert marks == []
