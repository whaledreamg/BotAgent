#!/usr/bin/env python3
"""vision.grasp: 最近目标选择、失败路径、追溯落盘 (假客户端, 不需要真机)。

运行: cd services/agent_service && python test_vision_grasp.py
"""

import asyncio
import json
import tempfile
from pathlib import Path

from skill.builtin.vision_grasp import VisionGraspSkill

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def obj(x, y, z, conf=0.8, valid=True, frame="world"):
    return {"label": "mouse", "label_zh": "鼠标", "confidence": conf,
            "pose": {"x": x, "y": y, "z": z, "frame": frame, "depth_valid": valid}}


class FakeVision:
    def __init__(self, objects):
        self.objects = objects

    async def find(self, name, frame="camera"):
        self.frame = frame
        return {"found": bool(self.objects), "objects": self.objects}


class FakeArm:
    def __init__(self, move_result=None):
        self.moves = []
        self.move_result = move_result or {"ok": True, "message": "ok", "error_m": 0.01}

    async def kinematics(self, refresh=True):
        return {"arms": {"RightArm": {"base_frame": {"x": 0.0, "y": -0.2, "z": 0.0}}}}

    async def move_to(self, arm, x, y, z, duration=None, **kw):
        self.moves.append((arm, x, y, z, duration))
        return self.move_result


async def run(objects, arm=None, **args):
    tmp = Path(tempfile.mkdtemp()) / "t.jsonl"
    vision, arm = FakeVision(objects), arm or FakeArm()
    skill = VisionGraspSkill(VisionGraspSkill.spec(), None, vision, arm, trace_path=tmp)
    res = await skill.execute({"target": "鼠标", **args})
    lines = [json.loads(l) for l in tmp.read_text(encoding="utf-8").splitlines()]
    return res, vision, arm, lines


async def main():
    print("多个目标取最近:")
    res, vision, arm, tr = await run([obj(0.9, 0.3, -0.3), obj(0.5, -0.2, -0.3), obj(0.7, 0.0, -0.3)])
    check(vision.frame == "world", "向视觉请求世界系")
    check(res.ok and len(arm.moves) == 1, "移动了一次")
    check(abs(arm.moves[0][1] - 0.5) < 1e-9, f"选了最近的 x=0.5, 实际 {arm.moves[0][1]}")
    check(abs(arm.moves[0][3] - (-0.3 + 0.1)) < 1e-9, "预抓取点在目标上方 0.1m")
    check(len(tr) == 1 and len(tr[0]["candidates"]) == 3, "追溯记录含 3 个候选")
    check(tr[0]["candidates"][0]["distance_to_base"] < tr[0]["candidates"][1]["distance_to_base"], "候选按距离升序")

    print("过滤无效目标:")
    res, _, arm, tr = await run([obj(0.3, -0.2, -0.3, valid=False), obj(0.8, 0.0, -0.3)])
    check(abs(arm.moves[0][1] - 0.8) < 1e-9, "最近但深度无效的被跳过")
    check(tr[0]["skipped_invalid"] == 1, "记录跳过数")

    print("全部无效 / 没找到:")
    res, _, arm, tr = await run([obj(0.3, 0, 0, valid=False)])
    check(not res.ok and not arm.moves and "测不准" in res.reply, f"不动并说明原因: {res.reply}")
    res, _, arm, tr = await run([obj(0.3, 0, 0, frame="camera")])
    check(not res.ok and not arm.moves, "非世界系结果不能拿来移动")
    res, _, arm, tr = await run([])
    check(not res.ok and not arm.moves and tr[0]["outcome"] == "not_found", "没找到 -> 不动并落盘")

    print("门阀/可达性拒绝:")
    deny = FakeArm({"ok": False, "code": "ops_mode", "stage": "safety", "message": "控制器处于手动模式，请切到自动模式"})
    res, _, _, tr = await run([obj(0.5, 0, -0.3)], arm=deny)
    check(not res.ok and res.reply == "控制器处于手动模式，请切到自动模式", "原因原样念出")
    check(tr[0]["outcome"] == "rejected:ops_mode", "追溯记录写明拒绝原因")

    print("控制总闸关着 (只算不动):")
    #
    # 开关在**机械臂服务**那边 (configs/arm.yaml 的 arm.enabled), 表现是
    # move_to 返回 code="disabled"。skill 这边无条件请求移动, 由服务决定。
    disabled = FakeArm({
        "ok": False, "code": "disabled", "stage": "gate",
        "message": "机械臂控制已关闭，只计算了位置，机械臂没有动",
    })
    res, _, arm, tr = await run([obj(0.5, 0, -0.3)], arm=disabled)
    check(len(arm.moves) == 1, "确实请求了移动 (由机械臂服务决定动不动)")
    check(res.ok and tr[0]["outcome"] == "planned_only", "总闸关着 -> 只算不动")
    check("目标点" in res.reply and "机械臂没有动" in res.reply, f"报出算好的位置: {res.reply}")

    print("时长透传:")
    _, _, arm, _ = await run([obj(0.5, 0, -0.3)], duration=5)
    check(arm.moves[0][4] == 5.0, "duration 透传")


asyncio.run(main())
raise SystemExit(1 if failures else 0)
