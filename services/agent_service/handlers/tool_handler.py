"""tool 分支: 执行动作类指令。

工具分两类, 决定了结果要不要朗读:
- 执行型 (kind="action"): 开灯、播放音乐这类。结果是「做完了」, 不需要念,
    安静执行即可。
- 输出型 (kind="query"): 查天气、查时间这类。结果本身就是内容, 必须经 TTS
    播报给用户听, 否则用户除了看到一句 "已执行 查天气" 之外拿不到任何信息。

首版内置的均为示例动作, 不连接任何真实设备。
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from handlers.base import AgentRequest, BranchResult, Handler

#: 工具类别
KIND_ACTION = "action"   #: 执行型, 只执行不朗读
KIND_QUERY = "query"     #: 输出型, 结果要朗读

#: 动作表: name -> 规格
#:   kind      工具类别 (KIND_ACTION / KIND_QUERY)
#:   patterns  匹配模式
#:   label     中文名, 用于日志与回报
TOOLS: Dict[str, Dict[str, Any]] = {
    "light_on": {
        "kind": KIND_ACTION,
        "label": "开灯",
        "patterns": [r"(打开|开启|开|点开)\s*(电?灯|台灯|电灯)"],
    },
    "light_off": {
        "kind": KIND_ACTION,
        "label": "关灯",
        "patterns": [r"(关闭|关掉|关|熄)\s*(电?灯|台灯|电灯)"],
    },
    "aircon_on": {
        "kind": KIND_ACTION,
        "label": "开空调",
        "patterns": [r"(打开|开启|开)\s*(空?调)"],
    },
    "aircon_off": {
        "kind": KIND_ACTION,
        "label": "关空调",
        "patterns": [r"(关闭|关掉|关)\s*(空?调)"],
    },
    "music_play": {
        "kind": KIND_ACTION,
        "label": "播放音乐",
        "patterns": [r"(播放|放一?首|来一?首)\s*(歌|音乐|曲)"],
    },
    # ---- 输出型: 结果需要播报 ----
    "weather": {
        "kind": KIND_QUERY,
        "label": "查天气",
        "patterns": [r"(天气|气温|下雨|温度)"],
    },
    "time": {
        "kind": KIND_QUERY,
        "label": "查时间",
        "patterns": [r"(几点|现在时间|什么时间|报时)"],
    },
    "date": {
        "kind": KIND_QUERY,
        "label": "查日期",
        "patterns": [r"(几号|今天.*日期|什么日期|今天星期)"],
    },
}


class ToolHandler(Handler):
    """动作执行分支。"""

    speak = False

    #: 闲聊/疑问句式, 命中则不算动作指令 (避免"今天天气怎么样"被当成查天气)
    QUESTION_PATTERNS = [
        r"怎么样",
        r"如何",
        r"为什么",
        r"什么是",
        r"是什么",
        r"多少",
        r"介绍",
        r"说说",
        r"讲讲",
    ]

    def __init__(self, config: Any = None, llm: Any = None) -> None:
        super().__init__(config=config, llm=llm)
        self._speak_tools: Optional[set] = None

    # ------------------------------------------------------------ 分类判定

    @property
    def speak_tools(self) -> set:
        """输出型工具名集合。

        默认按 TOOLS 表里的 kind 判定; 也可以在 configs/agent.yaml 里用
        handlers.tool.options.speak_tools 显式指定 (覆盖内置判定)。
        """
        if self._speak_tools is None:
            configured = self._options().get("speak_tools")
            if isinstance(configured, (list, tuple, set)):
                self._speak_tools = {str(x) for x in configured}
            else:
                self._speak_tools = {
                    name for name, spec in TOOLS.items()
                    if spec.get("kind") == KIND_QUERY
                }
        return self._speak_tools

    def _options(self) -> Dict[str, Any]:
        """取 tool 分支在 agent.yaml 里配的自定义选项。"""
        handlers = getattr(self.config, "handlers", None) or {}
        hcfg = handlers.get(self.name) if isinstance(handlers, dict) else None
        options = getattr(hcfg, "options", None)
        return options if isinstance(options, dict) else {}

    def is_output_tool(self, name: str) -> bool:
        """该工具是否为输出型 (结果需要 TTS 播报)。"""
        return name in self.speak_tools

    # ------------------------------------------------------------ 主流程

    async def handle(self, req: AgentRequest) -> BranchResult:
        started = time.time()
        name, spec, args = self._match(req.text)
        if name is None:
            # 没匹配到具体动作: 明确回报给用户 (要念出来, 否则用户一脸茫然)
            return BranchResult(
                reply="暂不支持这个操作。",
                speak=True,
                action={
                    "name": "none",
                    "args": {},
                    "status": "unsupported",
                    "speak": True,
                },
                trace=[self._trace("match", (time.time() - started) * 1000, "no-tool")],
            )

        status, detail = await self.execute(name, args)
        label = spec.get("label", name)
        speak = self.is_output_tool(name)

        if speak:
            # 输出型: 把结果本身念出来, 而不是只说 "已执行 查天气"
            reply = detail
        else:
            reply = f"已执行 {label}"

        return BranchResult(
            reply=reply,
            speak=speak,
            action={
                "name": name,
                "args": args,
                "status": status,
                "detail": detail,
                "kind": spec.get("kind", KIND_ACTION),
                "speak": speak,
            },
            trace=[self._trace("execute", (time.time() - started) * 1000, f"{name}:{status}")],
        )

    async def classify(self, req: AgentRequest) -> float:
        """只有能真正匹配到动作时才认可, 否则让 router 回落 chat。

        防止 LLM 把"今天天气怎么样"这类闲聊误判成查天气。
        """
        name, _, _ = self._match(req.text)
        return 1.0 if name else 0.0

    # ------------------------------------------------------------ 匹配

    def _match(self, text: str) -> Tuple[Optional[str], Dict[str, Any], Dict[str, Any]]:
        # 疑问句式视为闲聊, 不当动作指令
        if any(re.search(p, text) for p in self.QUESTION_PATTERNS):
            return None, {}, {}
        for name, spec in TOOLS.items():
            for pattern in spec.get("patterns", []):
                if re.search(pattern, text):
                    return name, spec, self._extract_args(name, text)
        return None, {}, {}

    def _extract_args(self, name: str, text: str) -> Dict[str, Any]:
        args: Dict[str, Any] = {"raw": text}
        if name == "weather":
            m = re.search(r"([一-龥]{2,6}?)(?:的)?天气", text)
            if m:
                city = m.group(1)
                # "今天/明天" 这类时间词不是城市
                if city in ("今天", "明天", "昨天", "现在", "目前"):
                    city = "默认城市"
                # "查询北京天气" 里的 "查询" 是动词不是城市名, 要剥掉
                for verb in ("查询", "查一下", "查查", "帮我查", "请问"):
                    if city.startswith(verb):
                        city = city[len(verb):]
                # 剥完可能就空了 (如 "查天气" -> "查")
                if len(city) < 2:
                    city = "默认城市"
            else:
                city = "默认城市"
            args["city"] = city
        return args

    # ------------------------------------------------------------ 执行

    def tool_label(self, name: str) -> str:
        """工具的中文名。"""
        return str((TOOLS.get(name) or {}).get("label", name))

    async def execute(self, name: str, args: Dict[str, Any]) -> Tuple[str, str]:
        """执行动作。真实项目里在这里接设备控制。

        返回 (status, detail); detail 是给人听的一句话。

        **tool calling 路径也走这里** —— 与正则匹配路径共用同一份实现。
        """
        if name == "light_on":
            return "ok", "灯已打开"
        if name == "light_off":
            return "ok", "灯已关闭"
        if name == "aircon_on":
            return "ok", "空调已打开"
        if name == "aircon_off":
            return "ok", "空调已关闭"
        if name == "music_play":
            return "ok", "开始播放音乐"

        # ---- 输出型工具: detail 直接就是要念给用户听的内容 ----
        if name == "weather":
            city = args.get("city", "默认城市")
            return "ok", f"{city}今天天气晴，气温 24 度"
        if name == "time":
            now = datetime.now()
            return "ok", f"现在是{now.hour}点{now.minute}分"
        if name == "date":
            now = datetime.now()
            week = "一二三四五六日"[now.weekday()]
            return "ok", f"今天是{now.month}月{now.day}日，星期{week}"
        return "failed", "未知动作"
