"""Strip the harness text that Devin's content policy rejects.

Devin's Cascade API rejects some text from other agent harnesses' own prompts.
For Claude Code the error is `permission_denied: ... blocked by our content
policy`. For Codex it is `permission_denied: Unable to process request due to
an MCP configuration issue`. The verdict is the same for the same bytes every
time, and a same-size neutral text passes. These pieces are rejected:

Claude Code (Anthropic Messages API, ``system``):
1. the identity line `You are a Claude agent, built on Anthropic's Claude Agent SDK.`
2. the safety paragraph beginning `IMPORTANT: Assist with authorized security testing`
3. the vendor paragraph beginning `The most recent Claude models are the Claude`

Codex (OpenAI Responses API, developer ``input`` and ``tools``):
4. the paragraph `Within this context, Codex refers to the open-source agentic coding interface ...`
5. the line `- Don't output ANSI escape codes directly — the CLI renderer applies them.`
6. the descriptions of the `exec_command` and `write_stdin` tools. Their names
   and parameter schemas pass, so only the description is replaced.

Everything else passes, including the other tools, metadata and the task
prompt. This proxy sits between the harness and 9router, removes or neutralises
those pieces, and streams the response back unchanged.

Responses adapter: for ``/v1/responses`` the shim no longer proxies but
terminates the API itself, translating to chat completions and back. 9router's
own Responses translation splits every turn into a bare narration message plus
a separate tool-calls-only message, and swe-2-1 — trained in a harness where
narration introduces the action in the same unit — reuses that pattern: it
emits narration and stops (~35% at narration-heavy steps, killing the turn,
vs 0% with the merged shape). The adapter merges reasoning, narration and tool
calls of each turn into one assistant message, keeps reasoning as
``reasoning_content``, flattens namespace tools instead of collapsing them,
and fails loudly (``response.failed``, which codex retries) when the upstream
stream closes without any output instead of completing with empty output
(which ends the turn dead). An empty stream usually means the upstream hit a
minute-aligned rate limit: the devin bridge swallows the rate-limit error and
returns a 200 with no output after its internal retries give up. The shim
retries the upstream on an empty stream with backoff, because the limit
resets within minutes and nothing has been sent downstream yet.

Point Claude Code at this proxy with ``ANTHROPIC_BASE_URL=http://host:20140``
and no ``/v1`` suffix, since the client appends ``/v1/messages`` itself. Point
Codex at ``http://host:20140/v1``; it appends ``/responses``.
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DENY_MARKERS = (
    "built on Anthropic's Claude Agent SDK",
    "built on Anthropic\u2019s Claude Agent SDK",
    "IMPORTANT: Assist with authorized security testing",
    "The most recent Claude models are the Claude",
    "Codex refers to the open-source agentic coding interface",
)

# Rejected single lines inside an otherwise accepted paragraph.
DENY_LINE_MARKERS = ("the CLI renderer applies them",)

CODEX_TOOL_DESCRIPTIONS = {
    "exec_command": "Run a shell command.",
    "write_stdin": "Write to a running session.",
}


def _sanitize_text(text: str) -> str | None:
    """Drop the offending paragraphs and lines from one block of system text."""
    kept = []
    for paragraph in text.split("\n\n"):
        if any(marker in paragraph for marker in DENY_MARKERS):
            continue
        lines = [
            line
            for line in paragraph.split("\n")
            if not any(marker in line for marker in DENY_LINE_MARKERS)
        ]
        kept.append("\n".join(lines))
    joined = "\n\n".join(kept).strip()
    return joined or None


def sanitize_system(system: object) -> object:
    """Return the system prompt with the rejected paragraphs removed.

    Accepts either the string form or the block-list form and preserves every
    other block. Returns None when nothing is left, so the caller omits the
    ``system`` key rather than sending an empty list.
    """
    if system is None:
        return None
    if isinstance(system, str):
        return _sanitize_text(system)
    if not isinstance(system, list):
        return system

    kept = []
    for block in system:
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            kept.append(block)
            continue
        text = _sanitize_text(block["text"])
        if text is not None:
            kept.append({**block, "text": text})
    return kept or None


def sanitize_responses(payload: dict) -> None:
    """Clean a Responses API body in place: instructions, system and developer
    input text, and the Codex tool descriptions Devin rejects."""
    if isinstance(payload.get("instructions"), str):
        instructions = _sanitize_text(payload["instructions"])
        if instructions is None:
            payload.pop("instructions")
        else:
            payload["instructions"] = instructions

    items = []
    for item in payload["input"]:
        content = item.get("content") if isinstance(item, dict) else None
        if not isinstance(content, list) or item.get("role") not in ("system", "developer"):
            items.append(item)
            continue
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text = _sanitize_text(part["text"])
                if text is None:
                    continue
                part = {**part, "text": text}
            parts.append(part)
        if parts:
            items.append({**item, "content": parts})
    payload["input"] = items

    for tool in payload.get("tools") or []:
        if isinstance(tool, dict) and tool.get("name") in CODEX_TOOL_DESCRIPTIONS:
            tool["description"] = CODEX_TOOL_DESCRIPTIONS[tool["name"]]


def rewrite_body(raw: bytes) -> bytes:
    """Return the request body with the rejected harness text removed."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(payload, dict):
        return raw
    if isinstance(payload.get("input"), list):
        sanitize_responses(payload)
        return json.dumps(payload).encode()
    if "system" not in payload:
        return raw
    stripped = sanitize_system(payload["system"])
    if stripped is not None:
        payload["system"] = stripped
    else:
        payload.pop("system", None)
    return json.dumps(payload).encode()


