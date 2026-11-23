"""Rules of the Web UI that one stylesheet block or one function carries, pinned
as their words (the look itself is checked in a headless browser at 360 and
390 px, in the slice's record): a press selects nothing of the app's chrome
(BACKLOG B-90), the HQPlayer picker's row is two lines (B-88), the AI
canonization list never cuts a name (B-86), and a toast's "HQPlayer settings"
opens the HQPlayer screen at one picker (B-89). The functions run under Node
where it is installed."""

import json
import re
import shutil
import subprocess

import pytest

from tests.conftest import REPO_ROOT

NODE = shutil.which("node")
STATIC = REPO_ROOT / "backend" / "static"


def _css() -> str:
    """The stylesheet without its comments."""
    return re.sub(r"/\*.*?\*/", "", (STATIC / "style.css").read_text(encoding="utf-8"), flags=re.S)


def _shell() -> str:
    return (STATIC / "app-shell.js").read_text(encoding="utf-8")


def _rule(css: str, selector: str) -> dict:
    """The declarations of the first rule whose selector is exactly `selector`."""
    body = re.search(r"(?:^|[{};])\s*" + re.escape(selector) + r"\s*\{([^{}]*)\}", css, re.S).group(1)
    return {k.strip(): v.strip() for k, v in (d.split(":", 1) for d in body.split(";") if ":" in d)}


def test_a_press_selects_nothing_that_takes_a_tap():
    """B-90: on a phone a press on an Output row selected its text and raised
    the copy menu. Everything that takes a tap — the press feedback's own
    list, so a control that joins it dips and selects nothing — is
    unselectable everywhere and calls up no touch menu; no double-tap zooms
    (touch-action composes down from the document)."""
    css = _css()
    taps = re.search(r":where\(button, a\[href\], select, summary,([^)]*)\)\s*\{(.*?)\n\}", css, re.S)
    selectors = {s.strip() for s in ("button, a[href], select, summary," + taps.group(1)).split(",")}
    assert {"button", "a[href]", "select", "summary", '[role="button"]', ".track-row", ".q-row", ".friend-row",
            ".form-row.is-clickable", ".add-gear-result", ".artist-tile", ".album-tile"} <= selectors
    body = taps.group(2)
    for declaration in ("user-select: none;", "-webkit-user-select: none;", "-webkit-touch-callout: none;",
                        "filter: var(--own-filter, none);"):
        assert declaration in body, declaration
    assert "html, body { touch-action: manipulation; }" in css


def test_on_a_touch_screen_only_prose_and_fields_select():
    """...and on a touch screen the whole app is chrome: the body selects
    nothing, and what is there to be read or typed opts back in — a Why?
    sheet and every dialog, the assistant's answers, a chat's messages, a
    bio, HQPlayer's log, every field. A mouse keeps its selection."""
    css = _css()
    block = re.search(r"@media \(pointer: coarse\) \{\n(.*?)\n\}\n", css, re.S).group(1)
    assert _rule(block, "body") == {"user-select": "none", "-webkit-user-select": "none",
                                    "-webkit-touch-callout": "none"}
    prose = re.search(r":where\(([^)]*)\)\s*\{([^}]*)\}", block, re.S)
    selects = {s.strip() for s in prose.group(1).split(",")}
    assert {"input", "textarea", "[contenteditable]", ".confirm-sheet", ".ai-msg-prose", ".bubble", ".bio",
            ".hqp-log"} <= selects
    assert "user-select: text;" in prose.group(2) and "-webkit-user-select: text;" in prose.group(2)
    # a link inside that prose selects with it, on a desktop too: the rule after the tap list (which makes
    # every link a control) names the same prose
    links = re.search(r":where\(([^)]*)\) a\[href\]\s*\{([^}]*)\}", css, re.S)
    assert {s.strip() for s in links.group(1).split(",")} == selects - {"input", "textarea", "[contenteditable]"}
    assert {k.strip(): v.strip() for k, v in (d.split(":", 1) for d in links.group(2).split(";") if ":" in d)} == {
        "user-select": "text", "-webkit-user-select": "text"}
    assert css.index(links.group(0)) > css.index(":where(button, a[href], select, summary,")
    # outside that block the body is left alone: a desktop selects what it could before
    assert "user-select" not in _rule(css.replace(block, ""), "body")
    # no control is given its selection back by a rule of its own
    for m in re.finditer(r"([^{}]+)\{[^{}]*user-select:\s*(text|auto|all)[^{}]*\}",
                         css.replace(block, "").replace(links.group(0), "")):
        assert not re.search(r"(^|[\s,>])(button|a)\b|-row\b|-btn\b", m.group(1)), m.group(1).strip()


