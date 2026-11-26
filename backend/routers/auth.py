"""Device authentication endpoints — how a browser earns a signing token.

Unauthenticated by necessity (a client with no token cannot sign yet), so
every route here is either read-only about *how* to log in, or itself the
credential check. See backend/device_auth.py for the model and for why the
brute-force defences differ between password and PIN.

Every route that carries a credential — a password or PIN in, a token out —
speaks the boxed envelope (`BoxedRequest` in, `{nonce, box}` out): the Web UI
rides plain HTTP, and the box is what keeps a LAN sniffer from reading the
exchange. `GET /handshake` mints the server half of one exchange; the
credential request that quotes it consumes it (device_auth "Credential
channel").

`boxed(Model)` and `step_up` are shared beyond these doors: every route
elsewhere that takes a secret typed in the Web UI (an AI key, a sign-in's
code, the email's code) or a change a device token alone does not make (the
email, an invite token with rights, the AI key, an agent's sign-in) takes
its body through `boxed`, and the latter the password through `step_up`
(CLAUDE.md Security Posture rule 8).
"""

import asyncio
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

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


# A pairing code: 7 groups of 4 and their hyphens (device_auth.PIN_GROUPS)
PIN_MAX = 64


class PairRequest(BaseModel):
    code: str = Field(default="", max_length=PIN_MAX)


class ChangeIdentityRequest(BaseModel):
    username: str = Field(default="", max_length=32)
    # Empty = minted, as in the wizard: the identity stays this machine's.
    password: str = Field(default="", max_length=256)
    # The person's word for a password one GPU would find within a year,
    # given after the time was said (device_auth.check_new_password)
    keep: bool = False
    # The account's password as it is now — a device token alone does not
    # change who the node is. An anonymous account has none anyone knows:
    # its host vouches instead, by the pairing code it shows now.
    current_password: str = Field(default="", max_length=256)
    pin: str = Field(default="", max_length=PIN_MAX)


class EmptyPayload(BaseModel):
    pass


class BoxedRequest(BaseModel):
    """One sealed credential exchange: the handshake it answers (`eph`), the
    browser's own box key, and the nonce + ciphertext of the JSON payload."""
    eph: str = Field(max_length=64)
    client: str = Field(max_length=64)
    nonce: str = Field(max_length=64)
    # An invite link's welcome message (2000 characters, Cyrillic two bytes
    # each) rides one too
    box: str = Field(max_length=16384)


def _open(req: BoxedRequest, model):
    """The payload inside the box, validated as `model`, plus the reply
    sealer. A box that does not open is a 400 marked `channel` — a handshake
    the table has let go since, as often as a broken one: the page asks for
    a fresh one once; a payload of the wrong shape is the 422 the plain body
    used to get."""
    try:
        payload, reply = device_auth.unbox(req.eph, req.client, req.nonce, req.box)
    except device_auth.ChannelError as e:
        raise HTTPException(status_code=400, detail=str(e),
                            headers={"X-Sautium-Auth-Error": "channel"})
    try:
        return model.model_validate(payload), reply
    except ValidationError as e:
        # The refusal rides in the clear: it names the field, never what was in it
        raise HTTPException(status_code=422, detail=e.errors(include_url=False, include_input=False,
                                                             include_context=False))


_ENVELOPE = frozenset(BoxedRequest.model_fields)


def boxed(model):
    """The dependency of every route whose body rides the box (`model` is the
    payload's shape): the envelope opened (_open) and the reply sealer, as
    `(payload, reply)`. A body that is not an envelope is refused before it
    is read as anything — the 400 marked `unboxed` names no field of it:
    FastAPI's own 422 for a body of the wrong shape echoes the body back,
    which for a plain API key is the key."""
    async def opened(request: Request):
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or set(body) != _ENVELOPE:
            raise HTTPException(status_code=400,
                                detail="This is accepted only through the credential box — reload the page",
                                headers={"X-Sautium-Auth-Error": "unboxed"})
        try:
            envelope = BoxedRequest.model_validate(body)
        except ValidationError:
            raise HTTPException(status_code=400, detail="malformed envelope")
        return _open(envelope, model)
    return Depends(opened)


class StepUp(BaseModel):
    """What a payload carries for a change a device token alone does not
    make (device_auth.step_up): the account's password, or on an anonymous
    account the pairing code its host shows now."""
    password: str = Field(default="", max_length=256)
    pin: str = Field(default="", max_length=PIN_MAX)


