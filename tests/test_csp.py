"""The Content-Security-Policy of the pages the node serves (backend/csp.py):
on the shell, the same file under /static/ and the Last.fm callback page,
naming the node's own origin alone; and the route a browser reports a
violation to — unsigned, bounded, each kind logged once.

What a browser does with the policy is scripts/csp-probe.mjs (a planted
third-party load, refused) and scripts/ui-shots.mjs (every screen, no
violation)."""

import json
import logging
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import csp  # noqa: E402

NODE = "http://192.0.2.10:8800"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(csp, "_kinds", set())
    monkeypatch.setattr(csp, "_kinds_full", False)


@pytest.fixture(scope="module")
def node():
    """The backend's own app — its middlewares, the Host guard among them —
    without its lifespan: nothing here needs the database or a worker."""
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(BACKEND)              # main mounts static/ relative to its directory
        import main
        yield TestClient(main.app, base_url="http://localhost")


@pytest.fixture
def reports():
    app = FastAPI()
    app.include_router(csp.router)
    return TestClient(app)


def _report(**fields):
    """A report as Chrome sends one (CSP 3); a field set to None is left out."""
    body = {"document-uri": f"{NODE}/", "referrer": "", "effective-directive": "img-src",
            "violated-directive": "img-src", "original-policy": csp.POLICY, "disposition": "report",
            "blocked-uri": "https://images.example/front.jpg", "line-number": 1609,
            "column-number": 23, "source-file": f"{NODE}/static/app-shell.js?v=258",
            "status-code": 200, "script-sample": ""}
    body.update(fields)
    return json.dumps({"csp-report": {k: v for k, v in body.items() if v is not None}})


def _post(client, body):
    return client.post(csp.REPORT_PATH, content=body,
                       headers={"Content-Type": "application/csp-report"})


def _logged(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "csp"]


def test_every_page_the_node_serves_carries_the_policy(node):
    stale = node.get("/lastfm/auth/callback/" + "0" * 32)
    assert stale.status_code == 400                  # an expired link's page
    for path, response in (("/", node.get("/")),
                           ("/static/index.html", node.get("/static/index.html")),
                           ("/lastfm/auth/callback/<nonce>", stale)):
        assert response.headers["content-type"].startswith("text/html"), path
        assert response.headers[csp.HEADER] == csp.POLICY, path
    # FastAPI's interactive docs are no page of the node's: theirs load from a CDN
    assert node.app.docs_url is None and node.app.redoc_url is None


def test_what_is_not_a_page_carries_no_policy(node):
    for response in (node.get("/static/app-shell.js"), node.get("/static/fonts.css"),
                     node.get("/api/settings/sync")):                 # unsigned: a 401 in JSON
        assert csp.HEADER not in response.headers
        assert "content-security-policy" not in response.headers


def test_the_policy_names_the_nodes_origin_alone():
    directives = dict(d.split(" ", 1) for d in csp.POLICY.split("; "))
    assert directives.pop("report-uri") == csp.REPORT_PATH
    for name, sources in directives.items():
        assert set(sources.split()) <= {"'self'", "'none'", "'unsafe-inline'", "blob:", "data:"}, name
    assert directives["default-src"] == "'self'"
    assert [name for name, sources in directives.items()
            if "blob:" in sources or "data:" in sources] == ["media-src"]
    for name in ("object-src", "base-uri", "form-action", "frame-ancestors"):
        assert directives[name] == "'none'", name


def test_the_report_route_takes_no_signature_and_keeps_the_host_guard(node, caplog):
    caplog.set_level(logging.WARNING, logger="csp")
    assert _post(node, _report()).status_code == 204
    assert len(_logged(caplog)) == 1
    refused = node.post(csp.REPORT_PATH, content=_report(), headers={"Host": "rebound.example"})
    assert refused.status_code == 421


