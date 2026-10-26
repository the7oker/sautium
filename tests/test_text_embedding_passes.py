"""A text-embedding pass the device's memory stops (backend/text_embedder.py
and the four passes over it: genre wikis, artist bios, lyrics, track
metadata). A full device — a text of a few tokens does not fit alone — stops
the pass with one line, never one per batch, and never "done"; what it did
not embed is selected again by the next pass. A text that does not fit alone
fails its own row (a row of several chunks whole), and the rest of its batch
is written, a batch of long texts only included. On 2026-10-18
one pass wrote an error line per batch, 314 of them, then "Genre desc
embeddings done: 0 genres, 0 chunks".

On MPS, which keeps the attention a forward takes (a 24 GB Mac held 32 GiB
of it after four batches of bios on 2026-10-25), a forward's texts fit a
share of the device's working set, and a text over the share alone goes to
a copy of the encoder on the CPU, given back at the end of the pass.

The encoder is a fake that fails as MPS fails; the database is real.
"""

import logging
import sys
import threading
import types
import uuid
import zlib
from pathlib import Path

import numpy as np
import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

FULL = ("MPS backend out of memory (MPS allocated: 18.13 GB, other allocations: 1.98 GB, max "
        "allowed: 20.13 GB). Tried to allocate 256.00 MB on private pool.")
# A word in the one text too long for what the device has free
LONG = "zylophant"
NAME = "a/text-encoder"
# The working set, in GB, whose share (1/8, 40 bytes a score, 16 heads) is
# 10,000 for a forward's texts times its padded length squared: one text of 100
# tokens, four of 50, sixteen of 25
SHARE_GB = "0.0512"


class Encoder:
    """sentence-transformers' encode, as the passes call it; out of memory
    whenever `full(texts, batch_size)` says so."""

    def __init__(self, full=lambda texts, batch_size: False):
        self.full, self.calls = full, []

    def encode(self, texts, batch_size, show_progress_bar, normalize_embeddings):
        self.calls.append((len(texts), batch_size))
        if self.full(texts, batch_size):
            raise RuntimeError(FULL)
        return np.stack([np.full(1024, _mark(t), dtype=np.float32) for t in texts])


class Tokenized(Encoder):
    """The encoder as a device that keeps its attention sees it: it tokenizes
    a word a token and two for the ends — only as sentence-transformers'
    encode does (no tokenizer of its own to call otherwise) — it has 16 heads,
    and it says each forward's texts and padded length."""

    def __init__(self, full=lambda texts, batch_size: False):
        super().__init__(full)
        self.forwards = []

    @staticmethod
    def preprocess(texts):
        longest = max(map(_tokens, texts), default=0)
        return {"attention_mask": np.array([[1] * _tokens(t) + [0] * (longest - _tokens(t))
                                            for t in texts]).reshape(len(texts), longest)}

    def __getitem__(self, i):
        return types.SimpleNamespace(auto_model=types.SimpleNamespace(
            config=types.SimpleNamespace(num_attention_heads=16)))

    def encode(self, texts, batch_size, show_progress_bar, normalize_embeddings):
        self.forwards.append((len(texts), max(_tokens(t) for t in texts)))
        return super().encode(texts, batch_size, show_progress_bar, normalize_embeddings)


def _tokens(text: str) -> int:
    return len(text.split()) + 2


def _words(n: int, i: int = 0) -> str:
    """A text of n tokens (n - 2 words), its own by i."""
    return " ".join([f"w{i}"] * (n - 2))


def _mark(text: str) -> float:
    """The value of every component of the fake's vector for `text`: a row
    written with another text's vector shows."""
    return (zlib.crc32(text.encode()) % 1000 + 1) / 1000


def _id(i: int) -> uuid.UUID:
    """The i-th row's id: every pass selects in the order of its ids, so the
    i-th row is the i-th of its pass."""
    return uuid.UUID(int=i + 1)


def _genres(db, texts):
    from sqlalchemy import text
    for i, content in enumerate(texts):
        gid = _id(i)
        db.execute(text("INSERT INTO genres (id, name) VALUES (:id, :name)"),
                   {"id": gid, "name": f"demo-genre-{i}"})
        db.execute(text("INSERT INTO genre_descriptions (genre_id, source, content) "
                        "VALUES (:id, 'lastfm', :content)"), {"id": gid, "content": content})


def _bios(db, texts):
    from sqlalchemy import text
    for i, content in enumerate(texts):
        aid = _id(i)
        db.execute(text("INSERT INTO artists (id, name) VALUES (:id, :name)"),
                   {"id": aid, "name": f"demo-artist-{i}"})
        db.execute(text("INSERT INTO artist_bios (artist_id, source, content) "
                        "VALUES (:id, 'lastfm', :content)"), {"id": aid, "content": content})


def _lyrics(db, texts):
    from sqlalchemy import text
    for i, lyric in enumerate(texts):
        tid = _id(i)
        db.execute(text("INSERT INTO tracks (id, title) VALUES (:id, :title)"),
                   {"id": tid, "title": f"demo-track-{i}"})
        db.execute(text("INSERT INTO track_lyrics (track_id, source, plain_lyrics) "
                        "VALUES (:id, 'lrclib', :lyric)"), {"id": tid, "lyric": lyric})


