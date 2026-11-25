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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from handlers.base import AgentRequest, BranchResult, Handler
from arm_client import ArmClient
from vision_client import VisionClient, VisionError
from weather_client import (
    CityNotFound,
    CityNotSpecified,
    WeatherClient,
    WeatherError,
)
from parameter_extraction_manager import ParameterExtractionManager
from llm_parameter_extractor import SkillSpec, ParameterSpec
from thinking_logger import ThinkingLogger
from event_bus import bus
import yaml

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
    #
    # 天气要覆盖多种说法:
    # - 「天气怎么样」「今天天气」   -> 查当前城市
    # - 「北京天气」「查一下上海天气」-> 查指定城市
    # - 「气温多少」「下雨了吗」      -> 也是天气
    "weather": {
        "kind": KIND_QUERY,
        "label": "查天气",
        "patterns": [
            r"天气",
            r"气温",
            r"温度",
            r"下雨",
            r"下雪",
            r"刮风",
            r"台风",
        ],
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
    # ---- 视觉: 描述场景 ----
    #
    # 「你看到了什么」「前面有什么」没有具体物体名, 要列出全部检测结果。
    # **必须排在 find_object 之前**, 否则「看看有什么」会被「看看」抢走。
    "describe_scene": {
        "kind": KIND_QUERY,
        "label": "描述场景",
        "patterns": [
            r"(你)?(看到|看见)了?(什么|啥|哪些)",
            r"(前面|面前|眼前|桌上|桌子上)(有|是)?(什么|啥|哪些)",
            r"(有什么|有哪些)(东西|物体)",
            r"看看(有什么|周围)",
            r"描述一下(眼前|周围|场景)",
        ],
    },
    # ---- 视觉 + 机械臂: 抓取 ----
    #
    # 没有正则: 会动的指令只走 LLM tool calling (tools.yaml 的 grasp_object),
    # 不用关键词快速通道, 避免「拿」「抓」这类词误触发机械臂移动。
    "grasp_object": {
        "kind": KIND_QUERY,
        "label": "抓取物体",
        "patterns": [],
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
        self._arm_client: Optional[ArmClient] = None
        self._weather_client: Optional[WeatherClient] = None
        # 混合参数提取管理器
        self._extraction_manager: Optional[ParameterExtractionManager] = None
        # 思考过程日志记录器
        self._thinking_logger: Optional[ThinkingLogger] = None
        # Skill 定义缓存
        self._skills: Dict[str, SkillSpec] = {}
        # 加载 Skill 定义
        self._load_skills()

    # ------------------------------------------------------------ 天气

    @property
    def weather_client(self) -> WeatherClient:
        """天气服务客户端 (懒加载)。"""
        if self._weather_client is None:
            opts = self._options()
            self._weather_client = WeatherClient(
                geocode_url=str(
                    opts.get("weather_geocode_url")
                    or "https://geocoding-api.open-meteo.com/v1/search"
                ),
                forecast_url=str(
                    opts.get("weather_forecast_url")
                    or "https://api.open-meteo.com/v1/forecast"
                ),
                timeout=float(opts.get("weather_timeout", 5.0)),
                default_city=str(opts.get("weather_default_city") or ""),
            )
        return self._weather_client

    @property
    def extraction_manager(self) -> ParameterExtractionManager:
        """混合参数提取管理器 (懒加载)。"""
        if self._extraction_manager is None:
            self._extraction_manager = ParameterExtractionManager(llm=self.llm)
        return self._extraction_manager

    @property
    def thinking_logger(self) -> ThinkingLogger:
        """思考过程日志记录器 (懒加载)。"""
        if self._thinking_logger is None:
            self._thinking_logger = ThinkingLogger()
        return self._thinking_logger

    def _load_skills(self) -> None:
        """从 configs/param_skills.yaml 加载 Skill 定义。"""
        try:
            # 找到项目根目录 (Bot Agent/)
            root_dir = Path(__file__).resolve().parents[3]
            skill_config_path = root_dir / "configs" / "param_skills.yaml"
            
            if not skill_config_path.exists():
                logger.warning(f"[Skill加载] 配置文件不存在: {skill_config_path}")
                return
            
            with open(skill_config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
            
            skills_config = config.get("skills", {})
            for skill_name, skill_data in skills_config.items():
                parameters = []
                for param_data in skill_data.get("parameters", []):
                    param_spec = ParameterSpec(
                        name=param_data.get("name", ""),
                        description=param_data.get("description", ""),
                        param_type=param_data.get("type", "string"),
                        required=param_data.get("required", False),
                        examples=param_data.get("examples", []),
                        notes=param_data.get("notes"),
                    )
                    parameters.append(param_spec)
                
                skill_spec = SkillSpec(
                    name=skill_name,
                    label=skill_data.get("label", skill_name),
                    description=skill_data.get("description", ""),
                    category=skill_data.get("category", "query"),
                    parameters=parameters,
                )
                self._skills[skill_name] = skill_spec
                logger.info(f"[Skill加载] 成功加载 Skill: {skill_name}")
        
        except Exception as e:
            logger.error(f"[Skill加载] 加载失败: {e}")

    async def aclose(self) -> None:
        if self._vision_client is not None:
            await self._vision_client.aclose()
            self._vision_client = None
        if self._arm_client is not None:
            await self._arm_client.aclose()
            self._arm_client = None
        if self._weather_client is not None:
            await self._weather_client.aclose()
            self._weather_client = None

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

    @property
    def arm_client(self) -> ArmClient:
        """机械臂服务客户端 (懒加载), 抓取移动用。"""
        if self._arm_client is None:
            opts = self._options()
            base = opts.get("arm_base_url") or getattr(
                getattr(self.config, "arm_actions", None), "base_url", ""
            ) or "http://127.0.0.1:8802"
            self._arm_client = ArmClient(
                base_url=str(base), timeout=float(opts.get("arm_timeout", 5.0))
            )
        return self._arm_client

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
        
        # 检查是否是复合查询（包含多个关键词）
        # 例如：「先查天气再查时间」「查上海和杭州的天气」
        if self._is_composite_query(req.text):
            logger.info("[工具处理] 检测到复合查询，尝试使用编排引擎")
            result = await self._handle_composite(req.text, started)
            if result:
                return result
        
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

        # 使用 LLM-First 参数提取（异步）
        args = await self._extract_args_async(
            name, req.text, args, req.session_id, req.turn_id
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
        name, spec, args = self._match_tool(text)
        if name is None:
            return None, {}, {}

        # 疑问句式视为闲聊, 不当动作指令
        #
        # **天气是例外**: 「今天天气怎么样」就是标准的查天气说法,
        # 而且用户没提城市时问的正是**当前城市**的天气。
        # 把它当闲聊挡掉, 用户永远等不到天气结果。
        if name not in ("weather", "describe_scene") and any(
            re.search(p, text) for p in self.QUESTION_PATTERNS
        ):
            return None, {}, {}
        return name, spec, args

    def _match_tool(self, text: str) -> Tuple[Optional[str], Dict[str, Any], Dict[str, Any]]:
        """按 TOOLS 表的正则找工具 (不看疑问句式)。"""
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
        """同步参数提取（兼容旧代码）。"""
        args: Dict[str, Any] = {"raw": text}
        if name == "weather":
            # 使用混合提取器提取城市（同步调用正则版本）
            city = self.extraction_manager.extract_without_llm("city", text)
            args["city"] = city or ""
        elif name == "find_object":
            # 使用混合提取器提取物体
            obj = self.extraction_manager.extract_without_llm("object", text)
            args["name"] = obj or ""
        return args

    async def _extract_args_async(
        self,
        name: str,
        text: str,
        base_args: Dict[str, Any],
        session_id: str = "",
        turn_id: str = "",
    ) -> Dict[str, Any]:
        """异步 LLM-First 参数提取。
        
        使用三层架构：缓存 → LLM → 正则
        """
        args = base_args.copy()
        
        try:
            if name == "weather" and name in self._skills:
                # 使用 LLM-First 提取城市
                skill_spec = self._skills[name]
                start_time = time.time()
                
                params, confidence, reasoning = await self.extraction_manager.llm_first_extract(
                    text=text,
                    skill_spec=skill_spec,
                )
                
                elapsed_ms = (time.time() - start_time) * 1000
                
                if params:
                    city = params.get("city", "")
                    args["city"] = city or ""
                    logger.info(
                        f"[LLM-First] 天气参数提取: city={city}, "
                        f"confidence={confidence:.2f}, elapsed={elapsed_ms:.1f}ms"
                    )
                    
                    # 记录思考日志
                    self.thinking_logger.log_extraction(
                        skill=name,
                        user_query=text,
                        prompt=skill_spec.description,
                        llm_response={"status": "success"},
                        extracted_params=params,
                        confidence=confidence,
                        reasoning=reasoning,
                        total_ms=elapsed_ms,
                        timing_breakdown={"extraction": elapsed_ms},
                    )
                    # ---- 监控埋点: 参数提取的思考过程 ----
                    #
                    # `reasoning` 是模型解释「我怎么从这句话里认出城市的」,
                    # 是整条链路上最有信息量的一段 —— 推给监控端展示。
                    bus.publish(
                        "thinking",
                        {
                            "phase": "param_extraction",
                            "text": text,
                            "skill": name,
                            "extracted_params": params,
                            "confidence": confidence,
                            "reasoning": reasoning or "",
                            "elapsed_ms": round(elapsed_ms, 1),
                            "prompt": skill_spec.description,
                        },
                        session_id=session_id,
                        turn_id=turn_id,
                    )
                else:
                    # 降级到同步正则提取
                    city = self.extraction_manager.extract_without_llm("city", text)
                    args["city"] = city or ""
                    logger.debug(f"[LLM-First] 天气参数提取失败，使用正则降级")
                    
            elif name == "find_object" and name in self._skills:
                # 使用 LLM-First 提取物体名称
                skill_spec = self._skills[name]
                start_time = time.time()
                
                params, confidence, reasoning = await self.extraction_manager.llm_first_extract(
                    text=text,
                    skill_spec=skill_spec,
                )
                
                elapsed_ms = (time.time() - start_time) * 1000
                
                if params:
                    obj_name = params.get("object", "")
                    args["name"] = obj_name or ""
                    logger.info(
                        f"[LLM-First] 物体查找参数提取: object={obj_name}, "
                        f"confidence={confidence:.2f}, elapsed={elapsed_ms:.1f}ms"
                    )
                    
                    # 记录思考日志
                    self.thinking_logger.log_extraction(
                        skill=name,
                        user_query=text,
                        prompt=skill_spec.description,
                        llm_response={"status": "success"},
                        extracted_params=params,
                        confidence=confidence,
                        reasoning=reasoning,
                        total_ms=elapsed_ms,
                        timing_breakdown={"extraction": elapsed_ms},
                    )
                    # ---- 监控埋点: 参数提取的思考过程 ----
                    bus.publish(
                        "thinking",
                        {
                            "phase": "param_extraction",
                            "text": text,
                            "skill": name,
                            "extracted_params": params,
                            "confidence": confidence,
                            "reasoning": reasoning or "",
                            "elapsed_ms": round(elapsed_ms, 1),
                            "prompt": skill_spec.description,
                        },
                        session_id=session_id,
                        turn_id=turn_id,
                    )
                else:
                    # 降级到同步正则提取
                    obj = self.extraction_manager.extract_without_llm("object", text)
                    args["name"] = obj or ""
                    logger.debug(f"[LLM-First] 物体参数提取失败，使用正则降级")
                    
        except Exception as e:
            logger.error(f"[LLM-First] 异步参数提取异常: {e}", exc_info=True)
            # 异常时使用同步降级
            args = self._extract_args(name, text)
        
        return args

    async def _extract_city_with_llm(self, text: str) -> str:
        """使用 LLM 精准提取城市名。
        
        对于"先查南京天气再查时间"这样的复杂句子，
        正则容易失败，但 LLM 能理解意图。
        """
        try:
            # 用 LLM 提取参数
            prompt = f"""从这句话里提取城市名。
句子: "{text}"

规则:
1. 返回只包含城市名, 不要其他文字
2. 如果没有提到城市, 返回空

城市名:"""
            
            # 调用 LLM
            response = await self.llm_client.complete(
                prompt,
                max_tokens=10,
                temperature=0,
            )
            
            city = response.strip()
            
            # 验证是否是合理的城市名（2-4 个汉字）
            if city and 2 <= len(city) <= 4 and all('\u4e00' <= c <= '\u9fff' or c.isalnum() for c in city):
                logger.info(f"[参数提取] LLM 提取城市: {city}")
                return city
            else:
                logger.debug(f"[参数提取] LLM 提取结果无效: {city}")
                return ""
                
        except Exception as e:
            logger.debug(f"[参数提取] LLM 提取失败: {e}")
            return ""

    @staticmethod
    def _extract_weather_city(text: str) -> str:
        """从「查一下北京天气」这类句子里挑出可能的城市名。

        挑不出就返回空串 —— 空串代表「没指定城市」, 由客户端去查
        当前所在城市 (IP 定位), 而不是随便报一个假城市。

        **这里做的是「粗筛」, 不是最终判定**: 句子里可能混着「适合出门」
        这种没法穷举的修饰词。所以结果还要交给天气 API 校验 ——
        查不到这个名字就当「没指定城市」, 回落当前城市。
        """
        s = text.strip()

        # 先处理连接词和"再查时间"这样的后续任务标记
        # 「先查南京天气再查时间」-> 「先查南京天气」
        s = re.sub(r"(再|然后|接着|和|跟|以及)(查|看|问|说).*$", "", s)
        
        # 天气相关的词本身不是城市
        s = re.sub(r"(天气预报|天气|气温|温度|下雨|下雪|刮风|台风)", "", s)
        
        # 疑问词/副词/语气词 —— 都不属于城市名
        s = re.sub(
            r"(怎么样|怎么样了|如何|好不好|是否|能不能|可不可以|会不会|要不要"
            r"|适合|不适合|值得|需要|大概|大约|多少|几"
            r"|一下|一下的|吗|呢|啊|呀|哈|嘛|的)",
            "",
            s,
        )
        # 时间词
        s = re.sub(r"(今天|今日|明天|明日|后天|昨天|现在|目前|此刻|当下|今儿|时间)", "", s)

        # 动词前缀 —— 可能出现在城市名前面, 要**循环剥**:
        # 「帮我查一下北京」-> 删「一下」后变「帮我查北京」-> 还要再删「帮我查」
        # 「先查南京」-> 删后变「南京」
        for _ in range(4):
            new = re.sub(
                r"^(帮我|帮忙|给我|替我|请问|请|查询|查一下|查查|查|看看|看下|看下|先|后|想要|让|让我)",
                "",
                s,
            )
            if new == s:
                break
            s = new

        # 只保留中英文数字
        s = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s)
        # 少于 2 个字不可能是地名 —— 当成「没指定城市」
        return s if len(s) >= 2 else ""

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
            return await self._execute_weather(args)
        if name == "time":
            now = datetime.now()
            return "ok", f"现在是{now.hour}点{now.minute}分"
        if name == "date":
            now = datetime.now()
            week = "一二三四五六日"[now.weekday()]
            return "ok", f"今天是{now.month}月{now.day}日，星期{week}"
        if name == "describe_scene":
            return await self._execute_describe_scene(args)
        if name == "find_object":
            return await self._execute_find_object(args)
        if name == "grasp_object":
            return await self._execute_grasp_object(args)
        return "failed", "未知动作"

    # ------------------------------------------------------------ 天气

    async def _execute_weather(self, args: Dict[str, Any]) -> Tuple[str, str]:
        """查天气 -> 播报当前实况 + 今日预报。

        城市为空 = 用户没说城市 -> **反问用户**「查哪个城市」,
        而不是靠 IP 定位去猜 (IP 定位常常落在几百公里外, 是错的)。
        """
        city = str(args.get("city", "") or "").strip()
        # 「默认城市」这类占位词也当作「没指定」
        if city in ("默认城市", "默认", "当前城市", "本地"):
            city = ""

        try:
            report = await self.weather_client.query(city)
        except CityNotSpecified:
            return "ok", "想查哪个城市的天气呢？"
        except CityNotFound:
            logger.warning("[天气] 查不到城市 %r", city)
            return "failed", f"没找到{city}这个地方，换个城市名试试"
        except WeatherError as exc:
            logger.error("[天气] 查询失败: %s", exc)
            return "failed", "天气服务暂时用不了，请稍后再试"
        except Exception as exc:  # pragma: no cover - 兜底
            logger.exception("[天气] 查询异常")
            return "failed", "天气服务暂时用不了，请稍后再试"

        parts = [
            f"{report.city}现在{report.condition}",
            f"气温 {report.temperature:.0f} 度",
        ]
        if report.apparent and abs(report.apparent - report.temperature) >= 1:
            parts.append(f"体感 {report.apparent:.0f} 度")
        parts.append(f"湿度 {report.humidity}%")
        if report.wind:
            parts.append(f"风速 {report.wind:.0f} 公里每小时")
        parts.append(f"今天 {report.temp_min:.0f} 到 {report.temp_max:.0f} 度")
        if report.precip_prob:
            parts.append(f"降水概率 {report.precip_prob}%")

        return "ok", "，".join(parts) + "。"

    # ------------------------------------------------------------ 视觉

    #: 描述场景时最多报几类物体
    _SCENE_MAX_KINDS = 5

    async def _execute_describe_scene(self, args: Dict[str, Any]) -> Tuple[str, str]:
        """描述场景 -> 「我看到 2 个杯子、1 个人。」

        中文名用视觉服务返回的 label_zh, 没有就用原类名 (自定义中文模型)。
        不报位置和距离, 避免句子过长。
        """
        try:
            result = await self.vision_client.detect()
        except VisionError as exc:
            logger.error("[视觉] 描述场景失败: %s", exc)
            return "failed", "视觉服务暂时用不了，请稍后再试"

        counts: Dict[str, int] = {}
        for obj in result.get("objects") or []:
            name = str(obj.get("label_zh") or obj.get("label") or "").strip()
            if name:
                counts[name] = counts.get(name, 0) + 1
        if not counts:
            return "ok", "我现在没有看到明显的物体。"

        # 数量降序; 同数量保持首次出现的顺序
        ranked = sorted(counts.items(), key=lambda kv: -kv[1])
        shown = ranked[: self._SCENE_MAX_KINDS]
        text = "、".join(f"{n} 个{name}" for name, n in shown)
        reply = f"我看到 {text}"
        if len(ranked) > len(shown):
            reply += "，还有其他几样东西"
        return "ok", reply + "。"

    async def _execute_grasp_object(self, args: Dict[str, Any]) -> Tuple[str, str]:
        """抓取: 按流程定义跑一遍 (默认: 准备点->拍照->上方点->抓取点->安全点)。

        结果文案 (含失败原因) 直接念给用户。

        **动不动由机械臂服务决定** (``configs/arm.yaml`` 的
        ``arm.enabled`` 控制指令总闸) —— 这里无条件请求移动, 总闸关着
        的话 arm_service 会只算位置、不下发指令, 流程照走完。
        """
        from skill.builtin.vision_grasp import VisionGraspSkill

        target = str(args.get("name") or args.get("target") or "").strip()
        if not target:
            return "failed", "没说要抓什么物体"
        skill = VisionGraspSkill(
            VisionGraspSkill.spec(),
            None,
            vision_client=self.vision_client,
            arm_client=self.arm_client,
        )
        skill_args: Dict[str, Any] = {"target": target}
        # ``flow`` 让用户可以指定别的流程 (configs/flows/ 里定义的)
        for key in ("arm", "approach_distance", "duration", "flow"):
            if args.get(key) not in (None, ""):
                skill_args[key] = args[key]

        result = await skill.execute(skill_args)
        text = result.reply or result.error or result.output or "抓取没有完成"
        return ("ok" if result.ok else "failed"), text

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
            # fresh=True —— 「XX 在哪」问的是**此刻**的位置, 不是上一次
            # 有人看的时候的位置。走缓存会报出被挪动过的旧坐标。
            result = await self.vision_client.find(
                label, frame=frame, fresh=True
            )
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

    # ---- 复合查询处理 ----

    def _is_composite_query(self, text: str) -> bool:
        """检查是否是复合查询。
        
        复合查询特征：
        - 包含多个工具关键词（如「天气」和「时间」）
        - 包含「先...再...」「...然后...」等连接词
        """
        # 连接词标志
        connectors = [
            "先", "再", "然后", "之后", "接着", "随后", "紧接着",
            "和", "跟", "还有", "以及", "同时", "一起"
        ]
        
        has_connector = any(conn in text for conn in connectors)
        
        if not has_connector:
            return False
        
        # 检查是否有多个工具关键词
        tool_keywords = set()
        for keyword in ["天气", "时间", "日期", "几号", "星期", "周几"]:
            if keyword in text:
                tool_keywords.add(keyword)
        
        for keyword in ["查", "看", "告诉", "帮"]:
            if keyword in text:
                for obj in ["天气", "时间", "日期"]:
                    if obj in text:
                        tool_keywords.add(f"{keyword}-{obj}")
        
        return len(tool_keywords) >= 2

    async def _handle_composite(self, text: str, started: float) -> Optional[BranchResult]:
        """处理复合查询。
        
        尝试使用编排引擎解析和执行。
        """
        try:
            from task_parser import parse_task_sequence
            from parameter_validator import create_default_validator
            from orchestrator import create_orchestrator
            
            # 简单的任务识别逻辑（使用关键词匹配）
            tasks = []
            task_order = 1
            
            # 检查天气任务
            if any(w in text for w in ["天气", "温度", "气温", "下雨", "下雪"]):
                # 优先使用 LLM 精准提取，降级到正则
                city = await self._extract_city_with_llm(text)
                if not city:
                    city = self._extract_weather_city(text)
                
                tasks.append({
                    "tool": "weather",
                    "params": {"city": city},  # 始终包含 city，即使为空
                    "order": task_order
                })
                task_order += 1
            
            # 检查时间任务
            if any(w in text for w in ["时间", "几点", "现在几点", "几号", "今天几号", "周几", "星期"]):
                tasks.append({
                    "tool": "time",
                    "params": {},
                    "order": task_order
                })
                task_order += 1
            
            if len(tasks) < 2:
                return None  # 没有检测到复合任务
            
            # 构造 TaskSequence 格式
            task_json = {
                "tasks": tasks,
                "sequence_type": "sequential"
            }
            import json
            sequence_str = json.dumps(task_json)
            
            # 解析任务序列
            sequence = parse_task_sequence(sequence_str)
            
            if not sequence.is_valid or not sequence.tasks:
                logger.warning("[工具处理] 任务序列解析失败")
                return None
            
            # 参数验证
            validator = create_default_validator()
            for task in sequence.tasks:
                result = validator.validate(task.tool, task.params or {}, text)
                if result.is_valid:
                    task.params = result.params
            
            # 创建编排器并执行
            # 多个任务需要足够的时间：DNS 解析 + API 调用 + JSON 解析
            # 15 秒确保即使网络较慢也能完成
            orchestrator = create_orchestrator(self._execute_task_wrapper, timeout_per_task=15.0)
            orch_result = await orchestrator.execute(sequence)
            
            if not orch_result or not orch_result.aggregated_text:
                return None
            
            logger.info(f"[工具处理] 复合查询执行成功: {orch_result.aggregated_text}")
            
            # 返回聚合结果
            return BranchResult(
                reply=orch_result.aggregated_text,
                speak=True,
                action={
                    "name": "composite",
                    "args": {"tasks": len(sequence.tasks)},
                    "status": "success" if orch_result.all_success else "partial",
                    "detail": orch_result.aggregated_text,
                    "kind": "query",
                    "speak": True,
                },
                trace=[self._trace("composite", (time.time() - started) * 1000, f"{len(sequence.tasks)}-tasks")],
            )
        except Exception as e:
            logger.warning(f"[工具处理] 复合查询处理失败: {e}")
            return None

    async def _execute_task_wrapper(self, tool: str, params: dict) -> tuple:
        """编排引擎用的任务执行包装器。"""
        try:
            status, detail = await self.execute(tool, params)
            return ("ok", detail) if status == "ok" else ("failed", detail)
        except Exception as e:
            return ("failed", str(e))
