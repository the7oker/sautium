"""The rate check (backend/hqp_rate_check.py): whether HQPlayer reaches its
output rate from the tracks still to play, and its words — on HQPlayer-shaped
lists with invented names and the synthetic atlas
(fixtures/hqp_atlas/app-synthetic.json: invented modulators refused at
invented rates on the invented engine 9.4.1), and on engine 6.2.4's own PCM
lists (fixtures/hqp_lists/desktop_6.2.4.json) for what was measured there
(UP_MATRIX). Pure: no database, no HQPlayer."""

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

import hqp_rate_check as rc  # noqa: E402
from hqp_atlas import plain  # noqa: E402
from hqp_rate_check import Filter, Inputs, Sources  # noqa: E402
from hqplayer_client import parse_filter_description  # noqa: E402

from test_hqp_atlas import BANNED, MODULATORS, PCM, SDM, _another_engine, _doc, _read, _rows  # noqa: E402

LISTS_624 = Path(__file__).resolve().parent / "fixtures" / "hqp_lists" / "desktop_6.2.4.json"
DSD64 = 2822400
# The rate HQPlayer 6.2.4's log named for a DSD64 track in PCM mode ("Rate: 176400")
STAGE = 176400
# What HQPlayer engine 6.2.4 did (Desktop, PCM mode, the filter in both slots,
# the adaptive output rate off, 2026-10-19), per filter and cell — a source and
# the output rate fixed: "plays"; "silent": not refused, its engine started,
# its Status at 0 for eight seconds (where watched longer, it played on);
# "own": it played at the source's own rate; "stops":
# refused before the engine started ("Requested filter not possible with this
# rate combination A/B, stop", A the source's rate, a DSD64 track's 176.4 kHz);
# "builds": the filter built, then the engine refused ("clHQPlayerEngine::
# Execute(): lInRate > lOutRate").
UP_CELLS = {"44.1k->88.2k": (44100, 88200), "44.1k->44.1k": (44100, 44100), "88.2k->88.2k": (88200, 88200),
            "352.8k->352.8k": (352800, 352800), "88.2k->44.1k": (88200, 44100),
            "352.8k->176.4k": (352800, 176400), "DSD64->352.8k": (DSD64, 352800), "DSD64->88.2k": (DSD64, 88200)}
UP_MATRIX = {name: dict(zip(UP_CELLS, row.split())) for name, row in {
    "sinc-M": "silent silent silent silent silent silent silent silent",
    "closed-form": "plays plays plays plays stops stops plays stops",
    "FFT": "plays stops stops stops plays plays plays plays",
    "sinc-medium": "plays plays plays plays builds builds plays builds",
    "poly-sinc-ext2": "plays plays plays plays plays plays plays plays",
    "poly-sinc-ext2-short": "plays plays plays plays stops stops plays stops",
    "FIR": "plays stops stops stops plays plays plays plays",
    "none": "own plays plays plays own own stops stops",
}.items()}
STOPS = ("stops", "builds")

SOURCES = (22050, 32000, 44100, 48000, 88200, 96000, 176400, 192000)
# The sources each class does not reach, by output rate: 705.6 kHz, 768 kHz,
# DSD128, DSD128 (48k), DSD256 — a class's arithmetic, worked by hand.
NOT_REACHED = {
    "2^x": {705600: {32000, 48000, 96000, 192000},
            768000: {22050, 32000, 44100, 88200, 176400},
            5644800: {32000, 48000, 96000, 192000},
            6144000: {22050, 32000, 44100, 88200, 176400},
            11289600: {32000, 48000, 96000, 192000}},
    "Int": {705600: {32000, 48000, 96000, 192000},
            768000: {22050, 44100, 88200, 176400},
            5644800: {32000, 48000, 96000, 192000},
            6144000: {22050, 44100, 88200, 176400},
            11289600: {32000, 48000, 96000, 192000}},
}
MODES = [{"index": 0, "name": "[source]", "value": -1}, {"index": 1, "name": "PCM", "value": 0},
         {"index": 2, "name": "SDM (DSD)", "value": 1}]
SDM_RATES = [{"index": i, "rate": r} for i, r in enumerate((0, 2822400, 5644800, 11289600, 6144000, 12288000))]
PCM_RATES = [{"index": i, "rate": r} for i, r in enumerate((0, 44100, 88200, 705600, 768000))]


def _inp(ratio="2^x", rate=768000, *, up=False, adaptive="not checked", mode="pcm", x1=True, **kw):
    f = Filter("demo-sinc-a", ratio, up)
    return Inputs(mode, rate, adaptive, f, f if x1 else None, **kw)


def _src(*rates, dsd=0, streamed=0, first=None):
    return Sources({r: 1 for r in rates}, dsd, streamed, first)


def _refused(c):
    return {s for g in c.groups for s in g.sources}


def _said(c):
    """Every sentence of a check: the screen's, the notice's."""
    out = [plain(p) for phrases in rc.lines(c).values() for p in phrases]
    n = rc.notice(c)
    if n:
        out += [plain(n["title"])] + [plain(p) for p in n["text"]]
    return out


