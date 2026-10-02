"""What a DSP setting costs on an HQPlayer's host (backend/playback/hqp_load.py)
and the schema behind it (migrations 033 and 034).

The sampler's gate is pure. The rollup, the coverage the benchmark skips by,
the headroom classes and the retention run on a real PostgreSQL — a
throwaway database built from the migrations, the pool pointed at it. The
migrations are checked twice over: they re-apply on the schema 001 builds,
and on a database that lacks what they add they build exactly what 001
declares. Skipped where there is no cluster.
"""

import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")

from hqplayer_client import PlaybackState, TrackStatus  # noqa: E402
from playback import hqp_load  # noqa: E402

PG = dict(host=os.environ.get("SAUTIUM_TEST_PGHOST", "postgres"),
          port=int(os.environ.get("SAUTIUM_TEST_PGPORT", "5432")),
          user=os.environ.get("SAUTIUM_TEST_PGUSER", "musicai"),
          password=os.environ.get("SAUTIUM_TEST_PGPASSWORD", "supervisor"))
DBNAME = "sautium_hqp_load_test"
MIGRATIONS = [Path(__file__).resolve().parent.parent / "desktop" / "migrations" / name
              for name in ("033_hqp_dsp_load.sql", "034_hqp_benchmark_points.sql")]


def _status(**kw) -> TrackStatus:
    base = dict(state=PlaybackState.PLAYING, track_index=1, track_id="", position=30.0,
                length=300.0, volume=-3.0, process_speed=2.5, active_mode="SDM (DSD)",
                active_filter="poly-sinc-gauss-xla", active_shaper="ASDM7ECv3",
                active_rate=11289600, src_rate=44100, src_bits=16, src_channels=2,
                src_sdm=False, input_fill=-1.0, output_fill=0.99)
    base.update(kw)
    return TrackStatus(**base)


# -- the sampler's gate (pure) ------------------------------------------------------

def test_sampler_waits_out_the_initialisation_then_samples_a_minute_apart():
    s = hqp_load.Sampler()
    st = _status()
    # the setting first seen now: HQPlayer's average still holds its start
    assert not s.due(st, ours=True, engine="6.2.3", now=100.0)
    assert not s.due(st, ours=True, engine="6.2.3", now=114.0)
    assert s.due(st, ours=True, engine="6.2.3", now=115.0)
    s.taken(115.0)
    assert not s.due(st, ours=True, engine="6.2.3", now=150.0)
    assert s.due(st, ours=True, engine="6.2.3", now=175.0)
    s.taken(175.0)
    # another modulator: a new key, and 15 s of steady state again — then at once
    other = _status(active_shaper="ASDM7EC-super")
    assert not s.due(other, ours=True, engine="6.2.3", now=180.0)
    assert s.due(other, ours=True, engine="6.2.3", now=195.0)


def test_sampler_wants_our_slot_playing_well_into_the_track():
    s = hqp_load.Sampler()
    assert not s.due(_status(), ours=False, engine="6.2.3", now=0.0)                 # another controller
    assert not s.due(_status(state=PlaybackState.PAUSED), ours=True, engine="6.2.3", now=1.0)
    s2 = hqp_load.Sampler()
    s2.due(_status(position=2.0), ours=True, engine="6.2.3", now=0.0)
    assert not s2.due(_status(position=12.0), ours=True, engine="6.2.3", now=20.0)  # 12 s into the track
    assert s2.due(_status(position=16.0), ours=True, engine="6.2.3", now=24.0)
    # a pause restarts the steady clock
    s2.due(_status(state=PlaybackState.PAUSED), ours=True, engine="6.2.3", now=25.0)
    assert not s2.due(_status(position=40.0), ours=True, engine="6.2.3", now=26.0)


def test_sampler_needs_the_speed_and_the_source():
    s = hqp_load.Sampler()
    for st in (_status(process_speed=None), _status(process_speed=0.0),
               _status(src_rate=None, src_channels=None), _status(active_filter="")):
        s.due(st, ours=True, engine="6.2.3", now=0.0)
        assert not s.due(st, ours=True, engine="6.2.3", now=30.0)


