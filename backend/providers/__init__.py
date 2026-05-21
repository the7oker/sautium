"""LLM provider registry — lazy creation and caching."""

import logging
from typing import Optional

from providers.base import BaseProvider

logger = logging.getLogger(__name__)

_providers: dict[str, BaseProvider] = {}
_initialized = False


def reset() -> None:
    """Force `_init_providers` to re-run. Call after the user
    completes Claude Code sign-in or adds an API key in Settings —
    provider availability is otherwise cached for the process
    lifetime."""
    global _initialized
    _initialized = False
    _providers.clear()


def _claude_code_ready() -> bool:
    """True when the Claude Code CLI is installed and authenticated.
    Used instead of an env flag so adding/removing the credentials
    file is picked up after a reset(). Docker can't shell out to the
    host so `get_claude_executable()` returns None there."""
    try:
        from claude_code import get_claude_executable, claude_authenticated
        return get_claude_executable() is not None and claude_authenticated()
    except Exception as e:
        logger.debug(f"claude_code readiness probe failed: {e}")
        return False


def _maybe_reset_for_claude_code() -> None:
    """If Claude Code's readiness has changed since the cache was
    built, drop the cache so the next access re-detects. Cheap
    enough to call on every entry — it's two file/keychain probes."""
    if not _initialized:
        return
    have = "claude_code" in _providers
    ready = _claude_code_ready()
    if have != ready:
        reset()


def _init_providers():
    """Initialize available providers based on configuration."""
    global _initialized
    _maybe_reset_for_claude_code()
    if _initialized:
        return
    _initialized = True

    # Ensure tool definitions are loaded
    from tools import ensure_definitions
    ensure_definitions()

    from config import settings

    # Claude Code (subprocess) — fact-based detection so that signing
    # in from the Web UI flips it on without a backend restart. The
    # legacy env flag still acts as an override for the corner case
    # where the user wants to force-enable it.
    if settings.claude_code_enabled or _claude_code_ready():
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
