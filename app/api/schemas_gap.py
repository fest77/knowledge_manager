# -*- coding: utf-8 -*-
"""模块 08 的请求/响应模型。

两条与 Spec 对齐的刻意取舍：

| 取舍 | 为什么 |
|---|---|
| `page_size` 上限由**服务层**报 `GAP-1001` | 写在 Pydantic 上会变成 `SYS-1001`，前端按 Spec 映射的文案就落空了 |
| 响应里带 `suggested_category_basis` | 它告诉前端这个建议"是算出来的、还是兜底的、还是依赖不可用"。没有它，管理员无法判断"建议为空"是没数据还是服务挂了 |
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# `gap_status` 的三个取值（与 `core/enums.GapStatus` 一致，ER-10 独立枚举）
GAP_STATUSES: tuple[str, ...] = ("open", "converted", "ignored")
GAP_SORTS: tuple[str, ...] = ("frequency_desc", "frequency_asc", "max_score_desc",
                              "max_score_asc", "last_seen_desc")
GAP_BASES: tuple[str, ...] = ("allowed_chunks", "top_recalled", "unavailable", "none")


class GapItem(BaseModel):
    """缺口清单的一行（原型 6 列 + 状态与留痕）。"""

    gap_id: str
    question: str | None = None
    normalized_key: str | None = None
    normalized_text: str | None = None
    dept_id: str | None = None
    dept_name: str = ""
    frequency: int = 0
    max_score: float = 0.0
    suggested_category_id: str | None = None
    suggested_category_path: str | None = None
    suggested_category_basis: str = "none"
    sample_log_ids: list[str] = Field(default_factory=list)
    first_seen_at: int | None = None
    last_seen_at: int | None = None
    status: str = "open"
    status_text: str = ""
    converted_doc_id: str | None = None
    converted_at: int | None = None
    converted_by: str | None = None
    ignored_at: int | None = None
    ignored_by: str | None = None
    ignore_reason: str | None = None
    recurred: bool = False
    last_aggregated_at: int | None = None


class GapSummary(BaseModel):
    """顶部计数（**全量**，不受当前筛选影响）。"""

    open: int = 0
    converted: int = 0
    ignored: int = 0
    total_frequency: int = 0


class GapListResponse(BaseModel):
    """清单出参；`group_by=question` 时 `dept_id`/`dept_name` 会变成数组。"""

    items: list[dict[str, Any]] = Field(default_factory=list)
    summary: GapSummary = Field(default_factory=GapSummary)
    total: int = 0
    page: int = 1
    page_size: int = 20
    aggregated_at: int = 0


class GapDetailResponse(BaseModel):
    """详情出参（含代表性样本与同义问法）。"""

    gap: dict[str, Any] = Field(default_factory=dict)
    samples: list[dict[str, Any]] = Field(default_factory=list)
    synonym_questions: list[str] = Field(default_factory=list)


class GapConvertRequest(BaseModel):
    """转建入参：`title` 缺省取缺口问法，`category_id` 缺省取建议分类。"""

    title: str | None = None
    category_id: str | None = None


class GapConvertResponse(BaseModel):
    """转建出参（`upload_url` 指引用户去上传原文件）。"""

    gap_id: str
    status: str
    converted_doc_id: str
    import_task_id: str
    doc_status: str = "disabled"
    task_status: str = "pending"
    upload_url: str = ""


class GapIgnoreRequest(BaseModel):
    """忽略入参（原因 ≤ 200 字）。"""

    reason: str = ""


class GapIgnoreResponse(BaseModel):
    """忽略出参（留痕：谁在什么时候以什么理由忽略的）。"""

    gap_id: str
    status: str
    ignored_by: str


class GapAggregateRequest(BaseModel):
    """手动聚合入参（`window_days` 1~90，缺省取配置）。"""

    window_days: int | None = None


class GapAggregateResponse(BaseModel):
    """聚合出参；`notice` 非空时是降级提示（如建议分类反查不可用）。"""

    window_start: int
    window_end: int
    scanned_logs: int = 0
    identified: int = 0
    written: int = 0
    removed: int = 0
    elapsed_ms: int = 0
    degraded: bool = False
    notice: str = ""


__all__ = [
    "GAP_STATUSES", "GAP_SORTS", "GAP_BASES",
    "GapItem", "GapSummary", "GapListResponse", "GapDetailResponse",
    "GapConvertRequest", "GapConvertResponse", "GapIgnoreRequest",
    "GapIgnoreResponse", "GapAggregateRequest", "GapAggregateResponse",
]
