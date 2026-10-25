"""One heavy model pass on the accelerator at a time (backend/device.py
heavy_pass) and the passes that take it: the analysis run's audio and text
halves, the background text half, a stream's analysis and its lyrics, and the
idle release of the stream analyser's instrument pair. On 2026-10-25 a 24 GB
Mac's backend held 32 GiB of Metal memory after one text pass alone; passes
that each size their work to the whole device must not share it. The passes
wait their turn in the order they asked, the run gives way at its batch seams,
and a stopping node tells whatever waits. The models are fakes; the lane, the
loop, the run and the stream's jobs are the real ones."""

import logging
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
    """A lane of the test's own and no subscriber of another module's; the
    allocator's cache is counted, never asked of torch: the pass that held
    the accelerator at each return of it."""
    monkeypatch.setattr(device, "_lane", device._Lane())
    monkeypatch.setattr(device, "_at_end", [])
    freed = []
    monkeypatch.setattr(device, "empty_cache", lambda dev=None: freed.append(_holder()))
    return freed


def _holder():
    return device._lane.holder


def _waiting():
    return len(device._lane.queue)


def _until(cond):
    deadline = time.monotonic() + WAIT
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


class Pass(threading.Thread):
    """A heavy pass in a thread of its own: in the accelerator until `finish`,
    or out at once when it was not let in."""

    def __init__(self, label):
        super().__init__(daemon=True)
        self.label, self.held = label, None
        self.inside, self.end = threading.Event(), threading.Event()

    def run(self):
        with device.heavy_pass(self.label) as held:
            self.held = held
            self.inside.set()
            if held:
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
    assert lane == ["first", "second"] and _holder() is None


def test_the_passes_that_wait_go_in_the_order_they_asked():
    holder = Pass("holder").begin()
    first, second = Pass("first"), Pass("second")
    first.start()
    _until(lambda: _waiting() == 1)
    second.start()
    _until(lambda: _waiting() == 2)
    holder.finish()
    assert first.inside.wait(WAIT) and not second.inside.is_set()
    first.finish()
    assert second.inside.wait(WAIT)
    second.finish()


def test_a_pass_that_does_not_wait_is_turned_away_while_one_holds_the_accelerator(lane):
    holder = Pass("holder").begin()
    with device.heavy_pass("eager", wait=False) as held:
        assert held is False
    # nothing given back for a pass that never held it, and the holder keeps it
    assert lane == [] and _holder() == "holder"
    holder.finish()
    with device.heavy_pass("eager", wait=False) as held:
        assert held is True


def test_a_pass_that_waits_comes_before_one_that_does_not():
    # the moment between a pass's end and the start of the one that waited for it
    device._lane.queue.append(object())
    with device.heavy_pass("eager", wait=False) as held:
        assert held is False
    assert _holder() is None


def test_the_end_of_a_pass_that_failed_gives_back_and_frees_the_accelerator(lane):
    with pytest.raises(RuntimeError):
        with device.heavy_pass("failing"):
            raise RuntimeError("a failed batch")
    assert lane == ["failing"] and _holder() is None
    with device.heavy_pass("next", wait=False) as held:
        assert held is True


def test_a_thread_that_holds_the_accelerator_cannot_take_it_again():
    with device.heavy_pass("outer"):
        with pytest.raises(RuntimeError, match="holds the accelerator already"):
            with device.heavy_pass("inner"):
                pass
        assert _holder() == "outer"
    assert _holder() is None


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


def test_a_subscriber_that_fails_is_said_and_keeps_neither_the_pass_nor_the_others(caplog):
    caplog.set_level(logging.ERROR, logger="device")
    calls = []

    def fails():
        raise ValueError("a subscriber's own failure")

    device.at_pass_end(fails)
    device.at_pass_end(lambda: calls.append("at the end"))
    with device.heavy_pass("a pass"):
        device.call_when_idle(fails)
        device.call_when_idle(lambda: calls.append("idle"))
    assert calls == ["at the end", "idle"] and _holder() is None
    said = [r for r in caplog.records if r.name == "device"]
    assert len(said) == 2 and all(r.exc_info for r in said)


def test_a_pass_with_nothing_waiting_gives_nothing_at_its_seam(lane):
    with device.heavy_pass("long"):
        assert device.give_way() is True
        assert lane == [] and _holder() == "long"


