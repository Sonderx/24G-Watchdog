# 2.4G 键鼠电量监控 — Agent 工作手册

> 项目级 `AGENTS.md`。DSH、Codex 等会在新会话开始时自动读取；Claude Code 读取的是 `CLAUDE.md`（可用 `@AGENTS.md` 引用本文件）。
> 最后更新：2026-11-21（键盘省电重构 + Raw Input 自动刷新）
> 平台：Windows 10/11 · 运行时：Python 3.14 · 无构建步骤
> 代码是最终事实；本文件说明“为什么、边界、不能做什么”。与用户当前指令冲突时，以用户指令为准。

## 0. 新 Agent 先读这里

**一句话**：单文件 Python 系统托盘程序，读雷蛇鼠标日志、通过 VGN 厂商 HID 查键盘电量。**键盘查询必须按需触发，禁止后台轮询**，否则会持续把键盘叫醒、耗电加快。

**阅读顺序**：0 → 1 → 2.3 → 4 → 5 → 7；第 3、6、8 节按需。

**三条红线**：

1. 不在 `worker_loop`、定时器或“顺手加个实时刷新”里周期性查询键盘。
2. 键盘 RF 查询只能通过 `HardwareMonitor.probe_keyboard()`；`force=True` 只给用户主动刷新、有线轮询或端点变化。
3. 键盘休眠（`0xFF` 或 10 分钟无按键）后不再发任何 `0x1A` 探测包，只显示缓存值。

**10 分钟验证**：

1. 启动后看 `monitor.log`：`app start` → `kb-probe reason=startup`。
2. 按一下键盘：`rawinput: VGN key activity detected`，约 3 秒后 `kb-probe reason=activity`（若距上次成功查询不足 30 分钟，会跳过 probe，这是正常的）。
3. 静置 10 分钟：`kb state -> sleep (no key activity)`；之后日志不应再有 `kb-probe`。
4. 键盘背光/指示灯应正常熄灭；24 小时电量下降应与不开软件接近。

**不要做**：把 60/180 秒轮询加回来；把 timeout/未知当 0% 报警；在 DSH 沙箱里测托盘（`pystray` 会被系统的 `ChangeWindowMessageFilterEx` 拒绝）。

## 1. 项目结构与运行

| 文件 | 作用 |
|---|---|
| [battery_monitor.py](battery_monitor.py) | 全部逻辑：采集、状态机、托盘、浮窗、设置、Toast、Raw Input、单实例 |
| [config.json](config.json) | 用户配置（JSON，UTF-8） |
| [monitor.log](monitor.log) / `monitor.log.1` | 运行日志；256 KB 自动轮转 |
| [README.md](README.md) | 用户向功能与启动说明 |
| [AGENTS.md](AGENTS.md) | 本文件 |
| [启动键鼠电量监控(无控制台黑框).bat](<启动键鼠电量监控(无控制台黑框).bat>) | 用 `pythonw.exe` 静默启动 |

- **平台**：Windows 10/11，仅此；依赖 Win32 / HID / `winrt` / `pystray`。
- **运行时**：Python 3.14；依赖 `pystray`、`Pillow`、`winrt`、`pywin32`；`tkinter` 为标准库。
- **启动**：`python battery_monitor.py`（调试）或上面的 `.bat`（推荐，无黑框）。
- **托盘限制**：必须有交互式桌面会话；在无桌面的服务、远程无会话或受限沙箱里，`pystray` 无法创建托盘。
- **无构建、无测试框架**：验证靠手动 + `monitor.log`；改动后至少做语法编译和一次真实启动。

**关键代码地图**：

