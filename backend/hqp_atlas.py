"""The HQPlayer atlas as this node reads it, and every word the HQPlayer
screen's pickers say about a filter or a modulator.

The atlas is Sautium's measurement of HQPlayer's filters and modulators; its
app subset is one JSON file. A node reads the file shipped beside this module
or, only while none is shipped, its own copy (`settings.hqp_atlas_overlay`),
which stays on the node: no backup, share export, sync or support bundle
carries it, and no route returns more of it than the rows a screen shows.

The file is read whole and checked against the shape read here; whatever
breaks it refuses the file whole, and the node then shows HQPlayer's own
facts alone, as without a file. The refusal is logged in one line naming the
reason and a place in the file, never a value of it: the logs travel in
support bundles, and no atlas content may. A key not read here is ignored.
Each read stats the file and loads it again when its modification time or
size changed.

A number stays the text the file writes (`Num`): nothing here rounds, formats
or computes a measured value, and nothing ranks by one — every list keeps
HQPlayer's order or the file's. A name is looked up in the file's registry,
never turned into a slug, so a name the registry lacks is not measured. A
relation, an axis or a difference without words here shows nothing and is
logged by its name, once. A source not re-read in full (status other than O)
is never quoted or cited, and no link is given while an entry's release has
no date.

What a screen gets is wire-ready: a phrase is a list of parts, {"t": text} or
{"n": a number's token}, so a number reaches the screen as the file wrote it;
`plain` prints the same phrase as text.

`rate_name` names a rate HQPlayer reports as the HQPlayer screen does, for
every text the node words about one; it reads no atlas.
"""

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA = "hqp-atlas-app/v1"
SHIPPED = Path(__file__).with_name("hqp_atlas.json")
KINDS = {"filter": "filters", "modulator": "modulators"}
# The release family each kind's names are measured in
RELEASE_FAMILY = {"filter": "pcm", "modulator": "sdm"}
PAGES = "https://sautium.net/hqplayer"
MINUS = "−"


class Num(str):
    """A number as the file writes it: "0.100" stays "0.100", "6.0" stays
    "6.0", "-0.5" stays "-0.5". The screen shows the token."""


# -- The words -------------------------------------------------------------------
# Every string the pickers and the card say about a filter or a modulator, one
# place: {x} is a value from HQPlayer or from the file, the rest is literal.
# Where the atlas's pages on sautium.net say the same fact, these are their
# words.

# HQPlayer's own facts, from its live lists
RATIO_TITLE = "Ratio class, from HQPlayer's description"
FOCUS_TITLE = "The author's focus, from HQPlayer's description"
TAG = "Signalyst {n}/5"
TAG_TITLE = "Signalyst's technical rating {n}/5 — relative to the other filters; not a listening score"
GENERATION_TITLE = "HQPlayer's generation field"
UP = " up"

# The sheets' head
MEASURED_ON = "Measured on HQPlayer engine {engine}"
RUNS_ENGINE = " · this HQPlayer runs engine {engine}"
NO_ENGINE = " · this HQPlayer does not report its engine"
PROPERTIES_AT = "Properties at {context}"
SDM_HEAD = "In SDM mode: the properties each filter showed in PCM mode."

# A row
PHASE_CHIP = "{phase} phase"
PHASE_TITLE = "Measured by Sautium, {context}"
RING_EVEN = "±{pre} ms"
RING_APART = "{pre} ms before · {post} ms after"
RING_TITLE = "Impulse before and after its peak at −100 dB, {context}"
POSITION_CHIP = "position-dependent"
POSITION_TITLE = "The readings move with the impulse's position, {context}"
APODIZING_CLAUSE = "apodizing by HQPlayer's label"
APOD_CLAUSE = "Apod {mark} per the manual"
CLAUSES = " · "
NOT_MEASURED = "not measured on engine {engine}"
NOT_MEASURED_TITLE = "Listed by HQPlayer, not measured on engine {engine}"
TWIN = {
    "bit_identical": "Same output as {other} (bit for bit)",
    "within_1_lsb": "Same output as {other} (within one step)",
    "same_figures_within_precision": "Same figures as {other} (within the measurement's precision)",
}
ORDER = {"5": "fifth", "7": "seventh"}
ORDER_WORD = "{order} order"
ORDER_NUMBER = "order {order}"
ORDER_TITLE = "Order, read off the name"
REFUSED_SOME = "refused at {rates}"
REFUSED_SOME_TITLE = "HQPlayer refused it at {rates} on engine {engine}"
REFUSED_ALL = "refused at every measured rate"
REFUSED_ALL_TITLE = "HQPlayer refused it at every measured rate: {rates}."
BRACKET = "{names} — {say}."
BRACKET_PLAIN = "{names}."
CHOOSE_BY = "Choose by {choose_by}."
STEM_NAMES = "{stem}: {ends}"
AND = " and "

# Labels
CONTEXT_LABEL = "{slot} · {source} → {output}"
RATE_LABEL = "DSD{n} · from {family}"
RATE_SHORT = "DSD{n}"
RATE_SHORT_48 = "DSD{n} (48k)"
FAMILY = {"44100": "44.1k", "48000": "48k"}

# The card
NOT_MEASURED_CARD = "Not measured on HQPlayer engine {engine}."
SDM_PATH = ("In SDM mode HQPlayer runs a filter to an intermediate rate before its modulator. That path was not "
            "measured on its own: the properties are what each filter did in PCM mode.")
SDM_LISTED = "In SDM mode HQPlayer lists it as {ratio}; the facts here are measured in PCM mode."
SDM_LISTED_PLAIN = "In SDM mode HQPlayer lists it; the facts here are measured in PCM mode."
SAYS = "What HQPlayer says"
SAYS_STRUCTURE = "What HQPlayer says; the structure"
RATING = "Signalyst's technical rating"
RATING_DEF = "{n}/5 — relative to the other filters; not a listening score"
FOCUS = "The author's focus"
FOCUS_NONE = "none named"
RATIO = "Ratio class"
DESCRIBED = "From HQPlayer's description “{description}”."
STRUCTURE = "Structure, read off the name"
ADAPTIVE = ("fixed", "adaptive")
EC = ("without EC", "with EC")
TUNED = "tuned {tuning}"
MULTIBIT = "multibit ({multibit})"
APODIZING = "Apodizing"
LABEL_TERM = "Apodizing, HQPlayer's label"
LABEL_DEF = "{label} — its log names the design “{name}”"
LABEL = ("not apodizing", "apodizing")
PER_MANUAL = "per the manual"
PER_SECTION = "per the manual ({section})"
APOD_TERM = "Apod, {per}"
APOD_HALF = "½, as the table prints it"
APOD_NONE = "the manual's table has no row for it"
MANUAL = "The manual"
GENRE_TERM = "The author's genre suggestion, {per}"
GENRE_DEF = "{genres} — Sautium shows it and never applies it"
GENRE = {"any-by-length": "any, depending on length"}
VARIANT_TERM = "A variant of, {per}"
RELATION = {
    "bit_identical": "Same output as",
    "within_1_lsb": "Same output as",
    "same_figures_within_precision": "Same figures as",
    "same_magnitude": "Same magnitude as",
}
GROUP_LEAD = "{relation} {others} — {say}."
GROUP_LEAD_PLAIN = "{relation} {others}."
ACROSS = "Across rate families: {families}."
ACROSS_ONE = "{name}: {what}"
PROPERTIES = "Properties"
OTHER_CONTEXTS = "The other contexts"
ROWS = (
    ("Phase", "phase"),
    ("−3 dB, kHz", "f_3db_khz"),
    ("100 dB down from here to the output Nyquist, kHz", "transition_end_khz"),
    ("Attenuation at the source Nyquist, dB", "att_nyquist_db"),
    ("Stopband minimum, dB", "stopband_min_db"),
    ("Ringing before the main lobe, above −100 dB, ms", "pre_ring_ms_100"),
    ("Ringing after the main lobe, above −100 dB, ms", "post_ring_ms_100"),
    ("Length above −100 dB, ms", "length_ms_100"),
    ("Filter delay, ms", "filter_delay_ms"),
    ("Time-invariant", "time_invariant"),
)
YES_NO = ("no", "yes")
PLUS_MINUS = "{v} ± {pm}"
AT_LEAST = "≥ {v}"
AT_MOST = "≤ {v}"
RANGE = "{min}–{max} (median {median})"
NONE = "—"
BY = {"impulse": "ⁱ", "sweep": "ˢ", "contradict": "ˣ"}
NOTES = {
    "bound": "≥ at least, ≤ at most: the reading lies within reach of the measurement's noise, so it is a bound.",
    "ⁱ": "ⁱ the bound is set by the impulse.",
    "ˢ": "ˢ the bound is set by the sweep.",
    "ˣ": "ˣ the impulse and the sweep disagreed; the larger bound stands.",
    "range": "a range: the reading moves with the impulse's position.",
    "none": "— none: the method defines no reading here.",
}
FIGURES_NOTE = ("For a member of {names}, each number is the group's figure where it holds one — the members' "
                "median, a bound where any member's reading is one; the filter delay is the member's own.")
IDLE_NOTE = ("For a member of {names}, the idle noise is the group's figure where it holds one — the members' "
             "median, a bound where any member's reading is one.")
