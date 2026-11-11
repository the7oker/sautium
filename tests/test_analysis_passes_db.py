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
import logging
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
_REAL_LOAD_AUDIO = embeddings.AudioEmbeddingGenerator._load_audio


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
                               on_clap=None, tagger_load_seconds=0.0, unreadable=set(),
                               decode_seconds=0.0, clap_fails=set())

    def decoded(kind):
        def load(*args):
            name = next(a for a in args if isinstance(a, str)).rsplit("/", 1)[-1]
            calls.append((kind, name))
            time.sleep(ns.decode_seconds)
            if name in ns.unreadable:
                if kind == "sonic decode":
                    return None                     # _load_audio's answer to a failure
                raise ValueError(f"ffmpeg produced no samples for {name}")
            return silence
        return load

    monkeypatch.setattr(embeddings, "_reanalyze_imported", lambda: False)
    monkeypatch.setattr(notary, "wake", lambda reason, **kw: ns.wakes.append(reason))
    # any track fits: what this machine holds is the tests of the bound's own
    monkeypatch.setattr(analysis_pace, "available_memory_gb", lambda: 64.0)
    monkeypatch.setattr(gen, "load_model", lambda self: calls.append(("load", "CLAP")))
    monkeypatch.setattr(gen, "_load_audio", lambda self, *a: decoded("sonic decode")(*a))
    def clap(self, audio):
        calls.append(("CLAP", None))
        if ns.on_clap:
            ns.on_clap()
        if len([c for c in calls if c[0] == "CLAP"]) in ns.clap_fails:
            return None                             # what an out-of-memory forward answers
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
    monkeypatch.setattr(analysis_pace, "available_memory_gb", lambda: 64.0)   # any track fits
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
    monkeypatch.setattr(analysis_pace, "available_memory_gb", lambda: 64.0)   # any track fits
    monkeypatch.setattr(analysis_pace, "_machine", analysis_pace._machine)
    monkeypatch.setattr(analysis_pace, "_tagger_threads", analysis_pace._tagger_threads)


@pytest.mark.parametrize("name,cores,threads,pace", [
    ("lite", 4, 2, 18.9),                                          # the Pi 5 measured
    ("standard", 6, 4, 18.9 + (2.89 - 18.9) / 3),
    ("lite", 32, 16, 2.89),                                        # the 32-core x86 measured
])
def test_the_estimate_before_any_batch_is_the_taggers_measured_pace_at_this_machines_threads(
        cur, no_model, machine, monkeypatch, name, cores, threads, pace):
    """Round 2's Q2: the Pi's sonic pace scaled by the cores, times a ratio
    already chosen by the threads, counted the cores twice — a 32-core lite
    x86 read 0.73 s a minute against its measured 2.89."""
    _pending_files(cur, [240, 120, None])     # an unknown length counts as five minutes
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile(name, cores=cores))
    assert analysis_pace._tagger_threads() == threads
    analysis_pace._machine = lambda: ("cpu", cores)
    with database.get_db_context() as db:
        e = analysis_pace.estimate(db, reanalyze=False)
    assert (e["tracks"], e["audio_seconds"], e["basis"]) == (3, 660.0, "reference")
    assert e["pace"] == pytest.approx(pace)
    assert e["seconds"] == pytest.approx(11 * pace)


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
    loads = []
    monkeypatch.setattr(enricher, "_models", lambda sonic, tag: (
        loads.append((sonic, tag)), (enricher._embedder, analyzer))[1])
    return types.SimpleNamespace(
        run=lambda track, lengths=(60.0,), data=b"stream": enricher._enrich(
            track, data, lengths, True, "lossless-test"),
        switch=switch, loads=loads)


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


# -- What this node saves is unit vectors ----------------------------------------

def _off_unit(what: str):
    """What CLAP answers when it went wrong in one way: a segment or the
    portrait off the unit sphere, or not a number at all."""
    bad = {"a long segment": (np.stack([UNIT, 3 * UNIT]), UNIT),
           "a short segment": (np.stack([0.5 * UNIT]), UNIT),
           "the portrait": (np.stack([UNIT]), 1.5 * UNIT),
           "not a number": (np.stack([np.full(512, np.nan, dtype=np.float32)]), UNIT)}[what]
    return list(range(len(bad[0]))), bad[0], bad[1]


def _clap_answers(monkeypatch, models, answers: dict) -> None:
    """The n-th CLAP call (1-based) answers `answers[n]`; any other, the unit vector."""
    def clap(self, audio):
        models.calls.append(("CLAP", None))
        n = len([c for c in models.calls if c[0] == "CLAP"])
        return answers.get(n, ([0], np.stack([UNIT]), UNIT))
    monkeypatch.setattr(embeddings.AudioEmbeddingGenerator, "_compute_segments", clap)


def _segments(cur, track) -> int:
    cur.execute("SELECT count(*) FROM embedding_segments s JOIN embeddings e ON e.id = s.embedding_id "
                "WHERE e.track_id = %s", (track,))
    return cur.fetchone()[0]


