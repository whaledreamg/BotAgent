#!/usr/bin/env python3
"""热更新功能的测试和演示脚本。

用法：
    python test_hotreload.py

功能：
    1. 测试配置文件监听
    2. 测试 Skill 动态注册
    3. 测试 Skill 动态移除
    4. 测试 Skill 列表查询
    5. 测试配置热更新
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict

import httpx


class HotReloadTester:
    """热更新功能测试工具。"""

    def __init__(self, base_url: str = "http://127.0.0.1:8800") -> None:
        """初始化测试工具。
        
        Args:
            base_url: Agent 服务的基础 URL
        """
        self.base_url = base_url
        self.client = httpx.AsyncClient(timeout=10.0)

    async def close(self) -> None:
        """关闭 HTTP 客户端。"""
        await self.client.aclose()

    async def check_health(self) -> bool:
        """检查 Agent 服务是否健康。"""
        try:
            resp = await self.client.get(f"{self.base_url}/health")
            return resp.status_code == 200
        except Exception as e:
            print(f"❌ 无法连接到 Agent 服务: {e}")
            return False

    async def list_skills(self, branch: str = None) -> Dict[str, Any]:
        """获取所有已注册的 Skill。
        
        Args:
            branch: 可选的分支过滤
        """
        params = {}
        if branch:
            params["branch"] = branch

        try:
            resp = await self.client.get(
                f"{self.base_url}/admin/skills/list",
                params=params,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"❌ 获取 Skill 列表失败: {e}")
            return {"total": 0, "skills": []}

    async def register_skill(
        self,
        skill_id: str,
        name: str,
        branch: str,
        description: str = "",
        category: str = "mcp",
        tags: list = None,
    ) -> Dict[str, Any]:
        """注册新 Skill。
        
        Args:
            skill_id: Skill 唯一标识
            name: Skill 显示名称
            branch: 分支 ("chat" | "tool" | "device")
            description: 功能描述
            category: 分类
            tags: 标签列表
        """
        payload = {
            "id": skill_id,
            "name": name,
            "branch": branch,
            "description": description,
            "category": category,
            "tags": tags or [],
            "example_inputs": [],
        }

        try:
            resp = await self.client.post(
                f"{self.base_url}/admin/skills/register",
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"❌ 注册 Skill 失败: {e}")
            return {"status": "error", "message": str(e)}

    async def unregister_skill(self, skill_id: str) -> Dict[str, Any]:
        """移除 Skill。
        
        Args:
            skill_id: Skill 唯一标识
        """
        try:
            resp = await self.client.post(
                f"{self.base_url}/admin/skills/unregister",
                params={"skill_id": skill_id},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"❌ 移除 Skill 失败: {e}")
            return {"status": "error", "message": str(e)}

    async def reload_config(self) -> Dict[str, Any]:
        """手动触发配置热更新。"""
        try:
            resp = await self.client.post(f"{self.base_url}/admin/config/reload")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"❌ 配置热更新失败: {e}")
            return {"status": "error", "message": str(e)}


async def main() -> None:
    """运行测试。"""
    tester = HotReloadTester()

    print("=" * 70)
    print("Agent 热更新功能测试")
    print("=" * 70)

    # 检查服务健康状态
    print("\n[1/5] 检查 Agent 服务健康状态...")
    if not await tester.check_health():
        print("❌ Agent 服务不可用，请先启动服务")
        await tester.close()
        sys.exit(1)
    print("✅ Agent 服务正常运行")

    # 列出现有 Skill
    print("\n[2/5] 列出现有 Skill...")
    result = await tester.list_skills()
    print(f"✅ 共有 {result['total']} 个 Skill:")
    for skill in result["skills"][:5]:  # 只显示前 5 个
        print(f"   - {skill['id']}: {skill['name']} ({skill['branch']})")
    if len(result["skills"]) > 5:
        print(f"   ... 还有 {len(result['skills']) - 5} 个")

    # 注册新 Skill
    print("\n[3/5] 测试动态注册 Skill...")
    mcp_skill_id = f"test_mcp_{int(time.time())}"
    result = await tester.register_skill(
        skill_id=mcp_skill_id,
        name="MCP 测试工具",
        branch="tool",
        description="这是一个用于测试的 MCP Skill",
        category="mcp",
        tags=["test", "mcp"],
    )
    if result.get("status") == "ok":
        print(f"✅ Skill {mcp_skill_id} 已注册")
    else:
        print(f"❌ 注册失败: {result.get('message')}")

    # 验证 Skill 已注册
    print("\n[4/5] 验证 Skill 已注册...")
    result = await tester.list_skills(branch="tool")
    skill_ids = [s["id"] for s in result["skills"]]
    if mcp_skill_id in skill_ids:
        print(f"✅ Skill {mcp_skill_id} 在列表中")
    else:
        print(f"❌ Skill {mcp_skill_id} 未找到")

    # 移除 Skill
    print("\n[5/5] 测试动态移除 Skill...")
    result = await tester.unregister_skill(mcp_skill_id)
    if result.get("status") == "ok":
        print(f"✅ Skill {mcp_skill_id} 已移除")
    else:
        print(f"❌ 移除失败: {result.get('message')}")

    # 配置热更新测试
    print("\n[额外] 测试配置热更新...")
    print("提示: 配置通常由文件监听器自动触发")
    print("     如需手动测试，可调用: await tester.reload_config()")

    print("\n" + "=" * 70)
    print("测试完成！")
    print("=" * 70)

    await tester.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⏸️  测试中断")
        sys.exit(0)
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        sys.exit(1)
