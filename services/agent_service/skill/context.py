"""Skill 执行上下文：跨多轮的状态管理。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from skill.resolver import SkillCall

logger = logging.getLogger("skill.context")


@dataclass
class SkillContext:
    """一个请求的执行上下文（贯穿多轮）。"""

    session_id: str                                      # 会话 ID
    request_id: str                                      # 请求 ID

    # 状态机
    state: Dict[str, Any] = field(default_factory=dict)  # 当前状态
    state_history: List[Dict[str, Any]] = field(default_factory=list)  # 状态变化历史

    # 感知结果缓存
    perception: Dict[str, Any] = field(default_factory=dict)           # 感知数据
    perception_age_ms: Dict[str, int] = field(default_factory=dict)    # 数据新鲜度

    # 决策链
    decisions: List[SkillCall] = field(default_factory=list)  # 已执行的 skill 调用
    next_decision: Optional[SkillCall] = None                  # 建议的下一步

    # 多轮编排
    conversation_turn: int = 0                          # 当前对话轮数
    max_turns: int = 5                                  # 最多编排轮数

    # 时间戳
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def get(self, key: str, default: Any = None) -> Any:
        """获取状态字段。"""
        return self.state.get(key, default)

    def set(self, key: str, value: Any, ttl_ms: Optional[int] = None) -> None:
        """
        更新状态字段。

        Args:
            key: 字段名
            value: 字段值
            ttl_ms: 生存时间（毫秒，可选）
        """
        # 保存到历史
        self.state_history.append(
            {
                "timestamp": time.time(),
                "key": key,
                "old_value": self.state.get(key),
                "new_value": value,
            }
        )

        # 更新状态
        self.state[key] = value
        self.updated_at = time.time()

        if ttl_ms:
            # 记录过期时间
            if "_ttl" not in self.state:
                self.state["_ttl"] = {}
            self.state["_ttl"][key] = time.time() * 1000 + ttl_ms

    def get_perception(self, key: str, default: Any = None) -> Any:
        """获取感知数据。"""
        return self.perception.get(key, default)

    def set_perception(self, key: str, value: Any) -> None:
        """设置感知数据并记录新鲜度。"""
        self.perception[key] = value
        self.perception_age_ms[key] = 0

    def age_perception(self, age_ms: int = 1000) -> None:
        """增加感知数据的年龄（毫秒）。"""
        for key in self.perception_age_ms:
            self.perception_age_ms[key] += age_ms

    def add_decision(self, skill_call: SkillCall) -> None:
        """添加已执行的 skill 调用。"""
        self.decisions.append(skill_call)
        self.conversation_turn += 1

    def is_expired(self, key: str) -> bool:
        """检查字段是否已过期。"""
        if "_ttl" not in self.state:
            return False
        ttl = self.state["_ttl"].get(key)
        if ttl is None:
            return False
        return time.time() * 1000 > ttl

    def clear_expired(self) -> None:
        """清理已过期的字段。"""
        if "_ttl" not in self.state:
            return
        now_ms = time.time() * 1000
        expired_keys = [
            key for key, ttl in self.state["_ttl"].items() if now_ms > ttl
        ]
        for key in expired_keys:
            self.state.pop(key, None)
            self.state["_ttl"].pop(key, None)

    def to_dict(self) -> Dict[str, Any]:
        """转成字典（调试用）。"""
        return {
            "session_id": self.session_id,
            "request_id": self.request_id,
            "state": self.state,
            "perception": self.perception,
            "conversation_turn": self.conversation_turn,
            "max_turns": self.max_turns,
            "decisions_count": len(self.decisions),
        }

    def __repr__(self) -> str:
        return (
            f"<SkillContext session={self.session_id} turn={self.conversation_turn}/{self.max_turns}>"
        )


__all__ = ["SkillContext"]
