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
          AND ug.removed_at IS NULL
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
    caveat_rows = db_query(
        """
        SELECT gear_model_id::text AS model_id, role, severity::text AS severity,
               load_z_below, only_above_vrms, text, source_url
        FROM gear_measured_caveats
        WHERE gear_model_id = ANY(%(ids)s::uuid[])
        """,
        {"ids": ids},
    )
    caveats_by_model: Dict[str, List[Dict[str, Any]]] = {}
    for c in caveat_rows:
        caveats_by_model.setdefault(c["model_id"], []).append(c)
    for r in rows:
        r["specs"] = by_model.get(r["model_id"], {})
        r["caveats"] = caveats_by_model.get(r["model_id"], [])
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
        driver = specs.get("driver_type") or ""
        roles.append({
            "role": "transducer", "z": z, "sens_mw": sens_mw, "sens_v": sens_v,
            "max_spl": _num(specs, "max_spl_db"),
            "driver_type": driver,
            # Electrostats live in a different voltage domain entirely:
            # sensitivity per 100 Vrms + a DC bias supply requirement.
            "electrostatic": "electrostat" in driver.lower(),
            "sens_100v": _num(specs, "sensitivity_db_100v"),
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
            "bias_v": _num(specs, "electrostatic_bias_v"),
        })

    line_se = (_num(specs, "line_out_vrms_se") or _num(specs, "output_voltage_rca_vrms")
               or _num(specs, "preamp_out_vrms_max_se"))
    line_bal = (_num(specs, "line_out_vrms_bal") or _num(specs, "output_voltage_xlr_vrms")
                or _num(specs, "preamp_out_vrms_max_bal"))
    if line_se is not None or line_bal is not None:
        roles.append({
            "role": "line_out", "v_se": line_se, "v_bal": line_bal,
            # Volume-controlled preamp outs report their MAX level; the
            # fixed/max distinction matters for clipping, not bridging.
            "variable": _num(specs, "preamp_out_vrms_max_se") is not None
                        or _num(specs, "preamp_out_vrms_max_bal") is not None,
            "out_z_se": _num(specs, "output_impedance_rca_ohm")
                        or _num(specs, "line_output_impedance_ohm"),
            "out_z_bal": _num(specs, "output_impedance_xlr_ohm"),
        })

    if cat in ("power_amp", "integrated_amp"):
        roles.append({
            "role": "speaker_out",
            "p8": _num(specs, "output_power_8ohm_w"),
            "p4": _num(specs, "output_power_4ohm_w"),
            "stable_to": _num(specs, "stable_to_ohm"),
            "damping": _num(specs, "damping_factor"),
            "in_sens": _num(specs, "input_sensitivity_vrms"),
        })
        roles.append({
            "role": "line_in",
            "in_z_se": _num(specs, "line_input_impedance_ohm"),
            "in_z_bal": _num(specs, "line_input_impedance_bal_ohm"),
            "max_in_se": _num(specs, "max_input_vrms_se"),
            "max_in_bal": _num(specs, "max_input_vrms_bal"),
            "gain_max": _num(specs, "max_gain_db"),
            "power_sens": _num(specs, "input_sensitivity_vrms"),
        })

    if cat == "cartridge":
        roles.append({
            "role": "phono_source",
            "type": specs.get("cartridge_type"),
            "output_mv": _num(specs, "output_mv"),
            "z_int": _num(specs, "internal_impedance_ohm"),
            "rec_load": specs.get("recommended_load_ohm"),
            "compliance": _num(specs, "compliance_cu"),
            "mass_g": _num(specs, "cartridge_mass_g"),
        })

    if cat == "phono_stage":
        mc_flag = (specs.get("mc_input") or "").lower()
        roles.append({
            "role": "phono_in",
            "mm_gain": _num(specs, "mm_gain_db"),
            "mc_gain": _num(specs, "mc_gain_db"),
            # tri-state: True / False / None (not captured) — absence of
            # a gain NUMBER must never read as absence of the INPUT
            "mc_input": True if mc_flag == "true" else False if mc_flag == "false"
                        else (_num(specs, "mc_gain_db") is not None or None),
            "mm_cap": _num(specs, "mm_input_capacitance_pf"),
            "load_min": _num(specs, "mc_load_min_ohm"),
            "load_max": _num(specs, "mc_load_max_ohm"),
        })

    if cat == "turntable":
        roles.append({
            "role": "tonearm",
            "eff_mass": _num(specs, "effective_mass_g"),
        })

    if cat == "speakers":
        roles.append({
            "role": "speaker_load",
            "sens_2v83": _num(specs, "speaker_sensitivity_db_2v83"),
            "z_nom": _num(specs, "impedance_ohm"),
            "z_min": _num(specs, "impedance_min_ohm"),
            "powered": (specs.get("powered_speaker") or "").lower() == "true",
        })
        if (specs.get("powered_speaker") or "").lower() == "true":
            roles.append({
                "role": "line_in",
                "in_z_se": _num(specs, "line_input_impedance_ohm"),
                "in_z_bal": _num(specs, "line_input_impedance_bal_ohm"),
                "max_in_se": _num(specs, "max_input_vrms_se"),
                "max_in_bal": _num(specs, "max_input_vrms_bal"),
                "gain_max": None, "power_sens": None,
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

    if cat == "player":
        if (specs.get("usb_dac_mode") or "").lower() == "true":
            roles.append({"role": "usb_dac"})

    return roles


# ─── pair checks ────────────────────────────────────────────────────────────

def _check(name, status, numbers, tier, note=None):
    c = {"name": name, "status": status, "numbers": numbers, "tier": tier}
    if note:
        c["note"] = note
    return c


def _headroom_note(status: str) -> Optional[str]:
    """Plain-language reading of an SPL-headroom verdict — the practical
    'so what' behind the ±dB figure. Only warn/fail carry a note; a
    positive 'ok' margin is self-evident from the figure and green tier."""
    if status == "warn":
        return ("usable but tight — the loudest transients on wide-dynamic "
                "material can outrun the amp's headroom here; a stronger "
                "source is the only thing that adds real margin")
    if status == "fail":
        return ("underpowered for reference peaks — the loudest passages "
                "clip before target loudness; this pair needs a stronger source")
    return None


def _pair_hp_transducer(src, s_role, dst, d_role) -> Dict[str, Any]:
    checks = []

    # True-ribbon domain gate: a sub-ohm ribbon motor is a near-short
    # for any voltage amplifier — the damping arithmetic below would
    # grade it as a mild warn when the physics is "amp into protection,
    # ribbon at risk". Same hard boundary as electrostats; the escape
    # is the bundled current-drive/transformer interface, whose INPUT
    # (typically ~32 ohm) is what an amp actually pairs with.
    if (d_role.get("driver_type") or "").lower() == "ribbon" and \
            d_role.get("z") is not None and d_role["z"] < 1:
        checks.append(_check(
            "domain", "fail",
            f"true-ribbon motor at {d_role['z']:g} Ω — a near-short no conventional "
            "headphone output may drive directly",
            "ds",
            "connect only through the ribbon's current-drive/transformer interface "
            "(bundled with such headphones); the amp then sees the interface's "
            "input impedance (~32 Ω class), and THAT is the load to judge power against",
        ))
        return _verdict(src, "hp_out", dst, "transducer", checks)

    # Electrostatic domain gate: bias supply + 100V-swing territory.
    # A conventional headphone output is a hard incompatibility, not a
    # headroom question — different connector, different physics.
    if d_role.get("electrostatic"):
        if s_role.get("bias_v") is None:
            checks.append(_check(
                "domain", "fail",
                "electrostatic load — needs an energizer (DC bias + 100V-domain swing); "
                "conventional headphone outputs cannot drive it",
                "ds",
                "an electrostat needs a dedicated energizer — DC bias plus a "
                "hundreds-of-volts swing that a conventional headphone output can "
                "neither supply nor connect to",
            ))
            return _verdict(src, "hp_out", dst, "transducer", checks)
        # Matched electrostatic domain — surface it as a positive check so
        # the correct partner reads as compatible, not merely "nodata" when
        # the headroom maths below lacks the energizer's voltage swing.
        checks.append(_check(
            "domain", "ok",
            f"energizer + electrostat — matched domain (DC bias {s_role['bias_v']:.0f} V)",
            "ds",
        ))
        sens100, rail = d_role.get("sens_100v"), s_role["rail_v"]
        if sens100 is not None and rail:
            need_v = 100 * 10 ** ((PEAK_TARGET_DB - sens100) / 20)
            margin = 20 * math.log10(rail / need_v)
            status = ("ok" if margin >= THRESHOLDS["headroom_ok_db"]
                      else "warn" if margin >= 0 else "fail")
            checks.append(_check(
                "spl_headroom", status,
                f"{PEAK_TARGET_DB} dB peaks need {need_v:.0f} Vrms; energizer swings ~{rail:.0f} Vrms → {margin:+.0f} dB",
                s_role["rail_tier"],
                _headroom_note(status),
            ))
        else:
            checks.append(_check(
                "spl_headroom", "nodata",
                "missing: sensitivity per 100 Vrms or energizer voltage swing", "d",
            ))
        return _verdict(src, "hp_out", dst, "transducer", checks)

    if s_role.get("bias_v") is not None:
        # Energizer × conventional transducer is a non-pair, not a bad
        # pair: a fail row per headphone is noise. The estat ×
        # conventional-source direction stays — it explains what a
        # candidate the user is actually eyeing is missing.
        return None

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
            _headroom_note(status),
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


# Speaker SPL math needs a listening geometry; without a room model we
# state the assumption instead of hiding it. The per-channel free-field
# figure is deliberately conservative, so the VERDICT must include the
# known corrections as explicit terms — a model that knows it underrates
# by "several dB" may not fail a pairing inside those several dB.
LISTENING_DISTANCE_M = 2.5
SPEAKER_PEAK_TARGET_DB = 105   # at the listening position
STEREO_SUM_DB = 3              # two channels: +3 (uncorrelated) to +6 (coherent bass)
ROOM_GAIN_DB = 3               # domestic reverberant field vs free-field at 2.5 m


def _pair_speaker(src, s_role, dst, d_role) -> Dict[str, Any]:
    checks = []
    if d_role.get("powered"):
        checks.append(_check(
            "domain", "warn",
            "active speaker — feed it line level; a power amplifier output would damage it",
            "ds",
        ))
        return _verdict(src, "speaker_out", dst, "speaker_load", checks)

    sens, z_nom, z_min = d_role["sens_2v83"], d_role["z_nom"], d_role["z_min"]
    load_z = z_min or z_nom
    p_avail = None
    if load_z is not None and load_z < 6 and s_role["p4"] is not None:
        p_avail = s_role["p4"]
    elif s_role["p8"] is not None:
        p_avail = s_role["p8"]
    else:
        p_avail = s_role["p4"]

    if sens is None or p_avail is None:
        missing = [k for k, v in (("speaker sensitivity", sens),
                                  ("amp power into this load", p_avail)) if v is None]
        checks.append(_check("spl_headroom", "nodata", f"missing: {', '.join(missing)}", "d"))
    else:
        spl_1m = sens + 10 * math.log10(max(p_avail, 0.001))
        spl_pos = spl_1m - 20 * math.log10(LISTENING_DISTANCE_M)
        margin_cons = spl_pos - SPEAKER_PEAK_TARGET_DB
        margin_real = margin_cons + STEREO_SUM_DB + ROOM_GAIN_DB
        status = ("ok" if margin_real >= THRESHOLDS["headroom_ok_db"]
                  else "warn" if margin_real >= 0 else "fail")
        checks.append(_check(
            "spl_headroom", status,
            f"{p_avail:.0f} W into ~{(load_z or 8):g} Ω → {spl_1m:.0f} dB @1m; per-channel free-field "
            f"{margin_cons:+.0f} dB vs {SPEAKER_PEAK_TARGET_DB} dB at {LISTENING_DISTANCE_M} m, "
            f"≈{margin_real:+.0f} dB with stereo +{STEREO_SUM_DB} and room +{ROOM_GAIN_DB}",
            "d",
            "verdict uses the realistic figure; both terms shown — the free-field "
            "number is the conservative floor, not the experience",
        ))

    if z_min is not None:
        stable = s_role["stable_to"]
        if stable is None:
            checks.append(_check(
                "load", "nodata",
                f"speaker dips to {z_min:g} Ω; amp minimum stable load unpublished", "d",
            ))
        else:
            status = "ok" if stable <= z_min else "fail"
            checks.append(_check(
                "load", status,
                f"impedance minimum {z_min:g} Ω vs amp stable into {stable:g} Ω",
                "ds",
                None if status == "ok" else
                "the speaker's impedance dips below what the amp is specified to drive",
            ))
    elif z_nom is not None:
        checks.append(_check(
            "load", "nodata",
            f"nominal {z_nom:g} Ω only — impedance CURVE minimum unknown; nominal ratings hide dips",
            "d",
        ))

    if s_role["damping"] is not None:
        checks.append(_check(
            "damping", "ok" if s_role["damping"] >= 50 else "warn",
            f"damping factor {s_role['damping']:g} (ref 8 Ω)",
            "ds",
            None if s_role["damping"] >= 50 else
            "single-digit damping interacts audibly with the speaker impedance curve — "
            "tube-amp territory, a choice rather than a defect",
        ))

    return _verdict(src, "speaker_out", dst, "speaker_load", checks)


def _pair_phono(src, s_role, dst, d_role) -> Dict[str, Any]:
    """Cartridge → phono stage: gain domain (MM vs MC is as hard a
    boundary as the electrostatic one), noise-margin math on the
    resulting line level, and MC resistive loading vs the stage's
    selectable range."""
    checks = []
    ctype, out_mv = s_role.get("type"), s_role.get("output_mv")
    is_mc_low = ctype == "mc_low"

    gain = d_role["mc_gain"] if is_mc_low else d_role["mm_gain"]
    if is_mc_low and d_role.get("mc_input") is False:
        checks.append(_check(
            "domain", "fail",
            "low-output MC cartridge into an MM-only stage — 40 dB of gain leaves the "
            "signal in the noise floor; an MC input (60+ dB) or a step-up transformer is required",
            "ds",
        ))
        return _verdict(src, "phono_source", dst, "phono_in", checks)
    if is_mc_low and d_role.get("mc_input") is None:
        checks.append(_check(
            "domain", "nodata",
            "MC support not captured for this stage — verify an MC input exists before pairing",
            "d",
        ))
        return _verdict(src, "phono_source", dst, "phono_in", checks)

    if out_mv is None or gain is None:
        missing = [k for k, v in (("cartridge output (mV)", out_mv),
                                  ("stage gain for this type", gain)) if v is None]
        checks.append(_check("gain", "nodata", f"missing: {', '.join(missing)}", "d"))
    else:
        line_v = out_mv / 1000 * (10 ** (gain / 20))
        status = "ok" if 0.2 <= line_v <= 2.5 else "warn"
        note = None
        if line_v < 0.2:
            note = "resulting line level is low — expect extra preamp gain and a higher noise floor"
        elif line_v > 2.5:
            note = "hot output — fine into a volume control, watch input ceilings downstream"
        checks.append(_check(
            "gain", status,
            f"{out_mv:g} mV × {gain:g} dB → ~{line_v:.2f} Vrms line level",
            "ds", note,
        ))

    if is_mc_low:
        z_int = s_role.get("z_int")
        lo, hi = d_role["load_min"], d_role["load_max"]
        if z_int is not None and lo is not None and hi is not None:
            want_lo, want_hi = z_int * 10, z_int * 25
            overlap = not (hi < want_lo or lo > want_hi)
            checks.append(_check(
                "load", "ok" if overlap else "warn",
                f"coil {z_int:g} Ω → wants ~{want_lo:.0f}-{want_hi:.0f} Ω; stage offers {lo:g}-{hi:g} Ω",
                "ds",
                None if overlap else
                "no overlap with the 10-25x rule of thumb — tonally usable but check the "
                "manufacturer's own loading advice"
                + (f" ({s_role['rec_load']})" if s_role.get("rec_load") else ""),
            ))
        elif s_role.get("rec_load"):
            checks.append(_check(
                "load", "nodata",
                f"manufacturer recommends {s_role['rec_load']} Ω; stage load options not captured",
                "d",
            ))
    elif ctype == "mm" and d_role["mm_cap"] is not None:
        checks.append(_check(
            "load", "ok",
            f"MM input capacitance {d_role['mm_cap']:g} pF (add tonearm cable ~100 pF against "
            "the cartridge's recommended total)",
            "ds",
        ))

    return _verdict(src, "phono_source", dst, "phono_in", checks)


def _pair_tonearm(src, s_role, dst, d_role) -> Dict[str, Any]:
    """Cartridge → tonearm: the classic resonance calculation.
    f = 1000 / (2π · sqrt(M_total · compliance)), M in grams, compliance
    in cu — target 8-12 Hz (below: warp-wow coupling; above: audible
    band intrusion)."""
    checks = []
    compliance, mass = s_role.get("compliance"), s_role.get("mass_g")
    eff = d_role.get("eff_mass")
    if compliance is None or mass is None or eff is None:
        missing = [k for k, v in (("compliance (10 Hz)", compliance),
                                  ("cartridge mass", mass),
                                  ("tonearm effective mass", eff)) if v is None]
        checks.append(_check("resonance", "nodata", f"missing: {', '.join(missing)}", "d"))
    else:
        m_total = eff + mass + 1.0  # +1 g fasteners
        f_res = 1000 / (2 * math.pi * math.sqrt(m_total * compliance))
        status = "ok" if 8 <= f_res <= 12 else "warn"
        note = None
        if f_res < 8:
            note = "below 8 Hz — couples with warp/footfall energy; a lighter arm or lower-mass cartridge helps"
        elif f_res > 12:
            note = "above 12 Hz — resonance creeps toward the audible band; a heavier arm or headshell weight helps"
        checks.append(_check(
            "resonance", status,
            f"eff. mass {eff:g} g + cart {mass:g} g + 1 g → {f_res:.1f} Hz vs 8-12 Hz window",
            "d", note,
        ))
    return _verdict(src, "phono_source", dst, "tonearm", checks)


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
        variable = bool(s_role.get("variable"))
        max_in = d_role["max_in_bal"] if balanced else d_role["max_in_se"]
        if max_in is not None:
            margin = 20 * math.log10(max_in / v)
            if margin >= 0:
                status, note = "ok", None
            elif variable:
                # A volume-controlled preamp quoting its MAX level can
                # always back off — headroom deficit is operational, not
                # a hard mismatch.
                status, note = "ok", "preamp max exceeds amp input ceiling — volume stays below max, no clipping in normal use"
            else:
                status, note = "fail", None
            checks.append(_check(
                "level", status,
                f"{conn} {v:g} Vrms{' (max, volume-controlled)' if variable else ''} vs amp max input "
                f"{max_in:g} Vrms → {margin:+.1f} dB input headroom"
                + (f"; gain {d_role['gain_max']:+g} dB max" if d_role["gain_max"] is not None else ""),
                "ds", note,
            ))
        else:
            checks.append(_check(
                "level", "ok",
                f"{conn} line level {v:g} Vrms{' (max, volume-controlled)' if variable else ''} into amp input"
                + (f"; amp gain up to {d_role['gain_max']:+g} dB" if d_role["gain_max"] is not None else ""),
                "ds", "amp max input level not captured — clipping check partial",
            ))

        # Power-amp gain staging: can this source drive the amp to its
        # full rated power? Judged against input sensitivity when known.
        power_sens = d_role.get("power_sens")
        if power_sens is not None:
            margin = 20 * math.log10(v / power_sens)
            checks.append(_check(
                "full_power", "ok" if margin >= 0 else "warn",
                f"amp reaches full power at {power_sens:g} Vrms; source delivers up to {v:g} Vrms → {margin:+.1f} dB",
                "ds",
                None if margin >= 0 else
                "full rated power is unreachable from this source — fine if the SPL "
                "budget above already clears the target",
            ))

    return _verdict(src, "line_out", dst, "line_in", checks)


def _need_vrms(item) -> Optional[float]:
    """Voltage a transducer needs for PEAK_TARGET_DB, or None."""
    specs = item.get("specs", {})
    z, sens_mw = _num(specs, "impedance_ohm"), _num(specs, "sensitivity_db_mw")
    if z is None or sens_mw is None or z <= 0:
        return None
    sens_v = sens_mw + 10 * math.log10(1000 / z)
    return 10 ** ((PEAK_TARGET_DB - sens_v) / 20)


def _apply_caveats(src, s_role_name, dst, d_role_name, checks) -> None:
    """Measurement-sourced behavior gates spec math: a caveat attached
    to either side's active role (or model-wide) joins the checks —
    'warn' severity drags the pair status with it via the aggregate."""
    for item, active_role, partner in ((src, s_role_name, dst), (dst, d_role_name, src)):
        for cv in item.get("caveats", []):
            if cv["role"] is not None and cv["role"] != active_role:
                continue
            # Conditional caveats: a condition can pass (caveat bites),
            # refute (skip), or be UNVERIFIABLE when the partner's data
            # is missing. Unverifiable also skips — the pair's own
            # nodata checks already say the partner is unmeasured, and
            # advice-shaped text reads as a live warning no matter what
            # status it wears. The finding itself belongs to the model
            # sheet, not to a pair we can't evaluate.
            applies = True
            if cv.get("load_z_below") is not None:
                partner_z = _num(partner.get("specs", {}), "impedance_ohm")
                if partner_z is None or partner_z >= cv["load_z_below"]:
                    applies = False
            if applies and cv.get("only_above_vrms") is not None:
                need_v = _need_vrms(partner)
                if need_v is None or need_v <= cv["only_above_vrms"]:
                    applies = False
            if not applies:
                continue
            checks.append(_check(
                "measured", "warn" if cv["severity"] == "warn" else "ok",
                f'{item["name"]}: {cv["text"]}', "m",
            ))


def _verdict(src, s_role, dst, d_role, checks) -> Dict[str, Any]:
    _apply_caveats(src, s_role, dst, d_role, checks)
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
            # No own/want gate. want↔want pairs are electrically real and
            # sometimes the ONLY meaningful pairing: an energizer + an
            # electrostat you are eyeing together drive nothing in your
            # owned rack, so gating them out hides the one verdict that
            # matters. The loop below only emits real (out-port, in-port)
            # pairs, so there is no want↔want noise to suppress here.
            if sr["role"] == "hp_out" and dr["role"] == "transducer":
                v = _pair_hp_transducer(src, sr, dst, dr)
                if v:
                    pairs.append(v)
            elif sr["role"] == "line_out" and dr["role"] == "line_in":
                pairs.append(_pair_line(src, sr, dst, dr))
            elif sr["role"] == "speaker_out" and dr["role"] == "speaker_load":
                pairs.append(_pair_speaker(src, sr, dst, dr))
            elif sr["role"] == "phono_source" and dr["role"] == "phono_in":
                pairs.append(_pair_phono(src, sr, dst, dr))
            elif sr["role"] == "phono_source" and dr["role"] == "tonearm":
                pairs.append(_pair_tonearm(src, sr, dst, dr))

    # Community pair-synergy voice (the fourth voice, tier F): attached
    # to any pair whose model combination carries a cached, discussed
    # note. Never gates the status — physics gates, consensus informs.
    note_rows = db_query(
        """
        SELECT model_a::text AS a, model_b::text AS b, summary, terms, sample_size
        FROM gear_pair_notes
        WHERE research_state = 'cached' AND summary IS NOT NULL
        """
    )
    notes = {(r["a"], r["b"]): r for r in note_rows}
    for p in pairs:
        key = tuple(sorted((p["source"]["model_id"], p["target"]["model_id"])))
        n = notes.get(key)
        if n:
            terms = " · ".join(n["terms"] or []) or (n["summary"] or "")[:120]
            p["checks"].append(_check(
                "synergy", "ok",
                f'{terms} (~{n["sample_size"] or "?"} voices)',
                "f", n["summary"],
            ))

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
