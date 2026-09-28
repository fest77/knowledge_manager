# -*- coding: utf-8 -*-
"""模块 03 的接口契约（请求/响应模型）。

`DOC-1001` 里有一条容易漏掉的规则：**提交不可写字段要报参数错**——
`file_hash` / `file_name` / `chunk_count` / `status` / `permission_summary` /
`import_status` 都属 04 或本模块内部维护，前端传了必须明确拒绝。
如果只是靠 `extra="forbid"`，它们会被当成"多传字段"报 `SYS-1001`，
而 Spec 要求的是 `DOC-1001`——同一个错误两种码，前端没法按码处理。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.errors import BizError, Err

# 客户端**不得**提交的字段（§5 `DOC-1001`）
READONLY_FIELDS = ("file_hash", "file_name", "file_ext", "file_size", "storage",
                   "chunk_count", "char_count", "status", "permission_summary",
                   "import_status", "doc_no", "deleted_at", "deleted_by")


class _RejectReadonly(BaseModel):
    """拒绝提交只读字段的基类。"""

    @model_validator(mode="before")
    @classmethod
    def _no_readonly(cls, data: Any) -> Any:
        if isinstance(data, dict):
            hit = sorted(set(data) & set(READONLY_FIELDS))
            if hit:
                raise BizError(Err.DOC_PARAM_INVALID,
                               f"以下字段不可由接口写入（由导入链路或内部维护）：{hit}")
        return data


class DocListItem(BaseModel):
    """台账列表的一行（9 列，AC-03-01）。"""

    doc_id: str
    doc_no: str
    title: str = ""
    file_name: str = ""
    file_ext: str = ""
    file_size: int = 0
    category_id: str | None = None
    category_path: str = ""
    tags: list[str] = Field(default_factory=list)
    chunk_count: int = 0
    status: str = "disabled"
    status_text: str = ""
    import_status: str = "pending"
    permission_label: str = "unconfigured"
    permission_text: str = ""
    updated_at: int = 0
    created_at: int = 0
    deleted_at: int | None = None


class DocSummary(BaseModel):
    """列表汇总（原型 `dlCount`）。"""

    total: int = 0
    enabled: int = 0
    disabled: int = 0
    importing: int = 0
    deleted: int = 0


class DocPageResponse(BaseModel):
    """`GET /api/v1/docs` 的出参。"""

    items: list[DocListItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    summary: DocSummary = Field(default_factory=DocSummary)


class DocDetailResponse(DocListItem):
    """详情在列表字段之上补权限摘要与导入失败原因。"""

    permission_summary: dict[str, Any] = Field(default_factory=dict)
    import_error: dict[str, Any] | None = None
    deleted_by: str | None = None


class DocUpdateRequest(_RejectReadonly):
    """编辑（标题 / 分类 / 标签）。三个字段都可选，**空请求体报 `DOC-1001`**。"""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    category_id: str | None = None
    tags: list[str] | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> "DocUpdateRequest":
        if not self.model_fields_set:
            raise BizError(Err.DOC_PARAM_INVALID, "编辑请求体不能为空")
        return self


class DocUpdateResponse(BaseModel):
    """编辑的出参：实际改了哪些字段。"""

    doc_id: str
    changed: dict[str, object] = Field(default_factory=dict)


class DocToggleRequest(BaseModel):
    """启用 / 停用。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


class DocToggleResponse(BaseModel):
    """启停的出参。`changed=false` 表示本来就是这个状态（幂等）。"""

    doc_id: str
    status: str
    changed: bool = True


class DocDeleteResponse(BaseModel):
    """软删除的出参。重复删除时 `deleted=false`（幂等，不报错）。"""

    doc_id: str
    deleted: bool = True


class DocRestoreRequest(BaseModel):
    """从回收站恢复。`restore_status=enabled` 要求导入已完成（`DOC-3010`）。"""

    model_config = ConfigDict(extra="forbid")

    restore_status: str = "disabled"


class DocRestoreResponse(BaseModel):
    """恢复的出参。"""

    doc_id: str
    status: str


class DedupItem(BaseModel):
    """一条去重预检请求。"""

    model_config = ConfigDict(extra="forbid")

    file_hash: str
    file_name: str = ""


class DedupRequest(BaseModel):
    """批量去重预检（1~200 条）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[DedupItem] = Field(min_length=1)


class DedupResult(BaseModel):
    """一条去重决策。"""

    file_hash: str
    file_name: str = ""
    decision: str
    doc_id: str | None = None
    title: str | None = None
    name_conflict: bool = False


class DedupResponse(BaseModel):
    """`POST /api/v1/docs/dedup-check` 的出参。"""

    items: list[DedupResult] = Field(default_factory=list)


class CategoryNode(BaseModel):
    """分类树节点。"""

    category_id: str
    name: str
    parent_id: str | None = None
    path: list[str] = Field(default_factory=list)
    path_ids: list[str] = Field(default_factory=list)
    level: int = 0
    sort: int = 0
    doc_count: int = 0
    children: list["CategoryNode"] = Field(default_factory=list)


class CategoryTreeResponse(BaseModel):
    """`GET /api/v1/categories` 的出参。"""

    items: list[CategoryNode] = Field(default_factory=list)


class CategoryCreateRequest(BaseModel):
    """新建分类。名称不能含 `/`（`DOC-1003`）。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=50)
    parent_id: str | None = None
    sort: int | None = None


class CategoryUpdateRequest(BaseModel):
    """编辑 / 移动 / 排序分类。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=50)
    parent_id: str | None = None
    sort: int | None = None


class CategoryIdResponse(BaseModel):
    """新建分类的出参。"""

    category_id: str


class CategoryUpdateResponse(BaseModel):
    """编辑分类的出参。"""

    category_id: str
    changed: dict[str, object] = Field(default_factory=dict)


class CategoryDeleteResponse(BaseModel):
    """删除分类的出参：顺带回报被转为"未分类"的回收站文档数。"""

    deleted: bool = True
    detached_docs: int = 0


class RecountResponse(BaseModel):
    """计数重算的出参：差异清单供人工核对。"""

    checked: int = 0
    changed: dict[str, dict[str, int]] = Field(default_factory=dict)


__all__ = [
    "DocListItem", "DocSummary", "DocPageResponse", "DocDetailResponse",
    "DocUpdateRequest", "DocUpdateResponse", "DocToggleRequest", "DocToggleResponse",
    "DocDeleteResponse", "DocRestoreRequest", "DocRestoreResponse",
    "DedupItem", "DedupRequest", "DedupResult", "DedupResponse",
    "CategoryNode", "CategoryTreeResponse", "CategoryCreateRequest",
    "CategoryUpdateRequest", "CategoryIdResponse", "CategoryUpdateResponse",
    "CategoryDeleteResponse", "RecountResponse", "READONLY_FIELDS",
]
