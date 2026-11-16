"""OpenAI 兼容接口封装。

支持:
- chat():  普通对话, 返回文本
- stream(): 流式对话, 逐块 yield 增量
- json_completion(): 要求模型输出 JSON (用于语义识别的意图分类)
- tool_call(): 带 tools 的原生 function calling, 返回结构化的工具调用

**多 provider**: 按角色 (chat / classify / tool) 解析出一条候选链 ——
主 provider 在前, `fallback` 依次在后。某个 provider 失败 (限流/超时/
鉴权错) 时自动切到下一个候选。见 `resolve()`。

实现只用 httpx, 不依赖 openai SDK, 便于在轻量环境里运行。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import httpx

from config import LLMConfig

logger = logging.getLogger("agent.llm")


def _debug_enabled() -> bool:
    """是否打印 LLM 请求/响应全文。

    默认关。开了以后 `logs/agent.log` 里会出现完整的 prompt 和模型返回,
    用来排查「模型为什么这么答 / 为什么没调工具」。
    """
    return os.environ.get("LLM_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def _debug_max_chars() -> int:
    try:
        return int(os.environ.get("LLM_DEBUG_MAX_CHARS", "4000"))
    except ValueError:
        return 4000


def _dump(label: str, obj: Any) -> None:
    """把一个对象漂亮地打出来 (超长截断)。"""
    if isinstance(obj, str):
        text = obj
    else:
        try:
            text = json.dumps(obj, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            text = repr(obj)
    limit = _debug_max_chars()
    if limit > 0 and len(text) > limit:
        text = text[:limit] + f"\n... [截断, 共 {len(text)} 字符]"
    logger.info("[LLM-DEBUG] %s\n%s", label, text)


class LLMError(RuntimeError):
    """LLM 调用失败。"""


@dataclass
class ToolCall:
    """一次工具调用。"""

    #: 模型给的调用 id (回灌结果时要用)
    id: str = ""
    #: function 名
    name: str = ""
    #: 已解析的参数 (模型给的是 JSON 字符串)
    arguments: Dict[str, Any] = field(default_factory=dict)
    #: 原始的 arguments 字符串 (解析失败时保留, 便于排查)
    raw_arguments: str = ""


@dataclass
class ToolCallResult:
    """带 tools 的一次调用结果。"""

    #: 模型生成的文本 (没有调工具时就是回复正文)
    content: str = ""
    #: 模型请求调用的工具
    calls: List[ToolCall] = field(default_factory=list)
    #: finish_reason
    finish_reason: str = ""
    #: 用量统计
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: 实际服务的 provider 预设名 (降级时可能是备用那家)
    provider: str = ""
    #: 实际使用的模型名
    model: str = ""

    @property
    def has_calls(self) -> bool:
        return bool(self.calls)


#: httpx 不认识的代理协议 (如 socks://)。出现这类环境变量时 httpx 会直接抛
#: ValueError, 导致整个请求 500, 所以这里先把它们摘掉。
_BAD_PROXY_SCHEMES = ("socks://", "socks4://", "socks5://", "socks4a://", "socks5h://")
_PROXY_ENV_KEYS = (
    "all_proxy", "ALL_PROXY",
    "http_proxy", "HTTP_PROXY",
    "https_proxy", "HTTPS_PROXY",
    "socks_proxy", "SOCKS_PROXY",
    "socks5_proxy", "SOCKS5_PROXY",
)


def _sanitize_proxy_env() -> List[str]:
    """移除 httpx 不支持的代理环境变量, 返回被摘掉的变量名。

    只在进程内删除 os.environ 里的值, 不改动调用者的 shell 环境。
    """
    removed: List[str] = []
    for key in _PROXY_ENV_KEYS:
        value = os.environ.get(key)
        if not value:
            continue
        if value.lower().startswith(_BAD_PROXY_SCHEMES):
            os.environ.pop(key, None)
            removed.append(f"{key}={value}")
    return removed


def _extract_json(text: str) -> Optional[Any]:
    """从模型输出里抠出 JSON。

    优先直接 parse; 失败则尝试提取 ```json 代码块, 或找第一个平衡的 {...}。
    """
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


@dataclass
class Target:
    """一次请求的具体落点: 用哪家 provider 的哪个模型。"""

    #: provider 预设名
    provider: str = ""
    #: 模型名
    model: str = ""
    #: 温度 (None = 用 provider 的默认)
    temperature: Optional[float] = None
    #: 生成长度上限 (0 = 用 provider 的默认)
    max_tokens: int = 0
    #: 该 provider 的配置
    provider_cfg: Any = None

    def resolved_temperature(self, fallback: float) -> float:
        if self.temperature is not None:
            return self.temperature
        if self.provider_cfg is not None and self.provider_cfg.temperature is not None:
            return self.provider_cfg.temperature
        return fallback

    def resolved_max_tokens(self, fallback: int) -> int:
        if self.max_tokens:
            return self.max_tokens
        if self.provider_cfg is not None and self.provider_cfg.max_tokens:
            return self.provider_cfg.max_tokens
        return fallback


class LLMClient:
    """异步 LLM 客户端。

    **多 provider**: 内部按 provider 名缓存 httpx 客户端 (各家 base_url /
    timeout 不同), 并按**角色** (chat / classify / tool) 解析出一条候选链:
    主 provider 在前, `fallback` 依次在后。某个 provider 失败 (限流/超时/
    鉴权错) 时自动切到下一个候选。
    """

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        #: provider 名 -> httpx client
        self._clients: Dict[str, httpx.AsyncClient] = {}
        self._proxy_cleaned = False

    # ------------------------------------------------------------- 角色解析

    def _providers(self) -> Dict[str, Any]:
        """取 provider 表; 老配置 (只有扁平字段) 兜底合成一个。"""
        providers = getattr(self.cfg, "providers", None) or {}
        if providers:
            return providers
        # 兼容旧构造方式
        from config import ProviderConfig

        return {
            "default": ProviderConfig(
                name="default",
                base_url=self.cfg.base_url,
                model=self.cfg.model,
                api_key=self.cfg.api_key,
                timeout=self.cfg.timeout,
                max_retries=self.cfg.max_retries,
                temperature=self.cfg.temperature,
                max_tokens=self.cfg.max_tokens,
            )
        }

    def resolve(
        self,
        role: Optional[str] = None,
        model: Optional[str] = None,
    ) -> List[Target]:
        """把角色解析成有序候选链。

        - role 未指定 -> 用 `chat`
        - 角色没配 -> 用 default_provider
        - `model` 参数可在链内指定模型名 (找不到就用该 provider 的默认模型)
        - `supports_tools=False` 的 provider 在 role=tool 时跳过
        """
        providers = self._providers()
        role_name = role or "chat"
        rc = self.cfg.role(role_name) if hasattr(self.cfg, "role") else None

        primary = (rc.provider if rc else "") or getattr(self.cfg, "default_provider", "") or ""
        # 显式传入的 model 优先级最高, 其次是角色配置的 model
        model_override = model or ""
        fallbacks: List[str] = []
        if rc is not None:
            model_override = model_override or rc.model or ""
            fallbacks = list(rc.fallback or [])

        chain: List[Target] = []
        seen = set()

        def _add(pname: str, model: str) -> None:
            pcfg = providers.get(pname)
            if pcfg is None:
                return
            if role_name == "tool" and not getattr(pcfg, "supports_tools", True):
                logger.warning(
                    "[LLM] provider %s 不支持 function calling, role=tool 跳过", pname
                )
                return
            model_name = model or pcfg.model
            key = (pname, model_name)
            if key in seen:
                return
            seen.add(key)
            chain.append(
                Target(
                    provider=pname,
                    model=model_name,
                    temperature=(rc.temperature if rc else None),
                    max_tokens=(rc.max_tokens if rc else 0),
                    provider_cfg=pcfg,
                )
            )

        _add(primary, model_override)
        for pname in fallbacks:
            _add(pname, model_override)

        if not chain:
            # 兜底: 用第一个可用 provider, 保证至少有一个候选
            for pname, pcfg in providers.items():
                _add(pname, model_override)
                if chain:
                    break
        return chain

    # ------------------------------------------------------------- 连接管理

    async def _get_client(self, provider: str) -> httpx.AsyncClient:
        """取 (或建) 某个 provider 的 httpx 客户端。"""
        if provider in self._clients:
            return self._clients[provider]

        if not self._proxy_cleaned:
            removed = _sanitize_proxy_env()
            if removed:
                logger.warning("已忽略不受支持的代理环境变量: %s", ", ".join(removed))
            self._proxy_cleaned = True

        pcfg = self._providers()[provider]
        headers = {"Content-Type": "application/json"}
        # 无鉴权的本地服务 (Ollama / vLLM) 不发 Authorization 头
        if pcfg.api_key:
            headers["Authorization"] = f"Bearer {pcfg.api_key}"

        client = httpx.AsyncClient(
            base_url=pcfg.base_url,
            timeout=pcfg.timeout or self.cfg.timeout,
            headers=headers,
        )
        self._clients[provider] = client
        return client

    async def aclose(self) -> None:
        for client in list(self._clients.values()):
            try:
                await client.aclose()
            except Exception:  # pragma: no cover
                pass
        self._clients.clear()

    def _headers(self, provider: str) -> Dict[str, str]:
        pcfg = self._providers()[provider]
        headers = {"Content-Type": "application/json"}
        if pcfg.api_key:
            headers["Authorization"] = f"Bearer {pcfg.api_key}"
        return headers

    # ------------------------------------------------------------- 内部

    async def _post_one(
        self,
        provider: str,
        path: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """在**单个 provider 内**重试。

        返回解析好的响应 dict; 失败抛 LLMError (由调用方决定是否降级)。
        401/403 不重试 —— 那是配置问题, 再试多少次都是一样的。
        """
        pcfg = self._providers()[provider]
        if not pcfg.base_url:
            raise LLMError(f"provider {provider} 未配置 base_url")

        last_err: Optional[Exception] = None
        retries = max(1, pcfg.max_retries or self.cfg.max_retries)
        for attempt in range(retries):
            try:
                if _debug_enabled():
                    _dump(f"请求 -> {provider}{path}", payload)
                client = await self._get_client(provider)
                resp = await client.post(
                    path, json=payload, headers=self._headers(provider)
                )
            except httpx.TimeoutException as exc:
                last_err = LLMError(f"网络请求超时: {exc}")
            except httpx.HTTPError as exc:
                last_err = LLMError(f"网络请求失败: {exc}")
            except LLMError:
                raise
            except Exception as exc:
                # 兜底: 代理配置之类的意外异常不能冒泡成 500
                logger.exception("LLM 请求异常")
                last_err = LLMError(f"请求失败: {exc}")
            else:
                if _debug_enabled():
                    _dump(
                        f"响应 <- {provider}{path} (HTTP {resp.status_code})",
                        resp.text,
                    )
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        last_err = LLMError(f"响应不是合法 JSON: {exc}")
                elif resp.status_code in (401, 403):
                    # 鉴权错误重试无意义 —— 直接让上层降级
                    raise LLMError(
                        f"provider {provider} 鉴权失败 HTTP {resp.status_code}: "
                        f"{resp.text[:200]}"
                    )
                elif resp.status_code == 429:
                    # **速率限制** —— 免费额度下很常见。
                    #
                    # 关键: 429 必须等**更久**。之前用的 2**attempt 秒
                    # (1s) 对限流窗口来说太短, 重试必然再撞 429, 表现
                    # 就是「required 重试失败」—— 但根因不是重试逻辑有问题,
                    # 而是被限流了。
                    #
                    # 这里按 (attempt+1) * 2.5 秒退避, 并且尊重服务端
                    # 给的 Retry-After (如果有)。
                    wait = self._retry_after(resp, attempt)
                    last_err = LLMError(
                        f"provider {provider} HTTP 429 速率限制: {resp.text[:200]}"
                    )
                    if attempt < retries - 1:
                        logger.warning(
                            "provider %s 触发速率限制, %.1fs 后重试 (%d/%d)",
                            provider, wait, attempt + 1, retries,
                        )
                        await _sleep(wait)
                        continue
                elif resp.status_code in (500, 502, 503, 504):
                    last_err = LLMError(
                        f"provider {provider} HTTP {resp.status_code}: {resp.text[:500]}"
                    )
                else:
                    # 其它 4xx (如 400 参数错误) 是我们自己的问题, 重试无意义
                    raise LLMError(
                        f"provider {provider} HTTP {resp.status_code}: {resp.text[:500]}"
                    )
            if attempt < retries - 1:
                await _sleep(2 ** attempt)
        raise last_err or LLMError("请求失败")

    async def _post(
        self,
        path: str,
        payload: Dict[str, Any],
        targets: List[Target],
        role: str,
    ) -> Tuple[Dict[str, Any], Target]:
        """按候选链发请求, 失败自动降级到下一个 provider。

        返回 (响应, 实际生效的 target)。
        """
        if not targets:
            raise LLMError(f"角色 {role!r} 没有可用的 provider")

        last_err: Optional[Exception] = None
        for idx, target in enumerate(targets):
            if idx > 0:
                logger.warning(
                    "[LLM] 降级到备用 provider %s (model=%s, role=%s)",
                    target.provider, target.model, role,
                )
            body = dict(payload)
            body["model"] = target.model
            try:
                data = await self._post_one(target.provider, path, body)
            except LLMError as exc:
                last_err = exc
                logger.warning(
                    "[LLM] provider %s 失败: %s", target.provider, exc
                )
                continue
            return data, target

        raise last_err or LLMError(f"角色 {role!r} 的所有 provider 都失败")

    @staticmethod
    def _retry_after(resp: httpx.Response, attempt: int) -> float:
        """429 之后该等多久。

        优先用服务端的 Retry-After 头; 没有就用指数退避 (2.5s / 5s / ...)。
        """
        header = resp.headers.get("Retry-After")
        if header:
            try:
                return max(0.5, float(header))
            except (TypeError, ValueError):
                pass
        return 2.5 * (attempt + 1)

    # ------------------------------------------------------------- 公开

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        role: Optional[str] = None,
    ) -> str:
        role_name = role or "chat"
        targets = self.resolve(role_name, model=model)
        if not targets:
            raise LLMError(f"角色 {role_name!r} 没有可用的 provider")
        head = targets[0]
        payload: Dict[str, Any] = {
            "model": head.model,
            "messages": messages,
            "max_tokens": max_tokens or head.resolved_max_tokens(self.cfg.max_tokens),
            "temperature": (
                head.resolved_temperature(self.cfg.temperature)
                if temperature is None
                else temperature
            ),
        }
        if tools:
            payload["tools"] = tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
        t0 = time.time()
        data, used = await self._post("/chat/completions", payload, targets, role_name)
        elapsed_ms = int((time.time() - t0) * 1000)
        try:
            content = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"响应格式异常: {exc}") from exc
        usage = data.get("usage") or {}
        logger.info(
            "[LLM] %dms provider=%s model=%s role=%s tokens=%s/%s",
            elapsed_ms,
            used.provider,
            used.model,
            role_name,
            usage.get("prompt_tokens", "?"),
            usage.get("completion_tokens", "?"),
        )
        return content

    async def tool_call(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        tool_choice: Optional[Any] = "auto",
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        role: Optional[str] = None,
        thinking: Optional[Dict[str, Any]] = None,
    ) -> ToolCallResult:
        """带 tools 的原生 function calling。

        模型要么返回 `tool_calls` (要执行动作), 要么返回普通文本
        (就是闲聊回复)。两种结果都在这里解析好, 调用方只看
        `has_calls` 决定走哪条路。
        
        Args:
            thinking: Thinking Mode 配置 (用于改进意图理解)
                      格式: {"type": "enabled", "budget_tokens": 8000}
        """
        if not tools:
            raise LLMError("tool_call 需要至少一个 tool 定义")

        role_name = role or "tool"
        targets = self.resolve(role_name, model=model)
        if not targets:
            raise LLMError(f"角色 {role_name!r} 没有可用的 provider")
        head = targets[0]
        payload: Dict[str, Any] = {
            "model": head.model,
            "messages": messages,
            "max_tokens": max_tokens or head.resolved_max_tokens(self.cfg.max_tokens),
            "temperature": (
                head.resolved_temperature(self.cfg.temperature)
                if temperature is None
                else temperature
            ),
            "tools": tools,
        }
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        
        # 添加 thinking 参数 (Agnes/DeepSeek 支持)
        if thinking:
            payload["thinking"] = thinking

        t0 = time.time()
        data, used = await self._post("/chat/completions", payload, targets, role_name)
        elapsed_ms = int((time.time() - t0) * 1000)

        try:
            choice = data["choices"][0]
            message = choice.get("message") or {}
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"响应格式异常: {exc}") from exc

        content = str(message.get("content") or "")
        finish_reason = str(choice.get("finish_reason") or "")

        calls: List[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function") or {}
            raw_args = fn.get("arguments") or ""
            args: Dict[str, Any] = {}
            if isinstance(raw_args, dict):
                args = dict(raw_args)
            elif raw_args:
                try:
                    parsed = json.loads(raw_args)
                    if isinstance(parsed, dict):
                        args = parsed
                except json.JSONDecodeError:
                    # 参数不是合法 JSON —— 交给上层校验时拦住, 这里只记日志
                    logger.warning("[LLM] 工具参数不是合法 JSON: %r", raw_args[:200])
            calls.append(
                ToolCall(
                    id=str(raw.get("id") or ""),
                    name=str(fn.get("name") or ""),
                    arguments=args,
                    raw_arguments=str(raw_args),
                )
            )

        usage = data.get("usage") or {}
        result = ToolCallResult(
            content=content,
            calls=calls,
            finish_reason=finish_reason,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            provider=used.provider,
            model=used.model,
        )
        logger.info(
            "[LLM] %dms provider=%s model=%s role=%s tools=%d calls=%d finish=%s tokens=%s/%s",
            elapsed_ms,
            used.provider,
            used.model,
            role_name,
            len(tools),
            len(calls),
            finish_reason,
            result.prompt_tokens,
            result.completion_tokens,
        )
        return result

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        role: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """流式对话, 逐块 yield 文本增量。

        流式不支持降级 —— 已经吐出第一个字就没法换 provider 了。
        """
        role_name = role or "chat"
        targets = self.resolve(role_name, model=model)
        if not targets:
            raise LLMError(f"角色 {role_name!r} 没有可用的 provider")
        head = targets[0]
        payload = {
            "model": head.model,
            "messages": messages,
            "max_tokens": max_tokens or head.resolved_max_tokens(self.cfg.max_tokens),
            "temperature": (
                head.resolved_temperature(self.cfg.temperature)
                if temperature is None
                else temperature
            ),
            "stream": True,
        }
        try:
            client = await self._get_client(head.provider)
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"连接初始化失败: {exc}") from exc
        try:
            async with client.stream(
                "POST", "/chat/completions", json=payload,
                headers=self._headers(head.provider),
            ) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "ignore")
                    raise LLMError(f"HTTP {resp.status_code}: {body[:500]}")
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    for choice in obj.get("choices", []):
                        delta = choice.get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            yield piece
        except LLMError:
            raise
        except httpx.HTTPError as exc:
            raise LLMError(f"网络请求失败: {exc}") from exc
        except Exception as exc:
            logger.exception("LLM 流式请求异常")
            raise LLMError(f"流式请求失败: {exc}") from exc

    async def json_completion(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 256,
        role: Optional[str] = None,
    ) -> Optional[Any]:
        """要求模型输出 JSON, 解析失败返回 None。"""
        text = await self.chat(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            role=role,
        )
        return _extract_json(text)

    async def json_completion_stream(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 256,
        role: Optional[str] = None,
    ) -> Optional[Any]:
        """流式地要求模型输出 JSON。

        意图分类的输出很短 (几十个字符), 用流式能在拿到第一个 token 后
        就开始累积, 省掉「等整个响应体」的尾部延迟。
        """
        parts: List[str] = []
        async for piece in self.stream(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            role=role,
        ):
            parts.append(piece)
        return _extract_json("".join(parts))

    async def complete_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        executor: Any,
        tool_choice: Optional[Any] = "auto",
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        max_rounds: int = 2,
        role: Optional[str] = None,
    ) -> ToolCallResult:
        """带 tools 的多轮调用。

        `executor` 是 async 可调用对象, 签名 `executor(call: ToolCall) -> str`,
        返回值作为 `tool` role 消息回灌给模型。

        **多轮的成本**: 每多一轮就多一次完整往返 (实测 ~850ms)。语音场景
        对延迟极敏感, 所以默认 `max_rounds=1` (只调一轮, 不回灌) ——
        工具执行完用本地固定文案回复即可, 又快又稳定。

        `max_rounds` 是硬上限, 防模型反复调同一个工具形成死循环。
        """
        result = await self.tool_call(
            messages,
            tools,
            tool_choice=tool_choice,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            role=role,
        )

        if not result.has_calls or max_rounds <= 1:
            return result

        # 多轮: 执行工具 -> 回灌结果 -> 再问一次
        convo: List[Dict[str, Any]] = list(messages)
        convo.append(
            {
                "role": "assistant",
                "content": result.content or "",
                **({"tool_calls": _calls_to_raw(result.calls)} if result.calls else {}),
            }
        )

        for _ in range(max_rounds - 1):
            for call in result.calls:
                try:
                    output = await executor(call)
                except Exception as exc:
                    output = f"执行失败: {exc}"
                convo.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": str(output),
                    }
                )

            result = await self.tool_call(
                convo,
                tools,
                tool_choice=tool_choice,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                role=role,
            )
            # 模型不再要工具了 -> 它的 content 就是最终回复
            if not result.has_calls:
                break

        return result


def _calls_to_raw(calls: List[ToolCall]) -> List[Dict[str, Any]]:
    """把 ToolCall 转回 OpenAI 的 tool_calls 报文 (回灌给模型用)。"""
    return [
        {
            "id": c.id,
            "type": "function",
            "function": {
                "name": c.name,
                "arguments": c.raw_arguments or json.dumps(c.arguments, ensure_ascii=False),
            },
        }
        for c in calls
    ]


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


__all__ = ["LLMClient", "LLMError", "ToolCall", "ToolCallResult", "Target"]
