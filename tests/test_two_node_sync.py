"""Two real nodes syncing — BIRTH-SYNC slice 2's gate scenario, kept as a
regression test of the sync's hottest path: the launcher's SyncServer
(`build_app`, every route) served over TLS bound to its key, and the Docker
peer surface (`p2p_app.app`) served over TLS bound to its key by the peer
protocol, each on a PostgreSQL of its own built from the migrations. Each
node's walk (`SyncWalk._sync_from_peer`, the runtimes' own) pulls its gaps
from the other, then pushes its own first-hand analysis back (carry); a
push of a third author's records goes over each carrier's route too. The
records are sealed by their authors under the test authority
(tests/p2p/sealed_records.py) and verified by the import gate end to end.

Checked: each node witnesses the other by its probe and by its
introduction; an author whose analysis the other node carried is witnessed
`carried`, delivered by that node — pulled (its certificate alone when that
node only witnessed it, with its proof when it verified it) or pushed — and
the pusher of its own analysis never is; the sync writes no identity row for
a carried author and evaluates no proof; the Docker node's own pull
endpoints serve the same `authors` map the shared handlers build.

One process holds both nodes, so what is process-wide is shared:
- config.settings and db_pool are the Docker node's alone; the launcher
  reads through its SyncServer's own pool and the walk's own connections,
  so neither node reads the other's database;
- the two witness records are separate objects (SyncServer.witness,
  p2p_identity.witness_recorder), each with its own memo;
- the proof evaluator's permit and its rejected-nonce memory
  (identity_registry._EVALUATION, _REJECTED) are never reached: the
  evaluation is patched to fail the test;
- no load meter runs, for either node; no assertion reads a price;
- the holdings filter is one per process (sync_queries._holdings), built by
  whichever node is first asked for it and served by both: it is emptied
  before each walk, so the node a walk asks builds and serves its own.
Skipped without a cluster."""

import asyncio
import json
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

import birth_authority  # noqa: E402
import db_pool  # noqa: E402
import p2p_identity  # noqa: E402
from config import settings  # noqa: E402
from desktop import node_identity, sync_client  # noqa: E402
from desktop.api_client import BackendAPIClient  # noqa: E402
from desktop.p2p import (birth_cert, identity_proof, identity_registry,  # noqa: E402
                         sync_queries, sync_walk, witness)
from desktop.p2p.sync_server import SyncServer  # noqa: E402
from tests.p2p.sealed_records import pub, seal_track  # noqa: E402
from tests.p2p.test_witness import ISSUER, certificate  # noqa: E402
from tests.p2p.test_witness_launcher import Served, _tls_context  # noqa: E402

MODEL = str(uuid.UUID(int=0xB22))


class _Node:
    def __init__(self, name: str):
        self.name, self.key = name, Ed25519PrivateKey.generate()
        self.pub = pub(self.key)
        self.cert = certificate(self.pub)
        self.proof = {"v": identity_proof.PROOF_VERSION, "pubkey": self.pub,
                      "cert_sig": self.cert["sig"], "nonce": "5a" * 16, "difficulty": 32,
                      "params_version": 1, "mined_at": "2026-11-09T08:00:00Z"}
        self.info = {"node_id": self.pub, "public_key_hex": self.pub, "username": name,
                     "invite_code": node_identity.make_invite_code(name, bytes.fromhex(self.pub))}

    @property
    def travels(self) -> tuple:
        """Its certificate and proof as an entry of its own build carries them."""
        return self.cert, {k: v for k, v in self.proof.items() if k != "mined_at"}


@pytest.fixture(scope="module")
def launcher_dsn(scratch_dsn):
    """The launcher node's own database (the module's is the Docker node's)."""
    from desktop import db_init
    from tests.conftest import PG, _recreate
    name = "sautium_test_two_node_sync_launcher"
    assert _recreate(name, create=True)
    conn = psycopg2.connect(dbname=name, **PG)
    db_init.apply_migrations(conn)
    conn.commit()
    conn.close()
    yield f"postgresql://{PG['user']}:{PG['password']}@{PG['host']}:{PG['port']}/{name}"
    _recreate(name, create=False)


def _connect(dsn):
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    return conn


