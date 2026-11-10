"""OpenAI 兼容接口封装。

支持:
- chat():  普通对话, 返回文本
- stream(): 流式对话, 逐块 yield 增量
- json_completion(): 要求模型输出 JSON (用于语义识别的意图分类)
- tool_call(): 带 tools 的原生 function calling, 返回结构化的工具调用

实现只用 httpx, 不依赖 openai SDK, 便于在轻量环境里运行。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from config import LLMConfig

logger = logging.getLogger("agent.llm")


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


class LLMClient:
    """异步 LLM 客户端。"""

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            removed = _sanitize_proxy_env()
            if removed:
                logger.warning(
                    "已忽略不受支持的代理环境变量: %s", ", ".join(removed)
                )
            self._client = httpx.AsyncClient(
                base_url=self.cfg.base_url,
                timeout=self.cfg.timeout,
                headers={
                    "Authorization": f"Bearer {self.cfg.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------- 内部

    async def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.cfg.api_key:
            raise LLMError("未配置 LLM_API_KEY (configs/.env 或环境变量)")
        last_err: Optional[Exception] = None
        for attempt in range(max(1, self.cfg.max_retries)):
            try:
                client = await self._get_client()
                resp = await client.post(path, json=payload, headers=self._headers())
            except httpx.HTTPError as exc:
                last_err = LLMError(f"网络请求失败: {exc}")
            except LLMError:
                raise
            except Exception as exc:
                # 兜底: 代理配置之类的意外异常不能冒泡成 500
                logger.exception("LLM 请求异常")
                last_err = LLMError(f"请求失败: {exc}")
            else:
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        last_err = LLMError(f"响应不是合法 JSON: {exc}")
                elif resp.status_code in (429, 500, 502, 503, 504):
                    last_err = LLMError(f"HTTP {resp.status_code}: {resp.text[:500]}")
                else:
                    raise LLMError(f"HTTP {resp.status_code}: {resp.text[:500]}")
            if attempt < self.cfg.max_retries - 1:
                await _sleep(2 ** attempt)
        raise last_err or LLMError("请求失败")

    # ------------------------------------------------------------- 公开

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
    ) -> str:
        payload = {
            "model": model or self.cfg.model,
            "messages": messages,
            "max_tokens": max_tokens or self.cfg.max_tokens,
            "temperature": self.cfg.temperature if temperature is None else temperature,
        }
        if tools:
            payload["tools"] = tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
        t0 = time.time()
        data = await self._post("/chat/completions", payload)
        elapsed_ms = int((time.time() - t0) * 1000)
        try:
            content = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"响应格式异常: {exc}") from exc
        usage = data.get("usage") or {}
        logger.info(
            "[LLM] %dms model=%s tokens=%s/%s",
            elapsed_ms,
            payload["model"],
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
    ) -> ToolCallResult:
        """带 tools 的原生 function calling。

        模型要么返回 `tool_calls` (要执行动作), 要么返回普通文本
        (就是闲聊回复)。两种结果都在这里解析好, 调用方只看
        `has_calls` 决定走哪条路。
        """
        if not tools:
            raise LLMError("tool_call 需要至少一个 tool 定义")

        payload: Dict[str, Any] = {
            "model": model or self.cfg.model,
            "messages": messages,
            "max_tokens": max_tokens or self.cfg.max_tokens,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "tools": tools,
        }
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

        t0 = time.time()
        data = await self._post("/chat/completions", payload)
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
        )
        logger.info(
            "[LLM] %dms model=%s tools=%d calls=%d finish=%s tokens=%s/%s",
            elapsed_ms,
            payload["model"],
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
    ) -> AsyncIterator[str]:
        """流式对话, 逐块 yield 文本增量。"""
        if not self.cfg.api_key:
            raise LLMError("未配置 LLM_API_KEY (configs/.env 或环境变量)")
        payload = {
            "model": model or self.cfg.model,
            "messages": messages,
            "max_tokens": max_tokens or self.cfg.max_tokens,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "stream": True,
        }
        try:
            client = await self._get_client()
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"连接初始化失败: {exc}") from exc
        try:
            async with client.stream(
                "POST", "/chat/completions", json=payload, headers=self._headers()
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
    ) -> Optional[Any]:
        """要求模型输出 JSON, 解析失败返回 None。"""
        text = await self.chat(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return _extract_json(text)

    async def json_completion_stream(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 256,
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


__all__ = ["LLMClient", "LLMError", "ToolCall", "ToolCallResult"]
