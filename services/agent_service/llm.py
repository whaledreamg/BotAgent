"""OpenAI 兼容接口封装。

支持:
- chat():  普通对话, 返回文本
- stream(): 流式对话, 逐块 yield 增量
- json_completion(): 要求模型输出 JSON (用于语义识别的意图分类)

实现只用 httpx, 不依赖 openai SDK, 便于在轻量环境里运行。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from config import LLMConfig

logger = logging.getLogger("agent.llm")


class LLMError(RuntimeError):
    """LLM 调用失败。"""


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
    ) -> str:
        payload = {
            "model": model or self.cfg.model,
            "messages": messages,
            "max_tokens": max_tokens or self.cfg.max_tokens,
            "temperature": self.cfg.temperature if temperature is None else temperature,
        }
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


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


__all__ = ["LLMClient", "LLMError"]
