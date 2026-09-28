# -*- coding: utf-8 -*-
"""模块 06 的数据访问层：**E14 会话 / E02 消息 / E15 问答日志的唯一写入者**（ER-02 / ER-06）。

## 三张表的关系与各自的存在理由

| 实体 | 集合 | 谁读 | 为什么单独存在 |
|---|---|---|---|
| E14 会话 | `chat_sessions` | 侧边栏 | 会话级元信息（标题 / 轮数 / 最后活跃）——列表页不能去扫消息表 |
| E02 消息 | `chat_messages` | 会话回放 | 消息级内容（含 `chunk_refs` 溯源卡片、`denied_count` 提示） |
| E15 问答日志 | `qa_logs` | **09 看板 / 07 FAQ / 08 缺口** | 一表两用：运营指标源 + 挖掘原料。**它的读者全是别的模块** |

## 为什么 `qa_logs` 的字段比"日志"多得多

它不是调试日志，而是**三张报表的数据源**：
`recalled_chunks` / `allowed_chunks` / `denied_chunks` 三列表是"鉴权过滤到底拦掉了什么"
的唯一证据（标注 6）；`max_score` 是 08 判知识缺口的输入；分段耗时
（`retrieval_ms` / `auth_ms` / `rerank_ms` / `llm_ms`）用来定位性能瓶颈，
其中 `auth_ms` 是 2.9 新增的观测点——**它必须小**，否则"鉴权拖慢了问答"没人发现。

## 三个必须写清的实现选择

1. **`qa_logs` 建 TTL 索引（180 天）**，而 `chat_sessions` / `chat_messages` **永久保留**：
   日志是统计原料（过期可接受，看板看的是趋势），会话是用户的资产（删了就是丢数据）。
2. **`request_id` 唯一稀疏索引**：幂等键。稀疏是必须的——不传 `request_id`
   的请求会让该字段缺失，普通唯一索引会把"多个缺失值"判成重复（Mongo 把 null 视为值）。
3. **`recalled_chunks` 里连被拦截的切片一起记**：只记 allowed 的话，
   事后无法回答"这次到底召回了什么、为什么没答上来"。
"""
from __future__ import annotations

import time
from typing import Any, Iterable, Mapping

from app.core.logging import logger
from app.infra.mongo import mongo

# E14 / E02 / E15（集合名遵循数据实体设计；注意是复数 `chat_messages`）
SESSIONS = "chat_sessions"
MESSAGES = "chat_messages"
QA_LOGS = "qa_logs"

SESSION_ID_PREFIX = "SESS"
QA_LOG_ID_PREFIX = "QL"
SEQUENCE_WIDTH = 6


async def ensure_indexes() -> None:
    """建三张表的索引（模块 06 §2.1~§2.3）。"""
    db = mongo.require_db()
    sessions = db[SESSIONS]
    # 侧边栏：按人按时间倒序（"我的最近会话"）
    await sessions.create_index([("user_id", 1), ("last_active_at", -1)],
                                name="ix_user_active")

    messages = db[MESSAGES]
    # 会话回放：按会话取全部消息并保证顺序
    await messages.create_index([("session_id", 1), ("ts", 1)], name="ix_session_ts")

    logs = db[QA_LOGS]
    # 看板：时间倒序 + TTL 180 天（`expireAfterSeconds=0` 表示"按字段值过期"）
    from app.core.config import settings

    await logs.create_index([("asked_at", -1)], name="ix_asked_at",
                            expireAfterSeconds=settings.qa_log_ttl_days * 86400)
    await logs.create_index([("user_id", 1), ("asked_at", -1)], name="ix_user_asked")
    await logs.create_index("session_id", name="ix_session")
    await logs.create_index("faq_hit", name="ix_faq_hit")
    await logs.create_index("max_score", name="ix_max_score")
    # 幂等键：**稀疏唯一**（见模块头 ②）
    await logs.create_index("request_id", name="uq_request_id", unique=True,
                            sparse=True)
    logger.info("问答索引已确保（%s / %s / %s）", SESSIONS, MESSAGES, QA_LOGS)


# =========================================================================== 编号
async def _next_sequence(collection: str, prefix: str, ts_ms: int) -> str:
    """`{prefix}{yyyyMMdd}{6位序列}`（与 DOC / IMP 同一套做法）。"""
    from datetime import datetime

    date = datetime.fromtimestamp(ts_ms / 1000).strftime("%Y%m%d")
    head = f"{prefix}{date}"
    row = await mongo.collection(collection).find_one(
        {"_id": {"$regex": f"^{head}\\d{{{SEQUENCE_WIDTH}}}$"}}, {"_id": 1},
        sort=[("_id", -1)])
    seq = int(str(row["_id"])[len(head):]) + 1 if row else 1
    return f"{head}{seq:0{SEQUENCE_WIDTH}d}"


