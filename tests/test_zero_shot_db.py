"""The zero-shot scores as the database derives them (the "Zero-shot scores"
block of 001_initial.sql, delta 054; backend/db_migrate.py's
zero_shot_labels_v1 step; the readers moved onto them).

- The SQL derivation against a float64 reference, correctly rounded sums in
  pure Python: per segment the softmax within each label set of
  scale × (segment · label), per embedding the mean over its segments.
- The triggers on embedding_segments: every statement that writes or removes
  segments leaves each bundle it touched with the scores of the segments it
  has; a cascade takes them; a merge carries them; a seal recomputes nothing.
- A segment that is not a unit vector: the derivation gives its bundle no
  scores and fails no write, and the import gate refuses the bundle; so it
  does a bundle whose model id is not its model name's.
- The data step: another labels file refused, a fresh node's model row, what
  the node held before scored, a second run, its place before the seed, its
  lock against a writer in flight, the model's name held under another id.
- 001 alone and 001 with its deltas hold the same objects, once; the delta
  on a node that ran the earlier files and holds segments.
- The readers' gates and display on a small library, a score at its
  threshold included.
Skipped without a cluster."""

import base64
import logging
import math
import random
import struct
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402

import zero_shot  # noqa: E402
from desktop.p2p import record_sig  # noqa: E402
from tests import conftest  # noqa: E402
from uuid_utils import embedding_model_uuid  # noqa: E402

DOC = zero_shot.load_labels()
SCALE = DOC["logit_scale"]
MODEL = str(embedding_model_uuid(DOC["model"]))


def _f32(values) -> list:
    """The values as float32 holds them: what the database stores."""
    return list(struct.unpack(f"<{len(values)}f", struct.pack(f"<{len(values)}f", *values)))


SETS = {name: [(l["key"], list(struct.unpack("<512f", base64.b64decode(l["vector"]))))
               for l in labels] for name, labels in DOC["sets"].items()}
LABEL = {key: vector for labels in SETS.values() for key, vector in labels}
KEYS = list(LABEL)


def _unit(values) -> list:
    norm = math.sqrt(math.fsum(x * x for x in values))
    return _f32([x / norm for x in values])


def _noise(rng) -> list:
    return _unit([rng.gauss(0.0, 1.0) for _ in range(512)])


def _near(rng, key: str, pull: float) -> list:
    """A unit vector leaning toward a label: a peaked softmax."""
    return _unit([pull * a + (1 - pull) * b for a, b in zip(LABEL[key], _noise(rng))])


def reference(segments: list) -> dict:
    """label key → score, the derivation written out in float64 with
    correctly rounded sums: an independent copy of what the SQL must give."""
    out = {}
    for labels in SETS.values():
        probs = []
        for segment in segments:
            logits = [SCALE * math.fsum(a * b for a, b in zip(segment, vector))
                      for _key, vector in labels]
            top = max(logits)
            weights = [math.exp(x - top) for x in logits]
            total = math.fsum(weights)
            probs.append([w / total for w in weights])
        for j, (key, _vector) in enumerate(labels):
            out[key] = math.fsum(p[j] for p in probs) / len(probs)
    return out


def _vec(values) -> str:
    return "[" + ",".join(repr(v) for v in values) + "]"


def _track(cur, title="t") -> str:
    track = str(uuid.uuid4())
    cur.execute("INSERT INTO tracks (id, title) VALUES (%s, %s)", (track, title))
    return track


def _embedding(cur, track: str, model: str = MODEL, source=None) -> int:
    cur.execute("""INSERT INTO embeddings (track_id, model_id, vector, analysis_source_id,
                                           analysis_version)
                   VALUES (%s, %s, %s::vector, %s, 2) RETURNING id""",
                (track, model, _vec(LABEL["vocal"]), source))
    return cur.fetchone()[0]


def _write(cur, rows: list) -> None:
    """Segments (embedding, index, vector) in ONE statement."""
    psycopg2.extras.execute_values(
        cur, "INSERT INTO embedding_segments (embedding_id, segment_index, vector) VALUES %s",
        [(e, i, _vec(v)) for e, i, v in rows], template="(%s, %s, %s::vector)", page_size=1000)


def _bundle(cur, segments: list, title="t") -> tuple:
    track = _track(cur, title)
    embedding = _embedding(cur, track)
    _write(cur, [(embedding, i, v) for i, v in enumerate(segments)])
    return track, embedding


def _scores(cur, embedding: int) -> dict:
    cur.execute("""SELECT l.key, s.score FROM zero_shot_scores s
                   JOIN zero_shot_labels l ON l.id = s.label_id WHERE s.embedding_id = %s""",
                (embedding,))
    return dict(cur.fetchall())


def _close(got: dict, segments: list) -> None:
    want = reference(segments)
    assert set(got) == set(want)
    worst = max(abs(got[k] - want[k]) for k in want)
    assert worst < 1e-6, worst


@pytest.fixture(scope="module")
def conn(scratch_dsn):
    """The module's database as the migrations leave it: no label, no model."""
    c = psycopg2.connect(scratch_dsn)
    c.autocommit = True
    yield c
    c.close()


@pytest.fixture(scope="module")
def step(scratch_dsn):
    """The data step on a connection of its own, as apply_pending runs it."""
    db_migrate = pytest.importorskip("db_migrate")

    def run() -> dict:
        own = psycopg2.connect(scratch_dsn)
        try:
            return db_migrate._load_zero_shot_labels(own)
        finally:
            own.close()
    return run


@pytest.fixture(scope="module")
def cur(conn, step):
    """A cursor on the database with the release's labels loaded."""
    step()
    with conn.cursor() as c:
        yield c


# ── the data step ───────────────────────────────────────────────────────────

