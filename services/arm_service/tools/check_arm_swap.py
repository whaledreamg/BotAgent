#!/usr/bin/env python3
"""回归测试: 右臂的运动学数据不能串到左臂名下。

**曾经的 bug**: ``ArmService._status_query_arm`` 是单个共享变量, 而
``robot/status`` 的应答里又没有 name 字段 —— 两条臂的查询互相覆盖,
右臂的槽位里装上了左臂的 TCP + 基座。

**现场表现**: 基座系目标坐标整个翻转 (差 0.88m), 逆解失败, 控制器报
``numericalIkFailed`` 或直接安全停机。

**现在**: 臂名记在请求的 meta 里, 应答按消息 id 原样带回来。

跑法: python3 tools/check_arm_swap.py
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "arm_service"))
from service import ArmService  # noqa: E402


#: 两条臂的真值 (实测自 2026-11-24)
TRUTH: Dict[str, Dict[str, Any]] = {
    "LeftArm": {
        "cartesian": {"x": 0.2941, "y": 0.4014, "z": -0.3323,
                      "a": 2.385, "b": 1.108, "c": 2.067, "e": -1.2073},
        "base_frame": {"x": 0.0, "y": 0.02, "z": 0.0,
                       "a": -1.5708, "b": 1.5708, "c": 0.0},
    },
    "RightArm": {
        "cartesian": {"x": 0.2930, "y": -0.4256, "z": -0.3039,
                      "a": -1.936, "b": 0.424, "c": -1.456, "e": 0.9853},
        "base_frame": {"x": 0.0, "y": -0.02, "z": 0.0,
                       "a": 1.5708, "b": 1.5708, "c": 0.0},
    },
}


class _FakeConn:
    """假连接: 模拟「两条臂的应答乱序回来」。"""

    def __init__(self) -> None:
        self.host = "192.168.100.16"
        self.connected = True
        self.sent: List[Tuple[int, str, Dict[str, Any], Dict[str, Any]]] = []
        self._pending: List[Tuple[str, Dict[str, Any]]] = []
        self._lock = threading.Lock()
        self._next_id = 1
        self._on_event = None

    def send_command(self, ty, db=None, meta=None):
        msg_id = self._next_id
        self._next_id += 1
        self.sent.append((msg_id, ty, db or {}, meta or {}))
        if ty == "robot/status":
            name = (db or {}).get("name", "")
            with self._lock:
                self._pending.append((name, self._make_db(name)))
        return msg_id

    def _make_db(self, name: str) -> Dict[str, Any]:
        """造一条**不含 name 字段**的状态 (跟真机一样)。"""
        t = TRUTH.get(name, {})
        return {
            "joints": [0.0] * 7,
            "cartesian": dict(t.get("cartesian") or {}),
            "baseFrame": dict(t.get("base_frame") or {}),
            "baseFrameKind": "static",
        }

    def deliver(self, reverse: bool = True) -> None:
        """把排队的应答交给上层。

        ``reverse=True`` 模拟最坏情况: 后发的先回 (现场就是这样)。
        """
        with self._lock:
            pending = list(self._pending)
            self._pending.clear()
        if reverse:
            pending.reverse()
        for name, db in pending:
            # 找出对应的 meta (按发出顺序配对)
            meta = {}
            for mid, ty, d, m in self.sent:
                if ty == "robot/status" and d.get("name") == name:
                    meta = m
            self._on_event("robot/status", {"db": db, "id": 0}, meta)

    def set_on_event(self, cb) -> None:
        self._on_event = cb


class _Ctl:
    def active_arms(self):
        return ["LeftArm", "RightArm"]

    def set_arm_enabled(self, *a):
        pass

    def set_actual_joints(self, *a):
        pass


def _make_svc():
    svc = ArmService.__new__(ArmService)
    svc._kin = {}
    svc._kin_lock = threading.Lock()
    svc._column = None
    svc._controller = _Ctl()
    conn = _FakeConn()
    svc._conn = conn
    conn.set_on_event(
        lambda action, response, meta: ArmService._on_controller_event(
            svc, action, response, meta
        )
    )
    return svc, conn


def _check_arms(svc) -> List[str]:
    """检查 _kin 里每条臂的数据对不对。返回问题列表。"""
    problems = []
    for name, truth in TRUTH.items():
        data = svc._kin.get(name)
        if not data:
            problems.append(f"{name}: 没有任何快照")
            continue
        got_c = data.get("cartesian") or {}
        got_b = data.get("base_frame") or {}
        want_c = truth["cartesian"]
        want_b = truth["base_frame"]
        for k in ("x", "y", "z"):
            if abs(got_c.get(k, 0.0) - want_c[k]) > 1e-6:
                problems.append(
                    f"{name}: cartesian.{k} = {got_c.get(k):+.4f}, 应为 {want_c[k]:+.4f}"
                )
        for k in ("x", "y", "z"):
            if abs(got_b.get(k, 0.0) - want_b[k]) > 1e-6:
                problems.append(
                    f"{name}: base_frame.{k} = {got_b.get(k):+.4f}, 应为 {want_b[k]:+.4f}"
                )
    return problems


def main() -> int:
    failed = 0

    # ---------------------------------------------------------- 场景 1
    print("=" * 74)
    print("场景 1: 两条臂的应答乱序回来 (最坏情况)")
    print("=" * 74)
    svc, conn = _make_svc()
    for name in svc._controller.active_arms():
        svc._query_robot_status(name)
    conn.deliver(reverse=True)

    for name, truth in TRUTH.items():
        data = svc._kin.get(name) or {}
        c = data.get("cartesian") or {}
        b = data.get("base_frame") or {}
        print(f"  [{name}]")
        print(f"      cartesian.y = {c.get('y', 0):+.4f}   base_frame.y = {b.get('y', 0):+.4f}"
              f"   (应为 {truth['cartesian']['y']:+.4f} / {truth['base_frame']['y']:+.4f})")

    problems = _check_arms(svc)
    if problems:
        failed += 1
        print("  [FAIL]")
        for p in problems:
            print(f"      - {p}")
    else:
        print("  [PASS] 两条臂的数据各自正确, 没有串台")

    # ---------------------------------------------------------- 场景 2
    print()
    print("=" * 74)
    print("场景 2: 认不出臂名的应答必须被丢弃, 不能乱写")
    print("=" * 74)
    svc2, conn2 = _make_svc()
    # 手动发一个没有 meta 也没有 name 的应答
    ArmService._on_controller_event(svc2, "robot/status", {"db": {
        "joints": [0.0] * 7,
        "cartesian": dict(TRUTH["LeftArm"]["cartesian"]),
        "baseFrame": dict(TRUTH["LeftArm"]["base_frame"]),
    }}, {})
    if svc2._kin:
        failed += 1
        print(f"  [FAIL] 认不出臂名的数据被写进去了: {list(svc2._kin.keys())}")
    else:
        print("  [PASS] 认不出臂名的数据被丢弃, _kin 保持为空")

    # ---------------------------------------------------------- 场景 3
    print()
    print("=" * 74)
    print("场景 3: 交叉校验能发现串台")
    print("=" * 74)
    svc3, _ = _make_svc()
    # 人为把左臂的数据塞进右臂的槽位
    svc3._kin = {
        "LeftArm": {"cartesian": dict(TRUTH["LeftArm"]["cartesian"]),
                    "base_frame": dict(TRUTH["LeftArm"]["base_frame"])},
        "RightArm": {"cartesian": dict(TRUTH["LeftArm"]["cartesian"]),
                     "base_frame": dict(TRUTH["LeftArm"]["base_frame"])},
    }
    msg = ArmService._detect_crossed_arms(svc3._kin)
    if msg:
        print(f"  [PASS] 检出串台: {msg}")
    else:
        failed += 1
        print("  [FAIL] 串台没被检出")

    # 正常情况不该误报
    svc3._kin = {
        "LeftArm": {"cartesian": dict(TRUTH["LeftArm"]["cartesian"]),
                    "base_frame": dict(TRUTH["LeftArm"]["base_frame"])},
        "RightArm": {"cartesian": dict(TRUTH["RightArm"]["cartesian"]),
                     "base_frame": dict(TRUTH["RightArm"]["base_frame"])},
    }
    msg = ArmService._detect_crossed_arms(svc3._kin)
    if msg:
        failed += 1
        print(f"  [FAIL] 正常数据被误报: {msg}")
    else:
        print("  [PASS] 正常数据不误报")

    print()
    print("=" * 74)
    print(f"结果: {'全部通过 ✓' if failed == 0 else f'{failed} 项失败 ✗'}")
    print("=" * 74)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