def _owned(db, texts):
    from sqlalchemy import text
    artist, album = uuid.uuid4(), uuid.uuid4()
    db.execute(text("INSERT INTO artists (id, name) VALUES (:id, 'demo-artist')"), {"id": artist})
    db.execute(text("INSERT INTO albums (id, title) VALUES (:id, 'demo-album')"), {"id": album})
    variant = db.execute(text("INSERT INTO album_variants (album_id, directory_path) "
                              "VALUES (:id, '/music/demo-album') RETURNING id"),
                         {"id": album}).scalar()
    for i, title in enumerate(texts):
        tid = _id(i)
        db.execute(text("INSERT INTO tracks (id, title) VALUES (:id, :title)"),
                   {"id": tid, "title": title})
        db.execute(text("INSERT INTO track_artists (track_id, artist_id, role) "
                        "VALUES (:t, :a, 'primary')"), {"t": tid, "a": artist})
        db.execute(text("INSERT INTO media_files (track_id, album_variant_id, file_path) "
                        "VALUES (:t, :v, :p)"), {"t": tid, "v": variant, "p": f"/music/{i:02d}.flac"})


def _genre_pass(db):
    from enrichment_embeddings import GenreDescEmbeddingGenerator
    return GenreDescEmbeddingGenerator(batch_size=4, device="cpu")


def _bio_pass(db):
    from enrichment_embeddings import ArtistBioEmbeddingGenerator
    return ArtistBioEmbeddingGenerator(batch_size=4, device="cpu")


def _lyrics_pass(db):
    from lyrics_embeddings import LyricsEmbeddingGenerator
    return LyricsEmbeddingGenerator(batch_size=4, device="cpu")


def _text_pass(db):
    from text_embeddings import TextEmbeddingGenerator
    return TextEmbeddingGenerator(batch_size=4, device="cpu")


# (rows, generator, the rows embedded, the chunks' text and vector, its logger,
# the end line's heads: stopped and done, and the done line of ten)
PASSES = {
    "genre wikis": (_genres, _genre_pass, "SELECT DISTINCT genre_id FROM genre_desc_embeddings",
                    "SELECT chunk_text, (vector::real[])[1] FROM genre_desc_embeddings",
                    "enrichment_embeddings", "Genre desc embeddings stopped: ",
                    "Genre desc embeddings done: ",
                    "Genre desc embeddings done: 10 genres, 10 chunks, 0 failed"),
    "artist bios": (_bios, _bio_pass, "SELECT DISTINCT artist_id FROM artist_bio_embeddings",
                    "SELECT chunk_text, (vector::real[])[1] FROM artist_bio_embeddings",
                    "enrichment_embeddings", "Artist bio embeddings stopped: ",
                    "Artist bio embeddings done: ",
                    "Artist bio embeddings done: 10 artists, 10 chunks, 0 failed"),
    "lyrics": (_lyrics, _lyrics_pass, "SELECT DISTINCT track_id FROM lyrics_embeddings",
               "SELECT chunk_text, (vector::real[])[1] FROM lyrics_embeddings",
               "lyrics_embeddings", "Lyrics embedding generation stopped: ",
               "Lyrics embedding generation complete: ",
               "Lyrics embedding generation complete: 10 tracks, 10 chunks, 0 failed, 0 skipped"),
    "track metadata": (_owned, _text_pass, "SELECT track_id FROM text_embeddings", None,
                       "text_embeddings", "Text embedding generation stopped: ",
                       "Text embedding generation complete: ",
                       "Text embedding generation complete: 10 success, 0 failed"),
}


@pytest.fixture
def db(scratch_dsn):
    import database
    from sqlalchemy import text
    from sqlalchemy.orm import sessionmaker
    engine = database.make_engine(scratch_dsn)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    session.execute(text("TRUNCATE genres, artists, tracks, albums, embedding_models CASCADE"))
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _texts(*long_at):
    """Ten texts, those at `long_at` far the longest and too long for what
    the device has free."""
    return [(" ".join([LONG] * 40) if i in long_at else f"A synthetic text, number {i}.")
            for i in range(10)]


def _run(db, kind, encoder):
    gen = PASSES[kind][1](db)
    gen.model = encoder
    return gen.generate_all(db)


def _embedded(db, kind) -> set:
    """The rows written, by their place (_id), each chunk with its own text's
    vector where the pass keeps the chunk."""
    from sqlalchemy import text
    places = {uuid.UUID(int=i + 1): i for i in range(10)}
    if PASSES[kind][3] is not None:
        for chunk, value in db.execute(text(PASSES[kind][3])):
            assert abs(value - _mark(chunk)) < 1e-6, (chunk, value)
    return {places[r[0]] for r in db.execute(text(PASSES[kind][2]))}


def _lines(caplog, kind, level):
    logger = PASSES[kind][4]
    return [r.getMessage() for r in caplog.records if r.name == logger and r.levelno == level]


