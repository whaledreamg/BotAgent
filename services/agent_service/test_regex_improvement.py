#!/usr/bin/env python3
"""
直接对比两个参数提取方法的实际性能
"""

import re
import time
import sys

# 测试用例
TEST_CASES = [
    ("先查南京天气再查时间", "南京"),
    ("先查一下北京天气然后查时间", "北京"),
    ("查天气和时间", ""),
    ("查南京天气", "南京"),
    ("南京天气怎么样", "南京"),
    ("查一下上海天气接着看时间", "上海"),
    ("帮我查一下杭州的天气", "杭州"),
    ("查广州和深圳的天气再看时间", "广州"),  # 只取第一个
    ("先看北京天气怎么样然后查现在几点", "北京"),
    ("查武汉天气后面再问时间", "武汉"),
    ("想知道成都天气", "成都"),
]


def extract_weather_city_old(text: str) -> str:
    """旧正则（改进前）"""
    s = text.strip()

    # 天气相关的词本身不是城市
    s = re.sub(r"(天气预报|天气|气温|温度|下雨|下雪|刮风|台风)", "", s)
    # 疑问词/副词/语气词
    s = re.sub(
        r"(怎么样|怎么样了|如何|好不好|是否|能不能|可不可以|会不会|要不要"
        r"|适合|不适合|值得|需要|大概|大约|多少|几"
        r"|一下|一下的|吗|呢|啊|呀|哈|嘛|的)",
        "",
        s,
    )
    # 时间词
    s = re.sub(r"(今天|今日|明天|明日|后天|昨天|现在|目前|此刻|当下|今儿|再|时间)", "", s)

    # 动词前缀
    for _ in range(4):
        new = re.sub(
            r"^(帮我|帮忙|给我|替我|请问|请|查询|查一下|查查|查|看看|看下|看下|先|后)",
            "",
            s,
        )
        # 也处理句末的动词词组
        new = re.sub(r"(再查|再看|接着查|接着看|然后查|然后看|和|跟|以及).*$", "", new)
        
        if new == s:
            break
        s = new

    # 只保留中英文数字
    s = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s)
    return s if len(s) >= 2 else ""


def extract_weather_city_new(text: str) -> str:
    """新正则（改进后）"""
    s = text.strip()

    # 先处理连接词和"再查时间"这样的后续任务标记
    s = re.sub(r"(再|然后|接着|和|跟|以及)(查|看|问|说).*$", "", s)
    
    # 天气相关的词本身不是城市
    s = re.sub(r"(天气预报|天气|气温|温度|下雨|下雪|刮风|台风)", "", s)
    
    # 疑问词/副词/语气词
    s = re.sub(
        r"(怎么样|怎么样了|如何|好不好|是否|能不能|可不可以|会不会|要不要"
        r"|适合|不适合|值得|需要|大概|大约|多少|几"
        r"|一下|一下的|吗|呢|啊|呀|哈|嘛|的)",
        "",
        s,
    )
    # 时间词
    s = re.sub(r"(今天|今日|明天|明日|后天|昨天|现在|目前|此刻|当下|今儿|时间)", "", s)

    # 动词前缀
    for _ in range(4):
        new = re.sub(
            r"^(帮我|帮忙|给我|替我|请问|请|查询|查一下|查查|查|看看|看下|看下|先|后|想要|让|让我|想知道|想|知道)",
            "",
            s,
        )
        if new == s:
            break
        s = new

    # 只保留中英文数字
    s = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s)
    return s if len(s) >= 2 else ""


