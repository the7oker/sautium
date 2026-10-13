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
 11. Nothing a filler took is lost: at rest it is in the queue, unless a
     newer queue replaced it or a person removed it.
 12. A replace rotates listening history only once the output took it:
     once when it did, never when it was refused.
 13. At rest HQPlayer's playlist is the queue without its gaps, in the same
     order, or the mirror is marked lost (or drifted: an edit made
     elsewhere) — and a play press then makes it so; no item twice in the
     queue. A gap is a slot that HQPlayer cannot open (a file of the other
     HQPlayer's library) or one whose file it refused, and nothing else.
 14. A tail mirror plays the queue's tracks from the entry it reads to the
     end.

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
                              _queue_stays_in_memory, _ways_in_by_library, fake, fake_b,
                              listens)

from hqplayer_client import PlaybackState  # noqa: E402
from playback import hqp_backend as hb  # noqa: E402
from playback import hqp_link as hl  # noqa: E402
from playback import manager as manager_mod  # noqa: E402
from playback.hqp_backend import HqpBackend  # noqa: E402
from playback.manager import PlaybackManager  # noqa: E402

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

    def queue_insert_next(self, items, anchor_index) -> list:
        return list(items)

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
        self.next_id = 100
        self._links = 0
        self.fills: list = []           # (queue generation, the items a filler took)
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
                                   threading.current_thread().name, kind, data))
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

        accept = hl.HqpLink.accept

        def accepted(link, kind):
            n = accept(link, kind)
            w.rec("accept", link=lid(link), intent=kind, n=n)
            return n
        mp.setattr(hl.HqpLink, "accept", accepted)

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

        drifted = HqpBackend._drifted

        def marked(backend, why, **kw):
            w.rec("drift", link=lid(backend._link), why=why)
            return drifted(backend, why, **kw)
        mp.setattr(HqpBackend, "_drifted", marked)

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
            (2, "play next", lambda: mgr.append(self.items(1), "next")),
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
        rotates."""
        rotated = []
        items = self.items(self.rng.randint(1, 4))
        refused = self.rng.random() < 0.2
        if refused:
            for f in (self.a, self.b):
                f.refuse.update(it.source["path"] for it in items)
        before = self.mgr.queue.snapshot()
        try:
            added, _ = self.mgr.replace_queue(items, play=True,
                                              archive=lambda: rotated.append(True))
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
        lies behind that entry — a tail mirror (I14)."""
        b = self.mgr.active
        if not isinstance(b, HqpBackend):
            return
        f = self.driven()
        with f._lock:
            if f.state != int(PlaybackState.PLAYING) or not 1 <= f.track <= len(f.playlist):
                return
            self.foreign += 1
            f.playlist.insert(f.track - 1, f"file:///Elsewhere/before-{self.foreign}.flac")
            f.track += 1
        b.lose_mirror("an entry of HQPlayer's own before the one it plays")

    def reorder(self) -> None:
        order = [it.track_id for it in self.mgr.queue.snapshot()]
        if not order:
            return                      # nothing to order: the route says 400
        self.rng.shuffle(order)
        self.player.reorder(self.player.ReorderRequest(order=order))

    def rebind(self) -> None:
        """A file left the library under one slot: re-bound in place, the
        output told — PlaybackManager.rebind_files without the database."""
        mgr = self.mgr
        n = len(mgr.queue)
        if not n:
            return
        self.next_id += 1
        slot, new = self.rng.randint(1, n), self.next_id
        with mgr._mutation():
            moved = mgr.queue.rebind(
                lambda items: [{"media_file_id": new,
                                "source": {"kind": "file", "path": f"E:/Music/R/{new}.flac",
                                           "format": "FLAC"}}
                               if i == slot else None for i, _ in enumerate(items, start=1)],
                None)
            backend = mgr.active
            if moved and backend is not None:
                backend.queue_changed("rebind")
        if backend is not None:
            backend.queue_start()

    def _refused(self, name: str, n: Optional[int], e: HTTPException) -> None:
        """A route's refusal — 409 busy, a 503 whose cause is the output
        out of reach or HQPlayer's own refusal in its words — never a crash
        a route wrapped into a 503."""
        cause = e.__context__
        if e.status_code != 409 and not (
                isinstance(cause, (ConnectionError, OSError))
                or (isinstance(cause, RuntimeError)
                    and str(cause).startswith(("HQPlayer did not", "none of these tracks is on")))):
            self.unexpected.append(f"{name}: {e.status_code} {e.detail} — "
                                   + "".join(traceback.format_exception(cause)))
        self.rec("step-refused", step=n, status=e.status_code, why=e.detail)

    def run_step(self, name: str, fn: Callable[[], Any]) -> None:
        n = self.rec("step", name=name)
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
        except RuntimeError as e:
            if not str(e).startswith(("HQPlayer did not", "none of these tracks is on")):
                self.unexpected.append(f"{name}: {traceback.format_exc()}")
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

    def at_rest(self) -> None:
        """Invariants 11, 13 and 14, and the Stops a switch away owed, at a
        full settle: what is left of HQPlayer's own background work done,
        then the queue and the mirror compared — given a bounded while to
        converge (the poller sees a return). A mirror marked lost or drifted
        is given a play press: HQPlayer holds the queue after it."""
        deadline = time.monotonic() + 8.0
        while (any(t.name in _WORKERS for t in threading.enumerate())
               and time.monotonic() < deadline):
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
            self._gaps(b, snapshot)
            want = [b._uri_of(it)[0] for it in b._layout(snapshot).handed]
            if playlist == want:
                return
            if b._mirror == "tail" and _tail_holds(playlist, want, track, state):
                return
            if not snapshot and any("/Elsewhere/" in u for u in playlist):
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
        """Did the canary mark the drift — "an external edit?" — with no
        entry of anyone else's in HQPlayer's playlist? Every other drift
        names its own reason (a removal, a new order HQPlayer did not take)."""
        with self._lock:
            marks = [e for e in self.log if e.kind == "drift" and e.data["link"] == self.lid(b._link)]
        return (bool(marks) and marks[-1].data["why"].startswith("HQPlayer's playlist drifted")
                and not any("/Elsewhere/" in u for u in playlist))

    def _gaps(self, b: HqpBackend, snapshot: list) -> None:
        """I13's gaps: a slot is one exactly when HQPlayer cannot open it — a
        file of the other HQPlayer's library — or its file was refused there."""
        endpoint = b.endpoint_id
        for it in snapshot:
            why = b.gap(it)
            elsewhere = it.source["kind"] == "hqp" and it.source["endpoint"] != endpoint
            if elsewhere != (why is not None and why != hb.REFUSED_HERE):
                self.violate(f"I13: {it.title} is {'a gap: ' + why if why else 'no gap'} "
                             f"on endpoint {endpoint}")
            if why == hb.REFUSED_HERE and it.source.get("path") not in self.refused:
                self.violate(f"I13: {it.title} is a gap HQPlayer never refused")

    def _press(self) -> bool:
        """A play press — a tap on the first slot HQPlayer holds — on a
        playlist that is no mirror: whether it was taken."""
        self.rec("step", name="press (at rest)")
        held = self.mgr.active._layout().slots
        if not held:
            return False                # nothing of the queue on this HQPlayer
        try:
            self.player.jump(self.player.JumpRequest(index=held[0]))
        except HTTPException as e:
            self._refused("press (at rest)", None, e)
            return False
        except (ConnectionError, OSError) as e:
            self.rec("step-refused", why=str(e))
            return False
        deadline = time.monotonic() + 8.0
        while (any(t.name in _WORKERS for t in threading.enumerate())
               and time.monotonic() < deadline):
            time.sleep(0.05)
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
        for quiet: no fake in a silence, no build in progress, the steps on
        threads done (one still running past it hung)."""
        if not full:
            time.sleep(self.rng.uniform(0.0, 0.3))
            return
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            silent = any(f._silent_until > time.monotonic() for f in (self.a, self.b))
            building = any(ln.build.building() for ln in list(hl._links.values()))
            if not silent and not building and not any(t.is_alive() for t in self.threads):
                break
            time.sleep(0.05)
        else:
            hung = [t.name for t in self.threads if t.is_alive()]
            if hung:
                self.violate(f"steps never ended: {hung}")
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
        supersedes it was accepted before it began, nor one made for a queue
        that was replaced since."""
        latest: dict = {}
        newer_by: dict = {}
        for e in log:
            d = e.data
            if e.kind == "accept":
                key = (d["link"], d["intent"])
                latest[key] = max(latest.get(key, 0), d["n"])
            elif e.kind == "request" and d["item"]:
                kind, n = d["item"].split("#")
                newer_by[e.seq] = [k for k in hl.SUPERSEDED_BY[kind]
                                   if latest.get((d["link"], k), 0) > int(n)]
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
            if e.kind == "archive":
                archived.add(e.thread)
            elif e.kind == "send" and e.data["command"] == "PlaylistClear":
                # The rotation and the clear are one step's, on its thread
                if e.thread not in archived:
                    self._once(e, "I12: a radio's PlaylistClear went before history rotated")
                archived.discard(e.thread)

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
            world.settle(full=i % 6 == 5)
            if i % 6 == 5:
                world.at_rest()
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
