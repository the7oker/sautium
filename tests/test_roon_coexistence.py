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

Each scenario prints one OBSERVED line — what the node did — for the record.
A take that moved by no command of the node's is read against the playlist
at that tick: Roon's entry is another controller's from its first tick
whether or not Status names it. A renderer naming a URI the node never
handed it is another controller's: external, the node's load let go,
nothing of it a listen or an end of ours. Another controller that cuts the
node's own listen leaves the spot there: Now Playing names that track,
saying another app took the output, and a Play in the node takes the output
back at it — the queue taken afresh, never the other controller's stream
resumed."""

import asyncio
import collections
import json
import re
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_hqp_backend import (_QUESTIONS, _album, _attach, _item, _link, _queue_stays_in_memory,  # noqa: E402,F401
                              _routes_on, _stopped_at_spot, _streams_on_hqplayer, _wait, fake, listens)
from test_dlna_backend import (FakeRenderer, _gena_port, _on_renderer, _playing_at, _seconds,  # noqa: E402,F401
                               heard, manager, nodes, proxy, quality, renderers, streamed, track, wait_for)

from hqplayer_client import PlaybackState, TrackStatus  # noqa: E402
from playback import hqp_backend as hb  # noqa: E402
from playback import hqp_link as hl  # noqa: E402
from playback import hqp_diagnostics as diag  # noqa: E402
from playback import tracker  # noqa: E402
from playback.queue import CanonicalQueue  # noqa: E402


PLAYING = int(PlaybackState.PLAYING)


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

    def next_track(self) -> str:
        """Roon's natural transition, once HQPlayer ended the stream it
        played: the same as a start, without the Stop."""
        self.n += 1
        uri = f"http://127.0.0.1:30000/{self.n:032x}/stream.raw"
        self._send("<PlaylistClear/>", f'<PlaylistAdd uri="{uri}" queued="0" clear="0"/>', "<Play/>")
        return uri

    def pause(self) -> None:
        self._send("<Pause/>")

    def start_before_its_play(self) -> str:
        """Roon's start up to its Play: Stop, PlaylistClear and the add of
        its stream (its Play follows once its Core has the stream ready)."""
        self.n += 1
        uri = f"http://127.0.0.1:30000/{self.n:032x}/stream.raw"
        self._send("<Stop/>", "<PlaylistClear/>", f'<PlaylistAdd uri="{uri}" queued="0" clear="0"/>')
        return uri

    def play_again(self) -> None:
        """Roon's Play sent again on an entry HQPlayer refused: 4–5 a second
        until its zone is stopped (ROON-COEX §10.1, A8: 200 Plays in 53 s)."""
        self._send("<Play/>")

    def resume(self) -> None:
        self._send("<Play/>")

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


def test_a1_roon_starts_while_the_node_plays(fake, roon, listens, monkeypatch):
    """A1. Designed: external from the first Status that reads Roon's entry
    (C1 Z31: an entry not ours read in step is judged at that answer); no
    command of the node's (transport only on a person's intent); the node's
    own listen is held at its own seconds — a takeover is a pause for it
    (F6) — and Roon's playback is never a listen (the own-window rule); the
    queue as it was. Roon cut the node's listen:
    Now Playing keeps naming that track, stopped at the second of the node's
    last own reading — the spot a Play takes HQPlayer back at — saying
    another app took HQPlayer (external)."""
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
                  status={k_: st.get(k_) for k_ in ("state", "track_index", "source", "song", "position",
                                                    "resume")},
                  queue_kept=mgr.queue.snapshot() == items)
        assert ticks is not None and ticks <= 1, f"external after {ticks} Status reads"
        assert _node_commands(fake, n, roon) == []
        held = tracker._play_session
        assert rows == [] and held is not None and held.track_id == "track-2" and held.max_position <= at + 1
        assert lastfm == ["update_now_playing"]
        assert mgr.queue.snapshot() == items
        assert (st["state"], st["track_index"], st["track_id"], st["source"]) == (
            "stopped", 2, "track-2", "external")
        assert st["resume"]["index"] == 2 and int(at) <= st["resume"]["position"] <= int(at) + 1
        assert st["position"] == st["resume"]["position"] and st["song"] != "Roon"
    finally:
        b.shutdown()


def test_roon_starting_during_a_queue_edit_under_a_status_that_names_no_uri_is_no_listen_of_ours(
        fake, listens, monkeypatch):
    """Roon starts while a queue mutation is in flight, its Status naming no
    URI: the playlist read waits for the mutation, and meanwhile the layout's
    slot is no verdict — Roon's stream is never a listen of slot 1 nor its
    now-playing. Read once the mutation ends, it is another controller's,
    and the spot stands where it cut the node's own listen."""
    import threading
    writes, lastfm = listens
    fake.raw_streams = "bare"
    roon = FakeRoon(fake.port)
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    hold = threading.Event()

    def edit():
        with mgr.queue.mutation():
            hold.wait(8)
    t = threading.Thread(target=edit, daemon=True)
    try:
        t.start()
        time.sleep(0.3)
        n_lastfm = len(lastfm)
        at = mgr._latest_status.get("position")
        roon.play_track()
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 3, 10)
        during = dict(mgr._latest_status)
        hold.set()
        t.join()
        _wait(lambda: mgr._latest_status.get("source") == "external", 10)
        st = mgr._latest_status
        rows = _rows(writes)
        _observed("A1-mutation", at=at, during=(during.get("state"), during.get("track_index")),
                  rows=[(r["tid"], r["dur"]) for r in rows], lastfm=lastfm[n_lastfm:],
                  status={k_: st.get(k_) for k_ in ("state", "track_index", "source", "resume")})
        assert during.get("track_index") != 1 and lastfm[n_lastfm:] == []
        held = tracker._play_session
        assert rows == [] and held is not None and held.track_id == "track-2" and held.max_position <= at + 1
        assert st["source"] == "external" and st["resume"]["index"] == 2
        assert int(at) <= st["resume"]["position"] <= int(at) + 1
    finally:
        hold.set()
        b.shutdown()
        roon.close()


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


MIRROR = [f"file:///E:/Music/A/0{i}.flac" for i in (1, 2, 3)]


def _takes_back(fake, mgr, b, roon, scenario, **context):
    """A person presses Play in the node while Roon holds HQPlayer. Designed
    (A2): the node takes HQPlayer back — the queue taken afresh, as from a
    stop, none of Roon's entries left, never HQPlayer's own Play of Roon's
    stream — at the spot where Roon's takeover cut the node's own listen:
    slot 2, its start sought to that second."""
    n = _mark(fake, roon)
    spot = mgr._latest_status.get("resume")
    assert b.play()
    _wait(lambda: fake.playlist == MIRROR and fake.state == PLAYING
          and (mgr._latest_status.get("track_index") or 0) >= 1, timeout=15)
    st = mgr._latest_status
    seeks = [int(float(a["position"])) for c, a in fake.commands[n[0]:] if c == "Seek"]
    _observed(scenario, mode=fake.raw_streams, node_commands=_node_commands(fake, n, roon),
              playlist=["roon" if u.endswith("/stream.raw") else u.rsplit("/", 1)[-1] for u in fake.playlist],
              hqplayer_state=fake.state, hqplayer_entry=fake.track, spot=spot, seeks=seeks,
              status={k_: st.get(k_) for k_ in ("state", "track_index", "source", "position")}, **context)
    assert fake.playlist == MIRROR, "Roon's entry is still HQPlayer's playlist"
    assert fake.state == PLAYING and st.get("source") != "external"
    assert (fake.track, st["track_index"]) == (2, 2)
    assert spot is not None and spot["index"] == 2 and seeks == [spot["position"]]


def test_a2_a_play_in_the_node_while_roon_plays_takes_hqplayer_back(fake, roon, listens, monkeypatch):
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    try:
        at, _, ticks = _roon_takes_over(fake, mgr, roon)
        assert ticks is not None, "the node never said external"
        _takes_back(fake, mgr, b, roon, "A2", roon_state="playing", interrupted_at=(2, at))
    finally:
        b.shutdown()


def test_a3_roon_paused_then_a_play_in_the_node(fake, roon, listens, monkeypatch):
    """A3. Roon pauses its track: the node stays silent, Now Playing at the
    spot Roon's takeover left, external still; a Play in the node takes
    HQPlayer back there as in A2 — never resumes Roon's stream."""
    writes, lastfm = listens
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    try:
        at, _, ticks = _roon_takes_over(fake, mgr, roon)
        assert ticks is not None, "the node never said external"
        roon.pause()
        mark = len(fake.commands)
        _wait(lambda: fake.state == int(PlaybackState.PAUSED) and _statuses(fake, mark) >= 3, timeout=10)
        st = mgr._latest_status
        _observed("A3-paused", mode=fake.raw_streams, rows=len(_rows(writes)), lastfm=lastfm,
                  status={k_: st.get(k_) for k_ in ("state", "track_index", "source", "resume")})
        assert (st["state"], st["track_index"], st["source"], st["resume"]["index"]) == (
            "stopped", 2, "external", 2)
        _takes_back(fake, mgr, b, roon, "A3", roon_state="paused", interrupted_at=(2, at))
    finally:
        b.shutdown()


