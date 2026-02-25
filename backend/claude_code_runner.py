"""
Claude Code subprocess wrapper for AI DJ.

Calls `claude -p` in headless mode with MCP tools (PostgreSQL + HQPlayer).
Parses JSON output, extracts track recommendations from [DJ_TRACKS] marker.
"""

import json
import logging
import os
import pwd
import re
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MCP_CONFIG_PATH = "/app/mcp-docker.json"
DEFAULT_MODEL = "sonnet"
ALLOWED_MODELS = {"sonnet", "haiku"}
TIMEOUT_SECONDS = 120
CLAUDE_USER = "claudeuser"  # non-root user (--dangerously-skip-permissions requires non-root)


def call_claude_code(
    message: str,
    system_prompt: str,
    session_id: Optional[str] = None,
    resume: bool = False,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Call Claude Code CLI in headless mode.

    Args:
        message: User message to send
        system_prompt: System prompt for AI DJ context
        session_id: Previous Claude Code session ID for continuity
        resume: Whether to resume a previous session
        model: Model to use (sonnet or haiku). Defaults to DEFAULT_MODEL.

    Returns:
        dict with keys: answer, tracks, claude_session_id, model
    """
    use_model = model if model in ALLOWED_MODELS else DEFAULT_MODEL

    cmd = [
        "claude",
        "-p", message,
        "--output-format", "json",
        "--mcp-config", MCP_CONFIG_PATH,
        "--model", use_model,
        "--system-prompt", system_prompt,
        "--dangerously-skip-permissions",
    ]

    if resume and session_id:
        cmd.extend(["--resume", session_id])

    logger.info(f"Claude Code call: message={message[:80]!r}, resume={resume}, session={session_id}")

    try:
        # Run as non-root user (Claude Code blocks --dangerously-skip-permissions as root)
        pw = pwd.getpwnam(CLAUDE_USER)

        def demote():
            os.setgid(pw.pw_gid)
            os.setuid(pw.pw_uid)

        env = os.environ.copy()
        env["HOME"] = pw.pw_dir

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            preexec_fn=demote,
            env=env,
        )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            logger.error(f"Claude Code failed (rc={result.returncode}): {stderr}")
            return {
                "answer": f"Claude Code error: {stderr or 'unknown error'}",
                "tracks": [],
                "claude_session_id": None,
                "model": use_model,
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
                "model": use_model,
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
            "model": use_model,
        }

    except subprocess.TimeoutExpired:
        logger.error(f"Claude Code timed out after {TIMEOUT_SECONDS}s")
        return {
            "answer": "Request timed out. Please try a simpler query.",
            "tracks": [],
            "claude_session_id": None,
            "model": use_model,
        }
    except FileNotFoundError:
        logger.error("Claude Code CLI not found. Is it installed?")
        return {
            "answer": "Claude Code CLI is not installed in this environment.",
            "tracks": [],
            "claude_session_id": None,
            "model": use_model,
        }
    except Exception as e:
        logger.error(f"Unexpected error calling Claude Code: {e}")
        return {
            "answer": f"Error: {e}",
            "tracks": [],
            "claude_session_id": None,
            "model": use_model,
        }


try:
    from tools.track_parser import extract_tracks as _extract_tracks
    from tools.track_parser import strip_tracks_marker as _strip_tracks_marker
except ImportError:
    # Fallback if tools package not available
    def _extract_tracks(text: str) -> List[Dict[str, Any]]:
        """Extract track list from [DJ_TRACKS][...][/DJ_TRACKS] marker."""
        match = re.search(r'\[DJ_TRACKS\]\s*(\[.*?\])\s*\[/DJ_TRACKS\]', text, re.DOTALL)
        if not match:
            return []
        try:
            tracks = json.loads(match.group(1))
            if not isinstance(tracks, list):
                return []
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
