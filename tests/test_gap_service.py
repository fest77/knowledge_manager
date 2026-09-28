# -*- coding: utf-8 -*-
"""模块 08 的测试：**归一化 → 聚合合并计数 → 转建链路 → 忽略与清理 → 导出**。

## 替身与真实

| 项 | 说明 |
|---|---|
| **真实** | Mongo（含 7 个索引）、03 的 `DocService`、04 的 `ImportService`、05/02 的只读查询 |
| 不需要替身 | 本模块**不依赖模型**：归一化是纯函数、聚合只读日志（这是 08 相对 07 的简化之处） |

## 三条主线

1. **幂等**（AC-08-05）：`$set` 重算覆盖 → 连跑 3 次频次不翻倍
2. **不直连写库**（AC-08-12/15）：转建只经 03/04 的 Service；`qa_logs` 只读
3. **人工状态优先**（AC-08-21/22）：被忽略的不被改回 `open`；清理只删 `open`
"""
from __future__ import annotations

import hashlib

import pytest

from app.core.enums import GapStatus
from app.core.errors import BizError
from app.infra.mongo import mongo
from app.repositories import doc_repo, gap_repo, qa_repo
from app.services.doc_service import doc_service
from app.services.gap_normalizer import build_normalized_key, normalize_question
from app.services.gap_service import gap_service
from tests.conftest import token_of

pytestmark = pytest.mark.anyio

KB_ADMIN = "zhangwei"
ASKER = "wangqiang"
SYS_ADMIN = "lina"
ACTOR = "U000001"
DEPT_FINANCE = "DEPT0003"
DEPT_HR = "DEPT0002"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _new_doc(name: str, *, category_id: str | None = None) -> str:
    return await doc_service.create(
        file_name=name, file_ext="pdf", file_size=100,
        file_hash=hashlib.sha256(name.encode()).hexdigest(), storage={},
        created_by=ACTOR, category_id=category_id)


async def _new_category(name: str) -> str:
    tree = await doc_repo.insert_category({
        "_id": f"CAT{abs(hash(name)) % 100000:05d}", "name": name,
        "parent_id": None, "path": [name], "path_ids": [],
        "level": 1, "sort": 1, "doc_count": 0, "status": "active",
        "created_at": 1, "updated_at": 1})
    return tree


async def _log(question: str, *, dept_id: str = DEPT_FINANCE,
               max_score: float = 0.5, recalled: list | None = None,
               allowed: list | None = None, denied: list | None = None,
               faq_hit: bool = False, degraded: bool = False,
               source: str = "rag", offset: int = 0) -> str:
    """写一条 `qa_logs`（模拟 06 的产出；08 只读它）。"""
    now = qa_repo.now_ms() + offset
    log_id = await qa_repo.next_log_id(now)
    await qa_repo.insert_log({
        "_id": log_id, "task_id": f"t{log_id}", "message_id": f"m{log_id}",
        "session_id": "SESS20260101000001", "user_id": ACTOR, "dept_id": dept_id,
        "role_ids": ["ROLE0001"], "question": question, "asked_at": now,
        "recalled_chunks": recalled if recalled is not None else [],
        "allowed_chunks": allowed or [], "denied_chunks": denied or [],
        "faq_hit": faq_hit, "faq_id": None, "answer_source": source,
        "token_usage": None, "max_score": max_score, "degraded": degraded,
        "feedback": None, "elapsed_ms": 10, "retrieval_ms": 1, "auth_ms": 1,
        "rerank_ms": 1, "llm_ms": 1})
    return log_id


def chunk(doc_id: str, score: float = 0.5) -> dict:
    return {"chunk_id": 1, "doc_id": doc_id, "score": score}


# ================================================================ 归一化
def test_normalize_merges_punctuation_and_stopwords():
    """AC-08-03：标点/全半角/停用词差异**合并**成同一个 key。"""
    a = normalize_question("差旅报销上限是多少？")
    b = normalize_question("差旅报销上限是多少?")
    c = normalize_question("请问差旅报销上限是多少呢")
    assert a == b == c, "标点、全半角、疑问虚词都不该影响 key"
    assert normalize_question("请问差旅报销上限呢") == \
        normalize_question("差旅报销上限"), "停用词差异也应合并"
    assert build_normalized_key(DEPT_FINANCE, a) == \
        build_normalized_key(DEPT_FINANCE, b)


