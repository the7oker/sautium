"""The two analysis passes and the owner's switch (2026-11-11), on a real
PostgreSQL built from the migrations — the module's scratch database
(conftest.scratch_dsn), skipped without a cluster.

A node analyses its files in two passes, each one decode a track: the sonic
pass (the fingerprint, the segment grid, CLAP, librosa — segments and
features) and the instrument pass (the AST+PaSST tagger), the second behind
"Recognise instruments" (enrichment.instruments: on for full and standard,
off for lite while the owner never set it). Every track's sonic pass goes
before any track's instruments: the instrument pass gives way at its batch
seams to files a scan added. Both register before they compute and skip what
the material group holds at a rank no lower; an embedding without its
segments holds nothing. Each records its pace after every batch, and the
Library screen's estimate is the instrument pass's pending audio at it, no
model loaded. A stream runs the tagger only with the switch on, alone when
the track's sonic records are held, and is "already held" per kind.

The models are fakes (CLAP one unit vector, librosa a fixed map, the tagger
fixed scores), as fpcalc and ffprobe are where a registration would decode."""

import asyncio
import math
import sys
import time
import types
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
for p in (REPO, BACKEND):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

psycopg2 = pytest.importorskip("psycopg2")
np = pytest.importorskip("numpy")
import psycopg2.pool  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import analysis_pace  # noqa: E402
import audio_analysis  # noqa: E402
import database  # noqa: E402
import db_pool  # noqa: E402
import embeddings  # noqa: E402
import hardware_profile  # noqa: E402
import notary  # noqa: E402
import provenance  # noqa: E402
from desktop.p2p import material  # noqa: E402
from routers import settings as settings_router  # noqa: E402
from tests.p2p.fingerprints import encode, flip, noise  # noqa: E402
from uuid_utils import embedding_model_uuid  # noqa: E402

MODEL = str(embedding_model_uuid("clap"))
FP = encode(noise(950, 11))
NEAR = encode(flip(material.decode(FP), 0.01, 12))     # FP under another decoder
OTHER = encode(noise(950, 13))                         # another material
UNIT = np.full(512, 1 / np.sqrt(512), dtype=np.float32)
VECTOR = "[" + ",".join([str(float(UNIT[0]))] * 512) + "]"


def _profile(name="full", device="cpu", ml=True, cores=4):
    return hardware_profile.HardwareProfile(
        name=name, source="env", device=device, accel_name=None, accel_memory_gb=16.0,
        ram_gb=16.0, cores=cores, ml_available=ml)


