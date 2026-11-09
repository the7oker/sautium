"""Roon and the node on one HQPlayer, and on one renderer: the scenarios of
the matrix that fakes can show, asserted as the design has them.

Roon is HQPlayer's other controller on a control connection of its own
(FakeRoon), as phase 1 recorded it (ROON-COEX §10.2, §10.4): a person's start
in Roon is Stop, PlaylistClear, a PlaylistAdd of a new raw stream from its
Core and Play, with no metadata; Next, a seek and a resume are a whole new
cycle with a new stream; across a format change a natural transition is the
same without the Stop; a pause is Pause, then Stop 5 s later, its zone still
"paused"; a stop is Stop. How Desktop's Status names that entry is kept
both ways (FakeHqp.raw_streams): the URI, as 6.1.1 and 6.2.0 name it, and a
bare answer. On a renderer Roon goes past UPnP (RAAT): the KANN said our URI
paused at its second (B1, FakeRenderer.taken_by_another "paused"); a
renderer another UPnP controller loads names its URI ("playing"), and one
that stops on ours ("stopped") is kept as a guess.

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
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_hqp_backend import (_QUESTIONS, _GatedStream, _album, _attach, _item, _link, _polls,  # noqa: E402,F401
                              _queue_stays_in_memory, _routes_on, _stopped_at_spot, _streams_on_hqplayer, _wait,
                              fake, listens, output)
from test_dlna_backend import (FakeRenderer, _gena_port, _on_renderer, _playing_at, _seconds,  # noqa: E402,F401
                               heard, manager, nodes, proxy, quality, renderers, stored, streamed, track, wait_for)

from hqplayer_client import PlaybackState, TrackStatus  # noqa: E402
from playback import hqp_backend as hb  # noqa: E402
from playback import hqp_link as hl  # noqa: E402
from playback import hqp_diagnostics as diag  # noqa: E402
from playback import tracker  # noqa: E402
from playback.queue import CanonicalQueue, QueueItem  # noqa: E402


PLAYING = int(PlaybackState.PLAYING)


def _observed(scenario: str, **what) -> None:
    print("OBSERVED", scenario, json.dumps(what, default=str, sort_keys=True))


# Roon's Stop after its Pause, recorded live (ROON-COEX §10.2 A3: 16:40:40 → 16:40:45)
PAUSE_STOP_S = 5.0


class FakeRoon:
    """Roon driving HQPlayer on a control connection of its own: per track a
    new raw stream from its Core, handed as phase 1 recorded it. Its zone's
    volume is Fixed (the trial sheet): no SetVolume."""

    def __init__(self, port: int):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.n = 0
        self.sent: list[str] = []
        self._lock = threading.Lock()
        self._stop_due: threading.Timer | None = None

    def _send(self, *commands: str) -> None:
        with self._lock:
            for c in commands:
                self.sock.sendall(c.encode())
                answer = b""
                while not answer.endswith(b"\n"):
                    chunk = self.sock.recv(65536)
                    if not chunk:
                        raise ConnectionError("HQPlayer closed Roon's control connection")
                    answer += chunk
                self.sent.append(re.match(r"<(\w+)", c).group(1))

    def _cancel_stop(self) -> None:
        if self._stop_due is not None:
            self._stop_due.cancel()
            self._stop_due = None

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

    def pause(self, stop_after: float = PAUSE_STOP_S) -> None:
        """Roon's pause: Pause, then Stop `stop_after` seconds later, its zone
        still "paused" (recorded: 5 s)."""
        self._send("<Pause/>")
        self._stop_due = threading.Timer(stop_after, self._send, args=("<Stop/>",))
        self._stop_due.daemon = True
        self._stop_due.start()

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

    def resume(self) -> str:
        """Roon's resume: a whole new cycle with a new stream, from the second
        it paused at (recorded); its pause's Stop goes no more."""
        self._cancel_stop()
        return self.play_track()

    def stop(self) -> None:
        """Roon's stop: Stop (recorded)."""
        self._cancel_stop()
        self._send("<Stop/>")

    def close(self) -> None:
        self._cancel_stop()
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
    """A3. Roon pauses its track — Pause, then Stop 5 s later, as recorded:
    the node stays silent, Now Playing at the spot Roon's takeover left,
    external while HQPlayer pauses Roon's entry, and once Roon's Stop came no
    `source` (a stopped HQPlayer names no entry); a Play in the node takes
    HQPlayer back there as in A2 — never resumes Roon's stream."""
    writes, lastfm = listens
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    try:
        at, _, ticks = _roon_takes_over(fake, mgr, roon)
        assert ticks is not None, "the node never said external"
        roon.pause()
        mark = len(fake.commands)
        _wait(lambda: fake.state == int(PlaybackState.PAUSED) and _statuses(fake, mark) >= 3, timeout=10)
        paused = dict(mgr._latest_status)
        _wait(lambda: fake.state == int(PlaybackState.STOPPED), timeout=10)
        mark = len(fake.commands)
        _wait(lambda: _statuses(fake, mark) >= 3, timeout=10)
        st = mgr._latest_status
        _observed("A3-paused", mode=fake.raw_streams, rows=len(_rows(writes)), lastfm=lastfm,
                  paused={k_: paused.get(k_) for k_ in ("state", "track_index", "source", "resume")},
                  stopped={k_: st.get(k_) for k_ in ("state", "track_index", "source", "resume")})
        assert (paused["state"], paused["track_index"], paused["source"], paused["resume"]["index"]) == (
            "stopped", 2, "external", 2)
        assert (st["state"], st["track_index"], st.get("source"), st["resume"]["index"]) == ("stopped", 2, None, 2)
        assert _rows(writes) == [] and tracker._play_session is not None
        _takes_back(fake, mgr, b, roon, "A3", roon_state="paused, then stopped", interrupted_at=(2, at))
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
    paused, resumed (a new cycle with a new stream, before its pause's Stop)
    — Now Playing naming slot 2 at that second the whole time, the node
    sending nothing; then a Play in the node takes HQPlayer back there."""
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
                   (2, q.item_at(2), False), position, 1.0, 0.0, moving=True)
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
                   (2, q.item_at(2), False), position, 1.0, 0.0, moving=True)
    if case == "edited_after_our_stop":
        b._stopped_at = 1.5
    later = None if serial is None else (5 if case == "edited_before_the_take_moved" else 47)
    stopped = _st(PlaybackState.STOPPED, 1, 0.0, uri="http://127.0.0.1:30000/1/stream.raw", serial=later)
    b._note_listen(stopped, (0, None, False), 0.0, 2.0, 1.0, edited=True)
    assert b._spot_at() == spot
    held = b._listen_held(stopped, b._playing(stopped) or (0, None, False), 2.0)
    assert held is (spot is not None or case == "edited_before_the_take_moved")
    # HQPlayer playing an entry of ours again ends the hold
    b._note_listen(_st(PlaybackState.PLAYING, 3, 1.0, uri="file:///E:/Music/A/03.flac", serial=48),
                   (3, q.item_at(3), False), 1.0, 3.0, 2.0)
    playing = _st(PlaybackState.PLAYING, 3, 2.0, uri="file:///E:/Music/A/03.flac", serial=48)
    assert not b._listen_held(playing, (3, q.item_at(3), False), 3.0)


def test_f7_an_edit_is_an_entry_the_node_never_gave_and_on_hqplayer5_newer_than_the_listen(fake):
    """F7's edit (`_edited`) is another controller's entry in HQPlayer's
    playlist — never the node's own tail (an entry it handed, then removed
    from the queue, playing out before the queue's slots). Where HQPlayer
    names no take (HQPlayer 5), an edit judges only a listen read while the
    playlist was the queue's mirror: an entry already there then is no
    newer word than the listen."""
    q = CanonicalQueue()
    q.replace([_item(f"E:/Music/A/0{i}.flac", i) for i in (1, 2, 3)])
    b = hb.HqpBackend(emit=lambda *_: None, queue=q)
    stopped = _st(PlaybackState.STOPPED, 2, 0.0, uri="file:///E:/Music/A/02.flac", serial=None)
    b._given = {"file:///E:/Music/X/removed.flac"}
    b._mirror = "tail"
    tail = [{"uri": "file:///E:/Music/X/removed.flac"}, {"uri": "file:///E:/Music/A/02.flac"},
            {"uri": "file:///E:/Music/A/03.flac"}]
    assert b._judge_mirror(tail, stopped, expect=b._mirror_moves) and not b._edited
    roons = [{"uri": "http://127.0.0.1:30000/1/stream.raw"}]
    assert b._judge_mirror(roons, stopped, expect=b._mirror_moves) and b._edited
    b._note_listen(_st(PlaybackState.PLAYING, 2, 30.0, uri="file:///E:/Music/A/02.flac", serial=None),
                   (2, q.item_at(2), False), 30.0, 1.0, 0.0, moving=True)
    assert not b._note_listen(stopped, (0, None, False), 0.0, 2.0, 1.0, edited=True)
    assert b._spot_at() is None and b._cut is None


def _listening(b, q, second, *, serial=5, at=1.0, length=300.0):
    """The node's listen of slot 2 read at `second`, at `at`, its position moving."""
    b._note_listen(_st(PlaybackState.PLAYING, 2, second, uri="file:///E:/Music/A/02.flac", serial=serial,
                       length=length), (2, q.item_at(2), False), second, at, at - 1.0, moving=True)


def _three(fake):
    q = CanonicalQueue()
    q.replace([_item(f"E:/Music/A/0{i}.flac", i) for i in (1, 2, 3)])
    return q, hb.HqpBackend(emit=lambda *_: None, queue=q)


def test_a_reading_whose_position_never_moved_holds_nothing_and_a_spot_set_since_stands(fake):
    """A start of the node's own that HQPlayer refuses after one PLAYING
    answer at 0 s (a filter it builds): that reading's position never moved,
    so nothing is held while HQPlayer rests on the refused track; and once
    D6 left the spot there and the person moved it, a takeover puts nothing
    back over that spot — the newer word stands. A takeover in a start's
    first seconds, its position not yet moved and no spot set since, cuts it
    there all the same (D3.2). Read on its own take again, paused or
    playing, a listen stays one."""
    q, b = _three(fake)
    _listening(b, q, 40.0)
    b._moved_at = 4.9                                       # the node's start of slot 3
    b._note_listen(_st(PlaybackState.PLAYING, 3, 0.0, uri="file:///E:/Music/A/03.flac", serial=6),
                   (3, q.item_at(3), False), 0.0, 5.0, 4.0)
    assert b._listen.item is q.item_at(3) and not b._listen.moved
    stopped = _st(PlaybackState.STOPPED, 3, 0.0, uri="file:///E:/Music/A/03.flac", serial=6)
    assert not b._listen_held(stopped, (3, q.item_at(3), False), 6.0)
    b._set_spot(q.item_at(3), 0)                            # D6's spot at the refused start
    b._set_spot(q.item_at(1), 0)                            # the person's Previous moved it
    roons = _st(PlaybackState.PLAYING, 1, 2.0, uri="http://127.0.0.1:30000/1/stream.raw", serial=7)
    assert not b._note_listen(roons, (0, None, True), 2.0, 9.0, 8.0) and b._spot_at() == (1, 0)
    q1, b1 = _three(fake)
    b1._note_listen(_st(PlaybackState.PLAYING, 2, 0.0, uri="file:///E:/Music/A/02.flac", serial=6),
                    (2, q1.item_at(2), False), 0.0, 5.0, 4.0)       # a start's first tick, nothing moved yet
    assert b1._note_listen(roons, (0, None, True), 2.0, 6.0, 5.0) and b1._spot_at() == (2, 0)
    q2, b2 = _three(fake)
    _listening(b2, q2, 40.0, at=1.0)
    b2._note_listen(_st(PlaybackState.PAUSED, 2, 41.0, uri="file:///E:/Music/A/02.flac", serial=5),
                    (2, q2.item_at(2), False), 41.0, 30.0, 29.0)
    assert (b2._listen.at, b2._listen.second, b2._listen.moved) == (30.0, 41.0, True)   # its own take, paused


@pytest.mark.parametrize("hqplayer", ["6", "5"])
def test_a_takeover_read_after_hqplayer_was_silent_late_in_the_track_still_cuts_the_listen(fake, hqplayer):
    """The listen of slot 2 read at 280 s of 300; HQPlayer silent 15 s while
    another controller's start builds, then the tick that reads its entry,
    the take moved once. Where HQPlayer names a take, the take tells what
    moved — Roon's start, not HQPlayer's own end: the spot at 280 s and the
    listen held (wall time plays nothing). HQPlayer 5 names none: the time
    since stands for it there, and HQPlayer 5's tick-before rule judges only
    the tick right after the reading."""
    q, b = _three(fake)
    serial = 5 if hqplayer == "6" else None
    _listening(b, q, 280.0, serial=serial)
    roons = _st(PlaybackState.PLAYING, 1, 2.0, uri="http://127.0.0.1:30000/1/stream.raw",
                serial=6 if serial is not None else None)
    cut = b._note_listen(roons, (0, None, True), 2.0, 16.0, 1.0)
    if hqplayer == "6":
        assert cut and b._spot_at() == (2, 280) and b._listen_held(roons, (0, None, True), 16.0)
    else:
        assert not cut and b._spot_at() is None


def test_the_hold_before_a_verdict_follows_the_take_where_hqplayer_names_one(fake):
    """Stopped on the node's own playlist after the listen's reading, with no
    command of the node's since: the take unmoved — a Stop someone sent (Roon
    before its Play, the person in HQPlayer's window) — holds; moved and read
    since as the node's own playlist — HQPlayer's own end — ends it; moved by
    the node's own DSP change (its build's resume comes) or still unread
    against the playlist (the verdict to come) holds. Where HQPlayer names no
    take, the time since the reading decides (HQPlayer 5)."""
    q, b = _three(fake)
    _listening(b, q, 40.0)
    st = lambda serial: _st(PlaybackState.STOPPED, 2, 0.0, uri="file:///E:/Music/A/02.flac", serial=serial)
    playing = (2, q.item_at(2), False)
    assert b._listen_held(st(5), playing, 900.0)                     # unmoved: no time bound
    assert not b._listen_held(st(6), playing, 2.0)                   # HQPlayer's own end, read as ours
    b._take_unread = True
    assert b._listen_held(st(6), playing, 2.0)                       # unread yet: the verdict to come
    b._take_unread, b._took_at = False, 1.5
    assert b._listen_held(st(6), playing, 2.0)                       # the node's own DSP change moved it
    q5, b5 = _three(fake)
    _listening(b5, q5, 40.0, serial=None)
    assert b5._listen_held(st(None), playing, 200.0)
    assert not b5._listen_held(st(None), playing, 260.0)             # 40 + 259 s: it could have played out


def test_the_hold_before_a_verdict_ends_at_the_nodes_own_word_and_with_its_track(fake):
    """Ticks between a listen's reading and a verdict hold it — but not past
    the node's own Seek, start or clear after that reading, nor once its
    track left the queue: each ended the listen, no one cut it."""
    def held(setup) -> bool:
        q, b = _three(fake)
        _listening(b, q, 40.0)
        setup(q, b)
        stopped = _st(PlaybackState.STOPPED, 1, 0.0, uri="file:///E:/Music/A/01.flac", serial=5)
        return b._listen_held(stopped, (1, q.item_at(1), False), 3.0)
    assert held(lambda q, b: None)
    for command in ("Seek", "Play", "SelectTrack", "Next", "Previous"):
        def sent(q, b, command=command):
            b._on_outcome(types.SimpleNamespace(command=command, failed=False, sent=2.0, ts=2.0, attributes={}))
        assert not held(sent), command
    def filtered(q, b):
        b._on_outcome(types.SimpleNamespace(command="SetFilter", failed=False, sent=2.0, ts=2.0, attributes={}))
    assert held(filtered)                                            # a DSP change ends nothing
    def cleared(q, b):
        b._cleared_at = 2.0
    assert not held(cleared)
    def removed(q, b):
        q.remove(2)
    assert not held(removed)


def test_l11_a_takeovers_cut_on_the_nodes_own_playlist_again_leaves_any_stopped_spot(fake, monkeypatch):
    """A takeover cut the node's listen (`_cut`), and the node's own playlist
    is HQPlayer's again (a take-back it refused: the queue mirrored, in
    step), a spot of the node's own standing (D6's): no other controller
    holds HQPlayer — Next only moves the spot (C1 §4.1), never starts."""
    q = CanonicalQueue()
    items = [_item(f"E:/Music/A/0{i}.flac", i) for i in (1, 2, 3, 4)]
    q.replace(items)
    b = hb.HqpBackend(emit=lambda *_: None, queue=q)
    b._cut = items[1]
    b._spot, b._spot_gen = (items[2], 0), 1
    b._last_tick = hb.PlaybackStatus(state="stopped", position=0.0, length=300.0, queue_index=3, volume=-3.0,
                                     extra={}, item=items[2])
    picked = []
    monkeypatch.setattr(b, "select", lambda index, track_id=None: picked.append(index) or True)
    assert b._mirror == "step" and b.next()
    assert picked == [] and b._spot == (items[3], 0)
    b._spot, b._mirror = (items[2], 0), "drift"                     # its playlist resting in HQPlayer
    assert b.next() and picked == [4]


def test_the_canary_reads_a_playlist_against_one_layout(fake, monkeypatch):
    """F7's edit asks, for each entry of HQPlayer's playlist, whether the node
    gave it: against one layout of the queue, read once — a fresh one per
    entry made the canary quadratic on the poller (0.5 s at 1000 entries)."""
    q = CanonicalQueue()
    q.replace([_item(f"E:/Music/A/{i:04d}.flac", i) for i in range(1, 301)])
    b = hb.HqpBackend(emit=lambda *_: None, queue=q)
    layouts = []
    whole = b._layout
    monkeypatch.setattr(b, "_layout", lambda: layouts.append(1) or whole())
    playlist = [{"uri": f"file:///E:/Music/A/{i:04d}.flac"} for i in range(2, 301)]
    b._judge_mirror(playlist, _st(PlaybackState.STOPPED, 1, 0.0, uri=playlist[0]["uri"], serial=3),
                    expect=b._mirror_moves)
    assert len(layouts) == 1 and not b._edited


# -- the renderer: Roon past UPnP -------------------------------------------------

@pytest.fixture(params=["paused", "playing", "stopped"])
def taken(request):
    """How the renderer's AVTransport says another controller took it: the
    KANN under Roon's RAAT recorded ("paused": our URI paused at its second,
    ROON-COEX §10.3 B1), another UPnP controller's URI ("playing"), our URI
    stopped (a guess)."""
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
    if taken == "stopped":
        assert [t for t, _ in listened] == ["00000000-0000-0000-0000-000000000001"] and listened[0][1] < 50
    else:
        assert listened == []           # held: a takeover's (F6), or a pause of our own track's (the KANN's word)
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
    controller's, the status external with the refusal said. A Stop or a
    Pause meanwhile sends it nothing (L11), so nothing finds it lost then:
    the next Play does, and the spot is still the takeover's."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    r.taken_by_another("playing")
    wait_for(lambda: (n.last.extra.get("resume") or {}).get("index") == 1, timeout=5, what="the spot")
    spot = n.last.extra["resume"]
    r.faults["SetAVTransportURI"] = [701]
    assert n.backend.select(2)
    wait_for(lambda: n.backend._error is not None and n.last.state != "loading", timeout=10, what="the refusal")
    assert n.last.extra.get("source") == "external" and n.last.extra.get("resume") == spot
    r.stop_answering()
    mark = len(r.calls)
    assert n.backend.stop()
    assert "Stop" not in _sent(r, mark) and n.last.extra.get("resume") == spot
    n.backend.play()
    wait_for(lambda: n.backend._gone, timeout=20, what="lost at the next Play")
    assert n.last.state == "stopped" and n.last.extra.get("resume") == spot
    r.answer_again()
    r2, n2 = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    assert n2.backend.stop()
    wait_for(lambda: n2.last.state == "stopped" and r2.state == "STOPPED", timeout=5, what="stopped")
    r2.taken_by_another("playing")
    wait_for(lambda: n2.last.extra.get("source") == "external", timeout=5, what="external")
    r2.stop_answering()
    mark2 = len(r2.calls)
    assert n2.backend.pause()
    assert "Pause" not in _sent(r2, mark2)
    n2.backend.play()
    wait_for(lambda: n2.backend._gone, timeout=20, what="lost at the next Play")
    # no spot made from the other controller's state: none, or the node's own start's, at the top
    assert n2.last.state == "stopped" and (n2.last.extra.get("resume") or {}).get("position", 0) == 0
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
        spot = st["resume"]
        _observed("F7", at=at, node_commands=_node_commands(fake, n, roon), rows=len(_rows(writes)),
                  status={k_: st.get(k_) for k_ in ("state", "track_index", "source", "resume")})
        assert (st["state"], st["track_index"], st["resume"]["index"]) == ("stopped", 2, 2)
        assert abs(st["resume"]["position"] - at) <= 2
        assert _node_commands(fake, n, roon) == []
        assert _rows(writes) == [] and tracker._play_session is not None
        # The rest of A8's cycle under the verdict — more Plays, Roon's stop,
        # its clear: the spot and the held listen stand through it
        for _ in range(4):
            roon.play_again()
            time.sleep(0.15)
        roon.stop()
        roon._send("<PlaylistClear/>")
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 4, timeout=10)
        st = mgr._latest_status
        assert (st["state"], st["track_index"], st.get("resume")) == ("stopped", 2, spot)
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


