"""A click in Discovery — or the assistant minting a search's top result — on
a node with catalogue slices and no MusicBrainz database mints the artist
only on MusicBrainz's word (backend/mb_discovery.mint): MusicBrainz must know
the MBID, and know it by the name the person clicked or the assistant was
asked for (case, accents and punctuation aside); the artist then carries
MusicBrainz's own name, a merged MBID its target's. A MusicBrainz that does not answer in time is a retry, never a
mint. On a real PostgreSQL built from the migrations, MusicBrainz faked —
skipped without a cluster."""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
for p in (REPO, REPO / "backend", REPO / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

pytest.importorskip("psycopg2")
pytest.importorskip("sqlalchemy")

import mb_fakes  # noqa: E402
from mb_fakes import FakeMusicBrainz, Slice, drain  # noqa: E402

G = "6b2e0000-0000-4000-8000-000000000001"
G_TARGET = "6b2e0000-0000-4000-8000-000000000002"


@pytest.fixture
def node(scratch_dsn, monkeypatch):
    conn, pool, engine = mb_fakes.slice_fed(scratch_dsn, monkeypatch)
    fake = FakeMusicBrainz().install(monkeypatch)
    import mb_answers

    def ask_now(items, priority=mb_answers.PRIORITY_PERSON, timeout=15.0):
        # The consumer's work done in the test's thread: what the waiting
        # click would see answered by the time it looks.
        items = list(items)
        mb_answers.enqueue(items, priority)
        drain()
        return all((a := mb_answers.answer(*i)) is not None and a.fresh for i in items)
    monkeypatch.setattr(mb_answers, "ask_now", ask_now)
    yield conn, fake
    conn.close()
    pool.closeall()
    engine.dispose()


def _slice_artist(conn, gid: str, name: str) -> None:
    with conn.cursor() as cur:
        Slice(cur).artist(gid, name)


def _artists(conn) -> list:
    with conn.cursor() as cur:
        cur.execute("""SELECT a.name, am.mbid::text, am.confidence::text FROM artists a
                         LEFT JOIN artist_mbids am ON am.artist_id = a.id ORDER BY a.name""")
        return cur.fetchall()


def test_a_click_mints_nothing_for_an_mbid_musicbrainz_does_not_know(node):
    conn, fake = node
    _slice_artist(conn, G, "Ghost Act")
    import mb_discovery
    assert mb_discovery.mint(G, artist_name="Ghost Act") == {"status": "unknown_artist"}
    assert _artists(conn) == []
    assert ("artist", G) in fake.asked


def test_a_click_mints_nothing_when_musicbrainz_does_not_know_the_name_shown(node):
    conn, fake = node
    _slice_artist(conn, G, "Sade")
    fake.artist(G, "A Different Artist Altogether")
    import mb_discovery
    assert mb_discovery.mint(G, artist_name="Sade") == {"status": "unknown_artist"}
    assert _artists(conn) == []


def test_a_click_mints_the_artist_under_musicbrainz_s_own_name(node):
    conn, fake = node
    _slice_artist(conn, G, "Sade Adu")
    fake.artist(G, "Sade", aliases=["Sade Adu"])
    import mb_discovery
    out = mb_discovery.mint(G, artist_name="Sade Adu")
    from uuid_utils import artist_uuid
    assert out["artist_id"] == str(artist_uuid("Sade"))
    assert _artists(conn) == [("Sade", G, "user")]


def test_a_merged_mbid_mints_the_artist_it_was_merged_into(node):
    conn, fake = node
    _slice_artist(conn, G, "Sade")
    fake.artist(G_TARGET, "Sade")
    fake.merge("artist", G, G_TARGET)
    import mb_discovery
    mb_discovery.mint(G, artist_name="Sade")
    assert _artists(conn) == [("Sade", G_TARGET, "user")]


def test_the_assistant_mints_a_top_result_only_under_the_name_it_was_asked_for(node):
    conn, fake = node
    _slice_artist(conn, G, "Radiohead")
    fake.artist(G, "Radiohead", aliases=["On a Friday"])
    import mb_discovery
    assert mb_discovery.mint(G, artist_name="Radiohead",
                             asked_name="Radiohedd") == {"status": "unknown_artist"}
    assert _artists(conn) == []
    mb_discovery.mint(G, artist_name="Radiohead", asked_name="on a friday")
    assert _artists(conn) == [("Radiohead", G, "user")]


def test_the_assistant_asked_for_a_name_without_its_marks_mints_the_artist(node):
    conn, fake = node
    _slice_artist(conn, G, "Björk")
    fake.artist(G, "Björk")
    import mb_discovery
    mb_discovery.mint(G, artist_name="Björk", asked_name="Bjork")
    assert _artists(conn) == [("Björk", G, "user")]


def test_a_name_is_matched_folded_as_identities_fold_and_unaccented():
    import mb_discovery
    bjork = {"name": "Björk", "aliases": [{"name": "Bjork Gudmundsdottir"}]}
    guns = {"name": "Guns N’ Roses", "aliases": []}
    sigur = {"name": "Sigur Rós", "aliases": []}
    assert mb_discovery._names_artist("Bjork", bjork)
    assert mb_discovery._names_artist("bjork  gudmundsdottir", bjork)
    assert mb_discovery._names_artist("Guns N Roses", guns)
    assert mb_discovery._names_artist("guns n' roses", guns)
    assert mb_discovery._names_artist("SIGUR ROS", sigur)
    assert not mb_discovery._names_artist("Bork", bjork)
    assert not mb_discovery._names_artist("Guns and Roses", guns)
    assert not mb_discovery._names_artist("", bjork)


def test_a_click_musicbrainz_does_not_answer_in_time_is_a_retry_not_a_mint(node):
    conn, fake = node
    _slice_artist(conn, G, "Sade")
    fake.down = True
    import mb_discovery
    out = mb_discovery.mint(G, artist_name="Sade")
    assert out["status"] == "rate_limited" and out["retry_after"] > 0
    assert _artists(conn) == []
