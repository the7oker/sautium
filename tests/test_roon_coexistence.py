"""Roon and the node on one HQPlayer, and on one renderer: the scenarios of
the matrix that fakes can show, asserted as the design has them.

Roon is HQPlayer's other controller on a control connection of its own
(FakeRoon). A person's start in Roon is Stop, PlaylistClear, a PlaylistAdd
of a new raw stream from its Core and Play — as HQPlayer's own logs under
Roon show (2016–2025); at a natural transition the same without the Stop —
and no metadata. How Desktop
6.1.1's Status names that entry is guessed both ways (FakeHqp.raw_streams)
until the live run's A7 records it. On a renderer Roon goes past UPnP (RAAT):
what the renderer's AVTransport says then is guessed both ways too
(FakeRenderer.taken_by_another) until B1 records it live.

Each scenario prints one OBSERVED line — what the node did — for the record,
whether or not it meets the design. Where the code
does not yet, the test is a strict xfail naming the finding (TODAY): the fix
turns it into an XPASS, which fails until the mark goes. A take that moved by
no command of the node's is read against the playlist at that tick: Roon's
entry is another controller's from its first tick whether or not Status
names it. A renderer naming a URI the node never handed it is another
controller's: external, the node's load let go, nothing of it a listen or an
end of ours."""

import collections
import json
import re
import socket
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_hqp_backend import (_QUESTIONS, _album, _attach, _queue_stays_in_memory,  # noqa: E402,F401
                              _wait, fake, listens)
from test_dlna_backend import (_gena_port, _on_renderer, _playing_at, heard, manager,  # noqa: E402,F401
                               nodes, proxy, quality, renderers, track, wait_for)

from hqplayer_client import PlaybackState  # noqa: E402
from playback import hqp_backend as hb  # noqa: E402
from playback import hqp_diagnostics as diag  # noqa: E402
from playback import tracker  # noqa: E402
from playback.queue import CanonicalQueue  # noqa: E402


PLAYING = int(PlaybackState.PLAYING)

# (scenario, guess) -> the finding the code still meets there
TODAY = {
    ("A2", "uri"): "F2: Play while another controller plays is a bare Play to HQPlayer (_press_play)",
    ("A2", "bare"): "F2: Play while another controller plays is a bare Play to HQPlayer (_press_play)",
    ("A3", "uri"): "F3: Play while another controller paused resumes its stream (_press_play)",
    ("A3", "bare"): "F3: Play while another controller paused resumes its stream (_press_play)",
}


def _today(request, scenario: str, guess: str) -> None:
    """Marked just before the assertions that state the finding: a failure in a
    scenario's set-up stays a failure, never read as the finding."""
    why = TODAY.get((scenario, guess))
    if why:
        request.node.add_marker(pytest.mark.xfail(reason=why, strict=True, raises=AssertionError))


def _observed(scenario: str, **what) -> None:
    print("OBSERVED", scenario, json.dumps(what, default=str, sort_keys=True))


class FakeRoon:
    """Roon driving HQPlayer on a control connection of its own: per track a
    new raw stream from its Core, handed as HQPlayer's logs under Roon show
    it. Its zone's volume is Fixed (the trial sheet): no SetVolume."""

    def __init__(self, port: int):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.n = 0
        self.sent: list[str] = []

    def _send(self, *commands: str) -> None:
        for c in commands:
            self.sock.sendall(c.encode())
            answer = b""
            while not answer.endswith(b"\n"):
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise ConnectionError("HQPlayer closed Roon's control connection")
                answer += chunk
            self.sent.append(re.match(r"<(\w+)", c).group(1))

    def play_track(self) -> str:
        self.n += 1
        uri = f"http://127.0.0.1:30000/{self.n:032x}/stream.raw"
        self._send("<Stop/>", "<PlaylistClear/>", f'<PlaylistAdd uri="{uri}" queued="0" clear="0"/>', "<Play/>")
        return uri

    def pause(self) -> None:
        self._send("<Pause/>")

    def stop(self) -> None:
        # A person's stop in Roon: Pause, then Stop (HQPlayer's author, 2024-12-11)
        self._send("<Pause/>", "<Stop/>")

    def close(self) -> None:
        self.sock.close()


