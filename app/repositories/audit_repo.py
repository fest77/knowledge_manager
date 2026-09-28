# -*- coding: utf-8 -*-
"""E21 审计日志 `audit_logs` 的数据访问层（模块 10 §2.1 / §2.4）。

**它是全平台唯一持有 `audit_logs` 写入方法的模块**（ER-02 / ER-05）。
`AC-10-04` 用静态检查 + 负例单测钉死这一点：除本文件与
`app/services/audit_service.py` 之外，任何地方出现对 `audit_logs` 的
`insert_one` / `insert_many` 都算违规。

**append-only 在仓储层就已经是结构性的**：本文件**不提供** `update` / `delete`
方法。不是"忘了写"，而是刻意不写——少一个方法就少一种被误用的可能。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from app.core.logging import logger
from app.infra.mongo import mongo

AUDIT_LOGS = "audit_logs"
USERS = "sys_users"

# 四维筛选的排序白名单（§3.6 R-14）：不接受调用方拼字段名
SORTS: dict[str, list[tuple[str, int]]] = {
    "ts desc": [("ts", -1)],
    "ts asc": [("ts", 1)],
}

# 列表页**不含** before/after（§4.3 步 6：快照是详情载荷，20 行 × 2KB 会让首屏变慢）
LIST_PROJECTION: dict[str, int] = {
    "_id": 1, "ts": 1, "actor": 1, "actor_name": 1, "actor_role": 1,
    "action": 1, "target_type": 1, "target_id": 1, "target_name": 1,
    "changed_fields": 1, "reason": 1, "outcome": 1, "ip": 1, "snapshot_state": 1,
}

# 导出用投影：比列表多 before/after，但仍不带 ua（导出列里没有它）
EXPORT_PROJECTION: dict[str, int] = {**LIST_PROJECTION, "before": 1, "after": 1}


async def ensure_indexes() -> None:
    """幂等建索引（§2.4）。

    **绝不建 TTL**（DEC-10-7）：`audit_logs` 是责任凭证，保留策略是**永久**，
    过期删除等于自毁证据链。`AC-10-18` 会断言索引清单里没有 `expireAfterSeconds`。
    """
    db = mongo.require_db()
    logs = db[AUDIT_LOGS]

    await logs.create_index([("ts", -1)], name="ix_ts")
    await logs.create_index([("actor", 1), ("ts", -1)], name="ix_actor_ts")
    await logs.create_index([("target_type", 1), ("target_id", 1)], name="ix_target")
    # 本 Spec 唯一的索引增补：action 是管理员排障的第一入口（§2.4）
    await logs.create_index([("action", 1), ("ts", -1)], name="ix_action_ts")
    # 回放幂等键：**唯一 + 稀疏**（只有降级落盘的记录才带它）
    await logs.create_index([("extra.spool_id", 1)], name="uq_spool_id",
                            unique=True, sparse=True)
    logger.info("审计索引已确保（%d 条）", 5)


async def insert(doc: Mapping[str, Any]) -> str:
    """插入一条审计记录，返回 `_id`。**唯一写入路径**，由 `AuditService` 调用。

    超时不在这里做：`insert_one` 不接受单次超时参数，统一由服务层用
    `asyncio.wait_for` 包一层（见 `AuditService._insert_once`）。
    """
    await mongo.collection(AUDIT_LOGS).insert_one(dict(doc))
    return str(doc["_id"])


async def insert_replayed(docs: Sequence[Mapping[str, Any]]) -> tuple[int, list[Mapping]]:
    """回放补偿文件：批量写入，`ordered=False`，**不做 upsert 覆盖**（§2.1）。

    返回 `(成功条数, 仍然失败的文档)`。`spool_id` 重复（code 11000）算**成功**——
    那正是幂等键要表达的"这条已经进过库了"，不是错误。
    """
    if not docs:
        return 0, []
    coll = mongo.collection(AUDIT_LOGS)
    try:
        result = await coll.insert_many([dict(d) for d in docs], ordered=False)
        return len(result.inserted_ids), []
    except Exception as exc:                                  # noqa: BLE001
        details = getattr(exc, "details", None) or {}
        errors = details.get("writeErrors") or []
        if not errors:                # 连接级失败：整批都没进去，全部留待下次
            logger.warning("回放批量写入失败，%d 条仍留在补偿文件", len(docs))
            return 0, list(docs)
        real_bad = {e.get("index") for e in errors if e.get("code") != 11000}
        inserted = details.get("nInserted", 0)
        failed = [docs[i] for i in sorted(i for i in real_bad if isinstance(i, int))]
        return inserted + (len(errors) - len(real_bad)), failed


async def find_by_id(log_id: str) -> dict[str, Any] | None:
    """按审计编号取全字段（详情用，含 before/after）。"""
    return await mongo.collection(AUDIT_LOGS).find_one({"_id": log_id})


async def is_same_event(doc: Mapping[str, Any]) -> bool:
    """库里那条同号记录**是不是我这一次要写的事件**？

    只用于"插入超时"后的判定：如果 `_id` 撞号但 `ts` 与 `action` 都对得上，
    说明上一次其实写成功了（幂等，视为成功）；对不上则说明撞的是别人的号，
    必须换号重试——否则会把"编号冲突"误判成"已经写过"，**静默丢事件**。
    """
    row = await mongo.collection(AUDIT_LOGS).find_one(
        {"_id": doc["_id"], "ts": doc["ts"], "action": doc["action"]}, {"_id": 1})
    return row is not None


async def count(filters: Mapping[str, Any]) -> int:
    """数一数命中多少条（分页契约要 `total`，导出上限也要它）。"""
    return await mongo.collection(AUDIT_LOGS).count_documents(dict(filters))


async def find_page(filters: Mapping[str, Any], *, skip: int, limit: int,
                    sort: str) -> list[dict[str, Any]]:
    """取一页列表（不带 before/after）。"""
    cursor = (mongo.collection(AUDIT_LOGS)
              .find(dict(filters), LIST_PROJECTION)
              .sort(SORTS[sort]).skip(skip).limit(limit))
    return await cursor.to_list(length=limit)


def iterate(filters: Mapping[str, Any], *, sort: str, batch_size: int) -> Any:
    """返回导出用的游标（`batch_size` 让内存恒定，§3.3）。

    `no_cursor_timeout=True` 是必须的：五万条流式导出可能超过 Mongo 默认的
    10 分钟游标空闲上限，而"游标被服务端回收"会让下载中途断流。
    """
    return (mongo.collection(AUDIT_LOGS)
            .find(dict(filters), EXPORT_PROJECTION, no_cursor_timeout=True)
            .sort(SORTS[sort]).batch_size(batch_size))


async def real_names_of(user_ids: Sequence[str]) -> dict[str, str]:
    """批量取用户姓名（E09 只读，总纲 §5 允许 10 读 `sys_users`）。

    只用于**回填历史记录缺失的 `actor_name`**：`actor_name` 的正道是写入时快照，
    这里只是"快照缺失时尽量补上"，取不到就不补（降级为显示 `actor` 原值）。
    """
    if not user_ids:
        return {}
    cursor = mongo.collection(USERS).find(
        {"_id": {"$in": list(user_ids)}}, {"real_name": 1})
    rows = await cursor.to_list(length=None)
    return {r["_id"]: r.get("real_name") or "" for r in rows}


async def find_user_id_by_username(username: str) -> str | None:
    """按 username 解析 `user_id`（四维筛选的 `actor` 维支持传账号名）。

    `username` 的唯一索引带大小写不敏感 collation（模块 01 建的），这里显式复用
    同一个 `Collation`，否则会出现"登录时 lina 能进、审计里筛 Lina 查不到"。
    """
    from app.repositories.auth_repo import USERNAME_COLLATION   # 循环导入：延迟到调用时

    row = await mongo.collection(USERS).find_one(
        {"username": username}, {"_id": 1}, collation=USERNAME_COLLATION)
    return row["_id"] if row else None


async def purge_before(cutoff_ms: int) -> int:
    """**唯一被认可的删除入口**（模块 10 §2.5）。

    保留策略是"永久"，所以这里不是"清理任务"而是**人工留档后的例外通道**：
    调用方（`scripts/purge_audit.py`）必须先写一条 `audit.export` 把待删范围留档、
    导出到本地归档文件，**然后**才允许调用本函数。

    放在仓储里而不是脚本里，是为了让"能删审计"这件事只有一个可被 code review
    看见的位置——否则 ER-05 的静态检查会被一个"顺手在脚本里 delete_many"绕过。
    """
    result = await mongo.collection(AUDIT_LOGS).delete_many({"ts": {"$lt": cutoff_ms}})
    logger.warning("审计记录已按人工指令删除：before_ts=%s 条数=%s", cutoff_ms,
                   result.deleted_count)
    return result.deleted_count


__all__ = [
    "AUDIT_LOGS", "SORTS", "LIST_PROJECTION", "EXPORT_PROJECTION",
    "ensure_indexes", "insert", "insert_replayed", "find_by_id", "is_same_event",
    "count", "find_page", "iterate", "real_names_of", "find_user_id_by_username",
    "purge_before",
]
