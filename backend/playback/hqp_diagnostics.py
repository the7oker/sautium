"""
Why didn't it play? — the HQPlayer output's playback diagnostics
(docs/HQPLAYER_INTEGRATION.md § "Diagnostics").

Every play intent on the HQPlayer output — play, a jump, next/previous, a
queue replaced with play, a rebuild that resumes, the resume after a DSP
change — is one ATTEMPT, and so is HQPlayer's own advance onto an entry it
stops before (`advance`): what Sautium handed HQPlayer for that slot and how
(a path it opens, a stream from the media proxy, a phantom preview), every
command and HQPlayer's answer to it, the status for ten seconds after the
first transport command (an advance: from its first stopped tick to its
deadline), what the media proxy saw for that file's token, and a VERDICT:
one of a fixed set of causes, with a sentence and the owner's next step. The last 20 are kept in memory and never in the database — a trace
holds paths and track identities, which are content: it leaves the process
only through the local API (whole) and a support warrant's `playback` scope
(paths cut to their last two components).

HQPlayer's own log names what the protocol does not. It is read on demand —
the Diagnostics screen, an attempt the protocol does not explain, a warrant
— and followed only while an advance attempt waits for its deadline, from
the offset at its first stopped tick, on this machine only (LogFollower).

This module holds the data and the rules; playback.hqp_backend feeds it. The
lock is a LEAF lock: held only to change or copy memory, never across a
socket, the database, a file, HTTP or a thread start (failing_run() is read
on the event loop through the notices snapshot).
"""

import copy
import logging
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

import hqp_rate_check as rc
from hqp_atlas import rate_name
from hqplayer_client import CommandOutcome, redact_path, redact_uri, uri_to_file_path

logger = logging.getLogger(__name__)

OBSERVE_S = 10.0       # watched this long after the first transport command
FETCH_WINDOW_S = 5.0   # an http hand-over HQPlayer has not fetched by then never reached us
# ... and up to this long while HQPlayer is still making the start — it says
# it plays, or its control port is silent, and the position has not moved: a
# long filter initialises for seconds before any audio leaves (sinc-MGa at
# DSD256 on a laptop's CPU: 5–7 s, the port answering no Status meanwhile —
# observed 2026-10-03; the WSL2 hop flaps for seconds while music plays on).
START_LIMIT_S = 60.0
# HQPlayer's own advance onto an entry of another rate: at the end of the one
# before it stops on no entry (track 0, its track_serial one more), plays out
# the tail, then builds the next entry or refuses it — the tail up to 1.71 ×
# the output delay (an output delay of 3.05 s: decided at +5.2 s, playing at
# +6.2 s; Desktop 6.2.4, 2026-10-18), a build up to ten seconds (a heavy SDM
# filter changed mid-play, 2026-10-11). Judged at the first stopped tick +
# ADVANCE_DELAYS × the last playing tick's output delay + ADVANCE_BUILD_S;
# with no output delay reported, ADVANCE_DELAY_S (the largest measured,
# rounded up), and the trace says so.
ADVANCE_DELAYS = 2
ADVANCE_BUILD_S = 11.0
ADVANCE_DELAY_S = 3.5
# ... and only from an entry's last seconds — its status read up to a second
# before the stop, the position HQPlayer refreshes once a second, the length
# in whole seconds: a click elsewhere in its window takes an entry of the
# owner's choosing, not the next
ADVANCE_END_S = 5.0
RING_SIZE = 20
LEDGER_SIZE = 2048
LOG_LINES = 200

HTTP_MODES = frozenset({"stream", "cut", "transcode", "preview"})
TRANSPORT = frozenset({"Play", "SelectTrack", "Next", "Previous"})
STEP_COMMANDS = TRANSPORT | {"Stop", "Pause", "Seek", "PlaylistClear", "PlaylistRemove"}
# Sent from outside an attempt's own intent, these end it: the owner moved on
# — a stop or a pause, a DSP change (HQPlayer may stop the transport for its
# build: 6.2.3 on Windows did), a playlist emptied or replaced. Appends (a
# filler streaming an album in) and seeks or volume do not.
OWNER_COMMANDS = frozenset({"Stop", "Pause", "PlaylistClear", "SetMode", "SetFilter",
                            "SetShaping", "SetRate", "SetConvolution", "MatrixSetProfile"})
PROXY_ERRORS = frozenset({401, 403, 404, 500, 502, 504})

_lock = threading.Lock()
_ring: deque = deque(maxlen=RING_SIZE)
_ledger: "OrderedDict[str, dict]" = OrderedDict()   # handed URI → how and with what outcome
_by_slot: "OrderedDict[tuple, str]" = OrderedDict()  # slot key → the URI it was last handed as
_last_id = 0
_run_key = None
_follower: Optional["LogFollower"] = None   # the newest advance's log reader


# -- The hand-over ledger ---------------------------------------------------------
# What each URI HQPlayer holds stands for, and how its PlaylistAdd ended — an
# attempt on a playlist mirrored minutes ago still knows how its slot got there.

def mode_of(uri: str) -> str:
    if uri.startswith("file://"):
        return "path"
    if "/file/" in uri:
        return "stream"
    if "/preview/" in uri:
        return "preview"
    return "foreign"


def note_handover(uri: str, key: tuple, item, mode: str) -> None:
    entry = {"uri": uri, "ts": time.time(), "mode": mode, "track_id": item.track_id,
             "media_file_id": item.media_file_id, "title": item.title,
             "artist": item.artist, "ok": None, "result": None, "message": ""}
    with _lock:
        _ledger.pop(uri, None)
        _ledger[uri] = entry
        _by_slot.pop(key, None)
        _by_slot[key] = uri
        while len(_ledger) > LEDGER_SIZE:
            _ledger.popitem(last=False)
        while len(_by_slot) > LEDGER_SIZE:
            _by_slot.popitem(last=False)


def note_add(uri: str, ok: bool, result: Optional[str], message: str) -> None:
    """The FINAL outcome of handing `uri` to HQPlayer (after any retry)."""
    with _lock:
        entry = _ledger.get(uri)
        if entry is None:
            # Re-appended from HQPlayer's own playlist (queue_insert_next).
            entry = _ledger[uri] = {"uri": uri, "ts": time.time(), "mode": mode_of(uri),
                                    "track_id": None, "media_file_id": None,
                                    "title": "", "artist": ""}
        entry.update(add_ts=time.time(), ok=ok, result=result, message=message)


def handover(uri: Optional[str]) -> Optional[dict]:
    if not uri:
        return None
    with _lock:
        entry = _ledger.get(uri)
        return dict(entry) if entry else None