def test_a4_roon_stopped_then_a_play_in_the_node(fake, roon, listens, monkeypatch):
    """A4. Roon stops: HQPlayer stopped on Roon's entry, no listen of anyone's
    opens, Now Playing at the spot Roon's takeover left — HQPlayer is no
    other controller's playing now; a Play in the node takes HQPlayer back
    there."""
    writes, lastfm = listens
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    try:
        at, _, ticks = _roon_takes_over(fake, mgr, roon)
        assert ticks is not None, "the node never said external"
        roon.stop()
        mark = len(fake.commands)
        _wait(lambda: fake.state == int(PlaybackState.STOPPED) and _statuses(fake, mark) >= 3, timeout=10)
        st = mgr._latest_status
        _observed("A4-stopped", mode=fake.raw_streams, rows=len(_rows(writes)), lastfm=lastfm,
                  session=tracker._play_session is not None,
                  status={k_: st.get(k_) for k_ in ("state", "track_index", "source", "resume")})
        # A takeover is a pause for the listen (F6): held through Roon's stop, nothing written
        assert st["state"] == "stopped" and tracker._play_session is not None and _rows(writes) == []
        assert (st["track_index"], st.get("source"), st["resume"]["index"]) == (2, None, 2)
        _takes_back(fake, mgr, b, roon, "A4", roon_state="stopped", interrupted_at=(2, at))
    finally:
        b.shutdown()


def test_the_spot_roons_takeover_left_holds_until_a_play_in_the_node(fake, roon, listens, monkeypatch):
    """Roon's takeover cut the node's listen of slot 2: the spot holds while
    Roon plays on — its stream ended, its next track started (no Stop),
    paused, played again — Now Playing naming slot 2 at that second the whole
    time, the node sending nothing; then a Play in the node takes HQPlayer
    back there."""
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    try:
        at, _, ticks = _roon_takes_over(fake, mgr, roon)
        assert ticks is not None, "the node never said external"
        spot = mgr._latest_status.get("resume")
        n = _mark(fake, roon)
        seen = []

        def held(steps: int = 3) -> None:
            mark = len(fake.commands)
            _wait(lambda: _statuses(fake, mark) >= steps, timeout=10)
            st = mgr._latest_status
            seen.append((st.get("state"), st.get("track_index"), st.get("source"), st.get("resume")))
        fake.end_now()                  # Roon's stream ends: HQPlayer stops on no entry
        held()
        roon.next_track()
        held()
        roon.pause()
        held()
        roon.resume()
        held()
        _observed("A-spot", mode=fake.raw_streams, spot=spot, seen=seen, node_commands=_node_commands(fake, n, roon))
        assert spot is not None and spot["index"] == 2
        assert [s[1:] for s in seen] == [(2, None, spot), (2, "external", spot), (2, "external", spot),
                                         (2, "external", spot)]
        assert all(s[0] == "stopped" for s in seen) and _node_commands(fake, n, roon) == []
        _takes_back(fake, mgr, b, roon, "A-spot", interrupted_at=(2, at))
    finally:
        b.shutdown()


def test_roon_taking_an_idle_hqplayer_cuts_no_listen_and_leaves_no_spot(fake, roon, listens, monkeypatch):
    """The node stopped first — its own Stop ended its listen — and Roon took
    an idle HQPlayer: no listen was cut, no spot is set, Now Playing says
    external; a Play in the node takes HQPlayer back as from a stop with no
    spot: the queue afresh from its head, from 0."""
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    try:
        assert b.stop()
        assert _wait(lambda: fake.state == int(PlaybackState.STOPPED)
                     and mgr._latest_status.get("state") == "stopped")
        _, _, ticks = _roon_takes_over(fake, mgr, roon)
        st = mgr._latest_status
        _observed("A-idle", mode=fake.raw_streams, ticks_to_external=ticks,
                  status={k_: st.get(k_) for k_ in ("state", "track_index", "source", "resume")})
        assert ticks is not None and (st["track_index"], st["source"], st.get("resume")) == (0, "external", None)
        n = _mark(fake, roon)
        assert b.play()
        _wait(lambda: fake.playlist == MIRROR and fake.state == PLAYING
              and (mgr._latest_status.get("track_index") or 0) >= 1, timeout=15)
        assert fake.playlist == MIRROR and fake.track == 1
        assert not any(c == "Seek" for c, _ in fake.commands[n[0]:])
    finally:
        b.shutdown()



def test_a_play_before_the_canary_judged_roons_entry_still_takes_the_queue_afresh(fake, listens, monkeypatch):
    """Roon's entry is read as another controller's (its Status names its
    URI) while the canary's verdict on the playlist could not stand yet — a
    read HQPlayer did not answer, a mutation in flight — so the mirror is
    still said to be in step. A Play then is a play press all the same: the
    queue taken afresh, never a start selected into Roon's playlist."""
    fake.raw_streams = "uri"
    roon = FakeRoon(fake.port)
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    try:
        monkeypatch.setattr(b, "_judge_mirror", lambda *a, **k: False)
        _, _, ticks = _roon_takes_over(fake, mgr, roon)
        assert ticks is not None and b._mirror == "step"
        _takes_back(fake, mgr, b, roon, "A-unjudged", roon_state="playing")
    finally:
        b.shutdown()
        roon.close()


def _roon_holds_the_spot(fake, monkeypatch, *, mode="uri"):
    """The node plays slot 2; Roon takes HQPlayer over it: the spot there.
    Returns (manager, backend, roon, the spot)."""
    fake.raw_streams = mode
    roon = FakeRoon(fake.port)
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    _, _, ticks = _roon_takes_over(fake, mgr, roon)
    assert ticks is not None, "the node never said external"
    spot = mgr._latest_status.get("resume")
    assert spot is not None and spot["index"] == 2
    return mgr, b, roon, spot


def _roon_holds_an_idle_hqplayer(fake, monkeypatch):
    """The node's own Stop, then Roon takes the idle HQPlayer: external, no
    spot. Returns (manager, backend, roon)."""
    fake.raw_streams = "uri"
    roon = FakeRoon(fake.port)
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    assert b.stop()
    assert _wait(lambda: fake.state == int(PlaybackState.STOPPED) and mgr._latest_status.get("state") == "stopped")
    _, _, ticks = _roon_takes_over(fake, mgr, roon)
    assert ticks is not None and mgr._latest_status.get("resume") is None
    return mgr, b, roon


def test_while_roon_holds_hqplayer_the_radio_refills_nothing_and_an_append_is_the_queues_alone(
        fake, listens, monkeypatch):
    """Radio on, Roon's takeover leaving the spot near the queue's end: the
    radio does not refill from it (nothing of the queue plays), and an
    append — a filler's, a person's — is the queue's alone: nothing goes
    into Roon's playlist, so when Roon's stream ends HQPlayer plays nothing
    of ours by itself, and no listen of ours opens."""
    from routers import player as player_router
    writes, lastfm = listens
    mgr, b, roon, spot = _roon_holds_the_spot(fake, monkeypatch)
    fired = []
    try:
        monkeypatch.setattr(player_router, "manager", mgr)
        monkeypatch.setattr(player_router, "_radio_refilling", False)
        monkeypatch.setattr(player_router, "_radio_fill", lambda seed, gen: fired.append(seed))
        mgr.subscribe_status(player_router._radio_refill_observer)
        mgr.radio_mode = True
        mark = len(fake.commands)
        _wait(lambda: _statuses(fake, mark) >= 3)
        refilled = list(fired)
        n = _mark(fake, roon)
        n_lastfm = len(lastfm)
        assert mgr.append([_item("E:/Music/B/09.flac", 9)], generation=mgr.queue.generation) == 1
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 2)
        sent, held = _node_commands(fake, n, roon), list(fake.playlist)
        fake.end_now()                      # Roon's stream ends: HQPlayer moves on by itself, if it can
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 3)
        _observed("A-radio", refilled=refilled, node_commands=sent, playlist=[u[-20:] for u in held],
                  hqplayer=(fake.state, fake.track), lastfm=lastfm[n_lastfm:])
        assert refilled == [] and sent == []
        assert len(held) == 1 and held[0].endswith("/stream.raw")
        assert fake.state == int(PlaybackState.STOPPED) and lastfm[n_lastfm:] == []
        assert mgr.queue.item_at(4).track_id == "track-9"
    finally:
        b.shutdown()
        roon.close()


