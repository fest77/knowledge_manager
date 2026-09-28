# -*- coding: utf-8 -*-
"""模块 06 的请求/响应模型。

`POST /qa/ask` 的出参刻意很薄：`task_id` / `session_id` / `message_id` / `stream_url`。
**正文不在这个响应里**——它走 SSE（`/qa/stream/{task_id}`）。
这样做的理由见 Spec §3.1：大模型首 token 要几百毫秒到数秒，
同步等答案会让前端像卡死；先给 `task_id` 让前端立刻开流，
首字节到达即可展示"正在思考"（AC-06-12）。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    """提问入参。长度上限由服务层按 `QA_MAX_QUESTION_LEN` 报 `QA-1001`，
    **不在模型上写 `max_length`**（否则越界会变成 `SYS-1001`，与 Spec 的码不符）。"""

    question: str
    session_id: str | None = None
    request_id: str | None = None


class AskResponse(BaseModel):
    """受理出参：任务号 + 会话号 + 流地址。"""

    task_id: str
    session_id: str
    message_id: str
    stream_url: str


class SessionItem(BaseModel):
    """侧边栏一行（原型 `s1T`/`s1S`：标题 + 「今天 10:24 · 6 轮」）。"""

    session_id: str
    title: str = ""
    message_count: int = 0
    last_active_at: int | None = None


class SessionListResponse(BaseModel):
    """历史会话侧边栏出参（**仅本人**，G-12）。"""

    items: list[SessionItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20


class MessageItem(BaseModel):
    """一条消息（助手消息带引用卡片与受限计数）。"""

    message_id: str
    role: str
    text: str = ""
    chunk_refs: list[dict[str, Any]] = Field(default_factory=list)
    denied_count: int = 0
    token_usage: dict[str, int] | None = None
    elapsed_ms: int | None = None
    rewritten_query: str | None = None
    ts: int | None = None


class MessageListResponse(BaseModel):
    """会话消息回放出参（含引用溯源卡片与受限计数）。"""

    session_id: str
    title: str = ""
    messages: list[MessageItem] = Field(default_factory=list)


class FeedbackRequest(BaseModel):
    """答案反馈入参（PRD 预留功能；`rating` 的合法性由服务层报 `QA-1002`）。"""

    rating: str
    comment: str = ""


class FeedbackResponse(BaseModel):
    """反馈出参：只回 `message_id` 与最终 `rating`。"""

    message_id: str
    rating: str


# SSE 事件的取值域（前端按它分支；也是文档）
SSE_EVENTS: tuple[str, ...] = ("meta", "delta", "citation", "notice", "done", "error")
# `answer_source` 的仨取值（Step 1 冻结，**不新增** —— "找到了但全被拦"
# 靠 `denied_count > 0` 区分，见模块 06 Spec §2.4）
ANSWER_SOURCES: tuple[str, ...] = ("faq_cache", "rag", "no_knowledge")

__all__ = [
    "AskRequest", "AskResponse", "SessionItem", "SessionListResponse",
    "MessageItem", "MessageListResponse", "FeedbackRequest", "FeedbackResponse",
    "SSE_EVENTS", "ANSWER_SOURCES",
]
