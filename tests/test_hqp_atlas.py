"""The HQPlayer atlas as the node reads it (backend/hqp_atlas.py), on a
synthetic file in the atlas's shape (fixtures/hqp_atlas/app-synthetic.json):
every name, number and word in it is invented, none a measurement. Variants —
another schema, a broken pair, a dated release, two engines — are made from it
here. Pure: no database, no HQPlayer."""

import copy
import json
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

import hqp_atlas  # noqa: E402
from hqp_atlas import Num, plain  # noqa: E402
from hqplayer_client import parse_filter_description, parse_shaper_description  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "hqp_atlas" / "app-synthetic.json"
CANARY = "CANARY-c2-atlas-7f3a"
FILTERS = "engines.9.4.1.filters"

# HQPlayer's lists as the client returns them: its order, its descriptions.
PCM = [("demo-sinc-a", "4/5 transients, timbre, space ⥮ 2^x up"),
       ("demo-sinc-b", "4/5 transients, timbre, space ⥮ 2^x up"),
       ("demo-poly-lp", "3/5 space ⥮ Any"),
       ("demo-poly-mp", "3/5 space, transients ⥮ Any"),
       ("demo-gauss-x", "5/5 timbre ⥮ Any"),
       ("demo-gauss-xa", "5/5 timbre ⥮ 2^x up"),
       ("demo-gone", "2/5 ⥮ Int up"),
       ("demo-new", "1/5 ⥮ 1:1")]
SDM = [("demo-sinc-a", "4/5 transients, timbre, space ⥣ 2^x"),
       ("demo-sinc-b", "4/5 transients, timbre, space ⥣ 2^x"),
       ("demo-poly-lp", "3/5 space ⥣ Any"),
       ("demo-poly-mp", "3/5 transients, space ⥣ Any"),
       ("demo-gauss-x", "5/5 timbre ⥣ Any"),
       ("demo-2s", "2/5 ⥣ Any")]
MODULATORS = [("DMX5", "Gen1"), ("DMX7EC-c", "Gen9"), ("DMX7EC-a", "Gen9"), ("DMX7EC-b", "Gen9"),
              ("DMX7 512+fs", "Gen5"), ("DMXH8", "Gen8"), ("DMX-new", "Gen2")]
# The words no string may hold: the atlas's ranking words and the words for an
# engine that stops, each a whole word in any case.
BANNED = re.compile(r"\b(best|better|worse|worst|superior|inferior|recommend|recommended|prefer|preferred|"
                    r"ideal|optimal|stall|restart|hang|crash|freeze|bug)\b", re.IGNORECASE)
# The pickers' own words, drawn by the screen around what this module gives.
FRAME = ("Filter", "Modulator", "Search…", "No filters match.", "No modulators match.", "★ Favourites", "DSD5",
         "ASDM5", "DSD7", "ASDM7", "AMSDM", "AHM", "Details: {name}", "Close", "The other contexts",
         "Details unavailable", "HQPlayer does not list it now — Refresh asks again.")


@pytest.fixture(autouse=True)
def _words_said_afresh(monkeypatch):
    monkeypatch.setattr(hqp_atlas, "_unworded", set())


def _rows(spec, kind):
    parse = parse_filter_description if kind == "filter" else parse_shaper_description
    return [{"index": 10 + i, "name": name, "value": i, "arg": 1, "description": text, "parsed": parse(text)}
            for i, (name, text) in enumerate(spec)]


def _doc():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _write(path, doc):
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


def _read(tmp_path, doc=None):
    """The fixture, or a variant of it, as the node's own copy; nothing shipped."""
    overlay = tmp_path / "overlay.json"
    if doc is None:
        overlay.write_bytes(FIXTURE.read_bytes())
    else:
        _write(overlay, doc)
    return hqp_atlas.read(tmp_path / "shipped.json", overlay)


@pytest.fixture
def atlas(tmp_path):
    return _read(tmp_path)


def _filter(doc, slug):
    return doc["engines"]["9.4.1"]["filters"][slug]


def _strings(x):
    """Every text a result holds, keys aside."""
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for value in x.values():
            yield from _strings(value)
    elif isinstance(x, (list, tuple)):
        for value in x:
            yield from _strings(value)


def _card_text(c):
    """A card as the lines a reader sees."""
    out = [c["title"]] + [plain(s) for s in c["status"]]

    def items(its):
        for it in its:
            if "term" in it:
                out.append(f"{it['term']}: {plain(it['def'])}")
            elif "line" in it or "note" in it:
                out.append(plain(it.get("line") or it["note"]))
            elif "table" in it:
                t = it["table"]
                out.append(" | ".join([t["corner"] or ""] + t["columns"]))
                out.extend(" | ".join([r["label"]] + [plain(x) for x in r["cells"]]) for r in t["rows"])
            else:
                out.append(it["more"])
                items(it["items"])

    for s in c["sections"]:
        out.append(f"## {s['head']}")
        items(s["items"])
    return out


def _chips(f):
    return [plain(c["parts"]) for c in f["chips"]]


def _facts_of(atlas, kind, spec, engine, mode):
    rows = _rows(spec, kind)
    return {row["name"]: f for row, f in zip(rows, hqp_atlas.facts(atlas, kind, rows, engine, mode))}


def _card(atlas, kind, name, spec, engine="9.4.1", mode="pcm"):
    return hqp_atlas.card(atlas, kind, name, _rows(spec, kind), engine, mode)


def _everything(atlas, engines=("9.4.1", "9.5.0", "9.0.0", "", "8.0.0")):
    """Every result for every fixture name in every state: rows, heads, cards."""
    out = []
    for engine in engines:
        for kind, spec, mode in (("filter", PCM, "pcm"), ("filter", SDM, "sdm"), ("modulator", MODULATORS, "sdm"),
                                 ("dither", [("DEMO-TPDF", "")], "pcm")):
            rows = _rows(spec, kind)
            out.append(hqp_atlas.facts(atlas, kind, rows, engine, mode))
            out.append(hqp_atlas.head(atlas, engine, mode))
            out += [hqp_atlas.card(atlas, kind, row["name"], rows, engine, mode) for row in rows]
    return out


