"""The assistant's six HQPlayer filter and modulator tools' words
(backend/hqp_assistant.py), on the synthetic atlas file
(fixtures/hqp_atlas/app-synthetic.json: every name, number and word in it
invented, none a measurement) and on synthetic answers of HQPlayer and rows
of this host. Pure: no database, no HQPlayer."""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

import hqp_assistant  # noqa: E402
import hqp_atlas  # noqa: E402
from hqp_assistant import Read  # noqa: E402
from hqp_rate_check import Sources  # noqa: E402
from hqplayer_client import parse_filter_description, parse_shaper_description  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "hqp_atlas" / "app-synthetic.json"
UNDATED = '"releases": {"9.4.1/pcm": null, "9.4.1/sdm": null}'
CANARY = "CANARY-c4-atlas-5d1e"
BANNED = re.compile(r"\b(best|better|worse|worst|superior|inferior|recommend|recommended|prefer|preferred|"
                    r"ideal|optimal|stall|restart|hang|crash|freeze|bug)\b", re.IGNORECASE)

PCM = [("demo-sinc-a", "4/5 transients, timbre, space ⥮ 2^x up"),
       ("demo-sinc-b", "4/5 transients, timbre, space ⥮ 2^x up"),
       ("demo-poly-lp", "3/5 space ⥮ Any"),
       ("demo-poly-mp", "3/5 space, transients ⥮ Any"),
       ("demo-gauss-x", "5/5 timbre ⥮ Any"),
       ("demo-gauss-xa", "5/5 timbre ⥮ 2^x up"),
       ("demo-new", "1/5 ⥮ 1:1")]
SDM = [("demo-sinc-a", "4/5 transients, timbre, space ⥣ 2^x"),
       ("demo-sinc-b", "4/5 transients, timbre, space ⥣ 2^x"),
       ("demo-gauss-x", "5/5 timbre ⥣ Any"),
       ("demo-2s", "2/5 ⥣ Any")]
DITHERS = [("none", ""), ("DEMO-TPDF", "")]
MODULATORS = [("DMX5", "Gen1"), ("DMX7EC-c", "Gen9"), ("DMX7EC-a", "Gen9"), ("DMX7EC-b", "Gen9"),
              ("DMX7 512+fs", "Gen5"), ("DMXH8", "Gen8"), ("DMX-new", "Gen2")]
MODES = [{"index": 0, "name": "[source]", "value": -1}, {"index": 1, "name": "PCM", "value": 0},
         {"index": 2, "name": "SDM (DSD)", "value": 1}]
RATES = {"pcm": [0, 705600, 768000], "sdm": [0, 5644800, 11289600, 12288000, 22579200]}


def _rows(spec, parse, at=10):
    return [{"index": at + i, "name": name, "value": i, "description": text, "parsed": parse(text)}
            for i, (name, text) in enumerate(spec)]


def _shipped(tmp_path, text):
    path = tmp_path / "shipped.json"
    path.write_text(text, encoding="utf-8")
    return hqp_atlas.read(path, None)


def _dated(tmp_path, pcm="2001-05-06", sdm="2001-05-06"):
    """The fixture at the shipped path with its releases dated, every other
    byte as the file writes it."""
    releases = json.dumps({"9.4.1/pcm": pcm, "9.4.1/sdm": sdm})
    return _shipped(tmp_path, FIXTURE.read_text(encoding="utf-8").replace(UNDATED, f'"releases": {releases}'))


def _one_family(tmp_path, family="pcm"):
    """The fixture as a shipped file holds it with one family dated and the
    other absent."""
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    engine = doc["engines"]["9.4.1"]
    key = f"9.4.1/{family}"
    for plural in hqp_atlas.KINDS.values():
        engine[plural] = {slug: e for slug, e in engine[plural].items() if e["release"] == key}
    names = {e["name"] for plural in hqp_atlas.KINDS.values() for e in engine[plural].values()}
    doc["releases"] = {key: "2001-05-06"}
    doc["slugs"] = [row for row in doc["slugs"] if row["name"] in names]
    doc["groups"] = [g for g in doc["groups"] if set(g["members"]) <= names]
    doc["pairs"] = [p for p in doc["pairs"] if set(p["members"]) <= names]
    engine["identity_groups"] = [g for g in engine["identity_groups"] if set(g["members"]) <= names]
    engine["fullscale"] = {slug: c for slug, c in engine["fullscale"].items() if slug in engine["modulators"]}
    return _shipped(tmp_path, json.dumps(doc, ensure_ascii=False))