def test_the_step_refuses_another_labels_file_and_writes_nothing(conn, step, tmp_path, monkeypatch):
    other = tmp_path / "labels.json"
    other.write_text(zero_shot.LABELS_PATH.read_text(encoding="utf-8")
                     .replace("This is singing vocals", "This is a singing voice"), encoding="utf-8")
    monkeypatch.setattr(zero_shot, "LABELS_PATH", other)
    held = """SELECT (SELECT count(*) FROM zero_shot_labels), (SELECT count(*) FROM zero_shot_scores),
                     (SELECT max(xmin::text::bigint) FROM zero_shot_labels),
                     (SELECT array_agg(logit_scale ORDER BY id) FROM embedding_models)"""
    with conn.cursor() as c:
        c.execute(held)
        before = c.fetchone()
        with pytest.raises(ValueError, match="not this release's label file"):
            step()
        c.execute(held)
        assert c.fetchone() == before


def test_a_fresh_node_gets_the_models_row_and_the_labels(conn, step):
    with conn.cursor() as c:
        c.execute("DELETE FROM embedding_models")
    assert step() == {"loaded": True, "labels": 12, "scores": 0, "unscored": 0,
                      "seconds": pytest.approx(0, abs=30)}
    with conn.cursor() as c:
        c.execute("SELECT id::text, name, dimension, logit_scale FROM embedding_models")
        assert c.fetchall() == [(MODEL, DOC["model"], 512, SCALE)]
        c.execute("SELECT label_set::text, key, prompt FROM zero_shot_labels ORDER BY id")
        assert c.fetchall() == [(name, l["key"], l["prompt"])
                                for name, labels in DOC["sets"].items() for l in labels]
        c.execute("SELECT key, vector::real[] FROM zero_shot_labels")
        assert {key: _f32(vector) for key, vector in c.fetchall()} == LABEL


def test_the_step_scores_what_the_node_held_before_it_and_runs_again_to_the_same_rows(conn, step):
    """The cutover's state: segments from before the labels, first-hand and
    imported, under a model row an import made (no scale, no description)."""
    rng = random.Random(54)
    own = [_noise(rng) for _ in range(5)]
    theirs = [_near(rng, "calm and relaxing", 0.6), _noise(rng)]
    with conn.cursor() as c:
        c.execute("DELETE FROM embedding_models")           # the labels and scores with it
        c.execute("INSERT INTO embedding_models (id, name, dimension) VALUES (%s, %s, 512)",
                  (MODEL, DOC["model"]))
        _, mine = _bundle(c, own)
        track = _track(c)
        c.execute("""INSERT INTO analysis_sources (track_id, chromaprint, duration_seconds,
                                                   grid_version, imported)
                     VALUES (%s, 'AQAAimported', 200, %s, true) RETURNING id""",
                  (track, record_sig.GRID_VERSION))
        imported = _embedding(c, track, source=c.fetchone()[0])
        _write(c, [(imported, i, v) for i, v in enumerate(theirs)])
        legacy = _embedding(c, _track(c))                   # a mean with no segments
        _, stretched = _bundle(c, [own[0], [40 * x for x in own[1]]])   # exp() would overflow on it
        c.execute("SELECT count(*) FROM zero_shot_scores")
        assert c.fetchone()[0] == 0

        result = step()
        assert (result["scores"], result["unscored"]) == (24, 1)
        _close(_scores(c, mine), own)
        _close(_scores(c, imported), theirs)
        assert _scores(c, legacy) == {} == _scores(c, stretched)
        c.execute("SELECT logit_scale FROM embedding_models WHERE id = %s", (MODEL,))
        assert c.fetchone()[0] == SCALE

        c.execute("SELECT id, key FROM zero_shot_labels ORDER BY id")
        labels = c.fetchall()
        c.execute("SELECT embedding_id, label_id, score FROM zero_shot_scores ORDER BY 1, 2")
        scores = c.fetchall()
        again = step()
        assert (again["scores"], again["unscored"]) == (24, 1)
        c.execute("SELECT id, key FROM zero_shot_labels ORDER BY id")
        assert c.fetchall() == labels
        c.execute("SELECT embedding_id, label_id, score FROM zero_shot_scores ORDER BY 1, 2")
        assert c.fetchall() == scores


def test_the_step_runs_once_before_the_seed(conn, step, scratch_dsn, monkeypatch):
    """apply_pending on this database with every other step taken as done:
    the labels are in when the seed import is called, and a second start
    does not load them again."""
    db_migrate = pytest.importorskip("db_migrate")
    import seed_import
    from config import settings
    from urllib.parse import urlparse

    url = urlparse(scratch_dsn)
    for key, value in dict(postgres_host=url.hostname, postgres_port=url.port,
                           postgres_user=url.username, postgres_password=url.password,
                           postgres_db=url.path.lstrip("/")).items():
        monkeypatch.setattr(settings, key, value)
    with conn.cursor() as c:
        c.execute("DELETE FROM embedding_models")
        c.execute("DELETE FROM _schema_migrations WHERE filename = 'zero_shot_labels_v1'")
    marked = db_migrate._marked
    monkeypatch.setattr(db_migrate, "_marked", lambda c, name: (
        marked(c, name) if name == "zero_shot_labels_v1" else not name.startswith("seed_v")))
    monkeypatch.setattr(seed_import, "bundle_info", lambda: {"version": 0})
    seen = []

    def apply_seed(seed_conn, dsn, info):
        with seed_conn.cursor() as c:
            c.execute("SELECT count(*) FROM zero_shot_labels")
            seen.append(c.fetchone()[0])
        seed_conn.commit()
        return {"complete": False}
    monkeypatch.setattr(seed_import, "apply_seed", apply_seed)

    assert db_migrate.apply_pending()["zero_shot"]["labels"] == 12
    assert "zero_shot" not in db_migrate.apply_pending()
    assert seen == [12, 12]


