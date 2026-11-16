"""专业名词(热词)纠错。

## 为什么需要这一层

Vosk 的**大模型** (`vosk-model-cn-0.22`) 只有静态 `HCLG.fst`, 没有 `Gr.fst`,
所以**不支持运行时语法图** —— 没法在解码时把「公司名」塞进词表加权。
小模型 (`vosk-model-small-cn-0.22`) 有 `Gr.fst` 能这么做, 但小模型整体
精度差一截, 长句错误更多, 得不偿失。

于是走**后处理纠错**: 识别完之后, 把文本里「读音相近」的片段换成词表里的
正确写法。

- 纯本地, 零网络延迟 (语音场景对延迟极敏感, 一次 LLM 往返就是 1~3 秒)
- 不改 ASR 引擎, 不动模型, 只是文本后处理

## 错误的真实形态 (实测)

用本地 ChatTTS 合成含公司名的真实语音, 跑 Vosk 观察:

| 输入                      | Vosk 输出                        | 性质       |
| ------------------------- | -------------------------------- | ---------- |
| 打开星河科技的灯              | `打开 星河 科技 的 灯`                 | 正确       |
| 星河科技有限公司的机器人          | `星河 科技 有限公司 的 机器人`             | 正确       |
| 智元未来实验室的机器人           | `支援 未来 实验室 的 机器人`              | 音近替换    |
| 埃斯顿电气                    | `埃弗顿 惦记`                      | 音近替换    |

三条关键事实, 决定了算法形态:

1. **错输出全是词表内的合法词** (`支援`/`埃弗顿`/`至元` 都在 `words.txt` 里),
   不存在非法组合 —— 所以「找出词表外的片段」这种检测思路**不成立**。
2. **N-best alternatives 也不含正确公司名** (`智元` 的 N-best 是
   `至元/至于/日元`) —— 正确词根本不在解码图里, 后处理是唯一出路。
3. **置信度对错误的指示不普适** —— `埃弗顿` 的 conf 是 **1.000**,
   而 `星河` 是 0.669。即 **Vosk 会对「自信的错误」给出高置信度**。

## 判别策略: 加权打分

主判据是「**同音竞争者匹配**」, 置信度只作辅助权重 (不是硬门槛):

```
score = 拼音相似度 × 歧义因子 × 置信度因子
```

- **拼音相似度** —— `SequenceMatcher` 比对音节序列
- **歧义因子** —— 该热词拼音在 Vosk 全表中的**候选数**。
  唯一候选 (`ai si dun` 全表只有 `埃斯顿`) → 因子 1.0, 最可靠;
  多候选 → 因子随候选数下降, 趋于保守
- **置信度因子** —— 低置信度的词更容易被误纠, 略微降权;
  高置信度但**拼音完全匹配且唯一候选**时仍给满权重 (覆盖 `埃弗顿`
  conf=1.000 这种「自信的错误」)

另加三条硬约束防误纠:

- **首音节锚点** —— 窗口首音节必须与热词首音节完全一致 (专业名词首字
  发音最稳定)。「公安」不会去匹配「华为」; `阿斯顿`/`莱斯顿` 不会去
  匹配 `埃斯顿`
- **尾音节锚点** —— 窗口尾音节也必须与热词尾音节完全一致。
  这是实测出来的**最有效**的防误伤信号:

  | 样本         | 拼音      | 首同 | 尾同 | 结论 |
  | ----------- | --------- | ---- | ---- | ---- |
  | 埃弗顿(错)   | ai fu dun | ✓    | ✓    | 纠正 |
  | 埃尔顿(错)   | ai er dun | ✓    | ✓    | 纠正 |
  | 华位(错)     | hua wei   | ✓    | ✓    | 纠正 |
  | 机气人(错)   | ji qi ren | ✓    | ✓    | 纠正 |
  | 腾人(误伤)   | teng ren  | ✓    | ✗    | 不改 |
  | 腾现(误伤)   | teng xian | ✓    | ✗    | 不改 |

  专业名词的**首尾读音**都比中间稳定 (首字是品牌标识, 尾字多为
  「技术/集团/科技」这类后缀)。中间音节才容易被听错 —— 而那正是
  我们要纠正的部分。
- **音节数 ±1** —— 「华为」不会去匹配「化」字打头的一长串

## 词表语义

```yaml
- word: 华为                # 规范写法, 最终输出这个。必填
  aliases: [华位, 华渭]      # 常见误识别的写法。参与拼音比对
  keep: [Huawei]            # 已是正确写法, 命中后保护, 不参与模糊匹配
```

- ``aliases`` —— **要被纠正掉的错误写法**。它们参与模糊匹配的读音比对,
  但本身不是保护区。
- ``keep`` —— 已经正确的写法, 命中后**保护**该片段, 不再参与模糊匹配,
  避免把对的改错。

## 局限

读音完全不同的错 (比如把「麒麟」听成「奇林」) 拼音也救不回来 —— 那种情况
需要真正的上下文解码 (换 FunASR/Paraformer 的 hotword, 或者用 LLM 纠错)。
"""