def handover_like(same) -> Optional[dict]:
    """The newest hand-over whose URI names the same entry as `same` says —
    HQPlayer reports a URI back in its own form (Desktop 6: `file://E:/…`,
    two slashes; escaped brackets), never byte for byte as it was handed."""
    with _lock:
        entries = list(_ledger.items())
    return next((dict(e) for uri, e in reversed(entries) if same(uri)), None)


def handed_uri(key: tuple) -> Optional[str]:
    with _lock:
        return _by_slot.get(key)


# -- Attempts ---------------------------------------------------------------------

def _new_id() -> int:
    global _last_id
    with _lock:
        _last_id = max(_last_id + 1, int(time.time() * 1000))
        return _last_id


@dataclass
class Attempt:
    intent: str
    # HQPlayer's entry it should play — never the queue's slot, which the
    # gaps move away from it (playback.hqp_backend.Entry); None = read off HQPlayer
    slot: Optional[int] = None
    uris: Optional[list] = None          # what this intent handed, in playlist order
    items: Optional[list] = None         # their queue items (a replace: the queue still holds the old ones)
    id: int = field(default_factory=_new_id)
    started: float = field(default_factory=time.time)
    t0: Optional[float] = None           # its first transport command
    steps: list = field(default_factory=list)
    adds: dict = field(default_factory=lambda: {"count": 0, "failed": []})
    ticks: list = field(default_factory=list)
    misses: list = field(default_factory=list)
    error: Optional[str] = None          # raised before a command reached HQPlayer
    end: Optional[str] = None            # window | hard | owner | superseded | shutdown | refused
    facts: Optional[dict] = None
    verdict: Optional[dict] = None
    hqp_log: Optional[dict] = None
    # A failure HQPlayer has since gone past — answered (an `unreachable`),
    # or played that very slot (note_playing): it explains nothing any more.
    cleared: bool = False
    stop_asked: bool = False             # its stop at the start was judged (stopped_at_start)
    # HQPlayer's own advance (advance()): the entry before, the take's
    # track_serial, the output delay its deadline counts and the deadline,
    # its log — None not followed (no log here), "followed", "lost" (a read
    # that failed, a file cut short), "hung" (an earlier reader still hangs:
    # none started) — and the refusal it named (heard): the trace's "advance"
    advance: Optional[dict] = None
    follower: Optional["LogFollower"] = None

    def record(self, o: CommandOutcome) -> None:
        """A command this intent sent, with HQPlayer's answer."""
        command = o.during or o.command
        with _lock:
            if command == "PlaylistAdd":
                self.adds["count"] += 1
                if o.failed and len(self.adds["failed"]) < 5:
                    self.adds["failed"].append({"uri": o.attributes.get("uri", ""),
                                                "result": o.result, "message": o.message})
                return
            if command not in STEP_COMMANDS and not o.failed:
                return
            if command in TRANSPORT and self.t0 is None:
                self.t0 = o.ts
            if command == "SelectTrack" and o.result == "OK":
                self.slot = int(o.attributes.get("index") or 0) or self.slot
            self.steps.append({"ts": o.ts, "command": command, "result": o.result,
                               "message": o.message, "attributes": dict(o.attributes)})

    def refused(self, command: str, message: str) -> None:
        """A command that never reached HQPlayer (no connection to send it on)."""
        with _lock:
            self.error = self.error or f"{command}: {message}"
            self.steps.append({"ts": time.time(), "command": command, "result": "refused",
                               "message": message, "attributes": {}})

    def tick(self, status, state: str, read_at: float) -> None:
        """A status the poller asked for at `read_at`. One asked before the
        transport command went is what HQPlayer held before it — another
        thread's command may pass between the poller's read and this call —
        and no tick of the attempt."""
        with _lock:
            if self.t0 is None or read_at < self.t0:
                return
            self.ticks.append(tick_of(status, state, read_at))
            follower = self.follower
        if follower is not None:
            follower.wake()

    def stopped_at_start(self) -> bool:
        """The first tick since the transport command that says HQPlayer
        stopped at 0 s, nothing having played yet: how a rate HQPlayer
        refuses looks — it takes the Play and stops at once. True once: the
        close's questions are asked then, and an attempt the rate check
        names a cause for closes at that tick (end "refused") instead of at
        the window."""
        with _lock:
            # An advance stops by its first tick: HQPlayer decides after its tail
            if (self.stop_asked or self.t0 is None or self.advance is not None or not self.ticks
                    or started(self.ticks)):
                return False
            last = self.ticks[-1]
            if last["state"] != "stopped" or last["position"] > 0:
                return False
            self.stop_asked = True
            return True

    def miss(self, message: str, *, silent: bool = False) -> None:
        """A status read that got nothing: `silent` when HQPlayer took it and
        did not answer (busy building), not when it refused or closed."""
        with _lock:
            if self.t0 is not None:
                self.misses.append({"ts": time.time(), "message": message, "silent": silent})

    def intent_done(self, exc: Optional[BaseException]) -> None:
        """The intent returned: a fact that settles it now — a command that
        never reached HQPlayer, the FINAL answer to a transport command being
        a refusal (the last answer of each command counts), or no transport
        command sent at all — closes it on the next poller pass instead of
        after the window."""
        with _lock:
            if exc is not None and self.error is None:
                self.error = str(exc) or type(exc).__name__
            hard = (self.error is not None or self.t0 is None
                    or _final_refusal(self.steps) is not None)
            if hard and self.end is None:
                self.end = "hard"

    def request_end(self, why: str) -> None:
        with _lock:
            if self.end is None:
                self.end = why

    def due(self, now: float) -> Optional[str]:
        """Why it closes now, or None while it is still watching: the
        window — an advance's deadline — and past it a start HQPlayer is
        still making (see START_LIMIT_S) — judged before the audio leaves, a
        play that initialises for seven seconds read as nothing explaining
        it."""
        with _lock:
            if self.end is not None:
                return self.end
            if self.t0 is None:
                return None
            if now < (self.advance["deadline"] if self.advance else self.t0 + OBSERVE_S):
                return None
            if now < self.t0 + START_LIMIT_S and self._starting():
                return None
            self.end = "window"
            return self.end

    def _starting(self) -> bool:
        """The position has not moved, and HQPlayer's last word was that it
        plays, a state its SDK does not name (Embedded said 5 while it
        started a play) — or silence: it took the read and said nothing (a
        build). Refused or closed is HQPlayer away, judged at the window.
        Called under _lock."""
        if started(self.ticks, self._serial()):
            return False
        last_tick = self.ticks[-1] if self.ticks else None
        last_miss = self.misses[-1] if self.misses else None
        if last_miss is not None and (last_tick is None or last_miss["ts"] > last_tick["ts"]):
            return last_miss.get("silent", False)
        return last_tick is not None and last_tick["state"] in ("playing", "unknown")

    def snapshot(self) -> dict:
        with _lock:
            out = {"intent": self.intent, "started": self.started, "t0": self.t0,
                   "end": self.end, "error": self.error, "slot": self.slot,
                   "uris": list(self.uris) if self.uris else None,
                   "steps": copy.deepcopy(self.steps), "adds": copy.deepcopy(self.adds),
                   "ticks": copy.deepcopy(self.ticks), "misses": list(self.misses)}
            if self.advance is not None:
                out["advance"] = dict(self.advance)
            return out

    def moved(self) -> bool:
        """HQPlayer started it: audio left (started)."""
        with _lock:
            return started(self.ticks, self._serial())

    def _serial(self) -> Optional[int]:
        """An advance's take; None for a play. Called under _lock."""
        return self.advance["serial"] if self.advance else None

    def follow(self, info: dict, wake_poller) -> None:
        """Follow HQPlayer's log on this machine while this advance waits
        for its deadline (LogFollower) — stopped as it closes. While an
        earlier advance's reader is still alive (an open on a mount that
        dropped hangs), none is started: this one has its deadline alone,
        and the trace says so."""
        global _follower
        with _lock:
            if _follower is not None and _follower.alive():
                self.advance["log"] = "hung"
                return
            self.follower = _follower = LogFollower(self, info, wake_poller)
        self.follower.start()

    def heard(self, line: str) -> None:
        """HQPlayer's log refused this advance's rate combination
        (LogFollower): it closes at the poller's next pass, as refused."""
        with _lock:
            self.advance["line"] = line
            if self.end is None:
                self.end = "refused"

    def following(self, state: str) -> None:
        """Its log "followed" from the stop's offset, or "lost" on a read
        that failed: the trace says which (LogFollower)."""
        with _lock:
            self.advance["log"] = state


