""" "New in my collection" orders by entry into the library
(album_variants.entered_at; PI-NODE-2D item 3, F15).

The shelf ordered albums by their files' birth time alone. On a box an exFAT
stick keeps the day its files were copied onto it and ntfs-3g keeps none, so
the stick scanned sixteen minutes after another sorted two days before it.
Now a copy carries the start of the scan round (or HQPlayer sync) that made
its row; the birth time orders the copies of one round.

On a real PostgreSQL — the module's scratch database (conftest.scratch_dsn),
the pool and the SQLAlchemy sessions pointed at it. Skipped where there is no
cluster.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("sqlalchemy")

import psycopg2.pool  # noqa: E402

import scanner  # noqa: E402
from routers import home  # noqa: E402
from tests import conftest  # noqa: E402

MIGRATIONS = REPO / "desktop" / "migrations"
DELTA = next(MIGRATIONS.glob("*_shelf_entered.sql"))
NOW = datetime.now(timezone.utc).replace(microsecond=0)
ROOT = "/music"
BOX = ("192.0.2.53", 4321)
BOX_INFO = {"name": "HQPlayerEmbedded", "product": "Signalyst HQPlayer Embedded",
            "version": "6", "engine": "6.2.3", "platform": "Linux"}
BOX_LIBRARY = """<?xml version="1.0" encoding="utf-8"?><LibraryGet>\
<LibraryDirectory album="Both" artist="Aurora" bitrate="1411" bits="16" channels="2" date="1994" \
genre="Ambient" hash="d1" path="/smb/Both" rate="44100">\
<LibraryFile hash="f1" length="200" name="01. Both I.flac" number="1" song="Both I"/>\
</LibraryDirectory>\
<LibraryDirectory album="Box Only" artist="Aurora" bitrate="1411" bits="16" channels="2" date="1995" \
genre="Ambient" hash="d2" path="/smb/Box Only" rate="44100">\
<LibraryFile hash="f2" length="200" name="01. Box Only I.flac" number="1" song="Box Only I"/>\
</LibraryDirectory></LibraryGet>
"""

# The shelf's key until 2026-11-24, as routers/home.py ordered by it.
OLD_ORDER = """
    SELECT al.title FROM albums al JOIN album_variants av ON av.album_id = al.id
     GROUP BY al.id, al.title
     ORDER BY COALESCE(MAX(av.arrived_at) FILTER (WHERE av.location = 'local'),
                       MAX(av.arrived_at)) DESC NULLS LAST, al.id DESC"""


@pytest.fixture
def cur(scratch_dsn, monkeypatch):
    import database
    import db_pool
    from sqlalchemy.orm import sessionmaker
    pool = psycopg2.pool.ThreadedConnectionPool(1, 4, dsn=scratch_dsn, options="-c timezone=UTC")
    engine = database.make_engine(scratch_dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    monkeypatch.setattr(database, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    conn = psycopg2.connect(scratch_dsn, options="-c timezone=UTC")
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute("TRUNCATE tracks, albums, artists, genres, user_settings, hqp_endpoints CASCADE")
        yield c
    conn.close()
    pool.closeall()
    engine.dispose()


def _album(title, born=None, files=1, folder=None):
    """What extract_metadata hands the import for an album's files: `born`
    is the files' birth on the disk, None where the file system keeps none
    (the arrival is then the first read)."""
    d = f"{ROOT}/{folder or title}"
    return [(d, {"file_path": f"{d}/{n:02d}.flac", "title": f"{title} {n}", "artist": "Aurora",
                 "album_artist": "Aurora", "album": title, "track_number": n, "disc_number": 1,
                 "duration_seconds": 200.0, "file_format": "FLAC", "is_lossless": True,
                 "sample_rate": 44100, "bit_depth": 16, "arrived_at": born})
            for n in range(1, files + 1)]


def _round(started, *albums):
    """One round of the scan queue, as scan_and_import hands it to the import."""
    scanner.import_metadata([entry for album in albums for entry in album],
                            sink=scanner.LOCAL_FILES, stats={}, entered_at=started)


def _shelf(limit=50, **cursor):
    return home.get_new_in_library(limit=limit, before=cursor.get("before"),
                                   before_arrived=cursor.get("before_arrived"),
                                   before_id=cursor.get("before_id"))


def _titles(**kw):
    return [a["title"] for a in _shelf(**kw)["albums"]]


def test_what_was_scanned_later_is_newer_whatever_its_files_say(cur):
    """The box's case: the first stick's file system keeps no birth time, the
    second's keeps the day it was filled — two days before."""
    first = NOW - timedelta(minutes=16)
    _round(first, _album("First Stick", born=None))
    cur.execute("UPDATE media_files SET arrived_at = %s", (first + timedelta(seconds=5),))
    _round(NOW, _album("Second Stick", born=NOW - timedelta(days=2)))

    assert _titles() == ["Second Stick", "First Stick"]
    # the cold-start Recommendations are the same order
    assert [a["title"] for a in home._cold_start_albums(10)] == ["Second Stick", "First Stick"]