# -- Reading ---------------------------------------------------------------------


def test_the_nodes_own_copy_is_read_while_no_file_is_shipped(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="hqp_atlas")
    atlas = _read(tmp_path)
    assert atlas is not None and atlas.origin == "overlay" and atlas.engines == ["9.4.1"]
    assert "read the node's own atlas copy" in caplog.text
    assert hqp_atlas.read(tmp_path / "shipped.json", tmp_path / "none.json") is None
    assert hqp_atlas.read(tmp_path / "shipped.json", None) is None


def test_a_shipped_file_wins_and_the_overlay_is_never_opened(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="hqp_atlas")
    shipped = tmp_path / "shipped.json"
    shipped.write_bytes(FIXTURE.read_bytes())
    overlay = tmp_path / "overlay.json"
    overlay.write_text("{ not json", encoding="utf-8")
    atlas = hqp_atlas.read(shipped, overlay)
    assert atlas.origin == "shipped"
    assert "own atlas copy" not in caplog.text

    shipped.write_text('{"schema": "another/v9"}', encoding="utf-8")
    _write(overlay, _doc())
    assert hqp_atlas.read(shipped, overlay) is None
    assert "the shipped atlas file refused whole" in caplog.text
    assert "own atlas copy" not in caplog.text


def test_a_changed_file_is_read_again_and_an_unchanged_one_is_not(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="hqp_atlas")
    first = _read(tmp_path)
    assert hqp_atlas.read(tmp_path / "shipped.json", tmp_path / "overlay.json") is first
    doc = _doc()
    _filter(doc, "demo-sinc-a")["contexts"]["1x-44.1k-705.6k"]["phase"] = "minimum"
    _write(tmp_path / "overlay.json", doc)
    again = hqp_atlas.read(tmp_path / "shipped.json", tmp_path / "overlay.json")
    assert again is not first
    found = again.lookup("filter", "demo-sinc-a", "9.4.1", "pcm")
    assert found.entry["contexts"]["1x-44.1k-705.6k"]["phase"] == "minimum"
    assert caplog.text.count("read the node's own atlas copy") == 2


def _break(doc, how):
    entry = _filter(doc, "demo-sinc-a")
    if how == "another schema":
        doc["schema"] = "hqp-atlas-app/v2"
    elif how == "a missing key":
        del doc["engines"]
    elif how == "a wrong type deep inside":
        entry["contexts"]["nx-88.2k-705.6k"]["time_invariant"] = "yes"
    elif how == "an unread value deep inside":
        entry["contexts"]["1x-44.1k-705.6k"]["phase"] = CANARY
    elif how == "a name of another shape":
        doc["slugs"][0]["name"] = f"{CANARY} / a path"
    elif how == "apodizing without its log name":
        del entry["log_name"]
    elif how == "a log name without apodizing":
        del entry["apodizing"]
    elif how == "a manual mark without its sources":
        del entry["doc_sources"]
    elif how == "manual sources without a mark":
        del entry["apod_doc"]
    elif how == "a group figure outside the closed list":
        figures = doc["engines"]["9.4.1"]["identity_groups"][0]["figures"]["1x-44.1k-705.6k"]
        figures["post_ring_ms100"] = figures.pop("post_ring_ms_100")
    elif how == "a group it names that the file does not hold":
        entry["groups"] = ["demo-sinc-a+demo-zzz"]
    elif how == "an entry under another slug":
        doc["engines"]["9.4.1"]["filters"]["demo-other"] = doc["engines"]["9.4.1"]["filters"].pop("demo-sinc-a")
    elif how == "a rate no label names":
        doc["engines"]["9.4.1"]["modulators"]["dmx5"]["rates"]["5644800"]["fs_in"] = 88200
    elif how == "a group not approved":
        doc["groups"][1]["status"] = "candidate"
    elif how == "a source it cites that the file does not hold":
        entry["doc_sources"] = ["signalyst-fictional-other"]
    elif how == "no engine":
        doc["engines"] = {}
    elif how == "a group figure's bound set by a list":
        doc["engines"]["9.4.1"]["identity_groups"][1]["figures"]["12288000"]["idle_db"]["value"]["by"] = ["impulse"]
    elif how == "an idle bound set by no excitation the words name":
        doc["engines"]["9.4.1"]["modulators"]["dmx7ec-a"]["rates"]["12288000"]["idle_db"]["by"] = "echo"
    elif how == "a range without its min":
        del _filter(doc, "demo-gauss-x")["contexts"]["1x-44.1k-705.6k"]["position_dependent"]["f_3db_khz"]["min"]
    return doc


BREAKS = ("another schema", "a missing key", "a wrong type deep inside", "an unread value deep inside",
          "a name of another shape", "apodizing without its log name", "a log name without apodizing",
          "a manual mark without its sources", "manual sources without a mark",
          "a group figure outside the closed list", "a group it names that the file does not hold",
          "an entry under another slug", "a rate no label names", "a group not approved",
          "a source it cites that the file does not hold", "no engine", "a group figure's bound set by a list",
          "an idle bound set by no excitation the words name", "a range without its min")


