# BotAgent 监控台 (Qt6)

只读的桌面监控程序，看五个服务的连接状态，并把 **Agent 每轮的思考过程**实时展示出来。

- Qt **6.8.3** / QWidget / C++17
- **qmake** 构建（`.pro` + `.pri` 分模块）
- 界面风格对齐 `RobotControl`：**果冻风**（圆角、半透明底、蓝色强调色 `#4a90e2`）
- 只读：不下发任何控制指令，不会驱动机械臂

## 目录结构

```
monitor/
├── monitor.pro              主工程 (qmake)
├── selftest.pro             自测程序 (独立 qmake 工程)
├── build.sh                 一键构建
├── config.json              服务地址与 WS 配置
├── main.cpp                 入口 (加载 QSS / 调试截图)
├── mainwindow.h/.cpp        主窗口逻辑
├── mainwindow.ui            界面布局 (Qt Designer)
├── core/
│   ├── qssmanager.h/.cpp    QSS 样式管理器 (单例)
│   ├── appconfig.h/.cpp     配置加载
│   ├── servicepoller.h/.cpp HTTP 轮询
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
│   └── styles/
│       ├── theme_light.qss  浅色主题 (唯一定义配色的地方)
│       ├── global.qss       全局字体
│       └── mainwindow.qss   主窗口控件 (果冻风)
└── build/                   构建产物
```

### 样式约定

- `theme_light.qss` 是**唯一**定义配色的文件，覆盖所有控件选择器。
- `global.qss` 只管字体，不设颜色。
- `mainwindow.qss` 全用 `#objectName` 选择器，只影响具名控件。
- 加载顺序：`theme → global → mainwindow`，后者不覆盖前者的颜色。

## 构建

```bash
cd monitor
./build.sh              # 产物: monitor/build/botagent_monitor
./build.sh --clean      # 先清理再构建
```

Qt 路径默认写死 `/home/gxh/Qt/6.8.3/gcc_64`，换版本用环境变量覆盖：

```bash
QT_PREFIX=/path/to/Qt/6.8.3/gcc_64 ./build.sh
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
│ │16:20:05 [tool] 完成        │ │ ▸ 第 1 轮 「查南京天气」               │
│ │ASR 识别  查南京天气        │ │   └ ...                              │
├───────────────────────────────┴───────────────────────────────────────┤
│ 服务  ●Agent 正常8ms ●ASR 正常16ms ●TTS 正常8ms ●Arm 降级6ms ●Vision… │
│                                          WS: Agent✓ Arm✗ Vision✗ 轮询 12 次 │
└───────────────────────────────────────────────────────────────────────┘
```

- **左**：**对话流程**。每轮对话一张卡片，按「ASR 识别 → Agent 分流 → Agent 回复
  → 流程耗时」四段展示，文字自动换行完整显示（不做表格列截断）。
  顶部是流水线状态条，实时显示 ASR 状态机（IDLE / REC / THINK / SAY）；
  底部收纳 Arm / Vision 的系统事件。
- **右**：Agent 思考过程。按「轮次」分组，**倒叙排列 —— 最新的一轮在最上面**，
  往下翻是更早的对话（轮次编号仍按时间递增，所以「第 3 轮」排在「第 1 轮」上面）。
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

### 对话流程的数据来源

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
