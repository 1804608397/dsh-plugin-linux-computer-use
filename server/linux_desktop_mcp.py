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
import shutil
import subprocess
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cu_a11y  # noqa: E402
import cu_display  # noqa: E402
import cu_input  # noqa: E402
import cu_sys  # noqa: E402
import cu_unlock  # noqa: E402

SERVER_NAME = "linux-desktop-mcp"
SERVER_VERSION = "1.6.2"
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}
LOG_PATH = os.path.join(cu_display.CACHE_DIR, "server.log")
MAX_IMAGE_BYTES = 3_500_000
SHOT_MAX_WIDTH = 1600
IDLE_GUARD_MS = int(os.environ.get("DSH_LINUX_CU_IDLE_GUARD_MS", "60000"))

A11Y = cu_a11y.A11y()
INPUT: cu_input.InputController | None = None
LAST_SHOT: dict = {"path": None, "width": 0, "height": 0, "logical": None, "origin": (0, 0), "ratio": 1.0, "at": 0.0}


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


# Injected events reset Mutter's idle timer, so a sequence would trip the very
# guard its own first step triggered. Anything inside this window is our echo.
SELF_ECHO_S = 2.5
_LAST_INJECT = {"at": 0.0}


def _stamp_injection() -> None:
    _LAST_INJECT["at"] = time.monotonic()


def _guard_input(what: str, args: dict | None = None) -> None:
    """Refuse to inject input while the session is locked or the user is active.

    A locked GNOME session keeps the password field focused, so a stray click
    or keystroke lands inside it. Screenshots stay allowed; only input is
    blocked. ``DSH_LINUX_CU_ALLOW_LOCKED_INPUT=1`` is the escape hatch for
    someone who really knows what they are doing.

    The second guard covers a subtler failure: driving the pointer while the
    user is typing fights them for the keyboard and moves their cursor mid
    gesture. When the compositor reports recent input, injection is refused
    unless the caller says the user asked for it right now
    (``allow_active_input=true``) or the machine-wide escape hatch
    ``DSH_LINUX_CU_ALLOW_ACTIVE_INPUT=1`` is set.
    """
    if os.environ.get("DSH_LINUX_CU_ALLOW_LOCKED_INPUT") != "1":
        if cu_sys.screen_locked() is True:
            if cu_unlock.unlock_enabled():
                hint = "unlock_screen is available because the user turned AI unlocking on."
            else:
                hint = (
                    "Unlock the desktop and retry; the user can also switch AI unlocking "
                    "on (unlock policy) to let this be handled here."
                )
            raise ToolError(
                f"refusing to {what}: the session is locked. The lock screen's focused "
                f"widget is the password field, so injected input would type into it. "
                f"{hint} mcp__linux__screenshot still works."
            )

    if os.environ.get("DSH_LINUX_CU_ALLOW_ACTIVE_INPUT") == "1":
        _stamp_injection()
        return
    if args and args.get("allow_active_input"):
        _stamp_injection()
        return
    idle = cu_sys.idle_ms()
    if idle is None or idle >= IDLE_GUARD_MS:
        _stamp_injection()
        return
    if (time.monotonic() - _LAST_INJECT["at"]) < SELF_ECHO_S:
        _stamp_injection()
        return
    raise ToolError(
        f"refusing to {what}: the user is at the keyboard (last input {idle / 1000:.1f}s ago), "
        f"so synthetic input would fight them. Wait until they have been idle for "
        f"{IDLE_GUARD_MS // 1000}s, or pass allow_active_input=true when they just asked you "
        f"to drive the desktop. Screenshots and reads stay available."
    )



def _to_logical(x: float, y: float, space: str) -> tuple[float, float]:
    if space == "screenshot":
        shot_w = LAST_SHOT.get("width") or 0
        logical = LAST_SHOT.get("logical") or None
        if not shot_w or not logical:
            raise ToolError("no screenshot yet: take one first, or pass space='logical'")
        ratio = LAST_SHOT.get("ratio") or ((logical[0] / shot_w) if shot_w else 1.0)
        origin = LAST_SHOT.get("origin") or (0, 0)
        return (origin[0] + x * ratio, origin[1] + y * ratio)
    return (x, y)


def _parse_region(value) -> tuple[float, float, float, float] | None:
    """Accept ``{x, y, width, height}`` (logical pixels) or ``[x, y, w, h]``."""
    if value is None:
        return None
    if isinstance(value, dict):
        try:
            x = float(value.get("x", 0))
            y = float(value.get("y", 0))
            w = float(value["width"] if "width" in value else value["w"])
            h = float(value["height"] if "height" in value else value["h"])
        except (KeyError, TypeError, ValueError) as error:
            raise ToolError("region must be {x, y, width, height} in logical pixels") from error
    elif isinstance(value, (list, tuple)) and len(value) == 4:
        x, y, w, h = (float(v) for v in value)
    else:
        raise ToolError("region must be {x, y, width, height} in logical pixels")
    if w <= 0 or h <= 0:
        raise ToolError("region width and height must be positive")
    return (x, y, w, h)


def _resolve_monitor(name: str | None) -> tuple[str, tuple[float, float, float, float]]:
    """Turn a connector name ("eDP-1") or "primary" into a logical rectangle.

    ``region`` is expressed in whole-desktop coordinates, so a multi-monitor
    user has to add the monitor offset by hand - and forgetting it silently
    crops the wrong screen. Naming the monitor removes that failure mode.
    """
    current = layout()
    monitors = list(getattr(current, "monitors", []) or [])
    if not monitors:
        raise ToolError("cannot read the monitor layout, so monitor= is unavailable; pass region instead")
    wanted = str(name or "").strip().lower()
    if wanted in ("", "primary", "main"):
        for monitor in monitors:
            if getattr(monitor, "primary", False):
                return monitor.connector, (monitor.x, monitor.y, monitor.width, monitor.height)
        first = monitors[0]
        return first.connector, (first.x, first.y, first.width, first.height)
    for monitor in monitors:
        if monitor.connector.lower() == wanted:
            return monitor.connector, (monitor.x, monitor.y, monitor.width, monitor.height)
    for monitor in monitors:
        if wanted in monitor.connector.lower():
            return monitor.connector, (monitor.x, monitor.y, monitor.width, monitor.height)
    names = ", ".join(f"{m.connector} ({m.width}x{m.height} at {m.x},{m.y})" for m in monitors)
    raise ToolError(f"no monitor matches {name!r}; available: {names}")


