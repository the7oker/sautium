"""LLM provider registry — lazy creation and caching."""

import logging
from typing import Optional

from providers.base import BaseProvider

logger = logging.getLogger(__name__)

_providers: dict[str, BaseProvider] = {}
_initialized = False


def _init_providers():
    """Initialize available providers based on configuration."""
    global _initialized
    if _initialized:
        return
    _initialized = True

    # Ensure tool definitions are loaded
    from tools import ensure_definitions
    ensure_definitions()

    from config import settings

    # Claude Code (subprocess) — always available if enabled
    if settings.claude_code_enabled:
        from providers.claude_code import ClaudeCodeProvider
        _providers["claude_code"] = ClaudeCodeProvider()

    # Anthropic API
    if settings.anthropic_api_key:
        from providers.anthropic_provider import AnthropicProvider
        _providers["anthropic"] = AnthropicProvider(api_key=settings.anthropic_api_key)

    # OpenAI API
    openai_key = getattr(settings, "openai_api_key", None)
    if openai_key:
        from providers.openai_provider import OpenAIProvider
        _providers["openai"] = OpenAIProvider(api_key=openai_key)

    # OpenAI-compatible custom endpoint
    compat_url = getattr(settings, "openai_compat_base_url", None)
    compat_key = getattr(settings, "openai_compat_api_key", None)
    compat_model = getattr(settings, "openai_compat_model", None)
    if compat_url and compat_model:
        from providers.openai_compat import OpenAICompatProvider
        compat_name = getattr(settings, "openai_compat_name", None) or "Custom API"
        _providers["openai_compat"] = OpenAICompatProvider(
            api_key=compat_key or "no-key",
            base_url=compat_url,
            model=compat_model,
            display_name=compat_name,
        )

    logger.info(f"Initialized LLM providers: {list(_providers.keys())}")


def get_provider(name: str) -> Optional[BaseProvider]:
    """Get a provider by name. Returns None if not available."""
    _init_providers()
    return _providers.get(name)


def available_providers() -> list[dict]:
    """Return list of configured providers with their models."""
    _init_providers()
    result = []
    for pid, provider in _providers.items():
        result.append({
            "id": pid,
            "name": provider.display_name,
            "models": provider.models(),
        })
    return result