@pytest.fixture(autouse=True)
def said_afresh(monkeypatch):
    import text_embedder
    monkeypatch.setattr(text_embedder, "_one_at_a_time", set())
    monkeypatch.setattr(text_embedder, "_refusing", set())
    monkeypatch.setattr(text_embedder, "_took_now", set())
    monkeypatch.setattr(text_embedder, "_refused_now", set())


@pytest.mark.parametrize("kind", PASSES)
def test_a_pass_the_full_device_stops_says_so_once_and_leaves_the_rest_to_the_next(db, caplog,
                                                                                   kind):
    PASSES[kind][0](db, _texts())
    db.commit()
    caplog.set_level(logging.DEBUG)
    full = Encoder(lambda texts, batch_size: True)
    stats = _run(db, kind, full)
    # tried at its batch size, then its first text alone, then a text of a few tokens — and no
    # batch after it
    assert full.calls == [(4, 4), (1, 1), (1, 1)]
    assert stats["stopped"].startswith("out of memory on cpu: MPS backend out of memory")
    assert (stats["success"], stats["failed"], _embedded(db, kind)) == (0, 4, set())
    errors = _lines(caplog, kind, logging.ERROR)
    assert len(errors) == 1 and errors[0].startswith(PASSES[kind][5]), errors
    assert not [m for m in _lines(caplog, kind, logging.INFO) if m.startswith(PASSES[kind][6])]
    # the next pass selects them all again
    caplog.clear()
    stats = _run(db, kind, Encoder())
    assert (stats["success"], stats["failed"], "stopped" in stats, _embedded(db, kind)) == (
        10, 0, False, set(range(10)))
    done = [m for m in _lines(caplog, kind, logging.INFO) if m.startswith(PASSES[kind][6])]
    assert len(done) == 1 and done[0].startswith(PASSES[kind][7]), done


@pytest.mark.parametrize("kind", PASSES)
def test_a_text_that_does_not_fit_alone_fails_its_row_and_the_rest_are_written(db, caplog, kind):
    PASSES[kind][0](db, _texts(0))
    db.commit()
    caplog.set_level(logging.DEBUG)
    tight = Encoder(lambda texts, batch_size: any(LONG in t for t in texts))
    stats = _run(db, kind, tight)
    assert (stats["success"], stats["failed"], "stopped" in stats) == (9, 1, False), stats
    assert _embedded(db, kind) == set(range(1, 10))     # the long one alone left out
    # the long text's batch at its size, then one text at a time — the long one, a text of a
    # few tokens after it, the other three — and the batches after it at their size
    assert tight.calls == [(4, 4), (1, 1), (1, 1), (1, 1), (1, 1), (1, 1), (4, 4), (2, 4)], \
        tight.calls
    assert not _lines(caplog, kind, logging.ERROR)
    warned = [m for m in _lines(caplog, kind, logging.WARNING) if "finished with failures" in m]
    assert len(warned) == 1, warned
    assert [r.getMessage() for r in caplog.records
            if r.name == "text_embedder" and r.levelno == logging.WARNING] == [
        "Text encoder out of memory on cpu at 4 texts a batch: such a batch goes one text at a "
        "time"]


@pytest.mark.parametrize("kind", PASSES)
def test_a_batch_of_long_texts_only_fails_its_rows_and_the_pass_goes_on(db, caplog, kind):
    """The rows a pass left out come first in the next (the selection is by
    id): a batch may hold nothing but texts too long for what the device has
    free. Its texts fail, never the device: the pass goes on past it."""
    PASSES[kind][0](db, _texts(0, 1, 2, 3))
    db.commit()
    caplog.set_level(logging.DEBUG)
    stats = _run(db, kind, Encoder(lambda texts, batch_size: any(LONG in t for t in texts)))
    assert (stats["success"], stats["failed"], "stopped" in stats) == (6, 4, False), stats
    assert _embedded(db, kind) == set(range(4, 10))
    assert not _lines(caplog, kind, logging.ERROR)


def test_a_row_of_several_chunks_fails_whole_when_one_does_not_fit(db, caplog):
    """A bio long enough for two chunks, the second too long for what the
    device has free: the artist is written with neither — a row with a chunk
    is a row embedded, never selected again — and the others are written."""
    words = [f"w{i}" for i in range(1600)] + [LONG]
    texts = _texts()
    texts[2] = " ".join(words)
    _bios(db, texts)
    db.commit()
    caplog.set_level(logging.DEBUG)
    tight = Encoder(lambda texts, batch_size: any(LONG in t for t in texts))
    stats = _run(db, "artist bios", tight)
    assert tight.calls[0] == (5, 4)             # the first batch's four artists: five chunks
    assert (stats["success"], stats["failed"], "stopped" in stats) == (9, 1, False), stats
    assert _embedded(db, "artist bios") == set(range(10)) - {2}
    assert not _lines(caplog, "artist bios", logging.ERROR)