from __future__ import annotations

import logging
import os
import re
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("hotwords")

try:  # pypinyin 是可选依赖 —— 没装就退化成「只做精确匹配」
    from pypinyin import Style, lazy_pinyin

    _HAS_PINYIN = True
except ImportError:  # pragma: no cover
    _HAS_PINYIN = False

#: 字符 -> 无声调拼音 的查表。
#:
#: 用它而不是 ``lazy_pinyin`` 是因为**速度差 60 倍** —— 构建歧义表要遍历
#: 80 万个词条, ``lazy_pinyin`` 要 17 秒, 查表只要 0.3 秒。
#: 语音服务启动时不能为了这个卡十几秒。
try:
    from pypinyin.contrib.tone_convert import to_normal
    from pypinyin import pinyin_dict

    _CHAR_PINYIN: Dict[str, str] = {
        chr(code): to_normal(pys.split(",")[0])
        for code, pys in pinyin_dict.pinyin_dict.items()
    }
except Exception:  # pragma: no cover - 退化成走 lazy_pinyin
    _CHAR_PINYIN = {}

logger.info(
    "热词纠错模块加载 (拼音支持: %s, 查表: %s)",
    "是" if _HAS_PINYIN else "否",
    "可用" if _CHAR_PINYIN else "不可用",
)

#: 非中文片段 (字母/数字) 在拼音比对里统一用小写原样保留
_ASCII_RE = re.compile(r"[A-Za-z0-9]+")

#: Vosk 中文模型的词表路径 —— 用来统计「某个拼音有多少个候选词」。
#: 找不到就退化为「不做歧义惩罚」(见 :class:`AmbiguityTable`)。
DEFAULT_VOCAB_PATHS = (
    os.path.expanduser("~/.cache/vosk/vosk-model-cn-0.22/graph/words.txt"),
    os.path.expanduser("~/.cache/vosk/vosk-model-small-cn-0.22/graph/words.txt"),
)

#: 纯中文词判定
_CHINESE_RE = re.compile(r"^[一-鿿]+$")


@lru_cache(maxsize=65536)
def to_pinyin(text: str) -> str:
    """把一段文本转成拼音音节序列 (无声调, 空格分隔)。

    非中文字符原样保留 (转小写), 这样「AI」这类中英混排的词也能比对。
    """
    if not text:
        return ""
    if not _HAS_PINYIN:
        return text.lower()
    if _CHAR_PINYIN:
        # 快路径: 逐字查表, 拼不出来的原样保留
        out: List[str] = []
        for ch in text:
            if "一" <= ch <= "鿿":
                out.append(_CHAR_PINYIN.get(ch, ch))
            else:
                out.append(ch.lower())
        return " ".join(out)
    parts = lazy_pinyin(
        text,
        style=Style.NORMAL,
        errors=lambda item: list(item),  # 非汉字部分原样返回
    )
    out = []
    for part in parts:
        if not part:
            continue
        if _ASCII_RE.fullmatch(part):
            out.append(part.lower())
        else:
            out.append(part)
    return " ".join(out)