def tick_of(status, state: str, read_at: float) -> dict:
    """One status read as an attempt's trace keeps it, at the time the
    poller asked for it."""
    return {"ts": read_at, "state": state, "track": status.track_index,
            "position": status.position, "length": status.length,
            "speed": status.process_speed, "in_fill": status.input_fill,
            "out_fill": status.output_fill, "tracks_total": status.tracks_total,
            "mode": status.active_mode, "filter": status.active_filter,
            "shaper": status.active_shaper, "rate": status.active_rate,
            "src_rate": status.src_rate, "serial": status.track_serial}


def started(ticks: list, serial: Optional[int] = None) -> bool:
    """The start signal, asked of this one place by every attempt, a play's
    and an advance's — so that another, the meter stream's first sound, can
    take its place here alone: HQPlayer said it played and the position
    moved, audio left. For an advance, only on its take (`serial`:
    HQPlayer's track_serial at its first stopped tick) — a Play of that
    entry afterwards takes it again, one more, and is an attempt of its
    own."""
    playing = [t for t in ticks
               if t["state"] == "playing" and (serial is None or t.get("serial") == serial)]
    return (any(t["position"] >= 1.0 for t in playing)
            or (len(playing) >= 2 and playing[-1]["position"] > playing[0]["position"]))


def advance(entry: int, serial: int, output_delay: Optional[float], now: float) -> Attempt:
    """HQPlayer's own advance from `entry`, seen at its first stopped tick
    (`now`, its track_serial `serial`): an attempt on the entry after it,
    watched from that tick to its deadline (ADVANCE_DELAYS)."""
    delay = output_delay or ADVANCE_DELAY_S
    return Attempt(intent="advance", slot=entry + 1, t0=now,
                   advance={"from": entry, "serial": serial, "output_delay": delay,
                            "delay_assumed": not output_delay,
                            "deadline": now + ADVANCE_DELAYS * delay + ADVANCE_BUILD_S,
                            "log": None, "line": None})


class LogFollower:
    """HQPlayer's log on this machine, followed while an advance attempt
    waits for its deadline: from the log's size at the first stopped tick, a
    refused rate combination HQPlayer writes closes the attempt at once
    (Attempt.heard, then `wake_poller`). Its own thread finds and opens the
    file once and, woken by each status tick (wake), reads only the bytes
    appended since — a partial last line waits for the next tick; the
    poller never touches the file (an open on a mount that dropped can hang
    for minutes), and while one hangs no other is started (Attempt.follow).
    Any trouble — no log, a refused read, a file shorter than at the stop
    (cut or rotated) — ends the following quietly, with one line in our
    log: the deadline judges the attempt."""

    def __init__(self, attempt: Attempt, info: dict, wake_poller) -> None:
        self._attempt, self._info, self._wake_poller = attempt, dict(info or {}), wake_poller
        self._tick = threading.Event()
        self._stopped = False
        self._thread: Optional[threading.Thread] = None
        self.offset = None              # how far into the file it has read

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="hqp-log-follow")
        self._thread.start()

    def alive(self) -> bool:
        return self._thread is None or self._thread.is_alive()

    def wake(self) -> None:
        self._tick.set()

    def stop(self) -> None:
        self._stopped = True
        self._tick.set()

    def _run(self) -> None:
        att = self._attempt.id
        try:
            path, note, _ = _local_log(self._info)
            if path is None:
                logger.info("HQPlayer's log not followed for attempt %d: %s", att, note)
                return
            with open(path, "rb") as f:
                self.offset = f.seek(0, os.SEEK_END)
                self._attempt.following("followed")
                rest = b""
                while True:
                    self._tick.wait()
                    self._tick.clear()
                    if self._stopped:
                        return
                    size = os.fstat(f.fileno()).st_size
                    if size < self.offset:
                        self._attempt.following("lost")
                        logger.info("HQPlayer's log not followed further for attempt %d: "
                                    "shorter than at its stop", att)
                        return
                    chunk = f.read(size - self.offset)
                    self.offset += len(chunk)
                    *lines, rest = (rest + chunk).split(b"\n")
                    for raw in lines:
                        line = raw.decode("utf-8", "replace").rstrip("\r")
                        if _RATE_LINE.search(line):
                            self._attempt.heard(line)
                            self._wake_poller()
                            return
        except (OSError, ET.ParseError) as e:
            if self.offset is None:
                logger.info("HQPlayer's log not followed for attempt %d: %s", att, e)
            else:
                self._attempt.following("lost")
                logger.info("HQPlayer's log not followed further for attempt %d: %s", att, e)


def close(attempt: Attempt, facts: dict) -> bool:
    """Judge a finished attempt — an advance with the refusal its log named
    while it waited — and keep it; an advance HQPlayer played, or one another
    action took over, is not kept: the ring holds what failed. Returns
    whether the failing run (failing_run) changed — the notices channel is
    woken on that."""
    if attempt.follower is not None:
        attempt.follower.stop()
    with _lock:
        line = (attempt.advance or {}).get("line")
    v = verdict(facts, {"cause": classify([line])} if line else None)
    with _lock:
        attempt.facts = facts
        attempt.verdict = v
        if attempt.advance is not None and v["code"] in ("played", "interrupted"):
            return False
        _ring.append(attempt)
    return _run_changed()


