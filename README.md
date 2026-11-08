# Bot Agent — 模块化机器人语音系统

三个独立进程、三个独立 conda 环境，组成一条**不落盘**的语音问答闭环。

```
麦克风 → [ASR :8801] → 文本 → [Agent :8800] → 分流
                                          ├─ chat  → speak=true  → [TTS :8000] → 扬声器直放
                                          └─ tool  → speak=false → 直接执行动作（不进 TTS）
```

## 快速开始

```bash
# 一键启动三个服务（会等健康检查通过）
./scripts/start_all.sh

# 端到端自检（自动拉起服务、回放真实录音、断言分流与播放）
./scripts/full_chain_test.sh

# 停止
./scripts/stop_all.sh
```

## Conda 环境

| 服务 | 环境 | Python | 端口 |
| --- | --- | --- | --- |
| ASR | `asrt` | 3.10 | 8801 |
| Agent | `botagent` | 3.11 | 8800 |
| TTS | `chattts` | 3.11 | 8000 |

vosk 中文模型已在 `~/.cache/vosk/`，无需下载。

## 目录

```
services/
  asr_service/    源自 project/ASRT（wakeup_asr.py 原样保留）+ 编排层
  tts_service/    源自 project/ChatTTS（WS 协议未改）
  agent_service/  新增：FastAPI + 意图分流 + 可扩展分支
configs/          agent.yaml / pipeline.yaml / .env
scripts/          启停与自检脚本
docs/             architecture.md（接口契约 + 排障）
```

## 核心语义

1. **ASR 永远是第一环**：麦克风 → 唤醒词 → 识别文本。
2. **Agent 做语义识别并分流**，返回 `speak` 字段决定下游。
3. **只有 `speak=true` 才连 TTS**，PCM 边收边播，全程不写任何音频文件。
4. **新增分支只需两步**：写一个 `Handler` 子类 + 在 `configs/agent.yaml` 注册，不用改 ASR / TTS。

详见 [`docs/architecture.md`](docs/architecture.md)（架构与接口契约）和
[`docs/testing.md`](docs/testing.md)（怎么测）。