def _sdm(tmp_path, *, rate=2, engine="9.4.1", shaper=14, mode=2, adaptive="not checked", described=True, doc=None,
         published=False):
    """HQPlayer in SDM mode with the synthetic atlas (or `doc`, a variant of
    it): demo-sinc-a ("⥣ 2^x") in both slots, the modulator by its index in
    MODULATORS (14: DMX7 512+fs, which the file refuses at DSD128 from 44.1k
    and at DSD256 (48k) from 48k)."""
    filters = _rows(SDM, "filter")
    if not described:
        filters = [{**f, "description": "", "parsed": None} for f in filters]
    state = {"mode": mode, "rate": rate, "filterNx": 10, "filter1x": 10, "shaper": shaper}
    return rc.inputs(state, MODES, SDM_RATES, filters, _rows(MODULATORS, "modulator"),
                     adaptive=adaptive, atlas=_read(tmp_path, doc), engine=engine, published=published)


# -- What runs, and what reaches ---------------------------------------------------

def test_a_source_plays_at_its_own_family():
    assert rc.same_family(11289600, 44100) and rc.same_family(768000, 96000)
    assert rc.same_family(352800, 2822400)              # a DSD source down to PCM
    assert not rc.same_family(12288000, 44100) and not rc.same_family(705600, 96000)
    assert not rc.same_family(12000000, 44100) and not rc.same_family(0, 44100)
    assert [rc.mode_kind(n) for n in ("PCM", "SDM (DSD)", "[source]", None)] == ["pcm", "sdm", None, None]


@pytest.mark.parametrize("up", (False, True))
@pytest.mark.parametrize("ratio", ("2^x", "Int", "Any", "1:1"))
def test_each_class_reaches_what_its_arithmetic_does(ratio, up):
    for rate in (705600, 768000, 5644800, 6144000, 11289600):
        c = rc.check(_inp(ratio, rate, up=up), _src(*SOURCES))
        assert _refused(c) == NOT_REACHED.get(ratio, {}).get(rate, set()), (ratio, rate)
    # an output under the source: judged only by what was measured, on its
    # engine's major (below) — here no engine is known
    assert _refused(rc.check(_inp(ratio, 88200, up=up), _src(*SOURCES))) == \
        ({32000, 48000} if ratio in ("2^x", "Int") else set())


def test_the_1x_filter_judges_every_source_under_50_khz():
    nx, x1 = Filter("demo-poly-lp", "Any"), Filter("demo-gauss-xa", "2^x", True)
    c = rc.check(Inputs("pcm", 768000, "not checked", nx, x1), _src(32000, 44100, 88200))
    assert [(g.name, g.ratio, g.sources) for g in c.groups] == [("demo-gauss-xa", "2^x up", (32000, 44100))]
    assert not rc.check(Inputs("pcm", 768000, "not checked", nx, None), _src(32000, 44100, 88200)).groups
    c = rc.check(Inputs("pcm", 768000, "not checked", x1, nx), _src(32000, 44100, 88200))
    assert [(g.name, g.sources) for g in c.groups] == [("demo-gauss-xa", (88200,))]


def test_nothing_is_said_at_source_auto_or_with_the_adaptive_output_rate_grayed_or_checked(tmp_path):
    src = _src(44100, 48000, 96000)
    assert rc.check(_inp(), src).groups
    for inp in (_inp(mode=None), _inp(rate=0), _inp(adaptive="grayed"), _inp(adaptive="checked"),
                _inp(ratio=None)):
        c = rc.check(inp, src)
        assert not c.groups and rc.lines(c) == {} and rc.notice(c) is None
    # SDM, where both halves would speak: HQPlayer picking the rate silences both
    assert {g.cause for g in rc.check(_sdm(tmp_path), src).groups} == {"modulator", "filter"}
    for state in ("grayed", "checked"):
        assert not rc.check(_sdm(tmp_path, adaptive=state), src).groups
    assert not rc.check(_sdm(tmp_path, mode=0), src).groups           # [source]
    assert not rc.check(_sdm(tmp_path, rate=0), src).groups           # Auto


def test_an_adaptive_output_rate_not_known_hedges_both_halves(tmp_path):
    src = _src(44100, 48000, 96000)
    firm, hedged = rc.check(_sdm(tmp_path), src), rc.check(_sdm(tmp_path, adaptive=None), src)
    assert firm.groups == hedged.groups and not firm.hedged and hedged.hedged
    assert not any("unless" in s for s in _said(firm))
    lines = {k: [plain(p) for p in v] for k, v in rc.lines(hedged).items()}
    assert lines["modulator"][0].endswith("unless its adaptive output rate picks another rate.")
    assert lines["filter"][0].endswith("unless its adaptive output rate is on.")
    assert lines["filter"][1] == ("Pick a filter HQPlayer describes as Any, or turn on adaptive output rate in "
                                  "HQPlayer's own settings (Output) if it is off.")


# -- The modulator first -------------------------------------------------------------

