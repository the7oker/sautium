"""A scan never reports a walk over an unreachable music folder as a scan
(scanner.library_unreachable — the rule the library.mount_missing notice, the
Library screen and the prune read): every entry point refuses it, a walk that
found nothing records nothing, and a prune handed a partial tree deletes
nothing once the folder is gone. On 2026-10-09 a Docker bind that came up on
an empty directory turned "Scan for new" into "Scan complete" and a fresh
"Last scan" while the new album sat on the drive nothing had mounted.

On a real PostgreSQL — a throwaway database built from the migrations, the
connection pool AND the SQLAlchemy sessions pointed at it, so not even a
regressed prune can reach the node's own catalog. Skipped where there is no
cluster.
"""

import asyncio
import select
import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
settings_router = pytest.importorskip("routers.settings")

import scanner  # noqa: E402
from test_hqp_load import PG, _make_db  # noqa: E402

DBNAME = "sautium_scan_unreachable_test"


@pytest.fixture(scope="module")
def dsn():
    admin, nb = _make_db(DBNAME)
    yield f"postgresql://{PG['user']}:{PG['password']}@{PG['host']}:{PG['port']}/{DBNAME}"
    nb._drop_database(admin, DBNAME)
    admin.close()


@pytest.fixture
def library(tmp_path, monkeypatch):
    """The music folder the node walks — an empty directory, as the
    2026-10-09 bind was — and the host's own name for it, the one the catalog
    stores and the owner knows (a Docker node walks its bind)."""
    from config import settings
    root = tmp_path / "music"
    root.mkdir()
    monkeypatch.setattr(settings, "music_library_path", str(root))
    monkeypatch.setattr(settings, "music_host_path", (tmp_path / "host" / "Music").as_posix())
    return root


@pytest.fixture
def cur(dsn, monkeypatch):
    import database
    import db_pool
    import psycopg2.pool
    from sqlalchemy.orm import sessionmaker
    pool = psycopg2.pool.ThreadedConnectionPool(1, 2, dsn=dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    engine = database.make_engine(dsn)
    monkeypatch.setattr(database, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    # The notice's onset is process state: each test starts with none armed.
    monkeypatch.setattr(settings_router, "_derived_since", {})
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as c:
        for table in ("user_settings", "media_files", "album_variants", "albums", "tracks"):
            c.execute(f"DELETE FROM {table}")
        yield c
    conn.close()
    engine.dispose()
    pool.closeall()


@pytest.fixture
def heard(dsn):
    """What the notices channel hears from the backend."""
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute("LISTEN sautium_notices")

    def wakes(timeout=2.0):
        if select.select([conn], [], [], timeout)[0]:
            conn.poll()
        got = [n.channel for n in conn.notifies]
        conn.notifies.clear()
        return got

    yield wakes
    conn.close()


@pytest.fixture
def scan_state(monkeypatch):
    monkeypatch.chdir(BACKEND)   # main mounts static/ relative to the server's directory
    import main
    saved = dict(main._scan_state)
    yield main
    main._scan_state.clear()
    main._scan_state.update(saved)


def _owned_file(cur):
    from config import settings
    album_dir = f"{settings.library_db_root()}/Solar Fields/Shaped By Time"
    track, album = str(uuid.uuid4()), str(uuid.uuid4())
    cur.execute("INSERT INTO tracks (id, title) VALUES (%s, 'Silent Walking')", (track,))
    cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'Shaped By Time')", (album,))
    cur.execute("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, %s) "
                "RETURNING id", (album, album_dir))
    cur.execute("INSERT INTO media_files (track_id, album_variant_id, file_path) "
                "VALUES (%s, %s, %s)", (track, cur.fetchone()[0], f"{album_dir}/01.flac"))
    return album_dir


def _last_scan(cur):
    cur.execute("SELECT value FROM user_settings WHERE key = 'library.last_scan_at'")
    return cur.fetchone()


def test_an_empty_folder_is_unreachable_only_while_the_catalog_knows_files(
        cur, library, monkeypatch):
    from config import settings
    assert not scanner.library_unreachable()      # a fresh node's empty folder

    _owned_file(cur)
    assert scanner.library_unreachable()

    (library / "Electronic").mkdir()
    assert not scanner.library_unreachable()

    library.joinpath("Electronic").rmdir()
    library.rmdir()                               # the drive itself gone
    assert scanner.library_unreachable()

    monkeypatch.setattr(settings, "music_library_path", "")
    assert not scanner.library_unreachable()      # no folder chosen yet is not "unmounted"


def test_the_notice_names_the_folder_the_owner_knows(cur, library):
    _owned_file(cur)

    notice = {n["key"]: n for n in settings_router._notices_state()["items"]}

    # the host's name for the folder, never the path the node walks
    assert notice["library.mount_missing"]["data"]["path"] == (
        library.parent / "host" / "Music").as_posix()


def test_scan_start_refuses_an_unreachable_folder_and_wakes_the_notices(
        cur, library, scan_state, heard):
    from fastapi import HTTPException
    _owned_file(cur)

    with pytest.raises(HTTPException) as refused:
        asyncio.run(scan_state.scan_start())

    assert refused.value.status_code == 409
    assert refused.value.detail == scanner.UNREACHABLE
    assert scan_state._scan_state["running"] is False
    assert heard() == ["sautium_notices"]         # every open tab learns the onset


def test_the_cli_and_the_legacy_endpoint_refuse_as_well(cur, library):
    _owned_file(cur)

    with pytest.raises(RuntimeError, match="empty or not mounted"):
        scanner.scan_library()


def _empty_the_folder_during_the_walk(monkeypatch, library):
    (library / "Electronic").mkdir()

    def walk(self, **kw):
        library.joinpath("Electronic").rmdir()
        return [], []

    monkeypatch.setattr(scanner.LibraryScanner, "find_audio_files", walk)


@pytest.mark.parametrize("case, progress, wakes", [
    ("gone before the walk", f"Scan failed: {scanner.UNREACHABLE}", []),
    ("gone during the walk", f"Scan failed: {scanner.UNREACHABLE}", ["sautium_notices"]),
    ("a fresh node's empty folder", "No audio files found", []),
], ids=["gone-before", "gone-during", "fresh-node"])
def test_an_empty_walk_records_no_scan(cur, library, scan_state, heard, monkeypatch,
                                       case, progress, wakes):
    from canon import post_import
    if case != "a fresh node's empty folder":
        _owned_file(cur)
    if case == "gone during the walk":
        _empty_the_folder_during_the_walk(monkeypatch, library)
    ran = []
    monkeypatch.setattr(post_import, "run", lambda *a: ran.append("post_import"))
    monkeypatch.setattr(scanner, "prune_missing_files", lambda **kw: ran.append("prune"))
    scan_state._scan_state.update(running=True, cancel_requested=False,
                                  progress="Starting scan...", stats=None, result=None)

    scan_state._scan_worker(None, True, None, True)

    assert scan_state._scan_state["running"] is False
    assert scan_state._scan_state["progress"] == progress
    assert ran == []                              # no post-import pass, no prune
    assert _last_scan(cur) is None
    assert heard(timeout=0.5) == wakes


def test_a_prune_handed_a_partial_tree_deletes_nothing_once_the_folder_is_gone(cur, library):
    album_dir = _owned_file(cur)

    # a walk that saw one file before the drive left
    stats = scanner.prune_missing_files(disk_paths={f"{album_dir}/02.flac"})

    assert stats["pruned"] == 0
    cur.execute("SELECT count(*) FROM media_files")
    assert cur.fetchone()[0] == 1