async def next_session_id(ts_ms: int) -> str:
    """会话编号 `SESS{yyyyMMdd}{6位}`。"""
    return await _next_sequence(SESSIONS, SESSION_ID_PREFIX, ts_ms)


async def next_log_id(ts_ms: int) -> str:
    """日志编号 `QL{yyyyMMdd}{6位}`。"""
    return await _next_sequence(QA_LOGS, QA_LOG_ID_PREFIX, ts_ms)


# =========================================================================== E14 会话
async def insert_session(doc: Mapping[str, Any]) -> str:
    """建会话（标题取首轮提问前 20 字，见 `qa_service`）。"""
    await mongo.collection(SESSIONS).insert_one(dict(doc))
    return str(doc["_id"])


async def get_session(session_id: str) -> dict[str, Any] | None:
    """按编号取会话（归属校验由服务层做）。"""
    return await mongo.collection(SESSIONS).find_one({"_id": session_id})


async def list_sessions(*, user_id: str, keyword: str = "", page: int = 1,
                        page_size: int = 20) -> tuple[list[dict[str, Any]], int]:
    """侧边栏：**仅本人**（G-12：历史问答不跨用户可见）按最后活跃倒序。"""
    query: dict[str, Any] = {"user_id": user_id}
    if keyword:
        # 只按标题搜：正文搜索要走消息表，代价与收益不成比例（侧边栏是"找回"入口）
        query["title"] = {"$regex": _escape(keyword), "$options": "i"}
    collection = mongo.collection(SESSIONS)
    total = await collection.count_documents(query)
    cursor = (collection.find(query).sort("last_active_at", -1)
              .skip((page - 1) * page_size).limit(page_size))
    return await cursor.to_list(length=page_size), total


async def touch_session(session_id: str, *, ts_ms: int, message_count: int) -> int:
    """更新最后活跃与消息数（每轮问答后调用）。"""
    result = await mongo.collection(SESSIONS).update_one(
        {"_id": session_id},
        {"$set": {"last_active_at": ts_ms, "message_count": int(message_count)}})
    return result.modified_count


async def count_sessions(user_id: str) -> int:
    """某人的会话总数（/health 与测试断言用）。"""
    return await mongo.collection(SESSIONS).count_documents({"user_id": user_id})


# =========================================================================== E02 消息
async def insert_message(doc: Mapping[str, Any]) -> str:
    """落一条消息，返回 `message_id`（Mongo 生成的 ObjectId 字符串）。"""
    result = await mongo.collection(MESSAGES).insert_one(dict(doc))
    return str(result.inserted_id)


async def list_messages(session_id: str, *, page: int = 1, page_size: int = 100
                        ) -> tuple[list[dict[str, Any]], int]:
    """按会话取消息（按时间升序，回放顺序即对话顺序）。"""
    collection = mongo.collection(MESSAGES)
    total = await collection.count_documents({"session_id": session_id})
    cursor = (collection.find({"session_id": session_id}).sort("ts", 1)
              .skip((page - 1) * page_size).limit(page_size))
    return await cursor.to_list(length=page_size), total


async def recent_messages(session_id: str, limit: int) -> list[dict[str, Any]]:
    """取最近 N 条消息（查询改写的上下文）。

    先按时间**倒序**取 N 条再反转：正序 + `limit` 取到的是"最早 N 条"，
    而指代消解要的是"最近的几轮"——取错的症状是"第二轮追问被改写成了第一轮的内容"。
    """
    cursor = (mongo.collection(MESSAGES).find({"session_id": session_id})
              .sort("ts", -1).limit(limit))
    rows = await cursor.to_list(length=limit)
    rows.reverse()
    return rows


async def get_message(message_id: str) -> dict[str, Any] | None:
    """按 `message_id` 取消息（反馈接口用；ObjectId 非法时返回 `None`）。"""
    from bson import ObjectId
    from bson.errors import InvalidId

    try:
        oid = ObjectId(message_id)
    except (InvalidId, TypeError):
        return None
    return await mongo.collection(MESSAGES).find_one({"_id": oid})


