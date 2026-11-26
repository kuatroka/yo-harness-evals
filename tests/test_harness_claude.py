from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from harnesses.claude import ClaudeCodeAgent


def build_agent(logs: Path) -> ClaudeCodeAgent:
    return ClaudeCodeAgent(
        logs,
        model_name="engy/deepseek-v4.1-flash-1017(max)",
        skip_model_parsing=True,
    )


class SessionSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.logs = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_stream(self, session_id: str | None) -> None:
        event = '{"type":"system","subtype":"init","cwd":"/app/src"'
        if session_id:
            event += f',"session_id":"{session_id}"'
        (self.logs / "claude-code.txt").write_text(event + "}\n")

    def write_session(self, slug: str, session_id: str, lines: int) -> Path:
        directory = self.logs / "sessions" / "projects" / slug
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{session_id}.jsonl").write_text("{}\n" * lines)
        return directory

    def test_launched_session_wins_when_a_nested_cli_wrote_another(self) -> None:
        self.write_stream("primary")
        primary = self.write_session("-app-src", "primary", 50)
        self.write_session("-tmp-enc3", "stray", 2)

        self.assertEqual(build_agent(self.logs)._get_session_dir(), primary)

    def test_two_sessions_without_an_announced_id_stay_unresolved(self) -> None:
        self.write_stream(None)
        self.write_session("-app-src", "primary", 50)
        self.write_session("-tmp-enc3", "stray", 2)

        self.assertIsNone(build_agent(self.logs)._get_session_dir())

    def test_single_session_needs_no_announced_id(self) -> None:
        self.write_stream(None)
        only = self.write_session("-app-src", "primary", 5)

        self.assertEqual(build_agent(self.logs)._get_session_dir(), only)
