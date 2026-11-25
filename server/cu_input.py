"""Pointer and keyboard injection for the Linux desktop MCP server.

Backends
--------
uinput (default)
    Writes struct input_event records straight into /dev/uinput. The kernel turns
    them into a real virtual device, so they are indistinguishable from hardware
    for Wayland compositors and X11 alike. Requires write access to /dev/uinput
    (udev rule: GROUP="input" MODE="0660" plus the user in group "input").

ydotool (fallback)
    Shells out to the ydotool client, which talks to ydotoold over a unix socket.
    Used only when /dev/uinput cannot be opened.

The virtual pointer is an *absolute* pointer (ABS_X/ABS_Y, range 0..32767), the
same shape as a QEMU usb-tablet: libinput classifies it as an ordinary pointer,
so clicks, drags and double-clicks behave normally while positioning stays exact
(no pointer acceleration to fight). Wheel scrolling uses a second, relative
device because high-resolution wheel events are EV_REL.
"""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import struct
import subprocess
import time

# --------------------------------------------------------------------------- #
# kernel constants
# --------------------------------------------------------------------------- #
EV_SYN, EV_KEY, EV_REL, EV_ABS = 0x00, 0x01, 0x02, 0x03
SYN_REPORT = 0x00
REL_WHEEL, REL_HWHEEL = 0x08, 0x06
ABS_X, ABS_Y = 0x00, 0x01
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112
BUS_VIRTUAL = 0x06
INPUT_PROP_POINTER = 0x00

UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_DEV_SETUP = 0x405C5503
UI_ABS_SETUP = 0x401C5504
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_SET_ABSBIT = 0x40045567
UI_SET_PROPBIT = 0x4004556E

ABS_MAX_VALUE = 32767
KEY_MAX = 0x2FF
REL_MAX = 0x0F
ABS_MAX = 0x3F

INPUT_EVENT = struct.Struct("llHHi")  # struct input_event on 64-bit Linux

class InputError(RuntimeError):
    """Input injection is unavailable or was rejected."""


DEFAULT_UINPUT = "/dev/uinput"
DEFAULT_YDOTOOL_SOCKET = "/run/ydotoold.socket"

# --------------------------------------------------------------------------- #
# key codes
# --------------------------------------------------------------------------- #

# Only the names the tool surfaces to the model; the full table is loaded from
# the kernel headers when they are installed.
_BUILTIN_KEYS = {
    "ESC": 1, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9,
    "9": 10, "0": 11, "MINUS": 12, "EQUAL": 13, "BACKSPACE": 14, "TAB": 15,
    "Q": 16, "W": 17, "E": 18, "R": 19, "T": 20, "Y": 21, "U": 22, "I": 23,
    "O": 24, "P": 25, "LEFTBRACE": 26, "RIGHTBRACE": 27, "ENTER": 28,
    "LEFTCTRL": 29, "A": 30, "S": 31, "D": 32, "F": 33, "G": 34, "H": 35,
    "J": 36, "K": 37, "L": 38, "SEMICOLON": 39, "APOSTROPHE": 40, "GRAVE": 41,
    "LEFTSHIFT": 42, "BACKSLASH": 43, "Z": 44, "X": 45, "C": 46, "V": 47,
    "B": 48, "N": 49, "M": 50, "COMMA": 51, "DOT": 52, "SLASH": 53,
    "RIGHTSHIFT": 54, "LEFTALT": 56, "SPACE": 57, "CAPSLOCK": 58,
    "F1": 59, "F2": 60, "F3": 61, "F4": 62, "F5": 63, "F6": 64, "F7": 65,
    "F8": 66, "F9": 67, "F10": 68, "F11": 87, "F12": 88,
    "RIGHTCTRL": 97, "RIGHTALT": 100, "HOME": 102, "UP": 103, "PAGEUP": 104,
    "LEFT": 105, "RIGHT": 106, "END": 107, "DOWN": 108, "PAGEDOWN": 109,
    "INSERT": 110, "DELETE": 111, "LEFTMETA": 125, "RIGHTMETA": 126,
    "SYSRQ": 99, "PAUSE": 119, "MENU": 139,
}

