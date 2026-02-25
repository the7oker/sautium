"""Generic OpenAI-compatible provider for custom endpoints."""

from providers.openai_provider import OpenAIProvider


class OpenAICompatProvider(OpenAIProvider):
    """OpenAI-compatible provider with custom base_url."""

    name = "openai_compat"
    display_name = "Custom API"

    def __init__(self, api_key: str, base_url: str, model: str, display_name: str = "Custom API"):
        super().__init__(api_key=api_key, base_url=base_url)
        self._custom_model = model
        self.display_name = display_name

    def models(self) -> list[str]:
        return [self._custom_model]