HOST_CODE_NEEDED = ("This node has no password: open this page from the computer that runs "
                    "Sautium (on a Docker node, the link ./sautium pair or python -m pairing "
                    "prints), or scan its QR code, and do it within a few minutes")


async def step_up(req: StepUp) -> None:
    """The person's own word, or the refusal that says what was missing: a
    403 marked `step-up` (the page asks again), 429 while the door waits out
    its delay, 503 when the node cannot derive now."""
    try:
        await device_auth.step_up(req.password, req.pin)
    except device_auth.Busy:
        raise HTTPException(status_code=503, detail="busy", headers={"Retry-After": "5"})
    except device_auth.Shut as e:
        raise _shut(e)
    except device_auth.WrongPassword:
        raise HTTPException(status_code=403, detail="That is not this account's password",
                            headers={"X-Sautium-Auth-Error": "step-up"})
    except device_auth.WrongPin:
        raise HTTPException(status_code=403, detail=HOST_CODE_NEEDED,
                            headers={"X-Sautium-Auth-Error": "step-up"})
    except device_auth.OlderIdentity:
        raise _older()


def _older() -> HTTPException:
    """An identity made before SEC-S5: its password can no longer be checked."""
    return HTTPException(status_code=409, detail=device_auth.OlderIdentity.WORDS,
                         headers={"X-Sautium-Auth-Error": "older-identity"})


@router.get("/handshake")
async def handshake() -> dict:
    """The server half of one credential exchange, signed by the node's
    identity key so a browser that knows this node can tell it from something
    answering in its place. Unsigned while the node has no identity yet; its
    `tag` is the same under the device token, for a browser that holds one
    and pinned nothing yet (device_auth.open_channel)."""
    return device_auth.open_channel(_secret)


@router.get("/status")
async def auth_status() -> dict:
    """What this node accepts, so the UI knows which form to show. Says
    nothing secret — only whether an account exists to log into and whether a
    pairing PIN is currently outstanding."""
    configured = device_auth.account_configured()
    onboarding = not configured and not device_auth.identity_key_on_disk()
    older = device_auth.older_identity()
    return {
        # An anonymous account (wizard, random password nobody has seen) is
        # configured and still has no password door. Reporting one sent the
        # UI to a prompt that could never be satisfied, on the very nodes the
        # PIN exists for. Nor has an identity made before SEC-S5: nothing
        # checks its password
        "password_login": device_auth.password_door(),
        # Where the account's key stands (device_auth.key_state): "ready",
        # "deriving", "another_identity", "interrupted", or None. Before the
        # key the node answers its handshake unsigned, and a browser signs in
        # all the same — it pins the node when the key is there
        "key": device_auth.key_state(),
        # Who makes an interrupted key: this node at a sign-in, or its launcher
        "key_here": device_auth.births_here(),
        "older_identity": older,
        # No account yet: the node cannot be signed into at all, so the UI
        # offers to create one. The window closes for good the moment it is
        # — and is never open over a key on disk, named or not.
        "onboarding": onboarding,
        # No identity, while the database says the node had one: the setup
        # brings that one back, and makes another only when told to
        "identity_missing": onboarding and device_auth.identity_missing(),
        # A key with no account beside it: nothing here can sign a browser in
        "unnamed_key": not configured and device_auth.identity_key_on_disk(),
        "pairing_open": device_auth.pin_state() is not None,
        # A host that shows pairing codes (the launcher): without one the
        # account password is the only way in, so it is never minted away
        "pin_door": device_auth.pin_door(),
        # A launcher's node (its window, its identity folder) or a Docker
        # node (its console, its log): where its setup code and its pairing
        # code are found
        "launcher": device_auth.launcher_mode(),
        "username": device_auth.account_username(),
        # While the setup form is open: this node never earns its identity
        # by computing (it runs no model — routers/p2p._email_road), so the
        # form's optional email is the way to one, and says so
        "email_road": onboarding and _runs_no_model(),
    }


def _runs_no_model() -> bool:
    import hardware_profile
    return not hardware_profile.resolve().ml_available


@router.get("/device-links")
async def device_links(request: Request) -> dict:
    """"Add a device": this node's own addresses as links with their QR
    codes (device_links.py). A signed route — the page of a device already
    signed in shows them; the other device then signs in with the account's
    password. Nothing is minted."""
    import device_links as links
    # The port the page reached the node on; a page that came through a
    # front (a name, the scheme's own port) is given the node's own door
    port = request.url.port or int(os.getenv("UVICORN_PORT") or 8800)
    return {"links": await asyncio.to_thread(links.links, port)}


