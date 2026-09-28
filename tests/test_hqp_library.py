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
PI_INFO = {"name": "HQPlayerEmbedded", "product": "Signalyst HQPlayer Embedded"}

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
        cur.execute("TRUNCATE tracks, albums, artists, genres, user_settings, hqp_endpoints CASCADE")
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
    monkeypatch.setattr(hqp_library, "get_info", lambda h, p: PI_INFO)
    stats = hqp_library.sync(*PI)
    assert (stats["library_files"], stats["unsupported"], stats["known"], stats["added"], stats["errors"]) == (4, 1, 0, 3, 0)
    # the library got its row, named after what HQPlayer calls itself
    assert _one(db, "SELECT name || '@' || host || ':' || port || '#' || library_hash FROM hqp_endpoints") == \
        f"HQPlayerEmbedded@{PI[0]}:{PI[1]}#h1"
    # The Prodigy copy at the HQPlayer is a second VARIANT of the album a local
    # scan minted — no twin tracks, no twin album; Bonobo is new and HQP-only.
    assert _one(db, "SELECT count(*) FROM tracks") == 3
    assert _one(db, "SELECT count(*) FROM albums") == 2
    assert _one(db, """SELECT count(*) FROM album_variants av JOIN albums al ON al.id = av.album_id
                       WHERE al.title = 'Music for the Jilted Generation'""") == 2
    assert _one(db, """SELECT count(*) FROM album_variants av JOIN hqp_endpoints e ON e.id = av.hqp_endpoint_id
                       WHERE av.location = 'hqplayer' AND e.host = %s AND e.port = %s""", *PI) == 2
    assert _one(db, "SELECT count(*) FROM hqp_library_files") == 3
    assert _one(db, """SELECT count(*) FROM hqp_library_files f JOIN tracks t ON t.id = f.track_id
                       JOIN media_files mf ON mf.track_id = t.id""") == 2
    assert _one(db, "SELECT sample_rate FROM album_variants WHERE directory_path = 'E:/Music/Bonobo/Days to Come/CD2'") == 48000

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


CREDITS = """<?xml version="1.0" encoding="utf-8"?><LibraryGet>\
<LibraryDirectory album="Black Sands" artist="Bonobo" bitrate="1411" bits="16" channels="2" date="2010" \
genre="Downtempo" hash="d3" path="/media/FLASH/Bonobo/Black Sands" rate="44100">\
<LibraryFile hash="f5" length="327" name="04 - Eyesdown ft. Andreya Triana.flac" number="4" song="Eyesdown ft. Andreya Triana"/>\
<LibraryFile hash="f6" length="236" name="03 - Kong.flac" number="3" song="Kong"/>\
</LibraryDirectory>\
<LibraryDirectory album="Duets" artist="Andrea Bocelli" bitrate="1411" bits="16" channels="2" date="2017" \
genre="Classical" hash="d4" path="/media/FLASH/Bocelli/Duets" rate="44100">\
<LibraryFile hash="f7" length="240" name="06 - Canto (feat. Lauren).flac" number="6" song="Canto della terra (feat. Lauren Daigle)"/>\
<LibraryFile hash="f8" length="242" name="20 - Canto (feat. A-Lin).flac" number="20" song="Canto della terra (feat. A-Lin)"/>\
</LibraryDirectory></LibraryGet>
"""


def test_credit_duplicates_fold_across_copies_never_within_one(db, dsn, monkeypatch):
    """A featuring credit spelt two ways minted two rows for one track — a
    rip here and the HQPlayer's reading of a copy; they fold. Two duets with
    different guests in ONE folder are two tracks and stay."""
    from sqlalchemy.orm import sessionmaker
    from scanner import LOCAL_FILES, import_metadata
    from canon import content
    monkeypatch.setattr(content, "SessionLocal", sessionmaker(bind=sqlalchemy.create_engine(dsn)))
    local_dir = "E:/Music/Bonobo/Black Sands"

    def scan(title, number, seconds):
        return {"file_path": f"{local_dir}/{number:02d}. {title}.flac", "title": title, "artist": "Bonobo",
                "album_artist": "Bonobo", "album": "Black Sands", "date": "2010", "release_year": 2010,
                "genre": "Downtempo", "track_number": number, "disc_number": 1, "duration_seconds": seconds,
                "file_format": "FLAC", "is_lossless": True, "sample_rate": 44100, "bit_depth": 16}
    import_metadata([(local_dir, scan("Eyesdown (feat. Adreya Triana)", 4, 331.0)),
                     (local_dir, scan("Kong", 3, 236.0))], sink=LOCAL_FILES, stats={})
    monkeypatch.setattr(hqp_library, "library_hash", lambda h, p: "h2")
    monkeypatch.setattr(hqp_library, "fetch_library", lambda h, p: CREDITS)
    monkeypatch.setattr(hqp_library, "get_info", lambda h, p: PI_INFO)
    hqp_library.sync(*PI)
    assert _one(db, "SELECT count(*) FROM tracks") == 5          # Eyesdown x2, Kong, Canto x2
    assert _one(db, "SELECT count(*) FROM tracks WHERE title LIKE %s", "Eyesdown%") == 2

    st = content.fold_credit_duplicates(dry_run=False)
    assert (st["groups"], st["merged"], st["vetoed"], st["unproven"]) == (2, 1, 1, 0)
    assert _one(db, "SELECT count(*) FROM tracks WHERE title LIKE %s", "Eyesdown%") == 1
    # the keeper is the row with the file here; the copy's file followed it
    assert _one(db, """SELECT count(*) FROM hqp_library_files hf JOIN tracks t ON t.id = hf.track_id
                       WHERE t.title = 'Eyesdown (feat. Adreya Triana)'""") == 1
    assert _one(db, "SELECT count(*) FROM tracks WHERE title LIKE %s", "Canto della terra%") == 2
    assert content.fold_credit_duplicates(dry_run=False)["merged"] == 0


