"""Last.fm account authorization — the flow behind Profile › Last.fm in the
Web UI and the launcher's first-run dialog.

Last.fm's web flow ends in a redirect. The authorization page is opened with
the API key and a callback (`cb`) naming this node, and when the user grants
access Last.fm mints a token and sends the browser to the callback with it.
That redirect IS the completion event: /lastfm/auth/callback/<nonce>
exchanges the token for the session key, persists it and wakes every client
on /lastfm/auth/stream — nobody has to guess when the browser step is over.
The first version asked the user to (a "Complete" button), and a click a
moment early failed with "Unauthorized Token" (launcher first run,
2026-09-22).

The page carries no token of the node's own. A `token` parameter makes it
Last.fm's desktop flow, whose browser step ends on Last.fm's own page —
the user is told to go back to the application — and which ignores `cb`:
the version of 2026-09-22 minted one (auth.getToken) as a manual fallback
and put `cb` beside it, and on a real account the browser never came back
(2026-10-07).

The callback is unsigned by necessity (a browser redirect from last.fm
cannot carry HMAC headers) and is admitted by the whitelist on the nonce
alone: 128 bits, minted only for a signed caller, good for one connection,
gone with the flow — which ends on the exchange's outcome or at the page's
deadline, and stays open only while Last.fm does not answer. The page it
renders never echoes the token or the session key, and the Host guard still
applies to it.
"""

import asyncio
import html
import json
import logging
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urlsplit

import pylast
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

import auth_hmac
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/lastfm/auth", tags=["lastfm"])

AUTH_PAGE = "https://www.last.fm/api/auth/"
CALLBACK_PREFIX = "/lastfm/auth/callback/"

# How long an open authorization page stays good: a slow login on the
# Last.fm side and not much more, so a Last.fm tab found later in the
# browser's history connects nothing. The deadline is an event — it ends the
# flow and wakes the window waiting on it, which otherwise would wait on a
# page that can no longer finish.
FLOW_TTL_SECONDS = 30 * 60
_EXPIRED = "The Last.fm page expired."

_SESSION_KEY_KEY = "lastfm.session_key"
_USERNAME_KEY = "lastfm.username"


class FlowError(Exception):
    """A sentence for the user; the flow's state is described by status()."""


@dataclass
class Flow:
    nonce: str
    callback_base: str                          # where Last.fm sends the browser back
    auth_url: str
    deadline: threading.Timer = field(init=False)
    exchanging: bool = False                    # a callback is turning the token into a session
    expired: bool = False                       # the deadline passed while it was exchanging


_flow: Optional[Flow] = None
_last_error: Optional[str] = None               # how the last flow failed, until the next start
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


# Last.fm's error 9, "Invalid session key - Please re-authenticate": the owner
# revoked the access, or the session was issued to an app this node no longer
# uses — the API key changed on 2026-10-05.
_INVALID_SESSION = "9"


def rejects_session(exc: BaseException) -> bool:
    return isinstance(exc, pylast.WSError) and str(exc.status) == _INVALID_SESSION


def session_rejected() -> None:
    """Last.fm no longer honours the session: forget it — the row and the copy
    in `settings` — so Profile › Last.fm offers to connect again instead of
    reading "connected" over scrobbles that go nowhere. A session that came
    from .env (LASTFM_SESSION_KEY) returns at the next start; it belongs
    there only on a node without the in-app flow."""
    global _last_error
    from db_pool import db_execute
    db_execute("DELETE FROM user_settings WHERE key = %s", (_SESSION_KEY_KEY,))
    settings.lastfm_session_key = None
    with _lock:
        _last_error = "Last.fm no longer accepts this node's connection — connect it again."
    logger.warning("Last.fm rejected the session key — the connection is dropped")
    _notify()


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
    """The URL to open in the browser. A live flow is handed back as-is to
    the address that started it: a double tap on the button must produce one
    page, not two, and a second page would replace the nonce the open tab
    comes back with. Another address gets a flow of its own, because the
    callback IS the address — a phone handed the page the launcher started
    would be sent back to 127.0.0.1. The newest flow wins; a page still open
    on the other address lands on "expired". Both hold mid-exchange too: the
    exchange still reports its outcome, which leaves a newer flow's status
    alone (`_end`)."""
    global _flow, _last_error
    callback_base = _callback_base(origin)
    with _lock:
        if _flow is not None and _flow.callback_base == callback_base:
            return _flow.auth_url
        if _flow is not None:
            _flow.deadline.cancel()
        nonce = secrets.token_urlsafe(16)
        auth_url = AUTH_PAGE + "?" + urlencode({"api_key": settings.lastfm_api_key,
                                                "cb": callback_base + nonce})
        _flow = Flow(nonce, callback_base, auth_url)
        _flow.deadline = threading.Timer(FLOW_TTL_SECONDS, _expire, (_flow,))
        _flow.deadline.daemon = True
        _flow.deadline.start()
        _last_error = None
        return auth_url