def _read(atlas=None, mode="pcm", engine="9.4.1", *, filters=None, shapers=None, state=None, **kw):
    """HQPlayer as the route reads it: its lists in `mode`, a fixed rate and
    the first entries set; the file at the shipped path."""
    filters = filters if filters is not None else _rows(PCM if mode == "pcm" else SDM, parse_filter_description)
    shapers = (shapers if shapers is not None
               else _rows(DITHERS if mode == "pcm" else MODULATORS, parse_shaper_description, at=0))
    rates = [{"index": i, "rate": r} for i, r in enumerate(RATES[mode])]
    st = {"mode": 1 if mode == "pcm" else 2, "rate": 1, "filter": 10, "filterNx": 10, "filter1x": 10,
          "shaper": 0, "active_mode": 0 if mode == "pcm" else 1, "matrix_profile": "", "convolution": 0}
    st.update(state or {})
    return Read(info={"engine": engine, "product": "HQPlayer Desktop", "version": engine}, state=st,
                lists={"modes": MODES, "filters": filters, "shapers": shapers, "rates": rates},
                mode=mode, atlas=atlas, **kw)


def _say(tool, read, **args):
    return hqp_assistant.answer(tool, args, read)


def _every_output(atlas):
    """Every tool's text on every name, axis and pair of names, PCM and SDM,
    on the engine the file holds, a newer and an older one within its
    major, another major, and none reported."""
    out = []
    for engine in ("9.4.1", "9.5.0", "9.0.0", "8.0.0", ""):
        for mode, spec, shapers in (("pcm", PCM, DITHERS), ("sdm", SDM, MODULATORS)):
            read = _read(atlas, mode, engine)
            names = [n for n, _ in spec + shapers]
            for name in names:
                out += [_say("filter_facts", read, name=name), _say("atlas_link", read, name=name),
                        _say("differs_only_in", read, name=name)]
            for a in names:
                for b in names:
                    out.append(_say("compare", read, name_a=a, name_b=b))
            for axis in hqp_atlas.AXIS:
                out.append(_say("ab_pair", read, axis=axis))
                out += [_say("differs_only_in", read, name=name, axis=axis) for name in names]
    return out


# -- The table ---------------------------------------------------------------------


def test_the_table_names_six_tools_each_parameter_described():
    assert [t.name for t in hqp_assistant.TOOLS] == [
        "hqplayer_filter_facts", "hqplayer_compare", "hqplayer_differs_only_in", "hqplayer_ab_pair",
        "hqplayer_host_fit", "hqplayer_atlas_link"]
    assert set(hqp_assistant.BY_SHORT) == set(hqp_assistant.READS)
    for t in hqp_assistant.TOOLS:
        assert t.description and len({p.name for p in t.params}) == len(t.params)
        assert all(p.description for p in t.params)
        assert {p.name for p in t.params} <= {"name", "name_a", "name_b", "axis", "near", "rate", "mode"}
    # The axes the descriptions name are the atlas's own
    axes = ", ".join(hqp_atlas.AXIS)
    assert axes in hqp_assistant.BY_SHORT["differs_only_in"].description
    assert axes in hqp_assistant.AXES.format(axes=axes) and set(hqp_assistant.AXIS_KIND) == set(hqp_atlas.AXIS)


def test_a_missing_argument_or_a_refusal_is_the_answer():
    assert _say("filter_facts", _read(), name="") == "Give name: this tool needs it."
    assert _say("filter_facts", Read(output="dlna"), name="x").startswith(
        "HQPlayer is not the active audio output (currently: dlna): ")
    assert _say("compare", Read(held="The HQPlayer benchmark is running"), name_a="a", name_b="b") == (
        "The HQPlayer benchmark is running: HQPlayer's filters and modulators can be read again when it "
        "finishes.")
    assert _say("host_fit", Read(busy="HQPlayer is building the filter"), name="x") == \
        "HQPlayer is building the filter"
    assert _say("atlas_link", Read(error="timed out"), name="x") == "HQPlayer is not answering (timed out)."
    read = _read()._replace(lists={**_read().lists, "filters": None}, refused={"filters": "Error"})
    assert _say("filter_facts", read, name="demo-sinc-a") == "HQPlayer did not list its filters (Error)."


# -- Names -------------------------------------------------------------------------


def test_a_name_is_hqplayers_own_never_a_neighbour(tmp_path):
    atlas = _dated(tmp_path)
    assert _say("filter_facts", _read(atlas), name="DEMO-SINC-A").startswith(
        "demo-sinc-a — a filter HQPlayer lists in PCM mode.")
    assert _say("filter_facts", _read(atlas), name="sinc") == (
        "HQPlayer lists no filter or modulator named “sinc” now. Names that contain it: demo-sinc-a, "
        "demo-sinc-b. HQPlayer lists its modulators only in SDM mode.")
    assert _say("filter_facts", _read(atlas, "sdm"), name="DMX7") == (
        "HQPlayer lists no filter or modulator named “DMX7” now. Names that contain it: DMX7EC-c, DMX7EC-a, "
        "DMX7EC-b, DMX7 512+fs.")
    assert _say("filter_facts", _read(atlas), name="DMX5") == (
        "HQPlayer lists no filter or modulator named “DMX5” now. HQPlayer lists its modulators only in SDM mode.")
    assert _say("filter_facts", _read(atlas), name="DEMO-TPDF") == (
        "DEMO-TPDF — a dither HQPlayer lists in PCM mode.\nSautium has published no measurement of it.")


