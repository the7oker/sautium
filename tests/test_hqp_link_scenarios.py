"""
The HQPlayer links under random interleavings.

Two fake HQPlayers, A and B, and a manager driving A. People's intents,
HQPlayer's own events and output changes are drawn from a seed — some on
threads of their own, as in production, with small random delays between
them — and after every step the invariants are checked on one event log:
the links' requests, intents and builds, the fakes' wire, the waits, the
statuses the output pushed. The node stop ends every run.

  1. At most one request of this node is outstanding at an HQPlayer;
     nothing goes out while it builds, only questions once a build ran out.
  2. A request goes only on its own link: what was made for one HQPlayer
     never reaches another, and of what began after a link's retire only
     the Stop it owes goes.
  3. An item is never sent once a newer intent of a kind that supersedes it
     was accepted (hqp_link.SUPERSEDED_BY).
  4. Waiting out a build holds neither the command lock nor the queue lock.
  5. The node stop is bounded: everything waiting on HQPlayer is let go
     within 2 s, and nothing goes out after it.
  6. The Stop a switch away sends reaches only an HQPlayer that plays or
     pauses an entry of ours.
  7. A build's end is never lost: a wait returns as soon as it is told.
  8. An item made for one queue is never sent once another queue plays.
  9. Statuses: "unfinished" only after a build ran out — never after a
     broken held answer — "disconnected" only after a miss that was no
     silence, "not answering" never together with a build.
 10. The resume spot names a queue item and stays with it through the queue
     edits that keep its item.
 11. Nothing a filler, a "play next" or a replace took is lost: a batch is
     taken whole — what HQPlayer refused of it a gap — or, refused whole,
     not at all; at rest it is in the queue, unless a newer queue replaced
     it or a person removed it.
 12. A replace rotates listening history only once the output took it:
     once when it did, never when it was refused.
 13. At rest HQPlayer's playlist is the queue without its gaps, in the same
     order, or the mirror is marked lost (or drifted: an edit made
     elsewhere) — and a play press then makes it so: taken unless HQPlayer
     is away, silent or building, and the track it starts — the slot
     pressed, or the next HQPlayer holds — plays. No item twice in the
     queue. A gap is a slot that HQPlayer cannot open (a file of the other
     HQPlayer's library) or one whose file it refused, and nothing else.
 14. A tail mirror plays the queue's tracks from the entry it reads to the
     end.
 15. The order of intents is the order of the asks: an intent is numbered
     as its sequence is asked — never once it holds the command lock — checked
     as each number a sequence publishes while it holds it is published (I3
     reads intents so). That it holds from
     the moment it has the lock, and that a new queue's or a rebuild's
     number, pending from its ask, is withdrawn when the lock refuses it, the
     clock's own tests pin. A sequence's ask and its taking the lock are
     spread apart at random.

The Stop a switch away owes (6) is also looked for: it went, or a newer
attach superseded it, or HQPlayer stayed silent past the limit, went away,
or the node stopped.

A failure names its seed and prints the whole event log.

    SAUTIUM_SCENARIO_SEEDS=40 pytest tests/test_hqp_link_scenarios.py   # a wider run
    SAUTIUM_SCENARIO_SEED=1234 pytest tests/test_hqp_link_scenarios.py  # one seed again

Time is scaled down — an answer is late after 0.15 s, a build unfinished after
1 s — so the regular run stays under a minute; what depends on the shipped
values has a test of its own in test_hqp_backend.py.
"""

import os
import random
import threading
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import pytest
from fastapi import HTTPException

# The harness and its fixtures — importing it puts backend/ on the path
from test_hqp_backend import (FakeHqp, _held_by, _item, _picker,  # noqa: F401
                              _queue_stays_in_memory, _wait, _ways_in_by_library, fake,
                              fake_b, listens)

from hqplayer_client import PlaybackState  # noqa: E402
from playback import hqp_backend as hb  # noqa: E402
from playback import hqp_link as hl  # noqa: E402
from playback import manager as manager_mod  # noqa: E402
from playback.base import OutputRefused  # noqa: E402
from playback.hqp_backend import HqpBackend  # noqa: E402
from playback.manager import PlaybackManager, QueueRefused  # noqa: E402
from playback.queue import QueueItem  # noqa: E402

_QUESTIONS = {"Status", "State", "GetInfo", "PlaylistGet", "VolumeRange", "GetModes",
              "GetFilters", "GetShapers", "GetRates", "MatrixListProfiles", "MatrixGetProfile",
              "raw"}
_WORKERS = ("hqp-dsp-change", "hqp-late-stop", "hqp-start", "hqp-rebind",
            "hqp-benchmark-recover", "hqp-reconverge", "hqp-press", "hqp-held-status")
# The queue edits a spot stays through, its item kept
_KEEP_SPOT = ("fill", "play next", "remove", "reorder", "rebind")


def _seeds() -> list:
    one = os.environ.get("SAUTIUM_SCENARIO_SEED")
    if one:
        return [int(one)]
    return list(range(1, 1 + int(os.environ.get("SAUTIUM_SCENARIO_SEEDS", "4"))))


