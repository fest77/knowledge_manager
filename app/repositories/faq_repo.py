# -*- coding: utf-8 -*-
"""模块 07 的数据访问层：**E16 候选 / E17 已发布 FAQ 的唯一写入者**（ER-02）。

## 三张集合与各自的角色

| 实体 | 集合 | 谁读 | 关键约束 |
|---|---|---|---|
| E16 候选 | `faq_candidates` | 审核页（原型 `05`） | **唯一索引 `cluster_key + window_start`** 防同窗重复生成 |
| E17 已发布 | `faqs` | 缓存重建、列表页 | **唯一索引 `question`**（normalize 后）防重复发布 |
| E18 缓存副本 | `faq_cache` | 启动兜底 / 排查 | 只是副本，**没有保留价值**（真源是 `faqs`） |

## 两个唯一索引的职责完全不同（容易混）

- `cluster_key + window_start`：只保证**同一窗口内不重复生成**。
  同一个簇在不同窗口是两次独立产出（`frequency` 要重算）；
  跨窗口的"不打扰"由**应用层**的「pending 复用 / rejected 抑制」两条规则承担。
- `faqs.question`：比较前先做 `normalize`（trim + 全角半角 + 折叠空白 + 小写）。
  没有它，"生鲜食品破损如何申请退款"与带尾部空格的同句会变成两条 FAQ，
  而两条 FAQ 会各自进缓存、命中同义问题时返回两个不同答案。

## 为什么这里不做"跨集合 join"

`related_docs` 的标题要靠 03 的只读接口补齐（Spec §3.1 R-03）。
把 join 写在仓储层会让"文档标题"变成候选表的冗余字段，
而文档改名之后候选里的标题就陈旧了——**展示用数据不落库**是这个项目一贯的口径。
"""
from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from typing import Any, Iterable, Mapping, Sequence

from app.core.enums import FaqCandidateStatus
from app.core.logging import logger
from app.infra.mongo import mongo

# E16 / E17 / E18 副本
CANDIDATES = "faq_candidates"
FAQS = "faqs"
CACHE_COPY = "faq_cache"

CANDIDATE_ID_PREFIX = "CAND"
FAQ_ID_PREFIX = "FAQ"
SEQ_WIDTH = 6
# 簇内保留的原始提问上限（Spec §2.1：超出按 `asked_at` 取最近 50 条）
MAX_CLUSTER_QUESTIONS = 50
# 别名上限（Spec §2.2）
MAX_ALIASES = 10


# =========================================================================== 文本归一
_PUNCT_TAIL = "。！？；：、，,.!?;:"
_WS = re.compile(r"\s+")


def normalize_question(text: str) -> str:
    """问法归一化（Spec §2.1 的定义，逐条落实）。

    `trim` + **全角转半角** + 去句末标点 + 连续空白折叠 + 英文小写。

    为什么这五步缺一不可：唯一索引比较的是归一化后的值，
    少任何一步都会让"看起来一样"的两个问法被判成不同 →
    重复发布 → 缓存里两条几乎相同的 FAQ → 同一个问题命中哪条取决于浮点误差。
    """
    value = unicodedata.normalize("NFKC", (text or "").strip())
    value = value.rstrip(_PUNCT_TAIL)
    value = _WS.sub(" ", value)
    return value.lower()


def cluster_key_of(representative: str) -> str:
    """`"ck_" + sha1(normalize(代表问法))[:16]`（Spec §2.1 的确定性定义）。

    用**代表问法**而不是簇内全部问法：同一簇跨窗口稳定，
    这样"上窗已驳回"的抑制名单才能跨窗口命中。
    """
    digest = hashlib.sha1(normalize_question(representative).encode("utf-8"))
    return f"ck_{digest.hexdigest()[:16]}"


