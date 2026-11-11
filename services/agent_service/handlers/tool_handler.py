"""tool 分支: 执行动作类指令。

工具分两类, 决定了结果要不要朗读:
- 执行型 (kind="action"): 开灯、播放音乐这类。结果是「做完了」, 不需要念,
    安静执行即可。
- 输出型 (kind="query"): 查天气、查时间这类。结果本身就是内容, 必须经 TTS
    播报给用户听, 否则用户除了看到一句 "已执行 查天气" 之外拿不到任何信息。

首版内置的均为示例动作, 不连接任何真实设备。
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from handlers.base import AgentRequest, BranchResult, Handler
from vision_client import VisionClient, VisionError

logger = logging.getLogger("agent.tools")

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
    # ---- 视觉: 找物体 ----
    #
    # 匹配要**排除机械臂相关的说法** (「左手在哪」是问机械臂, 不是问
    # 相机能看到的物体)。机械臂动作由 device 分支处理, 这里别抢。
    #
    # Python 的 re 只支持固定宽度的前瞻, 所以排除机械臂部位这件事
    # 交给 _match 里的 _is_arm_query 判断。
    "find_object": {
        "kind": KIND_QUERY,
        "label": "找物体",
        "patterns": [
            r"(在哪|在哪儿|什么位置|什么方位|哪个位置)",
            r"(有没有|有没)(看到|看见)",
            r"(找一下|找找|查找|寻找|搜索)",
            r"(看看|瞧一瞧|瞧)",
            # 计数型提问: 「桌上有几个瓶子」「有几个苹果」
            # 答案是数出来的, 必须查相机 —— 模型说「有3个」就是编的。
            #
            # 量词要**必需**: 「几点」是查时间, 「几个」才是数东西。
            r"几(个|只|瓶|杯|张|块|把|支|颗|人|件|条|台|本|双|副)",
            r"多少(个|只|瓶|杯|张|块|把|支|颗|人|件|条|台|本)",
        ],
    },
}

#: 机械臂部位词 —— 出现在句子里说明问的是机械臂, 不是相机能看到的物体
_ARM_PARTS = ("左手", "右手", "机械臂", "手臂", "胳膊", "爪子", "手爪")


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
        self._vision_client: Optional[VisionClient] = None

    # ------------------------------------------------------------ 视觉

    @property
    def vision_client(self) -> VisionClient:
        """视觉服务客户端 (懒加载)。"""
        if self._vision_client is None:
            self._vision_client = VisionClient(
                base_url=self._options().get("vision_base_url")
                or "http://127.0.0.1:8803",
                timeout=float(self._options().get("vision_timeout", 10.0)),
            )
        return self._vision_client

    async def aclose(self) -> None:
        if self._vision_client is not None:
            await self._vision_client.aclose()
            self._vision_client = None

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
                    # 问的是机械臂部位 -> 不是视觉服务能回答的
                    if name == "find_object" and self._is_arm_query(text):
                        continue
                    return name, spec, self._extract_args(name, text)
        return None, {}, {}

    @staticmethod
    def _is_arm_query(text: str) -> bool:
        """句子里问的是机械臂部位吗?

        「左手在哪」「机械臂在什么位置」这类问题是问机械臂自己的状态,
        归 device 分支管, 视觉服务答不了。
        """
        return any(part in text for part in _ARM_PARTS)

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
        elif name == "find_object":
            args["name"] = self._guess_label(text)
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
        if name == "find_object":
            return await self._execute_find_object(args)
        return "failed", "未知动作"

    # ------------------------------------------------------------ 视觉

    async def _execute_find_object(self, args: Dict[str, Any]) -> Tuple[str, str]:
        """找物体 -> 播报它的位置。

        视觉服务没起来 / 相机没连上 / 没找到物体, 都要给出**能听懂**
        的话 —— 这些都是要念给用户听的, 不能只说 "failed"。
        """
        raw = str(args.get("raw", "") or "")
        label = str(args.get("name", "") or "").strip()
        if not label:
            label = self._guess_label(raw)
        if not label:
            return "failed", "没说要找什么物体"

        frame = str(args.get("frame", "camera") or "camera")

        try:
            result = await self.vision_client.find(label, frame=frame)
        except VisionError as exc:
            logger.error("[视觉] 查询失败: %s", exc)
            return "failed", "视觉服务暂时用不了，请稍后再试"

        objects = result.get("objects") or []
        if not objects:
            return "ok", f"没有看到{label}"

        obj = objects[0]
        pose = obj.get("pose") or {}
        frame_name = pose.get("frame", "camera")

        # 深度不可信时 (反光/透明物体), 位置只能算大概
        if not pose.get("depth_valid", True):
            return "ok", f"看到了{label}，但测不准它的距离"

        x = float(pose.get("x", 0.0))
        y = float(pose.get("y", 0.0))
        z = float(pose.get("z", 0.0))

        if frame_name in ("arm_base", "base", "left_base", "right_base"):
            # 双臂: 说清楚是哪条臂的基座系
            side = ""
            if frame_name == "left_base":
                side = "左臂"
            elif frame_name == "right_base":
                side = "右臂"
            where = f"{side}机械臂坐标系" if side else "机械臂坐标系"
            return "ok", (
                f"找到{label}，在{where}里，"
                f"X {x:.2f} 米，Y {y:.2f} 米，Z {z:.2f} 米"
            )
        if frame_name == "world":
            return "ok", (
                f"找到{label}，在机器人世界坐标系里，"
                f"X {x:.2f} 米，Y {y:.2f} 米，Z {z:.2f} 米"
            )
        return "ok", (
            f"找到{label}，距离相机 {z:.2f} 米，"
            f"往右 {x:.2f} 米，往下 {y:.2f} 米"
        )

    @staticmethod
    def _guess_label(text: str) -> str:
        """从「找一下杯子」这类句子里猜出要找的物体。

        思路: 先剥掉句子里表示「动作/疑问」的词, 剩下的就是物体名。

        「杯子在哪」-> 「在哪」是疑问词, 剥掉后剩「杯子」
        「看看有没有苹果」-> 「看看」「有没有」都是动词/疑问, 剥掉后剩「苹果」
        「桌上有几个瓶子」-> 「桌上」「有」「几个」都是修饰/疑问, 剩「瓶子」
        """
        if not text:
            return ""

        # 疑问句式词 + 动作词, 从长到短剥
        words = [
            "在什么位置", "在哪个位置", "在哪儿", "在哪里", "在哪",
            "有没有", "有没", "看见了吗", "看到没", "看见", "看到",
            "找一下", "找一找", "找找", "查找", "搜索", "寻找", "找",
            "看看", "看下", "看一下", "瞧", "帮我", "请问", "请",
            "那个", "这个", "那些", "这些", "一个",
            "了吗", "呢", "吗", "啊", "呀", "了", "的",
        ]

        s = text.strip()
        changed = True
        while changed:
            changed = False
            for w in words:
                if s.startswith(w) and len(s) > len(w):
                    s = s[len(w):].strip()
                    changed = True
                if s.endswith(w) and len(s) > len(w):
                    s = s[: -len(w)].strip()
                    changed = True
                # 中间的疑问词也剥 (如 "看看有没有苹果")
                mid = f"有没有{w}"
                if mid in s:
                    s = s.replace(mid, w).strip()
                    changed = True

        # ---- 计数型提问: 「桌上有几个瓶子」-> 「瓶子」 ----
        #
        # 这类句子里残留的「桌上」「有」「几个」不是物体名的一部分,
        # 直接送去做类别匹配会查不到东西 (「桌上有几个瓶子」查不到 bottle)。
        #
        # 做法: 先剥「几个/多少」这类疑问, 再剥「有/还剩」这类动词,
        # 最后剥剩下的方位/位置前缀 (桌上、上面、周围)。
        s = re.sub(r"(还剩|剩下)?(有|看见|看到)?\s*几(个|只|瓶|杯|张|块|把|支|颗|人|件)?", "", s)
        s = re.sub(r"(还剩|剩下)(有)?", "", s)
        s = re.sub(r"多少", "", s)
        s = re.sub(r"(东西|物体|物品)", "", s)
        # 时间/状态前缀: 「现在」「目前」「今天」—— 不是物体名的一部分
        s = re.sub(r"^(现在|目前|今天|此刻|眼前)", "", s)
        # 位置/方位前缀: 「桌上」「上面」「周围」「桌子上」
        #
        # **注意**: 剥完必须还剩东西。剥之前先看剥完的结果 ——
        # 「椅子」里的「椅」如果被当成方位词剥掉, 就剩个「子」了。
        for prefix in (r"^(在|从)?(桌|柜子|架子|地面|地|墙|窗)(上|下|面|里|内|旁|边|周围|附近)?",
                       r"^(上|下|面|里|内|旁|边|周围|附近)"):
            m = re.match(prefix, s)
            if m and m.end() < len(s):
                s = s[m.end():]
        # 「上面」「旁边」这类后缀方位词
        s = re.sub(r"(上面|旁边|周围|附近|里面)$", "", s)
        s = s.strip()

        # 只保留中英文数字, 去掉标点
        s = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s)
        return s.strip()
