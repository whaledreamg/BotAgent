#!/usr/bin/env python3
"""Tool Handler 集成验证"""

from handlers.tool_handler import ToolHandler
from config import load_config

config = load_config()
handler = ToolHandler(config=config, llm=None)

print(f"\n✅ ToolHandler 初始化成功")
print(f"   - 加载的 Skills: {len(handler._skills)}")

if handler._skills:
    print("\n   已加载的 Skills:")
    for skill_name, skill_spec in list(handler._skills.items()):
        params_list = [p.name for p in skill_spec.parameters]
        params_str = ', '.join(params_list) if params_list else "无参数"
        print(f"   - {skill_name}: {skill_spec.label}")
        print(f"     参数: {params_str}")
        print(f"     类型: {skill_spec.category}")

print(f"\n✅ Tool Handler 集成验证完成！")
