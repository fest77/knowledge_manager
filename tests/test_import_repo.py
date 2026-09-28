# -*- coding: utf-8 -*-
"""模块 04 · E06 导入任务的仓储测试。

任务进度是本模块**唯一能脱离 Milvus/MinIO 单独验证**的部分，而且它承载着
两条容易出错的语义：**6 槽阶段固定**（不随文件类型变）与**僵尸任务自愈**
（进程被强杀后不能永远占着并发上限）。
"""
from __future__ import annotations

import time

import pytest

from app.core.enums import ImportTaskStage, ImportTaskStatus
from app.repositories import import_repo

pytestmark = pytest.mark.anyio

ACTOR = "U000001"


async def _new_task(doc_id: str = "DOC20260925000001", **overrides) -> str:
    ts = int(time.time() * 1000)
    task_id = await import_repo.next_task_id(ts)
    body = {
        "_id": task_id, "doc_id": doc_id, "file_name": "制度.pdf",
        "status": ImportTaskStatus.PENDING.value,
        "stage": ImportTaskStage.UPLOAD.value, "progress": 0, "done_stages": [],
        "degraded": [], "error": None, "created_by": ACTOR,
        "created_at": ts, "updated_at": ts, "finished_at": None,
    }
    body.update(overrides)
    await import_repo.insert_task(body)
    return task_id


def test_six_stages_are_fixed_and_ordered():
    """6 槽阶段：名字与顺序都固定，且与枚举声明顺序一致。"""
    assert import_repo.STAGES == ("upload", "pdf_to_md", "md_img", "split",
                                  "embedding", "milvus")
    assert import_repo.STAGES == tuple(s.value for s in ImportTaskStage)
    assert import_repo.stage_index("split") == 3
    assert import_repo.stage_index("不存在") == -1
    assert import_repo.stages_after("md_img") == ("split", "embedding", "milvus")


def test_terminal_statuses_and_done_stage_normalization():
    """终态判定 + `done_stages` 规范化（去重、按流水线顺序）。"""
    assert import_repo.is_terminal("succeeded") and import_repo.is_terminal("timeout")
    assert not import_repo.is_terminal("running")
    # 重试会重复经过同一阶段；规范化后既不重复、也不乱序
    assert import_repo.normalize_done_stages(
        ["split", "upload", "split", "md_img", "垃圾"]) == ["upload", "md_img", "split"]


async def test_task_id_is_sequential_within_a_day(client):
    """任务号 `IMP{日期}{6位}` 同日递增。"""
    first = await _new_task()
    second = await _new_task()
    assert first[:11] == second[:11]
    assert int(second[11:]) == int(first[11:]) + 1
    assert len(first) == 17 and first.startswith("IMP")


async def test_advance_stage_records_done_stages_and_progress(client):
    """推进阶段：写 `stage`/`progress`，并把**之前的**阶段记进 `done_stages`。"""
    task_id = await _new_task()
    await import_repo.advance_stage(task_id, stage="split", progress=45,
                                    ts_ms=int(time.time() * 1000))
    row = await import_repo.get_task(task_id)
    assert row["stage"] == "split" and row["progress"] == 45
    assert row["status"] == ImportTaskStatus.RUNNING.value
    assert row["done_stages"] == ["upload", "pdf_to_md", "md_img"]

    # 重试再经过一次 split：`$addToSet` 不该产生重复项
    await import_repo.advance_stage(task_id, stage="split", progress=50,
                                    ts_ms=int(time.time() * 1000))
    row = await import_repo.get_task(task_id)
    assert row["done_stages"] == ["upload", "pdf_to_md", "md_img"], "不能重复计入"


async def test_advance_stage_rejects_unknown_stage(client):
    """未知阶段直接抛错：阶段名是代码契约，不该让拼错的名字静默落库。"""
    task_id = await _new_task()
    with pytest.raises(ValueError):
        await import_repo.advance_stage(task_id, stage="parse", progress=10,
                                        ts_ms=int(time.time() * 1000))


async def test_progress_is_clamped(client):
    """`progress` 钳在 0~100：越界值会让前端的进度条画出屏幕。"""
    task_id = await _new_task()
    await import_repo.advance_stage(task_id, stage="milvus", progress=150,
                                    ts_ms=int(time.time() * 1000))
    assert (await import_repo.get_task(task_id))["progress"] == 100
    await import_repo.advance_stage(task_id, stage="upload", progress=-5,
                                    ts_ms=int(time.time() * 1000))
    assert (await import_repo.get_task(task_id))["progress"] == 0


async def test_finish_task_writes_terminal_state(client):
    """终态：`finished_at` 必写，成功时 `progress` 自动补到 100。"""
    task_id = await _new_task()
    await import_repo.finish_task(task_id, status=ImportTaskStatus.SUCCEEDED.value,
                                  ts_ms=int(time.time() * 1000))
    row = await import_repo.get_task(task_id)
    assert row["status"] == "succeeded" and row["progress"] == 100
    assert row["finished_at"] is not None and row["error"] is None

    failed_id = await _new_task()
    await import_repo.finish_task(failed_id, status=ImportTaskStatus.FAILED.value,
                                  ts_ms=int(time.time() * 1000),
                                  error={"stage": "pdf_to_md", "code": "IMP-4001",
                                         "message": "MinerU 解析失败"})
    row = await import_repo.get_task(failed_id)
    assert row["error"]["code"] == "IMP-4001" and row["error"]["stage"] == "pdf_to_md"

    with pytest.raises(ValueError):
        await import_repo.finish_task(failed_id, status="running",
                                      ts_ms=int(time.time() * 1000))


