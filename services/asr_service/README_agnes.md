# Agnes AI 多模态模型接入

把 [Agnes AI](https://www.agnes-ai.cn/zh-Hans/docs/agnes-25-flash) 的多模态模型接入本项目，
实现「唤醒词 → 语音识别 → 模型对话 → 可选朗读」的完整链路。

## 文件

| 文件 | 说明 |
| --- | --- |
| `agnes_client.py` | Agnes API 客户端（对话/流式/工具调用/图像理解/图像生成） |
| `voice_assistant.py` | 语音助手：Vosk 识别 + Agnes 对话 |
| `.env` | API Key 等配置（已在 `.gitignore` 中排除） |
| `.gitignore` | 排除 `.env` 和录音文件 |

## 配置

`.env`：

```bash
AGNES_API_KEY=sk-xxxxxxxx
AGNES_BASE_URL=https://api.agnes-ai.cn/v1
AGNES_MODEL=agnes-3.0-flash
```

也可用环境变量覆盖：`export AGNES_API_KEY=...`

## 快速验证

`agnes_client.py` 只依赖 `requests`，系统 `python3` 和 conda 环境都能跑：

```bash
cd /home/gxh/workspace/project/ASRT
PY=/home/gxh/miniconda3/envs/vosk/bin/python

# 列出可用模型
$PY agnes_client.py --list-models

# 文本对话
$PY agnes_client.py "用一句话介绍你自己"

# 流式输出
$PY agnes_client.py --stream "写一首关于秋天的小诗"

# 图像理解 (本地图片, 自动转 base64 data URI)
$PY agnes_client.py "这张图里有什么?" --image /path/to/photo.jpg

# 工具调用
$PY agnes_client.py "上海现在天气怎么样?" --tools

# Thinking 模式
$PY agnes_client.py --thinking "9.11 和 9.8 谁大?"
```

## 语音助手

```bash
PY=/home/gxh/miniconda3/envs/vosk/bin/python

# 基本用法
$PY voice_assistant.py --wake "你好助手"

# 流式输出回复
$PY voice_assistant.py --wake "你好助手" --stream

# 多模态: 唤醒后同时拍摄像头画面一起送给模型
$PY voice_assistant.py --wake "你好助手" --camera 0

# 开启 Thinking
$PY voice_assistant.py --wake "你好助手" --thinking

# 用 wav 回放测试, 不占麦克风
$PY voice_assistant.py --replay test.wav --wake "你好助手"
```

### 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--wake` | `你好助手` | 唤醒词，多个用逗号分隔 |
| `--llm-model` | `.env` 的 `AGNES_MODEL` | Agnes 模型名 |
| `--system` | 内置中文助手提示词 | 系统提示词 |
| `--stream` | 关 | 流式输出回复 |
| `--thinking` | 关 | 开启 Thinking 模式 |
| `--camera` | 无 | 摄像头设备号，启用多模态提问 |
| `--speak` | 关 | 朗读模型回复 |
| `--history-turns` | `3` | 保留最近几轮上下文 |
| `--max-tokens` | `1024` | 最大输出 token |
| `--temperature` | `0.7` | 采样温度 |

其余参数（`--model` `--device` `--duration` `--silence` `--replay` 等）与 `wakeup_asr.py` 一致。

## 实现要点

### API 兼容层

Agnes 提供三种兼容接口，客户端封装的是 **Chat Completions**：

```
POST https://api.agnes-ai.cn/v1/chat/completions
Authorization: Bearer YOUR_API_KEY
```

也支持 `POST /v1/responses`（OpenAI Responses）和 `POST /v1/messages`（Anthropic Messages）。

### 图像输入

文档说图像需要「公开可访问的 URL」，但同时在错误码说明里提到
「对于图像，使用公共 URL 而不是发送大的 base64 数据」——实测 **base64 data URI 同样可用**，
所以客户端默认把本地图片转成 data URI，省去搭图床的麻烦：

```json
{
  "type": "image_url",
  "image_url": { "url": "data:image/jpeg;base64,..." }
}
```

`image_to_data_uri()` 会自动推断 MIME 类型。

### 工具调用

模型返回 `choices[].message.tool_calls`：

```bash
$PY agnes_client.py "上海现在天气怎么样?" --tools
[工具调用] get_weather 参数={"city": "上海"}
```

### 重试策略

文档列出 `429 / 5xx` 需要退避重试，客户端实现了指数退避（1s → 2s → 4s），
`4xx`（除 429）直接抛出，因为那是请求本身的问题。

**网络层异常（SSL EOF / 连接重置 / 超时）同样会重试**——这类故障是瞬时的，
之前只对 HTTP 状态码重试，导致 `SSLError: UNEXPECTED_EOF_WHILE_READING` 直接抛出。
现在会打印 `网络异常 (SSLError), 1s 后重试 (1/3)` 然后自动恢复。

另外用 `requests.Session` + `HTTPAdapter` 复用连接，减少 TLS 握手次数，
从根上降低 SSL 握手中断的概率。

实测 `agnes-2.5-flash` 经常返回 `503 The request queue is full`，
`agnes-3.0-flash` 稳定，所以 `.env` 默认用 `agnes-3.0-flash`。

### 流式编码

`requests` 的 `iter_lines(decode_unicode=True)` 会按 ISO-8859-1 解码导致中文乱码，
客户端显式设置 `resp.encoding = "utf-8"` 修正。

### 模型可用性

| 模型 | 状态 |
| --- | --- |
| `agnes-3.0-flash` | 可用，默认 |
| `agnes-2.5-flash` | 经常 503（上游队列满） |
| `agnes-2.5-pro` | 403 额度不足 |
| `agnes-image-2.5-flash` | 图像生成，可通过 `generate_image()` 调用 |

## 实测记录

测试环境：HP ZBook Power 15.6 (Linux)，麦克风 `sof-hda-dsp hw:0,7` (DMIC16kHz)。

| 测试 | 结果 |
| --- | --- |
| 文本对话 | 正常 |
| 流式输出 | 正常，中文无乱码 |
| 工具调用 | 正常，正确返回 `{"city": "上海"}` |
| Thinking 模式 | 正常 |
| 图像理解 (base64) | 正常 |
| 扬声器播放唤醒词 → 麦克风拾音 → 模型回复 | 成功，识别「今天天气怎么样」并得到回复 |
| 多模态（摄像头画面 + 语音） | 正常，回复结合了图像内容 |

## 已知限制

- `agnes-2.5-flash` 上游不稳定，建议用 `agnes-3.0-flash`
- `--speak` 需要系统装 TTS（`espeak-ng` / `espeak` / `pico2wave`），当前环境未安装，
  所以只打印文本
- 图像走 base64 时请求体会变大，Agnes 文档提示过大可能返回 `413`
- 免费账户有 RPM 限制（文档提到免费用户 20 RPM）