@pytest.mark.parametrize("what", ["a long segment", "a short segment", "the portrait", "not a number"])
def test_the_sonic_pass_saves_no_embedding_of_a_track_whose_vectors_are_not_unit(
        cur, prints, models, monkeypatch, caplog, what):
    """The band the import gate holds a peer's bundle to, held to this node's
    own. The refused kind is the embedding alone: the track's features are
    not CLAP's and are saved, as when the forward fails; the rest of the
    batch saves; the pass counts the track and says so."""
    tracks = _three(cur, prints, fixed=True)
    _clap_answers(monkeypatch, models, {2: _off_unit(what)})
    with caplog.at_level(logging.ERROR, logger="embeddings"):
        stats = models.sonic()
    assert {k: stats[k] for k in ("processed", "success", "failed", "not_unit", "embeddings",
                                  "features")} == {"processed": 3, "success": 3, "failed": 0,
                                                   "not_unit": 1, "embeddings": 2, "features": 3}
    # whichever of the batch's three the second forward was (its decodes race)
    held = {n: set(_records(cur, track)) for n, track in tracks.items()}
    refused = [n for n in held if held[n] == {"features"}]
    assert len(refused) == 1 and all(held[n] == {"embeddings", "features"}
                                     for n in held if n not in refused), held
    bad = tracks[refused[0]]
    assert [_segments(cur, tracks[n]) for n in "abc"] == [0 if n in refused else 1 for n in "abc"]
    said = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    named = [m for m in said if "not unit vectors:" in m]
    assert len(named) == 1 and bad in named[0] and "model clap" in named[0] \
        and "segment norms" in named[0] and "portrait" in named[0], said
    assert any("1 track(s) kept no embedding" in m for m in said)
    assert models.wakes == ["sonic pass"]
    assert _queued(bad)["embeddings"] and not _queued(bad)["features"]

    # still in the queue: once the node analyses rightly again, it is saved
    _clap_answers(monkeypatch, models, {})
    again = models.sonic()
    assert (again["success"], again["failed"], again["not_unit"], again["embeddings"]) == (1, 0, 0, 1)
    assert set(_records(cur, bad)) == {"embeddings", "features"}


def test_a_batch_refused_whole_with_nothing_else_to_save_fails_whole_and_wakes_no_notary(
        cur, prints, models, monkeypatch):
    """Tracks that hold their features and lack only the embedding: refused,
    nothing is saved — failed, as a track whose forward failed with nothing
    else to save is — the notary is not woken, and the registrations stay
    for the run that analyses rightly."""
    tracks = _three(cur, prints, fixed=True)
    assert models.sonic()["success"] == 3
    cur.execute("DELETE FROM embeddings")
    cur.execute("SELECT array_agg(id ORDER BY id) FROM analysis_sources")
    registered = cur.fetchone()[0]
    models.wakes.clear()
    models.calls.clear()
    _clap_answers(monkeypatch, models, {n: _off_unit("the portrait") for n in (1, 2, 3)})
    stats = models.sonic()
    assert {k: stats[k] for k in ("processed", "success", "failed", "not_unit", "embeddings",
                                  "features")} == {"processed": 3, "success": 0, "failed": 3,
                                                   "not_unit": 3, "embeddings": 0, "features": 0}
    assert models.wakes == []
    assert all(set(_records(cur, t)) == {"features"} for t in tracks.values())
    _clap_answers(monkeypatch, models, {})
    again = models.sonic()
    assert (again["success"], again["failed"], again["not_unit"], again["embeddings"]) == (3, 0, 0, 3)
    cur.execute("SELECT array_agg(id ORDER BY id) FROM analysis_sources")
    assert cur.fetchone()[0] == registered


def test_a_forced_re_analysis_refused_leaves_the_old_embedding_and_its_segments(
        cur, prints, models, monkeypatch):
    track = _track(cur)
    _file(cur, track, "/music/a.flac")
    prints["/music/a.flac"] = FP
    assert models.sonic([uuid.UUID(track)])["success"] == 1

    def held():
        cur.execute("""SELECT e.id, e.updated_at, e.vector::text,
                              (SELECT array_agg(s.id ORDER BY s.id) FROM embedding_segments s
                                WHERE s.embedding_id = e.id),
                              (SELECT f.bpm FROM audio_features f WHERE f.track_id = e.track_id)
                         FROM embeddings e WHERE e.track_id = %s""", (track,))
        return cur.fetchall()
    before = held()
    assert len(before) == 1 and before[0][3]
    _clap_answers(monkeypatch, models, {1: _off_unit("a short segment")})
    models.calls.clear()
    stats = models.sonic([uuid.UUID(track)], force=True)
    assert (stats["not_unit"], stats["embeddings"], stats["failed"]) == (1, 0, 0)
    assert held() == before


def test_an_analysis_the_rank_declines_is_not_saved_and_not_checked(cur, models):
    """A lower-ranked copy's analysis is declined before the check: what is
    not saved is not this node's to refuse."""
    track = _track(cur)
    owned = _source(cur, track, FP)
    _embedding(cur, track, owned)
    lesser = _source(cur, track, NEAR, imported=True)
    generator = embeddings.AudioEmbeddingGenerator(model_name="clap", device="cpu")
    with database.get_db_context() as db:
        model = db.get(embeddings.EmbeddingModel, uuid.UUID(MODEL))
        assert generator._persist_analysis(db, track, model, *_off_unit("a long segment"), lesser) is False
        with pytest.raises(embeddings.NotUnitVectors):
            generator._persist_analysis(db, track, model, *_off_unit("a long segment"), owned)
    assert _records(cur, track)["embeddings"] == owned


