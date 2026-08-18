"""Identity proof — the mined companion of a `method: pow` certificate, and
the worker that keeps a node's own proof current.

A proof `{v, pubkey, cert_sig, nonce, difficulty, params_version, mined_at}`
binds to exactly one certificate through `cert_sig` (the PoW challenge, see
identity_pow.py). It is stored next to the certificate, travels with it
(export/import), is presented beside it at first contact, and a peer checks
it with one Argon2id call before caching the verdict for good.

`ensure_identity_proof` is the shared policy for both surfaces (launcher
thread, Docker startup task):

- `method: email` needs no work — the state is `ready` at once;
- a stored proof that binds to the certificate is re-verified ONCE (one
  call, in this background worker, never on a request path): a corrupt file
  must never be presented, because a failed proof blacklists its presenter;
- otherwise mine in the background at below-normal thread priority behind
  an admission gate that runs before EVERY attempt: >= 2.5 GiB available
  memory (the 2 GiB working set beside a resident ML stack is the real
  constraint, not CPU) and not on battery. There is no notification API
  for either condition, so a paused worker re-checks on a fixed cadence
  through `stop.wait()` — cancellable, and not a race being papered over.
  `HashingError` (allocation failure) is the same pause, not an error.

Staged upgrades (grace policy, 2026-08-18). A re-signed certificate (a
version bump, an email upgrade) invalidates the proof mined over the old
signature. Peers accept the previous format for a grace period, so the
old (certificate, proof) pair stays PRESENTABLE — `stage_certificate`
keeps it as the current pair and parks the fresh pow certificate in
`birth_certificate.next.json`; `run_worker` mines for the staged one and
promotes it (proof written, then the certificate renamed into place) only
when the new proof is ready. An email certificate needs no proof and is
adopted at once; a node without a complete old pair adopts the fresh one
directly (nothing to protect). Result: a bump never leaves an identity
without a valid pair, and a crash between the two promotion writes is
healed on the next start (a stored proof that binds to the staged
certificate promotes without mining).

Depends only on identity_pow (argon2-cffi); psutil is optional and used
solely for the battery check.
"""

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from desktop.p2p import identity_pow

logger = logging.getLogger(__name__)

PROOF_VERSION = 1
PROOF_FILENAME = "identity_proof.json"
NEXT_CERT_SUFFIX = ".next.json"
MEM_GUARD_KIB = 2560 * 1024          # 2 GiB working set + headroom
PAUSE_SECONDS = 30.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_proof(cert: dict, nonce: bytes) -> dict:
    return {
        "v": PROOF_VERSION,
        "pubkey": cert["pubkey"],
        "cert_sig": cert["sig"],
        "nonce": nonce.hex(),
        "difficulty": cert["difficulty"],
        "params_version": cert["params_version"],
        "mined_at": _now_iso(),
    }


def proof_binds(proof: Optional[dict], cert: dict) -> bool:
    """Structural binding only (no Argon2): same certificate, same policy,
    well-formed nonce. Cheap enough for request paths."""
    if not isinstance(proof, dict) or proof.get("v") != PROOF_VERSION:
        return False
    nonce = proof.get("nonce")
    return (
        proof.get("pubkey") == cert.get("pubkey")
        and proof.get("cert_sig") == cert.get("sig")
        and proof.get("difficulty") == cert.get("difficulty")
        and proof.get("params_version") == cert.get("params_version")
        and isinstance(nonce, str)
        and len(nonce) == 2 * identity_pow.NONCE_LEN
        and all(c in "0123456789abcdef" for c in nonce)
    )


def verify_proof(proof: dict, cert: dict,
                 params: Optional[identity_pow.PowParams] = None) -> bool:
    """Binding + one memory-hard evaluation. Raises HashingError when the
    verifier cannot allocate — transient, never evidence against anyone."""
    if not proof_binds(proof, cert):
        return False
    params = params or identity_pow.POW_PARAMS.get(cert["params_version"])
    if params is None:
        return False
    return identity_pow.pow_verify(identity_pow.pow_challenge(cert),
                                   bytes.fromhex(proof["nonce"]),
                                   cert["difficulty"], params)


def load_proof(path: Path) -> Optional[dict]:
    try:
        proof = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return proof if isinstance(proof, dict) and proof.get("v") == PROOF_VERSION else None


def save_proof(path: Path, proof: dict) -> None:
    Path(path).write_text(json.dumps(proof, indent=2), encoding="utf-8")


