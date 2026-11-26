"""
Signed invite delivery for Sautium, through the Cloudflare Worker.

(The email's verification is the backend's: routers/p2p's email routes, on
both runtimes — the launcher's wizard verifies nothing since SEC-S5: its
code waits for the account's key, which is made in the background.)

- Invite: the Web UI's route (backend/routers/p2p.py) signs the request;
  the Worker mails a single-use token under the verified badge. The
  inviter's pickup (pick_up) adds whoever accepted, and brings every add of
  the code by plain code for the person's Add back.
- Every route is a key's own code's, and the Worker answers only a key it
  issued a certificate to.

All state-changing requests are signed with the user's Ed25519 private key.
The Worker verifies the signature, so a modified client can't impersonate
others.
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from desktop.p2p import chat_wire

logger = logging.getLogger(__name__)

VERIFY_WORKER_URL = "https://sautium-verify.sautium.workers.dev"

_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Sautium/1.0",
}


def _post_worker(path: str, payload: dict) -> Optional[dict]:
    """POST to the Worker API. Returns response dict or None on failure."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{VERIFY_WORKER_URL}{path}",
            method="POST",
            data=data,
            headers=_HEADERS,
        )
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        # A busy Worker is an anomaly the next try recovers from, not a failure.
        (logger.warning if e.code == 429 else logger.error)(f"Worker {path} failed ({e.code}): {body}")
        return None
    except Exception as e:
        logger.error(f"Worker {path} failed: {e}")
        return None


# -------------------------------------------------------------------
# The pickup (one function, three triggers: the Friends screen on either
# runtime, the launcher's start, a peer's nudge)
# -------------------------------------------------------------------

# Accepted invites, and adds, one pickup takes (the Worker's page of each).
PICKUP_PAGE = 300


def is_open_invite(conn, invite_id: str) -> bool:
    """`invite_id` is one of this node's invites, mailed, its acceptance not
    taken yet, and young enough for the Worker to still hold it
    (chat_wire.INVITE_PICKUP_DAYS)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 1 FROM sent_invites
             WHERE invite_id = %s AND taken_at IS NULL
               AND sent_at > now() - make_interval(days => %s)
        """, (invite_id, chat_wire.INVITE_PICKUP_DAYS))
        return cur.fetchone() is not None


def pick_up(conn, invite_code: str, public_key_hex: str, sign) -> dict:
    """Ask the Worker for this node's accepted invites and the adds of its
    code. Every acceptance that holds up adds its acceptor
    (chat_wire.take_acceptance) and every add that holds up waits for the
    person's Add back (chat_wire.take_add), each in a transaction of its
    own → {"added": [codes of the contacts added], "requested": [codes of
    the new requests]}. The Worker deletes nothing, so a node of the same
    account, a restore or a pickup lost on its way loses nothing.
    `sign(bytes) -> bytes` is this node's key; `conn` a connection the
    caller holds."""
    ts = int(time.time())
    result = _post_worker("/pending-accepts", {
        "invite_code": invite_code,
        "public_key_hex": public_key_hex,
        "ts": ts,
        "signature": sign(chat_wire.pickup_text(ts, invite_code, public_key_hex).encode("utf-8")).hex(),
    })
    if not isinstance(result, dict):
        return {"added": [], "requested": []}
    accepts, adds = result.get("accepts"), result.get("adds")
    added, requested = [], []
    for accept in accepts[:PICKUP_PAGE] if isinstance(accepts, list) else ():
        outcome = _take(conn, "an acceptance", lambda: chat_wire.take_acceptance(
            conn, accept, invite_code, public_key_hex))
        if outcome == "added":
            added.append(accept["acceptor"]["invite_code"])
            logger.info("Pickup: the invite accepted by %s added", accept["acceptor"]["invite_code"])
        elif outcome not in (None, "taken", "held", "not an invite of this node"):
            logger.warning("Pickup: an acceptance skipped — %s", outcome)
    # An old Worker answers no adds: nothing to take, as before it kept them.
    for add in adds[:PICKUP_PAGE] if isinstance(adds, list) else ():
        outcome = _take(conn, "an add", lambda: chat_wire.take_add(conn, add, invite_code, public_key_hex))
        if outcome == "requested":
            requested.append(add["adder"]["invite_code"])
            logger.info("Pickup: %s added this node's code — waiting for Add back", add["adder"]["invite_code"])
        elif outcome in ("own key", "not this node's code", "a key a contact retired"):
            # our own key under another name, a code typed with our digest
            # under a name that is not ours, or a friend's add from before
            # its rotation: nobody's request, every pickup for 30 days
            logger.debug("Pickup: an add skipped — %s", outcome)
        elif outcome not in (None, "renewed", "dismissed", "answered", "held"):
            logger.warning("Pickup: an add skipped — %s", outcome)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM sent_invites WHERE sent_at < now() - make_interval(days => %s)",
                    (chat_wire.INVITE_PICKUP_DAYS,))
    return {"added": added, "requested": requested}


def _take(conn, what: str, step) -> Optional[str]:
    """One entry of a pickup's page in a transaction of its own → its
    outcome, or None when it failed: its failure is its own, and the rest of
    the page, and the sweep after it, go on — but not past a connection
    that is gone."""
    try:
        return _in_transaction(conn, step)
    except Exception:
        if conn.closed:
            raise
        logger.exception("Pickup: %s failed", what)
        return None


def _in_transaction(conn, step):
    """`step()` committed as one transaction, whatever mode `conn` is in."""
    autocommit = conn.autocommit
    conn.autocommit = False
    try:
        outcome = step()
        conn.commit()
        return outcome
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.autocommit = autocommit