# -- filter_facts --------------------------------------------------------------------


def test_filter_facts_prints_the_card_as_published(tmp_path):
    text = _say("filter_facts", _read(_dated(tmp_path), engine="9.5.0"), name="demo-gauss-xa").split("\n")
    assert text[:3] == ["demo-gauss-xa — a filter HQPlayer lists in PCM mode.",
                        "Measured on HQPlayer engine 9.4.1 · this HQPlayer runs engine 9.5.0",
                        "What HQPlayer says:"]
    assert "Properties:" in text and "  1x · 44.1k → 705.6k:" in text and "    Phase: linear" in text
    assert "Differs only in:" in text and "  Apodizing — from demo-gauss-x." in text
    assert text[-1] == "Measured properties: https://sautium.net/hqplayer/filters/demo-gauss-xa/"


@pytest.mark.parametrize("variant", ["none", "undated", "pcm-only"])
def test_before_publication_a_name_has_hqplayers_facts_and_that_nothing_is_published(tmp_path, variant):
    atlas = {"none": None, "undated": _shipped(tmp_path, FIXTURE.read_text(encoding="utf-8")),
             "pcm-only": _one_family(tmp_path)}[variant]
    mod = _say("filter_facts", _read(atlas, "sdm"), name="DMX7EC-a").split("\n")
    assert mod == ["DMX7EC-a — a modulator HQPlayer lists in SDM mode.",
                   "Sautium has published no measurement of it.",
                   "What HQPlayer says:", "  HQPlayer's generation field: Gen9",
                   "  From HQPlayer's description “Gen9”."]
    if variant != "pcm-only":
        assert _say("filter_facts", _read(atlas), name="demo-sinc-a").split("\n")[1] == \
            "Sautium has published no measurement of it."
        assert _say("atlas_link", _read(atlas), name="demo-sinc-a") == "Sautium has published no page of it."
        assert _say("ab_pair", _read(atlas), axis="length") == "Sautium has published no pair on this axis."


def test_another_engine_within_the_major_is_named_and_another_major_gets_no_measured_fact(tmp_path):
    atlas = _dated(tmp_path)
    assert _say("filter_facts", _read(atlas, engine="9.0.0"), name="demo-sinc-a").split("\n")[1] == \
        "Measured on HQPlayer engine 9.4.1 · this HQPlayer runs engine 9.0.0"
    other = _say("filter_facts", _read(atlas, engine="8.0.0"), name="demo-sinc-a").split("\n")
    assert other[1] == ("Measured on HQPlayer engine 9.4.1 · this HQPlayer runs engine 8.0.0 — another major "
                        "version: no measured fact is given for it.")
    assert "Properties:" not in other and "What HQPlayer says:" in other
    for engine, runs in (("", "does not report its engine"), ("9.4." + "1" * 70, "runs engine 9.4." + "1" * 70)):
        line = _say("filter_facts", _read(atlas, engine=engine), name="demo-sinc-a").split("\n")[1]
        assert line == f"Measured on HQPlayer engine 9.4.1 · this HQPlayer {runs}: no measured fact is given for it."


def test_a_name_hqplayer_describes_not_says_so_where_its_description_would_stand(tmp_path):
    filters = _rows([(name, "") for name, _ in PCM], parse_filter_description)
    text = _say("filter_facts", _read(_dated(tmp_path), filters=filters), name="demo-sinc-a").split("\n")
    assert text[1:3] == ["Measured on HQPlayer engine 9.4.1", "HQPlayer gives no description of it."]
    assert "What HQPlayer says:" not in text


def test_a_name_the_published_family_does_not_measure(tmp_path):
    text = _say("filter_facts", _read(_one_family(tmp_path)), name="demo-new").split("\n")
    assert text[1:3] == ["Measured on HQPlayer engine 9.4.1", "Not measured on HQPlayer engine 9.4.1."]


# -- compare --------------------------------------------------------------------------


def test_compare_twins_say_their_group_and_side_by_side_values(tmp_path):
    text = _say("compare", _read(_dated(tmp_path)), name_a="demo-sinc-a", name_b="demo-sinc-b").split("\n")
    assert text[:3] == ["demo-sinc-a and demo-sinc-b — two filters HQPlayer lists in PCM mode.",
                        "Measured on HQPlayer engine 9.4.1", "What HQPlayer says:"]
    assert ("  Signalyst's technical rating: 4/5, both — relative to the other filters; not a listening score"
            in text)
    assert "Same output as demo-sinc-b — the same output within one step, in this fictional file." in text
    assert "Properties, demo-sinc-a / demo-sinc-b:" in text
    assert "    −3 dB, kHz: 17.532, both" in text and "    Filter delay, ms: 6.0 ± 20 / 6.5 ± 20" in text
    # The group's figure stands for each member's reading, and the card's
    # note says so — once for the two
    note = [line for line in text if line.startswith("  For a member of ")]
    assert note == ["  For a member of demo-sinc-a and demo-sinc-b, each number is the group's figure where it "
                    "holds one — the members' median, a bound where any member's reading is one; the filter delay "
                    "is the member's own."]


