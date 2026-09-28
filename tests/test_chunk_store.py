# -*- coding: utf-8 -*-
"""切片入库与启停的**真机测试**（真连 Milvus）。

这是解开模块 03 ↔ 04 连续批次约定的关键一环：`set_chunks_enabled()` 一旦真机验证通过，
模块 03 就能补上那处调用并重跑测试。

测试用**独立的 doc_id 前缀**（`DOCEST...`）并自行清理，不去碰真实导入产生的切片。
Milvus 集合不可达时 `pytest.skip`（换机器/虚拟机没起是环境问题，不该让套件变红）。
"""
from __future__ import annotations

import socket

import pytest

from app.core.config import settings
from app.infra.milvus import milvus
from app.services.chunk_store import (ChunkStoreError, build_rows, count_chunks,
                                      drop_chunks_of, ensure_collection,
                                      set_chunks_enabled, store_chunks)
from app.services.splitter import split_markdown

pytestmark = pytest.mark.anyio

# 用固定前缀，便于清理与识别（不碰真实导入的数据）
EST_DOC_A = "DOCEST0000000001"
EST_DOC_B = "DOCEST0000000002"


def _milvus_up() -> bool:
    host, _, port = settings.milvus_url.replace("http://", "").partition(":")
    sock = socket.socket()
    sock.settimeout(4)
    try:
        sock.connect((host, int(port)))
        return True
    except Exception:                                         # noqa: BLE001
        return False
    finally:
        sock.close()


requires_milvus = pytest.mark.skipif(
    not _milvus_up(), reason=f"Milvus {settings.milvus_url} 不可达——启动虚拟机后重跑")


def _fake_dense(count: int, dim: int | None = None) -> list[list[float]]:
    """构造 count 条 dim 维的假向量（归一化，只为验证写入与启停，不验证语义）。"""
    dim = dim or settings.embedding_dim
    out: list[list[float]] = []
    for i in range(count):
        vec = [0.0] * dim
        vec[i % dim] = 1.0
        out.append(vec)
    return out


# ===================================================================== 纯逻辑（不需 Milvus）
def test_build_rows_rejects_mismatched_counts():
    """切片数与向量数不一致时必须抛错。

    不抛的后果：Milvus 按位置对应字段，错位后"第 1 片的正文配了第 2 片的向量"——
    检索会命中正确的文档却给出错误的原文，而**从检索结果上完全看不出来**。
    """
    chunks = split_markdown("# 标题\n正文一。\n## 子标题\n正文二。", "文档")
    with pytest.raises(ChunkStoreError):
        build_rows(EST_DOC_A, chunks, _fake_dense(len(chunks) - 1))


def test_build_rows_defaults_to_disabled_and_keeps_order():
    """新切片默认 `enabled=false`，且 `chunk_index` 保持切分顺序。

    默认 false 的理由：文档要经 03 的 `toggle` 显式启用才对外可见。
    若默认 true，"导入中就已经能被搜到"会让答案引用一篇半成品。
    """
    chunks = split_markdown("# 甲\n正文甲。\n# 乙\n正文乙。", "文档")
    rows = build_rows(EST_DOC_A, chunks, _fake_dense(len(chunks)))
    assert all(r["enabled"] is False for r in rows), "必须默认不可检索"
    assert [r["chunk_index"] for r in rows] == list(range(len(chunks)))
    assert all(len(r["dense_vector"]) == settings.embedding_dim for r in rows)
    assert all(isinstance(r["sparse_vector"], dict) and r["sparse_vector"] for r in rows)


def test_sparse_vector_is_stable_across_processes():
    """sparse 下标必须**跨进程稳定**：用内置 `hash()` 会带 PYTHONHASHSEED，重启就漂。"""
    from app.services.chunk_store import _sparse_vector_of

    first = _sparse_vector_of("财务 报销 标准")
    second = _sparse_vector_of("财务 报销 标准")
    assert first == second
    assert len(first) == 3, "三个不同的词应落在三个下标上"
    assert all(v == 1.0 for v in first.values())


# ===================================================================== Milvus 真机
@requires_milvus
async def test_store_and_toggle_chunks_roundtrip():
    """★ 核心链路：写入切片 → 默认不可检索 → `set_chunks_enabled` 双向切换。"""
    await milvus.connect()
    try:
        await ensure_collection()
        await drop_chunks_of(EST_DOC_A)                   # 清掉上轮残留

        chunks = split_markdown("# 报销标准\n住宿上限 600 元。\n# 报销流程\n先审批后报销。",
                               "财务制度")
        written = await store_chunks(EST_DOC_A, chunks, _fake_dense(len(chunks)))
        assert written == len(chunks)

        assert await count_chunks(EST_DOC_A) == len(chunks)
        assert await count_chunks(EST_DOC_A, enabled=True) == 0, "默认不可检索"
        assert await count_chunks(EST_DOC_A, enabled=False) == len(chunks)

        enabled = await set_chunks_enabled(EST_DOC_A, True)
        assert enabled == len(chunks), "应全部受影响"
        assert await count_chunks(EST_DOC_A, enabled=True) == len(chunks)
        assert await count_chunks(EST_DOC_A, enabled=False) == 0

        disabled = await set_chunks_enabled(EST_DOC_A, False)
        assert disabled == len(chunks)
        assert await count_chunks(EST_DOC_A, enabled=True) == 0
    finally:
        await drop_chunks_of(EST_DOC_A)
        await milvus.close()