| 符号 | 职责 |
|---|---|
| `find_vgn_hid_endpoints()` | SetupAPI 枚举 `VID_320F` 的厂商集合；有线 `0x5055` 优先于 2.4G `0x5088` |
| `HardwareMonitor.query_vgn()` | 一次有界 HID 查询，返回 `QueryResult(state=online/asleep/timeout/error/no_endpoint, ...)` |
| `HardwareMonitor.probe_keyboard(reason, force)` | **唯一** RF 查询入口；锁 + 去抖 + 休眠拦截 |
| `HardwareMonitor.tick_keyboard()` | 每 4 秒跑一次的策略：启动查询、有线轮询、休眠判定、按键后刷新；**不主动查** |
| `HardwareMonitor.refresh_endpoint()` | 每 60 秒 access-0 枚举（不发 report），发现插拔/有线切换才查询 |
| `HardwareMonitor.note_kb_activity()` | Raw Input 回调；只记时间戳，不记键值 |
| `KeyboardActivityWatcher` | 隐藏窗口 + `RIDEV_INPUTSINK`；过滤 VGN 设备 |
| `Win10Flyout` / `SettingsWindow` / `BatteryApp` | 浮窗 / 设置 / 生命周期与托盘 |
| `__main__` 单实例块 | 命名互斥量 + 旧实例提示 |

## 2. 硬件与协议

### 2.1 鼠标：Razer DeathAdder V3 Pro

- 硬件标识：`VID_1532` / `PID_00B7`。
- 读取策略：从 Razer 守护引擎日志 `%LOCALAPPDATA%\Razer\RazerAppEngine\User Data\Logs\systray_systrayv2*.log` 以 `FILE_SHARE_READ | FILE_SHARE_WRITE` 只读打开，读最后 64 KB，取最后一条 `"powerStatus":{"chargingStatus":"...","level":N}`。
- 轮询间隔：`poll_interval_sec`，默认 4 秒；只读日志，不发 HID。
- 充电状态：`chargingStatus` 为 `charging` 或 `on` 视为充电。
- 该读取不消耗鼠标电池（Razer 守护进程本身在维护日志）。

### 2.2 键盘：VGN S99（2.4G Dongle / 有线兼容）

- 硬件标识：`VID_320F`；2.4G `PID_5088`，有线 `PID_5055`。
- 通信端点：`MI_01&Col04`，UsagePage `0xFF1C`，Usage `0x0092`，输入/输出报告长度均为 64。
- 协议（逆向自 ATK V HUB）：
  - 发送 64 字节 Output Report：`[0]=0x04`（Report ID），`[1..2]=RequestCode`（小端），`[3]=0x1A`（`xr.power_info`），其余为 0。
  - 返回 `[3]==0x1A`：设备在线；`[8]` 为电量百分比 0–100；`[9]` 非 0 表示充电中。
  - 返回 `[3]==0xFF`：接收器在线，但键盘休眠/离线。
- 查询超时：写 250 ms，读最多 2 次 × 350 ms，总耗时 < 1 秒；超时用 `CancelIoEx`/`CancelIo` 取消并回收，再关闭句柄。
- 不要在后台定时查询。**一次 `xr.power_info` 就是一次 RF 请求，会经接收器把键盘叫醒并重置其空闲计时。**

**HID 拓扑（本机实测）**：

| 集合 | UsagePage / Usage | 长度 | 说明 |
|---|---|---|---|
| `MI_00` | `0x0001 / 0x0006` | IN 9 / OUT 2 / FEAT 65 | Boot keyboard，Windows 键盘 |
| `MI_01&Col01` | `0x0001 / 0x0006` | IN 16 | NKRO keyboard |
| `MI_01&Col02` | `0x0001 / 0x0080` | IN 2 | System control |
| `MI_01&Col03` | `0x000C / 0x0001` | IN 3 | Consumer control |
| `MI_01&Col04` | `0xFF1C / 0x0092` | IN 64 / OUT 64 | **厂商电量通道，使用这个** |
| `MI_01&Col05` | `0x0001 / 0x0000` | IN 33 / OUT 33 | 厂商/指针 |
| `MI_01&Col06` | `0x0001 / 0x0002` | IN 8 | Mouse |

- Windows 对键盘集合的用户态读取有限制（受限用户态会话中实测 `CreateFile(GENERIC_READ)` 返回 error 5）。**不要靠读键盘 HID 做按键检测**；用 Raw Input。
- 换其他 VGN/ATK 型号时，VID/PID、UsagePage/Usage、`RequestCode` 语义都可能不同；先用 `monitor.log` 抓原始包再改。

