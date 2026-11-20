"""
AB-1's choices that need no HQPlayer: who is compared, in what order, and
when a sitting stops (sautium-private AB-1-DESIGN §1.1, §4, §14.3–§14.6,
§15–§16). playback.hqp_ab plays what these decide.

**Who is compared.** A filter HQPlayer lists now, that this host keeps up
with at the track's rates (hqp_load class ok or tight — never one not
measured here), whose ratio class reaches the output rate, and — in the
chain, which waits for the atlas (Q1) — that the atlas measured in the
track's context; an identical group is heard once, by its fastest member
here, and never the champion's own. Each one left out is counted with its
reason. Filters only, in PCM and in SDM mode (§14.6).

**Find my filter: contrast first, then finer.** Three independent axes of
a filter in the context (§14.4, checked on the atlas's data in the record):
the phase type, where it cuts (the −3 dB point over the source's Nyquist)
and how long it rings (pre + post ringing at −100 dB, in source samples),
each normalised over the candidates by rank. A probe on an axis is the
candidate farthest from the champion on it and nearest on the others; the
answers move a bracket per axis — "better" moves the champion to the probe
and closes the side it left, "worse" closes the far side, "no difference"
closes the axis — and the next probe on that axis halves the distance to
the farthest candidate still open. The first axis is the one with the
largest contrast open. The brackets of a context are its logged duels
replayed (replay), so the next sitting resumes the search. This is an order
of questions: nothing here is shown, and nothing sorts a list by it.

**The pool.** Every filter a sitting may play is warmed up first (§2.3), so
the search runs on a pool: the probes its answer tree reaches first,
breadth-first, at most POOL_MAX of them (level A: a sitting of the whole
screened list would warm up for minutes).

**The chain** (§1.1): step one plays the champion then a probe; every
later step one new probe after the last fragment heard; after "worse" the
champion plays again first (check_step), and that replay is the focus
check — failed ("no difference" or "worse" on it), the answers since the
last passed check are set aside and a swap among them undone. "Better"
swaps at once (after the cost sheet when the probe costs clearly more,
§5.2); "no difference" keeps the faster one when it is more than 10 %
faster (Q13). CHAIN_STEPS probes a sitting (Q5).

**Two filters, blind** (§14.3): their current filter against one they chose,
in blocks of two rounds, each side first once a block in a drawn order;
stopped by two "no difference", a clear preference (a flat Beta prior,
P(p > ½) ≥ PAIR_SURE) or PAIR_CAP rounds.
"""

import math
import random
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

AXES = ("phase", "cutoff", "ringing")
# Where the ringing sits, as a position on the phase axis: linear rings both
# sides, intermediate between, minimum after only
PHASE_AT = {"linear": 0.0, "intermediate": 0.5, "minimum": 1.0}
CHAIN_STEPS = 6                   # Q5
POOL_MAX = 10
CHEAPER_BY = 1.10                 # Q13: "no difference" keeps one more than 10 % faster
COSTLY_UNDER = 0.5                # Q14: a probe under half the champion's speed …
PAIR_CAP = 10
PAIR_SAME_STOP = 2
PAIR_SURE = 0.95
# How much a probe's distance on the other axes weighs against its contrast
# on its own: "farthest on it, nearest on the others"
OTHERS_WEIGHT = 0.5

# The sitting's timing (§15.1, §16)
LEAD_MIN_S = 1.0                  # the cut's digital silence before the music
LEAD_ABOVE_S = 0.3                # the lead over the latest pause the warm-up saw
LEAD_SLIP_S = 0.1                 # a pause this near the music is a slip
MARGIN_S = 0.5
TOLERANCE_S = {"pcm": 0.15, "sdm": 0.3}       # Q10, Q18
LONG_SILENCE_S = 5.0              # §14's W20 from here


# -- who is compared ----------------------------------------------------------------

@dataclass(frozen=True)
class Filter:
    """One of HQPlayer's filters as a sitting sees it."""
    name: str
    order: int                         # its place in HQPlayer's list
    speed: Optional[float] = None      # how much faster than real time here (hqp_load)
    klass: Optional[str] = None        # 'ok' | 'tight' | 'no'; None: not measured here
    reaches: bool = True               # its ratio class converts the source to the output rate
    group: Optional[str] = None        # the atlas's identical group
    at: Optional[Mapping[str, float]] = None   # axis → value; None: not measured in the context

    @property
    def runs(self) -> bool:
        return self.klass in ("ok", "tight")


