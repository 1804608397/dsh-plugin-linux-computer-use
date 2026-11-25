#!/usr/bin/env python3
"""linux-desktop-mcp: a stdio MCP server that drives a Wayland GNOME desktop.

The server speaks newline-delimited JSON-RPC 2.0 on stdin/stdout (the MCP stdio
transport) and exposes the tools DSH publishes as ``mcp__linux__*``:

  vision    screenshot                       portal Screenshot -> PNG
  tree      ui_apps, ui_tree, ui_find, ui_describe, ui_text
  action    ui_action, ui_focus, ui_set_text
  pointer   click, move, drag, scroll, pointer_button
  keyboard  key, type_text
  system    clipboard_get, clipboard_set, process_list, process_kill,
            launch_app, settings_get, settings_set, settings_list_keys,
            notify, window_list, display_info, desktop_status

Design notes that matter when editing this file:

* stdout is protocol-only. Every diagnostic goes to stderr or the log file.
* Input injection goes through /dev/uinput (see cu_input.py): it needs the udev
  rule from the prep script and a re-login. When it is unavailable the tools
  fail with an actionable message instead of silently doing nothing.
* Screenshots go through the portal Screenshot interface: GNOME asks once and
  then remembers the answer, so repeated captures are silent (~0.5s each). The
  ScreenCast session in cu_display.py is only a fallback behind
  DSH_LINUX_CU_SCREENCAST=1.
* Coordinates are logical desktop pixels. ``space="screenshot"`` converts from
  the pixels of the most recent screenshot, which differ on HiDPI setups.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cu_a11y  # noqa: E402
import cu_display  # noqa: E402
import cu_input  # noqa: E402
import cu_sys  # noqa: E402

SERVER_NAME = "linux-desktop-mcp"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}
LOG_PATH = os.path.join(cu_display.CACHE_DIR, "server.log")
MAX_IMAGE_BYTES = 3_500_000
SHOT_MAX_WIDTH = 1600

A11Y = cu_a11y.A11y()
INPUT: cu_input.InputController | None = None
LAST_SHOT: dict = {"path": None, "width": 0, "height": 0, "logical": None, "at": 0.0}


BUTTON_NAMES = {1: "left", 2: "middle", 3: "right"}


def _button_name(value) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ToolError("button must be 1 (left), 2 (middle) or 3 (right)") from None
    name = BUTTON_NAMES.get(number)
    if name is None:
        raise ToolError("button must be 1 (left), 2 (middle) or 3 (right)")
    return name


class ToolError(RuntimeError):
    """A tool-level failure with a message meant for the model."""


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n"
    try:
        os.makedirs(cu_display.CACHE_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(line)
        if os.path.getsize(LOG_PATH) > 1_000_000:
            os.replace(LOG_PATH, LOG_PATH + ".1")
    except OSError:
        pass
    sys.stderr.write(line)
    sys.stderr.flush()


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def layout() -> cu_display.Layout:
    return cu_display.read_layout()


def controller(reload_layout: bool = False) -> cu_input.InputController:
    global INPUT
    current = layout()
    if INPUT is None or reload_layout or INPUT.layout.serial != current.serial:
        if INPUT is not None:
            INPUT.close()
        INPUT = cu_input.InputController(current)
    return INPUT


def _guard_input(what: str) -> None:
    """Refuse to inject input while the session is locked.

    A locked GNOME session keeps the password field focused, so a stray click
    or keystroke lands inside it. Screenshots stay allowed; only input is
    blocked. ``DSH_LINUX_CU_ALLOW_LOCKED_INPUT=1`` is the escape hatch for
    someone who really knows what they are doing.
    """
    if os.environ.get("DSH_LINUX_CU_ALLOW_LOCKED_INPUT") == "1":
        return
    if cu_sys.screen_locked() is True:
        raise ToolError(
            f"refusing to {what}: the session is locked. The lock screen's focused "
            "widget is the password field, so injected input would type into it. "
            "Unlock the desktop and retry; mcp__linux__screenshot still works."
        )


def _to_logical(x: float, y: float, space: str) -> tuple[float, float]:
    if space == "screenshot":
        shot_w = LAST_SHOT.get("width") or 0
        logical = LAST_SHOT.get("logical") or None
        if not shot_w or not logical:
            raise ToolError("no screenshot yet: take one first, or pass space='logical'")
        ratio = (logical[0] / shot_w) if shot_w else 1.0
        return (x * ratio, y * ratio)
    return (x, y)


def capture_shot(max_width: int = SHOT_MAX_WIDTH) -> dict:
    """Grab the desktop, preferring the screenshot portal over a pipewire stream.

    The portal Screenshot interface is a single round trip (~0.5s) and needs no
    long-lived session, so it is the default. The ScreenCast + GStreamer path
    stays as a fallback because it is the only one that can stream frames when
    capture permission exists but the screenshot interface is missing.
    """
    name = f"shot-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.png"
    path = os.path.join(cu_display.SHOT_DIR, name)
    method = ""
    screenshot_error: Exception | None = None

    if os.environ.get("DSH_LINUX_CU_SCREENCAST") != "1":
        try:
            cu_display.portal_screenshot(path)
            method = "portal-screenshot"
        except cu_display.PortalError as error:
            screenshot_error = error

    if not method:
        session = cu_display.ScreencastSession.shared()
        try:
            session.ensure()
            cu_display.capture_frame(session, path)
            method = "portal-screencast"
        except cu_display.PortalError as error:
            session.close()
            detail = f" (the screenshot portal said: {screenshot_error})" if screenshot_error else ""
            raise ToolError(
                f"screen capture is unavailable: {error}{detail}. "
                "Grant screen sharing once when the desktop asks, then retry; "
                "run linux_mcp_doctor to see the current state."
            ) from error

    width, height = cu_display.downscale(path, max_width)
    current = layout()
    LAST_SHOT.update({
        "path": path,
        "width": width,
        "height": height,
        "logical": (current.width, current.height),
        "at": time.time(),
    })
    return {
        "path": path,
        "width": width,
        "height": height,
        "logical_width": current.width,
        "logical_height": current.height,
        "coordinate_scale": round(current.width / width, 6) if width else 1.0,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "method": method,
    }


# --------------------------------------------------------------------------- #
# tool implementations
# --------------------------------------------------------------------------- #
def tool_screenshot(args: dict) -> dict:
    max_width = int(args.get("max_width", SHOT_MAX_WIDTH))
    info = capture_shot(max_width)
    try:
        with open(info["path"], "rb") as handle:
            payload = handle.read()
    except OSError as error:
        raise ToolError(f"captured frame is not readable: {error}") from error
    if len(payload) > MAX_IMAGE_BYTES:
        cu_display.downscale(info["path"], max(640, int(info["width"] * 0.6)))
        with open(info["path"], "rb") as handle:
            payload = handle.read()
        width, height = cu_display.png_size(info["path"])
        info["width"], info["height"] = width, height
        info["coordinate_scale"] = round(info["logical_width"] / width, 6) if width else 1.0
    encoded = base64.b64encode(payload).decode("ascii")
    note = (
        f"screenshot {info['width']}x{info['height']} px saved at {info['path']}; "
        f"logical desktop is {info['logical_width']}x{info['logical_height']}. "
        f"To click at a pixel of this image pass space='screenshot'; "
        f"otherwise multiply by {info['coordinate_scale']}."
    )
    return {
        "content": [
            {"type": "image", "data": encoded, "mimeType": "image/png"},
            {"type": "text", "text": json.dumps({**info, "note": note}, ensure_ascii=False)},
        ]
    }


def tool_ui_apps(args: dict) -> dict:
    apps = A11Y.apps()
    return {"applications": apps, "count": len(apps)}


def tool_ui_tree(args: dict) -> dict:
    app = args.get("app")
    depth = max(1, min(int(args.get("max_depth", 3)), 8))
    nodes = max(10, min(int(args.get("max_nodes", 220)), 1200))
    snapshot = A11Y.snapshot(app=app, max_depth=depth, max_nodes=nodes)
    if snapshot["nodes_used"] <= 3 and snapshot["applications"]:
        snapshot["hint"] = (
            "the application exposes (almost) no accessibility nodes: Chromium, "
            "Electron and Qt apps stay dark unless started with accessibility on. "
            "Use screenshot + coordinate clicks for that app instead."
        )
    return snapshot


def tool_ui_find(args: dict) -> dict:
    hits = A11Y.find(
        query=args.get("query"),
        role=args.get("role"),
        app=args.get("app"),
        limit=max(1, min(int(args.get("limit", 20)), 100)),
    )
    return {"matches": hits, "count": len(hits)}


def tool_ui_describe(args: dict) -> dict:
    ref = _require(args, "ref")
    return A11Y.describe(ref)


def tool_ui_text(args: dict) -> dict:
    ref = _require(args, "ref")
    return A11Y.text(ref)


def tool_ui_action(args: dict) -> dict:
    _guard_input("tool_ui_action")
    ref = _require(args, "ref")
    action = args.get("action")
    return A11Y.action(ref, action)


def tool_ui_focus(args: dict) -> dict:
    _guard_input("tool_ui_focus")
    ref = _require(args, "ref")
    return A11Y.focus(ref)


def tool_ui_set_text(args: dict) -> dict:
    _guard_input("tool_ui_set_text")
    ref = _require(args, "ref")
    text = args.get("text")
    if not isinstance(text, str):
        raise ToolError("'text' must be a string")
    return A11Y.set_text(ref, text)


def tool_click(args: dict) -> dict:
    _guard_input("click")
    x, y = _point(args)
    x, y = _to_logical(x, y, str(args.get("space", "logical")))
    button = _button_name(args.get("button", 1))
    count = max(1, min(int(args.get("count", 1)), 3))
    controller().click(x, y, button=button, count=count)
    return {"clicked": {"x": round(x), "y": round(y), "button": button, "count": count}}


def tool_move(args: dict) -> dict:
    _guard_input("move the pointer")
    x, y = _point(args)
    x, y = _to_logical(x, y, str(args.get("space", "logical")))
    controller().move(x, y)
    return {"moved_to": {"x": round(x), "y": round(y)}}


def tool_drag(args: dict) -> dict:
    _guard_input("drag")
    start = (float(args.get("x1", 0)), float(args.get("y1", 0)))
    end = (float(args.get("x2", 0)), float(args.get("y2", 0)))
    space = str(args.get("space", "logical"))
    start = _to_logical(*start, space)
    end = _to_logical(*end, space)
    duration = max(100, min(int(args.get("duration_ms", 450)), 5000))
    controller().drag(start[0], start[1], end[0], end[1], duration_ms=duration)
    return {"dragged": {"from": [round(v) for v in start], "to": [round(v) for v in end]}}


def tool_scroll(args: dict) -> dict:
    _guard_input("scroll")
    dx = int(args.get("dx", 0))
    dy = int(args.get("dy", 0))
    if not dx and not dy:
        raise ToolError("pass a non-zero dx and/or dy (positive dy scrolls down)")
    controller().scroll(dx=dx, dy=dy)
    return {"scrolled": {"dx": dx, "dy": dy}}


def tool_pointer_button(args: dict) -> dict:
    _guard_input("press a mouse button")
    action = str(args.get("action", "press")).lower()
    button = _button_name(args.get("button", 1))
    if args.get("x") is not None and args.get("y") is not None:
        x, y = _to_logical(float(args["x"]), float(args["y"]), str(args.get("space", "logical")))
        controller().move(x, y)
    if action == "press":
        controller().press_hold(button)
    elif action == "release":
        controller().release_hold(button)
    else:
        raise ToolError("action must be 'press' or 'release'")
    return {"button": button, "action": action}


def tool_key(args: dict) -> dict:
    _guard_input("press a key")
    combo = _require(args, "combo")
    controller().key(str(combo))
    return {"pressed": combo}


def tool_type_text(args: dict) -> dict:
    _guard_input("type text")
    text = _require(args, "text")
    controller().type_text(str(text))
    return {"typed": len(str(text))}


def tool_clipboard_get(args: dict) -> dict:
    return cu_sys.clipboard_get()


def tool_clipboard_set(args: dict) -> dict:
    text = _require(args, "text")
    return cu_sys.clipboard_set(str(text))


def tool_process_list(args: dict) -> dict:
    rows = cu_sys.process_list(
        limit=max(1, min(int(args.get("limit", 25)), 200)),
        sort=str(args.get("sort", "cpu")),
        name_filter=args.get("name"),
    )
    return {"processes": rows, "count": len(rows)}


def tool_process_kill(args: dict) -> dict:
    pid = int(args.get("pid", 0))
    return cu_sys.process_kill(pid, str(args.get("signal", "TERM")))


def tool_launch_app(args: dict) -> dict:
    command = args.get("command")
    if isinstance(command, list):
        result = cu_sys.launch([str(part) for part in command])
    elif isinstance(command, str) and command.strip():
        result = cu_sys.launch(command)
    else:
        raise ToolError("'command' must be a string or a list of arguments")
    time.sleep(0.4)
    return result


def tool_settings_get(args: dict) -> dict:
    return cu_sys.settings_get(_require(args, "schema"), _require(args, "key"))


def tool_settings_set(args: dict) -> dict:
    return cu_sys.settings_set(
        _require(args, "schema"), _require(args, "key"), _require(args, "value")
    )


def tool_settings_list_keys(args: dict) -> dict:
    keys = cu_sys.settings_list_keys(_require(args, "schema"))
    return {"schema": args["schema"], "keys": keys, "count": len(keys)}


def tool_notify(args: dict) -> dict:
    return cu_sys.notify(
        _require(args, "summary"),
        str(args.get("body", "")),
        int(args.get("timeout_ms", 8000)),
    )


def tool_window_list(args: dict) -> dict:
    windows = []
    for app in A11Y.apps(max_age=2.0):
        try:
            node = A11Y.describe(app["ref"])
        except cu_a11y.A11yError:
            continue
        target = _first_frame(A11Y.resolve(app["ref"]))
        if target is None:
            windows.append({"app": app["name"], "title": "", "ref": app["ref"], "pid": app["pid"]})
            continue
        title, states = target
        windows.append({
            "app": app["name"],
            "title": title,
            "ref": app["ref"],
            "pid": app["pid"],
            "active": "active" in states or "focused" in states,
        })
        del node
    return {"windows": windows, "count": len(windows)}


def _first_frame(app):
    try:
        count = app.get_child_count()
    except Exception:  # noqa: BLE001
        return None
    for index in range(min(count, 4)):
        try:
            child = app.get_child_at_index(index)
            if child is None:
                continue
            if (child.get_role_name() or "") == "frame":
                return (child.get_name() or "", cu_a11y._state_names(child))
        except Exception:  # noqa: BLE001
            continue
    return None


def tool_display_info(args: dict) -> dict:
    current = layout()
    info = current.describe()
    info["session"] = {
        "type": os.environ.get("XDG_SESSION_TYPE", ""),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
        "wayland_display": os.environ.get("WAYLAND_DISPLAY", ""),
        "display": os.environ.get("DISPLAY", ""),
    }
    info["last_screenshot"] = {key: LAST_SHOT[key] for key in ("path", "width", "height", "at")}
    return info


def tool_desktop_status(args: dict) -> dict:
    status: dict = {"server": {"name": SERVER_NAME, "version": SERVER_VERSION, "log": LOG_PATH}}
    status["session"] = {
        "type": os.environ.get("XDG_SESSION_TYPE", ""),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
        "locked": cu_sys.screen_locked(),
    }
    try:
        current = layout()
        status["display"] = current.describe()
    except cu_display.PortalError as error:
        status["display"] = {"error": str(error)}
    try:
        backend = controller().backend_name
        status["input"] = {"backend": backend, "ready": True}
    except Exception as error:  # noqa: BLE001
        status["input"] = {"ready": False, "error": str(error)}
    token = cu_display.ScreencastSession.load_token()
    status["capture"] = {
        "preferred": "portal-screenshot",
        "shots_dir": cu_display.SHOT_DIR,
        "portal_screenshot": True,
        "portal_screencast": True,
        "screencast_restore_token": bool(token),
        "screencast_session_open": cu_display.ScreencastSession.shared().fd is not None,
        "gstreamer_pipewiresrc": _have_gst("pipewiresrc"),
    }
    try:
        apps = A11Y.apps(max_age=10.0)
        status["a11y"] = {"apps_visible": len(apps)}
    except cu_a11y.A11yError as error:
        status["a11y"] = {"error": str(error)}
    status["system"] = {
        "wl_clipboard": cu_sys.clipboard_available(),
        "notify_send": cu_sys.have("notify-send"),
        "gsettings": cu_sys.have("gsettings"),
        "uinput_writable": os.access("/dev/uinput", os.W_OK),
    }
    return status


def _have_gst(element_name: str) -> bool:
    try:
        gi = __import__("gi")
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        if not Gst.is_initialized():
            Gst.init(None)
        return Gst.ElementFactory.find(element_name) is not None
    except Exception:  # noqa: BLE001
        return False


def _require(args: dict, key: str) -> object:
    value = args.get(key)
    if value is None or value == "":
        raise ToolError(f"missing required argument {key!r}")
    return value


def _point(args: dict) -> tuple[float, float]:
    if args.get("x") is None or args.get("y") is None:
        raise ToolError("both 'x' and 'y' are required")
    return (float(args["x"]), float(args["y"]))


# --------------------------------------------------------------------------- #
# tool registry
# --------------------------------------------------------------------------- #
SPACE_PROP = {
    "type": "string",
    "enum": ["logical", "screenshot"],
    "default": "logical",
    "description": "Coordinate space: 'logical' desktop pixels, or 'screenshot' pixels of the last screenshot.",
}

TOOLS: list[dict] = [
    {
        "name": "screenshot",
        "description": (
            "Capture the whole desktop and return it as a PNG image plus its size and path. "
            "Use it to see the screen before deciding where to click. The desktop asks for a "
            "one-time screen-capture approval; after that every capture is silent and takes "
            "about half a second."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_width": {
                    "type": "integer",
                    "default": SHOT_MAX_WIDTH,
                    "description": "Downscale the image to this pixel width (0 keeps native resolution).",
                }
            },
        },
        "handler": tool_screenshot,
    },
    {
        "name": "ui_apps",
        "description": (
            "List the applications currently exposed through the AT-SPI accessibility bus, with "
            "element references usable by ui_tree/ui_find/ui_action."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_ui_apps,
    },
    {
        "name": "ui_tree",
        "description": (
            "Read the accessibility tree of one application (or a few) as nested role/name/rect/"
            "action nodes. Bounded by max_depth and max_nodes. Chromium/Electron/Qt apps usually "
            "expose almost nothing; use screenshot for those."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Application name as shown by ui_apps (substring match)."},
                "max_depth": {"type": "integer", "default": 3, "minimum": 1, "maximum": 8},
                "max_nodes": {"type": "integer", "default": 220, "minimum": 10, "maximum": 1200},
            },
        },
        "handler": tool_ui_tree,
    },
    {
        "name": "ui_find",
        "description": (
            "Search every reachable accessibility node for a name/text substring and/or a role, "
            "returning refs with screen rectangles. Much cheaper than reading a whole tree."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Case-insensitive substring of the node name or text."},
                "role": {"type": "string", "description": "Role filter, e.g. 'push button', 'text', 'entry'."},
                "app": {"type": "string", "description": "Restrict to one application (substring match)."},
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
            },
        },
        "handler": tool_ui_find,
    },
    {
        "name": "ui_describe",
        "description": "Describe one node: role, name, states, rectangle, available actions, text.",
        "inputSchema": {
            "type": "object",
            "properties": {"ref": {"type": "string", "description": "Element reference from ui_tree/ui_find."}},
            "required": ["ref"],
        },
        "handler": tool_ui_describe,
    },
    {
        "name": "ui_text",
        "description": "Read the full text of a text-bearing node (label, entry, document).",
        "inputSchema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}},
            "required": ["ref"],
        },
        "handler": tool_ui_text,
    },
    {
        "name": "ui_action",
        "description": (
            "Invoke an accessibility action on a node (e.g. 'click', 'press', 'activate'). "
            "More reliable than a coordinate click when the node exists."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "action": {"type": "string", "description": "Action name; defaults to the first action."},
            },
            "required": ["ref"],
        },
        "handler": tool_ui_action,
    },
    {
        "name": "ui_focus",
        "description": "Give keyboard focus to a node, so a following key/type_text lands there.",
        "inputSchema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}},
            "required": ["ref"],
        },
        "handler": tool_ui_focus,
    },
    {
        "name": "ui_set_text",
        "description": "Set the contents of an editable node directly (no keystrokes, no focus dance).",
        "inputSchema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}, "text": {"type": "string"}},
            "required": ["ref", "text"],
        },
        "handler": tool_ui_set_text,
    },
    {
        "name": "click",
        "description": "Move the pointer to (x, y) and click. Supports button 1-3 and up to triple clicks.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "button": {"type": "integer", "default": 1, "description": "1 left, 2 middle, 3 right."},
                "count": {"type": "integer", "default": 1, "minimum": 1, "maximum": 3},
                "space": SPACE_PROP,
            },
            "required": ["x", "y"],
        },
        "handler": tool_click,
    },
    {
        "name": "move",
        "description": "Move the pointer without clicking (useful to hover for menus and tooltips).",
        "inputSchema": {
            "type": "object",
            "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "space": SPACE_PROP},
            "required": ["x", "y"],
        },
        "handler": tool_move,
    },
    {
        "name": "drag",
        "description": "Press at (x1, y1), move to (x2, y2) over duration_ms, then release.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x1": {"type": "number"},
                "y1": {"type": "number"},
                "x2": {"type": "number"},
                "y2": {"type": "number"},
                "duration_ms": {"type": "integer", "default": 450, "minimum": 100, "maximum": 5000},
                "space": SPACE_PROP,
            },
            "required": ["x1", "y1", "x2", "y2"],
        },
        "handler": tool_drag,
    },
    {
        "name": "scroll",
        "description": "Scroll the pointer's position. Positive dy scrolls down, positive dx scrolls right.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dx": {"type": "integer", "default": 0, "description": "Horizontal wheel notches."},
                "dy": {"type": "integer", "default": 0, "description": "Vertical wheel notches."},
            },
        },
        "handler": tool_scroll,
    },
    {
        "name": "pointer_button",
        "description": "Hold or release a mouse button without moving (drag with explicit control).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["press", "release"]},
                "button": {"type": "integer", "default": 1},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "space": SPACE_PROP,
            },
            "required": ["action"],
        },
        "handler": tool_pointer_button,
    },
    {
        "name": "key",
        "description": (
            "Press a key combination, e.g. 'Return', 'Escape', 'ctrl+t', 'ctrl+shift+Tab', 'alt+F4'. "
            "Modifiers are ctrl/alt/shift/super."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"combo": {"type": "string"}},
            "required": ["combo"],
        },
        "handler": tool_key,
    },
    {
        "name": "type_text",
        "description": "Type a string as keyboard input into whatever has focus (US layout).",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "handler": tool_type_text,
    },
    {
        "name": "clipboard_get",
        "description": "Read the Wayland clipboard as text.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_clipboard_get,
    },
    {
        "name": "clipboard_set",
        "description": "Replace the Wayland clipboard with text (useful to paste long answers).",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "handler": tool_clipboard_set,
    },
    {
        "name": "process_list",
        "description": "List processes sorted by cpu/memory with pid, user, command line.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 25, "minimum": 1, "maximum": 200},
                "sort": {"type": "string", "enum": ["cpu", "mem", "pid"], "default": "cpu"},
                "name": {"type": "string", "description": "Only rows whose command or args contain this substring."},
            },
        },
        "handler": tool_process_list,
    },
    {
        "name": "process_kill",
        "description": "Send a signal to a process (TERM by default). Refuses pid 1 and the server itself.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "signal": {"type": "string", "enum": ["TERM", "KILL", "INT", "HUP"], "default": "TERM"},
            },
            "required": ["pid"],
        },
        "handler": tool_process_kill,
    },
    {
        "name": "launch_app",
        "description": "Start an application detached from the server, e.g. 'google-chrome https://...'.",
        "inputSchema": {
            "type": "object",
            "properties": {"command": {"description": "A shell-like string or an argv list."}},
            "required": ["command"],
        },
        "handler": tool_launch_app,
    },
    {
        "name": "settings_get",
        "description": "Read a gsettings value, e.g. schema 'org.gnome.desktop.interface', key 'clock-format'.",
        "inputSchema": {
            "type": "object",
            "properties": {"schema": {"type": "string"}, "key": {"type": "string"}},
            "required": ["schema", "key"],
        },
        "handler": tool_settings_get,
    },
    {
        "name": "settings_set",
        "description": "Write a gsettings value (the closest thing to the Windows registry).",
        "inputSchema": {
            "type": "object",
            "properties": {"schema": {"type": "string"}, "key": {"type": "string"}, "value": {"type": "string"}},
            "required": ["schema", "key", "value"],
        },
        "handler": tool_settings_set,
    },
    {
        "name": "settings_list_keys",
        "description": "List the keys of a gsettings schema.",
        "inputSchema": {
            "type": "object",
            "properties": {"schema": {"type": "string"}},
            "required": ["schema"],
        },
        "handler": tool_settings_list_keys,
    },
    {
        "name": "notify",
        "description": "Show a desktop notification (used to report progress or ask the user to step in).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "body": {"type": "string"},
                "timeout_ms": {"type": "integer", "default": 8000},
            },
            "required": ["summary"],
        },
        "handler": tool_notify,
    },
    {
        "name": "window_list",
        "description": "List application windows with their title and which one looks active.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_window_list,
    },
    {
        "name": "display_info",
        "description": (
            "Report the monitor layout (logical size, per-monitor rectangles, scale) and the "
            "coordinate mapping to use for clicks."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_display_info,
    },
    {
        "name": "desktop_status",
        "description": (
            "Self-check of the desktop server: input backend, capture permission token, "
            "accessibility reachability and optional helper binaries."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_desktop_status,
    },
]

TOOL_INDEX = {spec["name"]: spec for spec in TOOLS}


def call_tool(name: str, arguments: dict) -> dict:
    spec = TOOL_INDEX.get(name)
    if spec is None:
        raise ToolError(f"unknown tool {name!r}; available: {sorted(TOOL_INDEX)}")
    handler = spec["handler"]
    try:
        result = handler(arguments or {})
    except (ToolError, cu_a11y.A11yError, cu_display.PortalError, cu_sys.SysError,
            cu_input.InputError) as error:
        log(f"tool {name} failed: {error}")
        return {"content": [{"type": "text", "text": str(error)}], "isError": True}
    except Exception as error:  # noqa: BLE001 - never kill the server on a tool bug
        log(f"tool {name} crashed: {error}\n{traceback.format_exc()}")
        return {
            "content": [{"type": "text", "text": f"{type(error).__name__}: {error}"}],
            "isError": True,
        }
    if isinstance(result, dict) and "content" in result:
        return result
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}]}


# --------------------------------------------------------------------------- #
# JSON-RPC plumbing
# --------------------------------------------------------------------------- #
def respond(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def error_response(identifier, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


def handle(message: dict) -> dict | None:
    method = message.get("method", "")
    identifier = message.get("id")
    params = message.get("params") or {}
    is_notification = "id" not in message

    if method == "initialize":
        requested = params.get("protocolVersion") or PROTOCOL_VERSION
        version = requested if requested in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": identifier,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Linux (Wayland GNOME) desktop control. Prefer screenshot to observe, "
                    "ui_tree/ui_find for GTK apps, and short waits between actions."
                ),
            },
        }
    if is_notification:
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": identifier, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": identifier,
            "result": {
                "tools": [
                    {
                        "name": spec["name"],
                        "description": spec["description"],
                        "inputSchema": spec["inputSchema"],
                    }
                    for spec in TOOLS
                ]
            },
        }
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str):
            return error_response(identifier, -32602, "tools/call requires a tool name")
        try:
            result = call_tool(name, arguments)
        except ToolError as error:
            return error_response(identifier, -32602, str(error))
        return {"jsonrpc": "2.0", "id": identifier, "result": result}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": identifier, "result": {"resources": []}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": identifier, "result": {"prompts": []}}
    return error_response(identifier, -32601, f"method not supported: {method}")


# --------------------------------------------------------------------------- #
# self test (used by the linux_mcp_doctor DSH tool)
# --------------------------------------------------------------------------- #
def _check(checks: list, ident: str, status: str, title: str, detail: str = "", fix: str = "") -> None:
    entry = {"id": ident, "status": status, "title": title}
    if detail:
        entry["detail"] = detail
    if fix:
        entry["fix"] = fix
    checks.append(entry)


def _portal_screencast_version() -> int | None:
    """Read the Screencast portal version without triggering a permission dialog."""
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio

    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.DBus.Properties",
            "Get",
            __import__("gi").repository.GLib.Variant(
                "(ss)", ("org.freedesktop.portal.ScreenCast", "version")
            ),
            __import__("gi").repository.GLib.VariantType("(v)"),
            Gio.DBusCallFlags.NONE,
            3000,
            None,
        )
        return int(reply.unpack()[0])
    except Exception:  # noqa: BLE001
        return None


def selftest() -> dict:
    """Collect everything the doctor tool needs, without asking for permissions."""
    checks: list[dict] = []

    session = os.environ.get("XDG_SESSION_TYPE", "unknown")
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "unknown")
    if session == "wayland":
        _check(checks, "session", "ok", f"Wayland session ({desktop})", f"WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY', '')}")
    elif session == "x11":
        _check(
            checks,
            "session",
            "warn",
            "X11 session",
            "This server targets Wayland. Screenshots still work, but use xdotool-style tools instead if you prefer Xorg.",
        )
    else:
        _check(checks, "session", "warn", f"Unknown session type ({session})", "XDG_SESSION_TYPE is not wayland/x11.")

    # python + GI stack
    modules = ["gi", "gi.repository.Atspi", "gi.repository.Gst", "gi.repository.GdkPixbuf", "gi.repository.Gio"]
    missing = []
    for module in modules:
        try:
            __import__(module)
        except Exception as error:  # noqa: BLE001
            missing.append(f"{module} ({error.__class__.__name__})")
    version = "%d.%d.%d" % sys.version_info[:3]
    if missing:
        _check(
            checks,
            "python",
            "fail",
            f"Python {version} is missing GI bindings",
            ", ".join(missing),
            "sudo apt install python3-gi gir1.2-atspi-2.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gir1.2-gdkpixbuf-2.0",
        )
    else:
        _check(checks, "python", "ok", f"Python {version} with gi/Atspi/Gst/GdkPixbuf")

    # input injection: /dev/uinput
    device = cu_input.DEFAULT_UINPUT
    if not os.path.exists(device):
        _check(
            checks,
            "uinput",
            "fail",
            f"{device} does not exist",
            "input injection cannot work without it",
            "sudo ./linux-computer-use-prep.sh, then log out and back in",
        )
    elif not (os.access(device, os.R_OK) and os.access(device, os.W_OK)):
        import grp

        import stat as stat_module

        info = os.stat(device)
        group = grp.getgrgid(info.st_gid).gr_name
        _check(
            checks,
            "uinput",
            "fail",
            f"{device} is not writable by this user",
            f"mode {stat_module.filemode(info.st_mode)}, group {group}; your groups: {' '.join(os.getgroups() and [grp.getgrgid(g).gr_name for g in os.getgroups()] or [])}",
            "sudo ./linux-computer-use-prep.sh, then LOG OUT and back in so the 'input' group applies",
        )
    else:
        _check(checks, "uinput", "ok", f"{device} is readable and writable")

    socket_path = cu_input.DEFAULT_YDOTOOL_SOCKET
    if os.path.exists(socket_path) and os.access(socket_path, os.W_OK):
        _check(checks, "ydotool", "ok", "ydotool fallback socket is available", socket_path)
    else:
        _check(
            checks,
            "ydotool",
            "warn",
            "ydotool fallback is not available",
            "optional: only used when /dev/uinput cannot be opened",
            "sudo ./linux-computer-use-prep.sh installs and starts ydotoold",
        )

    # clipboard + notification helpers
    for ident, binary, package, required in (
        ("wl-clipboard", "wl-copy", "wl-clipboard", True),
        ("wl-paste", "wl-paste", "wl-clipboard", True),
        ("notify-send", "notify-send", "libnotify-bin", False),
        ("gsettings", "gsettings", "libglib2.0-bin", False),
    ):
        if cu_sys.have(binary):
            _check(checks, ident, "ok", f"{binary} found")
        else:
            _check(
                checks,
                ident,
                "fail" if required else "warn",
                f"{binary} is not installed",
                "" if required else "only needed for the corresponding tool",
                f"sudo apt install {package}",
            )

    # portal + stream capture
    portal_version = _portal_screencast_version()
    if portal_version is None:
        _check(
            checks,
            "portal",
            "fail",
            "xdg-desktop-portal ScreenCast is not reachable",
            "screenshots are impossible without it",
            "make sure xdg-desktop-portal and xdg-desktop-portal-gnome are running",
        )
    else:
        _check(checks, "portal", "ok", f"ScreenCast portal version {portal_version}")

    if not _have_gst("pipewiresrc"):
        _check(
            checks,
            "gstreamer",
            "fail",
            "GStreamer element pipewiresrc is missing",
            "the ScreenCast stream cannot be decoded into PNG",
            "sudo apt install gstreamer1.0-pipewire gstreamer1.0-plugins-base",
        )
    else:
        _check(checks, "gstreamer", "ok", "GStreamer pipewiresrc available")

    locked = cu_sys.screen_locked()
    if locked is True:
        _check(
            checks,
            "screen-lock",
            "warn",
            "the session is locked right now",
            "screenshots still work; input tools refuse to type into the password field",
            "unlock the desktop to drive it",
        )
    elif locked is False:
        _check(checks, "screen-lock", "ok", "the session is unlocked")
    else:
        _check(checks, "screen-lock", "warn", "lock state is unknown", "org.gnome.ScreenSaver.GetActive did not answer")

    token_path = cu_display.ScreencastSession.token_path() if hasattr(cu_display.ScreencastSession, "token_path") else None
    has_token = bool(cu_display.ScreencastSession.load_token())
    if has_token:
        _check(checks, "capture-permission", "ok", "screen capture permission is remembered")
    else:
        _check(
            checks,
            "capture-permission",
            "warn",
            "screen capture has not been granted yet",
            "the first screenshot shows a one-time approval dialog"
            + (f"; token file: {token_path}" if token_path else ""),
            "run mcp__linux__screenshot once and approve the dialog",
        )

    # display + a11y
    try:
        current = layout()
        _check(checks, "display", "ok", "display layout readable", current.describe().get("monitors_summary", "") or str(current.describe())[:200])
    except Exception as error:  # noqa: BLE001
        _check(checks, "display", "warn", "display layout unavailable", str(error))

    try:
        started = time.time()
        apps = A11Y.apps(max_age=2.0)
        elapsed = time.time() - started
        _check(
            checks,
            "a11y",
            "ok" if len(apps) > 1 else "warn",
            f"AT-SPI sees {len(apps)} applications",
            f"enumeration took {elapsed:.1f}s; Chromium/Electron apps expose an empty tree without accessibility enabled",
        )
    except Exception as error:  # noqa: BLE001
        _check(checks, "a11y", "warn", "AT-SPI is unavailable", str(error), "check that org.a11y.Bus is running")

    ok = not any(check["status"] == "fail" for check in checks)
    return {
        "ok": ok,
        "server": {"name": SERVER_NAME, "version": SERVER_VERSION, "log": LOG_PATH},
        "checks": checks,
    }


def main() -> int:
    if "--selftest" in sys.argv[1:]:
        print(json.dumps(selftest(), indent=2, ensure_ascii=False))
        return 0
    if "--status" in sys.argv[1:]:
        print(json.dumps(tool_desktop_status({}), indent=2, ensure_ascii=False))
        return 0
    log(f"{SERVER_NAME} {SERVER_VERSION} starting (pid {os.getpid()})")
    stream = sys.stdin
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            respond(error_response(None, -32700, "invalid JSON"))
            continue
        if not isinstance(message, dict):
            respond(error_response(None, -32600, "invalid request"))
            continue
        try:
            reply = handle(message)
        except Exception as error:  # noqa: BLE001
            log(f"handler crashed: {error}\n{traceback.format_exc()}")
            if "id" in message:
                respond(error_response(message.get("id"), -32603, f"internal error: {error}"))
            continue
        if reply is not None:
            respond(reply)
    log("stdin closed; exiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