def test_the_modulator_first_for_the_family_its_refusal_was_measured_from(tmp_path):
    inp = _sdm(tmp_path)
    assert (inp.mode, inp.rate, inp.modulator, inp.engine) == ("sdm", 5644800, "DMX7 512+fs", "9.4.1")
    assert inp.nx == inp.x1 == Filter("demo-sinc-a", "2^x", False)
    c = rc.check(inp, Sources({44100: 3, 88200: 1, 48000: 2, 96000: 1}, first=44100))
    assert [(g.cause, g.name, g.sources) for g in c.groups] == [
        ("modulator", "DMX7 512+fs", (44100, 88200)), ("filter", "demo-sinc-a", (48000, 96000))]
    assert c.started_refused
    lines = {k: [plain(p) for p in v] for k, v in rc.lines(c).items()}
    assert lines == {
        "modulator": ["HQPlayer refused DMX7 512+fs at DSD128 on engine 9.4.1, so it will stop on the 44.1 kHz "
                      "and 88.2 kHz tracks in the queue.",
                      "Pick another modulator, or another output rate."],
        "filter": ["HQPlayer describes demo-sinc-a as 2^x: DSD128 is no power-of-two multiple of 48 kHz or "
                   "96 kHz, so HQPlayer will stop on the 48 kHz and 96 kHz tracks in the queue.",
                   "Pick a filter HQPlayer describes as Any, or turn on adaptive output rate in HQPlayer's own "
                   "settings (Output)."]}
    # at the rate refused from 48k, the 48k family is the modulator's and 44.1k the filter's
    c = rc.check(_sdm(tmp_path, rate=5), _src(44100, 48000, 96000))
    assert [(g.cause, g.sources) for g in c.groups] == [("modulator", (48000, 96000)), ("filter", (44100,))]
    assert plain(rc.lines(c)["modulator"][0]).startswith(
        "HQPlayer refused DMX7 512+fs at DSD256 (48k) on engine 9.4.1, so it will stop on the 48 kHz and 96 kHz")
    assert plain(rc.lines(c)["filter"][0]).startswith(
        "HQPlayer describes demo-sinc-a as 2^x: DSD256 (48k) is no power-of-two multiple of 44.1 kHz")


def test_the_modulator_half_reads_only_its_own_major_engine_and_what_it_names(tmp_path):
    src = _src(44100, 48000)
    other = _sdm(tmp_path, engine="8.0.0")
    assert other.refused == {} and other.engine is None
    assert [(g.cause, g.sources) for g in rc.check(other, src).groups] == [("filter", (48000,))]
    later = _sdm(tmp_path, engine="9.5.0")                # the newest the atlas holds of its major
    assert later.engine == "9.4.1" and later.refused == {5644800: 44100, 12288000: 48000}
    assert not _sdm(tmp_path, shaper=16).refused           # a name the atlas does not hold
    pcm = rc.inputs({"mode": 1, "rate": 4, "filterNx": 10, "filter1x": -1, "shaper": 14}, MODES, PCM_RATES,
                    _rows(PCM, "filter"), _rows(MODULATORS, "modulator"), adaptive="not checked",
                    atlas=_read(tmp_path), engine="9.4.1")
    assert (pcm.mode, pcm.rate, pcm.modulator, pcm.refused, pcm.x1) == ("pcm", 768000, None, {}, None)
    assert pcm.nx == Filter("demo-sinc-a", "2^x", True)


def test_the_modulator_half_reads_the_engine_that_holds_the_modulators(tmp_path):
    # A newer engine measured in PCM alone: a 9.6 HQPlayer's modulator is
    # judged on 9.4.1's refusals, the newest engine of its major that holds
    # an SDM release — the screen shows it the same engine's modulators
    doc = _doc()
    _another_engine(doc, "9.6.0", ("pcm",))
    inp = _sdm(tmp_path, engine="9.6.2", doc=doc)
    assert inp.engine == "9.4.1" and inp.refused == {5644800: 44100, 12288000: 48000}
    # Published: 9.6.0 measured in SDM too, undated — the dated 9.4.1 judges,
    # as the shipped file, which holds the dated releases alone, would
    _another_engine(doc, "9.6.0")
    doc["releases"].update({"9.4.1/pcm": "2001-05-06", "9.4.1/sdm": "2001-05-06"})
    assert _sdm(tmp_path, engine="9.6.2", doc=doc).engine == "9.6.0"
    published = _sdm(tmp_path, engine="9.6.2", doc=doc, published=True)
    assert published.engine == "9.4.1" and published.refused == {5644800: 44100, 12288000: 48000}


def test_published_takes_a_modulators_refusals_only_from_a_release_with_a_date(tmp_path):
    # every release of the synthetic file is null: a file put at the shipped
    # path before its release, which gives nothing — as no file at all
    early = _sdm(tmp_path, published=True)
    assert early == _sdm(tmp_path)._replace(refused={}, engine=None)
    assert [(g.cause, g.sources) for g in rc.check(early, _src(44100, 48000, 96000)).groups] == [
        ("filter", (48000, 96000))]
    doc = _doc()
    doc["releases"]["9.4.1/sdm"] = "2001-05-06"
    dated = _sdm(tmp_path, doc=doc, published=True)
    assert dated == _sdm(tmp_path, doc=doc) and dated.refused == {5644800: 44100, 12288000: 48000}
    # a PCM release's date is none of a modulator's
    doc["releases"] = {"9.4.1/pcm": "2001-05-06", "9.4.1/sdm": None}
    assert _sdm(tmp_path, doc=doc, published=True) == early


def test_hqplayer_5_describes_no_class_and_knows_no_atlas_engine(tmp_path):
    inp = _sdm(tmp_path, engine="5.17.0", described=False)
    assert inp.nx == Filter("demo-sinc-a", None) and inp.refused == {}
    c = rc.check(inp, _src(44100, 48000, 96000))
    assert not c.groups and rc.lines(c) == {} and rc.verdict(c, 48000, None) is None