class CreateAccountRequest(BaseModel):
    username: str = Field(default="", max_length=32)
    # Empty: no password — a random key, made at once (SEC-S5 §15, §20)
    password: str = Field(default="", max_length=256)
    # The email typed beside it, read here only as a word an attacker would
    # try first (the form sets it on the account once it exists)
    email: str = Field(default="", max_length=254)
    keep: bool = False
    # The code the node wrote to its log and its identity folder at start —
    # or a line pasted around it, which the code is read out of
    setup_code: str = Field(default="", max_length=256)
    # The owner's word, asked by the page after a refusal said so: an
    # identity other than the one this node's records were signed with
    new_identity: bool = False


@router.post("/create-account")
async def create_account(opened=boxed(CreateAccountRequest)) -> dict:
    """First-run setup for a node with no identity — the Docker case, where
    there is no launcher to show a pairing code and no password to know.

    Unsigned, because nothing exists yet to sign with; what stands in for
    a credential is the setup code the node wrote where only its owner
    reads (its log, its identity folder — device_auth.prepare_setup). The
    window shuts the instant an account exists, and a second call is
    refused.

    The account is the node's P2P identity, not a local login — so this also
    turns on sync, chat and analysis signing, which stay dark without one.

    On a node that lost its key (`identity_missing` in the status) the setup
    brings that identity back from its nickname and password. Whether the
    pair makes that identity is known when its key is made, minutes later:
    a pair that makes another waits for the owner's word (status `key`
    "another_identity", POST /new-identity) — or is made at once with
    `new_identity`, the page's "a new one" said before."""
    req, reply = opened
    try:
        # The check lives inside create_account, under its lock — testing it
        # out here as well would only re-open the race it exists to close.
        info = await device_auth.create_account(req.username, req.password, req.setup_code,
                                                req.new_identity, req.keep, req.email)
    except device_auth.WeakPassword as e:
        return reply(_weak(e))
    except device_auth.AccountExists:
        raise HTTPException(status_code=409, detail="account already exists")
    except device_auth.WrongSetupCode:
        raise HTTPException(status_code=403, detail="That is not this node's setup code: the node writes a "
                                                    "new one at each start, to its log and its identity folder")
    except device_auth.Shut as e:
        raise HTTPException(status_code=429,
                            detail=f"Too many wrong setup codes: the next is taken in {e.seconds} s",
                            headers={"Retry-After": str(e.seconds), "X-Sautium-Auth-Error": "shut"})
    except ValueError as e:                       # username shape
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"account creation failed: {e}")
        raise HTTPException(status_code=500, detail="could not create account")
    # The account exists from now on, and this browser is signed in to it;
    # its key is made in the background, and what speaks for the node — the
    # chat client, the identity worker, the peer surface under the key — is
    # started when it lands (device_auth._key_arrived), with no restart.
    return reply({"token": device_auth.current_token(_secret()), **info})


class NewIdentityRequest(StepUp):
    make: bool


@router.post("/new-identity")
async def new_identity(opened=boxed(NewIdentityRequest)) -> dict:
    """The owner's word on the key the setup made, which is not the identity
    this node's records were signed with (status `key`:
    "another_identity"): make it the node's — those records are another
    author's from then on — or take the account back and set up again. Who
    the node is changes only on the person's own word: boxed, with the
    account's password (step_up), and the answer sealed — a refusal of a
    weak password says something of the password."""
    req, reply = opened
    await step_up(req)
    try:
        return reply(await device_auth.decide_new_identity(req.make))
    except device_auth.NothingHeld:
        raise HTTPException(status_code=409, detail="no key waits for that answer")
    except device_auth.WeakPassword as e:
        return reply(_weak(e))


@router.post("/resume-key")
async def resume_key(opened=boxed(StepUp)) -> dict:
    """The account's password, asked by the page when its key's making was
    stopped (the identity line's "interrupted", device_auth.say_key_at_start):
    checked at the signed door, and the key is made again (password_tried's
    resume). 403 marked `step-up` for a wrong one, as every step-up."""
    req, reply = opened
    await step_up(req)
    return reply({"key": device_auth.key_state()})


