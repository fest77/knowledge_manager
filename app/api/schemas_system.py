# -*- coding: utf-8 -*-
"""模块 00 · 系统配置的接口契约（请求/响应模型）。字段对齐模块 00 与 E22。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ConfigItem(BaseModel):
    """一个配置项（定义 + 当前值）。`minimum`/`maximum` 供前端做输入约束。"""

    key: str
    group: str
    label: str
    value: Any
    value_type: str
    default_value: Any
    editable: bool
    description: str = ""
    minimum: float | None = None
    maximum: float | None = None


class ConfigListResponse(BaseModel):
    """`GET /api/v1/system/config` 的出参。"""

    items: list[ConfigItem] = Field(default_factory=list)


class ConfigUpdateRequest(BaseModel):
    """`PUT /api/v1/system/config` 的入参。

    `reason` 必填（≥5 字）：配置变更会写审计 `config.update`，
    原型 `07` 的 `stNote4` 明确要求留痕，所以原因不能省（与四维权限变更 G-11 同口径）。
    """

    model_config = ConfigDict(extra="forbid")

    values: dict[str, Any] = Field(min_length=1, description="配置键 → 新值")
    reason: str = Field(min_length=5, max_length=200, description="变更原因，写入审计")


class ConfigUpdateResponse(BaseModel):
    """`PUT` 的出参：改了什么、没改什么、以及最新全量配置。"""

    changed: list[str]
    unchanged: list[str]
    items: list[ConfigItem]
