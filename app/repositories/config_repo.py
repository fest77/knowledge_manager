# -*- coding: utf-8 -*-
"""E22 系统配置 `system_config` 的数据访问层。

**归属**：模块 00 公共基础是它的**唯一写入者**（总纲 §5 的实体归属矩阵）。
其他模块只允许经 `ConfigService` 读取，不得直接读写本集合。
"""
from __future__ import annotations

from typing import Any

from app.core.logging import logger
from app.infra.mongo import mongo

CONFIG = "system_config"


async def ensure_indexes() -> None:
    """按分组+顺序渲染界面，所以建一个组合索引。"""
    db = mongo.require_db()
    await db[CONFIG].create_index([("group", 1), ("label", 1)], name="ix_group_label")


async def list_all() -> list[dict[str, Any]]:
    """取全部配置项（顺序由 `ConfigService` 按预置表决定，这里不排序）。"""
    cursor = mongo.collection(CONFIG).find({})
    return await cursor.to_list(length=None)


async def insert_missing(item: dict[str, Any]) -> bool:
    """只插入缺失的预置项，**绝不覆盖已有值**。返回是否真的插入了。

    用 `update_one(..., upsert=True)` 配 `$setOnInsert` 实现原子"存在则跳过"，
    避免"先查后插"的竞态。
    """
    result = await mongo.collection(CONFIG).update_one(
        {"_id": item["_id"]},
        {"$setOnInsert": item},
        upsert=True,
    )
    return result.upserted_id is not None


async def set_value(key: str, value: Any, actor: str, ts: int) -> dict[str, Any] | None:
    """更新某个配置项的值并 `version+1`；返回更新前的文档（供审计比对），不存在则 None。"""
    before = await mongo.collection(CONFIG).find_one({"_id": key})
    if before is None:
        return None
    await mongo.collection(CONFIG).update_one(
        {"_id": key},
        {"$set": {"value": value, "updated_by": actor, "updated_at": ts},
         "$inc": {"version": 1}},
    )
    logger.info("配置已更新 key=%s actor=%s", key, actor)
    return before
