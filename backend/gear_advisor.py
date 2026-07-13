"""
Upgrade advisor — Phases 4/5 of the Gear Advisor.

Composes existing pieces: the deterministic pair engine diagnoses
where the owned chain has measurably PLATEAUED (anti-recommendations
are first-class output — "don't spend here" saves more money than any
recommendation), the library's genre/DR profile supplies the personal
axes, and researched candidates (want-list + catalog) are laid out
along the price axis with per-axis trait matches.

No merged score, ever: a candidate card carries price, park
compatibility (from the pair engine), community sentiment with sample
size, and which of the USER'S OWN listening axes its praised traits
hit. Ranking is the price axis; judgment stays with the owner.
"""

import math
from typing import Any, Dict, List, Optional

from db_pool import db_query, db_query_one
from gear_pairs import system_analysis, _num

# Transparency thresholds for the plateau diagnosis. Sources: accepted
# psychoacoustic floors (THD+N below ~-96 dB / SINAD above ~96 is past
# blind-test audibility for line-level electronics).
PLATEAU_SINAD_DB = 100
PLATEAU_SNR_DB = 110

_SUB_BASS_GENRES = (
    "Electronic", "Techno", "House", "Hip-Hop", "Industrial", "Dub",
    "Drum And Bass", "Dubstep", "Trip-Hop", "Electro", "Downtempo",
    "Dark Ambient", "Drone", "Synth-Pop", "IDM", "Breakbeat",
)
_TEXTURE_GENRES = (
    "Ambient", "Dark Ambient", "Drone", "Classical", "New Age",
    "Experimental", "Jazz", "Modern Classical", "Field Recording", "Minimalism",
)
_TIMBRE_GENRES = (
    "Classical", "Jazz", "Modern Classical", "Folk", "Acoustic",
    "Soul", "Blues", "Vocal",
)

# praise-term keyword → listening axis. Sentiment terms are community
# voice (tier F): shown as attributed matches, never converted to a score.
_AXIS_TERMS = {
    "sub_bass": ("bass", "slam", "sub", "impact", "punch", "dynamics"),
    "texture_stage": ("stage", "texture", "detail", "resolution",
                      "imaging", "separation", "air", "spacious", "wide"),
    "timbre_vocal": ("timbre", "natural", "organic", "vocal", "midrange", "tonal"),
}

_AXIS_LABELS = {
    "sub_bass": "sub-bass / slam",
    "texture_stage": "texture / stage",
    "timbre_vocal": "timbre / vocals",
}


def _library_axes() -> Dict[str, Any]:
    row = db_query_one(
        """
        WITH owned_albums AS (
          SELECT DISTINCT at.album_id FROM album_tracks at
          JOIN media_files mf ON mf.track_id = at.track_id
        ),
        weights AS (
          SELECT g.name, SUM(ag.count) AS w
          FROM album_genres ag
          JOIN genres g ON g.id = ag.genre_id
          JOIN owned_albums oa ON oa.album_id = ag.album_id
          GROUP BY g.name
        )
        SELECT
          ROUND(100.0 * SUM(w) FILTER (WHERE name = ANY(%(sub)s)) / SUM(w), 1) AS sub_bass_pct,
          ROUND(100.0 * SUM(w) FILTER (WHERE name = ANY(%(tex)s)) / SUM(w), 1) AS texture_pct,
          ROUND(100.0 * SUM(w) FILTER (WHERE name = ANY(%(tim)s)) / SUM(w), 1) AS timbre_pct
        FROM weights
        """,
        {"sub": list(_SUB_BASS_GENRES), "tex": list(_TEXTURE_GENRES),
         "tim": list(_TIMBRE_GENRES)},
    ) or {}
    dr = db_query_one(
        """
        SELECT ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY dynamic_range_db)::numeric, 1) AS p50,
               ROUND(percentile_cont(0.9) WITHIN GROUP (ORDER BY dynamic_range_db)::numeric, 1) AS p90
        FROM audio_features af
        WHERE EXISTS (SELECT 1 FROM media_files mf WHERE mf.track_id = af.track_id)
        """
    ) or {}
    axes = []
    for key, pct in (("sub_bass", row.get("sub_bass_pct")),
                     ("texture_stage", row.get("texture_pct")),
                     ("timbre_vocal", row.get("timbre_pct"))):
        axes.append({"axis": key, "label": _AXIS_LABELS[key],
                     "share_pct": float(pct) if pct is not None else None})
    axes.sort(key=lambda a: -(a["share_pct"] or 0))
    return {
        "axes": axes,
        "dr_p50": float(dr["p50"]) if dr.get("p50") else None,
        "dr_p90": float(dr["p90"]) if dr.get("p90") else None,
    }