def test_a_long_pass_gives_way_at_its_seam_and_holds_the_accelerator_again(lane):
    events, idle = [], []
    short = Pass("short")

    def long_pass():
        with device.heavy_pass("long"):
            short.start()
            _until(device.heavy_pass_wanted)
            device.call_when_idle(lambda: idle.append("idle"))
            events.append(("gave way", device.give_way()))
            events.append(("holds again", _holder()))

    long = threading.Thread(target=long_pass, daemon=True)
    long.start()
    assert short.inside.wait(WAIT)      # the waiting pass went at the seam
    assert lane == ["long"]             # given back first
    assert idle == []
    short.finish()
    long.join(WAIT)
    assert events == [("gave way", True), ("holds again", "long")]
    # idle only once the long one ended, not while it waited its turn again
    assert lane == ["long", "short", "long"] and idle == ["idle"]


def test_a_stopping_node_tells_the_waiting_and_lets_none_begin(lane):
    calls = []
    holder = Pass("holder").begin()
    waiting = Pass("waiting")
    waiting.start()
    _until(device.heavy_pass_wanted)
    device.call_when_idle(lambda: calls.append("idle"))
    device.close_heavy_passes()
    assert waiting.inside.wait(WAIT) and waiting.held is False
    waiting.join(WAIT)
    for wait in (True, False):
        with device.heavy_pass("late", wait=wait) as held:
            assert held is False
    assert device.heavy_pass_wanted()            # a pass that yields stops
    device.call_when_idle(lambda: calls.append("after the stop"))
    holder.finish()
    assert _holder() is None and calls == [] and lane == ["holder"]


def test_a_pass_that_gives_way_while_the_node_stops_ends_holding_nothing():
    events = []
    short = Pass("short")

    def long_pass():
        with device.heavy_pass("long"):
            short.start()
            _until(device.heavy_pass_wanted)
            events.append(device.give_way())
            events.append(device.give_way())

    long = threading.Thread(target=long_pass, daemon=True)
    long.start()
    assert short.inside.wait(WAIT)
    device.close_heavy_passes()
    long.join(WAIT)
    assert not long.is_alive() and events == [False, False]
    # the long one's end left the short one's hold alone
    assert _holder() == "short"
    short.finish()
    assert _holder() is None


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
        ("text_emb", lambda limit: ran.append(_holder()) or {"processed": 0}, 500),))
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

@pytest.fixture
def run(monkeypatch):
    """run_parallel_enrichment with every model step a fake that says which
    pass held the accelerator when it ran; `steps` says what each step does."""
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
    steps = {}

    def step(what):
        def call(**kw):
            seen.append((what, _holder()))
            return steps.get(what, lambda cancel: {"success": 1})(kw.get("cancel_flag"))
        return call

    monkeypatch.setattr(hardware_profile, "resolve", lambda: types.SimpleNamespace(
        ml_available=True, unload_instruments_after_run=True))
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(migrations, "normalize_artists", lambda db, pass1: {})
    monkeypatch.setattr(notary, "wake", lambda *a, **kw: None)
    monkeypatch.setattr(embeddings, "AudioEmbeddingGenerator", lambda: types.SimpleNamespace(
        generate_embeddings=step("embeddings")))
    monkeypatch.setattr(audio_analysis, "AudioAnalyzer", lambda: types.SimpleNamespace(
        analyze_all=step("features")))
    monkeypatch.setattr(instrument_tagger, "release_instrument_tagger",
                        lambda dev: seen.append(("AST+PaSST released", _holder())))
    monkeypatch.setattr(text_embeddings, "generate_text_embeddings", step("titles"))
    monkeypatch.setattr(lyrics_embeddings, "generate_lyrics_embeddings", step("lyrics"))
    monkeypatch.setattr(enrichment_embeddings, "generate_all_enrichment_embeddings",
                        step("bios and genres"))
    return types.SimpleNamespace(
        go=lambda cancel=None: track_enrichment.run_parallel_enrichment(
            skip_lastfm=True, skip_lyrics=True, cancel_flag=cancel),
        seen=seen, steps=steps)


def test_the_analysis_run_holds_the_accelerator_for_each_of_its_halves(run, lane):
    run.go()
    assert run.seen == [("embeddings", AUDIO), ("features", AUDIO), ("AST+PaSST released", AUDIO),
                        ("titles", TEXT), ("lyrics", TEXT), ("bios and genres", TEXT)]
    assert lane == [AUDIO, TEXT] and _holder() is None


def _batches(events, n, after_first=None):
    """A generator's batches, each asked of its cancel flag first."""
    def generate(cancel):
        for b in range(1, n + 1):
            if cancel():
                break
            events.append(f"batch {b}")
            if b == 1 and after_first:
                after_first()
        return {"success": 1}
    return generate


def test_a_streams_analysis_waits_one_batch_of_the_run_not_the_run(run):
    events = []

    def stream():
        with device.heavy_pass("a stream's analysis") as held:
            events.append(("stream", held, _holder()))

    job = threading.Thread(target=stream, daemon=True)

    def asked_mid_run():
        job.start()
        _until(device.heavy_pass_wanted)

    run.steps["embeddings"] = _batches(events, 3, after_first=asked_mid_run)
    run.go()
    job.join(WAIT)
    assert events == ["batch 1", ("stream", True, "a stream's analysis"), "batch 2", "batch 3"]


