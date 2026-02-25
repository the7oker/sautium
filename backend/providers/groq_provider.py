"""Groq provider — thin subclass of OpenAI provider."""

from typing import Optional

from providers.openai_provider import OpenAIProvider


class GroqProvider(OpenAIProvider):
    """Groq (OpenAI-compatible API) with Llama models."""

    name = "groq"
    display_name = "Groq"

    def __init__(self, api_key: str):
        super().__init__(
            api_key=api_key,
            base_url="https://api.groq.com/openai/v1",
        )

    def _tool_choice(self, iteration: int) -> str | None:
        # Force first call to use tools, then let model decide
        return "required" if iteration == 0 else "auto"

    def models(self) -> list[str]:
        return ["llama-3.3-70b-versatile"]
