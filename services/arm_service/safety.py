"""安全门阀: 动作执行前的状态检查。

**为什么要门阀**: 机械臂出问题时 (急停、安全锁定、伺服故障、没使能、
控制器连不上), 如果直接把轨迹发下去, 表现是「下发成功但机械臂不动」
或者「动作做到一半卡住」—— 用户和现场都无从判断到底出了什么事。

门阀在**入控之前**逐项检查, 有任何一项不通过就拒绝动作, 并把原因
用 TTS 播报出来, 让用户知道「为什么不动」而不是「静默失败」。

检查项 (对应控制器的 `robot/status` 字段):

===================  ==================================  ==========
检查项                字段                                 不通过含义
===================  ==================================  ==========
控制器连接           (连接状态)                           控制器不可达
安全锁定             ``faulted``                          有未清除的故障
保护主码             ``faultCode``                        当前告警码
急停                ``emergencyStopActive``              急停按下
安全响应             ``safetyResponse``                   安全响应已触发
安全相位             ``safetyPhase``                      正在减速/快停
使能状态             ``enabled``                          机械臂没上使能
运行模式             ``mode``                             模式不对 (如仿真)
===================  ==================================  ==========

**为什么用 `robot/status` 而不是状态推送**: 推送是异步的, 且只在
``tc`` 周期内到达; 门阀要在**当下**拿到一份确定的快照, 所以走
请求-响应 (``robot/status``), 拿到应答才继续。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("arm.safety")


class SafetyError(RuntimeError):
    """安全门阀拒绝放行。

    ``reason`` 是给用户听的短句 (会被 TTS 播报);
    ``code`` 是机器可读的判定项, 便于排查。
    """

    def __init__(self, reason: str, code: str = "unknown", detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.detail = detail


@dataclass
class SafetyCheck:
    """单项检查结果。"""

    #: 判定项标识 (如 "faulted")
    code: str
    #: 通过与否
    ok: bool
    #: 不通过时的中文说明 (给用户听)
    message: str = ""
    #: 原始值 / 细节 (给工程师看)
    detail: str = ""


@dataclass
class SafetyReport:
    """一次门阀检查的完整结果。"""

    ok: bool = True
    checks: List[SafetyCheck] = field(default_factory=list)
    #: 不通过项的简短原因 (给 TTS 播报)
    reason: str = ""
    #: 机器可读的问题代码
    code: str = ""
    #: 检查耗时 (毫秒)
    elapsed_ms: int = 0

    @property
    def failed_checks(self) -> List[SafetyCheck]:
        return [c for c in self.checks if not c.ok]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "reason": self.reason,
            "elapsed_ms": self.elapsed_ms,
            "checks": [
                {
                    "code": c.code,
                    "ok": c.ok,
                    "message": c.message,
                    "detail": c.detail,
                }
                for c in self.checks
            ],
        }


class SafetyGate:
    """动作执行前的安全门阀。

    门阀只做「判定」, 不自己发控制命令 —— 状态查询由调用方 (service)
    提供, 这样门阀可以脱离控制器做单元测试。
    """

    #: 安全响应名 -> 给用户听的一句话
    RESPONSE_MESSAGES: Dict[str, str] = {
        "immediateDisable": "机械臂刚被安全保护停掉了",
        "servoQuickStop": "机械臂刚被安全保护急停了",
        "pathStopThenDisable": "机械臂刚被安全保护停掉了",
        "pathStop": "机械臂刚被安全保护减速停住了",
        "zeroSpeedHold": "机械臂刚被安全保护锁在原地",
    }

    #: 允许动作的运行模式。
    #:
    #: 只有 ``CSPPassthrough`` (位置控制模式) 能接受实时位置流 —— 这是日常
    #: 运行时机械臂所处的模式 (``robot/enable`` 之后)。
    #: 其它模式都不该动:
    #: - ``Disabled``      下使能, 机械臂不受控
    #: - ``CSPRescue``     救援模式, 只做重力保持
    #: - ``CSTDrag`` / ``ServoDrag``  拖动模式, 人手扶着示教
    #: - ``CSVZeroSpeed``  零速保持, 指令速度恒为零
    ALLOWED_MODES = frozenset({"CSPPassthrough"})

    def __init__(
        self,
        require_enabled: bool = True,
        require_connection: bool = True,
        allowed_modes: Optional[frozenset] = None,
        check_fault: bool = True,
    ) -> None:
        self.require_enabled = require_enabled
        self.require_connection = require_connection
        self.allowed_modes = allowed_modes or self.ALLOWED_MODES
        self.check_fault = check_fault

    # ------------------------------------------------------------ 判定

    def check(self, status: Optional[Dict[str, Any]], connected: bool = True) -> SafetyReport:
        """对一份 `robot/status` 快照做检查。

        ``status`` 为 None 表示没拿到状态 —— 视为不通过 (不能盲动)。
        """
        started = time.time()
        report = SafetyReport()
        checks: List[SafetyCheck] = []

        # 1) 控制器连接
        if self.require_connection:
            checks.append(
                SafetyCheck(
                    code="connected",
                    ok=bool(connected),
                    message="机械臂控制器连不上",
                    detail="WebSocket 未连接",
                )
            )

        # 2) 有没有拿到状态快照
        if not isinstance(status, dict):
            checks.append(
                SafetyCheck(
                    code="status",
                    ok=False,
                    message="读不到机械臂的状态",
                    detail="robot/status 没有应答",
                )
            )
            report.checks = checks
            report.ok = False
            report.code = "status"
            report.reason = "读不到机械臂的状态"
            report.elapsed_ms = int((time.time() - started) * 1000)
            return report

        checks.append(SafetyCheck(code="status", ok=True, message="", detail="已读取状态"))

        # 3) 安全锁定 (faulted)
        if self.check_fault:
            faulted = bool(status.get("faulted"))
            fault_code = status.get("faultCode") or 0
            checks.append(
                SafetyCheck(
                    code="faulted",
                    ok=not faulted,
                    message="机械臂处于安全锁定状态, 有故障没清除"
                    if faulted
                    else "",
                    detail=f"faulted={faulted} faultCode={fault_code}",
                )
            )

        # 4) 急停
        estop = bool(status.get("emergencyStopActive"))
        checks.append(
            SafetyCheck(
                code="emergency_stop",
                ok=not estop,
                message="机械臂的急停还按着" if estop else "",
                detail=f"emergencyStopActive={estop}",
            )
        )

        # 5) 安全响应
        response = str(status.get("safetyResponse") or "none")
        if response != "none":
            message = self.RESPONSE_MESSAGES.get(
                response, f"机械臂触发了安全保护 ({response})"
            )
        else:
            message = ""
        checks.append(
            SafetyCheck(
                code="safety_response",
                ok=response == "none",
                message=message,
                detail=f"safetyResponse={response} "
                f"code={status.get('safetyResponseCode')}",
            )
        )

        # 6) 安全相位 (正在减速/快停 —— 即使 faulted 为 false 也不能动)
        phase = str(status.get("safetyPhase") or "none")
        checks.append(
            SafetyCheck(
                code="safety_phase",
                ok=phase == "none",
                message="机械臂还在安全减速中" if phase != "none" else "",
                detail=f"safetyPhase={phase} code={status.get('safetyPhaseCode')}",
            )
        )

        # 7) 使能状态
        if self.require_enabled:
            enabled = bool(status.get("enabled"))
            checks.append(
                SafetyCheck(
                    code="enabled",
                    ok=enabled,
                    message="机械臂还没上使能" if not enabled else "",
                    detail=f"enabled={enabled}",
                )
            )

        # 8) 运行模式
        mode = str(status.get("mode") or "")
        mode_ok = mode in self.allowed_modes
        checks.append(
            SafetyCheck(
                code="mode",
                ok=mode_ok,
                message=self._mode_message(mode) if not mode_ok else "",
                detail=f"mode={mode} channel={status.get('channel')}",
            )
        )

        report.checks = checks
        failed = [c for c in checks if not c.ok]
        report.ok = not failed
        if failed:
            # 播报用第一条不通过项 —— 它是最「根本」的那个原因
            report.reason = failed[0].message
            report.code = failed[0].code
        report.elapsed_ms = int((time.time() - started) * 1000)
        return report

    # ------------------------------------------------------------ 播报文案

    #: 非位置控制模式的说明 —— 给用户听
    MODE_MESSAGES: Dict[str, str] = {
        "Disabled": "机械臂没上电",
        "CSPRescue": "机械臂在救援模式",
        "CSTDrag": "机械臂在拖动模式",
        "ServoDrag": "机械臂在拖动模式",
        "CSVZeroSpeed": "机械臂在零速保护中",
    }

    def _mode_message(self, mode: str) -> str:
        """模式不对时的说明。"""
        return self.MODE_MESSAGES.get(
            mode, f"机械臂当前不在位置控制模式 ({mode})"
        )

    def speak_text(self, report: SafetyReport) -> str:
        """把检查结果转成一句给用户听的播报文案。"""
        if report.ok:
            return ""
        reason = report.reason or "机械臂现在不能动"
        return f"{reason}，请先处理一下"