def test_a_seek_while_roon_holds_hqplayer_moves_the_spot_and_sends_nothing(fake, listens, monkeypatch):
    """Now Playing names the node's track at the spot Roon's takeover left —
    a bar the person can scrub: a Seek moves the spot, nothing goes to
    HQPlayer (Roon's stream is not ours to move), and Play takes HQPlayer
    back at the new second. With no spot — Roon took an idle HQPlayer —
    there is nothing of ours to seek in: refused in words, nothing sent."""
    fake.takes_seek = True
    mgr, b, roon, spot = _roon_holds_the_spot(fake, monkeypatch)
    try:
        n = _mark(fake, roon)
        assert b.seek(120)
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 2)
        assert _node_commands(fake, n, roon) == []
        assert mgr._latest_status.get("resume") == {"index": 2, "position": 120}
        m = len(fake.commands)
        assert b.play()
        _wait(lambda: fake.playlist == MIRROR and fake.state == PLAYING, timeout=15)
        assert [int(float(a["position"])) for c, a in fake.commands[m:] if c == "Seek"] == [120]
    finally:
        b.shutdown()
        roon.close()


def test_a_seek_while_roon_holds_an_idle_hqplayer_is_refused(fake, listens, monkeypatch):
    mgr, b, roon = _roon_holds_an_idle_hqplayer(fake, monkeypatch)
    try:
        n = _mark(fake, roon)
        assert not b.seek(120)
        assert "another app is playing through" in b.refusal() and _node_commands(fake, n, roon) == []
    finally:
        b.shutdown()
        roon.close()


@pytest.mark.parametrize("press, slot", [("next", 3), ("previous", 1)])
def test_l11_next_and_previous_at_the_spot_roons_takeover_left_take_hqplayer_back_at_their_slot(
        fake, listens, monkeypatch, press, slot):
    """L11 (ROON-COEX-DESIGN, the addendum of 2026-11-01 evening: one rule) —
    a press that starts playback takes the output back at its slot: Next and
    Previous at the spot Roon's takeover left start the slot beside it, from
    its start, the queue taken afresh — never HQPlayer's own Next in Roon's
    playlist."""
    mgr, b, roon, spot = _roon_holds_the_spot(fake, monkeypatch)
    try:
        n = _mark(fake, roon)
        assert getattr(b, press)()
        _wait(lambda: fake.playlist == MIRROR and fake.state == PLAYING and fake.track == slot, timeout=15)
        st = mgr._latest_status
        _observed("L11-" + press, node_commands=_node_commands(fake, n, roon), hqplayer=(fake.state, fake.track),
                  status={k_: st.get(k_) for k_ in ("state", "track_index", "source")})
        assert fake.playlist == MIRROR and fake.track == slot
        assert not any(c in ("Next", "Previous") for c, _ in fake.commands[n[0]:])
        assert not any(c == "Seek" for c, _ in fake.commands[n[0]:])
        assert st.get("source") != "external"
    finally:
        b.shutdown()
        roon.close()


def test_next_or_previous_while_roon_holds_an_idle_hqplayer_is_refused(fake, listens, monkeypatch):
    mgr, b, roon = _roon_holds_an_idle_hqplayer(fake, monkeypatch)
    try:
        n = _mark(fake, roon)
        assert not b.next() and not b.previous()
        assert "another app is playing through" in b.refusal() and _node_commands(fake, n, roon) == []
    finally:
        b.shutdown()
        roon.close()


@pytest.mark.parametrize("press", ["stop", "pause"])
def test_a_tap_or_a_stop_while_roon_holds_hqplayer(fake, listens, monkeypatch, press):
    """L11: a Stop or a Pause while Roon holds HQPlayer sends the other app
    nothing — Roon's stream plays on — and the spot its takeover left stays;
    a tap then takes HQPlayer back at the tapped track, from its start: the
    queue afresh."""
    mgr, b, roon, spot = _roon_holds_the_spot(fake, monkeypatch)
    try:
        n = _mark(fake, roon)
        assert getattr(b, press)()
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 3)
        assert fake.state == PLAYING and fake.playlist[0].endswith("/stream.raw"), "Roon's stream plays on"
        assert _node_commands(fake, n, roon) == [] and mgr._latest_status.get("resume") == spot
        m = len(fake.commands)
        assert b.select(3, "track-3")
        _wait(lambda: fake.playlist == MIRROR and fake.state == PLAYING and fake.track == 3, timeout=15)
        assert fake.track == 3 and not any(c == "Seek" for c, _ in fake.commands[m:])
        assert mgr._latest_status.get("source") != "external"
    finally:
        b.shutdown()
        roon.close()


def test_roon_taking_over_the_nodes_own_pause_leaves_the_spot_at_its_second(fake, listens, monkeypatch):
    """The node paused at its slot, then Roon took HQPlayer: a listen paused
    is a listen cut — the spot at that slot and the paused second."""
    fake.raw_streams = "uri"
    roon = FakeRoon(fake.port)
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    try:
        assert b.pause()
        assert _wait(lambda: mgr._latest_status.get("state") == "paused")
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 2)
        at = mgr._latest_status.get("position")
        _, _, ticks = _roon_takes_over(fake, mgr, roon)
        st = mgr._latest_status
        assert ticks is not None and (st["state"], st["source"]) == ("stopped", "external")
        assert st["resume"] == {"index": 2, "position": int(at)}
    finally:
        b.shutdown()
        roon.close()


def test_roon_taking_over_the_last_slot_is_no_end_of_the_queue(fake, listens, monkeypatch):
    """The node plays the queue's last slot and Roon takes HQPlayer: the
    status at the spot says stopped there, external — no end of the queue,
    the session is not archived (a play that takes HQPlayer back goes on in
    it)."""
    from playback import manager as manager_mod
    from playback import sessions
    closed = []
    monkeypatch.setattr(sessions, "close_active_session", lambda queue: closed.append(len(queue)))
    fake.raw_streams = "uri"
    roon = FakeRoon(fake.port)
    mgr = _album(3)
    monkeypatch.setattr(manager_mod, "manager", mgr)
    b = _attach(mgr)
    try:
        assert b.select(3)
        assert _wait(lambda: mgr._latest_status.get("state") == "playing"
                     and mgr._latest_status.get("track_index") == 3
                     and (mgr._latest_status.get("position") or 0) >= 3)
        _, _, ticks = _roon_takes_over(fake, mgr, roon)
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 3)
        st = mgr._latest_status
        assert ticks is not None and (st["state"], st["track_index"], st["source"]) == ("stopped", 3, "external")
        assert closed == []
    finally:
        b.shutdown()
        roon.close()


def test_a_tap_waiting_for_its_stream_ends_when_roon_takes_hqplayer(fake, monkeypatch):
    """A tap on a streamed slot whose bytes are away waits (loading); Roon
    then starts a track: its word is the newer one — the wait ends, the spot
    at the tapped track and its second, external — and the bytes' landing
    sends HQPlayer nothing: Roon plays on."""
    fake.raw_streams = "uri"
    mgr, b, prov, toks = _streams_on_hqplayer(fake, monkeypatch, dropped=3)
    roon = FakeRoon(fake.port)
    try:
        prov.go.clear()
        assert b.select(3, "stream-3")
        assert _wait(lambda: mgr._latest_status.get("state") == "loading")
        roon.play_track()
        assert _wait(lambda: mgr._latest_status.get("source") == "external", 10)
        st = dict(mgr._latest_status)
        n = _mark(fake, roon)
        prov.go.set()
        assert _wait(lambda: proxy_audio(toks[3]))
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 3)
        _observed("A-park", status=(st.get("state"), st.get("track_index"), st.get("source"), st.get("resume")),
                  node_commands=_node_commands(fake, n, roon))
        assert (st["state"], st["track_index"], st["resume"]) == ("stopped", 3, {"index": 3, "position": 0})
        assert _node_commands(fake, n, roon) == [] and fake.playlist[-1].endswith("/stream.raw")
    finally:
        prov.go.set()
        b.shutdown()
        roon.close()


def proxy_audio(token) -> bool:
    from streaming import service as streaming_service
    e = streaming_service.get_proxy()._peek(token)
    return e is not None and e.audio is not None


