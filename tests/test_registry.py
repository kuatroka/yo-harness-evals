"""The registry is the one place a model name exists. These tests hold it there."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))

import lib  # noqa: E402


class ModelRegistrySchemaTest(unittest.TestCase):
    def setUp(self):
        self.models = lib.load_config("models")

    def test_the_registry_has_no_schema_problems(self):
        self.assertEqual(lib.validate_models(self.models), [])

    def test_every_key_is_its_own_id(self):
        for key, spec in self.models.items():
            with self.subTest(key=key):
                self.assertEqual(key, lib.model_id(spec))

    def test_every_transport_is_known(self):
        for key, spec in self.models.items():
            with self.subTest(key=key):
                self.assertIn(spec["transport"], lib.TRANSPORTS)

    def test_every_harness_is_known(self):
        for key, spec in self.models.items():
            for harness in spec.get("harnesses") or ():
                with self.subTest(key=key, harness=harness):
                    self.assertIn(harness, lib.HARNESSES)

    def test_a_route_and_effort_pair_belongs_to_one_id(self):
        seen: dict[tuple[str, str], str] = {}
        for key, spec in self.models.items():
            pair = (spec["route"], spec["effort"])
            with self.subTest(key=key):
                self.assertNotIn(pair, seen)
            seen[pair] = key

    def test_the_slug_is_safe_for_a_job_folder(self):
        for key in self.models:
            with self.subTest(key=key):
                self.assertNotIn("/", lib.slug(key))
                self.assertEqual(lib.slug(key), key.replace("/", "-"))

    def test_a_drifted_id_is_reported(self):
        spec = {
            "transport": "carrier",
            "endpoint": "dv-bridge",
            "model": "swe-2",
            "effort": "max",
            "base": "swe-2",
            "harnesses": ["droid"],
            "provider": "devin",
            "route": "devin/swe-2-max",
        }
        problems = lib.validate_models({"swe-2-max": spec})
        self.assertTrue(any("id should be" in problem for problem in problems))
        self.assertTrue(any("unknown transport" in problem for problem in problems))


if __name__ == "__main__":
    unittest.main()
