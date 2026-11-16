"""热更新管理接口模块。

提供 HTTP API 用于：
1. 配置文件热更新：POST /admin/config/reload
2. Skill 动态注册：POST /admin/skills/register
3. Skill 动态移除：POST /admin/skills/unregister
4. Skill 列表查询：GET /admin/skills/list

这些接口主要用于 MCP 集成场景。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel

from skill.spec import SkillSpec

logger = logging.getLogger("admin")


class ReloadConfigResponse(BaseModel):
    """配置热更新的响应。"""
    status: str  # "ok" | "error"
    message: str
    timestamp: Optional[str] = None


class SkillRegisterRequest(BaseModel):
    """Skill 注册请求。"""
    id: str
    name: str
    branch: str  # "chat" | "tool" | "device"
    description: str = ""
    category: str = "mcp"
    source: str = "mcp"
    tags: List[str] = []
    example_inputs: List[str] = []


class SkillRegisterResponse(BaseModel):
    """Skill 注册响应。"""
    status: str  # "ok" | "error"
    skill_id: str
    message: str


class SkillUnregisterResponse(BaseModel):
    """Skill 移除响应。"""
    status: str  # "ok" | "error"
    skill_id: str
    message: str


class SkillListResponse(BaseModel):
    """Skill 列表响应。"""
    total: int
    skills: List[Dict[str, Any]]


class AdminManager:
    """热更新和管理操作的集中管理器。"""

    def __init__(self, get_service: Callable[[], Any]) -> None:
        """初始化管理器。
        
        Args:
            get_service: 获取 Service 实例的可调用对象
        """
        self.get_service = get_service
        self.logger = logging.getLogger("admin_manager")

    async def reload_config(self) -> ReloadConfigResponse:
        """热更新配置文件。
        
        流程：
        1. 重新加载 agent.yaml
        2. 重新创建 Router（编译规则）
        3. 保留已注册的 Skill
        
        失败时保持原配置运行。
        """
        self.logger.info("开始热更新配置...")
        try:
            from config import load_config
            
            svc = self.get_service()
            new_cfg = load_config()
            
            # 备份旧配置
            old_cfg = svc.cfg
            old_router = svc.router
            
            try:
                # 更新配置
                svc.cfg = new_cfg
                
                # 重新加载 Registry（Handler）
                # 注意：这可能会导致正在运行的请求使用混合的旧/新 Handler
                # 如果需要更强的一致性，应该使用版本机制或请求队列
                svc.registry.load(new_cfg, svc.llm)
                
                # 重新创建 Router（重新编译规则）
                from router import Router
                svc.router = Router(
                    new_cfg,
                    svc.registry,
                    svc.llm,
                    svc.skill_catalog,
                )
                
                self.logger.info("配置热更新完成")
                return ReloadConfigResponse(
                    status="ok",
                    message="配置已重新加载",
                )
            except Exception as e:
                # 恢复旧配置
                self.logger.error(f"配置热更新失败: {e}, 正在恢复旧配置")
                svc.cfg = old_cfg
                svc.router = old_router
                raise

        except Exception as e:
            self.logger.error(f"热更新配置失败: {e}", exc_info=True)
            return ReloadConfigResponse(
                status="error",
                message=f"配置热更新失败: {str(e)}",
            )

    async def register_skill(
        self,
        skill_id: str,
        name: str,
        branch: str,
        description: str = "",
        category: str = "mcp",
        source: str = "mcp",
        tags: List[str] = None,
        example_inputs: List[str] = None,
    ) -> SkillRegisterResponse:
        """动态注册 Skill（MCP 集成用）。
        
        Args:
            skill_id: Skill 唯一标识
            name: Skill 显示名称
            branch: 分支 ("chat" | "tool" | "device")
            description: 功能描述
            category: 分类
            source: 来源标记
            tags: 标签列表
            example_inputs: 使用示例
        """
        self.logger.info(f"尝试注册 Skill: {skill_id} ({name})")
        try:
            svc = self.get_service()
            
            # 创建 SkillSpec
            spec = SkillSpec(
                id=skill_id,
                name=name,
                branch=branch,
                description=description,
                category=category,
                source=source,
                tags=tags or [],
                example_inputs=example_inputs or [],
            )
            
            # 注册到 Catalog（不提供工厂函数，因为 MCP Skill 由外部实现）
            svc.skill_catalog.register(spec, factory=None)
            
            self.logger.info(f"Skill {skill_id} 已注册")
            return SkillRegisterResponse(
                status="ok",
                skill_id=skill_id,
                message=f"Skill {name} 已注册",
            )
        except Exception as e:
            self.logger.error(f"注册 Skill {skill_id} 失败: {e}", exc_info=True)
            return SkillRegisterResponse(
                status="error",
                skill_id=skill_id,
                message=f"注册失败: {str(e)}",
            )

    async def unregister_skill(self, skill_id: str) -> SkillUnregisterResponse:
        """动态移除 Skill（MCP 集成用）。
        
        Args:
            skill_id: Skill 唯一标识
        """
        self.logger.info(f"尝试移除 Skill: {skill_id}")
        try:
            svc = self.get_service()
            
            if skill_id not in svc.skill_catalog._specs:
                return SkillUnregisterResponse(
                    status="error",
                    skill_id=skill_id,
                    message=f"Skill {skill_id} 不存在",
                )
            
            # 移除 Spec
            del svc.skill_catalog._specs[skill_id]
            
            # 移除工厂函数（如果存在）
            if skill_id in svc.skill_catalog._factories:
                del svc.skill_catalog._factories[skill_id]
            
            self.logger.info(f"Skill {skill_id} 已移除")
            return SkillUnregisterResponse(
                status="ok",
                skill_id=skill_id,
                message=f"Skill {skill_id} 已移除",
            )
        except Exception as e:
            self.logger.error(f"移除 Skill {skill_id} 失败: {e}", exc_info=True)
            return SkillUnregisterResponse(
                status="error",
                skill_id=skill_id,
                message=f"移除失败: {str(e)}",
            )

    async def list_skills(
        self,
        branch: Optional[str] = None,
    ) -> SkillListResponse:
        """列出所有已注册的 Skill。
        
        Args:
            branch: 可选的分支过滤 ("chat" | "tool" | "device")
        """
        try:
            svc = self.get_service()
            
            if branch:
                specs = svc.skill_catalog.find_by_branch(branch)
            else:
                specs = svc.skill_catalog.get_all_specs()
            
            skills = [
                {
                    "id": spec.id,
                    "name": spec.name,
                    "branch": spec.branch,
                    "description": spec.description,
                    "category": spec.category,
                    "source": spec.source,
                    "tags": spec.tags,
                    "example_inputs": spec.example_inputs,
                }
                for spec in specs
            ]
            
            return SkillListResponse(
                total=len(skills),
                skills=skills,
            )
        except Exception as e:
            self.logger.error(f"列出 Skill 失败: {e}", exc_info=True)
            return SkillListResponse(total=0, skills=[])
