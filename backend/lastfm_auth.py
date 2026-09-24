"""Last.fm account authorization — the flow behind Profile › Last.fm in the
Web UI and the launcher's first-run dialog.

Last.fm's browser step ends in a redirect. The authorization page is opened
with a callback (`cb`) naming this node, and when the user grants access
Last.fm sends the browser to it carrying the authorised token. That redirect
IS the completion event: /lastfm/auth/callback/<nonce> exchanges the token
for the session key, persists it and wakes every client on
/lastfm/auth/stream — nobody has to guess when the browser step is over.
The first version asked the user to (a "Complete" button), and a click a
moment early, or a Last.fm redirect nobody saw land, failed with
"Unauthorized Token" (launcher first run, 2026-09-22).

The desktop flow stays underneath as the fallback: the page also carries a
token this node minted (auth.getToken), so a browser that never comes back
can still be finished by hand — POST /lastfm/auth/complete exchanges that
token.

The callback is unsigned by necessity (a browser redirect from last.fm
cannot carry HMAC headers) and is admitted by the whitelist on the nonce
alone: 128 bits, minted only for a signed caller, single use, gone with the
flow. The page it renders never echoes the token or the session key, and
the Host guard still applies to it.
"""

import asyncio
import html
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import quote, urlsplit

import pylast
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

import auth_hmac
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/lastfm/auth", tags=["lastfm"])

CALLBACK_PREFIX = "/lastfm/auth/callback/"

# A Last.fm token lives 60 minutes from auth.getToken. The flow expires a
# little earlier so every callback it still accepts carries a live token —
# and a slow login with 2FA on the Last.fm side stays inside the window.
FLOW_TTL_SECONDS = 50 * 60

_SESSION_KEY_KEY = "lastfm.session_key"
_USERNAME_KEY = "lastfm.username"


class FlowError(Exception):
    """A sentence for the user; the flow's state is described by status()."""


@dataclass
class Flow:
    nonce: str
    token: str                                  # minted by auth.getToken
    auth_url: str
    generator: pylast.SessionKeyGenerator
    started_at: float

    def alive(self) -> bool:
        return time.time() - self.started_at < FLOW_TTL_SECONDS


_flow: Optional[Flow] = None
_last_error: Optional[str] = None               # the callback's refusal, until the next start
_lock = threading.Lock()
_sse_clients: list = []
_sse_lock = threading.Lock()


# -- Persistence --------------------------------------------------------------
#
# Pydantic Settings reads .env once at startup, so what the flow earns goes to
# user_settings and onto `settings` at runtime; the lifespan hook overlays the
# rows again on the next start. Nothing is written to .env — the launcher keeps
# it read-only after generating it.

def _upsert(key: str, value: str) -> None:
    from db_pool import db_execute
    db_execute(
        """
        INSERT INTO user_settings (key, value) VALUES (%s, %s::jsonb)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                                        updated_at = CURRENT_TIMESTAMP
        """,
        (key, json.dumps(value)),
    )


def persist(session_key: str, username: Optional[str]) -> None:
    _upsert(_SESSION_KEY_KEY, session_key)
    if username:
        _upsert(_USERNAME_KEY, username)
    settings.lastfm_session_key = session_key
    if username:
        settings.lastfm_username = username


def load_from_db() -> None:
    """Overlay the user_settings credentials onto `settings`. DB wins over env."""
    try:
        from db_pool import db_query_one
        for key, attr in ((_SESSION_KEY_KEY, "lastfm_session_key"),
                          (_USERNAME_KEY, "lastfm_username")):
            row = db_query_one("SELECT value FROM user_settings WHERE key = %(k)s", {"k": key})
            if row and row.get("value"):
                setattr(settings, attr, row["value"])
    except Exception as e:
        logger.warning(f"Failed to load Last.fm credentials from DB: {e}")


# -- The flow -----------------------------------------------------------------

def _network() -> pylast.LastFMNetwork:
    from lastfm import lastfm_network
    return lastfm_network()


def _callback_base(origin: str) -> str:
    """Where Last.fm sends the browser back: the origin the CLIENT reached
    this node at (127.0.0.1 for the launcher, the LAN or tunnel address for
    a phone, the front's name behind TLS), which only the client knows. The
    Host guard decides whether that is one of this node's own addresses."""
    parts = urlsplit(origin or "")
    if (parts.scheme not in ("http", "https") or not parts.netloc
            or parts.path not in ("", "/") or parts.query or parts.fragment):
        raise FlowError("origin must be the scheme and host this node was reached at")
    if not auth_hmac.host_allowed(parts.netloc):
        raise FlowError(f"{parts.netloc} is not one of this node's addresses")
    return f"{parts.scheme}://{parts.netloc}{CALLBACK_PREFIX}"


def start(origin: str) -> str:
    """The URL to open in the browser. A live flow is handed back as-is: a
    second start would mint a token the open tab is not authorising, and a
    double tap on the button must produce one page, not two."""
    global _flow, _last_error
    callback_base = _callback_base(origin)
    with _lock:
        if _flow is not None and _flow.alive():
            return _flow.auth_url
        generator = pylast.SessionKeyGenerator(_network())
        desktop_url = generator.get_web_auth_url()          # auth.getToken
        token = generator.web_auth_tokens[desktop_url]
        nonce = secrets.token_urlsafe(16)
        auth_url = f"{desktop_url}&cb={quote(callback_base + nonce, safe='')}"
        _flow = Flow(nonce, token, auth_url, generator, time.time())
        _last_error = None
        return auth_url