@dataclass
class Screened:
    candidates: List[Filter]
    # reason → the names it left out, in HQPlayer's order
    left_out: Dict[str, List[str]] = field(default_factory=dict)

    def leave(self, reason: str, name: str) -> None:
        self.left_out.setdefault(reason, []).append(name)


def _num(x: Any) -> Optional[float]:
    if isinstance(x, dict):
        x = x.get("v")
    return None if x is None else float(x)


def axes_of(context: Optional[Mapping[str, Any]], fs_in: int) -> Optional[Dict[str, float]]:
    """The three axes of a filter from its atlas context (the app subset's
    fields); None for a context the atlas holds no readings of."""
    if not context or "void" in context or context.get("phase") not in PHASE_AT:
        return None
    f3 = _num(context.get("f_3db_khz"))
    pre, post = _num(context.get("pre_ring_ms_100")), _num(context.get("post_ring_ms_100"))
    if f3 is None or pre is None or post is None:
        return None
    return {"phase": PHASE_AT[context["phase"]], "cutoff": f3 * 1000 / (fs_in / 2),
            "ringing": (pre + post) * fs_in / 1000}


def screen(filters: Sequence[Filter], champion: Filter, *, atlas: bool) -> Screened:
    """The chain's candidates (§4.1): listed, measured here and keeping up,
    reaching the rate, and with the atlas measured in the context; one of
    each identical group, its fastest, never the champion's group."""
    out = Screened([])
    eligible: List[Filter] = []
    for f in sorted(filters, key=lambda f: f.order):
        if f.name == champion.name:
            continue
        why = _refusal(f, atlas)
        if why is None and atlas and f.group is not None and f.group == champion.group:
            why = "identical"
        if why is None:
            eligible.append(f)
        else:
            out.leave(why, f.name)
    best: Dict[str, Filter] = {}
    for f in eligible:
        if atlas and f.group is not None and (f.group not in best
                                             or (f.speed or 0) > (best[f.group].speed or 0)):
            best[f.group] = f
    for f in eligible:
        if atlas and f.group is not None and best[f.group] is not f:
            out.leave("identical", f.name)
        else:
            out.candidates.append(f)
    order = {f.name: f.order for f in filters}
    for names in out.left_out.values():
        names.sort(key=lambda n: order.get(n, 0))
    return out


def seconds(filters: Sequence[Filter], champion: Filter, *, atlas: bool) -> Screened:
    """The pair's list "Against yours" (§14.3): what this host keeps up with
    at the track's rates, A–Z; with the atlas, an identical output to theirs
    left out."""
    out = Screened([])
    for f in sorted(filters, key=lambda f: f.name.casefold()):
        if f.name == champion.name:
            continue
        why = _refusal(f, atlas=False)
        if why is None and atlas and f.group is not None and f.group == champion.group:
            why = "identical"
        if why is None:
            out.candidates.append(f)
        else:
            out.leave(why, f.name)
    return out


def _refusal(f: Filter, atlas: bool) -> Optional[str]:
    if f.klass is None:
        return "not_measured_here"
    if not f.runs:
        return "too_slow"
    if not f.reaches:
        return "rate"
    if atlas and f.at is None:
        return "not_in_atlas"
    return None


# -- Find my filter: the search ---------------------------------------------------------

def normalised(filters: Iterable[Filter]) -> Dict[str, Dict[str, float]]:
    """Each filter's place on each axis, 0…1: the phase by its type, the
    other two by rank among these filters (the ringing spans three orders
    of magnitude; a rank keeps one long filter from flattening the rest)."""
    fs = [f for f in filters if f.at is not None]
    out: Dict[str, Dict[str, float]] = {f.name: {"phase": f.at["phase"]} for f in fs}
    for axis in ("cutoff", "ringing"):
        values = sorted({f.at[axis] for f in fs})
        span = max(len(values) - 1, 1)
        place = {v: i / span for i, v in enumerate(values)}
        for f in fs:
            out[f.name][axis] = place[f.at[axis]]
    return out


@dataclass
class Bracket:
    lo: float = -math.inf             # open below this
    hi: float = math.inf              # open above this
    closed: bool = False
    probed: bool = False


@dataclass
class Probe:
    name: str
    axis: str


