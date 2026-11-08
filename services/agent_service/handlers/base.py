"""分支处理器抽象。

一个 Handler 代表一类语义分支, 由 router 决定把请求交给谁。
Handler 产出 BranchResult, 其中 speak 决定该结果是否要交给 TTS 朗读:
- speak=True  -> 文本进 TTS 合成并播放 (chat 分支)
- speak=False -> 直接执行动作, 不走 TTS (tool / device 分支)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AgentRequest:
    """进入 Agent 分流的请求。"""

    text: str
    session_id: str = "default"
    history: List[Dict[str, Any]] = field(default_factory=list)
    image_path: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BranchResult:
    """分支处理结果。"""

    reply: str = ""
    speak: bool = False
    action: Optional[Dict[str, Any]] = None
    trace: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reply": self.reply,
            "speak": self.speak,
            "action": self.action,
            "trace": self.trace,
        }


class Handler:
    """分支处理器基类。

    子类至少要实现 handle(); classify() 可选, 用于让分支自行参与打分。
    """

    #: 分支名, registry 装载时会用配置里的名字覆盖
    name: str = "base"
    #: 该分支默认是否朗读
    speak: bool = False

    def __init__(self, config: Any = None, llm: Any = None) -> None:
        self.config = config
        self.llm = llm

    async def classify(self, req: AgentRequest) -> float:
        """返回 0.0~1.0 的置信度。

        router 在用 LLM 判定意图后会回来问这个分支"你确定能处理吗",
        返回 0 表示不认可, router 会回落到默认分支。
        """
        return 0.0

    async def handle(self, req: AgentRequest) -> BranchResult:
        """执行分支逻辑。"""
        raise NotImplementedError

    # ------------------------------------------------------------ 工具

    def _trace(self, step: str, ms: float, detail: str = "") -> Dict[str, Any]:
        return {"step": step, "ms": round(ms, 1), "detail": detail}


__all__ = ["AgentRequest", "BranchResult", "Handler"]