class _TrackedRLock:
    """The manager's queue lock, with its owner known (invariant 4)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.owner: Optional[int] = None
        self._depth = 0

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        ok = self._lock.acquire(blocking, timeout)
        if ok:
            self.owner = threading.get_ident()
            self._depth += 1
        return ok

    def release(self) -> None:
        self._depth -= 1
        if not self._depth:
            self.owner = None
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc) -> bool:
        self.release()
        return False


class _Elsewhere:
    """The output switched to: it takes every command and plays nothing."""
    id, label = "browser", "elsewhere"
    holds_words = False

    def __init__(self, emit, queue) -> None:
        self._gone = False

    def start(self) -> None: ...
    def shutdown(self) -> None: ...
    def resume_at(self, index: int) -> None: ...
    def lose_mirror(self, why: str) -> None: ...

    def release(self) -> None:
        return None
    def queue_changed(self, kind: str, *, play: bool = False) -> None: ...
    def queue_start(self) -> None: ...
    def slot_ready(self, index: int) -> None: ...

    @contextmanager
    def mutation(self):
        yield

    def healthy(self) -> bool:
        return True

    def reachable(self) -> bool:
        return True

    def refusal(self) -> str:
        return ""

    def pressing(self) -> None:
        return None

    def queue_replace(self, items, *, play, probe_first=False) -> list:
        return list(items)

    def queue_append(self, items) -> list:
        return list(items)

    def queue_insert_next(self, items, anchor_index) -> tuple:
        return list(items), anchor_index

    def queue_remove(self, index) -> bool:
        return True

    def queue_clear_after_current(self, archive) -> bool:
        archive()
        return True

    def queue_abandon(self) -> None: ...

    def queue_reorder(self, plan) -> dict:
        return {"removed": 0, "added": 0, "interrupted": False}

    def __getattr__(self, name):
        if name in ("play", "pause", "stop", "next", "previous", "select", "seek",
                    "set_volume", "volume_up", "volume_down"):
            return lambda *a: True
        raise AttributeError(name)


@dataclass
class _Event:
    seq: int
    t: float
    thread: str
    kind: str
    data: dict = field(default_factory=dict)
    # Which thread: step threads of one kind share a name
    tid: int = 0

    def __str__(self) -> str:
        fields = " ".join(f"{k}={v}" for k, v in self.data.items())
        return f"{self.seq:5d} {self.t:9.3f} {self.thread:<22.22} {self.kind:<14} {fields}"


class World:
    """Everything one seed's run touches, and its one event log."""

    def __init__(self, seed: int, a: FakeHqp, b: FakeHqp, monkeypatch) -> None:
        self.seed = seed
        self.rng = random.Random(seed)
        self.a, self.b = a, b
        a.name, b.name = "A", "B"
        self.mp = monkeypatch
        self.log: list[_Event] = []
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self.threads: list[threading.Thread] = []
        self.violations: list[str] = []
        self.unexpected: list[str] = []
        self.checked = 0
        self.foreign = 0
        # The tail step whose outcome the next settle judges at rest — its
        # step's number, when no other step ran beside it — and the newest
        # step begun: judged as right after it only while none began since
        self.rest_due: Optional[int] = None
        self.last_step = -1
        # How long a full settle waits for quiet
        self.settle_limit = 15.0
        self.next_id = 100
        self._links = 0
        self.fills: list = []           # (queue generation, the items a filler took)
        # (queue generation, a batch the queue took whole): a replace, a play next
        self.batches: list = []
        self.removed: set = set()       # id() of the items people removed
        self.refused: set = set()       # the paths an HQPlayer was made to refuse for good
        self.mgr: Optional[PlaybackManager] = None
        self.settings_router = None
        self.owed_checked = 0

    # -- the log ------------------------------------------------------------

    def rec(self, kind: str, **data) -> int:
        with self._lock:
            n = len(self.log)
            self.log.append(_Event(n, time.monotonic() - self._t0,
                                   threading.current_thread().name, kind, data,
                                   threading.get_ident()))
            return n

    def violate(self, what: str) -> None:
        self.violations.append(what)
        self.rec("VIOLATION", what=what)

    def dump(self) -> str:
        with self._lock:
            lines = [str(e) for e in self.log]
        return "\n".join([f"seed {self.seed} — event log:"] + lines)

    # -- the instruments -------------------------------------------------------

    def lid(self, link) -> str:
        """A link's name in the log — its HQPlayer, then which of the links
        that HQPlayer had: a closed link is made afresh, its clock from 1."""
        name = getattr(link, "scenario_id", None)
        if name is None:
            self._links += 1
            name = link.scenario_id = (
                {self.a.port: "A", self.b.port: "B"}.get(link.port, "?") + str(self._links))
        return name

    def instrument(self) -> None:
        mp, w = self.mp, self
        lid = self.lid

        def on_wire(hqp, conn, what, command):
            entry = (hqp.playlist[hqp.track - 1]
                     if 1 <= hqp.track <= len(hqp.playlist) else None)
            w.rec("wire", fake=hqp.name, conn=conn, what=what, command=command,
                  state=hqp.state, entry=entry)
        for f in (self.a, self.b):
            f.on_wire = on_wire

        # The intent clock: when a number holds (published), pends (a new
        # queue's or a rebuild's, from its ask) or is withdrawn — a withdrawal
        # logged before it takes effect, every other change after, so I3
        # never reads a supersession the link did not apply
        publish, ask_early, withdraw = (hl.HqpLink.publish, hl.HqpLink.ask_early,
                                        hl.HqpLink.withdraw)
        asked: dict = {}

        def published(link, kind, n):
            publish(link, kind, n)
            # I15: every number a sequence publishes while it holds the
            # command lock was taken as it was asked — before it asked for
            # the lock — never once it holds it
            before = asked.get(threading.get_ident())
            if before is not None and n > before:
                w.violate(f"I15: {kind} #{n} numbered once it held the command lock, "
                          f"not as it was asked (the clock stood at {before})")
            w.rec("accept", link=lid(link), intent=kind, n=n)
        mp.setattr(hl.HqpLink, "publish", published)

        def pended(link, kind):
            n = ask_early(link, kind)
            w.rec("pend", link=lid(link), intent=kind, n=n)
            return n
        mp.setattr(hl.HqpLink, "ask_early", pended)

        def withdrawn(link, n):
            w.rec("withdraw", link=lid(link), n=n)
            withdraw(link, n)
        mp.setattr(hl.HqpLink, "withdraw", withdrawn)

        # A sequence's ask and its taking the command lock, spread apart at
        # random: the clock is read as it asks for the lock
        enter, leave = hl._CommandLock.__enter__, hl._CommandLock.__exit__
        spread = random.Random(self.seed)

        def entering(lock):
            if spread.random() < 0.25:
                time.sleep(spread.uniform(0.0, 0.08))
            before = lock._link._clock
            out = enter(lock)
            asked[threading.get_ident()] = before
            return out

        def leaving(lock, *exc):
            asked.pop(threading.get_ident(), None)
            return leave(lock, *exc)
        mp.setattr(hl._CommandLock, "__enter__", entering)
        mp.setattr(hl._CommandLock, "__exit__", leaving)

        make = hl.HqpLink.item

        def made(link, kind, n, **kw):
            item = make(link, kind, n, **kw)
            item.made_on = lid(link)
            if kind in ("start", "resume"):
                w.rec("accept", link=lid(link), intent="start", n=n, made=kind)
            return item
        mp.setattr(hl.HqpLink, "item", made)

        request = hl.HqpLink.request

        @contextmanager
        def logged(link, command):
            item = getattr(link._local, "item", None)
            gen = item.queue.generation if item is not None and item.queue is not None else None
            begun = w.rec("request", link=lid(link), command=command,
                          item=None if item is None else f"{item.kind}#{item.n}",
                          made_on=getattr(item, "made_on", None),
                          generation=None if item is None else item.generation, queue=gen)
            with request(link, command):
                w.rec("send", link=lid(link), command=command, begun=begun)
                try:
                    yield
                finally:
                    w.rec("answered", link=lid(link), command=command, begun=begun)
        mp.setattr(hl.HqpLink, "request", logged)

        begin, end, answered = hl.BuildWatch.begin, hl.BuildWatch._end, hl.BuildWatch.answered

        def began(watch, command, attributes):
            ends = begin(watch, command, attributes)
            w.rec("build-begin", link=lid(watch._link), command=command, n=watch._builds)
            return ends

        def ended(watch, n, how):
            w.rec("build-end", link=lid(watch._link), n=n, how=how)
            end(watch, n, how)

        def cleared(watch):
            was = watch.state
            answered(watch)
            if was is not None and watch.state is None:
                w.rec("build-answered", link=lid(watch._link))
        mp.setattr(hl.BuildWatch, "begin", began)
        mp.setattr(hl.BuildWatch, "_end", ended)
        mp.setattr(hl.BuildWatch, "answered", cleared)

        wait_out = hl.BuildWatch.wait_out

        def waited(watch, until=lambda: False):
            me = threading.get_ident()
            link = watch._link
            if link.lock.owner == me:
                w.violate("I4: a wait for the build's end holds the command lock")
            if w.mgr is not None and w.mgr._mutate_lock.owner == me:
                w.violate("I4: a wait for the build's end holds the queue lock")
            entered = w.rec("wait", link=lid(link), build=watch._builds, ended=watch._ended[0])
            try:
                out = wait_out(watch, until)
            finally:
                w.rec("waited", link=lid(link), entered=entered)
            return out
        mp.setattr(hl.BuildWatch, "wait_out", waited)

        # A switch away from HQPlayer, as the manager makes it — whatever the
        # backend then does about the Stop it owes
        activate = PlaybackManager.activate

        def activated(mgr, output_type, *, stop_old=True, by_hold=False, **cfg):
            old = mgr._active
            if output_type is None and stop_old and not by_hold and isinstance(old, HqpBackend):
                link = old._link
                f = w.a if link.port == w.a.port else w.b
                with f._lock:
                    entry = f.playlist[f.track - 1] if 1 <= f.track <= len(f.playlist) else None
                    state = f.state
                built = link.build.silent()
                w.rec("released", link=lid(link), fake=f.name, state=state, entry=entry,
                      given=[old._uri_of(it)[0] for it in old._layout().handed],
                      unfinished=bool(built and built.get("unfinished")))
            return activate(mgr, output_type, stop_old=stop_old, by_hold=by_hold, **cfg)
        mp.setattr(PlaybackManager, "activate", activated)

        for name in ("retire", "close"):
            orig = getattr(hl.HqpLink, name)

            def life(link, why, _orig=orig, _name=name):
                _orig(link, why)        # logged once it took effect
                w.rec(_name, link=lid(link), why=why, state=link.state)
            mp.setattr(hl.HqpLink, name, life)
        unretire = hl.HqpLink.unretire

        def chosen_again(link):
            unretire(link)
            w.rec("unretire", link=lid(link), state=link.state)
        mp.setattr(hl.HqpLink, "unretire", chosen_again)

        def thread_failed(args):
            w.unexpected.append(f"{args.thread.name if args.thread else '?'}: "
                                + "".join(traceback.format_exception(
                                    args.exc_type, args.exc_value, args.exc_traceback)))
        mp.setattr(threading, "excepthook", thread_failed)

        heard = PlaybackManager._on_backend_status

        def emitted(manager, backend, status):
            if isinstance(backend, HqpBackend):
                x = status.extra or {}
                b = x.get("building") or {}
                w.rec("emit", link=lid(backend._link), state=status.state,
                      building=("unfinished" if b.get("unfinished") else "silent"
                                if b.get("silent") else "answering" if b else None),
                      not_answering=bool(x.get("not_answering")))
            heard(manager, backend, status)
        mp.setattr(PlaybackManager, "_on_backend_status", emitted)

        # A drift the canary marks is judged by the playlist it read then —
        # HQPlayer may have dropped it since (a vanish, a restart)
        judge, judging = HqpBackend._judge_mirror, threading.local()

        def judged(backend, playlist, status, **kw):
            judging.playlist, judging.status = playlist, status
            try:
                return judge(backend, playlist, status, **kw)
            finally:
                judging.playlist = judging.status = None
        mp.setattr(HqpBackend, "_judge_mirror", judged)
        drifted = HqpBackend._drifted

        def marked(backend, why, **kw):
            read = getattr(judging, "playlist", None)
            judging.why = why
            if read is not None and backend._mirror == "tail":
                # A tail mirror holds entries of anyone's before the one
                # HQPlayer reads: only one at or behind it is an edit made
                # elsewhere since
                read = read[max(judging.status.track_index, 1) - 1:]
            judging.foreign = (None if read is None else
                               any("/Elsewhere/" in (t.get("uri") or "") for t in read))
            try:
                return drifted(backend, why, **kw)
            finally:
                judging.why = judging.foreign = None
        mp.setattr(HqpBackend, "_drifted", marked)

        # Every move of the mirror, with what made it: a drift the canary
        # marks names its reason and the playlist it read; any other move —
        # a press's own drift among them — is the node's own
        move = HqpBackend._move_mirror

        def moved(backend, state, **kw):
            done = move(backend, state, **kw)
            if done:
                w.rec("mirror", link=lid(backend._link), state=state, by=kw.get("by"),
                      why=getattr(judging, "why", None),
                      foreign=getattr(judging, "foreign", None))
            return done
        mp.setattr(HqpBackend, "_move_mirror", moved)

        miss = HqpBackend._register_failure

        def missed(backend, *, silent):
            w.rec("miss", link=lid(backend._link), silent=silent)
            miss(backend, silent=silent)
        mp.setattr(HqpBackend, "_register_failure", missed)

    # -- the world's set-up ----------------------------------------------------------

    def scale_time(self) -> None:
        mp = self.mp
        mp.setattr(hl.BuildWatch, "after", 0.15)
        mp.setattr(hl, "BUILD_LIMIT_S", 1.0)
        mp.setattr(hl, "COMMAND_WAIT_S", 0.6)
        mp.setattr(hl, "STOP_ANSWER_S", 0.8)
        mp.setattr(hl, "QUESTION_WAIT_S", 0.4)
        mp.setattr(hb, "QUESTION_WAIT_S", 0.4)
        mp.setattr(hl, "PROBE_WAIT_S", 0.4)
        mp.setattr(hl, "CIRCUIT_COOLDOWN_S", 0.2)
        mp.setattr(hb, "STATUS_FAILURE_THRESHOLD", 2)
        mp.setattr(hb, "_poll_interval",
                   lambda failures: 0.05 if failures <= 0 else min(0.1 * 2 ** min(failures, 3), 0.4))

    def start(self) -> None:
        from routers import player as player_router
        self.settings_router = _picker(self.mp, self.a)
        mgr = self.mgr = PlaybackManager()
        mgr._mutate_lock = _TrackedRLock()
        mgr.queue.replace([_item(f"E:/Music/A/0{i}.flac", i) for i in range(1, 5)])
        self.mp.setattr(manager_mod, "manager", mgr)
        self.mp.setattr(player_router, "manager", mgr)
        # The output a switch goes to: no browser, no media URLs
        from playback import browser_backend
        self.mp.setattr(browser_backend, "BrowserBackend", _Elsewhere)
        # A playlist adopted back at an attach resolves its files in the
        # database: here every entry comes back as a pass-through
        from playback import queue as queue_mod
        self.mp.setattr(queue_mod, "items_for_file_spans", lambda spans: {})
        self.mp.setattr(queue_mod, "items_for_hqp_paths", lambda paths, endpoint_id: {})
        # A file that left the library: the manager's own re-bind, its
        # database read replaced (rebind)
        self.mp.setattr(queue_mod, "rebind_origins", self._rebind_origins)
        # Each HQPlayer has a library of its own: A holds the H/ files, which
        # B cannot open — a slot of A's library is a gap on B
        import hqp_library
        from playback import substitute
        self.mp.setattr(hqp_library, "endpoint_by_address",
                        lambda host, port: {"id": 7 if port == self.a.port else 8, "name": "fake"})
        self.mp.setattr(substitute, "native_plays", _ways_in_by_library)
        self.b.refuse.add("Library/H/")
        self.player = player_router
        self.pick(self.a)

    def pick(self, hqp: FakeHqp) -> None:
        self.settings_router.put_output_prefs(self.settings_router.OutputPrefs(
            type="hqplayer", hqplayer={"host": "127.0.0.1", "port": hqp.port}))

    # -- the steps ----------------------------------------------------------------------

    def items(self, n: int) -> list:
        """New tracks — some held in A's library (a gap on B), now and then
        one whose file the HQPlayer driven refuses from now on."""
        made = []
        for _ in range(n):
            self.next_id += 1
            if self.rng.random() < 0.2:
                made.append(_held_by(f"D:/Library/H/{self.next_id}.flac", self.next_id, 7))
                continue
            path = f"E:/Music/S/{self.next_id}.flac"
            if self.rng.random() < 0.1:
                self.refused.add(path)
                self.driven().refuse.add(path)
            made.append(_item(path, self.next_id))
        return made

    def hqp(self) -> FakeHqp:
        """The HQPlayer the next HQPlayer event happens to: mostly the one
        the output drives."""
        if self.rng.random() < 0.8:
            return self.driven()
        return self.a if self.rng.random() < 0.5 else self.b

    def driven(self) -> FakeHqp:
        b = self.mgr.active
        link = getattr(b, "_link", None)
        return self.b if link is not None and link.port == self.b.port else self.a

    def build(self, command: str, *, answer: Optional[str] = None) -> None:
        """The next `command` sets off a build on the driven HQPlayer — some
        past the limit, some leaving it stopped."""
        r = self.rng
        self.driven().build(command, r.uniform(0.2, 1.6), answer=answer or r.choice(["first", "last"]),
                            stops=r.random() < 0.3)

    def steps(self) -> list:
        r, p = self.rng, self.player
        mgr = self.mgr
        return [
            (6, "play", lambda: p._transport(True, "play")),
            (3, "pause", lambda: p._transport(False, "pause")),
            (3, "stop", lambda: p._transport(False, "stop")),
            (3, "next", lambda: p._transport(True, "next")),
            (2, "previous", lambda: p._transport(True, "previous")),
            (3, "jump", lambda: p.jump(p.JumpRequest(index=r.randint(1, max(len(mgr.queue), 1))))),
            (2, "seek", lambda: p._transport(False, "seek", r.randint(0, 200))),
            (2, "volume", lambda: p._transport(False, "set_volume", float(-r.randint(3, 40)))),
            (4, "replace", self.replace),
            (3, "fill", self.fill),
            (2, "play next", self.play_next),
            (2, "remove", self.remove),
            (2, "reorder", self.reorder),
            (2, "radio", lambda: mgr.clear_for_radio(lambda: self.rec("archive"))),
            (3, "dsp", self.dsp),
            (4, "dsp builds", lambda: (self.build(r.choice(["SetFilter", "SetShaping"])),
                                       self.dsp())),
            (3, "play builds", lambda: (self.build("Play", answer="last"),
                                        p._transport(True, "play"))),
            (2, "jump builds", lambda: (self.build("SelectTrack", answer="first"),
                                        p.jump(p.JumpRequest(index=r.randint(
                                            1, max(len(mgr.queue), 1)))))),
            (2, "build next", self.arm_build),
            (3, "dsp, then a switch", self.dsp_then_switch),
            (3, "fill through a build", self.fill_through_build),
            (3, "jump, then a switch", self.jump_then_switch),
            (2, "silence", lambda: self.hqp().silence(r.uniform(0.2, 1.3))),
            (1, "restart", self.restart),
            (1, "vanish", self.vanish),
            (2, "theirs", self.theirs),
            (2, "an entry before", self.entry_before),
            (2, "switch away", lambda: self.settings_router.put_output_prefs(
                self.settings_router.OutputPrefs(type="browser"))),
            (2, "back to A", lambda: self.pick(self.a)),
            (2, "to B", lambda: self.pick(self.b)),
            (2, "rebind", self.rebind),
        ]

    def replace(self) -> None:
        """A play route's replace, its session rotation handed in — now and
        then of files HQPlayer refuses whole: nothing commits, nothing
        rotates, and the replace answers so (QueueRefused)."""
        rotated = []
        items = self.items(self.rng.randint(1, 4))
        refused = self.rng.random() < 0.2
        if refused:
            for f in (self.a, self.b):
                f.refuse.update(it.source["path"] for it in items)
        before = self.mgr.queue.snapshot()
        try:
            added, gen = self.mgr.replace_queue(items, play=True,
                                                archive=lambda: rotated.append(True))
        except QueueRefused as e:
            self.rec("step-refused", why=str(e))
            added, gen = 0, self.mgr.queue.generation
        except BaseException:
            if rotated:
                self.violate("I12: history rotated for a replace the output refused")
            raise
        finally:
            if refused:
                for f in (self.a, self.b):
                    f.refuse.difference_update(it.source["path"] for it in items)
        if len(rotated) != (1 if added else 0):
            self.violate(f"I12: {len(rotated)} rotation(s) for a replace that took {added}")
        if refused and isinstance(self.mgr.active, HqpBackend) and (
                added or self.mgr.queue.snapshot() != before):
            self.violate("I12: a replace HQPlayer refused whole changed the queue")
        self._taken_whole("a replace", items, added, gen)

    def play_next(self) -> None:
        """One track to play next, or two now and then: a batch HQPlayer
        may take part of."""
        items, gen = self.items(self.rng.randint(1, 2)), self.mgr.queue.generation
        self._taken_whole("a play next", items, self.mgr.append(items, "next"), gen)

    def _taken_whole(self, what: str, items: list, added: int, gen: int) -> None:
        """I11: a batch is taken whole — what HQPlayer refused of it a gap —
        or, refused whole, not at all."""
        if added and added != len(items):
            self.violate(f"I11: {what} took {added} of {len(items)} tracks: what HQPlayer "
                         "refused of it is no gap")
        elif added:
            self.batches.append((gen, items))

    def fill_through_build(self) -> None:
        """A filler adding while HQPlayer builds — a build that may run out
        or end in a vanish: what it took is never lost."""
        r = self.rng
        self.driven().build("SetFilter", r.uniform(0.6, 1.6), answer=r.choice(["first", "last"]))
        self.dsp()
        if r.random() < 0.3:
            self.vanish()
        self.fill()

    def fill(self) -> None:
        items, gen = self.items(self.rng.randint(1, 3)), self.mgr.queue.generation
        self.fills.append((gen, items))
        self.player._owned_filler(items, gen)

    def remove(self) -> None:
        mgr = self.mgr
        k = self.rng.randint(1, max(len(mgr.queue), 1))
        item = mgr.queue.item_at(k)
        if item is not None and mgr.remove(k, item.track_id):
            self.removed.add(id(item))

    def dsp(self) -> None:
        hb.change_dsp(self.rng.choice([{"filter": 57, "filter1x": 57}, {"shaper": 2},
                                       {"filter": 12, "filter1x": 12, "shaper": 3}]))

    def switch(self) -> None:
        r = self.rng.random()
        if r < 0.4:
            self.settings_router.put_output_prefs(self.settings_router.OutputPrefs(type="browser"))
        else:
            self.pick(self.b if r < 0.7 else self.a)

    def dsp_then_switch(self) -> None:
        """A change of several knobs whose first builds, and the output
        changed while it does: what is left of the change is not sent."""
        self.build("SetFilter")
        try:
            hb.change_dsp({"filter": 12, "filter1x": 12, "shaper": 3})
        finally:
            time.sleep(self.rng.uniform(0.0, 0.4))
            self.switch()

    def jump_then_switch(self) -> None:
        """A jump whose SelectTrack holds its answer through the start's
        build, and the output changed meanwhile: its Play is not sent."""
        self.build("SelectTrack", answer="last")
        jump = threading.Thread(target=self.run_step, daemon=True, name="scenario-jump",
                                args=("jump", lambda: self.player.jump(self.player.JumpRequest(
                                    index=self.rng.randint(1, max(len(self.mgr.queue), 1))))))
        self.threads.append(jump)
        jump.start()
        time.sleep(self.rng.uniform(0.2, 0.5))
        self.switch()

    def arm_build(self) -> None:
        r = self.rng
        command = r.choice(["SetFilter", "SetShaping", "Play", "SelectTrack"])
        answer = "last" if command == "Play" else r.choice(["first", "last"])
        self.hqp().build(command, r.uniform(0.1, 1.5), answer=answer, stops=r.random() < 0.3)

    def restart(self) -> None:
        f = self.hqp()
        self.rec("gone", fake=f.name)
        f.restart()

    def vanish(self) -> None:
        f = self.hqp()
        if f._down:
            return
        self.rec("gone", fake=f.name)
        f.close()
        back = threading.Timer(self.rng.uniform(0.3, 1.2), f.come_back)
        back.name = "scenario-return"
        back.daemon = True
        back.start()
        self.threads.append(back)

    def theirs(self) -> None:
        """Another controller plays an entry of its own on that HQPlayer."""
        f = self.hqp()
        self.foreign += 1
        with f._lock:
            f.playlist.append(f"file:///Elsewhere/theirs-{self.foreign}.flac")
            f.track = len(f.playlist)
            f.state = int(PlaybackState.PLAYING)

    def entry_before(self) -> None:
        """An entry of HQPlayer's own put before the one it plays, its
        mirror then lost through a silence: answering again it gets what
        lies behind that entry — a tail mirror (I14), judged at the rest
        that follows. HQPlayer not playing a slot of the queue it holds, the
        step makes it play one first — a tap on the first slot it holds — so
        there is an entry of ours to put one before."""
        b = self.mgr.active
        if not isinstance(b, HqpBackend):
            return
        alone = not any(t.is_alive() for t in self.threads
                        if t is not threading.current_thread())
        began = self.last_step
        f = self.driven()
        ours = {b._uri_of(it)[0] for it in b._layout().handed}

        def playing() -> bool:
            return (f.state == int(PlaybackState.PLAYING) and 1 <= f.track <= len(f.playlist)
                    and f.playlist[f.track - 1] in ours)
        with f._lock:
            ready = playing()
        if not ready:
            held = b._layout().slots
            if not held:
                return
            self.player.jump(self.player.JumpRequest(index=held[0]))
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                with f._lock:
                    if playing():
                        break
                time.sleep(0.05)
        with f._lock:
            if not playing():
                return                  # the start did not take (a build, a silence)
            self.foreign += 1
            f.playlist.insert(f.track - 1, f"file:///Elsewhere/before-{self.foreign}.flac")
            f.track += 1
        b.lose_mirror("an entry of HQPlayer's own before the one it plays")
        if alone and self.last_step == began:
            self.rest_due = began       # judged at rest before another step moves it

    def right_after(self, due: Optional[int]) -> bool:
        """Is the rest that follows the tail step `due` right after it — no
        step began since, none ran beside it?"""
        return due is not None and self.last_step == due

    def reorder(self) -> None:
        order = [it.track_id for it in self.mgr.queue.snapshot()]
        if not order or None in order:
            # nothing to order (the route says 400), or an entry adopted
            # from HQPlayer — it has no track to name in the new order
            return
        self.rng.shuffle(order)
        self.player.reorder(self.player.ReorderRequest(order=order))

    def rebind(self) -> None:
        """A file left the library under one slot: the manager's re-bind, as
        the media_files trigger wakes it."""
        self.mgr.rebind_files()

    def _rebind_origins(self, items: list) -> list:
        """What rebind_origins reads from the database for one gone file:
        one slot that names a file of the library — never a pass-through
        entry adopted from HQPlayer — takes another copy of its track."""
        files = [i for i, it in enumerate(items)
                 if it.source.get("kind") == "file" and it.media_file_id is not None]
        out: list = [None] * len(items)
        if files:
            self.next_id += 1
            new = self.next_id
            out[self.rng.choice(files)] = {
                "media_file_id": new,
                "source": {"kind": "file", "path": f"E:/Music/R/{new}.flac", "format": "FLAC"}}
        return out

    def _refused(self, name: str, n: Optional[int], e: HTTPException) -> None:
        """A route's refusal — 409 busy, a 503 whose cause is the output
        out of reach or HQPlayer's own refusal in its words — never a crash
        a route wrapped into a 503."""
        cause = e.__context__
        if e.status_code != 409 and not isinstance(cause, (ConnectionError, OSError,
                                                           OutputRefused)):
            self.unexpected.append(f"{name}: {e.status_code} {e.detail} — "
                                   + "".join(traceback.format_exception(cause)))
        self.rec("step-refused", step=n, status=e.status_code, why=e.detail)

    def run_step(self, name: str, fn: Callable[[], Any]) -> None:
        n = self.rec("step", name=name)
        with self._lock:
            self.last_step = max(self.last_step, n)
        b = self.mgr.active
        alone = not any(t.is_alive() for t in self.threads
                        if t is not threading.current_thread())
        before = getattr(b, "_spot", None) if name in _KEEP_SPOT and alone else None
        try:
            fn()
            self.rec("step-done", step=n)
            after = getattr(self.mgr.active, "_spot", None)
            if (before is not None and self.mgr.active is b
                    and self.mgr.queue.index_of(before[0]) is not None
                    and (after is None or after[0] is not before[0])):
                self.violate(f"I10: {name} took the spot off its item")
        except HTTPException as e:
            self._refused(name, n, e)
        except (ConnectionError, OSError) as e:
            self.rec("step-refused", step=n, why=f"{type(e).__name__}: {e}")
        except OutputRefused as e:
            self.rec("step-refused", step=n, why=str(e))
        except Exception:
            self.unexpected.append(f"{name}: {traceback.format_exc()}")
            self.rec("step-error", step=n)

    def step(self) -> None:
        steps = self.steps()
        weight, name, fn = self.rng.choices(steps, weights=[s[0] for s in steps])[0]
        if self.rng.random() < 0.35:
            t = threading.Thread(target=self.run_step, args=(name, fn), daemon=True,
                                 name=f"scenario-{name}")
            self.threads.append(t)
            t.start()
        else:
            self.run_step(name, fn)

    def at_rest(self, *, press_first: bool = False, tail: bool = False) -> None:
        """Invariants 11, 13 and 14, and the Stops a switch away owed, at a
        full settle: what is left of HQPlayer's own background work done —
        the reconverge a silence made due among it, unless the person
        presses first (`press_first`: the press and the reconverge meet) —
        then the queue and the mirror compared, given a bounded while to
        converge (the poller sees a return). A mirror marked lost or drifted
        is given a play press: HQPlayer holds the queue after it. Right
        after the tail step (`tail`), no step since to explain it, a tail
        mirror that does not hold breaks invariant 14 at once."""
        deadline = time.monotonic() + 8.0
        quiet = 0
        while quiet < 2 and time.monotonic() < deadline:
            # Twice in a row: the poller clears the due reconverge a moment
            # before it starts its thread
            b = self.mgr.active
            busy = (any(t.name in _WORKERS for t in threading.enumerate())
                    or (not press_first and getattr(b, "_reconverge_due", False)))
            quiet = 0 if busy else quiet + 1
            time.sleep(0.05)
        queue = self.mgr.queue.snapshot()
        if len({id(it) for it in queue}) != len(queue):
            self.violate("I13: an item twice in the queue")
        for gen, items in self.fills:
            if gen != self.mgr.queue.generation:
                continue
            # what HQPlayer refused whole the filler never took
            lost = [it.title for it in items
                    if self.mgr.queue.index_of(it) is None and id(it) not in self.removed
                    and it.source.get("path") not in self.refused]
            if lost:
                self.violate(f"I11: a filler's tracks are not in the queue: {lost}")
        for gen, items in self.batches:
            if gen != self.mgr.queue.generation:
                continue
            lost = [it.title for it in items
                    if self.mgr.queue.index_of(it) is None and id(it) not in self.removed]
            if lost:
                self.violate(f"I11: tracks a replace or a play next took are not in the "
                             f"queue: {lost}")
        self._owed_stops()
        pressed = False
        while True:
            # Read afresh each time round: the press attaches the output
            # again when the play gate found it gone
            b = self.mgr.active
            if not isinstance(b, HqpBackend):
                return
            hqp = self.b if b._link.port == self.b.port else self.a
            if hqp._down:
                return
            with hqp._lock:
                playlist, track, state = list(hqp.playlist), hqp.track, hqp.state
            snapshot = self.mgr.queue.snapshot()
            self._gaps(b, hqp, snapshot)
            want = [b._uri_of(it)[0] for it in b._layout(snapshot).handed]
            if playlist == want:
                return
            holds = _tail_holds(playlist, want, track, state)
            if b._mirror == "tail" and not pressed and tail and not holds:
                self.violate(f"I14: at rest after the tail step HQPlayer holds {playlist} and "
                             f"reads entry {track}, the queue {want}")
                return
            if b._mirror == "tail" and not pressed and holds:
                # I14 judged at rest — on an entry read, or nothing to judge —
                # then the play press a person's tap there is: HQPlayer takes
                # the whole queue
                self.rec("rest", tail=state in (int(PlaybackState.PLAYING),
                                                int(PlaybackState.PAUSED)))
                pressed = True
                if not self._press():
                    return
                deadline = time.monotonic() + 8.0
                continue
            if (not snapshot and b._mirror in ("lost", "drift")
                    and any("/Elsewhere/" in u for u in playlist)):
                return      # another controller's playlist and no queue: nothing to give it
            if (b._mirror == "drift" and not pressed and self._drift_unexplained(b, playlist)):
                self.violate(f"I13: HQPlayer holds {playlist}, the queue {want}: a divergence "
                             "of the node's own, taken for an edit made elsewhere")
                return
            if b._mirror in ("lost", "drift") and not pressed and snapshot:
                pressed = True
                if not self._press():
                    return              # refused (a build, a silence): no verdict now
                deadline = time.monotonic() + 8.0
                continue
            if time.monotonic() > deadline:
                self.violate(f"I13: at rest HQPlayer holds {playlist}, the queue {want}, "
                             f"the mirror {b._mirror}" + (" after a play press" if pressed else ""))
                return
            time.sleep(0.1)

    def _drift_unexplained(self, b: HqpBackend, playlist: list) -> bool:
        """Did the canary make the drift that stands — "an external edit?" —
        over a playlist with no entry of anyone else's, as it read it then?
        Judged by the move that made the state: every other drift is the
        node's own (a removal, a new order HQPlayer did not take, a press's
        edits followed)."""
        with self._lock:
            moves = [e for e in self.log
                     if e.kind == "mirror" and e.data["link"] == self.lid(b._link)]
        last = moves[-1].data if moves else None
        if (last is None or last["state"] != "drift"
                or not (last["why"] or "").startswith("HQPlayer's playlist drifted")):
            return False
        foreign = last["foreign"]
        return not (foreign if foreign is not None else any("/Elsewhere/" in u for u in playlist))

    def _gaps(self, b: HqpBackend, hqp: FakeHqp, snapshot: list) -> None:
        """I13's gaps: a slot is one exactly when HQPlayer cannot open it — a
        file of the other HQPlayer's library — or `hqp`, the HQPlayer `b`
        drives, refused its file: one the runner made it refuse, or one of
        the other's library a pass-through entry names (the queue took
        HQPlayer's own playlist over, unresolved)."""
        endpoint = b.endpoint_id
        for it in snapshot:
            why = b.gap(it)
            elsewhere = it.source["kind"] == "hqp" and it.source["endpoint"] != endpoint
            if elsewhere != (why is not None and why != hb.REFUSED_HERE):
                self.violate(f"I13: {it.title} is {'a gap: ' + why if why else 'no gap'} "
                             f"on endpoint {endpoint}")
            src = it.opener()
            opens = src.get("path") or src.get("uri") or ""
            if why == hb.REFUSED_HERE and not any(part in opens for part in hqp.refuse):
                self.violate(f"I13: {it.title} is a gap HQPlayer never refused")

    def _press(self) -> bool:
        """A play press — a tap on the first slot HQPlayer holds — on a
        playlist that is no mirror, at rest: taken, and the track it starts
        plays — the slot pressed, or, refused as HQPlayer takes the queue,
        the next slot it holds. Rest is quiet: no build of HQPlayer's own is
        left armed for it to meet. A refusal is HQPlayer's to explain —
        away, silent, building or just back — never the node's own."""
        self.rec("step", name="press (at rest)")
        b = self.mgr.active
        layout = b._layout()
        if not layout.slots:
            return False                # nothing of the queue on this HQPlayer
        slot = layout.slots[0]
        hqp = self.b if b._link.port == self.b.port else self.a
        for f in (self.a, self.b):
            with f._lock:
                f._builds.clear()
        link = b._link
        try:
            self.player.jump(self.player.JumpRequest(index=slot))
        except (HTTPException, ConnectionError, OSError) as e:
            why = f"{e.status_code} {e.detail}" if isinstance(e, HTTPException) else str(e)
            self.rec("step-refused", why=why)
            if not (hqp._down or hqp.mute or hqp._silent_until > time.monotonic()
                    or link.build.silent() is not None or link.silent_since is not None
                    or link._unreachable_until > time.monotonic()):
                self.violate(f"I13: at rest a play press was refused — {why} — with "
                             f"HQPlayer up, answering and building nothing")
            return False
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            working = any(t.name in _WORKERS for t in threading.enumerate())
            now = b._layout()
            starts = next((b._uri_of(now.items[s - 1])[0] for s in now.slots if s >= slot), None)
            if not working and (starts is None
                                or (b._mirror == "lost" and b._lost_by == "refusal")):
                return True             # HQPlayer took nothing to start: nothing plays
            with hqp._lock:
                plays = (hqp.state == int(PlaybackState.PLAYING)
                         and 1 <= hqp.track <= len(hqp.playlist)
                         and hqp.playlist[hqp.track - 1] == starts)
            if plays and not working:
                return True
            time.sleep(0.05)
        self.violate(f"I13: at rest the track the press on slot {slot} starts never played")
        return True

    def _owed_stops(self) -> None:
        """I6, the other half: the Stop a switch away owed went — unless a
        newer attach superseded it, HQPlayer stopped or played another
        entry meanwhile, stayed silent past the limit or went away, or the
        node stopped; past a build it did not finish none is owed."""
        with self._lock:
            log = list(self.log)
        for i, e in enumerate(log):
            d = e.data
            if (e.kind != "released" or e.seq < self.owed_checked
                    or d["entry"] not in d["given"] or d["unfinished"]
                    or d["state"] not in (int(PlaybackState.PLAYING), int(PlaybackState.PAUSED))):
                continue
            fake = d["fake"]

            def settled(x) -> bool:
                k, y = x.kind, x.data
                return ((k == "wire" and y["fake"] == fake
                         and ((y["what"] == "in" and y["command"] == "Stop")
                              or y["state"] not in (int(PlaybackState.PLAYING),
                                                    int(PlaybackState.PAUSED))
                              or (y["entry"] or "").find("/Elsewhere/") >= 0))
                        or (k == "accept" and y["intent"] == "attach" and y["link"][0] == fake)
                        or (k == "build-end" and y["link"][0] == fake and y["how"] != "answered")
                        or (k == "gone" and y["fake"] == fake))
            if not any(settled(x) for x in log[i + 1:]):
                self.violate(f"I6: the Stop the switch away (event {e.seq}) owed {fake} never went")
        self.owed_checked = len(log)

    def settle(self, *, full: bool) -> None:
        """Between steps a random breath only: what one set going — a build,
        a silence, a step on its thread — runs on beside the next, which is
        where the orders that matter happen. A full settle is a bounded wait
        for quiet: no fake in a silence; on every link not closed no build in
        progress and no breaker open after a connect that failed (an
        HQPlayer just back is dialled once its cooldown is over); the link
        the output drives has heard HQPlayer answer since every build and
        silence (until then it refuses — a build it did not finish
        included); the steps on threads done. A closed link is nobody's: its
        watch keeps whatever it last saw. One that never gets quiet is a
        violation naming what stayed."""
        if not full:
            time.sleep(self.rng.uniform(0.0, 0.3))
            return
        deadline = time.monotonic() + self.settle_limit
        while True:
            now = time.monotonic()
            links = [ln for ln in list(hl._links.values()) if ln.state != "closed"]
            driven = getattr(self.mgr.active, "_link", None)
            pending = (
                [f"{f.name} silent" for f in (self.a, self.b) if f._silent_until > now]
                + [f"{self.lid(ln)} building" for ln in links if ln.build.building() is not None]
                + [f"{self.lid(ln)} breaker open" for ln in links if ln._unreachable_until > now]
                + ([f"{self.lid(driven)} not heard since a build or a silence"]
                   if driven in links and (driven.build.silent() is not None
                                           or driven.silent_since is not None) else [])
                + [f"step {t.name} running" for t in self.threads if t.is_alive()])
            if not pending:
                break
            if now > deadline:
                self.violate(f"the rest never came: {pending}")
                break
            time.sleep(0.05)
        self.threads = [t for t in self.threads if t.is_alive()]

    # -- the node stop: invariant 5 --------------------------------------------------

    def node_stop(self) -> None:
        if self.rng.random() < 0.5:
            # mid-build: a change holding its answer while the node stops
            self.build("SetFilter", answer="last")
            self.run_step("dsp", self.dsp)
        self.rec("step", name="node stop")
        workers = [t for t in threading.enumerate() if t.name in _WORKERS] + self.threads
        t0 = time.monotonic()
        hl.close_all("the node is stopping")
        if time.monotonic() - t0 > 0.5:
            self.violate(f"I5: close_all took {time.monotonic() - t0:.2f} s")
        stopped = self.rec("node-stopped")
        for t in workers:
            if not t.name.startswith("scenario-return"):
                t.join(max(0.0, 2.0 - (time.monotonic() - t0)))
        late = [t.name for t in workers if t.is_alive() and not t.name.startswith("scenario-return")]
        if late:
            self.violate(f"I5: still waiting on HQPlayer 2 s after the node stop: {late}")
        self.mgr.activate(None, stop_old=False)
        with self._lock:
            after = [e for e in self.log[stopped:] if e.kind == "send"]
        if after:
            self.violate(f"I5: sent after the node stop: {[str(e) for e in after]}")

    # -- the invariants ------------------------------------------------------------------

    def check(self) -> None:
        with self._lock:
            log = list(self.log)
        self._one_at_a_time(log)
        self._own_address(log)
        self._superseded(log)
        self._ends_heard(log)
        self._switch_stop(log)
        self._statuses(log)
        self._radio_order(log)
        self.checked = len(log)

    def _one_at_a_time(self, log: list) -> None:
        """I1: per link one request open at a time; every request a fake got
        came while one of its link's requests was open; none goes out while
        a build is on, only questions while one is unfinished."""
        open_by: dict = {}
        building: dict = {}
        unfinished: dict = {}
        for e in log:
            d = e.data
            if e.kind == "send":
                hqp, link = d["link"][0], d["link"]
                if open_by.get(hqp):
                    self._once(e, f"I1: two requests open at once at {hqp}")
                if building.get(link):
                    self._once(e, f"I1: {d['command']} sent while {link} builds")
                if unfinished.get(link) and d["command"] not in _QUESTIONS:
                    self._once(e, f"I1: {d['command']} sent into an unfinished build")
                open_by[hqp] = open_by.get(hqp, 0) + 1
            elif e.kind == "answered":
                open_by[d["link"][0]] = max(open_by.get(d["link"][0], 0) - 1, 0)
            elif e.kind == "build-begin":
                building[d["link"]] = True
            elif e.kind == "build-end":
                building[d["link"]] = False
                unfinished[d["link"]] = d["how"] == "timeout"
            elif e.kind == "build-answered":
                unfinished[d["link"]] = False
            elif e.kind == "wire" and d["what"] == "in":
                if not open_by.get(d["fake"]):
                    self._once(e, f"I1: {d['command']} reached {d['fake']} from no request of its link")

    def _own_address(self, log: list) -> None:
        """I2: an item goes only on the link it was made on; of what began
        after a link's retire only the Stop it owes goes, and nothing after
        its close. A request already on its way at the retire is answered."""
        state: dict = {}
        at_begin: dict = {}
        for e in log:
            d = e.data
            if e.kind in ("retire", "close", "unretire"):
                state[d["link"]] = d["state"]           # as it stood once changed
            elif e.kind == "request":
                at_begin[e.seq] = state.get(d["link"], "open")
                if d["made_on"] is not None and d["made_on"] != d["link"]:
                    self._once(e, f"I2: an item made on {d['made_on']} sent on {d['link']}")
            elif e.kind == "send":
                begun = log[d["begun"]].data
                st = at_begin.get(d["begun"], "open")
                if st == "closed":
                    self._once(e, f"I2: {d['command']} begun on a closed link went out")
                elif (st == "retiring" and state.get(d["link"]) == "retiring"
                      and not (begun["item"] or "").startswith("late_stop")):
                    # retiring from before it began until it went: never chosen again between
                    self._once(e, f"I2: {d['command']} begun on a retiring link went out "
                                  f"for {begun['item']}")

    def _superseded(self, log: list) -> None:
        """I3 and I8: no request serves an item a newer intent of a kind that
        supersedes it held before it began — published, or a new queue's or
        a rebuild's pending from its ask and not withdrawn — nor one made for
        a queue that was replaced since."""
        latest: dict = {}
        pending: dict = {}
        newer_by: dict = {}
        for e in log:
            d = e.data
            if e.kind == "accept":
                key = (d["link"], d["intent"])
                latest[key] = max(latest.get(key, 0), d["n"])
                pending.pop((d["link"], d["n"]), None)
            elif e.kind == "pend":
                pending[(d["link"], d["n"])] = d["intent"]
            elif e.kind == "withdraw":
                pending.pop((d["link"], d["n"]), None)
            elif e.kind == "request" and d["item"]:
                kind, n = d["item"].split("#")
                by = hl.SUPERSEDED_BY[kind]
                newer_by[e.seq] = (
                    [k for k in by if latest.get((d["link"], k), 0) > int(n)]
                    + [k for (link, m), k in pending.items()
                       if link == d["link"] and m > int(n) and k in by])
            elif e.kind == "send" and d["begun"] in newer_by:
                begun = log[d["begun"]].data
                if newer_by[d["begun"]]:
                    self._once(e, f"I3: {d['command']} for {begun['item']} sent after a newer "
                                  f"{newer_by[d['begun']]}")
                if begun["generation"] is not None and begun["queue"] != begun["generation"]:
                    self._once(e, f"I8: {d['command']} for {begun['item']} of queue "
                                  f"{begun['generation']} sent while queue {begun['queue']} plays")

    def _ends_heard(self, log: list) -> None:
        """I7: a wait returns as soon as the build's end is told — at once
        when it was told before."""
        ends: dict = {}
        waits: dict = {}
        for e in log:
            d = e.data
            if e.kind == "build-end":
                ends[(d["link"], d["n"])] = e.t
            elif e.kind == "wait":
                waits[e.seq] = e
            elif e.kind == "waited":
                w = waits.get(d["entered"])
                if w is None:
                    continue
                end = ends.get((w.data["link"], w.data["build"]))
                if end is not None and e.t - max(end, w.t) > 0.5:
                    self._once(e, f"I7: a wait returned {e.t - max(end, w.t):.2f} s after the "
                                  "build's end")

    def _switch_stop(self, log: list) -> None:
        """I6: the Stop a switch away sends (its late_stop item) reaches only
        an HQPlayer that plays or pauses an entry of ours."""
        stopping: dict = {}
        seen: dict = {}
        for e in log:
            d = e.data
            if e.kind == "send" and d["command"] == "Stop":
                if (log[d["begun"]].data["item"] or "").startswith("late_stop"):
                    stopping[d["link"][0]] = True
            elif e.kind == "wire" and d["what"] == "in" and d["command"] == "Status":
                seen[d["fake"]] = (d["entry"], d["state"])
            elif e.kind == "wire" and d["what"] == "in" and d["command"] == "Stop":
                if stopping.pop(d["fake"], False):
                    # Judged on what the Status before it found: another
                    # controller may act between the two
                    entry, state = seen.get(d["fake"], (None, None))
                    ours = entry is not None and "/Elsewhere/" not in entry
                    if not ours or state not in (int(PlaybackState.PLAYING),
                                                 int(PlaybackState.PAUSED)):
                        self._once(e, f"I6: the switch-away Stop reached {d['fake']}, which "
                                      f"played {entry} in state {state}")

    def _statuses(self, log: list) -> None:
        """I9: unfinished only after a build HQPlayer did not finish — never
        after a broken held answer; 'disconnected' only after a miss that
        was no silence."""
        last_end: dict = {}
        last_miss: dict = {}
        for e in log:
            d = e.data
            if e.kind == "build-end":
                last_end[d["link"]] = d["how"]
            elif e.kind == "miss":
                last_miss[d["link"]] = d["silent"]
            elif e.kind == "emit":
                if d["building"] == "unfinished" and last_end.get(d["link"]) != "timeout":
                    self._once(e, "I9: unfinished said with no build that ran out")
                if d["state"] == "disconnected" and last_miss.get(d["link"]) is not False:
                    self._once(e, "I9: disconnected said after a silent miss")
                if d["not_answering"] and d["building"]:
                    self._once(e, "I9: not answering said together with a build")

    def _radio_order(self, log: list) -> None:
        """I12, the radio's half: history rotates before HQPlayer's playlist
        is cleared — no PlaylistClear the rotation did not come before."""
        archived: set = set()
        for e in log:
            if e.kind == "step":
                archived.discard(e.tid)         # a radio's mark is its own step's
            elif e.kind == "archive":
                archived.add(e.tid)
            elif e.kind == "send" and e.data["command"] == "PlaylistClear":
                # The rotation and the clear are one step's, on its thread
                if e.tid not in archived:
                    self._once(e, "I12: a radio's PlaylistClear went before history rotated")
                archived.discard(e.tid)

    def _once(self, e: _Event, what: str) -> None:
        if e.seq >= self.checked:
            self.violate(f"{what} (event {e.seq})")


