"""Anthropic API provider with agentic tool-use loop."""

import json
import logging
import time
from typing import Any, Iterator, Optional

from providers.base import (
    BaseProvider,
    ProviderMessage,
    ProviderResult,
    StreamDone,
    StreamEvent,
    TextDelta,
    ToolStart,
)

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 15
TIMEOUT_SECONDS = 120


def _humanize_error(exc: Exception) -> tuple[str, Optional[dict]]:
    """Pull (message, action) out of an Anthropic SDK exception.

    `str(exc)` from the SDK looks like
        `Error code: 400 - {'type': 'error', 'error': {...message: 'X'}}`
    — readable for engineers, not for users. The SDK exposes the same
    message under `.message`/`.body['error']['message']`; preferring it
    drops the HTTP code, the dict noise and the request_id, leaving
    just 'Your credit balance is too low …'.

    When the failure is something the user can fix with a single click
    (top up balance, rotate the key) we also return an `action` dict
    so the chat UI can render a clickable link."""
    # Priority order: the parsed `body['error']['message']` is the only
    # clean source. `.message` and str(exc) both look like
    # `Error code: 400 - {dict}` because the SDK formats them via
    # __init__ — using them first means we re-emit the raw payload.
    msg = ""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            m = err.get("message")
            if isinstance(m, str) and m.strip():
                msg = m.strip()
    if not msg:
        # SDK exceptions that don't carry a body — typically network
        # errors. `.message` may still be human-readable here, just
        # less specific.
        direct = getattr(exc, "message", None)
        if isinstance(direct, str) and direct.strip():
            msg = direct.strip()
    if not msg:
        msg = str(exc).strip() or "Anthropic API call failed"

    # Map known classes of Anthropic errors to concrete next steps. We
    # match on the SDK exception class first (most reliable), then fall
    # back to keyword sniffing in the message text for cases where the
    # SDK only raises a generic APIError.
    low = msg.lower()
    name = type(exc).__name__
    if "credit balance" in low or "billing" in low or "insufficient" in low:
        return msg, {
            "label": "Top up balance",
            "url": "https://console.anthropic.com/settings/billing",
        }
    if name == "AuthenticationError" or "api key" in low or "401" in low:
        return msg, {
            "label": "Manage API keys",
            "url": "https://console.anthropic.com/settings/keys",
        }
    if name == "RateLimitError" or "rate limit" in low or "429" in low:
        return msg, {
            "label": "View rate limits",
            "url": "https://docs.anthropic.com/en/api/rate-limits",
        }
    return msg, None