def test_compare_never_computes_and_keeps_each_side(tmp_path):
    text = _say("compare", _read(_dated(tmp_path)), name_a="demo-poly-mp", name_b="demo-poly-lp")
    assert "Same magnitude as demo-poly-lp — one magnitude with two phases, in this fictional file." in text
    assert "    Phase: minimum / linear" in text
    assert "    Ringing before the main lobe, above −100 dB, ms: 0.0 / 0.300" in text
    text = _say("compare", _read(_dated(tmp_path), "sdm"), name_a="DMX5", name_b="DMX7EC-a").split("\n")
    assert text[1:3] == ["Measured on HQPlayer engine 9.4.1", "What HQPlayer says; the structure:"]
    assert "  HQPlayer's generation field: Gen1 / Gen9" in text
    # A side with no reading at a rate is "not given": the card's "—" says
    # the method defines none
    assert "    DSD128: −81.75 / −91.5" in text and "    DSD256 (48k): not given / ≤ −97.5" in text
    assert "  Rates HQPlayer refused: DSD256 / none" in text
    # DMX7EC-a's idle noise is its group's figure; its pair with DMX5 gives
    # its own reading above
    assert [line for line in text if line.startswith("  For a member of ")] == [
        "  For a member of DMX7EC: -c, -a, -b, the idle noise is the group's figure where it holds one — the "
        "members' median, a bound where any member's reading is one."]


def test_compare_of_two_names_with_no_idle_reading_gives_no_empty_table(tmp_path):
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    doc["releases"] = {"9.4.1/pcm": "2001-05-06", "9.4.1/sdm": "2001-05-06"}
    body = doc["engines"]["9.4.1"]
    body["modulators"]["dmx7-512fs"]["rates"] = {}
    body["fullscale"].pop("dmx7-512fs", None)
    text = _say("compare", _read(_shipped(tmp_path, json.dumps(doc, ensure_ascii=False)), "sdm"),
                name_a="DMXH8", name_b="DMX7 512+fs").split("\n")
    at = text.index("Per rate, DMXH8 / DMX7 512+fs:")
    assert text[at + 1:at + 2] == ["  Rates HQPlayer refused: DSD128 and DSD256 (48k) / DSD128 and DSD256 (48k)"]
    assert not any(line.startswith("  Idle noise") or line.startswith("  For a member of ") for line in text)


def test_compare_says_not_given_for_a_side_hqplayer_does_not_describe(tmp_path):
    filters = _rows([(name, "" if name == "demo-sinc-b" else text) for name, text in PCM], parse_filter_description)
    text = _say("compare", _read(_dated(tmp_path), filters=filters), name_a="demo-sinc-a",
                name_b="demo-sinc-b").split("\n")
    assert "  Signalyst's technical rating: 4/5 / not given — relative to the other filters; not a listening " \
           "score" in text
    assert "  The author's focus: transients, timbre, space / not given" in text


def test_compare_refuses_one_name_twice_and_two_kinds(tmp_path):
    read = _read(_dated(tmp_path), "sdm")
    assert _say("compare", read, name_a="DMX5", name_b="dmx5") == "Both names are DMX5: give two different names."
    assert _say("compare", read, name_a="demo-sinc-a", name_b="DMX5") == (
        "demo-sinc-a is a filter and DMX5 a modulator: compare two filters or two modulators.")


def test_compare_says_what_each_side_alone_is(tmp_path):
    text = _say("compare", _read(None), name_a="demo-sinc-a", name_b="demo-sinc-b").split("\n")
    assert text[1:3] == ["demo-sinc-a: Sautium has published no measurement of it.",
                         "demo-sinc-b: Sautium has published no measurement of it."]
    text = _say("compare", _read(_one_family(tmp_path)), name_a="demo-sinc-a", name_b="demo-new").split("\n")
    assert text[1:3] == ["Measured on HQPlayer engine 9.4.1", "demo-new: Not measured on HQPlayer engine 9.4.1."]
    assert "Properties, demo-sinc-a / demo-new:" not in text


def test_compare_with_no_group_or_pair_says_so(tmp_path):
    text = _say("compare", _read(_dated(tmp_path)), name_a="demo-sinc-a", name_b="demo-poly-lp")
    assert "No group or approved pair holds both." in text


# -- This host's speeds -------------------------------------------------------------------


_TH = {"ok": 1.25, "no": 1.0, "dropout_speed": None}


