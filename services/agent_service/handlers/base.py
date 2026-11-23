"""分支处理器抽象。

一个 Handler 代表一类语义分支, 由 router 决定把请求交给谁。
Handler 产出 BranchResult, 其中 speak 决定该结果是否要交给 TTS 朗读:
- speak=True  -> 文本进 TTS 合成并播放 (chat 分支)
- speak=False -> 直接执行动作, 不走 TTS (tool / device 分支)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: 朗读文本的**硬**字数上限 (最后的兜底, 不是主要手段)。
#:
#: 为什么需要: 系统提示词里的「不超过 40 个字」只是**建议**, 模型
#: 不一定遵守; 而 tool 分支的聚合文本 (orchestrator 的 "。".join(parts))
#: 更是完全绕过了提示词, 可以拼出几百字。
#:
#: **这个值是怎么定的**:
#:
#: 实测 TTS 合成耗时与字数基本**线性** (分段修复之后):
#:
#:   ==============  ==============  ==============
#:   字数            合成耗时        首帧
#:   ==============  ==============  ==============
#:   132 字          15.0s          229ms
#:   379 字          70.3s          868ms
#:   ==============  ==============  ==============
#:
#: 也就是说 300 字大约要 55 秒 —— 语音场景下这个等待已经很长了,
#: 但**不会**再出现「念到一半就没声」（那是分段修复前的问题）。
#:
#: 所以这里的闸是**防呆**, 不是「内容限制」: 300 字以内正常放行,
#: 超了才裁, 并明确告诉用户被裁了。
#:
#: 主要的「说短」责任在**提示词**上（configs/agent.yaml 的
#: system_prompt) —— 让模型自己写短, 比事后硬砍体验好得多。
MAX_SPEAK_CHARS = 300

#: 裁剪时给用户的提示语 —— 说清楚「被截了」, 别让用户以为机器人念完了
TRUNCATE_NOTICE = "内容有点长，我只说重点，"

#: 正文上限 —— 裁剪时正文保留这么多字, 加上前缀后总长会略超 MAX_SPEAK_CHARS
_BODY_MAX_CHARS = MAX_SPEAK_CHARS - len(TRUNCATE_NOTICE)


def clamp_reply(text: str, max_chars: int = MAX_SPEAK_CHARS) -> str:
    """把待朗读文本裁到 ``max_chars`` 以内, 并在开头说明被截断了。

    裁剪点优先落在标点边界上 —— 硬切会把词/句子切坏, 听起来很怪。

    **幂等**: 已经是「前缀 + 正文」形式的文本再裁一次, 不会叠加前缀。
    （修复: 上一轮裁剪后的结果带着前缀, 再次裁剪会再加一个前缀,
    用户听到「内容有点长我只说重点, 内容有点长我只说重点, ...」）

    返回值长度不超过 ``max_chars``。
    """
    s = (text or "").strip()
    if not s:
        return s

    # 幂等: 已经带前缀的, 先剥掉再裁, 避免叠加
    prefixed = s.startswith(TRUNCATE_NOTICE)
    body_src = s[len(TRUNCATE_NOTICE):].lstrip("，, ") if prefixed else s

    # 正文上限 —— 前缀不占正文额度
    body_max = max(1, max_chars - len(TRUNCATE_NOTICE))
    if len(body_src) <= body_max:
        # 没有超出, 原样返回 (但要保证带前缀的文本不再被裁)
        return (TRUNCATE_NOTICE + body_src) if prefixed else body_src

    body = body_src[:body_max]
    # 尽量切在句末 —— 找不到就在最近的逗号处切
    cut = -1
    for sep in ("。", "！", "？", "!", "?"):
        idx = body.rfind(sep)
        if idx > body_max * 0.5:
            cut = idx + 1
            break
    if cut < 0:
        idx = body.rfind("，")
        if idx > body_max * 0.5:
            cut = idx
    if cut > 0:
        body = body[:cut]
    else:
        # 连一个标点都找不到 —— 硬切会切坏词, 但总比超长好
        # （尽量多留一个字, 别把词劈开）
        body = body.rstrip("，,、；; ")
    return TRUNCATE_NOTICE + body


#: 需要在朗读前清掉的 markdown 标记。
#:
#: LLM 返回的文本常带 markdown (``**加粗**``、``# 标题``、``- 列表``、
#: ``\`代码\```)。这些标记会被 TTS **原样念出来** —— 用户听到的是
#: 「星号星号 加粗 星号星号」，非常突兀。
#:
#: 提示词里虽然写了「不要使用 markdown 语法」, 但模型并不总是遵守,
#: 所以这里做一层兜底净化。
_MD_PATTERNS = (
    # 水平分隔线 (--- / *** / ___), 独占一行
    (re.compile(r"(?m)^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$"), ""),
    # ``` 代码块围栏
    (re.compile(r"```[a-zA-Z0-9_+-]*"), ""),
    # 行内代码的反引号
    (re.compile(r"`"), ""),
    # 加粗 / 斜体
    (re.compile(r"\*\*"), ""),
    (re.compile(r"(?<!\*)\*(?!\*)"), ""),
    (re.compile(r"__"), ""),
    # 标题井号 (行首)
    (re.compile(r"(?m)^\s{0,3}#{1,6}\s*"), ""),
    # 引用 / 无序列表的行首标记
    (re.compile(r"(?m)^\s{0,3}[-*+]\s+"), ""),
    (re.compile(r"(?m)^\s{0,3}>\s*"), ""),
    # 表格分隔行
    (re.compile(r"(?m)^\s*\|?[\s:|-]{3,}\|[\s:|-]*$"), ""),
)


#: emoji 的匹配范围（代理对 + 变体选择符 + ZWJ 序列）
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF"
    "\U0001F1E0-\U0001F1FF\uFE0F\u200D]"
)


def strip_markdown(text: str) -> str:
    """去掉会被念出来的 markdown 标记。

    只清**标记符号**, 不动文字内容 —— 列表项会变成平铺的短句,
    朗读效果反而更自然。
    """
    s = text or ""
    for pattern, repl in _MD_PATTERNS:
        s = pattern.sub(repl, s)
    # 表格竖线 -> 逗号 (念作停顿)
    s = re.sub(r"[ \t]*\|[ \t]*", "，", s)
    # 多个空行压成一行
    s = re.sub(r"\n{2,}", "\n", s)
    # 行尾空格
    s = "\n".join(line.rstrip() for line in s.split("\n"))
    return s.strip()


def prepare_speak_text(text: str, max_chars: int = MAX_SPEAK_CHARS) -> str:
    """待朗读文本的**统一出口**: 净化 markdown -> 去 emoji -> 裁长度。

    所有要走 TTS 的文本都应该过这一道, 保证:
    - 不会被念出 markdown 符号
    - 不会因为太长而念不全
    """
    s = strip_markdown(text or "")
    # emoji 留着没意义 —— ChatTTS 的归一化会把它们当非法字符丢掉
    # (服务端日志里的 "found invalid characters"), 留着只会污染上下文
    s = _EMOJI_RE.sub("", s)
    # 换行在 TTS 里没有意义, 直接当逗号处理 (会被归一化)
    s = re.sub(r"\s*\n+\s*", "，", s)
    # 残留的孤立 markdown 符号 (--- 分隔线残留、列表符等)
    s = re.sub(r"(?m)^\s*(?:[-*_=~]{2,})\s*$", "", s)
    s = re.sub(r"[-*_=~]{3,}", "", s)
    # 列表编号 ("1. " / "2、" / "(3) ") —— 语音里没有视觉锚点,
    # 念「一、二、三」只是增加噪音, 去掉后直接连着讲更自然。
    # 但要保留数字本身 (「成立于1994年」不能被误伤)。
    s = re.sub(r"(?:^|(?<=[，。！？；、\s]))\d{1,2}\s*[.、)）]\s*(?=\S)", "", s)
    # 中文序数词开头 ("一、概况" / "第二点是") —— 「第二点」保留 (有语义),
    # 「一、概况」这种列表头去掉
    s = re.sub(r"(?:^|(?<=[，。！？；、\s]))[一二三四五六七八九十]{1,3}\s*[、.]\s*(?=\S)", "", s)
    # 连续标点压缩 (markdown 清理后可能留下 "，，")
    s = re.sub(r"[，、]{2,}", "，", s)
    # 「。，」/「：，」这种: 换行紧跟在句号/冒号后, 逗号是多余的
    s = re.sub(r"[。！？!?:：][，、]+", lambda m: m.group(0)[0], s)
    s = re.sub(r"[，、]{2,}", "，", s)
    # 标点前后多余空格
    s = re.sub(r"\s+([，。！？、：；,.!?])", lambda m: m.group(1), s)
    s = re.sub(r"([，。！？、：；])\s+", lambda m: m.group(1), s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return clamp_reply(s, max_chars=max_chars)


@dataclass
class AgentRequest:
    """进入 Agent 分流的请求。"""

    text: str
    session_id: str = "default"
    history: List[Dict[str, Any]] = field(default_factory=list)
    image_path: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)
    #: 本轮对话的唯一 id —— **分轮靠它, 不靠 session_id**。
    #:
    #: session_id 是常驻的会话标识 (比如 robot-01), 用户说一百句话它也不变;
    #: turn_id 每轮对话换一个, 监控端据此把事件归到对应的轮次。
    turn_id: str = ""


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
