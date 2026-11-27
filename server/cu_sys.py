"""System-side tools for the Linux desktop MCP server.

Covers the pieces the Windows build got from Win32 that have no single Wayland
equivalent:

* clipboard        -> wl-clipboard (``wl-copy`` / ``wl-paste``)
* process list     -> ``ps`` (portable, no psutil dependency)
* app launch       -> fork/exec of an argv, detached from the server
* "registry"       -> ``gsettings`` / ``dconf`` (the user-facing settings store)
* notifications    -> ``org.freedesktop.Notifications`` over the session bus
* lock state       -> ``org.gnome.ScreenSaver.GetActive`` over the session bus
* capability probe -> what the doctor tool reports

Everything shells out to the system binaries the prep script installs, so the
module keeps working when those packages are missing: each helper reports a
precise reason instead of raising a traceback into the MCP layer.
"""

from __future__ import annotations

import os
import shutil
import subprocess

RUN_TIMEOUT = 10
WL_COPY = "wl-copy"
WL_PASTE = "wl-paste"
GSETTINGS = "gsettings"
DCONF = "dconf"


class SysError(RuntimeError):
    """A system-side operation failed (missing binary, bad argument, ...)."""


def _run(argv: list[str], timeout: float = RUN_TIMEOUT, stdin: str | None = None) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            argv,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as error:
        raise SysError(f"{argv[0]} is not installed") from error
    except subprocess.TimeoutExpired as error:
        raise SysError(f"{argv[0]} timed out after {timeout:.0f}s") from error
    return completed.returncode, completed.stdout, completed.stderr


