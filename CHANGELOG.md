# Changelog

## 0.3.0 - 2026-11-27

- New `window_activate`: raise a window by title substring (or app name). Three
  strategies are tried in order and the reply says which one worked: `xdotool
  windowactivate` for XWayland surfaces, the AT-SPI default action for toolkits
  that honour it, and Alt+Tab cycling verified against the accessibility tree
  for native Wayland windows, which neither X11 nor AT-SPI can raise. Both
  Alt+Tab (windows of the current application) and Super+Tab (applications)
  are cycled, because GNOME binds them differently.
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
