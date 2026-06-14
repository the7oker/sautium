"""Folder-aware album identity for recovering a loosely-organized catalog.

A directory is a **MIX** when its files don't form clean albums: grouped by album
tag, at least one group's track numbers aren't a contiguous per-disc 1..N (each
track pulled from a different source album, or numbers missing). A mix collapses
to ONE album titled by the folder, credited to Various Artists (or the sole
artist), with tracks renumbered by filename **only when** the existing disc/track
numbers actually collide. A folder whose every album group IS a clean sequence is
left alone — those are real albums (one, or a box set), and MB canon owns their
canonical titles.

This handles single-artist mixes (a personal "favourites" folder) that an
artist-count test alone would miss, and is best-effort: deluxe editions with
continuous cross-disc numbering, or two editions dumped in one folder, classify
as mix — acceptable for catalog recovery, and canon re-derives real titles
downstream. Per-track artist/title stay from each file's own tags, so track
identity and dedupe are unchanged. Shared by the scanner and
``migrate_folder_albums``.
"""

from collections import defaultdict
from typing import List, Optional, Tuple

from discography import release_match_key
from uuid_utils import normalize

VARIOUS_ARTISTS = "Various Artists"


def folder_album_artist(artist_keys: List[Optional[str]]) -> str:
    """``VARIOUS_ARTISTS`` when the folder credits ≥2 distinct artists, else the
    single one. Each key is a file's ``album_artist`` (falling back to its track
    artist); distinctness uses the same ``normalize`` as ``album_uuid``."""
    seen = {}
    for k in artist_keys:
        k = (k or "").strip()
        if k:
            seen.setdefault(normalize(k), k)
    if len(seen) >= 2:
        return VARIOUS_ARTISTS
    return next(iter(seen.values()), VARIOUS_ARTISTS)


def folder_is_mix(tracks: List[Tuple[Optional[str], Optional[int], Optional[int]]]) -> bool:
    """``tracks`` = ``(album_tag, disc_number, track_number)`` per file. A folder
    is a MIX when any album group's positions aren't a contiguous per-disc 1..N
    (empty track → 1) — fragments of different albums rather than whole albums.
    Every group clean → real album(s), not a mix."""
    groups: dict = defaultdict(lambda: defaultdict(list))
    for album, disc, track in tracks:
        groups[release_match_key((album or "").strip())][disc or 1].append(track or 1)
    for per_disc in groups.values():
        for nums in per_disc.values():
            if sorted(nums) != list(range(1, len(nums) + 1)):
                return True
    return False


def is_reshapeable_mix(tracks: List[Tuple[Optional[str], Optional[int], Optional[int]]],
                       artist_keys: List[Optional[str]]) -> bool:
    """A folder worth collapsing into a single folder album: it's a MIX
    (``folder_is_mix``) AND not merely one real album by one artist — those are
    left alone even when oddly numbered (vinyl with no track tags, a 2-disc set
    with continuous numbering). So: fragments AND (≥2 artists OR ≥2 album tags).
    A genuine single-artist "favourites" folder qualifies (≥2 album tags)."""
    if not folder_is_mix(tracks):
        return False
    if folder_album_artist(artist_keys) == VARIOUS_ARTISTS:
        return True
    return len({release_match_key((a or "").strip())
                for a, _d, _t in tracks if (a or "").strip()}) >= 2


def has_duplicate_positions(positions: List[tuple]) -> bool:
    """True when the same ``(disc, track)`` appears twice — the numbers can't
    order the album. The renumber trigger: a clean unique sequence is never
    touched, so real track orders are preserved."""
    seen = set()
    for p in positions:
        if p in seen:
            return True
        seen.add(p)
    return False
