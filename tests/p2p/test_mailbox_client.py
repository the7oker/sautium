"""Master mailbox client against an in-process stand-in for the Worker: the
deposit signature contract, page/ack drain semantics, and the wake socket
(drain on connect, drain on "mail")."""

import asyncio
import hashlib

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from desktop.p2p import mailbox_client as mc

MASTER = Ed25519PrivateKey.generate()
MASTER_PUB = MASTER.public_key().public_bytes_raw().hex()
SENDER = Ed25519PrivateKey.generate()
SENDER_PUB = SENDER.public_key().public_bytes_raw().hex()


class FakeWorker:
    """Just enough of worker/verify.js: signature checks, id-ordered store,
    paged drain (page size 2 here), ack, wake sockets."""

    def __init__(self):
        self.rows = []
        self.next_id = 1
        self.sockets = []
        self.acks = []
        self.acked = asyncio.Event()
        self.app = web.Application()
        self.app.router.add_post("/mailbox", self.put)
        self.app.router.add_get("/mailbox", self.drain)
        self.app.router.add_delete("/mailbox", self.ack)
        self.app.router.add_get("/mailbox/wake", self.wake)

    @staticmethod
    def _verify(message: str, sig_hex: str, pub_hex: str) -> bool:
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex)).verify(bytes.fromhex(sig_hex), message.encode())
            return True
        except Exception:
            return False

    async def put(self, request):
        b = await request.json()
        digest = hashlib.sha256(b["encrypted"].encode()).hexdigest()
        msg = f"mailbox:v1:{MASTER_PUB}:{b['message_uuid']}:{b['timestamp']}:{digest}"
        if b["to"] != MASTER_PUB:
            return web.json_response({"error": "no mailbox for this recipient"}, status=404)
        if not self._verify(msg, b["signature"], b["from_public_key"]):
            return web.json_response({"error": "invalid signature"}, status=403)
        if any(r["message_uuid"] == b["message_uuid"] for r in self.rows):
            return web.json_response({"stored": False, "duplicate": True})
        row = {"id": self.next_id, "message_uuid": b["message_uuid"], "from_public_key": b["from_public_key"],
               "encrypted": b["encrypted"], "timestamp": b["timestamp"], "received_at": 0}
        self.next_id += 1
        self.rows.append(row)
        for ws in list(self.sockets):
            await ws.send_str("mail")
        return web.json_response({"stored": True, "id": row["id"]})

    def _master(self, request, label, extra=None):
        q = request.query
        message = f"{label}:v1:{q['ts']}" if extra is None else f"{label}:v1:{q['ts']}:{extra}"
        return q["public_key_hex"] == MASTER_PUB and self._verify(message, q["signature"], MASTER_PUB)

    async def drain(self, request):
        after = int(request.query.get("after", "0"))
        if not self._master(request, "mailbox-drain", str(after)):
            return web.json_response({"error": "not the master"}, status=403)
        rows = [r for r in self.rows if r["id"] > after]
        return web.json_response({"messages": rows[:2], "more": len(rows) > 2})

    async def ack(self, request):
        upto = int(request.query.get("upto", "0"))
        if not self._master(request, "mailbox-ack", str(upto)):
            return web.json_response({"error": "not the master"}, status=403)
        before = len(self.rows)
        self.rows = [r for r in self.rows if r["id"] > upto]
        self.acks.append(upto)
        self.acked.set()
        return web.json_response({"deleted": before - len(self.rows)})

    async def wake(self, request):
        if not self._master(request, "mailbox-wake"):
            return web.json_response({"error": "not the master"}, status=403)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        try:
            async for _ in ws:
                pass
        finally:
            self.sockets.remove(ws)
        return ws


