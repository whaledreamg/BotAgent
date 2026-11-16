#!/usr/bin/env python3
"""
用真实 LLM 的对比测试

这会调用实际的 LLM API (agnes)
"""

import re
import asyncio
import time
import sys
from pathlib import Path

# 添加路径
sys.path.insert(0, str(Path(__file__).parent))

from llm import LLMClient
from config import Config, load_config

# 测试用例: (输入, 期望城市)
TEST_CASES = [
    ("先查南京天气再查时间", "南京"),
    ("先查一下北京天气然后查时间", "北京"),
    ("查天气和时间", ""),
    ("查南京天气", "南京"),
    ("南京天气怎么样", "南京"),
    ("查一下上海天气接着看时间", "上海"),
    ("帮我查一下杭州的天气", "杭州"),
    ("查广州和深圳的天气再看时间", "广州"),
    ("先看北京天气怎么样然后查现在几点", "北京"),
    ("查武汉天气后面再问时间", "武汉"),
    ("想知道成都天气", "成都"),
]


def extract_weather_city_regex(text: str) -> str:
    """正则方案"""
    s = text.strip()
    s = re.sub(r"(再|然后|接着|和|跟|以及)(查|看|问|说).*$", "", s)
    s = re.sub(r"(天气预报|天气|气温|温度|下雨|下雪|刮风|台风)", "", s)
    s = re.sub(
        r"(怎么样|怎么样了|如何|好不好|是否|能不能|可不可以|会不会|要不要"
        r"|适合|不适合|值得|需要|大概|大约|多少|几"
        r"|一下|一下的|吗|呢|啊|呀|哈|嘛|的)",
        "",
        s,
    )
    s = re.sub(r"(今天|今日|明天|明日|后天|昨天|现在|目前|此刻|当下|今儿|时间)", "", s)
    for _ in range(4):
        new = re.sub(
            r"^(帮我|帮忙|给我|替我|请问|请|查询|查一下|查查|查|看看|看下|看下|先|后|想要|让|让我|想知道|想|知道)",
            "",
            s,
        )
        if new == s:
            break
        s = new
    s = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s)
    return s if len(s) >= 2 else ""


async def extract_weather_city_llm(text: str, llm_client: LLMClient) -> str:
    """LLM 方案（真实）"""
    try:
        prompt = f"""从这句话里提取城市名。
句子: "{text}"

规则:
1. 返回只包含城市名, 不要其他文字
2. 如果没有提到城市, 返回空

城市名:"""
        
        response = await llm_client.complete(
            prompt,
            max_tokens=10,
            temperature=0,
        )
        
        city = response.strip()
        
        # 验证是否是合理的城市名
        if city and 2 <= len(city) <= 4 and all('\u4e00' <= c <= '\u9fff' or c.isalnum() for c in city):
            return city
        else:
            return ""
    except Exception as e:
        print(f"  ⚠ LLM 调用失败: {e}")
        return ""


