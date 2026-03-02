"""
AI DJ chat with persistent history and feedback.

Sessions, messages stored in PostgreSQL. Feedback endpoint for debugging
recommendation quality. Supports multiple LLM providers.
"""

import json
import logging
from typing import Optional

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

# ---------------------------------------------------------------------------
# DB helpers (reuse pattern from player.py)
# ---------------------------------------------------------------------------

_db_conn: Optional[psycopg2.extensions.connection] = None


def _get_db() -> psycopg2.extensions.connection:
    global _db_conn
    if _db_conn is None or _db_conn.closed:
        _db_conn = psycopg2.connect(settings.database_url)
        _db_conn.autocommit = True
    return _db_conn


def _db_query(sql: str, params=None) -> list[dict]:
    conn = _get_db()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def _db_query_one(sql: str, params=None) -> Optional[dict]:
    rows = _db_query(sql, params)
    return rows[0] if rows else None


def _db_execute(sql: str, params=None) -> Optional[dict]:
    """Execute INSERT/UPDATE and return first row if RETURNING is used."""
    conn = _get_db()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        if cur.description:
            row = cur.fetchone()
            return dict(row) if row else None
        return None


# ---------------------------------------------------------------------------
# Player context (for AI DJ awareness of what's playing)
# ---------------------------------------------------------------------------

def _get_player_context() -> Optional[str]:
    """Get current HQPlayer state by calling player router functions directly."""
    try:
        from routers.player import get_status, get_playlist

        status = get_status()

        if status.get("state") == "disconnected":
            return None

        parts = []

        # Now playing
        state = status.get("state", "unknown")
        song = status.get("song")
        artist = status.get("artist")
        album = status.get("album")
        genre = status.get("genre")

        if song:
            np = f"Now playing ({state}): \"{song}\" by {artist or 'Unknown'}"
            if album:
                np += f" | Album: {album}"
            if genre:
                np += f" | Genre: {genre}"
            pos = status.get("position_formatted", "")
            length = status.get("length_formatted", "")
            if pos and length:
                np += f" | Position: {pos}/{length}"
            parts.append(np)
        else:
            parts.append(f"Player state: {state} (no track loaded)")

        # Playlist
        try:
            pl = get_playlist()
            pl_tracks = pl.get("tracks", [])
            if pl_tracks:
                current_idx = status.get("track_index")
                playlist_lines = [f"Playlist ({len(pl_tracks)} tracks):"]
                for t in pl_tracks:
                    marker = " >>> " if t.get("index") == (current_idx - 1 if current_idx else -1) else "     "
                    playlist_lines.append(f"{marker}{t.get('artist', '?')} - {t.get('title', '?')}")
                parts.append("\n".join(playlist_lines))
        except Exception:
            pass

        return "\n".join(parts)

    except Exception as e:
        logger.debug(f"Failed to get player context: {e}")
        return None


# ---------------------------------------------------------------------------
# Claude Code DJ integration
# ---------------------------------------------------------------------------

def _get_claude_session_id(session_id: int) -> Optional[str]:
    """Get Claude Code session ID mapped to our chat session."""
    row = _db_query_one(
        "SELECT claude_session_id FROM chat_sessions WHERE id = %(id)s",
        {"id": session_id},
    )
    return row["claude_session_id"] if row and row.get("claude_session_id") else None


def _save_claude_session_id(session_id: int, claude_sid: str):
    """Save Claude Code session ID for continuity."""
    _db_execute(
        "UPDATE chat_sessions SET claude_session_id = %(csid)s WHERE id = %(id)s",
        {"csid": claude_sid, "id": session_id},
    )


def _call_claude_code_dj(session_id: int, message: str, player_context: Optional[str], model: Optional[str] = None) -> dict:
    """Call Claude Code as AI DJ backend."""
    from claude_code_runner import call_claude_code
    from claude_dj_prompt import get_system_prompt

    claude_sid = _get_claude_session_id(session_id)
    prompt = get_system_prompt("claude_code", player_context)

    result = call_claude_code(
        message=message,
        system_prompt=prompt,
        session_id=claude_sid,
        resume=bool(claude_sid),
        model=model,
    )

    # Save Claude session ID for future messages
    if result.get("claude_session_id"):
        _save_claude_session_id(session_id, result["claude_session_id"])

    return {
        "answer": result.get("answer", ""),
        "tracks": result.get("tracks", []),
        "model": result.get("model", "claude-code"),
        "provider": "claude_code",
        "filters_detected": {},
        "retrieval_log": [],
        "tracks_retrieved": len(result.get("tracks", [])),
    }


