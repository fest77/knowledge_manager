# -*- coding: utf-8 -*-
"""Milvus 连接与 E01 切片集合（`kb_chunks_v2`）的建表。

**这是模块 04 的唯一 Milvus 入口**（ER-02：E01 切片的唯一写入者是 04）。
其他模块只经 `ImportService` 或 `PermissionService` 读，**不自己连 Milvus**——
否则"切片 `enabled` 谁在改"就说不清了（模块 03 的 AC-03-11 专门查这一点）。

三个刻意的决定：

| 决定 | 为什么 |
|---|---|
| 用 `AsyncMilvusClient` | pymilvus 3.0.1 自带。同步客户端配 `to_thread` 也能跑，但每条召回占一个线程，白费 |
| 集合名默认**不含 v1** | 实测 Milvus 里真有旧项目的 `kb_chunks_v1`。默认指向它 = 写进别人的集合 |
| 不给 `content` 建标量索引 | 它只用于回填答案片段，不参与筛选。ER-11 只允许按 `enabled`（+可选 `doc_id`）过滤，故只给这两个字段建 INVERTED |

**⚠️ 与 ER-12 的关系**：本文件**不定义任何权限字段**。切片不冗余权限（AD-03），
鉴权在应用层按 `doc_id` 实时判定——所以这里的 schema 一旦多出一个 `dept_id`，
整个"权限即时生效"的设计就破了。
"""
from __future__ import annotations

from typing import Any, Sequence

from app.core.config import settings
from app.core.logging import logger

# E01 的字段名（供其他模块引用，避免各处拼字符串）
F_CHUNK_ID = "chunk_id"
F_DOC_ID = "doc_id"
F_CHUNK_INDEX = "chunk_index"
F_CONTENT = "content"
F_DENSE = "dense_vector"
F_SPARSE = "sparse_vector"
F_ENABLED = "enabled"
# 四个**切分锚点**字段（模块 04 Spec §2.1 的表里是必填/可空的显式字段）。
# 早期骨架只建了 6 个字段，这里补齐——不补的话切片预览（§3.7）拿不到 `title`，
# 06 的引用溯源卡片也就没法渲染"这条答案出自哪一章"。
F_TITLE = "title"
F_PARENT_TITLE = "parent_title"
F_FILE_TITLE = "file_title"
F_PART = "part"
# 启停（read-modify-write）与检索都要按同一份字段清单取回实体，
# 抽成常量避免"新增字段后漏改某一处"——`upsert` 是整实体替换，
# 少取一个字段就会把它**悄悄写成空值**。
ALL_FIELDS: tuple[str, ...] = (F_CHUNK_ID, F_DOC_ID, F_CHUNK_INDEX, F_CONTENT,
                               F_TITLE, F_PARENT_TITLE, F_FILE_TITLE, F_PART,
                               F_DENSE, F_SPARSE, F_ENABLED)



class MilvusUnavailable(RuntimeError):
    """Milvus 不可用（未连接或探测失败）。模块 04 据此走降级或快速失败。"""


