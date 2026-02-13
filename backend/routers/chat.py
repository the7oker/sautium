"""
AI DJ chat endpoint.

Thin wrapper around assistant.ask_assistant() for the Web UI.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])


class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []


@router.post("/chat")
async def chat(req: ChatRequest):
    """AI DJ chat — send a message, get recommendations with track list."""
    if not settings.anthropic_api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY is not configured")

    from database import get_db_context
    from assistant import ask_assistant

    try:
        # Keep only last 10 history messages
        history = req.history[-10:] if req.history else None

        with get_db_context() as db:
            result = ask_assistant(db, req.message, limit=20, history=history)

        return {
            "answer": result.get("answer", ""),
            "tracks": result.get("tracks", []),
            "filters_detected": result.get("filters_detected", {}),
        }
    except Exception as e:
        logger.error(f"Chat failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