def _weak(e: "device_auth.WeakPassword") -> dict:
    """A door's answer to a password under its edge, sealed: a 200 whose
    body says `password` — the time in the clear would tell a LAN listener
    the password is near the top of the lists. A refusal brings a phrase."""
    from desktop import password_strength
    said = dict(e.verdict)
    if said["zone"] == "refused":
        said["offer"] = password_strength.phrase_offer()
    return {"password": said}


class EstimateRequest(BaseModel):
    password: str = Field(default="", max_length=256)
    username: str = Field(default="", max_length=32)
    email: str = Field(default="", max_length=254)


# The setup form's meter is unsigned (no token exists yet): one estimate at a
# time for it, so a LAN neighbour buys at most one of zxcvbn's bounded runs
_setup_estimates = 0


def _onboarding() -> bool:
    return not device_auth.account_configured() and not device_auth.identity_key_on_disk()


@router.post("/setup-estimate")
async def setup_estimate(opened=boxed(EstimateRequest)) -> dict:
    """The first-run form's meter: the time one GPU would take to find the
    password typed, as the setup door will read it. Only while the node has
    no account; the words an attacker tries first are what the request
    carries — the nickname and email typed beside it — never the node's own."""
    global _setup_estimates
    req, reply = opened
    if not _onboarding():
        raise HTTPException(status_code=409, detail="account already exists")
    if _setup_estimates:
        raise HTTPException(status_code=503, detail="busy", headers={"Retry-After": "1"})
    from desktop import password_strength
    _setup_estimates += 1
    try:
        return reply(await asyncio.to_thread(password_strength.verdict, req.password,
                                             (req.username, req.email)))
    finally:
        _setup_estimates -= 1


@router.post("/estimate")
async def estimate(opened=boxed(EstimateRequest)) -> dict:
    """The meter of a signed form (a new password for this account): as the
    setup's, with the account's own email among the words tried first."""
    req, reply = opened
    from desktop import password_strength
    return reply(await asyncio.to_thread(
        password_strength.verdict, req.password, (req.username, device_auth.account_email())))


@router.post("/setup-phrase")
async def setup_phrase(opened=boxed(EmptyPayload)) -> dict:
    """A phrase for the first-run form, drawn by `secrets` on the node, with
    the draw's own count; sealed, as a password is. Only while the node has
    no account."""
    _, reply = opened
    if not _onboarding():
        raise HTTPException(status_code=409, detail="account already exists")
    from desktop import password_strength
    return reply(password_strength.phrase_offer())


@router.post("/phrase")
async def phrase(opened=boxed(EmptyPayload)) -> dict:
    """A phrase for a signed form (a new password for this account)."""
    _, reply = opened
    from desktop import password_strength
    return reply(password_strength.phrase_offer())


def _shut(e: "device_auth.Shut") -> HTTPException:
    return HTTPException(status_code=429, detail="too many wrong passwords",
                         headers={"Retry-After": str(e.seconds), "X-Sautium-Auth-Error": "shut"})


@router.post("/login")
async def login(opened=boxed(LoginRequest)) -> dict:
    """Exchange the account password for a device token, through the
    password door: shut by wrong passwords it answers 429 before anything is
    derived; each wrong one is counted and told to the owner
    (device_auth.password_tried)."""
    req, reply = opened
    try:
        ok = await device_auth.password_tried(req.password)
    except device_auth.Shut as e:
        raise _shut(e)
    except device_auth.Busy:
        raise HTTPException(status_code=503, detail="busy", headers={"Retry-After": "5"})
    except device_auth.OlderIdentity:
        raise _older()
    if not ok:
        raise HTTPException(status_code=401, detail="invalid credentials")
    # The first sign-in of this key's password since S2: its time, said
    # once when it is under a year (the page shows it, nothing keeps it)
    told = await asyncio.to_thread(device_auth.told_once, req.password)
    return reply({"token": device_auth.current_token(_secret()),
                  **({"password": told} if told else {})})


@router.post("/pair")
async def pair(opened=boxed(PairRequest)) -> dict:
    """Exchange a host-displayed pairing code for a device token. A code
    pairs until it expires (device_auth.redeem_pin); a wrong one is told to
    the owner."""
    req, reply = opened
    if not await device_auth.redeem_pin(req.code):
        raise HTTPException(status_code=401, detail="invalid or expired code")
    return reply({"token": device_auth.current_token(_secret())})


