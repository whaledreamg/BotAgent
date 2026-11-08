"""调用 Agent 服务的分流接口。

Agent 返回体里 `speak` 决定后续是否走 TTS:
- speak=True  -> 回复文本需要合成并朗读
- speak=False -> 分支直接执行了动作, 不需要朗读
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

from config import AgentEndpoint

logger = logging.getLogger("agent_client")


class AgentError(RuntimeError):
    """Agent 调用失败。"""


@dataclass
class AgentReply:
    """Agent 的分流结果。"""

    intent: str = "chat"
    route: list = None
    speak: bool = True
    reply: str = ""
    action: Optional[Dict[str, Any]] = None
    latency_ms: int = 0
    trace: list = None
    #: Agent 服务内部耗时 (毫秒); 与本地 HTTP 往返的差值即为网络开销
    server_ms: int = 0
    #: 本地 HTTP 往返总耗时 (毫秒)
    rtt_ms: int = 0

    def __post_init__(self) -> None:
        if self.route is None:
            self.route = [self.intent]
        if self.trace is None:
            self.trace = []


class AgentClient:
    """Agent 服务的 HTTP 客户端。"""

    def __init__(self, endpoint: AgentEndpoint, session_id: str = "robot-01") -> None:
        self.endpoint = endpoint
        self.session_id = session_id

    def dispatch(self, text: str, session_id: Optional[str] = None) -> AgentReply:
        """把识别文本送给 Agent 分流, 返回处理结果。"""
        payload = json.dumps(
            {
                "session_id": session_id or self.session_id,
                "text": text,
                "use_history": True,
            }
        ).encode("utf-8")

        url = self.endpoint.dispatch_url
        last_err: Optional[Exception] = None
        for attempt in range(max(1, self.endpoint.max_retries)):
            started = time.time()
            try:
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self.endpoint.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, OSError) as exc:
                last_err = AgentError(f"Agent 网络请求失败: {exc}")
            except json.JSONDecodeError as exc:
                last_err = AgentError(f"Agent 响应不是合法 JSON: {exc}")
            except Exception as exc:
                last_err = AgentError(f"Agent 调用失败: {exc}")
            else:
                rtt_ms = int((time.time() - started) * 1000)
                server_ms = int(body.get("latency_ms", 0))
                logger.info(
                    "[Agent] 往返 %dms (服务端 %dms, 网络+解析 %dms) "
                    "intent=%s speak=%s",
                    rtt_ms,
                    server_ms,
                    max(0, rtt_ms - server_ms),
                    body.get("intent"),
                    body.get("speak"),
                )
                return AgentReply(
                    intent=body.get("intent", "chat"),
                    route=body.get("route") or [],
                    speak=bool(body.get("speak", True)),
                    reply=body.get("reply", "") or "",
                    action=body.get("action"),
                    latency_ms=server_ms,
                    trace=body.get("trace") or [],
                    server_ms=server_ms,
                    rtt_ms=rtt_ms,
                )
            if attempt < self.endpoint.max_retries - 1:
                time.sleep(1.0 * (attempt + 1))
        raise last_err or AgentError("Agent 调用失败")
