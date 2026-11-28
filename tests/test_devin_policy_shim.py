from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

SHIM = Path(__file__).resolve().parent.parent / "scripts" / "devin_policy_shim.py"
spec = importlib.util.spec_from_file_location("devin_policy_shim", SHIM)
assert spec and spec.loader
shim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shim)

IDENTITY = {
    "type": "text",
    "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK.",
}
BILLING = {"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.319;"}
SAFETY = (
    "IMPORTANT: Assist with authorized security testing, defensive security, "
    "CTF challenges. Refuse requests for DoS attacks and C2 frameworks."
)
VENDOR = "The most recent Claude models are the Claude 5 family. Model IDs."
NEUTRAL = "You are an agent working with the user toward their goals."


class SanitizeSystemTests(unittest.TestCase):
    def test_identity_block_is_removed_and_billing_kept(self) -> None:
        self.assertEqual(shim.sanitize_system([BILLING, IDENTITY]), [BILLING])

    def test_safety_paragraph_is_removed_from_a_block(self) -> None:
        block = {"type": "text", "text": f"{NEUTRAL}\n\n{SAFETY}\n\nMore rules."}
        result = shim.sanitize_system([block])
        assert isinstance(result, list)
        text = result[0]["text"]
        self.assertIn(NEUTRAL, text)
        self.assertIn("More rules.", text)
        self.assertNotIn("authorized security testing", text)

    def test_vendor_paragraph_is_removed(self) -> None:
        block = {"type": "text", "text": f"{NEUTRAL}\n\n{VENDOR}"}
        result = shim.sanitize_system([block])
        assert isinstance(result, list)
        self.assertEqual(result[0]["text"], NEUTRAL)

    def test_list_of_only_rejected_text_becomes_none(self) -> None:
        self.assertIsNone(shim.sanitize_system([IDENTITY]))

    def test_plain_string_identity_is_dropped(self) -> None:
        self.assertIsNone(shim.sanitize_system(IDENTITY["text"]))

    def test_plain_string_without_markers_is_kept(self) -> None:
        self.assertEqual(shim.sanitize_system(NEUTRAL), NEUTRAL)

    def test_curly_apostrophe_variant_is_dropped(self) -> None:
        curly = "You are a Claude agent, built on Anthropic\u2019s Claude Agent SDK."
        self.assertIsNone(shim.sanitize_system([{"type": "text", "text": curly}]))

    def test_missing_system_stays_missing(self) -> None:
        self.assertIsNone(shim.sanitize_system(None))


class RewriteBodyTests(unittest.TestCase):
    def test_body_without_system_is_unchanged(self) -> None:
        raw = b'{"model":"devin/swe-2-max","messages":[]}'
        self.assertEqual(shim.rewrite_body(raw), raw)

    def test_body_with_only_rejected_system_loses_the_key(self) -> None:
        raw = (
            b'{"model":"devin/swe-2-max","system":[{"type":"text","text":'
            b'"You are a Claude agent, built on Anthropic\'s Claude Agent SDK."}]}'
        )
        rewritten = shim.rewrite_body(raw)
        self.assertNotIn(b"system", rewritten)
        self.assertIn(b"devin/swe-2-max", rewritten)

    def test_invalid_json_passes_through(self) -> None:
        self.assertEqual(shim.rewrite_body(b"not json"), b"not json")


CODEX_IDENTITY = (
    "Within this context, Codex refers to the open-source agentic coding "
    "interface (not the old Codex language model built by OpenAI)."
)
CODEX_DONT = (
    "- Don’t nest bullets or create deep hierarchies.\n"
    "- Don’t output ANSI escape codes directly — the CLI renderer applies them.\n"
    "- Don’t cram unrelated keywords into a single bullet; split for clarity."
)
EXEC_PARAMS = {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}


def codex_body() -> dict:
    return {
        "model": "devin/swe-2-1-max",
        "input": [
            {"type": "message", "role": "developer", "content": [
                {"type": "input_text", "text": f"{NEUTRAL}\n\n{CODEX_IDENTITY}\n\n{CODEX_DONT}"},
            ]},
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": f"Explain this line: {CODEX_IDENTITY}"},
            ]},
        ],
        "tools": [
            {"type": "function", "name": "exec_command",
             "description": "Runs a command in a PTY, returning output or a session ID for ongoing interaction.",
             "parameters": EXEC_PARAMS},
            {"type": "function", "name": "view_image",
             "description": "View a local image file from the filesystem.", "parameters": {}},
        ],
    }