def test_what_the_pass_refuses_to_save_is_what_the_import_gate_refuses(cur, models):
    """One function decides both (desktop/sync_client.is_unit): a vector at
    either edge of the band is saved here and taken there, one past it is
    neither."""
    from desktop import sync_client
    assert embeddings.is_unit is sync_client.is_unit
    low, high = sync_client.UNIT_NORM
    axis = np.zeros(512, dtype=np.float32)
    axis[0] = 1.0
    generator = embeddings.AudioEmbeddingGenerator(model_name="clap", device="cpu")
    for norm, unit in ((low + 1e-4, True), (high - 1e-4, True), (low - 1e-4, False), (high + 1e-4, False)):
        track = _track(cur)
        vector = (norm * axis).astype(np.float32)
        assert sync_client.is_unit(vector.tolist()) is unit
        with database.get_db_context() as db:
            model = db.get(embeddings.EmbeddingModel, uuid.UUID(MODEL))
            if unit:
                assert generator._persist_analysis(db, track, model, [0], np.stack([vector]), UNIT, None)
            else:
                with pytest.raises(embeddings.NotUnitVectors, match=f"track {track}.*model clap"):
                    generator._persist_analysis(db, track, model, [0], np.stack([vector]), UNIT, None)
        assert bool(_records(cur, track)) is unit


def test_the_vectors_the_encoders_own_chain_makes_pass_the_check(cur, monkeypatch):
    """The real _compute_segments — float32 unit rows from the forward, their
    normalized mean — over grids from one window to four hours and rows from
    nearly equal to nearly unrelated, into the real save: every vector
    inside the band by five orders, as computed and as stored."""
    from desktop import sync_client
    rng = np.random.default_rng(1)
    generator = embeddings.AudioEmbeddingGenerator(model_name="clap", device="cpu")
    spread = {"x": 1.0}

    def forward(windows):
        base = rng.standard_normal(512).astype(np.float32)
        rows = base + rng.standard_normal((len(windows), 512)).astype(np.float32) * spread["x"]
        return (rows / np.linalg.norm(rows, axis=1, keepdims=True)).astype(np.float32)
    monkeypatch.setattr(generator, "_generate_batch_embeddings", forward)
    second = np.zeros(48000, dtype=np.float32)
    worst = 0.0
    with database.get_db_context() as db:
        model = db.get(embeddings.EmbeddingModel, uuid.UUID(MODEL))
        for seconds in (10, 20, 35, 60, 240, 600, 3600, 4 * 3600):
            for spread["x"] in (0.01, 1.0, 30.0):
                audio = np.lib.stride_tricks.as_strided(second, shape=(48000 * seconds,), strides=(0,))
                idxs, vecs, portrait = generator._compute_segments(audio)
                assert vecs.dtype == portrait.dtype == np.float32
                for vector in list(vecs) + [portrait]:
                    values = vector.tolist()
                    assert sync_client.is_unit(values)
                    worst = max(worst, abs(math.sqrt(math.fsum(x * x for x in values)) - 1))
                assert generator._persist_analysis(db, _track(cur), model, idxs, vecs, portrait, None)
                db.commit()
    assert worst < 1e-6
    cur.execute("""SELECT (SELECT max(abs(vector_norm(vector) - 1)) FROM embedding_segments),
                          (SELECT max(abs(vector_norm(vector) - 1)) FROM embeddings),
                          (SELECT count(*) FROM embeddings)""")
    stored_segments, stored_portraits, saved = cur.fetchone()
    assert saved == 24 and stored_segments < 1e-6 and stored_portraits < 1e-6


@pytest.mark.parametrize("what", ["a long segment", "the portrait"])
def test_a_stream_whose_vectors_are_not_unit_keeps_its_other_records_and_no_embedding(
        cur, models, stream, monkeypatch, caplog, what):
    """The stream path saves through the same check, and the refused kind is
    the embedding alone: the stream may be the track's one full listen, so
    its features and instruments are saved and sealed, as when the forward
    fails. Streamed again and analysed rightly, the embedding joins them
    under the same registration."""
    track = _track(cur)
    _clap_answers(monkeypatch, models, {1: _off_unit(what)})
    from streaming import enrichment
    with caplog.at_level(logging.ERROR, logger=enrichment.__name__):
        stream.run(track)
    records = _records(cur, track)
    assert set(records) == {"features", "instruments"} and _segments(cur, track) == 0
    assert models.wakes == ["preview analysis"]
    said = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(said) == 1 and track in said[0] and "model clap" in said[0] \
        and "no embedding saved" in said[0], said
    cur.execute("SELECT id FROM analysis_sources WHERE track_id = %s", (track,))
    registered = [r[0] for r in cur.fetchall()]
    assert registered == [records["features"]] == [records["instruments"]]

    stream.run(track)                                       # the forward is right this time
    assert _records(cur, track) == dict(records, embeddings=registered[0])
    assert _segments(cur, track) == 1 and models.tagger.calls == 1
    assert models.wakes == ["preview analysis", "preview analysis"]
    cur.execute("SELECT id FROM analysis_sources WHERE track_id = %s", (track,))
    assert [r[0] for r in cur.fetchall()] == registered


