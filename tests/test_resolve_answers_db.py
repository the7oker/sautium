"""What steers the stream a phantom slot accepts — and so whose audio this
node analyses and signs — on a node with catalogue slices and no MusicBrainz
database comes from MusicBrainz's answers alone (backend/routers/player.py):
the alternate names are its aliases for the one MBID the local artist is
bound to, the performers its credit for the album's group, the barcodes those
of the editions it answered for. A slice's aliases, credits and barcodes
reach none of them. On a real PostgreSQL built from the migrations,
MusicBrainz faked — skipped without a cluster."""

import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
for p in (REPO, REPO / "backend", REPO / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

pytest.importorskip("psycopg2")
pytest.importorskip("sqlalchemy")

import mb_fakes  # noqa: E402
from mb_fakes import FakeMusicBrainz, Slice, credit, drain  # noqa: E402

NAME = "Contemporary Noise Ensemble"
A = "8d4a0000-0000-4000-8000-000000000001"
COMPOSER = "8d4a0000-0000-4000-8000-000000000002"
ENSEMBLE = "8d4a0000-0000-4000-8000-000000000003"
RG = "8d4a0000-0000-4000-8000-000000000011"
REL = "8d4a0000-0000-4000-8000-000000000021"
REC = "8d4a0000-0000-4000-8000-000000000031"


@pytest.fixture
def node(scratch_dsn, monkeypatch):
    conn, pool, engine = mb_fakes.slice_fed(scratch_dsn, monkeypatch)
    fake = FakeMusicBrainz().install(monkeypatch)
    from routers import player
    yield conn, fake, player
    conn.close()
    pool.closeall()
    engine.dispose()


def _answer(*items):
    import mb_answers
    mb_answers.enqueue(items, mb_answers.PRIORITY_DISCOVERY)
    assert drain() == len(items)


def test_alternate_names_are_musicbrainz_s_aliases_for_the_bound_mbid(node):
    conn, fake, player = node
    from uuid_utils import artist_uuid
    with conn.cursor() as cur:
        aid = str(artist_uuid(NAME))
        cur.execute("INSERT INTO artists (id, name) VALUES (%s, %s)", (aid, NAME))
        cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) "
                    "VALUES (%s, %s, 'overlap_verified')", (A, aid))
        Slice(cur).artist(A, NAME, aliases=["Contemporary Noise Quintet"])
    fake.artist(A, NAME, aliases=["Contemporary Noise Sextet"])

    assert player._artist_alts(NAME) == ()
    _answer(("artist", A))
    assert player._artist_alts(NAME) == ("Contemporary Noise Sextet",)


def test_performers_and_barcodes_are_musicbrainz_s_answers_for_the_group(node):
    conn, fake, player = node
    album = str(uuid.uuid4())
    with conn.cursor() as cur:
        s = Slice(cur)
        composer = s.artist(COMPOSER, "Steve Reich")
        ensemble = s.artist(ENSEMBLE, "Slice Ensemble")
        c = s.credit((composer, "Steve Reich"), (ensemble, "Slice Ensemble"))
        cur.execute("UPDATE mb_artist_credit_name SET join_phrase = '; ' WHERE artist_credit = %s "
                    "AND position = 0", (c,))
        g = s.release_group(RG, "Music for 18 Musicians", c)
        s.release(REL, "Music for 18 Musicians", g, c, [("Pulses", 300000, REC)])
        cur.execute("UPDATE mb_release SET barcode = '111111111111' WHERE gid = %s", (REL,))
        cur.execute("INSERT INTO albums (id, title, musicbrainz_id) VALUES (%s, %s, %s)",
                    (album, "Music for 18 Musicians", RG))
        track = str(uuid.uuid4())
        cur.execute("INSERT INTO tracks (id, title) VALUES (%s, 'Pulses')", (track,))
        cur.execute("INSERT INTO album_tracks (album_id, track_id, position, recording_mbid, length_ms) "
                    "VALUES (%s, %s, 1, %s, 300000)", (album, track, REC))
    members = credit(("Steve Reich", COMPOSER, "; "), ("Ensemble Signal", ENSEMBLE))
    fake.release_group(RG, "Music for 18 Musicians", members)
    fake.release(REL, "Music for 18 Musicians", RG, members,
                 [{"title": "Pulses", "length": 300000, "recording": REC}],
                 barcode="222222222222", media_format="Digital Media")

    assert player._album_performers([album]) == {}
    assert player._album_barcodes([album]) == {}
    _answer(("release_group", RG), ("release", REL))
    assert player._album_performers([album]) == {album: ("Ensemble Signal",)}
    assert player._album_barcodes([album]) == {album: ("222222222222",)}
