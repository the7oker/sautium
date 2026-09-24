"""One Last.fm pace per process (backend/lastfm.py).

Every request goes through `_PacedNetwork._delay_call`, which pylast calls
before each download once `limit_rate` is set. These tests guard the pylast
internals that hook relies on, the spacing under parallel callers, the single
getInfo read of an artist, and the cooldown reset on success. Pure logic — no
network, no database.
"""

import inspect
import threading
import time
from xml.dom.minidom import parseString

import pylast
import pytest

import lastfm
from lastfm import LastFmService


def test_the_pylast_hooks_the_pace_relies_on_are_still_there():
    net = lastfm._PacedNetwork(api_key="k", api_secret="s")
    assert net.limit_rate is True
    src = inspect.getsource(pylast._Request._download_response)
    assert "limit_rate" in src and "_delay_call()" in src
    assert callable(getattr(pylast._BaseObject, "_request"))
    assert pylast.Artist("x", net).ws_prefix == "artist"


def test_parallel_callers_never_share_a_slot(monkeypatch):
    monkeypatch.setattr(lastfm, "_PACE_S", 0.05)
    monkeypatch.setattr(lastfm, "_last_call", 0.0)
    nets = [lastfm._PacedNetwork(api_key="k", api_secret="s") for _ in range(6)]
    stamps, lock = [], threading.Lock()

    def call(net):
        for _ in range(2):
            net._delay_call()
            with lock:
                stamps.append(time.monotonic())

    threads = [threading.Thread(target=call, args=(n,)) for n in nets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(stamps) == 12
    assert min(gaps) >= 0.05 - 0.005


GET_INFO = """<?xml version="1.0" encoding="utf-8"?>
<lfm status="ok"><artist>
  <name>Sade</name>
  <mbid>2f9ecbed-27be-40e6-abca-6de49d50299e</mbid>
  <url>https://www.last.fm/music/Sade</url>
  <stats><listeners>2345678</listeners><playcount>98765432</playcount></stats>
  <similar><artist><name>Anita Baker</name><url>https://www.last.fm/music/Anita+Baker</url></artist></similar>
  <tags><tag><name>soul</name></tag></tags>
  <bio><published>01 Jan 2006</published>
    <summary><![CDATA[Sade is an English band.]]></summary>
    <content><![CDATA[Sade is an English band formed in London.]]></content></bio>
</artist></lfm>"""


def test_one_get_info_answer_carries_bio_stats_and_mbid():
    info = lastfm._artist_info(parseString(GET_INFO))
    assert info == {
        "bio": {"summary": "Sade is an English band.",
                "content": "Sade is an English band formed in London."},
        "stats": {"listeners": 2345678, "playcount": 98765432},
        "mbid": "2f9ecbed-27be-40e6-abca-6de49d50299e",
    }


def test_an_artist_without_mbid_or_bio_reads_as_none():
    doc = parseString('<lfm status="ok"><artist><name>X</name>'
                      '<stats><listeners>3</listeners><playcount>4</playcount></stats>'
                      '<bio><summary></summary></bio></artist></lfm>')
    info = lastfm._artist_info(doc)
    assert info["mbid"] is None and info["bio"] == {"summary": None, "content": None}


def test_a_successful_call_clears_the_cooldown(monkeypatch):
    cleared = []
    monkeypatch.setattr("api_cooldown.clear", lambda source: cleared.append(source))
    assert LastFmService._with_retry(lambda: 42, base_delay=0) == 42
    assert cleared == ["lastfm"]


class _Net:
    """pylast's exceptions read the network's name when stringified."""
    name = "Last.fm"


def test_a_verdict_does_not_clear_it(monkeypatch):
    cleared = []
    monkeypatch.setattr("api_cooldown.clear", lambda source: cleared.append(source))

    def not_found():
        raise pylast.WSError(_Net(), "6", "The artist you supplied could not be found")

    with pytest.raises(pylast.WSError):
        LastFmService._with_retry(not_found, base_delay=0)
    assert cleared == []