def _waits_for_the_lock(c) -> bool:
    """Whether the step stands at its LOCK TABLE, read from pg_stat_activity
    until it does (a bounded wait on the database's own state)."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        c.execute("""SELECT 1 FROM pg_stat_activity
                      WHERE datname = current_database() AND wait_event_type = 'Lock'
                        AND query LIKE 'LOCK TABLE embedding_segments%'""")
        if c.fetchone():
            return True
        time.sleep(0.05)
    return False


def test_the_step_waits_for_a_writer_in_flight_and_scores_its_bundle(conn, step, scratch_dsn):
    """Segments written after the delta and before the step, their writer's
    transaction still open when the step starts: without the lock the step's
    snapshot misses the bundle, and its trigger ran with no labels."""
    rng = random.Random(55)
    committed, flying = [_noise(rng), _noise(rng)], [_noise(rng) for _ in range(3)]
    writer = psycopg2.connect(scratch_dsn)
    done = {}
    try:
        with conn.cursor() as c, writer.cursor() as w:
            c.execute("INSERT INTO embedding_models (id, name, dimension) VALUES (%s, %s, 512) "
                      "ON CONFLICT DO NOTHING", (MODEL, DOC["model"]))
            c.execute("DELETE FROM zero_shot_labels")           # the node between the delta and the step
            _, held = _bundle(c, committed)
            _, in_flight = _bundle(w, flying)                   # not committed
            runner = threading.Thread(target=lambda: done.update(step()))
            runner.start()
            assert _waits_for_the_lock(c), "the step did not wait for the writer"
            c.execute("SELECT count(*) FROM zero_shot_labels")
            assert c.fetchone()[0] == 0
            writer.commit()
            runner.join(30)
            assert done["loaded"]
            _close(_scores(c, held), committed)
            _close(_scores(c, in_flight), flying)
    finally:
        writer.close()


def test_the_models_name_under_another_id_leaves_the_step_undone_and_the_start_alive(
        conn, step, scratch_dsn, monkeypatch, caplog):
    db_migrate = pytest.importorskip("db_migrate")
    import seed_import
    from config import settings
    from urllib.parse import urlparse

    with conn.cursor() as c:
        c.execute("DELETE FROM embedding_models")
        c.execute("DELETE FROM _schema_migrations WHERE filename = 'zero_shot_labels_v1'")
        other = str(uuid.uuid4())
        c.execute("INSERT INTO embedding_models (id, name, dimension) VALUES (%s, %s, 512)",
                  (other, DOC["model"]))
        with caplog.at_level(logging.ERROR, logger="db_migrate"):
            assert step() == {"loaded": False, "model_named_under": other}
        assert "are not loaded" in caplog.text
        c.execute("SELECT count(*) FROM zero_shot_labels")
        assert c.fetchone()[0] == 0

        # the start goes on, and the step is due again at the next one
        url = urlparse(scratch_dsn)
        for key, value in dict(postgres_host=url.hostname, postgres_port=url.port,
                               postgres_user=url.username, postgres_password=url.password,
                               postgres_db=url.path.lstrip("/")).items():
            monkeypatch.setattr(settings, key, value)
        marked = db_migrate._marked
        monkeypatch.setattr(db_migrate, "_marked", lambda cn, name: (
            marked(cn, name) if name == "zero_shot_labels_v1" else True))
        monkeypatch.setattr(seed_import, "bundle_info", lambda: None)
        assert db_migrate.apply_pending()["zero_shot"]["loaded"] is False
        c.execute("SELECT 1 FROM _schema_migrations WHERE filename = 'zero_shot_labels_v1'")
        assert c.fetchone() is None
        c.execute("DELETE FROM embedding_models")
        assert db_migrate.apply_pending()["zero_shot"]["loaded"] is True
        c.execute("SELECT 1 FROM _schema_migrations WHERE filename = 'zero_shot_labels_v1'")
        assert c.fetchone() is not None


# ── the derivation ──────────────────────────────────────────────────────────

def test_the_scores_are_the_mean_over_the_segments_of_each_segments_softmax(cur):
    rng = random.Random(20261110)
    bundles = [
        [_noise(rng)],
        [_noise(rng), _near(rng, "danceable", 0.5), _near(rng, "sad and melancholic", 0.7)],
        [_near(rng, rng.choice(KEYS), rng.uniform(0.2, 0.9)) for _ in range(16)],
        [_f32(LABEL["instrumental"])],
    ]
    for segments in bundles:
        track, embedding = _bundle(cur, segments)
        got = _scores(cur, embedding)
        _close(got, segments)
        for labels in SETS.values():
            assert math.fsum(got[key] for key, _v in labels) == pytest.approx(1.0, abs=1e-12)
        cur.execute("SELECT model_id::text, moods, vocal_score, danceability "
                    "FROM track_zero_shot WHERE track_id = %s", (track,))
        assert cur.fetchall() == [(MODEL, {key: got[key] for key, _v in SETS["moods"]},
                                   got["vocal"], got["danceable"])]
    peaked = reference(bundles[3])                      # the softmax at both of its ends
    assert peaked["instrumental"] > 0.999 and peaked["vocal"] < 0.001


def test_an_embedding_of_a_model_with_no_labels_has_no_scores(cur):
    other = str(uuid.uuid4())
    cur.execute("INSERT INTO embedding_models (id, name, dimension) VALUES (%s, %s, 512)",
                (other, f"another-clap-{other[:8]}"))
    track = _track(cur)
    embedding = _embedding(cur, track, model=other)
    _write(cur, [(embedding, 0, _f32(LABEL["vocal"]))])
    assert _scores(cur, embedding) == {}
    cur.execute("SELECT count(*) FROM track_zero_shot WHERE track_id = %s", (track,))
    assert cur.fetchone()[0] == 0


def test_a_segment_off_the_unit_sphere_fails_no_write_and_its_bundle_has_no_scores(cur):
    """exp() raises on overflow and on underflow, and a seal never says a
    vector is a unit one: 40× a label is an overflow, −50× an underflow."""
    for factor in (50.0, 40.0, -50.0, 3.0, 1.02, 0.98, 0.0):
        _, embedding = _bundle(cur, [[factor * x for x in LABEL["vocal"]]])
        assert _scores(cur, embedding) == {}, factor
    for factor in (1.005, 0.995):                           # inside the band: scored as it is
        segments = [_f32([factor * x for x in LABEL["danceable"]])]
        _, embedding = _bundle(cur, segments)
        _close(_scores(cur, embedding), segments)


def test_one_bad_segment_takes_its_bundles_scores_and_nothing_elses(cur, conn, scratch_dsn):
    rng = random.Random(10)
    good, more = [_noise(rng), _noise(rng)], [_noise(rng)]
    bad = [64.0 * x for x in LABEL["vocal"]]
    # the importer's shape: a good bundle and a bad one in one transaction
    writer = psycopg2.connect(scratch_dsn)
    with writer.cursor() as w:
        _, kept = _bundle(w, good)
        _, lost = _bundle(w, [good[0], bad])
    writer.commit()
    writer.close()
    _close(_scores(cur, kept), good)
    assert _scores(cur, lost) == {}
    # a bad segment added to a scored bundle takes its scores; removed, they are back
    _write(cur, [(kept, 7, bad)])
    assert _scores(cur, kept) == {}
    _write(cur, [(kept, 8, more[0])])
    assert _scores(cur, kept) == {}
    cur.execute("DELETE FROM embedding_segments WHERE embedding_id = %s AND segment_index = 7", (kept,))
    _close(_scores(cur, kept), good + more)


def test_labels_without_the_models_scale_fail_no_write(cur):
    rng = random.Random(11)
    segments = [_noise(rng)]
    cur.execute("UPDATE embedding_models SET logit_scale = NULL WHERE id = %s", (MODEL,))
    try:
        _, embedding = _bundle(cur, segments)
        assert _scores(cur, embedding) == {}
    finally:
        cur.execute("UPDATE embedding_models SET logit_scale = %s WHERE id = %s", (SCALE, MODEL))
    _write(cur, [(embedding, 1, segments[0])])
    _close(_scores(cur, embedding), segments * 2)


def test_the_import_gate_refuses_a_bundle_off_the_unit_sphere_or_under_a_model_id_not_its_names(
        cur, conn, scratch_dsn, monkeypatch, caplog):
    """Sealed validly by its author, each: the page's good bundle imports,
    the others are dropped whole."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from desktop import sync_client
    from desktop.p2p import birth_cert, sync_queries, witness
    from tests.p2p.sealed_records import seal_track
    from tests.p2p.test_witness import ISSUER

    for mirror in (birth_cert, witness, sync_client):
        monkeypatch.setattr(mirror, "TRUSTED_AUTHORITIES", [ISSUER])
    assert sync_client.model_uuid(DOC["model"]) == MODEL
    misnamed = str(uuid.uuid4())                            # a model row whose id is not its name's
    cur.execute("INSERT INTO embedding_models (id, name, dimension) VALUES (%s, %s, 512)",
                (misnamed, f"clap-{misnamed[:8]}"))
    author = Ed25519PrivateKey.generate()
    tracks = {"good": (MODEL, 1.0), "stretched": (MODEL, 64.0), "shrunk": (MODEL, 0.5),
              "misnamed": (misnamed, 1.0)}
    ids = {}
    for name, (model, stretch) in tracks.items():
        ids[name] = _track(cur, name)
        seal_track(cur, author, ids[name], model, recording=str(uuid.uuid4()),
                   chromaprint=f"AQAA{name}", stretch=stretch)
    payload = sync_queries.pull_segments(conn, list(ids.values()))
    assert len(payload["items"]) == 4
    for table in ("embeddings", "audio_features", "track_mbids", "analysis_sources"):
        cur.execute(f"DELETE FROM {table} WHERE track_id = ANY(%s::uuid[])", (list(ids.values()),))

    client = sync_client.SyncClient(None, scratch_dsn)
    try:
        with caplog.at_level(logging.WARNING, logger="desktop.sync_client"):
            assert client._import_items("segments", payload) == 1
    finally:
        client._close_conn()
    assert caplog.text.count("not a unit vector") == 2
    assert caplog.text.count("its model id is not its model name's") == 1
    cur.execute("""SELECT t.title, count(es.id) FROM tracks t
                   LEFT JOIN embeddings e ON e.track_id = t.id
                   LEFT JOIN embedding_segments es ON es.embedding_id = e.id
                   WHERE t.id = ANY(%s::uuid[]) GROUP BY t.title""", (list(ids.values()),))
    assert dict(cur.fetchall()) == {"good": 3, "stretched": 0, "shrunk": 0, "misnamed": 0}
    cur.execute("SELECT count(*) FROM zero_shot_scores s JOIN embeddings e ON e.id = s.embedding_id "
                "WHERE e.track_id = %s", (ids["good"],))
    assert cur.fetchone()[0] == 12
    # the gate's band is the derivation's: what one takes the other scores
    edge = sync_client.UNIT_NORM
    for norm, inside in ((edge[0] + 1e-4, True), (edge[1] - 1e-4, True),
                         (edge[0] - 1e-4, False), (edge[1] + 1e-4, False)):
        vector = [norm * x for x in LABEL["vocal"]]
        assert sync_client._unit(vector) is inside
        _, embedding = _bundle(cur, [vector])
        assert bool(_scores(cur, embedding)) is inside, norm