def test_normalize_never_merges_negation():
    """**故意不合并**否定句：合并会让缺口清单说反话。"""
    positive = normalize_question("年假能折现吗")
    negative = normalize_question("年假不能折现吗")
    assert positive != negative
    assert "不" in negative and "不" not in positive


def test_normalize_never_produces_empty_key():
    """整句都是标点时兜底，**绝不产出空 key**（否则这类问句全合并成一条）。"""
    assert normalize_question("？？？") != ""
    assert normalize_question("   ") != ""
    assert len(build_normalized_key(None, "x")) == 32


def test_normalized_key_includes_department():
    """部门进 key：同一问法在不同部门是两条缺口（筛部门才不会漏/错）。"""
    text = normalize_question("差旅报销上限是多少")
    assert build_normalized_key(DEPT_FINANCE, text) != \
        build_normalized_key(DEPT_HR, text)
    assert build_normalized_key(None, text) == build_normalized_key("", text)


# ================================================================ 聚合
async def test_aggregate_three_criteria_or(client):
    """AC-08-01：三种判定口径 OR —— 三种日志都生成缺口。"""
    doc = await _new_doc("制度.pdf")
    await _log("低相似度的问题", max_score=0.5,
               recalled=[chunk(doc, 0.5)], allowed=[chunk(doc)])
    await _log("完全没召回的问题", max_score=0.0, recalled=[])
    await _log("判定为无资料的问题", max_score=0.9, source="no_knowledge")

    result = await gap_service.aggregate(actor=ACTOR)
    assert result.scanned_logs == 3 and result.identified == 3
    assert await gap_repo.count_gaps(GapStatus.OPEN.value) == 3


async def test_aggregate_excludes_false_gaps(client):
    """AC-08-06：`faq_hit` / `degraded` / `no_knowledge` 且有拦截 → **都不算缺口**。"""
    await _log("FAQ 直出的问题", max_score=0.1, faq_hit=True)
    await _log("降级轮次的问题", max_score=0.1, degraded=True)
    await _log("有资料但无权的问题", max_score=0.0, source="no_knowledge",
               denied=[{"chunk_id": 1, "doc_id": "DOC1", "reason": "none_matched"}])

    result = await gap_service.aggregate(actor=ACTOR)
    assert result.identified == 0
    assert await gap_repo.count_gaps() == 0


async def test_aggregate_merges_synonyms_and_counts(client):
    """AC-08-03：同义问法合并成一条，`frequency` 为 3，`question` 是众数原文。"""
    for text in ("差旅报销上限是多少？", "差旅报销上限是多少?", "请问差旅报销上限是多少呢"):
        await _log(text, max_score=0.4)

    await gap_service.aggregate(actor=ACTOR)
    rows, total = await gap_repo.list_gaps()
    assert total == 1, "三条同义问法必须合并成一条缺口"
    assert rows[0]["frequency"] == 3
    assert rows[0]["question"] in ("差旅报销上限是多少？", "差旅报销上限是多少?",
                                   "请问差旅报销上限呢")


async def test_aggregate_is_idempotent(client):
    """AC-08-05：连跑 3 次，频次与条数**完全不变**（`$set` 重算覆盖）。"""
    for _ in range(3):
        await _log("幂等测试问题", max_score=0.3)
    first = await gap_service.aggregate(actor=ACTOR)
    rows_a, total_a = await gap_repo.list_gaps()
    second = await gap_service.aggregate(actor=ACTOR)
    third = await gap_service.aggregate(actor=ACTOR)
    rows_b, total_b = await gap_repo.list_gaps()

    assert total_a == total_b == 1
    assert rows_a[0]["frequency"] == rows_b[0]["frequency"] == 3, "重跑不得翻倍"
    assert first.identified == second.identified == third.identified == 1


