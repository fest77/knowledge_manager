# -*- coding: utf-8 -*-
"""MongoDB 连接（概要设计 AD：pymongo 4.17 自带 `AsyncMongoClient`，**不引 motor**）。"""
from __future__ import annotations

from typing import Any

from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import settings
from app.core.errors import BizError, Err
from app.core.logging import logger


class Mongo:
    """进程级单例。连接在 lifespan 里建立，退出时释放。"""

    def __init__(self) -> None:
        self.client: AsyncMongoClient | None = None
        self.db: AsyncDatabase | None = None

    async def connect(self) -> None:
        """建立连接并 ping 一次——连不上就 fail-fast，不要让服务半死不活地起来。"""
        self.client = AsyncMongoClient(
            settings.mongo_url,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            appname="knowledge_manager",
        )
        self.db = self.client[settings.mongo_db]
        await self.client.admin.command("ping")
        logger.info("MongoDB 已连接：%s / %s", settings.mongo_url, settings.mongo_db)

    async def close(self) -> None:
        """释放连接池，并把句柄置空。

        置空很关键：否则关闭后 `require_db()` 仍会返回一个**指向已关闭客户端**的库句柄，
        错误会推迟到真正查询时才炸，排障困难。置空后立刻转为 `SYS-4001`（快速失败）。
        """
        if self.client is not None:
            await self.client.close()
            self.client = None
            self.db = None
            logger.info("MongoDB 连接已释放")

    async def ping(self) -> bool:
        """健康检查用：不抛异常，只回答通不通。"""
        if self.client is None:
            return False
        try:
            await self.client.admin.command("ping")
            return True
        except Exception:                             # noqa: BLE001 — 健康检查不抛
            return False

    def require_db(self) -> AsyncDatabase:
        """取库句柄；未连接时转成 `SYS-4001`，由统一处理器产出规范响应。"""
        if self.db is None:
            raise BizError(Err.SYS_DB_UNAVAILABLE, detail="数据库尚未连接")
        return self.db

    def collection(self, name: str) -> Any:
        """取集合句柄（等价于 `require_db()[name]`，便于统一加日志/埋点）。"""
        return self.require_db()[name]


mongo = Mongo()
