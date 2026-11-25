# dsh-plugin-linux-computer-use

**Computer Use for Ubuntu / GNOME Wayland, bridged into DeepSeek Harness as MCP tools.**

给 DeepSeek Harness 装上"手和眼睛"：截屏、UI 树、鼠标键盘、剪贴板、进程、系统设置，全部以 `mcp__linux__*` 工具暴露给模型；外带一个 `linux_mcp_doctor` 诊断工具。**默认关闭**，在插件面板里手动打开。

English summary at the bottom.

---

## 它能做什么

| 能力 | 后端（为何） |
| --- | --- |
| 截屏 | `xdg-desktop-portal` Screenshot 接口（Wayland 下唯一合法路径）；首次点一次系统授权框，之后每次静默、约 0.5 秒出图。ScreenCast+`pipewiresrc` 流式路径保留作兜底（GNOME 46 下静态画面取不到帧） |
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

# 3. 重启 dsh（不需要注销重登，见下方说明）
```

准备脚本做四件事：

1. udev 规则给 `/dev/uinput` 加上 `uaccess`，并立刻 `setfacl` 授予你 `rw`（这是主路径：进程身份不变）；
2. 顺带把它设成 `input` 组、0660（备用路径，给没有 logind 的场景）；
3. 装 `wl-clipboard`（必需）和 `ydotool`（可选兜底；Ubuntu 24.04 的 ydotool 包里没有 `ydotoold` 守护进程，所以兜底路径默认不可用——主路径 `/dev/uinput` 不依赖它）；
4. 装一个固定路径启动器 `/usr/local/bin/dsh-linux-desktop-mcp`。

**不需要注销重登**：权限走 ACL（`u:cheng:rw`），不靠进程的组身份，所以哪怕宿主进程是很久以前启动的、哪怕 `systemd --user` 因为 `Linger=yes` 一直没重启，注入依然可用——只要重启一次 `dsh` 让 MCP 子进程重新拉起。

> 为什么不用 `sg input` 换组：`sg` 会把**真实 gid** 也切成 `input` 组的 995，而 `xdg-desktop-portal` 要用 `/proc/<pid>/root` 识别调用者身份，内核的 gid 检查会直接 `EACCES`，截屏就废了（实测报 `Portal operation not allowed: Unable to open /proc/<pid>/root`）。ACL 不碰进程身份，两条路都通。

## 打开

设置 → 插件 → **Ubuntu 桌面自动化 (Computer Use)** → 打开开关 → 刷新页面。

开着插件但没开 Computer Use 时也可以先跑 `linux_mcp_doctor`（不需要 MCP 在线），它会逐项告诉你缺什么、怎么补。

## 已知限制（重要，别踩坑）

- **Chromium / Electron / Qt 应用（Chrome、VS Code、微信、WPS、Bitwarden）暴露的 UI 树是空的**——这是平台事实，不是 bug。它们只能靠"截屏 + 坐标点击"，窗口一动就得重新截图。原生 GTK 应用用 `ui_find` + `ui_action` 很稳。
- AT-SPI 全量枚举约 3–4 秒（某个无响应应用会拖慢），所以应用列表有 5 秒缓存。
- 截屏必须走 portal：第一次会弹一次系统授权框（GNOME 记住你的选择），之后每次截图静默、约 0.5 秒。ScreenCast 流式路径（`DSH_LINUX_CU_SCREENCAST=1` 可强制）在 GNOME 46 上画面静止时不产帧，只当兜底。
- **锁屏时只截图、不注入**：服务器会向 `org.gnome.ScreenSaver.GetActive` 查询锁屏状态，处于锁屏时 `click` / `key` / `type_text` 等全部拒绝执行（锁屏焦点在密码框上，误打就会往密码里塞字符），`screenshot` 仍可用。`desktop_status` 的 `session.locked` 会告诉你当前状态。
- 只能工作在**当前登录的图形会话**里；SSH / 无头环境下没有桌面可控制。

## 安全模型

- 打开 `input` 组 + `/dev/uinput` 之后，**该账户下任何进程都能控制键鼠**。这就是这个能力的本质，没有免费午餐。
- 缓解：MCP 桥接默认 `disabled`；`/dev/uinput` 只对 `input` 组开放；`ydotoold` socket 0600；所有工具调用走 DSH 的审批体系。
- 日志在 `~/.cache/dsh-linux-cu/server.log`（超 1 MB 轮转）。不联网、不上报、不含遥测。

## 无人值守与锁屏

锁屏是这台机器的安全基线，插件不试图绕过它：`/dev/uinput` 注入的是真实内核输入事件，在锁屏界面上它与物理键盘没有区别——正因为如此，锁屏时插件拒绝一切注入。

- **锁屏时能做**：截图（画面是锁屏本身，通常没用）、读文件、跑命令、写代码、git / npm 等一切非 GUI 工作。
- **锁屏时不能做**：点击、拖拽、滚轮、按键、输入文字、UI 树操作。`desktop_status` 的 `session.locked` 和 `linux_mcp_doctor` 的 `screen-lock` 项会告诉你当前状态。
- **想让"人不在场"也能做 GUI 任务**，干净的方向是给自动化一块**独立的虚拟显示器**（`org.gnome.Mutter.RemoteDesktop` 的 virtual monitor，GNOME 46 起可用），由 RemoteDesktop 接口往虚拟屏注入，物理屏保持锁定、你的桌面不被共享。这条路尚未实现，属于后续计划；不会走"关掉自动锁屏"或"存一份登录密码"这两条捷径。

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

Linux desktop automation for DeepSeek Harness on Wayland. Screenshots go through the `xdg-desktop-portal` Screenshot interface (the only Wayland-legal path, ~0.5s per capture after one granted approval); pointer and keyboard input go straight to `/dev/uinput` so applications cannot tell them from a real user; the UI tree comes from AT-SPI2. 28 tools are published as `mcp__linux__*`, plus a `linux_mcp_doctor` host tool that diagnoses the environment even while the bridge is off. Ships disabled: enable it in Settings → Plugins.

Install: `dsh plugin --profile web add dsh-plugin-linux-computer-use`, run `sudo ./linux-computer-use-prep.sh`, restart `dsh`, then flip the switch.

Known limit: Chromium, Electron and Qt windows expose an empty accessibility tree, so they need screenshot-and-coordinates clicking rather than semantic element actions.
