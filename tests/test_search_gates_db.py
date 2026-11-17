"""Discovery's year and quality gates on a real database (SEARCH-GATES,
2026-11-17): sql_queries.YEAR_GATE / QUALITY_GATE through the engine
(routers.discovery.run_search, what the Web UI and both assistant surfaces
call) and through the CLI's track filter (track_filter.get_filtered_track_ids).

- Each gate alone and with a text query, open year ranges included.
- A track's tier is its best copy's: a mixed-format album, a track owned as
  MP3 and as 24/44.1, DSD, a copy only at the HQPlayer.
- A phantom track has no tier: no quality gate passes it, under any corpus;
  the year gate reaches it through its tracklist.
- The CLI and the Web UI give the same rows.
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

# title → (album, [(format, lossless, sample_rate, bit_depth), ...]) for the
# owned tracks; every copy of one track sits in its own variant of the album.
OWNED = {
    "Alpha Line":  ("Mixed", [("FLAC", True, 44100, 16)]),
    "Bravo":       ("Mixed", [("MP3", False, 44100, None)]),
    "Charlie":     ("Mixed", [("FLAC", True, 96000, 24)]),
    "Delta":       ("Mixed", [("MP3", False, 44100, None), ("FLAC", True, 44100, 24)]),
    "Echo":        ("Dsd", [("DSF", True, 2822400, 1)]),
    "Hotel":       ("Undated", [("FLAC", True, 48000, 16)]),
}
AT_HQPLAYER = {"Foxtrot": ("Dsd", ("FLAC", True, 44100, 16))}
PHANTOM = {"Alpha Waves": "Phantom"}
YEARS = {"Mixed": 1975, "Dsd": 1982, "Undated": None, "Phantom": 1977}

TIER = {"Alpha Line": "lossless", "Bravo": "lossy", "Charlie": "hi-res", "Delta": "hi-res",
        "Echo": "hi-res", "Hotel": "lossless", "Foxtrot": "lossless"}


@pytest.fixture(scope="module")
def library(scratch_dsn):
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    cur = conn.cursor()
    artist = str(uuid.uuid4())
    cur.execute("INSERT INTO artists (id, name) VALUES (%s, 'The Gate Band')", (artist,))
    albums = {}
    for title, year in YEARS.items():
        albums[title] = str(uuid.uuid4())
        cur.execute("INSERT INTO albums (id, title, release_year) VALUES (%s, %s, %s)",
                    (albums[title], title, year))
        cur.execute("INSERT INTO album_artists (album_id, artist_id, role) "
                    "VALUES (%s, %s, 'primary')", (albums[title], artist))
    cur.execute("INSERT INTO hqp_endpoints (name) VALUES ('box') RETURNING id")
    endpoint = cur.fetchone()[0]
    tracks = {}

    def track(title: str) -> str:
        tracks[title] = str(uuid.uuid4())
        cur.execute("INSERT INTO tracks (id, title, title_latin) VALUES (%s, %s, %s)",
                    (tracks[title], title, latinize(title)))
        cur.execute("INSERT INTO track_artists (track_id, artist_id, role) "
                    "VALUES (%s, %s, 'primary')", (tracks[title], artist))
        return tracks[title]

    def variant(album: str, path: str, hqp=None) -> int:
        cur.execute("INSERT INTO album_variants (album_id, directory_path, location, "
                    "hqp_endpoint_id) VALUES (%s, %s, %s, %s) RETURNING id",
                    (albums[album], path, "hqplayer" if hqp else "local", hqp))
        return cur.fetchone()[0]

    for title, (album, copies) in OWNED.items():
        tid = track(title)
        for n, (fmt, lossless, rate, bits) in enumerate(copies):
            av = variant(album, f"/lib/{album}/{title}/{n}")
            cur.execute("INSERT INTO media_files (track_id, album_variant_id, file_path, "
                        "file_format, is_lossless, sample_rate, bit_depth, track_number, "
                        "duration_seconds) VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 200)",
                        (tid, av, f"/lib/{album}/{title}/{n}/{title}.{fmt.lower()}", fmt, lossless,
                         rate, bits))
    for title, (album, (fmt, lossless, rate, bits)) in AT_HQPLAYER.items():
        tid = track(title)
        av = variant(album, f"/hqp/{album}", hqp=endpoint)
        cur.execute("INSERT INTO hqp_library_files (track_id, album_variant_id, hqp_path, "
                    "file_format, is_lossless, sample_rate, bit_depth, duration_seconds) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, 200)",
                    (tid, av, f"/hqp/{album}/{title}.flac", fmt, lossless, rate, bits))
    for n, (title, album) in enumerate(PHANTOM.items()):
        tid = track(title)
        cur.execute("INSERT INTO album_tracks (album_id, track_id, position) VALUES (%s, %s, %s)",
                    (albums[album], tid, n + 1))
    conn.close()
    return {"tracks": tracks, "albums": albums}


@pytest.fixture(scope="module")
def node(scratch_dsn, library):
    """run_search and the CLI's sessions on the module's database."""
    database = pytest.importorskip("database")
    from sqlalchemy.orm import sessionmaker
    engine = database.make_engine(scratch_dsn)
    mp = pytest.MonkeyPatch()
    mp.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    from routers import discovery
    yield discovery, sessionmaker(bind=engine)
    mp.undo()
    engine.dispose()


def _titles(node, target="track", **params) -> set:
    discovery, _ = node
    out = discovery.run_search(target=target, limit=50, **params)
    key = {"track": "title", "album": "album", "artist": "artist"}[target]
    return {r[key] for r in out["results"]}


def _tier(names, *tiers) -> set:
    return {n for n in names if TIER[n] in tiers}


EVERY_OWNED = set(OWNED) | set(AT_HQPLAYER)


@pytest.mark.parametrize("corpus", ["all", "owned"])
def test_each_tier_is_the_best_copy_owned(node, corpus):
    assert _titles(node, quality="hi-res", corpus=corpus) == {"Charlie", "Delta", "Echo"}
    assert _titles(node, quality="lossless", corpus=corpus) == _tier(
        EVERY_OWNED, "lossless", "hi-res")
    # Delta's MP3 is not its best copy: only a track with no lossless copy is lossy
    assert _titles(node, quality="lossy", corpus=corpus) == {"Bravo"}


def test_a_phantom_has_no_tier_under_any_corpus(node):
    for tier in ("lossy", "lossless", "hi-res"):
        assert _titles(node, quality=tier, corpus="phantom") == set()
        assert "Alpha Waves" not in _titles(node, q="alpha", quality=tier, corpus="all")
    assert _titles(node, q="alpha", corpus="all") >= {"Alpha Line", "Alpha Waves"}


def test_the_year_range_alone_and_open_at_either_end(node):
    """An album-level gate alone answers at the album's grain and above, as the
    genre gate does; a track-level tool beside it brings the tracks."""
    assert _titles(node, "album", year_from=1970, year_to=1979, corpus="all") == {
        "Mixed", "Phantom"}
    assert _titles(node, "album", year_from=1980, corpus="all") == {"Dsd"}
    assert _titles(node, "album", year_to=1976, corpus="all") == {"Mixed"}
    assert _titles(node, "album", year_from=1977, year_to=1977, corpus="phantom") == {"Phantom"}
    assert "Undated" not in _titles(node, "album", year_from=0, year_to=9999, corpus="all")
    assert _titles(node, "artist", year_to=1976, corpus="all") == {"The Gate Band"}
    assert _titles(node, year_from=1970, corpus="all") == set()
    assert _titles(node, year_from=1980, quality="lossless", corpus="all") == {
        "Echo", "Foxtrot"}


def test_each_gate_with_text(node):
    assert _titles(node, q="alpha", year_from=1970, year_to=1979, corpus="all") == {
        "Alpha Line", "Alpha Waves"}
    assert _titles(node, q="alpha", year_from=1980, corpus="all") == set()
    assert _titles(node, q="alpha", quality="lossless", corpus="all") == {"Alpha Line"}
    assert _titles(node, q="alpha", quality="hi-res", corpus="all") == set()
    assert _titles(node, q="delta", quality="hi-res", year_to=1979, corpus="all") == {"Delta"}


def test_a_mixed_format_album_holds_each_tier_its_tracks_hold(node):
    for tier in ("lossy", "lossless", "hi-res"):
        assert "Mixed" in _titles(node, "album", quality=tier, corpus="all"), tier
    assert _titles(node, "album", quality="hi-res", corpus="all") == {"Mixed", "Dsd"}
    assert _titles(node, "album", quality="lossy", corpus="all") == {"Mixed"}
    assert _titles(node, "album", year_from=1976, corpus="all") == {"Dsd", "Phantom"}
    assert _titles(node, "artist", quality="hi-res", corpus="all") == {"The Gate Band"}


@pytest.mark.parametrize("gates", [
    {"quality": "lossy"},
    {"quality": "lossless"},
    {"quality": "hi-res"},
    {"quality": "lossless", "year_from": 1980},
    {"quality": "lossless", "year_to": 1976},
])
def test_the_cli_and_the_web_ui_give_the_same_rows(node, library, gates):
    track_filter = pytest.importorskip("track_filter")
    discovery, sessions = node
    with sessions() as db:
        cli = {str(t) for t in track_filter.get_filtered_track_ids(db, **gates)}
    web = {r["track_id"] for r in
           discovery.run_search(target="track", limit=50, corpus="owned", **gates)["results"]}
    assert cli == web and cli


@pytest.mark.parametrize("years", [(1970, 1979), (1980, None), (None, 1976)])
def test_the_cli_years_are_the_web_uis_albums(node, library, years):
    """A year range alone answers albums in Discovery: the CLI's tracks are
    those albums' owned tracks."""
    track_filter = pytest.importorskip("track_filter")
    discovery, sessions = node
    lo, hi = years
    with sessions() as db:
        cli = {str(t) for t in track_filter.get_filtered_track_ids(db, year_from=lo, year_to=hi)}
    albums = {r["album"] for r in discovery.run_search(
        target="album", limit=50, corpus="owned", year_from=lo, year_to=hi)["results"]}
    owned = {**{n: a for n, (a, _c) in OWNED.items()},
             **{n: a for n, (a, _c) in AT_HQPLAYER.items()}}
    assert cli == {library["tracks"][n] for n, a in owned.items() if a in albums} and cli


