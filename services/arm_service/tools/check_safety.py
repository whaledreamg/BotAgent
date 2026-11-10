#!/usr/bin/env python3
"""安全门阀自检。

不连真机 —— 直接用假的 robot/status 快照喂门阀, 验证每一项的判定
与播报文案。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from safety import SafetyGate  # noqa: E402

FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global FAILED
    if ok:
        print(f"  [PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {name}" + (f"  ({detail})" if detail else ""))


def healthy() -> dict:
    """一切正常的状态快照。"""
    return {
        "mode": "CSPPassthrough",
        "channel": "Planned",
        "faulted": False,
        "faultCode": 0,
        "enabled": True,
        "emergencyStopActive": False,
        "safetyResponse": "none",
        "safetyResponseCode": 0,
        "safetyPhase": "none",
        "safetyPhaseCode": 0,
        "joints": [0.0] * 7,
    }


def main() -> int:
    gate = SafetyGate()

    print("\n[1] 正常状态 -> 放行")
    report = gate.check(healthy(), connected=True)
    check("全部通过", report.ok, f"reason={report.reason!r}")
    check("检查项齐全", len(report.checks) == 8, f"{len(report.checks)} 项")
    check("播报文案为空", gate.speak_text(report) == "")

    print("\n[2] 安全锁定 (faulted) -> 拒绝")
    st = healthy()
    st["faulted"] = True
    st["faultCode"] = 13220
    report = gate.check(st, connected=True)
    check("不通过", not report.ok)
    check("code=faulted", report.code == "faulted", report.code)
    check("播报提到安全锁定", "安全锁定" in report.reason, report.reason)
    check("播报文案带处理建议", "处理" in gate.speak_text(report))

    print("\n[3] 急停 -> 拒绝")
    st = healthy()
    st["emergencyStopActive"] = True
    report = gate.check(st, connected=True)
    check("不通过", not report.ok)
    check("code=emergency_stop", report.code == "emergency_stop", report.code)
    check("播报提到急停", "急停" in report.reason, report.reason)

    print("\n[4] 安全响应 (servoQuickStop) -> 拒绝")
    st = healthy()
    st["safetyResponse"] = "servoQuickStop"
    st["safetyResponseCode"] = 13222
    report = gate.check(st, connected=True)
    check("不通过", not report.ok)
    check("code=safety_response", report.code == "safety_response", report.code)
    check("播报提到安全保护", "安全保护" in report.reason, report.reason)

    print("\n[5] 安全减速中 -> 拒绝")
    st = healthy()
    st["safetyPhase"] = "pathDecelerating"
    report = gate.check(st, connected=True)
    check("不通过", not report.ok)
    check("code=safety_phase", report.code == "safety_phase", report.code)

    print("\n[6] 未上使能 -> 拒绝")
    st = healthy()
    st["enabled"] = False
    report = gate.check(st, connected=True)
    check("不通过", not report.ok)
    check("code=enabled", report.code == "enabled", report.code)
    check("播报提到使能", "使能" in report.reason, report.reason)

    print("\n[7] 控制器未连接 -> 拒绝")
    report = gate.check(healthy(), connected=False)
    check("不通过", not report.ok)
    check("code=connected", report.code == "connected", report.code)

    print("\n[8] 读不到状态 -> 拒绝 (不能盲动)")
    report = gate.check(None, connected=True)
    check("不通过", not report.ok)
    check("code=status", report.code == "status", report.code)

    print("\n[9] 模式不是位置控制 -> 拒绝")
    for mode, expect in (
        ("Disabled", "没上电"),
        ("CSPRescue", "救援模式"),
        ("CSTDrag", "拖动模式"),
        ("CSVZeroSpeed", "零速保护"),
    ):
        st = healthy()
        st["mode"] = mode
        report = gate.check(st, connected=True)
        check(f"{mode} 不通过", not report.ok)
        check(f"{mode} code=mode", report.code == "mode", report.code)
        check(f"{mode} 播报文案", expect in report.reason, report.reason)

    print("\n[10] 关掉某些检查项后, 对应问题不再拦截")
    g2 = SafetyGate(require_enabled=False, check_fault=False)
    st = healthy()
    st["enabled"] = False
    st["faulted"] = True
    report = g2.check(st, connected=True)
    check("放行", report.ok, f"reason={report.reason!r}")

    print("\n[11] 自定义模式白名单")
    g3 = SafetyGate(allowed_modes=frozenset({"CSPRescue"}))
    report = g3.check(healthy(), connected=True)
    check("CSPPassthrough 被拒", not report.ok)
    check("code=mode", report.code == "mode", report.code)
    st = healthy()
    st["mode"] = "CSPRescue"
    report = g3.check(st, connected=True)
    check("CSPRescue 放行", report.ok, f"reason={report.reason!r}")

    print("\n[12] to_dict 可序列化")
    st = healthy()
    st["faulted"] = True
    report = gate.check(st, connected=True)
    body = report.to_dict()
    check("含 ok/code/reason", "ok" in body and "code" in body and "reason" in body)
    check("含检查明细", len(body["checks"]) == 8, f"{len(body['checks'])} 项")
    import json

    check("可 JSON 序列化", bool(json.dumps(body, ensure_ascii=False)))

    print()
    if FAILED == 0:
        print("全部通过 ✓")
    else:
        print(f"存在 {FAILED} 个失败项")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