async def main():
    print("\n")
    print("╔" + "=" * 78 + "╗")
    print("║" + " " * 78 + "║")
    print("║" + "  真实 LLM 对比测试 (Agnes)".center(78) + "║")
    print("║" + " " * 78 + "║")
    print("╚" + "=" * 78 + "╝")
    
    # 初始化 LLM
    try:
        cfg = load_config()
        llm_client = LLMClient(cfg.llm)
        print(f"\n✓ LLM 客户端初始化成功: {cfg.llm.default_provider or 'default'}")
    except Exception as e:
        print(f"✗ LLM 初始化失败: {e}")
        import traceback
        traceback.print_exc()
        return
    
    # 测试正则方案
    print("\n" + "=" * 80)
    print("📊 测试正则方案")
    print("=" * 80)
    
    regex_results = []
    regex_time = 0
    regex_passed = 0
    
    for text, expected in TEST_CASES:
        start = time.time()
        result = extract_weather_city_regex(text)
        elapsed = (time.time() - start) * 1000
        regex_time += elapsed
        
        status = "✓" if result == expected else "✗"
        if result == expected:
            regex_passed += 1
        
        regex_results.append((text, expected, result, elapsed, status))
        print(f"{status} '{text}' → '{result}' ({elapsed:.2f}ms)")
    
    # 测试 LLM 方案
    print("\n" + "=" * 80)
    print("📊 测试 LLM 方案 (真实调用)")
    print("=" * 80)
    
    llm_results = []
    llm_time = 0
    llm_passed = 0
    
    for i, (text, expected) in enumerate(TEST_CASES, 1):
        print(f"\n[{i}/{len(TEST_CASES)}] '{text}'")
        start = time.time()
        result = await extract_weather_city_llm(text, llm_client)
        elapsed = (time.time() - start) * 1000
        llm_time += elapsed
        
        status = "✓" if result == expected else "✗"
        if result == expected:
            llm_passed += 1
        
        llm_results.append((text, expected, result, elapsed, status))
        print(f"  {status} 期望: '{expected}' | 提取: '{result}' ({elapsed:.2f}ms)")
    
    # 打印统计
    print("\n" + "=" * 80)
    print("📈 统计对比")
    print("=" * 80)
    
    print(f"\n正则方案:")
    print(f"  通过率: {regex_passed}/{len(TEST_CASES)} ({100*regex_passed/len(TEST_CASES):.1f}%)")
    print(f"  平均耗时: {regex_time/len(TEST_CASES):.2f}ms")
    print(f"  总耗时: {regex_time:.2f}ms")
    
    print(f"\nLLM 方案:")
    print(f"  通过率: {llm_passed}/{len(TEST_CASES)} ({100*llm_passed/len(TEST_CASES):.1f}%)")
    print(f"  平均耗时: {llm_time/len(TEST_CASES):.2f}ms")
    print(f"  总耗时: {llm_time:.2f}ms")
    
    print(f"\n📊 性能指标对比:")
    print(f"{'指标':<20} {'正则':<20} {'LLM':<20} {'差异':<15}")
    print("-" * 75)
    
    regex_acc = 100 * regex_passed / len(TEST_CASES)
    llm_acc = 100 * llm_passed / len(TEST_CASES)
    regex_avg = regex_time / len(TEST_CASES)
    llm_avg = llm_time / len(TEST_CASES)
    
    print(f"{'准确度':<20} {regex_acc:>6.1f}% {'':<12} {llm_acc:>6.1f}% {'':<12} {llm_acc-regex_acc:+.1f}%")
    print(f"{'平均延迟':<20} {regex_avg:>6.2f}ms {'':<12} {llm_avg:>6.2f}ms {'':<12} {llm_avg-regex_avg:+.2f}ms")
    
    # 找出差异
    print(f"\n🎯 差异分析:")
    different = []
    for i, (text, expected) in enumerate(TEST_CASES):
        regex_res = regex_results[i][2]
        llm_res = llm_results[i][2]
        if regex_res != llm_res:
            different.append((text, expected, regex_res, llm_res))
    
    if different:
        print(f"  发现 {len(different)} 个差异结果:")
        for text, expected, regex_res, llm_res in different:
            regex_ok = "✓" if regex_res == expected else "✗"
            llm_ok = "✓" if llm_res == expected else "✗"
            print(f"    '{text}'")
            print(f"      正则 {regex_ok}: '{regex_res}'")
            print(f"      LLM  {llm_ok}: '{llm_res}'")
    else:
        print(f"  ✓ 所有用例结果相同")
    
    print("\n💡 结论:")
    if llm_acc > regex_acc:
        speedup = llm_avg / regex_avg if regex_avg > 0 else 0
        print(f"  ✓ LLM 方案精准度更高 ({llm_acc:.1f}% vs {regex_acc:.1f}%)")
        print(f"    但延迟是正则的 {speedup:.0f}x")
        if speedup < 20:  # 如果在可接受范围
            print(f"    📌 建议: 如果能容忍 {llm_avg:.0f}ms 延迟，LLM 更优")
        else:
            print(f"    📌 建议: 延迟过大，不如用正则")
    else:
        print(f"  ✓ 两者准确度相当 (正则{regex_acc:.1f}% vs LLM{llm_acc:.1f}%)")
        print(f"    正则方案快 {llm_avg/regex_avg:.0f}x")
        print(f"    📌 建议: 优先使用正则方案")
    
    print("\n" + "=" * 80)
    print("✅ 测试完成")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