PER_RATE = "Per rate"
RATE = "Rate"
IDLE = "Idle noise, 20 Hz–20 kHz, dB"
REFUSED = "Rates HQPlayer refused"
REFUSED_LEAD = "HQPlayer refused it at {rates}."
REFUSED_LINE = "{rate}: “{line}”"
FULLSCALE = "Near full scale"
FULLSCALE_LEAD = "The highest level stable on both channels at the modulator's input."
VOLUME = "HQPlayer volume {volume} dB"
CELL = "{v} dBFS · {what}"
TONE_PAIR = (("tone_dbfs", "tone"), ("pair_dbfs", "pair"))
NONE_TESTED = "none of the tested levels"
NOT_TESTED = "not tested"
FULLSCALE_NOTE = "Tone: one sine; pair: two tones together. Not tested: no play at that volume."
DIFFERS = "Differs only in"
AXIS = {"phase": "Phase", "apodizing": "Apodizing", "length": "Length", "order": "Order", "ec": "EC",
        "adaptive": "Adaptive or fixed", "generation": "Generation", "tuning": "Tuning", "multibit": "Multibit"}
PAIR_LEAD = "{axis} — from {other}."
PAIR_AT = "At {context}, {name} / {other}: {diffs}."
PAIR_ALSO = " Also differs: {diffs}."
DIFF = {
    "apodizing": ("HQPlayer's label", ""),
    "phase": ("phase", ""),
    "f_3db_khz": ("−3 dB", " kHz"),
    "length_ms_100": ("impulse length above −100 dB", " ms"),
    "transition_hz": ("transition", " Hz"),
    "group_delay_20k_us": ("group delay at 20 kHz", " µs"),
    "filter_delay_ms": ("filter delay", " ms"),
    "ratio_class": ("ratio class", ""),
    "generation": ("HQPlayer's generation field", ""),
}
DIFF_ITEM = "{label}: {a} / {b}{unit}"
UNMEASURED = "Not measured: the {axes}."
UNMEASURED_JOIN = ", the "
FROM = "From {cites}."
PURPOSE = "The author's stated purpose"
IN_OUR_WORDS = "In Sautium's words, from {cites}."
HISTORY = "History"
INTRODUCED = "Introduced in {release} — {cites}."
RENAMED = "Renamed from {name} — {cites}."
RENAMED_IN = "Renamed from {name} in {release} — {cites}."
REMOVED = "Removed in {release} — {cites}."
HISTORY_NOTE = "Only what the sources state; a missing event means not stated in the sources read."
SOURCES = "Sources"
MEASURED_BY = "Measured by Sautium on HQPlayer engine {engine}."
CONTEXT_FILTER = "Measured with {filter} in both filter slots."
SOURCE = "{who}, {where}, {date}."
QUOTE = " “{quote}”"
CITE_MANUAL = "the manual ({section})"
CITE_DOC = "Signalyst, {where}, {date}"
CITE_JUSSI = "Jussi Laako, {date}"
CITE_OWNER = "Sautium's"
CITE_OTHER = "{who}, {date}"
CITES = "; "
LINK = "Measured properties ↗"


# -- Reading ---------------------------------------------------------------------

_ORIGIN = {"shipped": "the shipped atlas file", "overlay": "the node's own atlas copy"}
_cache: Dict[Path, Tuple[Tuple[int, int], Optional["Atlas"]]] = {}
_cache_lock = threading.Lock()


def read(shipped: Path, overlay: Optional[Path]) -> Optional["Atlas"]:
    """The atlas this node holds — the shipped file, else the node's own copy
    — or None without either, or for a file refused. A shipped file wins: the
    overlay is then never opened, the shipped one refused or not."""
    for path, origin in ((shipped, "shipped"), (overlay, "overlay")):
        if path is None:
            continue
        try:
            stat = path.stat()
            key = (stat.st_mtime_ns, stat.st_size)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            # There, but not to be looked at (a folder not readable, a link
            # loop, a failing disk): the read says why, once per error
            key = (-1, exc.errno or 0)
        with _cache_lock:
            held = _cache.get(path)
            if held is None or held[0] != key:
                held = (key, _load(path, origin))
                _cache[path] = held
        return held[1]
    return None


class _Refused(Exception):
    def __init__(self, reason: str, at: str):
        super().__init__(reason)
        self.reason, self.at = reason, at


def _load(path: Path, origin: str) -> Optional["Atlas"]:
    try:
        text = path.read_text(encoding="utf-8")
        atlas = Atlas(json.loads(text, parse_float=Num, parse_int=Num, parse_constant=_constant), origin)
    except _Refused as refused:
        reason, at = refused.reason, refused.at
    except json.JSONDecodeError as exc:
        reason, at = "not JSON", f"line {exc.lineno}, column {exc.colno}"
    except RecursionError:
        reason, at = "nested deeper than JSON is read here", "the file"
    except UnicodeDecodeError:
        reason, at = "not UTF-8 text", "the file"
    except OSError as exc:
        reason, at = f"not readable ({exc.strerror})", "the file"
    else:
        logger.info("hqp_atlas: read %s", _ORIGIN[origin])
        return atlas
    logger.warning("hqp_atlas: %s refused whole — %s, at %s; the HQPlayer screen shows HQPlayer's own facts alone",
                   _ORIGIN[origin], reason, at)
    return None


def _constant(name: str) -> None:
    raise _Refused("NaN or Infinity, which JSON does not write", "the file")


# -- The shape -------------------------------------------------------------------
# What is read is checked, here and once; a place in a refusal is a path of keys
# that passed their own pattern (an entry by its slug, a context, a rate) and of
# indices (a group, a pair, a source), so no value of the file reaches the log.

# No control character, and no lone surrogate: a JSON escape can make one,
# and no answer of the node could carry it
_LINE = re.compile(r"[^\x00-\x1f\ud800-\udfff]*")
_RELEASE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+/(?:pcm|sdm)")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# Digits are bounded where `int()` reads them — an engine's parts here, a
# rate (_RATE) below: Python refuses to read past 4300 digits, and that would
# raise instead of refusing the file
_ENGINE = re.compile(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}")
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_CONTEXT = re.compile(r"(?:1x|nx)-[0-9.]+k-[0-9.]+k")
_RATE = re.compile(r"[0-9]{1,10}")
# An integer's shape — an order, drawn as it stands, and fs_in — bounding no
# digits int() reads: fs_in reaches int() only as one of FAMILY's keys
_INT = re.compile(r"-?[0-9]{1,10}")
_VOLUME = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
_GROUP_ID = re.compile(r"[a-z0-9-]+(?:\+[a-z0-9-]+)+")
_PAIR_ID = re.compile(r"[a-z_]+:[a-z0-9-]+\|[a-z0-9-]+")
_NAME = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?:/[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)?(?: [0-9]+\+fs)?")
_LOG_NAME = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?:/[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)?"
                       r"(?: [A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?:/[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)?)*"
                       r"(?: \([A-Za-z0-9]+(?: [A-Za-z0-9]+)*\))?")
_GENERATION = re.compile(r"Gen[0-9]+")
_RATIO = re.compile(r"(?:1:1|2\^x|Int|Any)(?: up)?")
_PRODUCT = re.compile(r"HQPlayer Desktop [0-9]+\.[0-9]+\.[0-9]+")
_REFUSAL = re.compile(r"Requested filter not possible with this rate combination [0-9]+/[0-9]+, stop")
_RELATIONS = ("bit_identical", "within_1_lsb", "same_figures_within_precision", "not_compared", "same_magnitude")
_IDENTICAL = ("bit_identical", "within_1_lsb", "same_figures_within_precision")
_AXES = ("phase", "apodizing", "length", "depth", "time_invariance", "order", "ec", "tuning", "multibit",
         "adaptive", "generation")
_GENRES = ("any", "classical", "jazz", "blues", "pop", "rock", "electronic", "any-by-length")
# A group figure's quantities: a closed list, so a misspelled one refuses the
# file instead of leaving a member's own number where the figure belongs.
_FIGURES = ("f_3db_khz", "transition_end_khz", "att_nyquist_db", "stopband_min_db", "pre_ring_ms_100",
            "post_ring_ms_100", "length_ms_100", "idle_db")
_ALSO = ("process_speed_min", "filter_delay_ms", "ratio_class", "generation")


def _need(ok: Any, reason: str, at: str) -> None:
    if not ok:
        raise _Refused(reason, at)


def _obj(x: Any, at: str) -> Dict[str, Any]:
    _need(type(x) is dict, "not an object", at)
    return x


def _arr(x: Any, at: str, least: int = 0) -> List[Any]:
    _need(type(x) is list, "not a list", at)
    _need(len(x) >= least, f"fewer than {least} items", at)
    return x


def _text(x: Any, at: str, shape: re.Pattern = _LINE) -> str:
    _need(type(x) is str and shape.fullmatch(x), "not text of its shape", at)
    return x


def _one_of(x: Any, values: Iterable[str], at: str) -> str:
    _need(type(x) is str and x in values, "not one of its values", at)
    return x


def _bool(x: Any, at: str) -> bool:
    _need(type(x) is bool, "not true or false", at)
    return x


def _num(x: Any, at: str, null: bool = False) -> None:
    _need(isinstance(x, Num) or (null and x is None), "not a number", at)


def _int(x: Any, at: str) -> None:
    _need(isinstance(x, Num) and _INT.fullmatch(x), "not an integer", at)


def _keys(obj: Dict[str, Any], shape: re.Pattern, what: str, at: str) -> None:
    for key in obj:
        _need(shape.fullmatch(key), f"a key that is not {what}", at)