class _Docker:
    """`p2p_app.app` on uvicorn with the peer protocol, over TLS bound to the
    node's key, on its own loop thread."""

    def __init__(self, node: _Node, tls_dir: Path):
        import ssl

        import uvicorn
        from uvicorn.protocols.http.auto import AutoHTTPProtocol

        import p2p_app
        import serving
        import tls_gen
        cert, key = tls_gen.ensure_cert(tls_dir, binding=(node.pub, node.key.sign))
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        http = serving.peer_protocol(AutoHTTPProtocol, head_s=10, head_max=65536, stall_s=10,
                                     max_connections=64, per_address=None, tls=ctx)
        self.server = uvicorn.Server(uvicorn.Config(
            p2p_app.app, host="127.0.0.1", port=0, http=http, lifespan="off",
            log_level="warning", timeout_keep_alive=1, timeout_graceful_shutdown=1))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            assert time.monotonic() < deadline, "the Docker peer surface never started"
            time.sleep(0.05)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"https://127.0.0.1:{port}"

    def close(self):
        self.server.should_exit = True
        self.thread.join(10)


def _snapshot(cur, node: _Node, track: str, recording: str) -> None:
    """The canon a node's own first-hand analysis is pushed under: the track
    on a sealed tracklist row of a sealed album anchored in MusicBrainz."""
    artist, album = str(uuid.uuid4()), str(uuid.uuid4())
    cur.execute("INSERT INTO artists (id, name) VALUES (%s, %s)", (artist, f"by {node.name}"))
    cur.execute("INSERT INTO track_artists (track_id, artist_id, role) VALUES (%s, %s, 'primary')",
                (track, artist))
    cur.execute("INSERT INTO albums (id, title, musicbrainz_id, author_pubkey, signature) "
                "VALUES (%s, 'LP', %s, %s, %s)", (album, str(uuid.uuid4()), node.pub, "ab" * 64))
    cur.execute("INSERT INTO album_tracks (album_id, track_id, position, recording_mbid, "
                "author_pubkey, signature) VALUES (%s, %s, 1, %s, %s, %s)",
                (album, track, recording, node.pub, "cd" * 64))


def _wanted(cur, track: str, recording: str) -> None:
    """A phantom slot of this node's catalogue for `track`: a carrier wants it."""
    album = str(uuid.uuid4())
    cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'wanted')", (album,))
    cur.execute("INSERT INTO album_tracks (album_id, track_id, position, recording_mbid) "
                "VALUES (%s, %s, 1, %s)", (album, track, recording))


def _rows(conn, subject) -> list:
    with conn.cursor() as cur:
        cur.execute("SELECT channel::text, via FROM p2p_witness WHERE subject = %s ORDER BY id",
                    (subject,))
        return [(r[0], r[1]) for r in cur.fetchall()]


def _registered(conn, subject) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM p2p_identities WHERE pubkey = %s", (subject,))
        return cur.fetchone() is not None


def _holds(conn, track) -> dict:
    with conn.cursor() as cur:
        cur.execute("""SELECT (SELECT count(*) FROM embeddings e JOIN embedding_segments es
                                  ON es.embedding_id = e.id WHERE e.track_id = %(t)s),
                              (SELECT count(*) FROM audio_features WHERE track_id = %(t)s),
                              (SELECT count(*) FROM track_mbids WHERE track_id = %(t)s)""", {"t": track})
        segments, features, bindings = cur.fetchone()
    return {"segments": segments, "features": features, "bindings": bindings}


