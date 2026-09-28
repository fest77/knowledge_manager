# -*- coding: utf-8 -*-
"""模块 03 的接口层（`/api/v1/docs/*` 与 `/api/v1/categories/*`）。

**路由声明顺序是硬要求**（两处，写错了功能就悄悄失效）：

| 必须在前 | 必须在后 | 写反的后果 |
|---|---|---|
| `/docs/dedup-check` | `/docs/{doc_id}` | `dedup-check` 被当成 `doc_id` → 预检报"知识单元不存在" |
| `/categories/recount` | `/categories/{category_id}` | `recount` 被当成 `category_id` → 404 |

**本模块不产出"功能权限不足"的码**（AC-03-22）：那是 01 的 `AUTH-2004`，由全局依赖产出。
这里只做服务层兜底（防绕过 FastAPI 直调），用的是 `AUTH-2004` 而不是自造一个 `DOC-2xxx`
—— 同一个现象两个码指向两处实现，正是 2.3 项目踩过的坑（Spec §5 的说明）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import current_user, ensure_permission
from app.api.schemas_doc import (CategoryCreateRequest, CategoryDeleteResponse,
                                 CategoryIdResponse, CategoryTreeResponse,
                                 CategoryUpdateRequest, CategoryUpdateResponse,
                                 DedupRequest, DedupResponse, DocDeleteResponse,
                                 DocDetailResponse, DocPageResponse,
                                 DocRestoreRequest, DocRestoreResponse,
                                 DocToggleRequest, DocToggleResponse,
                                 DocUpdateRequest, DocUpdateResponse, RecountResponse)
from app.core.errors import Err
from app.core.permissions import require_perm
from app.core.response import ok
from app.services.auth_service import UserContext
from app.services.doc_service import category_service, doc_service

router = APIRouter(prefix="/api/v1", tags=["03 知识单元与分类"])


# --------------------------------------------------------------------------- 知识单元
@router.get("/docs", summary="知识单元台账列表（筛选 + 搜索 + 分页 + 汇总）")
@require_perm("doc:read")
async def list_docs(
    user: UserContext = Depends(current_user),
    category_id: str | None = Query(None, description="按分类筛选（默认含子分类）"),
    include_sub: bool = Query(True, description="是否含子分类"),
    status: str | None = Query(None, description="enabled / disabled"),
    permission_label: str | None = Query(None,
                                        description="global / limited / unconfigured"),
    keyword: str | None = Query(None, description="同时匹配标题与文件名"),
    sort_by: str = Query("updated_at", description="updated_at / created_at"),
    view: str = Query("default", description="default / deleted（回收站）"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=1000, description="上限 200，超出报 DOC-1001"),
):
    """列表**不因调用者角色/部门而变**（AC-03-05）：只有功能权限门槛。

    `page_size` 的 `le=1000` 是给服务层留出判断余地——真正的上限 200 由服务层
    报 `DOC-1001`，**不静默截断**（AC-03-02）。若这里直接写 `le=200`，
    越界会变成 Pydantic 的 `SYS-1001`，与 Spec 要求的码不一致。
    """
    ensure_permission(user, "doc:read", Err.AUTH_PERM_DENIED)
    data = await doc_service.list_documents(
        page=page, page_size=page_size, category_id=category_id,
        include_sub=include_sub, status=status, permission_label=permission_label,
        keyword=keyword, sort_by=sort_by, view=view)
    return ok(DocPageResponse(**data).model_dump())


@router.post("/docs/dedup-check", summary="按 file_hash 批量去重预检（1~200 条）")
@require_perm("doc:read")
async def dedup_check(body: DedupRequest, user: UserContext = Depends(current_user)):
    """返回每条的五种 `decision`；**必须声明在 `/docs/{doc_id}` 之前**。"""
    ensure_permission(user, "doc:read", Err.AUTH_PERM_DENIED)
    items = await doc_service.dedup_check([i.model_dump() for i in body.items])
    return ok(DedupResponse(items=items).model_dump())


@router.get("/docs/{doc_id}", summary="知识单元详情")
@require_perm("doc:read")
async def get_doc(doc_id: str, user: UserContext = Depends(current_user),
                  include_deleted: bool = Query(False)):
    """已软删除的默认报 `DOC-3002`；回收站视图带 `include_deleted=true` 才给。"""
    ensure_permission(user, "doc:read", Err.AUTH_PERM_DENIED)
    detail = await doc_service.detail(doc_id, include_deleted=include_deleted)
    return ok(DocDetailResponse(**detail).model_dump())


@router.put("/docs/{doc_id}", summary="编辑知识单元（标题 / 分类 / 标签）")
@require_perm("doc:edit")
async def update_doc(doc_id: str, body: DocUpdateRequest, request: Request,
                     user: UserContext = Depends(current_user)):
    """不可写字段由契约层拒绝（`DOC-1001`）；导入中的文档报 `DOC-3012`。"""
    ensure_permission(user, "doc:edit", Err.AUTH_PERM_DENIED)
    fields = {name: getattr(body, name) for name in body.model_fields_set}
    result = await doc_service.update(doc_id=doc_id, fields=fields,
                                     actor_id=user.user_id, request=request)
    return ok(DocUpdateResponse(**result).model_dump())


@router.post("/docs/{doc_id}/toggle", summary="启用 / 停用知识单元")
@require_perm("doc:toggle")
async def toggle_doc(doc_id: str, body: DocToggleRequest, request: Request,
                     user: UserContext = Depends(current_user)):
    """启用前要求导入已完成（`DOC-3010`），否则会出现"已启用但零切片"的假可用。"""
    ensure_permission(user, "doc:toggle", Err.AUTH_PERM_DENIED)
    result = await doc_service.toggle(doc_id=doc_id, enabled=body.enabled,
                                      actor_id=user.user_id, request=request)
    return ok(DocToggleResponse(**result).model_dump())


@router.delete("/docs/{doc_id}", summary="软删除知识单元（进回收站）")
@require_perm("doc:delete")
async def delete_doc(doc_id: str, request: Request,
                     user: UserContext = Depends(current_user)):
    """**幂等**：重复删除不报错，`deleted=false` 表示这次没有改动。"""
    ensure_permission(user, "doc:delete", Err.AUTH_PERM_DENIED)
    changed = await doc_service.soft_delete(doc_id=doc_id, actor_id=user.user_id,
                                            request=request)
    return ok(DocDeleteResponse(doc_id=doc_id, deleted=changed).model_dump())


@router.post("/docs/{doc_id}/restore", summary="从回收站恢复")
@require_perm("doc:delete")
async def restore_doc(doc_id: str, request: Request,
                      user: UserContext = Depends(current_user),
                      body: DocRestoreRequest | None = None):
    """恢复后默认保持停用；要直接启用则要求导入已完成。"""
    ensure_permission(user, "doc:delete", Err.AUTH_PERM_DENIED)
    restore_status = body.restore_status if body else "disabled"
    result = await doc_service.restore(doc_id=doc_id, actor_id=user.user_id,
                                       restore_status=restore_status, request=request)
    return ok(DocRestoreResponse(**result).model_dump())


# --------------------------------------------------------------------------- 分类
@router.get("/categories", summary="知识分类树（含子树文档数）")
@require_perm("doc:read")
async def list_categories(user: UserContext = Depends(current_user)):
    """`doc_count` 的语义是**含子分类**的未软删除文档数（§2.2）。"""
    ensure_permission(user, "doc:read", Err.AUTH_PERM_DENIED)
    items = await category_service.list_tree()
    return ok(CategoryTreeResponse(items=items).model_dump())


@router.post("/categories/recount", summary="分类文档计数全量重算（兜底）")
@require_perm("doc:category")
async def recount_categories(request: Request, user: UserContext = Depends(current_user)):
    """增量维护总会因异常路径漂移；这个兜底入口回报 before/after 差异供核对。

    **必须声明在 `/categories/{category_id}` 之前**，否则 `recount` 会被当成
    一个分类编号，重算接口直接 404。
    """
    ensure_permission(user, "doc:category", Err.AUTH_PERM_DENIED)
    result = await category_service.recount(actor_id=user.user_id, request=request)
    return ok(RecountResponse(**result).model_dump())


@router.post("/categories", summary="新建分类")
@require_perm("doc:category")
async def create_category(body: CategoryCreateRequest, request: Request,
                          user: UserContext = Depends(current_user)):
    """同级重名 `DOC-3007`、层级超限 `DOC-1004`、名称含 `/` `DOC-1003`。"""
    ensure_permission(user, "doc:category", Err.AUTH_PERM_DENIED)
    category_id = await category_service.create(
        name=body.name, parent_id=body.parent_id, sort=body.sort,
        actor_id=user.user_id, request=request)
    return ok(CategoryIdResponse(category_id=category_id).model_dump())


@router.put("/categories/{category_id}", summary="编辑 / 移动 / 排序分类")
@require_perm("doc:category")
async def update_category(category_id: str, body: CategoryUpdateRequest, request: Request,
                          user: UserContext = Depends(current_user)):
    """移动会**级联重算全部子孙**的 `path` / `path_ids` / `level`。"""
    ensure_permission(user, "doc:category", Err.AUTH_PERM_DENIED)
    fields = {name: getattr(body, name) for name in body.model_fields_set}
    result = await category_service.update(category_id=category_id, fields=fields,
                                          actor_id=user.user_id, request=request)
    return ok(CategoryUpdateResponse(**result).model_dump())


@router.delete("/categories/{category_id}", summary="删除分类（硬删）")
@require_perm("doc:category")
async def delete_category(category_id: str, request: Request,
                          user: UserContext = Depends(current_user)):
    """前置：无子分类（`DOC-3005`）、无在用文档（`DOC-3006`）。

    只有**回收站里**的文档不算阻塞——它们会被转为"未分类"并回报条数。
    """
    ensure_permission(user, "doc:category", Err.AUTH_PERM_DENIED)
    detached = await category_service.delete(category_id=category_id,
                                             actor_id=user.user_id, request=request)
    return ok(CategoryDeleteResponse(detached_docs=detached).model_dump())