_ALIASES = {
    "ctrl": "LEFTCTRL", "control": "LEFTCTRL", "shift": "LEFTSHIFT",
    "alt": "LEFTALT", "option": "LEFTALT", "super": "LEFTMETA",
    "meta": "LEFTMETA", "cmd": "LEFTMETA", "win": "LEFTMETA",
    "enter": "ENTER", "return": "ENTER", "esc": "ESC", "escape": "ESC",
    "space": "SPACE", "spacebar": "SPACE", "tab": "TAB",
    "backspace": "BACKSPACE", "delete": "DELETE", "del": "DELETE",
    "insert": "INSERT", "ins": "INSERT", "home": "HOME", "end": "END",
    "up": "UP", "down": "DOWN", "left": "LEFT", "right": "RIGHT",
    "pageup": "PAGEUP", "pgup": "PAGEUP", "pagedown": "PAGEDOWN",
    "pgdn": "PAGEDOWN", "print": "SYSRQ", "printscreen": "SYSRQ",
    "capslock": "CAPSLOCK", "minus": "MINUS", "equal": "EQUAL",
    "comma": "COMMA", "period": "DOT", "dot": "DOT", "slash": "SLASH",
    "semicolon": "SEMICOLON", "apostrophe": "APOSTROPHE", "grave": "GRAVE",
    "backslash": "BACKSLASH", "leftbrace": "LEFTBRACE",
    "rightbrace": "RIGHTBRACE",
}

_CHAR_SHIFT = {
    "1": "!", "2": "@", "3": "#", "4": "$", "5": "%", "6": "^", "7": "&",
    "8": "*", "9": "(", "0": ")", "-": "_", "=": "+", "[": "{", "]": "}",
    ";": ":", "'": '"', ",": "<", ".": ">", "/": "?", "\\": "|", "`": "~",
}
_CHAR_PLAIN = {v: k for k, v in _CHAR_SHIFT.items()}


