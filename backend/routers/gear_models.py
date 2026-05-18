"""
Canonical gear catalog — search + detail.

Search joins gear_brands to expose `brand` as a string in autocomplete
results. Detail joins gear_brands + gear_specs (with their attribute
metadata) + gear_technologies + gear_sentiment_terms, returning a
shape the UI can render without further roundtrips.

The catalog grows lazily: when a user adds a model not yet known via
POST /api/profile/gear, that endpoint inserts a `queued` row; a
background research worker (Phase 2) picks queued rows up, runs
WebSearch + Claude synthesis and writes back specs / technologies /
sentiment. Until then every newly-seen model stays in `queued` and
the gear sheet renders the "Awaiting research" panel.
"""

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query

from db_pool import db_query, db_query_one


router = APIRouter(prefix="/api/gear-models", tags=["gear_models"])


@router.get("/brands/search")
def search_gear_brands(
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(default=10, ge=1, le=20),
) -> List[Dict[str, Any]]:
    """Autocomplete against gear_brands.name. Lets the add-gear UI
    collapse user-typed brand variants ("ambient", "Ambient Acoustics")
    to a canonical row instead of forking off duplicates."""
    needle = f"%{q.strip().lower()}%"
    return db_query(
        """
        SELECT id::text AS id, name
        FROM gear_brands
        WHERE LOWER(name) LIKE %(q)s
        ORDER BY
            CASE WHEN LOWER(name) LIKE %(prefix)s THEN 0 ELSE 1 END,
            name
        LIMIT %(limit)s
        """,
        {"q": needle, "prefix": f"{q.strip().lower()}%", "limit": limit},
    )


@router.get("/search")
def search_gear_models(
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(default=10, ge=1, le=50),
) -> List[Dict[str, Any]]:
    """Autocomplete against brand + model.

    Case-insensitive LIKE on both fields. Brand-prefix matches rank
    first so 'sennheiser' yields all Sennheiser models even when
    'hd' appears in many other brands' models."""
    needle = f"%{q.strip().lower()}%"
    return db_query(
        """
        SELECT gm.id::text          AS id,
               b.name               AS brand,
               gm.model,
               gm.category,
               gm.research_state::text AS research_state
        FROM gear_models gm
        JOIN gear_brands b ON b.id = gm.brand_id
        WHERE LOWER(b.name) LIKE %(q)s OR LOWER(gm.model) LIKE %(q)s
        ORDER BY
            CASE WHEN LOWER(b.name) LIKE %(q)s THEN 0 ELSE 1 END,
            b.name, gm.model
        LIMIT %(limit)s
        """,
        {"q": needle, "limit": limit},
    )


@router.get("/{model_id}")
def get_gear_model(model_id: str) -> Dict[str, Any]:
    head = db_query_one(
        """
        SELECT gm.id::text                AS id,
               gm.brand_id::text          AS brand_id,
               b.name                     AS brand,
               b.website                  AS brand_website,
               b.country                  AS brand_country,
               b.founded_year             AS brand_founded_year,
               gm.model,
               gm.category,
               gm.research_state::text    AS research_state,
               gm.research_summary,
               gm.researched_at,
               gm.sentiment_score,
               gm.sentiment_sample_size,
               gm.sentiment_updated_at,
               gm.created_at,
               gm.updated_at
        FROM gear_models gm
        JOIN gear_brands b ON b.id = gm.brand_id
        WHERE gm.id = %(id)s::uuid
        """,
        {"id": model_id},
    )
    if not head:
        raise HTTPException(status_code=404, detail="gear model not found")

    # Specs as a list of {key, label, unit, value_type, value} so the
    # UI can render the catalog metadata (label, unit) without a
    # second roundtrip per attribute.
    specs = db_query(
        """
        SELECT a.key, a.label, a.unit, a.value_type::text AS value_type,
               gs.value_text AS value, gs.source_url
        FROM gear_specs gs
        JOIN gear_spec_attributes a ON a.id = gs.attribute_id
        WHERE gs.gear_model_id = %(id)s::uuid
        ORDER BY a.label
        """,
        {"id": model_id},
    )
    head["specs"] = specs

    technologies = db_query(
        """
        SELECT t.id::text AS id, t.key, t.label, t.description,
               t.patent_or_source, t.introduced_year
        FROM gear_model_technologies mt
        JOIN gear_technologies t ON t.id = mt.technology_id
        WHERE mt.gear_model_id = %(id)s::uuid
        ORDER BY t.label
        """,
        {"id": model_id},
    )
    head["technologies"] = technologies

    sentiment_rows = db_query(
        """
        SELECT polarity, term, weight
        FROM gear_sentiment_terms
        WHERE gear_model_id = %(id)s::uuid
        ORDER BY polarity, COALESCE(weight, 0) DESC, term
        """,
        {"id": model_id},
    )
    praise = [r["term"] for r in sentiment_rows if r["polarity"] == "praise"]
    crit   = [r["term"] for r in sentiment_rows if r["polarity"] == "criticism"]
    head["sentiment"] = {
        "score":        head.pop("sentiment_score", None),
        "sample_size":  head.pop("sentiment_sample_size", None),
        "updated_at":   head.pop("sentiment_updated_at", None),
        "praise":       praise,
        "criticism":    crit,
    }

    return head