@pytest.fixture
def cur(scratch_dsn, monkeypatch):
    pool = psycopg2.pool.ThreadedConnectionPool(1, 4, dsn=scratch_dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    engine = database.make_engine(scratch_dsn)
    monkeypatch.setattr(database, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute("TRUNCATE tracks, albums, embedding_models, signing_batches, stream_providers, "
                  "analysis_pace, user_settings CASCADE")
        c.execute("INSERT INTO embedding_models (id, name, dimension) VALUES (%s, 'clap', 512)",
                  (MODEL,))
        c.execute("INSERT INTO stream_providers (id, name, lossless) VALUES "
                  "('lossless-test', 'A lossless provider', true)")
        yield c
    conn.close()
    engine.dispose()
    pool.closeall()


@pytest.fixture
def prints(monkeypatch):
    """What fpcalc prints for a file (by its name) or a stream ("stream"),
    and what ffprobe reads of a stream."""
    printed = {}
    monkeypatch.setattr(provenance, "fingerprinter",
                        lambda: provenance.Fingerprinter("ok", "/usr/bin/fpcalc"))
    monkeypatch.setattr(provenance, "chromaprint_file", lambda path, *a: printed.get(
        "/music/" + path.rsplit("/", 1)[-1], printed.get("stream")))
    monkeypatch.setattr(provenance, "_probe_audio", lambda path: (44100, 16, printed["stream_dur"]))
    return printed


class _Tagger:
    def __init__(self, scores, on_tag=None):
        self.scores, self.on_tag, self.calls = scores, on_tag, 0

    def tag_full_track(self, audio):
        self.calls += 1
        if self.on_tag:
            self.on_tag(self.calls)
        return dict(self.scores)


@pytest.fixture
def models(monkeypatch):
    """The passes' models stood in for, each call said in `calls`: every
    decode twenty seconds of silence (by pass and file name), CLAP one unit
    vector, librosa a fixed map, the tagger `models.tagger`. Loading CLAP or
    the tagger is said too; the notary's wakes are kept."""
    calls = []
    silence = np.zeros(48000 * 20, dtype=np.float32)
    gen = embeddings.AudioEmbeddingGenerator
    ns = types.SimpleNamespace(calls=calls, wakes=[], tagger=_Tagger({"piano": 0.62}),
                               on_clap=None, tagger_load_seconds=0.0)

    def decoded(kind):
        def load(*args):
            path = next(a for a in args if isinstance(a, str))
            calls.append((kind, path.rsplit("/", 1)[-1]))
            return silence
        return load

    monkeypatch.setattr(embeddings, "_reanalyze_imported", lambda: False)
    monkeypatch.setattr(notary, "wake", lambda reason, **kw: ns.wakes.append(reason))
    monkeypatch.setattr(gen, "load_model", lambda self: calls.append(("load", "CLAP")))
    monkeypatch.setattr(gen, "_load_audio", lambda self, *a: decoded("sonic decode")(*a))
    def clap(self, audio):
        calls.append(("CLAP", None))
        if ns.on_clap:
            ns.on_clap()
        return [0], np.stack([UNIT]), UNIT
    monkeypatch.setattr(gen, "_compute_segments", clap)
    monkeypatch.setattr(audio_analysis.AudioAnalyzer, "features_from_array",
                        lambda self, audio, sr=48000: (calls.append(("librosa", None)),
                                                       {"bpm": 77.0, "energy": 0.5})[1])
    monkeypatch.setattr(audio_analysis, "load_full_track_48k", decoded("instrument decode"))

    def load_tagger(self):
        calls.append(("load", "AST+PaSST"))
        time.sleep(ns.tagger_load_seconds)
        self.ensemble = ns.tagger
    monkeypatch.setattr(audio_analysis.AudioAnalyzer, "load_model", load_tagger)
    ns.sonic = lambda track_ids=None, **kw: gen(model_name="clap", device="cpu").sonic_pass(
        track_ids=track_ids, **kw)
    ns.instruments = lambda track_ids=None, **kw: audio_analysis.AudioAnalyzer(
        device="cpu").instrument_pass(track_ids=track_ids, **kw)
    return ns


def _track(cur, title="t") -> str:
    tid = str(uuid.uuid4())
    cur.execute("INSERT INTO tracks (id, title) VALUES (%s, %s)", (tid, title))
    return tid


def _file(cur, track, path, bit_depth=16, lossless=True, duration=240, source=True,
          away=False) -> int:
    album = str(uuid.uuid4())
    cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'LP')", (album,))
    cur.execute("INSERT INTO album_variants (album_id, directory_path, absent_since) "
                "VALUES (%s, %s, %s) RETURNING id",
                (album, path.rsplit("/", 1)[0] + "/" + album, "2026-11-01" if away else None))
    variant = cur.fetchone()[0]
    cur.execute("""INSERT INTO media_files (track_id, album_variant_id, file_path, bit_depth,
                                            sample_rate, is_lossless, duration_seconds,
                                            is_analysis_source)
                   VALUES (%s, %s, %s, %s, 44100, %s, %s, %s) RETURNING id""",
                (track, variant, path, bit_depth, lossless, duration, source))
    return cur.fetchone()[0]


def _source(cur, track, fp, *, imported=False, duration=240, media_file=None, provider=None,
            bit_depth=16, material_of=None) -> int:
    cur.execute("""INSERT INTO analysis_sources (track_id, chromaprint, duration_seconds, imported,
                                                 media_file_id, provider_id, is_lossless,
                                                 bit_depth, material_of)
                   VALUES (%s, %s, %s, %s, %s, %s, true, %s, %s) RETURNING id""",
                (track, fp, duration, imported, media_file, provider,
                 None if imported else bit_depth, material_of))
    return cur.fetchone()[0]


def _embedding(cur, track, source, segments=True) -> int:
    cur.execute("""INSERT INTO embeddings (track_id, model_id, vector, analysis_source_id,
                                           analysis_version)
                   VALUES (%s, %s, %s::vector, %s, 2) RETURNING id""",
                (track, MODEL, VECTOR, source))
    eid = cur.fetchone()[0]
    if segments:
        cur.execute("INSERT INTO embedding_segments (embedding_id, segment_index, vector) "
                    "VALUES (%s, 0, %s::vector)", (eid, VECTOR))
    return eid


def _features(cur, track, source):
    cur.execute("INSERT INTO audio_features (track_id, bpm, analysis_source_id, analysis_version) "
                "VALUES (%s, 99, %s, 2)", (track, source))


def _instruments(cur, track, source, scores='{"organ": 0.4}'):
    cur.execute("INSERT INTO track_instruments (track_id, instruments, analysis_source_id, "
                "analysis_version) VALUES (%s, %s, %s, 2)", (track, scores, source))


def _sonic(cur, track, source):
    _embedding(cur, track, source)
    _features(cur, track, source)


def _records(cur, track) -> dict:
    cur.execute("""SELECT 'embeddings', analysis_source_id FROM embeddings WHERE track_id = %(t)s
                   UNION ALL SELECT 'features', analysis_source_id FROM audio_features
                    WHERE track_id = %(t)s
                   UNION ALL SELECT 'instruments', analysis_source_id FROM track_instruments
                    WHERE track_id = %(t)s""", {"t": track})
    return dict(cur.fetchall())


def _queued(track, reanalyze=False) -> dict:
    """Which record each pass's own queue holds the track for."""
    from sqlalchemy import text
    params = {"min_seconds": provenance.MIN_MATERIAL_SECONDS, "reanalyze": reanalyze,
              "t": track}
    out = {}
    with database.get_db_context() as db:
        row = db.execute(text(f"SELECT q.embedding_pending, q.features_pending "
                              f"FROM ({embeddings.SOURCE_FILES_SQL}) q WHERE q.track_id = :t"),
                         params).first()
        out["embeddings"], out["features"] = (bool(row[0]), bool(row[1])) if row else (False, False)
        out["instruments"] = bool(db.execute(text(
            f"SELECT 1 FROM ({analysis_pace.INSTRUMENTS_PENDING_SQL}) q WHERE q.track_id = :t"),
            params).first())
    return out


# -- The two passes --------------------------------------------------------------

def test_each_pass_decodes_a_track_once_and_makes_its_own_records(cur, prints, models):
    track = _track(cur)
    _file(cur, track, "/music/a.flac")
    prints["/music/a.flac"] = FP
    assert _queued(track) == {"embeddings": True, "features": True, "instruments": True}

    sonic = models.sonic([uuid.UUID(track)])
    assert (sonic["success"], sonic["embeddings"], sonic["features"]) == (1, 1, 1)
    assert models.calls == [("load", "CLAP"), ("sonic decode", "a.flac"), ("librosa", None),
                            ("CLAP", None)]
    assert _queued(track) == {"embeddings": False, "features": False, "instruments": True}

    models.calls.clear()
    stats = models.instruments([uuid.UUID(track)])
    assert (stats["success"], stats["skipped"], stats["failed"]) == (1, 0, 0)
    assert models.calls == [("load", "AST+PaSST"), ("instrument decode", "a.flac")]
    assert models.tagger.calls == 1
    assert _queued(track) == {"embeddings": False, "features": False, "instruments": False}
    records = _records(cur, track)
    assert set(records) == {"embeddings", "features", "instruments"}
    assert len(set(records.values())) == 1        # one registration, every record under it
    cur.execute("SELECT pass::text, batches FROM analysis_pace ORDER BY analysis_pace.pass")
    assert cur.fetchall() == [("sonic", 1), ("instruments", 1)]


def test_the_notary_is_woken_after_an_instrument_save_and_not_for_a_batch_that_saved_none(
        cur, prints, models):
    """sign() leaves a record re-saved while it signed unsigned until the
    next wake, so the instrument pass wakes the notary after each batch it
    saved in."""
    track = _track(cur)
    _file(cur, track, "/music/a.flac")
    prints["/music/a.flac"] = FP
    models.sonic([uuid.UUID(track)])
    assert models.wakes == ["sonic pass"]
    models.instruments([uuid.UUID(track)])
    assert models.wakes == ["sonic pass", "instrument pass"]


def test_both_passes_register_first_and_skip_what_the_group_holds(cur, prints, models):
    """Another rip of the analysed master at a rank no higher (a 24-bit copy
    of a CD's, the CD gone): registered, joined to the group, nothing
    decoded or computed, the notary not woken — and not asked again."""
    track = _track(cur)
    cd = _source(cur, track, FP, bit_depth=16)
    _sonic(cur, track, cd)
    _instruments(cur, track, cd)
    _file(cur, track, "/music/hr.flac", bit_depth=24)
    prints["/music/hr.flac"] = NEAR
    assert _queued(track) == {"embeddings": True, "features": True, "instruments": True}

    assert models.sonic([uuid.UUID(track)])["skipped"] == 1
    # the sonic pass's registration joined the file to the group, which
    # holds the instruments too: the instrument pass has nothing queued
    assert _queued(track)["instruments"] is False
    assert models.instruments([uuid.UUID(track)])["processed"] == 0
    assert models.calls == [("load", "CLAP")] and models.tagger.calls == 0
    assert models.wakes == []
    assert _queued(track) == {"embeddings": False, "features": False, "instruments": False}
    assert set(_records(cur, track).values()) == {cd}


def test_the_instrument_pass_registers_first_and_skips_what_the_group_holds(cur, prints, models):
    """Run on its own over such a file (no sonic pass registered it): the
    registration finds the group holding the instruments, and the pair is
    never loaded, nothing decoded."""
    track = _track(cur)
    cd = _source(cur, track, FP, bit_depth=16)
    _instruments(cur, track, cd)
    _file(cur, track, "/music/hr.flac", bit_depth=24)
    prints["/music/hr.flac"] = NEAR
    stats = models.instruments([uuid.UUID(track)])
    assert (stats["processed"], stats["skipped"], stats["success"]) == (1, 1, 0)
    assert models.calls == [] and models.wakes == []
    assert _queued(track)["instruments"] is False


# -- The instrument pass's queue ---------------------------------------------------

@pytest.mark.parametrize("record,pending,with_reanalyze", [
    ("none", True, True),
    ("unlinked", True, True),
    ("another material", True, True),
    ("this material, a rank no higher", False, False),
    ("imported", False, True),
])
def test_the_instrument_queue_asks_what_the_file_does_not_hold(cur, prints, record, pending,
                                                               with_reanalyze):
    track = _track(cur)
    mf = _file(cur, track, "/music/a.flac")
    if record == "unlinked":
        _instruments(cur, track, None)
    elif record == "another material":
        _instruments(cur, track, _source(cur, track, OTHER))
    elif record == "this material, a rank no higher":
        _instruments(cur, track, _source(cur, track, FP, media_file=mf))
    elif record == "imported":
        _instruments(cur, track, _source(cur, track, NEAR, imported=True))
    assert _queued(track)["instruments"] is pending
    assert _queued(track, reanalyze=True)["instruments"] is with_reanalyze


@pytest.mark.parametrize("how", ["under the floor", "away", "not the analysis source"])
def test_the_instrument_queue_never_holds_a_file_no_pass_reads(cur, how):
    track = _track(cur)
    _file(cur, track, "/music/a.flac", duration=5 if how == "under the floor" else 240,
          away=how == "away", source=how != "not the analysis source")
    assert _queued(track) == {"embeddings": False, "features": False, "instruments": False}


def test_an_owned_track_whose_old_instruments_were_absent_is_the_instrument_pass_s(
        cur, prints, models):
    """The cutover copied only maps: a track whose instruments were SQL NULL
    or JSON null before it has sonic records and no instruments row, and the
    instrument pass makes them."""
    track = _track(cur)
    mf = _file(cur, track, "/music/a.flac")
    prints["/music/a.flac"] = FP
    _sonic(cur, track, _source(cur, track, FP, media_file=mf))
    assert _queued(track) == {"embeddings": False, "features": False, "instruments": True}
    assert models.instruments([uuid.UUID(track)])["success"] == 1
    assert _queued(track)["instruments"] is False


# -- The legacy mean ---------------------------------------------------------------

@pytest.mark.parametrize("imported", [False, True])
def test_a_first_hand_embedding_without_segments_is_the_sonic_pass_s_again(
        cur, prints, models, imported):
    """A portrait without its segments derives no zero-shot score: first-hand,
    it is pending and held for nothing, and the sonic pass makes its segments;
    imported, the sync fills the gap."""
    track = _track(cur)
    mf = _file(cur, track, "/music/a.flac")
    prints["/music/a.flac"] = FP
    sid = _source(cur, track, FP, imported=imported, media_file=None if imported else mf)
    eid = _embedding(cur, track, sid, segments=False)
    _features(cur, track, sid)
    assert _queued(track)["embeddings"] is not imported
    if imported:
        return
    with database.get_db_context() as db:
        assert not provenance.held_in_group(db, sid, "embeddings", model_id=MODEL)
        assert provenance.held_in_group(db, sid, "audio_features")
    stats = models.sonic([uuid.UUID(track)])
    assert (stats["embeddings"], stats["features"]) == (1, 0)
    cur.execute("SELECT count(*) FROM embedding_segments WHERE embedding_id = %s", (eid,))
    assert cur.fetchone() == (1,)
    assert _queued(track)["embeddings"] is False


# -- The order rule --------------------------------------------------------------

@pytest.fixture
def run(cur, prints, models, monkeypatch):
    """run_parallel_enrichment with the passes' models stood in for, one
    track a batch (so the instrument pass has a seam between any two), the
    text half and the canon out of the way."""
    import device
    import enrichment_embeddings
    import instrument_tagger
    import lyrics_embeddings
    import shutil
    import text_embeddings
    import track_enrichment
    from canon import migrations
    monkeypatch.setattr(device, "audio_batch_budget_seconds", lambda **kw: 1.0)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(migrations, "normalize_artists", lambda db, pass1: {})
    monkeypatch.setattr(instrument_tagger, "release_instrument_tagger", lambda dev: None)
    for module, name in ((text_embeddings, "generate_text_embeddings"),
                         (lyrics_embeddings, "generate_lyrics_embeddings"),
                         (enrichment_embeddings, "generate_all_enrichment_embeddings")):
        monkeypatch.setattr(module, name, lambda **kw: {})
    _on_cpu(monkeypatch)
    switch = {"on": True}
    monkeypatch.setattr(settings_router, "instruments_on", lambda: switch["on"])
    return types.SimpleNamespace(
        go=lambda **kw: track_enrichment.run_parallel_enrichment(skip_lastfm=True, skip_lyrics=True,
                                                                **kw),
        switch=switch)


def _on_cpu(monkeypatch):
    """The passes' classes as the run and the pipeline construct them: CLAP
    by the name the scratch database knows, every model on the CPU."""
    real_gen, real_analyzer = embeddings.AudioEmbeddingGenerator, audio_analysis.AudioAnalyzer
    monkeypatch.setattr(embeddings, "AudioEmbeddingGenerator",
                        lambda *a, **kw: real_gen(model_name="clap", device="cpu"))
    monkeypatch.setattr(audio_analysis, "AudioAnalyzer",
                        lambda *a, **kw: real_analyzer(device="cpu"))


def _decodes(models):
    return [(kind.split()[0], name) for kind, name in models.calls if kind.endswith("decode")]


def test_the_instrument_pass_gives_way_at_a_seam_to_files_a_scan_added(cur, prints, models, run):
    tracks = {name: _track(cur, name) for name in ("a", "b")}
    for name, track in tracks.items():
        _file(cur, track, f"/music/{name}.flac")
        prints[f"/music/{name}.flac"] = encode(noise(950, ord(name)))
    prints["/music/c.flac"] = encode(noise(950, ord("c")))

    def a_scan_adds_a_file(call):
        if call == 1:
            _file(cur, _track(cur, "c"), "/music/c.flac")
    models.tagger.on_tag = a_scan_adds_a_file

    result = run.go()
    decodes = _decodes(models)
    assert set(decodes[:2]) == {("sonic", "a.flac"), ("sonic", "b.flac")}
    first = decodes[2]
    assert first[0] == "instrument"
    assert decodes[3] == ("sonic", "c.flac")           # before any other track's instruments
    rest = {"a.flac", "b.flac", "c.flac"} - {first[1]}
    assert set(decodes[4:]) == {("instrument", name) for name in rest}
    gpu = result["gpu"]
    assert (gpu["sonic"]["success"], gpu["instruments"]["success"]) == (3, 3)


def test_a_file_the_sonic_pass_could_not_analyse_never_calls_it_back(cur, prints, models, run):
    """One without a fingerprint stays pending, never decoded (no address,
    nothing to save), and the instrument pass, which cannot address it
    either, does not give way to it at every seam."""
    for name in ("a", "b", "x"):
        _file(cur, _track(cur, name), f"/music/{name}.flac")
        if name != "x":
            prints[f"/music/{name}.flac"] = encode(noise(950, ord(name)))
    result = run.go()
    assert result["gpu"]["sonic"]["failed"] == result["gpu"]["instruments"]["failed"] == 1
    assert result["gpu"]["instruments"]["success"] == 2
    assert sorted(_decodes(models)) == [("instrument", "a.flac"), ("instrument", "b.flac"),
                                        ("sonic", "a.flac"), ("sonic", "b.flac")]


def test_with_the_switch_off_the_run_makes_no_instruments_and_removes_none(
        cur, prints, models, run):
    keep = _track(cur, "kept")
    _instruments(cur, keep, None, '{"harp": 0.3}')
    track = _track(cur, "a")
    _file(cur, track, "/music/a.flac")
    prints["/music/a.flac"] = FP
    run.switch["on"] = False
    result = run.go()
    assert "instruments" not in result["gpu"] and models.tagger.calls == 0
    assert ("load", "AST+PaSST") not in models.calls
    assert set(_records(cur, track)) == {"embeddings", "features"}
    cur.execute("SELECT instruments FROM track_instruments WHERE track_id = %s", (keep,))
    assert cur.fetchone() == ({"harp": 0.3},)


# -- The switch ------------------------------------------------------------------

@pytest.mark.parametrize("name,ml,stored,on", [
    ("full", True, None, True), ("standard", True, None, True), ("lite", True, None, False),
    ("lite", True, True, True), ("full", True, False, False), ("lite", False, True, False),
    ("full", False, None, False),
])
def test_the_switch_is_the_profiles_until_the_owner_sets_it(cur, monkeypatch, name, ml, stored,
                                                            on):
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile(name, ml=ml))
    if stored is not None:
        settings_router._write("enrichment.instruments", stored)
    assert settings_router.instruments_on() is on


