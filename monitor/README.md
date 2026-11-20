# BotAgent 监控台 (Qt6)

只读的监控程序，看五个服务的连接状态，并把 **Agent 每轮的思考过程**实时展示出来。

- Qt **6.8.3** / QWidget / C++17
- **qmake** 构建（`.pro` + `.pri` 分模块）
- **桌面端 + Android 平板端**（横屏）—— 同一套代码同一套布局
- 界面风格对齐 `RobotControl`：**果冻风**（圆角、半透明底、蓝色强调色 `#4a90e2`）
- 默认只读：不下发任何控制指令，不会驱动机械臂
- 底部的「手动指令」输入框是**唯一的例外** —— 它会真的向 Agent 提问，
    **同样会触发设备动作**，详见 [手动指令](#手动指令) 一节

## 目录结构

```
monitor/
├── monitor.pro              主工程 (qmake)
├── selftest.pro             自测程序 (独立 qmake 工程)
├── build.sh                 一键构建 (桌面端)
├── build_android.sh         一键打包 Android APK
├── config.json              服务地址与 WS 配置
├── main.cpp                 入口 (加载 QSS / 应用图标 / 调试截图)
├── mainwindow.h/.cpp        主窗口逻辑
├── mainwindow.ui            界面布局 (Qt Designer)
├── core/
│   ├── qssmanager.h/.cpp    QSS 样式管理器 (单例)
│   ├── appconfig.h/.cpp     配置加载
│   ├── servicepoller.h/.cpp HTTP 轮询
│   ├── agentclient.h/.cpp   手动指令客户端 (POST /v1/dispatch)
│   └── wsclient.h/.cpp      WebSocket 订阅 (指数退避重连)
├── models/
│   ├── monitormodel.*       服务状态表模型 (状态数据的唯一来源)
│   ├── thinkingmodel.*      思考过程树模型 (带 tooltip 全文)
│   └── statusdelegate.*     状态圆点绘制
├── ui/
│   ├── flowcard.h/.cpp      单轮对话的流程卡片
│   ├── flowpanel.h/.cpp     对话流程面板 (卡片容器 + 状态条 + 系统事件)
│   ├── servicebar.h/.cpp    底部服务状态条 (胶囊式状态指示)
│   └── ui.pri
├── resources/
│   ├── resources.qrc        资源清单
│   ├── icons/appicon.png    应用图标 (桌面窗口图标 + Android 启动图标)
│   └── styles/
│       ├── theme_light.qss  浅色主题 (唯一定义配色的地方)
│       ├── global.qss       全局字体
│       └── mainwindow.qss   主窗口控件 (果冻风)
├── android/                 Android 工程 (Qt 的 androiddeployqt 约定布局)
│   ├── AndroidManifest.xml  包名 / 权限 / 横屏 / 主题
│   ├── build.gradle
│   ├── res/
│   │   ├── mipmap/appicon.png   启动器图标
│   │   ├── values/styles.xml    NoActionBar 主题 (去掉系统标题栏)
│   │   └── values/libs.xml
│   └── gradle/
└── build/                   构建产物
```

### 样式约定

- `theme_light.qss` 是**唯一**定义配色的文件，覆盖所有控件选择器。
- `global.qss` 只管字体，不设颜色。
- `mainwindow.qss` 全用 `#objectName` 选择器，只影响具名控件。
- 加载顺序：`theme → global → mainwindow`，后者不覆盖前者的颜色。

## 构建

### 桌面端

```bash
cd monitor
./build.sh              # 产物: monitor/build/botagent_monitor
./build.sh --clean      # 先清理再构建
```

Qt 路径默认写死 `/home/gxh/Qt/6.8.3/gcc_64`，换版本用环境变量覆盖：

```bash
QT_PREFIX=/path/to/Qt/6.8.3/gcc_64 ./build.sh
```

### Android 平板端

```bash
cd monitor
./build_android.sh              # 产物: monitor/build-android/build/outputs/apk/debug/
./build_android.sh --clean      # 先清理再构建
./build_android.sh --install    # 打完后直接装到已连接的设备
./build_android.sh --release    # release 版 (需自己配签名)
```

脚本内部做四件事：

| 步骤 | 干什么 |
| --- | --- |
| 1. qmake | 用 `android_arm64_v8a` 那套 mkspec 生成 Android 的 Makefile |
| 2. make | 交叉编译出 `libbotagent_monitor_arm64-v8a.so` |
| 3. androiddeployqt | 把 Qt 库 + OpenSSL + 图标塞进 APK 工程，生成 Gradle 工程 |
| 4. gradlew | `assembleDebug` / `assembleRelease` 出 APK |

依赖的工具链（都能用环境变量覆盖）：

```bash
QT_PREFIX=/home/gxh/Qt/6.8.3
ANDROID_SDK_ROOT=/home/gxh/Android/Sdk
ANDROID_NDK_ROOT=/home/gxh/Android/Sdk/ndk/26.1.10909125
JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
```

> **OpenSSL 是必需的** —— `monitor.pro` 里的 `openssl.pri` 会把预编译的
> `libssl` / `libcrypto` 加进 `ANDROID_EXTRA_LIBS`。少了它 WebSocket 的
> TLS 后端就编不出来。

#### 平板适配要点

| 项 | 说明 |
| --- | --- |
| **横屏锁定** | `AndroidManifest.xml` 里 `android:screenOrientation="sensorLandscape"` —— 平板横屏显示，随重力在两个横屏方向间转 |
| **去掉系统标题栏** | `res/values/styles.xml` 里的 `AppTheme` 继承 `Theme.DeviceDefault.NoActionBar`。不设的话系统会在顶部画一条白底标题栏，Qt 界面从它下面开始，浪费一屏高度 |
| **启动闪白** | `AppTheme` 的 `android:windowBackground` 设成 `#F2F4F8`（跟应用背景一致），避免启动瞬间到 Qt 首帧之间的白闪 |
| **触摸目标** | 窗口宽度 ≥ 640 时（平板横屏一定满足），按钮/输入框自动放大到 44dp，手指才点得准。桌面端保持鼠标友好尺寸 |
| **明文 HTTP** | `android:usesCleartextTraffic="true"` —— 监控的是局域网里的 `http://` 服务，Android 9+ 默认禁明文，不开就连不上 |
| **权限** | `INTERNET` + `ACCESS_NETWORK_STATE` |

> 窗口最小尺寸是 **640x380**（逻辑像素）—— 10 寸平板横屏在 density=2 下
> 是 640x400，刚好放得下；竖屏（宽度 < 640）系统会自动处理，我们只保证横屏可用。

#### 应用图标

图标只有**一份源文件**，两端共用：

```
resources/icons/appicon.png   ← 源文件 (192x192, 透明底)
        │
        ├── 桌面端: 打进 QRC, main.cpp 里 setWindowIcon() 读
        │           :/resources/icons/appicon.png
        │
        └── Android: android/res/mipmap/appicon.png
                    manifest 里 android:icon="@mipmap/appicon"
```

改图标只需换掉 `resources/icons/appicon.png` 和
`android/res/mipmap/appicon.png` 两个文件即可，**不需要**生成各密度尺寸
（Android 会自己缩放）。

> Android 端还有个 `android:roundIcon` —— 现在也指向同一张图，
> 想要真正的圆形图标再单独放一张 `mipmap/ic_launcher_round.png`。

### 验证布局

`main.cpp` 支持 `--size`，用来检查不同屏幕下的布局：

```bash
# 平板横屏 (1280x800)
monitor/build/botagent_monitor --size 1280 800 --screenshot /tmp/tablet.png

# 桌面宽屏
monitor/build/botagent_monitor --size 1500 880 --screenshot /tmp/wide.png
```

## 运行

```bash
# 先把被监控的服务拉起来
./scripts/start_all.sh

# 再启动监控台
monitor/build/botagent_monitor
```

开窗后**不会**自动开始监控 —— 停在「未监控」状态，点顶栏的「开始监控」按钮
（或调 `MainWindow::startMonitoring()`）才开始轮询与 WebSocket 订阅。
这样不会一开窗就往生产服务上打请求。

调试用：

```bash
# 跑 12 秒后把窗口截图存成 PNG 再退出
monitor/build/botagent_monitor --screenshot /tmp/monitor.png 12000

# 同上，但跳过「点开始监控」这一步，直接进入监控态
monitor/build/botagent_monitor --start --screenshot /tmp/monitor.png 12000

# 验证模型层逻辑（不需要图形界面）
monitor/build/selftest/botagent_monitor_selftest
```

## 界面

```
┌───────────────────────────────────────────────────────────────────────┐
│ BotAgent 监控台  只读监控 · 不下发任何控制指令   监控中·正常5·降级0    │
│                              [刷新间隔: 2000] [暂停监控] [清空流程]…  │
├───────────────────────────────┬───────────────────────────────────────┤
│ 对话流程 (ASR → Agent → 回复)  │ Agent 思考过程                        │
│ ┌───────────────────────────┐ │ ▸ 第 2 轮 「帮我看看…」  ← 最新在上    │
│ │16:20:31 [chat] 完成        │ │   ├ 参数提取  weather                │
│ │ASR 识别  帮我看看…          │ │   │   ├ 推理  用户查询中明确…         │
│ │Agent 分流 chat·rule       │ │   │   ├ 置信度  1.0                   │
│ │Agent 回复 我看不到…        │ │   │   └ 参数  {"city":"南京"}          │
│ │流程耗时  录音1150ms…       │ │   ├ 工具调用  query_weather           │
│ └───────────────────────────┘ │   ├ 分流  tool (rule)                 │
│ ┌───────────────────────────┐ │   └ 结果  南京今天晴，气温 25 度        │
│ │16:20:05 [tool] 完成        │ │ ▸ 第 1 轮 「查南京天气」  (已折叠)     │
│ │ASR 识别  查南京天气        │ │   └ ...                              │
├───────────────────────────────┴───────────────────────────────────────┤
│ 服务  ●Agent 正常8ms ●ASR 正常16ms ●TTS 正常8ms ●Arm 降级6ms ●Vision… │
│                                          WS: Agent✓ Arm✗ Vision✗ 轮询 12 次 │
├───────────────────────────────────────────────────────────────────────┤
│   +  ╭ 问点什么...  (Enter 发送, Shift+Enter 换行) ╮              [↑] │
└───────────────────────────────────────────────────────────────────────┘
```

- **左**：**对话流程**。每轮对话一张卡片，按「ASR 识别 → Agent 分流 → Agent 回复
  → 流程耗时」四段展示，文字自动换行完整显示（不做表格列截断）。
  顶部是流水线状态条，实时显示 ASR 状态机（IDLE / REC / THINK / SAY）；
  底部收纳 Arm / Vision 的系统事件。
- **右**：Agent 思考过程。按「轮次」分组，**倒叙排列 —— 最新的一轮在最上面**，
  往下翻是更早的对话（轮次编号仍按时间递增，所以「第 3 轮」排在「第 1 轮」上面）。
  **新的一轮进来时，旧的轮次会自动折叠** —— 只留最新一轮铺开，
  想看旧细节手动点开即可（顶栏「展开/折叠」按钮可以全部展开）。
  展开可看到分流依据、工具选择、参数提取的推理全文与置信度、各阶段耗时。
  **内容在树里是单行省略的，鼠标悬停任意单元格会弹出完整原文**
  （推理全文、LLM 原始输出、参数 JSON、分流 trace 详情）。
- **底**：**服务状态条**。五个服务各一个胶囊：彩色圆点 + 名称 + 状态 + 延迟。
  绿=正常、黄=降级、红=断开、灰=未知/未监控。悬停看完整信息，
  **点击胶囊**看该服务的原始 JSON 详情。
  右侧还有三个 WebSocket 的连接状态与轮询次数。

### 手动开启监控

监控台默认**不监控**，需要显式开启：

| 入口 | 说明 |
| --- | --- |
| 顶栏「开始监控」按钮 | 切换开始/暂停，暂停时底部胶囊全部置灰 |
| `MainWindow::startMonitoring()` | 代码里直接调用（public slot） |
| `MainWindow::stopMonitoring()` | 同上 |
| `MainWindow::toggleMonitoring()` | 同上 |
| `MainWindow::isMonitoring()` | 查询当前状态 |
| `monitoringChanged(bool)` | 状态变化的信号 |
| `config.json` 的 `autoStart` | 设为 `true` 可恢复「开窗即监控」 |

开启后才会发起 HTTP 轮询与 WebSocket 订阅；关闭时两者都停掉。

### 手动指令

底部状态栏上方有一条**指令输入框**（Cursor / Codex 那种），可以直接打字向 Agent 提问，
像语音链路一样看到完整的「分流 → 工具调用 → 参数提取 → 回复」。

| 项 | 说明 |
| --- | --- |
| 位置 | 底部状态栏**上方**，跨整个宽度 |
| 快捷键 | **Enter 发送** / **Shift+Enter 换行**（也可以点「发送」按钮） |
| 走的接口 | Agent 的 `POST /v1/dispatch` |
| session_id | 固定 `monitor-ui`（可在 `config.json` 里改 `agentSessionId`） |

样式是对齐 Cursor 的输入框：

```
  +   ╭──────────────────────────────────────────╮   [↑]
      │ 问点什么...  (Enter 发送, Shift+Enter 换行) │
      ╰──────────────────────────────────────────╯
```

- 中间的输入区是一个**胶囊体**（20px 大圆角 + 白底 + 细边框 + 淡投影），
  **左右边缘都是圆弧**，不是方角
- 左边是 `+` 按钮，右侧是**圆形发送箭头** `↑`
- 发送按钮在**有内容时**才点亮成深色

**卡片上会多一个「手动」徽标**，正文第一行的标题从「ASR 识别」变成「输入文本」，
一眼能分出哪些是打字问的、哪些是语音说的。

#### ⚠️ 会触发设备动作

`/v1/dispatch` 与 ASR 语音走的是**完全同一条路径** —— 分流、工具调用、
设备动作全都一样。机械臂、空调、灯这类动作会被**真的执行**。
（内部有安全门阀预检，但动作本身会动。）

想在生产环境试，请确认当前房间里没有人在机械臂工作范围内。

#### 必须先点「开始监控」

**没点「开始监控」就没有 WebSocket 订阅**，思考树不会更新 —— 你只能看到
HTTP 返回的最终回复，看不到中间的推理过程。所以用输入框前先开监控。

#### 数据流

```
输入框 ──Enter──▶ AgentClient (POST /v1/dispatch)
                        │
                        ├─▶ HTTP 响应 ──▶ 回填卡片 (回复 / 耗时)
                        │
Agent ──WS──▶ WsClient ──▶ ThinkingModel (思考树, 倒叙)
                          └──▶ FlowPanel  (流程卡片)
```

WS 的 `turn_end` 是**主**（它带完整 trace），HTTP 响应只做**兜底** ——
只有当卡片阶段还停在「发送中」时才会用 HTTP 结果回填。

#### 手动轮次与语音轮次不打架

- `session_id` 用独立的 `monitor-ui`，**不会混进 `robot-01` 的语音历史**。
- 手动指令在飞行期间（`m_manualPending`）**跳过 ASR 快照建卡** ——
  否则 ASR 的「最近一轮」会跟手动指令抢同一张卡片。
- 手动轮次结束后两边靠文本比对自然分开。

#### 并发注意

`dispatch()` 会 abort 掉上一个还没回来的请求 —— 防止快速连点堆积。
代价是：**上一条还没回来就发下一条，上一条的 HTTP 响应会丢**。
WS 事件不受影响，思考过程照样收全。



| 卡片字段 | 来源 | 时机 |
| --- | --- | --- |
| ASR 识别 | ASR `GET /health` 的 `last_text` | 识别完成即刻（不等 Agent） |
| Agent 分流 | Agent WS `turn_end` 的 `intent` / `by` | 本轮结束 |
| Agent 回复 | Agent WS `turn_end` 的 `reply` | 本轮结束 |
| 流程耗时 | ASR `timings` + Agent WS 的 `latency_ms`/`route_ms`/`handler_ms` | 本轮结束 |

> ASR 服务**没有** WebSocket 推送，识别文本只能靠 HTTP 轮询拿到。
> 为此在 `services/asr_service/pipeline.py` 里把 `last.text` 的写入提前到
> 「等 Agent 之前」，否则监控端要等整轮跑完才看得到识别结果。
>
> ASR 侧还加了 `POST /v1/submit`（测试入口），麦克风不可用时可以直接
> 灌一段文本走完整条 ASR → Agent → TTS 链路：
>
> ```bash
> curl -X POST http://127.0.0.1:8801/v1/submit \
>      -H 'Content-Type: application/json' \
>      -d '{"text":"查南京天气","record_ms":1200}'
> ```

## 配置

`monitor/config.json`：

```json
{
  "pollIntervalMs": 2000,
  "requestTimeoutMs": 3000,
  "autoStart": false,
  "agentSessionId": "monitor-ui",
  "services": [
    { "name": "Agent",  "host": "127.0.0.1", "port": 8800, "healthPath": "/health", "statusPath": "/v1/branches" },
    { "name": "ASR",    "host": "127.0.0.1", "port": 8801, "healthPath": "/health", "statusPath": null },
    { "name": "TTS",    "host": "127.0.0.1", "port": 8000, "healthPath": "/health", "statusPath": null },
    { "name": "Arm",    "host": "127.0.0.1", "port": 8802, "healthPath": "/health", "statusPath": "/v1/status" },
    { "name": "Vision", "host": "127.0.0.1", "port": 8803, "healthPath": "/health", "statusPath": "/v1/status" }
  ],
  "websockets": [
    { "name": "Agent",  "url": "ws://127.0.0.1:8800/v1/events/" },
    { "name": "Arm",    "url": "ws://127.0.0.1:8802/ws" },
    { "name": "Vision", "url": "ws://127.0.0.1:8803/ws" }
  ]
}
```

找不到 `config.json` 时用内置默认值（与上面一致）。

`autoStart` 默认为 `false`（开窗不监控）；改成 `true` 可恢复「开窗即监控」。

`agentSessionId` 默认为 `monitor-ui` —— 手动指令用的**独立会话**，
跟语音链路的 `robot-01` 分开，互不污染历史。

## 数据来源

| 来源 | 方式 | 内容 |
| --- | --- | --- |
| Agent `:8800` | WS `/v1/events/` | 每轮对话的 trace / 推理 / 耗时 |
| ASR `:8801` | HTTP `/health` | 状态机 (IDLE/REC/THINK/SAY)、最近一轮、耗时 |
| TTS `:8000` | HTTP `/health` | 模型加载状态、音色列表 |
| Arm `:8802` | HTTP `/health` + `/v1/status` + WS | 控制器连接、播放阶段、动作事件 |
| Vision `:8803` | HTTP `/health` + `/v1/status` + WS | 相机连接、检测结果 |

> TTS 的 WebSocket 是二进制 PCM 音频流，**不订阅**，只轮询 `/health`。
>
> ASR 的 `/health` 与 `/state` 返回同一份 snapshot，所以只请求一次即可。

### 降级判定

服务连上了但自报异常时显示黄色：

| 信号 | 判定 |
| --- | --- |
| `status == "loading"` | TTS 模型还在加载 |
| `model_loaded == false` | 模型没就绪 |
| `camera_connected == false` | 相机没连上 |
| `controller_connected == false` | 机械臂控制器没连上 |

## Agent 侧新增的推送接口

`services/agent_service/` 下加了两个文件：

- `event_bus.py` —— 进程内发布订阅中心。有界队列（256），满了丢最旧的一条；
  跨线程 publish 用 `call_soon_threadsafe`。
- `events_api.py` —— `GET /v1/events` WebSocket。握手先发一条 `hello`，
  之后持续推送。**只往外推，不接受任何客户端指令。**

事件信封：

```json
{
  "type": "hello | turn_start | thinking | turn_end | error | heartbeat",
  "ts": "2026-11-18T15:12:31.204881",
  "session_id": "robot-01",
  "turn_id": "3f2a9c1b7e04",
  "payload": { }
}
```

| 字段 | 含义 |
| --- | --- |
| `session_id` | **常驻**的会话标识（比如 `robot-01`），用户说一百句话它也不变 |
| `turn_id` | **每轮对话唯一**，一轮换一个 —— 分轮靠它，不能靠 `session_id` |
| `payload` | 各事件自定义内容 |

> ⚠️ 早期版本只发 `session_id`。由于它是常驻的，客户端若拿它分轮，
> 所有轮次会塌成第一轮。现在客户端优先按 `turn_id` 分组，
> 老协议（无 `turn_id`）则回落到 `turn_start` 事件边界分轮。

| 事件 | 何时发 | payload 要点 |
| --- | --- | --- |
| `turn_start` | 一轮对话开始 | `text` |
| `thinking` | LLM 思考中 | `phase` (`tool_calling` / `tool_calling_retry` / `param_extraction`)、`content`、`tools`、`token` 用量 |
| `turn_end` | 一轮对话结束 | `intent`、`by`、`speak`、`reply`、`action`、`latency_ms`、`trace[]` |
| `error` | tool calling 超时/失败 | `reason`、`message` |

埋点位置：

- `app.py` 的 `Service.dispatch()` —— 发 `turn_start` / `turn_end`
- `router.py` 的 `_decide_by_tools()` —— 发 `thinking`（工具调用）
- `handlers/tool_handler.py` 的 `_extract_args_async()` —— 发 `thinking`（参数提取 + 推理全文）

## 已知边界

- 只做**实时**展示，不回看 `logs/thinking/*.jsonl` 的历史记录。
- Agent 的 `/v1/thinking/*` 那组 HTTP 接口目前还是 TODO 空壳，本工具不依赖它们。
- **ASR 的识别文本靠 HTTP 轮询**，最快也就一个轮询间隔（默认 2s）才出现在
  卡片上；Agent 的分流/回复走 WS，是即时的。
- ASR 侧只保留「最近一轮」的快照，所以卡片历史是从监控端自己累积的
  （最多 50 张卡片）；监控端中途启动的话，之前几轮看不到。
- 退出时若 WS 正好在重连，可能看到一条系统事件 —— 属正常收尾噪音。
- **底部的指令输入框会真的触发设备动作**（`/v1/dispatch` 与语音同权）。
  不用的时候可以不管它，但别在生产环境随手敲 —— 见 [手动指令](#手动指令)。