def test_a_kind_is_logged_once(reports, caplog):
    caplog.set_level(logging.WARNING, logger="csp")
    assert _post(reports, _report()).status_code == 204
    assert _post(reports, _report()).status_code == 204
    _post(reports, _report(**{"blocked-uri": "https://images.example/back.jpg?w=500"}))
    assert _logged(caplog) == [
        f"Content-Security-Policy (report only): img-src does not allow https://images.example; "
        f"page {NODE}/, from {NODE}/static/app-shell.js?v=258:1609"]

    _post(reports, _report(**{"blocked-uri": "https://cdn.example/a.css",
                              "effective-directive": "style-src-elem"}))
    _post(reports, _report(**{"blocked-uri": "https://cdn.example/a.png"}))
    _post(reports, _report(**{"blocked-uri": "data", "effective-directive": "img-src"}))
    _post(reports, _report(**{"blocked-uri": "eval", "effective-directive": "script-src",
                              "disposition": "enforce", "script-sample": "return this"}))
    assert [m.split(":", 1)[1].split(";")[0] for m in _logged(caplog)[1:]] == [
        " style-src-elem does not allow https://cdn.example",
        " img-src does not allow https://cdn.example",
        " img-src does not allow data",
        " script-src does not allow eval"]
    assert _logged(caplog)[-1].startswith("Content-Security-Policy (enforced): ")
    assert _logged(caplog)[-1].endswith(', sample "return this"')


def test_a_report_of_the_older_form_is_read(reports, caplog):
    """CSP 2's report: the violated directive whole, no effective directive,
    no disposition (the page's own header decides) — and, as a planted load
    has none, no source file."""
    caplog.set_level(logging.WARNING, logger="csp")
    _post(reports, _report(**{"effective-directive": None, "disposition": None, "source-file": None,
                              "violated-directive": "img-src 'self'"}))
    assert _logged(caplog) == [
        f"Content-Security-Policy (report only): img-src does not allow https://images.example; page {NODE}/"]


def test_the_kinds_logged_are_bounded(reports, caplog):
    caplog.set_level(logging.WARNING, logger="csp")
    for i in range(csp._KINDS_LOGGED + 5):
        assert _post(reports, _report(**{"blocked-uri": f"https://host{i}.example/x"})).status_code == 204
    logged = _logged(caplog)
    assert len(logged) == csp._KINDS_LOGGED + 1
    assert logged[-1].startswith(f"Content-Security-Policy: more than {csp._KINDS_LOGGED} kinds")
    _post(reports, _report(**{"blocked-uri": "https://one-more.example/x"}))
    assert len(_logged(caplog)) == csp._KINDS_LOGGED + 1


@pytest.mark.parametrize("body, status", [
    ("x" * (csp._REPORT_MAX_BYTES + 1), 413),
    ("not json", 400),
    (json.dumps({"something": "else"}), 400),
    (json.dumps({"csp-report": ["a", "list"]}), 400),
    (json.dumps({"csp-report": {"blocked-uri": "https://images.example/"}}), 400),
    (json.dumps({"csp-report": {"violated-directive": ""}}), 400),
    ("[" * 8000, 400),                         # deeper than the JSON parser goes
])
def test_a_report_the_route_cannot_read_logs_nothing(reports, caplog, body, status):
    caplog.set_level(logging.DEBUG, logger="csp")
    assert _post(reports, body).status_code == status
    assert _logged(caplog) == []


def test_a_report_cannot_write_a_line_of_its_own(reports, caplog):
    caplog.set_level(logging.WARNING, logger="csp")
    forged = "\r\n2026-10-25 12:00:00 - auth_hmac - WARNING - forged"
    _post(reports, _report(**{"blocked-uri": "inline" + forged, "effective-directive": "img-src" + forged,
                              "document-uri": NODE + forged, "source-file": forged,
                              "line-number": forged, "script-sample": forged}))
    _post(reports, _report(**{"blocked-uri": "https://images.example\n" + forged}))
    logged = _logged(caplog)
    assert len(logged) == 2
    assert not any(c in m for m in logged for c in "\r\n")
    assert " does not allow inline; " in logged[0]
    assert " does not allow https://images.example; " in logged[1]