def test_the_switch_is_written_and_the_library_event_carries_the_new_estimate(cur, monkeypatch):
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("lite"))
    woken = []
    monkeypatch.setattr(settings_router, "notify_library_subscribers", lambda: woken.append(1))
    assert settings_router._instruments_state()["enabled"] is False
    answer = asyncio.run(settings_router.set_instruments(settings_router.InstrumentsReq(enabled=True)))
    assert answer == {"success": True, "enabled": True} and woken == [1]
    assert settings_router._instruments_state()["enabled"] is True


def test_lite_runs_the_sonic_pass_and_keeps_the_instruments_behind_the_switch():
    lite = _profile("lite")
    assert lite.local_analysis and not lite.instruments_by_default
    assert _profile("standard", device="cuda").instruments_by_default
    assert not _profile("lite", ml=False).local_analysis


def test_the_guidance_trail_points_a_lite_node_at_analyse_library(cur, monkeypatch):
    monkeypatch.chdir(BACKEND)
    import main  # noqa: F401  — the trail reads its workers' state
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("lite"))
    monkeypatch.setattr(settings_router, "_synced_since_scan", lambda: True)
    monkeypatch.setattr(settings_router, "_notices_state", lambda: {"items": []})
    track = _track(cur)
    _file(cur, track, "/music/a.flac")
    assert "analyse_library" in settings_router._guidance_state()["tasks"]