# -- What was measured on engine 6.2.4 ------------------------------------------------

def _measured(name, out, *, engine="6.2.4", adaptive="not checked"):
    """HQPlayer 6.2.4's PCM lists, `name` in both filter slots at the fixed
    output rate `out`."""
    lists = json.loads(LISTS_624.read_text(encoding="utf-8"))["pcm"]
    filters = [{**f, "parsed": parse_filter_description(f["description"])} for f in lists["filters"]]
    index = next(f["index"] for f in filters if f["name"] == name)
    rate = next(r["index"] for r in lists["rates"] if r["rate"] == out)
    return rc.inputs({"mode": 1, "rate": rate, "filterNx": index, "filter1x": index, "shaper": 7}, MODES,
                     lists["rates"], filters, lists["shapers"], adaptive=adaptive, engine=engine)


def _cell(name, cell, **kw):
    """The check at an UP_CELLS cell: a PCM source as the queue holds it; a
    DSD64 one as a verdict sees it, at the rate HQPlayer's log named."""
    src, out = UP_CELLS[cell]
    inp = _measured(name, out, **kw)
    if src == DSD64:
        return rc.check(inp, Sources({STAGE: 1}, first=STAGE, of_dsd=DSD64))
    return rc.check(inp, Sources({src: 1}, first=src))


@pytest.mark.parametrize("name", UP_MATRIX)
def test_a_stop_is_said_exactly_where_engine_6_2_4_stopped(name):
    for cell, (src, out) in UP_CELLS.items():
        did, c = UP_MATRIX[name][cell], _cell(name, cell)
        if name == "none":
            # 1:1 is never judged: with a DSD track HQPlayer's line speaks alone
            assert not c.groups, cell
        else:
            assert bool(c.groups) == (did in STOPS), (cell, did, c.groups)
            # a stop measured is said as one: "will", never "may"
            assert all(g.why != "may" for g in c.groups), (cell, c.groups)
        # sinc-M under the source's rate: its words, no stop
        assert bool(c.notes) == (name == "sinc-M" and out < (STAGE if src == DSD64 else src)), cell
        # before Play a DSD track is not checked: the rate it reaches the filter at is HQPlayer's to name
        if src == DSD64:
            assert not rc.check(_measured(name, out), Sources({}, dsd=1)).groups


def test_what_was_measured_speaks_on_its_engines_major_alone():
    assert [rc.measured_on(e) for e in ("6.2.4", "6.0.0", "6.3.12", "7.0.1", "5.17.0", None, "")] == \
        ["6.2.4", "6.2.4", "6.2.4", None, None, None, None]
    for engine in ("7.0.1", None):
        for name, cell in (("closed-form", "88.2k->44.1k"), ("FFT", "44.1k->44.1k"), ("FIR", "352.8k->352.8k"),
                           ("sinc-medium", "352.8k->176.4k")):
            assert not _cell(name, cell, engine=engine).groups, (engine, name)
        assert not _cell("sinc-M", "88.2k->44.1k", engine=engine).notes
        # the class's arithmetic needs no measurement: 2^x up across families still speaks
        c = rc.check(_measured("closed-form", 705600, engine=engine), Sources({48000: 1}))
        assert [(g.why, g.sources) for g in c.groups] == [("class", (48000,))]
    # the measured engine is named, whichever of its major HQPlayer runs
    c = _cell("closed-form", "88.2k->44.1k", engine="6.0.0")
    assert c.measured == "6.2.4"
    assert " On engine 6.2.4 HQPlayer stopped with closed-form " in plain(rc.lines(c)["filter"][0])


def test_an_up_filter_not_tried_may_stop_and_the_others_without_up_are_not_judged_at_1_to_1():
    # "will stop" only for the three HQPlayer stopped; "up" filters not tried — sinc-short,
    # poly-sinc-gauss-short, closed-form-fast — may stop under the source's rate, at any downward ratio
    assert rc.UP_REFUSED == {name for name, row in UP_MATRIX.items()
                             if any(row[cell] in STOPS for cell in ("88.2k->44.1k", "352.8k->176.4k"))}
    for name in ("sinc-short", "poly-sinc-gauss-short", "closed-form-fast"):
        assert [g.why for g in _cell(name, "352.8k->176.4k").groups] == ["may"], name
        c = rc.check(_measured(name, 44100), Sources({96000: 1, 352800: 1}))
        assert [(g.why, g.sources) for g in c.groups] == [("may", (96000, 352800))], name
    # sinc-Mx, sinc-M's twin, never tried there: no stop, the class's words and sinc-M's conversion — where
    # sinc-M gets its own, under a source of its family
    c = _cell("sinc-Mx", "352.8k->176.4k")
    assert not c.groups and [(g.why, g.sources) for g in c.notes] == [("twin", (352800,))]
    assert [plain(p) for p in rc.lines(c)["filter"]] == [
        "HQPlayer describes sinc-Mx as 2^x up, and the 352.8 kHz tracks in the queue are above 176.4 kHz. Filters "
        "described with “up” are made for upsampling, not for decimation; on engine 6.2.4 sinc-Mx was not tried "
        "under the source's rate, and sinc-M, its twin, converted 352.8 kHz to 176.4 kHz and did not stop."]
    assert rc.notice(c) is None and rc.verdict(c, 352800, None) is None and not c.started_refused
    c = rc.check(_measured("sinc-Mx", 44100), Sources({96000: 1}))
    assert not c.groups and not c.notes
    # another major: nothing of either twin
    assert not _cell("sinc-Mx", "352.8k->176.4k", engine="7.0.1").notes
    # the 2^x and Int filters without "up" other than FFT and FIR: 1:1 not tried, nothing said
    for name in ("IIR", "IIR2", "asymFIR", "minphaseFIR", "poly-sinc-ext"):
        assert not _cell(name, "88.2k->88.2k").groups, name


