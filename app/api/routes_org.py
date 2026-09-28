# -*- coding: utf-8 -*-
"""模块 02 的接口层（`/api/v1/org/*`）。

**本文件只做三件事**：声明功能权限、把请求体翻译成服务层入参、把结果装进统一信封。
业务规则一律在 `org_service` 里——路由里出现 `if` 判断业务合法性就是分层漏了。

`include_sub_dept` 之类的参数会被**显式拒绝**（`ORG-1004`）而不是静默忽略：
G-02 裁定部门授权**不含子部门**，静默忽略会让调用方以为"传了参数真的把子部门算进来了"。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import current_user, ensure_permission
from app.api.schemas_org import (DeptCreateRequest, DeptCreateResponse,
                                 DeptDeleteResponse, DeptTreeResponse,
                                 DeptUpdateRequest, DeptUpdateResponse,
                                 ResetPasswordRequest, ResetPasswordResponse,
                                 RoleCreateRequest, RoleDeleteResponse, RoleIdResponse,
                                 RoleListResponse, RoleUpdateRequest, RoleUpdateResponse,
                                 UserCreateRequest, UserIdResponse, UserPageResponse,
                                 UserRolesRequest, UserRolesResponse,
                                 UserStatusRequest, UserStatusResponse,
                                 UserUpdateRequest, UserUpdateResponse)
from app.core.errors import Err
from app.core.permissions import require_perm
from app.core.response import ok
from app.services import org_service
from app.services.auth_service import UserContext

router = APIRouter(prefix="/api/v1/org", tags=["02 组织架构"])


@router.get("/departments", summary="部门树（含每部门用户数）")
@require_perm("org:manage")
async def list_departments(
    user: UserContext = Depends(current_user),
    with_user_count: bool | None = Query(True, description="是否返回每部门用户数"),
    include_sub_dept: str | None = Query(None, description="**不接受**（G-02：不含子部门）"),
    sub_dept: str | None = Query(None, description="**不接受**（同上）"),
):
    """返回完整部门树；`user_count` 供删除前提示使用。"""
    ensure_permission(user, "org:manage", Err.AUTH_PERM_DENIED)
    org_service.reject_sub_dept_params(include_sub_dept=include_sub_dept,
                                       sub_dept=sub_dept)
    tree = await org_service.list_dept_tree(
        with_user_count=True if with_user_count is None else with_user_count)
    return ok(DeptTreeResponse(items=tree).model_dump())


@router.post("/departments", summary="新建部门")
@require_perm("dept:create")
async def create_department(body: DeptCreateRequest, request: Request,
                            user: UserContext = Depends(current_user)):
    """同级不重名、父部门须存在且启用、层级上限 5 层；成功后写 `dept.create` 审计。"""
    ensure_permission(user, "dept:create", Err.AUTH_PERM_DENIED)
    dept_id = await org_service.create_dept(
        name=body.name, parent_id=body.parent_id, sort=body.sort,
        leader_user_id=body.leader_user_id, actor_id=user.user_id, request=request)
    return ok(DeptCreateResponse(dept_id=dept_id).model_dump())


@router.put("/departments/{dept_id}", summary="编辑部门（改名 / 移动 / 排序 / 负责人 / 停用）")
@require_perm("dept:edit")
async def update_department(dept_id: str, body: DeptUpdateRequest, request: Request,
                            user: UserContext = Depends(current_user)):
    """移动部门会**级联重算全部子孙**的 `path` / `path_ids` / `level`。

    只处理请求体里**显式出现**的字段（`model_fields_set`）：不传 `parent_id`
    表示"不动它"，传 `parent_id: null` 才是"移到根下"。
    """
    ensure_permission(user, "dept:edit", Err.AUTH_PERM_DENIED)
    fields = {name: getattr(body, name) for name in body.model_fields_set}
    result = await org_service.update_dept(dept_id=dept_id, fields=fields,
                                           actor_id=user.user_id, request=request)
    return ok(DeptUpdateResponse(dept_id=result["dept_id"],
                                 changed=result["changed"]).model_dump())


@router.delete("/departments/{dept_id}", summary="删除部门（三项前置校验）")
@require_perm("dept:delete")
async def delete_department(dept_id: str, request: Request,
                            user: UserContext = Depends(current_user)):
    """无子部门、无用户（含已停用）、未被知识权限引用，三者全过才删。"""
    ensure_permission(user, "dept:delete", Err.AUTH_PERM_DENIED)
    await org_service.delete_dept(dept_id=dept_id, actor_id=user.user_id, request=request)
    return ok(DeptDeleteResponse().model_dump())


# --------------------------------------------------------------------------- 用户
@router.get("/users", summary="用户列表（分页 + 关键字）")
@require_perm("user:manage")
async def list_users(
    user: UserContext = Depends(current_user),
    keyword: str | None = Query(None, description="同时匹配姓名与账号"),
    dept_id: str | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    include_sub_dept: str | None = Query(None, description="**不接受**（G-02）"),
):
    """按关键字/部门/状态分页取用户；列表**不返回手机号与邮箱**。"""
    ensure_permission(user, "user:manage", Err.AUTH_PERM_DENIED)
    org_service.reject_sub_dept_params(include_sub_dept=include_sub_dept)
    data = await org_service.list_users(keyword=keyword, dept_id=dept_id, status=status,
                                        page=page, page_size=page_size)
    return ok(UserPageResponse(**data).model_dump())


@router.post("/users", summary="新增用户")
@require_perm("user:create")
async def create_user(body: UserCreateRequest, request: Request,
                      user: UserContext = Depends(current_user)):
    """账号唯一（大小写不敏感）、口令 ≥8 位含字母与数字、部门与角色须存在。"""
    ensure_permission(user, "user:create", Err.AUTH_PERM_DENIED)
    user_id = await org_service.create_user(
        username=body.username, password=body.password, real_name=body.real_name,
        dept_id=body.dept_id, role_ids=body.role_ids, phone=body.phone,
        email=body.email, actor_id=user.user_id, request=request)
    return ok(UserIdResponse(user_id=user_id).model_dump())


@router.put("/users/{user_id}", summary="编辑用户（姓名 / 部门 / 联系方式）")
@require_perm("user:edit")
async def update_user(user_id: str, body: UserUpdateRequest, request: Request,
                      user: UserContext = Depends(current_user)):
    """调岗会**立即失效该用户的权限缓存**——下一次请求就按新部门判定数据权限。"""
    ensure_permission(user, "user:edit", Err.AUTH_PERM_DENIED)
    fields = {name: getattr(body, name) for name in body.model_fields_set}
    result = await org_service.update_user(user_id=user_id, fields=fields,
                                          actor_id=user.user_id, request=request)
    return ok(UserUpdateResponse(user_id=result["user_id"],
                                 changed=result["changed"]).model_dump())


@router.post("/users/{user_id}/status", summary="停用 / 启用用户")
@require_perm("user:disable")
async def set_user_status(user_id: str, body: UserStatusRequest, request: Request,
                          user: UserContext = Depends(current_user)):
    """两道防自锁：不能停用自己、不能停用最后一个在用的系统管理员。"""
    ensure_permission(user, "user:disable", Err.AUTH_PERM_DENIED)
    status = await org_service.set_user_status(
        user_id=user_id, status=body.status, reason=body.reason,
        actor_id=user.user_id, request=request)
    return ok(UserStatusResponse(user_id=user_id, status=status).model_dump())


@router.post("/users/{user_id}/reset-password", summary="重置密码")
@require_perm("user:reset_pwd")
async def reset_password(user_id: str, body: ResetPasswordRequest, request: Request,
                         user: UserContext = Depends(current_user)):
    """强度同新增；重置后强制重新登录（权限缓存失效）。审计**不含任何密码信息**。"""
    ensure_permission(user, "user:reset_pwd", Err.AUTH_PERM_DENIED)
    await org_service.reset_password(user_id=user_id, new_password=body.new_password,
                                     actor_id=user.user_id, request=request)
    return ok(ResetPasswordResponse(user_id=user_id).model_dump())


@router.put("/users/{user_id}/roles", summary="绑定角色（差集 + 原因必填）")
@require_perm("user:manage")
async def set_user_roles(user_id: str, body: UserRolesRequest, request: Request,
                         user: UserContext = Depends(current_user)):
    """至少保留 1 个角色；`reason` 必填 ≥5 字（G-11）。"""
    ensure_permission(user, "user:manage", Err.AUTH_PERM_DENIED)
    result = await org_service.set_user_roles(
        user_id=user_id, role_ids=body.role_ids, reason=body.reason,
        actor_id=user.user_id, request=request)
    return ok(UserRolesResponse(**result).model_dump())


# --------------------------------------------------------------------------- 角色
@router.get("/roles", summary="角色列表（含功能权限数与用户数）")
@require_perm("role:manage")
async def list_roles(user: UserContext = Depends(current_user)):
    """原型 `07` 的「角色与功能权限」表：编码 / 名称 / 类型 / 功能权限数 / 用户数 / 说明。"""
    ensure_permission(user, "role:manage", Err.AUTH_PERM_DENIED)
    items = await org_service.list_roles()
    return ok(RoleListResponse(items=items).model_dump())


@router.post("/roles", summary="新建业务角色（默认不含任何功能权限）")
@require_perm("role:manage")
async def create_role(body: RoleCreateRequest, request: Request,
                      user: UserContext = Depends(current_user)):
    """裁定 A：业务角色用于**数据权限分组**（PRD 2.9.9 的「管理层」），
    `is_system` 恒为 `False`，且**默认不授予功能权限**。"""
    ensure_permission(user, "role:manage", Err.AUTH_PERM_DENIED)
    role_id = await org_service.create_role(
        code=body.code, name=body.name, description=body.description,
        actor_id=user.user_id, request=request)
    return ok(RoleIdResponse(role_id=role_id).model_dump())


@router.put("/roles/{role_id}", summary="编辑角色（`name` / `description`）")
@require_perm("role:manage")
async def update_role(role_id: str, body: RoleUpdateRequest, request: Request,
                      user: UserContext = Depends(current_user)):
    """`code` 不可修改（`ORG-2007`）：功能权限判定与防自锁逻辑按 code 引用它。"""
    ensure_permission(user, "role:manage", Err.AUTH_PERM_DENIED)
    fields = {name: getattr(body, name) for name in body.model_fields_set}
    result = await org_service.update_role(role_id=role_id, fields=fields,
                                           actor_id=user.user_id, request=request)
    return ok(RoleUpdateResponse(role_id=result["role_id"],
                                 changed=result["changed"]).model_dump())


@router.delete("/roles/{role_id}", summary="删除业务角色（内置角色不可删）")
@require_perm("role:manage")
async def delete_role(role_id: str, request: Request,
                      user: UserContext = Depends(current_user)):
    """内置角色一律拒绝（`ORG-2008`）；仍被用户使用则拒绝（`ORG-2009`）；
    清理 E13 授权时经 01 的 `AuthService`，失败则**不删角色**（`ORG-4002`）。"""
    ensure_permission(user, "role:manage", Err.AUTH_PERM_DENIED)
    await org_service.delete_role(role_id=role_id, actor_id=user.user_id, request=request)
    return ok(RoleDeleteResponse().model_dump())