def test_compares_cpu_line_is_the_pickers_marks():
    hr = {"filters": {"demo-sinc-a": {"class": "ok", "speed": 2.5, "n": 3, "settled": True}},
          "shapers": {}, "rates": {}, "thresholds": _TH, "source": {"rate": 44100, "channels": 2},
          "setting": {"rate_out": 705600, "filter": "demo-sinc-a", "shaper": "none"}}
    text = _say("compare", _read(endpoint={"hqp_engine": "9.4.1"}, headroom=hr),
                name_a="demo-sinc-a", name_b="demo-sinc-b").split("\n")
    at = text.index("On this HQPlayer, at 705.6 kHz with none, for a 44.1 kHz source:")
    assert text[at + 1:at + 3] == [
        "  demo-sinc-a runs at 2.50× (3 samples) — keeps up here (from 1.25×)",
        "  demo-sinc-b: not measured on this HQPlayer — the Benchmark on the HQPlayer screen measures it."]
    auto = {**hr, "setting": {**hr["setting"], "rate_out": 0}, "source": {"rate": 48000, "channels": 1}}
    assert "On this HQPlayer, at auto with none, for a 48 kHz source with 1 channel:" in _say(
        "compare", _read(endpoint={"hqp_engine": "9.4.1"}, headroom=auto), name_a="demo-sinc-a",
        name_b="demo-sinc-b").split("\n")
    assert hqp_assistant.CPU_NO_SOURCE in _say("compare", _read(endpoint={"hqp_engine": "9.4.1"}, headroom={}),
                                               name_a="demo-sinc-a", name_b="demo-sinc-b")
    assert hqp_assistant.NOTHING_HERE in _say("compare", _read(), name_a="demo-sinc-a", name_b="demo-sinc-b")


def _host_row(src, rate, other, cls, speed=None, n=0, channels=2, settled=True, dropout=False):
    return {"filter": "demo-sinc-a", "shaper": other, "rate_out": rate, "src_rate": src, "src_channels": channels,
            "n": n, "speed": speed, "class": cls, "bench_settled": settled if speed else None,
            "init_s": None, "dropout": dropout}


def test_host_fit_groups_this_hosts_rows_by_source_and_names_no_source_in_a_row():
    rows = [_host_row(44100, 0, "DMX5", "unstarted"),
            _host_row(44100, 5644800, "DMX5", "ok", 2.5, 4),
            _host_row(88200, 5644800, "DMX5", "tight", 1.1, 1, settled=False),
            _host_row(2822400, 5644800, "DMX5", "no", 0.8, 2, dropout=True),
            _host_row(2822400, 11289600, "DMX5", "refused", channels=6),
            _host_row(48000, 5644800, "DMX5", "ok", 3.0, 2, channels=1)]
    host = {"context": {"engine": "9.4.1", "cuda": "full", "mode": "SDM (DSD)", "matrix_profile": "Room",
                        "convolution": True},
            "knob": "filter", "thresholds": _TH, "rows": rows, "total": len(rows)}
    text = _say("host_fit", _read(host=host), name="demo-sinc-a").split("\n")
    assert text == [
        "On this HQPlayer — engine 9.4.1, CUDA filters and convolution, SDM (DSD), matrix Room, convolution on:",
        "  From a 44.1 kHz source:",
        "    auto · DMX5: never got going here when the Benchmark asked it",
        "    DSD128 · DMX5: runs at 2.50× (4 samples) — keeps up here (from 1.25×)",
        "  From a 88.2 kHz source:",
        "    DSD128 · DMX5: runs at 1.10× (1 sample), the readings did not settle within 15 s — tight here "
        "(from 1× to 1.25×)",
        "  From a DSD64 source:",
        "    DSD128 · DMX5: runs at 0.80× (2 samples), and it dropped out here — too slow here (under 1×)",
        "  From a DSD64 source with 6 channels:",
        "    DSD256 · DMX5: refused by HQPlayer when the Benchmark asked it",
        "  From a 48 kHz source with 1 channel:",
        "    DSD128 · DMX5: runs at 3.00× (2 samples) — keeps up here (from 1.25×)",
        "These speeds are this HQPlayer's own, measured while it played and by its Benchmark."]
    assert not any("source" in line for line in text if line.startswith("    "))
    more = _say("host_fit", _read(host={**host, "total": 30}), name="demo-sinc-a").split("\n")
    assert more[-2] == "  … and 24 more."


def test_host_fit_says_why_it_has_nothing():
    assert _say("host_fit", _read(host={"words": hqp_assistant.NOTHING_HERE}), name="x") == \
        hqp_assistant.NOTHING_HERE
    # HQPlayer's lists are not read for host_fit: the words neither say the
    # name is HQPlayer's nor that it is not
    assert hqp_assistant.no_record("x", "SDM (DSD)") == (
        "This HQPlayer holds no record of “x” in SDM (DSD) mode: the Benchmark on the HQPlayer screen measures "
        "what HQPlayer lists.")
    at = {"context": {"engine": "9.4.1", "cuda": "off", "mode": "PCM", "matrix_profile": None,
                      "convolution": False},
          "knob": "filter", "name": "demo-sinc-a", "rate": 705600, "thresholds": _TH, "rows": [], "total": 0}
    assert _say("host_fit", _read(host=at), name="DEMO-SINC-A") == (
        "This HQPlayer holds no record of “demo-sinc-a” at 705.6 kHz in PCM mode: the Benchmark on the HQPlayer "
        "screen measures what HQPlayer lists.")