def _tail_holds(playlist: list, want: list, track: int, state: int) -> bool:
    """Invariant 14 as the fake shows it: from the entry HQPlayer reads to
    its end, its playlist is the queue's from that entry's slot."""
    if state not in (int(PlaybackState.PLAYING), int(PlaybackState.PAUSED)):
        return True
    if not 1 <= track <= len(playlist) or playlist[track - 1] not in want:
        return False
    q = want.index(playlist[track - 1]) + 1
    return playlist[track - 1:] == want[q - 1:]


@pytest.mark.parametrize("seed", _seeds())
def test_the_links_keep_their_invariants_whatever_the_order(seed, fake, fake_b, listens,
                                                           monkeypatch):
    world = World(seed, fake, fake_b, monkeypatch)
    world.scale_time()
    world.instrument()
    try:
        world.start()
        for i in range(int(os.environ.get("SAUTIUM_SCENARIO_STEPS", "25"))):
            world.step()
            full = i % 6 == 5 or world.rest_due is not None
            world.settle(full=full)
            if full:
                # A tail mirror is judged before anyone presses; any other
                # rest, now and then the person presses before the
                # reconverge HQPlayer's return made due
                due, world.rest_due = world.rest_due, None
                world.at_rest(press_first=due is None and world.rng.random() < 0.5,
                              tail=world.right_after(due))
            world.check()
            if world.violations or world.unexpected:
                break
        world.node_stop()
        world.check()
    except BaseException:
        print(world.dump())
        raise
    if world.violations or world.unexpected:
        print(world.dump())
    assert not world.unexpected, f"seed {seed}: " + "\n".join(world.unexpected)
    assert not world.violations, f"seed {seed}: " + "\n".join(world.violations)


