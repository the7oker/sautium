"""Support diagnostics on a node — the one worker both runtimes run
(P2P_NETWORK.md § "Support diagnostics"; protocol in diag_protocol.py, the
node's state machine in diag_events.py, the bundle in desktop/diag_bundle.py).

Moved out of the launcher's P2PManager on 2026-11-21 (B-99), as B-91 moved
the chat client: the launcher composes it, and the Docker backend builds it
beside its chat client (backend/diag_node.py) — never a second copy of a
state machine whose correctness rests on one row per warrant and one
decision per request. What differs per runtime goes in: the node's key
(`identity`), where boxed bundles wait (`stage_dir`), how the master is
found (`find_master`, the chat client's), and what a bundle is collected
from (`collect`, over that runtime's diag_bundle.Sources).

One sequential consumer of one queue — warrants from the master's wake
stream, the stream's reconnects (resume), the event recorder's NOTIFY
(report), the person's decision on the request sheet (decided) — on purpose:
a warrant re-delivered while its first copy is still collecting lands behind
it and becomes the no-op the state machine makes of it. A job that fails
leaves its state in the database (diag_warrants.error, unreported rows); the
worker lives on. Its database calls run on ONE connection of its own, on one
thread (the chat client's store pattern): the gate, the state machine, the
settings it writes, the NOTIFYs it sends.
"""

import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Optional

import psycopg2

from desktop.p2p import diag_events, diag_protocol, master_node, peer_auth
from desktop.p2p.addrs import fmt_addr

logger = logging.getLogger(__name__)

# The statuses of a bundle's upload that no retry changes: its warrant closes
FINAL_REFUSALS = (403, 404, 409, 410, 413)


@dataclass(frozen=True)
class DiagIdentity:
    """The node's account as the worker speaks it: its key, a signer, and the
    box to the master (the raw key stays with the runtime)."""
    public_key_hex: str
    sign: Callable[[bytes], bytes]
    seal: Callable[[bytes], bytes]


def seal_with(seed: bytes) -> Callable[[bytes], bytes]:
    """The box to the master from a key's seed — the master read at the
    moment of sealing, as everything here reads it."""
    return lambda data: diag_protocol.encrypt_to(seed, master_node.MASTER_PUBKEY_HEX, data)


