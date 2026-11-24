"""机械臂动作服务客户端。

Agent 的 device 分支通过它把「动作名」变成「机械臂真的动起来」。

动作名的对应关系: arm_service 的动作库是「一个 JSON 文件 = 一个动作」,
**文件名就是动作名** (挥手.json -> "挥手")。所以这里下发的就是动作名本身。

接口:
- GET  /v1/actions  取动作清单 (启动时缓存, 用于校验动作名是否存在)
- POST /v1/play     下发播放 (异步, 立即返回)
- POST /v1/stop     停止播放
- GET  /v1/status   播放状态

只依赖 httpx, 与 llm.py 用同一套代理清洗逻辑。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("agent.arm")

#: httpx 不认识的代理协议 (如 socks://) —— 出现这类环境变量时 httpx 会直接抛
#: ValueError, 导致整个请求 500, 所以这里先把它们摘掉。
_BAD_PROXY_SCHEMES = ("socks://", "socks4://", "socks5://", "socks4a://", "socks5h://")
_PROXY_ENV_KEYS = (
    "all_proxy", "ALL_PROXY",
    "http_proxy", "HTTP_PROXY",
    "https_proxy", "HTTPS_PROXY",
    "socks_proxy", "SOCKS_PROXY",
    "socks5_proxy", "SOCKS5_PROXY",
)


def _sanitize_proxy_env() -> None:
    for key in _PROXY_ENV_KEYS:
        value = os.environ.get(key)
        if value and value.lower().startswith(_BAD_PROXY_SCHEMES):
            os.environ.pop(key, None)


class ArmError(RuntimeError):
    """调用机械臂服务失败。"""


class ArmClient:
    """机械臂动作服务客户端。"""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8802",
        timeout: float = 5.0,
    ) -> None:
        _sanitize_proxy_env()
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)
        #: 动作名清单 (启动时拉一次); 为空表示还没拉过, 此时不做名称校验
        self._actions: List[str] = []
        self._actions_loaded_at = 0.0

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------ 动作清单

    async def load_actions(self) -> List[str]:
        """拉取动作清单并缓存。失败时返回空列表 (不阻塞服务启动)。"""
        try:
            resp = await self._client.get("/v1/actions")
            resp.raise_for_status()
            body = resp.json()
            actions = [str(x) for x in (body.get("labels") or [])]
            self._actions = actions
            self._actions_loaded_at = time.time()
            logger.info("[机械臂] 已加载 %d 个动作: %s", len(actions), ", ".join(actions))
            return actions
        except Exception as exc:
            logger.warning("[机械臂] 动作清单拉取失败 (%s): %s", self.base_url, exc)
            return []

    @property
    def actions(self) -> List[str]:
        return list(self._actions)

    def has_action(self, name: str) -> bool:
        """动作名是否存在。没加载过清单时一律当作存在 (交给服务端判定)。"""
        if not self._actions:
            return True
        return name in self._actions

    # ------------------------------------------------------------ 播放

    async def play(
        self,
        action: str,
        speed: float = 1.0,
        loop: bool = False,
        replace: bool = True,
    ) -> Dict[str, Any]:
        """下发播放一个动作。返回服务端应答。"""
        return await self.play_many(
            [action], speed=speed, loop=loop, replace=replace
        )

    async def play_and_wait(
        self,
        action: str,
        timeout: float = 30.0,
        replace: bool = True,
    ) -> Dict[str, Any]:
        """播放一个动作并**等它播完**。

        抓取流程要按顺序走 (回准备点 -> 拍照 -> 移到上方点 -> ...) ——
        下一步必须等上一步真的动完了才能开始, 否则「拍照」会在手臂还没
        到准备点时就拍了。

        服务端 ``wait=true`` 会阻塞到播完, 所以这里不用轮询
        ``/v1/status`` —— 轮询既慢又容易在边界上判错。

        Returns:
            服务端应答 + ``finished`` (bool) 与 ``error`` (str)
        """
        payload = {"action": action, "wait": True}
        try:
            resp = await self._client.post(
                "/v1/play", json=payload, timeout=timeout
            )
        except Exception as exc:
            raise ArmError(f"机械臂服务不可达 ({self.base_url}): {exc}") from exc

        if resp.status_code >= 400:
            detail = ""
            try:
                detail = resp.json().get("detail", "")
            except Exception:
                detail = resp.text[:200]
            raise ArmError(f"下发播放失败 (HTTP {resp.status_code}): {detail}")

        body = resp.json()
        if not body.get("ok", False):
            raise ArmError(f"机械臂拒绝执行: {body.get('message') or body}")
        return body

    async def play_many(
        self,
        actions: List[str],
        speed: float = 1.0,
        loop: bool = False,
        replace: bool = True,
        wait: bool = False,
    ) -> Dict[str, Any]:
        """下发播放一个动作序列 (按顺序)。返回服务端应答。

        arm_service 的 /v1/play 是**异步**的: 立即返回 ok, 实际播放在后台线程,
        所以这里不代表机械臂已经动起来了 —— 真正的结果要查 /v1/status。

        整个序列**只入控一次** —— 动作之间不松手, 机械臂运动连贯。

        ``replace=True`` 时, 若已有动作在播 (HTTP 409), 先停掉再重新下发 ——
        机器人收到新指令时应该立刻改做新的动作, 而不是报「忙」。
        """
        names = [str(a).strip() for a in (actions or []) if str(a).strip()]
        if not names:
            raise ArmError("没指定要执行的动作")

        payload = {
            "actions": names,
            "speed": speed,
            "loop": loop,
        }
        if wait:
            payload["wait"] = True
        try:
            resp = await self._client.post("/v1/play", json=payload)
        except Exception as exc:
            raise ArmError(f"机械臂服务不可达 ({self.base_url}): {exc}") from exc

        if resp.status_code == 409 and replace:
            # 已有动作在播: 停掉再试一次
            #
            # arm_service 的 stop() 会等播放线程真正退出才返回, 所以这里
            # 紧接着重发就能成功。保险起见多等一次 —— 万一线程还没退干净,
            # 再停一次 (stop 是幂等的)。
            logger.info("[机械臂] 已有动作在播, 先停止再执行 %s", names)
            for attempt in range(2):
                try:
                    await self.stop()
                except ArmError as exc:
                    logger.warning("[机械臂] 停止失败: %s", exc)
                try:
                    resp = await self._client.post("/v1/play", json=payload)
                except Exception as exc:
                    raise ArmError(
                        f"机械臂服务不可达 ({self.base_url}): {exc}"
                    ) from exc
                if resp.status_code != 409:
                    break
                if attempt == 0:
                    # 第一次重发还是 409 -> 再停一次 (线程可能还没退干净)
                    time.sleep(0.2)

        if resp.status_code >= 400:
            detail = ""
            try:
                detail = resp.json().get("detail", "")
            except Exception:
                detail = resp.text[:200]
            raise ArmError(f"下发播放失败 (HTTP {resp.status_code}): {detail}")

        body = resp.json()
        if not body.get("ok", False):
            raise ArmError(f"机械臂拒绝执行: {body.get('message') or body}")
        return body

    async def stop(self) -> Dict[str, Any]:
        try:
            resp = await self._client.post("/v1/stop")
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            raise ArmError(f"停止失败: {exc}") from exc

    async def status(self) -> Dict[str, Any]:
        try:
            resp = await self._client.get("/v1/status")
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            raise ArmError(f"状态查询失败: {exc}") from exc

    async def kinematics(self, refresh: bool = True) -> Dict[str, Any]:
        """运动学快照: 各臂 TCP 与基座在世界系下的位姿。"""
        try:
            resp = await self._client.get(
                "/v1/kinematics", params={"refresh": "true" if refresh else "false"},
                timeout=6.0,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            raise ArmError(f"运动学查询失败: {exc}") from exc

    async def move_to(
        self,
        arm: str,
        x: float,
        y: float,
        z: float,
        duration: Optional[float] = None,
        a: Optional[float] = None,
        b: Optional[float] = None,
        c: Optional[float] = None,
    ) -> Dict[str, Any]:
        """把 TCP 移到世界系目标点。**同步等到走完**。

        被门阀或可达性检查拒绝时不抛异常, 返回 ``ok=false`` 和可播报的
        ``message``; 只有服务不可达这类传输错误才抛 ArmError。
        """
        # speak=False: 失败原因由 agent 回复念出来, 服务端不再重复播报
        payload: Dict[str, Any] = {"arm": arm, "x": x, "y": y, "z": z, "speak": False}
        for key, value in (("duration", duration), ("a", a), ("b", b), ("c", c)):
            if value is not None:
                payload[key] = value
        # 服务端会阻塞到走完, 超时要覆盖 运动时长 + 入控 + 核对
        timeout = float(duration or 3.0) + 20.0
        try:
            resp = await self._client.post("/v1/move_to", json=payload, timeout=timeout)
        except Exception as exc:
            raise ArmError(f"机械臂服务不可达 ({self.base_url}): {exc}") from exc
        if resp.status_code >= 400:
            raise ArmError(f"移动请求失败 (HTTP {resp.status_code}): {resp.text[:200]}")
        return resp.json()

    async def flow_config(self) -> Dict[str, Any]:
        """抓取流程 (5 步) 的配置。

        来自机械臂服务的 ``configs/arm.yaml`` —— agent 那边不自己读
        yaml, 两处配置不同步很麻烦。
        """
        try:
            resp = await self._client.get("/v1/flow", timeout=6.0)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            raise ArmError(f"查询抓取流程配置失败: {exc}") from exc

    async def check_safety(self) -> Dict[str, Any]:
        """跑一次安全门阀检查 (不执行动作)。

        用于**在下发前**先探一下: 机械臂有急停/故障/未使能时, 与其先说
        「好的」再让它不动, 不如直接告诉用户「为什么现在不能动」。
        """
        try:
            resp = await self._client.get("/v1/safety", timeout=6.0)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("[机械臂] 安全门阀查询失败: %s", exc)
            # 查不到不等于有故障 —— 放行, 让 arm_service 自己在入控前把关
            return {"ok": True, "code": "unknown", "reason": ""}

    async def health(self) -> bool:
        try:
            resp = await self._client.get("/health", timeout=2.0)
            return resp.status_code == 200
        except Exception:
            return False

    async def get_control_enabled(self) -> Dict[str, Any]:
        """看机械臂的控制指令总闸状态。

        ``{"enabled": false}`` = 只算不动, 不下发任何控制指令。
        """
        try:
            resp = await self._client.get("/v1/control_enabled", timeout=6.0)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            raise ArmError(f"查询控制总闸失败: {exc}") from exc

    async def set_control_enabled(self, enabled: Optional[bool]) -> Dict[str, Any]:
        """切换机械臂的控制指令总闸。

        ``enabled=False`` -> 只算不动; ``None`` -> 回到配置默认值。
        """
        try:
            resp = await self._client.post(
                "/v1/control_enabled", json={"enabled": enabled}, timeout=6.0
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            raise ArmError(f"设置控制总闸失败: {exc}") from exc
