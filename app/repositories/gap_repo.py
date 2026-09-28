# -*- coding: utf-8 -*-
"""模块 08 的数据访问层：**E19 知识缺口 `knowledge_gaps` 的唯一写入者**（ER-02）。

## 幂等性的第一重保证就在这个文件里

`frequency` 用 **`$set` 重算覆盖**，绝不用 `$inc` 自增：
`$inc` 一旦重跑就翻倍（"连续触发 3 次聚合，频次变成 3 倍"），
而重算覆盖天然幂等 —— 这是 AC-08-05 的实现基础。

## 人工状态一律 `$setOnInsert`

`status` / `converted_*` / `ignored_*` 只在**首次插入**时写。
聚合任务永远不能把"已被忽略的缺口"改回 `open`，也不能抹掉转建留痕：
**人工决策优先于机器重算**（Spec §2.4 的"人工状态保护"）。

## 三张集合的读写边界

| 集合 | 本模块权限 |
|---|---|
| `knowledge_gaps`（E19） | **唯一写入者** |
| `qa_logs`（E15） | **只读**（ER-06：06 独占写） |
| `kb_documents` / `kb_import_tasks` | **只读**（经 03/04 的 Service 写，ER-02 / AC-08-12） |
"""
from __future__ import annotations

import time
from typing import Any, Mapping, Sequence

from app.core.enums import GapStatus
from app.core.logging import logger
from app.infra.mongo import mongo

GAPS = "knowledge_gaps"
GAP_ID_PREFIX = "GAP"
GAP_ID_WIDTH = 6


async def ensure_indexes() -> None:
    """建 E19 的 7 个索引（Spec §2.2）。"""
    collection = mongo.require_db()[GAPS]
    # 合并计数的基石：同类问题只有一条记录
    await collection.create_index("normalized_key", name="uq_normalized_key",
                                  unique=True)
    # 清单默认排序（frequency desc）
    await collection.create_index([("status", 1), ("frequency", -1)],
                                  name="ix_status_freq")
    # 原型「全部部门 ▾」筛选
    await collection.create_index([("dept_id", 1), ("status", 1),
                                   ("frequency", -1)], name="ix_dept_status_freq")
    # 「最该补」排序：max_score 越接近阈值，说明离答对只差一点
    await collection.create_index([("max_score", 1)], name="ix_max_score")
    # 「最近出现」排序 + 清理 frequency=0 的扫描
    await collection.create_index([("last_seen_at", -1)], name="ix_last_seen")
    # 反查"某篇文档是由哪个缺口转建的"
    await collection.create_index("converted_doc_id", name="ix_converted_doc",
                                  sparse=True)
    # 跨部门聚合视图（group_by=question）
    await collection.create_index([("status", 1), ("normalized_text", 1)],
                                  name="ix_status_normtext", sparse=True)
    logger.info("知识缺口索引已确保（%s）", GAPS)


async def next_gap_id() -> str:
    """缺口编号 `GAP{6位序列}`（不按日期分段：清单上不展示编号的可读性）。"""
    row = await mongo.collection(GAPS).find_one(
        {"_id": {"$regex": f"^{GAP_ID_PREFIX}\\d{{{GAP_ID_WIDTH}}}$"}}, {"_id": 1},
        sort=[("_id", -1)])
    seq = int(str(row["_id"])[len(GAP_ID_PREFIX):]) + 1 if row else 1
    return f"{GAP_ID_PREFIX}{seq:0{GAP_ID_WIDTH}d}"


async def next_gap_ids(count: int) -> list[str]:
    """**一次**分配 `count` 个连续编号（批量插入必须用它）。

    ⚠️ 不能在一个循环里反复调 `next_gap_id()`：它查的是库里的最大 `_id`，
    而这一批**还没插进去** —— 于是每个候选都拿到同一个 `GAP000001`，
    第二条插入时撞 `_id` 唯一索引，整轮聚合失败（我踩过这个坑：
    症状是"一次只产出 1 条缺口，第二条就报 DuplicateKeyError"）。
    """
    if count <= 0:
        return []
    first = await next_gap_id()
    start = int(first[len(GAP_ID_PREFIX):])
    return [f"{GAP_ID_PREFIX}{start + i:0{GAP_ID_WIDTH}d}" for i in range(count)]