def next_id(prefix: str, seq: int | None = None) -> str:
    """`CAND000001` / `FAQ000001`（本模块的编号只有一个全局序列，不按日期分段）。"""
    if seq is None:                                          # pragma: no cover
        seq = int(time.time()) % 1_000_000
    return f"{prefix}{seq:0{SEQ_WIDTH}d}"


# =========================================================================== 索引
async def ensure_indexes() -> None:
    """建 E16 / E17 / E18 副本的索引（Spec §2.1/§2.2）。"""
    db = mongo.require_db()
    candidates = db[CANDIDATES]
    # 防同窗重复生成（**不是**防跨窗重复——那交给应用层规则）
    await candidates.create_index([("cluster_key", 1), ("window_start", 1)],
                                  name="uq_cluster_window", unique=True)
    await candidates.create_index([("status", 1), ("frequency", -1)],
                                  name="ix_status_freq")
    await candidates.create_index([("cluster_key", 1), ("status", 1)],
                                  name="ix_cluster_status")
    await candidates.create_index([("last_seen_at", -1)], name="ix_last_seen")

    faqs = db[FAQS]
    # 比较前先 normalize（见模块头）：这里靠 `question_norm` 这个派生字段落唯一索引，
    # 因为 Mongo 的唯一索引没法对"表达式"建（除非用 partial + collation，
    # 而 collation 只能处理大小写、处理不了全角与句末标点）
    await faqs.create_index("question_norm", name="uq_question_norm", unique=True)
    await faqs.create_index([("enabled", 1), ("published_at", -1)],
                            name="ix_enabled_published")
    await faqs.create_index([("question", "text")], name="ix_question_text")
    await faqs.create_index("category_id", name="ix_category")

    await db[CACHE_COPY].create_index("faq_id", name="uq_faq_id", unique=True)
    logger.info("FAQ 索引已确保（%s / %s / %s）", CANDIDATES, FAQS, CACHE_COPY)


async def next_candidate_seq() -> int:
    """候选编号序列（查当日/当前最大 `_id` 再 +1，避免删记录后撞号）。"""
    return await _next_seq(CANDIDATES, CANDIDATE_ID_PREFIX)


async def next_faq_seq() -> int:
    """FAQ 编号序列。"""
    return await _next_seq(FAQS, FAQ_ID_PREFIX)


async def _next_seq(collection: str, prefix: str) -> int:
    row = await mongo.collection(collection).find_one(
        {"_id": {"$regex": f"^{prefix}\\d{{{SEQ_WIDTH}}}$"}}, {"_id": 1},
        sort=[("_id", -1)])
    return int(str(row["_id"])[len(prefix):]) + 1 if row else 1


# =========================================================================== E16 候选
async def upsert_candidate(doc: Mapping[str, Any]) -> bool:
    """按 `(cluster_key, window_start)` 幂等写入候选；返回"是否新建"。

    `$setOnInsert` 写 `_id` / `created_at`，`$set` 写本次挖掘算出来的值：
    重复挖掘同一窗口时**更新频次与置信度**，但**不改变 `window_start`**
    （它是唯一索引的一部分，改了就等于换了一条记录）。
    """
    key = {"cluster_key": doc["cluster_key"], "window_start": doc["window_start"]}
    payload = {k: v for k, v in doc.items()
               if k not in ("cluster_key", "window_start", "_id", "created_at")}
    existing = await mongo.collection(CANDIDATES).find_one(key, {"_id": 1})
    if existing:
        await mongo.collection(CANDIDATES).update_one(
            key, {"$set": {**payload, "updated_at": doc.get("updated_at")}})
        return False
    await mongo.collection(CANDIDATES).insert_one(dict(doc))
    return True


async def get_candidate(candidate_id: str) -> dict[str, Any] | None:
    """按编号取候选（审核与追溯用）。"""
    return await mongo.collection(CANDIDATES).find_one({"_id": candidate_id})


