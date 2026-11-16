#!/usr/bin/env python3
"""热词纠错的离线自测。

不需要麦克风、不需要服务、不需要网络 —— 直接对文本跑纠错器。

用法:
    conda run -n asrt python tools/check_hotwords.py
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # services/asr_service
sys.path.insert(0, str(ROOT))

from hotwords import AmbiguityTable, HotwordCorrector  # noqa: E402

#: 测试用词表。**只放公司名** —— 通用词 (机器人/机械臂) 读音太常见,
#: 撞车概率高, 会误伤普通句子。
TERMS = [
    {"word": "埃斯顿", "aliases": ["埃弗顿", "埃尔顿", "艾尔顿", "爱斯顿"]},
    {"word": "汇川技术", "aliases": ["会川技术"]},
    {"word": "华为", "aliases": ["华位", "华渭", "华伪"]},
    {"word": "腾讯", "aliases": ["腾迅", "腾汛"]},
    {"word": "阿里巴巴", "aliases": ["阿里八八"]},
    {"word": "星河科技", "aliases": ["星合科技", "新河科技"]},
    {"word": "智元未来", "aliases": ["至元未来", "支援未来"]},
]

PASS = 0
FAIL = 0


def check(name: str, got: str, want: str) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [OK]   {name}")
        print(f"         {got!r}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")
        print(f"         得到: {got!r}")
        print(f"         期望: {want!r}")


def check_bool(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  ({detail})" if detail else ""))


def main() -> int:
    global PASS, FAIL

    print("=" * 70)
    print("热词纠错自测")
    print("=" * 70)

    t0 = time.perf_counter()
    ambiguity = AmbiguityTable.from_vocab()
    build_ms = (time.perf_counter() - t0) * 1000
    print(f"\n[歧义表] 构建耗时 {build_ms:.0f}ms, 可用={ambiguity.available}")
    for p in ("ai si dun", "hua wei", "xing he ke ji"):
        print(f"         {p}: {ambiguity.count(p)} 个候选")

    c = HotwordCorrector(TERMS, ambiguity=ambiguity)
    print(f"[纠错器] {len(c.known_words())} 条热词")

    # ---------------------------------------------------------- 1. 同音纠正
    print("\n--- 1. 同音/音近纠正 ---")
    check("埃弗顿 -> 埃斯顿", c.correct("埃弗顿的机器人").after, "埃斯顿的机器人")
    check("埃尔顿 -> 埃斯顿", c.correct("埃尔顿的伺服系统").after, "埃斯顿的伺服系统")
    check("华位 -> 华为", c.correct("打开华位的灯").after, "打开华为的灯")
    check("腾迅 -> 腾讯", c.correct("腾迅的股票涨了吗").after, "腾讯的股票涨了吗")
    check(
        "阿里八八 -> 阿里巴巴",
        c.correct("阿里八八的产品").after,
        "阿里巴巴的产品",
    )
    check("会川技术 -> 汇川技术", c.correct("会川技术的电机").after, "汇川技术的电机")
    check(
        "星合科技 -> 星河科技",
        c.correct("星合科技的产品").after,
        "星河科技的产品",
    )
    check(
        "至元未来 -> 智元未来",
        c.correct("至元未来的发布会").after,
        "智元未来的发布会",
    )

    # ---------------------------------------------------------- 2. 正确写法不被改坏
    print("\n--- 2. 正确写法不被二次改坏 ---")
    check("埃斯顿保持不变", c.correct("埃斯顿的机器人").after, "埃斯顿的机器人")
    check("华为保持不变", c.correct("打开华为的灯").after, "打开华为的灯")
    check(
        "多词混合保持不变",
        c.correct("埃斯顿和华为和腾讯").after,
        "埃斯顿和华为和腾讯",
    )

    # ---------------------------------------------------------- 3. 首音节锚点
    print("\n--- 3. 首音节锚点挡掉误配 ---")
    check_bool(
        "公安 不匹配 华为",
        "华为" not in c.correct("公安的执法记录仪").after,
        c.correct("公安的执法记录仪").after,
    )
    check_bool(
        "阿斯顿 不匹配 埃斯顿",
        "埃斯顿" not in c.correct("阿斯顿的汽车").after,
        c.correct("阿斯顿的汽车").after,
    )
    check_bool(
        "莱斯顿 不匹配 埃斯顿",
        "埃斯顿" not in c.correct("莱斯顿的公交车").after,
        c.correct("莱斯顿的公交车").after,
    )

    # ---------------------------------------------------------- 4. 尾音节锚点
    print("\n--- 4. 尾音节锚点挡掉误配 (实测最有效的信号) ---")
    # 「腾人」的拼音是 teng ren, 尾音节 ren != xun -> 不该匹配「腾讯」
    check_bool(
        "腾人 不匹配 腾讯",
        "腾讯" not in c.correct("腾人的东西").after,
        c.correct("腾人的东西").after,
    )
    check_bool(
        "腾现 不匹配 腾讯",
        "腾讯" not in c.correct("腾现的价格").after,
        c.correct("腾现的价格").after,
    )

    # ---------------------------------------------------------- 5. 词表外读音不匹配
    print("\n--- 5. 词表外读音不做模糊匹配 ---")
    # ASR 只会输出**词表内的合法词** —— 实测确认过 (错输出如「支援」「埃弗顿」
    # 都在 words.txt 里)。所以读音不在全表里的片段, 不可能是 ASR 的输出,
    # 不该被模糊匹配改写。
    #
    # 「小号手」读音是 xiao hao shou, 在全表里 (1 个候选) -> 会被纠正。
    # 而「小傻手」(xiao sha shou) 全表查不到 —— ASR 不可能输出它。
    c1 = HotwordCorrector([{"word": "小助手"}], min_len=2, ambiguity=ambiguity)
    check_bool(
        "词表外的「小傻手」不被改写",
        c1.correct("小傻手帮我挥挥手").after == "小傻手帮我挥挥手",
        c1.correct("小傻手帮我挥挥手").after,
    )
    check(
        "词表内的「小号手」-> 小助手",
        c1.correct("小号手帮我挥挥手").after,
        "小助手帮我挥挥手",
    )

    # ---------------------------------------------------------- 6. 正常文本不误伤
    print("\n--- 6. 正常文本不误伤 ---")
    for text in [
        "今天天气怎么样",
        "打开客厅的灯",
        "把空调开一下",
        "现在几点了",
        "明天会下雨吗",
        "北京今天天气晴气温二十四度",
        "请告诉我你在哪个城市",
        "我的快递什么时候到",
        "股市今天涨还是跌",
    ]:
        check(f"「{text}」原样返回", c.correct(text).after, text)

    # ---------------------------------------------------------- 7. 自信的错误
    print("\n--- 7. 「自信的错误」也能纠正 (conf=1.000) ---")
    # 实测: Vosk 把「埃斯顿」听成「埃弗顿」时 conf=1.000
    # 所以置信度不能当硬门槛
    words = [
        {"word": "埃弗顿", "conf": 1.0},
        {"word": "的", "conf": 1.0},
        {"word": "机器人", "conf": 1.0},
    ]
    result = c.correct("埃弗顿的机器人", words)
    check("conf=1.000 的埃弗顿仍被纠正", result.after, "埃斯顿的机器人")

    # ---------------------------------------------------------- 8. 歧义惩罚
    print("\n--- 8. 歧义惩罚: 唯一候选更激进, 多候选更保守 ---")
    check_bool(
        "埃斯顿的读音在全表唯一",
        ambiguity.count("ai si dun") == 1,
        f"ai si dun -> {ambiguity.count('ai si dun')}",
    )
    check_bool(
        "华为的读音在全表有多个候选",
        ambiguity.count("hua wei") > 1,
        f"hua wei -> {ambiguity.count('hua wei')}",
    )
    factor_unique = ambiguity.factor("ai si dun", 0.5)
    factor_multi = ambiguity.factor("hua wei", 0.5)
    check_bool(
        "唯一候选的歧义因子更高",
        factor_unique > factor_multi,
        f"唯一={factor_unique:.3f} 多候选={factor_multi:.3f}",
    )

    # ---------------------------------------------------------- 9. 精确匹配保护
    print("\n--- 9. keep 字段保护 ---")
    c2 = HotwordCorrector(
        [{"word": "埃斯顿", "keep": ["ESTUN"]}], ambiguity=ambiguity
    )
    check("ESTUN 保持不变", c2.correct("用 ESTUN 的机器人").after, "用 ESTUN 的机器人")

    # ---------------------------------------------------------- 10. 随机短语压力测试
    print("\n--- 10. 随机短语压力测试 (防误伤) ---")
    # 用常见字随机拼短语, 看有多少被误改。
    # 尾音节锚点加进来之前这个测试是 1.12%, 加完之后应该是 0%。
    common = list(
        "的了在是我有和就不人都一个上也很到说要去你会着没看好自己这现在"
        "天气开灯空调机械手机动电脑公司股票价格多少几点今天明天机电人"
        "天人支持支爱华腾埃星智"
    )
    random.seed(42)
    total = 4000
    bad = 0
    samples = []
    for _ in range(total):
        n = random.randint(3, 8)
        s = "".join(random.choice(common) for _ in range(n))
        r = c.correct(s)
        if r.changed:
            bad += 1
            if len(samples) < 5:
                samples.append((s, r.after, r.hits))
    check_bool(
        f"随机短语 {total} 条, 误伤 {bad} 条 ({bad / total * 100:.2f}%)",
        bad / total < 0.005,
        f"{bad}/{total}",
    )
    for s, a, h in samples:
        print(f"         {s!r} -> {a!r} {h}")

    # ---------------------------------------------------------- 11. 词表外公司名
    print("\n--- 11. 词表外公司名不被改坏 ---")
    r = c.correct("星河科技和智元未来")
    check_bool(
        "没有把正确写法改坏",
        "星河科技" in r.after and "智元未来" in r.after,
        r.after,
    )

    # ---------------------------------------------------------- 12. 性能
    print("\n--- 12. 性能 ---")
    text = "打开埃斯顿和华为的灯今天天气怎么样股票行情如何"
    c.correct(text)  # 预热
    t0 = time.perf_counter()
    n = 200
    for _ in range(n):
        c.correct(text)
    dt = (time.perf_counter() - t0) / n * 1000
    check_bool(
        f"单次纠错耗时 {dt:.2f}ms (< 20ms)",
        dt < 20,
        f"{dt:.2f}ms",
    )

    # ---------------------------------------------------------- 汇总
    print("\n" + "=" * 70)
    print(f"通过 {PASS} / {PASS + FAIL}")
    if FAIL:
        print(f"失败 {FAIL} 项")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