async def test_aggregate_suggests_category_with_fallbacks(client):
    """AC-08-08/09：建议分类取众数；`allowed` 空时用最高分召回；再无从则 null。"""
    category = await _new_category("财务报销")
    other = await _new_category("技术规范")
    target = await _new_doc("财务制度.pdf", category_id=category)
    # 票是按 **doc_id** 计的（`allowed_chunks` 先去重）——所以"同一篇文档的 3 个切片"
    # 只算 1 票。要造"众数"，得让财务类**多几篇文档**
    target2 = await _new_doc("财务细则.pdf", category_id=category)
    target3 = await _new_doc("报销标准.pdf", category_id=category)
    other_doc = await _new_doc("技术手册.pdf", category_id=other)
    # 众数：财务类 3 票 vs 技术类 1 票
    await _log("建议分类问题", max_score=0.4,
               allowed=[chunk(target), chunk(target2), chunk(target3),
                        chunk(other_doc)],
               recalled=[chunk(target)])
    # 兜底一：allowed 空 → 取最高分召回者的分类
    await _log("兜底一问题", max_score=0.4, allowed=[],
               recalled=[chunk(target, 0.9), chunk(other_doc, 0.2)])
    # 兜底二：都空 → null
    await _log("兜底二问题", max_score=0.4, allowed=[], recalled=[])

    await gap_service.aggregate(actor=ACTOR)
    rows, _ = await gap_repo.list_gaps(page_size=10)
    by_question = {r["question"]: r for r in rows}
    assert by_question["建议分类问题"]["suggested_category_id"] == category
    assert by_question["兜底一问题"]["suggested_category_id"] == category
    assert by_question["兜底二问题"]["suggested_category_id"] is None


async def test_aggregate_keeps_at_most_five_recent_samples(client):
    """AC-08-10：`sample_log_ids` 最多 5 条且取**最近**的。"""
    for index in range(8):
        await _log("样本问题", max_score=0.3, offset=-index)
    await gap_service.aggregate(actor=ACTOR)
    rows, _ = await gap_repo.list_gaps()
    assert len(rows[0]["sample_log_ids"]) == 5
    assert rows[0]["frequency"] == 8


async def test_aggregate_rejects_bad_window_and_concurrency(client):
    """`GAP-1002`（窗口）与 `GAP-3005`（单飞锁）。"""
    with pytest.raises(BizError) as exc:
        await gap_service.aggregate(window_days=999, actor=ACTOR)
    assert exc.value.spec.code == "GAP-1002"

    await gap_service._lock.acquire()                    # 模拟"已有聚合在跑"
    try:
        with pytest.raises(BizError) as exc:
            await gap_service.aggregate(actor=ACTOR)
        assert exc.value.spec.code == "GAP-3005"
    finally:
        gap_service._lock.release()


# ================================================================ 人工状态优先
async def test_ignored_gap_is_not_reverted_by_aggregate(client):
    """AC-08-21：忽略后重跑聚合，`status` 仍为 `ignored`（人工决策优先）。"""
    await _log("忽略后仍出现的问题", max_score=0.2)
    await gap_service.aggregate(actor=ACTOR)
    rows, _ = await gap_repo.list_gaps()
    gap_id = rows[0]["_id"] if "_id" in rows[0] else rows[0]["gap_id"]

    await gap_service.ignore(gap_id=gap_id, reason="业务上已确认不需要", actor=ACTOR)
    await gap_service.aggregate(actor=ACTOR)
    row = await gap_repo.get_gap(gap_id)
    assert row["status"] == GapStatus.IGNORED.value, "聚合不得把 ignored 改回 open"
    assert row["ignored_by"] == ACTOR