def test_the_analysis_run_on_lite_runs_its_sonic_pass(monkeypatch):
    monkeypatch.chdir(BACKEND)
    import main
    import track_enrichment
    asked = {}
    monkeypatch.setattr(track_enrichment, "run_parallel_enrichment",
                        lambda **kw: asked.update(kw) or {})
    monkeypatch.setattr(main, "_notify_library_subs_safe", lambda: None)
    # The run's state is the process's: this test's own, put back after it
    monkeypatch.setattr(main, "_enrich_state", {**main._enrich_state, "running": True})
    main._enrich_worker(None, _profile("lite").local_analysis)
    assert asked["skip_sonic"] is False and asked["skip_instruments"] is False


# -- The estimate --------------------------------------------------------------

@pytest.fixture
def no_model(monkeypatch):
    """Loading either model for an estimate fails the test."""
    import clap_model
    import instrument_tagger

    def refused(*a, **kw):
        raise AssertionError("a model was loaded for an estimate")
    monkeypatch.setattr(instrument_tagger, "get_instrument_tagger", refused)
    monkeypatch.setattr(clap_model, "get_clap_model", refused)


def _pending_files(cur, durations):
    for i, d in enumerate(durations):
        _file(cur, _track(cur), f"/music/p{i}.flac", duration=d)