class ResponsesBodyTests(unittest.TestCase):
    def rewrite(self, body: dict) -> dict:
        return json.loads(shim.rewrite_body(json.dumps(body).encode()))

    def test_developer_text_loses_only_the_rejected_paragraph_and_line(self) -> None:
        text = self.rewrite(codex_body())["input"][0]["content"][0]["text"]
        self.assertEqual(
            text,
            f"{NEUTRAL}\n\n- Don’t nest bullets or create deep hierarchies.\n"
            "- Don’t cram unrelated keywords into a single bullet; split for clarity.",
        )

    def test_user_text_is_never_rewritten(self) -> None:
        body = codex_body()
        self.assertEqual(self.rewrite(body)["input"][1], body["input"][1])

    def test_exec_description_is_replaced_and_schema_kept(self) -> None:
        exec_tool, image_tool = self.rewrite(codex_body())["tools"]
        self.assertEqual(exec_tool["description"], "Run a shell command.")
        self.assertEqual(exec_tool["parameters"], EXEC_PARAMS)
        self.assertEqual(image_tool, codex_body()["tools"][1])

    def test_developer_message_of_only_rejected_text_is_dropped(self) -> None:
        body = codex_body()
        body["input"][0]["content"][0]["text"] = CODEX_IDENTITY
        self.assertEqual([m["role"] for m in self.rewrite(body)["input"]], ["user"])

    def test_instructions_field_is_cleaned(self) -> None:
        body = {"model": "m", "input": [], "instructions": f"{NEUTRAL}\n\n{CODEX_IDENTITY}"}
        self.assertEqual(self.rewrite(body)["instructions"], NEUTRAL)


def agentic_input() -> list:
    """A Responses input shaped like a real codex agentic history."""
    return [
        {"type": "message", "role": "developer",
         "content": [{"type": "input_text", "text": NEUTRAL}]},
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "fix the bug"}]},
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "I should look first."}]},
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "Let me look."}]},
        {"type": "function_call", "name": "exec_command", "arguments": '{"cmd":"ls"}', "call_id": "c1"},
        {"type": "function_call_output", "call_id": "c1", "output": "file.py"},
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Found it."}]},
        {"type": "function_call", "name": "exec_command", "arguments": '{"cmd":"cat file.py"}', "call_id": "c2"},
        {"type": "function_call_output", "call_id": "c2", "output": "print(1)"},
    ]


class ResponsesToChatTests(unittest.TestCase):
    """The adapter merges each turn's narration, reasoning and tool calls into
    one assistant message: 9router's split shape (bare narration followed by a
    separate tool-calls-only message) is what makes swe-2-1 emit narration-only
    turns that die. These tests pin the merged structure."""

    def chat(self, items):
        payload = {"model": "m", "input": items, "tools": []}
        return shim.responses_to_chat_body(payload)["messages"]

    def test_narration_merges_with_its_tool_call(self) -> None:
        msgs = self.chat(agentic_input())
        first = next(m for m in msgs if m["role"] == "assistant")
        self.assertEqual(first["content"], "Let me look.")
        self.assertEqual(first["reasoning_content"], "I should look first.")
        self.assertEqual(len(first["tool_calls"]), 1)
        self.assertEqual(first["tool_calls"][0]["id"], "c1")
        self.assertEqual(first["tool_calls"][0]["function"]["arguments"], '{"cmd":"ls"}')

    def test_no_bare_narration_messages(self) -> None:
        msgs = self.chat(agentic_input())
        bare = [
            m for m in msgs
            if m["role"] == "assistant" and not m.get("tool_calls") and m.get("content")
        ]
        self.assertEqual(bare, [])

    def test_tool_outputs_follow_their_call(self) -> None:
        msgs = self.chat(agentic_input())
        kinds = [(m["role"], bool(m.get("tool_calls"))) for m in msgs]
        self.assertEqual(
            kinds,
            [("system", False), ("user", False), ("assistant", True), ("tool", False),
             ("assistant", True), ("tool", False)],
        )
        self.assertEqual(msgs[3]["tool_call_id"], "c1")
        self.assertEqual(msgs[3]["content"], "file.py")

    def test_bare_call_keeps_no_reasoning_confusion(self) -> None:
        msgs = self.chat(agentic_input())
        second = msgs[4]
        self.assertEqual(second["reasoning_content"], "Found it.")
        self.assertIsNone(second["content"])
        self.assertEqual(len(second["tool_calls"]), 1)

    def test_reasoning_without_text_is_not_invented(self) -> None:
        items = [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]},
            {"type": "reasoning", "summary": []},
            {"type": "function_call", "name": "exec_command", "arguments": "{}", "call_id": "c1"},
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        ]
        msgs = self.chat(items)
        self.assertNotIn("reasoning_content", msgs[1])

    def test_final_answer_message_is_preserved(self) -> None:
        items = agentic_input() + [
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "All fixed."}]},
        ]
        msgs = self.chat(items)
        self.assertEqual(msgs[-1]["content"], "All fixed.")
        self.assertNotIn("tool_calls", msgs[-1])

    def test_structured_output_is_flattened(self) -> None:
        items = [
            {"type": "function_call_output", "call_id": "c1",
             "output": [{"type": "input_text", "text": "part"}]},
        ]
        msgs = self.chat(items)
        self.assertEqual(msgs[0]["content"], "part")

    def test_namespace_tools_are_flattened_not_collapsed(self) -> None:
        tools = [
            {"type": "function", "name": "exec_command", "description": "d",
             "parameters": EXEC_PARAMS},
            {"type": "namespace", "name": "multi_agent_v1", "description": "agents",
             "tools": [{"type": "function", "name": "spawn_agent", "description": "s",
                        "parameters": EXEC_PARAMS}]},
        ]
        chat = shim.responses_tools_to_chat(tools)
        names = [t["function"]["name"] for t in chat]
        self.assertEqual(names, ["exec_command", "spawn_agent"])
        self.assertEqual(chat[1]["function"]["parameters"], EXEC_PARAMS)

    def test_exec_description_is_sanitized_in_chat_tools(self) -> None:
        tools = [{"type": "function", "name": "exec_command", "description": "long original",
                  "parameters": EXEC_PARAMS}]
        chat = shim.responses_tools_to_chat(tools)
        self.assertEqual(chat[0]["function"]["description"], "Run a shell command.")

    def test_instructions_become_first_system_message(self) -> None:
        payload = {"model": "m", "instructions": "be brief", "input": agentic_input(), "tools": []}
        msgs = shim.responses_to_chat_body(payload)["messages"]
        self.assertEqual(msgs[0], {"role": "system", "content": "be brief"})


