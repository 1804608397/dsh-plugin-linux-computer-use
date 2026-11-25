# Ubuntu Desktop Automation (Computer Use)

Gives the agent eyes and hands on a **Wayland GNOME** desktop: it can look at the
screen, walk the accessibility tree of a window, move the pointer, click, type,
use the clipboard, inspect processes and change GSettings.

The equivalent on Windows is a single `SendInput` call plus UI Automation. On
Wayland neither exists, so each capability has its own path:

| Capability | How it works here | Why |
| --- | --- | --- |
| Screenshot | `xdg-desktop-portal` **ScreenCast** (one approval, remembered) → PipeWire → GStreamer → PNG | Wayland forbids silent screen capture; `org.gnome.Shell.Screenshot` returns *AccessDenied* for clients |
| Pointer / keyboard | `/dev/uinput`: the server creates an absolute tablet device, a wheel device and a full keyboard, and writes kernel input events | No `SendInput`. `xdotool` only reaches XWayland windows, and `ydotool` (fallback) cannot scroll |
| UI tree | **AT-SPI2** (`org.a11y.Bus`) | UI Automation equivalent |
| Clipboard | `wl-copy` / `wl-paste` | `xclip` does not work on Wayland |
| Settings ("registry") | `gsettings` / dconf | closest analogue to the registry |
| Processes | `ps` and `/proc` | portable |

## Requirements

Run the preparation script once (no logout needed):

```bash
sudo ./linux-computer-use-prep.sh        # packages, udev rule, ACL, launcher
./linux-computer-use-prep.sh --verify
```

It installs `wl-clipboard` (required), `ydotool` (optional fallback), creates
`/etc/udev/rules.d/60-uinput.rules` so `/dev/uinput` carries `TAG+="uaccess"`
(an ACL for the active desktop user) and also belongs to group `input`
with mode `0660`, adds you to that group, and installs a `ydotoold` unit. It is
idempotent and has `--uninstall`.

## Enabling

Computer Use ships **disabled**: enabling it lets the agent move your real
pointer and type on your real keyboard, so it is not something to switch on by
accident. Turn it on in **Settings → Plugins → Ubuntu Desktop Automation**, then
reload the page. `linux_mcp_doctor` works either way.

## Tools (`mcp__linux__*`)

| Group | Tools |
| --- | --- |
| Vision | `screenshot` — PNG + size + path; the first call asks for permission once |
| Tree | `ui_apps`, `ui_tree`, `ui_find`, `ui_describe`, `ui_text` |
| Act on elements | `ui_action` (invoke/press/toggle), `ui_focus`, `ui_set_text` |
| Pointer | `click`, `move`, `drag`, `scroll`, `pointer_button` (hold/release) |
| Keyboard | `key` (`ctrl+shift+t`), `type_text` (Unicode via clipboard paste) |
| Clipboard | `clipboard_get`, `clipboard_set` |
| System | `process_list`, `process_kill`, `launch_app`, `settings_get`, `settings_set`, `settings_list_keys`, `notify`, `window_list`, `display_info`, `desktop_status` |

Coordinates are **logical desktop pixels**. Pass `space: "screenshot"` to give
coordinates in the pixels of the most recent screenshot instead; on a HiDPI
display those differ, and the tool converts for you.

## What works well and what does not

* **Native GTK apps** (Files, Settings, Terminal, Text Editor) expose a real
  accessibility tree — prefer `ui_find` + `ui_action` there, it survives layout
  changes and works with the window in the background.
* **Chromium, Electron, Qt apps** (Chrome, VS Code, Slack, WeChat, WPS) expose
  an empty tree unless accessibility is force-enabled. For these, use
  `screenshot` + coordinate clicks. This is a platform limitation, not a bug
  in this bundle.
* Enumerating applications takes ~3–4 s because one unresponsive app stalls the
  a11y bus; results are cached for a few seconds.
* `type_text` falls back to clipboard-paste for characters the key table cannot
  type, which briefly overwrites the clipboard.

## Security

Any process running as your user can inject input once `/dev/uinput` is
writable — that is exactly the power being granted. Mitigations: `/dev/uinput`
is group `input` (not world-writable), the `ydotoold` socket is `0600`, and the
MCP entry is disabled until you enable it in the plugin panel.

`process_kill` refuses pid 1 and the server itself. `settings_set` only writes
GSettings keys; it cannot edit files.

## Diagnostics

`linux_mcp_doctor` checks the bridge, `/dev/uinput`, the Python GI stack, the
portal, GStreamer, the clipboard helpers, the display layout and AT-SPI, and
prints a `fix:` line for each problem. The server also answers
`python3 server/linux_desktop_mcp.py --selftest` and `--status` when run by hand.

Logs: `~/.cache/dsh-linux-cu/server.log`, screenshots:
`~/.cache/dsh-linux-cu/shots/`.
