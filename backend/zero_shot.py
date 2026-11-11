"""CLAP's zero-shot values — moods, the vocal score, danceability — as every
reader sees them: derived, never analysed.

A node holds no zero-shot analysis of its own. The database derives the
scores from the segment embeddings it holds (zero_shot_scores, kept by the
triggers on embedding_segments — desktop/migrations/001_initial.sql): per
segment the softmax within each label set of scale × (segment · label), per
embedding the mean of those over its segments (a bundle holding a segment
that is not a unit vector derives none). The label embeddings are
constants of the release (zero_shot_labels/labels.json, made once by
scripts/make_zero_shot_labels.py); db_migrate's `zero_shot_labels_v1` step
loads them. Two nodes holding the same signed segments derive the same
scores to ~1e-7, so nothing here travels or is signed.

What the scores MEAN is decided here, at read time: the stored rows are the
raw distribution, and presence is a threshold, as the instruments' is
(ensemble_instruments.threshold_for).
"""

import hashlib
import json
from pathlib import Path
from typing import Dict, Optional

LABELS_PATH = Path(__file__).resolve().parent / "zero_shot_labels" / "labels.json"
# BLAKE2b-256 of the file's content (labels_digest). The release pins the
# constants by it: a node never derives from another file.
LABELS_DIGEST = "8dddc91e69bc9f16a65a521a55ec89e41a0b9cc3e4abcdf8b226c2b5606a3afb"

# What readers name — a label set, a label's key — never an id.
MOODS = "moods"
DANCEABLE = "danceable"

# Set 2026-11-11 from a measurement on a real library of ~41 000 tracks:
# each threshold is the one at which the gate passes the share of that
# library's tracks the stored value it replaces passed (a mood stored above
# 0.05 of one random crop; danceable at 0.5) — so each gate keeps its share
# there by construction. One threshold per mood: how high a prompt scores is
# the model's and the prompt's doing, and the eight differ nearly twofold.
# A mood with no threshold here is never present.
MOOD_MIN = {
    "happy and upbeat": 0.068,
    "sad and melancholic": 0.063,
    "energetic and intense": 0.054,
    "calm and relaxing": 0.081,
    "dark and ominous": 0.056,
    "romantic and dreamy": 0.048,
    "aggressive and angry": 0.089,
    "mysterious and atmospheric": 0.051,
}
DANCEABLE_MIN = 0.48
# There is no vocal/instrumental class: the vocal score does not separate
# the tracks of singing artists from those of instrumental ones (AUC 0.67;
# medians 0.038 against 0.014), so it stays a number — whether an artist
# sings is artists.is_vocalist.


def labels_digest(doc: dict) -> str:
    """The digest of a labels document: its content without the `digest`
    field, as sorted compact JSON."""
    body = {k: v for k, v in doc.items() if k != "digest"}
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.blake2b(blob.encode("utf-8"), digest_size=32).hexdigest()


def load_labels() -> dict:
    """The release's label constants. A file whose content is not the one
    this code was released with is refused."""
    doc = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    found = labels_digest(doc)
    if found != LABELS_DIGEST:
        raise ValueError(f"{LABELS_PATH.name} is not this release's label file: "
                         f"digest {found[:16]}…, expected {LABELS_DIGEST[:16]}…")
    return doc


def label_keys(label_set: str) -> tuple:
    """The keys of one label set, in the file's order."""
    return tuple(label["key"] for label in load_labels()["sets"][label_set])


def present_moods(moods: Optional[Dict[str, float]]) -> Dict[str, float]:
    """A track's raw mood scores → the moods present, each at its own
    threshold, strongest first."""
    if not moods:
        return {}
    return {k: round(v, 3) for k, v in sorted(moods.items(), key=lambda kv: -kv[1])
            if k in MOOD_MIN and v >= MOOD_MIN[k]}


def mood_thresholds(moods) -> str:
    """The thresholds of the moods asked for, as the JSON object the mood
    gate binds ({key: threshold}; a name that is no mood has none)."""
    return json.dumps({m: MOOD_MIN[m] for m in moods if m in MOOD_MIN})


def track_gate_sql(track: str, key: str, bound: str) -> str:
    """SQL predicate: the track whose id is `track` has the label `key`
    scored within `bound` (">= :danceable_min")."""
    return ("EXISTS (SELECT 1 FROM embeddings zse"
            " JOIN zero_shot_scores zs ON zs.embedding_id = zse.id"
            " JOIN zero_shot_labels zl ON zl.id = zs.label_id"
            f" WHERE zse.track_id = {track} AND zl.key = '{key}' AND zs.score {bound})")
