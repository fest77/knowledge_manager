# -*- coding: utf-8 -*-
"""模块 09 的接口层（`/api/v1/metrics/*`）。

## 本模块**刻意不用 `@require_perm`**（与别的模块都不一样）

原因在 AC-09-14：**`kb_admin` 调 `/metrics/*` 必须返回 `MET-2003`**，
而全局的 `enforce_perm` 依赖只会产出 `AUTH-2004`（01 的码，语义是"功能权限不足"）。

两个码对用户的意义不同：

| 码 | 含义 | 前端文案 |
|---|---|---|
| `AUTH-2004` | 你没有这个权限 | 「需要权限：metric:read」 |
| `MET-2003` | **这个页面对知识管理员不开放**（原型 `08` 矩阵画了「—」） | 「运营看板仅系统管理员可见」 |

所以这里把判定收进路由层，用 `MET-*` 码表达（**fail-closed**：异常即拒，ER-04）；
**认证**（你是谁）仍然由 JWT 中间件负责（ER-08），不受影响。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from app.api.deps import current_user
from app.core.errors import BizError, Err
from app.core.response import ok
from app.services.auth_service import UserContext
from app.services.metric_service import metric_service

router = APIRouter(prefix="/api/v1/metrics", tags=["09 运营看板"])

READ = "metric:read"
EXPORT = "metric:export"
# 知识管理员的标志性权限（用它区分"这个页面对他不开放"与"完全没权限"）
KB_ADMIN_MARKER = "doc:upload"


def _assert_read(user: UserContext) -> None:
    """看板读取权限（`MET-2002` / `MET-2003`）。"""
    if READ in user.permissions:
        return
    if KB_ADMIN_MARKER in user.permissions:
        # 知识管理员：不是"权限不足"，而是这个页面本就不对他开放。
        # 这里**不能**返回空数据——原型 `08` 矩阵画的是「—」，边界必须是拒绝
        raise BizError(Err.MET_KB_ADMIN_DENIED, "运营看板仅系统管理员可见")
    raise BizError(Err.MET_PERM_DENIED, f"需要权限：{READ}")


def _assert_export(user: UserContext) -> None:
    """导出权限（`MET-2005`）：有 `metric:read` 但无 `metric:export` 时走这里。"""
    _assert_read(user)
    if EXPORT not in user.permissions:
        raise BizError(Err.MET_EXPORT_DENIED, f"需要权限：{EXPORT}")


def _guard(user: UserContext) -> None:
    """权限 + 限流（R-11：> 60 次/分钟/用户 → `MET-2004`）。"""
    _assert_read(user)
    metric_service.check_rate(user.user_id)


@router.get("/overview", summary="核心指标摘要（5 张卡片 + 总量）", response_model=None)
async def overview(user: UserContext = Depends(current_user),
                   days: int = Query(7, description="近 N 天；1~365"),
                   start_ts: int | None = Query(None, description="起始时间（毫秒）"),
                   end_ts: int | None = Query(None, description="结束时间（毫秒）")):
    """**降级返回零值 + `degraded:true`**：总览是第一屏，整体 500 会让页面全白。"""
    _guard(user)
    return ok(await metric_service.overview(days=days, start_ts=start_ts,
                                            end_ts=end_ts))


@router.get("/trend", summary="访问量与提问量趋势（自动降采样）", response_model=None)
async def trend(user: UserContext = Depends(current_user),
                days: int = Query(7),
                metrics: str = Query("pv,uv", description="逗号分隔：pv/uv/token/"
                                                          "denied/rag/no_knowledge/"
                                                          "faq_hit_cnt"),
                granularity: str | None = Query(None, description="1h / 1d；缺省按跨度自动选"),
                start_ts: int | None = Query(None, description="起始时间（毫秒）"),
                end_ts: int | None = Query(None, description="结束时间（毫秒）")):
    """`x_axis` 与各 `series[].data` **等长**（AC-09-23：前端零补位逻辑）。"""
    _guard(user)
    names = [m.strip() for m in (metrics or "").split(",") if m.strip()]
    return ok(await metric_service.trend(days=days, metrics=names,
                                         granularity=granularity,
                                         start_ts=start_ts, end_ts=end_ts))


@router.get("/latency", summary="响应延时分布（直方图 + P50/P95）", response_model=None)
async def latency(user: UserContext = Depends(current_user),
                  days: int = Query(7),
                  mode: str = Query("bucket", description="bucket（桶估算）/ exact（扫日志）"),
                  group_by: str | None = Query(None, description="source / stage"),
                  percentiles: str = Query("P50,P95", description="只支持 P50 / P95（G-08）"),
                  start_ts: int | None = Query(None, description="起始时间（毫秒）"),
                  end_ts: int | None = Query(None, description="结束时间（毫秒）")):
    """`exact` 不可用时**自动降级为 `bucket`** 并标 `degraded`（AC-09-18 / MET-4005）。"""
    _guard(user)
    names = [p.strip() for p in (percentiles or "").split(",") if p.strip()]
    return ok(await metric_service.latency(
        days=days, mode=mode, group_by=group_by,
        percentiles=names or ["P50", "P95"], start_ts=start_ts, end_ts=end_ts))


@router.get("/ranking", summary="高频问题榜 / 热门知识榜（TOP-N）", response_model=None)
async def ranking(user: UserContext = Depends(current_user),
                  type: str = Query("both", description="question / doc / both"),
                  days: int = Query(7),
                  limit: int = Query(10, description="1~50；超出报 MET-1008"),
                  start_ts: int | None = Query(None, description="起始时间（毫秒）"),
                  end_ts: int | None = Query(None, description="结束时间（毫秒）")):
    """高频问题榜走 `qa_logs` 归一化聚合（AC-09-10）；热门知识榜读 `doc` 桶 + 一次 `$in`。"""
    _guard(user)
    return ok(await metric_service.ranking(kind=type, days=days, limit=limit,
                                           start_ts=start_ts, end_ts=end_ts))


@router.get("/export", summary="看板数据导出（UTF-8 BOM CSV）", response_model=None)
async def export(user: UserContext = Depends(current_user),
                 metric: str = Query("overview", description="overview/trend/latency/ranking"),
                 format: str = Query("csv", description="只支持 csv"),
                 days: int = Query(7),
                 start_ts: int | None = Query(None, description="起始时间（毫秒）"),
                 end_ts: int | None = Query(None, description="结束时间（毫秒）")):
    """**UTF-8 BOM**：没有它 Excel 打开中文是乱码（AC-09-21）。"""
    _assert_export(user)
    metric_service.check_rate(user.user_id)
    if format != "csv":
        raise BizError(Err.MET_EXPORT_FORMAT, f"只支持 csv：{format}")
    filename, content = await metric_service.export_csv(
        metric=metric, days=days, start_ts=start_ts, end_ts=end_ts)
    # 同步流式响应：不落临时文件（Spec §3.5 R-4）
    return Response(content=content.encode("utf-8"),
                    media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="{filename}"'})


__all__ = ["router", "READ", "EXPORT"]
