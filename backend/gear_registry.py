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


# ── import guardrails ───────────────────────────────────────────────────────
# Dataset imports follow collect → validate → write: rows are built in
# memory, invariants run BEFORE any DB write, and a degraded source
# aborts without touching good data. Learned live: a sparse-checkout
# that silently materialized zero files, a per-rig subdir change that
# yielded zero entries, a field that turned into a dict — silence is
# the failure mode, so every anomaly must be loud.

SHRINK_TOLERANCE = 0.8   # new count per source must be ≥ 80% of what DB has
MAX_SKIP_RATIO = 0.3     # >30% unparsable rows in a source = malformed dataset


class RegistryImportError(RuntimeError):
    pass


def _current_counts() -> Dict[str, int]:
    return {r["source"]: r["n"] for r in db_query(
        "SELECT source, COUNT(*) AS n FROM gear_registry_entries GROUP BY source")}


def _validate_batch(source: str, rows: List[dict], skipped: int,
                    current: Dict[str, int]) -> None:
    if not rows:
        raise RegistryImportError(
            f"{source}: produced ZERO entries — layout change or empty checkout "
            f"(configured sources must never import silence)")
    total = len(rows) + skipped
    if total and skipped / total > MAX_SKIP_RATIO:
        raise RegistryImportError(
            f"{source}: {skipped}/{total} rows unparsable — dataset format likely changed")
    have = current.get(source, 0)
    if have and len(rows) < have * SHRINK_TOLERANCE:
        raise RegistryImportError(
            f"{source}: import shrank to {len(rows)} entries vs {have} in DB "
            f"(>{int((1-SHRINK_TOLERANCE)*100)}% regression) — refusing to overwrite")


def fetch_json(url: str, min_bytes: int = 100_000):
    """Refresh helper with explicit failure modes: HTTP status, body
    size (a 404 page or truncated download must not masquerade as a
    dataset), and JSON parse — each raises loudly."""
    import httpx
    import json
    resp = httpx.get(url, timeout=120, follow_redirects=True)
    if resp.status_code != 200:
        raise RegistryImportError(f"fetch {url}: HTTP {resp.status_code}")
    if len(resp.content) < min_bytes:
        raise RegistryImportError(
            f"fetch {url}: body {len(resp.content)} bytes < {min_bytes} floor — not a dataset")
    try:
        return json.loads(resp.content)
    except json.JSONDecodeError as e:
        raise RegistryImportError(f"fetch {url}: invalid JSON ({e})")


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
    current = _current_counts()
    meas_root = os.path.join(root, "measurements")
    seen_configured = 0
    for measurer in sorted(os.listdir(meas_root)):
        data_dir = os.path.join(meas_root, measurer, "data")
        if not os.path.isdir(data_dir) or measurer not in SOURCE_TARGETS:
            continue
        seen_configured += 1
        cfg = SOURCE_TARGETS[measurer]
        rebase = cfg.get("rebase_shelf", False)
        targets = {}
        for kind in CATEGORY:
            if kind not in cfg:
                continue
            tpath = os.path.join(root, "targets", cfg[kind])
            targets[kind] = _read_curve(tpath)
            if not targets[kind]:
                raise RegistryImportError(f"target curve missing/empty: {tpath}")
        source = f"autoeq:{measurer}"

        # Phase 1: collect + validate before a single write.
        batch, skipped = [], 0
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
                    skipped += 1
                    continue
                if rebase:
                    for key, shelf in TARGET_BASS_SHELF.items():
                        if key in sig:
                            sig[key] = round(sig[key] - shelf, 2)
                batch.append({"category": category, "model_name": model_name, "sig": sig})
        _validate_batch(source, batch, skipped, current)

        # Phase 2: write.
        for row in batch:
            sig = row["sig"]
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
                {"id": str(registry_entry_uuid(source, row["model_name"])),
                 "src": source, "cat": row["category"], "name": row["model_name"],
                 "sb": sig.get("dev_sub_bass_db"), "b": sig.get("dev_bass_db"),
                 "m": sig.get("dev_mids_db"), "p": sig.get("dev_presence_db"),
                 "t": sig.get("dev_treble_db")},
            )
        stats["entries"] += len(batch)
        stats["skipped"] += skipped
        logger.info(f"registry: {source} imported ({len(batch)} entries, {skipped} skipped)")
    if seen_configured < len(SOURCE_TARGETS):
        missing = set(SOURCE_TARGETS) - {m for m in os.listdir(meas_root)
                                         if os.path.isdir(os.path.join(meas_root, m, "data"))}
        raise RegistryImportError(
            f"configured sources absent from checkout: {sorted(missing)} — "
            f"sparse paths or upstream layout changed")
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


