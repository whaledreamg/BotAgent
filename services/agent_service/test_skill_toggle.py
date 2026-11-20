#!/usr/bin/env python3
"""skill / tool 的选择性关闭。

验证 configs/agent.yaml 的 `skills.disabled` 与 `skills.disabled_tools`:
- 被禁用的 skill 不注册进 SkillCatalog
- 被禁用的 tool 不出现在给模型的工具清单里
- 禁用 skill 会**连带**禁用依赖它的 tool (免得 skill 关了 tool 还留着)

运行: cd services/agent_service && python test_skill_toggle.py
"""

import sys

from action_tools import build_tool_specs
from config import load_config
from skill.catalog import SkillCatalog
from skill.loader import load_all_skills

failures = []


def check(cond, msg, detail=""):
    print(("  ok   " if cond else "  FAIL ") + msg + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(msg)


def load(disabled_skills=None, disabled_tools=None):
    cfg = load_config()
    cfg.disabled_skills = list(disabled_skills or [])
    cfg.disabled_tools = list(disabled_tools or [])
    catalog = SkillCatalog()
    load_all_skills(catalog, disabled=cfg.disabled_skills)
    return cfg, catalog, [s.name for s in build_tool_specs(cfg)]


def main():
    print("默认 (全开):")
    _, catalog, tools = load()
    skill_ids = [s.id for s in catalog.get_all_specs()]
    check("vision.grasp" in skill_ids, "vision.grasp 已注册")
    check("grasp_object" in tools, "grasp_object 在工具清单里")
    check("find_object" in tools, "find_object 在工具清单里")

    print("禁用 skill:")
    _, catalog, tools = load(disabled_skills=["vision.grasp"])
    skill_ids = [s.id for s in catalog.get_all_specs()]
    check("vision.grasp" not in skill_ids, "vision.grasp 未注册")
    check(
        "vision.recognize" in skill_ids and "weather" in skill_ids,
        "其它 skill 不受影响",
    )
    check("grasp_object" not in tools, "grasp_object 连带被禁用", str(tools))
    check("find_object" in tools, "find_object 不受影响")

    print("禁用 tool:")
    _, catalog, tools = load(disabled_tools=["find_object"])
    check("find_object" not in tools, "find_object 不在工具清单里")
    check(
        "grasp_object" in tools and "query_time" in tools,
        "其它 tool 不受影响",
    )
    check(
        "vision.grasp" in [s.id for s in catalog.get_all_specs()],
        "skill 不受影响",
    )

    print("同时禁用多个:")
    _, catalog, tools = load(disabled_skills=["weather", "time"], disabled_tools=["play_music"])
    skill_ids = [s.id for s in catalog.get_all_specs()]
    check("weather" not in skill_ids, "weather 未注册")
    check("time" not in skill_ids, "time 未注册")
    check("query_weather" not in tools, "query_weather 连带禁用")
    check("query_time" not in tools, "query_time 连带禁用")
    check("play_music" not in tools, "play_music 被禁用")

    print("禁用不存在的名字 (不该报错):")
    _, catalog, tools = load(
        disabled_skills=["nonexistent.skill"], disabled_tools=["nonexistent_tool"]
    )
    check(len(catalog.get_all_specs()) == 7, "skill 全部加载", str(len(catalog.get_all_specs())))
    check(len(tools) == 10, "工具清单完整", str(len(tools)))


if __name__ == "__main__":
    main()
    print(f"\n{'全部通过' if not failures else str(len(failures)) + ' 项失败'}")
    sys.exit(1 if failures else 0)
