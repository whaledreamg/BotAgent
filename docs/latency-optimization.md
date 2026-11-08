# 延迟优化记录

记录 2026-11-07 ~ 11-08 两次端到端响应慢的排查与优化过程。

## 一、结论速览

### 第一轮（2026-11-07）：全链路

| 指标 | 优化前 | 优化后 | 变化 |
| --- | --- | --- | --- |
| **端到端** | **50940ms** | **18375ms** | **-64%** |
| 语音 → 文字 | 15328ms | 12281ms | -20%（含用户说话时长） |
| Agent 分流 | 3002ms | 498ms | -83% |
| Agent 生成 | 10717ms | 677ms | **-94%** |
| Agent 合计 | 13723ms | 1179ms | **-91%** |
| TTS 首帧 | 9303ms | 465ms | **-95%** |
| TTS 合成 | 12474ms | 4337ms | -65% |
| 文字 → 语音 | 40372ms | 6094ms | **-85%** |

### 第二轮（2026-11-08）：TTS 合成仍然慢

| 指标 | 优化前 | 优化后 | 变化 |
| --- | --- | --- | --- |
| TTS 首帧（稳态） | ~4600ms | **~350ms** | **-92%** |
| TTS 首帧（首个请求） | 3529ms | 430ms | **-88%** |
| TTS 平均首帧 | 2135ms | 447ms | **-79%** |

三个原因，按贡献排序：

1. **末字截断重试** —— 净负收益，开着让首帧翻 4~9 倍 → 已默认关闭
2. **前导静音裁剪死等 600ms lookahead** → 改成自适应提前放行
3. **首个请求承担 CUDA + NeMo 懒加载**（~3.4s） → 挪到启动时的 warmup

网络开销始终 < 10ms，不是瓶颈。

---

## 二、排查方法

### 2.1 打点位置

每一环都打了分段耗时，端到端汇总在 `services/asr_service/pipeline.py`：

```
[全链路计时] 语音->文字 12281ms | Agent 1179ms (服务端 1176ms) | TTS 4914ms
             | 文字->语音 6094ms | 端到端 18375ms
```

| 环节 | 日志标签 | 文件 |
| --- | --- | --- |
| ASR 录音+识别 | `[计时] ASR 录音+识别阶段` | `services/asr_service/server.py` |
| Agent 网络往返 | `[Agent] 往返 Xms (服务端 Yms)` | `services/asr_service/agent_client.py` |
| Agent 分流/分支 | `[计时] 总计/分流/分支执行` | `services/agent_service/app.py` |
| 上游 LLM | `[LLM] Xms model=... tokens=...` | `services/agent_service/llm.py` |
| TTS 客户端 | `[TTS] 总计/连接/首帧/合成/播放收尾` | `services/asr_service/tts_client.py` |
| TTS 服务端 | `[TTS-服务端] 总计/首帧/队列等待` | `services/tts_service/examples/ws_server/main.py` |

`/state` 接口也带上 `timings` 字段，便于程序化取数。

### 2.2 分段压测工具

`services/asr_service/tools/profile_latency.py` 把链路拆开单独测：

```bash
cd "Bot Agent/services/asr_service"
conda run -n asrt python tools/profile_latency.py
```

输出带横条图，宽度即耗时占比，一眼看出瓶颈。

---

## 三、发现的问题与修复

### 问题 1：上游模型首字延迟剧烈波动（最大收益）

**现象**

```
[LLM] 10717ms model=agnes-3.0-flash tokens=125/37
```

37 个 token 花了 10.7 秒 ≈ 290ms/token。进一步压测三个模型各 3 次：

| 模型 | TTFT 抽样 | 总耗时抽样 |
| --- | --- | --- |
| `agnes-3.0-flash` | 1516ms / **33078ms** / 15171ms | 2.1s / 34.0s / 15.8s |
| `agnes-2.5-flash` | 395ms / 468ms / 450ms | 653ms / 705ms / 854ms |
| `agnes-2.5-pro` | 403（无权限） | — |

`agnes-3.0-flash` 的延迟不是「慢」，而是**随机**（1.5s ~ 33s），
无法通过调参改善。

