#!/usr/bin/env python3
"""vision.grasp: 通用流程执行器 (假客户端, 不需要真机)。

覆盖:
- 默认流程的步骤顺序正确 (记录调用序列)
- 第 3 步失败 -> 仍会回安全点
- 总闸关着 -> 流程全走完, 只是不动
- 每步的坐标都进了播报
- 多个目标取最近、失败路径、追溯落盘
- **自定义流程** (增一步/调顺序) 能正确执行
- 未知步骤类型 -> 报错而不是静默跳过
- ``approach_distance`` / ``duration`` 参数真的生效

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


#: 默认流程 —— 与 configs/flows/grasp.yaml 等价
DEFAULT_FLOW = {
    "name": "grasp",
    "description": "标准抓取",
    "recovery_action": "安全点",
    "recovery_label": "回安全点",
    "approach_distance": 0.10,
    "defaults": {"close_action": "闭合夹爪", "open_action": "张开夹爪"},
    "steps": [
        {"id": "ready", "type": "arm", "action": "准备点", "label": "回准备点"},
        {"id": "detect", "type": "detect", "duration": 1.5, "label": "拍照识别"},
        {"id": "object", "type": "target", "source": "detect", "label": "目标物体"},
        {"id": "above_point", "type": "target", "base": "object",
         "distance": "approach", "label": "目标上方"},
        {"id": "above", "type": "arm", "point": "above_point", "duration": 3.0,
         "label": "移到上方点"},
        {"id": "grasp", "type": "arm", "point": "object",
         "offset": {"x": 0.0, "y": 0.0, "z": 0.0}, "duration": 2.0,
         "label": "移到抓取点"},
        {"id": "close", "type": "hand", "action": "闭合夹爪", "label": "闭合夹爪"},
        {"id": "safe", "type": "arm", "action": "安全点", "label": "回安全点"},
    ],
}


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

    记录所有调用的**顺序** —— 流程的正确性就看这个序列。
    """

    def __init__(self, move_result=None, play_result=None, flows=None):
        self.moves = []
        self.plays = []
        self.calls = []          # 全部调用, 按时间顺序
        self.sequences = []     # 合成序列的调用
        #: 合成序列的结果 (None = 用默认的成功结果)
        self.sequence_result = None
        self.move_result = move_result or {"ok": True, "message": "ok", "error_m": 0.01}
        self.play_result = play_result or {"ok": True, "finished": True, "error": ""}
        #: 播哪个动作会失败 ("准备点" / "安全点" / "闭合夹爪" ...)
        self.play_fails = set()
        #: 可用的流程定义 ``{流程名: 定义}``
        self.flows = flows if flows is not None else {"grasp": DEFAULT_FLOW}

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

    async def flow_definition(self, name="grasp"):
        self.calls.append(("flow", name))
        if name not in self.flows:
            raise RuntimeError(f"没有流程 {name!r}")
        return self.flows[name]

    async def grasp_sequence(self, x, y, z, *, side="right", flow="grasp",
                             approach_distance=None, duration=None,
                             plan_only=False):
        """合成动作序列 (服务端做的)。"""
        self.calls.append(("grasp_sequence", side, flow, x, y, z))
        self.sequences.append((side, flow, x, y, z))
        if self.sequence_result is not None:
            return self.sequence_result
        return {
            "ok": True,
            "sequence": {
                "duration": 12.5,
                "segments": 5,
                "sources": ["准备点", "above", "grasp", "close", "安全点"],
                "steps": [
                    {"step": "ready", "type": "arm", "label": "回准备点",
                     "action": "准备点", "start": 0.0, "end": 3.0},
                    {"step": "above", "type": "arm", "label": "移到上方点",
                     "pose": [x, y, z + 0.10], "start": 3.0, "end": 6.0},
                    {"step": "grasp", "type": "arm", "label": "移到抓取点",
                     "pose": [x, y, z], "start": 6.0, "end": 8.0},
                    {"step": "close", "type": "hand", "label": "闭合夹爪",
                     "direction": "close", "time": 8.0},
                    {"step": "safe", "type": "arm", "label": "回安全点",
                     "action": "安全点", "start": 8.0, "end": 11.0},
                ],
                "hand_cues": [
                    {"time": 8.0, "label": "闭合夹爪",
                     "left": [1000, 0, 1000, 1000, 1000, 1000],
                     "right": [1000, 0, 1000, 1000, 1000, 1000]},
                ],
            },
            "ik": [
                {"step": "above", "pose": [x, y, z + 0.10], "ok": True,
                 "err_pos_mm": 0.05, "err_rot_deg": 0.01},
                {"step": "grasp", "pose": [x, y, z], "ok": True,
                 "err_pos_mm": 0.08, "err_rot_deg": 0.02},
            ],
            "arm": "RightArm",
            "side": "right",
        }

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
    """跑一次抓取, 返回 (结果, 视觉 mock, 机械臂 mock, 追溯记录)。

    **默认走序列模式** (先识别, 再合成一条动作序列)。想测逐步骤的
    老行为, 传 ``sequence=False``。
    """
    tmp = Path(tempfile.mkdtemp()) / "t.jsonl"
    vision, arm = FakeVision(objects), arm or FakeArm()
    skill = VisionGraspSkill(VisionGraspSkill.spec(), None, vision, arm, trace_path=tmp)
    res = await skill.execute({"target": "鼠标", **args})
    lines = [json.loads(l) for l in tmp.read_text(encoding="utf-8").splitlines()]
    return res, vision, arm, lines