def callback(nonce: str, token: str) -> str:
    """The browser is back from last.fm. Returns the username; raises
    FlowError with the sentence for the page when nothing was earned. A
    stale or foreign nonce changes nothing and wakes nobody."""
    with _lock:
        flow = _flow
        if flow is None or not flow.alive() or not secrets.compare_digest(flow.nonce, nonce):
            raise FlowError("This authorisation link has expired. "
                            "Return to Sautium and start again.")
    if not token:
        raise FlowError("Last.fm sent the browser back without a token. "
                        "Return to Sautium and start again.")
    # A token Last.fm itself sent and then refuses is not worth a second
    # try — the flow ends, and the next start mints a fresh one.
    return _finish(flow, token, keep_on_error=False)


def complete() -> str:
    """The manual fallback: exchange the token this node minted. Fails with
    Last.fm's own words while the user has not granted access yet, and the
    flow stays open for the moment they do."""
    with _lock:
        flow = _flow
    if flow is None or not flow.alive():
        raise FlowError("Auth flow not started. Call /lastfm/auth/start first.")
    return _finish(flow, flow.token, keep_on_error=True)


def _finish(flow: Flow, token: str, *, keep_on_error: bool) -> str:
    global _flow, _last_error
    try:
        session_key, username = flow.generator.get_web_auth_session_key_username(None, token)
    except Exception as e:
        detail = str(e)
        if not keep_on_error:
            with _lock:
                if _flow is flow:
                    _flow = None
                _last_error = detail
            _notify()
        raise FlowError(detail) from e
    persist(session_key, username)
    with _lock:
        if _flow is flow:
            _flow = None
        _last_error = None
    logger.info("Last.fm authorized as %s", username)
    _notify()
    # The connection is the import's first trigger: the owner's history is
    # what makes a new node's Home theirs (backend/lastfm_history.py).
    import lastfm_history
    lastfm_history.start("connected")
    return username


def status() -> Dict[str, Any]:
    with _lock:
        flow = _flow if _flow is not None and _flow.alive() else None
        error = _last_error
    return {
        "authorized": bool(settings.lastfm_session_key),
        "username": settings.lastfm_username or "",
        "pending": flow is not None,
        "auth_url": flow.auth_url if flow else None,
        "error": error,
    }


# -- Wake channel -------------------------------------------------------------

def subscribe() -> tuple:
    evt = asyncio.Event()
    entry = (evt, asyncio.get_event_loop())
    with _sse_lock:
        _sse_clients.append(entry)
    return entry


def unsubscribe(entry: tuple) -> None:
    with _sse_lock:
        _sse_clients[:] = [e for e in _sse_clients if e[0] is not entry[0]]


def _notify() -> None:
    with _sse_lock:
        for evt, loop in list(_sse_clients):
            try:
                loop.call_soon_threadsafe(evt.set)
            except RuntimeError:
                continue          # loop closed; the generator cleans itself up


# -- Routes -------------------------------------------------------------------

class StartRequest(BaseModel):
    origin: str


@router.post("/start")
async def auth_start(req: StartRequest) -> Dict[str, str]:
    """Mint the flow and hand back the page to open in the browser."""
    try:
        url = await asyncio.to_thread(start, req.origin)
    except FlowError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"auth_url": url}


@router.get("/callback/{nonce}", response_class=HTMLResponse)
async def auth_callback(nonce: str, token: str = "") -> HTMLResponse:
    """Where Last.fm sends the browser once the user has granted access."""
    try:
        username = await asyncio.to_thread(callback, nonce, token)
    except FlowError as e:
        return HTMLResponse(_page("Last.fm authorisation failed", html.escape(str(e))),
                            status_code=400)
    return HTMLResponse(_page(
        "Connected to Last.fm",
        f"Sautium is connected as <b>{html.escape(username)}</b>. "
        "You can close this tab and return to Sautium."))


@router.get("/status")
async def auth_status() -> Dict[str, Any]:
    return status()


@router.get("/stream")
async def auth_stream() -> StreamingResponse:
    """SSE wake channel for the sheet or dialog that opened the browser: a
    frame whenever the callback landed, either way — the state itself is
    read back over /status, the pattern of every wake channel here."""
    entry = subscribe()
    evt = entry[0]

    async def event_generator():
        try:
            yield "data: {}\n\n"
            while True:
                try:
                    await asyncio.wait_for(evt.wait(), timeout=20.0)
                    evt.clear()
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield "data: {}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            unsubscribe(entry)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/complete")
async def auth_complete() -> Dict[str, Any]:
    """The manual fallback for a browser that did not come back."""
    try:
        username = await asyncio.to_thread(complete)
    except FlowError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Authorization failed. Make sure you allowed access in the browser. ({e})",
        )
    return {"success": True, "username": username, "lastfm_authorized": True}


def _page(title: str, body_html: str) -> str:
    """The one page the callback renders — a bare document in the app's
    palette, since nothing of the Web UI is loaded on it."""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sautium · Last.fm</title>
<style>
  html, body {{ margin: 0; min-height: 100%; background: #1B1714; color: #EDE2D4;
               font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
  main {{ max-width: 28rem; margin: 18vh auto 0; padding: 0 1.5rem; text-align: center; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 .75rem; }}
  p {{ margin: 0; color: #A69B8E; }}  b {{ color: #EDE2D4; }}
</style></head>
<body><main><h1>{html.escape(title)}</h1><p>{body_html}</p></main></body></html>
"""
