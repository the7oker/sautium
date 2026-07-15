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

import asyncio
import logging
import select
import threading
from typing import Any, Dict, List, Optional

import psycopg2
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from config import settings
from db_pool import db_execute, db_query, db_query_one

logger = logging.getLogger(__name__)

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

    head["measured_caveats"] = db_query(
        """
        SELECT role, severity::text AS severity, load_z_below, only_above_vrms,
               text, source_url
        FROM gear_measured_caveats
        WHERE gear_model_id = %(id)s::uuid
        ORDER BY severity DESC, text
        """,
        {"id": model_id},
    )

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


@router.post("/{model_id}/retry-research")
def retry_research(model_id: str) -> Dict[str, Any]:
    """Deliberate user action — research burns real tokens, so gear never
    re-researches on its own. Re-queues a failed retry or a cached refresh;
    a row already queued or researching is left alone."""
    row = db_execute(
        """
        UPDATE gear_models SET research_state = 'queued'
        WHERE id = %(id)s::uuid AND research_state IN ('failed', 'cached')
        RETURNING id::text AS id
        """,
        {"id": model_id},
    )
    if row is None:
        raise HTTPException(status_code=409, detail="model is already queued or researching")
    db_execute("SELECT pg_notify('gear_research', %(id)s)", {"id": model_id})
    from gear_research_worker import request_drain
    request_drain()
    return {"ok": True}


# ── research-state SSE (worker NOTIFY → UI chips) ──────────────────────

_state_sse_clients: list = []  # (asyncio.Event, loop)
_state_sse_lock = threading.Lock()
_state_listener_thread: Optional[threading.Thread] = None
_state_listener_running = False


def _wake_state_clients():
    with _state_sse_lock:
        for evt, loop in _state_sse_clients:
            loop.call_soon_threadsafe(evt.set)


def _state_db_listener():
    while _state_listener_running:
        conn = None
        try:
            conn = psycopg2.connect(settings.database_url)
            conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
            with conn.cursor() as cur:
                cur.execute("LISTEN sautium_gear_state")
            while _state_listener_running:
                ready = select.select([conn], [], [], 1)
                if ready[0]:
                    conn.poll()
                    if conn.notifies:
                        conn.notifies.clear()
                        _wake_state_clients()
        except Exception as e:
            logger.debug(f"gear state listener error: {e}")
            if _state_listener_running:
                import time
                time.sleep(1)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass


def start_gear_state_listener():
    global _state_listener_thread, _state_listener_running
    if _state_listener_thread and _state_listener_thread.is_alive():
        return
    _state_listener_running = True
    _state_listener_thread = threading.Thread(
        target=_state_db_listener, daemon=True, name="gear-state-sse"
    )
    _state_listener_thread.start()


def stop_gear_state_listener():
    global _state_listener_running
    _state_listener_running = False
    if _state_listener_thread:
        _state_listener_thread.join(timeout=3)


@router.get("/research/stream")
async def research_state_stream():
    """One event per research-state transition; payload-free — the
    client refetches whatever screen it is on."""
    evt = asyncio.Event()
    loop = asyncio.get_running_loop()
    with _state_sse_lock:
        _state_sse_clients.append((evt, loop))

    async def gen():
        try:
            while True:
                try:
                    await asyncio.wait_for(evt.wait(), timeout=15)
                    evt.clear()
                    yield "event: changed\ndata: {}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            with _state_sse_lock:
                try:
                    _state_sse_clients.remove((evt, loop))
                except ValueError:
                    pass

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})