# ── the triggers ────────────────────────────────────────────────────────────

def test_segments_added_to_a_bundle_score_it_from_all_it_has(cur):
    rng = random.Random(1)
    first, more = [_noise(rng), _noise(rng)], [_near(rng, "vocal", 0.6), _noise(rng)]
    _, embedding = _bundle(cur, first)
    _write(cur, [(embedding, 2 + i, v) for i, v in enumerate(more)])
    _close(_scores(cur, embedding), first + more)


def test_a_segment_removed_scores_the_rest_and_the_last_one_takes_the_scores(cur):
    rng = random.Random(2)
    segments = [_near(rng, "danceable", 0.7), _noise(rng), _noise(rng)]
    _, embedding = _bundle(cur, segments)
    cur.execute("DELETE FROM embedding_segments WHERE embedding_id = %s AND segment_index = 0",
                (embedding,))
    _close(_scores(cur, embedding), segments[1:])
    cur.execute("DELETE FROM embedding_segments WHERE embedding_id = %s", (embedding,))
    assert _scores(cur, embedding) == {}


def test_one_statement_scores_every_bundle_it_touches(cur):
    rng = random.Random(3)
    a, b = [_noise(rng) for _ in range(3)], [_near(rng, "calm and relaxing", 0.5), _noise(rng)]
    ea, eb = _embedding(cur, _track(cur)), _embedding(cur, _track(cur))
    _write(cur, [(ea, i, v) for i, v in enumerate(a)] + [(eb, i, v) for i, v in enumerate(b)])
    _close(_scores(cur, ea), a)
    _close(_scores(cur, eb), b)
    # one DELETE: a's first segment, and all of b's
    cur.execute("""DELETE FROM embedding_segments
                    WHERE (embedding_id = %s AND segment_index = 0) OR embedding_id = %s""", (ea, eb))
    _close(_scores(cur, ea), a[1:])
    assert _scores(cur, eb) == {}