def import_spinorama(path: str) -> Dict[str, int]:
    """Loudspeaker registry from a spinorama.org metadata.json snapshot
    (refresh: curl https://www.spinorama.org/json/metadata.json into
    data/registry/spinorama/). Speakers carry CEA-2034 aggregates
    instead of band deltas: Olive preference score (the loudspeaker
    sibling of the Harman headphone research), score with an ideal
    subwoofer, LFX bass extension, sensitivity — plus price and form
    factor straight from the dataset. One entry per speaker from its
    default (best-quality) measurement; origin recorded in source."""
    import json
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    stats = {"entries": 0, "skipped": 0}
    current = _current_counts()

    # Phase 1: collect + validate before a single write. All spinorama
    # origins are validated as one family — origin split is fine-grained
    # provenance, not independent datasets.
    batch, skipped = [], 0
    for sp in data.values():
        brand, model = sp.get("brand"), sp.get("model")
        meas = (sp.get("measurements") or {}).get(sp.get("default_measurement") or "", {})
        pref = meas.get("pref_rating") or {}
        if not brand or not model or not pref.get("pref_score"):
            skipped += 1
            continue
        source = f'spinorama:{meas.get("origin") or "unknown"}'
        model_name = f"{brand} {model}"
        sens = meas.get("computed_sensitivity") or sp.get("sensitivity")
        if isinstance(sens, dict):  # newer schema nests it
            sens = sens.get("sensitivity_1m") or sens.get("computed")
        price = sp.get("price")
        try:
            price = float(price) if price not in (None, "") else None
        except (TypeError, ValueError):
            price = None
        batch.append({"source": source, "model_name": model_name, "pref": pref,
                      "sens": sens, "price": price, "sp": sp, "meas": meas})

    family_current = sum(n for s, n in current.items() if s.startswith("spinorama:"))
    if not batch:
        raise RegistryImportError("spinorama: produced ZERO entries — schema changed?")
    total = len(batch) + skipped
    if skipped / total > MAX_SKIP_RATIO:
        raise RegistryImportError(
            f"spinorama: {skipped}/{total} entries unparsable — schema likely changed")
    if family_current and len(batch) < family_current * SHRINK_TOLERANCE:
        raise RegistryImportError(
            f"spinorama: import shrank to {len(batch)} vs {family_current} in DB — refusing")

    # Phase 2: write.
    for row in batch:
        sp, meas, pref = row["sp"], row["meas"], row["pref"]
        source, model_name = row["source"], row["model_name"]
        sens, price = row["sens"], row["price"]
        db_execute(
            """
            INSERT INTO gear_registry_entries
                (id, source, category, model_name, pref_score, pref_score_wsub,
                 lfx_hz, sens_db, price_usd, shape, quality, active_speaker)
            VALUES (%(id)s::uuid, %(src)s, 'speakers', %(name)s, %(ps)s, %(psw)s,
                    %(lfx)s, %(sens)s, %(price)s, %(shape)s, %(q)s, %(act)s)
            ON CONFLICT (source, model_name) DO UPDATE
            SET pref_score = EXCLUDED.pref_score,
                pref_score_wsub = EXCLUDED.pref_score_wsub,
                lfx_hz = EXCLUDED.lfx_hz, sens_db = EXCLUDED.sens_db,
                price_usd = EXCLUDED.price_usd, shape = EXCLUDED.shape,
                quality = EXCLUDED.quality, active_speaker = EXCLUDED.active_speaker,
                imported_at = now()
            """,
            {"id": str(registry_entry_uuid(source, model_name)),
             "src": source, "name": model_name,
             "ps": pref.get("pref_score"), "psw": pref.get("pref_score_wsub"),
             "lfx": pref.get("lfx_hz"), "sens": sens,
             "price": price,
             "shape": (sp.get("shape") or None),
             "q": meas.get("quality") or None,
             "act": sp.get("type") == "active"},
        )
        stats["entries"] += 1
    stats["skipped"] = skipped
    logger.info(f"registry: spinorama imported ({stats['entries']} entries, {skipped} skipped)")
    return stats


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--import-autoeq", metavar="PATH")
    ap.add_argument("--import-spinorama", metavar="PATH")
    ap.add_argument("--refresh-spinorama", action="store_true",
                    help="fetch the live metadata.json and import it (guardrails apply)")
    ap.add_argument("--match", action="store_true")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    if args.import_autoeq:
        print(import_autoeq(args.import_autoeq))
    if args.import_spinorama:
        print(import_spinorama(args.import_spinorama))
    if args.refresh_spinorama:
        import json
        import tempfile
        data = fetch_json("https://www.spinorama.org/json/metadata.json")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
            json.dump(data, tmp)
            tmp_path = tmp.name
        try:
            print(import_spinorama(tmp_path))
        finally:
            os.unlink(tmp_path)
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
