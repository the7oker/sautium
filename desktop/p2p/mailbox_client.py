"""Master mailbox — the off-master fallback for messages TO the master
(P2P-SYNC-INTEGRITY.md § "Defense strategy", plan Ф16).

The master is the one node every install talks to (auto-friend, support
token, relay #0), which makes "the master is offline" and "the master is
being blocked" the same failure: a message for it sits in the sender's
outbox until BOTH ends are online at once. The Worker mailbox breaks that
coupling. When the direct path finds no route the sender parks the
ordinary chat wire payload — NaCl Box ciphertext to the master's chat key,
the Worker never sees plaintext — at `POST /mailbox`, signed with its own
key (`mailbox:v1:{to}:{message_uuid}:{timestamp}:{sha256(encrypted)}`)
and gated by a birth certificate issued there (no free keys) plus per-IP,
per-sender and global caps. Acceptance is delivery from the sender's side
(handed to the mailbox, like mail to an MTA); the master dedups by
`message_uuid` exactly as it does on the direct path.

The master drains on an EVENT, not a timer: it holds an outbound WebSocket
to `GET /mailbox/wake` (a hibernating socket on the mailbox's Durable
Object) and receives "mail" whenever something is stored; it drains on
that and on every (re)connect, imports each message through the same
`handle_incoming` the peer surface uses, then acks by id. Nothing here
polls; a lost socket reconnects with backoff.

Shared by the launcher (sender fallback; drain too, if its account IS the
master) and the Docker backend (the shipped master).
"""

import asyncio
import hashlib
import logging
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

WORKER_URL = "https://sautium-verify.sautium.workers.dev"
RECONNECT_MIN_S = 5.0
RECONNECT_MAX_S = 300.0
WS_HEARTBEAT_S = 30.0

Signer = Callable[[bytes], bytes]      # raw Ed25519 signature over raw bytes


def deposit_signature(sign: Signer, master_pubkey: str, message_uuid: str,
                      timestamp_iso: str, encrypted: str) -> str:
    digest = hashlib.sha256(encrypted.encode("utf-8")).hexdigest()
    message = f"mailbox:v1:{master_pubkey.lower()}:{message_uuid}:{timestamp_iso}:{digest}"
    return sign(message.encode("utf-8")).hex()


async def deposit(session, *, master_pubkey: str, sender_pubkey: str, sign: Signer,
                  encrypted: str, timestamp_iso: str, message_uuid: str,
                  worker_url: str = WORKER_URL) -> Optional[dict]:
    """Park one chat payload for the master. Returns the Worker's answer
    ({stored, id} | {duplicate}) or None when the mailbox did not take it —
    the message then stays in the outbox for the direct path, as before."""
    payload = {
        "to": master_pubkey.lower(),
        "from_public_key": sender_pubkey.lower(),
        "encrypted": encrypted,
        "timestamp": timestamp_iso,
        "message_uuid": message_uuid,
        "signature": deposit_signature(sign, master_pubkey, message_uuid, timestamp_iso, encrypted),
    }
    try:
        async with session.post(f"{worker_url}/mailbox", json=payload) as resp:
            body = await resp.json(content_type=None)
            if resp.status == 200:
                return body
            logger.warning("mailbox refused %s: %s %s", message_uuid[:8], resp.status,
                           body.get("error") if isinstance(body, dict) else body)
    except Exception as e:                       # network — the outbox keeps the message
        logger.debug("mailbox unreachable: %s", e)
    return None


class MasterMailbox:
    """The master's side: wake socket + drain + ack."""

    def __init__(self, pubkey_hex: str, sign: Signer,
                 on_message: Callable[[dict], object], *,
                 worker_url: str = WORKER_URL, clock: Callable[[], float] = time.time):
        """`on_message(m)` runs in a worker thread (it hits the DB) for every
        drained message: {message_uuid, from_public_key, encrypted,
        timestamp, received_at, id}. Raising skips the ack — the batch is
        served again on the next drain."""
        self.pubkey = pubkey_hex.lower()
        self._sign = sign
        self._on_message = on_message
        self._worker_url = worker_url
        self._clock = clock
        self._drain_lock = asyncio.Lock()
        self.drained_total = 0
        self.connected = False

    def _signed_query(self, label: str, extra: Optional[str] = None) -> str:
        ts = str(int(self._clock()))
        message = f"{label}:v1:{ts}" if extra is None else f"{label}:v1:{ts}:{extra}"
        sig = self._sign(message.encode("utf-8")).hex()
        return f"public_key_hex={self.pubkey}&ts={ts}&signature={sig}"

    async def drain(self, session) -> int:
        """Import everything parked, page by page, acking each page after its
        messages were handed to on_message. Returns the number imported."""
        imported = 0
        async with self._drain_lock:
            after = 0
            while True:
                q = self._signed_query("mailbox-drain", str(after))
                async with session.get(f"{self._worker_url}/mailbox?{q}&after={after}") as resp:
                    body = await resp.json(content_type=None)
                    if resp.status != 200:
                        logger.warning("mailbox drain refused: %s %s", resp.status, body)
                        return imported
                messages = body.get("messages") or []
                if not messages:
                    return imported
                for m in messages:
                    await asyncio.to_thread(self._on_message, m)
                imported += len(messages)
                self.drained_total += len(messages)
                last_id = int(messages[-1]["id"])
                q = self._signed_query("mailbox-ack", str(last_id))
                async with session.delete(f"{self._worker_url}/mailbox?{q}&upto={last_id}") as resp:
                    if resp.status != 200:
                        logger.warning("mailbox ack refused: %s", resp.status)
                        return imported
                logger.info("mailbox: %d message(s) drained", len(messages))
                after = last_id
                if not body.get("more"):
                    return imported

    async def run(self, running: Callable[[], bool]) -> None:
        """Hold the wake socket while `running()`; drain on connect and on
        every "mail"; reconnect with backoff."""
        import aiohttp
        backoff = RECONNECT_MIN_S
        while running():
            try:
                # gzip only: aiohttp advertises brotli whenever a Brotli module is
                # importable, and an old Brotli beside a new aiohttp cannot decode
                # what the edge then sends (seen on the dev host).
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=15),
                                                 headers={"Accept-Encoding": "gzip"}) as session:
                    q = self._signed_query("mailbox-wake")
                    async with session.ws_connect(f"{self._worker_url}/mailbox/wake?{q}",
                                                  heartbeat=WS_HEARTBEAT_S) as ws:
                        self.connected = True
                        backoff = RECONNECT_MIN_S
                        logger.info("mailbox wake socket connected")
                        await self.drain(session)
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT and msg.data == "mail":
                                await self.drain(session)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("mailbox wake socket: %s", e)
            finally:
                self.connected = False
            if not running():
                return
            await asyncio.sleep(backoff)
            backoff = min(RECONNECT_MAX_S, backoff * 2)
