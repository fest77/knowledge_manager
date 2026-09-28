# -*- coding: utf-8 -*-
"""审计服务（模块 10）——全平台**唯一**写 `audit_logs` 的地方（ER-02 / ER-05）。

这份文件里最重要的不是功能，而是**三个不可能被违反的承诺**：

| # | 承诺 | 靠什么保证 |
|---|---|---|
| 1 | `record()` **永不抛异常**（DEC-10-1） | 最外层 `except Exception` 兜底 + 三段式降级，任何分支都返回结果对象 |
| 2 | 审计写入**绝不阻断业务** | 调用方无需 `try/except`，也不得用返回值决定业务响应 |
| 3 | 敏感值**绝不落库** | 写入路径上唯一一次 `SnapshotRedactor.redact()`，失败即丢快照（DEC-10-5） |

第 3 条容易被低估：脱敏只能在**写入路径上**做一次。事后清理意味着敏感值已经落过盘，
而审计库是永久保留的——那时再想删，删掉的就是审计本身。

失败降级链（§4.2）：Mongo 失败 → 立即重试 1 次 → 本地 JSONL 补偿文件 → CRITICAL 日志。
连续 5 次失败进入 60 秒熔断，期间直接走补偿文件（省掉每次业务 500ms 的等待）。
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime
from ipaddress import ip_address
from typing import Any, AsyncIterator, Mapping, Sequence

from fastapi import Request
from pymongo.errors import DuplicateKeyError

from app.audit import actions as action_dict
from app.core.config import settings
from app.core.enums import SnapshotState, worst_snapshot_state
from app.core.errors import BizError, Err
from app.core.logging import logger, trace_id_var
from app.infra.audit_spool import AuditSpool
from app.infra.mongo import mongo
from app.repositories import audit_repo
from app.services.audit_redactor import (TRUNCATED_KEY, RedactionError, SnapshotRedactor,
                                         value_length, value_sha256)

UA_MAX = 256
ID_ATTEMPTS = 3
SYSTEM_ACTOR = "system"

# 导出 CSV 的**固定列序**（§3.3：不随筛选变化，便于外部脚本按列索引解析）
CSV_COLUMNS: tuple[str, ...] = (
    "ts", "actor", "actor_name", "actor_role", "action", "action_name",
    "target_type", "target_id", "target_name", "changed_fields", "reason",
    "outcome", "ip", "before", "after",
)
CSV_BOM = "\ufeff"

# `actor_role` 快照的取值偏好：系统管理员 > 知识管理员 > 提问者 > 业务角色
_ROLE_PRIORITY: dict[str, int] = {"sys_admin": 3, "kb_admin": 2, "asker": 1}


@dataclass(frozen=True, slots=True)
class AuditWriteResult:
    """一次审计写入的结果（§3.5）。**调用方不得用它决定业务响应**（D-01）。"""

    written: bool
    audit_id: str | None = None
    degraded: bool = False
    spool_path: str | None = None
    error_code: str | None = None
    snapshot_state: str = SnapshotState.FULL.value


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """一次补偿文件回放的结果。"""

    claimed: bool
    replayed: int = 0
    failed: int = 0
    archive_path: str | None = None
    error_code: str | None = None


@dataclass
class _Breaker:
    """连续失败熔断器（§4.2）：连续 N 次失败后开门 M 秒，期间直接走补偿文件。"""

    threshold: int = 5
    seconds: float = 60.0
    failures: int = 0
    open_until: float = 0.0

    def is_open(self) -> bool:
        """熔断是否生效。"""
        return time.monotonic() < self.open_until

    def on_success(self) -> None:
        """一次成功即清零：熔断只针对"持续失败"，不是"偶发失败"。"""
        self.failures = 0
        self.open_until = 0.0

    def on_failure(self) -> None:
        """记一次失败；达到阈值就开门。"""
        self.failures += 1
        if self.failures >= self.threshold and not self.is_open():
            self.open_until = time.monotonic() + self.seconds
            logger.error("审计写入连续失败 %d 次，熔断 %.0f 秒（期间直接落补偿文件）",
                         self.failures, self.seconds)


# --------------------------------------------------------------------------- 纯函数
def now_ms() -> int:
    """当前 UTC 毫秒时间戳（`ts` 与 `_id` 日期段的**同一来源**）。"""
    return int(time.time() * 1000)


def local_date_of(ts_ms: int) -> str:
    """`ts` 的本地日历日（`yyyyMMdd`）。

    用本地日而不是 UTC 日：`_id` 的日期段是给人按"某天的记录"定位用的，
    必须与界面上展示的时间同一时区，否则会出现"9 月 23 日晚上查不到当天的记录"。
    """
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%Y%m%d")


def format_ts(ts_ms: int) -> str:
    """把毫秒时间戳格式化成本地 `YYYY-MM-DD HH:MM:SS`（导出列用）。"""
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def client_ip(request: Request) -> str:
    """取来源 IP：`X-Forwarded-For` 里第一个**公网**地址，否则退回直连地址。

    为什么挑"第一个公网"而不是"第一个"：内网部署里 `X-Forwarded-For` 常常是
    `客户端, 内网代理1, 内网代理2`，取第一个会稳定记成网关地址，审计就失去意义；
    而如果整串都是内网地址（无外网出口），退回最后一个仍是合理近似。
    """
    forwarded = request.headers.get("x-forwarded-for") or ""
    hops = [h.strip() for h in forwarded.split(",") if h.strip()]
    for hop in hops:
        try:
            if ip_address(hop).is_global:
                return hop
        except ValueError:
            continue
    if hops:
        return hops[-1]
    return request.client.host if request.client else "unknown"


def primary_role_of(roles: Sequence[Any]) -> str:
    """取用户"当时的角色 code"快照。

    §2.1 把 `actor_role` 记为单个值（`asker` / `kb_admin` / `sys_admin`），
    但一个用户可以同时持有多个角色（含业务角色）。这里取**权限最高**的那个：
    审计要回答的是"他凭什么能这么操作"，取最高角色才不会低估操作人的权限。
    """
    if not roles:
        return "unknown"
    codes = [getattr(r, "code", None) or str(r) for r in roles]
    return sorted(codes, key=lambda c: (-_ROLE_PRIORITY.get(c, 0), c))[0]


def changed_fields_of(before: Mapping[str, Any] | None,
                      after: Mapping[str, Any] | None) -> list[str]:
    """顶层字段名差集（`changed_fields`）。"""
    left = dict(before or {})
    right = dict(after or {})
    keys = set(left) | set(right)
    return sorted(k for k in keys if left.get(k) != right.get(k))


def truncate_value(value: Any) -> dict[str, Any]:
    """把超限的值换成"长度 + 摘要"占位（§2.3 S-03 / S-05 的统一形状）。"""
    return {TRUNCATED_KEY: True, "len": value_length(value), "sha256": value_sha256(value)}


def _encoded_len(*objs: Any) -> int:
    """按统一序列化口径量字节数（S-06：`sort_keys` 保证两次结果一致）。"""
    text = json.dumps(objs, ensure_ascii=False, default=str, sort_keys=True)
    return len(text.encode("utf-8"))


def _leaf_paths(node: Any, path: tuple[Any, ...] = ()) -> list[tuple[tuple[Any, ...], int]]:
    """收集全部叶子的 `(路径, 序列化字节数)`，供按大小截断。"""
    if isinstance(node, dict):
        out: list[tuple[tuple[Any, ...], int]] = []
        for key, value in node.items():
            out.extend(_leaf_paths(value, (*path, key)))
        return out
    if isinstance(node, list):
        out = []
        for index, value in enumerate(node):
            out.extend(_leaf_paths(value, (*path, index)))
        return out
    return [(path, len(json.dumps(node, ensure_ascii=False, default=str)))]


def _set_leaf(root: Any, path: tuple[Any, ...], value: Any) -> None:
    """把 `path` 指向的叶子替换成 `value`（路径由 `_leaf_paths` 产出，必然存在）。"""
    cursor = root
    for step in path[:-1]:
        cursor = cursor[step]
    cursor[path[-1]] = value


def enforce_size_limit(payload: dict[str, Any], limit: int) -> tuple[dict[str, Any], bool]:
    """把 `before`/`after`/`extra` 合计压到 `limit` 字节以内（§2.3 S-03）。

    实现方式是**从最大的叶子开始换占位**，而不是整块丢弃：一次编辑改了 30 个字段、
    其中 1 个是长文本时，丢掉整个快照会连带丢掉另外 29 个关键字段。
    极端情况下（叶子全换完仍超限）才退回整块丢弃。
    """
    if _encoded_len(payload) <= limit:
        return payload, False

    leaves = []
    for key, value in payload.items():
        leaves.extend(_leaf_paths(value, (key,)))
    leaves.sort(key=lambda item: item[1], reverse=True)

    for path, _size in leaves:
        if not path:
            continue
        cursor = payload
        for step in path[:-1]:
            cursor = cursor[step]
        if isinstance(cursor, dict) and cursor.get(TRUNCATED_KEY) is True:
            continue                                   # 已经是占位，别再套一层
        _set_leaf(payload, path, truncate_value(cursor[path[-1]]))
        if _encoded_len(payload) <= limit:
            return payload, True

    for key in ("before", "after", "extra"):
        if payload.get(key):
            payload[key] = truncate_value(payload[key])
            if _encoded_len(payload) <= limit:
                break
    return payload, True


class AuditService:
    """审计服务单例。对外只暴露 §3.5 末表列出的那些方法。"""

    def __init__(self) -> None:
        self._spool = AuditSpool(settings.audit_spool_dir)
        self._breaker = _Breaker(threshold=settings.audit_breaker_threshold,
                                 seconds=float(settings.audit_breaker_seconds))
        self._replay_lock = asyncio.Lock()
        self._seq_date = ""
        self._seq = 0
        self._degraded = False

    # ---------------------------------------------------------------- 生命周期
    async def startup(self) -> None:
        """启动钩子：准备补偿目录并把当日序列对齐到库里的最大值。

        序列对齐这一步不是优化而是**正确性**：`_id` 的后 4 位是进程内计数器，
        重启后会从 0 重来，当天已有的记录会立刻撞号。
        `_next_id()` 里还有一次**惰性对齐**，所以即便忘了调 `startup()`
        （比如脚本、单测直接使用服务），行为仍然是正确的，只是会多一次查询。
        """
        self._spool.prepare()
        await self._sync_sequence(local_date_of(now_ms()))

    def reset(self) -> None:
        """清空进程内状态（测试用例隔离 / 清库后调用），并**重新探测补偿目录**。

        重新探测是必要的：`mark_unwritable()` 可能在运行中把目录标记成不可写，
        而"下次启动前一直显示 degraded"会让 `/health` 从告警退化成背景噪声。
        """
        self._breaker = _Breaker(threshold=settings.audit_breaker_threshold,
                                 seconds=float(settings.audit_breaker_seconds))
        self._seq_date = ""
        self._seq = 0
        self._degraded = False
        self._spool.prepare()

    def health(self) -> dict[str, Any]:
        """`/health` 的审计段：把"审计正在降级"这件事显式暴露出来（§4.2 第 3 段）。"""
        state = "ok"
        if self._degraded or self._breaker.is_open() or not self._spool.writable:
            state = "degraded"
        return {
            "state": state,
            "degraded": self._degraded,
            "breaker_open": self._breaker.is_open(),
            "spool_writable": self._spool.writable,
        }

    async def _sync_sequence(self, date: str) -> None:
        """把当日序号顶到库里已有的最大值（对不上就退回 0，靠撞号重试兜底）。"""
        self._seq_date, self._seq = date, 0
        try:
            row = await mongo.require_db()[audit_repo.AUDIT_LOGS].find_one(
                {"_id": {"$regex": f"^LOG{date}"}}, {"_id": 1}, sort=[("_id", -1)])
        except Exception as exc:                              # noqa: BLE001
            logger.warning("审计序列对齐失败（将靠撞号换号兜底）：%s", exc)
            return
        tail = str(row["_id"])[-4:] if row else ""
        if tail.isdigit():
            self._seq = int(tail)
        logger.info("审计编号序列已对齐到 %s-%04d", self._seq_date, self._seq)

    async def _next_id(self, ts_ms: int) -> str:
        """生成审计编号 `LOG{yyyyMMdd}{4位序列}`（日期段与 `ts` 同源）。

        **惰性对齐**：一旦发现日期变了（跨天、或进程刚重启），先与库对一次当日
        最大序号再自增。少了这一步，重启后的头几条会挨个撞号——不会丢数据
        （有换号重试），但每条要多花两次往返，P95 会从毫秒级掉到几十毫秒。
        """
        date = local_date_of(ts_ms)
        if date != self._seq_date:
            await self._sync_sequence(date)
        self._seq += 1
        return f"LOG{date}{self._seq:04d}"

    # ---------------------------------------------------------------- 写（对内契约）
    async def record(                                    # noqa: PLR0913 — 契约即如此
        self,
        action: str,
        actor: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        reason: str | None = None,
        ip: str | None = None,
        ua: str | None = None,
        outcome: str = "success",
        *,
        actor_role: str | None = None,
        actor_name: str | None = None,
        target_name: str | None = None,
        extra: Mapping[str, Any] | None = None,
        request: Request | None = None,
    ) -> AuditWriteResult:
        """写一条审计。**契约保证：永不抛异常**（§3.5 / DEC-10-1）。

        调用方的两条义务（§7.2 D-01 / D-02）：
        ① 不要 `try/except` 包裹本方法，也不要用返回值决定业务响应；
        ② 必须在**业务数据提交成功之后**调用，避免"审计有、业务无"的幽灵记录。
        """
        state = SnapshotState.FULL.value
        try:
            doc = await self._build(action, actor=actor, target_type=target_type,
                                    target_id=target_id, before=before, after=after,
                                    reason=reason, ip=ip, ua=ua, outcome=outcome,
                                    actor_role=actor_role, actor_name=actor_name,
                                    target_name=target_name, extra=extra, request=request)
            state = str(doc["snapshot_state"])
            return await self._deliver(doc)
        except Exception as exc:                              # noqa: BLE001 — 兜底
            logger.critical("审计写入出现未预期异常（已丢弃该事件）action=%s err=%s",
                            action, exc, exc_info=True)
            return AuditWriteResult(written=False, degraded=True,
                                    error_code=Err.AUD_INTERNAL.code, snapshot_state=state)

    async def record_from_request(self, request: Request, action: str,
                                  **kwargs: Any) -> AuditWriteResult:
        """便捷入口：从 `request.state.user` 自动补齐上下文，其余参数透传（C-03）。"""
        kwargs["request"] = request
        return await self.record(action, **kwargs)

    # ---------------------------------------------------------------- 组装文档
    async def _build(self, action: str, **kw: Any) -> dict[str, Any]:
        """组装一条审计文档：填上下文 → diff → 截断 → 脱敏（永不抛）。"""
        ts = now_ms()
        request: Request | None = kw.get("request")
        meta = action_dict.meta_of(action)
        if meta is None:
            logger.warning("未注册的审计动作，将落库为 unknown.%s（不丢弃事件）", action)

        user = getattr(getattr(request, "state", None), "user", None) if request else None
        actor = kw.get("actor") or (getattr(user, "user_id", None) if user else None)
        missing_actor = actor is None
        actor = actor or SYSTEM_ACTOR

        actor_role = (kw.get("actor_role")
                      or (primary_role_of(getattr(user, "roles", []) or []) if user else None)
                      or "unknown")
        actor_name = kw.get("actor_name") or (getattr(user, "real_name", None) if user else None)
        if kw.get("ip"):
            ip = str(kw["ip"])
        elif request is not None:
            ip = client_ip(request)
        else:
            ip = "unknown"
        ua = kw.get("ua") or (request.headers.get("user-agent") if request else None)
        ua = (str(ua) if ua else None)
        ua = ua[:UA_MAX] if ua else None
        trace_id = (getattr(getattr(request, "state", None), "trace_id", None) if request
                    else None) or trace_id_var.get()

        target_type = kw.get("target_type") or (meta.target_type if meta else "auth")
        target_id = kw.get("target_id") or "-"
        outcome = self._fixed_outcome(action, str(kw.get("outcome") or "success"))

        snapshot = self._snapshot(meta, action, kw.get("before"), kw.get("after"),
                                  kw.get("extra"), kw.get("reason"), missing_actor)
        doc: dict[str, Any] = {
            "_id": await self._next_id(ts),
            "ts": ts,
            "actor": actor,
            "actor_name": actor_name,
            "actor_role": actor_role,
            "action": action_dict.resolve_action(action),
            "target_type": target_type,
            "target_id": target_id,
            "target_name": kw.get("target_name"),
            "before": snapshot["before"],
            "after": snapshot["after"],
            "changed_fields": snapshot["changed_fields"],
            "redacted_keys": snapshot["redacted_keys"],
            "snapshot_state": snapshot["state"].value,
            "reason": kw.get("reason"),
            "outcome": outcome,
            "ip": ip,
            "ua": ua,
            "trace_id": trace_id,
        }
        if snapshot["extra"] is not None:
            doc["extra"] = snapshot["extra"]
        return doc

    @staticmethod
    def _fixed_outcome(action: str, outcome: str) -> str:
        """两个动作的 `outcome` 是**固定**的（§3.5）：登录失败必为 `failure`，鉴权拒绝必为 `denied`。"""
        if action == "auth.login_fail":
            return "failure"
        if action == "auth.denied":
            return "denied"
        return outcome

    def _snapshot(self, meta: Any, action: str, before: Any, after: Any, extra: Any,
                  reason: str | None, missing_actor: bool) -> dict[str, Any]:
        """产出快照四件套：`before` / `after` / `extra` / `changed_fields` / 状态。

        处理顺序（每一步都在写入路径上、且只做一次）：
        diff-only → 脱敏 → 16 KB 截断 → **再脱敏一次**（§2.3 C 要求执行点在截断之后）。
        两次脱敏不是重复劳动：第一遍把大文本降级（否则量体积时会被几 MB 正文拖死），
        第二遍是"入库前最后一道闸"，保证任何路径产出的值都被检查过。
        """
        fields = changed_fields_of(before, after)
        state = SnapshotState.FULL
        payload: dict[str, Any] = {
            "before": dict(before) if isinstance(before, Mapping) else before,
            "after": dict(after) if isinstance(after, Mapping) else after,
            "extra": self._merge_extra(extra, missing_actor),
        }
        if isinstance(payload["before"], dict) and isinstance(payload["after"], dict) \
                and payload["before"] and payload["after"] and fields:
            payload["before"] = {k: v for k, v in payload["before"].items() if k in fields}
            payload["after"] = {k: v for k, v in payload["after"].items() if k in fields}
            state = worst_snapshot_state(state, SnapshotState.DIFF)

        try:
            payload, hits, redact_state = self._redact_payload(payload)
        except RedactionError as exc:
            logger.error("快照脱敏失败，丢弃整个快照（%s）action=%s",
                         Err.AUD_REDACT_FAILED.code, action)
            logger.debug("脱敏失败细节：%s", exc)
            return self._dropped(fields)

        payload, truncated = enforce_size_limit(payload, settings.audit_snapshot_limit_bytes)
        if truncated:
            try:
                payload, more_hits, redact_state = self._redact_payload(payload)
                hits = sorted(set(hits) | set(more_hits))
            except RedactionError:
                logger.error("截断后复检脱敏失败，丢弃整个快照 action=%s",
                             Err.AUD_REDACT_FAILED.code)
                return self._dropped(fields)

        if redact_state != SnapshotState.FULL:
            state = worst_snapshot_state(state, redact_state)
        if truncated:
            state = worst_snapshot_state(state, SnapshotState.TRUNCATED)

        if self._reason_missing(meta, reason):
            logger.error("G-11 类动作缺少变更原因（≥5 字），快照置 dropped action=%s", action)
            return self._dropped(fields, redacted=hits)
        if meta is not None and meta.requires_snapshot and not (before and after):
            logger.error("动作 %s 要求 before/after，调用方未提供，快照置 dropped", action)
            return self._dropped(fields, redacted=hits)

        return {"before": payload["before"], "after": payload["after"],
                "extra": payload["extra"], "changed_fields": fields,
                "redacted_keys": hits, "state": state}

    @staticmethod
    def _merge_extra(extra: Any, missing_actor: bool) -> Any:
        """把 `extra.actor_missing` 标记合进去（§3.5：拿不到操作人时**照写**，但要标注）。"""
        data = dict(extra) if isinstance(extra, Mapping) else {}
        if missing_actor:
            data["actor_missing"] = True
        return data or None

    @staticmethod
    def _reason_missing(meta: Any, reason: str | None) -> bool:
        """G-11 / R-11：要求原因的动作，原因缺失或不足 5 字即算缺失。"""
        if meta is None:
            return False
        required = meta.requires_reason or meta.action in action_dict.REASON_REQUIRED
        if not required:
            return False
        return len((reason or "").strip()) < 5

    @staticmethod
    def _redact_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], SnapshotState]:
        """对三个载荷各脱敏一次，合并 `redacted_keys` 与状态。"""
        hits: list[str] = []
        state = SnapshotState.FULL
        cleaned: dict[str, Any] = {}
        for key, value in payload.items():
            new_value, keys, one_state = SnapshotRedactor.redact(value)
            cleaned[key] = new_value
            hits.extend(keys)
            state = worst_snapshot_state(state, one_state)
        return cleaned, sorted(set(hits)), state

    @staticmethod
    def _dropped(fields: list[str], redacted: list[str] | None = None) -> dict[str, Any]:
        """`dropped` 意味着**快照为空**（§3.2：此时 `before`/`after` 返回 `null`）。"""
        return {"before": None, "after": None, "extra": None, "changed_fields": fields,
                "redacted_keys": redacted or [], "state": SnapshotState.DROPPED}

    # ---------------------------------------------------------------- 投递（降级链）
    async def _deliver(self, doc: dict[str, Any]) -> AuditWriteResult:
        """三段式投递：写库 → 重试 1 次 → 补偿文件。任何分支都返回、都不抛。"""
        state = str(doc["snapshot_state"])
        if self._breaker.is_open():
            logger.warning("审计写入处于熔断期，直接落补偿文件 action=%s", doc["action"])
            return await self._to_spool(doc, Err.AUD_WRITE_BREAKER.code, state)

        try:
            await self._insert(doc, allow_own=False)
            self._breaker.on_success()
            return AuditWriteResult(written=True, audit_id=str(doc["_id"]),
                                    snapshot_state=state)
        except Exception as first:                            # noqa: BLE001
            logger.warning("审计写入失败，立即重试 1 次 action=%s err=%s",
                           doc["action"], first)

        await asyncio.sleep(settings.audit_retry_backoff_ms / 1000)
        try:
            await self._insert(doc, allow_own=True)
            self._breaker.on_success()
            return AuditWriteResult(written=True, audit_id=str(doc["_id"]),
                                    snapshot_state=state)
        except Exception as second:                           # noqa: BLE001
            self._breaker.on_failure()
            logger.error("审计重试仍失败，转补偿文件 action=%s err=%s", doc["action"], second)
            return await self._to_spool(doc, Err.AUD_WRITE_FAILED.code, state)

    async def _insert(self, doc: dict[str, Any], *, allow_own: bool) -> None:
        """带超时的单次插入，并在撞号时区分"自己的那条"与"别人的号"。

        | 场景 | 判定 | 动作 |
        |---|---|---|
        | 编号被**别人**占了（重启后序列回退、多进程并发） | `ts`/`action` 对不上 | 换号重试 |
        | **自己**上一次其实写成功了（插入超时，但服务端已落库） | `ts`/`action` 都对得上 | 视为成功（幂等） |

        `allow_own` 控制第二种判定是否启用：只有**第一次尝试失败后的重试**才可能是
        "自己已经写进去了"，第一次尝试撞号必然是撞了别人。
        这个区分很重要——早先的版本把"重试时撞号"一律当成功，于是在序列严重
        落后时会**静默丢事件**（压测里实测到过）。
        """
        timeout = settings.audit_insert_timeout_ms / 1000
        for attempt in range(ID_ATTEMPTS):
            try:
                await asyncio.wait_for(audit_repo.insert(doc), timeout=timeout)
                return
            except DuplicateKeyError:
                if allow_own and await audit_repo.is_same_event(doc):
                    logger.warning("审计 %s 已存在且事件一致，判定为上次写入实际成功",
                                   doc["_id"])
                    return
                if attempt == ID_ATTEMPTS - 1:
                    raise
                logger.warning("审计编号 %s 已被占用，换号重试", doc["_id"])
                doc["_id"] = await self._next_id(int(doc["ts"]))

    async def _to_spool(self, doc: dict[str, Any], error_code: str,
                        state: str) -> AuditWriteResult:
        """第 2 / 3 段降级：落本地 JSONL；连它也写不进去才是真的丢事件（记 CRITICAL）。"""
        doc["extra"] = {**(doc.get("extra") or {}), "spool_id": doc["_id"]}
        try:
            path = self._spool.append(doc)
        except Exception as exc:                              # noqa: BLE001
            self._degraded = True
            self._spool.mark_unwritable()
            logger.critical("审计补偿文件不可写，事件丢失 action=%s trace_id=%s err=%s",
                            doc["action"], doc["trace_id"], exc)
            return AuditWriteResult(written=False, degraded=True,
                                    error_code=Err.AUD_SPOOL_UNWRITABLE.code,
                                    snapshot_state=state)
        self._degraded = True
        logger.error("审计已降级到补偿文件（%s）action=%s trace_id=%s",
                     error_code, doc["action"], doc["trace_id"])
        return AuditWriteResult(written=False, degraded=True, spool_path=str(path),
                                error_code=error_code, snapshot_state=state)

    # ---------------------------------------------------------------- 回放
    async def replay_spool(self) -> ReplayResult:
        """回放补偿文件（启动钩子 + 每 5 分钟一次）；`spool_id` 唯一索引做幂等。

        并发保护用进程内锁（§7.2）：跨进程冲突由 `AUD-3003` 表达，不抛异常。
        """
        if self._replay_lock.locked():
            logger.warning("补偿文件回放正在进行，本次跳过（%s）", Err.AUD_REPLAY_RUNNING.code)
            return ReplayResult(claimed=False, error_code=Err.AUD_REPLAY_RUNNING.code)

        async with self._replay_lock:
            try:
                archive, docs = self._spool.claim()
                if archive is None:
                    return ReplayResult(claimed=False)
                inserted, failed = await audit_repo.insert_replayed(docs)
                if failed:
                    self._spool.restore(failed)
                logger.info("补偿文件回放：归档=%s 命中=%d 入库=%d 仍失败=%d",
                            archive.name, len(docs), inserted, len(failed))
                if inserted:
                    self._degraded = False
                    await self.record(
                        "audit.spool_replay", actor=SYSTEM_ACTOR, target_type="audit",
                        target_id="-", after={"replayed": inserted, "failed": len(failed)},
                        extra={"archive": archive.name, "claimed": len(docs)})
                return ReplayResult(claimed=True, replayed=inserted, failed=len(failed),
                                    archive_path=str(archive))
            except Exception as exc:                          # noqa: BLE001
                logger.exception("补偿文件回放失败：%s", exc)
                return ReplayResult(claimed=True, error_code=Err.AUD_INTERNAL.code)

    # ---------------------------------------------------------------- 查
    async def query(self, filters: Mapping[str, Any], page: int, page_size: int,
                    *, sort: str = "ts desc", degraded: bool = False) -> dict[str, Any]:
        """四维筛选 + 分页（§3.1）。

        ★ 查询失败**绝不返回空列表**（R-13）：`AUD-4004` 必须显式抛出去。
        "最近一周没有权限变更"与"查询挂了"在响应上一模一样，后者会让管理员
        得出"没有人动过权限"的错误结论——沉默的错误比 500 危险得多。
        """
        skip = (page - 1) * page_size
        try:
            total = await audit_repo.count(filters)
            rows = await audit_repo.find_page(filters, skip=skip, limit=page_size, sort=sort)
        except Exception as exc:                              # noqa: BLE001
            logger.exception("审计查询失败 filters=%s", dict(filters))
            raise BizError(Err.AUD_QUERY_FAILED, f"审计查询失败：{exc}") from exc
        items = [self.to_list_item(row) for row in rows]
        await self._fill_actor_names(items)
        return {"items": items, "total": total, "page": page, "page_size": page_size,
                "degraded": degraded}

    async def count(self, filters: Mapping[str, Any]) -> int:
        """数命中条数（导出上限的判据）。

        ★ 失败**必须显式报 `AUD-4004`**（R-13）：导出前算不出条数却继续流式下载，
        用户会拿到一个"看起来正常但其实缺数据"的文件——这是最难被发现的错误形态。
        """
        try:
            return await audit_repo.count(filters)
        except Exception as exc:                              # noqa: BLE001
            logger.exception("审计计数失败 filters=%s", dict(filters))
            raise BizError(Err.AUD_QUERY_FAILED, f"审计查询失败：{exc}") from exc

    async def detail(self, log_id: str) -> dict[str, Any]:
        """单条详情（含 `before`/`after`）；不存在抛 `AUD-3001`。"""
        try:
            row = await audit_repo.find_by_id(log_id)
        except Exception as exc:                              # noqa: BLE001
            logger.exception("审计详情查询失败 log_id=%s", log_id)
            raise BizError(Err.AUD_QUERY_FAILED, f"审计查询失败：{exc}") from exc
        if row is None:
            raise BizError(Err.AUD_NOT_FOUND, f"审计记录不存在：{log_id}")
        item = self.to_list_item(row)
        item.update({
            "before": row.get("before"),
            "after": row.get("after"),
            "redacted_keys": list(row.get("redacted_keys") or []),
            "extra": row.get("extra"),
            "trace_id": row.get("trace_id"),
            "ua": row.get("ua"),
        })
        return item

    @staticmethod
    def action_name_of(action: str) -> str:
        """动作的中文名；未注册动作显式标出来，让"字典缺项"在界面上可见。"""
        meta = action_dict.meta_of(action)
        if meta is not None:
            return meta.name
        if action.startswith(action_dict.UNKNOWN_PREFIX):
            return f"未注册动作：{action[len(action_dict.UNKNOWN_PREFIX):]}"
        return action

    def to_list_item(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """把库里的文档转成列表级视图（不含 `before`/`after`）。"""
        action = str(row.get("action") or "")
        return {
            "audit_id": row["_id"],
            "ts": row.get("ts") or 0,
            "actor": row.get("actor") or SYSTEM_ACTOR,
            "actor_name": row.get("actor_name") or "",
            "actor_role": row.get("actor_role") or "unknown",
            "action": action,
            "action_name": self.action_name_of(action),
            "target_type": row.get("target_type") or "",
            "target_id": row.get("target_id") or "-",
            "target_name": row.get("target_name") or "",
            "changed_fields": list(row.get("changed_fields") or []),
            "reason": row.get("reason") or "",
            "outcome": row.get("outcome") or "success",
            "ip": row.get("ip") or "unknown",
            "snapshot_state": row.get("snapshot_state") or SnapshotState.FULL.value,
        }

    async def _fill_actor_names(self, items: list[dict[str, Any]]) -> None:
        """回填缺失的 `actor_name`（§1.4 对 02 的**唯一**反向读取，且是只读）。

        优先用 02 将来提供的 `UserService`；02 尚未落地时退回直读 `sys_users`
        （总纲 §5 允许模块 10 读 E09）。整段失败只记 WARN——姓名是**展示冗余**，
        不能让"补一个名字"把审计查询搞挂。
        """
        missing = sorted({i["actor"] for i in items
                          if not i["actor_name"] and i["actor"] != SYSTEM_ACTOR})
        if not missing:
            return
        try:
            names = await self._lookup_names(missing)
        except Exception as exc:                              # noqa: BLE001
            logger.warning("回填 actor_name 失败（降级为显示 actor 原值）：%s", exc)
            return
        for item in items:
            if not item["actor_name"] and item["actor"] in names:
                item["actor_name"] = names[item["actor"]]

    @staticmethod
    async def _lookup_names(user_ids: list[str]) -> dict[str, str]:
        """按模块 02 是否已落地选择姓名来源（函数级延迟导入，破解 02 ↔ 10 循环）。"""
        try:
            from app.services import org_service           # 02 模块
        except ImportError:
            return await audit_repo.real_names_of(user_ids)
        return await org_service.real_names_of(user_ids)

    def list_actions(self) -> list[dict[str, object]]:
        """动作字典（`GET /api/v1/audit/actions` 的数据源，§3.4）。"""
        return action_dict.list_action_meta()

    # ---------------------------------------------------------------- 导出
    def iter_export_rows(self, filters: Mapping[str, Any], *, sort: str,
                         batch_size: int) -> Any:
        """导出用的游标（`batch_size=500` 让内存恒定，§3.3）。"""
        return audit_repo.iterate(filters, sort=sort, batch_size=batch_size)

    def csv_header(self) -> bytes:
        """CSV 表头（**UTF-8 带 BOM**：否则 Excel 打开中文列名乱码）。"""
        return (CSV_BOM + ",".join(CSV_COLUMNS) + "\r\n").encode("utf-8")

    def csv_row(self, row: Mapping[str, Any]) -> bytes:
        """一行 CSV：公式注入防护 + 固定列序。"""
        item = self.to_list_item(row)
        cells = [
            format_ts(int(item["ts"])), item["actor"], item["actor_name"],
            item["actor_role"], item["action"], item["action_name"],
            item["target_type"], item["target_id"], item["target_name"],
            "|".join(item["changed_fields"]), item["reason"], item["outcome"], item["ip"],
            self._json_cell(row.get("before")), self._json_cell(row.get("after")),
        ]
        return (",".join(self._csv_cell(c) for c in cells) + "\r\n").encode("utf-8")

    @staticmethod
    def _csv_cell(value: Any) -> str:
        """单元格转义。

        以 `=` `+` `-` `@` `\\t` `\\r` 开头的值**前置一个单引号**：审计里的
        `target_name`（文档标题）与 `reason` 都是用户可控文本，不转义的话
        导出的 CSV 在 Excel 里会被当公式执行（CSV injection）。
        """
        text = "" if value is None else str(value)
        if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
            text = "'" + text
        if any(ch in text for ch in (',', '"', "\n", "\r")):
            text = '"' + text.replace('"', '""') + '"'
        return text

    @staticmethod
    def _json_cell(value: Any) -> str:
        """`before`/`after` 在 CSV 里压成单列 JSON 文本（§3.3）。"""
        if value is None:
            return ""
        return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)

    def export_meta(self, filters: Mapping[str, Any], fmt: str) -> dict[str, Any]:
        """`audit.export` 留痕的内容（筛选条件 + 条数 + 格式）。"""
        return {"filters": self._plain(filters), "format": fmt}

    @staticmethod
    def _plain(value: Any) -> Any:
        """把 Mongo 过滤器（含 `$in` 等）转成可 JSON 化的普通结构，便于入审计。"""
        try:
            return json.loads(json.dumps(value, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            return str(value)

    async def export_audit(self, request: Request | None, filters: Mapping[str, Any],
                           fmt: str, rows: int) -> None:
        """导出结束后的留痕（R-15）。**失败不阻断下载**——`record()` 本就不抛。"""
        await self.record(
            "audit.export",
            target_type="audit", target_id="-",
            after={"rows": rows, **self.export_meta(filters, fmt)},
            extra={"rows": rows, "format": fmt},
            request=request)

    def export_stream(self, filters: Mapping[str, Any], fmt: str, *,
                      request: Request | None = None,
                      rows: int = 0) -> AsyncIterator[bytes]:
        """流式导出（§3.3）：CSV 带 BOM + 公式转义；JSON 为数组；结束时写 `audit.export`。

        返回的是**异步生成器**而非协程：调用方直接交给 `StreamingResponse`，
        逐批产出字节，5 万条也不会把内存抬起来。
        """
        return self._stream(filters, fmt, request=request, rows=rows)

    async def _stream(self, filters: Mapping[str, Any], fmt: str, *,
                      request: Request | None, rows: int) -> AsyncIterator[bytes]:
        try:
            if fmt == "json":
                yield b"["
                first = True
                async for row in self._cursor(filters):
                    item = self.to_list_item(row)
                    item["before"] = row.get("before")
                    item["after"] = row.get("after")
                    text = json.dumps(item, ensure_ascii=False, default=str)
                    yield (b"" if first else b",") + text.encode("utf-8")
                    first = False
                yield b"]"
            else:
                yield self.csv_header()
                async for row in self._cursor(filters):
                    yield self.csv_row(row)
        except Exception as exc:                              # noqa: BLE001
            logger.exception("审计导出生成失败：%s", exc)
            raise BizError(Err.AUD_EXPORT_FAILED, f"导出生成失败：{exc}") from exc
        finally:
            await self.export_audit(request, filters, fmt, rows)

    async def _cursor(self, filters: Mapping[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """把 pymongo 异步游标包成 `async for`（一层适配，隔离具体驱动）。"""
        cursor = self.iter_export_rows(filters, sort="ts desc",
                                       batch_size=settings.audit_export_batch)
        async for row in cursor:
            yield row


audit_service = AuditService()

__all__ = [
    "AuditService", "AuditWriteResult", "ReplayResult", "audit_service",
    "CSV_COLUMNS", "CSV_BOM", "changed_fields_of", "client_ip", "enforce_size_limit",
    "format_ts", "local_date_of", "now_ms", "primary_role_of", "truncate_value",
]
