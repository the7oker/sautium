"""The Content-Security-Policy of the Web UI's pages (backend/csp.py): on the
shell, the same file under /static/ and the Last.fm callback page, naming
the node's own origin alone; and the route a browser reports a violation
to — unsigned, taking only a browser's report about a page of the node's,
bounded, each kind logged once.

What a browser does with the policy is scripts/csp-probe.mjs (third-party
loads planted on the page, refused) and scripts/ui-shots.mjs (every screen,
no violation)."""

import asyncio
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

NODE = "http://localhost:8800"


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


def _reporting_app():
    app = FastAPI()
    app.include_router(csp.router)
    return app


@pytest.fixture
def reports():
    return TestClient(_reporting_app())


def _report(**fields):
    """A report as Chrome sends one (CSP 3); a field set to None is left out."""
    body = {"document-uri": f"{NODE}/", "referrer": "", "effective-directive": "img-src",
            "violated-directive": "img-src", "original-policy": csp.POLICY, "disposition": "report",
            "blocked-uri": "https://images.example/front.jpg", "line-number": 1609,
            "column-number": 23, "source-file": f"{NODE}/static/app-shell.js?v=258",
            "status-code": 200, "script-sample": ""}
    body.update(fields)
    return json.dumps({"csp-report": {k: v for k, v in body.items() if v is not None}})


def _post(client, body, content_type="application/csp-report"):
    return client.post(csp.REPORT_PATH, content=body, headers={"Content-Type": content_type})


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
        assert response.headers["x-dns-prefetch-control"] == "off", path
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


def test_the_policy_allows_what_the_page_needs():
    """The lower bound: the browser output plays a data: clip and blob: tracks
    (player.js), and the page carries inline handlers, style attributes and
    the build stamp — until those go, 'unsafe-inline' stays."""
    directives = dict(d.split(" ", 1) for d in csp.POLICY.split("; "))
    assert set(directives["media-src"].split()) == {"'self'", "blob:", "data:"}
    for name in ("script-src", "style-src"):
        assert set(directives[name].split()) == {"'self'", "'unsafe-inline'"}, name
    for name in ("img-src", "font-src", "connect-src"):
        assert directives[name] == "'self'", name


def test_the_report_route_takes_no_signature_and_keeps_the_host_guard(node, caplog):
    caplog.set_level(logging.WARNING, logger="csp")
    assert _post(node, _report()).status_code == 204
    assert len(_logged(caplog)) == 1
    refused = node.post(csp.REPORT_PATH, content=_report(),
                        headers={"Host": "rebound.example", "Content-Type": "application/csp-report"})
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
                              "disposition": "enforce", "script-sample": "return this"}),
          content_type="application/json")
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


def test_a_frame_around_the_page_is_said_as_such(reports, caplog):
    """The report names the framed page, never the site around it."""
    caplog.set_level(logging.WARNING, logger="csp")
    _post(reports, _report(**{"effective-directive": "frame-ancestors", "blocked-uri": f"{NODE}/",
                              "source-file": None}))
    assert _logged(caplog) == [
        f"Content-Security-Policy (report only): frame-ancestors does not allow a frame around it; page {NODE}/"]


def test_the_kinds_logged_are_bounded(reports, caplog):
    caplog.set_level(logging.WARNING, logger="csp")
    for i in range(csp._KINDS_LOGGED + 5):
        assert _post(reports, _report(**{"blocked-uri": f"https://host{i}.example/x"})).status_code == 204
    logged = _logged(caplog)
    assert len(logged) == csp._KINDS_LOGGED + 1
    assert logged[-1].startswith(f"Content-Security-Policy: more than {csp._KINDS_LOGGED} kinds")
    _post(reports, _report(**{"blocked-uri": "https://one-more.example/x"}))
    assert len(_logged(caplog)) == csp._KINDS_LOGGED + 1


def test_a_browser_extensions_own_loads_are_not_the_pages(reports, caplog):
    """WebKit reports such a load as its URL, Firefox as the scheme alone."""
    caplog.set_level(logging.WARNING, logger="csp")
    for i in range(csp._KINDS_LOGGED + 5):
        blocked = ("moz-extension", f"safari-web-extension://{i:08x}/inject.css",
                   f"chrome-extension://{i:08x}/inject.js")[i % 3]
        assert _post(reports, _report(**{"blocked-uri": blocked,
                                         "effective-directive": f"img-src-{i}"})).status_code == 204
    assert _logged(caplog) == []
    _post(reports, _report())                        # the budget is the page's still
    assert len(_logged(caplog)) == 1


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded",
                                          "multipart/form-data; boundary=x", ""])
