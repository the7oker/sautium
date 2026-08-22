"""Capability directory client — Ф16c, the generalization of master_hint.

When the DHT is dead (mobile carriers throttle UDP) every role — MB
search, slices, sync, relays — used to funnel onto the master through its
hint. Reachable volunteer nodes now list themselves in the Worker's
directory (address observed by the edge, port and capabilities inside the
node's signature, certificate required, single-use signatures), and a
bootstrapping client asks for K RANDOM fresh volunteers per capability.
The master stays a SEPARATE, LAST tier (master_hint) — that ordering is
the de-specialization.

Discovery, never trust: a hint is an address candidate. Health probes,
node_id checks, ban lists, per-artist slice signatures and the relay
voucher protocol judge it exactly like a DHT-found peer; a fake
registration costs the client one probe.

`fetch(cap)` is cached per capability (TTL, failure backoff) and blocking
— async callers run it in a thread. `register(...)` is the volunteer
side, called from the node's existing periodic cycles.
"""

import json
import logging
import threading
import time
import urllib.request
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

HINTS_TTL_S = 600.0
NEGATIVE_TTL_S = 60.0
FETCH_TIMEOUT_S = 6.0
CAPS = ("sync", "mbdump", "relay", "mbslices")

_lock = threading.Lock()
_cache: dict = {}                  # cap → (nodes, fetched_at)
_failed_at: dict = {}              # cap → monotonic


def _worker_url() -> str:
    from desktop.p2p.email_verify import VERIFY_WORKER_URL
    return VERIFY_WORKER_URL


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Sautium/1.0",
                                               "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch(cap: str, force: bool = False, _get=None) -> List[Tuple[str, int, str]]:
    """[(host, port, pubkey)] of fresh volunteers for a capability —
    possibly empty. v4 slot preferred; a v6-only entry is still dialable
    (fmt_addr brackets it)."""
    from desktop.p2p.master_node import master_configured
    if cap not in CAPS or not master_configured():
        return []
    now = time.monotonic()
    with _lock:
        if not force:
            hit = _cache.get(cap)
            if hit is not None and now - hit[1] < HINTS_TTL_S:
                return hit[0]
            if now - _failed_at.get(cap, -1e9) < NEGATIVE_TTL_S:
                return hit[0] if hit else []
        try:
            body = (_get or globals()["_get"])(f"{_worker_url()}/node-hints?cap={cap}")
        except Exception as e:
            logger.debug("node hints fetch failed for %s: %s", cap, e)
            _failed_at[cap] = now
            hit = _cache.get(cap)
            return hit[0] if hit else []
        nodes = []
        for n in (body.get("nodes") or []):
            host, port = n.get("host") or n.get("host6"), n.get("port")
            pubkey = str(n.get("pubkey") or "")
            if isinstance(host, str) and host and isinstance(port, int) and 0 < port < 65536:
                nodes.append((host, port, pubkey))
        _cache[cap] = (nodes, now)
        _failed_at.pop(cap, None)
        return nodes


def registration_signature(sign: Callable[[bytes], bytes], port: int,
                           caps: List[str], ts: int) -> Tuple[str, List[str]]:
    caps = sorted(set(caps))
    message = f"sautium-directory:v1:{int(port)}:{','.join(caps)}:{int(ts)}"
    return sign(message.encode("utf-8")).hex(), caps


def register(pubkey: str, sign: Callable[[bytes], bytes], port: int,
             caps: List[str]) -> bool:
    """One signed registration (the volunteer's side). Blocking — callers
    run it from their periodic cycle in an executor. False on any refusal;
    the next cycle retries, so failures need no handling here."""
    ts = int(time.time())
    sig, caps = registration_signature(sign, port, caps, ts)
    payload = json.dumps({"pubkey": pubkey.lower(), "port": int(port),
                          "capabilities": caps, "ts": ts, "signature": sig}).encode("utf-8")
    req = urllib.request.Request(
        f"{_worker_url()}/node-register", data=payload, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "Sautium/1.0",
                 "Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
            ok = bool(json.loads(resp.read().decode("utf-8")).get("registered"))
        if ok:
            logger.info("directory: registered %s as %s", pubkey[:8], "+".join(caps))
        return ok
    except Exception as e:
        logger.debug("directory registration failed: %s", e)
        return False
