# -*- coding: utf-8 -*-
"""模块 06 的接口层测试（含真实 SSE 流的端到端读取）。

与 `test_qa_service.py` 的分工：那个测编排逻辑，这个测**HTTP 契约**——
状态码、权限码、响应头、SSE 帧格式。三者都容易被"服务层对了"掩盖：
服务层全绿但 `Content-Type` 写成 `application/json`，前端照样收不到流。

**大模型/向量化/召回都打桩**（理由见 `test_qa_service.py` 模块头）。
"""
from __future__ import annotations

import hashlib
import json

import pytest

from app.core.config import settings
from app.infra.llm import ChatChunk, ChatResult, TokenUsage, llm_client
from app.infra.milvus import milvus
from app.infra.sse import sse_hub
from app.repositories import perm_repo
from app.services.doc_service import doc_service
from app.services.embedding_service import embedding_service
from app.services.faq_cache import faq_cache
from app.services.rerank_service import rerank_service
from tests.conftest import token_of

pytestmark = pytest.mark.anyio

ASKER = "wangqiang"
KB_ADMIN = "zhangwei"
ACTOR = "U000001"
ANSWER = "根据[1]的规定，住宿费上限为 500 元。"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _clean_caches():
    sse_hub.reset()
    faq_cache.reset()
    yield
    sse_hub.reset()
    faq_cache.reset()


@pytest.fixture
def stub_qa(monkeypatch):
    """把问答链路的外部依赖全部打桩，返回一个"设置召回结果"的函数。"""
    async def _stream(messages, *, temperature=None, max_tokens=None):
        for piece in ("根据", "[1]", "的规定，", "住宿费上限为 500 元。"):
            yield ChatChunk(text=piece)
        yield ChatChunk(usage=TokenUsage(prompt=10, completion=20, total=30))

    async def _chat(messages, *, temperature=None, max_tokens=None):
        return ChatResult(text="改写后的问题")

    def _embed_one(_text: str) -> list[float]:
        vector = [0.0] * settings.embedding_dim
        vector[0] = 1.0
        return vector

    def _identity(_query, documents, *, top_k=None):
        ranked = [(index, 1.0 - index * 0.01) for index in range(len(documents))]
        return ranked[:top_k] if top_k is not None else ranked

    state: dict[str, list[dict]] = {"hits": []}

    async def _search(**_kwargs):
        return list(state["hits"])

    monkeypatch.setattr(llm_client, "stream_chat", _stream)
    monkeypatch.setattr(llm_client, "chat", _chat)
    monkeypatch.setattr(embedding_service, "embed_one", _embed_one)
    monkeypatch.setattr(rerank_service, "rerank", _identity)
    monkeypatch.setattr(milvus, "search", _search)

    def _set(hits: list[dict]) -> None:
        state["hits"] = list(hits)

    return _set


def hit(doc_id: str, index: int, content: str, score: float) -> dict:
    return {"chunk_id": index, "doc_id": doc_id, "content": content,
            "title": f"第{index}章", "parent_title": "", "part": 0,
            "enabled": True, "score": score}


async def _new_doc(name: str) -> str:
    return await doc_service.create(
        file_name=name, file_ext="pdf", file_size=100,
        file_hash=hashlib.sha256(name.encode()).hexdigest(), storage={},
        created_by=ACTOR)


async def _grant(doc_id: str) -> None:
    await perm_repo.upsert(doc_id, is_global=True, departments=[], roles=[], users=[],
                           reason="测试授权（全局公开）", updated_by=ACTOR, ts_ms=1)


