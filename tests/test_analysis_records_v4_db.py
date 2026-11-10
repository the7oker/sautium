"""The analysis records' cutover to a version per kind (2026-11-10): the
schema (001_initial.sql's track_instruments block and audio_features without
its five fields, delta 056), the data step analysis_records_v4
(backend/db_migrate.py), and the signing of what it keeps (sign_audio):

- 001 alone and 001 with its deltas hold the same objects, once;
- an old node through 056: the instruments moved, unsigned, with their row's
  source link, version and dates; the five columns, their checks, indexes and
  type gone; no seal touched; 056 again a no-op; a failure inside it rolls it
  back whole;
- the data step on an old node's rows — own sealed (the current key and a
  retired one), own unsealed, under no source, foreign, same-account under an
  imported source, flipped (features; grids split onto their imported twin,
  one failing the twin's declaration), the unsealed remainder, a legacy mean,
  rows from before the material groups; the node's keys; a node with no key
  resolving fails closed and a later start with the key runs; one whose
  records carry no seal runs keyless; a failure inside it rolls back whole;
- apply_pending runs it once, before the labels, marks it only when done,
  and the seed waits for it;
- sign() signs the kept records as features v4 and instruments v1, they
  stamp, and verify_all holds them.
Skipped without a cluster."""

import json
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import birth_authority  # noqa: E402
import database  # noqa: E402
import db_migrate  # noqa: E402
import p2p_identity  # noqa: E402
import provenance  # noqa: E402
import sign_audio  # noqa: E402
from config import settings  # noqa: E402
from desktop import db_init, sync_client  # noqa: E402
from desktop.p2p import record_sig as rs  # noqa: E402
from tests import conftest  # noqa: E402
from tests.p2p.sealed_records import pub, vector  # noqa: E402
from tests.p2p.test_witness import AUTHORITY, ISSUER  # noqa: E402

MIGRATIONS = REPO / "desktop" / "migrations"
DELTA = MIGRATIONS / "056_analysis_records_v4.sql"
MODEL = sync_client.model_uuid("clap")
OWN, RETIRED, OTHER = (Ed25519PrivateKey.generate() for _ in range(3))
FP = "AQAAcutover"
SCORES = {"piano": 0.62, "drum kit": 0.45}
OWN_KEYS = db_migrate._own_keys        # as it is, before a test states the keys in its place


def _connect(name: str, autocommit: bool = True):
    c = psycopg2.connect(dbname=name, **conftest.PG)
    c.autocommit = autocommit
    return c


def _dsn(name: str) -> str:
    pg = conftest.PG
    return f"postgresql://{pg['user']}:{pg['password']}@{pg['host']}:{pg['port']}/{name}"


# The audio_features of the release before: the five columns, their checks,
# their indexes and their type, the guard over all fourteen.
_OLD_SCHEMA = """
    DROP TABLE track_instruments;
    DROP FUNCTION seal_guard_track_instruments();
    CREATE TYPE vocal_class AS ENUM ('vocal', 'instrumental', 'mixed');
    ALTER TABLE audio_features
        ADD COLUMN instruments JSONB, ADD COLUMN moods JSONB,
        ADD COLUMN vocal_instrumental vocal_class, ADD COLUMN vocal_score DOUBLE PRECISION,
        ADD COLUMN danceability DOUBLE PRECISION,
        ADD CONSTRAINT chk_af_danceability
            CHECK (danceability IS NULL OR (danceability >= 0 AND danceability <= 1)),
        ADD CONSTRAINT chk_af_vocal_score
            CHECK (vocal_score IS NULL OR (vocal_score >= 0 AND vocal_score <= 1));
    CREATE INDEX idx_audio_features_danceability ON audio_features(danceability);
    CREATE INDEX idx_audio_features_vocal ON audio_features(vocal_instrumental);
    CREATE INDEX idx_audio_features_instruments ON audio_features USING gin (instruments);
    CREATE INDEX idx_audio_features_moods ON audio_features USING gin (moods);
    CREATE OR REPLACE FUNCTION seal_guard_audio_features() RETURNS trigger AS $$
    BEGIN
        IF (NEW.bpm IS DISTINCT FROM OLD.bpm OR NEW.key IS DISTINCT FROM OLD.key
            OR NEW.mode IS DISTINCT FROM OLD.mode
            OR NEW.key_confidence IS DISTINCT FROM OLD.key_confidence
            OR NEW.energy IS DISTINCT FROM OLD.energy OR NEW.energy_db IS DISTINCT FROM OLD.energy_db
            OR NEW.brightness IS DISTINCT FROM OLD.brightness
            OR NEW.dynamic_range_db IS DISTINCT FROM OLD.dynamic_range_db
            OR NEW.zero_crossing_rate IS DISTINCT FROM OLD.zero_crossing_rate
            OR NEW.danceability IS DISTINCT FROM OLD.danceability
            OR NEW.vocal_instrumental IS DISTINCT FROM OLD.vocal_instrumental
            OR NEW.vocal_score IS DISTINCT FROM OLD.vocal_score
            OR NEW.instruments IS DISTINCT FROM OLD.instruments
            OR NEW.moods IS DISTINCT FROM OLD.moods
            OR NEW.analysis_version IS DISTINCT FROM OLD.analysis_version)
           AND NEW.signature IS NOT DISTINCT FROM OLD.signature THEN
            NEW.author_pubkey := NULL; NEW.signature := NULL;
            NEW.batch_root := NULL; NEW.merkle_proof := NULL;
        END IF;
        RETURN NEW;
    END $$ LANGUAGE plpgsql;
    DELETE FROM _schema_migrations WHERE filename = '056_analysis_records_v4.sql';
"""


