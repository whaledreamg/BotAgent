#!/usr/bin/env python3
"""识别物体 -> 抓取 的端到端测试脚本。

**两种跑法** —— 走的**不是同一条路**:

``--via-agent`` (推荐)
    走**线上真正跑的那条路**: agent_service 收到「抓瓶子」-> LLM tool
    calling -> ``grasp_object`` -> ``vision.grasp`` skill -> 视觉识别 ->
    机械臂服务合成序列 -> 下发。

    验证整条链路 (含 agent 的编排、播报、追溯) 用这个。

默认 (直接调机械臂服务)
    脚本自己拍一张照识别, 然后直接调 ``POST /v1/grasp_sequence``。

    **跳过 agent** —— 所以测不到 agent 那层 (LLM 路由、tool calling、
    播报文案)。机械臂/视觉那部分是相同的。

**两条路的差别**:

- 目标选择: 都用「离真实臂基座最近 + 排除深度无效候选」, 但默认模式
  由脚本自己选, skill 模式由 skill 选 (逻辑一致)
- ``approach_distance``: skill 模式取**流程定义里的值** (服务端返回);
  默认模式不给就走服务端默认 (同样是流程里的值)

**跟 ``run_bottle_flow.py`` 的区别**: 那个脚本用**写死的历史坐标**,
只测机械臂; 这个脚本**真的去拍一张照识别**, 测的是「相机 -> 识别 ->
抓取」的完整链路 —— 相机拔了就会失败, 正好当相机在位检查。

**用法**::

    # 走线上那条路 (推荐)
    python3 services/arm_service/tools/test_grasp_from_camera.py --via-agent

    # 直接调机械臂服务 (跳过 agent)
    python3 services/arm_service/tools/test_grasp_from_camera.py

    # 换个物体 / 换条臂
    python3 services/arm_service/tools/test_grasp_from_camera.py --target 杯子 --arm left

    # 只识别不抓 (相机自检)
    python3 services/arm_service/tools/test_grasp_from_camera.py --detect-only

    # 多个同类物体时选第 2 个
    python3 services/arm_service/tools/test_grasp_from_camera.py --index 1
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ARM_BASE = "http://127.0.0.1:8802"
VISION_BASE = "http://127.0.0.1:8803"
AGENT_BASE = "http://127.0.0.1:8800"


# ---------------------------------------------------------------- HTTP


def _request(url: str, body=None, timeout: float = 60.0) -> dict:
    """发一个 HTTP 请求, 返回解析后的 JSON (失败不抛异常)。"""
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    if body is not None:
        req.get_method = lambda: "POST"
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode() or "{}")
        except Exception:
            return {"ok": False, "code": "http_%d" % exc.code,
                    "message": str(exc)}
    except Exception as exc:
        return {"ok": False, "code": "error", "message": str(exc)}


def vision_get(path: str, **params) -> dict:
    url = VISION_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _request(url, timeout=90.0)


def arm_get(path: str, **params) -> dict:
    url = ARM_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _request(url, timeout=30.0)


def arm_post(path: str, body: dict, timeout: float = 300.0) -> dict:
    return _request(ARM_BASE + path, body=body, timeout=timeout)


# ---------------------------------------------------------------- 视觉


def detect(target: str, frame: str = "world", fresh: bool = True) -> list:
    """拍一张找目标物体, 返回候选列表 (按置信度从高到低)。

    先查中文名, 查不到再查英文名 —— ``/v1/objects/{name}`` 两种都认。
    """
    url = "/v1/objects/" + urllib.parse.quote(target)
    res = vision_get(url, frame=frame, fresh="true" if fresh else "false")
    hits = res.get("objects") or []
    if hits:
        return hits

    # 中文名没命中 -> 试英文 (「瓶子」-> bottle)
    for en in ("bottle", "cup", "can", "box", "book", "phone", "apple"):
        res = vision_get("/v1/objects/" + en, frame=frame,
                         fresh="true" if fresh else "false")
        hits = res.get("objects") or []
        if hits:
            return hits
    return []


def pick_target(hits: list, side: str, index: int, base_pos=None):
    """从候选里挑一个。

    默认挑**离这条臂基座最近**的那个 (按距离), ``--index`` 则直接
    取列表里的第 N 个 (按置信度排好序的)。

    **深度不可信的候选直接排除** —— 它们的坐标是瞎猜的 (比如刚才就抓到
    一个 x=1.81m 的幽灵瓶子), 拿去逆解必然失败。

    Args:
        base_pos: 臂基座在世界系下的真实位置。**从机械臂服务读** ——
            硬编码的位置跟实际差 20~40cm, 会选错物体。
    """
    if not hits:
        return None

    # 深度有效 (depth_valid) 的优先 —— 深度不可信的候选坐标不可信
    reliable = [o for o in hits if (o.get("pose") or {}).get("depth_valid", True)]
    pool = reliable or hits

    if index is not None:
        if index >= len(pool):
            return None
        return pool[index]

    if base_pos is None:
        # 读不到基座 -> 退化成「按置信度」, 并在日志里说明
        return max(pool, key=lambda o: o.get("confidence") or 0.0)

    bx, by = float(base_pos[0]), float(base_pos[1])

    def key(o):
        p = o.get("pose") or {}
        return math.hypot((p.get("x") or 0.0) - bx, (p.get("y") or 0.0) - by)

    return sorted(pool, key=key)[0]


def base_of(side: str):
    """读这条臂的**真实**基座位置 (世界系)。

    从机械臂服务拿 (``/v1/kinematics``) —— 不要硬编码: 实测基座在
    ``(0, -0.02, 0)``, 而写死的 ``(0, -0.42, 0)`` 差了 40cm, 多个
    同类物体时会选错。
    """
    snap = arm_get("/v1/kinematics", refresh="true")
    arms = snap.get("arms") or {}
    for name, data in arms.items():
        if side in name.lower():
            bf = data.get("base_frame") or {}
            try:
                return (float(bf["x"]), float(bf["y"]), float(bf["z"]))
            except (KeyError, TypeError, ValueError):
                return None
    return None


# ---------------------------------------------------------------- 汇报


def print_objects(hits: list, chosen) -> None:
    if not hits:
        print("  (没识别到)")
        return
    for i, o in enumerate(hits):
        p = o.get("pose") or {}
        mark = " <== 选中" if o is chosen else ""
        print("  [%d] %-10s conf=%.3f  pos=(%+.4f, %+.4f, %+.4f)  "
              "frame=%s  depth_ok=%s%s"
              % (i, o.get("label") or "?", o.get("confidence") or 0.0,
                 p.get("x") or 0.0, p.get("y") or 0.0, p.get("z") or 0.0,
                 p.get("frame") or "?", p.get("depth_valid"), mark))


def report_ik(ik_list: list) -> None:
    for ik in ik_list or []:
        print("    逆解 %-8s 位置误差 %7.3f mm   姿态误差 %6.3f deg   "
              "迭代 %3d   at_limit=%s"
              % (ik.get("step"), ik.get("err_pos_mm") or 0.0,
                 ik.get("err_rot_deg") or 0.0, ik.get("iters") or 0,
                 ik.get("at_limit")))


def report_steps(seq: dict) -> None:
    for s in (seq.get("steps") or []):
        if "start" in s and "end" in s:
            print("    %-6s %-10s [%.2f ~ %.2f]"
                  % (s.get("type"), s.get("label"), s["start"], s["end"]))
        elif "time" in s:
            print("    %-6s %-10s @%.2f"
                  % (s.get("type"), s.get("label"), s["time"]))
        else:
            print("    %-6s %-10s" % (s.get("type"), s.get("label") or ""))


def tcp_of(side: str) -> dict:
    snap = arm_get("/v1/kinematics", refresh="true")
    arms = snap.get("arms") or {}
    for name, data in arms.items():
        if side in name.lower():
            return data.get("cartesian") or {}
    return {}


def run_via_agent(args) -> int:
    """走**原始的 skill 路径** —— agent_service 的 ``vision.grasp``。

    这是线上真正跑的那条路: agent 收到「抓瓶子」-> LLM tool calling
    -> ``grasp_object`` -> skill -> 视觉识别 -> 机械臂服务合成序列。

    **跟直接调 ``/v1/grasp_sequence`` 的区别**:

    - 目标选择用的是 skill 里的逻辑 (读真实基座、排除深度无效的候选)
    - ``approach_distance`` 取的是**流程定义里的值** (服务端返回的),
      而不是调用参数
    - 整个流程的编排、播报文案、追溯记录都在 skill 里

    所以要验证「线上那条路能不能跑通」, 用这个模式。
    """
    health = _request(AGENT_BASE + "/health", timeout=10.0)
    if not health.get("status"):
        print("[FAIL] agent 服务不可达 (%s)" % AGENT_BASE)
        return 2
    print("  agent 服务 OK  分支=%s" % health.get("branches"))

    text = "抓%s" % args.target
    body = {
        "session_id": "cam_test_%d" % int(time.time()),
        "text": text,
        "use_history": False,
    }
    t0 = time.time()
    res = _request(AGENT_BASE + "/v1/dispatch", body=body, timeout=180.0)
    elapsed = time.time() - t0

    action = res.get("action") or {}
    ok = action.get("status") == "ok"
    print("  dispatch(%r): status=%s  耗时 %.1fs"
          % (text, action.get("status"), elapsed))
    if action.get("reply"):
        print("  播报: %s" % action["reply"])
    if not ok:
        print("  detail: %s" % (action.get("detail") or res.get("reply")))

    # 追溯记录里能看到真实的序列和逆解
    trace = read_last_trace()
    if trace:
        print("\n  追溯记录 (grasp_trace.jsonl):")
        print("    outcome: %s" % trace.get("outcome"))
        print("    approach_distance: %s" % trace.get("approach_distance"))
        seq = trace.get("sequence") or {}
        if seq.get("steps"):
            print("    时间轴:")
            report_steps(seq)
        print("    逆解:")
        report_ik(trace.get("ik") or [])

    print("\n" + "=" * 74)
    print("结果: %s" % ("完成 ✓" if ok else "失败 ✗"))
    print("=" * 74)
    return 0 if ok else 1


def read_last_trace() -> dict:
    """读 skill 写的最后一条追溯记录。"""
    path = Path(__file__).resolve().parents[2] / "agent_service" / "logs" / \
        "grasp_trace.jsonl"
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
        return json.loads(lines[-1]) if lines else {}
    except Exception:
        return {}


# ---------------------------------------------------------------- 主流程


def main() -> int:
    ap = argparse.ArgumentParser(
        description="识别物体 -> 抓取 端到端测试")
    ap.add_argument("--target", default="瓶子",
                    help="要抓的物体名 (中文或英文, 默认「瓶子」)")
    ap.add_argument("--arm", default="right", choices=["left", "right"],
                    help="用哪条臂 (默认 right)")
    ap.add_argument("--index", type=int, default=None,
                    help="挑第几个识别结果 (默认挑离该臂最近的那个)")
    ap.add_argument("--frame", default="world",
                    help="识别坐标系: world / camera / arm_base "
                         "(默认 world)")
    ap.add_argument("--flow", default="grasp", help="流程名 (默认 grasp)")
    ap.add_argument("--approach", type=float, default=None,
                    help="上方点高度 (米), 不给用流程配置值")
    ap.add_argument("--detect-only", action="store_true",
                    help="只识别, 不抓")
    ap.add_argument("--no-fresh", action="store_true",
                    help="用缓存的识别结果, 不重新拍")
    ap.add_argument("--verify", action="store_true",
                    help="流程跑完后再单独 move_to 一次到目标点, "
                         "用实测 TCP 核对逆解精度")
    ap.add_argument("--via-agent", action="store_true",
                    help="走**原始的 skill 路径** (agent_service 的 "
                         "vision.grasp), 而不是直接调机械臂服务。\n"
                         "            这才是线上真正跑的那条路 —— "
                         "要验证整条链路就用这个。")
    args = ap.parse_args()

    print("=" * 74)
    print("识别物体 -> 抓取  (目标=%s, 臂=%s)" % (args.target, args.arm))
    print("=" * 74)

    # ---- 服务在位检查 ----
    vs = vision_get("/health")
    if not vs.get("camera_connected"):
        print("[FAIL] 视觉服务没连上相机: %s" % vs.get("errors"))
        return 2
    print("  视觉服务 OK   相机=%s  设备=%s  帧=%s"
          % (vs.get("camera_name"), vs.get("device"), vs.get("frame")))

    as_ = arm_get("/health")
    if not as_.get("status") and not as_.get("ok"):
        print("[FAIL] 机械臂服务不可达")
        return 2
    print("  机械臂服务 OK  控制总闸=%s" % as_.get("control_enabled"))
    print()

    # ---- 第 1 步: 识别 ----
    print("-" * 74)
    print("第 1 步: 识别「%s」" % args.target)
    print("-" * 74)
    hits = detect(args.target, frame=args.frame, fresh=not args.no_fresh)
    base_pos = base_of(args.arm)
    chosen = pick_target(hits, args.arm, args.index, base_pos)
    print_objects(hits, chosen)
    if base_pos:
        print("  (臂基座实测位置: (%+.4f, %+.4f, %+.4f))" % base_pos)

    if not chosen:
        print("\n[FAIL] 没识别到「%s」" % args.target)
        return 1

    pose = chosen.get("pose") or {}
    x, y, z = pose.get("x"), pose.get("y"), pose.get("z")
    print("\n  选中: %s  conf=%.3f  坐标=(%+.4f, %+.4f, %+.4f) [%s]"
          % (chosen.get("label"), chosen.get("confidence") or 0.0,
             x, y, z, pose.get("frame")))

    if args.detect_only:
        print("\n[OK] --detect-only: 识别成功, 不抓")
        return 0

    # ---- 第 2 步: 抓取 ----
    print()
    print("-" * 74)
    if args.via_agent:
        print("第 2 步: 走原始 skill 路径 (agent_service -> vision.grasp)")
    else:
        print("第 2 步: 跑抓取流程 (%s)" % args.flow)
    print("-" * 74)

    if args.via_agent:
        return run_via_agent(args)

    body = {
        "x": x, "y": y, "z": z,
        "side": args.arm,
        "flow": args.flow,
        "speak": False,
    }
    if args.approach is not None:
        body["approach_distance"] = args.approach

    t0 = time.time()
    res = arm_post("/v1/grasp_sequence", body)
    elapsed = time.time() - t0

    ok = bool(res.get("ok"))
    print("  流程返回: ok=%s  耗时 %.1fs" % (ok, elapsed))
    if not ok:
        print("  code=%s  message=%s"
              % (res.get("code"), res.get("message")))
        print("  detail=%s" % json.dumps(res.get("detail") or {},
                                        ensure_ascii=False)[:400])
        return 1

    print("\n  时间轴:")
    report_steps(res.get("sequence") or {})

    print("\n  逆解:")
    report_ik(res.get("ik") or [])

    # ---- 第 3 步: 到位核对 ----
    print()
    print("-" * 74)
    print("第 3 步: 到位核对")
    print("-" * 74)
    if args.verify:
        # 流程最后一步是「回安全点」, 所以流程跑完 TCP 停在安全点,
        # 不是目标点 —— 想看**实测**到位情况, 得单独再走一次 move_to。
        print("  (--verify) 单独 move_to 到目标点, 用实测 TCP 核对")
        mv = arm_post("/v1/move_to", {
            "arm": args.arm, "x": x, "y": y, "z": z,
            "duration": 4.0, "speak": False,
        })
        print("  move_to: ok=%s  code=%s" % (mv.get("ok"), mv.get("code")))
        if mv.get("message"):
            print("  %s" % mv["message"])
        ik = mv.get("ik") or {}
        if ik:
            print("  逆解: 位置误差 %s mm / 姿态误差 %s deg / 迭代 %s"
                  % (ik.get("err_pos_mm"), ik.get("err_rot_deg"),
                     ik.get("iters")))
        if mv.get("error_m") is not None:
            print("  实测误差: %.1f mm" % (mv["error_m"] * 1000))
        if mv.get("tcp_after"):
            ta = mv["tcp_after"]
            print("  实测 TCP = (%+.4f, %+.4f, %+.4f)"
                  % (ta.get("x", 0), ta.get("y", 0), ta.get("z", 0)))
        ok = bool(mv.get("ok")) or ok
    else:
        tcp = tcp_of(args.arm)
        if tcp:
            d = math.dist((tcp.get("x", 0), tcp.get("y", 0), tcp.get("z", 0)),
                          (x, y, z))
            print("  目标      = (%+.4f, %+.4f, %+.4f)" % (x, y, z))
            print("  当前 TCP  = (%+.4f, %+.4f, %+.4f)"
                  % (tcp.get("x", 0), tcp.get("y", 0), tcp.get("z", 0)))
            print("  距离      = %.1f mm" % (d * 1000))
            print("  (注: 流程最后一步是「回安全点」, 这里看到的是安全点位置)")
        else:
            print("  (读不回 TCP 状态)")

    print("\n" + "=" * 74)
    print("结果: %s" % ("完成 ✓" if ok else "失败 ✗"))
    print("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