def test_f6_roons_stop_before_its_play_holds_the_listen_until_its_play_cuts_it_there(fake, roon, listens,
                                                                                    monkeypatch):
    """F6 before the verdict (ROON-COEX §6.1 P4): Roon's Stop, its clear and
    its add, read before its Play — a stop of our entry the node did not
    send — hold the node's listen of slot 2, nothing written; Roon's Play
    then cuts it at its second, and the node's Play takes HQPlayer back
    there: one history row, one now-playing."""
    writes, lastfm = listens
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    try:
        at = mgr._latest_status.get("position")
        roon.start_before_its_play()
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 4, timeout=10)
        held = (_rows(writes), tracker._play_session is not None, mgr._latest_status.get("state"))
        roon.play_again()
        _wait(lambda: mgr._latest_status.get("resume") is not None, timeout=10)
        spot = mgr._latest_status["resume"]
        _observed("F6-before-its-play", mode=fake.raw_streams, at=at, held=held, spot=spot)
        assert held == ([], True, "stopped")
        assert spot["index"] == 2 and abs(spot["position"] - at) <= 2
        assert _rows(writes) == [] and tracker._play_session is not None
        _takes_back(fake, mgr, b, roon, "F6-before-its-play", interrupted_at=(2, at))
        assert b.stop()
        _wait(lambda: _rows(writes), timeout=10)
        assert [p["tid"] for p in _rows(writes)] == [_slot_track(b, 2)]
        assert lastfm.count("update_now_playing") == 1
    finally:
        b.shutdown()