def _old_node(name: str):
    """A database as the files before 056 left it, the model's row in it."""
    if not conftest._recreate(name, create=True):
        pytest.skip("no PostgreSQL for a scratch database")
    build = _connect(name, autocommit=False)
    db_init.apply_migrations(build)
    build.commit()
    build.close()
    c = _connect(name)
    with c.cursor() as cur:
        cur.execute(_OLD_SCHEMA)
        cur.execute("INSERT INTO embedding_models (id, name, dimension) VALUES (%s, 'clap', 512)",
                    (MODEL,))
    return c


def _apply_delta(name: str) -> int:
    runner = _connect(name, autocommit=False)
    try:
        return db_init.apply_migrations(runner)
    finally:
        runner.close()


class _On:
    """The backend's sessions, config.settings and db_pool on one database,
    the node's keys stated."""

    def __init__(self, name: str, keys):
        self.mp = pytest.MonkeyPatch()
        self.engine = database.make_engine(_dsn(name))
        self.mp.setattr(database, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=self.engine))
        url = urlparse(_dsn(name))
        for key, value in dict(postgres_host=url.hostname, postgres_port=url.port,
                               postgres_user=url.username, postgres_password=url.password,
                               postgres_db=url.path.lstrip("/")).items():
            self.mp.setattr(settings, key, value)
        self.keys(keys)

    def keys(self, keys):
        self.mp.setattr(db_migrate, "_own_keys", lambda: frozenset(pub(k) for k in keys))

    def close(self):
        self.mp.undo()
        self.engine.dispose()


# -- an old node's rows --------------------------------------------------------

def _track(cur) -> str:
    tid = str(uuid.uuid4())
    cur.execute("INSERT INTO tracks (id, title) VALUES (%s, 't')", (tid,))
    return tid