@pytest.fixture(params=["uri", "bare"])
def roon(request, fake):
    fake.raw_streams = request.param
    r = FakeRoon(fake.port)
    yield r
    r.close()


def _mark(fake, roon: FakeRoon) -> tuple:
    return len(fake.commands), len(roon.sent)


def _statuses(fake, since: int) -> int:
    return sum(1 for c, _ in fake.commands[since:] if c == "Status")


def _node_commands(fake, since: tuple, roon: FakeRoon) -> list:
    """What the node sent HQPlayer since `since` (_mark), its questions
    aside: every command that is not one of Roon's since then."""
    sent = collections.Counter(c for c, _ in fake.commands[since[0]:] if c not in _QUESTIONS)
    sent.subtract(collections.Counter(roon.sent[since[1]:]))
    return sorted((+sent).elements())


def _node_plays_slot_2(fake, monkeypatch):
    """The node plays slot 2 of a three-track album, its listen running."""
    from playback import manager as manager_mod
    mgr = _album(3)
    monkeypatch.setattr(manager_mod, "manager", mgr)
    b = _attach(mgr)
    assert b.select(2)
    assert _wait(lambda: mgr._latest_status.get("state") == "playing"
                 and mgr._latest_status.get("track_index") == 2
                 and tracker._play_session is not None and tracker._play_session.max_position >= 3)
    return mgr, b


def _roon_takes_over(fake, mgr, roon):
    """Roon starts a track on HQPlayer; returns (the node's position as Roon
    began, the index of Roon's Play in fake.commands, Status reads until the
    node said external — None if it never did within 40)."""
    at = mgr._latest_status.get("position")
    roon.play_track()
    k = max(i for i, (c, _) in enumerate(fake.commands) if c == "Play")
    _wait(lambda: mgr._latest_status.get("source") == "external" or _statuses(fake, k) >= 40, timeout=60)
    external = mgr._latest_status.get("source") == "external"
    return at, k, (_statuses(fake, k) if external else None)


def _rows(writes):
    return [p for sql, p in writes if "INSERT INTO listening_history" in sql]


def test_a1_roon_starts_while_the_node_plays(request, fake, roon, listens, monkeypatch):
    """A1. Designed: external from the first Status that reads Roon's entry
    (C1 Z31: an entry not ours read in step is judged at that answer); no
    command of the node's (transport only on a person's intent); the node's
    own listen ends at its own seconds and Roon's playback is never a listen
    (the own-window rule); the queue as it was; Now Playing says external."""
    writes, lastfm = listens
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    items = mgr.queue.snapshot()
    n = _mark(fake, roon)
    try:
        at, k, ticks = _roon_takes_over(fake, mgr, roon)
        mark = len(fake.commands)
        _wait(lambda: _statuses(fake, mark) >= 5)
        rows = _rows(writes)
        st = mgr._latest_status
        _observed("A1", mode=fake.raw_streams, ticks_to_external=ticks, node_commands=_node_commands(fake, n, roon),
                  rows=[(r["tid"], r["dur"], r["comp"]) for r in rows], lastfm=lastfm, at=at,
                  status={k_: st.get(k_) for k_ in ("state", "track_index", "source", "song", "position")},
                  queue_kept=mgr.queue.snapshot() == items)
        _today(request, "A1", fake.raw_streams)
        assert ticks is not None and ticks <= 1, f"external after {ticks} Status reads"
        assert _node_commands(fake, n, roon) == []
        assert [r["tid"] for r in rows] == ["track-2"] and rows[0]["dur"] <= at + 1 and not rows[0]["comp"]
        assert lastfm == ["update_now_playing"]
        assert mgr.queue.snapshot() == items
        assert (st["track_index"], st["source"]) == (0, "external")
    finally:
        b.shutdown()


