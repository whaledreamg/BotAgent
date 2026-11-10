#!/usr/bin/env python3
"""校验 configs/actions.yaml 与机械臂动作库是否对得上。

两边必须一致:
- 配置里的 action 必须是动作库里真实存在的文件名 (否则 arm_service 返回 404)
- 动作库里的每个动作最好都配了说法 (否则语音说不到它)

用法::

    python tools/check_actions.py            # 直接读本地文件
    python tools/check_actions.py --live     # 顺便问一次运行中的 arm_service
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parents[1]
ROOT = SERVICE_DIR.parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

ACTIONS_YAML = ROOT / "configs" / "actions.yaml"
ARM_DATA = SERVICE_DIR.parent / "arm_service" / "data"
#: 动作库现在都是「一个文件一个动作」, 没有需要排除的旧版大文件
LEGACY_FILES: list[str] = []

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global FAILED
    if ok:
        print(f"  [{PASS}] {name}" + (f"  ({detail})" if detail else ""))
    else:
        print(f"  [{FAIL}] {name}" + (f"  ({detail})" if detail else ""))
        FAILED = 1
    return ok


def load_config_actions() -> list[dict]:
    """读 configs/actions.yaml 的 arm.actions 列表。"""
    import yaml

    data = yaml.safe_load(ACTIONS_YAML.read_text(encoding="utf-8")) or {}
    arm = data.get("arm") or {}
    items: list[dict] = []
    for item in arm.get("actions") or []:
        name = str(item.get("action", "") or "").strip()
        if not name:
            continue
        items.append(
            {
                "action": name,
                "say": [str(x).strip() for x in (item.get("say") or [])],
            }
        )
    return items


def load_library_actions() -> list[str]:
    """从 arm_service 的动作库读动作名。

    arm_service 不是 Python 包, 直接把它的目录加进 sys.path 来 import。
    """
    arm_dir = SERVICE_DIR.parent / "arm_service"
    if str(arm_dir) not in sys.path:
        sys.path.insert(0, str(arm_dir))
    from library import ActionLibrary

    lib = ActionLibrary(ARM_DATA, exclude_files=LEGACY_FILES)
    lib.load()
    return lib.labels()


def main() -> int:
    ap = argparse.ArgumentParser(description="校验动作映射表")
    ap.add_argument("--live", action="store_true", help="顺便问运行中的 arm_service")
    args = ap.parse_args()

    print("=" * 60)
    print("动作映射表校验")
    print("=" * 60)

    print("\n[1] 动作库")
    names = load_library_actions()
    check("动作库非空", len(names) > 0, f"{len(names)} 个动作")

    print("\n[2] 映射表")
    items = load_config_actions()
    check("映射表非空", len(items) > 0, f"{len(items)} 条")
    mapped = {str(i.get("action", "")) for i in items}
    check("动作名都非空", "" not in mapped, f"{len(mapped)} 个不同动作")

    missing_say = [i["action"] for i in items if not i.get("say")]
    check("每条都有说法", not missing_say, ", ".join(missing_say) or "全部有")

    # 说法之间可能互相包含 ("伸左手" 含 "伸手"), 这本身不是问题 ——
    # 匹配用**最长说法优先**, 所以文件之间不需要排先后顺序。
    # 但要确认没有两条说法完全一样却指向不同动作 (那就没法判断了)。
    phrase_owner: dict[str, str] = {}
    conflict = []
    for item in items:
        action = str(item["action"])
        for phrase in item.get("say") or []:
            phrase = str(phrase)
            if not phrase:
                continue
            owner = phrase_owner.get(phrase)
            if owner and owner != action:
                conflict.append(f"「{phrase}」-> {owner} / {action}")
            phrase_owner[phrase] = action
    check("没有重复的说法指向不同动作", not conflict,
          "; ".join(conflict[:3]) or "无冲突")

    # 最长说法优先要能真正消解包含关系: 「伸左手」比「伸手」长
    contain = []
    for phrase_a, act_a in phrase_owner.items():
        for phrase_b, act_b in phrase_owner.items():
            if phrase_a == phrase_b or act_a == act_b:
                continue
            if phrase_b in phrase_a and len(phrase_b) < len(phrase_a):
                contain.append((phrase_a, act_a, phrase_b, act_b))
    # 存在包含关系是正常的 (只要长的确实更长, 最长优先就能消解)
    unresolvable = [c for c in contain if len(c[2]) >= len(c[0])]
    check("包含关系可由最长优先消解", not unresolvable,
          "; ".join(f"{a}/{b}" for a, _, b, _ in unresolvable[:3])
          or f"{len(contain)} 处包含关系")

    print("\n[3] 两边对齐")
    library = set(names)
    unknown = sorted(mapped - library)
    check("配置的动作都存在", not unknown, ", ".join(unknown) or "全部存在")

    silent = sorted(library - mapped)
    check("动作库都有说法", not silent, ", ".join(silent) or "全部覆盖")

    if args.live:
        print("\n[4] 运行中的 arm_service")
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:8802/v1/actions", timeout=5
            ) as resp:
                body = json.load(resp)
            live = set(body.get("labels") or [])
            check("运行中的动作库与本地一致", live == library,
                  f"运行 {len(live)} / 本地 {len(library)}")
        except Exception as exc:
            check("arm_service 可达", False, str(exc))

    print("\n" + "=" * 60)
    if FAILED:
        print("存在失败项")
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