def _source(cur, track, *, imported=False, fp=FP, duration=240) -> int:
    cur.execute("""INSERT INTO analysis_sources (track_id, chromaprint, duration_seconds,
                                                 grid_version, imported, is_lossless)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (track, fp, duration, rs.GRID_VERSION, imported, None if imported else True))
    return cur.fetchone()[0]


def _seal(author) -> tuple:
    """A seal's columns, as a v3 author left them (the step decides by the
    seal's author, never by its bytes)."""
    return (pub(author), "5e" * 64, None, None) if author else (None, None, None, None)


def _old_features(cur, track, source, *, author=None, instruments=SCORES, version=2) -> None:
    cur.execute("""INSERT INTO audio_features
                       (track_id, bpm, energy, instruments, moods, vocal_instrumental, vocal_score,
                        danceability, analysis_source_id, analysis_version, created_at,
                        updated_at, author_pubkey, signature, batch_root, merkle_proof)
                   VALUES (%s, 120.5, 0.25, %s, %s, 'mixed', 0.5, 0.6, %s, %s,
                           '2026-07-03T10:00:00Z', '2026-07-04T10:00:00Z', %s, %s, %s, %s)""",
                (track, psycopg2.extras.Json(instruments) if instruments is not None else None,
                 psycopg2.extras.Json({"calm and relaxing": 0.4}), source, version,
                 *_seal(author)))


def _bundle(cur, track, source, *, author=None, declared=240, segments=2) -> int:
    """A grid under `source`, sealed by `author` over FP and the `declared`
    duration (its source's, unless a flip rewrote that), or unsealed."""
    cur.execute("""INSERT INTO embeddings (track_id, model_id, vector, analysis_source_id,
                                           analysis_version)
                   VALUES (%s, %s, %s::vector, %s, 2) RETURNING id""",
                (track, MODEL, str(vector(99)), source))
    eid = cur.fetchone()[0]
    for i in range(segments):
        values = vector(i)
        seal = (None, None)
        if author is not None:
            payload = rs.segment_payload(pub(author), track, FP, declared, MODEL, i,
                                         rs.vector_hash(rs.vector_to_bytes(values)))
            seal = (pub(author), rs.sign(payload, author))
        cur.execute("""INSERT INTO embedding_segments (embedding_id, segment_index, vector,
                                                       author_pubkey, signature)
                       VALUES (%s, %s, %s::vector, %s, %s)""", (eid, i, str(values), *seal))
    return eid


def _elected_file(cur, track) -> None:
    album = str(uuid.uuid4())
    cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'LP')", (album,))
    cur.execute("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, '/m') "
                "RETURNING id", (album,))
    cur.execute("""INSERT INTO media_files (track_id, album_variant_id, file_path, bit_depth,
                                            sample_rate, is_lossless, duration_seconds,
                                            is_analysis_source)
                   VALUES (%s, %s, %s, 16, 44100, true, 240, true)""",
                (track, cur.fetchone()[0], f"/m/{track}.flac"))


def _holds(cur, track) -> dict:
    cur.execute("""SELECT (SELECT count(*) FROM audio_features WHERE track_id = %(t)s),
                          (SELECT count(*) FROM track_instruments WHERE track_id = %(t)s),
                          (SELECT count(*) FROM embeddings WHERE track_id = %(t)s)""", {"t": track})
    features, instruments, embeddings = cur.fetchone()
    return {"features": features, "instruments": instruments, "embeddings": embeddings}


@pytest.fixture(scope="module")
def cutover():
    """An old node's every kind of row, through 056 and the data step with
    this node's current key and a retired one."""
    name = "sautium_test_records_v4_old"
    c = _old_node(name)
    t = {}
    with c.cursor() as cur:
        t["own"] = _track(cur)                                   # sealed by the key that signs
        _old_features(cur, t["own"], _source(cur, t["own"]), author=OWN)
        t["retired"] = _track(cur)                               # sealed by a key it retired
        _old_features(cur, t["retired"], _source(cur, t["retired"]), author=RETIRED)
        t["unsealed"] = _track(cur)
        _old_features(cur, t["unsealed"], _source(cur, t["unsealed"]))
        t["sourceless"] = _track(cur)
        _old_features(cur, t["sourceless"], None)
        t["sourceless_other"] = _track(cur)
        _old_features(cur, t["sourceless_other"], None, author=OTHER)
        t["no_instruments"] = _track(cur)
        _old_features(cur, t["no_instruments"], _source(cur, t["no_instruments"]),
                      instruments=None, version=1)
        t["foreign"] = _track(cur)
        _old_features(cur, t["foreign"], _source(cur, t["foreign"], imported=True), author=OTHER)
        t["same_account"] = _track(cur)                          # this node's key, imported
        _old_features(cur, t["same_account"], _source(cur, t["same_account"], imported=True),
                      author=OWN)
        t["flipped"] = _track(cur)                               # another's, under its own row
        flipped = _source(cur, t["flipped"])
        _old_features(cur, t["flipped"], flipped, author=OTHER)
        t["flipped_grid"] = _bundle(cur, t["flipped"], flipped, author=OTHER)
        t["rewritten"] = _track(cur)                             # the flip wrote 240 over 239
        _bundle(cur, t["rewritten"], _source(cur, t["rewritten"]), author=OTHER, declared=239)
        t["twinned"] = _track(cur)                               # its twin already here
        t["twin"] = _source(cur, t["twinned"], imported=True)
        _bundle(cur, t["twinned"], _source(cur, t["twinned"]), author=OTHER)
        t["own_grid"] = _track(cur)                              # this node's own: untouched
        _bundle(cur, t["own_grid"], _source(cur, t["own_grid"]), author=OWN)
        t["remainder"] = _track(cur)                             # unsealed, imported, a file here
        _bundle(cur, t["remainder"], _source(cur, t["remainder"], imported=True))
        _elected_file(cur, t["remainder"])
        t["remainder_phantom"] = _track(cur)                     # the same, no file
        _bundle(cur, t["remainder_phantom"], _source(cur, t["remainder_phantom"], imported=True))
        t["legacy"] = _track(cur)                                # a first-hand mean, no segment
        _bundle(cur, t["legacy"], _source(cur, t["legacy"]), segments=0)
        t["groups"] = _track(cur)                                # two rows before the groups
        t["group_rows"] = (_source(cur, t["groups"], duration=240),
                           _source(cur, t["groups"], duration=241))
        cur.execute("SELECT count(*) FROM audio_features WHERE signature IS NOT NULL")
        sealed_before = cur.fetchone()[0]
    assert _apply_delta(name) == 1
    on = _On(name, (OWN, RETIRED))
    try:
        with c.cursor() as cur:
            cur.execute("SELECT count(*) FROM audio_features WHERE signature IS NOT NULL")
            assert cur.fetchone()[0] == sealed_before            # the delta touched no seal
        result = db_migrate._reseal_analysis_records()
        yield {"conn": c, "tracks": t, "result": result, "name": name, "on": on}
    finally:
        on.close()
        c.close()
        conftest._recreate(name, create=False)


