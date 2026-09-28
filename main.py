# -*- coding: utf-8 -*-
r"""knowledge_manager - 知识库管理平台（2.9）

⚠️ **本文件是切片之前的"环境自检"入口，已被取代**，不要用它启动应用。
当前应用入口：`app.main:app`

    .venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8102

保留本文件的原因：`/health` 会逐个 import 依赖库并报告版本与 CUDA 可用性，
在排查"环境到底装全了没有"时仍然好用。它与 `app.main` 的 `/health`
（报三存储**连通性**）是**互补**关系，不是重复。

    .venv\Scripts\python.exe -m uvicorn main:app --port 8103   # 仅在需要环境自检时用
"""
from __future__ import annotations

import importlib
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# 显式加载本目录下的 .env（与本班其他项目共用 192.168.6.170 上的 Milvus / MongoDB / MinIO）
load_dotenv(Path(__file__).resolve().with_name(".env"))

app = FastAPI(
    title="knowledge_manager · 环境自检（legacy）",
    version="0.1.0",
    description="仅用于确认依赖与外部存储配置是否就绪；业务接口在 app.main:app",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
def health() -> dict:
    """健康检查：回报各依赖是否可导入，以及 GPU 与外部存储配置。"""
    deps: dict[str, str] = {}
    for mod in ("fastapi", "pymilvus", "pymongo", "minio", "langgraph",
                "sentence_transformers", "torch", "mineru"):
        try:
            m = importlib.import_module(mod)
            deps[mod] = str(getattr(m, "__version__", "installed"))
        except Exception as e:
            deps[mod] = f"MISSING ({type(e).__name__})"

    try:
        import torch
        cuda = f"available={torch.cuda.is_available()}"
    except Exception:
        cuda = "torch 未安装"

    return {
        "app": "knowledge_manager (legacy env-check)",
        "status": "ok",
        "time": datetime.now().isoformat(timespec="seconds"),
        "deps": deps,
        "torch_cuda": cuda,
        "storage": {
            "milvus_url": os.getenv("MILVUS_URL", "(未配置)"),
            "mongo_url": os.getenv("MONGO_URL", "(未配置)"),
            "mongo_db": os.getenv("MONGO_DB_NAME", "(未配置)"),
            "minio_endpoint": os.getenv("MINIO_ENDPOINT", "(未配置)"),
        },
    }


@app.get("/")
def index() -> dict:
    """根路径：给出本入口的定位与真正应用入口的地址。"""
    return {
        "message": "knowledge_manager legacy env-check is running",
        "real_app_entry": "app.main:app  →  uvicorn app.main:app --port 8102",
        "docs": "/docs",
    }