def test_the_words_of_an_up_filter_not_tried():
    c = _cell("sinc-short", "88.2k->44.1k")
    assert [plain(p) for p in rc.lines(c)["filter"]] == [
        "HQPlayer describes sinc-short as Any up. Of the “up” filters tried on engine 6.2.4, HQPlayer stopped with "
        "all but sinc-M under the source's rate; sinc-short was not tried there, so HQPlayer may stop on the "
        "88.2 kHz tracks in the queue at 44.1 kHz.",
        "Filters described with “up” are made for upsampling, not for decimation: pick one HQPlayer describes "
        "without “up”, or as the output rate the tracks' own or a power-of-two multiple of it."]
    # a queue whose only finding is such a filter may not play; hedged as any other claim
    hedged = rc.check(_measured("sinc-short", 44100, adaptive=None), Sources({88200: 2, 44100: 1}))
    notice = rc.notice(hedged)
    assert plain(notice["title"]) == "Some tracks in the queue may not play at 44.1 kHz"
    assert plain(notice["text"][0]).endswith(
        "so HQPlayer may stop on the 88.2 kHz tracks in the queue at 44.1 kHz unless its adaptive output rate is on.")
    # after the stop it is said as a stop: the stop is the evidence
    assert rc.verdict(c, 88200, None) == {
        "title": "HQPlayer refused the rate combination",
        "sentence": "HQPlayer describes sinc-short as Any up: 44.1 kHz is under 88.2 kHz, and HQPlayer stopped on "
                    "this track.",
        "next": "Filters described with “up” are made for upsampling, not for decimation: pick one HQPlayer "
                "describes without “up”, or as the output rate 88.2 kHz or a power-of-two multiple of it."}


def test_a_queue_with_a_stop_measured_and_one_not_tried_cannot_play_and_gives_its_fix_once():
    # closed-form in the Nx slot (88.2 kHz tracks), sinc-short in the 1x slot (48 kHz ones), at 44.1 kHz
    inp = rc.Inputs("pcm", 44100, "not checked", Filter("closed-form", "2^x", True), Filter("sinc-short", "Any", True),
                    measured="6.2.4")
    c = rc.check(inp, Sources({48000: 1, 88200: 1}))
    assert [(g.name, g.why) for g in c.groups] == [("sinc-short", "may"), ("closed-form", "down")]
    said = [plain(p) for p in rc.lines(c)["filter"]]
    assert len(said) == 3 and said[0].endswith("so HQPlayer may stop on the 48 kHz tracks in the queue at 44.1 kHz.")
    assert said[1].endswith("so it will stop on the 88.2 kHz tracks in the queue at 44.1 kHz.")
    assert said[2].startswith("Filters described with “up” are made for upsampling, not for decimation")
    assert plain(rc.notice(c)["title"]) == "Some tracks in the queue cannot play at 44.1 kHz"


def test_a_may_hqplayer_disproved_is_said_no_more_and_the_rest_still_is():
    # sinc-short in the Nx slot at 44.1 kHz, closed-form in the 1x one: the 48 kHz tracks will stop, the 88.2 and
    # 96 kHz ones may
    inp = rc.Inputs("pcm", 44100, "not checked", Filter("sinc-short", "Any", True), Filter("closed-form", "2^x", True),
                    measured="6.2.4")
    c = rc.check(inp, Sources({88200: 1, 96000: 1, 48000: 1}, first=88200))
    assert not c.started_refused and c.started_may and c.first == 88200
    # an 88.2 kHz track heard playing at this setting (the backend's word): no verdict comes, and its "may" is said
    # no more, in the notice as on the screen; the 48 kHz tracks still will stop, the 96 kHz ones still may
    heard = frozenset({88200})
    notice = rc.notice(c, heard)
    assert plain(notice["title"]) == "Some tracks in the queue cannot play at 44.1 kHz"
    said = [plain(t) for t in notice["text"]]
    assert len(said) == 2 and said[0].endswith("so it will stop on the 48 kHz tracks in the queue at 44.1 kHz.")
    assert said[1].endswith("so HQPlayer may stop on the 96 kHz tracks in the queue at 44.1 kHz."), said
    assert plain(rc.lines(c, heard)["filter"][1]).endswith("may stop on the 96 kHz tracks in the queue at 44.1 kHz.")
    # nothing heard yet: the whole check
    assert "88.2 kHz and 96 kHz tracks" in plain(rc.lines(c)["filter"][1])
    assert "88.2 kHz and 96 kHz tracks" in plain(rc.notice(c)["text"][1])
    # a queue whose only other finding is a "may": that title; every "may" disproved, no notice and no line
    c = rc.check(inp, Sources({88200: 1, 96000: 1}, first=88200))
    assert plain(rc.notice(c, heard)["title"]) == "Some tracks in the queue may not play at 44.1 kHz"
    assert rc.notice(c, frozenset({88200, 96000})) is None and rc.lines(c, frozenset({88200, 96000})) == {}
    # a track a finding says will stop, playing at its start: its verdict speaks, not the notice
    assert rc.check(inp, Sources({48000: 1, 88200: 1}, first=48000)).started_refused