def test_a_replaced_bundle_has_the_new_segments_scores(cur):
    """The writers' own sequence: every segment deleted, the new grid inserted."""
    rng = random.Random(4)
    old, new = [_noise(rng) for _ in range(4)], [_near(rng, "dark and ominous", 0.6), _noise(rng)]
    _, embedding = _bundle(cur, old)
    cur.execute("DELETE FROM embedding_segments WHERE embedding_id = %s", (embedding,))
    _write(cur, [(embedding, i, v) for i, v in enumerate(new)])
    _close(_scores(cur, embedding), new)


def test_a_statement_that_touches_no_segment_changes_nothing(cur):
    rng = random.Random(5)
    segments = [_noise(rng)]
    _, embedding = _bundle(cur, segments)
    before = _scores(cur, embedding)
    cur.execute("DELETE FROM embedding_segments WHERE embedding_id = -1")
    cur.execute("INSERT INTO embedding_segments (embedding_id, segment_index, vector) "
                "SELECT 1, 1, NULL::vector WHERE false")
    assert _scores(cur, embedding) == before


def test_a_deleted_embedding_takes_its_segments_and_scores_with_it(cur):
    rng = random.Random(6)
    track, embedding = _bundle(cur, [_noise(rng), _noise(rng)])
    other_track, other = _bundle(cur, [_noise(rng)])
    cur.execute("DELETE FROM embeddings WHERE id = %s", (embedding,))
    cur.execute("DELETE FROM tracks WHERE id = %s", (other_track,))        # two cascades deep
    cur.execute("""SELECT (SELECT count(*) FROM zero_shot_scores WHERE embedding_id = ANY(%(e)s)),
                          (SELECT count(*) FROM embedding_segments WHERE embedding_id = ANY(%(e)s))""",
                {"e": [embedding, other]})
    assert cur.fetchone() == (0, 0)


def test_a_merge_that_re_points_the_embedding_carries_its_scores(cur):
    """The statement a merge re-homes the analysis with (canon.identity's
    _move_analysis): the scores hang on the embedding, not on the track."""
    rng = random.Random(7)
    segments = [_noise(rng), _near(rng, "vocal", 0.5)]
    old, embedding = _bundle(cur, segments)
    new = _track(cur)
    cur.execute("SELECT label_id, score, xmin::text FROM zero_shot_scores "
                "WHERE embedding_id = %s ORDER BY 1", (embedding,))
    before = cur.fetchall()
    cur.execute("UPDATE embeddings SET track_id = %s WHERE track_id = %s", (new, old))
    cur.execute("SELECT label_id, score, xmin::text FROM zero_shot_scores "
                "WHERE embedding_id = %s ORDER BY 1", (embedding,))
    assert cur.fetchall() == before                         # the same rows, never rewritten
    cur.execute("SELECT track_id::text, vocal_score FROM track_zero_shot WHERE track_id = ANY(%s::uuid[])",
                ([old, new],))
    assert cur.fetchall() == [(new, _scores(cur, embedding)["vocal"])]
    _close(_scores(cur, embedding), segments)


def test_sealing_and_shedding_a_seal_recompute_nothing(cur):
    rng = random.Random(8)
    _, embedding = _bundle(cur, [_noise(rng), _noise(rng)])
    cur.execute("UPDATE zero_shot_scores SET score = 0 WHERE embedding_id = %s", (embedding,))
    cur.execute("UPDATE embedding_segments SET author_pubkey = %s, signature = %s "
                "WHERE embedding_id = %s", ("ab" * 32, "cd" * 64, embedding))
    cur.execute("UPDATE embedding_segments SET author_pubkey = NULL, signature = NULL "
                "WHERE embedding_id = %s", (embedding,))
    assert set(_scores(cur, embedding).values()) == {0.0}


