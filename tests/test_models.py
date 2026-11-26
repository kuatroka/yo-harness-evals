import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))

from harbor.agents.installed.base import ApiUsageLimitError
from harbor.models.agent.context import AgentContext

from harnesses.amp import AmpAgent, AmpModel
from harnesses.claude import ClaudeCodeAgent
from harnesses.droid import DroidAgent, DroidModel


class DroidModelTableTest(unittest.TestCase):
    def test_new_router_models_are_registered(self):
        self.assertEqual(
            DroidAgent.MODEL_IDS["deepseek-engy-max"],
            "custom:engy/deepseek-v4.1-flash-1017(max)",
        )
        self.assertEqual(
            DroidAgent.MODEL_IDS["haiku-devin-max"], "custom:devin/haiku-5-5-max"
        )
        self.assertEqual(
            DroidAgent.MODEL_IDS["swe-2-1-xhigh"], "custom:devin/swe-2-1-xhigh"
        )
        self.assertEqual(
            DroidAgent.MODEL_IDS["swe-2-1-max"], "custom:devin/swe-2-1-max"
        )

    def test_every_literal_key_has_an_id(self):
        for key in DroidModel.__args__:
            with self.subTest(key=key):
                self.assertIn(key, DroidAgent.MODEL_IDS)
                route = DroidAgent.MODEL_IDS[key]
                self.assertTrue(route.startswith("custom:"))

    def test_xhigh_is_a_valid_effort(self):
        with tempfile.TemporaryDirectory() as logs:
            agent = DroidAgent(
                logs_dir=Path(logs),
                model_name="devin/swe-2-1-xhigh",
                effort="xhigh",
                model="swe-2-1-xhigh",
            )
        self.assertEqual(agent.effort, "xhigh")
        self.assertEqual(agent.model, "swe-2-1-xhigh")

    def test_swe_2_1_rows_resolve_through_the_registry(self):
        import lib
        models = lib.load_config("models")
        self.assertEqual(
            lib.resolve_model("custom:devin/swe-2-1-xhigh", models), "swe-2-1-xhigh"
        )
        self.assertEqual(
            lib.resolve_model("custom:devin/swe-2-1-max", models), "swe-2-1-max"
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
    def test_new_router_models_are_registered(self):
        self.assertEqual(
            AmpAgent.MODE_SPECS["deepseek-engy-max"]["model"],
            "engy/deepseek-v4.1-flash-1017(max)",
        )
        self.assertEqual(
            AmpAgent.MODE_SPECS["haiku-devin-max"]["model"], "devin/haiku-5-5-max"
        )
        self.assertEqual(
            AmpAgent.MODE_SPECS["swe-2-1-xhigh"]["model"], "devin/swe-2-1-xhigh"
        )
        self.assertEqual(
            AmpAgent.MODE_SPECS["swe-2-1-xhigh"]["effort"], "xhigh"
        )
        self.assertEqual(
            AmpAgent.MODE_SPECS["swe-2-1-max"]["model"], "devin/swe-2-1-max"
        )

    def test_every_literal_key_has_a_mode(self):
        for key in AmpModel.__args__:
            with self.subTest(key=key):
                self.assertIn(key, AmpAgent.MODE_SPECS)

    def test_plugin_registers_every_mode(self):
        """The uploaded plugin names every mode by its registry key."""
        plugin = AmpAgent.plugin_source()
        for key, spec in AmpAgent.MODE_SPECS.items():
            with self.subTest(key=key):
                self.assertIn(f"key: 'fh-{key}'", plugin)
                self.assertIn(f"label: '{key}'", plugin)
                self.assertIn(f"model: '{spec['model']}'", plugin)
                self.assertIn(
                    f"reasoningEffort: '{spec['effort']}'", plugin
                )
                self.assertIn(f'"key":"fh-{key}"', plugin)
                self.assertIn(f'"label":"{key}"', plugin)


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
