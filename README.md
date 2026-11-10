# Bot Agent — 模块化机器人语音系统

四个独立进程、三个独立 conda 环境，组成一条**不落盘**的语音控制闭环。

```
麦克风 → [ASR :8801] → 文本 → [Agent :8800] → 分流
                                          ├─ chat   → speak=true  → [TTS :8000] → 扬声器直放
                                          ├─ tool   → 按类型决定是否播报
                                          └─ device → 安全门阀 → [Arm :8802] → 机械臂
```

## 快速开始

```bash
# 一键启动四个服务（会等健康检查通过）
./scripts/start_all.sh

# 端到端自检（自动拉起服务、回放真实录音、断言分流与播放）
./scripts/full_chain_test.sh

# 停止
./scripts/stop_all.sh
```

## 服务一览

| 服务 | 环境 | Python | 端口 | 职责 |
| --- | --- | --- | --- | --- |
| ASR | `asrt` | 3.10 | 8801 | 采集 + 唤醒词 + 识别 + 直放 TTS 音频 |
| Agent | `botagent` | 3.11 | 8800 | 意图分流 + 动作编排 + LLM 对话 |
| TTS | `chattts` | 3.11 | 8000 | 语音合成（WS 流式 PCM） |
| Arm | `botagent` | 3.11 | 8802 | 机械臂动作序列播放 + 安全门阀 |

vosk 中文模型已在 `~/.cache/vosk/`，无需下载。

## 目录

```
services/
  asr_service/    源自 project/ASRT（wakeup_asr.py 原样保留）+ 编排层
  tts_service/    源自 project/ChatTTS（WS 协议未改）
  agent_service/  FastAPI + 意图分流 + 统一动作层 + 可扩展分支
  arm_service/    机械臂动作序列播放（时间轴 / 关节流 / 安全门阀）
configs/
  agent.yaml      Agent 配置（LLM / 分流规则 / tool calling）
  tools.yaml      tool 定义（模型看到的工具清单）
  actions.yaml    机械臂「说法 → 动作名」映射表
  arm.yaml        机械臂服务配置（控制器 / 关节限幅 / 安全门阀）
  tts.yaml        TTS 音色与模型配置
  pipeline.yaml   ASR 侧配置（唤醒词 / 连续对话窗口）
  .env            密钥，不入库
scripts/          启停与自检脚本
docs/
  architecture.md         架构与接口契约
  action-integration.md   动作接入（Agnes 原生 function calling）
  arm-service.md          机械臂服务（协议约束 / 安全门阀）
  testing.md              怎么测
  latency-optimization.md 延迟优化记录
```

## 核心语义

1. **ASR 永远是第一环**：麦克风 → 唤醒词 → 识别文本。
2. **Agent 做语义识别并分流**，返回 `speak` 字段决定下游。
3. **只有 `speak=true` 才连 TTS**，PCM 边收边播，全程不写任何音频文件。
4. **新增分支只需两步**：写一个 `Handler` 子类 + 在 `configs/agent.yaml` 注册，不用改 ASR / TTS。

## 动作接入

Agent 用 **Agnes 的原生 function calling** 识别动作，机械臂动作与设备工具
共用同一套 tool schema。

```
用户话语 → 规则匹配 (零延迟)
              │ 命中 → 直接执行，不调 LLM
              └ 未命中 → Agnes function calling → 选工具 + 填参数 → 执行
```

- **规则命中是 0ms 快速通道** —— 语音体感的基础，不能动
- **规则没命中才调 LLM**（约 700ms），一次调用同时完成「分类」和「选动作」
- **机械臂动作必经安全门阀** —— 换成 tool calling 不会绕过安全检查

详见 [`docs/action-integration.md`](docs/action-integration.md)。

### 自定义工具

**tool 定义在 yaml 里** —— 加工具 = 在 `configs/tools.yaml` 加一条，不用改 Python：