def test_a_queue_put_together_without_playing_while_roon_holds_hqplayer_sends_it_nothing(
        fake, listens, monkeypatch):
    """A new queue put together without playing it — a curation act, the
    assistant's playlist — while Roon holds HQPlayer: nothing is sent, Roon
    plays on; a Play then takes HQPlayer back with the new queue."""
    mgr, b, roon, spot = _roon_holds_the_spot(fake, monkeypatch)
    try:
        n = _mark(fake, roon)
        mgr.replace_queue([_item("E:/Music/C/01.flac", 21), _item("E:/Music/C/02.flac", 22)], play=False)
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 2)
        assert _node_commands(fake, n, roon) == [] and fake.state == PLAYING
        assert fake.playlist[0].endswith("/stream.raw") and mgr._latest_status.get("resume") is None
        assert b.play()
        _wait(lambda: fake.playlist == ["file:///E:/Music/C/01.flac", "file:///E:/Music/C/02.flac"]
              and fake.state == PLAYING, timeout=15)
        assert fake.playlist == ["file:///E:/Music/C/01.flac", "file:///E:/Music/C/02.flac"] and fake.track == 1
    finally:
        b.shutdown()
        roon.close()



def _drop_bytes(toks, slot):
    """The RAM budget drops slot `slot`'s stream bytes as well (slot 2's
    kept)."""
    from streaming import service as streaming_service
    p = streaming_service.get_proxy()
    p.enforce_budget({toks[2]}, [toks[slot]], 0)
    assert p._peek(toks[slot]).audio is None


def _roon_holds_streams(fake, monkeypatch):
    """HQPlayer plays slot 1 of [a file, three streams], the bytes of slots 3
    and 4 away; Roon takes HQPlayer. Returns (manager, backend, provider,
    tokens by slot, roon)."""
    fake.raw_streams = "uri"
    mgr, b, prov, toks = _streams_on_hqplayer(fake, monkeypatch, dropped=3)
    _drop_bytes(toks, 4)
    roon = FakeRoon(fake.port)
    _, _, ticks = _roon_takes_over(fake, mgr, roon)
    assert ticks is not None, "the node never said external"
    return mgr, b, prov, toks, roon


@pytest.mark.parametrize("how", ["one", "next", "tap"])
def test_a_take_back_that_waits_for_its_stream_under_roon_goes_and_so_does_the_word_after_it(fake, monkeypatch,
                                                                                              how):
    """Roon holds HQPlayer; the person taps a streamed slot whose bytes are
    away: the take-back waits ("loading"), asked while another controller
    held HQPlayer — the newer word, which Roon's ticks never end — and so
    is the person's next word while it waits (Next, or a tap on another such
    slot), however the status reads meanwhile: when its bytes land it takes
    HQPlayer back at its slot."""
    mgr, b, prov, toks, roon = _roon_holds_streams(fake, monkeypatch)
    try:
        prov.go.clear()
        assert b.select(3, "stream-3")
        assert _wait(lambda: mgr._latest_status.get("state") == "loading")
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 3)
        target = 3
        if how != "one":
            assert (b.next() if how == "next" else b.select(4, "stream-4"))
            target = 4
            k = len(fake.commands)
            _wait(lambda: _statuses(fake, k) >= 3)
        waiting = b._parked
        st = dict(mgr._latest_status)
        prov.go.set()
        _wait(lambda: fake.state == PLAYING and fake.track == target
              and not any(u.endswith("/stream.raw") for u in fake.playlist), timeout=15)
        _observed("A-take-back-waits", how=how, waiting=getattr(waiting, "under_another", None),
                  status=(st.get("state"), st.get("track_index")), hqplayer=(fake.state, fake.track))
        assert waiting is not None and waiting.under_another and (st["state"], st["track_index"]) == ("loading",
                                                                                                        target)
        assert fake.track == target and not any(u.endswith("/stream.raw") for u in fake.playlist)
    finally:
        prov.go.set()
        b.shutdown()
        roon.close()


def test_while_a_take_back_waits_under_roon_nothing_goes_into_roons_playlist_but_the_take_back(fake, listens,
                                                                                                monkeypatch):
    """While the person's take-back waits for its stream under Roon — the
    status "loading" — an append and a new queue put together without play
    are still the queue's alone: nothing goes into Roon's playlist, and Roon
    plays on until the take-back itself."""
    mgr, b, prov, toks, roon = _roon_holds_streams(fake, monkeypatch)
    try:
        prov.go.clear()
        assert b.select(3, "stream-3")
        assert _wait(lambda: mgr._latest_status.get("state") == "loading")
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 2)
        n = _mark(fake, roon)
        assert mgr.append([_item("E:/Music/B/09.flac", 9)], generation=mgr.queue.generation) == 1
        mgr.replace_queue([_item("E:/Music/C/01.flac", 21), _item("E:/Music/C/02.flac", 22)], play=False)
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 2)
        assert _node_commands(fake, n, roon) == [] and fake.state == PLAYING
        assert fake.playlist[-1].endswith("/stream.raw") and len(fake.playlist) == 1
    finally:
        prov.go.set()
        b.shutdown()
        roon.close()


def test_the_radios_clear_while_roon_holds_hqplayer_is_the_queues_alone(fake, listens, monkeypatch):
    """Radio started from the spot Roon's takeover left: the clear around the
    seed is the queue's alone — Roon's playlist is not ours to clear."""
    mgr, b, roon, spot = _roon_holds_the_spot(fake, monkeypatch)
    try:
        n = _mark(fake, roon)
        mgr.clear_for_radio(lambda: None)
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 2)
        assert _node_commands(fake, n, roon) == [] and fake.state == PLAYING
        assert [it.track_id for it in mgr.queue.snapshot()] == ["track-2"]
    finally:
        b.shutdown()
        roon.close()


def test_a_seek_at_the_spot_moves_it_once_roon_stopped_as_at_any_stopped_spot(fake, listens, monkeypatch):
    """Roon's takeover left the spot, then Roon stopped: HQPlayer rests on
    Roon's entry, the spot stands — and a Seek moves it as at any stopped
    spot, nothing sent; Play takes HQPlayer back at the new second."""
    fake.takes_seek = True
    mgr, b, roon, spot = _roon_holds_the_spot(fake, monkeypatch)
    try:
        roon.stop()
        k = len(fake.commands)
        _wait(lambda: fake.state == int(PlaybackState.STOPPED) and _statuses(fake, k) >= 3)
        assert mgr._latest_status.get("source") is None and mgr._latest_status.get("resume") == spot
        n = _mark(fake, roon)
        assert b.seek(150)
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 2)
        assert _node_commands(fake, n, roon) == [] and mgr._latest_status.get("resume") == {"index": 2,
                                                                                               "position": 150}
        m = len(fake.commands)
        assert b.play()
        _wait(lambda: fake.playlist == MIRROR and fake.state == PLAYING, timeout=15)
        assert [int(float(a["position"])) for c, a in fake.commands[m:] if c == "Seek"] == [150]
    finally:
        b.shutdown()
        roon.close()


def test_roon_starting_while_hqplayer_answers_no_playlist_read_is_no_listen_of_ours(fake, listens, monkeypatch):
    """Roon starts under a Status that names no URI while HQPlayer answers
    no PlaylistGet for a while: the moved take stays unread, and the entry
    nothing names gets no verdict meanwhile — never the layout's slot 1, a
    listen or a now-playing of it. Read at last, it is Roon's."""
    writes, lastfm = listens
    fake.raw_streams = "bare"
    roon = FakeRoon(fake.port)
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    try:
        fake.replies["PlaylistGet"] = '<PlaylistGet result="Error">busy</PlaylistGet>'
        n_lastfm = len(lastfm)
        roon.play_track()
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 4)
        during = dict(mgr._latest_status)
        del fake.replies["PlaylistGet"]
        _wait(lambda: mgr._latest_status.get("source") == "external", 10)
        rows = _rows(writes)
        assert during.get("track_index") != 1 and lastfm[n_lastfm:] == []
        assert rows == [] and tracker._play_session is not None and tracker._play_session.track_id == "track-2"
        assert mgr._latest_status.get("source") == "external"
    finally:
        b.shutdown()
        roon.close()