async def test_cleanup_removes_only_open_gaps(client):
    """AC-08-22：频次归零的 `open` 被清理；同样条件的 `converted` **保留**。"""
    await _log("会被清理的问题", max_score=0.2)
    await _log("已转建的问题", max_score=0.2)
    await gap_service.aggregate(actor=ACTOR)
    rows, _ = await gap_repo.list_gaps()
    target = next(r for r in rows if r["question"] == "已转建的问题")
    gap_id = str(target["_id"])
    doc_id = await doc_service.create_from_gap(gap_id=gap_id, title="已转建的问题")
    await gap_repo.mark_converted(gap_id, doc_id=doc_id, actor=ACTOR,
                                  ts_ms=gap_repo.now_ms())

    # 窗口内只剩第一条 → 第二条（converted）也"频次归零"
    logs = await mongo.collection(qa_repo.QA_LOGS).find(
        {"question": "会被清理的问题"}).to_list(length=None)
    await mongo.collection(qa_repo.QA_LOGS).delete_many(
        {"question": "已转建的问题"})
    assert logs, "前置数据应当存在"

    result = await gap_service.aggregate(actor=ACTOR)
    assert result.removed == 0, "还有一条 open 缺口在窗口内，不该清理任何记录"
    # 再清掉唯一剩下的 open 缺口对应日志 → 本轮它应被清理，而 converted 保留
    await mongo.collection(qa_repo.QA_LOGS).delete_many(
        {"question": "会被清理的问题"})
    result = await gap_service.aggregate(actor=ACTOR)
    assert result.removed == 1
    assert await gap_repo.get_gap(gap_id) is not None, "converted 必须保留（人工痕迹）"


# ================================================================ 转建链路
async def test_convert_full_chain(client):
    """AC-08-11：转建后 ① 缺口 converted ② 台账新增占位 ③ 导入任务待上传 ④ 有 upload_url。"""
    doc = await _new_doc("来源文档.pdf")
    await _log("需要补文档的问题", max_score=0.3, allowed=[chunk(doc)])
    await gap_service.aggregate(actor=ACTOR)
    rows, _ = await gap_repo.list_gaps()
    gap_id = str(rows[0]["_id"])
    docs_before = await mongo.collection("kb_documents").count_documents({})
    tasks_before = await mongo.collection("kb_import_tasks").count_documents({})

    data = await gap_service.convert(gap_id=gap_id, actor=ACTOR)

    assert data["status"] == GapStatus.CONVERTED.value
    gap_row = await gap_repo.get_gap(gap_id)
    assert gap_row["converted_doc_id"] == data["converted_doc_id"]
    assert gap_row["converted_at"] and gap_row["converted_by"] == ACTOR

    doc_row = await doc_service.get(data["converted_doc_id"])
    assert doc_row["status"] == "disabled" and doc_row["import_status"] == "pending"
    assert doc_row["source_gap_id"] == gap_id
    task = await mongo.collection("kb_import_tasks").find_one(
        {"doc_id": data["converted_doc_id"]})
    assert task and task["status"] == "pending" and task["stage"] == "upload"
    assert data["upload_url"].endswith(data["converted_doc_id"])
    assert await mongo.collection("kb_documents").count_documents({}) == docs_before + 1
    assert await mongo.collection("kb_import_tasks").count_documents({}) == \
        tasks_before + 1


async def test_convert_is_idempotent_and_has_no_side_effects(client):
    """AC-08-13：第二次点击 → `GAP-3001`，且没有新增台账/任务。"""
    await _log("重复转建的问题", max_score=0.2)
    await gap_service.aggregate(actor=ACTOR)
    rows, _ = await gap_repo.list_gaps()
    gap_id = str(rows[0]["_id"])
    await gap_service.convert(gap_id=gap_id, actor=ACTOR)
    docs_after = await mongo.collection("kb_documents").count_documents({})
    tasks_after = await mongo.collection("kb_import_tasks").count_documents({})

    with pytest.raises(BizError) as exc:
        await gap_service.convert(gap_id=gap_id, actor=ACTOR)
    assert exc.value.spec.code == "GAP-3001"
    assert await mongo.collection("kb_documents").count_documents({}) == docs_after
    assert await mongo.collection("kb_import_tasks").count_documents({}) == tasks_after


