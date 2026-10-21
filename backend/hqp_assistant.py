"""The assistant's six HQPlayer filter and modulator tools: their table — the
names, descriptions and parameters both tool surfaces register — and every
word they say.

What a tool says is built here from what one route gathered
(routers/hqplayer.py, GET /api/hqplayer/assistant/{tool}): HQPlayer's
answers on its link, the atlas, and this host's own speeds. The in-process
tools and the MCP server both go through that route, so the two surfaces
cannot say a thing two ways.

The atlas here is the file shipped beside hqp_atlas, never the node's own
copy, and of it only what the site published: an entry speaks once its
release has a date. A tool says a measured fact only once Sautium published
it; before, it gives HQPlayer's own facts, this host's speeds and the words
that nothing is published. A number is the token the file wrote; this
host's speeds are written to two decimals, as the HQPlayer screen writes
them. Nothing is ranked or computed: lists keep HQPlayer's order, the
file's, or their labels', and two values stand side by side. A rate is
named as the HQPlayer screen names it (hqp_atlas.rate_name); a filter's
measured context is named as the site names it (hqp_atlas.context_label).
Nothing here logs.

Pure: no database, no HQPlayer, no file.
"""

from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Tuple

import hqp_atlas
import hqp_rate_check
from hqp_atlas import plain, rate_name, wire

# -- The words -------------------------------------------------------------------
# Every string the six say of their own, one place: {x} is a value from
# HQPlayer, the atlas file or this host; the rest is literal. The card's
# words, the engine line and the rate check's lines are hqp_atlas's and
# hqp_rate_check's, printed as they stand.

KIND_LINE = {"filter": "{name} — a filter HQPlayer lists in {mode} mode.",
             "modulator": "{name} — a modulator HQPlayer lists in SDM mode.",
             "dither": "{name} — a dither HQPlayer lists in PCM mode."}
MODE_NAME = {"pcm": "PCM", "sdm": "SDM"}
NOT_OUTPUT = ("HQPlayer is not the active audio output (currently: {output}): its filters and modulators are "
              "read against HQPlayer itself — the names it lists and the engine it reports — so nothing is said "
              "of them meanwhile.")
HELD = "{label}: HQPlayer's filters and modulators can be read again when it finishes."
UNREACHABLE = "HQPlayer is not answering ({error})."
NOT_LISTED = "HQPlayer did not list its {list} ({refusal})."
LISTS = {"filters": "filters", "shapers": "modulators or dithers", "modes": "modes", "rates": "rates"}
NEEDS = "Give {param}: this tool needs it."
NO_NAME = "HQPlayer lists no filter or modulator named “{name}” now."
CONTAINING = " Names that contain it: {names}."
SDM_ONLY = " HQPlayer lists its modulators only in SDM mode."
UNPUBLISHED = "Sautium has published no measurement of it."
OTHER_MAJOR = "{note} — another major version: no measured fact is given for it."
NO_ENGINE = "{note}: no measured fact is given for it."
NO_DESCRIPTION = "HQPlayer gives no description of it."
NO_MODE = "HQPlayer's lists name no mode now, so no measured fact is given."
CHECK = {"filter": "With {name} in both filter slots, at this output rate and queue:",
         "modulator": "With {name} as the modulator, at this output rate and queue:"}
LINK = "Measured properties: {href}"
INDENT = "  "

# compare
TWO = "{a} and {b} — two {kind}s HQPlayer lists in {mode} mode."
SAME_NAME = "Both names are {name}: give two different names."
TWO_KINDS = "{a} is a {kind_a} and {b} a {kind_b}: compare two filters or two modulators."
SIDE = "{name}: {said}"
SIDES = "{a} / {b}"
BOTH = "{v}, both"
NEITHER = "No group or approved pair holds both."
SIDE_BY_SIDE = "Properties, {a} / {b}:"
PER_RATE_BOTH = "Per rate, {a} / {b}:"
REFUSED_BOTH = "Rates HQPlayer refused: {a} / {b}"
NONE_REFUSED = "none"
NOT_GIVEN = "not given"
PAGE_OF = "Measured properties of {name}: {href}"

# This host's speeds: compare's CPU line and host_fit
CPU_AT = "On this HQPlayer, at {rate} with {other}, for a {source}:"
CPU_RUNS = "{name} runs at {speed}× ({samples}){unsettled} — {cls}"
CPU_NO_SOURCE = ("On this HQPlayer no speed is read for the setting now: it is read for the track playing, else "
                 "for a file next in the queue. hqplayer_host_fit lists each one measured.")
NOT_MEASURED_HERE = "{name}: not measured on this HQPlayer — the Benchmark on the HQPlayer screen measures it."
SAMPLES = ("{n} sample", "{n} samples")
UNSETTLED = ", the readings did not settle within 15 s"
CLASS = {"ok": "keeps up here (from {ok}×)", "tight": "tight here (from {no}× to {ok}×)",
         "no": "too slow here (under {no}×)"}