def _similarity(a: str, b: str) -> float:
    """两个拼音序列的相似度 (0~1)。"""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


class AmbiguityTable:
    """拼音 -> 全表候选词数量的反查表。

    用来判断「一个热词的读音是否唯一」:

    - ``ai si dun`` 全表只有 ``埃斯顿`` 一个 → 该热词极可靠, 纠错门槛可放宽
    - ``hua wei`` 有 15 个候选 → 有歧义, 纠错要更保守

    这张表只依赖 Vosk 词表, **构建一次**即可 (不是每轮)。
    """

    def __init__(self, counts: Optional[Dict[str, int]] = None) -> None:
        self._counts = counts or {}
        self._available = bool(self._counts)

    @classmethod
    def from_vocab(cls, paths: Sequence[str] = DEFAULT_VOCAB_PATHS) -> "AmbiguityTable":
        """从 Vosk 词表构建反查表。

        词表很大 (82 万中文词条), 全量算拼音要十几秒 —— 只在启动时做一次,
        且用查表而非 ``lazy_pinyin`` (快 60 倍)。

        找不到词表就退化成「无歧义信息」, 此时歧义因子恒为 1.0。
        """
        for path in paths:
            if not path or not os.path.isfile(path):
                continue
            counts: Dict[str, int] = {}
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        word = line.split(" ", 1)[0]
                        if not word or not _CHINESE_RE.match(word):
                            continue
                        pinyin = to_pinyin(word)
                        if not pinyin:
                            continue
                        counts[pinyin] = counts.get(pinyin, 0) + 1
            except OSError as exc:  # pragma: no cover - 词表读不了不该致命
                logger.warning("[热词] 读取词表 %s 失败: %s", path, exc)
                continue
            logger.info(
                "[热词] 歧义表构建完成: %s (%d 个拼音)", path, len(counts)
            )
            return cls(counts)
        logger.warning("[热词] 找不到 Vosk 词表, 歧义因子恒为 1.0 (纠错会更激进)")
        return cls({})

    @property
    def available(self) -> bool:
        return self._available

    def count(self, pinyin: str) -> int:
        """某个拼音在全表有多少个候选词 (0 = 表里没有)。"""
        return self._counts.get(pinyin, 0)

    def factor(self, pinyin: str, weight: float) -> float:
        """歧义因子: 候选越少越可靠。

        - 0 个候选 (词表里没有这个音) → 1.0 (无从判断, 不惩罚)
        - 1 个候选 (唯一)          → 1.0 (最可靠)
        - N 个候选                 → ``1 / N ** weight`` 随 weight 衰减
        """
        if not self._available:
            return 1.0
        n = self.count(pinyin)
        if n <= 1:
            return 1.0
        return 1.0 / (n ** weight)


class Hotword:
    """一条热词记录。"""

    __slots__ = (
        "word",
        "aliases",
        "keep",
        "variants",
        "first",
        "last",
        "syllables",
        "unique",
    )

    def __init__(
        self,
        word: str,
        aliases: Sequence[str] = (),
        keep: Sequence[str] = (),
        unique: bool = False,
    ) -> None:
        self.word = word
        self.aliases = tuple(a for a in aliases if a)
        self.keep = tuple(k for k in keep if k)
        #: 候选读音: [(拼音串, 音节数, 音节列表)]
        #: 规范写法 + 所有误识别的写法
        variants: List[Tuple[str, int, List[str]]] = []
        for form in (self.word,) + self.aliases:
            pinyin = to_pinyin(form)
            if not pinyin:
                continue
            syllables = pinyin.split()
            variants.append((pinyin, len(syllables), syllables))
        self.variants = variants
        #: 首音节 —— 模糊匹配的锚点 (取规范写法的)
        self.first = variants[0][2][0] if variants and variants[0][2] else ""
        #: 尾音节 —— 模糊匹配的锚点 (取规范写法的)
        self.last = variants[0][2][-1] if variants and variants[0][2] else ""
        #: 目标音节数 —— 决定滑窗长度 (取规范写法的)
        self.syllables = variants[0][1] if variants else len(self.word)
        #: 该热词的读音在 Vosk 全表中是否唯一 —— 最强的可靠性信号
        self.unique = unique

    @property
    def protected_forms(self) -> List[str]:
        """已经是正确写法、不该被改动的形式。"""
        return [self.word] + list(self.keep)

    @property
    def min_len(self) -> int:
        """该热词的最短音节数 (规范写法 vs 别名可能长度不同)。"""
        lengths = [v[1] for v in self.variants if v[1]]
        return min(lengths) if lengths else 0


