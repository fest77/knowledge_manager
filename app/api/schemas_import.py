# -*- coding: utf-8 -*-
"""模块 04 的请求/响应模型（Pydantic 2）。

**为什么把请求模型也写在这里而不是直接用 `Form(...)` 散在路由签名里**：
本模块的入参几乎全是 `multipart/form-data`（文件 + 若干表单字段），
FastAPI 对 multipart 的校验错误会产出**它自己的** 422 结构，
而本项目所有接口都必须是 `{code, message, data, trace_id}` 信封。
所以这里做两件事：① 把响应结构固化成模型（评审与前端契约的唯一来源）；
② 把请求侧的业务校验（数量、长度、枚举）放在**服务层**用 `IMP-*` 报错，
模型只做"结构对不对"，不抢业务码。

**枚举取值直接引 `core/enums.py`**：ER-10 要求每个实体各自定义枚举，
这里只做"对外暴露同一份取值域"，不复制常量——复制会让"新增一个阶段"
变成改两处，而漏改的那处就是接口悄悄不认新阶段的原因。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.core.enums import ImportTaskStage, ImportTaskStatus

# 路径与查询参数的**服务层**真正上限（Spec §3.4 R-01 / §3.7 R-05）
PAGE_SIZE_MAX = 200


class UploadResponse(BaseModel):
    """`POST /import/upload` 出参（Spec §3.1）。

    `reused=true` 时 `task_id` 为 `null`：命中去重复用**不产生任务**，
    前端据此直接提示"该文件已存在"而不是去轮询一个不存在的任务。
    """

    doc_id: str
    task_id: str | None = None
    reused: bool = False
    file_hash: str
    file_ext: str
    file_size: int
    stage: str = ImportTaskStage.PDF_TO_MD.value
    progress: int = 10
    duplicated_of: str | None = None


class BatchItem(BaseModel):
    """批量上传中的一条受理项。"""

    doc_id: str
    task_id: str | None = None
    file_name: str
    reused: bool = False
    file_hash: str


class BatchRejected(BaseModel):
    """批量上传中的一条被拒项（**逐项带码**，前端逐行标红）。"""

    file_name: str
    code: str
    message: str


class BatchResponse(BaseModel):
    """`POST /import/batch` 出参（Spec §3.2）：部分成功也返回 200。"""

    batch_id: str
    total: int
    accepted: int
    reused_count: int
    rejected: list[BatchRejected] = Field(default_factory=list)
    items: list[BatchItem] = Field(default_factory=list)


class TaskDetailResponse(BaseModel):
    """`GET /import/tasks/{task_id}` 出参（Spec §3.3）。

    `stage_index` / `stage_total` 由后端给（Spec §3.4 R-02）：前端要零分支渲染，
    自己算"第几步"就一定会和阶段表脱节。
    """

    task_id: str
    doc_id: str | None = None
    doc_title: str | None = None
    file_name: str | None = None
    status: str
    stage: str
    stage_index: int
    stage_total: int
    progress: int
    done_stages: list[str] = Field(default_factory=list)
    durations: dict[str, int] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
    retry_count: int = 0
    retry_of: str | None = None
    from_stage: str | None = None
    storage_degraded: bool = False
    degraded: list[dict[str, Any]] = Field(default_factory=list)
    created_by: str | None = None
    created_at: int | None = None
    finished_at: int | None = None
    cancel_requested: bool = False


class TaskListItem(BaseModel):
    """导入队列的一行（原型 `02`：文件 / 阶段 / 进度）。"""

    task_id: str
    doc_id: str | None = None
    doc_title: str | None = None
    file_name: str | None = None
    status: str
    stage: str
    stage_index: int
    progress: int
    storage_degraded: bool = False
    created_by: str | None = None
    created_at: int | None = None


class TaskPageResponse(BaseModel):
    """导入队列分页出参（Spec §3.4）。"""

    items: list[TaskListItem] = Field(default_factory=list)
    total: int
    page: int
    page_size: int


class CancelRequest(BaseModel):
    """取消原因（可选，≤ 200 字，写入审计 `doc.import.cancel`）。"""

    reason: str = Field("", max_length=200)


class CancelResponse(BaseModel):
    """取消出参：`cancel_pending` 恒为 `true`（协作式取消，见 Spec §3.5 R-03）。"""

    task_id: str
    status: str
    cancel_pending: bool = True


class RetryRequest(BaseModel):
    """重试入参：`from_stage` 默认从 `pdf_to_md` 起（`upload` 不重做）。"""

    from_stage: str | None = None


class RetryResponse(BaseModel):
    """重试出参：**新任务号** + `retry_of` 指向原任务。"""

    task_id: str
    retry_of: str
    retry_count: int
    status: str
    from_stage: str


class ChunkItem(BaseModel):
    """一片切片的预览项（`content` 默认只给 200 字预览）。"""

    chunk_id: int | None = None
    chunk_index: int
    title: str = ""
    parent_title: str = ""
    file_title: str = ""
    part: int = 0
    enabled: bool = False
    char_count: int = 0
    content: str = ""
    truncated: bool = False


class ChunkPreviewResponse(BaseModel):
    """`GET /import/docs/{doc_id}/chunks` 出参（Spec §3.7）。"""

    doc_id: str
    doc_title: str | None = None
    import_status: str
    total: int
    page: int
    page_size: int
    items: list[ChunkItem] = Field(default_factory=list)


# 供路由层做"枚举取值域"提示（前端联调时直接看 OpenAPI 里的 example）
TASK_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in ImportTaskStatus)
TASK_STAGE_VALUES: tuple[str, ...] = tuple(s.value for s in ImportTaskStage)

__all__ = [
    "PAGE_SIZE_MAX", "TASK_STATUS_VALUES", "TASK_STAGE_VALUES",
    "UploadResponse", "BatchItem", "BatchRejected", "BatchResponse",
    "TaskDetailResponse", "TaskListItem", "TaskPageResponse",
    "CancelRequest", "CancelResponse", "RetryRequest", "RetryResponse",
    "ChunkItem", "ChunkPreviewResponse",
]
