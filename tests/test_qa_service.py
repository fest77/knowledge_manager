# -*- coding: utf-8 -*-
"""模块 06 的问答主链路测试（**不调用真实大模型、不加载真模型、不连 Milvus**）。

## 测试策略：把"外部依赖"全部替换掉，只留被测的编排逻辑

| 替身 | 为什么 |
|---|---|
| `llm_client.stream_chat` / `chat` | DashScope 是**付费外部接口**：单测里绝不能打；而且它不可控（限流/延迟） |
| `embedding_service.embed_one` | 真 BGE-M3 要 2.2GB 显存、十几秒加载；这里只验证"向量正确流经链路" |
| `milvus.search` | 直接给"召回结果"，把注意力放在**鉴权过滤与 Prompt 拼装**上 |
| `rerank_service.rerank` | 同上；另有一个用例单独验证"重排不可用时跳过" |

**保留真实的**：Mongo（含 E14/E02/E15 三张表的索引与写入）、`permission_service`
（鉴权必须走真实现——它是本模块最关键的协作者，不能打桩）、`sse_hub`（事件顺序是被测对象）。

## 两条踩过的坑（写在这里省后来人一次）

1. **替身重排必须是 `def` 而不是 `async def`**：服务层用
   `asyncio.to_thread(rerank_service.rerank, ...)` 调用它，异步替身会返回一个
   未被 await 的协程，表现为 `TypeError: cannot unpack non-sequence coroutine`。
2. **替身一律经 `monkeypatch`**：直接赋值 `milvus.search = ...` 会**跨用例泄漏**，
   症状是"单独跑通过、一起跑失败"。

## 三条主线

1. **未授权内容绝不进 Prompt**（AC-06-03）：抓取实际传给 LLM 的 messages 逐字检查
2. **全部受限时不用大模型凭空作答**（AC-06-11）
3. **SSE 归属校验**（AC-06-07）：越权订阅必须在建流之前被拦
"""
from __future__ import annotations

import asyncio
import hashlib

import pytest

from app.core.config import settings
from app.core.errors import BizError
from app.infra.llm import ChatChunk, ChatResult, TokenUsage, llm_client
from app.infra.milvus import milvus
from app.infra.mongo import mongo
from app.infra.sse import sse_hub
from app.repositories import perm_repo, qa_repo
from app.services import permission_service as ps
from app.services.doc_service import doc_service
from app.services.embedding_service import embedding_service
from app.services.faq_cache import FaqEntry, faq_cache
from app.services.permission_service import Subject
from app.services.qa_service import (DENIED_ALL_ANSWER, NO_KNOWLEDGE_ANSWER,
                                     build_prompt, qa_service)
from app.services.rerank_service import RerankUnavailable, rerank_service

pytestmark = pytest.mark.anyio

ASKER_ID = "U000003"          # wangqiang（技术部）
OTHER_ID = "U000002"          # zhangwei（人力资源部）
ACTOR = "U000001"

ANSWER_PIECES = ("根据", "[1]", "的规定，", "住宿费上限为 500 元。")


def user(user_id: str = ASKER_ID, dept_id: str = "DEPT0004") -> Subject:
    """构造判定/提问用的用户快照。"""
    return Subject(user_id=user_id, dept_id=dept_id,
                   role_ids=frozenset({"ROLE0001"}))


def hit(doc_id: str, index: int, content: str, title: str, score: float) -> dict:
    """构造一条召回结果（字段与 `MilvusStore.search` 的返回一致）。"""
    return {"chunk_id": index, "doc_id": doc_id, "content": content, "title": title,
            "parent_title": "", "part": 0, "enabled": True, "score": score}


# ================================================================ 夹具
@pytest.fixture(autouse=True)
def _clean_caches():
    sse_hub.reset()
    faq_cache.reset()
    yield
    sse_hub.reset()
    faq_cache.reset()