def test_the_analysis_pass_writes_a_bundle_in_one_statement_and_it_is_scored(cur, scratch_dsn):
    embeddings = pytest.importorskip("embeddings")
    np = pytest.importorskip("numpy")
    import database
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    rng = random.Random(9)
    first = [_noise(rng) for _ in range(12)]
    again = [_near(rng, "energetic and intense", 0.5) for _ in range(3)]
    track = _track(cur)
    engine = database.make_engine(scratch_dsn)
    inserts = []
    event.listen(engine, "before_cursor_execute",
                 lambda c, cursor, statement, *a: inserts.append(statement)
                 if "INSERT INTO embedding_segments" in statement else None)
    generator = object.__new__(embeddings.AudioEmbeddingGenerator)
    with Session(engine) as db:
        model = db.get(embeddings.EmbeddingModel, uuid.UUID(MODEL))
        for segments in (first, again):                     # an analysis, then a re-analysis
            vectors = np.array(segments, dtype=np.float32)
            assert generator._persist_analysis(db, track, model, list(range(len(segments))),
                                               vectors, vectors.mean(axis=0), None, None)
            db.commit()
    engine.dispose()
    assert len(inserts) == 2
    cur.execute("SELECT id FROM embeddings WHERE track_id = %s", (track,))
    _close(_scores(cur, cur.fetchone()[0]), again)


# ── 001 and its deltas ──────────────────────────────────────────────────────

_TABLES = ("zero_shot_labels", "zero_shot_scores")


def _objects(c) -> dict:
    """The catalogue's own text of every object the zero-shot block makes."""
    out = {}
    c.execute("""SELECT p.proname, pg_get_functiondef(p.oid) FROM pg_proc p
                  WHERE p.pronamespace = 'public'::regnamespace AND p.proname LIKE 'zero\\_shot%'
                  ORDER BY 1, 2""")
    out["functions"] = c.fetchall()
    c.execute("""SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger
                  WHERE tgrelid = 'embedding_segments'::regclass AND NOT tgisinternal ORDER BY 1""")
    out["triggers"] = c.fetchall()
    c.execute("SELECT pg_get_viewdef('track_zero_shot'::regclass, true)")
    out["view"] = c.fetchall()
    c.execute("""SELECT table_name, column_name, udt_name, is_nullable, column_default
                   FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND (table_name = ANY(%s) OR (table_name, column_name) = ('embedding_models', 'logit_scale'))
                  ORDER BY table_name, ordinal_position""", (list(_TABLES),))
    out["columns"] = c.fetchall()
    c.execute("""SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint
                  WHERE conrelid = ANY(%s::regclass[]) ORDER BY 1, 2""", (list(_TABLES),))
    out["constraints"] = c.fetchall()
    c.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = ANY(%s) ORDER BY 1",
              (list(_TABLES),))
    out["indexes"] = c.fetchall()
    c.execute("SELECT attstorage FROM pg_attribute "
              "WHERE attrelid = 'zero_shot_labels'::regclass AND attname = 'vector'")
    out["label storage"] = c.fetchall()
    c.execute("SELECT enumlabel FROM pg_enum WHERE enumtypid = 'zero_shot_set'::regtype "
              "ORDER BY enumsortorder")
    out["sets"] = c.fetchall()
    return out


def test_the_baseline_and_its_deltas_hold_the_same_objects_once(conn):
    from desktop import db_init

    name = "sautium_test_zero_shot_baseline"
    assert conftest._recreate(name, create=True)
    try:
        baseline = psycopg2.connect(dbname=name, **conftest.PG)
        baseline.autocommit = True
        with baseline.cursor() as b:
            b.execute((Path(db_init.__file__).parent / "migrations" / "001_initial.sql")
                      .read_text(encoding="utf-8"))
            alone = _objects(b)
        baseline.close()
    finally:
        conftest._recreate(name, create=False)

    delta = (Path(db_init.__file__).parent / "migrations" / "054_zero_shot_scores.sql").read_text(
        encoding="utf-8")
    with conn.cursor() as c:
        with_deltas = _objects(c)
        c.execute(delta)                                    # a second time: a no-op
        assert _objects(c) == with_deltas
    assert with_deltas == alone
    assert [name for name, _def in alone["functions"]] == ["zero_shot_derive",
                                                           "zero_shot_scores_refresh"]
    assert [name for name, _def in alone["triggers"]] == [
        "trg_embedding_segments_scores_del", "trg_embedding_segments_scores_ins",
        "trg_embedding_segments_seal_guard"]
    assert [s for (s,) in alone["sets"]] == list(DOC["sets"])
    assert alone["label storage"] == [("p",)]


def test_the_delta_lands_on_a_node_that_ran_the_earlier_files_and_holds_segments():
    """A database as the file before 054 left it — none of the zero-shot
    objects, a model row, a bundle held: the runner applies 054 once, the
    assistant's login may read what it made, and the step scores the bundle."""
    db_migrate = pytest.importorskip("db_migrate")
    from desktop import db_init

    name = "sautium_test_zero_shot_upgrade"
    if not conftest._recreate(name, create=True):
        pytest.skip("no PostgreSQL for a scratch database")
    try:
        node = psycopg2.connect(dbname=name, **conftest.PG)
        db_init.apply_migrations(node)
        node.commit()
        rng = random.Random(53)
        segments = [_noise(rng), _noise(rng)]
        with node.cursor() as c:
            c.execute("""DROP VIEW track_zero_shot;
                         DROP TRIGGER trg_embedding_segments_scores_ins ON embedding_segments;
                         DROP TRIGGER trg_embedding_segments_scores_del ON embedding_segments;
                         DROP FUNCTION zero_shot_scores_refresh();
                         DROP FUNCTION zero_shot_derive(integer[]);
                         DROP TABLE zero_shot_scores, zero_shot_labels;
                         DROP TYPE zero_shot_set;
                         ALTER TABLE embedding_models DROP COLUMN logit_scale;
                         DELETE FROM _schema_migrations WHERE filename LIKE '054%'""")
            c.execute("INSERT INTO embedding_models (id, name, dimension) VALUES (%s, %s, 512)",
                      (MODEL, DOC["model"]))
            _, held = _bundle(c, segments)
        node.commit()
        assert db_init.apply_migrations(node) == 1
        node.commit()
        assert db_init.apply_migrations(node) == 0
        node.commit()
        with node.cursor() as c:
            c.execute("SELECT bool_and(has_table_privilege(%s, r, 'SELECT')) FROM unnest(%s::text[]) r",
                      (db_init.ASSISTANT_ROLE, ["track_zero_shot", "zero_shot_scores", "zero_shot_labels"]))
            assert c.fetchone()[0]
            assert _scores(c, held) == {}
        node.commit()
        result = db_migrate._load_zero_shot_labels(node)
        assert (result["scores"], result["unscored"]) == (12, 0)
        with node.cursor() as c:
            _close(_scores(c, held), segments)
        node.close()
    finally:
        conftest._recreate(name, create=False)


