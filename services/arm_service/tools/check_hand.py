#!/usr/bin/env python3
"""灵巧手 + 动作序列自检。

分两部分:

1. **离线** —— 帧构造是否与 ActionPlanner 的 ``HandController.cpp`` 逐字节
   一致; 序列合成的时序是否正确。不需要控制器。
2. **真机** (可选) —— 连控制器, 真的读一次手的位置、真的合一次手。

怎么用::

    # 离线自检
    conda run -n botagent python services/arm_service/tools/check_hand.py

    # 真机验证 (需要能连到控制器)
    conda run -n botagent python services/arm_service/tools/check_hand.py \\
        --controller 192.168.100.16

⚠️ 真机部分会**真的动手指** (合一次再张开)。仿真模式下无所谓, 真机上
要确保周围没人。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from library import ActionLibrary  # noqa: E402
from robot.hand import (  # noqa: E402
    DOF_COUNT,
    PRESETS,
    REG_READ_POS,
    REG_WRITE_ALL,
    HandController,
    HandError,
    build_read_positions_frame,
    build_write_all_frame,
    check_write_ack,
    crc16,
    deg_to_raw,
    parse_positions,
    raw_to_deg,
    verify_crc,
)
from sequence import compose_from_names, plan_from_flow  # noqa: E402
from timeline.model import JointState  # noqa: E402

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

_results: List[tuple] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    _results.append((name, condition, detail))
    tag = PASS if condition else FAIL
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail else ""))
    return condition


# ======================================================================
# 离线: 帧构造
# ======================================================================

def _ref_write_all_frame(slave: int, positions: List[int], time_ms: int) -> List[int]:
    """按 ActionPlanner ``buildWriteAllFrame`` 独立复算一遍。"""
    body = [slave & 0xFF, 0x10,
            (REG_WRITE_ALL >> 8) & 0xFF, REG_WRITE_ALL & 0xFF,
            0x00, 0x0C, 0x18]
    for pos in positions:
        pos = max(0, min(1000, pos))
        body += [(pos >> 8) & 0xFF, pos & 0xFF,
                 (time_ms >> 8) & 0xFF, time_ms & 0xFF]
    c = crc16(bytes(body))
    return body + [c & 0xFF, (c >> 8) & 0xFF]


def _ref_read_frame(slave: int) -> List[int]:
    """按 ActionPlanner ``buildReadPositionsFrame`` 独立复算一遍。"""
    body = [slave & 0xFF, 0x04,
            (REG_READ_POS >> 8) & 0xFF, REG_READ_POS & 0xFF,
            0x00, DOF_COUNT]
    c = crc16(bytes(body))
    return body + [c & 0xFF, (c >> 8) & 0xFF]


def check_offline() -> None:
    print("========== 离线: 协议帧 ==========")
    pose = PRESETS["一把抓"]

    frame = build_write_all_frame(127, pose, 100)
    check(
        "写帧与 ActionPlanner 逐字节一致",
        frame == _ref_write_all_frame(127, pose, 100),
        str(frame),
    )
    # 从站(1) + 功能码(1) + 起始寄存器(2) + 数量(2) + 字节数(1)
    # + 24 字节数据 + CRC(2) = 33
    check("写帧长度 33 字节", len(frame) == 33, f"{len(frame)}")
    check("写帧 CRC 正确", verify_crc(frame))
    check("写帧功能码 0x10", frame[1] == 0x10)
    check("写帧起始寄存器 0x03F2", (frame[2] << 8 | frame[3]) == REG_WRITE_ALL)

    rframe = build_read_positions_frame(127)
    check(
        "读帧与 ActionPlanner 逐字节一致",
        rframe == _ref_read_frame(127),
        str(rframe),
    )
    check("读帧 CRC 正确", verify_crc(rframe))
    check("读帧功能码 0x04", rframe[1] == 0x04)
    check("读帧起始寄存器 0x07D0", (rframe[2] << 8 | rframe[3]) == REG_READ_POS)

    # 越界值要被夹紧
    clamped = build_write_all_frame(127, [2000, -5, 0, 0, 0, 0], 100)
    check("越界位置被夹到 0~1000", clamped[7] == 0x03 and clamped[8] == 0xE8,
          f"{clamped[7] << 8 | clamped[8]}")

    # CRC 校验要能认出坏帧
    bad = list(frame)
    bad[-1] ^= 0xFF
    check("坏帧被 CRC 校验挡下", not verify_crc(bad))

    # 写应答
    check("写应答校验通过", _ack_ok())
    check("异常功能码被挡下", _exception_rejected())

    # 位置解析
    rx = [127, 4, 12, 0, 10, 0, 20, 0, 30, 0, 40, 0, 50, 0, 60, 95, 136]
    check("读应答解出 6 个 DOF",
          parse_positions(rx) == [10, 20, 30, 40, 50, 60],
          str(parse_positions(rx)))

    # ---- 真机上抓到的帧 (回归用) ----
    #
    # 2026-11-25 在控制器 192.168.100.16 上实测: 通道
    # ``dualEc2485:Master1/EtherCAT485R``, 波特率 115200, 从站 127,
    # 读寄存器 0x07D0 拿到的真实应答。
    #
    # 这一帧是**只读寄存器**的应答, 不是「写完再读」—— 所以它证明的是
    # 「寄存器 0x07D0 + 从站 127 + 波特率 115200」这一组参数是对的。
    real = [127, 4, 12, 1, 144, 1, 144, 0, 49, 0, 50, 0, 49, 0, 50, 95, 136]
    check("真机帧 CRC 正确", verify_crc(real))
    check("真机帧长度 17 字节", len(real) == 17, str(len(real)))
    check("真机帧功能码 0x04", real[1] == 0x04)
    check("真机帧字节数 12", real[2] == 12, str(real[2]))
    check("真机帧解出 6 个 DOF",
          parse_positions(real) == [400, 400, 49, 50, 49, 50],
          str(parse_positions(real)))
    check("真机帧的值都在 0~1000",
          all(0 <= v <= 1000 for v in parse_positions(real)))

    print()
    print("========== 离线: 预设与换算 ==========")
    check("一把抓 = [1000,0,1000,1000,1000,1000]",
          PRESETS["一把抓"] == [1000, 0, 1000, 1000, 1000, 1000])
    check("释放 = 全 0", PRESETS["释放"] == [0] * 6)
    check("拇指弯曲 0 度 -> 0", deg_to_raw(0, 0) == 0)
    check("拇指弯曲 59 度 -> 1000", deg_to_raw(59, 0) == 1000)
    check("小指 81 度 -> 1000", deg_to_raw(81, 5) == 1000)
    check("反算 1000 -> 最大角度", abs(raw_to_deg(1000, 1) - 90.0) < 1e-6)

    print()
    print("========== 离线: 动作序列合成 ==========")
    lib = ActionLibrary(SERVICE_DIR / "data")
    lib.load()

    seq = compose_from_names(
        lib, ["准备点", "闭合夹爪", "安全点"], JointState.zero()
    )
    check("合成出 3 段", seq.timeline.segment_count == 3,
          str(seq.timeline.segment_count))

    segs = seq.timeline.segments
    check("片段时间不重叠 (首尾相接)",
          all(segs[i].end_time <= segs[i + 1].start_time + 1e-9
              for i in range(len(segs) - 1)),
          str([(s.start_time, s.end_time) for s in segs]))
    check("总时长等于各段之和",
          abs(seq.duration - sum(s.duration for s in segs)) < 1e-6,
          f"{seq.duration}")

    check("夹爪指令 1 个", len(seq.cues) == 1, str(len(seq.cues)))
    if seq.cues:
        cue = seq.cues[0]
        check("夹爪指令在准备点结束之后",
              cue.time >= segs[0].end_time - 1e-9,
              f"t={cue.time} vs 准备点结束 {segs[0].end_time}")
        check("夹爪手位是一把抓",
              cue.right == PRESETS["一把抓"], str(cue.right))

    print()
    print("========== 离线: 流程解析 ==========")
    try:
        from flow import load_flows
        flows = load_flows(SERVICE_DIR.parents[1] / "configs" / "flows")
        flow = flows.get("grasp")
    except Exception as exc:
        print(f"  (跳过: 读流程失败 {exc})")
        flow = None

    if flow is not None:
        plan = plan_from_flow(
            flow, (0.55, -0.30, -0.15), approach_distance=0.10,
            defaults={"close_action": "闭合夹爪", "open_action": "张开夹爪"},
        )
        ids = [s.id for s in plan]
        check("流程解析出 5 步 (detect 不产生动作)",
              ids == ["ready", "above", "grasp", "close", "safe"], str(ids))
        above = next(s for s in plan if s.id == "above")
        grasp = next(s for s in plan if s.id == "grasp")
        check("上方点 = 目标 + 0.10",
              abs(above.pos[2] - (-0.05)) < 1e-6, str(above.pos))
        check("抓取点 = 目标", abs(grasp.pos[2] - (-0.15)) < 1e-6, str(grasp.pos))


def _ack_ok() -> bool:
    """构造一个合法的 0x10 应答, 看校验器认不认。"""
    body = [127, 0x10, 0x03, 0xF2, 0x00, 0x0C]
    c = crc16(bytes(body))
    rx = body + [c & 0xFF, (c >> 8) & 0xFF]
    try:
        check_write_ack(rx)
        return True
    except HandError:
        return False


def _exception_rejected() -> bool:
    """构造一个从站异常应答 (功能码 0x90), 看校验器认不认。"""
    body = [127, 0x90, 0x02]
    c = crc16(bytes(body))
    rx = body + [c & 0xFF, (c >> 8) & 0xFF]
    try:
        check_write_ack(rx)
        return False
    except HandError:
        return True


# ======================================================================
# 真机
# ======================================================================

def check_live(controller_ip: str, do_move: bool) -> None:
    print(f"========== 真机: {controller_ip} ==========")
    from robot.connection import RobotConnection

    conn = RobotConnection(f"ws://{controller_ip}:9000")
    if not conn.connect():
        check("连上控制器", False, controller_ip)
        return
    check("连上控制器", True, controller_ip)

    try:
        hc = HandController(conn)
        try:
            hc.connect()
            check("灵巧手连接 + 串口初始化", True)
        except HandError as exc:
            check("灵巧手连接 + 串口初始化", False, str(exc))
            return

        status = hc.status()
        print(f"    左手: {json.dumps(status['left'], ensure_ascii=False)}")
        print(f"    右手: {json.dumps(status['right'], ensure_ascii=False)}")

        # ---- 读回当前位置 ----
        pos = hc.refresh_positions()
        check("读回双手位置", "right" in pos, str(pos))
        if "right" in pos:
            print(f"    右手当前: {pos['right']}")

        if not do_move:
            print("  (跳过实际开合, 加 --move 才动)")
            return

        # ---- 合上 ----
        try:
            hc.send_preset("一把抓")
            check("下发「一把抓」", True)
        except HandError as exc:
            check("下发「一把抓」", False, str(exc))
            return
        time.sleep(1.0)
        after = hc.refresh_positions()
        check("手位确实变了", after.get("right") != pos.get("right"),
              f"{pos.get('right')} -> {after.get('right')}")

        # ---- 张开 ----
        try:
            hc.send_preset("释放")
            check("下发「释放」", True)
        except HandError as exc:
            check("下发「释放」", False, str(exc))
            return
        time.sleep(1.0)
        final = hc.refresh_positions()
        check("回到张开状态", final.get("right") == [0] * DOF_COUNT,
              str(final.get("right")))
    finally:
        conn.disconnect()


def main() -> int:
    ap = argparse.ArgumentParser(description="灵巧手 + 动作序列自检")
    ap.add_argument("--controller", help="控制器 IP (如 192.168.100.16)")
    ap.add_argument("--move", action="store_true",
                    help="真机部分真的动手指 (合一次再张开)")
    args = ap.parse_args()

    check_offline()

    if args.controller:
        print()
        check_live(args.controller, args.move)
    else:
        print()
        print("========== 真机 ==========")
        print("  (跳过 —— 加 --controller <ip> 连真机)")

    print()
    failed = [r for r in _results if not r[1]]
    print(f"共 {len(_results)} 项, 失败 {len(failed)} 项")
    for name, _ok, detail in failed:
        print(f"  [FAIL] {name}  {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