def test_sampler_waits_out_a_matrix_or_convolution_change():
    s = hqp_load.Sampler()
    st = _status()
    state = {"matrix_profile": "", "convolution": False}
    s.due(st, ours=True, engine="6.2.3", now=0.0)
    assert s.due(st, ours=True, engine="6.2.3", now=20.0)
    assert s.steady(state, 20.0)
    s.taken(20.0)
    # a minute on, the matrix profile is another one: the setting changed
    # under the same <Status/> — the sample waits out its start like any change
    assert s.due(st, ours=True, engine="6.2.3", now=80.0)
    assert not s.steady({**state, "matrix_profile": "Room"}, 80.0)
    assert not s.due(st, ours=True, engine="6.2.3", now=90.0)
    assert s.due(st, ours=True, engine="6.2.3", now=95.0)
    assert s.steady({**state, "matrix_profile": "Room"}, 95.0)


def test_sampler_skips_a_dsd_source_on_5_17_0():
    dsd = _status(src_rate=2822400, src_bits=1, src_sdm=True)
    s = hqp_load.Sampler()
    s.due(dsd, ours=True, engine="5.17.0", now=0.0)
    assert not s.due(dsd, ours=True, engine="5.17.0", now=30.0)
    s = hqp_load.Sampler()
    s.due(dsd, ours=True, engine="5.18.0", now=0.0)
    assert s.due(dsd, ours=True, engine="5.18.0", now=30.0)


# -- the database -----------------------------------------------------------------

def _make_db(name):
    try:
        admin = psycopg2.connect(dbname="postgres", **PG)
    except psycopg2.OperationalError as e:
        pytest.skip(f"no PostgreSQL for the DSP load test: {e}")
    admin.autocommit = True
    from desktop import db_init, node_backup as nb
    nb._drop_database(admin, name)
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {name}")
    conn = psycopg2.connect(dbname=name, **PG)
    db_init.apply_migrations(conn)
    conn.commit()
    conn.close()
    return admin, nb


@pytest.fixture(scope="module")
def dsn():
    admin, nb = _make_db(DBNAME)
    yield f"postgresql://{PG['user']}:{PG['password']}@{PG['host']}:{PG['port']}/{DBNAME}"
    nb._drop_database(admin, DBNAME)
    admin.close()


class _Db:
    """The test's own connection and the endpoint every sample belongs to."""

    def __init__(self, conn, endpoint_id):
        self.conn, self.endpoint_id = conn, endpoint_id

    def cursor(self):
        return self.conn.cursor()


@pytest.fixture
def db(dsn, monkeypatch):
    import db_pool
    pool = psycopg2.pool.ThreadedConnectionPool(1, 4, dsn=dsn, options="-c timezone=UTC")
    monkeypatch.setattr(db_pool, "_pool", pool)
    monkeypatch.setattr(hqp_load, "_pruned_at", None)
    conn = psycopg2.connect(dsn, options="-c timezone=UTC")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("TRUNCATE hqp_endpoints CASCADE")
        cur.execute("""INSERT INTO hqp_endpoints (name, host, port, product, hqp_engine, cuda)
                       VALUES ('STUDIO-PC', '127.0.0.1', 4321, 'Signalyst HQPlayer Desktop', '6.2.3', 'full')
                       RETURNING id""")
        endpoint_id = cur.fetchone()[0]
    yield _Db(conn, endpoint_id)
    conn.close()
    pool.closeall()