def _estimate(device="cpu", cores=4, threads=2):
    with database.get_db_context() as db:
        analysis_pace._machine = lambda: (device, cores)
        analysis_pace._tagger_threads = lambda: threads
        return analysis_pace.estimate(db, reanalyze=False)


@pytest.fixture
def machine(monkeypatch):
    monkeypatch.setattr(analysis_pace, "_machine", analysis_pace._machine)
    monkeypatch.setattr(analysis_pace, "_tagger_threads", analysis_pace._tagger_threads)


def test_the_estimate_before_any_batch_is_the_measured_reference_scaled_by_the_cores(
        cur, no_model, machine):
    _pending_files(cur, [240, 120, None])     # an unknown length counts as five minutes
    e = _estimate(cores=8)
    assert (e["tracks"], e["audio_seconds"], e["basis"]) == (3, 660.0, "reference")
    assert e["pace"] == pytest.approx(2.2 * 4 / 8 * 8.5)
    assert e["seconds"] == pytest.approx(11 * 2.2 * 4 / 8 * 8.5)


def test_the_estimate_takes_the_sonic_pace_then_the_instrument_pace_of_this_machine(
        cur, no_model, machine):
    _pending_files(cur, [600])
    with database.get_db_context() as db:
        analysis_pace._machine = lambda: ("cpu", 4)
        analysis_pace.record(db, analysis_pace.SONIC, 30.0, 600.0)          # 3 s a minute
        analysis_pace.record(db, analysis_pace.SONIC, 10.0, 600.0)          # 1 s a minute
    e = _estimate()
    assert (e["basis"], e["pace"]) == ("sonic", pytest.approx(2.0 * 8.5))
    with database.get_db_context() as db:
        analysis_pace.record(db, analysis_pace.INSTRUMENTS, 200.0, 600.0)   # 20 s a minute
        analysis_pace.record(db, analysis_pace.INSTRUMENTS, 0.0, 0.0)       # nothing tagged
    e = _estimate()
    assert (e["basis"], e["pace"], e["seconds"]) == ("measured", pytest.approx(20.0),
                                                      pytest.approx(200.0))
    cur.execute("SELECT pass::text, batches FROM analysis_pace ORDER BY analysis_pace.pass")
    assert cur.fetchall() == [("sonic", 2), ("instruments", 1)]
    # another machine — a GPU added, other cores — measures anew
    assert _estimate(device="mps")["basis"] == "measuring"
    assert _estimate(cores=8)["basis"] == "reference"


def test_with_no_ratio_for_the_device_class_the_row_says_it_is_measuring(cur, no_model, machine):
    _pending_files(cur, [240])
    with database.get_db_context() as db:
        analysis_pace._machine = lambda: ("mps", 16)
        analysis_pace.record(db, analysis_pace.SONIC, 4.0, 240.0)
    e = _estimate(device="mps", cores=16)
    assert (e["tracks"], e["basis"], e["pace"], e["seconds"]) == (1, "measuring", None, None)


def test_on_cuda_the_estimate_waits_for_a_sonic_batch_then_takes_its_ratio(cur, no_model,
                                                                          machine):
    """No reference pace for a GPU: before any batch here it is measuring;
    after the sonic pass's first batch, its pace × the measured ratio."""
    _pending_files(cur, [240])
    assert _estimate(device="cuda", cores=16)["basis"] == "measuring"
    with database.get_db_context() as db:
        analysis_pace._machine = lambda: ("cuda", 16)
        analysis_pace.record(db, analysis_pace.SONIC, 2.0, 240.0)          # 0.5 s a minute
    e = _estimate(device="cuda", cores=16)
    assert (e["basis"], e["pace"]) == ("sonic", pytest.approx(0.5 * analysis_pace.CUDA_RATIO))


def test_nothing_pending_is_no_time_at_all(cur, no_model, machine):
    e = _estimate()
    assert (e["tracks"], e["audio_seconds"], e["seconds"]) == (0, 0.0, 0.0)


# -- The coverage rows --------------------------------------------------------------

def test_the_coverage_rows_count_the_analysable_tracks_with_instruments(cur, monkeypatch):
    monkeypatch.chdir(BACKEND)
    import main
    tagged, untagged, short = _track(cur), _track(cur), _track(cur)
    _file(cur, tagged, "/music/a.flac")
    _file(cur, untagged, "/music/b.flac")
    _file(cur, short, "/music/c.flac", duration=5)
    _instruments(cur, tagged, None)
    _instruments(cur, short, None)            # under the floor: in neither side
    stats = asyncio.run(main.get_stats())
    assert (stats["analysable_tracks"], stats["tracks_with_instruments"]) == (2, 1)


# -- Streams ----------------------------------------------------------------------

@pytest.fixture
def stream(cur, prints, models, monkeypatch):
    """A stream of a phantom track, its decode said in `models.calls`; the
    stream's own models are the passes' fakes."""
    import librosa
    from streaming import enrichment
    switch = {"on": True}
    monkeypatch.setattr(settings_router, "instruments_on", lambda: switch["on"])
    monkeypatch.setattr(librosa, "load", lambda f, sr, mono: (
        models.calls.append(("stream decode", None)), (np.zeros(48000 * 60, dtype=np.float32), sr))[1])
    prints.update(stream=FP, stream_dur=60)
    enricher = enrichment.PreviewEnricher()
    analyzer = audio_analysis.AudioAnalyzer(device="cpu")
    analyzer.ensemble = models.tagger
    enricher._embedder = embeddings.AudioEmbeddingGenerator(model_name="clap", device="cpu")
    enricher._analyzer = analyzer
    monkeypatch.setattr(enricher, "_models", lambda sonic, tag: (enricher._embedder, analyzer))
    return types.SimpleNamespace(
        run=lambda track: enricher._enrich(track, b"stream", (60.0,), True, "lossless-test"),
        switch=switch)