def test_f6_a_start_of_the_nodes_own_hqplayer_refuses_ends_the_listen_before_it(fake, listens, monkeypatch):
    """A tap of the node's own on a slot whose rate HQPlayer refuses — it
    stops there at once: that tap ended the listen of the slot before, no
    one cut it — written at once, never held for a verdict."""
    writes, lastfm = listens
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    try:
        fake.refuse_play.add("A/03.flac")
        assert b.select(3, "track-3")
        _wait(lambda: fake.state == int(PlaybackState.STOPPED) and fake.track == 3, timeout=10)
        k = len(fake.commands)
        _wait(lambda: _rows(writes) or _statuses(fake, k) >= 6, timeout=20)
        assert [p["tid"] for p in _rows(writes)] == ["track-2"] and tracker._play_session is None
    finally:
        b.shutdown()


def test_f6_the_queues_last_slot_played_to_its_end_writes_its_listen_at_once(fake, listens, monkeypatch):
    """HQPlayer plays the queue's last slot to its end and stops by itself:
    the listen is over — written at once, never held as a stop the node did
    not send."""
    from playback import manager as manager_mod
    writes, lastfm = listens
    mgr = _album(3)
    monkeypatch.setattr(manager_mod, "manager", mgr)
    b = _attach(mgr)
    try:
        assert b.select(3)
        assert _wait(lambda: mgr._latest_status.get("track_index") == 3
                     and tracker._play_session is not None and tracker._play_session.max_position >= 3)
        with fake._lock:
            fake.length = int(fake.position) + 3            # it ends three reads from now
        _wait(lambda: fake.state == int(PlaybackState.STOPPED), timeout=10)
        k = len(fake.commands)
        _wait(lambda: _rows(writes) or _statuses(fake, k) >= 3, timeout=10)
        assert [p["tid"] for p in _rows(writes)] == ["track-3"] and tracker._play_session is None
    finally:
        b.shutdown()


def test_f6_the_cut_listens_track_leaving_the_queue_ends_the_hold_at_its_own_seconds(fake, listens, monkeypatch):
    """Roon holds HQPlayer where it cut the node's listen of slot 2; the
    person removes that track from the queue: the spot goes with it, and so
    does the hold — the listen is written at its own seconds."""
    writes, lastfm = listens
    mgr, b, roon, spot = _roon_holds_the_spot(fake, monkeypatch)
    try:
        assert _rows(writes) == [] and tracker._play_session is not None
        assert mgr.remove(2, "track-2") is True
        _wait(lambda: _rows(writes), timeout=10)
        rows = _rows(writes)
        assert [p["tid"] for p in rows] == ["track-2"] and abs(rows[0]["dur"] - spot["position"]) <= 1.5
        assert tracker._play_session is None and mgr._latest_status.get("resume") is None
    finally:
        b.shutdown()
        roon.close()


def test_roons_stop_on_the_last_slot_before_its_play_is_no_end_of_the_queue(fake, listens, monkeypatch):
    """The node plays the queue's last slot; Roon's Stop, its clear and its
    add are read before its Play: the listen is held for the verdict to come
    — no end of the queue, the session not archived."""
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
        roon.start_before_its_play()
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 4, timeout=10)
        assert mgr._latest_status.get("state") == "stopped" and closed == []
    finally:
        b.shutdown()
        roon.close()


def test_l11_a_stop_while_a_take_back_waits_under_roon_keeps_its_spot_and_next_takes_hqplayer_back(
        fake, monkeypatch):
    """Roon holds HQPlayer; the person taps slot 3, whose bytes are away (the
    take-back waits), and presses Stop: nothing is sent, the waiting start's
    track and second are the spot, as a Stop leaves them. Next there is a
    press that starts playback under another controller (ROON-COEX L11): it
    takes HQPlayer back at the slot after — never a spot moved in silence."""
    mgr, b, prov, toks, roon = _roon_holds_streams(fake, monkeypatch)
    try:
        prov.go.clear()
        assert b.select(3, "stream-3")
        assert _wait(lambda: mgr._latest_status.get("state") == "loading")
        n = _mark(fake, roon)
        assert b.stop()
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 2)
        stopped = dict(mgr._latest_status)
        assert _node_commands(fake, n, roon) == [] and fake.state == PLAYING
        assert (stopped["state"], stopped.get("resume")) == ("stopped", {"index": 3, "position": 0})
        assert b.next()
        prov.go.set()
        _wait(lambda: fake.state == PLAYING and fake.track == 4
              and not any(u.endswith("/stream.raw") for u in fake.playlist), timeout=15)
        st = mgr._latest_status
        _observed("L11-stop-then-next", stopped=stopped.get("resume"), hqplayer=(fake.state, fake.track),
                  status=(st.get("state"), st.get("track_index"), st.get("source")))
        assert fake.track == 4 and st.get("source") != "external"
    finally:
        prov.go.set()
        b.shutdown()
        roon.close()


def test_next_previous_or_a_seek_once_roon_stopped_an_hqplayer_it_took_idle_is_refused(fake, listens,
                                                                                      monkeypatch):
    """Roon took an idle HQPlayer — no spot — and stopped: its playlist
    rests in HQPlayer with nothing of ours to count from. Next, Previous and
    a Seek are refused in words and nothing is sent, as while it plays
    (ROON-COEX L11) — never HQPlayer's own, which moves Roon's playlist."""
    mgr, b, roon = _roon_holds_an_idle_hqplayer(fake, monkeypatch)
    try:
        roon.stop()
        k = len(fake.commands)
        _wait(lambda: fake.state == int(PlaybackState.STOPPED) and _statuses(fake, k) >= 3, timeout=10)
        n = _mark(fake, roon)
        refused = (b.next(), b.previous(), b.seek(30))
        words = b.refusal()
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 2)
        _observed("L11-roon-stopped-idle", refused=refused, mirror=b._mirror,
                  node_commands=_node_commands(fake, n, roon))
        assert refused == (False, False, False) and "another app is playing through" in words
        assert _node_commands(fake, n, roon) == [] and fake.playlist[0].endswith("/stream.raw")
    finally:
        b.shutdown()
        roon.close()


def _one_stream_on_hqplayer(fake, monkeypatch):
    """HQPlayer plays a queue of ONE streamed slot (a phantom track played
    alone), its listen running."""
    from playback import manager as manager_mod
    from playback.manager import PlaybackManager
    from streaming import service as streaming_service
    from streaming.base import TrackQuery
    proxy = streaming_service.get_proxy()
    prov = _GatedStream()
    toks = proxy.start_session([(TrackQuery(artist="Artist", title="Stream 1", album="Album", duration=300.0,
                                            track_id="stream-1"), [(prov, None)])])
    assert proxy.wait_ready(toks[0], timeout=5).audio is not None
    mgr = PlaybackManager()
    mgr.queue.replace([QueueItem(track_id="stream-1", media_file_id=None, source={"kind": "proxy", "token": toks[0]},
                                 title="Stream 1", artist="Artist", album="Album", duration_seconds=300.0,
                                 preview=True, provider="stub")])
    monkeypatch.setattr(manager_mod, "manager", mgr)
    b = _attach(mgr)
    assert b.select(1)
    assert _wait(lambda: mgr._latest_status.get("state") == "playing" and mgr._latest_status.get("track_index") == 1
                 and tracker._play_session is not None and tracker._play_session.max_position >= 3, timeout=15)
    return mgr, b


@pytest.mark.parametrize("roon_does", ["its refused start", "its stream, then its stop"])
def test_roons_one_entry_against_a_one_slot_streamed_queue_is_no_mirror_of_ours(fake, listens, monkeypatch,
                                                                                roon_does):
    """A queue of one streamed slot, and Roon's one entry in HQPlayer's
    playlist: a stream slot's token is re-minted, so its value is never
    compared — but an entry the node never handed there is no mirror of ours
    (it was read as the queue's, in step). Roon's start HQPlayer refuses
    leaves the spot (F7); its stream played and stopped, the node's Play
    takes the queue afresh — never HQPlayer's own Play, nor a SelectTrack and
    a Seek into Roon's playlist (D3)."""
    fake.raw_streams = "uri"
    if roon_does == "its refused start":
        fake.refuse_play.add("stream.raw")
    roon = FakeRoon(fake.port)
    mgr, b = _one_stream_on_hqplayer(fake, monkeypatch)
    try:
        roon.play_track()
        if roon_does == "its refused start":
            for _ in range(6):
                roon.play_again()
                time.sleep(0.15)
        assert _wait(lambda: mgr._latest_status.get("resume") is not None, timeout=10)
        if roon_does != "its refused start":
            roon.stop()
        k = len(fake.commands)
        _wait(lambda: fake.state == int(PlaybackState.STOPPED) and _statuses(fake, k) >= 3, timeout=10)
        assert b._mirror in ("drift", "lost") and mgr._latest_status.get("resume", {}).get("index") == 1
        fake.refuse_play.clear()
        n = _mark(fake, roon)
        assert b.play()
        _wait(lambda: fake.state == PLAYING and not any(u.endswith("/stream.raw") for u in fake.playlist),
              timeout=15)
        sent = _node_commands(fake, n, roon)
        _observed("one-stream", roon_does=roon_does, sent=sent, playlist=fake.playlist)
        assert "PlaylistClear" in sent or any(a.get("clear") == "1" for c, a in fake.commands[n[0]:]
                                               if c == "PlaylistAdd")
        assert not any(u.endswith("/stream.raw") for u in fake.playlist) and fake.state == PLAYING
    finally:
        b.shutdown()
        roon.close()


def test_a_drifted_playlist_of_the_nodes_own_is_no_other_apps(fake, listens, monkeypatch):
    """The node's own playlist drifted — an entry removed in HQPlayer's own
    window, nothing of another's in it — and HQPlayer played its last entry
    out, resting on no entry: Previous, Next and a Seek are never refused as
    another app's (no app is); L11's refusal is for another controller's
    playlist alone."""
    monkeypatch.setattr(hb, "DRIFT_CHECK_EVERY", 1)
    from playback import manager as manager_mod
    mgr = _album(3)
    monkeypatch.setattr(manager_mod, "manager", mgr)
    b = _attach(mgr)
    other = FakeRoon(fake.port)
    try:
        assert b.select(2)
        assert _wait(lambda: mgr._latest_status.get("track_index") == 2 and fake.state == PLAYING)
        other._send('<PlaylistRemove index="3"/>')            # an edit in HQPlayer's own window
        assert _wait(lambda: b._mirror == "drift", timeout=10)
        fake.end_now()
        k = len(fake.commands)
        _wait(lambda: fake.state == int(PlaybackState.STOPPED) and _statuses(fake, k) >= 3, timeout=10)
        words = []
        for press in (b.previous, b.next, lambda: b.seek(20)):
            press()
            words.append(b.refusal())
        assert not b._edited and not any("another app" in w for w in words), words
    finally:
        b.shutdown()
        other.close()


