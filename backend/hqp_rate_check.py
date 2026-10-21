"""Whether HQPlayer reaches its output rate from the tracks the queue still
has to play: the rule behind the HQPlayer screen's line, the notice
`hqplayer.rate_check` and the verdict `rate_refused`, and every word they
say.

HQPlayer takes a Play and stops at once on a track whose source rate the
filter that runs it, or in SDM mode its modulator, does not convert to the
chosen output rate; its log names the combination, the source's rate over
the output rate ("Requested filter not possible with this rate combination
…, stop"), or, from a filter it built first, "clHQPlayerEngine::Execute():
lInRate > lOutRate". The rule takes each distinct PCM source rate still to
play:

- Nothing is said in the mode [source] or at the rate Auto, where the chosen
  rate only sets a default and a ceiling (HQPlayer Desktop manual 6.1,
  §4.4), nor while HQPlayer's adaptive output rate is grayed or checked: it
  then picks the rate as it plays (§4.2) — grayed, the default rate or a
  lower one the filter and the DAC take; checked, a multiple of the
  source's base rate.
- In SDM mode the modulator comes first: an output rate the atlas measured
  it refused, for a source of the family the refusal was measured from, on
  the engine the atlas shows this HQPlayer's modulators from
  (hqp_atlas.Atlas.engine_for) — never stretched to another rate or family.
- Else the filter that runs the source (`running_slot`), by the ratio class
  HQPlayer describes it with: `2^x` does not reach an output rate above the
  source that is no power-of-two multiple of it, `Int` one that is no
  integer multiple; `Any` and `1:1` set no limit the rule reads (none, the
  `1:1` filter, plays a PCM track at its own rate whatever rate is set).
  HQPlayer 5 describes no class, and a description not read gives none:
  nothing is said.
- And what Sautium measured on HQPlayer engine 6.2.4 (UP_ENGINE, Desktop,
  PCM mode, 2026-10-19), said on an HQPlayer of that major alone, naming
  the engine: of the four filters tried whose class HQPlayer writes with
  "up", none was refused at 1:1 or upward, and every one but sinc-M was
  stopped under the source's rate (×½, the one downward ratio tried). As a
  rule, then, an "up" filter under the source's rate is refused ("down"),
  the others not tried included — but sinc-M, which converted 352.8 kHz to
  176.4 kHz and was never stopped, gets the class's words without a stop
  under a source of its family (`Check.notes`), and sinc-Mx, its twin,
  never tried there, nothing. FFT and FIR, without "up", were refused at
  1:1: said of those two names alone ("one"). Under the source's rate a
  filter without "up" is not judged: FFT, FIR and poly-sinc-ext2 played
  there.

Where the adaptive output rate is not known (an HQPlayer on another
machine), every claim is hedged. DSD and streamed tracks are not checked
before they play: a DSD file reaches the filter at a PCM rate HQPlayer
names only as it converts it — its log's "Rate:" (176.4 kHz for DSD64 on
6.2.4), never assumed here — and a stream's rate is known only once it
plays. After the fact a DSD track is judged in PCM mode by that rate, as
HQPlayer's log names it (`Sources.of_dsd`).

The words state HQPlayer's description, the arithmetic and what was
measured, never a reading of the class beyond it ("made for upsampling, not
for decimation" is the plain sense of the measured stops); a rate is named
as the HQPlayer screen names it
(hqp_atlas.rate_name), and a phrase is wire-ready, as hqp_atlas's are. Pure:
no database, no HQPlayer, no file.
"""

from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Tuple

from hqp_atlas import AND, engine_version, rate_name, wire

# -- The words -------------------------------------------------------------------
# Every string the rate check says, one place: {x} is a value from HQPlayer, the
# queue or the atlas file; the rest is literal.

FILTER_LINE = {
    "2^x": "HQPlayer describes {filter} as {ratio}: {out} is no power-of-two multiple of {of}, "
           "so HQPlayer will stop on the {tracks} tracks in the queue",
    "Int": "HQPlayer describes {filter} as {ratio}: {out} is no integer multiple of {of}, "
           "so HQPlayer will stop on the {tracks} tracks in the queue",
}
# What was measured on UP_ENGINE: an "up" filter under the source's rate, and
# FFT or FIR at 1:1 — the measurement said as such, its plain sense ("made for
# upsampling, not for decimation") Sautium's, never inside HQPlayer's description
DOWN_LINE = ("HQPlayer describes {filter} as {ratio}. Of the “up” filters tried on engine {engine}, it stopped "
             "all but sinc-M under the source's rate, so it will stop on the {tracks} tracks in the queue at {out}")
