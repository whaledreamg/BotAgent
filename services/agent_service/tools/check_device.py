#!/usr/bin/env python3
"""语音 -> 机械臂动作 的端到端自检。

验证 Agent 的 device 分支能不能把一句话变成机械臂真的动起来。

检查项:
1. 说法匹配: 每句话是否命中预期的动作名
2. 疑问句不被误判成动作指令
3. tool / device 分支互不干扰 (exclude 按规则隔离)
4. 真实下发: 调用 agent_service 的 /v1/dispatch, 确认 arm_service 收到并入控

第 4 项需要 agent_service 与 arm_service 都在运行 (arm_service 还要连着控制器)。

用法::

    python tools/check_device.py            # 只做匹配检查 (不需要服务)
    python tools/check_device.py --live     # 加上真实下发
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

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


#: 疑问句式 —— 这些话不该被判成动作指令
NEGATIVE_CASES = [
    "你怎么挥手",
    "介绍一下机械臂",
    "怎么挥手",
    "挥手是什么意思",
    "今天天气怎么样",
    "怎么伸手",
    "打包是什么意思",
]


def build_match_cases() -> list[tuple[str, str]]:
    """从 configs/actions.yaml 动态生成匹配用例。

    动作数据改了 (换动作、加说法), 这个用例表自动跟着变 ——
    不需要在测试代码里写死动作名。
    """
    from config import load_config

    cfg = load_config()
    cases: list[tuple[str, str]] = []
    for item in cfg.arm_actions.actions:
        action = str(item["action"])
        # 动作名本身当然要能匹配
        cases.append((action, action))
        # 每个说法都要能匹配到本动作
        for phrase in item.get("say") or []:
            cases.append((str(phrase), action))
    return cases


def test_matching() -> None:
    """说法匹配。"""
    print("\n[1] 说法匹配")
    from config import load_config
    from registry import Registry

    cfg = load_config()
    reg = Registry()
    reg.load(cfg)
    handler = reg.get("device")
    if handler is None:
        check("device 分支已装载", False)
        return
    check("device 分支已装载", True)

    check("映射表已加载", cfg.arm_actions.loaded)
    check("映射表非空", len(cfg.arm_actions.actions) > 0,
          f"{len(cfg.arm_actions.actions)} 条")

    for text, expected in build_match_cases():
        got, how = handler._match(text)
        check(f"「{text}」-> {expected}", got == expected, f"实际 {got} ({how})")

    print("\n[2] 疑问句不误判")
    for text in NEGATIVE_CASES:
        got, _ = handler._match(text)
        check(f"「{text}」不匹配动作", got is None, f"实际 {got}")


def test_router_isolation() -> None:
    """exclude 必须按规则隔离。"""
    print("\n[3] 分支隔离")
    from config import load_config
    from registry import Registry
    from router import Router

    cfg = load_config()
    reg = Registry()
    reg.load(cfg)
    router = Router(cfg, reg, None)

    cases = [
        ("打开空调", "tool"),
        ("查一下时间", "tool"),
        ("介绍一下北京", None),
    ]
    # 机械臂的说法从配置目录动态取 (取每个动作的第一个说法)
    for item in cfg.arm_actions.actions:
        phrases = item.get("say") or []
        if phrases:
            cases.append((str(phrases[0]), "device"))

    for text, expected in cases:
        hit = router.match_rules(text)
        got = hit[0] if hit else None
        check(f"「{text}」-> {expected}", got == expected, f"实际 {got}")


def _post(url: str, payload: dict, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _get(url: str, timeout: float = 8.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


def test_live(agent_url: str, arm_url: str) -> None:
    """真实下发。"""
    print("\n[4] 真实下发")

    try:
        health = _get(f"{arm_url}/health")
    except Exception as exc:
        check("arm_service 可达", False, str(exc))
        return
    check("arm_service 可达", health.get("status") == "ok",
          f"actions={health.get('actions')}")

    try:
        agent_health = _get(f"{agent_url}/health")
    except Exception as exc:
        check("agent_service 可达", False, str(exc))
        return
    branches = agent_health.get("branches") or []
    check("device 分支已装载", "device" in branches, str(branches))

    # 下发动作: 从配置目录动态取 (取最短的两个动作, 免得等太久)
    from config import load_config

    cfg = load_config()
    items = sorted(
        cfg.arm_actions.actions,
        key=lambda i: len(i.get("say") or []),
    )
    # 挑两个说法最多的动作 (说法多 = 好验证), 最多测 2 个
    picks = items[:2]
    for item in picks:
        action = str(item["action"])
        phrases = [str(p) for p in (item.get("say") or [])]
        if not phrases:
            continue
        text = phrases[0]
        resp = _post(
            f"{agent_url}/v1/dispatch",
            {"session_id": "check-device", "text": text, "use_history": False},
        )
        got = (resp.get("action") or {}).get("name")
        check(f"「{text}」下发成功", resp.get("intent") == "device"
              and got == action and (resp.get("action") or {}).get("status") == "ok",
              f"intent={resp.get('intent')} action={got} "
              f"status={(resp.get('action') or {}).get('status')}")
        check(f"「{text}」有语音确认", resp.get("speak") is True,
              repr(resp.get("reply")))

        # 等 arm_service 真正入控播放
        time.sleep(1.5)
        try:
            st = _get(f"{arm_url}/v1/status")
        except Exception as exc:
            check("arm_service 状态可读", False, str(exc))
            continue
        check(f"「{text}」arm_service 已收到",
              st.get("phase") in ("entering", "aligning", "playing")
              or st.get("action"),
              f"phase={st.get('phase')} action={st.get('action')}")

        # 等播完, 避免下一条撞上「已有动作正在播放」
        time.sleep(float(st.get("total_duration") or 4.0) + 2.0)


def main() -> int:
    ap = argparse.ArgumentParser(description="语音控制机械臂自检")
    ap.add_argument("--live", action="store_true", help="加上真实下发检查")
    ap.add_argument("--agent", default="http://127.0.0.1:8800")
    ap.add_argument("--arm", default="http://127.0.0.1:8802")
    args = ap.parse_args()

    print("=" * 60)
    print("语音 -> 机械臂 端到端自检")
    print("=" * 60)

    test_matching()
    test_router_isolation()
    if args.live:
        test_live(args.agent, args.arm)

    print("\n" + "=" * 60)
    if FAILED:
        print("存在失败项")
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
