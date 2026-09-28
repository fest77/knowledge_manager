# -*- coding: utf-8 -*-
"""模块 10 的接口层（§3.7 的四只 GET 接口）。

**本模块没有任何写接口**——写入走 Python 层的 `AuditService.record()`。
对 `/api/v1/audit/*` 发 `PUT`/`PATCH`/`DELETE` 一律 `AUD-2003`（即使 `sys_admin`），
由文件末尾的兜底路由表达（R-01：append-only 的**对外**表达）。

**路由声明顺序是硬要求**：`/logs/export` 必须声明在 `/logs/{log_id}` **之前**，
否则 FastAPI 会把 `export` 当成一个 `log_id` 匹配掉——这个坑在实现时就得避开。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.deps import current_user, ensure_permission
from app.api.schemas_audit import (ActionListResponse, AuditLogDetail, AuditLogPage,
                                   parse_query, validate_log_id)
from app.core.config import settings
from app.core.errors import BizError, Err
from app.core.permissions import require_perm
from app.core.response import fail, ok
from app.services.audit_service import audit_service
from app.services.auth_service import UserContext

router = APIRouter(prefix="/api/v1/audit", tags=["10 审计日志"])




@router.get("/actions", summary="审计动作字典（前端筛选下拉的数据源）")
@require_perm("audit:read")
async def list_actions(user: UserContext = Depends(current_user)):
    """返回全部动作（含中文名、目标类型、是否要求快照、别名指向）。"""
    ensure_permission(user, "audit:read", Err.AUD_READ_DENIED)
    return ok(ActionListResponse(items=audit_service.list_actions()).model_dump())


@router.get("/logs/export", summary="导出审计流水（CSV / JSON，流式）")
@require_perm("audit:export")
async def export_logs(
    request: Request,
    user: UserContext = Depends(current_user),
    action: str | None = Query(None, description="动作名，逗号分隔可多值（别名自动展开）"),
    actor: str | None = Query(None, description="user_id / username / system"),
    target_type: str | None = Query(None),
    target_id: str | None = Query(None),
    start_ts: str | None = Query(None, description="起始时间（UTC 毫秒，含）"),
    end_ts: str | None = Query(None, description="结束时间（UTC 毫秒，含）"),
    outcome: str | None = Query(None),
    actor_role: str | None = Query(None),
    sort: str | None = Query(None),
    fmt: str | None = Query("csv", alias="format"),
):
    """导出为附件下载。命中 > 50000 条直接拒绝，提示缩小时间范围（`AUD-1005`）。"""
    ensure_permission(user, "audit:export", Err.AUD_EXPORT_DENIED)
    query = await parse_query(action=action, actor=actor, target_type=target_type,
                              target_id=target_id, start_ts=start_ts, end_ts=end_ts,
                              outcome=outcome, actor_role=actor_role, sort=sort,
                              fmt=fmt, need_format=True)
    rows = await audit_service.count(query.mongo_filter)
    if rows > settings.audit_export_max_rows:
        raise BizError(
            Err.AUD_EXPORT_INVALID,
            f"命中 {rows} 条，超过导出上限 {settings.audit_export_max_rows}，请缩小时间范围")

    stamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"audit_logs_{stamp}.{query.fmt}"
    media = "text/csv; charset=utf-8" if query.fmt == "csv" else "application/json"
    stream = audit_service.export_stream(query.mongo_filter, query.fmt,
                                         request=request, rows=rows)
    return StreamingResponse(stream, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Audit-Rows": str(rows),
    })


@router.get("/logs", summary="审计流水（四维筛选 + 分页）")
@require_perm("audit:read")
async def list_logs(
    user: UserContext = Depends(current_user),
    action: str | None = Query(None),
    actor: str | None = Query(None),
    target_type: str | None = Query(None),
    target_id: str | None = Query(None),
    start_ts: str | None = Query(None),
    end_ts: str | None = Query(None),
    outcome: str | None = Query(None),
    actor_role: str | None = Query(None),
    page: str | None = Query(None),
    page_size: str | None = Query(None),
    sort: str | None = Query(None),
):
    """四维筛选：`action` / `actor` / `target_type`+`target_id` / 时间范围。"""
    ensure_permission(user, "audit:read", Err.AUD_READ_DENIED)
    query = await parse_query(action=action, actor=actor, target_type=target_type,
                              target_id=target_id, start_ts=start_ts, end_ts=end_ts,
                              outcome=outcome, actor_role=actor_role, page=page,
                              page_size=page_size, sort=sort)
    data = await audit_service.query(query.mongo_filter, query.page, query.page_size,
                                     sort=query.sort, degraded=query.degraded)
    return ok(AuditLogPage(**data).model_dump())


@router.get("/logs/{log_id}", summary="单条审计详情（含 before / after）")
@require_perm("audit:read")
async def get_log(log_id: str, user: UserContext = Depends(current_user)):
    """`log_id` 先校验格式（`AUD-1003`），再查库（不存在则 `AUD-3001`）。"""
    ensure_permission(user, "audit:read", Err.AUD_READ_DENIED)
    validate_log_id(log_id)
    return ok(AuditLogDetail(**await audit_service.detail(log_id)).model_dump())


@router.api_route("/{rest:path}", methods=["PUT", "PATCH", "DELETE"],
                  include_in_schema=False)
async def reject_write(rest: str) -> JSONResponse:
    """审计记录的**只读**表达（R-01 / AC-10-02）。

    刻意做成"任何路径、任何写方法都拒绝"而不是逐个接口声明：
    将来若有人新增了一个审计子路由却忘了它也是只读的，这里仍然挡得住。
    """
    return fail(Err.AUD_APPEND_ONLY.code, f"{Err.AUD_APPEND_ONLY.message}（/{rest}）",
                Err.AUD_APPEND_ONLY.http)
