#!/usr/bin/env python3
"""语音 -> 机械臂 完整链路自检。

模拟一次真实的语音交互, 逐段验证:
    ASR (模拟识别) -> Agent 分流 -> 机械臂动作 + TTS 播报

各服务都必须已启动:
    scripts/start_all.sh   (或分别启动 asr / agent / arm / tts)

用法::

    python tools/check_voice_arm.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
AGENT_URL = "http://127.0.0.1:8800"
ARM_URL = "http://127.0.0.1:8802"
ASR_URL = "http://127.0.0.1:8801"
TTS_URL = "http://127.0.0.1:8000"

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global FAILED
    if ok:
        print(f"  [{PASS}] {name}" + (f"  ({detail})" if detail else ""))
    else:
        print(f"  [{FAIL}] {name}" + (f"  ({detail})" if detail else ""))
        FAILED = 1
    return ok


def _get(url: str, timeout: float = 8.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


def _post(url: str, payload: dict, timeout: float = 25.0):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def test_services() -> None:
    print("\n[1] 服务状态")
    for name, url, key in [
        ("ASR", f"{ASR_URL}/state", "state"),
        ("Agent", f"{AGENT_URL}/health", "status"),
        ("机械臂", f"{ARM_URL}/health", "status"),
        ("TTS", f"{TTS_URL}/health", "status"),
    ]:
        try:
            body = _get(url)
            check(f"{name} 可用", body.get(key) is not None, str(body.get(key)))
        except Exception as exc:
            check(f"{name} 可用", False, str(exc))


def load_action_cases() -> list[tuple[str, str]]:
    """从 configs/actions.yaml 动态生成测试用例。

    动作数据改了, 这个用例表自动跟着变 —— 不写死动作名。
    """
    import yaml

    path = ROOT / "configs" / "actions.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    arm = data.get("arm") or {}

    cases: list[tuple[str, str]] = []
    for item in arm.get("actions") or []:
        name = str(item.get("action", "") or "").strip()
        phrases = [str(x) for x in (item.get("say") or [])]
        if name and phrases:
            # 取最后一个说法 (最泛的那个), 更容易命中
            cases.append((phrases[-1], name))
    return cases


def test_voice_to_arm() -> None:
    """模拟识别出一句话, 验证它变成机械臂动作。"""
    print("\n[2] 语音 -> 机械臂动作")

    cases = load_action_cases()
    if not cases:
        check("能从 configs/actions.yaml 读到动作", False)
        return

    for text, expected in cases:
        try:
            resp = _post(
                f"{AGENT_URL}/v1/dispatch",
                {"session_id": "voice-arm", "text": text, "use_history": False},
            )
        except Exception as exc:
            check(f"「{text}」分发成功", False, str(exc))
            continue

        action = resp.get("action") or {}
        ok = (
            resp.get("intent") == "device"
            and action.get("name") == expected
            and action.get("status") == "ok"
        )
        check(
            f"「{text}」-> {expected}",
            ok,
            f"intent={resp.get('intent')} action={action.get('name')} "
            f"status={action.get('status')}",
        )
        check(f"「{text}」有语音确认", resp.get("speak") is True,
              repr(resp.get("reply")))

        # 等机械臂真的动起来
        time.sleep(1.5)
        try:
            st = _get(f"{ARM_URL}/v1/status")
            check(
                f"「{text}」机械臂在播",
                st.get("phase") in ("entering", "aligning", "playing"),
                f"phase={st.get('phase')}",
            )
        except Exception as exc:
            check(f"「{text}」机械臂在播", False, str(exc))

        # 等播完, 避免下一条撞上「已有动作正在播放」
        deadline = time.time() + 20.0
        while time.time() < deadline:
            try:
                st = _get(f"{ARM_URL}/v1/status")
                if st.get("phase") in ("idle", "error"):
                    break
            except Exception:
                break
            time.sleep(0.3)


def test_chat_untouched() -> None:
    """普通闲聊不能被误判成机械臂指令。"""
    print("\n[3] 闲聊不受影响")
    for text in ["今天天气怎么样", "介绍一下北京", "讲个笑话"]:
        try:
            resp = _post(
                f"{AGENT_URL}/v1/dispatch",
                {"session_id": "voice-arm-chat", "text": text, "use_history": False},
            )
        except Exception as exc:
            check(f"「{text}」正常响应", False, str(exc))
            continue
        intent = resp.get("intent")
        check(
            f"「{text}」不是 device",
            intent != "device",
            f"intent={intent}",
        )


def main() -> int:
    print("=" * 60)
    print("语音 -> 机械臂 完整链路自检")
    print("=" * 60)

    test_services()
    test_voice_to_arm()
    test_chat_untouched()

    print("\n" + "=" * 60)
    if FAILED:
        print("存在失败项")
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
