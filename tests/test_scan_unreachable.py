"""A scan never reports a walk over an unreachable music folder as a scan
(routers.settings.music_folder_unreachable — the rule the library.mount_missing
notice and the Library screen read): scan_start refuses it, and a walk that
found nothing records nothing. On 2026-10-09 a Docker bind that came up on an
empty directory turned "Scan for new" into "Scan complete" and a fresh "Last
scan" while the new album sat on the drive nothing had mounted.

On a real PostgreSQL — a throwaway database built from the migrations, the
connection pool pointed at it. Skipped where there is no cluster.
"""

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
settings_router = pytest.importorskip("routers.settings")

PG = dict(host=os.environ.get("SAUTIUM_TEST_PGHOST", "postgres"),
          port=int(os.environ.get("SAUTIUM_TEST_PGPORT", "5432")),
          user=os.environ.get("SAUTIUM_TEST_PGUSER", "musicai"),
          password=os.environ.get("SAUTIUM_TEST_PGPASSWORD", "supervisor"))
DBNAME = "sautium_scan_unreachable_test"


@pytest.fixture(scope="module")
def dsn():
    try:
        admin = psycopg2.connect(dbname="postgres", **PG)
    except psycopg2.OperationalError as e:
        pytest.skip(f"no PostgreSQL for the scan test: {e}")
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
    import db_pool
    import psycopg2.pool
    pool = psycopg2.pool.ThreadedConnectionPool(1, 2, dsn=dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute("DELETE FROM user_settings")
        c.execute("DELETE FROM media_files")
        c.execute("DELETE FROM album_variants")
        c.execute("DELETE FROM albums")
        c.execute("DELETE FROM tracks")
        yield c
    conn.close()
    pool.closeall()


@pytest.fixture
def scan_state(monkeypatch):
    monkeypatch.chdir(BACKEND)   # main mounts static/ relative to the server's directory
    main = pytest.importorskip("main")
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


def _last_scan(cur):
    cur.execute("SELECT value FROM user_settings WHERE key = 'library.last_scan_at'")
    return cur.fetchone()


def test_an_empty_folder_is_unreachable_only_while_the_catalog_knows_files(cur, library):
    assert not settings_router.music_folder_unreachable()   # a fresh node's empty folder

    _owned_file(cur)
    assert settings_router.music_folder_unreachable()

    (library / "Electronic").mkdir()
    assert not settings_router.music_folder_unreachable()

    library.joinpath("Electronic").rmdir()
    library.rmdir()                                          # the drive itself gone
    assert settings_router.music_folder_unreachable()


def test_the_notice_names_the_folder_the_owner_knows(cur, library):
    _owned_file(cur)

    notice = {n["key"]: n for n in settings_router._notices_state()["items"]}

    # the host's name for the folder, never the path the node walks
    assert notice["library.mount_missing"]["data"]["path"] == (
        library.parent / "host" / "Music").as_posix()


def test_scan_start_refuses_an_unreachable_folder(cur, library, scan_state):
    from fastapi import HTTPException
    _owned_file(cur)

    with pytest.raises(HTTPException) as refused:
        asyncio.run(scan_state.scan_start())

    assert refused.value.status_code == 409
    assert refused.value.detail.startswith(
        f"{(library.parent / 'host' / 'Music').as_posix()} is empty or not mounted")
    assert scan_state._scan_state["running"] is False


@pytest.mark.parametrize("catalog, progress", [
    (True, "Scan failed: music folder unreachable"),   # the folder gone mid-run
    (False, "No audio files found"),                   # a fresh node, an empty folder
])
def test_an_empty_walk_records_no_scan(cur, library, scan_state, catalog, progress):
    if catalog:
        _owned_file(cur)
    scan_state._scan_state.update(running=True, cancel_requested=False,
                                  progress="Starting scan...", stats=None, result=None)

    scan_state._scan_worker(None, True, None, True)

    assert scan_state._scan_state["running"] is False
    assert scan_state._scan_state["progress"] == progress
    assert "mb_canon" not in scan_state._scan_state["result"]   # no post-import pass
    assert _last_scan(cur) is None
