"""What the slice cycles (desktop/p2p/mb_slice_cycle.py, lb_slice_cycle.py) do
with a peer's word about this node and with a name a signed zero-match closed:

- a peer that refuses this node's signed slice request on the slice lanes
  (HTTP 403 with the evidence, desktop/p2p/slice_trust.refusal) is asked no
  more this run, the names stay pending, and what it said is recorded in the
  node's diagnostics as `mb_slice.refused` / `lb_slice.refused`, content-free;
- a name re-opened after its zero-match aged out is asked of another
  author's source first; the author whose zero-match closed it is asked last,
  and its zero-match is no answer before then from whichever source;
- one source whose answer raises is skipped for the rest of the run, and
  what it was asked goes to the next source — in both cycles.

A real HTTP server on loopback plays the refusing peer and the client is a
peer client, so the refusal takes the bounded path; no database: the cycles'
database steps are stand-ins."""

import asyncio
import http.server
import json
import threading
from types import SimpleNamespace

import pytest

from desktop.api_client import BackendAPIClient
from desktop.p2p import lb_slice_cycle, mb_slice_cycle, peer_auth, slice_trust
from desktop.p2p.sync_walk import DeadAddresses

REFUSAL = {"error": "slice_refused", "party": "relay", "kind": "P2", "entity": "subtree", "mbid": None,
           "name": "a name", "author": "ab" * 32, "dump_version": "20261101-001500",
           "seen": "2026-11-07T09:00:00+00:00"}
ARTIST = "0b0c25f4-f31c-46a5-a4fb-ccbf53d663bd"


class _Refusing(http.server.BaseHTTPRequestHandler):
    """A peer whose slice lanes refuse this node, with the evidence."""

    def _send(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._send(200, {"node_id": ""})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.asked.append(self.path)
        self._send(403, self.server.body)

    def log_message(self, *args):
        pass


@pytest.fixture
def refusing():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Refusing)
    srv.daemon_threads = True
    srv.asked = []
    srv.body = REFUSAL
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ident = peer_auth.PeerIdentity(pubkey="cd" * 32, sign=lambda m: b"\0" * 64, cert_bundle=lambda: None)
    yield srv, BackendAPIClient(f"http://127.0.0.1:{srv.server_port}", peer=ident)
    srv.shutdown()
    srv.server_close()


async def _no_connect(addr, expected_pubkey=None):
    return None


def _mb_cycle(sources, recorded, dropped, published):
    cycle = mb_slice_cycle.MbSliceCycle(
        "postgresql://unused", connect=_no_connect, dead=DeadAddresses(), after_import=lambda: None,
        diag_record=lambda kind, detail: recorded.append((kind, detail)))
    cycle._sources = SimpleNamespace(drop=dropped.append)

    async def find_sources():
        return sources

    async def publish(**state):
        published.append(state)

    cycle.find_sources, cycle._publish = find_sources, publish
    return cycle


@pytest.fixture
def mb_steps(monkeypatch):
    """The MB cycle's database steps: no dump here, MetaBrainz's listing
    unread, two owned names pending, `reopened` the names a zero-match
    closed and the author that signed it."""
    reopened: dict = {}
    monkeypatch.setattr(mb_slice_cycle, "local_dump_available", lambda dsn: None)
    monkeypatch.setattr(mb_slice_cycle, "published_newest", lambda dsn, family: None)
    monkeypatch.setattr(mb_slice_cycle, "pending_names", lambda dsn, n: [("Owned One", 0), ("Gone Quiet", 1)])
    monkeypatch.setattr(mb_slice_cycle, "reopened_authors",
                        lambda dsn, names: {n: a for n, a in reopened.items() if n in names})
    return reopened


def test_a_refusal_with_evidence_is_recorded_and_its_source_asked_no_more(refusing, mb_steps):
    srv, api = refusing
    recorded, dropped, published = [], [], []
    cycle = _mb_cycle([(api, "ef" * 32)], recorded, dropped, published)
    asyncio.run(cycle.run())
    assert srv.asked == ["/api/mb/slice"]                       # one request, no second batch
    # Content-free for support: the kind and the key's prefix, never the name or the MBID.
    assert recorded == [("mb_slice.refused",
                         slice_trust.refusal_report({k: v for k, v in REFUSAL.items() if k != "error"}))]
    assert dropped == ["ef" * 32]
    assert (published[-1]["unserved"], published[-1]["reason"]) == (2, "error")


