"""A text-embedding pass the device's memory stops (backend/text_embedder.py
and the four passes over it: genre wikis, artist bios, lyrics, track
metadata). A full device — a text of a few tokens does not fit alone — stops
the pass with one line, never one per batch, and never "done"; what it did
not embed is selected again by the next pass. A text that does not fit alone
fails its own row (a row of several chunks whole), and the rest of its batch
is written, a batch of long texts only included. On 2026-10-18
one pass wrote an error line per batch, 314 of them, then "Genre desc
embeddings done: 0 genres, 0 chunks".

The encoder is a fake that fails as MPS fails; the database is real.
"""

import logging
import sys
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
                                             device="cpu")
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
        text_embedder.encode(Naming(), ["A short one."], batch_size=4, device="cpu")
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
                         device="cpu")
    assert events == [2, "freed", 1, "freed", 1, 1]


def test_the_fall_back_to_one_text_at_a_time_is_said_once_a_process(caplog):
    import text_embedder
    caplog.set_level(logging.DEBUG, logger="text_embedder")
    tight = Encoder(lambda texts, batch_size: len(texts) > 1)     # every text fits alone
    for _ in range(2):
        vectors, left_out = text_embedder.encode(tight, ["A first.", "A second."], batch_size=2,
                                                 device="cpu")
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