@requires_milvus
async def test_toggle_only_affects_the_target_document():
    """启停**只影响目标文档**：串到别人的切片上就是越权（把别人停用的内容放出来）。"""
    await milvus.connect()
    try:
        await ensure_collection()
        await drop_chunks_of(EST_DOC_A)
        await drop_chunks_of(EST_DOC_B)
        chunks = split_markdown("# 甲文\n内容甲。", "甲")
        await store_chunks(EST_DOC_A, chunks, _fake_dense(len(chunks)))
        await store_chunks(EST_DOC_B, chunks, _fake_dense(len(chunks)))

        await set_chunks_enabled(EST_DOC_A, True)
        assert await count_chunks(EST_DOC_A, enabled=True) == len(chunks)
        assert await count_chunks(EST_DOC_B, enabled=True) == 0, "乙文档不该被影响"
    finally:
        await drop_chunks_of(EST_DOC_A)
        await drop_chunks_of(EST_DOC_B)
        await milvus.close()


@requires_milvus
async def test_toggle_is_idempotent_and_reversible():
    """幂等且可逆：软删除 ↔ 恢复之间反复切换，切片数**不减**（ER-15 不物理删）。"""
    await milvus.connect()
    try:
        await ensure_collection()
        await drop_chunks_of(EST_DOC_A)
        chunks = split_markdown("# 可逆\n内容。", "文档")
        await store_chunks(EST_DOC_A, chunks, _fake_dense(len(chunks)))

        for _ in range(3):
            await set_chunks_enabled(EST_DOC_A, False)
            assert await count_chunks(EST_DOC_A, enabled=True) == 0
            await set_chunks_enabled(EST_DOC_A, True)
            assert await count_chunks(EST_DOC_A, enabled=True) == len(chunks)
        # 反复切换后总条数不减 —— 证明"只改 enabled，不删数据"
        assert await count_chunks(EST_DOC_A) == len(chunks)
    finally:
        await drop_chunks_of(EST_DOC_A)
        await milvus.close()


@requires_milvus
async def test_toggle_on_document_without_chunks_returns_zero():
    """没有切片的文档（如 08 转建的占位单元）启停返回 0，**不报错**。

    报错会让模块 03 的正常业务失败：启用一篇"待补充"的占位文档是合法操作。
    """
    await milvus.connect()
    try:
        await ensure_collection()
        assert await set_chunks_enabled("DOCNONE0000000001", True) == 0
    finally:
        await milvus.close()


@requires_milvus
async def test_physical_cleanup_only_removes_target_document():
    """物理清理（显式脚本用）只删目标文档，且返回删除条数。"""
    await milvus.connect()
    try:
        await ensure_collection()
        await drop_chunks_of(EST_DOC_A)
        await drop_chunks_of(EST_DOC_B)
        chunks = split_markdown("# 清理\n内容。", "文档")
        await store_chunks(EST_DOC_A, chunks, _fake_dense(len(chunks)))
        await store_chunks(EST_DOC_B, chunks, _fake_dense(len(chunks)))

        removed = await drop_chunks_of(EST_DOC_A)
        assert removed == len(chunks)
        assert await count_chunks(EST_DOC_A) == 0
        assert await count_chunks(EST_DOC_B) == len(chunks), "乙文档必须还在"
    finally:
        await drop_chunks_of(EST_DOC_A)
        await drop_chunks_of(EST_DOC_B)
        await milvus.close()


@requires_milvus
async def test_ensure_collection_is_safe_to_call_repeatedly():
    """流水线开始前会调 `ensure_collection()`，必须幂等且不破坏已有数据。"""
    await milvus.connect()
    try:
        assert await ensure_collection() in (True, False)
        assert await ensure_collection() is False, "已存在时不该重建"
        chunks = split_markdown("# 数据保全\n内容。", "文档")
        await store_chunks(EST_DOC_A, chunks, _fake_dense(len(chunks)))
        await ensure_collection()                          # 再调一次
        assert await count_chunks(EST_DOC_A) == len(chunks), "已有切片不能被冲掉"
    finally:
        await drop_chunks_of(EST_DOC_A)
        await milvus.close()