@pytest.fixture
def fake_llm(monkeypatch):
    """替身大模型：返回固定流式文本，并**记录每次收到的 messages**（供 AC-06-03 检查）。"""
    calls: list[list[dict[str, str]]] = []

    async def _stream(messages, *, temperature=None, max_tokens=None):
        calls.append(list(messages))
        for piece in ANSWER_PIECES:
            yield ChatChunk(text=piece)
        yield ChatChunk(usage=TokenUsage(prompt=120, completion=30, total=150))

    async def _chat(messages, *, temperature=None, max_tokens=None):
        calls.append(list(messages))
        return ChatResult(text="改写后的独立问题", usage=TokenUsage(5, 5, 10))

    monkeypatch.setattr(llm_client, "stream_chat", _stream)
    monkeypatch.setattr(llm_client, "chat", _chat)
    return calls


@pytest.fixture
def fake_models(monkeypatch):
    """替身向量化：返回固定 1024 维单位向量（避免加载 2.2GB 模型）。"""
    def _embed_one(_text: str) -> list[float]:
        vector = [0.0] * settings.embedding_dim
        vector[0] = 1.0
        return vector

    monkeypatch.setattr(embedding_service, "embed_one", _embed_one)
    return _embed_one


@pytest.fixture
def fake_recall(monkeypatch):
    """替身召回：返回"由用例设置的固定结果"（见模块头坑 2）。"""
    state: dict[str, list[dict]] = {"hits": []}

    async def _search(**_kwargs):
        return list(state["hits"])

    monkeypatch.setattr(milvus, "search", _search)

    def _set(hits: list[dict]) -> None:
        state["hits"] = list(hits)

    return _set


@pytest.fixture
def fake_rerank(monkeypatch):
    """替身重排：**同步函数**（见模块头坑 1），按原顺序返回。"""
    def _identity(_query, documents, *, top_k=None):
        ranked = [(index, 1.0 - index * 0.01) for index in range(len(documents))]
        return ranked[:top_k] if top_k is not None else ranked

    monkeypatch.setattr(rerank_service, "rerank", _identity)
    return _identity


# ================================================================ 工具
async def _new_doc(name: str) -> str:
    return await doc_service.create(
        file_name=name, file_ext="pdf", file_size=100,
        file_hash=hashlib.sha256(name.encode()).hexdigest(), storage={},
        created_by=ACTOR)


async def _grant(doc_id: str, *, is_global: bool = True) -> None:
    await perm_repo.upsert(doc_id, is_global=is_global, departments=[], roles=[],
                           users=[], reason="测试授权（全局公开）",
                           updated_by=ACTOR, ts_ms=1)


async def _collect_frames(task_id: str) -> list[dict]:
    """订阅 SSE 并收集全部事件（直到 done/error）。

    用**真实订阅路径**等待完成：这样"事件顺序"与"是否发过 notice"都是被测对象，
    而不是绕过流去读内部状态。
    """
    frames: list[dict] = []
    async for item in sse_hub.subscribe(task_id):
        frames.append(item)
        if item["event"] in ("done", "error"):
            break
    return frames


def events_of(frames: list[dict], event: str) -> list[dict]:
    return [f["data"] for f in frames if f["event"] == event]


async def _ask_and_collect(subject: Subject, question: str, **kwargs
                           ) -> tuple[list[dict], str, str]:
    """提问 → 收集事件 → 返回 `(frames, session_id, task_id)`。"""
    result = await qa_service.ask(user=subject, question=question, **kwargs)
    frames = await asyncio.wait_for(_collect_frames(result.task_id), timeout=20)
    return frames, result.session_id, result.task_id


