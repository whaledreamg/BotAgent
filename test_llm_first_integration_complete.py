#!/usr/bin/env python3
"""LLM-First 参数提取系统集成验证测试"""

import asyncio
import sys
import logging
from pathlib import Path

# 添加 services/agent_service 到 Python 路径
sys.path.insert(0, str(Path(__file__).parent / "services" / "agent_service"))

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s][%(name)s] %(message)s"
)

async def test_skill_loading():
    """测试 Skill 定义加载"""
    print("\n" + "="*60)
    print("测试 1: Skill 定义加载")
    print("="*60)
    
    from handlers.tool_handler import ToolHandler
    from config import Config
    
    config = Config()
    handler = ToolHandler(config=config, llm=None)
    
    if handler._skills:
        print(f"✅ 成功加载 {len(handler._skills)} 个 Skill:")
        for skill_name, skill_spec in handler._skills.items():
            print(f"   - {skill_name}: {skill_spec.label} ({len(skill_spec.parameters)} 个参数)")
    else:
        print("⚠️  未加载到任何 Skill")
    
    return len(handler._skills) > 0

async def test_parameter_extraction_manager():
    """测试参数提取管理器"""
    print("\n" + "="*60)
    print("测试 2: 参数提取管理器初始化")
    print("="*60)
    
    from parameter_extraction_manager import ParameterExtractionManager
    from parameter_cache import ParameterCache
    
    manager = ParameterExtractionManager()
    print("✅ 参数提取管理器初始化成功")
    print(f"   - 缓存: {manager.cache is not None}")
    print(f"   - 思考日志: {manager.thinking_logger is not None}")
    print(f"   - LLM 提取器: {manager.llm_extractor is not None}")
    
    return True

async def test_thinking_logger():
    """测试思考日志记录器"""
    print("\n" + "="*60)
    print("测试 3: 思考日志记录器")
    print("="*60)
    
    from thinking_logger import ThinkingLogger
    from pathlib import Path
    
    logger = ThinkingLogger()
    print(f"✅ 思考日志记录器初始化成功")
    print(f"   - 日志目录: {logger.log_dir}")
    print(f"   - 目录存在: {logger.log_dir.exists()}")
    
    # 测试日志记录
    trace_id = logger.log_extraction(
        skill="weather",
        user_query="查北京天气",
        prompt="从用户查询中提取参数...",
        llm_response={"params": {"city": "北京"}},
        extracted_params={"city": "北京"},
        confidence=0.98,
        reasoning="识别到'北京'和'天气'关键词",
        total_ms=85.3,
        timing_breakdown={
            "stage_1_prompt": 1.2,
            "stage_2_llm_call": 82.1,
            "stage_3_parsing": 1.5,
        }
    )
    
    print(f"   - 日志记录成功, trace_id: {trace_id}")
    return True

async def test_query_api_routes():
    """测试查询 API 路由"""
    print("\n" + "="*60)
    print("测试 4: 查询 API 路由")
    print("="*60)
    
    try:
        from query_api import router
        
        # 查看路由
        routes = []
        for route in router.routes:
            if hasattr(route, 'path'):
                routes.append(f"{route.path} ({', '.join(route.methods) if hasattr(route, 'methods') else 'N/A'})")
        
        if routes:
            print(f"✅ 查询 API 已注册 {len(routes)} 个端点:")
            for route in sorted(routes):
                print(f"   - {route}")
        else:
            print("⚠️  未找到任何路由")
        
        return len(routes) > 0
    except Exception as e:
        print(f"❌ 导入查询 API 失败: {e}")
        return False

async def test_cache_functionality():
    """测试缓存功能"""
    print("\n" + "="*60)
    print("测试 5: 缓存功能")
    print("="*60)
    
    from parameter_cache import ParameterCache
    
    cache = ParameterCache()
    
    # 测试存储
    await cache.set(
        skill="weather",
        query="北京天气",
        params={"city": "北京"},
        confidence=0.98,
        ttl=3600
    )
    print("✅ 缓存存储成功")
    
    # 测试查询
    result = await cache.get("weather", "北京天气")
    if result:
        print(f"✅ 缓存查询成功: {result}")
    else:
        print("⚠️  缓存查询失败")
    
    return result is not None

async def main():
    """运行所有测试"""
    print("\n")
    print("╔" + "="*58 + "╗")
    print("║" + " "*58 + "║")
    print("║  LLM-First 参数提取系统 - 集成验证测试".center(58) + "║")
    print("║" + " "*58 + "║")
    print("╚" + "="*58 + "╝")
    
    results = []
    
    try:
        results.append(("Skill 加载", await test_skill_loading()))
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        results.append(("Skill 加载", False))
    
    try:
        results.append(("参数提取管理器", await test_parameter_extraction_manager()))
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        results.append(("参数提取管理器", False))
    
    try:
        results.append(("思考日志记录器", await test_thinking_logger()))
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        results.append(("思考日志记录器", False))
    
    try:
        results.append(("查询 API 路由", await test_query_api_routes()))
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        results.append(("查询 API 路由", False))
    
    try:
        results.append(("缓存功能", await test_cache_functionality()))
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        results.append(("缓存功能", False))
    
    # 总结
    print("\n" + "="*60)
    print("测试总结")
    print("="*60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✅" if result else "❌"
        print(f"{status} {name}")
    
    print(f"\n总计: {passed}/{total} 通过")
    
    if passed == total:
        print("\n🎉 所有测试通过！LLM-First 系统已完整集成。")
        return 0
    else:
        print(f"\n⚠️  有 {total - passed} 个测试失败")
        return 1

if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
