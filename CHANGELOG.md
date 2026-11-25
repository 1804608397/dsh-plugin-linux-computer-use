# Changelog

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