def test_a_stopping_node_ends_the_run_at_its_next_batch(run):
    """Its seam gave the accelerator up: no stage after it begins, and the
    instrument pair, which a stream's analysis let in at that seam may be
    using, is not the run's to release."""
    events = []
    run.steps["embeddings"] = _batches(events, 3, after_first=device.close_heavy_passes)
    run.go()
    assert events == ["batch 1"]
    assert run.seen == [("embeddings", AUDIO)]


def test_a_cancelled_run_ends_at_its_next_batch_and_gives_the_pair_back(run):
    """Cancelled, the run still holds the accelerator: its seam gives no way
    to the stream's analysis that waits, no stage after it begins, and the
    pair goes inside the audio half, before that analysis is let in."""
    events, cancelled = [], []

    def stream():
        with device.heavy_pass("a stream's analysis"):
            run.seen.append(("a stream's analysis", _holder()))

    job = threading.Thread(target=stream, daemon=True)

    def cancelled_with_a_stream_waiting():
        cancelled.append(True)
        job.start()
        _until(device.heavy_pass_wanted)

    run.steps["embeddings"] = _batches(events, 3, after_first=cancelled_with_a_stream_waiting)
    run.go(cancel=lambda: bool(cancelled))
    job.join(WAIT)
    assert events == ["batch 1"]
    assert run.seen == [("embeddings", AUDIO), ("AST+PaSST released", AUDIO),
                        ("a stream's analysis", "a stream's analysis")]


def test_what_runs_at_once_for_an_idle_accelerator_is_guarded_as_the_rest(caplog):
    caplog.set_level(logging.ERROR, logger="device")

    def fails():
        raise ValueError("a callback's own failure")

    device.call_when_idle(fails)        # no pass holds it: at once, nothing raised here
    assert [r.exc_info is not None for r in caplog.records if r.name == "device"] == [True]


def test_a_cache_that_fails_to_return_keeps_neither_the_lane_nor_the_callbacks(monkeypatch,
                                                                             caplog):
    caplog.set_level(logging.ERROR, logger="device")

    def fails(dev=None):
        raise RuntimeError("the allocator's own failure")

    monkeypatch.setattr(device, "empty_cache", fails)
    calls = []
    with device.heavy_pass("a pass"):
        device.call_when_idle(lambda: calls.append("idle"))
    assert calls == ["idle"] and _holder() is None
    assert len([r for r in caplog.records if r.name == "device"]) == 1


# ── a stream's analysis and its lyrics ─────────────────────────────────────

@pytest.fixture
def stream_db(scratch_dsn, monkeypatch):
    import database
    from sqlalchemy.orm import sessionmaker
    engine = database.make_engine(scratch_dsn)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    yield engine
    engine.dispose()


@pytest.fixture
def stream(monkeypatch):
    """A stream's analysis with every model a fake that says which pass held
    the accelerator when it ran, its audio a minute of silence."""
    import librosa
    import provenance
    from streaming import enrichment

    seen = []

    class Models:
        def analyze_from_array(self, audio, sr):
            seen.append(("features", _holder()))

        def _compute_segments(self, audio):
            seen.append(("segments", _holder()))

        def _get_or_create_embedding_model(self, db):
            return None

    monkeypatch.setattr(librosa, "load",
                        lambda f, sr, mono: (np.zeros(48000 * 60, dtype=np.float32), sr))
    monkeypatch.setattr(provenance, "create_stream_source", lambda *a: 1)
    enricher = enrichment.PreviewEnricher()
    monkeypatch.setattr(enricher, "_models", lambda: (Models(), Models()))
    job = threading.Thread(target=enricher._enrich, daemon=True,
                           args=(str(uuid.uuid4()), b"stream", (60.0,), True, "youtube"))
    return types.SimpleNamespace(enricher=enricher, seen=seen, job=job)


def test_a_streams_analysis_waits_for_the_accelerator_and_works_inside_it(stream_db, stream,
                                                                         lane):
    analysis = Pass(AUDIO).begin()
    stream.job.start()
    _until(device.heavy_pass_wanted)
    assert stream.seen == []            # it waits: the track may never stream in full again
    analysis.finish()
    stream.job.join(WAIT)
    assert not stream.job.is_alive()
    assert stream.seen == [("features", "a stream's analysis"),
                           ("segments", "a stream's analysis")]
    assert lane == [AUDIO, "a stream's analysis"]


