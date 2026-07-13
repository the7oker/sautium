"""
Research-worker prompt template.

The (future) gear research worker takes a queued `gear_models` row,
runs WebSearch + WebFetch on audiophile sources (Head-Fi, ASR,
manufacturer pages, patent registries) and asks Claude to extract
canonical specs, brand-IP technologies and a community-sentiment
summary. Output lands as rows in `gear_specs`, `gear_technologies`,
`gear_model_technologies`, `gear_sentiment_terms` plus the
sentiment aggregate columns on `gear_models`.

The hard part of running this on many user installs is producing
*the same* attribute keys for the same physical fact. This template
enforces that by injecting the current canonical catalog into every
call and instructing Claude to reuse-or-propose. New attributes land
with `seeded=FALSE` and can be reviewed before promotion.
"""

from typing import Dict, List

from db_pool import db_query


_LANG_AND_KEY_RULES = """
LANGUAGE
- All attribute/technology keys, labels, descriptions and enum values
  are in **English**. Localisation happens client-side later.
- Values are kept in their canonical SI/audiophile form: impedance in
  ohms ("300"), sample rate in kHz ("44.1") or MHz ("1.536"), price in
  USD without currency symbol ("3700"). Units live on the attribute.

KEY FORMAT
- snake_case, descriptive, lowercase.
- Technical specs: short and physical ("impedance_ohm", "pcm_max_khz",
  "driver_type", "total_harmonic_distortion_pct").
- Technologies: brand-prefixed dotted form to avoid collisions:
  "sennheiser.ring_radiator", "holo.linear_compensation",
  "chord.wta_filter". Use lowercase brand slug from the catalog.
- Industry-wide technologies (not owned by one brand): no prefix, just
  the key: "i2s", "dsd_native", "asynchronous_usb".
"""

_REUSE_RULES = """
REUSE THE CATALOG
- For every spec / technology you extract, scan the canonical catalog
  below first. If the semantic matches, use that exact key. Do NOT
  invent variants like `sn_db` when `signal_to_noise_db` exists.
- A new attribute is justified only if the physical quantity is not
  represented in the catalog. When you propose one, include:
  key, label, description (2-3 sentences explaining what it measures
  and what it affects in listening), unit, value_type
  (number / string / enum / boolean), enum_values (if enum), and
  applies_to (list of gear categories).
- A new technology is justified only if it is a *named, distinct*
  proprietary feature (not a generic spec). Include: key, label,
  description, brand (must match the canonical brands or be a new
  one to propose), patent_or_source (URL or patent number, optional),
  introduced_year, applies_to.
- Attach a brand technology to THIS model only when a source ties it
  to this exact model or its series. Brands introduce technologies in
  newer lines and market them site-wide — do NOT generalize across
  the whole range (observed failure: a non-crossover topology from a
  brand's newest series was wrongly attached to its older
  crossover-based models).

CITATIONS
- Every spec value must come from a verifiable source. The
  `source_url` is required and ranked by authority:
  manufacturer datasheet > ASR review with measurements >
  Head-Fi thread with confirmed impressions > forum hearsay.
- If you cannot verify a value, omit it rather than guess.

SENTIMENT
- Score is on a 0-10 scale, rounded to one decimal.
- sample_size is the *approximate* number of distinct owner posts /
  reviews you considered. Don't pretend precision.
- praise/criticism are short noun phrases ("organic timbre",
  "runs hot"), 2-4 words. Limit to 6 per polarity.

MEASURED CAVEATS
- Behavioral findings from MEASUREMENT-TIER sources ONLY (bench
  reviews with instruments: ASR, GoldenSound, L7Audiolab, Stereophile
  measurements) that a spec sheet hides and that change how the
  device should be paired or used: "clips into low-impedance loads
  at high gain", "driver bottoms out at 114 dB", "elevated noise
  floor on max gain". NOT community opinion, NOT tonal impressions.
- 0-3 entries; omit the section's entries entirely if no instrumented
  source documents such behavior. Each needs a source_url.
- role: which port the caveat gates — "hp_out", "line_out",
  "transducer", or null for model-wide (e.g. thermal).
- severity: "warn" if it should flag an affected pairing, "info" if
  purely contextual.
- load_z_below (optional number, ohms): when the finding only applies
  below a load-impedance threshold ("clips into low-Z loads"), state
  the threshold so the engine skips unaffected pairings.
- only_above_vrms (optional number): when the finding only applies in
  high-gain territory, state the low-gain output voltage — pairings
  that fit under it are unaffected and get no flag.
"""