# =========================================================================== 写
async def upsert_batch(records: Sequence[Mapping[str, Any]], *, ts_ms: int) -> int:
    """批量重算覆盖写入（`normalized_key` 唯一索引 + `upsert=True`）。

    每条的写入口径：
    - **`$set`**：`frequency` / `max_score` / `sample_log_ids` / `first_seen_at` /
      `last_seen_at` / `question` / `normalized_text` / `suggested_category_id` /
      `dept_id` / `last_aggregated_at`（机器算出来的，每轮重算）
    - **`$setOnInsert`**：`status=open` / `created_at`（**人工状态不被覆盖**）
    """
    if not records:
        return 0
    collection = mongo.collection(GAPS)
    written = 0
    for record in records:
        payload = dict(record)
        gap_id = payload.pop("_id", None)
        normalized_key = payload.pop("normalized_key")
        for protected in ("status", "converted_doc_id", "converted_at",
                          "converted_by", "ignored_at", "ignored_by",
                          "ignore_reason"):
            payload.pop(protected, None)
        payload["last_aggregated_at"] = ts_ms
        result = await collection.update_one(
            {"normalized_key": normalized_key},
            {"$set": payload,
             "$setOnInsert": {"_id": gap_id or await next_gap_id(),
                              "normalized_key": normalized_key,
                              "status": GapStatus.OPEN.value,
                              "converted_doc_id": None, "converted_at": None,
                              "converted_by": None, "ignored_at": None,
                              "ignored_by": None, "ignore_reason": None,
                              "created_at": ts_ms}},
            upsert=True)
        written += 1 if (result.upserted_id or result.modified_count) else 0
    return written


async def remove_stale_open(*, key_keep: Sequence[str], ts_ms: int) -> int:
    """清理"窗口内频次归零"的 **open** 缺口（AC-08-22）。

    ⚠️ **只删 `open`**：`converted` / `ignored` 是人工决策痕迹，永久保留
    （Step 1 §7 的保留策略）。把人工决策过的记录删掉，等于把"补了没有"的
    追踪线索一起删了。
    """
    result = await mongo.collection(GAPS).delete_many(
        {"status": GapStatus.OPEN.value,
         "normalized_key": {"$nin": list(key_keep)},
         "last_seen_at": {"$lt": ts_ms}})
    if result.deleted_count:
        logger.info("已清理 %d 条频次归零的 open 缺口", result.deleted_count)
    return int(result.deleted_count)


async def get_gap(gap_id: str) -> dict[str, Any] | None:
    """按编号取缺口。"""
    return await mongo.collection(GAPS).find_one({"_id": gap_id})


async def get_by_key(normalized_key: str) -> dict[str, Any] | None:
    """按归一化键取（幂等/复现判定用）。"""
    return await mongo.collection(GAPS).find_one({"normalized_key": normalized_key})


async def list_gaps(*, status: str | None = None, dept_id: str | None = None,
                    keyword: str = "", category_id: str | None = None,
                    min_frequency: int = 1, sort: str = "frequency_desc",
                    page: int = 1, page_size: int = 20
                    ) -> tuple[list[dict[str, Any]], int]:
    """缺口清单（默认 `open` + 三级排序，Spec §3.1 R-02）。"""
    query: dict[str, Any] = {}
    if status:
        query["status"] = status
    if dept_id:
        query["dept_id"] = dept_id
    if category_id:
        query["suggested_category_id"] = category_id
    if min_frequency > 1:
        query["frequency"] = {"$gte": int(min_frequency)}
    if keyword:
        import re

        pattern = {"$regex": re.escape(keyword), "$options": "i"}
        query["$or"] = [{"question": pattern}, {"normalized_text": pattern}]
    collection = mongo.collection(GAPS)
    total = await collection.count_documents(query)
    cursor = (collection.find(query).sort(_sort_spec(sort))
              .skip((page - 1) * page_size).limit(page_size))
    return await cursor.to_list(length=page_size), total


async def list_all(*, status: str | None = None, dept_id: str | None = None,
                   keyword: str = "", category_id: str | None = None,
                   min_frequency: int = 1, sort: str = "frequency_desc",
                   limit: int | None = None) -> list[dict[str, Any]]:
    """不分页取（导出与跨部门聚合视图用）。"""
    rows, _ = await list_gaps(status=status, dept_id=dept_id, keyword=keyword,
                              category_id=category_id, min_frequency=min_frequency,
                              sort=sort, page=1, page_size=limit or 100000)
    return rows


