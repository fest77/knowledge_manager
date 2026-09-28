# -*- coding: utf-8 -*-
"""模块 08 的接口层（`/api/v1/gaps/*`）。

## 路由顺序（静态路径必须在 `{gap_id}` 之前）

| 必须在前 | 必须在后 | 写反的后果 |
|---|---|---|
| **`POST /gaps/aggregate`** | `GET /gaps/{gap_id}` | `aggregate` 被当成 `gap_id` → 聚合变成"查这个缺口" |
| `GET /gaps/export` | `GET /gaps/{gap_id}` | 同上：导出变成 404 |

> 这是本模块**最容易踩的一处**：`/gaps/aggregate` 与 `/gaps/{gap_id}` 同深度、
> 只差一个静态段 vs 路径参数，FastAPI 按声明顺序匹配。

## 权限

| 接口 | 功能权限 |
|---|---|
| `GET /gaps`、`GET /gaps/{id}`、`GET /gaps/export` | `gap:read` |
| `POST /gaps/{id}/convert`、`/ignore`、`POST /gaps/aggregate` | `gap:convert` |

`asker` 与 `sys_admin` **都没有** `gap:*`（AC-08-17：两者都返回 403，由 01 产出
`AUTH-2004`）；只有在 01 的中间件被绕过时，服务层才用 `GAP-2001` 兜底。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from app.api.deps import current_user, ensure_permission
from app.api.schemas_gap import (GapAggregateRequest, GapAggregateResponse,
                                 GapConvertRequest, GapConvertResponse,
                                 GapIgnoreRequest,
                                 GapIgnoreResponse, GapListResponse)
from app.core.errors import Err
from app.core.permissions import require_perm
from app.core.response import ok
from app.services.auth_service import UserContext
from app.services.gap_service import gap_service

router = APIRouter(prefix="/api/v1/gaps", tags=["08 知识缺口"])



@router.get("", summary="缺口清单（未命中提问 / 提问部门 / 近期频次 / 最高相似度 / 建议创建分类）",
            response_model=None)
@require_perm("gap:read")
async def list_gaps(
    user: UserContext = Depends(current_user),
    status: str = Query("open", description="open / converted / ignored / all"),
    dept_id: str | None = Query(None, description="部门筛选（原型「全部部门 ▾」）"),
    keyword: str = Query("", max_length=100),
    category_id: str | None = Query(None),
    min_frequency: int = Query(1, description="频次下限；<0 报 GAP-1001"),
    sort: str = Query("frequency_desc", description="五种排序之一"),
    group_by: str = Query("none", description="none / question（跨部门聚合视图）"),
    page: int = Query(1),
    page_size: int = Query(20, le=1000, description="上限 200，超出报 GAP-1001"),
):
    """**永远读快照，不触发实时聚合**（Spec §3.1 R-07）；`aggregated_at` 供前端提示数据时间。"""
    ensure_permission(user, "gap:read", Err.GAP_PERM_DENIED)
    data = await gap_service.list_gaps(
        status=status, dept_id=dept_id, keyword=keyword, category_id=category_id,
        min_frequency=min_frequency, sort=sort, group_by=group_by, page=page,
        page_size=page_size)
    return ok(GapListResponse(**data).model_dump())


# --------------------------------------------------------------------------- 聚合
@router.post("/aggregate", summary="手动触发聚合（演示用，同步返回）",
             response_model=None)
@require_perm("gap:convert")
async def aggregate(body: GapAggregateRequest,
                    user: UserContext = Depends(current_user)):
    """已有聚合在跑时返回 `GAP-3005`（单飞锁，AC-08-19）。

    ⚠️ **必须声明在 `/{gap_id}` 之前**：否则会被当成"查一个叫 aggregate 的缺口"。
    """
    ensure_permission(user, "gap:convert", Err.GAP_PERM_DENIED)
    data = await gap_service.aggregate(window_days=body.window_days,
                                       actor=user.user_id)
    return ok(GapAggregateResponse(**data.as_dict()).model_dump())


# --------------------------------------------------------------------------- 导出
@router.get("/export", summary="导出缺口清单（UTF-8 BOM CSV）", response_model=None)
@require_perm("gap:read")
async def export_gaps(
    user: UserContext = Depends(current_user),
    status: str = Query("open"),
    dept_id: str | None = Query(None),
    keyword: str = Query("", max_length=100),
    category_id: str | None = Query(None),
    min_frequency: int = Query(1),
    sort: str = Query("frequency_desc"),
):
    """CSV 带 **UTF-8 BOM**：没有它，Excel 打开中文是乱码（AC-08-23）。

    超过 `gap.export_max_rows`（默认 5000）行返回 `GAP-1005` —— 不静默截断：
    截断会让使用者以为"缺口就这么多"。
    """
    ensure_permission(user, "gap:read", Err.GAP_PERM_DENIED)
    filename, content = await gap_service.export_csv(
        status=status, dept_id=dept_id, keyword=keyword, category_id=category_id,
        min_frequency=min_frequency, sort=sort)
    return Response(
        content=content.encode("utf-8"), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# --------------------------------------------------------------------------- 详情
@router.get("/{gap_id}", summary="缺口详情（含代表性样本）", response_model=None)
@require_perm("gap:read")
async def get_gap(gap_id: str, user: UserContext = Depends(current_user)):
    """样本展开原始问法与同义问法（AC-08-10：最多 5 条、取最近的）。"""
    ensure_permission(user, "gap:read", Err.GAP_PERM_DENIED)
    data = await gap_service.get_detail(gap_id)
    return ok({"gap": data, "samples": data.get("samples", []),
               "synonym_questions": data.get("synonym_questions", [])})


@router.post("/{gap_id}/convert", summary="一键转建文档（跨模块链路）",
             response_model=None)
@require_perm("gap:convert")
async def convert(gap_id: str, body: GapConvertRequest | None = None,
                  user: UserContext = Depends(current_user)):
    """08 → 03（建占位）→ 04（建导入任务）→ 回写缺口状态。

    重复点击第二次必然命中 `GAP-3001`，且**不会**产生第二个占位文档或第二个任务
    （幂等键分别是 03 的 `source_gap_id` 唯一索引与 04 的"同 doc_id 在途任务"）。
    """
    ensure_permission(user, "gap:convert", Err.GAP_PERM_DENIED)
    data = await gap_service.convert(
        gap_id=gap_id, title=(body.title if body else None),
        category_id=(body.category_id if body else None), actor=user.user_id)
    return ok(GapConvertResponse(**data).model_dump())


@router.post("/{gap_id}/ignore", summary="忽略缺口（人工决策优先于机器重算）",
             response_model=None)
@require_perm("gap:convert")
async def ignore(gap_id: str, body: GapIgnoreRequest | None = None,
                 user: UserContext = Depends(current_user)):
    """忽略后**聚合不会再改回 `open`**（Spec §2.4 的"人工状态保护"）。"""
    ensure_permission(user, "gap:convert", Err.GAP_PERM_DENIED)
    data = await gap_service.ignore(gap_id=gap_id,
                                    reason=(body.reason if body else ""),
                                    actor=user.user_id)
    return ok(GapIgnoreResponse(**data).model_dump())


__all__ = ["router"]