# -- the runner's own judgments ----------------------------------------------------------
#
# The oracle is code too: each test below sets up one state the seeds draw and
# asks what the runner says of it — no violation for an outcome the design
# gives, a violation for a fault it exists to catch.


@contextmanager
def _a_world(fake, fake_b, monkeypatch):
    world = World(1, fake, fake_b, monkeypatch)
    world.scale_time()
    world.instrument()
    try:
        world.start()
        assert _wait(lambda: len(fake.playlist) == 4, timeout=5), fake.playlist
        yield world
    finally:
        hl.close_all("the test is over")
        world.mgr.activate(None, stop_old=False)


def _lost_for_good(world) -> None:
    """The mirror lost by a refusal, the queue edited on its own — no
    reconverge mends it: the rest's press is HQPlayer's next hand-over."""
    world.mgr.active.lose_mirror("a filler's wait gave up", by="refusal")
    assert world.mgr.remove(4, world.mgr.queue.item_at(4).track_id)


def test_the_rest_waits_until_hqplayer_answered_a_build_it_did_not_finish(
        fake, fake_b, listens, monkeypatch):
    """A build past the limit leaves the link refusing until it hears
    HQPlayer answer again — the poller's next Status, a moment after the
    silence: the rest waits for it, and its press is taken."""
    with _a_world(fake, fake_b, monkeypatch) as world:
        _lost_for_good(world)
        link = world.mgr.active._link
        fake.build("SetFilter", 1.5, answer="last")
        threading.Thread(target=lambda: hb.change_dsp({"filter": 57, "filter1x": 57}),
                         daemon=True).start()
        assert _wait(lambda: (link.build.state or {}).get("unfinished"), timeout=5)
        fake.service["Status"] = 0.15           # heard a little after the silence
        world.settle(full=True)
        n = len(world.log)
        world.at_rest()
        fake.service.pop("Status", None)
        assert not world.violations, world.violations
        assert not [e for e in world.log[n:] if e.kind == "step-refused"], world.log[n:]
        b = world.mgr.active
        want = [b._uri_of(it)[0] for it in b._layout().handed]
        assert _wait(lambda: fake.state == int(PlaybackState.PLAYING) and fake.playlist == want)