ONE_LINE = ("HQPlayer refused {filter} at the source's own rate (1:1) on engine {engine}, so it will stop on the "
            "{tracks} tracks in the queue")
# sinc-M under the source's rate, within its family: the class's words, the
# one downward conversion measured, and no stop
DOWN_NOTE = ("HQPlayer describes {filter} as {ratio}, and the {tracks} tracks in the queue are above {out}. "
             "Filters described with “up” are made for upsampling, not for decimation; on engine {engine} "
             "HQPlayer converted 352.8 kHz to 176.4 kHz with {filter} and did not stop.")
MODULATOR_LINE = ("HQPlayer refused {modulator} at {rate} on engine {engine}, so it will stop on the "
                  "{tracks} tracks in the queue")
FILTER_UNLESS = " unless its adaptive output rate is on"
MODULATOR_UNLESS = " unless its adaptive output rate picks another rate"
END = "."
FILTER_FIX = ("Pick a filter HQPlayer describes as Any, or turn on adaptive output rate in HQPlayer's "
              "own settings (Output){if_off}{or_rate}.")
IF_OFF = " if it is off"
OR_RATE = ", or pick {rate}"
DOWN_FIX = ("Filters described with “up” are made for upsampling, not for decimation: pick one HQPlayer "
            "describes without “up”, or as the output rate the tracks' own or a power-of-two multiple of it.")
# A verdict's: its one track, by the rate that reached the filter — its own (played
# 1:1 and ×2), or a DSD track's converted rate ×2 or more (1:1 from it was not tried)
VERDICT_DOWN_FIX = ("Filters described with “up” are made for upsampling, not for decimation: pick one HQPlayer "
                    "describes without “up”, or as the output rate {rates}.")
OWN_RATES = "{rate} or a power-of-two multiple of it"
STAGE_RATES = "twice {rate} or a higher power-of-two multiple of it"
ONE_FIX = "Pick another filter, or another output rate."
MODULATOR_FIX = "Pick another modulator, or another output rate."
NOT_CHECKED = {
    (True, False): "The DSD tracks in the queue are not checked.",
    (False, True): "The streamed tracks in the queue are not checked: their rate is known only once they play.",
    (True, True): "The DSD and streamed tracks in the queue are not checked.",
}
NOTICE_TITLE = "Some tracks in the queue cannot play at {out}"
VERDICT_TITLE = "HQPlayer refused the rate combination"
VERDICT_FILTER = {
    "2^x": "HQPlayer describes {filter} as {ratio}: {out} is no power-of-two multiple of {of}, "
           "and HQPlayer stopped on this track.",
    "Int": "HQPlayer describes {filter} as {ratio}: {out} is no integer multiple of {of}, "
           "and HQPlayer stopped on this track.",
    "down": "HQPlayer describes {filter} as {ratio}: {out} is under {of}, and HQPlayer stopped on this track.",
}
VERDICT_ONE = ("HQPlayer refused {filter} at the source's own rate (1:1) on engine {engine}, and it stopped on "
               "this {source} track.")
# A DSD track's source in a verdict: the PCM rate HQPlayer's log names it converted to
STAGE = "the {stage} HQPlayer converted this {dsd} track to"
VERDICT_MODULATOR = ("HQPlayer refused {modulator} at {rate} on engine {engine}, and it stopped on this "
                     "{source} track.")
VERDICT_LINE = "HQPlayer stopped on it: “{line}”."
VERDICT_LOG = " HQPlayer's log: “{line}”."
LINE_FIX = {"sdm": "Pick another filter, modulator or output rate.",
            "pcm": "Pick another filter or output rate."}
LOG_CAUSE = "HQPlayer refused this rate combination."
OR = " or "

