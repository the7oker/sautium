"""LLM provider registry — lazy creation and caching."""

import logging
from typing import Optional

from providers.base import BaseProvider

logger = logging.getLogger(__name__)

_providers: dict[str, BaseProvider] = {}
_initialized = False


def reset() -> None:
    """Force `_init_providers` to re-run. Call after a CLI agent is
    installed or an API key is added in Settings — provider
    availability is otherwise cached for the process lifetime."""
    global _initialized
    _initialized = False
    _providers.clear()


# A CLI agent is registered when it is installed, signed in or not. Signed
# out is a condition the agent answers with ("sign in again"), never a reason
# to drop it: an unregistered pick makes chat._resolve_provider fall through
# to another provider — another account, or a pay-as-you-go key — without a
# word. The sign-in itself is claude_code.auth / codex_cli.auth.

def _claude_code_installed() -> bool:
    try:
        import claude_code
        return claude_code.get_claude_executable() is not None
    except Exception as e:
        logger.debug(f"claude_code detection failed: {e}")
        return False


def _codex_installed() -> bool:
    try:
        import codex_cli
        return codex_cli.get_codex_executable() is not None
    except Exception as e:
        logger.debug(f"codex detection failed: {e}")
        return False


def _maybe_reset_for_cli_agents() -> None:
    """If either CLI agent's registration would change since the cache was
    built, drop the cache so the next access re-detects. Cheap enough
    to call on every entry — a few file probes; the async chat handlers
    call it, so nothing here may spawn a process. The test is
    _init_providers' own, env override included."""
    if not _initialized:
        return
    from config import settings
    if ("claude_code" in _providers) != (settings.claude_code_enabled or _claude_code_installed()):
        reset()
        return
    if ("codex" in _providers) != (settings.codex_cli_enabled or _codex_installed()):
        reset()


def _init_providers():
    """Initialize available providers based on configuration."""
    global _initialized
    _maybe_reset_for_cli_agents()
    if _initialized:
        return
    _initialized = True

    # Ensure tool definitions are loaded
    from tools import ensure_definitions
    ensure_definitions()

    from config import settings

    # Claude Code (subprocess) — fact-based detection so that installing
    # it from the Web UI flips it on without a backend restart. The
    # legacy env flag still acts as an override for the corner case
    # where the user wants to force-enable it.
    if settings.claude_code_enabled or _claude_code_installed():
        from providers.claude_code import ClaudeCodeProvider
        _providers["claude_code"] = ClaudeCodeProvider()

    # OpenAI Codex (subprocess) — same fact-based detection; registered
    # after claude_code so the providers[0] fallback in
    # chat._resolve_provider keeps preferring Claude when both are there.
    if settings.codex_cli_enabled or _codex_installed():
        from providers.codex import CodexProvider
        _providers["codex"] = CodexProvider()

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
