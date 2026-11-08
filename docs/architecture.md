# 模块化机器人语音系统

三个独立进程、三个独立 conda 环境，通过 HTTP / WebSocket 组成一条语音问答闭环。

```mermaid
flowchart LR
    Mic[麦克风] --> ASRSvc["ASR Service :8801<br/>env asrt<br/>采集+唤醒+播放"]
    ASRSvc -->|"识别文本"| Agent["Agent Service :8800<br/>env botagent"]
    Agent --> Sem["语义识别 / 意图分流"]
    Sem -->|"route=chat"| Chat[ChatHandler]
    Sem -->|"route=tool"| Tool[ToolHandler]
    Sem -->|"route=device"| Dev[DeviceHandler]
    Chat --> LLM[OpenAI 兼容 LLM]
    Tool --> Exec[动作执行]
    Dev --> Exec
    Chat -->|"speak=true"| ASRSvc
    Tool -->|"speak=false"| ASRSvc
    Dev -->|"speak=false"| ASRSvc
    ASRSvc -->|"仅 speak=true 才连"| TTSSvc["TTS Service :8000<br/>env chattts"]
    TTSSvc -->|"PCM16 24kHz 流"| ASRSvc
    ASRSvc --> Spk[扬声器 直放, 不落盘]
```

## 核心语义

1. **ASR 永远是第一环**：`麦克风 → 唤醒词 → 识别文本`，任何输入都从 ASR 起步。
2. **Agent 做语义识别并分流**：拿到文本后判断意图，决定走哪个分支。
3. **分支决定下游去向**：
   - `chat` 分支 → `speak=true` → 进 TTS 合成 → 扬声器播放
   - `tool` 分支 → 分两种：
     - **执行型**（开灯、播放音乐）→ `speak=false` → 直接执行动作，不进 TTS
     - **输出型**（查天气、查时间）→ `speak=true` → 结果经 TTS 播报
   - `device` 分支 → `speak=false` → 直接执行动作，**不进 TTS**
4. **不保存本地**：识别音频只在内存；TTS 的 PCM 边收边播，不写文件。

## 目录结构

```
Bot Agent/
├── services/
│   ├── asr_service/          # 源自 project/ASRT
│   │   ├── wakeup_asr.py     # 原样保留采集/唤醒/VAD
│   │   ├── server.py         # 新增: 采集线程 + /health + /state
│   │   ├── pipeline.py       # 新增: ASR->Agent->(TTS->播放) 状态机
│   │   ├── agent_client.py   # 新增: 调用 Agent 分流接口
│   │   ├── tts_client.py     # 新增: ChatTTS WS 客户端 + 直放
│   │   └── config.py         # 新增: 读 configs/pipeline.yaml
│   ├── tts_service/          # 源自 project/ChatTTS (含 853MB 权重)
│   │   └── examples/ws_server/   # 原样复用, 协议不动
│   └── agent_service/        # 新增
│       ├── app.py            # FastAPI 路由
│       ├── config.py         # 读 configs/agent.yaml + configs/.env
│       ├── llm.py            # OpenAI 兼容客户端
│       ├── session.py        # 会话历史
│       ├── router.py         # 意图分流
│       ├── registry.py       # 分支注册表
│       └── handlers/         # chat / tool / device
├── configs/
│   ├── agent.yaml
│   ├── pipeline.yaml
│   └── .env                  # 密钥, 不入库
├── scripts/
│   ├── start_all.sh / stop_all.sh
│   ├── start_tts.sh / start_agent.sh / start_asr.sh
│   ├── smoke_test.sh          # 三段式联通自检
│   └── full_chain_test.sh     # 端到端: 回放 -> 分流 -> TTS -> 播放 -> 断言
└── docs/architecture.md
```

## Conda 环境

| 服务 | 环境 | Python | 关键依赖 |
| --- | --- | --- | --- |
| ASR | `asrt` | 3.10 | vosk 0.3.45、sounddevice、requests、websockets、pyyaml |
| TTS | `chattts` | 3.11 | torch 2.6、fastapi、uvicorn、websockets |
| Agent | `botagent` | 3.11 | fastapi、uvicorn、httpx、pyyaml、pydantic |

vosk 中文模型在 `~/.cache/vosk/`（`vosk-model-cn-0.22`、`vosk-model-small-cn-0.22`），无需下载。

## 关键配置

### `configs/pipeline.yaml`（ASR 侧）

```yaml
asr:
  # 唤醒词。可自定义, 支持两种写法:
  #   1) 逗号分隔字符串: "你好助手,小助手"
  #   2) YAML 列表:      ["你好助手", "小助手"]
  wake_words: "你好助手"

  # 没识别到内容时的播报文案 (会经 TTS 播报出来); 留空表示不播报。
  # 同一句话也用于 /state 的 errors 记录, 保证日志与播报一致。
  empty_result_prompt: "没听清内容，请再说一遍"

  # 连续对话窗口 (秒)。一轮播报完成后, 这段时间内再次说话无需唤醒词。
  # 从「播报结束」开始计时 (机器人说话的时间不占用用户的窗口)。
  # 设为 0 关闭连续对话。
  continuous_timeout: 30.0
```

