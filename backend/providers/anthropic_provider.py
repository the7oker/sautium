"""Anthropic API provider with agentic tool-use loop."""

import json
import logging
import time
from typing import Any, Optional

from providers.base import BaseProvider, ProviderMessage, ProviderResult
from tools.track_parser import extract_tracks, strip_tracks_marker

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 15
TIMEOUT_SECONDS = 120


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
                # Model is done — return accumulated text
                tracks = extract_tracks(last_text)
                clean = strip_tracks_marker(last_text)
                return ProviderResult(
                    answer=clean,
                    tracks=tracks,
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
            tracks = extract_tracks(last_text)
            clean = strip_tracks_marker(last_text)
            return ProviderResult(
                answer=clean,
                tracks=tracks,
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
