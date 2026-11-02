"""What the AI canon tier tells its provider about where an artist's albums
sit (canon/ai_canon._context, _prompt): the album's folders below the library
root, never an absolute path. On the module's scratch database, the
connection pool pointed at it."""

import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

ROOT = "/srv/library-root"


@pytest.fixture
def cur(scratch_dsn, monkeypatch):
    import db_pool
    import psycopg2
    import psycopg2.pool
    from config import settings
    pool = psycopg2.pool.ThreadedConnectionPool(1, 2, dsn=scratch_dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    monkeypatch.setattr(settings, "music_library_path", "/music")
    monkeypatch.setattr(settings, "music_host_path", ROOT)
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    with conn.cursor() as c:
        yield c
    conn.close()
    pool.closeall()


def _artist_with_album_in(cur, *folders: str) -> str:
    artist = str(uuid.uuid4())
    cur.execute("INSERT INTO artists (id, name) VALUES (%s, %s)", (artist, f"Кино {artist[:8]}"))
    for folder in folders:
        album = str(uuid.uuid4())
        cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'Группа крови')", (album,))
        cur.execute("INSERT INTO album_artists (album_id, artist_id) VALUES (%s, %s)", (album, artist))
        cur.execute("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, %s)",
                    (album, folder))
    return artist


def test_the_canon_names_the_album_folder_below_the_library_root(cur):
    from canon import ai_canon
    artist = _artist_with_album_in(cur, f"{ROOT}/Кино/1988 - Группа крови",
                                   "/home/someone/Downloads/stray")

    ctx, cands = ai_canon._context(artist, "Кино")
    prompt = ai_canon._prompt([{"n": 1, "name": "Кино", "ctx": ctx, "cands": cands}])

    assert ctx["path"] == "Кино/1988 - Группа крови"
    assert ROOT not in prompt
    assert "/home/" not in prompt


def test_an_album_only_outside_the_library_root_names_no_folder(cur):
    from canon import ai_canon
    artist = _artist_with_album_in(cur, "/home/someone/Music/Кино")

    ctx, cands = ai_canon._context(artist, "Кино")

    assert ctx["path"] is None
    assert "/home/" not in ai_canon._prompt([{"n": 1, "name": "Кино", "ctx": ctx, "cands": cands}])
