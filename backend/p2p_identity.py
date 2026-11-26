"""
P2P identity for the backend — the key this node signs with, and the identity
dict every reader resolves.

The key is never derived here (SEC-S5): the launcher and a Web-UI account keep
theirs in the identity folder (node_ed25519.key), and the env road
(P2P_USERNAME/P2P_PASSWORD: the master, a hand-made .env) keeps its own after
its one derivation (node_identity.ENV_KEY_FILENAME), made by
desktop/identity_birth on device_auth's orders. Until a key is there, every
reader here says None, which each already reads as "no identity yet".
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Optional

from desktop.node_identity import CERT_FILENAME, ENV_KEY_FILENAME

logger = logging.getLogger(__name__)


def make_invite_code(username: str, public_key_raw: bytes) -> str:
    """Mirror of desktop/node_identity.make_invite_code — keep in step."""
    digest = hashlib.sha256(public_key_raw).digest()[:6]
    h = digest.hex().upper()
    return f"{username}#{h[:4]}-{h[4:8]}-{h[8:]}"


def parse_invite_code(invite_code: str) -> tuple:
    """Mirror of desktop/node_identity.parse_invite_code — keep in step."""
    if "#" not in invite_code:
        raise ValueError(f"Invalid invite code format: {invite_code}")
    username, hash_part = invite_code.split("#", 1)
    clean = hash_part.replace("-", "")
    if len(clean) != 12:
        raise ValueError(f"Invalid invite code hash length: {hash_part}")
    return username, clean.upper()


def verify_invite_code(invite_code: str, public_key_hex: str) -> bool:
    """Mirror of desktop/node_identity.verify_invite_code — keep in step."""
    try:
        _username, hash_part = parse_invite_code(invite_code)
    except ValueError:
        return False
    pub_raw = bytes.fromhex(public_key_hex)
    digest = hashlib.sha256(pub_raw).digest()[:6]
    return digest.hex().upper() == hash_part


# Cached on success only: a key can appear mid-process (an account's key
# derived in the background), and a cached None would hide it.
_signing_key_cache = None
_identity_cache = None
_identity_cache_mtime = None


_followers: list = []


def on_change(follower) -> None:
    """`follower` — a coroutine function — runs each time this node's
    identity appears (its account is made) or changes (a rotation) under the
    running process: what a start decided on the identity it had then is
    decided again, with no restart (main's three followers)."""
    if follower not in _followers:
        _followers.append(follower)


async def changed() -> None:
    """Said by the two places that make or change the identity
    (routers/auth.create_account, device_auth._after_rotation)."""
    for follower in list(_followers):
        try:
            await follower()
        except Exception:
            logger.exception("identity: %s did not follow the identity's change — "
                             "a restart of the backend decides it again", follower.__name__)


def forget_cached() -> None:
    """Drop the process-wide identity caches — after a rotation on disk the
    next call reads the new key and node_info.json."""
    global _signing_key_cache, _identity_cache, _identity_cache_mtime
    _signing_key_cache = None
    _identity_cache = None
    _identity_cache_mtime = None


def _env_key_path(settings) -> Optional[Path]:
    """The env road's kept key, when this node's account is the
    environment's: P2P_USERNAME and P2P_PASSWORD set, and no key of the
    folder's own account (node_ed25519.key — that account wins, as it always
    did) nor an account file of another road."""
    from desktop.node_identity import read_account
    if not (settings.p2p_username and settings.p2p_password and settings.p2p_identity_dir):
        return None
    d = Path(settings.p2p_identity_dir)
    if (d / "node_ed25519.key").exists():
        return None
    account = read_account(d)
    if account is not None and (account.get("source") != "env"
                                or account.get("username") != settings.p2p_username):
        return None
    return d / ENV_KEY_FILENAME


def env_road(settings) -> bool:
    """Whether this node's account is the environment's (_env_key_path)."""
    return _env_key_path(settings) is not None


def env_configured(settings) -> bool:
    """Whether a start is to settle the env road (settle_env): P2P_USERNAME
    and P2P_PASSWORD set, and the identity folder holding no account of
    another road — the name in the account file may still be another pair's."""
    from desktop.node_identity import read_account
    if not (settings.p2p_username and settings.p2p_password and settings.p2p_identity_dir):
        return False
    d = Path(settings.p2p_identity_dir)
    if (d / "node_ed25519.key").exists():
        return False
    account = read_account(d)
    return account is None or account.get("source") == "env"


def load_signing_key(settings):
    """The node's Ed25519 signing key — the folder's account key, else the
    env road's kept one — or None while there is none (no account, or its
    key not derived yet). Never derives."""
    from cryptography.hazmat.primitives import serialization

    global _signing_key_cache
    if _signing_key_cache is not None:
        return _signing_key_cache

    if settings.p2p_identity_dir:
        key_path = Path(settings.p2p_identity_dir) / "node_ed25519.key"
        if key_path.exists():
            _signing_key_cache = serialization.load_pem_private_key(
                key_path.read_bytes(), password=None)
            return _signing_key_cache
    env_key = _env_key_path(settings)
    if env_key is not None and env_key.exists():
        _signing_key_cache = serialization.load_pem_private_key(env_key.read_bytes(), password=None)
    return _signing_key_cache


def resolve_identity(settings) -> Optional[dict]:
    """The node's account identity dict — node_info.json (an account of the
    folder's) first, the env road's kept key second, None without either.
    The single resolution point mirrored by main.py's lifespan and
    routers/p2p._get_identity."""
    global _identity_cache, _identity_cache_mtime

    if settings.p2p_identity_dir:
        # node_info.json is written by the launcher AND by this backend
        # (email changes) while we run — the cache is keyed on the file's
        # mtime, so an edit is seen on the next call and a stale copy can
        # never claim "no email configured" after one was just saved (the
        # bug this replaced: two independent caches, one invalidated, the
        # other not).
        info_path = Path(settings.p2p_identity_dir) / "node_info.json"
        if info_path.exists():
            try:
                mtime = info_path.stat().st_mtime_ns
                if _identity_cache is not None and _identity_cache_mtime == mtime:
                    return _identity_cache
                data = json.loads(info_path.read_text(encoding="utf-8"))
                if data.get("username"):
                    _identity_cache = {
                        "node_id": data["node_id"],
                        "public_key_hex": data["public_key_hex"],
                        "username": data["username"],
                        "invite_code": data["invite_code"],
                        "email": data.get("email", ""),
                    }
                    _identity_cache_mtime = mtime
                    return _identity_cache
            except Exception as e:
                logger.warning(f"Failed to read node_info.json: {e}")
    if _identity_cache is not None:
        return _identity_cache
    if env_road(settings):
        key = load_signing_key(settings)
        if key is not None:
            from desktop.node_identity import public_hex
            pubkey = public_hex(key)
            _identity_cache = {
                "node_id": pubkey,
                "public_key_hex": pubkey,
                "username": settings.p2p_username,
                "invite_code": make_invite_code(settings.p2p_username, bytes.fromhex(pubkey)),
                "email": settings.p2p_email,
            }
    return _identity_cache


def settle_env(settings) -> bool:
    """At a start on the env road, before anything reads the key: True when
    its kept key is this environment's — the account file names
    P2P_USERNAME, its hash takes P2P_PASSWORD (one check, about a second)
    and the kept key is the one the file names; False when the key is to be
    derived (device_auth.derive_env_key). A first start writes the account
    file; another pair in .env is another account — the files the previous
    pair kept are moved whole aside (replaced-<when>/), as the folder's
    other replaced files are."""
    from cryptography.hazmat.primitives import serialization

    from desktop.node_identity import (_move_aside, new_account, public_hex, read_account,
                                       signin_matches, write_account)
    d = Path(settings.p2p_identity_dir)
    account = read_account(d)
    same = (account is not None and account.get("username") == settings.p2p_username
            and account.get("signin") and signin_matches(account["signin"], settings.p2p_password))
    if same:
        key_path = d / ENV_KEY_FILENAME
        if account.get("pubkey") and key_path.exists():
            held = public_hex(serialization.load_pem_private_key(key_path.read_bytes(), password=None))
            if held == account["pubkey"]:
                return True
            logger.error("The env road's kept key (%s…) is not the one its account file names "
                         "(%s…): it is moved aside and the key derived again", held[:16],
                         account["pubkey"][:16])
            aside = _move_aside(d, ENV_KEY_FILENAME)
            logger.warning("The env road's key moved to %s", aside.name)
        return False
    for name in ("account.json", ENV_KEY_FILENAME):
        aside = _move_aside(d, name)
        if aside is not None:
            logger.warning("P2P_USERNAME/P2P_PASSWORD are not the pair that made %s: moved to %s",
                           name, aside.name)
    write_account(d, new_account(settings.p2p_username, settings.p2p_password, source="env",
                                 email=settings.p2p_email or ""))
    forget_cached()
    return False


def _recorded_signing_key() -> Optional[str]:
    """The database's signing key of record (node_identity.SIGNING_KEY_SETTING)
    as the previous start left it: read before the migrations, so a database
    without `user_settings` (a new one) has none."""
    from db_pool import db_query_one
    from desktop.node_identity import SIGNING_KEY_SQL
    if not db_query_one("SELECT to_regclass('user_settings') IS NOT NULL AS present")["present"]:
        return None
    return db_query_one(f"SELECT {SIGNING_KEY_SQL} AS key")["key"]


def master_refusal(settings) -> Optional[str]:
    """The start's one decision about the master (MASTER-FAILCLOSED §1–§3):
    this process's role, decided from the key it signs with
    (desktop/p2p/master_node.decide), and — for a backend meant to be the
    master whose key is not the pin — the words of its refusal to start.
    Meant to be the master: its database records the pin as its signing key
    of record (a start the pin made here, or its restored backup), or its
    configuration says SAUTIUM_MASTER=1 (a master on a new database). The
    trusted front and the username are no signal: a person may hold either.
    None: the start goes on."""
    from pathlib import Path

    from cryptography.hazmat.primitives import serialization

    import master_node
    from desktop.p2p import master_node as role

    key = load_signing_key(settings)
    pubkey = None if key is None else key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw).hex()
    if role.decide(pubkey) or not master_node.master_configured():
        return None
    recorded = _recorded_signing_key() == master_node.MASTER_PUBKEY_HEX
    if not (recorded or settings.sautium_master):
        return None

    pin = master_node.MASTER_INVITE_CODE
    if recorded and settings.sautium_master:
        meant = f"its database records {pin} as its key; SAUTIUM_MASTER=1"
    elif recorded:
        meant = f"its database records {pin} as its key"
    else:
        meant = f"SAUTIUM_MASTER=1: its key is {pin}"
    # The code is made from the key and the name as given, never derived or
    # validated again: a name with a character the nickname rule refuses is
    # exactly the accident to name (and it would cost a second Argon2id)
    folder = Path(settings.p2p_identity_dir) if settings.p2p_identity_dir else None
    if pubkey is None:
        held = ("but it has no key: neither its identity folder nor P2P_USERNAME and "
                "P2P_PASSWORD give one. Check both in .env.")
    elif folder is not None and (folder / "node_ed25519.key").exists():
        try:
            info = json.loads((folder / "node_info.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = None
        name = info.get("username") if isinstance(info, dict) else None
        code = make_invite_code(name, bytes.fromhex(pubkey)) if name else f"the key {pubkey[:16]}…"
        held = f"but its identity folder holds {code}. Check that the folder is the master's."
    else:
        code = make_invite_code(settings.p2p_username, bytes.fromhex(pubkey))
        held = (f"but its credentials derive {code}. Check P2P_USERNAME and P2P_PASSWORD "
                f"in .env — one changed character is another key.")
    return (f"This backend is the Sautium master ({meant}), {held} "
            f"Nothing was written, signed or announced; not starting.")


# ---------------------------------------------------------------------------
# Identity documents on disk: certificate + proof
# ---------------------------------------------------------------------------
# The Docker node derives its KEY in memory, but the certificate (a Worker
# fact) and the proof (minutes of mining) are worth keeping: p2p_identity_dir
# is a bind mount (./data/node_identity), so both survive container
# recreation and the node never re-mines. Same file names as the launcher —
# the export/import bundle moves between the two unchanged.


def identity_dir(settings):
    from pathlib import Path
    if not settings.p2p_identity_dir:
        return None
    d = Path(settings.p2p_identity_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_certificate(settings) -> Optional[dict]:
    """The cached certificate for THIS identity, verified on every load so a
    tampered or foreign file degrades to 'no certificate'."""
    import json
    from birth_authority import verify_certificate

    d = identity_dir(settings)
    identity = resolve_identity(settings)
    if d is None or not identity:
        return None
    try:
        cert = json.loads((d / CERT_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if verify_certificate(cert) and cert["pubkey"] == identity["public_key_hex"].lower():
        return cert
    return None


def save_certificate(settings, cert: dict) -> bool:
    import json
    from birth_authority import verify_certificate

    d = identity_dir(settings)
    identity = resolve_identity(settings)
    if d is None or not identity or not verify_certificate(cert):
        return False
    if cert["pubkey"] != identity["public_key_hex"].lower():
        return False
    from desktop.node_identity import write_identity_document
    write_identity_document(d, CERT_FILENAME, json.dumps(cert, indent=2).encode("utf-8"),
                            cert["pubkey"])
    logger.info("identity cert stored (issued_at=%s method=%s)",
                cert["issued_at"], cert["method"])
    return True


_served = (None, None)       # (the certificate file's mtime_ns, its verified certificate)


def served_certificate(settings) -> Optional[dict]:
    """This node's certificate as its /health serves it — the certificate
    only, never the proof, whose mined_at the Worker does not publish. Read
    and verified once per version of the file, not on every probe."""
    global _served
    d = identity_dir(settings)
    if d is None:
        return None
    try:
        mtime = (d / CERT_FILENAME).stat().st_mtime_ns
    except OSError:
        return None
    if _served[0] != mtime:
        _served = (mtime, load_certificate(settings))
    return _served[1]


def proof_path(settings):
    from desktop.p2p.identity_proof import PROOF_FILENAME
    d = identity_dir(settings)
    return None if d is None else d / PROOF_FILENAME


def tls_binding(settings):
    """(node_pubkey_hex, sign_fn) for tls_gen.ensure_cert's peer channel
    binding — None without identity (the cert is then generated unbound, and
    made again bound the moment an account exists: main._peer_surface_follows_identity)."""
    ident = resolve_identity(settings)
    key = load_signing_key(settings)
    if not ident or key is None:
        return None
    return ident["public_key_hex"].lower(), key.sign


_witness = None


def witness_recorder(settings):
    """This process's witness record (desktop/p2p/witness.py) over db_pool:
    the introductions and handshakes the peer surface serves, the probes the
    walk and the slice cycles make, and the authors' certificates the sync's
    payloads carry, pulled or pushed. The launcher's lives on its sync
    server."""
    global _witness
    if _witness is None:
        from pathlib import Path

        from db_pool import get_conn
        from desktop.p2p import witness

        def own_keys() -> frozenset:
            ident = resolve_identity(settings) or {}
            d = Path(settings.p2p_identity_dir) if settings.p2p_identity_dir else None
            return witness.own_keys(ident.get("public_key_hex", ""), d)

        _witness = witness.Recorder(get_conn, own_keys=own_keys)
    return _witness


def peer_identity(settings):
    """This node as a peer CLIENT (wire format v1, desktop/p2p/peer_auth.py):
    signer, pubkey and a lazy {cert, proof} loader. None without identity.
    A peer's certificate on its /health goes to the witness record as a
    `probe` sighting, and an author's certificate a pulled payload carried
    as a `carried` one."""
    from desktop.p2p import identity_proof, peer_auth

    ident = resolve_identity(settings)
    key = load_signing_key(settings)
    if not ident or key is None:
        return None

    def bundle():
        cert = load_certificate(settings)
        if not cert:
            return None
        path = proof_path(settings)
        proof = identity_proof.load_proof(path) if path is not None else None
        return {"cert": cert,
                "proof": proof if identity_proof.proof_binds(proof, cert) else None}

    def witness(cert: dict, pubkey: str) -> None:
        witness_recorder(settings).note(cert, "probe", pubkey)

    def carried(cert: dict, author: str, via: str) -> None:
        witness_recorder(settings).note(cert, "carried", author, via)

    return peer_auth.PeerIdentity(pubkey=ident["public_key_hex"].lower(),
                                  sign=key.sign, cert_bundle=bundle, witness=witness,
                                  carried=carried)
