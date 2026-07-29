"""Device authentication endpoints — how a browser earns a signing token.

Unauthenticated by necessity (a client with no token cannot sign yet), so
every route here is either read-only about *how* to log in, or itself the
credential check. See backend/device_auth.py for the model and for why the
brute-force defences differ between password and PIN.
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import device_auth
from auth_hmac import ensure_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _secret() -> bytes:
    from main import _API_SECRET_PATH
    return ensure_secret(_API_SECRET_PATH)


class LoginRequest(BaseModel):
    # No username: the node has one account, and which one is not the
    # signer's choice — the server reads it from its own settings.
    password: str = Field(default="", max_length=256)


class PairRequest(BaseModel):
    code: str = Field(default="", max_length=32)


@router.get("/status")
async def auth_status() -> dict:
    """What this node accepts, so the UI knows which form to show. Says
    nothing secret — only whether an account exists to log into and whether a
    pairing PIN is currently outstanding."""
    configured = device_auth.account_configured()
    return {
        "password_login": configured,
        # No account yet: the node cannot be signed into at all, so the UI
        # offers to create one. The window closes for good the moment it is.
        "onboarding": not configured,
        "pairing_open": device_auth.pin_state() is not None,
        "username": device_auth.account_username(),
    }


class CreateAccountRequest(BaseModel):
    username: str = Field(default="", max_length=32)
    password: str = Field(default="", min_length=8, max_length=256)


@router.post("/create-account")
async def create_account(req: CreateAccountRequest) -> dict:
    """First-run setup for a node with no identity — the Docker case, where
    there is no launcher to show a pairing code and no password to know.

    Deliberately unauthenticated, because nothing exists yet to authenticate
    against; the window shuts the instant an account exists, and a second
    call is refused. This is the Jellyfin/Home Assistant bargain: whoever
    reaches the machine first during setup owns it.

    The account is the node's P2P identity, not a local login — so this also
    turns on sync, chat and analysis signing, which stay dark without one."""
    try:
        # The check lives inside create_account, under its lock — testing it
        # out here as well would only re-open the race it exists to close.
        info = await device_auth.create_account(req.username, req.password)
    except device_auth.AccountExists:
        raise HTTPException(status_code=409, detail="account already exists")
    except ValueError as e:                       # username shape
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"account creation failed: {e}")
        raise HTTPException(status_code=500, detail="could not create account")
    return {"token": device_auth.current_token(_secret()),
            "invite_code": info.get("invite_code"),
            "username": info.get("username")}


@router.post("/login")
async def login(req: LoginRequest) -> dict:
    """Exchange the account password for a device token."""
    if not await device_auth.verify_password(req.password):
        raise HTTPException(status_code=401, detail="invalid credentials")
    return {"token": device_auth.current_token(_secret())}


@router.post("/pair")
async def pair(req: PairRequest) -> dict:
    """Exchange a host-displayed PIN for a device token. The PIN is one-shot;
    a wrong code costs one of MAX_PIN_ATTEMPTS."""
    if not await device_auth.redeem_pin(req.code):
        raise HTTPException(status_code=401, detail="invalid or expired code")
    return {"token": device_auth.current_token(_secret())}


@router.post("/logout-all")
async def logout_all() -> dict:
    """Invalidate every device token, including the caller's — then hand the
    caller a fresh one so the browser that pressed the button stays signed in.
    Requires a valid signature, so only an already-authenticated client (or
    the host) can trigger it."""
    device_auth.bump_epoch()
    return {"token": device_auth.current_token(_secret())}


@router.get("/pin")
async def get_pin() -> dict:
    """The pairing code to put on screen, with the deadline the host needs to
    schedule its own refresh. Idempotent: repeated calls return the same code
    while it lives, so the QR and the button that mints the URL cannot drift
    apart — asking used to replace it, which silently invalidated whatever was
    already drawn.

    Signature-protected: the launcher holds the server secret, a stranger on
    the LAN does not."""
    return device_auth.current_pin()


@router.get("/pin/stream")
async def pin_stream() -> StreamingResponse:
    """SSE wake channel for the host displaying the code.

    The host knows when the code expires — it was told — so expiry needs no
    channel. What it cannot foresee is the code being burned by someone
    guessing at /pair, which would otherwise leave a dead QR on screen until
    the next scheduled refresh. Wake-event only; the code itself is pulled
    over the signed API."""
    entry = device_auth.pin_subscribe()
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
            device_auth.pin_unsubscribe(entry)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
