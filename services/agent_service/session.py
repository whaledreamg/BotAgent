"""会话历史管理。

按 session_id 保存最近 N 轮对话, 超过 TTL 自动清理。
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List


class SessionStore:
    """线程安全的会话历史存储。"""

    def __init__(self, max_turns: int = 6, ttl_seconds: int = 1800) -> None:
        self.max_turns = max(0, int(max_turns))
        self.ttl_seconds = max(0, int(ttl_seconds))
        self._lock = threading.Lock()
        self._sessions: Dict[str, dict] = {}

    def _expired(self, item: dict) -> bool:
        if not self.ttl_seconds:
            return False
        return (time.time() - item["updated"]) > self.ttl_seconds

    def history(self, session_id: str) -> List[dict]:
        """取历史消息列表 (OpenAI 格式), 没有则返回空列表。"""
        with self._lock:
            item = self._sessions.get(session_id)
            if item is None:
                return []
            if self._expired(item):
                self._sessions.pop(session_id, None)
                return []
            return list(item["messages"])

    def append(self, session_id: str, user: str, assistant: str) -> None:
        """记录一轮对话, 只保留最近 max_turns 轮。"""
        if not session_id or self.max_turns == 0:
            return
        with self._lock:
            item = self._sessions.setdefault(
                session_id, {"messages": [], "updated": time.time()}
            )
            item["messages"].append({"role": "user", "content": user})
            item["messages"].append({"role": "assistant", "content": assistant})
            limit = self.max_turns * 2
            if len(item["messages"]) > limit:
                item["messages"] = item["messages"][-limit:]
            item["updated"] = time.time()

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def cleanup(self) -> int:
        """清理过期会话, 返回清理数量。"""
        if not self.ttl_seconds:
            return 0
        with self._lock:
            dead = [sid for sid, item in self._sessions.items() if self._expired(item)]
            for sid in dead:
                self._sessions.pop(sid, None)
            return len(dead)
