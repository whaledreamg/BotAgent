#!/usr/bin/env python3
"""vision.grasp: 5 步抓取流程 (假客户端, 不需要真机)。

覆盖:
- 5 步顺序正确 (记录调用序列)
- 第 3 步失败 -> 仍会回安全点
- 总闸关着 -> 5 步全走完, 只是不动
- 每步的坐标都进了播报
- 多个目标取最近、失败路径、追溯落盘

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
        self.fresh_calls = 0

    async def find(self, name, frame="camera", fresh=False):
        self.frame = frame
        self.fresh = fresh
        if fresh:
            self.fresh_calls += 1
        return {"found": bool(self.objects), "objects": self.objects}


class FakeArm:
    """假机械臂客户端。

    记录所有调用的**顺序** —— 5 步流程的正确性就看这个序列。
    """

    def __init__(self, move_result=None, play_result=None):
        self.moves = []
        self.plays = []
        self.calls = []          # 全部调用, 按时间顺序
        self.move_result = move_result or {"ok": True, "message": "ok", "error_m": 0.01}
        self.play_result = play_result or {"ok": True, "finished": True, "error": ""}
        #: 播哪个动作会失败 ("准备点" / "安全点" / "闭合夹爪" ...)
        self.play_fails = set()

    async def kinematics(self, refresh=True):
        self.calls.append("kinematics")
        return {"arms": {"RightArm": {"base_frame": {"x": 0.0, "y": -0.2, "z": 0.0}}}}

    async def move_to(self, arm, x, y, z, duration=None, **kw):
        self.calls.append(("move_to", arm, x, y, z))
        self.moves.append((arm, x, y, z, duration))
        return self.move_result

    async def play_and_wait(self, action, **kw):
        self.calls.append(("play", action))
        self.plays.append(action)
        if action in self.play_fails:
            return {"ok": False, "finished": False, "error": f"{action}播不了"}
        return self.play_result

    async def flow_config(self):
        self.calls.append("flow")
        return {
            "ready_action": "准备点",
            "safe_action": "安全点",
            "above_distance": 0.10,
            "grasp_offset_x": 0.0,
            "grasp_offset_y": 0.0,
            "grasp_offset_z": 0.0,
            "close_action": "闭合夹爪",
            "open_action": "张开夹爪",
            "above_duration": 3.0,
            "grasp_duration": 2.0,
        }


async def run(objects, arm=None, **args):
    tmp = Path(tempfile.mkdtemp()) / "t.jsonl"
    vision, arm = FakeVision(objects), arm or FakeArm()
    skill = VisionGraspSkill(VisionGraspSkill.spec(), None, vision, arm, trace_path=tmp)
    res = await skill.execute({"target": "鼠标", **args})
    lines = [json.loads(l) for l in tmp.read_text(encoding="utf-8").splitlines()]
    return res, vision, arm, lines


async def main():
    print("5 步顺序:")
    res, vision, arm, tr = await run([obj(0.5, 0.02, -0.3)])
    plays = [c[1] for c in arm.calls if isinstance(c, tuple) and c[0] == "play"]
    moves = [c for c in arm.calls if isinstance(c, tuple) and c[0] == "move_to"]
    check(plays == ["准备点", "闭合夹爪", "安全点"],
          f"播的顺序是 准备点->闭合夹爪->安全点: {plays}")
    check(len(moves) == 2, f"移了两次 (上方点 + 抓取点): {len(moves)}")
    check(res.ok, f"整个流程成功: {res.error or ''}")
    check(tr[0]["outcome"] == "grasp_ok", f"追溯写明成功: {tr[0]['outcome']}")

    print("5 步都进了追溯:")
    steps = tr[0].get("steps") or []
    names = [s.get("name") for s in steps]
    check(names[:5] == ["ready", "detect", "above", "grasp", "safe"],
          f"追溯里有 5 步 (ready/detect/above/grasp/safe): {names}")

    print("上方点 / 抓取点的位置:")
    # 目标 (0.5, 0.02, -0.3), 上方 0.1m -> z = -0.2
    check(abs(arm.moves[0][3] - (-0.3 + 0.1)) < 1e-9,
          f"上方点在目标上方 0.1m: {arm.moves[0][3]}")
    # 抓取点 = 目标 + 偏移(0) -> z = -0.3
    check(abs(arm.moves[1][3] - (-0.3)) < 1e-9,
          f"抓取点就是目标点 (偏移为 0): {arm.moves[1][3]}")
    check(abs(arm.moves[1][1] - 0.5) < 1e-9, "抓取点 x 就是目标 x")

    print("多个目标取最近:")
    res, vision, arm, tr = await run(
        [obj(0.9, 0.3, -0.3), obj(0.5, -0.2, -0.3), obj(0.7, 0.0, -0.3)]
    )
    check(vision.frame == "world", "向视觉请求世界系")
    check(res.ok and len(arm.moves) == 2, "移了两次")
    check(abs(arm.moves[0][1] - 0.5) < 1e-9,
          f"选了最近的 x=0.5, 实际 {arm.moves[0][1]}")
    check(len(tr) == 1 and len(tr[0]["candidates"]) == 3, "追溯记录含 3 个候选")
    check(tr[0]["candidates"][0]["distance_to_base"]
          < tr[0]["candidates"][1]["distance_to_base"], "候选按距离升序")

    print("过滤无效目标:")
    res, _, arm, tr = await run([obj(0.3, -0.2, -0.3, valid=False), obj(0.8, 0.0, -0.3)])
    check(abs(arm.moves[0][1] - 0.8) < 1e-9, "最近但深度无效的被跳过")
    check(tr[0]["skipped_invalid"] == 1, "记录跳过数")

    print("全部无效 / 没找到:")
    res, _, arm, tr = await run([obj(0.3, 0, 0, valid=False)])
    check(not res.ok and not arm.moves and "测不准" in res.reply,
          f"不动并说明原因: {res.reply}")
    res, _, arm, tr = await run([obj(0.3, 0, 0, frame="camera")])
    check(not res.ok and not arm.moves, "非世界系结果不能拿来移动")
    res, _, arm, tr = await run([])
    check(not res.ok and not arm.moves and tr[0]["outcome"] == "not_found",
          "没找到 -> 不动并落盘")
    # 没找到时**不回安全点** (已经在准备点了, 准备点本身就是安全的)
    check(not any(isinstance(c, tuple) and c[0] == "play" and c[1] == "安全点"
                  for c in arm.calls),
          "没找到 -> 不回安全点 (已经在准备点)")

    print("第 3 步失败 -> 必须回安全点:")
    #
    # 关键约束: 手停在半空不能就这么报失败 —— 得先收回去。
    # 上面点移动失败, 抓取点当然也不该动。
    fail_above = FakeArm({"ok": False, "code": "unreachable", "stage": "done",
                           "message": "够不着"})
    # 上方点成功、抓取点失败: 用两次不同的返回
    class TwoStepArm(FakeArm):
        def __init__(self):
            super().__init__()
            self.n = 0

        async def move_to(self, arm, x, y, z, duration=None, **kw):
            self.n += 1
            self.calls.append(("move_to", arm, x, y, z))
            self.moves.append((arm, x, y, z, duration))
            if self.n == 1:
                return {"ok": True, "message": "ok", "error_m": 0.0}
            return {"ok": False, "code": "unreachable", "stage": "done",
                    "message": "够不着"}

    arm2 = TwoStepArm()
    res, _, arm2, tr = await run([obj(0.5, 0.02, -0.3)], arm=arm2)
    plays = [c[1] for c in arm2.calls if isinstance(c, tuple) and c[0] == "play"]
    check(not res.ok, "第 4 步失败 -> 报失败")
    check("安全点" in plays, f"失败后回安全点: {plays}")
    check("够不着" in res.reply, f"原因原样念出: {res.reply}")
    check("X 0.50" in res.reply, f"失败时也播报位姿: {res.reply}")

    print("门阀/可达性拒绝:")
    deny = FakeArm({"ok": False, "code": "ops_mode", "stage": "safety",
                     "message": "控制器处于手动模式，请切到自动模式"})
    res, _, _, tr = await run([obj(0.5, 0, -0.3)], arm=deny)
    check("控制器处于手动模式，请切到自动模式" in res.reply,
          f"原因原样念出: {res.reply}")

    print("控制总闸关着 (只算不动):")
    #
    # 开关在**机械臂服务**那边 (configs/arm.yaml 的 arm.enabled), 表现是
    # move_to 返回 code="disabled"。skill 这边无条件请求移动, 由服务决定。
    disabled = FakeArm({
        "ok": False, "code": "disabled", "stage": "gate",
        "message": "机械臂控制已关闭，只计算了位置，机械臂没有动",
    })
    res, _, arm, tr = await run([obj(0.5, 0, -0.3)], arm=disabled)
    check(len(arm.moves) == 2, "确实请求了两次移动 (由机械臂服务决定动不动)")
    check(res.ok and tr[0]["outcome"] == "planned_only", "总闸关着 -> 只算不动")
    check("目标点" in res.reply and "机械臂没有动" in res.reply,
          f"报出算好的位置: {res.reply}")
    # 5 步要**全走完** —— 用户要看的正是完整的位置序列
    plays = [c[1] for c in arm.calls if isinstance(c, tuple) and c[0] == "play"]
    check(plays == ["准备点", "闭合夹爪", "安全点"],
          f"总闸关着也走完 5 步: {plays}")

    print("时长透传:")
    _, _, arm, _ = await run([obj(0.5, 0, -0.3)], duration=5)
    check(arm.moves[0][4] == 5.0, "duration 透传到上方点")

    print("必须用新鲜的检测结果 (不走视觉服务的缓存):")
    #
    # 视觉服务默认返回上一次检测的缓存 —— 物体挪动了它也不知道。
    # 抓取问的是「此刻在哪」, 所以必须 fresh=True。
    res, vision, arm, tr = await run([obj(0.5, 0, -0.3)])
    check(vision.fresh is True, "向视觉请求时带 fresh=True")
    check(vision.fresh_calls == 1, "确实要求重新检测")

    print("播报里带每个点的坐标:")
    res, _, _, tr = await run([obj(0.5, 0.02, -0.3)])
    check("X 0.50" in res.reply and "Y 0.02" in res.reply and "Z -0.30" in res.reply,
          f"播报里含目标点坐标: {res.reply}")
    check("上方点" in res.reply and "Z -0.20" in res.reply,
          f"播报里含上方点坐标: {res.reply}")
    check("抓取点" in res.reply, f"播报里含抓取点: {res.reply}")

    disabled = FakeArm({
        "ok": False, "code": "disabled", "stage": "gate",
        "message": "机械臂控制已关闭，只计算了位置，机械臂没有动",
    })
    res, _, _, tr = await run([obj(0.5, 0.02, -0.3)], arm=disabled)
    check("目标点" in res.reply and "X 0.50" in res.reply,
          f"总闸关着也播报目标点: {res.reply}")
    check("上方点" in res.reply and "Z -0.20" in res.reply,
          f"总闸关着也播报上方点: {res.reply}")
    check("抓取点" in res.reply, f"总闸关着也播报抓取点: {res.reply}")

    deny = FakeArm({"ok": False, "code": "unreachable", "stage": "done",
                     "message": "够不着"})
    res, _, _, tr = await run([obj(0.5, 0.02, -0.3)], arm=deny)
    check("够不着" in res.reply and "X 0.50" in res.reply,
          f"失败时也播报位姿: {res.reply}")

    print("第 5 步失败: 抓到了但回安全点失败:")
    arm3 = FakeArm()
    arm3.play_fails = {"安全点"}
    res, _, arm3, tr = await run([obj(0.5, 0.02, -0.3)], arm=arm3)
    check(not res.ok, "回安全点失败 -> 如实报失败")
    check("安全点" in res.reply, f"说明回安全点失败: {res.reply}")
    check(tr[0]["outcome"].startswith("grasp_ok_safe_failed"),
          f"追溯写明抓到了但收尾失败: {tr[0]['outcome']}")

    print("第 1 步失败 -> 不拍照:")
    arm4 = FakeArm()
    arm4.play_fails = {"准备点"}
    res, vision, arm4, tr = await run([obj(0.5, 0.02, -0.3)], arm=arm4)
    check(not res.ok, "准备点失败 -> 报失败")
    check(vision.fresh_calls == 0, "准备点失败就不拍照了")
    check(not any(isinstance(c, tuple) and c[0] == "move_to" for c in arm4.calls),
          "准备点失败 -> 不移动")


asyncio.run(main())
raise SystemExit(1 if failures else 0)
