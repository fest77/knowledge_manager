# -*- coding: utf-8 -*-
"""模块 08 的服务层：**知识缺口**（识别 → 合并计数 → 一键转建 → 忽略 → 导出）。

```
qa_logs（06 只读）                        ← 唯一数据源（ER-06：08 不写）
   │  ① 三种判定口径 OR：max_score < 阈值 / recalled_chunks 空 / no_knowledge
   │  ② 排除假缺口：faq_hit=true、degraded=true、no_knowledge 且 denied 非空
   ▼
按 normalized_key（部门 + 归一化问法）合并计数       ← 唯一索引兜底
   │  ③ 窗口内**重算覆盖**（$set 而非 $inc）——幂等性的第一重保证
   ▼
E19 knowledge_gaps（open）
   │  ④ 一键转建：03 建占位 → 04 建导入任务 → 回写 converted（跨模块链路）
   ▼
kb_documents（03 写） + kb_import_tasks（04 写）
```

## 四条本模块独有的约束

| # | 约束 | 为什么 |
|---|---|---|
| 1 | **08 不直连写 `kb_documents` / `kb_import_tasks`** | 写入者是 03 / 04（ER-02 / AC-08-12），必须经其 Service |
| 2 | **`qa_logs` 只读**（ER-06 / AC-08-15） | 06 独占写；08 只 `find` / `aggregate` / `count` |
| 3 | **人工状态优先于机器重算**（Spec §2.4） | 被忽略的缺口不能被聚合改回 `open`；转建留痕不能被抹掉 |
| 4 | **清理只删 `open` 且频次归零的**（AC-08-22） | `converted` / `ignored` 是人工决策痕迹，永久保留 |

## 识别的三种口径为什么是 OR

| 口径 | 场景 |
|---|---|
| `max_score < gap.score_threshold` | **召回了但不够像** —— 最典型的缺口（"库里可能有，但没答对"） |
| `recalled_chunks == []` | 压根没召回（`max_score` 记 `0.0`） |
| `answer_source == no_knowledge` | 链路判定为无资料（含降级路径） |

三者是**同一个现象的不同侧面**，用 OR 才不会漏。而"假缺口"必须排除：
`faq_hit=true`（FAQ 直出，不需要文档）、`degraded=true`（这轮鉴权/检索降级了）、
`no_knowledge` 且 `denied_chunks` 非空（**有资料但无权** —— 那是权限问题，不是知识缺口）。
"""
from __future__ import annotations

import asyncio
import csv
import io
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from app.core.enums import GapStatus
from app.core.errors import BizError, Err
from app.core.logging import logger
from app.repositories import gap_repo
from app.services.audit_service import audit_service
from app.services.config_service import config_service
from app.services.doc_service import doc_service
from app.services.gap_normalizer import build_normalized_key, normalize_question

# 样本保留条数（Spec §2.1：最多 5 条，保留**最近**的）
MAX_SAMPLES = 5
# 导出 CSV 的列名（AC-08-23/24：与页面表头一致）
EXPORT_HEADERS = ("未命中提问", "提问部门", "近期频次", "最高相似度",
                  "建议创建分类", "状态", "首次出现", "最近出现")


@dataclass(slots=True)
class AggregateResult:
    """一轮聚合的结果（`POST /gaps/aggregate` 的出参）。"""

    window_start: int
    window_end: int
    scanned_logs: int = 0
    identified: int = 0
    written: int = 0
    removed: int = 0
    elapsed_ms: int = 0
    degraded: bool = False
    notice: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"window_start": self.window_start, "window_end": self.window_end,
                "scanned_logs": self.scanned_logs, "identified": self.identified,
                "written": self.written, "removed": self.removed,
                "elapsed_ms": self.elapsed_ms, "degraded": self.degraded,
                "notice": self.notice}


@dataclass(slots=True)
class _Bucket:
    """一个 `normalized_key` 的聚合中间态。"""

    dept_id: str | None
    normalized_text: str
    questions: list[tuple[int, str]] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)
    first_seen_at: int = 0
    last_seen_at: int = 0
    log_ids: list[tuple[int, str]] = field(default_factory=list)
    allowed_doc_ids: list[str] = field(default_factory=list)
    top_recalled_doc_id: str | None = None
    top_score: float = -1.0