@pytest.mark.parametrize("how", BREAKS)
def test_a_file_that_breaks_the_shape_is_refused_whole_in_one_line_and_no_value(tmp_path, caplog, how):
    caplog.set_level(logging.INFO, logger="hqp_atlas")
    doc = _doc()
    doc["groups"][0]["say"] = CANARY
    assert _read(tmp_path, _break(doc, how)) is None
    lines = [r for r in caplog.records if r.name == "hqp_atlas" and r.levelno == logging.WARNING]
    assert len(lines) == 1 and "refused whole" in lines[0].getMessage()
    assert CANARY not in caplog.text and "a path" not in caplog.text
    # The same file read again says nothing more until it changes.
    assert hqp_atlas.read(tmp_path / "shipped.json", tmp_path / "overlay.json") is None
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_a_refusal_names_the_place_by_keys_that_passed_their_shape(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="hqp_atlas")
    assert _read(tmp_path, _break(_doc(), "apodizing without its log name")) is None
    assert f"apodizing without log_name, at {FILTERS}.demo-sinc-a" in caplog.text
    caplog.clear()
    assert _read(tmp_path, _break(_doc(), "a group figure outside the closed list")) is None
    assert "a quantity no group figure holds, at engines.9.4.1.identity_groups[0].figures.1x-44.1k-705.6k" \
        in caplog.text
    caplog.clear()
    assert _read(tmp_path, _break(_doc(), "a name of another shape")) is None
    assert "not text of its shape, at slugs[0].name" in caplog.text
    caplog.clear()
    assert _read(tmp_path, _break(_doc(), "a group figure's bound set by a list")) is None
    assert "not one of its values, at engines.9.4.1.identity_groups[1].figures.12288000.idle_db.value.by" \
        in caplog.text


@pytest.mark.parametrize("raw", [b"", b'{"schema": "hqp-atlas-app/v1", "releases": {', b'{"schema": NaN}',
                                 b"\xff\xfe not utf-8", b"[" * 100_000])
def test_a_truncated_or_unreadable_file_is_refused_whole(tmp_path, caplog, raw):
    caplog.set_level(logging.INFO, logger="hqp_atlas")
    (tmp_path / "overlay.json").write_bytes(raw)
    assert hqp_atlas.read(tmp_path / "shipped.json", tmp_path / "overlay.json") is None
    assert "refused whole" in caplog.text


def test_a_file_that_cannot_be_looked_at_is_refused_whole_once(tmp_path, caplog):
    # A link to itself: stat and open both fail, as on a folder not readable
    caplog.set_level(logging.INFO, logger="hqp_atlas")
    overlay = tmp_path / "overlay.json"
    overlay.symlink_to(overlay)
    assert hqp_atlas.read(tmp_path / "shipped.json", overlay) is None
    assert hqp_atlas.read(tmp_path / "shipped.json", overlay) is None
    lines = [r.getMessage() for r in caplog.records if r.name == "hqp_atlas" and r.levelno == logging.WARNING]
    assert len(lines) == 1 and "the node's own atlas copy refused whole — not readable" in lines[0]
    # The shipped path counts as a file there: the overlay is not read
    _write(tmp_path / "copy.json", _doc())
    assert hqp_atlas.read(overlay, tmp_path / "copy.json") is None


def test_a_key_not_read_is_ignored(tmp_path):
    doc = _doc()
    assert "fictional" in doc
    _filter(doc, "demo-sinc-a")["a_field_from_later"] = {"anything": [1, 2]}
    doc["pairs"][0]["contexts"]["1x-44.1k-705.6k"]["difference"]["a_new_quantity"] = [1, 2]
    assert _read(tmp_path, doc) is not None


# -- Lookups ---------------------------------------------------------------------


def test_lookup_per_kind_engine_and_mode(atlas):
    def state(kind, name, mode="pcm", engine="9.4.1"):
        return atlas.lookup(kind, name, engine, mode).state

    assert state("filter", "demo-sinc-a") == "measured"
    assert state("filter", "demo-sinc-a", "sdm") == "pcm-measurement"
    assert state("filter", "demo-2s", "sdm") == "not-measured"      # the mode not measured
    assert state("filter", "demo-gauss-x", "sdm") == "not-measured"  # a mode the entry does not list
    assert state("filter", "demo-new") == "not-measured"             # a name the registry lacks
    assert state("filter", "demo-gone") == "not-measured"            # an entry the engine lacks
    assert state("filter", "demo-sinc-a", engine=None) == "not-measured"
    assert state("modulator", "DMX5", "sdm") == "measured"
    assert state("modulator", "DMXH8", "sdm") == "refused"
    assert atlas.lookup("dither", "DEMO-TPDF", "9.4.1", "pcm") is None


def test_a_name_is_looked_up_never_turned_into_a_slug(atlas):
    found = atlas.lookup("filter", "Demo-Sinc-A", "9.4.1", "pcm")
    assert (found.state, found.slug, found.entry) == ("not-measured", None, None)
    assert atlas.slug_of("modulator", "DMX7 512+fs") == "dmx7-512fs"


def test_the_engine_shown_is_the_newest_not_newer_within_the_major(tmp_path, atlas):
    assert [atlas.engine_for(e) for e in ("9.4.1", "9.5.0", "9.0.0", "8.9.9", "10.0.0", "", None)] == \
        ["9.4.1", "9.4.1", "9.4.1", None, None, None, None]
    doc = _doc()
    doc["engines"]["9.6.0"] = copy.deepcopy(doc["engines"]["9.4.1"])
    two = _read(tmp_path, doc)
    assert two.engines == ["9.4.1", "9.6.0"]
    assert [two.engine_for(e) for e in ("9.5.0", "9.7.2", "9.6.0", "9.0.0", "8.0.0")] == \
        ["9.4.1", "9.6.0", "9.6.0", "9.4.1", None]


def test_the_engine_line(atlas):
    def note(engine):
        return plain(hqp_atlas.wire(atlas.engine_note(engine)))

    assert note("9.4.1") == "Measured on HQPlayer engine 9.4.1"
    assert note("9.5.0") == "Measured on HQPlayer engine 9.4.1 · this HQPlayer runs engine 9.5.0"
    assert note("") == "Measured on HQPlayer engine 9.4.1 · this HQPlayer does not report its engine"
    assert note("8.0.0") == "Measured on HQPlayer engine 9.4.1 · this HQPlayer runs engine 8.0.0"


def test_the_labels():
    assert hqp_atlas.context_label("1x-44.1k-705.6k") == "1x · 44.1k → 705.6k"
    assert hqp_atlas.context_label("nx-96k-768k") == "Nx · 96k → 768k"
    assert hqp_atlas.rate_label("5644800", Num("44100")) == "DSD128 · from 44.1k"
    assert hqp_atlas.rate_label("12288000", Num("48000")) == "DSD256 · from 48k"
    assert hqp_atlas.rate_short("5644800", Num("44100")) == "DSD128"
    assert hqp_atlas.rate_short("24576000", Num("48000")) == "DSD512 (48k)"