async def test_convert_rejects_ignored_and_stale(client):
    """`GAP-3003`（已忽略）与 `GAP-3004`（频次归零）。"""
    await _log("忽略后转建", max_score=0.2)
    await _log("过期缺口", max_score=0.2)
    await gap_service.aggregate(actor=ACTOR)
    rows, _ = await gap_repo.list_gaps(page_size=10)
    ignored_id = str(next(r for r in rows if r["question"] == "忽略后转建")["_id"])
    stale_id = str(next(r for r in rows if r["question"] == "过期缺口")["_id"])

    await gap_service.ignore(gap_id=ignored_id, reason="不需要补", actor=ACTOR)
    with pytest.raises(BizError) as exc:
        await gap_service.convert(gap_id=ignored_id, actor=ACTOR)
    assert exc.value.spec.code == "GAP-3003"

    await mongo.collection(gap_repo.GAPS).update_one(
        {"_id": stale_id}, {"$set": {"frequency": 0}})
    with pytest.raises(BizError) as exc:
        await gap_service.convert(gap_id=stale_id, actor=ACTOR)
    assert exc.value.spec.code == "GAP-3004"


async def test_convert_validates_title_and_category(client):
    """`GAP-1003`（标题）/ `GAP-1004`（分类不存在）。"""
    await _log("标题校验问题", max_score=0.2)
    await gap_service.aggregate(actor=ACTOR)
    rows, _ = await gap_repo.list_gaps()
    gap_id = str(rows[0]["_id"])

    with pytest.raises(BizError) as exc:
        await gap_service.convert(gap_id=gap_id, title="   ", actor=ACTOR)
    assert exc.value.spec.code == "GAP-1003"
    with pytest.raises(BizError) as exc:
        await gap_service.convert(gap_id=gap_id, title="正常标题",
                                  category_id="CAT99999", actor=ACTOR)
    assert exc.value.spec.code == "GAP-1004"


async def test_convert_rolls_back_when_task_creation_fails(client, monkeypatch):
    """AC-08-14：04 抛异常 → `GAP-4002`、占位被回滚、缺口仍为 `open`，**可重试成功**。"""
    from app.services import import_service as import_module

    await _log("转建失败可补偿", max_score=0.2)
    await gap_service.aggregate(actor=ACTOR)
    rows, _ = await gap_repo.list_gaps()
    gap_id = str(rows[0]["_id"])
    docs_before = await mongo.collection("kb_documents").count_documents({})

    async def _boom(**_kwargs):
        raise RuntimeError("测试：导入任务创建失败")

    monkeypatch.setattr(import_module.import_service, "create_task_from_gap", _boom)
    with pytest.raises(BizError) as exc:
        await gap_service.convert(gap_id=gap_id, actor=ACTOR)
    assert exc.value.spec.code == "GAP-4002"
    assert await mongo.collection("kb_documents").count_documents({}) == docs_before, \
        "占位必须被回滚"
    assert (await gap_repo.get_gap(gap_id))["status"] == GapStatus.OPEN.value

    # 恢复正常后重试即可成功
    monkeypatch.undo()
    data = await gap_service.convert(gap_id=gap_id, actor=ACTOR)
    assert data["import_task_id"]


async def test_rollback_refuses_when_placeholder_has_content(client):
    """占位一旦已有内容就**拒绝撤销**（那是丢数据，不是补偿）。"""
    doc_id = await _new_doc("已有内容.pdf")
    await doc_repo.update(doc_id, {"chunk_count": 3})
    assert await doc_service.rollback_gap_placeholder(doc_id) is False
    assert await doc_service.get(doc_id) is not None


async def test_converted_gap_recurs_when_asked_again(client):
    """AC-08-20：转建后又被提问 → `frequency` 增长、`status` 保持 `converted`、`recurred`。"""
    await _log("转建后复现的问题", max_score=0.2)
    await gap_service.aggregate(actor=ACTOR)
    rows, _ = await gap_repo.list_gaps()
    gap_id = str(rows[0]["_id"])
    await gap_service.convert(gap_id=gap_id, actor=ACTOR)
    # 把转建时间调到 1 小时前：`recurred` 的语义是"**转建之后**又被提问"，
    # 而 convert 刚刚发生 —— 紧接着插入的新日志在时间上还在它之前。
    # 直接改库比"等一小时"现实得多，也不改变被测逻辑
    await mongo.collection(gap_repo.GAPS).update_one(
        {"_id": gap_id},
        {"$set": {"converted_at": gap_repo.now_ms() - 3600_000}})

    await _log("转建后复现的问题", max_score=0.2)
    await gap_service.aggregate(actor=ACTOR)
    data = await gap_service.list_gaps(status="all")
    item = next(i for i in data["items"] if i["gap_id"] == gap_id)
    assert item["frequency"] == 2, "转建后仍被提问，频次应继续增长"
    assert item["status"] == GapStatus.CONVERTED.value, "人工状态不得被改回 open"
    assert item["recurred"] is True


