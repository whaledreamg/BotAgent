"""Skill 执行结果模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SkillResult:
    """Skill 执行的结果。"""

    #: 执行是否成功
    ok: bool = False

    #: 给用户/TTS 的输出内容
    output: str = ""

    #: 建议的人类可读回复
    reply: str = ""

    #: 是否应该让 TTS 播报
    speak: bool = False

    #: 执行过程中发生的错误
    error: str = ""

    #: 执行耗时（毫秒）
    elapsed_ms: float = 0.0

    #: 建议接下来执行的 skill（用于多轮编排）
    next_skills: List[str] = field(default_factory=list)

    #: 上下文状态更新（会并入 SkillContext.state）
    state_updates: Dict[str, Any] = field(default_factory=dict)

    #: 感知数据更新（会并入 SkillContext.perception）
    perception_updates: Dict[str, Any] = field(default_factory=dict)

    #: 执行过程中的跟踪信息（调试用）
    trace: List[Dict[str, Any]] = field(default_factory=list)

    #: 原始数据（可被更高层解析）
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转成字典。"""
        return {
            "ok": self.ok,
            "output": self.output,
            "reply": self.reply,
            "speak": self.speak,
            "error": self.error,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "next_skills": self.next_skills,
            "state_updates": self.state_updates,
            "perception_updates": self.perception_updates,
            "trace": self.trace,
            "payload": self.payload,
        }


__all__ = ["SkillResult"]
