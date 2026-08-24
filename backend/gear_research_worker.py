"""
Gear research worker — Phase 2 of the gear catalog.

Picks up gear_models rows in research_state='queued', runs one AI
research call per model (Claude Code CLI with built-in WebSearch /
WebFetch preferred — subscription-billed, same runner the AI assistant and
ai_canon use; Anthropic API with the server-side web_search tool as
fallback), parses the strict-JSON payload defined by
gear_research_prompt.build_prompt() and persists it:

  - gear_specs               (+ AI-proposed gear_spec_attributes, seeded=FALSE)
  - gear_technologies        (+ gear_model_technologies links)
  - gear_sentiment_terms     (+ aggregate columns on gear_models)
  - research_summary / researched_at / research_state='cached'

Event-driven per project rules: routers/profile.py NOTIFYs
'gear_research' after inserting a queued row; the worker holds a
LISTEN connection (same pattern as the chat SSE listener) and drains
the whole queue on every wake-up. On startup it re-queues rows left
in 'researching' by a dead process, then drains any backlog — safe
because every write below is upsert-shaped (idempotent enrichment).

'failed' rows stay failed: research costs real tokens, so retries are
a deliberate user action (re-add the model or a future Retry button),
never an automatic loop.

CLI (inside the backend container):
    python gear_research_worker.py --list
    python gear_research_worker.py --one <gear_model_id>
"""

import json
import logging
import re
import select
import threading
import time
from typing import Any, Dict, List, Optional

import psycopg2

from config import settings
from db_pool import db_execute, db_query, db_query_one
from uuid_utils import (gear_caveat_uuid, gear_pair_uuid,
                        gear_spec_attribute_uuid, gear_technology_uuid)

logger = logging.getLogger(__name__)

# In-process drain signal. One long-lived consumer thread owns the drain;
# every producer just sets this Event. This deliberately replaces the old
# "kick a daemon thread that try-acquires a lock" scheme: try-acquire
# returned False mid-drain and dropped the wake-up on the floor, and the
# fallback leaned on in-process Postgres NOTIFY, which does NOT reliably
# reach this process's own LISTEN socket under Docker/WSL — together they
# stranded freshly-added models in 'queued' forever (observed live). With an
# Event the consumer clears-then-drains, so a set() that races an in-flight
# drain is never lost: the next wait() returns immediately and re-drains.
# NOTIFY + the LISTEN thread remain purely the cross-process / P2P wake path,
# folded into the same Event.
_drain_wake = threading.Event()


def request_drain() -> None:
    """Signal the consumer that the queue has work (any thread, non-blocking).

    Producers MUST commit their INSERT/UPDATE before calling this: the
    consumer clears the Event before it drains, so the row has to be visible
    by the time the woken drain runs its claim SELECT."""
    _drain_wake.set()


RESEARCH_TIMEOUT_SECONDS = 600   # one model = many web fetches; chat's 150s is far too small.
                                 # 480 proved too tight for review-rich categories (Empyrean II
                                 # timed out on the first live drain) — headphones carry the
                                 # largest source corpus, so they set the budget.
RESEARCH_MODEL = "sonnet"        # Claude Code alias; measured-cheap and good enough for extraction
ANTHROPIC_RESEARCH_MODEL = "claude-sonnet-5"
ANTHROPIC_MAX_TURNS = 8          # pause_turn continuations for server-side web_search
PAUSE_BETWEEN_MODELS = 3         # seconds; politeness between research calls

_VALID_VALUE_TYPES = {"number", "string", "enum", "boolean"}

# CLI-level failures (usage limit exhausted, OAuth expiry, transport errors)
# are infrastructure conditions, not verdicts about the model being
# researched: the row goes back to 'queued' and the drain pauses instead of
# burning the whole queue into 'failed' (observed live: an exhausted
# subscription window failed 10 models in under a minute).
_CLI_INFRA_RE = re.compile(
    r"^Claude Code (error|OAuth token expired)|limit reached|usage limit|rate limit",
    re.IGNORECASE,
)
INFRA_PAUSE_SECONDS = 15 * 60


class ResearchInfraPause(Exception):
    """AI backend temporarily unavailable — requeue and pause the drain."""


_consumer_thread: Optional[threading.Thread] = None
_listener_thread: Optional[threading.Thread] = None
_worker_running = False


# ─── AI call ────────────────────────────────────────────────────────────────

