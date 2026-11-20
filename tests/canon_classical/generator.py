"""The synthetic cases of CANON-CLASSICAL (DESIGN §6.3): an album's files as
a real library writes them, made from one of MusicBrainz's real releases (CC0),
with the answer the canon must reach.

A pure function of (release, form spec, defect spec, seed): no clock and no
global random, so the same seed gives the same files and the same answer.
The caller writes the files — a silent FLAC per length, tagged with mutagen —
into its own temporary folder: no audio file is in the repository.

The forms are those tests/fixtures/canon_classical/forms.json counts in the
owner's library (the names are its keys); a form no file there shows is not here.
"""
import random
from typing import Dict, List, NamedTuple, Optional

TITLE_FORMS = ("Work: Movement", "2 values", "Work - Movement", "the work alone")
CREDIT_FORMS = ("one name", "N names joined by '; '", "N names joined by ', '", "N names joined by ' & '",
                "Ensemble: members")
DEFECTS = ("drift", "missing track", "wrong track number", "two files of one title")


class File(NamedTuple):
    path: str               # relative to the music folder
    tags: Dict[str, List[str]]
    seconds: float


class Case(NamedTuple):
    files: List[File]
    rg: str
    release: str
    recordings: Dict[str, Optional[str]]   # path -> the recording the file must bind to (None: none)


def _split(title: str):
    """MusicBrainz's classical title: `Work: Movement`."""
    work, sep, movement = title.partition(": ")
    return (work, movement) if sep else (title, None)


def _credit(form: str, names: List[str]) -> str:
    if form == "one name" or len(names) == 1:
        return names[0]
    if form == "N names joined by '; '":
        return "; ".join(names)
    if form == "N names joined by ', '":
        return ", ".join(names)
    if form == "N names joined by ' & '":
        return " & ".join(names)
    return f"{names[0]}: " + ", ".join(names[1:-1]) + (" & " if len(names) > 2 else "") + names[-1]


def generate(release: dict, title_form: str, credit_form: str = "one name", defects: Dict[str, float] = None,
             seed: int = 0) -> Case:
    """`release`: {"mbid", "rg", "title", "artists": [names in credit order], "tracks": [(title, length_ms,
    recording mbid)], "barcode"}; `defects`: {name: amount} — drift in seconds, the others 1."""
    rnd = random.Random(seed)
    defects = defects or {}
    folder = f"{release['artists'][0]}/{release['title']}".replace(":", " -")
    artist = _credit(credit_form, release["artists"])
    tracks = list(enumerate(release["tracks"], 1))
    if defects.get("missing track") and len(tracks) > 1:
        tracks.pop(rnd.randrange(len(tracks)))
    files, answer = [], {}
    for n, (title, length_ms, rec) in tracks:
        work, movement = _split(title)
        if title_form == "2 values" and movement:
            values = [work, movement]
        elif title_form == "Work - Movement" and movement:
            values = [f"{work} - {movement}"]
        elif title_form == "the work alone":
            values = [work]
        else:
            values = [title]
        if defects.get("two files of one title") and n == 2:
            values = list(files[0].tags["title"]) if files else values
        number = n
        if defects.get("wrong track number") and n == len(tracks):
            number = 1
        base = length_ms / 1000.0 if length_ms else 200.0      # MusicBrainz knows no length: the file has one
        seconds = round(base + rnd.uniform(-1, 1) * defects.get("drift", 0.0), 2)
        tags = {"title": values, "artist": [artist], "albumartist": [artist], "album": [release["title"]],
                "tracknumber": [str(number)], "tracktotal": [str(len(release["tracks"]))]}
        if release.get("barcode"):
            tags["barcode"] = [release["barcode"]]
        path = f"{folder}/{n:02d}.flac"
        files.append(File(path, tags, seconds))
        answer[path] = rec
    return Case(files, release["rg"], release["mbid"], answer)
