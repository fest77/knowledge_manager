# -*- coding: utf-8 -*-
"""`GET /health`：存活 + 三存储连通性 + 模型可用性（总纲 §6.1 白名单接口）。

**报 `not_configured` 与报 `unavailable` 是两回事**，不能混：

| 取值 | 含义 | 排查方向 |
|---|---|---|
| `ok` | 连通 | — |
| `not_configured` | **本次进程没有去连它**（配置缺失 / 依赖被跳过） | 看 `.env` 与 `lifespan` |
| `unavailable` | 连了但连不上 | 看对端服务 |

早期骨架把 Milvus / MinIO 一律写死成 `not_configured`（"本切片未接入，不伪装成不可用"）。
模块 04 落地后它们**真的被连接了**，所以这里必须换成真实探测——否则
"Milvus 挂了"和"没连 Milvus"在界面上长得一模一样，而这两种情况的处置完全不同。

`embedding_model` 用**不触发加载**的 `health()`：`/health` 会被探针高频调用，
而 BGE-M3 加载一次要十几秒、2.2GB 显存——探针不能成为压垮显存的那根稻草。
所以它只报"是否已加载 / 跑在哪个设备 / 是否降级"，未加载就是 `not_configured`。
"""
from __future__ import annotations

import time

from fastapi import APIRouter

from app.core.config import settings
from app.core.response import ok
from app.infra.milvus import milvus
from app.infra.minio import minio_store
from app.infra.mongo import mongo
from app.services.audit_service import audit_service
from app.services.config_service import config_service
from app.services.embedding_service import embedding_service
from app.services.rerank_service import rerank_service

router = APIRouter(tags=["00 公共基础"])


def _tri_state(connected: bool, reachable: bool) -> str:
    """把"连了没"×"通不通"压成一个三元取值（见模块头部的语义表）。"""
    if not connected:
        return "not_configured"
    return "ok" if reachable else "unavailable"


def _model_state(health: dict) -> str:
    """本地模型的取值域：`ok` / `loading` / `not_configured`。

    `loading` 是**必要的第四态**：BGE-M3 首次加载要 15~40 秒，
    其间 `/health` 报 `not_configured` 会让人以为"没配好"，
    而实际上它正在工作（日志里每 5 秒有一行心跳）。
    """
    if health.get("loaded"):
        return "ok"
    if health.get("loading"):
        return "loading"
    return "not_configured"


def _import_config(key: str, fallback: int) -> int:
    """读导入运行参数，**读不到也不抛**。

    `/health` 的契约是"永远能回答"，而 `config_service.get_raw()` 在
    "配置还没 bootstrap"（例如探针在 `lifespan` 完成前就打进来）时会抛错。
    让探针因为一个展示用字段而 500，是把可观测性做成了新的故障点。
    """
    try:
        return int(config_service.get_raw(key))
    except Exception:                                     # noqa: BLE001
        return fallback


@router.get("/health", summary="存活与依赖连通性")
async def health():
    """存活与依赖连通性；未接入的依赖如实报 `not_configured`。

    只有 MongoDB 是**硬依赖**（挂了就 `degraded`）：Milvus / MinIO 挂掉时
    台账维护、权限、审计仍然可用，业务不该被判成"整体不可用"。
    """
    mongo_ok = await mongo.ping()
    milvus_ok = await milvus.ping()
    minio_ok = await minio_store.ping()
    embedding = embedding_service.health()
    reranker = rerank_service.health()
    return ok({
        "status": "ok" if mongo_ok else "degraded",
        "version": settings.version,
        "server_time": int(time.time()),
        "dependencies": {
            "mongodb": "ok" if mongo_ok else "unavailable",
            "milvus": _tri_state(milvus.client is not None, milvus_ok),
            "minio": _tri_state(minio_store.client is not None, minio_ok),
            # 三态 + `loading`：模型加载要 15~40 秒，探针期间必须能区分
            # "没加载" 与 "正在加载（已用 N 秒）"——否则第一次导入看起来就是卡死
            "embedding_model": _model_state(embedding),
            "reranker_model": _model_state(reranker),
        },
        # 导入是重链路，它的运行参数与降级状态要能被一眼看到（Spec §7.2）
        "import": {
            "embedding_device": embedding["device"],
            "embedding_degraded": embedding["degraded"],
            "embedding_reason": embedding["reason"],
            # 正在加载时的进度（秒）；不在加载就是 null。前端可据此显示"加载中…"
            "embedding_loading_seconds": embedding.get("loading_seconds"),
            "reranker_loading_seconds": reranker.get("loading_seconds"),
            "queue_limit": _import_config("import.max_queue", 20),
            "concurrency": _import_config("import.concurrency", 2),
        },
        # 审计降级状态：`degraded` 或 `spool_writable=false` 都必须被看见
        "audit": audit_service.health(),
    })