def test_what_is_copied_has_a_button_and_one_handler():
    """...and what a person may need to copy carries a button: the page's one
    copy handler ([data-copy-text], [data-copy-from]) with the
    selection-based fallback the plain-HTTP origin needs, appended inside an
    open dialog (everything outside a modal dialog is inert). Its attributes
    are its own: the copy buttons the page had before (`data-copy`,
    `data-copy-code`, `data-share-copy`, each with its own listener) are not
    its to answer — it said "Could not copy" over a copy that worked."""
    shell = _shell()
    assert shell.count("e.target.closest('[data-copy-text], [data-copy-from]')") == 1
    assert "closest('[data-copy]," not in shell and "btn.dataset.copy " not in shell
    copy = shell[shell.index("  async function copyText(text) {"):shell.index("  window.copyText = copyText;")]
    assert "document.execCommand('copy')" in copy and "dialog[open]" in copy
    # an attempt's details and HQPlayer's log (support asks for them), a sign-in's one-time code
    assert shell.count("""copyButtonHtml('data-copy-from=".hqp-diag"',""") == 2
    assert 'copyButtonHtml(`data-copy-text="${escapeProfileHtml(sg.code)}"`' in shell


def test_the_pickers_row_is_two_lines():
    """B-88: a filter's row was three lines on a phone, the third holding ⓘ
    alone. The pick button is the whole row; ⓘ and the star lie over the end
    of its first line (ⓘ first), each a 44 design-px target; the name's line
    keeps their width clear and wraps — never cut; the facts take the second
    line across the row. The modulator's picker is the same row."""
    css, shell = _css(), _shell()
    name, side = _rule(css, ".hqp-sheet-row-name"), _rule(css, ".hqp-sheet-side")
    assert (name["grid-column"], name["grid-row"]) == ("1 / -1", "1")
    assert (side["grid-column"], side["grid-row"]) == ("2", "1") and "flex-direction" not in side
    for button in (".hqp-sheet-star", ".hqp-sheet-info"):
        rule = _rule(css, button)
        assert rule["width"] == rule["height"] == "calc(44 * var(--px))", button
    head, title = _rule(css, ".hqp-sheet-row-head"), _rule(css, ".hqp-sheet-row-title")
    assert "var(--side)" in head["padding-right"] and title["overflow-wrap"] == "anywhere"
    assert _rule(css, '.hqp-sheet-row[data-side="2"]') == {"--side": "calc(88 * var(--px))"}
    for rule in (name, head, title):
        assert "white-space" not in rule and "text-overflow" not in rule
    assert ".is-starless" not in css and "is-starless" not in shell
    row = shell[shell.index("    const rowHtml = (r, bracketed) => {"):shell.index("    function paint() {")]
    assert 0 < row.index('class="hqp-sheet-info"') < row.index('class="hqp-sheet-star')
    assert "data-side=\"${(info ? 1 : 0) + (filter ? 1 : 0)}\"" in row


def test_the_canonization_list_cuts_no_name():
    """B-86: the names were cut (with an ellipsis) and a row with a long
    reason showed none at all — the reason, set not to wrap, took the line
    and the names shrank to nothing. A row is a column: the names wrap, what
    happened and the model's reason have lines of their own."""
    css = _css()
    row = _rule(css, ".canon-log-row")
    assert row["flex-direction"] == "column" and row["min-height"] == "calc(44 * var(--px))"
    assert _rule(css, ".canon-names")["flex-wrap"] == "wrap"
    canon = css[css.index(".form-group.canon-log"):css.index(".toast-inline {")]
    assert "nowrap" not in canon and "ellipsis" not in canon and "overflow: hidden" not in canon
    assert _rule(css, ".canon-log-row .from,\n.canon-log-row .to")["overflow-wrap"] == "anywhere"


