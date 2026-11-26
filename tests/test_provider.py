from __future__ import annotations

import logging
import tempfile
import unittest

from harbor.models.trial.result import AgentInfo, ModelInfo

from harnesses.common import provider_from_model_name


class ProviderFromModelNameTests(unittest.TestCase):
    def test_provider_is_the_prefix(self) -> None:
        self.assertEqual(provider_from_model_name("devin/swe-2-max"), "devin")

    def test_model_name_without_prefix_has_no_provider(self) -> None:
        self.assertIsNone(provider_from_model_name("swe-2-max"))

    def test_missing_model_name_has_no_provider(self) -> None:
        self.assertIsNone(provider_from_model_name(None))

    def test_trailing_slash_has_no_provider(self) -> None:
        self.assertIsNone(provider_from_model_name("devin/"))


class ToAgentInfoTests(unittest.TestCase):
    """Harbor labels these agents harbor-skip-parsing; the provider must be real."""

    def test_to_agent_info_replaces_skip_parsing_provider(self) -> None:
        from harnesses.amp import AmpAgent
        from harnesses.claude import ClaudeCodeAgent
        from harnesses.droid import DroidAgent

        for cls in (AmpAgent, DroidAgent, ClaudeCodeAgent):
            with self.subTest(agent=cls.__name__):
                with tempfile.TemporaryDirectory() as logs_dir:
                    agent = cls(
                        model_name="devin/swe-2-max",
                        logs_dir=logs_dir,
                        logger=logging.getLogger(cls.__name__),
                    )
                    info = agent.to_agent_info()
                self.assertIsInstance(info, AgentInfo)
                self.assertIsInstance(info.model_info, ModelInfo)
                assert info.model_info is not None
                self.assertEqual(info.model_info.provider, "devin")
                self.assertEqual(info.model_info.name, "devin/swe-2-max")


if __name__ == "__main__":
    unittest.main()