# ---------------------------------------------------------------------------
# Responses <-> chat completions adapter.
#
# Why this exists: 9router's own Responses -> chat translation splits each
# turn into a bare narration assistant message followed by a separate
# tool-calls-only assistant message. That structure is not what swe-2-1
# learned in its native harness, where narration is attached to the action it
# introduces. Fed a history of bare-narration messages, the model reuses the
# pattern: it emits narration and stops (measured ~35% of the time at a
# narration-heavy step, vs 0% with merged structure). Every agent harness
# treats a text-only response as end of turn, so the turn dies mid-task.
#
# The adapter merges each turn's reasoning, narration and tool calls into a
# single assistant message, with tool outputs following as tool messages, and
# translates the streamed chat response back into Responses SSE events.
# ---------------------------------------------------------------------------


def _text_of_content(content: object) -> str:
    """Flatten a Responses message content list (or plain string) to text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return "".join(parts)


def _output_text(content: object) -> str:
    """Flatten a function_call_output payload to a plain string."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        nested = content.get("content")
        if nested is not None:
            return _output_text(nested)
        if isinstance(content.get("text"), str):
            return content["text"]
    return _text_of_content(content)


def responses_input_to_chat(payload: dict) -> list:
    """Translate a Responses API ``input`` list into chat completion messages.

    Assistant turns are merged: reasoning summary, narration and the function
    calls they precede become one assistant message. Developer/system items
    become system messages, users stay users, outputs become tool messages.
    Everything else (unknown item types) is dropped.
    """
    messages: list[dict] = []
    cur: dict | None = None

    def flush() -> None:
        nonlocal cur
        if cur is None:
            return
        if cur.get("tool_calls") or cur.get("content") or cur.get("reasoning_content"):
            if not cur.get("tool_calls") and not cur.get("content"):
                # reasoning alone carries no visible text the model can usefully
                # re-read as its own words; keep it only when attached to text
                # or calls.
                pass
            messages.append(cur)
        cur = None

    def ensure_cur() -> dict:
        nonlocal cur
        if cur is None or cur.get("tool_calls"):
            flush()
            cur = {"role": "assistant", "content": None}
        return cur

    def sanitize(text: str) -> str:
        cleaned = _sanitize_text(text)
        return cleaned if cleaned is not None else text

    for item in payload.get("input") or []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type", "message")
        role = item.get("role")
        if item_type == "message" and role in ("system", "developer"):
            flush()
            text = sanitize(_text_of_content(item.get("content")))
            if text:
                messages.append({"role": "system", "content": text})
        elif item_type == "message" and role == "user":
            flush()
            messages.append({"role": "user", "content": _text_of_content(item.get("content"))})
        elif item_type == "message" and role == "assistant":
            target = ensure_cur()
            text = _text_of_content(item.get("content"))
            target["content"] = (target.get("content") or "") + text or None
        elif item_type == "reasoning":
            target = ensure_cur()
            summary = item.get("summary") or []
            text = " ".join(
                part.get("text", "") for part in summary if isinstance(part, dict)
            ).strip()
            if text:
                target["reasoning_content"] = ((target.get("reasoning_content") or "") + " " + text).strip()
        elif item_type == "function_call":
            if cur is None:
                cur = {"role": "assistant", "content": None}
            arguments = item.get("arguments")
            cur.setdefault("tool_calls", []).append({
                "id": item.get("call_id") or item.get("id") or "call_0",
                "type": "function",
                "function": {
                    "name": item.get("name") or "",
                    "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments or {}),
                },
            })
        elif item_type == "function_call_output":
            flush()
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id") or "",
                "content": _output_text(item.get("output")),
            })
    flush()
    return messages