def test_a_natural_advance_is_read_against_the_playlist_and_stays_ours(fake, listens, monkeypatch):
    """HQPlayer's own advance through our playlist — gapless, no stopped
    tick between the two entries — moves its take with no command of the
    node's: the playlist is read at that tick and found the queue's — the
    slot after plays as ours, in step, nothing drifted. The canary's own turn
    is out of the way, and the start's attempt closed before the advance."""
    from playback import manager as manager_mod
    monkeypatch.setattr(hb, "DRIFT_CHECK_EVERY", 10 ** 6)
    monkeypatch.setattr(diag, "OBSERVE_S", 1.5)
    mgr = _album(3)
    monkeypatch.setattr(manager_mod, "manager", mgr)
    b = _attach(mgr)
    try:
        assert b.select(1)
        assert _wait(lambda: mgr._latest_status.get("track_index") == 1 and b._attempt is None
                     and mgr._latest_status.get("state") == "playing", timeout=15)
        n = len(fake.commands)
        fake.end_now()
        assert _wait(lambda: mgr._latest_status.get("track_index") == 2
                     and mgr._latest_status.get("state") == "playing", timeout=20)
        _observed("advance", reads=sum(1 for c, _ in fake.commands[n:] if c == "PlaylistGet"),
                  mirror=b._mirror)
        assert any(c == "PlaylistGet" for c, _ in fake.commands[n:]), "the moved take was not read"
        assert b._mirror == "step" and mgr._latest_status.get("source") != "external"
    finally:
        b.shutdown()


def _takes_back(request, fake, mgr, b, roon, scenario, **context):
    """A person presses Play in the node while Roon holds HQPlayer. Designed
    (A2): the node takes HQPlayer back — its queue mirrored
    again, none of Roon's entries left, one of its slots playing. Which slot
    is the design's question, recorded, not asserted."""
    n = _mark(fake, roon)
    assert b.play()
    mirror = [f"file:///E:/Music/A/0{i}.flac" for i in (1, 2, 3)]
    _wait(lambda: fake.playlist == mirror and fake.state == PLAYING
          and (mgr._latest_status.get("track_index") or 0) >= 1, timeout=15)
    st = mgr._latest_status
    _observed(scenario, mode=fake.raw_streams, node_commands=_node_commands(fake, n, roon),
              playlist=["roon" if u.endswith("/stream.raw") else u.rsplit("/", 1)[-1] for u in fake.playlist],
              hqplayer_state=fake.state, hqplayer_entry=fake.track,
              status={k_: st.get(k_) for k_ in ("state", "track_index", "source", "position")}, **context)
    _today(request, scenario, fake.raw_streams)
    assert fake.playlist == mirror, "Roon's entry is still HQPlayer's playlist"
    assert fake.state == PLAYING and st.get("source") != "external" and st["track_index"] >= 1


def test_a2_a_play_in_the_node_while_roon_plays_takes_hqplayer_back(request, fake, roon, listens, monkeypatch):
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    try:
        at, _, ticks = _roon_takes_over(fake, mgr, roon)
        assert ticks is not None, "the node never said external"
        _takes_back(request, fake, mgr, b, roon, "A2", roon_state="playing", interrupted_at=(2, at))
    finally:
        b.shutdown()


def test_a3_roon_paused_then_a_play_in_the_node(request, fake, roon, listens, monkeypatch):
    """A3. Roon pauses its track: the node stays external and silent; a Play
    in the node takes HQPlayer back as in A2 — never resumes Roon's stream."""
    writes, lastfm = listens
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    try:
        at, _, ticks = _roon_takes_over(fake, mgr, roon)
        assert ticks is not None, "the node never said external"
        roon.pause()
        _wait(lambda: mgr._latest_status.get("state") == "paused", timeout=5)
        st = mgr._latest_status
        _observed("A3-paused", mode=fake.raw_streams, rows=len(_rows(writes)), lastfm=lastfm,
                  status={k_: st.get(k_) for k_ in ("state", "track_index", "source")})
        assert (st["state"], st["track_index"], st["source"]) == ("paused", 0, "external")
        _takes_back(request, fake, mgr, b, roon, "A3", roon_state="paused", interrupted_at=(2, at))
    finally:
        b.shutdown()