def test_a_batch_of_one_text_that_does_not_fit_is_tried_alone_once():
    import text_embedder
    tight = Encoder(lambda texts, batch_size: any(LONG in t for t in texts))
    vectors, left_out = text_embedder.encode(tight, [" ".join([LONG] * 40)], batch_size=4,
                                             device="cpu", name=NAME)
    assert (vectors, left_out) == ([None], {0})
    assert tight.calls == [(1, 4), (1, 1)]          # the text, then a text of a few tokens


def test_a_batch_of_one_text_on_a_full_device_stops_in_the_few_tokens_own_words(monkeypatch):
    """The batch's try was its text alone; then the text of a few tokens,
    which does not fit either: the stop quotes what that one met, and the
    device's cache is freed before it and before the stop."""
    import device
    import text_embedder
    events = []
    monkeypatch.setattr(device, "empty_cache", lambda dev: events.append("freed"))

    class Naming(Encoder):
        def encode(self, texts, **kw):
            events.append(texts[0])
            raise RuntimeError(f"{FULL} [{texts[0]}]")

    with pytest.raises(text_embedder.OutOfMemory) as stop:
        text_embedder.encode(Naming(), ["A short one."], batch_size=4, device="cpu", name=NAME)
    assert events == ["A short one.", "freed", text_embedder._CANARY, "freed"]
    assert str(stop.value).endswith(f"[{text_embedder._CANARY}]"), stop.value


def test_the_devices_cache_is_freed_before_every_try_after_a_failed_one(monkeypatch):
    import device
    import text_embedder
    events = []
    monkeypatch.setattr(device, "empty_cache", lambda dev: events.append("freed"))

    class Logged(Encoder):
        def encode(self, texts, **kw):
            events.append(len(texts))
            return super().encode(texts, **kw)

    tight = Logged(lambda texts, batch_size: any(LONG in t for t in texts))
    text_embedder.encode(tight, [" ".join([LONG] * 40), "A short one."], batch_size=2,
                         device="cpu", name=NAME)
    assert events == [2, "freed", 1, "freed", 1, 1]


def test_the_fall_back_to_one_text_at_a_time_is_said_once_a_process(caplog):
    import text_embedder
    caplog.set_level(logging.DEBUG, logger="text_embedder")
    tight = Encoder(lambda texts, batch_size: len(texts) > 1)     # every text fits alone
    for _ in range(2):
        vectors, left_out = text_embedder.encode(tight, ["A first.", "A second."], batch_size=2,
                                                 device="cpu", name=NAME)
        assert left_out == set() and [float(v[0]) for v in vectors] == pytest.approx(
            [_mark("A first."), _mark("A second.")])
    said = [r.levelno for r in caplog.records if r.name == "text_embedder"]
    assert said == [logging.WARNING, logging.DEBUG], said


def test_an_analysis_run_whose_text_pass_stopped_is_no_success(monkeypatch):
    monkeypatch.chdir(BACKEND)        # main mounts static/ relative to its directory
    import main
    import track_enrichment
    stopped = {"processed": 4, "success": 0, "skipped": 0, "failed": 4, "chunks": 0,
               "stopped": "out of memory on mps: " + FULL}
    monkeypatch.setattr(track_enrichment, "run_parallel_enrichment", lambda **kw: {
        "text_embeddings": {"processed": 0, "success": 0, "failed": 0},
        "lyrics_embeddings": {"success": 0},
        "enrichment_embeddings": {"artist_bios": {"success": 3}, "genre_descs": stopped}})
    # The run's state is the process's: this test's own, put back after it
    monkeypatch.setattr(main, "_enrich_state", {**main._enrich_state, "running": True})
    main._enrich_worker(None, True)
    assert main._enrich_state["result"]["success"] is False
    assert main._enrich_state["progress"].endswith(
        "| Bio emb: 3 | not finished: genre emb stopped (out of memory)")


# ── on a device that keeps its attention ───────────────────────────────────

@pytest.fixture
def cpu_copy(monkeypatch):
    """A working set whose share is 10,000 (SHARE_GB), and the encoder's copy
    on the CPU a fake that says what it took and how often it was loaded. No
    MPS here to free a cache of."""
    import device
    import text_embedder
    monkeypatch.setenv("SAUTIUM_ACCEL_MEMORY_GB", SHARE_GB)
    monkeypatch.setattr(device, "empty_cache", lambda dev=None: None)
    cpu = Tokenized()
    cpu.loads = []
    monkeypatch.setattr(text_embedder, "_load", lambda name, device, **kw: cpu.loads.append(
        (name, device, kw.get("local_files_only"))) or cpu)
    monkeypatch.setattr(text_embedder, "_cpu", None)
    return cpu


def test_on_mps_a_forward_takes_no_more_attention_than_its_share(cpu_copy):
    import text_embedder
    mps = Tokenized()
    texts = ([_words(50, i) for i in range(5)] + [_words(100)] + [_words(20, i) for i in range(6)]
             + [_words(101)])
    vectors, left_out = text_embedder.encode(mps, texts, batch_size=4, device="mps", name=NAME)
    # the longest first, each forward's texts times its padded length squared within 10,000:
    # 1 × 100², 4 × 50², 50² with three of 20, the three of 20 left
    assert mps.forwards == [(1, 100), (4, 50), (4, 50), (3, 20)]
    # the one over the share alone went to the CPU
    assert cpu_copy.forwards == [(1, 101)] and cpu_copy.loads == [(NAME, "cpu", True)]
    assert left_out == set()
    assert [float(v[0]) for v in vectors] == pytest.approx([_mark(t) for t in texts])


