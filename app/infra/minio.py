# -*- coding: utf-8 -*-
"""MinIO 文件对象存储（E03）。

**这是模块 04 的唯一 MinIO 入口**（ER-02：E03 文件对象的唯一写入者是 04）。
模块 03 只经 `import_service` 读元信息，不直接操作桶。

**为什么全部走 `asyncio.to_thread`**：`minio` SDK 是**纯同步**的（没有 async 版本），
而上传/下载都是网络 IO。在协程里直接调它会**阻塞整个事件循环**——问答接口会跟着卡住。
`to_thread` 把同步调用挪到线程池，代价可控（导入本身是重任务，ADR-09 已用
`Semaphore` 限并发）。

**对象键的设计**：`{doc_id}/{index:04d}_{safe_name}`。以 `doc_id` 打头有两个好处：
① 删文档时可以按前缀批量清理（`list_objects(prefix=f"{doc_id}/")`）；
② 桶里一眼能看出"这个对象属于哪篇知识"，排障时不必去查库。
"""
from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import logger


class MinioUnavailable(RuntimeError):
    """MinIO 不可用（未连接或探测失败）。"""


def safe_object_name(file_name: str) -> str:
    """把原始文件名净化为可安全放进对象键的形式。

    只保留字母/数字/下划线/点/横线，其余（含中文与空格）替换为 `_`——
    S3 的对象键允许 UTF-8，但**签名与 URL 编码**在这些字符上极易出坑，
    而对象键对用户不可见（展示用的是 `kb_documents.file_name`），所以净化没有代价。
    """
    cleaned = "".join(ch if (ch.isalnum() and ch.isascii()) or ch in "._-"
                      else "_" for ch in (file_name or "unnamed"))
    return cleaned[-80:] or "unnamed"


class MinioStore:
    """进程级单例：连接与桶校验在 lifespan 里完成。"""

    def __init__(self) -> None:
        self.client: Any = None

    # ------------------------------------------------------------------ 生命周期
    async def connect(self, *, create_bucket: bool = True) -> None:
        """建立连接、确认桶存在（不存在则建）。

        **不 fail-fast 到阻止启动**：MinIO 挂了应该只让"导入"不可用，
        问答与台账维护仍要能用。所以 `connect()` 失败由调用方决定降级——
        实测该桶已存在，正常路径不会走到创建分支。
        """
        from minio import Minio

        self.client = Minio(settings.minio_endpoint,
                            access_key=settings.minio_access_key,
                            secret_key=settings.minio_secret_key,
                            secure=settings.minio_secure)
        exists = await asyncio.to_thread(self.client.bucket_exists, settings.minio_bucket)
        if not exists and create_bucket:
            await asyncio.to_thread(self.client.make_bucket, settings.minio_bucket)
            logger.info("MinIO 桶已创建：%s", settings.minio_bucket)
        logger.info("MinIO 已连接：%s / %s", settings.minio_endpoint,
                    settings.minio_bucket)

    async def close(self) -> None:
        """minio 的客户端没有连接池需要显式关闭，这里只置空句柄。"""
        self.client = None

    async def ping(self) -> bool:
        """健康检查：不抛异常，只回答通不通。"""
        if self.client is None:
            return False
        try:
            return await asyncio.to_thread(self.client.bucket_exists,
                                           settings.minio_bucket)
        except Exception:                                     # noqa: BLE001
            return False

    def require(self) -> Any:
        """取客户端；未连接时抛 `MinioUnavailable`。"""
        if self.client is None:
            raise MinioUnavailable("MinIO 尚未连接")
        return self.client

    # ------------------------------------------------------------------ 对象
    @staticmethod
    def object_key(doc_id: str, index: int, file_name: str) -> str:
        """对象键：`{doc_id}/{index:04d}_{safe_name}`（见模块头部的说明）。"""
        return f"{doc_id}/{index:04d}_{safe_object_name(file_name)}"

    async def put_bytes(self, object_key: str, data: bytes,
                        content_type: str = "application/octet-stream") -> str:
        """上传字节流，返回对象键。"""
        await asyncio.to_thread(
            self.require().put_object, settings.minio_bucket, object_key,
            io.BytesIO(data), len(data), content_type=content_type)
        logger.info("对象已上传 %s（%d 字节）", object_key, len(data))
        return object_key

    async def put_file(self, object_key: str, path: Path | str, *,
                       content_type: str = "application/octet-stream") -> str:
        """**流式**上传本地文件（导入原文件走这条）。

        为什么不用 `put_bytes`：单文件上限 100MB（`import.max_file_mb`），
        而导入是**并发 2**——把两份 100MB 读进内存只为了传一次，是白送的内存峰值。
        `minio` 的 `put_object` 接受任意 file-like，所以直接给文件句柄，
        由 SDK 分片读取（它内部按 5MB 分片）。

        文件大小取 `stat().st_size` 而不是让调用方传：传错长度是**静默截断**
        （S3 按声明长度读），症状是"文件传上去了但少了一段"，极难发现。
        """
        import os

        size = os.path.getsize(path)

        def _put() -> None:
            with open(path, "rb") as handle:
                self.require().put_object(settings.minio_bucket, object_key,
                                          handle, size, content_type=content_type)

        await asyncio.to_thread(_put)
        logger.info("对象已上传 %s（%d 字节，流式）", object_key, size)
        return object_key

    async def get_bytes(self, object_key: str) -> bytes:
        """下载对象为字节。"""
        response = await asyncio.to_thread(
            self.require().get_object, settings.minio_bucket, object_key)
        try:
            return await asyncio.to_thread(response.read)
        finally:
            await asyncio.to_thread(response.close)
            await asyncio.to_thread(response.release_conn)

    async def remove(self, object_key: str) -> None:
        """删除单个对象（**幂等**：对象不存在也算成功）。"""
        await asyncio.to_thread(self.require().remove_object,
                                settings.minio_bucket, object_key)

    async def remove_prefix(self, doc_id: str) -> int:
        """按 `doc_id/` 前缀批量清理，返回删除条数。

        只在**显式物理清理脚本**里调用：软删除文档时**不删对象**（AD-05），
        否则"从回收站恢复"就恢复不回来了——文件已经没了。
        """
        client = self.require()
        keys = await asyncio.to_thread(
            lambda: [obj.object_name for obj in client.list_objects(
                settings.minio_bucket, prefix=f"{doc_id}/", recursive=True)])
        for key in keys:
            await self.remove(key)
        if keys:
            logger.info("已清理 %s 的 %d 个对象", doc_id, len(keys))
        return len(keys)


minio_store = MinioStore()

__all__ = ["MinioStore", "MinioUnavailable", "minio_store", "safe_object_name"]