### 2.3 查询策略（省电关键，不得回退）

旧版在后台按 60 秒（充电 15 秒、判定休眠 180 秒）查询，查询本身把键盘叫醒，键盘永远等不到足够长的空闲；软件也永远收不到 `0xFF`，于是继续查，形成“越查越不睡”的自锁。修复后策略如下：

| 触发 | 频率 / 条件 | `force` | 说明 |
|---|---|---|---|
| 启动 | 第一个 worker tick 一次 | 否 | `kb_probe_on_start`，可在设置关闭 |
| 用户“立即刷新” | 每次点击，串行锁 | 是 | 用户主动，允许唤醒睡眠中的键盘 |
| 按键后 | 首次按键后 3 秒，30 分钟冷却 | 否 | 上次结果不是 `online` 时绕过长冷却 |
| 有线 | 每 60 秒 | 是 | USB 供电，不耗键盘电池；避免休眠标记卡死 |
| 端点变化 | 插拔/有线切换时一次 | 是 | 60 秒 access-0 枚举发现 |
| 后台定时 | **无** | — | 禁止 |

- 休眠判定：收到 `0xFF`，或开启活动检测时 10 分钟无按键 → `keyboard.asleep=True`，本地标记，不再探测。
- 超时/错误：保持上一次状态，不当作 0%，不据此发低电量通知。
- `timeout` 不是 `asleep`：首次启动时键盘在睡眠、接收器又不回 `0xFF`，查询可能超时；此时无缓存就显示“未连接/--”，按键后会自动刷新。
- 有线判定：PID `0x5055`；有线时 `charging` 强制为真。

## 3. 功能与交互规范

### 3.1 托盘图标

- 鼠标和键盘合并在**同一个托盘图标**中上下分栏，默认 64×64。
- 默认样式：**双横向电池条**（独立外框 + 正极凸起）；设置中可切换“双行大号数字”；支持“上鼠下键 / 上键下鼠”。
- 颜色语义：
  - 在线：高对比亮色（深色任务栏下鼠标偏蓝、键盘偏白；浅色任务栏下深色）。
  - 充电：亮绿。
  - 低电量：亮红（低于提醒阈值）。
  - 休眠：暗灰。
- Tooltip：两行，显示“鼠标(上): 80% (在线, 更新 16:20)”等；休眠显示“休眠·缓存”。
- 托盘图标只在状态签名（样式/顺序/电量/充电/在线/名称/主题）变化时重绘；tooltip 文本变化时才更新。

### 3.2 左键浮窗（Flyout）

- 左键托盘图标，在屏幕右下角弹出 336×268 的无边框浮窗；失去焦点自动关闭。
- 两张设备卡片：名称、状态、大号电量、进度条、上次更新时间。
- 底部“立即刷新”和“设置”；刷新会强制查询一次（可能唤醒键盘，用户主动）。
- **打开浮窗本身不查询**；显示缓存值 + 更新时间。

### 3.3 低电量 Toast

- 使用 `Windows.UI.Notifications`（`winrt`）+ XML，`ToastNotificationManager` 的 AppUserModelID 走 PowerShell，保证进入 Win10 操作中心。
- 首次跌破 `low_battery_warn`（默认 20%）提醒一次；跌破 `low_battery_crit`（默认 10%）紧急提醒一次；同一低电量周期内不重复；充电或回升后重置。
- 设置里有“测试弹窗通知”。

### 3.4 设置界面

- 480×680，Tkinter 仿 Win10 风格（卡片 + 深/浅色 + 扁平控件）。
- 项目：托盘样式与顺序、主题（跟随系统/深/浅）、提醒阈值 + 紧急阈值 + 测试、鼠标刷新间隔、键盘查询开关（启用查询 / 启动时查询 / 按键后自动刷新）、开机自启。
- 保存时写 `config.json`，并移除已废弃的 `kb_poll_interval_sec`。
- 开机自启写 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，优先 `pythonw.exe`。

### 3.5 单实例