def test_a_stream_of_a_track_with_its_sonic_records_held_runs_the_tagger_alone(
        cur, models, stream):
    """Its instruments bind the stream's own first-hand row, beside the
    imported twin that holds the sonic records."""
    track = _track(cur)
    twin = _source(cur, track, NEAR, imported=True, duration=60)
    _sonic(cur, track, twin)
    stream.run(track)
    assert ("CLAP", None) not in models.calls and ("librosa", None) not in models.calls
    assert models.tagger.calls == 1
    records = _records(cur, track)
    assert records["embeddings"] == records["features"] == twin
    cur.execute("SELECT imported, provider_id, material_of FROM analysis_sources WHERE id = %s",
                (records["instruments"],))
    assert cur.fetchone() == (False, "lossless-test", twin)
    assert "preview analysis" in models.wakes


@pytest.mark.parametrize("why", ["the switch off", "the instruments held"])
def test_a_stream_of_a_track_with_an_embedding_returns_early(cur, models, stream, why):
    track = _track(cur)
    _sonic(cur, track, _source(cur, track, NEAR, imported=True, duration=60))
    if why == "the switch off":
        stream.switch["on"] = False
    else:
        _instruments(cur, track, _source(cur, track, OTHER, imported=True, duration=60))
    stream.run(track)
    assert models.calls == [] and models.tagger.calls == 0      # not even decoded


def test_a_stream_with_the_switch_off_makes_its_sonic_records_and_no_instruments(
        cur, models, stream):
    track = _track(cur)
    stream.switch["on"] = False
    stream.run(track)
    assert models.tagger.calls == 0
    assert set(_records(cur, track)) == {"embeddings", "features"}


def test_a_stream_held_for_its_sonic_records_but_not_its_instruments_is_tagged(
        cur, models, stream):
    """Per kind: a lossless stream's sonic records held at a better copy of
    the material leave a stream of it its instruments to add."""
    track = _track(cur)
    owned = _source(cur, track, NEAR, duration=60)       # the owned file's row, never tagged
    _sonic(cur, track, owned)
    stream.run(track)
    assert models.tagger.calls == 1
    assert _records(cur, track)["embeddings"] == owned


# -- The per-track pipeline ------------------------------------------------------

@pytest.mark.parametrize("switch", [True, False])
def test_the_per_track_pipeline_runs_the_sonic_pass_then_the_instruments(
        cur, prints, models, monkeypatch, switch):
    import track_enrichment
    monkeypatch.setattr(settings_router, "instruments_on", lambda: switch)
    _on_cpu(monkeypatch)
    track = _track(cur)
    _file(cur, track, "/music/a.flac")
    prints["/music/a.flac"] = FP
    _track(cur, "a phantom, no file")
    stats = track_enrichment.TrackEnrichmentPipeline(skip_lastfm=True).enrich_tracks()
    assert stats["processed"] == 1               # the file's track alone, never a phantom
    assert (stats["sonic_success"], stats["instruments_success"]) == (1, int(switch))
    decodes = _decodes(models)
    assert decodes == [("sonic", "a.flac")] + ([("instrument", "a.flac")] if switch else [])
    assert models.wakes == ["sonic pass"] + (["instrument pass"] if switch else [])
    assert set(_records(cur, track)) == ({"embeddings", "features", "instruments"} if switch
                                         else {"embeddings", "features"})


# -- Review round 1 -------------------------------------------------------------

def _three(cur, prints, names=("a", "b", "c"), fixed=False):
    """Three owned tracks, a file each, in the queue's order when `fixed`."""
    tracks = {}
    for i, n in enumerate(names, 1):
        if fixed:
            tracks[n] = f"00000000-0000-4000-8000-00000000000{i}"
            cur.execute("INSERT INTO tracks (id, title) VALUES (%s, %s)", (tracks[n], n))
        else:
            tracks[n] = _track(cur, n)
        _file(cur, tracks[n], f"/music/{n}.flac")
        prints[f"/music/{n}.flac"] = encode(noise(950, ord(n[0]) + 7 * i))
    return tracks


@pytest.fixture
def one_a_batch(monkeypatch):
    import device
    monkeypatch.setattr(device, "audio_batch_budget_seconds", lambda **kw: 1.0)


def test_a_track_renamed_under_the_instrument_pass_fails_alone(cur, prints, models, one_a_batch):
    """Probe P1: the canon renamed a queued track (ON UPDATE CASCADE) while
    the batch before it was tagged; its registration's insert named a track
    that was gone and ended the whole pass. Now it fails alone."""
    tracks = _three(cur, prints, fixed=True)
    models.sonic()
    renamed = str(uuid.uuid4())

    def the_canon_renames_b(call):
        if call == 1:
            cur.execute("UPDATE tracks SET id = %s WHERE id = %s", (renamed, tracks["b"]))
    models.tagger.on_tag = the_canon_renames_b
    stats = models.instruments()
    assert (stats["success"], stats["failed"]) == (2, 1)
    assert "instruments" in _records(cur, tracks["c"])


def test_a_registration_that_raises_fails_its_track_alone_in_both_passes(cur, prints, models,
                                                                        monkeypatch, one_a_batch):
    """Probe P1b: the same error for one track — the sonic pass counted it
    failed and went on, the instrument pass ended."""
    tracks = _three(cur, prints, fixed=True)
    real = provenance.get_or_create_local

    def flaky(db, track_id, *a, **kw):
        if str(track_id) == tracks["b"]:
            raise RuntimeError("the analysis source of the track changed under two registrations")
        return real(db, track_id, *a, **kw)
    monkeypatch.setattr(provenance, "get_or_create_local", flaky)
    sonic = models.sonic()
    assert (sonic["success"], sonic["failed"]) == (2, 1)
    stats = models.instruments()
    assert (stats["success"], stats["failed"]) == (2, 1)
    assert [n for n in tracks if "instruments" in _records(cur, tracks[n])] == ["a", "c"]