def test_an_lb_refusal_with_evidence_is_recorded_and_its_source_asked_no_more(refusing, monkeypatch):
    srv, api = refusing
    recorded, dropped, published = [], [], []
    monkeypatch.setattr(lb_slice_cycle, "local_dump_available", lambda dsn: None)
    monkeypatch.setattr(lb_slice_cycle, "published_newest", lambda dsn, family: None)
    monkeypatch.setattr(lb_slice_cycle, "pending_slice_mbids", lambda dsn, n, newest: [(ARTIST, None)])
    cycle = lb_slice_cycle.LbSliceCycle(
        "postgresql://unused", connect=_no_connect, dead=DeadAddresses(),
        diag_record=lambda kind, detail: recorded.append((kind, detail)))
    cycle._sources = SimpleNamespace(drop=dropped.append)

    async def find_sources(published_version):
        return [(api, "ef" * 32, None)], None

    async def publish(**state):
        published.append(state)

    cycle.find_sources, cycle._publish = find_sources, publish
    asyncio.run(cycle.run())
    assert srv.asked == ["/api/lb/slice"]
    assert recorded == [("lb_slice.refused",
                         slice_trust.refusal_report({k: v for k, v in REFUSAL.items() if k != "error"}))]
    assert dropped == ["ef" * 32]
    assert (published[-1]["unserved"], published[-1]["reason"]) == (1, "error")


MALFORMED = {"error": "slice_refused", "party": "relay", "kind": "P2", "entity": "subtree",
             "author": 1, "seen": "<script>"}


def test_a_refusal_that_is_no_refusal_records_nothing_and_drops_its_source(refusing, mb_steps):
    """Outside text never reaches the node's diagnostics or the support
    desk, and a body that would raise ends no run."""
    srv, api = refusing
    srv.body = MALFORMED
    recorded, dropped, published = [], [], []
    cycle = _mb_cycle([(api, "ef" * 32)], recorded, dropped, published)
    asyncio.run(cycle.run())
    assert recorded == [] and dropped == ["ef" * 32]
    assert (published[-1]["unserved"], published[-1]["reason"]) == (2, "error")


def test_an_lb_refusal_that_is_no_refusal_records_nothing_and_drops_its_source(refusing, monkeypatch):
    srv, api = refusing
    srv.body = MALFORMED
    recorded, dropped, published = [], [], []
    monkeypatch.setattr(lb_slice_cycle, "local_dump_available", lambda dsn: None)
    monkeypatch.setattr(lb_slice_cycle, "published_newest", lambda dsn, family: None)
    monkeypatch.setattr(lb_slice_cycle, "pending_slice_mbids", lambda dsn, n, newest: [(ARTIST, None)])
    cycle = lb_slice_cycle.LbSliceCycle(
        "postgresql://unused", connect=_no_connect, dead=DeadAddresses(),
        diag_record=lambda kind, detail: recorded.append((kind, detail)))
    cycle._sources = SimpleNamespace(drop=dropped.append)

    async def find_sources(published_version):
        return [(api, "ef" * 32, None)], None

    async def publish(**state):
        published.append(state)

    cycle.find_sources, cycle._publish = find_sources, publish
    asyncio.run(cycle.run())
    assert recorded == [] and dropped == ["ef" * 32]


def test_a_refusal_is_read_field_by_field():
    assert slice_trust.refusal_of(REFUSAL)["author"] == "ab" * 32
    assert slice_trust.refusal_of(dict(REFUSAL, kind=None, entity=None, seen=None, author="")) is not None
    for field, value in (("party", "judge"), ("kind", "S3"), ("author", "AB" * 32), ("mbid", "x"),
                         ("name", "a\x00name"), ("dump_version", "soon"), ("seen", "yesterday")):
        assert slice_trust.refusal_of(dict(REFUSAL, **{field: value})) is None, field


class _Client:
    """MBSliceClient's stand-in: what each source was asked and the zero-matches
    it was told to take as no answer (`avoided`), what it lacks (`missing`),
    and the sources whose answer raises (`broken`)."""
    asked: list = []
    avoided: list = []
    missing: dict = {}
    broken: set = set()

    def __init__(self, api, db_dsn, source_node):
        self.node = source_node

    def run(self, batch, avoid=None):
        _Client.asked.append((self.node, list(batch)))
        _Client.avoided.append((self.node, dict(avoid or {})))
        if self.node in _Client.broken:
            raise ValueError("a malformed answer")
        gone = [n for n in batch if n in _Client.missing.get(self.node, ())]
        return {"names": len(batch), "matched": len(batch) - len(gone), "rows_inserted": 1, "missing": gone}

    def close(self):
        pass

    def finalize(self):
        pass