def test_a_streams_analysis_waiting_when_the_node_stops_ends_without_it(stream_db, stream):
    analysis = Pass(AUDIO).begin()
    stream.job.start()
    _until(device.heavy_pass_wanted)
    device.close_heavy_passes()
    stream.job.join(WAIT)
    assert not stream.job.is_alive() and stream.seen == []
    analysis.finish()


def test_a_stopped_enricher_takes_no_job_and_drops_the_queued(monkeypatch):
    from streaming import enrichment
    enricher = enrichment.PreviewEnricher()
    enricher._fpcalc, enricher._max_pending = True, None
    began, release = [], threading.Event()

    def enrich(track_id, *a):
        began.append(track_id)
        assert release.wait(WAIT)

    monkeypatch.setattr(enricher, "_enrich", enrich)
    enricher.submit("under way", b"stream", (60.0,), True, "youtube")
    _until(lambda: began == ["under way"])
    enricher.submit("queued", b"stream", (60.0,), True, "youtube")
    enricher.stop()
    enricher.submit("after the stop", b"stream", (60.0,), True, "youtube")
    release.set()
    enricher._pool.shutdown(wait=True)
    assert began == ["under way"]


def test_a_streams_job_is_handed_to_its_worker_under_the_lock_its_stop_takes(monkeypatch):
    """A stop between the check and the hand-over would have the pool refuse
    the job into the media proxy's hook."""
    from streaming import enrichment
    handed = []
    enricher = enrichment.PreviewEnricher()
    enricher._fpcalc, enricher._max_pending = True, None
    monkeypatch.setattr(enricher, "_pool", types.SimpleNamespace(
        submit=lambda *a: handed.append(enricher._lock.locked())))
    enricher.submit("a track", b"stream", (60.0,), True, "youtube")
    lyrics = enrichment.PreviewLyricsEnricher()
    monkeypatch.setattr(lyrics, "_pool", types.SimpleNamespace(
        submit=lambda *a: handed.append(lyrics._lock.locked())))
    lyrics.submit(types.SimpleNamespace(track_id=uuid.uuid4(), title="a title", artist="an artist",
                                        album=None, duration=None))
    assert handed == [True, True]


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


@pytest.fixture
def released(monkeypatch):
    import instrument_tagger
    seen = []
    monkeypatch.setattr(instrument_tagger, "release_instrument_tagger",
                        lambda dev: seen.append(_holder()))
    return seen


def _enricher_holding_the_pair():
    from streaming import enrichment
    enricher = enrichment.PreviewEnricher()
    enricher._analyzer = object()
    return enricher


def test_the_idle_release_of_the_instrument_pair_comes_after_the_accelerators_passes(released):
    enricher = _enricher_holding_the_pair()
    analysis = Pass(AUDIO).begin()       # the run tags with the same pair
    enricher._idle_release()
    assert released == [] and enricher._analyzer is not None
    analysis.finish()
    assert released == ["the preview's idle release"] and enricher._analyzer is None


def test_an_idle_release_that_waited_leaves_the_pair_to_a_job_that_came_meanwhile(released):
    enricher = _enricher_holding_the_pair()
    analysis = Pass(AUDIO).begin()
    enricher._idle_release()
    enricher._inflight.add("a track streamed meanwhile")
    analysis.finish()
    assert released == [] and enricher._analyzer is not None


def test_an_idle_release_that_waited_leaves_the_pair_to_a_newer_timer(released):
    enricher = _enricher_holding_the_pair()
    analysis = Pass(AUDIO).begin()
    enricher._idle_release()
    # a job came and went meanwhile: its own timer counts from its end
    enricher._idle_timer = threading.Timer(600, enricher._on_idle_timer)
    analysis.finish()
    assert released == [] and enricher._analyzer is not None
    # and a timer that is not the one armed last releases nothing either
    enricher._on_idle_timer()
    assert released == [] and enricher._idle_timer is not None


def test_an_idle_release_with_nothing_loaded_asks_nothing_of_torch(monkeypatch):
    """A torch-less node runs no analysis, but its idle timer is armed after
    every stream all the same."""
    from streaming import enrichment
    enricher = enrichment.PreviewEnricher()
    monkeypatch.setitem(sys.modules, "device", None)     # no torch: no device module
    enricher._idle_release()


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
                        lambda track_ids: seen.append((track_ids, _holder())))
    lyrics = enrichment.PreviewLyricsEnricher()
    lyrics._enrich(str(track), "demo-track", "demo-artist", None, None)
    assert seen == [([track], "a stream's lyrics")]
    assert lane == ["a stream's lyrics"]
    device.close_heavy_passes()
    lyrics._enrich(str(track), "demo-track", "demo-artist", None, None)
    assert len(seen) == 1                # the node stops: not embedded