HOST = "On this HQPlayer — engine {engine}, CUDA {cuda}, {mode}{matrix}{convolution}:"
CUDA = {"full": "filters and convolution", "convolution": "convolution only", "off": "off", None: "not known"}
MATRIX = ", matrix {profile}"
CONVOLUTION = ", convolution on"
FROM_SOURCE = "From a {source}:"
CHANNELS = (" with {n} channel", " with {n} channels")
RUNS = "{rate} · {other}: runs at {speed}× ({samples}){unsettled}{dropout} — {cls}"
DROPOUT = ", and it dropped out here"
REFUSED_HERE = "{rate} · {other}: refused by HQPlayer when the Benchmark asked it"
UNSTARTED_HERE = "{rate} · {other}: never got going here when the Benchmark asked it"
AUTO = "auto"
MORE = "… and {n} more."
NO_RATE = "This HQPlayer holds no record at “{rate}” in {mode} mode. The rates it holds records at there: {rates}."
NO_RECORD = ("This HQPlayer holds no record of “{name}”{at} in {mode} mode: the Benchmark on the HQPlayer screen "
             "measures what HQPlayer lists.")
AT_RATE = " at {rate}"
BAD_MODE = "A mode is pcm or sdm."
NO_SUCH_MODE = "HQPlayer lists no {mode} mode."
NOTHING_HERE = "Nothing is measured on this HQPlayer: the Benchmark on the HQPlayer screen measures it."
OWN = "These speeds are this HQPlayer's own, measured while it played and by its Benchmark."

# differs_only_in and ab_pair
NO_PAIR = "No approved pair holds it."
NO_PAIR_AXIS = "No approved pair holds it on the {axis} axis."
LEFT_OUT = "Left out, as this HQPlayer does not list them now: {names}."
AXES = "An axis is one of: {axes}."
AXIS_OF = "{name} is a {kind}, and {axis} is an axis of {other} pairs."
AB_HEAD = "Approved pairs that differ only in one property — {axis}:"
AB_NEAR = "None of them holds {near}."
AB_WHERE = "Measured where both play: {where}."
AB_RATIO = "HQPlayer describes {a} as {ra} and {b} as {rb}."
AB_NONE = "Sautium has published no pair on this axis."
AB_STEPS = (
    "How to compare them by listening:",
    "1. One passage for both: the same track, from the same moment, at the same volume.",
    "2. One mode and one output rate for both — one where both were measured playing. Switch modes before the "
    "comparison, never between its sides.",
    "3. Do not know which one plays: let someone else switch, or let a coin set the order and look only "
    "afterwards. A switch may make HQPlayer pause to set up — a pause both sides share, which tells that a switch "
    "happened, not which.",
    "4. Name the one you heard, or say you could not tell. Could not tell is an answer: then the CPU each takes on "
    "this HQPlayer decides (hqplayer_host_fit).",
    "5. Repeat it a few times: one answer can be chance.")
# The kind of name each axis of the file's pairs holds
AXIS_KIND = {"phase": "filter", "apodizing": "filter", "length": "filter", "order": "modulator",
             "ec": "modulator", "adaptive": "modulator", "generation": "modulator", "tuning": "modulator",
             "multibit": "modulator"}

# atlas_link
NO_PAGE = "Sautium has published no page of it."

# -- The table -------------------------------------------------------------------


class Param(NamedTuple):
    name: str
    description: str
    required: bool = False


class Tool(NamedTuple):
    name: str
    description: str
    params: Tuple[Param, ...]


_AXIS_PARAM = "One of phase, apodizing, length, order, ec, adaptive, generation, tuning, multibit"
TOOLS = (
    Tool("hqplayer_filter_facts",
         "What HQPlayer says of one of its filters or modulators — Signalyst's technical rating, the author's "
         "focus, the ratio class, a modulator's generation — and what Sautium has published of its measurements on "
         "the HQPlayer engine they were made on: properties per context or per rate, the names with the same "
         "output, the approved pairs it belongs to, history and the author's stated purpose with their sources, "
         "and the page's address. Refuses while HQPlayer is not the selected output.",
         (Param("name", "A filter or modulator name as HQPlayer lists it (hqplayer_get_settings).", True),)),
    Tool("hqplayer_compare",
         "Two filters, or two modulators, side by side: what HQPlayer says of each, what Sautium has published of "
         "their measurements, whether they give the same output, and how each runs on this HQPlayer. States "
         "differences; never ranks.",
         (Param("name_a", "The first filter or modulator, as HQPlayer lists it.", True),
          Param("name_b", "The second, of the same kind.", True))),
    Tool("hqplayer_differs_only_in",
         "The names that differ from this filter or modulator in exactly one property, from the approved pairs, "
         "with the measured size of the difference. axis narrows it to one: phase, apodizing, length, order, ec, "
         "adaptive, generation, tuning, multibit.",
         (Param("name", "A filter or modulator name as HQPlayer lists it.", True),
          Param("axis", _AXIS_PARAM + "; empty for every axis."))),
    Tool("hqplayer_ab_pair",
         "Approved pairs that differ in one property only, for a blind comparison by listening, with how to run "
         "it. near puts the pairs holding that name first. Pairs stay within one mode: PCM against SDM is not "
         "compared, as a DAC may play DSD at another level than PCM.",
         (Param("axis", _AXIS_PARAM + ".", True),
          Param("near", "A filter or modulator name whose pairs come first; empty for the current setting's."))),
    Tool("hqplayer_host_fit",
         "How a filter, modulator or dither runs on this HQPlayer's own host, as measured here while playing and "
         "by the Benchmark: speed over real time, samples, settled or not, CUDA, engine. Never another machine's "
         "speed.",
         (Param("name", "A filter, modulator or dither as HQPlayer runs it.", True),
          Param("rate", "An output rate as hqplayer_get_settings names it (705.6 kHz, DSD256) or in Hz; empty for "
                        "every rate measured."),
          Param("mode", "pcm or sdm; empty for the mode HQPlayer is in."))),
    Tool("hqplayer_atlas_link",
         "The address of the published page of this filter or modulator, or that none is published.",
         (Param("name", "A filter or modulator name as HQPlayer lists it.", True),)),
)
BY_SHORT = {t.name.removeprefix("hqplayer_"): t for t in TOOLS}