**修复**：默认模型切到 `agnes-2.5-flash`。

> **踩坑**：`configs/.env` 里的 `LLM_MODEL` 会覆盖 `configs/agent.yaml` 的 `llm.model`。
> 第一次只改了 YAML，服务日志仍显示 `model=agnes-3.0-flash`，排查后才发现。
> **改完 YAML 一定要同步改 `.env`。**

### 问题 2：TTS「流式」其实是整段阻塞生成

**现象**

```
[TTS] 总计 21889ms | 连接 4ms | 首帧 9303ms | 合成 12474ms | 音频 12523ms
[TTS-服务端] 总计 9302ms | 首帧 9301ms | 队列等待 0ms
```

首帧 9.3 秒 —— 用户要干等 9 秒才听到第一个字。服务端「首帧」几乎等于「总耗时」，
说明**整段音频生成完才推出来**。

**根因**：`engine.synthesize()` 里为了判定「末字是否被 EOS 截断」，
无论文本长短都先 `list()` 把整个生成器缓冲完再 yield：

```python
chunks = list(self._synthesize_once(...))   # ← 缓冲整段，流式退化
```

这个「末字重试」逻辑是为短文本设计的（`RETRY_MAX_CHARS = 20`），
但代码对所有文本都走了这条路径。

**修复**：只在确实需要重试判定时才缓冲；并加一道时间闸。

```python
needs_retry_check = (
    retry_on_truncation
    and AUTO_PUNCTUATE
    and len(prepared_text) <= RETRY_MAX_CHARS
    and elapsed_ms < RETRY_TIME_BUDGET_MS   # 新增: 2500ms 预算
)
```

时间闸的作用：重试要重新生成整段音频，代价很高。
只有当这一轮已经花掉的时间还在预算内，才值得再试；
超了就直接用当前结果（末字略有截断，远好过让用户多等几秒）。

**效果**：首帧 9303ms → **465ms**。

### 问题 3：路由规则命中后还要花 1~3 秒做 LLM 二次确认

**现象**

```
trace: rules:0.0ms llm:3000.8ms default:0.0ms llm:12312.4ms
```

「今天天气怎么样」命中了 `exclude: ["怎么样"]` 排除词 → 规则不生效 →
走 LLM 分类 → **卡满 `llm_timeout: 3.0s` 整** → 超时回落 chat。

规则命中时，router 还会调 `handler.classify()` 让分支自己确认，
chat 分支的 `classify()` 恒返回 1.0，但这次确认本身不打 LLM ——
真正的 3 秒是 LLM 分类本身卡满超时。

**修复**：

1. `llm_timeout` 从 3.0s 降到 **1.2s**（分类只需几十个 token，1.2s 足够）
2. 新增 `router.trust_rules`（默认 `true`）：规则命中就直接采信，
   跳过 LLM 分类和二次确认
3. 规则分类改用**流式**（新增 `json_completion_stream`）

**效果**：

| 输入 | 修复前 | 修复后 |
| --- | --- | --- |
| 打开客厅的灯（规则命中） | 3ms（但走了 LLM 确认） | **3ms**（`trust` 跳过 LLM） |
| 今天天气怎么样（规则未命中） | 15314ms | **967ms** |

### 问题 4：回复过长直接放大 TTS 耗时

CPU 上 ChatTTS 合成速度约 **1 字 / 0.19 秒**。回复 67 字 → 音频 12.5 秒。

**修复**：

- `llm.max_tokens` 从 2048 降到 **256**
- 系统提示词明确「总长度不超过 40 个字，通常只说一到两句话」
- `llm.timeout` 从 60s 降到 **20s**（超时快速回落，不让用户干等）

### 问题 5：脚本的端口回收有漏洞，导致「配置改了没效果」

**现象**：改完模型配置重启，日志里还是旧模型。

**根因**：`scripts/stop_all.sh` 只按 pid 文件回收。
但 `conda run` 会多 fork 一层，子进程 pid 与 pid 文件里的不一致，
于是旧进程活下来占着端口；新进程绑定失败退出，
而健康检查查的是**旧进程**的端口，所以照样「就绪」。