```yaml
tools:
  - name: control_desk_lamp
    kind: device          # arm / device / query
    label: 台灯
    description: 打开或关闭台灯。
    value_arg: state
    action_map:
      "on": desk_lamp_on    # 注意: on/off 必须加引号
      "off": desk_lamp_off
    parameters:
      type: object
      properties:
        state:
          type: string
          enum: ["on", "off"]
      required: ["state"]
```

只有**实际执行逻辑**才需要动代码 —— 在
`services/agent_service/handlers/tool_handler.py` 的 `TOOLS` 表加内部动作项，
并在 `execute()` 里实现。

| `kind` | 走哪个分支 | 结果是否朗读 |
|---|---|---|
| `arm` | `device`（机械臂分支，必经安全门阀） | 是 |
| `device` | `tool` | 否 |
| `query` | `tool` | 是 |

### 机械臂动作库

**一个 JSON 文件 = 一个动作，文件名就是动作名**：

```
services/arm_service/data/挥手.json   →  动作 "挥手"
services/arm_service/data/零点.json   →  动作 "零点"
```

新增动作：往 `data/` 放 `<动作名>.json`，再在 `configs/actions.yaml` 加一条
说法映射。

### 音色与模型

**TTS 的音色和模型都在 `configs/tts.yaml` 里**，改配置重启服务即可：

```yaml
model:
  source: local          # local / huggingface / custom
  device: ""             # "" = 自动（有 CUDA 用 CUDA）
voices:
  - name: Default
    seed: 2              # 方式 1: 随机种子
  - name: 客服小美
    ref_audio: assets/voices/xiaomei.wav   # 方式 2: 参考音频克隆（跨机器一致）
defaults:
  voice: Default         # 默认音色
  rate: 1.0              # 保音高变速倍率
```

客户端**没显式传**的字段走服务端默认值；传了不存在的音色会回落到默认音色。

查当前生效的配置：`curl -s http://127.0.0.1:8000/health`

### 安全门阀

**动作执行前先检查控制器状态，有异常就拒绝动作，并把原因播报出来。**

检查项：控制器连通 / 安全锁定 / 急停 / 安全响应 / 未上使能 / 运行模式。
详见 [`docs/arm-service.md`](docs/arm-service.md)。

## 常用命令

```bash
# 看服务状态
curl -s http://127.0.0.1:8800/health | python -m json.tool

# 手动试分流 (不经过 ASR)
curl -s -X POST http://127.0.0.1:8800/v1/dispatch \
  -H 'Content-Type: application/json' \
  -d '{"text":"挥个手","use_history":false}' | python -m json.tool

# 看机械臂动作库
curl -s http://127.0.0.1:8802/v1/actions | python -m json.tool

# 跑一次安全门阀 (不执行动作)
curl -s http://127.0.0.1:8802/v1/safety | python -m json.tool
```

## 自检脚本

```bash
cd services/agent_service
python tools/check_tool_calling.py          # 动作接入 (离线)
python tools/check_tool_calling.py --live   # + 真实 API 的 tool_calls 解析
python tools/selftest.py                    # 分流 / 播报语义
python tools/check_device.py                # 机械臂说法匹配
python tools/check_actions.py               # actions.yaml 与动作库是否对得上

cd ../arm_service
python tools/check_arm.py                   # 协议级: 时序 / 报文 / 对齐 / 停控
python tools/check_arm_service.py           # 端到端: mock 控制器 + 真实 HTTP/WS
python tools/check_safety.py                # 安全门阀: 逐项判定 + 播报文案

cd ../tts_service
python examples/ws_server/check_tts_config.py      # TTS 音色 / 模型配置读取
python examples/ws_server/check_tts_behavior.py    # 客户端覆盖 vs 服务端默认
```

## 文档

- [`docs/architecture.md`](docs/architecture.md) — 架构与接口契约
- [`docs/action-integration.md`](docs/action-integration.md) — 动作接入与自定义工具
- [`docs/arm-service.md`](docs/arm-service.md) — 机械臂服务
- [`docs/testing.md`](docs/testing.md) — 怎么测
- [`docs/latency-optimization.md`](docs/latency-optimization.md) — 延迟优化记录