- 命名互斥量 `Win10BatteryMonitor_SingleInstance_Mutex`。
- `CreateMutex` 返回 `ERROR_ALREADY_EXISTS (183)` 或权限拒绝 `5` 时：写日志 `single-instance: another instance is running, exit`，弹 MessageBox 提示“先退出旧实例”，然后退出。
- **旧版只要还在托盘里，就仍会按旧逻辑 60 秒查询键盘**；升级后必须先退出旧实例，再启动新版。

## 4. 键盘省电状态机（本次重构核心）

### 4.1 状态字段

- `_kb_asleep`：软件判定的休眠（与 `keyboard.asleep` 同步）。
- `_kb_last_query`：上次 RF 查询开始时间。
- `_kb_last_result`：上次查询结果（`online/asleep/timeout/error/no_endpoint`）。
- `_kb_last_activity`：最近一次 Raw Input 按键。
- `_kb_last_awake`：最近一次“确认醒着”（按键或 online 查询）。
- `_kb_activity_pending`：上次查询后的首次按键时间。
- `_kb_wired`：当前是否走有线端点。
- `_kb_activity_enabled`：Raw Input 是否可用。

### 4.2 每 4 秒 tick 的伪代码

```python
# worker 每 poll_interval_sec 调一次；全程不主动发 RF
def tick_keyboard():
    if not keyboard_enabled: return
    if endpoint missing: refresh_endpoint(probe_on_change=False)
    elif 60s elapsed: refresh_endpoint(probe_on_change=True)  # access 0，不发 report

    if startup probe not done and kb_probe_on_start:
        probe_keyboard("startup")           # 唯一一次启动查询

    if wired:
        if 60s since last query: probe_keyboard("wired", force=True)
        return

    if activity detection enabled:
        if 10min since last awake and not asleep:
            mark asleep; no RF
        if pending and 3s since first key:
            if last_result != "online" or 30min since last query:
                probe_keyboard("activity")
```

### 4.3 状态与 UI

| 状态 | 条件 | 浮窗 / Tooltip |
|---|---|---|
| 充电 | `charging` | `⚡ 正在充电 · 更新 HH:MM`，绿色 |
| 在线 | `is_online` | `● 2.4G 已连接 · 更新 HH:MM`，蓝色 |
| 休眠缓存 | `asleep` 且 `battery>=0` | `☾ 休眠 · 缓存 XX% · 更新 HH:MM`，灰色 |
| 休眠无缓存 | `asleep` 且 `battery<0` | `☾ 休眠（无缓存）`，灰色 |
| 未连接 | 无端点且 `battery<0` | `○ 未连接`，灰色 |
| 离线缓存 | 非在线且 `battery>=0` | `休眠·缓存`，灰色 |

- `keyboard_enabled=False`：不启动 Raw Input、不查询、不参与低电量通知；只显示缓存。
- `kb_activity_detect=False`：不做 10 分钟本地休眠判定，只保留上次查询状态；手动刷新仍可用。

### 4.4 Raw Input

- 独立线程创建隐藏窗口（`WS_EX_TOOLWINDOW`，不显示），`RegisterRawInputDevices` 注册 UsagePage `0x01` / Usage `0x06`，`RIDEV_INPUTSINK`。
- `WM_INPUT` 中取 `RAWKEYBOARD`；`VKey` 为 0 或 0xFF 忽略；设备名必须包含 `VID_320F` 且包含 `PID_5088` 或 `PID_5055`，否则忽略。
- 只调用 `note_kb_activity()` 记录时间戳；**不记录键值、不注入、不拦截**。
- 首次收到 `WM_INPUT` 记 `rawinput: first WM_INPUT received`；首次 VGN 按键记 `rawinput: VGN key activity detected`；从休眠唤醒记 `kb activity: wake`。
- 如果注册失败或设备名匹配不上：日志写 `keyboard activity detection unavailable` 或没有 VGN 活动；降级为手动刷新，不影响省电修复。

## 5. 配置、日志、单实例

### 5.1 `config.json` 键

