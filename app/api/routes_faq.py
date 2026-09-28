# -*- coding: utf-8 -*-
"""模块 07 的接口层（`/api/v1/faq/*` 与 `/api/v1/faqs/*`）。

## 路由顺序（三条静态路径必须排在 `{id}` 之前）

| 必须在前 | 必须在后 | 写反的后果 |
|---|---|---|
| `/faq/candidates` | `/faq/candidates/{candidate_id}/…` | 深度不同，不冲突 |
| **`/faq/mine`** | — | 与 `/faq/candidates` 平级，不冲突 |
| **`/faq/cache/rebuild`、`/faq/cache/status`** | `/faqs/{faq_id}` | 前缀不同（`faq` vs `faqs`），不冲突 |

> 本模块的路径分成两族：`/faq/*`（候选、挖掘、缓存）与 `/faqs/*`（已发布 FAQ）。
> 这是 Spec §3 的原文口径，**不是笔误**——候选是"过程"，FAQ 是"结果"，
> 两族的权限码也不同（`faq:review` vs `faq:manage`）。

## 权限划分（Spec §3 的细化）

| 族 | 接口 | 权限码 |
|---|---|---|
| 候选 | 列表 / 审核 / 挖掘 | `faq:review` |
| 已发布与缓存 | 列表 / 编辑 / 停用 / 删除 / 重建 / 状态 | `faq:manage` |

两者都只授予 `kb_admin`；`sys_admin` **不含** `faq:*`（与原型 `08` 矩阵的「—」一致）。
所以「无权限 → 403」由 01 的全局依赖产出 `AUTH-2004`，
**本模块不产出功能权限不足的码**（AC-07-29）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import current_user, ensure_permission
from app.api.schemas_faq import (ApproveRequest, ApproveResponse,
                                 CacheRebuildResponse, CacheStatusResponse,
                                 CandidateListResponse, FaqDeleteResponse,
                                 FaqListResponse, FaqToggleRequest,
                                 FaqToggleResponse, FaqUpdateRequest,
                                 FaqUpdateResponse, MineRequest, MineResponse,
                                 RejectRequest, RejectResponse)
from app.core.errors import Err
from app.core.permissions import require_perm
from app.core.response import ok
from app.services.auth_service import UserContext
from app.services.faq_service import faq_service

router = APIRouter(prefix="/api/v1", tags=["07 FAQ 沉淀"])


# --------------------------------------------------------------------------- 候选
@router.get("/faq/candidates", summary="FAQ 候选列表（聚类问题簇）", response_model=None)
@require_perm("faq:review")
async def list_candidates(
    user: UserContext = Depends(current_user),
    status: str = Query("pending", description="pending / approved / rejected；空串=全部"),
    min_frequency: int | None = Query(None, description="只看频次不低于该值的簇"),
    keyword: str = Query("", max_length=50),
    page: int = Query(1, description="页码；<1 报 FAQ-1007"),
    page_size: int = Query(20, le=1000, description="上限 200，超出报 FAQ-1007"),
):
    """默认 `pending` + 频次降序，对齐原型候选表的行序。"""
    ensure_permission(user, "faq:review", Err.FAQ_QUERY_INVALID)
    data = await faq_service.list_candidates(
        status=status or None, min_frequency=min_frequency, keyword=keyword,
        page=page, page_size=page_size)
    return ok(CandidateListResponse(**data).model_dump())


@router.post("/faq/candidates/{candidate_id}/approve",
             summary="审核通过并发布（可改写问法与答案）", response_model=None)
@require_perm("faq:review")
async def approve(candidate_id: str, body: ApproveRequest,
                  user: UserContext = Depends(current_user)):
    """`question`/`answer` 缺省时取候选的代表问法与草案。

    发布成功**≤1 秒**内即可命中缓存（AC-07-09）：缓存是增量的，没有"等重建"。
    """
    ensure_permission(user, "faq:review", Err.FAQ_CANDIDATE_NOT_FOUND)
    data = await faq_service.approve(
        candidate_id=candidate_id, question=body.question, answer=body.answer,
        aliases=body.aliases, category_id=body.category_id,
        related_doc_ids=body.related_doc_ids, review_note=body.review_note,
        actor=user.user_id)
    return ok(ApproveResponse(**data).model_dump())


@router.post("/faq/candidates/{candidate_id}/reject", summary="驳回（可转建文档）",
             response_model=None)
@require_perm("faq:review")
async def reject(candidate_id: str, body: RejectRequest,
                 user: UserContext = Depends(current_user)):
    """备注必填 ≥5 字；`convert_to_gap=true` 时**投递给 08**（07 不写 `knowledge_gaps`）。"""
    ensure_permission(user, "faq:review", Err.FAQ_CANDIDATE_NOT_FOUND)
    data = await faq_service.reject(
        candidate_id=candidate_id, review_note=body.review_note,
        convert_to_gap=body.convert_to_gap, actor=user.user_id)
    return ok(RejectResponse(**data).model_dump())


# --------------------------------------------------------------------------- 挖掘
@router.post("/faq/mine", summary="手动触发挖掘（同步返回）", response_model=None)
@require_perm("faq:review")
async def mine(body: MineRequest, user: UserContext = Depends(current_user)):
    """**同步返回**（演示要即时看到结果）。

    窗口内没有可用日志时返回 `notice`（`FAQ-3009` 的业务告警）——**HTTP 仍是 200**：
    它不是失败，前端据此区分"窗口里真没数据"与"确实 0 候选"。
    """
    ensure_permission(user, "faq:review", Err.FAQ_MINING_RUNNING)
    data = await faq_service.mine(
        window_days=body.window_days, freq_threshold=body.freq_threshold,
        sim_threshold=body.sim_threshold, seed=body.seed, actor=user.user_id)
    return ok(MineResponse(**data.as_dict()).model_dump())


# --------------------------------------------------------------------------- 缓存
@router.post("/faq/cache/rebuild", summary="重建缓存（原子替换）", response_model=None)
@require_perm("faq:manage")
async def rebuild_cache(user: UserContext = Depends(current_user)):
    """失败**保留旧缓存**，绝不清空（`FAQ-4003`）；并发调用返回 `FAQ-3008`。"""
    ensure_permission(user, "faq:manage", Err.FAQ_CACHE_REBUILDING)
    data = await faq_service.rebuild_cache(actor=user.user_id)
    return ok(CacheRebuildResponse(**data).model_dump())


@router.get("/faq/cache/status", summary="缓存状态（运维 / 演示）", response_model=None)
@require_perm("faq:manage")
async def cache_status(user: UserContext = Depends(current_user)):
    """含一致性自检：`cache_size != enabled_count` 时 `consistent=false` + 告警码。"""
    ensure_permission(user, "faq:manage", Err.FAQ_CACHE_INCONSISTENT)
    return ok(CacheStatusResponse(**await faq_service.cache_status()).model_dump())


# --------------------------------------------------------------------------- 已发布
@router.get("/faqs", summary="已发布 FAQ 列表", response_model=None)
@require_perm("faq:manage")
async def list_faqs(
    user: UserContext = Depends(current_user),
    keyword: str = Query("", max_length=50),
    enabled: bool | None = Query(None, description="缓存生效筛选"),
    category_id: str | None = Query(None),
    page: int = Query(1),
    page_size: int = Query(20, le=1000, description="上限 200，超出报 FAQ-1007"),
):
    """响应**不含 `embedding`**（AC-07-32）。"""
    ensure_permission(user, "faq:manage", Err.FAQ_QUERY_INVALID)
    data = await faq_service.list_published(
        keyword=keyword, enabled=enabled, category_id=category_id, page=page,
        page_size=page_size)
    return ok(FaqListResponse(**data).model_dump())


@router.put("/faqs/{faq_id}", summary="编辑已发布 FAQ", response_model=None)
@require_perm("faq:manage")
async def update_faq(faq_id: str, body: FaqUpdateRequest,
                     user: UserContext = Depends(current_user)):
    """改问法/别名 → **必须重新向量化**；改关联文档 → 重新做缓存准入校验。"""
    ensure_permission(user, "faq:manage", Err.FAQ_NOT_FOUND)
    data = await faq_service.update_faq(
        faq_id=faq_id, question=body.question, answer=body.answer,
        aliases=body.aliases, category_id=body.category_id,
        related_doc_ids=body.related_doc_ids, actor=user.user_id)
    return ok(FaqUpdateResponse(**data).model_dump())


@router.post("/faqs/{faq_id}/toggle", summary="缓存生效开关（启用 / 停用）",
             response_model=None)
@require_perm("faq:manage")
async def toggle_faq(faq_id: str, body: FaqToggleRequest,
                     user: UserContext = Depends(current_user)):
    """停用 → **立刻**从缓存移除（同一问题回到 RAG，AC-07-13）；
    启用 → 重新做准入校验（关联文档可能已变成部门受限 → `FAQ-2001`）。"""
    ensure_permission(user, "faq:manage", Err.FAQ_NOT_FOUND)
    data = await faq_service.toggle(faq_id=faq_id, enabled=body.enabled,
                                    actor=user.user_id, reason=body.reason)
    return ok(FaqToggleResponse(**data).model_dump())


@router.delete("/faqs/{faq_id}", summary="删除已发布 FAQ（物理删除）",
               response_model=None)
@require_perm("faq:manage")
async def delete_faq(faq_id: str, user: UserContext = Depends(current_user)):
    """**物理删除**（`question` 是唯一索引，软删会让同问法无法重发）。

    前端必须二次确认；建议优先用「停用」而不是删除。
    """
    ensure_permission(user, "faq:manage", Err.FAQ_NOT_FOUND)
    data = await faq_service.delete(faq_id=faq_id, actor=user.user_id)
    return ok(FaqDeleteResponse(**data).model_dump())


__all__ = ["router"]
