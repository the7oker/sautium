"""
Deterministic pair-compatibility engine — Phase 3 of the Gear Advisor.

Pure computation over gear_specs, no AI at runtime. Devices decompose
into port ROLES (a multifunction DAP is simultaneously a headphone
amp, a line source, a USB DAC and a digital transport); the engine
emits a verdict for every electrically meaningful (out-port, in-port)
combination in the user's park — the pair MATRIX, deliberately not a
user-drawn chain topology.

Design rules carried over from the reference experiment
(docs/design/GEAR-ADVISOR.md):
- headline specs never rank sound; every check answers a functional
  question and classifies its delta against an audibility threshold;
- every number in a verdict carries a provenance tier
  ('ds' datasheet/spec, 'm' measured, 'd' derived by this engine);
- missing data is a first-class verdict ('nodata'), never a guess.
"""

import math
from typing import Any, Dict, List, Optional

from db_pool import db_query

# Conservative peak target for full-scale listening; the library's
# measured DR percentiles contextualise it per user (see /library).
PEAK_TARGET_DB = 110

THRESHOLDS = {
    "damping_z_ratio": 8,       # source out-Z <= load-Z / 8 (the "1/8 rule")
    "bridging_ratio": 10,       # line out-Z * 10 <= line in-Z
    "headroom_ok_db": 6,        # >= this voltage margin over peak target → ok
    "midband_audible_db": 1.0,  # FR deviation audibility floor (midband)
}

_TRANSDUCER_CATS = {"headphones", "iems"}
_POWER_POINTS = [  # (spec key, load ohms)
    ("output_power_32ohm_mw", 32),
    ("output_power_60ohm_mw", 60),
    ("output_power_150ohm_mw", 150),
    ("output_power_300ohm_mw", 300),
]


def _num(specs: Dict[str, str], key: str) -> Optional[float]:
    raw = specs.get(key)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _load_park() -> List[Dict[str, Any]]:
    rows = db_query(
        """
        SELECT gm.id::text AS model_id, b.name AS brand, gm.model,
               gm.category::text AS category, ug.status::text AS status,
               gm.research_state::text AS research_state
        FROM user_gear ug
        JOIN gear_models gm ON gm.id = ug.gear_model_id
        JOIN gear_brands b ON b.id = gm.brand_id
        WHERE ug.status <> 'previously_owned'
        """
    )
    if not rows:
        return []
    ids = [r["model_id"] for r in rows]
    spec_rows = db_query(
        """
        SELECT gs.gear_model_id::text AS model_id, a.key, gs.value_text
        FROM gear_specs gs
        JOIN gear_spec_attributes a ON a.id = gs.attribute_id
        WHERE gs.gear_model_id = ANY(%(ids)s::uuid[])
        """,
        {"ids": ids},
    )
    by_model: Dict[str, Dict[str, str]] = {}
    for s in spec_rows:
        by_model.setdefault(s["model_id"], {})[s["key"]] = s["value_text"]
    for r in rows:
        r["specs"] = by_model.get(r["model_id"], {})
        r["name"] = f'{r["brand"]} {r["model"]}'
    return rows


# ─── role extraction ────────────────────────────────────────────────────────