def test_hqplayers_own_end_of_the_queue_read_short_of_its_last_seconds_writes_the_listen(fake, listens,
                                                                                       monkeypatch):
    """The node plays the queue's last slot; HQPlayer ends it by itself with
    the last reading not in its last seconds (a short entry whose Status kept
    the position, a poll backed off): its take moved with no command of the
    node's, read as the node's own playlist — HQPlayer's own end, no one's
    Stop: the listen is written, never held."""
    from playback import manager as manager_mod
    writes, lastfm = listens
    mgr = _album(3)
    monkeypatch.setattr(manager_mod, "manager", mgr)
    b = _attach(mgr)
    try:
        assert b.select(3)
        assert _wait(lambda: mgr._latest_status.get("track_index") == 3
                     and tracker._play_session is not None and tracker._play_session.max_position >= 5)
        fake.end_now()
        _wait(lambda: fake.state == int(PlaybackState.STOPPED), timeout=5)
        k = len(fake.commands)
        _wait(lambda: _rows(writes) or _statuses(fake, k) >= 8, timeout=20)
        assert [p["tid"] for p in _rows(writes)] == ["track-3"] and tracker._play_session is None
    finally:
        b.shutdown()


def _idle_node(fake, monkeypatch, n=3):
    from playback import manager as manager_mod
    mgr = _album(n)
    monkeypatch.setattr(manager_mod, "manager", mgr)
    return mgr, _attach(mgr)


@pytest.mark.parametrize("start", ["a tap's first tick", "a tap, its filter starting", "HQPlayer's own advance"])
def test_a_takeover_in_a_starts_first_seconds_leaves_the_spot_there(fake, listens, monkeypatch, start):
    """D3.2 from a start's first seconds, its position not yet moved — the
    first tick of a tap, a filter starting (the position standing for
    seconds), HQPlayer's own advance onto slot 2: Roon's takeover cuts that
    listen there all the same — the spot at slot 2, and the node's Play takes
    HQPlayer back at slot 2, never the queue's head again."""
    fake.raw_streams = "uri"
    roon = FakeRoon(fake.port)
    mgr, b = _idle_node(fake, monkeypatch)
    try:
        if start == "HQPlayer's own advance":
            assert b.select(1)
            assert _wait(lambda: mgr._latest_status.get("track_index") == 1
                         and (mgr._latest_status.get("position") or 0) >= 3, timeout=10)
            fake.advance_lag_polls = 4
            fake.end_now()
        else:
            fake.start_polls = 0 if start == "a tap's first tick" else 4
            assert b.select(2)
        assert _wait(lambda: mgr._latest_status.get("state") == "playing"
                     and mgr._latest_status.get("track_index") == 2, timeout=15)
        roon.play_track()
        assert _wait(lambda: mgr._latest_status.get("source") == "external", timeout=10)
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 2, timeout=10)
        spot = mgr._latest_status.get("resume")
        assert spot is not None and spot["index"] == 2 and spot["position"] <= 2
        assert b.play()
        _wait(lambda: fake.playlist == MIRROR and fake.state == PLAYING, timeout=15)
        assert fake.track == 2
    finally:
        b.shutdown()
        roon.close()


def test_f7_under_a_tail_mirror_is_another_controllers_playlist(fake, listens, monkeypatch):
    """The node removed the slot it played (a tail mirror, as any removal of
    the playing slot leaves) and plays on; Roon takes over with a stream
    HQPlayer refuses (F7): an entry of another controller's makes the
    playlist drift, never a tail that holds — so Next there takes HQPlayer
    back (L11), never only moves the spot."""
    fake.raw_streams = "uri"
    roon = FakeRoon(fake.port)
    mgr, b = _idle_node(fake, monkeypatch, n=4)
    try:
        assert b.select(2)
        assert _wait(lambda: mgr._latest_status.get("track_index") == 2
                     and (mgr._latest_status.get("position") or 0) >= 3, timeout=10)
        assert mgr.remove(2, "track-2") is True
        assert _wait(lambda: b._mirror == "tail", timeout=5), b._mirror
        fake.end_now()
        assert _wait(lambda: mgr._latest_status.get("track_id") == "track-3"
                     and mgr._latest_status.get("state") == "playing"
                     and (mgr._latest_status.get("position") or 0) >= 3, timeout=15)
        fake.refuse_play.add("stream.raw")
        roon.play_track()
        for _ in range(6):
            roon.play_again()
            time.sleep(0.15)
        assert _wait(lambda: mgr._latest_status.get("resume") is not None, timeout=10)
        assert b._mirror == "drift"
        fake.refuse_play.clear()
        assert b.next()
        _wait(lambda: fake.state == PLAYING and not any(u.endswith("/stream.raw") for u in fake.playlist),
              timeout=15)
        assert not any(u.endswith("/stream.raw") for u in fake.playlist) and fake.state == PLAYING
    finally:
        b.shutdown()
        roon.close()


def test_a_uri_slot_of_the_nodes_own_is_its_mirror_in_a_new_attachment(fake):
    """A queue holding a "uri" slot (adopted from HQPlayer's playlist, a file
    re-bound after it left the library), HQPlayer's playlist exactly the
    queue, judged by an attachment that handed nothing yet (a backend
    restart): the slot's URI is its identity, never re-minted — in step."""
    q = CanonicalQueue()
    q.replace([_item("E:/Music/A/01.flac", 1),
               QueueItem(track_id=None, media_file_id=None, source={"kind": "uri", "uri": "file:///D:/Other/x.flac"},
                         title="x"),
               _item("E:/Music/A/03.flac", 3)])
    b = hb.HqpBackend(emit=lambda *_: None, queue=q)
    b._mirror = "lost"
    playlist = [{"uri": "file:///E:/Music/A/01.flac"}, {"uri": "file:///D:/Other/x.flac"},
                {"uri": "file:///E:/Music/A/03.flac"}]
    assert b._judge_mirror(playlist, _st(PlaybackState.PLAYING, 1, 30.0, uri=playlist[0]["uri"], serial=3),
                           expect=b._mirror_moves)
    assert b._mirror == "step" and not b._edited


def test_a_stream_of_the_nodes_own_is_its_mirror_before_the_media_proxy_started(fake, monkeypatch):
    """A boot: HQPlayer attached before the media proxy starts, playing the
    node's own phantom stream (a token of the session before). Its address
    is the node's own — the port the proxy is configured to serve on — so
    the playlist is the queue's mirror, no edit of another's; never read
    as another app playing (L11 then sent the node's Pause nothing)."""
    from config import settings
    from streaming import service as streaming_service
    proxy = streaming_service.get_proxy()
    monkeypatch.setattr(settings, "media_proxy_port", proxy.port)
    q = CanonicalQueue()
    q.replace([_item("E:/Music/A/01.flac", 1),
               QueueItem(track_id="stream-2", media_file_id=None, source={"kind": "proxy", "token": "earlier"},
                         title="s", preview=True)])
    b = hb.HqpBackend(emit=lambda *_: None, queue=q)
    b._mirror = "lost"
    url = proxy.url_for("earlier", host=b._url_host)
    monkeypatch.setattr(streaming_service, "_proxy", None)
    playlist = [{"uri": "file:///E:/Music/A/01.flac"}, {"uri": url}]
    assert b._judge_mirror(playlist, _st(PlaybackState.PLAYING, 1, 30.0, uri=playlist[0]["uri"], serial=3),
                           expect=b._mirror_moves)
    assert b._mirror == "step" and not b._edited


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


def test_p1_a_reconnect_inside_roons_cycle_sends_its_playlist_nothing(fake, listens, monkeypatch):
    """P1 (the heavy A1's question, ROON-COEX §6.1; the design session's
    answer (a) of 2026-11-09): Roon's start stands between its add and its
    Play while HQPlayer answers nothing past the node's client timeout (a
    heavy engine's Stop). The node reconnects and reads a stopped HQPlayer
    holding Roon's stream: that is another controller's playlist, not a
    restart — drift, the node sends it nothing, and Roon's Play plays
    Roon's stream."""
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
                  hqplayer=(fake.state, fake.track), mirror=b._mirror)
        assert _node_commands(fake, n, roon) == [] and b._mirror == "drift"
        assert fake.playlist == [uri] and fake.state == PLAYING
    finally:
        b.shutdown()
        roon.close()


def test_p1_an_add_owed_through_the_silence_sends_roons_playlist_nothing(fake, listens, monkeypatch):
    """P1 with the person's append in the silence: its add outlasts the
    client's wait — owed, the mirror lost to it, a reconverge due — and
    HQPlayer carries it out after the silence into Roon's playlist. The
    return reads Roon's entry there: drift all the same, the reconverge
    given up, nothing sent past that add — and Roon's Play plays Roon's
    stream."""
    fake.raw_streams = "uri"
    roon = FakeRoon(fake.port)
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    try:
        n = _mark(fake, roon)
        uri = roon.start_before_its_play()
        fake.silence(8.0)
        monkeypatch.setattr(b._link.client, "timeout", 1.0)        # the add's wait (5 s live)
        added = []
        t = threading.Thread(target=lambda: added.append(mgr.append([_item("E:/Music/S/later.flac", 9)])),
                             daemon=True)
        t.start()
        t.join(20)
        assert (b._mirror, b._reconverge_due) == ("lost", True)
        monkeypatch.setattr(b._link.client, "timeout", hl.COMMAND_WAIT_S)
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 5, timeout=40)
        roon.play_again()
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 3, timeout=15)
        _observed("P1-owed", added=added, node_commands=_node_commands(fake, n, roon), playlist=fake.playlist[:6],
                  hqplayer=(fake.state, fake.track), mirror=b._mirror)
        assert _node_commands(fake, n, roon) == ["PlaylistAdd"], "the node wrote over Roon's playlist"
        assert b._mirror == "drift" and not b._reconverge_due
        assert fake.playlist[0] == uri and fake.state == PLAYING and fake.track == 1
    finally:
        b.shutdown()
        roon.close()


def _an_owed_add(fake, mgr, b, monkeypatch) -> threading.Event:
    """The person's append goes to a file HQPlayer opens first (a drive
    spinning up): the add owed, the mirror lost to it, a reconverge due —
    HQPlayer answering the rest meanwhile, no silence, no return. Returns
    the gate that lets HQPlayer carry the add out."""
    go = threading.Event()
    fake.probe_holds["S/later.flac"] = go
    monkeypatch.setattr(b._link.client, "timeout", 0.4)        # the add's wait (5 s live)
    assert mgr.append([_item("E:/Music/S/later.flac", 9)]) == 1
    assert _wait(lambda: b._link.owed is not None and b._mirror == "lost", 10), b._mirror
    assert b._reconverge_due
    monkeypatch.setattr(b._link.client, "timeout", hl.COMMAND_WAIT_S)
    return go