def screen_locked() -> bool | None:
    """Report whether the GNOME session is locked (``True``/``False``/``None``).

    A locked screen still allows screenshots but must never receive injected
    keystrokes or clicks: the lock screen's focused widget is the password
    field, so synthetic input would type a password nobody asked for. ``None``
    means the state could not be read (no session bus, no screen saver
    service), in which case callers keep working and rely on the user.
    """
    try:
        import gi

        gi.require_version("Gio", "2.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import Gio, GLib
    except (ImportError, ValueError):
        return None
    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = connection.call_sync(
            "org.gnome.ScreenSaver",
            "/org/gnome/ScreenSaver",
            "org.gnome.ScreenSaver",
            "GetActive",
            None,
            GLib.VariantType.new("(b)"),
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )
        return bool(reply.unpack()[0])
    except Exception:  # noqa: BLE001 - a missing service is not an error here
        return None


def idle_ms() -> int | None:
    """Milliseconds since the last keyboard or pointer event, or ``None``.

    Mutter's IdleMonitor is the compositor's own idea of idleness, so it sees
    input delivered to any client (including the ones synthetic input misses).
    ``None`` means the service is not there (not GNOME, or a very old release);
    callers then fall back to behaving as before.
    """
    try:
        import gi

        gi.require_version("Gio", "2.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import Gio, GLib
    except (ImportError, ValueError):
        return None
    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = connection.call_sync(
            "org.gnome.Mutter.IdleMonitor",
            "/org/gnome/Mutter/IdleMonitor/Core",
            "org.gnome.Mutter.IdleMonitor",
            "GetIdletime",
            None,
            GLib.VariantType.new("(t)"),
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )
        return int(reply.unpack()[0])
    except Exception:  # noqa: BLE001 - a missing service is not an error here
        return None


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


# --------------------------------------------------------------------------- #
# clipboard
# --------------------------------------------------------------------------- #
def clipboard_available() -> bool:
    return have(WL_COPY) and have(WL_PASTE)


def clipboard_get() -> dict:
    if not clipboard_available():
        raise SysError("wl-clipboard is missing; run the prep script to install wl-clipboard")
    code, out, err = _run([WL_PASTE, "--no-newline"], timeout=8)
    if code != 0:
        raise SysError(f"wl-paste failed: {err.strip() or 'no clipboard content'}")
    return {"text": out, "length": len(out)}


def clipboard_set(text: str) -> dict:
    if not clipboard_available():
        raise SysError("wl-clipboard is missing; run the prep script to install wl-clipboard")
    # wl-copy forks into the background to keep serving the selection.
    code, _out, err = _run([WL_COPY], timeout=8, stdin=text)
    if code != 0:
        raise SysError(f"wl-copy failed: {err.strip()}")
    return {"ok": True, "length": len(text)}


# --------------------------------------------------------------------------- #
# processes
# --------------------------------------------------------------------------- #
def process_list(limit: int = 25, sort: str = "cpu", name_filter: str | None = None) -> list[dict]:
    sort_key = {"cpu": "-pcpu", "mem": "-pmem", "pid": "pid"}.get(sort, "-pcpu")
    code, out, err = _run(
        ["ps", "-eo", "pid=,ppid=,user=,pcpu=,pmem=,etime=,comm=,args=", f"--sort={sort_key}"],
        timeout=8,
    )
    if code != 0:
        raise SysError(f"ps failed: {err.strip()}")
    rows: list[dict] = []
    needle = (name_filter or "").lower()
    for line in out.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        pid, ppid, user, cpu, mem, elapsed, comm, args = parts
        if needle and needle not in comm.lower() and needle not in args.lower():
            continue
        rows.append({
            "pid": int(pid),
            "ppid": int(ppid),
            "user": user,
            "cpu_percent": float(cpu),
            "mem_percent": float(mem),
            "elapsed": elapsed,
            "command": comm,
            "args": args.strip()[:300],
        })
        if len(rows) >= max(1, min(limit, 200)):
            break
    return rows


def process_kill(pid: int, signal_name: str = "TERM") -> dict:
    if pid <= 1:
        raise SysError("refusing to signal pid 1 or an invalid pid")
    if pid == os.getpid():
        raise SysError("refusing to signal the desktop server itself")
    allowed = {"TERM", "KILL", "INT", "HUP"}
    if signal_name not in allowed:
        raise SysError(f"unsupported signal {signal_name!r}; use one of {sorted(allowed)}")
    code, _out, err = _run(["kill", f"-{signal_name}", str(pid)], timeout=5)
    if code != 0:
        raise SysError(f"kill {pid} failed: {err.strip()}")
    return {"pid": pid, "signal": signal_name, "sent": True}


# --------------------------------------------------------------------------- #
# launching applications
# --------------------------------------------------------------------------- #
def launch(argv: list[str] | str, wait_ms: int = 1200) -> dict:
    if isinstance(argv, str):
        import shlex

        args = shlex.split(argv)
    else:
        args = list(argv)
    if not args:
        raise SysError("no command given")
    binary = args[0]
    if not (have(binary) or os.path.isabs(binary)):
        raise SysError(f"{binary!r} is not an executable on PATH")
    try:
        process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        raise SysError(f"cannot launch {binary}: {error}") from error
    return {"pid": process.pid, "command": args, "started": True}


# --------------------------------------------------------------------------- #
# settings ("registry")
# --------------------------------------------------------------------------- #
def settings_get(schema: str, key: str) -> dict:
    code, out, err = _run([GSETTINGS, "get", schema, key], timeout=5)
    if code != 0:
        raise SysError(f"gsettings get {schema} {key} failed: {err.strip()}")
    return {"schema": schema, "key": key, "value": out.strip()}


def settings_set(schema: str, key: str, value: str) -> dict:
    code, _out, err = _run([GSETTINGS, "set", schema, key, value], timeout=5)
    if code != 0:
        raise SysError(f"gsettings set {schema} {key} failed: {err.strip()}")
    return {"schema": schema, "key": key, "value": value, "ok": True}


def settings_list_keys(schema: str) -> list[str]:
    code, out, err = _run([GSETTINGS, "list-keys", schema], timeout=5)
    if code != 0:
        raise SysError(f"gsettings list-keys {schema} failed: {err.strip()}")
    return [line.strip() for line in out.splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# notifications
# --------------------------------------------------------------------------- #
def notify(summary: str, body: str = "", timeout_ms: int = 8000) -> dict:
    if not have("notify-send"):
        raise SysError("notify-send is missing (install libnotify-bin)")
    argv = ["notify-send", "--app-name=DSH Computer Use", "-t", str(int(timeout_ms)), summary]
    if body:
        argv.append(body)
    code, _out, err = _run(argv, timeout=8)
    if code != 0:
        raise SysError(f"notify-send failed: {err.strip()}")
    return {"sent": True}