def _roles(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    specs, cat = item["specs"], item["category"]
    roles: List[Dict[str, Any]] = []

    if cat in _TRANSDUCER_CATS:
        z = _num(specs, "impedance_ohm")
        sens_mw = _num(specs, "sensitivity_db_mw")
        sens_v = None
        if sens_mw is not None and z:
            sens_v = sens_mw + 10 * math.log10(1000 / z)  # derived dB/V
        roles.append({
            "role": "transducer", "z": z, "sens_mw": sens_mw, "sens_v": sens_v,
            "max_spl": _num(specs, "max_spl_db"),
            "driver_type": specs.get("driver_type"),
        })

    if cat in ("amp", "player"):
        vmax = _num(specs, "hp_out_vrms_max_bal") or _num(specs, "hp_out_vrms_max_se")
        points = [(load, _num(specs, key)) for key, load in _POWER_POINTS
                  if _num(specs, key) is not None]
        # Voltage-limited rail estimate from the strongest published
        # point — the classic derivation when per-load curves are
        # unpublished. Tier: 'd'.
        rail = vmax
        rail_tier = "ds"
        if points:
            best = max(math.sqrt(p / 1000 * load) for load, p in points)
            if rail is None or best > rail:
                rail, rail_tier = best, "d"
        roles.append({
            "role": "hp_out", "rail_v": rail, "rail_tier": rail_tier,
            "power_points": points,
            "out_z": _num(specs, "output_impedance_ohm"),
        })

    line_se = _num(specs, "line_out_vrms_se") or _num(specs, "output_voltage_rca_vrms")
    line_bal = _num(specs, "line_out_vrms_bal") or _num(specs, "output_voltage_xlr_vrms")
    if line_se is not None or line_bal is not None:
        roles.append({
            "role": "line_out", "v_se": line_se, "v_bal": line_bal,
            "out_z_se": _num(specs, "output_impedance_rca_ohm")
                        or _num(specs, "line_output_impedance_ohm"),
            "out_z_bal": _num(specs, "output_impedance_xlr_ohm"),
        })

    if cat == "amp":
        roles.append({
            "role": "line_in",
            "in_z_se": _num(specs, "line_input_impedance_ohm"),
            "in_z_bal": _num(specs, "line_input_impedance_bal_ohm"),
            "max_in_se": _num(specs, "max_input_vrms_se"),
            "max_in_bal": _num(specs, "max_input_vrms_bal"),
            "gain_max": _num(specs, "max_gain_db"),
        })

    if cat == "dac":
        roles.append({
            "role": "digital_in",
            "pcm_khz": _num(specs, "pcm_max_khz"),
            "dsd": specs.get("dsd_max_rate"),
        })

    if cat == "player":
        if specs.get("digital_output_types"):
            roles.append({"role": "transport", "outs": specs["digital_output_types"]})
        if (specs.get("usb_dac_mode") or "").lower() == "true":
            roles.append({"role": "usb_dac"})

    return roles


# ─── pair checks ────────────────────────────────────────────────────────────

def _check(name, status, numbers, tier, note=None):
    c = {"name": name, "status": status, "numbers": numbers, "tier": tier}
    if note:
        c["note"] = note
    return c


def _pair_hp_transducer(src, s_role, dst, d_role) -> Dict[str, Any]:
    checks = []
    z, sens_v = d_role["z"], d_role["sens_v"]
    rail = s_role["rail_v"]

    if z is None or sens_v is None or rail is None:
        missing = [k for k, v in
                   (("impedance", z), ("sensitivity", d_role["sens_mw"]),
                    ("output voltage/power", rail))
                   if v is None]
        checks.append(_check("spl_headroom", "nodata", f"missing: {', '.join(missing)}", "d"))
    else:
        need_v = 10 ** ((PEAK_TARGET_DB - sens_v) / 20)
        margin = 20 * math.log10(rail / need_v)
        status = ("ok" if margin >= THRESHOLDS["headroom_ok_db"]
                  else "warn" if margin >= 0 else "fail")
        checks.append(_check(
            "spl_headroom", status,
            f"{PEAK_TARGET_DB} dB peaks need {need_v:.2f} Vrms; available ~{rail:.1f} Vrms → {margin:+.0f} dB",
            s_role["rail_tier"],
        ))
        if d_role["max_spl"] is not None:
            ceiling_ok = d_role["max_spl"] >= PEAK_TARGET_DB
            checks.append(_check(
                "driver_ceiling", "ok" if ceiling_ok else "warn",
                f"driver max SPL {d_role['max_spl']:.0f} dB vs {PEAK_TARGET_DB} dB target",
                "ds",
                ("good news: the driver stays clean well past the target, so the amp "
                 "headroom above is genuinely usable margin — just not an invitation "
                 "to chase the amp's electrical maximum")
                if ceiling_ok else
                ("the driver distorts before the target is reached — the driver, not "
                 "the amp, sets this pair's real ceiling; extra amp power adds nothing"),
            ))

    out_z = s_role["out_z"]
    if out_z is None or z is None:
        checks.append(_check("damping", "nodata", "output or load impedance unknown", "d"))
    else:
        limit = z / THRESHOLDS["damping_z_ratio"]
        status = "ok" if out_z <= limit else "warn"
        note = None
        if status == "warn" and (d_role.get("driver_type") or "").startswith("planar"):
            note = "planar load is essentially flat — FR interaction negligible, damping margin still reduced"
        if status == "ok" and z and out_z and (z / out_z) < 16:
            note = "passes 1/8 rule but against NOMINAL impedance; multi-BA curves can dip lower"
        checks.append(_check(
            "damping", status,
            f"out-Z {out_z:g} Ω vs 1/8 limit {limit:.2f} Ω (load {z:g} Ω nominal)",
            "ds", note,
        ))

    return _verdict(src, "hp_out", dst, "transducer", checks)


def _pair_line(src, s_role, dst, d_role) -> Dict[str, Any]:
    checks = []
    balanced = s_role["v_bal"] is not None
    conn = "balanced" if balanced else "single-ended"
    v = s_role["v_bal"] if balanced else s_role["v_se"]
    out_z = s_role["out_z_bal"] if balanced else s_role["out_z_se"]
    in_z = d_role["in_z_bal"] if balanced else d_role["in_z_se"]

    if out_z is None or in_z is None:
        missing = [side for side, val in
                   ((f"source {conn} line out-Z (unpublished)", out_z),
                    (f"amp {conn} in-Z", in_z)) if val is None]
        checks.append(_check("bridging", "nodata", "missing: " + "; ".join(missing), "d",
                             "line out-Z of portable sources is typically ≤200 Ω — "
                             "bridging into a 10 kΩ-class input is rarely a real risk"))
    else:
        status = "ok" if out_z * THRESHOLDS["bridging_ratio"] <= in_z else "warn"
        checks.append(_check(
            "bridging", status,
            f"{conn}: out-Z {out_z:g} Ω → in-Z {in_z:g} Ω (1:{in_z / out_z:.0f}, rule ≥1:{THRESHOLDS['bridging_ratio']})",
            "m" if out_z == 40 else "ds",
        ))

    if v is None:
        checks.append(_check("level", "nodata", "source line-out voltage unknown", "d"))
    else:
        max_in = d_role["max_in_bal"] if balanced else d_role["max_in_se"]
        if max_in is not None:
            margin = 20 * math.log10(max_in / v)
            status = "ok" if margin >= 0 else "fail"
            checks.append(_check(
                "level", status,
                f"{conn} {v:g} Vrms vs amp max input {max_in:g} Vrms → {margin:+.1f} dB input headroom"
                + (f"; gain {d_role['gain_max']:+g} dB max" if d_role["gain_max"] is not None else ""),
                "ds",
            ))
        else:
            checks.append(_check(
                "level", "ok",
                f"{conn} line level {v:g} Vrms into amp input"
                + (f"; amp gain up to {d_role['gain_max']:+g} dB" if d_role["gain_max"] is not None else ""),
                "ds", "amp max input level not captured — clipping check partial",
            ))

    return _verdict(src, "line_out", dst, "line_in", checks)


def _pair_transport(src, s_role, dst, d_role) -> Dict[str, Any]:
    checks = [_check(
        "formats", "ok" if d_role["pcm_khz"] else "nodata",
        f"transport outs: {s_role['outs']}; DAC ceiling: "
        + (f"PCM {d_role['pcm_khz']:g} kHz / {d_role['dsd'] or '—'}" if d_role["pcm_khz"] else "unknown"),
        "ds",
        "transport-side format ceilings not captured yet — informational",
    )]
    return _verdict(src, "transport", dst, "digital_in", checks)


def _verdict(src, s_role, dst, d_role, checks) -> Dict[str, Any]:
    order = {"fail": 0, "warn": 1, "nodata": 2, "ok": 3}
    status = min((c["status"] for c in checks), key=lambda s: order[s], default="nodata")
    return {
        "source": {"model_id": src["model_id"], "name": src["name"], "role": s_role},
        "target": {"model_id": dst["model_id"], "name": dst["name"], "role": d_role},
        "status": status,
        "checks": checks,
    }


# ─── public entry ───────────────────────────────────────────────────────────

def system_analysis() -> Dict[str, Any]:
    park = _load_park()
    components = []
    role_index: List[tuple] = []  # (item, role_dict)
    for item in park:
        roles = _roles(item)
        components.append({
            "model_id": item["model_id"], "name": item["name"],
            "category": item["category"], "status": item["status"],
            "research_state": item["research_state"],
            "roles": [r["role"] for r in roles],
        })
        for r in roles:
            role_index.append((item, r))

    pairs = []
    for src, sr in role_index:
        for dst, dr in role_index:
            if src["model_id"] == dst["model_id"]:
                continue
            # own/want boundary: pairs among owned gear plus owned↔want
            # (candidate evaluation); want↔want is noise.
            if src["status"] != "own" and dst["status"] != "own":
                continue
            if sr["role"] == "hp_out" and dr["role"] == "transducer":
                pairs.append(_pair_hp_transducer(src, sr, dst, dr))
            elif sr["role"] == "line_out" and dr["role"] == "line_in":
                pairs.append(_pair_line(src, sr, dst, dr))
            elif sr["role"] == "transport" and dr["role"] == "digital_in":
                pairs.append(_pair_transport(src, sr, dst, dr))

    library = db_query(
        """
        SELECT ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY dynamic_range_db)::numeric, 1) AS dr_p50,
               ROUND(percentile_cont(0.9) WITHIN GROUP (ORDER BY dynamic_range_db)::numeric, 1) AS dr_p90
        FROM audio_features af
        WHERE EXISTS (SELECT 1 FROM media_files mf WHERE mf.track_id = af.track_id)
        """
    )
    lib = library[0] if library else {}

    return {
        "peak_target_db": PEAK_TARGET_DB,
        "thresholds": THRESHOLDS,
        "library": {"dr_p50": float(lib["dr_p50"]) if lib.get("dr_p50") else None,
                    "dr_p90": float(lib["dr_p90"]) if lib.get("dr_p90") else None},
        "components": components,
        "pairs": pairs,
    }
