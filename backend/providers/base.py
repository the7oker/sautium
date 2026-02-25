"""Base classes for LLM providers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ProviderMessage:
    """A single message in chat history."""
    role: str  # "user" or "assistant"
    content: str


@dataclass
class ProviderResult:
    """Result from an LLM provider chat call."""
    answer: str
    tracks: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    provider: str = ""
    tool_calls_count: int = 0


class BaseProvider(ABC):
    """Abstract base for LLM providers."""

    name: str = "base"
    display_name: str = "Base"

    @abstractmethod
    def models(self) -> list[str]:
        """Return list of available model identifiers."""
        ...

    @abstractmethod
    def chat(
        self,
        message: str,
        history: Optional[list[ProviderMessage]] = None,
        system_prompt: str = "",
        player_context: Optional[str] = None,
        model: Optional[str] = None,
    ) -> ProviderResult:
        """Send a message and get a response, possibly with tool calls.

        Args:
            message: User message text
            history: Previous messages for context
            system_prompt: System prompt to use
            player_context: Current HQPlayer state info
            model: Model identifier to use (from self.models())

        Returns:
            ProviderResult with answer and extracted tracks
        """
        ...
