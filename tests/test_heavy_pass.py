"""One heavy model pass on the accelerator at a time (backend/device.py
heavy_pass) and the passes that take it: the analysis run's audio and text
halves, the background text half, a stream's analysis and its lyrics, and the
idle release of the stream analyser's instrument pair. On 2026-10-25 a 24 GB
Mac's backend held 32 GiB of Metal memory after one text pass alone; passes
that each size their work to the whole device must not share it. The models
are fakes; the lane, the loop, the run and the stream's jobs are the real
ones."""

import shutil
import sys
import threading
import time
import types
import uuid
from pathlib import Path

import numpy as np
import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import device  # noqa: E402

WAIT = 10   # seconds that bound every wait of these tests

AUDIO = "the analysis run's audio half"
TEXT = "the analysis run's text half"


@pytest.fixture(autouse=True)
def lane(monkeypatch):
    """A lane of the test's own; the allocator's cache is counted, never asked
    of torch: the pass that held the accelerator at each return of it."""
    monkeypatch.setattr(device, "_lane", threading.Condition())
    monkeypatch.setattr(device, "_holder", None)
    monkeypatch.setattr(device, "_waiting", 0)
    monkeypatch.setattr(device, "_when_idle", [])
    freed = []
    monkeypatch.setattr(device, "empty_cache", lambda dev=None: freed.append(device._holder))
    return freed


def _until(cond):
    deadline = time.monotonic() + WAIT
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


class Pass(threading.Thread):
    """A heavy pass in a thread of its own: in the accelerator until `finish`."""

    def __init__(self, label):
        super().__init__(daemon=True)
        self.label = label
        self.inside, self.end = threading.Event(), threading.Event()

    def run(self):
        with device.heavy_pass(self.label):
            self.inside.set()
            assert self.end.wait(WAIT)

    def begin(self):
        self.start()
        assert self.inside.wait(WAIT)
        return self

    def finish(self):
        self.end.set()
        self.join(WAIT)
        assert not self.is_alive()


def test_a_pass_waits_until_the_one_that_holds_the_accelerator_has_ended(lane):
    first = Pass("first").begin()
    second = Pass("second")
    second.start()
    _until(device.heavy_pass_wanted)
    assert not second.inside.is_set()
    first.finish()
    assert second.inside.wait(WAIT)
    # the first one gave its cache back while the accelerator was still its own
    assert lane == ["first"]
    second.finish()
    assert lane == ["first", "second"] and device._holder is None


def test_a_pass_that_does_not_wait_is_turned_away_while_one_holds_the_accelerator(lane):
    holder = Pass("holder").begin()
    with device.heavy_pass("eager", wait=False) as held:
        assert held is False
    # nothing given back for a pass that never held it, and the holder keeps it
    assert lane == [] and device._holder == "holder"
    holder.finish()
    with device.heavy_pass("eager", wait=False) as held:
        assert held is True


def test_a_pass_that_waits_comes_before_one_that_does_not(monkeypatch):
    # the moment between a pass's end and the start of the one that waited for it
    monkeypatch.setattr(device, "_waiting", 1)
    with device.heavy_pass("eager", wait=False) as held:
        assert held is False
    assert device._holder is None


def test_the_end_of_a_pass_that_failed_gives_back_and_frees_the_accelerator(lane):
    with pytest.raises(RuntimeError):
        with device.heavy_pass("failing"):
            raise RuntimeError("a failed batch")
    assert lane == ["failing"] and device._holder is None
    with device.heavy_pass("next", wait=False) as held:
        assert held is True


def test_what_waits_for_an_idle_accelerator_runs_at_once_when_it_is():
    calls = []
    device.call_when_idle(lambda: calls.append("now"))
    assert calls == ["now"]


def test_what_waits_for_an_idle_accelerator_runs_once_after_the_last_pass():
    calls = []

    def note():
        calls.append(threading.current_thread().name)

    first = Pass("first").begin()
    second = Pass("second")
    second.start()
    _until(device.heavy_pass_wanted)
    device.call_when_idle(note)
    device.call_when_idle(note)         # asked again meanwhile
    first.finish()
    assert second.inside.wait(WAIT)
    assert calls == []                  # the pass that waited went first
    second.finish()
    assert calls == [second.name]       # once, at the end of the last pass, in its thread


# ── the background text half ───────────────────────────────────────────────

