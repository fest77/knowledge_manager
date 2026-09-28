# -*- coding: utf-8 -*-
"""模块 04 的数据访问层：**E06 导入任务 `kb_import_tasks` 的唯一写入者**（ER-02）。

E03（MinIO 里的文件对象）没有 Mongo 集合——它的元信息就落在 E04 的 `storage` 字段里，
所以"文件对象"的读写入口是 `infra/minio.py`，不是本文件。

**为什么任务进度要落库而不是放内存**（AD-10 / Spec 第 6 条）：原型 `02` 的导入队列
要显示"第 3 个文件 62% · split 阶段"，而**刷新页面、甚至重启服务**之后这个进度不能丢。
放内存队列的话，一次重启就等于"所有正在导入的任务人间蒸发"——用户看到一个永远
停在 62% 的界面，却没有任何错误可查。

**6 槽阶段名是固定的**（Spec §2.1 的说明）：即使上传 `.txt`（不需要 PDF 转换、
也没有图片），流水线**仍然走完 6 个阶段名**，只是对应阶段耗时趋近 0。
理由：`stage` 是给**进度条与排障**用的稳定坐标系；如果阶段数随文件类型变化，
前端就得为每种格式写一套进度映射，"现在卡在哪一步"也没法用同一句话回答。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from app.core.enums import ImportTaskStage, ImportTaskStatus
from app.core.logging import logger
from app.infra.mongo import mongo

IMPORT_TASKS = "kb_import_tasks"
TASK_ID_WIDTH = 6

# 6 槽阶段（顺序即流水线顺序，`done_stages` 按它累加）
STAGES: tuple[str, ...] = tuple(s.value for s in ImportTaskStage)
# 终态：到了这几个就写 `finished_at`，且不再推进
TERMINAL_STATUSES: frozenset[str] = frozenset({
    ImportTaskStatus.SUCCEEDED.value, ImportTaskStatus.FAILED.value,
    ImportTaskStatus.TIMEOUT.value, ImportTaskStatus.CANCELLED.value,
})


async def ensure_indexes() -> None:
    """建 E06 的索引。

    - `status + created_at desc`：导入队列按状态查（"正在跑的有哪些"）
    - `doc_id`：由文档反查任务（03 展示"导入中"、04 重试）
    - `created_at` TTL：**这里刻意不建**——任务记录是排查依据，
      过期删除会让"上周那次失败为什么失败"永远查不到。
      清理走显式脚本，与 `audit_logs` 的永久保留同口径。
    """
    db = mongo.require_db()
    tasks = db[IMPORT_TASKS]
    await tasks.create_index([("status", 1), ("created_at", -1)], name="ix_status_created")
    await tasks.create_index("doc_id", name="ix_doc_id")
    # 重试链：同一文档可能被重试多次，靠 `retry_of` 串起来
    await tasks.create_index("retry_of", name="ix_retry_of", sparse=True)
    logger.info("导入任务索引已确保（%s）", IMPORT_TASKS)


async def next_task_id(ts_ms: int) -> str:
    """生成任务号 `IMP{yyyyMMdd}{6位序列}`（日期段与 `created_at` 同源）。

    与模块 03 的 `doc_id`、模块 10 的审计编号同一套做法：**先查当日最大值**
    再自增，避免进程重启后撞号（撞号会让"任务进度"串到另一个任务上，比报错更难查）。
    """
    date = datetime.fromtimestamp(ts_ms / 1000).strftime("%Y%m%d")
    prefix = f"IMP{date}"
    row = await mongo.collection(IMPORT_TASKS).find_one(
        {"_id": {"$regex": f"^{prefix}\\d{{{TASK_ID_WIDTH}}}$"}}, {"_id": 1},
        sort=[("_id", -1)])
    seq = int(str(row["_id"])[len(prefix):]) + 1 if row else 1
    return f"{prefix}{seq:0{TASK_ID_WIDTH}d}"


async def insert_task(doc: Mapping[str, Any]) -> str:
    """建任务（导入链路的第一步）。"""
    await mongo.collection(IMPORT_TASKS).insert_one(dict(doc))
    return str(doc["_id"])


async def get_task(task_id: str) -> dict[str, Any] | None:
    """按任务号取（`GET /import/tasks/{task_id}`）。"""
    return await mongo.collection(IMPORT_TASKS).find_one({"_id": task_id})


async def tasks_of_doc(doc_id: str) -> list[dict[str, Any]]:
    """某文档的全部任务（含历史重试），按创建时间倒序。"""
    cursor = mongo.collection(IMPORT_TASKS).find({"doc_id": doc_id}).sort("created_at", -1)
    return await cursor.to_list(length=None)


async def update_task(task_id: str, fields: Mapping[str, Any]) -> int:
    """更新任务字段。"""
    result = await mongo.collection(IMPORT_TASKS).update_one({"_id": task_id},
                                                            {"$set": dict(fields)})
    return result.modified_count


async def advance_stage(task_id: str, *, stage: str, progress: int,
                        ts_ms: int, durations: Mapping[str, int] | None = None) -> int:
    """推进到某个阶段：写 `stage` / `progress`，并把 `stage` 之前的阶段记入 `done_stages`。

    `$addToSet` 而不是 `$push`：流水线可能因为重试而**重复经过**同一个阶段，
    用 `push` 会让 `done_stages` 里出现重复项，前端算"已完成几步"就会算多。

    `durations` 是"各阶段耗时快照"（`{stage: ms}`）：它随阶段边界一起写，
    是为了让"这次导入卡在哪一步"能事后回答。**它在阶段开始时写的是上一阶段的耗时**，
    所以最后一次推进（milvus）必须由 `complete_task()` 补写——见那里的说明。
    """
    if stage not in STAGES:
        raise ValueError(f"未知阶段：{stage}（只允许 {list(STAGES)}）")
    index = STAGES.index(stage)
    finished = [s for s in STAGES[:index]]
    update: dict[str, Any] = {
        "$set": {"stage": stage, "progress": max(0, min(100, int(progress))),
                 "status": ImportTaskStatus.RUNNING.value, "updated_at": ts_ms},
    }
    if durations:
        update["$set"]["durations"] = {str(k): int(v) for k, v in durations.items()}
    if finished:
        update["$addToSet"] = {"done_stages": {"$each": finished}}
    result = await mongo.collection(IMPORT_TASKS).update_one({"_id": task_id}, update)
    return result.modified_count


async def complete_task(task_id: str, *, ts_ms: int, progress: int = 100,
                        durations: Mapping[str, int] | None = None) -> int:
    """把任务写成**成功终态**，并把 `done_stages` 一次补全为 6 个阶段。

    为什么不复用 `advance_stage("milvus", 100)` + `finish_task(succeeded)`：
    `advance_stage` 只把**当前阶段之前**的阶段记入 `done_stages`，
    所以走到 `milvus` 时 `done_stages` 里始终缺 `milvus` 自己——
    AC-04-05 明确要求"最终含全部 6 项"，前端进度条才会显示 6/6。
    两个写操作合成一个，也顺手消掉了"中间态被前端看到 5/6"的窗口。
    """
    result = await mongo.collection(IMPORT_TASKS).update_one(
        {"_id": task_id},
        {"$set": {"status": ImportTaskStatus.SUCCEEDED.value, "stage": STAGES[-1],
                  "progress": max(0, min(100, int(progress))),
                  "done_stages": list(STAGES), "error": None,
                  "finished_at": ts_ms, "updated_at": ts_ms,
                  **({"durations": {str(k): int(v) for k, v in durations.items()}}
                     if durations else {})}})
    return result.modified_count


async def finish_task(task_id: str, *, status: str, ts_ms: int,
                      progress: int | None = None,
                      error: Mapping[str, Any] | None = None) -> int:
    """写终态：`status` + `finished_at`（+ 失败时的 `error`）。"""
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"不是终态：{status}（只允许 {sorted(TERMINAL_STATUSES)}）")
    fields: dict[str, Any] = {"status": status, "finished_at": ts_ms,
                              "updated_at": ts_ms, "error": dict(error) if error else None}
    if progress is not None:
        fields["progress"] = max(0, min(100, int(progress)))
    elif status == ImportTaskStatus.SUCCEEDED.value:
        fields["progress"] = 100
    return await update_task(task_id, fields)


async def note_degradation(task_id: str, *, kind: str, detail: str, ts_ms: int) -> int:
    """记录一次降级（`minio_local` / `mineru_pdfplumber` / `rerank_skip` / `gpu_cpu`）。

    降级必须**留痕**而不是只写日志：演示时最容易被问"你说 GPU 不可用会自动降级，
    怎么证明它真的降了？"——`degraded[]` 就是答案。日志会滚掉，库里的记录不会。
    """
    result = await mongo.collection(IMPORT_TASKS).update_one(
        {"_id": task_id},
        {"$addToSet": {"degraded": {"kind": kind, "detail": detail, "at": ts_ms}},
         "$set": {"updated_at": ts_ms}})
    if result.modified_count:
        logger.warning("导入任务 %s 发生降级：%s（%s）", task_id, kind, detail)
    return result.modified_count


async def list_tasks(*, status: str | None = None, limit: int = 50
                     ) -> list[dict[str, Any]]:
    """导入队列（原型 `02` 的「导入队列」区块：最近 N 条，可按状态筛）。"""
    query: dict[str, Any] = {"status": status} if status else {}
    cursor = (mongo.collection(IMPORT_TASKS).find(query)
              .sort("created_at", -1).limit(limit))
    return await cursor.to_list(length=limit)


async def count_running() -> int:
    """正在跑的任务数（`/health` 与导入并发上限判据）。"""
    return await mongo.collection(IMPORT_TASKS).count_documents(
        {"status": {"$in": [ImportTaskStatus.PENDING.value,
                            ImportTaskStatus.RUNNING.value]}})


async def fail_stale_as_interrupted(*, ts_ms: int) -> int:
    """**启动补偿**：把库里遗留的 `pending` / `running` 任务改判为 `failed`（`IMP-5003`）。

    为什么需要它：进程被强杀时，内存里的流水线没了，但库里的任务还是 `running`。
    不清的话导入队列会永远挂着几条"正在跑"的僵尸任务，而且**并发上限会被它们占满**
    ——新的导入永远排不进去，界面却显示"任务很多、都在跑"。

    **为什么改判 `failed` 而不是自动重排**（Spec §4.5 / OQ-04-07）：重启瞬间自动把
    所有中断任务灌回去，会把 GPU 立刻打满，问答链路跟着劣化；而且中断原因未知
    （可能是 OOM），盲目重跑很可能再崩一次。交给管理员显式 `retry` 决定。

    ⚠️ 这里写 `IMP-5003`（"因服务重启而中断"）而不是别的码：错误码是**排障入口**，
    写成 `IMP-4004`（向量库写入失败）会把"重启"误导成"Milvus 挂了"，
    排查方向直接跑偏。
    """
    result = await mongo.collection(IMPORT_TASKS).update_many(
        {"status": {"$in": [ImportTaskStatus.PENDING.value,
                            ImportTaskStatus.RUNNING.value]}},
        {"$set": {"status": ImportTaskStatus.FAILED.value, "finished_at": ts_ms,
                  "updated_at": ts_ms,
                  "error": {"stage": None, "code": "IMP-5003",
                            "message": "服务重启导致任务中断；如需继续请显式重试"}}})
    if result.modified_count:
        logger.warning("启动补偿：%d 条中断任务已改判为 failed(IMP-5003)",
                       result.modified_count)
    return int(result.modified_count)


async def mark_overdue_as_timeout(*, before_ms: int, ts_ms: int) -> int:
    """**watchdog**：把长时间未推进的 `running` 任务改判为 `timeout`（`IMP-4006`）。

    判据是 `updated_at`（= 最后一次阶段推进的心跳）。与启动补偿的区别：

    | | 触发时机 | 判据 | 终态 |
    |---|---|---|---|
    | `fail_stale_as_interrupted` | 服务启动 | 无条件（进程刚重启） | `failed` + `IMP-5003` |
    | `mark_overdue_as_timeout` | 运行中定期 | 心跳超过 `import.timeout_min` | `timeout` + `IMP-4006` |

    两者**不能合并**：启动时并不知道任务是"刚挂上"还是"早就死了"，
    而运行中扫到的超时任务可能只是很慢（不是坏了）——把慢任务判成"服务重启中断"
    会让人去查根本不存在的重启记录。
    """
    result = await mongo.collection(IMPORT_TASKS).update_many(
        {"status": ImportTaskStatus.RUNNING.value, "updated_at": {"$lt": before_ms}},
        {"$set": {"status": ImportTaskStatus.TIMEOUT.value, "finished_at": ts_ms,
                  "updated_at": ts_ms,
                  "error": {"stage": None, "code": "IMP-4006",
                            "message": "任务长时间未推进，已判定超时"}}})
    if result.modified_count:
        logger.warning("watchdog：%d 条任务超过阈值未推进，已改判为 timeout",
                       result.modified_count)
    return int(result.modified_count)


async def request_cancel(task_id: str, *, ts_ms: int, reason: str = "") -> int:
    """**协作式取消**：只置 `cancel_requested=true`，由工作线程在阶段边界读它。

    `asyncio.to_thread` 无法安全强杀线程（Spec §3.5 R-03），所以取消**不保证立即生效**。
    把请求落在库里（而不是只放内存）有个额外好处：**换个进程也生效**
    ——单应用部署无所谓，但"导入在 A 进程、取消请求打到 B 进程"时只有落库才不会丢。
    """
    return await update_task(task_id, {"cancel_requested": True,
                                       "cancel_reason": reason[:200],
                                       "updated_at": ts_ms})


async def is_cancel_requested(task_id: str) -> bool:
    """读取消标志（工作线程在每个阶段边界调一次）。"""
    row = await mongo.collection(IMPORT_TASKS).find_one(
        {"_id": task_id}, {"cancel_requested": 1})
    return bool(row and row.get("cancel_requested"))


async def active_task_of_hash(file_hash: str) -> dict[str, Any] | None:
    """查"同哈希是否已有在途任务"（`IMP-3003`，Spec §4.3）。

    在 `kb_import_tasks` **单集合**内查：任务的 `file_hash` 是建构时冗余落库的
    （Spec §2.3），所以不必先查 `kb_documents` 再回表——那次回表既慢又可能与
    建台账产生竞态。
    """
    return await mongo.collection(IMPORT_TASKS).find_one(
        {"file_hash": file_hash,
         "status": {"$in": [ImportTaskStatus.PENDING.value,
                            ImportTaskStatus.RUNNING.value]}},
        sort=[("created_at", -1)])


async def drop_collection() -> None:
    """清空任务集合（仅测试路径调用）。"""
    await mongo.require_db().drop_collection(IMPORT_TASKS)


def stage_index(stage: str) -> int:
    """阶段在流水线中的序号（0~5）；未知阶段返回 -1。

    供前端进度条与"卡在哪一步"的文案共用——阶段顺序只在这一处定义。
    """
    return STAGES.index(stage) if stage in STAGES else -1


def is_terminal(status: str) -> bool:
    """是否终态（前端据此决定"还要不要轮询"）。"""
    return status in TERMINAL_STATUSES


def stages_after(stage: str) -> Sequence[str]:
    """某阶段之后还剩哪些阶段（`GET /import/tasks/{id}` 回给前端的"待办"）。"""
    index = stage_index(stage)
    return STAGES[index + 1:] if index >= 0 else STAGES


def normalize_done_stages(done: Iterable[str]) -> list[str]:
    """把 `done_stages` 规范成"按流水线顺序、去重"。

    前端直接按这个数组的长度算进度百分比；顺序错乱会让进度条视觉上往回跳。
    """
    seen = {s for s in done if s in STAGES}
    return [s for s in STAGES if s in seen]


__all__ = [
    "IMPORT_TASKS", "STAGES", "TERMINAL_STATUSES", "TASK_ID_WIDTH",
    "ensure_indexes", "next_task_id", "insert_task", "get_task", "tasks_of_doc",
    "update_task", "advance_stage", "complete_task", "finish_task", "note_degradation",
    "list_tasks", "count_running", "fail_stale_as_interrupted",
    "mark_overdue_as_timeout", "request_cancel", "is_cancel_requested",
    "active_task_of_hash", "drop_collection",
    "stage_index", "is_terminal", "stages_after", "normalize_done_stages",
]