class GapService:
    """知识缺口服务（进程级单例）。"""

    def __init__(self) -> None:
        # 单飞锁（Spec §2.4 的第三重保证）：上一轮未结束则跳过本轮（AC-08-19）
        self._lock = asyncio.Lock()
        self._running = False

    @property
    def aggregating(self) -> bool:
        return self._running

    # ================================================================== 聚合
    async def aggregate(self, *, window_days: int | None = None,
                        actor: str = "scheduler") -> AggregateResult:
        """一轮聚合（幂等：`$set` 重算覆盖 + 唯一索引 + 单飞锁）。"""
        days = config_service.gap_window_days if window_days is None \
            else int(window_days)
        if not 1 <= days <= 90:
            raise BizError(Err.GAP_WINDOW_INVALID, f"window_days 需在 1~90：{days}")
        if self._lock.locked():
            # 手动触发撞上正在跑的定时任务 → GAP-3005（**跳过而不是排队**：
            # 两轮并发会互相覆盖，最终写进去的是哪一轮完全取决于时序）
            raise BizError(Err.GAP_AGGREGATING, "缺口聚合任务正在运行中，请稍后再试")

        async with self._lock:
            self._running = True
            started = time.monotonic()
            now = gap_repo.now_ms()
            # 窗口起点对齐到当天 00:00（与 07 同一理由：让"同一窗口"有稳定标识）
            window_start = _start_of_day(now - days * 86400_000)
            result = AggregateResult(window_start=window_start, window_end=now)
            try:
                logs = await self._read_logs(window_start, now)
                result.scanned_logs = len(logs)
                buckets = self._bucket(logs, window_start, now)
                result.identified = len(buckets)
                records = await self._build_records(buckets, now, result)
                result.written = await gap_repo.upsert_batch(records, ts_ms=now)
                result.removed = await gap_repo.remove_stale_open(
                    key_keep=list(buckets.keys()), ts_ms=now)
            except BizError:
                raise
            except Exception as exc:                        # noqa: BLE001
                logger.exception("缺口聚合失败（窗口 %s~%s）", window_start, now)
                raise BizError(Err.GAP_AGGREGATE_FAILED, f"聚合失败：{exc}") from exc
            finally:
                self._running = False
                result.elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.info("缺口聚合完成：扫描 %d 日志 → 识别 %d 缺口 → 写入 %d / 清理 %d",
                        result.scanned_logs, result.identified, result.written,
                        result.removed)
            return result

    async def _read_logs(self, window_start: int, window_end: int
                         ) -> list[dict[str, Any]]:
        """读窗口内日志（**只读** qa_logs，AC-08-15）。"""
        from app.infra.mongo import mongo
        from app.repositories import qa_repo

        try:
            cursor = mongo.collection(qa_repo.QA_LOGS).find(
                {"asked_at": {"$gte": window_start, "$lte": window_end}},
                {"question": 1, "asked_at": 1, "dept_id": 1, "max_score": 1,
                 "recalled_chunks": 1, "allowed_chunks": 1, "denied_chunks": 1,
                 "faq_hit": 1, "answer_source": 1, "degraded": 1})
            return await cursor.to_list(length=200_000)
        except Exception as exc:                            # noqa: BLE001
            # GAP-4003：日志不可读 → **本轮跳过**（清单仍返回已有数据）
            logger.error("问答日志不可读，本轮聚合跳过（code=%s）：%s",
                         Err.GAP_LOGS_UNAVAILABLE.code, exc)
            raise BizError(Err.GAP_LOGS_UNAVAILABLE,
                           f"问答日志不可读：{exc}") from exc

    def _is_gap(self, log: Mapping[str, Any]) -> bool:
        """缺口判定（三种口径 OR + 三类假缺口排除，见模块头）。"""
        if log.get("faq_hit"):
            return False        # FAQ 直出：本来就不需要文档
        if log.get("degraded"):
            return False        # 这轮鉴权/检索降级了，判定不可信
        question = str(log.get("question") or "").strip()
        if not question:
            return False
        recalled = log.get("recalled_chunks") or []
        denied = log.get("denied_chunks") or []
        source = str(log.get("answer_source") or "")
        if source == "no_knowledge" and denied:
            # ★ 有资料但无权查阅 —— 那是**权限问题**，不是知识缺口。
            # 把它记成缺口会让管理员去补一份"其实已经存在"的文档
            return False
        threshold = config_service.gap_score_threshold
        max_score = float(log.get("max_score") or 0.0)
        return (max_score < threshold) or (not recalled) or (source == "no_knowledge")

    def _bucket(self, logs: Sequence[Mapping[str, Any]], window_start: int,
                window_end: int) -> dict[str, _Bucket]:
        """按 `normalized_key` 合并（Spec §2.3 的部门 + 归一化文本）。"""
        buckets: dict[str, _Bucket] = {}
        for log in logs:
            if not self._is_gap(log):
                continue
            question = str(log.get("question") or "").strip()
            dept_id = log.get("dept_id") or None
            normalized_text = normalize_question(question)
            key = build_normalized_key(dept_id, normalized_text)
            bucket = buckets.get(key)
            if bucket is None:
                bucket = _Bucket(dept_id=dept_id, normalized_text=normalized_text)
                buckets[key] = bucket
            asked_at = int(log.get("asked_at") or 0)
            bucket.questions.append((asked_at, question))
            bucket.scores.append(float(log.get("max_score") or 0.0))
            bucket.log_ids.append((asked_at, str(log.get("_id") or "")))
            bucket.first_seen_at = (min(bucket.first_seen_at, asked_at)
                                    if bucket.first_seen_at else asked_at)
            bucket.last_seen_at = max(bucket.last_seen_at, asked_at)
            for chunk in log.get("allowed_chunks") or []:
                doc_id = _doc_id_of(chunk)
                if doc_id and doc_id not in bucket.allowed_doc_ids:
                    bucket.allowed_doc_ids.append(doc_id)
            for chunk in log.get("recalled_chunks") or []:
                score = float((chunk or {}).get("score") or 0.0) \
                    if isinstance(chunk, Mapping) else 0.0
                if score > bucket.top_score:
                    bucket.top_score = score
                    bucket.top_recalled_doc_id = _doc_id_of(chunk)
        return buckets

    async def _build_records(self, buckets: Mapping[str, _Bucket], now: int,
                             result: AggregateResult) -> list[dict[str, Any]]:
        """把簇写成 E19 记录（含建议分类推断，Spec §2.5）。"""
        records: list[dict[str, Any]] = []
        # ★ 编号必须**一次批量分配**（见 `gap_repo.next_gap_ids` 的说明）
        gap_ids = await gap_repo.next_gap_ids(len(buckets))
        for index, (key, bucket) in enumerate(buckets.items()):
            samples = [log_id for _, log_id in
                       sorted(bucket.log_ids, key=lambda x: x[0])[-MAX_SAMPLES:]]
            category_id, basis, degraded = await self._suggest_category(bucket)
            result.degraded = result.degraded or degraded
            if degraded and not result.notice:
                result.notice = "建议分类反查不可用，已按「暂无建议分类」处理"
            records.append({
                "_id": gap_ids[index],
                "normalized_key": key,
                "normalized_text": bucket.normalized_text,
                "dept_id": bucket.dept_id,
                # 众数原文（平局取最早）——保证清单标题稳定不跳动
                "question": _mode_question(bucket.questions),
                "frequency": len(bucket.questions),
                "max_score": round(max(bucket.scores) if bucket.scores else 0.0, 4),
                "suggested_category_id": category_id,
                "suggested_category_basis": basis,
                "sample_log_ids": samples,
                "first_seen_at": bucket.first_seen_at,
                "last_seen_at": bucket.last_seen_at,
            })
        return records

    async def _suggest_category(self, bucket: _Bucket
                                ) -> tuple[str | None, str, bool]:
        """建议分类推断（Spec §2.5 的四步 + 兜底链）。

        返回 `(category_id, basis, degraded)`；`basis` 会作为**响应字段**告诉前端
        这个建议"是算出来的、还是兜底的、还是依赖不可用"——
        没有它，管理员无法判断"建议为空"是因为没数据还是因为服务挂了。
        """
        try:
            from app.repositories import doc_repo

            doc_ids = list(bucket.allowed_doc_ids)
            basis = "allowed_chunks"
            if not doc_ids:
                if not bucket.top_recalled_doc_id:
                    return None, "none", False
                doc_ids = [bucket.top_recalled_doc_id]
                basis = "top_recalled"
            # ★ 一次 `$in`（ER-13）：逐 doc 查在 50 篇关联文档时就是 50 次往返
            rows = await doc_repo.find_by_ids(doc_ids)
            votes: dict[str, int] = {}
            for row in rows:
                category_id = row.get("category_id")
                if category_id:
                    votes[category_id] = votes.get(category_id, 0) + 1
            if not votes:
                return None, "none", False
            # 众数；平局取 doc 数最多者，仍平局取字典序最小（确定性）
            best = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            return best, basis, False
        except Exception as exc:                            # noqa: BLE001
            logger.warning("建议分类反查不可用（code=%s）：%s",
                           Err.GAP_CATEGORY_LOOKUP_FAILED.code, exc)
            return None, "unavailable", True

    # ================================================================== 清单
    async def list_gaps(self, *, status: str = "open", dept_id: str | None = None,
                        keyword: str = "", category_id: str | None = None,
                        min_frequency: int = 1, sort: str = "frequency_desc",
                        group_by: str = "none", page: int = 1,
                        page_size: int = 20) -> dict[str, Any]:
        """缺口清单（Spec §3.1）。**永远读快照，不触发实时聚合**（R-07）。"""
        _assert_query(status, sort, group_by, min_frequency, page, page_size)
        status_filter = None if status == "all" else status
        rows, total = await gap_repo.list_gaps(
            status=status_filter, dept_id=dept_id, keyword=keyword,
            category_id=category_id, min_frequency=min_frequency, sort=sort,
            page=page, page_size=page_size)
        items = await self._decorate(rows)
        if group_by == "question":
            items = _merge_by_question(items)
            total = len(items)
        summary = await self._summary()
        return {"items": items, "summary": summary, "total": total, "page": page,
                "page_size": page_size,
                "aggregated_at": await gap_repo.last_aggregated_at()}

    async def get_detail(self, gap_id: str) -> dict[str, Any]:
        """缺口详情（含代表性样本的原始问法与同义问法，Spec §3.2）。"""
        gap = await gap_repo.get_gap(gap_id)
        if gap is None:
            raise BizError(Err.GAP_NOT_FOUND, f"知识缺口不存在：{gap_id}")
        items = await self._decorate([gap])
        detail = items[0]
        samples = await self._samples(gap.get("sample_log_ids") or [])
        detail["samples"] = samples
        detail["synonym_questions"] = [
            s["question"] for s in samples if s["question"] != gap.get("question")]
        return detail

    async def _samples(self, log_ids: Sequence[str]) -> list[dict[str, Any]]:
        """展开样本日志（**只读** qa_logs）：原始问法 + 时间 + 相似度。"""
        if not log_ids:
            return []
        from app.infra.mongo import mongo
        from app.repositories import qa_repo

        cursor = mongo.collection(qa_repo.QA_LOGS).find(
            {"_id": {"$in": list(log_ids)}},
            {"question": 1, "asked_at": 1, "max_score": 1, "answer_source": 1,
             "dept_id": 1, "user_id": 1})
        rows = await cursor.to_list(length=len(log_ids))
        rows.sort(key=lambda r: r.get("asked_at") or 0)
        return [{"log_id": str(r["_id"]), "question": r.get("question"),
                 "asked_at": r.get("asked_at"), "max_score": r.get("max_score"),
                 "answer_source": r.get("answer_source"),
                 "dept_id": r.get("dept_id")} for r in rows]

    async def _decorate(self, rows: Sequence[Mapping[str, Any]]
                        ) -> list[dict[str, Any]]:
        """补齐展示字段：部门名（经 02）、建议分类路径（经 03）、`recurred`。"""
        dept_ids = [r.get("dept_id") for r in rows if r.get("dept_id")]
        dept_names = await _dept_names(dept_ids)
        category_ids = [r.get("suggested_category_id") for r in rows
                        if r.get("suggested_category_id")]
        paths = await _category_paths(category_ids)
        out: list[dict[str, Any]] = []
        for row in rows:
            dept_id = row.get("dept_id")
            category_id = row.get("suggested_category_id")
            converted_at = row.get("converted_at")
            basi = row.get("suggested_category_basis") or (
                "allowed_chunks" if category_id else "none")
            out.append({
                "gap_id": str(row["_id"]),
                "question": row.get("question"),
                "normalized_key": row.get("normalized_key"),
                "normalized_text": row.get("normalized_text"),
                "dept_id": dept_id,
                # R-03：部门名取不到时**回落为 dept_id**（降级不报错）
                "dept_name": dept_names.get(dept_id or "", dept_id or "未知部门"),
                "frequency": int(row.get("frequency") or 0),
                "max_score": float(row.get("max_score") or 0.0),
                "suggested_category_id": category_id,
                "suggested_category_path": paths.get(category_id or ""),
                "suggested_category_basis": basi,
                "sample_log_ids": list(row.get("sample_log_ids") or []),
                "first_seen_at": row.get("first_seen_at"),
                "last_seen_at": row.get("last_seen_at"),
                "status": row.get("status"),
                "status_text": _status_text(row.get("status")),
                "converted_doc_id": row.get("converted_doc_id"),
                "converted_at": converted_at, "converted_by": row.get("converted_by"),
                "ignored_at": row.get("ignored_at"), "ignored_by": row.get("ignored_by"),
                "ignore_reason": row.get("ignore_reason"),
                # R-05：转建**之后**又被提问 → 说明那份文档还没解决这个盲区
                "recurred": bool(row.get("status") == GapStatus.CONVERTED.value
                                 and converted_at
                                 and (row.get("last_seen_at") or 0) > converted_at),
                "last_aggregated_at": row.get("last_aggregated_at"),
            })
        return out

    async def _summary(self) -> dict[str, int]:
        """全量统计（**不受当前筛选影响**，R-06）。"""
        counts = await gap_repo.count_by_status()
        return {"open": counts.get(GapStatus.OPEN.value, 0),
                "converted": counts.get(GapStatus.CONVERTED.value, 0),
                "ignored": counts.get(GapStatus.IGNORED.value, 0),
                "total_frequency": await gap_repo.total_frequency()}

    # ================================================================== 转建
    async def convert(self, *, gap_id: str, title: str | None = None,
                      category_id: str | None = None, actor: str) -> dict[str, Any]:
        """一键转建文档（Spec §3.3）：**08 唯一的跨模块写操作**。"""
        gap = await gap_repo.get_gap(gap_id)
        if gap is None:
            raise BizError(Err.GAP_NOT_FOUND, f"知识缺口不存在：{gap_id}")
        status = gap.get("status")
        if status == GapStatus.CONVERTED.value:
            raise BizError(Err.GAP_ALREADY_CONVERTED,
                           "该缺口已转建，不能重复转建")
        if status == GapStatus.IGNORED.value:
            raise BizError(Err.GAP_ALREADY_IGNORED, "该缺口已被忽略，不能转建")
        if int(gap.get("frequency") or 0) == 0:
            raise BizError(Err.GAP_STALE, "缺口数据已过期（窗口内未再出现），请刷新清单")

        final_title = (title or gap.get("question") or "").strip()
        if not final_title or len(final_title) > 200:
            raise BizError(Err.GAP_TITLE_INVALID, "转建标题需非空且不超过 200 字")
        final_category = category_id if category_id is not None \
            else gap.get("suggested_category_id")
        if final_category:
            await _assert_category(final_category)

        # R-07：经 03 建占位（**绝不直连写 kb_documents**，AC-08-12）
        try:
            doc_id = await doc_service.create_from_gap(
                gap_id=gap_id, title=final_title, category_id=final_category)
        except BizError:
            raise
        except Exception as exc:                            # noqa: BLE001
            raise BizError(Err.GAP_DOC_CREATE_FAILED,
                           f"知识单元占位创建失败：{exc}") from exc

        # R-08：经 04 建导入任务；失败要**补偿撤销占位**（R-09）
        from app.services.import_service import import_service

        try:
            task_id = await import_service.create_task_from_gap(doc_id=doc_id,
                                                               gap_id=gap_id)
        except Exception as exc:                            # noqa: BLE001
            logger.error("导入任务创建失败，正在回滚占位文档 %s：%s", doc_id, exc)
            await self._rollback_placeholder(doc_id)
            if isinstance(exc, BizError):
                raise BizError(Err.GAP_TASK_CREATE_FAILED,
                               f"导入任务创建失败：{exc.detail or exc.spec.message}"
                               ) from exc
            raise BizError(Err.GAP_TASK_CREATE_FAILED,
                           f"导入任务创建失败：{exc}") from exc

        # R-10：回写 E19（本模块的表，可直写）。失败**不回滚**（R-13）
        try:
            await gap_repo.mark_converted(gap_id, doc_id=doc_id, actor=actor,
                                          ts_ms=gap_repo.now_ms())
        except Exception as exc:                            # noqa: BLE001
            logger.error("缺口状态回写失败（占位与任务已创建，供人工核对）"
                         "gap=%s doc=%s task=%s：%s", gap_id, doc_id, task_id, exc)
            await self._audit_convert(actor, gap_id, gap, doc_id)
            raise BizError(Err.GAP_STATE_WRITE_FAILED,
                           f"缺口状态回写失败（占位文档 {doc_id} 与任务 "
                           f"{task_id} 已创建，请人工核对）") from exc

        await self._audit_convert(actor, gap_id, gap, doc_id)
        logger.info("缺口已转建 gap=%s doc=%s task=%s", gap_id, doc_id, task_id)
        return {"gap_id": gap_id, "status": GapStatus.CONVERTED.value,
                "converted_doc_id": doc_id, "import_task_id": task_id,
                "doc_status": "disabled", "task_status": "pending",
                "upload_url": f"/api/v1/import/upload?doc_id={doc_id}"}

    async def _rollback_placeholder(self, doc_id: str) -> None:
        """补偿：撤销占位文档（Spec R-09）。补偿失败只记 ERROR ——
        宁可留一个占位文档待人工处理，也不能把原始错误吞掉。"""
        try:
            await doc_service.rollback_gap_placeholder(doc_id)
            logger.warning("已回滚占位文档 %s", doc_id)
        except Exception as exc:                            # noqa: BLE001
            logger.error("回滚占位文档失败（需人工处理）：doc=%s %s", doc_id, exc)

    async def _audit_convert(self, actor: str, gap_id: str,
                             gap: Mapping[str, Any], doc_id: str) -> None:
        await audit_service.record(
            action="gap.convert", actor=actor, actor_name=actor, target_type="gap",
            target_id=gap_id, target_name=str(gap.get("question") or ""),
            before={"status": gap.get("status")},
            after={"status": GapStatus.CONVERTED.value, "converted_doc_id": doc_id},
            reason="一键转建文档")

    # ================================================================== 忽略
    async def ignore(self, *, gap_id: str, reason: str, actor: str) -> dict[str, Any]:
        """忽略缺口（Spec §3.4）。忽略是**人工决策**，聚合不会再改回 open。"""
        gap = await gap_repo.get_gap(gap_id)
        if gap is None:
            raise BizError(Err.GAP_NOT_FOUND, f"知识缺口不存在：{gap_id}")
        if gap.get("status") == GapStatus.IGNORED.value:
            raise BizError(Err.GAP_ALREADY_IGNORED, "该缺口已被忽略")
        note = (reason or "").strip()
        if len(note) > 200:
            raise BizError(Err.GAP_QUERY_INVALID, "忽略原因不超过 200 字")
        await gap_repo.mark_ignored(gap_id, actor=actor, reason=note,
                                    ts_ms=gap_repo.now_ms())
        await audit_service.record(
            action="gap.ignore", actor=actor, actor_name=actor, target_type="gap",
            target_id=gap_id, target_name=str(gap.get("question") or ""),
            before={"status": gap.get("status")},
            after={"status": GapStatus.IGNORED.value, "ignore_reason": note},
            reason=note or "忽略缺口")
        return {"gap_id": gap_id, "status": GapStatus.IGNORED.value,
                "ignored_by": actor}

    # ================================================================== 07 的投递入口
    async def upsert_from_cluster(self, *, representative_question: str,
                                  questions: Sequence[str], frequency: int,
                                  actor: str = "system") -> str:
        """**07 的转建入口**（Spec §1.4）：把"无来源的簇"沉淀成一条缺口。

        07 的驳回接口会调它（`convert_to_gap=true`）。它**不创建文档**，
        只落一条 E19 记录 —— 与"一键转建"是两件事：
        前者记下"这里缺知识"，后者去建一份文档来补。

        `dept_id` 允许为 `None`（07 的簇来自问答日志，可能没有部门信息）：
        Spec §2.1 已按 ER-17 登记这一放宽。
        """
        text = normalize_question(representative_question)
        if not text:
            raise BizError(Err.GAP_TITLE_INVALID, "代表问法为空，无法沉淀为缺口")
        key = build_normalized_key(None, text)
        now = gap_repo.now_ms()
        existing = await gap_repo.get_by_key(key)
        await gap_repo.upsert_batch([{
            "_id": (existing or {}).get("_id") or await gap_repo.next_gap_id(),
            "normalized_key": key,
            "normalized_text": text,
            "dept_id": None,
            "question": representative_question.strip(),
            "frequency": max(1, int(frequency)),
            "max_score": 0.0,
            "suggested_category_id": None,
            "suggested_category_basis": "none",
            "sample_log_ids": [],
            "first_seen_at": (existing or {}).get("first_seen_at") or now,
            "last_seen_at": now,
        }], ts_ms=now)
        row = await gap_repo.get_by_key(key)
        gap_id = str((row or {}).get("_id") or "")
        logger.info("07 投递的簇已沉淀为缺口 %s（%d 条同义问法）", gap_id, frequency)
        await audit_service.record(
            action="gap.create", actor=actor, actor_name=actor, target_type="gap",
            target_id=gap_id, target_name=representative_question,
            after={"frequency": frequency, "questions": len(questions)},
            reason="FAQ 候选驳回时转建缺口")
        return gap_id

    # ================================================================== 导出
    async def export_csv(self, *, status: str = "open", dept_id: str | None = None,
                         keyword: str = "", category_id: str | None = None,
                         min_frequency: int = 1, sort: str = "frequency_desc"
                         ) -> tuple[str, str]:
        """导出缺口清单（CSV，**UTF-8 BOM**，AC-08-23）。

        返回 `(文件名, CSV 文本)`。BOM 不是可选项：没有它，Excel 打开中文会乱码，
        而"导出的表在 Excel 里是乱码"对使用者来说等同于"导出功能坏了"。
        """
        _assert_query(status, sort, "none", min_frequency, 1, 20)
        rows = await gap_repo.list_all(
            status=None if status == "all" else status, dept_id=dept_id,
            keyword=keyword, category_id=category_id, min_frequency=min_frequency,
            sort=sort)
        limit = config_service.gap_export_max_rows
        if len(rows) > limit:
            raise BizError(Err.GAP_EXPORT_TOO_LARGE,
                           f"命中 {len(rows)} 条，超过导出上限 {limit} 条")
        items = await self._decorate(rows)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(EXPORT_HEADERS)
        for item in items:
            writer.writerow([
                item["question"], item["dept_name"], item["frequency"],
                item["max_score"],
                item["suggested_category_path"] or "暂无建议分类",
                item["status_text"], _fmt_ts(item["first_seen_at"]),
                _fmt_ts(item["last_seen_at"]),
            ])
        filename = f"knowledge_gaps_{time.strftime('%Y%m%d_%H%M')}.csv"
        logger.info("缺口清单已导出：%d 行", len(items))
        return filename, "\ufeff" + buffer.getvalue()

    # ================================================================== 维护
    async def startup(self) -> int:
        """启动后先跑一轮（AC-08-19：60s 内首轮由 main 的调度延时控制）。"""
        try:
            result = await self.aggregate(actor="scheduler")
        except Exception as exc:                            # noqa: BLE001
            logger.error("启动期缺口聚合失败（服务继续启动）：%s", exc)
            return 0
        return result.identified