def _load_key_codes() -> dict[str, int]:
    """Merge the builtin table with /usr/include/linux/input-event-codes.h."""
    codes = dict(_BUILTIN_KEYS)
    for header in (
        "/usr/include/linux/input-event-codes.h",
        "/usr/src/linux-headers/include/uapi/linux/input-event-codes.h",
    ):
        try:
            with open(header, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        for name, value in re.findall(r"#define\s+KEY_(\w+)\s+(0x[0-9a-fA-F]+|\d+)", text):
            codes.setdefault(name, int(value, 0))
        break
    return codes


KEYS = _load_key_codes()


def key_code(name: str) -> int:
    """Resolve one key name ("a", "Return", "F5", "LEFTCTRL") to an EV_KEY code."""
    raw = name.strip()
    if not raw:
        raise ValueError("empty key name")
    if re.fullmatch(r"0x[0-9a-fA-F]+|\d+", raw):
        return int(raw, 0)
    lowered = raw.lower()
    if lowered in _ALIASES:
        lowered = _ALIASES[lowered].lower()
    if len(raw) == 1 and raw.isalpha():
        return KEYS[raw.upper()]
    if lowered in _ALIASES:
        return KEYS[_ALIASES[lowered]]
    upper = lowered.upper()
    if upper in KEYS:
        return KEYS[upper]
    if upper.startswith("KEY_") and upper[4:] in KEYS:
        return KEYS[upper[4:]]
    raise ValueError(f"unknown key name {name!r}")


# --------------------------------------------------------------------------- #
# /dev/uinput backend
# --------------------------------------------------------------------------- #
class UinputDevice:
    """One virtual input device created from /dev/uinput and kept open."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.fd = os.open(DEFAULT_UINPUT, os.O_WRONLY | os.O_NONBLOCK)
        self._closed = False

    # -- capability bits -------------------------------------------------- #
    def enable_event(self, *types: int) -> "UinputDevice":
        for value in types:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, value)
        return self

    def enable_keys(self, codes) -> "UinputDevice":
        for code in codes:
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)
        return self

    def enable_rel(self, codes) -> "UinputDevice":
        for code in codes:
            fcntl.ioctl(self.fd, UI_SET_RELBIT, code)
        return self

    def enable_abs(self, code: int, minimum: int, maximum: int) -> "UinputDevice":
        fcntl.ioctl(self.fd, UI_SET_ABSBIT, code)
        # struct uinput_abs_setup { __u16 code; __u16 pad; struct input_absinfo; }
        payload = struct.pack("HHiiiii", code, 0, 0, minimum, maximum, 0, 0)
        fcntl.ioctl(self.fd, UI_ABS_SETUP, payload)
        return self

    def enable_prop(self, *props: int) -> "UinputDevice":
        for prop in props:
            fcntl.ioctl(self.fd, UI_SET_PROPBIT, prop)
        return self

    # -- creation --------------------------------------------------------- #
    def create(self, vendor: int = 0x4453, product: int = 0x4C43) -> "UinputDevice":
        # struct uinput_setup { struct input_id id; char name[80]; __u32 ff; }
        setup = struct.pack(
            "HHHH80sI",
            BUS_VIRTUAL,
            vendor & 0xFFFF,
            product & 0xFFFF,
            1,
            self.name.encode("utf-8")[:79],
            0,
        )
        fcntl.ioctl(self.fd, UI_DEV_SETUP, setup)
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        time.sleep(0.12)  # let udev/libinput finish device setup
        return self

    # -- events ----------------------------------------------------------- #
    def emit(self, etype: int, code: int, value: int) -> None:
        os.write(self.fd, INPUT_EVENT.pack(0, 0, etype, code, value))

    def sync(self) -> None:
        self.emit(EV_SYN, SYN_REPORT, 0)

    def tap(self, code: int) -> None:
        self.emit(EV_KEY, code, 1)
        self.sync()
        self.emit(EV_KEY, code, 0)
        self.sync()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass


class UinputBackend:
    """Lazily created absolute pointer, wheel and keyboard devices."""

    name = "uinput"

    def __init__(self, uinput_path: str = DEFAULT_UINPUT) -> None:
        self.uinput_path = uinput_path
        self._pointer: UinputDevice | None = None
        self._wheel: UinputDevice | None = None
        self._keyboard: UinputDevice | None = None

    # -- capability check -------------------------------------------------- #
    @classmethod
    def available(cls, uinput_path: str = DEFAULT_UINPUT) -> bool:
        return os.access(uinput_path, os.R_OK | os.W_OK)

    def reset(self) -> None:
        """Drop every uinput device so the next call recreates them.

        Useful after a compositor restart: mutter can stop honouring a
        virtual pointer that was created before the session restarted.
        """
        for attribute in ("_pointer", "_wheel", "_keyboard"):
            device = getattr(self, attribute, None)
            if device is not None:
                try:
                    device.close()
                except Exception:  # noqa: BLE001
                    pass
                setattr(self, attribute, None)

    # -- devices ----------------------------------------------------------- #
    def pointer(self) -> UinputDevice:
        if self._pointer is None:
            device = UinputDevice("DSH Computer Use Pointer")
            device.enable_event(EV_KEY, EV_ABS, EV_SYN)
            device.enable_keys((BTN_LEFT, BTN_RIGHT, BTN_MIDDLE))
            device.enable_abs(ABS_X, 0, ABS_MAX_VALUE)
            device.enable_abs(ABS_Y, 0, ABS_MAX_VALUE)
            device.enable_prop(INPUT_PROP_POINTER)
            self._pointer = device.create(product=0x0001)
        return self._pointer

    def wheel(self) -> UinputDevice:
        if self._wheel is None:
            device = UinputDevice("DSH Computer Use Wheel")
            device.enable_event(EV_REL, EV_KEY, EV_SYN)
            device.enable_rel((REL_WHEEL, REL_HWHEEL))
            device.enable_keys((BTN_LEFT,))
            self._wheel = device.create(product=0x0002)
        return self._wheel

    def keyboard(self) -> UinputDevice:
        if self._keyboard is None:
            device = UinputDevice("DSH Computer Use Keyboard")
            device.enable_event(EV_KEY, EV_SYN)
            device.enable_keys(range(1, KEY_MAX + 1))
            self._keyboard = device.create(product=0x0003)
        return self._keyboard

    # -- primitives -------------------------------------------------------- #
    def move(self, x: int, y: int, layout) -> None:
        """Move the absolute pointer to logical desktop coordinate (x, y)."""
        abs_x, abs_y = layout.to_absolute(x, y)
        device = self.pointer()
        device.emit(EV_ABS, ABS_X, abs_x)
        device.emit(EV_ABS, ABS_Y, abs_y)
        device.sync()

    def button(self, name: str, pressed: bool) -> None:
        code = {"left": BTN_LEFT, "right": BTN_RIGHT, "middle": BTN_MIDDLE}[name]
        device = self.pointer()
        device.emit(EV_KEY, code, 1 if pressed else 0)
        device.sync()

    def scroll(self, dx: int, dy: int) -> None:
        device = self.wheel()
        if dy:
            device.emit(EV_REL, REL_WHEEL, -int(dy))
            device.sync()
        if dx:
            device.emit(EV_REL, REL_HWHEEL, -int(dx))
            device.sync()

    def press(self, codes) -> None:
        device = self.keyboard()
        for code in codes:
            device.emit(EV_KEY, code, 1)
            device.sync()

    def release(self, codes) -> None:
        device = self.keyboard()
        for code in reversed(list(codes)):
            device.emit(EV_KEY, code, 0)
            device.sync()

    def tap_key(self, code: int) -> None:
        self.keyboard().tap(code)

    def type_char(self, char: str) -> None:
        if char == "\n":
            self.tap_key(KEYS["ENTER"])
            return
        if char == "\t":
            self.tap_key(KEYS["TAB"])
            return
        shift = char in _CHAR_PLAIN
        base = _CHAR_PLAIN.get(char, char)
        if not base.isprintable():
            raise ValueError(f"cannot type character {char!r}")
        code = KEYS[base.upper()] if base.isalpha() else KEYS[_CHAR_KEY_NAME[base]]
        device = self.keyboard()
        if shift:
            device.emit(EV_KEY, KEYS["LEFTSHIFT"], 1)
            device.sync()
        device.emit(EV_KEY, code, 1)
        device.sync()
        device.emit(EV_KEY, code, 0)
        device.sync()
        if shift:
            device.emit(EV_KEY, KEYS["LEFTSHIFT"], 0)
            device.sync()

    def close(self) -> None:
        for device in (self._pointer, self._wheel, self._keyboard):
            if device is not None:
                device.close()
        self._pointer = self._wheel = self._keyboard = None


_CHAR_KEY_NAME = {
    " ": "SPACE", "-": "MINUS", "=": "EQUAL", "[": "LEFTBRACE",
    "]": "RIGHTBRACE", ";": "SEMICOLON", "'": "APOSTROPHE", "`": "GRAVE",
    "\\": "BACKSLASH", ",": "COMMA", ".": "DOT", "/": "SLASH",
    "1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6", "7": "7",
    "8": "8", "9": "9", "0": "0",
}


# --------------------------------------------------------------------------- #
# ydotool backend
# --------------------------------------------------------------------------- #
class YdotoolBackend:
    """Fallback backend that delegates to the ydotool client."""

    name = "ydotool"

    def __init__(self, socket_path: str = DEFAULT_YDOTOOL_SOCKET, binary: str | None = None) -> None:
        self.socket_path = socket_path
        self.binary = binary or shutil.which("ydotool") or "ydotool"

    @classmethod
    def available(cls, socket_path: str = DEFAULT_YDOTOOL_SOCKET) -> bool:
        return bool(shutil.which("ydotool")) and os.path.exists(socket_path)

    def _run(self, *args: str) -> None:
        command = [self.binary]
        if self.socket_path:
            command += ["-s", self.socket_path]
        command += list(args)
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            raise RuntimeError(
                f"ydotool {' '.join(args)} failed ({result.returncode}): "
                f"{(result.stderr or result.stdout).strip()}"
            )

    def move(self, x: int, y: int, layout) -> None:
        # ydotool 0.1.x drives a relative device, so track our own cursor and
        # send the delta. Approximate by nature; the uinput backend is exact.
        x, y = int(x), int(y)
        last_x, last_y = getattr(self, "_pos", (0, 0))
        self._run("mousemove_relative", str(x - last_x), str(y - last_y))
        self._pos = (x, y)

    def button(self, name: str, pressed: bool) -> None:
        # BTN_LEFT/MIDDLE/RIGHT are EV_KEY codes, so `ydotool key` can press them.
        code = {"left": BTN_LEFT, "right": BTN_RIGHT, "middle": BTN_MIDDLE}[name]
        self._run("key", f"{code}:{1 if pressed else 0}")

    def scroll(self, dx: int, dy: int) -> None:
        raise RuntimeError(
            "the ydotool backend cannot synthesize wheel events (ydotool 0.1.x has "
            "no REL_WHEEL support); install/enable the uinput backend for scrolling"
        )

    def press(self, codes) -> None:
        self._run("key", *[f"{code}:1" for code in codes])

    def release(self, codes) -> None:
        self._run("key", *[f"{code}:0" for code in reversed(list(codes))])

    def tap_key(self, code: int) -> None:
        self._run("key", f"{code}:1", f"{code}:0")

    def type_char(self, char: str) -> None:
        try:
            self._run("type", "--key-delay", "12", char)
        except RuntimeError:
            self._run("type", char)

    def close(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# facade
# --------------------------------------------------------------------------- #
class InputController:
    """Layout-aware pointer/keyboard controller over the selected backend."""

    def __init__(self, layout, preferred: str = "auto", uinput_path: str = DEFAULT_UINPUT,
                 ydotool_socket: str = DEFAULT_YDOTOOL_SOCKET) -> None:
        self.layout = layout
        self.backend = self._select(preferred, uinput_path, ydotool_socket)

    @staticmethod
    def _select(preferred: str, uinput_path: str, ydotool_socket: str):
        if preferred in ("auto", "uinput") and UinputBackend.available(uinput_path):
            return UinputBackend(uinput_path)
        if preferred in ("auto", "ydotool") and YdotoolBackend.available(ydotool_socket):
            return YdotoolBackend(ydotool_socket)
        raise InputError(
            "no input backend available: /dev/uinput is not readable+writable "
            f"({uinput_path}) and ydotool is not usable. Run the prep script "
            "(linux-computer-use-prep.sh) once (it adds a uaccess ACL on the "
            "device), then RESTART the app that launches this server - a "
            "long-running process keeps the permissions it started with."
        )

    @property
    def backend_name(self) -> str:
        return "uinput" if isinstance(self.backend, UinputBackend) else "ydotool"

    def reset_input(self) -> None:
        """Recreate the injection devices (used after a session restart)."""
        reset = getattr(self.backend, "reset", None)
        if reset is not None:
            reset()

    # -- pointer ----------------------------------------------------------- #
    def move(self, x: int, y: int) -> None:
        self.move_to(x, y)

    def move_to(self, x: int, y: int) -> None:
        self.backend.move(int(x), int(y), self.layout)

    def click(self, x: int | None = None, y: int | None = None, button: str = "left",
              double: bool = False, count: int = 1) -> None:
        if button not in ("left", "right", "middle"):
            raise ValueError("button must be left, right or middle")
        if x is not None and y is not None:
            self.move_to(x, y)
            time.sleep(0.05)
        for _ in range(max(1, count)):
            self.backend.button(button, True)
            time.sleep(0.02)
            self.backend.button(button, False)
            if _ < count - 1 or double:
                time.sleep(0.06)

    def press_hold(self, button: str = "left") -> None:
        self.backend.button(button, True)

    def release_hold(self, button: str = "left") -> None:
        self.backend.button(button, False)

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 400,
             button: str = "left") -> None:
        self.move_to(x1, y1)
        time.sleep(0.06)
        self.backend.button(button, True)
        steps = max(4, min(60, duration_ms // 12))
        for step in range(1, steps + 1):
            ratio = step / steps
            self.move_to(round(x1 + (x2 - x1) * ratio), round(y1 + (y2 - y1) * ratio))
            time.sleep(max(0.004, duration_ms / 1000 / steps))
        time.sleep(0.05)
        self.backend.button(button, False)

    def scroll(self, x: int | None = None, y: int | None = None, dx: int = 0, dy: int = 0) -> None:
        if x is not None and y is not None:
            self.move_to(x, y)
            time.sleep(0.03)
        self.backend.scroll(int(dx), int(dy))

    # -- keyboard ---------------------------------------------------------- #
    @staticmethod
    def parse_combo(combo: str) -> list[int]:
        parts = [part for part in re.split(r"\s*\+\s*", combo.strip()) if part]
        if not parts:
            raise ValueError("empty key combination")
        return [key_code(part) for part in parts]

    def key(self, combo: str) -> None:
        codes = self.parse_combo(combo)
        self.backend.press(codes[:-1])
        self.backend.tap_key(codes[-1])
        self.backend.release(codes[:-1])

    def type_text(self, text: str, delay_ms: int = 12) -> None:
        for char in text:
            self.backend.type_char(char)
            time.sleep(max(0.001, delay_ms / 1000))

    def close(self) -> None:
        self.backend.close()