**修复**：

- `stop_all.sh` 增加**按端口**回收（`ss -ltnpH` 取 pid），结束时明确报告端口状态
- `start_all.sh` 启动前检查端口占用，**直接报错退出**，不再静默降级

```
[error] agent 端口 8800 已被占用, 请先执行 scripts/stop_all.sh
```

### 问题 6：末字截断重试 —— 净负收益，首帧翻 4~9 倍

**现象**（2026-11-08）：

```
[TTS] 总计 8806ms | 连接 7ms | 首帧 5274ms | 合成 3451ms
[TTSEngine] tail truncated (decay=50ms < 110ms), retry #1 (spent=2178ms)
[TTSEngine] tail still truncated (decay=80ms, spent=4454ms), 不再重试
```

服务端日志里**大量** `retry #1` —— 引擎在把整段音频合成两遍。

**根因**：「末字截断重试」这个功能本身是净负收益。实测
（每条文本跑不重试/开重试各一次，对比首帧延迟与末音节衰减）：

| 文本 | 不重试首帧 | 开重试首帧 | 代价 | 重试是否修好 |
| --- | --- | --- | --- | --- |
| 北京今天天气晴，气温 24 度 | 749ms | 4250ms | 5.67x | ❌ 仍截断 |
| 请告诉我你在哪个城市 | 644ms | 1566ms | 2.43x | ✅ 修好 |
| 没听清内容，请再说一遍 | 643ms | 2966ms | 4.61x | ❌ **弄坏了** |
| 建议您打开手机自带的天气应用查看 | 560ms | 4879ms | 8.71x | ❌ 仍截断 |

**汇总：平均首帧 649ms → 3415ms（+2766ms），修好 1/4 次，反而弄坏 1/4 次。**

换随机种子重采样对「末字」只有约 25% 的命中率，而且重采样本身有随机性，
会把原本正常的末字弄坏。

**为什么会误判**：`tail_decay_ms` 原来测的是「末音节峰值衰减到**10%**」所需时间。
但韵尾（`-i`/`-u`/`-n` 等浊韵尾）是渐弱的，振幅降到 10% 之后还会再拖
50~150ms 才归零 —— 这段尾巴**是听得见的**。同一批音频换不同判定比例：

| 文本 | 10% | 5% | 3% | 2% |
| --- | --- | --- | --- | --- |
| 你好，今天天气不错。 | 75 | 75 | 80 | 80 |
| 北京今天天气晴，气温 24 度。 | 55 | 55 | 55 | 60 |
| 请告诉我你在哪个城市。 | 30 | 225 | 225 | 395 |
| 建议您打开手机自带的天气应用查看。 | 45 | 60 | 220 | 225 |

「请告诉我你在哪个城市。」在 10% 下只有 30ms（判为截断），在 2% 下有 395ms
（判为正常）—— 同一段音频，结论完全相反。

**修复**（两处）：

1. `tail_decay_ms` 的结束比例从 10% 放宽到 **2%**（`TAIL_DECAY_END_RATIO`）
2. 重试**默认关闭**（`RETRY_ON_TRUNCATION`，可用环境变量 `TTS_RETRY_ON_TRUNCATION=1` 打开）

语音场景对延迟极敏感，宁可末字偶尔收得急一点，也不要让用户多等 3 秒。

### 问题 7：前导静音裁剪死等 600ms lookahead

**现象**：GPU 只用 125~153ms 就能产出第一个音频块，但首帧却是 650ms。

**根因**：裁剪前导静音要等攒够 600ms 音频才能判断（ChatTTS 固定带
240~400ms 前导静音），于是**每块都在等 lookahead 攒满**，凭空多出几百毫秒。

**修复**：改成自适应提前放行 —— 每收到一块就检查「是否已经进入发声段」，
一旦确认就立刻裁剪输出，不再等满 lookahead：

```python
if pending.size < lookahead and is_all_silence(pending):
    continue
arr = trim_leading_silence(pending)
```

**效果**：首帧 651ms → **333ms**。

