# Ubuntu Desktop Automation (Computer Use)

Gives the agent eyes and hands on a **Wayland GNOME** desktop: it can look at the
screen, walk the accessibility tree of a window, move the pointer, click, type,
use the clipboard, inspect processes and change GSettings.

The equivalent on Windows is a single `SendInput` call plus UI Automation. On
Wayland neither exists, so each capability has its own path:

| Capability | How it works here | Why |
| --- | --- | --- |
| Screenshot | `xdg-desktop-portal` **Screenshot** (one approval, remembered by the desktop) → PNG in ~0.5s. A **ScreenCast** + `pipewiresrc` session stays as a fallback (`DSH_LINUX_CU_SCREENCAST=1`) | Wayland forbids silent screen capture; `org.gnome.Shell.Screenshot` returns *AccessDenied* for clients, and the ScreenCast stream stalls on static content under GNOME 46 |
| Pointer / keyboard | `/dev/uinput`: the server creates an absolute tablet device, a wheel device and a full keyboard, and writes kernel input events | No `SendInput`. `xdotool` only reaches XWayland windows, and `ydotool` (fallback) cannot scroll |
| UI tree | **AT-SPI2** (`org.a11y.Bus`) | UI Automation equivalent |
| Clipboard | `wl-copy` / `wl-paste` | `xclip` does not work on Wayland |
| Settings ("registry") | `gsettings` / dconf | closest analogue to the registry |
| Lock state | `org.gnome.ScreenSaver.GetActive` over the session bus | input must stop while the lock screen holds the password field; screenshots stay allowed |
| Idle state | `org.gnome.Mutter.IdleMonitor.GetIdletime` over the session bus | synthetic input stops while the user is at the keyboard, so automation cannot fight them |
| Raising a window | `xdotool windowactivate` (XWayland), the AT-SPI default action, then Alt+Tab and Super+Tab with an accessibility check | native Wayland surfaces are invisible to X11, `_NET_ACTIVE_WINDOW` cannot be honoured by the compositor, and the AT-SPI default action is a no-op for most toolkits |
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
| Vision | `screenshot` — PNG + size + path; `region` crops before scaling (sharp text), `scale` sets pixels per logical pixel, `ref` crops to one element, `marks` draws numbered boxes around clickable elements and returns them in screenshot pixels; the first call asks for permission once |
| Tree | `ui_apps`, `ui_tree` (prune with `roles` + `compact`), `ui_find`, `ui_describe`, `ui_text` |
| Act on elements | `ui_action` (invoke/press/toggle), `ui_focus`, `ui_set_text` |
| Pointer | `click` (coordinates, or `text` to hit an element by name), `click_ref` (by accessibility ref), `move`, `drag`, `scroll`, `pointer_button` (hold/release) |
| Keyboard | `key` (`ctrl+shift+t`), `type_text` (Unicode via clipboard paste) |
| Clipboard | `clipboard_get`, `clipboard_set` |
| System | `process_list`, `process_kill`, `launch_app`, `settings_get`, `settings_set`, `settings_list_keys`, `notify`, `display_info`, `desktop_status` |
| Windows | `window_list` (titles + `active`), `window_activate` — raises a window by title substring; XWayland through `xdotool`, native Wayland by cycling Alt+Tab then Super+Tab and checking the accessibility tree (GNOME binds Alt+Tab to windows of the current app and Super+Tab to applications), and the reply names the strategy that worked |
| Waiting | `wait_until` — poll until `window` appears/disappears, `window_active` is raised, a `ui` node shows up, or `idle_at_least` ms pass; on timeout it returns `satisfied: false` with the last state instead of throwing |
| Unlocking (opt-in) | `unlock_screen` — only listed while the unlock policy is on; see below |

Coordinates are **logical desktop pixels**. Pass `space: "screenshot"` to give
coordinates in the pixels of the most recent screenshot instead; on a HiDPI
display those differ, and the tool converts for you. A cropped screenshot keeps
the offset, so `space: "screenshot"` still lands on the right spot.

Reading one window is much cheaper than reading the whole desktop:

```json
{"region": {"x": 120, "y": 140, "width": 900, "height": 620}, "scale": 1.0}
```

The desktop is 4480x1440 on HiDPI; scaled to 1400 px wide the same capture
turns 11 px text into mush, while the crop above stays at native sharpness and
costs a third of the tokens.

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

## Lock state and the opt-in unlock switch

While the session is locked, every input tool refuses to run: the lock screen
keeps the password field focused, so injected keystrokes would land inside it.
Screenshots keep working. `desktop_status` reports `session.locked`.

Unlocking is **off by default**. While it is off, the `unlock_screen` tool is not
listed at all — the model does not learn the capability exists. To switch it on,
store a password in the GNOME keyring from your own terminal:

```bash
python3 -u ~/.local/share/dsh-linux-computer-use/linux_desktop_mcp.py --set-unlock-password
python3 -u ~/.local/share/dsh-linux-computer-use/linux_desktop_mcp.py --unlock-status
python3 -u ~/.local/share/dsh-linux-computer-use/linux_desktop_mcp.py --unlock-policy off
```

The password lives in libsecret (never in a chat, a config file or the log), is
read only at the moment `unlock_screen` runs, and is dropped immediately after
typing. The call also has to carry `confirm: "unlock"`; it is the single
sanctioned exception to the lock-screen guard, and every other tool still
refuses while the screen is locked.

Switching it on means the lock screen stops a person at the keyboard, not
software running as you: any process of yours can read the keyring item. That
is why the default is off and why turning it back off afterwards is worth the
ten seconds.

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