def _plateau_diagnosis(analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Per owned electronics category: is there measurable room left?
    Transducers are excluded by design — that axis is taste, not
    plateau, and it is where the candidates section lives."""
    park = {c["model_id"]: c for c in analysis["components"]}
    specs_by_model = _load_specs([m for m in park])
    out: List[Dict[str, Any]] = []

    for comp in analysis["components"]:
        if comp["status"] != "own":
            continue
        cat = comp["category"]
        specs = specs_by_model.get(comp["model_id"], {})

        if cat == "dac":
            sinad = _num(specs, "sinad_db")
            snr = _num(specs, "signal_to_noise_db") or _num(specs, "dynamic_range_db")
            if (sinad is not None and sinad >= PLATEAU_SINAD_DB) or \
               (snr is not None and snr >= PLATEAU_SNR_DB):
                nums = f"SINAD {sinad:g} dB" if sinad is not None else f"SNR/DNR {snr:g} dB"
                out.append({
                    "name": comp["name"], "category": cat, "verdict": "plateau",
                    "numbers": nums, "tier": "m",
                    "reason": "transparency thresholds passed — a costlier DAC buys no "
                              "measurable gain in this chain; differences from here are "
                              "signature/taste, not fidelity",
                })
            else:
                out.append({
                    "name": comp["name"], "category": cat, "verdict": "open",
                    "numbers": "transparency metrics missing or below threshold",
                    "tier": "d", "reason": "cannot confirm a plateau from captured specs",
                })

        elif cat in ("amp", "player"):
            own_pairs = [p for p in analysis["pairs"]
                         if p["source"]["model_id"] == comp["model_id"]
                         and p["source"]["role"] == "hp_out"
                         and park.get(p["target"]["model_id"], {}).get("status") == "own"]
            evaluated = [p for p in own_pairs if p["status"] in ("ok", "warn", "fail")]
            if evaluated and all(p["status"] == "ok" for p in evaluated):
                out.append({
                    "name": comp["name"], "category": cat, "verdict": "plateau",
                    "numbers": f"{len(evaluated)} owned pairing(s), all pass with margin",
                    "tier": "d",
                    "reason": "every owned transducer is driven past the peak target with "
                              "headroom and clean damping — more power or a lower noise "
                              "floor changes nothing audible here",
                })
            elif evaluated:
                worst = min(evaluated, key=lambda p: {"fail": 0, "warn": 1, "ok": 2}[p["status"]])
                out.append({
                    "name": comp["name"], "category": cat, "verdict": "open",
                    "numbers": f'{worst["target"]["name"]}: {worst["status"]}',
                    "tier": "d",
                    "reason": "at least one owned pairing carries a caveat — see the "
                              "System screen before spending elsewhere",
                })

        elif cat in ("power", "cable"):
            out.append({
                "name": comp["name"], "category": cat, "verdict": "out_of_scope",
                "numbers": "no third-party audio-band measurements exist",
                "tier": "d",
                "reason": "functional engineering (protection, shielding, CMRR) is real; "
                          "audible-improvement claims sit outside measured evidence — "
                          "never the first place to spend",
            })
    return out


def _load_specs(model_ids: List[str]) -> Dict[str, Dict[str, str]]:
    if not model_ids:
        return {}
    rows = db_query(
        """
        SELECT gs.gear_model_id::text AS model_id, a.key, gs.value_text
        FROM gear_specs gs JOIN gear_spec_attributes a ON a.id = gs.attribute_id
        WHERE gs.gear_model_id = ANY(%(ids)s::uuid[])
        """,
        {"ids": model_ids},
    )
    out: Dict[str, Dict[str, str]] = {}
    for r in rows:
        out.setdefault(r["model_id"], {})[r["key"]] = r["value_text"]
    return out


def _candidates(analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Researched transducers not currently owned: the want-list plus
    the rest of the catalog. Small pool is stated, never hidden."""
    own_ids = {c["model_id"] for c in analysis["components"] if c["status"] == "own"}
    rows = db_query(
        """
        SELECT gm.id::text AS model_id, b.name AS brand, gm.model,
               gm.category::text AS category, gm.sentiment_score,
               gm.sentiment_sample_size, ug.status::text AS user_status
        FROM gear_models gm
        JOIN gear_brands b ON b.id = gm.brand_id
        LEFT JOIN user_gear ug ON ug.gear_model_id = gm.id
        WHERE gm.category IN ('headphones', 'iems')
          AND gm.research_state = 'cached'
        """
    )
    rows = [r for r in rows if r["model_id"] not in own_ids
            and r["user_status"] != "previously_owned"]
    specs_by_model = _load_specs([r["model_id"] for r in rows])
    terms_rows = db_query(
        """
        SELECT gear_model_id::text AS model_id, term
        FROM gear_sentiment_terms
        WHERE polarity = 'praise' AND gear_model_id = ANY(%(ids)s::uuid[])
        """,
        {"ids": [r["model_id"] for r in rows]},
    ) if rows else []
    praise_by_model: Dict[str, List[str]] = {}
    for t in terms_rows:
        praise_by_model.setdefault(t["model_id"], []).append(t["term"])

    pair_status: Dict[str, str] = {}
    order = {"fail": 0, "warn": 1, "nodata": 2, "ok": 3}
    for p in analysis["pairs"]:
        tid = p["target"]["model_id"]
        cur = pair_status.get(tid)
        if cur is None or order[p["status"]] < order[cur]:
            pair_status[tid] = p["status"]

    out = []
    for r in rows:
        specs = specs_by_model.get(r["model_id"], {})
        praise = praise_by_model.get(r["model_id"], [])
        axis_hits = {}
        for axis, keywords in _AXIS_TERMS.items():
            hits = [t for t in praise if any(k in t.lower() for k in keywords)]
            if hits:
                axis_hits[axis] = hits
        out.append({
            "model_id": r["model_id"],
            "name": f'{r["brand"]} {r["model"]}',
            "category": r["category"],
            "want": r["user_status"] == "want",
            "price_usd": _num(specs, "price_usd"),
            "driver_type": specs.get("driver_type"),
            "park_compatibility": pair_status.get(r["model_id"], "nodata"),
            "sentiment_score": float(r["sentiment_score"]) if r["sentiment_score"] is not None else None,
            "sentiment_sample": r["sentiment_sample_size"],
            "axis_hits": axis_hits,
        })
    out.sort(key=lambda c: (c["price_usd"] is None, c["price_usd"] or 0))
    return out


def advisor() -> Dict[str, Any]:
    analysis = system_analysis()
    candidates = _candidates(analysis)
    return {
        "library": _library_axes(),
        "plateau": _plateau_diagnosis(analysis),
        "candidates": candidates,
        "pool_note": (
            "Candidate pool = your want-list plus this node's researched catalog "
            f"({len(candidates)} model(s)). Measurement-registry imports (squig/ASR "
            "indexes) and the P2P network catalog widen it later — additions you "
            "make with status 'want' are researched automatically."
        ),
    }