def record_unreachable(message: str, context: dict) -> bool:
    """A play intent that never reached HQPlayer — the play-intent gate's
    GetInfo got no answer. Kept as a closed attempt of its own."""
    attempt = Attempt(intent="play", error=message, end="hard")
    now = time.time()
    return close(attempt, {"intent": "play", "started": attempt.started, "t0": None,
                           "closed": now, "end": "hard", "error": message, "slot": None,
                           "expected_uri": None, "mode": None, "handover": None,
                           "steps": [], "adds": {"count": 0, "failed": []}, "ticks": [],
                           "misses": [], "proxy": None, "playlist": None,
                           "item": None, "context": context})


def attach_log(attempt: Attempt, log: dict) -> bool:
    """HQPlayer's log, read after the attempt closed. A verdict the protocol
    left without a cause, or a refused rate, is judged again with it: the
    log's line can name the refusal, and is quoted. The new verdict replaces
    the old in place — the Diagnostics list, the sheet, Now Playing's offer;
    no new toast. Returns whether the failing run changed."""
    with _lock:
        attempt.hqp_log = log
        code = (attempt.verdict or {}).get("code")
        facts = attempt.facts
    if facts is None or code not in ("not_played", "unknown", "rate_refused"):
        return False
    v = verdict(facts, log)
    with _lock:
        attempt.verdict = v
    return _run_changed()


def note_answered() -> bool:
    """HQPlayer answered a status read: an `unreachable` run ends here — the
    condition ends when its source does. Returns whether the run changed."""
    with _lock:
        newest = next((a for a in reversed(_ring)
                       if a.verdict and a.verdict["code"] != "interrupted"), None)
        if newest is None or newest.cleared or newest.verdict["code"] != "unreachable":
            return False
        newest.cleared = True
    return _run_changed()


def late_start(tick: dict, generation: int) -> bool:
    """HQPlayer started an advance's entry after its deadline judged it, on
    that same take (track_serial as at its first stopped tick) — the start
    signal (started) says so: the attempt becomes `played` and its failure
    is cleared. A Play of that entry is another take, an attempt of its own,
    and changes nothing here. Returns whether the failing run changed."""
    with _lock:
        late = [a for a in _ring
                if a.advance is not None and a.verdict
                and a.verdict["code"] not in ("played", "interrupted")
                and a.advance["serial"] == tick.get("serial")
                and (a.facts or {}).get("generation") == generation
                and started([tick], a.advance["serial"])]
        for a in late:
            a.verdict = _v("played", a.facts, late_s=round(tick["ts"] - a.t0, 1))
            a.cleared = True
    return bool(late) and _run_changed()


def note_playing(slot: int, generation: int) -> bool:
    """HQPlayer plays this slot of this queue, its position moving and its
    speed keeping up: a failure judged on that very slot explains nothing
    any more — it is cleared, Now Playing stops offering it and a failing
    run ends. Without this, the end of an album brought back a failure from
    its start: a failed attempt is held back only while something plays.
    Returns whether the run changed."""
    with _lock:
        # Every failure judged on this slot of this queue, not the newest
        # alone: an unreachable attempt closed after it (a Play pressed again
        # while HQPlayer was still silent) kept the failure for good
        failed = [a for a in _ring
                  if a.verdict and not a.cleared
                  and a.verdict["code"] not in ("played", "interrupted")
                  and (a.facts or {}).get("slot") == slot
                  and (a.facts or {}).get("generation") == generation]
        if not failed:
            return False
        for a in failed:
            a.cleared = True
    return _run_changed()


# -- What HQPlayer refused of a batch ------------------------------------------------
# One notice per batch it refused files of — never one per track: what (the
# album, or the first track) and how many; the notices channel words the
# causes it may have. A batch it takes whole ends it.

_refused: Optional[dict] = None


def note_refused(items: list, total: int, *, stream: bool) -> bool:
    """HQPlayer refused `items` of a batch of `total` tracks it was handed.
    Returns whether the notices changed: a batch is a notice of its own."""
    global _refused
    albums = {it.album for it in items}
    what = items[0].album if len(albums) == 1 and items[0].album else items[0].title
    with _lock:
        _refused = {"since": _iso(time.time()), "what": what, "count": len(items),
                    "total": total, "stream": stream}
    return True


def note_taken() -> bool:
    """HQPlayer took a batch whole: the last refusal's notice is over.
    Returns whether the notices changed."""
    global _refused
    with _lock:
        changed, _refused = _refused is not None, None
    return changed


def refused() -> Optional[dict]:
    with _lock:
        return dict(_refused) if _refused else None


def failing_run() -> Optional[dict]:
    """Three or more closed attempts in a row ending in the same failure — a
    dead output, as opposed to one track that would not play."""
    with _lock:
        closed = [(a.verdict, a.started, a.cleared) for a in reversed(_ring) if a.verdict]
    run: list = []
    for v, started, cleared in closed:
        if v["code"] == "interrupted":
            continue
        if v["code"] == "played" or cleared or (run and v["code"] != run[0][0]["code"]):
            break
        run.append((v, started))
    if len(run) < 3:
        return None
    return {"code": run[0][0]["code"], "title": run[0][0]["title"],
            "next": run[0][0]["next"], "count": len(run), "since": _iso(run[-1][1])}


def _run_changed() -> bool:
    global _run_key
    run = failing_run()
    key = (run["code"], run["since"], run["count"]) if run else None
    with _lock:
        changed = key != _run_key
        _run_key = key
    return changed


def attempts() -> list:
    with _lock:
        return list(reversed(_ring))


def find(attempt_id: int) -> Optional[Attempt]:
    with _lock:
        return next((a for a in _ring if a.id == attempt_id), None)


def summary(a: Attempt) -> dict:
    with _lock:
        f = a.facts or {}
        item = f.get("item") or {}
        return {"id": a.id, "started": _iso(a.started), "intent": a.intent,
                "slot": f.get("slot"), "title": item.get("title") or "",
                "artist": item.get("artist") or "", "mode": f.get("mode"),
                "verdict": dict(a.verdict) if a.verdict else None}


def public(a: Attempt, *, redact: bool) -> dict:
    with _lock:
        d = {"id": a.id, "started": _iso(a.started), "intent": a.intent, "end": a.end,
             "verdict": copy.deepcopy(a.verdict), "facts": copy.deepcopy(a.facts),
             "hqp_log": copy.deepcopy(a.hqp_log)}
    return _redacted(d) if redact else d


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# -- The verdict --------------------------------------------------------------------