def test_the_cli_reads_the_options_into_the_same_filter(node, library, monkeypatch):
    click_testing = pytest.importorskip("click.testing")
    import track_filter
    seen = {}

    @track_filter.track_filter_options
    def command(filters):
        seen.update(filters)

    import click
    click_testing.CliRunner().invoke(
        click.command()(command), ["--year-from", "1970", "--year-to", "1979",
                                   "--quality", "hi-res"], catch_exceptions=False)
    assert seen["year_from"] == 1970 and seen["year_to"] == 1979 and seen["quality"] == "hi-res"
    _, sessions = node
    with sessions() as db:
        ids = {str(t) for t in track_filter.get_filtered_track_ids(db, **seen)}
    assert ids == {library["tracks"][n] for n in ("Charlie", "Delta")}


def test_the_delta_lands_on_a_node_that_ran_the_earlier_files(scratch_dsn, library):
    """063 on the view 027/028 left: the quality columns appended, the rows kept."""
    old = (REPO / "desktop/migrations/028_hqp_recording_mbid.sql").read_text()
    old_view = old[old.index("CREATE OR REPLACE VIEW"):old.index("END IF;")]
    conn = psycopg2.connect(scratch_dsn)
    try:
        cur = conn.cursor()
        cur.execute("DROP VIEW owned_files CASCADE")      # rolled back below
        cur.execute(old_view)
        cur.execute("SELECT count(*) FROM owned_files")
        rows = cur.fetchone()[0]
        cur.execute((REPO / "desktop/migrations/063_owned_files_quality.sql").read_text())
        cur.execute("SELECT count(*), count(*) FILTER (WHERE is_lossless), "
                    "max(sample_rate) FROM owned_files")
        assert cur.fetchone() == (rows, rows - 2, 2822400)     # Bravo's and Delta's MP3s
        cur.execute((REPO / "desktop/migrations/063_owned_files_quality.sql").read_text())
    finally:
        conn.rollback()
        conn.close()