# ---------------------------------------------------------------------------
# API provider DJ integration
# ---------------------------------------------------------------------------

def _call_api_provider_dj(
    provider_name: str,
    session_id: int,
    message: str,
    player_context: Optional[str],
    model: Optional[str] = None,
    history: Optional[list[dict]] = None,
) -> dict:
    """Call an API-based LLM provider as AI DJ backend."""
    from providers import get_provider
    from providers.base import ProviderMessage
    from claude_dj_prompt import get_system_prompt

    provider = get_provider(provider_name)
    if provider is None:
        raise ValueError(f"Provider '{provider_name}' is not available")

    prompt = get_system_prompt(provider_name, player_context)

    # Convert history dicts to ProviderMessage objects
    pm_history = None
    if history:
        pm_history = [ProviderMessage(role=h["role"], content=h["content"]) for h in history]

    result = provider.chat(
        message=message,
        history=pm_history,
        system_prompt=prompt,
        player_context=player_context,
        model=model,
    )

    return {
        "answer": result.answer,
        "tracks": result.tracks,
        "model": result.model,
        "provider": result.provider,
        "filters_detected": {},
        "retrieval_log": [{"source": "tools", "description": f"{result.tool_calls_count} tool calls"}] if result.tool_calls_count else [],
        "tracks_retrieved": len(result.tracks),
    }


# ---------------------------------------------------------------------------
# Track ID post-validation
# ---------------------------------------------------------------------------

def _validate_tracks(tracks: list[dict]) -> list[dict]:
    """Validate track IDs against the database.

    LLMs (especially smaller ones like Llama) may hallucinate track IDs.
    This function checks each ID exists and replaces title/artist/album
    with real data from the DB. Invalid IDs are removed.
    """
    if not tracks:
        return tracks

    ids = [t.get("id") for t in tracks if t.get("id") is not None]
    if not ids:
        return tracks

    try:
        rows = _db_query("""
            SELECT mf.id, t.title, a.name as artist, al.title as album
            FROM media_files mf
            JOIN tracks t ON mf.track_id = t.id
            JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
            JOIN artists a ON ta.artist_id = a.id
            JOIN album_variants av ON mf.album_variant_id = av.id
            JOIN albums al ON av.album_id = al.id
            WHERE mf.id = ANY(%(ids)s)
        """, {"ids": ids})

        db_map = {r["id"]: r for r in rows}

        validated = []
        for t in tracks:
            tid = t.get("id")
            if tid in db_map:
                real = db_map[tid]
                validated.append({
                    "id": tid,
                    "title": real["title"],
                    "artist": real["artist"],
                    "album": real["album"],
                    "similarity": t.get("similarity"),
                })
            else:
                logger.warning(f"Track ID {tid} not found in DB — removed from results")

        if len(validated) < len(tracks):
            logger.info(f"Track validation: {len(tracks)} → {len(validated)} (removed {len(tracks) - len(validated)} invalid)")

        return validated
    except Exception as e:
        logger.error(f"Track validation failed: {e}")
        return tracks  # return original on error


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class CreateSessionRequest(BaseModel):
    title: Optional[str] = None


class ChatMessageRequest(BaseModel):
    message: str
    model: Optional[str] = None
    provider: Optional[str] = None


class FeedbackRequest(BaseModel):
    is_not_relevant: bool = True
    comment: Optional[str] = None


# ---------------------------------------------------------------------------
# Providers endpoint
# ---------------------------------------------------------------------------

@router.get("/providers")
async def list_providers():
    """List available LLM providers and their models."""
    from providers import available_providers
    return available_providers()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@router.get("/sessions")
async def list_sessions(limit: int = 50):
    """List chat sessions, newest first."""
    rows = _db_query("""
        SELECT s.id, s.title, s.created_at, s.updated_at,
               COUNT(m.id) as message_count
        FROM chat_sessions s
        LEFT JOIN chat_messages m ON m.session_id = s.id
        GROUP BY s.id
        ORDER BY s.updated_at DESC
        LIMIT %(limit)s
    """, {"limit": limit})
    return rows