### 问题 8：第一个请求承担 CUDA + NeMo 懒加载

**现象**：服务起来后的第一个请求要 3529ms，之后只要 ~400ms。

**根因**：两处懒加载
- NeMo 文本归一化：首次 2659ms，之后 2~3ms
- CUDA kernel 首次 JIT 编译与显存分配

**修复**：`TTSEngine.load()` 里加 `_warmup()`，用一段极短文本跑完整条推理链路，
把这些开销一次性吃掉（实测 3377ms，发生在启动阶段而非首个请求）。

**效果**：首个请求 3529ms → **430ms**。

---

## 四、剩余瓶颈

**ChatTTS 跑在 CPU 上**（未解决，需要硬件）：

```
ChatTTS | gpu | no GPU or NPU found, use CPU instead
```

CPU 上合成速度约等于实时（RTF≈0.74），音频 12 秒就要合成 12 秒。
上 GPU 后首帧与总时长可再降一个量级。

> 2026-11-08 更新：这台机器已经跑在 GPU 上（`device=cuda:0`，RTX A500），
> 所以上面的 CPU 限制不再是瓶颈。GPU 上 RTF≈0.84，首帧已压到 ~350ms。
> 下面这条保留作为「没有 GPU 时」的性能基线参考。

**ASR 阶段 12.3 秒**里绝大部分是用户实际说话时长（16.75s 音频），
vosk 纯计算 RTF=0.21，不是系统开销。若还想更快，
可调 `configs/pipeline.yaml` 的 `asr.silence_timeout`（当前 1.2s）。

---

## 五、最终配置

### `configs/agent.yaml`

```yaml
llm:
  model: agnes-2.5-flash
  timeout: 20
  max_retries: 2
  max_tokens: 256

router:
  mode: hybrid
  llm_fallback_model: agnes-2.5-flash
  llm_timeout: 1.2
  trust_rules: true
```

### `configs/.env`

```
LLM_MODEL=agnes-2.5-flash   # 会覆盖 YAML, 必须同步改
```

### `services/tts_service/examples/ws_server/engine.py`

```python
# 末字截断重试: 默认关闭 (实测净负收益, 首帧 +2766ms)
RETRY_ON_TRUNCATION = os.environ.get("TTS_RETRY_ON_TRUNCATION", "0") ...
# 末音节衰减判定: 结束比例从 10% 放宽到 2%, 避免误判
TAIL_DECAY_END_RATIO = 0.02
TAIL_DECAY_MIN_MS = 110
# 需要时仍可用这两项限制重试的行为
RETRY_MAX_CHARS = 20
RETRY_TIME_BUDGET_MS = 2500   # 重试时间闸
```

### `services/tts_service/examples/ws_server/audio.py`

```python
# 前导静音裁剪的自适应提前放行 (不再死等 600ms lookahead)
def is_all_silence(wav, threshold=SILENCE_THRESH) -> bool: ...
```

---

## 六、后续可选优化

1. **LLM 流式 + TTS 边生成边合成** —— 把 Agent 与 TTS 从串行改并行，
   当前回复只有 15~40 token，收益有限；回复长时才有明显价值
2. **缩短 `silence_timeout`** —— 1.2s → 0.6~0.8s
3. **打断（barge-in）** —— WS 协议已支持 `stop` 报文，接入成本低
4. **降低 `stream_batch`** —— 当前 8（每块约 160ms 音频）。调到 4 可以让
   首帧再快几十毫秒，代价是块数变多、WS 帧开销变大

---

## 七、怎么验证

```bash
# 分阶段耗时 + 重试开/关对比 (会加载模型, 约 20s)
cd "Bot Agent/services/tts_service"
conda run -n chattts python tools/profile_tts.py "北京今天天气晴，气温 24 度"

# 走真实 WS 服务的首帧延迟 (不需要加载模型)
cd "Bot Agent/services/asr_service"
/home/gxh/miniconda3/envs/asrt/bin/python tools/bench_tts_ws.py
```

`profile_tts.py` 的输出里，`retry=True` 和 `retry=False` 的首帧差距
就是重试的真实代价。