@pytest.mark.parametrize("opening", [0, 2])
def test_an_album_that_takes_hqplayer_back_from_roon_reaches_it_whole(fake, listens, monkeypatch, opening):
    """Roon holds HQPlayer and the person plays an album that rolls in — its
    first track a new queue that plays, the rest appended right after (a
    filler's way): the new queue's hand-over takes HQPlayer back, and the
    appends after it go to HQPlayer though no tick has read it since — the
    hold read before the node's own clear is over. The album plays on past
    its first track. `opening`: Status reads after the start that name no
    slot yet."""
    fake.raw_streams = "uri"
    roon = FakeRoon(fake.port)
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    try:
        _, _, ticks = _roon_takes_over(fake, mgr, roon)
        assert ticks is not None, "the node never said external"
        player_router = _routes_on(monkeypatch, mgr)
        real = fake._dispatch

        def dispatch(cmd, attrs):
            out = real(cmd, attrs)
            if cmd in ("SelectTrack", "Play") and opening:
                with fake._lock:
                    fake.opening = opening
            return out
        monkeypatch.setattr(fake, "_dispatch", dispatch)
        with fake._lock:
            fake.length = 8
        added, gen = mgr.replace_queue([_item("E:/Music/C/01.flac", 31)], play=True)
        assert added == 1
        for it in (_item("E:/Music/C/02.flac", 32), _item("E:/Music/C/03.flac", 33)):
            assert player_router._filler_append(it, gen)
        album = [f"file:///E:/Music/C/0{i}.flac" for i in (1, 2, 3)]
        assert fake.playlist == album, fake.playlist
        assert _wait(lambda: fake.state == PLAYING and fake.track == 2, 30), (fake.state, fake.track)
    finally:
        b.shutdown()
        roon.close()


def test_a_seek_while_a_start_from_the_spot_builds_is_not_swallowed(fake, listens, monkeypatch):
    """Stopped at a spot, Play starts it — HQPlayer silent while it builds,
    the last tick still "stopped". A Seek made during that build is no Seek
    at a stopped spot: the start reads the spot already. It goes to HQPlayer
    as a Seek of the start (said if HQPlayer refuses it), never moves a spot
    no start reads any more."""
    monkeypatch.setattr(hl.BuildWatch, "after", 0.3)
    mgr = _album(3)
    b = _attach(mgr)
    try:
        _stopped_at_spot(fake, mgr, monkeypatch)
        assert b._spot is not None and (b._spot[0].track_id, b._spot[1]) == ("track-2", 150)
        fake.takes_seek = True
        fake.build("SelectTrack", 3.0)
        n = len(fake.commands)
        res = {}
        t = threading.Thread(target=lambda: res.setdefault("play", b.play()), daemon=True)
        t.start()
        assert _wait(lambda: _link().build.state is not None, 5)
        try:
            res["seek"] = b.seek(30)
        except Exception as e:
            res["seek"] = type(e).__name__
        moved = (b._spot[0].track_id, b._spot[1]) if b._spot is not None else None
        t.join(15)
        assert _wait(lambda: fake.state == PLAYING and fake.track == 2, 10)
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 3)
        seeks = [int(float(a.get("position", 0))) for c, a in fake.commands[n:] if c == "Seek"]
        _observed("A-seek-while-starting", res=res, moved=moved, seeks=seeks)
        assert moved != ("track-2", 30)
        assert res["seek"] is not True or 30 in seeks
    finally:
        b.shutdown()

def _st(state, track, position, *, uri, serial, length=300.0):
    return TrackStatus(state=state, track_index=track, track_id="", position=position, length=length,
                       volume=-3.0, uri=uri, track_serial=serial)


@pytest.mark.parametrize("case, spot", [
    ("cut", (2, 30)),                  # the take moved once, no Stop of ours: Roon's start cut the listen
    ("moved_on_first", None),          # two takes past: HQPlayer moved on by itself before
    ("same_take", None),               # a verdict that turned on the same take: no one's start
    ("last_seconds", None),            # read in its last seconds: HQPlayer's own advance, near enough
    ("stopped_by_us", None),           # a Stop of ours after the reading ended the listen
    ("named_by_nothing", None),        # the slot from the layout alone: never a listen to cut
    ("hqp5_next_tick", (2, 30)),       # HQPlayer 5, no take: the tick before was the listen's
    ("hqp5_a_tick_between", None),     # HQPlayer 5, a tick between: no telling whose start
])
def test_the_takeover_rule_case_by_case(fake, case, spot):
    """_note_listen, tick by tick: the listen of slot 2 at 30 s read at t=1,
    then a tick that reads an entry not ours playing — the spot set or not
    as the rule has it."""
    q = CanonicalQueue()
    q.replace([_item(f"E:/Music/A/0{i}.flac", i) for i in (1, 2, 3)])
    b = hb.HqpBackend(emit=lambda *_: None, queue=q)
    own_uri = "" if case == "named_by_nothing" else "file:///E:/Music/A/02.flac"
    serial = None if case.startswith("hqp5") else 5
    position = 297.0 if case == "last_seconds" else 30.0
    b._note_listen(_st(PlaybackState.PLAYING, 2, position, uri=own_uri, serial=serial),
                   (2, q.item_at(2), False), position, 1.0, 0.0)
    if case == "stopped_by_us":
        b._stopped_at = 1.5
    later = {"moved_on_first": 7, "same_take": 5}.get(case, 6) if serial is not None else None
    prev_at = 1.7 if case == "hqp5_a_tick_between" else 1.0
    b._note_listen(_st(PlaybackState.PLAYING, 1, 2.0, uri="http://127.0.0.1:30000/1/stream.raw", serial=later),
                   (0, None, True), 2.0, 2.0, prev_at)
    held = b._spot_at()
    assert held == spot

@pytest.mark.parametrize("case, spot", [
    ("edited_after_the_take_moved", (2, 30)),    # Roon's Play refused again and again: the take moved, nothing plays
    ("edited_before_the_take_moved", None),      # its Clear and Add read before its Play: no start yet — held, waiting
    ("edited_after_our_stop", None),             # a Stop of ours after the reading ended the listen
    ("edited_in_the_last_seconds", None),        # read in its last seconds: HQPlayer's own advance, near enough
    ("edited_hqp5_no_take", (2, 30)),            # HQPlayer 5 names no take: the edit is the verdict
])
def test_f7_an_edit_under_the_listen_judges_it_whatever_hqplayer_plays_of_it(fake, case, spot):
    """F7 (ROON-COEX §10.2, A8): another controller took HQPlayer from the
    node's listen and HQPlayer refused its start — stopped on its entry, the
    take moving at every Play it sent again. The tick whose playlist the
    canary read as not the node's (`edited`) judges the listen as the first
    tick of the other controller's entry does: the spot at the listen's slot
    and second; and while nothing of ours plays the tracker holds that
    listen (_listen_held)."""
    q = CanonicalQueue()
    q.replace([_item(f"E:/Music/A/0{i}.flac", i) for i in (1, 2, 3)])
    b = hb.HqpBackend(emit=lambda *_: None, queue=q)
    serial = None if case == "edited_hqp5_no_take" else 5
    position = 297.0 if case == "edited_in_the_last_seconds" else 30.0
    b._note_listen(_st(PlaybackState.PLAYING, 2, position, uri="file:///E:/Music/A/02.flac", serial=serial),
                   (2, q.item_at(2), False), position, 1.0, 0.0)
    if case == "edited_after_our_stop":
        b._stopped_at = 1.5
    later = None if serial is None else (5 if case == "edited_before_the_take_moved" else 47)
    stopped = _st(PlaybackState.STOPPED, 1, 0.0, uri="http://127.0.0.1:30000/1/stream.raw", serial=later)
    b._note_listen(stopped, (0, None, False), 0.0, 2.0, 1.0, edited=True)
    assert b._spot_at() == spot
    held = b._listen_held(stopped, b._playing(stopped) or (0, None, False))
    assert held is (spot is not None or case == "edited_before_the_take_moved")
    # HQPlayer playing an entry of ours again ends the hold
    b._note_listen(_st(PlaybackState.PLAYING, 3, 1.0, uri="file:///E:/Music/A/03.flac", serial=48),
                   (3, q.item_at(3), False), 1.0, 3.0, 2.0)
    playing = _st(PlaybackState.PLAYING, 3, 2.0, uri="file:///E:/Music/A/03.flac", serial=48)
    assert not b._listen_held(playing, (3, q.item_at(3), False))


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


def test_b1_another_controller_takes_the_renderer_while_the_node_plays(tmp_path, proxy, renderers, nodes, quality,
                                                                         taken):
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
    assert _sent(r, mark) == []
    # The event and a poll already under way may still name the slot once
    assert len(ours_playing) <= 1, f"our slot said playing {len(ours_playing)} times after the takeover"
    assert all(s.queue_index in (0, 1) for s in after)


def test_b2_a_play_in_the_node_after_another_controller_took_the_renderer(tmp_path, proxy, renderers, nodes,
                                                                           quality, taken):
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
    assert ok and r.state == "PLAYING" and "raat://" not in r.uri
    assert n.last.state == "playing" and n.last.queue_index == 1


def test_b1_the_other_controllers_track_running_past_ours_moves_nothing(tmp_path, proxy, renderers, nodes, quality,
                                                                         taken):
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


