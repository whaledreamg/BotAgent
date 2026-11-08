# 测试指南

本文档覆盖三种测试场景，从简单到完整：

| 场景 | 命令 | 覆盖范围 | 耗时 |
| --- | --- | --- | --- |
| 一键端到端自检 | `./scripts/full_chain_test.sh` | 全链路 + 分流 + 不落盘 | 1~2 分钟 |
| 分段延迟压测 | 见 [第 3.6 节](#36-分段延迟压测) | 定位慢在哪一环 | 约 10 秒 |
| 分步手工排查 | 见 [第 3 节](#3-分步手工排查) | 定位到具体哪一环 | 按需 |
| 真机语音交互 | 见 [第 4 节](#4-真机语音交互) | 麦克风 → 扬声器 | 需声卡 |

---

## 1. 前置条件

### 1.1 环境检查

三个 conda 环境必须存在：

```bash
conda env list | grep -E "asrt|chattts|botagent"
```

期望输出：

```
asrt       /home/gxh/miniconda3/envs/asrt
botagent   /home/gxh/miniconda3/envs/botagent
chattts    /home/gxh/miniconda3/envs/chattts
```

### 1.2 音频设备检查

```bash
arecord -l   # 录音设备
aplay -l     # 播放设备
```

> **无声卡也能测。** 若上面报「找不到音效卡」，说明这台机器没有声卡。
> 此时 TTS 会自动降级为「只合成不播放」（日志里 `played=false`），
> 链路的识别、分流、合成、传输部分全部可测，只有「扬声器出声」这一项无法验证。
> 详见 [第 5 节](#5-无音频设备时的表现)。

### 1.3 模型与密钥

```bash
ls ~/.cache/vosk/                          # 需要 vosk-model-cn-0.22
cat "Bot Agent/configs/.env"               # 需要 LLM_API_KEY
```

`configs/.env` 不存在时，Agent 仍可启动，但 LLM 分支会失败，
`/health` 里 `api_key_configured` 会是 `false`。

---

## 2. 一键端到端自检

**这是最推荐的入门测试。** 脚本自己完成全部编排，不需要手动启动任何服务。

```bash
cd "/home/gxh/workspace/project/Bot Agent"
./scripts/full_chain_test.sh
```

### 2.1 它做了什么

1. 清理上一轮遗留进程（避免端口占用）
2. 启动 TTS（`:8000`）→ 等待 `model_loaded=true`
3. 启动 Agent（`:8800`）→ 等待就绪
4. 用真实录音生成 16k 测试音频（`--skip-wake-audio`）
5. 以 `--no-wake` 启动 ASR 回放（`:8801`），跳过唤醒词环节
6. 轮询 `/state` 直到本轮处理完成
7. 逐项断言，结束后自动拆掉三个服务

### 2.2 期望输出

```
=== 启动 TTS ===
{"status":"ok","model_loaded":true,...}
=== 启动 Agent ===
{"status":"ok","model":"agnes-2.5-flash","branches":["chat","tool"],...}
=== 生成测试 wav ===
OK: /tmp/fc_test_16k.wav
=== ASR 状态 ===
{"state":"SAY","turns":1,"last_text":"今天天气怎么样说话呀",
 "last_intent":"chat","last_speak":true,"last_reply":"...","last_action":null}

  [PASS] ASR 已记录识别文本
  [PASS] 分流到 chat 分支
  [PASS] speak=true (走了 TTS 朗读)
  [PASS] 已生成回复文本
  [PASS] 工具类分支 speak=false (不走 TTS)
  [PASS] 工具类意图识别为 tool
  [PASS] 全程未产生任何音频文件

结果: 全部通过
```

### 2.3 断言含义

| 断言 | 验证的是什么 |
| --- | --- |
| ASR 已记录识别文本 | 回放音频被 vosk 正确转成文字 |
| 分流到 chat 分支 | Agent 路由把闲聊判给了 `chat` |
| speak=true | 该分支需要朗读，会连 TTS |
| 已生成回复文本 | LLM 真的产出了回复内容 |
| 工具类分支 speak=false | 工具分支**不**进 TTS（分支核心语义） |
| 工具类意图识别为 tool | 动作指令被判给了 `tool` |
| 全程未产生任何音频文件 | 满足「不保存本地，直接播放」的要求 |

### 2.4 指定测试录音

默认用 `ASRT/recordings/rec_20261106_182400.wav`（内容是「今天天气怎么样」）。
可以换成自己的录音：

```bash
./scripts/full_chain_test.sh /path/to/your_16k_mono.wav
```

**要求**：16kHz、单声道、16bit PCM。

### 2.5 关于 `--no-wake`

自检脚本用 `--no-wake` 跳过唤醒词判定。原因见
[第 6 节](#6-关于唤醒词)。

---

## 3. 分步手工排查

适合「自检挂了，想知道是哪一环出的问题」。

### 3.1 启动服务

```bash
cd "/home/gxh/workspace/project/Bot Agent"
./scripts/start_all.sh
```

期望看到三行：

```
[ok] tts 就绪: http://127.0.0.1:8000/health
[ok] agent 就绪: http://127.0.0.1:8800/health
[ok] asr 就绪: http://127.0.0.1:8801/health
```

日志在 `logs/`：`tts.out` / `agent.out` / `asr.out`。

### 3.2 单独验 TTS

```bash
cd "/home/gxh/workspace/project/Bot Agent/services/asr_service"
/home/gxh/miniconda3/envs/asrt/bin/python tools/check_tts.py --text "你好，今天天气怎么样"
```

期望：

```
  事件: {"type":"ready","sample_rate":24000,...}
  事件: {"type":"accepted","request_id":"check-tts","queue_pos":1,...}
  收到 AUDIO_END (seq=9)
OK: 9 帧, 84064 字节, 24000Hz, 约 1.75 秒
```

帧数和字节数 > 0 即说明 TTS 合成与 WS 传输正常。

### 3.3 单独验 Agent 分流

```bash
curl -s -X POST http://127.0.0.1:8800/v1/dispatch \
  -H 'Content-Type: application/json' \
  -d '{"text":"打开客厅的灯","session_id":"t1"}'
```

关注两个字段：`intent` 和 `speak`。

| 输入 | 期望 `intent` | 期望 `speak` | 是否进 TTS |
| --- | --- | --- | --- |
| 今天天气怎么样 | `chat` | `true` | 是 |
| 讲个笑话 | `chat` | `true` | 是 |
| 打开客厅的灯 | `tool` | `false` | 否 |
| 打开空调 | `tool` | `false` | 否 |
| 把音量调大一点 | `tool` | `false` | 否 |
| 查询北京天气 | `tool` | `true` | 是（输出型工具） |
| 现在几点 | `tool` | `true` | 是（输出型工具） |
| 今天几号 | `tool` | `true` | 是（输出型工具） |

完整响应示例：

```json
{
  "session_id": "t1",
  "text": "打开客厅的灯",
  "intent": "tool",
  "route": ["tool"],
  "speak": false,
  "reply": "暂不支持这个操作。",
  "action": {"name": "none", "args": {}, "status": "unsupported"},
  "latency_ms": 1,
  "trace": [{"step": "rules", "ms": 0.0, "detail": "tool:打开"}]
}
```

> `chat` 类输入会真实调用 LLM，首次可能需要 10~30 秒（上游延迟）。
> 超时会自动回落 `default_route`，不会卡死。

### 3.4 看 ASR 当前状态

```bash
curl -s http://127.0.0.1:8801/state
```

```json
{
  "status": "ok",
  "state": "SAY",
  "turns": 1,
  "wake_words": "你好助手",
  "last_text": "今天天气怎么样说话呀",
  "last_intent": "chat",
  "last_speak": true,
  "last_reply": "建议你看一眼手机自带的天气应用……",
  "last_action": null,
  "timings": {
    "asr_record_ms": 12281,
    "agent_total_ms": 1179,
    "agent_server_ms": 1176,
    "tts_total_ms": 4914,
    "turn_total_ms": 6094,
    "end_to_end_ms": 18375
  },
  "errors": []
}
```

`state` 字段是状态机当前位置：

```
IDLE  --唤醒-->  REC  --识别完成-->  THINK  --speak=true-->  SAY  --播完-->  IDLE
                                    \--speak=false-------------------------->  IDLE
```

### 3.5 停止服务

```bash
./scripts/stop_all.sh
```

### 3.6 分段延迟压测

**排查「响应慢」的第一站。** 把链路拆成几块各自单独测，直接看出慢在哪。

```bash
cd "/home/gxh/workspace/project/Bot Agent/services/asr_service"
/home/gxh/miniconda3/envs/asrt/bin/python tools/profile_latency.py
```

输出带横条图，宽度即耗时占比：

```
 2) Agent 分流
  [打开客厅的灯]
  总往返                        3ms | ········································
    intent=tool speak=False
    trace: rules:0.0ms trust:0.0ms match:0.7ms

 4) TTS 合成
总计                        4913ms | ████████████████████████████████████████
  连接                        4ms | ········································
  首帧                      465ms | █████████·······························
  合成                     4337ms | ████████████████████████████████████████
  音频时长                  4380ms | ████████████████████████████████████████
```

参数：

| 参数 | 作用 |
| --- | --- |
| `--wav <path>` | 额外测 ASR 纯计算耗时（需 16kHz 单声道 wav） |
| `--skip asr,agent,chat,tts` | 跳过指定段落 |

**关键指标含义：**

| 指标 | 含义 | 健康值 |
| --- | --- | --- |
| Agent `总往返` − `服务端内部` | 网络开销 | < 10ms |
| TTS `首帧` | 从发请求到听到第一个字 | < 1s |
| TTS `合成` | 整段合成耗时 | ≈ `音频时长`（RTF≈1） |
| TTS `音频时长` | 播报本身要多久 | 越短越好，取决于回复字数 |
| Agent `分流` | 意图识别耗时 | 规则命中应 < 10ms |

**如果 `首帧` 很大**（几秒），最常见的原因是「末字截断重试」被打开了 ——
它要把整段音频合成两遍，首帧会翻 4~9 倍。检查
`services/tts_service/examples/ws_server/engine.py` 里的
`RETRY_ON_TRUNCATION`（默认关闭）。

日志里出现 `tail truncated (decay=...ms), retry #1` 就说明重试开着。
需要临时打开时置环境变量 `TTS_RETRY_ON_TRUNCATION=1`。

**如果 `分流` 是几百毫秒**，说明规则没命中、走了 LLM 兜底分类。
把常用指令加进 `configs/agent.yaml` 的 `router.rules.keywords` 即可归零。

---

## 4. 真机语音交互

需要声卡（麦克风 + 扬声器）。

### 4.1 确认设备

```bash
arecord -l    # 录音设备列表
aplay -l      # 播放设备列表
```

### 4.2 启动

```bash
cd "/home/gxh/workspace/project/Bot Agent"
./scripts/start_all.sh
```

ASR 会自动挑选原生的 16kHz 麦克风。挑错了就显式指定：

```bash
./scripts/start_asr.sh --device 3
```

### 4.3 交互流程

对着麦克风说：

```
你好助手，今天天气怎么样
```

预期行为：

1. ASR 检测到唤醒词 → 进入录音
2. 静音 1.2 秒后结束录音 → 识别出文本
3. Agent 判为 `chat` → `speak=true`
4. TTS 合成 → PCM 边收边播 → 扬声器念出回复

### 4.4 验证工具分支不朗读

对着麦克风说：

```
你好助手，打开客厅的灯
```

预期：**扬声器不出声**（工具分支 `speak=false`，不进 TTS），
`/state` 里 `last_intent="tool"`、`last_speak=false`、`last_action` 有值。

### 4.5 用录音代替麦克风

没有麦克风但有音频文件时，用回放模式：

```bash
# 用现成的真人录音
./scripts/start_asr.sh --replay /home/gxh/workspace/project/ASRT/recordings/rec_20261106_182400.wav

# 自己录一段（需要麦克风）
arecord -f S16_LE -r 16000 -c 1 -d 5 /tmp/my.wav
./scripts/start_asr.sh --replay /tmp/my.wav
```

**要求**：16kHz、单声道、16bit PCM。

回放模式下想保留 HTTP 服务方便查状态：

```bash
./scripts/start_asr.sh --replay x.wav --keep-alive 60
```

### 4.6 自定义唤醒词

改 `configs/pipeline.yaml`：

```yaml
asr:
  # 写法一: 逗号分隔
  wake_words: "你好助手,小助手"
  # 写法二: YAML 列表
  # wake_words: ["你好助手", "小助手"]
```

改完重启 ASR 服务生效。启动日志会打印实际切分结果：

```
[*] 唤醒词: 小助手  (词表切分: 小助手)
[*] 唤醒词: 你好助手  (词表切分: 你好 + 助手)
```

临时用不同的（不改配置文件）：

```bash
./scripts/start_asr.sh --wake "小助手,你好助手"
```

也可用环境变量：`ASR_WAKE_WORDS="小助手"`。

> **注意**：唤醒词必须能被 vosk 模型词表切分。切不开的词会在启动时被提示并忽略，
> 如果所有唤醒词都切不开，服务会直接报错退出。启动日志里看到「词表切分」才说明生效了。

### 4.7 「没听清」的语音提示

识别不到内容时，ASR 会通过 TTS 播报一句提示。改 `configs/pipeline.yaml`：

```yaml
asr:
  empty_result_prompt: "没听清内容，请再说一遍"
```

> 这句话**既用于 TTS 播报，也用于 `/state` 的 `errors` 记录**，两处内容保证一致。
> 否则会出现「日志说一套、机器人说另一套」，排查时容易误判。

留空则不播报，只在日志里记录。也可以用环境变量 `ASR_EMPTY_RESULT_PROMPT` 覆盖。

单独验证这条链路（不需要麦克风）：

```bash
cd "/home/gxh/workspace/project/Bot Agent/services/asr_service"
/home/gxh/miniconda3/envs/asrt/bin/python tools/check_empty_prompt.py
```

期望：

```
[PASS] 已播报: '没听清内容，请再说一遍'
[PASS] errors 记录与播报一致: '没听清内容，请再说一遍'
```

### 4.8 连续对话（免唤醒）

一轮对话播报完成后，**30 秒内**再次说话不需要唤醒词，直接开始录音；
超时则回到等待唤醒的状态。

改 `configs/pipeline.yaml`：

```yaml
asr:
  # 连续对话窗口（秒）。从「本轮播报结束」开始计时。
  # 设为 0 关闭连续对话（每轮都要唤醒）。
  continuous_timeout: 30.0
```

也可以用环境变量 `ASR_CONTINUOUS_TIMEOUT=30` 或命令行 `--continuous-timeout 30` 覆盖。

工作方式：

1. 一轮播报完成 → 开启窗口，日志打印 `[连续对话] 已开启, 30s 内再次说话无需唤醒词`
2. 窗口内持续做 VAD 监听，**检测到人声才**开始录音
   （否则环境噪声会被当成用户开口，白等一个静音超时）
3. 录音结束后重新计时，窗口继续有效
4. 窗口过期 → 日志打印 `[连续对话] 已关闭, 需要重新唤醒`

日志里能直接看到状态：

```
[连续对话] 已开启, 30s 内再次说话无需唤醒词
[连续对话] 检测到语音 (窗口剩 24.1s), 开始录音
```

终端提示也会区分：

```
已唤醒! 唤醒词: [小助手]  请开始说话...     ← 说了唤醒词
[连续对话] 窗口内 (剩 24s), 请继续说话...   ← 连续对话中
```

> **两个设计细节**
>
> - **计时从播报结束开始**，机器人自己说话的时间不占用用户的 30 秒。
> - **前滚音频会排除 TTS 播放期间录到的声音**。麦克风在机器人播报时
>   也会录到声音，如果不排除，下一轮会把机器人的声音当成用户的话识别出来。

单独验证（不需要麦克风，9 项断言）：

```bash
cd "/home/gxh/workspace/project/Bot Agent/services/asr_service"
/home/gxh/miniconda3/envs/asrt/bin/python tools/check_continuous.py
```

端到端验证（真实模型 + 真实 Agent/TTS，回放两段语音，中间不唤醒）：

```bash
/home/gxh/miniconda3/envs/asrt/bin/python tools/check_continuous_live.py
```

### 4.9 ASR 服务可用参数

| 参数 | 作用 |
| --- | --- |
| `--wake "你好助手"` | 唤醒词，多个用逗号分隔 |
| `--replay <wav>` | 回放模式，不占麦克风 |
| `--replay-speed <倍率>` | 回放速度（默认 1.0 = 真实时间，2.0 = 两倍速） |
| `--device <索引>` | 指定采集设备 |
| `--duration <秒>` | 单次录音最长时长（默认 30） |
| `--silence <秒>` | 静音多久结束录音（默认 1.2） |
| `--no-wake` | 跳过唤醒词判定，**仅链路自测用** |
| `--continuous-timeout <秒>` | 连续对话窗口（默认取配置文件，30） |
| `--keep-alive <秒>` | 回放结束后保留 HTTP 服务，便于读 `/state` |
| `--quiet` | 不显示电平条 |

> **回放速度为什么默认是真实时间？**
> 基类的回放是加速 5 倍（每 256ms 的音频只 sleep 50ms）。加速回放时，
> 第二段语音会在第一轮的 Agent/TTS 还没跑完时就到达，被 `busy` 状态挡掉，
> 测不出「窗口内无需唤醒词」这个行为。改成真实节奏后才是真实的场景。

---

## 5. 无音频设备时的表现

没有声卡时系统不会崩，会自动降级：

| 环节 | 降级行为 | 日志 |
| --- | --- | --- |
| 麦克风采集 | 不可用，只能用 `--replay` | `找不到音效卡` |
| TTS 播放 | 只合成不播放，链路其余部分照常 | `[TTS] 无可用输出设备, 只合成不播放` |
| TTS 统计 | `played=false` | `[TTS] 完成 xxxms frames=N bytes=M played=False` |

所以「扬声器出声」这一项在本机无法验证，其余全部可验证。

---

## 6. 关于唤醒词

唤醒词判定属于 **ASR 模型层面**，不在本编排层的职责范围内。
`services/asr_service/wakeup_asr.py` 与 `project/ASRT` 原版**完全一致**，没有任何改动。

当前状态：

- 使用的 `vosk-model-cn-0.22` **不含 `Gr.fst`**，走通用识别器兜底匹配唤醒词
- 唤醒词「你好助手」会被切成「你好」+「助手」两个词表单元
- **真人语音**的识别质量明显好于 **TTS 合成音**
  （TTS 合成音会被识别成「你好注射」，真人录音识别正常）

### 排查建议

```bash
cd "/home/gxh/workspace/project/Bot Agent/services/asr_service"
/home/gxh/miniconda3/envs/asrt/bin/python tools/check_asr.py /path/to/your.wav
```

先确认这段音频**被识别成什么**，再判断是唤醒词选得不对还是识别质量问题。

### 可选改进方向（尚未实施）

- 换一个词表里确实存在的唤醒词
- 安装一个带 `Gr.fst` 的小模型，走语法受限识别，唤醒更准、误触发更少
- 链路自检用 `--no-wake` 绕开（推荐，不影响真实使用）

---

## 7. 常见问题

### Agent 报 401 / 未配置密钥

密钥在 `configs/.env`（`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`），可用环境变量覆盖。
`curl http://127.0.0.1:8800/health` 看 `api_key_configured` 字段。

### 回应太慢

**先做分段压测定位，别猜。** 见 [第 3.6 节](#36-分段延迟压测)。

链路每一环都打了分段耗时日志：

| 环节 | 日志标签 | 文件 |
| --- | --- | --- |
| ASR 录音+识别 | `[计时] ASR 录音+识别阶段` | `services/asr_service/server.py` |
| Agent 网络往返 | `[Agent] 往返 Xms (服务端 Yms)` | `services/asr_service/agent_client.py` |
| Agent 分流 vs 分支 | `[计时] 总计/分流/分支执行` | `services/agent_service/app.py` |
| 上游 LLM | `[LLM] Xms model=... tokens=...` | `services/agent_service/llm.py` |
| TTS 分段 | `[TTS] 总计/连接/首帧/合成/播放收尾` | `services/asr_service/tts_client.py` |
| TTS 服务端 | `[TTS-服务端] 总计/首帧/队列等待` | `services/tts_service/examples/ws_server/main.py` |
| **端到端汇总** | `[全链路计时]` | `services/asr_service/pipeline.py` |

端到端汇总长这样：

```
[全链路计时] 语音->文字 12281ms | Agent 1179ms (服务端 1176ms) | TTS 4914ms
             | 文字->语音 6094ms | 端到端 18375ms
```

`/state` 接口也会带上 `timings` 字段，可直接用 `jq` 查：

```bash
curl -s http://127.0.0.1:8801/state | jq .timings
```

#### 已知的三个主要瓶颈及处理

**1. 上游 LLM 延迟（已解决）**

`agnes-3.0-flash` 实测首字延迟在 1.5s ~ 33s 之间剧烈波动（三次抽样）。
`agnes-2.5-flash` 实测稳定在 TTFT ~450ms / 总耗时 ~700ms。
默认模型已切到 `agnes-2.5-flash`。

> **注意**：`configs/.env` 里的 `LLM_MODEL` **会覆盖** `configs/agent.yaml` 的
> `llm.model`。改完 YAML 记得同步改 `.env`，否则「配置改了没效果」。

**2. TTS 首帧延迟（已解决）**

`engine.synthesize` 原来无论文本长短都先 `list()` 缓冲完整段音频，
把「流式」退化成了整段阻塞生成 —— 首帧延迟等于整段合成耗时（实测 9.3s）。
现在只在需要判定末字截断时才缓冲，且加了时间闸
（`RETRY_TIME_BUDGET_MS`，默认 2500ms，超预算直接用当前结果）。

**2b. TTS 首帧仍然慢（2026-11-08 再次解决）**

首帧还有 4600ms 左右，原因是「末字截断重试」在把整段音频合成两遍。
实测这个功能是净负收益（修好 1/4、弄坏 1/4、首帧 +2766ms），已默认关闭。

另外还修了「前导静音裁剪死等 600ms lookahead」和「首个请求承担 CUDA 预热」
两个问题。详见 [latency-optimization.md](latency-optimization.md) 的问题 6~8。

现在首帧：稳态 ~350ms，首个请求 ~430ms。

**3. ChatTTS 跑在 CPU 上（未解决，需要硬件）**

```
ChatTTS | gpu | no GPU or NPU found, use CPU instead
```

CPU 上合成速度约等于实时（RTF≈0.74），即音频 12 秒就要合成 12 秒。
上 GPU 可再降一个量级。

#### 可调的参数

```yaml
# configs/agent.yaml
llm:
  model: agnes-2.5-flash   # 换更快的模型
  timeout: 20              # 上游单次请求超时
  max_tokens: 256          # 限制生成长度 -> 直接降低 TTFT 和 TTS 时长
router:
  llm_timeout: 1.2         # 路由分类超时, 超时回落 default_route
  trust_rules: true        # 规则命中就信, 跳过 LLM 二次确认
```

系统提示词里限制回复字数也很关键 —— CPU 上 TTS 合成速度约 1 字 / 0.19 秒，
回复 67 字就要合成 12.5 秒。当前提示词已限制「总长度不超过 40 个字」。

让规则优先命中更多场景，可以把常用指令加进 `router.rules.keywords`，
这样分流阶段就不用等 LLM（实测规则命中 3ms，LLM 分类 500ms+）。

### Agent 返回 500 / LLM 请求异常

**已修复**：某些环境里带着 `ALL_PROXY=socks://...` 这类 socks 代理变量，
httpx 不认识该协议，构造客户端时会直接抛 `ValueError`，冒泡成 HTTP 500。
现在 `LLMClient` 会先摘掉这类环境变量，并把意外异常统一转成 `LLMError`，
最坏情况也只是回复「服务暂时不可用」而不是 500。

Agent 日志里会出现这行属正常：

```
已忽略不受支持的代理环境变量: ALL_PROXY=socks://127.0.0.1:7897
```

如果确实需要走代理，在 `configs/.env` 里用 httpx 支持的 `http://` 形式配置。

### 服务起不来：端口被占用

**现在脚本会自动处理。** `scripts/stop_all.sh` 会按 pid 文件 + **按端口**双重回收
（`conda run` 会多 fork 一层，子进程 pid 与 pid 文件里的不一致，只按 pid 回收会漏）。

```bash
./scripts/stop_all.sh
```

它结束时会明确报告端口状态：

```
[ok] 端口 8000/8800/8801 均已释放
```

`scripts/start_all.sh` 启动前会检查端口占用并**直接报错退出**，
避免旧进程顶掉新进程、而健康检查却照样通过（测到的就是旧配置）。

万一还有漏网的，手动按端口回收：

```bash
for p in 8000 8800 8801; do
    ss -ltnpH "sport = :$p" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | sort -u | xargs -r kill -9
done
```

`full_chain_test.sh` 开头会自动做这两件事。
ASR 服务端口被占用时会直接报错退出（不会静默降级），避免读到别的进程的 `/state`。

### TTS 加载慢

ChatTTS 首次加载 853MB 权重较慢，`/health` 在 `model_loaded=true` 之前返回 `loading`。
启动脚本已做轮询等待。

### 工具分支的播报规则

`tool` 分支不再是一刀切不朗读，而是按工具类型区分：

| 类型 | 例子 | `speak` | 说明 |
| --- | --- | --- | --- |
| 执行型 `kind=action` | 开灯、开空调、播放音乐 | `false` | 静默执行 |
| 输出型 `kind=query` | 查天气、查时间、查日期 | `true` | 结果经 TTS 播报 |

输出型工具的 `reply` 就是**要朗读的内容本身**（如「北京今天天气晴，气温 24 度」）。

哪些工具算输出型，可在 `configs/agent.yaml` 的 `handlers.tool.options.speak_tools`
里显式指定；不填则按代码内置判定（`kind=query`）。

### 打断（barge-in）暂未实现

TTS 的 WS 协议已支持 `stop` 控制报文，接入成本低。
当前实现会在 `THINK` 和 `SAY` 阶段停止识别，以避免把自己的声音再识别一遍。

---

## 8. 相关文档

- [`architecture.md`](architecture.md) — 架构、接口契约、新增分支的方法
- [`latency-optimization.md`](latency-optimization.md) — 延迟排查与优化记录（端到端 50.9s → 18.4s）
- [`../README.md`](../README.md) — 快速开始