def test_the_copies_of_one_round_are_in_the_order_their_files_came(cur):
    """A first scan of a whole library is one round, walked in the
    alphabet: the files' own birth is the only word on what came last."""
    _round(NOW, _album("A Oldest", born=NOW - timedelta(days=30)),
           _album("B Newest", born=NOW - timedelta(days=1)),
           _album("C Middle", born=NOW - timedelta(days=7)))
    assert _titles() == ["B Newest", "C Middle", "A Oldest"]
    cur.execute("SELECT count(DISTINCT entered_at), min(entered_at) FROM album_variants")
    assert cur.fetchone() == (1, NOW)


def test_the_cursor_pages_a_round_of_one_instant_without_a_duplicate_or_a_gap(cur):
    """Thirty copies of one round on a file system with no birth time, read
    in one statement's instant: the album's id is all that tells them
    apart, and the cursor carries the whole key."""
    born = NOW - timedelta(days=3)
    _round(NOW - timedelta(hours=1), _album("An Earlier Round", born=NOW))
    _round(NOW, *[_album(f"Tie {n:02d}", born=born) for n in range(30)])
    whole = _shelf(limit=50)
    assert len(whole["albums"]) == 31 and whole["next_cursor"] is None
    assert whole["albums"][-1]["title"] == "An Earlier Round"

    paged, cursor, pages = [], {}, 0
    while True:
        page = _shelf(limit=7, **cursor)
        paged += [a["id"] for a in page["albums"]]
        pages += 1
        if page["next_cursor"] is None:
            break
        assert sorted(page["next_cursor"]) == ["before", "before_arrived", "before_id"]
        cursor = page["next_cursor"]
    assert paged == [a["id"] for a in whole["albums"]] and pages == 5
    # a row of the shelf, as the page reads it
    assert sorted(whole["albums"][0]) == ["artist", "cover_id", "cover_url", "id", "location",
                                           "media_file_id", "title", "year"]


def test_a_cursor_is_whole_or_absent(cur):
    from fastapi import HTTPException
    for part in ({"before": NOW.isoformat()},
                 {"before": NOW.isoformat(), "before_id": "0a0b0c0d-0000-4000-8000-00000000000a"},
                 {"before_arrived": NOW.isoformat()}):
        with pytest.raises(HTTPException) as refused:
            _shelf(**part)
        assert refused.value.status_code == 400


def test_files_that_join_a_copy_later_do_not_make_it_new(cur):
    _round(NOW - timedelta(hours=2), _album("Grows Later", born=NOW - timedelta(days=9)))
    _round(NOW - timedelta(hours=1), _album("In Between", born=NOW - timedelta(days=8)))
    more = _album("Grows Later", born=NOW, files=2)[1:]
    _round(NOW, more)

    assert _titles() == ["In Between", "Grows Later"]
    cur.execute("SELECT entered_at FROM album_variants av JOIN albums al ON al.id = av.album_id "
                "WHERE al.title = 'Grows Later'")
    assert cur.fetchall() == [(NOW - timedelta(hours=2),)]


def test_a_copy_made_again_at_another_path_enters_again(cur):
    _round(NOW - timedelta(hours=2), _album("Moved", born=NOW - timedelta(days=9)))
    _round(NOW - timedelta(hours=1), _album("In Between", born=NOW - timedelta(days=8)))
    _round(NOW, _album("Moved", born=NOW - timedelta(days=9), folder="Elsewhere/Moved"))
    assert _titles() == ["Moved", "In Between"]


def test_an_album_only_at_the_hqplayer_is_dated_by_its_syncs_round(cur, monkeypatch):
    """A sync is a round of its own: its copies carry its start. A copy of an
    album that is on disk is not news — the album keeps its local copy's
    place."""
    import hqp_library
    hour = NOW - timedelta(hours=1)
    _round(hour, _album("Both", born=hour - timedelta(minutes=10)),
           _album("Local Only", born=hour - timedelta(minutes=20)))
    _round(NOW - timedelta(minutes=30), _album("Later Local", born=hour - timedelta(days=5)))
    monkeypatch.setattr(hqp_library, "library_hash", lambda h, p: "h1")
    monkeypatch.setattr(hqp_library, "fetch_library", lambda h, p: BOX_LIBRARY)
    monkeypatch.setattr(hqp_library, "get_info", lambda h, p: BOX_INFO)

    before = datetime.now(timezone.utc)
    assert hqp_library.sync(*BOX)["added"] == 2
    after = datetime.now(timezone.utc)

    shelf = _shelf()["albums"]
    assert [(a["title"], a["location"]) for a in shelf] == [
        ("Box Only", "hqplayer"), ("Later Local", "local"), ("Both", "local"), ("Local Only", "local")]
    cur.execute("SELECT DISTINCT entered_at FROM album_variants WHERE location = 'hqplayer'")
    (entered,), = cur.fetchall()
    assert before <= entered <= after


def _old_node(cur):
    """The copies as a node from before the column holds them."""
    cur.execute("ALTER TABLE album_variants DROP COLUMN entered_at")
    cur.execute("CREATE INDEX idx_album_variants_arrived_at_desc "
                "ON album_variants(arrived_at DESC NULLS LAST)")


