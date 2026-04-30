"""Claude Code provider — wraps existing subprocess-based Claude Code runner."""

import logging
from typing import Optional

from providers.base import BaseProvider, ProviderMessage, ProviderResult

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
        # Sentinel: this provider is registered for `available_providers()`
        # but the chat router routes its work through the subprocess
        # streamer (`call_claude_code_stream`) directly — Claude Code
        # tracks its own session id, which the registry has no way to
        # carry. The router special-cases `claude_code` for this reason.
        raise NotImplementedError(
            "ClaudeCodeProvider.chat() is not used. The chat router "
            "dispatches Claude Code via call_claude_code_stream()."
        )