def chat_sse_lines(chunks):
    return [("data: " + json.dumps(c)).encode() for c in chunks] + [b"data: [DONE]"]


def parse_sse(events):
    out = []
    for raw in events:
        if raw.startswith(b"data: {"):
            out.append(json.loads(raw.decode().split("data: ", 1)[1]))
    return out


class ChatToResponsesSseTests(unittest.TestCase):
    def test_reasoning_message_call_roundtrip(self) -> None:
        chunks = [
            {"choices": [{"delta": {"reasoning_content": "think "}, "finish_reason": None}]},
            {"choices": [{"delta": {"reasoning_content": "more"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "Doing it."}, "finish_reason": None}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "exec_command", "arguments": ""}}]}, "finish_reason": None}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '{"cmd":"ls"}'}}]}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
        ]
        events = parse_sse(shim.translate_chat_sse(iter(chat_sse_lines(chunks)), "resp_t", "m"))
        completed = next(e for e in events if e["type"] == "response.completed")
        output = completed["response"]["output"]
        self.assertEqual([i["type"] for i in output], ["reasoning", "message", "function_call"])
        self.assertEqual(output[0]["summary"][0]["text"], "think more")
        self.assertEqual(output[1]["content"][0]["text"], "Doing it.")
        self.assertEqual(output[2]["arguments"], '{"cmd":"ls"}')
        self.assertEqual(output[2]["call_id"], "call_1")
        self.assertEqual(completed["response"]["usage"]["input_tokens"], 10)
        dones = [e["item"]["type"] for e in events if e["type"] == "response.output_item.done"]
        self.assertEqual(dones, ["reasoning", "message", "function_call"])

    def test_empty_stream_fails_instead_of_completing_empty(self) -> None:
        # An upstream stream with no output items must surface as a failure:
        # codex retries stream errors, but treats a completed empty response as
        # a finished turn, which kills the trial.
        chunks = [
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        events = parse_sse(shim.translate_chat_sse(iter(chat_sse_lines(chunks)), "resp_t", "m"))
        kinds = [e["type"] for e in events]
        self.assertIn("response.failed", kinds)
        self.assertNotIn("response.completed", kinds)

    def test_text_only_response_has_no_function_call(self) -> None:
        chunks = [
            {"choices": [{"delta": {"content": "Final answer."}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        events = parse_sse(shim.translate_chat_sse(iter(chat_sse_lines(chunks)), "resp_t", "m"))
        completed = next(e for e in events if e["type"] == "response.completed")
        kinds = [i["type"] for i in completed["response"]["output"]]
        self.assertEqual(kinds, ["message"])
        self.assertNotIn("function_call", kinds)

    def test_parallel_calls_stay_separate(self) -> None:
        chunks = [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c0", "function": {"name": "a", "arguments": '{"x"'}}]}, "finish_reason": None}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 1, "id": "c1", "function": {"name": "b", "arguments": '{"y"'}}]}, "finish_reason": None}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": ":1}"}}]}, "finish_reason": None}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 1, "function": {"arguments": ":2}"}}]}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        events = parse_sse(shim.translate_chat_sse(iter(chat_sse_lines(chunks)), "resp_t", "m"))
        completed = next(e for e in events if e["type"] == "response.completed")
        calls = [i for i in completed["response"]["output"] if i["type"] == "function_call"]
        self.assertEqual(len(calls), 2)
        args = sorted(c["arguments"] for c in calls)
        self.assertEqual(args, ['{"x":1}', '{"y":2}'])


