# -*- coding: utf-8 -*-
"""模块 09 的指标桶存储：**E20 `metric_buckets` 的唯一写入者**（ER-07 / ER-02）。

## 为什么写入是本模块最热的路径

每一轮问答都要写 4~6 个桶（全局分钟桶、全局 UV 日桶、FAQ 桶、延时桶、文档桶）。
所以这里有三条硬性取舍：

| 取舍 | 为什么 |
|---|---|
| `_id = "{bucket_type}:{bucket_key}:{ts}"` | 三段组合即业务主键，`upsert` 天然不产生重复桶；**少建一个索引就少一次维护** |
| 全部用 `$inc` / `$addToSet`（**没有读-改-写**） | 原子增量，并发安全；换成"查出来加一再写回"会在并发下丢计数 |
| **`1d` 桶的 `uv_set` 用 `$addToSet`** | UV 的定义是"去重人数"，`$inc` 会把同一个人的多次提问重复计数（AD-12 明令禁止） |

## TTL 的正确写法（Mongo 的 TTL 不能按字段值分支）

`1m`/`1h` 桶保留 30 天、`1d` 桶**永久**。Mongo 的 TTL 索引只能基于一个日期字段，
所以用「**双字段 + 部分索引**」：写 `expire_at = bucket_ts + 30天`，
`1d` 桶**不写 `expire_at`**，部分索引的范围也只覆盖 `1m`/`1h`。
"""
from __future__ import annotations

import time
from typing import Any, Mapping, Sequence

from app.core.enums import MetricGranularity
from app.core.logging import logger
from app.infra.mongo import mongo

BUCKETS = "metric_buckets"
# `1m` / `1h` 桶的保留天数（`1d` 桶永久）
SHORT_GRANULARITY_TTL_DAYS = 30


def bucket_id(bucket_type: str, bucket_key: str, ts: int) -> str:
    """桶主键：`{bucket_type}:{bucket_key}:{ts}`（ts 是**对齐后**的 epoch 秒）。"""
    return f"{bucket_type}:{bucket_key}:{int(ts)}"


async def ensure_indexes() -> None:
    """建 E20 的索引（Spec §2.4）。TTL 失败**不阻止启动**（`MET-5003`）。"""
    collection = mongo.require_db()[BUCKETS]
    # 看板主查询：按类型 + 粒度 + 时间范围取桶序列
    await collection.create_index(
        [("bucket_type", 1), ("granularity", 1), ("bucket_ts", 1)],
        name="ix_metric_query")
    # 单维度下钻（某 FAQ / 某文档的历史曲线）
    await collection.create_index([("bucket_type", 1), ("bucket_key", 1)],
                                  name="ix_metric_drill")
    try:
        # 见模块头：部分索引让 TTL 只作用于 1m/1h
        await collection.create_index(
            "expire_at", expireAfterSeconds=0, name="ttl_metric_short",
            partialFilterExpression={
                "granularity": {"$in": [MetricGranularity.M1.value,
                                        MetricGranularity.H1.value]}})
    except Exception as exc:                                # noqa: BLE001
        logger.error("指标桶 TTL 索引创建失败（不阻止启动，code=MET-5003）：%s", exc)
    logger.info("指标桶索引已确保（%s）", BUCKETS)


# =========================================================================== 写
async def inc_bucket(*, bucket_type: str, bucket_key: str, ts: int,
                     granularity: str, increments: Mapping[str, int],
                     add_uv: Sequence[str] = (), ts_ms: int) -> None:
    """增量写一个桶（`$inc` 可加指标 + `$addToSet` UV 集合）。**幂等不由这里保证**：
    指标是"发生即计数"的，重放同一次问答会重复计数——由调用方（06）保证只投递一次。"""
    update: dict[str, Any] = {
        "$inc": {f"metrics.{k}": int(v) for k, v in increments.items() if v},
        "$set": {"updated_at": ts_ms},
        # ★ `bucket_type` / `bucket_key` / `granularity` / `bucket_ts` 必须**落进文档**：
        # 查询侧走的是 `ix_metric_query`（bucket_type + granularity + bucket_ts），
        # 只在 `_id` 里拼这些段是**查不出来**的——`_id` 是字符串，索引字段是独立字段。
        # 少了这一步的症状是"写进去了但看板全零"，而且 `get_bucket`（按 `_id` 查）
        # 还能查到，排查时极容易走错方向（Spec §4.3 的示例代码就带了这几个字段）。
        "$setOnInsert": {"created_at": ts_ms, "bucket_type": bucket_type,
                         "bucket_key": bucket_key, "granularity": granularity,
                         "bucket_ts": int(ts)},
    }
    if granularity in (MetricGranularity.M1.value, MetricGranularity.H1.value):
        # 只有 1m/1h 桶写 `expire_at`（1d 桶永久）
        update["$set"]["expire_at"] = _expire_document(ts)
    if add_uv:
        update["$addToSet"] = {"uv_set": {"$each": list(dict.fromkeys(add_uv))}}
    await mongo.collection(BUCKETS).update_one(
        {"_id": bucket_id(bucket_type, bucket_key, ts)}, update, upsert=True)


def _expire_document(ts_seconds: int) -> Any:
    """TTL 索引用 `expireAfterSeconds=0`，所以字段值必须是**过期时刻**本身。"""
    from datetime import datetime, timedelta, timezone

    moment = datetime.fromtimestamp(ts_seconds, tz=timezone.utc) + \
        timedelta(days=SHORT_GRANULARITY_TTL_DAYS)
    return moment


