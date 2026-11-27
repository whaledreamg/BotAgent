# 模块化机器人语音系统

四个独立进程、三个独立 conda 环境，通过 HTTP / WebSocket 组成一条语音控制闭环。

```mermaid
flowchart LR
    Mic[麦克风] --> ASRSvc["ASR Service :8801<br/>env asrt<br/>采集+唤醒+播放"]
    ASRSvc -->|"识别文本"| Agent["Agent Service :8800<br/>env botagent"]
    Agent --> Sem["语义识别 / 意图分流"]
    Sem -->|"route=chat"| Chat[ChatHandler]
    Sem -->|"route=tool"| Tool[ToolHandler]
    Sem -->|"route=device"| Dev[DeviceHandler]
    Chat --> LLM[LLM (OpenAI 兼容)<br/>原生 function calling]
    Tool --> Exec[动作执行]
    Dev --> Exec
    Chat -->|"speak=true"| ASRSvc
    Tool -->|"按工具类型"| ASRSvc
    Dev -->|"speak=true"| ASRSvc
    ASRSvc -->|"仅 speak=true 才连"| TTSSvc["TTS Service :8000<br/>env chattts"]
    TTSSvc -->|"PCM16 24kHz 流"| ASRSvc
    ASRSvc --> Spk[扬声器 直放, 不落盘]
    Dev --> Gate["安全门阀<br/>状态检查"]
    Gate -->|"放行"| ArmSvc["Arm Service :8802<br/>env botagent"]
    ArmSvc --> Arm[机械臂]
    Gate -->|"拒绝 + 播报原因"| ASRSvc
```

## 核心语义

1. **ASR 永远是第一环**：`麦克风 → 唤醒词 → 识别文本`，任何输入都从 ASR 起步。
2. **Agent 做语义识别并分流**：拿到文本后判断意图，决定走哪个分支。
3. **分支决定下游去向**：
   - `chat` 分支 → `speak=true` → 进 TTS 合成 → 扬声器播放
   - `tool` 分支 → 分两种：
     - **执行型**（开灯、播放音乐）→ `speak=false` → 直接执行动作，不进 TTS
     - **输出型**（查天气、查时间）→ `speak=true` → 结果经 TTS 播报
   - `device` 分支 → **安全门阀** → 放行则驱动机械臂 → `speak=true` 播报结果
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
│       ├── llm.py            # LLM 客户端 (OpenAI 兼容, 多 provider + 按角色路由)
│       ├── session.py        # 会话历史
│       ├── router.py         # 意图分流 (规则 + tool calling)
│       ├── registry.py       # 分支注册表
│       ├── action_tools.py   # 统一动作层 (tool 定义 + 参数校验 + 执行)
│       └── handlers/         # chat / tool / device
│   └── arm_service/          # 新增: 机械臂动作服务
│       ├── main.py           # FastAPI 入口 (HTTP + WebSocket)
│       ├── service.py        # 播放编排 (入控 -> 对齐 -> 播放 -> 停控)
│       ├── library.py        # 动作库加载 (一个 JSON 文件 = 一个动作)
│       ├── safety.py         # 安全门阀 (状态检查 + 拒绝原因)
│       ├── notifier.py       # 门阀拒绝时的 TTS 播报
│       ├── timeline/         # 时间轴模型 / 插值 / 播放引擎
│       ├── robot/            # 控制面连接 / UDP 关节流 / 控制状态机
│       └── data/             # 动作序列 JSON
├── configs/
│   ├── agent.yaml
│   ├── tools.yaml             # tool 定义 (模型看到的工具清单)
│   ├── actions.yaml          # 机械臂「说法 -> 动作名」映射
│   ├── arm.yaml              # 机械臂服务配置
│   ├── tts.yaml              # TTS 音色 + 模型配置
│   ├── pipeline.yaml
│   └── .env                  # 密钥, 不入库
├── scripts/
│   ├── start_all.sh / stop_all.sh
│   ├── start_tts.sh / start_agent.sh / start_asr.sh / start_arm.sh
│   ├── smoke_test.sh          # 三段式联通自检
│   └── full_chain_test.sh     # 端到端: 回放 -> 分流 -> TTS -> 播放 -> 断言
└── docs/
    ├── architecture.md
    ├── action-integration.md  # 动作接入 (function calling)
    ├── arm-service.md         # 机械臂服务
    └── testing.md