def test_a_stream_whose_forward_fails_keeps_its_other_records_the_same_way(cur, models, stream):
    """What the refusal is counted as: CLAP answering nothing."""
    track = _track(cur)
    models.clap_fails = {1}
    stream.run(track)
    assert set(_records(cur, track)) == {"features", "instruments"}
    assert models.wakes == ["preview analysis"]


def test_the_cli_says_how_many_tracks_kept_no_embedding(cur, monkeypatch):
    from click.testing import CliRunner
    monkeypatch.chdir(BACKEND)
    import cli
    import track_enrichment
    monkeypatch.setattr(hardware_profile, "cap_torch_threads", lambda *a: None)
    counted = {"processed": 5, "success": 4, "failed": 1, "skipped": 0, "embeddings": 3,
               "features": 4, "too_long": 0, "not_unit": 2}
    monkeypatch.setattr(embeddings, "sonic_pass", lambda **_kw: dict(counted))
    said = CliRunner().invoke(cli.cli, ["sonic-pass"])
    assert said.exit_code == 0, said.output
    assert ("   • No embedding kept: 2 tracks — this node's own analysis made vectors that are "
            "not unit vectors; the log has the details") in said.output.splitlines()
    monkeypatch.setattr(embeddings, "sonic_pass", lambda **_kw: dict(counted, not_unit=0))
    assert "No embedding kept" not in CliRunner().invoke(cli.cli, ["sonic-pass"]).output

    monkeypatch.setattr(track_enrichment, "run_parallel_enrichment",
                        lambda **_kw: {"gpu": {"sonic": dict(counted)}})
    run = CliRunner().invoke(cli.cli, ["enrich-tracks"])
    assert run.exit_code == 0, run.output
    line = next(l for l in run.output.splitlines() if "Sonic pass:" in l)
    assert line == ("   • Sonic pass: 4 success, 1 failed, 2 kept no embedding (this node's own "
                    "analysis made vectors that are not unit vectors; the log has the details)")


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


# -- Review round 2 -------------------------------------------------------------

def _analysed(cur, prints, n):
    """`n` owned tracks whose sonic records are complete and whose
    instruments are none."""
    for i in range(n):
        t = _track(cur, f"t{i}")
        mf = _file(cur, t, f"/music/t{i}.flac")
        fp = encode(noise(950, 500 + i))
        prints[f"/music/t{i}.flac"] = fp
        _sonic(cur, t, _source(cur, t, fp, media_file=mf))


@pytest.mark.parametrize("skip_sonic", [False, True])
def test_switched_off_the_per_track_pipeline_selects_no_track_for_its_instruments(
        cur, prints, models, monkeypatch, skip_sonic):
    """Round 2's Q1: with the switch off every owned track lacking
    instruments was selected and given a scoped sonic pass for nothing."""
    import track_enrichment
    _analysed(cur, prints, 6)
    monkeypatch.setattr(settings_router, "instruments_on", lambda: False)
    _on_cpu(monkeypatch)
    stats = track_enrichment.TrackEnrichmentPipeline(skip_sonic=skip_sonic,
                                                     skip_lastfm=True).enrich_tracks()
    assert stats["processed"] == 0 and models.calls == []


def test_with_the_sonic_pass_skipped_and_the_switch_off_the_run_takes_no_lane(
        cur, prints, models, run, monkeypatch):
    import device
    taken = []
    real = device.heavy_pass
    monkeypatch.setattr(device, "heavy_pass", lambda name, **kw: (taken.append(name), real(name, **kw))[1])
    _analysed(cur, prints, 2)
    run.switch["on"] = False
    run.go(skip_sonic=True)
    assert "the analysis run's audio half" not in taken


def test_a_timed_run_never_gives_way(cur, prints, models, run):
    _three(cur, prints)
    prints["/music/late.flac"] = encode(noise(950, 999))

    def a_scan_adds_a_file(call):
        if call == 1:
            _file(cur, _track(cur, "late"), "/music/late.flac")
    models.tagger.on_tag = a_scan_adds_a_file
    run.go(max_duration_seconds=3600)
    assert ("sonic", "late.flac") not in _decodes(models)


@pytest.fixture
def one_batch(monkeypatch):
    import device
    monkeypatch.setattr(device, "audio_batch_budget_seconds", lambda **kw: 100000.0)


@pytest.mark.parametrize("which", ["sonic", "instruments"])
def test_a_batch_whose_decode_failed_records_no_pace(cur, prints, models, which, one_batch):
    _three(cur, prints)
    if which == "instruments":
        models.sonic()
        cur.execute("DELETE FROM analysis_pace")
    models.unreadable.add("b.flac")
    stats = getattr(models, which)()
    assert stats["failed"] == 1 and stats["success"] == 2
    cur.execute("SELECT count(*) FROM analysis_pace")
    assert cur.fetchone() == (0,)