@pytest.fixture
def asked(monkeypatch):
    monkeypatch.setattr(mb_slice_cycle, "MBSliceClient", _Client)
    monkeypatch.setattr(_Client, "asked", [])
    monkeypatch.setattr(_Client, "avoided", [])
    monkeypatch.setattr(_Client, "missing", {})
    monkeypatch.setattr(_Client, "broken", set())
    return _Client.asked


CLOSER, OTHER = "a1" * 32, "b2" * 32


def _source(node):
    return SimpleNamespace(base_url="https://198.51.100.30:21000", session_deadline=None), node


def test_a_reopened_zero_match_is_asked_of_another_authors_source_first(mb_steps, asked):
    mb_steps["Gone Quiet"] = CLOSER
    asyncio.run(_mb_cycle([_source(CLOSER), _source(OTHER)], [], [], []).run())
    assert asked == [(CLOSER, ["Owned One"]), (OTHER, ["Gone Quiet"])]


def test_the_author_that_closed_a_name_is_asked_last(mb_steps, asked):
    mb_steps["Gone Quiet"] = CLOSER
    _Client.missing[OTHER] = ["Gone Quiet"]
    asyncio.run(_mb_cycle([_source(OTHER), _source(CLOSER)], [], [], []).run())
    assert asked == [(OTHER, ["Owned One", "Gone Quiet"]), (CLOSER, ["Gone Quiet"])]
    asked.clear()
    asyncio.run(_mb_cycle([_source(CLOSER)], [], [], []).run())      # the only source left
    assert asked == [(CLOSER, ["Owned One", "Gone Quiet"])]


def test_the_closing_authors_zero_match_is_no_answer_from_any_source_but_the_last(mb_steps, asked):
    """A replica re-serving the zero-match the closing author signed would
    otherwise close the name again under the same author."""
    mb_steps["Gone Quiet"] = CLOSER
    relay = "c3" * 32
    asyncio.run(_mb_cycle([_source(relay), _source(OTHER)], [], [], []).run())
    assert _Client.avoided[0] == (relay, {"Gone Quiet": CLOSER})
    assert all(avoid == {} for node, avoid in _Client.avoided if node == OTHER)


def test_a_source_whose_answer_raises_is_skipped_and_its_names_asked_of_the_next(mb_steps, asked):
    broken = "d4" * 32
    _Client.broken.add(broken)
    dropped, published = [], []
    asyncio.run(_mb_cycle([_source(broken), _source(OTHER)], [], dropped, published).run())
    assert asked == [(broken, ["Owned One", "Gone Quiet"]), (OTHER, ["Owned One", "Gone Quiet"])]
    assert dropped == [broken]
    assert (published[-1]["unserved"], published[-1]["reason"]) == (0, "error")


def test_an_lb_source_whose_answer_raises_is_skipped_and_its_artists_asked_of_the_next(monkeypatch):
    dropped, published, asked_lb = [], [], []
    monkeypatch.setattr(lb_slice_cycle, "local_dump_available", lambda dsn: None)
    monkeypatch.setattr(lb_slice_cycle, "published_newest", lambda dsn, family: None)
    monkeypatch.setattr(lb_slice_cycle, "pending_slice_mbids", lambda dsn, n, newest: [(ARTIST, None)])
    monkeypatch.setattr(lb_slice_cycle, "analyze_tables", lambda dsn: None)
    cycle = lb_slice_cycle.LbSliceCycle("postgresql://unused", connect=_no_connect, dead=DeadAddresses())
    cycle._sources = SimpleNamespace(drop=dropped.append)
    broken, good = "d4" * 32, "e5" * 32

    async def find_sources(published_version):
        return [(SimpleNamespace(base_url="https://198.51.100.31:21000", session_deadline=None), broken, None),
                (SimpleNamespace(base_url="https://198.51.100.32:21000", session_deadline=None), good, None)], None

    async def exchange(api, node, source_addr, batch, min_version):
        asked_lb.append((node, list(batch)))
        if node == broken:
            raise ValueError("a malformed answer")
        return {"imported": len(batch), "recordings": 1, "missing": [], "dropped": 0}

    async def publish(**state):
        published.append(state)

    cycle.find_sources, cycle._exchange, cycle._publish = find_sources, exchange, publish
    asyncio.run(cycle.run())
    assert asked_lb == [(broken, [ARTIST]), (good, [ARTIST])]
    assert dropped == [broken]
    assert published[-1]["unserved"] == 0
