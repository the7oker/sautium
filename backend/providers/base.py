"""Base classes for LLM providers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional


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


# ---------------------------------------------------------------------------
# Streaming events — yielded by `BaseProvider.chat_stream`. The chat router
# transcodes them into SSE events for the browser. Keeping the shape
# narrow (text deltas + tool starts + a final "done" with metadata) lets
# every provider pick the closest natural granularity without contorting
# its SDK.
# ---------------------------------------------------------------------------

@dataclass
class StreamEvent:
    """Marker base class for provider stream events."""


@dataclass
class TextDelta(StreamEvent):
    """A chunk of generated assistant text. Multiple TextDeltas may
    arrive across iterations of a tool-use loop; the chat router
    concatenates them and runs the marker filter live so prose
    streams to the UI while the trailing `[DJ_BLOCKS]` payload is
    redirected into a structured `blocks` event."""
    text: str


@dataclass
class ToolStart(StreamEvent):
    """A tool call has started. Informational — the chat router
    forwards this so the UI can show a transient "searching tracks…"
    indicator. Tool execution itself happens inside the provider."""
    name: str


@dataclass
class StreamDone(StreamEvent):
    """Final event of a stream. Carries everything the chat router
    needs to persist the assistant message and close the SSE."""
    model: str = ""
    provider: str = ""
    tool_calls_count: int = 0
    # CLI agents (claude_code, codex): the agent-side resume handle.
    # `provider` tells the chat router which chat_sessions column it
    # belongs to (claude_session_id vs codex_thread_id).
    agent_session_id: Optional[str] = None
    error: Optional[str] = None
    # When the provider knows what concrete action the user can take to
    # fix `error` (top up balance, rotate the API key, wait out a rate
    # limit page) the chat UI renders this as a clickable link under
    # the error bubble. None means "no actionable next step".
    error_action: Optional[dict] = None  # {"label": str, "url": str}


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

    def chat_stream(
        self,
        message: str,
        history: Optional[list[ProviderMessage]] = None,
        system_prompt: str = "",
        player_context: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Iterator[StreamEvent]:
        """Stream a chat response as a sequence of `StreamEvent`s.

        Default implementation falls back to the synchronous `chat()`
        and emits the whole answer as a single `TextDelta`, then a
        `StreamDone`. Providers with native streaming SDKs override
        this for token-level delivery.
        """
        result = self.chat(
            message=message,
            history=history,
            system_prompt=system_prompt,
            player_context=player_context,
            model=model,
        )
        if result.answer:
            yield TextDelta(text=result.answer)
        yield StreamDone(
            model=result.model,
            provider=result.provider,
            tool_calls_count=result.tool_calls_count,
        )