# What the route reads from HQPlayer for each tool (GetInfo and State always)
READS = {"filter_facts": ("modes", "filters", "shapers", "rates"),
         "compare": ("modes", "filters", "shapers", "rates"),
         "differs_only_in": ("modes", "filters", "shapers"),
         "ab_pair": ("modes", "filters", "shapers"),
         "host_fit": ("modes",),
         "atlas_link": ("modes", "filters", "shapers")}


class Read(NamedTuple):
    """What the route gathered for one call, or why it gathered nothing:
    another output chosen (`output`), the output lent (`held`), the link's
    own line while it refuses questions (`busy`), HQPlayer not reachable
    (`error`)."""
    output: Optional[str] = None
    held: Optional[str] = None
    busy: Optional[str] = None
    error: Optional[str] = None
    info: Optional[dict] = None
    state: Optional[dict] = None
    lists: Mapping[str, Optional[list]] = {}
    refused: Mapping[str, str] = {}
    # The mode HQPlayer's lists belong to: "pcm", "sdm", None when they say none
    mode: Optional[str] = None
    # The file at the shipped path (hqp_atlas.read(SHIPPED, None))
    atlas: Any = None
    # The rate check's own inputs (hqp_rate_check): the adaptive output rate,
    # and the queue's sources — None when the queue is not checked
    adaptive: Optional[str] = None
    sources: Any = None
    # compare: this HQPlayer's endpoint row, and the picker's marks with the
    # setting and source they are for (routers.hqplayer._headroom)
    endpoint: Optional[dict] = None
    headroom: Optional[dict] = None
    # host_fit: what was resolved and read (`fit`)
    host: Optional[dict] = None


def answer(tool: str, args: Mapping[str, str], read: Read) -> str:
    """The text `tool` gives for `args` from what the route gathered."""
    missing = next((p.name for p in BY_SHORT[tool].params if p.required and not args.get(p.name)), None)
    if missing:
        return NEEDS.format(param=missing)
    refusal = _refusal(read)
    if refusal:
        return refusal
    return "\n".join(_SAY[tool](args, read))


def _refusal(read: Read) -> Optional[str]:
    if read.output:
        return NOT_OUTPUT.format(output=read.output)
    if read.held:
        return HELD.format(label=read.held)
    if read.busy:
        return read.busy
    if read.error:
        return UNREACHABLE.format(error=read.error)
    return None


# -- Names, kinds, engines -----------------------------------------------------------


class Named(NamedTuple):
    name: str           # HQPlayer's own spelling
    kind: str           # filter | modulator | dither | shaper (a list of no known mode)
    row: Dict[str, Any]
    rows: List[Dict[str, Any]]


def _missing(read: Read, needs: Tuple[str, ...]) -> Optional[str]:
    gone = next((name for name in needs if read.lists.get(name) is None), None)
    return NOT_LISTED.format(list=LISTS[gone], refusal=read.refused.get(gone, "")) if gone else None


def _shaper_kind(mode: Optional[str]) -> str:
    return {"sdm": "modulator", "pcm": "dither"}.get(mode, "shaper")


def _pick(read: Read, wanted: str) -> Tuple[Optional[Named], Optional[str]]:
    """HQPlayer's name for `wanted`: exactly as written, else the one name
    equal to it in any case — a fact must not land on a neighbour — with its
    kind from the live lists. Else the miss, in words."""
    gone = _missing(read, ("filters", "shapers"))
    if gone:
        return None, gone
    lists = (("filter", read.lists["filters"]), (_shaper_kind(read.mode), read.lists["shapers"]))
    named = [Named(row["name"], kind, row, rows) for kind, rows in lists for row in rows]
    exact = [n for n in named if n.name == wanted]
    if not exact:
        exact = [n for n in named if n.name.lower() == wanted.lower()]
    if len(exact) == 1 or (exact and len({n.name for n in exact}) == 1):
        return exact[0], None
    near = [n.name for n in named if wanted.lower() in n.name.lower()][:10]
    miss = NO_NAME.format(name=wanted) + (CONTAINING.format(names=", ".join(near)) if near else "")
    return None, miss + (SDM_ONLY if read.mode == "pcm" else "")


def _engine(read: Read) -> Optional[str]:
    return (read.info or {}).get("engine")


