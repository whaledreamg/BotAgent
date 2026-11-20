"""位姿换算 (纯 Python, arm_service 的 conda 环境没有 numpy)。

约定与控制器实时接口一致 (API 说明 21.9):
- 欧拉角 ``R = Rz(rz) · Ry(ry) · Rx(rx)``, 即 ``a=rx, b=ry, c=rz`` (rad)
- 四元数 ``(w, x, y, z)``, ``w`` 在前
"""

from __future__ import annotations

import math
from typing import Sequence, Tuple

Vec3 = Tuple[float, float, float]
Quat = Tuple[float, float, float, float]


def euler_to_quat(rx: float, ry: float, rz: float) -> Quat:
    """ZYX 欧拉角 -> 四元数 (w, x, y, z)。"""
    cx, sx = math.cos(rx / 2.0), math.sin(rx / 2.0)
    cy, sy = math.cos(ry / 2.0), math.sin(ry / 2.0)
    cz, sz = math.cos(rz / 2.0), math.sin(rz / 2.0)
    return (
        cz * cy * cx + sz * sy * sx,
        cz * cy * sx - sz * sy * cx,
        cz * sy * cx + sz * cy * sx,
        sz * cy * cx - cz * sy * sx,
    )


def quat_to_euler(q: Quat) -> Vec3:
    """四元数 -> ZYX 欧拉角 (rx, ry, rz)。"""
    w, x, y, z = q
    rx = math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y))
    s = max(-1.0, min(1.0, 2.0 * (w * y - z * x)))
    ry = math.asin(s)
    rz = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    return rx, ry, rz


def quat_normalize(q: Quat) -> Quat:
    n = math.sqrt(sum(c * c for c in q))
    if n < 1e-12:
        return (1.0, 0.0, 0.0, 0.0)
    return (q[0] / n, q[1] / n, q[2] / n, q[3] / n)


def quat_conj(q: Quat) -> Quat:
    return (q[0], -q[1], -q[2], -q[3])


def quat_mul(a: Quat, b: Quat) -> Quat:
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def quat_rotate(q: Quat, v: Sequence[float]) -> Vec3:
    """用四元数旋转向量。"""
    p = (0.0, float(v[0]), float(v[1]), float(v[2]))
    r = quat_mul(quat_mul(q, p), quat_conj(q))
    return (r[1], r[2], r[3])


def quat_angle(a: Quat, b: Quat) -> float:
    """两个姿态之间的夹角 (rad, 0~π)。"""
    dot = abs(sum(x * y for x, y in zip(a, b)))
    return 2.0 * math.acos(max(-1.0, min(1.0, dot)))


def slerp(a: Quat, b: Quat, t: float) -> Quat:
    """球面线性插值, 走短弧。"""
    dot = sum(x * y for x, y in zip(a, b))
    if dot < 0.0:
        b = (-b[0], -b[1], -b[2], -b[3])
        dot = -dot
    if dot > 0.9995:
        # 几乎同向: 退化为线性插值再归一化
        return quat_normalize(tuple(x + t * (y - x) for x, y in zip(a, b)))  # type: ignore[arg-type]
    theta = math.acos(max(-1.0, min(1.0, dot)))
    sin_theta = math.sin(theta)
    wa = math.sin((1.0 - t) * theta) / sin_theta
    wb = math.sin(t * theta) / sin_theta
    return quat_normalize(tuple(wa * x + wb * y for x, y in zip(a, b)))  # type: ignore[arg-type]


def distance(a: Sequence[float], b: Sequence[float]) -> float:
    return math.sqrt(sum((float(x) - float(y)) ** 2 for x, y in zip(a, b)))


Pose = Tuple[Vec3, Quat]


def pose_mul(a: Pose, b: Pose) -> Pose:
    """位姿复合 ``a ∘ b`` (先 b 后 a)。"""
    rp = quat_rotate(a[1], b[0])
    return (
        (a[0][0] + rp[0], a[0][1] + rp[1], a[0][2] + rp[2]),
        quat_normalize(quat_mul(a[1], b[1])),
    )


def pose_inv(a: Pose) -> Pose:
    """位姿求逆。"""
    inv_q = quat_conj(a[1])
    p = quat_rotate(inv_q, a[0])
    return ((-p[0], -p[1], -p[2]), inv_q)


def pose_from_xyzabc(d: dict) -> Pose:
    """``{x,y,z,a,b,c}`` -> 位姿 (a/b/c 为 rad, ZYX 欧拉)。"""
    return (
        (float(d["x"]), float(d["y"]), float(d["z"])),
        euler_to_quat(float(d["a"]), float(d["b"]), float(d["c"])),
    )


def world_to_base(
    base_pos: Sequence[float],
    base_q: Quat,
    pos: Sequence[float],
    q: Quat,
) -> Tuple[Vec3, Quat]:
    """把世界系下的位姿换到臂基座系。

    ``T_base_target = T_world_base^-1 · T_world_target``
    """
    inv = quat_conj(base_q)
    rel = (
        float(pos[0]) - float(base_pos[0]),
        float(pos[1]) - float(base_pos[1]),
        float(pos[2]) - float(base_pos[2]),
    )
    return quat_rotate(inv, rel), quat_normalize(quat_mul(inv, q))
