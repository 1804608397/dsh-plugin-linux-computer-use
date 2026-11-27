"""AT-SPI2 accessibility-tree access for the Linux desktop MCP server.

Every X11 trick for "look at the UI" is unavailable on Wayland, so this module
reads the accessibility tree that GNOME exposes over ``org.a11y.Bus``. It gives
the model the same three things the Windows build got from UIA:

* a structural snapshot of the running applications and their widgets;
* stable element references the model can act on (``do_action``, ``focus``);
* text read/write and screen rectangles, so a click can target a widget
  instead of a guessed pixel.

Tree walks are bounded by both a node cap and a wall-clock deadline: a single
unresponsive application must not hang the whole server.
"""

from __future__ import annotations

import subprocess
import sys
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

STATE_FLAGS = (
    "ENABLED", "SENSITIVE", "SHOWING", "VISIBLE", "FOCUSED", "FOCUSABLE",
    "CHECKED", "EDITABLE", "SELECTED", "EXPANDED", "PRESSED",
)


_PROBE_CACHE: "list[bool]" = []


def a11y_reachable() -> bool:
    """True when Atspi.init() can run without aborting this process.

    libatspi calls g_error() when it cannot reach the accessibility bus, which
    kills the whole process instead of raising. The only safe test is to try it
    in a throwaway child, so do that once and cache the answer.
    """
    # Only a positive result is cached: the accessibility bus starts a moment
    # after the desktop session does, so a failure now can become a success a
    # second later.
    if _PROBE_CACHE:
        return _PROBE_CACHE[0]
    code = (
        "import gi;gi.require_version('Atspi','2.0');"
        "from gi.repository import Atspi;Atspi.init();print('ok')"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, timeout=20, check=False
        )
        ok = proc.returncode == 0 and b"ok" in proc.stdout
    except Exception:  # noqa: BLE001
        ok = False
    if ok:
        _PROBE_CACHE.append(True)
    return ok


class A11yError(RuntimeError):
    """The accessibility tree could not be read or the element is gone."""


def _count_nodes(node: dict) -> int:
    """How many nodes this subtree actually carries (after filtering)."""
    return 1 + sum(_count_nodes(child) for child in node.get("children", []))


def _state_names(acc) -> list[str]:
    try:
        state_set = acc.get_state_set()
    except Exception:  # noqa: BLE001 - dead proxies raise many GLib errors
        return []
    found = []
    for flag in STATE_FLAGS:
        value = getattr(Atspi.StateType, flag, None)
        if value is None:
            continue
        try:
            if state_set.contains(value):
                found.append(flag.lower())
        except Exception:  # noqa: BLE001
            break
    return found


def _extents(acc) -> dict | None:
    try:
        iface = acc.get_component_iface()
    except Exception:  # noqa: BLE001
        return None
    if iface is None:
        return None
    try:
        x, y, width, height = iface.get_extents(Atspi.CoordType.SCREEN)
    except Exception:  # noqa: BLE001
        return None
    if width <= 0 or height <= 0:
        return None
    return {"x": int(x), "y": int(y), "width": int(width), "height": int(height)}


def _actions(acc) -> list[str]:
    try:
        iface = acc.get_action_iface()
    except Exception:  # noqa: BLE001
        return []
    if iface is None:
        return []
    names = []
    try:
        count = iface.get_n_actions()
    except Exception:  # noqa: BLE001
        return []
    for index in range(min(count, 8)):
        try:
            names.append(iface.get_action_name(index) or f"action{index}")
        except Exception:  # noqa: BLE001
            break
    return names


def _text_preview(acc, limit: int = 160) -> str:
    try:
        iface = acc.get_text_iface()
    except Exception:  # noqa: BLE001
        return ""
    if iface is None:
        return ""
    try:
        count = iface.get_character_count()
        if not count:
            return ""
        text = iface.get_text(0, min(int(count), limit))
    except Exception:  # noqa: BLE001
        return ""
    return (text or "").replace("\n", " ").strip()