class Search:
    """The brackets of one context over a pool of candidates, and the
    champion they are read around."""

    def __init__(self, candidates: Sequence[Filter], champion: Filter):
        self.by_name = {f.name: f for f in (*candidates, champion)}
        self.pool = [f.name for f in candidates]
        self.place = normalised(self.by_name.values())
        self.champion = champion.name
        self.brackets = {a: Bracket() for a in AXES}
        self.axis: Optional[str] = None
        self.heard: set = set()

    def copy(self, pool: Optional[Sequence[str]] = None) -> "Search":
        s = Search.__new__(Search)
        s.by_name, s.place, s.champion, s.axis = self.by_name, self.place, self.champion, self.axis
        s.pool = list(self.pool if pool is None else pool)
        s.brackets = {a: replace(b) for a, b in self.brackets.items()}
        s.heard = set(self.heard)
        return s

    def _open(self, axis: str) -> List[str]:
        b = self.brackets[axis]
        here = self.place.get(self.champion)
        if b.closed or here is None:
            return []
        champ_group = self.by_name[self.champion].group
        return [n for n in self.pool
                if n not in self.heard and n != self.champion and n in self.place
                and (champ_group is None or self.by_name[n].group != champ_group)
                and b.lo < self.place[n][axis] < b.hi and self.place[n][axis] != here[axis]]

    def _others(self, name: str, axis: str) -> float:
        here = self.place[self.champion]
        return sum(abs(self.place[name][a] - here[a]) for a in AXES if a != axis)

    def _contrast(self, axis: str) -> float:
        here = self.place[self.champion][axis]
        return max((abs(self.place[n][axis] - here) for n in self._open(axis)), default=0.0)

    def _pick(self, axis: str) -> Optional[str]:
        open_ = self._open(axis)
        if not open_:
            return None
        here = self.place[self.champion][axis]
        order = {n: self.by_name[n].order for n in open_}
        if not self.brackets[axis].probed:
            return max(open_, key=lambda n: (abs(self.place[n][axis] - here)
                                             - OTHERS_WEIGHT * self._others(n, axis), -order[n]))
        # Half the way to the farthest one still open, on its side
        far = max(open_, key=lambda n: (abs(self.place[n][axis] - here), -order[n]))
        side = 1 if self.place[far][axis] > here else -1
        want = abs(self.place[far][axis] - here) / 2
        same_side = [n for n in open_ if (self.place[n][axis] - here) * side > 0]
        return min(same_side, key=lambda n: (abs(abs(self.place[n][axis] - here) - want)
                                             + OTHERS_WEIGHT * self._others(n, axis), order[n]))

    def next_probe(self) -> Optional[Probe]:
        """The next candidate to hear and the axis it probes; None when no
        axis has one open."""
        if self.champion not in self.place:
            return None
        if self.axis is not None and not self.brackets[self.axis].closed:
            name = self._pick(self.axis)
            if name is not None:
                return Probe(name, self.axis)
            self.brackets[self.axis].closed = True
        ranked = sorted((a for a in AXES if self._open(a)),
                        key=lambda a: (-self._contrast(a), AXES.index(a)))
        if not ranked:
            return None
        self.axis = ranked[0]
        return Probe(self._pick(self.axis), self.axis)

    def record(self, axis: str, ref: str, probe: str, outcome: str, *, champion: str) -> None:
        """A duel on `axis`: `probe` heard after `ref`, answered 'better',
        'worse' or 'same'; `champion` the champion after it."""
        self.heard.add(probe)
        b = self.brackets[axis]
        b.probed = True
        if ref not in self.place or probe not in self.place:
            self.champion = champion
            return
        r, v = self.place[ref][axis], self.place[probe][axis]
        if outcome == "same":
            b.closed = True
        elif outcome == "worse":
            if v > r:
                b.hi = min(b.hi, v)
            else:
                b.lo = max(b.lo, v)
        elif v > r:
            b.lo = max(b.lo, r)
        else:
            b.hi = min(b.hi, r)
        self.champion = champion


def replay(search: Search, duels: Iterable[Mapping[str, Any]]) -> Search:
    """The context's earlier sittings, their counted duels with an axis in
    the order they were answered: the brackets they left. The champion is
    HQPlayer's own now, so it is kept; a name no longer a candidate moves
    nothing."""
    champion = search.champion
    for d in duels:
        if d.get("axis") is None or d.get("check_step") or not d.get("counted", True):
            continue
        outcome = {"second": "better", "first": "worse", "same": "same"}[d["answer"]]
        search.record(d["axis"], d["first_name"], d["second_name"], outcome, champion=champion)
    search.heard.clear()
    search.champion = champion
    search.axis = None
    return search


def pool(search: Search, *, steps: int = CHAIN_STEPS, limit: int = POOL_MAX) -> List[str]:
    """The filters the chain's answers reach first, breadth-first over the
    three answers to each probe, at most `limit` — what the warm-up plays."""
    out: List[str] = []
    level = [search.copy()]
    for _ in range(steps):
        nxt = []
        for s in level:
            probe = s.copy().next_probe()
            if probe is None:
                continue
            if probe.name not in out:
                out.append(probe.name)
                if len(out) >= limit:
                    return out
            for outcome in ("better", "worse", "same"):
                child = s.copy()
                p = child.next_probe()
                child.record(p.axis, child.champion, p.name, outcome,
                             champion=p.name if outcome == "better" else child.champion)
                nxt.append(child)
        level = nxt
    return out