def _names(x: Any, at: str, least: int, most: Optional[int] = None) -> List[str]:
    names = _arr(x, at, least)
    _need(most is None or len(names) <= most, f"more than {most} items", at)
    for name in names:
        _text(name, at, _NAME)
    _need(len(set(names)) == len(names), "a name held twice", at)
    return names


def _cites(x: Any, at: str, sources: Dict[str, Any], least: int) -> None:
    ids = _arr(x, at, least)
    for sid in ids:
        _need(type(sid) is str and sid in sources, "a source the file does not hold", at)
    _need(len(set(ids)) == len(ids), "a source cited twice", at)


def _bound(x: Any, b: Iterable[str], at: str) -> None:
    # `by` is read wherever a bound is drawn (_value), so it is checked
    # wherever one stands
    _obj(x, at)
    _num(x.get("v"), f"{at}.v")
    _one_of(x.get("b"), b, f"{at}.b")
    if "by" in x:
        _one_of(x["by"], BY, f"{at}.by")


def _check(doc: Any) -> None:
    _obj(doc, "the file")
    _need(doc.get("schema") == SCHEMA, f"a schema other than {SCHEMA}", "schema")
    releases = _obj(doc.get("releases"), "releases")
    _keys(releases, _RELEASE, "a release", "releases")
    for key, date in releases.items():
        _need(date is None or (type(date) is str and _DATE.fullmatch(date)), "neither null nor a date",
              f"releases.{key}")

    registry: Dict[Tuple[str, str], str] = {}
    taken = set()
    for i, row in enumerate(_arr(doc.get("slugs"), "slugs")):
        at = f"slugs[{i}]"
        _obj(row, at)
        kind = _one_of(row.get("kind"), ("filter", "modulator", "dither"), f"{at}.kind")
        slug = _text(row.get("slug"), f"{at}.slug", _SLUG)
        name = _text(row.get("name"), f"{at}.name", _NAME)
        _text(row.get("first_engine"), f"{at}.first_engine", _ENGINE)
        _need((kind, name) not in registry and (kind, slug) not in taken, "a name or slug the registry holds twice",
              at)
        registry[(kind, name)] = slug
        taken.add((kind, slug))

    sources: Dict[str, Any] = {}
    for i, s in enumerate(_arr(doc.get("sources"), "sources")):
        at = f"sources[{i}]"
        _obj(s, at)
        sid = _text(s.get("id"), f"{at}.id", _SLUG)
        _one_of(s.get("src"), ("signalyst.doc", "jussi", "owner", "community"), f"{at}.src")
        _text(s.get("who"), f"{at}.who")
        _text(s.get("where"), f"{at}.where")
        _text(s.get("date"), f"{at}.date", _DATE)
        _one_of(s.get("status"), ("O", "S"), f"{at}.status")
        if "quote" in s:
            _text(s["quote"], f"{at}.quote")
        _need(sid not in sources, "a source id held twice", at)
        sources[sid] = s

    groups: Dict[str, Any] = {}
    for i, g in enumerate(_arr(doc.get("groups"), "groups")):
        at = f"groups[{i}]"
        _obj(g, at)
        gid = _text(g.get("id"), f"{at}.id", _GROUP_ID)
        _one_of(g.get("kind"), KINDS, f"{at}.kind")
        _one_of(g.get("relation"), _RELATIONS, f"{at}.relation")
        members = _names(g.get("members"), f"{at}.members", 2)
        _need(g.get("status") == "approved", "not approved", f"{at}.status")
        for key in ("say", "note"):
            if key in g:
                _text(g[key], f"{at}.{key}")
        if g.get("choose_by") is not None:
            _text(g["choose_by"], f"{at}.choose_by")
        if "sources" in g:
            _cites(g["sources"], f"{at}.sources", sources, 0)
        if "cross_family" in g:
            across = _obj(g["cross_family"], f"{at}.cross_family")
            _need(sorted(across) == sorted(members), "not one word per member", f"{at}.cross_family")
            for what in across.values():
                _one_of(what, ("plays", "refused", "void", "substituted"), f"{at}.cross_family")
        _need(gid not in groups, "a group id held twice", at)
        groups[gid] = g

    pairs: Dict[str, Any] = {}
    for i, p in enumerate(_arr(doc.get("pairs"), "pairs")):
        at = f"pairs[{i}]"
        _obj(p, at)
        pid = _text(p.get("id"), f"{at}.id", _PAIR_ID)
        kind = _one_of(p.get("kind"), KINDS, f"{at}.kind")
        _one_of(p.get("axis"), _AXES, f"{at}.axis")
        _names(p.get("members"), f"{at}.members", 2, 2)
        _need(p.get("status") == "approved", "not approved", f"{at}.status")
        contexts = _obj(p.get("contexts"), f"{at}.contexts")
        for key, c in contexts.items():
            _need(_CONTEXT.fullmatch(key) or _RATE.fullmatch(key), "a key that is neither a context nor a rate",
                  f"{at}.contexts")
            _check_pair_context(c, f"{at}.contexts.{key}")
        if "sources" in p:
            _cites(p["sources"], f"{at}.sources", sources, 0)
        if "unmeasured_axes" in p:
            for axis in _arr(p["unmeasured_axes"], f"{at}.unmeasured_axes"):
                _one_of(axis, ("window",), f"{at}.unmeasured_axes")
        if kind == "filter":
            dc = _text(p.get("default_context"), f"{at}.default_context", _CONTEXT)
            _need(dc in contexts, "a default context the pair does not hold", f"{at}.default_context")
        else:
            _need("default_context" not in p, "a default context on a modulator pair", at)
        _need(pid not in pairs, "a pair id held twice", at)
        pairs[pid] = p

    engines = _obj(doc.get("engines"), "engines")
    _need(engines, "no engine", "engines")
    _keys(engines, _ENGINE, "an engine", "engines")
    for engine, body in engines.items():
        _check_engine(body, engine, f"engines.{engine}", releases, registry, groups, pairs, sources)


def _check_pair_context(c: Any, at: str) -> None:
    # A difference is free in the file's shape: one without words here shows
    # nothing (`_pair_detail`). A text in it is drawn as it stands, so it is
    # held to a line's shape; its key is free, so the place stops before it.
    # What else differs is closed.
    _obj(c, at)
    difference = _obj(c.get("difference"), f"{at}.difference")
    for values in difference.values():
        for x in values if type(values) is list else ():
            if type(x) is str:
                _text(x, f"{at}.difference")
    also = _obj(c.get("also_differs"), f"{at}.also_differs")
    for key, value in also.items():
        _need(key in _ALSO, "a quantity what else differs does not hold", f"{at}.also_differs")
        if key == "process_speed_min":
            continue
        values = _arr(value, f"{at}.also_differs.{key}", 2)
        _need(len(values) == 2, "not two values", f"{at}.also_differs.{key}")
        for x in values:
            if key == "filter_delay_ms":
                _num(x, f"{at}.also_differs.{key}", null=True)
            else:
                _text(x, f"{at}.also_differs.{key}", _RATIO if key == "ratio_class" else _GENERATION)


def _check_engine(body: Any, engine: str, at: str, releases: Dict[str, Any], registry: Dict[Tuple[str, str], str],
                  groups: Dict[str, Any], pairs: Dict[str, Any], sources: Dict[str, Any]) -> None:
    _obj(body, at)
    identical: Dict[str, Any] = {}
    for i, g in enumerate(_arr(body.get("identity_groups"), f"{at}.identity_groups")):
        gat = f"{at}.identity_groups[{i}]"
        _obj(g, gat)
        gid = _text(g.get("id"), f"{gat}.id", _GROUP_ID)
        kind = _one_of(g.get("kind"), KINDS, f"{gat}.kind")
        _one_of(g.get("relation"), _IDENTICAL, f"{gat}.relation")
        members = _names(g.get("members"), f"{gat}.members", 2)
        curated = groups.get(gid)
        _need(curated is not None and curated["kind"] == kind and curated["relation"] == g["relation"]
              and sorted(curated["members"]) == sorted(members), "an identical group the groups do not hold", gat)
        figures = _obj(g.get("figures"), f"{gat}.figures")
        _keys(figures, _CONTEXT if kind == "filter" else _RATE, "a context or a rate of its kind", f"{gat}.figures")
        for key, node in figures.items():
            fat = f"{gat}.figures.{key}"
            _obj(node, fat)
            for quantity, leaf in node.items():
                _need(quantity in _FIGURES, "a quantity no group figure holds", fat)
                _obj(leaf, f"{fat}.{quantity}")
                value = leaf.get("value")
                if not isinstance(value, Num):
                    _bound(value, ("ge", "le"), f"{fat}.{quantity}.value")
        _need(gid not in identical, "an identical group held twice", gat)
        identical[gid] = g

    for kind, plural in KINDS.items():
        entries = _obj(body.get(plural), f"{at}.{plural}")
        _keys(entries, _SLUG, "a slug", f"{at}.{plural}")
        for slug, e in entries.items():
            eat = f"{at}.{plural}.{slug}"
            (_check_filter if kind == "filter" else _check_modulator)(e, eat, sources)
            _need(registry.get((kind, e["name"])) == slug, "an entry whose key is not the registry's slug of its name",
                  eat)
            _need(e["release"] in releases, "a release the releases do not hold", f"{eat}.release")
            # Which engine's facts a family has is read off its releases
            # (Atlas.holds): an entry's release is its own engine's
            _need(e["release"].split("/")[0] == engine, "a release of another engine", f"{eat}.release")
            for key, held, what in (("groups", groups, "a group"), ("pairs", pairs, "a pair")):
                for ref in e[key]:
                    other = held.get(ref)
                    _need(other is not None and other["kind"] == kind and e["name"] in other["members"],
                          f"{what} that does not name it", f"{eat}.{key}")
            if "identity_group" in e:
                ident = identical.get(e["identity_group"])
                _need(ident is not None and e["name"] in ident["members"] and e["identity_group"] in e["groups"],
                      "an identical group that does not name it", f"{eat}.identity_group")

    modulators = body["modulators"]
    fullscale = _obj(body.get("fullscale"), f"{at}.fullscale")
    for slug, rates in fullscale.items():
        _need(slug in modulators, "a modulator the engine does not hold", f"{at}.fullscale")
        fat = f"{at}.fullscale.{slug}"
        held = {**modulators[slug]["rates_refused"], **modulators[slug]["rates"]}
        for rate, volumes in _obj(rates, fat).items():
            _need(rate in held, "a rate its modulator does not hold", fat)
            _keys(_obj(volumes, f"{fat}.{rate}"), _VOLUME, "a volume", f"{fat}.{rate}")
            for volume, cell in volumes.items():
                _obj(cell, f"{fat}.{rate}.{volume}")
                for key, _ in TONE_PAIR:
                    if key in cell:
                        _num(cell[key], f"{fat}.{rate}.{volume}.{key}", null=True)