```

## Conda 环境

| 服务 | 环境 | Python | 关键依赖 |
| --- | --- | --- | --- |
| ASR | `asrt` | 3.10 | vosk 0.3.45、sounddevice、requests、websockets、pyyaml |
| TTS | `chattts` | 3.11 | torch 2.6、fastapi、uvicorn、websockets |
| Agent | `botagent` | 3.11 | fastapi、uvicorn、httpx、pyyaml、pydantic |
| Arm | `botagent` | 3.11 | fastapi、uvicorn、websockets、pyyaml |

vosk 中文模型在 `~/.cache/vosk/`（`vosk-model-cn-0.22`、`vosk-model-small-cn-0.22`），无需下载。

## 关键配置

### `configs/pipeline.yaml`（ASR 侧）

```yaml
asr:
  # 唤醒词。可自定义, 支持两种写法:
  #   1) 逗号分隔字符串: "你好助手,小助手"
  #   2) YAML 列表:      ["你好助手", "小助手"]
  wake_words: "你好助手"

  # 结束词 (会话结束语)。用户说完结束词后, 这一轮对话立即结束,
  # 连续对话窗口关闭, 需要重新说唤醒词才能继续。
  # 写法和唤醒词一样; 留空 = 不启用。
  end_words: "再见"

  # 结束词的别名 —— 命中任意一个都算结束。
  # 用来兜住 ASR 的常见误识别 ("拜拜" 听成 "白白"/"百拜")。
  end_aliases: "拜拜,白白,百拜,回头见"

  # 是否启用结束词的拼音模糊兜底匹配 (读音相近就算命中)。
  end_match_fuzzy: true
  # 拼音模糊匹配的相似度阈值 (0~1)。越高越严格。
  end_match_threshold: 0.6

  # 识别到结束词时的播报文案。留空 = 不播报, 只结束会话。
  # 这段话在**正文回答播完之后**才说 —— "查下天气再见" 先播天气, 再播告别。
  end_prompt: "好的，我走了"

  # 没识别到内容时的播报文案 (会经 TTS 播报出来); 留空表示不播报。
  # 同一句话也用于 /state 的 errors 记录, 保证日志与播报一致。
  empty_result_prompt: "没听清内容，请再说一遍"

  # 连续对话窗口 (秒)。一轮播报完成后, 这段时间内再次说话无需唤醒词。
  # 从「播报结束」开始计时 (机器人说话的时间不占用用户的窗口)。
  # 设为 0 关闭连续对话。
  continuous_timeout: 30.0