async def replace_bucket(*, bucket_type: str, bucket_key: str, ts: int,
                         granularity: str, metrics: Mapping[str, int],
                         uv_set: Sequence[str], ts_ms: int) -> None:
    """**覆盖写**一个桶（汇总任务用它）。

    ⚠️ 汇总必须用 `$set` 覆盖而不是 `$inc`：`$inc` 一旦重跑就会翻倍
    （AC-09-16 专测"连跑 3 次数字不变"）。所以汇总的口径是
    "读源桶 → 求和 → 覆盖目标桶"，天然幂等。
    """
    fields: dict[str, Any] = {
        "bucket_type": bucket_type, "bucket_key": bucket_key, "bucket_ts": int(ts),
        "granularity": granularity,
        "metrics": {k: int(v) for k, v in metrics.items()},
        "uv_set": list(dict.fromkeys(uv_set)),
        "updated_at": ts_ms,
    }
    if granularity in (MetricGranularity.M1.value, MetricGranularity.H1.value):
        fields["expire_at"] = _expire_document(ts)
    await mongo.collection(BUCKETS).update_one(
        {"_id": bucket_id(bucket_type, bucket_key, ts)},
        {"$set": fields, "$setOnInsert": {"created_at": ts_ms}}, upsert=True)


# =========================================================================== 读
async def list_buckets(*, bucket_type: str, granularity: str,
                       start_ts: int, end_ts: int,
                       bucket_key: str | None = None,
                       limit: int = 5000) -> list[dict[str, Any]]:
    """按范围取桶序列（**必须命中 `ix_metric_query`**，AC-09-04 禁集合扫描）。"""
    query: dict[str, Any] = {"bucket_type": bucket_type,
                             "granularity": granularity,
                             "bucket_ts": {"$gte": int(start_ts),
                                           "$lte": int(end_ts)}}
    if bucket_key:
        query["bucket_key"] = bucket_key
    cursor = (mongo.collection(BUCKETS).find(query)
              .sort("bucket_ts", 1).limit(limit))
    return await cursor.to_list(length=limit)


async def get_bucket(bucket_type: str, bucket_key: str,
                     ts: int) -> dict[str, Any] | None:
    """按精确 `_id` 取单桶（下钻与测试断言用）。"""
    return await mongo.collection(BUCKETS).find_one(
        {"_id": bucket_id(bucket_type, bucket_key, ts)})


async def aggregate_metrics(*, bucket_type: str, granularity: str,
                            start_ts: int, end_ts: int,
                            bucket_key: str | None = None) -> dict[str, Any]:
    """在**库内**求和（`$group` + `$sum`），返回 `{字段: 合计}` 与桶数。

    为什么不把桶取回应用层求和：看板查询的桶数可能上千，
    库内聚合只回一行，网络与解析成本都低一个量级。
    """
    match: dict[str, Any] = {"bucket_type": bucket_type, "granularity": granularity,
                             "bucket_ts": {"$gte": int(start_ts),
                                           "$lte": int(end_ts)}}
    if bucket_key:
        match["bucket_key"] = bucket_key
    # ⚠️ 异步 `aggregate()` 返回**协程**，必须先 await 拿游标再 `to_list()`
    cursor = await mongo.collection(BUCKETS).aggregate([
        {"$match": match},
        {"$group": {"_id": None, "count": {"$sum": 1},
                    "pv": {"$sum": "$metrics.pv"},
                    "faq_hit_cnt": {"$sum": "$metrics.faq_hit_cnt"},
                    "rag_cnt": {"$sum": "$metrics.rag_cnt"},
                    "no_knowledge_cnt": {"$sum": "$metrics.no_knowledge_cnt"},
                    "token_prompt": {"$sum": "$metrics.token_prompt"},
                    "token_completion": {"$sum": "$metrics.token_completion"},
                    "elapsed_sum_ms": {"$sum": "$metrics.elapsed_sum_ms"},
                    "denied_chunk_cnt": {"$sum": "$metrics.denied_chunk_cnt"},
                    "question_cnt": {"$sum": "$metrics.question_cnt"}}}])
    rows = await cursor.to_list(length=1)
    row = rows[0] if rows else {}
    row.pop("_id", None)
    return {k: int(v or 0) for k, v in row.items()}


async def overview_uv(*, start_ts: int, end_ts: int) -> int:
    """区间 UV = **各日桶 `uv_set` 的并集大小**（AC-09-02：区间 UV 不可相加）。

    ⚠️ 这是本模块唯一不能"求和"的指标：把 7 天的 UV 加起来会把
    "连续 3 天都提问的同一个人"算成 3 个用户。所以这里用 `$addToSet` 归并集合。
    """
    cursor = await mongo.collection(BUCKETS).aggregate([
        {"$match": {"bucket_type": "global", "bucket_key": "uv",
                    "granularity": MetricGranularity.D1.value,
                    "bucket_ts": {"$gte": int(start_ts), "$lte": int(end_ts)}}},
        {"$group": {"_id": None, "uv": {"$addToSet": "$uv_set"}}}])
    rows = await cursor.to_list(length=1)
    if not rows:
        return 0
    merged: set[str] = set()
    for chunk in rows[0].get("uv") or []:
        merged.update(chunk or [])
    return len(merged)


async def count_buckets() -> int:
    """桶总数（看板自检与测试断言用；不做任何过滤）。"""
    return await mongo.collection(BUCKETS).count_documents({})


async def drop_collection() -> None:
    """清空（仅测试路径）。"""
    await mongo.require_db().drop_collection(BUCKETS)


def now_ms() -> int:
    """当前毫秒时间戳。"""
    return int(time.time() * 1000)


__all__ = [
    "BUCKETS", "SHORT_GRANULARITY_TTL_DAYS", "bucket_id", "ensure_indexes",
    "inc_bucket", "replace_bucket", "list_buckets", "get_bucket",
    "aggregate_metrics", "overview_uv", "count_buckets", "drop_collection",
    "now_ms",
]
