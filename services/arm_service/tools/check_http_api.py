#!/usr/bin/env python3
"""arm_service 的 HTTP 接口冒烟测试 (不起真服务, 用 TestClient)。

**为什么要有这个**

`/v1/kinematics?refresh=true` 曾因为 `await run_blocking(...)` (同步函数)
直接报 500:

    TypeError: object dict can't be used in 'await' expression

而这个错误**只在真机连上控制器时才暴露** —— 因为 `refresh=true` 会走
「主动拉一次状态」的分支。冒烟测试能在没有控制器的情况下发现这类
「await 同步函数」的低级错误。

覆盖:

- 每个 GET 接口都能返回 200 (不是 500)
- ``/v1/kinematics?refresh=true`` 不抛 ``TypeError``
- 响应能被 ``KinematicsResponse`` 模型校验通过
- **静态扫描**: 所有 ``async def`` 里没有 ``await`` 同步函数

用法::

    conda run -n botagent python tools/check_http_api.py
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path
from typing import Any, List, Tuple

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

_results: List[Tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    _results.append((name, condition, detail))
    tag = "\033[32mPASS\033[0m" if condition else "\033[31mFAIL\033[0m"
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail else ""))
    return condition


# ------------------------------------------------------------ 静态扫描


def _is_async_fn(node: ast.AST) -> bool:
    return isinstance(node, ast.AsyncFunctionDef)


def _called_name(node: ast.AST) -> str:
    """取 ``await foo(...)`` / ``await foo.bar(...)`` 里的名字。"""
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
    return ""


def scan_await_sync() -> List[str]:
    """扫出「在 async def 里 await 一个同步函数」的可疑写法。

    判定依据: 被 await 的名字和**已知同步函数名**同名。

    已知同步函数名来自三个地方:
    1. 本文件里定义的同步函数
    2. ``service.py`` 里定义的同步函数 (跨模块调用最容易踩坑 ——
       ``run_blocking`` 是同步的, 在 ``async def`` 里 await 它会报
       "object dict can't be used in 'await' expression")
    3. 内置的同步阻塞函数
    """
    # 跨模块的已知同步函数
    known_sync: set = set()
    try:
        import service as _service

        for _name in dir(_service):
            _obj = getattr(_service, _name)
            if inspect.isfunction(_obj) and not inspect.iscoroutinefunction(_obj):
                known_sync.add(_name)
    except Exception:
        pass

    problems: List[str] = []
    for path in SERVICE_DIR.rglob("*.py"):
        if "vosk" in str(path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue

        # 本文件里定义的同步函数名
        sync_fns = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and not _is_async_fn(node)
        }
        sync_fns |= known_sync

        for node in ast.walk(tree):
            if not _is_async_fn(node):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Await):
                    continue
                name = _called_name(sub.value)
                if name and name in sync_fns:
                    problems.append(
                        f"{path.relative_to(SERVICE_DIR)}:{sub.lineno} "
                        f"await {name}(...) —— {name} 是同步函数"
                    )
    return problems


# ------------------------------------------------------------ HTTP 冒烟


def http_smoke() -> None:
    """用 TestClient 打一遍所有 GET 接口。"""
    try:
        from fastapi.testclient import TestClient
    except Exception as exc:
        check("能导入 TestClient", False, str(exc))
        return

    import main as arm_main

    app = arm_main.app
    # 控制器连不上也没关系 —— 服务是降级模式
    try:
        with TestClient(app) as client:
            _smoke_gets(client)
    except Exception as exc:
        check("TestClient 能起来", False, f"{type(exc).__name__}: {exc}")


def _smoke_gets(client: Any) -> None:
    gets = [
        ("/health", None),
        ("/v1/actions", None),
        ("/v1/status", None),
        ("/v1/safety", None),
        ("/v1/kinematics", None),
        ("/v1/kinematics?refresh=true", None),
        ("/v1/flows", None),
        ("/v1/flows/grasp", None),
        ("/v1/hand/status", None),
    ]
    for url, _ in gets:
        try:
            resp = client.get(url)
        except Exception as exc:
            check(f"GET {url}", False, f"抛异常: {type(exc).__name__}: {exc}")
            continue
        ok = resp.status_code == 200
        detail = f"HTTP {resp.status_code}"
        if not ok:
            detail += f"  {resp.text[:160]}"
        check(f"GET {url}", ok, detail)

    # refresh=true 必须能返回, 且响应体符合模型
    try:
        resp = client.get("/v1/kinematics?refresh=true")
        if resp.status_code == 200:
            from models import KinematicsResponse

            data = resp.json()
            KinematicsResponse(**data)
            check("refresh=true 的响应能通过模型校验", True)
            check(
                "refresh=true 返回了 arms 字段",
                isinstance(data.get("arms"), dict),
            )
        else:
            check("refresh=true 的响应能通过模型校验", False, f"HTTP {resp.status_code}")
    except Exception as exc:
        check("refresh=true 的响应能通过模型校验", False, f"{type(exc).__name__}: {exc}")

    _smoke_posts(client)


def _smoke_posts(client: Any) -> None:
    """POST 接口冒烟。

    **期望的不是 200** —— 控制器连不上时这些接口应该返回 409 (冲突),
    而不是 500 (服务器错误)。500 说明代码本身有 bug。
    """
    posts = [
        ("/v1/sequence", {"actions": ["准备点", "闭合夹爪", "安全点"]}),
        ("/v1/grasp_sequence", {"x": 0.5, "y": 0.0, "z": -0.3, "side": "right"}),
        ("/v1/hand/connect", {}),
        ("/v1/hand/open", {}),
        ("/v1/hand/close", {}),
        ("/v1/hand/preset", {"preset": "一把抓"}),
    ]
    for url, payload in posts:
        try:
            resp = client.post(url, json=payload)
        except Exception as exc:
            check(f"POST {url}", False, f"抛异常: {type(exc).__name__}: {exc}")
            continue
        # 200 (成功) 或 409 (控制器不可达/冲突) 都算正常
        ok = resp.status_code in (200, 409)
        detail = f"HTTP {resp.status_code}"
        if not ok:
            detail += f"  {resp.text[:160]}"
        check(f"POST {url}", ok, detail)

    # 参数缺失应该 400, 不是 500
    try:
        resp = client.post("/v1/sequence", json={"actions": []})
        check(
            "POST /v1/sequence 没指定动作 -> 400",
            resp.status_code == 400,
            f"HTTP {resp.status_code}",
        )
    except Exception as exc:
        check("POST /v1/sequence 没指定动作 -> 400", False, str(exc))

    try:
        resp = client.post("/v1/hand/preset", json={})
        check(
            "POST /v1/hand/preset 没指定预设 -> 400",
            resp.status_code == 400,
            f"HTTP {resp.status_code}",
        )
    except Exception as exc:
        check("POST /v1/hand/preset 没指定预设 -> 400", False, str(exc))


def main() -> int:
    print("=" * 62)
    print("arm_service HTTP 接口冒烟测试")
    print("=" * 62)

    print("\n[1] 静态扫描: async def 里不该 await 同步函数")
    problems = scan_await_sync()
    check(
        "没有 await 同步函数的写法",
        not problems,
        "; ".join(problems) if problems else "",
    )

    print("\n[2] HTTP 接口冒烟")
    http_smoke()

    passed = sum(1 for _, ok, _ in _results if ok)
    failed = len(_results) - passed
    print()
    print("=" * 62)
    print(f"结果: {passed} 通过, {failed} 失败")
    print("=" * 62)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