class Correction:
    """一次纠错的明细 (用于日志和 /state 排查)。"""

    __slots__ = ("before", "after", "changed", "hits")

    def __init__(
        self, before: str, after: str, hits: Optional[List[str]] = None
    ) -> None:
        self.before = before
        self.after = after
        self.changed = before != after
        self.hits = hits or []

    def as_dict(self) -> Dict[str, Any]:
        return {
            "before": self.before,
            "after": self.after,
            "changed": self.changed,
            "hits": list(self.hits),
        }


class HotwordCorrector:
    """按词表纠正识别文本里的专业名词。"""

    def __init__(
        self,
        terms: Iterable[Any],
        min_len: int = 2,
        fuzzy: bool = True,
        score: float = 0.72,
        min_score: float = 0.35,
        anchor: bool = True,
        tail_anchor: bool = True,
        require_in_vocab: bool = True,
        ambiguity_weight: float = 0.5,
        conf_weight: float = 0.3,
        conf_floor: float = 0.5,
        ambiguity: Optional[AmbiguityTable] = None,
    ) -> None:
        """
        Args:
            terms: 词表。每项可以是字符串, 也可以是
                ``{"word": "华为", "aliases": [...], "keep": [...]}``。
            min_len: 少于这么多**音节**的热词不做模糊匹配 —— 短词拼音撞车
                概率太高 (「安」能匹配「公安」), 只保留精确匹配。
            fuzzy: 是否做拼音模糊匹配。
            score: 拼音相似度阈值。
            anchor: 是否要求首音节完全相同。
            ambiguity_weight: 歧义因子里候选数的指数。0 = 不惩罚歧义,
                0.5 = 15 个候选时因子约 0.26。
            conf_weight: 置信度因子的最大衰减幅度 (0~1)。0 = 完全不看置信度。
            conf_floor: 低于此置信度的词额外降权 (更容易被误纠)。
            ambiguity: 歧义表。None 时自动从 Vosk 词表构建。
        """
        self.terms: List[Hotword] = []
        self.ambiguity = ambiguity if ambiguity is not None else AmbiguityTable.from_vocab()
        for item in terms or []:
            hotword = self._build(item)
            if hotword:
                self.terms.append(hotword)
        # 长词优先 —— 「华为技术」要先于「华为」被匹配, 否则会被短词先吃掉
        self.terms.sort(key=lambda t: len(t.word), reverse=True)
        self.min_len = max(1, int(min_len))
        self.fuzzy = bool(fuzzy) and _HAS_PINYIN
        self.score = float(score)
        self.min_score = float(min_score)
        self.anchor = bool(anchor)
        self.tail_anchor = bool(tail_anchor)
        self.require_in_vocab = bool(require_in_vocab)
        self.ambiguity_weight = float(ambiguity_weight)
        self.conf_weight = max(0.0, min(1.0, float(conf_weight)))
        self.conf_floor = float(conf_floor)

        if self.terms and not self.fuzzy:
            logger.warning("热词模糊匹配已关闭 (缺少 pypinyin?), 只做精确匹配")

        if self.terms:
            unique_cnt = sum(1 for t in self.terms if t.unique)
            logger.info(
                "[热词] 载入 %d 条, 其中 %d 条读音在全表唯一",
                len(self.terms),
                unique_cnt,
            )

    # ------------------------------------------------------------ 构建

    def _build(self, item: Any) -> Optional[Hotword]:
        if isinstance(item, str):
            word = item.strip()
            return self._make(word) if word else None
        if isinstance(item, dict):
            word = str(item.get("word") or "").strip()
            if not word:
                return None
            aliases = [str(a).strip() for a in (item.get("aliases") or [])]
            keep = [str(k).strip() for k in (item.get("keep") or [])]
            return self._make(word, aliases, keep)
        word = str(item or "").strip()
        return self._make(word) if word else None

    def _make(
        self, word: str, aliases: Sequence[str] = (), keep: Sequence[str] = ()
    ) -> Optional[Hotword]:
        if not word:
            return None
        hotword = Hotword(word, aliases, keep)
        # 「该音在全表唯一」是强信号, 直接查歧义表
        hotword.unique = self.ambiguity.available and self.ambiguity.count(
            hotword.variants[0][0] if hotword.variants else ""
        ) == 1
        return hotword

    # ------------------------------------------------------------ 精确匹配

    def _protected_spans(self, text: str) -> List[Tuple[int, int]]:
        """找出文本里「已经是正确写法」的片段区间。"""
        spans: List[Tuple[int, int]] = []
        for hotword in self.terms:
            for form in hotword.protected_forms:
                if not form:
                    continue
                start = 0
                while True:
                    idx = text.find(form, start)
                    if idx < 0:
                        break
                    spans.append((idx, idx + len(form)))
                    start = idx + len(form)
        return spans

    @staticmethod
    def _in_spans(pos: int, spans: Sequence[Tuple[int, int]]) -> bool:
        return any(start <= pos < end for start, end in spans)

    # ------------------------------------------------------------ 打分

    def _conf_factor(self, conf: Optional[float]) -> float:
        """置信度因子。

        低置信度的词**更容易被误纠** (ASR 自己都不确定), 所以降权;
        高置信度的词降权幅度小 —— 但**不会归零**, 因为存在「自信的错误」
        (`埃弗顿` conf=1.000 却是错的)。
        """
        if self.conf_weight <= 0.0 or conf is None:
            return 1.0
        c = max(0.0, min(1.0, float(conf)))
        if c >= self.conf_floor:
            # 0.5 -> 因子 1.0; 1.0 -> 因子 1 - conf_weight
            return 1.0 - self.conf_weight * (c - self.conf_floor) / max(
                1e-6, 1.0 - self.conf_floor
            )
        # 低于 floor 的更不可信, 继续降
        return max(0.3, 1.0 - self.conf_weight) * (c / max(1e-6, self.conf_floor))

    def _hotword_score(
        self,
        hotword: Hotword,
        win_pinyin: str,
        variant_pinyin: str,
        exact: bool,
        conf: Optional[float],
    ) -> float:
        """给一个候选窗口打分。

        分数 = 拼音相似度 × 歧义因子 × 置信度因子

        两级门槛:

        1. **相似度门槛** (``score``, 默认 0.72) —— 模糊匹配的读音必须
           足够像, 否则直接淘汰。这一步挡住「现在几点了」被误认成
           「机器人」(相似度只有 0.30)。
        2. **总分门槛** (``min_score``, 默认 0.35) —— 过了相似度门槛后,
           还要乘上歧义因子和置信度因子, 乘完不能低于这个值。

        **exact (窗口读音与某个 variant 完全一致)** 的处理是特殊的:
        歧义因子**不参与** —— 因为 ``aliases`` 是用户明确声明的「已知错法」,
        属于人工给定的 ground truth, 不该被自动歧义统计推翻。
        歧义因子只用来压制**模糊匹配**(靠相似度猜出来的), 避免模糊匹配
        抢走「精确命中」的优先级。

        ## 窗口读音的存在性检查 (关键防误伤信号)

        模糊匹配时, 额外检查**窗口自己的读音在 Vosk 全表里存不存在**:

        - 真实错法 (「机气人」) 的读音 ``ji qi ren`` 全表**存在** (1 个候选)
          —— 因为 ASR 只会输出词表里的合法词
        - 随机拼出的假词 (「机天人」) 的读音 ``ji tian ren`` 全表**不存在**
          (0 个候选) —— 那根本不是 ASR 可能输出的东西

        实测 (3000 条随机短语): 相似度达标的匹配里, **86%** 的窗口读音
        在全表不存在。用这个信号过滤, 能挡掉绝大部分误伤。

        注意: 这只对**模糊匹配**生效。精确命中 (``aliases`` 里人工声明的
        错法) 不受影响 —— 那些是人工给定的 ground truth, 全表查不到也认。
        """
        if exact:
            # 精确命中: 相似度满分, 置信度只作轻微调整 (但不会把它压到 0)
            return max(0.5, self._conf_factor(conf))

        # 模糊匹配: 窗口读音必须是「ASR 可能输出」的词
        if self.ambiguity.available and self.require_in_vocab:
            if self.ambiguity.count(win_pinyin) == 0:
                return 0.0

        sim = _similarity(win_pinyin, variant_pinyin)
        if sim < self.score:
            return 0.0
        amb = self.ambiguity.factor(
            hotword.variants[0][0] if hotword.variants else "",
            self.ambiguity_weight,
        )
        score = sim * amb * self._conf_factor(conf)
        return score if score >= self.min_score else 0.0

    # ------------------------------------------------------------ 模糊匹配

    def _best_match(
        self, segment: str, conf: Optional[float]
    ) -> Optional[Tuple[int, Hotword, float]]:
        """在 segment 开头找一个最佳匹配。

        返回 (匹配长度, 热词, 分数); 找不到返回 None。

        窗口长度由**候选读音的音节数**决定 —— 不能用「目标 ±1」这种
        宽松范围, 否则会截出「埃斯顿顿」这种多一个字的窗口。
        """
        best: Optional[Tuple[int, Hotword, float]] = None

        for hotword in self.terms:
            if hotword.min_len < self.min_len:
                continue
            for variant_pinyin, n_syll, _ in hotword.variants:
                if n_syll < self.min_len or n_syll > len(segment):
                    continue
                # 窗口长度 = 音节数 (中文 1 汉字 ≈ 1 音节)
                window = segment[:n_syll]
                win_pinyin = to_pinyin(window)
                if not win_pinyin:
                    continue
                # 锚点: 首音节必须完全一致
                # 「公安」不会匹配「华为」;「阿斯顿」不会匹配「埃斯顿」
                if self.anchor:
                    win_sylls = win_pinyin.split()
                    if not win_sylls:
                        continue
                    if win_sylls[0] != hotword.first:
                        continue
                    # 尾音节锚点 —— 实测最有效的防误伤信号。
                    # 专业名词首尾读音都比中间稳定 (首字是品牌标识, 尾字多是
                    # 「技术/集团」这类后缀), 而中间音节才是被听错的部分。
                    if self.tail_anchor and len(win_sylls) > 1:
                        if win_sylls[-1] != hotword.last:
                            continue
                exact = win_pinyin == variant_pinyin
                score = self._hotword_score(
                    hotword, win_pinyin, variant_pinyin, exact, conf
                )
                if score <= 0:
                    continue
                if best is None or score > best[2]:
                    best = (n_syll, hotword, score)
        return best

    # ------------------------------------------------------------ 入口

    def correct(
        self, text: str, words: Optional[Sequence[Dict[str, Any]]] = None
    ) -> Correction:
        """纠正文本里的专业名词, 返回纠错前后的文本。

        Args:
            text: 识别出的文本。
            words: 词级识别结果 (``[{"word": ..., "conf": ...}]``)。
                有它就能用置信度参与打分; 没有则只用拼音 + 歧义。
        """
        if not text or not self.terms:
            return Correction(text, text)

        conf_by_word = self._conf_map(words)
        spans = self._protected_spans(text)
        out: List[str] = []
        hits: List[str] = []
        pos = 0
        n = len(text)

        while pos < n:
            # 命中保护区间 —— 原样输出
            if self._in_spans(pos, spans):
                end = n
                for start, stop in spans:
                    if start <= pos < stop:
                        end = min(end, stop)
                        break
                out.append(text[pos:end])
                pos = end
                continue

            # 从当前位置往后取一段「未被保护」的区域, 在区域内部做模糊匹配
            seg_end = n
            for start, stop in spans:
                if start > pos:
                    seg_end = min(seg_end, start)
                    break
            segment = text[pos:seg_end]

            i = 0
            while i < len(segment):
                # 取当前窗口起始词的置信度 (取该词及后续窗口内词的最小值)
                conf = self._window_conf(segment, i, conf_by_word)
                match = self._best_match(segment[i:], conf)
                if match is None:
                    out.append(segment[i])
                    i += 1
                    continue
                length, hotword, score = match
                logger.debug(
                    "[热词纠错] %r -> %r (score %.3f)",
                    segment[i : i + length],
                    hotword.word,
                    score,
                )
                out.append(hotword.word)
                hits.append(hotword.word)
                i += length
            pos = seg_end

        result = "".join(out)
        if result != text:
            logger.info("[热词纠错] %r -> %r (命中 %s)", text, result, hits)
        return Correction(text, result, hits)

    @staticmethod
    def _conf_map(
        words: Optional[Sequence[Dict[str, Any]]],
    ) -> Dict[str, float]:
        """把词级结果转成 ``{词: 最低置信度}``。

        同一个词可能出现多次, 取**最低**那个 —— 只要有一次识别得不好,
        这个词就值得怀疑。
        """
        out: Dict[str, float] = {}
        for item in words or []:
            if not isinstance(item, dict):
                continue
            word = str(item.get("word") or "")
            conf = item.get("conf")
            if not word or conf is None:
                continue
            try:
                c = float(conf)
            except (TypeError, ValueError):
                continue
            if word not in out or c < out[word]:
                out[word] = c
        return out

    @staticmethod
    def _window_conf(
        segment: str,
        offset: int,
        conf_by_word: Dict[str, float],
    ) -> Optional[float]:
        """取窗口内所有词置信度的最小值 (没有置信度信息就返回 None)。"""
        if not conf_by_word:
            return None
        # 窗口长度未知, 这里保守地取「从 offset 开始连续能匹配到的词」的最小值
        best: Optional[float] = None
        idx = offset
        # 最多看 8 个字符 (热词一般不超过 8 字)
        limit = min(len(segment), offset + 8)
        while idx < limit:
            matched = False
            for word, conf in conf_by_word.items():
                if segment.startswith(word, idx):
                    best = conf if best is None else min(best, conf)
                    idx += len(word)
                    matched = True
                    break
            if not matched:
                break
        return best

    def known_words(self) -> List[str]:
        return [t.word for t in self.terms]

    def suspects(self, text: str) -> List[str]:
        """找出「读音像热词但写法不对」的片段。

        给后续 LLM 兜底纠错用 —— 只在有嫌疑时才值得花一次 LLM 往返。
        """
        if not text:
            return []
        result = self.correct(text)
        if not result.changed:
            return []
        return list(result.hits)


def load_corrector(spec: Any) -> Optional[HotwordCorrector]:
    """按配置构造纠错器。

    ``spec`` 可以是:
    - ``None`` / 空 -> 不启用
    - 词表列表 (字符串或 dict)
    - dict: ``{"enabled": bool, "terms": [...], "min_len": int, ...}``
    """
    if not spec:
        return None
    if isinstance(spec, dict):
        if not spec.get("enabled", True):
            return None
        terms = spec.get("terms") or []
        return HotwordCorrector(
            terms,
            min_len=int(spec.get("min_len", 2)),
            fuzzy=bool(spec.get("fuzzy", True)),
            score=float(spec.get("score", 0.72)),
            min_score=float(spec.get("min_score", 0.35)),
            anchor=bool(spec.get("anchor", True)),
            tail_anchor=bool(spec.get("tail_anchor", True)),
            require_in_vocab=bool(spec.get("require_in_vocab", True)),
            ambiguity_weight=float(spec.get("ambiguity_weight", 0.5)),
            conf_weight=float(spec.get("conf_weight", 0.3)),
            conf_floor=float(spec.get("conf_floor", 0.5)),
        )
    return HotwordCorrector(spec)