def next_cert_path(cert_path: Path) -> Path:
    cert_path = Path(cert_path)
    return cert_path.with_name(cert_path.stem + NEXT_CERT_SUFFIX)


def _load_cert(path: Path, verify: Callable[[dict], bool], own_pubkey: Optional[str]) -> Optional[dict]:
    try:
        cert = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(cert, dict) or not verify(cert):
        return None
    if own_pubkey and cert.get("pubkey", "").lower() != own_pubkey.lower():
        return None
    return cert


def _write_json(path: Path, value: dict) -> None:
    Path(path).write_text(json.dumps(value, indent=2), encoding="utf-8")


def stage_certificate(current: Optional[dict], fetched: dict, cert_path: Path,
                      proof_path: Path, verify: Callable[[dict], bool]) -> dict:
    """A fresh certificate arrived from the Worker: decide what this node
    PRESENTS from now on and persist accordingly. Returns the presented
    certificate. `current` is the verified stored certificate (or None).

    - nothing stored / not verifiable, or the same signature: adopt/keep;
    - email (no proof needed): adopt at once, drop any staged file;
    - pow with a complete old pair (stored proof binds to `current`): keep
      the old pair current, stage the fresh certificate for the worker;
    - pow without a complete old pair: adopt at once (nothing to protect)."""
    cert_path, proof_path = Path(cert_path), Path(proof_path)
    staged = next_cert_path(cert_path)
    if not verify(fetched):
        return current if current is not None else fetched
    if current is not None and current.get("sig") == fetched.get("sig"):
        return current
    if fetched["method"] != "pow" or current is None:
        _write_json(cert_path, fetched)
        if staged.exists():
            staged.unlink()
        logger.info("identity cert %s (issued_at=%s method=%s v%s)",
                    "adopted" if current is None else "upgraded",
                    fetched["issued_at"], fetched["method"], fetched.get("v"))
        return fetched
    if proof_binds(load_proof(proof_path), current):
        _write_json(staged, fetched)
        logger.info("identity cert v%s staged behind the current pair (v%s) until its proof is mined",
                    fetched.get("v"), current.get("v"))
        return current
    _write_json(cert_path, fetched)
    if staged.exists():
        staged.unlink()
    logger.info("identity cert upgraded to v%s (no complete pair to keep)", fetched.get("v"))
    return fetched


