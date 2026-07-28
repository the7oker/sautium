"""Device authentication endpoints — how a browser earns a signing token.

Unauthenticated by necessity (a client with no token cannot sign yet), so
every route here is either read-only about *how* to log in, or itself the
credential check. See backend/device_auth.py for the model and for why the
brute-force defences differ between password and PIN.
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException
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


@router.post("/pin")
async def issue_pin() -> dict:
    """Mint a pairing PIN. Signature-protected: the launcher (which holds the
    server secret) and already-paired devices may open a pairing window; a
    stranger on the LAN may not."""
    return {"code": device_auth.issue_pin(),
            "expires_in": device_auth.PIN_TTL_SECONDS}
