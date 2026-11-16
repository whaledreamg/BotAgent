# LLM Provider 接入

Agent 的 LLM 走 **OpenAI 兼容协议**（`POST {base_url}/chat/completions` + Bearer 鉴权），
所以任何兼容这个协议的服务都能接进来：Agnes、DeepSeek、Qwen、Kimi、OpenAI、
Ollama、vLLM、SiliconFlow…… 只需要改配置，不用改代码。

> **不支持**：Anthropic（`/v1/messages`）、Gemini（`generateContent`）——
> 它们的报文和工具调用格式都不同，需要单独写适配器。

---

## 一、核心概念

配置分两层：

| 层 | 位置 | 作用 |
| --- | --- | --- |
| **provider 预设表** | `configs/agent.yaml` → `llm.providers` | 有哪些家可用，每家的地址/模型/密钥来源 |
| **角色绑定** | `configs/agent.yaml` → `llm.roles` | 哪个角色用哪家 |

三个角色：

| 角色 | 干什么 | 代码位置 |
| --- | --- | --- |
| `chat` | 闲聊回复 | `handlers/chat_handler.py`、`app.py` 流式接口 |
| `classify` | 意图分类（router 的 LLM 兜底） | `router.py: classify_llm()`、`handlers/device_handler.py` |
| `tool` | 原生 function calling | `router.py: _decide_by_tools()` |

**默认三个角色全部指向 `default_provider`（当前是 `agnes`）** —— 开箱即用就是现在的行为。
需要拆开时只改 yaml 一行。

---

## 二、默认配置

```yaml
llm:
  default_provider: agnes
  providers:
    agnes:
      base_url: https://api.agnes-ai.cn/v1
      model: agnes-2.5-flash
      api_key_env: AGNES_API_KEY
    deepseek:
      base_url: https://api.deepseek.com/v1
      model: deepseek-chat
      api_key_env: DEEPSEEK_API_KEY
    # ...
  roles:
    chat:     { provider: agnes }
    classify: { provider: agnes }
    tool:     { provider: agnes }
```

密钥放在 `configs/.env`（不入库），由 `api_key_env` 指向：

```
AGNES_API_KEY=sk-xxx
DEEPSEEK_API_KEY=sk-yyy
```

---

## 三、三种切换方式

### 1. 按角色拆开（推荐，最常用）

```yaml
roles:
  chat:     { provider: agnes }              # 聊天继续用 agnes
  classify: { provider: deepseek }           # 分类换一家（可以更快/更便宜）
  tool:     { provider: qwen, model: qwen-max }  # 工具调用换强模型
```

### 2. 换默认 provider

改 `llm.default_provider`，或者用环境变量：

```bash
LLM_PROVIDER=deepseek python app.py
```

### 3. 临时试某个模型（不�� yaml）

```bash
LLM_BASE_URL=https://api.deepseek.com/v1 \
LLM_MODEL=deepseek-chat \
LLM_API_KEY=sk-xxx \
python app.py
```

> **坑（老坑新坑）**：`LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` 仍然被支持，
> 但**只作用于 `default_provider`**。它们一旦存在就会覆盖那个 provider 在 yaml 里
> 写的地址和模型名。所以 `configs/.env` 里**故意不设**这三个 ——
> 否则把 `default_provider` 切成 deepseek 时，地址会被残留的 agnes 值覆盖，
> 出现「以为在测 DeepSeek，其实请求发给了 Agnes」。
>
> 覆盖发生时服务启动会打 warning：
> `[LLM] 环境变量 LLM_MODEL 覆盖了 agnes 的模型名 -> xxx`

---

## 四、provider 字段说明

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `base_url` | 必填 | OpenAI 兼容接口根地址，形如 `https://api.xxx.com/v1` |
| `model` | 必填 | 模型名 |
| `api_key` | 空 | 直接写在 yaml 里的密钥（本地服务用占位值即可） |
| `api_key_env` | 空 | 从哪个环境变量读密钥，**优先于** `api_key` |
| `timeout` | 全局 `llm.timeout` | 超时秒数 |
| `max_retries` | 全局 `llm.max_retries` | 单个 provider 内的重试次数 |
| `temperature` | 全局 `llm.temperature` | 采样温度 |
| `max_tokens` | 全局 `llm.max_tokens` | 生成长度上限 |
| `supports_tools` | `true` | 是否支持原生 function calling。不支持就填 `false` |
| `supports_stream` | `true` | 是否支持流式 |
| `notes` | 空 | 备注，如「本地服务, 无需真实 key」 |

**密钥解析顺序**：`api_key`（yaml）→ `api_key_env` 指定的环境变量
→ `<NAME>_API_KEY` → `LLM_API_KEY`。

**无鉴权服务**（Ollama / vLLM）：`api_key` 留空即可，客户端不会发
`Authorization` 头。想让探测脚本识别本地服务，配一个占位值也行。

---

## 五、故障降级