def test_the_cpu_copy_is_loaded_once_a_pass_and_given_back_at_its_end(cpu_copy, caplog):
    import device
    import text_embedder
    caplog.set_level(logging.INFO, logger="text_embedder")
    mps = Tokenized()
    with device.heavy_pass("a text pass"):
        for i in range(2):
            text_embedder.encode(mps, [_words(101, i), _words(10, i)], batch_size=4, device="mps",
                                 name=NAME)
        assert cpu_copy.loads == [(NAME, "cpu", True)]
    assert text_embedder._cpu is None
    assert [r.getMessage() for r in caplog.records if r.name == "text_embedder"] == [
        f"Text embedding model on the CPU released: {NAME}"]
    with device.heavy_pass("the next text pass"):
        text_embedder.encode(mps, [_words(101)], batch_size=4, device="mps", name=NAME)
    assert cpu_copy.loads == [(NAME, "cpu", True), (NAME, "cpu", True)]


def test_a_bio_pass_on_mps_writes_the_bio_over_the_share_from_the_cpu(db, cpu_copy):
    from enrichment_embeddings import ArtistBioEmbeddingGenerator
    texts = _texts()
    texts[3] = _words(150, 3)
    _bios(db, texts)
    db.commit()
    gen = ArtistBioEmbeddingGenerator(batch_size=4, device="mps")
    gen.model = Tokenized()
    stats = gen.generate_all(db)
    assert (stats["success"], stats["failed"], "stopped" in stats) == (10, 0, False), stats
    assert _embedded(db, "artist bios") == set(range(10))
    assert cpu_copy.forwards == [(1, 152)]      # that bio alone, with its "Artist: …." words


def _refusals(caplog):
    return [r for r in caplog.records
            if r.name == "text_embedder" and r.getMessage().startswith("Text encoder: mps refused")]


def test_what_mps_refuses_goes_to_the_cpu_said_once_until_a_pass_goes_by_without(cpu_copy,
                                                                                  caplog):
    import device
    import text_embedder
    caplog.set_level(logging.DEBUG, logger="text_embedder")
    refusing = Tokenized(lambda texts, batch_size: True)      # not a text of a few tokens either
    taking = Tokenized()
    texts = [_words(10, i) for i in range(3)]

    def a_pass(*models):
        with device.heavy_pass("a text pass"):
            for model in models:
                vectors, left_out = text_embedder.encode(model, texts, batch_size=4,
                                                         device="mps", name=NAME)
                assert left_out == set()
                assert [float(v[0]) for v in vectors] == pytest.approx([_mark(t) for t in texts])

    a_pass(refusing, refusing)
    said = _refusals(caplog)
    assert [r.levelno for r in said] == [logging.WARNING]
    assert said[0].getMessage().startswith(
        "Text encoder: mps refused a text of a few tokens (out of memory on mps: MPS backend out "
        "of memory")
    a_pass(refusing, taking, refusing)      # the same episode, however it alternates
    assert len(_refusals(caplog)) == 1
    a_pass()                                # a pass that encodes no text (a stream's analysis)
    a_pass(refusing)
    assert len(_refusals(caplog)) == 1
    a_pass(taking)                          # a pass that encodes without a refusal ends it
    a_pass(refusing)
    assert len(_refusals(caplog)) == 2


def test_a_text_the_cpu_leaves_out_is_the_one_left_out(cpu_copy):
    import text_embedder
    cpu_copy.full = lambda texts, batch_size: any(LONG in t for t in texts)
    texts = ["A short one.", " ".join([LONG] * 8), "Another short one."]
    vectors, left_out = text_embedder.encode(Tokenized(lambda texts, batch_size: True), texts,
                                             batch_size=4, device="mps", name=NAME)
    assert left_out == {1} and vectors[1] is None
    assert [float(vectors[i][0]) for i in (0, 2)] == pytest.approx([_mark(texts[0]),
                                                                    _mark(texts[2])])


@pytest.mark.parametrize("cycle, collected", [("model_card_data", 0), ("another", 1)])
def test_the_cpu_copy_given_back_at_the_end_of_a_pass_is_freed_then(cpu_copy, monkeypatch,
                                                                    cycle, collected):
    """sentence-transformers' model and its model card refer to each other:
    parted, the copy goes with its last reference; another cycle is collected."""
    import gc
    import weakref
    import device
    import text_embedder

    class Cyclic(Tokenized):
        def __init__(self):
            super().__init__()
            setattr(self, cycle, types.SimpleNamespace(model=self))

    loaded, collections = [], []

    def load(name, device, **kw):
        model = Cyclic()
        loaded.append(weakref.ref(model))
        return model

    monkeypatch.setattr(text_embedder, "_load", load)
    real_collect = gc.collect
    monkeypatch.setattr(gc, "collect", lambda *a: collections.append(1) or real_collect(*a))
    with device.heavy_pass("a text pass"):
        text_embedder.encode(Tokenized(), [_words(101)], batch_size=4, device="mps", name=NAME)
        assert loaded[0]() is not None
    assert loaded[0]() is None and len(collections) == collected