def test_b1_the_listen_counts_its_own_seconds_and_the_other_controllers_track_is_none_of_ours(
        tmp_path, proxy, renderers, manager, heard, quality, taken):
    """B1 through the manager and the play tracker: the node's listen counts
    its own seconds alone; what the other controller plays on is no listen
    of ours — its seconds never run our slot's clock past the scrobble's
    floor (F4: the node scrobbled a track that did not sound). A URI of the
    other controller's is a takeover, a pause for the listen (F6): held,
    nothing written; the transport stopped on our URI by no word of ours ends
    it, written once under half its track."""
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
    if taken == "playing":
        assert listened == []
    else:
        assert [t for t, _ in listened] == ["00000000-0000-0000-0000-000000000001"] and listened[0][1] < 50
    assert _sent(r, mark) == []


def test_b_the_spot_another_controller_left_on_the_renderer_holds_until_play(tmp_path, proxy, renderers, nodes,
                                                                              quality):
    """B2 at its second: the other controller's takeover cut the node's
    listen of slot 1 — the status names slot 1 stopped at the node's last own
    second, external, and holds it while the other controller plays on, the
    node sending nothing; then Play loads our URI and starts it there."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    at = n.last.position
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("source") == "external", timeout=5, what="external")
    spot = n.last.extra.get("resume")
    mark, seen = len(r.calls), len(n.statuses)
    time.sleep(2.5)
    held = {(s.state, s.queue_index, json.dumps(s.extra.get("resume"))) for s in n.statuses[seen:]}
    _observed("B-spot", at=round(at, 1), spot=spot, held=sorted(held), node_sent=_sent(r, mark))
    assert spot is not None and spot["index"] == 1 and at - 1.5 <= spot["position"] <= at + 1.5
    assert held <= {("stopped", 1, json.dumps(spot))} and _sent(r, mark) == []
    assert n.backend.play()
    wait_for(lambda: r.state == "PLAYING" and "raat://" not in r.uri and n.last.state == "playing"
             and n.last.queue_index == 1 and n.last.position >= spot["position"], timeout=10, what="slot 1 at the spot")
    seeks = [_seconds(a["Target"]) for a in r.called("Seek")]
    assert seeks and abs(seeks[-1] - spot["position"]) <= 1.0


def test_b_another_controller_taking_a_stopped_renderer_leaves_no_spot(tmp_path, proxy, renderers, nodes, quality):
    """The node's own Stop ended its listen, then another controller took the
    renderer: no listen was cut — external at slot 0, no spot; Play loads our
    slot from its start."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    assert n.backend.stop()
    wait_for(lambda: n.last.state == "stopped" and r.state == "STOPPED", timeout=5, what="stopped")
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("source") == "external", timeout=5, what="external")
    _observed("B-idle", status=(n.last.state, n.last.queue_index, n.last.extra.get("resume")))
    assert (n.last.queue_index, n.last.extra.get("resume")) == (0, None)
    mark = len(r.calls)
    assert n.backend.play()
    wait_for(lambda: r.state == "PLAYING" and "raat://" not in r.uri and n.last.state == "playing"
             and n.last.queue_index == 1, timeout=10, what="slot 1 playing again")
    assert "Seek" not in _sent(r, mark)


def test_b_a_play_waiting_for_its_stream_ends_when_another_controller_takes_the_renderer(tmp_path, proxy,
                                                                                         renderers, nodes,
                                                                                         quality):
    """A tap waits for its slot's stream (loading, the renderer still on the
    track before); another controller takes the renderer meanwhile: its word
    is newer — the wait ends, its track and second the spot, external — and
    the stream's landing sends the renderer nothing."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    waiting = n.queue.item_at(2)
    waiting.play = {"kind": "pending"}
    assert n.backend.select(2)
    wait_for(lambda: n.last.state == "loading" and n.last.queue_index == 2, what="waiting on slot 2")
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("source") == "external", timeout=5, what="external")
    mark = len(r.calls)
    st = n.last
    waiting.play = streamed(proxy, 2, cover_url="").source
    n.backend.slot_ready(2)
    time.sleep(1.0)
    _observed("B-park", status=(st.state, st.queue_index, st.extra.get("resume")), node_sent=_sent(r, mark))
    assert (st.state, st.queue_index, st.extra.get("resume")) == ("stopped", 2, {"index": 2, "position": 0.0})
    assert _sent(r, mark) == [] and "raat://" in r.uri


def test_b_a_queue_edit_while_another_controller_holds_the_renderer_moves_its_spot_and_arms_nothing(
        tmp_path, proxy, renderers, nodes, quality):
    """The spot another controller's takeover left follows its track through
    a queue edit — a slot inserted before it moves it one on — and goes with
    it; the renderer is sent nothing, no next armed into what it plays."""
    r = renderers()
    q = CanonicalQueue()
    q.replace([track(tmp_path, i, seconds=60.0) for i in (1, 2, 3)])
    n = nodes(q, r)
    assert n.backend.select(2)
    wait_for(lambda: n.last.state == "playing" and n.last.queue_index == 2 and n.last.position >= 3,
             timeout=15, what="slot 2 playing")
    r.taken_by_another("playing")
    wait_for(lambda: (n.last.extra.get("resume") or {}).get("index") == 2, timeout=5, what="the spot")
    mark = len(r.calls)
    q.insert_after(0, [track(tmp_path, 4, seconds=60.0)])
    n.backend.queue_changed("insert")
    wait_for(lambda: (n.last.extra.get("resume") or {}).get("index") == 3, timeout=5, what="the spot moved")
    q.remove(3)
    n.backend.queue_changed("remove")
    wait_for(lambda: n.last.extra.get("resume") is None and n.last.queue_index == 0, timeout=5,
             what="the spot gone with its track")
    _observed("B-edit", node_sent=_sent(r, mark), status=(n.last.state, n.last.queue_index))
    assert _sent(r, mark) == [] and n.last.extra.get("source") == "external"


def test_b_a_replace_without_play_while_another_controller_holds_the_renderer_sends_it_nothing(
        tmp_path, proxy, renderers, nodes, quality):
    """A new queue put together without playing it — a curation act — while
    another controller holds the renderer: nothing is sent to it, the spot
    goes with the queue it was in, and Play then loads the new queue's
    head."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("resume") is not None, timeout=5, what="the spot")
    mark = len(r.calls)
    n.queue.replace([track(tmp_path, 5, seconds=60.0), track(tmp_path, 6, seconds=60.0)])
    n.backend.queue_changed("replace", play=False)
    wait_for(lambda: n.last.extra.get("resume") is None and n.last.queue_index == 0, timeout=5,
             what="the spot gone")
    assert _sent(r, mark) == [] and "raat://" in r.uri
    assert n.backend.play()
    wait_for(lambda: r.state == "PLAYING" and "raat://" not in r.uri and n.last.state == "playing"
             and n.last.queue_index == 1, timeout=10, what="the new queue's head")
    assert n.last.extra.get("source") is None