class MilvusStore:
    """进程级单例：连接在 lifespan 里建立，退出时释放。"""

    def __init__(self) -> None:
        self.client: Any = None

    # ------------------------------------------------------------------ 生命周期
    async def connect(self) -> None:
        """建立连接并 ping 一次（fail-fast：连不上就别让服务半死不活地起来）。"""
        from pymilvus import AsyncMilvusClient

        self.client = AsyncMilvusClient(uri=settings.milvus_url, timeout=10)
        await self.client.list_collections()
        logger.info("Milvus 已连接：%s（集合 %s）", settings.milvus_url,
                    settings.chunks_collection)

    async def close(self) -> None:
        """释放连接并把句柄置空（置空很关键：否则关闭后仍会拿到一个已关闭的客户端）。"""
        if self.client is not None:
            try:
                await self.client.close()
            except Exception:                                 # noqa: BLE001
                logger.warning("关闭 Milvus 连接时出现异常（忽略）", exc_info=True)
            self.client = None
            logger.info("Milvus 连接已释放")

    async def ping(self) -> bool:
        """健康检查：不抛异常，只回答通不通。"""
        if self.client is None:
            return False
        try:
            await self.client.list_collections()
            return True
        except Exception:                                     # noqa: BLE001
            return False

    def require(self) -> Any:
        """取客户端；未连接时抛 `MilvusUnavailable`。"""
        if self.client is None:
            raise MilvusUnavailable("Milvus 尚未连接")
        return self.client

    # ------------------------------------------------------------------ 集合
    async def collection_exists(self, name: str | None = None) -> bool:
        """集合是否存在（建表幂等与自检用）。"""
        target = name or settings.chunks_collection
        names = await self.require().list_collections()
        return target in list(names)

    async def ensure_chunks_collection(self, *, dim: int | None = None,
                                       recreate: bool = False) -> bool:
        """幂等建 E01 集合。返回"本次是否真的建了"。

        `recreate=True` 会**先 drop 再建**——只在测试里用（清空切片）。
        生产路径绝不能传它：`kb_chunks_v2` 里是全平台的知识切片，
        drop 掉等于把知识库清空，而 05/06 只会看到"什么都检索不到"。
        """
        from pymilvus import DataType

        client = self.require()
        name = settings.chunks_collection
        dim = dim or settings.embedding_dim
        metric = settings.milvus_metric_type.upper()

        if recreate:
            logger.warning("重建 Milvus 集合 %s（仅测试路径允许）", name)
            await client.drop_collection(name)
        elif await self.collection_exists(name):
            return False

        schema = client.create_schema(auto_id=True, enable_dynamic_field=False)
        schema.add_field(F_CHUNK_ID, DataType.INT64, is_primary=True, auto_id=True)
        # `doc_id` 是**鉴权与溯源的关键**：05/06 先按 doc_id 批量取权限，再决定用哪些切片
        schema.add_field(F_DOC_ID, DataType.VARCHAR, max_length=64)
        schema.add_field(F_CHUNK_INDEX, DataType.INT64)
        schema.add_field(F_CONTENT, DataType.VARCHAR, max_length=65535)
        # 切分锚点：`title` 是"标题即答案"型查询的关键（§2.6 把标题并进了 content），
        # 同时它单独保留一份供引用溯源卡片渲染（content 里混着正文，不能拿来展示）
        schema.add_field(F_TITLE, DataType.VARCHAR, max_length=65535)
        schema.add_field(F_PARENT_TITLE, DataType.VARCHAR, max_length=65535)
        schema.add_field(F_FILE_TITLE, DataType.VARCHAR, max_length=65535)
        schema.add_field(F_PART, DataType.INT64)
        schema.add_field(F_DENSE, DataType.FLOAT_VECTOR, dim=dim)
        schema.add_field(F_SPARSE, DataType.SPARSE_FLOAT_VECTOR)
        # `enabled` 支撑切片级启停（文档停用时批量置 false，ER-15：软删除不物理删切片）
        schema.add_field(F_ENABLED, DataType.BOOL)

        index_params = client.prepare_index_params()
        index_params.add_index(F_DENSE, index_type="AUTOINDEX", metric_type=metric)
        index_params.add_index(F_SPARSE, index_type="SPARSE_INVERTED_INDEX", metric_type="IP")
        # 只给会被 `expr` 过滤的两个字段建标量索引（ER-11：expr 只允许 enabled / doc_id）
        index_params.add_index(F_DOC_ID, index_type="INVERTED")
        index_params.add_index(F_ENABLED, index_type="INVERTED")

        await client.create_collection(name, schema=schema, index_params=index_params)
        logger.info("Milvus 集合已创建 %s（dim=%d metric=%s）", name, dim, metric)
        return True

    async def describe_chunks_collection(self) -> dict[str, Any]:
        """取集合的字段与索引摘要（`/health` 与真机验收用）。"""
        client = self.require()
        name = settings.chunks_collection
        info = await client.describe_collection(name)
        fields = {f["name"]: str(f.get("type")) for f in info.get("fields", [])}
        return {"collection": name, "fields": fields,
                "dim": settings.embedding_dim}

    async def count(self, expr: str = "") -> int:
        """数切片条数（`expr` 例如 `enabled == true`）。

        用 `query(..., output_fields=["count(*)"])` 而不是 `num_entities`：
        后者忽略 `expr`，会把"已停用的切片"也算进去。
        """
        client = self.require()
        rows = await client.query(collection_name=settings.chunks_collection,
                                  filter=expr or "", output_fields=["count(*)"])
        if not rows:
            return 0
        first = rows[0]
        return int(first.get("count(*)", first.get("count", 0)))

    # ------------------------------------------------------------------ 写 / 读
    async def flush(self) -> None:
        """把内存里的写入落盘，**让它们立刻可被 query/search 看见**。

        ⚠️ 这一步不能省：Milvus 的 `insert` 是"先进内存段"的，未 flush 之前
        `query(count(*))` 会返回 **0** —— 而插入接口本身**不报错**。
        症状是"导入显示成功、切片数却是 0"，最容易误判成"向量没写进去"。
        """
        await self.require().flush(collection_name=settings.chunks_collection)

    async def insert_chunks(self, rows: Sequence[dict[str, Any]]) -> int:
        """批量写入切片（**E01 的唯一写入口**，由 `ImportService` 调用）。

        写完立刻 `flush()`：流水线紧接着要回填 `chunk_count` 并让文档可被检索，
        不 flush 的话"刚导入的文档搜不到"，而任务状态却是成功。
        """
        if not rows:
            return 0
        result = await self.require().insert(collection_name=settings.chunks_collection,
                                             data=list(rows))
        await self.flush()
        return int(result.get("insert_count", len(rows)))

    async def set_enabled(self, doc_id: str, enabled: bool) -> int:
        """按 `doc_id` 批量启停切片，返回受影响条数（ER-15 的落点）。

        **只改 `enabled`，不删数据**：软删除的文档其切片仍可重建，
        物理清理走显式脚本——这是 AD-05 的要求。

        ⚠️ **实现方式的由来（实测）**：pymilvus 3.0.1 的 `AsyncMilvusClient`
        **没有 `update` 方法**（只有 `upsert` / `delete`）。Milvus 的 `upsert`
        是"整实体替换"，所以必须先 `query` 出完整实体（含向量与主键）、
        改掉 `enabled`、再 `upsert` 回去。三个后果写在这里以免后人踩：

        1. **不能只传 `enabled`** 做 upsert——那会把 `doc_id`/`content`/向量全部清空；
        2. **必须带上 `chunk_id`**（自动主键），否则会插出**新**切片而不是改旧的；
        3. 因此启停是"读-改-写"三步，**不是原子的**。并发启停同一文档会有竞态，
           但启停都由 03 的文档级操作串行触发，实际不会并发。
        """
        client = self.require()
        name = settings.chunks_collection
        rows = await client.query(
            collection_name=name, filter=f'{F_DOC_ID} == "{doc_id}"',
            output_fields=list(ALL_FIELDS))
        if not rows:
            return 0
        for row in rows:
            row[F_ENABLED] = bool(enabled)
        result = await client.upsert(collection_name=name, data=rows)
        # 同样要 flush：否则"刚启用"的切片在下一跳检索里仍不可见，
        # 用户会看到"启用了但问答还是搜不到"，然后反复点启用
        await self.flush()
        count = int(result.get("upsert_count", len(rows))) \
            if isinstance(result, dict) else len(rows)
        logger.info("切片启停：doc_id=%s enabled=%s 受影响=%d", doc_id, enabled, count)
        return count

    async def query_chunks(self, doc_id: str, *, offset: int = 0, limit: int = 20,
                           keyword: str = "") -> tuple[list[dict[str, Any]], int]:
        """按 `doc_id` 翻页取切片（**只读**，模块 04 §3.7 切片预览用）。

        返回 `(rows, total)`；`rows` 在 Python 侧按 `chunk_index` 升序排好。

        ⚠️ **为什么要在应用层排序**：Milvus 的 `query` **不保证返回顺序**
        （它按内部段与主键扫描）。而切片预览的语义是"按文档原始顺序看第 1~20 片"，
        顺序乱了会出现"翻到第 2 页看到第 1 页的内容"这种明显 bug。
        代价是分页边界依赖 Milvus 的稳定返回——同一集合、同一 filter 下
        `offset/limit` 在实践中稳定（段不变则顺序不变），且切片写完即 `flush()`，
        预览时不会再有新段产生。
        """
        client = self.require()
        name = settings.chunks_collection
        expr = f'{F_DOC_ID} == "{doc_id}"'
        if keyword:
            # Milvus 的 `like` 是**前缀/包含**匹配（`%` 通配）。关键词里的引号必须
            # 转义，否则会拼出一个语法错误的 filter，表现为"预览直接 500"
            safe = keyword.replace("\\", "").replace('"', "")
            expr += f' and ({F_CONTENT} like "%{safe}%" or {F_TITLE} like "%{safe}%")'
        total = await self.count(expr)
        rows = await client.query(collection_name=name, filter=expr,
                                  output_fields=list(ALL_FIELDS),
                                  offset=max(0, int(offset)), limit=max(1, int(limit)))
        rows = sorted(rows, key=lambda r: int(r.get(F_CHUNK_INDEX, 0)))
        return rows, total

    async def search(self, *, dense: Sequence[float], limit: int = 10,
                     expr: str = "", output_fields: Sequence[str] | None = None
                     ) -> list[dict[str, Any]]:
        """dense 向量检索（**只读**；06 走这条路）。

        `expr` 由调用方给出，且**只允许** `enabled == true`（+ 可选 `doc_id in [...]`）
        ——ER-11：不在 Milvus 侧做权限过滤，权限变更才能即时生效。

        ★ **必须显式指定 `anns_field=F_DENSE`**：E01 的集合里有**两个**向量字段
        （`dense_vector` 与 `sparse_vector` 占位），Milvus 无法自行判断要搜哪一个，
        会直接报 `code=65535 multiple anns_fields exist, please specify a anns_field
        in search_params`。不指定时的症状极具迷惑性：**每一次检索都失败**，
        问答一律回落成 `no_knowledge`、看板的召回/拦截全是 0、缺口聚合识别到 0 条
        （因为降级轮次被 `_is_gap()` 正确排除了）——看起来像"知识库是空的"，
        而入库、切片预览都完全正常（`query()` 不需要 `anns_field`）。
        稀疏向量只是占位（`sparse_placeholder` 降级），本期检索是**纯稠密**，
        所以这里指定稠密字段是唯一正确的选择。

        ★ **`output_fields` 必须带上 `F_CHUNK_ID`**：切片主键是"引用了哪一片"的
        唯一标识。漏了它的症状同样是"看起来都好、细看全是空"：
        `qa_logs` 的三列表里 `chunk_id` 全为 `null`，引用卡片定位不到切片，
        而 07 的 FAQ 挖掘会把 `str(None)` 当成文档号存进候选的 `related_docs`，
        导致**审核通过永久报 404/409「关联知识单元不存在」**（`FAQ-3004`）。
        """
        result = await self.require().search(
            collection_name=settings.chunks_collection, data=[list(dense)],
            anns_field=F_DENSE,
            limit=limit, filter=expr or "",
            output_fields=list(output_fields or [F_CHUNK_ID, F_DOC_ID, F_CHUNK_INDEX,
                                                 F_CONTENT, F_TITLE, F_PARENT_TITLE,
                                                 F_FILE_TITLE, F_PART, F_ENABLED]))
        hits: list[dict[str, Any]] = []
        for group in result or []:
            for hit in group:
                item = dict(hit.get("entity") or {})
                item["score"] = hit.get("distance")
                hits.append(item)
        return hits


milvus = MilvusStore()

__all__ = [
    "MilvusStore", "MilvusUnavailable", "milvus",
    "F_CHUNK_ID", "F_DOC_ID", "F_CHUNK_INDEX", "F_CONTENT", "F_DENSE", "F_SPARSE",
    "F_ENABLED", "F_TITLE", "F_PARENT_TITLE", "F_FILE_TITLE", "F_PART", "ALL_FIELDS",
]
