"""The Web UI page — its stylesheets, scripts, typefaces and covers — loads
from its own origin.

A client that loads only the node's origin (an app's web view) must find
what the page is made of there, and a browser on the LAN must tell no font
service or image host its address by opening it."""

import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest

STATIC = Path(__file__).resolve().parent.parent / "backend" / "static"
NODE = shutil.which("node")


def test_the_page_names_no_other_origin():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    loads = re.findall(r'<(?:link|script|img|source|iframe)\b[^>]*\b(?:href|src)="([^"]+)"', html)
    assert loads, "the page loads its stylesheets and scripts"
    assert [u for u in loads if re.match(r"(?:[a-z]+:)?//", u)] == []


def test_the_page_gives_no_resource_hint():
    """A preconnect, a DNS prefetch or speculation rules reach another origin
    without a word from the Content-Security-Policy, which governs none of
    them: the page carries none."""
    hint = re.compile(r"preconnect|dns-prefetch|prerender|modulepreload|speculationrules", re.IGNORECASE)
    for path in [STATIC / "index.html", *STATIC.glob("*.js")]:
        assert not hint.search(path.read_text(encoding="utf-8")), path.name


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


# app-shell.js's cover builders, the Now Playing sheet and the mini-player
# (each block cut between its banners), then player.js whole, under Node:
# every cover URL they give the page — an <img>, the mini-player's probe, the
# Media Session's artwork, which the browser and the OS fetch themselves —
# for every kind of cover, and where a browser's URL parser says each goes.
_COVERS = r"""
const fs = require('node:fs'), vm = require('node:vm');
const [shellPath, playerPath] = process.argv.slice(-2);
const shell = fs.readFileSync(shellPath, 'utf8');
const cut = (from, to) => {
  const start = shell.indexOf(from), end = shell.indexOf(to, start);
  if (start < 0 || end < 0) throw new Error('block not found: ' + from);
  return shell.slice(start, end);
};
const probes = [];
class Image { set src(u) { this._src = u; probes.push(this); } get src() { return this._src; } }
const window = { addEventListener() {}, dispatchEvent() {} };
const navigator = { mediaSession: { metadata: null, setActionHandler() {} } };
const ctx = vm.createContext({
  window, navigator, Image, MediaMetadata: class { constructor(m) { Object.assign(this, m); } },
  document: { readyState: 'loading', addEventListener() {}, visibilityState: 'visible' },
  fetch: async () => ({ ok: false, status: 404, json: async () => ({}) }),
  crypto: require('node:crypto').webcrypto, CustomEvent: class {},
  console: { warn() {}, log() {}, error() {} }, setTimeout, clearTimeout,
});
vm.runInContext(cut('  /* ---------- Cover URL builder', '  /* ---------- Lightweight markdown')
  + cut('  /* ---------- Now Playing sheet', '  /* ---------- Mini-player adapter')
  + cut('  /* ---------- Mini-player adapter', '  /* ---------- Queue sheet')
  + '\n;this.coverUrl = coverUrl; this.artistAvatarInner = artistAvatarInner;'
  + ' this.sheet = sheet; this.mp = mp;', ctx);
vm.runInContext(fs.readFileSync(playerPath, 'utf8'), ctx);

const spec = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = { cover: spec.items.map(it => ctx.coverUrl(it)) };
const img = { srcs: [], set src(u) { this.srcs.push(u); } };
ctx.sheet.coverImg = img;
out.paint = spec.provider.map(url => {
  img.srcs = [];
  ctx.sheet.renderDetail({ cover_url: spec.caa, provider_cover_url: url });
  while (img.onerror) img.onerror();               // each load fails: the next is tried
  return img.srcs;
});
out.avatar = ctx.artistAvatarInner('a1', '<i></i>', ctx.coverUrl({ cover_url: spec.odd }));
ctx.mp.cover = { style: {} };
ctx.mp.setCover({ cover_url: spec.caa, provider_cover_url: spec.provider[0] });
for (let n = 0; n < probes.length; n++) probes[n].onerror();
out.mini = probes.map(p => p.src);
out.session = [spec.caa, ...spec.provider].map(url => {
  window.browserRenderer._mediaSession({ cover_url: url });
  return navigator.mediaSession.metadata.artwork[0].src;
});
const shown = [...out.cover, ...out.paint.flat(), ...out.mini, ...out.session,
               ...[...out.avatar.matchAll(/src="([^"]+)"|this\.src='([^']+)'/g)].map(m => m[1] || m[2])];
const base = 'http://node.test:8800/';
out.elsewhere = shown.filter(u => new URL(u, base).origin !== new URL(base).origin);
out.shown = shown;
process.stdout.write(JSON.stringify(out));
"""

_RG = "f5093c06-23e3-404f-aeaa-40f72885ee3a"
_CAA = f"https://coverartarchive.org/release-group/{_RG}/front-500"
_DEEZER = ("https://cdn-images.dzcdn.net/images/cover/5718f7c81c27e0b2417e2a4c45224f8a/"
           "1000x1000-000000-80-0-0.jpg")
_YOUTUBE = "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_every_cover_the_page_shows_comes_from_the_node():
    """Owned covers are the node's; an outside one — a Cover Art Archive
    front, a provider's art, whatever a phantom's cover_url holds — is asked
    of the node too (/api/covers/caa, /api/covers/art), never of its host,
    and travels encoded whatever it carries: a cover_url comes from data a
    share file can bring."""
    odd = "https://images.example.com/it's (1).jpg"
    hostile = ["/\\images.example.com/pixel.gif", "/\t/images.example.com/pixel.gif",
               '/x" onerror="alert(1)', "/x';alert(1);'", "javascript:alert(1)",
               "//images.example.com/x.jpg", f"/api/covers/caa/{_RG}"]
    spec = {"caa": _CAA, "provider": [_DEEZER, _YOUTUBE], "odd": odd, "items": [
        {"cover_url": _CAA}, {"cover_url": _DEEZER}, {"cover_url": odd},
        *({"cover_url": h} for h in hostile),
        {"cover_id": "c0ffee00-0000-4000-8000-000000000001"}, {"media_file_id": 7}]}
    proc = subprocess.run(
        [NODE, "-e", _COVERS, "--", str(STATIC / "app-shell.js"), str(STATIC / "player.js")],
        input=json.dumps(spec).encode(), capture_output=True, check=True)
    out = json.loads(proc.stdout)

    def art(url):
        return "/api/covers/art?url=" + quote(url, safe="")

    caa = f"/api/covers/caa/{_RG}"
    assert out["cover"] == [caa, art(_DEEZER), art(odd), *map(art, hostile),
                            "/api/covers/c0ffee00-0000-4000-8000-000000000001", "/api/covers/by-media/7"]
    assert out["paint"] == [[caa, art(_DEEZER)], [caa, art(_YOUTUBE)]]
    assert out["mini"] == [caa, art(_DEEZER)]
    assert out["session"] == [caa, art(_DEEZER), art(_YOUTUBE)]
    # The avatar's fallback sits in a quoted inline handler: the quote in the
    # URL travels encoded, or it would end the handler's string.
    assert out["shown"][-2:] == ["/api/covers/by-artist/a1", art(odd)]
    assert out["elsewhere"] == []
    # Nothing that could end an attribute, a handler's string or a CSS url().
    assert [u for u in out["shown"] if not re.fullmatch(r"/api/covers/[A-Za-z0-9_.~!*%/?=-]+", u)] == []