def test_the_words_of_each_measured_stop_and_of_sinc_m():
    said = {k: [plain(p) for p in v] for k, v in rc.lines(_cell("closed-form", "88.2k->44.1k")).items()}
    assert said == {"filter": [
        "HQPlayer describes closed-form as 2^x up. On engine 6.2.4 HQPlayer stopped with closed-form under the "
        "source's rate, so it will stop on the 88.2 kHz tracks in the queue at 44.1 kHz.",
        "Filters described with “up” are made for upsampling, not for decimation: pick one HQPlayer describes "
        "without “up”, or as the output rate the tracks' own or a power-of-two multiple of it."]}
    said = {k: [plain(p) for p in v] for k, v in rc.lines(_cell("FFT", "44.1k->44.1k")).items()}
    assert said == {"filter": [
        "HQPlayer refused FFT at the source's own rate (1:1) on engine 6.2.4, so it will stop on the 44.1 kHz "
        "tracks in the queue.",
        "Pick another filter, or another output rate."]}
    # sinc-M: the screen's words alone — no notice, no verdict, nothing the queue cannot play
    c = _cell("sinc-M", "352.8k->176.4k")
    assert [plain(p) for p in rc.lines(c)["filter"]] == [
        "HQPlayer describes sinc-M as 2^x up, and the 352.8 kHz tracks in the queue are above 176.4 kHz. Filters "
        "described with “up” are made for upsampling, not for decimation; on engine 6.2.4 HQPlayer converted "
        "352.8 kHz to 176.4 kHz with sinc-M and did not stop."]
    assert rc.notice(c) is None and rc.verdict(c, 352800, None) is None and not c.started_refused
    # downward across the families, as from 96 to 44.1 kHz, nothing was measured: nothing said
    c = rc.check(_measured("sinc-M", 44100), Sources({96000: 1, 88200: 1}))
    assert not c.groups and [g.sources for g in c.notes] == [(88200,)]
    # the notice names the queue's tracks that cannot play; hedged when the adaptive output rate is not known
    hedged = rc.check(_measured("poly-sinc-ext2-short", 44100, adaptive=None), Sources({88200: 2, 44100: 1}))
    assert plain(rc.notice(hedged)["title"]) == "Some tracks in the queue cannot play at 44.1 kHz"
    assert plain(rc.notice(hedged)["text"][0]).endswith(
        "so it will stop on the 88.2 kHz tracks in the queue at 44.1 kHz unless its adaptive output rate is on.")


def test_a_verdict_names_the_measured_stop_and_a_dsd_tracks_rate_as_hqplayer_named_it():
    line = "Requested filter not possible with this rate combination 88200/44100, stop"
    v = rc.verdict(_cell("closed-form", "88.2k->44.1k"), 88200, line)
    assert v == {"title": "HQPlayer refused the rate combination",
                 "sentence": "HQPlayer describes closed-form as 2^x up: 44.1 kHz is under 88.2 kHz, and HQPlayer "
                             f"stopped on this track. HQPlayer's log: “{line}”.",
                 "next": "Filters described with “up” are made for upsampling, not for decimation: pick one "
                         "HQPlayer describes without “up”, or as the output rate 88.2 kHz or a power-of-two "
                         "multiple of it."}
    v = rc.verdict(_cell("FIR", "88.2k->88.2k"), 88200, None)
    assert (v["sentence"], v["next"]) == (
        "HQPlayer refused FIR at the source's own rate (1:1) on engine 6.2.4, and it stopped on this 88.2 kHz "
        "track.",
        "Pick another filter, or another output rate.")
    # a DSD64 track: by the rate HQPlayer's log named, never its own
    v = rc.verdict(_cell("sinc-medium", "DSD64->88.2k"), STAGE, "clHQPlayerEngine::Execute(): lInRate > lOutRate",
                   dsd=DSD64)
    assert v["sentence"] == (
        "HQPlayer describes sinc-medium as Any up: 88.2 kHz is under the 176.4 kHz HQPlayer converted this DSD64 "
        "track to, and HQPlayer stopped on this track. HQPlayer's log: “clHQPlayerEngine::Execute(): lInRate > "
        "lOutRate”.")
    # the class's arithmetic from that rate too; the 1:1 measured from PCM sources alone
    stage = rc.check(_measured("closed-form", 768000), Sources({STAGE: 1}, first=STAGE, of_dsd=DSD64))
    assert rc.verdict(stage, STAGE, None, dsd=DSD64)["sentence"] == (
        "HQPlayer describes closed-form as 2^x up: 768 kHz is no power-of-two multiple of the 176.4 kHz HQPlayer "
        "converted this DSD64 track to, and HQPlayer stopped on this track.")
    assert not rc.check(_measured("FFT", 176400), Sources({STAGE: 1}, first=STAGE, of_dsd=DSD64)).groups
    # a DSD file whose tags give a PCM-looking rate (WavPack's 352.8 kHz for DSD64): named DSD alone
    assert rc.verdict(stage, STAGE, None, dsd=352800)["sentence"].endswith(
        "the 176.4 kHz HQPlayer converted this DSD track to, and HQPlayer stopped on this track.")