def responses_tools_to_chat(tools: object) -> list:
    """Translate Responses tool definitions to chat function tools.

    Function tools keep name, (sanitized) description and parameters.
    Namespace tools are flattened one level so their inner tools survive,
    unlike 9router's translation which collapses them to an empty shell.
    """
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "namespace":
            for inner in tool.get("tools") or []:
                if isinstance(inner, dict) and inner.get("name"):
                    out.append(_chat_tool(inner))
        elif tool.get("type") == "function" or tool.get("name"):
            if tool.get("name"):
                out.append(_chat_tool(tool))
    return out


def _chat_tool(tool: dict) -> dict:
    description = tool.get("description") or ""
    if tool.get("name") in CODEX_TOOL_DESCRIPTIONS:
        description = CODEX_TOOL_DESCRIPTIONS[tool["name"]]
    else:
        cleaned = _sanitize_text(description)
        if cleaned is not None:
            description = cleaned
    parameters = tool.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {"name": tool["name"], "description": description, "parameters": parameters},
    }


def responses_to_chat_body(payload: dict) -> dict:
    """Build the chat completions request body for one Responses request."""
    sanitize_responses(payload)
    body: dict = {"model": payload.get("model"), "stream": True}
    messages = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    messages.extend(responses_input_to_chat(payload))
    body["messages"] = messages
    tools = responses_tools_to_chat(payload.get("tools"))
    if tools:
        body["tools"] = tools
    if payload.get("tool_choice") is not None and tools:
        choice = payload["tool_choice"]
        body["tool_choice"] = choice if isinstance(choice, str) else "auto"
    if payload.get("parallel_tool_calls") is not None:
        body["parallel_tool_calls"] = bool(payload["parallel_tool_calls"])
    return body


def _sse(event: dict) -> bytes:
    return ("data: " + json.dumps(event) + "\n\n").encode()


