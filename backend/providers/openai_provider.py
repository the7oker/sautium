"""OpenAI API provider with agentic tool-use loop."""

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

            # No tool calls — return raw answer including any DJ_BLOCKS
            # marker. The chat router parses + hydrates the marker
            # centrally so providers stay format-agnostic.
            if choice.finish_reason != "tool_calls" or not msg.tool_calls:
                return ProviderResult(
                    answer=msg.content or "",
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

    def chat_stream(
        self,
        message: str,
        history: Optional[list[ProviderMessage]] = None,
        system_prompt: str = "",
        player_context: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Iterator[StreamEvent]:
        """Streaming variant of `chat`. OpenAI's streaming API delivers
        text and tool-call fragments interleaved across iterations of
        the tool-use loop. We forward text fragments live and assemble
        tool calls per iteration before executing them.
        """
        from tools.converters import to_openai_tools
        from tools.executor import execute_tool
        from tools import REGISTRY

        client = self._get_client()
        use_model = model if model in self.models() else self.models()[0]
        tools = to_openai_tools(REGISTRY)

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
                yield StreamDone(
                    model=use_model, provider=self.name,
                    tool_calls_count=tool_calls_count,
                    error="timeout",
                )
                return

            try:
                create_kwargs: dict[str, Any] = {
                    "model": use_model,
                    "messages": messages,
                    "tools": tools if tools else None,
                    "max_tokens": 4096,
                    "stream": True,
                }
                tc = self._tool_choice(iteration)
                if tc:
                    create_kwargs["tool_choice"] = tc
                stream = client.chat.completions.create(**create_kwargs)
            except Exception as e:
                logger.error(f"OpenAI API error: {e}")
                yield StreamDone(
                    model=use_model, provider=self.name,
                    tool_calls_count=tool_calls_count,
                    error=str(e),
                )
                return

            # Per-iteration accumulators. OpenAI delivers tool calls in
            # fragments keyed by index; we stitch them together so we
            # can execute them after `finish_reason` arrives.
            tool_call_acc: dict[int, dict[str, Any]] = {}
            content_acc = ""
            tool_names_yielded: set[int] = set()
            finish_reason: Optional[str] = None

            try:
                for chunk in stream:
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta

                    if getattr(delta, "content", None):
                        content_acc += delta.content
                        yield TextDelta(text=delta.content)

                    if getattr(delta, "tool_calls", None):
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            slot = tool_call_acc.setdefault(idx, {
                                "id": "", "name": "", "arguments": "",
                            })
                            if tc_delta.id:
                                slot["id"] += tc_delta.id
                            fn = getattr(tc_delta, "function", None)
                            if fn is not None:
                                if fn.name:
                                    slot["name"] += fn.name
                                if fn.arguments:
                                    slot["arguments"] += fn.arguments
                            # Announce the tool the moment we have its name.
                            if slot["name"] and idx not in tool_names_yielded:
                                tool_calls_count += 1
                                tool_names_yielded.add(idx)
                                yield ToolStart(name=slot["name"])

                    if choice.finish_reason:
                        finish_reason = choice.finish_reason
            except Exception as e:
                logger.error(f"OpenAI streaming error: {e}")
                yield StreamDone(
                    model=use_model, provider=self.name,
                    tool_calls_count=tool_calls_count,
                    error=str(e),
                )
                return

            if finish_reason != "tool_calls" or not tool_call_acc:
                yield StreamDone(
                    model=use_model, provider=self.name,
                    tool_calls_count=tool_calls_count,
                )
                return

            # Assemble assistant message with tool calls and execute them
            # — same shape as the non-streaming path in `chat()`.
            ordered_calls = [tool_call_acc[i] for i in sorted(tool_call_acc.keys())]
            assistant_msg = {
                "role": "assistant",
                "content": content_acc or None,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"] or "{}",
                        },
                    }
                    for tc in ordered_calls
                ],
            }
            messages.append(assistant_msg)

            for tc in ordered_calls:
                fn_name = tc["name"]
                try:
                    fn_args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except json.JSONDecodeError:
                    fn_args = {}
                logger.info(
                    f"Tool call [{iteration+1}]: {fn_name}({json.dumps(fn_args)[:200]})"
                )
                result = execute_tool(fn_name, fn_args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

        yield StreamDone(
            model=use_model, provider=self.name,
            tool_calls_count=tool_calls_count,
            error="max_iterations",
        )