@router.post("/sessions")
async def create_session(req: CreateSessionRequest = None):
    """Create a new chat session."""
    title = (req.title if req and req.title else None)
    row = _db_execute("""
        INSERT INTO chat_sessions (title) VALUES (%(title)s)
        RETURNING id, title, created_at
    """, {"title": title})
    return row


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: int):
    """Delete a session and all its messages (CASCADE)."""
    existing = _db_query_one(
        "SELECT id FROM chat_sessions WHERE id = %(id)s", {"id": session_id}
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Session not found")
    _db_execute("DELETE FROM chat_sessions WHERE id = %(id)s", {"id": session_id})
    return {"ok": True}


@router.get("/sessions/{session_id}/messages")
async def get_messages(session_id: int):
    """Get all messages in a session."""
    existing = _db_query_one(
        "SELECT id FROM chat_sessions WHERE id = %(id)s", {"id": session_id}
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Session not found")
    rows = _db_query("""
        SELECT id, role, content, tracks_data, is_not_relevant,
               feedback_comment, created_at
        FROM chat_messages
        WHERE session_id = %(sid)s
        ORDER BY id
    """, {"sid": session_id})
    return rows


# ---------------------------------------------------------------------------
# Send message (main chat endpoint)
# ---------------------------------------------------------------------------

@router.post("/sessions/{session_id}/messages")
async def send_message(session_id: int, req: ChatMessageRequest):
    """
    Send a user message -> get AI response. Both are persisted.

    1. Save user message
    2. Load last 10 messages as history
    3. Dispatch to provider (Claude Code or API provider)
    4. Save assistant response (content, tracks_data, model, filters, retrieval_log)
    5. Return both messages with DB IDs
    """
    # Check that at least one provider is available
    from providers import available_providers
    providers = available_providers()
    if not providers:
        raise HTTPException(
            status_code=503,
            detail="No LLM providers configured. Set CLAUDE_CODE_ENABLED, ANTHROPIC_API_KEY, GROQ_API_KEY, or OPENAI_API_KEY.",
        )

    # Determine which provider to use
    provider_name = req.provider or settings.default_provider

    # Validate provider exists
    from providers import get_provider
    if provider_name != "claude_code" and get_provider(provider_name) is None:
        # Fallback to first available provider
        provider_name = providers[0]["id"]

    # For claude_code, verify it's enabled
    if provider_name == "claude_code" and not settings.claude_code_enabled:
        # Fallback to first non-claude_code provider
        non_cc = [p for p in providers if p["id"] != "claude_code"]
        if non_cc:
            provider_name = non_cc[0]["id"]
        else:
            raise HTTPException(status_code=503, detail="Claude Code is not enabled and no other providers available.")

    # Verify session exists
    session = _db_query_one(
        "SELECT id, title FROM chat_sessions WHERE id = %(id)s", {"id": session_id}
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # 1. Save user message
    user_row = _db_execute("""
        INSERT INTO chat_messages (session_id, role, content)
        VALUES (%(sid)s, 'user', %(content)s)
        RETURNING id, role, content, created_at
    """, {"sid": session_id, "content": req.message})

    # Auto-set session title from first message
    if not session["title"]:
        title = req.message[:80].strip()
        _db_execute(
            "UPDATE chat_sessions SET title = %(t)s WHERE id = %(id)s",
            {"t": title, "id": session_id},
        )

    # Update session timestamp
    _db_execute(
        "UPDATE chat_sessions SET updated_at = NOW() WHERE id = %(id)s",
        {"id": session_id},
    )

    # 2. Load history (last 10 messages before the one we just inserted)
    history_rows = _db_query("""
        SELECT role, content FROM chat_messages
        WHERE session_id = %(sid)s AND id < %(uid)s
        ORDER BY id DESC LIMIT 10
    """, {"sid": session_id, "uid": user_row["id"]})
    history_rows.reverse()  # chronological order

    history = [{"role": r["role"], "content": r["content"]} for r in history_rows] if history_rows else None

    # 3. Gather player context (non-blocking, best-effort)
    player_context = _get_player_context()

    # 4. Dispatch to provider
    try:
        if provider_name == "claude_code":
            result = _call_claude_code_dj(session_id, req.message, player_context, model=req.model)
        else:
            result = _call_api_provider_dj(
                provider_name, session_id, req.message,
                player_context, model=req.model, history=history,
            )
    except Exception as e:
        logger.error(f"Provider '{provider_name}' failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

    answer = result.get("answer", "")
    tracks = _validate_tracks(result.get("tracks", []))
    model = result.get("model", "")
    provider_used = result.get("provider", provider_name)
    filters_detected = result.get("filters_detected", {})
    retrieval_log = result.get("retrieval_log", [])
    tracks_retrieved = result.get("tracks_retrieved", 0)

    # Prepare tracks_data for JSONB (list of dicts with key info)
    tracks_data = [
        {
            "id": t.get("id"),
            "title": t.get("title"),
            "artist": t.get("artist"),
            "album": t.get("album"),
            "similarity": t.get("similarity"),
        }
        for t in tracks
    ] if tracks else None

    # 4. Save assistant message
    assistant_row = _db_execute("""
        INSERT INTO chat_messages
            (session_id, role, content, tracks_data, model,
             filters_detected, retrieval_log, tracks_retrieved)
        VALUES
            (%(sid)s, 'assistant', %(content)s, %(tracks_data)s, %(model)s,
             %(filters)s, %(rlog)s, %(tr)s)
        RETURNING id, role, content, tracks_data, created_at
    """, {
        "sid": session_id,
        "content": answer,
        "tracks_data": json.dumps(tracks_data) if tracks_data else None,
        "model": f"{provider_used}:{model}" if provider_used else model,
        "filters": json.dumps(filters_detected) if filters_detected else None,
        "rlog": json.dumps(retrieval_log) if retrieval_log else None,
        "tr": tracks_retrieved,
    })

    # 5. Return both messages + full tracks for the UI player
    return {
        "user_msg": user_row,
        "assistant_msg": assistant_row,
        "tracks": tracks,
        "filters_detected": filters_detected,
        "retrieval_log": retrieval_log,
        "model": model,
        "provider": provider_used,
        "tracks_retrieved": tracks_retrieved,
    }


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

@router.post("/messages/{message_id}/feedback")
async def set_feedback(message_id: int, req: FeedbackRequest):
    """Mark an assistant message as not relevant."""
    msg = _db_query_one(
        "SELECT id, role FROM chat_messages WHERE id = %(id)s", {"id": message_id}
    )
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg["role"] != "assistant":
        raise HTTPException(status_code=400, detail="Feedback only for assistant messages")

    _db_execute("""
        UPDATE chat_messages
        SET is_not_relevant = %(flag)s,
            feedback_comment = %(comment)s,
            feedback_at = NOW()
        WHERE id = %(id)s
    """, {
        "id": message_id,
        "flag": req.is_not_relevant,
        "comment": req.comment,
    })
    return {"ok": True}


@router.get("/feedback")
async def list_feedback(limit: int = 50):
    """
    List assistant messages marked as not relevant (for debugging).
    Includes the original user query (previous message in session).
    """
    rows = _db_query("""
        SELECT
            m.id,
            m.session_id,
            m.content,
            m.tracks_data,
            m.feedback_comment,
            m.feedback_at,
            m.filters_detected,
            m.retrieval_log,
            m.tracks_retrieved,
            m.model,
            m.created_at,
            (
                SELECT um.content FROM chat_messages um
                WHERE um.session_id = m.session_id
                  AND um.id < m.id
                  AND um.role = 'user'
                ORDER BY um.id DESC LIMIT 1
            ) as user_query
        FROM chat_messages m
        WHERE m.is_not_relevant = TRUE
        ORDER BY m.feedback_at DESC
        LIMIT %(limit)s
    """, {"limit": limit})
    return rows


# ---------------------------------------------------------------------------
# Legacy endpoint (backward compatibility with current frontend)
# ---------------------------------------------------------------------------

class LegacyChatRequest(BaseModel):
    message: str
    history: list[dict] = []


@router.post("")
async def legacy_chat(req: LegacyChatRequest):
    """Stateless chat endpoint for backward compatibility with existing frontend."""
    player_context = _get_player_context()

    if not settings.claude_code_enabled:
        raise HTTPException(status_code=503, detail="Claude Code is not enabled")

    try:
        from claude_code_runner import call_claude_code
        from claude_dj_prompt import get_system_prompt

        prompt = get_system_prompt("claude_code", player_context)

        result = call_claude_code(
            message=req.message,
            system_prompt=prompt,
        )
        return {
            "answer": result.get("answer", ""),
            "tracks": result.get("tracks", []),
            "filters_detected": {},
            "retrieval_log": [],
            "model": result.get("model", "claude-code"),
            "tracks_retrieved": len(result.get("tracks", [])),
        }
    except Exception as e:
        logger.error(f"Claude Code chat failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
