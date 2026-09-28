# -*- coding: utf-8 -*-
"""模块 06 的服务层：**AI 鉴权问答主链路**。

## 主链路（每一步都写清了"为什么是这个顺序"）

```
提问
 ├─ 0) 校验 + 取用户上下文（user_id / dept_id / role_ids）        ← 标注 1
 ├─ 1) FAQ 缓存匹配（纯内存余弦）──命中──► 直出标准答案（不走大模型）
 ├─ 2) 查询改写（多轮指代消解；失败则降级用原问题）
 ├─ 3) Milvus 召回 top_k = need_k × recall_multiplier（expr: enabled == true）
 ├─ 4) **batch_check() 鉴权过滤**（一次 $in）                      ← 标注 2 / ER-13
 │      └─ denied 切片**绝不进 Prompt**：不是"让模型别说"，是根本不给它  ← 标注 3
 ├─ 5) 重排（rerank 不可用则跳过，用召回顺序）
 ├─ 6) 拼 Prompt（**只含 allowed 切片**）→ 流式生成
 ├─ 7) citation 事件（引用溯源卡片）                                ← 标注 5
 ├─ 8) denied 非空 → notice 事件「部分参考资料因权限受限无法展示」    ← 标注 4 / PRD 硬要求
 └─ 9) 异步落 qa_logs（三列表 + Token + 分段耗时）+ 助手消息        ← 标注 6
```

## 四条不可让步的约束

| # | 约束 | 违反的后果 |
|---|---|---|
| 1 | **denied 切片不进 Prompt** | 只靠"在提示词里让模型别说"是不可靠的：模型会用被拒内容"润色"答案，边界一松就漏 |
| 2 | **全部受限时不用大模型凭空作答** | 那是把"无权查阅"变成"模型编一个"，比答不出来更糟（AC-06-11） |
| 3 | **SSE 订阅必须校验归属** | 存量缺陷：任何人拿到 `task_id` 就能窃听他人问答（AC-06-07 / ER-14） |
| 4 | **`/qa/ask` 立即返回 `task_id`** | 大模型首 token 要几百毫秒到数秒，同步等待会让前端像卡死（AC-06-12） |

## 降级矩阵（Spec §5 的实现口径）

| 依赖 | 不可用时 | `answer_source` | `degraded` |
|---|---|---|---|
| Embedding | 无法检索 → 兜底话术 | `no_knowledge` | ✅ |
| Milvus | 同上 | `no_knowledge` | ✅ |
| **查询改写（LLM）** | 用原问题检索 | 正常 | ✅ |
| **Rerank** | 跳过重排，用召回顺序 | 正常 | ✅ |
| **权限引擎（05）** | **全部拦截**（fail-closed）→ 保守话术 | `no_knowledge` + `denied` 非空 | ✅ |
| 大模型（生成） | 发 `error` 事件 | — | — |
| `qa_logs` 写入 | 只记 ERROR，**不影响用户已看到的答案** | — | — |
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from app.core.config import settings
from app.core.errors import BizError, Err
from app.core.logging import logger
from app.infra.llm import LLMUnavailable, llm_client
from app.infra.milvus import milvus
from app.infra.sse import sse_hub
from app.repositories import qa_repo
from app.services.config_service import config_service
from app.services.doc_service import doc_service
from app.services.embedding_service import (EmbeddingUnavailable,
                                            embedding_service)
from app.services.faq_cache import FaqEntry, faq_cache
from app.services.permission_service import (RESTRICTED_NOTICE,
                                             permission_service)
from app.services.rerank_service import RerankUnavailable, rerank_service

# 会话标题取首轮提问前 N 字（Spec §2.1）
TITLE_CHARS = 20

# 落档（qa_logs + 助手消息）的超时上限：Mongo 卡住时不让后台任务一直挂着。
# 5 秒是"够慢的正常写入 + 不拖住关停"的折中
PERSIST_TIMEOUT_SECONDS = 5.0

# 兜底话术（§4.4 的四种形态，逐字对齐原型与 Spec）
NO_KNOWLEDGE_ANSWER = "知识库中暂无相关内容，已记录该问题。"
DENIED_ALL_ANSWER = ("我在知识库中检索到了相关文档，但您当前所属部门/角色无权查阅该内容。"
                     "如需查看，请联系知识管理员为你的部门或角色开通权限。")

# 系统提示词：三条硬约束（只用资料、标明出处、资料不足要直说）
SYSTEM_PROMPT = (
    "你是企业内部知识库的问答助手。请严格遵守以下规则：\n"
    "1. 只依据【参考资料】回答，不要使用资料之外的知识，也不要编造条款、数字或流程。\n"
    "2. 回答时用 [编号] 标注依据（如 [1][3]），编号对应参考资料的序号。\n"
    "3. 如果参考资料不足以回答，就直接说明「根据现有资料无法完整回答」，"
    "并指出还缺什么信息，不要猜。\n"
    "4. 用简体中文回答，条理清晰，不要输出与问题无关的内容。"
)

# 查询改写的提示词（多轮指代消解，冲突 C-04：入口是"指代消解"而不是"确认商品名"）
REWRITE_PROMPT = (
    "下面是一段对话历史和用户的最新提问。请把最新提问改写成一个**独立、完整、"
    "可检索**的问题：把其中的代词（它/这个/那）和省略的主语补全成明确的说法。\n"
    "要求：只输出改写后的问题本身，不要解释、不要加引号、不要换行。"
    "如果最新提问本身已经完整，就原样输出。"
)


@dataclass(slots=True)
class RecalledChunk:
    """一条召回切片（含鉴权结果）。"""

    chunk_id: Any
    doc_id: str
    content: str
    title: str = ""
    parent_title: str = ""
    doc_title: str = ""
    score: float = 0.0
    allowed: bool = False
    deny_reason: str = ""

    @property
    def citation(self) -> dict[str, Any]:
        """引用溯源卡片的一项（标注 5）。"""
        return {"chunk_id": self.chunk_id, "doc_id": self.doc_id,
                "title": self.title or self.parent_title or self.doc_title,
                "doc_title": self.doc_title}


@dataclass(slots=True)
class Timings:
    """分段耗时（`qa_logs` 的观测字段）。"""

    retrieval_ms: int = 0
    auth_ms: int = 0
    rerank_ms: int = 0
    llm_ms: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)

    def as_dict(self) -> dict[str, int]:
        return {"retrieval_ms": self.retrieval_ms, "auth_ms": self.auth_ms,
                "rerank_ms": self.rerank_ms, "llm_ms": self.llm_ms,
                "elapsed_ms": self.elapsed_ms}


@dataclass(slots=True)
class AskResult:
    """`POST /qa/ask` 的返回值。"""

    task_id: str
    session_id: str
    message_id: str

    @property
    def stream_url(self) -> str:
        return f"/api/v1/qa/stream/{self.task_id}"


class QaService:
    """问答服务（进程级单例）。"""

    def __init__(self) -> None:
        # 幂等键 → 首次受理结果（热路径；`qa_logs` 是它的持久后盾）
        self._requests: dict[str, AskResult] = {}
        self._running: dict[str, asyncio.Task[Any]] = {}

    # ================================================================== 提问
    async def ask(self, *, user: Any, question: str,
                  session_id: str | None = None,
                  request_id: str | None = None) -> AskResult:
        """受理提问：**同步做校验与落库，然后立即返回 `task_id`**（AC-06-12）。"""
        user_id = _user_id(user)
        if not user_id:
            # 标注 1：不允许未登录问答。走到这里说明中间件没装上上下文
            raise BizError(Err.QA_CONTEXT_MISSING, "缺少登录上下文")
        text = (question or "").strip()
        if not text or len(text) > settings.qa_max_question_len:
            raise BizError(Err.QA_QUESTION_INVALID,
                           f"提问长度需在 1~{settings.qa_max_question_len} 字")

        if request_id:
            replayed = await self._replay(request_id)
            if replayed is not None:
                logger.info("幂等命中 request_id=%s → 复用 task=%s", request_id,
                            replayed.task_id)
                return replayed

        session = await self._resolve_session(user_id=user_id, text=text,
                                             session_id=session_id)
        sid = str(session["_id"])

        now = qa_repo.now_ms()
        try:
            message_id = await qa_repo.insert_message({
                "session_id": sid, "user_id": user_id, "role": "user",
                "text": text, "ts": now, "rewritten_query": None})
        except Exception as exc:                            # noqa: BLE001
            raise BizError(Err.QA_MESSAGE_SAVE_FAILED,
                           f"用户消息保存失败：{exc}") from exc

        task_id = uuid.uuid4().hex
        sse_hub.register(task_id, user_id)                  # 绑定 user_id（ER-14）
        result = AskResult(task_id=task_id, session_id=sid, message_id=message_id)
        if request_id:
            self._requests[request_id] = result
            if len(self._requests) > 1000:                  # 只保最近 1000 条
                self._requests.pop(next(iter(self._requests)), None)

        task = asyncio.create_task(
            self._run_pipeline(task_id=task_id, user=user, question=text,
                               session_id=sid, request_id=request_id),
            name=f"qa:{task_id}")
        self._running[task_id] = task
        task.add_done_callback(lambda _: self._running.pop(task_id, None))
        logger.info("问答已受理 task=%s session=%s user=%s", task_id, sid, user_id)
        return result

    async def _replay(self, request_id: str) -> AskResult | None:
        """幂等回放（AC-06-14）：先查内存，再查日志。"""
        cached = self._requests.get(request_id)
        if cached is not None:
            return cached
        row = await qa_repo.find_log_by_request(request_id)
        if not row:
            return None
        task_id = str(row.get("task_id") or "")
        if not task_id:
            # 日志里没有 task_id（历史数据）时**不假装能回放**：
            # 返回一个已结束的任务号会让前端去订阅一个不存在的流
            return None
        replayed = AskResult(task_id=task_id, session_id=str(row.get("session_id")),
                             message_id=str(row.get("message_id") or ""))
        self._requests[request_id] = replayed
        return replayed

    async def _resolve_session(self, *, user_id: str, text: str,
                               session_id: str | None) -> Mapping[str, Any]:
        """定位或新建会话（QA-1003 / QA-2001 / QA-3001 / QA-3003）。"""
        if session_id:
            if not session_id.startswith(qa_repo.SESSION_ID_PREFIX):
                raise BizError(Err.QA_SESSION_ID_INVALID,
                               f"会话编号应以 {qa_repo.SESSION_ID_PREFIX} 开头")
            session = await qa_repo.get_session(session_id)
            if session is None:
                raise BizError(Err.QA_SESSION_NOT_FOUND, f"会话不存在：{session_id}")
            if str(session.get("user_id")) != user_id:
                # G-12：历史问答不跨用户可见。这里用 403 而不是 404 ——
                # 404 会让用户以为"会话被删了"从而反复重试
                raise BizError(Err.QA_SESSION_FORBIDDEN, "无权访问该会话")
            count = await qa_repo.count_messages(session_id)
            if count >= settings.qa_session_msg_limit:
                # 防上下文爆炸：单会话消息数上限（Spec QA-3003）
                raise BizError(Err.QA_SESSION_FULL,
                               f"会话消息已达上限 {settings.qa_session_msg_limit}")
            return session
        now = qa_repo.now_ms()
        new_id = await qa_repo.next_session_id(now)
        await qa_repo.insert_session({
            "_id": new_id, "user_id": user_id,
            "title": text[:TITLE_CHARS], "message_count": 0,
            "last_active_at": now, "created_at": now})
        session = await qa_repo.get_session(new_id)
        assert session is not None
        return session

    # ================================================================== 主链路
    async def _run_pipeline(self, *, task_id: str, user: Any, question: str,
                            session_id: str, request_id: str | None) -> None:
        """后台执行主链路，**通过 SSE 总线推送结果**。

        异常在这里全部收口：任何未捕获异常都要变成 `error` 事件，
        否则前端会一直等一个永远不会来的 `done`（表现为"转圈转到超时"）。
        """
        timings = Timings()
        try:
            await self._pipeline(task_id, user=user, question=question,
                                 session_id=session_id, request_id=request_id,
                                 timings=timings)
        except asyncio.CancelledError:
            logger.warning("问答任务被取消 task=%s", task_id)
            self._publish_error(task_id, Err.QA_LLM_FAILED, "问答已中断")
            raise
        except BizError as exc:
            self._publish_error(task_id, exc.spec, exc.detail or exc.spec.message)
        except Exception as exc:                            # noqa: BLE001
            logger.exception("问答链路出现未预期异常 task=%s", task_id)
            self._publish_error(task_id, Err.QA_LLM_FAILED,
                                f"问答失败：{type(exc).__name__}")
        finally:
            sse_hub.finish(task_id)

    async def _pipeline(self, task_id: str, *, user: Any, question: str,
                        session_id: str, request_id: str | None,
                        timings: Timings) -> None:
        need_k = settings.qa_need_k
        multiplier = config_service.recall_multiplier
        top_k = need_k * multiplier
        user_id = _user_id(user)
        degraded = False

        # ---- 1) FAQ 缓存（纯内存，毫秒级）—— 命中则不走大模型（AC-06-08）
        faq_hit = await self._try_faq(task_id, question, session_id, user,
                                      timings, request_id)
        if faq_hit:
            return

        # ---- 2) 查询改写（多轮指代消解）
        rewritten, rewrite_degraded = await self._rewrite(session_id, question)
        degraded = degraded or rewrite_degraded
        query_text = rewritten or question

        # ---- 3) 召回（expr 只有 enabled == true，ER-11）
        recalled, recall_degraded = await self._recall(query_text, top_k, timings)
        degraded = degraded or recall_degraded

        # ---- 4) 鉴权过滤（一次 $in；ER-13 / 标注 2）
        auth_started = time.monotonic()
        batch = await permission_service.batch_check(
            user, [c.doc_id for c in recalled])
        timings.auth_ms = int((time.monotonic() - auth_started) * 1000)
        degraded = degraded or batch.degraded
        for chunk in recalled:
            chunk.allowed = chunk.doc_id in set(batch.allowed)
            if not chunk.allowed:
                chunk.deny_reason = batch.denied_detail.get(chunk.doc_id, "")
        allowed = [c for c in recalled if c.allowed]
        denied = [c for c in recalled if not c.allowed]

        # ---- 5) 重排（只对 allowed；不可用则跳过）
        ranked, rerank_degraded = await self._rerank(query_text, allowed, need_k,
                                                     timings)
        degraded = degraded or rerank_degraded
        picked = ranked[:need_k]

        await self._publish_meta(task_id, session_id=session_id, faq_hit=False,
                                 answer_source=("rag" if picked else "no_knowledge"),
                                 recalled=len(recalled), allowed=len(allowed),
                                 denied=len(denied), degraded=degraded)

        # ---- 6) 三种结局：无资料 / 全被拦 / 正常作答
        if not picked:
            if denied:
                # 全部受限：**保守话术，不调大模型**（AC-06-11）
                await self._stream_text(task_id, DENIED_ALL_ANSWER)
                source = "no_knowledge"
            else:
                await self._stream_text(task_id, NO_KNOWLEDGE_ANSWER)
                source = "no_knowledge"
            await self._emit_notice(task_id, len(denied))
            await self._finish_answer(
                task_id, session_id=session_id, user=user, question=question,
                rewritten=rewritten, answer=DENIED_ALL_ANSWER if denied
                else NO_KNOWLEDGE_ANSWER, source=source, faq_hit=False,
                recalled=recalled, allowed=allowed, denied=denied, picked=[],
                timings=timings, degraded=degraded, request_id=request_id,
                token_usage={})
            return

        # ---- 7) 拼 Prompt（**只含 allowed**）→ 流式生成
        citations = [c.citation for c in picked]
        await self._publish(task_id, "citation", {"citations": citations})
        answer, usage, llm_degraded = await self._generate(task_id, question,
                                                          picked, timings)
        degraded = degraded or llm_degraded
        await self._emit_notice(task_id, len(denied))
        await self._finish_answer(
            task_id, session_id=session_id, user=user, question=question,
            rewritten=rewritten, answer=answer, source="rag", faq_hit=False,
            recalled=recalled, allowed=allowed, denied=denied, picked=picked,
            timings=timings, degraded=degraded, request_id=request_id,
            token_usage=usage)

    # ------------------------------------------------------------------ 各环节
    async def _try_faq(self, task_id: str, question: str, session_id: str,
                       user: Any, timings: Timings,
                       request_id: str | None) -> bool:
        """FAQ 缓存匹配；命中则直出并返回 `True`（AC-06-08）。

        未发布任何 FAQ 时缓存为空 → 返回 `False` → 正常走 RAG。
        """
        if faq_cache.size == 0:
            return False
        try:
            vector = await asyncio.to_thread(embedding_service.embed_one, question)
        except EmbeddingUnavailable as exc:
            # FAQ 匹配也依赖向量，所以它跟检索一起失效（Spec §5 QA-4005）
            logger.warning("FAQ 匹配跳过（向量化不可用）：%s", exc)
            return False
        hit = faq_cache.match(vector)
        if hit is None:
            return False
        entry: FaqEntry = hit.entry
        await self._publish_meta(task_id, session_id=session_id, faq_hit=True,
                                 answer_source="faq_cache", recalled=0, allowed=0,
                                 denied=0, degraded=False)
        await self._stream_text(task_id, entry.answer)
        await self._finish_answer(
            task_id, session_id=session_id, user=user, question=question,
            rewritten=None, answer=entry.answer, source="faq_cache", faq_hit=True,
            recalled=[], allowed=[], denied=[], picked=[], timings=timings,
            degraded=False, request_id=request_id, token_usage={},
            faq_id=entry.faq_id)
        return True

    async def _rewrite(self, session_id: str, question: str) -> tuple[str | None, bool]:
        """多轮指代消解（AC-06-09）。返回 `(改写后的问题, 是否降级)`。

        首轮**不调用大模型**：没有历史就没有指代，"改写"只会引入变数（也可能改坏）。
        失败/超时时降级为原问题并记 `degraded=true`（Spec §4.3）。
        """
        history = await qa_repo.recent_messages(session_id,
                                                settings.qa_rewrite_turns * 2)
        turns = [m for m in history if m.get("role") in ("user", "assistant")]
        if len(turns) <= 1:                                 # 只有当前这一问 → 首轮
            return None, False
        dialogue = "\n".join(
            f"{'用户' if m.get('role') == 'user' else '助手'}：{str(m.get('text'))[:200]}"
            for m in turns[:-1])
        try:
            result = await llm_client.chat([
                {"role": "system", "content": REWRITE_PROMPT},
                {"role": "user", "content": f"对话历史：\n{dialogue}\n\n"
                                            f"最新提问：{question}"},
            ], temperature=0.0, max_tokens=200)
        except LLMUnavailable as exc:
            logger.warning("查询改写失败，降级用原问题检索：%s", exc)
            return None, True
        text = (result.text or "").strip().strip('"').splitlines()[0].strip() \
            if result.text else ""
        if not text or len(text) > settings.qa_max_question_len:
            return None, True
        logger.info("查询已改写：%s → %s", question[:30], text[:60])
        return text, False

    async def _recall(self, query_text: str, top_k: int,
                      timings: Timings) -> tuple[list[RecalledChunk], bool]:
        """向量召回（只读 Milvus）。不可用时**降级为空召回**而不是报错。"""
        started = time.monotonic()
        try:
            vector = await asyncio.to_thread(embedding_service.embed_one, query_text)
        except EmbeddingUnavailable as exc:
            logger.error("向量化不可用，本轮按无资料处理：%s", exc)
            timings.retrieval_ms = int((time.monotonic() - started) * 1000)
            return [], True
        try:
            hits = await milvus.search(dense=vector, limit=top_k, expr="enabled == true")
        except Exception as exc:                            # noqa: BLE001
            logger.error("Milvus 检索不可用，本轮按无资料处理：%s", exc)
            timings.retrieval_ms = int((time.monotonic() - started) * 1000)
            return [], True
        timings.retrieval_ms = int((time.monotonic() - started) * 1000)
        chunks = [RecalledChunk(
            chunk_id=hit.get("chunk_id"),
            doc_id=str(hit.get("doc_id") or ""),
            content=str(hit.get("content") or ""),
            title=str(hit.get("title") or ""),
            parent_title=str(hit.get("parent_title") or ""),
            score=float(hit.get("score") or 0.0)) for hit in hits]
        await self._fill_doc_titles(chunks)
        logger.info("召回 %d 片（top_k=%d）", len(chunks), top_k)
        return chunks, False

    @staticmethod
    async def _fill_doc_titles(chunks: Sequence[RecalledChunk]) -> None:
        """补文档标题（引用卡片要显示"出自哪篇"）。

        批量取一次（`$in`），并在**同一篇文档的多片之间复用**结果：
        一次问答的 50 片通常只来自 3~5 篇文档，逐片查是白花 45 次往返。
        """
        doc_ids = list(dict.fromkeys(c.doc_id for c in chunks if c.doc_id))
        titles: dict[str, str] = {}
        for doc_id in doc_ids:
            doc = await doc_service.get(doc_id)
            titles[doc_id] = str((doc or {}).get("title") or "")
        for chunk in chunks:
            chunk.doc_title = titles.get(chunk.doc_id, "")

    async def _rerank(self, query: str, allowed: Sequence[RecalledChunk],
                      need_k: int, timings: Timings) -> tuple[list[RecalledChunk], bool]:
        """重排；不可用则**跳过**（返回原顺序）并记降级。"""
        if not allowed:
            return [], False
        started = time.monotonic()
        try:
            ranked = await asyncio.to_thread(
                rerank_service.rerank, query, [c.content for c in allowed],
                top_k=min(need_k, len(allowed)))
        except RerankUnavailable as exc:
            timings.rerank_ms = int((time.monotonic() - started) * 1000)
            rerank_service.note_skip(str(exc))
            # 跳过重排时的顺序 = 召回顺序（按 Milvus 的相似度）
            return list(allowed)[:need_k], True
        timings.rerank_ms = int((time.monotonic() - started) * 1000)
        return [allowed[index] for index, _ in ranked], False

    async def _generate(self, task_id: str, question: str,
                        picked: Sequence[RecalledChunk],
                        timings: Timings) -> tuple[str, dict[str, int], bool]:
        """拼 Prompt + 流式生成。返回 `(完整答案, token 用量, 是否降级)`。"""
        prompt = build_prompt(question, picked)
        collected: list[str] = []
        usage: dict[str, int] = {}
        started = time.monotonic()
        try:
            async for chunk in llm_client.stream_chat(
                    [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": prompt}]):
                if chunk.text:
                    collected.append(chunk.text)
                    await self._publish(task_id, "delta", {"text": chunk.text})
                if chunk.usage is not None and not chunk.usage.empty:
                    usage = chunk.usage.as_dict()
        except LLMUnavailable as exc:
            timings.llm_ms = int((time.monotonic() - started) * 1000)
            logger.error("大模型调用失败 task=%s：%s", task_id, exc)
            raise BizError(Err.QA_LLM_FAILED, str(exc)) from exc
        timings.llm_ms = int((time.monotonic() - started) * 1000)
        answer = "".join(collected).strip()
        if not answer:
            # 模型返回空：**不能落一条空答案**（界面上是"回答了但什么都没有"）
            answer = NO_KNOWLEDGE_ANSWER
        return answer, usage, False

    # ------------------------------------------------------------------ 事件
    async def _publish(self, task_id: str, event: str, data: dict[str, Any]) -> None:
        sse_hub.publish(task_id, event, data)

    async def _publish_meta(self, task_id: str, **data: Any) -> None:
        """`meta` 事件：前端据此先渲染 `RAG · 已鉴权过滤` 徽标（标注 2）。"""
        await self._publish(task_id, "meta", {"task_id": task_id, **data})

    async def _stream_text(self, task_id: str, text: str) -> None:
        """把一段固定文案按小块推成 `delta`（保持与流式一致的交互观感）。"""
        step = 20
        for start in range(0, len(text), step):
            await self._publish(task_id, "delta", {"text": text[start:start + step]})

    async def _emit_notice(self, task_id: str, denied_count: int) -> None:
        """`notice` 事件（**仅当有被拦截的切片**，AC-06-04 / PRD 硬要求）。"""
        if denied_count <= 0:
            return
        await self._publish(task_id, "notice",
                            {"notice": RESTRICTED_NOTICE,
                             "denied_count": denied_count})

    def _publish_error(self, task_id: str, spec: Any, message: str) -> None:
        """`error` 事件（LLM 失败等）。"""
        sse_hub.publish(task_id, "error",
                        {"code": getattr(spec, "code", Err.QA_LLM_FAILED.code),
                         "message": message[:300]})

    # ------------------------------------------------------------------ 收尾
    async def _finish_answer(self, task_id: str, *, session_id: str, user: Any,
                             question: str, rewritten: str | None, answer: str,
                             source: str, faq_hit: bool,
                             recalled: Sequence[RecalledChunk],
                             allowed: Sequence[RecalledChunk],
                             denied: Sequence[RecalledChunk],
                             picked: Sequence[RecalledChunk], timings: Timings,
                             degraded: bool, request_id: str | None,
                             token_usage: Mapping[str, int],
                             faq_id: str | None = None) -> None:
        """发 `done` 事件，然后落日志与助手消息。

        ⚠️ **`done` 先发、落档后做**：用户已经拿到完整答案，落档慢一点无感；
        但落档**必须在本任务内完成**（而不是再 `create_task` 甩出去）——
        甩出去的后果有两个，都很实际：
        ① 关停时这些写入会被连带取消，日志悄悄丢掉（而 09 的看板就少算轮次）；
        ② 测试/脚本进程随后关掉 Mongo，写入撞上 "Cannot use AsyncMongoClient
           after close" —— 日志里全是噪声，真正的错误被淹没。
        """
        await self._publish(task_id, "done", {
            "session_id": session_id,
            "answer_source": source, "faq_hit": faq_hit,
            "denied_count": len(denied),
            "token_usage": dict(token_usage), **timings.as_dict(),
        })
        try:
            # 给一个上限：Mongo 卡住时不能让后台任务一直挂着
            await asyncio.wait_for(self._persist(
                task_id=task_id, session_id=session_id, user=user,
                question=question, rewritten=rewritten, answer=answer,
                source=source, faq_hit=faq_hit, recalled=recalled, allowed=allowed,
                denied=denied, picked=picked, timings=timings, degraded=degraded,
                request_id=request_id, token_usage=token_usage, faq_id=faq_id),
                timeout=PERSIST_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.error("问答落档超时（%ss），已放弃：task=%s",
                         PERSIST_TIMEOUT_SECONDS, task_id)
        except Exception as exc:                            # noqa: BLE001
            logger.error("问答落档异常 task=%s：%s", task_id, exc)

    async def _persist(self, *, task_id: str, session_id: str, user: Any,
                       question: str, rewritten: str | None, answer: str,
                       source: str, faq_hit: bool,
                       recalled: Sequence[RecalledChunk],
                       allowed: Sequence[RecalledChunk],
                       denied: Sequence[RecalledChunk],
                       picked: Sequence[RecalledChunk], timings: Timings,
                       degraded: bool, request_id: str | None,
                       token_usage: Mapping[str, int],
                       faq_id: str | None) -> None:
        """写 `qa_logs` + 助手消息 + 更新会话（**失败只记 ERROR**）。

        为什么把日志与消息放同一个异步任务里：它们是"这一轮问答的档案"，
        分开写会让"消息有了但日志没有"变成常态，而 09 的看板会因此少算轮次。
        """
        now = qa_repo.now_ms()
        citations = [c.citation for c in picked]
        try:
            message_id = await qa_repo.insert_message({
                "session_id": session_id, "user_id": _user_id(user),
                "role": "assistant", "text": answer, "ts": now,
                "rewritten_query": rewritten, "chunk_refs": citations,
                "denied_count": len(denied),
                "token_usage": dict(token_usage),
                "elapsed_ms": timings.elapsed_ms})
        except Exception as exc:                            # noqa: BLE001
            message_id = ""
            logger.error("助手消息写入失败 session=%s：%s", session_id, exc)

        try:
            log_id = await qa_repo.next_log_id(now)
            doc: dict[str, Any] = {
                "_id": log_id,
                # `task_id` / `message_id` 是**为幂等回放与反馈定位**加的最小冗余：
                # 没有它们，同一个 request_id 再来时无法告诉调用方"原任务号是多少"
                "task_id": task_id, "message_id": message_id,
                "session_id": session_id, "user_id": _user_id(user),
                "dept_id": _dept_id(user), "role_ids": sorted(_role_ids(user)),
                "question": question, "rewritten_query": rewritten, "asked_at": now,
                # ★ 三列表：召回（含被拦）/ 放行 / 拦截（标注 6）
                "recalled_chunks": [{"chunk_id": c.chunk_id, "doc_id": c.doc_id,
                                     "score": c.score} for c in recalled],
                "allowed_chunks": [c.chunk_id for c in allowed],
                "denied_chunks": [{"chunk_id": c.chunk_id, "doc_id": c.doc_id,
                                   "reason": c.deny_reason} for c in denied],
                "faq_hit": faq_hit, "faq_id": faq_id, "answer_source": source,
                "token_usage": dict(token_usage) or None,
                "max_score": max((c.score for c in recalled), default=None),
                "degraded": degraded, "feedback": None,
                **timings.as_dict(),
            }
            if request_id:
                # ⚠️ **不传 `request_id` 时必须整个字段都不出现**。
                # 唯一稀疏索引只跳过"字段缺失"的文档，**不跳过值为 `null` 的** ——
                # 如果没有这一层判断，第二条不带 request_id 的问答就会撞上
                # `dup key: { request_id: null }`，而症状是"从第二轮开始所有日志都写不进去"
                # （看板上却只是"数据变少了"，极难定位）。
                doc["request_id"] = request_id
            await qa_repo.insert_log(doc)
        except Exception as exc:                            # noqa: BLE001
            # ER-06：日志只由本模块写；写失败**不影响用户已看到的答案**
            logger.error("问答日志写入失败（code=%s）task=%s：%s",
                         Err.QA_LOG_WRITE_FAILED.code, task_id, exc)
            log_id = ""

        try:
            count = await qa_repo.count_messages(session_id)
            await qa_repo.touch_session(session_id, ts_ms=now, message_count=count)
        except Exception as exc:                            # noqa: BLE001
            logger.warning("会话活跃度更新失败 session=%s：%s", session_id, exc)
        logger.info("问答已落档 task=%s log=%s source=%s 三列表=%d/%d/%d 耗时=%dms",
                    task_id, log_id, source, len(recalled), len(allowed),
                    len(denied), timings.elapsed_ms)

        # ---- 06 → 09 交付：把本轮的运营计数投给看板（ER-07：桶只由 09 写）----
        # 为什么放在**最后**并再包一层 try：指标是旁路观测，
        # 绝不能因为它失败而去回滚"用户已经看到的答案 + 已经落档的日志"。
        # `metric_service.inc` 自身也已吞异常，这里是第二道保险。
        # 签名逐字对齐 09 Spec §3.6 的内部契约（token 拆成 prompt/completion，
        # **不含 embedding** —— G-07/AC-09-07 就是靠这里只传大模型用量来保证的）。
        try:
            from app.services.metric_service import metric_service

            usage = dict(token_usage or {})
            await metric_service.inc(
                _user_id(user), _dept_id(user) or "", ts_ms=now,
                faq_hit=faq_hit, answer_source=source,
                token_prompt=int(usage.get("prompt") or 0),
                token_completion=int(usage.get("completion") or 0),
                elapsed_ms=timings.elapsed_ms, denied_chunk_cnt=len(denied),
                # Spec §4.1 第 6 步：`doc_ids` = **放行列表**去重后的 doc_id
                # （热门知识榜统计的是"哪些知识被授权引用过"，不是最终答了几条）
                doc_ids=[c.doc_id for c in allowed], faq_id=faq_id)
        except Exception as exc:                            # noqa: BLE001
            logger.warning("运营指标投递失败（不影响问答）task=%s：%s", task_id, exc)

    # ================================================================== SSE
    async def open_stream(self, *, task_id: str, user: Any) -> Any:
        """校验归属并返回 SSE 帧生成器（**建流之前**校验，AC-06-07）。"""
        owner = sse_hub.owner_of(task_id)
        if owner is None:
            # 任务从未注册 / 已回收：按"无权订阅"处理，**不暴露"存在与否"**
            raise BizError(Err.QA_STREAM_FORBIDDEN, "该问答流不存在或已结束")
        if owner != _user_id(user):
            logger.warning("越权订阅被拒：task=%s owner=%s caller=%s",
                           task_id, owner, _user_id(user))
            raise BizError(Err.QA_STREAM_FORBIDDEN, "无权订阅该问答流")
        return self._frames(task_id)

    @staticmethod
    async def _frames(task_id: str) -> Any:
        """把总线事件编码成 SSE 帧（路由层直接 `StreamingResponse`）。"""
        from app.infra.sse import format_sse

        async for item in sse_hub.subscribe(task_id):
            yield format_sse(item["event"], item["data"])

    # ================================================================== 历史
    async def list_sessions(self, *, user: Any, keyword: str = "", page: int = 1,
                            page_size: int = 20) -> dict[str, Any]:
        """侧边栏：**仅本人**（G-12 / AC-06-10）。"""
        rows, total = await qa_repo.list_sessions(
            user_id=_user_id(user), keyword=keyword, page=page, page_size=page_size)
        return {"items": [{"session_id": r["_id"], "title": r.get("title", ""),
                           "message_count": int(r.get("message_count") or 0),
                           "last_active_at": r.get("last_active_at")}
                          for r in rows],
                "total": total, "page": page, "page_size": page_size}

    async def session_messages(self, *, user: Any, session_id: str, page: int = 1,
                               page_size: int = 100) -> dict[str, Any]:
        """会话消息回放（含 `chunk_refs` 引用卡片与 `denied_count` 提示）。"""
        session = await qa_repo.get_session(session_id)
        if session is None:
            raise BizError(Err.QA_SESSION_NOT_FOUND, f"会话不存在：{session_id}")
        if str(session.get("user_id")) != _user_id(user):
            raise BizError(Err.QA_SESSION_FORBIDDEN, "无权访问该会话")
        rows, _ = await qa_repo.list_messages(session_id, page=page,
                                              page_size=page_size)
        return {"session_id": session_id, "title": session.get("title", ""),
                "messages": [{
                    "message_id": str(r["_id"]), "role": r.get("role"),
                    "text": r.get("text", ""),
                    "chunk_refs": r.get("chunk_refs") or [],
                    "denied_count": int(r.get("denied_count") or 0),
                    "token_usage": r.get("token_usage"),
                    "elapsed_ms": r.get("elapsed_ms"),
                    "rewritten_query": r.get("rewritten_query"),
                    "ts": r.get("ts")} for r in rows]}

    async def feedback(self, *, user: Any, message_id: str, rating: str,
                       comment: str = "") -> dict[str, Any]:
        """答案反馈（PRD 预留）：写 `qa_logs.feedback`。"""
        if rating not in ("up", "down"):
            raise BizError(Err.QA_FEEDBACK_INVALID, "rating 只能是 up / down")
        message = await qa_repo.get_message(message_id)
        if message is None:
            raise BizError(Err.QA_MESSAGE_NOT_FOUND, f"消息不存在：{message_id}")
        if str(message.get("user_id")) != _user_id(user):
            raise BizError(Err.QA_SESSION_FORBIDDEN, "只能反馈自己的问答")
        # 按 `message_id` 精确定位这一轮的日志（不是"按会话找最新一条"：
        # 那会把反馈写到另一轮上，而错位在任何界面上都看不出来）
        log = await qa_repo.find_log_by_message(message_id)
        if log is not None:
            await qa_repo.attach_feedback(str(log["_id"]),
                                          {"rating": rating, "comment": comment[:200],
                                           "at": qa_repo.now_ms()})
        return {"message_id": message_id, "rating": rating}

    # ================================================================== 维护
    async def shutdown(self) -> None:
        """关停：取消在跑问答并唤醒订阅者（不留"永远等不到 done"的前端）。"""
        pending = list(self._running.items())
        for task_id, task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*[t for _, t in pending], return_exceptions=True)
        self._running.clear()
        await llm_client.close()

    def reap(self) -> int:
        """回收过期的 SSE 任务（scheduler 定期调）。"""
        return sse_hub.reap(settings.qa_stream_ttl_seconds)

    def health(self) -> dict[str, Any]:
        return {"llm": llm_client.health(), "rerank": rerank_service.health(),
                "faq_cache": faq_cache.stats(), "sse": sse_hub.stats(),
                "need_k": settings.qa_need_k,
                "recall_multiplier": config_service.recall_multiplier}


def build_prompt(question: str, chunks: Sequence[RecalledChunk]) -> str:
    """把 **allowed** 切片拼成 Prompt（编号即引用卡片里的 idx）。

    编号从 1 开始且与 `citation` 事件的顺序**完全一致**：
    两边不一致的话，答案里的 `[2]` 会指向另一张卡片，引用溯源就成了误导。
    """
    blocks = []
    for index, chunk in enumerate(chunks, start=1):
        title = chunk.title or chunk.parent_title or "（无标题）"
        blocks.append(f"[{index}] 文档：{chunk.doc_title or '未命名'}"
                      f"｜章节：{title}\n{chunk.content}")
    materials = "\n\n".join(blocks)
    return (f"【参考资料】\n{materials}\n\n"
            f"【问题】\n{question}\n\n"
            f"请依据上述参考资料回答，并用 [编号] 标注依据。")


# ---------------------------------------------------------------------- 小工具
def _user_id(user: Any) -> str:
    return str(getattr(user, "user_id", "") or "")


def _dept_id(user: Any) -> str:
    return str(getattr(user, "dept_id", "") or "")


def _role_ids(user: Any) -> Sequence[str]:
    value = getattr(user, "role_ids", None)
    if value is None:
        return []
    return sorted(str(v) for v in value)


qa_service = QaService()

__all__ = [
    "QaService", "qa_service", "AskResult", "RecalledChunk", "Timings",
    "build_prompt", "NO_KNOWLEDGE_ANSWER", "DENIED_ALL_ANSWER",
    "SYSTEM_PROMPT", "TITLE_CHARS",
]