def test_host_fit_reads_a_rate_this_host_holds_speeds_at_and_a_mode_as_hqplayer_names_them():
    # The rates are this host's records' in the mode asked — HQPlayer's own
    # list is the mode it is in
    rates = [0, 705600, 5644800, 24576000]
    assert hqp_assistant.host_rate("", rates, "SDM (DSD)") == (None, None)
    assert hqp_assistant.host_rate("705.6 kHz", rates, "SDM (DSD)") == (705600, None)
    assert hqp_assistant.host_rate("dsd128", rates, "SDM (DSD)") == (5644800, None)
    assert hqp_assistant.host_rate("24576000", rates, "SDM (DSD)") == (24576000, None)
    assert hqp_assistant.host_rate("Auto", rates, "SDM (DSD)") == (0, None)
    assert hqp_assistant.host_rate("DSD256", rates, "SDM (DSD)") == (
        None, "This HQPlayer holds no record at “DSD256” in SDM (DSD) mode. The rates it holds records at there: "
              "auto, 705.6 kHz, DSD128, DSD512 (48k).")
    state = {"mode": 0, "active_mode": 1}
    assert hqp_assistant.host_mode("", state, MODES) == ("SDM (DSD)", None)
    assert hqp_assistant.host_mode("PCM", state, MODES) == ("PCM", None)
    assert hqp_assistant.host_mode("dsd", state, MODES) == (None, "A mode is pcm or sdm.")
    assert hqp_assistant.host_mode("sdm", state, MODES[:2]) == (None, "HQPlayer lists no SDM mode.")
    names = {"filters": ["demo-sinc-a"], "shapers": ["DMX5", "dmx5"]}
    assert hqp_assistant.host_name("DEMO-SINC-A", names) == ("filter", "demo-sinc-a")
    assert hqp_assistant.host_name("DMX5", names) == ("shaper", "DMX5")
    assert hqp_assistant.host_name("Dmx5", names) is None


# -- differs_only_in and ab_pair ----------------------------------------------------------


def test_differs_only_in_gives_a_modulator_pairs_size_per_rate_in_the_sites_words(tmp_path):
    text = _say("differs_only_in", _read(_dated(tmp_path), "sdm"), name="DMX7EC-a").split("\n")
    assert text[2:] == [
        "Differs only in:",
        "  Order — from DMX5.",
        "  At DSD128, DMX7EC-a / DMX5: idle noise: −91.25 / −81.75 dB; the noise shaping's largest difference: "
        "9.25 dB. Also differs: HQPlayer's generation field: Gen9 / Gen1.",
        "  From Fictional Author, 2001-02-03."]


def test_differs_only_in_narrows_by_axis_and_names_what_it_left_out(tmp_path):
    read = _read(_dated(tmp_path))
    assert _say("differs_only_in", read, name="demo-gauss-x", axis="phase").split("\n")[-1] == \
        "No approved pair holds it on the phase axis."
    assert _say("differs_only_in", read, name="demo-gauss-x", axis="order") == \
        "demo-gauss-x is a filter, and order is an axis of modulator pairs."
    assert _say("differs_only_in", read, name="demo-gauss-x", axis="window") == (
        "An axis is one of: phase, apodizing, length, order, ec, adaptive, generation, tuning, multibit.")
    gone = _read(_dated(tmp_path), filters=[r for r in _read().lists["filters"] if r["name"] != "demo-gauss-xa"])
    assert _say("differs_only_in", gone, name="demo-gauss-x", axis="apodizing").split("\n")[-1] == \
        "Left out, as this HQPlayer does not list them now: demo-gauss-xa."


def test_ab_pair_offers_the_approved_pairs_with_their_size_and_the_steps(tmp_path):
    text = _say("ab_pair", _read(_dated(tmp_path)), axis="apodizing").split("\n")
    assert text[:6] == [
        "Approved pairs that differ only in one property — Apodizing:",
        "demo-gauss-x / demo-gauss-xa",
        "  At 1x · 44.1k → 705.6k, demo-gauss-x / demo-gauss-xa: HQPlayer's label: not apodizing / apodizing; "
        "−3 dB: 15.75 / 14.5 kHz. Also differs: ratio class: Any / 2^x up.",
        "  Measured where both play: 1x · 44.1k → 705.6k.",
        "  HQPlayer describes demo-gauss-x as Any and demo-gauss-xa as 2^x up.",
        "  Not measured: the window. From the manual (§9.9)."]
    assert text[-6:] == list(hqp_assistant.AB_STEPS)
    near = _say("ab_pair", _read(_dated(tmp_path)), axis="apodizing", near="demo-poly-lp")
    assert "None of them holds demo-poly-lp." in near
    sdm = _say("ab_pair", _read(_dated(tmp_path), "sdm"), axis="order").split("\n")
    assert sdm[1:4] == [
        "DMX5 / DMX7EC-a",
        "  At DSD128, DMX5 / DMX7EC-a: idle noise: −81.75 / −91.25 dB; the noise shaping's largest difference: "
        "9.25 dB. Also differs: HQPlayer's generation field: Gen1 / Gen9.",
        "  Measured where both play: DSD128."]
    assert _say("ab_pair", _read(_dated(tmp_path)), axis="order") == \
        "Left out, as this HQPlayer does not list them now: DMX5, DMX7EC-a."