BASE_LIMIT = 50_000       # a source under it runs the 1x filter (running_slot)
# The adaptive output rate's states in which HQPlayer picks the rate as it
# plays — grayed took 705.6 kHz for a 44.1 kHz source under a 768 kHz
# default and a 2^x filter (Desktop 6.2.4, 2026-10-18)
PICKS_RATE = frozenset({"grayed", "checked"})
# The engine "up" was measured on (the module's docstring): 44.1 kHz up ×2
# and at 1:1, 88.2 and 352.8 kHz at 1:1 and down ×½, DSD64 to 352.8 and
# 88.2 kHz, with closed-form, sinc-medium, poly-sinc-ext2-short and sinc-M
# ("up"), FFT, FIR, poly-sinc-ext2 and none in both filter slots, the
# adaptive output rate off
UP_ENGINE = "6.2.4"
# The one "up" filter HQPlayer converted downward with and never stopped there
UP_PLAYS = frozenset({"sinc-M"})
# sinc-M's twin — the same output, bit for bit, at the ratios the atlas
# measured — never tried downward: neither a stop nor sinc-M's words
UP_UNTRIED_TWIN = frozenset({"sinc-Mx"})
# The filters without "up" HQPlayer refused at 1:1 — the others of their
# classes (2^x, Int) were not tried
REFUSES_ONE = frozenset({"FFT", "FIR"})


# -- What runs, and what reaches ---------------------------------------------------

def mode_kind(name: Optional[str]) -> Optional[str]:
    """'pcm' or 'sdm' for a mode's name; None for [source]."""
    n = (name or "").upper()
    if n == "PCM":
        return "pcm"
    if "SDM" in n or "DSD" in n:
        return "sdm"
    return None


def same_family(rate_hz: int, src_rate: int) -> bool:
    """An output rate the source reaches by a whole power of two — its own
    family (44.1 kHz: 88.2 kHz … 44.1k × 1024); a rate of another family
    takes an asynchronous conversion."""
    high, low = max(rate_hz, src_rate), min(rate_hz, src_rate)
    if low <= 0 or high % low:
        return False
    ratio = high // low
    return ratio & (ratio - 1) == 0


def running_slot(nx: Any, x1: Any, src_rate: int) -> Any:
    """Which of HQPlayer's two filter slots runs a source: the 1x one when it
    is set (`x1` not None) and the source is under 50 kHz — 22.05 and 32 kHz
    as much as 44.1 and 48 (HQPlayer Desktop manual 6.1, §4.6: the 1x
    selection covers every source rate under 50 kHz, the base rates) — the
    Nx one otherwise."""
    return x1 if x1 is not None and src_rate < BASE_LIMIT else nx


def measured_on(engine: Optional[str]) -> Optional[str]:
    """UP_ENGINE when HQPlayer reports an engine of its major — what was
    measured there speaks there alone, as the atlas's modulators keep to
    their major (hqp_atlas.Atlas.engine_for); None for another major or
    none reported."""
    own = engine_version(engine)
    return UP_ENGINE if own and own[0] == engine_version(UP_ENGINE)[0] else None


# -- The rule ----------------------------------------------------------------------

class Filter(NamedTuple):
    name: str
    ratio: Optional[str]       # HQPlayer's class: "1:1", "Int", "Any", "2^x"; None: none read
    up: bool = False           # " up" after it (PCM mode)

    def described(self) -> str:
        return self.ratio + (" up" if self.up else "")


def refusal(f: Filter, src_rate: int, rate_out: int, measured: Optional[str], stage: bool = False) -> Optional[str]:
    """Why HQPlayer stops `f` converting `src_rate` to `rate_out`, by the
    class it describes `f` with and what `measured` (measured_on) holds:
    "class" — the class's arithmetic does not reach an output above the
    source; "down" — an "up" filter under the source's rate, sinc-M and its
    twin aside; "one" — FFT or FIR at 1:1, from a PCM source (`stage`: the
    rate HQPlayer converted a DSD track to, never tried at 1:1). None:
    nothing says it stops."""
    if rate_out < src_rate:
        return ("down" if measured and f.up and f.name not in UP_PLAYS and f.name not in UP_UNTRIED_TWIN
                else None)
    if rate_out == src_rate:
        return "one" if measured and not stage and f.name in REFUSES_ONE else None
    if f.ratio == "2^x" and not same_family(rate_out, src_rate):
        return "class"
    if f.ratio == "Int" and rate_out % src_rate:
        return "class"
    return None


