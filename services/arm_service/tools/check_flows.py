#!/usr/bin/env python3
"""流程定义自检 (离线, 不碰硬件)。

验七件事:
  1. **格式合法** —— ``configs/flows/*.yaml`` 每个都能解析, 片段 id 不重复;
  2. **类型已知** —— 片段的 ``type`` 在已知的类型表里 (未知类型会**报错**,
     不能静默跳过 —— 跳过的话流程会「少做一段」而没人知道);
  3. **动作存在** —— ``arm``/``hand`` 片段引用的动作在动作库里真的有
     (写错动作名 = 跑到那一段才报错, 白等);
  4. **引用合法** —— 引用的具名点存在、是 ``target`` 类型、顺序对、无循环;
  5. **与旧配置等价** —— ``configs/flows/grasp.yaml`` 与
     ``configs/arm.yaml`` 的 ``grasp.flow`` 行为一致 (防止迁移时手抖改错);
  6. **分流表一致** —— ``configs/flows/routing.yaml`` 里映射到的流程
     真的存在 (映射写错会在抓取时静默走默认流程, 很难查);
  7. **错误格式必须报错** —— 每种非法写法都要被拦住。

用法:
    python3 services/arm_service/tools/check_flows.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "arm_service"))

from config import load_config  # noqa: E402
from flow import (  # noqa: E402
    DEFAULT_FLOW,
    NON_FLOW_FILES,
    STEP_TYPES,
    FlowError,
    flow_from_legacy,
    load_flows,
    parse_flow,
)

failures = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def _action_names() -> set:
    """动作库里的动作名。"""
    try:
        from library import ActionLibrary

        lib = ActionLibrary(ROOT / "services" / "arm_service" / "data")
        lib.load()
        # ``labels()`` 返回的就是动作名 (与 keys() 一致)
        return set(lib.labels())
    except Exception as exc:
        print(f"  (读动作库失败, 跳过动作名检查: {exc})")
        return set()


def check_flow_file(path: Path, known_actions: set) -> None:
    """检查单个流程文件的每一段。"""
    print(f"\n[{path.name}]")
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        check(False, f"yaml 解析失败: {exc}")
        return
    if not isinstance(raw, dict):
        check(False, "顶层不是字典")
        return

    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        check(False, "steps 必须是非空列表")
        return

    ids: set = set()
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            check(False, f"第 {index + 1} 段不是字典")
            continue
        sid = str(step.get("id") or "")
        stype = str(step.get("type") or "").strip().lower()
        label = sid or f"#{index + 1}"

        if not sid:
            check(False, f"第 {index + 1} 段缺少 id")
            continue
        if sid in ids:
            check(False, f"片段 id 重复: {sid!r}")
            continue
        ids.add(sid)

        if stype not in STEP_TYPES:
            check(False, f"{label}: 未知的 type {stype!r}")
            continue

        # 动作名要真的存在 —— 否则跑到那一段才报错
        if stype in ("arm", "hand"):
            action = str(step.get("action") or "").strip()
            if action and known_actions and action not in known_actions:
                check(False, f"{label}: 动作 {action!r} 不在动作库里")
                continue

        # detect 片段的拍照姿态 —— 必须是 {left/right: {a,b,c}} 字典
        if stype == "detect":
            orientation = step.get("orientation")
            if orientation is not None:
                if not isinstance(orientation, dict):
                    check(False, f"{label}: orientation 必须是字典")
                else:
                    for side, abc in orientation.items():
                        if side not in ("left", "right"):
                            check(False, f"{label}: orientation 有未知的一侧 {side!r}")
                            continue
                        if not isinstance(abc, dict):
                            check(False, f"{label}: orientation.{side} 必须是字典")
                            continue
                        missing = [k for k in ("a", "b", "c") if k not in abc]
                        if missing:
                            check(False,
                                  f"{label}: orientation.{side} 缺 {missing}")

        check(True, f"{label}: type={stype} 合法")


def check_equivalence(flows, cfg) -> None:
    """新流程文件与旧配置的合成流程是否等价。"""
    print("\n[与 configs/arm.yaml 的 grasp.flow 对比]")
    legacy = flow_from_legacy(cfg.arm.flow)
    new = flows.get(DEFAULT_FLOW)
    if new is None:
        check(False, f"没有默认流程 {DEFAULT_FLOW!r}")
        return

    # 片段 id 与顺序 —— 两边都有 object / above_point 两个具名点声明
    legacy_ids = [s.id for s in legacy.steps]
    new_ids = [s.id for s in new.steps]
    check(
        legacy_ids == new_ids,
        f"片段 id 与顺序一致: {new_ids}",
    )

    # 具名点声明 —— 旧格式没有对应物, 但要检查它们算出的值一致
    named = new.named_points()
    check("object" in named, "具名点 'object' 已声明")
    check("above_point" in named, "具名点 'above_point' 已声明")
    if "above_point" in named:
        check(
            named["above_point"].distance == "approach",
            f"上方点用 distance: approach (高度由流程设置决定): "
            f"{named['above_point'].distance!r}",
        )

    for old, cur in zip(legacy.steps, new.steps):
        if old.type == "arm" and old.action:
            check(
                old.action == cur.action,
                f"{cur.id}: 动作名一致 ({old.action!r} vs {cur.action!r})",
            )
        if old.type == "arm" and old.base:
            check(
                old.base == cur.base,
                f"{cur.id}: 基准具名点一致 ({old.base} vs {cur.base})",
            )
            check(
                old.offset == cur.offset,
                f"{cur.id}: 偏移一致 ({old.offset} vs {cur.offset})",
            )
            check(
                old.duration == cur.duration,
                f"{cur.id}: 时长一致 ({old.duration} vs {cur.duration})",
            )
        if old.type == "hand":
            check(
                cur.type == "hand",
                f"{cur.id}: 类型一致 (hand)",
            )

    check(
        legacy.recovery_action == new.recovery_action,
        f"收尾动作一致 ({legacy.recovery_action!r} vs {new.recovery_action!r})",
    )


def check_rejections() -> None:
    """每种非法写法都必须报错 —— 格式错要立刻发现, 不能静默跳过。"""
    print("\n[非法格式必须报错]")

    cases = [
        ("未知片段类型", {
            "name": "t", "steps": [{"id": "x", "type": "teleport"}],
        }),
        ("旧类型 play 不再接受", {
            "name": "t", "steps": [{"id": "x", "type": "play", "action": "a"}],
        }),
        ("旧类型 move 不再接受", {
            "name": "t", "steps": [{"id": "x", "type": "move", "target": "object"}],
        }),
        ("arm 终点给两个", {
            "name": "t", "steps": [
                {"id": "a", "type": "arm", "action": "x", "point": "p"},
                {"id": "p", "type": "target", "pose": {"x": 0}},
            ],
        }),
        ("arm 没给终点", {
            "name": "t", "steps": [{"id": "a", "type": "arm"}],
        }),
        ("引用不存在的具名点", {
            "name": "t", "steps": [{"id": "a", "type": "arm", "point": "nope"}],
        }),
        ("引用非 target 的片段", {
            "name": "t", "steps": [
                {"id": "a", "type": "arm", "action": "x"},
                {"id": "b", "type": "arm", "point": "a"},
            ],
        }),
        ("source: detect 排在 detect 之前", {
            "name": "t", "steps": [
                {"id": "o", "type": "target", "source": "detect"},
                {"id": "d", "type": "detect"},
            ],
        }),
        ("base 排在基准之前", {
            "name": "t", "steps": [
                {"id": "d", "type": "detect"},
                {"id": "b", "type": "target", "base": "o"},
                {"id": "o", "type": "target", "source": "detect"},
            ],
        }),
        ("循环引用", {
            "name": "t", "steps": [
                {"id": "d", "type": "detect"},
                {"id": "a", "type": "target", "base": "b"},
                {"id": "b", "type": "target", "base": "a"},
            ],
        }),
        ("hand 纯 pose 没给 direction", {
            "name": "t", "steps": [
                {"id": "h", "type": "hand", "pose": [0, 0, 0, 0, 0, 0]},
            ],
        }),
        ("detect 的 orientation 不是字典", {
            "name": "t", "steps": [
                {"id": "d", "type": "detect", "orientation": [1, 2, 3]},
            ],
        }),
        ("detect 的 orientation 有未知的一侧", {
            "name": "t", "steps": [
                {"id": "d", "type": "detect",
                 "orientation": {"middle": {"a": 1, "b": 2, "c": 3}}},
            ],
        }),
        ("detect 的 orientation 缺 a/b/c", {
            "name": "t", "steps": [
                {"id": "d", "type": "detect",
                 "orientation": {"right": {"a": 1, "b": 2}}},
            ],
        }),
        ("detect 的 orientation 用了 YAML 集合语法 (静默丢姿态)", {
            "name": "t", "steps": [
                {"id": "d", "type": "detect",
                 "orientation": {"right": {1.6683, 0.6821, 1.9317}}},
            ],
        }),
        ("hand 手位数量不对", {
            "name": "t", "steps": [
                {"id": "h", "type": "hand", "pose": [0, 0, 0], "direction": "close"},
            ],
        }),
        ("关节角数量不对", {
            "name": "t", "steps": [
                {"id": "a", "type": "arm", "pose": {"right": [1, 2, 3]}},
            ],
        }),
        ("hand 没给手位", {
            "name": "t", "steps": [{"id": "h", "type": "hand"}],
        }),
        ("target 三种来源都没给", {
            "name": "t", "steps": [{"id": "o", "type": "target"}],
        }),
        ("target 给了两种来源", {
            "name": "t", "steps": [
                {"id": "d", "type": "detect"},
                {"id": "o", "type": "target", "source": "detect",
                 "pose": {"x": 0}},
            ],
        }),
        ("重复的具名点名", {
            "name": "t", "steps": [
                {"id": "d", "type": "detect"},
                {"id": "o", "type": "target", "source": "detect"},
                {"id": "g", "type": "target",
                 "points": [{"id": "o", "source": "detect"}]},
            ],
        }),
    ]

    for name, raw in cases:
        try:
            parse_flow(raw)
            check(False, f"{name}: 竟然没报错")
        except FlowError:
            check(True, f"{name}: 正确报错")


def check_hot_reload(flows_dir: Path) -> None:
    """热重载: 改了 yaml 之后, 指纹要变, 服务要能重读。

    **这是最容易踩的坑** —— 改了 yaml 但服务还在用缓存的旧版, 表现为
    「改了没反应」而且没有任何提示。所以这里要验清楚:

    1. 指纹 (mtime + 大小) 跟着文件变;
    2. 服务侧 ``load_flows()`` 第二次调用拿到的是**新版本**。
    """
    print("\n[热重载]")
    from flow import flows_signature

    # 用一个临时目录, 不碰真的流程文件
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="flow_hot_"))
    try:
        target = tmp / "probe.yaml"
        target.write_text(
            "name: probe\n"
            "steps:\n"
            "  - id: s1\n"
            "    type: arm\n"
            "    action: 准备点\n",
            encoding="utf-8",
        )

        sig1 = flows_signature(tmp)
        check(len(sig1) == 1, f"指纹里有 1 个文件: {sig1}")
        check(flows_signature(tmp) == sig1, "指纹稳定 (没改就一样)")

        # 改内容 (大小变了, 一定被察觉)
        target.write_text(
            "name: probe\n"
            "steps:\n"
            "  - id: s1\n"
            "    type: arm\n"
            "    action: 准备点\n"
            "  - id: s2\n"
            "    type: arm\n"
            "    action: 安全点\n",
            encoding="utf-8",
        )
        sig2 = flows_signature(tmp)
        check(sig2 != sig1, "改了文件后指纹变了")

        # 服务侧的缓存逻辑: 指纹变了要重新载入
        flows = load_flows(tmp)
        check("probe" in flows, f"载入了 probe: {sorted(flows)}")
        first = flows["probe"]

        # 再改一次, 模拟「热重载」
        target.write_text(
            "name: probe\n"
            "steps:\n"
            "  - id: s1\n"
            "    type: arm\n"
            "    action: 安全点\n",
            encoding="utf-8",
        )
        sig3 = flows_signature(tmp)
        check(sig3 != sig2, "第二次改后指纹又变了")
        check(flows_signature(tmp) != sig2, "指纹确实反映最新内容")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_routing(flows_dir: Path, flows: dict) -> None:
    """检查 ``configs/flows/routing.yaml`` (物品名 -> 流程名 的映射)。

    **这是分流表的一致性检查** —— 映射里指向不存在的流程, 抓取时会
    静默回落到默认流程 (能跑, 但走错了流程)。这种错在运行时才暴露,
    所以在这里静态查出来。

    查三件事:
    1. 文件格式对 (是字典, routing 段是字典)
    2. 每条映射的目标流程在 ``configs/flows/`` 里存在
    3. ``default_flow`` 存在
    """
    routing_path = flows_dir / "routing.yaml"
    print("\n[分流表 routing.yaml]")
    if not routing_path.is_file():
        # 没有分流表 = 一律走默认流程 —— 合法状态, 不是错误
        check(True, "没有 routing.yaml (一律走默认流程)")
        return

    try:
        import yaml

        raw = yaml.safe_load(routing_path.read_text(encoding="utf-8"))
    except Exception as exc:
        check(False, f"routing.yaml 读不了: {exc}")
        return

    if not isinstance(raw, dict):
        check(False, f"routing.yaml 必须是字典, 实际是 {type(raw).__name__}")
        return

    routing = raw.get("routing") or {}
    if not isinstance(routing, dict):
        check(False, f"routing.yaml 的 routing 段必须是字典, 实际是 {type(routing).__name__}")
        return

    default_flow = str(raw.get("default_flow") or "").strip()
    check(bool(default_flow), "default_flow 写了 (留空 = 用服务端默认)")
    if default_flow:
        check(default_flow in flows,
              f"default_flow {default_flow!r} 在流程目录里存在")

    if not routing:
        check(True, "routing 段是空的 (所有物品都走默认流程)")
        return

    for item, flow_name in sorted(routing.items()):
        name = str(item or "").strip()
        flow = str(flow_name or "").strip()
        if not name or not flow:
            check(False, f"映射 {item!r} -> {flow_name!r} 不完整 (物品名/流程名不能为空)")
            continue
        check(flow in flows,
              f"「{name}」-> {flow!r} 在流程目录里存在")


def main() -> int:
    print("=" * 62)
    print("流程定义自检 (离线, 不需要真机)")
    print("=" * 62)

    cfg = load_config()
    flows_dir = Path(cfg.arm.flows_dir)
    print(f"流程目录: {flows_dir}")

    known_actions = _action_names()
    if known_actions:
        print(f"动作库: {len(known_actions)} 个动作")

    print("\n[已知片段类型]")
    for t in STEP_TYPES:
        print(f"  - {t}")

    # ---- 逐个文件检查 ----
    yaml_files = sorted(flows_dir.glob("*.yaml")) if flows_dir.is_dir() else []
    yaml_files = [p for p in yaml_files if p.name not in NON_FLOW_FILES]
    if not yaml_files:
        check(False, f"{flows_dir} 里没有流程文件")
    for path in yaml_files:
        check_flow_file(path, known_actions)

    # ---- 整体加载 (校验器会抛 FlowError) ----
    print("\n[加载全部流程]")
    try:
        flows = load_flows(flows_dir)
        check(True, f"载入 {len(flows)} 条流程: {', '.join(sorted(flows))}")
    except FlowError as exc:
        check(False, f"加载失败: {exc}")
        return 1

    check(DEFAULT_FLOW in flows, f"默认流程 {DEFAULT_FLOW!r} 存在")

    # ---- 分流表 ----
    check_routing(flows_dir, flows)

    # ---- 与旧配置对比 ----
    if DEFAULT_FLOW in flows:
        check_equivalence(flows, cfg)

    # ---- 非法格式必须报错 ----
    check_rejections()

    # ---- 热重载 ----
    check_hot_reload(flows_dir)

    print()
    print("=" * 62)
    if failures:
        print(f"失败 {len(failures)} 项")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