def _check_common(e: Any, at: str, sources: Dict[str, Any]) -> None:
    _obj(e, at)
    _text(e.get("name"), f"{at}.name", _NAME)
    _text(e.get("release"), f"{at}.release", _RELEASE)
    for key in ("groups", "pairs"):
        for ref in _arr(e.get(key), f"{at}.{key}"):
            _text(ref, f"{at}.{key}")
    if "identity_group" in e:
        _text(e["identity_group"], f"{at}.identity_group", _GROUP_ID)
    if "history" in e:
        history = _obj(e["history"], f"{at}.history")
        _need(history, "no event", f"{at}.history")
        for key, event in history.items():
            hat = f"{at}.history.{key}"
            _need(key in ("introduced", "renamed_from", "removed_in"), "an event the history does not hold",
                  f"{at}.history")
            _obj(event, hat)
            if key == "renamed_from":
                _text(event.get("name"), f"{hat}.name", _NAME)
            if key != "renamed_from" or "release" in event:
                _text(event.get("release"), f"{hat}.release", _PRODUCT)
            _cites(event.get("sources"), f"{hat}.sources", sources, 1)
    if "intent" in e:
        for i, line in enumerate(_arr(e["intent"], f"{at}.intent", 1)):
            iat = f"{at}.intent[{i}]"
            _obj(line, iat)
            _text(line.get("id"), f"{iat}.id", _SLUG)
            _text(line.get("text"), f"{iat}.text")
            _cites(line.get("sources"), f"{iat}.sources", sources, 1)


def _check_filter(e: Any, at: str, sources: Dict[str, Any]) -> None:
    _check_common(e, at, sources)
    modes = _obj(e.get("modes"), f"{at}.modes")
    for mode, values in (("pcm", ("measured", "not-measured")), ("sdm", ("pcm-measurement", "not-measured"))):
        if mode in modes:
            _one_of(modes[mode], values, f"{at}.modes.{mode}")
    # Each source beside its pair: HQPlayer's label with the log's name of the
    # design, the manual's mark with the manual's rows it is read from.
    for a, b in (("apodizing", "log_name"), ("apod_doc", "doc_sources")):
        _need((a in e) == (b in e), f"{a} without {b}" if a in e else f"{b} without {a}", at)
    if "apodizing" in e:
        _bool(e["apodizing"], f"{at}.apodizing")
        _text(e["log_name"], f"{at}.log_name", _LOG_NAME)
    if "apod_doc" in e:
        _one_of(e["apod_doc"], ("Y", "½", "N"), f"{at}.apod_doc")
        _cites(e["doc_sources"], f"{at}.doc_sources", sources, 1)
    if "variant_of" in e:
        _text(e["variant_of"], f"{at}.variant_of", _NAME)
    if "genre_doc" in e:
        genres = _arr(e["genre_doc"], f"{at}.genre_doc", 1)
        for token in genres:
            _one_of(token, _GENRES, f"{at}.genre_doc")
        _need(len(set(genres)) == len(genres), "a genre held twice", f"{at}.genre_doc")
    contexts = _obj(e.get("contexts"), f"{at}.contexts")
    _keys(contexts, _CONTEXT, "a context", f"{at}.contexts")
    for key, c in contexts.items():
        _check_context(c, f"{at}.contexts.{key}")


def _check_context(c: Any, at: str) -> None:
    _obj(c, at)
    if "void" in c:
        _need(len(c) == 1, "a void context with readings", at)
        for reason in _arr(c["void"], f"{at}.void", 1):
            _text(reason, f"{at}.void")
        return
    _one_of(c.get("phase"), ("linear", "intermediate", "minimum"), f"{at}.phase")
    _bool(c.get("time_invariant"), f"{at}.time_invariant")
    for key in ("f_3db_khz", "transition_end_khz", "pre_ring_ms_100", "post_ring_ms_100", "length_ms_100"):
        if key in c:
            _num(c[key], f"{at}.{key}", null=True)
    for key in ("att_nyquist_db", "stopband_min_db"):
        x = c.get(key)
        if x is not None and not isinstance(x, Num):
            _bound(x, ("ge",), f"{at}.{key}")
    if "filter_delay_ms" in c:
        delay = _obj(c["filter_delay_ms"], f"{at}.filter_delay_ms")
        _num(delay.get("v"), f"{at}.filter_delay_ms.v")
        _num(delay.get("pm"), f"{at}.filter_delay_ms.pm")
    if "position_dependent" in c:
        ranges = _obj(c["position_dependent"], f"{at}.position_dependent")
        for key, r in ranges.items():
            _obj(r, f"{at}.position_dependent")
            for end in ("min", "median", "max"):
                # all three, as a range is drawn by its ends (_range)
                _need(end in r, "a range without its three ends", f"{at}.position_dependent")
                _num(r[end], f"{at}.position_dependent", null=True)


def _check_modulator(e: Any, at: str, sources: Dict[str, Any]) -> None:
    _check_common(e, at, sources)
    _text(e.get("generation"), f"{at}.generation", _GENERATION)
    s = _obj(e.get("structure"), f"{at}.structure")
    _int(s.get("order"), f"{at}.structure.order")
    _bool(s.get("ec"), f"{at}.structure.ec")
    _bool(s.get("adaptive"), f"{at}.structure.adaptive")
    for key in ("multibit", "tuning"):
        _need(key in s and (s[key] is None or (type(s[key]) is str and _LINE.fullmatch(s[key]))),
              "neither null nor text", f"{at}.structure.{key}")
    _need(s.get("src") == "name", "not read off the name", f"{at}.structure.src")
    _text(e.get("context_filter"), f"{at}.context_filter", _NAME)
    for key in ("rates", "rates_refused"):
        rates = _obj(e.get(key), f"{at}.{key}")
        _keys(rates, _RATE, "a rate", f"{at}.{key}")
        for rate, x in rates.items():
            rat = f"{at}.{key}.{rate}"
            _obj(x, rat)
            _int(x.get("fs_in"), f"{rat}.fs_in")
            # The labels name a DSD rate by its multiple of a 44.1k or 48k source.
            _need(x["fs_in"] in FAMILY and int(rate) % int(x["fs_in"]) == 0, "a rate no label names", rat)
            if key == "rates_refused":
                _text(x.get("line"), f"{rat}.line", _REFUSAL)
                continue
            idle = x.get("idle_db")
            if not isinstance(idle, Num):
                _bound(idle, ("le",), f"{rat}.idle_db")


# -- The atlas -------------------------------------------------------------------

# HQPlayer's engine is read up to this length, a longer one as none: the
# string is HQPlayer's own, and int() raises on a digit run past 4300 digits —
# the sheets' state and the card would answer 500.
MAX_ENGINE_LEN = 64


def _version(engine: Optional[str]) -> Tuple[int, ...]:
    """GetInfo's engine as a comparable tuple, read as playback.hqp_load.build
    reads it (that module opens the database; this one never does): () for
    none, and for one past MAX_ENGINE_LEN — an engine the atlas does not know."""
    engine = engine or ""
    if len(engine) > MAX_ENGINE_LEN:
        return ()
    return tuple(int(x) for x in re.findall(r"\d+", engine)[:3])


def _first_context(entry: Dict[str, Any]) -> Optional[str]:
    """The context a filter's chips and card open on: the first the file lists
    that is not void — 1x · 44.1k → 705.6k where measured, as the site's
    summary picks it, since the file lists that one first."""
    return next((key for key, c in entry["contexts"].items() if "void" not in c), None)


class Found(NamedTuple):
    """What the atlas holds for one of HQPlayer's names. `state`: measured;
    pcm-measurement (a filter in SDM mode, its PCM facts); refused (a modulator
    HQPlayer refused at every measured rate); not-measured (a name the
    registry lacks, an entry the engine lacks, a mode the entry was not
    measured in). `entry` and `slug` are None where the file holds none."""
    kind: str
    state: str
    entry: Optional[Dict[str, Any]]
    slug: Optional[str]
    engine: Optional[str]
    origin: str