# -- the chain ----------------------------------------------------------------------------

@dataclass
class Duel:
    n: int
    first: str                       # heard first
    second: str
    answer: str                      # 'first' | 'second' | 'same'
    axis: Optional[str] = None
    check: bool = False
    counted: bool = True
    swapped: Optional[bool] = None


@dataclass
class Step:
    kind: str                        # 'first' | 'next' | 'check'
    plays: List[str]                 # in the order heard
    probe: Optional[Probe] = None


class Chain:
    """The optician's chain over a search. The engine asks `step()`, plays
    it, and hands the answer to `answer()`; a 'better' that needs the cost
    sheet waits for `decide()`."""

    def __init__(self, search: Search, champion: Filter, *, steps: int = CHAIN_STEPS):
        self.search = search
        self.start = champion.name
        self.champion = champion.name
        self.checked = champion.name          # the champion of the last passed check
        self.since_check: List[int] = []      # duel numbers since that check
        self.duels: List[Duel] = []
        self.last: Optional[str] = None       # the fragment heard last
        self.steps_left = steps
        self.pending: Optional[Step] = None
        self.deciding: Optional[Duel] = None
        self.ended: Optional[str] = None      # 'done' | 'break'
        self.note: Optional[Dict[str, Any]] = None
        self._check_due = False

    def speed(self, name: str) -> Optional[float]:
        return self.search.by_name[name].speed

    def costly(self, probe: str) -> bool:
        """§5.2: the probe costs clearly more here — near real time, or under
        half the champion's speed (Q14)."""
        f, c = self.search.by_name[probe], self.search.by_name[self.champion]
        return f.klass == "tight" or (f.speed is not None and c.speed is not None
                                      and f.speed < COSTLY_UNDER * c.speed)

    def step(self) -> Optional[Step]:
        if self.ended is not None or self.deciding is not None:
            return None
        if self.pending is not None:
            return self.pending
        if self._check_due:
            self.pending = Step("check", [self.champion])
            return self.pending
        if self.steps_left <= 0:
            self.ended = "done"
            return None
        probe = self.search.next_probe()
        if probe is None:
            self.ended = "done"
            return None
        plays = [self.champion, probe.name] if self.last is None else [probe.name]
        self.pending = Step("first" if self.last is None else "next", plays, probe)
        return self.pending

    def _duel(self, first: str, second: str, answer: str, **kw) -> Duel:
        d = Duel(len(self.duels) + 1, first, second, answer, **kw)
        self.duels.append(d)
        self.since_check.append(d.n)
        return d

    def answer(self, outcome: str) -> Duel:
        """`outcome` — 'better', 'worse' or 'same' — of the step played."""
        step, self.pending = self.pending, None
        self.note = None
        if step.kind == "check":
            # The champion again against the one before it
            d = self._duel(self.last, self.champion, _ANSWER[outcome], check=True)
            self._check_due = False
            self.last = self.champion
            if outcome == "better":
                self.checked, self.since_check = self.champion, []
            else:
                for x in self.duels:
                    if x.n in self.since_check:
                        x.counted = False
                self.champion = self.checked
                self.ended = "break"
            return d
        ref = step.plays[-2] if len(step.plays) > 1 else self.last
        probe = step.probe
        self.steps_left -= 1
        d = self._duel(ref, probe.name, _ANSWER[outcome], axis=probe.axis)
        self.last = probe.name
        if outcome == "better":
            if self.costly(probe.name):
                self.deciding = d
                self.note = {"cost": True, "probe": self.speed(probe.name),
                             "yours": self.speed(self.champion),
                             "tight": self.search.by_name[probe.name].klass == "tight"}
                return d
            self._swap(d, True)
        elif outcome == "same":
            a, b = self.speed(probe.name), self.speed(self.champion)
            faster = a is not None and b is not None and a > CHEAPER_BY * b
            d.swapped = faster
            self.note = {"same": True, "faster": faster, "a": a, "b": b}
            self.search.record(probe.axis, ref, probe.name, "same",
                               champion=probe.name if faster else self.champion)
            if faster:
                self.champion = probe.name
        else:
            self._check_due = True
            self.search.record(probe.axis, ref, probe.name, "worse", champion=self.champion)
        return d

    def decide(self, make: bool) -> Duel:
        """The cost sheet's answer: Make it my filter, or Keep mine."""
        d, self.deciding = self.deciding, None
        self._swap(d, make)
        return d

    def _swap(self, d: Duel, make: bool) -> None:
        d.swapped = make
        self.search.record(d.axis, d.first, d.second, "better",
                           champion=d.second if make else self.champion)
        if make:
            self.champion = d.second

    def stop(self) -> None:
        """Stop tapped: a step still owed its answer is not counted, and a
        cost sheet left open keeps the champion."""
        if self.deciding is not None:
            self.decide(False)
        self.pending = None
        self.ended = self.ended or "stopped"


