"""The Web UI page — its stylesheets, scripts and typefaces — loads from its
own origin.

A client that loads only the node's origin (an app's web view) must find
what the page is made of there, and a browser on the LAN must tell no font
service its address by opening it."""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "backend" / "static"


def test_the_page_names_no_other_origin():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    loads = re.findall(r'<(?:link|script|img|source|iframe)\b[^>]*\b(?:href|src)="([^"]+)"', html)
    assert loads, "the page loads its stylesheets and scripts"
    assert [u for u in loads if re.match(r"(?:[a-z]+:)?//", u)] == []


def test_every_typeface_the_page_asks_for_is_served_with_its_licence():
    css = (STATIC / "fonts.css").read_text(encoding="utf-8")
    faces = re.findall(r'font-family: "([^"]+)";[^}]*font-weight: (\d+);[^}]*url\(([^)]+)\)', css)
    assert {(family, int(weight)) for family, weight, _ in faces} == (
        {("Inter Tight", w) for w in (400, 500, 600, 700)}
        | {("JetBrains Mono", w) for w in (400, 500, 600)})
    for _family, _weight, url in faces:
        assert url.startswith("/static/fonts/") and url.endswith(".woff2")
        assert (STATIC / url.removeprefix("/static/")).is_file(), url
    for licence in ("LICENSE-inter-tight.txt", "LICENSE-jetbrains-mono.txt"):
        assert "SIL OPEN FONT LICENSE Version 1.1" in (STATIC / "fonts" / licence).read_text()
    assert '<link rel="stylesheet" href="/static/fonts.css' in (STATIC / "index.html").read_text()