def _a_bin_load_refused(tmp_path, monkeypatch):
    """The CPU copy's load as BGE-M3 ships it, a .bin read by transformers'
    load_state_dict, whose torch.load the system refuses the mmap of its
    2.27 GB (ENOMEM): what reaches the loader is transformers' own OSError."""
    import torch
    from transformers.modeling_utils import load_state_dict
    path = tmp_path / "pytorch_model.bin"
    torch.save({"w": torch.zeros(1)}, path)

    def refused(*a, **kw):
        raise RuntimeError(f"unable to mmap 2271064456 bytes from file <{path}>: "
                           "Cannot allocate memory (12)")

    monkeypatch.setattr(torch, "load", refused)
    return lambda name, device, **kw: load_state_dict(str(path))


@pytest.mark.parametrize("load", ["the allocator", "a .bin's mmap"])
def test_a_cpu_with_no_memory_for_the_copy_stops_the_pass(db, caplog, cpu_copy, monkeypatch,
                                                          tmp_path, load):
    import text_embedder
    _bios(db, _texts())
    db.commit()

    def no_room(name, device, **kw):
        raise RuntimeError("DefaultCPUAllocator: not enough memory: you tried to allocate "
                           "1048576 bytes.")

    monkeypatch.setattr(text_embedder, "_load", no_room if load == "the allocator"
                        else _a_bin_load_refused(tmp_path, monkeypatch))
    gen = PASSES["artist bios"][1](db)
    gen.device, gen.model = "mps", Tokenized(lambda texts, batch_size: True)
    stats = gen.generate_all(db)
    # in the words of the error that says so, not transformers' of a checkpoint
    said = {"the allocator": "DefaultCPUAllocator: not enough memory",
            "a .bin's mmap": "unable to mmap 2271064456 bytes"}[load]
    assert stats["stopped"].startswith(f"out of memory loading {gen.model_name} on the cpu: {said}")
    assert (stats["success"], _embedded(db, "artist bios")) == (0, set())
    # one stop, not an error and a new load for every batch
    assert len(_lines(caplog, "artist bios", logging.ERROR)) == 1


def test_an_mps_node_caps_the_threads_of_its_cpu_inference():
    import hardware_profile

    def threads(device, name="full"):
        return hardware_profile.HardwareProfile(
            name=name, source="auto", device=device, accel_name=None, accel_memory_gb=24.0,
            ram_gb=24.0, cores=10, ml_available=True).torch_cpu_threads

    assert (threads("mps"), threads("mps", "lite"), threads("cpu"), threads("cuda")) == (
        8, 5, 8, None)


def test_a_text_mps_cannot_take_alone_goes_to_the_cpu(cpu_copy):
    import text_embedder
    tight = Tokenized(lambda texts, batch_size: any(LONG in t for t in texts))
    texts = ["A short one.", " ".join([LONG] * 8), "Another short one."]
    vectors, left_out = text_embedder.encode(tight, texts, batch_size=4, device="mps", name=NAME)
    assert left_out == set()
    assert [float(v[0]) for v in vectors] == pytest.approx([_mark(t) for t in texts])
    assert cpu_copy.forwards == [(1, 10)]       # that one alone; MPS still took a few tokens


@pytest.mark.parametrize("kind", PASSES)
def test_a_pass_on_a_refusing_mps_is_done_on_the_cpu(db, caplog, cpu_copy, kind):
    """The Mac's case: every allocation on MPS refused. The pass is done, not
    stopped every half hour."""
    PASSES[kind][0](db, _texts())
    db.commit()
    caplog.set_level(logging.DEBUG)
    gen = PASSES[kind][1](db)
    gen.device, gen.model = "mps", Tokenized(lambda texts, batch_size: True)
    stats = gen.generate_all(db)
    assert (stats["success"], stats["failed"], "stopped" in stats, _embedded(db, kind)) == (
        10, 0, False, set(range(10)))
    assert not _lines(caplog, kind, logging.ERROR)
    done = [m for m in _lines(caplog, kind, logging.INFO) if m.startswith(PASSES[kind][6])]
    assert len(done) == 1 and done[0].startswith(PASSES[kind][7]), done
    assert len(_refusals(caplog)) == 1


def test_a_pass_whose_cpu_cannot_take_a_few_tokens_either_stops(db, caplog, cpu_copy):
    _bios(db, _texts())
    db.commit()
    cpu_copy.full = lambda texts, batch_size: True
    gen = PASSES["artist bios"][1](db)
    gen.device, gen.model = "mps", Tokenized(lambda texts, batch_size: True)
    stats = gen.generate_all(db)
    assert stats["stopped"].startswith("out of memory on cpu: MPS backend out of memory")
    assert (stats["success"], stats["failed"], _embedded(db, "artist bios")) == (0, 4, set())