def test_the_same_library_at_a_new_address_keeps_its_row(db, monkeypatch):
    """A DHCP lease later the Pi answers from another address with the same
    library: the endpoint row moves, nothing is imported twice. A different
    HQPlayer that inherits the old address is a new library."""
    monkeypatch.setattr(hqp_library, "library_hash", lambda h, p: "h1")
    monkeypatch.setattr(hqp_library, "fetch_library", lambda h, p: PRODIGY_ONLY)
    monkeypatch.setattr(hqp_library, "get_info", lambda h, p: PI_INFO)
    assert hqp_library.sync(*PI)["added"] == 2
    moved = ("192.168.1.77", 4321)
    again = hqp_library.sync(*moved)
    assert (again["unchanged"], again.get("added", 0)) == (True, 0)
    assert _one(db, "SELECT count(*) FROM hqp_endpoints") == 1
    assert _one(db, "SELECT host FROM hqp_endpoints") == moved[0]
    assert _one(db, "SELECT count(*) FROM hqp_library_files") == 2
    # a Desktop that took the old address is refused — no row of its own —
    # and the Pi's row, evidently not there any more, keeps its files and
    # loses only the address
    monkeypatch.setattr(hqp_library, "library_hash", lambda h, p: "h9")
    monkeypatch.setattr(hqp_library, "get_info", lambda h, p: {"name": "VH11", "product": "Signalyst HQPlayer Desktop"})
    monkeypatch.setattr(hqp_library, "fetch_library", lambda h, p: EMPTY)
    assert hqp_library.sync(*moved)["refused"] == "desktop"
    assert _one(db, "SELECT count(*) FROM hqp_endpoints") == 1
    assert _one(db, "SELECT host FROM hqp_endpoints WHERE hqp_name = 'HQPlayerEmbedded'") is None
    assert _one(db, "SELECT count(*) FROM hqp_library_files") == 2
    # the endpoint that went home is forgotten by its row, address or not
    gone = hqp_library.forget_endpoint_id(_one(db, "SELECT id FROM hqp_endpoints WHERE hqp_name = 'HQPlayerEmbedded'"))
    assert gone["forgotten"] == 2
    assert _one(db, "SELECT count(*) FROM hqp_endpoints") == 0
    assert _one(db, "SELECT count(*) FROM hqp_library_files") == 0


def test_native_plays_follow_the_output(db, monkeypatch):
    """Each output opens its own copy of a queued track: a rip here for the
    browser, this HQPlayer's held copy when it is the output (outranking
    the rip), a stream only where no copy is reachable."""
    from scanner import LOCAL_FILES, import_metadata
    from playback.queue import items_for_hqp_ids, items_for_media_ids
    from playback.substitute import native_plays
    local_dir = f"E:/Music/{PRODIGY_DIR}"
    import_metadata([(local_dir, _local_scan("Intro", 1)), (local_dir, _local_scan("Break & Enter", 2))],
                    sink=LOCAL_FILES, stats={})
    monkeypatch.setattr(hqp_library, "library_hash", lambda h, p: "h4")
    monkeypatch.setattr(hqp_library, "fetch_library", lambda h, p: LIBRARY)
    monkeypatch.setattr(hqp_library, "get_info", lambda h, p: PI_INFO)
    hqp_library.sync(*PI)
    ep = _one(db, "SELECT id FROM hqp_endpoints")
    with db.cursor() as cur:
        cur.execute("SELECT hf.id FROM hqp_library_files hf JOIN tracks t ON t.id = hf.track_id ORDER BY t.title")
        hqp_ids = [r[0] for r in cur.fetchall()]           # Break & Enter, Intro, Ketto
        cur.execute("SELECT mf.id FROM media_files mf JOIN tracks t ON t.id = mf.track_id ORDER BY t.title")
        media_ids = [r[0] for r in cur.fetchall()]         # Break & Enter, Intro
    held = items_for_hqp_ids(hqp_ids)
    assert [it.source["kind"] for it in held] == ["hqp"] * 3

    # the browser: the Prodigy copies fall back to the rips here, Bonobo waits for a stream
    plays = native_plays(held, "browser", None)
    assert [p and p["kind"] for p in plays] == ["file", "file", "pending"]
    assert plays[1]["path"] == f"{local_dir}/The Prodigy - 01. Intro.flac"
    assert plays[1]["media_file_id"] == media_ids[1]
    # the HQPlayer that holds them: as queued
    assert native_plays(held, "hqplayer", ep) == [None, None, None]
    # another HQPlayer: the rips here, and Bonobo has nothing it could open
    other = native_plays(held, "hqplayer", ep + 1)
    assert [p and p["kind"] for p in other] == ["file", "file", "unplayable"]
    # a rip queued on the browser, played on the HQPlayer that holds a copy: its copy
    local = items_for_media_ids(media_ids)
    assert [p and p["kind"] for p in native_plays(local, "hqplayer", ep)] == ["hqp", "hqp"]
    assert native_plays(local, "browser", None) == [None, None]
    assert native_plays(local, "hqplayer", ep + 1) == [None, None]


