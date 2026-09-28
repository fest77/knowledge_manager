# -*- coding: utf-8 -*-
"""模块 07 的服务层：**FAQ 沉淀**（挖掘 → 审核 → 发布 → 缓存直出）。

```
qa_logs（06 只读）                     ← 挖掘原料（ER-06：07 不写）
   │  ① 时间窗 + faq_hit=false + question 非空
   ▼
聚类（相似度 ≥ faq.mine_sim_threshold）
   │  ② 簇频次 ≥ faq.mine_freq_threshold 才产出候选
   ▼
E16 faq_candidates（pending）           ← 审核页（原型 05）
   │  ③ 人工审核：approve（可改写问法/答案）或 reject（必填备注）
   ▼
E17 faqs（含 embedding）                ← 唯一真源
   │  ④ 缓存准入：related_doc_ids 必须**全局可见**（经 05 判定，ER-03）
   ▼
E18 FaqCache（进程内）──► 06 的 `match()` 毫秒级直出
```

## 四条本模块独有的约束（写错任何一条都会造成安全或一致性问题）

| # | 约束 | 为什么 |
|---|---|---|
| 1 | **只有"全局可见"文档的 FAQ 才能进缓存**（§1.5 / AC-07-15/16） | 缓存直出**不经过鉴权**。放进受限文档的 FAQ = 给所有人开一条绕过四维权限的捷径 |
| 2 | **准入判定经 05，失败 fail-closed**（ER-03/ER-04） | "查不到就放行"会直接把越权内容送进缓存。失败时**可发布但 `enabled=false`** |
| 3 | **07 不写 `qa_logs` / `knowledge_gaps`** | 那是 06/08 的实体（AC-07-19/30）。转建一律**投递**给 08 |
| 4 | **`hit_count` 只由 07 写** | 它是"缓存命中率"的分子，谁都能改就没人能解释看板上的数字（AC-07-18） |

## 可复现（AC-07-21）

聚类**完全确定**：日志按 `(asked_at, question)` 排序后贪心归簇，
同一输入必然得到同一组 `cluster_key`。`faq.mine_seed` 被记录进审计，
但算法不依赖随机数——**比"固定种子"更强**：换个种子结果也一样。
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from app.core.config import settings
from app.core.enums import FaqCandidateStatus
from app.core.errors import BizError, Err
from app.core.logging import logger
from app.infra.llm import LLMUnavailable, llm_client
from app.repositories import faq_repo
from app.services.audit_service import audit_service
from app.services.config_service import config_service
from app.services.doc_service import doc_service
from app.services.embedding_service import EmbeddingUnavailable, embedding_service
from app.services.faq_cache import FaqEntry, faq_cache
from app.services.permission_service import permission_service

# 提问长度下限：太短的问句（"？"、"在吗"）不参与挖掘——它们不是知识需求
MIN_QUESTION_CHARS = 2
# 时间窗内扫描日志的上限（Spec §3.4 R-04；演示环境不会触发）
MAX_SCANNED_LOGS = 200_000
# 文档号形态（`DOC20260925000001`）：用来把"切片主键"与"文档号"区分开，
# 见 `_doc_ids_of()` 的说明——把切片号当文档号会让候选永远审不过
DOC_ID_RE = re.compile(r"^DOC\d+$")
# 参考答案草案的提示词（失败只降级，不阻断候选生成）
DRAFT_PROMPT = (
    "下面是一组来自企业知识库的用户提问，它们语义相近，被聚成了同一簇。\n"
    "请写一段**简洁、可核对**的标准答案草稿（100 字以内），"
    "只依据通用的企业管理常识，不要编造具体金额、期限或流程编号；"
    "如果不确定，就写出「需要补充：」开头的待补信息清单。"
)


@dataclass(slots=True)
class Cluster:
    """一个提问簇（挖掘的中间产物）。"""

    questions: list[dict[str, Any]] = field(default_factory=list)
    related_docs: list[str] = field(default_factory=list)
    # 代表问法（出现次数最多；并列取最早）—— 由 `_finalize` 计算
    representative: str = ""
    frequency: int = 0
    first_seen_at: int = 0
    last_seen_at: int = 0
    confidence: float = 0.0
    # 簇内向量（聚类时就拿到，用来算置信度；不为了置信度再调一次模型）
    vectors: list[list[float]] = field(default_factory=list)


@dataclass(slots=True)
class MineResult:
    """一次挖掘的结果（`POST /faq/mine` 的出参）。"""

    window_start: int
    window_end: int
    scanned_logs: int = 0
    clusters: int = 0
    candidates_created: int = 0
    candidates_updated: int = 0
    candidates_skipped_suppressed: int = 0
    gaps_forwarded: int = 0
    elapsed_ms: int = 0
    degraded: bool = False
    notice: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"window_start": self.window_start, "window_end": self.window_end,
                "scanned_logs": self.scanned_logs, "clusters": self.clusters,
                "candidates_created": self.candidates_created,
                "candidates_updated": self.candidates_updated,
                "candidates_skipped_suppressed": self.candidates_skipped_suppressed,
                "gaps_forwarded": self.gaps_forwarded,
                "elapsed_ms": self.elapsed_ms, "degraded": self.degraded,
                "notice": self.notice}


class FaqService:
    """FAQ 服务（进程级单例）。

    `_mining` 是**进程内互斥标志**：手动挖掘与定时挖掘共用它（AC-07-20 / `FAQ-3007`）。
    放在实例而不是模块级，是为了让测试能干净地重置。
    """

    def __init__(self) -> None:
        self._mining = False

    # ================================================================== 挖掘
    async def mine(self, *, window_days: int | None = None,
                   freq_threshold: int | None = None,
                   sim_threshold: float | None = None,
                   seed: int | None = None, actor: str = "system") -> MineResult:
        """挖掘：读日志 → 聚类 → 产出候选（同步返回，演示要即时看到结果）。"""
        # ⚠️ 必须用 `is None` 判"没传"，不能用 `or`：
        # `window_days=0` 是**非法入参**（要报 FAQ-1001），而 `0 or 默认值`
        # 会把它悄悄当成"没传"，于是非法入参被静默接受——这类 bug 在界面上
        # 表现为"填 0 也能跑"，而它掩盖的是一次本该发生的参数校验
        days = config_service.faq_mine_window_days if window_days is None \
            else int(window_days)
        if not 1 <= days <= 180:
            raise BizError(Err.FAQ_WINDOW_INVALID, f"window_days 需在 1~180：{days}")
        freq_min = config_service.faq_mine_freq_threshold \
            if freq_threshold is None else int(freq_threshold)
        if not 2 <= freq_min <= 1000:
            raise BizError(Err.FAQ_THRESHOLD_INVALID, f"freq_threshold 需在 2~1000：{freq_min}")
        sim_min = config_service.faq_mine_sim_threshold \
            if sim_threshold is None else float(sim_threshold)
        if not 0.5 <= sim_min <= 1.0:
            raise BizError(Err.FAQ_THRESHOLD_INVALID, f"sim_threshold 需在 0.5~1.0：{sim_min}")
        seed_value = int(seed if seed is not None else config_service.faq_mine_seed)

        if self._mining:
            raise BizError(Err.FAQ_MINING_RUNNING, "已有挖掘任务在运行中")
        self._mining = True
        started = time.monotonic()
        now = _now_ms()
        # ⚠️ `window_start` 必须**稳定**（对齐到当天 00:00），不能用 `now - days*86400_000`：
        # 后者每次调用都差几毫秒 → 唯一索引 `cluster_key + window_start` 永远不冲突 →
        # 同一天连跑两次挖掘会**生成两份候选**（AC-07-01 直接失败）。
        # 对齐到"天"是"同一批日志就算同一个窗口"的最小实现。
        window_start = _start_of_day(now - days * 86400_000)
        result = MineResult(window_start=window_start, window_end=now)
        try:
            logs = await self._read_logs(window_start, now)
            result.scanned_logs = len(logs)
            if not logs:
                # R-05：**不是失败**（HTTP 200 + 告警信息），前端据此区分
                # "窗口内真没数据"与"确实 0 候选"
                result.notice = (f"近 {days} 天没有可用的问答日志"
                                 "（已命中 FAQ 直出的日志不参与挖掘）")
                logger.warning("FAQ 挖掘：%s", result.notice)
                await self._audit_mine(actor, result, days, freq_min, sim_min,
                                       seed_value)
                return result

            vectors = await self._embed_questions(logs)
            clusters = self._cluster(logs, vectors, sim_min)
            result.clusters = len(clusters)
            suppressed = await self._suppressed_keys(now,
                                                     config_service.faq_mine_suppress_days)
            for cluster in clusters:
                if cluster.frequency < freq_min:
                    # AC-07-02：频次不到阈值的簇不产出候选
                    continue
                if cluster.representative and \
                        faq_repo.cluster_key_of(cluster.representative) in suppressed:
                    # DEC-07-3：已驳回的簇在抑制期内不再打扰审核人
                    result.candidates_skipped_suppressed += 1
                    continue
                if await self._emit_candidate(cluster, window_start, now, freq_min,
                                              result):
                    result.candidates_updated += 1
                else:
                    result.candidates_created += 1
        except BizError:
            raise
        except Exception as exc:                            # noqa: BLE001
            logger.exception("FAQ 挖掘失败（窗口 %s~%s）", window_start, now)
            raise BizError(Err.FAQ_MINE_FAILED, f"挖掘失败：{exc}") from exc
        finally:
            self._mining = False
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info("FAQ 挖掘完成：扫描 %d 条日志 → %d 簇 → 新建 %d / 更新 %d",
                    result.scanned_logs, result.clusters, result.candidates_created,
                    result.candidates_updated)
        await self._audit_mine(actor, result, days, freq_min, sim_min, seed_value)
        return result

    @property
    def mining(self) -> bool:
        """是否正在挖掘（`/health` 观测与并发保护）。"""
        return self._mining

    async def _read_logs(self, window_start: int, window_end: int
                         ) -> list[dict[str, Any]]:
        """读时间窗内的问答日志（**只读**，过滤 `faq_hit=true`，ER-06 / R-07）。"""
        from app.infra.mongo import mongo
        from app.repositories import qa_repo

        cursor = (mongo.collection(qa_repo.QA_LOGS)
                  .find({"asked_at": {"$gte": window_start, "$lte": window_end},
                         "faq_hit": False},
                        # `recalled_chunks` 是"切片主键 → 文档号"的**唯一**映射来源，
                        # 少了它就无法把 `allowed_chunks`（切片号）还原成关联文档
                        {"question": 1, "allowed_chunks": 1, "recalled_chunks": 1,
                         "asked_at": 1, "user_id": 1})
                  .sort("asked_at", -1).limit(MAX_SCANNED_LOGS + 1))
        rows = await cursor.to_list(length=MAX_SCANNED_LOGS + 1)
        if len(rows) > MAX_SCANNED_LOGS:
            logger.warning("窗口内日志超过 %d 条，已按时间倒序截断", MAX_SCANNED_LOGS)
            rows = rows[:MAX_SCANNED_LOGS]
        return [r for r in rows if len(str(r.get("question") or "").strip())
                >= MIN_QUESTION_CHARS]

    async def _embed_questions(self, logs: Sequence[Mapping[str, Any]]
                               ) -> dict[str, list[float]]:
        """把窗口内的提问**批量**向量化（去重后一次前向）。

        为什么必须用向量而不是字面相似：真实的高频簇长这样 ——
        「生鲜食品破损如何申请退款 / 水果烂了怎么赔 / 到货坏了怎么办」，
        三句话**几乎不共享字符**，只有语义向量能把它们聚到一起（AC-07-04）。
        我最初用字符 2-gram 的 Jaccard 做聚类，结果这三条各成一簇、
        阈值形同虚设 —— 这是被测试直接抓出来的设计错误。

        向量化失败即 `FAQ-4001`（AC-07-23）：没有向量就没有聚类，
        返回"0 候选"会让调用方以为"窗口里没问题"，而事实是"服务坏了"。
        """
        unique = list(dict.fromkeys(
            str(row.get("question") or "").strip() for row in logs
            if str(row.get("question") or "").strip()))
        if not unique:
            return {}
        try:
            vectors = await asyncio.to_thread(embedding_service.embed, unique)
        except EmbeddingUnavailable as exc:
            raise BizError(Err.FAQ_EMBEDDING_FAILED,
                           f"挖掘时向量化失败：{exc}") from exc
        return dict(zip(unique, vectors))

    def _cluster(self, logs: Sequence[Mapping[str, Any]],
                 vectors: Mapping[str, Sequence[float]], sim_min: float
                 ) -> list[Cluster]:
        """贪心聚类（**确定性**）：按时间升序，逐条归入第一个够像的簇。

        为什么贪心而不是 k-means：k-means 需要预先指定 k，而"这 30 天里到底有
        几个高频问题"正是我们要**求出来**的东西。贪心 + 阈值天然给出簇数。

        相似度用**语义向量余弦**（`faq.mine_sim_threshold`，默认 0.85）；
        簇心取"已有成员的逐维均值"，让后续提问按簇的整体语义吸附。
        """
        ordered = sorted(logs, key=lambda r: (r.get("asked_at") or 0,
                                              str(r.get("question") or "")))
        clusters: list[Cluster] = []
        centroids: list[list[float]] = []
        members: list[list[list[float]]] = []
        for row in ordered:
            question = str(row.get("question") or "").strip()
            if not question:
                continue
            vector = [float(x) for x in (vectors.get(question) or [])]
            target = -1
            best = 0.0
            for index, centroid in enumerate(centroids):
                score = _cosine(vector, centroid)
                if score >= sim_min and score > best:
                    target, best = index, score
            if target < 0:
                clusters.append(Cluster())
                centroids.append(list(vector))
                members.append([list(vector)])
                target = len(clusters) - 1
            else:
                members[target].append(list(vector))
                centroids[target] = _mean_vector(members[target])
            cluster = clusters[target]
            cluster.vectors.append(vector)
            asked_at = int(row.get("asked_at") or 0)
            cluster.questions.append({"question": question, "asked_at": asked_at,
                                      "doc_ids": _doc_ids_of(row)})
            cluster.first_seen_at = (min(cluster.first_seen_at, asked_at)
                                     if cluster.first_seen_at else asked_at)
            cluster.last_seen_at = max(cluster.last_seen_at, asked_at)
        for cluster in clusters:
            _finalize(cluster)
        # 按频次降序返回（与审核列表的行序一致）
        clusters.sort(key=lambda c: (-c.frequency, c.representative))
        return clusters

    async def _emit_candidate(self, cluster: Cluster, window_start: int, now: int,
                              freq_min: int, result: MineResult) -> bool:
        """把簇写成候选（幂等 upsert）。返回"是否已存在并更新"。"""
        representative = cluster.representative
        cluster_key = faq_repo.cluster_key_of(representative)
        existing = await faq_repo.find_candidate(cluster_key, window_start=window_start)
        if existing is not None and existing.get("status") != \
                FaqCandidateStatus.PENDING.value:
            # 已审核过的同窗候选不再覆盖：审核留痕（谁批的、批的什么）比"重算频次"重要
            return True
        draft, degraded = await self._draft_answer(cluster)
        if degraded:
            result.degraded = True
            result.notice = result.notice or "参考答案草案生成失败，请人工填写答案"
        questions = faq_repo.clamp_questions(cluster.questions)
        seq = await faq_repo.next_candidate_seq()
        doc = {
            "_id": (existing or {}).get("_id") or faq_repo.next_id(
                faq_repo.CANDIDATE_ID_PREFIX, seq),
            "cluster_key": cluster_key,
            "questions": [q["question"] for q in questions],
            "representative_question": representative,
            "frequency": cluster.frequency,
            "related_docs": cluster.related_docs,
            "draft_answer": draft,
            "confidence": _confidence(cluster.vectors, cluster.frequency,
                                      freq_min),
            "status": FaqCandidateStatus.PENDING.value,
            "reviewed_by": None, "reviewed_at": None, "review_note": None,
            "faq_id": None,
            "first_seen_at": cluster.first_seen_at,
            "last_seen_at": cluster.last_seen_at,
            "window_start": window_start, "window_end": now,
            "created_at": (existing or {}).get("created_at") or now,
            "updated_at": now,
        }
        created = await faq_repo.upsert_candidate(doc)
        return bool(existing) and not created

    async def _draft_answer(self, cluster: Cluster) -> tuple[str, bool]:
        """用大模型生成参考答案草案；失败**降级为空串**（`FAQ-4004`，候选照常生成）。"""
        samples = "\n".join(f"- {q['question']}" for q in cluster.questions[:10])
        try:
            result = await llm_client.chat(
                [{"role": "system", "content": DRAFT_PROMPT},
                 {"role": "user", "content": f"用户提问：\n{samples}"}],
                temperature=0.2, max_tokens=300)
        except LLMUnavailable as exc:
            logger.warning("草案生成失败（候选仍会生成，由人工填写答案）：%s", exc)
            return "", True
        return (result.text or "").strip()[:2000], False

    async def _suppressed_keys(self, now: int, suppress_days: int) -> set[str]:
        """驳回抑制名单（DEC-07-3）：近 N 天被驳回的簇不再生成候选。"""
        from app.infra.mongo import mongo

        since = now - suppress_days * 86400_000
        cursor = mongo.collection(faq_repo.CANDIDATES).find(
            {"status": FaqCandidateStatus.REJECTED.value,
             "reviewed_at": {"$gte": since}}, {"cluster_key": 1})
        rows = await cursor.to_list(length=None)
        return {str(r["cluster_key"]) for r in rows}

    async def _audit_mine(self, actor: str, result: MineResult, days: int,
                          freq_min: int, sim_min: float, seed: int) -> None:
        await audit_service.record(
            action="faq.mine", actor=actor, actor_name=actor, target_type="faq",
            target_id="mine", target_name="FAQ 挖掘",
            after={"window_start": result.window_start, "window_end": result.window_end,
                   "window_days": days, "freq_threshold": freq_min,
                   "sim_threshold": sim_min, "seed": seed,
                   "scanned_logs": result.scanned_logs, "clusters": result.clusters,
                   "created": result.candidates_created,
                   "updated": result.candidates_updated,
                   "suppressed": result.candidates_skipped_suppressed,
                   "degraded": result.degraded},
            outcome="success")

    # ================================================================== 候选
    async def list_candidates(self, *, status: str | None = None,
                              min_frequency: int | None = None, keyword: str = "",
                              page: int = 1, page_size: int = 20) -> dict[str, Any]:
        """候选列表（原型 `05` 的 6 列）。"""
        _assert_page(page, page_size)
        if status and status not in {s.value for s in FaqCandidateStatus}:
            raise BizError(Err.FAQ_QUERY_INVALID, f"status 取值非法：{status}")
        rows, total = await faq_repo.list_candidates(
            status=status, min_frequency=min_frequency, keyword=keyword, page=page,
            page_size=page_size)
        titles = await _doc_titles([d for r in rows for d in (r.get("related_docs") or [])])
        window = await self._window_hint()
        return {"items": [_candidate_json(r, titles) for r in rows], "total": total,
                "page": page, "page_size": page_size, "window": window}

    async def _window_hint(self) -> dict[str, Any]:
        """窗口与阈值提示（前端表头「近 30 天 · 频次阈值 20」）。"""
        now = _now_ms()
        days = config_service.faq_mine_window_days
        return {"window_start": now - days * 86400_000, "window_end": now,
                "freq_threshold": config_service.faq_mine_freq_threshold,
                "sim_threshold": config_service.faq_mine_sim_threshold}

    async def approve(self, *, candidate_id: str, question: str | None = None,
                      answer: str | None = None,
                      aliases: Sequence[str] | None = None,
                      category_id: str | None = None,
                      related_doc_ids: Sequence[str] | None = None,
                      review_note: str = "", actor: str = "system"
                      ) -> dict[str, Any]:
        """审核通过并发布（Spec §3.2）。顺序与失败处置逐条照 Spec。"""
        candidate = await faq_repo.get_candidate(candidate_id)
        if candidate is None:
            raise BizError(Err.FAQ_CANDIDATE_NOT_FOUND, f"候选不存在：{candidate_id}")
        if candidate.get("status") != FaqCandidateStatus.PENDING.value:
            raise BizError(Err.FAQ_CANDIDATE_REVIEWED,
                           f"该候选已审核（{candidate.get('status')}）")

        final_question = (question or candidate.get("representative_question") or "").strip()
        final_answer = (answer if answer is not None
                        else candidate.get("draft_answer") or "").strip()
        _assert_question(final_question)
        _assert_answer(final_answer)
        final_aliases = faq_repo.clean_aliases(aliases, final_question)
        docs = list(related_doc_ids if related_doc_ids is not None
                    else candidate.get("related_docs") or [])
        _assert_sources(docs, candidate)

        # R-06：非空时每一篇都必须存在且未软删除（经 03 的只读接口）
        if docs:
            for doc_id in docs:
                doc = await doc_service.get(doc_id)
                if doc is None or doc.get("deleted_at") is not None:
                    raise BizError(Err.FAQ_SOURCE_INVALID,
                                   f"关联知识单元不存在或已删除：{doc_id}")

        # R-08：问法唯一（先查一次给出友好错误，再靠唯一索引兜并发）
        existing = await faq_repo.find_faq_by_question(final_question)
        if existing is not None:
            raise BizError(Err.FAQ_QUESTION_TAKEN,
                           f"标准问法已存在（{existing['_id']}）")

        # R-09：向量化（失败**不落库** —— 不允许出现无向量的 FAQ）
        try:
            vector = await asyncio.to_thread(
                embedding_service.embed_one,
                _embed_text(final_question, final_aliases))
        except EmbeddingUnavailable as exc:
            raise BizError(Err.FAQ_EMBEDDING_FAILED, f"向量化失败：{exc}") from exc

        now = _now_ms()
        seq = await faq_repo.next_faq_seq()
        faq_id = faq_repo.next_id(faq_repo.FAQ_ID_PREFIX, seq)
        # R-10/R-11：缓存准入（非全局可见 → 仍发布但 enabled=false）
        inject, reason = await self._admission(docs)
        doc = {
            "_id": faq_id, "question": final_question,
            "question_norm": faq_repo.normalize_question(final_question),
            "answer": final_answer, "aliases": final_aliases,
            "category_id": category_id or candidate.get("category_id"),
            "related_doc_ids": docs, "enabled": inject,
            "hit_count": 0, "published_by": actor, "published_at": now,
            "embedding": [float(x) for x in vector],
            "updated_by": actor, "updated_at": now,
        }
        try:
            await faq_repo.insert_faq(doc)
        except Exception as exc:                            # noqa: BLE001
            if faq_repo.is_duplicate_key_error(exc):
                raise BizError(Err.FAQ_QUESTION_TAKEN,
                               "标准问法已存在（并发发布）") from exc
            raise
        # R-12：先写 faqs，再更新候选，最后注入缓存；任一步失败不回滚已发布的 FAQ
        await faq_repo.mark_candidate_reviewed(
            candidate_id, status=FaqCandidateStatus.APPROVED.value, actor=actor,
            note=review_note, ts_ms=now, faq_id=faq_id)
        if inject:
            faq_cache.upsert(_entry_of(doc))
        if reason:
            logger.warning("FAQ %s 已发布但未进缓存：%s", faq_id, reason)
        await self._audit("faq.publish", actor, faq_id, final_question,
                          before={"candidate_id": candidate_id,
                                  "representative_question":
                                      candidate.get("representative_question"),
                                  "draft_answer": candidate.get("draft_answer")},
                          after={"question": final_question,
                                 "answer_brief": final_answer[:80],
                                 "enabled": inject, "related_doc_ids": docs},
                          reason=review_note or "审核通过并发布")
        return {"candidate_id": candidate_id, "status": FaqCandidateStatus.APPROVED.value,
                "faq_id": faq_id, "enabled": inject,
                "cache_size": faq_cache.size, "cache_injected": inject,
                "message": reason or "已发布并注入缓存"}

    async def _admission(self, doc_ids: Sequence[str]) -> tuple[bool, str]:
        """缓存准入：所有关联文档都必须**全局可见**（§1.5 / AC-07-15/16）。

        判定**必须经 05**（ER-03）：07 不自己读 `kb_permissions`。
        判定服务异常时 `check_visibility` 内部 fail-closed 返回全 False，
        于是这里得到"非全局"——正是 ER-04 要求的处置。
        """
        if not doc_ids:
            # 无关联文档：允许发布但不进缓存（没有来源的答案不该被直出）
            return False, "该 FAQ 没有关联知识单元，已发布但不进入缓存"
        visibility = await permission_service.check_visibility(list(doc_ids))
        blocked = [d for d in doc_ids if not visibility.get(d)]
        if blocked:
            return False, ("关联知识单元并非全局可见（"
                           f"{'/'.join(blocked[:3])}），已发布但不进入缓存")
        return True, ""

    async def reject(self, *, candidate_id: str, review_note: str,
                     convert_to_gap: bool = False, actor: str) -> dict[str, Any]:
        """驳回（Spec §3.3）：备注必填 ≥5 字；可选转建文档（**投递给 08**）。"""
        candidate = await faq_repo.get_candidate(candidate_id)
        if candidate is None:
            raise BizError(Err.FAQ_CANDIDATE_NOT_FOUND, f"候选不存在：{candidate_id}")
        if candidate.get("status") != FaqCandidateStatus.PENDING.value:
            raise BizError(Err.FAQ_CANDIDATE_REVIEWED,
                           f"该候选已审核（{candidate.get('status')}）")
        note = (review_note or "").strip()
        if len(note) < 5:
            raise BizError(Err.FAQ_NOTE_REQUIRED, "驳回备注至少 5 个字")

        forwarded = False
        if convert_to_gap:
            forwarded = await self._forward_to_gap(candidate, actor)
        await faq_repo.mark_candidate_reviewed(
            candidate_id, status=FaqCandidateStatus.REJECTED.value, actor=actor,
            note=note, ts_ms=_now_ms())
        await self._audit("faq.reject", actor, candidate_id,
                          str(candidate.get("representative_question") or ""),
                          after={"review_note": note,
                                 "convert_to_gap": bool(convert_to_gap),
                                 "gap_forwarded": forwarded},
                          reason=note)
        return {"candidate_id": candidate_id,
                "status": FaqCandidateStatus.REJECTED.value,
                "gap_forwarded": forwarded}

    async def _forward_to_gap(self, candidate: Mapping[str, Any], actor: str) -> bool:
        """把"无来源的簇"投递给 **08 的知识缺口服务**（ER-02：07 不写 E19）。

        08 尚未落地时投递必然失败 —— Spec §7.1 已经把这种情况的处置写死了：
        **候选仍为 `rejected`，只返回 `gap_forwarded=false`**，前端提示"可稍后重试"。
        所以这里不写任何兜底数据，只如实回报失败。
        """
        try:
            from app.services.gap_service import gap_service
        except ImportError:
            logger.error("转建失败：08 的 GapService 尚未就绪（候选仍为 rejected，"
                         "可稍后重试）candidate=%s", candidate.get("_id"))
            return False
        try:
            await gap_service.upsert_from_cluster(
                representative_question=str(
                    candidate.get("representative_question") or ""),
                questions=list(candidate.get("questions") or []),
                frequency=int(candidate.get("frequency") or 0),
                actor=actor)
            return True
        except Exception as exc:                            # noqa: BLE001
            logger.error("转建缺口失败（候选仍为 rejected）：%s", exc)
            return False

    # ================================================================== 已发布 FAQ
    async def list_published(self, *, keyword: str = "", enabled: bool | None = None,
                             category_id: str | None = None, page: int = 1,
                             page_size: int = 20) -> dict[str, Any]:
        """已发布列表（原型 FAQ 表 6 列；**不返回 embedding**，AC-07-32）。"""
        _assert_page(page, page_size)
        rows, total = await faq_repo.list_faqs(keyword=keyword, enabled=enabled,
                                              category_id=category_id, page=page,
                                              page_size=page_size)
        titles = await _doc_titles([d for r in rows
                                    for d in (r.get("related_doc_ids") or [])])
        enabled_count = await faq_repo.count_faqs(enabled=True)
        if faq_cache.size != enabled_count:
            # AC-07-14 的自检：条数不等说明缓存与真源分叉了（`FAQ-5002`）
            logger.warning("缓存与 faqs 不一致（code=%s）：cache=%d enabled=%d",
                           Err.FAQ_CACHE_INCONSISTENT.code, faq_cache.size,
                           enabled_count)
        return {"items": [_faq_json(r, titles) for r in rows], "total": total,
                "enabled_count": enabled_count, "cache_size": faq_cache.size,
                "page": page, "page_size": page_size}

    async def update_faq(self, *, faq_id: str, question: str | None = None,
                         answer: str | None = None,
                         aliases: Sequence[str] | None = None,
                         category_id: str | None = None,
                         related_doc_ids: Sequence[str] | None = None,
                         actor: str = "system") -> dict[str, Any]:
        """编辑已发布 FAQ（Spec §3.6）。改问法/别名 → **必须重新向量化**。"""
        faq = await faq_repo.get_faq(faq_id)
        if faq is None:
            raise BizError(Err.FAQ_NOT_FOUND, f"FAQ 不存在：{faq_id}")
        changed: dict[str, Any] = {}
        fields: dict[str, Any] = {"updated_by": actor, "updated_at": _now_ms()}

        if question is not None:
            text = question.strip()
            _assert_question(text)
            if faq_repo.normalize_question(text) != faq.get("question_norm"):
                other = await faq_repo.find_faq_by_question(text)
                if other is not None and other["_id"] != faq_id:
                    raise BizError(Err.FAQ_QUESTION_TAKEN,
                                   f"标准问法已存在（{other['_id']}）")
            fields["question"] = text
            fields["question_norm"] = faq_repo.normalize_question(text)
            changed["question"] = text
        if answer is not None:
            text = answer.strip()
            _assert_answer(text)
            fields["answer"] = text
            changed["answer"] = text
        if aliases is not None:
            base = fields.get("question") or faq.get("question") or ""
            fields["aliases"] = faq_repo.clean_aliases(aliases, base)
            changed["aliases"] = fields["aliases"]
        if category_id is not None:
            fields["category_id"] = category_id or None
            changed["category_id"] = fields["category_id"]
        if related_doc_ids is not None:
            fields["related_doc_ids"] = list(related_doc_ids)
            changed["related_doc_ids"] = fields["related_doc_ids"]

        # R-04：问法或别名变了就必须重新向量化（否则"答案对不上问题"）
        if "question" in changed or "aliases" in changed:
            text = _embed_text(fields.get("question") or faq["question"],
                               fields.get("aliases") or faq.get("aliases") or [])
            try:
                vector = await asyncio.to_thread(embedding_service.embed_one, text)
            except EmbeddingUnavailable as exc:
                raise BizError(Err.FAQ_EMBEDDING_FAILED,
                               f"向量化失败：{exc}") from exc
            fields["embedding"] = [float(x) for x in vector]

        # R-05：改了关联文档要重新做准入校验（可能从"全局"变成"受限"）
        reinjected = False
        if "related_doc_ids" in changed:
            inject, reason = await self._admission(fields["related_doc_ids"])
            if not inject and faq.get("enabled"):
                fields["enabled"] = False
                logger.warning("FAQ %s 因关联文档不再全局可见而自动停用：%s",
                               faq_id, reason)

        try:
            await faq_repo.update_faq(faq_id, fields)
        except Exception as exc:                            # noqa: BLE001
            if faq_repo.is_duplicate_key_error(exc):
                raise BizError(Err.FAQ_QUESTION_TAKEN, "标准问法已存在") from exc
            raise
        updated = await faq_repo.get_faq(faq_id) or {}
        if updated.get("enabled"):
            faq_cache.upsert(_entry_of(updated))
            reinjected = True
        else:
            faq_cache.remove(faq_id)
        await self._audit("faq.update", actor, faq_id,
                          str(updated.get("question") or ""),
                          before={"question": faq.get("question"),
                                  "answer_brief": str(faq.get("answer") or "")[:80],
                                  "enabled": faq.get("enabled")},
                          after={"changed": list(changed.keys()),
                                 "enabled": updated.get("enabled")},
                          reason="编辑已发布 FAQ")
        return {"faq_id": faq_id, "changed": changed,
                "cache_reinjected": reinjected,
                "enabled": bool(updated.get("enabled"))}

    async def toggle(self, *, faq_id: str, enabled: bool, actor: str,
                     reason: str = "") -> dict[str, Any]:
        """缓存生效开关（Spec §3.7）。

        **开→关**：立刻从缓存移除（AC-07-13：同一问题回到 RAG 路径）。
        **关→开**：必须重新做准入校验 —— 否则"停用期间文档被改成部门受限"
        会被这次启用悄悄绕过（`FAQ-2001`）。
        """
        faq = await faq_repo.get_faq(faq_id)
        if faq is None:
            raise BizError(Err.FAQ_NOT_FOUND, f"FAQ 不存在：{faq_id}")
        note = (reason or "").strip()
        if enabled:
            inject, why = await self._admission(faq.get("related_doc_ids") or [])
            if not inject:
                raise BizError(Err.FAQ_NOT_GLOBAL, why or "关联知识单元非全局可见")
        if not enabled and note and len(note) < 5:
            raise BizError(Err.FAQ_NOTE_REQUIRED, "停用原因至少 5 个字")
        await faq_repo.update_faq(faq_id, {"enabled": bool(enabled),
                                          "updated_by": actor,
                                          "updated_at": _now_ms()})
        if enabled:
            faq_cache.upsert(_entry_of({**faq, "enabled": True}))
        else:
            faq_cache.remove(faq_id)
        await self._audit("faq.toggle", actor, faq_id, str(faq.get("question") or ""),
                          before={"enabled": faq.get("enabled")},
                          after={"enabled": bool(enabled)}, reason=note or "缓存生效开关")
        return {"faq_id": faq_id, "enabled": bool(enabled),
                "cache_size": faq_cache.size}

    async def delete(self, *, faq_id: str, actor: str) -> dict[str, Any]:
        """删除已发布 FAQ（**物理删除**，Spec §2.2 的说明）。"""
        faq = await faq_repo.get_faq(faq_id)
        if faq is None:
            raise BizError(Err.FAQ_NOT_FOUND, f"FAQ 不存在：{faq_id}")
        await faq_repo.delete_faq(faq_id)
        faq_cache.remove(faq_id)
        await self._audit("faq.delete", actor, faq_id, str(faq.get("question") or ""),
                          before={"question": faq.get("question"),
                                  "enabled": faq.get("enabled")},
                          reason="删除已发布 FAQ（物理删除）")
        return {"faq_id": faq_id, "deleted": True, "cache_size": faq_cache.size}

    # ================================================================== 缓存运维
    async def rebuild_cache(self, *, actor: str) -> dict[str, Any]:
        """全量重建缓存（Spec §3.8 / §4.4）。

        三个必须做对的地方：
        ① **原子替换**：先在旁边构建好条目列表再整体换上，中途没有空缓存窗口（AC-07-25）；
        ② **失败保留旧缓存**：读 `faqs` 失败绝不清空（`FAQ-4003`）；
        ③ **并发保护**：`_rebuilding` 为真时返回 `FAQ-3008`，不排队。
        """
        if not faq_cache.begin_rebuild():
            raise BizError(Err.FAQ_CACHE_REBUILDING, "缓存正在重建中，请稍后再试")
        started = time.monotonic()
        try:
            rows = await faq_repo.list_enabled_faqs()
            entries = [_entry_of(r) for r in rows]
            count = faq_cache.replace_all(entries)
            try:
                await faq_repo.save_cache_copy([e.as_row() for e in entries],
                                               dim=settings.embedding_dim,
                                               model="BGE-M3")
            except Exception as exc:                        # noqa: BLE001
                # 副本只是兜底，写失败不该让重建失败（真源是 faqs）
                logger.warning("缓存副本写入失败（不影响内存缓存）：%s", exc)
        except BizError:
            raise
        except Exception as exc:                            # noqa: BLE001
            logger.exception("缓存重建失败，保留旧缓存继续服务")
            raise BizError(Err.FAQ_CACHE_REBUILD_FAILED,
                           f"缓存重建失败：{exc}") from exc
        finally:
            faq_cache.end_rebuild()
        elapsed = int((time.monotonic() - started) * 1000)
        await self._audit("faq.cache.rebuild", actor, "cache", "FAQ 缓存",
                          after={"cache_size": count, "elapsed_ms": elapsed},
                          reason="手动重建缓存")
        logger.info("FAQ 缓存重建完成：%d 条（%dms）", count, elapsed)
        return {"cache_size": count, "elapsed_ms": elapsed,
                "generation": faq_cache.generation}

    async def cache_status(self) -> dict[str, Any]:
        """缓存状态（运维 / 演示用；`cache_size != enabled_count` 时附告警）。"""
        counts = await faq_repo.status_counts()
        consistent = faq_cache.size == counts["enabled"]
        return {"cache_size": faq_cache.size,
                "enabled_count": counts["enabled"],
                "faqs_total": counts["faqs"],
                "candidates_total": counts["candidates"],
                "candidates_pending": await faq_repo.count_candidates(
                    FaqCandidateStatus.PENDING.value),
                "generation": faq_cache.generation,
                "dim": settings.embedding_dim,
                "threshold": config_service.faq_cache_sim_threshold,
                "cache_enabled": config_service.faq_cache_enabled,
                "pending_hits": faq_cache.pending_hit_counts(),
                "match_p95_ms": faq_cache.match_p95_ms(),
                "mining": self._mining,
                "consistent": consistent,
                "warning": "" if consistent else
                f"{Err.FAQ_CACHE_INCONSISTENT.code} 缓存条数与已生效 FAQ 不一致，"
                "可执行重建"}

    async def flush_hit_counts(self) -> int:
        """把内存里的命中计数批量落库（scheduler 周期调用，AC-07-17）。"""
        drained = faq_cache.drain_hit_counts()
        if not drained:
            return 0
        try:
            return await faq_repo.inc_hit_counts(drained)
        except Exception as exc:                            # noqa: BLE001
            logger.warning("命中计数落库失败（已丢弃，不影响命中统计的趋势）：%s", exc)
            return 0

    async def startup(self) -> dict[str, Any]:
        """启动时全量重建缓存（AC-07-14）+ 多 worker 自检（`FAQ-2002`）。"""
        try:
            result = await self.rebuild_cache(actor="system")
        except Exception as exc:                            # noqa: BLE001
            # 启动期重建失败**不阻止启动**：问答仍可用（只是没有 FAQ 直出）
            logger.error("启动期 FAQ 缓存重建失败（服务继续启动）：%s", exc)
            return {"cache_size": faq_cache.size, "rebuilt": False}
        return {"cache_size": result["cache_size"], "rebuilt": True}

    # ================================================================== 审计
    async def _audit(self, action: str, actor: str, target_id: str,
                     target_name: str, *, before: Mapping[str, Any] | None = None,
                     after: Mapping[str, Any] | None = None,
                     reason: str = "") -> None:
        """写审计（ER-05）。**审计失败不阻断业务**（契约保证永不抛异常）。

        ⚠️ 快照里**不能出现 `embedding`**（AC-07-27）：1024 个浮点数会让审计体积
        膨胀几十倍，而它对"谁改了什么"没有任何信息量。
        """
        await audit_service.record(
            action=action, actor=actor, actor_name=actor, target_type="faq",
            target_id=target_id, target_name=target_name,
            before=_strip_embedding(before), after=_strip_embedding(after),
            reason=reason or None, outcome="success")


# ---------------------------------------------------------------------- 纯函数
def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度（零向量返回 0，**不返回 NaN**）。

    返回 NaN 的后果很隐蔽：`NaN >= 0.85` 是 `False`（看起来"没聚上"），
    但一旦有人写成 `max(...)` 比较，NaN 会污染整个排序。
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


def _mean_vector(vectors: Sequence[Sequence[float]]) -> list[float]:
    """簇心 = 成员的逐维均值（**不归一化**：余弦自带尺度无关性）。"""
    if not vectors:
        return []
    dim = len(vectors[0])
    total = [0.0] * dim
    for vector in vectors:
        for index, value in enumerate(vector[:dim]):
            total[index] += float(value)
    count = len(vectors)
    return [value / count for value in total]


def _finalize(cluster: Cluster) -> None:
    """算代表问法、频次、关联文档（Spec §2.1 的确定性口径）。"""
    counts: dict[str, int] = {}
    earliest: dict[str, int] = {}
    for item in cluster.questions:
        question = item["question"]
        counts[question] = counts.get(question, 0) + 1
        asked = int(item.get("asked_at") or 0)
        if question not in earliest or asked < earliest[question]:
            earliest[question] = asked
    if not counts:
        cluster.representative = ""
        cluster.frequency = 0
        return
    # 出现次数最多；并列取 `asked_at` 最早（保证确定性 —— 否则同一批日志
    # 两次运行可能选出不同的代表问法，进而得到不同的 cluster_key）
    cluster.representative = sorted(
        counts, key=lambda q: (-counts[q], earliest.get(q, 0), q))[0]
    cluster.frequency = len(cluster.questions)
    docs: list[str] = []
    for item in cluster.questions:
        for doc_id in item.get("doc_ids") or []:
            if doc_id not in docs:
                docs.append(doc_id)
    cluster.related_docs = docs


def _confidence(vectors: Sequence[Sequence[float]], frequency: int,
                freq_threshold: int) -> float:
    """置信度 = `mean(簇内两两余弦) × min(1, frequency / (2 × threshold))`（保留 2 位）。

    余弦与聚类用**同一套口径**：换一套度量会让"聚成一簇"与"置信度高"互相矛盾，
    而审核人正是靠这个数字决定"值不值得看一眼"。
    """
    pairs = 0
    total = 0.0
    for i in range(len(vectors)):
        for j in range(i + 1, len(vectors)):
            total += _cosine(vectors[i], vectors[j])
            pairs += 1
    mean_sim = total / pairs if pairs else 1.0
    weight = min(1.0, frequency / (2 * max(1, freq_threshold)))
    return round(mean_sim * weight, 2)


def _doc_ids_of(row: Mapping[str, Any]) -> list[str]:
    """从日志反推关联文档（**只用放行的切片**：被拦的不算来源）。

    ★ 为什么不能"直接读 `allowed_chunks` 当文档号"：那里存的是**切片主键**（int64），
    而关联文档要的是 `doc_id`。两者的桥梁只有 `recalled_chunks` 里的
    `{chunk_id, doc_id}` 快照，所以这里**先建映射、再按放行集合过滤**。

    ★ 为什么要丢掉假值：`str(None)` 会产出 `"None"` 这个**假文档号**。
    它一旦落进候选的 `related_docs`，审核通过就会永久卡在
    `FAQ-3004 关联知识单元不存在或已删除：None`（谁也没法发布这条 FAQ）。
    这种"上游给空值、下游拼字符串"的链式故障不会报错，只会让人以为
    "这条候选有问题"，所以在入口处就把它掐掉。
    """
    mapping: dict[Any, str] = {}
    for chunk in row.get("recalled_chunks") or []:
        if isinstance(chunk, Mapping):
            doc_id = _clean_doc_id(chunk.get("doc_id"))
            key = chunk.get("chunk_id")
            if doc_id and key is not None:
                mapping[key] = doc_id
    out: list[str] = []
    for chunk in row.get("allowed_chunks") or []:
        if isinstance(chunk, Mapping):
            doc_id = _clean_doc_id(chunk.get("doc_id"))
        else:
            # 切片主键 → 文档号（映射不到就**不猜**；只有本身就是 `DOCxxxx`
            # 形态的值才当作文档号接受，绝不能把切片号当文档号）
            doc_id = mapping.get(chunk) or (
                _clean_doc_id(chunk) if DOC_ID_RE.match(_clean_doc_id(chunk)) else "")
        if doc_id and doc_id not in out:
            out.append(doc_id)
    return out


def _clean_doc_id(value: Any) -> str:
    """把 `None` / `"None"` / `"null"` / 空串一律归为 `""`（= 无效文档号）。"""
    text = "" if value is None else str(value).strip()
    return "" if text.lower() in ("", "none", "null") else text


def _embed_text(question: str, aliases: Sequence[str]) -> str:
    """向量化输入 = 标准问法 + 同义问法拼接（Spec §2.3：单条向量）。"""
    parts = [question] + [a for a in aliases if a]
    return " ".join(p.strip() for p in parts if p and p.strip())


def _entry_of(row: Mapping[str, Any]) -> FaqEntry:
    """E17 记录 → 缓存条目。"""
    return FaqEntry(faq_id=str(row["_id"]), question=str(row.get("question") or ""),
                    answer=str(row.get("answer") or ""),
                    vector=row.get("embedding") or [],
                    aliases=list(row.get("aliases") or []),
                    published_at=int(row.get("published_at") or 0))


def _assert_question(question: str) -> None:
    if not (2 <= len(question.strip()) <= 200):
        raise BizError(Err.FAQ_QUESTION_INVALID, "标准问法需 2~200 字")


def _assert_answer(answer: str) -> None:
    if not (1 <= len(answer.strip()) <= 2000):
        raise BizError(Err.FAQ_ANSWER_INVALID, "标准答案需 1~2000 字")


def _assert_sources(docs: Sequence[str], candidate: Mapping[str, Any]) -> None:
    """R-07：无来源**且**无草案 → 拒绝发布（原型 `candTblR1`：该走 08 转建）。"""
    draft = str(candidate.get("draft_answer") or "").strip()
    if not docs and not draft:
        raise BizError(
            Err.FAQ_SOURCE_INVALID,
            "该簇没有关联知识单元、也没有参考答案：请改为「驳回 / 转建文档」")


def _assert_page(page: int, page_size: int) -> None:
    if page < 1 or not 1 <= page_size <= 200:
        raise BizError(Err.FAQ_QUERY_INVALID,
                       f"分页参数非法：page={page} page_size={page_size}（上限 200）")


async def _doc_titles(doc_ids: Sequence[str]) -> dict[str, str]:
    """批量取文档标题（展示用，**不落库** —— 文档改名后候选里的标题不该陈旧）。"""
    titles: dict[str, str] = {}
    for doc_id in dict.fromkeys(d for d in doc_ids if d):
        doc = await doc_service.get(doc_id)
        titles[doc_id] = str((doc or {}).get("title") or doc_id)
    return titles


def _candidate_json(row: Mapping[str, Any], titles: Mapping[str, str]
                    ) -> dict[str, Any]:
    """候选卡片（原型 `05` 的 6 列：聚类问题簇 / 聚合频次 / 关联知识单元 /
    推荐标准答案 / 置信度 / 操作）。"""
    related = list(row.get("related_docs") or [])
    return {
        "candidate_id": str(row["_id"]), "cluster_key": row.get("cluster_key"),
        "questions": list(row.get("questions") or []),
        "representative_question": row.get("representative_question"),
        "frequency": int(row.get("frequency") or 0),
        "related_docs": [{"doc_id": d, "title": titles.get(d, d)} for d in related],
        "draft_answer": row.get("draft_answer") or "",
        "confidence": float(row.get("confidence") or 0.0),
        "status": row.get("status"),
        "status_text": FaqCandidateStatus(str(row.get("status"))).label
        if row.get("status") in {s.value for s in FaqCandidateStatus} else "",
        "reviewed_by": row.get("reviewed_by"), "reviewed_at": row.get("reviewed_at"),
        "review_note": row.get("review_note"), "faq_id": row.get("faq_id"),
        "first_seen_at": row.get("first_seen_at"),
        "last_seen_at": row.get("last_seen_at"),
        "window_start": row.get("window_start"), "window_end": row.get("window_end"),
    }


def _faq_json(row: Mapping[str, Any], titles: Mapping[str, str]) -> dict[str, Any]:
    """已发布 FAQ 列表行（**不含 `embedding`**）。"""
    answer = str(row.get("answer") or "")
    brief = answer if len(answer) <= 40 else answer[:40] + "…"
    related = list(row.get("related_doc_ids") or [])
    return {
        "faq_id": str(row["_id"]), "question": row.get("question"),
        "answer_brief": brief,
        "related_doc_ids": related,
        "related_doc_titles": [titles.get(d, d) for d in related],
        "category_id": row.get("category_id"),
        "hit_count": int(row.get("hit_count") or 0),
        "enabled": bool(row.get("enabled")),
        "enabled_text": "已生效" if row.get("enabled") else "已停用",
        "aliases": list(row.get("aliases") or []),
        "published_by": row.get("published_by"),
        "published_at": row.get("published_at"), "updated_at": row.get("updated_at"),
    }


def _strip_embedding(snapshot: Mapping[str, Any] | None
                     ) -> dict[str, Any] | None:
    """审计快照去掉 `embedding`（AC-07-27）。"""
    if not snapshot:
        return None if snapshot is None else {}
    return {k: v for k, v in snapshot.items() if k != "embedding"}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _start_of_day(ts_ms: int) -> int:
    """把毫秒时间戳对齐到当天 00:00（本地时区）。

    用途：让"同一个挖掘窗口"有一个**稳定**的标识（见 `mine()` 里的说明）。
    """
    import datetime as _dt

    moment = _dt.datetime.fromtimestamp(ts_ms / 1000)
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


faq_service = FaqService()

__all__ = ["FaqService", "faq_service", "Cluster", "MineResult", "MIN_QUESTION_CHARS"]