async def test_degradation_is_recorded_not_only_logged(client):
    """降级必须留痕：演示时"怎么证明它真的降了"靠的就是这条记录。"""
    task_id = await _new_task()
    ts = int(time.time() * 1000)
    await import_repo.note_degradation(task_id, kind="gpu_cpu",
                                       detail="CUDA 不可用，改用 CPU 推理", ts_ms=ts)
    await import_repo.note_degradation(task_id, kind="mineru_pdfplumber",
                                       detail="MinerU 超时，回退 pdfplumber", ts_ms=ts)
    row = await import_repo.get_task(task_id)
    kinds = sorted(d["kind"] for d in row["degraded"])
    assert kinds == ["gpu_cpu", "mineru_pdfplumber"]
    assert all(d["detail"] for d in row["degraded"])


async def test_list_and_count_running(client):
    """导入队列：按状态筛、按创建时间倒序、能数出"正在跑"的数量。"""
    done_id = await _new_task(doc_id="DOC00000000000001")
    await import_repo.finish_task(done_id, status=ImportTaskStatus.SUCCEEDED.value,
                                  ts_ms=int(time.time() * 1000))
    await _new_task(doc_id="DOC00000000000002")
    await _new_task(doc_id="DOC00000000000003")

    assert await import_repo.count_running() == 2
    running = await import_repo.list_tasks(status=ImportTaskStatus.PENDING.value)
    assert len(running) == 2
    assert all(t["status"] == "pending" for t in running)
    assert (await import_repo.list_tasks())[0]["created_at"] >= \
        (await import_repo.list_tasks())[-1]["created_at"], "倒序"


async def test_tasks_of_doc_groups_by_document(client):
    """同一文档的多次重试靠 `tasks_of_doc` 串起来（重试链）。"""
    doc_id = "DOC00000000000009"
    first = await _new_task(doc_id=doc_id)
    await import_repo.finish_task(first, status=ImportTaskStatus.FAILED.value,
                                  ts_ms=int(time.time() * 1000))
    second = await _new_task(doc_id=doc_id, retry_of=first)
    rows = await import_repo.tasks_of_doc(doc_id)
    assert len(rows) == 2
    assert rows[0]["_id"] == second, "最新的在前"


async def test_stale_running_tasks_are_reaped(client):
    """僵尸任务自愈：进程被强杀后，`running` 不能永远占着并发上限。

    不清理的后果很具体：导入队列永远挂着几条"正在跑"，并发上限被占满，
    **新的导入永远排不进去**，而界面显示的是"任务很多、都在跑"。

    启动补偿把 `pending` / `running` **无条件**改判为 `failed` + `IMP-5003`
    （Spec §4.5）。为什么码必须是 `IMP-5003` 而不是向量库那个 `IMP-4004`：
    错误码是排障入口，写成"向量库写入失败"会把"进程重启过"误导成"Milvus 挂了"。
    """
    stale = await _new_task(doc_id="DOC00000000000011")
    old_ts = int(time.time() * 1000) - 3600_000
    await import_repo.update_task(stale, {"updated_at": old_ts})
    fresh = await _new_task(doc_id="DOC00000000000012")

    now = int(time.time() * 1000)
    reaped = await import_repo.fail_stale_as_interrupted(ts_ms=now)
    # **两条都被改判**：启动补偿不看心跳，因为进程刚起来，
    # 它无法区分"刚挂上的新任务"与"早就死了的旧任务"——都交给管理员显式重试
    assert reaped == 2
    row = await import_repo.get_task(stale)
    assert row["status"] == ImportTaskStatus.FAILED.value
    assert row["error"]["code"] == "IMP-5003"
    assert (await import_repo.get_task(fresh))["status"] == \
        ImportTaskStatus.FAILED.value
    assert await import_repo.count_running() == 0, "不能用挂着的僵尸任务占满队列"


async def test_watchdog_reaps_only_overdue_tasks(client):
    """watchdog 只看心跳：超过阈值未推进的 `running` → `timeout` + `IMP-4006`。

    与启动补偿**刻意不同**：运行中扫到的超时任务可能只是很慢（不是坏了），
    判成"服务重启中断"会让人去查根本不存在的重启记录。
    """
    overdue = await _new_task(doc_id="DOC00000000000021")
    await import_repo.advance_stage(overdue, stage="split", progress=45,
                                    ts_ms=int(time.time() * 1000))
    await import_repo.update_task(overdue,
                                  {"updated_at": int(time.time() * 1000) - 3600_000})
    healthy = await _new_task(doc_id="DOC00000000000022")
    await import_repo.advance_stage(healthy, stage="split", progress=45,
                                    ts_ms=int(time.time() * 1000))

    now = int(time.time() * 1000)
    assert await import_repo.mark_overdue_as_timeout(before_ms=now - 600_000,
                                                     ts_ms=now) == 1
    row = await import_repo.get_task(overdue)
    assert row["status"] == ImportTaskStatus.TIMEOUT.value
    assert row["error"]["code"] == "IMP-4006"
    assert (await import_repo.get_task(healthy))["status"] == \
        ImportTaskStatus.RUNNING.value, "心跳新鲜的不动"


async def test_mark_stale_is_idempotent(client):
    """自愈幂等：第二次调用不该重复改判（否则每次重启都刷一遍）。"""
    task_id = await _new_task()
    old_ts = int(time.time() * 1000) - 3600_000
    await import_repo.update_task(task_id, {"updated_at": old_ts})
    now = int(time.time() * 1000)
    assert await import_repo.fail_stale_as_interrupted(ts_ms=now) == 1
    assert await import_repo.fail_stale_as_interrupted(ts_ms=now) == 0
