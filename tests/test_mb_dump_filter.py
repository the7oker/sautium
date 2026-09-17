"""The dump loader's row filters (backend/mb_dump_load.py): a url row
survives only for a Bandcamp shop, a link row only when its url did — decided
on the COPY text the dump ships, through the reader COPY drains."""

import io
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

mb = pytest.importorskip("mb_dump_load")

URL_ROWS = [
    b"1\tg1\thttps://jonhopkins.bandcamp.com/album/immunity\t0\t2020-01-01 00:00:00+00\n",
    b"2\tg2\thttps://www.hdtracks.com/#/album/5def4d96b45f07686f01498e\t0\t\\N\n",
    b"3\tg3\thttps://daily.bandcamp.com/best-of-2013\t0\t\\N\n",
    b"4\tg4\thttp://NinjaTune.Bandcamp.com/\t0\t\\N\n",
    b"5\tg5\thttps://bandcamp.com/tag/ambient\t0\t\\N\n",
    b"6\tg6\thttps://notbandcamp.com/album/x\t0\t\\N\n",
    b"7\tg7\thttps://evil.example/?u=https://x.bandcamp.com/\t0\t\\N\n",
]
KEPT = URL_ROWS[0] + URL_ROWS[3]


def _drain(reader, size):
    out = b""
    while True:
        chunk = reader.read(size)
        if not chunk:
            return out
        assert chunk.endswith(b"\n"), "a chunk must end on a row boundary"
        out += chunk


def test_url_filter_keeps_bandcamp_shops_only():
    ids = set()
    reader = mb._FilteredReader(io.BytesIO(b"".join(URL_ROWS)), mb._row_filters(ids)["mb_url"])
    assert _drain(reader, 32) == KEPT
    assert ids == {1, 4}
    assert (reader.seen, reader.kept) == (7, 2)


def test_link_filter_follows_the_surviving_urls():
    keep = mb._row_filters({1, 4})["mb_l_release_url"]
    rows = [b"100\t9\t555\t1\t0\t\\N\t0\t\t\n",
            b"101\t9\t556\t2\t0\t\\N\t0\t\t\n",
            b"102\t9\t557\t4\t0\t\\N\t0\t\t\n"]
    reader = mb._FilteredReader(io.BytesIO(b"".join(rows)), keep)
    assert _drain(reader, 8192) == rows[0] + rows[2]


def test_chunks_end_on_row_boundaries_at_any_size():
    for size in (1, 7, 64, 100_000):
        reader = mb._FilteredReader(io.BytesIO(b"".join(URL_ROWS)),
                                    mb._row_filters(set())["mb_url"])
        assert _drain(reader, size) == KEPT, size


def test_link_tables_load_after_url():
    assert mb._FILTER_AFTER == {"mb_l_artist_url": "mb_url", "mb_l_release_url": "mb_url"}
    order = [t for t, _ in mb.TABLES]
    assert order.index("mb_url") < order.index("mb_l_artist_url") < order.index("mb_l_release_url")
    assert set(mb._row_filters(set())) == {"mb_url", "mb_l_artist_url", "mb_l_release_url"}
