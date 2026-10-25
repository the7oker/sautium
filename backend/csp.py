"""What a page the node serves may load: its Content-Security-Policy.

Every load the Web UI makes is the node's own — its scripts, sheets and
typefaces under /static/, every cover through /api/covers/ (an outside one
too: the node fetches it), the API and its event streams, the browser
output's media. Until this policy only the code kept it so, and a hotlink a
later change let in would have told an image host the listener's address
without a sign. The policy names this origin and nothing else, so that the
browser itself refuses the rest and tells REPORT_PATH.

It allows what the pages hold today and no more:
- inline script and style: app-shell.js's templates carry on* handlers (a
  cover's onerror fallback, the Back buttons) and style attributes, root()
  writes the build stamp into the shell as an inline script, and the
  Last.fm callback page is a bare document with a <style> of its own;
- blob: and data: for media only — the silent clip that spends a tap on
  the browser output, a prefetched track it plays from memory; no image
  the page shows is either;
- no form the browser sends (every submit is the page's script), no frame
  around a page, no <base>, no plugin.

Report-only: a violation is reported and logged, nothing is refused.
"""

import json
import logging
import re

from fastapi import APIRouter, Request, Response
from starlette.datastructures import MutableHeaders

logger = logging.getLogger(__name__)

REPORT_PATH = "/api/csp-report"
HEADER = "Content-Security-Policy-Report-Only"
POLICY = "; ".join((
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self'",
    "media-src 'self' blob: data:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
    # Not report-to: the Reporting API delivers only to a potentially
    # trustworthy endpoint, which a plain-HTTP LAN address is not, and a
    # policy that names report-to has its report-uri ignored.
    f"report-uri {REPORT_PATH}",
))

_REPORT_MAX_BYTES = 16 * 1024
_KINDS_LOGGED = 32
_kinds: set = set()          # (directive, what it refused) — logged once each; capped: the route is open to the LAN
_kinds_full = False

# What a report says was refused, by kind: a URL's origin, the scheme of a
# data: or blob: URL, or CSP's own word for the rest ("inline", "eval").
_REFUSED = re.compile(r"([a-z][a-z0-9+.-]{0,30})(?:(:)(?://([a-z0-9.:\[\]-]{1,100}))?)?",
                      re.IGNORECASE | re.ASCII)


class PolicyMiddleware:
    """The policy on every HTML document the node sends — the shell at /,
    the same file under /static/, the Last.fm callback page, a page added
    later — whichever route made it. Pure ASGI: the header joins the
    response as it starts; the body passes untouched."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_policy(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if headers.get("content-type", "").startswith("text/html"):
                    headers.append(HEADER, POLICY)
            await send(message)

        await self.app(scope, receive, send_with_policy)


router = APIRouter()


def _refused(blocked) -> str:
    m = _REFUSED.match(str(blocked))
    if not m:
        return "?"
    scheme, colon, host = m.groups()
    if host:
        return f"{scheme.lower()}://{host.lower()}"
    return f"{scheme.lower()}:" if colon else scheme.lower()


def _clean(value, limit: int = 200) -> str:
    """A value from a report as one log-safe token: printable ASCII, bounded."""
    return re.sub(r"[^ -~]", "?", str(value))[:limit]


@router.post(REPORT_PATH, status_code=204, include_in_schema=False)
async def report(request: Request) -> Response:
    """A browser's report of a load the policy refused — or, report-only,
    would refuse. Unsigned by necessity (the browser sends it on its own),
    so it defends itself: the body is bounded, each kind — the directive
    and what it refused — is logged once, and so many kinds at most."""
    global _kinds_full
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > _REPORT_MAX_BYTES:
            return Response(status_code=413)
    try:
        violation = json.loads(body)["csp-report"]
        directive = violation.get("effective-directive") or violation["violated-directive"].split()[0]
        kind = (_clean(directive, 40), _refused(violation.get("blocked-uri") or ""))
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError):
        return Response(status_code=400)        # RecursionError: JSON nested deeper than the parser goes
    if kind in _kinds:
        return Response(status_code=204)
    if len(_kinds) >= _KINDS_LOGGED:
        if not _kinds_full:
            _kinds_full = True
            logger.warning("Content-Security-Policy: more than %d kinds of violation; "
                           "a new kind goes unlogged until the backend restarts", _KINDS_LOGGED)
        return Response(status_code=204)
    _kinds.add(kind)
    disposition = violation.get("disposition") or ("report" if HEADER.endswith("-Report-Only") else "enforce")
    source, line, sample = (violation.get(k) for k in ("source-file", "line-number", "script-sample"))
    logger.warning("Content-Security-Policy (%s): %s does not allow %s; page %s%s%s",
                   "report only" if disposition == "report" else "enforced", *kind,
                   _clean(violation.get("document-uri") or "?"),
                   f", from {_clean(source)}" + (f":{_clean(line, 10)}" if line else "") if source else "",
                   f', sample "{_clean(sample, 60)}"' if sample else "")
    return Response(status_code=204)
