"""The HQPlayer library as a source of album variants (backend/hqp_library.py).

The parser runs on a hand-written LibraryGet answer; the sync runs on a real
PostgreSQL — a throwaway database built from the migrations, the pool and
the sessions pointed at it — after a local scan minted the same album, so
the copy at the HQPlayer must land on the existing tracks as a second
variant. Skipped where there is no cluster.
"""

import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
sqlalchemy = pytest.importorskip("sqlalchemy")
hqp_library = pytest.importorskip("hqp_library")

PG = dict(host=os.environ.get("SAUTIUM_TEST_PGHOST", "postgres"),
          port=int(os.environ.get("SAUTIUM_TEST_PGPORT", "5432")),
          user=os.environ.get("SAUTIUM_TEST_PGUSER", "musicai"),
          password=os.environ.get("SAUTIUM_TEST_PGPASSWORD", "supervisor"))
DBNAME = "sautium_hqp_library_test"
PI = ("192.168.1.253", 4321)

PRODIGY_DIR = "Electronic/Big beat/The Prodigy/[Vinyl]/Albums/Music for the Jilted Generation"

LIBRARY = """<?xml version="1.0" encoding="utf-8"?><LibraryGet>\
<LibraryDirectory album="Music for the Jilted Generation" artist="The Prodigy" bitrate="1411" bits="16" \
channels="2" date="1994" genre="Big beat" hash="d1" path="/smb/{prodigy}" rate="44100">\
<LibraryFile hash="f1" length="46" name="The Prodigy - 01. Intro.flac" number="1" song="Intro"/>\
<LibraryFile hash="f2" length="505" name="02. Break &amp; Enter.flac" song="Break &amp; Enter"/>\
<LibraryFile hash="f3" length="0" name="notes.xyz" song="notes"/>\
</LibraryDirectory>\
<LibraryDirectory album="Days to Come" artist="Bonobo" bitrate="2304" bits="24" channels="2" date="2006-10-02" \
genre="Downtempo" hash="d2" path="E:\\Music\\Bonobo\\Days to Come\\CD2" rate="48000">\
<LibraryFile artist="Bonobo feat. Bajka" hash="f4" length="240" name="01. Ketto.flac" number="1" song="Ketto"/>\
</LibraryDirectory></LibraryGet>
""".format(prodigy=PRODIGY_DIR)

PRODIGY_ONLY = LIBRARY[:LIBRARY.index('<LibraryDirectory album="Days to Come"')] + "</LibraryGet>\n"
EMPTY = '<?xml version="1.0" encoding="utf-8"?><LibraryGet/>\n'


def test_parse_shapes_entries_like_the_scanner():
    entries, counts = hqp_library.parse_library(LIBRARY)
    assert counts == {"files": 4, "unsupported": 1}
    assert [d for d, _ in entries] == [f"/smb/{PRODIGY_DIR}"] * 2 + ["E:/Music/Bonobo/Days to Come/CD2"]
    intro, break_enter, ketto = (md for _, md in entries)
    assert intro["file_path"] == f"/smb/{PRODIGY_DIR}/The Prodigy - 01. Intro.flac"
    assert (intro["title"], intro["artist"], intro["album_artist"], intro["album"]) == (
        "Intro", "The Prodigy", "The Prodigy", "Music for the Jilted Generation")
    assert (intro["track_number"], intro["disc_number"], intro["duration_seconds"]) == (1, 1, 46.0)
    assert (intro["sample_rate"], intro["bit_depth"], intro["bitrate"], intro["channels"]) == (44100, 16, 1411, 2)
    assert (intro["file_format"], intro["is_lossless"], intro["release_year"], intro["genre"]) == ("FLAC", True, 1994, "Big beat")
    assert (intro["hqp_file_hash"], intro["hqp_dir_hash"]) == ("f1", "d1")
    # No `number`: the file name's leading digits stand in; entities unescape.
    assert (break_enter["title"], break_enter["track_number"]) == ("Break & Enter", 2)
    # Windows paths take forward slashes; a CD2 folder is disc 2; a file-level
    # artist is the track credit while the directory's stays the album credit.
    assert (ketto["artist"], ketto["album_artist"], ketto["disc_number"], ketto["release_year"]) == (
        "Bonobo feat. Bajka", "Bonobo", 2, 2006)


def test_parse_survives_raw_bytes_and_control_characters():
    raw = LIBRARY.replace('song="Intro"', 'song="Intr\x01o \ufffd"').encode("utf-8") + b""
    raw = raw.replace('song="Ketto"'.encode(), b'song="Ket\xb2to"')
    entries, _ = hqp_library.parse_library(raw.decode("utf-8", "replace"))
    titles = [md["title"] for _, md in entries]
    assert titles[0] == "Intr o \ufffd" and titles[2].startswith("Ket") and titles[2].endswith("to")