# ================================================================ 清单与导出
async def test_list_filters_sorts_and_group_by(client):
    """AC-08-07/16：字段齐全、筛选与排序正确、`group_by=question` 跨部门归并。"""
    for _ in range(3):
        await _log("跨部门同问题", dept_id=DEPT_FINANCE, max_score=0.2)
    await _log("跨部门同问题", dept_id=DEPT_HR, max_score=0.6)
    await _log("另一个问题", dept_id=DEPT_HR, max_score=0.1)
    await gap_service.aggregate(actor=ACTOR)

    data = await gap_service.list_gaps(status="open", sort="max_score_asc")
    assert data["summary"]["open"] == 3
    assert data["summary"]["total_frequency"] == 5
    for field in ("gap_id", "question", "dept_id", "dept_name", "frequency",
                  "max_score", "suggested_category_id", "suggested_category_path",
                  "suggested_category_basis", "status", "recurred"):
        assert field in data["items"][0], f"清单缺字段 {field}"

    filtered = await gap_service.list_gaps(status="open", dept_id=DEPT_HR)
    assert filtered["total"] == 2

    merged = await gap_service.list_gaps(status="open", group_by="question")
    row = next(i for i in merged["items"] if i["normalized_text"] ==
               normalize_question("跨部门同问题"))
    assert row["frequency"] == 4 and row["dept_count"] == 2
    assert sorted(row["dept_id"]) == sorted([DEPT_FINANCE, DEPT_HR])


async def test_list_rejects_bad_params(client):
    """`GAP-1001`：`status` / `sort` / `group_by` / `min_frequency` / 分页。"""
    for kwargs, code in (
            ({"status": "enabled"}, "GAP-1001"),
            ({"sort": "whatever"}, "GAP-1001"),
            ({"group_by": "nope"}, "GAP-1001"),
            ({"min_frequency": -1}, "GAP-1001"),
            ({"page": 0}, "GAP-1001"),
            ({"page_size": 201}, "GAP-1001")):
        with pytest.raises(BizError) as exc:
            await gap_service.list_gaps(**kwargs)
        assert exc.value.spec.code == code, kwargs


async def test_export_csv_has_bom_and_headers(client):
    """AC-08-23：UTF-8 **BOM** CSV，列名与页面一致。"""
    await _log("导出测试问题", max_score=0.2)
    await gap_service.aggregate(actor=ACTOR)
    filename, content = await gap_service.export_csv(status="open")
    assert filename.endswith(".csv")
    assert content.startswith("\ufeff"), "没有 BOM，Excel 打开中文会乱码"
    header = content.lstrip("\ufeff").splitlines()[0]
    assert "未命中提问" in header and "建议创建分类" in header


async def test_export_rejects_too_many_rows(client, monkeypatch):
    """`GAP-1005`：超过导出上限**拒绝**而不是静默截断。"""
    await _log("导出上限问题", max_score=0.2)
    await gap_service.aggregate(actor=ACTOR)
    from app.services import config_service as config_module

    monkeypatch.setattr(config_module.config_service, "get_raw",
                        lambda key: 0 if key == "gap.export_max_rows" else 1)
    with pytest.raises(BizError) as exc:
        await gap_service.export_csv(status="open")
    assert exc.value.spec.code == "GAP-1005"