def test_a_batch_whose_tagging_failed_records_no_instruments_pace(cur, prints, models, one_batch):
    _three(cur, prints)
    models.sonic()
    cur.execute("DELETE FROM analysis_pace")

    def the_second_fails(call):
        if call == 2:
            raise RuntimeError("the tagger failed on this track")
    models.tagger.on_tag = the_second_fails
    stats = models.instruments()
    assert (stats["success"], stats["failed"]) == (2, 1)
    cur.execute("SELECT count(*) FROM analysis_pace")
    assert cur.fetchone() == (0,)


def test_a_forward_out_of_memory_fails_its_track_alone(cur, prints, models, one_a_batch):
    """A refused CLAP forward answers None (embeddings._generate_batch_
    embeddings catches the out-of-memory): that track fails, its features
    stand, the others go on, and its batch is no pace."""
    tracks = _three(cur, prints, fixed=True)
    models.clap_fails.add(2)
    stats = models.sonic()
    assert (stats["success"], stats["embeddings"], stats["features"]) == (3, 2, 3)
    assert "embeddings" not in _records(cur, tracks["b"])
    cur.execute("SELECT batches FROM analysis_pace WHERE pass = 'sonic'")
    assert cur.fetchone() == (2,)


def test_the_sonic_pace_keeps_the_decode_under_way_beside_a_seam(cur, prints, models, one_a_batch):
    """Round 2: subtracting a seam's whole wait also took out the batch's
    decode, under way beside it in the prefetch."""
    _three(cur, prints)
    models.decode_seconds = 0.6

    def a_stream_goes_first():
        time.sleep(0.6)
        return False
    models.sonic(cancel_flag=a_stream_goes_first)
    cur.execute("SELECT batches, wall_seconds FROM analysis_pace WHERE pass = 'sonic'")
    batches, wall = cur.fetchone()
    assert batches == 3 and wall > 1.2


def test_a_stream_making_features_alone_loads_no_clap(cur, models, stream):
    track = _track(cur)
    _embedding(cur, track, _source(cur, track, NEAR, imported=True, duration=60))
    stream.switch["on"] = False
    stream.run(track)
    assert stream.loads == [(False, False)]
    assert "features" in _records(cur, track)


# -- The longest track this machine can hold -------------------------------------

def test_a_track_longer_than_this_machine_holds_is_left_out_and_counted(cur, prints, models,
                                                                       monkeypatch):
    """Round 2's R2.H1: the decode and its resample grow with a track's
    length, so past what this machine holds now a track is in no queue — by
    its known duration, never decoded — and counted; nothing is deleted."""
    short, long_ = _track(cur, "short"), _track(cur, "long")
    _file(cur, short, "/music/short.flac", duration=120)
    _file(cur, long_, "/music/long.flac", duration=2700)
    prints.update({"/music/short.flac": FP, "/music/long.flac": OTHER})
    monkeypatch.setattr(analysis_pace, "longest_track_seconds", lambda kinds: 600.0)
    sonic = models.sonic()
    assert (sonic["success"], sonic["too_long"]) == (1, 1)
    instruments = models.instruments()
    assert (instruments["success"], instruments["too_long"]) == (1, 1)
    assert [c for c in models.calls if c[1] == "long.flac"] == []
    with database.get_db_context() as db:
        e = analysis_pace.estimate(db, reanalyze=False)
    assert (e["tracks"], e["too_long"]) == (0, 1)


def _room(available, reserve, forward):
    return (available * 0.85 - reserve) * 1e3 / 1.048576 - 64 - forward


@pytest.mark.parametrize("name,available,resident,kind,minutes", [
    ("lite", 8.0, {"clap", "text"}, "sonic", _room(8.0, 0.0, 620) / 22.0),
    # the tagger and the text encoder still to load
    ("lite", 6.0, {"clap"}, "instruments", _room(6.0, 1.0 + 2.1, 1800) / 30.0),
    # standard lazy-loads the translator too
    ("standard", 10.0, set(), "sonic", _room(10.0, 1.0 + 2.1 + 3.0, 620) / 22.0),
    ("lite", 1.0, {"clap", "tagger", "text"}, "instruments", 0.0),     # no room: no track
])
def test_the_longest_track_follows_what_is_available(monkeypatch, name, available, resident,
                                                     kind, minutes):
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile(name))
    monkeypatch.setattr(analysis_pace, "available_memory_gb", lambda: available)
    monkeypatch.setattr(analysis_pace, "resident", lambda model: model in resident)
    assert analysis_pace.longest_track_seconds([kind]) == pytest.approx(max(0.0, minutes) * 60)
    both = analysis_pace.longest_track_seconds([analysis_pace.SONIC, analysis_pace.INSTRUMENTS])
    assert both <= analysis_pace.longest_track_seconds([analysis_pace.INSTRUMENTS])


def test_a_containers_limit_bounds_what_is_available(monkeypatch, tmp_path):
    (tmp_path / "max").write_text("4294967296\n")
    (tmp_path / "current").write_text("3221225472\n")
    (tmp_path / "stat").write_text("anon 1\ninactive_file 1073741824\n")
    monkeypatch.setattr(analysis_pace, "_CGROUPS", (
        (str(tmp_path / "max"), str(tmp_path / "current"), str(tmp_path / "stat"), "inactive_file"),))
    assert analysis_pace._cgroup_room_bytes() == 2 * 1073741824     # the dropped cache is room
    assert analysis_pace.available_memory_gb() <= 2 * 1073741824 / 1e9
    (tmp_path / "max").write_text("max\n")
    assert analysis_pace._cgroup_room_bytes() is None