class A11y:
    """Thin, defensive wrapper around the AT-SPI2 tree."""

    def __init__(self) -> None:
        self._started = False
        self._registry: dict[str, object] = {}
        self._by_id: dict[int, str] = {}
        self._counter = 0
        self._apps: dict[str, object] = {}
        self._apps_cache: list[dict] | None = None
        self._apps_at = 0.0
        self.apps_ttl = 5.0

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> None:
        if self._started:
            return
        # libatspi aborts the whole process (dbind-ERROR) when the session bus
        # is gone, which is exactly what happens after the user logs out and
        # back in while this server keeps running. Check first so the tools can
        # report a fixable error instead of the server dying.
        if not a11y_reachable():
            raise A11yError(
                "the accessibility bus is unreachable: this server has no working "
                "session D-Bus connection. Restart the app that launched it (dsh web) "
                "from a terminal in the current desktop session."
            )
        try:
            # Tighten the D-Bus timeouts used for tree queries: one hung
            # application must not stall a whole snapshot. 1s is far above the
            # reply time of any healthy local toolkit.
            Atspi.set_timeout(1000, 3000)
        except Exception:  # noqa: BLE001 - older libatspi may not export it
            pass
        try:
            Atspi.init()
        except Exception as error:  # noqa: BLE001
            raise A11yError(f"cannot initialise AT-SPI: {error}") from error
        try:
            desktop = Atspi.get_desktop(0)
        except Exception as error:  # noqa: BLE001
            raise A11yError(
                "AT-SPI is not reachable; is at-spi2-core running and "
                "org.a11y.Bus available on the session bus? "
                f"({error})"
            ) from error
        if desktop is None:
            raise A11yError("AT-SPI returned no desktop; accessibility is unavailable")
        self._started = True

    def _desktop(self):
        self.start()
        return Atspi.get_desktop(0)

    # -- references -------------------------------------------------------- #
    def _ref(self, acc) -> str:
        key = id(acc)
        known = self._by_id.get(key)
        if known is not None and self._registry.get(known) is acc:
            return known
        self._counter += 1
        ref = f"e{self._counter}"
        self._registry[ref] = acc
        self._by_id[key] = ref
        if len(self._registry) > 4000:
            # Keep memory bounded: forget the oldest quarter of the refs.
            for stale in list(self._registry)[:1000]:
                self._registry.pop(stale, None)
            self._by_id = {id(obj): name for name, obj in self._registry.items()}
        return ref

    def resolve(self, ref: str):
        self.start()
        acc = self._registry.get(ref)
        if acc is None:
            raise A11yError(f"unknown element reference {ref!r}; take a fresh snapshot")
        return acc

    # -- applications ------------------------------------------------------ #
    def apps(self, max_age: float | None = None) -> list[dict]:
        ttl = self.apps_ttl if max_age is None else max_age
        if self._apps_cache is not None and (time.monotonic() - self._apps_at) < ttl:
            return [dict(item) for item in self._apps_cache]
        desktop = self._desktop()
        try:
            count = desktop.get_child_count()
        except Exception as error:  # noqa: BLE001
            raise A11yError(f"cannot enumerate applications: {error}") from error
        found = []
        self._apps.clear()
        for index in range(count):
            try:
                app = desktop.get_child_at_index(index)
            except Exception:  # noqa: BLE001
                continue
            if app is None:
                continue
            try:
                name = app.get_name() or ""
                role = app.get_role_name() or ""
                pid = app.get_process_id() or 0
            except Exception:  # noqa: BLE001
                continue
            ref = self._ref(app)
            self._apps[name] = app
            found.append({"ref": ref, "name": name, "role": role, "pid": int(pid)})
        found.sort(key=lambda item: item["name"].lower())
        self._apps_cache = found
        self._apps_at = time.monotonic()
        return [dict(item) for item in found]

    def _app(self, name: str | None):
        if not name:
            return None
        if not self._apps:
            self.apps()
        exact = self._apps.get(name)
        if exact is not None:
            return exact
        lowered = name.lower()
        for app_name, app in self._apps.items():
            if lowered in app_name.lower():
                return app
        return None

    # -- snapshot ---------------------------------------------------------- #
    def node(self, acc, depth: int, budget: dict, deadline: float,
             wanted_roles: set | None = None, compact: bool = False) -> dict | None:
        if budget["nodes"] <= 0 or time.monotonic() > deadline:
            budget["truncated"] = True
            return None
        budget["nodes"] -= 1
        try:
            role = acc.get_role_name() or ""
            name = acc.get_name() or ""
        except Exception:  # noqa: BLE001
            return None
        states = _state_names(acc)
        if "defunct" in states:
            # A dead application still answers AT-SPI; walking it wastes the budget.
            return None
        children: list[dict] = []
        if depth > 0:
            try:
                count = acc.get_child_count()
            except Exception:  # noqa: BLE001
                count = 0
            for index in range(count):
                child = self.node(acc.get_child_at_index(index), depth - 1, budget, deadline,
                                  wanted_roles, compact)
                if child is None:
                    if budget["truncated"]:
                        break
                    continue
                children.append(child)
        matched = True
        if wanted_roles is not None:
            lowered = role.lower()
            matched = any(needle in lowered for needle in wanted_roles)
        if not matched and not children:
            # Nothing here and nothing below is interesting: drop the node.
            return None
        item: dict = {"ref": self._ref(acc), "role": role}
        if name:
            item["name"] = name[:120]
        if matched and not compact:
            if states:
                item["states"] = states
            rect = _extents(acc)
            if rect:
                item["rect"] = rect
            actions = _actions(acc)
            if actions:
                item["actions"] = actions
            text = _text_preview(acc)
            if text:
                item["text"] = text
        elif matched and compact:
            actions = _actions(acc)
            if actions:
                item["actions"] = actions
        if children:
            item["children"] = children
        return item

    def snapshot(self, app: str | None = None, max_depth: int = 3,
                 max_nodes: int = 220, timeout_s: float = 20.0,
                 roles: list[str] | None = None, compact: bool = False) -> dict:
        self.start()
        deadline = time.monotonic() + timeout_s
        budget = {"nodes": max_nodes, "truncated": False}
        if app:
            target = self._app(app)
            if target is None:
                available = [item["name"] for item in self.apps()]
                raise A11yError(f"no application matches {app!r}; running: {available}")
            roots = [(target, app)]
        else:
            roots = []
            for item in self.apps()[:12]:
                target = self._apps.get(item["name"])
                if target is not None:
                    roots.append((target, item["name"]))
        wanted_roles = {item.lower() for item in roles} if roles else None
        tree = []
        for root, label in roots:
            node = self.node(root, max_depth, budget, deadline, wanted_roles, compact)
            if node is not None:
                node.setdefault("name", label)
                node["app"] = label
                tree.append(node)
        emitted = sum(_count_nodes(item) for item in tree)

        return {
            "applications": tree,
            "truncated": bool(budget["truncated"]),
            "nodes_used": max_nodes - max(0, budget["nodes"]),
            "nodes_returned": emitted,
        }

    # -- search ------------------------------------------------------------ #
    def find(self, query: str | None = None, role: str | None = None,
             app: str | None = None, limit: int = 20, timeout_s: float = 15.0) -> list[dict]:
        self.start()
        deadline = time.monotonic() + timeout_s
        needle = (query or "").lower()
        role_needle = (role or "").lower()
        matches: list[dict] = []
        queue: list[tuple] = []
        if app:
            target = self._app(app)
            if target is None:
                raise A11yError(f"no application matches {app!r}")
            queue.append(target)
        else:
            for item in self.apps()[:20]:
                target = self._apps.get(item["name"])
                if target is not None:
                    queue.append(target)
        visited = 0
        while queue and len(matches) < limit and time.monotonic() < deadline and visited < 6000:
            acc = queue.pop(0)
            visited += 1
            try:
                name = acc.get_name() or ""
                role_name = acc.get_role_name() or ""
            except Exception:  # noqa: BLE001
                continue
            haystack = f"{name}\n{_text_preview(acc, 200)}".lower()
            hit = (not needle or needle in haystack) and (not role_needle or role_needle in role_name.lower())
            if hit and (needle or role_needle):
                matches.append(self.describe_object(acc, name, role_name))
            try:
                count = acc.get_child_count()
            except Exception:  # noqa: BLE001
                count = 0
            for index in range(count):
                try:
                    queue.append(acc.get_child_at_index(index))
                except Exception:  # noqa: BLE001
                    continue
        return matches

    def describe_object(self, acc, name: str | None = None, role_name: str | None = None) -> dict:
        try:
            name = name if name is not None else (acc.get_name() or "")
            role_name = role_name if role_name is not None else (acc.get_role_name() or "")
        except Exception as error:  # noqa: BLE001
            raise A11yError(f"element is no longer available: {error}") from error
        info: dict = {"ref": self._ref(acc), "role": role_name}
        if name:
            info["name"] = name[:160]
        states = _state_names(acc)
        if states:
            info["states"] = states
        rect = _extents(acc)
        if rect:
            info["rect"] = rect
            info["center"] = [rect["x"] + rect["width"] // 2, rect["y"] + rect["height"] // 2]
        actions = _actions(acc)
        if actions:
            info["actions"] = actions
        text = _text_preview(acc, 400)
        if text:
            info["text"] = text
        try:
            description = acc.get_description() or ""
        except Exception:  # noqa: BLE001
            description = ""
        if description:
            info["description"] = description[:200]
        return info

    def describe(self, ref: str) -> dict:
        return self.describe_object(self.resolve(ref))

    # -- interaction ------------------------------------------------------- #
    def action(self, ref: str, action: str | int | None = None) -> dict:
        acc = self.resolve(ref)
        try:
            iface = acc.get_action_iface()
        except Exception as error:  # noqa: BLE001
            raise A11yError(f"element {ref} exposes no actions: {error}") from error
        if iface is None:
            raise A11yError(f"element {ref} exposes no actions")
        index: int | None = None
        names: list[str] = []
        try:
            count = iface.get_n_actions()
            names = [(iface.get_action_name(i) or "") for i in range(count)]
        except Exception:  # noqa: BLE001
            count = 0
        if isinstance(action, int):
            index = action
        elif isinstance(action, str) and action:
            lowered = action.lower()
            for position, name in enumerate(names):
                if name.lower() == lowered:
                    index = position
                    break
            if index is None:
                for position, name in enumerate(names):
                    if lowered in name.lower():
                        index = position
                        break
        else:
            index = 0 if count else None
        if index is None or (count and index >= count):
            raise A11yError(f"element {ref} has no matching action; available: {names}")
        try:
            done = bool(iface.do_action(index))
        except Exception as error:  # noqa: BLE001
            raise A11yError(f"action failed on {ref}: {error}") from error
        return {"ref": ref, "action": names[index] if index < len(names) else index, "performed": done}

    def focus(self, ref: str) -> dict:
        acc = self.resolve(ref)
        try:
            iface = acc.get_component_iface()
        except Exception as error:  # noqa: BLE001
            raise A11yError(f"element {ref} has no component interface: {error}") from error
        if iface is None:
            raise A11yError(f"element {ref} has no component interface")
        try:
            done = bool(iface.grab_focus())
        except Exception as error:  # noqa: BLE001
            raise A11yError(f"cannot focus {ref}: {error}") from error
        return {"ref": ref, "focused": done}

    def text(self, ref: str) -> dict:
        acc = self.resolve(ref)
        try:
            iface = acc.get_text_iface()
        except Exception as error:  # noqa: BLE001
            raise A11yError(f"element {ref} has no text interface: {error}") from error
        if iface is None:
            raise A11yError(f"element {ref} has no text interface")
        try:
            count = int(iface.get_character_count())
            value = iface.get_text(0, count) if count else ""
        except Exception as error:  # noqa: BLE001
            raise A11yError(f"cannot read text of {ref}: {error}") from error
        return {"ref": ref, "text": value or "", "length": count}

    def set_text(self, ref: str, value: str) -> dict:
        acc = self.resolve(ref)
        try:
            iface = acc.get_editable_text_iface()
        except Exception:  # noqa: BLE001
            iface = None
        if iface is None:
            raise A11yError(
                f"element {ref} is not directly editable; focus it and type instead"
            )
        try:
            ok = bool(iface.set_text_contents(value))
        except Exception as error:  # noqa: BLE001
            raise A11yError(f"cannot set text of {ref}: {error}") from error
        return {"ref": ref, "set": ok, "length": len(value)}