def capture_shot(
    max_width: int = SHOT_MAX_WIDTH,
    region: tuple[float, float, float, float] | None = None,
    scale: float | None = None,
) -> dict:
    """Grab the desktop, preferring the screenshot portal over a pipewire stream.

    The portal Screenshot interface is a single round trip (~0.5s) and needs no
    long-lived session, so it is the default. The ScreenCast + GStreamer path
    stays as a fallback because it is the only one that can stream frames when
    capture permission exists but the screenshot interface is missing.

    ``region`` (logical pixels) crops before downscaling, which is what makes
    small UI text readable: a 900 px wide slice of the desktop stays sharp at
    1400 px of output, while the same output from a 4480 px desktop is mush.
    ``scale`` sets output pixels per logical pixel (1.0 = native size) and takes
    precedence over ``max_width``.
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

    current = layout()
    raw_width, raw_height = cu_display.png_size(path)
    # The portal hands back physical pixels; logical desktop size can be smaller
    # on HiDPI, and region/scale are both expressed in logical pixels.
    physical = (raw_width / current.width) if current.width and raw_width else 1.0
    origin = (0.0, 0.0)
    region_used = None

    if region is not None:
        x, y, w, h = region
        box = (
            round(x * physical),
            round(y * physical),
            max(1, round(w * physical)),
            max(1, round(h * physical)),
        )
        target = round(w * scale) if scale else max_width
        width, height = cu_display.crop_and_scale(path, box, max(1, int(target)))
        origin = (x, y)
        region_used = {"x": round(x), "y": round(y), "width": round(w), "height": round(h)}
        ratio = (w / width) if width else 1.0
    elif scale:
        width, height = cu_display.crop_and_scale(
            path, (0, 0, raw_width, raw_height), max(1, round(current.width * scale))
        )
        ratio = (current.width / width) if width else 1.0
    else:
        width, height = cu_display.downscale(path, max_width)
        ratio = (current.width / width) if width else 1.0

    LAST_SHOT.update({
        "path": path,
        "width": width,
        "height": height,
        "logical": (current.width, current.height),
        "origin": origin,
        "ratio": ratio,
        "at": time.time(),
    })
    info = {
        "path": path,
        "width": width,
        "height": height,
        "logical_width": current.width,
        "logical_height": current.height,
        "coordinate_scale": round(ratio, 6),
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "method": method,
    }
    if region_used:
        info["region"] = region_used
        info["origin_x"], info["origin_y"] = region_used["x"], region_used["y"]
    return info


# --------------------------------------------------------------------------- #
# tool implementations
# --------------------------------------------------------------------------- #
def _draw_marks(info: dict, limit: int) -> list[dict]:
    """Collect clickable nodes, express them in screenshot pixels, and draw them."""
    origin_x = float(info.get("origin_x", 0.0))
    origin_y = float(info.get("origin_y", 0.0))
    ratio = float(LAST_SHOT.get("ratio") or 1.0) or 1.0
    try:
        nodes = A11Y.actionable(limit=max(1, limit))
    except cu_a11y.A11yError:
        return []
    marks: list[dict] = []
    for node in nodes:
        rect = node.get("rect")
        if not rect:
            continue
        left = (rect["x"] - origin_x) / ratio
        top = (rect["y"] - origin_y) / ratio
        width = rect["width"] / ratio
        height = rect["height"] / ratio
        if left + width < 0 or top + height < 0:
            continue
        if left > info["width"] or top > info["height"]:
            continue
        if width < 6 or height < 6:
            continue
        marks.append({
            "id": len(marks) + 1,
            "ref": node["ref"],
            "role": node["role"],
            "name": node.get("name", ""),
            "x": round(left),
            "y": round(top),
            "width": round(width),
            "height": round(height),
            "colour": len(marks),
        })
    if marks:
        cu_display.annotate(info["path"], marks)
    return [{key: value for key, value in mark.items() if key != "colour"} for mark in marks]


def tool_screenshot(args: dict) -> dict:
    max_width = int(args.get("max_width", SHOT_MAX_WIDTH))
    region = _parse_region(args.get("region"))
    scale = args.get("scale")
    scale = float(scale) if scale else None
    if scale is not None and scale <= 0:
        raise ToolError("scale must be positive (1.0 = one screenshot pixel per logical pixel)")
    monitor_arg = args.get("monitor")
    monitor: dict | str | None = monitor_arg
    window_arg = args.get("window")
    window: dict | None = None
    if monitor_arg:
        if region is not None:
            raise ToolError("pass either monitor or region, not both: monitor covers the whole screen, region is a slice of the desktop")
        if args.get("ref"):
            raise ToolError("pass either monitor or ref, not both")
        connector, rect = _resolve_monitor(monitor_arg)
        region = rect
        if scale is None and max_width == SHOT_MAX_WIDTH and not args.get("max_width"):
            scale = 1.0
        current = layout()
        for item in getattr(current, "monitors", []) or []:
            if item.connector == connector:
                monitor = {"connector": connector, "x": item.x, "y": item.y,
                           "width": item.width, "height": item.height}
                break
    if window_arg:
        if region is not None:
            raise ToolError(
                "pass either window or region/monitor/ref, not both: window derives the region "
                "of one window"
            )
        if args.get("ref"):
            raise ToolError("pass either window or ref, not both")
        match = _resolve_window(str(window_arg), str(args.get("window_app") or ""))
        rect, source = _window_geometry(match)
        if rect is None:
            if source == "hidden":
                raise ToolError(
                    f"{match['title'] or match['app']} exists but is not on screen right now "
                    "(hidden, minimised or on another workspace); raise it with window_activate "
                    "first."
                )
            raise ToolError(
                f"{match['title'] or match['app']} is a native Wayland surface: neither AT-SPI nor "
                "X11 publishes its geometry, so it cannot be cropped by name. Capture its monitor "
                "(screenshot with monitor=...) or act on its elements (ui_find / click with text=..."
                ")."
            )
        region = (rect["x"], rect["y"], rect["width"], rect["height"])
        if scale is None and max_width == SHOT_MAX_WIDTH and not args.get("max_width"):
            scale = 1.0
        window = {"app": match["app"], "title": match["title"], "source": source}
    ref = args.get("ref")
    if ref:
        if region is not None:
            raise ToolError("pass either region or ref, not both")
        node = A11Y.describe(str(ref))
        rect = node.get("rect")
        if not rect:
            raise ToolError(f"element {ref} has no on-screen rectangle to crop to")
        region = (rect["x"], rect["y"], rect["width"], rect["height"])
        if scale is None:
            scale = 1.0
    info = capture_shot(max_width, region=region, scale=scale)
    if isinstance(monitor, dict):
        info["monitor"] = monitor
    if isinstance(window, dict):
        info["window"] = window
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
        # The ratio must follow the *visible* slice, not the whole desktop, so a
        # cropped capture still maps screenshot pixels back to logical ones.
        logical_width = (info.get("region") or {}).get("width") or info["logical_width"]
        info["coordinate_scale"] = round(logical_width / width, 6) if width else 1.0
        LAST_SHOT.update({"width": width, "height": height, "ratio": info["coordinate_scale"]})
    marks: list[dict] = []
    wanted_marks = bool(args.get("marks"))
    if wanted_marks:
        marks = _draw_marks(info, int(args.get("marks_limit", 40)))
        with open(info["path"], "rb") as handle:
            payload = handle.read()
    encoded = base64.b64encode(payload).decode("ascii")
    if info.get("region"):
        note = (
            f"screenshot {info['width']}x{info['height']} px saved at {info['path']}; it shows the "
            f"logical region x={info['origin_x']} y={info['origin_y']} "
            f"{info['region']['width']}x{info['region']['height']} of a "
            f"{info['logical_width']}x{info['logical_height']} desktop. "
            f"To click at a pixel of this image pass space='screenshot'; "
            f"otherwise add ({info['origin_x']}, {info['origin_y']}) and multiply by "
            f"{info['coordinate_scale']}."
        )
    else:
        note = (
            f"screenshot {info['width']}x{info['height']} px saved at {info['path']}; "
            f"logical desktop is {info['logical_width']}x{info['logical_height']}. "
            f"To click at a pixel of this image pass space='screenshot'; "
            f"otherwise multiply by {info['coordinate_scale']}."
        )
    if wanted_marks:
        info["marks"] = marks
        info["mark_count"] = len(marks)
        if marks:
            note += (
                f" {len(marks)} numbered boxes mark clickable elements; click one with "
                "click_ref({ref}) or by the coordinates listed in \"marks\" (screenshot space)."
            )
        else:
            # Wayland apps often publish no screen rectangles, so nothing can be
            # boxed; a list of refs is still useful because click_ref can act on them.
            try:
                clickable = [
                    {k: node[k] for k in ("ref", "role", "name", "actions") if k in node}
                    for node in A11Y.actionable(limit=max(1, min(int(args.get("marks_limit", 40)), 40)))
                ]
            except cu_a11y.A11yError:
                clickable = []
            info["clickable"] = clickable
            if clickable:
                note += (
                    f" No boxes were drawn because these windows publish no screen rectangles on"
                    f" Wayland, but {len(clickable)} clickable elements were found: pick a ref from"
                    " \"clickable\" and pass it to click_ref."
                )
            else:
                note += (
                    " No clickable element was found in this area"
                    " (a locked session hides most of the tree), so no boxes were drawn."
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
    roles = args.get("roles")
    if isinstance(roles, str):
        roles = [roles]
    if roles is not None and not isinstance(roles, list):
        raise ToolError("roles must be a list of role name fragments, e.g. [\"push button\", \"entry\"]")
    compact = bool(args.get("compact", False))
    snapshot = A11Y.snapshot(app=app, max_depth=depth, max_nodes=nodes,
                             roles=[str(item) for item in roles] if roles else None,
                             compact=compact)
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
    _guard_input("act on an accessibility node", args)
    ref = _require(args, "ref")
    action = args.get("action")
    return A11Y.action(ref, action)


def tool_ui_focus(args: dict) -> dict:
    _guard_input("move focus", args)
    ref = _require(args, "ref")
    return A11Y.focus(ref)


def tool_ui_set_text(args: dict) -> dict:
    _guard_input("set a widget value", args)
    ref = _require(args, "ref")
    text = args.get("text")
    if not isinstance(text, str):
        raise ToolError("'text' must be a string")
    return A11Y.set_text(ref, text)


def tool_click(args: dict) -> dict:
    _guard_input("click", args)
    text = args.get("text")
    if text and args.get("x") is None and args.get("y") is None:
        hits = A11Y.find(
            query=str(text),
            role=args.get("role"),
            app=args.get("app"),
            limit=5,
        )
        if not hits:
            raise ToolError(
                f"no element whose name or text matches {text!r}; "
                "try ui_find to see what is there, or click by coordinates"
            )
        best = hits[0]
        if not best.get("center") and not best.get("rect"):
            # Wayland toolkits publish no screen rectangle, so the widget's own
            # action is the only way in.
            try:
                done = A11Y.action(best["ref"], "click")
            except cu_a11y.A11yError:
                try:
                    done = A11Y.action(best["ref"], None)
                except cu_a11y.A11yError as error:
                    raise ToolError(
                        f"{text!r} matched {best.get('role')} {best['ref']}, but it has no "
                        f"on-screen rectangle and no clickable action ({error})"
                    ) from error
            return {
                "clicked": {
                    "ref": best["ref"],
                    "role": best.get("role"),
                    "matched": best.get("name") or str(text)[:60],
                    "via": "accessibility-action",
                    "action": (done or {}).get("action"),
                }
            }
        if not best.get("center"):
            raise ToolError(
                f"{text!r} matched {best.get('role')} {best['ref']}, which has neither a "
                "clickable action nor a usable rectangle"
            )
        button = _button_name(args.get("button", 1))
        count = max(1, min(int(args.get("count", 1)), 3))
        controller().click(best["center"][0], best["center"][1], button=button, count=count)
        return {
            "clicked": {
                "x": best["center"][0],
                "y": best["center"][1],
                "button": button,
                "count": count,
                "matched": best.get("name") or best.get("text", "")[:60],
                "role": best.get("role"),
                "ref": best.get("ref"),
            }
        }
    x, y = _point(args)
    x, y = _to_logical(x, y, str(args.get("space", "logical")))
    button = _button_name(args.get("button", 1))
    count = max(1, min(int(args.get("count", 1)), 3))
    controller().click(x, y, button=button, count=count)
    return {"clicked": {"x": round(x), "y": round(y), "button": button, "count": count}}


def tool_click_ref(args: dict) -> dict:
    """Click an accessibility node.

    Wayland apps rarely publish global screen rectangles, so a pointer click is not
    always possible: try the widget's own click action first and fall back to the
    pointer when the node does have a rectangle.
    """
    _guard_input("click", args)
    ref = str(_require(args, "ref"))
    node = A11Y.describe(ref)
    center = node.get("center")
    rect = node.get("rect")
    if not center and rect:
        center = [rect["x"] + rect["width"] // 2, rect["y"] + rect["height"] // 2]
    button = _button_name(args.get("button", 1))
    count = max(1, min(int(args.get("count", 1)), 3))
    prefer = args.get("via", "auto")
    failures: list[str] = []

    if prefer in ("auto", "action"):
        try:
            done = A11Y.action(ref, "click")
        except cu_a11y.A11yError:
            try:
                done = A11Y.action(ref, None)
            except cu_a11y.A11yError as error:
                done = None
                failures.append(str(error))
        if done is not None:
            return {
                "clicked": {
                    "ref": ref,
                    "role": node.get("role"),
                    "name": node.get("name", ""),
                    "via": "accessibility-action",
                    "action": done.get("action"),
                    "button": button,
                    "count": count,
                }
            }

    if not center:
        detail = f"element {ref} has no on-screen rectangle"
        if failures:
            detail = f"{detail} and no clickable action ({'; '.join(failures)})"
        raise ToolError(f"{detail}, so it cannot be clicked")
    controller().click(center[0], center[1], button=button, count=count)
    return {
        "clicked": {
            "x": round(center[0]),
            "y": round(center[1]),
            "button": button,
            "count": count,
            "ref": ref,
            "role": node.get("role"),
            "name": node.get("name", ""),
            "via": "pointer",
        }
    }


def tool_move(args: dict) -> dict:
    _guard_input("move the pointer", args)
    x, y = _point(args)
    x, y = _to_logical(x, y, str(args.get("space", "logical")))
    controller().move(x, y)
    return {"moved_to": {"x": round(x), "y": round(y)}}


def tool_drag(args: dict) -> dict:
    _guard_input("drag", args)
    start = (float(args.get("x1", 0)), float(args.get("y1", 0)))
    end = (float(args.get("x2", 0)), float(args.get("y2", 0)))
    space = str(args.get("space", "logical"))
    start = _to_logical(*start, space)
    end = _to_logical(*end, space)
    duration = max(100, min(int(args.get("duration_ms", 450)), 5000))
    controller().drag(start[0], start[1], end[0], end[1], duration_ms=duration)
    return {"dragged": {"from": [round(v) for v in start], "to": [round(v) for v in end]}}


def tool_scroll(args: dict) -> dict:
    _guard_input("scroll", args)
    dx = int(args.get("dx", 0))
    dy = int(args.get("dy", 0))
    if not dx and not dy:
        raise ToolError("pass a non-zero dx and/or dy (positive dy scrolls down)")
    controller().scroll(dx=dx, dy=dy)
    return {"scrolled": {"dx": dx, "dy": dy}}


def tool_pointer_button(args: dict) -> dict:
    _guard_input("press a mouse button", args)
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
    _guard_input("press a key", args)
    combo = _require(args, "combo")
    controller().key(str(combo))
    return {"pressed": combo}


def tool_type_text(args: dict) -> dict:
    _guard_input("type text", args)
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
        frame = _first_frame_obj(A11Y.resolve(app["ref"]))
        if frame is None:
            windows.append({"app": app["name"], "title": "", "ref": app["ref"], "pid": app["pid"]})
            continue
        title = frame.get_name() or ""
        windows.append({
            "app": app["name"],
            "title": title,
            "ref": app["ref"],
            "pid": app["pid"],
            "active": _window_is_active(frame),
        })
        del node
    return {"windows": windows, "count": len(windows)}


def tool_window_activate(args: dict) -> dict:
    _guard_input("activate a window", args)
    title = str(args.get("title") or "").strip()
    app = str(args.get("app") or "").strip()
    if not title and not app:
        raise ToolError("window_activate needs either a title substring or an app name")
    rows = _frame_rows()
    match = _match_window(rows, title, app)
    if match is None:
        known = sorted({f"{row['app']}: {row['title']}" for row in rows if row["title"]})[:8]
        raise ToolError(
            "no window matches "
            + (f"title {title!r} " if title else "")
            + (f"app {app!r}" if app else "")
            + ("; known windows: " + "; ".join(known) if known else "")
        )
    window = {"app": match["app"], "title": match["title"]}
    if match["active"]:
        return {"activated": True, "method": "already-active", "window": window, "tried": []}

    tried: list[dict] = []
    needle = title or match["title"]

    # XWayland windows answer to xdotool; native Wayland ones never show up here.
    if needle:
        if not os.environ.get("DISPLAY"):
            tried.append({"method": "xdotool", "result": "no DISPLAY in this session"})
        else:
            code, out = _xdotool("search", "--name", needle)
            ids = [line for line in out.splitlines() if line.strip().isdigit()] if code == 0 else []
            if not ids:
                tried.append({"method": "xdotool", "result": "no X11 match (native Wayland window)"})
            for wid in ids[:4]:
                _xdotool("windowactivate", wid)
                if _wait_active(match["title"], 1.2):
                    return {"activated": True, "method": "xdotool", "window": window, "tried": tried}
            else:
                if ids:
                    tried.append({"method": "xdotool", "result": "activate did not raise it"})

    try:
        iface = match["acc"].get_action_iface()
        if iface is not None and iface.get_n_actions() > 0:
            iface.do_action(0)
            if _wait_active(match["title"], 1.0):
                return {"activated": True, "method": "atspi", "window": window, "tried": tried}
            tried.append({"method": "atspi", "result": "default action did not raise it"})
        else:
            tried.append({"method": "atspi", "result": "frame exposes no actions"})
    except Exception as error:  # noqa: BLE001
        tried.append({"method": "atspi", "result": str(error)})

    cycles = max(1, min(int(args.get("max_cycles", WINDOW_ACTIVATE_MAX_CYCLES)), 30))
    # GNOME binds Alt+Tab to switching windows *inside* the current application
    # and Super+Tab to switching applications, so both are worth trying. A
    # combination that changes nothing on its first press is not bound here (or
    # the application has a single window), so move on instead of cycling.
    for combo in ("alt+Tab", "super+Tab"):
        try:
            before = _active_titles()
            for index in range(cycles):
                controller().key(combo)
                time.sleep(0.35)
                if _is_active(match["title"]):
                    return {"activated": True, "method": combo.lower().replace("+", "-"), "window": window, "tried": tried}
                now = _active_titles()
                if now != before:
                    before = now
                    continue
                if index == 0:
                    tried.append({"method": combo, "result": "no effect here (not bound, or the application has a single window)"})
                    break
            else:
                tried.append({"method": combo, "result": f"{cycles} cycles without reaching it"})
        except cu_input.InputError as error:
            tried.append({"method": combo, "result": str(error)})
            break
        except Exception as error:  # noqa: BLE001
            tried.append({"method": combo, "result": str(error)})

    return {
        "activated": False,
        "method": "none",
        "window": window,
        "tried": tried,
        "hint": (
            "none of the three strategies raised it: X11 sees only XWayland surfaces, the "
            "accessibility default action is a no-op for most toolkits, and the keyboard "
            "fallback needs uinput plus a matching keybinding (Alt+Tab switches windows of "
            "the current application, Super+Tab switches applications). Raise it by hand, or "
            "pass a more distinctive title"
        ),
    }


WAIT_CONDITIONS = ("window", "window_active", "ui", "file", "screenshot_changed", "idle_at_least")


def _region_signature(region: tuple[float, float, float, float] | None, cells: int = 12) -> list[int]:
    """A tiny luminance grid of one slice of the screen, for "did it change?" checks."""
    name = f"sig-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.png"
    path = os.path.join(cu_display.SHOT_DIR, name)
    try:
        cu_display.portal_screenshot(path)
        current = layout()
        raw_width, raw_height = cu_display.png_size(path)
        physical = (raw_width / current.width) if current.width and raw_width else 1.0
        if region is not None:
            x, y, w, h = region
            box = (
                round(x * physical),
                round(y * physical),
                max(1, round(w * physical)),
                max(1, round(h * physical)),
            )
        else:
            box = (0, 0, raw_width, raw_height)
        cu_display.crop_and_scale(path, box, max(4, cells))
        return cu_display.gray_grid(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _grid_distance(left: list[int], right: list[int]) -> float:
    if not left or not right or len(left) != len(right):
        return 1.0
    return sum(abs(a - b) for a, b in zip(left, right)) / (len(left) * 255.0)


def _check_condition(condition: dict, state: dict | None = None) -> tuple[bool, dict]:
    state = state if state is not None else {}
    for key in WAIT_CONDITIONS:
        if key not in condition:
            continue
        if key == "window":
            spec = condition["window"]
            if isinstance(spec, str):
                spec = {"title": spec}
            if not isinstance(spec, dict):
                raise ToolError("condition.window must be a title string or an object")
            want = bool(spec.get("exists", True))
            rows = _frame_rows()
            hit = _match_window(rows, str(spec.get("title") or ""), str(spec.get("app") or ""))
            return (hit is not None) == want, {"windows": len(rows), "found": hit is not None}
        if key == "window_active":
            spec = condition["window_active"]
            needle = str(spec.get("title") if isinstance(spec, dict) else spec or "").lower()
            if not needle:
                raise ToolError("condition.window_active needs a title substring")
            active = [row["title"] for row in _frame_rows() if row["active"]]
            hit = any(needle in (title or "").lower() for title in active)
            return hit, {"active": active[:6]}
        if key == "ui":
            spec = condition["ui"]
            if not isinstance(spec, dict):
                raise ToolError("condition.ui must be an object such as {app, name, role}")
            hits = A11Y.find(
                query=spec.get("name"),
                role=spec.get("role"),
                app=spec.get("app"),
                limit=3,
                timeout_s=2.0,
            )
            return len(hits) > 0, {"matches": len(hits)}
        if key == "file":
            spec = condition["file"]
            if isinstance(spec, str):
                spec = {"path": spec}
            if not isinstance(spec, dict) or not spec.get("path"):
                raise ToolError('condition.file must be a path or {"path": "...", "min_bytes": 0, "stable_ms": 0}')
            target = os.path.expanduser(str(spec["path"]))
            want = bool(spec.get("exists", True))
            try:
                size = os.path.getsize(target)
                exists = True
            except OSError:
                exists, size = False, 0
            if want is False:
                return (not exists), {"path": target, "exists": exists}
            if not exists:
                state.pop(f"file:{target}", None)
                return False, {"path": target, "exists": False}
            minimum = int(spec.get("min_bytes") or 0)
            if size < minimum:
                state.pop(f"file:{target}", None)
                return False, {"path": target, "exists": True, "bytes": size, "min_bytes": minimum}
            stable_ms = int(spec.get("stable_ms") or 0)
            if stable_ms <= 0:
                return True, {"path": target, "exists": True, "bytes": size}
            marker = state.get(f"file:{target}")
            now = time.monotonic()
            if not marker or marker[0] != size:
                state[f"file:{target}"] = (size, now)
                return False, {"path": target, "exists": True, "bytes": size, "stable_for_ms": 0}
            settled = int((now - marker[1]) * 1000)
            return settled >= stable_ms, {
                "path": target, "exists": True, "bytes": size, "stable_for_ms": settled,
            }
        if key == "screenshot_changed":
            spec = condition["screenshot_changed"]
            if spec is True or spec is None:
                spec = {}
            if not isinstance(spec, dict):
                raise ToolError('condition.screenshot_changed must be an object such as {"region": {...}, "threshold": 0.02}')
            region = _parse_region(spec.get("region"))
            if region is None and spec.get("monitor"):
                _connector, rect = _resolve_monitor(spec.get("monitor"))
                region = rect
            threshold = float(spec.get("threshold", 0.02))
            cells = int(spec.get("cells", 12))
            grid = _region_signature(region, cells)
            if not grid:
                return False, {"error": "could not read the screen slice"}
            baseline = state.get("signature")
            if baseline is None:
                state["signature"] = grid
                return False, {"baseline": True, "cells": len(grid)}
            distance = _grid_distance(baseline, grid)
            return distance >= threshold, {"difference": round(distance, 4), "threshold": threshold}
        idle = cu_sys.idle_ms()
        want_idle = int(condition["idle_at_least"])
        return (idle is not None and idle >= want_idle), {"idle_ms": idle}
    raise ToolError(
        "condition must contain one of: " + ", ".join(WAIT_CONDITIONS) + " (got " + repr(sorted(condition)) + ")"
    )


def tool_wait_until(args: dict) -> dict:
    condition = args.get("condition")
    if not isinstance(condition, dict) or not condition:
        raise ToolError(
            "wait_until needs a condition object, for example "
            '{"window": {"title": "Build finished"}} or {"window_active": "Chrome"}'
        )
    timeout_ms = max(100, min(int(args.get("timeout_ms", 15000)), 120000))
    interval_ms = max(80, min(int(args.get("interval_ms", 300)), 5000))
    started = time.monotonic()
    deadline = started + timeout_ms / 1000.0
    polls = 0
    detail: dict = {}
    satisfied = False
    state: dict = {}
    while True:
        polls += 1
        try:
            satisfied, detail = _check_condition(condition, state)
        except ToolError:
            raise
        except Exception as error:  # noqa: BLE001
            detail = {"error": str(error)}
            satisfied = False
        if satisfied or time.monotonic() >= deadline:
            break
        time.sleep(interval_ms / 1000.0)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        "satisfied": satisfied,
        "waited_ms": elapsed_ms,
        "polls": polls,
        "timeout_ms": timeout_ms,
        "condition": condition,
        "state": detail,
    }


def _window_geometry(match: dict) -> tuple[dict | None, str]:
    """Best-effort screen rectangle for a window, plus where it came from.

    AT-SPI is asked first because GTK3/Qt windows publish their toplevel
    rectangle there. XWayland windows are then measured through xdotool, but only
    while they are actually on screen: a hidden or minimised window keeps its old
    geometry in X11, and reporting that would aim a capture at bare wallpaper.
    Native Wayland surfaces publish nothing and X11 cannot see them, so the
    caller gets (None, "unknown") and has to work around it.
    """
    acc = match.get("acc")
    if acc is not None:
        rect = cu_a11y.extents(acc)
        if rect:
            return (rect, "atspi")
    title = match.get("title") or ""
    if not title:
        return (None, "unknown")
    rc, out = _xdotool("search", "--onlyvisible", "--name", title)
    if rc != 0 or not out:
        rc_all, out_all = _xdotool("search", "--name", title)
        if rc_all == 0 and out_all:
            return (None, "hidden")
        return (None, "unknown")
    # A title search also matches helper and popup surfaces (Bitwarden has 10x10
    # ones), so take the biggest rectangle instead of the first id.
    best: dict | None = None
    for window_id in out.splitlines()[:12]:
        rc2, geometry = _xdotool("getwindowgeometry", "--shell", window_id.strip())
        if rc2 != 0:
            continue
        values: dict[str, str] = {}
        for line in geometry.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
        try:
            rect = {
                "x": int(values["X"]),
                "y": int(values["Y"]),
                "width": int(values["WIDTH"]),
                "height": int(values["HEIGHT"]),
            }
        except (KeyError, ValueError):
            continue
        if rect["width"] < 50 or rect["height"] < 50:
            continue
        if best is None or rect["width"] * rect["height"] > best["width"] * best["height"]:
            best = rect
    if best is not None:
        return (best, "x11")
    return (None, "unknown")


def _resolve_window(title: str, app: str) -> dict:
    """Match one window or raise a ToolError listing what is open."""
    match = _match_window(_frame_rows(), title, app)
    if match is None:
        raise ToolError(
            f"no window matches {title or app!r}; call window_list to see the open windows"
        )
    return match


def tool_window_rect(args: dict) -> dict:
    title = str(args.get("title") or "").strip()
    app = str(args.get("app") or "").strip()
    if not title and not app:
        raise ToolError("pass title (a window title substring) or app (an application name)")
    match = _resolve_window(title, app)
    rect, source = _window_geometry(match)
    payload: dict = {
        "window": {"app": match["app"], "title": match["title"], "active": match["active"]},
        "rect": rect,
        "source": source,
    }
    if rect is None:
        if source == "hidden":
            payload["hint"] = (
                "this window exists but is not on screen right now (hidden to the tray, "
                "minimised, or on another workspace), so there is no rectangle to report. "
                "Raise it first with window_activate, then ask again."
            )
        else:
            payload["hint"] = (
                "this window is a native Wayland surface: neither AT-SPI nor X11 publishes its "
                "geometry, so it cannot be aimed at by name. Capture the monitor it lives on "
                "(screenshot with monitor=...) and crop by eye, or act on its elements with "
                "ui_find / click (text=...)."
            )
    return payload


def _first_frame_obj(app):
    """The first frame child of an application, or None."""
    if app is None:
        return None
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
                return child
        except Exception:  # noqa: BLE001
            continue
    return None


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


# --------------------------------------------------------------------------- #
# Windows: enumerate frames, raise one, wait for conditions
# --------------------------------------------------------------------------- #

WINDOW_ACTIVATE_MAX_CYCLES = int(os.environ.get("DSH_LINUX_CU_MAX_TAB_CYCLES", "12"))


def _xdotool(*argv: str, timeout: float = 3.0) -> tuple[int, str]:
    """Run xdotool against the XWayland display; (127, "") when unavailable."""
    if not os.environ.get("DISPLAY"):
        return (127, "")
    try:
        done = subprocess.run(
            ["xdotool", *argv], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return (127, "")
    return (done.returncode, (done.stdout or "").strip())


def _window_is_active(acc) -> bool:
    """True when the toolkit marks this window ACTIVE (raised/focused).

    cu_a11y.STATE_FLAGS stops at FOCUSED, and most toolkits set ACTIVE on the
    toplevel frame instead, so the flag is queried directly here.
    """
    try:
        state_set = acc.get_state_set()
    except Exception:  # noqa: BLE001
        return False
    for flag in ("ACTIVE", "FOCUSED"):
        value = getattr(cu_a11y.Atspi.StateType, flag, None)
        if value is None:
            continue
        try:
            if state_set.contains(value):
                return True
        except Exception:  # noqa: BLE001
            return False
    return False


def _frame_rows() -> list[dict]:
    """Every top-level window frame the accessibility bus can see."""
    rows: list[dict] = []
    for app in A11Y.apps(max_age=1.5):
        try:
            target = A11Y.resolve(app["ref"])
            if target is None:
                continue
            count = target.get_child_count()
        except Exception:  # noqa: BLE001
            continue
        for index in range(min(count, 4)):
            try:
                child = target.get_child_at_index(index)
                if child is None or (child.get_role_name() or "") != "frame":
                    continue
            except Exception:  # noqa: BLE001
                continue
            active = _window_is_active(child)
            rows.append(
                {
                    "app": app.get("name") or "",
                    "title": child.get_name() or "",
                    "ref": app.get("ref"),
                    "pid": app.get("pid"),
                    "active": active,
                    "acc": child,
                }
            )
            break
    return rows


def _match_window(rows: list[dict], title: str, app: str) -> dict | None:
    """Prefer an exact title, then a title substring, then the app name."""
    needle = (title or "").strip().lower()
    app_needle = (app or "").strip().lower()
    if needle:
        for row in rows:
            if (row["title"] or "").lower() == needle:
                return row
        for row in rows:
            if needle in (row["title"] or "").lower():
                return row
        for row in rows:
            if needle in (row["app"] or "").lower():
                return row
    if app_needle:
        for row in rows:
            if (row["app"] or "").lower() == app_needle:
                return row
        for row in rows:
            if app_needle in (row["app"] or "").lower():
                return row
    return None


def _active_titles() -> set:
    """Titles of the windows the accessibility tree currently reports as ACTIVE."""
    return {row["title"] for row in _frame_rows() if row["active"]}


def _is_active(title: str) -> bool:
    needle = (title or "").strip().lower()
    if not needle:
        return False
    for row in _frame_rows():
        if not row["active"]:
            continue
        if (row["title"] or "").lower() == needle or needle in (row["title"] or "").lower():
            return True
    return False


def _wait_active(title: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _is_active(title):
            return True
        time.sleep(0.12)
    return _is_active(title)



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


def _unlock_status() -> dict:
    """Never let a locked keyring take the whole status call down."""
    try:
        return cu_unlock.status()
    except Exception as error:  # noqa: BLE001 - diagnostics must not raise
        return {"unlock_policy": cu_unlock.policy(), "password_stored": None, "error": str(error)}


def tool_desktop_status(args: dict) -> dict:
    status: dict = {"server": {"name": SERVER_NAME, "version": SERVER_VERSION, "log": LOG_PATH}}
    status["session"] = {
        "type": os.environ.get("XDG_SESSION_TYPE", ""),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
        "locked": cu_sys.screen_locked(),
        "idle_ms": cu_sys.idle_ms(),
        "input_guard_ms": IDLE_GUARD_MS,
        "unlock": _unlock_status(),
    }
    try:
        current = layout()
        status["display"] = current.describe()
    except cu_display.PortalError as error:
        status["display"] = {"error": str(error)}
    try:
        active = controller()
        if args.get("reset_input"):
            active.reset_input()
            status["input_reset"] = True
        status["input"] = {"backend": active.backend_name, "ready": True}
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

ALLOW_ACTIVE_PROP = {
    "type": "boolean",
    "default": False,
    "description": (
        "Set true when the user just asked you to drive the desktop: it skips the idle guard, "
        "which otherwise refuses injected input while they are using the keyboard or pointer."
    ),
}

# Tools that inject input all accept the same escape hatch; adding it here keeps
# the schema in one place instead of ten copies.
_INJECTING_TOOLS = {
    "click_ref",
    "click",
    "move",
    "drag",
    "scroll",
    "pointer_button",
    "key",
    "type_text",
    "ui_action",
    "ui_focus",
    "ui_set_text",
}

def tool_unlock_screen(args: dict) -> dict:
    confirm = str(args.get("confirm") or "").strip().lower()
    if confirm != "unlock":
        raise ToolError(
            "unlock_screen requires confirm='unlock'. It types your stored password into the "
            "lock screen, so only call it when the user explicitly asked you to unlock now."
        )
    result = cu_unlock.unlock(controller())
    log(
        "unlock_screen: unlocked={} attempts={}".format(
            result.get("unlocked"), result.get("attempts")
        )
    )
    return result


SEQUENCE_MAX_STEPS = int(os.environ.get("DSH_LINUX_CU_SEQUENCE_MAX_STEPS", "25"))
SEQUENCE_TEXT_LIMIT = 1200


def _sequence_step_report(position: int, op: str, ok: bool, elapsed_ms: float, error: str | None = None) -> dict:
    entry = {"step": position, "op": op, "ok": ok, "ms": int(elapsed_ms)}
    if error:
        entry["error"] = error
    return entry


def tool_sequence(args: dict) -> dict:
    steps = args.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ToolError('steps must be a non-empty list of {"op": "...", "args": {...}} objects')
    if len(steps) > SEQUENCE_MAX_STEPS:
        raise ToolError(f"too many steps ({len(steps)}); the limit is {SEQUENCE_MAX_STEPS} per call")
    stop_on_error = bool(args.get("stop_on_error", True))
    inject_default = bool(args.get("allow_active_input", False))
    index = tool_index()
    report: list[dict] = []
    last_content: list[dict] | None = None
    stopped_at: int | None = None
    started = time.monotonic()
    for position, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise ToolError(f'step {position} must be an object like {{"op": "click", "args": {{...}}}}')
        op = step.get("op")
        if not isinstance(op, str) or not op.strip():
            raise ToolError(f'step {position} needs an "op" name')
        op = op.strip()
        if op == "sequence":
            raise ToolError("a sequence cannot contain another sequence")
        step_args = step.get("args") or {}
        if not isinstance(step_args, dict):
            raise ToolError(f"step {position} args must be an object")
        step_args = dict(step_args)
        if op not in index:
            report.append(_sequence_step_report(position, op, False, 0.0, f"unknown tool; available: {sorted(index)}"))
            if stop_on_error:
                stopped_at = position
                break
            continue
        if inject_default and op in _INJECTING_TOOLS:
            step_args.setdefault("allow_active_input", True)
        began = time.monotonic()
        payload = call_tool(op, step_args)
        took = (time.monotonic() - began) * 1000
        content = payload.get("content") or []
        if payload.get("isError"):
            message = " ".join(
                str(block.get("text", "")) for block in content if isinstance(block, dict)
            ).strip()
            report.append(_sequence_step_report(position, op, False, took, message or "failed"))
            if stop_on_error:
                stopped_at = position
                break
            continue
        report.append(_sequence_step_report(position, op, True, took))
        last_content = content
    elapsed = int((time.monotonic() - started) * 1000)
    ok = stopped_at is None and all(entry["ok"] for entry in report)
    summary = {
        "ok": ok,
        "steps_total": len(steps),
        "steps_run": len(report),
        "stopped_at": stopped_at,
        "elapsed_ms": elapsed,
        "steps": report,
    }
    boxes = [block for block in (last_content or []) if isinstance(block, dict)]
    text_tail = " ".join(str(block.get("text", "")) for block in boxes if block.get("type") == "text").strip()
    if text_tail:
        summary["last_text"] = text_tail[:SEQUENCE_TEXT_LIMIT]
    content_out: list[dict] = [{"type": "text", "text": json.dumps(summary, ensure_ascii=False, default=str)}]
    content_out.extend(block for block in boxes if block.get("type") == "image")
    return {"content": content_out, "isError": not ok}


TOOLS: list[dict] = [
    {
        "name": "screenshot",
        "description": (
            "Capture the desktop (or one region of it) and return a PNG image plus its size and path. "
            "Use it to see the screen before deciding where to click. Pass region {x, y, width, height} "
            "in logical pixels when you only need one window or one corner: a cropped capture stays "
            "sharp, while the whole 4K desktop scaled down does not. The desktop asks for a one-time "
            "screen-capture approval; after that every capture is silent and takes about half a second."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_width": {
                    "type": "integer",
                    "default": SHOT_MAX_WIDTH,
                    "description": "Downscale the image to this pixel width (0 keeps native resolution). Ignored when scale is given.",
                },
                "region": {
                    "type": "object",
                    "description": (
                        "Capture only this rectangle, in logical desktop pixels. Cropping before "
                        "downscaling is what keeps small UI text readable."
                    ),
                    "properties": {
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "width": {"type": "number"},
                        "height": {"type": "number"},
                    },
                    "required": ["x", "y", "width", "height"],
                },
                "scale": {
                    "type": "number",
                    "description": (
                        "Output pixels per logical pixel: 1.0 keeps native size, 2.0 doubles it. "
                        "Takes precedence over max_width."
                    ),
                },
                "window": {
                    "type": "string",
                    "description": (
                        "Window title substring (e.g. \"嘉立创\"). Crops to that window's rectangle, "
                        "which is exact for GTK3/Qt/X11 windows and unavailable for some native "
                        "Wayland surfaces; conflicts with region/monitor/ref."
                    ),
                },
                "window_app": {
                    "type": "string",
                    "description": "Application name, used with window when the title is unknown.",
                },
                "monitor": {
                    "type": "string",
                    "description": (
                        "Capture one whole monitor by connector name (e.g. eDP-1, HDMI-1) or \"primary\". "
                        "Region coordinates are whole-desktop, so this saves you from adding the monitor "
                        "offset by hand and cropping the wrong screen. Cannot be combined with region or ref."
                    ),
                },
                "ref": {
                    "type": "string",
                    "description": (
                        "Crop to one accessibility node (a ref from ui_find/ui_tree) instead of a "
                        "region. Great for reading a single widget without guessing its rectangle."
                    ),
                },
                "marks": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Draw numbered boxes over the clickable elements in the capture and return "
                        "them as \"marks\": [{id, ref, role, name, x, y, width, height}] in screenshot "
                        "pixels. Click one with click_ref or by its coordinates, so no guessing."
                    ),
                },
                "marks_limit": {
                    "type": "integer",
                    "default": 40,
                    "minimum": 1,
                    "maximum": 120,
                    "description": "How many clickable elements to label at most.",
                },
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
            "action nodes. Bounded by max_depth and max_nodes. Pass roles to keep only interesting "
            "kinds of node (ancestors collapse to a bare ref/role/name path), and compact to drop "
            "rect/states/text: a filtered compact tree is often 10x cheaper than the full one. "
            "Chromium/Electron/Qt apps usually expose almost nothing; use screenshot for those."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Application name as shown by ui_apps (substring match)."},
                "max_depth": {"type": "integer", "default": 3, "minimum": 1, "maximum": 8},
                "max_nodes": {"type": "integer", "default": 220, "minimum": 10, "maximum": 1200},
                "roles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Keep only nodes whose role contains one of these fragments, e.g. "
                        "[\"push button\", \"entry\", \"menu item\"]. Case-insensitive."
                    ),
                },
                "compact": {
                    "type": "boolean",
                    "default": False,
                    "description": "Drop rect, states and text previews; keep ref, role, name and actions.",
                },
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
        "description": (
            "Click at (x, y), or pass text instead of coordinates to click the element whose name "
            "or text matches. Supports button 1-3 and up to triple clicks."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "text": {
                    "type": "string",
                    "description": (
                        "Click the first element whose accessible name or text contains this string "
                        "instead of using coordinates. Ignored when x and y are given."
                    ),
                },
                "role": {"type": "string", "description": "With text: restrict to this role fragment."},
                "app": {"type": "string", "description": "With text: restrict to this application."},
                "button": {"type": "integer", "default": 1, "description": "1 left, 2 middle, 3 right."},
                "count": {"type": "integer", "default": 1, "minimum": 1, "maximum": 3},
                "space": SPACE_PROP,
            },
        },
        "handler": tool_click,
    },
    {
        "name": "click_ref",
        "description": (
            "Click the centre of one accessibility element by ref (from ui_find, ui_tree, or the "
            "marks list of an annotated screenshot). The reliable way to click: no coordinates."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Element ref, e.g. \"e42\"."},
                "button": {"type": "integer", "default": 1, "description": "1 left, 2 middle, 3 right."},
                "count": {"type": "integer", "default": 1, "minimum": 1, "maximum": 3},
            },
            "required": ["ref"],
        },
        "handler": tool_click_ref,
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
        "name": "window_rect",
        "description": (
            "Report the on-screen rectangle of one window, so a capture or a click can be aimed "
            "at it instead of guessed. Ask by title (substring) or app name; call window_list "
            "first when unsure. GTK3/Qt/X11 windows publish their geometry; some native Wayland "
            "surfaces (Chrome, GTK4) do not, and then rect is null with a hint on what to do."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Case-insensitive substring of the window title."},
                "app": {"type": "string", "description": "Application name, used when the title is unknown."},
            },
        },
        "handler": tool_window_rect,
    },
    {
        "name": "window_activate",
        "description": (
            "Raise a window by title substring (or app name) and report which method worked: "
            "xdotool for XWayland surfaces, the AT-SPI default action, or Alt+Tab cycling for "
            "native Wayland windows."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Case-insensitive substring of the window title."},
                "app": {"type": "string", "description": "Application name, used when the title is unknown."},
                "max_cycles": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 30,
                    "default": 12,
                    "description": "How many Alt+Tab presses to try before giving up.",
                },
                "allow_active_input": ALLOW_ACTIVE_PROP,
            },
        },
        "handler": tool_window_activate,
    },
    {
        "name": "wait_until",
        "description": (
            "Poll the desktop until a condition holds, instead of sleeping a fixed time: a window "
            "appears or disappears, a window becomes active, an accessibility node shows up, a file "
            "grows to a size and stops changing (downloads), the pixels of a region change (a spinner "
            "ending, a page loading) with the screenshot_changed condition, or the user has been idle long "
            "enough. Returns satisfied=false on timeout with the last state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "condition": {
                    "type": "object",
                    "description": (
                        'One of: {"window": {"title": "...", "app": "...", "exists": true}}, '
                        '{"window_active": "Chrome"}, {"ui": {"app": "...", "name": "...", "role": "..."}}, '
                        '{"file": {"path": "~/Downloads/x.zip", "min_bytes": 1048576, "stable_ms": 800}}, '
                        '{"screenshot_changed": {"region": {"x": 0, "y": 0, "width": 600, "height": 400}, "threshold": 0.02}}, '
                        '{"idle_at_least": 60000}. The first condition key wins; file and screenshot_changed '
                        'keep their own baseline between polls, so one call can wait for a download to finish '
                        'or a spinner to go away without a sleep.'
                    ),
                },
                "timeout_ms": {"type": "integer", "minimum": 100, "maximum": 120000, "default": 15000},
                "interval_ms": {"type": "integer", "minimum": 80, "maximum": 5000, "default": 300},
            },
            "required": ["condition"],
        },
        "handler": tool_wait_until,
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
        "inputSchema": {
            "type": "object",
            "properties": {
                "reset_input": {
                    "type": "boolean",
                    "description": (
                        "Recreate the virtual input devices before reporting. Use it after the "
                        "desktop session restarted (log out and back in): the compositor can stop "
                        "honouring pointer devices that were created before the restart."
                    ),
                },
            },
        },
        "handler": tool_desktop_status,
    },
    {
        "name": "sequence",
        "description": (
            "Run several tools in one call and get a compact per-step report plus the last step's "
            "output. Use it for a fixed multi-step flow (raise a window, click, type, screenshot): it "
            "saves one model round trip per step, and images from the final step are passed through. "
            "Steps run in order and a failing step stops the run unless stop_on_error is false."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "description": (
                        'Ordered steps, e.g. [{"op": "click", "args": {"x": 40, "y": 30}}, '
                        '{"op": "type_text", "args": {"text": "hello"}}]'
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {"type": "string", "description": "Tool name, e.g. click, type_text, screenshot"},
                            "args": {"type": "object", "description": "Arguments for that tool"},
                        },
                        "required": ["op"],
                    },
                },
                "stop_on_error": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Stop at the first failing step (default). Set false to keep going and report "
                        "every failure, which is useful when a step may legitimately fail."
                    ),
                },
                "allow_active_input": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Apply allow_active_input=true to every injecting step of the run, instead of "
                        "repeating it in each step's args."
                    ),
                },
            },
            "required": ["steps"],
        },
        "handler": tool_sequence,
    },
]

# Every input-injecting tool advertises the idle-guard escape hatch.
for _spec in TOOLS:
    if _spec["name"] in _INJECTING_TOOLS:
        _spec["inputSchema"].setdefault("properties", {})["allow_active_input"] = ALLOW_ACTIVE_PROP


UNLOCK_TOOL: dict = {
    "name": "unlock_screen",
    "description": (
        "Type the unlock password kept in the GNOME keyring into the lock screen and press Return. "
        "It only exists while the user has switched unlocking on; with the default policy the tool "
        "is not listed at all. The password is never sent to the model, never read from a chat, and "
        "never written to a config file or the log."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "confirm": {
                "type": "string",
                "enum": ["unlock"],
                "description": "Must be literally 'unlock'; a guard against accidental calls.",
            }
        },
        "required": ["confirm"],
    },
    "handler": tool_unlock_screen,
}


def visible_tools() -> list[dict]:
    """Tools the model may see right now.

    ``unlock_screen`` is opt-in per user setting, so when unlocking is off the
    model does not even learn that the capability exists.
    """
    specs = list(TOOLS)
    if cu_unlock.unlock_enabled():
        specs.append(UNLOCK_TOOL)
    return specs


def tool_index() -> dict[str, dict]:
    return {spec["name"]: spec for spec in visible_tools()}


def call_tool(name: str, arguments: dict) -> dict:
    index = tool_index()
    spec = index.get(name)
    if spec is None:
        raise ToolError(f"unknown tool {name!r}; available: {sorted(index)}")
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
                    for spec in visible_tools()
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


def _gsettings_list(schema: str, key: str) -> list[str] | None:
    """Read a list-valued gsettings key ("['<Super>Tab', '<Alt>Tab']")."""
    try:
        result = subprocess.run(
            ["gsettings", "get", schema, key],
            capture_output=True, text=True, timeout=4.0, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw.startswith("[") or not raw.endswith("]"):
        return None
    inner = raw[1:-1].strip()
    if not inner:
        return []
    return [part.strip().strip("'\"") for part in inner.split(",")]


def _keybinding_checks(checks: list) -> None:
    """Explain which window-switch shortcut window_activate can actually use."""
    schema = "org.gnome.desktop.wm.keybindings"
    windows = _gsettings_list(schema, "switch-windows")
    apps = _gsettings_list(schema, "switch-applications")
    if windows is None and apps is None:
        _check(
            checks,
            "keybindings",
            "warn",
            "window-switch keybindings are unreadable",
            "gsettings did not answer; window_activate can still raise XWayland windows",
            "install libglib2.0-bin (gsettings) to make the keyboard fallback verifiable",
        )
        return
    windows = windows or []
    apps = apps or []
    has_alt = any("Alt" in item and "Tab" in item for item in windows)
    has_super = any("Super" in item and "Tab" in item for item in apps)
    if has_alt and has_super:
        _check(
            checks,
            "keybindings",
            "ok",
            "both window-switch shortcuts are bound",
            "Alt+Tab switches windows of one application, Super+Tab switches applications",
        )
    elif has_super:
        _check(
            checks,
            "keybindings",
            "ok",
            "Super+Tab is bound to switch applications",
            "Alt+Tab is not bound, so window_activate goes straight to Super+Tab",
        )
    elif has_alt:
        _check(
            checks,
            "keybindings",
            "warn",
            "only Alt+Tab is bound",
            "Alt+Tab switches windows inside the current application, so raising another application's window may fail",
            "add a Super+Tab binding to switch-applications, or drag the window forward by hand",
        )
    else:
        _check(
            checks,
            "keybindings",
            "warn",
            "no window-switch shortcut is bound",
            "window_activate can only rely on xdotool and the accessibility default action",
            "bind switch-windows to <Alt>Tab in Settings -> Keyboard -> View and Customise Shortcuts",
        )


def _sync_check(checks: list) -> None:
    """Report whether this running copy is in step with the installed package."""
    if not os.path.exists(SOURCE_POINTER):
        _check(
            checks,
            "server-copy",
            "warn",
            "cannot tell which package this server copy came from",
            f"{SOURCE_POINTER} is missing; the copy never refreshes itself",
            "reinstall the bundle, or reload it from Settings -> Plugins",
        )
        return
    try:
        with open(SOURCE_POINTER, encoding="utf-8") as handle:
            source = handle.read().strip()
    except OSError as error:  # noqa: BLE001
        _check(checks, "server-copy", "warn", "cannot read the package pointer", str(error))
        return
    fresh = os.path.join(source, "linux_desktop_mcp.py")
    if not os.path.exists(fresh):
        _check(
            checks,
            "server-copy",
            "warn",
            "the package this copy came from is gone",
            source,
            "reinstall the bundle; the copy now updates itself from the package on start",
        )
        return
    ours = os.path.abspath(__file__)
    try:
        same = open(fresh, "rb").read() == open(ours, "rb").read()
    except OSError as error:  # noqa: BLE001
        _check(checks, "server-copy", "warn", "cannot compare server copies", str(error))
        return
    if same:
        _check(checks, "server-copy", "ok", "this copy matches the installed package", source)
    else:
        _check(
            checks,
            "server-copy",
            "warn",
            "this copy differs from the installed package",
            f"package: {fresh}",
            "restart the MCP entry (or reload the bundle): the server refreshes itself on start",
        )


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

    unlock_state = cu_unlock.status()
    if unlock_state["unlock_policy"] == "off":
        _check(
            checks,
            "unlock-policy",
            "ok",
            "AI unlocking is switched off (the default)",
            "while the session is locked, input tools refuse to type; screenshots still work",
            "switch it on with: settings.json {\"unlock\": \"keyring\"} after storing a password",
        )
    elif unlock_state["password_stored"]:
        _check(
            checks,
            "unlock-policy",
            "ok",
            "AI unlocking is on; a password is stored in the keyring",
            "the lock screen now stops a person at the keyboard, not software running as you",
            "turn it off with: --unlock-policy off",
        )
    else:
        _check(
            checks,
            "unlock-policy",
            "warn",
            "AI unlocking is on but no password is stored",
            "unlock_screen will fail until a password is stored",
            "run: linux_desktop_mcp.py --set-unlock-password",
        )

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

    _keybinding_checks(checks)
    _sync_check(checks)

    ok = not any(check["status"] == "fail" for check in checks)
    return {
        "ok": ok,
        "server": {"name": SERVER_NAME, "version": SERVER_VERSION, "log": LOG_PATH},
        "checks": checks,
    }


def _cli_unlock(args: list[str]) -> int | None:
    """Handle the unlock-related command line switches. Returns an exit code or None."""
    if "--set-unlock-password" in args:
        try:
            print(cu_unlock.prompt_and_store())
        except cu_unlock.UnlockError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        return 0
    if "--clear-unlock-password" in args:
        removed = cu_unlock.clear_password()
        print("cleared the stored unlock password" if removed else "no unlock password was stored")
        return 0
    if "--unlock-status" in args:
        print(json.dumps(cu_unlock.status(), indent=2, ensure_ascii=False))
        return 0
    if "--unlock-policy" in args:
        position = args.index("--unlock-policy")
        value = args[position + 1] if position + 1 < len(args) else ""
        if not value or value.startswith("-"):
            print("usage: --unlock-policy off|keyring", file=sys.stderr)
            return 2
        try:
            chosen = cu_unlock.set_policy(value)
        except cu_unlock.UnlockError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        print(f"unlock policy: {chosen}")
        return 0
    return None


INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCE_POINTER = os.path.join(INSTALL_DIR, ".source")
SELF_UPDATE_FLAG = "DSH_LINUX_CU_SELF_UPDATED"


def _self_update() -> None:
    """Refresh this copy of the server from the installed package, then re-exec.

    The bundle syncs server/*.py into a stable directory when the *doctor*
    plugin loads, but the MCP server is started by a different patch entry, so
    toggling only that entry (or upgrading the npm package) leaves a stale copy
    behind. A pointer file records where the package lives; comparing against
    it here makes the server heal itself before it serves a single request.
    """
    if os.environ.get(SELF_UPDATE_FLAG) == "1":
        return
    try:
        with open(SOURCE_POINTER, encoding="utf-8") as handle:
            source = handle.read().strip()
    except OSError:
        return
    if not source or not os.path.isdir(source):
        return
    if os.path.realpath(source) == os.path.realpath(INSTALL_DIR):
        return
    if not os.path.exists(os.path.join(source, "linux_desktop_mcp.py")):
        return
    changed: list[str] = []
    for name in sorted(os.listdir(source)):
        if not name.endswith(".py"):
            continue
        fresh = os.path.join(source, name)
        stale = os.path.join(INSTALL_DIR, name)
        try:
            with open(fresh, "rb") as mine, open(stale, "rb") as theirs:
                if mine.read() == theirs.read():
                    continue
        except OSError:
            pass
        try:
            shutil.copyfile(fresh, stale)
            changed.append(name)
        except OSError as error:  # noqa: BLE001
            log(f"self-refresh could not copy {name}: {error}")
    if not changed:
        return
    log(f"refreshed {len(changed)} server file(s) from {source}: {', '.join(changed)}")
    env = dict(os.environ)
    env[SELF_UPDATE_FLAG] = "1"
    try:
        os.execve(sys.executable, [sys.executable, os.path.abspath(__file__), *sys.argv[1:]], env)
    except OSError as error:  # noqa: BLE001
        log(f"self-refresh re-exec failed: {error}")


def main() -> int:
    # Serve (and diagnose) with the freshest copy of the code we can find.
    _self_update()
    unlock_exit = _cli_unlock(sys.argv[1:])
    if unlock_exit is not None:
        return unlock_exit
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
