# -*- coding: utf-8 -*-
"""模块 07 的请求/响应模型。

两条刻意的取舍（与 04/05/06 同一口径）：

| 取舍 | 为什么 |
|---|---|
| 长度/范围约束**不写在模型上** | `question` 2~200、`aliases` ≤10、`page_size` ≤200 都由服务层报 `FAQ-*` 码 |
| 列表响应**不含 `embedding`** | 1024 维数组会让 100 条列表膨胀到几百 KB（AC-07-32 要求 < 100KB），而列表页根本不用它 |
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# 候选的三种状态（与 `core/enums.FaqCandidateStatus` 一致，ER-10 独立枚举）
CANDIDATE_STATUSES: tuple[str, ...] = ("pending", "approved", "rejected")


class RelatedDoc(BaseModel):
    """关联知识单元（标题由 03 的只读接口补齐，**不落库**）。"""

    doc_id: str
    title: str = ""


class CandidateItem(BaseModel):
    """候选卡片（原型 `05` 的 6 列）。"""

    candidate_id: str
    cluster_key: str | None = None
    questions: list[str] = Field(default_factory=list)
    representative_question: str | None = None
    frequency: int = 0
    related_docs: list[RelatedDoc] = Field(default_factory=list)
    draft_answer: str = ""
    confidence: float = 0.0
    status: str = "pending"
    status_text: str = ""
    reviewed_by: str | None = None
    reviewed_at: int | None = None
    review_note: str | None = None
    faq_id: str | None = None
    first_seen_at: int | None = None
    last_seen_at: int | None = None
    window_start: int | None = None
    window_end: int | None = None


class WindowHint(BaseModel):
    """表头提示：近 N 天 · 频次阈值 · 相似度阈值。"""

    window_start: int = 0
    window_end: int = 0
    freq_threshold: int = 20
    sim_threshold: float = 0.85


class CandidateListResponse(BaseModel):
    """候选列表出参。"""

    items: list[CandidateItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    window: WindowHint = Field(default_factory=WindowHint)


class ApproveRequest(BaseModel):
    """审核通过入参：`question`/`answer` 是**人工改写后的最终值**。"""

    question: str | None = None
    answer: str | None = None
    aliases: list[str] | None = None
    category_id: str | None = None
    related_doc_ids: list[str] | None = None
    review_note: str = ""


class ApproveResponse(BaseModel):
    """发布出参：`cache_injected=false` 时要看 `message`（说明了原因）。"""

    candidate_id: str
    status: str
    faq_id: str
    enabled: bool = False
    cache_size: int = 0
    cache_injected: bool = False
    message: str = ""


class RejectRequest(BaseModel):
    """驳回入参：`review_note` 必填 ≥5 字（`FAQ-1004`）。"""

    review_note: str = ""
    convert_to_gap: bool = False


class RejectResponse(BaseModel):
    """驳回出参：`gap_forwarded=false` 表示投递给 08 失败（可稍后重试）。"""
    candidate_id: str
    status: str
    gap_forwarded: bool = False


class MineRequest(BaseModel):
    """手动挖掘入参（全部可选，缺省取配置）。"""

    window_days: int | None = None
    freq_threshold: int | None = None
    sim_threshold: float | None = None
    seed: int | None = None


class MineResponse(BaseModel):
    """挖掘出参；`notice` 非空时是 `FAQ-3009` 的业务告警（**HTTP 仍 200**）。"""

    window_start: int
    window_end: int
    scanned_logs: int = 0
    clusters: int = 0
    candidates_created: int = 0
    candidates_updated: int = 0
    candidates_skipped_suppressed: int = 0
    gaps_forwarded: int = 0
    elapsed_ms: int = 0
    degraded: bool = False
    notice: str = ""


class FaqItem(BaseModel):
    """已发布 FAQ 列表行（**无 `embedding`**）。"""

    faq_id: str
    question: str | None = None
    answer_brief: str = ""
    related_doc_ids: list[str] = Field(default_factory=list)
    related_doc_titles: list[str] = Field(default_factory=list)
    category_id: str | None = None
    hit_count: int = 0
    enabled: bool = False
    enabled_text: str = ""
    aliases: list[str] = Field(default_factory=list)
    published_by: str | None = None
    published_at: int | None = None
    updated_at: int | None = None


class FaqListResponse(BaseModel):
    """已发布列表出参：`cache_size` 应恒等于 `enabled_count`（不等则 `FAQ-5002`）。"""

    items: list[FaqItem] = Field(default_factory=list)
    total: int = 0
    enabled_count: int = 0
    cache_size: int = 0
    page: int = 1
    page_size: int = 20


class FaqUpdateRequest(BaseModel):
    """编辑入参（全部可选，只改传了的字段）。"""

    question: str | None = None
    answer: str | None = None
    aliases: list[str] | None = None
    category_id: str | None = None
    related_doc_ids: list[str] | None = None


class FaqUpdateResponse(BaseModel):
    """编辑出参：`changed` 列出真正改动的字段，`cache_reinjected` 表示已重新注入缓存。"""
    faq_id: str
    changed: dict[str, Any] = Field(default_factory=dict)
    cache_reinjected: bool = False
    enabled: bool = False


class FaqToggleRequest(BaseModel):
    """缓存生效开关入参。"""

    enabled: bool
    reason: str = ""


class FaqToggleResponse(BaseModel):
    """缓存生效开关出参（`cache_size` 便于前端立刻看到条数变化）。"""
    faq_id: str
    enabled: bool
    cache_size: int = 0


class FaqDeleteResponse(BaseModel):
    """删除出参（物理删除；建议优先用「停用」）。"""
    faq_id: str
    deleted: bool = True
    cache_size: int = 0


class CacheRebuildResponse(BaseModel):
    """缓存重建出参：条数 / 耗时 / 代次。"""
    cache_size: int
    elapsed_ms: int
    generation: int


class CacheStatusResponse(BaseModel):
    """缓存状态（含一致性自检与匹配耗时 P95）。"""

    cache_size: int
    enabled_count: int
    faqs_total: int
    candidates_total: int
    candidates_pending: int
    generation: int
    dim: int
    threshold: float
    cache_enabled: bool
    pending_hits: int
    match_p95_ms: float
    mining: bool
    consistent: bool
    warning: str = ""


__all__ = [
    "CANDIDATE_STATUSES", "RelatedDoc", "CandidateItem", "WindowHint",
    "CandidateListResponse", "ApproveRequest", "ApproveResponse", "RejectRequest",
    "RejectResponse", "MineRequest", "MineResponse", "FaqItem", "FaqListResponse",
    "FaqUpdateRequest", "FaqUpdateResponse", "FaqToggleRequest", "FaqToggleResponse",
    "FaqDeleteResponse", "CacheRebuildResponse", "CacheStatusResponse",
]