async def find_candidate(cluster_key: str, status: str | None = None,
                         *, window_start: int | None = None
                         ) -> dict[str, Any] | None:
    """按簇查最近一条候选（挖掘时的「pending 复用 / rejected 抑制」判据）。"""
    query: dict[str, Any] = {"cluster_key": cluster_key}
    if status:
        query["status"] = status
    if window_start is not None:
        query["window_start"] = window_start
    return await mongo.collection(CANDIDATES).find_one(
        query, sort=[("last_seen_at", -1)])


async def list_candidates(*, status: str | None = None, min_frequency: int | None = None,
                          keyword: str = "", page: int = 1, page_size: int = 20
                          ) -> tuple[list[dict[str, Any]], int]:
    """候选列表（默认按 `frequency desc, last_seen_at desc`，对齐原型行序）。"""
    query: dict[str, Any] = {}
    if status:
        query["status"] = status
    if min_frequency:
        query["frequency"] = {"$gte": int(min_frequency)}
    if keyword:
        query["$or"] = [{"representative_question": {"$regex": re.escape(keyword),
                                                     "$options": "i"}},
                        {"questions": {"$regex": re.escape(keyword), "$options": "i"}}]
    collection = mongo.collection(CANDIDATES)
    total = await collection.count_documents(query)
    cursor = (collection.find(query).sort([("frequency", -1), ("last_seen_at", -1)])
              .skip((page - 1) * page_size).limit(page_size))
    return await cursor.to_list(length=page_size), total


async def mark_candidate_reviewed(candidate_id: str, *, status: str, actor: str,
                                  note: str, ts_ms: int,
                                  faq_id: str | None = None) -> int:
    """审核留痕（`reviewed_by` / `reviewed_at` / `review_note`）。

    这三个字段是**唯一**能回答"这条 FAQ 当初为什么发"的记录 ——
    所以它们必须和状态一起写，不能只写 `status`。
    """
    fields: dict[str, Any] = {"status": status, "reviewed_by": actor,
                              "reviewed_at": ts_ms, "review_note": note,
                              "updated_at": ts_ms}
    if faq_id:
        fields["faq_id"] = faq_id
    result = await mongo.collection(CANDIDATES).update_one(
        {"_id": candidate_id}, {"$set": fields})
    return result.modified_count


async def distinct_cluster_keys(*, window_start: int | None = None) -> list[str]:
    """全部簇标识（测试核对"同参数二次运行产出相同簇集合"）。"""
    query = {"window_start": window_start} if window_start is not None else {}
    return list(await mongo.collection(CANDIDATES).distinct("cluster_key", query))


async def count_candidates(status: str | None = None) -> int:
    """候选条数（可按状态筛；看板与测试断言用）。"""
    query = {"status": status} if status else {}
    return await mongo.collection(CANDIDATES).count_documents(query)


# =========================================================================== E17 已发布
async def insert_faq(doc: Mapping[str, Any]) -> str:
    """落一条 FAQ。`question_norm` 冲突**向上抛** `DuplicateKeyError`（服务层转 `FAQ-3003`）。"""
    await mongo.collection(FAQS).insert_one(dict(doc))
    return str(doc["_id"])


async def get_faq(faq_id: str) -> dict[str, Any] | None:
    """按编号取 FAQ（**含 `embedding`**；列表接口不返回它）。"""
    return await mongo.collection(FAQS).find_one({"_id": faq_id})


async def find_faq_by_question(question: str) -> dict[str, Any] | None:
    """按归一化问法查（唯一性预检，给出比"数据库报冲突"更友好的错误）。"""
    return await mongo.collection(FAQS).find_one(
        {"question_norm": normalize_question(question)})


async def find_faqs_by_questions(questions: Sequence[str]) -> list[dict[str, Any]]:
    """批量按问法取已发布 FAQ（**一次 `$in`**）。

    模块 09 的高频问题榜要用它回填 `faq_id`（Spec §3.4 出参）。
    逐条 `find_one` 会把"TOP10 榜单"变成 10 次数据库往返 —— 与 ER-13 同理，
    批量能一次做完的事不允许拆成 N 次。
    """
    keys = [normalize_question(q) for q in dict.fromkeys(questions) if q]
    if not keys:
        return []
    cursor = mongo.collection(FAQS).find(
        {"question_norm": {"$in": keys}}, {"question_norm": 1, "question": 1})
    return await cursor.to_list(length=None)