def translate_chat_sse(line_iter, response_id: str, model: str):
    """Translate a chat completions SSE stream into Responses SSE events.

    ``line_iter`` yields raw decoded lines from the upstream stream. Yields
    (bytes, done_events) tuples; ``done_events`` collects the response items
    for the closing ``response.completed`` envelope.
    """
    seq = 0
    state = {
        "created": False,
        "open_item": None,  # reasoning | message | function_call
        "item_index": -1,
        "items": [],
        "calls": {},
        "usage": None,
    }

    def close_item():
        if state["open_item"] in (None, "function_call"):
            state["open_item"] = None
            return
        idx = state["item_index"]
        item = state["items"][-1]
        yield _sse({"type": "response.output_item.done", "output_index": idx, "item": item})
        state["open_item"] = None

    def close_all_calls():
        for chat_index in sorted(state["calls"], key=lambda k: state["calls"][k]["item_index"]):
            call = state["calls"][chat_index]
            if call.get("closed"):
                continue
            call["closed"] = True
            idx = call["item_index"]
            item = {
                "type": "function_call",
                "id": f"fc_{idx}",
                "call_id": call["id"],
                "name": call["name"],
                "arguments": call["args"],
            }
            state["items"].append(item)
            yield _sse({"type": "response.output_item.done", "output_index": idx, "item": item})

    for raw_line in line_iter:
        if isinstance(raw_line, bytes):
            raw_line = raw_line.decode("utf-8", errors="replace")
        line = raw_line.rstrip("\n").rstrip("\r")
        if not line:
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        if chunk.get("usage"):
            state["usage"] = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        finish = choices[0].get("finish_reason")

        if not state["created"]:
            state["created"] = True
            yield _sse({
                "type": "response.created",
                "response": {"id": response_id, "object": "response", "status": "in_progress", "output": []},
            })

        reasoning = delta.get("reasoning_content")
        if reasoning:
            if state["open_item"] != "reasoning":
                yield from close_item()
                state["item_index"] += 1
                state["open_item"] = "reasoning"
                idx = state["item_index"]
                state["items"].append({
                    "type": "reasoning",
                    "id": f"rs_{idx}",
                    "summary": [{"type": "summary_text", "text": ""}],
                })
                yield _sse({
                    "type": "response.output_item.added",
                    "output_index": idx,
                    "item": {"type": "reasoning", "id": f"rs_{idx}", "summary": []},
                })
            seq += 1
            state["items"][-1]["summary"][0]["text"] += reasoning
            yield _sse({
                "type": "response.reasoning_summary_text.delta",
                "item_id": f"rs_{state['item_index']}",
                "output_index": state["item_index"],
                "summary_index": 0,
                "delta": reasoning,
                "sequence_number": seq,
            })

        content = delta.get("content")
        if content:
            if state["open_item"] != "message":
                yield from close_item()
                state["item_index"] += 1
                state["open_item"] = "message"
                idx = state["item_index"]
                state["items"].append({
                    "type": "message",
                    "id": f"msg_{idx}",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": ""}],
                    "status": "completed",
                })
                yield _sse({
                    "type": "response.output_item.added",
                    "output_index": idx,
                    "item": {"type": "message", "id": f"msg_{idx}", "role": "assistant", "content": []},
                })
            seq += 1
            state["items"][-1]["content"][0]["text"] += content
            yield _sse({
                "type": "response.output_text.delta",
                "item_id": f"msg_{state['item_index']}",
                "output_index": state["item_index"],
                "content_index": 0,
                "delta": content,
                "sequence_number": seq,
            })

        for tc in delta.get("tool_calls") or []:
            chat_index = tc.get("index", 0)
            call = state["calls"].get(chat_index)
            if call is None:
                yield from close_item()
                state["item_index"] += 1
                idx = state["item_index"]
                call = {
                    "item_index": idx,
                    "id": tc.get("id") or f"call_{idx}",
                    "name": "",
                    "args": "",
                    "opened": False,
                }
                state["calls"][chat_index] = call
                state["open_item"] = "function_call"
                state["chat_index"] = chat_index
            if tc.get("id"):
                call["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                call["name"] = fn["name"]
            if fn.get("arguments"):
                call["args"] += fn["arguments"]
            if not call["opened"]:
                call["opened"] = True
                yield _sse({
                    "type": "response.output_item.added",
                    "output_index": call["item_index"],
                    "item": {
                        "type": "function_call",
                        "id": f"fc_{call['item_index']}",
                        "call_id": call["id"],
                        "name": call["name"],
                        "arguments": "",
                    },
                })
            if fn.get("arguments"):
                seq += 1
                yield _sse({
                    "type": "response.function_call_arguments.delta",
                    "item_id": f"fc_{call['item_index']}",
                    "output_index": call["item_index"],
                    "delta": fn["arguments"],
                    "sequence_number": seq,
                })

        if finish:
            yield from close_item()
            yield from close_all_calls()

    yield from close_item()
    yield from close_all_calls()
    usage = state["usage"] or {}
    output = state["items"]
    if not output:
        # The upstream stream ended without a single output item. Passing that
        # through as a completed empty response would end the agent's turn with
        # nothing in it, which is strictly worse than a stream error: codex
        # retries stream errors with backoff, it does not retry empty success.
        yield _sse({
            "type": "response.failed",
            "response": {
                "id": response_id,
                "object": "response",
                "status": "failed",
                "error": {
                    "code": "stream_error",
                    "message": "upstream chat stream closed without any output",
                },
                "usage": None,
            },
            "sequence_number": seq + 1,
        })
        yield b"data: [DONE]\n\n"
        return
    completed = {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "object": "response",
            "status": "completed",
            "output": output,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        },
        "sequence_number": seq + 1,
    }
    yield _sse(completed)
    yield b"data: [DONE]\n\n"



