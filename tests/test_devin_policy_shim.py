from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SHIM = Path(__file__).resolve().parent.parent / "scripts" / "claude_identity_shim.py"
spec = importlib.util.spec_from_file_location("claude_identity_shim", SHIM)
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


if __name__ == "__main__":
    unittest.main()
