"""Folder-aware album identity for recovering a loosely-organized catalog.

A directory's files are grouped by ``release_match_key(album_tag)``; how the
directory maps to albums depends on its shape (``assign_dir_albums``):

* **FOLD** to ONE album titled by the folder, credited to Various Artists (or the
  sole artist), when the directory is a *reshapeable mix* (some group's track
  numbers aren't a contiguous per-disc 1..N — fragments of different sources —
  AND it isn't merely one real album by one artist) or a *loose per-track dump*
  (>=2 groups, each a single track — a genre/favourites pile or per-track
  mistagging). Tracks are renumbered by filename only when positions collide.
* **SPLIT** into one album per group when there are >=2 clean groups and it isn't
  a fold — a box set (each disc a real album) or a singles collection. A group
  sitting on a single non-1 disc is renormalised to disc 1 so a split-out
  sub-album looks like a standalone release.
* Otherwise (one group, not a fold) the directory is a plain single album and is
  left to the caller's default per-file path.

Per-track artist/title stay from each file's own tags, so track identity and
dedupe are unchanged; a VA-credited fold falls out of the MB release-group canon,
which is what stops a mix from stealing a real album's identity. Best-effort:
deluxe editions with continuous cross-disc numbering classify as a fold —
acceptable for recovery, and canon re-derives real titles downstream. Shared by
the scanner and ``migrate_folder_albums``.
"""

import os
import re
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

from discography import release_match_key
from uuid_utils import normalize

VARIOUS_ARTISTS = "Various Artists"

# A disc/CD/part marker plus the descriptive subtitle and catalog junk that
# trails it, to END of string: "Ummagumma CD1 Live Album (2011, 50999…)",
# "Here Lies Love - Disc Two", "The Maze To Nowhere / Part 2" all reduce to the
# base album. release_match_key only strips a bare "CD1"/"(Disc 2)" token, so on
# its own it would split a real multi-disc album into separate albums. A *volume*
# marker is deliberately NOT here — "Vol.1"/"Vol.2" denote separate releases in a
# series (Ambient Highway), which must stay distinct.
# No word boundary after the marker — "CD1" has none between "cd" and "1".
_DISC_TAIL_RE = re.compile(
    r"[\s\-–—/.,:]*[\(\[]?\s*\b(?:cd|disc|disk|lp|pt|part)\s*\.?\s*"
    r"(?:\d+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten)\b.*$",
    re.IGNORECASE,
)
_DVD_TAIL_RE = re.compile(
    r"[\s\-–—/.,:]*[\(\[]\s*(?:dvd|blu-?ray|bonus\s+dvd)\s*[\)\]]\s*$",
    re.IGNORECASE,
)


def _album_key(title: Optional[str]) -> str:
    """Stricter sibling of ``release_match_key`` for deciding whether two files
    in one folder are the same album: also folds any disc/part marker (digit,
    roman, or spelled-out) and the subtitle trailing it, so a real multi-disc
    release groups as one album rather than splitting. Used only here; the canon
    keeps the plain ``release_match_key``."""
    t = (title or "").strip()
    t = _DVD_TAIL_RE.sub("", t)
    t = _DISC_TAIL_RE.sub("", t)
    return release_match_key(t)


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
        groups[_album_key(album)][disc or 1].append(track or 1)
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
    return len({_album_key(a) for a, _d, _t in tracks if (a or "").strip()}) >= 2


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


def _basename(path: Optional[str]) -> str:
    return os.path.basename((path or "").replace("\\", "/").rstrip("/"))


def _rep_title(group: List[dict]) -> str:
    """The group's representative album title: most common raw tag, ties broken
    lexicographically so the choice is deterministic across runs."""
    counts = Counter((f["album"] or "").strip() for f in group if (f["album"] or "").strip())
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _rep_artist(group: List[dict]) -> str:
    """The group's album-credit: the credit that holds a strict majority of the
    files, else Various Artists. A release inconsistently credited ("Lemonchill"
    5× vs "Kota & Lemonchill" 4×) is one album by its dominant credit; a genuine
    compilation (every track a different artist, no album_artist) has no majority
    and is Various Artists. ``artist_key`` is matched by normalized identity, the
    original spelling returned."""
    counts: Counter = Counter()
    spelling: dict = {}
    for f in group:
        k = (f["artist_key"] or "").strip()
        if k:
            nk = normalize(k)
            counts[nk] += 1
            spelling.setdefault(nk, k)
    if not counts:
        return VARIOUS_ARTISTS
    nk, n = counts.most_common(1)[0]
    return spelling[nk] if n * 2 > sum(counts.values()) else VARIOUS_ARTISTS


def dir_group_count(files: List[dict]) -> int:
    """Number of distinct release-key groups in a directory (``files`` as for
    ``assign_dir_albums``). The retrofit uses it to leave single-group folders to
    the scanner + canon and only restructure real box sets / singles / mixes."""
    return len({_album_key(f["album"]) for f in files})


def assign_dir_albums(
    folder_name: str, files: List[dict]
) -> Optional[Dict[str, Tuple[str, str, Optional[int], Optional[int]]]]:
    """Map each file in a directory to ``(album_title, album_artist,
    track_override, disc_override)``, or ``None`` when the directory is a plain
    single album that needs no reshaping (the caller keeps each file's own tags).

    ``files`` is a list of ``{album, disc, track, artist_key, path}``. A
    ``track``/``disc`` override of ``None`` means "keep the file's own value".
    See the module docstring for the FOLD vs SPLIT decision.
    """
    groups: Dict[str, List[dict]] = defaultdict(list)
    for f in files:
        groups[_album_key(f["album"])].append(f)

    tracks = [(f["album"], f["disc"], f["track"]) for f in files]
    akeys = [f["artist_key"] for f in files]
    all_singleton = len(groups) >= 2 and all(len(g) == 1 for g in groups.values())
    fold = is_reshapeable_mix(tracks, akeys) or all_singleton

    # A plain single album the scanner's per-file path already gets right — one
    # release key, one non-empty title, one credit — needs no override. Anything
    # else falls through: >=2 groups, a fold, or intra-group title/credit variance
    # that would otherwise fragment one folder into several albums (a collab
    # tagged two ways, a multi-disc release with per-disc titles, untagged files).
    if not fold and len(groups) == 1:
        g = next(iter(groups.values()))
        titles = {(f["album"] or "").strip() for f in g}
        artists = {normalize(f["artist_key"] or "") for f in g}
        if len(titles) == 1 and "" not in titles and len(artists) == 1:
            return None

    if fold:
        artist = folder_album_artist(akeys)
        renumber = has_duplicate_positions([(f["disc"], f["track"]) for f in files])
        order: Dict[str, int] = {}
        if renumber:
            for i, f in enumerate(sorted(files, key=lambda x: _basename(x["path"]).lower()), 1):
                order[f["path"]] = i
        return {f["path"]: (folder_name, artist,
                            order.get(f["path"]) if renumber else None,
                            1 if renumber else None)
                for f in files}

    out: Dict[str, Tuple[str, str, Optional[int], Optional[int]]] = {}
    for g in groups.values():
        title = _rep_title(g) or folder_name
        artist = _rep_artist(g)
        # A sub-album that occupies one non-1 disc within the box reads as disc 1
        # once it stands alone; a genuinely multi-disc group keeps its numbering.
        discs = {(f["disc"] or 1) for f in g}
        flatten_disc = 1 if (len(discs) == 1 and discs != {1}) else None
        for f in g:
            out[f["path"]] = (title, artist, None, flatten_disc)
    return out
