"""An album whose own files declare positions that are not all here is
arriving by its own words, and a watcher's scan makes no canon decision
about it (LIBRARY-WATCH-DESIGN §3.5, §15): a partial album could be bound to
the same-titled single it fills half of and folded into the owned single
for good. Few tracks is no incomplete album: an album that declares nothing,
or declares 2/2 with its two files, is complete as soon as it is read.

Real FLACs scanned into the module's scratch database (the scan tests'
fixtures); the canon's resolve read with no MusicBrainz rows — what is asked
here is which albums it takes, not what it makes of them."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

pytest.importorskip("psycopg2")

from canon import content, post_import  # noqa: E402
from tests.test_library_reread_db import _clean, _flac, _scan, template  # noqa: E402,F401
from tests.test_scan_unreachable import cur, library  # noqa: E402,F401
from uuid_utils import album_uuid, artist_uuid  # noqa: E402

pytestmark = pytest.mark.usefixtures("cur")


def _album(title, artist="Aurora"):
    return str(album_uuid(title, artist))


def test_declared_positions_not_all_here_are_an_arriving_album(cur, library, template):
    _clean(cur)
    for i in (1, 2, 3):                                   # 3 of 12
        _flac(template, library / "A" / "LP" / f"{i:02d}.flac", f"T{i}", "Aurora", "LP",
              track=f"{i}/12")
    for i in (1, 2):                                      # a whole two-track album
        _flac(template, library / "A" / "Long" / f"{i:02d}.flac", f"L{i}", "Aurora", "Long",
              track=f"{i}/2")
    _flac(template, library / "A" / "Bare" / "01.flac", "B1", "Aurora", "Bare", track=1)
    _scan()
    ids = [_album("LP"), _album("Long"), _album("Bare")]
    assert content.arriving_albums(ids) == {_album("LP")}
    for i in range(4, 13):
        _flac(template, library / "A" / "LP" / f"{i:02d}.flac", f"T{i}", "Aurora", "LP",
              track=f"{i}/12")
    _scan()
    assert content.arriving_albums(ids) == set()


def test_a_disc_total_counts_the_sibling_disc_folders(cur, library, template):
    _clean(cur)
    _flac(template, library / "A" / "X" / "CD1" / "01.flac", "A1", "Aurora", "X", track="1/1",
          disc="1/2")
    _scan()
    assert content.arriving_albums([_album("X")]) == {_album("X")}
    _flac(template, library / "A" / "X" / "CD2" / "01.flac", "B1", "Aurora", "X", track="1/1",
          disc="2/2")
    _scan()
    assert content.arriving_albums([_album("X")]) == set()


def test_a_watcher_s_canon_leaves_an_arriving_album_and_an_explicit_one_takes_it(
        cur, library, template):
    _clean(cur)
    _flac(template, library / "A" / "X" / "01.flac", "X", "Aurora", "X", track="1/12")
    _flac(template, library / "A" / "Y" / "01.flac", "Y1", "Aurora", "Y", track="1/1")
    _scan()
    aid = str(artist_uuid("Aurora"))
    watched = {a["album_id"] for a in content.resolve_artist(aid, "Aurora", take_arriving=False)["albums"]}
    assert watched == {_album("Y")}
    taken = {a["album_id"] for a in content.resolve_artist(aid, "Aurora")["albums"]}
    assert taken == {_album("X"), _album("Y")}


def test_the_ai_tier_waits_for_an_explicit_scan_and_then_takes_the_deferred_files(cur):
    cur.execute("DELETE FROM user_settings WHERE key = 'canon.ai_pending_since'")
    first = datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)
    assert post_import._ai_since(first, take_arriving=False) is None
    assert post_import._ai_since(first + timedelta(hours=1), take_arriving=False) is None
    explicit = first + timedelta(hours=2)
    assert post_import._ai_since(explicit, take_arriving=True) == first
    assert post_import._ai_since(explicit, take_arriving=True) == explicit