# -- The rate check (Q6) -------------------------------------------------------------------


def test_filter_facts_adds_the_rate_checks_lines_for_the_name_as_if_set(tmp_path):
    read = _read(_dated(tmp_path), adaptive="not checked", sources=Sources({48000: 2}, first=48000))
    text = _say("filter_facts", read, name="demo-gauss-xa").split("\n")
    at = text.index("With demo-gauss-xa in both filter slots, at this output rate and queue:")
    assert text[at + 1].startswith("  HQPlayer describes demo-gauss-xa as 2^x up: 705.6 kHz is no power-of-two "
                                   "multiple of 48 kHz")
    assert "in both filter slots" not in _say("filter_facts", read._replace(adaptive="checked"),
                                               name="demo-gauss-xa")
    assert "in both filter slots" not in _say("filter_facts", read, name="demo-gauss-x")   # Any


def test_an_empty_line_of_the_file_is_an_empty_line_never_an_error(tmp_path):
    # The file's shape takes an empty text: the card's line of it is empty,
    # as on the screen
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    doc["releases"] = {"9.4.1/pcm": "2001-05-06", "9.4.1/sdm": "2001-05-06"}
    doc["engines"]["9.4.1"]["filters"]["demo-sinc-a"]["intent"][0]["text"] = ""
    text = _say("filter_facts", _read(_shipped(tmp_path, json.dumps(doc, ensure_ascii=False))),
                name="demo-sinc-a").split("\n")
    at = text.index("The author's stated purpose:")
    assert text[at + 1].strip() == "" and text[at + 2].startswith("  In Sautium's words, from ")


# -- What leaves the node: the published atlas alone (F4 (a)) -----------------------------


def test_nothing_of_an_undated_family_reaches_any_output(tmp_path):
    # PCM dated, SDM in the file without a date — a file at the shipped path
    # before SDM's release — its entries planted with a canary where the card
    # would print it, and with the fixture's own words and numbers
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    doc["releases"] = {"9.4.1/pcm": "2001-05-06", "9.4.1/sdm": None}
    for e in doc["engines"]["9.4.1"]["modulators"].values():
        e["context_filter"] = CANARY
    outputs = _every_output(_shipped(tmp_path, json.dumps(doc, ensure_ascii=False)))
    every = "\n".join(outputs)
    for token in (CANARY, "−91.25", "−97.5", "three fictional names", "Same figures", "near full scale",
                  "dmx7ec", "modulators/"):
        assert token not in every, token
    said = [text.split("\n")[1] for text in outputs
            if " — a modulator HQPlayer lists in SDM mode." in text.split("\n")[0]]
    assert said and all(line == "Sautium has published no measurement of it."
                        or line.endswith("no measured fact is given for it.") for line in said)
    # The family dated reads whole
    assert "Measured properties: https://sautium.net/hqplayer/filters/demo-sinc-a/" in every


def test_a_newer_engine_of_the_major_with_no_date_is_never_named(tmp_path):
    # 9.6.0 in the file, undated, after 9.4.1, dated: a 9.6 HQPlayer is told
    # 9.4.1's facts, as the shipped file, which holds the dated releases
    # alone, would tell it
    from test_hqp_atlas import _another_engine
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    doc["releases"] = {"9.4.1/pcm": "2001-05-06", "9.4.1/sdm": "2001-05-06"}
    _another_engine(doc, "9.6.0")["filters"]["demo-sinc-a"]["contexts"]["1x-44.1k-705.6k"]["phase"] = "minimum"
    atlas = _shipped(tmp_path, json.dumps(doc, ensure_ascii=False))
    outputs = []
    for mode, spec, shapers in (("pcm", PCM, DITHERS), ("sdm", SDM, MODULATORS)):
        read = _read(atlas, mode, "9.6.2")
        for name, _ in spec + shapers:
            outputs += [_say(tool, read, name=name) for tool in ("filter_facts", "atlas_link", "differs_only_in")]
        outputs += [_say("ab_pair", read, axis=axis) for axis in hqp_atlas.AXIS]
    assert "9.6.0" not in "\n".join(outputs)
    facts = _say("filter_facts", _read(atlas, "pcm", "9.6.2"), name="demo-sinc-a").split("\n")
    assert facts[1] == "Measured on HQPlayer engine 9.4.1 · this HQPlayer runs engine 9.6.2"
    assert facts[facts.index("  1x · 44.1k → 705.6k:") + 1] == "    Phase: linear"