def _user_message(brand: str, model: str, category: str) -> str:
    return (
        f"Research this audio product now: {brand} {model} (category: {category}). "
        "Use web search to verify every value against real sources. "
        "RESEARCH BUDGET: at most ~8 web searches and ~10 page fetches total — "
        "take the first authoritative source per fact (manufacturer page beats "
        "aggregators), do not cross-check values that already have a solid source, "
        "and when the budget is spent STOP researching and emit the JSON with "
        "what you have. An incomplete-but-sourced payload beats a timeout. "
        "Return ONLY the JSON object described in the output schema — no prose "
        "before or after it."
    )


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        payload = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _call_claude_code_research(system_prompt: str, user_msg: str) -> Optional[Dict[str, Any]]:
    from claude_code_runner import call_claude_code
    res = call_claude_code(
        message=user_msg,
        system_prompt=system_prompt,
        model=RESEARCH_MODEL,
        timeout_seconds=RESEARCH_TIMEOUT_SECONDS,
        mcp=False,
        max_turns=25,
    )
    answer = res.get("answer") or ""
    payload = _extract_json(answer)
    if payload is None:
        # Raised before the Anthropic fallback on purpose: when the
        # subscription window is exhausted the pay-as-you-go key is the
        # wrong tool to silently burn instead.
        if _CLI_INFRA_RE.search(answer):
            raise ResearchInfraPause(answer[:120])
        logger.warning(f"gear research: Claude Code returned no JSON; head: {answer[:300]!r}")
    return payload


def _call_anthropic_research(system_prompt: str, user_msg: str) -> Optional[Dict[str, Any]]:
    if not settings.anthropic_api_key:
        return None
    import anthropic
    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key, timeout=RESEARCH_TIMEOUT_SECONDS
    )
    messages: List[Dict[str, Any]] = [{"role": "user", "content": user_msg}]
    response = None
    for _ in range(ANTHROPIC_MAX_TURNS):
        response = client.messages.create(
            model=ANTHROPIC_RESEARCH_MODEL,
            max_tokens=8000,
            system=system_prompt,
            messages=messages,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 12}],
        )
        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue
        break
    if response is None:
        return None
    text = "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    )
    return _extract_json(text)


def _run_research_call(brand: str, model: str, category: str) -> Optional[Dict[str, Any]]:
    from gear_research_prompt import build_prompt
    system_prompt = build_prompt(brand, model, category)
    user_msg = _user_message(brand, model, category)

    payload = _call_claude_code_research(system_prompt, user_msg)
    if payload is not None:
        return payload
    logger.info("gear research: Claude Code path yielded no JSON, trying Anthropic API")
    return _call_anthropic_research(system_prompt, user_msg)


# ─── persistence ────────────────────────────────────────────────────────────

