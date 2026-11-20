"""几何换算自检 (纯 Python, 不需要真机)。"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from robot.geometry import (  # noqa: E402
    euler_to_quat, quat_to_euler, quat_rotate, quat_angle,
    pose_mul, pose_inv, world_to_base,
)

ok = True


def check(name, cond, detail=""):
    global ok
    ok = ok and cond
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


for e in [(0.1, 0.2, 0.3), (-1.2, 0.4, 2.5), (3.0, -0.5, -2.0)]:
    r = quat_to_euler(euler_to_quat(*e))
    check(f"欧拉往返 {e}", all(abs(a - b) < 1e-9 for a, b in zip(e, r)))

v = quat_rotate(euler_to_quat(0, 0, math.pi / 2), (1, 0, 0))
check("绕 z 转 90° 把 x 轴转到 y 轴", abs(v[0]) < 1e-9 and abs(v[1] - 1) < 1e-9, str(v))

p = ((0.3, -0.2, 0.5), euler_to_quat(0.2, -0.4, 1.1))
i = pose_mul(p, pose_inv(p))
check("位姿乘逆 = 单位", all(abs(x) < 1e-9 for x in i[0]) and quat_angle(i[1], (1, 0, 0, 0)) < 1e-9)

base = ((0.1, 0.2, 0.3), euler_to_quat(0, 0, math.pi / 2))
tw = ((0.1, 1.2, 0.3), euler_to_quat(0, 0, math.pi / 2))
pos, q2 = world_to_base(base[0], base[1], tw[0], tw[1])
check("世界系 -> 基座系", abs(pos[0] - 1) < 1e-9 and abs(pos[1]) < 1e-9 and abs(pos[2]) < 1e-9, str(pos))
check("同姿态换到基座系为单位姿态", quat_angle(q2, (1, 0, 0, 0)) < 1e-9)

sys.exit(0 if ok else 1)
