"""Admission gate primitives — Mode A (sampling gate) of the per-event
pricing layer, wire format v1 (P2P-SYNC-INTEGRITY.md §§ "Admission gate",
"Wire format v1"; vectors in tests/p2p/vectors/peer_wire_v1.json `gate`).

The gate prices EVENTS, not identities (that is identity_pow.py). A server
hands a client a quote — n opaque 32-byte task inputs under a short-TTL,
client-bound signature — the client answers every task with one 64 MiB
Argon2id evaluation each (~40 ms here, "w", the unit of price), and the
server checks only R of the fresh answers, chosen AFTER submission (so
there is nothing to grind): honest pass costs the client n·w and the
server R·w; solving a fraction f passes with probability f^R, i.e. costs
W/f^(R−1) per success — a loss for R ≥ 2. Small instances are exactly what
this layer wants: its job is throttling, not scarcity.

What this module fixes (the rest of the gate — pool, pricing, dormancy —
builds on it in later phases):

- fresh task inputs are DERIVED, not stored:
  `HMAC-SHA256(gate_secret, "gate-fresh:v1" ‖ nonce ‖ client_pubkey ‖ i₂)`
  — client-bound and single-use by construction, zero server state;
- pool tasks (gold/silver, phase Ф11) will be leased into the same packet
  and the positions shuffled by `HMAC(gate_secret, nonce)` — the client
  cannot tell which answers are checked by memcmp, which by sampling;
- one answer function for every task on every connection (so a pool
  answer stays valid across connections):
  `Argon2id(secret = input, salt = "sautium-gate:v1", 64 MiB, t = 1, p = 1, 32 B)`;
- the quote core `{v, server, client, nonce, issued, deadline,
  price_version, params_version, n, tasks_digest}` signed by the server
  key over `sautium-gate-quote:v1:` + canonical JSON (sorted keys,
  compact); the payment `{quote, sig, answers}` rides the priced request
  as `X-Sautium-Gate` (base64url, unpadded).

Shared by both peer surfaces and both clients through the bind mount.
Depends on identity_pow (argon2-cffi) and `cryptography` for the quote
signature.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional, Sequence

from desktop.p2p import identity_pow
from desktop.p2p.identity_pow import PowParams

GATE_PARAMS = {
    1: PowParams(version=1, memory_kib=64 * 1024, time_cost=1, parallelism=1),
}
CURRENT_GATE_PARAMS_VERSION = 1
GATE_SALT = b"sautium-gate:v1"
FRESH_PREFIX = b"gate-fresh:v1"
SHUFFLE_PREFIX = b"gate-shuffle:v1"
QUOTE_PREFIX = "sautium-gate-quote:v1:"
QUOTE_VERSION = 1
NONCE_LEN = 16
TASK_LEN = 32
HDR_GATE = "X-Sautium-Gate"
DEFAULT_SAMPLE = 2                       # R
QUOTE_BASE_TTL = 30.0                    # seconds — placeholder, calibrated in Ф8/Ф10
QUOTE_TTL_PER_TASK = 0.5


# ----------------------------------------------------------------------------
# tasks
# ----------------------------------------------------------------------------

def fresh_input(gate_secret: bytes, nonce: bytes, client_pubkey: str, i: int) -> bytes:
    return hmac.new(gate_secret,
                    FRESH_PREFIX + nonce + bytes.fromhex(client_pubkey) + i.to_bytes(2, "big"),
                    hashlib.sha256).digest()


def derive_fresh_inputs(gate_secret: bytes, nonce: bytes, client_pubkey: str, n: int) -> list:
    return [fresh_input(gate_secret, nonce, client_pubkey, i) for i in range(n)]


def shuffle_positions(gate_secret: bytes, nonce: bytes, n: int) -> list:
    """Deterministic permutation of n packet positions (Fisher–Yates driven
    by an HMAC counter): the server recomputes it, the client cannot."""
    order = list(range(n))
    counter = 0
    for i in range(n - 1, 0, -1):
        block = hmac.new(gate_secret, SHUFFLE_PREFIX + nonce + counter.to_bytes(4, "big"),
                         hashlib.sha256).digest()
        counter += 1
        j = int.from_bytes(block[:8], "big") % (i + 1)
        order[i], order[j] = order[j], order[i]
    return order


def solve(task_input: bytes, params: PowParams = GATE_PARAMS[1]) -> bytes:
    """One task = one 64 MiB Argon2id evaluation. Raises HashingError when
    the machine cannot allocate (transient, like everywhere else)."""
    from argon2.low_level import Type, hash_secret_raw
    return hash_secret_raw(secret=task_input, salt=GATE_SALT, time_cost=params.time_cost,
                           memory_cost=params.memory_kib, parallelism=params.parallelism,
                           hash_len=TASK_LEN, type=Type.ID)


def solve_all(inputs: Sequence[bytes], params: PowParams = GATE_PARAMS[1],
              threads: int = 4) -> list:
    """Client side: every task, in a small thread pool (argon2-cffi releases
    the GIL; 4 × 64 MiB is a bounded, brief working set)."""
    if len(inputs) <= 1 or threads <= 1:
        return [solve(t, params) for t in inputs]
    with ThreadPoolExecutor(max_workers=min(threads, len(inputs))) as pool:
        return list(pool.map(lambda t: solve(t, params), inputs))


def tasks_digest(inputs: Sequence[bytes]) -> str:
    return hashlib.sha256(b"".join(inputs)).hexdigest()


def sample_indices(n: int, r: int = DEFAULT_SAMPLE) -> list:
    """Server randomness, drawn AFTER the answers arrived — never derivable
    from the quote, so a cheater cannot pick which tasks to solve."""
    r = min(r, n)
    return sorted(secrets.SystemRandom().sample(range(n), r)) if r > 0 else []


def verify_sampled(inputs: Sequence[bytes], answers: Sequence[bytes], indices: Sequence[int],
                   params: PowParams = GATE_PARAMS[1]) -> bool:
    """Recompute R positions; a single mismatch fails the whole packet."""
    if len(answers) != len(inputs):
        return False
    for i in indices:
        if len(answers[i]) != TASK_LEN or not hmac.compare_digest(solve(inputs[i], params), answers[i]):
            return False
    return True


def cheat_pass_probability(solved_fraction: float, r: int = DEFAULT_SAMPLE) -> float:
    return solved_fraction ** r


# ----------------------------------------------------------------------------
# quote + payment encoding
# ----------------------------------------------------------------------------

def quote_deadline(issued: int, n: int) -> int:
    return int(issued + QUOTE_BASE_TTL + QUOTE_TTL_PER_TASK * n)


def build_quote_core(server_pubkey: str, client_pubkey: str, nonce: bytes, inputs: Sequence[bytes],
                     *, issued: Optional[int] = None, deadline: Optional[int] = None,
                     price_version: int = 1,
                     params_version: int = CURRENT_GATE_PARAMS_VERSION) -> dict:
    issued = int(time.time()) if issued is None else int(issued)
    return {
        "v": QUOTE_VERSION,
        "server": server_pubkey.lower(),
        "client": client_pubkey.lower(),
        "nonce": nonce.hex(),
        "issued": issued,
        "deadline": quote_deadline(issued, len(inputs)) if deadline is None else int(deadline),
        "price_version": int(price_version),
        "params_version": int(params_version),
        "n": len(inputs),
        "tasks_digest": tasks_digest(inputs),
    }


def quote_message(core: dict) -> bytes:
    return (QUOTE_PREFIX + json.dumps(core, sort_keys=True, separators=(",", ":"))).encode("utf-8")


def sign_quote(sign: Callable[[bytes], bytes], core: dict) -> str:
    return sign(quote_message(core)).hex()


def verify_quote(core: dict, sig_hex: str, server_pubkey: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        if not isinstance(core, dict) or core.get("v") != QUOTE_VERSION:
            return False
        if core.get("server", "").lower() != server_pubkey.lower():
            return False
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(server_pubkey)).verify(
            bytes.fromhex(sig_hex), quote_message(core))
        return True
    except (InvalidSignature, ValueError, TypeError, KeyError):
        return False


def encode_submission(core: dict, sig_hex: str, answers: Sequence[bytes]) -> str:
    raw = json.dumps({"quote": core, "sig": sig_hex, "answers": [a.hex() for a in answers]},
                     sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_submission(value: str) -> Optional[dict]:
    """{"quote": core, "sig": hex, "answers": [bytes…]} or None if malformed."""
    try:
        padded = value + "=" * (-len(value) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        core, sig, answers = data["quote"], data["sig"], data["answers"]
        if not isinstance(core, dict) or not isinstance(sig, str) or not isinstance(answers, list):
            return None
        return {"quote": core, "sig": sig, "answers": [bytes.fromhex(a) for a in answers]}
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


# ----------------------------------------------------------------------------
# python -m desktop.p2p.admission --bench
# ----------------------------------------------------------------------------

def _bench(repeats: int) -> None:
    import os
    import threading
    params = GATE_PARAMS[1]
    inputs = [os.urandom(TASK_LEN) for _ in range(20)]
    solve(inputs[0], params)                                     # warm-up
    t0 = time.perf_counter()
    for _ in range(repeats):
        for t in inputs[:5]:
            solve(t, params)
    single = (time.perf_counter() - t0) / (5 * repeats)
    print(f"64 MiB task, single thread: {single * 1000:.1f} ms  (w)")
    for threads in (4, 8):
        t0 = time.perf_counter()
        solve_all(inputs, params, threads=threads)
        wall = time.perf_counter() - t0
        print(f"20 tasks, {threads} threads: {wall * 1000:.0f} ms wall  "
              f"({wall / 20 * 1000:.1f} ms per task effective, "
              f"{single * 20 / wall:.1f}x over serial)")
    peak = identity_pow._peak_rss_mib()
    if peak is not None:
        print(f"peak RSS {peak:,.0f} MiB")
    print(f"honest packet n=20: client {single * 20 * 1000:.0f} ms, server R=2 {single * 2 * 1000:.0f} ms; "
          f"cheater f=0.5 passes {cheat_pass_probability(0.5):.0%}, "
          f"cost per success {1 / cheat_pass_probability(0.5) * 0.5:.1f}× honest")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Admission gate primitives")
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--repeats", type=int, default=3)
    args = ap.parse_args()
    if args.bench:
        _bench(args.repeats)
    else:
        ap.print_help()