可用环境变量覆盖：`ASR_WAKE_WORDS` / `ASR_EMPTY_RESULT_PROMPT` / `ASR_CONTINUOUS_TIMEOUT`。
命令行 `--wake` / `--continuous-timeout` 优先级最高。

> 唤醒词必须能被 vosk 模型词表切分，否则启动时会提示并忽略该词。
> 启动日志会打印实际切分结果，例如 `唤醒词: 你好助手 (词表切分: 你好 + 助手)`。

#### 连续对话的状态流转

```
IDLE ──唤醒词──> RECORDING ──识别完成──> THINK ──speak──> SAY ──播完──> IDLE
                  ↑                                                 │
                  └────────────── 窗口内(30s) 检测到人声 ←───────────┘
                                    窗口过期 → 必须重新唤醒
```

连续对话窗口内用 VAD 判断「用户是否开口」，检测到人声才开始录音 ——
否则环境噪声会被当成用户开口，白等一个静音超时。

前滚音频（补上开口前 0.5 秒，避免吞掉第一句话）会**排除 TTS 播放期间
录到的声音**，否则下一轮会把机器人的声音当成用户的话识别出来。

### `configs/agent.yaml`（Agent 侧）

```yaml
llm:
  model: agnes-2.5-flash   # agnes-3.0-flash 首字延迟波动大 (1.5s~33s), 语音场景别用
  max_tokens: 256          # 限制生成长度 -> 直接降低 TTFT 和 TTS 合成时长
router:
  mode: hybrid            # rules | llm | hybrid
  default_route: chat
  llm_timeout: 1.2        # 路由分类超时, 超过就回落 default_route
  trust_rules: true       # 规则命中就信, 跳过 LLM 二次确认 (省 1~3 秒)
  rules:
    - intent: tool
      keywords: ["打开", "关闭", "设置"]
      exclude: ["怎么样", "如何", "为什么"]
```

> `configs/.env` 里的 `LLM_MODEL` **会覆盖** YAML 的 `llm.model`。
> 改完 YAML 记得同步改 `.env`，否则「配置改了没效果」。

## 启动

```bash
# 三个服务一起起（会等健康检查通过）
./scripts/start_all.sh

# 或分别启动
./scripts/start_tts.sh      # env chattts, :8000
./scripts/start_agent.sh    # env botagent, :8800
./scripts/start_asr.sh      # env asrt, :8801

# 停止
./scripts/stop_all.sh
```

### 端到端自检

```bash
./scripts/full_chain_test.sh            # 自动拉起三个服务, 回放真实录音, 断言分流与播放
./scripts/smoke_test.sh                 # 只做三段式联通检查
```

`full_chain_test.sh` 会验证：识别文本 → `chat` 分支 `speak=true` → TTS 直放；
`tool` 分支 `speak=false` 不进 TTS；以及全程没有任何音频文件落盘。

ASR 服务也支持透传参数：

```bash
./scripts/start_asr.sh --wake "你好助手"
./scripts/start_asr.sh --replay /path/to/16k_mono.wav   # 不占麦克风
./scripts/start_asr.sh --replay x.wav --no-wake         # 跳过唤醒词, 只验下游链路
./scripts/start_asr.sh --replay x.wav --keep-alive 60   # 回放后保留 HTTP 60s 便于读 /state
```

## 接口契约

### Agent Service `:8800`

`POST /v1/dispatch` — 分流并执行（ASR 主用）

```json
// 请求
{"session_id": "robot-01", "text": "打开空调", "use_history": true}

// 响应
{
  "session_id": "robot-01",
  "text": "打开空调",
  "intent": "tool",
  "route": ["tool"],
  "speak": false,
  "reply": "已执行 light_on",
  "action": {"name": "light_on", "args": {"raw": "打开空调"}, "status": "ok", "detail": "灯已打开"},
  "latency_ms": 42,
  "trace": [{"step": "rules", "ms": 0.3, "detail": "tool:打开"}]
}
```

| 字段 | 含义 |
| --- | --- |
| `intent` / `route` | 语义识别得到的意图与命中分支 |
| `speak` | `true` = 回复文本需经 TTS 朗读；`false` = 不朗读 |
| `reply` | 面向用户的短文本（`speak=true` 时是朗读内容） |
| `action` | 工具类分支的动作与执行结果 |
| `trace` | 各阶段耗时与判定依据，调试用 |

其它接口：

- `POST /v1/chat` — 只做对话，跳过分流
- `POST /v1/chat/stream` — SSE 流式对话，事件 `delta` / `done` / `error`
- `GET /health` — `{"status":"ok","model":"...","branches":[...],"router_mode":"hybrid"}`
- `GET /v1/branches` — 各分支启用状态