@pytest.fixture(scope="module")
def dsn():
    try:
        admin = psycopg2.connect(dbname="postgres", **PG)
    except psycopg2.OperationalError as e:
        pytest.skip(f"no PostgreSQL for the HQPlayer library test: {e}")
    admin.autocommit = True
    from desktop import db_init, node_backup as nb
    nb._drop_database(admin, DBNAME)
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {DBNAME}")
    conn = psycopg2.connect(dbname=DBNAME, **PG)
    db_init.apply_migrations(conn)
    conn.commit()
    conn.close()
    yield f"postgresql://{PG['user']}:{PG['password']}@{PG['host']}:{PG['port']}/{DBNAME}"
    nb._drop_database(admin, DBNAME)
    admin.close()


@pytest.fixture
def db(dsn, monkeypatch):
    import database
    import db_pool
    from sqlalchemy.orm import sessionmaker
    pool = psycopg2.pool.ThreadedConnectionPool(1, 4, dsn=dsn, options="-c timezone=UTC")
    engine = sqlalchemy.create_engine(dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    conn = psycopg2.connect(dsn, options="-c timezone=UTC")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("TRUNCATE tracks, albums, artists, genres, user_settings CASCADE")
    yield conn
    conn.close()
    pool.closeall()
    engine.dispose()


def _one(conn, sql, *params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def _local_scan(title, number):
    """What extract_metadata hands the import for a Prodigy rip on this node's disk."""
    return {"file_path": f"E:/Music/{PRODIGY_DIR}/The Prodigy - {number:02d}. {title}.flac",
            "title": title, "artist": "The Prodigy", "album_artist": "The Prodigy",
            "album": "Music for the Jilted Generation", "date": "1994", "release_year": 1994,
            "genre": "Big beat", "track_number": number, "disc_number": 1, "duration_seconds": 100.0,
            "file_format": "FLAC", "is_lossless": True, "sample_rate": 44100, "bit_depth": 16}


def test_sync_lands_on_the_local_scan_and_is_idempotent(db, monkeypatch):
    from scanner import LOCAL_FILES, import_metadata
    local_dir = f"E:/Music/{PRODIGY_DIR}"
    import_metadata([(local_dir, _local_scan("Intro", 1)), (local_dir, _local_scan("Break & Enter", 2))],
                    sink=LOCAL_FILES, stats={})
    assert _one(db, "SELECT count(*) FROM tracks") == 2

    monkeypatch.setattr(hqp_library, "library_hash", lambda h, p: "h1")
    monkeypatch.setattr(hqp_library, "fetch_library", lambda h, p: LIBRARY)
    stats = hqp_library.sync(*PI)
    assert (stats["library_files"], stats["unsupported"], stats["known"], stats["added"], stats["errors"]) == (4, 1, 0, 3, 0)
    # The Prodigy copy at the HQPlayer is a second VARIANT of the album a local
    # scan minted — no twin tracks, no twin album; Bonobo is new and HQP-only.
    assert _one(db, "SELECT count(*) FROM tracks") == 3
    assert _one(db, "SELECT count(*) FROM albums") == 2
    assert _one(db, """SELECT count(*) FROM album_variants av JOIN albums al ON al.id = av.album_id
                       WHERE al.title = 'Music for the Jilted Generation'""") == 2
    assert _one(db, """SELECT count(*) FROM album_variants
                       WHERE location = 'hqplayer' AND hqp_endpoint_host = %s AND hqp_endpoint_port = %s""", *PI) == 2
    assert _one(db, "SELECT count(*) FROM hqp_library_files") == 3
    assert _one(db, """SELECT count(*) FROM hqp_library_files f JOIN tracks t ON t.id = f.track_id
                       JOIN media_files mf ON mf.track_id = t.id""") == 2
    assert _one(db, "SELECT sample_rate FROM album_variants WHERE directory_path = 'E:/Music/Bonobo/Days to Come/CD2'") == 48000
    assert _one(db, "SELECT value #>> '{}' FROM user_settings WHERE key = %s",
                hqp_library.HASH_SETTING.format(host=PI[0], port=PI[1])) == "h1"

    # Same hash: nothing to do. Forced: everything is known, nothing added.
    assert hqp_library.sync(*PI)["unchanged"] is True
    again = hqp_library.sync(*PI, force=True)
    assert (again["known"], again["added"]) == (3, 0)
    assert _one(db, "SELECT count(*) FROM hqp_library_files") == 3

    # The explicit rescan forgets what the library no longer lists — the
    # Bonobo variant, album and (analysis-less) track go, the Prodigy variant
    # at the HQPlayer stays, the local rip is untouched.
    monkeypatch.setattr(hqp_library, "fetch_library", lambda h, p: PRODIGY_ONLY)
    gone = hqp_library.forget_missing(*PI)
    assert (gone["forgotten"], gone["orphan_variants"], gone["orphan_albums"], gone["orphan_tracks"]) == (1, 1, 1, 1)
    assert _one(db, "SELECT count(*) FROM hqp_library_files") == 2
    assert _one(db, "SELECT count(*) FROM tracks") == 2
    assert _one(db, "SELECT count(*) FROM media_files") == 2
    # An empty answer is refused, like the scanner's empty tree.
    monkeypatch.setattr(hqp_library, "fetch_library", lambda h, p: EMPTY)
    assert hqp_library.forget_missing(*PI)["refused"] is True
    assert _one(db, "SELECT count(*) FROM hqp_library_files") == 2