```

可用环境变量覆盖：`ASR_WAKE_WORDS` / `ASR_END_WORDS` / `ASR_END_ALIASES` /
`ASR_END_MATCH_FUZZY` / `ASR_END_MATCH_THRESHOLD` / `ASR_END_PROMPT` /
`ASR_EMPTY_RESULT_PROMPT` / `ASR_CONTINUOUS_TIMEOUT`。
命令行 `--wake` / `--end` / `--end-aliases` / `--no-end-fuzzy` /
`--end-match-threshold` / `--end-prompt` / `--continuous-timeout` 优先级最高。

> 唤醒词必须能被 vosk 模型词表切分，否则启动时会提示并忽略该词。
> 启动日志会打印实际切分结果，例如 `唤醒词: 你好助手 (词表切分: 你好 + 助手)`。
> **结束词不需要能被词表切分** —— 它走的是纯文本子串匹配，词表里没有也能用。

#### 结束词（会话结束语）

除了「连续对话窗口超时」，还可以用**结束词**显式结束会话：

| | 唤醒词 | 结束词 |
| --- | --- | --- |
| 作用 | **开始**一轮对话 | **结束**当前会话 |
| 配置 | `asr.wake_words` | `asr.end_words` |
| 生效时机 | 等待录音时 | 录音过程中 |
| 未配置时 | 用默认值 | 功能关闭，靠窗口超时结束 |

用户说出结束词之后：

1. **立即停止录音** —— 不等静音超时，用户说完就能收尾
2. **结束词从正文里摘掉** —— 「再见」不是问题，不发给 Agent
   （「查一下天气，再见」→ 剩下「查一下天气」照常处理）
3. **关闭连续对话窗口** —— 回到「必须重新唤醒」状态
4. 新的唤醒词会**恢复会话**，窗口重新开启
5. **正文播完之后再说收尾话** —— `end_prompt` 的内容（如「好的，我走了」）

```
IDLE ──唤醒词──> RECORDING ──"再见"──> 立即结束
                  ↑                      │
                  └── 需要重新唤醒 ──────┘
```

##### 结束词怎么匹配

三层匹配，任意一层命中就算结束：

| 层次 | 规则 | 例子 |
| --- | --- | --- |
| 精确子串 | 句子里**含有**触发词即命中 | 「再见吧」「查下天气再见」 |
| 别名 | 命中 `end_aliases` 里任意一个 | 「拜拜」「回头见」 |
| 拼音兜底 | 逐音节比对，每节都相近 | 「白白」「百拜」→「拜拜」 |

**为什么是「含有即命中」而不是「末尾匹配」**：

结束词在识别文本里的位置很不稳定 —— vosk 可能把「再见」切成两块，
后面也可能跟着语气词（「再见吧」）。要求「结尾正好等于结束词」会把这些
全都漏掉，表现为「说了再见但机器人不反应」。**放宽的代价**是「跟他说再见
之后我就走了」也会结束会话 —— 但这时结束词会被从正文里摘掉，剩余的
「跟他说之后我就走了」照常交给 Agent，行为上可以接受。

**拼音兜底**（`end_match_fuzzy`，默认开）解决的是**同音误识别**：
「拜拜」被听成「白白」「百拜」时，纯文本匹配完全无能为力，只能靠读音
比对兜住。代价是可能误触发，所以留了 `--no-end-fuzzy` 开关。

##### 拼音兜底怎么比（重要）

比对的是**逐个音节**，不是拼音字符串。

早期版本用 `SequenceMatcher` 直接比拼音**字符串**，误伤极其严重：

| 识别文本 | 命中窗口 | 结束词 | 相似度 |
| --- | --- | --- | --- |
| 现在几点 | 在几 | 再见 | 0.86 |
| 查一下天气 | 下天 | 再见 | 0.62 |
| 音量调大一点 | 一点 | 再见 | 0.67 |
| 今天会下雨吗 | 下雨吗 | 下次聊 | 0.60 |

原因：`SequenceMatcher` 比的是**字符**，而「在几」(zai ji) 与「再见」
(zai jian) 的字符前 5 个完全一样 —— **前缀碰巧相同**就被判成 0.86。
这根本不是读音相近，只是巧合。表现就是「用户说一句话，机器人就以为
听见了告别，会话直接结束」。

现在的规则：

1. 音节拆成 **(声母, 韵母)** —— 声母必须**完全相同**
2. 韵母**完全相同** → 该音节 0.95 分
3. 韵母**长**（>2 字符）且只差一处 → 0.9 分
4. **短韵母（≤2 字符）必须完全一致** —— 这是防误伤的关键。
   「几」(ji) 与「见」(jian) 声母相同但读音差得远，不该算相近
5. **每个音节都要相近**（≥0.9）才算命中 —— 防止三音节结束词里
   两个音节对上、第三个差很远时平均分仍然超阈值

所以 `end_match_threshold` 的语义是「音节相似度下限」，不是「字符串
相似度」。调低它会显著增加误触发风险。

> **结束词不再是「必须能在 vosk 词表切分」** —— 这是「结束词识别不上」的
> 根因：切分失败的词启动时就被忽略，于是它永远匹配不上。现在匹配走纯
> 文本子串 + 拼音兜底，词表里没有也能用。

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
  default_provider: agnes
  providers:
    agnes:
      base_url: https://api.agnes-ai.cn/v1
      # agnes-3.0-flash 首字延迟波动大 (1.5s~33s), 语音场景别用
      model: agnes-2.5-flash
      api_key_env: AGNES_API_KEY
  # 三个角色默认用同一个 provider; 需要时按角色拆开
  roles:
    chat:     { provider: agnes }
    classify: { provider: agnes }
    tool:     { provider: agnes }
  max_tokens: 256          # 限制生成长度 -> 直接降低 TTFT 和 TTS 合成时长
router:
  mode: hybrid            # rules | llm | hybrid
  default_route: chat
  llm_timeout: 1.2        # 路由分类超时, 超过就回落 default_route
  trust_rules: true       # 规则命中就信, 跳过 LLM 二次确认 (省 1~3 秒)
  # 原生 function calling: 规则没命中时才走, 一次问出「调哪个工具、参数」
  tool_calling:
    enabled: true
    timeout: 5.0
    tool_choice: auto     # 不要用 required
  rules:
    - intent: tool
      keywords: ["打开", "关闭", "设置"]
      exclude: ["怎么样", "如何", "为什么"]
```