def test_on_mps_the_cpu_takes_its_texts_one_at_a_time(cpu_copy):
    """They are the call's longest: padded together they would build their
    mask at texts x length². And one the CPU cannot take costs none of those
    before it: each is its own try."""
    import text_embedder
    texts = [_words(101 + i, i) for i in range(3)] + ["A short one.", _words(104, 9) + " " + LONG]
    cpu_copy.full = lambda texts, batch_size: any(LONG in t for t in texts)
    vectors, left_out = text_embedder.encode(Tokenized(), texts, batch_size=4, device="mps",
                                             name=NAME)
    assert left_out == {4} and vectors[4] is None
    assert [float(v[0]) for v in vectors[:4]] == pytest.approx([_mark(t) for t in texts[:4]])
    # the longest first, each alone; the one that failed tried once, then a text of a few tokens
    assert cpu_copy.forwards == [(1, 105), (1, 3), (1, 103), (1, 102), (1, 101)]


def _held(lock) -> bool:
    """Whether a thread holds `lock`: another cannot take it. What the probe
    takes it gives back itself — a lock left owned by a thread that ended
    would hang every later test that takes it."""
    free = []

    def probe():
        if lock.acquire(blocking=False):
            lock.release()
            free.append(True)

    t = threading.Thread(target=probe)
    t.start()
    t.join()
    return not free


@pytest.fixture
def factory_lock(monkeypatch):
    """model_cache._factory_lock, a lock of the test's own: should the waits
    the test is about come back, the threads they leave keep that one, and
    the tests after it fail on their own."""
    import model_cache
    monkeypatch.setattr(model_cache, "_factory_lock", threading.RLock())
    return model_cache._factory_lock


def test_the_encoders_loads_wait_for_any_other_load_of_the_process(monkeypatch, factory_lock):
    """transformers' loading patches process-wide state: the encoder's loads
    take the search models' lock (model_cache._factory_lock) — the CPU copy,
    loaded every pass, without a hub check — and a pass asking for the model a
    search's factory is loading waits on that lock and finds it loaded, where
    a single-flight event of its own had left the two waiting on each other."""
    import text_embedder
    seen = []

    class Loaded:
        def __init__(self, name, device, **kw):
            seen.append((name, device, kw.get("local_files_only"), _held(factory_lock)))

    monkeypatch.setattr(text_embedder, "SentenceTransformer", Loaded)
    monkeypatch.setattr(text_embedder, "_cpu", None)
    monkeypatch.setattr(text_embedder, "_cache", {})
    text_embedder._cpu_copy(NAME)
    text_embedder._end_pass()
    assert seen == [(NAME, "cpu", True, True)]

    # a search's factory holds the lock while it loads; a pass asks meanwhile
    passes, searched = [], []
    asking = threading.Thread(target=lambda: passes.append(
        text_embedder.get_text_embedder(NAME, "cpu")), daemon=True)

    def factory():
        with factory_lock:
            asking.start()
            asking.join(0.2)
            searched.append((asking.is_alive(), text_embedder.get_text_embedder(NAME, "cpu")))

    loading = threading.Thread(target=factory, daemon=True)
    loading.start()
    loading.join(10)
    asking.join(10)
    # neither waits for the other: the pass waited on the lock, holding nothing
    assert not loading.is_alive() and not asking.is_alive()
    assert searched[0][0] and passes == [searched[0][1]]
    assert seen[1:] == [(NAME, "cpu", None, True)]    # one load, the lock held


