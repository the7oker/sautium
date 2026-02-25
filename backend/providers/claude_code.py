"""Claude Code provider — wraps existing subprocess-based Claude Code runner."""

import logging
from typing import Optional

from providers.base import BaseProvider, ProviderMessage, ProviderResult
from tools.track_parser import extract_tracks, strip_tracks_marker

logger = logging.getLogger(__name__)


class ClaudeCodeProvider(BaseProvider):
    """Provider that uses Claude Code CLI (subprocess) with MCP tools."""

    name = "claude_code"
    display_name = "Claude Code"

    def models(self) -> list[str]:
        return ["sonnet", "haiku"]

    def chat(
        self,
        message: str,
        history: Optional[list[ProviderMessage]] = None,
        system_prompt: str = "",
        player_context: Optional[str] = None,
        model: Optional[str] = None,
    ) -> ProviderResult:
        # This provider is special — it delegates to _call_claude_code_dj in chat.py
        # which manages Claude Code sessions. So this class is mainly for the
        # providers registry; actual call happens in the router.
        raise NotImplementedError(
            "ClaudeCodeProvider.chat() should not be called directly. "
            "Use the existing _call_claude_code_dj() path in routers/chat.py."
        )