@pytest.mark.parametrize("emptied", [False, True])
def test_p1_an_owed_add_answered_into_roons_playlist_sends_it_nothing(fake, listens, monkeypatch, emptied):
    """P1's binding with no reconnect at all (review round 2 of the answers):
    the add HQPlayer owed lands behind Roon's stream, and the reconverge that
    answer brings reads Roon's playlist — stopped on an entry the node never
    gave, the queue's slots gone from it: nothing is written over it, and
    Roon's Play plays Roon's stream. So too with the queue emptied while the
    add was owed (every slot removed: the queue's alone), no slot of ours to
    look for — never a clear of HQPlayer's playlist under Roon's Play."""
    fake.raw_streams = "uri"
    roon = FakeRoon(fake.port)
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    try:
        go = _an_owed_add(fake, mgr, b, monkeypatch)
        if emptied:
            while mgr.queue.snapshot():
                assert mgr.remove(1)
        n = _mark(fake, roon)
        uri = roon.start_before_its_play()                         # Stop, Clear, its stream's add
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 3, timeout=15)
        go.set()                                                   # the drive is up: the add lands
        assert _wait(lambda: b._link.owed is None, 10)
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 4, timeout=20)
        roon.play_again()
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 3, timeout=15)
        _observed("P1-owed-add", emptied=emptied, node_commands=_node_commands(fake, n, roon),
                  playlist=fake.playlist[:6], hqplayer=(fake.state, fake.track), mirror=b._mirror)
        assert _node_commands(fake, n, roon) == ["PlaylistAdd"], "the node wrote over Roon's playlist"
        assert b._mirror == "drift"
        assert fake.playlist[0] == uri and fake.state == PLAYING and fake.track == 1
    finally:
        b.shutdown()
        roon.close()


def test_p1_an_owed_add_that_is_all_the_queue_holds_sends_roons_playlist_nothing(fake, listens, monkeypatch):
    """The owed add is all the queue holds (the album removed while it was
    owed: the queue's alone); HQPlayer carries it out behind Roon's stream,
    and the reconverge reads Roon's playlist, stopped — the one slot of the
    queue present in it. By content alone that is a tail mirror's playlist;
    the mirror was lost from in step, so it is another controller's:
    nothing is written over it, and Roon's Play plays Roon's stream."""
    fake.raw_streams = "uri"
    roon = FakeRoon(fake.port)
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    try:
        go = _an_owed_add(fake, mgr, b, monkeypatch)
        for _ in range(3):
            assert mgr.remove(1)
        assert [it.track_id for it in mgr.queue.snapshot()] == ["track-9"]
        n = _mark(fake, roon)
        uri = roon.start_before_its_play()
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 3, timeout=15)
        go.set()
        assert _wait(lambda: b._link.owed is None, 10)
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 4, timeout=20)
        roon.play_again()
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 3, timeout=15)
        _observed("P1-owed-whole-queue", node_commands=_node_commands(fake, n, roon),
                  playlist=fake.playlist[:6], hqplayer=(fake.state, fake.track), mirror=b._mirror)
        assert _node_commands(fake, n, roon) == ["PlaylistAdd"], "the node wrote over Roon's playlist"
        assert fake.playlist[0] == uri and fake.state == PLAYING and fake.track == 1
    finally:
        b.shutdown()
        roon.close()


@pytest.mark.parametrize("word", ["append", "replace", "radio"])
def test_p1_the_persons_words_for_the_queue_while_hqplayer_rests_on_roons_stream_send_it_nothing(
        fake, listens, monkeypatch, word):
    """P1's drift: a reconnect read a stopped HQPlayer holding Roon's stream,
    its Play still to come. The person's words for the queue then — an
    append, a new queue put together without play, the radio's clear — are
    the queue's alone, as while Roon plays: nothing goes into Roon's
    playlist, and Roon's Play plays its stream — never our track inside
    Roon's session."""
    fake.raw_streams = "uri"
    roon = FakeRoon(fake.port)
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    try:
        uri = roon.start_before_its_play()
        fake.silence(6.0)
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 4, timeout=30)
        assert b._mirror == "drift" and fake.playlist == [uri] and fake.state == int(PlaybackState.STOPPED)
        n = _mark(fake, roon)
        if word == "append":
            assert mgr.append([_item("E:/Music/B/09.flac", 9)]) == 1
        elif word == "replace":
            mgr.replace_queue([_item("E:/Music/C/01.flac", 21), _item("E:/Music/C/02.flac", 22)], play=False)
        else:
            mgr.clear_for_radio(lambda: None)
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 2)
        sent, held = _node_commands(fake, n, roon), list(fake.playlist)
        roon.play_again()
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 3)
        _observed("P1-rests", word=word, node_commands=sent, playlist=[u[-24:] for u in held],
                  hqplayer=(fake.state, fake.track))
        assert sent == [] and held == [uri], "the node wrote into Roon's playlist"
        assert fake.playlist == [uri] and fake.state == PLAYING and fake.track == 1
    finally:
        b.shutdown()
        roon.close()


@pytest.mark.parametrize("owed", [False, True])
def test_p1_a_persons_play_after_the_drift_takes_hqplayer_back(fake, listens, monkeypatch, owed):
    """D3 after P1's drift — and after a lost mirror's drift: Roon's Play
    plays its stream; then the person's Play in the node takes HQPlayer
    back, the queue handed over, an entry of ours playing."""
    fake.raw_streams = "uri"
    roon = FakeRoon(fake.port)
    mgr, b = _node_plays_slot_2(fake, monkeypatch)
    try:
        roon.start_before_its_play()
        fake.silence(8.0 if owed else 6.0)
        if owed:
            monkeypatch.setattr(b._link.client, "timeout", 1.0)
            t = threading.Thread(target=lambda: mgr.append([_item("E:/Music/S/later.flac", 9)]), daemon=True)
            t.start()
            t.join(20)
            monkeypatch.setattr(b._link.client, "timeout", hl.COMMAND_WAIT_S)
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 5, timeout=40)
        roon.play_again()
        k = len(fake.commands)
        _wait(lambda: _statuses(fake, k) >= 3, timeout=15)
        assert b._mirror == "drift" and fake.state == PLAYING and fake.playlist[0].endswith("stream.raw")
        assert b.play()
        assert _wait(lambda: fake.state == PLAYING and 1 <= fake.track <= len(fake.playlist)
                     and fake.playlist[fake.track - 1].startswith("file:///E:/Music/"), 15), \
            (fake.state, fake.track, fake.playlist)
    finally:
        b.shutdown()
        roon.close()


def test_p1_a_tail_mirror_through_a_silence_stays_a_tail(fake, output, monkeypatch):
    """A tail mirror over an entry of HQPlayer's own before ours, HQPlayer
    playing our slot 1: a silence past the client's timeout (a heavy build)
    is no other controller's playlist — HQPlayer's own entries before the
    one it reads are a tail's — so the tail stays the node's, and a play
    next reaches HQPlayer behind the entry it reads."""
    mgr, b = output
    with fake._lock:
        fake.playlist.insert(0, "file:///E:/Other/x.flac")       # the person's, in HQPlayer's own window
        fake.track += 1
    b._move_mirror("tail")
    _polls(fake, 2)
    assert b._mirror == "tail"
    fake.silence(6.0)
    k = len(fake.commands)
    _wait(lambda: _statuses(fake, k) >= 4, timeout=30)
    _polls(fake, 2)
    after = b._mirror
    assert mgr.append([_item("E:/Music/B/09.flac", 19)], "next") == 1
    _polls(fake, 2)
    _observed("P1-tail", after=after, playlist=list(fake.playlist), track=fake.track)
    assert after == "tail"
    assert any("B/09.flac" in u for u in fake.playlist), "the play next never reached HQPlayer"


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


def test_b_a_pause_while_a_take_back_waits_for_its_stream_ends_the_wait_and_sends_nothing(tmp_path, proxy,
                                                                                         renderers, nodes, quality):
    """Another controller holds the renderer, the spot standing; the person
    taps slot 2, whose stream is on its way (the take-back waits), then
    presses Pause: the wait ends — nothing is sent, the waiting slot and its
    second the spot — and the stream's landing loads nothing."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("resume") is not None, timeout=5, what="the spot")
    waiting = n.queue.item_at(2)
    waiting.play = {"kind": "pending"}
    assert n.backend.select(2)
    wait_for(lambda: n.last.state == "loading" and n.last.queue_index == 2, what="the take-back waiting")
    mark = len(r.calls)
    assert n.backend.pause()
    st = n.last
    waiting.play = streamed(proxy, 2, cover_url="").source
    n.backend.slot_ready(2)
    time.sleep(1.5)
    _observed("B-pause-waiting-take-back", status=(st.state, st.queue_index, st.extra.get("resume")),
              node_sent=_sent(r, mark), renderer=r.uri[-24:])
    assert _sent(r, mark) == [] and "raat://" in r.uri
    assert (st.state, st.queue_index, st.extra.get("source"), st.extra.get("resume")) == (
        "stopped", 2, "external", {"index": 2, "position": 0.0})


def test_b_next_at_the_last_slot_under_another_controller_keeps_the_held_listen(tmp_path, proxy, renderers,
                                                                                manager, heard, quality):
    """A renderer: the node plays the last slot past its scrobble line;
    another controller takes it, the listen held. Next has no slot after
    it: the spot stays and nothing is sent — the held listen goes on, and
    the take-back at the spot is the same listen: one row, one now-playing,
    one scrobble."""
    listened, lastfm = heard
    r = renderers()
    _on_renderer(manager, r, [track(tmp_path, 1, seconds=60.0), track(tmp_path, 2, seconds=60.0)])
    _playing_at(manager, r, 1)
    assert manager.backend().select(2)
    wait_for(lambda: manager.latest_status.get("state") == "playing"
             and manager.latest_status.get("track_index") == 2, timeout=10, what="slot 2")
    assert manager.backend().seek(33)
    _playing_at(manager, r, 34)
    r.taken_by_another("playing")
    wait_for(lambda: manager.latest_status.get("source") == "external", timeout=5, what="external")
    spot = manager.latest_status.get("resume")
    t2 = "00000000-0000-0000-0000-000000000002"
    mark = len(r.calls)
    assert manager.backend().next()
    time.sleep(1.5)
    assert _sent(r, mark) == [] and manager.latest_status.get("resume") == spot
    assert [t for t, _ in listened if t == t2] == []
    assert manager.backend().play()
    wait_for(lambda: r.state == "PLAYING" and "raat://" not in r.uri
             and manager.latest_status.get("state") == "playing", timeout=10, what="taken back")
    _playing_at(manager, r, 38)
    assert manager.backend().stop()
    wait_for(lambda: any(t == t2 for t, _ in listened), timeout=10, what="the listen written")
    _observed("B-next-at-the-end", spot=spot, listened=listened, lastfm=lastfm)
    assert [t for t, _ in listened if t == t2] == [t2]
    assert lastfm.count(("update_now_playing", "Track 2")) == 1
    assert lastfm.count(("scrobble", "Track 2")) == 1


@pytest.mark.parametrize("word", ["stop", "pause"])
def test_b_a_stop_or_a_pause_during_the_take_back_load_ends_it_and_sends_the_renderer_nothing(
        tmp_path, proxy, renderers, nodes, quality, monkeypatch, word):
    """Another controller holds the renderer, the spot standing; the person
    presses Play — the take-back loads, its stream still being readied —
    then Stop or Pause before our URI is handed over: the load ends there,
    nothing sent, the renderer the other controller's still, the spot and
    the held listen as they were."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("taken") and n.last.extra.get("resume") is not None, timeout=5,
             what="the spot")
    spot = n.last.extra["resume"]
    gate = threading.Event()
    ready = n.backend._serve_ready

    async def slow(item, url, *, front):
        await asyncio.get_running_loop().run_in_executor(None, gate.wait, 10)
        return await ready(item, url, front=front)
    monkeypatch.setattr(n.backend, "_serve_ready", slow)
    mark = len(r.calls)
    assert n.backend.play()
    wait_for(lambda: n.last.state == "loading", timeout=5, what="the take-back loading")
    loading = n.last
    assert getattr(n.backend, word)()
    said = n.last
    gate.set()
    time.sleep(1.5)
    _observed("B-word-during-take-back", word=word, node_sent=_sent(r, mark), renderer=r.uri[-24:],
              statuses=[(s_.state, s_.extra.get("source"), s_.extra.get("resume"), s_.extra.get("taken"))
                        for s_ in (loading, said, n.last)])
    assert loading.extra.get("taken") and _sent(r, mark) == [] and "raat://" in r.uri
    for st in (said, n.last):
        assert (st.state, st.extra.get("source"), st.extra.get("resume"), st.extra.get("taken")) == (
            "stopped", "external", spot, True)