class Atlas:
    """A file read and checked. `origin` says where it was read from —
    "shipped" or "overlay" — and the results carry it, so a reader that must
    keep the node's own copy to itself can."""

    def __init__(self, doc: Any, origin: str):
        _check(doc)
        self.origin = origin
        self.releases: Dict[str, Optional[str]] = doc["releases"]
        self.engines: List[str] = sorted(doc["engines"], key=_version)
        self._engines = doc["engines"]
        self._slugs = {(row["kind"], row["name"]): row["slug"] for row in doc["slugs"]}
        self._groups = {g["id"]: g for g in doc["groups"]}
        self._pairs = {p["id"]: p for p in doc["pairs"]}
        self._sources = {s["id"]: s for s in doc["sources"]}
        self._identical = {e: {g["id"]: g for g in body["identity_groups"]} for e, body in doc["engines"].items()}

    def slug_of(self, kind: str, name: str) -> Optional[str]:
        """The registry's slug of HQPlayer's `name`, or None: never computed,
        so a name the registry has no row for has none."""
        return self._slugs.get((kind, name))

    def engine_for(self, user_engine: Optional[str], kind: Optional[str] = None) -> Optional[str]:
        """The engine whose facts this HQPlayer is shown — of `kind`'s names,
        among the engines that hold their family's release (holds): the
        newest the atlas holds not newer than its own, else the oldest —
        within its own major version only: a major release may change what a
        name does, and a label does not undo a wrong number shown as
        measured. A file's releases are per engine and family, so a newer
        engine measured in one family first leaves the other's names with
        the engine before it. None when the atlas holds no engine of that
        major (holding that family), or `_version` reads HQPlayer's as
        none."""
        own = _version(user_engine)
        if not own:
            return None
        same = [e for e in self.engines if _version(e)[0] == own[0] and (kind is None or self.holds(e, kind))]
        older = [e for e in same if _version(e) <= own]
        return older[-1] if older else (same[0] if same else None)

    def named_engine(self, user_engine: Optional[str], kind: str) -> Optional[str]:
        """The engine the words name of `kind`'s family: the one whose facts
        are shown, else the newest that holds the family; None when none
        does."""
        held = [e for e in self.engines if self.holds(e, kind)]
        return self.engine_for(user_engine, kind) or (held[-1] if held else None)

    def engine_note(self, user_engine: Optional[str], kind: str) -> List[Any]:
        """The head's engine line, and the card's, of `kind`'s family — one
        the file speaks of (speaks_of)."""
        named = self.named_engine(user_engine, kind)
        note = _phrase(MEASURED_ON, engine=named)
        if not user_engine:
            note.append(NO_ENGINE)
        elif _version(user_engine) != _version(named):
            note += _phrase(RUNS_ENGINE, engine=user_engine)
        return note

    def holds(self, engine: str, kind: str) -> bool:
        """Whether the file holds a release, dated or not, of the family
        `kind`'s names are measured in — a filter's PCM, a modulator's SDM —
        on `engine`."""
        return f"{engine}/{RELEASE_FAMILY[kind]}" in self.releases

    def speaks_of(self, user_engine: Optional[str], kind: str) -> bool:
        """Whether the file says anything of `kind`'s names to this HQPlayer:
        it holds their family's release, on HQPlayer's own major wherever the
        file holds that major. A shipped file holds the dated releases alone:
        with one family dated and the other not, it says nothing of the
        other's names. The SDM release's own filters, the names HQPlayer
        lists in SDM mode alone, are the filter sheet's names and go with
        PCM's. Another major's HQPlayer, or one that reports no engine, is
        told so by the engine line (engine_for is None)."""
        if self.named_engine(user_engine, kind) is None:
            return False
        return self.engine_for(user_engine, kind) is not None or self.engine_for(user_engine) is None

    def lookup(self, kind: str, name: str, engine: Optional[str], mode: Optional[str]) -> Optional[Found]:
        """What the atlas says of HQPlayer's `name` on `engine` (engine_for's)
        in `mode` ("pcm", "sdm"; a modulator's is SDM's); None for a kind it
        holds no entries of — a dither."""
        if kind not in KINDS:
            return None
        slug = self.slug_of(kind, name)
        entry = self._engines[engine][KINDS[kind]].get(slug) if engine and slug else None
        state = "not-measured"
        if entry is not None:
            if kind == "filter":
                listed = entry["modes"].get(mode)
                if listed in ("measured", "pcm-measurement") and _first_context(entry):
                    state = listed
            elif entry["rates"]:
                state = "measured"
            elif entry["rates_refused"]:
                state = "refused"
        return Found(kind, state, entry, slug, engine, self.origin)

    def identical(self, found: Found) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """The entry's identical group: its figures (the engine's) and its
        words (the curation's); None for an entry in none."""
        gid = found.entry.get("identity_group") if found.entry else None
        return (self._identical[found.engine][gid], self._groups[gid]) if gid else None

    def groups(self, found: Found) -> List[Dict[str, Any]]:
        """Every approved group naming the entry, same magnitude included."""
        return [self._groups[gid] for gid in found.entry["groups"]] if found.entry else []

    def pairs(self, found: Found) -> List[Dict[str, Any]]:
        """Every approved pair naming the entry. A pair's speeds of the
        measuring host are never read."""
        return [self._pairs[pid] for pid in found.entry["pairs"]] if found.entry else []

    def source(self, sid: str) -> Optional[Dict[str, Any]]:
        """A source re-read in full (status O); any other is treated as absent,
        its quote never shown."""
        s = self._sources[sid]
        return s if s["status"] == "O" else None

    def cites(self, ids: Iterable[str]) -> str:
        """The short citations of `ids`, as the site prints them; a source not
        re-read in full is left out."""
        return CITES.join(_cite(s) for s in (self.source(sid) for sid in ids) if s is not None)

    def per_manual(self, entry: Dict[str, Any]) -> str:
        """"per the manual", with the section of the manual's rows the
        entry's marks are read from where they name one."""
        for sid in entry.get("doc_sources", ()):
            s = self.source(sid)
            section = _SECTION.search(s["where"]) if s else None
            if section:
                return PER_SECTION.format(section=section.group(0))
        return PER_MANUAL

    def link(self, found: Found) -> Optional[str]:
        """The entry's page on sautium.net once its release has a date and it
        was measured — the site's own rule for a page; None until then."""
        e = found.entry
        if e is None or found.state == "not-measured" or not self.releases[e["release"]]:
            return None
        if not (_first_context(e) if found.kind == "filter" else e["rates"]):
            return None
        return f"{PAGES}/{KINDS[found.kind]}/{found.slug}/"

    def fullscale(self, found: Found) -> Optional[Dict[str, Any]]:
        return self._engines[found.engine]["fullscale"].get(found.slug)

    def chips_context(self, engine: str, mode: str) -> Optional[str]:
        """The one context every measured filter's chips name, for the head;
        None when they name more than one."""
        keys = {_first_context(e) for e in self._engines[engine]["filters"].values()
                if e["modes"].get(mode) in ("measured", "pcm-measurement")}
        keys.discard(None)
        return keys.pop() if len(keys) == 1 else None


_SECTION = re.compile(r"§[0-9.]+")


def _cite(s: Dict[str, Any]) -> str:
    if s["src"] == "signalyst.doc":
        section = _SECTION.search(s["where"])
        return CITE_MANUAL.format(section=section.group(0)) if section else CITE_DOC.format(**s)
    if s["src"] == "jussi":
        return CITE_JUSSI.format(**s)
    if s["src"] == "owner":
        return CITE_OWNER
    return CITE_OTHER.format(**s)


# -- Phrases ---------------------------------------------------------------------

_FIELD = re.compile(r"\{(\w+)\}")


def _phrase(template: str, **values: Any) -> List[Any]:
    """`template` with each {x} put in: a text, a Num kept as a number, or a
    phrase spliced in."""
    out: List[Any] = []
    at = 0
    for m in _FIELD.finditer(template):
        out.append(template[at:m.start()])
        value = values[m.group(1)]
        out.extend(value if isinstance(value, list) else [value])
        at = m.end()
    out.append(template[at:])
    return out


def _join(phrases: List[List[Any]], sep: str) -> List[Any]:
    out: List[Any] = []
    for i, p in enumerate(phrases):
        out += ([sep] if i else []) + p
    return out


def wire(phrase: List[Any]) -> List[Dict[str, str]]:
    """A phrase as the screen takes it: {"t": text} and {"n": a number's
    token}, adjacent texts merged."""
    out: List[Dict[str, str]] = []
    for part in phrase:
        if isinstance(part, Num):
            out.append({"n": str(part)})
        elif part:
            if out and "t" in out[-1]:
                out[-1] = {"t": out[-1]["t"] + part}
            else:
                out.append({"t": part})
    return out


def plain(parts: List[Dict[str, str]]) -> str:
    """Wire parts as text, a negative number's hyphen-minus drawn as the minus
    sign — as the screen draws it."""
    return "".join(p["t"] if "t" in p else _minus(p["n"]) for p in parts)


def _minus(token: str) -> str:
    return MINUS + token[1:] if token.startswith("-") else token


def _words(items: List[str]) -> str:
    """"a", "a and b", "a, b and c"."""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + AND + items[-1]


def context_label(key: str) -> str:
    """"1x-44.1k-705.6k" as "1x · 44.1k → 705.6k"."""
    slot, source, output = key.split("-")
    return CONTEXT_LABEL.format(slot="Nx" if slot == "nx" else "1x", source=source, output=output)


