"""A start that finds no identity (desktop/node_identity.ensure_identity — the
launcher settles its identity there before P2P runs): the one on disk is kept;
with none, a database holding no seal of this node's own analysis gets a new
one; one that holds such seals — signed by the identity that is gone — gets
none, and only the owner's choice (generate_identity) makes one; a database
that cannot be read decides nothing. The seals are read by the check the
analysis records' cutover fails closed on (SEALED_FIRST_HAND_SQL). On a real
PostgreSQL built from the migrations; skipped without a cluster."""

import json
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from desktop import node_identity, sync_client  # noqa: E402

MODEL = sync_client.model_uuid("clap")
SEAL = ("ab" * 32, "5e" * 64)                      # an author key and a signature, as stored


@pytest.fixture
def folder(tmp_path, monkeypatch):
    """The identity folder of a config dir of its own."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    return tmp_path / "Sautium" / "node_identity"


@pytest.fixture
def conn(scratch_dsn):
    c = psycopg2.connect(scratch_dsn)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("TRUNCATE tracks, embedding_models CASCADE")
        cur.execute("INSERT INTO embedding_models (id, name, dimension) VALUES (%s, 'clap', 512)",
                    (MODEL,))
    yield c
    c.close()


def _track(cur) -> str:
    tid = str(uuid.uuid4())
    cur.execute("INSERT INTO tracks (id, title) VALUES (%s, 't')", (tid,))
    return tid


def _source(cur, track, imported=False) -> int:
    cur.execute("""INSERT INTO analysis_sources (track_id, chromaprint, duration_seconds, imported)
                   VALUES (%s, 'AQAAidentity', 240, %s) RETURNING id""", (track, imported))
    return cur.fetchone()[0]


def _record(cur, kind, source, seal=(None, None)) -> None:
    """A record of `kind` under `source` (None: under no source)."""
    track = _track(cur)
    if kind == "segments":
        cur.execute("""INSERT INTO embeddings (track_id, model_id, vector, analysis_source_id)
                       VALUES (%s, %s, %s::vector, %s) RETURNING id""",
                    (track, MODEL, "[" + ",".join(["0.04"] * 512) + "]", source))
        cur.execute("""INSERT INTO embedding_segments (embedding_id, segment_index, vector,
                                                       author_pubkey, signature)
                       VALUES (%s, 0, %s::vector, %s, %s)""",
                    (cur.fetchone()[0], "[" + ",".join(["0.04"] * 512) + "]", *seal))
    elif kind == "instruments":
        cur.execute("""INSERT INTO track_instruments (track_id, instruments, analysis_source_id,
                                                      analysis_version, author_pubkey, signature)
                       VALUES (%s, '{}', %s, 2, %s, %s)""", (track, source, *seal))
    else:
        cur.execute("""INSERT INTO audio_features (track_id, bpm, analysis_source_id,
                                                   author_pubkey, signature)
                       VALUES (%s, 99.0, %s, %s, %s)""", (track, source, *seal))


def test_the_identity_on_disk_is_the_one_a_start_takes(conn, scratch_dsn, folder):
    with conn.cursor() as cur:
        _record(cur, "features", _source(cur, _track(cur)), SEAL)
    node = node_identity.generate_identity()
    assert node_identity.ensure_identity(scratch_dsn) == "present"
    assert json.loads((folder / "node_info.json").read_text())["node_id"] == node


def test_no_identity_over_no_seal_of_this_nodes_own_gets_a_new_one(conn, scratch_dsn, folder):
    """Its own analysis unsealed, another author's sealed under an imported
    source: nothing says the node ever had an identity."""
    with conn.cursor() as cur:
        for kind in ("segments", "features", "instruments"):
            _record(cur, kind, _source(cur, _track(cur)))
            _record(cur, kind, _source(cur, _track(cur), imported=True), SEAL)
    assert node_identity.ensure_identity(scratch_dsn) == "minted"
    assert (folder / "node_info.json").exists() and (folder / "node_ed25519.key").exists()


@pytest.mark.parametrize("kind,under", [("segments", "own"), ("features", "own"),
                                        ("instruments", "own"), ("features", "none")])
def test_no_identity_over_seals_of_its_own_gets_none_until_the_owner_chooses(
        conn, scratch_dsn, folder, kind, under):
    with conn.cursor() as cur:
        _record(cur, kind, _source(cur, _track(cur)) if under == "own" else None, SEAL)
    assert node_identity.ensure_identity(scratch_dsn) == "missing"
    assert not (folder / "node_info.json").exists()
    assert not (folder / "node_ed25519.key").exists()
    assert node_identity.ensure_identity(scratch_dsn) == "missing"   # every start, the same
    node_identity.generate_identity()                                 # the owner's choice
    assert node_identity.ensure_identity(scratch_dsn) == "present"


def test_a_database_that_cannot_be_read_decides_nothing(folder):
    with pytest.raises(psycopg2.OperationalError):
        node_identity.ensure_identity("postgresql://nobody:none@127.0.0.1:1/none")
    assert not (folder / "node_info.json").exists()
