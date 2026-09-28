# -*- coding: utf-8 -*-
"""模块 05 的接口层（`/api/v1/perm/*`）。

## 路由声明顺序是硬要求

| 必须在前 | 必须在后 | 写反的后果 |
|---|---|---|
| `/perm/check` | `/perm/{doc_id}` | `check` 被当成 `doc_id` → 判定变成"读配置"，405，前端看到"接口不存在" |

（`/perm/check` 是 POST、`/perm/{doc_id}` 是 GET/PUT，方法不同看似不冲突，
但 FastAPI 按**路径**顺序匹配，`/perm/{doc_id}` 的 GET 会先吃掉 `/perm/check`
的 POST 请求吗？——不会（方法不匹配会继续找），但 PUT `/perm/check` 会命中
`{doc_id}="check"` 进而报 `PERM-3001`。为了不依赖这种微妙行为，**显式把静态路径写在前面**。）

## 权限码

| 接口 | 功能权限 | 说明 |
|---|---|---|
| GET/PUT `/perm/{doc_id}` | `perm:manage` | 配置类，只有知识管理员与系统管理员有 |
| POST `/perm/check` | `perm:check` | **三角色都有**（原型 `08` 标注第 5 条）：它只返回判定结果，不泄漏知识内容 |

`PERM-2002`（功能权限不足）在本模块只作为**服务层兜底**出现，
正常路径由 00 的全局 `enforce_perm` 依赖产出 `AUTH-2004`（ER-09：权限码只有一份实现）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.deps import current_user, ensure_permission
from app.api.schemas_perm import (PermCheckRequest, PermCheckResponse,
                                  PermConfigResponse, PermSaveRequest,
                                  PermSaveResponse)
from app.core.errors import Err
from app.core.permissions import require_perm
from app.core.response import ok
from app.services.auth_service import UserContext
from app.services.permission_service import permission_service

router = APIRouter(prefix="/api/v1/perm", tags=["05 四维数据权限与鉴权引擎"])

# 代查他人鉴权结果所需的功能权限（Spec §3.3：只有 sys_admin 可代查）
IMPERSONATE_PERM = "user:manage"


# --------------------------------------------------------------------------- 判定
@router.post("/check", summary="鉴权判定接口（PRD 验收入口）", response_model=None)
@require_perm("perm:check")
async def check(body: PermCheckRequest, request: Request,
                user: UserContext = Depends(current_user)):
    """判定"某人能不能读某篇知识"，**HTTP 恒为 200**。

    为什么恒 200：`allowed=false` 是**正确答案**，不是错误。
    用 403 表达"不可读"会让调用方（含 06 问答）把它当成"接口调用失败"，
    从而走上错误处理分支 —— 而这个项目里"被拒绝"是**正常业务路径**。

    响应体**不含任何文档内容**（AC-05-10）：否则这个接口就成了越权读取通道。
    """
    ensure_permission(user, "perm:check", Err.PERM_MANAGE_DENIED)
    data = await permission_service.check_for_user(
        doc_id=body.doc_id, current=user, target_user_id=body.user_id,
        can_impersonate=IMPERSONATE_PERM in user.permissions)
    return ok(PermCheckResponse(**data).model_dump())


# --------------------------------------------------------------------------- 配置
@router.get("/{doc_id}", summary="读取四维权限配置", response_model=None)
@require_perm("perm:manage")
async def get_config(doc_id: str, user: UserContext = Depends(current_user)):
    """回显四维勾选态；**无记录返回默认值 + `version=0`，不返回 404**。

    `version=0` 是"尚未配置"的标记 —— 前端据此提示"当前无人可读"
    （原型 `04` 的 `gTxtS` 警告文案描述的就是这个状态）。
    """
    ensure_permission(user, "perm:manage", Err.PERM_MANAGE_DENIED)
    data = await permission_service.get_config(doc_id)
    return ok(PermConfigResponse(**data).model_dump())


@router.put("/{doc_id}", summary="保存四维权限（即时生效）", response_model=None)
@require_perm("perm:manage")
async def save_config(doc_id: str, body: PermSaveRequest,
                      user: UserContext = Depends(current_user)):
    """保存四维权限 + 变更原因（≥5 字，G-11）。

    保存成功后**下一次请求立即按新权限判定**（AD-02）：权限只存文档级一条记录，
    不回写 Milvus，所以没有"回写期间权限不一致"的窗口。
    """
    ensure_permission(user, "perm:manage", Err.PERM_MANAGE_DENIED)
    data = await permission_service.save(
        doc_id=doc_id, is_global=body.is_global, departments=body.departments,
        roles=body.roles, users=body.users, reason=body.reason,
        actor_id=user.user_id)
    return ok(PermSaveResponse(**data).model_dump())


__all__ = ["router", "IMPERSONATE_PERM"]
