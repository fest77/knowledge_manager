# -*- coding: utf-8 -*-
"""模块 04 的切片写入与启停（E01 的**唯一写入口**）。

本文件先交付整条流水线里**最容易被别的模块依赖**的两件事：

| 方法 | 谁调用 | 为什么它必须先落地 |
|---|---|---|
| `store_chunks()` | 本模块的流水线 | 切片进 Milvus 的唯一路径；`enabled` 初值、`chunk_index` 顺序都在这里定 |
| **`set_chunks_enabled()`** | **模块 03**（文档启停/软删除时） | 03↔04 连续批次约定（总纲 §4.2）的"前向调用点" |

**ER-02 / ER-12 的落点**：整个项目里**只有这个文件**会写 `kb_chunks_v2`。
模块 03 不连 Milvus（AC-03-11 专门查这一点），06 只读。所以"切片 `enabled`
到底是谁改的"这个问题，答案永远是这一处。

**为什么`set_chunks_enabled` 用 `update` 而不是"删了重建"**：软删除的文档要能从
回收站恢复（AD-05）。删切片再重建意味着恢复时要重新解析、重新向量化——那既慢
又可能因为模型版本变化而得到**与原来不同的切片**，历史引用就断了。
"""
from __future__ import annotations

from typing import Any, Sequence

from app.core.logging import logger
from app.infra.milvus import (F_CHUNK_INDEX, F_CONTENT, F_DENSE, F_DOC_ID, F_ENABLED,
                              F_FILE_TITLE, F_PARENT_TITLE, F_PART, F_SPARSE, F_TITLE,
                              milvus)
from app.services.splitter import Chunk


class ChunkStoreError(RuntimeError):
    """切片写入/更新失败（交给流水线转成任务失败，不向上抛到业务）。"""


def _sparse_vector_of(text: str) -> dict[int, float]:
    """构造 sparse 向量（词袋式占位，权重 1.0）。

    ⚠️ **这是显式的降级实现，不是最终方案**：BGE-M3 的 sparse 头要经
    `FlagEmbedding`，而它**不在 `requirements.txt` 里**（见进度文档 §3.2.1）。
    项目规则是"不引入未声明依赖"，所以这里先用可用的信息量最小的实现把链路打通，
    并**在任务里记一条 `sparse_placeholder` 降级**——让"现在用的是占位实现"
    这件事在演示与验收时是可见的，而不是悄悄降级。

    为什么不能返回空 dict：Milvus 的 `SPARSE_FLOAT_VECTOR` 字段允许空值，
    但全空会让 `SPARSE_INVERTED_INDEX` 退化成"永远命中 0 条"，
    而查询侧看不出区别——症状是"混合检索的 sparse 那一路从来没贡献过结果"。
    """
    tokens = {token for token in text.replace("\n", " ").split() if token}
    if not tokens:
        return {0: 0.0}
    # 用稳定的哈希下标（不用内置 hash：它带 PYTHONHASHSEED，重启就漂）
    import hashlib

    out: dict[int, float] = {}
    for token in tokens:
        digest = hashlib.md5(token.encode("utf-8")).hexdigest()
        out[int(digest[:8], 16) % 1_000_000] = 1.0
    return out


async def ensure_collection() -> bool:
    """确保 E01 集合存在（流水线开始前调用，幂等）。"""
    return await milvus.ensure_chunks_collection()


def build_rows(doc_id: str, chunks: Sequence[Chunk],
               dense_vectors: Sequence[Sequence[float]]) -> list[dict[str, Any]]:
    """把切片 + dense 向量组装成 Milvus 行（**顺序严格对齐**）。

    这里做一次长度断言：切片数与向量数不一致时**立刻抛错**。
    不这么做的后果很具体——Milvus 按位置对应字段，错位之后
    "第 1 片的正文配了第 2 片的向量"，检索会命中正确的文档却给出错误的原文，
    而这种错误**从检索结果上完全看不出来**。

    **字段口径严格照模块 04 Spec §2.6**：

    | 字段 | 取值 | 为什么 |
    |---|---|---|
    | `content` | `f"{title}\\n\\n{body}"` | 标题并进被检索的文本，提升"标题即答案"型查询的召回 |
    | `title` / `parent_title` / `file_title` / `part` | 切分锚点**原样落库** | 预览与溯源卡片要展示"出自哪一章" |
    | `enabled` | `False` | 新切片默认不可检索，要经 03 的 `toggle` 显式启用 |

    ⚠️ `title` 是"标题即答案"的检索信号，而 §2.6 明确要求 `content` 带上标题、
    `title` **另存一份**——两者都要写，少写 `title` 会让切片预览的标题列永远是空的。
    """
    if len(chunks) != len(dense_vectors):
        raise ChunkStoreError(
            f"切片数与向量数不一致：{len(chunks)} vs {len(dense_vectors)}")
    rows: list[dict[str, Any]] = []
    for chunk, dense in zip(chunks, dense_vectors):
        if not dense:
            raise ChunkStoreError(f"切片 {chunk.index} 的 dense 向量为空")
        rows.append({
            F_DOC_ID: doc_id,
            F_CHUNK_INDEX: int(chunk.index),
            # ⚠️ **不要 `.strip()`**：AC-04-12 的判定是
            # `content == f"{title}\n\n{body}"`（逐字相等），任何"顺手清理空白"
            # 都会让这条验收失败，而失败原因是"多删了一个空格"，极难定位
            F_CONTENT: f"{chunk.title}\n\n{chunk.body}",
            F_TITLE: chunk.title,
            F_PARENT_TITLE: chunk.parent_title,
            F_FILE_TITLE: chunk.file_title,
            F_PART: int(chunk.part),
            F_DENSE: [float(x) for x in dense],
            F_SPARSE: _sparse_vector_of(chunk.content_for_embedding),
            # 新切片默认**不可检索**：文档要经 03 的 `toggle` 显式启用才对外可见。
            # 若默认 true，"导入中就已经能被搜到"会造成答案引用一篇半成品。
            F_ENABLED: False,
        })
    return rows