# -- 001 and its deltas -----------------------------------------------------------

def _objects(c) -> dict:
    """The catalogue's own text of every object the records' blocks make."""
    out = {}
    with c.cursor() as cur:
        cur.execute("""SELECT table_name, column_name, data_type, is_nullable, column_default
                         FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name IN ('audio_features', 'track_instruments')
                        ORDER BY table_name, column_name""")
        out["columns"] = cur.fetchall()
        cur.execute("""SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid)
                         FROM pg_constraint
                        WHERE conrelid IN ('audio_features'::regclass, 'track_instruments'::regclass)
                        ORDER BY 1, 2""")
        out["constraints"] = cur.fetchall()
        cur.execute("""SELECT indexname, indexdef FROM pg_indexes
                        WHERE tablename IN ('audio_features', 'track_instruments') ORDER BY 1""")
        out["indexes"] = cur.fetchall()
        cur.execute("""SELECT tgrelid::regclass::text, tgname, pg_get_triggerdef(oid) FROM pg_trigger
                        WHERE tgrelid IN ('audio_features'::regclass, 'track_instruments'::regclass)
                          AND NOT tgisinternal ORDER BY 1, 2""")
        out["triggers"] = cur.fetchall()
        cur.execute("""SELECT proname, pg_get_functiondef(oid) FROM pg_proc
                        WHERE proname IN ('seal_guard_audio_features', 'seal_guard_track_instruments')
                        ORDER BY 1""")
        out["guards"] = cur.fetchall()
        cur.execute("SELECT typname FROM pg_type WHERE typname = 'vocal_class'")
        out["vocal_class"] = cur.fetchall()
    return out


def test_the_baseline_and_its_deltas_hold_the_same_records_once(scratch_dsn):
    name = "sautium_test_records_v4_baseline"
    assert conftest._recreate(name, create=True)
    try:
        baseline = _connect(name)
        with baseline.cursor() as cur:
            cur.execute((MIGRATIONS / "001_initial.sql").read_text(encoding="utf-8"))
        alone = _objects(baseline)
        baseline.close()
    finally:
        conftest._recreate(name, create=False)
    c = psycopg2.connect(scratch_dsn)
    c.autocommit = True
    try:
        with_deltas = _objects(c)
        with c.cursor() as cur:
            cur.execute(DELTA.read_text(encoding="utf-8"))         # a second time: a no-op
        assert _objects(c) == with_deltas
    finally:
        c.close()
    assert with_deltas == alone
    assert alone["vocal_class"] == []
    assert {col for table, col, *_ in alone["columns"] if table == "audio_features"} >= set(
        rs.FEATURE_ORDER)
    assert not {col for table, col, *_ in alone["columns"] if table == "audio_features"} & {
        "instruments", "moods", "vocal_instrumental", "vocal_score", "danceability"}
    assert [name for name, _def in alone["guards"]] == ["seal_guard_audio_features",
                                                        "seal_guard_track_instruments"]
    assert "idx_track_instruments_unstamped" in {i for i, _d in alone["indexes"]}


