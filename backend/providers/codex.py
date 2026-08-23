"""Codex provider — wraps the subprocess-based OpenAI Codex CLI runner."""

import logging
from typing import Optional

from providers.base import BaseProvider, ProviderMessage, ProviderResult

logger = logging.getLogger(__name__)


class CodexProvider(BaseProvider):
    """Provider that uses the OpenAI Codex CLI (subprocess) with MCP tools."""

    name = "codex"
    display_name = "OpenAI Codex"

    def models(self) -> list[str]:
        from codex_runner import ALLOWED_MODELS, DEFAULT_MODEL
        return [DEFAULT_MODEL] + sorted(ALLOWED_MODELS - {DEFAULT_MODEL})

    def chat(
        self,
        message: str,
        history: Optional[list[ProviderMessage]] = None,
        system_prompt: str = "",
        player_context: Optional[str] = None,
        model: Optional[str] = None,
    ) -> ProviderResult:
        # Sentinel: registered for `available_providers()` but the chat
        # router routes its work through the subprocess streamer
        # (`call_codex_stream`) directly — codex tracks its own thread
        # id, which the registry has no way to carry. Same pattern as
        # ClaudeCodeProvider.
        raise NotImplementedError(
            "CodexProvider.chat() is not used. The chat router "
            "dispatches Codex via call_codex_stream()."
        )
