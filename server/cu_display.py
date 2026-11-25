"""Display geometry and screen capture for the Linux desktop MCP server.

Screenshots on a Wayland GNOME session cannot come from ``org.gnome.Shell``
(its Screenshot interface answers AccessDenied to every external client), so
frames are pulled from the ``org.freedesktop.portal.ScreenCast`` portal:

* the first call opens the portal's "share your screen" dialog exactly once and
  stores the returned ``restore_token`` under ``~/.cache/dsh-linux-cu``;
* later calls reuse the token, so periodic sampling never asks again;
* frames are read from the portal's private PipeWire remote through
  ``pipewiresrc`` and encoded with ``pngenc``.

``org.freedesktop.portal.Screenshot`` is kept as a one-shot fallback, but that
path shows a confirmation dialog on every single screenshot.
"""

from __future__ import annotations

import json
import os
import random
import struct
import time
from dataclasses import dataclass, field

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

PORTAL_DEST = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
SCREENSHOT_IFACE = "org.freedesktop.portal.Screenshot"
DISPLAYCONFIG_DEST = "org.gnome.Mutter.DisplayConfig"
DISPLAYCONFIG_PATH = "/org/gnome/Mutter/DisplayConfig"
DISPLAYCONFIG_IFACE = "org.gnome.Mutter.DisplayConfig"

CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "dsh-linux-cu")
TOKEN_FILE = os.path.join(CACHE_DIR, "screencast.json")
SHOT_DIR = os.path.join(CACHE_DIR, "shots")

SOURCE_MONITOR = 1
SOURCE_WINDOW = 2
CURSOR_HIDDEN, CURSOR_EMBEDDED, CURSOR_METADATA = 0, 1, 2


class PortalError(RuntimeError):
    """Portal call failed, was cancelled, or timed out."""


def _ensure_dirs() -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(SHOT_DIR, exist_ok=True)


# --------------------------------------------------------------------------- #
# display geometry
# --------------------------------------------------------------------------- #
@dataclass
class Monitor:
    connector: str
    vendor: str
    product: str
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    scale: float = 1.0
    primary: bool = False
    refresh: float = 0.0


@dataclass
class Layout:
    """Logical desktop extent plus per-monitor geometry."""

    x: int
    y: int
    width: int
    height: int
    monitors: list[Monitor] = field(default_factory=list)
    serial: int = 0

    def to_absolute(self, x: float, y: float) -> tuple[int, int]:
        """Map logical desktop coordinates to the pointer's 0..32767 range."""
        x = max(self.x, min(self.x + self.width - 1, float(x)))
        y = max(self.y, min(self.y + self.height - 1, float(y)))
        span_x = max(1, self.width - 1)
        span_y = max(1, self.height - 1)
        return (round((x - self.x) * 32767 / span_x), round((y - self.y) * 32767 / span_y))

    def describe(self) -> dict:
        return {
            "logical": {"x": self.x, "y": self.y, "width": self.width, "height": self.height},
            "monitors": [
                {
                    "connector": m.connector,
                    "x": m.x,
                    "y": m.y,
                    "width": m.width,
                    "height": m.height,
                    "scale": m.scale,
                    "primary": m.primary,
                    "refresh": round(m.refresh, 3),
                }
                for m in self.monitors
            ],
        }