async def list_faqs(*, keyword: str = "", enabled: bool | None = None,
                    category_id: str | None = None, page: int = 1,
                    page_size: int = 20, with_embedding: bool = False
                    ) -> tuple[list[dict[str, Any]], int]:
    """已发布 FAQ 列表。

    ⚠️ **默认不投影 `embedding`**：1024 维数组会让 100 条的响应体膨胀到几百 KB
    （AC-07-32 要求 < 100 KB），而列表页根本不用它。
    """
    query: dict[str, Any] = {}
    if enabled is not None:
        query["enabled"] = bool(enabled)
    if category_id:
        query["category_id"] = category_id
    if keyword:
        query["question"] = {"$regex": re.escape(keyword), "$options": "i"}
    projection = None if with_embedding else {"embedding": 0}
    collection = mongo.collection(FAQS)
    total = await collection.count_documents(query)
    cursor = (collection.find(query, projection).sort("published_at", -1)
              .skip((page - 1) * page_size).limit(page_size))
    return await cursor.to_list(length=page_size), total


async def list_enabled_faqs(*, with_embedding: bool = True) -> list[dict[str, Any]]:
    """取全部 `enabled=true` 的 FAQ（缓存重建的数据源）。"""
    projection = None if with_embedding else {"embedding": 0}
    cursor = mongo.collection(FAQS).find({"enabled": True}, projection)
    return await cursor.to_list(length=None)


async def count_faqs(enabled: bool | None = None) -> int:
    """FAQ 条数（可按缓存生效筛；`cache_size == enabled_count` 的自检靠它）。"""
    query = {} if enabled is None else {"enabled": bool(enabled)}
    return await mongo.collection(FAQS).count_documents(query)


async def update_faq(faq_id: str, fields: Mapping[str, Any]) -> int:
    """更新一条 FAQ（`question_norm` 冲突同样向上抛）。"""
    result = await mongo.collection(FAQS).update_one({"_id": faq_id},
                                                    {"$set": dict(fields)})
    return result.modified_count


async def delete_faq(faq_id: str) -> int:
    """**物理删除**（Spec §2.2 的说明：`question` 是唯一索引，软删会让同问法无法重发）。"""
    result = await mongo.collection(FAQS).delete_one({"_id": faq_id})
    return result.deleted_count


async def inc_hit_counts(counts: Mapping[str, int]) -> int:
    """批量累加 `hit_count`（**只由本模块写**，AC-07-18）。

    命中是高频读路径，逐次 `$inc` 会把"毫秒级直出"拖成"毫秒级 + 一次写库"；
    所以缓存里先在内存累加，由 scheduler 按 `faq.hit_flush_interval_s` 批量落库。
    """
    updated = 0
    for faq_id, delta in counts.items():
        if delta <= 0:
            continue
        result = await mongo.collection(FAQS).update_one(
            {"_id": faq_id}, {"$inc": {"hit_count": int(delta)}})
        updated += result.modified_count
    return updated


async def distinct_faq_questions() -> list[str]:
    """全部标准问法（测试核对唯一性与归一化）。"""
    return list(await mongo.collection(FAQS).distinct("question_norm"))


# =========================================================================== E18 副本
async def save_cache_copy(rows: Sequence[Mapping[str, Any]], *, dim: int,
                          model: str) -> int:
    """整批覆盖缓存副本（可选持久化副本）。

    ⚠️ **副本里不存 `answer`**：`faqs.answer` 是唯一真源，
    副本再存一份就有"两处答案不一致"的风险（ER-02 的同类问题）。
    """
    db = mongo.require_db()
    now = int(time.time() * 1000)
    await db[CACHE_COPY].delete_many({})
    if not rows:
        return 0
    payload = [{**dict(row), "dim": dim, "model": model, "rebuilt_at": now,
                "version": 1} for row in rows]
    await db[CACHE_COPY].insert_many(payload)
    return len(payload)