@pytest.fixture
def loop(monkeypatch):
    import background_enrichment as loop
    import hardware_profile
    monkeypatch.setattr(hardware_profile, "resolve",
                        lambda: types.SimpleNamespace(ml_available=True, name="full"))
    monkeypatch.setattr(loop, "_playback_hold", lambda: None)
    monkeypatch.setattr(loop, "_pass_wake", threading.Event())
    monkeypatch.setattr(loop, "_state", {**loop._state, "cancel": False,
                                         "total": dict(loop._state["total"])})
    return loop


def test_the_background_text_half_gives_way_to_a_busy_accelerator_and_follows_it(loop,
                                                                                  monkeypatch):
    ran = []
    monkeypatch.setattr(loop, "_LOCAL_MODEL_STEPS", (
        ("text_emb", lambda limit: ran.append(device._holder) or {"processed": 0}, 500),))
    run = Pass(AUDIO).begin()
    assert loop._run_local_model_steps() == ({"text_half": {"held": "accelerator"}}, False)
    assert ran == [] and not loop._pass_wake.is_set()
    run.finish()
    assert loop._pass_wake.is_set()     # woken by the run's end, not by the interval
    loop._run_local_model_steps()
    assert ran == ["the background text half"]


def test_a_pass_that_asks_for_the_accelerator_stops_the_text_half_at_its_next_batch(loop,
                                                                                    monkeypatch):
    run = Pass(AUDIO)
    asked = []

    def text_step(limit):
        run.start()                     # the run is asked for mid-step
        _until(device.heavy_pass_wanted)
        asked.append(loop._model_cancel())    # what a generator asks before each batch
        return {"processed": 1, "success": 1}

    later = []
    monkeypatch.setattr(loop, "_LOCAL_MODEL_STEPS", (
        ("text_emb", text_step, 500),
        ("lyrics_emb", lambda limit: later.append(limit) or {"processed": 0}, 200)))
    loop._run_local_model_steps()
    assert asked == [True] and later == []    # it stopped, and the next step did not begin
    assert run.inside.wait(WAIT)              # the run went in at the half's end
    assert not loop._pass_wake.is_set()
    run.finish()
    assert loop._pass_wake.is_set()


# ── the analysis run ───────────────────────────────────────────────────────

def test_the_analysis_run_holds_the_accelerator_for_each_of_its_halves(monkeypatch, lane):
    import audio_analysis
    import embeddings
    import enrichment_embeddings
    import hardware_profile
    import instrument_tagger
    import lyrics_embeddings
    import notary
    import text_embeddings
    import track_enrichment
    from canon import migrations

    seen = []

    def at(what):
        seen.append((what, device._holder))
        return {"success": 1}

    monkeypatch.setattr(hardware_profile, "resolve", lambda: types.SimpleNamespace(
        ml_available=True, unload_instruments_after_run=True))
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(migrations, "normalize_artists", lambda db, pass1: {})
    monkeypatch.setattr(notary, "wake", lambda *a, **kw: None)
    monkeypatch.setattr(embeddings, "AudioEmbeddingGenerator", lambda: types.SimpleNamespace(
        generate_embeddings=lambda **kw: at("embeddings")))
    monkeypatch.setattr(audio_analysis, "AudioAnalyzer", lambda: types.SimpleNamespace(
        analyze_all=lambda **kw: at("features")))
    monkeypatch.setattr(instrument_tagger, "release_instrument_tagger", lambda dev: at("AST+PaSST released"))
    monkeypatch.setattr(text_embeddings, "generate_text_embeddings", lambda **kw: at("titles"))
    monkeypatch.setattr(lyrics_embeddings, "generate_lyrics_embeddings", lambda **kw: at("lyrics"))
    monkeypatch.setattr(enrichment_embeddings, "generate_all_enrichment_embeddings",
                        lambda **kw: at("bios and genres"))
    track_enrichment.run_parallel_enrichment(skip_lastfm=True, skip_lyrics=True)
    assert seen == [("embeddings", AUDIO), ("features", AUDIO), ("AST+PaSST released", AUDIO),
                    ("titles", TEXT), ("lyrics", TEXT), ("bios and genres", TEXT)]
    assert lane == [AUDIO, TEXT] and device._holder is None


# ── a stream's analysis and its lyrics ─────────────────────────────────────