def test_a_rate_is_named_as_the_hqplayer_screen_names_it():
    assert [hqp_atlas.rate_name(hz) for hz in (0, 44100, 48000, 352800, 705600, 768000)] == \
        ["—", "44.1 kHz", "48 kHz", "352.8 kHz", "705.6 kHz", "768 kHz"]
    assert [hqp_atlas.rate_name(hz) for hz in (2822400, 11289600, 12288000, 24576000, 2048000, 1411200)] == \
        ["DSD64", "DSD256", "DSD256 (48k)", "DSD512 (48k)", "DSD64 (32k)", "DSD64 (22.05k)"]
    # above 768 kHz and no power-of-two multiple of a base from 64 up
    assert [hqp_atlas.rate_name(hz) for hz in (1536000, 3000000)] == ["1.54 MHz", "3 MHz"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_node_names_every_rate_as_the_screen_does():
    # app-shell.js fmtRateLabel run as written, on every rate of the four
    # families and a few no family holds
    src = (BACKEND / "static" / "app-shell.js").read_text(encoding="utf-8")
    fn = re.search(r"\n  function fmtRateLabel\(hz\) \{\n.*?\n  \}\n", src, re.S).group(0)
    rates = [base * 2 ** k for base in (44100, 48000, 32000, 22050) for k in range(12)]
    rates += [0, 8000, 11025, 1536000, 3000000]
    script = f"{fn}\nconsole.log(JSON.stringify({json.dumps(rates)}.map(fmtRateLabel)));"
    out = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == [hqp_atlas.rate_name(hz) for hz in rates]


# -- HQPlayer's own facts --------------------------------------------------------


def test_without_an_atlas_the_rows_say_what_hqplayer_says():
    f = _facts_of(None, "filter", PCM + [("demo-odd", "a shape not read"), ("demo-old", "")], "9.4.1", "pcm")
    a = f["demo-sinc-a"]
    assert _chips(a) == ["2^x up"] and a["chips"][0]["title"] == "Ratio class, from HQPlayer's description"
    assert a["words"] == {"text": "transients, timbre, space",
                          "title": "The author's focus, from HQPlayer's description"}
    assert a["tag"] == {"text": "Signalyst 4/5",
                        "title": "Signalyst's technical rating 4/5 — relative to the other filters; "
                                 "not a listening score"}
    assert (a["caption"], a["line3"], a["bracket"], a["card"]) == (None, None, None, True)
    assert f["demo-poly-mp"]["words"]["text"] == "space, transients"
    assert f["demo-new"]["words"] is None and _chips(f["demo-new"]) == ["1:1"]
    odd = f["demo-odd"]
    assert (odd["chips"], odd["tag"], plain(odd["caption"]), odd["card"]) == ([], None, "a shape not read", False)
    assert f["demo-old"] == hqp_atlas.runtime("filter", {"name": "demo-old", "description": "", "parsed": None})
    assert f["demo-old"]["chips"] == [] and f["demo-old"]["card"] is False

    m = _facts_of(None, "modulator", MODULATORS, "9.4.1", "sdm")["DMX7EC-a"]
    assert _chips(m) == ["Gen9"] and m["chips"][0]["title"] == "HQPlayer's generation field"
    assert m["card"] is False
    assert hqp_atlas.head(None, "9.4.1", "pcm") is None
    card = _card(None, "filter", "demo-sinc-a", PCM)
    assert card["origin"] is None and card["status"] == [] and [s["head"] for s in card["sections"]] == \
        ["What HQPlayer says"]
    assert _card_text(card)[1:] == [
        "## What HQPlayer says",
        "Signalyst's technical rating: 4/5 — relative to the other filters; not a listening score",
        "The author's focus: transients, timbre, space", "Ratio class: 2^x up",
        "From HQPlayer's description “4/5 transients, timbre, space ⥮ 2^x up”."]


# -- The rows with an atlas ------------------------------------------------------


def test_a_measured_filter_row(atlas):
    f = _facts_of(atlas, "filter", PCM, "9.4.1", "pcm")
    a = f["demo-sinc-a"]
    # The ring's sides read alike as text; the twin's figures, not its own reading.
    assert _chips(a) == ["linear phase", "±12.346 ms", "2^x up"]
    assert [c["title"] for c in a["chips"]] == [
        "Measured by Sautium, 1x · 44.1k → 705.6k",
        "Impulse before and after its peak at −100 dB, 1x · 44.1k → 705.6k",
        "Ratio class, from HQPlayer's description"]
    assert a["chips"][1]["parts"] == [{"t": "±"}, {"n": "12.346"}, {"t": " ms"}]
    assert a["tag"]["text"] == "Signalyst 4/5" and a["words"] is None and a["card"] is True
    assert plain(a["caption"]) == "Apod Y per the manual"
    assert plain(a["line3"]) == "Same output as demo-sinc-b (within one step)"
    assert f["demo-sinc-b"]["caption"] is None  # the manual's N says nothing on the row
    assert _chips(f["demo-poly-mp"]) == ["minimum phase", "0.0 ms before · 0.300 ms after", "Any"]
    assert _chips(f["demo-gauss-x"]) == ["intermediate phase", "1.5 ms before · 2.5 ms after", "position-dependent",
                                         "Any"]
    assert f["demo-gauss-x"]["chips"][2]["title"] == \
        "The readings move with the impulse's position, 1x · 44.1k → 705.6k"
    assert plain(f["demo-gauss-xa"]["caption"]) == "apodizing by HQPlayer's label · Apod Y per the manual"
    assert f["demo-poly-lp"]["line3"] is None  # one magnitude: in the card, never on the row


def test_a_name_the_atlas_does_not_measure_says_so_on_the_atlas_engine(atlas):
    f = _facts_of(atlas, "filter", PCM, "9.5.0", "pcm")
    for name in ("demo-gone", "demo-new"):
        assert _chips(f[name])[-1] == "not measured on engine 9.4.1"
        muted = f[name]["chips"][-1]
        assert muted["muted"] is True and muted["title"] == "Listed by HQPlayer, not measured on engine 9.4.1"
        assert f[name]["line3"] is None and f[name]["card"] is True and f[name]["tag"] is not None
    assert _chips(f["demo-new"]) == ["1:1", "not measured on engine 9.4.1"]


def test_another_major_engine_shows_hqplayers_facts_and_not_measured_on_every_row(atlas):
    for engine in ("8.0.0", "10.1.0", ""):
        for kind, spec, mode in (("filter", PCM, "pcm"), ("modulator", MODULATORS, "sdm")):
            for f in _facts_of(atlas, kind, spec, engine, mode).values():
                assert _chips(f)[-1] == "not measured on engine 9.4.1"
                assert f["line3"] is None and f["bracket"] is None
        head = hqp_atlas.head(atlas, engine, "sdm")
        assert len(head["filters"]) == 1
    card = _card(atlas, "filter", "demo-sinc-a", PCM, "8.0.0")
    assert [plain(s) for s in card["status"]] == [
        "Measured on HQPlayer engine 9.4.1 · this HQPlayer runs engine 8.0.0",
        "Not measured on HQPlayer engine 9.4.1."]
    assert [s["head"] for s in card["sections"]] == ["What HQPlayer says"] and card["link"] is None


def test_in_sdm_mode_the_rows_show_their_pcm_facts_and_the_head_says_so(atlas):
    f = _facts_of(atlas, "filter", SDM, "9.4.1", "sdm")
    assert _chips(f["demo-sinc-a"]) == ["linear phase", "±12.346 ms", "2^x"]
    assert _chips(f["demo-2s"]) == ["Any", "not measured on engine 9.4.1"]
    assert _chips(f["demo-gauss-x"]) == ["Any", "not measured on engine 9.4.1"]
    assert [plain(x) for x in hqp_atlas.head(atlas, "9.4.1", "sdm")["filters"]] == [
        "Measured on HQPlayer engine 9.4.1", "Properties at 1x · 44.1k → 705.6k",
        "In SDM mode: the properties each filter showed in PCM mode."]
    assert [plain(x) for x in hqp_atlas.head(atlas, "9.4.1", "pcm")["filters"]] == [
        "Measured on HQPlayer engine 9.4.1", "Properties at 1x · 44.1k → 705.6k"]
    card = _card(atlas, "filter", "demo-sinc-a", SDM, mode="sdm")
    assert [plain(s) for s in card["status"]] == [
        "Measured on HQPlayer engine 9.4.1",
        "In SDM mode HQPlayer runs a filter to an intermediate rate before its modulator. That path was not "
        "measured on its own: the properties are what each filter did in PCM mode.",
        "In SDM mode HQPlayer lists it as 2^x; the facts here are measured in PCM mode."]
    # A filter list in no known mode is HQPlayer's alone.
    assert hqp_atlas.facts(atlas, "filter", _rows(PCM, "filter"), "9.4.1", None) == \
        [hqp_atlas.runtime("filter", row) for row in _rows(PCM, "filter")]


def test_the_head_names_the_chips_context_only_while_every_row_shares_it(tmp_path):
    doc = _doc()
    contexts = _filter(doc, "demo-gauss-xa")["contexts"]
    contexts["nx-88.2k-705.6k"] = contexts.pop("1x-44.1k-705.6k")
    contexts["1x-44.1k-705.6k"] = {"void": ["analysis failed"]}
    atlas = _read(tmp_path, doc)
    assert [plain(x) for x in hqp_atlas.head(atlas, "9.4.1", "pcm")["filters"]] == \
        ["Measured on HQPlayer engine 9.4.1"]
    xa = _facts_of(atlas, "filter", PCM, "9.4.1", "pcm")["demo-gauss-xa"]
    assert xa["chips"][0]["title"] == "Measured by Sautium, Nx · 88.2k → 705.6k"


def test_a_modulator_row(atlas):
    f = _facts_of(atlas, "modulator", MODULATORS, "9.4.1", "sdm")
    assert _chips(f["DMX5"]) == ["Gen1", "fifth order", "refused at DSD256"]
    assert f["DMX5"]["chips"][1]["title"] == "Order, read off the name"
    assert f["DMX5"]["chips"][2]["title"] == "HQPlayer refused it at DSD256 · from 44.1k on engine 9.4.1"
    assert _chips(f["DMX7 512+fs"]) == ["Gen5", "seventh order", "refused at DSD128 and DSD256 (48k)"]
    assert _chips(f["DMXH8"]) == ["Gen8", "fifth order", "refused at every measured rate"]
    assert f["DMXH8"]["chips"][2]["title"] == \
        "HQPlayer refused it at every measured rate: DSD128 and DSD256 (48k)."
    assert _chips(f["DMX-new"]) == ["Gen2", "not measured on engine 9.4.1"]
    assert all(f[name]["card"] for name in f)
    assert not any("tuned" in chip for name in f for chip in _chips(f[name]))


def test_an_identical_group_of_three_draws_one_bracket_in_hqplayers_order(atlas):
    f = _facts_of(atlas, "modulator", MODULATORS, "9.4.1", "sdm")
    brackets = [f[name]["bracket"] for name in ("DMX7EC-a", "DMX7EC-b", "DMX7EC-c")]
    assert brackets[0] == brackets[1] == brackets[2]
    # Keyed by the first member HQPlayer lists — an index, never the group's id.
    assert brackets[0]["key"] == 11
    assert [plain(line) for line in brackets[0]["head"]] == [
        "DMX7EC: -c, -a, -b — three fictional names alike in every figure of this file.",
        "Choose by the fictional computer's spare time."]
    assert f["DMX5"]["bracket"] is None and f["DMX7EC-a"]["line3"] is None


@pytest.mark.parametrize("relation, line", [
    ("bit_identical", "Same output as demo-sinc-b (bit for bit)"),
    ("within_1_lsb", "Same output as demo-sinc-b (within one step)"),
    ("same_figures_within_precision", "Same figures as demo-sinc-b (within the measurement's precision)")])
def test_a_twins_line_in_its_relations_words(tmp_path, relation, line):
    doc = _doc()
    doc["groups"][0]["relation"] = relation
    doc["engines"]["9.4.1"]["identity_groups"][0]["relation"] = relation
    atlas = _read(tmp_path, doc)
    assert plain(_facts_of(atlas, "filter", PCM, "9.4.1", "pcm")["demo-sinc-a"]["line3"]) == line


def test_a_relation_without_words_shows_nothing_and_is_logged_by_its_name(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="hqp_atlas")
    doc = _doc()
    doc["groups"][1]["relation"] = "not_compared"
    doc["groups"][1]["say"] = CANARY
    atlas = _read(tmp_path, doc)
    text = _card_text(_card(atlas, "filter", "demo-poly-mp", PCM))
    assert not any(CANARY in line or "Same magnitude" in line for line in text)
    _card(atlas, "filter", "demo-poly-lp", PCM)
    assert caplog.text.count("no words for the relation not_compared") == 1
    assert CANARY not in caplog.text


# -- The card --------------------------------------------------------------------


def test_a_filters_card(atlas):
    card = _card(atlas, "filter", "demo-sinc-a", PCM, "9.5.0")
    assert (card["title"], card["origin"], card["link"]) == ("demo-sinc-a", "overlay", None)
    assert _card_text(card) == [
        "demo-sinc-a",
        "Measured on HQPlayer engine 9.4.1 · this HQPlayer runs engine 9.5.0",
        "## What HQPlayer says",
        "Signalyst's technical rating: 4/5 — relative to the other filters; not a listening score",
        "The author's focus: transients, timbre, space",
        "Ratio class: 2^x up",
        "From HQPlayer's description “4/5 transients, timbre, space ⥮ 2^x up”.",
        "## Apodizing",
        "Apodizing, HQPlayer's label: not apodizing — its log names the design “Demo sinc A (fictional)”",
        "Apod, per the manual (§9.9): Y",
        "## The manual",
        "The author's genre suggestion, per the manual (§9.9): classical, any, depending on length — Sautium "
        "shows it and never applies it",
        "A variant of, per the manual (§9.9): demo-gauss-x",
        "## None",
        "Same output as demo-sinc-b — the same output within one step, in this fictional file.",
        "Across rate families: demo-sinc-a: plays; demo-sinc-b: refused.",
        "Choose by the fictional listener's own ears.",
        "A fictional note on how the two were compared.",
        "## Properties",
        " | 1x · 44.1k → 705.6k",
        "Phase | linear",
        "−3 dB, kHz | 17.532",
        "100 dB down from here to the output Nyquist, kHz | 18.532",
        "Attenuation at the source Nyquist, dB | ≥ 77.37",
        "Stopband minimum, dB | ≥ 87.73",
        "Ringing before the main lobe, above −100 dB, ms | 12.346",
        "Ringing after the main lobe, above −100 dB, ms | 12.346",
        "Length above −100 dB, ms | 24.692",
        "Filter delay, ms | 6.0 ± 20",
        "Time-invariant | yes",
        "≥ at least, ≤ at most: the reading lies within reach of the measurement's noise, so it is a bound.",
        "The other contexts",
        " | Nx · 88.2k → 705.6k",
        "Phase | linear",
        "−3 dB, kHz | 17.632",
        "100 dB down from here to the output Nyquist, kHz | 18.632",
        "Attenuation at the source Nyquist, dB | 71.73",
        "Stopband minimum, dB | ≥ 85.37",
        "Ringing before the main lobe, above −100 dB, ms | 6.13",
        "Ringing after the main lobe, above −100 dB, ms | 6.13",
        "Length above −100 dB, ms | 12.26",
        "Filter delay, ms | 3.0 ± 20",
        "Time-invariant | yes",
        "For a member of demo-sinc-a and demo-sinc-b, each number is the group's figure where it holds one — the "
        "members' median, a bound where any member's reading is one; the filter delay is the member's own.",
        "## Differs only in",
        "Length — from demo-gauss-x.",
        "At 1x · 44.1k → 705.6k, demo-sinc-a / demo-gauss-x: impulse length above −100 dB: 24.69 / 4.0 ms; "
        "transition: 111 / 2222 Hz. Also differs: filter delay: 6.0 / 0.300 ms.",
        "## The author's stated purpose",
        "A fictional line on what the author meant this filter for.",
        "In Sautium's words, from Jussi Laako, 2001-02-03.",
        "## History",
        "Introduced in HQPlayer Desktop 9.0.0 — Jussi Laako, 2001-02-03.",
        "Only what the sources state; a missing event means not stated in the sources read.",
        "## Sources",
        "Measured by Sautium on HQPlayer engine 9.4.1.",
        "Fictional Author, Fictional Forum › Fictional thread #1, 2001-02-03. “A fictional sentence, quoted only "
        "by Sautium's tests.”",
        "Fictional Vendor, Fictional User Manual 0.0.0, §9.9 Fictional filter table, 2001-01-01."]


def test_a_time_variant_filters_card_reads_its_ranges_bounds_and_gaps(atlas):
    text = _card_text(_card(atlas, "filter", "demo-gauss-x", PCM))
    assert "Apod, per the manual: the manual's table has no row for it" in text
    assert "−3 dB, kHz | 15.5–16.0 (median 15.75)" in text
    assert "Attenuation at the source Nyquist, dB | 79.37" in text  # one value where min and max are one
    assert "100 dB down from here to the output Nyquist, kHz | —" in text
    assert "Stopband minimum, dB | ≥ 81.37ˣ" in text
    assert "Time-invariant | no" in text
    assert text[text.index("Time-invariant | no") + 1:text.index("## Differs only in")] == [
        "≥ at least, ≤ at most: the reading lies within reach of the measurement's noise, so it is a bound.",
        "ˣ the impulse and the sweep disagreed; the larger bound stands.",
        "a range: the reading moves with the impulse's position.",
        "— none: the method defines no reading here."]


def test_pairs_in_the_card_never_carry_the_measuring_hosts_speed_or_a_note(atlas):
    text = _card_text(_card(atlas, "filter", "demo-gauss-xa", PCM))
    assert text[text.index("## Differs only in") + 1:text.index("## Sources")] == [
        "Apodizing — from demo-gauss-x.",
        "At 1x · 44.1k → 705.6k, demo-gauss-xa / demo-gauss-x: HQPlayer's label: apodizing / not apodizing; "
        "−3 dB: 14.5 / 15.75 kHz. Also differs: ratio class: 2^x up / Any.",
        "Not measured: the window. From the manual (§9.9)."]
    text = _card_text(_card(atlas, "filter", "demo-poly-mp", PCM))
    assert "At 1x · 44.1k → 705.6k, demo-poly-mp / demo-poly-lp: phase: minimum / linear; group delay at 20 kHz: " \
           "— / 12.345 µs. Also differs: filter delay: −0.5 / 6.0 ms." in text
    assert "Same magnitude as demo-poly-lp — one magnitude with two phases, in this fictional file." in text
    text = _card_text(_card(atlas, "modulator", "DMX5", MODULATORS, mode="sdm"))
    assert text[text.index("## Differs only in") + 1:text.index("## Sources")] == [
        "Order — from DMX7EC-a.", "From Jussi Laako, 2001-02-03."]
    every = json.dumps(_everything(atlas), ensure_ascii=False)
    for token in ("987.65", "876.54", "98.765", "9.8765", "process_speed", "not comparable", "card never shows"):
        assert token not in every


def test_an_axis_or_a_difference_without_words_shows_nothing_of_it(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="hqp_atlas")
    doc = _doc()
    doc["pairs"][0]["axis"] = "depth"
    doc["pairs"][1]["contexts"]["1x-44.1k-705.6k"]["difference"]["ripple_db"] = [0.5, 0.25]
    atlas = _read(tmp_path, doc)
    text = _card_text(_card(atlas, "filter", "demo-poly-mp", PCM))
    assert "## Differs only in" not in text
    text = _card_text(_card(atlas, "filter", "demo-gauss-x", PCM))
    assert "Apodizing — from demo-gauss-xa." in text and "Not measured: the window. From the manual (§9.9)." in text
    assert not any(line.startswith("At 1x · 44.1k → 705.6k, demo-gauss-x / demo-gauss-xa") for line in text)
    assert "no words for the axis depth" in caplog.text and "no words for the difference ripple_db" in caplog.text


def test_a_modulators_card(atlas):
    text = _card_text(_card(atlas, "modulator", "DMX7EC-a", MODULATORS, mode="sdm"))
    assert text == [
        "DMX7EC-a",
        "Measured on HQPlayer engine 9.4.1",
        "## What HQPlayer says; the structure",
        "HQPlayer's generation field: Gen9",
        "Structure, read off the name: seventh order, adaptive, with EC",
        "From HQPlayer's description “Gen9”.",
        "## None",
        "Same figures as DMX7EC-c, DMX7EC-b — three fictional names alike in every figure of this file.",
        "Choose by the fictional computer's spare time.",
        "A fictional note on how the three were compared.",
        "## Per rate",
        "Rate | Idle noise, 20 Hz–20 kHz, dB",
        "DSD128 · from 44.1k | −91.5",
        "DSD256 · from 48k | ≤ −97.5",
        "≥ at least, ≤ at most: the reading lies within reach of the measurement's noise, so it is a bound.",
        "For a member of DMX7EC: -c, -a, -b, the idle noise is the group's figure where it holds one — the "
        "members' median, a bound where any member's reading is one.",
        "## Near full scale",
        "The highest level stable on both channels at the modulator's input.",
        "Rate | HQPlayer volume 0 dB | HQPlayer volume −3 dB",
        "DSD128 · from 44.1k | −0.5 dBFS · tone; −6 dBFS · pair | −3.5 dBFS · tone; none of the tested levels",
        "DSD256 · from 48k | −0.5 dBFS · tone | not tested",
        "Tone: one sine; pair: two tones together. Not tested: no play at that volume.",
        "## Differs only in",
        "Order — from DMX5.",
        "From Jussi Laako, 2001-02-03.",
        "## History",
        "Introduced in HQPlayer Desktop 9.1.0 — Jussi Laako, 2001-02-03.",
        "Only what the sources state; a missing event means not stated in the sources read.",
        "## Sources",
        "Measured by Sautium on HQPlayer engine 9.4.1.",
        "Measured with demo-sinc-a in both filter slots.",
        "Fictional Author, Fictional Forum › Fictional thread #1, 2001-02-03. “A fictional sentence, quoted only "
        "by Sautium's tests.”"]
    # The highest stable tone and pair stay out: near full scale stands alone.
    assert "-11" not in json.dumps(_card(atlas, "modulator", "DMX7EC-a", MODULATORS, mode="sdm"))


def test_a_modulator_refused_at_every_rate(atlas):
    text = _card_text(_card(atlas, "modulator", "DMXH8", MODULATORS, mode="sdm"))
    assert text == [
        "DMXH8",
        "Measured on HQPlayer engine 9.4.1",
        "## What HQPlayer says; the structure",
        "HQPlayer's generation field: Gen8",
        "Structure, read off the name: fifth order, adaptive, with EC, multibit (8B)",
        "From HQPlayer's description “Gen8”.",
        "## Rates HQPlayer refused",
        "HQPlayer refused it at DSD128 and DSD256 (48k).",
        "DSD128 · from 44.1k: “Requested filter not possible with this rate combination 44100/5644800, stop”",
        "DSD256 · from 48k: “Requested filter not possible with this rate combination 48000/12288000, stop”",
        "## Sources",
        "Measured by Sautium on HQPlayer engine 9.4.1.",
        "Measured with demo-sinc-a in both filter slots."]
    assert "Structure, read off the name: seventh order, adaptive, without EC, tuned 512+fs" in \
        _card_text(_card(atlas, "modulator", "DMX7 512+fs", MODULATORS, mode="sdm"))
    assert "Structure, read off the name: fifth order, fixed, without EC" in \
        _card_text(_card(atlas, "modulator", "DMX5", MODULATORS, mode="sdm"))


def test_a_card_for_a_name_hqplayer_lists_and_the_atlas_does_not_measure(atlas):
    card = _card(atlas, "filter", "demo-new", PCM)
    assert [plain(s) for s in card["status"]] == ["Measured on HQPlayer engine 9.4.1",
                                                  "Not measured on HQPlayer engine 9.4.1."]
    assert [s["head"] for s in card["sections"]] == ["What HQPlayer says"] and card["link"] is None
    card = _card(atlas, "modulator", "DMX-new", MODULATORS, mode="sdm")
    assert [s["head"] for s in card["sections"]] == ["What HQPlayer says"]
    assert _card(atlas, "filter", "demo-2s", PCM) is None  # a name the list does not hold now


def test_numbers_reach_the_screen_as_the_file_writes_them(atlas):
    f = _facts_of(atlas, "filter", PCM, "9.4.1", "pcm")
    assert f["demo-poly-lp"]["chips"][1]["parts"] == [{"t": "±"}, {"n": "0.300"}, {"t": " ms"}]
    card = _card(atlas, "filter", "demo-poly-mp", PCM)
    table = next(s for s in card["sections"] if s["head"] == "Properties")["items"][0]["table"]
    delay = next(row for row in table["rows"] if row["label"] == "Filter delay, ms")
    assert delay["cells"] == [[{"n": "-0.5"}, {"t": " ± "}, {"n": "20"}]]
    every = json.dumps(_everything(atlas), ensure_ascii=False)
    for token in ('{"n": "0.300"}', '{"n": "6.0"}', '{"n": "-0.5"}', '{"n": "12.346"}', '{"n": "-91.5"}',
                  '{"n": "20"}'):
        assert token in every
    assert plain([{"t": "Filter delay "}, {"n": "-0.5"}]) == "Filter delay −0.5"


def test_a_source_not_reread_in_full_is_never_quoted_or_cited(tmp_path, atlas):
    every = json.dumps(_everything(atlas), ensure_ascii=False)
    assert "never re-read in full" not in every and "Fictional Poster" not in every
    doc = _doc()
    sinc = _filter(doc, "demo-sinc-a")
    sinc["intent"][0]["sources"] = ["fictional-forum-snippet"]
    sinc["history"]["introduced"]["sources"] = ["fictional-forum-snippet"]
    text = _card_text(_card(_read(tmp_path, doc), "filter", "demo-sinc-a", PCM))
    assert "## The author's stated purpose" not in text and "## History" not in text
    assert not any("Fictional Author" in line for line in text)


def test_a_link_waits_for_a_dated_release(tmp_path, atlas):
    assert "sautium.net/hqplayer" not in json.dumps(_everything(atlas))
    doc = _doc()
    doc["releases"]["9.4.1/pcm"] = "2001-05-06"
    dated = _read(tmp_path, doc)
    assert _card(dated, "filter", "demo-sinc-a", PCM)["link"] == {
        "href": "https://sautium.net/hqplayer/filters/demo-sinc-a/", "text": "Measured properties ↗"}
    assert _card(dated, "filter", "demo-new", PCM)["link"] is None
    assert _card(dated, "modulator", "DMX5", MODULATORS, mode="sdm")["link"] is None
    hrefs = {c["link"]["href"] for c in _everything(dated) if isinstance(c, dict) and c.get("link")}
    assert hrefs == {f"https://sautium.net/hqplayer/filters/{slug}/" for slug in
                     ("demo-sinc-a", "demo-sinc-b", "demo-poly-lp", "demo-poly-mp", "demo-gauss-x", "demo-gauss-xa")}
    doc["releases"] = {"9.4.1/pcm": None, "9.4.1/sdm": "2001-05-06"}
    dated = _read(tmp_path, doc)
    assert _card(dated, "modulator", "DMX5", MODULATORS, mode="sdm")["link"]["href"] == \
        "https://sautium.net/hqplayer/modulators/dmx5/"
    assert _card(dated, "modulator", "DMXH8", MODULATORS, mode="sdm")["link"] is None  # refused: no page
    assert _card(dated, "filter", "demo-2s", SDM, mode="sdm")["link"] is None
    assert _card(dated, "filter", "demo-sinc-a", SDM, mode="sdm")["link"] is None  # its release is PCM's


def test_where_the_atlas_was_read_from_travels_with_every_result(tmp_path, atlas):
    found = atlas.lookup("filter", "demo-sinc-a", "9.4.1", "pcm")
    assert (atlas.origin, found.origin) == ("overlay", "overlay")
    assert hqp_atlas.head(atlas, "9.4.1", "pcm")["origin"] == "overlay"
    assert _card(atlas, "filter", "demo-new", PCM)["origin"] == "overlay"
    shipped = tmp_path / "shipped.json"
    shipped.write_bytes(FIXTURE.read_bytes())
    assert hqp_atlas.read(shipped, None).lookup("modulator", "DMX5", "9.4.1", "sdm").origin == "shipped"


# -- The words -------------------------------------------------------------------


def _constants():
    for name, value in vars(hqp_atlas).items():
        if name.isupper():
            yield from (s for s in _strings(value) if isinstance(s, str))


def test_no_word_ranks_and_none_speaks_of_an_engine_stopping(tmp_path, atlas):
    doc = _doc()
    doc["releases"] = {"9.4.1/pcm": "2001-05-06", "9.4.1/sdm": "2001-05-06"}
    said = list(_constants()) + list(FRAME)
    for results in (_everything(atlas), _everything(_read(tmp_path, doc)), _everything(None, engines=("9.4.1",))):
        said += list(_strings(results))
    hits = sorted({m.group(0) for text in said for m in BANNED.finditer(text)})
    assert hits == []
    assert any("not a listening score" in text for text in said)
