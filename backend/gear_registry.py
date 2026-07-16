"""
Measurement-registry importer — Gear Advisor candidate pool widening.

Parses public FR-measurement databases (an AutoEq sparse clone:
measurements/<measurer>/data/{over-ear,in-ear}/<Model>.csv) into
gear_registry_entries with a compact band signature: deviation from
the category's Harman target per perceptual band, level-anchored at
200-800 Hz. Entries are NOT gear_models — the research worker must
never see thousands of queued rows; an entry links to the catalog by
normalized-name match, or later when the user promotes it to 'want'.

Band deltas are comparable ONLY within one source (measurer = rig);
the source string keeps that boundary explicit everywhere downstream.

CLI (inside the backend container, registry bind-mounted at /app/registry):
    python gear_registry.py --import-autoeq /app/registry/autoeq
    python gear_registry.py --match
    python gear_registry.py --stats
"""

import csv
import logging
import os
import re
from typing import Dict, List, Optional, Tuple

from db_pool import db_execute, db_query, db_query_one
from uuid_utils import NAMESPACE, normalize
import uuid as _uuid

logger = logging.getLogger(__name__)

BANDS = [
    ("dev_sub_bass_db", 20, 60),
    ("dev_bass_db", 60, 200),
    ("dev_mids_db", 200, 2000),
    ("dev_presence_db", 2000, 5000),
    ("dev_treble_db", 5000, 10000),
]
ANCHOR = (200, 800)

# Targets are RIG-BOUND: a compensation curve only makes sense against
# measurements taken on the rig family it was derived for. Each
# measurer maps to the target files matching its fixture.
SOURCE_TARGETS = {
    "oratory1990": {  # GRAS 43AG — the rig Harman research used
        "over-ear": "Harman over-ear 2018.csv",
        "in-ear": "Harman in-ear 2019.csv",
    },
    "Rtings": {  # legacy HEAD acoustics HMS II.3 rig — AutoEq only ships
        # the without-bass Harman variant for this fixture, so deltas are
        # rebased to the full-Harman reference by adding the shelf back.
        "over-ear": "HMS II.3 Harman over-ear 2018 without bass.csv",
        "in-ear": "HMS II.3 Harman in-ear 2019 without bass.csv",
        "rebase_shelf": True,
        # Rtings nests per-rig subdirs; only the HMS branch is Harman-
        # comparable. The B&K 5128 branch lives in a different reference
        # system (DF-tilt) — skipped until it gets its own basis.
        "subdir": "HMS II.3",
    },
}
CATEGORY = {"over-ear": "headphones", "in-ear": "iems"}

# Harman preference bass shelf per band (full target minus without-bass
# variant, computed from the GRAS pair; the shelf is a property of the
# target design, not the rig — reused for HMS rebasing within ~0.1 dB).
TARGET_BASS_SHELF = {"dev_sub_bass_db": 5.86, "dev_bass_db": 2.82, "dev_mids_db": 0.05}


def registry_entry_uuid(source: str, model_name: str) -> _uuid.UUID:
    return _uuid.uuid5(NAMESPACE, f"gear_registry:{normalize(source)}:{normalize(model_name)}")


def _read_curve(path: str) -> List[Tuple[float, float]]:
    pts = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row or row[0].lower().startswith("freq"):
                continue
            try:
                pts.append((float(row[0]), float(row[1])))
            except (ValueError, IndexError):
                continue
    pts.sort()
    return pts


def _interp(curve: List[Tuple[float, float]], freq: float) -> Optional[float]:
    if not curve or freq < curve[0][0] or freq > curve[-1][0]:
        return None
    lo, hi = 0, len(curve) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if curve[mid][0] <= freq:
            lo = mid
        else:
            hi = mid
    (f1, v1), (f2, v2) = curve[lo], curve[hi]
    if f2 == f1:
        return v1
    return v1 + (v2 - v1) * (freq - f1) / (f2 - f1)


def _band_mean(delta: List[Tuple[float, float]], lo: float, hi: float) -> Optional[float]:
    vals = [v for f, v in delta if lo <= f < hi]
    return sum(vals) / len(vals) if vals else None


def band_signature(curve: List[Tuple[float, float]],
                   target: List[Tuple[float, float]]) -> Optional[Dict[str, float]]:
    delta = []
    for f, v in curve:
        t = _interp(target, f)
        if t is not None:
            delta.append((f, v - t))
    if not delta:
        return None
    anchor_vals = [v for f, v in delta if ANCHOR[0] <= f < ANCHOR[1]]
    if not anchor_vals:
        return None
    anchor = sum(anchor_vals) / len(anchor_vals)
    delta = [(f, v - anchor) for f, v in delta]
    sig = {}
    for key, lo, hi in BANDS:
        m = _band_mean(delta, lo, hi)
        if m is not None:
            sig[key] = round(m, 2)
    return sig or None


