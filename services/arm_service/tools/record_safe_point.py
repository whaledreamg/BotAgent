#!/usr/bin/env python3
"""录一个「固定点」动作 (安全点 / 准备点) —— 读当前实测关节角存成动作文件。

**为什么需要这个**: 抓取流程的第 1 步和第 5 步要回到**固定姿态**
(准备点 / 安全点)。这姿态必须现场示教 —— 抄别人的关节角可能撞限位。

用法::

    # 1. 手动把机械臂摆到想要的姿态 (要确保手臂上电、急停已松开)
    # 2. 录下来
    conda run -n yolo python services/arm_service/tools/record_safe_point.py --name 安全点

    # 指定某条臂 (默认两条都录)
    conda run -n yolo python services/arm_service/tools/record_safe_point.py \
        --name 安全点 --arm right

    # 看一下当前姿态 (不写文件)
    conda run -n yolo python services/arm_service/tools/record_safe_point.py --show

注意: 动作库里已有的同名文件会被**覆盖**。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from timeline.model import JOINT_COUNT  # noqa: E402

DEFAULT_URL = "http://127.0.0.1:8802"
DEFAULT_DATA_DIR = SERVICE_DIR / "data"


def _get(url: str, timeout: float = 8.0) -> Dict[str, Any]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(url: str, payload: Dict[str, Any], timeout: float = 8.0) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fmt(values: List[float]) -> str:
    return "[" + ", ".join(f"{v:.6f}" for v in values) + "]"


def main() -> int:
    ap = argparse.ArgumentParser(description="录一个固定点动作")
    ap.add_argument("--name", help="动作名 (文件名, 如 安全点)")
    ap.add_argument("--arm", default="both", choices=("left", "right", "both"))
    ap.add_argument("--url", default=DEFAULT_URL, help="arm_service 地址")
    ap.add_argument("--duration", type=float, default=3.0, help="动作时长 (秒)")
    ap.add_argument("--show", action="store_true", help="只打印当前姿态, 不写文件")
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    args = ap.parse_args()

    base = args.url.rstrip("/")

    # ---- 读当前运动学 ----
    try:
        kin = _get(f"{base}/v1/kinematics?refresh=true")
    except (urllib.error.URLError, OSError) as exc:
        print(f"连不上 arm_service ({base}): {exc}")
        print("先启动服务: bash scripts/start_arm.sh")
        return 1

    arms = kin.get("arms") or {}
    if not arms:
        print("没读到机械臂位姿 —— 控制器可能没连上, 或者机器人没使能")
        print(f"原始返回: {json.dumps(kin, ensure_ascii=False)[:400]}")
        return 1

    print("当前实测关节角:")
    picked: Dict[str, List[float]] = {}
    for arm_name, data in arms.items():
        joints = (data or {}).get("joints") or []
        if len(joints) < JOINT_COUNT:
            print(f"  {arm_name}: 关节角不完整 ({len(joints)} 个)")
            continue
        vals = [float(v) for v in joints[:JOINT_COUNT]]
        low = arm_name.lower()
        side = "left" if "left" in low else "right" if "right" in low else ""
        print(f"  {arm_name}: {_fmt(vals)}")
        if args.arm == "both" or side == args.arm:
            picked[side or "right"] = vals

    if args.show:
        return 0

    if not args.name:
        print("要写文件的话得给 --name (如 --name 安全点)")
        return 1
    if not picked:
        print(f"没选中任何臂 (--arm {args.arm})")
        return 1

    # ---- 写动作文件 ----
    end_joints: Dict[str, List[float]] = {}
    for side, vals in picked.items():
        key = "leftArm" if side == "left" else "rightArm"
        end_joints[key] = vals

    payload = {
        "segments": [
            {
                "duration": args.duration,
                "endJoints": end_joints,
                "interpolation": "cubic",
                "label": "机械1",
                "startTime": 0,
                "type": "arm",
            }
        ],
        "totalDuration": args.duration,
        "version": "4.0",
    }

    out = Path(args.data_dir) / f"{args.name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=4) + "\n", encoding="utf-8"
    )
    print()
    print(f"已写入: {out}")
    print(f"录的臂: {', '.join(picked)}")
    print("下次抓取流程的第 1 步/第 5 步就会用它。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
