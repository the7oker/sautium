"""The rate check (backend/hqp_rate_check.py): whether HQPlayer reaches its
output rate from the tracks still to play, and its words — on HQPlayer-shaped
lists with invented names and the synthetic atlas
(fixtures/hqp_atlas/app-synthetic.json: invented modulators refused at
invented rates on the invented engine 9.4.1). Pure: no database, no
HQPlayer."""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

import hqp_rate_check as rc  # noqa: E402
from hqp_atlas import plain  # noqa: E402
from hqp_rate_check import Filter, Inputs, Sources  # noqa: E402

from test_hqp_atlas import BANNED, MODULATORS, PCM, SDM, _read, _rows  # noqa: E402

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


def _inp(ratio="2^x", rate=768000, *, up=False, adaptive=False, mode="pcm", x1=True, **kw):
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


def _sdm(tmp_path, *, rate=2, engine="9.4.1", shaper=14, mode=2, adaptive=False, described=True):
    """HQPlayer in SDM mode with the synthetic atlas: demo-sinc-a ("⥣ 2^x")
    in both slots, the modulator by its index in MODULATORS (14: DMX7 512+fs,
    which the file refuses at DSD128 from 44.1k and at DSD256 (48k) from 48k)."""
    filters = _rows(SDM, "filter")
    if not described:
        filters = [{**f, "description": "", "parsed": None} for f in filters]
    state = {"mode": mode, "rate": rate, "filterNx": 10, "filter1x": 10, "shaper": shaper}
    return rc.inputs(state, MODES, SDM_RATES, filters, _rows(MODULATORS, "modulator"),
                     adaptive=adaptive, atlas=_read(tmp_path), engine=engine)


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
    # an output under the source is HQPlayer's own stop, not the class's
    assert _refused(rc.check(_inp(ratio, 88200, up=up), _src(*SOURCES))) == \
        ({32000, 48000} if ratio in ("2^x", "Int") else set())


def test_the_1x_filter_judges_every_source_under_50_khz():
    nx, x1 = Filter("demo-poly-lp", "Any"), Filter("demo-gauss-xa", "2^x", True)
    c = rc.check(Inputs("pcm", 768000, False, nx, x1), _src(32000, 44100, 88200))
    assert [(g.name, g.ratio, g.sources) for g in c.groups] == [("demo-gauss-xa", "2^x up", (32000, 44100))]
    assert not rc.check(Inputs("pcm", 768000, False, nx, None), _src(32000, 44100, 88200)).groups
    c = rc.check(Inputs("pcm", 768000, False, x1, nx), _src(32000, 44100, 88200))
    assert [(g.name, g.sources) for g in c.groups] == [("demo-gauss-xa", (88200,))]


def test_nothing_is_said_at_source_auto_or_with_the_adaptive_output_rate_on(tmp_path):
    src = _src(44100, 48000, 96000)
    assert rc.check(_inp(), src).groups
    for inp in (_inp(mode=None), _inp(rate=0), _inp(adaptive=True), _inp(ratio=None)):
        c = rc.check(inp, src)
        assert not c.groups and rc.lines(c) == {} and rc.notice(c) is None
    # SDM, where both halves would speak: the adaptive output rate on silences both
    assert {g.cause for g in rc.check(_sdm(tmp_path), src).groups} == {"modulator", "filter"}
    assert not rc.check(_sdm(tmp_path, adaptive=True), src).groups
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
        "modulator": ["HQPlayer refused DMX7 512+fs at DSD128 · from 44.1k on engine 9.4.1, so it will stop on "
                      "the 44.1 kHz and 88.2 kHz tracks in the queue.",
                      "Pick another modulator, or another output rate."],
        "filter": ["HQPlayer describes demo-sinc-a as 2^x: DSD128 is no power-of-two multiple of 48 kHz or "
                   "96 kHz, so HQPlayer will stop on the 48 kHz and 96 kHz tracks in the queue.",
                   "Pick a filter HQPlayer describes as Any, or turn on adaptive output rate in HQPlayer's own "
                   "settings (Output)."]}
    # at the rate refused from 48k, the 48k family is the modulator's and 44.1k the filter's
    c = rc.check(_sdm(tmp_path, rate=5), _src(44100, 48000, 96000))
    assert [(g.cause, g.sources) for g in c.groups] == [("modulator", (48000, 96000)), ("filter", (44100,))]
    assert plain(rc.lines(c)["modulator"][0]).startswith(
        "HQPlayer refused DMX7 512+fs at DSD256 · from 48k on engine 9.4.1")
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
                    _rows(PCM, "filter"), _rows(MODULATORS, "modulator"), adaptive=False,
                    atlas=_read(tmp_path), engine="9.4.1")
    assert (pcm.mode, pcm.rate, pcm.modulator, pcm.refused, pcm.x1) == ("pcm", 768000, None, {}, None)
    assert pcm.nx == Filter("demo-sinc-a", "2^x", True)