def _final_refusal(steps: list) -> Optional[dict]:
    """The transport command whose LAST answer was a refusal, if any."""
    last: dict = {}
    for s in steps:
        if s["command"] in TRANSPORT:
            last[s["command"]] = s
    return next((s for s in last.values() if s["result"] == "Error"), None)


def _load(ticks: list) -> Optional[dict]:
    """The host could not keep up: slower than real time on three ticks in
    a row (a single dip — a seek, a burst of other load — plays through), or
    the output buffer draining while the input held (a draining INPUT is a
    starving stream, not the DSP). The first two seconds are start-up."""
    playing = [t for t in ticks if t["state"] == "playing"]
    if not playing:
        return None
    late = [t for t in playing if t["ts"] >= playing[0]["ts"] + 2.0]
    run: list = []
    for t in late:
        run = run + [t["speed"]] if t.get("speed") and 0 < t["speed"] < 1.0 else []
        if len(run) >= 3:
            return {"speed": round(min(run), 2)}
    outs = [t["out_fill"] for t in late if t.get("out_fill") is not None]
    # A negative input fill is no input buffer at all: HQPlayer reads a file
    # it opened itself (-1, measured on Desktop 6.2.3) — nothing to starve.
    ins = [t["in_fill"] for t in late if t.get("in_fill") is not None and t["in_fill"] >= 0]
    tail = outs[-3:]
    if (len(tail) == 3 and all(b < a for a, b in zip(tail, tail[1:]))
            and tail[-1] < 0.1 * max(outs) and (not ins or min(ins[-3:]) >= 0.5 * max(ins))):
        return {"output_fill": tail[-1]}
    return None


def _ran_out(tick: dict) -> bool:
    return bool(tick.get("length")) and tick["position"] >= tick["length"] - 2.0


def _dsp_changed(ticks: list) -> bool:
    """Someone changed the DSP while the track was loaded — HQPlayer may
    stop the transport for the build (6.2.3 on Windows did; 6.2.4 plays
    on), so a stop that follows is the owner's, whoever made the change
    (HQPlayer's own window included). Only ticks of a loaded track count:
    before it loads, the previous track's rate shows."""
    loaded = {(t["mode"], t["filter"], t["shaper"], t["rate"]) for t in ticks
              if t["track"] >= 1 and t["length"] and t["filter"]}
    return len(loaded) > 1


def verdict(f: dict, log: Optional[dict] = None) -> dict:
    """The cause, from the facts an attempt gathered (playback.hqp_backend
    _finalize; the shape the fixtures in tests/fixtures/hqp_traces record)
    and HQPlayer's log once it was read (attach_log). Positive evidence of
    playback is weighed before any absence of evidence: a Next onto a track
    HQPlayer pre-buffered for gapless sends no new GET, and a playlist that
    drifted mid-append is still ours."""
    steps, ticks = f.get("steps") or [], f.get("ticks") or []
    t0 = f.get("t0") or f["started"]
    adv = f.get("advance") or {}
    # An advance's entry is asked for once HQPlayer takes it, after the tail
    fetch_by = adv.get("deadline") or t0 + FETCH_WINDOW_S
    pl = f.get("playlist") or {}
    mode = f.get("mode")
    ho = f.get("handover") or {}
    since = (ho.get("ts") or f["started"]) - 1.0
    hits = ([h for h in f["proxy"] if h["ts"] >= since]
            if mode in HTTP_MODES and f.get("proxy") is not None else None)
    playing = [t for t in ticks if t["state"] == "playing"]
    began = (started(ticks, adv.get("serial"))
             and (not pl.get("read") or (pl.get("at_slot_ours")
                                         and (not f.get("expected_uri") or pl.get("at_slot_expected"))))
             and (hits is None or any(h.get("bytes", 0) > 0 for h in hits)))
    stopped_after = (began and ticks[-1]["state"] == "stopped"
                     and not _ran_out(playing[-1]))
    owner = f["end"] in ("owner", "superseded", "shutdown") or _dsp_changed(ticks)
    if began:
        load = _load(ticks)
        if load:
            return _v("too_slow", f, **load)
        # A stop the owner caused (or another intent) is not HQPlayer giving up.
        if not stopped_after or owner:
            return _v("played", f)

    # A command that never got an answer explains a play that did not start —
    # never one that did: a lost answer to a Stop before a good Play is noise.
    lost = next((s for s in steps if s["result"] in ("refused", "lost")), None)
    if f.get("error") or lost:
        return _v("unreachable", f, message=f.get("error") or lost["message"])

    # HQPlayer holds something not ours where it plays: another controller
    # (Roon) drives it, and our file is simply not asked for — read before
    # the proxy's silence could be taken for a network fault.
    if pl.get("read") and pl.get("at_slot") and not pl.get("at_slot_ours"):
        return _v("external", f)

    if hits is not None:
        bad = next((h for h in hits if (h.get("status") or 0) in PROXY_ERRORS), None)
        if bad:
            return _v("proxy_error", f, status=bad["status"])
        add_failed = ho.get("ok") is False
        watched = f["closed"] >= fetch_by
        if not began and (add_failed or watched) and not any(h["ts"] <= fetch_by for h in hits):
            return _v("no_fetch", f)

    if ho.get("ok") is False:
        if ho.get("result") in ("refused", "lost"):
            return _v("unreachable", f, message=ho.get("message") or "")
        return _v("rejected", f, quote=ho.get("message") or None)
    refusal = _final_refusal(steps)
    if refusal:
        return _v("rejected", f, quote=refusal["message"] or None, command=refusal["command"])
    if pl.get("read") and f.get("expected_uri") and pl.get("expected_present") is False:
        return _v("rejected", f, not_kept=True)
    if pl.get("foreign"):
        return _v("external", f)
    if owner:
        return _v("interrupted", f)
    if ticks and not began and ticks[-1]["state"] == "stopped":
        refused = _rate_refused(f, log)
        if refused is not None:
            return _v("rate_refused", f, **refused)
    if ticks and (not playing or ticks[-1]["state"] == "stopped"):
        return _v("not_played", f, stopped_after=stopped_after)
    return _v("unknown", f)


# HQPlayer's line for a refused rate combination, the source's rate first
# (_CAUSES rate_filter finds it in the log)
_RATE_LINE = re.compile(r"Requested filter not possible with this rate combination (\d+)/(\d+)[^\n]*")