def test_an_audio_half_that_failed_is_no_success_of_the_run(monkeypatch):
    monkeypatch.chdir(BACKEND)
    import main
    import track_enrichment
    monkeypatch.setattr(track_enrichment, "run_parallel_enrichment", lambda **kw: {
        "gpu": {"error": "ForeignKeyViolation"}, "text_embeddings": {"success": 2}})
    monkeypatch.setattr(main, "_notify_library_subs_safe", lambda: None)
    monkeypatch.setattr(main, "_enrich_state", {**main._enrich_state, "running": True})
    main._enrich_worker(None, True)
    assert main._enrich_state["result"]["success"] is False
    assert "not finished: audio analysis failed" in main._enrich_state["progress"]


def test_switched_off_under_way_the_instrument_pass_ends_at_its_next_seam(cur, prints, models,
                                                                         run):
    """Probe P2: off after the first track, the run tagged the other two."""
    _three(cur, prints)

    def the_owner_switches_off(call):
        if call == 1:
            run.switch["on"] = False
    models.tagger.on_tag = the_owner_switches_off
    result = run.go()
    assert models.tagger.calls == 1 and result["gpu"]["instruments"]["success"] == 1


def test_switched_on_during_the_sonic_pass_the_run_recognises_instruments(cur, prints, models,
                                                                          run):
    _three(cur, prints)
    run.switch["on"] = False
    models.on_clap = lambda: run.switch.update(on=True)
    result = run.go()
    assert result["gpu"]["instruments"]["success"] == 3


def test_a_forced_instrument_pass_is_the_owners_whatever_the_switch_says(cur, prints, models, run):
    _three(cur, prints)
    run.switch["on"] = False
    result = run.go(force_instruments=True)
    assert result["gpu"]["instruments"]["success"] == 3


def test_the_per_track_pipeline_reads_the_switch_for_every_track(cur, prints, models,
                                                                 monkeypatch):
    import track_enrichment
    switch = {"on": True}
    monkeypatch.setattr(settings_router, "instruments_on", lambda: switch["on"])
    _on_cpu(monkeypatch)
    tracks = _three(cur, prints)

    def the_owner_switches_off(call):
        switch["on"] = False
    models.tagger.on_tag = the_owner_switches_off
    stats = track_enrichment.TrackEnrichmentPipeline(skip_lastfm=True).enrich_tracks()
    assert (stats["sonic_success"], stats["instruments_success"]) == (3, 1)
    assert sum("instruments" in _records(cur, t) for t in tracks.values()) == 1


def test_a_legacy_mean_of_a_better_copy_is_replaced_once(cur, prints, models):
    """Probe P3: the CD's portrait of before 2026-07-05 (no segments), its
    file gone, a 24-bit copy elected: computed and refused on every run. A
    portrait without its segments outranks nothing."""
    track = _track(cur)
    cd = _source(cur, track, FP, bit_depth=16)
    _embedding(cur, track, cd, segments=False)
    _features(cur, track, cd)
    _file(cur, track, "/music/hr.flac", bit_depth=24)
    prints["/music/hr.flac"] = NEAR
    stats = models.sonic([uuid.UUID(track)])
    assert (stats["embeddings"], stats["features"]) == (1, 0)
    assert _queued(track)["embeddings"] is False
    cur.execute("SELECT count(*) FROM embedding_segments s JOIN embeddings e ON e.id = s.embedding_id "
                "WHERE e.track_id = %s", (track,))
    assert cur.fetchone() == (1,)
    assert models.sonic([uuid.UUID(track)])["processed"] == 0
    assert sum(1 for c in models.calls if c[0] == "CLAP") == 1


@pytest.mark.parametrize("switch", [False, True])
def test_a_stream_of_a_phantom_with_imported_segments_makes_its_features(cur, models, stream,
                                                                         switch):
    """Probe P4: after the records' cutover a phantom holds imported segments
    and no features; its stream — maybe its one full listen — made none.
    Per kind now: librosa alone, the tagger too with the switch on."""
    track = _track(cur)
    twin = _source(cur, track, NEAR, imported=True, duration=60)
    _embedding(cur, track, twin)
    stream.switch["on"] = switch
    stream.run(track)
    records = _records(cur, track)
    assert records["embeddings"] == twin
    assert "features" in records and records["features"] != twin
    assert ("CLAP", None) not in models.calls
    assert models.tagger.calls == int(switch) and ("instruments" in records) is switch


def test_a_stream_of_a_phantom_with_a_legacy_mean_makes_its_segments(cur, models, stream):
    track = _track(cur)
    own = _source(cur, track, NEAR, provider="lossless-test", duration=60)
    eid = _embedding(cur, track, own, segments=False)
    _features(cur, track, own)
    stream.switch["on"] = False
    stream.run(track)
    cur.execute("SELECT count(*) FROM embedding_segments WHERE embedding_id = %s", (eid,))
    assert cur.fetchone() == (1,) and ("librosa", None) not in models.calls


def test_a_give_way_takes_only_the_new_files_and_tries_nothing_twice(cur, prints, models, run,
                                                                     monkeypatch):
    """Probe P6: every give-way read the whole sonic queue again, and the
    resumed instrument pass the whole instrument queue: three files no
    fingerprint addresses were fingerprinted 5-6 times in one run."""
    for n in ("a", "b"):
        _file(cur, _track(cur, n), f"/music/{n}.flac")
        prints[f"/music/{n}.flac"] = encode(noise(950, ord(n)))
    for n in ("x1", "x2", "x3"):
        _file(cur, _track(cur, n), f"/music/{n}.flac")
    asked = []
    real = provenance.chromaprint_file
    monkeypatch.setattr(provenance, "chromaprint_file",
                        lambda path, *a: (asked.append(path.rsplit("/", 1)[-1]), real(path, *a))[1])

    def a_scan_adds_files(call):
        if call in (1, 2):
            prints[f"/music/n{call}.flac"] = encode(noise(950, 100 + call))
            _file(cur, _track(cur, f"n{call}"), f"/music/n{call}.flac")
    models.tagger.on_tag = a_scan_adds_files
    result = run.go()
    assert {n: asked.count(f"{n}.flac") for n in ("x1", "x2", "x3")} == {"x1": 2, "x2": 2, "x3": 2}
    assert result["gpu"]["instruments"]["success"] == 4
    assert sorted(d for d in _decodes(models) if d[0] == "sonic") == [
        ("sonic", n) for n in ("a.flac", "b.flac", "n1.flac", "n2.flac")]