> 多 provider 的完整说明见 [`llm-providers.md`](llm-providers.md)。
>
> `configs/.env` 里的 `LLM_MODEL` **会覆盖** `default_provider` 对应预设的模型名。
> 改完 YAML 记得同步检查 `.env`，否则「配置改了没效果」。

### `configs/actions.yaml`（机械臂说法映射）

动作名 = `arm_service/data/` 里的 JSON 文件名。匹配时命中**最长的说法**，
所以「伸左手」永远赢过「伸手」，排列顺序不影响结果。

```yaml
arm:
  base_url: "http://127.0.0.1:8802"
  exclude: ["怎么样", "如何", "为什么", "怎么"]
  actions:
    - action: "挥手"
      say: ["挥手", "挥个手", "挥挥手", "打个招呼"]
    - action: "伸左手"
      say: ["伸左手", "左手伸出来", "伸出左手"]
```

## 启动

```bash
# 五个服务一起起（会等健康检查通过）
./scripts/start_all.sh

# 或分别启动
./scripts/start_tts.sh      # env chattts, :8000
./scripts/start_agent.sh    # env botagent, :8800
./scripts/start_asr.sh      # env asrt, :8801
./scripts/start_arm.sh      # env botagent, :8802
./scripts/start_vision.sh   # env yolo, :8803  (相机独占, 排在 Arm 之后)

# 停止
./scripts/stop_all.sh
```

> **Vision 排在 Arm 之后** —— 视觉服务的动态外参要连 `arm_service` 拿
> 臂基座 / 头位姿，Arm 先起来它才拿得到。

### 端到端自检