def test_a_number_taken_while_the_lock_is_held_breaks_invariant_15(fake, fake_b, listens,
                                                                   monkeypatch):
    """Every number a sequence publishes while it holds the command lock is
    held against the clock as it asked — a second one too, taken once the
    lock was the sequence's."""
    with _a_world(fake, fake_b, monkeypatch) as world:
        link = world.mgr.active._link
        with link.command("transport"):
            link.accept("transport")
        assert [v for v in world.violations if v.startswith("I15:")], world.violations


def test_a_build_left_armed_never_meets_the_rest_press(fake, fake_b, listens, monkeypatch):
    """A step armed a build of HQPlayer's own on the next Play — one that
    leaves it stopped — and nothing met it: rest is quiet, the press plays."""
    with _a_world(fake, fake_b, monkeypatch) as world:
        _lost_for_good(world)
        fake.build("Play", 0.3, answer="last", stops=True)
        world.settle(full=True)
        world.at_rest()
        assert not world.violations, world.violations
        assert fake.state == int(PlaybackState.PLAYING)


def test_the_rest_press_waits_for_the_track_it_starts(fake, fake_b, listens, monkeypatch):
    """The slot pressed is a file HQPlayer refuses, queued while the mirror
    was lost: the press hands it over, it is a gap, and the next track
    HQPlayer took plays — the outcome the design gives, no violation."""
    with _a_world(fake, fake_b, monkeypatch) as world:
        mgr = world.mgr
        mgr.active.lose_mirror("HQPlayer refused all 1 tracks", by="refusal")
        for k in (4, 3, 2, 1):
            assert mgr.remove(k, mgr.queue.item_at(k).track_id)
        refused, fine = _item("E:/Music/S/901.flac", 901), _item("E:/Music/S/902.flac", 902)
        world.refused.add(refused.source["path"])
        fake.refuse.add(refused.source["path"])
        assert mgr.append([refused, fine]) == 2
        world.settle(full=True)
        world.at_rest()
        assert not world.violations, world.violations
        assert fake.playlist[fake.track - 1] == "file:///E:/Music/S/902.flac"