def test_a_desktop_is_never_synced(db, monkeypatch):
    """HQPlayer Desktop reads this node's own library: a sync refuses it
    before any row is minted, whatever its library lists."""
    monkeypatch.setattr(hqp_library, "library_hash", lambda h, p: "h5")
    monkeypatch.setattr(hqp_library, "fetch_library", lambda h, p: LIBRARY)
    monkeypatch.setattr(hqp_library, "get_info",
                        lambda h, p: {"name": "VH11", "product": "Signalyst HQPlayer Desktop"})
    stats = hqp_library.sync("192.168.1.188", 4321)
    assert (stats["refused"], stats["added"]) == ("desktop", 0)
    assert _one(db, "SELECT count(*) FROM hqp_endpoints") == 0
    assert _one(db, "SELECT count(*) FROM hqp_library_files") == 0
    assert hqp_library.forget_missing("192.168.1.188", 4321)["refused"] == "desktop"


def test_a_chosen_hqplayer_is_registered_once_across_its_own_aliases(db, monkeypatch):
    """The Output picker registers every HQPlayer the owner chose, a Desktop
    included; this machine's aliases — localhost, the Docker host, its LAN
    address — are one HQPlayer and share the row, which keeps the address
    it was registered at. A sync still refuses the Desktop and leaves its
    row alone; a library row that moved away from that address loses it."""
    monkeypatch.setattr(hqp_library, "library_hash", lambda h, p: "h0")
    monkeypatch.setattr(hqp_library, "get_info",
                        lambda h, p: {"name": "VH11", "product": "Signalyst HQPlayer Desktop"})
    first = hqp_library.register("host.docker.internal", 4321)
    assert (first["name"], first["host"]) == ("VH11", "host.docker.internal")
    again = hqp_library.register("localhost", 4321)
    assert (again["id"], again["host"]) == (first["id"], "host.docker.internal")
    assert hqp_library.endpoint_by_address("127.0.0.1", 4321)["id"] == first["id"]
    assert hqp_library.address_key("localhost", 4321) == hqp_library.address_key("host.docker.internal", 4321)
    assert hqp_library.address_key("192.168.1.253", 4321) == ("192.168.1.253", 4321)
    assert _one(db, "SELECT count(*) FROM hqp_endpoints") == 1
    # the sync refuses a Desktop — and the Desktop's own row keeps its address
    monkeypatch.setattr(hqp_library, "fetch_library", lambda h, p: LIBRARY)
    assert hqp_library.sync("localhost", 4321)["refused"] == "desktop"
    assert _one(db, "SELECT host FROM hqp_endpoints") == "host.docker.internal"
    assert _one(db, "SELECT count(*) FROM hqp_library_files") == 0
    # an Embedded library that moved away still names an address a Desktop
    # now answers at: that row is freed, the Desktop's is not
    monkeypatch.setattr(hqp_library, "get_info", lambda h, p: PI_INFO)
    monkeypatch.setattr(hqp_library, "library_hash", lambda h, p: "h1")
    monkeypatch.setattr(hqp_library, "fetch_library", lambda h, p: PRODIGY_ONLY)
    assert hqp_library.sync(*PI)["added"] == 2
    monkeypatch.setattr(hqp_library, "get_info",
                        lambda h, p: {"name": "VH11", "product": "Signalyst HQPlayer Desktop"})
    monkeypatch.setattr(hqp_library, "library_hash", lambda h, p: "h0")
    assert hqp_library.sync(*PI)["refused"] == "desktop"
    assert _one(db, "SELECT host FROM hqp_endpoints WHERE hqp_name = 'HQPlayerEmbedded'") is None
    assert _one(db, "SELECT host FROM hqp_endpoints WHERE hqp_name = 'VH11'") == "host.docker.internal"
    # not answering: no row, no error — the choice stands
    def down(h, p):
        raise OSError("refused")
    monkeypatch.setattr(hqp_library, "get_info", down)
    assert hqp_library.register("192.168.1.99", 4321) is None
    assert _one(db, "SELECT count(*) FROM hqp_endpoints") == 2