def test_the_backfill_leaves_a_shelf_in_the_order_it_had(cur):
    """The delta on a node from before the column: every copy enters at its
    arrival, so nothing reshuffles under a person at the update."""
    for n, days in enumerate((12, 3, 40, 7, 21)):
        _round(NOW - timedelta(days=days) + timedelta(hours=1),
               _album(f"Album {n}", born=NOW - timedelta(days=days)))
    # one held only at an HQPlayer, and one whose copy there is newer than its rip
    cur.execute("INSERT INTO hqp_endpoints (name) VALUES ('box') RETURNING id")
    endpoint = cur.fetchone()[0]
    cur.execute("INSERT INTO albums (id, title) VALUES ('0a0b0c0d-0000-4000-8000-00000000000a', 'Box Only')")
    cur.execute("""INSERT INTO album_variants (album_id, directory_path, location, hqp_endpoint_id, arrived_at)
                   SELECT id, '/smb/' || title, 'hqplayer', %s, %s FROM albums
                    WHERE title IN ('Box Only', 'Album 2')""", (endpoint, NOW - timedelta(days=5)))
    _old_node(cur)
    try:
        cur.execute(OLD_ORDER)
        before = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT id, arrived_at FROM album_variants ORDER BY id")
        arrivals = cur.fetchall()
    finally:
        cur.execute(DELTA.read_text(encoding="utf-8"))

    assert before == ["Album 1", "Box Only", "Album 3", "Album 0", "Album 4", "Album 2"]
    assert _titles() == before
    cur.execute("SELECT id, entered_at FROM album_variants ORDER BY id")
    assert cur.fetchall() == arrivals


def _objects(conn):
    """The catalogue's own text of what the delta makes."""
    with conn.cursor() as c:
        c.execute("""SELECT column_name, udt_name, is_nullable, column_default
                       FROM information_schema.columns
                      WHERE table_schema = 'public' AND table_name = 'album_variants' ORDER BY 1""")
        columns = c.fetchall()
        c.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'album_variants' ORDER BY 1")
        return {"columns": columns, "indexes": c.fetchall()}


def test_the_baseline_and_its_delta_make_the_same_column_and_index(scratch_dsn):
    """001 alone, a node from before the column brought up by the delta, and
    001 with every delta hold the same objects; the delta again is a no-op."""
    name = "sautium_test_shelf_entered_baseline"
    assert conftest._recreate(name, create=True)
    try:
        c = psycopg2.connect(dbname=name, **conftest.PG)
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute((MIGRATIONS / "001_initial.sql").read_text(encoding="utf-8"))
            alone = _objects(c)
            _old_node(cur)
            for _ in range(2):
                cur.execute(DELTA.read_text(encoding="utf-8"))
                assert _objects(c) == alone
        c.close()
    finally:
        conftest._recreate(name, create=False)
    c = psycopg2.connect(scratch_dsn)
    try:
        assert _objects(c) == alone
    finally:
        c.close()
    assert ("entered_at", "timestamptz", "NO", "CURRENT_TIMESTAMP") in alone["columns"]
    names = [name for name, _def in alone["indexes"]]
    assert "idx_album_variants_entered_desc" in names and "idx_album_variants_arrived_at_desc" not in names


def test_a_page_of_the_shelf_reads_a_page_of_its_index(cur, monkeypatch):
    """4 000 copies in 40 rounds, a tenth of the albums with a second copy:
    the first page and a later one walk idx_album_variants_entered_desc and
    sort nothing but what they return."""
    cur.execute("""
        WITH made AS (
            INSERT INTO albums (id, title)
            SELECT gen_random_uuid(), 'Album ' || n FROM generate_series(1, 3600) n RETURNING id, title)
        INSERT INTO album_variants (album_id, directory_path, entered_at, arrived_at)
        SELECT id, '/music/' || title, %(now)s::timestamptz - mod(n, 40) * interval '1 day',
               %(now)s::timestamptz - n * interval '1 hour'
          FROM (SELECT id, title, row_number() OVER () AS n FROM made) m""", {"now": NOW})
    cur.execute("""
        INSERT INTO album_variants (album_id, directory_path, entered_at, arrived_at)
        SELECT album_id, directory_path || ' (2)', entered_at + interval '50 days', arrived_at
          FROM album_variants WHERE mod(id, 9) = 0""")
    cur.execute("ANALYZE albums, album_variants")
    run, plans = home.db_query, []

    def explained(sql, params=None):
        plans.append("\n".join(r["QUERY PLAN"] for r in run("EXPLAIN " + sql, params)))
        return run(sql, params)

    monkeypatch.setattr(home, "db_query", explained)
    first = _shelf(limit=20)
    second = _shelf(limit=20, **first["next_cursor"])
    assert len({a["id"] for a in first["albums"] + second["albums"]}) == 40
    for plan in plans:
        assert "Index Scan using idx_album_variants_entered_desc on album_variants av" in plan, plan
        assert "Seq Scan on album_variants" not in plan, plan
    assert "Index Cond: (ROW(" in plans[1], plans[1]
