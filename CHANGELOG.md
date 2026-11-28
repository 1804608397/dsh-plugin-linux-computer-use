# Changelog

## 0.6.1 - 2026-11-28

- `desktop_status` no longer fails when the login keyring is locked: the secret
  lookup gets a 6 second deadline and a locked keyring is reported as
  `password_stored: null` with an `error` line instead of a 20 second hang.
- Same guard on `unlock_screen`: it now says the keyring is locked instead of
  blocking the whole tool call.
- **Wayland apps publish no screen rectangles.** GTK 4 and Chromium report empty
  AT-SPI extents, so `marks: true` can draw no numbered boxes and `click_ref`
  cannot compute a centre point. `marks: true` now answers with a `clickable`
  list of `{ref, role, name}` instead of pretending nothing is on screen, and
  `click_ref` drives elements through their accessibility action first (which
  works without coordinates), falling back to a pointer click only when a
  rectangle exists. `actionable()` also scans 25 applications instead of 6, so
  the list is no longer empty when the interesting window is late in the list.
- server 1.5.1.

## 0.6.0 - 2026-11-28

- **One call can run a whole flow.** `sequence {steps: [{op, args}, ...]}` runs up
  to 25 tools in order and answers with a compact per-step report
  (`{step, op, ok, ms, error?}`), the run's `elapsed_ms`, and the **last** step's
  output — images included, so a "raise the window, click, type, screenshot" flow
  costs one round trip instead of four. `stop_on_error: false` keeps going and
  reports every failure; `allow_active_input: true` applies the idle-guard escape
  hatch to every injecting step at once instead of repeating it in each step.
  A sequence cannot nest another sequence (the cap is 25 steps, override with
  `DSH_LINUX_CU_SEQUENCE_MAX_STEPS`).
- `ok` is true only when *every* step succeeded; `stopped_at` names the step that
  halted the run.
- Server 1.5.0.

## 0.5.0 - 2026-11-27

- **Screenshots can label what is clickable.** `screenshot {marks: true}` draws a
  numbered box around every button, entry, link, tab and menu item it can see in
  the captured area (pycairo, already on the machine through GTK), and returns
  the same numbers as `marks: [{id, ref, role, name, x, y, width, height}]` in
  *screenshot* pixels — so a model can point at "box 4" instead of guessing
  coordinates. `marks_limit` caps how many boxes are drawn (default 40).
- `screenshot {ref: "<ref>"}` crops straight to one element's rectangle, which
  makes "look closely at this button" a single call.
- `click_ref {ref: "<ref>"}` clicks an element by its accessibility reference
  (the `ref` that `ui_tree`, `ui_find` and the new `marks` hand out), and
  `click {text: "Save"}` clicks the first element whose name or text matches —
  no coordinates at all. Both run through the same lock and idle guards as
  `click`, and both accept `allow_active_input`.
- An empty `marks` list is explained in the note (a locked session hides most of
  the tree) instead of looking like a silent failure.
- Server 1.4.0.

## 0.4.0 - 2026-11-27

- **The server now refreshes itself.** The bundle copied `server/*.py` into
  `~/.local/share/dsh-linux-computer-use` when the *doctor* plugin loaded, but
  the MCP server is started by a separate patch entry: toggling only that entry
  (the normal thing to do in Settings → Plugins) or upgrading the npm package
  left a stale copy running, with no visible sign. The sync now also drops a
  `.source` breadcrumb, and the server compares itself against that package
  before doing anything, copies the newer files and re-executes — so a restart
  is enough, and a stale code path can no longer survive one.
- `linux_mcp_doctor` grew two preflight checks: `keybindings` (which of
  Alt+Tab / Super+Tab are actually bound, because the keyboard fallback of
  `window_activate` depends on it) and `server-copy` (whether the running copy
  is in step with the installed package). Both also run from
  `linux_desktop_mcp.py --selftest`.
- `ui_tree` can now be pruned: pass `roles` (e.g. `["push button", "entry"]`)
  to keep only the kinds of node you care about — ancestors collapse into a
  bare `ref`/`role`/`name` path — and `compact` to drop rectangles, states and
  text previews. Measured on this machine: 140 nodes / 10.2 KB → 20 nodes /
  0.85 KB for the same screen, and filtered trees on a real application save
  far more. The reply now reports `nodes_returned` next to `nodes_used`.
- Defunct applications are no longer walked (they still answer AT-SPI and
  quietly ate the node budget).
- MCP server version 1.3.0.

## 0.3.0 - 2026-11-27

- New `window_activate`: raise a window by title substring (or app name). Three
  strategies are tried in order and the reply says which one worked: `xdotool
  windowactivate` for XWayland surfaces, the AT-SPI default action for toolkits
  that honour it, and Alt+Tab cycling verified against the accessibility tree
  for native Wayland windows, which neither X11 nor AT-SPI can raise. Both
  Alt+Tab (windows of the current application) and Super+Tab (applications)
  are cycled, because GNOME binds them differently. A combination whose first
  press moves no window is abandoned straight away, so raising a window of
  another application takes one Super+Tab instead of thirteen Alt+Tabs.