```bash
./scripts/full_chain_test.sh            # 自动拉起三个服务 (TTS/Agent/ASR), 回放真实录音, 断言分流与播放
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

- `GET /health` — `{"status":"ok","state":"IDLE","wake_words":["你好助手"],"end_words":["再见"],...}`
- `GET /state` — 最近一轮的 `last_text` / `last_intent` / `last_speak` / `last_reply` / `last_action` / `errors`

状态机：`IDLE → REC → THINK → SAY → IDLE`，其中 `SAY` 只在 `speak=true` 时进入。

> 没识别到内容时也会进入 `SAY`，播报 `empty_result_prompt`（如「没听清内容，请再说一遍」）。

### TTS Service `:8000`

复用 ChatTTS 自带的 `examples/ws_server`，协议未改。`TTS_PLAY=0`，播放统一由 ASR 侧负责，避免回声与串扰。

**音色与模型从 `configs/tts.yaml` 读** —— 换音色 / 换模型不用改代码。

- `GET /health` — `{"model_loaded":true,"sample_rate":24000,"voices":[...], "default_voice":"Default", "model":{...}}`

| 配置项 | 说明 |
|---|---|
| `model.source` | `local` / `huggingface` / `custom` |
| `model.device` | `""` = 自动（有 CUDA 用 CUDA） |
| `voices[].seed` | 随机种子定义音色（同机同硬件一致） |
| `voices[].ref_audio` | 参考音频克隆音色（**跨机器一致**） |
| `defaults.voice` | 默认音色 |

客户端没显式传的字段走服务端默认值；传了不存在的音色回落到默认音色。

### Arm Service `:8802`

机械臂动作服务。**一个 JSON 文件 = 一个动作，文件名就是动作名**。

- `GET /health` — `{"status":"ok","controller_connected":false,"actions":6,...}`
- `GET /v1/actions` — 动作库清单
- `POST /v1/play` — 下发播放（异步，立即返回）
- `POST /v1/stop` — 停止播放并交还控制通道
- `GET /v1/status` — 当前播放状态
- `GET /v1/safety` — 跑一次安全门阀检查（不执行动作）

详见 [`arm-service.md`](arm-service.md)。

### 监控台（Qt6 桌面 APP）

`monitor/` 目录下一个 **只读** 的 Qt6 (QWidget) 桌面程序，看五个服务的连接状态，
并实时展示 Agent 每轮的**思考过程**。

```bash
cd monitor && ./build.sh          # 构建 (Qt 6.8.3)
monitor/build/botagent_monitor    # 运行
```

Agent 侧为此新增了一个只读的 WebSocket 推送接口：

- `GET /v1/events` (WS) — 推送每轮对话的 `turn_start` / `thinking` / `turn_end` /
  `error` 事件，含分流依据、工具选择、参数提取的推理全文与各阶段耗时。

详见 [`monitor/README.md`](../monitor/README.md)。

## 分流机制

**两级**结构，由 `configs/agent.yaml` 控制：

### 第 1 级：规则快速通道（零延迟）

`router.mode` 为 `rules` 或 `hybrid` 时启用。关键词/正则匹配，命中即返回，
**不调 LLM**。`trust_rules: true`（默认）表示不再让 LLM 二次确认。

规则示例：

```yaml
router:
  mode: hybrid
  trust_rules: true       # 规则命中就信, 跳过 LLM 二次确认
  default_route: chat
  rules:
    - intent: tool
      keywords: ["打开", "关闭", "设置", "播放", "查询"]
      exclude: ["怎么样", "如何", "为什么"]
    - intent: device
      keywords: []          # 机械臂说法由 configs/actions.yaml 生成
      exclude: ["怎么样", "怎么", "为什么"]
```

### 第 2 级：tool calling 升级路径

规则没命中（或命中了但分支认不出具体动作）时，用 **原生 function calling**
一次问出「调哪个工具、参数是什么」。

```yaml
llm:
  roles:
    tool: { provider: agnes, model: agnes-2.5-flash }
router:
  tool_calling:
    enabled: true
    timeout: 5.0
    tool_choice: auto      # 不要用 required, 否则闲聊也会调工具
