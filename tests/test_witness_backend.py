"""The backend's half of the witness record, against a real PostgreSQL built
from the migrations and the REAL Worker code (tests/p2p/worker_harness.mjs in
serve mode, a throwaway authority):

- the Docker peer surface records `introduced` after the gate's verdict and
  `handshake` after an accepted token, serves its certificate on /health;
- sign_audio signs witness rows on their own, stamps them with an analysis
  batch or alone when allowed, copies the stamp into the row, refuses a stamp
  under an untrusted authority, and verify_all checks them;
- the notary signs on the listener's wake without a catalogue scan, and its
  backstop is a permission: armed from the oldest waiting row, never ahead of
  the pace, kept through failures, one Worker call per retry rung;
- an identity-rule migration (_shed_all_seals) leaves witness rows whole;
- a rotation signs every waiting row with the old key first;
- a backup round trip keeps the rows and the guard.
Skipped without a cluster; the Worker cases also without node."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.errors  # noqa: E402
import psycopg2.extras  # noqa: E402
pytest.importorskip("fastapi")

import birth_authority  # noqa: E402
import db_pool  # noqa: E402
import notary  # noqa: E402
import p2p_identity  # noqa: E402
import sign_audio  # noqa: E402
from config import settings  # noqa: E402
from desktop import node_backup as nb  # noqa: E402
from desktop import node_identity  # noqa: E402
from desktop.p2p import birth_cert, peer_auth, record_sig, witness  # noqa: E402
from tests.p2p.test_witness import ISSUER, certificate  # noqa: E402

HARNESS = Path(__file__).resolve().parent / "p2p" / "worker_harness.mjs"
NODE = Ed25519PrivateKey.generate()
NODE_PUB = NODE.public_key().public_bytes_raw().hex()
IDENTITY = {"node_id": NODE_PUB, "public_key_hex": NODE_PUB, "username": "node",
            "invite_code": node_identity.make_invite_code("node", bytes.fromhex(NODE_PUB)), "email": ""}


def _key_hex() -> str:
    return Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()


@pytest.fixture(autouse=True)
def node(scratch_dsn, monkeypatch):
    """config.settings, db_pool and the identity caches on this module's
    database and key; the test authority trusted by every mirror."""
    url = urlparse(scratch_dsn)
    for key, value in dict(postgres_host=url.hostname, postgres_port=url.port, postgres_user=url.username,
                           postgres_password=url.password, postgres_db=url.path.lstrip("/")).items():
        monkeypatch.setattr(settings, key, value)
    assert settings.database_url == scratch_dsn
    db_pool.close_pool()
    monkeypatch.setattr(p2p_identity, "_signing_key_cache", NODE)
    monkeypatch.setattr(p2p_identity, "_witness", None)
    monkeypatch.setattr(p2p_identity, "resolve_identity", lambda s: IDENTITY)
    for mirror in (birth_cert, witness, birth_authority):
        monkeypatch.setattr(mirror, "TRUSTED_AUTHORITIES", [ISSUER])
    yield
    db_pool.close_pool()


@pytest.fixture
def conn(scratch_dsn):
    c = psycopg2.connect(scratch_dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    c.autocommit = True
    yield c
    c.close()


@pytest.fixture
def stage(scratch_dsn):
    """The notary's kind of connection (sign_audio.connect: not autocommit)."""
    c = sign_audio.connect()
    yield c
    c.close()


def _sighting(channel="probe") -> str:
    subject = _key_hex()
    assert p2p_identity.witness_recorder(settings).record(certificate(subject), channel, subject)
    return subject


def _row(conn, subject, channel="probe") -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM p2p_witness WHERE subject = %s AND channel = %s", (subject, channel))
        return cur.fetchone()


def _analysis_row(conn) -> str:
    """A track_mbids row signed by this node, waiting for its stamp."""
    tid, mbid = str(uuid.uuid4()), str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO tracks (id, title) VALUES (%s, 'x')", (tid,))
        cur.execute("INSERT INTO track_mbids (recording_mbid, track_id, author_pubkey, signature) "
                    "VALUES (%s, %s, %s, %s)", (mbid, tid, NODE_PUB, os.urandom(64).hex()))
    return mbid