每个角色的候选链 = **主 provider** + `fallback` 列表。链上某个 provider 失败时
自动切到下一个：

| 情况 | 行为 |
| --- | --- |
| HTTP 429（限流） | 该 provider 内按 `Retry-After` / 指数退避重试，耗尽后切下一个 |
| HTTP 5xx | 同上 |
| HTTP 401/403（鉴权错） | **不重试**，直接切下一个 |
| 网络错误 / 超时 | 同上 |
| 其它 4xx（如 400 参数错） | **不重试**，直接抛出 —— 这是我们自己的问题，换家也没用 |

流式接口（`/v1/chat/stream`）**不支持降级** —— 已经吐出第一个字就没法换 provider 了。

当前 `fallback` 全部留空，只有 agnes 一家在服务，不会误切到没配 key 的 provider。
接入第二家后再按需开启：

```yaml
roles:
  chat: { provider: agnes, fallback: [deepseek] }
```

> 降级会引入额外延迟（切 provider 需要重试一次）。语音场景建议 `fallback` 链
> 最多 2 个，且只在主 provider 失败时触发。

---

## 六、探测与测试

### 探测各家连通性

```bash
conda run -n botagent python tools/check_providers.py --list
conda run -n botagent python tools/check_providers.py --provider agnes
conda run -n botagent python tools/check_providers.py --role tool
```

输出示例：

```
=== 角色绑定 ===
  chat      -> agnes/agnes-2.5-flash
  classify  -> agnes/agnes-2.5-flash
  tool      -> agnes/agnes-2.5-flash

=== provider 探测 ===
  [OK ] agnes      model=agnes-2.5-flash
         base=https://api.agnes-ai.cn/v1
         chat: 673ms  '你好，我是Agnes，很高兴认识你！'
         tools: 414ms  支持 — 调用了 ['get_time']
```

没配 key 的 provider 会自动跳过（不会拿 401 刷屏）。

### 测延迟

```bash
conda run -n botagent python tools/bench_llm.py --times 5
```

按角色分别打点 —— 三个角色可能绑到不同 provider，混在一起测没意义。

### 确认当前生效配置

```bash
curl localhost:8800/health
```

```json
{
  "status": "ok",
  "model": "agnes-2.5-flash",
  "base_url": "https://api.agnes-ai.cn/v1",
  "default_provider": "agnes",
  "providers": ["agnes", "deepseek", "qwen", "kimi", "openai", "ollama"],
  "roles": {
    "chat":     { "provider": "agnes", "model": "agnes-2.5-flash", "fallback": [] },
    "classify": { "provider": "agnes", "model": "agnes-2.5-flash", "fallback": [] },
    "tool":     { "provider": "agnes", "model": "agnes-2.5-flash", "fallback": [] }
  }
}
```

**这个接口是排查「切换没生效」的第一站** —— 它打印的是**最终生效**的
provider 和模型名，不受环境变量覆盖的影响。

---

## 七、常见坑

### 切换没生效

1. `configs/.env` 里残留了 `LLM_MODEL` / `LLM_BASE_URL` → 删掉（见第三节）
2. 改的是 `router.llm_fallback_model` 而不是 `llm.roles.classify` ——
   前者是 deprecated，只在 `llm.roles.classify` 没显式配模型时才生效
3. 服务没重启 → 改完 yaml 必须重启
4. 看 `/health` 确认最终生效值

### 「三个角色用的不是同一个模型」

这是**故意的** —— 之前的三处模型名各自独立（`llm.model` / `router.llm_fallback_model` /
`router.tool_calling.model`），改一处漏两处就会「测出来的效果是混的」。
现在统一由 `llm.roles` 收敛，默认三者一致。

### 模型不支持 function calling

会收到 HTTP 400。两种处理：

1. 在 provider 预设里标 `supports_tools: false` —— 此时 `role=tool` 会跳过它，
   不会把 `tools` 参数发给它
2. 换一家

### 限流 429

免费额度下很常见。表现是「工具调用重试失败」，但根因是被限流。
客户端已经按 `(attempt+1) * 2.5s` 退避并尊重 `Retry-After`。
仍然频繁限流就配 `fallback`。

### 本地模型（Ollama）

```yaml
ollama:
  base_url: http://127.0.0.1:11434/v1
  model: qwen2.5:7b
  api_key: ollama        # 占位值, 让客户端带上 Authorization 头
  notes: 本地服务, 无需真实 key
```

---

## 八、代码位置

| 文件 | 职责 |
| --- | --- |
| `services/agent_service/config.py` | `ProviderConfig` / `RoleConfig` / `LLMConfig` 容器 + `load_config` 解析 |
| `services/agent_service/llm.py` | provider 客户端池、`resolve()` 角色解析、跨 provider 降级 |
| `services/agent_service/tools/check_providers.py` | 连通性与能力探测 |
| `services/agent_service/tools/bench_llm.py` | 按角色测延迟 |