def test_a_link_closed_while_hqplayer_built_is_nobodys(fake, fake_b, listens, monkeypatch):
    """Another HQPlayer chosen while the one no output drove built: its link
    closes with its watch saying "building" for good. The rest neither waits
    for it nor takes it for the reason a press at the other was refused — a
    play gate that refuses a playlist that is no mirror is still caught."""
    with _a_world(fake, fake_b, monkeypatch) as world:
        a = world.mgr.active._link
        world.settings_router.put_output_prefs(world.settings_router.OutputPrefs(type="browser"))
        fake.build("SetFilter", 1.5, answer="last")
        threading.Thread(target=lambda: hb.change_dsp({"filter": 57, "filter1x": 57}),
                         daemon=True).start()
        assert _wait(lambda: a.build.building() is not None, timeout=3)
        world.pick(fake_b)
        assert _wait(lambda: isinstance(world.mgr.active, HqpBackend)
                     and world.mgr.active._link.port == fake_b.port
                     and len(fake_b.playlist) == 4, timeout=5)
        assert a.state == "closed" and a.build.building() is not None
        assert _wait(lambda: fake._silent_until <= time.monotonic(), timeout=5)
        t0 = time.monotonic()
        world.settle(full=True)
        assert time.monotonic() - t0 < 5.0 and not world.violations, world.violations
        reachable = HqpBackend.reachable
        monkeypatch.setattr(HqpBackend, "reachable", lambda self: False if self._mirror in (
            "lost", "drift") else reachable(self))
        _lost_for_good(world)
        world.at_rest()
        assert any(v.startswith("I13: at rest a play press was refused")
                   for v in world.violations), world.violations