@pytest.fixture
def world(scratch_dsn, launcher_dsn, monkeypatch, tmp_path):
    """Two nodes, served, with their data; authors C (analysis carried by
    the Docker node, which only witnessed it), E (carried by the launcher,
    which verified it), F and G (third authors pushed by hand)."""
    for mirror in (birth_cert, witness, sync_client, birth_authority):
        monkeypatch.setattr(mirror, "TRUSTED_AUTHORITIES", [ISSUER])

    def evaluate(*_a, **_k):
        raise AssertionError("the sync evaluated a proof")
    monkeypatch.setattr(identity_proof, "verify_proof", evaluate)
    monkeypatch.setattr(identity_registry, "verify_relayed", evaluate)

    L, D = _Node("launcher"), _Node("docker")
    C, E, F, G = (_Node(n) for n in "CEFG")
    tracks = {n: str(uuid.uuid4()) for n in ("C", "E", "F", "G", "L", "D")}
    recordings = {n: str(uuid.uuid4()) for n in tracks}

    # The Docker node: config.settings and db_pool on the module's database,
    # its identity files in a folder of its own.
    url = urlparse(scratch_dsn)
    for key, value in dict(postgres_host=url.hostname, postgres_port=url.port,
                           postgres_user=url.username, postgres_password=url.password,
                           postgres_db=url.path.lstrip("/")).items():
        monkeypatch.setattr(settings, key, value)
    db_pool.close_pool()
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir()
    (docker_dir / p2p_identity.CERT_FILENAME).write_text(json.dumps(D.cert))
    (docker_dir / identity_proof.PROOF_FILENAME).write_text(json.dumps(D.proof))
    monkeypatch.setattr(settings, "p2p_identity_dir", str(docker_dir))
    monkeypatch.setattr(p2p_identity, "_served", (None, None))
    monkeypatch.setattr(p2p_identity, "_signing_key_cache", D.key)
    monkeypatch.setattr(p2p_identity, "_witness", None)
    monkeypatch.setattr(p2p_identity, "resolve_identity", lambda s: D.info)

    # The launcher node: its identity in its config folder, as an account
    # leaves it; its sync server and its manager's client identity.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "launcher"))
    node_identity._save_keypair(L.key, L.info)
    (node_identity._identity_dir() / "birth_certificate.json").write_text(json.dumps(L.cert))
    (node_identity._identity_dir() / identity_proof.PROOF_FILENAME).write_text(json.dumps(L.proof))
    monkeypatch.setattr(birth_cert, "_served", (None, None))
    srv = SyncServer(launcher_dsn, port=0, node_id=L.pub, account_info=L.info)
    from desktop.p2p.p2p_manager import P2PManager
    manager = P2PManager("unused", {})
    manager._sync_server = srv

    docker_db, launcher_db = _connect(scratch_dsn), _connect(launcher_dsn)
    for conn in (docker_db, launcher_db):
        with conn.cursor() as cur:
            cur.execute("TRUNCATE tracks, albums, artists, embedding_models, signing_batches CASCADE")
            cur.execute("INSERT INTO embedding_models (id, name, dimension) VALUES (%s, 'clap', 512)",
                        (MODEL,))
            for n, track in tracks.items():
                cur.execute("INSERT INTO tracks (id, title) VALUES (%s, %s)", (track, f"track {n}"))
    docker_record = p2p_identity.witness_recorder(settings)
    with docker_db.cursor() as cur:
        for author in (C, G):                       # the Docker node met them; never verified
            assert docker_record.record(author.cert, "introduced", author.pub)
            seal_track(cur, author.key, tracks[author.name], MODEL, recording=recordings[author.name])
        seal_track(cur, D.key, tracks["D"], MODEL, recording=recordings["D"], first_hand=True)
        _snapshot(cur, D, tracks["D"], recordings["D"])
        for n in ("L", "F"):                         # what it would take from the launcher
            _wanted(cur, tracks[n], recordings[n])
    with launcher_db.cursor() as cur:
        identity_registry.observe(launcher_db, E.cert, proof=dict(E.proof))
        identity_registry.mark_verified(launcher_db, E.pub, E.proof["nonce"])
        assert srv.witness.record(F.cert, "introduced", F.pub)
        for author in (E, F):
            seal_track(cur, author.key, tracks[author.name], MODEL, recording=recordings[author.name])
        seal_track(cur, L.key, tracks["L"], MODEL, recording=recordings["L"], first_hand=True)
        _snapshot(cur, L, tracks["L"], recordings["L"])
        for n in ("D", "G"):
            _wanted(cur, tracks[n], recordings[n])

    docker = _Docker(D, tmp_path / "tls")
    launcher = Served(srv.build_app(), _tls_context(L))
    try:
        yield dict(L=L, D=D, C=C, E=E, F=F, G=G, tracks=tracks, srv=srv, manager=manager,
                   docker=docker, launcher=launcher, docker_db=docker_db, launcher_db=launcher_db)
    finally:
        launcher.close()
        docker.close()
        docker_db.close()
        launcher_db.close()
        if srv._pool is not None:
            srv._pool.closeall()
        db_pool.close_pool()


def _walk(dsn, identity, monkeypatch) -> sync_walk.SyncWalk:
    """A node's walk, the process's holdings filter emptied first: the node
    it asks builds its own (the module doc)."""
    monkeypatch.setattr(sync_queries, "_holdings",
                        {"checked_at": 0.0, "version": None, "payload": None, "tracks_n": 0})
    return sync_walk.SyncWalk(dsn, identity=identity, sharing_enabled=lambda: True)