async def _read_sse(client, url: str, headers: dict) -> tuple[dict, list[tuple[str, dict]]]:
    """读完整条 SSE 流，返回 `(响应头, [(事件名, 数据)])`。

    用 `client.stream` + `aiter_lines`：这样断言的正是**客户端真正看到的帧**，
    而不是服务层内部 publish 了什么。
    """
    events: list[tuple[str, dict]] = []
    async with client.stream("GET", url, headers=headers) as resp:
        assert resp.status_code == 200, await resp.aread()
        assert resp.headers["content-type"].startswith("text/event-stream")
        # 反代缓冲必须关掉，否则"流式"会变成"一次性吐出"（等于没有流式）
        assert resp.headers.get("x-accel-buffering") == "no"
        assert resp.headers.get("cache-control") == "no-cache"
        name = ""
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                events.append((name, json.loads(line[5:].strip())))
            if name in ("done", "error") and events and events[-1][0] == name:
                break
    return dict(resp.headers), events


def names_of(events: list[tuple[str, dict]]) -> list[str]:
    return [name for name, _ in events]


def data_of(events: list[tuple[str, dict]], event: str) -> list[dict]:
    return [data for name, data in events if name == event]


# ================================================================ 权限与鉴权
async def test_ask_requires_login_and_qa_use(client):
    """AC-06-01 / 标注 1：**不允许未登录问答**（401 由中间件产出）。"""
    resp = await client.post("/api/v1/qa/ask", json={"question": "谁能看？"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH-2003"

    # 三个内置角色都有 qa:use（ASKER 是它们的公共部分）
    for username in ("wangqiang", "zhangwei", "lina"):
        token = await token_of(client, username)
        ok = await client.post("/api/v1/qa/ask", headers=auth(token),
                               json={"question": ""})
        # 空问题 → QA-1001（说明权限已过，走到了业务校验）
        assert ok.status_code == 400
        assert ok.json()["code"] == "QA-1001", username


async def test_ask_returns_task_id_fast(client, stub_qa):
    """AC-06-12 / 分流式输出：`/qa/ask` **不等 LLM**，立即返回 `task_id`。"""
    import time

    stub_qa([])
    token = await token_of(client, ASKER)
    started = time.monotonic()
    resp = await client.post("/api/v1/qa/ask", headers=auth(token),
                             json={"question": "住宿标准是多少？"})
    elapsed_ms = (time.monotonic() - started) * 1000
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["task_id"] and data["session_id"] and data["message_id"]
    assert data["stream_url"] == f"/api/v1/qa/stream/{data['task_id']}"
    assert elapsed_ms < 2000, f"受理耗时 {elapsed_ms:.0f}ms 过长"

    # 收尾：把流读完，避免后台任务在下一个用例里继续跑
    await _read_sse(client, data["stream_url"], auth(token))


# ================================================================ SSE 全流程
async def test_sse_stream_delivers_full_event_sequence(client, stub_qa):
    """主干：`meta → citation → delta* → notice → done`，且文本拼起来就是答案。"""
    doc = await _new_doc("可读制度.pdf")
    secret = await _new_doc("机密制度.pdf")
    await _grant(doc)
    stub_qa([hit(doc, 11, "住宿费上限 500 元。", 0.91),
             hit(secret, 22, "绝密：高管薪酬 999 万。", 0.88)])

    token = await token_of(client, ASKER)
    ask = await client.post("/api/v1/qa/ask", headers=auth(token),
                            json={"question": "住宿标准是多少？"})
    stream_url = ask.json()["data"]["stream_url"]
    _, events = await _read_sse(client, stream_url, auth(token))

    order = names_of(events)
    assert order[0] == "meta" and order[-1] == "done"
    assert order.count("citation") == 1
    assert order.count("notice") == 1
    assert order.count("delta") >= 3

    meta = data_of(events, "meta")[0]
    assert meta["recalled"] == 2 and meta["allowed"] == 1 and meta["denied"] == 1
    notice = data_of(events, "notice")[0]
    assert notice["denied_count"] == 1
    assert "部分参考资料因权限受限无法展示" in notice["notice"]
    text = "".join(d["text"] for d in data_of(events, "delta"))
    assert text == ANSWER
    done = data_of(events, "done")[0]
    assert done["token_usage"]["total"] == 30
    assert done["answer_source"] == "rag"


async def test_sse_rejects_other_users_task(client, stub_qa):
    """AC-06-07：用别人的 `task_id` 订阅 → **403 QA-2003，且不建立流**。"""
    stub_qa([])
    owner = await token_of(client, ASKER)
    other = await token_of(client, KB_ADMIN)
    ask = await client.post("/api/v1/qa/ask", headers=auth(owner),
                            json={"question": "我的私有问题"})
    task_id = ask.json()["data"]["task_id"]

    resp = await client.get(f"/api/v1/qa/stream/{task_id}", headers=auth(other))
    assert resp.status_code == 403
    assert resp.json()["code"] == "QA-2003"
    assert "text/event-stream" not in resp.headers.get("content-type", "")

    # 不存在的 task_id 也不给流
    ghost = await client.get("/api/v1/qa/stream/deadbeef", headers=auth(owner))
    assert ghost.status_code == 403
    assert ghost.json()["code"] == "QA-2003"

    # 本人可以正常读完
    await _read_sse(client, f"/api/v1/qa/stream/{task_id}", auth(owner))


async def test_sse_error_event_on_llm_failure(client, stub_qa, monkeypatch):
    """`QA-4002` 走 `error` 事件（HTTP 仍是 200：流已经建立）。"""
    from app.infra.llm import LLMUnavailable

    doc = await _new_doc("可读2.pdf")
    await _grant(doc)

    async def _boom(*_args, **_kwargs):
        raise LLMUnavailable("测试：大模型不可用")
        yield                                              # pragma: no cover

    monkeypatch.setattr(llm_client, "stream_chat", _boom)
    def _search_hits(**_kwargs):
        return [hit(doc, 1, "内容", 0.9)]
    # 召回桩返回固定一篇
    async def _search(**_kwargs):
        return [hit(doc, 1, "内容", 0.9)]
    monkeypatch.setattr(milvus, "search", _search)

    token = await token_of(client, ASKER)
    ask = await client.post("/api/v1/qa/ask", headers=auth(token),
                            json={"question": "问题"})
    _, events = await _read_sse(client, ask.json()["data"]["stream_url"],
                                auth(token))
    errors = data_of(events, "error")
    assert errors and errors[0]["code"] == "QA-4002"


# ================================================================ 会话历史
async def test_sessions_and_messages_are_scoped_to_the_caller(client, stub_qa):
    """AC-06-10 / G-12：A 读不到 B 的会话；会话消息带引用卡片与受限计数。"""
    doc = await _new_doc("可读3.pdf")
    await _grant(doc)
    stub_qa([hit(doc, 7, "放行内容", 0.9)])

    a_token = await token_of(client, ASKER)
    b_token = await token_of(client, KB_ADMIN)
    ask_a = await client.post("/api/v1/qa/ask", headers=auth(a_token),
                              json={"question": "A 的问题"})
    session_a = ask_a.json()["data"]["session_id"]
    await _read_sse(client, ask_a.json()["data"]["stream_url"], auth(a_token))

    ask_b = await client.post("/api/v1/qa/ask", headers=auth(b_token),
                              json={"question": "B 的问题"})
    await _read_sse(client, ask_b.json()["data"]["stream_url"], auth(b_token))

    # 侧边栏：只有自己的
    mine = (await client.get("/api/v1/qa/sessions", headers=auth(a_token))).json()["data"]
    assert mine["total"] == 1
    assert mine["items"][0]["session_id"] == session_a
    assert mine["items"][0]["title"] == "A 的问题"

    # 消息回放：助手消息带引用卡片
    detail = (await client.get(f"/api/v1/qa/sessions/{session_a}/messages",
                               headers=auth(a_token))).json()["data"]
    roles = [m["role"] for m in detail["messages"]]
    assert roles == ["user", "assistant"]
    assistant = detail["messages"][-1]
    assert assistant["text"] == ANSWER
    assert len(assistant["chunk_refs"]) == 1
    assert assistant["chunk_refs"][0]["doc_id"] == doc
    assert assistant["token_usage"]["total"] == 30

    # 越权读别人的会话 → QA-2001
    cross = await client.get(f"/api/v1/qa/sessions/{session_a}/messages",
                             headers=auth(b_token))
    assert cross.status_code == 403
    assert cross.json()["code"] == "QA-2001"

    missing = await client.get("/api/v1/qa/sessions/SESS20260101999999/messages",
                              headers=auth(a_token))
    assert missing.status_code == 404
    assert missing.json()["code"] == "QA-3001"


async def test_session_param_validation(client, stub_qa):
    """`QA-1003`（会话编号格式）与 `QA-3003`（会话消息上限）。"""
    stub_qa([])
    token = await token_of(client, ASKER)
    bad = await client.post("/api/v1/qa/ask", headers=auth(token),
                            json={"question": "问题", "session_id": "WRONG"})
    assert bad.status_code == 400
    assert bad.json()["code"] == "QA-1003"

    from app.core.config import settings as cfg
    from app.repositories import qa_repo

    session_id = await qa_repo.next_session_id(qa_repo.now_ms())
    await qa_repo.insert_session({
        "_id": session_id, "user_id": "U000003", "title": "满会话",
        "message_count": cfg.qa_session_msg_limit, "last_active_at": 1,
        "created_at": 1})
    for i in range(cfg.qa_session_msg_limit):
        await qa_repo.insert_message({"session_id": session_id, "user_id": "U000003",
                                      "role": "user", "text": f"m{i}", "ts": i})
    full = await client.post("/api/v1/qa/ask", headers=auth(token),
                             json={"question": "还能问吗", "session_id": session_id})
    assert full.status_code == 409
    assert full.json()["code"] == "QA-3003"


async def test_feedback_endpoint(client, stub_qa):
    """反馈接口：`rating` 校验（`QA-1002`）与成功写入。"""
    stub_qa([])
    token = await token_of(client, ASKER)
    ask = await client.post("/api/v1/qa/ask", headers=auth(token),
                            json={"question": "反馈接口测试"})
    await _read_sse(client, ask.json()["data"]["stream_url"], auth(token))

    bad = await client.post(f"/api/v1/qa/messages/{ask.json()['data']['message_id']}"
                            "/feedback", headers=auth(token),
                            json={"rating": "sideways"})
    assert bad.status_code == 400
    assert bad.json()["code"] == "QA-1002"

    ghost = await client.post("/api/v1/qa/messages/000000000000000000000000/feedback",
                              headers=auth(token), json={"rating": "up"})
    assert ghost.status_code == 404
    assert ghost.json()["code"] == "QA-3002"


async def test_history_permission_is_enforced(client, stub_qa):
    """`qa:history` 的门槛由 01 的全局依赖产出（`AUTH-2004`）。"""
    stub_qa([])
    # 造一个只有 qa:use 没有 qa:history 的角色绑定，验证 403
    from app.infra.mongo import mongo

    await mongo.collection("sys_roles").insert_one(
        {"_id": "ROLE9001", "code": "qa_only", "name": "仅问答", "is_system": False,
         "created_at": 1, "updated_at": 1})
    await mongo.collection("sys_role_permissions").insert_one(
        {"_id": "ROLE9001:qa:use", "role_id": "ROLE9001", "permission_id": "qa:use",
         "granted_by": "test", "granted_at": 1})
    await mongo.collection("sys_user_roles").delete_many({"user_id": "U000003"})
    await mongo.collection("sys_user_roles").insert_one(
        {"_id": "U000003:ROLE9001", "user_id": "U000003", "role_id": "ROLE9001",
         "granted_by": "test", "granted_at": 1})
    from app.services.permission_cache import permission_cache

    permission_cache.invalidate_all()

    token = await token_of(client, ASKER)
    denied = await client.get("/api/v1/qa/sessions", headers=auth(token))
    assert denied.status_code == 403
    assert denied.json()["code"] == "AUTH-2004"