def _session_bus() -> Gio.DBusConnection:
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def read_layout(timeout_ms: int = 4000) -> Layout:
    """Read the live layout from Mutter's DisplayConfig D-Bus interface."""
    connection = _session_bus()
    try:
        reply = connection.call_sync(
            DISPLAYCONFIG_DEST,
            DISPLAYCONFIG_PATH,
            DISPLAYCONFIG_IFACE,
            "GetCurrentState",
            None,
            None,
            Gio.DBusCallFlags.NONE,
            timeout_ms,
            None,
        )
        serial, monitors, logical, _props = reply.unpack()
    except GLib.Error as error:
        raise PortalError(
            f"cannot read the display layout from Mutter DisplayConfig: {error.message}"
        ) from error

    # connector -> (current width, height, refresh)
    current: dict[str, tuple[int, int, float]] = {}
    for entry in monitors:
        spec, modes, _enabled = entry
        connector = spec[0]
        for mode in modes:
            mode_id, width, height, refresh = mode[0], mode[1], mode[2], mode[3]
            mode_props = mode[6] if len(mode) > 6 else {}
            if mode_props.get("is-current"):
                current[connector] = (width, height, refresh)
                break
            del mode_id

    result: list[Monitor] = []
    for entry in logical:
        x, y, scale, _transform, primary, attached, _props = entry
        attached = attached or []
        for spec in attached:
            connector, vendor, product = spec[0], spec[1], spec[2]
            width, height, refresh = current.get(connector, (0, 0, 0.0))
            logical_width = round(width / scale) if width and scale else 0
            logical_height = round(height / scale) if height and scale else 0
            result.append(
                Monitor(
                    connector=connector,
                    vendor=vendor,
                    product=product,
                    x=int(x),
                    y=int(y),
                    width=logical_width,
                    height=logical_height,
                    scale=float(scale),
                    primary=bool(primary),
                    refresh=float(refresh or 0.0),
                )
            )

    if not result:
        # Extremely defensive: assume a single 1080p monitor at the origin.
        result = [Monitor("unknown", "", "", 0, 0, 1920, 1080, 1.0, True)]

    min_x = min(m.x for m in result)
    min_y = min(m.y for m in result)
    max_x = max(m.x + m.width for m in result)
    max_y = max(m.y + m.height for m in result)
    return Layout(x=min_x, y=min_y, width=max_x - min_x, height=max_y - min_y,
                  monitors=result, serial=int(serial))