async def store_chunks(doc_id: str, chunks: Sequence[Chunk],
                       dense_vectors: Sequence[Sequence[float]]) -> int:
    """写入切片（**E01 的唯一写入口**），返回写入条数。"""
    if not chunks:
        logger.warning("文档 %s 没有产出任何切片，跳过写入", doc_id)
        return 0
    rows = build_rows(doc_id, chunks, dense_vectors)
    try:
        written = await milvus.insert_chunks(rows)
    except Exception as exc:                                  # noqa: BLE001
        raise ChunkStoreError(f"切片写入 Milvus 失败：{exc}") from exc
    logger.info("文档 %s 已写入 %d 条切片（默认 enabled=false）", doc_id, written)
    return written


async def set_chunks_enabled(doc_id: str, enabled: bool) -> int:
    """**按 `doc_id` 批量启停切片**，返回受影响条数（ER-15 的落点）。

    这是模块 03 的**唯一合法入口**（总纲 §4.2 约定的那个前向调用点）：
    03 停用/软删除/恢复文档时调它，而**不自己连 Milvus**。

    **三种情形必须区分清楚**（含糊会把"危险"当成"正常"）：

    | 情形 | 处置 | 为什么 |
    |---|---|---|
    | Milvus **未连接** | 返回 0（no-op）+ WARN | 进程压根没接 Milvus = 不可能有切片。**测试路径就是这种情形**，不该为启停去连 Milvus |
    | 已连接、集合不存在 | 返回 0（no-op）+ INFO | 从没导入过任何切片（如 08 转建的纯占位文档）。报错会让 03 的正常业务失败 |
    | 已连接、**update 抛错** | 抛 `ChunkStoreError` | `DOC-4003`、状态不变（AC-03-10）；静默吞掉 = 已停用仍可召回 |

    > 生产路径必然已连接（`lifespan` 里 `milvus.connect()`），所以第一行
    > 那个 no-op 分支**只在实际出问题时才会走到**；它记 WARN 而不是静默返回，
    > 就是为了避免"忘了连 Milvus 导致启停静默失效"这件事没人发现。
    """
    from app.infra.milvus import milvus as _milvus

    if _milvus.client is None:
        logger.warning("Milvus 未连接，%s 的切片启停按 0 条处理（系统内不可能有切片）",
                       doc_id)
        return 0
    if not await _milvus.collection_exists():
        logger.info("E01 集合尚不存在，%s 的切片启停按 0 条处理", doc_id)
        return 0
    try:
        count = await _milvus.set_enabled(doc_id, enabled)
    except Exception as exc:                                  # noqa: BLE001
        raise ChunkStoreError(f"切片启停失败 doc_id={doc_id}：{exc}") from exc
    return count


async def count_chunks(doc_id: str, *, enabled: bool | None = None) -> int:
    """数某文档的切片数（03 的 `chunk_count` 回填、验收核对用）。"""
    expr = f'{F_DOC_ID} == "{doc_id}"'
    if enabled is not None:
        expr += f" and {F_ENABLED} == {str(bool(enabled)).lower()}"
    return await milvus.count(expr)


async def drop_chunks_of(doc_id: str) -> int:
    """物理清理某文档的全部切片（**只在显式清理脚本里调用**）。

    软删除**不**走这里（AD-05 / ER-15）：先删切片再恢复文档就恢复不回来了。
    """
    client = milvus.require()
    from app.core.config import settings

    before = await count_chunks(doc_id)
    if before:
        await client.delete(collection_name=settings.chunks_collection,
                            filter=f'{F_DOC_ID} == "{doc_id}"')
        # 与 insert 同理：不 flush 的话"已删除"在下一跳查询里仍看得见，
        # 清理脚本会误报"删了但还在"
        await milvus.flush()
        logger.warning("已物理清理文档 %s 的 %d 条切片", doc_id, before)
    return before


__all__ = [
    "ChunkStoreError", "ensure_collection", "build_rows", "store_chunks",
    "set_chunks_enabled", "count_chunks", "drop_chunks_of",
]
