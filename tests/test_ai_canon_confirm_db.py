"""The AI canon tier on a node with catalogue slices and no MusicBrainz
database (backend/canon/ai_canon.py): the model is shown MusicBrainz's own
answers for the slice's candidates — a candidate MusicBrainz does not know is
not offered —, a pick must be one of those, a proposed name must be one
MusicBrainz carries for the artist the slice resolves it to, and the artist
is renamed to MusicBrainz's name.

On the module's scratch database; MusicBrainz is tests/mb_fakes.py's fake,
the model a stub that records its prompt and answers as told."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import mb_fakes  # noqa: E402
from mb_fakes import FakeMusicBrainz, Library, Slice, drain, mbid  # noqa: E402

JON, OTHER_JON, ANDERSON = mbid(), mbid(), mbid()


@pytest.fixture
def node(scratch_dsn, monkeypatch):
    import mb_answers
    from canon import ai_canon
    from sqlalchemy.orm import sessionmaker
    conn, pool, engine = mb_fakes.slice_fed(scratch_dsn, monkeypatch)
    monkeypatch.setattr(ai_canon, "SessionLocal", sessionmaker(bind=engine))
    fake = FakeMusicBrainz().install(monkeypatch)

    def ask_now(items, priority=mb_answers.PRIORITY_PERSON, timeout=15.0):
        mb_answers.enqueue(items, priority)
        drain()
        return all((a := mb_answers.answer(*i)) and a.fresh for i in items)

    monkeypatch.setattr(mb_answers, "ask_now", ask_now)
    monkeypatch.setattr(ai_canon, "_provider",
                        lambda: SimpleNamespace(name="anthropic", models=lambda: ["claude-opus"]))
    with conn.cursor() as cur:
        Library(cur, "Jon").album("The Friends of Mr. Cairo", [("State of Independence", 470)])
        s = Slice(cur)
        s.artist(JON, "Jon")
        s.artist(OTHER_JON, "Jon")
        s.artist(ANDERSON, "Jon Anderson")
    fake.artist(JON, "Jon Anderson", aliases=["Jon"], disambiguation="Yes singer")
    yield conn, fake, ai_canon, monkeypatch
    conn.close()
    pool.closeall()
    engine.dispose()


def _model(monkeypatch, ai_canon, pick):
    prompts = []

    def ask(prompt, model, provider):
        prompts.append(prompt)
        return [{"n": 1, "pick": pick, "conf": "high", "why": "the folder"}]

    monkeypatch.setattr(ai_canon, "_ask", ask)
    return prompts


def _bound(cur):
    cur.execute("""SELECT a.name, am.mbid::text, am.confidence::text
                     FROM artist_mbids am JOIN artists a ON a.id = am.artist_id""")
    return cur.fetchall()


def test_the_model_sees_musicbrainz_answers_and_the_artist_takes_musicbrainz_name(node):
    conn, fake, ai_canon, monkeypatch = node
    prompts = _model(monkeypatch, ai_canon, JON)

    stats = ai_canon.ai_canonize()

    assert stats["canonized"] == 1
    assert f"GID={JON} Jon Anderson (Yes singer)" in prompts[0]
    assert OTHER_JON not in prompts[0]          # MusicBrainz does not know it
    with conn.cursor() as cur:
        assert _bound(cur) == [("Jon Anderson", JON, "ai")]


def test_a_gid_musicbrainz_did_not_answer_for_is_refused(node):
    conn, fake, ai_canon, monkeypatch = node
    _model(monkeypatch, ai_canon, OTHER_JON)

    stats = ai_canon.ai_canonize()

    assert stats["canonized"] == 0 and stats["skipped"] == 1
    with conn.cursor() as cur:
        assert _bound(cur) == []


def test_a_proposed_name_counts_only_when_musicbrainz_carries_it(node):
    conn, fake, ai_canon, monkeypatch = node
    _model(monkeypatch, ai_canon, "PROPOSE: Jon Anderson")
    fake.artist(ANDERSON, "Jon Anderson of Yes")   # MusicBrainz does not carry the proposed name

    assert ai_canon.ai_canonize()["canonized"] == 0
    fake.artist(ANDERSON, "Jon Anderson")
    import mb_answers
    from db_pool import db_execute
    db_execute("DELETE FROM mbws_answers WHERE mbid = %s::uuid", (ANDERSON,))
    assert mb_answers.answer("artist", ANDERSON) is None

    assert ai_canon.ai_canonize()["canonized"] == 1
    with conn.cursor() as cur:
        assert _bound(cur) == [("Jon Anderson", ANDERSON, "ai")]


def test_an_artist_whose_decision_came_to_wait_during_the_model_call_is_not_renamed(node):
    """The algorithmic canon queued a decision of the artist while the model
    was asked: keyed by the artist's id, it would wake nothing after a rename,
    so the AI's pick waits too."""
    import mb_answers
    conn, fake, ai_canon, monkeypatch = node
    with conn.cursor() as cur:
        cur.execute("SELECT id::text FROM artists WHERE name = 'Jon'")
        jon = cur.fetchone()[0]

    def ask(prompt, model, provider):
        mb_answers.enqueue([("release", mbid())], mb_answers.PRIORITY_OWNED, "owned_artist", jon)
        fake.down = True        # the decision's answer does not come during this run
        return [{"n": 1, "pick": JON, "conf": "high", "why": "the folder"}]
    monkeypatch.setattr(ai_canon, "_ask", ask)

    stats = ai_canon.ai_canonize()

    assert stats["canonized"] == 0 and stats["skipped"] == 1
    with conn.cursor() as cur:
        assert _bound(cur) == []
        cur.execute("SELECT name FROM artists WHERE id = %s", (jon,))
        assert cur.fetchone() == ("Jon",)