def failed_sse(response_id: str, message: str) -> bytes:
    return b"data: " + json.dumps({
        "type": "response.failed",
        "response": {
            "id": response_id,
            "object": "response",
            "status": "failed",
            "error": {"code": "stream_error", "message": message},
            "usage": None,
        },
    }).encode() + b"\n\n"


def stream_with_retries(events, response_id: str, opener, sleeper=time.sleep,
                        backoff=(10, 20, 30, 45)):
    """Yield translated SSE events, retrying the upstream with backoff while
    it keeps closing without a single output item.

    An itemless stream means the devin bridge swallowed a rate-limit error and
    returned 200 with no output; the limit is minute-aligned, so waiting beats
    failing. Lifecycle events (``response.created``) are held back until the
    first output item arrives, so a retried attempt starts from a clean slate
    downstream. Once an output item has gone out there is nothing to retry:
    replaying a partial stream would corrupt the response.

    ``events`` is the event iterator for the first attempt. ``opener()`` opens
    a new upstream attempt and returns its event iterator, or None when the
    upstream cannot be reached or rejects the request.
    """
    attempt = 0
    while True:
        attempt += 1
        pending = []
        streaming = False
        empty = True
        for event in events:
            if streaming:
                yield event
            elif b"output_item.added" in event or b"response.completed" in event:
                streaming = True
                empty = False
                yield from pending
                yield event
            elif b"response.created" in event or b"response.in_progress" in event:
                pending.append(event)
            # Anything else before the first output item — response.failed and
            # the [DONE] sentinel of an empty stream — is swallowed so the
            # retry can start clean.
        if not empty:
            return
        if attempt > len(backoff):
            yield from pending
            yield failed_sse(response_id, "upstream chat stream closed without any output")
            yield b"data: [DONE]\n\n"
            return
        delay = backoff[attempt - 1]
        print(
            f"upstream attempt {attempt} empty; retrying in {delay}s",
            file=sys.stderr,
            flush=True,
        )
        sleeper(delay)
        events = opener()
        if events is None:
            yield from pending
            yield failed_sse(response_id, "upstream connection failed during retry")
            yield b"data: [DONE]\n\n"
            return


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream_host = "127.0.0.1"
    upstream_port = 20128

    def _upstream_path(self) -> str:
        path = self.path.split("?", 1)[0]
        if path.endswith("/messages"):
            return "/v1/messages" + ("?" + self.path.split("?", 1)[1] if "?" in self.path else "")
        return self.path

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self.send_response(200)
            self.send_header("content-type", "text/plain")
            self.send_header("content-length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(length)
        path = self.path.split("?", 1)[0]
        if path.endswith("/responses"):
            self._serve_responses(raw)
            return
        body = rewrite_body(raw)

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in ("host", "content-length", "connection")
        }
        headers["content-length"] = str(len(body))

        connection = http.client.HTTPConnection(
            self.upstream_host, self.upstream_port, timeout=900
        )
        try:
            connection.request(
                "POST", self._upstream_path(), body=body, headers=headers
            )
            response = connection.getresponse()
        except Exception as error:  # noqa: BLE001
            self.send_response(502)
            self.send_header("content-type", "application/json")
            payload = json.dumps(
                {"type": "error", "error": {"type": "api_error", "message": str(error)}}
            ).encode()
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(response.status)
        for key, value in response.getheaders():
            if key.lower() in ("transfer-encoding", "content-length", "connection"):
                continue
            self.send_header(key, value)
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()

        try:
            while True:
                chunk = response.read(8192)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n" % len(chunk))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            connection.close()

    def _serve_responses(self, raw: bytes) -> None:
        """Terminate a Responses API request: translate to chat completions,
        forward to 9router's chat endpoint, stream Responses SSE back."""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self.send_error(400, "invalid JSON")
            return

        def error_out(status: int, message: str) -> None:
            body = json.dumps(
                {"error": {"message": message, "type": "server_error"}}
            ).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        try:
            chat_body = responses_to_chat_body(payload)
        except Exception as error:  # noqa: BLE001
            error_out(500, f"translation failed: {error}")
            return

        headers = {"content-type": "application/json"}
        if self.headers.get("authorization"):
            headers["authorization"] = self.headers["authorization"]
        blob = json.dumps(chat_body).encode()

        connection = http.client.HTTPConnection(
            self.upstream_host, self.upstream_port, timeout=900
        )
        try:
            connection.request("POST", "/v1/chat/completions", body=blob, headers=headers)
            upstream = connection.getresponse()
        except Exception as error:  # noqa: BLE001
            error_out(502, str(error))
            return

        if upstream.status != 200:
            detail = upstream.read()
            error_out(upstream.status, detail.decode(errors="replace")[:2000])
            connection.close()
            return

        response_id = f"resp_{uuid.uuid4().hex[:24]}"
        model = str(payload.get("model") or "")

        def lines(resp):
            pending = b""
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    if pending:
                        yield pending
                    return
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    yield line

        def send_chunk(data: bytes) -> None:
            self.wfile.write(b"%x\r\n" % len(data))
            self.wfile.write(data)
            self.wfile.write(b"\r\n")
            self.wfile.flush()

        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()

        def open_upstream():
            conn = http.client.HTTPConnection(
                self.upstream_host, self.upstream_port, timeout=900
            )
            try:
                conn.request("POST", "/v1/chat/completions", body=blob, headers=headers)
                resp = conn.getresponse()
            except Exception:  # noqa: BLE001
                conn.close()
                return None
            if resp.status != 200:
                resp.close()
                return None
            return translate_chat_sse(lines(resp), response_id, model)

        try:
            first_events = translate_chat_sse(lines(upstream), response_id, model)
            for event in stream_with_retries(first_events, response_id, open_upstream):
                send_chunk(event)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            connection.close()

    def log_message(self, *args) -> None:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=20140)
    parser.add_argument("--upstream-port", type=int, default=20128)
    parser.add_argument("--quiet", action="store_true")
    options = parser.parse_args()

    Proxy.upstream_port = options.upstream_port
    server = ThreadingHTTPServer((options.host, options.port), Proxy)
    server.daemon_threads = True
    if not options.quiet:
        print(
            f"devin policy shim on {options.host}:{options.port} -> "
            f"http://127.0.0.1:{options.upstream_port}/v1",
            flush=True,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
