# 待更新清单 / ROADMAP

> 记录于 2026-11-28，基线版本 **0.7.2**（33 个 `mcp__linux__*` 工具、`tests/` 29 个用例、GitHub Actions 绿）。
> 勾选框是给人看的 backlog：做完了就把 `[ ]` 改成 `[x]` 并写上版本号。

## 1. 下一个版本（0.8）

- [ ] **`watch` 工具（把轮询搬进 server）**：一次调用里连续观察，条件满足才返回，模型侧只有 1 次往返。
  - 签名草案：`watch {condition, interval_ms=500, timeout_ms=60000, image=false, region?}` → `{satisfied, waited_ms, polls, state, shot?}`
  - 条件复用现有 `wait_until` 的六种（window / window_active / ui / file / screenshot_changed / idle_at_least），再加一种 `screenshot_stable`（连续两帧哈希相同 = 动画停了）
  - 与 `wait_until` 的区别：`wait_until` 只观察不截图；`watch` 可以把「最后一帧」一起带回（`image: true`），于是「等页面加载完 → 看一眼 → 动手」变成一次调用
- [ ] **`ui_tree` 增量缓存**：按 app + 节点路径记住上次的 `ref`，只重扫 `states`/`rect` 变化的子树；目标是 IDE 这类大窗口从 300+ 节点降到几十，重复调用耗时减半。
- [ ] **`window_activate` 补 `Super+\``**：和 `Alt+Tab` / `Super+Tab` 一样进「无效组合立刻放弃」的循环，覆盖单窗口应用里切换同一应用的多个窗口。
- [ ] **README 补「隐藏窗口」**：`window_rect` 对最小化/隐藏窗口返回 `null` + `kind: "hidden"`，要让用户一眼看懂，而不是以为坏了。
- [ ] 顺手：`display_info` 输出里加每个显示器的逻辑 DPI，方便算 `scale`。

## 2. 工程化（0.9 之前完成，1.0 的门槛）

- [ ] **`docs/API.md`**：33 个工具的入参/返回/错误文案契约，冻结后改参数要写进 CHANGELOG 的 Breaking 段。
- [ ] **测试面补齐**：现在覆盖工具面、参数校验、纯逻辑；缺 `sequence` 的成功路径、`wait_until` 的四种条件、`window_rect` 的 X11 解析（后两条可用 monkeypatch）。
- [ ] **多桌面后端抽象**：把 GNOME 相关调用（Mutter IdleMonitor/DisplayConfig、portal、gsettings）收进 `backend_gnome.py`，给 KDE/Sway 留空实现，未实现时 doctor 明确报「不支持」而不是崩。
- [ ] **错误文案统一**：所有 `ToolError` 都带「下一步怎么办」的一句话（现在有些只报现象）。
- [ ] **prep 脚本可重入**：`linux-computer-use-prep.sh` 跑第二遍不应重复加 udev 规则/组。

## 3. 1.0 判定标准

- [ ] 工具面冻结 + `docs/API.md` 完整
- [x] 有 `tests/` 且 CI 绿（2026-11-28 达成）
- [ ] 安全默认值可审计：idle 守卫、锁屏守卫、keyring 默认 off、威胁模型写进 README 的单独一节
- [ ] 平台边界写死：只支持 GNOME 46+ / Wayland / XWayland，其他环境优雅降级并说明
- [ ] 文档闭环：README（是什么/怎么装/怎么用）、DETAIL（怎么实现）、CHANGELOG（改了什么）、ROADMAP（要做什么）、TROUBLESHOOTING（坏了怎么办）
- [ ] 硬指标：同一个脚本任务（如「打开计算器 → 7+8 → 读结果」）连跑 20 次零失败

## 4. 已知限制（不是 bug，短期不修）

| 限制 | 原因 | 规避 |
| --- | --- | --- |
| 原生 Wayland 窗口无法按标题抬到最前 | X11 看不见 Wayland 表面，只能 Super+Tab 轮转 | 用 `window_activate`，它会报告用了哪条路径 |
| GTK4 / Chromium 应用不暴露控件矩形 | AT-SPI 在该工具包下只报 application/frame | 用坐标点击，或改用 `browser_*`（Chrome 页面） |
| 锁屏后 / 登录界面下无法截图与读树 | portal 与 AT-SPI 都是会话内服务 | 解锁后再操作；`unlock_screen` 需 keyring 策略 |
| 截图依赖 portal 授权 | Wayland 无静默截屏 | 首次运行点一次授权，桌面会记住 |

## 5. 长期方向（按性价比排序）

- [ ] **OCR 兜底**：AT-SPI 拿不到控件时，用截图 + OCR 找文字位置再点；能救 GTK4/Electron/游戏这类硬骨头。
- [ ] **ScreenCast 持续流（暂缓，用户 2026-11-28 说「之后再说，不一定加」）**：常开一路 PipeWire 流可把变化检测延迟从 ~0.5s 降到 50–100ms，但 GNOME 46 静态画面不重绘、要处理 damage/keyframe、4K 编码吃 CPU，工期 1–2 天。先做 0.8 的 `watch`（把轮询搬进 server），够用就不做这个。
- [ ] **多模态模型直读截图**：不依赖无障碍树，直接看图和文字，代价是慢和贵。
- [ ] **跨平台端口**：Windows 用 UIA + SendInput，macOS 用 AXAPI + CGEvent；工具协议不变，只换后端。

## 6. 采用度（比功能更缺的这半边）

现状数据（2026-11-28 查）：

| 指标 | 数值 | 解读 |
| --- | --- | --- |
| npm 近 30 天 | 828 | 全集中在 11-25（352）与 11-27（439）两天 |
| 连占位版 `0.0.0-stage` | 156 | 没人用的版本也被下 156 次 → 是抓取不是人 |
| GitHub | 0 star / 0 fork / 14 天 4 个访客 | 真实人类用户个位数 |

- [ ] **README 首屏重写**：一张真实截图 + 三行安装命令 + 一句「能干什么」，30 秒判断要不要装。
- [ ] **一条演示视频/动图**：录「说一句话 → 它打开计算器按 7+8 → 读出 15」，放 README 顶部与 B 站。
- [ ] **prep 脚本失败时给可复制的命令**：现在只说「需要 sudo」，要直接打印可以粘到终端的整行。
- [ ] **一篇中文介绍**（知乎/B 站专栏）：讲清 Wayland 为什么难、这个插件怎么绕过去。
- [ ] **观察真实信号**：非发布日仍出现的下载、star、issue —— 这三样有一个非零才算「有人在用」。

## 7. 发布纪律（踩过的坑，别踩第二次）

- npm publish 必须真 TTY，否则授权链接被打印成 `***`。
- 授权链接约 10 分钟过期，过期后要重新 publish 拿新链接。
- publish 成功后 tarball 可能连续几分钟 404（Cloudflare 负缓存），加 `?cb=1` 立即 200；判定是否成功要看 `dist-tags` 与真实 `npm install`。
- `/tmp` 在工具调用之间会被清空，验证产物要放工作区。
- 改 `server/*.py` 后：插件面板只开关 MCP 条目**不会**同步文件，需要重启 `dsh web` 或手动 `cp` 到 `~/.local/share/dsh-linux-computer-use/`（0.4.0 起有 `.source` 面包屑会自动同步，但仍依赖 doctor 插件先 apply 一次）。
