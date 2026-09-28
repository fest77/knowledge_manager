# -*- coding: utf-8 -*-
"""模块 04 的服务层：六阶段导入流水线（`upload → pdf_to_md → md_img → split →
embedding → milvus`）。

## 这个文件在整条链路里的位置

```
routes_import ─┐
               ├─► ImportService ──► parser（归一化）/ splitter / embedding_service
08 缺口转建 ───┘        │                    │
                        │                    └─► chunk_store ─► Milvus（E01）
                        ├─► minio_store（E03 原文件 / MD / 图片）
                        ├─► import_repo（E06 任务，**本模块独占写**）
                        └─► DocService（E04 台账，**必须经 03**，ER-02）
```

## 五条不可让步的设计约束

| # | 约束 | 违反的后果 |
|---|---|---|
| 1 | **`upload` 在请求内完成，其余五个丢后台** | 全放请求内 → 100 页 PDF 让请求挂几分钟；全放后台 → 前端拿不到 `doc_id` 无法轮询 |
| 2 | **先 `store_chunks()` 再 `mark_import_done()`** | 反过来 → 切片仍不可检索、文档却已启用。症状是"文档绿的、搜不到" |
| 3 | **重试/重建前先按 `doc_id` 删旧切片** | 否则同一文档两份切片，检索重复、`chunk_count` 翻倍 |
| 4 | **取消是协作式的，只在阶段边界生效** | 线程内的推理无法安全强杀；只能靠杀进程，会留下僵尸任务 |
| 5 | **每个阶段边界写一次 `stage`/`progress`** | 前端才能看到"卡在哪一步"；只写一次会停在 10% 然后突然 100% |

## 关于 §2.6 的 `enabled` 初值

Spec 说"`enabled` 初始 = 台账 `status`（`auto_enable=true` 写 `true`）"，
但 `chunk_store.build_rows()` **一律写 `false`**，由 `mark_import_done()` 在
最后一步按 `auto_enable` 统一同步。两者**语义等价**，但后者有个关键好处：
**切片从写入到"文档完成"之间绝不可检索**。若按 `auto_enable` 提前写 `true`，
中途失败的文档会留下"半截切片可被召回"的窗口
——而 §3.5 R-04 对取消的要求恰恰是"绝不允许半截切片被召回"。
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.core.config import settings
from app.core.enums import ImportStatus, ImportTaskStage, ImportTaskStatus
from app.core.errors import BizError, Err
from app.core.logging import logger
from app.infra.minio import minio_store
from app.repositories import import_repo
from app.services import chunk_store, parser
from app.services.audit_service import audit_service
from app.services.config_service import config_service
from app.services.doc_service import doc_service
from app.services.embedding_service import EmbeddingUnavailable, embedding_service
from app.services.splitter import split_markdown, split_stats

# 阶段 → (起始进度, 结束进度)。严格照 Spec §4.2 的权重表：
# 解析 10→30、图片 30→40、切片 40→55、向量化 55→95、入库 95→100。
# 权重不均**是刻意的**：`embedding` 占 40 个点是实测最慢的阶段（BGE-M3 逐批推理），
# 若平均分配，进度条会"很快爬到 80% 然后卡住不动"，看起来像死了。
STAGE_PROGRESS: dict[str, tuple[int, int]] = {
    ImportTaskStage.UPLOAD.value: (0, 10),
    ImportTaskStage.PDF_TO_MD.value: (10, 30),
    ImportTaskStage.MD_IMG.value: (30, 40),
    ImportTaskStage.SPLIT.value: (40, 55),
    ImportTaskStage.EMBEDDING.value: (55, 95),
    ImportTaskStage.MILVUS.value: (95, 100),
}
STAGE_TOTAL = len(STAGE_PROGRESS)

# 单次读取的分片大小：1MB。**流式**读上传体，不把 100MB 读进内存
CHUNK_READ = 1 << 20
# MinIO 不可用时的本地降级目录（`minio_local` 降级，Spec §7.2）
LOCAL_STORE_DIR = Path(__file__).resolve().parents[2] / "storage" / "files"
# 本地降级时台账 `storage.bucket` 的取值（AC-04-18 逐字要求 `"LOCAL"`）。
# **不能用 `None` 表示降级**：`None` 与"还没落盘"无法区分，而 AC-04-18
# 的判定方式是"查库看到 LOCAL"——用一个显式哨兵值才可判定、可检索
_LOCAL_BUCKET = "LOCAL"
# 中间产物（MD）在对象存储里的固定后缀
MD_OBJECT_SUFFIX = "parsed.md"

_EXT_CONTENT_TYPE = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown", ".txt": "text/plain",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}

# 规范的知识单元编号：`DOC` + 8 位日期 + 6 位序列（与 `doc_repo.next_doc_id` 同源）。
# 对象键的第一段必须匹配它——见 `assert_object_key` 的说明。
_DOC_ID_RE = re.compile(r"^DOC\d{14}$")


class StageFailure(RuntimeError):
    """阶段内失败：携带错误码，由流水线转成任务 `error` 与台账 `import_status`。"""

    def __init__(self, code: Any, message: str, *, stage: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage


@dataclass(slots=True)
class UploadedItem:
    """单个文件的导入结果（`items[]` 的一项，Spec §3.1/§3.2 出参）。"""

    doc_id: str
    task_id: str | None
    file_name: str
    file_hash: str
    file_ext: str
    file_size: int
    reused: bool = False
    duplicated_of: str | None = None
    stage: str = ImportTaskStage.PDF_TO_MD.value
    progress: int = 10


@dataclass(slots=True)
class RejectedItem:
    """被拒的文件（`rejected[]` 的一项）：**逐项返回**而不是让整批失败。"""

    file_name: str
    code: str
    message: str


@dataclass(slots=True)
class _Pipeline:
    """一次流水线的运行态。

    **为什么把中间产物都挂在对象上而不是层层返回**：六个阶段之间有 4 份传递物
    （Markdown、图片、切片、向量），层层 `return` 会让每一层的签名都被这 4 份
    绑死，任何一个阶段想多带一点信息都要改 6 个函数签名。
    声明成 `slots` 字段则"流水线状态"是显式的、可断言的，也不会写错属性名
    （`slots` 会在写错时直接抛错，而不是悄悄多一个没人读的属性）。
    """

    task_id: str
    doc_id: str
    file_hash: str
    file_name: str
    file_ext: str
    title: str
    auto_enable: bool
    from_stage: str
    durations: dict[str, int] = field(default_factory=dict)
    workdir: Path | None = None
    markdown: str = ""
    images: list[parser.ParsedImage] = field(default_factory=list)
    parser_name: str = ""
    char_count: int = 0
    chunks: list[Any] = field(default_factory=list)
    split_stats: dict[str, int] = field(default_factory=dict)
    written: int = 0
    _stage_name: str = ""
    _stage_started: float = 0.0

    def mark(self, stage: str) -> None:
        """记一次阶段耗时（`durations[上一阶段]` = 实际毫秒数），并把计时切到新阶段。"""
        if self._stage_name and self._stage_started:
            self.durations[self._stage_name] = int(
                (time.monotonic() - self._stage_started) * 1000)
        self._stage_name = stage
        self._stage_started = time.monotonic()

    def flush_stage(self) -> None:
        """结算最后一个阶段的耗时。

        没有这一步，`durations` 会**永远缺最后一项**（`milvus`）——
        而"入库花了多久"恰恰是排障时最想知道的那个数（它区分"模型慢"与"向量库慢"）。
        """
        if self._stage_name and self._stage_started:
            self.durations[self._stage_name] = int(
                (time.monotonic() - self._stage_started) * 1000)
            self._stage_started = 0.0


class ImportService:
    """导入服务。**进程级单例**：`_sem` 与在跑任务表都必须全局唯一。

    为什么并发闸门放在**服务实例**而不是模块函数：`Semaphore` 必须绑定到
    "当前运行的那个事件循环"。模块级常量会在 import 时创建，绑定到可能是
    另一个 loop（测试里每个用例一个新 loop），一 await 就报
    "got Future attached to a different loop"。所以 `_sem` 惰性创建。
    """

    def __init__(self) -> None:
        self._sem: asyncio.Semaphore | None = None
        self._running: dict[str, asyncio.Task[Any]] = {}

    # ------------------------------------------------------------------ 生命周期
    @property
    def semaphore(self) -> asyncio.Semaphore:
        """并发闸门（惰性创建，见类文档）。

        GPU 不可用时**降到 1**（Spec §4.4）：CPU 推理会占满所有核心，
        两个并发只会让彼此与问答链路一起变慢。
        """
        if self._sem is None:
            limit = config_service.import_concurrency
            if embedding_service.device in (None, "cpu"):
                limit = min(limit, 1)
            self._sem = asyncio.Semaphore(max(1, limit))
            logger.info("导入并发闸门已创建：%d", max(1, limit))
        return self._sem

    async def startup(self) -> dict[str, Any]:
        """启动补偿（在 `lifespan` 里调）。

        顺序很重要：**先补集合、再改判僵尸任务**。反过来的话，
        "改判"这个动作本身可能因为集合缺失而失败，僵尸任务就留下来了。
        """
        await import_repo.ensure_indexes()
        await chunk_store.ensure_collection()
        interrupted = await import_repo.fail_stale_as_interrupted(ts_ms=_now_ms())
        return {"interrupted_tasks": interrupted}

    async def watchdog(self) -> int:
        """超时看门狗（由 scheduler 定期调）：心跳超阈值的 `running` → `timeout`。"""
        return await import_repo.mark_overdue_as_timeout(
            before_ms=_now_ms() - config_service.import_timeout_min * 60_000,
            ts_ms=_now_ms())

    # ------------------------------------------------------------------ 上传
    async def upload_one(self, *, upload: Any, actor_id: str, category_id: str | None,
                         title: str | None, auto_enable: bool = True,
                         batch_id: str | None = None) -> UploadedItem:
        """单文件上传（Spec §3.1）：**请求内完成 `upload` 阶段**，其余丢后台。

        `upload` 阶段做的事全部是"要么立刻知道成败、要么不该产生副作用"的：
        校验 → 流式落盘 → 建台账 → 建任务。放在请求内才能保证
        "落盘失败就不产生台账孤儿记录"。
        """
        started = time.monotonic()          # `upload` 阶段耗时起点（见下方 durations）
        file_name = str(getattr(upload, "filename", "") or "")
        # R-03 先于 R-04：`.pptx` 这种"格式不支持"与"文件名非法"是两回事，
        # 报错文案与前端引导都不同（前者提示支持哪些格式，后者提示改名）
        if not parser.is_supported(file_name):
            raise BizError(Err.IMP_EXT_UNSUPPORTED,
                           f"不支持的文件格式：{file_name}（只支持 "
                           f"{', '.join(parser.SUPPORTED_EXTS)}）")
        reason = parser.validate_file_name(file_name)
        if reason:
            raise BizError(Err.IMP_FILENAME_INVALID, reason)
        ext = parser.normalize_ext(file_name)

        # R-14 队列保护：先看队列再干活，避免白落盘
        queue_max = config_service.import_max_queue
        running = await import_repo.count_running()
        if running >= queue_max:
            raise BizError(Err.IMP_QUEUE_FULL,
                           f"导入队列已满（{running}/{queue_max}），请稍后重试")

        temp_path, file_hash, file_size, head = await self._spool(upload, ext)
        try:
            if file_size == 0:
                raise BizError(Err.IMP_FILE_MISSING, "上传文件为空（0 字节）")
            max_bytes = config_service.import_max_file_mb * 1024 * 1024
            if file_size > max_bytes:
                raise BizError(Err.IMP_FILE_TOO_LARGE,
                               f"文件 {file_size} 字节超出上限 {max_bytes} 字节")
            if not parser.magic_ok(ext, head):
                raise BizError(Err.IMP_CONTENT_MISMATCH,
                               f"文件内容与扩展名 {ext} 不符（魔数校验失败）")
            if category_id:
                await self._assert_category(category_id)

            # R-10：同哈希已有在途任务 → IMP-3003，**不投递第二个任务**
            await self._assert_not_importing(file_hash)
            decision = await doc_service.resolve_by_hash(file_hash)
            if decision["decision"] == "reuse":
                # R-09：命中未软删除且已导完的同哈希文档 → **什么都不做，直接复用**。
                # 不建台账、不建任务、不落盘、不解析、不向量化——这是幂等入口的全部意义
                doc = await doc_service.get(str(decision["doc_id"])) or {}
                return UploadedItem(
                    doc_id=str(decision["doc_id"]), task_id=None, file_name=file_name,
                    file_hash=file_hash, file_ext=ext, file_size=file_size,
                    reused=True, duplicated_of=str(doc.get("file_name") or "") or None,
                    stage=ImportTaskStage.UPLOAD.value, progress=10)
            # 决策表里剩下的 `new` 与 `soft_deleted` 都走"新文档导入"：
            # 软删除的同哈希文档**不自动恢复**（恢复语义属于 03），
            # 靠 `file_hash` 的部分唯一索引（partialFilterExpression deleted_at=null）
            # 允许新记录落库——否则唯一索引会永久阻塞重导。

            # 预分配编号：对象键是 `{doc_id}/...`，落盘时就得知道编号（见 DocService.create）
            allocated_id, allocated_no = await self._peek_doc_id()
            object_key = minio_store.object_key(allocated_id, 0, file_name)
            storage, degraded_kind = await self._persist(object_key, temp_path, ext)

            # R-12：建台账（**必须经 03**，ER-02）
            try:
                doc_id = await doc_service.create(
                    file_name=file_name, file_ext=ext.lstrip("."),
                    file_size=file_size, file_hash=file_hash, storage=storage,
                    created_by=actor_id, category_id=category_id, title=title,
                    doc_id=allocated_id, doc_no=allocated_no)
            except Exception as exc:                            # noqa: BLE001
                await self._rollback_object(storage)
                if isinstance(exc, BizError):
                    raise
                raise BizError(Err.IMP_META_STORE_FAILED,
                               f"建台账失败：{exc}") from exc

            if doc_id != allocated_id:
                # 同哈希并发创建被改判为复用（唯一索引兜底）。此时对象写在
                # `allocated_id/` 下，而台账指向 `doc_id` —— 文档会**读不到自己的原文件**。
                # 处理：按正确前缀重传一份，再删掉错前缀的那份。
                await self._realign_object(storage, temp_path, ext, doc_id, file_name)

            # R-13：建任务；失败要**回滚已落盘对象**，否则桶里留下孤儿文件
            try:
                task_id = await self._create_task(
                    doc_id=doc_id, file_hash=file_hash, file_name=file_name,
                    actor_id=actor_id, batch_id=batch_id, auto_enable=auto_enable,
                    storage_degraded=degraded_kind is not None,
                    # `upload` 是请求内跑完的阶段，它的耗时只能在这里量。
                    # 不写的话 `durations` 永远缺第一项，前端"各阶段耗时"表
                    # 第一行就空着，看起来像"上传没耗时"（其实它可能占了大头：
                    # 100MB 流式落盘 + 校验 + 建台账）
                    durations={"upload": int((time.monotonic() - started) * 1000)})
            except Exception as exc:                            # noqa: BLE001
                await self._rollback_object(storage)
                if isinstance(exc, BizError):
                    raise
                raise BizError(Err.IMP_META_STORE_FAILED,
                               f"建导入任务失败：{exc}") from exc

            if degraded_kind:
                await import_repo.note_degradation(
                    task_id, kind=degraded_kind,
                    detail=f"MinIO 不可用，原文件已落本地：{storage.get('local_path')}",
                    ts_ms=_now_ms())

            await self._audit(actor_id, "doc.import.start", doc_id, file_name,
                              {"task_id": task_id, "file_hash": file_hash,
                               "file_size": file_size, "category_id": category_id})
            self.submit(task_id)
            return UploadedItem(doc_id=doc_id, task_id=task_id, file_name=file_name,
                                file_hash=file_hash, file_ext=ext, file_size=file_size)
        finally:
            # 临时文件**必须删**：它在系统临时目录里，不删就会随每次上传堆积，
            # 而"谁在用磁盘"根本查不出来（文件名是随机串）
            temp_path.unlink(missing_ok=True)

    async def upload_batch(self, *, uploads: Sequence[Any], actor_id: str,
                           category_id: str | None,
                           relative_paths: Sequence[str] | None = None,
                           auto_map_category: bool = False,
                           auto_enable: bool = True) -> dict[str, Any]:
        """批量上传（Spec §3.2）：**部分成功返回 200**，逐项进 `rejected[]`。

        为什么不让一个坏文件拖垮整批：拖 50 个文件时"第 37 个是 .pptx 导致整批失败"
        是纯损失——49 个可用文件白传一遍。所以逐文件独立校验，
        只有**全部被拒**才抛错（此时 `code` 取第一条失败项的码）。
        """
        if not uploads:
            raise BizError(Err.IMP_FILE_MISSING, "未提供任何文件")
        max_files = config_service.import_max_batch_files
        if len(uploads) > max_files:
            raise BizError(Err.IMP_BATCH_TOO_LARGE,
                           f"文件数 {len(uploads)} 超过上限 {max_files}")
        if relative_paths and len(relative_paths) != len(uploads):
            raise BizError(Err.IMP_BATCH_MISMATCH,
                           "relative_paths 与 files 数量不一致")
        total_bytes = sum(int(getattr(u, "size", 0) or 0) for u in uploads)
        max_batch = config_service.import_max_batch_mb * 1024 * 1024
        if total_bytes > max_batch:
            raise BizError(Err.IMP_BATCH_TOO_LARGE,
                           f"批次总量 {total_bytes} 字节超出上限 {max_batch} 字节")
        if auto_map_category and not relative_paths:
            # G-01 未确认，这里**明确拒绝**而不是静默忽略：静默忽略会让前端以为
            # 分类已按目录建好，实际全都落在"未分类"，排查方向完全错
            raise BizError(Err.IMP_QUERY_INVALID,
                           "auto_map_category=true 时必须提供 relative_paths")

        batch_id = f"BATCH{int(time.time() * 1000)}"
        items: list[dict[str, Any]] = []
        rejected: list[RejectedItem] = []
        reused_count = 0
        for upload in uploads:
            name = str(getattr(upload, "filename", "") or "")
            try:
                item = await self.upload_one(
                    upload=upload, actor_id=actor_id, category_id=category_id,
                    title=None, auto_enable=auto_enable, batch_id=batch_id)
            except BizError as exc:
                # `BizError` 的对外文案是 `spec.message`，`detail` 是更具体的上下文
                # （哪个文件、哪个参数）。批量场景下两者都要——只回 `spec.message`
                # 会让"不支持的文件格式"这句出现在 5 个不同文件上，无法分辨
                rejected.append(RejectedItem(file_name=name, code=exc.spec.code,
                                             message=str(exc.detail or exc.spec.message)))
                continue
            except Exception as exc:                            # noqa: BLE001
                logger.exception("批量导入 %s 出现未预期异常", name)
                rejected.append(RejectedItem(file_name=name, code=Err.IMP_INTERNAL.code,
                                             message=str(exc)[:200]))
                continue
            if item.reused:
                reused_count += 1
            items.append({"doc_id": item.doc_id, "task_id": item.task_id,
                          "file_name": item.file_name, "reused": item.reused,
                          "file_hash": item.file_hash})
        if not items:
            first = rejected[0]
            raise BizError(_lookup_spec(first.code), first.message)
        logger.info("批量导入 %s：受理 %d 复用 %d 拒绝 %d",
                    batch_id, len(items), reused_count, len(rejected))
        return {"batch_id": batch_id, "total": len(uploads), "accepted": len(items),
                "reused_count": reused_count,
                "rejected": [{"file_name": r.file_name, "code": r.code,
                              "message": r.message} for r in rejected],
                "items": items}

    # ------------------------------------------------------------------ 查询
    async def get_task_detail(self, task_id: str) -> dict[str, Any]:
        """任务进度（Spec §3.3）：`stage_index` / `stage_total` 由后端算好。

        Spec §3.4 R-02 说得很直白：前端要**零分支**渲染阶段条。所以"第几步/共几步"
        这个纯计算不留给前端——否则前端会各写一套映射，改阶段名时两边不一致。
        """
        task = await import_repo.get_task(task_id)
        if task is None:
            raise BizError(Err.IMP_TASK_NOT_FOUND, f"导入任务不存在：{task_id}")
        doc = await doc_service.get(str(task.get("doc_id"))) or {}
        stage = str(task.get("stage") or ImportTaskStage.UPLOAD.value)
        return {
            "task_id": task_id, "doc_id": task.get("doc_id"),
            "doc_title": doc.get("title") or task.get("doc_title"),
            "file_name": task.get("file_name"),
            "status": task.get("status"), "stage": stage,
            "stage_index": import_repo.stage_index(stage), "stage_total": STAGE_TOTAL,
            "progress": int(task.get("progress") or 0),
            "done_stages": import_repo.normalize_done_stages(
                task.get("done_stages") or []),
            "durations": task.get("durations") or {},
            "error": task.get("error"),
            "retry_count": int(task.get("retry_count") or 0),
            "retry_of": task.get("retry_of"),
            "from_stage": task.get("from_stage"),
            "storage_degraded": bool(task.get("storage_degraded")),
            "degraded": task.get("degraded") or [],
            "created_by": task.get("created_by"), "created_at": task.get("created_at"),
            "finished_at": task.get("finished_at"),
            "cancel_requested": bool(task.get("cancel_requested")),
        }

    async def list_queue(self, *, doc_id: str | None = None, status: str | None = None,
                         stage: str | None = None, batch_id: str | None = None,
                         page: int = 1, page_size: int = 20) -> dict[str, Any]:
        """导入队列（Spec §3.4）：原型 `02` 的「文件 / 阶段 / 进度」三列。"""
        _assert_page(page, page_size)
        if status and status not in {s.value for s in ImportTaskStatus}:
            raise BizError(Err.IMP_QUERY_INVALID, f"status 取值非法：{status}")
        if stage and stage not in {s.value for s in ImportTaskStage}:
            raise BizError(Err.IMP_QUERY_INVALID, f"stage 取值非法：{stage}")
        query: dict[str, Any] = {}
        if doc_id:
            query["doc_id"] = doc_id
        if status:
            query["status"] = status
        if stage:
            query["stage"] = stage
        if batch_id:
            query["batch_id"] = batch_id
        from app.infra.mongo import mongo

        collection = mongo.collection(import_repo.IMPORT_TASKS)
        total = await collection.count_documents(query)
        cursor = (collection.find(query).sort("created_at", -1)
                  .skip((page - 1) * page_size).limit(page_size))
        rows = await cursor.to_list(length=page_size)
        titles = await self._titles_of([str(r.get("doc_id")) for r in rows
                                        if r.get("doc_id")])
        return {
            "items": [{
                "task_id": r.get("_id"), "doc_id": r.get("doc_id"),
                "doc_title": titles.get(str(r.get("doc_id"))) or r.get("doc_title"),
                "file_name": r.get("file_name"), "status": r.get("status"),
                "stage": r.get("stage"), "stage_index": import_repo.stage_index(
                    str(r.get("stage") or "")),
                "progress": int(r.get("progress") or 0),
                "storage_degraded": bool(r.get("storage_degraded")),
                "created_by": r.get("created_by"), "created_at": r.get("created_at"),
            } for r in rows],
            "total": total, "page": page, "page_size": page_size,
        }

    async def chunk_preview(self, doc_id: str, *, page: int = 1, page_size: int = 20,
                            keyword: str = "", full: bool = False) -> dict[str, Any]:
        """切片预览（Spec §3.7）：供知识管理员核对切分质量。

        用于答辩演示"切片可查看"——所以 `title` / `parent_title` / `part` 三个
        锚点必须回传：只看正文没法判断"切得对不对"，得看它挂在哪个章节下。
        """
        _assert_page(page, page_size)
        doc = await doc_service.get(doc_id)
        if doc is None or doc.get("deleted_at") is not None:
            raise BizError(Err.IMP_DOC_UNAVAILABLE, f"知识单元不存在或已删除：{doc_id}")
        import_status = str(doc.get("import_status") or "")
        if import_status in (ImportStatus.PENDING.value, ImportStatus.PARSING.value,
                             ImportStatus.EMBEDDING.value):
            # R-02：导入中切片不完整。**拒绝而不是返回部分**——部分切片会让管理员
            # 以为"这份文件只切出 3 片"，据此去调切片参数，方向完全错了
            raise BizError(Err.IMP_CHUNKS_NOT_READY,
                           f"文档正在导入中（{import_status}），切片暂不可查看")
        from app.infra.milvus import (F_CHUNK_ID, F_CHUNK_INDEX, F_CONTENT, F_ENABLED,
                                      F_FILE_TITLE, F_PARENT_TITLE, F_PART, F_TITLE,
                                      milvus)

        rows, total = await milvus.query_chunks(
            doc_id, offset=(page - 1) * page_size, limit=page_size, keyword=keyword)
        items: list[dict[str, Any]] = []
        for row in rows:
            content = str(row.get(F_CONTENT) or "")
            truncated = False
            if not full and len(content) > 200:
                content, truncated = content[:200], True
            elif full and len(content) > 4000:
                content, truncated = content[:4000], True
            items.append({
                "chunk_id": row.get(F_CHUNK_ID),
                "chunk_index": int(row.get(F_CHUNK_INDEX, 0)),
                "title": row.get(F_TITLE) or "",
                "parent_title": row.get(F_PARENT_TITLE) or "",
                "file_title": row.get(F_FILE_TITLE) or "",
                "part": int(row.get(F_PART, 0)),
                "enabled": bool(row.get(F_ENABLED)),
                "char_count": len(content), "content": content,
                "truncated": truncated,
            })
        return {"doc_id": doc_id, "doc_title": doc.get("title"),
                "import_status": import_status, "total": total,
                "page": page, "page_size": page_size, "items": items}

    # ------------------------------------------------------------------ 取消 / 重试
    async def cancel(self, task_id: str, *, actor_id: str, reason: str = "",
                     can_manage_others: bool = False) -> dict[str, Any]:
        """取消导入（Spec §3.5）：协作式，阶段边界生效。

        `cancel_pending=true` 是**诚实的**回答：请求已受理，但工作线程可能正卡在
        MinerU 或 BGE 推理里（`to_thread` 无法安全强杀）。前端据此显示"取消中"，
        而不是"已取消"——后者会让用户以为切片已经不可召回了。
        """
        task = await import_repo.get_task(task_id)
        if task is None:
            raise BizError(Err.IMP_TASK_NOT_FOUND, f"导入任务不存在：{task_id}")
        if import_repo.is_terminal(str(task.get("status"))):
            raise BizError(Err.IMP_TASK_STATE_INVALID,
                           f"任务已处于终态 {task.get('status')}，无法取消")
        if str(task.get("created_by")) != actor_id and not can_manage_others:
            # R-02：取消他人任务需要管理级动作（`doc:delete`），由路由层传入判定结果
            raise BizError(Err.IMP_TASK_FORBIDDEN, "只能取消自己创建的导入任务")
        await import_repo.request_cancel(task_id, ts_ms=_now_ms(), reason=reason)
        await self._audit(actor_id, "doc.import.cancel", str(task.get("doc_id")),
                          str(task.get("file_name")),
                          {"task_id": task_id, "reason": reason})
        return {"task_id": task_id, "status": ImportTaskStatus.CANCELLED.value,
                "cancel_pending": True}

    async def retry(self, task_id: str, *, actor_id: str, from_stage: str | None = None
                    ) -> dict[str, Any]:
        """重试导入（Spec §3.6）：**新建任务**（`retry_of`），保留失败现场。"""
        task = await import_repo.get_task(task_id)
        if task is None:
            raise BizError(Err.IMP_TASK_NOT_FOUND, f"导入任务不存在：{task_id}")
        status = str(task.get("status"))
        if status not in (ImportTaskStatus.FAILED.value, ImportTaskStatus.TIMEOUT.value,
                          ImportTaskStatus.CANCELLED.value):
            raise BizError(Err.IMP_TASK_STATE_INVALID,
                           f"任务当前状态 {status} 不允许重试")
        retry_count = int(task.get("retry_count") or 0) + 1
        max_retry = config_service.import_max_retry
        if retry_count > max_retry:
            # 上限的意义：一个必然失败的文件（比如加密 PDF）不该被无限重试，
            # 每次重试都占满并发闸门，把正常导入全堵在后面
            raise BizError(Err.IMP_RETRY_EXHAUSTED, f"重试次数已达上限（{max_retry}）")
        stage = from_stage or ImportTaskStage.PDF_TO_MD.value
        if stage not in {s.value for s in ImportTaskStage} or \
                stage == ImportTaskStage.UPLOAD.value:
            raise BizError(Err.IMP_QUERY_INVALID,
                           f"from_stage 取值非法：{from_stage}（upload 不重做）")

        if await import_repo.count_running() >= config_service.import_max_queue:
            raise BizError(Err.IMP_QUEUE_FULL, "导入队列已满，请稍后重试")

        doc_id = str(task.get("doc_id"))
        # R-05：跳过解析阶段的前提是中间产物还在。**先探测再决定**，
        # 否则重试会从 md_img 开始、却拿不到 Markdown，失败在一堆无关的地方
        if stage != ImportTaskStage.PDF_TO_MD.value and \
                not await self._md_artifact_exists(doc_id):
            stage = ImportTaskStage.PDF_TO_MD.value
            logger.info("文档 %s 的 MD 产物已丢失，重试改从 pdf_to_md 开始", doc_id)

        new_id = await self._create_task(
            doc_id=doc_id, file_hash=str(task.get("file_hash")),
            file_name=str(task.get("file_name")), actor_id=actor_id,
            batch_id=task.get("batch_id"),
            auto_enable=bool(task.get("auto_enable", True)),
            storage_degraded=bool(task.get("storage_degraded")),
            retry_of=task_id, retry_count=retry_count, from_stage=stage)
        # 审计动作名照 Spec 只有四个，重试记 `doc.import.start`（它确实"重新开始导入"了），
        # 靠 detail 里的 `retry_of` 区分——**不自创 `doc.import.retry`**（ER-16）
        await self._audit(actor_id, "doc.import.start", doc_id,
                          str(task.get("file_name")),
                          {"task_id": new_id, "retry_of": task_id,
                           "from_stage": stage, "retry_count": retry_count})
        self.submit(new_id)
        return {"task_id": new_id, "retry_of": task_id, "retry_count": retry_count,
                "status": ImportTaskStatus.PENDING.value, "from_stage": stage}

    # ------------------------------------------------------------------ 内部服务接口（§3.8）
    async def set_chunks_enabled(self, doc_id: str, enabled: bool) -> int:
        """**03 知识单元的调用点**（ER-15 / ER-02）：批量改切片 `enabled`。

        这里只是转发到 `chunk_store`（E01 的唯一写入口）。保留这层薄封装是为了让
        "03 调 04"这条依赖关系在**代码里可见**——03 不该直接 import `chunk_store`，
        否则"谁写了 `kb_chunks_v2`"又要多看一个文件才能回答。
        """
        return await chunk_store.set_chunks_enabled(doc_id, enabled)

    async def rebuild_chunks(self, doc_id: str, *, actor_id: str = "system") -> str:
        """从原文件重建切片（AD-05「切片可重建」）：**仅脚本 / 内部调用**。

        重建 = 新建一个从 `pdf_to_md` 起的导入任务。**不新开通道**——
        重建与导入走同一条流水线，两者行为才不可能漂移；
        `milvus` 阶段会先按 `doc_id` 删旧切片再插，天然幂等。
        """
        doc = await doc_service.get(doc_id)
        if doc is None:
            raise BizError(Err.IMP_DOC_UNAVAILABLE, f"知识单元不存在：{doc_id}")
        task_id = await self._create_task(
            doc_id=doc_id, file_hash=str(doc.get("file_hash")),
            file_name=str(doc.get("file_name")), actor_id=actor_id, batch_id=None,
            auto_enable=True,
            storage_degraded=bool((doc.get("storage") or {}).get("storage_degraded")),
            from_stage=ImportTaskStage.PDF_TO_MD.value)
        self.submit(task_id)
        logger.warning("文档 %s 触发切片重建（任务 %s）", doc_id, task_id)
        return task_id

    # ------------------------------------------------------------------ 对象读取
    @staticmethod
    def assert_object_key(object_key: str) -> str:
        """校验对象键归属，返回它所属的 `doc_id`；越界抛 `IMP-2002`。

        这是 §2.2「越界保护」的唯一落点（AC-04-24）。规则：
        ① 非空、不含 `..` 与反斜杠（防路径穿越）；
        ② 第一段必须是**规范的知识单元编号**（`DOC{8位日期}{6位序列}`）。
        第二点是关键：只检查"有没有斜杠"是不够的，`../` 这类键的第一段不是编号，
        走到存储层就会读到别的目录；而用正则钉死编号格式，
        既挡住了穿越，也顺带保证"任何能读到的对象都属于某个真实存在的文档"。
        """
        key = (object_key or "").strip()
        if not key or key.startswith("/") or ".." in key or "\\" in key:
            raise BizError(Err.IMP_OBJECT_KEY_MISMATCH, f"非法的对象键：{object_key!r}")
        head = key.split("/", 1)[0]
        if not _DOC_ID_RE.match(head):
            raise BizError(Err.IMP_OBJECT_KEY_MISMATCH,
                           f"对象键前缀不是合法的知识单元编号：{head}")
        return head

    async def read_object(self, object_key: str) -> tuple[bytes, str]:
        """读导入产物（原文件 / MD / 图片），返回 `(bytes, content_type)`。

        先校验前缀归属，再确认**该文档真实存在**——只校验格式的话，
        一个伪造的 `DOC20260101000001/whatever` 也能通过，
        而它背后根本没有文档，读出来的内容无法归属、也无从鉴权。
        """
        doc_id = self.assert_object_key(object_key)
        key = object_key.strip()
        if await doc_service.get(doc_id) is None:
            raise BizError(Err.IMP_OBJECT_KEY_MISMATCH,
                           f"对象键指向的知识单元不存在：{doc_id}")
        content_type = _content_type(key)
        local = LOCAL_STORE_DIR / key
        if local.is_file():
            return await asyncio.to_thread(local.read_bytes), content_type
        try:
            return await minio_store.get_bytes(key), content_type
        except Exception as exc:                            # noqa: BLE001
            raise BizError(Err.IMP_STORAGE_FAILED,
                           f"对象读取失败（key={key}）：{exc}") from exc

    # ------------------------------------------------------------------ 调度
    def submit(self, task_id: str) -> None:
        """把任务丢进后台（**立即返回**，Spec §3.1 R-15）。

        用 `asyncio.create_task` + 服务内的 `_running` 表：**必须拿住强引用**。
        `asyncio` 只持弱引用，不保存的话任务可能在 GC 时被回收——
        症状是"任务莫名其妙永远停在 pending"，且没有任何报错。
        """
        async def _runner() -> None:
            async with self.semaphore:                  # 排队期间 status=pending
                await self._run_pipeline(task_id)

        task = asyncio.create_task(_runner(), name=f"import:{task_id}")
        self._running[task_id] = task
        task.add_done_callback(lambda _: self._running.pop(task_id, None))

    async def shutdown(self) -> None:
        """关停：取消所有在跑任务（它们会走 `failed` 分支，不留僵尸任务）。"""
        pending = list(self._running.items())
        for task_id, task in pending:
            task.cancel()
            logger.warning("关停：导入任务 %s 已请求取消", task_id)
        if pending:
            await asyncio.gather(*[t for _, t in pending], return_exceptions=True)
        self._running.clear()

    # ------------------------------------------------------------------ 流水线
    async def _run_pipeline(self, task_id: str) -> None:
        """六阶段流水线主体。**异常全部在这里收口**，绝不外抛。

        外抛的后果很具体：`asyncio` 会把异常记进那个没人 await 的 Task，
        而库里的任务永远停在 `running`——只能等 watchdog 判超时。
        """
        task = await import_repo.get_task(task_id)
        if task is None:
            logger.error("导入任务 %s 不存在，流水线放弃", task_id)
            return
        if import_repo.is_terminal(str(task.get("status"))):
            logger.warning("导入任务 %s 已是终态 %s，跳过", task_id, task.get("status"))
            return
        run = _Pipeline(
            task_id=task_id, doc_id=str(task.get("doc_id")),
            file_hash=str(task.get("file_hash")), file_name=str(task.get("file_name")),
            file_ext=parser.normalize_ext(str(task.get("file_name"))),
            title=str(task.get("doc_title") or task.get("title") or ""),
            auto_enable=bool(task.get("auto_enable", True)),
            # 带上库里已有的耗时：`upload` 是请求内跑完的，它的耗时只在建任务时写过一次。
            # 不带的话流水线第一次 `_touch` 就会用新 dict **整体覆盖**掉它
            durations={str(k): int(v) for k, v in (task.get("durations") or {}).items()},
            from_stage=str(task.get("from_stage") or ImportTaskStage.PDF_TO_MD.value))
        try:
            await self._execute(run)
        except asyncio.CancelledError:
            await self._finalize_failure(run, Err.IMP_RESTART_INTERRUPTED,
                                         "任务在阶段边界被中断")
            raise
        except StageFailure as exc:
            await self._finalize_failure(run, exc.code, exc.message, stage=exc.stage)
        except BizError as exc:
            await self._finalize_failure(run, exc.spec, exc.message)
        except Exception as exc:                            # noqa: BLE001
            logger.exception("导入任务 %s 出现未捕获异常", task_id)
            await self._finalize_failure(run, Err.IMP_INTERNAL,
                                         f"未捕获异常：{type(exc).__name__}: {exc}")

    async def _execute(self, run: _Pipeline) -> None:
        """真正的六阶段。每阶段边界：① 写进度 ② 检查取消。"""
        run.workdir = Path(tempfile.mkdtemp(prefix=f"kbimport_{run.doc_id}_"))
        try:
            await self._stage_pdf_to_md(run)
            await self._stage_md_img(run)
            chunks = await self._stage_split(run)
            vectors = await self._stage_embedding(run, chunks)
            await self._stage_milvus(run, chunks, vectors)
            run.flush_stage()
            # 顺序要求见模块头 ②：切片先落库并**按 auto_enable 置启停**，再回填台账。
            # `mark_import_done` 内部会调 03 的启停同步（ER-02）
            await doc_service.mark_import_done(run.doc_id, len(chunks),
                                               _char_total(chunks),
                                               enable=run.auto_enable)
            await import_repo.complete_task(run.task_id, ts_ms=_now_ms(),
                                            durations=run.durations)
            await self._audit("system", "doc.import.done", run.doc_id, run.file_name,
                              {"task_id": run.task_id, "chunk_count": len(chunks),
                               "durations": run.durations, "parser": run.parser_name,
                               "auto_enable": run.auto_enable})
            logger.info("导入完成 %s：%d 片，耗时 %s", run.doc_id, len(chunks),
                        run.durations)
        finally:
            if run.workdir is not None:
                shutil.rmtree(run.workdir, ignore_errors=True)

    # ---- 阶段 2/6：格式归一化为 Markdown ----
    async def _stage_pdf_to_md(self, run: _Pipeline) -> None:
        run.mark(ImportTaskStage.PDF_TO_MD.value)
        if run.from_stage != ImportTaskStage.PDF_TO_MD.value and \
                await self._md_artifact_exists(run.doc_id):
            # 续跑：复用上次的 MD（**这就是重试能跳过解析的依据**，Spec §4.5）
            run.markdown = await self._read_md_artifact(run.doc_id)
            run.parser_name = "reused"
            await self._touch(run, ImportTaskStage.PDF_TO_MD.value,
                              STAGE_PROGRESS[ImportTaskStage.PDF_TO_MD.value][1])
            logger.info("文档 %s 续跑：复用已有 MD 产物（%d 字）",
                        run.doc_id, len(run.markdown))
            return

        await self._touch(run, ImportTaskStage.PDF_TO_MD.value,
                          STAGE_PROGRESS[ImportTaskStage.PDF_TO_MD.value][0] + 2)
        await doc_service.update_import_status(run.doc_id, ImportStatus.PARSING.value)
        data = await self._load_source(run)
        try:
            outcome = await parser.to_markdown(data=data, file_name=run.file_name,
                                               workdir=run.workdir or Path("."))
        except parser.ParseError as exc:
            raise StageFailure(Err.IMP_PARSE_FAILED, str(exc),
                               stage=ImportTaskStage.PDF_TO_MD.value) from exc
        except Exception as exc:                            # noqa: BLE001
            raise StageFailure(Err.IMP_PARSE_FAILED, f"解析异常：{exc}",
                               stage=ImportTaskStage.PDF_TO_MD.value) from exc
        for kind, detail in outcome.degradations:
            await import_repo.note_degradation(run.task_id, kind=kind, detail=detail,
                                               ts_ms=_now_ms())
        run.markdown = outcome.markdown
        run.images = outcome.images
        run.parser_name = outcome.parser
        run.char_count = outcome.char_count
        await self._touch(run, ImportTaskStage.PDF_TO_MD.value,
                          STAGE_PROGRESS[ImportTaskStage.PDF_TO_MD.value][1])

    # ---- 阶段 3/6：图片抽取入库 ----
    async def _stage_md_img(self, run: _Pipeline) -> None:
        run.mark(ImportTaskStage.MD_IMG.value)
        await self._touch(run, ImportTaskStage.MD_IMG.value,
                          STAGE_PROGRESS[ImportTaskStage.MD_IMG.value][0])
        mapping: dict[int, str] = {}
        for image in run.images:
            key = image.object_key(run.doc_id)
            try:
                await self._put_bytes(key, image.data, _content_type(key))
                mapping[image.seq] = await self._object_url(key)
            except Exception as exc:                        # noqa: BLE001
                # 单张图片失败**不阻断整个导入**：图片是"锦上添花"，
                # 为了一张图让一份 100 页的制度导入失败，代价明显不成比例。
                # 但要留痕，否则"图片怎么都没了"无法回答
                logger.warning("图片 %s 上传失败：%s", key, exc)
                await import_repo.note_degradation(
                    run.task_id, kind="minio_local",
                    detail=f"图片 {key} 上传失败：{exc}", ts_ms=_now_ms())
        run.markdown = parser.apply_image_urls(run.markdown, mapping)
        await self._write_md_artifact(run.doc_id, run.markdown)
        await self._touch(run, ImportTaskStage.MD_IMG.value,
                          STAGE_PROGRESS[ImportTaskStage.MD_IMG.value][1])

    # ---- 阶段 4/6：标题层级切分 ----
    async def _stage_split(self, run: _Pipeline) -> list[Any]:
        run.mark(ImportTaskStage.SPLIT.value)
        await self._touch(run, ImportTaskStage.SPLIT.value,
                          STAGE_PROGRESS[ImportTaskStage.SPLIT.value][0])
        doc = await doc_service.get(run.doc_id) or {}
        # `file_title` 是**建档时 E04.title 的快照**（§2.6）：改名不回写切片，
        # 所以这里取当前 title 生成新切片，但已有切片里的旧标题会保留——
        # 这是刻意的，回写全量切片的代价远大于收益
        file_title = str(doc.get("title") or run.title or run.file_name)
        chunks = split_markdown(run.markdown, file_title)
        if not chunks:
            # 零切片 = 下游什么都搜不到。**在这里失败**，不要让它变成
            # "导入成功但没有内容"（那种状态界面上是绿的，没人会去查）
            raise StageFailure(Err.IMP_PARSE_FAILED, "切分结果为空：文档没有可用正文",
                               stage=ImportTaskStage.SPLIT.value)
        run.chunks = chunks
        run.split_stats = split_stats(chunks)
        await self._touch(run, ImportTaskStage.SPLIT.value,
                          STAGE_PROGRESS[ImportTaskStage.SPLIT.value][1])
        logger.info("文档 %s 切分完成：%s", run.doc_id, run.split_stats)
        return chunks

    # ---- 阶段 5/6：向量化 ----
    async def _stage_embedding(self, run: _Pipeline, chunks: Sequence[Any]
                               ) -> list[list[float]]:
        run.mark(ImportTaskStage.EMBEDDING.value)
        await self._touch(run, ImportTaskStage.EMBEDDING.value,
                          STAGE_PROGRESS[ImportTaskStage.EMBEDDING.value][0])
        await doc_service.update_import_status(run.doc_id, ImportStatus.EMBEDDING.value)
        texts = [c.content_for_embedding for c in chunks]
        total = len(texts)
        batch = max(1, config_service.import_embed_batch_size)
        low, high = STAGE_PROGRESS[ImportTaskStage.EMBEDDING.value]
        vectors: list[list[float]] = []
        try:
            for start in range(0, total, batch):
                await self._check_cancel(run)
                # `to_thread`：BGE-M3 推理是同步阻塞调用，直接在事件循环里跑会让
                # 问答的 SSE 流式输出停摆（Spec §4.4 的第一条决策）
                part = await asyncio.to_thread(embedding_service.embed,
                                               texts[start:start + batch],
                                               batch_size=batch)
                vectors.extend(part)
                ratio = min(1.0, (start + len(part)) / total) if total else 1.0
                await self._touch(run, ImportTaskStage.EMBEDDING.value,
                                  int(low + (high - low - 1) * ratio))
        except EmbeddingUnavailable as exc:
            raise StageFailure(Err.IMP_EMBEDDING_FAILED, str(exc),
                               stage=ImportTaskStage.EMBEDDING.value) from exc
        except StageFailure:
            raise
        except Exception as exc:                            # noqa: BLE001
            raise StageFailure(Err.IMP_EMBEDDING_FAILED, f"向量化异常：{exc}",
                               stage=ImportTaskStage.EMBEDDING.value) from exc
        if embedding_service.degraded_reason:
            # GPU→CPU 降级必须留痕：否则"今天怎么比昨天慢十倍"只能靠猜
            await import_repo.note_degradation(
                run.task_id, kind="gpu_cpu",
                detail=f"向量化降级到 {embedding_service.device}："
                       f"{embedding_service.degraded_reason}", ts_ms=_now_ms())
        await self._touch(run, ImportTaskStage.EMBEDDING.value, high - 1)
        return vectors

    # ---- 阶段 6/6：写入向量库 ----
    async def _stage_milvus(self, run: _Pipeline, chunks: Sequence[Any],
                            vectors: Sequence[Sequence[float]]) -> None:
        run.mark(ImportTaskStage.MILVUS.value)
        await self._touch(run, ImportTaskStage.MILVUS.value,
                          STAGE_PROGRESS[ImportTaskStage.MILVUS.value][0])
        try:
            await chunk_store.ensure_collection()
            # 幂等重建（R-04）：先按 `doc_id` 删旧切片再插。
            # **必须在 insert 之前**——重试/重建时旧切片还在，
            # 不删就会同一片内容出现两份，检索命中重复、chunk_count 翻倍
            await chunk_store.drop_chunks_of(run.doc_id)
            written = await chunk_store.store_chunks(run.doc_id, chunks, vectors)
        except chunk_store.ChunkStoreError as exc:
            raise StageFailure(Err.IMP_VECTOR_STORE_FAILED, str(exc),
                               stage=ImportTaskStage.MILVUS.value) from exc
        except Exception as exc:                            # noqa: BLE001
            raise StageFailure(Err.IMP_VECTOR_STORE_FAILED, f"向量库写入异常：{exc}",
                               stage=ImportTaskStage.MILVUS.value) from exc
        if written == 0:
            # 写入 0 条**必须当失败**：`insert_chunks` 对空列表直接返回 0 且不报错，
            # 放过去就得到"文档 enabled、切片 0 条"的假可用态
            raise StageFailure(Err.IMP_VECTOR_STORE_FAILED, "向量库写入 0 条切片",
                               stage=ImportTaskStage.MILVUS.value)
        run.written = written
        await self._touch(run, ImportTaskStage.MILVUS.value,
                          STAGE_PROGRESS[ImportTaskStage.MILVUS.value][1])

    # ---- 失败收口 ----
    async def _finalize_failure(self, run: _Pipeline, spec: Any, message: str,
                                *, stage: str = "") -> None:
        """把失败写进任务与台账，并**保留中间产物**（重试要复用 MD）。

        取消（`cancel_requested`）在这里被翻译成 `cancelled` 终态，
        而不是 `failed`：两者对用户的意义完全不同——"被取消"是用户自己干的，
        "失败"是系统出问题了。混在一起会让"为什么这个文件老是失败"变成悬案。
        """
        run.flush_stage()
        current = stage or run._stage_name or ImportTaskStage.UPLOAD.value
        code = getattr(spec, "code", Err.IMP_INTERNAL.code)
        cancelled = await import_repo.is_cancel_requested(run.task_id)
        status = (ImportTaskStatus.CANCELLED.value if cancelled
                  else (ImportTaskStatus.TIMEOUT.value
                        if code == Err.IMP_TASK_TIMEOUT.code
                        else ImportTaskStatus.FAILED.value))
        await import_repo.finish_task(
            run.task_id, status=status, ts_ms=_now_ms(),
            error={"stage": current, "code": code, "message": message[:500]})
        await import_repo.update_task(run.task_id, {"durations": run.durations})
        try:
            await doc_service.mark_import_failed(run.doc_id, current, code,
                                                 message[:500])
        except Exception:                                   # noqa: BLE001
            logger.exception("失败回填台账出错 doc_id=%s", run.doc_id)
        if cancelled:
            # 已写入的切片保留但**置为不可检索**（Spec §3.5 R-04）：
            # 半截切片被召回会给出"引用了一篇没导完的文档"的答案
            await self.set_chunks_enabled(run.doc_id, False)
        await self._audit("system", "doc.import.fail", run.doc_id, run.file_name,
                          {"task_id": run.task_id, "code": code, "stage": current,
                           "message": message[:200], "cancelled": cancelled})
        logger.warning("导入%s %s stage=%s code=%s：%s",
                       "被取消" if cancelled else "失败", run.task_id, current, code,
                       message[:200])

    # ------------------------------------------------------------------ 存储小工具
    async def _spool(self, upload: Any, ext: str) -> tuple[Path, str, int, bytes]:
        """**流式**落临时文件 + 算 SHA256（Spec §3.1 R-08）。

        为什么不能 `await upload.read()` 一把梭：100MB 全读进内存，
        并发 2 就是 200MB 峰值，而"算哈希"这件事根本不需要整份内容在手。
        流式的另一好处是**边读边校验大小**，超限立刻停，不必先收完再拒绝。
        """
        digest = hashlib.sha256()
        size = 0
        head = b""
        declared = getattr(upload, "size", None)
        limit = config_service.import_max_file_mb * 1024 * 1024
        path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as handle:
                path = Path(handle.name)
                while True:
                    try:
                        chunk = await upload.read(CHUNK_READ)
                    except Exception as exc:                # noqa: BLE001
                        raise BizError(Err.IMP_UPLOAD_INCOMPLETE,
                                       f"上传流读取失败：{exc}") from exc
                    if not chunk:
                        break
                    if not head:
                        head = bytes(chunk[:8])
                    digest.update(chunk)
                    size += len(chunk)
                    handle.write(chunk)
                    if size > limit:
                        raise BizError(
                            Err.IMP_FILE_TOO_LARGE,
                            f"文件超过上限 {config_service.import_max_file_mb}MB")
        except BizError:
            if path is not None:
                path.unlink(missing_ok=True)
            raise
        if declared is not None and int(declared) > 0 and size < int(declared):
            assert path is not None
            path.unlink(missing_ok=True)
            raise BizError(Err.IMP_UPLOAD_INCOMPLETE,
                           f"实际收到 {size} 字节，少于声明的 {declared} 字节")
        assert path is not None
        return path, digest.hexdigest(), size, head

    async def _persist(self, object_key: str, temp_path: Path, ext: str
                       ) -> tuple[dict[str, Any], str | None]:
        """原文件落盘（R-11）：MinIO 优先，失败降级本地并**返回降级标记**。"""
        try:
            await minio_store.put_file(object_key, temp_path,
                                       content_type=_content_type(object_key))
            return {"bucket": settings.minio_bucket, "object_key": object_key,
                    "md_object_key": None, "storage_degraded": False}, None
        except Exception as exc:                            # noqa: BLE001
            logger.warning("MinIO 写入失败（%s），降级本地存储", exc)
        try:
            target = LOCAL_STORE_DIR / object_key
            target.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.copyfile, temp_path, target)
        except OSError as exc:
            raise BizError(Err.IMP_STORAGE_FAILED,
                           f"MinIO 与本地存储都不可写：{exc}") from exc
        return {"bucket": _LOCAL_BUCKET, "object_key": object_key,
                "md_object_key": None, "local_path": str(target),
                "storage_degraded": True}, "minio_local"

    async def _realign_object(self, storage: Mapping[str, Any], temp_path: Path,
                              ext: str, doc_id: str, file_name: str) -> None:
        """把落到错误 `doc_id` 前缀下的对象迁到正确前缀（并发复用改判时）。

        触发条件是"预分配的编号 ≠ 台账编号"，即同哈希并发创建被唯一索引改判为复用。
        不迁移的后果：台账指向 B 号，原文件在 A 号前缀下——**这份知识永远读不到自己的
        原文件**，预览、重试、重建全部失败，而且报错会指向"文件不存在"，
        让人去查存储而不是查这个竞态。
        """
        old_key = str(storage.get("object_key") or "")
        new_key = minio_store.object_key(doc_id, 0, file_name)
        logger.error("检测到编号错位（台账 %s ≠ 预分配），迁移对象 %s → %s",
                     doc_id, old_key, new_key)
        try:
            await self._persist(new_key, temp_path, ext)
            await self._rollback_object(storage)
            await doc_service.update_storage(
                doc_id, {"bucket": settings.minio_bucket, "object_key": new_key,
                         "md_object_key": None, "storage_degraded": False})
        except Exception as exc:                            # noqa: BLE001
            logger.critical("错位对象迁移失败（文档 %s 可能读不到原文件）：%s",
                            doc_id, exc)

    async def _load_source(self, run: _Pipeline) -> bytes:
        """取回原文件字节（解析阶段）。**从存储读**而不是内存缓存：

        缓存一份字节会让"服务重启后仍能重试"失效——重试是新进程里的新任务，
        内存里什么都没有。从存储读是唯一能同时满足"上传一次"和"可重试"的方式。
        """
        doc = await doc_service.get(run.doc_id) or {}
        storage = dict(doc.get("storage") or {})
        local_path = storage.get("local_path")
        if local_path and Path(str(local_path)).is_file():
            return await asyncio.to_thread(Path(str(local_path)).read_bytes)
        key = storage.get("object_key") or minio_store.object_key(run.doc_id, 0,
                                                                  run.file_name)
        try:
            return await minio_store.get_bytes(str(key))
        except Exception as exc:                            # noqa: BLE001
            raise StageFailure(
                Err.IMP_STORAGE_FAILED,
                f"原文件读取失败（key={key}）：{exc}",
                stage=ImportTaskStage.PDF_TO_MD.value) from exc

    async def _md_artifact_exists(self, doc_id: str) -> bool:
        """MD 中间产物是否还在（决定重试能否跳过解析阶段）。

        ⚠️ **先看本地再看 MinIO**：`_put_bytes` 在 MinIO 不可用时会落本地
        （降级路径），此时 `md_object_key` 里存的是**同一个对象键**，
        如果只查 MinIO 就会得出"产物不存在"的结论——重试白白重解析一次，
        而且日志里看不出原因。因为一次存储降级就把"续跑"这条优化废掉，是没必要的损失。
        """
        doc = await doc_service.get(doc_id) or {}
        key = dict(doc.get("storage") or {}).get("md_object_key")
        if key and str(key).startswith("local:"):
            return Path(str(key)[len("local:"):]).is_file()
        if key:
            if (LOCAL_STORE_DIR / str(key)).is_file():
                return True
            try:
                await minio_store.get_bytes(str(key))
                return True
            except Exception:                               # noqa: BLE001
                return False
        return (LOCAL_STORE_DIR / doc_id / MD_OBJECT_SUFFIX).is_file()

    async def _read_md_artifact(self, doc_id: str) -> str:
        """读 MD 中间产物（重试续跑用）。**本地优先**，理由同 `_md_artifact_exists`。"""
        doc = await doc_service.get(doc_id) or {}
        key = dict(doc.get("storage") or {}).get("md_object_key")
        if key and str(key).startswith("local:"):
            return await asyncio.to_thread(
                Path(str(key)[len("local:"):]).read_text, "utf-8")
        if key:
            local = LOCAL_STORE_DIR / str(key)
            if local.is_file():
                return await asyncio.to_thread(local.read_text, "utf-8")
            data = await minio_store.get_bytes(str(key))
            return data.decode("utf-8", errors="replace")
        return await asyncio.to_thread(
            (LOCAL_STORE_DIR / doc_id / MD_OBJECT_SUFFIX).read_text, "utf-8")

    async def _write_md_artifact(self, doc_id: str, markdown: str) -> None:
        """写 MD 中间产物（原文件之外的第二类 E03 对象）。

        写入**不阻断**导入：MD 产物只服务于"重试时跳过解析"这一条优化路径，
        写不进去最多是多解析一次；为它让整条导入失败是本末倒置。
        """
        key = f"{doc_id}/{MD_OBJECT_SUFFIX}"
        try:
            await self._put_bytes(key, markdown.encode("utf-8"), "text/markdown")
            current = await doc_service.get(doc_id) or {}
            await doc_service.update_storage(doc_id, {
                **dict(current.get("storage") or {}), "md_object_key": key})
        except Exception as exc:                            # noqa: BLE001
            logger.warning("MD 产物写入失败（不影响导入，重试时会重新解析）：%s", exc)

    async def _put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        """写字节对象：MinIO 优先，失败落本地（`local:` 前缀标记）。"""
        try:
            await minio_store.put_bytes(key, data, content_type=content_type)
            return
        except Exception as exc:                            # noqa: BLE001
            logger.warning("对象 %s 未写入 MinIO（%s），落本地", key, exc)
        target = LOCAL_STORE_DIR / key
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, data)

    async def _object_url(self, key: str) -> str:
        """对象的对外可读 URL。

        ⚠️ 桶是**私有**的（Spec §2.2），所以这里给的是**后端代理路径**而不是
        带签名的直链：签名会过期，而 MD 产物要长期保存，存进去的链接必须长期有效。
        前端渲染图片时走这个路径，由后端做鉴权 + 流转发。
        """
        return f"/api/v1/import/files/{key}"

    async def _rollback_object(self, storage: Mapping[str, Any]) -> None:
        """回滚已落盘对象（建台账/建任务失败时，避免桶里留孤儿文件）。"""
        key = storage.get("object_key")
        local = storage.get("local_path")
        bucket = storage.get("bucket")
        try:
            # 只对**真正的 MinIO 桶名**调 remove：降级路径的 bucket 是哨兵 `"LOCAL"`，
            # 拿它去调 MinIO 会报"桶不存在"，把一次成功的回滚变成一条吓人的错误日志
            if bucket and bucket != _LOCAL_BUCKET and key:
                await minio_store.remove(str(key))
            if local:
                Path(str(local)).unlink(missing_ok=True)
            logger.warning("已回滚落盘对象 %s", key)
        except Exception as exc:                            # noqa: BLE001
            logger.error("回滚对象 %s 失败（桶里可能留下孤儿文件）：%s", key, exc)

    async def _peek_doc_id(self) -> tuple[str, str]:
        """**预取**一个即将使用的 `doc_id` / `doc_no`（理由见 `DocService.create`）。"""
        from app.repositories import doc_repo

        return await doc_repo.next_doc_id(_now_ms())

    async def _create_task(self, *, doc_id: str, file_hash: str, file_name: str,
                           actor_id: str, batch_id: str | None, auto_enable: bool,
                           storage_degraded: bool = False, retry_of: str | None = None,
                           retry_count: int = 0,
                           from_stage: str = ImportTaskStage.PDF_TO_MD.value,
                           durations: Mapping[str, int] | None = None) -> str:
        """建 E06 任务（R-13）。"""
        now = _now_ms()
        task_id = await import_repo.next_task_id(now)
        doc = await doc_service.get(doc_id) or {}
        index = import_repo.stage_index(from_stage)
        await import_repo.insert_task({
            "_id": task_id, "doc_id": doc_id, "doc_title": doc.get("title"),
            "file_name": file_name, "file_hash": file_hash,
            "title": doc.get("title"),
            "status": ImportTaskStatus.PENDING.value,
            "stage": from_stage,
            "progress": STAGE_PROGRESS.get(from_stage, (0, 10))[0],
            # `upload` 不重做：从 `pdf_to_md` 起时 `done_stages` 已含 `upload`
            "done_stages": [s.value for s in ImportTaskStage
                            if import_repo.stage_index(s.value) < index],
            # 重试**不带**上次的耗时：那是上一次尝试的成绩，混进来会让
            # "这次卡在哪一步"看不出真实分布
            "durations": {str(k): int(v) for k, v in (durations or {}).items()},
            "error": None,
            "retry_of": retry_of, "retry_count": retry_count,
            "batch_id": batch_id, "auto_enable": auto_enable, "from_stage": from_stage,
            "storage_degraded": storage_degraded, "degraded": [],
            "cancel_requested": False, "cancel_reason": "",
            "created_by": actor_id, "created_at": now, "updated_at": now,
            "finished_at": None,
        })
        logger.info("导入任务已建 %s（文档 %s，从 %s 开始）", task_id, doc_id, from_stage)
        return task_id

    async def create_task_from_gap(self, *, doc_id: str, gap_id: str) -> str:
        """**08 的转建入口**：为一个缺口占位文档建"待上传"导入任务（ER-02）。

        任务停在 `stage=upload` / `progress=0`，**不进流水线**：
        原文件还没有（用户要先通过 `/import/upload` 把文件传上来），
        此时 `submit()` 会让流水线立刻在 `upload` 阶段失败。

        **幂等键是"同 doc_id 且 status ∈ {pending, running}"**（跨模块契约）：
        该文档已有在途任务就返回它，不重复建。08 的重复点击会被它自己的
        `GAP-3001` 拦住，但补偿重试路径可能走到这里，所以这层幂等仍需存在。
        """
        from app.infra.mongo import mongo

        existing = await mongo.collection(import_repo.IMPORT_TASKS).find_one(
            {"doc_id": doc_id,
             "status": {"$in": [ImportTaskStatus.PENDING.value,
                                ImportTaskStatus.RUNNING.value]}},
            sort=[("created_at", -1)])
        if existing is not None:
            logger.info("文档 %s 已有在途导入任务 %s，直接复用（缺口 %s）",
                        doc_id, existing["_id"], gap_id)
            return str(existing["_id"])

        now = _now_ms()
        task_id = await import_repo.next_task_id(now)
        doc = await doc_service.get(doc_id) or {}
        await import_repo.insert_task({
            "_id": task_id, "doc_id": doc_id, "doc_title": doc.get("title"),
            "file_name": doc.get("file_name"), "file_hash": doc.get("file_hash"),
            "title": doc.get("title"),
            "status": ImportTaskStatus.PENDING.value,
            # 停在 `upload`：**原文件还没上传**，进度 0 让界面显示"等待上传"
            "stage": ImportTaskStage.UPLOAD.value, "progress": 0,
            "done_stages": [], "durations": {}, "error": None,
            "retry_of": None, "retry_count": 0, "batch_id": None,
            "auto_enable": True,
            "from_stage": ImportTaskStage.UPLOAD.value,
            "storage_degraded": False, "degraded": [],
            "cancel_requested": False, "cancel_reason": "",
            "source_gap_id": gap_id,
            "created_by": "gap", "created_at": now, "updated_at": now,
            "finished_at": None,
        })
        logger.info("缺口 %s 的占位文档 %s 已建导入任务 %s（等待上传）",
                    gap_id, doc_id, task_id)
        return task_id

    async def _assert_not_importing(self, file_hash: str) -> None:
        """R-10：同哈希已有在途任务 → `IMP-3003`（防并发重复导入同一文件）。"""
        active = await import_repo.active_task_of_hash(file_hash)
        if active is not None:
            raise BizError(Err.IMP_HASH_IMPORTING,
                           f"该文件正在导入中（任务 {active.get('_id')}）")

    async def _assert_category(self, category_id: str) -> None:
        """R-06：分类必须存在（**只读**校验，分类的写入者是 03）。"""
        from app.repositories import doc_repo

        if await doc_repo.get_category(category_id) is None:
            raise BizError(Err.IMP_CATEGORY_NOT_FOUND, f"知识分类不存在：{category_id}")

    async def _check_cancel(self, run: _Pipeline) -> None:
        """阶段边界检查取消标志（协作式取消的落地处）。"""
        if await import_repo.is_cancel_requested(run.task_id):
            raise StageFailure(Err.IMP_TASK_STATE_INVALID, "任务已被取消",
                               stage=run._stage_name or ImportTaskStage.EMBEDDING.value)

    async def _touch(self, run: _Pipeline, stage: str, progress: int) -> None:
        """写一次阶段进度 + 检查取消。**每个阶段边界都走这里**（约束 5）。"""
        await import_repo.advance_stage(run.task_id, stage=stage, progress=progress,
                                        ts_ms=_now_ms(), durations=run.durations)
        await self._check_cancel(run)

    async def _titles_of(self, doc_ids: Sequence[str]) -> dict[str, str]:
        """批量取文档标题（导入队列的 `doc_title` 列）。"""
        if not doc_ids:
            return {}
        from app.repositories import doc_repo

        found = await doc_repo.find_by_ids(list(dict.fromkeys(doc_ids)))
        return {str(d.get("_id")): str(d.get("title") or "") for d in found}

    async def _audit(self, actor_id: str, action: str, target_id: str,
                     target_name: str, detail: Mapping[str, Any]) -> None:
        """写审计（ER-05：只能经 `AuditService`）。

        **不 try/except**：`AuditService.record()` 的契约是"永不抛异常"（DEC-10-1），
        自己再包一层只会掩盖"它真的抛了"这个更严重的问题（说明契约被破坏了）。
        审计服务不可用时它会自己降级到补偿文件，导入不受影响（AC-04-21）。
        """
        await audit_service.record(
            action=action, actor=actor_id or "system",
            actor_name=actor_id or "system", target_type="doc",
            target_id=target_id, target_name=target_name,
            after=dict(detail), outcome="success")


def _char_total(chunks: Sequence[Any]) -> int:
    """切片正文字符总数（回填 E04 `char_count`）。"""
    return sum(len(c.body) for c in chunks)


def _content_type(name: str) -> str:
    """按扩展名猜 MIME（MinIO 会把它写进对象的 Content-Type）。"""
    return _EXT_CONTENT_TYPE.get(Path(name).suffix.lower(),
                                 "application/octet-stream")


def _assert_page(page: int, page_size: int) -> None:
    """分页参数校验（Spec §3.4 R-01 / §3.7 R-05）。"""
    if page < 1:
        raise BizError(Err.IMP_QUERY_INVALID, f"page 必须是正整数：{page}")
    if not 1 <= page_size <= 200:
        raise BizError(Err.IMP_QUERY_INVALID, f"page_size 需在 1~200：{page_size}")


def _lookup_spec(code: str) -> Any:
    """按错误码字符串反查 `ErrorSpec`（批量"全拒"时回传第一条失败项的码）。

    查不到就退回 `IMP_INTERNAL`：**宁可报一个笼统但正确的码，也不要编一个**。
    编出来的码在错误码表里不存在，前端映射不到文案，用户看到的就是裸编号。
    """
    for name in dir(Err):
        if name.startswith("_"):
            continue
        value = getattr(Err, name)
        if getattr(value, "code", None) == code:
            return value
    return Err.IMP_INTERNAL


def _now_ms() -> int:
    """当前毫秒时间戳（全模块统一，避免各处的秒/毫秒混用）。"""
    return int(time.time() * 1000)


import_service = ImportService()

__all__ = [
    "ImportService", "import_service", "StageFailure", "UploadedItem", "RejectedItem",
    "STAGE_PROGRESS", "STAGE_TOTAL", "LOCAL_STORE_DIR", "MD_OBJECT_SUFFIX",
    "CHUNK_READ",
]