```

机械臂动作与设备工具共用同一套 tool schema，定义在
`configs/tools.yaml`（机械臂那个是自动生成的）。详见
[`action-integration.md`](action-integration.md)。

### 分流结果

| 分支 | speak | 行为 |
| --- | --- | --- |
| `chat` | true | LLM 回复 → TTS 播报 |
| `tool`（执行型） | false | 直接执行，不进 TTS |
| `tool`（输出型） | true | 结果经 TTS 播报 |
| `device` | true | 安全门阀 → 驱动机械臂 → 播报结果 |

> `router.mode` 为 `llm` 时完全跳过规则层，所有请求都走 LLM 分类（默认不用）。

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

排查建议：
- 换一个模型词表里确实存在的唤醒词（如「你好助手」会被切成「你好」+「助手」）
- 用 `tools/check_asr.py <wav>` 先确认这段音频被识别成什么
- 链路自检可用 `--no-wake` 跳过唤醒环节，只验证 Agent 分流与 TTS 播放
- 环境嘈杂导致唤醒不稳时，先调低 VAD 门限（`asr.vad_threshold`），
  或调整高通截止频率（见下节）

## 麦克风输入的高通滤波

麦克风采到的原始 PCM **默认会先经过一级高通滤波**再进入识别链路
（`services/asr_service/audio_filter.py`）。

### 为什么要有

空调隆隆声、电源 50Hz 哼声这类**低频**噪声，能量集中在几十到一两百 Hz。
但 VAD 的电平判断用的是 **RMS** —— RMS 把所有频段的能量加起来算：

- 静默时的 RMS 被低频噪声拉高 → 噪声底抬高
- 噪声底抬高 → VAD 门限（`噪声底 + 10dB`）跟着抬高
- 门限抬高 → 说话声更容易被判成噪声 → 表现为「听不清」「经常不响应」

### 滤波器参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `asr.highpass_hz` | `80.0` | 截止频率。`0` = 关闭滤波 |
| `asr.highpass_order` | `4` | 阶数（4 = 两个二阶节） |

实现用 `scipy.signal.butter(..., output="sos")` 的 **SOS 形式**（高阶直接型 IIR
数值上不稳），并**跨块保持滤波器状态**（`zi`），否则 256ms 的分块边界会产生
咔哒声。

### 频响（80Hz 截止，4 阶）

| 频率 | 增益 | 说明 |
|------|------|------|
| 30Hz | -34.1dB | 隆隆声，重度衰减 |
| 50Hz | -16.4dB | 电源哼声，明显衰减 |
| 80Hz | -3.0dB | 截止点 |
| 100Hz | -0.7dB | 已基本保留 |
| 200Hz 以上 | -0.0dB | **人声内容完全无损** |

### 滤波位置

滤波在**音频入队前**应用（`wakeup_asr.py` 的 `_callback`），这样 VAD 的电平
判断、唤醒词识别、正式识别看到的都是**同一份**滤波后信号 —— 不会出现
「门限按未滤波电平算、识别按滤波信号做」的不一致。

回放模式（`--replay`）不走麦克风回调，因此在回放循环里手动应用同样的
滤波，保证回放测试测的是滤波后的链路。

### 验证

```bash
# 频响 + 信号处理 + 分块连续性 自检
conda run --no-capture-output -n asrt python services/asr_service/tools/check_highpass.py

# 用真实麦克风测 (需先停掉 ASR 服务, 麦克风是独占的)
conda run --no-capture-output -n asrt python services/asr_service/tools/check_highpass_live.py --seconds 8
```

### 没装 scipy 时

降级为**直通**（只记一次警告），不会因为缺个滤波依赖就让 ASR 服务起不来。

**TTS 加载慢**

ChatTTS 首次加载 853MB 权重较慢，`/health` 在 `model_loaded=true` 之前会返回 `loading`。启动脚本已做轮询等待。

**Agent 报 401 / 未配置密钥**

密钥在 `configs/.env`，按 provider 分开配（`AGNES_API_KEY` / `DEEPSEEK_API_KEY` …）。
`/health` 里的 `api_key_configured` 可快速确认，完整说明见
[`llm-providers.md`](llm-providers.md)。

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
