# -*- coding: utf-8 -*-
"""模块 01 的两只接口：`POST /api/v1/auth/login` 与 `GET /api/v1/auth/me`。

`login` 上挂了模块 10 的 `auth.login_fail` 审计（模块 10 §2.2.1）：
它是本平台里**唯一记录"失败"的登录类事件**，也是排障"用户说登不上"的第一手证据。
写审计的时机在**业务判定失败之后、重新抛出之前**，且只记 username 与失败原因——
`after` 里**绝不出现密码**（模块 10 §2.2.1 的注 + 01 的 AC-01-12）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.deps import current_user, ensure_permission
from app.api.schemas_auth import LoginRequest, LoginResponse, UserProfile
from app.api.schemas_org import (GrantPermissionRequest, GrantPermissionResponse,
                                 PermissionListResponse, RolePermissionResponse)
from app.core.errors import BizError, Err
from app.core.permissions import require_perm
from app.core.response import ok
from app.services import auth_service
from app.services import auth_service as auth_svc
from app.services.audit_service import audit_service

router = APIRouter(prefix="/api/v1/auth", tags=["01 登录与功能权限"])


@router.post("/login", summary="登录换 JWT",
             description="白名单接口。账号不存在与密码错误返回同一个错误码（AUTH-2001），防账号枚举。")
async def login(payload: LoginRequest, request: Request):
    """账号密码换 JWT（白名单接口）。失败时留一条 `auth.login_fail` 审计。"""
    try:
        result: LoginResponse = await auth_service.login(payload.username, payload.password)
    except BizError as exc:
        await _audit_login_fail(request, payload.username, exc)
        raise
    return ok(result.model_dump())


async def _audit_login_fail(request: Request, username: str, exc: BizError) -> None:
    """写登录失败审计。`record()` 永不抛，因此这里不需要 `try/except`（§7.2 D-01）。"""
    await audit_service.record_from_request(
        request,
        action="auth.login_fail",
        actor=username,
        target_type="auth",
        target_id=username,
        after={"username": username, "fail_reason": exc.spec.code},
        outcome="failure",
    )


@router.get("/me", summary="当前用户 + 角色 + 功能权限码 + 菜单",
            description="需要有效 JWT（AUTH-2003/2002 之外的任何失败都会 401）。")
async def me(request: Request, user=Depends(current_user)):
    """返回当前用户 + 角色 + 功能权限码 + 可渲染菜单。"""
    profile = UserProfile(
        user_id=user.user_id,
        username=user.username,
        real_name=user.real_name,
        dept_id=user.dept_id,
        dept_name=user.dept_name,
        roles=user.roles,
        permissions=sorted(user.permissions),
        menus=user.menus,
    )
    return ok(profile.model_dump())


@router.get("/permissions", summary="功能权限定义清单（原型 08 矩阵页的行标题）")
@require_perm("role:manage")
async def list_permissions(request: Request, user=Depends(current_user)):
    """34 条功能权限定义，供矩阵页渲染行与分组。"""
    ensure_permission(user, "role:manage", Err.AUTH_PERM_DENIED)
    items = await auth_svc.list_permission_defs()
    return ok(PermissionListResponse(items=items).model_dump())


# `/api/v1/org/roles/{role_id}/permissions` 的**路径在 /org 分区下，但归属模块是 01**
# （E13 角色功能权限的唯一写入者是 01，总纲 §5）。所以它写在 01 的路由文件里，
# 而不是顺手放进 routes_org.py —— 否则"谁能写 E13"这件事在代码里就看不出来了。
org_perm_router = APIRouter(prefix="/api/v1/org", tags=["01 登录与功能权限"])


@org_perm_router.get("/roles/{role_id}/permissions", summary="角色已有的功能权限")
@require_perm("role:manage")
async def get_role_permissions(role_id: str, request: Request, user=Depends(current_user)):
    """返回该角色已绑定的权限 ID 与权限码（矩阵页的勾选状态）。"""
    ensure_permission(user, "role:manage", Err.AUTH_PERM_DENIED)
    data = await auth_svc.role_permissions(role_id)
    return ok(RolePermissionResponse(**data).model_dump())


@org_perm_router.put("/roles/{role_id}/permissions", summary="分配功能权限（差集）")
@require_perm("role:grant")
async def grant_role_permissions(role_id: str, body: GrantPermissionRequest,
                                 request: Request, user=Depends(current_user)):
    """差集落库（不删重建，避免瞬时权限真空）；无变更报 `AUTH-3001` 且不写审计；
    **不允许移除系统管理员的 `role:grant`**（`AUTH-3002`，防自锁）。"""
    ensure_permission(user, "role:grant", Err.AUTH_PERM_DENIED)
    data = await auth_svc.grant_role_permissions(
        role_id=role_id, permission_ids=body.permission_ids, reason=body.reason,
        actor_id=user.user_id, request=request)
    return ok(GrantPermissionResponse(**data).model_dump())