class DiagWorker:
    """Warrants answered, bundles uploaded, event reports shipped for one node."""

    def __init__(self, db_dsn: str, *,
                 identity: Callable[[], Optional[DiagIdentity]],
                 stage_dir: Path,
                 find_master: Callable[[dict], Awaitable[list]],
                 collect: Callable[[dict, list, str], Awaitable[bytes]]):
        """
        Args:
            identity: () -> DiagIdentity | None (no account).
            stage_dir: where a boxed bundle waits for its upload.
            find_master: async (the master's friend row) -> [(ip, port)].
            collect: async (warrant, granted, node pubkey) -> the bundle's
                tar.gz, over this runtime's sources.
        """
        self.db_dsn = db_dsn
        self._identity = identity
        self.stage_dir = Path(stage_dir)
        self._find_master = find_master
        self._collect = collect
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="diag-db")
        self._conn = None                     # the pool thread's alone
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Made here, not at the start: what the chat client hands over before
        # the start (on its loop, the one the worker starts on) waits in it
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._master_addr: Optional[tuple] = None
        self._sent: list = []                 # monotonic times of the reports sent this hour
        self.running = False

    # ------------------------------------------------------------------
    # Lifecycle and the runtime's events — any thread
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """On the loop that runs the worker; unreported events are looked at
        once at the start, after what was handed over before it."""
        self._loop = asyncio.get_running_loop()
        self.running = True
        self._task = asyncio.create_task(self._run())
        self._put(("report",))

    async def stop(self) -> None:
        self.running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await asyncio.get_running_loop().run_in_executor(self._pool, self._close)
        self._pool.shutdown(wait=False)

    def warrant(self, warrant: dict, addr: tuple) -> None:
        """A warrant from the master's stream (the chat client's on_warrant)."""
        self._put(("warrant", warrant, addr))

    def master_reachable(self, addr: tuple) -> None:
        """The master's stream connected (the chat client's
        on_master_reachable): staged bundles and unshipped reports go."""
        self._master_addr = addr
        self._put(("resume", addr))

    def notes(self, channels: Iterable[str]) -> None:
        """What a LISTEN on this database delivered: sautium_diag (an event
        recorded) and sautium_diag_consent (the person decided)."""
        channels = set(channels)
        if "sautium_diag" in channels:
            self._put(("report",))
        if "sautium_diag_consent" in channels:
            self._put(("decided",))

    def listening(self) -> None:
        """The LISTEN (re)connected: a decision made while it was down woke
        nobody, and is looked for now."""
        self._put(("decided",))

    def _put(self, job: tuple) -> None:
        """From any thread once started; before the start only from the loop
        it will start on (the chat client's callbacks). A stopped worker
        takes nothing."""
        if self._loop is None:
            self._queue.put_nowait(job)
        elif self.running:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, job)
        else:
            logger.info("diag worker stopped: %s not taken", job[0])

    # ------------------------------------------------------------------
    # The database — one connection, on the pool's one thread
    # ------------------------------------------------------------------

    def _blocking(self, fn, *args):
        """fn(conn, *args) on the worker's connection. A connection the
        server dropped (a PostgreSQL restart: psycopg2 learns it only from
        the next query) is made again and the call made once more — every
        call here is safe to repeat (the state machine's ON CONFLICT and
        guarded UPDATEs, reads, upserts, NOTIFYs)."""
        for attempt in (1, 2):
            if self._conn is None or self._conn.closed:
                self._conn = psycopg2.connect(self.db_dsn)
                self._conn.autocommit = True
            try:
                return fn(self._conn, *args)
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                self._close()
                if attempt == 2:
                    raise
                logger.info("diag worker: its database connection was lost; made again")

    def _close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()

    async def _db(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self._pool, self._blocking, fn, *args)

    @staticmethod
    def _gate_row(conn) -> tuple:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT (SELECT value FROM user_settings WHERE key = 'support.diagnostics_enabled'),
                       (SELECT value FROM user_settings WHERE key = 'p2p.master_removed'),
                       (SELECT is_blocked FROM friends WHERE public_key_hex = %s)
            """, (master_node.MASTER_PUBKEY_HEX,))
            return cur.fetchone()

    @staticmethod
    def _master_row(conn) -> Optional[dict]:
        """The master's friend row by its current key, as find_friend_peers takes it."""
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, username, public_key_hex, invite_code, display_name,
                       is_blocked, source::text AS source
                  FROM friends WHERE public_key_hex = %s
            """, (master_node.MASTER_PUBKEY_HEX,))
            row = cur.fetchone()
            return dict(zip([d[0] for d in cur.description], row)) if row else None

    @staticmethod
    def _said(conn, key: str, channel: str = "sautium_identity") -> None:
        """A moment the Support row shows (support.last_*_at), and its wake."""
        with conn.cursor() as cur:
            cur.execute(diag_events._SETTINGS_UPSERT, (key, json.dumps(datetime.now(timezone.utc).isoformat())))
            cur.execute(f"NOTIFY {channel}")

    @staticmethod
    def _notify(conn, channel: str) -> None:
        with conn.cursor() as cur:
            cur.execute(f"NOTIFY {channel}")

    # ------------------------------------------------------------------
    # The gate and the queue's consumer
    # ------------------------------------------------------------------

    async def _allowed(self) -> Optional[DiagIdentity]:
        """The node's identity while it may speak to support, else None: the
        switch, the Support contact present and not blocked — and never when
        this node IS the master."""
        if not master_node.master_configured():
            return None
        ident = self._identity()
        if ident is None or ident.public_key_hex == master_node.MASTER_PUBKEY_HEX:
            return None
        enabled, removed, blocked = await self._db(self._gate_row)
        if enabled is False or removed or blocked is None or blocked:
            return None
        return ident

    async def _run(self) -> None:
        while self.running:
            job = await self._queue.get()
            try:
                ident = await self._allowed()
                if ident is None:
                    continue
                if job[0] == "warrant":
                    await self._handle_warrant(ident, job[1], job[2])
                elif job[0] == "resume":
                    await self._decided(ident)
                    await self._resume(ident, job[1])
                    await self._report(ident)
                elif job[0] == "report":
                    await self._report(ident)
                elif job[0] == "decided":
                    await self._decided(ident)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("diag worker: %s job failed: %s", job[0], e, exc_info=True)

    # ------------------------------------------------------------------
    # Warrants
    # ------------------------------------------------------------------

    def _stage(self, warrant_id: str) -> Path:
        self.stage_dir.mkdir(parents=True, exist_ok=True)
        return self.stage_dir / f"{warrant_id}.box"

    async def _handle_warrant(self, ident: DiagIdentity, warrant: dict, addr: tuple) -> None:
        """A verified warrant: answered at once when it names structural
        scopes only; one that names a content scope waits for the person's
        decision on the request sheet (diag_events.decide → _decided) — the
        notices tell the Web UI it is there."""
        ok, reason = diag_protocol.verify_warrant(warrant, ident.public_key_hex,
                                                  issuer=master_node.MASTER_PUBKEY_HEX)
        if not ok:
            logger.warning("Support warrant rejected: %s", reason)
            return
        short = warrant["id"][:8]
        state = await self._db(diag_events.receive_warrant, warrant)
        if state["uploaded_at"] or state["error"]:
            logger.info("Support warrant %s… already %s", short,
                        "fulfilled" if state["uploaded_at"] else "failed")
            return
        stage = self._stage(warrant["id"])
        if state["withdrawn"]:
            # The person decided, and the switch moved after: nothing of it
            # is gathered
            stage.unlink(missing_ok=True)
            await self._db(diag_events.mark_failed, warrant["id"], diag_events.WITHDRAWN)
            logger.info("Support warrant %s… %s", short, diag_events.WITHDRAWN)
            return
        if state["awaits"]:
            # A bundle an earlier release staged before asking is not the
            # person's to send: it goes, and the request waits like any other
            stage.unlink(missing_ok=True)
            if state["fresh"]:
                logger.info("Support warrant %s… waits for the owner's decision: scopes=%s",
                            short, ",".join(warrant["scopes"]))
                await self._db(self._notify, "sautium_notices")
            return
        if state["collected_at"] is None or not stage.exists():
            if not await self._collect_into(ident, warrant, state["granted"], stage):
                return
        await self._upload(ident, warrant["id"], stage, addr)

    async def _collect_into(self, ident: DiagIdentity, warrant: dict, granted: list,
                            stage: Path) -> bool:
        """Collect a warrant's bundle — its content scopes as far as the
        person granted them — box it to the master and stage it. False when
        the collection failed; its warrant is closed with the error."""
        short = warrant["id"][:8]
        logger.info("Support warrant %s… accepted: scopes=%s granted=%s", short,
                    ",".join(warrant["scopes"]), ",".join(granted) or "-")
        began = datetime.now(timezone.utc)
        try:
            tar = await self._collect(warrant, granted, ident.public_key_hex)
        except Exception as e:
            logger.error("Support bundle %s… collection failed: %s", short, e, exc_info=True)
            await self._db(diag_events.mark_failed, warrant["id"], f"collect: {e}")
            return False
        boxed = await asyncio.to_thread(ident.seal, tar)
        await asyncio.to_thread(stage.write_bytes, boxed)
        await self._db(diag_events.mark_collected, warrant["id"], len(boxed), began)
        return True

    async def _decided(self, ident: DiagIdentity) -> None:
        """The person decided on waiting requests (sautium_diag_consent): each
        is collected with what they ticked and sent at once when the master's
        stream holds an address, else staged for its next reconnect."""
        for warrant, granted in await self._db(diag_events.decided_uncollected):
            stage = self._stage(warrant["id"])
            if not await self._collect_into(ident, warrant, granted, stage):
                continue
            if self._master_addr is not None:
                await self._upload(ident, warrant["id"], stage, self._master_addr)

    async def _post(self, ident: DiagIdentity, addr: tuple, target: str, body: bytes,
                    total_s: int) -> tuple:
        """(status, text) of a POST to the master at `addr`, signed for it."""
        headers = peer_auth.sign_headers(ident.sign, ident.public_key_hex,
                                         master_node.MASTER_PUBKEY_HEX, "POST", target, body)
        headers["Content-Type"] = "application/octet-stream"
        return await self._send(addr, target, body, headers, total_s)

    @staticmethod
    async def _send(addr: tuple, target: str, body: bytes, headers: dict, total_s: int) -> tuple:
        """The POST itself, to the PINNED master: its TLS key is the master's
        or the connection fails."""
        import aiohttp
        from desktop.p2p.peer_auth import pinned_ssl_context
        master = master_node.MASTER_PUBKEY_HEX
        ip, port = addr
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=pinned_ssl_context(master)),
            timeout=aiohttp.ClientTimeout(total=total_s, connect=10),
        ) as session:
            async with session.post(f"https://{fmt_addr(ip, port)}{target}",
                                    data=body, headers=headers) as resp:
                return resp.status, await resp.text()

    async def _upload(self, ident: DiagIdentity, warrant_id: str, stage: Path, addr) -> bool:
        """POST the staged box to the master's peer surface. A refusal that
        cannot change (FINAL_REFUSALS) closes the warrant; a network failure
        leaves the stage for the next reconnect. A bundle the owner withdrew
        — diagnostics switched off since its collection began, whatever they
        say now — is deleted and its warrant closed, decided right before it
        would leave."""
        short = warrant_id[:8]
        if not await self._db(diag_events.may_upload, warrant_id):
            stage.unlink(missing_ok=True)
            await self._db(diag_events.mark_failed, warrant_id, diag_events.WITHDRAWN)
            logger.info("Support bundle %s… %s", short, diag_events.WITHDRAWN)
            return False
        body = await asyncio.to_thread(stage.read_bytes)
        try:
            status, text = await self._post(ident, addr, f"/api/diag/bundle?warrant={warrant_id}",
                                            body, 300)
        except Exception as e:
            logger.info("Support bundle %s… upload deferred: %s", short, e)
            return False
        if status == 200:
            await self._db(diag_events.mark_uploaded, warrant_id)
            stage.unlink(missing_ok=True)
            await self._db(self._said, "support.last_bundle_at")
            logger.info("Support bundle %s… delivered (%d bytes)", short, len(body))
            return True
        if status in FINAL_REFUSALS:
            await self._db(diag_events.mark_failed, warrant_id, f"HTTP {status}: {text[:200]}")
            stage.unlink(missing_ok=True)
            logger.warning("Support bundle %s… refused by the master: HTTP %s %s",
                           short, status, text[:200])
            return False
        logger.info("Support bundle %s… upload deferred: HTTP %s", short, status)
        return False

    async def _resume(self, ident: DiagIdentity, addr: tuple) -> None:
        for warrant_id in await self._db(diag_events.resumable_warrants):
            stage = self._stage(warrant_id)
            if not stage.exists():
                await self._db(diag_events.mark_failed, warrant_id, "staged bundle missing")
                continue
            await self._upload(ident, warrant_id, stage, addr)

    # ------------------------------------------------------------------
    # Event reports
    # ------------------------------------------------------------------

    async def _report(self, ident: DiagIdentity) -> None:
        """Ship unreported events to the master — content-free (encode_report
        strips the local-only fields), boxed, capped per hour. Whatever stays
        behind rides the next wake. The events are read after the master's
        address is found, just before they leave: the switch's rule
        (diag_events.SWITCH_ALLOWS) decides at that moment, not before a
        lookup that may take a DHT's timeout."""
        now = time.monotonic()
        self._sent = [t for t in self._sent if now - t < 3600]
        if len(self._sent) >= diag_protocol.REPORT_MAX_PER_HOUR:
            return
        if not await self._db(diag_events.pending_count):
            return
        addr = self._master_addr
        if addr is None:
            master = await self._db(self._master_row)
            peers = await self._find_master(master) if master else []
            if not peers:
                return
            addr = peers[0]
        events = await self._db(diag_events.drain_unreported)
        if not events:
            return
        body = ident.seal(diag_protocol.encode_report(events))
        try:
            status, _ = await self._post(ident, addr, "/api/diag/report", body, 30)
        except Exception as e:
            logger.debug("diag report deferred: %s", e)
            return
        self._sent.append(now)
        if status != 200:
            logger.info("diag report refused by the master: HTTP %s", status)
            return
        await self._db(diag_events.mark_reported, [e["id"] for e in events])
        await self._db(self._said, "support.last_report_at")
        logger.info("diag report delivered: %d event(s)", len(events))
        if len(events) >= diag_protocol.REPORT_MAX_EVENTS:
            self._put(("report",))     # more is waiting