async def run_steps(objects, arm=None, **args):
    """跑一次抓取, 但**强制走逐步骤模式** (老行为)。"""
    return await run(objects, arm=arm, sequence=False, **args)

async def main():
    print("5 步顺序:")
    res, vision, arm, tr = await run_steps([obj(0.5, 0.02, -0.3)])
    plays = [c[1] for c in arm.calls if isinstance(c, tuple) and c[0] == "play"]
    moves = [c for c in arm.calls if isinstance(c, tuple) and c[0] == "move_to"]
    check(plays == ["准备点", "闭合夹爪", "安全点"],
          f"播的顺序是 准备点->闭合夹爪->安全点: {plays}")
    check(len(moves) == 2, f"移了两次 (上方点 + 抓取点): {len(moves)}")
    check(res.ok, f"整个流程成功: {res.error or ''}")
    check(tr[0]["outcome"] == "grasp_ok", f"追溯写明成功: {tr[0]['outcome']}")

    print("每一步都进了追溯:")
    steps = tr[0].get("steps") or []
    names = [s.get("name") for s in steps]
    # 具名点声明 (target) 也是一步 —— 它们登记了点, 只是不产生运动
    check(names == ["ready", "detect", "object", "above_point", "above",
                    "grasp", "close", "safe"],
          f"追溯里有全部片段 (含具名点声明): {names}")

    print("上方点 / 抓取点的位置:")
    # 目标 (0.5, 0.02, -0.3), 上方 0.1m -> z = -0.2
    check(abs(arm.moves[0][3] - (-0.3 + 0.1)) < 1e-9,
          f"上方点在目标上方 0.1m: {arm.moves[0][3]}")
    # 抓取点 = 目标 + 偏移(0) -> z = -0.3
    check(abs(arm.moves[1][3] - (-0.3)) < 1e-9,
          f"抓取点就是目标点 (偏移为 0): {arm.moves[1][3]}")
    check(abs(arm.moves[1][1] - 0.5) < 1e-9, "抓取点 x 就是目标 x")

    print("多个目标取最近:")
    res, vision, arm, tr = await run_steps(
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
    res, _, arm, tr = await run_steps([obj(0.3, -0.2, -0.3, valid=False), obj(0.8, 0.0, -0.3)])
    check(abs(arm.moves[0][1] - 0.8) < 1e-9, "最近但深度无效的被跳过")
    check(tr[0]["skipped_invalid"] == 1, "记录跳过数")

    print("全部无效 / 没找到:")
    res, _, arm, tr = await run_steps([obj(0.3, 0, 0, valid=False)])
    check(not res.ok and not arm.moves and "测不准" in res.reply,
          f"不动并说明原因: {res.reply}")
    res, _, arm, tr = await run_steps([obj(0.3, 0, 0, frame="camera")])
    check(not res.ok and not arm.moves, "非世界系结果不能拿来移动")
    res, _, arm, tr = await run_steps([])
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
    res, _, arm2, tr = await run_steps([obj(0.5, 0.02, -0.3)], arm=arm2)
    plays = [c[1] for c in arm2.calls if isinstance(c, tuple) and c[0] == "play"]
    check(not res.ok, "第 4 步失败 -> 报失败")
    check("安全点" in plays, f"失败后回安全点: {plays}")
    check("够不着" in res.reply, f"原因原样念出: {res.reply}")
    # 失败时**不念坐标** —— 坐标在 payload/追溯里, 排查时看那里
    check("X 0.50" not in res.reply, f"失败时不播报位姿: {res.reply}")

    print("门阀/可达性拒绝:")
    deny = FakeArm({"ok": False, "code": "ops_mode", "stage": "safety",
                     "message": "控制器处于手动模式，请切到自动模式"})
    res, _, _, tr = await run_steps([obj(0.5, 0, -0.3)], arm=deny)
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
    res, _, arm, tr = await run_steps([obj(0.5, 0, -0.3)], arm=disabled)
    check(len(arm.moves) == 2, "确实请求了两次移动 (由机械臂服务决定动不动)")
    check(res.ok and tr[0]["outcome"] == "planned_only", "总闸关着 -> 只算不动")
    # 总闸关着**不能**说「抓取完成」—— 根本没抓
    check("抓取完成" not in res.reply, f"没说抓取完成: {res.reply}")
    check("机械臂没有动" in res.reply, f"说明了没动: {res.reply}")
    # 5 步要**全走完** —— 用户要看的正是完整的位置序列
    plays = [c[1] for c in arm.calls if isinstance(c, tuple) and c[0] == "play"]
    check(plays == ["准备点", "闭合夹爪", "安全点"],
          f"总闸关着也走完 5 步: {plays}")

    print("时长透传:")
    _, _, arm, _ = await run_steps([obj(0.5, 0, -0.3)], duration=5)
    check(arm.moves[0][4] == 5.0, "duration 透传到上方点")

    print("必须用新鲜的检测结果 (不走视觉服务的缓存):")
    #
    # 视觉服务默认返回上一次检测的缓存 —— 物体挪动了它也不知道。
    # 抓取问的是「此刻在哪」, 所以必须 fresh=True。
    res, vision, arm, tr = await run_steps([obj(0.5, 0, -0.3)])
    check(vision.fresh is True, "向视觉请求时带 fresh=True")
    check(vision.fresh_calls == 1, "确实要求重新检测")

    print("播报只报结论, 坐标进 payload 不进播报:")
    res, _, _, tr = await run_steps([obj(0.5, 0.02, -0.3)])
    check(res.reply == "抓取鼠标完成", f"播报是简短结论: {res.reply!r}")
    for noise in ("X 0.50", "Y 0.02", "Z -0.30", "目标点", "上方点", "抓取点"):
        check(noise not in res.reply, f"播报里没有 {noise!r}: {res.reply!r}")
    # 坐标**仍然保留**在 payload 里 —— 排查问题要看它
    check(res.payload.get("target_pose"), "payload 里保留了目标位置")
    check(res.payload.get("approach_pose"), "payload 里保留了上方点位置")
    check(res.payload.get("grasp_pose"), "payload 里保留了抓取点位置")

    disabled = FakeArm({
        "ok": False, "code": "disabled", "stage": "gate",
        "message": "机械臂控制已关闭，只计算了位置，机械臂没有动",
    })
    res, _, _, tr = await run_steps([obj(0.5, 0.02, -0.3)], arm=disabled)
    check("机械臂没有动" in res.reply, f"总闸关着也说明没动: {res.reply}")
    check("抓取完成" not in res.reply, f"总闸关着不说抓取完成: {res.reply}")
    # 位置照样在 payload 里
    check(res.payload.get("target_pose"), "总闸关着时 payload 仍有目标位置")

    deny = FakeArm({"ok": False, "code": "unreachable", "stage": "done",
                     "message": "够不着"})
    res, _, _, tr = await run_steps([obj(0.5, 0.02, -0.3)], arm=deny)
    check("够不着" in res.reply, f"说明了失败原因: {res.reply}")
    check("X 0.50" not in res.reply, f"失败时不播报位姿: {res.reply}")

    print("第 5 步失败: 抓到了但回安全点失败:")
    arm3 = FakeArm()
    arm3.play_fails = {"安全点"}
    res, _, arm3, tr = await run_steps([obj(0.5, 0.02, -0.3)], arm=arm3)
    check(not res.ok, "回安全点失败 -> 如实报失败")
    check("安全点" in res.reply, f"说明回安全点失败: {res.reply}")
    check(tr[0]["outcome"].startswith("grasp_ok_safe_failed"),
          f"追溯写明抓到了但收尾失败: {tr[0]['outcome']}")

    print("第 1 步失败 -> 不拍照:")
    arm4 = FakeArm()
    arm4.play_fails = {"准备点"}
    res, vision, arm4, tr = await run_steps([obj(0.5, 0.02, -0.3)], arm=arm4)
    check(not res.ok, "准备点失败 -> 报失败")
    check(vision.fresh_calls == 0, "准备点失败就不拍照了")
    check(not any(isinstance(c, tuple) and c[0] == "move_to" for c in arm4.calls),
          "准备点失败 -> 不移动")
    # 还没动过 -> 不用收尾 (回安全点) —— 手根本没离开准备点
    check(not any(isinstance(c, tuple) and c[0] == "play" and c[1] == "安全点"
                  for c in arm4.calls),
          "没动过 -> 不收尾 (手还在准备点)")

    # ================= 通用流程执行器 =================

    print("自定义流程 (只写 YAML, 不改代码):")
    #
    # 加一步「拍完照再抬 10cm」, 调整顺序 —— 全靠流程定义。
    custom = {
        "name": "custom",
        "recovery_action": "安全点",
        "recovery_label": "回安全点",
        "approach_distance": 0.05,
        "defaults": {"close_action": "闭合夹爪", "open_action": "张开夹爪"},
        "steps": [
            {"id": "ready", "type": "arm", "action": "准备点", "label": "回准备点"},
            {"id": "detect", "type": "detect", "duration": 1.5, "label": "拍照识别"},
            {"id": "object", "type": "target", "source": "detect", "label": "目标物体"},
            {"id": "lift_point", "type": "target", "base": "object",
             "distance": "approach",
             "offset": {"x": 0.0, "y": 0.0, "z": 0.10}, "label": "目标上方"},
            {"id": "lift", "type": "arm", "point": "lift_point", "duration": 1.0,
             "label": "抬到上方"},
            {"id": "grasp", "type": "arm", "point": "object",
             "offset": {"x": 0.0, "y": 0.0, "z": 0.0}, "duration": 2.0,
             "label": "移到抓取点"},
            {"id": "open", "type": "hand", "action": "张开夹爪",
             "direction": "open", "label": "张开夹爪"},
            {"id": "safe", "type": "arm", "action": "安全点", "label": "回安全点"},
        ],
    }
    arm5 = FakeArm(flows={"custom": custom})
    res, _, arm5, tr = await run_steps([obj(0.5, 0.02, -0.3)], arm=arm5, flow="custom")
    plays = [c[1] for c in arm5.calls if isinstance(c, tuple) and c[0] == "play"]
    check(plays == ["准备点", "张开夹爪", "安全点"],
          f"自定义流程按自己的步骤播: {plays}")
    check(res.ok, f"自定义流程成功: {res.error or ''}")
    names = [s.get("name") for s in (tr[0].get("steps") or [])]
    check(names == ["ready", "detect", "object", "lift_point", "lift",
                    "grasp", "open", "safe"],
          f"追溯记录自定义流程的片段: {names}")
    # 上方点 = 目标 z + 0.05 (流程里的 approach_distance) + 0.10 (offset)
    check(abs(arm5.moves[0][3] - (-0.3 + 0.05 + 0.10)) < 1e-9,
          f"上方点 = 目标 + 流程距离 + 步骤偏移: {arm5.moves[0][3]}")

    print("自定义流程里的 open 步骤 (open_action 不再是死配置):")
    # open 步骤**照样执行**了 (plays 里有「张开夹爪」), 只是不逐步播报
    check("张开夹爪" in plays, f"播了张开夹爪: {plays}")
    check(res.reply == "抓取鼠标完成", f"播报仍是简短结论: {res.reply!r}")

    print("未知步骤类型 -> 报错, 不静默跳过:")
    bad = dict(custom)
    bad = {**custom, "steps": [
        {"id": "ready", "type": "arm", "action": "准备点", "label": "回准备点"},
        {"id": "weird", "type": "teleport", "label": "瞬移"},
    ]}
    arm6 = FakeArm(flows={"bad": bad})
    res, _, arm6, tr = await run_steps([obj(0.5, 0.02, -0.3)], arm=arm6, flow="bad")
    check(not res.ok, "未知类型 -> 报失败")
    check("teleport" in res.reply, f"说明是未知的步骤类型: {res.reply}")

    print("approach_distance 参数真的生效:")
    #
    # 之前这个参数被读了却没用 —— 实际算上方点时用的是配置里的值。
    # 用户传 0.3 应该得到目标上方 0.3m。
    _, _, arm7, _ = await run_steps([obj(0.5, 0.02, -0.3)], approach_distance=0.3)
    check(abs(arm7.moves[0][3] - (-0.3 + 0.3)) < 1e-9,
          f"参数覆盖了流程里的 above_distance: {arm7.moves[0][3]}")
    # 不传就用流程里的值 (0.10)
    _, _, arm8, _ = await run_steps([obj(0.5, 0.02, -0.3)])
    check(abs(arm8.moves[0][3] - (-0.3 + 0.10)) < 1e-9,
          f"不传就用流程里配的距离: {arm8.moves[0][3]}")

    print("duration 覆盖所有 move 步骤 (之前只作用于第 3 步):")
    _, _, arm9, _ = await run_steps([obj(0.5, 0.02, -0.3)], duration=5)
    check(all(m[4] == 5.0 for m in arm9.moves),
          f"两个 move 步骤都用 5 秒: {[m[4] for m in arm9.moves]}")
    # 不传则各步用自己配的时长
    _, _, arm10, _ = await run_steps([obj(0.5, 0.02, -0.3)])
    check(arm10.moves[0][4] == 3.0 and arm10.moves[1][4] == 2.0,
          f"各步用自己的时长: {[m[4] for m in arm10.moves]}")

    print("流程不存在 -> 用兜底流程, 不崩:")
    arm11 = FakeArm(flows={})
    res, _, arm11, tr = await run_steps([obj(0.5, 0.02, -0.3)], arm=arm11, flow="nope")
    check(res.ok, f"兜底流程跑完了: {res.error or ''}")
    plays = [c[1] for c in arm11.calls if isinstance(c, tuple) and c[0] == "play"]
    check(plays == ["准备点", "闭合夹爪", "安全点"],
          f"兜底流程照常播: {plays}")

    print("追溯记录带流程名:")
    _, _, _, tr = await run_steps([obj(0.5, 0.02, -0.3)])
    check(tr[0].get("flow") == "grasp", f"追溯写明用的哪条流程: {tr[0].get('flow')}")

    # ================= 序列模式 (先识别, 再合成一条序列) =================

    print("序列模式 (默认): 先识别, 再合成一条动作序列执行:")
    res, vision, arm, tr = await run([obj(0.5, 0.02, -0.3)])
    check(res.ok, f"序列模式跑通了: {res.error or ''}")
    check(vision.fresh_calls == 1, f"识别了一次: {vision.fresh_calls}")
    check(len(arm.sequences) == 1, f"合成了一次序列: {len(arm.sequences)}")
    # 合成序列时**不该**再逐步骤下发 move_to / play
    check(not arm.moves, f"序列模式下没有逐步骤移动: {arm.moves}")
    check(not arm.plays, f"序列模式下没有逐步骤播动作: {arm.plays}")
    # 传给服务端的必须**是识别到的位置**
    seq_call = [c for c in arm.calls
                if isinstance(c, tuple) and c[0] == "grasp_sequence"]
    check(len(seq_call) == 1, "调用了 grasp_sequence")
    if seq_call:
        c = seq_call[0]
        check(abs(c[3] - 0.5) < 1e-9 and abs(c[4] - 0.02) < 1e-9
              and abs(c[5] - (-0.3)) < 1e-9,
              f"传的是识别到的位置: ({c[3]}, {c[4]}, {c[5]})")
        check(c[1] == "right", f"用的哪条臂: {c[1]}")

    print("序列模式: 追溯记录里有序列和逆解明细:")
    check(tr[0].get("outcome") == "grasp_ok", f"追溯写明成功: {tr[0].get('outcome')}")
    check("sequence" in tr[0], "追溯里有 sequence")
    check("ik" in tr[0], "追溯里有逆解明细")
    names = [s.get("name") for s in (tr[0].get("steps") or [])]
    check("detect" in names, f"追溯里有识别步骤: {names}")
    check("above" in names and "grasp" in names,
          f"追溯里有移动步骤: {names}")

    print("序列模式: 播报只报结论, 不报过程细节:")
    check(res.reply == "抓取鼠标完成", f"播报是简短结论: {res.reply!r}")
    # 过程细节**不播出来** —— 用户要的是「做完了没」, 不是坐标和每一步
    for noise in ("目标点", "已到", "手已合拢", "置信度", "X ", "已回准备点"):
        check(noise not in res.reply, f"播报里没有 {noise!r}: {res.reply!r}")
    # 但过程细节**仍然保留**在 payload 里 (排查问题要用)
    steps_in_payload = res.payload.get("steps") or []
    check(len(steps_in_payload) >= 3, f"payload 里保留了各步明细: {len(steps_in_payload)}")
    check(res.payload.get("target_pose"), "payload 里保留了目标位置")

    print("序列模式: 识别失败 -> 不合成序列, 直接报失败:")
    res, vision, arm, tr = await run([])
    check(not res.ok, "没找到物体 -> 报失败")
    check(not arm.sequences, "识别失败时不合成序列")
    check("没有找到" in res.reply or "请调整" in res.reply,
          f"说明了没找到: {res.reply}")

    print("序列模式: 服务端合成失败 -> 如实报失败:")
    arm12 = FakeArm()
    arm12.sequence_result = {"ok": False, "code": "ik_failed",
                              "message": "目标点够不着"}
    res, _, arm12, tr = await run([obj(0.5, 0.02, -0.3)], arm=arm12)
    check(not res.ok, "服务端失败 -> 报失败")
    check("够不着" in res.reply, f"说明了失败原因: {res.reply}")

    print("序列模式: 服务端不支持时退回逐步骤:")
    class NoSeqArm(FakeArm):
        """旧版机械臂服务 —— 没有 grasp_sequence 接口。"""

        grasp_sequence = None

    arm13 = NoSeqArm()
    res, _, arm13, tr = await run([obj(0.5, 0.02, -0.3)], arm=arm13)
    check(res.ok, f"退回逐步骤也跑通了: {res.error or ''}")
    check(len(arm13.moves) == 2, f"退回后逐步骤移动: {len(arm13.moves)}")
    plays = [c[1] for c in arm13.calls
             if isinstance(c, tuple) and c[0] == "play"]
    check(plays == ["准备点", "闭合夹爪", "安全点"],
          f"退回后照常播动作: {plays}")

    print("序列模式: 可以用 sequence=false 退回逐步骤:")
    _, _, arm14, _ = await run([obj(0.5, 0.02, -0.3)], sequence=False)
    check(not arm14.sequences, "没合成序列")
    check(len(arm14.moves) == 2, f"逐步骤移了两次: {len(arm14.moves)}")

    print("序列模式: 流程名传给服务端:")
    arm15 = FakeArm(flows={"custom": custom})
    res, _, arm15, tr = await run([obj(0.5, 0.02, -0.3)], arm=arm15,
                                  flow="custom")
    seq_call = [c for c in arm15.calls
                if isinstance(c, tuple) and c[0] == "grasp_sequence"]
    check(res.ok and seq_call and seq_call[0][2] == "custom",
          f"流程名传对了: {seq_call[0][2] if seq_call else '?'}")


asyncio.run(main())
raise SystemExit(1 if failures else 0)