_CANON = r"""
const fs = require('node:fs'), vm = require('node:vm');
const shell = fs.readFileSync(process.argv[process.argv.length - 1], 'utf8');
const a = shell.indexOf('  let _aiCanonShown = [];'), b = shell.indexOf('  // AI canonization tier — toggle');
if (a < 0 || b < 0) throw new Error('the canonization list was not found');
const opened = [];
const escape = s => String(s).replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);
const ctx = { escapeProfileHtml: escape,
              openModal: html => { opened.push(html.replace(/\s+/g, ' '));
                                   return { el: { querySelector: () => ({ addEventListener() {}, focus() {} }) },
                                            close() {} }; } };
vm.runInNewContext(shell.slice(a, b) + '\nthis.row = canonRowHtml; this.details = openCanonDetails;', ctx);
const items = JSON.parse(fs.readFileSync(0, 'utf8'));
const rows = items.map((it, i) => ctx.row(it, i).replace(/\s+/g, ' '));
items.forEach(it => ctx.details(it));
process.stdout.write(JSON.stringify({ rows, opened }));
"""

CANDS = [{"gid": "g-1", "name": "Northlight", "comment": "Finnish ambient duo"},
         {"gid": "g-2", "name": "Northlight", "comment": "Canadian folk singer"}]