def _no_engine(atlas: Any, engine: Optional[str], kind: str) -> str:
    """Why no measured fact of `kind` is given on this HQPlayer's engine:
    another major, or none read."""
    note = plain(wire(atlas.engine_note(engine, kind, published=True)))
    return (OTHER_MAJOR if hqp_atlas.engine_version(engine) else NO_ENGINE).format(note=note)


class Facts(NamedTuple):
    """A name as the assistant may speak of it: the status lines of the
    engine and the mode, the name's own (published nothing, not measured,
    what HQPlayer lists a filter as in SDM mode), its card, and what the
    atlas found — None where nothing published speaks."""
    status: List[str]
    own: Optional[str]
    card: Dict[str, Any]
    found: Any


def _facts(read: Read, n: Named) -> Facts:
    engine, atlas = _engine(read), read.atlas
    alone = hqp_atlas.card(None, n.kind, n.name, n.rows, engine, read.mode)
    if read.mode is None:
        return Facts([NO_MODE], None, alone, None)
    if atlas is None or n.kind not in hqp_atlas.KINDS or not atlas.speaks_of(engine, n.kind, published=True):
        return Facts([], UNPUBLISHED, alone, None)
    shown = atlas.engine_for(engine, n.kind, published=True)
    if shown is None:
        return Facts([_no_engine(atlas, engine, n.kind)], None, alone, None)
    found = atlas.lookup(n.kind, n.name, shown, read.mode, published=True)
    card = hqp_atlas.card(atlas, n.kind, n.name, n.rows, engine, read.mode, published=True)
    status = [plain(line) for line in card["status"]]
    if found.state in ("measured", "refused"):
        return Facts(status, None, card, found)
    # The card's last line is the name's own: not measured, or what HQPlayer
    # lists it as in SDM mode
    return Facts(status[:-1], status[-1], card, None if found.state == "not-measured" else found)


def _kind_line(n: Named, mode: Optional[str]) -> List[str]:
    if mode is None:
        return []
    return [KIND_LINE[n.kind].format(name=n.name, mode=MODE_NAME[mode])]


# -- The card as text ------------------------------------------------------------------


def _sections(card: Dict[str, Any], n: Named) -> List[str]:
    """The card's sections as lines: a section's head, its items one level
    in; a table row by row; a disclosure opened in full."""
    out = [NO_DESCRIPTION] if not n.row.get("description") and n.kind in hqp_atlas.KINDS else []
    for s in card["sections"]:
        if s["head"]:
            out += [f"{s['head']}:"] + _items(s["items"], INDENT)
        else:
            out += _items(s["items"], "")
    return out


def _items(items: List[Dict[str, Any]], pad: str) -> List[str]:
    out = []
    for it in items:
        if "term" in it:
            out.append(f"{pad}{it['term']}: {plain(it['def'])}")
        elif "line" in it or "note" in it:
            out.append(pad + plain(it["line"] if "line" in it else it["note"]))
        elif "more" in it:
            out += [f"{pad}{it['more']}:"] + _items(it["items"], pad + INDENT)
        else:
            out += _table(it["table"], pad)
    return out


def _table(t: Dict[str, Any], pad: str) -> List[str]:
    head = " | ".join(t["columns"])
    head = f"{t['corner']}, {head}" if t["corner"] else head
    return [f"{pad}{head}:"] + [f"{pad}{INDENT}{r['label']}: " + " | ".join(plain(c) for c in r["cells"])
                                for r in t["rows"]]


# -- The rate check (hqp_rate_check) ------------------------------------------------


def _check(read: Read, n: Named) -> List[str]:
    """The rate check's own lines for the name as if set — a filter in both
    filter slots, a modulator as the modulator — at the current rate, mode
    and queue; its modulator half from the published atlas alone."""
    lists = read.lists
    if (read.sources is None or n.kind not in CHECK or not read.state
            or any(lists.get(k) is None for k in ("modes", "rates", "filters", "shapers"))):
        return []
    slots = ({"filterNx": n.row["index"], "filter1x": n.row["index"]} if n.kind == "filter"
             else {"shaper": n.row["index"]})
    inputs = hqp_rate_check.inputs({**read.state, **slots}, lists["modes"], lists["rates"], lists["filters"],
                                   lists["shapers"], adaptive=read.adaptive, atlas=read.atlas,
                                   engine=_engine(read), published=True)
    said = hqp_rate_check.lines(hqp_rate_check.check(inputs, read.sources))
    lines = [plain(p) for cause in ("modulator", "filter") for p in said.get(cause, ())]
    return [CHECK[n.kind].format(name=n.name)] + [INDENT + line for line in lines] if lines else []


# -- filter_facts and atlas_link --------------------------------------------------------


def _filter_facts(args: Mapping[str, str], read: Read) -> List[str]:
    n, miss = _pick(read, args["name"])
    if miss:
        return [miss]
    f = _facts(read, n)
    out = _kind_line(n, read.mode) + _status(f) + _sections(f.card, n) + _check(read, n)
    return out + ([LINK.format(href=f.card["link"]["href"])] if f.card["link"] else [])


