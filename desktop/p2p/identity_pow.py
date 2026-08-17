"""Identity proof-of-work — the scarcity layer of the P2P trust design.

An anonymous (`method: pow`) identity certificate is worth nothing until its
holder has spent real memory-time on it: find a nonce with
`SHA256(Argon2id(challenge, nonce)) < target(difficulty)` where every
Argon2id call touches ~2 GiB. Peers verify a presented proof with ONE
Argon2id call (seconds), cache the verdict per pubkey and never ask again
(docs/design/P2P-SYNC-INTEGRITY.md § "Proof-of-work certificates").

`difficulty` is the EXPECTED NUMBER OF ATTEMPTS (an integer E >= 1,
target = 2^256 // E), not a bit count: the price scale is continuous, so the
issuing authority can move it in small steps (golden age: seconds of work;
escalation: minutes) and every certificate pins the E it was mined under.

Why memory-hard, and why this big: plain hash puzzles give a GPU farm a
~1000x edge, and the audience owns RTX 4090s. Memory-hard work is measured
in GB·s and is shape-independent, but the per-instance footprint decides who
runs at full utilisation — at 64 MB a 24 GB card runs ~375 instances against
a CPU's dozen threads, at ~2 GiB (>= RAM per core of commodity hardware)
both sides are capacity-limited. Small instances are reserved for the
admission gate (desktop/p2p/admission.py), whose job is throttling, not
scarcity. Total work is set by `difficulty` (expected `difficulty` calls),
NOT by Argon2's time_cost, so a verifier always pays exactly one call.

The challenge is the authority's signature over the certificate: it commits
to every signed field, is deterministic (Ed25519) so a re-issued identical
certificate keeps its proof, and does not exist before issuance — nothing
can be pre-mined for a key that has no certificate yet.

Shared between the launcher and the Docker backend (bind-mounted at
/app/desktop) — one implementation, both surfaces agree on what "verified"
means. Depends only on argon2-cffi (already required for account keys).
"""

import hashlib
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

try:
    from argon2.exceptions import HashingError
except ImportError:      # the launcher installs argon2-cffi on first run
    class HashingError(Exception):
        """Placeholder until argon2-cffi is importable; never raised."""


@dataclass(frozen=True)
class PowParams:
    version: int
    memory_kib: int
    time_cost: int
    parallelism: int
    hash_len: int = 32


# Pinned per params_version; a certificate carries the version it was mined
# under, so raising the policy later never invalidates existing proofs.
POW_PARAMS = {
    1: PowParams(version=1, memory_kib=2 * 1024 * 1024, time_cost=1, parallelism=1),
}
CURRENT_PARAMS_VERSION = 1

NONCE_LEN = 16
DIGEST_BITS = 256
MAX_DIFFICULTY = 1 << 62      # sanity bound for a network-supplied value


def pow_challenge(cert: dict) -> bytes:
    """The certificate's authority signature (raw bytes) — see module doc."""
    return bytes.fromhex(cert["sig"])


def target(difficulty: int) -> int:
    """Digest values strictly below this win; one attempt wins with
    probability 1/difficulty, so `difficulty` is the expected attempt count."""
    return (1 << DIGEST_BITS) // difficulty


def pow_digest(challenge: bytes, nonce: bytes, params: PowParams) -> bytes:
    """One full memory-hard evaluation. Raises argon2.exceptions.HashingError
    when the machine cannot allocate the working set — that is a transient
    VERIFIER failure and must never be read as evidence against the prover."""
    from argon2.low_level import Type, hash_secret_raw
    raw = hash_secret_raw(
        secret=challenge,
        salt=nonce,
        time_cost=params.time_cost,
        memory_cost=params.memory_kib,
        parallelism=params.parallelism,
        hash_len=params.hash_len,
        type=Type.ID,
    )
    return hashlib.sha256(raw).digest()


def meets_target(digest: bytes, difficulty: int) -> bool:
    return int.from_bytes(digest, "big") < target(difficulty)


def pow_verify(challenge: bytes, nonce: bytes, difficulty: int,
               params: PowParams) -> bool:
    """True iff `nonce` is a valid proof for `challenge` at `difficulty`.
    The nonce arrives from the network, so its shape is checked here;
    difficulty/params come from the authority-signed certificate."""
    if len(nonce) != NONCE_LEN or not 1 <= difficulty <= MAX_DIFFICULTY:
        return False
    return meets_target(pow_digest(challenge, nonce, params), difficulty)


def pow_mine(challenge: bytes, difficulty: int, params: PowParams, *,
             stop: Optional[threading.Event] = None,
             before_attempt: Optional[Callable[[], None]] = None,
             on_attempt: Optional[Callable[[int, float], None]] = None,
             ) -> Optional[bytes]:
    """Search for a proof; returns the nonce, or None once `stop` is set.
    `stop` is honoured between attempts — a single evaluation cannot be
    interrupted, so cancellation latency is one call (seconds at 2 GiB).
    `before_attempt()` runs ahead of every evaluation and may block (an
    admission gate: memory, battery); `on_attempt(attempts, elapsed_seconds)`
    fires after every evaluation. HashingError from an evaluation propagates
    — the caller decides whether that is a pause or a failure."""
    attempts = 0
    started = time.monotonic()
    while True:
        if stop is not None and stop.is_set():
            return None
        if before_attempt is not None:
            before_attempt()
            if stop is not None and stop.is_set():
                return None
        nonce = os.urandom(NONCE_LEN)
        digest = pow_digest(challenge, nonce, params)
        attempts += 1
        if on_attempt is not None:
            on_attempt(attempts, time.monotonic() - started)
        if meets_target(digest, difficulty):
            return nonce