# ---------------------------------------------------------------------- 纯函数
def _doc_id_of(chunk: Any) -> str | None:
    if isinstance(chunk, Mapping):
        value = chunk.get("doc_id")
        return str(value) if value else None
    return str(chunk) if chunk else None


def _mode_question(questions: Sequence[tuple[int, str]]) -> str:
    """众数原文（平局取最早）—— 保证清单标题稳定不跳动。"""
    counts: dict[str, int] = {}
    earliest: dict[str, int] = {}
    for asked_at, question in questions:
        counts[question] = counts.get(question, 0) + 1
        if question not in earliest or asked_at < earliest[question]:
            earliest[question] = asked_at
    if not counts:
        return ""
    return sorted(counts, key=lambda q: (-counts[q], earliest.get(q, 0), q))[0]


def _merge_by_question(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """`group_by=question`：按 `normalized_text` 归并（**派生视图，不落库**）。

    它回答的是"这个盲区是不是好几个部门都在踩"——所以 `dept_id`/`dept_name`
    在这里变成**数组**，而 `frequency` 求和、`max_score` 取最大。
    """
    merged: dict[str, dict[str, Any]] = {}
    for item in items:
        key = str(item.get("normalized_text") or item.get("question"))
        target = merged.get(key)
        if target is None:
            target = dict(item)
            target["dept_id"] = [item["dept_id"]] if item.get("dept_id") else []
            target["dept_name"] = [item["dept_name"]] if item.get("dept_name") else []
            target["gap_ids"] = [item["gap_id"]]
            target["dept_count"] = 1 if item.get("dept_id") else 0
            merged[key] = target
            continue
        target["frequency"] += item["frequency"]
        target["max_score"] = max(target["max_score"], item["max_score"])
        target["last_seen_at"] = max(target.get("last_seen_at") or 0,
                                     item.get("last_seen_at") or 0)
        target["gap_ids"].append(item["gap_id"])
        if item.get("dept_id") and item["dept_id"] not in target["dept_id"]:
            target["dept_id"].append(item["dept_id"])
            target["dept_name"].append(item["dept_name"])
            target["dept_count"] += 1
    return sorted(merged.values(), key=lambda r: (-r["frequency"], r["max_score"]))


def _status_text(status: Any) -> str:
    try:
        return GapStatus(str(status)).label
    except ValueError:                                      # pragma: no cover
        return str(status or "")


def _fmt_ts(ts_ms: Any) -> str:
    if not ts_ms:
        return ""
    import datetime as _dt

    return _dt.datetime.fromtimestamp(int(ts_ms) / 1000).strftime("%Y-%m-%d %H:%M")


def _start_of_day(ts_ms: int) -> int:
    """对齐到当天 00:00（让"同一个聚合窗口"有稳定标识）。"""
    import datetime as _dt

    moment = _dt.datetime.fromtimestamp(ts_ms / 1000)
    return int(moment.replace(hour=0, minute=0, second=0, microsecond=0)
               .timestamp() * 1000)


def _assert_query(status: str, sort: str, group_by: str, min_frequency: int,
                  page: int, page_size: int) -> None:
    """参数校验（Spec §3.1 R-09，**用 GAP-1001 而不是通用参数错**）。"""
    allowed_status = {s.value for s in GapStatus} | {"all"}
    if status not in allowed_status:
        raise BizError(Err.GAP_QUERY_INVALID, f"status 取值非法：{status}")
    allowed_sort = {"frequency_desc", "frequency_asc", "max_score_desc",
                    "max_score_asc", "last_seen_desc"}
    if sort not in allowed_sort:
        raise BizError(Err.GAP_QUERY_INVALID, f"sort 取值非法：{sort}")
    if group_by not in ("none", "question"):
        raise BizError(Err.GAP_QUERY_INVALID, f"group_by 取值非法：{group_by}")
    if min_frequency < 0:
        raise BizError(Err.GAP_QUERY_INVALID, f"min_frequency 不能为负：{min_frequency}")
    if page < 1 or not 1 <= page_size <= 200:
        raise BizError(Err.GAP_QUERY_INVALID,
                       f"分页参数非法：page={page} page_size={page_size}（上限 200）")


async def _assert_category(category_id: str) -> None:
    """转建分类必须存在且启用（`GAP-1004`）。"""
    from app.repositories import doc_repo

    node = await doc_repo.get_category(category_id)
    if node is None or node.get("status") not in (None, "active"):
        raise BizError(Err.GAP_CATEGORY_INVALID, f"分类不存在或已停用：{category_id}")


async def _dept_names(dept_ids: Sequence[str]) -> dict[str, str]:
    """批量取部门名（R-03：经 02 的仓储**一次**取回，失败回落为 id）。"""
    wanted = [d for d in dict.fromkeys(dept_ids) if d]
    if not wanted:
        return {}
    try:
        from app.repositories import org_repo

        rows = await org_repo.find_depts_by_ids(wanted)
        return {dept_id: str((row or {}).get("name") or dept_id)
                for dept_id, row in rows.items()}
    except Exception as exc:                                # noqa: BLE001
        logger.warning("部门名批量取回失败，回落到 dept_id：%s", exc)
        return {}


async def _category_paths(category_ids: Sequence[str]) -> dict[str, str]:
    """批量取分类路径（`客户服务 / 跨境物流`，对齐原型 `gapTblR0C4T`）。"""
    wanted = [c for c in dict.fromkeys(category_ids) if c]
    if not wanted:
        return {}
    try:
        from app.repositories import doc_repo

        rows = await doc_repo.find_categories_by_ids(wanted)
        return {category_id: " / ".join((row or {}).get("path") or []) or category_id
                for category_id, row in rows.items()}
    except Exception as exc:                                # noqa: BLE001
        logger.warning("分类路径批量取回失败，回落为 category_id：%s", exc)
        return {}


gap_service = GapService()

__all__ = ["GapService", "gap_service", "AggregateResult", "MAX_SAMPLES",
           "EXPORT_HEADERS"]