def _atlas_link(args: Mapping[str, str], read: Read) -> List[str]:
    n, miss = _pick(read, args["name"])
    if miss:
        return [miss]
    f = _facts(read, n)
    return [LINK.format(href=f.card["link"]["href"]) if f.card["link"] else NO_PAGE]


# -- compare ----------------------------------------------------------------------------


def _compare(args: Mapping[str, str], read: Read) -> List[str]:
    a, miss = _pick(read, args["name_a"])
    b, miss_b = _pick(read, args["name_b"]) if not miss else (None, None)
    if miss or miss_b:
        return [miss or miss_b]
    if a.name == b.name:
        return [SAME_NAME.format(name=a.name)]
    if a.kind != b.kind or a.kind not in hqp_atlas.KINDS:
        return [TWO_KINDS.format(a=a.name, b=b.name, kind_a=a.kind, kind_b=b.kind)]
    fa, fb = _facts(read, a), _facts(read, b)
    out = ([TWO.format(a=a.name, b=b.name, kind=a.kind, mode=MODE_NAME[read.mode])] if read.mode else [])
    out += _both_status(a, fa, b, fb)
    out += _says_both(a, fa, b, fb)
    if fa.found is not None and fb.found is not None:
        out += _relation(read, a, fa, b)
        out += _side_by_side(read, a, fa, b, fb)
    out += _cpu(read, a, b)
    out += _check(read, a) + _check(read, b)
    out += [PAGE_OF.format(name=n.name, href=f.card["link"]["href"]) for n, f in ((a, fa), (b, fb))
            if f.card["link"]]
    return out


def _status(f: Facts) -> List[str]:
    return f.status + ([f.own] if f.own else [])


def _both_status(a: Named, fa: Facts, b: Named, fb: Facts) -> List[str]:
    """The lines both read alike once — the engine's, the mode's — then
    each side's own by name: what one alone is never stands for two."""
    shared = [line for line in fa.status if line in fb.status]
    return shared + [SIDE.format(name=n.name, said=line) for n, f in ((a, fa), (b, fb))
                     for line in _status(f) if line not in shared]


def _says_both(a: Named, fa: Facts, b: Named, fb: Facts) -> List[str]:
    """What HQPlayer says of each, side by side, in the card's words — the
    rating's two n/5 before the one definition they share."""
    terms_a, terms_b = _terms(fa.card), _terms(fb.card)
    labels = list(dict.fromkeys(list(terms_a) + list(terms_b)))
    if not labels:
        return []
    heads = {s["head"] for f in (fa, fb) for s in f.card["sections"]}
    out = [f"{hqp_atlas.SAYS_STRUCTURE if hqp_atlas.SAYS_STRUCTURE in heads else hqp_atlas.SAYS}:"]
    for label in labels:
        if label == hqp_atlas.RATING:
            rated = [f"{n.row['parsed']['rating']}/5" if label in terms else None
                     for n, terms in ((a, terms_a), (b, terms_b))]
            said = hqp_atlas.RATING_DEF.replace("{n}/5", "{v}").format(v=_pair(*rated))
        else:
            said = _pair(terms_a.get(label), terms_b.get(label))
        out.append(f"{INDENT}{label}: {said}")
    return out


def _terms(card: Dict[str, Any]) -> Dict[str, str]:
    says = next((s for s in card["sections"] if s["head"] in (hqp_atlas.SAYS, hqp_atlas.SAYS_STRUCTURE)), None)
    return {it["term"]: plain(it["def"]) for it in (says or {}).get("items", ()) if "term" in it}


def _pair(a: Optional[str], b: Optional[str]) -> str:
    """Two values side by side: "a / b", one where the two read alike as
    text, "not given" for a side that has none — never the card's "—", which
    says the method defines no reading."""
    a, b = a or NOT_GIVEN, b or NOT_GIVEN
    return BOTH.format(v=a) if a == b else SIDES.format(a=a, b=b)


def _relation(read: Read, a: Named, fa: Facts, b: Named) -> List[str]:
    """The group holding both, in the card's words, or their approved pair;
    else that neither does."""
    atlas = read.atlas
    order = {row["name"]: i for i, row in enumerate(a.rows)}
    group = next((g for g in atlas.groups(fa.found) if b.name in g["members"]), None)
    if group is not None:
        section = hqp_atlas.group_section(group, a.name, order)
        return _items(section["items"], "") if section else []
    pair = next((p for p in atlas.pairs(fa.found) if b.name in p["members"]), None)
    if pair is None:
        return [NEITHER]
    used: set = set()
    items = hqp_atlas.pair_items(atlas, fa.found, pair, used)
    return [f"{hqp_atlas.DIFFERS}:"] + _items(items + hqp_atlas.notes(used), INDENT) if items else []