async def load_cache_copy() -> list[dict[str, Any]]:
    """读副本（启动兜底 / 排查比对）。"""
    cursor = mongo.collection(CACHE_COPY).find({})
    return await cursor.to_list(length=None)


async def drop_collections() -> None:
    """清空三张集合（仅测试路径调用）。"""
    db = mongo.require_db()
    for name in (CANDIDATES, FAQS, CACHE_COPY):
        await db.drop_collection(name)


async def status_counts() -> dict[str, int]:
    """三张集合的条数（`/faq/cache/status` 的一致性自检）。"""
    return {"candidates": await count_candidates(),
            "faqs": await count_faqs(),
            "enabled": await count_faqs(enabled=True),
            "cache_copy": await mongo.collection(CACHE_COPY).count_documents({})}


def clamp_questions(questions: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """簇内提问按 `asked_at` 取最近 N 条（Spec §2.1 的 50 条上限）。"""
    ordered = sorted(questions, key=lambda q: (q.get("asked_at") or 0,
                                               str(q.get("question") or "")))
    return [dict(q) for q in ordered[-MAX_CLUSTER_QUESTIONS:]]


def clean_aliases(aliases: Sequence[str] | None, question: str) -> list[str]:
    """校验并去重别名（≤10 条、单条 2~200、不得与标准问法重复）。

    与 `question` 重复的别名会被 `normalize` 后判等 —— 那会让向量化输入里
    同一句话出现两次，人为抬高它与自身的相似度（没有意义，还稀释了其它词）。
    """
    from app.core.errors import BizError, Err

    if aliases is None:
        return []
    if isinstance(aliases, (str, bytes)) or not isinstance(aliases,
                                                           (list, tuple, set,
                                                            frozenset)):
        raise BizError(Err.FAQ_ALIAS_INVALID, "aliases 必须是数组")
    out: list[str] = []
    base = normalize_question(question)
    for item in aliases:
        text = str(item or "").strip()
        if not (2 <= len(text) <= 200):
            raise BizError(Err.FAQ_ALIAS_INVALID, f"同义问法长度需在 2~200 字：{text!r}")
        if normalize_question(text) == base:
            raise BizError(Err.FAQ_ALIAS_INVALID, "同义问法不得与标准问法重复")
        if text not in out:
            out.append(text)
    if len(out) > MAX_ALIASES:
        raise BizError(Err.FAQ_ALIAS_INVALID, f"同义问法最多 {MAX_ALIASES} 条")
    return out


def is_duplicate_key_error(exc: Exception) -> bool:
    """判断是否是唯一索引冲突（服务层据此转 `FAQ-3003`）。"""
    return exc.__class__.__name__ == "DuplicateKeyError"


__all__ = [
    "CANDIDATES", "FAQS", "CACHE_COPY", "MAX_CLUSTER_QUESTIONS", "MAX_ALIASES",
    "normalize_question", "cluster_key_of", "next_id",
    "ensure_indexes", "next_candidate_seq", "next_faq_seq",
    "upsert_candidate", "get_candidate", "find_candidate", "list_candidates",
    "mark_candidate_reviewed", "distinct_cluster_keys", "count_candidates",
    "insert_faq", "get_faq", "find_faq_by_question", "list_faqs", "list_enabled_faqs",
    "count_faqs", "update_faq", "delete_faq", "inc_hit_counts",
    "distinct_faq_questions",
    "save_cache_copy", "load_cache_copy", "drop_collections", "status_counts",
    "clamp_questions", "clean_aliases", "is_duplicate_key_error",
    "FaqCandidateStatus",
]