def completion_probability(attempts: int, difficulty: int) -> float:
    """P(a proof was found within `attempts` evaluations) — the honest
    progress figure for a geometric search (there is no percentage of a
    lottery, only the odds so far)."""
    return 1.0 - (1.0 - 1.0 / difficulty) ** attempts


def attempts_for_quantile(q: float, difficulty: int) -> int:
    """How many evaluations the q-quantile miner needs (p50/p90/p99 wait)."""
    import math
    if difficulty == 1:
        return 1
    return max(1, math.ceil(math.log(1.0 - q) / math.log(1.0 - 1.0 / difficulty)))


# ----------------------------------------------------------------------------
# Benchmark — `python -m desktop.p2p.identity_pow --bench`
# ----------------------------------------------------------------------------

def mem_available_kib() -> Optional[int]:
    """Free-for-allocation memory (MemAvailable / psutil), None if unknown."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1])
    except OSError:
        pass
    try:
        import psutil
        return psutil.virtual_memory().available // 1024
    except ImportError:
        return None


def _peak_rss_mib() -> Optional[float]:
    try:
        import resource
        import sys
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return rss / 1024 if sys.platform != "darwin" else rss / (1024 * 1024)
    except ImportError:
        try:
            import psutil
            return psutil.Process().memory_info().peak_wset / (1024 * 1024)
        except (ImportError, AttributeError):
            return None


def _cpu_label() -> str:
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    import platform
    return platform.processor() or platform.machine()


def _time_call(params: PowParams, challenge: bytes) -> float:
    nonce = os.urandom(NONCE_LEN)
    t0 = time.perf_counter()
    pow_digest(challenge, nonce, params)
    return time.perf_counter() - t0


def _bench_series(label: str, params: PowParams, repeats: int,
                  challenge: bytes) -> Optional[float]:
    need_kib = params.memory_kib + 256 * 1024
    avail = mem_available_kib()
    if avail is not None and avail < need_kib:
        print(f"  {label:<28} skipped — need {need_kib // 1024} MiB, "
              f"{avail // 1024} MiB available")
        return None
    times = [_time_call(params, challenge) for _ in range(repeats)]
    mean = sum(times) / len(times)
    gbps = params.memory_kib / 1024 / 1024 / min(times)
    print(f"  {label:<28} mean {mean:7.3f}s  min {min(times):7.3f}s  "
          f"max {max(times):7.3f}s  ({gbps:4.2f} GiB/s at best)")
    return mean


def _bench_gil(params: PowParams, challenge: bytes) -> None:
    """Two concurrent evaluations vs. two sequential ones: a ratio near 2x
    means argon2-cffi releases the GIL and a verifier thread pool scales."""
    avail = mem_available_kib()
    if avail is not None and avail < 2 * params.memory_kib + 256 * 1024:
        print("  GIL check                    skipped — not enough memory for 2 instances")
        return
    seq = _time_call(params, challenge) + _time_call(params, challenge)
    threads = [threading.Thread(target=_time_call, args=(params, challenge))
               for _ in range(2)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    par = time.perf_counter() - t0
    print(f"  GIL check ({params.memory_kib // 1024} MiB x2)"
          f"{'':<8} sequential {seq:6.3f}s  threaded {par:6.3f}s  "
          f"speedup {seq / par:4.2f}x")


def _difficulty_table(seconds_per_call: float) -> None:
    print(f"\n  difficulty E  mean      p50       p90       p99   "
          f"(minutes, at {seconds_per_call:.2f}s per 2 GiB call)")
    for e in (8, 16, 32, 64, 128, 256, 512, 1024):
        row = [e * seconds_per_call / 60]
        row += [attempts_for_quantile(q, e) * seconds_per_call / 60
                for q in (0.5, 0.9, 0.99)]
        print(f"  {e:>12}  " + "  ".join(f"{m:7.1f}" for m in row))


def _bench(repeats: int) -> None:
    challenge = os.urandom(64)
    print(f"identity_pow bench — {_cpu_label()}, {os.cpu_count()} threads, "
          f"MemAvailable "
          f"{(mem_available_kib() or 0) // 1024} MiB")
    print(f"  {'warm-up (64 MiB)':<28} {_time_call(PowParams(0, 64 * 1024, 1, 1), challenge):.3f}s")
    print("\nper-call cost:")
    _bench_series("64 MiB p=1 (gate unit w)", PowParams(0, 64 * 1024, 1, 1), repeats * 2, challenge)
    _bench_series("256 MiB p=1", PowParams(0, 256 * 1024, 1, 1), repeats, challenge)
    _bench_series("2 GiB p=4 (RFC 9106 shape)", PowParams(0, 2 * 1024 * 1024, 1, 4), repeats, challenge)
    p1 = _bench_series("2 GiB p=1 (params v1)", POW_PARAMS[1], repeats, challenge)
    print("\nthreading:")
    _bench_gil(PowParams(0, 256 * 1024, 1, 1), challenge)
    peak = _peak_rss_mib()
    if peak is not None:
        print(f"\n  peak RSS {peak:,.0f} MiB")
    if p1 is not None:
        _difficulty_table(p1)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Identity PoW primitive")
    ap.add_argument("--bench", action="store_true",
                    help="measure Argon2id call costs on this machine")
    ap.add_argument("--repeats", type=int, default=3)
    args = ap.parse_args()
    if args.bench:
        _bench(args.repeats)
    else:
        ap.print_help()
