# -*- coding: utf-8 -*-
"""模块 03 的数据访问层：**E04 `kb_documents` / E05 `kb_categories` 的唯一写入者**（ER-02）。

两条与别处不同的**存储层约束**写在本文件里而不是服务层：
它们保护的是"数据不可能变形"，而不是"业务规则"。

| 约束 | 落点 | 为什么必须在存储层 |
|---|---|---|
| `file_hash` 唯一 | 索引 1 | 并发上传同一文件时，应用层的"先查后插"必然漏一个；只有唯一索引挡得住 |
| **索引缺失即拒写** | `assert_dedup_index_ready()` | 缺索引仍建台账会静默产生重复知识单元（ER-04） |

编号 `DOC{yyyyMMdd}{6位序列}` 由 `next_doc_id()` 生成，**日期段与 `created_at` 同源**
（与模块 10 的审计编号同法），同样采用"先查库最大值"的惰性对齐，避免重启撞号。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from pymongo import UpdateOne

from app.core.enums import DocStatus, ImportStatus
from app.core.logging import logger
from app.infra.mongo import mongo

DOCUMENTS = "kb_documents"
CATEGORIES = "kb_categories"
# 切片集合（E01，归属 04）：本模块**只读**，仅用于"文档被停用后切片是否同步"的自检
CHUNKS = "kb_chunks_v2"

DUPLICATE_KEY = 11000

# 同级唯一的哨兵：Mongo 唯一索引把多个 `null` 视为重复，根分类会有多个
ROOT_SENTINEL = "__root__"
DOC_ID_WIDTH = 6


def _parent_key(parent_id: str | None) -> str:
    """`parent_id` 归一成索引用哨兵值（与 02 模块部门树完全同法）。"""
    return parent_id or ROOT_SENTINEL


# --------------------------------------------------------------------------- 索引
async def ensure_indexes() -> None:
    """建 E04/E05 的 10 条索引（模块 03 §2.3 逐条对应）。

    **幂等**：`create_index` 对同名同键是幂等的，重复启动不会重建。
    """
    db = mongo.require_db()

    docs = db[DOCUMENTS]
    # 1 去重的唯一依据。注意它**不豁免软删除**（§2.3 的说明）：已进回收站的文件
    #   仍占着哈希，复用会得到"上传成功但检索不到"的假成功，因此服务层返回 DOC-3004
    await docs.create_index("file_hash", unique=True, name="uq_file_hash")
    # 1b **缺口转建的幂等键**（模块 08 的跨模块契约）：同一 `gap_id` 只能产出
    #    一篇占位文档。**稀疏**是必须的 —— 绝大多数文档没有 `source_gap_id`，
    #    普通唯一索引会把"多个缺失值"判成重复（Mongo 把 null 视为值）。
    await docs.create_index("source_gap_id", unique=True, sparse=True,
                            name="uq_source_gap_id")
    # 2 台账主查询（选了分类 + 状态，按更新时间倒序）
    await docs.create_index([("deleted_at", 1), ("category_id", 1), ("status", 1),
                             ("updated_at", -1)], name="ix_deleted_cat_status_updated")
    # 3 「全部分类」时的列表查询。**必须单独建**：索引 2 的前缀是
    #   `deleted_at + category_id`，不选分类时 category_id 位置空，无法利用后续字段排序
    await docs.create_index([("deleted_at", 1), ("updated_at", -1)],
                            name="ix_deleted_updated")
    # 4 原型筛选项「全部权限 ▾」
    await docs.create_index([("deleted_at", 1), ("permission_summary.label", 1)],
                            name="ix_deleted_perm_label")
    # 5 搜索框是「搜索标题 / 文件名」，两列都要搜（Step 1 只写了 title，本步扩为复合）
    await docs.create_index([("title", "text"), ("file_name", "text")],
                            name="tx_title_file_name",
                            weights={"title": 3, "file_name": 1})
    await docs.create_index([("created_at", -1)], name="ix_created")
    # 7 04 的回填 / 补偿扫描；本模块只读
    await docs.create_index([("import_status", 1), ("updated_at", 1)],
                            name="ix_import_status_updated")

    cats = db[CATEGORIES]
    await cats.create_index([("parent_id", 1), ("name", 1)], unique=True,
                            name="uq_parent_name")
    await cats.create_index("path_ids", name="ix_path_ids")
    await cats.create_index([("level", 1), ("sort", 1)], name="ix_level_sort")

    logger.info("知识单元与分类索引已确保（%d 条）", 10)


async def assert_dedup_index_ready() -> None:
    """校验 `file_hash` 唯一索引存在；缺失即抛 `DOC-4004`（**fail-closed**）。

    ER-04 的动机在这里最直白：唯一索引是去重的**唯一**硬保障。它缺了而接口照常
    建台账，结果就是"两篇一模一样的知识"，而且是**检索时才被发现**——
    宁可上传失败，不能静默产生重复。
    """
    from app.core.errors import BizError, Err

    try:
        cursor = await mongo.collection(DOCUMENTS).list_indexes()
        rows = await cursor.to_list(length=None)
    except Exception as exc:                                  # noqa: BLE001
        raise BizError(Err.DOC_STORE_FAILED, f"索引自检失败：{exc}") from exc
    spec = next((i for i in rows if i["name"] == "uq_file_hash"), None)
    if spec is None or not spec.get("unique"):
        logger.critical("kb_documents.file_hash 唯一索引缺失，拒绝建台账（DOC-4004）")
        raise BizError(Err.DOC_DEDUP_INDEX_MISSING,
                       "去重索引未就绪：kb_documents.file_hash 唯一索引缺失")


async def dedup_index_ready() -> bool:
    """只回答"索引在不在"，不抛异常（供 `/health` 与启动自检用）。"""
    try:
        cursor = await mongo.collection(DOCUMENTS).list_indexes()
        rows = await cursor.to_list(length=None)
    except Exception:                                         # noqa: BLE001
        return False
    spec = next((i for i in rows if i["name"] == "uq_file_hash"), None)
    return bool(spec and spec.get("unique"))


# --------------------------------------------------------------------------- 编号
async def next_doc_id(ts_ms: int) -> tuple[str, str]:
    """生成 `(_id, doc_no)`。

    - `_id` = `DOC{yyyyMMdd}{6位零填充序列}`（保容量与稳定排序）
    - `doc_no` = `DOC{yyyyMMdd}{序列去前导零后至少 3 位}`（**与原型逐字一致**）

    两者一次算好、一起落库：换算规则只写在这一处，避免前端各实现一套。
    """
    date = datetime.fromtimestamp(ts_ms / 1000).strftime("%Y%m%d")
    prefix = f"DOC{date}"
    row = await mongo.collection(DOCUMENTS).find_one(
        {"_id": {"$regex": f"^{prefix}\\d{{{DOC_ID_WIDTH}}}$"}}, {"_id": 1},
        sort=[("_id", -1)])
    seq = int(str(row["_id"])[len(prefix):]) + 1 if row else 1
    doc_id = f"{prefix}{seq:0{DOC_ID_WIDTH}d}"
    doc_no = f"{prefix}{str(seq).zfill(3)}"
    return doc_id, doc_no


# --------------------------------------------------------------------------- E04 读
async def get(doc_id: str) -> dict[str, Any] | None:
    """按编号取知识单元（含已软删除的，由调用方判断 `deleted_at`）。"""
    return await mongo.collection(DOCUMENTS).find_one({"_id": doc_id})


async def find_by_hash(file_hash: str) -> dict[str, Any] | None:
    """按 SHA256 找已有知识单元（去重决策的唯一依据，§3.7）。"""
    return await mongo.collection(DOCUMENTS).find_one({"file_hash": file_hash})


async def find_by_hashes(file_hashes: Sequence[str]) -> dict[str, dict[str, Any]]:
    """批量按哈希取（一次 `$in`）：去重预检最多 200 条，逐条查就是 200 次往返。"""
    if not file_hashes:
        return {}
    cursor = mongo.collection(DOCUMENTS).find({"file_hash": {"$in": list(file_hashes)}})
    rows = await cursor.to_list(length=None)
    return {r["file_hash"]: r for r in rows}


async def exists(doc_id: str) -> bool:
    """编号是否存在（供 05/06/08 的只读校验用）。"""
    return await get(doc_id) is not None


async def find_by_ids(doc_ids: Sequence[str]) -> list[dict[str, Any]]:
    """批量按编号取（一次 `$in`）：导入队列要显示 `doc_title`，
    逐条查会让"50 条队列"变成 50 次数据库往返——列表接口的首屏延迟就是这么来的。
    """
    if not doc_ids:
        return []
    cursor = mongo.collection(DOCUMENTS).find(
        {"_id": {"$in": list(doc_ids)}},
        # `category_id` 也带上：模块 08 要用它做"建议创建分类"的众数投票
        # （只投影 `title`/`status` 会让那一步永远投不出结果 → 建议恒为 null）
        # `deleted_at` 给模块 09 的热门知识榜用：软删的文档**仍展示**
        # （历史引用事实不变），但必须带 `deleted:true` 标记
        {"title": 1, "status": 1, "category_id": 1, "deleted_at": 1})
    return await cursor.to_list(length=None)


async def find_categories_by_ids(category_ids: Sequence[str]
                                 ) -> dict[str, dict[str, Any]]:
    """批量取分类（一次 `$in`）——模块 08 要拼"建议创建分类"的路径。

    没有它，50 条缺口就要查 50 次分类表（ER-13 要拦的 N+1）。
    """
    wanted = [c for c in dict.fromkeys(category_ids) if c]
    if not wanted:
        return {}
    cursor = mongo.collection(CATEGORIES).find(
        {"_id": {"$in": wanted}}, {"name": 1, "path": 1, "status": 1})
    return {str(row["_id"]): row for row in await cursor.to_list(length=None)}


async def find_by_source_gap(gap_id: str) -> dict[str, Any] | None:
    """按 `source_gap_id` 查占位文档（模块 08 转建的幂等键）。"""
    if not gap_id:
        return None
    return await mongo.collection(DOCUMENTS).find_one({"source_gap_id": gap_id})


async def hard_delete(doc_id: str) -> int:
    """**物理删除**台账（只有"缺口转建的占位被回滚"这一条路径用它）。

    ⚠️ 与软删除的区别必须守住：业务上的"删文档"永远是软删除（AD-05）。
    这里删的是**从未有过内容的占位**（无切片、无上传），
    它的存在本身就是一次失败的转建留下的残渣——留着会让知识台账里
    多出一条永远导不进来的空记录。
    """
    result = await mongo.collection(DOCUMENTS).delete_one({"_id": doc_id})
    return int(result.deleted_count)


# --------------------------------------------------------------------------- E04 写
async def insert(doc: Mapping[str, Any]) -> str:
    """建台账。`file_hash` 冲突**向上抛 DuplicateKeyError**，由服务层改判。"""
    await mongo.collection(DOCUMENTS).insert_one(dict(doc))
    return str(doc["_id"])


async def update(doc_id: str, fields: Mapping[str, Any]) -> int:
    """更新字段（`updated_at` 由服务层一并传入）。"""
    result = await mongo.collection(DOCUMENTS).update_one({"_id": doc_id},
                                                          {"$set": dict(fields)})
    return result.modified_count


async def soft_delete(doc_id: str, actor: str, ts_ms: int) -> int:
    """软删除：只写 `deleted_at` / `deleted_by`（**不物理删除**，ER-15）。"""
    result = await mongo.collection(DOCUMENTS).update_one(
        {"_id": doc_id, "deleted_at": None},
        {"$set": {"deleted_at": ts_ms, "deleted_by": actor, "updated_at": ts_ms,
                  "updated_by": actor}})
    return result.modified_count


async def restore(doc_id: str, actor: str, ts_ms: int) -> int:
    """从回收站恢复（清空 `deleted_at` / `deleted_by`）。"""
    result = await mongo.collection(DOCUMENTS).update_one(
        {"_id": doc_id},
        {"$set": {"deleted_at": None, "deleted_by": None, "updated_at": ts_ms,
                  "updated_by": actor}})
    return result.modified_count


async def mark_import_done(doc_id: str, chunk_count: int, char_count: int | None,
                           ts_ms: int, *, enable: bool = True) -> int:
    """04 导入完成后的回填（`chunk_count` / `char_count` / `import_status=done`）。

    `enable=True`（默认）顺带把 `status` 置 `enabled`（概要设计 §3.1 末步）：
    **导入完成即可检索**，否则用户要"上传完再去列表点一次启用"，
    而那时列表上那条记录还是灰色的。

    `enable=False` 时**只写 `import_status=done`，不动 `status`**：
    这是上传时选了"导入后不自动启用"（Spec §3.1 `auto_enable=false`）的路径。
    两者必须区分——把 `auto_enable=false` 也写成 `enabled`，
    等于用户明确说了"先别启用"而系统擅自启用了。
    """
    fields: dict[str, Any] = {
        "chunk_count": int(chunk_count), "import_status": ImportStatus.DONE.value,
        "updated_at": ts_ms}
    if enable:
        fields["status"] = DocStatus.ENABLED.value
    if char_count is not None:
        fields["char_count"] = int(char_count)
    return await update(doc_id, fields)


async def mark_import_failed(doc_id: str, stage: str, code: str, message: str,
                             ts_ms: int) -> int:
    """04 导入失败：置 `import_status=failed`，台账保留待重试。

    失败原因进 `import_error`（不在 Spec 的字段表里，是**排障必需**的最小扩展）：
    没有它就只剩一个 `failed`，管理员无法知道是解析失败还是向量化失败。
    """
    return await update(doc_id, {
        "import_status": ImportStatus.FAILED.value, "updated_at": ts_ms,
        "import_error": {"stage": stage, "code": code, "message": message,
                         "at": ts_ms}})


async def update_permission_summary(doc_id: str, summary: Mapping[str, Any],
                                    ts_ms: int) -> int:
    """05 写完 E07 后回填**展示用**摘要（`label` 由 03 计算，见 §3.14）。"""
    return await update(doc_id, {"permission_summary": dict(summary),
                                 "updated_at": ts_ms})


async def update_import_status(doc_id: str, import_status: str, ts_ms: int) -> int:
    """推进 `import_status`（04 用：`parsing` / `embedding` / `failed`）。

    **为什么不复用 `mark_import_done` / `mark_import_failed`**：这两个是"结局"，
    而流水线中途要把 `parsing`、`embedding` 这些**过程态**写进去——原型 `02`
    的队列列显示"解析中/向量化中"，靠的就是这个字段。若中途不写，
    用户在整个导入期间看到的都是 `pending`，无法区分"在跑"与"没跑起来"。
    """
    return await update(doc_id, {"import_status": import_status, "updated_at": ts_ms})


async def update_storage(doc_id: str, storage: Mapping[str, Any], ts_ms: int) -> int:
    """回填对象定位（04 落盘成功后写 `storage`）。

    落盘**先于**建台账（Spec §3.1 的顺序调整），所以正常路径下 `storage`
    是随 `create()` 一起写进去的；本方法服务于两条旁路：
    ① MinIO 失败降级本地后，`storage_degraded` / `local_path` 需要补写；
    ② 重试时中间产物（MD / 图片）的对象键发生变化。
    """
    return await update(doc_id, {"storage": dict(storage), "updated_at": ts_ms})



# --------------------------------------------------------------------------- E04 查列表
def _list_filter(*, deleted: bool, category_ids: Sequence[str] | None,
                 status: str | None, permission_label: str | None) -> dict[str, Any]:
    query: dict[str, Any] = {} if deleted else {"deleted_at": None}
    if deleted:
        query["deleted_at"] = {"$ne": None}
    if category_ids is not None:
        query["category_id"] = {"$in": list(category_ids)}
    if status:
        query["status"] = status
    if permission_label:
        query["permission_summary.label"] = permission_label
    return query


async def count_documents(*, deleted: bool = False,
                          category_ids: Sequence[str] | None = None,
                          status: str | None = None,
                          permission_label: str | None = None,
                          keyword: str | None = None) -> int:
    """台账列表总数（分页契约要 `total`）。"""
    return await mongo.collection(DOCUMENTS).count_documents(
        _keyword_merge(_list_filter(deleted=deleted, category_ids=category_ids,
                                    status=status, permission_label=permission_label),
                       keyword))


def _keyword_merge(query: dict[str, Any], keyword: str | None) -> dict[str, Any]:
    """关键字走 `$text`（标题权重 3 / 文件名权重 1，索引 5）。

    中文短词用 `$text` 会分词失败，因此**同时**兜一个正则——两条路取并集，
    宁可多查一次也不让"搜得到却搜不出来"。理由同 02 模块的 `DEC-02-3`。
    """
    import re

    if not keyword or not keyword.strip():
        return query
    pattern = re.escape(keyword.strip())
    regex = {"$or": [{"title": {"$regex": pattern, "$options": "i"}},
                     {"file_name": {"$regex": pattern, "$options": "i"}}]}
    return {"$and": [query, regex]} if query else regex


async def list_documents(*, deleted: bool = False,
                         category_ids: Sequence[str] | None = None,
                         status: str | None = None, permission_label: str | None = None,
                         keyword: str | None = None, sort_by: str = "updated_at",
                         desc: bool = True, skip: int = 0,
                         limit: int = 20) -> list[dict[str, Any]]:
    """台账列表分页。排序键白名单由服务层保证（这里只认 `updated_at` / `created_at`）。"""
    field = "created_at" if sort_by == "created_at" else "updated_at"
    cursor = (mongo.collection(DOCUMENTS)
              .find(_keyword_merge(_list_filter(deleted=deleted,
                                                category_ids=category_ids,
                                                status=status,
                                                permission_label=permission_label),
                                   keyword))
              .sort(field, -1 if desc else 1).skip(skip).limit(limit))
    return await cursor.to_list(length=limit)


async def summary_counts(*, category_ids: Sequence[str] | None = None,
                         status: str | None = None,
                         permission_label: str | None = None,
                         keyword: str | None = None) -> dict[str, int]:
    """列表汇总（原型 `dlCount`）：**不受分页影响**的前四项 + 回收站总数。

    用一次 `$facet` 拿全部计数，避免为 5 个数字跑 5 次 `count_documents`。
    """
    base = _keyword_merge(_list_filter(deleted=False, category_ids=category_ids,
                                       status=status,
                                       permission_label=permission_label), keyword)
    pipeline = [{"$match": base}, {"$facet": {
        "total": [{"$count": "n"}],
        "enabled": [{"$match": {"status": DocStatus.ENABLED.value}}, {"$count": "n"}],
        "disabled": [{"$match": {"status": DocStatus.DISABLED.value}}, {"$count": "n"}],
        "importing": [{"$match": {"import_status": {"$in": [
            ImportStatus.PENDING.value, ImportStatus.PARSING.value,
            ImportStatus.EMBEDDING.value]}}}, {"$count": "n"}],
    }}]
    cursor = await mongo.collection(DOCUMENTS).aggregate(pipeline)
    rows = await cursor.to_list(length=1)
    facet = rows[0] if rows else {}

    def pick(key: str) -> int:
        bucket = facet.get(key) or []
        return int(bucket[0]["n"]) if bucket else 0

    deleted_total = await mongo.collection(DOCUMENTS).count_documents(
        {"deleted_at": {"$ne": None}})
    return {"total": pick("total"), "enabled": pick("enabled"),
            "disabled": pick("disabled"), "importing": pick("importing"),
            "deleted": deleted_total}


# --------------------------------------------------------------------------- E05 分类
async def list_categories() -> list[dict[str, Any]]:
    """全部分类（按层级 + 同级排序，服务层组树）。"""
    cursor = mongo.collection(CATEGORIES).find({}).sort([("level", 1), ("sort", 1)])
    return await cursor.to_list(length=None)


async def get_category(category_id: str) -> dict[str, Any] | None:
    """按编号取分类。"""
    return await mongo.collection(CATEGORIES).find_one({"_id": category_id})


async def find_category_by_parent_name(parent_id: str | None,
                                       name: str) -> dict[str, Any] | None:
    """同级同名分类（唯一索引的哨兵口径由 `_parent_key` 统一）。"""
    return await mongo.collection(CATEGORIES).find_one(
        {"parent_id": _parent_key(parent_id), "name": name})


async def next_category_id() -> str:
    """下一个分类编号 `CAT{4位}`。"""
    row = await mongo.collection(CATEGORIES).find_one(
        {"_id": {"$regex": r"^CAT\d{4}$"}}, {"_id": 1}, sort=[("_id", -1)])
    seq = int(str(row["_id"])[3:]) + 1 if row else 1
    return f"CAT{seq:04d}"


async def insert_category(doc: Mapping[str, Any]) -> str:
    """插入分类（`parent_id` 自动归一成哨兵值）。"""
    payload = dict(doc)
    payload["parent_id"] = _parent_key(payload.get("parent_id"))
    await mongo.collection(CATEGORIES).insert_one(payload)
    return str(payload["_id"])


async def update_category(category_id: str, fields: Mapping[str, Any]) -> int:
    """更新分类字段。"""
    payload = dict(fields)
    if "parent_id" in payload:
        payload["parent_id"] = _parent_key(payload["parent_id"])
    result = await mongo.collection(CATEGORIES).update_one({"_id": category_id},
                                                           {"$set": payload})
    return result.modified_count


async def delete_category(category_id: str) -> int:
    """**硬删除**分类（纯组织维度，不承载正文/切片/权限，§2.2 的说明）。"""
    result = await mongo.collection(CATEGORIES).delete_one({"_id": category_id})
    return result.deleted_count


async def list_category_subtree(path_id: str) -> list[dict[str, Any]]:
    """某分类的整棵子树（含自身），靠 `path_ids` 一次查完。"""
    cursor = mongo.collection(CATEGORIES).find({"path_ids": path_id})
    return await cursor.to_list(length=None)


async def apply_category_paths(updates: Sequence[Mapping[str, Any]]) -> int:
    """批量重写子孙的 `path` / `path_ids` / `level`（移动/改名后的级联）。

    `ordered=True`：中途失败即停，由服务层转 `DOC-5002` 并附受影响清单——
    物化路径半截更新是最难查的坏数据。
    """
    if not updates:
        return 0
    ops = [UpdateOne({"_id": u["category_id"]},
                     {"$set": {"path": u["path"], "path_ids": u["path_ids"],
                               "level": u["level"], "updated_at": u["updated_at"]}})
           for u in updates]
    result = await mongo.collection(CATEGORIES).bulk_write(ops, ordered=True)
    return result.modified_count


async def count_category_children(category_id: str) -> int:
    """直接子分类数（删除前置 `DOC-3005`）。"""
    return await mongo.collection(CATEGORIES).count_documents(
        {"parent_id": category_id})


async def count_docs_in_categories(category_ids: Iterable[str],
                                   *, include_deleted: bool = False) -> int:
    """这些分类下（**含子分类**，由调用方传入完整 ID 集合）的文档数。

    `doc_count` 的语义是"含子分类的未软删除文档数"（§2.2），所以调用方要先把
    `path_ids` 展开成完整集合再传进来——展开用 `path_ids` 索引，一次查完。
    """
    query: dict[str, Any] = {"category_id": {"$in": list(category_ids)}}
    if not include_deleted:
        query["deleted_at"] = None
    return await mongo.collection(DOCUMENTS).count_documents(query)


async def doc_counts_by_category(*, include_deleted: bool = False
                                 ) -> dict[str, int]:
    """一次聚合出"每个分类直属文档数"；子分类的累加由服务层沿 `path_ids` 做。

    逐分类查会变成 N 次查询（分类树常有几十个节点），而这一步只是把
    "分类 → 直属文档数"一次拿回来，累加是纯内存操作。
    """
    pipeline: list[dict[str, Any]] = []
    if not include_deleted:
        pipeline.append({"$match": {"deleted_at": None}})
    pipeline.append({"$group": {"_id": "$category_id", "n": {"$sum": 1}}})
    cursor = await mongo.collection(DOCUMENTS).aggregate(pipeline)
    rows = await cursor.to_list(length=None)
    return {r["_id"]: int(r["n"]) for r in rows if r["_id"]}


async def update_doc_counts(pairs: Mapping[str, int], ts_ms: int) -> int:
    """批量写 `doc_count`（`/categories/recount` 与增删改后的增量维护共用）。"""
    if not pairs:
        return 0
    ops = [UpdateOne({"_id": cid}, {"$set": {"doc_count": int(count),
                                             "updated_at": ts_ms}})
           for cid, count in pairs.items()]
    result = await mongo.collection(CATEGORIES).bulk_write(ops, ordered=False)
    return result.modified_count


# --------------------------------------------------------------------------- 自检用
async def count_chunks_of(doc_id: str, *, enabled: bool | None = None) -> int:
    """数某文档的切片数（E01 属 04，本模块**只读**）。

    用途只有一个：验证"文档停用后切片是否同步停用"（§4.3 的样板流程）。
    集合不存在时返回 0——04 未落地时没有切片，这是正确的降级方向。
    """
    query: dict[str, Any] = {"doc_id": doc_id}
    if enabled is not None:
        query["enabled"] = enabled
    try:
        return await mongo.collection(CHUNKS).count_documents(query)
    except Exception:                                         # noqa: BLE001
        return 0


__all__ = [
    "DOCUMENTS", "CATEGORIES", "CHUNKS", "ROOT_SENTINEL", "DUPLICATE_KEY",
    "ensure_indexes", "assert_dedup_index_ready", "dedup_index_ready", "next_doc_id",
    "find_categories_by_ids", "find_by_source_gap", "hard_delete",
    "get", "find_by_hash", "find_by_hashes", "exists",
    "insert", "update", "soft_delete", "restore", "mark_import_done",
    "mark_import_failed", "update_permission_summary",
    "count_documents", "list_documents", "summary_counts",
    "list_categories", "get_category", "find_category_by_parent_name",
    "next_category_id", "insert_category", "update_category", "delete_category",
    "list_category_subtree", "apply_category_paths", "count_category_children",
    "count_docs_in_categories", "doc_counts_by_category", "update_doc_counts",
    "count_chunks_of",
]