| 键 | 默认 | 设置 UI | 含义 |
|---|---|---|---|
| `poll_interval_sec` | 4 | 鼠标刷新 | worker 周期，也是鼠标日志轮询；**不是键盘查询间隔** |
| `low_battery_warn` | 20 | 提醒阈值 | 低电量提醒 |
| `low_battery_crit` | 10 | 紧急阈值 | 紧急提醒 |
| `theme_mode` | `system` | 主题 | `system/dark/light` |
| `start_with_windows` | false | 自启 | HKCU Run |
| `style` | `bars` | 样式 | `bars/digits` |
| `order` | `mouse_first` | 顺序 | `mouse_first/keyboard_first` |
| `keyboard_enabled` | true | 启用键盘查询 | 主开关；关闭后只显示缓存 |
| `kb_probe_on_start` | true | 启动时查询一次 | 每次启动一次 |
| `kb_activity_detect` | true | 按键后自动刷新 | Raw Input + 10 分钟休眠判定 |
| `kb_query_cooldown_sec` | 1800 | 未暴露 | 按键触发的查询冷却 |
| `kb_sleep_hint_sec` | 600 | 未暴露 | 无按键多久本地标记休眠 |

- 旧键 `kb_poll_interval_sec` 已废弃：读取时忽略，设置保存时删除。

### 5.2 `monitor.log`

- 位置：源码目录（`os.path.dirname(__file__)`），UTF-8；超过 256 KB 轮转为 `monitor.log.1`。
- 典型行：

```text
2026-11-21 20:15:01 app start
2026-11-21 20:15:01 kb-probe reason=startup pid=5088 state=online level=41 chg=0 elapsed=15ms raw=0401001a0000000029000000
2026-11-21 20:27:26 kb state -> sleep (no key activity)
2026-11-21 21:07:50 kb activity: wake
2026-11-21 21:07:51 kb-probe reason=activity pid=5088 state=online level=41 chg=0 elapsed=10ms raw=0403001a0000000029000000
2026-11-21 21:10:58 kb-probe reason=manual pid=5088 state=online level=48 chg=1 elapsed=18ms raw=0404001a0000000030010000
```

- 记录：启动/退出、端点变化、每次 probe（reason/pid/state/level/chg/elapsed/raw 前 12 字节）、Raw Input 首次/唤醒、休眠判定、单实例、worker 错误。
- 日志不记录按键内容、不记录用户输入明文。

### 5.3 单实例与升级

- 锁名 `Win10BatteryMonitor_SingleInstance_Mutex`；错误 183/5 → 日志 + MessageBox + 退出。
- 升级步骤：托盘右键旧实例 → 退出（或任务管理器结束旧的 `pythonw.exe` / 重启）→ 拔插 VGN 接收器 → 启动新版。
- 旧实例不退，新版不会启动；旧实例还会继续 60 秒查询键盘。

## 6. 性能与安全

- 鼠标日志每 4 秒一次；键盘正常情况下每小时 0–2 次（按键冷却 30 分钟、手动/启动另算）。
- Raw Input 线程阻塞在消息循环，空闲 CPU ≈ 0；托盘只在状态变化时重绘。
- 常驻内存目标 15–40 MB；Tk 按需创建浮窗/设置。
- 不做全局键盘钩子、不注入、不拦截；Raw Input 只读，且只保留时间戳。
- 少数反作弊软件可能对后台 Raw Input 敏感；如遇到，关闭“按键后自动刷新”，手动刷新仍可用；省电修复不依赖它。
- 日志不含按键内容/密码；256 KB 上限，避免无限增长。

## 7. 验证与回归清单

**语法检查（不写 pyc）**：

```powershell
python -B -c "compile(open('battery_monitor.py', encoding='utf-8').read(), 'battery_monitor.py', 'exec')"
```

**启动与日志断言**：

1. `app start`、`rawinput watcher started`、`keyboard activity detection enabled`、`kb endpoint changed`、`kb-probe reason=startup`。
2. 按一下键盘：`rawinput: first WM_INPUT received` + `rawinput: VGN key activity detected`；约 3 秒后 `kb-probe reason=activity`（若 30 分钟冷却未到，可能没有 probe，这是正常的）。
3. 静置 10 分钟：`kb state -> sleep (no key activity)`；之后不应出现 `kb-probe`。
4. 点“立即刷新”：只出现一条 `reason=manual`。
5. 插线：`kb endpoint changed ... wired=1`，之后每 60 秒 `reason=wired`。
6. 空闲 8 小时：日志中不应有周期性 probe。
7. 24 小时电量下降：开软件 vs 不开软件应接近。
8. 回归：托盘、浮窗、设置、Toast、开机自启、单实例。

