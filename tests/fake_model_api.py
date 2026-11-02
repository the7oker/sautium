"""A fake of the model APIs the agents talk to, on loopback: the Anthropic
Messages API (Claude Code, the Anthropic provider), the OpenAI Responses API
(Codex) and Chat Completions (the OpenAI provider).

A test points a client at `api.url` and gives the fake a script: one step
per model request, in order — `{"text": ...}` answers in words,
`{"tool": name, "input": {...}}` calls a function tool (`"namespace"` for a
Codex MCP tool), `{"custom": name, "input": "..."}` calls a Codex freeform
tool. Past the script the answer is the text "done". Every request body is
kept, so a test reads what the client offered the model and what each tool
call returned."""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeModelApi:
    def __init__(self, script=()):
        self.script = list(script)
        self.requests: list[tuple[str, dict]] = []
        self._lock = threading.Lock()
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                size = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(size) or b"{}")
                path = self.path.split("?", 1)[0]
                if path.endswith("/count_tokens"):
                    return self._send(200, "application/json", json.dumps({"input_tokens": 1}))
                if not path.endswith(("/messages", "/responses", "/chat/completions")):
                    return self._send(404, "application/json", "{}")
                with api._lock:
                    n = len(api.requests)
                    api.requests.append((path, body))
                step = api.script[n] if n < len(api.script) else {"text": "done"}
                if path.endswith("/messages"):
                    return self._messages(n, step, body)
                if path.endswith("/responses"):
                    return self._responses(n, step)
                return self._completions(n, step, body)

            def do_GET(self):
                self._send(404, "application/json", "{}")

            def _send(self, status, ctype, text):
                data = text.encode() if isinstance(text, str) else text
                self.send_response(status)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _messages(self, n, step, body):
                if "tool" in step:
                    block = {"type": "tool_use", "id": f"toolu_{n}", "name": step["tool"],
                             "input": step.get("input", {})}
                    stop = "tool_use"
                else:
                    block = {"type": "text", "text": step["text"]}
                    stop = "end_turn"
                usage = {"input_tokens": 1, "output_tokens": 1}
                if not body.get("stream"):
                    return self._send(200, "application/json", json.dumps({
                        "id": f"msg_{n}", "type": "message", "role": "assistant",
                        "model": body.get("model", "m"), "content": [block],
                        "stop_reason": stop, "stop_sequence": None, "usage": usage}))
                start = dict(block, input={}) if "tool" in step else dict(block, text="")
                delta = ({"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
                         if "tool" in step else {"type": "text_delta", "text": block["text"]})
                events = [
                    ("message_start", {"type": "message_start", "message": {
                        "id": f"msg_{n}", "type": "message", "role": "assistant",
                        "model": body.get("model", "m"), "content": [], "stop_reason": None,
                        "stop_sequence": None, "usage": usage}}),
                    ("content_block_start", {"type": "content_block_start", "index": 0,
                                             "content_block": start}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                             "delta": delta}),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ("message_delta", {"type": "message_delta", "usage": {"output_tokens": 1},
                                       "delta": {"stop_reason": stop, "stop_sequence": None}}),
                    ("message_stop", {"type": "message_stop"}),
                ]
                self._send(200, "text/event-stream",
                           "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events))

            def _responses(self, n, step):
                if "tool" in step:
                    item = {"type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}",
                            "name": step["tool"], "arguments": json.dumps(step.get("input", {}))}
                    if step.get("namespace"):
                        item["namespace"] = step["namespace"]
                elif "custom" in step:
                    item = {"type": "custom_tool_call", "id": f"ct_{n}", "call_id": f"call_{n}",
                            "name": step["custom"], "input": step["input"]}
                else:
                    item = {"type": "message", "id": f"msg_{n}", "role": "assistant",
                            "content": [{"type": "output_text", "text": step["text"],
                                         "annotations": []}]}
                events = [
                    {"type": "response.created", "response": {"id": f"resp_{n}"}},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": {"id": f"resp_{n}", "usage": {
                        "input_tokens": 1, "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens": 1, "output_tokens_details": {"reasoning_tokens": 0},
                        "total_tokens": 2}}},
                ]
                self._send(200, "text/event-stream",
                           "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events))

            def _completions(self, n, step, body):
                message = {"role": "assistant", "content": step.get("text")}
                finish = "stop"
                if "tool" in step:
                    message["tool_calls"] = [{"id": f"call_{n}", "type": "function", "function": {
                        "name": step["tool"], "arguments": json.dumps(step.get("input", {}))}}]
                    finish = "tool_calls"
                self._send(200, "application/json", json.dumps({
                    "id": f"chatcmpl-{n}", "object": "chat.completion", "created": 0,
                    "model": body.get("model", "m"),
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}))

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    def bodies(self, suffix: str) -> list[dict]:
        """The request bodies sent to the endpoint ending in `suffix`."""
        return [b for p, b in self.requests if p.endswith(suffix)]


def anthropic_tools(body: dict) -> list[str]:
    """The tools a Messages request offers the model, by name."""
    return [t["name"] for t in body.get("tools") or []]


def anthropic_tool_results(body: dict) -> list[str]:
    """What the tool calls answered, from every message of a request."""
    return [json.dumps(c.get("content")) for m in body.get("messages") or []
            for c in (m.get("content") if isinstance(m.get("content"), list) else [])
            if isinstance(c, dict) and c.get("type") == "tool_result"]


def codex_tools(body: dict) -> dict[str, list[str]]:
    """The tools a Responses request from codex offers, per namespace."""
    offered: dict[str, list[str]] = {}
    for item in body.get("input") or []:
        if item.get("type") == "additional_tools":
            for ns in item.get("tools") or []:
                offered[ns.get("name")] = [t.get("name") for t in ns.get("tools") or []]
    for t in body.get("tools") or []:
        offered.setdefault("", []).append(t.get("name"))
    return offered


def codex_instructions(body: dict) -> str:
    """The base instructions a Responses request from codex carries: codex
    0.149 sends model_instructions_file's text as the first developer
    message, not in the `instructions` field."""
    for item in body.get("input") or []:
        if item.get("type") == "message" and item.get("role") == "developer":
            return "".join(c.get("text", "") for c in item.get("content") or [])
    return ""


def codex_hosted_tools(body: dict) -> list[str]:
    """The tools codex offers inside its code-mode host, by name — the host's
    description lists each under a "### `name`" heading."""
    return [name for item in body.get("input") or [] if item.get("type") == "additional_tools"
            for ns in item.get("tools") or [] for t in ns.get("tools") or []
            for name in re.findall(r"### `([a-z_]+)`", t.get("description") or "")]


def codex_tool_outputs(body: dict) -> list[str]:
    """What codex sent back for the tool calls, from a Responses request."""
    return [json.dumps(item.get("output")) for item in body.get("input") or []
            if item.get("type") in ("function_call_output", "custom_tool_call_output")]