def rate_facts(inputs: "rc.Inputs", source: Optional[int], queue: Optional[Mapping[int, int]]) -> dict:
    """The rate check's inputs as an attempt's facts keep them (JSON: the
    traces record them, a warrant's playback scope carries them whole), with
    the PCM rates of the queue from the attempt's slot to its end — tracks
    by rate, None when they were not read."""
    f = inputs.nx
    return {"inputs": {"mode": inputs.mode, "rate": inputs.rate, "adaptive": inputs.adaptive,
                       "filter": {"name": f.name, "ratio": f.ratio, "up": f.up} if f else None,
                       "rates": list(inputs.rates), "modulator": inputs.modulator,
                       "refused": {str(r): fs for r, fs in inputs.refused.items()},
                       "engine": inputs.engine},
            "source": source,
            "queue": {str(r): n for r, n in queue.items()} if queue is not None else None}


def _rate_check(rate: Optional[dict], source: Optional[int]) -> Optional["rc.Check"]:
    """The rate check over an attempt's one source, by the filter HQPlayer
    ran it with (both slots, its own choice); None without its inputs. The
    rate its fix names is the queue's to say: one only when every PCM track
    from the attempt's slot to the queue's end is refused, all of one
    family (hqp_rate_check._fix_rate) — none when the facts hold no queue."""
    d = (rate or {}).get("inputs")
    if not d or not source:
        return None
    f = d["filter"]
    filt = rc.Filter(f["name"], f["ratio"], f["up"]) if f else None
    inputs = rc.Inputs(d["mode"], d["rate"], d["adaptive"], filt, filt, tuple(d["rates"]), d["modulator"],
                       {int(r): fs for r, fs in d["refused"].items()}, d["engine"])
    fix = None
    if rate.get("queue"):
        ahead = {int(r): n for r, n in rate["queue"].items()}
        ahead.setdefault(source, 1)
        fix = rc.check(inputs, rc.Sources(ahead, first=source)).fix_rate
    return rc.check(inputs, rc.Sources({source: 1}, first=source))._replace(fix_rate=fix)


def names_cause(rate: Optional[dict]) -> bool:
    """Whether the rate check names why an attempt's track stopped at its
    start — the attempt then closes at once (Attempt.stopped_at_start)."""
    check = _rate_check(rate, (rate or {}).get("source"))
    return bool(check and check.groups)


def _rate_refused(f: dict, log: Optional[dict]) -> Optional[dict]:
    """rate_refused's words for an attempt that never moved and stopped: the
    rate check's cause, the filter's or the modulator's, and HQPlayer's line
    when its log was read — or that line alone; None with neither. The
    source is the slot's rate as HQPlayer reported it or its file row gave
    it (_facts), else the line's."""
    cause = (log or {}).get("cause") or {}
    line = _RATE_LINE.search(cause.get("line") or "") if cause.get("code") == "rate_filter" else None
    rate = f.get("rate") or {}
    source = rate.get("source") or (int(line.group(1)) if line else None)
    check = _rate_check(rate, source)
    if check is None:
        mode = rc.mode_kind(next((t["mode"] for t in reversed(f.get("ticks") or []) if t.get("mode")), None))
        check = rc.check(rc.Inputs(mode, 0, None, None, None), rc.Sources({}))
    words = rc.verdict(check, source, line.group(0) if line else None)
    if words is None:
        return None
    return {**words, "cause": check.groups[0].cause if check.groups else "line"}


def dsp_line(f: dict) -> str:
    t = next((t for t in reversed(f.get("ticks") or []) if t.get("filter") or t.get("mode")), None)
    matrix = ((f.get("context") or {}).get("dsp") or {}).get("matrix_profile")
    parts = [t["mode"], t["filter"], t["shaper"], rate_name(t["rate"]) if t["rate"] else ""] if t else []
    if matrix:
        parts.append(f"matrix {matrix}")
    return " · ".join(p for p in parts if p)


def _v(code: str, f: dict, **detail) -> dict:
    ctx = f.get("context") or {}
    media = ctx.get("media_url") or "the media proxy"
    hqp = ctx.get("hqplayer") or "its address"
    dsp = dsp_line(f)
    mode = f.get("mode")
    quote = detail.get("quote")
    if code == "unreachable":
        title = "HQPlayer did not answer"
        sentence = f"Sautium could not reach HQPlayer's control port at {hqp} ({detail['message']})."
        nxt = ("Start HQPlayer, or check that it runs at that address. An HQPlayer "
               "Embedded in trial mode stops every 30 minutes and must be restarted.")
    elif code == "played":
        title, sentence, nxt = "Played", "HQPlayer played it.", ""
    elif code == "too_slow":
        title = "HQPlayer cannot keep up"
        how = (f"processing ran at {detail['speed']}× real time" if "speed" in detail
               else "its output buffer ran dry while the input kept up")
        sentence = f"It started, but {how} — the host is too slow for this DSP setting{f' ({dsp})' if dsp else ''}."
        nxt = ("Choose a lighter filter or modulator, or a lower output rate, on the "
               "HQPlayer screen.")
    elif code == "proxy_error":
        status = detail["status"]
        title = "Sautium's media server could not serve it"
        if status == 404:
            sentence = (f"HQPlayer asked for the file and the media server answered 404: the "
                        "file was not registered — this happens right after Sautium restarts, "
                        "before the queue is restored.")
            nxt = "Press play again."
        elif status == 500:
            sentence = ("HQPlayer asked for the file and the media server answered 500: the "
                        "file is missing on this machine, or its CUE cut failed.")
            nxt = "Check that the music folder is mounted and the file still exists, then rescan."
        elif status in (502, 504):
            sentence = (f"The stream's provider {'failed' if status == 502 else 'timed out'} "
                        f"(HTTP {status}) when HQPlayer asked for it.")
            nxt = "Try again later, or play another copy."
        else:
            sentence = (f"The media server answered {status}, which its file URLs never do — "
                        "a bug in Sautium.")
            nxt = "Report it (More → Support)."
    elif code == "no_fetch":
        title = "HQPlayer never asked for the file"
        sentence = (f"HQPlayer was handed the file's address on Sautium's media server and "
                    f"never asked for it — it cannot reach {media}.")
        port = media.rsplit(":", 1)[-1]
        nxt = (f"Check that HQPlayer's machine is on the same network as this one, that a "
               f"firewall on this computer lets TCP {port} in, and on a Docker node that "
               f"port {port} is forwarded to the container.")
    elif code == "rejected":
        title = "HQPlayer refused the file"
        if detail.get("not_kept"):
            sentence = ("HQPlayer accepted the file but did not keep it in its playlist — it "
                        "could not open it.")
        elif quote and "Empty transport" in quote:
            sentence = "HQPlayer's playlist was empty when Play arrived: the file was not taken."
        elif quote:
            sentence = f"HQPlayer refused it: “{quote}”."
        else:
            sentence = "HQPlayer refused it without saying why."
        if mode == "held":
            nxt = ("This copy lives in HQPlayer's own library and is no longer at that path — "
                   "rescan its library (HQPlayer screen → Library → Rescan).")
        elif mode == "path":
            nxt = "Check that the file exists and that HQPlayer can read that drive."
        else:
            nxt = "HQPlayer's log names the reason."
    elif code == "external":
        title = "Another controller is driving HQPlayer"
        sentence = ("HQPlayer's playlist holds entries Sautium did not put there — another app "
                    "(Roon?) or HQPlayer's own window is in control.")
        nxt = "Stop playback in that app, then start the album or track again here."
    elif code == "not_played":
        title = "HQPlayer could not play it"
        got = "fetched the stream" if mode in HTTP_MODES else "opened the file"
        if detail.get("stopped_after"):
            sentence = f"HQPlayer {got} and started, then stopped on its own."
        else:
            sentence = (f"HQPlayer {got} but never started playing — a decoding problem, or "
                        "the output could not open at the required rate.")
        nxt = f"HQPlayer's log names the reason.{f' DSP: {dsp}.' if dsp else ''}"
    elif code == "rate_refused":
        # hqp_rate_check's words (_rate_refused), its cause kind beside them
        title, sentence, nxt = detail.pop("title"), detail.pop("sentence"), detail.pop("next")
    elif code == "interrupted":
        title = "Interrupted"
        sentence = "Another action took over before it could play."
        nxt = ""
    else:
        title = "No clear cause"
        sentence = "Nothing Sautium saw explains it."
        nxt = "HQPlayer's log may say more."
    out = {"code": code, "title": title, "sentence": sentence, "next": nxt}
    out.update({k: v for k, v in detail.items() if v is not None})
    if dsp and code in ("too_slow", "not_played", "rate_refused", "unknown"):
        out["dsp"] = dsp
    return out