@router.post("/change-identity")
async def change_identity(opened=boxed(ChangeIdentityRequest)) -> dict:
    """A new name and/or password. The name and the password are both KDF
    inputs, so either change IS a new key: on a launcher friends are told by
    a notice the old key signed (its P2P manager; a Docker node sends none),
    a verified email has to be verified again from the new key (which is
    what makes the notary record succession), other devices sign in with the
    new pair, and every paired browser is out — this one gets a fresh token
    in the reply, like logout-all.

    Signature-protected (not whitelisted): only a paired browser or the
    host can rotate, and an account with a password has it typed again — an
    anonymous one carries the pairing code its host shows now — checked with
    the rotation, under its lock, before anything else is derived
    (device_auth.rotate_identity). Every host shows a pairing code (a
    Docker node's console since SEC-S5 §18), so a change to no password
    leaves a way in. Boxed all the same — the passwords go in and the new
    token comes out."""
    req, reply = opened
    username = req.username.strip()
    if not username:
        raise HTTPException(status_code=422, detail="Nickname required")
    try:
        info = await device_auth.rotate_identity(username, req.password, req.current_password, req.pin,
                                                 req.keep)
    except device_auth.WeakPassword as e:
        return reply(_weak(e))
    except device_auth.Busy:
        raise HTTPException(status_code=503, detail="busy", headers={"Retry-After": "5"})
    except device_auth.Shut as e:
        raise _shut(e)
    except device_auth.WrongPassword:
        raise HTTPException(status_code=403, detail="The current password is not this account's")
    except device_auth.WrongPin:
        raise HTTPException(status_code=403, detail="This node has no password: open this page from the "
                                                    "computer that runs Sautium, or scan its QR code, "
                                                    "and change it within a few minutes")
    except device_auth.OlderIdentity:
        raise _older()
    except device_auth.KeyBeingMade:
        raise HTTPException(status_code=409, detail="This node's key is being made: change it once that is done")
    except ValueError as e:                       # username shape / same pair
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if info.get("key") == "deriving":
        # The new key is made in the background; the old identity signs in
        # with the old password until it lands, and then every browser — this
        # one too — signs in again with the new one (the epoch's bump)
        from config import settings
        return reply({"username": info["username"], "key": "deriving",
                      "restart_required": not settings.p2p_listen_port})
    from config import settings
    from routers.p2p import EMAIL_FLOW_SECONDS
    return reply({
        "token": device_auth.current_token(_secret()),
        "username": info["username"],
        "invite_code": info["invite_code"],
        "public_key_hex": info["public_key_hex"],
        "anonymous": info["anonymous"],
        "email_needs_verification": bool(info.get("email")),
        # The current password or the host's code was given just now: the
        # address's code is sent on it (routers/p2p, the email flow's grant)
        "email_grant": (device_auth.grant("email", EMAIL_FLOW_SECONDS)
                        if info.get("email") else None),
        # The launcher restarts its P2P manager on the NOTIFY; a Docker
        # node serves its peer surface from this process: its certificate
        # and its request check follow the key at once
        # (main._peer_surface_follows_identity), its admission gate keeps the key the
        # start built it on.
        "restart_required": not settings.p2p_listen_port,
    })


@router.post("/logout-all")
async def logout_all(opened=boxed(EmptyPayload)) -> dict:
    """Invalidate every device token, including the caller's — then hand the
    caller a fresh one so the browser that pressed the button stays signed in.
    Requires a valid signature, so only an already-authenticated client (or
    the host) can trigger it. The fresh token rides back in the box."""
    _, reply = opened
    device_auth.bump_epoch()
    return reply({"token": device_auth.current_token(_secret())})


def _host_only(request: Request) -> None:
    """The pairing code is the host's to show: a request a browser's device
    token signed is refused — the code is what vouches for a browser, and
    for an identity change of an account with no password."""
    if getattr(request.state, "signer", None) != "host":
        raise HTTPException(status_code=403, detail="the host's own")


@router.get("/pin")
async def get_pin(request: Request) -> dict:
    """The pairing code to put on screen, with the deadline the host needs to
    schedule its own refresh. Idempotent: repeated calls return the same code
    while it lives, so the QR and the button that mints the URL cannot drift
    apart — asking used to replace it, which silently invalidated whatever was
    already drawn.

    Signed by the host alone (_host_only): the launcher holds the server
    secret; a browser's device token is refused."""
    _host_only(request)
    return device_auth.current_pin()