def test_b_a_takeover_of_the_stopped_renderer_after_a_take_back_holds_nothing(tmp_path, proxy, renderers,
                                                                              nodes, quality):
    """The take-back hands the renderer our URI: the cut listen is ours
    again, the hold over. The node stops; another controller takes the
    stopped renderer: no listen was cut — no spot, nothing held."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("taken"), timeout=5, what="the hold")
    assert n.backend.play()
    wait_for(lambda: r.state == "PLAYING" and "raat://" not in r.uri and n.last.state == "playing",
             timeout=10, what="taken back")
    assert not n.last.extra.get("taken")
    assert n.backend.stop()
    wait_for(lambda: n.last.state == "stopped" and r.state == "STOPPED", timeout=5, what="stopped")
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("source") == "external", timeout=5, what="external")
    time.sleep(0.5)
    assert n.last.extra.get("resume") is None and not n.last.extra.get("taken")


def test_b_the_kanns_pause_under_raat_is_said_as_the_renderers(tmp_path, proxy, renderers, manager, heard, quality):
    """The KANN under Roon's RAAT says our URI paused at its second (B1): the
    node cannot see RAAT and keeps "paused", the renderer's word — said as
    the renderer's (`paused_on_device`: "Paused on ‹renderer›"), so the
    person knows it was not Sautium, and sending it nothing; the node's own
    Pause is said plainly. Play resumes our track: the same listen."""
    listened, lastfm = heard
    r = renderers()
    _on_renderer(manager, r, [track(tmp_path, 1, seconds=60.0), track(tmp_path, 2, seconds=60.0)])
    _playing_at(manager, r, 1)
    assert manager.backend().pause()
    wait_for(lambda: manager.latest_status.get("state") == "paused" and r.state == "PAUSED_PLAYBACK",
             timeout=5, what="our pause")
    ours = dict(manager.latest_status)
    assert manager.backend().play()
    _playing_at(manager, r, 3)
    mark = len(r.calls)
    r.taken_by_another("paused")
    wait_for(lambda: manager.latest_status.get("state") == "paused", timeout=5, what="the renderer's pause")
    theirs = dict(manager.latest_status)
    assert manager.backend().play()
    wait_for(lambda: r.state == "PLAYING" and manager.latest_status.get("state") == "playing", timeout=10,
             what="resumed")
    _observed("B-paused-on-device", ours=ours.get("paused_on_device"), theirs=theirs.get("paused_on_device"),
              node_sent=_sent(r, mark), listened=listened)
    assert not ours.get("paused_on_device") and theirs.get("paused_on_device") is True
    assert theirs.get("source") is None and _sent(r, mark) == ["Play"]
    assert not manager.latest_status.get("paused_on_device") and listened == []
    assert lastfm.count(("update_now_playing", "Track 1")) == 1


def _gated_serve(n, monkeypatch):
    """The node's stream for a load waits on the returned gate (a slow fetch)."""
    gate = threading.Event()
    ready = n.backend._serve_ready

    async def slow(item, url, *, front):
        await asyncio.get_running_loop().run_in_executor(None, gate.wait, 10)
        return await ready(item, url, front=front)
    monkeypatch.setattr(n.backend, "_serve_ready", slow)
    return gate


def _cut_at_slot_2(tmp_path, renderers, nodes, count=3):
    r = renderers()
    q = CanonicalQueue()
    tracks = [track(tmp_path, i, seconds=60.0) for i in range(1, count + 1)]
    q.replace(tracks)
    n = nodes(q, r)
    assert n.backend.select(2)
    wait_for(lambda: n.last.state == "playing" and n.last.queue_index == 2 and n.last.position >= 3,
             timeout=15, what="slot 2 playing")
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("taken") and (n.last.extra.get("resume") or {}).get("index") == 2,
             timeout=5, what="the spot")
    return r, n, q, tracks, dict(n.last.extra["resume"])


def test_b_a_stop_during_a_take_back_load_says_the_spot_where_its_track_stands_now(tmp_path, proxy, renderers,
                                                                                   nodes, quality, monkeypatch):
    """The spot is slot 2's track at its second; the person taps slot 3 (its
    stream readied slowly), removes slot 1 meanwhile, then presses Stop: the
    spot is the cut track's, at its slot as the queue stands — slot 1 now,
    never the number it had (Track 3's now) — and Play loads it there."""
    r, n, q, (t1, t2, t3), spot = _cut_at_slot_2(tmp_path, renderers, nodes)
    gate = _gated_serve(n, monkeypatch)
    assert n.backend.select(3)
    wait_for(lambda: n.last.state == "loading" and n.last.queue_index == 3, timeout=5, what="loading slot 3")
    assert q.remove(1) is t1
    n.backend.queue_changed("remove")
    wait_for(lambda: n.last.queue_index == 2, timeout=5, what="the load's slot following its track")
    assert n.backend.stop()
    said = n.last
    gate.set()
    time.sleep(1.5)
    assert (said.state, said.extra.get("source"), said.extra.get("resume")) == (
        "stopped", "external", {"index": 1, "position": spot["position"]})
    assert n.last.extra.get("resume") == said.extra["resume"] and "raat://" in r.uri
    assert n.backend.play()
    wait_for(lambda: r.state == "PLAYING" and "raat://" not in r.uri, timeout=10, what="taken back")
    assert "Track 2" in r.meta


def test_b_a_queue_edit_during_the_take_back_load_names_the_loaded_tracks_slot(tmp_path, proxy, renderers, nodes,
                                                                               quality, monkeypatch):
    """A queue edit while the take-back's load is on its way moves the slot it
    names with the loaded track — never back to the cut track's: once it
    plays, the status names the track the renderer plays."""
    r, n, q, tracks, spot = _cut_at_slot_2(tmp_path, renderers, nodes, count=4)
    gate = _gated_serve(n, monkeypatch)
    assert n.backend.select(4)
    wait_for(lambda: n.last.state == "loading" and n.last.queue_index == 4, timeout=5, what="loading slot 4")
    assert q.remove(1) is tracks[0]
    n.backend.queue_changed("remove")
    gate.set()
    wait_for(lambda: r.state == "PLAYING" and "raat://" not in r.uri and n.last.state == "playing",
             timeout=10, what="playing")
    time.sleep(1.0)
    assert "Track 4" in r.meta and q.item_at(n.last.queue_index) is tracks[3]


def test_b_a_new_queue_played_under_another_controller_leaves_the_old_spot_behind(tmp_path, proxy, renderers,
                                                                                  nodes, quality, monkeypatch):
    """Another controller cut slot 1 of the old queue at its second; the person
    starts a new queue (replace, play) and presses Stop while its first
    stream is readied: no spot is left — the old one went with its queue —
    and Play starts the new queue from its head, at 0."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    r.taken_by_another("playing")
    wait_for(lambda: (n.last.extra.get("resume") or {}).get("index") == 1, timeout=5, what="the spot")
    gate = _gated_serve(n, monkeypatch)
    n.queue.replace([track(tmp_path, 11, seconds=60.0), track(tmp_path, 12, seconds=60.0)])
    n.backend.queue_changed("replace", play=True)
    wait_for(lambda: n.last.state == "loading" and n.last.queue_index == 1, timeout=5, what="the new queue loading")
    assert n.backend.stop()
    said = n.last
    gate.set()
    time.sleep(1.5)
    assert said.extra.get("source") == "external" and said.extra.get("resume") is None
    mark = len(r.calls)
    assert n.backend.play()
    wait_for(lambda: r.state == "PLAYING" and "raat://" not in r.uri, timeout=10, what="the new queue")
    time.sleep(1.0)
    assert "Track 11" in r.meta and not any(a == "Seek" for a, _ in r.calls[mark:])


def test_b_a_take_back_whose_walk_runs_off_the_queue_says_the_takeovers_spot(tmp_path, proxy, renderers, nodes,
                                                                             quality, monkeypatch):
    """The spot at slot 1; the person taps slot 2, the last, whose stream
    fails: nothing of ours was handed over, so the status says the
    takeover's spot still — slot 1 at its second, with the failure said —
    never the failed slot; Play loads slot 1 there."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    r.taken_by_another("playing")
    wait_for(lambda: (n.last.extra.get("resume") or {}).get("index") == 1, timeout=5, what="the spot")
    spot = dict(n.last.extra["resume"])
    ready = n.backend._serve_ready

    async def fails(item, url, *, front):
        if item is n.queue.item_at(2):
            return None
        return await ready(item, url, front=front)
    monkeypatch.setattr(n.backend, "_serve_ready", fails)
    k = len(n.statuses)
    assert n.backend.select(2)
    wait_for(lambda: any(s_.state == "loading" for s_ in n.statuses[k:]) and not n.backend._loading
             and n.last.state != "loading", timeout=10, what="the failure")
    st = n.last
    assert (st.state, st.queue_index, st.extra.get("resume"), st.extra.get("source")) == (
        "stopped", 1, spot, "external") and st.extra.get("error")
    assert n.backend.play()
    wait_for(lambda: r.state == "PLAYING" and "raat://" not in r.uri, timeout=10, what="taken back")
    assert "Track 1" in r.meta


def test_b_a_stop_while_the_take_backs_hand_over_is_on_its_way_holds_it_paused_at_its_second(
        tmp_path, proxy, renderers, manager, heard, quality):
    """Play takes the renderer back at the spot; the person's Stop lands while
    our URI's hand-over is on its way: the renderer is ours then, never
    started — held paused at the spot's second, as a switch's spot is, and
    the listen goes on: Play loads it there, one row."""
    listened, lastfm = heard
    r = renderers()
    _on_renderer(manager, r, [track(tmp_path, 1, seconds=60.0), track(tmp_path, 2, seconds=60.0)])
    _playing_at(manager, r, 1)
    assert manager.backend().seek(20)
    _playing_at(manager, r, 21)
    r.taken_by_another("playing")
    wait_for(lambda: (manager.latest_status.get("resume") or {}).get("index") == 1, timeout=5, what="the spot")
    second = manager.latest_status["resume"]["position"]
    r.slow["SetAVTransportURI"] = 1.5
    calls = len(r.calls)
    assert manager.backend().play()
    wait_for(lambda: any(a == "SetAVTransportURI" for a, _ in r.calls[calls:]), timeout=5, what="the hand-over")
    assert manager.backend().stop()
    time.sleep(2.5)
    r.slow.pop("SetAVTransportURI", None)
    st = dict(manager.latest_status)
    assert (st.get("state"), st.get("position"), st.get("source")) == ("paused", second, None)
    assert listened == []
    assert manager.backend().play()
    wait_for(lambda: r.state == "PLAYING" and manager.latest_status.get("state") == "playing"
             and manager.latest_status.get("position", 0) >= second, timeout=10, what="played at its second")
    _playing_at(manager, r, second + 2)
    assert manager.backend().stop()
    wait_for(lambda: listened, timeout=10, what="the listen written")
    assert [t for t, _ in listened] == ["00000000-0000-0000-0000-000000000001"]
    assert lastfm.count(("update_now_playing", "Track 1")) == 1


