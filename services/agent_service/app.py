"""Agent 服务入口。

职责:
1. 接收 ASR 识别出的文本
2. 做语义识别与意图分流
3. 交给对应分支处理
4. 统一返回, 由 `speak` 字段告诉调用方 (ASR) 是否要走 TTS

接口:
- POST /v1/dispatch    分流并执行 (ASR 主用)
- POST /v1/chat        只做对话 (调试用)
- POST /v1/chat/stream 流式对话, SSE
- GET  /health         健康检查
- GET  /v1/branches    分支状态
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from config import Config, load_config
from handlers.base import AgentRequest, BranchResult
from llm import LLMClient, LLMError
from registry import Registry
from router import Router
from session import SessionStore
from skill.catalog import SkillCatalog
from skill.loader import load_all_skills
from query_api import router as thinking_api_router
from event_bus import bus, new_turn_id
from events_api import router as events_api_router

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="[%(asctime)s][%(levelname)s][%(name)s] %(message)s",
)
logger = logging.getLogger("agent")


class DispatchRequest(BaseModel):
    session_id: str = Field(default="default", max_length=128)
    text: str = Field(min_length=1, max_length=4000)
    image_path: Optional[str] = None
    use_history: bool = True


class ChatRequest(BaseModel):
    session_id: str = Field(default="default", max_length=128)
    text: str = Field(min_length=1, max_length=4000)
    use_history: bool = True


class DispatchResponse(BaseModel):
    session_id: str
    text: str
    intent: str
    route: List[str] = []
    speak: bool
    reply: str
    action: Optional[Dict[str, Any]] = None
    latency_ms: int = 0
    trace: List[Dict[str, Any]] = []


class Service:
    """把各组件组装起来。"""

    def __init__(self) -> None:
        self.cfg: Config = load_config()
        self.llm = LLMClient(self.cfg.llm)
        self.registry = Registry()
        self.registry.load(self.cfg, self.llm)
        
        # Phase 1-7：加载 Skill 目录
        #
        # configs/agent.yaml 的 skills.disabled 里列出的 skill 不注册 ——
        # 模型看不见, 路由也找不到, 等于整个功能下线。
        self.skill_catalog = SkillCatalog()
        load_all_skills(
            self.skill_catalog, self.llm, disabled=self.cfg.disabled_skills,
        )
        
        self.router = Router(self.cfg, self.registry, self.llm, self.skill_catalog)
        self.sessions = SessionStore(
            max_turns=self.cfg.session.max_turns,
            ttl_seconds=self.cfg.session.ttl_seconds,
        )
        logger.info(
            "Agent 服务就绪: provider=%s model=%s branches=%s router=%s skills=%s",
            self.cfg.llm.default_provider,
            self.cfg.llm.model,
            self.registry.names(),
            self.cfg.router.mode,
            len(self.skill_catalog.get_all_specs()),
        )
        logger.info(
            "LLM 角色绑定 (实际生效): %s",
            {
                name: f"{rc.provider}/{rc.model or '-'}"
                for name, rc in self.cfg.llm.roles.items()
            },
        )

    def _history(self, session_id: str, use_history: bool) -> List[Dict[str, Any]]:
        return self.sessions.history(session_id) if use_history else []

    async def dispatch(self, req: DispatchRequest) -> DispatchResponse:
        started = time.time()

        # ---- 本轮的唯一 id ----
        #
        # session_id 是常驻的 (robot-01), 不能用来分轮; turn_id 每轮换一个。
        # 显式传给下面所有 publish, 监控端据此把事件归到同一轮。
        turn_id = new_turn_id()

        areq = AgentRequest(
            text=req.text,
            session_id=req.session_id,
            history=self._history(req.session_id, req.use_history),
            image_path=req.image_path,
            turn_id=turn_id,
        )

        # ---- 监控埋点: 本轮开始 ----
        bus.publish(
            "turn_start",
            {
                "text": req.text,
                "history_len": len(areq.history),
                "image_path": req.image_path,
            },
            session_id=req.session_id,
            turn_id=turn_id,
        )

        t_route = time.time()
        decision = await self.router.decide(areq)
        route_ms = int((time.time() - t_route) * 1000)
        intent = decision.intent
        trace = list(decision.trace)

        handler = self.registry.get(intent)
        t_handler = time.time()
        if decision.tool is not None:
            # tool calling 选中了工具 -> 走统一动作层执行
            # (机械臂走安全门阀, 设备工具走现有实现)
            result = await self._execute_tool(decision)
        elif handler is None:
            result = BranchResult(
                reply="暂时没有可用的处理分支。",
                speak=True,
                trace=[{"step": "dispatch", "ms": 0.0, "detail": "no-handler"}],
            )
        elif decision.by == "tools" and decision.content:
            # tool calling 没选中工具, 模型的 content 就是回复 ——
            # 直接用, 不必再调一次 LLM (省一次往返)
            result = BranchResult(
                reply=decision.content,
                speak=True,
                action=None,
                trace=[
                    {
                        "step": "tools-reply",
                        "ms": 0.0,
                        "detail": f"{len(decision.content)}chars",
                    }
                ],
            )
        else:
            try:
                result = await handler.handle(areq)
            except Exception as exc:  # pragma: no cover - 分支异常兜底
                logger.exception("分支 [%s] 执行失败", intent)
                result = BranchResult(
                    reply="处理出错了，请稍后再试。",
                    speak=True,
                    trace=[{"step": "error", "ms": 0.0, "detail": str(exc)}],
                )
            if result is None:
                result = BranchResult(
                    reply="处理出错了，请稍后再试。",
                    speak=True,
                    trace=[{"step": "error", "ms": 0.0, "detail": "no-result"}],
                )
        handler_ms = int((time.time() - t_handler) * 1000)

        all_trace = list(trace) + list(result.trace or [])
        all_trace.append(
            {"step": "timing", "ms": route_ms, "detail": f"route:{route_ms}ms"}
        )
        all_trace.append(
            {"step": "timing", "ms": handler_ms, "detail": f"handler:{handler_ms}ms"}
        )
        if result.speak and req.use_history:
            self.sessions.append(req.session_id, req.text, result.reply)

        total_ms = int((time.time() - started) * 1000)
        logger.info(
            "[计时] 总计 %dms | 分流 %dms | 分支执行 %dms | intent=%s speak=%s",
            total_ms,
            route_ms,
            handler_ms,
            intent,
            result.speak,
        )

        # 调试模式: 把「收到什么 -> 怎么分流 -> 回了什么」整条链打出来
        if os.environ.get("AGENT_DEBUG", "").strip().lower() in (
            "1", "true", "yes", "on",
        ):
            logger.info(
                "[AGENT-DEBUG] 输入=%r | intent=%s | by=%s | speak=%s\n"
                "[AGENT-DEBUG] trace=%s\n"
                "[AGENT-DEBUG] 回复=%r | action=%s",
                req.text,
                intent,
                getattr(decision, "by", ""),
                result.speak,
                all_trace,
                result.reply or "",
                result.action,
            )

        # ---- 监控埋点: 本轮结束 ----
        #
        # 把这一轮的完整结论推给监控端: 分流怎么判的、走了哪个分支、
        # 回了什么、动作结果、各阶段耗时。trace 是逐条的 step/ms/detail。
        bus.publish(
            "turn_end",
            {
                "text": req.text,
                "intent": intent,
                "route": [intent],
                "by": getattr(decision, "by", ""),
                "speak": bool(result.speak),
                "reply": result.reply or "",
                "action": result.action,
                "latency_ms": total_ms,
                "route_ms": route_ms,
                "handler_ms": handler_ms,
                "trace": all_trace,
            },
            session_id=req.session_id,
            turn_id=turn_id,
        )

        return DispatchResponse(
            session_id=req.session_id,
            text=req.text,
            intent=intent,
            route=[intent],
            speak=bool(result.speak),
            reply=result.reply or "",
            action=result.action,
            latency_ms=total_ms,
            trace=all_trace,
        )

    async def _execute_tool(self, decision: Any) -> BranchResult:
        """执行 tool calling 选中的工具。

        参数先校验再执行 —— 副作用动作不能让模型乱传参数。
        机械臂动作在这里经过 `DeviceHandler.execute()`, 也就是**安全门阀
        照常生效**, 不会因为换了调用方式就绕过去。

        模型可能一次给多个调用 (实测会), 按顺序全部执行, 回复合并成一句。
        """
        started = time.time()
        calls = list(decision.tool_calls or [])
        if not calls and decision.tool is not None:
            # 兜底: 没有 tool_calls 但有 tool (理论上不会发生)
            calls = []

        outcomes = []
        for call in calls:
            spec = self.router.catalog.get(call.name)
            if spec is None:
                continue
            try:
                outcome = await self.router.catalog.execute(spec, call.arguments)
            except Exception as exc:
                logger.warning("工具 %s 执行失败: %s", call.name, exc)
                outcomes.append(
                    (
                        "failed",
                        f"{spec.label}没做成",
                        {
                            "name": spec.name,
                            "args": dict(call.arguments or {}),
                            "status": "failed",
                            "detail": str(exc),
                            "speak": True,
                        },
                    )
                )
                continue
            outcomes.append((outcome, spec))

        if not outcomes:
            return BranchResult(
                reply="这个操作现在做不到，请换个说法试试。",
                speak=True,
                action={"name": "none", "args": {}, "status": "failed", "speak": True},
                trace=[
                    {
                        "step": "tool-exec",
                        "ms": round((time.time() - started) * 1000, 1),
                        "detail": "no-executable-tool",
                    }
                ],
            )

        # 合并回复: 多个动作时用「、」连起来, 保持简短
        replies = []
        speak = False
        payloads = []
        for item in outcomes:
            if isinstance(item[0], str):
                # 失败分支
                status, reply, payload = item
                replies.append(reply)
                speak = True
                payloads.append(payload)
                continue
            outcome, spec = item
            replies.append(outcome.reply)
            speak = speak or outcome.speak
            payloads.append(dict(outcome.payload or {}))

        if len(replies) == 1:
            reply = replies[0]
        else:
            # 去掉每个回复里重复的引导词, 避免「已好的、挥手、已执行关空调」
            parts = []
            for r in replies:
                r = r or ""
                for prefix in ("好的，", "好的,", "已执行 ", "已执行"):
                    if r.startswith(prefix):
                        r = r[len(prefix):]
                        break
                if r:
                    parts.append(r)
            reply = "已" + "、".join(parts) if parts else "已执行"

        return BranchResult(
            reply=reply,
            speak=speak,
            action=payloads[0] if len(payloads) == 1 else {"items": payloads},
            trace=[
                {
                    "step": "tool-exec",
                    "ms": round((time.time() - started) * 1000, 1),
                    "detail": f"{len(outcomes)}tool(s)",
                }
            ],
        )

    async def chat_only(self, req: ChatRequest) -> Dict[str, Any]:
        """跳过路由, 直接走 chat 分支 (调试用)。"""
        handler = self.registry.get("chat")
        if handler is None:
            raise RuntimeError("chat 分支未启用")
        started = time.time()
        areq = AgentRequest(
            text=req.text,
            session_id=req.session_id,
            history=self._history(req.session_id, req.use_history),
        )
        result = await handler.handle(areq)
        if req.use_history:
            self.sessions.append(req.session_id, req.text, result.reply)
        return {
            "session_id": req.session_id,
            "reply": result.reply,
            "speak": True,
            "route": ["chat"],
            "latency_ms": int((time.time() - started) * 1000),
        }


service: Optional[Service] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global service
    service = Service()
    # 把事件循环绑到事件总线上 —— 跨线程 publish 时靠它投递
    try:
        bus.bind_loop(asyncio.get_running_loop())
    except RuntimeError:  # pragma: no cover
        pass
    try:
        yield
    finally:
        bus.unbind_loop()
        if service is not None:
            await service.llm.aclose()
            # 各个分支可能持有 HTTP 客户端 (视觉/机械臂), 一起关掉
            for handler in list(service.registry.all().values()):
                closer = getattr(handler, "aclose", None)
                if closer is not None:
                    try:
                        await closer()
                    except Exception:  # pragma: no cover
                        pass


app = FastAPI(title="Bot Agent Service", version="1.0.0", lifespan=lifespan)

# 注册思考过程查询 API
app.include_router(thinking_api_router)
# 注册实时事件推送 API (只读, 监控端用)
app.include_router(events_api_router)


def _svc() -> Service:
    if service is None:
        raise RuntimeError("服务尚未初始化")
    return service


@app.get("/health")
async def health():
    svc = _svc()
    llm_cfg = svc.cfg.llm

    # 角色 -> 最终绑定的 provider/model (不含密钥)
    roles: Dict[str, Any] = {}
    for role in llm_cfg.role_names():
        targets = svc.llm.resolve(role)
        roles[role] = {
            "provider": targets[0].provider if targets else "",
            "model": targets[0].model if targets else "",
            "fallback": [t.provider for t in targets[1:]],
        }

    # 顶层 model/base_url 反映**实际生效**的那家 (取 chat 角色),
    # 不能用 default_provider —— 那只是「角色没显式配置时的兜底」。
    # 否则 roles 全切走了, 顶层还显示旧 provider, 看着像没切换生效。
    chat_targets = svc.llm.resolve("chat")
    active_provider = chat_targets[0].provider if chat_targets else ""
    active_model = chat_targets[0].model if chat_targets else ""
    active_cfg = llm_cfg.providers.get(active_provider)

    return JSONResponse(
        {
            "status": "ok",
            "model": active_model,
            "base_url": active_cfg.base_url if active_cfg else "",
            "branches": svc.registry.names(),
            "router_mode": svc.cfg.router.mode,
            "api_key_configured": bool(active_cfg.api_key) if active_cfg else False,
            # ---- provider / role ----
            "active_provider": active_provider,
            "default_provider": llm_cfg.default_provider,
            "providers": llm_cfg.provider_names,
            "roles": roles,
        }
    )


@app.get("/v1/branches")
async def branches():
    svc = _svc()
    return JSONResponse(
        {
            "router_mode": svc.cfg.router.mode,
            "default_route": svc.cfg.router.default_route,
            "branches": [
                {"name": name, "speak": h.speak, "type": type(h).__name__}
                for name, h in svc.registry.all().items()
            ],
        }
    )


@app.get("/v1/skills")
async def skills():
    """列出所有已加载的 Skill。"""
    svc = _svc()
    specs = svc.skill_catalog.get_all_specs()
    
    # 按分支分组
    by_branch = {}
    for spec in specs:
        if spec.branch not in by_branch:
            by_branch[spec.branch] = []
        by_branch[spec.branch].append({
            "id": spec.id,
            "name": spec.name,
            "category": spec.category,
            "kind": spec.kind,
            "speak": spec.speak,
            "examples": spec.natural_language_examples[:3],  # 取前 3 个例子
        })
    
    return JSONResponse({
        "total": len(specs),
        "by_branch": by_branch,
        "all_specs": svc.skill_catalog.list_all(),
    })


@app.post("/v1/dispatch", response_model=DispatchResponse)
async def dispatch(req: DispatchRequest):
    return await _svc().dispatch(req)


@app.post("/v1/chat")
async def chat(req: ChatRequest):
    svc = _svc()
    handler = svc.registry.get("chat")
    if handler is None:
        return JSONResponse(
            {"error": "chat 分支未启用"},
            status_code=503,
        )
    return JSONResponse(await svc.chat_only(req))


@app.post("/v1/chat/stream")
async def chat_stream(req: ChatRequest):
    """SSE 流式对话, 事件: delta / done / error。"""
    svc = _svc()

    async def gen():
        try:
            messages = []
            if svc.cfg.system_prompt:
                messages.append({"role": "system", "content": svc.cfg.system_prompt})
            if req.use_history:
                messages.extend(svc.sessions.history(req.session_id))
            messages.append({"role": "user", "content": req.text})

            parts = []
            async for piece in svc.llm.stream(messages, role="chat"):
                parts.append(piece)
                yield "data: " + json.dumps(
                    {"type": "delta", "text": piece}, ensure_ascii=False
                ) + "\n\n"
            reply = "".join(parts)
            if req.use_history:
                svc.sessions.append(req.session_id, req.text, reply)
            yield "data: " + json.dumps(
                {"type": "done", "reply": reply}, ensure_ascii=False
            ) + "\n\n"
        except LLMError as exc:
            yield "data: " + json.dumps(
                {"type": "error", "message": str(exc)}, ensure_ascii=False
            ) + "\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("AGENT_HOST", "0.0.0.0"),
        port=int(os.environ.get("AGENT_PORT", "8800")),
        log_level="info",
    )