def _side_by_side(read: Read, a: Named, fa: Facts, b: Named, fb: Facts) -> List[str]:
    """Each measured value of the two, side by side, as the card writes it:
    a filter's per context both were measured at, a modulator's idle noise
    per rate and the rates HQPlayer refused it at."""
    atlas = read.atlas
    used: set = set()
    if a.kind == "filter":
        ea, eb = fa.found.entry, fb.found.entry
        keys = [k for k, c in ea["contexts"].items()
                if "void" not in c and k in eb["contexts"] and "void" not in eb["contexts"][k]]
        out = [SIDE_BY_SIDE.format(a=a.name, b=b.name)] if keys else []
        valued = bool(keys)
        for key in keys:
            rows_a = dict(hqp_atlas.context_rows(atlas, fa.found, key, used))
            rows_b = dict(hqp_atlas.context_rows(atlas, fb.found, key, used))
            out.append(f"{INDENT}{hqp_atlas.context_label(key)}:")
            out += [f"{INDENT * 2}{label}: " + _pair(_said(rows_a.get(label)), _said(rows_b.get(label)))
                    for label, _ in hqp_atlas.ROWS if label in rows_a or label in rows_b]
    else:
        idle_a = {rate: value for rate, _, value in hqp_atlas.idle_rows(atlas, fa.found, used)}
        idle_b = {rate: value for rate, _, value in hqp_atlas.idle_rows(atlas, fb.found, used)}
        held = {**fb.found.entry["rates"], **fa.found.entry["rates"]}
        out = [PER_RATE_BOTH.format(a=a.name, b=b.name)]
        valued = bool(held)
        if valued:
            out.append(f"{INDENT}{hqp_atlas.IDLE}:")
            out += [f"{INDENT * 2}{rate_name(int(rate))}: " + _pair(_said(idle_a.get(rate)), _said(idle_b.get(rate)))
                    for rate, _ in hqp_atlas.by_rate(held)]
        out.append(INDENT + REFUSED_BOTH.format(a=_refused(fa.found), b=_refused(fb.found)))
    out += [INDENT + plain(note["note"]) for note in hqp_atlas.notes(used)]
    # A member of an identical group shows the group's figure, as the card
    # says under it: its pair's line above gives its own reading
    order = {row["name"]: i for i, row in enumerate(a.rows)}
    group_notes = dict.fromkeys(plain(note["note"]) for f in (fa, fb)
                                for note in hqp_atlas.figures_note(atlas, f.found, order)) if valued else {}
    return out + [INDENT + line for line in group_notes]


def _said(value: Optional[List[Any]]) -> Optional[str]:
    return plain(wire(value)) if value is not None else None


def _refused(found: Any) -> str:
    rates = [rate_name(int(rate)) for rate, _ in hqp_atlas.by_rate(found.entry["rates_refused"])]
    return hqp_atlas.words(rates) if rates else NONE_REFUSED


# -- This host's speeds -----------------------------------------------------------------


def _cpu(read: Read, a: Named, b: Named) -> List[str]:
    """How each runs on this HQPlayer at the current setting, for the source
    the owner hears — the picker's marks (routers.hqplayer._headroom)."""
    if not read.endpoint or not read.endpoint.get("hqp_engine"):
        return [NOTHING_HERE]
    hr = read.headroom or {}
    if not hr.get("setting"):
        return [CPU_NO_SOURCE]
    setting, src, th = hr["setting"], hr["source"], hr["thresholds"]
    marks = hr["filters"] if a.kind == "filter" else hr["shapers"]
    other = setting["shaper"] if a.kind == "filter" else setting["filter"]
    out = [CPU_AT.format(rate=_rate_word(setting["rate_out"]), other=other,
                         source=_source(src["rate"], src["channels"]))]
    for n in (a, b):
        mark = marks.get(n.name)
        out.append(INDENT + (NOT_MEASURED_HERE.format(name=n.name) if mark is None else CPU_RUNS.format(
            name=n.name, speed=f"{mark['speed']:.2f}", samples=_samples(mark["n"]),
            unsettled="" if mark["settled"] else UNSETTLED, cls=_class(mark["class"], th))))
    return out


def _source(rate: int, channels: int) -> str:
    """A source as the six name it: its rate, and its channels when not two."""
    return f"{rate_name(rate)} source" + (CHANNELS[channels != 1].format(n=channels) if channels != 2 else "")


def _rate_word(hz: int) -> str:
    return rate_name(hz) if hz else AUTO


def _samples(n: int) -> str:
    return SAMPLES[n != 1].format(n=n)


def _class(cls: str, th: Mapping[str, Any]) -> str:
    return CLASS[cls].format(ok=f"{th['ok']:g}", no=f"{th['no']:g}")


def _host_fit(args: Mapping[str, str], read: Read) -> List[str]:
    """This host's records of one name in the mode asked (the one HQPlayer
    is in, by default): every key measured there, by source, then rate and
    the other knob — never by speed."""
    host = read.host or {}
    if "words" in host:
        return [host["words"]]
    ctx, th = host["context"], host["thresholds"]
    if not host["rows"]:
        return [no_record(host["name"], ctx["mode"], host["rate"])]
    out = [HOST.format(engine=ctx["engine"], cuda=CUDA.get(ctx["cuda"], ctx["cuda"]), mode=ctx["mode"],
                       matrix=MATRIX.format(profile=ctx["matrix_profile"]) if ctx["matrix_profile"] else "",
                       convolution=CONVOLUTION if ctx["convolution"] else "")]
    source = None
    for r in host["rows"]:
        if (r["src_rate"], r["src_channels"]) != source:
            source = (r["src_rate"], r["src_channels"])
            out.append(INDENT + FROM_SOURCE.format(source=_source(*source)))
        out.append(INDENT * 2 + _host_row(r, host["knob"], th))
    if host["total"] > len(host["rows"]):
        out.append(INDENT + MORE.format(n=host["total"] - len(host["rows"])))
    return out + [OWN]


