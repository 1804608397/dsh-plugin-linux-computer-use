# Changelog

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
- Ships disabled: Computer Use is turned on in Settings → Plugins.
- `linux-computer-use-prep.sh`: idempotent sudo setup with `--verify` and `--uninstall`.