def _sort_spec(sort: str) -> list[tuple[str, int]]:
    """排序口径（默认三级排序：同频次时结果**稳定**，翻页不会重复/漏项）。"""
    mapping = {
        "frequency_desc": [("frequency", -1), ("max_score", -1), ("last_seen_at", -1)],
        "frequency_asc": [("frequency", 1), ("max_score", -1), ("last_seen_at", -1)],
        "max_score_asc": [("max_score", 1), ("frequency", -1), ("last_seen_at", -1)],
        "max_score_desc": [("max_score", -1), ("frequency", -1), ("last_seen_at", -1)],
        "last_seen_desc": [("last_seen_at", -1), ("frequency", -1)],
    }
    return mapping.get(sort, mapping["frequency_desc"])


async def mark_converted(gap_id: str, *, doc_id: str, actor: str, ts_ms: int) -> int:
    """转建成功后的状态回写（**E19 是本模块的表，可直写**）。"""
    result = await mongo.collection(GAPS).update_one(
        {"_id": gap_id},
        {"$set": {"status": GapStatus.CONVERTED.value, "converted_doc_id": doc_id,
                  "converted_at": ts_ms, "converted_by": actor,
                  "last_aggregated_at": ts_ms}})
    return result.modified_count


async def mark_ignored(gap_id: str, *, actor: str, reason: str, ts_ms: int) -> int:
    """忽略缺口（留痕；与 `converted_*` 对称，便于审计对齐）。"""
    result = await mongo.collection(GAPS).update_one(
        {"_id": gap_id},
        {"$set": {"status": GapStatus.IGNORED.value, "ignored_at": ts_ms,
                  "ignored_by": actor, "ignore_reason": reason[:200],
                  "last_aggregated_at": ts_ms}})
    return result.modified_count


async def count_by_status() -> dict[str, int]:
    """各状态的条数（清单 summary 用）。"""
    out: dict[str, int] = {}
    for status in GapStatus:
        out[status.value] = await mongo.collection(GAPS).count_documents(
            {"status": status.value})
    return out


async def total_frequency() -> int:
    """频次合计（清单 summary）。"""
    # ⚠️ pymongo 的 **异步** `aggregate()` 返回的是**协程**，必须先 await 拿到游标
    # 再 `to_list()`。直接 `.aggregate().to_list()` 会报
    # `'coroutine' object has no attribute 'to_list'`（我在模块 10 踩过一次，
    # 这里又踩了一次——所以把这条写进注释）
    cursor = await mongo.collection(GAPS).aggregate([
        {"$group": {"_id": None, "total": {"$sum": "$frequency"}}}])
    rows = await cursor.to_list(length=1)
    return int(rows[0]["total"]) if rows else 0


async def last_aggregated_at() -> int:
    """最近一次聚合时间（清单的 `aggregated_at`，R-07）。"""
    row = await mongo.collection(GAPS).find_one(
        {}, {"last_aggregated_at": 1}, sort=[("last_aggregated_at", -1)])
    return int((row or {}).get("last_aggregated_at") or 0)


async def all_dept_ids() -> list[str]:
    """出现过的部门集合（原型「全部部门 ▾」的选项来源）。"""
    return [d for d in await mongo.collection(GAPS).distinct("dept_id") if d]


async def count_gaps(status: str | None = None) -> int:
    """缺口条数（可按状态筛；测试断言用）。"""
    query = {"status": status} if status else {}
    return await mongo.collection(GAPS).count_documents(query)


async def drop_collection() -> None:
    """清空集合（仅测试路径调用）。"""
    await mongo.require_db().drop_collection(GAPS)


def now_ms() -> int:
    """当前毫秒时间戳（本模块统一用它，避免秒/毫秒混用）。"""
    return int(time.time() * 1000)


__all__ = [
    "GAPS", "GAP_ID_PREFIX", "ensure_indexes", "next_gap_id", "upsert_batch",
    "remove_stale_open", "get_gap", "get_by_key", "list_gaps", "list_all",
    "mark_converted", "mark_ignored", "count_by_status", "total_frequency",
    "last_aggregated_at", "all_dept_ids", "count_gaps", "drop_collection", "now_ms",
]