# -- Redaction ------------------------------------------------------------------------

_TEXT_TOKEN = re.compile(r'(https?://[^\s/"]+/(?:file|preview)/)([A-Za-z0-9_-]{7,})')
# A path as HQPlayer prints one: a file:// URI, a UNC share, a drive path, or
# any absolute POSIX path two folders deep — running to a quote or the end of
# the line. Not inside a word or a URL (`…:8830/file/…`, `-1/0`).
_TEXT_PATH = re.compile(
    r'file://[^"\n]*'
    r'|\\\\[^\\\s"]+\\[^"\n]*'
    r'|(?<![\w/:.\\])(?:[A-Za-z]:[\\/]|/(?:[^/\s"]+/){2,})[^"\n]*')
_PATH_KEYS = frozenset({"uri", "expected_uri", "at_slot", "path", "where"})
_TEXT_KEYS = frozenset({"message", "quote", "sentence", "line", "note", "error"})
_TOKEN_KEYS = frozenset({"token"})        # the capability that serves a file


def redact_text(text: str) -> str:
    """Paths and media tokens inside free text — a log line, an error
    message — cut like redact_uri; a path runs to a quote or the line's end,
    as HQPlayer prints them."""
    text = _TEXT_TOKEN.sub(lambda m: m.group(1) + m.group(2)[:6] + "…", text)

    def cut(m) -> str:
        s = m.group(0)
        return "file://" + redact_path(s[7:]) if s.startswith("file://") else redact_path(s)
    return _TEXT_PATH.sub(cut, text)


