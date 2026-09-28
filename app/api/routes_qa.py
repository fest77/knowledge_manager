# -*- coding: utf-8 -*-
"""模块 06 的接口层（`/api/v1/qa/*`）。

## 路由顺序

| 必须在前 | 必须在后 | 写反的后果 |
|---|---|---|
| `/qa/sessions`（列表） | `/qa/sessions/{session_id}/messages` | 深度不同，其实不冲突 |
| `/qa/ask`（POST） | `/qa/stream/{task_id}`（GET） | 路径前缀不同，各自独立 |
| `/qa/messages/{id}/feedback` | — | 与 sessions 不冲突 |

本模块真正要注意的是 **`/qa/stream/{task_id}` 必须声明在 `/qa/sessions` 之前**
没有硬性要求（路径前缀不同），所以这里按"功能分组"排：ask → stream → sessions → messages。

## SSE 响应的四个必带头

| 头 | 为什么 |
|---|---|
| `Content-Type: text/event-stream` | 协议要求；前端据此识别 |
| `Cache-Control: no-cache` | 中间层缓存了流 = 用户看到上一次的答案 |
| `X-Accel-Buffering: no` | 反代（nginx）默认**缓冲**响应，会让流式变成"一次性吐出"，等于没有流式 |
| `Connection: keep-alive` | 长连接语义（HTTP/1.1 默认，但显式声明避免被误解） |

## 权限

| 接口 | 功能权限 | 归属校验 |
|---|---|---|
| `POST /qa/ask` | `qa:use` | — |
| `GET /qa/stream/{task_id}` | `qa:use` | ✅ **必须**（`QA-2003` / ER-14） |
| `GET /qa/sessions` | `qa:history` | 仅本人（G-12） |
| `GET /qa/sessions/{id}/messages` | `qa:history` | ✅ 仅本人 |
| `POST /qa/messages/{id}/feedback` | `qa:feedback` | ✅ 仅本人 |

`QA-2002`（用户上下文缺失）在本模块只作为**兜底**：正常路径由 JWT 中间件
产出 `AUTH-2003`。它的存在是为了"中间件被绕过"这种情形（比如后台任务直调服务层）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.api.deps import current_user, ensure_permission
from app.api.schemas_qa import (AskRequest, AskResponse, FeedbackRequest,
                                FeedbackResponse, MessageListResponse,
                                SessionListResponse)
from app.core.errors import Err
from app.core.permissions import require_perm
from app.core.response import ok
from app.services.auth_service import UserContext
from app.services.qa_service import qa_service

router = APIRouter(prefix="/api/v1/qa", tags=["06 AI 鉴权问答"])

# SSE 响应头（见模块头部说明）
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.post("/ask", summary="提问（同步返回 task_id，答案走 SSE）",
             response_model=None)
@require_perm("qa:use")
async def ask(body: AskRequest, user: UserContext = Depends(current_user)):
    """**200ms 内返回 `task_id`**（AC-06-12）：真正的问答在后台任务里跑，
    前端立刻用 `stream_url` 打开 SSE。

    未登录在这里会被 JWT 中间件拦掉（`AUTH-2003`）；
    `QA-2002` 是"中间件装上了但上下文为空"的兜底。
    """
    ensure_permission(user, "qa:use", Err.QA_CONTEXT_MISSING)
    result = await qa_service.ask(user=user, question=body.question,
                                  session_id=body.session_id,
                                  request_id=body.request_id)
    return ok(AskResponse(task_id=result.task_id, session_id=result.session_id,
                          message_id=result.message_id,
                          stream_url=result.stream_url).model_dump())


@router.get("/stream/{task_id}", summary="SSE 流式答案（校验订阅归属）",
            response_model=None)
@require_perm("qa:use")
async def stream(task_id: str, user: UserContext = Depends(current_user)):
    """建立 SSE 流**之前**先校验归属（ER-14 / 标注 7 / AC-06-07）。

    越权订阅返回 403 `QA-2003` 且**不建立流** —— 这是与"建流后再报错"的关键区别：
    后者已经把 `Content-Type: text/event-stream` 发出去了，前端会先看到"连接成功"
    再看到错误，而中间那段时间里它已经在读别人的答案了（本实现根本不给这个窗口）。
    """
    ensure_permission(user, "qa:use", Err.QA_CONTEXT_MISSING)
    frames = await qa_service.open_stream(task_id=task_id, user=user)
    return StreamingResponse(frames, media_type="text/event-stream",
                             headers=SSE_HEADERS)


@router.get("/sessions", summary="历史会话侧边栏（仅本人）", response_model=None)
@require_perm("qa:history")
async def list_sessions(
    user: UserContext = Depends(current_user),
    keyword: str = Query("", max_length=50, description="按会话标题搜索"),
    page: int = Query(1, description="页码；≤0 报 QA-1003 之外的参数错"),
    page_size: int = Query(20, le=1000, description="上限 200，超出报 QA-1003"),
):
    """**仅本人**（G-12）：历史问答不跨用户可见。

    `page` / `page_size` 的范围约束在服务层判（与 04/05 同一做法）：
    写在路由的 `ge/le` 上会让越界变成 `SYS-1001`，与 Spec 的码不符。
    """
    ensure_permission(user, "qa:history", Err.QA_SESSION_FORBIDDEN)
    data = await qa_service.list_sessions(user=user, keyword=keyword, page=page,
                                          page_size=page_size)
    return ok(SessionListResponse(**data).model_dump())


@router.get("/sessions/{session_id}/messages", summary="会话消息与引用溯源（仅本人）",
            response_model=None)
@require_perm("qa:history")
async def session_messages(
    session_id: str,
    user: UserContext = Depends(current_user),
    page: int = Query(1),
    page_size: int = Query(100, le=1000),
):
    """回放一轮轮对话；助手消息里带 `chunk_refs`（引用卡片）与 `denied_count`。"""
    ensure_permission(user, "qa:history", Err.QA_SESSION_FORBIDDEN)
    data = await qa_service.session_messages(user=user, session_id=session_id,
                                             page=page, page_size=page_size)
    return ok(MessageListResponse(**data).model_dump())


@router.post("/messages/{message_id}/feedback", summary="答案反馈（预留）",
             response_model=None)
@require_perm("qa:feedback")
async def feedback(message_id: str, body: FeedbackRequest,
                   user: UserContext = Depends(current_user)):
    """PRD 未要求，**预留**：写 `qa_logs.feedback`（评分 + 备注）。"""
    ensure_permission(user, "qa:feedback", Err.QA_SESSION_FORBIDDEN)
    data = await qa_service.feedback(user=user, message_id=message_id,
                                     rating=body.rating, comment=body.comment)
    return ok(FeedbackResponse(**data).model_dump())


__all__ = ["router", "SSE_HEADERS"]