def test_the_delta_moves_the_instruments_unsigned_and_drops_the_five_columns():
    name = "sautium_test_records_v4_delta"
    c = _old_node(name)
    try:
        with c.cursor() as cur:
            track, bare = _track(cur), _track(cur)
            source = _source(cur, track)
            _old_features(cur, track, source, author=OWN)
            _old_features(cur, bare, _source(cur, bare), instruments=None, version=1)
        assert _apply_delta(name) == 1
        assert _apply_delta(name) == 0
        with c.cursor() as cur:
            cur.execute("""SELECT instruments, analysis_source_id, analysis_version,
                                  created_at = '2026-07-03T10:00:00Z',
                                  updated_at = '2026-07-04T10:00:00Z', author_pubkey, signature
                             FROM track_instruments""")
            assert cur.fetchall() == [(SCORES, source, 2, True, True, None, None)]
            cur.execute("SELECT author_pubkey, signature FROM audio_features WHERE track_id = %s",
                        (track,))
            assert cur.fetchone() == (pub(OWN), "5e" * 64)        # untouched
            cur.execute("""SELECT column_name FROM information_schema.columns
                            WHERE table_name = 'audio_features'
                              AND column_name IN ('instruments', 'moods', 'vocal_instrumental',
                                                  'vocal_score', 'danceability')""")
            assert cur.fetchall() == []
            # the guard, restated, sheds a changed value's seal and nothing else
            cur.execute("UPDATE audio_features SET energy_db = -14 WHERE track_id = %s", (track,))
            cur.execute("SELECT signature FROM audio_features WHERE track_id = %s", (track,))
            assert cur.fetchone() == (None,)
            cur.execute("""UPDATE track_instruments SET author_pubkey = %s, signature = %s
                            WHERE track_id = %s""", (pub(OWN), "5e" * 64, track))
            cur.execute("UPDATE track_instruments SET analysis_source_id = NULL WHERE track_id = %s",
                        (track,))
            cur.execute("SELECT signature FROM track_instruments WHERE track_id = %s", (track,))
            assert cur.fetchone() == ("5e" * 64,)                 # a link is no payload
            cur.execute("""UPDATE track_instruments SET instruments = '{"piano": 0.6}'
                            WHERE track_id = %s""", (track,))
            cur.execute("SELECT signature FROM track_instruments WHERE track_id = %s", (track,))
            assert cur.fetchone() == (None,)
    finally:
        c.close()
        conftest._recreate(name, create=False)


def test_a_failure_inside_the_delta_rolls_it_back_whole():
    """A kill in the middle leaves the node as the file before it: nothing
    of the delta stands, and the next start applies it whole."""
    name = "sautium_test_records_v4_killed"
    c = _old_node(name)
    try:
        with c.cursor() as cur:
            track = _track(cur)
            _old_features(cur, track, _source(cur, track))
            cur.execute("CREATE VIEW leans_on_moods AS SELECT moods FROM audio_features")
        with pytest.raises(psycopg2.Error):
            _apply_delta(name)
        with c.cursor() as cur:
            cur.execute("SELECT to_regclass('track_instruments'), "
                        "(SELECT count(*) FROM _schema_migrations WHERE filename LIKE '056%')")
            assert cur.fetchone() == (None, 0)
            cur.execute("SELECT instruments FROM audio_features")
            assert cur.fetchall() == [(SCORES,)]
            cur.execute("DROP VIEW leans_on_moods")
        assert _apply_delta(name) == 1
        with c.cursor() as cur:
            cur.execute("SELECT instruments FROM track_instruments")
            assert cur.fetchall() == [(SCORES,)]
    finally:
        c.close()
        conftest._recreate(name, create=False)


# -- the data step -----------------------------------------------------------------

def test_the_step_counts_what_it_did(cutover):
    r = cutover["result"]
    assert r["done"] and r["keys"] == 2
    assert (r["features_deleted"], r["instruments_deleted"]) == (4, 4)
    assert r["features_kept"] == 5
    assert r["feature_seals_shed"] == 2
    assert (r["bundles_split"], r["bundles_failing_the_twin"]) == (2, 1)
    assert r["unsealed_under_imported_deleted"] == 1
    assert r["own_embeddings_without_segments"] == 1
    assert r["sources_joined"] >= 1