def _q(conn, sql, *params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _sample(conn, speed, **kw) -> hqp_load.Sample:
    base = dict(endpoint_id=conn.endpoint_id, source="listen", mode="SDM (DSD)", rate_out=11289600,
                filter="poly-sinc-gauss-xla", shaper="ASDM7ECv3", matrix_profile=None,
                convolution=False, adaptive=True, src_rate=44100, src_bits=16, src_channels=2,
                src_sdm=False, process_speed=speed)
    base.update(kw)
    return hqp_load.Sample(**base)


def _ctx(conn, **kw):
    base = {"endpoint_id": conn.endpoint_id, "engine": "6.2.3", "cuda": "full",
            "mode": "SDM (DSD)", "matrix_profile": None, "convolution": False}
    base.update(kw)
    return base


def _schema(conn):
    """What 033 and 034 touch, as the catalogue describes it."""
    tables = ("hqp_endpoints", "hqp_benchmark_runs", "hqp_dsp_samples", "hqp_dsp_speed",
              "hqp_benchmark_points")
    return (
        _q(conn, """SELECT table_name, column_name, data_type, udt_name, is_nullable, column_default
                      FROM information_schema.columns WHERE table_name = ANY(%s)
                     ORDER BY table_name, column_name""", list(tables)),
        _q(conn, """SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid)
                      FROM pg_constraint WHERE conrelid::regclass::text = ANY(%s)
                     ORDER BY 1, 2""", list(tables)),
        _q(conn, "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE tablename = ANY(%s) ORDER BY 2",
           list(tables)),
        _q(conn, """SELECT t.typname, e.enumlabel FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
                     WHERE t.typname IN ('hqp_cuda', 'hqp_sample_source', 'hqp_bench_outcome',
                                         'hqp_point_result')
                     ORDER BY 1, e.enumsortorder"""),
    )


def test_033_and_034_re_apply_and_build_what_001_declares():
    name = "sautium_hqp_load_mig_test"
    admin, nb = _make_db(name)
    try:
        conn = psycopg2.connect(dbname=name, **PG)
        conn.autocommit = True
        declared = _schema(conn)
        with conn.cursor() as cur:
            for m in MIGRATIONS:
                cur.execute(m.read_text())         # a no-op on 001's schema
        assert _schema(conn) == declared
        with conn.cursor() as cur:                 # a node that ran neither
            cur.execute("""DROP TABLE hqp_benchmark_points, hqp_dsp_speed, hqp_dsp_samples,
                                      hqp_benchmark_runs;
                           ALTER TABLE hqp_endpoints DROP COLUMN hqp_version, DROP COLUMN hqp_engine,
                             DROP COLUMN hqp_platform, DROP COLUMN cuda, DROP COLUMN host_cpu,
                             DROP COLUMN host_cores, DROP COLUMN host_gpu, DROP COLUMN host_ram_gb;
                           DROP TYPE hqp_cuda, hqp_sample_source, hqp_bench_outcome,
                                     hqp_point_result;""")
            for m in MIGRATIONS:
                cur.execute(m.read_text())
        assert _schema(conn) == declared
        with conn.cursor() as cur:                 # a node that ran 033 before 034 existed
            cur.execute("""DROP TABLE hqp_benchmark_points;
                           ALTER TABLE hqp_benchmark_runs DROP COLUMN cuda, DROP COLUMN matrix_profile,
                             DROP COLUMN convolution, DROP COLUMN own_mode, DROP COLUMN own_rate,
                             DROP COLUMN own_filter, DROP COLUMN own_filter1x, DROP COLUMN own_shaper,
                             DROP COLUMN last_rate, DROP COLUMN last_filter, DROP COLUMN last_filter1x,
                             DROP COLUMN last_shaper;
                           DROP TYPE hqp_point_result;""")
            cur.execute(MIGRATIONS[1].read_text())
        assert _schema(conn) == declared
        conn.close()
    finally:
        nb._drop_database(admin, name)
        admin.close()


def test_rollup_p10_and_median_since_the_newest_benchmark_point(db):
    for v in (2.0, 2.2, 2.4, 2.6, 2.8):
        assert hqp_load.write(_sample(db, v))
    n, p10, med, bench = _q(db, "SELECT n, speed_p10, speed_median, bench_at FROM hqp_dsp_speed")[0]
    assert (n, round(p10, 3), round(med, 3), bench) == (5, 2.08, 2.4, None)
    # a benchmark point supersedes what came before it ...
    hqp_load.write(_sample(db, 1.5, source="benchmark", settled=False, init_s=3.2, settle_s=None,
                           dropout=False))
    n, p10, bench_settled, init = _q(db, "SELECT n, speed_p10, bench_settled, bench_init_s FROM hqp_dsp_speed")[0]
    assert (n, round(p10, 3), bench_settled, round(init, 1)) == (1, 1.5, False, 3.2)
    # ... and listens after it refine it
    hqp_load.write(_sample(db, 1.6))
    hqp_load.write(_sample(db, 1.7))
    n, p10, med = _q(db, "SELECT n, speed_p10, speed_median FROM hqp_dsp_speed")[0]
    assert (n, round(p10, 3), round(med, 3)) == (3, 1.52, 1.6)
    # every sample took the endpoint's build and CUDA offload
    assert _q(db, "SELECT DISTINCT hqp_engine, cuda::text FROM hqp_dsp_samples") == [("6.2.3", "full")]


def test_a_sample_waits_for_the_endpoints_build(db):
    with db.cursor() as cur:
        cur.execute("UPDATE hqp_endpoints SET hqp_engine = NULL")
    assert not hqp_load.write(_sample(db, 2.0))
    assert _q(db, "SELECT count(*) FROM hqp_dsp_samples") == [(0,)]


def test_covered_by_a_benchmark_point_or_three_listens_of_this_context(db):
    hqp_load.write(_sample(db, 2.0, shaper="A", source="benchmark", dropout=False, settled=True))
    for _ in range(2):
        hqp_load.write(_sample(db, 2.0, shaper="B"))
    for _ in range(3):
        hqp_load.write(_sample(db, 2.0, shaper="C"))
    for _ in range(3):
        hqp_load.write(_sample(db, 2.0, shaper="D", convolution=True))
    covered = hqp_load.covered(_ctx(db))
    assert {k[2] for k in covered} == {"A", "C"}
    assert {k[2] for k in hqp_load.covered(_ctx(db, convolution=True))} == {"D"}
    assert hqp_load.covered(_ctx(db, engine="6.2.4")) == set()
    assert hqp_load.covered(_ctx(db, cuda="off")) == set()
    # a key not seen within the retention is measured again
    with db.cursor() as cur:
        cur.execute("UPDATE hqp_dsp_speed SET last_seen = now() - interval '100 days' WHERE shaper = 'A'")
    assert {k[2] for k in hqp_load.covered(_ctx(db))} == {"C"}


def _run_row(conn, **kw):
    row = dict(hqp_endpoint_id=conn.endpoint_id, hqp_engine="6.2.3", mode="SDM (DSD)", points_planned=10,
               pre_mode=2, pre_rate=3, pre_filter=53, pre_filter1x=49, pre_shaper=21, pre_volume=-3.0,
               cuda="full", convolution=False)
    row.update(kw)
    with conn.cursor() as cur:
        cur.execute(f"INSERT INTO hqp_benchmark_runs ({', '.join(row)}) "
                    f"VALUES ({', '.join(['%s'] * len(row))}) RETURNING id", list(row.values()))
        return cur.fetchone()[0]


def _asked(conn, run_id, result, rate=11289600, filter="poly-sinc-gauss-xla", shaper="ASDM7ECv3",
           at="now()"):
    with conn.cursor() as cur:
        cur.execute(f"""INSERT INTO hqp_benchmark_points (run_id, rate_hz, filter, shaper, src_rate,
                          src_channels, result, at)
                        VALUES (%s, %s, %s, %s, 44100, 2, %s, {at})""",
                    (run_id, rate, filter, shaper, result))


def test_covered_by_what_a_run_asked_and_heard_back(db):
    run = _run_row(db)
    _asked(db, run, "refused", rate=5644800, shaper="ASDM7EC-light 512+fs")    # never a sample
    _asked(db, run, "measured", rate=0, shaper="A")                            # an asked auto rate
    _asked(db, run, "failed", shaper="B")                                      # did not run: again
    _asked(db, run, "unsettled", shaper="C", at="now() - interval '100 days'")  # past the retention
    covered = hqp_load.covered(_ctx(db))
    assert covered == {(5644800, "poly-sinc-gauss-xla", "ASDM7EC-light 512+fs", 44100, 2),
                       (0, "poly-sinc-gauss-xla", "A", 44100, 2)}
    # in another context the run asked nothing
    assert hqp_load.covered(_ctx(db, cuda="off")) == set()
    assert hqp_load.covered(_ctx(db, mode="PCM")) == set()
    assert hqp_load.covered(_ctx(db, engine="6.2.4")) == set()
    # the results sheet shows the refusal, until a sample of the key exists
    refused = [r for r in hqp_load.results(_ctx(db)) if r["class"] == "refused"]
    assert [(r["rate_out"], r["shaper"], r["speed"]) for r in refused] == \
        [(5644800, "ASDM7EC-light 512+fs", None)]
    hqp_load.write(_sample(db, 1.4, rate_out=5644800, shaper="ASDM7EC-light 512+fs"))
    assert [r for r in hqp_load.results(_ctx(db)) if r["class"] == "refused"] == []


def test_the_boundary_comes_from_runs_of_this_build_and_mode_that_said_it(db):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    _run_row(db, outcome="failed", finished_at=now, dropout_speed=2.3)       # lost its connection
    _run_row(db, mode="PCM", outcome="done", finished_at=now, dropout_speed=1.6)
    _run_row(db, hqp_engine="6.1.0", outcome="done", finished_at=now, dropout_speed=1.4)
    assert hqp_load.thresholds(_ctx(db)) == {"ok": 1.25, "no": 1.0, "dropout_speed": None}
    _run_row(db, outcome="cancelled", finished_at=now, dropout_speed=1.12)
    assert hqp_load.thresholds(_ctx(db)) == {"ok": 1.44, "no": 1.15, "dropout_speed": pytest.approx(1.12)}
    assert hqp_load.thresholds(_ctx(db, mode="PCM"))["no"] == 1.65


def test_headroom_classes_by_axis_and_the_hosts_own_boundary(db):
    cur_f, cur_s, cur_r = "poly-sinc-gauss-xla", "ASDM7ECv3", 11289600
    rows = [  # (filter, shaper, rate, speed)
        ("sinc-L", cur_s, cur_r, 0.8), ("poly-sinc-ext2", cur_s, cur_r, 1.1), (cur_f, cur_s, cur_r, 2.5),
        (cur_f, "ASDM7EC-super", cur_r, 1.2), (cur_f, cur_s, 22579200, 1.2),
        ("sinc-L", "ASDM7EC-super", 22579200, 0.3),        # varies two axes: on no picker
    ]
    for f, s, r, v in rows:
        hqp_load.write(_sample(db, v, filter=f, shaper=s, rate_out=r))
    hr = hqp_load.headroom(_ctx(db), rate_out=cur_r, filter=cur_f, shaper=cur_s,
                           src_rate=44100, src_channels=2)
    assert {k: e["class"] for k, e in hr["filters"].items()} == {
        "sinc-L": "no", "poly-sinc-ext2": "tight", cur_f: "ok"}
    assert {k: e["class"] for k, e in hr["shapers"].items()} == {"ASDM7EC-super": "tight", cur_s: "ok"}
    assert {k: e["class"] for k, e in hr["rates"].items()} == {"22579200": "tight", str(cur_r): "ok"}
    assert hr["thresholds"] == {"ok": 1.25, "no": 1.0, "dropout_speed": None}
    # another source rate: nothing measured, nothing guessed
    other = hqp_load.headroom(_ctx(db), rate_out=cur_r, filter=cur_f, shaper=cur_s,
                              src_rate=96000, src_channels=2)
    assert (other["filters"], other["shapers"], other["rates"]) == ({}, {}, {})
    # a run that dropped out at 1.12× moves "no" just above it on this host
    with db.cursor() as cur:
        cur.execute("""INSERT INTO hqp_benchmark_runs (hqp_endpoint_id, hqp_engine, mode, points_planned,
                         finished_at, outcome, dropout_speed, pre_mode, pre_rate, pre_filter, pre_filter1x,
                         pre_shaper, pre_volume)
                       VALUES (%s, '6.2.3', 'SDM (DSD)', 10, now(), 'done', 1.12, 2, 3, 53, 49, 21, -3)""",
                    (db.endpoint_id,))
    hr = hqp_load.headroom(_ctx(db), rate_out=cur_r, filter=cur_f, shaper=cur_s,
                           src_rate=44100, src_channels=2)
    assert hr["thresholds"] == {"ok": 1.44, "no": 1.15, "dropout_speed": pytest.approx(1.12)}
    assert hr["filters"]["poly-sinc-ext2"]["class"] == "no"
    assert hr["shapers"]["ASDM7EC-super"]["class"] == "tight"


def test_retention_drops_old_samples_and_keeps_the_rollup(db):
    for _ in range(3):
        hqp_load.write(_sample(db, 2.0))
    with db.cursor() as cur:
        cur.execute("UPDATE hqp_dsp_samples SET sampled_at = now() - interval '91 days'")
    hqp_load._pruned_at = None
    hqp_load.write(_sample(db, 1.4, shaper="other"))
    assert _q(db, "SELECT shaper FROM hqp_dsp_samples") == [("other",)]
    assert _q(db, "SELECT n FROM hqp_dsp_speed WHERE shaper = 'ASDM7ECv3'") == [(3,)]
    # at most once a day: an old sample written now stays until tomorrow's
    with db.cursor() as cur:
        cur.execute("UPDATE hqp_dsp_samples SET sampled_at = now() - interval '91 days'")
    hqp_load.write(_sample(db, 1.4, shaper="third"))
    assert _q(db, "SELECT count(*) FROM hqp_dsp_samples") == [(2,)]


def test_note_info_keeps_the_build_and_this_machine(db, monkeypatch):
    import hqp_library
    info = {"name": "STUDIO-PC", "product": "Signalyst HQPlayer Desktop", "version": "6",
            "platform": "Windows", "engine": "6.2.4"}
    hqp_library.note_info(db.endpoint_id, info, here=False)
    assert _q(db, "SELECT hqp_version, hqp_engine, hqp_platform, cuda::text, host_cpu FROM hqp_endpoints") == \
        [("6", "6.2.4", "Windows", "full", None)]
    monkeypatch.setattr(hqp_library, "_this_machine",
                        lambda: {"cpu": "Example CPU", "cores": 32, "gpu": "Example GPU", "ram_gb": 31.2})
    monkeypatch.setattr(hqp_library, "local_cuda", lambda: (True, "off"))
    hqp_library.note_info(db.endpoint_id, info, here=True)
    assert _q(db, "SELECT cuda::text, host_cpu, host_cores, host_gpu FROM hqp_endpoints") == \
        [("off", "Example CPU", 32, "Example GPU")]
    # settings that could not be read leave the answer standing ...
    monkeypatch.setattr(hqp_library, "local_cuda", lambda: (False, None))
    hqp_library.note_info(db.endpoint_id, info, here=True)
    assert _q(db, "SELECT cuda::text FROM hqp_endpoints") == [("off",)]
    # ... settings read with a value never seen are not known: not the old answer
    monkeypatch.setattr(hqp_library, "local_cuda", lambda: (True, None))
    hqp_library.note_info(db.endpoint_id, info, here=True)
    assert _q(db, "SELECT cuda::text FROM hqp_endpoints") == [(None,)]


def test_local_cuda_reads_hqplayers_engine_setting(tmp_path, monkeypatch):
    import hqp_library
    from playback import hqp_diagnostics
    monkeypatch.setattr(hqp_diagnostics, "local_log_dir", lambda: tmp_path)
    assert hqp_library.local_cuda() == (False, None)
    for value, expect in (("1", "full"), ("0", "off"), ("2", None)):
        path = tmp_path / "settings.xml"
        path.write_text(f'<?xml version="1.0"?><hqplayer><engine cuda="{value}" type="asio"/></hqplayer>')
        os.utime(path, ns=(0, len(value) + int(value) * 1000))      # a new version of the file
        assert hqp_library.local_cuda() == (True, expect)
    # a data folder that is there but cannot be read (a mount that dropped)
    monkeypatch.setattr(hqp_diagnostics, "local_log_dir", lambda: tmp_path / "gone")
    assert hqp_library.local_cuda() == (False, None)
