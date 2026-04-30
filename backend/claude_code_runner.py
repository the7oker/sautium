"""
Claude Code subprocess wrapper for AI DJ.

Calls `claude -p` in headless mode with MCP tools (PostgreSQL + HQPlayer).
Returns the raw model answer (including any DJ_BLOCKS marker) — the chat
router parses + hydrates the marker centrally so this wrapper stays
format-agnostic.
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

if sys.platform != "win32":
    import pwd

logger = logging.getLogger(__name__)

if sys.platform == "win32":
    # Windows desktop mode: mcp-windows.json next to this module
    MCP_CONFIG_PATH = str(Path(__file__).parent / "mcp-windows.json")
else:
    MCP_CONFIG_PATH = "/app/mcp-docker.json"
DEFAULT_MODEL = "sonnet"
ALLOWED_MODELS = {"sonnet", "haiku"}
TIMEOUT_SECONDS = 180
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
        env = os.environ.copy()
        kwargs = {
            "capture_output": True,
            "text": True,
            "timeout": TIMEOUT_SECONDS,
            "env": env,
        }

        if sys.platform == "win32":
            # Windows: no user switching needed, add CREATE_NO_WINDOW
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            # Linux/Docker: run as non-root user
            pw = pwd.getpwnam(CLAUDE_USER)

            def demote():
                os.setgid(pw.pw_gid)
                os.setuid(pw.pw_uid)

            kwargs["preexec_fn"] = demote
            env["HOME"] = pw.pw_dir

        result = subprocess.run(cmd, **kwargs)

        if result.returncode != 0:
            stderr = result.stderr.strip()
            logger.error(f"Claude Code failed (rc={result.returncode}): {stderr}")
            return {
                "answer": f"Claude Code error: {stderr or 'unknown error'}",
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
                "claude_session_id": None,
                "model": use_model,
            }

        raw_answer = output.get("result", "")
        claude_sid = output.get("session_id")

        logger.info(
            f"Claude Code response: {len(raw_answer)} chars, session={claude_sid}"
        )

        return {
            "answer": raw_answer,
            "claude_session_id": claude_sid,
            "model": use_model,
        }

    except subprocess.TimeoutExpired:
        logger.error(f"Claude Code timed out after {TIMEOUT_SECONDS}s")
        return {
            "answer": "Request timed out. Please try a simpler query.",
            "claude_session_id": None,
            "model": use_model,
        }
    except FileNotFoundError:
        logger.error("Claude Code CLI not found. Is it installed?")
        return {
            "answer": "Claude Code CLI is not installed in this environment.",
            "claude_session_id": None,
            "model": use_model,
        }
    except Exception as e:
        logger.error(f"Unexpected error calling Claude Code: {e}")
        return {
            "answer": f"Error: {e}",
            "claude_session_id": None,
            "model": use_model,
        }