def test_own_records_stay_their_seals_shed(cutover):
    c, t = cutover["conn"], cutover["tracks"]
    with c.cursor() as cur:
        for kind in ("own", "retired", "unsealed", "sourceless"):
            assert _holds(cur, t[kind])["features"] == 1, kind
        cur.execute("SELECT count(*) FROM audio_features WHERE signature IS NOT NULL")
        assert cur.fetchone() == (0,)                             # no v3 seal survives
        cur.execute("""SELECT a.track_id::text, i.instruments, i.analysis_source_id = a.analysis_source_id
                         FROM audio_features a JOIN track_instruments i ON i.track_id = a.track_id
                        WHERE a.track_id = ANY(%s::uuid[])""",
                    ([t["own"], t["retired"], t["unsealed"]],))
        assert sorted(cur.fetchall()) == sorted((t[k], SCORES, True)
                                                for k in ("own", "retired", "unsealed"))
        assert _holds(cur, t["no_instruments"]) == {"features": 1, "instruments": 0,
                                                    "embeddings": 0}


def test_another_authors_records_go_whatever_their_source_says(cutover):
    """Foreign under an imported source; this node's key under an imported
    source (a node of the same account shares it, and sign() never re-signs
    there); another key's under this node's own row (a flip) or none."""
    c, t = cutover["conn"], cutover["tracks"]
    with c.cursor() as cur:
        for kind in ("foreign", "same_account", "flipped", "sourceless_other"):
            holds = _holds(cur, t[kind])
            assert (holds["features"], holds["instruments"]) == (0, 0), kind


def test_a_flipped_grid_moves_to_its_imported_twin_in_the_rows_group(cutover):
    c, t = cutover["conn"], cutover["tracks"]
    with c.cursor() as cur:
        cur.execute("""SELECT s.imported, s.chromaprint, s.duration_seconds, s.copy_rank,
                              s.material_of = (SELECT COALESCE(o.material_of, o.id)
                                                 FROM analysis_sources o
                                                WHERE o.track_id = s.track_id AND NOT o.imported),
                              (SELECT count(*) FROM embedding_segments WHERE embedding_id = e.id)
                         FROM embeddings e JOIN analysis_sources s ON s.id = e.analysis_source_id
                        WHERE e.id = %s""", (t["flipped_grid"],))
        assert cur.fetchone() == (True, FP, 240, -1000, True, 2)
        cur.execute("""SELECT e.analysis_source_id FROM embeddings e WHERE e.track_id = %s""",
                    (t["twinned"],))
        assert cur.fetchone() == (t["twin"],)                     # the twin already here
        assert _holds(cur, t["rewritten"])["embeddings"] == 0     # its seals bind another duration
        cur.execute("""SELECT s.imported FROM embeddings e
                         JOIN analysis_sources s ON s.id = e.analysis_source_id
                        WHERE e.track_id = %s""", (t["own_grid"],))
        assert cur.fetchone() == (False,)                         # this node's own stays


def test_the_unsealed_remainder_goes_where_a_file_can_derive_it_again(cutover):
    c, t = cutover["conn"], cutover["tracks"]
    with c.cursor() as cur:
        assert _holds(cur, t["remainder"])["embeddings"] == 0
        assert _holds(cur, t["remainder_phantom"])["embeddings"] == 1
        assert _holds(cur, t["legacy"])["embeddings"] == 1        # counted, not touched


def test_rows_from_before_the_groups_are_joined(cutover):
    c, t = cutover["conn"], cutover["tracks"]
    first, second = t["group_rows"]
    with c.cursor() as cur:
        cur.execute("SELECT id, material_of FROM analysis_sources WHERE track_id = %s ORDER BY id",
                    (t["groups"],))
        assert cur.fetchall() == [(first, None), (second, first)]


def test_a_second_run_finds_nothing_more_to_do(cutover):
    again = db_migrate._reseal_analysis_records()
    assert again["done"]
    assert {k: again[k] for k in ("features_deleted", "instruments_deleted", "feature_seals_shed",
                                  "bundles_split", "bundles_failing_the_twin", "sources_joined",
                                  "unsealed_under_imported_deleted")} == dict.fromkeys(
        ("features_deleted", "instruments_deleted", "feature_seals_shed", "bundles_split",
         "bundles_failing_the_twin", "sources_joined", "unsealed_under_imported_deleted"), 0)