def test_b_a_take_back_waiting_for_its_stream_ended_by_a_pause_leaves_its_spot_with_its_track(
        tmp_path, proxy, renderers, nodes, quality):
    """A take-back waiting for its stream (slot 2) ended by a Pause leaves its
    own spot, slot 2 at 0; a queue edit then moves that spot with its track —
    an insert before it: slot 3."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("resume") is not None, timeout=5, what="the spot")
    waiting = n.queue.item_at(2)
    waiting.play = {"kind": "pending"}
    assert n.backend.select(2)
    wait_for(lambda: n.last.state == "loading" and n.last.queue_index == 2, what="the take-back waiting")
    assert n.backend.pause()
    assert n.last.extra.get("resume") == {"index": 2, "position": 0.0}
    n.queue.insert_after(1, [track(tmp_path, 9, seconds=60.0)])
    n.backend.queue_changed("insert")
    wait_for(lambda: (n.last.extra.get("resume") or {}).get("index") == 3, timeout=5, what="the spot moved")
    assert n.queue.item_at(3) is waiting


def test_b_the_nodes_own_pause_on_a_slow_renderer_is_said_plainly(tmp_path, proxy, renderers, manager, heard,
                                                                    quality):
    """The node pauses a renderer that takes 1.5 s to carry the Pause out (the
    KANN's SOAP): a reading during that round trip still says it plays —
    the node's Pause is its own all the same, never said as the renderer's;
    and the node's Play after the renderer's own pause is said plainly
    while the renderer has yet to carry it out."""
    r = renderers()
    _on_renderer(manager, r, [track(tmp_path, 1, seconds=60.0), track(tmp_path, 2, seconds=60.0)])
    _playing_at(manager, r, 3)
    r.slow["Pause"] = 1.5
    assert manager.backend().pause()
    wait_for(lambda: manager.latest_status.get("state") == "paused" and r.state == "PAUSED_PLAYBACK",
             timeout=5, what="our pause")
    time.sleep(1.5)
    assert not manager.latest_status.get("paused_on_device")
    r.slow.pop("Pause", None)
    assert manager.backend().play()
    _playing_at(manager, r, 5)
    r.taken_by_another("paused")
    wait_for(lambda: manager.latest_status.get("paused_on_device"), timeout=5, what="the renderer's pause")
    said = []
    manager.subscribe_status(lambda data, item: said.append(dict(data)))
    r.slow["Play"] = 1.5
    assert manager.backend().play()
    wait_for(lambda: r.state == "PLAYING" and manager.latest_status.get("state") == "playing", timeout=10,
             what="ours again")
    assert not any(d.get("paused_on_device") for d in said)


def test_b_a_spot_held_after_the_take_backs_hand_over_follows_its_track_and_is_no_renderers_pause(
        tmp_path, proxy, renderers, manager, heard, quality):
    """A Stop while the take-back's hand-over is on its way holds our track
    paused at its second — the node's own spot, never said as the renderer's
    pause; a removal before it then moves its slot with its track, and Play
    loads that track there."""
    r = renderers()
    _on_renderer(manager, r, [track(tmp_path, i, seconds=60.0) for i in (1, 2, 3)])
    assert manager.backend().select(2)
    wait_for(lambda: manager.latest_status.get("state") == "playing"
             and manager.latest_status.get("track_index") == 2
             and manager.latest_status.get("position", 0) >= 3, timeout=15, what="slot 2 playing")
    r.taken_by_another("playing")
    wait_for(lambda: (manager.latest_status.get("resume") or {}).get("index") == 2, timeout=5, what="the spot")
    r.slow["SetAVTransportURI"] = 1.5
    calls = len(r.calls)
    assert manager.backend().play()
    wait_for(lambda: any(a == "SetAVTransportURI" for a, _ in r.calls[calls:]), timeout=5, what="the hand-over")
    assert manager.backend().stop()
    time.sleep(2.5)
    r.slow.pop("SetAVTransportURI", None)
    held = dict(manager.latest_status)
    assert held.get("state") == "paused" and not held.get("paused_on_device")
    assert manager.remove(1, "00000000-0000-0000-0000-000000000001") is True
    wait_for(lambda: manager.latest_status.get("track_index") == 1, timeout=5, what="its slot moved")
    assert manager.backend().play()
    wait_for(lambda: r.state == "PLAYING" and "raat://" not in r.uri, timeout=10, what="loaded")
    assert "Track 2" in r.meta


def test_b_our_own_uri_played_again_by_the_renderer_is_ours_again(tmp_path, proxy, renderers, nodes, quality):
    """Another controller loaded its URI on the renderer; later the renderer
    plays our last URI again by itself (its own controls resuming its UPnP
    media): ours again (ROON-COEX §6.1 P9) — the slot of ours playing, never
    "another app", and the node's Pause reaches it."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    ours, meta = r.uri, r.meta
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("source") == "external", timeout=5, what="external")
    r.uri, r.meta, r.state = ours, meta, "PLAYING"
    r._base, r._t0 = 10.0, time.monotonic()
    r._run(r._changed())
    wait_for(lambda: n.last.extra.get("source") != "external" and n.last.state == "playing"
             and n.last.queue_index == 1, timeout=5, what="ours again")
    mark = len(r.calls)
    assert n.backend.pause()
    wait_for(lambda: r.state == "PAUSED_PLAYBACK" and n.last.state == "paused", timeout=5, what="paused")
    assert "Pause" in _sent(r, mark)


def test_b_ours_again_after_a_takeover_the_poll_found_is_the_listen_it_held(tmp_path, proxy, renderers, manager,
                                                                            heard, quality):
    """Slot 1 plays at 36 s; another controller's URI is found by the
    position poll (its event lost), its stream 2 s in as the poll reads it;
    later the renderer's own controls resume our URI where it was, evented.
    Ours again from the takeover's spot — the poll's last reading was the
    other app's stream — so the listen held at the takeover goes on: one
    row, one now-playing; and the poll runs on it again: a jump the
    renderer makes by itself, no event said, is read. One slot: arming its
    next (none) changes nothing the renderer would event."""
    listened, lastfm = heard
    r = renderers()
    _on_renderer(manager, r, [track(tmp_path, 1, seconds=60.0)])
    _playing_at(manager, r, 1)
    assert manager.backend().seek(35)
    _playing_at(manager, r, 36)
    ours, meta = r.uri, r.meta
    r.notifying = False
    r.taken_by_another("playing")
    r._base = 2.0
    wait_for(lambda: manager.latest_status.get("source") == "external", timeout=6, what="external")
    time.sleep(1.0)
    assert listened == []
    r.notifying = True
    r.uri, r.meta, r.state = ours, meta, "PLAYING"
    r._base, r._t0 = 37.0, time.monotonic()
    r._run(r._changed())
    wait_for(lambda: manager.latest_status.get("source") != "external"
             and manager.latest_status.get("state") == "playing", timeout=5, what="ours again")
    first = manager.latest_status.get("position")
    r.notifying = False                                 # only the poll reads it
    r._base, r._t0 = 50.0, time.monotonic()
    wait_for(lambda: manager.latest_status.get("position", 0) >= 50, timeout=6, what="the poll's reading")
    r.notifying = True
    assert manager.backend().stop()
    wait_for(lambda: listened, timeout=10, what="the listen written")
    time.sleep(1.0)
    _observed("B-P9-poll", first_position=first, listened=listened, lastfm=lastfm)
    assert first >= 30
    assert [t for t, _ in listened] == ["00000000-0000-0000-0000-000000000001"], "the held listen split"
    assert lastfm.count(("update_now_playing", "Track 1")) == 1


def test_b_ours_again_runs_the_position_poll_itself(tmp_path, proxy, renderers, nodes, quality):
    """Ours again starts the position poll itself — never left to a second
    callback of the same event (the UPnP stack delivers one here; a
    renderer that events once would leave the node deaf on its own track:
    no RelTime, no track-end backstop)."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    ours = r.uri
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("source") == "external", timeout=5, what="external")
    b = n.backend

    async def again() -> bool:
        b._stop_poll()
        await asyncio.sleep(0)
        b._ours_again(ours, 1)
        return b._poll_task is not None and not b._poll_task.done()
    assert asyncio.run_coroutine_threadsafe(again(), b._loop).result(5)


def test_b_our_uri_played_again_while_a_take_back_waits_for_its_stream_waits_for_it(tmp_path, proxy, renderers,
                                                                                    nodes, quality):
    """The person taps slot 2 while another controller holds the renderer,
    slot 2's stream on its way (the take-back waits for it: loading); the
    renderer's own controls then play slot 1's URI of ours again. The tap is
    the newer word, as a take-back whose load is under way: nothing is ours
    again meanwhile, and once the stream lands the renderer plays slot 2."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    ours, meta = r.uri, r.meta
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("source") == "external", timeout=5, what="external")
    waiting = n.queue.item_at(2)
    waiting.play = {"kind": "pending"}
    assert n.backend.select(2)
    wait_for(lambda: n.last.state == "loading" and n.last.queue_index == 2, what="the take-back waiting")
    r.uri, r.meta, r.state = ours, meta, "PLAYING"
    r._base, r._t0 = 10.0, time.monotonic()
    r._run(r._changed())
    time.sleep(1.0)
    during = (n.last.state, n.last.queue_index)
    waiting.play = streamed(proxy, 2, cover_url="").source
    n.backend.slot_ready(2)
    wait_for(lambda: n.last.state == "playing" and n.last.queue_index == 2 and "stream2" in r.uri, timeout=10,
             what="slot 2 playing")
    assert during == ("loading", 2), "the person's take-back tap was swallowed"


def test_b_ours_again_at_another_slot_starts_from_that_streams_start(tmp_path, proxy, renderers, nodes, quality):
    """Slot 1 plays at 8 s, slot 2 armed; another controller takes the
    renderer (the spot: slot 1 at 8 s); the renderer then plays slot 2's URI
    by itself from its start: ours again at slot 2, from that stream's
    start — never slot 1's takeover second."""
    r = renderers()
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=60.0), track(tmp_path, 2, seconds=60.0)])
    n = nodes(q, r)
    assert n.backend.select(1)
    wait_for(lambda: n.last.state == "playing" and n.last.queue_index == 1 and n.last.position >= 8,
             timeout=20, what="slot 1 at 8 s")
    wait_for(lambda: r.next_uri, timeout=5, what="slot 2 armed")
    nxt, nxt_meta = r.next_uri, r.next_meta
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("source") == "external"
             and (n.last.extra.get("resume") or {}).get("position", 0) >= 8, timeout=5, what="the spot at slot 1")
    k = len(n.statuses)
    r.uri, r.meta, r.state = nxt, nxt_meta, "PLAYING"
    r._base, r._t0 = 0.0, time.monotonic()
    r._run(r._changed())
    wait_for(lambda: any(s.queue_index == 2 and s.extra.get("source") != "external" for s in n.statuses[k:]),
             timeout=5, what="ours again at slot 2")
    first = next(s for s in n.statuses[k:] if s.queue_index == 2 and s.extra.get("source") != "external")
    assert first.position < 3, f"slot 2 said at {first.position} s — slot 1's takeover second"