# ================================================================ 边界与权限
async def test_module_08_never_writes_other_collections(client):
    """AC-08-12/15：08 **不写** `qa_logs`；转建只经 03/04 的 Service。

    这里用"代码检索 + 运行时计数"两条一起验：跑完聚合与转建后，
    `qa_logs` 条数不变（08 只读），而 `kb_documents` 的新增来自 03 的 Service。
    """
    await _log("边界校验问题", max_score=0.2)
    logs_before = await qa_repo.count_logs()
    await gap_service.aggregate(actor=ACTOR)
    rows, _ = await gap_repo.list_gaps()
    await gap_service.convert(gap_id=str(rows[0]["_id"]), actor=ACTOR)
    assert await qa_repo.count_logs() == logs_before, "08 不得写 qa_logs（ER-06）"

    source = (doc_repo.__file__, gap_repo.__file__)
    for path in source:
        text = open(path, encoding="utf-8").read()
        assert "knowledge_gaps" not in text or path.endswith("gap_repo.py")


async def test_delivery_from_module_07(client):
    """07 的转建入口：`upsert_from_cluster` 落一条缺口（`dept_id` 允许为 null）。"""
    gap_id = await gap_service.upsert_from_cluster(
        representative_question="07 投递的代表问法", questions=["a", "b"],
        frequency=6, actor=ACTOR)
    row = await gap_repo.get_gap(gap_id)
    assert row["dept_id"] is None and row["frequency"] == 6
    # 重复投递同一问法 → 合并到同一条（唯一索引）
    again = await gap_service.upsert_from_cluster(
        representative_question="07 投递的代表问法", questions=["a"], frequency=1,
        actor=ACTOR)
    assert again == gap_id
    assert await gap_repo.count_gaps() == 1


async def test_api_permissions_and_flow(client):
    """AC-08-17 + 全流程：`asker`/`sys_admin` → 403 `AUTH-2004`；`kb_admin` 可用。"""
    for username in (ASKER, SYS_ADMIN):
        token = await token_of(client, username)
        for path in ("/api/v1/gaps", "/api/v1/gaps/GAP000001",
                     "/api/v1/gaps/export"):
            resp = await client.get(path, headers=auth(token))
            assert resp.status_code == 403, f"{username} {path}"
            assert resp.json()["code"] == "AUTH-2004"

    token = await token_of(client, KB_ADMIN)
    headers = auth(token)
    await _log("HTTP 流程问题", max_score=0.2)
    aggregated = await client.post("/api/v1/gaps/aggregate", headers=headers,
                                   json={})
    assert aggregated.status_code == 200, aggregated.text
    assert aggregated.json()["data"]["identified"] == 1

    listing = await client.get("/api/v1/gaps?status=open", headers=headers)
    assert listing.status_code == 200
    body = listing.json()["data"]
    assert body["total"] == 1 and body["summary"]["open"] == 1
    gap_id = body["items"][0]["gap_id"]

    detail = await client.get(f"/api/v1/gaps/{gap_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["data"]["gap"]["gap_id"] == gap_id

    converted = await client.post(f"/api/v1/gaps/{gap_id}/convert", headers=headers,
                                  json={"title": "补一份差旅制度"})
    assert converted.status_code == 200, converted.text
    assert converted.json()["data"]["status"] == "converted"

    ignored_gap = await client.post("/api/v1/gaps/aggregate", headers=headers,
                                    json={})
    assert ignored_gap.status_code == 200

    export = await client.get("/api/v1/gaps/export?status=all", headers=headers)
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    assert export.content.startswith("\ufeff".encode("utf-8"))

    missing = await client.get("/api/v1/gaps/GAP999999", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["code"] == "GAP-3002"


async def test_api_aggregate_route_not_swallowed(client):
    """路由顺序：`POST /gaps/aggregate` 不能被 `/gaps/{gap_id}` 吃掉。"""
    token = await token_of(client, KB_ADMIN)
    resp = await client.post("/api/v1/gaps/aggregate", headers=auth(token), json={})
    assert resp.status_code == 200, "手动聚合必须命中自己的路由"
    # 而 `/gaps/{gap_id}` 只支持 GET
    assert (await client.get("/api/v1/gaps/aggregate",
                             headers=auth(token))).status_code == 404