_OUTPUT_SCHEMA = """
OUTPUT (JSON, strict):
{
  "specs": [
    {"key": "impedance_ohm", "value": "300", "raw_value": "300 Ω", "source_url": "..."},
    ...
  ],
  "new_attributes": [
    {"key": "...", "label": "...", "description": "...", "unit": "...",
     "value_type": "...", "enum_values": [...], "applies_to": [...]},
    ...
  ],
  "technologies": [
    {"key": "sennheiser.ring_radiator", "label": "Ring Radiator",
     "description": "...", "brand": "Sennheiser",
     "patent_or_source": "...", "introduced_year": 2009,
     "applies_to": ["headphones"]},
    ...
  ],
  "research_summary": "3-5 sentences of prose about this model.",
  "measured_caveats": [
    {"role": "hp_out", "severity": "warn", "load_z_below": 50,
     "text": "clips into low-impedance loads at high/super gain",
     "source_url": "..."},
    ...
  ],
  "sentiment": {
    "score": 8.7,
    "sample_size": 412,
    "praise": ["organic timbre", "wide stage", "natural decay"],
    "criticism": ["runs hot", "measurements"]
  }
}
"""


def _format_attribute_catalog(category: str) -> str:
    """Pull the live spec catalog for `category` and format it as a
    human-readable block the model can scan and copy keys from."""
    rows = db_query(
        """
        SELECT key, label, description, unit, value_type::text AS value_type,
               enum_values, applies_to
        FROM gear_spec_attributes
        WHERE %(cat)s = ANY(applies_to)
        ORDER BY label
        """,
        {"cat": category},
    )
    if not rows:
        return "(catalog empty for this category — propose new attributes freely)"
    lines: List[str] = []
    for r in rows:
        unit = f" [{r['unit']}]" if r["unit"] else ""
        enum = ""
        if r["value_type"] == "enum" and r.get("enum_values"):
            enum = " (enum: " + ", ".join(r["enum_values"]) + ")"
        lines.append(f"- {r['key']}{unit} — {r['label']} ({r['value_type']}){enum}")
        lines.append(f"    {r['description']}")
    return "\n".join(lines)


def _format_technology_catalog(category: str) -> str:
    rows = db_query(
        """
        SELECT t.key, t.label, t.description, b.name AS brand
        FROM gear_technologies t
        LEFT JOIN gear_brands b ON b.id = t.brand_id
        WHERE %(cat)s = ANY(t.applies_to)
        ORDER BY t.label
        """,
        {"cat": category},
    )
    if not rows:
        return "(no technologies catalogued for this category yet)"
    lines: List[str] = []
    for r in rows:
        brand = f" [{r['brand']}]" if r["brand"] else ""
        lines.append(f"- {r['key']}{brand} — {r['label']}")
        lines.append(f"    {r['description']}")
    return "\n".join(lines)


def _format_brand_list() -> str:
    rows = db_query("SELECT name FROM gear_brands ORDER BY name LIMIT 200")
    return ", ".join(r["name"] for r in rows) if rows else "(empty)"


def build_prompt(brand: str, model: str, category: str) -> str:
    """Build the full system prompt for a single research call."""
    spec_catalog = _format_attribute_catalog(category)
    tech_catalog = _format_technology_catalog(category)
    brand_list   = _format_brand_list()

    return f"""You are extracting technical specifications, proprietary
technologies and community sentiment for a single audio product:

  brand:    {brand}
  model:    {model}
  category: {category}

The output feeds a P2P-synchronised catalog shared across many user
installations of the Sautium app. To be mergeable across nodes, your
output MUST be canonical and stable for identical inputs.

{_LANG_AND_KEY_RULES}

CANONICAL SPEC ATTRIBUTES (reuse these keys whenever applicable):

{spec_catalog}

CANONICAL TECHNOLOGIES (reuse these keys when applicable):

{tech_catalog}

KNOWN BRANDS (use the exact spelling if the brand is in this list):

{brand_list}

{_REUSE_RULES}

{_OUTPUT_SCHEMA}
"""