def test_a4_roon_stopped_then_a_play_in_the_node(request, fake, roon, listens, monkeypatch):
    """A4. Roon stops: HQPlayer stopped on Roon's entry, no listen of anyone's
    opens; a Play in the node takes HQPlayer back."""
    writes, lastfm = listens
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    try:
        at, _, ticks = _roon_takes_over(fake, mgr, roon)
        assert ticks is not None, "the node never said external"
        roon.stop()
        _wait(lambda: mgr._latest_status.get("state") == "stopped", timeout=5)
        mark = len(fake.commands)
        _wait(lambda: _statuses(fake, mark) >= 3)
        st = mgr._latest_status
        _observed("A4-stopped", mode=fake.raw_streams, rows=len(_rows(writes)), lastfm=lastfm,
                  session=tracker._play_session is not None,
                  status={k_: st.get(k_) for k_ in ("state", "track_index", "source")})
        assert st["state"] == "stopped" and tracker._play_session is None
        _takes_back(request, fake, mgr, b, roon, "A4", roon_state="stopped", interrupted_at=(2, at))
    finally:
        b.shutdown()


# -- the renderer: Roon past UPnP -------------------------------------------------

@pytest.fixture(params=["stopped", "playing"])
def taken(request):
    return request.param


def _renderer_plays_slot_1(tmp_path, renderers, nodes):
    r = renderers()
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=60.0), track(tmp_path, 2, seconds=60.0)])
    n = nodes(q, r)
    assert n.backend.select(1)
    wait_for(lambda: n.last.state == "playing" and n.last.queue_index == 1 and n.last.position >= 3,
             timeout=15, what="slot 1 playing")
    return r, n


_TRANSPORT = ("SetAVTransportURI", "SetNextAVTransportURI", "Play", "Pause", "Stop", "Seek", "Next", "Previous",
              "SetVolume", "SetMute")


def _sent(r, since: int) -> list:
    return [a for a, _ in r.calls[since:] if a in _TRANSPORT]


def test_b1_another_controller_takes_the_renderer_while_the_node_plays(request, tmp_path, proxy, renderers, nodes,
                                                                         quality, taken):
    """B1. Designed: the node sends the renderer nothing; from its first
    reading of the takeover no status says our slot plays (a listen would run
    on the other controller's music); it never advances the queue for it."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    mark, seen = len(r.calls), len(n.statuses)
    at = n.last.position
    r.taken_by_another(taken)
    time.sleep(4.0)
    after = n.statuses[seen:]
    ours_playing = [(s.state, s.queue_index, round(s.position, 1)) for s in after
                    if s.state == "playing" and s.queue_index]
    _observed("B1", taken=taken, at=round(at, 1), node_sent=_sent(r, mark),
              statuses=[(s.state, s.queue_index, round(s.position, 1), s.extra.get("source")) for s in after][-6:],
              ours_playing_after=len(ours_playing))
    _today(request, "B1", taken)
    assert _sent(r, mark) == []
    # The event and a poll already under way may still name the slot once
    assert len(ours_playing) <= 1, f"our slot said playing {len(ours_playing)} times after the takeover"
    assert all(s.queue_index in (0, 1) for s in after)


def test_b2_a_play_in_the_node_after_another_controller_took_the_renderer(request, tmp_path, proxy, renderers,
                                                                           nodes, quality, taken):
    """B2. Designed: a person's Play in the node plays its slot on the
    renderer again — our URI loaded or played, and the renderer playing it."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    r.taken_by_another(taken)
    time.sleep(2.5)
    mark = len(r.calls)
    ok = n.backend.play()
    _wait(lambda: r.state == "PLAYING" and "raat://" not in r.uri and n.last.state == "playing", timeout=10)
    _observed("B2", taken=taken, play_returned=ok, node_sent=_sent(r, mark), renderer=(r.state, r.uri[-24:]),
              status=(n.last.state, n.last.queue_index, round(n.last.position, 1)))
    _today(request, "B2", taken)
    assert ok and r.state == "PLAYING" and "raat://" not in r.uri
    assert n.last.state == "playing" and n.last.queue_index == 1