def test_a_queue_with_several_kinds_of_stop_gives_each_its_fix_once():
    # closed-form at 88.2 kHz: 48 kHz no power-of-two step below it, 176.4 kHz and 192 kHz above it
    c = rc.check(_measured("closed-form", 88200), Sources({44100: 1, 48000: 2, 176400: 1, 192000: 1}))
    assert [(g.why, g.sources) for g in c.groups] == [("class", (48000,)), ("down", (176400, 192000))]
    assert c.fix_rate is None
    said = [plain(p) for p in rc.lines(c)["filter"]]
    assert said[2:] == ["Pick a filter HQPlayer describes as Any, or turn on adaptive output rate in HQPlayer's "
                        "own settings (Output).",
                        "Filters described with “up” are made for upsampling, not for decimation: pick one HQPlayer "
                        "describes without “up”, or as the output rate the tracks' own or a power-of-two multiple of "
                        "it."]
    # FFT at 44.1 kHz: the 44.1 kHz tracks at 1:1, and nothing for those above it
    c = rc.check(_measured("FFT", 44100), Sources({44100: 3, 88200: 1, 96000: 1}, first=88200))
    assert [(g.why, g.sources) for g in c.groups] == [("one", (44100,))] and not c.started_refused


def test_the_rate_to_pick_is_never_one_a_measured_refusal_stops_at():
    # FIR at 48 kHz, a queue of 44.1 kHz tracks: the class's fix would name 44.1 kHz — FIR's 1:1
    rates = tuple(r for r in (44100, 48000, 88200, 96000))
    c = rc.check(_measured("FIR", 48000)._replace(rates=rates), Sources({44100: 2}, first=44100))
    assert [(g.why, g.sources) for g in c.groups] == [("class", (44100,))] and c.fix_rate is None
    # FFT at 96 kHz, the 88.2 kHz tracks alone: not 88.2 kHz either
    c = rc.check(_measured("FFT", 96000)._replace(rates=rates), Sources({88200: 1}, first=88200))
    assert c.fix_rate is None
    # where nothing measured stops there, the rate is still named: another engine's major
    c = rc.check(_measured("FIR", 48000, engine="7.0.1")._replace(rates=rates), Sources({44100: 2}, first=44100))
    assert c.fix_rate == 44100


# -- The queue ---------------------------------------------------------------------

def test_the_rates_are_named_ascending_and_the_tracks_not_checked_are_said_last():
    c = rc.check(_inp(), _src(176400, 44100, 88200, 48000, dsd=2, streamed=1))
    said = [plain(p) for p in rc.lines(c)["filter"]]
    assert said == [
        "HQPlayer describes demo-sinc-a as 2^x: 768 kHz is no power-of-two multiple of 44.1 kHz, 88.2 kHz "
        "or 176.4 kHz, so HQPlayer will stop on the 44.1 kHz, 88.2 kHz and 176.4 kHz tracks in the queue.",
        "Pick a filter HQPlayer describes as Any, or turn on adaptive output rate in HQPlayer's own settings "
        "(Output).",
        "The DSD and streamed tracks in the queue are not checked."]
    assert plain(rc.lines(rc.check(_inp(), _src(44100, dsd=1)))["filter"][-1]) == \
        "The DSD tracks in the queue are not checked."
    assert plain(rc.lines(rc.check(_inp(), _src(44100, streamed=1)))["filter"][-1]) == \
        "The streamed tracks in the queue are not checked: their rate is known only once they play."
    # nothing refused, or nothing but DSD and streams: nothing at all
    assert rc.lines(rc.check(_inp(), _src(48000, dsd=1, streamed=1))) == {}
    assert rc.lines(rc.check(_inp(), _src(dsd=3, streamed=2))) == {}


def test_the_slot_hqplayer_reads_tells_a_queue_that_starts_refused():
    assert rc.check(_inp(), _src(44100, 48000, first=44100)).started_refused
    assert not rc.check(_inp(), _src(44100, 48000, first=48000)).started_refused
    assert not rc.check(_inp(), _src(44100, first=None)).started_refused


def test_the_filters_fix_names_a_rate_only_where_every_track_reaches_it():
    rates = (44100, 88200, 705600, 768000)
    c = rc.check(_inp(rates=rates), _src(44100, 88200))
    assert c.fix_rate == 705600
    assert plain(rc.lines(c)["filter"][-1]).endswith("in HQPlayer's own settings (Output), or pick 705.6 kHz.")
    assert rc.check(_inp(rate=705600, rates=rates), _src(48000, 96000)).fix_rate == 768000
    assert rc.check(_inp(rates=rates), _src(44100, 48000)).fix_rate is None     # a track that plays
    assert rc.check(_inp(rates=(768000,)), _src(44100)).fix_rate is None        # not in HQPlayer's list
    assert rc.check(_inp(rates=rates), _src(32000, 44100)).fix_rate is None     # two families
    # at DSD256 (48k) the 44.1k family's rate is DSD256, above every refused track
    assert rc.check(_inp(rate=12288000, rates=(11289600, 12288000)), _src(44100, 176400)).fix_rate == 11289600


