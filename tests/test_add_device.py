""""Add a device" and the first-run form's conveniences (PI-NODE-2B §4.2,
§5.6, Q20).

- The node's own addresses as links: a private address is "in this network";
  100.64.0.0/10 is a tailnet's only on an interface named tailscale* (it is
  also a phone tether's); a bridged container offers the addresses its
  compose file was told, never its bridge. The link is on the port the page
  itself reached the node on. Its QR code is a square of modules, and the
  route is a signed one.
- auth.js's own functions under Node: `#setup=` fills the form's code and
  leaves the address bar at once, other fragments kept; the setup code is
  grouped as it is typed, and a pasted line is left as it is.
- app-shell.js's `qrSvg`: one square a dark module, on white, with a quiet
  zone.
"""

import json
import shutil
import socket
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

NODE = shutil.which("node")


def _iface(*addresses):
    return [types.SimpleNamespace(family=socket.AF_INET, address=a) for a in addresses] + [
        types.SimpleNamespace(family=socket.AF_INET6, address="fe80::1")]


@pytest.fixture
def links(monkeypatch):
    pytest.importorskip("psutil")
    pytest.importorskip("qrcode")
    module = pytest.importorskip("device_links")
    import claude_code_runner
    import psutil
    from config import settings
    monkeypatch.setattr(psutil, "net_if_addrs", lambda: {
        "lo": _iface("127.0.0.1"), "eth0": _iface("192.0.2.10", "10.1.2.3", "169.254.7.7"),
        "tailscale0": _iface("100.64.0.10"), "usb0": _iface("100.80.0.2"),      # a phone's tether
        "wwan0": _iface("203.0.113.9"),                                          # a public address
        # Docker's own bridges on a host-networked node: this machine's, no door from another device
        "docker0": _iface("172.17.0.1"), "br-0a1b2c3d4e5f": _iface("172.18.0.1"), "veth9f3a": _iface("172.19.0.2")})
    monkeypatch.setattr(claude_code_runner, "IN_DOCKER", True)
    monkeypatch.setattr(settings, "sautium_host_network", True)
    return module


def test_a_nodes_links_are_its_own_addresses_named_for_what_they_are(links):
    found = links.links(8800)
    assert [(l["url"], l["kind"]) for l in found] == [
        ("http://10.1.2.3:8800/", "lan"), ("http://100.64.0.10:8800/", "tailnet")]
    # 192.0.2.0/24 is documentation space, not a private address: no link — as
    # for the tether's CGNAT address, the public one and loopback
    for link in found:
        size = len(link["qr"])
        assert size >= 21 and all(len(row) == size and set(row) <= {"0", "1"} for row in link["qr"])


def test_a_bridged_container_offers_the_addresses_it_was_told_never_its_bridge(links, monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "sautium_host_network", False)
    monkeypatch.setenv("SAUTIUM_HOST_IPS", "192.168.7.20, my-host.example, 100.64.0.10, 127.0.0.1")
    assert [l["url"] for l in links.links(8800)] == ["http://192.168.7.20:8800/"]
    monkeypatch.delenv("SAUTIUM_HOST_IPS")
    assert links.links(8800) == []


def test_the_links_route_is_signed_and_uses_the_port_the_page_came_by(links, monkeypatch):
    import asyncio

    import auth_hmac
    from routers import auth
    assert "/api/auth/device-links" not in auth_hmac.WHITELIST_EXACT
    assert not any("/api/auth/device-links".startswith(p) for p in auth_hmac.WHITELIST_PREFIX)
    request = types.SimpleNamespace(url=types.SimpleNamespace(port=18800))
    answer = asyncio.run(auth.device_links(request))
    assert [l["url"] for l in answer["links"]][0] == "http://10.1.2.3:18800/"


# -- the page's own functions ----------------------------------------------------------------

def _function(source: str, head: str) -> str:
    start = source.index(f"\n  function {head} {{\n") + 1
    return source[start:source.index("\n  }\n", start) + len("\n  }")]


