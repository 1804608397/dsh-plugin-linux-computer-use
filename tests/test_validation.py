"""Arguments that must be rejected, and the wording users will read."""

from __future__ import annotations

import unittest

from helpers import Server


class ValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server()
        cls.server.initialize()
        cls.tools = cls.server.tools()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def test_region_requires_positive_size(self):
        text = self.server.error_text("screenshot", {"region": {"x": 0, "y": 0, "width": 0, "height": 10}})
        self.assertIn("positive", text)

    def test_region_requires_four_numbers(self):
        text = self.server.error_text("screenshot", {"region": {"x": 0, "y": 0}})
        self.assertIn("region", text)

    def test_scale_must_be_positive(self):
        text = self.server.error_text("screenshot", {"scale": 0})
        self.assertIn("scale must be positive", text)

    def test_unknown_window_names_the_next_step(self):
        text = self.server.error_text("window_rect", {"title": "NoSuchWindowZZZ"})
        self.assertIn("no window matches", text)
        self.assertIn("window_list", text)

    def test_window_rect_needs_a_selector(self):
        text = self.server.error_text("window_rect", {})
        self.assertIn("title", text)

    def test_monitor_unknown_lists_the_available_ones(self):
        text = self.server.error_text("screenshot", {"monitor": "NOPE"})
        self.assertIn("no monitor matches", text)

    def test_monitor_and_region_are_exclusive(self):
        text = self.server.error_text("screenshot", {"monitor": "primary", "region": {"x": 0, "y": 0, "width": 10, "height": 10}})
        self.assertIn("not both", text)

    def test_wait_until_rejects_unknown_condition(self):
        text = self.server.error_text("wait_until", {"condition": {"nonsense": 1}})
        self.assertIn("condition must contain one of", text)
        for word in ("window", "file", "screenshot_changed", "idle_at_least"):
            self.assertIn(word, text)

    def test_sequence_needs_steps(self):
        text = self.server.error_text("sequence", {"steps": []})
        self.assertIn("steps", text)

    def test_sequence_reports_the_failing_step(self):
        # A sequence always answers with one entry per step, so the caller can see
        # which step broke; the call itself is flagged as an error when one failed.
        is_error, payload = self.server.tool(
            "sequence", {"steps": [{"op": "screenshot", "args": {"scale": 0}}]}
        )
        self.assertTrue(is_error)
        self.assertEqual(payload["steps_total"], 1)
        self.assertEqual(payload["stopped_at"], 1)
        self.assertFalse(payload["steps"][0]["ok"])
        self.assertIn("scale must be positive", payload["steps"][0]["error"])


if __name__ == "__main__":
    unittest.main()