async def _wait_log(task_id: str, timeout: float = 3.0) -> dict:
    """等 `qa_logs` 落库（异步写；AC-06-15 要求 1 秒内可查）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        row = await mongo.collection(qa_repo.QA_LOGS).find_one({"task_id": task_id})
        if row:
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"qa_logs 在 {timeout}s 内没有落库：task={task_id}")


# ================================================================ 单元素材
def test_cosine_and_faq_cache_match():
    """FAQ 命中判定：阈值以下不命中（G-03：宁可不命中也不给错答案）。"""
    faq_cache.upsert(FaqEntry(faq_id="FAQ000001", question="差旅标准",
                              answer="标准答案", vector=[1.0, 0.0]))
    assert faq_cache.size == 1
    matched = faq_cache.match([1.0, 0.0])
    assert matched is not None and matched.entry.faq_id == "FAQ000001"
    # 正交向量 → 余弦 0 → 远低于 0.92 阈值
    assert faq_cache.match([0.0, 1.0]) is None
    # 零向量不抛异常也不返回 NaN
    assert faq_cache.match([0.0, 0.0]) is None


def test_build_prompt_numbers_only_given_chunks():
    """编号从 1 开始且与 citation 顺序一致（否则答案里的 [2] 会指向别的卡片）。"""
    from app.services.qa_service import RecalledChunk

    chunks = [RecalledChunk(chunk_id=1, doc_id="DOC1", content="甲内容",
                            title="第一章", doc_title="制度A"),
              RecalledChunk(chunk_id=2, doc_id="DOC2", content="乙内容",
                            title="第二章", doc_title="制度B")]
    prompt = build_prompt("问题", chunks)
    assert "[1] 文档：制度A｜章节：第一章" in prompt
    assert "[2] 文档：制度B｜章节：第二章" in prompt
    assert "甲内容" in prompt and "乙内容" in prompt
    assert "【问题】\n问题" in prompt


# ================================================================ 受理校验
async def test_ask_validates_question_and_session(client, fake_llm, fake_models,
                                                  fake_recall):
    """QA-1001（长度）/ QA-1003（编号格式）/ QA-3001（不存在）/ QA-2001（非本人）。"""
    fake_recall([])
    subject = user()
    with pytest.raises(BizError) as exc:
        await qa_service.ask(user=subject, question="   ")
    assert exc.value.spec.code == "QA-1001"

    with pytest.raises(BizError) as exc:
        await qa_service.ask(user=subject, question="x" * 501)
    assert exc.value.spec.code == "QA-1001"

    with pytest.raises(BizError) as exc:
        await qa_service.ask(user=subject, question="问题", session_id="ABC123")
    assert exc.value.spec.code == "QA-1003"

    with pytest.raises(BizError) as exc:
        await qa_service.ask(user=subject, question="问题",
                             session_id="SESS20260101999999")
    assert exc.value.spec.code == "QA-3001"

    # 别人的会话 → QA-2001（G-12：历史问答不跨用户可见）
    other_user = user(OTHER_ID, "DEPT0002")
    other = await qa_service.ask(user=other_user, question="别人的问题")
    await asyncio.wait_for(_collect_frames(other.task_id), timeout=20)
    with pytest.raises(BizError) as exc:
        await qa_service.ask(user=subject, question="偷看",
                             session_id=other.session_id)
    assert exc.value.spec.code == "QA-2001"


async def test_ask_creates_session_with_first_twenty_chars(client, fake_llm,
                                                           fake_models,
                                                           fake_recall):
    """标题取首轮提问前 20 字（Spec §2.1），并把用户消息落库。"""
    fake_recall([])
    long_question = "差旅费报销的住宿标准到底是多少钱一个晚上呢"
    frames, session_id, _ = await _ask_and_collect(user(), long_question)
    assert events_of(frames, "done")

    session = await qa_repo.get_session(session_id)
    assert session["title"] == long_question[:20]
    assert session["user_id"] == ASKER_ID
    messages, _ = await qa_repo.list_messages(session_id)
    assert messages[0]["role"] == "user" and messages[0]["text"] == long_question
    assert messages[0]["user_id"] == ASKER_ID, "C-02：消息必须带 user_id"


async def test_request_id_is_idempotent(client, fake_llm, fake_models, fake_recall):
    """AC-06-14：同一 `request_id` 只执行一次，`qa_logs` 只落一条。"""
    fake_recall([])
    subject = user()
    first = await qa_service.ask(user=subject, question="幂等测试", request_id="REQ-1")
    await asyncio.wait_for(_collect_frames(first.task_id), timeout=20)
    second = await qa_service.ask(user=subject, question="幂等测试",
                                 request_id="REQ-1")
    assert second.task_id == first.task_id
    assert second.session_id == first.session_id
    await _wait_log(first.task_id)
    assert await mongo.collection(qa_repo.QA_LOGS).count_documents(
        {"request_id": "REQ-1"}) == 1


# ================================================================ 主链路
async def test_normal_answer_streams_and_logs_three_lists(client, fake_llm,
                                                          fake_models, fake_recall,
                                                          fake_rerank):
    """主干：meta → citation → delta → done；`qa_logs` 三列表齐全（AC-06-06）。"""
    doc_a = await _new_doc("可读制度.pdf")
    doc_b = await _new_doc("机密制度.pdf")
    await _grant(doc_a)
    # doc_b 不配权限 → 默认拒绝（本轮应当被拦）
    fake_recall([hit(doc_a, 11, "住宿费上限 500 元。", "第三章 住宿", 0.91),
                 hit(doc_b, 22, "绝密内容：高管薪酬为 X。", "机密章", 0.88)])

    frames, session_id, task_id = await _ask_and_collect(user(), "住宿标准是多少？")

    metas = events_of(frames, "meta")
    assert metas and metas[0]["recalled"] == 2
    assert metas[0]["allowed"] == 1 and metas[0]["denied"] == 1
    assert metas[0]["answer_source"] == "rag" and metas[0]["faq_hit"] is False

    citations = events_of(frames, "citation")
    assert citations and len(citations[0]["citations"]) == 1
    assert citations[0]["citations"][0]["doc_id"] == doc_a
    assert citations[0]["citations"][0]["doc_title"] == "可读制度", "标题取自文件名（去扩展名）"

    deltas = events_of(frames, "delta")
    assert "".join(d["text"] for d in deltas) == "".join(ANSWER_PIECES)

    notices = events_of(frames, "notice")
    assert notices and notices[0]["denied_count"] == 1
    assert "部分参考资料因权限受限无法展示" in notices[0]["notice"]

    done = events_of(frames, "done")[0]
    assert done["token_usage"]["total"] == 150
    assert done["answer_source"] == "rag"
    assert "retrieval_ms" in done and "auth_ms" in done and "llm_ms" in done

    log = await _wait_log(task_id)
    # ★ 三列表：字段必须**存在**（denied 可以为空数组，但字段不能缺）
    for field in ("recalled_chunks", "allowed_chunks", "denied_chunks"):
        assert field in log, f"qa_logs 缺字段 {field}"
    assert len(log["recalled_chunks"]) == 2
    assert len(log["allowed_chunks"]) == 1
    assert len(log["denied_chunks"]) == 1
    assert log["denied_chunks"][0]["doc_id"] == doc_b
    assert log["dept_id"] == "DEPT0004", "要落提问时的部门快照"
    assert log["role_ids"] == ["ROLE0001"]
    assert log["max_score"] == 0.91
    assert log["degraded"] is False

    # 助手消息带引用卡片与受限计数（AC-06-05）
    messages, _ = await qa_repo.list_messages(session_id)
    assistant = messages[-1]
    assert assistant["role"] == "assistant"
    assert len(assistant["chunk_refs"]) == 1
    assert assistant["chunk_refs"][0]["doc_id"] == doc_a
    assert assistant["denied_count"] == 1
    assert assistant["token_usage"]["total"] == 150


async def test_denied_chunk_never_enters_prompt(client, fake_llm, fake_models,
                                                fake_recall, fake_rerank):
    """AC-06-03 / 标注 3：**被拒切片绝不进 Prompt**（逐字检查发给 LLM 的内容）。

    这是本模块最重要的一条：只靠"在提示词里让模型别说"是不可靠的 ——
    模型会用被拒内容去"润色"答案，边界一松就漏。
    """
    readable = await _new_doc("公开制度.pdf")
    secret = await _new_doc("机密制度.pdf")
    await _grant(readable)
    secret_text = "绝密内容：高管薪酬为 999 万元，仅董事会可见。"
    fake_recall([hit(readable, 1, "公开内容：报销需 15 日内提交。", "流程", 0.9),
                 hit(secret, 2, secret_text, "薪酬", 0.89)])

    await _ask_and_collect(user(), "高管薪酬是多少？")

    assert fake_llm, "必须调用过大模型"
    sent = "\n".join(m["content"] for m in fake_llm[-1])
    assert "绝密内容" not in sent, "被拒切片的内容泄漏进了 Prompt"
    assert "999" not in sent
    assert "公开内容：报销需 15 日内提交。" in sent, "放行切片必须进 Prompt"


async def test_all_denied_uses_conservative_answer_without_llm(client, fake_llm,
                                                               fake_models,
                                                               fake_recall):
    """AC-06-11：全部受限时**不调大模型**、不泄漏标题，走保守话术 + notice。"""
    secret = await _new_doc("机密薪酬制度.pdf")
    fake_recall([hit(secret, 1, "绝密：高管薪酬 999 万。", "薪酬章", 0.93)])

    frames, _, task_id = await _ask_and_collect(user(), "高管薪酬是多少？")

    assert fake_llm == [], "全部受限时不得调用大模型"
    text = "".join(d["text"] for d in events_of(frames, "delta"))
    assert text == DENIED_ALL_ANSWER
    assert "机密薪酬制度" not in text, "不得泄漏被拒文档的标题"
    assert "999" not in text
    notices = events_of(frames, "notice")
    assert notices and notices[0]["denied_count"] == 1
    done = events_of(frames, "done")[0]
    # 枚举不新增：靠 denied_count > 0 区分"真没有"与"有但无权"
    assert done["answer_source"] == "no_knowledge"
    assert done["denied_count"] == 1

    log = await _wait_log(task_id)
    assert log["answer_source"] == "no_knowledge"
    assert len(log["denied_chunks"]) == 1 and log["allowed_chunks"] == []


async def test_no_knowledge_when_recall_empty(client, fake_llm, fake_models,
                                              fake_recall):
    """库里确实没有资料 → 兜底话术，`denied` 为空（与"全部受限"区分）。"""
    fake_recall([])
    frames, _, task_id = await _ask_and_collect(user(), "公司食堂几点开门？")
    text = "".join(d["text"] for d in events_of(frames, "delta"))
    assert text == NO_KNOWLEDGE_ANSWER
    assert events_of(frames, "notice") == [], "没有拦截就不该发受限提示"
    done = events_of(frames, "done")[0]
    assert done["answer_source"] == "no_knowledge" and done["denied_count"] == 0
    log = await _wait_log(task_id)
    assert log["allowed_chunks"] == [] and log["denied_chunks"] == []


async def test_faq_cache_hit_streams_standard_answer(client, fake_llm, fake_models,
                                                     fake_recall):
    """AC-06-08：FAQ 命中时 `faq_hit=true`、`answer_source=faq_cache`、**不调大模型**。"""
    faq_cache.upsert(FaqEntry(faq_id="FAQ000001", question="差旅标准是什么",
                              answer="住宿费一线城市上限 500 元。",
                              vector=[1.0] + [0.0] * (settings.embedding_dim - 1)))
    fake_recall([])                                          # 命中 FAQ 就不该再召回
    frames, _, task_id = await _ask_and_collect(user(), "差旅标准是什么？")

    assert fake_llm == [], "FAQ 直出不得调用大模型"
    text = "".join(d["text"] for d in events_of(frames, "delta"))
    assert text == "住宿费一线城市上限 500 元。"
    meta = events_of(frames, "meta")[0]
    assert meta["faq_hit"] is True and meta["answer_source"] == "faq_cache"
    log = await _wait_log(task_id)
    assert log["faq_hit"] is True and log["faq_id"] == "FAQ000001"


async def test_multi_turn_rewrite_is_recorded(client, fake_llm, fake_models,
                                              fake_recall, fake_rerank):
    """AC-06-09：多轮追问会被改写，且改写结果落到消息与日志里。"""
    doc = await _new_doc("薪酬制度.pdf")
    await _grant(doc)
    fake_recall([hit(doc, 1, "薪酬按岗位等级确定。", "薪酬章", 0.9)])

    subject = user()
    first = await qa_service.ask(user=subject, question="公司的福利有哪些？")
    await asyncio.wait_for(_collect_frames(first.task_id), timeout=20)
    first_calls = len(fake_llm)

    frames, _, task_id = await _ask_and_collect(
        subject, "那高管的薪酬呢？", session_id=first.session_id)
    assert len(fake_llm) > first_calls, "第二轮应触发查询改写"
    assert events_of(frames, "done")

    log = await _wait_log(task_id)
    assert log["rewritten_query"] == "改写后的独立问题"


async def test_rerank_failure_degrades_and_still_answers(client, fake_llm,
                                                         fake_models, fake_recall,
                                                         monkeypatch):
    """AC-06-13 / QA-4004：重排不可用时**跳过重排**，用召回顺序继续作答。"""
    doc = await _new_doc("可读.pdf")
    await _grant(doc)
    fake_recall([hit(doc, 1, "内容甲", "章一", 0.9),
                 hit(doc, 2, "内容乙", "章二", 0.8),
                 hit(doc, 3, "内容丙", "章三", 0.7)])

    def _boom(*_args, **_kwargs):
        raise RerankUnavailable("测试：模型不可用")

    monkeypatch.setattr(rerank_service, "rerank", _boom)
    frames, _, task_id = await _ask_and_collect(user(), "内容是什么？")

    assert events_of(frames, "done"), "重排失败不该让整轮问答失败"
    assert "".join(d["text"] for d in events_of(frames, "delta")) == \
        "".join(ANSWER_PIECES)
    log = await _wait_log(task_id)
    assert log["degraded"] is True, "跳过重排必须记 degraded=true"
    assert rerank_service.degraded_reason, "降级原因要能被观测到"


async def test_embedding_failure_degrades_to_no_knowledge(client, fake_llm,
                                                          monkeypatch, fake_recall):
    """AC-06-13 / QA-4005：向量化不可用 → `no_knowledge` + `degraded`，不抛 500。"""
    from app.services.embedding_service import EmbeddingUnavailable

    def _boom(_text: str):
        raise EmbeddingUnavailable("测试：模型不可用")

    monkeypatch.setattr(embedding_service, "embed_one", _boom)
    fake_recall([hit("DOC1", 1, "不该被用到", "x", 0.9)])
    frames, _, task_id = await _ask_and_collect(user(), "任何问题")
    done = events_of(frames, "done")[0]
    assert done["answer_source"] == "no_knowledge"
    log = await _wait_log(task_id)
    assert log["degraded"] is True
    assert log["recalled_chunks"] == []


async def test_llm_failure_emits_error_event(client, fake_models, monkeypatch,
                                             fake_recall, fake_rerank):
    """QA-4002：大模型失败 → `error` 事件（而不是让前端一直等）。"""
    from app.infra.llm import LLMUnavailable

    doc = await _new_doc("可读2.pdf")
    await _grant(doc)
    fake_recall([hit(doc, 1, "内容", "章", 0.9)])

    async def _boom(*_args, **_kwargs):
        raise LLMUnavailable("测试：大模型 401")
        yield                                              # pragma: no cover

    monkeypatch.setattr(llm_client, "stream_chat", _boom)
    frames, _, _ = await _ask_and_collect(user(), "问题")
    errors = events_of(frames, "error")
    assert errors and errors[0]["code"] == "QA-4002"


# ================================================================ SSE 归属
async def test_stream_ownership_is_enforced(client, fake_llm, fake_models,
                                            fake_recall):
    """AC-06-07 / ER-14：**A 的 task_id 用 B 的身份订阅 → QA-2003，且不建流**。"""
    fake_recall([])
    owner = user(ASKER_ID, "DEPT0004")
    result = await qa_service.ask(user=owner, question="我的问题")
    await asyncio.wait_for(_collect_frames(result.task_id), timeout=20)

    with pytest.raises(BizError) as exc:
        await qa_service.open_stream(task_id=result.task_id,
                                     user=user(OTHER_ID, "DEPT0002"))
    assert exc.value.spec.code == "QA-2003"

    # 不存在的 task_id 同样按"无权订阅"处理（不暴露"存在与否"）
    with pytest.raises(BizError) as exc:
        await qa_service.open_stream(task_id="deadbeef", user=owner)
    assert exc.value.spec.code == "QA-2003"

    # 本人可以订阅
    assert await qa_service.open_stream(task_id=result.task_id, user=owner)


async def test_sessions_and_messages_are_private(client, fake_llm, fake_models,
                                                 fake_recall):
    """AC-06-10 / G-12：A 读不到 B 的会话与消息。"""
    fake_recall([])
    other_user = user(OTHER_ID, "DEPT0002")
    other = await qa_service.ask(user=other_user, question="B 的私有问题")
    await asyncio.wait_for(_collect_frames(other.task_id), timeout=20)

    mine = await qa_service.list_sessions(user=user())
    assert all(item["session_id"] != other.session_id for item in mine["items"])

    with pytest.raises(BizError) as exc:
        await qa_service.session_messages(user=user(), session_id=other.session_id)
    assert exc.value.spec.code == "QA-2001"

    with pytest.raises(BizError) as exc:
        await qa_service.session_messages(user=user(),
                                          session_id="SESS20260101999999")
    assert exc.value.spec.code == "QA-3001"


async def test_feedback_is_reserved_but_validated(client, fake_llm, fake_models,
                                                  fake_recall):
    """反馈（预留）：`rating` 只接受 up/down；只能反馈自己的消息。"""
    fake_recall([])
    subject = user()
    _, session_id, task_id = await _ask_and_collect(subject, "反馈测试")
    await _wait_log(task_id)                                 # 落档完成再反馈
    messages, _ = await qa_repo.list_messages(session_id)
    assistant_id = str(messages[-1]["_id"])

    with pytest.raises(BizError) as exc:
        await qa_service.feedback(user=subject, message_id=assistant_id,
                                  rating="maybe")
    assert exc.value.spec.code == "QA-1002"

    with pytest.raises(BizError) as exc:
        await qa_service.feedback(user=subject, message_id="not-an-objectid",
                                  rating="up")
    assert exc.value.spec.code == "QA-3002"
    with pytest.raises(BizError) as exc:
        await qa_service.feedback(user=user(OTHER_ID, "DEPT0002"),
                                  message_id=assistant_id, rating="up")
    assert exc.value.spec.code == "QA-2001"

    result = await qa_service.feedback(user=subject, message_id=assistant_id,
                                       rating="up", comment="有用")
    assert result == {"message_id": assistant_id, "rating": "up"}
    await asyncio.sleep(0.2)
    log = await mongo.collection(qa_repo.QA_LOGS).find_one(
        {"session_id": session_id})
    assert log["feedback"]["rating"] == "up"


# ================================================================ 与 05 的契约
async def test_one_permission_query_per_turn(client, fake_llm, fake_models,
                                             monkeypatch, fake_recall, fake_rerank):
    """AC-06-02：一轮问答只产生 **1 次** `kb_permissions` 查询（无论召回多少片）。"""
    doc = await _new_doc("多片文档.pdf")
    await _grant(doc)
    fake_recall([hit(doc, i, f"内容{i}", f"章{i}", 0.9 - i * 0.001)
                 for i in range(40)])

    calls: list[list[str]] = []
    original = ps.perm_repo.find_by_docs

    async def counting(doc_ids):
        calls.append(list(doc_ids))
        return await original(doc_ids)

    monkeypatch.setattr(ps.perm_repo, "find_by_docs", counting)
    await _ask_and_collect(user(), "内容是什么？")
    assert len(calls) == 1, f"应只有 1 次权限查询，实际 {len(calls)} 次"
    assert len(calls[0]) == 1, "同一篇文档的 40 个切片只算 1 个 doc_id"


async def test_permission_failure_makes_turn_conservative(client, fake_llm,
                                                          fake_models, monkeypatch,
                                                          fake_recall):
    """05 的 fail-closed 会传导到本模块：查库异常 → 全拦 → 保守话术 + degraded。"""
    doc = await _new_doc("正常文档.pdf")
    await _grant(doc)
    fake_recall([hit(doc, 1, "内容甲", "章一", 0.9)])

    async def _boom(_doc_ids):
        raise RuntimeError("测试：权限库不可用")

    monkeypatch.setattr(ps.perm_repo, "find_by_docs", _boom)
    frames, _, task_id = await _ask_and_collect(user(), "内容是什么？")

    assert fake_llm == [], "鉴权降级时不得调用大模型"
    text = "".join(d["text"] for d in events_of(frames, "delta"))
    assert text == DENIED_ALL_ANSWER
    done = events_of(frames, "done")[0]
    assert done["answer_source"] == "no_knowledge" and done["denied_count"] == 1
    log = await _wait_log(task_id)
    assert log["degraded"] is True