**2026-11-21 实测记录**（见 [monitor.log](monitor.log)）：

- 16:55:27 首次拿到 `WM_INPUT` 并识别 VGN 按键；16:55:30 `reason=activity` 在线 46%。
- 17:34:51、18:04:52 两次 activity 相隔 30 分钟，符合冷却；17:48、18:14 在 10 分钟无按键后标记休眠。
- 20:15:01 重启后 startup 在线 41%；20:27 休眠；21:07 按键唤醒；21:10:58 手动在线 48%、充电中。
- 全程没有 60 秒/180 秒后台查询，验证修复生效。

## 8. 已知限制与待办

- **首次启动/休眠中查询可能超时**：接收器不回 `0xFF` 时，约 350 ms 超时；无缓存时 UI 显示“未连接/--”，按键后自动恢复。不要把 timeout 当 0%。
- **Raw Input 边界**：前台是提权窗口、UIPI、反作弊可能影响后台 Raw Input；不可用时关闭自动刷新，手动刷新仍有效。
- **固件/接收器状态**：如果连软件都不开、键盘仍不休眠，先拔插接收器/重启键盘；仍复现则用 USBPcap 对比 ATK V HUB 关闭时是否发送 release/sleep 命令。
- **键盘自身设置**：ATK V HUB 里“休眠时间”若设为“从不”，与本软件无关，需要用户改回。
- **型号差异**：VGN/ATK 其他型号的 PID、UsagePage/Usage、协议可能不同；不要假设 `0x1A` 通用。
- **UI 现代化未完成**：当前 Tkinter 风格可用但未达到 Twinkle Tray 的 Fluent 效果；备选方案见下。
- **无自动化测试**：改动必须做语法检查 + 真实启动 + 日志断言；托盘/浮窗需在正常交互桌面验证。
- **DSH 沙箱限制**：`pystray` 的 `ChangeWindowMessageFilterEx` 在受限会话会被拒绝（WinError 5），托盘测试请用正常桌面启动，不要据此改业务代码。
- **单实例**：升级时必须先退出旧实例，否则新版直接退出。
- **日志会轮转**：`monitor.log` 只保留最近 256 KB；长期证据请另行留存。

**后续 UI 升级备选**：

1. WebView2 按需加载 + 轻量后端：常驻不加载界面引擎，打开浮窗/设置时渲染 Fluent UI + 亚克力。
2. Python + `PySide6` + `PyQt-Fluent-Widgets`：原生 Fluent 控件重构。
3. 纯原生 C# WPF（`.NET 8` + `WPF-UI`）：独立绿色 `.exe`，零 Python 依赖。

## 9. 交接规则（给下一个 Agent）

1. **先读代码再改**：本文件是约束和背景，不是逐行实现；以 `battery_monitor.py` 为准。
2. **不要重新引入后台键盘轮询**，也不要把查询塞进 `refresh_tray()` 或 UI 定时器。
3. 键盘查询只走 `probe_keyboard()`；新增触发点必须说明理由，并更新本文件第 2.3/4 节。
4. 休眠/离线状态继续使用 `last_valid_battery` 缓存；禁止把未知、超时、0xFF 当 0%。
5. 保持单文件、零额外依赖（除非用户同意）；UI 文案保持中文/UTF-8。
6. 改动协议或超时参数时，先抓 `monitor.log` 原始包，再改，并在本文件记录。
7. 行为变化后同步更新本文件与 [README.md](README.md)；不要写死行号，用函数/类名。
8. 托盘、浮窗、设置、Toast、自启必须在真实 Windows 桌面回归；不要只在沙箱里测。
9. 保留用户的默认偏好：条状托盘、上鼠下键、Win10 原生风格、低资源占用。