@pytest.mark.parametrize("which", ["clap", "instruments"])
def test_clap_and_the_instrument_pair_load_under_the_same_lock(monkeypatch, factory_lock, which):
    """Loaded from a pass (a stream's analysis, the run) beside a search's
    load, they would race it as the text encoder would: a pass's load holds
    the lock, and a search's factory, which holds it already, takes it
    again."""
    held = []

    def loading():
        held.append(_held(factory_lock))
        return object()

    if which == "clap":
        import clap_model
        monkeypatch.setattr(clap_model, "_cache", {})
        monkeypatch.setattr(clap_model, "ClapProcessor", types.SimpleNamespace(
            from_pretrained=lambda name: loading()))
        monkeypatch.setattr(clap_model, "ClapModel", types.SimpleNamespace(
            from_pretrained=lambda name, torch_dtype: types.SimpleNamespace(
                to=lambda device: types.SimpleNamespace(eval=lambda: None))))
        get, drop = lambda: clap_model.get_clap_model("clap", "cpu"), clap_model._cache.clear
    else:
        import ensemble_instruments
        import instrument_tagger
        monkeypatch.setattr(instrument_tagger, "_cache", {})

        class Pair:
            def __init__(self, device):
                pass

            def load(self):
                loading()

        monkeypatch.setattr(ensemble_instruments, "InstrumentEnsembleTagger", Pair)
        get = lambda: instrument_tagger.get_instrument_tagger("cpu")
        drop = instrument_tagger._cache.clear
    first = get()                                   # a pass's, holding no lock
    assert get() == first                           # the same
    drop()
    searched = []

    def search():
        with factory_lock:                          # the search's factory
            searched.append(get())

    t = threading.Thread(target=search, daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive() and len(searched) == 1
    assert held == [True, True]                     # a load each, the lock held


def test_the_systems_own_out_of_memory_is_out_of_memory(tmp_path, monkeypatch):
    """A weights file's mmap that ENOMEM refuses — the CPU copy's load on a
    Mac out of memory — is no other failure, re-raised by transformers too:
    the words of the error that says so, read down the chain its traceback
    shows."""
    import errno
    import device
    enomem = OSError(errno.ENOMEM, "Cannot allocate memory")
    mmap = RuntimeError("unable to mmap 2271064456 bytes from file </x>: Cannot allocate "
                        "memory (12)")
    assert device.out_of_memory(enomem) == "[Errno 12] Cannot allocate memory"
    assert device.out_of_memory(mmap) == str(mmap)
    assert device.out_of_memory(OSError(errno.ENOENT, "No such file or directory")) is None
    with pytest.raises(OSError) as raised:
        _a_bin_load_refused(tmp_path, monkeypatch)(NAME, "cpu")
    assert raised.value.errno is None
    assert device.out_of_memory(raised.value).startswith("unable to mmap 2271064456 bytes")

    def raised_over(over, then):
        try:
            try:
                raise over
            except Exception:
                then()
        except Exception as e:
            return e

    def plain():
        raise ValueError("another failure")

    def from_none():
        raise ValueError("another failure") from None

    def from_full():
        raise ValueError("another failure") from MemoryError("Cannot allocate memory")

    assert device.out_of_memory(raised_over(MemoryError(), plain)) == "MemoryError"
    assert device.out_of_memory(raised_over(KeyError("a key"), from_full)) == (
        "Cannot allocate memory")
    assert device.out_of_memory(raised_over(MemoryError(), from_none)) is None
    assert device.out_of_memory(raised_over(KeyError("a key"), plain)) is None


@pytest.fixture
def no_collection():
    """The cyclic collector off, and as it was after: what the test lets go
    is freed by its last reference, or kept."""
    import gc
    was = gc.isenabled()
    gc.disable()
    yield
    if was:
        gc.enable()


def test_a_try_out_of_memory_leaves_nothing_of_itself_kept(cpu_copy, monkeypatch, no_collection):
    """Only the words of an out of memory are kept: the error's traceback
    holds the frames of the try that failed and what they allocated, which
    the retry after it needs freed — at once, not at a collection."""
    import weakref
    import text_embedder
    allocated = []

    class Activations:
        pass

    def failing(*a, **kw):
        activations = Activations()
        allocated.append(weakref.ref(activations))
        raise RuntimeError("MPS backend out of memory (MPS allocated: 1.36 GiB, other "
                           "allocations: 31.94 GiB, max allowed: 30.19 GiB)")

    monkeypatch.setattr(text_embedder, "_load", failing)
    vectors, why = text_embedder._encoded(types.SimpleNamespace(encode=failing), ["a"], 1)
    try:
        text_embedder._cpu_copy(NAME)
    except text_embedder.OutOfMemory as e:
        stopped = str(e)
    assert len(allocated) == 2 and [kept() for kept in allocated] == [None, None]
    assert vectors is None and why.startswith("MPS backend out of memory")
    assert stopped.startswith(f"out of memory loading {NAME} on the cpu: MPS backend")


def test_a_refusal_said_keeps_nothing_of_the_pass(cpu_copy, monkeypatch, caplog, no_collection):
    """The refusal's line keeps the words of the error, not the error: a log
    handler that keeps its records (pytest's own, a buffer) would keep the
    error's frames, and with them the CPU copy the pass gave back."""
    import weakref
    import device
    import text_embedder
    caplog.set_level(logging.WARNING, logger="text_embedder")
    copies = []

    def load(name, device, **kw):
        copy = Tokenized()
        copies.append(weakref.ref(copy))
        return copy

    monkeypatch.setattr(text_embedder, "_load", load)
    with device.heavy_pass("a text pass"):
        vectors, left_out = text_embedder.encode(Tokenized(lambda texts, batch_size: True),
                                                 [_words(10)], batch_size=4, device="mps",
                                                 name=NAME)
    assert left_out == set() and len(_refusals(caplog)) == 1
    assert len(copies) == 1 and copies[0]() is None


@pytest.mark.parametrize("kind", [*PASSES, "audio"])
def test_a_new_model_record_is_committed_at_once(db, scratch_dsn, kind):
    """The run gives way at its seams: a record left uncommitted there would
    hold another pass's insert of the same id, while the run waits for that
    pass (device.give_way)."""
    import database
    from sqlalchemy import text
    if kind == "audio":
        from embeddings import AudioEmbeddingGenerator
        AudioEmbeddingGenerator(device="cpu")._get_or_create_embedding_model(db)
    else:
        PASSES[kind][1](db)._get_or_create_model_record(db)
    other = database.make_engine(scratch_dsn)
    try:
        with other.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM embedding_models")).scalar() == 1
    finally:
        other.dispose()
