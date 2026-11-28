"""Pure logic: window geometry, region parsing, coordinate mapping."""

from __future__ import annotations

import pathlib
import sys
import unittest

SERVER_DIR = pathlib.Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))

import linux_desktop_mcp as mcp  # noqa: E402


def xdotool_table(table):
    def runner(*argv, timeout=3.0):
        return table.get(tuple(argv), (1, ""))

    return runner


class WindowGeometryTest(unittest.TestCase):
    def setUp(self):
        self._real = mcp._xdotool

    def tearDown(self):
        mcp._xdotool = self._real

    def test_measures_the_visible_window(self):
        mcp._xdotool = xdotool_table({
            ("search", "--onlyvisible", "--name", "xclock"): (0, "11\n22"),
            ("getwindowgeometry", "--shell", "11"): (0, "WINDOW=11\nX=0\nY=0\nWIDTH=10\nHEIGHT=10\n"),
            ("getwindowgeometry", "--shell", "22"): (0, "WINDOW=22\nX=100\nY=200\nWIDTH=320\nHEIGHT=320\n"),
        })
        rect, source = mcp._window_geometry({"title": "xclock"})
        self.assertEqual(source, "x11")
        self.assertEqual(rect, {"x": 100, "y": 200, "width": 320, "height": 320})

    def test_ignores_helper_windows(self):
        mcp._xdotool = xdotool_table({
            ("search", "--onlyvisible", "--name", "Bitwarden"): (0, "7"),
            ("getwindowgeometry", "--shell", "7"): (0, "X=10\nY=10\nWIDTH=10\nHEIGHT=10\n"),
        })
        self.assertEqual(mcp._window_geometry({"title": "Bitwarden"}), (None, "unknown"))

    def test_hidden_window_is_not_measured(self):
        def runner(*argv, timeout=3.0):
            if argv[:2] == ("search", "--onlyvisible"):
                return (0, "")
            if argv == ("search", "--name", "Bitwarden"):
                return (0, "33")
            return (1, "")

        mcp._xdotool = runner
        self.assertEqual(mcp._window_geometry({"title": "Bitwarden"}), (None, "hidden"))

    def test_unknown_when_x11_has_nothing(self):
        mcp._xdotool = lambda *argv, timeout=3.0: (127, "")
        self.assertEqual(mcp._window_geometry({"title": "Chrome"}), (None, "unknown"))


class RegionTest(unittest.TestCase):
    def test_accepts_a_dict(self):
        self.assertEqual(
            mcp._parse_region({"x": 1, "y": 2, "width": 30, "height": 40}),
            (1, 2, 30, 40),
        )

    def test_accepts_a_sequence(self):
        self.assertEqual(mcp._parse_region([1, 2, 30, 40]), (1, 2, 30, 40))

    def test_rejects_a_short_sequence(self):
        with self.assertRaises(mcp.ToolError):
            mcp._parse_region([1, 2])

    def test_rejects_empty_size(self):
        with self.assertRaises(mcp.ToolError):
            mcp._parse_region({"x": 0, "y": 0, "width": 0, "height": 5})

    def test_none_stays_none(self):
        self.assertIsNone(mcp._parse_region(None))


class CoordinateTest(unittest.TestCase):
    def setUp(self):
        self._saved = dict(mcp.LAST_SHOT)

    def tearDown(self):
        mcp.LAST_SHOT.clear()
        mcp.LAST_SHOT.update(self._saved)

    def test_full_screen_mapping(self):
        mcp.LAST_SHOT.update({"width": 1400, "height": 450, "logical": {"width": 4480, "height": 1440},
                              "origin": (0, 0), "ratio": 3.2})
        self.assertEqual(mcp._to_logical(1400, 450, "screenshot"), (4480, 1440))

    def test_region_mapping_adds_the_origin(self):
        mcp.LAST_SHOT.update({"width": 900, "height": 500, "logical": {"width": 4480, "height": 1440},
                              "origin": (200, 200), "ratio": 1.0})
        self.assertEqual(mcp._to_logical(10, 10, "screenshot"), (210, 210))

    def test_logical_space_is_untouched(self):
        mcp.LAST_SHOT.update({"width": 900, "height": 500, "logical": {"width": 4480, "height": 1440},
                              "origin": (200, 200), "ratio": 1.0})
        self.assertEqual(mcp._to_logical(10, 10, "logical"), (10, 10))


if __name__ == "__main__":
    unittest.main()