AUTH = (BACKEND / "static" / "auth.js").read_text(encoding="utf-8")
SHELL = (BACKEND / "static" / "app-shell.js").read_text(encoding="utf-8")

DRIVER = r"""
'use strict';
const replaced = [];
let location = { hash: '', pathname: '/', search: '' };
const history = { replaceState(_s, _t, url) { replaced.push(url); location.hash = url.includes('#') ? url.slice(url.indexOf('#')) : ''; } };
%(fns)s
const out = { setup: [], typed: {} };
for (const hash of %(hashes)s) {
  location = { hash, pathname: '/', search: '?x=1' };
  replaced.length = 0;
  out.setup.push([takeFragmentSetup(), replaced.slice()]);
}
for (const typed of %(typed)s) {
  const field = { value: '', listeners: {}, addEventListener(name, fn) { this.listeners[name] = fn; } };
  groupSetupCode(field);
  field.value = typed;
  field.listeners.input();
  out.typed[typed] = field.value;
}
groupSetupCode(null);                                 // a form with no such field
const prefilled = { value: 'ubb4uj65', addEventListener() {} };
groupSetupCode(prefilled);                            // the code the address carried
out.prefilled = prefilled.value;
out.svg = qrSvg(['101', '010', '111']);
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_setup_code_comes_from_the_address_and_is_grouped_as_typed():
    fns = "\n".join([_function(AUTH, "takeFragmentSetup()"), _function(AUTH, "groupSetupCode(field)"),
                     _function(SHELL, "qrSvg(rows)")])
    hashes = ["#setup=D889-VPL4", "#more/library&setup=d889vpl4", "#pair=ABCD-EFGH", "", "#setup="]
    typed = ["ubb4", "ubb4u", "UBB4UJ65", "ubb4 uj65", "ubb4-uj65",
             "backend-1 | WARNING The setup code to create it is UBB4-UJ65 (also in …)"]
    driver = DRIVER % {"fns": fns, "hashes": json.dumps(hashes), "typed": json.dumps(typed)}
    proc = subprocess.run([NODE, "-e", driver], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["setup"] == [
        ["D889-VPL4", ["/?x=1"]],                     # taken, and gone from the address bar
        ["d889vpl4", ["/?x=1#more/library"]],         # the screen's own fragment stays
        ["", []], ["", []], ["", []]]                 # a pairing code is not a setup code
    assert out["typed"] == {
        "ubb4": "UBB4", "ubb4u": "UBB4-U", "UBB4UJ65": "UBB4-UJ65", "ubb4 uj65": "UBB4-UJ65",
        "ubb4-uj65": "UBB4-UJ65",
        "backend-1 | WARNING The setup code to create it is UBB4-UJ65 (also in …)":
            "backend-1 | WARNING The setup code to create it is UBB4-UJ65 (also in …)"}
    assert out["prefilled"] == "UBB4-UJ65"
    svg = out["svg"]
    assert svg.count("h1v1h-1z") == 6 and 'viewBox="0 0 11 11"' in svg     # 3 modules + a quiet zone of 4
    assert 'fill="#fff"' in svg and "M4 4h1v1h-1z" in svg and "<image" not in svg


def test_the_first_run_form_offers_the_email_and_keeps_its_grant_for_the_next_page():
    """The email is optional, validated before anything is made, set with
    the password still in hand, and its code's grant handed to the page that
    loads next — which opens the code's dialog with no second tap."""
    assert 'input("auth-email", "email", "email (optional)")' in AUTH
    assert "This machine is too small to earn its network identity by computing." in AUTH
    assert AUTH.index("That email does not look right") < AUTH.index(
        "let res = await window.Sautium.auth.createAccount(...given, false, keep, email);")
    assert 'if (ok && email) await rememberEmail(email, given[1]);' in AUTH
    assert "sessionStorage.setItem(EMAIL_GRANT_KEY, r.data.grant)" in AUTH
    assert "const firstRunGrant = window.Sautium.auth.takeFirstRunEmailGrant();" in SHELL
    assert "if (firstRunGrant) openEmailVerifyFlow(firstRunGrant);" in SHELL
