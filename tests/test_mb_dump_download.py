"""The MB dump archive (backend/mb_dump_load.py) resumes only into its own
version and is read only after the mirror's SHA-256 vouches for it. On
2026-10-20 an older dump's interrupted download, saved under the fixed name
mbdump.tar.bz2, was resumed with the newer dump's bytes: the spliced archive
failed a third of the way into the load — and would at every retry, since a
complete-size file answers the Range request with 416 and reads as done.

Against a local mirror that honours Range, as the MetaBrainz one does. The
last two tests need PostgreSQL (a scratch database from the migrations): a
failed COPY leaves the table's rows and indexes as they were, and a failed
pass leaves no spooled member behind.
"""

import hashlib
import http.server
import io
import os
import sys
import tarfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

mb = pytest.importorskip("mb_dump_load")

OLD, NEW = "20261007-002148", "20261017-002244"
CORE_OLD = os.urandom(300_000)
CORE_NEW = os.urandom(300_000)       # the same length: a full-size file meets 416
DERIVED_NEW = os.urandom(50_000)


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    """A fullexport mirror on localhost: ``files`` maps a URL path to its
    bytes, ``seen`` records (path, Range header) per request."""
    files, seen = {}, []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            rng = self.headers.get("Range")
            seen.append((self.path, rng))
            body = files.get(self.path)
            if body is None:
                self.send_error(404)
                return
            start = int(rng.removeprefix("bytes=").split("-")[0]) if rng else 0
            if start >= len(body):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(body)}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206 if rng else 200)
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
            self.send_header("Content-Length", str(len(body) - start))
            self.end_headers()
            self.wfile.write(body[start:])

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    data = tmp_path / "mbdump"
    data.mkdir()
    monkeypatch.setattr(mb, "_MIRROR", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(mb, "_DATA", str(data))
    yield SimpleNamespace(files=files, seen=seen, data=data)
    server.shutdown()
    server.server_close()


def _publish(m, version, core):
    m.files[f"/{version}/mbdump.tar.bz2"] = core
    m.files[f"/{version}/mbdump-derived.tar.bz2"] = DERIVED_NEW
    m.files[f"/{version}/SHA256SUMS"] = (
        f"{hashlib.sha256(DERIVED_NEW).hexdigest()} *mbdump-derived.tar.bz2\n"
        f"{hashlib.sha256(core).hexdigest()} *mbdump.tar.bz2\n").encode()


def test_an_older_dumps_partial_is_never_resumed_with_a_newer_dumps_bytes(mirror):
    _publish(mirror, NEW, CORE_NEW)
    (mirror.data / "mbdump.tar.bz2").write_bytes(CORE_OLD[:100_000])
    (mirror.data / f"mbdump-{OLD}.tar.bz2").write_bytes(CORE_OLD[:50_000])
    kept = {"indexes_mbdump.json": b"{}", "VERSION": OLD.encode(),
            f"mbdump-derived-{NEW}.tar.bz2": DERIVED_NEW[:10]}
    for name, body in kept.items():
        (mirror.data / name).write_bytes(body)

    path = mb.download(NEW, "mbdump")

    assert path == str(mirror.data / f"mbdump-{NEW}.tar.bz2")
    assert Path(path).read_bytes() == CORE_NEW
    assert (f"/{NEW}/mbdump.tar.bz2", None) in mirror.seen
    assert sorted(p.name for p in mirror.data.iterdir()) == sorted(
        [*kept, f"mbdump-{NEW}.tar.bz2"])


def test_an_interrupted_download_of_the_same_version_resumes(mirror):
    _publish(mirror, NEW, CORE_NEW)
    (mirror.data / f"mbdump-{NEW}.tar.bz2").write_bytes(CORE_NEW[:123_456])

    path = mb.download(NEW, "mbdump")

    assert Path(path).read_bytes() == CORE_NEW
    assert (f"/{NEW}/mbdump.tar.bz2", "bytes=123456-") in mirror.seen


def test_a_complete_but_corrupt_archive_is_deleted_instead_of_loaded(mirror):
    """Kept, every retry would load the same bytes and fail at the same row."""
    _publish(mirror, NEW, CORE_NEW)
    target = mirror.data / f"mbdump-{NEW}.tar.bz2"
    target.write_bytes(CORE_OLD)

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        mb.download(NEW, "mbdump")
    assert not target.exists()
    assert Path(mb.download(NEW, "mbdump")).read_bytes() == CORE_NEW


def test_each_archive_takes_its_own_checksum_line(mirror):
    _publish(mirror, NEW, CORE_NEW)
    assert mb.expected_sha256(NEW, "mbdump") == hashlib.sha256(CORE_NEW).hexdigest()
    assert mb.expected_sha256(NEW, "mbdump-derived") == hashlib.sha256(DERIVED_NEW).hexdigest()
    with pytest.raises(RuntimeError, match="no SHA-256"):
        mb.expected_sha256(NEW, "mbdump-edit")


def test_no_checksum_no_download(mirror):
    mirror.files[f"/{NEW}/mbdump.tar.bz2"] = CORE_NEW
    mirror.files[f"/{NEW}/SHA256SUMS"] = b"not a checksum line\n"
    (mirror.data / f"mbdump-{OLD}.tar.bz2").write_bytes(CORE_OLD[:10])

    with pytest.raises(RuntimeError, match="no SHA-256"):
        mb.download(NEW, "mbdump")
    assert [path for path, _ in mirror.seen] == [f"/{NEW}/SHA256SUMS"]
    assert [p.name for p in mirror.data.iterdir()] == [f"mbdump-{OLD}.tar.bz2"]


# ── against PostgreSQL ───────────────────────────────────────────────────────

@pytest.fixture
def pool(scratch_dsn, monkeypatch):
    import db_pool
    import psycopg2.pool
    pool = psycopg2.pool.ThreadedConnectionPool(1, 4, dsn=scratch_dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    yield scratch_dsn
    pool.closeall()


def _archive(data, members):
    """The core archive at NEW holding ``members`` (basename, COPY text) in
    the order given."""
    with tarfile.open(data / f"mbdump-{NEW}.tar.bz2", "w:bz2") as tar:
        for name, text in members:
            info = tarfile.TarInfo(f"mbdump/{name}")
            info.size = len(text)
            tar.addfile(info, io.BytesIO(text))


def test_a_failed_copy_keeps_the_old_rows_and_their_indexes(pool, tmp_path, monkeypatch):
    import psycopg2
    monkeypatch.setattr(mb, "_DATA", str(tmp_path))
    conn = psycopg2.connect(pool)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("INSERT INTO mb_area (id, name) VALUES (1, 'Ukraine'), (2, 'Japan')")
    cur.execute("CREATE INDEX test_mb_area_name ON mb_area (name)")
    indexes = "SELECT indexname FROM pg_indexes WHERE tablename = 'mb_area' ORDER BY 1"
    cur.execute(indexes)
    before = cur.fetchall()
    assert len(before) == 2   # the primary key's and ours
    # Three fields of mb_area's fourteen: the COPY fails on the first row.
    _archive(tmp_path, [("area", b"3\t\\N\tFrance\n")])

    with pytest.raises(psycopg2.Error):
        mb.stream_load("mbdump", NEW, tables=[("mb_area", "area")])

    cur.execute("SELECT id, name FROM mb_area ORDER BY id")
    assert cur.fetchall() == [(1, "Ukraine"), (2, "Japan")]
    cur.execute(indexes)
    assert cur.fetchall() == before
    cur.execute("SELECT pg_try_advisory_lock(%s)", (mb.MB_LOAD_LOCK_KEY,))
    assert cur.fetchone()[0] is True
    cur.execute("SELECT pg_advisory_unlock(%s)", (mb.MB_LOAD_LOCK_KEY,))
    conn.close()


def test_a_failed_pass_leaves_no_spooled_member(pool, tmp_path, monkeypatch):
    import psycopg2
    monkeypatch.setattr(mb, "_DATA", str(tmp_path))
    # The link member precedes url, as in the real core archive, so it is
    # spooled; the url row is a field short, so the url COPY fails after it.
    _archive(tmp_path, [
        ("l_artist_url", b"100\t1\t7\t1\t0\t\\N\t0\t\t\n"),
        ("url", b"1\t\\N\thttps://jonhopkins.bandcamp.com/\t0\n"),
    ])

    with pytest.raises(psycopg2.Error):
        mb.stream_load("mbdump", NEW,
                       tables=[("mb_url", "url"), ("mb_l_artist_url", "l_artist_url")])
    assert not (tmp_path / "spool_mb_l_artist_url.tsv").exists()