def sse_of(kind: str) -> bytes:
    return b'data: {"type": "' + kind.encode() + b'"}\n\n'


EMPTY_STREAM = [sse_of("response.created"), sse_of("response.failed"), b"data: [DONE]\n\n"]
CONTENT_STREAM = [
    sse_of("response.created"),
    sse_of("response.output_item.added"),
    sse_of("response.output_text.delta"),
    sse_of("response.completed"),
    b"data: [DONE]\n\n",
]


class StreamWithRetriesTests(unittest.TestCase):
    def run_stream(self, attempts, backoff=(10, 20, 30, 45)):
        """Feed the given per-attempt event lists through stream_with_retries
        with a recording fake opener and sleeper."""
        sleeps = []
        opened = []
        state = {"next": 1}

        def opener():
            if state["next"] >= len(attempts):
                return None
            events = attempts[state["next"]]
            state["next"] += 1
            opened.append(True)
            return iter(events)

        out = list(shim.stream_with_retries(
            iter(attempts[0]), "resp_t", opener,
            sleeper=sleeps.append, backoff=backoff,
        ))
        return out, sleeps, len(opened)

    def test_empty_then_content_retries_once_and_streams_clean(self) -> None:
        out, sleeps, opened = self.run_stream([EMPTY_STREAM, CONTENT_STREAM])
        self.assertEqual(sleeps, [10])
        self.assertEqual(opened, 1)
        kinds = [e for e in out]
        # Exactly one response.created, no response.failed: the retried stream
        # reaches the client as if the first attempt never happened.
        self.assertEqual(kinds.count(sse_of("response.created")), 1)
        self.assertNotIn(sse_of("response.failed"), kinds)
        self.assertIn(sse_of("response.completed"), kinds)

    def test_content_first_attempt_never_retries(self) -> None:
        out, sleeps, opened = self.run_stream([CONTENT_STREAM, EMPTY_STREAM])
        self.assertEqual(sleeps, [])
        self.assertEqual(opened, 0)
        self.assertEqual(out, CONTENT_STREAM)

    def test_persistent_empties_fail_after_backoff_is_exhausted(self) -> None:
        attempts = [list(EMPTY_STREAM) for _ in range(6)]
        out, sleeps, opened = self.run_stream(attempts)
        # 1 initial + 4 retries, then the 5th empty stream is terminal.
        self.assertEqual(sleeps, [10, 20, 30, 45])
        kinds = [json.loads(e[5:].split(b"\n")[0])["type"] for e in out if e.startswith(b"data: {")]
        self.assertEqual(kinds, ["response.created", "response.failed"])
        self.assertEqual(out[-1], b"data: [DONE]\n\n")

    def test_failed_opener_surfaces_failure(self) -> None:
        sleeps = []

        def opener():
            return None

        out = list(shim.stream_with_retries(
            iter(EMPTY_STREAM), "resp_t", opener, sleeper=sleeps.append,
        ))
        self.assertEqual(sleeps, [10])
        kinds = [json.loads(e[5:].split(b"\n")[0])["type"] for e in out if e.startswith(b"data: {")]
        self.assertIn("response.failed", kinds)
        self.assertNotIn("response.completed", kinds)

    def test_mid_stream_items_are_never_retried(self) -> None:
        # Once an output item went downstream the attempt must stream through
        # even if it ends in failure: replaying it would duplicate content.
        partial = [
            sse_of("response.created"),
            sse_of("response.output_item.added"),
            sse_of("response.failed"),
            b"data: [DONE]\n\n",
        ]
        out, sleeps, opened = self.run_stream([partial, CONTENT_STREAM])
        self.assertEqual(sleeps, [])
        self.assertEqual(opened, 0)
        self.assertEqual(out, partial)


if __name__ == "__main__":
    unittest.main()
