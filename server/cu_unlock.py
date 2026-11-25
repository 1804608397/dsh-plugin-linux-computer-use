"""Opt-in screen unlocking (default: off).

The plugin never asks for, receives or logs the account password in a chat or a
config file.  When the user turns this on (``unlock: "keyring"``) they store the
password themselves in the GNOME keyring — ``loginctl``/libsecret — and the
server only reads it at the moment ``unlock_screen`` runs.

What it changes, stated plainly: with this on, the lock screen no longer stops
*software running as you*.  The way the password is typed is a real kernel
keyboard, so the lock screen cannot tell it apart from your hands.  The lock
screen keeps stopping a person at the keyboard, and nothing more.  That is why
the default is off and every call has to be an explicit ``unlock_screen``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from typing import Any

import cu_sys

SETTINGS_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "dsh-linux-cu",
)
SETTINGS_PATH = os.path.join(SETTINGS_DIR, "settings.json")

SERVICE = "dsh-linux-cu"
LABEL = "DSH Linux Computer Use unlock password"
KEYRING_SCHEMA = "org.dsh.linux.computer.use"

POLICIES = ("off", "keyring")
DEFAULT_POLICY = "off"

MAX_ATTEMPTS = 2


class UnlockError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #


def _account() -> str:
    return os.environ.get("USER") or os.environ.get("LOGNAME") or "user"


def load_settings() -> dict[str, Any]:
    data: dict[str, Any] = {}
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        data = {}
    # an environment override wins, so a profile can pin the policy
    env = os.environ.get("DSH_LINUX_CU_UNLOCK")
    if env:
        data["unlock"] = env
    policy = str(data.get("unlock") or DEFAULT_POLICY).strip().lower()
    if policy not in POLICIES:
        policy = DEFAULT_POLICY
    data["unlock"] = policy
    return data


def save_settings(**changes: Any) -> dict[str, Any]:
    data = load_settings()
    data.update(changes)
    os.makedirs(SETTINGS_DIR, mode=0o700, exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, SETTINGS_PATH)
    return data


def policy() -> str:
    return str(load_settings().get("unlock", DEFAULT_POLICY))


def set_policy(value: str) -> str:
    value = value.strip().lower()
    if value not in POLICIES:
        raise UnlockError(f"unknown unlock policy {value!r}; expected one of {', '.join(POLICIES)}")
    save_settings(unlock=value)
    return value


def unlock_enabled() -> bool:
    return policy() == "keyring"


# --------------------------------------------------------------------------- #
# keyring storage (libsecret, no plain files)
# --------------------------------------------------------------------------- #


def _attrs() -> dict[str, str]:
    return {"service": SERVICE, "account": _account()}


def _gi_secret():
    import gi

    gi.require_version("Secret", "1")
    from gi.repository import Secret  # noqa: WPS433

    return Secret


def _gi_schema(Secret):
    return Secret.Schema.new(
        KEYRING_SCHEMA,
        Secret.SchemaFlags.NONE,
        {
            "service": Secret.SchemaAttributeType.STRING,
            "account": Secret.SchemaAttributeType.STRING,
        },
    )


def store_password(password: str) -> None:
    if not password:
        raise UnlockError("empty password")
    try:
        Secret = _gi_secret()
        ok = Secret.password_store_sync(
            _gi_schema(Secret), _attrs(), Secret.COLLECTION_DEFAULT, LABEL, password, None
        )
        if not ok:
            raise UnlockError("libsecret refused to store the password")
        return
    except UnlockError:
        raise
    except Exception:
        pass
    # fallback: the secret-tool helper, password on stdin (never argv)
    binary = shutil.which("secret-tool")
    if not binary:
        raise UnlockError(
            "no keyring backend available; install libsecret-tools or gir1.2-secret-1"
        )
    proc = subprocess.run(
        [binary, "store", f"--label={LABEL}", "service", SERVICE, "account", _account()],
        input=password,
        text=True,
        capture_output=True,
        timeout=20,
    )
    if proc.returncode != 0:
        raise UnlockError(f"secret-tool store failed: {proc.stderr.strip()}")


def fetch_password() -> str | None:
    try:
        Secret = _gi_secret()
        value = Secret.password_lookup_sync(_gi_schema(Secret), _attrs(), None)
        if value:
            return str(value)
    except Exception:
        pass
    binary = shutil.which("secret-tool")
    if not binary:
        return None
    proc = subprocess.run(
        [binary, "lookup", "service", SERVICE, "account", _account()],
        text=True,
        capture_output=True,
        timeout=20,
    )
    if proc.returncode != 0:
        return None
    value = proc.stdout.rstrip("\n")
    return value or None


def clear_password() -> bool:
    removed = False
    try:
        Secret = _gi_secret()
        Secret.password_clear_sync(_gi_schema(Secret), _attrs(), None)
        removed = True
    except Exception:
        pass
    binary = shutil.which("secret-tool")
    if binary:
        proc = subprocess.run(
            [binary, "clear", "service", SERVICE, "account", _account()],
            text=True,
            capture_output=True,
            timeout=20,
        )
        removed = removed or proc.returncode == 0
    return removed


def has_password() -> bool:
    return fetch_password() is not None


# --------------------------------------------------------------------------- #
# unlocking
# --------------------------------------------------------------------------- #


def status() -> dict[str, Any]:
    policy_name = policy()
    stored = has_password() if policy_name == "keyring" else None
    return {
        "unlock_policy": policy_name,
        "password_stored": stored,
        "settings_path": SETTINGS_PATH,
        "available_policies": list(POLICIES),
    }


def unlock(controller, timeout_s: float = 8.0) -> dict[str, Any]:
    """Type the stored password into the lock screen.  Never echoes it."""
    if not unlock_enabled():
        raise UnlockError(
            'unlocking is switched off; set the unlock policy to "keyring" first '
            f"({SETTINGS_PATH})"
        )
    locked = cu_sys.screen_locked()
    if locked is False:
        return {"unlocked": True, "attempts": 0, "reason": "the session was not locked"}

    password = fetch_password()
    if not password:
        raise UnlockError(
            "no unlock password is stored; run "
            "`python3 -u ~/.local/share/dsh-linux-computer-use/linux_desktop_mcp.py "
            "--set-unlock-password` in your own terminal"
        )

    attempts = 0
    try:
        for attempts in range(1, MAX_ATTEMPTS + 1):
            controller.key("Escape")  # clear any half-typed leftovers
            time.sleep(0.3)
            controller.type_text(password)
            time.sleep(0.2)
            controller.key("Return")
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                time.sleep(0.5)
                if cu_sys.screen_locked() is False:
                    return {"unlocked": True, "attempts": attempts}
        raise UnlockError(
            f"the session is still locked after {attempts} attempt(s); "
            "the stored password may be wrong (or the screen is showing a system dialog)"
        )
    finally:
        password = None  # noqa: F841  (drop the reference as soon as possible)


def prompt_and_store() -> str:
    """Interactive store, for the human's own terminal."""
    import getpass

    first = getpass.getpass("Unlock password (input hidden, not logged): ")
    if not first:
        raise UnlockError("cancelled: empty password")
    second = getpass.getpass("Repeat to confirm: ")
    if first != second:
        raise UnlockError("cancelled: the two entries differ")
    store_password(first)
    set_policy("keyring")
    return "stored the password in the keyring and set unlock policy to keyring"