def test_two_radios_on_threads_of_one_name_keep_their_own_marks(fake, fake_b, listens,
                                                                 monkeypatch):
    """A radio step on a thread rotates history and waits for HQPlayer's
    answer; a second, on a thread of the same name, begins meanwhile: each
    clear follows its own rotation — no I12."""
    with _a_world(fake, fake_b, monkeypatch) as world:
        radio = lambda: world.mgr.clear_for_radio(lambda: world.rec("archive"))  # noqa: E731
        fake.service["Status"] = 0.3            # the radio's Status, answered slowly
        first = threading.Thread(target=world.run_step, args=("radio", radio), daemon=True,
                                 name="scenario-radio")
        first.start()
        assert _wait(lambda: any(e.kind == "archive" for e in world.log), timeout=3)
        second = threading.Thread(target=world.run_step, args=("radio", radio), daemon=True,
                                  name="scenario-radio")
        second.start()
        first.join(5)
        second.join(5)
        fake.service.pop("Status", None)
        world.check()
        assert not world.violations, world.violations


def test_the_tail_step_is_judged_at_rest(fake, fake_b, listens, monkeypatch):
    """HQPlayer plays a slot of the queue; the step puts an entry of its own
    before it, the mirror lost through a silence: the rest waits for the
    reconverge that gives it what lies behind the entry — due at the
    poller's next answer, here a little late — then judges the tail mirror
    (I14), no press first; the press after it hands HQPlayer the queue."""
    with _a_world(fake, fake_b, monkeypatch) as world:
        world.player.jump(world.player.JumpRequest(index=2))
        assert _wait(lambda: fake.state == int(PlaybackState.PLAYING) and fake.track == 2)
        world.run_step("an entry before", world.entry_before)
        assert world.right_after(world.rest_due)
        fake.service["Status"] = 0.3
        world.settle(full=True)
        n = len(world.log)
        world.at_rest()
        fake.service.pop("Status", None)
        assert not world.violations, world.violations
        rest = [e for e in world.log[n:] if e.kind in ("rest", "step")]
        assert [(e.kind, e.data) for e in rest] == [
            ("rest", {"tail": True}), ("step", {"name": "press (at rest)"})], rest
        assert fake.playlist == [f"file:///E:/Music/A/0{i}.flac" for i in range(1, 5)]