def main():
    print("\n")
    print("╔" + "=" * 80 + "╗")
    print("║" + " " * 80 + "║")
    print("║" + "  正则改进对比测试 (改进前 vs 改进后)".center(80) + "║")
    print("║" + " " * 80 + "║")
    print("╚" + "=" * 80 + "╝")
    
    # 测试旧版本
    print("\n" + "=" * 80)
    print("📊 改进前正则方案")
    print("=" * 80)
    
    old_results = []
    old_time = 0
    old_passed = 0
    
    for text, expected in TEST_CASES:
        start = time.time()
        result = extract_weather_city_old(text)
        elapsed = (time.time() - start) * 1000
        old_time += elapsed
        
        status = "✓" if result == expected else "✗"
        if result == expected:
            old_passed += 1
        
        old_results.append((text, expected, result, elapsed, status))
        print(f"{status} '{text}' → '{result}' ({elapsed:.2f}ms)")
    
    # 测试新版本
    print("\n" + "=" * 80)
    print("📊 改进后正则方案")
    print("=" * 80)
    
    new_results = []
    new_time = 0
    new_passed = 0
    
    for text, expected in TEST_CASES:
        start = time.time()
        result = extract_weather_city_new(text)
        elapsed = (time.time() - start) * 1000
        new_time += elapsed
        
        status = "✓" if result == expected else "✗"
        if result == expected:
            new_passed += 1
        
        new_results.append((text, expected, result, elapsed, status))
        print(f"{status} '{text}' → '{result}' ({elapsed:.2f}ms)")
    
    # 统计对比
    print("\n" + "=" * 80)
    print("📈 性能对比总结")
    print("=" * 80)
    
    print(f"\n{'指标':<20} {'改进前':<20} {'改进后':<20} {'改进幅度':<20}")
    print("-" * 80)
    
    old_acc = 100 * old_passed / len(TEST_CASES)
    new_acc = 100 * new_passed / len(TEST_CASES)
    old_avg = old_time / len(TEST_CASES)
    new_avg = new_time / len(TEST_CASES)
    
    print(f"{'准确度':<20} {old_acc:>6.1f}% {'':<12} {new_acc:>6.1f}% {'':<12} {new_acc-old_acc:+.1f}%")
    print(f"{'平均耗时':<20} {old_avg:>6.2f}ms {'':<12} {new_avg:>6.2f}ms {'':<12} {new_avg-old_avg:+.2f}ms")
    print(f"{'总耗时':<20} {old_time:>6.2f}ms {'':<12} {new_time:>6.2f}ms {'':<12} {new_time-old_time:+.2f}ms")
    
    # 找出改进的地方
    print(f"\n🎯 改进详情:")
    improvements = []
    for i, (text, expected) in enumerate(TEST_CASES):
        old_result = old_results[i][2]
        new_result = new_results[i][2]
        
        old_ok = old_result == expected
        new_ok = new_result == expected
        
        if not old_ok and new_ok:
            improvements.append((text, old_result, new_result))
    
    if improvements:
        print(f"  ✨ 修复了 {len(improvements)} 个失败用例:")
        for text, old_res, new_res in improvements:
            print(f"    '{text}'")
            print(f"      改进前: '{old_res}' ✗")
            print(f"      改进后: '{new_res}' ✓")
    else:
        print(f"  (没有从失败改为成功的用例)")
    
    # 性能数据
    print(f"\n💡 结论:")
    if new_acc > old_acc:
        print(f"  ✓ 准确度提升: {old_acc:.1f}% → {new_acc:.1f}% (+{new_acc-old_acc:.1f}%)")
    if abs(new_avg - old_avg) < 0.01:
        print(f"  ✓ 速度持平: {old_avg:.2f}ms ≈ {new_avg:.2f}ms")
    elif new_avg < old_avg:
        print(f"  ✓ 速度更快: {old_avg:.2f}ms → {new_avg:.2f}ms ({(old_avg/new_avg):.1f}x)")
    else:
        print(f"  ⚠ 速度变慢: {old_avg:.2f}ms → {new_avg:.2f}ms (可以接受)")
    
    print(f"\n  📌 推荐: 使用改进后的方案")
    print(f"     - 准确度: {new_acc:.1f}%")
    print(f"     - 平均延迟: {new_avg:.2f}ms")
    print(f"     - 很好的性能/准确度权衡")
    
    print("\n" + "=" * 80)
    print("✅ 对比完成")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