def test_sign_signs_the_kept_records_as_features_v4_and_instruments_v1():
    """The notary's startup wake after the step: every kept record under
    this node's own row is signed by the current key in its kind's format —
    one a retired key sealed included —, stamped, and holds under
    verify_all."""
    name = "sautium_test_records_v4_sign"
    c = _old_node(name)
    on = _On(name, (OWN, RETIRED))
    try:
        with c.cursor() as cur:
            own, retired = _track(cur), _track(cur)
            _old_features(cur, own, _source(cur, own), author=OWN)
            _old_features(cur, retired, _source(cur, retired), author=RETIRED)
            _bundle(cur, own, _source(cur, own, fp=FP + "b"))
        _apply_delta(name)
        assert db_migrate._reseal_analysis_records()["feature_seals_shed"] == 2
        on.mp.setattr(p2p_identity, "_signing_key_cache", OWN)
        on.mp.setattr(birth_authority, "TRUSTED_AUTHORITIES", [ISSUER])

        def stamp(root, key, author):
            date, ip_hash = "2026-11-10T20:00:00Z", str(uuid.uuid4())
            return {"root": root, "date": date, "ip_hash": ip_hash, "authority": ISSUER, "v": 2,
                    "sig": AUTHORITY.sign(rs.timestamp_payload(root, date, ip_hash)).hex(),
                    "not_before": None}
        on.mp.setattr(sign_audio, "_timestamp_root", stamp)
        stage = sign_audio.connect()
        try:
            assert sign_audio.sign(stage, full=True) == 2 + 2 + 2   # features, instruments, segments
            assert sign_audio.stamp(stage).stamped == 6
        finally:
            stage.close()
        with c.cursor() as cur:
            cur.execute("""SELECT a.author_pubkey, a.signature, s.chromaprint, s.duration_seconds,
                                  a.analysis_version, a.bpm, a.key, a.mode, a.key_confidence,
                                  a.energy, a.energy_db, a.brightness, a.dynamic_range_db,
                                  a.zero_crossing_rate
                             FROM audio_features a
                             JOIN analysis_sources s ON s.id = a.analysis_source_id
                            WHERE a.track_id = %s""", (own,))
            author, sig, fp, duration, version, *values = cur.fetchone()
            blob = rs.canonical_features_blob(dict(zip(rs.FEATURE_ORDER, values)))
            assert author == pub(OWN)
            assert rs.verify(rs.features_payload(author, own, fp, duration, version,
                                                 rs.blake2b_hex(blob)), sig, author)
            cur.execute("""SELECT author_pubkey, signature, instruments, analysis_version
                             FROM track_instruments WHERE track_id = %s""", (retired,))
            author, sig, instruments, version = cur.fetchone()
            assert (author, instruments, version) == (pub(OWN), SCORES, 2)   # the current key
            assert rs.verify(rs.instruments_payload(
                author, retired, FP, 240, version,
                rs.blake2b_hex(rs.canonical_instruments_blob(instruments))), sig, author)
            cur.execute("SELECT count(*) FROM track_instruments WHERE batch_root IS NULL")
            assert cur.fetchone() == (0,)
        assert sign_audio.verify_all()
    finally:
        on.close()
        c.close()
        conftest._recreate(name, create=False)


# -- the node's keys, failing closed -------------------------------------------------

def test_the_nodes_keys_are_the_signing_one_and_those_it_retired(tmp_path, monkeypatch):
    (tmp_path / "node_info.json").write_text(json.dumps(
        {"previous": [{"public_key_hex": pub(RETIRED).upper(), "dir": "1"}]}))
    monkeypatch.setattr(settings, "p2p_identity_dir", str(tmp_path))
    monkeypatch.setattr(p2p_identity, "_signing_key_cache", OWN)
    assert OWN_KEYS() == frozenset({pub(OWN), pub(RETIRED)})
    monkeypatch.setattr(p2p_identity, "_signing_key_cache", None)
    monkeypatch.setattr(settings, "p2p_identity_dir", None)
    monkeypatch.setattr(settings, "p2p_username", "")
    assert OWN_KEYS() == frozenset()


def test_no_key_resolving_beside_seals_of_its_own_fails_closed_until_one_does():
    name = "sautium_test_records_v4_keyless"
    c = _old_node(name)
    on = _On(name, ())
    try:
        with c.cursor() as cur:
            own, foreign = _track(cur), _track(cur)
            _old_features(cur, own, _source(cur, own), author=OWN)
            _old_features(cur, foreign, _source(cur, foreign, imported=True), author=OTHER)
        _apply_delta(name)
        assert db_migrate._reseal_analysis_records() == {"done": False, "keys": 0}
        with c.cursor() as cur:
            assert _holds(cur, own)["features"] == _holds(cur, foreign)["features"] == 1
            cur.execute("SELECT count(*) FROM audio_features WHERE signature IS NOT NULL")
            assert cur.fetchone() == (2,)
        on.keys((OWN,))                                           # a later start, the key found
        result = db_migrate._reseal_analysis_records()
        assert result["done"] and result["features_deleted"] == 1
        with c.cursor() as cur:
            assert _holds(cur, own)["features"] == 1 and _holds(cur, foreign)["features"] == 0
    finally:
        on.close()
        c.close()
        conftest._recreate(name, create=False)


