"""OpenAI API provider with agentic tool-use loop."""

import json
import logging
import time
from typing import Any, Optional

from providers.base import BaseProvider, ProviderMessage, ProviderResult
from tools.track_parser import extract_tracks, strip_tracks_marker

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 15
TIMEOUT_SECONDS = 120


class OpenAIProvider(BaseProvider):
    """Provider using OpenAI SDK with function calling."""

    name = "openai"
    display_name = "OpenAI"

    def __init__(self, api_key: str, base_url: Optional[str] = None):
        self._api_key = api_key
        self._base_url = base_url
        self._client = None

    def _get_client(self):
        if self._client is None:
            import openai
            kwargs: dict[str, Any] = {"api_key": self._api_key}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = openai.OpenAI(**kwargs)
        return self._client

    def _tool_choice(self, iteration: int) -> str | None:
        """Return tool_choice for API call. Override in subclasses."""
        return None

    def models(self) -> list[str]:
        return ["gpt-4o", "gpt-4o-mini"]

    def chat(
        self,
        message: str,
        history: Optional[list[ProviderMessage]] = None,
        system_prompt: str = "",
        player_context: Optional[str] = None,
        model: Optional[str] = None,
    ) -> ProviderResult:
        from tools.converters import to_openai_tools
        from tools.executor import execute_tool
        from tools import REGISTRY

        client = self._get_client()
        use_model = model if model in self.models() else self.models()[0]
        tools = to_openai_tools(REGISTRY)

        # Build messages
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if history:
            for m in history:
                messages.append({"role": m.role, "content": m.content})
        messages.append({"role": "user", "content": message})

        tool_calls_count = 0
        start_time = time.time()

        for iteration in range(MAX_ITERATIONS):
            if time.time() - start_time > TIMEOUT_SECONDS:
                logger.warning("OpenAI provider timeout reached")
                break

            try:
                create_kwargs: dict[str, Any] = {
                    "model": use_model,
                    "messages": messages,
                    "tools": tools if tools else None,
                    "max_tokens": 4096,
                }
                tc = self._tool_choice(iteration)
                if tc:
                    create_kwargs["tool_choice"] = tc
                response = client.chat.completions.create(**create_kwargs)
            except Exception as e:
                logger.error(f"OpenAI API error: {e}")
                return ProviderResult(
                    answer=f"API error: {e}",
                    provider=self.name,
                    model=use_model,
                )

            choice = response.choices[0]
            msg = choice.message

            # No tool calls — return final answer
            if choice.finish_reason != "tool_calls" or not msg.tool_calls:
                answer = msg.content or ""
                tracks = extract_tracks(answer)
                clean = strip_tracks_marker(answer)
                return ProviderResult(
                    answer=clean,
                    tracks=tracks,
                    model=use_model,
                    provider=self.name,
                    tool_calls_count=tool_calls_count,
                )

            # Process tool calls — convert to plain dict for safe serialization
            assistant_msg = {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }
            messages.append(assistant_msg)

            for tc in msg.tool_calls:
                tool_calls_count += 1
                fn_name = tc.function.name
                try:
                    fn_args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    fn_args = {}

                logger.info(f"Tool call [{iteration+1}]: {fn_name}({json.dumps(fn_args)[:200]})")
                result = execute_tool(fn_name, fn_args)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

        return ProviderResult(
            answer="Reached maximum tool call iterations.",
            provider=self.name,
            model=use_model,
            tool_calls_count=tool_calls_count,
        )