def _host_row(r: Mapping[str, Any], knob: str, th: Mapping[str, Any]) -> str:
    rate = _rate_word(r["rate_out"])
    other = r["shaper"] if knob == "filter" else r["filter"]
    if r["class"] == "refused":
        return REFUSED_HERE.format(rate=rate, other=other)
    if r["class"] == "unstarted":
        return UNSTARTED_HERE.format(rate=rate, other=other)
    return RUNS.format(rate=rate, other=other, speed=f"{r['speed']:.2f}", samples=_samples(r["n"]),
                       unsettled="" if r["bench_settled"] is not False else UNSETTLED,
                       dropout=DROPOUT if r["dropout"] else "", cls=_class(r["class"], th))


def host_ask(args: Mapping[str, str], read: Read) -> Tuple[Optional[str], Optional[str]]:
    """The mode host_fit asks this host's records in, by HQPlayer's own name
    for it — or the words why it cannot ask."""
    gone = _missing(read, ("modes",))
    if gone:
        return None, gone
    return host_mode(args["mode"], read.state, read.lists["modes"])


def host_mode(arg: str, state: Optional[dict], modes: Optional[List[dict]]) -> Tuple[Optional[str], Optional[str]]:
    """host_fit's mode: HQPlayer's own name for it in GetModes — the one it
    is in for "" ([source] reads as the mode it runs) — or the words why
    there is none."""
    if arg and arg.lower() not in ("pcm", "sdm"):
        return None, BAD_MODE
    if arg:
        name = next((m["name"] for m in modes or () if hqp_rate_check.mode_kind(m["name"]) == arg.lower()), None)
        return (name, None) if name else (None, NO_SUCH_MODE.format(mode=arg.upper()))
    chosen = next((m for m in modes or () if m["index"] == (state or {}).get("mode")), None)
    if chosen is not None and hqp_rate_check.mode_kind(chosen["name"]) is None:
        chosen = next((m for m in modes or () if m["value"] == (state or {}).get("active_mode")), None)
    return (chosen["name"], None) if chosen else (None, NO_MODE)


def host_rate(arg: str, rates: List[int], mode: str) -> Tuple[Optional[int], Optional[str]]:
    """host_fit's rate: one of the output rates this host holds records at in
    `mode` (hqp_load.names), given as the HQPlayer screen names it or in Hz;
    None for "" (every rate); else the words why not. HQPlayer's own rate
    list is the mode it is in, which need not be the one asked."""
    if not arg:
        return None, None
    given = arg.strip().lower()
    match = next((r for r in rates if str(r) == given or _rate_word(r).lower() == given), None)
    if match is None:
        return None, NO_RATE.format(rate=arg, mode=mode, rates=", ".join(_rate_word(r) for r in rates))
    return match, None


def no_record(name: str, mode: str, rate: Optional[int] = None) -> str:
    """host_fit's words for a name this host holds no record of in `mode` — at
    `rate`, when one was asked. HQPlayer's lists are not read for it, so they
    neither say the name is HQPlayer's nor that it is not."""
    return NO_RECORD.format(name=name, mode=mode, at="" if rate is None else AT_RATE.format(rate=_rate_word(rate)))


def host_name(arg: str, names: Mapping[str, List[str]]) -> Optional[Tuple[str, str]]:
    """The knob and the name this host's records hold for `arg`: exactly as
    written, else the one name equal to it in any case."""
    held = [(knob, name) for knob, plural in (("filter", "filters"), ("shaper", "shapers"))
            for name in names[plural]]
    exact = [h for h in held if h[1] == arg] or [h for h in held if h[1].lower() == arg.lower()]
    return exact[0] if len({h[1] for h in exact}) == 1 else None


# -- differs_only_in and ab_pair ----------------------------------------------------------


def _axis(arg: str, *, required: bool) -> Tuple[Optional[str], Optional[str]]:
    axis = arg.strip().lower()
    if not axis and not required:
        return None, None
    if axis not in hqp_atlas.AXIS:
        return None, AXES.format(axes=", ".join(hqp_atlas.AXIS))
    return axis, None


def _listed(read: Read, p: Dict[str, Any]) -> bool:
    """Whether HQPlayer lists both of a pair's members now, each as its
    pair's kind."""
    names = {row["name"] for row in read.lists[KIND_LIST[p["kind"]]] or ()}
    return (p["kind"] == "filter" or read.mode == "sdm") and all(m in names for m in p["members"])


KIND_LIST = {"filter": "filters", "modulator": "shapers"}