def test_b1_the_other_controllers_track_running_past_ours_moves_nothing(request, tmp_path, proxy, renderers, nodes,
                                                                         quality, taken):
    """B1, carried to the end of our slot's length: the other controller plays
    on past it and then stops. Designed: the node never sends the renderer
    anything for it — no load, no Play (a person's intent alone does) — and
    never says our slot reached its end."""
    r = renderers()
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=8.0), track(tmp_path, 2, seconds=60.0)])
    n = nodes(q, r)
    assert n.backend.select(1)
    wait_for(lambda: n.last.state == "playing" and n.last.queue_index == 1 and n.last.position >= 2,
             timeout=15, what="slot 1 playing")
    mark, seen = len(r.calls), len(n.statuses)
    r.taken_by_another(taken)
    time.sleep(8.0)                    # past slot 1's 8 s on the clock
    if taken == "playing":
        r.taken_by_another("stopped")  # the other controller stops: STOPPED on its URI
    time.sleep(4.0)
    after = n.statuses[seen:]
    _observed("B1-end", taken=taken, node_sent=_sent(r, mark), last_uri=r.uri[-24:],
              statuses=[(s.state, s.queue_index, round(s.position, 1)) for s in after][-5:],
              ended_ours=any(s.queue_index == 2 for s in after))
    _today(request, "B1-end", taken)
    assert not any(s.queue_index == 2 for s in after)
    assert _sent(r, mark) == []


def test_b_a_stale_uri_of_ours_after_our_own_load_is_no_other_controller(tmp_path, proxy, renderers, nodes,
                                                                          quality):
    """After the node's own load the renderer names the URI before it again,
    playing — as a renderer does for a tick after our own Stop,
    SetAVTransportURI and Play, and as one that sits PAUSED on the old URI
    does. Every URI this attachment handed it is ours however stale: never
    another controller's — the slot plays on, nothing is let go or sent."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    before = r.uri, r.meta
    assert n.backend.next()
    wait_for(lambda: n.last.state == "playing" and n.last.queue_index == 2 and n.last.position >= 2,
             timeout=15, what="slot 2 playing")
    ours = r.uri, r.meta
    mark, seen = len(r.calls), len(n.statuses)
    r.uri, r.meta = before
    r._run(r._changed())
    time.sleep(2.5)                 # its event, and the polls that read it
    r.uri, r.meta = ours
    r._run(r._changed())
    wait_for(lambda: n.last.state == "playing" and n.last.queue_index == 2, timeout=5, what="slot 2 on")
    after = n.statuses[seen:]
    _observed("B-stale", node_sent=_sent(r, mark),
              statuses=[(s.state, s.queue_index, s.extra.get("source")) for s in after][-6:])
    assert not any(s.extra.get("source") == "external" for s in after)
    assert _sent(r, mark) == [] and n.backend._current_url == ours[0]


def test_b1_the_listen_ends_at_its_own_seconds_and_the_other_controllers_track_is_none_of_ours(
        tmp_path, proxy, renderers, manager, heard, quality, taken):
    """B1 through the manager and the play tracker: the node's listen ends at
    its own seconds, written once under half its track; what the other
    controller plays on is no listen of ours — its seconds never run our
    slot's clock past the scrobble's floor (F4: the node scrobbled a track
    that did not sound)."""
    listened, lastfm = heard
    r = renderers()
    _on_renderer(manager, r, [track(tmp_path, 1, seconds=60.0), track(tmp_path, 2, seconds=60.0)])
    _playing_at(manager, r, 1)
    assert manager.backend().seek(25)
    _playing_at(manager, r, 26)
    mark = len(r.calls)
    r.taken_by_another(taken)
    time.sleep(7.0)                 # past 30 s on our slot's clock: half the track, the scrobble's floor
    _observed("B1-listen", taken=taken, listened=listened, lastfm=lastfm, node_sent=_sent(r, mark),
              status={k_: manager.latest_status.get(k_) for k_ in ("state", "track_index", "source")})
    assert lastfm == [("update_now_playing", "Track 1")]
    assert [t for t, _ in listened] == ["00000000-0000-0000-0000-000000000001"] and listened[0][1] < 50
    assert _sent(r, mark) == []