def test_the_setting_key_is_the_mode_rate_slots_modulator_and_adaptive_state():
    assert rc.check(_inp(), _src(44100)).key == ("pcm", 768000, "demo-sinc-a", "demo-sinc-a", None,
                                                 "not checked")
    assert rc.check(_inp(adaptive="grayed"), _src(44100)).key[-1] == "grayed"
    # an unset 1x slot leaves the Nx filter running every source: the setting of that filter in both
    assert rc.check(_inp(adaptive=None, x1=False), _src()).key == \
        ("pcm", 768000, "demo-sinc-a", "demo-sinc-a", None, None)
    assert rc.check(_inp(adaptive=None, x1=False), _src()).key == rc.check(_inp(adaptive=None), _src()).key


# -- After the fact ----------------------------------------------------------------

def test_the_verdict_names_the_cause_and_quotes_hqplayers_line(tmp_path):
    line = "Requested filter not possible with this rate combination 48000/705600, stop"
    c = rc.check(_inp(rate=705600, rates=(705600, 768000)), _src(48000))
    assert rc.verdict(c, 48000, line) == {
        "title": "HQPlayer refused the rate combination",
        "sentence": "HQPlayer describes demo-sinc-a as 2^x: 705.6 kHz is no power-of-two multiple of 48 kHz, "
                    f"and HQPlayer stopped on this track. HQPlayer's log: “{line}”.",
        "next": "Pick a filter HQPlayer describes as Any, or turn on adaptive output rate in HQPlayer's own "
                "settings (Output), or pick 768 kHz."}
    assert rc.verdict(rc.check(_inp(adaptive=None), _src(44100)), 44100, None)["sentence"].endswith(
        "and HQPlayer stopped on this track.")                  # the stop is the evidence: no hedge
    m = rc.verdict(rc.check(_sdm(tmp_path), _src(88200)), 88200, None)
    assert m["sentence"] == ("HQPlayer refused DMX7 512+fs at DSD128 on engine 9.4.1, and it stopped on this "
                             "88.2 kHz track.")
    assert m["next"] == "Pick another modulator, or another output rate."
    # the line alone: the rule names nothing (here a class that reaches)
    alone = rc.check(_inp("Any"), _src(44100))
    assert rc.verdict(alone, 44100, line) == {"title": "HQPlayer refused the rate combination",
                                              "sentence": f"HQPlayer stopped on it: “{line}”.",
                                              "next": "Pick another filter or output rate."}
    assert rc.verdict(rc.check(_inp("Any", mode="sdm"), _src(44100)), 44100, line)["next"] == \
        "Pick another filter, modulator or output rate."
    assert rc.verdict(alone, 44100, None) is None


# -- The words ---------------------------------------------------------------------

def _states(tmp_path):
    """A check of every state the words cover."""
    src = Sources({44100: 2, 88200: 1, 48000: 1, 96000: 1}, dsd=1, streamed=1, first=44100)
    for adaptive in ("not checked", None):
        yield rc.check(_sdm(tmp_path, adaptive=adaptive), src)
        yield rc.check(_sdm(tmp_path, rate=5, adaptive=adaptive), src)
        for ratio in ("2^x", "Int"):
            yield rc.check(_inp(ratio, adaptive=adaptive, rates=(705600, 768000)), _src(44100, 88200))
            yield rc.check(_inp(ratio, adaptive=adaptive), src)
        # what was measured on engine 6.2.4: a stop under the source's rate, at 1:1, sinc-M's words; an "up"
        # filter not tried, and sinc-Mx's words
        for name, out in (("closed-form", 88200), ("FFT", 44100), ("sinc-M", 44100), ("sinc-short", 44100),
                          ("sinc-Mx", 44100)):
            yield rc.check(_measured(name, out, adaptive=adaptive), Sources({44100: 1, 48000: 1, 176400: 1}))


def test_the_modulator_is_said_refused_at_exactly_the_rate_the_file_holds(tmp_path):
    seen = set()
    for c in _states(tmp_path):
        if not any(g.cause == "modulator" for g in c.groups):
            continue
        said = [plain(p) for p in rc.lines(c)["modulator"]]
        said += [plain(p) for p in rc.notice(c)["text"] if plain(p).startswith("HQPlayer refused")]
        said.append(rc.verdict(c, 48000 if c.rate == 12288000 else 44100, None)["sentence"])
        seen |= {name for name in ("DSD128", "DSD256 (48k)") if f" at {name} on engine " in said[0]}
        # the rate as the HQPlayer screen names it, the same on the line, the notice and the verdict
        assert len({s.split(" on engine ")[0] for s in said if s.startswith("HQPlayer refused")}) == 1, said
        for word in ("only", "below", "×", "1024", "512×", "· from"):
            assert not any(word in s for s in said), (word, said)
    assert seen == {"DSD128", "DSD256 (48k)"}


def test_no_word_ranks_none_speaks_of_an_engine_stopping_and_none_links(tmp_path):
    said = [s for name, value in vars(rc).items() if name.isupper()
            for s in (value.values() if isinstance(value, dict) else [value]) if isinstance(s, str)]
    for c in _states(tmp_path):
        said += _said(c)
        v = rc.verdict(c, 44100, "Requested filter not possible with this rate combination 48000/705600, stop")
        said += [v["title"], v["sentence"], v["next"]]
    assert sorted({m.group(0) for text in said for m in BANNED.finditer(text)}) == []
    assert not any("sautium.net" in s for s in said)