def test_a_node_that_never_had_a_key_runs_keyless():
    name = "sautium_test_records_v4_never_keyed"
    c = _old_node(name)
    on = _On(name, ())
    try:
        with c.cursor() as cur:
            own, foreign = _track(cur), _track(cur)
            _old_features(cur, own, _source(cur, own))
            _old_features(cur, foreign, _source(cur, foreign, imported=True), author=OTHER)
        _apply_delta(name)
        result = db_migrate._reseal_analysis_records()
        assert result["done"] and (result["keys"], result["features_deleted"]) == (0, 1)
        with c.cursor() as cur:
            assert _holds(cur, own)["features"] == 1 and _holds(cur, foreign)["features"] == 0
    finally:
        on.close()
        c.close()
        conftest._recreate(name, create=False)


def test_a_failure_inside_the_step_rolls_it_back_whole(monkeypatch):
    name = "sautium_test_records_v4_step_killed"
    c = _old_node(name)
    on = _On(name, (OWN,))
    try:
        with c.cursor() as cur:
            own, foreign = _track(cur), _track(cur)
            _old_features(cur, own, _source(cur, own), author=OWN)
            _old_features(cur, foreign, _source(cur, foreign, imported=True), author=OTHER)
        _apply_delta(name)

        def killed(db, track_ids=None):
            raise RuntimeError("the node stopped")
        monkeypatch.setattr(provenance, "join_existing_rows", killed)
        with pytest.raises(RuntimeError):
            db_migrate._reseal_analysis_records()
        with c.cursor() as cur:
            assert _holds(cur, foreign)["features"] == 1
            cur.execute("SELECT count(*) FROM audio_features WHERE signature IS NOT NULL")
            assert cur.fetchone() == (2,)
        monkeypatch.undo()
        assert db_migrate._reseal_analysis_records()["done"]
        with c.cursor() as cur:
            assert _holds(cur, foreign)["features"] == 0
    finally:
        on.close()
        c.close()
        conftest._recreate(name, create=False)


# -- the start --------------------------------------------------------------------

def test_apply_pending_runs_the_step_once_before_the_labels_and_the_seed_waits_for_it(monkeypatch):
    """apply_pending with every other step taken as done: the records step
    runs before the labels, its marker only when it is done, and the seed
    import only on a node past it."""
    import seed_import
    name = "sautium_test_records_v4_start"
    c = _old_node(name)
    on = _On(name, ())
    try:
        with c.cursor() as cur:
            own = _track(cur)
            _old_features(cur, own, _source(cur, own), author=OWN)
        order = []
        marked = db_migrate._marked
        monkeypatch.setattr(db_migrate, "_marked", lambda conn, step: (
            marked(conn, step) if step in ("analysis_records_v4", "zero_shot_labels_v1")
            or step.startswith("seed_v") else True))
        monkeypatch.setattr(db_migrate, "_load_zero_shot_labels",
                            lambda conn: order.append("labels") or {"loaded": False})
        real = db_migrate._reseal_analysis_records
        monkeypatch.setattr(db_migrate, "_reseal_analysis_records",
                            lambda: order.append("records") or real())
        monkeypatch.setattr(seed_import, "bundle_info", lambda: {"version": 0})
        monkeypatch.setattr(seed_import, "apply_seed",
                            lambda *a: order.append("seed") or {"complete": False})
        out = db_migrate.apply_pending()                          # no key: fails closed
        assert out["analysis_records_v4"] == {"done": False, "keys": 0}
        assert order == ["records", "labels"]                     # the seed waited
        on.keys((OWN,))
        order.clear()
        assert db_migrate.apply_pending()["analysis_records_v4"]["done"]
        assert order == ["records", "labels", "seed"]
        order.clear()
        assert "analysis_records_v4" not in db_migrate.apply_pending()
        assert order == ["labels", "seed"]
    finally:
        on.close()
        c.close()
        conftest._recreate(name, create=False)