def _redacted(obj, key: Optional[str] = None):
    if isinstance(obj, dict):
        return {k: _redacted(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        if key == "lines":
            return [redact_text(line) for line in obj]
        return [_redacted(v) for v in obj]
    if isinstance(obj, str):
        if key in _PATH_KEYS:
            return redact_uri(obj)
        if key in _TEXT_KEYS:
            return redact_text(obj)
        if key in _TOKEN_KEYS:
            return obj[:6] + "…"
    return obj


# -- HQPlayer's own log ----------------------------------------------------------------
# `<mark> YYYY/MM/DD HH:MM:SS <text>` on Desktop and Embedded alike, in
# HQPlayer's local time — which is not this node's (the Docker node runs UTC),
# so an attempt's lines are found by its URI, never by time.

_NOISE = re.compile(
    r"clUPnP::OnRequest\(\): clString::ToUInt\(\)"
    r"|NAA output (?:network Audio IPv6 support disabled|discovery from |discovered 0 Network Audio Adapters)"
    r"|Initializing processing for matrix pipeline|Matrix pipeline \d+:"
    r"|invalid album gain|clNetEngine::Disco\(\)"
    r"|Meter connection from |Metering (?:started|ended) from |Metering (?:enabled|disabled)"
    r"|^. \d{4}/\d\d/\d\d \d\d:\d\d:\d\d \t")

# Causes HQPlayer's log names — every pattern was seen in the maintainer's
# Desktop 5/6 and Embedded logs; anything else is shown raw.
_CAUSES = [
    ("file_not_found", re.compile(r"AddURI\(.*CreateFile\(.*cannot find the (?:path|file) specified"),
     "HQPlayer could not find the file at that path."),
    ("proxy_404", re.compile(r"AddURI\(.*GetHead\(\): 404|404 for range request"),
     "HQPlayer asked Sautium's media server for the file and got 404."),
    ("unsupported_stream", re.compile(r"AddURI\(\): unknown mime type"),
     "HQPlayer does not play this kind of stream."),
    ("fetch_failed", re.compile(r"GetHead\(\).*socket error"),
     "HQPlayer could not connect to Sautium's media server."),
    ("empty_transport", re.compile(r"Empty transport"),
     "HQPlayer's playlist was empty when Play arrived."),
    ("device_busy", re.compile(r"snd_pcm_open\(\): Device or resource busy"),
     "The audio device is busy — another program holds it."),
    ("device_missing", re.compile(r"snd_pcm_open\(\): No such device|ASIOInit\(\).*No device is connected"),
     "The audio device was not found — is the DAC on and connected?"),
    ("naa_lost", re.compile(r"NAA output .*not connected to adapter|NAA output network timeout"
                            r"|Audio Adapter keepalive", re.I),
     "HQPlayer lost its Network Audio Adapter."),
    # The filter or the modulator: the line names neither (rate_refused tells them apart)
    ("rate_filter", re.compile(r"Requested filter not possible with this rate combination"), rc.LOG_CAUSE),
    ("format_rejected", re.compile(r"StartAudioClient\(\): no formats available"),
     "The output device accepts none of the formats HQPlayer offered."),
    ("decode_failed", re.compile(r"FLAC__STREAM_DECODER_SEEK_ERROR"),
     "HQPlayer could not decode the stream."),
    ("open_failed", re.compile(r"SetTransport\(\): failed"),
     "HQPlayer could not open the file."),
]


def _noise(line: str) -> bool:
    return bool(_NOISE.search(line))


def classify(lines: list, uri: Optional[str] = None) -> Optional[dict]:
    """The cause the log names for this attempt: the first one from the last
    line that mentions its file onward (HQPlayer logs the URI it was handed,
    and why it refused it, on the add); when no line names the file, the
    NEWEST cause — the log is read right after the attempt, and an older one
    belongs to some earlier play."""
    needles = _needles(uri)
    start = next((i for i in range(len(lines) - 1, -1, -1)
                  if any(n in lines[i] for n in needles)), None) if needles else None
    scan = lines[start:] if start is not None else reversed(lines)
    for line in scan:
        for code, rx, sentence in _CAUSES:
            if rx.search(line):
                return {"code": code, "sentence": sentence, "line": line}
    return None


def _needles(uri: Optional[str]) -> list:
    if not uri:
        return []
    if uri.startswith("file://"):
        path = uri_to_file_path(uri)
        return [path, path.replace("/", "\\")]
    m = re.search(r"/(?:file|preview)/([^?/#]+)", uri)
    return [m.group(1)] if m else [uri]


def local_log_dir() -> Optional[Path]:
    from config import settings
    if settings.hqplayer_data_dir:
        return Path(settings.hqplayer_data_dir)
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "HQPlayer"
    if sys.platform == "darwin":
        return Path.home() / ".hqplayer"
    return None


def log_source(host: str, info: dict) -> dict:
    """Where this HQPlayer's log can be read from: a file on this machine, the
    web interface of an Embedded box, or nowhere from here."""
    import hqp_library
    from auth_hmac import is_own_address
    if is_own_address(host):
        d = local_log_dir()
        return {"kind": "local", "where": str(d) if d else None}
    if hqp_library.is_embedded((info or {}).get("product")):
        return {"kind": "web", "where": f"http://{host}:8088/log"}
    return {"kind": "none", "where": None}


def read_log(source: dict, info: dict, uri: Optional[str] = None) -> dict:
    """The last LOG_LINES meaningful lines of HQPlayer's log, read now, and the
    cause they name. Opens only HQPlayer's settings.xml and its log file."""
    out = {"source": source["kind"], "where": source.get("where"), "read_at": _iso(time.time()),
           "lines": [], "cause": None, "note": None}
    if source["kind"] == "none":
        out["note"] = ("HQPlayer runs on another computer; its log is there — on Windows "
                       "%LOCALAPPDATA%\\HQPlayer\\HQPlayer<version>Desktop.log, on macOS in "
                       "~/.hqplayer/, written while logging is on in HQPlayer's settings.")
        return out
    try:
        if source["kind"] == "local":
            out["lines"], out["note"], where = _read_local(info)
            out["where"] = where or out["where"]
        else:
            out["lines"] = _read_web(source["where"])
    except Exception as e:      # a file or a box outside this process: say what failed
        logger.warning("HQPlayer log not readable (%s): %s", source["kind"], e)
        out["note"] = f"HQPlayer's log could not be read: {e}"
    out["cause"] = classify(out["lines"], uri)
    return out


def _major(version: Optional[str]) -> Optional[int]:
    m = re.match(r"\s*(\d{1,2})\b", version or "")
    return int(m.group(1)) if m else None


_MOUNT_HINT = ("On a Docker node, set HQPLAYER_DATA_DIR in .env to HQPlayer's folder on the "
               "host (on Windows %LOCALAPPDATA%\\HQPlayer) and recreate the container.")


def _local_log(info: dict) -> tuple:
    """This machine's HQPlayer log: (path, note, where) — no path, and the
    note saying why, when there is none to read."""
    d = local_log_dir()
    if d is None:
        return None, f"No HQPlayer folder is known on this machine. {_MOUNT_HINT}", None
    major = _major((info or {}).get("version"))
    path = d / f"HQPlayer{major}Desktop.log" if major else None
    if path is None or not path.is_file():
        logs = sorted(d.glob("HQPlayer*Desktop.log"), key=lambda p: p.stat().st_mtime,
                      reverse=True)
        path = logs[0] if logs else None
    settings_xml = d / "settings.xml"
    if settings_xml.is_file():
        node = ET.parse(settings_xml).getroot().find(".//log")
        if node is not None and node.get("enabled") == "0":
            return None, "HQPlayer's log is off — turn logging on in HQPlayer's settings.", str(d)
    elif path is None:
        return None, f"Nothing of HQPlayer's in {d}. {_MOUNT_HINT}", str(d)
    if path is None:
        return None, f"No HQPlayer log in {d}.", str(d)
    return path, None, str(path)


def _read_local(info: dict) -> tuple:
    path, note, where = _local_log(info)
    return (_tail(path) if path is not None else []), note, where


def _tail(path: Path, want: int = LOG_LINES, cap: int = 4 * 1024 * 1024) -> list:
    """The last `want` meaningful lines, read backwards in blocks — HQPlayer's
    log only grows (hundreds of MB on a busy Desktop)."""
    kept: list = []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        end = pos = f.tell()
        rest = b""
        while pos > 0 and end - pos < cap and len(kept) < want:
            step = min(65536, pos)
            pos -= step
            f.seek(pos)
            lines = (f.read(step) + rest).split(b"\n")
            rest = lines[0] if pos > 0 else b""
            for raw in reversed(lines[1:] if pos > 0 else lines):
                line = raw.decode("utf-8", "replace").rstrip("\r")
                if line and not _noise(line):
                    kept.append(line)
                    if len(kept) >= want:
                        break
    return list(reversed(kept))


def _read_web(url: str, cap: int = 2 * 1024 * 1024) -> list:
    """The Embedded web interface's plain-text log page — verified 2026-10-02
    against HQPlayer OS (Embedded 6.1.0, engine 6.2.3): no login, the whole
    log since hqplayerd started."""
    import requests
    chunks: deque = deque()
    size = 0
    with requests.get(url, timeout=5, stream=True) as r:
        r.raise_for_status()
        for chunk in r.iter_content(65536):
            chunks.append(chunk)
            size += len(chunk)
            while size > cap and len(chunks) > 1:
                size -= len(chunks.popleft())
    lines = b"".join(chunks).decode("utf-8", "replace").splitlines()
    return [line for line in lines if line and not _noise(line)][-LOG_LINES:]


def bundle_view(source: dict, info: dict) -> dict:
    """Everything the `playback` warrant scope carries: every trace, the
    client's failed commands and a fresh tail of HQPlayer's log, every path
    cut to its last two components."""
    from hqplayer_client import HQPlayerClient
    return _redacted({
        "attempts": [public(a, redact=False) for a in attempts()],
        "failing": failing_run(),
        "client_errors": HQPlayerClient.last_errors(),
        "log": read_log(source, info),
    })