def png_size(path: str) -> tuple[int, int]:
    """Read width and height from a PNG header without decoding the image."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(24)
    except OSError:
        return (0, 0)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        return (0, 0)
    width, height = struct.unpack(">II", header[16:24])
    return (int(width), int(height))


# --------------------------------------------------------------------------- #
# portal plumbing
# --------------------------------------------------------------------------- #
def _token() -> str:
    return f"dsh_cu_{random.randrange(1 << 32):08x}"


def _request_path(connection: Gio.DBusConnection, handle_token: str) -> str:
    sender = connection.get_unique_name().lstrip(":").replace(".", "_")
    return f"{PORTAL_PATH}/request/{sender}/{handle_token}"


def _call_request(connection: Gio.DBusConnection, interface: str, method: str,
                  parameters: GLib.Variant, timeout_s: float = 90.0) -> dict:
    """Call a portal method that answers with a Request Response signal."""
    handle_token = _token()
    path = _request_path(connection, handle_token)
    # Bind the token into the options dict the caller prepared.
    if parameters is None:
        parameters = GLib.Variant("(a{sv})", ({"handle_token": GLib.Variant("s", handle_token)},))
    else:
        signature, values = parameters.get_type_string(), list(parameters.unpack())
        options_index = None
        for index, part in enumerate(signature[1:-1]):
            if part == "a":
                # first array-of-dict argument is the options dict of interest
                candidate = options_index = index
                del candidate
                break
        del options_index
        values = _with_token(signature, values, handle_token)
        parameters = GLib.Variant(signature, tuple(values))

    loop = GLib.MainLoop()
    outcome: dict = {}

    def on_response(_conn, _sender, _path, _iface, _signal, payload):
        response, results = payload.unpack()
        outcome["response"] = int(response)
        outcome["results"] = results
        loop.quit()

    subscription = connection.signal_subscribe(
        PORTAL_DEST, REQUEST_IFACE, "Response", path, None,
        Gio.DBusSignalFlags.NONE, on_response,
    )
    try:
        connection.call_sync(
            PORTAL_DEST, PORTAL_PATH, interface, method, parameters, None,
            Gio.DBusCallFlags.NONE, int(timeout_s * 1000), None,
        )
        timeout = GLib.timeout_add(int(timeout_s * 1000), lambda: (loop.quit(), False)[1])
        try:
            loop.run()
        finally:
            GLib.source_remove(timeout)
    except GLib.Error as error:
        raise PortalError(f"{interface}.{method} failed: {error.message}") from error
    finally:
        connection.signal_unsubscribe(subscription)

    if "response" not in outcome:
        raise PortalError(f"{interface}.{method} produced no response within {timeout_s:.0f}s")
    if outcome["response"] != 0:
        reason = "cancelled by the user" if outcome["response"] == 1 else "closed by the user"
        raise PortalError(f"{interface}.{method} was {reason}")
    return outcome["results"]


def _with_token(signature: str, values: list, handle_token: str) -> list:
    """Insert handle_token into the trailing a{sv} options argument."""
    del signature
    values = list(values)
    for index in range(len(values) - 1, -1, -1):
        if isinstance(values[index], dict):
            options = dict(values[index])
            options["handle_token"] = handle_token
            values[index] = options
            break
    return values


# --------------------------------------------------------------------------- #
# screencast session
# --------------------------------------------------------------------------- #
class ScreencastSession:
    """A persistent ScreenCast portal session feeding frames over PipeWire."""

    _instance: "ScreencastSession | None" = None

    def __init__(self) -> None:
        self.connection: Gio.DBusConnection | None = None
        self.session_handle: str | None = None
        self.node_id: int | None = None
        self.fd: int | None = None
        self.stream_size: tuple[int, int] = (0, 0)
        self.restore_token: str | None = None
        self._gst_ready = False

    # -- token persistence ------------------------------------------------- #
    @staticmethod
    def load_token() -> str | None:
        try:
            with open(TOKEN_FILE, "r", encoding="utf-8") as handle:
                return json.load(handle).get("restore_token")
        except (OSError, ValueError):
            return None

    @staticmethod
    def save_token(token: str | None) -> None:
        _ensure_dirs()
        try:
            with open(TOKEN_FILE, "w", encoding="utf-8") as handle:
                json.dump({"restore_token": token, "saved_at": time.time()}, handle)
            os.chmod(TOKEN_FILE, 0o600)
        except OSError:
            pass

    # -- lifecycle --------------------------------------------------------- #
    @classmethod
    def shared(cls) -> "ScreencastSession":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def ensure(self, timeout_s: float = 120.0) -> "ScreencastSession":
        """Open the portal session if needed; may show the share dialog once."""
        if self.fd is not None and self.node_id is not None:
            return self
        _ensure_dirs()
        connection = self.connection or _session_bus()
        self.connection = connection
        self.restore_token = self.restore_token or self.load_token()

        create = GLib.Variant(
            "(a{sv})",
            ({"handle_token": GLib.Variant("s", _token()),
              "session_handle_token": GLib.Variant("s", _token())},),
        )
        self.session_handle = _call_request(connection, SCREENCAST_IFACE, "CreateSession", create, 30)
        if isinstance(self.session_handle, tuple):
            self.session_handle = self.session_handle[0]
        if not isinstance(self.session_handle, str):
            raise PortalError(f"unexpected CreateSession reply: {self.session_handle!r}")

        options: dict[str, GLib.Variant] = {
            "types": GLib.Variant("u", SOURCE_MONITOR),
            "multiple": GLib.Variant("b", False),
            "cursor_mode": GLib.Variant("u", CURSOR_EMBEDDED),
            "persist_mode": GLib.Variant("u", 2),
        }
        if self.restore_token:
            options["restore_token"] = GLib.Variant("s", self.restore_token)
        select = GLib.Variant(
            "(oa{sv})",
            (self.session_handle,
             {"handle_token": GLib.Variant("s", _token()), **options}),
        )
        _call_request(connection, SCREENCAST_IFACE, "SelectSources", select, timeout_s)

        start = GLib.Variant(
            "(osa{sv})",
            (self.session_handle, "",
             {"handle_token": GLib.Variant("s", _token())}),
        )
        results = _call_request(connection, SCREENCAST_IFACE, "Start", start, timeout_s)
        streams = results.get("streams") or []
        if not streams:
            raise PortalError("the ScreenCast portal returned no streams")
        node_id, props = streams[0]
        self.node_id = int(node_id)
        size = props.get("size") if isinstance(props, dict) else None
        if size and len(size) == 2:
            self.stream_size = (int(size[0]), int(size[1]))
        token = results.get("restore_token")
        if token:
            self.restore_token = token
            self.save_token(token)

        reply, fd_list = connection.call_with_unix_fd_list_sync(
            PORTAL_DEST, PORTAL_PATH, SCREENCAST_IFACE, "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (self.session_handle, {})),
            GLib.VariantType.new("(h)"), Gio.DBusCallFlags.NONE, 20000, None, None,
        )
        del reply
        self.fd = fd_list.get(0)
        return self

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None
        self.node_id = None
        self.session_handle = None


# --------------------------------------------------------------------------- #
# frame capture
# --------------------------------------------------------------------------- #
def _init_gst():
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst  # local import keeps startup light

    if not Gst.is_initialized():
        Gst.init(None)
    return Gst


def capture_frame(session: ScreencastSession, path: str, timeout_s: float = 10.0) -> tuple[int, int]:
    """Grab one frame from the screencast session and write it as PNG."""
    Gst = _init_gst()
    _ensure_dirs()
    os.makedirs(os.path.dirname(path) or SHOT_DIR, exist_ok=True)

    pipeline = Gst.Pipeline.new("dsh-capture")
    source = Gst.ElementFactory.make("pipewiresrc", "src")
    convert = Gst.ElementFactory.make("videoconvert", "convert")
    encode = Gst.ElementFactory.make("pngenc", "encode")
    sink = Gst.ElementFactory.make("appsink", "sink")
    for element in (source, convert, encode, sink):
        if element is None:
            raise PortalError("GStreamer is missing an element required for capture "
                              "(pipewiresrc/videoconvert/pngenc/appsink)")
    source.set_property("path", str(session.node_id))
    source.set_property("fd", int(session.fd))
    source.set_property("do-timestamp", True)
    encode.set_property("snapshot", True)
    sink.set_property("max-buffers", 1)
    sink.set_property("drop", True)
    sink.set_property("sync", False)

    pipeline.add(source)
    pipeline.add(convert)
    pipeline.add(encode)
    pipeline.add(sink)
    if not (source.link(convert) and convert.link(encode) and encode.link(sink)):
        raise PortalError("cannot link the capture pipeline")

    loop = GLib.MainLoop()
    state: dict = {"ok": False, "error": None}

    def on_sample(_sink):
        sample = _sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR
        buffer = sample.get_buffer()
        success, mapinfo = buffer.map(Gst.MapFlags.READ)
        if not success:
            state["error"] = "cannot map the captured buffer"
            loop.quit()
            return Gst.FlowReturn.ERROR
        try:
            with open(path, "wb") as handle:
                handle.write(bytes(mapinfo.data))
            state["ok"] = True
        finally:
            buffer.unmap(mapinfo)
            loop.quit()
        return Gst.FlowReturn.OK

    sink.connect("new-sample", on_sample)
    pipeline.set_state(Gst.State.PLAYING)
    timeout = GLib.timeout_add(int(timeout_s * 1000), lambda: (loop.quit(), False)[1])
    try:
        loop.run()
    finally:
        GLib.source_remove(timeout)
        pipeline.set_state(Gst.State.NULL)

    if not state["ok"]:
        raise PortalError(state["error"] or f"no frame arrived within {timeout_s:.0f}s")
    width, height = png_size(path)
    if width and height:
        session.stream_size = (width, height)
    return (width, height)


def portal_screenshot(path: str, timeout_s: float = 120.0) -> tuple[int, int]:
    """One-shot fallback: portal Screenshot (shows a dialog every call)."""
    connection = _session_bus()
    options = {
        "handle_token": GLib.Variant("s", _token()),
        "interactive": GLib.Variant("b", False),
        "modal": GLib.Variant("b", True),
    }
    results = _call_request(
        connection, SCREENSHOT_IFACE, "Screenshot",
        GLib.Variant("(sa{sv})", ("", options)), timeout_s,
    )
    uri = results.get("uri")
    if not uri:
        raise PortalError("the Screenshot portal returned no uri")
    source_path = uri[len("file://"):] if uri.startswith("file://") else uri
    source_path = GLib.filename_from_uri(uri)[0] if uri.startswith("file://") else source_path
    _ensure_dirs()
    os.makedirs(os.path.dirname(path) or SHOT_DIR, exist_ok=True)
    with open(source_path, "rb") as src, open(path, "wb") as dst:
        dst.write(src.read())
    return png_size(path)


# --------------------------------------------------------------------------- #
# image scaling
# --------------------------------------------------------------------------- #
def downscale(path: str, max_width: int) -> tuple[int, int]:
    """Shrink a PNG in place when wider than ``max_width`` (vision economy)."""
    if max_width <= 0:
        return png_size(path)
    width, height = png_size(path)
    if not width or width <= max_width:
        return (width, height)
    try:
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf

        pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
        target_height = max(1, round(height * max_width / width))
        scaled = pixbuf.scale_simple(max_width, target_height, GdkPixbuf.InterpType.BILINEAR)
        if scaled is None:
            return (width, height)
        scaled.savev(path, "png", [], [])
        return png_size(path)
    except Exception:  # noqa: BLE001 - scaling is best effort
        return (width, height)