def rate_label(rate: str, fs_in: str) -> str:
    """"DSD256 · from 44.1k": the rate key over its source's, a label — the
    only arithmetic on the file's keys."""
    return RATE_LABEL.format(n=int(rate) // int(fs_in), family=FAMILY[fs_in])


def rate_short(rate: str, fs_in: str) -> str:
    """"DSD256", "DSD512 (48k)"."""
    return (RATE_SHORT_48 if fs_in == "48000" else RATE_SHORT).format(n=int(rate) // int(fs_in))


def rate_name(hz: int) -> str:
    """A rate HQPlayer reports, named as the HQPlayer screen names it
    (app-shell.js fmtRateLabel) — the one namer for every text the node
    words: kHz up to 768 kHz; above, a DSD rate by its power-of-two multiple
    (64 and up) of a 44.1k, 48k or 32k base, any base but 44.1k said beside
    it ("DSD256", "DSD512 (48k)", "DSD64 (32k)"); anything else — the PCM
    rates 1.4112 and 1.536 MHz among them — in MHz. "—" for none."""
    if not hz:
        return "—"
    if hz <= 768000:
        return f"{hz // 1000} kHz" if hz % 1000 == 0 else f"{hz / 1000:.1f} kHz"
    for base, beside in ((44100, ""), (48000, " (48k)"), (32000, " (32k)")):
        mult = hz // base
        if hz % base == 0 and mult >= 64 and mult & (mult - 1) == 0:
            return f"DSD{mult}{beside}"
    mhz = hz / 1_000_000
    return f"{mhz:.0f} MHz" if mhz % 1 == 0 else f"{mhz:.2f} MHz"


def _by_rate(rates: Dict[str, Dict[str, Any]]) -> List[Tuple[str, Dict[str, Any]]]:
    """A modulator's rates as the site lists them: the 44.1k family first,
    then by rate — labels, never a measured value."""
    return sorted(rates.items(), key=lambda kv: (int(kv[1]["fs_in"]), int(kv[0])))


def _in_order(names: Iterable[str], order: Dict[str, int]) -> List[str]:
    """Names in HQPlayer's order; one it does not list now after, in the
    file's."""
    return sorted(names, key=lambda name: order.get(name, len(order)))


_STEM = re.compile(r"([^- ]+)-([^ ]+)( [0-9]+\+fs)?")


def _group_names(members: List[str], order: Dict[str, int]) -> str:
    """A group's names as the site writes them: more than two sharing one stem
    and one tuning are "{stem}[ {tuning}]: -a, -b, -c"."""
    names = _in_order(members, order)
    if len(names) > 2:
        parts = [_STEM.fullmatch(name) for name in names]
        if all(parts) and len({p[1] for p in parts}) == 1 and len({p[3] or "" for p in parts}) == 1:
            return STEM_NAMES.format(stem=parts[0][1] + (parts[0][3] or ""), ends=", ".join(f"-{p[2]}" for p in parts))
    return _words(names)


_unworded: set = set()
_unworded_lock = threading.Lock()
_KEY = re.compile(r"[a-z0-9_]{1,40}")


def _no_words(what: str, name: str) -> None:
    with _unworded_lock:
        if (what, name) in _unworded:
            return
        _unworded.add((what, name))
    if _KEY.fullmatch(name):
        logger.info("hqp_atlas: no words for the %s %s; it is not shown", what, name)
    else:
        logger.info("hqp_atlas: no words for a %s of another shape; it is not shown", what)


# -- A row -----------------------------------------------------------------------


def _blank() -> Dict[str, Any]:
    return {"chips": [], "words": None, "tag": None, "caption": None, "line3": None, "bracket": None, "card": False}


def _chip(phrase: List[Any], title: str, muted: bool = False) -> Dict[str, Any]:
    chip: Dict[str, Any] = {"parts": wire(phrase), "title": title}
    if muted:
        chip["muted"] = True
    return chip


def _ratio(parsed: Optional[Dict[str, Any]]) -> Optional[str]:
    """The ratio class as HQPlayer writes it, "up" included: "2^x up"."""
    return parsed["ratio"] + (UP if parsed["up"] else "") if parsed and "ratio" in parsed else None


def runtime(kind: str, row: Dict[str, Any]) -> Dict[str, Any]:
    """One row's facts in HQPlayer's own words, from its live list: a filter's
    ratio class, focus words and rating tag, a modulator's generation, or the
    line as HQPlayer gives it when its shape is not read. All the pickers show
    while the node holds no atlas."""
    out = _blank()
    text, parsed = row.get("description") or "", row.get("parsed")
    if not text or kind not in KINDS:
        return out
    if parsed is None:
        out["caption"] = wire([text])
    elif kind == "filter":
        out["chips"].append(_chip([_ratio(parsed)], RATIO_TITLE))
        if parsed["focus"]:
            out["words"] = {"text": ", ".join(parsed["focus"]), "title": FOCUS_TITLE}
        out["tag"] = {"text": TAG.format(n=parsed["rating"]), "title": TAG_TITLE.format(n=parsed["rating"])}
        out["card"] = True
    else:
        out["chips"].append(_chip([text], GENERATION_TITLE))
    return out


def head(atlas: Optional[Atlas], user_engine: Optional[str], mode: Optional[str]) -> Optional[Dict[str, Any]]:
    """The sheets' head lines while the node holds an atlas: each sheet's
    engine line, and on the filter sheet the chips' context and, in SDM
    mode, where the properties come from. No line for a sheet whose family
    the file says nothing of (Atlas.speaks_of): its rows are HQPlayer's
    alone."""
    if atlas is None:
        return None
    out: Dict[str, Any] = {"origin": atlas.origin}
    for kind, plural in KINDS.items():
        lines = []
        if atlas.speaks_of(user_engine, kind):
            lines.append(wire(atlas.engine_note(user_engine, kind)))
            engine = atlas.engine_for(user_engine, kind)
            if kind == "filter" and engine is not None and mode in ("pcm", "sdm"):
                key = atlas.chips_context(engine, mode)
                if key:
                    lines.append(wire(_phrase(PROPERTIES_AT, context=context_label(key))))
                if mode == "sdm":
                    lines.append(wire([SDM_HEAD]))
        out[plural] = lines
    return out


def facts(atlas: Optional[Atlas], kind: str, rows: List[Dict[str, Any]], user_engine: Optional[str],
          mode: Optional[str]) -> List[Dict[str, Any]]:
    """The facts of each row of HQPlayer's live `kind` list ("filter",
    "modulator", "dither"), in its order: HQPlayer's own, and with an atlas the
    measured ones — the chips, the rating tag, a caption, a twin's line, an
    identical group's bracket. `mode` is the list's ("pcm", "sdm"); a filter
    list in no known mode, and a list of a family the file says nothing of
    (Atlas.speaks_of), get HQPlayer's facts alone."""
    if (atlas is None or kind not in KINDS or (kind == "filter" and mode not in ("pcm", "sdm"))
            or not atlas.speaks_of(user_engine, kind)):
        return [runtime(kind, row) for row in rows]
    engine = atlas.engine_for(user_engine, kind)
    named = atlas.named_engine(user_engine, kind)
    order = _order(rows)
    out = []
    for row in rows:
        found = atlas.lookup(kind, row["name"], engine, mode)
        own = runtime(kind, row)
        f = _blank()
        f["tag"], f["caption"], f["card"] = own["tag"], own["caption"], True
        if found.state == "not-measured":
            f["chips"] = own["chips"] + [_chip(_phrase(NOT_MEASURED, engine=named),
                                               NOT_MEASURED_TITLE.format(engine=named), muted=True)]
        elif kind == "filter":
            f["chips"] = _filter_chips(atlas, found) + own["chips"]
            f["caption"] = own["caption"] or _apodizing_caption(found.entry)
        else:
            f["chips"] = own["chips"] + _modulator_chips(found)
        if found.state != "not-measured":
            f["line3"] = _twin(atlas, found)
            f["bracket"] = _bracket(atlas, found, rows, order)
        out.append(f)
    return out


def _order(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    return {row["name"]: i for i, row in enumerate(rows)}


def _figures_at(atlas: Atlas, found: Found, key: str) -> Dict[str, Any]:
    """An identical group's figures at a context or a rate, by quantity."""
    ident = atlas.identical(found)
    return {q: leaf["value"] for q, leaf in ident[0]["figures"].get(key, {}).items()} if ident else {}


def _filter_chips(atlas: Atlas, found: Found) -> List[Dict[str, Any]]:
    key = _first_context(found.entry)
    c, label = found.entry["contexts"][key], context_label(key)
    figures = _figures_at(atlas, found, key)
    chips = [_chip(_phrase(PHASE_CHIP, phase=c["phase"]), PHASE_TITLE.format(context=label))]
    pre, post = (figures.get(q, c.get(q)) for q in ("pre_ring_ms_100", "post_ring_ms_100"))
    if isinstance(pre, Num) and isinstance(post, Num):
        # The two sides read alike as text, or each is given: a minimum- or
        # intermediate-phase impulse rings unevenly.
        ring = _phrase(RING_EVEN, pre=pre) if pre == post else _phrase(RING_APART, pre=pre, post=post)
        chips.append(_chip(ring, RING_TITLE.format(context=label)))
    if not c["time_invariant"]:
        chips.append(_chip([POSITION_CHIP], POSITION_TITLE.format(context=label)))
    return chips


def _apodizing_caption(entry: Dict[str, Any]) -> Optional[List[Dict[str, str]]]:
    """HQPlayer's label and the manual's mark, each with its source; the
    manual's N, or no row, says nothing here."""
    clauses = []
    if entry.get("apodizing"):
        clauses.append(APODIZING_CLAUSE)
    if entry.get("apod_doc") in ("Y", "½"):
        clauses.append(APOD_CLAUSE.format(mark=entry["apod_doc"]))
    return wire([CLAUSES.join(clauses)]) if clauses else None


def _order_words(structure: Dict[str, Any]) -> str:
    word = ORDER.get(structure["order"])
    return ORDER_WORD.format(order=word) if word else ORDER_NUMBER.format(order=structure["order"])


def _modulator_chips(found: Found) -> List[Dict[str, Any]]:
    e = found.entry
    chips = [_chip([_order_words(e["structure"])], ORDER_TITLE)]
    refused = _by_rate(e["rates_refused"])
    short = _words([rate_short(rate, x["fs_in"]) for rate, x in refused]) if refused else ""
    if found.state == "refused":
        chips.append(_chip([REFUSED_ALL], REFUSED_ALL_TITLE.format(rates=short)))
    elif refused:
        labels = _words([rate_label(rate, x["fs_in"]) for rate, x in refused])
        chips.append(_chip(_phrase(REFUSED_SOME, rates=short), REFUSED_SOME_TITLE.format(rates=labels,
                                                                                          engine=found.engine)))
    return chips


def _twin(atlas: Atlas, found: Found) -> Optional[List[Dict[str, str]]]:
    """A twin's line: the other member of an identical group of two."""
    ident = atlas.identical(found)
    if ident is None or len(ident[0]["members"]) != 2:
        return None
    other = next(m for m in ident[0]["members"] if m != found.entry["name"])
    return wire(_phrase(TWIN[ident[0]["relation"]], other=other))


def _bracket(atlas: Atlas, found: Found, rows: List[Dict[str, Any]],
             order: Dict[str, int]) -> Optional[Dict[str, Any]]:
    """An identical group of three or more: its head, drawn once above each
    run of its members, keyed by the first member's HQPlayer index — never
    the group's id."""
    ident = atlas.identical(found)
    if ident is None or len(ident[0]["members"]) < 3:
        return None
    members, g = ident[0]["members"], ident[1]
    first = next(row["index"] for row in rows if row["name"] in members)
    names = _group_names(members, order)
    lines = [wire(_phrase(BRACKET, names=names, say=g["say"]) if g.get("say") else _phrase(BRACKET_PLAIN,
                                                                                            names=names))]
    if g.get("choose_by"):
        lines.append(wire(_phrase(CHOOSE_BY, choose_by=g["choose_by"])))
    return {"key": first, "head": lines}


# -- The card --------------------------------------------------------------------


def card(atlas: Optional[Atlas], kind: str, name: str, rows: List[Dict[str, Any]], user_engine: Optional[str],
         mode: Optional[str]) -> Optional[Dict[str, Any]]:
    """The detail card of HQPlayer's `name`, which `rows` — its live list of
    that kind — holds now; None when it does not. Status lines, then sections
    in order, a section with nothing to say left out; `link` only once the
    entry's release has a date."""
    row = next((r for r in rows if r["name"] == name), None)
    if row is None:
        return None
    out: Dict[str, Any] = {"title": name, "origin": None, "status": [], "sections": [], "link": None}
    found = None
    # A name of a family the file says nothing of (speaks_of): as without a file
    if (atlas is not None and kind in KINDS and (kind == "modulator" or mode in ("pcm", "sdm"))
            and atlas.speaks_of(user_engine, kind)):
        found = atlas.lookup(kind, name, atlas.engine_for(user_engine, kind), mode)
        out["origin"] = atlas.origin
        out["status"].append(wire(atlas.engine_note(user_engine, kind)))
        if found.state == "not-measured":
            out["status"].append(wire(_phrase(NOT_MEASURED_CARD, engine=atlas.named_engine(user_engine, kind))))
        elif found.state == "pcm-measurement":
            ratio = _ratio(row.get("parsed"))
            out["status"] += [wire([SDM_PATH]),
                              wire(_phrase(SDM_LISTED, ratio=ratio) if ratio else [SDM_LISTED_PLAIN])]
    sections = [_says(kind, row, found)]
    if found is not None and found.state != "not-measured":
        order = _order(rows)
        e = found.entry
        if kind == "filter":
            sections += [_apodizing(atlas, e), _manual(atlas, e)]
        sections += [_group(g, name, order) for g in atlas.groups(found)]
        if kind == "filter":
            sections.append(_properties(atlas, found, order))
        else:
            sections += [_per_rate(atlas, found, order), _refused(e), _fullscale(atlas, found)]
        sections += [_differs(atlas, found), _purpose(atlas, e), _history(atlas, e), _sources(atlas, found)]
        link = atlas.link(found)
        if link:
            out["link"] = {"href": link, "text": LINK}
    out["sections"] = [s for s in sections if s is not None]
    return out


def _section(head: Optional[str], items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return {"head": head, "items": items} if items else None


def _term(term: str, phrase: List[Any]) -> Dict[str, Any]:
    return {"term": term, "def": wire(phrase)}


def _line(phrase: List[Any]) -> Dict[str, Any]:
    return {"line": wire(phrase)}


def _note(phrase: List[Any]) -> Dict[str, Any]:
    return {"note": wire(phrase)}


def _table(columns: List[str], rows: List[Dict[str, Any]], corner: Optional[str] = None) -> Dict[str, Any]:
    return {"table": {"corner": corner, "columns": columns, "rows": rows}}


def _says(kind: str, row: Dict[str, Any], found: Optional[Found]) -> Optional[Dict[str, Any]]:
    text, parsed = row.get("description") or "", row.get("parsed")
    items = []
    head = SAYS
    if kind == "filter" and parsed:
        items += [_term(RATING, _phrase(RATING_DEF, n=str(parsed["rating"]))),
                  _term(FOCUS, [", ".join(parsed["focus"]) or FOCUS_NONE]),
                  _term(RATIO, [_ratio(parsed)])]
    elif kind == "modulator":
        if parsed:
            items.append(_term(GENERATION_TITLE, [text]))
        if found is not None and found.state in ("measured", "refused"):
            items.append(_term(STRUCTURE, [_structure(found.entry["structure"])]))
            head = SAYS_STRUCTURE
    if text:
        items.append(_line(_phrase(DESCRIBED, description=text)))
    return _section(head, items)


def _structure(s: Dict[str, Any]) -> str:
    bits = [_order_words(s), ADAPTIVE[s["adaptive"]], EC[s["ec"]]]
    if s["tuning"]:
        bits.append(TUNED.format(tuning=s["tuning"]))
    if s["multibit"]:
        bits.append(MULTIBIT.format(multibit=s["multibit"]))
    return ", ".join(bits)


def _apodizing(atlas: Atlas, e: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    items = []
    if "apodizing" in e:
        items.append(_term(LABEL_TERM, _phrase(LABEL_DEF, label=LABEL[e["apodizing"]], name=e["log_name"])))
    if "apod_doc" in e:
        items.append(_term(APOD_TERM.format(per=atlas.per_manual(e)),
                           [APOD_HALF if e["apod_doc"] == "½" else e["apod_doc"]]))
    else:
        items.append(_term(APOD_TERM.format(per=PER_MANUAL), [APOD_NONE]))
    return _section(APODIZING, items)


def _manual(atlas: Atlas, e: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    per = atlas.per_manual(e)
    items = []
    if "genre_doc" in e:
        genres = ", ".join(GENRE.get(token, token) for token in e["genre_doc"])
        items.append(_term(GENRE_TERM.format(per=per), _phrase(GENRE_DEF, genres=genres)))
    if "variant_of" in e:
        items.append(_term(VARIANT_TERM.format(per=per), [e["variant_of"]]))
    return _section(MANUAL, items)


def _group(g: Dict[str, Any], name: str, order: Dict[str, int]) -> Optional[Dict[str, Any]]:
    relation = RELATION.get(g["relation"])
    if relation is None:
        _no_words("relation", g["relation"])
        return None
    members = _in_order(g["members"], order)
    others = ", ".join(m for m in members if m != name)
    items = [_line(_phrase(GROUP_LEAD, relation=relation, others=others, say=g["say"]) if g.get("say")
                   else _phrase(GROUP_LEAD_PLAIN, relation=relation, others=others))]
    if "cross_family" in g:
        across = "; ".join(ACROSS_ONE.format(name=m, what=g["cross_family"][m]) for m in members)
        items.append(_line(_phrase(ACROSS, families=across)))
    if g.get("choose_by"):
        items.append(_line(_phrase(CHOOSE_BY, choose_by=g["choose_by"])))
    if g.get("note"):
        items.append(_line([g["note"]]))
    return _section(None, items)


def _value(x: Any, used: set) -> List[Any]:
    """One reading: a number, a bound with what set it, or "—" where the
    method defines none."""
    if x is None:
        used.add("none")
        return [NONE]
    if isinstance(x, Num):
        return [x]
    used.add("bound")
    out = _phrase(AT_LEAST if x["b"] == "ge" else AT_MOST, v=x["v"])
    mark = BY.get(x.get("by"))
    if mark:
        used.add(mark)
        out.append(mark)
    return out


def _range(r: Dict[str, Any], used: set) -> List[Any]:
    """A reading over the impulse's positions (`position_dependent`, the only
    place a range stands — never told from a bound by its keys): its ends,
    one value where they meet."""
    used.add("range")
    ends = [_value(r[end], used) for end in ("min", "median", "max")]
    return ends[0] if ends[0] == ends[2] else _phrase(RANGE, min=ends[0], median=ends[1], max=ends[2])


def _notes(used: set) -> List[Dict[str, Any]]:
    return [_note([NOTES[key]]) for key in NOTES if key in used]


def _context_table(e: Dict[str, Any], key: str, figures: Dict[str, Any], used: set) -> Dict[str, Any]:
    c = e["contexts"][key]
    rows = []
    for label, field in ROWS:
        if field == "phase":
            value = [c["phase"]]
        elif field == "time_invariant":
            value = [YES_NO[c["time_invariant"]]]
        elif field == "filter_delay_ms":
            if field not in c:
                continue
            value = _phrase(PLUS_MINUS, v=c[field]["v"], pm=c[field]["pm"])
        elif field in figures:
            value = _value(figures[field], used)
        elif field in c:
            value = _value(c[field], used)
        elif field in c.get("position_dependent", {}):
            value = _range(c["position_dependent"][field], used)
        else:
            continue
        rows.append({"label": label, "cells": [wire(value)]})
    return _table([context_label(key)], rows)


def _properties(atlas: Atlas, found: Found, order: Dict[str, int]) -> Optional[Dict[str, Any]]:
    """A filter's numbers per measured context: the chips' context open, the
    others behind a disclosure. A member of an identical group shows the
    group's figure wherever the group holds one."""
    e = found.entry
    first = _first_context(e)
    used: set = set()
    items = [_context_table(e, first, _figures_at(atlas, found, first), used)] + _notes(used)
    rest = [key for key, c in e["contexts"].items() if key != first and "void" not in c]
    if rest:
        more_used: set = set()
        more = [_context_table(e, key, _figures_at(atlas, found, key), more_used) for key in rest]
        items.append({"more": OTHER_CONTEXTS, "items": more + _notes(more_used - used)})
    ident = atlas.identical(found)
    if ident:
        items.append(_note(_phrase(FIGURES_NOTE, names=_group_names(ident[0]["members"], order))))
    return _section(PROPERTIES, items)


def _per_rate(atlas: Atlas, found: Found, order: Dict[str, int]) -> Optional[Dict[str, Any]]:
    """A modulator's idle noise per measured rate. Its highest stable levels
    stay out: near full scale stands in its own table, under its own words."""
    e = found.entry
    if not e["rates"]:
        return None
    used: set = set()
    rows = []
    for rate, x in _by_rate(e["rates"]):
        idle = _figures_at(atlas, found, rate).get("idle_db", x["idle_db"])
        rows.append({"label": rate_label(rate, x["fs_in"]), "cells": [wire(_value(idle, used))]})
    items = [_table([IDLE], rows, corner=RATE)] + _notes(used)
    ident = atlas.identical(found)
    if ident:
        items.append(_note(_phrase(IDLE_NOTE, names=_group_names(ident[0]["members"], order))))
    return _section(PER_RATE, items)


def _refused(e: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    refused = _by_rate(e["rates_refused"])
    if not refused:
        return None
    lead = _line(_phrase(REFUSED_LEAD, rates=_words([rate_short(rate, x["fs_in"]) for rate, x in refused])))
    lines = [_line(_phrase(REFUSED_LINE, rate=rate_label(rate, x["fs_in"]), line=x["line"])) for rate, x in refused]
    return _section(REFUSED, [lead] + lines)


def _fullscale(atlas: Atlas, found: Found) -> Optional[Dict[str, Any]]:
    """The highest level stable at each HQPlayer volume tested; a volume not
    played at a rate is "not tested"."""
    cells = atlas.fullscale(found)
    if not cells:
        return None
    held = {**found.entry["rates_refused"], **found.entry["rates"]}
    # Volume keys are labels: listed from 0 dB down, as the site does.
    volumes = sorted({volume for played in cells.values() for volume in played}, key=float, reverse=True)
    rows = []
    for rate, x in _by_rate({rate: held[rate] for rate in cells}):
        row = []
        for volume in volumes:
            cell = cells[rate].get(volume)
            if cell is None:
                row.append(wire([NOT_TESTED]))
                continue
            bits = [[NONE_TESTED] if cell[key] is None else _phrase(CELL, v=cell[key], what=what)
                    for key, what in TONE_PAIR if key in cell]
            row.append(wire(_join(bits, "; ")))
        rows.append({"label": rate_label(rate, x["fs_in"]), "cells": row})
    columns = [VOLUME.format(volume=_minus(volume)) for volume in volumes]
    return _section(FULLSCALE, [_line([FULLSCALE_LEAD]), _table(columns, rows, corner=RATE),
                                _note([FULLSCALE_NOTE])])


def _side(key: str, x: Any, used: set) -> List[Any]:
    if key == "apodizing" and type(x) is bool:
        return [LABEL[x]]
    if type(x) is bool:
        return [YES_NO[x]]
    if type(x) is str:
        return [x]
    return _value(x, used)


def _pair_detail(p: Dict[str, Any], i: int, name: str, other: str, used: set) -> Optional[List[Any]]:
    """A filter pair at its default context: each difference and what else
    differs, this entry's value first. A difference without words here, or a
    value of a shape not read, leaves the line out."""
    c = p["contexts"][p["default_context"]]
    items: Dict[str, List[List[Any]]] = {"difference": [], "also_differs": []}
    for part in items:
        for key, values in c[part].items():
            if key == "process_speed_min":
                continue
            words = DIFF.get(key)
            if words is None or not _two_readings(values):
                _no_words("difference", key)
                return None
            label, unit = words
            items[part].append(_phrase(DIFF_ITEM, label=label, a=_side(key, values[i], used),
                                       b=_side(key, values[1 - i], used), unit=unit))
    out = _phrase(PAIR_AT, context=context_label(p["default_context"]), name=name, other=other,
                  diffs=_join(items["difference"], "; "))
    if items["also_differs"]:
        out += _phrase(PAIR_ALSO, diffs=_join(items["also_differs"], "; "))
    return out


def _two_readings(values: Any) -> bool:
    return type(values) is list and len(values) == 2 and all(
        x is None or type(x) in (bool, str, Num) for x in values)


def _differs(atlas: Atlas, found: Found) -> Optional[Dict[str, Any]]:
    name = found.entry["name"]
    items = []
    used: set = set()
    for p in atlas.pairs(found):
        axis = AXIS.get(p["axis"])
        if axis is None:
            _no_words("axis", p["axis"])
            continue
        i = p["members"].index(name)
        other = p["members"][1 - i]
        items.append(_line(_phrase(PAIR_LEAD, axis=axis, other=other)))
        detail = _pair_detail(p, i, name, other, used) if found.kind == "filter" else None
        if detail:
            items.append(_note(detail))
        tail: List[Any] = []
        if p.get("unmeasured_axes"):
            tail += _phrase(UNMEASURED, axes=UNMEASURED_JOIN.join(p["unmeasured_axes"]))
        cites = atlas.cites(p.get("sources", ()))
        if cites:
            tail += ([" "] if tail else []) + _phrase(FROM, cites=cites)
        if tail:
            items.append(_note(tail))
    return _section(DIFFERS, items + _notes(used) if items else [])


def _purpose(atlas: Atlas, e: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The author's stated purpose in Sautium's words, each line with its
    sources; a line whose every source is one not re-read in full is not
    shown."""
    items = []
    for line in e.get("intent", ()):
        cites = atlas.cites(line["sources"])
        if cites:
            items += [_line([line["text"]]), _note(_phrase(IN_OUR_WORDS, cites=cites))]
    return _section(PURPOSE, items)


def _history(atlas: Atlas, e: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    items = []
    for key, template in (("introduced", INTRODUCED), ("renamed_from", RENAMED), ("removed_in", REMOVED)):
        event = e.get("history", {}).get(key)
        cites = atlas.cites(event["sources"]) if event else ""
        if not cites:
            continue
        if key == "renamed_from" and "release" in event:
            template = RENAMED_IN
        items.append(_line(_phrase(template, release=event.get("release", ""), name=event.get("name", ""),
                                   cites=cites)))
    return _section(HISTORY, items + [_note([HISTORY_NOTE])] if items else [])


def _cited(atlas: Atlas, found: Found) -> List[str]:
    """The sources the card cites: the manual's rows, the shown purpose and
    history lines', the groups' and the pairs'."""
    e = found.entry
    # The manual's rows are a filter's (`doc_sources` beside `apod_doc`); a
    # modulator's entry has none, and a key not of its shape is not read
    ids = list(e.get("doc_sources", ())) if found.kind == "filter" else []
    ids += [sid for line in e.get("intent", ()) for sid in line["sources"]]
    ids += [sid for event in e.get("history", {}).values() for sid in event["sources"]]
    ids += [sid for g in atlas.groups(found) if g["relation"] in RELATION for sid in g.get("sources", ())]
    ids += [sid for p in atlas.pairs(found) if p["axis"] in AXIS for sid in p.get("sources", ())]
    return sorted(set(ids))


def _sources(atlas: Atlas, found: Found) -> Optional[Dict[str, Any]]:
    items = [_line(_phrase(MEASURED_BY, engine=found.engine))]
    if found.kind == "modulator":
        items.append(_line(_phrase(CONTEXT_FILTER, filter=found.entry["context_filter"])))
    for sid in _cited(atlas, found):
        s = atlas.source(sid)
        if s is None:
            continue
        line = _phrase(SOURCE, who=s["who"], where=s["where"], date=s["date"])
        if "quote" in s:
            line += _phrase(QUOTE, quote=s["quote"])
        items.append(_line(line))
    return _section(SOURCES, items)