def _resolve_attribute(key: str, category: str,
                       proposed: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """Attribute id for `key`, creating an AI-proposed one (seeded=FALSE)
    when the payload carries its definition. Unknown key with no
    definition is skipped — the prompt demands reuse-or-propose, and
    silently minting half-defined attributes would rot the catalog."""
    row = db_query_one(
        "SELECT id::text AS id FROM gear_spec_attributes WHERE key = %(k)s", {"k": key}
    )
    if row:
        return row["id"]

    spec = proposed.get(key)
    if not spec:
        logger.warning(f"gear research: spec key {key!r} neither in catalog nor proposed — skipped")
        return None

    value_type = spec.get("value_type")
    if value_type not in _VALID_VALUE_TYPES:
        logger.warning(f"gear research: attribute {key!r} has invalid value_type {value_type!r} — skipped")
        return None
    applies_to = spec.get("applies_to") or [category]

    attr_id = str(gear_spec_attribute_uuid(key))
    db_execute(
        """
        INSERT INTO gear_spec_attributes
            (id, key, label, description, unit, value_type, enum_values, applies_to, seeded)
        VALUES (%(id)s::uuid, %(key)s, %(label)s, %(descr)s, %(unit)s,
                %(vtype)s::spec_value_type, %(enum_values)s, %(applies)s, FALSE)
        ON CONFLICT (key) DO NOTHING
        """,
        {
            "id": attr_id,
            "key": key,
            "label": spec.get("label") or key,
            "descr": spec.get("description") or "",
            "unit": spec.get("unit"),
            "vtype": value_type,
            "enum_values": spec.get("enum_values"),
            "applies": applies_to,
        },
    )
    return attr_id


def _persist_specs(model_id: str, category: str, payload: Dict[str, Any]) -> int:
    proposed = {
        a.get("key"): a for a in payload.get("new_attributes") or [] if a.get("key")
    }
    written = 0
    for spec in payload.get("specs") or []:
        key, value = spec.get("key"), spec.get("value")
        if not key or value is None:
            continue
        source_url = spec.get("source_url")
        if not source_url:
            logger.warning(f"gear research: spec {key!r} has no source_url — skipped (unverifiable)")
            continue
        attr_id = _resolve_attribute(key, category, proposed)
        if not attr_id:
            continue
        db_execute(
            """
            INSERT INTO gear_specs (gear_model_id, attribute_id, value_text, raw_value, source_url)
            VALUES (%(m)s::uuid, %(a)s::uuid, %(v)s, %(raw)s, %(src)s)
            ON CONFLICT (gear_model_id, attribute_id)
            DO UPDATE SET value_text = EXCLUDED.value_text,
                          raw_value  = EXCLUDED.raw_value,
                          source_url = EXCLUDED.source_url
            """,
            {"m": model_id, "a": attr_id, "v": str(value),
             "raw": spec.get("raw_value"), "src": source_url},
        )
        written += 1
    return written


def _persist_technologies(model_id: str, payload: Dict[str, Any]) -> int:
    written = 0
    for tech in payload.get("technologies") or []:
        key = tech.get("key")
        if not key:
            continue
        row = db_query_one(
            "SELECT id::text AS id FROM gear_technologies WHERE key = %(k)s", {"k": key}
        )
        if row:
            tech_id = row["id"]
        else:
            brand_row = db_query_one(
                "SELECT id::text AS id FROM gear_brands WHERE LOWER(name) = LOWER(%(b)s)",
                {"b": tech.get("brand") or ""},
            )
            tech_id = str(gear_technology_uuid(key))
            db_execute(
                """
                INSERT INTO gear_technologies
                    (id, key, label, description, brand_id, patent_or_source,
                     introduced_year, applies_to, seeded)
                VALUES (%(id)s::uuid, %(key)s, %(label)s, %(descr)s, %(brand)s::uuid,
                        %(patent)s, %(year)s, %(applies)s, FALSE)
                ON CONFLICT (key) DO NOTHING
                """,
                {
                    "id": tech_id,
                    "key": key,
                    "label": tech.get("label") or key,
                    "descr": tech.get("description") or "",
                    "brand": brand_row["id"] if brand_row else None,
                    "patent": tech.get("patent_or_source"),
                    "year": tech.get("introduced_year"),
                    "applies": tech.get("applies_to") or [],
                },
            )
        db_execute(
            """
            INSERT INTO gear_model_technologies (gear_model_id, technology_id)
            VALUES (%(m)s::uuid, %(t)s::uuid)
            ON CONFLICT DO NOTHING
            """,
            {"m": model_id, "t": tech_id},
        )
        written += 1
    return written


_VALID_CAVEAT_ROLES = {"hp_out", "line_out", "transducer"}


def _persist_caveats(model_id: str, payload: Dict[str, Any]) -> int:
    db_execute(
        "DELETE FROM gear_measured_caveats WHERE gear_model_id = %(m)s::uuid",
        {"m": model_id},
    )
    written = 0
    for cv in payload.get("measured_caveats") or []:
        text = (cv.get("text") or "").strip()
        source_url = cv.get("source_url")
        if not text or not source_url:
            continue  # measurement-tier claims are citation-mandatory
        role = cv.get("role")
        if role not in _VALID_CAVEAT_ROLES:
            role = None
        severity = cv.get("severity") if cv.get("severity") in ("info", "warn") else "warn"
        def _f(key):
            try:
                return float(cv[key]) if cv.get(key) is not None else None
            except (TypeError, ValueError):
                return None
        db_execute(
            """
            INSERT INTO gear_measured_caveats
                (id, gear_model_id, role, severity, load_z_below, only_above_vrms, text, source_url)
            VALUES (%(id)s::uuid, %(m)s::uuid, %(role)s, %(sev)s::gear_caveat_severity,
                    %(lz)s, %(oav)s, %(t)s, %(src)s)
            ON CONFLICT (gear_model_id, text) DO UPDATE
            SET role = EXCLUDED.role, severity = EXCLUDED.severity,
                load_z_below = EXCLUDED.load_z_below,
                only_above_vrms = EXCLUDED.only_above_vrms,
                source_url = EXCLUDED.source_url
            """,
            {"id": str(gear_caveat_uuid(model_id, text)), "m": model_id,
             "role": role, "sev": severity, "lz": _f("load_z_below"),
             "oav": _f("only_above_vrms"), "t": text, "src": source_url},
        )
        written += 1
    return written


def _persist_sentiment(model_id: str, payload: Dict[str, Any]) -> None:
    sentiment = payload.get("sentiment") or {}
    db_execute(
        "DELETE FROM gear_sentiment_terms WHERE gear_model_id = %(m)s::uuid",
        {"m": model_id},
    )
    for polarity in ("praise", "criticism"):
        for term in sentiment.get(polarity) or []:
            term = str(term).strip()[:80]
            if not term:
                continue
            db_execute(
                """
                INSERT INTO gear_sentiment_terms (gear_model_id, polarity, term)
                VALUES (%(m)s::uuid, %(p)s::gear_polarity, %(t)s)
                ON CONFLICT DO NOTHING
                """,
                {"m": model_id, "p": polarity, "t": term},
            )
    db_execute(
        """
        UPDATE gear_models
        SET sentiment_score       = %(score)s,
            sentiment_sample_size = %(n)s,
            sentiment_updated_at  = now()
        WHERE id = %(m)s::uuid
        """,
        {"m": model_id, "score": sentiment.get("score"), "n": sentiment.get("sample_size")},
    )


def research_one(model_id: str) -> bool:
    """Full research cycle for one gear model. Returns True on success.
    Caller must have already claimed the row (research_state='researching')."""
    head = db_query_one(
        """
        SELECT gm.model, gm.category::text AS category, b.name AS brand
        FROM gear_models gm JOIN gear_brands b ON b.id = gm.brand_id
        WHERE gm.id = %(id)s::uuid
        """,
        {"id": model_id},
    )
    if not head:
        logger.error(f"gear research: model {model_id} vanished")
        return False

    brand, model, category = head["brand"], head["model"], head["category"]
    logger.info(f"gear research: start {brand} {model} ({category})")
    started = time.time()

    try:
        payload = _run_research_call(brand, model, category)
    except ResearchInfraPause:
        raise
    except Exception as e:
        logger.error(f"gear research: call failed for {brand} {model}: {e}")
        payload = None

    if not payload:
        db_execute(
            "UPDATE gear_models SET research_state = 'failed' WHERE id = %(m)s::uuid",
            {"m": model_id},
        )
        _notify_state(model_id, "failed")
        logger.error(f"gear research: FAILED {brand} {model} (no parseable payload)")
        return False

    n_specs = _persist_specs(model_id, category, payload)
    n_techs = _persist_technologies(model_id, payload)
    n_caveats = _persist_caveats(model_id, payload)
    _persist_sentiment(model_id, payload)
    db_execute(
        """
        UPDATE gear_models
        SET research_state   = 'cached',
            research_summary = %(summary)s,
            researched_at    = now()
        WHERE id = %(m)s::uuid
        """,
        {"m": model_id, "summary": (payload.get("research_summary") or "").strip() or None},
    )
    _notify_state(model_id, "cached")
    logger.info(
        f"gear research: done {brand} {model} — {n_specs} specs, {n_techs} technologies, "
        f"{n_caveats} caveats, {time.time() - started:.0f}s"
    )
    return True


# ─── pair-synergy research ──────────────────────────────────────────────────

def enqueue_pair(model_a: str, model_b: str) -> None:
    """Queue a pair-synergy research job (idempotent) and wake the worker."""
    a, b = sorted((model_a, model_b))
    db_execute(
        """
        INSERT INTO gear_pair_notes (id, model_a, model_b)
        VALUES (%(id)s::uuid, %(a)s::uuid, %(b)s::uuid)
        ON CONFLICT (model_a, model_b) DO NOTHING
        """,
        {"id": str(gear_pair_uuid(a, b)), "a": a, "b": b},
    )
    db_execute("SELECT pg_notify('gear_research', %(p)s)", {"p": f"pair:{a}:{b}"})
    request_drain()


def _pair_names(pair_id: str) -> Optional[Dict[str, str]]:
    return db_query_one(
        """
        SELECT pn.id::text AS id,
               ba.name || ' ' || ga.model AS name_a,
               bb.name || ' ' || gb.model AS name_b
        FROM gear_pair_notes pn
        JOIN gear_models ga ON ga.id = pn.model_a
        JOIN gear_brands ba ON ba.id = ga.brand_id
        JOIN gear_models gb ON gb.id = pn.model_b
        JOIN gear_brands bb ON bb.id = gb.brand_id
        WHERE pn.id = %(id)s::uuid
        """,
        {"id": pair_id},
    )


def research_pair(pair_id: str) -> bool:
    from gear_research_prompt import build_pair_prompt
    head = _pair_names(pair_id)
    if not head:
        return False
    logger.info(f"gear research: pair start {head['name_a']} + {head['name_b']}")
    started = time.time()
    system_prompt = build_pair_prompt(head["name_a"], head["name_b"])
    user_msg = (
        f"Research this pairing now: {head['name_a']} + {head['name_b']}. "
        "RESEARCH BUDGET: at most ~6 web searches / ~8 page fetches; when spent, "
        "emit the JSON with what you have (discussed:false is a valid answer). "
        "Return ONLY the JSON object."
    )
    try:
        payload = _call_claude_code_research(system_prompt, user_msg) \
            or _call_anthropic_research(system_prompt, user_msg)
    except ResearchInfraPause:
        raise
    except Exception as e:
        logger.error(f"gear research: pair call failed: {e}")
        payload = None

    if payload is None:
        db_execute(
            "UPDATE gear_pair_notes SET research_state = 'failed' WHERE id = %(id)s::uuid",
            {"id": pair_id},
        )
        logger.error(f"gear research: pair FAILED {head['name_a']} + {head['name_b']}")
        return False

    discussed = bool(payload.get("discussed"))
    db_execute(
        """
        UPDATE gear_pair_notes
        SET research_state = 'cached',
            summary     = %(summary)s,
            terms       = %(terms)s,
            sample_size = %(n)s,
            source_urls = %(srcs)s,
            researched_at = now()
        WHERE id = %(id)s::uuid
        """,
        {
            "id": pair_id,
            "summary": (payload.get("summary") or "").strip() or None if discussed else None,
            "terms": [str(t)[:80] for t in (payload.get("terms") or [])][:6] if discussed else None,
            "n": payload.get("sample_size") if discussed else None,
            "srcs": (payload.get("source_urls") or [])[:6] if discussed else None,
        },
    )
    db_execute("SELECT pg_notify('sautium_gear_state', %(p)s)", {"p": f"pair:{pair_id}:cached"})
    logger.info(
        f"gear research: pair done {head['name_a']} + {head['name_b']} — "
        f"{'discussed' if discussed else 'not discussed'}, {time.time() - started:.0f}s"
    )
    return True


def _claim_next_pair() -> Optional[str]:
    row = db_execute(
        """
        UPDATE gear_pair_notes
        SET research_state = 'researching'
        WHERE id = (
            SELECT id FROM gear_pair_notes
            WHERE research_state = 'queued'
            ORDER BY created_at
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id::text AS id
        """
    )
    return row["id"] if row else None


# ─── queue ──────────────────────────────────────────────────────────────────

def _notify_state(model_id: str, state: str) -> None:
    """Wake UI chips over the gear-state SSE bridge."""
    db_execute("SELECT pg_notify('sautium_gear_state', %(p)s)",
               {"p": f"{model_id}:{state}"})


def _claim_next() -> Optional[str]:
    row = db_execute(
        """
        UPDATE gear_models
        SET research_state = 'researching'
        WHERE id = (
            SELECT id FROM gear_models
            WHERE research_state = 'queued'
            ORDER BY created_at
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id::text AS id
        """
    )
    if row:
        _notify_state(row["id"], "researching")
    return row["id"] if row else None


def _drain_queue() -> None:
    # Models first (they feed specs/sentiment), then pair-synergy jobs.
    while _worker_running:
        model_id = _claim_next()
        pair_id = None
        if not model_id:
            pair_id = _claim_next_pair()
            if not pair_id:
                return
        try:
            if model_id:
                research_one(model_id)
            else:
                research_pair(pair_id)
        except ResearchInfraPause as e:
            if model_id:
                db_execute(
                    "UPDATE gear_models SET research_state = 'queued' WHERE id = %(m)s::uuid",
                    {"m": model_id},
                )
            else:
                db_execute(
                    "UPDATE gear_pair_notes SET research_state = 'queued' WHERE id = %(m)s::uuid",
                    {"m": pair_id},
                )
            logger.warning(
                f"gear research: AI backend unavailable ({e}); pausing drain "
                f"for {INFRA_PAUSE_SECONDS // 60} min"
            )
            slept = 0
            while _worker_running and slept < INFRA_PAUSE_SECONDS:
                time.sleep(30)
                slept += 30
            continue
        time.sleep(PAUSE_BETWEEN_MODELS)


def _requeue_orphans() -> None:
    """Rows stuck in 'researching' mean a previous process died mid-call —
    the claim is process-local, so on startup they go back to the queue."""
    n = db_query(
        """
        UPDATE gear_models SET research_state = 'queued'
        WHERE research_state = 'researching'
        RETURNING id
        """
    )
    n2 = db_query(
        """
        UPDATE gear_pair_notes SET research_state = 'queued'
        WHERE research_state = 'researching'
        RETURNING id
        """
    )
    if n or n2:
        logger.info(f"gear research: re-queued {len(n)} model + {len(n2)} pair orphan(s)")


def _consumer_loop() -> None:
    """Own the queue drain. Block on the wake Event until a producer (or the
    LISTEN thread) signals work, then drain to empty. clear() precedes the
    drain so a set() racing an in-flight drain survives for the next pass —
    the row is already committed, so the worst case is one harmless empty
    re-drain, never a lost wake-up."""
    _requeue_orphans()
    _drain_wake.set()  # sweep anything already queued at startup
    while _worker_running:
        _drain_wake.wait()
        if not _worker_running:
            break
        _drain_wake.clear()
        try:
            _drain_queue()
        except Exception:
            logger.exception("gear research: drain crashed; will re-drain on next wake")


def _listener_loop() -> None:
    """Bridge cross-process / P2P NOTIFY into the wake Event. In-process
    producers set the Event directly via request_drain(); this thread exists
    so a NOTIFY fired from another process still wakes the consumer.

    Aggressive keepalives so a silently-dropped LISTEN socket (an idle TCP
    conn reaped by Docker/WSL NAT with no RST) surfaces as a poll error and
    forces a reconnect. Without them the thread keeps select()-ing a dead
    socket, no exception fires, and every later NOTIFY is lost."""
    while _worker_running:
        conn = None
        try:
            conn = psycopg2.connect(
                settings.database_url,
                keepalives=1, keepalives_idle=30,
                keepalives_interval=10, keepalives_count=3,
            )
            conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
            with conn.cursor() as cur:
                cur.execute("LISTEN gear_research")
            # Re-sweep on every (re)connect: a NOTIFY only reaches a live
            # listener, so anything queued while the socket was down would
            # otherwise wait for the next producer.
            _drain_wake.set()
            while _worker_running:
                ready = select.select([conn], [], [], 5)
                if ready[0]:
                    conn.poll()
                    if conn.notifies:
                        conn.notifies.clear()
                        _drain_wake.set()
        except Exception as e:
            logger.warning(f"gear research listener error: {e}")
            if _worker_running:
                time.sleep(2)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass


def start_gear_research_worker() -> None:
    global _consumer_thread, _listener_thread, _worker_running
    if _consumer_thread and _consumer_thread.is_alive():
        return
    _worker_running = True
    _consumer_thread = threading.Thread(
        target=_consumer_loop, daemon=True, name="gear-research-worker"
    )
    _consumer_thread.start()
    _listener_thread = threading.Thread(
        target=_listener_loop, daemon=True, name="gear-research-listener"
    )
    _listener_thread.start()
    logger.info("gear research worker started")


def stop_gear_research_worker() -> None:
    global _worker_running
    _worker_running = False
    _drain_wake.set()  # unblock the consumer's wait() so it can observe the flag
    if _consumer_thread:
        _consumer_thread.join(timeout=3)
    if _listener_thread:
        _listener_thread.join(timeout=3)


# ─── CLI ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Gear research worker CLI")
    parser.add_argument("--list", action="store_true", help="show queue state")
    parser.add_argument("--one", metavar="MODEL_ID", help="research a single gear_models id")
    args = parser.parse_args()

    if args.list:
        for r in db_query(
            """
            SELECT gm.id::text AS id, b.name AS brand, gm.model,
                   gm.category::text AS category, gm.research_state::text AS state
            FROM gear_models gm JOIN gear_brands b ON b.id = gm.brand_id
            ORDER BY gm.research_state, b.name, gm.model
            """
        ):
            print(f"{r['state']:12s} {r['id']}  {r['brand']} {r['model']} ({r['category']})")
    elif args.one:
        _worker_running = True
        db_execute(
            "UPDATE gear_models SET research_state = 'researching' WHERE id = %(m)s::uuid",
            {"m": args.one},
        )
        ok = research_one(args.one)
        raise SystemExit(0 if ok else 1)
    else:
        parser.print_help()