EVIDENCE = {"albums": "Harbour Lights", "genres": "Ambient", "folder": "Electronic/Ambient/Northlight/2009 - Harbour Lights"}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_canonization_row_says_what_happened_and_opens_its_details():
    """B-86: a row under the same name says what happened — the link, with
    MusicBrainz's disambiguation, which tells the namesake picked — and is
    never folded into a count; a rename shows both names; a pick the name
    guard refused says the guard's reason, the model's own beside it. Each
    row is a button that opens its details: the candidates weighed with the
    pick marked, what the model was shown, the artist on MusicBrainz. A
    library's name is escaped."""
    same = {"from": "Northlight", "to": "Northlight", "verdict": "canonized", "conf": "high", "gid": "g-1",
            "why": "Ambient candidate matches Ambient folder, Harbour Lights releases", "comment": "Finnish ambient duo",
            "proposed": False, "guard": None, "candidates": CANDS, "evidence": EVIDENCE}
    renamed = {**same, "from": "Cafe <del> Sol", "to": "Café del Sol", "comment": "", "gid": "g-9", "candidates": []}
    refused = {**same, "from": "Harbour Town Orchestra", "to": "Northlight Trio", "verdict": "guard-rejected", "guard": "name",
               "why": "folder names Northlight Trio", "comment": "Norwegian jazz trio", "gid": "g-7", "proposed": True}
    proc = subprocess.run([NODE, "-e", _CANON, "--", str(STATIC / "app-shell.js")],
                          input=json.dumps([same, renamed, refused]).encode(), capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()
    out = json.loads(proc.stdout)
    row = out["rows"]
    assert all(r.startswith('<button class="canon-log-row') and f'data-canon="{i}"' in r for i, r in enumerate(row))
    assert 'class="from"' not in row[0] and '<span class="to">Northlight</span>' in row[0]
    assert "Linked to its MusicBrainz artist · Finnish ambient duo" in row[0]
    assert '<span class="canon-why">Ambient candidate matches Ambient folder, Harbour Lights releases</span>' in row[0]
    assert "Cafe &#60;del&#62; Sol" in row[1] and "Café del Sol" in row[1] and "canon-what" not in row[1]
    assert 'class="canon-log-row rejected"' in row[2] and "Not applied: the name guard" in row[2]
    assert "folder names Northlight Trio" in row[2]
    sheet = out["opened"]
    assert "is now linked to its MusicBrainz artist; the name did not change" in sheet[0]
    assert '<li class="is-picked">Northlight — Finnish ambient duo · its pick</li>' in sheet[0]
    assert "<li>Northlight — Canadian folk singer</li>" in sheet[0]
    assert "Electronic/Ambient/Northlight/2009 - Harbour Lights" in sheet[0]
    assert 'href="https://musicbrainz.org/artist/g-1" target="_blank" rel="noopener"' in sheet[0]
    assert "None under this spelling." in sheet[1] and "<del>" not in sheet[1]
    assert "Nothing was changed: the name guard applies a pick only when" in sheet[2]
    assert "It proposed the name itself" in sheet[2] and "that artist as: Norwegian jazz trio" in sheet[2]


_SETTINGS_AT = r"""
const fs = require('node:fs'), vm = require('node:vm');
const shell = fs.readFileSync(process.argv[process.argv.length - 1], 'utf8');
const a = shell.indexOf('  let hqpPickerWanted = null;'), b = shell.indexOf('  async function renderHqplayerSettings(root) {');
if (a < 0 || b < 0) throw new Error('the picker intent was not found');
const went = [];
let hash = 'home', mounted = true;
let routed = () => {};
const ctx = { navigate: h => { went.push(['navigate', h]); hash = h; }, parseHash: () => hash,
              document: { contains: () => mounted },
              window: { addEventListener: (type, fn) => { if (type === 'sautium:route') routed = fn; } } };
vm.runInNewContext(shell.slice(a, b) + `
  this.at = openHqpSettingsAt; this.openers = HQP_PICKER_OPENERS;
  this.mount = (load) => { hqpScreenMounted = load ? { screen: {}, load } : null; };
  this.wanted = () => hqpPickerWanted;`, ctx);
const out = [];
ctx.at('filter'); out.push([ctx.wanted(), went.splice(0)]);                 // from another screen
routed(); out.push([ctx.wanted()]);                                         // ...the route arrived there: the wish stands
hash = 'home'; routed(); out.push([ctx.wanted()]);                          // the person went elsewhere first: no wish left
hash = 'more/hqplayer'; ctx.mount(() => went.push(['load'])); ctx.at('rate'); out.push([ctx.wanted(), went.splice(0)]);
mounted = false; ctx.at('modulator'); out.push([ctx.wanted(), went.splice(0)]);   // a screen no longer in the document
process.stdout.write(JSON.stringify({ out, openers: ctx.openers }));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_hqplayer_settings_from_a_toast_opens_the_screen_at_one_picker():
    """B-89: the toast's action names a picker; the HQPlayer screen's next
    draw opens it, once — the route leads there from another screen, the
    mounted screen is drawn again — as the person's own tap on its row."""
    proc = subprocess.run([NODE, "-e", _SETTINGS_AT, "--", str(STATIC / "app-shell.js")], capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()
    got = json.loads(proc.stdout)
    assert got["out"] == [["filter", [["navigate", "more/hqplayer"]]], ["filter"], [None], ["rate", [["load"]]],
                          ["modulator", [["navigate", "more/hqplayer"]]]]
    assert got["openers"] == {"filter": '[data-action="open-filter"]', "modulator": 'button[data-knob="shaper"]',
                              "rate": 'button[data-knob="rate"]'}
    shell = _shell()
    body = shell[shell.index("    function renderBody(body, s) {"):shell.index("  // Modal sheet: HQPlayer's full filter")]
    # this draw's alone: taken at its top, opened at its end, a waiting control opening nothing
    assert body.index("hqpPickerWanted = null;") < body.index("const st = s.state || {};")
    assert "if (opener && !opener.disabled) opener.click();" in body
    # a state that arrives after the person left draws nothing and opens nothing
    load = shell[shell.index("    async function load() {\n      const fresh = claimFresh('hqp.load');"):shell.index(
        "    function renderBody(body, s) {")]
    assert load.index("if (!document.contains(screen)) return;") < load.index("renderBody(body, s);")
    for selector in got["openers"].values():
        marker = selector.split('"')[1]
        assert marker in body or f"data-knob=\"${{knob}}\"" in body, selector


def test_the_variant_picker_has_a_row_of_its_own():
    """B-133: the album header's pill stood on the artist's line wherever a
    short name and the short "All variants · best per track" fit beside each
    other — the artist line is an inline button, and so was the pill. It is
    a block-level box under the artist and the composer line, in every
    layout (no media rule makes it inline again)."""
    css = _css()
    toggle = _rule(css, ".album-variant-toggle")
    assert toggle["display"] in ("flex", "block", "grid")
    assert toggle["width"] == "fit-content"
    for body in re.findall(r"\.album-variant-toggle\s*\{([^{}]*)\}", css):
        assert not re.search(r"display\s*:\s*inline", body)
    head = _shell()
    meta = head[head.index('<div class="album-meta-block">'):]
    meta = meta[:meta.index('<div class="album-meta-row">')]
    assert meta.index("album-artist-line") < meta.index("${composerHtml}") < meta.index("${variantHtml}")
