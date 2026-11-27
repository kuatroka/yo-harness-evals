import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))

from harbor.agents.installed.base import ApiUsageLimitError
from harbor.models.agent.context import AgentContext

from harnesses.amp import AmpAgent
from harnesses.claude import ClaudeCodeAgent
from harnesses.droid import DroidAgent


class DroidModelTableTest(unittest.TestCase):
    def test_every_effort_has_a_default_route(self):
        for effort, route in DroidAgent.DEFAULT_MODEL_BY_EFFORT.items():
            with self.subTest(effort=effort):
                self.assertIn("/", route)

    def test_xhigh_is_a_valid_effort(self):
        with tempfile.TemporaryDirectory() as logs:
            agent = DroidAgent(
                logs_dir=Path(logs),
                model_name="devin/swe-2-1-xhigh",
                effort="xhigh",
                model="devin/swe-2-1-xhigh",
            )
        self.assertEqual(agent.effort, "xhigh")
        self.assertEqual(agent.model, "devin/swe-2-1-xhigh")

    def test_an_old_style_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as logs, self.assertRaises(ValueError):
            DroidAgent(
                logs_dir=Path(logs),
                model_name="devin/swe-2-1-max",
                model="swe-2-1-max",
            )

    def test_the_default_route_follows_the_effort(self):
        with tempfile.TemporaryDirectory() as logs:
            agent = DroidAgent(
                logs_dir=Path(logs),
                model_name="devin/deepseek-v4.1-flash-max",
                effort="max",
            )
        self.assertEqual(agent.model, "devin/deepseek-v4.1-flash-max")

    def test_swe_2_1_rows_resolve_through_the_registry(self):
        import lib

        models = lib.load_config("models")
        self.assertEqual(
            lib.resolve_model("custom:devin/swe-2-1-xhigh", models),
            "9r/dv-bridge/swe-2-1-xhigh",
        )
        self.assertEqual(
            lib.resolve_model("custom:devin/swe-2-1-max", models),
            "9r/dv-bridge/swe-2-1-max",
        )




class ClaudeCostClearTest(unittest.TestCase):
    """A bridged route carries no agent-side cost; list_cost prices it."""

    def _context(self):
        from harbor.models.agent.context import AgentContext
        c = AgentContext()
        c.cost_usd = 2.11
        c.metadata = {}
        return c

    def test_non_anthropic_route_clears_cost(self):
        from harnesses.claude import ClaudeCodeAgent
        import tempfile
        with tempfile.TemporaryDirectory() as logs:
            agent = ClaudeCodeAgent(
                logs_dir=Path(logs), model_name="devin/swe-2-max"
            )
            c = self._context()
            agent.populate_context_post_run(c)
            self.assertIsNone(c.cost_usd)

    def test_anthropic_route_keeps_cost(self):
        from harnesses.claude import ClaudeCodeAgent
        import tempfile
        with tempfile.TemporaryDirectory() as logs:
            agent = ClaudeCodeAgent(
                logs_dir=Path(logs), model_name="claude-haiku-5-5"
            )
            c = self._context()
            agent.populate_context_post_run(c)
            self.assertEqual(c.cost_usd, 2.11)


class ProviderQuotaPatternTest(unittest.TestCase):
    """Devin's 503 wrapper hides its quota 502; Amp exits zero on it anyway."""

    line = (
        '{"type":"result","subtype":"error_during_execution","is_error":true,'
        '"error":"503 [openai-compatible-chat-x/claude-haiku-5-5-max] [502]: '
        '{\\"error\\":{\\"message\\":\\"Devin stream error failed_precondition: '
        'Your weekly usage quota has been exhausted.\\"}}"}'
    )

    def test_amp_records_it_as_a_usage_limit(self):
        hits = [
            pattern.exception
            for pattern in AmpAgent.ERROR_PATTERNS
            if re.search(pattern.pattern, self.line, re.IGNORECASE)
        ]
        self.assertIn(ApiUsageLimitError, hits)

    def test_droid_records_it_as_a_usage_limit(self):
        hits = [
            pattern.exception
            for pattern in DroidAgent.ERROR_PATTERNS
            if re.search(pattern.pattern, self.line, re.IGNORECASE)
        ]
        self.assertIn(ApiUsageLimitError, hits)


class AmpModelTableTest(unittest.TestCase):
    def test_every_effort_has_a_default_route(self):
        for effort, route in AmpAgent.DEFAULT_MODEL_BY_EFFORT.items():
            with self.subTest(effort=effort):
                self.assertIn("/", route)

    def test_a_route_is_kept_beside_the_registry_id(self):
        with tempfile.TemporaryDirectory() as logs:
            agent = AmpAgent(
                logs_dir=Path(logs),
                model_name="devin/swe-2-1-max",
                effort="max",
                model="devin/swe-2-1-max",
                model_id="9r/dv-bridge/swe-2-1-max",
            )
        self.assertEqual(agent.model, "devin/swe-2-1-max")
        self.assertEqual(agent.model_id, "9r/dv-bridge/swe-2-1-max")

    def test_an_old_style_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as logs, self.assertRaises(ValueError):
            AmpAgent(
                logs_dir=Path(logs),
                model_name="devin/swe-2-1-max",
                model="swe-2-1-max",
            )

    def test_medium_effort_accepts_opus(self):
        with tempfile.TemporaryDirectory() as logs:
            agent = AmpAgent(
                logs_dir=Path(logs),
                model_name="anthropic/claude-opus-5-5",
                effort="medium",
                model="anthropic/claude-opus-5-5",
                model_id="native/amp/opus-5-5-medium",
            )
        self.assertEqual(agent.effort, "medium")
        self.assertEqual(agent.model, "anthropic/claude-opus-5-5")


class OpusRegistryTest(unittest.TestCase):
    def test_opus_medium_rows_route_to_native_anthropic(self):
        import lib

        models = lib.load_config("models")
        self.assertEqual(
            lib.resolve_model("anthropic/claude-opus-5-5", models, "medium"),
            "native/amp/opus-5-5-medium",
        )
        self.assertEqual(
            lib.resolve_model("claude-opus-5-5", models, "medium"),
            "native/claude/opus-5-5-medium",
        )


class ClaudeMetadataTest(unittest.TestCase):
    def test_effort_is_recorded_beside_the_model_name(self):
        """The jobs table shows the model only, so the effort needs its own field."""
        with tempfile.TemporaryDirectory() as logs:
            agent = ClaudeCodeAgent(
                logs_dir=Path(logs),
                model_name="claude-haiku-5-5",
                reasoning_effort="max",
            )
            context = AgentContext()
            agent.populate_context_post_run(context)
        self.assertEqual(context.metadata["effort"], "max")
        self.assertEqual(context.metadata["model"], "claude-haiku-5-5")


if __name__ == "__main__":
    unittest.main()