def test_hqplayer_5_describes_no_class_and_knows_no_atlas_engine(tmp_path):
    inp = _sdm(tmp_path, engine="5.17.0", described=False)
    assert inp.nx == Filter("demo-sinc-a", None) and inp.refused == {}
    c = rc.check(inp, _src(44100, 48000, 96000))
    assert not c.groups and rc.lines(c) == {} and rc.verdict(c, 48000, None) is None


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
    assert rc.check(_inp(), _src(44100)).key == ("pcm", 768000, "demo-sinc-a", "demo-sinc-a", None, False)
    assert rc.check(_inp(adaptive=None, x1=False), _src()).key == \
        ("pcm", 768000, None, "demo-sinc-a", None, None)


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
    assert m["sentence"] == ("HQPlayer refused DMX7 512+fs at DSD128 · from 44.1k on engine 9.4.1, and it "
                             "stopped on this 88.2 kHz track.")
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
    for adaptive in (False, None):
        yield rc.check(_sdm(tmp_path, adaptive=adaptive), src)
        yield rc.check(_sdm(tmp_path, rate=5, adaptive=adaptive), src)
        for ratio in ("2^x", "Int"):
            yield rc.check(_inp(ratio, adaptive=adaptive, rates=(705600, 768000)), _src(44100, 88200))
            yield rc.check(_inp(ratio, adaptive=adaptive), src)


def test_the_modulator_is_said_refused_at_exactly_the_rate_the_file_holds(tmp_path):
    seen = set()
    for c in _states(tmp_path):
        if not any(g.cause == "modulator" for g in c.groups):
            continue
        said = [plain(p) for p in rc.lines(c)["modulator"]]
        said += [plain(p) for p in rc.notice(c)["text"] if plain(p).startswith("HQPlayer refused")]
        said.append(rc.verdict(c, 48000 if c.rate == 12288000 else 44100, None)["sentence"])
        seen |= {label for label in ("DSD128 · from 44.1k", "DSD256 · from 48k") if label in said[0]}
        for word in ("only", "below", "×", "1024", "512×"):
            assert not any(word in s for s in said), (word, said)
    assert seen == {"DSD128 · from 44.1k", "DSD256 · from 48k"}


def test_no_word_ranks_none_speaks_of_an_engine_stopping_and_none_links(tmp_path):
    said = [s for name, value in vars(rc).items() if name.isupper()
            for s in (value.values() if isinstance(value, dict) else [value]) if isinstance(s, str)]
    for c in _states(tmp_path):
        said += _said(c)
        v = rc.verdict(c, 44100, "Requested filter not possible with this rate combination 48000/705600, stop")
        said += [v["title"], v["sentence"], v["next"]]
    assert sorted({m.group(0) for text in said for m in BANNED.finditer(text)}) == []
    assert not any("sautium.net" in s for s in said)
