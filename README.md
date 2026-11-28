# dsh-plugin-linux-computer-use

[![npm](https://img.shields.io/npm/v/dsh-plugin-linux-computer-use.svg)](https://www.npmjs.com/package/dsh-plugin-linux-computer-use)
[![GitHub release](https://img.shields.io/github/v/release/BeiningCheng/dsh-plugin-linux-computer-use.svg)](https://github.com/BeiningCheng/dsh-plugin-linux-computer-use/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

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
| 窗口操作 | `window_list` 列出标题与是否 `active`；`window_rect` 给出某个窗口的屏幕矩形（按标题或应用名取，原生 Wayland 窗口会如实返回 `null`）；`window_activate` 按标题子串把窗口抬到前台——XWayland 走 `xdotool`，原生 Wayland 用 Alt+Tab / Super+Tab 循环并以无障碍树校验（GNOME 下 Alt+Tab 只切本应用窗口，Super+Tab 才切应用），返回里会说明这次用的是哪条路 |
| 等待条件 | `wait_until` 轮询直到满足条件：窗口出现/消失、某个窗口变成前台、某个 UI 节点出现、文件长到指定大小并停止变化（下载完成）、某块区域的像素发生变化（转圈结束、页面加载完）、或用户空闲满多少毫秒；超时返回 `satisfied: false` 与最后一轮观察到的状态，不抛错 |

32 个工具（打开解锁策略后 33 个），分组见 [docs/DETAIL.md](docs/DETAIL.md)。模型调用方式：`mcp__linux__screenshot`、`mcp__linux__click`、`mcp__linux__ui_find` ……

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

## 第三招：一次调用跑一整段流程

`sequence` 把多个工具合成一次调用：`{"steps": [{"op": "window_activate", "args": {"title": "嘉立创"}}, {"op": "click", "args": {"text": "保存"}}, {"op": "screenshot", "args": {"region": {...}, "scale": 1}}]}`。

回复是每步的 `{step, op, ok, ms, error?}` 加**最后一步**的输出（含图片），所以上面这段从 4 次模型往返变成 1 次。注入类步骤不必逐个写 `allow_active_input`，在最外层给 `true` 即可；某步允许失败就设 `stop_on_error: false`。

## 省钱与提速的两招

- **`ui_tree` 先过滤再读**：`roles: ["push button", "entry"]` 只保留你要的节点类型，祖先会折叠成 `ref`/`role`/`name` 的路径；再加 `compact: true` 去掉 rect/states/text。实测同一屏 140 节点/10.2 KB → 20 节点/0.85 KB。
- **`screenshot` 用 `region` + `scale: 1.0`**：先在 4480×1440 原图上裁剪再缩放，小字才清楚，token 也只有整屏的三分之一。
- **看不清哪个能点就用 `marks: true`**：截图会围上带编号的框，同时返回 `marks: [{id, ref, role, name, x, y, width, height}]`（坐标已经是截图像素）。之后 `click_ref {"ref": "e42"}` 直接点，不需要自己算坐标。
  - **注意（GNOME Wayland 实测）**：GTK 4 与 Chromium 的窗口**不向 AT-SPI 报告屏幕矩形**，这时画不出框，`marks` 会是空数组，改为返回 `clickable: [{ref, role, name}]`。对这些元素 `click_ref` 走的是无障碍的「点击」动作（不需要坐标）；没有动作的控件只能靠 `screenshot` + 坐标点击。Chrome 更是只暴露 `application` 与 `frame`，网页里的元素请优先用 `browser_*`（DOM）。

## 已知限制（重要，别踩坑）

- **Chromium / Electron / Qt 应用（Chrome、VS Code、微信、WPS、Bitwarden）暴露的 UI 树是空的**——这是平台事实，不是 bug。它们只能靠"截屏 + 坐标点击"，窗口一动就得重新截图。原生 GTK 应用用 `ui_find` + `ui_action` 很稳。
- AT-SPI 全量枚举约 3–4 秒（某个无响应应用会拖慢），所以应用列表有 5 秒缓存。
- 截屏必须走 portal：第一次会弹一次系统授权框（GNOME 记住你的选择），之后每次截图静默、约 0.5 秒。ScreenCast 流式路径（`DSH_LINUX_CU_SCREENCAST=1` 可强制）在 GNOME 46 上画面静止时不产帧，只当兜底。
- **锁屏时只截图、不注入**：服务器会向 `org.gnome.ScreenSaver.GetActive` 查询锁屏状态，处于锁屏时 `click` / `key` / `type_text` 等全部拒绝执行（锁屏焦点在密码框上，误打就会往密码里塞字符），`screenshot` 仍可用。`desktop_status` 的 `session.locked` 会告诉你当前状态。
- **你在用电脑时也不注入**：服务器用 `org.gnome.Mutter.IdleMonitor.GetIdletime` 查空闲时间，最近 60 秒内有键鼠动作就拒绝点击/输入（否则自动化会和你抢光标）。这一条可以放行：工具参数带 `allow_active_input: true`（模型在你明确让它代操作时会带），或设环境变量 `DSH_LINUX_CU_ALLOW_ACTIVE_INPUT=1`；阈值用 `DSH_LINUX_CU_IDLE_GUARD_MS` 调（默认 `60000`）。截图、读 UI 树、读文件不受影响。`desktop_status` 的 `session.idle_ms` 就是当前空闲毫秒数。
- **原生 Wayland 窗口没法被 X11 抬起**：Chrome、GNOME 自家应用在 `xdotool search` 里完全不存在，`window_activate` 对它们会用 Alt+Tab（本应用内切窗口）与 Super+Tab（切应用）循环，需要 uinput 键盘可用。如果三条路都失败，返回 `activated: false` 与 `tried` 明细，而不是假装成功。
- **只想截某一块屏，就传 `monitor`**：`{"monitor": "eDP-1", "scale": 1.0}`（或 `"primary"`）直接按连接器名截整块显示器，不用自己加 `x` 偏移；和 `region`/`ref` 互斥，返回值里会带 `monitor: {connector, x, y, width, height}`。
- **截图要看清小字就用 `region`**：整屏 4480×1440 缩到 1400 宽，11px 的小字会糊成一团；传 `{"region": {"x":120,"y":140,"width":900,"height":620}, "scale":1.0}` 只截那块、按原尺寸返回，既清楚又省 token。`space: "screenshot"` 会自动加回偏移，不用自己算。
- **多显示器时，先 `display_info` 再裁 `region`**：`region` 用的是**整块桌面**的逻辑坐标（例如 eDP-1 从 `x=2560` 起），不是某一块屏的局部坐标。拿整屏缩略图估坐标很容易把窗口和壁纸搞混（我就干过：截了左屏左上角，还以为计算器是空的）。`display_info` 会给出每个显示器的 `{connector, x, y, width, height, primary}`，照它算。
- **`click {text}` / `click_ref` 只对"暴露出来且有动作"的控件有效**：`click {text}` 的顺序是「AT-SPI 找元素 → 执行它的 click 动作 → 找不到才退回指针点击」。GTK 4 应用常常**平时不暴露按钮**（GNOME Calculator 的键盘区在 `ui_tree` 里只剩 `frame → panel`，`ui_find` 也找不到，数字键根本点不到），这时它会明确报 `no element whose name or text matches ...`，不会假装成功。先用 `ui_find` 确认元素存在再点。
- **怎么判断"点击真的生效了"**：别靠截图猜。用客观信号——窗口标题（`window_list`）、面包屑/路径、列表前后差异。实测：`click {text:"Home", app:"Thunar"}` → 标题变 `cheng - Thunar`，再 `click {text:"Open Parent"}` → `home - Thunar`，两次都可用标题核对。
- 只能工作在**当前登录的图形会话**里；SSH / 无头环境下没有桌面可控制。

## 安全模型

- 打开 `input` 组 + `/dev/uinput` 之后，**该账户下任何进程都能控制键鼠**。这就是这个能力的本质，没有免费午餐。
- 缓解：MCP 桥接默认 `disabled`；`/dev/uinput` 只对 `input` 组开放；`ydotoold` socket 0600；所有工具调用走 DSH 的审批体系。
- 日志在 `~/.cache/dsh-linux-cu/server.log`（超 1 MB 轮转）。不联网、不上报、不含遥测。

## 无人值守与锁屏

锁屏是这台机器的安全基线，插件默认不试图绕过它：`/dev/uinput` 注入的是真实内核输入事件，在锁屏界面上它与物理键盘没有区别——正因为如此，默认配置下锁屏时插件拒绝一切注入。

- **锁屏时能做**：截图（画面是锁屏本身，通常没用）、读文件、跑命令、写代码、git / npm 等一切非 GUI 工作。
- **锁屏时不能做**：点击、拖拽、滚轮、按键、输入文字、UI 树操作。`desktop_status` 的 `session.locked` 和 `linux_mcp_doctor` 的 `screen-lock` 项会告诉你当前状态。
- **想让"人不在场"也能做 GUI 任务**，干净的方向是给自动化一块**独立的虚拟显示器**（`org.gnome.Mutter.RemoteDesktop` 的 virtual monitor，GNOME 46 起可用），由 RemoteDesktop 接口往虚拟屏注入，物理屏保持锁定、你的桌面不被共享。这条路尚未实现，属于后续计划；不会走"关掉自动锁屏"或"存一份登录密码"这两条捷径。

### 解锁开关（默认关闭）

如果确实希望模型能自己解锁屏幕，有一个显式开关，**默认关闭**；关闭时 `unlock_screen` 工具根本不会出现在模型面前（工具列表里没有它，模型也就不知道有这个能力）。

```bash
MCP=~/.local/share/dsh-linux-computer-use/linux_desktop_mcp.py

python3 -u $MCP --set-unlock-password    # 在你自己终端里输入一次，隐藏回显，存进 GNOME 钥匙环
python3 -u $MCP --unlock-status          # 查看开关与是否已存密码
python3 -u $MCP --unlock-policy off      # 关掉（推荐随手关）
python3 -u $MCP --clear-unlock-password  # 从钥匙环删除
```

规则只有三条，但都是硬的：

1. **密码不经过对话、配置文件和日志**。它存在 GNOME 钥匙环（libsecret）里，由你本人的终端 `getpass` 写进去；服务器只在 `unlock_screen` 被调用的那一瞬间读取，用完立刻丢引用，日志里只有"第几次尝试成功"。
2. **解锁是唯一的例外**。开关打开后，其它注入工具在锁屏时照旧拒绝；只有一次明确的 `unlock_screen` 调用（还必须带 `confirm: "unlock"` 参数）才会去敲密码，成功后会话就是解锁状态，后续操作照常。
3. **打开它意味着什么，要说清楚**：锁屏从此只挡"坐在键盘前的人"，不再挡"以你身份运行的软件"。任何以你账户跑的进程都能从钥匙环读到那个密码——而这本来就是插件已经给你的能力范围。这就是为什么默认关闭、为什么建议用完随手 `--unlock-policy off`。

密码里如果有中文或其它非 ASCII 字符，注入会失败（`/dev/uinput` 的键盘设备只能敲有限字符集）；这种情况请继续用"回来自己解锁"。

## 故障排查

### Pointer events are ignored

On rare sessions GNOME keeps the virtual pointer device open but stops
honouring its motion: keyboard and scroll still work, clicks do nothing.

1. `desktop_status` with `reset_input: true` recreates the devices
2. if that does not help, reboot the machine (a logout/login is not always
   enough, because `dsh web` can survive it through systemd user lingering)
3. `dsh web` itself must run inside the current desktop session: its
   `DBUS_SESSION_BUS_ADDRESS` is inherited, so an old process cannot reach the
   accessibility bus or the portal any more



```bash
python3 -u ~/.local/share/dsh-linux-computer-use/linux_desktop_mcp.py --selftest
```

或在对话里让模型调用 `linux_mcp_doctor`。逐项报告：会话类型、Python/GI/Atspi/Gst、`/dev/uinput`、启动器、wl-clipboard、portal 版本、pipewiresrc、截屏授权、显示器、AT-SPI 应用数、**GNOME 窗口切换键位绑定**（Alt+Tab / Super+Tab 哪个有效），以及**正在运行的 server 副本是否与安装的包一致**。

## 兼容性

- Ubuntu 24.04 / GNOME 46 / Wayland（主要目标，实测环境）
- 需要 `python3-gi`、`gir1.2-atspi-2.0`、`gstreamer1.0-pipewire`、`gstreamer1.0-plugins-base`（Ubuntu 桌面版默认自带）
- X11 会话可以工作（注入与截屏另有路径），但不是本插件的优化目标

## License

MIT

---

### English

Linux desktop automation for DeepSeek Harness on Wayland. Screenshots go through the `xdg-desktop-portal` Screenshot interface (the only Wayland-legal path, ~0.5s per capture after one granted approval); pointer and keyboard input go straight to `/dev/uinput` so applications cannot tell them from a real user; the UI tree comes from AT-SPI2. 30 tools are published as `mcp__linux__*`, plus a `linux_mcp_doctor` host tool that diagnoses the environment even while the bridge is off. Ships disabled: enable it in Settings → Plugins.

Install: `dsh plugin --profile web add dsh-plugin-linux-computer-use`, run `sudo ./linux-computer-use-prep.sh`, restart `dsh`, then flip the switch.

Known limit: Chromium, Electron and Qt windows expose an empty accessibility tree, so they need screenshot-and-coordinates clicking rather than semantic element actions.
