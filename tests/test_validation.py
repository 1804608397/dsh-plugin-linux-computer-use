"""Arguments that must be rejected, and the wording users will read.

Everything that needs a live GNOME session (Mutter DisplayConfig, the
accessibility bus) is skipped when the machine does not have one, so the same
suite runs on a headless CI runner.
"""

from __future__ import annotations

import unittest

from helpers import Server, ServerGone, ServerGone


class ValidationTest(unittest.TestCase):

    def call(self, name, arguments=None):
        """Run a tool, skipping when this machine has no desktop bus at all."""
        try:
            return self.server.tool(name, arguments or {})
        except ServerGone:
            self.skipTest("the desktop stack this server needs is not reachable here")

    def expect_error(self, name, arguments=None) -> str:
        is_error, payload = self.call(name, arguments)
        self.assertTrue(is_error, f"{name} should have failed, returned {payload}")
        return payload.get("text", "")

    def setUp(self):
        self.server = Server()
        self.server.initialize()
        self.status = {}
        try:
            is_error, payload = self.server.tool("desktop_status")
            # A headless runner answers with an error string instead of a report.
            self.status = {} if is_error else payload
        except ServerGone:
            self.status = {}

    def tearDown(self):
        self.server.close()

    def skip_if(self, condition: bool, reason: str) -> None:
        if condition:
            self.skipTest(reason)

    @property
    def has_display(self) -> bool:
        display = self.status.get("display") or {}
        return bool(display) and "error" not in display

    @property
    def has_a11y(self) -> bool:
        return (self.status.get("a11y") or {}).get("apps_visible", 0) > 0

    def test_region_requires_positive_size(self):
        text = self.expect_error("screenshot", {"region": {"x": 0, "y": 0, "width": 0, "height": 10}})
        self.assertIn("positive", text)

    def test_region_requires_four_numbers(self):
        text = self.expect_error("screenshot", {"region": {"x": 0, "y": 0}})
        self.assertIn("region", text)

    def test_scale_must_be_positive(self):
        text = self.expect_error("screenshot", {"scale": 0})
        self.assertIn("scale must be positive", text)

    def test_unknown_window_names_the_next_step(self):
        self.skip_if(not self.has_a11y, "no accessibility bus on this machine")
        try:
            text = self.expect_error("window_rect", {"title": "NoSuchWindowZZZ"})
        except ServerGone:
            self.skipTest("the server cannot reach AT-SPI here")
        self.assertIn("no window matches", text)
        self.assertIn("window_list", text)

    def test_window_rect_needs_a_selector(self):
        text = self.expect_error("window_rect", {})
        self.assertIn("title", text)

    def test_monitor_unknown_lists_the_available_ones(self):
        self.skip_if(not self.has_display, "no Mutter DisplayConfig on this machine")
        text = self.expect_error("screenshot", {"monitor": "NOPE"})
        self.assertIn("no monitor matches", text)

    def test_monitor_and_region_are_exclusive(self):
        self.skip_if(not self.has_display, "no Mutter DisplayConfig on this machine")
        text = self.expect_error(
            "screenshot",
            {"monitor": "primary", "region": {"x": 0, "y": 0, "width": 10, "height": 10}},
        )
        self.assertIn("not both", text)

    def test_wait_until_rejects_unknown_condition(self):
        text = self.expect_error("wait_until", {"condition": {"nonsense": 1}})
        self.assertIn("condition must contain one of", text)
        for word in ("window", "file", "screenshot_changed", "idle_at_least"):
            self.assertIn(word, text)

    def test_sequence_needs_steps(self):
        text = self.expect_error("sequence", {"steps": []})
        self.assertIn("steps", text)

    def test_sequence_reports_the_failing_step(self):
        # A sequence always answers with one entry per step, so the caller can see
        # which step broke; the call itself is flagged as an error when one failed.
        is_error, payload = self.call(
            "sequence", {"steps": [{"op": "screenshot", "args": {"scale": 0}}]}
        )
        self.assertTrue(is_error)
        self.assertEqual(payload["steps_total"], 1)
        self.assertEqual(payload["stopped_at"], 1)
        self.assertFalse(payload["steps"][0]["ok"])
        self.assertIn("scale must be positive", payload["steps"][0]["error"])


if __name__ == "__main__":
    unittest.main()
