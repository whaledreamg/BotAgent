"""Agent 追溯工具集成示例。

展示如何在 Agent 的 Tool Calling 中使用追溯系统。
"""

# ============================================================
# 示例 1: 简单查询最近识别的物体
# ============================================================

async def tool_query_cup_history(params: dict) -> str:
    """查询最近识别过的杯子。"""
    from agent_service.vision_client import VisionClient
    
    client = VisionClient()
    try:
        result = await client.traces_query(label="cup", limit=5)
        
        if result["count"] == 0:
            return "我还没有看到过杯子。"
        
        # 构建用户友好的回复
        reply = f"我最近看到过 {result['count']} 个杯子:\n"
        for i, record in enumerate(result["records"][-3:], 1):
            ts = record["timestamp"][:16]  # 只保留到分钟
            conf = int(record["confidence"] * 100)
            reply += f"{i}. {ts} - 置信度 {conf}%\n"
        
        return reply
    finally:
        await client.aclose()


# ============================================================
# 示例 2: 获取追溯统计并播报
# ============================================================

async def tool_recognition_summary(params: dict) -> str:
    """获取识别统计摘要 (适合 Agent 播报)。"""
    from agent_service.vision_client import VisionClient
    
    client = VisionClient()
    try:
        stats = await client.traces_stats()
        data = stats.get("stats", {})
        
        total = data.get("total_records", 0)
        if total == 0:
            return "我还没有识别过任何物体。"
        
        labels = data.get("labels", {})
        
        # 按识别数量排序
        sorted_labels = sorted(
            labels.items(),
            key=lambda x: x[1].get("count", 0),
            reverse=True
        )
        
        reply = f"到目前为止，我已经识别过 {total} 个物体:\n"
        for label, stats_item in sorted_labels[:5]:  # 只显示前 5 种
            count = stats_item.get("count", 0)
            conf = int(stats_item.get("last_confidence", 0) * 100)
            reply += f"  - {label}: {count} 个 (最近置信度 {conf}%)\n"
        
        if len(sorted_labels) > 5:
            reply += f"  ... 还有 {len(sorted_labels) - 5} 种物体"
        
        return reply
    finally:
        await client.aclose()


# ============================================================
# 示例 3: 按时间段查询识别记录
# ============================================================

async def tool_query_recent_detections(params: dict) -> str:
    """查询最近 N 分钟内的识别记录。"""
    from agent_service.vision_client import VisionClient
    from datetime import datetime, timedelta
    
    minutes = params.get("minutes", 10)  # 默认查询最近 10 分钟
    
    client = VisionClient()
    try:
        now = datetime.now()
        start = now - timedelta(minutes=minutes)
        
        result = await client.traces_query(
            start_time=start.isoformat(),
            end_time=now.isoformat(),
            limit=100
        )
        
        if result["count"] == 0:
            return f"最近 {minutes} 分钟内没有新的识别。"
        
        # 按类别分组统计
        by_label = {}
        for record in result["records"]:
            label = record["label"]
            by_label[label] = by_label.get(label, 0) + 1
        
        reply = f"最近 {minutes} 分钟内，我识别到:\n"
        for label, count in sorted(by_label.items(), key=lambda x: x[1], reverse=True):
            reply += f"  - {label}: {count} 个\n"
        
        return reply
    finally:
        await client.aclose()


# ============================================================
# 示例 4: 验证物体是否真的存在 (对比历史)
# ============================================================

async def tool_verify_object_exists(params: dict) -> str:
    """验证某个物体是否存在过。用于 Agent 判断。"""
    from agent_service.vision_client import VisionClient
    
    label = params.get("label", "")
    if not label:
        return "请指定物体类别。"
    
    client = VisionClient()
    try:
        result = await client.traces_query(label=label, limit=1)
        
        if result["count"] > 0:
            record = result["records"][0]
            conf = int(record["confidence"] * 100)
            return f"是的，我看到过 {label}，置信度是 {conf}%。"
        else:
            return f"我没有看到过 {label}。"
    finally:
        await client.aclose()


# ============================================================
# 示例 5: 导出识别历史 (用于日志/分析)
# ============================================================

async def tool_export_traces(params: dict) -> str:
    """导出识别历史到文件。"""
    import json
    from agent_service.vision_client import VisionClient
    from datetime import datetime
    
    client = VisionClient()
    try:
        # 查询所有记录
        result = await client.traces_query(limit=10000)
        records = result["records"]
        
        if not records:
            return "没有识别记录可导出。"
        
        # 保存到文件
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"traces_export_{timestamp}.json"
        
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, ensure_ascii=False)
        
        return f"已导出 {len(records)} 条记录到 {filename}"
    finally:
        await client.aclose()


# ============================================================
# 集成到 Agent Tool Router
# ============================================================

# 在 Agent 的 tool handler 中注册:
#
# async def handle_tool_call(tool_name: str, params: dict) -> str:
#     if tool_name == "query_cup_history":
#         return await tool_query_cup_history(params)
#     elif tool_name == "recognition_summary":
#         return await tool_recognition_summary(params)
#     elif tool_name == "query_recent_detections":
#         return await tool_query_recent_detections(params)
#     elif tool_name == "verify_object_exists":
#         return await tool_verify_object_exists(params)
#     elif tool_name == "export_traces":
#         return await tool_export_traces(params)
#     # ... 其他工具


# ============================================================
# Agent 提示词示例
# ============================================================

AGENT_SYSTEM_PROMPT_WITH_TRACES = """
你是一个具有视觉识别能力的机器人助手。你可以通过调用以下工具来追溯识别历史:

1. **query_cup_history** - 查询最近识别过的杯子
   返回值: 最近几次识别杯子的时间和置信度

2. **recognition_summary** - 获取识别统计摘要
   返回值: 已识别的所有物体类别及数量统计

3. **query_recent_detections** - 查询最近 N 分钟内的识别
   参数: minutes (可选，默认 10)
   返回值: 时间段内识别到的所有物体

4. **verify_object_exists** - 验证某个物体是否存在过
   参数: label (物体类别)
   返回值: 是否看到过该物体

5. **export_traces** - 导出所有识别历史
   返回值: 导出文件路径

当用户问到关于识别历史或统计时，使用这些工具。
"""


# ============================================================
# 测试脚本
# ============================================================

if __name__ == "__main__":
    import asyncio
    
    async def test():
        print("=" * 60)
        print("Agent 追溯工具测试")
        print("=" * 60)
        
        # 测试 1: 查询统计
        print("\n[工具] recognition_summary")
        result = await tool_recognition_summary({})
        print(result)
        
        # 测试 2: 查询特定物体
        print("\n[工具] query_cup_history")
        result = await tool_query_cup_history({})
        print(result)
        
        # 测试 3: 查询最近检测
        print("\n[工具] query_recent_detections")
        result = await tool_query_recent_detections({"minutes": 60})
        print(result)
        
        # 测试 4: 验证物体存在
        print("\n[工具] verify_object_exists")
        result = await tool_verify_object_exists({"label": "cup"})
        print(result)
    
    asyncio.run(test())