def test_the_give_way_asks_only_within_the_runs_scope(cur):
    """A run scoped to some tracks gives way to new files of those alone."""
    mine, analysed = _track(cur), _track(cur)
    _file(cur, _track(cur), "/music/elsewhere.flac")         # a new file outside the scope
    mf = _file(cur, analysed, "/music/a.flac")
    _sonic(cur, analysed, _source(cur, analysed, FP, media_file=mf))
    with database.get_db_context() as db:
        assert embeddings.sonic_work_since(db, 0)
        assert not embeddings.sonic_work_since(db, 0, [uuid.UUID(mine), uuid.UUID(analysed)])
    _file(cur, mine, "/music/b.flac")
    with database.get_db_context() as db:
        assert embeddings.sonic_work_since(db, 0, [uuid.UUID(mine), uuid.UUID(analysed)])


def test_the_paces_leave_out_the_seams_waits_and_the_tagger_load(cur, prints, models, one_a_batch):
    """Probe P7: three one-second seam waits gave the sonic pace 3 s of no
    work; the instrument pace must not take the tagger's load either."""
    _three(cur, prints)

    def a_stream_goes_first():
        time.sleep(1.0)
        return False
    models.tagger_load_seconds = 1.0
    models.sonic(cancel_flag=a_stream_goes_first)
    models.instruments(cancel_flag=a_stream_goes_first)
    cur.execute("SELECT pass::text, batches, wall_seconds FROM analysis_pace")
    rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    assert rows["sonic"][0] == 3 and rows["sonic"][1] < 0.5
    assert rows["instruments"][0] == 3 and rows["instruments"][1] < 0.5


def test_a_batch_of_features_alone_is_no_sonic_pace(cur, prints, models):
    track = _track(cur)
    mf = _file(cur, track, "/music/a.flac")
    prints["/music/a.flac"] = FP
    _embedding(cur, track, _source(cur, track, FP, media_file=mf))
    assert models.sonic([uuid.UUID(track)])["features"] == 1
    cur.execute("SELECT count(*) FROM analysis_pace")
    assert cur.fetchone() == (0,)


def test_the_first_seam_of_the_sonic_pass_holds_no_open_write(cur, prints, models, one_a_batch):
    """A seam may wait for another pass, which may write the same rows: the
    first batch's registrations are committed before it."""
    _three(cur, prints)
    seen = []

    def seam():
        cur.execute("SELECT count(*) FROM analysis_sources")
        seen.append(cur.fetchone()[0])
        return False
    models.sonic(cancel_flag=seam)
    assert seen[0] == 1


def test_the_running_mean_forgets_the_machine_it_was(cur, machine):
    with database.get_db_context() as db:
        analysis_pace._machine = lambda: ("cuda", 16)
        analysis_pace.record(db, analysis_pace.SONIC, 36000.0, 360000.0)     # 6 s a minute
        for _ in range(30):
            analysis_pace.record(db, analysis_pace.SONIC, 60.0, 3600.0)      # 1 s a minute
        assert analysis_pace.pace(db, analysis_pace.SONIC) < 1.5


@pytest.mark.parametrize("threads,ratio", [
    (1, 8.5), (2, 8.5), (4, 8.5 - (8.5 - 2.66) / 3), (8, 8.5 - 2 * (8.5 - 2.66) / 3),
    (16, 2.66), (64, 2.66),
])
def test_the_cpu_ratio_follows_the_taggers_threads(no_model, machine, threads, ratio):
    analysis_pace._tagger_threads = lambda: threads
    assert analysis_pace.tagger_to_sonic("cpu") == pytest.approx(ratio)
    assert analysis_pace.tagger_to_sonic("cuda") == analysis_pace.CUDA_RATIO
    assert analysis_pace.tagger_to_sonic("mps") is None


def test_the_tagger_threads_are_the_profiles(monkeypatch):
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("lite", cores=4))
    assert analysis_pace._tagger_threads() == 2                # the Pi: the 8.5 point
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("lite", cores=32))
    assert analysis_pace._tagger_threads() == 16


@pytest.mark.parametrize("device,memory,reserve,minutes", [
    ("cuda", 4.0, 0.0, 5),            # a lite card: the budget's floor
    ("mps", 5.3, 0.0, 5),             # an 8 GB Mac's working set: the floor
    ("cuda", 8.0, 0.0, (8.0 * 0.85 - 4.0) / 0.12),
    ("cuda", 8.0, 2.0, (8.0 * 0.85 - 4.0 - 2.0) / 0.12),
])
def test_the_batch_budget_on_small_devices_and_with_the_tagger_to_come(monkeypatch, device, memory,
                                                                      reserve, minutes):
    import device as device_module
    monkeypatch.delenv("SAUTIUM_AUDIO_BUDGET_MIN", raising=False)
    monkeypatch.setenv("SAUTIUM_ACCEL_MEMORY_GB", str(memory))
    monkeypatch.setattr(device_module, "get_device", lambda: device)
    assert device_module.audio_batch_budget_seconds(reserve_gb=reserve) == int(minutes * 60)


def test_the_switch_is_refused_without_the_ml_stack(cur, monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("lite", ml=False))
    with pytest.raises(HTTPException) as refused:
        asyncio.run(settings_router.set_instruments(settings_router.InstrumentsReq(enabled=True)))
    assert refused.value.status_code == 400
    cur.execute("SELECT count(*) FROM user_settings WHERE key = 'enrichment.instruments'")
    assert cur.fetchone() == (0,)


def test_an_estimate_that_fails_leaves_the_library_screen_standing(cur, monkeypatch):
    def broken(db, reanalyze):
        raise RuntimeError("the estimate's query failed")
    monkeypatch.setattr(analysis_pace, "estimate", broken)
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("full"))
    state = settings_router._instruments_state()
    assert state["enabled"] is True and state["tracks"] is None and state["seconds"] is None
