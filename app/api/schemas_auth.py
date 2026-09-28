# -*- coding: utf-8 -*-
"""模块 01 的接口契约（请求/响应模型）。字段与模块 Spec §3.1/§3.2 逐条对齐。"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings


class LoginRequest(BaseModel):
    """登录请求。`extra="forbid"`：多传字段直接判参数错，避免前端静默传错字段名。"""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(
        min_length=3,
        max_length=32,
        description="登录账号，规则 ^[a-zA-Z][a-zA-Z0-9_]{2,31}$（模块 02 §2.2）",
    )
    password: str = Field(
        min_length=settings.password_min_len,
        description=f"密码，{settings.password_min_len}~{settings.password_max_bytes} 字节",
    )


class RoleBrief(BaseModel):
    """角色的精简视图（供前端渲染角色标签）。"""

    role_id: str
    code: str
    name: str
    is_system: bool


class MenuItem(BaseModel):
    """一个可渲染的菜单入口。

    `code` 是该路由的**入口权限码**（同一路由可能被多个权限开放，取 `sort` 最小者），
    `path` 为前端 hash 路由（如 `#/docs`）。
    """

    code: str
    name: str
    path: str
    sort: int


class UserProfile(BaseModel):
    """`/auth/me` 与 `/auth/login` 共用的用户视图。"""

    user_id: str
    username: str
    real_name: str
    dept_id: str
    dept_name: str | None = None
    roles: list[RoleBrief] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    menus: list[MenuItem] = Field(default_factory=list)


class LoginResponse(BaseModel):
    """登录成功响应：令牌 + 用户上下文。"""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    user: UserProfile