async def _deposit(session, url, uuid_, text, sign=SENDER.sign, sender=SENDER_PUB):
    return await mc.deposit(session, master_pubkey=MASTER_PUB, sender_pubkey=sender, sign=sign,
                            encrypted=text, timestamp_iso="2026-08-18T14:00:00Z", message_uuid=uuid_,
                            worker_url=url)


def test_deposit_signature_is_the_worker_contract():
    sig = mc.deposit_signature(SENDER.sign, MASTER_PUB.upper(), "u-1", "2026-08-18T14:00:00Z", "cipher")
    digest = hashlib.sha256(b"cipher").hexdigest()
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(SENDER_PUB)).verify(
        bytes.fromhex(sig), f"mailbox:v1:{MASTER_PUB}:u-1:2026-08-18T14:00:00Z:{digest}".encode())


def test_deposit_and_drain_round_trip():
    async def run():
        fake = FakeWorker()
        async with TestServer(fake.app) as server:
            url = str(server.make_url("")).rstrip("/")
            async with aiohttp.ClientSession() as s:
                assert await _deposit(s, url, "u-1", "c1") == {"stored": True, "id": 1}
                assert await _deposit(s, url, "u-1", "c1") == {"stored": False, "duplicate": True}
                for i in (2, 3, 4, 5):
                    assert (await _deposit(s, url, f"u-{i}", f"c{i}"))["stored"]
                other = Ed25519PrivateKey.generate()
                assert await _deposit(s, url, "u-9", "x", sign=other.sign) is None          # signature ≠ sender key
                assert await mc.deposit(s, master_pubkey="ab" * 32, sender_pubkey=SENDER_PUB, sign=SENDER.sign,
                                        encrypted="x", timestamp_iso="t", message_uuid="u-8", worker_url=url) is None
                seen = []
                box = mc.MasterMailbox(MASTER_PUB, MASTER.sign, lambda m: seen.append(m["message_uuid"]), worker_url=url)
                assert await box.drain(s) == 5                                              # 3 pages of 2
                assert seen == ["u-1", "u-2", "u-3", "u-4", "u-5"] and fake.rows == []
                assert fake.acks == [2, 4, 5] and box.drained_total == 5
                # a foreign key cannot drain
                thief = mc.MasterMailbox(SENDER_PUB, SENDER.sign, lambda m: None, worker_url=url)
                assert (await _deposit(s, url, "u-6", "c6"))["stored"]
                assert await thief.drain(s) == 0 and len(fake.rows) == 1
                # a handler that raises leaves the batch unacked
                bad = mc.MasterMailbox(MASTER_PUB, MASTER.sign, lambda m: (_ for _ in ()).throw(RuntimeError("db down")),
                                       worker_url=url)
                with pytest.raises(RuntimeError):
                    await bad.drain(s)
                assert len(fake.rows) == 1
    asyncio.run(run())


def test_wake_socket_drains_on_connect_and_on_mail():
    async def run():
        fake = FakeWorker()
        async with TestServer(fake.app) as server:
            url = str(server.make_url("")).rstrip("/")
            async with aiohttp.ClientSession() as s:
                assert (await _deposit(s, url, "u-1", "parked while offline"))["stored"]
                seen = []
                got = asyncio.Event()

                def on_message(m):
                    seen.append(m["message_uuid"])
                    got.set()

                box = mc.MasterMailbox(MASTER_PUB, MASTER.sign, on_message, worker_url=url)
                running = True
                task = asyncio.create_task(box.run(lambda: running))
                await asyncio.wait_for(fake.acked.wait(), 5)                # drained + acked on connect
                assert seen == ["u-1"] and box.connected and fake.rows == []
                got.clear(); fake.acked.clear()
                assert (await _deposit(s, url, "u-2", "live"))["stored"]
                await asyncio.wait_for(fake.acked.wait(), 5)                # "mail" → drain → ack
                assert seen == ["u-1", "u-2"] and fake.rows == []
                running = False
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                for ws in list(fake.sockets):
                    await ws.close()
    asyncio.run(run())
