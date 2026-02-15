"""
Claude Code subprocess wrapper for AI DJ.

Calls `claude -p` in headless mode with MCP tools (PostgreSQL + HQPlayer).
Parses JSON output, extracts track recommendations from [DJ_TRACKS] marker.
"""

import json
import logging
import re
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MCP_CONFIG_PATH = "/app/mcp-docker.json"
CLAUDE_MODEL = "sonnet"
TIMEOUT_SECONDS = 120


def call_claude_code(
    message: str,
    system_prompt: str,
    session_id: Optional[str] = None,
    resume: bool = False,
) -> Dict[str, Any]:
    """
    Call Claude Code CLI in headless mode.

    Args:
        message: User message to send
        system_prompt: System prompt for AI DJ context
        session_id: Previous Claude Code session ID for continuity
        resume: Whether to resume a previous session

    Returns:
        dict with keys: answer, tracks, claude_session_id, model
    """
    cmd = [
        "claude",
        "-p", message,
        "--output-format", "json",
        "--mcp-config", MCP_CONFIG_PATH,
        "--model", CLAUDE_MODEL,
        "--system-prompt", system_prompt,
        "--dangerously-skip-permissions",
    ]

    if resume and session_id:
        cmd.extend(["--resume", session_id])

    logger.info(f"Claude Code call: message={message[:80]!r}, resume={resume}, session={session_id}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            env=None,  # inherit parent environment
        )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            logger.error(f"Claude Code failed (rc={result.returncode}): {stderr}")
            return {
                "answer": f"Claude Code error: {stderr or 'unknown error'}",
                "tracks": [],
                "claude_session_id": None,
                "model": CLAUDE_MODEL,
            }

        # Parse JSON output
        try:
            output = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Claude Code JSON: {e}\nstdout: {result.stdout[:500]}")
            return {
                "answer": result.stdout.strip() or "Failed to parse Claude Code response",
                "tracks": [],
                "claude_session_id": None,
                "model": CLAUDE_MODEL,
            }

        raw_answer = output.get("result", "")
        claude_sid = output.get("session_id")

        # Extract tracks from [DJ_TRACKS]...[/DJ_TRACKS] marker
        tracks = _extract_tracks(raw_answer)

        # Remove the marker from displayed answer
        clean_answer = _strip_tracks_marker(raw_answer)

        logger.info(
            f"Claude Code response: {len(clean_answer)} chars, "
            f"{len(tracks)} tracks, session={claude_sid}"
        )

        return {
            "answer": clean_answer,
            "tracks": tracks,
            "claude_session_id": claude_sid,
            "model": CLAUDE_MODEL,
        }

    except subprocess.TimeoutExpired:
        logger.error(f"Claude Code timed out after {TIMEOUT_SECONDS}s")
        return {
            "answer": "Request timed out. Please try a simpler query.",
            "tracks": [],
            "claude_session_id": None,
            "model": CLAUDE_MODEL,
        }
    except FileNotFoundError:
        logger.error("Claude Code CLI not found. Is it installed?")
        return {
            "answer": "Claude Code CLI is not installed in this environment.",
            "tracks": [],
            "claude_session_id": None,
            "model": CLAUDE_MODEL,
        }
    except Exception as e:
        logger.error(f"Unexpected error calling Claude Code: {e}")
        return {
            "answer": f"Error: {e}",
            "tracks": [],
            "claude_session_id": None,
            "model": CLAUDE_MODEL,
        }


def _extract_tracks(text: str) -> List[Dict[str, Any]]:
    """Extract track list from [DJ_TRACKS][...][/DJ_TRACKS] marker."""
    match = re.search(r'\[DJ_TRACKS\]\s*(\[.*?\])\s*\[/DJ_TRACKS\]', text, re.DOTALL)
    if not match:
        return []

    try:
        tracks = json.loads(match.group(1))
        if not isinstance(tracks, list):
            return []
        # Validate each track has required fields
        valid = []
        for t in tracks:
            if isinstance(t, dict) and t.get("id") and t.get("title"):
                valid.append({
                    "id": t["id"],
                    "title": t.get("title", ""),
                    "artist": t.get("artist", "Unknown"),
                    "album": t.get("album", ""),
                })
        return valid
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f"Failed to parse DJ_TRACKS JSON: {e}")
        return []


def _strip_tracks_marker(text: str) -> str:
    """Remove [DJ_TRACKS]...[/DJ_TRACKS] block from answer text."""
    cleaned = re.sub(r'\s*\[DJ_TRACKS\].*?\[/DJ_TRACKS\]\s*', '', text, flags=re.DOTALL)
    return cleaned.strip()