# ---------------------------------------------------------------------------
# The Worker: the real verify.js, served
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def harness():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    proc = subprocess.Popen(["node", str(HARNESS), os.urandom(32).hex(), "test-email-pepper", "--serve"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    info = json.loads(proc.stdout.readline())
    # The node's certificate, asked for as a node does on its first start:
    # the Worker stamps only for a key it issued a certificate to.
    urllib.request.urlopen(urllib.request.Request(
        info["url"] + "/birth-certificate", method="POST",
        data=json.dumps({"pubkey_hex": NODE_PUB,
                         "signature": NODE.sign(f"birth:{NODE_PUB}".encode()).hex()}).encode(),
        headers={"Content-Type": "application/json"}), timeout=10).read()
    yield info
    proc.terminate()
    proc.wait(10)


@pytest.fixture
def worker(harness, monkeypatch, conn, stage):
    """The Worker trusted and in use; no analysis or witness row waiting for a
    stamp when the test begins (the module's database is shared)."""
    monkeypatch.setattr(sign_audio, "WORKER_URL", harness["url"])
    monkeypatch.setattr(birth_authority, "TRUSTED_AUTHORITIES", [harness["authority_pub"], ISSUER])
    with conn.cursor() as cur:
        cur.execute("DELETE FROM track_mbids WHERE batch_root IS NULL")
    sign_audio.sign_witness(stage)
    sign_audio.stamp(stage, witness_only=True)
    return harness


def _calls(info) -> int:
    return json.loads(urllib.request.urlopen(info["url"] + "/__harness/calls").read())["timestamp"]


def _stamped(row) -> bool:
    return row["batch_root"] is not None


# ---------------------------------------------------------------------------
# Signing and stamping
# ---------------------------------------------------------------------------

def test_sign_witness_signs_witness_rows_and_nothing_else(conn, stage, monkeypatch):
    def scan(*_a, **_k):
        raise AssertionError("a witness pass ran a catalogue scan")
    for name in ("_materialize_owned_tracklists", "_pending_tracks", "_collect_enrichment"):
        monkeypatch.setattr(sign_audio, name, scan)
    subject = _sighting()
    tid, mbid = str(uuid.uuid4()), str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO tracks (id, title) VALUES (%s, 'y')", (tid,))
        cur.execute("INSERT INTO track_mbids (recording_mbid, track_id) VALUES (%s, %s)", (mbid, tid))
    assert sign_audio.sign_witness(stage) >= 1
    row = _row(conn, subject)
    assert row["author_pubkey"] == NODE_PUB
    assert record_sig.verify(witness.statement(row, NODE_PUB), row["signature"], NODE_PUB)
    with conn.cursor() as cur:
        cur.execute("SELECT signature FROM track_mbids WHERE recording_mbid = %s", (mbid,))
        assert cur.fetchone()["signature"] is None
    assert sign_audio.sign_witness(stage) == 0


def test_witness_rows_ride_an_analysis_stamp_and_keep_its_copy(worker, conn, stage):
    subject = _sighting()
    sign_audio.sign_witness(stage)
    mbid = _analysis_row(conn)
    before = _calls(worker)
    result = sign_audio.stamp(stage)
    assert _calls(worker) == before + 1                  # one root for both
    assert result.stamped >= 2 and result.witness_waiting is None
    row = _row(conn, subject)
    with conn.cursor() as cur:
        cur.execute("SELECT batch_root FROM track_mbids WHERE recording_mbid = %s", (mbid,))
        assert cur.fetchone()["batch_root"] == row["batch_root"]
        cur.execute("SELECT * FROM signing_batches WHERE batch_root = %s", (row["batch_root"],))
        batch = cur.fetchone()
    assert (row["worker_date"], str(row["worker_ip_hash"]), row["worker_sig"], row["worker_authority"],
            row["timestamp_version"]) == (batch["worker_date"], str(batch["ip_hash"]), batch["worker_sig"],
                                          batch["authority"], batch["timestamp_version"])
    assert row["worker_authority"] == worker["authority_pub"]
    assert sign_audio.verify_all()


def test_witness_rows_alone_wait_for_the_permission(worker, conn, stage):
    subject = _sighting()
    sign_audio.sign_witness(stage)
    before = _calls(worker)
    result = sign_audio.stamp(stage)
    assert (result.stamped, _calls(worker)) == (0, before)
    assert result.witness_waiting == _row(conn, subject)["seen_at"]
    assert sign_audio.verify_all()                       # signed, awaiting its stamp: not invalid
    result = sign_audio.stamp(stage, witness_only=True)
    assert (result.stamped, result.witness_waiting, _calls(worker)) == (1, None, before + 1)
    assert _stamped(_row(conn, subject))
    assert sign_audio.verify_all()


def test_a_retired_keys_rows_wait_for_the_backstop_and_stamp_under_their_own_key(worker, conn, stage,
                                                                                monkeypatch):
    retired = Ed25519PrivateKey.generate()
    retired_pub = retired.public_key().public_bytes_raw().hex()
    monkeypatch.setattr(p2p_identity, "_signing_key_cache", retired)
    subject = _sighting()
    sign_audio.sign_witness(stage)                       # signed by the key in force then
    monkeypatch.setattr(p2p_identity, "_signing_key_cache", NODE)
    _analysis_row(conn)                                  # analysis by the key in force now
    result = sign_audio.stamp(stage)
    assert result.stamped == 1                           # the analysis alone: another author's batch
    assert not _stamped(_row(conn, subject))
    assert result.witness_waiting == _row(conn, subject)["seen_at"]   # it arms the backstop
    assert sign_audio.stamp(stage, witness_only=True).stamped == 1
    row = _row(conn, subject)
    with conn.cursor() as cur:
        cur.execute("SELECT author_pubkey FROM signing_batches WHERE batch_root = %s", (row["batch_root"],))
        assert cur.fetchone()["author_pubkey"] == retired_pub == row["author_pubkey"]
    assert sign_audio.verify_all()


def test_a_stamp_under_an_untrusted_authority_is_refused(worker, conn, stage, monkeypatch):
    subject = _sighting()
    sign_audio.sign_witness(stage)
    monkeypatch.setattr(birth_authority, "TRUSTED_AUTHORITIES", [ISSUER])
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM signing_batches")
        batches = cur.fetchone()["n"]
    with pytest.raises(RuntimeError, match="trusted authorities"):
        sign_audio.stamp(stage, witness_only=True)
    assert not _stamped(_row(conn, subject))
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM signing_batches")
        assert cur.fetchone()["n"] == batches


def test_verify_all_finds_a_witness_row_whose_evidence_does_not_hold(scratch_dsn, monkeypatch):
    # A database of its own: the row this test writes can never be deleted.
    with _fresh_database(scratch_dsn, "sautium_witness_forged") as (name, c):
        monkeypatch.setattr(settings, "postgres_db", name)
        forged = _key_hex()
        cert = certificate(forged)
        with c.cursor() as cur:
            # A valid shape the database lets in, a statement another key
            # signed: only the check can tell.
            cur.execute("""INSERT INTO p2p_witness (subject, cert_payload, cert_sig, issuer, cert_v, issued_at,
                                                    method, difficulty, params_version, channel,
                                                    author_pubkey, signature)
                           VALUES (%s, %s, %s, %s, 4, %s, 'pow', 32, 1, 'probe', %s, %s)""",
                        (forged, birth_cert.canonical_payload(cert).decode(), cert["sig"], ISSUER,
                         cert["issued_at"], NODE_PUB, "ab" * 64))
        c.commit()
        assert not sign_audio.verify_all()


# ---------------------------------------------------------------------------
# The notary
# ---------------------------------------------------------------------------

def _waiting(stage) -> None:
    """One signed witness row waiting for its stamp."""
    _sighting()
    sign_audio.sign_witness(stage)


def test_the_backstop_is_armed_from_the_oldest_waiting_row(worker, conn, stage):
    n = notary._Notary()
    _waiting(stage)
    before = _calls(worker)
    n._stamp_if_due()
    assert _calls(worker) == before
    with conn.cursor() as cur:
        cur.execute("SELECT min(seen_at) AS at FROM p2p_witness WHERE signature IS NOT NULL AND batch_root IS NULL")
        oldest = cur.fetchone()["at"]
    assert n._backstop == oldest.timestamp() + notary.WITNESS_BACKSTOP_S + n._offset
    assert n._next_attempt() == n._backstop
    n._drop_conn()


def test_a_witness_backlog_stamps_at_the_backstop_not_before(worker, conn, stage):
    n = notary._Notary()
    _waiting(stage)
    before = _calls(worker)
    n._backstop = time.time() + 600
    n._stamp_if_due()
    assert _calls(worker) == before and n._backstop is not None
    n._backstop = time.time() - 1
    n._stamp_if_due()
    assert _calls(worker) == before + 1
    assert n._backstop is None and n._stamp_at is None
    assert n._earliest >= time.time() + notary.STAMP_MIN_SPACING_S - 5     # the pace after a stamp
    n._drop_conn()


def test_an_analysis_wake_stamps_at_the_pace_and_a_deferral_keeps_the_backstop(worker, conn, stage):
    n = notary._Notary()
    _waiting(stage)
    _analysis_row(conn)
    n._backstop = armed = time.time() + 10_000
    n._earliest = time.time() + 100                      # a stamp was made a moment ago
    n._stamp_if_due()                                    # the analysis wake: deferred to the pace
    assert n._stamp_at == pytest.approx(n._earliest, abs=1)
    assert n._backstop == armed and n._next_attempt() == n._stamp_at
    before = _calls(worker)
    n._stamp_at = n._earliest = time.time() - 1          # the pace reached
    n._stamp_if_due()
    assert _calls(worker) == before + 1                  # at the pace, not at the backstop
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM p2p_witness WHERE signature IS NOT NULL AND batch_root IS NULL")
        assert cur.fetchone()["n"] == 0                  # the witness row rode along
    n._drop_conn()


def test_a_backstop_inside_the_workers_not_before_waits_for_it(worker, conn, stage):
    n = notary._Notary()
    _waiting(stage)
    n._backstop = time.time() - 10
    n._earliest = time.time() + 100
    assert n._next_attempt() == n._earliest
    before = _calls(worker)
    n._stamp_if_due()
    assert _calls(worker) == before and n._stamp_at == pytest.approx(n._earliest, abs=1)
    n._drop_conn()


class _Refusing(BaseHTTPRequestHandler):
    """A Worker that fails every stamp: `status` 503 (down) or 429 (throttling)."""
    status, retry_after, calls = 503, None, None

    def do_POST(self):
        self.calls.append(time.time())                  # the notary schedules by this clock
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(self.status)
        if self.retry_after is not None:
            self.send_header("Retry-After", str(self.retry_after))
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *_a):
        pass


@contextmanager
def _refusing_worker(status, retry_after=None):
    handler = type("Handler", (_Refusing,), {"status": status, "retry_after": retry_after, "calls": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield handler, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def _running(n):
    assert n.start()
    try:
        yield n
    finally:
        n.stop()


def _calls_reach(handler, n: int, within: float = 15.0) -> None:
    deadline = time.time() + within
    while len(handler.calls) < n and time.time() < deadline:
        time.sleep(0.02)
    assert len(handler.calls) >= n


def test_a_due_backstop_meets_a_dead_worker_once_per_retry_rung(worker, stage, monkeypatch):
    _waiting(stage)
    monkeypatch.setattr(notary, "_RETRY_S", (0.5, 1.0, 2.0, 4.0))
    with _refusing_worker(503) as (handler, url):
        monkeypatch.setattr(sign_audio, "WORKER_URL", url)
        n = notary._Notary()
        n._backstop = time.time() - 1
        with _running(n):
            _calls_reach(handler, 3)
            first, second, third = handler.calls[:3]
            assert second - first >= 0.45 and third - second >= 0.95      # a rung each, no busy loop
            time.sleep(1.0)                                               # the next rung is 2 s away
            assert len(handler.calls) == 3
            assert n._backstop is not None and n._failures == 3           # the permission is kept


def test_a_throttling_worker_is_asked_again_when_it_says(worker, stage, monkeypatch):
    _waiting(stage)
    with _refusing_worker(429, retry_after=60) as (handler, url):
        monkeypatch.setattr(sign_audio, "WORKER_URL", url)
        n = notary._Notary()
        n._backstop = time.time() - 1
        with _running(n):
            _calls_reach(handler, 1)
            time.sleep(1.0)
            assert len(handler.calls) == 1
            assert n._stamp_at == pytest.approx(time.time() + 60, abs=3)
            assert n._backstop is not None


def test_the_listener_wakes_the_notary_to_sign_sightings_alone(worker, conn, monkeypatch):
    def scan(*_a, **_k):
        raise AssertionError("a witness wake ran a catalogue scan")
    monkeypatch.setattr(sign_audio, "_materialize_owned_tracklists", scan)
    n = notary._Notary()
    monkeypatch.setattr(notary, "_notary", n)
    with _running(n):
        notary.start_witness_listener()
        try:
            deadline = time.monotonic() + 10                 # the connect sweep's pass
            while n.status()["last_sign_at"] is None and time.monotonic() < deadline:
                time.sleep(0.05)
            before = _calls(worker)
            subject = _sighting("introduced")
            deadline = time.monotonic() + 10
            while _row(conn, subject, "introduced")["signature"] is None and time.monotonic() < deadline:
                time.sleep(0.05)
            row = _row(conn, subject, "introduced")
            assert row["author_pubkey"] == NODE_PUB
            deadline = time.monotonic() + 10                 # the same pass's stamp attempt
            while n.status()["witness_backstop_at"] is None and time.monotonic() < deadline:
                time.sleep(0.05)
            assert n.status()["witness_backstop_at"] is not None and n.status()["last_error"] is None
            assert _calls(worker) == before                  # alone, it waits for its stamp
        finally:
            notary.stop_witness_listener()


def test_the_backstop_offset_is_a_constant_of_the_node_key():
    a, b = notary.backstop_offset(NODE_PUB), notary.backstop_offset(_key_hex())
    assert a == notary.backstop_offset(NODE_PUB)
    assert 0 <= a < notary.WITNESS_OFFSET_S and 0 <= b < notary.WITNESS_OFFSET_S
    n = notary._Notary()
    with _running(n):
        assert n._offset == a


# ---------------------------------------------------------------------------
# Migrations, rotation, backup
# ---------------------------------------------------------------------------

def test_an_identity_rule_migration_leaves_witness_rows_whole(worker, conn, stage, scratch_dsn):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from canon import migrations as canon_migrations
    assert "p2p_witness" not in canon_migrations._SEALED_TABLES
    subject = _sighting()
    sign_audio.sign_witness(stage)
    sign_audio.stamp(stage, witness_only=True)
    before = _row(conn, subject)
    assert _stamped(before)
    engine = create_engine(scratch_dsn.replace("postgresql://", "postgresql+psycopg2://"))
    try:
        with Session(engine) as db:
            shed = canon_migrations._shed_all_seals(db)
    finally:
        engine.dispose()
    assert shed["signing_batches"] >= 1
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM signing_batches")
        assert cur.fetchone()["n"] == 0
    assert _row(conn, subject) == before
    assert sign_audio.verify_all()                       # by its own copy of the stamp


def test_a_rotation_signs_the_waiting_rows_with_the_old_key(conn, tmp_path, monkeypatch):
    import device_auth
    monkeypatch.setattr(settings, "p2p_identity_dir", str(tmp_path))
    monkeypatch.setattr(settings, "p2p_username", "")
    monkeypatch.setattr(settings, "p2p_password", "")
    monkeypatch.setattr(p2p_identity, "_signing_key_cache", None)
    monkeypatch.setattr(p2p_identity, "resolve_identity", lambda s: None)
    device_auth.prepare_setup()
    asyncio.run(device_auth.create_account("first", "correct-horse", device_auth._setup_code))
    old = json.loads((tmp_path / "node_info.json").read_text())["public_key_hex"]
    subjects = [_sighting("handshake") for _ in range(2)]
    assert all(_row(conn, s, "handshake")["signature"] is None for s in subjects)
    asyncio.run(device_auth.rotate_identity("second", "battery-staple", "correct-horse"))
    new = json.loads((tmp_path / "node_info.json").read_text())["public_key_hex"]
    assert new != old
    for s in subjects:
        row = _row(conn, s, "handshake")
        assert row["author_pubkey"] == old
        assert record_sig.verify(witness.statement(row, old), row["signature"], old)


KDF = nb.KdfParams(time_cost=1, memory_cost=8 * 1024, parallelism=1)


def _target(dsn: str, dbname=None) -> nb.PgTarget:
    url = urlparse(dsn)
    return nb.PgTarget(host=url.hostname, port=url.port, user=url.username, password=url.password,
                       dbname=dbname or url.path.lstrip("/"),
                       pg_bin=Path(os.environ["PG_BIN"]) if os.environ.get("PG_BIN") else None)


def test_a_backup_round_trip_keeps_the_rows_and_their_guard(worker, scratch_dsn, conn, stage, tmp_path):
    _sighting()
    sign_audio.sign_witness(stage)
    sign_audio.stamp(stage, witness_only=True)
    _sighting("introduced")                                       # one unsigned too
    source = _target(scratch_dsn)
    restored = _target(scratch_dsn, "sautium_witness_restore")
    created = nb.create_backup(tmp_path, target=source, kek=nb.derive_kek("pw", "node", KDF),
                               username="node", pubkey=NODE_PUB, kdf=KDF)
    with open(created["path"], "rb") as fp:
        reader = nb.BackupReader(fp)
        reader.unlock("pw", KDF)
        nb.restore_database(reader, restored, manifest=reader.read_manifest(), replace=True)
    try:
        copy = restored.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM p2p_witness ORDER BY id")
                want = [tuple(r.values()) for r in cur.fetchall()]
            with copy.cursor() as cur:
                cur.execute("SELECT * FROM p2p_witness ORDER BY id")
                assert cur.fetchall() == want
                assert nb.has_own_data(copy)
                with pytest.raises(psycopg2.errors.RaiseException, match="never deleted"):
                    cur.execute("DELETE FROM p2p_witness")
            copy.rollback()
        finally:
            copy.close()
    finally:
        admin = psycopg2.connect(scratch_dsn.rsplit("/", 1)[0] + "/postgres")
        admin.autocommit = True
        for name in ("sautium_witness_restore", "sautium_witness_restore__previous"):
            nb._drop_database(admin, name)
        admin.close()


@contextmanager
def _fresh_database(scratch_dsn, name):
    """A database of its own built from the migrations: (name, connection)."""
    from desktop import db_init
    base = scratch_dsn.rsplit("/", 1)[0]
    admin = psycopg2.connect(base + "/postgres")
    admin.autocommit = True
    nb._drop_database(admin, name)
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {name}")
    c = psycopg2.connect(f"{base}/{name}")
    try:
        db_init.apply_migrations(c)
        c.commit()
        yield name, c
    finally:
        c.close()
        nb._drop_database(admin, name)
        admin.close()


def test_a_statement_version_this_code_does_not_know_stops_nothing(scratch_dsn, monkeypatch):
    with _fresh_database(scratch_dsn, "sautium_witness_newer") as (name, c):
        monkeypatch.setattr(settings, "postgres_db", name)
        insert = """INSERT INTO p2p_witness (witness_v, subject, cert_payload, cert_sig, issuer, cert_v,
                                               issued_at, method, difficulty, params_version, channel,
                                               author_pubkey, signature)
                     VALUES (%s, %s, %s, %s, %s, 4, now(), 'pow', 32, 1, 'probe', %s, %s)"""
        known, newer = _key_hex(), _key_hex()
        cert = certificate(known)
        with c.cursor() as cur:
            cur.execute(insert, (2, newer, "x", "ab" * 64, ISSUER, None, None))  # merged from a newer node
            cur.execute(insert, (1, known, birth_cert.canonical_payload(cert).decode(), cert["sig"], ISSUER,
                                 None, None))
            cur.execute(insert, (2, _key_hex(), "x", "ab" * 64, ISSUER, NODE_PUB, "cd" * 64))
        c.commit()
        stage = sign_audio.connect()
        try:
            assert sign_audio.sign_witness(stage) == 1
        finally:
            stage.close()
        with c.cursor() as cur:
            cur.execute("SELECT subject, signature IS NOT NULL FROM p2p_witness WHERE subject = ANY(%s)",
                        ([known, newer],))
            assert dict(cur.fetchall()) == {known: True, newer: False}
        assert sign_audio.verify_all()


def test_witness_records_alone_are_own_data(scratch_dsn):
    with _fresh_database(scratch_dsn, "sautium_witness_own_data") as (_name, c):
        assert not nb.has_own_data(c)
        with c.cursor() as cur:
            cur.execute("""INSERT INTO p2p_witness (subject, cert_payload, cert_sig, issuer, cert_v,
                                                    issued_at, method, difficulty, params_version, channel)
                           VALUES (%s, 'x', %s, %s, 4, now(), 'pow', 32, 1, 'probe')""",
                        (_key_hex(), "ab" * 64, ISSUER))
        c.commit()
        assert nb.has_own_data(c)


# ---------------------------------------------------------------------------
# The Docker peer surface
# ---------------------------------------------------------------------------

def _docker_app():
    from fastapi import FastAPI

    import p2p_app
    app = FastAPI()

    @app.get("/api/sync/witness-test")
    async def ok():
        return {"ok": True}
    app.add_middleware(p2p_app.PeerAuthMiddleware)
    return app


def _signed(client, path="/api/sync/witness-test", cert=None) -> dict:
    headers = peer_auth.sign_headers(client.sign, client.public_key().public_bytes_raw().hex(),
                                     NODE_PUB, "GET", path, b"")
    if cert is not None:
        headers[peer_auth.HDR_CERT] = peer_auth.encode_cert_bundle(cert, None)
    return headers


def test_the_docker_surface_records_an_introduction(conn):
    from fastapi.testclient import TestClient
    client = Ed25519PrivateKey.generate()
    pub = client.public_key().public_bytes_raw().hex()
    with TestClient(_docker_app()) as http:
        r = http.get("/api/sync/witness-test", headers=_signed(client, cert=certificate(pub)))
        assert r.status_code == 200 and r.headers[peer_auth.HDR_IDENTITY] == "unverified"
        r = http.get("/api/sync/witness-test", headers=_signed(client))          # no introduction
        assert r.status_code == 200
    assert _row(conn, pub, "introduced") is not None
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM p2p_witness WHERE subject = %s", (pub,))
        assert cur.fetchone()["n"] == 1


def test_the_docker_surface_records_after_the_gate_and_never_fails_on_the_record(conn, monkeypatch, caplog):
    from fastapi.testclient import TestClient

    import p2p_app

    class Enforcing:
        def would_be(self, *_a):
            return 5

        def mode(self):
            return "enforce"
    unpaid, broken = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    unpaid_pub = unpaid.public_key().public_bytes_raw().hex()
    broken_pub = broken.public_key().public_bytes_raw().hex()
    with TestClient(_docker_app()) as http:
        with monkeypatch.context() as m:
            m.setattr(p2p_app, "_price", lambda: Enforcing())
            assert http.get("/api/sync/witness-test",
                            headers=_signed(unpaid, cert=certificate(unpaid_pub))).status_code == 402

        def fail(*_a, **_k):
            raise RuntimeError("the database is gone")
        monkeypatch.setattr(witness.Recorder, "record", fail)
        r = http.get("/api/sync/witness-test", headers=_signed(broken, cert=certificate(broken_pub)))
        assert r.status_code == 200 and r.headers[peer_auth.HDR_IDENTITY] == "unverified"
    assert _row(conn, unpaid_pub, "introduced") is None and _row(conn, broken_pub, "introduced") is None
    assert any("could not be recorded: the database is gone" in r.getMessage() for r in caplog.records)


def test_the_docker_probe_sink_records_a_peers_certificate(conn):
    sink = p2p_identity.peer_identity(settings).witness
    peer = _key_hex()
    sink(certificate(peer), peer)
    assert _row(conn, peer, "probe") is not None
    sink(certificate(NODE_PUB), NODE_PUB)                    # this node's own: never
    assert _row(conn, NODE_PUB, "probe") is None


def test_the_docker_health_serves_the_certificate_never_the_proof(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import p2p_app
    monkeypatch.setattr(settings, "p2p_identity_dir", str(tmp_path))
    monkeypatch.setattr(p2p_identity, "_served", (None, None))
    own = certificate(NODE_PUB)
    (tmp_path / p2p_identity.CERT_FILENAME).write_text(json.dumps(own))
    (tmp_path / "identity_proof.json").write_text(json.dumps({"mined_at": "2026-11-01T00:00:00Z"}))
    with TestClient(p2p_app.app) as http:
        body = http.get("/health").json()
        assert body["node_id"] == NODE_PUB and body["birth_cert"] == own
        assert "mined_at" not in json.dumps(body)
        upgraded = certificate(NODE_PUB, method="email", email_token="ab" * 32, email_class="major")
        (tmp_path / p2p_identity.CERT_FILENAME).write_text(json.dumps(upgraded))
        os.utime(tmp_path / p2p_identity.CERT_FILENAME, ns=(time.time_ns(), time.time_ns() + 10**9))
        assert http.get("/health").json()["birth_cert"] == upgraded
        (tmp_path / p2p_identity.CERT_FILENAME).unlink()
        assert http.get("/health").json()["birth_cert"] is None


def test_the_docker_handshake_records_an_accepted_token_only(conn, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from p2p_chat import PeerChatService
    from routers import peer_chat
    service = PeerChatService(NODE.private_bytes_raw(), NODE_PUB, ())
    monkeypatch.setattr(peer_chat, "get_peer_chat", lambda: service)
    monkeypatch.setattr(peer_chat, "resolve_identity", lambda s: IDENTITY)
    monkeypatch.setattr(peer_chat, "load_signing_key", lambda s: NODE)
    app = FastAPI()
    app.include_router(peer_chat.chat_router)

    def guest_body(key, *, token=None):
        pub = key.public_key().public_bytes_raw()
        body = {"public_key_hex": pub.hex(), "username": "guest",
                "invite_code": node_identity.make_invite_code("guest", pub), "birth_cert": certificate(pub.hex())}
        if token:
            ts = int(time.time())
            body.update(token_id=token, ts=ts, signature=key.sign(
                f"token_handshake:{ts}:{token}:{IDENTITY['invite_code']}".encode()).hex())
        else:
            from desktop.p2p import chat_wire
            body = chat_wire.sign_handshake(key.sign, body, IDENTITY["invite_code"])
        return body

    tid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO invite_tokens (id, label, max_uses) VALUES (%s, 'test', 5)", (tid,))
    guest, stranger, friend = (Ed25519PrivateKey.generate() for _ in range(3))
    fpub = friend.public_key().public_bytes_raw()
    service.add_friend(fpub.hex(), node_identity.make_invite_code("guest", fpub), "guest")
    with TestClient(app) as http:
        r = http.post("/api/chat/handshake", json=guest_body(guest, token=tid))
        assert r.status_code == 200 and r.json()["accepted"]
        assert http.post("/api/chat/handshake", json=guest_body(stranger, token=str(uuid.uuid4()))).status_code == 403
        assert http.post("/api/chat/handshake", json=guest_body(stranger)).status_code == 403
        r = http.post("/api/chat/handshake", json=guest_body(friend))           # classic, accepted
        assert r.status_code == 200 and r.json()["accepted"]
        gpub = guest.public_key().public_bytes_raw().hex()
        grant = http.post("/api/chat/handshake", json=guest_body(guest, token=tid)).json()["grant"]
        upgraded = guest_body(guest, token=tid)                                   # the re-accept…
        upgraded["birth_cert"] = certificate(gpub, method="email", email_token="ab" * 32, email_class="major")
        assert http.post("/api/chat/handshake", json=upgraded).status_code == 200
        with conn.cursor() as cur:
            cur.execute("DELETE FROM friends WHERE public_key_hex = %s", (gpub,))
        recovered = guest_body(guest)                                             # …and the grant recovery
        ts = int(time.time())
        recovered.update(grant=grant, ts=ts, birth_cert=certificate(
            gpub, method="email", email_token="cd" * 32, email_class="other"), signature=guest.sign(
            f"grant_handshake:{ts}:{grant['token_id']}:{IDENTITY['invite_code']}".encode()).hex())
        assert http.post("/api/chat/handshake", json=recovered).status_code == 200
    spub = stranger.public_key().public_bytes_raw().hex()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM p2p_witness WHERE subject = %s AND channel = 'handshake'",
                    (gpub,))
        assert cur.fetchone()["n"] == 3                  # the accept, the re-accept's and the recovery's
        cur.execute("SELECT count(*) AS n FROM p2p_witness WHERE subject = ANY(%s)", ([spub, fpub.hex()],))
        assert cur.fetchone()["n"] == 0