def _expire(flow: Flow) -> None:
    """The page's deadline. A flow whose callback is exchanging is left to
    that exchange, which reports its own outcome — and ends the flow as
    expired should Last.fm not answer, instead of offering a reload."""
    global _flow, _last_error
    with _lock:
        if _flow is not flow:
            return
        flow.expired = True
        if flow.exchanging:
            return
        _flow = None
        _last_error = _EXPIRED
    _notify()


def _end(flow: Flow, error: Optional[str]) -> None:
    """The callback's outcome — connected (no error) or not: the flow is
    over, status() says how it went, and every waiting client is woken. A
    flow another address started meanwhile keeps its own status."""
    global _flow, _last_error
    flow.deadline.cancel()
    with _lock:
        if _flow is flow:
            _flow = None
        if _flow is None:
            _last_error = error
    _notify()


def callback(nonce: str, token: str) -> str:
    """The browser is back from last.fm with the token Last.fm minted at the
    grant. Returns the username; raises FlowError with the sentence for the
    page when nothing was earned. A stale or foreign nonce changes nothing
    and wakes nobody, and neither does a second callback while the first is
    still exchanging."""
    from lastfm import LastFmService, SourceUnavailable
    with _lock:
        flow = _flow
        if flow is None or not secrets.compare_digest(flow.nonce.encode(), nonce.encode()):
            raise FlowError("This authorisation link has expired. "
                            "Return to Sautium and start again.")
        if not token:
            raise FlowError("Last.fm sent the browser back without a token. "
                            "Return to Sautium and start again.")
        if flow.exchanging:
            raise FlowError("Sautium is already finishing this authorisation. "
                            "Return to Sautium.")
        flow.exchanging = True
    try:
        session_key, username = LastFmService().auth_session(token)
        persist(session_key, username)
    except SourceUnavailable as e:
        # No answer about the token, which Last.fm keeps good for an hour:
        # the flow stays, and reloading this page asks again — while it is
        # still the flow and inside its deadline.
        logger.warning("Last.fm did not answer the session exchange: %s", e)
        with _lock:
            flow.exchanging = False
            reloadable = _flow is flow and not flow.expired
        if not reloadable:
            _end(flow, _EXPIRED)
            raise FlowError("This authorisation link has expired. "
                            "Return to Sautium and start again.") from e
        raise FlowError(f"Last.fm did not answer ({e}). "
                        "Reload this page to finish connecting.") from e
    except pylast.WSError as e:
        # Last.fm's verdict about the token it sent itself: not worth a
        # second try — the next start mints a fresh flow.
        logger.warning("Last.fm refused the authorisation token: %s", e)
        _end(flow, f"Last.fm refused the authorisation: {e}")
        raise FlowError(f"Last.fm refused the authorisation: {e}. "
                        "Return to Sautium and start again.") from e
    except Exception as e:
        logger.error("Finishing the Last.fm connection failed", exc_info=True)
        _end(flow, f"Sautium could not finish the connection: {e}")
        raise FlowError(f"Sautium could not finish the connection: {e}. "
                        "Return to Sautium and start again.") from e
    _end(flow, None)
    logger.info("Last.fm authorized as %s", username)
    # The connection is the import's first trigger: the owner's history is
    # what makes a new node's Home theirs (backend/lastfm_history.py).
    import lastfm_history
    lastfm_history.start("connected")
    return username


def status() -> Dict[str, Any]:
    with _lock:
        flow = _flow
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