def _flac(seconds, rate, subtype, channels=2) -> bytes:
    import io
    import soundfile
    out = io.BytesIO()
    soundfile.write(out, np.zeros((int(seconds * rate), channels), dtype=np.float32), rate,
                    subtype=subtype, format="FLAC")
    return out.getvalue()


@pytest.mark.parametrize("data,length,decoded", [
    (b"stream", 20 * 60, True),
    (b"stream", 25 * 60, False),
    (None, 90 * 60, True),              # a 10 s header over a catalogue length that lies
])
def test_a_stream_longer_than_this_machine_holds_is_not_analysed(cur, models, stream, monkeypatch,
                                                                 data, length, decoded):
    """Round 3's R3.L8: at 1.2 GB the bound was 0 and the test passed
    whatever the bound said. At 2 GB, every model resident, the sonic pass
    has room for 937 MiB; a stream whose header cannot be read counts as
    stereo at 48 kHz, 43.9 MiB a minute: 20 minutes fit, 25 do not."""
    monkeypatch.setattr(analysis_pace, "available_memory_gb", lambda: 2.0)
    monkeypatch.setattr(analysis_pace, "resident", lambda model: True)
    track = _track(cur)
    stream.switch["on"] = False
    stream.run(track, lengths=(float(length),), data=data or _flac(10, 44100, "PCM_16"))
    assert (("stream decode", None) in models.calls) is decoded


def test_the_guidance_trail_leaves_out_a_track_past_reach(cur, monkeypatch):
    monkeypatch.chdir(BACKEND)
    import main  # noqa: F401
    track = _track(cur)
    _file(cur, track, "/music/a.flac", duration=7200)
    monkeypatch.setattr(analysis_pace, "available_memory_gb", lambda: 64.0)
    assert settings_router._analysis_pending() is True
    monkeypatch.setattr(analysis_pace, "available_memory_gb", lambda: 1.5)
    assert settings_router._analysis_pending() is False


def test_the_cli_caps_torch_at_the_profiles_threads(monkeypatch):
    import torch
    capped = []
    monkeypatch.setattr(torch, "set_num_threads", capped.append)
    assert hardware_profile.cap_torch_threads(_profile("lite", cores=8)) == 4
    assert hardware_profile.cap_torch_threads(_profile("full", device="cuda")) is None
    assert capped == [4]


def test_enrich_tracks_says_and_exits_on_a_failed_audio_half(monkeypatch):
    from click.testing import CliRunner
    monkeypatch.chdir(BACKEND)
    import cli
    import track_enrichment
    monkeypatch.setattr(hardware_profile, "cap_torch_threads", lambda *a: None)
    monkeypatch.setattr(track_enrichment, "run_parallel_enrichment",
                        lambda **kw: {"gpu": {"error": "ForeignKeyViolation"}})
    result = CliRunner().invoke(cli.cli, ["enrich-tracks", "--skip-lastfm", "--skip-lyrics"])
    assert result.exit_code == 1 and "The audio analysis failed" in result.output
    assert "complete!" not in result.output


def test_a_registration_failure_is_logged_with_its_traceback(cur, prints, models, monkeypatch,
                                                             caplog, one_a_batch):
    tracks = _three(cur, prints, fixed=True)
    real = provenance.get_or_create_local

    def flaky(db, track_id, *a, **kw):
        if str(track_id) == tracks["b"]:
            raise RuntimeError("two registrations")
        return real(db, track_id, *a, **kw)
    monkeypatch.setattr(provenance, "get_or_create_local", flaky)
    models.sonic()
    models.instruments()
    failed = [r for r in caplog.records if r.getMessage().startswith("Registration failed")]
    assert len(failed) == 2 and all(r.exc_info and r.levelname == "ERROR" for r in failed)


# -- Review round 3 ---------------------------------------------------------------

@pytest.mark.parametrize("rate,subtype,per_minute,measured", [
    (44100, "PCM_16", 41.27, 34.0),     # P4: 33-34 MiB a minute held
    (96000, "PCM_24", 76.91, 66.0),
])
def test_a_streams_need_is_its_own_decode_read_off_its_header(rate, subtype, per_minute,
                                                              measured):
    """Round 3, the reviewer's P4: a stream is decoded at its native rate and
    channels as float32 (librosa.load), 3x (CD) to 6x (hi-res) what a file's
    48 kHz mono decode holds; its header says how long it is whatever the
    catalogue says."""
    data = _flac(30, rate, subtype)
    fits, need, room = analysis_pace.stream_fits(data, (5400.0,), [analysis_pace.SONIC])
    bytes_mb = len(data) / 2 ** 20
    assert (need - bytes_mb) * 2 == pytest.approx(per_minute, abs=0.05)
    assert per_minute > measured
    _, unread, _ = analysis_pace.stream_fits(b"stream", (5400.0,), [analysis_pace.SONIC])
    # a header it cannot read: stereo at 48 kHz over the catalogue's 90 minutes
    assert unread == pytest.approx(len(b"stream") / 2 ** 20 + 5400 * 48000 * 16 / 2 ** 20)