def test_b_a_seek_or_a_stop_while_another_controller_holds_the_renderer(tmp_path, proxy, renderers, nodes,
                                                                        quality):
    """While another controller holds the renderer, a Seek moves the spot
    its takeover left, and a Stop or a Pause sends it nothing (L11): its
    playback goes on, the spot stays, as on HQPlayer. With no spot (the node
    had stopped) a Seek is refused in words: nothing of ours to seek in."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("resume") is not None, timeout=5, what="the spot")
    mark = len(r.calls)
    assert n.backend.seek(20)
    assert n.last.extra.get("resume") == {"index": 1, "position": 20.0} and _sent(r, mark) == []
    assert n.backend.stop() and n.backend.pause()
    time.sleep(1.0)
    assert r.state == "PLAYING" and "raat://" in r.uri, "the other controller's playback goes on"
    assert _sent(r, mark) == [] and n.last.extra.get("resume") == {"index": 1, "position": 20.0}
    # Taken from the node's own Stop: no spot, and a Seek is refused
    r2, n2 = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    assert n2.backend.stop()
    wait_for(lambda: n2.last.state == "stopped" and r2.state == "STOPPED", timeout=5, what="stopped")
    r2.taken_by_another("playing")
    wait_for(lambda: n2.last.extra.get("source") == "external", timeout=5, what="external")
    mark2 = len(r2.calls)
    assert not n2.backend.seek(20)
    assert "another app is playing through" in n2.backend.refusal() and _sent(r2, mark2) == []
    assert n2.last.extra.get("resume") is None


@pytest.mark.parametrize("held", ["another_app", "stale", "ours_before"])
def test_b_a_play_that_waits_for_its_stream_goes_whatever_the_renderer_held_before_it(tmp_path, proxy, renderers,
                                                                                       nodes, quality, held):
    """On a fresh attachment the person presses Play and the first slot waits
    for its stream. What the renderer held before that word — another app
    playing on, an idle device's stale URI, one of the node's own from an
    earlier attachment — is no takeover, whatever event comes meanwhile (the
    node's own disarm of the NextURI it held among them): the wait stands,
    and the stream's landing loads and plays the slot."""
    r = renderers()
    if held == "ours_before":
        q0 = CanonicalQueue()
        q0.replace([track(tmp_path, 9, seconds=60.0)])
        n0 = nodes(q0, r)
        assert n0.backend.select(1)
        wait_for(lambda: r.state == "PLAYING", timeout=15, what="an earlier attachment playing")
        n0.backend.shutdown()
    elif held == "another_app":
        r.taken_by_another("playing")
    else:
        r.uri, r.meta, r.state = "http://192.0.2.7:8080/old.flac", "", "STOPPED"
    r.next_uri = "http://192.0.2.7:8080/next.flac"
    q = CanonicalQueue()
    waiting = track(tmp_path, 1, seconds=60.0)
    waiting.play = {"kind": "pending"}
    q.replace([waiting, track(tmp_path, 2, seconds=60.0)])
    n = nodes(q, r)
    assert n.backend.play()
    wait_for(lambda: n.last.state == "loading" and n.last.queue_index == 1, what="waiting on slot 1")
    time.sleep(1.5)                     # the disarm's event, and what else comes
    st = n.last
    assert n.backend._parked is not None and (st.state, st.queue_index, st.extra.get("source")) == ("loading", 1, None)
    waiting.play = streamed(proxy, 1, cover_url="").source
    n.backend.slot_ready(1)
    wait_for(lambda: r.state == "PLAYING" and r.uri.endswith("/preview/stream1") and n.last.state == "playing",
             timeout=10, what="slot 1 loaded and playing")


def test_b_a_renderer_taken_during_a_wait_without_an_event_is_read_when_the_stream_lands(tmp_path, proxy,
                                                                                        renderers, nodes,
                                                                                        quality):
    """A renderer whose events do not reach the node is taken by another
    controller while a play waits for its stream: the landing reads it once
    before loading — another URI than the wait began on, none of ours — and
    sends nothing; the wait's track and second are the spot."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    waiting = n.queue.item_at(2)
    waiting.play = {"kind": "pending"}
    assert n.backend.select(2)
    wait_for(lambda: n.last.state == "loading" and n.last.queue_index == 2, what="waiting on slot 2")
    r.notifying = False
    r.taken_by_another("playing")
    mark = len(r.calls)
    waiting.play = streamed(proxy, 2, cover_url="").source
    n.backend.slot_ready(2)
    wait_for(lambda: n.last.extra.get("source") == "external", timeout=10, what="external at the landing")
    time.sleep(0.5)
    assert _sent(r, mark) == [] and "raat://" in r.uri
    assert (n.last.state, n.last.queue_index, n.last.extra.get("resume")) == ("stopped", 2,
                                                                            {"index": 2, "position": 0.0})


def test_b_a_queue_put_together_while_a_take_back_waits_for_its_stream_ends_the_wait(tmp_path, proxy, renderers,
                                                                                     nodes, quality):
    """Another controller holds the renderer; the person taps a slot whose
    stream is on its way — the take-back waits — and a queue is then put
    together without playing it: the wait goes with the queue it was in, the
    status external again, never "loading" for good."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("source") == "external", timeout=5, what="external")
    waiting = n.queue.item_at(2)
    waiting.play = {"kind": "pending"}
    assert n.backend.select(2)
    wait_for(lambda: n.last.state == "loading" and n.last.queue_index == 2, what="the take-back waiting")
    n.queue.replace([track(tmp_path, 5, seconds=60.0)])
    n.backend.queue_changed("replace", play=False)
    wait_for(lambda: n.last.state != "loading" and n.last.extra.get("source") == "external", timeout=5,
             what="external again")
    assert n.backend._parked is None and n.last.queue_index == 0


def test_b_a_renderer_lost_or_refusing_our_load_while_another_controller_holds_it(tmp_path, proxy, renderers,
                                                                                  nodes, quality):
    """While another controller holds the renderer: lost — it stops
    answering — it keeps the spot the takeover left, and makes none from the
    other controller's state; a load of ours it refuses leaves it the other
    controller's, the status external with the refusal said."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    r.taken_by_another("playing")
    wait_for(lambda: (n.last.extra.get("resume") or {}).get("index") == 1, timeout=5, what="the spot")
    spot = n.last.extra["resume"]
    r.faults["SetAVTransportURI"] = [701]
    assert n.backend.select(2)
    wait_for(lambda: n.backend._error is not None and n.last.state != "loading", timeout=10, what="the refusal")
    assert n.last.extra.get("source") == "external" and n.last.extra.get("resume") == spot
    r.stop_answering()
    assert not n.backend.stop()
    wait_for(lambda: n.backend._gone, timeout=10, what="lost")
    assert n.last.state == "stopped" and n.last.extra.get("resume") == spot
    r.answer_again()
    r2, n2 = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    assert n2.backend.stop()
    wait_for(lambda: n2.last.state == "stopped" and r2.state == "STOPPED", timeout=5, what="stopped")
    r2.taken_by_another("playing")
    wait_for(lambda: n2.last.extra.get("source") == "external", timeout=5, what="external")
    r2.stop_answering()
    assert not n2.backend.pause()
    wait_for(lambda: n2.backend._gone, timeout=10, what="lost")
    assert n2.last.state == "stopped" and n2.last.extra.get("resume") is None
    r2.answer_again()


def test_b_a_seek_during_the_landings_read_moves_the_start_and_the_slot_still_goes(tmp_path, proxy, renderers,
                                                                                   nodes, quality, monkeypatch):
    """The stream of a waiting slot lands, and the landing asks the renderer
    its URI first — a renderer slow to answer. A Seek made meanwhile moves the
    wait's second: the landing goes on with the wait as it stands then, the
    slot loaded and started there — never "loading" for good."""
    real = FakeRenderer._control

    async def slow(self, request):
        if getattr(self, "gpi_delay", 0) and "GetPositionInfo" in request.headers.get("SOAPACTION", ""):
            await asyncio.sleep(self.gpi_delay)
        return await real(self, request)
    monkeypatch.setattr(FakeRenderer, "_control", slow)
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    waiting = n.queue.item_at(2)
    waiting.play = {"kind": "pending"}
    assert n.backend.select(2)
    wait_for(lambda: n.last.state == "loading" and n.last.queue_index == 2, what="waiting on slot 2")
    time.sleep(1.0)
    r.gpi_delay = 2.0
    waiting.play = streamed(proxy, 2, cover_url="").source
    n.backend.slot_ready(2)
    time.sleep(0.5)
    assert n.backend.seek(20)
    wait_for(lambda: r.state == "PLAYING" and r.uri.endswith("/preview/stream2") and r.called("Seek"),
             timeout=15, what="slot 2 loaded and sought")
    r.gpi_delay = 0
    assert r.called("Seek")[-1]["Target"] == "0:00:20"


def test_b_a_take_back_that_waits_survives_the_other_apps_own_next_track(tmp_path, proxy, renderers, nodes,
                                                                         quality):
    """Another app plays on the renderer when the person picks it and presses
    Play — a fresh attachment — and the first slot waits for its stream:
    the person's take-back, the newer word. The other app moving on to its
    own next track meanwhile does not end it; the landing loads and plays
    the slot."""
    r = renderers()
    r.taken_by_another("playing")
    q = CanonicalQueue()
    waiting = track(tmp_path, 1, seconds=60.0)
    waiting.play = {"kind": "pending"}
    q.replace([waiting, track(tmp_path, 2, seconds=60.0)])
    n = nodes(q, r)
    assert n.backend.play()
    wait_for(lambda: n.last.state == "loading" and n.last.queue_index == 1, what="waiting on slot 1")
    time.sleep(1.0)
    r.uri = "raat://another-controller/stream-2"
    r._base, r._t0 = 0.0, time.monotonic()
    r._run(r._changed())
    time.sleep(1.5)
    assert n.backend._parked is not None and n.last.state == "loading"
    waiting.play = streamed(proxy, 1, cover_url="").source
    n.backend.slot_ready(1)
    wait_for(lambda: r.state == "PLAYING" and r.uri.endswith("/preview/stream1") and n.last.state == "playing",
             timeout=10, what="slot 1 loaded and playing")


# -- F6 and F7: a takeover is a pause for the listen, and the spot is the node's ----------------------------

def _slot_track(b, slot: int) -> str:
    return b._queue.item_at(slot).track_id


def test_f6_a_takeover_taken_back_at_its_spot_is_one_listen(fake, roon, listens, monkeypatch):
    """F6 (ROON-COEX §10.2, §10.4; the design session's answer of 2026-11-02
    night: a takeover is a pause for the tracker). Roon's takeover and its
    stop hold the node's listen of slot 2 — nothing written, none of Roon's
    seconds counted; the node's Play takes HQPlayer back at the spot and the
    same listen goes on: one history row, its seconds the node's own, one
    now-playing."""
    writes, lastfm = listens
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    try:
        at, _, ticks = _roon_takes_over(fake, mgr, roon)
        assert ticks is not None, "the node never said external"
        spot = mgr._latest_status.get("resume")
        roon.stop()
        mark = len(fake.commands)
        _wait(lambda: fake.state == int(PlaybackState.STOPPED) and _statuses(fake, mark) >= 3, timeout=10)
        assert _rows(writes) == [] and tracker._play_session is not None, "the takeover is a pause: held"
        _takes_back(fake, mgr, b, roon, "F6", interrupted_at=(2, at))
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 4 and (mgr._latest_status.get("position") or 0) >= spot["position"] + 3,
              timeout=10)
        assert b.stop()
        _wait(lambda: _rows(writes), timeout=10)
        rows = _rows(writes)
        _observed("F6", mode=fake.raw_streams, spot=spot, lastfm=lastfm,
                  rows=[(p["tid"], p["dur"], p["comp"], p["skip"]) for p in rows])
        assert [p["tid"] for p in rows] == [_slot_track(b, 2)]
        assert rows[0]["dur"] >= spot["position"] + 2
        assert lastfm.count("update_now_playing") == 1
    finally:
        b.shutdown()


def test_f6_a_takeover_then_another_track_closes_the_held_listen_at_its_own_seconds(fake, listens, monkeypatch):
    """F6: the person starts another track instead of taking the cut listen
    back — the held listen closes at its own seconds (the spot's), and the
    new track opens its own."""
    writes, lastfm = listens
    mgr, b, roon, spot = _roon_holds_the_spot(fake, monkeypatch)
    try:
        assert _rows(writes) == [] and tracker._play_session is not None
        assert b.select(3, "track-3")
        _wait(lambda: fake.state == PLAYING and fake.track == 3 and _rows(writes), timeout=15)
        rows = _rows(writes)
        _observed("F6-another", spot=spot, lastfm=lastfm, rows=[(p["tid"], p["dur"]) for p in rows])
        assert [p["tid"] for p in rows] == [_slot_track(b, 2)]
        assert abs(rows[0]["dur"] - spot["position"]) <= 1.5
        assert lastfm.count("update_now_playing") == 2
    finally:
        b.shutdown()
        roon.close()


def test_f7_a_takeover_whose_start_hqplayer_refuses_leaves_the_spot_and_holds_the_listen(fake, listens,
                                                                                          monkeypatch):
    """F7 (ROON-COEX §10.2, A8): Roon takes HQPlayer from the node's listen
    with a track its output family refuses — HQPlayer stops at once, Roon
    sends Play again and again, nothing of its plays. The spot is the
    node's all the same: Now Playing names slot 2 stopped at its second, the
    node sends nothing and writes no listen; then its Play takes HQPlayer
    back there and the listen goes on as one."""
    writes, lastfm = listens
    fake.raw_streams = "uri"
    fake.refuse_play.add("stream.raw")
    roon = FakeRoon(fake.port)
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    try:
        at = mgr._latest_status.get("position")
        n = _mark(fake, roon)
        roon.play_track()
        for _ in range(6):
            roon.play_again()
            time.sleep(0.15)
        _wait(lambda: mgr._latest_status.get("resume") is not None, timeout=10)
        st = mgr._latest_status
        _observed("F7", at=at, node_commands=_node_commands(fake, n, roon), rows=len(_rows(writes)),
                  status={k_: st.get(k_) for k_ in ("state", "track_index", "source", "resume")})
        assert (st["state"], st["track_index"], st["resume"]["index"]) == ("stopped", 2, 2)
        assert abs(st["resume"]["position"] - at) <= 2
        assert _node_commands(fake, n, roon) == []
        assert _rows(writes) == [] and tracker._play_session is not None
        fake.refuse_play.clear()
        _takes_back(fake, mgr, b, roon, "F7", interrupted_at=(2, at))
        assert b.stop()
        _wait(lambda: _rows(writes), timeout=10)
        assert [p["tid"] for p in _rows(writes)] == [_slot_track(b, 2)]
        assert lastfm.count("update_now_playing") == 1
    finally:
        b.shutdown()
        roon.close()


def test_f6_on_a_renderer_a_takeover_taken_back_is_one_listen(tmp_path, proxy, renderers, manager, heard,
                                                              quality):
    """F6 on a renderer: another controller's URI takes it from the node's
    listen of slot 1 (D5) — held, nothing written while it plays on; the
    node's Play loads our URI at the spot and the same listen goes on: one
    row, one now-playing."""
    listened, lastfm = heard
    r = renderers()
    _on_renderer(manager, r, [track(tmp_path, 1, seconds=60.0), track(tmp_path, 2, seconds=60.0)])
    _playing_at(manager, r, 1)
    assert manager.backend().seek(25)
    _playing_at(manager, r, 26)
    r.taken_by_another("playing")
    wait_for(lambda: manager.latest_status.get("source") == "external", timeout=5, what="external")
    time.sleep(2.5)
    assert listened == [], "held: a takeover is a pause"
    assert manager.backend().play()
    wait_for(lambda: r.state == "PLAYING" and "raat://" not in r.uri
             and manager.latest_status.get("state") == "playing", timeout=10, what="taken back")
    _playing_at(manager, r, 30)
    assert manager.backend().stop()
    wait_for(lambda: listened, timeout=10, what="the listen written")
    _observed("F6-renderer", listened=listened, lastfm=lastfm)
    assert [t for t, _ in listened] == ["00000000-0000-0000-0000-000000000001"]
    assert lastfm.count(("update_now_playing", "Track 1")) == 1


@pytest.mark.xfail(strict=True, reason="ROON-COEX §6.1 P1, for the design session: a reconnect inside Roon's cycle "
                                       "reads its playlist as a restart and takes the queue afresh over it")
def test_p1_a_reconnect_inside_roons_cycle_sends_its_playlist_nothing(fake, listens, monkeypatch):
    """P1 (the heavy A1's question, ROON-COEX §6.1): Roon's start stands
    between its add and its Play while HQPlayer answers nothing past the
    node's client timeout (a heavy engine's Stop). The node reconnects and
    reads a stopped HQPlayer holding Roon's stream: that is another
    controller's playlist, not a restart — the node sends it nothing, and
    Roon's Play plays Roon's stream (option (a))."""
    fake.raw_streams = "uri"
    roon = FakeRoon(fake.port)
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    try:
        n = _mark(fake, roon)
        uri = roon.start_before_its_play()
        fake.silence(6.0)
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 4, timeout=30)
        roon.play_again()
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 3, timeout=15)
        _observed("P1", node_commands=_node_commands(fake, n, roon), playlist=fake.playlist[:3],
                  hqplayer=(fake.state, fake.track))
        assert _node_commands(fake, n, roon) == []
        assert fake.playlist == [uri] and fake.state == PLAYING
    finally:
        b.shutdown()
        roon.close()


@pytest.mark.parametrize("press, slot", [("next", 2), ("previous", 1)])
def test_l11_on_a_renderer_next_and_previous_at_the_takeovers_spot_load_their_slot(tmp_path, proxy, renderers, nodes,
                                                                                   quality, press, slot):
    """L11 on a renderer: Next and Previous at the spot another controller's
    takeover left load their slot and play it — the renderer taken back."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("resume") is not None, timeout=5, what="the spot")
    assert getattr(n.backend, press)()
    wait_for(lambda: r.state == "PLAYING" and "raat://" not in r.uri and n.last.state == "playing"
             and n.last.queue_index == slot, timeout=10, what=f"slot {slot} playing")
    assert n.last.extra.get("source") != "external"


def test_l11_on_a_renderer_next_or_previous_with_no_spot_is_refused_in_words(tmp_path, proxy, renderers, nodes,
                                                                            quality):
    """L11, as on HQPlayer: another controller took a renderer the node had
    stopped — no listen cut, no slot of ours to count from — so Next and
    Previous are refused in words and nothing is sent; Play takes it back."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    assert n.backend.stop()
    wait_for(lambda: n.last.state == "stopped" and r.state == "STOPPED", timeout=5, what="stopped")
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("source") == "external", timeout=5, what="external")
    mark = len(r.calls)
    assert not n.backend.next() and not n.backend.previous()
    assert "another app is playing through" in n.backend.refusal() and _sent(r, mark) == []