_ANSWER = {"better": "second", "worse": "first", "same": "same"}


# -- two filters, blind ------------------------------------------------------------------

def sure(k: int, m: int) -> float:
    """P(p > ½) under a flat Beta prior after `k` picks of one side and `m`
    of the other: Beta(k+1, m+1) ≤ ½ is Bin(k+m+1, ½) ≥ k+1."""
    n = k + m + 1
    return sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n


class Pair:
    """Their filter (`yours`) against one they chose (`other`)."""

    def __init__(self, yours: str, other: str, rng: Optional[random.Random] = None,
                 *, cap: int = PAIR_CAP):
        self.yours, self.other = yours, other
        self.rng = rng or random.Random()
        self.cap = cap
        self.block: List[Tuple[str, str]] = []
        self.duels: List[Duel] = []
        self.pending: Optional[Tuple[str, str]] = None
        self.ended: Optional[str] = None
        self.why: Optional[str] = None          # 'same' | 'clear' | 'cap'

    def order(self) -> Optional[Tuple[str, str]]:
        """The next round's order of hearing."""
        if self.ended is not None:
            return None
        if self.pending is None:
            if not self.block:
                self.block = [(self.yours, self.other), (self.other, self.yours)]
                self.rng.shuffle(self.block)
            self.pending = self.block.pop(0)
        return self.pending

    def answer(self, answer: str) -> Duel:
        """'first' | 'second' | 'same' — which of the two heard they preferred."""
        first, second = self.pending
        self.pending = None
        d = Duel(len(self.duels) + 1, first, second, answer)
        self.duels.append(d)
        k, m, same = self.counts()
        if same >= PAIR_SAME_STOP:
            self.ended, self.why = "done", "same"
        elif max(sure(k, m), sure(m, k)) >= PAIR_SURE:
            self.ended, self.why = "done", "clear"
        elif len(self.duels) >= self.cap:
            self.ended, self.why = "done", "cap"
        return d

    def counts(self) -> Tuple[int, int, int]:
        """(picks of yours, picks of the other, no difference)."""
        k = m = same = 0
        for d in self.duels:
            if d.answer == "same":
                same += 1
                continue
            picked = d.first if d.answer == "first" else d.second
            if picked == self.yours:
                k += 1
            else:
                m += 1
        return k, m, same

    def winner(self) -> Optional[str]:
        k, m, _ = self.counts()
        return self.yours if k > m else self.other if m > k else None

    def steady(self) -> bool:
        k, m, _ = self.counts()
        return max(sure(k, m), sure(m, k)) >= PAIR_SURE

    def stop(self) -> None:
        self.pending = None
        self.ended = self.ended or "stopped"


# -- the sitting's silence ----------------------------------------------------------------

@dataclass
class Timing:
    """A filter's warm-up (§15.1, §16): `d` its output delay; `start` from
    the end of the fragment before to its first frame (its pause's landing
    less `p`: a pause landing late changes neither); `p` the meter's
    position when its pause landed; `r` from its resume to its sound at the
    output beyond the lead its pause left (`lead − p`) — so a pause landing
    later, the lead played further, moves the resume and not the sound;
    `tail` how long after its last sound left the output its fragment's end
    showed (Status stopped and the meter quiet); the output rate it plays at."""
    d: float
    start: float
    r: float
    p: float
    tail: float
    rate: int


def lead(timings: Iterable[Timing]) -> float:
    return max([LEAD_MIN_S] + [t.p + LEAD_ABOVE_S for t in timings])


def silence(timings: Sequence[Timing], lead_s: float) -> float:
    """P: the silence before every fragment. The resume of the filter that
    waits longest is never asked for before its pause has landed — after
    the slowest end of a fragment before it, its own start to its first
    frame, the lead of `lead_s` and its resume's way to the output — plus
    MARGIN_S."""
    return (max(t.tail for t in timings) + max(t.start + lead_s + t.r for t in timings) + MARGIN_S)