async def count_messages(session_id: str) -> int:
    """会话内的消息数（判 QA-3003 上限、更新 message_count）。"""
    return await mongo.collection(MESSAGES).count_documents(
        {"session_id": session_id})


# =========================================================================== E15 日志
async def insert_log(doc: Mapping[str, Any]) -> str:
    """落一条问答日志（**07/08/09 的数据源**，本模块独占写）。"""
    await mongo.collection(QA_LOGS).insert_one(dict(doc))
    return str(doc["_id"])


async def find_log_by_request(request_id: str) -> dict[str, Any] | None:
    """按幂等键查已有日志。"""
    if not request_id:
        return None
    return await mongo.collection(QA_LOGS).find_one({"request_id": request_id})


async def find_log_by_message(message_id: str) -> dict[str, Any] | None:
    """按 `message_id` 定位该轮问答的日志（**反馈接口的正确入口**）。

    为什么不用 `session_id` 找：一个会话有多轮，"按会话找最新一条"
    会把反馈写到**另一轮**的日志上——用户给第 3 轮的答案点了赞，
    数据却记在第 8 轮上，而这种错位在任何界面上都看不出来。
    `message_id` 是这一轮的精确标识（日志里冗余存了它，见 `qa_service._persist`）。
    """
    if not message_id:
        return None
    return await mongo.collection(QA_LOGS).find_one({"message_id": message_id})


async def find_log_by_session(session_id: str,
                              *, only_answered: bool = True) -> dict[str, Any] | None:
    """取某会话最新一条日志（反馈接口据此定位要写的日志）。"""
    query: dict[str, Any] = {"session_id": session_id}
    if only_answered:
        query["answer_source"] = {"$in": ["rag", "faq_cache"]}
    return await mongo.collection(QA_LOGS).find_one(query, sort=[("asked_at", -1)])


async def attach_feedback(log_id: str, feedback: Mapping[str, Any]) -> int:
    """把反馈写到对应日志上（PRD 预留功能）。"""
    result = await mongo.collection(QA_LOGS).update_one(
        {"_id": log_id}, {"$set": {"feedback": dict(feedback)}})
    return result.modified_count


async def list_logs(*, user_id: str | None = None, session_id: str | None = None,
                    faq_hit: bool | None = None, page: int = 1, page_size: int = 20
                    ) -> tuple[list[dict[str, Any]], int]:
    """读日志（**只读**：07 的 FAQ 挖掘、08 的缺口识别、09 的看板都走这个口径）。

    本模块提供它只是为了让下游不必再写一遍查询；写入权限仍只在本文件。
    """
    query: dict[str, Any] = {}
    if user_id:
        query["user_id"] = user_id
    if session_id:
        query["session_id"] = session_id
    if faq_hit is not None:
        query["faq_hit"] = bool(faq_hit)
    collection = mongo.collection(QA_LOGS)
    total = await collection.count_documents(query)
    cursor = (collection.find(query).sort("asked_at", -1)
              .skip((page - 1) * page_size).limit(page_size))
    return await cursor.to_list(length=page_size), total


async def count_logs() -> int:
    """日志总数（测试断言"幂等只落一条"时用）。"""
    return await mongo.collection(QA_LOGS).count_documents({})


async def drop_collections() -> None:
    """清空三张表（仅测试路径调用）。"""
    db = mongo.require_db()
    for name in (SESSIONS, MESSAGES, QA_LOGS):
        await db.drop_collection(name)


def _escape(keyword: str) -> str:
    """转义正则元字符（用户在搜索框里输入 `.*` 不该变成"匹配全部"）。"""
    import re

    return re.escape(keyword.strip()[:50])


def now_ms() -> int:
    """当前毫秒时间戳（本模块统一用它，避免秒/毫秒混用）。"""
    return int(time.time() * 1000)


def snake_ids(values: Iterable[Any]) -> list[str]:
    """把任意 ID 序列规范成去重的字符串列表（日志里存 ID 而非对象）。"""
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


__all__ = [
    "SESSIONS", "MESSAGES", "QA_LOGS", "SESSION_ID_PREFIX", "QA_LOG_ID_PREFIX",
    "ensure_indexes", "next_session_id", "next_log_id",
    "insert_session", "get_session", "list_sessions", "touch_session",
    "count_sessions",
    "insert_message", "list_messages", "recent_messages", "get_message",
    "count_messages",
    "insert_log", "find_log_by_request", "find_log_by_session", "find_log_by_message",
    "attach_feedback",
    "list_logs", "count_logs", "drop_collections", "now_ms", "snake_ids",
]