class Inputs(NamedTuple):
    mode: Optional[str]                  # "pcm", "sdm"; None: [source], or not read
    rate: int                            # the chosen output rate, Hz; 0: Auto
    adaptive: Optional[str]              # its box: "not checked", "grayed", "checked"; None: not known
    nx: Optional[Filter]
    x1: Optional[Filter]                 # None: no 1x filter set
    rates: Tuple[int, ...] = ()          # HQPlayer's rate list, for the fix that names one
    modulator: Optional[str] = None      # SDM
    refused: Mapping[int, int] = {}      # its refused output rates → the source rate each was measured from
    engine: Optional[str] = None         # the atlas engine they were measured on
    measured: Optional[str] = None       # UP_ENGINE on an HQPlayer of its major (measured_on)


class Sources(NamedTuple):
    """The queue's slots from the one HQPlayer reads to the end, as
    playback.hqp_load.sources reads them."""
    pcm: Mapping[int, int]               # source rate → slots
    dsd: int = 0
    streamed: int = 0
    first: Optional[int] = None          # the PCM rate of the slot HQPlayer reads
    first_dsd: Optional[int] = None      # the DSD rate of that slot, when it holds a DSD file
    # A verdict's DSD track: its DSD rate, `pcm` holding the one rate HQPlayer's
    # log named it converted the track to
    of_dsd: Optional[int] = None


class Group(NamedTuple):
    cause: str                           # "modulator" | "filter"
    name: str
    ratio: str                           # the class as HQPlayer writes it; "" for a modulator
    sources: Tuple[int, ...]             # ascending
    why: str = ""                        # a filter's: "class", "down", "one" (refusal)


class Check(NamedTuple):
    mode: Optional[str]
    rate: int
    groups: Tuple[Group, ...]            # the modulator's first, then by their lowest source
    hedged: bool                         # the adaptive output rate not known
    dsd: bool                            # DSD tracks still to play, not checked
    streamed: bool                       # streamed ones, not checked
    started_refused: bool                # the slot HQPlayer reads is among the refused
    fix_rate: Optional[int]              # a rate of HQPlayer's list every refused track reaches
    engine: Optional[str]
    key: tuple                           # the setting: mode, rate, both slots, modulator, adaptive
    measured: Optional[str] = None       # UP_ENGINE where its measurements speak
    notes: Tuple[Group, ...] = ()        # sinc-M under the source's rate: the screen's words, no stop


def check(inp: Inputs, src: Sources) -> Check:
    """The rule (the module's docstring) over the source rates still to play."""
    key = (inp.mode, inp.rate, inp.x1.name if inp.x1 else None, inp.nx.name if inp.nx else None,
           inp.modulator, inp.adaptive)
    silent = inp.mode is None or not inp.rate or inp.adaptive in PICKS_RATE
    fs_in = inp.refused.get(inp.rate) if inp.mode == "sdm" and inp.modulator else None
    found: Dict[tuple, List[int]] = {}
    noted: Dict[tuple, List[int]] = {}
    for s in () if silent else sorted(src.pcm):
        if fs_in is not None and same_family(s, fs_in):
            found.setdefault(("modulator", inp.modulator, "", ""), []).append(s)
            continue
        f = running_slot(inp.nx, inp.x1, s)
        if f is None or not f.ratio:
            continue
        why = refusal(f, s, inp.rate, inp.measured, stage=src.of_dsd is not None)
        if why:
            found.setdefault(("filter", f.name, f.described(), why), []).append(s)
        elif inp.measured and f.up and f.name in UP_PLAYS and inp.rate < s and same_family(inp.rate, s):
            noted.setdefault(("filter", f.name, f.described(), "down"), []).append(s)
    groups = sorted((Group(*k[:3], tuple(v), k[3]) for k, v in found.items()),
                    key=lambda g: (g.cause != "modulator", g.sources[0]))
    refused = {s for g in groups for s in g.sources}
    return Check(inp.mode, inp.rate, tuple(groups), inp.adaptive is None,
                 not silent and src.dsd > 0, not silent and src.streamed > 0,
                 src.first in refused, _fix_rate(inp, src, groups), inp.engine, key, inp.measured,
                 tuple(Group(*k[:3], tuple(v), k[3]) for k, v in noted.items()))


def _base(hz: int) -> Optional[int]:
    """The base of the family a rate is of: 44.1k, 48k or 32k."""
    return next((b for b in (44100, 48000, 32000) if same_family(hz, b)), None)