def _push_by_hand(api: BackendAPIClient, conn, category: str, track: str) -> int:
    """What a node holds of `track` pushed to the peer behind `api`, part by
    part, as the walk pushes — the records' author is not the pusher."""
    took = 0
    for part in sync_walk.push_parts(sync_queries.PULL_HANDLERS[category](conn, [track])):
        took += (api.carry_push(category.replace("_", "-"), part) or {}).get("imported", 0)
    return took


def test_two_nodes_witness_the_authors_of_what_they_pull_and_carry(world, scratch_dsn, launcher_dsn,
                                                                   monkeypatch):
    w = world
    L, D, C, E, F, G, t = w["L"], w["D"], w["C"], w["E"], w["F"], w["G"], w["tracks"]
    say = lambda msg: None                                      # noqa: E731

    # The launcher's walk: its gap from the Docker node, then its own carried there.
    launcher_walk = _walk(launcher_dsn, w["manager"]._peer_identity, monkeypatch)
    stats = asyncio.run(launcher_walk._sync_from_peer(w["docker"].url, ([t["C"]], []), say, None))
    assert stats == {"segments": 1, "audio_features": 1}, stats
    # The Docker node's walk: its gap from the launcher, then its own carried there.
    docker_walk = _walk(scratch_dsn, lambda: p2p_identity.peer_identity(settings), monkeypatch)
    stats = asyncio.run(docker_walk._sync_from_peer(w["launcher"].url, ([t["E"]], []), say, None))
    assert stats == {"segments": 1, "audio_features": 1}, stats

    ldb, ddb = w["launcher_db"], w["docker_db"]
    assert _holds(ldb, t["C"]) == {"segments": 3, "features": 1, "bindings": 0}      # pulled
    assert _holds(ldb, t["D"]) == {"segments": 3, "features": 1, "bindings": 1}      # carried to it
    assert _holds(ddb, t["E"]) == {"segments": 3, "features": 1, "bindings": 0}
    assert _holds(ddb, t["L"]) == {"segments": 3, "features": 1, "bindings": 1}

    # A third author's records pushed by hand over each carrier's route.
    to_docker = BackendAPIClient(w["docker"].url, peer=w["manager"]._peer_identity())
    assert to_docker.get_health()["node_id"] == D.pub
    assert _push_by_hand(to_docker, ldb, "segments", t["F"]) == 1
    to_launcher = BackendAPIClient(w["launcher"].url, peer=p2p_identity.peer_identity(settings))
    assert to_launcher.get_health()["node_id"] == L.pub
    assert _push_by_hand(to_launcher, ddb, "audio_features", t["G"]) == 1

    # What each node witnessed: the other by its probe and its introduction.
    assert sorted(_rows(ldb, D.pub)) == [("introduced", None), ("probe", None)]
    assert sorted(_rows(ddb, L.pub)) == [("introduced", None), ("probe", None)]
    assert _rows(ldb, C.pub) == [("carried", D.pub)]            # pulled; D only witnessed C
    assert _rows(ddb, E.pub) == [("carried", L.pub)]            # pulled; L verified E
    assert _rows(ddb, F.pub) == [("carried", L.pub)]            # pushed by L
    assert _rows(ldb, G.pub) == [("carried", D.pub)]            # pushed by D
    # No identity row for an author met only through its data.
    for db, author in ((ldb, C), (ldb, G), (ddb, E), (ddb, F)):
        assert not _registered(db, author.pub), author.name

    # What each surface serves in its `authors` map: the Docker node's own
    # endpoints serve what the shared handlers build; each its own entry with
    # the proof that travels (no mined_at), a verified author with its proof,
    # a witnessed one with its certificate alone.
    from_docker = to_docker.sync_pull("segments", [t["C"], t["D"]])
    assert from_docker["authors"] == sync_queries.pull_segments(
        ddb, [t["C"], t["D"]], own=D.travels)["authors"]
    assert from_docker["authors"] == {
        D.pub: {"author_cert": D.cert, "author_proof": D.travels[1]},
        C.pub: {"author_cert": C.cert, "author_proof": None}}
    from_launcher = to_launcher.sync_pull("segments", [t["E"], t["L"]])
    assert from_launcher["authors"][L.pub] == {"author_cert": L.cert, "author_proof": L.travels[1]}
    assert from_launcher["authors"][E.pub]["author_cert"] == E.cert
    assert identity_proof.proof_binds(from_launcher["authors"][E.pub]["author_proof"], E.cert)
    assert "mined_at" not in json.dumps(from_docker) + json.dumps(from_launcher)