def _a_tail_that_does_not_hold(world, fake) -> None:
    """The tail step on slot 2, the reconverge's tail taken; then two
    entries behind the one HQPlayer reads change places — a repair that
    re-added them out of order."""
    world.player.jump(world.player.JumpRequest(index=2))
    assert _wait(lambda: fake.state == int(PlaybackState.PLAYING) and fake.track == 2)
    world.run_step("an entry before", world.entry_before)
    world.settle(full=True)
    assert _wait(lambda: world.mgr.active._mirror == "tail")
    with fake._lock:
        i = fake.track
        fake.playlist[i], fake.playlist[i + 1] = fake.playlist[i + 1], fake.playlist[i]


def test_a_tail_step_another_step_ran_after_is_no_rest_right_after_it(fake, fake_b, listens,
                                                                         monkeypatch):
    """The tail step drawn onto a thread, the reconverge's tail taken; then
    another step — another controller playing its own entry on the driven
    HQPlayer — before the rest: that rest is not right after the tail step,
    and judges the tail as any rest does (the canary's drift, explained by
    the entry at the one read, then the press) — never an I14 broken by the
    node."""
    with _a_world(fake, fake_b, monkeypatch) as world:
        world.player.jump(world.player.JumpRequest(index=2))
        assert _wait(lambda: fake.state == int(PlaybackState.PLAYING) and fake.track == 2)
        t = threading.Thread(target=world.run_step, args=("an entry before",
                                                           world.entry_before),
                             daemon=True, name="scenario-an entry before")
        world.threads.append(t)
        t.start()
        t.join(10)
        due = world.rest_due
        assert world.right_after(due)
        assert _wait(lambda: world.mgr.active._mirror == "tail")
        world.hqp = lambda: fake
        world.run_step("theirs", world.theirs)
        assert not world.right_after(due)
        world.settle(full=True)
        world.at_rest(tail=world.right_after(due))
        assert not world.violations, world.violations


def test_a_tail_step_run_beside_another_step_leaves_no_rest_right_after_it(fake, fake_b, listens,
                                                                           monkeypatch):
    """The tail step run while another step still runs — one that may move
    HQPlayer once it ends: the rest after them is no rest right after the
    tail step."""
    with _a_world(fake, fake_b, monkeypatch) as world:
        world.player.jump(world.player.JumpRequest(index=2))
        assert _wait(lambda: fake.state == int(PlaybackState.PLAYING) and fake.track == 2)
        running, release = threading.Event(), threading.Event()

        def beside() -> None:
            running.set()
            release.wait(10)
        t = threading.Thread(target=world.run_step, args=("beside", beside), daemon=True,
                             name="scenario-beside")
        world.threads.append(t)
        t.start()
        assert running.wait(5)
        world.run_step("an entry before", world.entry_before)
        release.set()
        t.join(10)
        assert any("/before-" in uri for uri in fake.playlist), fake.playlist
        assert world.rest_due is None


def test_a_tail_that_does_not_hold_at_rest_breaks_invariant_14(fake, fake_b, listens,
                                                                monkeypatch):
    """Right after the tail step, no step since to explain it, a tail mirror
    whose tail is not the queue's is invariant 14 broken — never waited on
    until the canary calls it drift."""
    with _a_world(fake, fake_b, monkeypatch) as world:
        _a_tail_that_does_not_hold(world, fake)
        world.at_rest(tail=True)
        assert [v for v in world.violations if v.startswith("I14:")], world.violations


def test_a_tails_drift_is_not_explained_by_the_entry_before_it(fake, fake_b, listens,
                                                               monkeypatch):
    """A tail that does not hold, drifted by the canary: the entry of
    HQPlayer's own before the one it reads is the tail's, no edit made
    elsewhere — the drift is the node's own, a violation."""
    monkeypatch.setattr(hb, "DRIFT_CHECK_EVERY", 1)
    with _a_world(fake, fake_b, monkeypatch) as world:
        _a_tail_that_does_not_hold(world, fake)
        assert _wait(lambda: (world.mgr.active.poke(), world.mgr.active._mirror)[1] == "drift")
        world.at_rest()
        assert [v for v in world.violations if "taken for an edit made elsewhere" in v], (
            world.violations)


def test_a_rest_that_never_comes_names_what_stayed(fake, fake_b, listens, monkeypatch):
    """A link whose breaker never closes again — HQPlayer back and never
    dialled: the full settle's deadline is a violation naming it, never a
    silent stall the next check may or may not meet."""
    with _a_world(fake, fake_b, monkeypatch) as world:
        world.settle_limit = 1.0
        link = world.mgr.active._link
        link._unreachable_until = time.monotonic() + 10 ** 6
        world.settle(full=True)
        assert world.violations == [f"the rest never came: ['{world.lid(link)} breaker open']"]
        link._unreachable_until = 0.0


def test_a_batch_that_lost_what_hqplayer_refused_is_caught(fake, fake_b, listens,
                                                           monkeypatch):
    """A "play next" of two, one file HQPlayer refuses: it stays in the queue
    as a gap. An output that dropped it from what the queue takes instead is
    the fault I11 exists for."""
    takes = HqpBackend._queue_takes

    def dropping(self, items, handed, taken, unsent, what):
        refused = {id(it) for it, t in zip(handed, taken) if not t}
        return [it for it in takes(self, items, handed, taken, unsent, what)
                if id(it) not in refused]
    monkeypatch.setattr(HqpBackend, "_queue_takes", dropping)
    with _a_world(fake, fake_b, monkeypatch) as world:
        refused, fine = _item("E:/Music/S/901.flac", 901), _item("E:/Music/S/902.flac", 902)
        world.refused.add(refused.source["path"])
        fake.refuse.add(refused.source["path"])
        gen = world.mgr.queue.generation
        world._taken_whole("a play next", [refused, fine],
                           world.mgr.append([refused, fine], "next"), gen)
        assert world.violations == ["I11: a play next took 1 of 2 tracks: what HQPlayer "
                                    "refused of it is no gap"]


def test_a_gap_is_judged_by_what_the_driven_hqplayer_refuses(fake, fake_b, listens,
                                                             monkeypatch):
    """A pass-through entry the queue took over from A's playlist names a
    file of A's library; handed to B, which opens none of A's files, it is
    refused while B takes the rest — a gap B refused, the outcome the design
    gives, no violation. A gap the driven HQPlayer never refused is still
    caught."""
    with _a_world(fake, fake_b, monkeypatch) as world:
        mgr = world.mgr
        world.pick(fake_b)
        assert _wait(lambda: isinstance(mgr.active, HqpBackend)
                     and mgr.active._link.port == fake_b.port
                     and len(fake_b.playlist) == 4, timeout=5)
        b = mgr.active
        adopted = QueueItem(track_id=None, media_file_id=None,
                            source={"kind": "uri", "uri": "file:///D:/Library/H/950.flac"},
                            title="", artist="")
        fine = _item("E:/Music/S/951.flac", 951)
        assert mgr.append([adopted, fine]) == 2
        assert b.gap(adopted) == hb.REFUSED_HERE and b.gap(fine) is None
        world._gaps(b, fake_b, mgr.queue.snapshot())
        assert not world.violations, world.violations
        b._link.refusals.append((fine, hb._slot_key(fine)))     # a refusal B never made
        world._gaps(b, fake_b, mgr.queue.snapshot())
        assert world.violations == ["I13: Song 951 is a gap HQPlayer never refused"]