def test_b_our_uri_handed_before_a_quality_change_played_again_is_ours_again(tmp_path, proxy, renderers, nodes,
                                                                             quality):
    """Slot 1 handed lossless; the person switches the stream quality while
    another controller holds the renderer; the renderer then plays slot 1's
    URI again by itself. Its token names the queued track at that slot —
    only its tier differs from what the setting would hand now: ours again,
    and the node's Pause reaches it."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    ours, meta = r.uri, r.meta
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("source") == "external", timeout=5, what="external")
    quality["q"] = "opus_96"
    r.uri, r.meta, r.state = ours, meta, "PLAYING"
    r._base, r._t0 = 10.0, time.monotonic()
    r._run(r._changed())
    wait_for(lambda: n.last.extra.get("source") != "external" and n.last.queue_index == 1, timeout=5,
             what="ours again")
    mark = len(r.calls)
    assert n.backend.pause()
    wait_for(lambda: "Pause" in _sent(r, mark), timeout=5, what="the node's Pause sent")


def test_b_ours_again_arms_the_next(tmp_path, proxy, renderers, nodes, quality):
    """Ours again arms the slot after it, as any start of ours does — the
    renderer's own NextURI went with the takeover."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    wait_for(lambda: r.next_uri, timeout=5, what="slot 2 armed")
    nxt = r.next_uri
    ours, meta = r.uri, r.meta
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("source") == "external", timeout=5, what="external")
    assert r.next_uri == ""
    r.uri, r.meta, r.state = ours, meta, "PLAYING"
    r._base, r._t0 = 10.0, time.monotonic()
    r._run(r._changed())
    wait_for(lambda: r.next_uri == nxt, timeout=5, what="slot 2 armed again")


def test_b_an_opus_seeks_uri_played_again_is_ours_again_in_the_tracks_time(tmp_path, proxy, renderers, nodes,
                                                                           quality, stored):
    """A URI of ours from an Opus seek's reload (?ss=) played again: ours
    again at its slot, its clock in the track's time — the stream's offset
    plus its RelTime."""
    quality["q"] = "opus_96"
    r = renderers()
    r.opus_position = False                      # no RelTime in Opus: the Seek reloads from ?ss=
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=60.0), track(tmp_path, 2, seconds=60.0)])
    n = nodes(q, r)
    assert n.backend.select(1)
    wait_for(lambda: n.last.state == "playing" and n.last.queue_index == 1, timeout=15, what="slot 1 playing")
    assert n.backend.seek(20)
    wait_for(lambda: "ss=20" in r.uri and r.state == "PLAYING" and n.last.state == "playing", timeout=15,
             what="reloaded from ?ss=20")
    ours, meta = r.uri, r.meta
    r.opus_position = True                       # from here its RelTime runs, 0-based on the ?ss= stream
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("source") == "external", timeout=5, what="external")
    k = len(n.statuses)
    r.uri, r.meta, r.state = ours, meta, "PLAYING"
    r._base, r._t0 = 5.0, time.monotonic()      # 5 s into the stream from 20 s: the track's 25 s
    r._run(r._changed())
    wait_for(lambda: any(s.extra.get("source") != "external" and s.queue_index == 1 for s in n.statuses[k:]),
             timeout=5, what="ours again")
    wait_for(lambda: n.last.position >= 24, timeout=6, what="the track's time")


def test_b_a_track_queued_twice_is_ours_again_at_the_slot_the_takeover_cut(tmp_path, proxy, renderers, nodes,
                                                                            quality):
    """Track 1 at slots 1 and 3; slot 3 plays at 8 s; another controller
    takes the renderer (the spot: slot 3 at 8 s); the renderer's own
    controls then play that URI of ours again where it was. Ours again is
    the takeover's slot, from its second — never slot 1 from 0 (the held
    listen split, the queue walking on from slot 1)."""
    r = renderers()
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=60.0), track(tmp_path, 2, seconds=60.0), track(tmp_path, 1, seconds=60.0)])
    n = nodes(q, r)
    assert n.backend.select(3)
    wait_for(lambda: n.last.state == "playing" and n.last.queue_index == 3 and n.last.position >= 8,
             timeout=20, what="slot 3 at 8 s")
    ours, meta = r.uri, r.meta
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("source") == "external"
             and (n.last.extra.get("resume") or {}).get("index") == 3, timeout=5, what="the spot at slot 3")
    at = n.last.extra["resume"]["position"]
    k = len(n.statuses)
    r.uri, r.meta, r.state = ours, meta, "PLAYING"
    r._base, r._t0 = at + 1.0, time.monotonic()
    r._run(r._changed())
    wait_for(lambda: any(s.extra.get("source") != "external" and s.state == "playing" for s in n.statuses[k:]),
             timeout=5, what="ours again")
    first = next(s for s in n.statuses[k:] if s.extra.get("source") != "external" and s.state == "playing")
    assert first.queue_index == 3, f"ours again at slot {first.queue_index}"
    assert first.position >= at - 1, f"from {first.position} s, the takeover at {at} s"


def test_b_the_next_it_had_armed_queued_twice_is_ours_again_at_that_slot(tmp_path, proxy, renderers, nodes,
                                                                         quality):
    """Track 1 at slots 1 and 3; slot 2 plays, slot 3 armed; another
    controller takes the renderer, which then plays the URI it had armed
    (Track 1's token) by itself from its start: ours again at slot 3, the
    next it had armed — never slot 1."""
    r = renderers()
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=60.0), track(tmp_path, 2, seconds=60.0), track(tmp_path, 1, seconds=60.0)])
    n = nodes(q, r)
    assert n.backend.select(2)
    wait_for(lambda: n.last.state == "playing" and n.last.queue_index == 2 and n.last.position >= 3,
             timeout=20, what="slot 2 playing")
    wait_for(lambda: r.next_uri, timeout=5, what="slot 3 armed")
    nxt, nxt_meta = r.next_uri, r.next_meta
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("source") == "external", timeout=5, what="external")
    k = len(n.statuses)
    r.uri, r.meta, r.state = nxt, nxt_meta, "PLAYING"
    r._base, r._t0 = 0.0, time.monotonic()
    r._run(r._changed())
    wait_for(lambda: any(s.extra.get("source") != "external" and s.state == "playing" for s in n.statuses[k:]),
             timeout=5, what="ours again")
    first = next(s for s in n.statuses[k:] if s.extra.get("source") != "external" and s.state == "playing")
    assert first.queue_index == 3, f"ours again at slot {first.queue_index}"


def test_b_ours_again_on_a_uri_of_another_tier_follows_its_track_through_a_queue_edit(tmp_path, proxy, renderers,
                                                                                      nodes, quality):
    """Ours again on a URI handed before a quality change; a track is then
    inserted before it: the slot follows its track (Track 1 at slot 2),
    every lookup by its token — never the inserted track's slot, from which
    the next armed would be Track 1 itself."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    ours, meta = r.uri, r.meta
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("source") == "external", timeout=5, what="external")
    quality["q"] = "opus_96"
    r.uri, r.meta, r.state = ours, meta, "PLAYING"
    r._base, r._t0 = 10.0, time.monotonic()
    r._run(r._changed())
    wait_for(lambda: n.last.extra.get("source") != "external" and n.last.queue_index == 1, timeout=5,
             what="ours again")
    n.queue.insert_after(0, [track(tmp_path, 3, seconds=60.0)])
    n.backend.queue_changed("insert")
    wait_for(lambda: n.last.queue_index == 2, timeout=5, what="the slot following its track")


def test_b_a_removed_tracks_uri_played_again_stays_another_apps(tmp_path, proxy, renderers, nodes, quality):
    """Another controller cut slot 1 (Track 1); the person removes Track 1
    from the queue meanwhile; then the renderer plays Track 1's URI again by
    itself. It names no slot of the queue: the playback stays the other
    app's — never said as the queued track now at that slot (a listen and a
    scrobble of a track that did not sound)."""
    r = renderers()
    q = CanonicalQueue()
    tracks = [track(tmp_path, i, seconds=60.0) for i in (1, 2, 3)]
    q.replace(tracks)
    n = nodes(q, r)
    assert n.backend.select(1)
    wait_for(lambda: n.last.state == "playing" and n.last.queue_index == 1 and n.last.position >= 3,
             timeout=15, what="slot 1 playing")
    ours, meta = r.uri, r.meta
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("source") == "external", timeout=5, what="external")
    assert q.remove(1) is tracks[0]
    n.backend.queue_changed("remove")
    time.sleep(0.5)
    r.uri, r.meta, r.state = ours, meta, "PLAYING"
    r._base, r._t0 = 10.0, time.monotonic()
    r._run(r._changed())
    time.sleep(2.0)
    last = n.last
    _observed("B-P9-removed", state=last.state, queue_index=last.queue_index, source=last.extra.get("source"))
    assert last.extra.get("source") == "external" and last.queue_index == 0


def test_b_our_own_uri_read_paused_while_another_app_holds_the_renderer_stays_its(tmp_path, proxy, renderers,
                                                                                  nodes, quality):
    """A URI of ours the renderer names PAUSED while another controller holds
    it is no word of its own controls — a renderer names the URI before ours
    for a tick after a load and can sit paused on it: only playing it again
    makes it ours (P9)."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    ours, meta = r.uri, r.meta
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("source") == "external", timeout=5, what="external")
    r.uri, r.meta, r.state = ours, meta, "PAUSED_PLAYBACK"
    r._base, r._t0 = 10.0, None
    r._run(r._changed())
    time.sleep(1.5)
    assert n.last.extra.get("source") == "external"


def test_b_our_own_uri_played_again_while_a_take_back_loads_waits_for_that_load(tmp_path, proxy, renderers, nodes,
                                                                               quality):
    """The person tapped slot 2 while another controller held the renderer:
    that take-back waits for slot 2's stream (loading). The renderer's own
    controls then play slot 1's URI again: the load under way is the newer
    word — nothing is ours again meanwhile — and once the stream lands the
    renderer plays slot 2, said as slot 2, never slot 1's track over it."""
    r, n = _renderer_plays_slot_1(tmp_path, renderers, nodes)
    ours, meta = r.uri, r.meta
    r.taken_by_another("playing")
    wait_for(lambda: n.last.extra.get("source") == "external", timeout=5, what="external")
    n.queue.item_at(2).play = streamed(proxy, 2, cover_url="").source
    proxy._entries["stream2"].ready.clear()
    assert n.backend.select(2)
    wait_for(lambda: n.last.state == "loading" and n.last.queue_index == 2, timeout=5, what="the take-back loading")
    r.uri, r.meta, r.state = ours, meta, "PLAYING"
    r._base, r._t0 = 10.0, time.monotonic()
    r._run(r._changed())
    time.sleep(1.0)
    during = (n.last.state, n.last.queue_index)
    proxy._entries["stream2"].ready.set()
    wait_for(lambda: n.last.state == "playing" and "stream2" in r.uri, timeout=10, what="slot 2 playing")
    time.sleep(1.0)
    _observed("B-P9-load", during=during, after=(n.last.state, n.last.queue_index))
    assert during == ("loading", 2) and n.last.queue_index == 2
