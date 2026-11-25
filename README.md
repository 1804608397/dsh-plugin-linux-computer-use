# dsh-plugin-linux-computer-use

**Computer Use for a Linux desktop (Wayland/GNOME), bridged into DeepSeek Harness as MCP tools.**

给 DeepSeek Harness 装上"手和眼睛"：截屏、UI 树、鼠标键盘、剪贴板、进程、系统设置，全部以 `mcp__linux__*` 工具暴露给模型；外带一个 `linux_mcp_doctor` 诊断工具。**默认关闭**，在插件面板里手动打开。

English summary at the bottom.

---

## 它能做什么

| 能力 | 后端（为何） |
| --- | --- |
| 截屏 | `xdg-desktop-portal` ScreenCast（Wayland 下唯一合法路径）+ GStreamer `pipewiresrc`→PNG；授权一次后靠 restore_token 长期复用 |
| UI 树 / 找元素 / 点击元素 | AT-SPI2（`org.a11y.Bus`）——GNOME 原生应用完整可用 |
| 鼠标 / 键盘注入 | 直写 `/dev/uinput`（内核级，应用无法区分真人）；`ydotool` 作兜底 |
| 剪贴板 | `wl-clipboard`（`wl-copy` / `wl-paste`） |
| 进程 | `ps` + `/proc`，可列表、可杀（有白名单与自保） |
| "注册表"类比 | `gsettings` / dconf |
| 通知 | `notify-send` |
| 显示器信息 | Mutter `DisplayConfig`（坐标换算、多屏） |

28 个工具，分组见 [docs/DETAIL.md](docs/DETAIL.md)。模型调用方式：`mcp__linux__screenshot`、`mcp__linux__click`、`mcp__linux__ui_find` ……

## 安装

```bash
# 1. 装插件（npm 包名 / GitHub 仓库均可）
dsh plugin --profile web add dsh-plugin-linux-computer-use

# 2. 一次性准备系统权限（需要 sudo，幂等，可 --uninstall 回滚）
sudo ./linux-computer-use-prep.sh
sudo ./linux-computer-use-prep.sh --verify   # 不需要 sudo

# 3. 注销并重新登录（input 组只对新会话生效），然后重启 dsh
```

准备脚本做四件事：

1. `/dev/uinput` → `input` 组、0660，并持久化成 udev 规则；
2. 把当前用户加进 `input` 组；
3. 装 `wl-clipboard`（必需）和 `ydotool`（可选兜底；Ubuntu 24.04 的 ydotool 包里没有 `ydotoold` 守护进程，所以兜底路径默认不可用——主路径 `/dev/uinput` 不依赖它）；
4. 装一个固定路径启动器 `/usr/local/bin/dsh-linux-desktop-mcp`。

改完组之后**必须注销重登**，并且**重启启动服务器的那个进程**（比如 `dsh web`）：长驻进程保留它启动时的组列表，光重登不重启仍然是 `operation not permitted`。

## 打开

设置 → 插件 → **Linux 桌面控制 (Computer Use)** → 打开开关 → 刷新页面。

开着插件但没开 Computer Use 时也可以先跑 `linux_mcp_doctor`（不需要 MCP 在线），它会逐项告诉你缺什么、怎么补。

## 已知限制（重要，别踩坑）

- **Chromium / Electron / Qt 应用（Chrome、VS Code、微信、WPS、Bitwarden）暴露的 UI 树是空的**——这是平台事实，不是 bug。它们只能靠"截屏 + 坐标点击"，窗口一动就得重新截图。原生 GTK 应用用 `ui_find` + `ui_action` 很稳。
- AT-SPI 全量枚举约 3–4 秒（某个无响应应用会拖慢），所以应用列表有 5 秒缓存。
- 截屏必须走 portal：第一次会弹一次系统授权框，之后靠 `restore_token` 静默复用；令牌存在 `~/.cache/dsh-linux-cu/screencast.json`（0600）。
- 锁屏状态下无法注入（这是好事）。
- 只能工作在**当前登录的图形会话**里；SSH / 无头环境下没有桌面可控制。

## 安全模型

- 打开 `input` 组 + `/dev/uinput` 之后，**该账户下任何进程都能控制键鼠**。这就是这个能力的本质，没有免费午餐。
- 缓解：MCP 桥接默认 `disabled`；`/dev/uinput` 只对 `input` 组开放；`ydotoold` socket 0600；所有工具调用走 DSH 的审批体系。
- 日志在 `~/.cache/dsh-linux-cu/server.log`（超 1 MB 轮转）。不联网、不上报、不含遥测。

## 故障排查

```bash
python3 -u ~/.local/share/dsh-linux-computer-use/linux_desktop_mcp.py --selftest
```

或在对话里让模型调用 `linux_mcp_doctor`。逐项报告：会话类型、Python/GI/Atspi/Gst、`/dev/uinput`、启动器、wl-clipboard、portal 版本、pipewiresrc、截屏授权、显示器、AT-SPI 应用数。

## 兼容性

- Ubuntu 24.04 / GNOME 46 / Wayland（主要目标，实测环境）
- 需要 `python3-gi`、`gir1.2-atspi-2.0`、`gstreamer1.0-pipewire`、`gstreamer1.0-plugins-base`（Ubuntu 桌面版默认自带）
- X11 会话可以工作（注入与截屏另有路径），但不是本插件的优化目标

## License

MIT

---

### English

Linux desktop automation for DeepSeek Harness on Wayland. Screenshots go through `xdg-desktop-portal` ScreenCast (the only Wayland-legal path) into GStreamer; pointer and keyboard input go straight to `/dev/uinput` so applications cannot tell them from a real user; the UI tree comes from AT-SPI2. 28 tools are published as `mcp__linux__*`, plus a `linux_mcp_doctor` host tool that diagnoses the environment even while the bridge is off. Ships disabled: enable it in Settings → Plugins.

Install: `dsh plugin --profile web add dsh-plugin-linux-computer-use`, run `sudo ./linux-computer-use-prep.sh`, log out and back in, restart `dsh`, then flip the switch.

Known limit: Chromium, Electron and Qt windows expose an empty accessibility tree, so they need screenshot-and-coordinates clicking rather than semantic element actions.