def test_a_resident_model_is_not_reserved(monkeypatch):
    """Round 3, the reviewer's P3: each pass read "a model still to load" off
    its own fresh instance, so CLAP, resident since the first run, was
    reserved by every later one. Residency is the process-wide caches'."""
    import sys
    import clap_model
    import instrument_tagger
    import model_cache
    text_embedder = pytest.importorskip("text_embedder")
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("standard"))
    monkeypatch.setattr(analysis_pace, "_machine", lambda: ("cpu", 4))
    sonic, instruments = [analysis_pace.SONIC], [analysis_pace.INSTRUMENTS]
    assert analysis_pace.reserve_gb(sonic) == pytest.approx(1.0 + 2.1 + 3.0)
    monkeypatch.setitem(clap_model._cache, ("clap", "cpu"), (None, None))
    monkeypatch.setitem(text_embedder._cache, ("bge", "cpu"), None)
    assert analysis_pace.reserve_gb(sonic) == pytest.approx(3.0)
    monkeypatch.setitem(model_cache._cache, "translate", None)
    assert analysis_pace.reserve_gb(sonic) == 0.0
    assert analysis_pace.reserve_gb(instruments) == pytest.approx(1.0)
    monkeypatch.setitem(instrument_tagger._cache, "cpu", None)
    assert analysis_pace.reserve_gb(instruments) == 0.0
    monkeypatch.delitem(sys.modules, "instrument_tagger")      # never imported: nothing held
    assert not analysis_pace.resident("tagger")
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("lite"))
    monkeypatch.delitem(model_cache._cache, "translate")
    assert analysis_pace.reserve_gb(sonic) == 0.0               # lite never loads the translator


@pytest.mark.parametrize("clap_resident", [True, False])
def test_with_clap_resident_a_small_machine_analyses_a_four_minute_track(
        cur, prints, models, monkeypatch, clap_resident):
    """The reviewer's P8: at 1.2 GB available with CLAP (and the text
    encoder) resident, a 4-minute track was left out — CLAP reserved again
    by the pass's new instance. Resident, it is not reserved: room for 13
    minutes; still to load, no room at all."""
    import clap_model
    text_embedder = pytest.importorskip("text_embedder")
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("lite"))
    monkeypatch.setitem(text_embedder._cache, ("bge", "cpu"), None)
    if clap_resident:
        monkeypatch.setitem(clap_model._cache, ("clap", "cpu"), (None, None))
    monkeypatch.setattr(analysis_pace, "available_memory_gb", lambda: 1.2)
    _file(cur, _track(cur, "a"), "/music/a.flac", duration=240)
    prints["/music/a.flac"] = FP
    stats = models.sonic()
    assert (stats["success"], stats["too_long"]) == ((1, 0) if clap_resident else (0, 1))


@pytest.mark.parametrize("which,available", [("sonic", 3.0), ("instruments", 3.0)])
def test_a_bound_that_falls_under_a_pass_leaves_the_long_track_undecoded(
        cur, prints, models, one_a_batch, monkeypatch, which, available):
    """Round 3, the reviewer's P2: the bound was read once, at the queue read;
    what is available fell after it (a model loaded beside the pass) and the
    90-minute track was decoded anyway. It is read again as each batch is
    planned."""
    reads = []

    def falls():
        reads.append(1)
        return 64.0 if len(reads) == 1 else available
    monkeypatch.setattr(analysis_pace, "resident", lambda model: True)
    monkeypatch.setattr(analysis_pace, "available_memory_gb", falls)
    _file(cur, _track(cur, "a"), "/music/a.flac", duration=240)
    prints["/music/a.flac"] = FP
    _file(cur, _track(cur, "long"), "/music/long.flac", duration=5400)
    prints["/music/long.flac"] = OTHER
    stats = getattr(models, which)()
    assert [n for _k, n in models.calls if _k.endswith("decode")] == ["a.flac"]
    assert (stats["success"], stats["too_long"]) == (1, 1)


@pytest.mark.parametrize("which", ["sonic", "instruments"])
def test_a_decode_past_the_admitted_bound_is_too_long(cur, prints, models, one_a_batch,
                                                      monkeypatch, which):
    """The note: a duration a tag under-states let the decode hold more
    than the bound; the decode stops there and the track is too long."""
    real = audio_analysis.TrackTooLong

    def stops(*args):
        if any(str(a).endswith("/a.flac") for a in args):
            raise real("/music/a.flac decodes past 10 min")
        return np.zeros(48000 * 20, dtype=np.float32)
    monkeypatch.setattr(embeddings, "load_full_track_48k", stops)
    monkeypatch.setattr(audio_analysis, "load_full_track_48k", stops)
    monkeypatch.setattr(embeddings.AudioEmbeddingGenerator, "_load_audio", _REAL_LOAD_AUDIO)
    _three(cur, prints)
    stats = getattr(models, which)()
    assert (stats["success"], stats["too_long"], stats["failed"]) == (2, 1, 0)
    cur.execute("SELECT batches FROM analysis_pace WHERE pass = %s", (which,))
    assert cur.fetchone() == (2,)                   # the stopped decode's batch is no pace


