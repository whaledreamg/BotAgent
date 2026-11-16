"""检查视觉识别追溯系统。

功能:
  - 查看追溯统计 (总共识别过多少物体)
  - 按类别查询识别历史 (最近识别过多少个 "杯子")
  - 查看识别时的图像 (追溯到当时拍的照片)
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Dict, Optional


async def main() -> None:
    """命令行工具。"""
    sys.path.insert(0, "/home/gxh/workspace/project/Bot Agent/services/agent_service")

    from vision_client import VisionClient

    client = VisionClient()

    print("\n=== 追溯系统统计 ===")
    try:
        stats = await client.traces_stats()
        print(f"统计信息: {stats}")
    except Exception as exc:
        print(f"❌ 获取统计失败: {exc}")
        await client.aclose()
        return

    print("\n=== 按类别查询 ===")
    try:
        result = await client.traces_query(label="cup", limit=10)
        print(f"查询结果: {result}")
        if result.get("records"):
            print(f"\n最近 {len(result['records'])} 条识别记录:")
            for record in result["records"][-3:]:  # 只显示最近 3 条
                img_url = None
                if record.get("image_filename"):
                    img_url = await client.traces_image_url(record["image_filename"])
                print(f"  - {record['label']} (置信度 {record['confidence']:.2%})")
                print(f"    时间: {record['timestamp']}")
                print(f"    位置: {record['bbox']}")
                if img_url:
                    print(f"    图像: {img_url}")
    except Exception as exc:
        print(f"❌ 查询失败: {exc}")

    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