@pytest.fixture
def stream_db(scratch_dsn, monkeypatch):
    import database
    from sqlalchemy.orm import sessionmaker
    engine = database.make_engine(scratch_dsn)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    yield engine
    engine.dispose()


def test_a_streams_analysis_waits_for_the_accelerator_and_works_inside_it(stream_db,
                                                                         monkeypatch, lane):
    import librosa
    import provenance
    from streaming import enrichment

    seen = []

    class Models:
        def analyze_from_array(self, audio, sr):
            seen.append(("features", device._holder))

        def _compute_segments(self, audio):
            seen.append(("segments", device._holder))

        def _get_or_create_embedding_model(self, db):
            return None

    monkeypatch.setattr(librosa, "load",
                        lambda f, sr, mono: (np.zeros(48000 * 60, dtype=np.float32), sr))
    monkeypatch.setattr(provenance, "create_stream_source", lambda *a: 1)
    enricher = enrichment.PreviewEnricher()
    monkeypatch.setattr(enricher, "_models", lambda: (Models(), Models()))
    run = Pass(AUDIO).begin()
    job = threading.Thread(target=enricher._enrich, daemon=True,
                           args=(str(uuid.uuid4()), b"stream", (60.0,), True, "youtube"))
    job.start()
    _until(device.heavy_pass_wanted)
    assert seen == []                   # it waits: the track may never stream in full again
    run.finish()
    job.join(WAIT)
    assert not job.is_alive()
    assert seen == [("features", "a stream's analysis"), ("segments", "a stream's analysis")]
    assert lane == [AUDIO, "a stream's analysis"]


def test_a_streams_analyser_takes_the_instrument_pair_anew_for_every_job(monkeypatch):
    import instrument_tagger
    from streaming import enrichment
    monkeypatch.setattr(instrument_tagger, "get_instrument_tagger",
                        lambda dev: f"the pair as it is now on {dev}")
    enricher = enrichment.PreviewEnricher()
    enricher._embedder = object()
    # the run released the pair this analyser was given
    enricher._analyzer = types.SimpleNamespace(device="cpu", ensemble="a released pair")
    _, analyzer = enricher._models()
    assert analyzer.ensemble == "the pair as it is now on cpu"


def test_the_idle_release_of_the_instrument_pair_comes_after_the_accelerators_passes(
        monkeypatch, lane):
    import instrument_tagger
    from streaming import enrichment
    released = []
    monkeypatch.setattr(instrument_tagger, "release_instrument_tagger",
                        lambda dev: released.append(device._holder))
    enricher = enrichment.PreviewEnricher()
    enricher._analyzer = object()
    run = Pass(AUDIO).begin()            # the run tags with the same pair
    enricher._do_idle_unload()
    assert released == [] and enricher._analyzer is not None
    run.finish()
    assert released == ["the preview's idle release"] and enricher._analyzer is None


def test_the_idle_release_that_waited_leaves_the_pair_to_a_job_that_came_meanwhile(monkeypatch):
    import instrument_tagger
    from streaming import enrichment
    released = []
    monkeypatch.setattr(instrument_tagger, "release_instrument_tagger", released.append)
    enricher = enrichment.PreviewEnricher()
    enricher._analyzer = object()
    run = Pass(AUDIO).begin()
    enricher._do_idle_unload()
    enricher._inflight.add("a track streamed meanwhile")
    run.finish()
    assert released == [] and enricher._analyzer is not None


def test_a_streams_lyrics_are_embedded_as_a_heavy_pass(stream_db, monkeypatch, lane):
    import lyrics_embeddings
    from sqlalchemy import text
    from streaming import enrichment
    track = uuid.uuid4()
    with stream_db.begin() as conn:
        conn.execute(text("INSERT INTO tracks (id, title) VALUES (:id, 'demo-track')"), {"id": track})
        conn.execute(text("INSERT INTO track_lyrics (track_id, source, plain_lyrics) "
                          "VALUES (:id, 'lrclib', 'A synthetic line.')"), {"id": track})
    seen = []
    monkeypatch.setattr(lyrics_embeddings, "generate_lyrics_embeddings",
                        lambda track_ids: seen.append((track_ids, device._holder)))
    enrichment.PreviewLyricsEnricher()._enrich(str(track), "demo-track", "demo-artist", None, None)
    assert seen == [([track], "a stream's lyrics")]
    assert lane == ["a stream's lyrics"]
