"""The tool surface DSH sees: names, schemas, documented parameters."""

from __future__ import annotations

import unittest

from helpers import Server

EXPECTED_TOOLS = {
    "screenshot", "ui_apps", "ui_tree", "ui_find", "ui_describe", "ui_text",
    "ui_action", "ui_focus", "ui_set_text", "click", "click_ref", "move", "drag",
    "scroll", "pointer_button", "key", "type_text", "clipboard_get", "clipboard_set",
    "process_list", "process_kill", "launch_app", "settings_get", "settings_set",
    "settings_list_keys", "notify", "window_list", "window_rect", "window_activate",
    "display_info", "desktop_status", "wait_until", "sequence",
}

INJECTING = {
    "click", "click_ref", "move", "drag", "scroll", "pointer_button", "key",
    "type_text", "ui_action", "ui_focus", "ui_set_text", "window_activate",
}


class ProtocolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server()
        cls.info = cls.server.initialize()
        cls.tools = cls.server.tools()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def test_initialize_reports_server(self):
        self.assertEqual(self.info["serverInfo"]["name"], "linux-desktop-mcp")
        self.assertIn("capabilities", self.info)

    def test_tool_surface_is_complete(self):
        missing = EXPECTED_TOOLS - set(self.tools)
        self.assertFalse(missing, f"tools missing from the surface: {sorted(missing)}")

    def test_every_tool_has_a_description_and_schema(self):
        for name, tool in self.tools.items():
            with self.subTest(tool=name):
                self.assertTrue(tool.get("description"), "empty description")
                self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_screenshot_schema(self):
        properties = self.tools["screenshot"]["inputSchema"]["properties"]
        for key in ("region", "scale", "monitor", "window", "window_app", "ref", "marks", "marks_limit"):
            self.assertIn(key, properties)

    def test_wait_until_documents_every_condition(self):
        condition = self.tools["wait_until"]["inputSchema"]["properties"]["condition"]
        text = (condition.get("description") or "") + self.tools["wait_until"]["description"]
        for word in ("window", "window_active", "ui", "file", "screenshot_changed", "idle_at_least"):
            self.assertIn(word, text)

    def test_injecting_tools_accept_the_idle_override(self):
        for name in INJECTING:
            with self.subTest(tool=name):
                properties = self.tools[name]["inputSchema"].get("properties", {})
                self.assertIn("allow_active_input", properties)

    def test_read_only_tools_do_not_take_the_override(self):
        for name in ("screenshot", "ui_tree", "window_list", "desktop_status"):
            with self.subTest(tool=name):
                properties = self.tools[name]["inputSchema"].get("properties", {})
                self.assertNotIn("allow_active_input", properties)


if __name__ == "__main__":
    unittest.main()