class AnthropicProvider(BaseProvider):
    """Provider using Anthropic SDK with tool calling."""

    name = "anthropic"
    display_name = "Anthropic API"

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._client = None

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def models(self) -> list[str]:
        return ["claude-sonnet-4-20250514", "claude-haiku-4-5-20251001"]

    def chat(
        self,
        message: str,
        history: Optional[list[ProviderMessage]] = None,
        system_prompt: str = "",
        player_context: Optional[str] = None,
        model: Optional[str] = None,
    ) -> ProviderResult:
        from tools.converters import to_anthropic_tools
        from tools.executor import execute_tool
        from tools import REGISTRY

        client = self._get_client()
        use_model = model if model in self.models() else self.models()[0]
        tools = to_anthropic_tools(REGISTRY)

        # Build messages
        messages: list[dict[str, Any]] = []
        if history:
            for m in history:
                messages.append({"role": m.role, "content": m.content})
        messages.append({"role": "user", "content": message})

        tool_calls_count = 0
        start_time = time.time()
        last_text = ""  # accumulate text across iterations

        for iteration in range(MAX_ITERATIONS):
            if time.time() - start_time > TIMEOUT_SECONDS:
                logger.warning("Anthropic provider timeout reached")
                break

            try:
                response = client.messages.create(
                    model=use_model,
                    max_tokens=4096,
                    system=system_prompt,
                    tools=tools,
                    messages=messages,
                )
            except Exception as e:
                logger.error(f"Anthropic API error: {e}")
                return ProviderResult(
                    answer=f"Anthropic API error: {e}",
                    provider=self.name,
                    model=use_model,
                )

            # Collect text from this response
            text_parts = []
            for block in response.content:
                if block.type == "text":
                    text_parts.append(block.text)
            if text_parts:
                last_text = "\n".join(text_parts)

            # Check if model wants to use tools
            # stop_reason == "tool_use" means: process tools and call me again
            # stop_reason == "end_turn" means: I'm done
            if response.stop_reason != "tool_use":
                # Model is done — return raw text including any DJ_BLOCKS
                # marker. The chat router parses + hydrates the marker
                # centrally so providers stay format-agnostic.
                return ProviderResult(
                    answer=last_text,
                    model=use_model,
                    provider=self.name,
                    tool_calls_count=tool_calls_count,
                )

            # Process tool calls
            # Convert content blocks to plain dicts (avoids Pydantic serialization issues)
            content_dicts = []
            for block in response.content:
                if block.type == "text":
                    content_dicts.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    content_dicts.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })
            messages.append({"role": "assistant", "content": content_dicts})

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_calls_count += 1
                    logger.info(f"Tool call [{iteration+1}]: {block.name}({json.dumps(block.input)[:200]})")
                    result = execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            messages.append({"role": "user", "content": tool_results})

        # Exceeded iterations — return whatever text we collected
        if last_text:
            return ProviderResult(
                answer=last_text,
                model=use_model,
                provider=self.name,
                tool_calls_count=tool_calls_count,
            )

        return ProviderResult(
            answer="Reached maximum tool call iterations without a final response.",
            provider=self.name,
            model=use_model,
            tool_calls_count=tool_calls_count,
        )

    def chat_stream(
        self,
        message: str,
        history: Optional[list[ProviderMessage]] = None,
        system_prompt: str = "",
        player_context: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Iterator[StreamEvent]:
        """Streaming variant of `chat`. Yields `TextDelta`s as the model
        produces tokens, `ToolStart` whenever the model invokes a tool,
        and ends with `StreamDone`. Tool execution still happens
        synchronously between iterations — only token generation is
        streamed."""
        from tools.converters import to_anthropic_tools
        from tools.executor import execute_tool
        from tools import REGISTRY

        client = self._get_client()
        use_model = model if model in self.models() else self.models()[0]
        tools = to_anthropic_tools(REGISTRY)

        messages: list[dict[str, Any]] = []
        if history:
            for m in history:
                messages.append({"role": m.role, "content": m.content})
        messages.append({"role": "user", "content": message})

        tool_calls_count = 0
        start_time = time.time()

        for iteration in range(MAX_ITERATIONS):
            if time.time() - start_time > TIMEOUT_SECONDS:
                logger.warning("Anthropic provider timeout reached")
                yield StreamDone(
                    model=use_model, provider=self.name,
                    tool_calls_count=tool_calls_count,
                    error="timeout",
                )
                return

            try:
                stream_ctx = client.messages.stream(
                    model=use_model,
                    max_tokens=4096,
                    system=system_prompt,
                    tools=tools,
                    messages=messages,
                )
            except Exception as e:
                logger.error(f"Anthropic API error: {e}")
                msg, action = _humanize_error(e)
                yield StreamDone(
                    model=use_model, provider=self.name,
                    tool_calls_count=tool_calls_count,
                    error=msg, error_action=action,
                )
                return

            try:
                with stream_ctx as stream:
                    for event in stream:
                        et = getattr(event, "type", None)
                        if et == "content_block_start":
                            cb = getattr(event, "content_block", None)
                            if cb is not None and getattr(cb, "type", None) == "tool_use":
                                tool_calls_count += 1
                                yield ToolStart(name=getattr(cb, "name", "tool"))
                        elif et == "content_block_delta":
                            delta = getattr(event, "delta", None)
                            if delta is not None and getattr(delta, "type", None) == "text_delta":
                                text = getattr(delta, "text", "")
                                if text:
                                    yield TextDelta(text=text)
                        # input_json_delta / message_delta / message_stop are
                        # not surfaced to the UI — we wait for the final
                        # message via `get_final_message()` below.

                    final_msg = stream.get_final_message()
            except Exception as e:
                logger.error(f"Anthropic streaming error: {e}")
                msg, action = _humanize_error(e)
                yield StreamDone(
                    model=use_model, provider=self.name,
                    tool_calls_count=tool_calls_count,
                    error=msg, error_action=action,
                )
                return

            if final_msg.stop_reason != "tool_use":
                yield StreamDone(
                    model=use_model, provider=self.name,
                    tool_calls_count=tool_calls_count,
                )
                return

            # Round-trip tool calls. Same logic as `chat()` but split out
            # so streaming and non-streaming paths share neither
            # transient state nor a buffered "last_text".
            content_dicts = []
            for block in final_msg.content:
                if block.type == "text":
                    content_dicts.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    content_dicts.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })
            messages.append({"role": "assistant", "content": content_dicts})

            tool_results = []
            for block in final_msg.content:
                if block.type == "tool_use":
                    logger.info(
                        f"Tool call [{iteration+1}]: {block.name}({json.dumps(block.input)[:200]})"
                    )
                    result = execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})

        yield StreamDone(
            model=use_model, provider=self.name,
            tool_calls_count=tool_calls_count,
            error="max_iterations",
        )