def _differs_only_in(args: Mapping[str, str], read: Read) -> List[str]:
    axis, bad = _axis(args.get("axis", ""), required=False)
    if bad:
        return [bad]
    n, miss = _pick(read, args["name"])
    if miss:
        return [miss]
    if axis and n.kind in AXIS_KIND.values() and AXIS_KIND[axis] != n.kind:
        other = AXIS_KIND[axis]
        return [AXIS_OF.format(name=n.name, kind=n.kind, axis=axis, other=other)]
    f = _facts(read, n)
    out = _kind_line(n, read.mode) + _status(f)
    if f.found is None:
        return out
    atlas = read.atlas
    pairs = [p for p in atlas.pairs(f.found) if not axis or p["axis"] == axis]
    shown = [p for p in pairs if _listed(read, p)]
    left = [m for p in pairs if p not in shown for m in p["members"] if m != n.name]
    used: set = set()
    items = [it for p in shown for it in hqp_atlas.pair_items(atlas, f.found, p, used)]
    if items:
        out += [f"{hqp_atlas.DIFFERS}:"] + _items(items + hqp_atlas.notes(used), INDENT)
    elif not left:
        out.append(NO_PAIR_AXIS.format(axis=axis) if axis else NO_PAIR)
    return out + ([LEFT_OUT.format(names=", ".join(left))] if left else [])


def _ab_pair(args: Mapping[str, str], read: Read) -> List[str]:
    axis, bad = _axis(args.get("axis", ""), required=True)
    if bad:
        return [bad]
    gone = _missing(read, ("filters", "shapers"))
    if gone:
        return [gone]
    atlas, engine, kind = read.atlas, _engine(read), AXIS_KIND[axis]
    if atlas is None or not atlas.speaks_of(engine, kind, published=True):
        return [AB_NONE]
    shown = atlas.engine_for(engine, kind, published=True)
    if shown is None:
        return [_no_engine(atlas, engine, kind)]
    pairs = [p for p in atlas.axis_pairs(axis, shown)
             if all(_published(atlas, p["kind"], m, shown) for m in p["members"])]
    if not pairs:
        return [AB_NONE]
    shown_pairs = [p for p in pairs if _listed(read, p)]
    if not shown_pairs:
        left = sorted({m for p in pairs for m in p["members"]}, key=[m for p in pairs for m in p["members"]].index)
        return [LEFT_OUT.format(names=", ".join(left))]
    near = args.get("near", "").strip()
    first = _near(read, near) if near else _current(read)
    holding = [p for p in shown_pairs if first & set(p["members"])]
    out = [AB_HEAD.format(axis=hqp_atlas.AXIS[axis])]
    if near and not holding:
        out.append(AB_NEAR.format(near=near))
    used: set = set()
    for p in holding + [p for p in shown_pairs if p not in holding]:
        out += _ab_one(read, atlas, p, used)
    out += [INDENT + plain(note["note"]) for note in hqp_atlas.notes(used)]
    if read.mode == "sdm" and any(p["kind"] == "filter" for p in shown_pairs):
        out.append(hqp_atlas.SDM_PATH)
    left = [m for p in pairs if p not in shown_pairs for m in p["members"]]
    if left:
        out.append(LEFT_OUT.format(names=", ".join(dict.fromkeys(left))))
    return out + list(AB_STEPS)


def _published(atlas: Any, kind: str, name: str, engine: str) -> bool:
    return atlas.lookup(kind, name, engine, "pcm", published=True).state in ("measured", "refused")


def _near(read: Read, near: str) -> set:
    n, _ = _pick(read, near)
    return {n.name if n else near}


def _current(read: Read) -> set:
    """The setting's own names: the filter in either slot, and in SDM the
    modulator."""
    state, lists = read.state or {}, read.lists
    names = {row["name"] for row in lists["filters"] if row["index"] in (state.get("filterNx"), state.get("filter1x"))}
    if read.mode == "sdm":
        names |= {row["name"] for row in lists["shapers"] if row["index"] == state.get("shaper")}
    return names


def _ab_one(read: Read, atlas: Any, p: Dict[str, Any], used: set) -> List[str]:
    a, b = p["members"]
    out = [SIDES.format(a=a, b=b)]
    out += [INDENT + plain(wire(detail)) for detail in hqp_atlas.pair_detail(p, 0, used)]
    where = [hqp_atlas.context_label(k) if p["kind"] == "filter" else rate_name(int(k)) for k in p["contexts"]]
    out.append(INDENT + AB_WHERE.format(where=", ".join(where)))
    if p["kind"] == "filter":
        ratio = {row["name"]: _ratio_of(row) for row in read.lists["filters"]}
        if ratio.get(a) and ratio.get(b):
            out.append(INDENT + AB_RATIO.format(a=a, ra=ratio[a], b=b, rb=ratio[b]))
    tail = hqp_atlas.pair_tail(atlas, p)
    return out + ([INDENT + plain(wire(tail))] if tail else [])


def _ratio_of(row: Mapping[str, Any]) -> Optional[str]:
    parsed = row.get("parsed")
    return parsed["ratio"] + (hqp_atlas.UP if parsed["up"] else "") if parsed else None


_SAY: Dict[str, Callable[[Mapping[str, str], Read], List[str]]] = {
    "filter_facts": _filter_facts, "compare": _compare, "differs_only_in": _differs_only_in,
    "ab_pair": _ab_pair, "host_fit": _host_fit, "atlas_link": _atlas_link}
