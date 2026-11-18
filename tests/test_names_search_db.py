"""The names search's whole-word arm (CREDITS-1 step 2a, DESIGN §3.1 and Q6)
through routers.discovery.run_search — what the Web UI and both assistant
surfaces call — on a real database.

A surname inside a long full name falls under the trigram floor: "bach" ~
"johann sebastian bach" is 0.23, "mozart" ~ "wolfgang amadeus mozart" 0.29,
and neither is a prefix. The arm finds a whole word of a name (pg_trgm's
strict word similarity at WORD_ARM_MIN), scored below a prefix and an exact
name, and lifts no word fragment: what trigrams found before keeps its score.
Skipped without a cluster."""

import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")

from transliterate import latinize  # noqa: E402

ARTISTS = (
    "Johann Sebastian Bach", "Carl Philipp Emanuel Bach", "Wolfgang Amadeus Mozart",
    "Bachman-Turner Overdrive", "Burt Bacharach", "Jacques Offenbach",
    "Philip Glass", "Steve Reich", "Bach", "Dmitri Shostakovich",
)
# An artist named in another script, found by a reading of it (0b's aliases)
ALIASED = ("Иоганн Себастьян Бах", "johann sebastian bach")


def _norm(raw: float) -> float:
    """The artist_name source's calibrated score of a raw one (floor 0.3)."""
    return round(max(0.0, min(1.0, (raw - 0.3) / 0.7)), 4)


@pytest.fixture(scope="module")
def library(scratch_dsn):
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    cur = conn.cursor()
    album = str(uuid.uuid4())
    cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'Recital')", (album,))
    cur.execute("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, '/lib/r') "
                "RETURNING id", (album,))
    variant = cur.fetchone()[0]
    ids = {}
    for n, name in enumerate(ARTISTS + ALIASED[:1], 1):
        ids[name] = str(uuid.uuid4())
        cur.execute("INSERT INTO artists (id, name, name_latin) VALUES (%s, %s, %s)",
                    (ids[name], name, latinize(name)))
        track = str(uuid.uuid4())
        cur.execute("INSERT INTO tracks (id, title, title_latin) VALUES (%s, %s, %s)",
                    (track, f"Piece {n}", f"piece {n}"))
        cur.execute("INSERT INTO track_artists (track_id, artist_id, role) "
                    "VALUES (%s, %s, 'primary')", (track, ids[name]))
        cur.execute("INSERT INTO media_files (track_id, album_variant_id, file_path, file_format, "
                    "track_number, duration_seconds) VALUES (%s, %s, %s, 'FLAC', %s, 200)",
                    (track, variant, f"/lib/r/{n}.flac", n))
    cur.execute("INSERT INTO artist_name_aliases (artist_id, alias_latin) VALUES (%s, %s)",
                (ids[ALIASED[0]], ALIASED[1]))
    conn.close()
    return ids


@pytest.fixture(scope="module")
def search(scratch_dsn, library):
    database = pytest.importorskip("database")
    from sqlalchemy.orm import sessionmaker
    engine = database.make_engine(scratch_dsn)
    mp = pytest.MonkeyPatch()
    mp.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    from routers import discovery

    def run(q, corpus="all"):
        out = discovery.run_search(q=q, target="artist", limit=50, corpus=corpus)
        return {r["artist"]: r["similarity"] for r in out["results"]}
    yield run
    mp.undo()
    engine.dispose()


@pytest.mark.parametrize("corpus", ["all", "owned"])
def test_a_surname_finds_the_full_name(search, corpus):
    hits = search("Bach", corpus)
    for name in ("Johann Sebastian Bach", "Carl Philipp Emanuel Bach"):
        assert hits[name] == _norm(0.8)
    assert search("Mozart", corpus)["Wolfgang Amadeus Mozart"] == _norm(0.8)
    assert search("glass", corpus)["Philip Glass"] == _norm(0.8)
    assert search("Reich", corpus)["Steve Reich"] == _norm(0.8)


def test_an_exact_name_and_a_prefix_rank_above_a_whole_word(search):
    hits = search("bach")
    assert hits["Bach"] == 1.0
    assert hits["Bachman-Turner Overdrive"] == _norm(0.85)
    assert hits["Bach"] > hits["Bachman-Turner Overdrive"] > hits["Johann Sebastian Bach"]


def test_a_word_fragment_is_not_lifted(search, scratch_dsn):
    from discovery_engine import WORD_ARM_MIN, _word_arm
    hits = search("bach")
    # "offenbach": trigram 0.25, strict word 0.25 — found by neither
    assert "Jacques Offenbach" not in hits and "Burt Bacharach" not in hits
    # "bacharach": its strict word similarity (0.56) is under the threshold,
    # so the arm scores it nothing where trigrams retrieve it
    conn = psycopg2.connect(scratch_dsn)
    with conn, conn.cursor() as cur:
        cur.execute("SET LOCAL pg_trgm.strict_word_similarity_threshold = %s", (WORD_ARM_MIN,))
        arm = _word_arm("n").replace("%", "%%").replace(":ql", "%(ql)s")
        cur.execute(f"SELECT n, {arm} FROM (VALUES ('burt bacharach'), "
                    "('johann sebastian bach')) v(n)", {"ql": "bach"})
        assert dict(cur.fetchall()) == pytest.approx({"burt bacharach": 0, "johann sebastian bach": 0.8})
    conn.close()


def test_an_alias_reading_finds_by_a_whole_word(search):
    assert search("bach")[ALIASED[0]] == _norm(0.8)