# ── the readers ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def library(cur):
    """Six owned tracks: five whose segments are label vectors, so every
    score the readers gate on is far from its threshold, and one with
    features alone."""
    cur.execute("TRUNCATE tracks, albums, artists CASCADE")
    made = {"dance": ["danceable"], "sung": ["vocal"], "still": ["not_danceable"],
            "calm": ["calm and relaxing"], "mixed": ["vocal", "instrumental"], "bare": []}
    artist, album = str(uuid.uuid4()), str(uuid.uuid4())
    cur.execute("INSERT INTO artists (id, name) VALUES (%s, 'the reader')", (artist,))
    cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'small library')", (album,))
    cur.execute("INSERT INTO album_artists (album_id, artist_id, role) VALUES (%s, %s, 'primary')",
                (album, artist))
    cur.execute("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, '/lib/small') "
                "RETURNING id", (album,))
    variant = cur.fetchone()[0]
    tracks, want = {}, {}
    for n, (name, keys) in enumerate(made.items()):
        segments = [_f32(LABEL[key]) for key in keys]
        track = tracks[name] = _track(cur, name)
        cur.execute("INSERT INTO track_artists (track_id, artist_id, role) VALUES (%s, %s, 'primary')",
                    (track, artist))
        cur.execute("INSERT INTO media_files (track_id, album_variant_id, file_path, track_number, "
                    "duration_seconds) VALUES (%s, %s, %s, %s, 200)",
                    (track, variant, f"/lib/small/{name}.flac", n + 1))
        cur.execute("INSERT INTO audio_features (track_id, bpm) VALUES (%s, %s)", (track, 100 + n))
        if segments:
            embedding = _embedding(cur, track)
            _write(cur, [(embedding, i, v) for i, v in enumerate(segments)])
        want[name] = reference(segments) if segments else None
    # The fixture sits on both sides of every threshold the readers apply.
    scored = [n for n in want if want[n]]
    assert want["dance"]["danceable"] > zero_shot.DANCEABLE_MIN + 0.1
    assert all(want[n]["danceable"] < zero_shot.DANCEABLE_MIN - 0.1 for n in scored if n != "dance")
    assert want["sung"]["vocal"] > zero_shot.VOCAL_MIN + 0.1
    assert want["still"]["vocal"] < zero_shot.INSTRUMENTAL_MAX - 0.1
    assert zero_shot.INSTRUMENTAL_MAX + 0.1 < want["mixed"]["vocal"] < zero_shot.VOCAL_MIN - 0.1
    assert all(abs(score - zero_shot.MOOD_MIN) > 1e-4
               for n in scored for key, score in want[n].items() if key in dict(SETS["moods"]))
    return {"tracks": tracks, "want": want, "artist": artist, "album": album}


def _class(score) -> str:
    return ("vocal" if score >= zero_shot.VOCAL_MIN
            else "instrumental" if score <= zero_shot.INSTRUMENTAL_MAX else "mixed")


@pytest.fixture(scope="module")
def session(scratch_dsn):
    """SQLAlchemy sessions on the module's database, as the readers take them."""
    database = pytest.importorskip("database")
    from sqlalchemy.orm import sessionmaker
    engine = database.make_engine(scratch_dsn)
    yield sessionmaker(bind=engine)
    engine.dispose()


def test_search_filters_by_the_danceable_score_and_shows_the_derived_values(library, session):
    search = pytest.importorskip("search")
    want = library["want"]
    scored = [n for n in want if want[n]]
    with session() as db:
        every = {r["title"]: r for r in search.search_by_features(db, {"bpm_min": 1})["results"]}
        danceable = {r["title"] for r in search.search_by_features(db, {"danceable": True})["results"]}
        not_danceable = {r["title"] for r in
                         search.search_by_features(db, {"not_danceable": True})["results"]}
    assert set(every) == set(want)
    assert danceable == {n for n in scored if want[n]["danceable"] >= zero_shot.DANCEABLE_MIN}
    assert not_danceable == set(scored) - danceable         # a track with no scores is neither
    assert "dance" in danceable and "still" in not_danceable
    for name in scored:
        assert every[name]["danceability"] == pytest.approx(want[name]["danceable"], abs=6e-4)
        assert every[name]["danceability"] == round(every[name]["danceability"], 3)
        assert every[name]["vocal_instrumental"] == _class(want[name]["vocal"])
    assert every["bare"]["danceability"] is None and every["bare"]["vocal_instrumental"] is None
    assert {every[n]["vocal_instrumental"] for n in scored} == {"vocal", "instrumental", "mixed"}


