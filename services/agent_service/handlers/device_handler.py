"""device 分支: 机器人本体控制占位。

首版只识别指令并回报意图, 不下发任何真实控制指令, 避免误动作。
后续接 RobotControl / brainco-hand-sdk 时在 _execute 里补实现即可。
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

from handlers.base import AgentRequest, BranchResult, Handler


class DeviceHandler(Handler):
    """机器人本体控制分支 (占位)。"""

    speak = False

    ACTIONS: Dict[str, List[str]] = {
        "walk": [r"(走|前进|前行)"],
        "stop": [r"(停下|停止|停)"],
        "photo": [r"(拍照|拍张|看一眼)"],
        "home": [r"(回到|回家|原点)"],
    }

    async def handle(self, req: AgentRequest) -> BranchResult:
        started = time.time()
        name, args = self._match(req.text)
        if name is None:
            return BranchResult(
                reply="暂不支持这个动作。",
                speak=False,
                action={"name": "none", "args": {}, "status": "unsupported"},
                trace=[self._trace("match", (time.time() - started) * 1000, "no-device-action")],
            )
        status, detail = await self._execute(name, args)
        return BranchResult(
            reply=f"已执行 {name}",
            speak=False,
            action={"name": name, "args": args, "status": status, "detail": detail},
            trace=[self._trace("execute", (time.time() - started) * 1000, f"{name}:{status}")],
        )

    async def classify(self, req: AgentRequest) -> float:
        """能匹配到本体动作才认可。"""
        name, _ = self._match(req.text)
        return 1.0 if name else 0.0

    def _match(self, text: str) -> Tuple[Optional[str], Dict[str, Any]]:
        for name, patterns in self.ACTIONS.items():
            for pattern in patterns:
                if re.search(pattern, text):
                    return name, {"raw": text}
        return None, {}

    async def _execute(self, name: str, args: Dict[str, Any]) -> Tuple[str, str]:
        # 占位实现: 只回报, 不下发真实指令
        return "dry_run", f"{name} 已识别, 设备控制待接入"
