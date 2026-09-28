# -*- coding: utf-8 -*-
"""模块 10 的接口契约：出参模型 + 入参校验（模块 10 §3）。

**为什么入参校验单独放一层**：§4.3 步骤 2 要求"参数校验**先于**任何库操作"。
把 `action` 拼错若不校验，接口会静默返回空列表，管理员据此得出"没有这类操作"
的结论——这正是 R-05 存在的理由。所以校验必须发生在 `count_documents` 之前，
且失败码必须是 `AUD-1xxx`（不能让它退化成框架默认的 `SYS-1001`）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from app.audit import actions as action_dict
from app.core.config import settings
from app.core.enums import AuditOutcome
from app.core.errors import BizError, Err
from app.core.logging import logger
from app.repositories import audit_repo

# 审计编号 `LOG{yyyyMMdd}{4位序列}`
LOG_ID_RE = re.compile(r"^LOG\d{12}$")
USER_ID_RE = re.compile(r"^U\d{6}$")
# 角色 code：内置三角色 + 业务角色（`management`…）。§2.1 只列了三个内置角色，
# 但裁定 A 放开了业务角色作数据权限分组标签，它同样会出现在 `actor_role` 快照里，
# 因此这里按**格式**校验而不是封闭集合（DEC-10-9）。
ROLE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
# 毫秒时间戳的下界（2001-09-09）。用来挡住"把秒当毫秒传"这个经典错误：
# 传秒不会报错，只会让查询静默地跨到 1970 年，变成永远查不到数据。
MIN_TS_MS = 1_000_000_000_000
EXPORT_FORMATS: tuple[str, ...] = ("csv", "json")


class AuditLogItem(BaseModel):
    """列表级审计条目（**不含** `before`/`after`，§3.1）。"""

    audit_id: str
    ts: int
    actor: str
    actor_name: str = ""
    actor_role: str = ""
    action: str
    action_name: str = ""
    target_type: str = ""
    target_id: str = "-"
    target_name: str = ""
    changed_fields: list[str] = Field(default_factory=list)
    reason: str = ""
    outcome: str = AuditOutcome.SUCCESS.value
    ip: str = "unknown"
    snapshot_state: str = "full"


class AuditLogDetail(AuditLogItem):
    """单条详情（在列表字段之上补快照载荷，§3.2）。"""

    before: Any = None
    after: Any = None
    redacted_keys: list[str] = Field(default_factory=list)
    extra: Any = None
    trace_id: str | None = None
    ua: str | None = None


class AuditLogPage(BaseModel):
    """四维筛选的分页响应。`degraded=true` 表示 `actor` 未能解析成 `user_id`。"""

    items: list[AuditLogItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    degraded: bool = False


class ActionItem(BaseModel):
    """动作字典的一项（字段与 §3.4 出参一一对应）。"""

    action: str
    name: str
    target_type: str
    requires_snapshot: bool = False
    module: str = ""
    alias_of: str | None = None


class ActionListResponse(BaseModel):
    """`GET /api/v1/audit/actions` 的出参（前端筛选下拉的数据源）。"""

    items: list[ActionItem] = Field(default_factory=list)


@dataclass(slots=True)
class AuditQuery:
    """校验通过后的查询意图：可直接喂给仓储的过滤器 + 分页/排序。"""

    mongo_filter: dict[str, Any] = field(default_factory=dict)
    page: int = 1
    page_size: int = 20
    sort: str = "ts desc"
    degraded: bool = False
    fmt: str = "csv"

    @property
    def skip(self) -> int:
        """`skip` 由页码与页长推出（R-04 已保证不会深分页）。"""
        return (self.page - 1) * self.page_size


def validate_log_id(log_id: str) -> str:
    """校验审计编号格式（§3.2：**先校验格式**，避免无意义的库查询）。"""
    if not LOG_ID_RE.match(log_id or ""):
        raise BizError(Err.AUD_ACTION_UNKNOWN, f"审计编号格式非法：{log_id}")
    return log_id


def _int_of(raw: str | None, spec: str) -> int | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(str(raw).strip())
    except ValueError as exc:
        spec_obj = Err.AUD_TIME_RANGE if spec == "ts" else Err.AUD_PAGE_INVALID
        raise BizError(spec_obj, f"参数必须是整数，收到 {raw!r}") from exc


def _parse_time(start_raw: str | None, end_raw: str | None) -> dict[str, int]:
    """时间范围校验（R-03）：毫秒整数、`start ≤ end`、跨度 ≤ 366 天。"""
    start = _int_of(start_raw, "ts")
    end = _int_of(end_raw, "ts")
    for label, value in (("start_ts", start), ("end_ts", end)):
        if value is not None and value < MIN_TS_MS:
            raise BizError(Err.AUD_TIME_RANGE, f"{label} 必须是毫秒时间戳：{value}")
    if start is not None and end is not None:
        if start > end:
            raise BizError(Err.AUD_TIME_RANGE, "start_ts 不能大于 end_ts")
        span_ms = end - start
        max_ms = settings.audit_query_max_span_days * 24 * 3600 * 1000
        if span_ms > max_ms:
            raise BizError(Err.AUD_TIME_RANGE,
                           f"时间跨度不能超过 {settings.audit_query_max_span_days} 天")
    window: dict[str, int] = {}
    if start is not None:
        window["$gte"] = start
    if end is not None:
        window["$lte"] = end
    return window


def _parse_paging(page_raw: str | None, size_raw: str | None) -> tuple[int, int]:
    """分页校验（R-04）：`page ≥ 1`、`page_size ∈ [1,200]`、`page × page_size ≤ 10000`。

    注意不能写 `_int_of(...) or 1`：`page=0` 会被 `or` 悄悄改成 1，
    于是"非法页码"变成了"合法首页"——少报一个参数错误，多出一个难以复现的行为。
    """
    page = _int_of(page_raw, "page")
    page_size = _int_of(size_raw, "page_size")
    page = 1 if page is None else page
    page_size = 20 if page_size is None else page_size
    if page < 1 or not 1 <= page_size <= 200:
        raise BizError(Err.AUD_PAGE_INVALID, f"page={page} page_size={page_size} 不合法")
    if page * page_size > settings.audit_query_max_depth:
        raise BizError(Err.AUD_PAGE_INVALID,
                       f"查询范围过深：page × page_size 不得超过 {settings.audit_query_max_depth}")
    return page, page_size


def _parse_actions(raw: str | None) -> list[str]:
    """动作筛选（R-05）：逗号分隔可多值，每个都必须在字典内。"""
    values = [v.strip() for v in (raw or "").split(",") if v.strip()]
    if not values:
        return []
    unknown = action_dict.unknown_actions(values)
    if unknown:
        raise BizError(Err.AUD_ACTION_UNKNOWN, f"动作名不在字典内：{unknown}")
    return action_dict.expand_action_filter(values)


def _parse_enum(raw: str | None, allowed: Any, label: str) -> str | None:
    if raw is None or str(raw).strip() == "":
        return None
    value = str(raw).strip()
    if value not in allowed:
        raise BizError(Err.AUD_FILTER_INVALID, f"{label} 取值非法：{value}")
    return value


async def _resolve_actor(raw: str | None) -> tuple[list[str], bool]:
    """`actor` 维（§3.1）：支持 `user_id` / `username` / 特殊值 `system`。

    降级口径（§7.1）：解析不出 `user_id` 时**不报 500**，只用原值精确筛，
    并在响应里置 `degraded=true`——让"筛不出东西"这件事有解释，而不是静默空列表。
    """
    value = (raw or "").strip()
    if not value:
        return [], False
    if value == "system" or USER_ID_RE.match(value):
        return [value], False
    try:
        user_id = await audit_repo.find_user_id_by_username(value)
    except Exception as exc:                                  # noqa: BLE001
        logger.warning("actor 解析失败，降级为按原值筛选：%s", exc)
        return [value], True
    if user_id:
        return [user_id], False
    return [value], True


async def parse_query(*, action: str | None = None, actor: str | None = None,
                      target_type: str | None = None, target_id: str | None = None,
                      start_ts: str | None = None, end_ts: str | None = None,
                      outcome: str | None = None, actor_role: str | None = None,
                      page: str | None = None, page_size: str | None = None,
                      sort: str | None = None, fmt: str | None = None,
                      need_format: bool = False) -> AuditQuery:
    """把原始查询串校验并组装成 `AuditQuery`（§3.6 R-03~R-07、R-14）。

    所有筛选之间是 **AND**，同一维度用 `$in`（同为 OR）——与概要设计 §5 的
    "四维筛选"语义一致。
    """
    sort_key = (sort or "ts desc").strip()
    if sort_key not in audit_repo.SORTS:
        raise BizError(Err.AUD_FILTER_INVALID, f"sort 只允许 {sorted(audit_repo.SORTS)}")

    export_fmt = "csv"
    if need_format:
        export_fmt = (fmt or "csv").strip().lower()
        if export_fmt not in EXPORT_FORMATS:
            raise BizError(Err.AUD_EXPORT_INVALID, f"format 只允许 {list(EXPORT_FORMATS)}")

    page_no, size = _parse_paging(page, page_size)
    actions = _parse_actions(action)
    actor_values, degraded = await _resolve_actor(actor)

    flt: dict[str, Any] = {}
    if actions:
        flt["action"] = {"$in": actions}
    if actor_values:
        flt["actor"] = {"$in": actor_values}
    target_type_value = _parse_enum(target_type, action_dict.TARGET_TYPES, "target_type")
    if target_type_value:
        flt["target_type"] = target_type_value
    if target_id and target_id.strip():
        flt["target_id"] = target_id.strip()
    window = _parse_time(start_ts, end_ts)
    if window:
        flt["ts"] = window
    outcome_value = _parse_enum(outcome, tuple(o.value for o in AuditOutcome), "outcome")
    if outcome_value:
        flt["outcome"] = outcome_value
    if actor_role and actor_role.strip():
        role = actor_role.strip()
        if not ROLE_CODE_RE.match(role):
            raise BizError(Err.AUD_FILTER_INVALID, f"actor_role 取值非法：{role}")
        flt["actor_role"] = role

    return AuditQuery(mongo_filter=flt, page=page_no, page_size=size,
                      sort=sort_key, degraded=degraded, fmt=export_fmt)


__all__ = [
    "ActionItem", "ActionListResponse", "AuditLogDetail", "AuditLogItem",
    "AuditLogPage", "AuditQuery", "parse_query", "validate_log_id",
]