def test_the_discovery_gates_read_the_scores_at_every_level(library, session):
    engine = pytest.importorskip("discovery_engine")
    from sqlalchemy import text
    want, tracks = library["want"], library["tracks"]
    scored = [n for n in want if want[n]]

    def found(active: dict, level: str = "track") -> set:
        sql, params = engine.build(active, {}, corpus="owned", limit=50)[level]
        with session() as db:
            return {str(r.id) for r in db.execute(text(sql), params)}

    assert found({"danceable": True}) == {
        tracks[n] for n in scored if want[n]["danceable"] >= zero_shot.DANCEABLE_MIN}
    moods = {key for key, _v in SETS["moods"]}
    split = [m for m in moods if len({want[n][m] >= zero_shot.MOOD_MIN for n in scored}) == 2]
    assert split, "the fixture has no mood present on one track and absent on another"
    for mood in sorted(moods):
        assert found({"moods": [mood]}) == {
            tracks[n] for n in scored if want[n][mood] >= zero_shot.MOOD_MIN}, mood
    assert found({"moods": split[:1], "danceable": True}) == {
        tracks[n] for n in scored
        if want[n][split[0]] >= zero_shot.MOOD_MIN and want[n]["danceable"] >= zero_shot.DANCEABLE_MIN}
    # promoted over the bridge: the album and the artist that have such a track
    assert found({"moods": ["calm and relaxing"]}, "album") == {library["album"]}
    assert found({"moods": ["calm and relaxing"]}, "artist") == {library["artist"]}
    assert found({"moods": ["no such mood"]}) == set()


def test_now_playing_and_the_assistant_show_the_derived_values(library, conn, scratch_dsn, monkeypatch):
    player = pytest.importorskip("routers.player")
    import assistant_queries
    import db_pool
    import psycopg2.pool

    pool = psycopg2.pool.ThreadedConnectionPool(1, 2, dsn=scratch_dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    try:
        want, tracks = library["want"], library["tracks"]
        for name in ("dance", "calm"):
            detail = player._zero_shot_detail(tracks[name])
            assert detail["danceability"] == pytest.approx(want[name]["danceable"], abs=6e-4)
            present = {k: want[name][k] for k, _v in SETS["moods"]
                       if want[name][k] >= zero_shot.MOOD_MIN}
            assert 0 < len(present) < 8
            assert detail["moods"] == pytest.approx(present, abs=6e-4)
            assert list(detail["moods"].values()) == sorted(detail["moods"].values(), reverse=True)
        assert player._zero_shot_detail(tracks["bare"]) == {"danceability": None, "moods": None}

        features = assistant_queries.track_features(db_pool.db_query, tracks["still"])
        assert features["vocal_instrumental"] == "instrumental"
        assert float(features["danceability"]) == pytest.approx(want["still"]["danceable"], abs=1e-6)
        assert float(features["bpm"]) == 100 + list(tracks).index("still")
        bare = assistant_queries.track_features(db_pool.db_query, tracks["bare"])
        assert bare["vocal_instrumental"] is None and bare["danceability"] is None
        assert "Type: instrumental" in assistant_queries.format_track_info(
            {"artist": "the reader", "title": "still", "album": "small library",
             "track_id": tracks["still"]}, features)
        assert assistant_queries.track_features(db_pool.db_query, str(uuid.uuid4())) is None
    finally:
        pool.closeall()


def test_the_sql_helpers_say_what_the_thresholds_say(library, cur):
    want, tracks = library["want"], library["tracks"]
    for name, track in tracks.items():
        cur.execute(f"""SELECT {zero_shot.track_gate_sql("t.id", zero_shot.DANCEABLE, ">= %s")},
                               (SELECT {zero_shot.vocal_class_sql("z.vocal_score")}
                                  FROM track_zero_shot z WHERE z.track_id = t.id)
                          FROM tracks t WHERE t.id = %s""", (zero_shot.DANCEABLE_MIN, track))
        gate, cls = cur.fetchone()
        if want[name] is None:
            assert (gate, cls) == (False, None)
        else:
            assert gate == (want[name]["danceable"] >= zero_shot.DANCEABLE_MIN)
            assert cls == _class(want[name]["vocal"])
    for score, cls in ((zero_shot.VOCAL_MIN, "vocal"), (zero_shot.INSTRUMENTAL_MAX, "instrumental"),
                       ((zero_shot.VOCAL_MIN + zero_shot.INSTRUMENTAL_MAX) / 2, "mixed")):
        cur.execute(f"SELECT {zero_shot.vocal_class_sql('%s::float8')}", (score, score, score))
        assert cur.fetchone()[0] == cls


def test_a_score_exactly_at_its_threshold_passes_its_gate(library, cur, session):
    """Present AT the threshold, not above it: the gates are >=."""
    engine = pytest.importorskip("discovery_engine")
    search = pytest.importorskip("search")
    from sqlalchemy import text
    track = library["tracks"]["still"]                      # far under both thresholds as made
    mood = "aggressive and angry"
    assert library["want"]["still"][mood] < zero_shot.MOOD_MIN

    def put(key: str, score: float) -> None:
        cur.execute("""UPDATE zero_shot_scores s SET score = %s FROM zero_shot_labels l, embeddings e
                        WHERE l.id = s.label_id AND e.id = s.embedding_id
                          AND e.track_id = %s AND l.key = %s""", (score, track, key))
        assert cur.rowcount == 1

    def found(active: dict) -> bool:
        sql, params = engine.build(active, {}, corpus="owned", limit=50)["track"]
        with session() as db:
            return track in {str(r.id) for r in db.execute(text(sql), params)}

    def searched(flt: str) -> bool:
        with session() as db:
            return "still" in {r["title"] for r in search.search_by_features(db, {flt: True})["results"]}

    kept = _scores(cur, _library_embedding(cur, track))
    try:
        put("danceable", zero_shot.DANCEABLE_MIN)
        put(mood, zero_shot.MOOD_MIN)
        assert found({"danceable": True}) and found({"moods": [mood]})
        assert searched("danceable") and not searched("not_danceable")
        put("danceable", math.nextafter(zero_shot.DANCEABLE_MIN, 0))
        put(mood, math.nextafter(zero_shot.MOOD_MIN, 0))
        assert not found({"danceable": True}) and not found({"moods": [mood]})
        assert searched("not_danceable") and not searched("danceable")
    finally:
        put("danceable", kept["danceable"])
        put(mood, kept[mood])


def _library_embedding(cur, track: str) -> int:
    cur.execute("SELECT id FROM embeddings WHERE track_id = %s", (track,))
    return cur.fetchone()[0]