def _fix_rate(inp: Inputs, src: Sources, groups: List[Group]) -> Optional[int]:
    """The rate the filter's fix names: only when every PCM track still to
    play is refused by the filter's class, all of one family, and HQPlayer
    lists that family's rate at the chosen rate's multiple — one every
    refused track reaches, no measured refusal stops there (FFT or FIR at
    1:1), and in SDM one the modulator was not measured refusing them at."""
    if not groups or any(g.why != "class" for g in groups):
        return None
    refused = sorted({s for g in groups for s in g.sources})
    if len(refused) != len(src.pcm):
        return None
    bases, own = {_base(s) for s in refused}, _base(inp.rate)
    if len(bases) != 1 or None in bases or own is None:
        return None
    other = bases.pop() * (inp.rate // own)
    if other == inp.rate or other not in inp.rates or other < refused[-1]:
        return None
    if any(refusal(running_slot(inp.nx, inp.x1, s), s, other, inp.measured) for s in refused):
        return None
    fs_in = inp.refused.get(other) if inp.mode == "sdm" and inp.modulator else None
    if fs_in is not None and same_family(refused[0], fs_in):
        return None
    return other


# -- The inputs from HQPlayer's answers ---------------------------------------------

def inputs(state: Mapping[str, Any], modes: List[dict], rates: List[dict], filters: List[dict],
           shapers: List[dict], *, adaptive: Optional[str], atlas: Any = None,
           engine: Optional[str] = None, published: bool = False) -> Inputs:
    """The rule's inputs from what HQPlayer answered: its State (indices into
    the lists of the mode it is in) and those lists, as hqplayer_client
    returns them — the one mapping the screen, the notice and the verdict
    share. `adaptive`: the adaptive output rate as HQPlayer saved it, by the
    manual's names for its box's states ("not checked", "grayed",
    "checked"), None where it is not known. `atlas` (hqp_atlas.Atlas) gives
    an SDM modulator's refused rates on the engine it shows HQPlayer's
    (`engine`, as GetInfo reports it, which also says whether what was
    measured on UP_ENGINE speaks: measured_on); `published`, only an entry
    whose release has a date, what the site published — a file at the
    shipped path may be one put there before its release, and then it gives
    nothing at all."""
    mode = mode_kind(next((m["name"] for m in modes if m["index"] == state.get("mode")), None))
    rate = next((r["rate"] for r in rates if r["index"] == state.get("rate")), 0)
    by_index = {f["index"]: f for f in filters}

    def slot(index: Optional[int]) -> Optional[Filter]:
        f = by_index.get(index)
        if f is None:
            return None
        parsed = f.get("parsed") or {}
        return Filter(f["name"], parsed.get("ratio"), bool(parsed.get("up")))

    modulator = (next((s["name"] for s in shapers if s["index"] == state.get("shaper")), None)
                 if mode == "sdm" else None)
    shown = atlas.engine_for(engine, "modulator", published=published) if atlas is not None and modulator else None
    found = atlas.lookup("modulator", modulator, shown, "sdm") if shown is not None else None
    entry = found.entry if found is not None else None
    if published and (entry is None or not atlas.releases[entry["release"]]):
        entry = shown = None
    refused = {int(r): int(v["fs_in"]) for r, v in entry["rates_refused"].items()} if entry is not None else {}
    x1 = state.get("filter1x")
    return Inputs(mode, rate, adaptive, slot(state.get("filterNx")),
                  slot(x1) if x1 is not None and x1 >= 0 else None,
                  tuple(r["rate"] for r in rates if r["rate"] > 0), modulator, refused, shown,
                  measured_on(engine))


# -- The words of a check ----------------------------------------------------------

def _names(rates: Tuple[int, ...], last: str) -> str:
    """Rates as the screen names them, ascending: "a", "a and b", "a, b and c"
    — or with "or"."""
    names = [rate_name(r) for r in rates]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + last + names[-1]


def _claim(c: Check, g: Group) -> str:
    """One group's sentence, as the screen and the notice say it."""
    if g.cause == "modulator":
        text = MODULATOR_LINE.format(modulator=g.name, rate=rate_name(c.rate), engine=c.engine,
                                     tracks=_names(g.sources, AND))
        return text + (MODULATOR_UNLESS if c.hedged else "") + END
    if g.why == "one":
        text = ONE_LINE.format(filter=g.name, engine=c.measured, tracks=_names(g.sources, AND))
    elif g.why == "down":
        text = DOWN_LINE.format(filter=g.name, ratio=g.ratio, engine=c.measured, out=rate_name(c.rate),
                                of=_names(g.sources, AND), tracks=_names(g.sources, AND))
    else:
        text = FILTER_LINE[g.ratio.split(" ")[0]].format(filter=g.name, ratio=g.ratio, out=rate_name(c.rate),
                                                         of=_names(g.sources, OR), tracks=_names(g.sources, AND))
    return text + (FILTER_UNLESS if c.hedged else "") + END


def _filter_fix(c: Check, why: str, hedged: bool) -> str:
    if why == "down":
        return DOWN_FIX
    if why == "one":
        return ONE_FIX
    return FILTER_FIX.format(if_off=IF_OFF if hedged else "",
                             or_rate=OR_RATE.format(rate=rate_name(c.fix_rate)) if c.fix_rate else "")


def lines(c: Check) -> Dict[str, List[List[Dict[str, str]]]]:
    """The HQPlayer screen's phrases, each list under the row it names —
    {"filter": [...], "modulator": [...]}; empty when there is nothing to
    say. A filter's claims come before their fixes, one a kind of refusal,
    and sinc-M's words after them; the tracks not checked are said after
    the last line."""
    out: Dict[str, List[List[Dict[str, str]]]] = {}
    for g in c.groups:
        out.setdefault(g.cause, []).append(wire([_claim(c, g)]))
    if "modulator" in out:
        out["modulator"].append(wire([MODULATOR_FIX]))
    for why in dict.fromkeys(g.why for g in c.groups if g.cause == "filter"):
        out["filter"].append(wire([_filter_fix(c, why, c.hedged)]))
    for g in c.notes:
        out.setdefault("filter", []).append(wire([DOWN_NOTE.format(
            filter=g.name, ratio=g.ratio, out=rate_name(c.rate), tracks=_names(g.sources, AND),
            engine=c.measured)]))
    if out and (c.dsd or c.streamed):
        out["filter" if "filter" in out else "modulator"].append(wire([NOT_CHECKED[(c.dsd, c.streamed)]]))
    return out


def notice(c: Check) -> Optional[Dict[str, Any]]:
    """The notice's words, served by the HQPlayer backend while a track of
    such a queue that can play plays or is paused: its title and the
    claims, the modulator's first; the fix is on the screen its Why? opens.
    None when there is nothing to say."""
    if not c.groups:
        return None
    return {"title": wire([NOTICE_TITLE.format(out=rate_name(c.rate))]),
            "text": [wire([_claim(c, g)]) for g in c.groups]}


def verdict(c: Check, source: Optional[int], line: Optional[str],
            dsd: Optional[int] = None) -> Optional[Dict[str, str]]:
    """The words of `rate_refused` for an attempt that never moved and
    stopped (playback.hqp_diagnostics): the rule's cause over its one source,
    with HQPlayer's log line when it was read, or that line alone; None with
    neither. Nothing is hedged: the stop is the evidence. `dsd`: a DSD
    track's own rate, `source` then the PCM rate HQPlayer's log named it
    converted the track to."""
    if not c.groups:
        if not line:
            return None
        return {"title": VERDICT_TITLE, "sentence": VERDICT_LINE.format(line=line),
                "next": LINE_FIX["sdm" if c.mode == "sdm" else "pcm"]}
    g = c.groups[0]
    # a DSD file whose tags give no DSD rate (WavPack's) is named DSD alone
    dsd_name = rate_name(dsd) if dsd else ""
    of = (STAGE.format(stage=rate_name(source), dsd=dsd_name if dsd_name.startswith("DSD") else "DSD") if dsd
          else rate_name(source))
    if g.cause == "modulator":
        sentence = VERDICT_MODULATOR.format(modulator=g.name, rate=rate_name(c.rate), engine=c.engine,
                                            source=rate_name(source))
        nxt = MODULATOR_FIX
    elif g.why == "one":
        sentence = VERDICT_ONE.format(filter=g.name, engine=c.measured, source=rate_name(source))
        nxt = ONE_FIX
    else:
        sentence = VERDICT_FILTER["down" if g.why == "down" else g.ratio.split(" ")[0]].format(
            filter=g.name, ratio=g.ratio, out=rate_name(c.rate), of=of)
        nxt = (VERDICT_DOWN_FIX.format(rates=(STAGE_RATES if dsd else OWN_RATES).format(rate=rate_name(source)))
               if g.why == "down" else _filter_fix(c, g.why, False))
    return {"title": VERDICT_TITLE, "sentence": sentence + (VERDICT_LOG.format(line=line) if line else ""),
            "next": nxt}
