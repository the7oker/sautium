"""
AI DJ chat with persistent history and feedback.

Sessions, messages stored in PostgreSQL. Feedback endpoint for debugging
recommendation quality.
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


def _call_claude_code_dj(session_id: int, message: str, player_context: Optional[str]) -> dict:
    """Call Claude Code as AI DJ backend."""
    from claude_code_runner import call_claude_code
    from claude_dj_prompt import CLAUDE_DJ_SYSTEM_PROMPT

    claude_sid = _get_claude_session_id(session_id)

    # Format system prompt with player context
    pc_block = f"\n\nCurrently playing:\n{player_context}" if player_context else ""
    prompt = CLAUDE_DJ_SYSTEM_PROMPT.format(player_context=pc_block)

    result = call_claude_code(
        message=message,
        system_prompt=prompt,
        session_id=claude_sid,
        resume=bool(claude_sid),
    )

    # Save Claude session ID for future messages
    if result.get("claude_session_id"):
        _save_claude_session_id(session_id, result["claude_session_id"])

    return {
        "answer": result.get("answer", ""),
        "tracks": result.get("tracks", []),
        "model": result.get("model", "claude-code"),
        "filters_detected": {},
        "retrieval_log": [],
        "tracks_retrieved": len(result.get("tracks", [])),
    }


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class CreateSessionRequest(BaseModel):
    title: Optional[str] = None


class ChatMessageRequest(BaseModel):
    message: str


class FeedbackRequest(BaseModel):
    is_not_relevant: bool = True
    comment: Optional[str] = None


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
    3. Call ask_assistant()
    4. Save assistant response (content, tracks_data, model, filters, retrieval_log)
    5. Return both messages with DB IDs
    """
    if not settings.claude_code_enabled and not settings.anthropic_api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY is not configured")

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

    # 4. Call AI backend (Claude Code agent or RAG fallback)
    if settings.claude_code_enabled:
        try:
            result = _call_claude_code_dj(session_id, req.message, player_context)
        except Exception as e:
            logger.error(f"Claude Code DJ failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    else:
        from database import get_db_context
        from assistant import ask_assistant
        try:
            with get_db_context() as db:
                result = ask_assistant(db, req.message, limit=20, history=history, player_context=player_context)
        except Exception as e:
            logger.error(f"ask_assistant failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    answer = result.get("answer", "")
    tracks = result.get("tracks", [])
    model = result.get("model", "")
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
        "model": model,
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

    if settings.claude_code_enabled:
        try:
            from claude_code_runner import call_claude_code
            from claude_dj_prompt import CLAUDE_DJ_SYSTEM_PROMPT

            pc_block = f"\n\nCurrently playing:\n{player_context}" if player_context else ""
            prompt = CLAUDE_DJ_SYSTEM_PROMPT.format(player_context=pc_block)

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

    # RAG fallback
    if not settings.anthropic_api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY is not configured")

    from database import get_db_context
    from assistant import ask_assistant

    try:
        history = req.history[-10:] if req.history else None

        with get_db_context() as db:
            result = ask_assistant(db, req.message, limit=20, history=history, player_context=player_context)

        return {
            "answer": result.get("answer", ""),
            "tracks": result.get("tracks", []),
            "filters_detected": result.get("filters_detected", {}),
            "retrieval_log": result.get("retrieval_log", []),
            "model": result.get("model", ""),
            "tracks_retrieved": result.get("tracks_retrieved", 0),
        }
    except Exception as e:
        logger.error(f"Chat failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