- New `wait_until`: poll until a condition holds instead of sleeping a fixed
  time. Conditions are a window appearing or disappearing
  (`{"window": {"title": "...", "exists": false}}`), a window becoming active
  (`{"window_active": "Chrome"}`), an accessibility node showing up
  (`{"ui": {"app": "...", "name": "...", "role": "..."}}`), or the user being
  idle for a while (`{"idle_at_least": 60000}`). On timeout it returns
  `satisfied: false` with the last observed state instead of throwing.
- `window_list` now reports `active` from the AT-SPI `ACTIVE` state, which is
  the flag most toolkits set on the toplevel frame (`FOCUSED` alone missed
  them, so every window used to look inactive).
- MCP server version 1.2.0.

## 0.2.0 - 2026-11-27

- `screenshot` gained `region` and `scale`. Cropping before downscaling is the
  fix for HiDPI desktops: 11 px UI text on a 4480x1440 desktop used to turn to
  mush once the capture was scaled to 1400 px wide, and re-reading a whole
  screen to make out one label burned most of the tokens in a session. A
  cropped capture keeps native sharpness and costs a fraction of the tokens.
  `space: "screenshot"` adds the crop offset back, so coordinates stay correct.
- New idle guard: synthetic input is refused while the user has touched the
  keyboard or pointer in the last 60 seconds (`org.gnome.Mutter.IdleMonitor`),
  so automation cannot fight the person at the machine. Pass
  `allow_active_input: true` on any input tool — the model does so when the
  user explicitly asked it to drive — or set
  `DSH_LINUX_CU_ALLOW_ACTIVE_INPUT=1`; tune the window with
  `DSH_LINUX_CU_IDLE_GUARD_MS`. Screenshots, UI reads and clipboard reads stay
  available, and `desktop_status` reports `session.idle_ms`.
- `capture_shot` now converts logical coordinates to the physical pixels the
  portal returns, so `region` is correct on fractional HiDPI scaling.
- MCP server version 1.1.0.

## 0.1.1 - 2026-11-25

- fix: send the complete `input_absinfo` when creating the absolute pointer.
  libinput rejected the device with "kernel bug: kernel has only x or y
  resolution, not both" and silently dropped every motion and button event, so
  clicks did nothing while the keyboard and the wheel kept working.

## 0.1.1 - 2026-11-25

- `desktop_status` gained `reset_input`, which recreates the virtual keyboard,
  pointer and wheel devices. The compositor can stop honouring devices that
  were created before the desktop session restarted, and this is the cheapest
  way to recover without restarting the server.
- The accessibility tree no longer takes the whole server down when AT-SPI is
  unreachable: the reachability test runs in a throwaway child process, so a
  dead accessibility bus turns into a normal tool error instead of a crash.
  Logging out and back in while the server keeps running is exactly the case
  that used to kill it.

## 0.1.0

First release.

- 28 desktop tools over a stdio MCP server, published to the model as `mcp__linux__*`:
  screenshots, AT-SPI UI tree (apps/tree/find/describe/text/action/focus/set_text),
  pointer and keyboard input, clipboard, processes, app launching, gsettings, notifications,
  window list, display info and a health snapshot.
- `linux_mcp_doctor` host tool: diagnoses the session, `/dev/uinput`, the launcher,
  wl-clipboard, the portal version, PipeWire capture, the screenshot grant, the display
  layout and AT-SPI availability — and works while the MCP bridge is off.
- Screenshots via the `xdg-desktop-portal` Screenshot interface: one approval, then silent
  captures in about half a second. The ScreenCast + GStreamer path is kept as an opt-in
  fallback (`DSH_LINUX_CU_SCREENCAST=1`).
- Input injection straight into `/dev/uinput` (absolute pointer, wheel device, full keyboard),
  with `ydotool` as the fallback backend.
- Refuses pointer/keyboard injection while the session is locked (screenshots keep working),
  and reports `session.locked` in `desktop_status`.
- Opt-in AI unlocking, **off by default**: with the default policy the `unlock_screen` tool is
  not listed at all. When switched on, the password is stored in the GNOME keyring from the
  user's own terminal (`--set-unlock-password`), read only at the moment of the call, never
  written to the conversation, a config file or the log; the call must pass `confirm: "unlock"`
  and is the only exception to the lock-screen guard. Managed with
  `--unlock-policy off|keyring`, `--unlock-status`, `--clear-unlock-password`.
- Ships disabled: Computer Use is turned on in Settings → Plugins.
- `linux-computer-use-prep.sh`: idempotent sudo setup with `--verify` and `--uninstall`.