def test_a_track_past_reach_is_counted_once_in_a_run_that_gave_way(cur, prints, models, run,
                                                                   monkeypatch):
    """R3.L1, the reviewer's P1: each pass of a run that gave way counted the
    track past reach again — 2 for 1."""
    monkeypatch.setattr(analysis_pace, "longest_track_seconds", lambda kinds: 600.0)
    for name in ("a", "b"):
        _file(cur, _track(cur, name), f"/music/{name}.flac")
        prints[f"/music/{name}.flac"] = encode(noise(950, ord(name)))
    _file(cur, _track(cur, "long"), "/music/long.flac", duration=2700)
    prints["/music/long.flac"] = encode(noise(950, 77))
    prints["/music/c.flac"] = encode(noise(950, ord("c")))

    def a_scan_adds_a_file(call):
        if call == 1:
            _file(cur, _track(cur, "c"), "/music/c.flac")
    models.tagger.on_tag = a_scan_adds_a_file
    gpu = run.go()["gpu"]
    assert ("sonic", "c.flac") in _decodes(models)              # it gave way
    assert ("sonic", "long.flac") not in _decodes(models)
    assert gpu["sonic"]["too_long"] == 1 and gpu["instruments"]["too_long"] == 1


def test_the_per_track_pipeline_leaves_out_a_track_past_reach(cur, prints, models, monkeypatch):
    """R3.L6: the pipeline's pre-filter selected a track the passes then
    left out, so it ran a scoped pass for nothing."""
    import track_enrichment
    monkeypatch.setattr(analysis_pace, "longest_track_seconds", lambda kinds: 600.0)
    monkeypatch.setattr(settings_router, "instruments_on", lambda: True)
    _on_cpu(monkeypatch)
    _file(cur, _track(cur, "long"), "/music/long.flac", duration=2700)
    prints["/music/long.flac"] = OTHER
    stats = track_enrichment.TrackEnrichmentPipeline(skip_lastfm=True).enrich_tracks()
    assert stats["processed"] == 0 and models.calls == []


def test_a_failed_sample_of_the_sonic_pass_is_logged(monkeypatch, caplog):
    """R3.L9: the CLI's sample was caught and ignored."""
    from click.testing import CliRunner
    monkeypatch.chdir(BACKEND)
    import cli

    def unreadable():
        raise RuntimeError("the database went away")
    monkeypatch.setattr(hardware_profile, "cap_torch_threads", lambda *a: None)
    monkeypatch.setattr(embeddings, "sonic_pass", lambda **_kw: {
        "processed": 1, "success": 1, "failed": 0, "skipped": 0, "embeddings": 1, "features": 1,
        "too_long": 0})
    monkeypatch.setattr(cli, "get_db_context", unreadable)
    result = CliRunner().invoke(cli.cli, ["sonic-pass"])
    assert result.exit_code == 0, result.output
    logged = [r for r in caplog.records if "could not be shown" in r.getMessage()]
    assert len(logged) == 1 and logged[0].levelname == "WARNING" and logged[0].exc_info


# -- The decode ----------------------------------------------------------------------

@pytest.fixture
def flac_file(tmp_path):
    import shutil
    if not shutil.which("ffmpeg"):
        pytest.skip("no ffmpeg")
    path = tmp_path / "a.flac"
    path.write_bytes(_flac(20, 44100, "PCM_16"))
    return str(path)


def _children():
    import psutil
    return [p for p in psutil.Process().children() if p.status() != psutil.STATUS_ZOMBIE]


def test_a_decode_stops_once_its_samples_pass_the_bound_and_reaps_ffmpeg(flac_file):
    """The note and R3.L5: a duration a tag under-states (5 s of a 20 s
    file) grew the buffer to twice the bound; the read now stops past the
    admitted 10 s, kills ffmpeg and waits for it — from a `finally`, so a
    read that raises leaves no decoder behind."""
    with pytest.raises(audio_analysis.TrackTooLong):
        audio_analysis.load_full_track_48k(flac_file, duration_seconds=5, max_seconds=10)
    assert _children() == []
    whole = audio_analysis.load_full_track_48k(flac_file, duration_seconds=5, max_seconds=30)
    assert len(whole) == 20 * 48000


@pytest.mark.parametrize("duration,allowed", [(None, 1.0), (8.0, 3.0)])
def test_the_decode_deadline_grows_with_the_track_and_a_kill_is_logged(
        flac_file, monkeypatch, caplog, duration, allowed):
    """R3.L4: the deadline was five minutes for every track; it is five
    minutes and a quarter of the known length, and a decode killed at it
    says so."""
    import subprocess
    real = subprocess.Popen
    monkeypatch.setattr(audio_analysis, "DECODE_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(audio_analysis.subprocess, "Popen",
                        lambda cmd, **kw: real(["sleep", "30"], **kw))
    began = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        audio_analysis.load_full_track_48k(flac_file, duration_seconds=duration)
    assert allowed - 0.2 < time.monotonic() - began < allowed + 2
    assert any("passed its deadline" in r.getMessage() and r.levelname == "WARNING"
               for r in caplog.records)
    assert _children() == []