def import_autoeq(root: str) -> Dict[str, int]:
    stats = {"entries": 0, "skipped": 0}
    meas_root = os.path.join(root, "measurements")
    for measurer in sorted(os.listdir(meas_root)):
        data_dir = os.path.join(meas_root, measurer, "data")
        if not os.path.isdir(data_dir) or measurer not in SOURCE_TARGETS:
            continue
        cfg = SOURCE_TARGETS[measurer]
        rebase = cfg.get("rebase_shelf", False)
        targets = {}
        for kind in CATEGORY:
            if kind not in cfg:
                continue
            tpath = os.path.join(root, "targets", cfg[kind])
            targets[kind] = _read_curve(tpath)
            if not targets[kind]:
                raise RuntimeError(f"target curve missing/empty: {tpath}")
        source = f"autoeq:{measurer}"
        for kind, category in CATEGORY.items():
            kdir = os.path.join(data_dir, kind)
            if cfg.get("subdir"):
                kdir = os.path.join(kdir, cfg["subdir"])
            if not os.path.isdir(kdir) or kind not in targets:
                continue
            for fname in sorted(os.listdir(kdir)):
                if not fname.endswith(".csv"):
                    continue
                model_name = fname[:-4]
                curve = _read_curve(os.path.join(kdir, fname))
                sig = band_signature(curve, targets[kind]) if curve else None
                if not sig:
                    stats["skipped"] += 1
                    continue
                if rebase:
                    for key, shelf in TARGET_BASS_SHELF.items():
                        if key in sig:
                            sig[key] = round(sig[key] - shelf, 2)
                db_execute(
                    """
                    INSERT INTO gear_registry_entries
                        (id, source, category, model_name,
                         dev_sub_bass_db, dev_bass_db, dev_mids_db,
                         dev_presence_db, dev_treble_db)
                    VALUES (%(id)s::uuid, %(src)s, %(cat)s, %(name)s,
                            %(sb)s, %(b)s, %(m)s, %(p)s, %(t)s)
                    ON CONFLICT (source, model_name) DO UPDATE
                    SET dev_sub_bass_db = EXCLUDED.dev_sub_bass_db,
                        dev_bass_db     = EXCLUDED.dev_bass_db,
                        dev_mids_db     = EXCLUDED.dev_mids_db,
                        dev_presence_db = EXCLUDED.dev_presence_db,
                        dev_treble_db   = EXCLUDED.dev_treble_db,
                        imported_at     = now()
                    """,
                    {"id": str(registry_entry_uuid(source, model_name)),
                     "src": source, "cat": category, "name": model_name,
                     "sb": sig.get("dev_sub_bass_db"), "b": sig.get("dev_bass_db"),
                     "m": sig.get("dev_mids_db"), "p": sig.get("dev_presence_db"),
                     "t": sig.get("dev_treble_db")},
                )
                stats["entries"] += 1
        logger.info(f"registry: {source} imported")
    return stats


_FILLER_WORDS = ("audio", "acoustics", "electronics", "the")
_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v2": "2", "v3": "3", "mk2": "2", "mk3": "3", "mkii": "2", "mkiii": "3"}


def _name_keys(text: str) -> List[str]:
    """Match keys for a free-form model name: diacritics stripped
    (Vérité ↔ Verite), parenthesized variant suffixes dropped
    ('(alcantara earpads)'), brand filler words dropped ('Meze Audio
    Elite' ↔ 'Meze Elite'), roman/mark generation forms unified
    ('Liric II' ↔ 'Liric 2'). Registry names carry brand+model in one
    string; several registry variants may map to one catalog model."""
    import unicodedata
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"\([^)]*\)", " ", text)
    base = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    words = [_ROMAN.get(w, w) for w in base.split()]
    keys = {" ".join(words)}
    keys.add(" ".join(w for w in words if w not in _FILLER_WORDS))
    return [k for k in keys if k]


def match_to_catalog() -> int:
    """Link registry entries to existing gear_models by normalized name."""
    models = db_query(
        """
        SELECT gm.id::text AS id, b.name AS brand, gm.model, gm.category::text AS category
        FROM gear_models gm JOIN gear_brands b ON b.id = gm.brand_id
        WHERE gm.category IN ('headphones', 'iems')
        """
    )
    by_key: Dict[Tuple[str, str], str] = {}
    for m in models:
        for key in _name_keys(f'{m["brand"]} {m["model"]}'):
            by_key[(m["category"], key)] = m["id"]

    linked = 0
    for e in db_query("SELECT id::text AS id, category::text AS category, model_name FROM gear_registry_entries"):
        target = None
        for key in _name_keys(e["model_name"]):
            target = by_key.get((e["category"], key))
            if target:
                break
        if target:
            db_execute(
                "UPDATE gear_registry_entries SET gear_model_id = %(g)s::uuid WHERE id = %(id)s::uuid",
                {"g": target, "id": e["id"]},
            )
            linked += 1
    return linked


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--import-autoeq", metavar="PATH")
    ap.add_argument("--match", action="store_true")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    if args.import_autoeq:
        print(import_autoeq(args.import_autoeq))
    if args.match:
        print(f"linked: {match_to_catalog()}")
    if args.stats:
        for r in db_query(
            """
            SELECT source, category::text AS cat, COUNT(*) AS n,
                   COUNT(gear_model_id) AS linked
            FROM gear_registry_entries GROUP BY source, category ORDER BY source, cat
            """
        ):
            print(f'{r["source"]:24s} {r["cat"]:10s} {r["n"]:5d} entries, {r["linked"]} linked')
