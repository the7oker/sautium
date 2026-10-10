"""The quality badge. A lossless rip past CD on either axis — more than 16
bits, or more than 48 kHz — is Hi-Res (sql_queries.rip_quality), and every
badge reads that one rule: the album header, an edition card, Now Playing for
an owned file and for a copy held at the HQPlayer. Until 2026-10-10 each kept
its own copy wanting 24 bits AND 48 kHz: a 24/44.1 album read "Lossless", and
so did every DSD rip (1 bit at 2.8 MHz).

On a real PostgreSQL — the module's scratch database (conftest.scratch_dsn),
the connection pool pointed at it. Skipped where there is no cluster.
"""

import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")

from sql_queries import rip_quality  # noqa: E402

# (format, lossless, sample rate, bit depth), as the scanner reads a file
CD = ("FLAC", True, 44100, 16)
TR24 = ("FLAC", True, 44100, 24)
DSD64 = ("DSF", True, 2822400, 1)
AAC96 = ("M4A", False, 96000, 16)


@pytest.fixture
def cur(scratch_dsn, monkeypatch):
    import db_pool
    import psycopg2.pool
    pool = psycopg2.pool.ThreadedConnectionPool(1, 2, dsn=scratch_dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    with conn.cursor() as c:
        for table in ("tracks", "albums", "hqp_endpoints"):   # the rest cascades
            c.execute(f"DELETE FROM {table}")
        yield c
    conn.close()
    pool.closeall()


def _album(cur):
    album = str(uuid.uuid4())
    cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'Shaped By Time')", (album,))
    return album


def _variant(cur, album, folder, rip, endpoint=None):
    """A folder of `album` whose files are all `rip`, carrying that
    resolution as the scanner records a variant's."""
    _, lossless, sample_rate, bit_depth = rip
    cur.execute("INSERT INTO album_variants (album_id, directory_path, is_lossless, sample_rate, "
                "bit_depth, location, hqp_endpoint_id) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (album, folder, lossless, sample_rate, bit_depth,
                 "hqplayer" if endpoint else "local", endpoint))
    return cur.fetchone()[0]


def _track(cur):
    track = str(uuid.uuid4())
    cur.execute("INSERT INTO tracks (id, title) VALUES (%s, 'Silent Walking')", (track,))
    return track


def _owned(cur, variant, rip):
    """One track's file in `variant` — its media_files id."""
    fmt, lossless, sample_rate, bit_depth = rip
    track = _track(cur)
    cur.execute("INSERT INTO media_files (track_id, album_variant_id, file_path, file_format, "
                "is_lossless, sample_rate, bit_depth) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (track, variant, f"/music/{track}.{fmt.lower()}", fmt, lossless,
                 sample_rate, bit_depth))
    return cur.fetchone()[0]


@pytest.mark.parametrize("lossless, sample_rate, bit_depth, badge", [
    (True, 44100, 16, "lossless"),       # CD
    (True, 48000, 16, "lossless"),       # DAT is CD-class
    (True, 44100, 24, "hi-res"),
    (True, 48000, 24, "hi-res"),
    (True, 96000, 16, "hi-res"),
    (True, 2822400, 1, "hi-res"),        # DSD64
    (True, 44100, None, "lossless"),     # depth unknown
    (False, 96000, 24, "lossy"),
    (None, None, None, "lossy"),         # no rip: BOOL_OR over nothing
])
def test_hi_res_is_lossless_past_cd_on_either_axis(cur, lossless, sample_rate, bit_depth,
                                                   badge):
    cur.execute(f"SELECT {rip_quality('l', 'sr', 'bd')} "
                "FROM (VALUES (%s::boolean, %s::int, %s::int)) v(l, sr, bd)",
                (lossless, sample_rate, bit_depth))
    assert cur.fetchone()[0] == badge


def test_a_24_bit_44_khz_album_reads_hi_res_wherever_its_badge_shows(cur):
    from routers.albums import get_album
    from routers.player import now_playing_detail
    from routers.release_groups import get_release_group
    album = _album(cur)
    variant = _variant(cur, album, "/music/Origin - Shaped By Time", TR24)
    first = _owned(cur, variant, TR24)
    _owned(cur, variant, TR24)

    assert get_album(album, variant_id=None)["quality"] == "hi-res"
    assert [e["quality"] for e in get_release_group(album)["editions"]] == ["hi-res"]
    assert now_playing_detail(media_file_id=first)["quality"] == "hi-res"


def test_the_best_lossless_rip_decides_and_a_lossy_rate_says_nothing(cur):
    """A CD rip beside a lossy 96 kHz one is Lossless: the badge reads the
    lossless rips' resolution, never the highest number any file carries."""
    from routers.albums import get_album
    from routers.release_groups import get_release_group
    album = _album(cur)
    _owned(cur, _variant(cur, album, "/music/cd", CD), CD)
    _owned(cur, _variant(cur, album, "/music/web", AAC96), AAC96)

    assert get_album(album, variant_id=None)["quality"] == "lossless"
    assert [e["quality"] for e in get_release_group(album)["editions"]] == ["lossless"]


def test_a_copy_held_at_the_hqplayer_reads_the_same_rule(cur, monkeypatch):
    import routers.player as player
    album = _album(cur)
    cur.execute("INSERT INTO hqp_endpoints (name) VALUES ('Embedded') RETURNING id")
    endpoint = cur.fetchone()[0]
    variant = _variant(cur, album, "/mnt/music/Origin", DSD64, endpoint=endpoint)
    track = _track(cur)
    cur.execute("INSERT INTO album_tracks (album_id, track_id, position) VALUES (%s, %s, 1)",
                (album, track))
    cur.execute("INSERT INTO hqp_library_files (track_id, album_variant_id, hqp_path, "
                "file_format, is_lossless, sample_rate, bit_depth) "
                "VALUES (%s, %s, '/mnt/music/Origin/01.dsf', %s, %s, %s, %s)",
                (track, variant, *DSD64))
    monkeypatch.setattr(player, "active_hqp_endpoint", lambda: endpoint)

    assert player.now_playing_detail(track_id=track)["quality"] == "hi-res"