def test_an_undated_file_at_the_shipped_path_gives_no_number_and_no_word_of_the_file(tmp_path):
    every = "\n".join(_every_output(_shipped(tmp_path, FIXTURE.read_text(encoding="utf-8"))))
    for token in ("17.532", "−91.5", "9.25", "Same output", "Same figures", "fictional", "Fictional", "9.4.1",
                  "sautium.net"):
        assert token not in every, token


def test_numbers_are_the_files_tokens_and_no_reference_speed_is_given(tmp_path):
    every = "\n".join(_every_output(_dated(tmp_path)))
    for token in ("0.300", "6.0 ± 20", "−0.5 ± 20", "12.346", "≤ −97.5"):
        assert token in every, token
    for token in ("987.65", "876.54", "98.765", "9.8765", "process_speed", "not comparable", "-0.5",
                  "never re-read in full", "Fictional Poster"):
        assert token not in every, token


def test_a_link_only_from_a_dated_release_and_only_in_its_lines(tmp_path):
    for atlas, links in ((_dated(tmp_path), True), (_shipped(tmp_path, FIXTURE.read_text(encoding="utf-8")), False)):
        for text in _every_output(atlas):
            for line in text.split("\n"):
                if "sautium.net" in line:
                    assert links and re.fullmatch(r"Measured properties(?: of \S.*)?: https://sautium\.net/"
                                                  r"hqplayer/(?:filters|modulators)/[a-z0-9-]+/", line), line


# -- The words ---------------------------------------------------------------------------


def _constants():
    for name, value in vars(hqp_assistant).items():
        if name.isupper():
            for s in (value.values() if isinstance(value, dict) else value
                      if isinstance(value, tuple) else [value]):
                if isinstance(s, str):
                    yield s


def test_no_word_ranks_and_no_rate_is_said_as_a_label(tmp_path):
    said = list(_constants()) + [t.description for t in hqp_assistant.TOOLS] + [
        p.description for t in hqp_assistant.TOOLS for p in t.params]
    for atlas in (_dated(tmp_path), None):
        said += _every_output(atlas)
    assert sorted({m.group(0) for text in said for m in BANNED.finditer(text)}) == []
    assert not any("· from" in text for text in said)
    # A modulator's refusals are the rates measured, never stretched to others
    assert not any(re.search(r"\bonly at\b|\bbelow\b|×\d", text) for text in said)
    contexts = {hqp_atlas.context_label(k) for k in ("1x-44.1k-705.6k", "nx-88.2k-705.6k")}
    assert {m for text in said for m in re.findall(r"(?:1x|Nx) · [0-9.]+k → [0-9.]+k", text)} == contexts


def test_both_prompts_carry_the_block_once_and_it_ranks_nothing_and_names_no_finding():
    import assistant_prompt
    block = assistant_prompt._HQPLAYER_FILTERS
    for prompt in (assistant_prompt.CLAUDE_SYSTEM_PROMPT, assistant_prompt.CODEX_SYSTEM_PROMPT,
                   assistant_prompt.API_SYSTEM_PROMPT):
        assert prompt.count(block) == 1
    assert BANNED.search(block) is None
    assert set(re.findall(r"\bhqplayer_[a-z_]+", block)) <= {t.name for t in hqp_assistant.TOOLS}
    # A model told to "offer a pair" offered two names with the same output,
    # on a property no axis holds: the block names the tool's pairs alone
    assert "approved pairs hqplayer_ab_pair returns" in block and "are no pair to listen to" in block
    # No engine number, no address and no measured value: the block is public text
    assert re.search(r"\d|sautium\.net|https?:", block.replace("n/5", "")) is None
    for tool in hqp_assistant.TOOLS:
        params = ", ".join(p.name for p in tool.params)
        assert assistant_prompt.API_SYSTEM_PROMPT.count(f"- **{tool.name}({params})**: {tool.description}") == 1


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_chat_says_the_six_look_up_filters_and_modulators():
    # app-shell.js toolPipLabel run as written: "hqplayer" holds "play", and
    # the six read; the transport keeps its label
    src = (BACKEND / "static" / "app-shell.js").read_text(encoding="utf-8")
    fn = re.search(r"\n  function toolPipLabel\(raw\) \{\n.*?\n  \}\n", src, re.S).group(0)
    names = [t.name for t in hqp_assistant.TOOLS]
    asked = names + [f"mcp__assistant__{name}" for name in names] + ["hqplayer_play", "mcp__assistant__hqplayer_next"]
    script = f"{fn}\nconsole.log(JSON.stringify({json.dumps(asked)}.map(toolPipLabel)));"
    out = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == ["looking up filters and modulators…"] * 12 + ["controlling playback…"] * 2