def test_only_a_reports_media_type_is_taken(reports, caplog, content_type):
    """What a script on another site can post to the node without a CORS
    preflight is none of a report's types."""
    caplog.set_level(logging.DEBUG, logger="csp")
    assert _post(reports, _report(), content_type=content_type).status_code == 415
    assert _logged(caplog) == []


@pytest.mark.parametrize("page", ["https://elsewhere.example/", "http://localhost.elsewhere.example:8800/",
                                  "http://localhost:8800@elsewhere.example/", "http://localhost:@elsewhere.example/",
                                  "about:blank", "data:text/html,x", "http:///", "//localhost/"])
def test_a_report_about_no_page_of_the_nodes_is_refused(reports, caplog, page):
    """Another site's own policy may name this route for its pages' reports;
    the browser sends them with a report's type."""
    caplog.set_level(logging.DEBUG, logger="csp")
    assert _post(reports, _report(**{"document-uri": page})).status_code == 400
    assert _logged(caplog) == []


@pytest.mark.parametrize("page", ["http://localhost:8800/", "https://localhost/", "http://[::1]:8800/",
                                  "http://user:secret@localhost:8800/"])
def test_a_report_about_a_page_on_the_nodes_own_host_is_taken(reports, caplog, page):
    caplog.set_level(logging.WARNING, logger="csp")
    assert _post(reports, _report(**{"document-uri": page})).status_code == 204
    assert len(_logged(caplog)) == 1


@pytest.mark.parametrize("body, status", [
    ("x" * (csp._REPORT_MAX_BYTES + 1), 413),
    ("not json", 400),
    (json.dumps({"something": "else"}), 400),
    (json.dumps({"csp-report": ["a", "list"]}), 400),
    (json.dumps({"csp-report": {"document-uri": f"{NODE}/", "blocked-uri": "https://images.example/"}}), 400),
    (json.dumps({"csp-report": {"document-uri": f"{NODE}/", "violated-directive": ""}}), 400),
    (json.dumps({"csp-report": {"document-uri": 8800, "effective-directive": "img-src"}}), 400),
    (json.dumps({"csp-report": {"effective-directive": "img-src", "blocked-uri": "https://images.example/"}}),
     400),
    ("[" * 8000, 400),                         # deeper than the JSON parser goes
])
def test_a_report_the_route_cannot_read_logs_nothing(reports, caplog, body, status):
    caplog.set_level(logging.DEBUG, logger="csp")
    assert _post(reports, body).status_code == status
    assert _logged(caplog) == []


def _sender_leaves_mid_body(app):
    """The server's own events for a report whose sender goes away after part
    of its body (the test client always sends a whole body): the status
    answered, if any."""
    events = [{"type": "http.request", "body": b'{"csp-report": {"document-', "more_body": True},
              {"type": "http.disconnect"}]
    sent = []

    async def receive():
        return events.pop(0) if events else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": csp.REPORT_PATH, "raw_path": csp.REPORT_PATH.encode(),
             "query_string": b"", "root_path": "", "server": ("localhost", 8800), "client": ("127.0.0.1", 50000),
             "headers": [(b"host", b"localhost:8800"), (b"content-type", b"application/csp-report"),
                         (b"content-length", b"1000")]}
    asyncio.run(app(scope, receive, send))
    return [m["status"] for m in sent if m["type"] == "http.response.start"]


def test_a_sender_that_leaves_mid_body_is_answered_without_a_trace(caplog):
    caplog.set_level(logging.DEBUG)
    assert _sender_leaves_mid_body(_reporting_app()) == [400]
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_a_sender_that_leaves_mid_body_is_answered_so_through_the_whole_app(node, caplog):
    """Through the backend's own middlewares: the signature check and the
    static files' cache headers wrap the stream the route reads."""
    caplog.set_level(logging.DEBUG)
    assert _sender_leaves_mid_body(node.app) == [400]
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_a_report_cannot_write_a_line_of_its_own(reports, caplog):
    caplog.set_level(logging.WARNING, logger="csp")
    forged = "\r\n2026-10-25 12:00:00 - auth_hmac - WARNING - forged"
    _post(reports, _report(**{"blocked-uri": "inline" + forged, "effective-directive": "img-src" + forged,
                              "document-uri": f"{NODE}/{forged}", "source-file": forged,
                              "line-number": forged, "script-sample": forged}))
    _post(reports, _report(**{"blocked-uri": "https://images.example\n" + forged}))
    logged = _logged(caplog)
    assert len(logged) == 2
    assert not any(c in m for m in logged for c in "\r\n")
    assert " does not allow inline; " in logged[0]
    assert " does not allow https://images.example; " in logged[1]