def promote_staged(cert_path: Path) -> Optional[dict]:
    """The staged certificate becomes the current one (its proof is on
    disk). Returns it, or None when nothing was staged."""
    cert_path = Path(cert_path)
    staged = next_cert_path(cert_path)
    if not staged.exists():
        return None
    try:
        cert = json.loads(staged.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        staged.unlink()
        return None
    os.replace(staged, cert_path)
    logger.info("identity cert v%s promoted — proof ready", cert.get("v"))
    return cert


def run_worker(cert_path: Path, proof_path: Path, *, verify: Callable[[dict], bool],
               own_pubkey: Optional[str], stop: threading.Event,
               on_state: Callable[[dict], None], **kwargs) -> Optional[dict]:
    """The node's identity worker: mine for a STAGED certificate and promote
    it when its proof is ready, otherwise keep the current certificate's
    proof ready (ensure_identity_proof). Returns the final proof or None."""
    cert_path, proof_path = Path(cert_path), Path(proof_path)
    current = _load_cert(cert_path, verify, own_pubkey)
    staged = _load_cert(next_cert_path(cert_path), verify, own_pubkey)
    if staged is None:
        if next_cert_path(cert_path).exists():
            next_cert_path(cert_path).unlink()            # unverifiable leftovers
        if current is None:
            on_state({"status": "stopped", "detail": "no certificate", "updated_at": _now_iso()})
            return None
        return ensure_identity_proof(current, proof_path, stop=stop, on_state=on_state, **kwargs)
    if current is None or current.get("sig") == staged.get("sig"):
        promote_staged(cert_path)                          # nothing to keep behind it
        return ensure_identity_proof(staged, proof_path, stop=stop, on_state=on_state, **kwargs)
    # The old pair stays presentable while this runs — the UI shows an
    # upgrade in progress, not a proof-less identity.
    proof = ensure_identity_proof(staged, proof_path, stop=stop,
                                  on_state=lambda st: on_state({**st, "upgrade": True}), **kwargs)
    if proof is not None and proof_binds(proof, staged):
        promote_staged(cert_path)
        on_state({"status": "ready", "detail": "proof", "method": staged["method"],
                  "difficulty": staged["difficulty"], "params_version": staged["params_version"],
                  "proof_mined_at": proof.get("mined_at"), "updated_at": _now_iso()})
    return proof


def _lower_thread_priority() -> None:
    """Best effort: mining must never compete with playback or the UI."""
    try:
        if sys.platform == "win32":
            import ctypes
            THREAD_PRIORITY_BELOW_NORMAL = -1
            ctypes.windll.kernel32.SetThreadPriority(
                ctypes.windll.kernel32.GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL)
        else:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
    except (OSError, AttributeError) as e:
        logger.debug("thread priority unchanged: %s", e)


def on_battery() -> bool:
    try:
        import psutil
        batt = psutil.sensors_battery()
    except (ImportError, AttributeError, OSError):
        return False
    return bool(batt is not None and batt.power_plugged is False)


def ensure_identity_proof(
    cert: dict,
    proof_path: Path,
    *,
    stop: threading.Event,
    on_state: Callable[[dict], None],
    params: Optional[identity_pow.PowParams] = None,
    mem_available_kib: Callable[[], Optional[int]] = identity_pow.mem_available_kib,
    mem_guard_kib: int = MEM_GUARD_KIB,
    battery: Callable[[], bool] = on_battery,
    hold: Callable[[], Optional[str]] = lambda: None,
    pause_seconds: float = PAUSE_SECONDS,
) -> Optional[dict]:
    """Bring this node's proof for `cert` to a ready state (see module doc).
    Runs in the caller's thread until ready or `stop`; returns the proof, or
    None when nothing is needed (email) or the worker was stopped.
    `on_state(state)` receives every state change plus one update per
    mining attempt — the caller publishes it (user_settings + NOTIFY).
    `hold()` is an external pause reason (the load meter's "playback"),
    checked by the same per-attempt gate as memory and battery."""
    _lower_thread_priority()
    started = _now_iso()
    state = {"method": cert["method"], "difficulty": cert["difficulty"],
             "params_version": cert["params_version"], "attempts": 0,
             "p_done": 0.0, "started_at": started}

    def emit(status: str, detail: Optional[str] = None, **extra) -> None:
        state.update(status=status, detail=detail, updated_at=_now_iso(), **extra)
        on_state(dict(state))

    if cert["method"] != "pow":
        emit("ready", "email")
        return None

    params = params or identity_pow.POW_PARAMS.get(cert["params_version"])
    if params is None:
        emit("stopped", f"unknown params_version {cert['params_version']}")
        return None
    challenge = identity_pow.pow_challenge(cert)
    difficulty = cert["difficulty"]

    stored = load_proof(proof_path)
    if stored is not None and proof_binds(stored, cert):
        emit("checking")
        while not stop.is_set():
            try:
                if verify_proof(stored, cert, params):
                    emit("ready", "proof", proof_mined_at=stored.get("mined_at"))
                    return stored
                logger.warning("stored identity proof does not verify — re-mining")
                break
            except identity_pow.HashingError:
                emit("paused", "memory")
                stop.wait(pause_seconds)
        if stop.is_set():
            emit("stopped")
            return None

    def gate() -> None:
        reason = None
        while not stop.is_set():
            avail = mem_available_kib()
            external = hold()
            if avail is not None and avail < mem_guard_kib:
                new_reason = "memory"
            elif battery():
                new_reason = "battery"
            elif external:
                new_reason = external
            else:
                break
            if new_reason != reason:
                reason = new_reason
                emit("paused", reason)
            stop.wait(pause_seconds)
        if reason is not None and not stop.is_set():
            emit("mining")

    attempts_before = 0

    def progress(n: int, _elapsed: float) -> None:
        total = attempts_before + n
        emit("mining", attempts=total,
             p_done=round(identity_pow.completion_probability(total, difficulty), 4))

    emit("mining")
    while not stop.is_set():
        try:
            nonce = identity_pow.pow_mine(challenge, difficulty, params, stop=stop,
                                          before_attempt=gate, on_attempt=progress)
        except identity_pow.HashingError:
            attempts_before = state["attempts"]
            emit("paused", "memory")
            stop.wait(pause_seconds)
            continue
        if nonce is None:
            break
        proof = make_proof(cert, nonce)
        save_proof(proof_path, proof)
        emit("ready", "proof", proof_mined_at=proof["mined_at"])
        logger.info("identity proof mined after %d attempts (E=%d)",
                    state["attempts"], difficulty)
        return proof

    emit("stopped")
    return None