### ASR Service `:8801`

- `GET /health` — `{"status":"ok","state":"IDLE","wake_words":["你好助手"],...}`
- `GET /state` — 最近一轮的 `last_text` / `last_intent` / `last_speak` / `last_reply` / `last_action` / `errors`

状态机：`IDLE → REC → THINK → SAY → IDLE`，其中 `SAY` 只在 `speak=true` 时进入。

> 没识别到内容时也会进入 `SAY`，播报 `empty_result_prompt`（如「没听清内容，请再说一遍」）。

### TTS Service `:8000`

复用 ChatTTS 自带的 `examples/ws_server`，协议未改。`TTS_PLAY=0`，播放统一由 ASR 侧负责，避免回声与串扰。

- `GET /health` — `{"model_loaded":true,"sample_rate":24000,"voices":[...]}`

## 分流机制

三级策略，由 `configs/agent.yaml` 的 `router.mode` 控制：

- `rules` — 只用关键词/正则，零延迟、可离线
- `llm` — 只用一次轻量 LLM 调用做分类
- `hybrid` — 规则优先，未命中再走 LLM 兜底（默认）

规则示例：

```yaml
router:
  mode: hybrid
  default_route: chat
  rules:
    - intent: tool
      keywords: ["打开", "关闭", "设置", "播放", "查询"]
    - intent: device
      keywords: ["走", "停下", "拍照", "抬起", "回到"]
```

## 新增一个分支

不需要改 ASR / TTS，只要两步：

1. 在 `services/agent_service/handlers/` 加一个文件，继承 `Handler`：

```python
from handlers.base import AgentRequest, BranchResult, Handler

class MyHandler(Handler):
    speak = False   # 该分支是否需要朗读

    async def handle(self, req: AgentRequest) -> BranchResult:
        return BranchResult(
            reply="已执行 my_action",
            speak=self.speak,
            action={"name": "my_action", "args": {}, "status": "ok"},
            trace=[self._trace("execute", 1.2, "my_action:ok")],
        )
```

2. 在 `configs/agent.yaml` 注册并加路由规则：

```yaml
handlers:
  my:
    module: handlers.my_handler
    class: MyHandler
    enabled: true
    speak: false

rules:
  - intent: my
    keywords: ["我的关键词"]
```

重启 Agent 服务即可生效。`GET /v1/branches` 可确认分支已装载。

## 排障

**没有麦克风 / 扬声器**

```bash
arecord -l     # 录音设备
aplay -l       # 播放设备
```

当前机器两者都为空。ASR 服务支持 `--replay <16k单声道wav>` 回放模式验证完整链路；TTS 侧在无输出设备时会自动降级为「只合成不播放」并在日志告警，链路其余部分照常工作。

接上 USB 声卡后可用 `--device <索引>` 指定采集设备。

**唤醒词没识别出来**

唤醒词判定属于 ASR 模型层面（vosk 词表/声学匹配），不属于本编排层的职责。
`wakeup_asr.py` 保持与 `project/ASRT` 原样一致，未做任何改动。

排查建议：
- 换一个模型词表里确实存在的唤醒词（如「你好助手」会被切成「你好」+「助手」）
- 用 `tools/check_asr.py <wav>` 先确认这段音频被识别成什么
- 链路自检可用 `--no-wake` 跳过唤醒环节，只验证 Agent 分流与 TTS 播放

**TTS 加载慢**

ChatTTS 首次加载 853MB 权重较慢，`/health` 在 `model_loaded=true` 之前会返回 `loading`。启动脚本已做轮询等待。

**Agent 报 401 / 未配置密钥**

密钥在 `configs/.env`（`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`），可用环境变量覆盖。`/health` 里的 `api_key_configured` 可快速确认。

**工具分支的播报规则**

`tool` 分支内部按工具类型区分是否朗读：

| 类型 | 例子 | `speak` | 说明 |
| --- | --- | --- | --- |
| 执行型 `kind=action` | 开灯、开空调、播放音乐 | `false` | 结果就是「做完了」，静默执行 |
| 输出型 `kind=query` | 查天气、查时间、查日期 | `true` | 结果本身就是内容，必须念给用户听 |

输出型工具的 `reply` 就是**要朗读的内容本身**（如「北京今天天气晴，气温 24 度」），
而不是「已执行 查天气」。

哪些工具算输出型，可以在 `configs/agent.yaml` 里显式指定：

```yaml
handlers:
  tool:
    module: handlers.tool_handler
    class: ToolHandler
    enabled: true
    speak: false
    options:
      speak_tools: ["weather", "time", "date"]
```

不填则按代码内置判定（`kind=query` 的算输出型）。显式列出可以覆盖内置判定，
例如把某个执行型动作也改成要播报。

**打断（barge-in）暂未实现**

TTS 的 WS 协议已支持 `stop` 控制报文，接入成本低。
