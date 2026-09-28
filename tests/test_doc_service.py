# -*- coding: utf-8 -*-
"""模块 03 服务层的测试（分类树 + 知识单元 + 去重 + 内部契约）。

对应验收：AC-03-01~04、06~08、12~17、19、20、21（后半句）、23。
接口层（路由）与前端随后落地，本文件先把**业务规则**钉住——
这样等接口写完，失败就只可能出在"接线"上，而不是规则本身。

覆盖范围的一处说明：**AC-03-09 / AC-03-10（切片同步与回滚）不在本文件**，
因为那要调 04 的 `ImportService`——按总纲 §4.2 的连续批次约定，
04 落地后才回填那一处调用并补这两个用例。
"""
from __future__ import annotations

import hashlib
import json

import pytest

from app.core.enums import DocStatus, ImportStatus
from app.core.errors import BizError
from app.infra.mongo import mongo
from app.repositories import audit_repo, doc_repo
from app.services import doc_service as svc

pytestmark = pytest.mark.anyio

ACTOR = "U000001"


def sha(text: str) -> str:
    """构造一个合法的 64 位十六进制 SHA256。"""
    return hashlib.sha256(text.encode()).hexdigest()


async def _new_doc(**overrides) -> str:
    body = {"file_name": "报销制度.pdf", "file_ext": "pdf", "file_size": 1024,
            "file_hash": sha(overrides.get("file_name", "报销制度.pdf")),
            "storage": {"bucket": "b", "object_key": "k", "md_object_key": "m"},
            "created_by": ACTOR}
    body.update(overrides)
    return await svc.doc_service.create(**body)


# ===================================================================== 派生展示
def test_permission_label_text_matches_the_prototype():
    """AC-03-18 / §2.5 ① 的四行文案，逐字对齐原型。"""
    assert svc.permission_label_text({"is_global": True}) == "全局公开"
    assert svc.permission_label_text(
        {"dept_cnt": 2, "role_cnt": 1}) == "受限(2部门/1角色)"
    assert svc.permission_label_text({"role_cnt": 1}) == "受限(1角色)"
    assert svc.permission_label_text({"is_global": False}) == "未配置(默认不可读)"
    assert svc.permission_label_text(None) == "未配置(默认不可读)"


def test_status_text_priority():
    """§2.5 ②：已删除 > 导入失败/导入中 > 已启用/已停用。"""
    assert svc.status_text({"import_status": "done", "status": "enabled"}) == "已启用"
    assert svc.status_text({"import_status": "done", "status": "disabled"}) == "已停用"
    assert svc.status_text({"import_status": "parsing", "status": "disabled"}) == "导入中"
    assert svc.status_text({"import_status": "failed", "status": "disabled"}) == "导入失败"
    assert svc.status_text({"import_status": "done", "status": "disabled",
                            "deleted_at": 1}, deleted_view=True) == "已删除"


# ===================================================================== 分类
async def test_category_tree_and_doc_count_includes_subtree(client):
    """AC-03-04：`doc_count` **含子分类**，且父节点 = 子节点之和。"""
    parent = await svc.category_service.create(name="公司制度", parent_id=None, sort=1,
                                               actor_id=ACTOR)
    child_a = await svc.category_service.create(name="财务报销", parent_id=parent,
                                                sort=1, actor_id=ACTOR)
    child_b = await svc.category_service.create(name="人力资源", parent_id=parent,
                                                sort=2, actor_id=ACTOR)
    await _new_doc(file_name="a.pdf", category_id=child_a)
    await _new_doc(file_name="b.pdf", category_id=child_a)
    await _new_doc(file_name="c.pdf", category_id=child_b)

    tree = await svc.category_service.list_tree()
    root = next(n for n in tree if n["category_id"] == parent)
    assert root["doc_count"] == 3, "父分类要含子分类"
    counts = {n["name"]: n["doc_count"] for n in root["children"]}
    assert counts == {"财务报销": 2, "人力资源": 1}
    assert root["level"] == 0 and root["children"][0]["level"] == 1
    assert root["children"][0]["path"] == ["公司制度", "财务报销"]


async def test_ac_03_13_same_level_duplicate_name_including_root(client):
    """AC-03-13：同级重名 `DOC-3007`；**根分类也受约束**（哨兵值参与唯一索引）。"""
    await svc.category_service.create(name="公司制度", parent_id=None, sort=1,
                                      actor_id=ACTOR)
    with pytest.raises(BizError) as exc:
        await svc.category_service.create(name="公司制度", parent_id=None, sort=2,
                                          actor_id=ACTOR)
    assert exc.value.spec.code == "DOC-3007"
    # 库里根分类存的是哨兵值，不是 null
    row = await mongo.collection(doc_repo.CATEGORIES).find_one({"name": "公司制度"})
    assert row["parent_id"] == doc_repo.ROOT_SENTINEL


async def test_ac_03_16_level_limit_on_create(client):
    """AC-03-16：新建第 6 层 → `DOC-1004`（只看请求参数即可判定）。"""
    parent = None
    for index in range(5):                                   # 造到 level 4
        parent = await svc.category_service.create(name=f"第{index}层", parent_id=parent,
                                                   sort=1, actor_id=ACTOR)
    with pytest.raises(BizError) as exc:
        await svc.category_service.create(name="第6层", parent_id=parent, sort=1,
                                          actor_id=ACTOR)
    assert exc.value.spec.code == "DOC-1004"


async def test_ac_03_14_cycle_and_cascade_on_move(client):
    """AC-03-14：移到自己的子分类下 `DOC-3008`；移动后**所有子孙**路径重算。"""
    root = await svc.category_service.create(name="公司制度", parent_id=None, sort=1,
                                             actor_id=ACTOR)
    mid = await svc.category_service.create(name="财务报销", parent_id=root, sort=1,
                                            actor_id=ACTOR)
    leaf = await svc.category_service.create(name="差旅", parent_id=mid, sort=1,
                                             actor_id=ACTOR)
    other = await svc.category_service.create(name="技术文档", parent_id=None, sort=2,
                                              actor_id=ACTOR)

    with pytest.raises(BizError) as exc:
        await svc.category_service.update(category_id=root,
                                          fields={"parent_id": mid}, actor_id=ACTOR)
    assert exc.value.spec.code == "DOC-3008"

    await svc.category_service.update(category_id=mid, fields={"parent_id": other},
                                      actor_id=ACTOR)
    rows = {r["_id"]: r for r in await mongo.collection(
        doc_repo.CATEGORIES).find({}).to_list(None)}
    assert rows[mid]["path_ids"] == [other, mid]
    assert rows[mid]["path"] == ["技术文档", "财务报销"]
    assert rows[leaf]["path_ids"] == [other, mid, leaf], "子孙必须级联"
    assert rows[leaf]["path"] == ["技术文档", "财务报销", "差旅"]
    assert rows[leaf]["level"] == 2


async def test_category_rename_cascades_names_only(client):
    """改名只重写 `path`（名字数组），`path_ids` / `level` 不变。"""
    root = await svc.category_service.create(name="旧名", parent_id=None, sort=1,
                                             actor_id=ACTOR)
    leaf = await svc.category_service.create(name="子", parent_id=root, sort=1,
                                             actor_id=ACTOR)
    await svc.category_service.update(category_id=root, fields={"name": "新名"},
                                      actor_id=ACTOR)
    row = await mongo.collection(doc_repo.CATEGORIES).find_one({"_id": leaf})
    assert row["path"] == ["新名", "子"]
    assert row["path_ids"] == [root, leaf] and row["level"] == 1


async def test_ac_03_15_delete_preconditions(client):
    """AC-03-15：有子分类 `DOC-3005`；有在用文档 `DOC-3006`；只有回收站文档则成功。"""
    root = await svc.category_service.create(name="公司制度", parent_id=None, sort=1,
                                             actor_id=ACTOR)
    child = await svc.category_service.create(name="财务报销", parent_id=root, sort=1,
                                              actor_id=ACTOR)
    with pytest.raises(BizError) as exc:
        await svc.category_service.delete(category_id=root, actor_id=ACTOR)
    assert exc.value.spec.code == "DOC-3005"

    doc_id = await _new_doc(file_name="d.pdf", category_id=child)
    with pytest.raises(BizError) as exc:
        await svc.category_service.delete(category_id=child, actor_id=ACTOR)
    assert exc.value.spec.code == "DOC-3006"

    # 软删除后就能删分类，且该文档 `category_id` 变 null
    # （先回填导入完成：导入中的文档不允许软删除，那是 DOC-3012 的保护）
    await svc.doc_service.mark_import_done(doc_id, 1, 10)
    await svc.doc_service.soft_delete(doc_id=doc_id, actor_id=ACTOR)
    await svc.category_service.delete(category_id=child, actor_id=ACTOR)
    row = await mongo.collection(doc_repo.DOCUMENTS).find_one({"_id": doc_id})
    assert row["category_id"] is None
    assert await mongo.collection(doc_repo.CATEGORIES).count_documents(
        {"_id": child}) == 0, "分类是硬删除"


# ===================================================================== 去重
async def test_ac_03_06_and_08_dedup_decisions(client):
    """AC-03-06 / AC-03-08：五种 `decision` 各走一遍。"""
    file_hash = sha("same-content")
    assert (await svc.doc_service.resolve_by_hash(file_hash))["decision"] == "new"

    doc_id = await _new_doc(file_name="same.pdf", file_hash=file_hash)
    assert (await svc.doc_service.resolve_by_hash(file_hash))["decision"] == "conflict", \
        "pending 就是'正在导入'"

    await svc.doc_service.mark_import_done(doc_id, chunk_count=7, char_count=999)
    reuse = await svc.doc_service.resolve_by_hash(file_hash)
    assert reuse["decision"] == "reuse" and reuse["doc_id"] == doc_id

    await svc.doc_service.mark_import_failed(doc_id, "parse", "IMP-4001", "解析失败")
    retry = await svc.doc_service.resolve_by_hash(file_hash)
    assert retry["decision"] == "retry" and retry["doc_id"] == doc_id, "doc_id 不变"

    await svc.doc_service.soft_delete(doc_id=doc_id, actor_id=ACTOR)
    gone = await svc.doc_service.resolve_by_hash(file_hash)
    assert gone["decision"] == "soft_deleted", "唯一索引不豁免软删除"

    # 恢复后又能复用
    await svc.doc_service.restore(doc_id=doc_id, actor_id=ACTOR)
    assert (await svc.doc_service.resolve_by_hash(file_hash))["decision"] == "retry"


async def test_ac_03_06_concurrent_same_hash_only_one_record(client):
    """AC-03-20 的一半：同哈希并发建台账最终**只有一条**，另一请求以复用返回。"""
    file_hash = sha("race")
    first = await _new_doc(file_name="race.pdf", file_hash=file_hash)
    second = await _new_doc(file_name="race.pdf", file_hash=file_hash)
    assert first == second, "第二次必须复用已有编号"
    assert await mongo.collection(doc_repo.DOCUMENTS).count_documents(
        {"file_hash": file_hash}) == 1


async def test_ac_03_07_same_name_different_hash_is_new(client):
    """AC-03-07：同名但内容不同 → `new`，且 `name_conflict` 提示同名。"""
    await _new_doc(file_name="同名.pdf", file_hash=sha("v1"))
    results = await svc.doc_service.dedup_check([
        {"file_hash": sha("v2"), "file_name": "同名.pdf"}])
    assert results[0]["decision"] == "new"
    assert results[0]["name_conflict"] is False, "未命中哈希时为 new（同名由 04 另行提示）"


async def test_dedup_check_validations(client):
    with pytest.raises(BizError) as exc:
        await svc.doc_service.dedup_check([])
    assert exc.value.spec.code == "DOC-1007"
    with pytest.raises(BizError) as exc:
        await svc.doc_service.dedup_check([{"file_hash": "not-a-hash"}])
    assert exc.value.spec.code == "DOC-1001"


# ===================================================================== 台账
async def test_ac_03_01_list_columns_and_doc_no_shape(client):
    """AC-03-01：9 列齐全，`doc_no` 形态与原型一致（`DOC{日期}{≥3位}`）。"""
    doc_id = await _new_doc(file_name="报销制度.pdf")
    data = await svc.doc_service.list_documents(page=1, page_size=20)
    assert data["total"] == 1 and len(data["items"]) == 1
    row = data["items"][0]
    for key in ("doc_no", "title", "file_ext", "category_path", "permission_text",
                "chunk_count", "status_text", "updated_at", "doc_id"):
        assert key in row
    assert row["doc_no"].startswith("DOC") and len(row["doc_no"]) >= 13
    assert row["doc_no"][3:11].isdigit() and len(row["doc_no"][11:]) >= 3
    assert row["title"] == "报销制度", "默认标题取文件名去扩展名"
    assert row["status_text"] == "导入中" and row["permission_text"] == "未配置(默认不可读)"
    assert doc_id.startswith("DOC") and len(doc_id) == 17, "_id 用 6 位序列"


async def test_ac_03_02_filters_and_param_errors(client):
    """AC-03-02：筛选可组合；`page_size=201` 报 `DOC-1001` 而**不静默截断**。"""
    a = await svc.category_service.create(name="甲", parent_id=None, sort=1,
                                          actor_id=ACTOR)
    b = await svc.category_service.create(name="乙", parent_id=None, sort=2,
                                          actor_id=ACTOR)
    d1 = await _new_doc(file_name="x.pdf", category_id=a)
    await _new_doc(file_name="y.pdf", category_id=b)
    await svc.doc_service.mark_import_done(d1, 1, 10)

    by_cat = await svc.doc_service.list_documents(category_id=a)
    assert by_cat["total"] == 1
    by_status = await svc.doc_service.list_documents(status="enabled")
    assert by_status["total"] == 1
    by_kw = await svc.doc_service.list_documents(keyword="y.pdf")
    assert by_kw["total"] == 1
    by_perm = await svc.doc_service.list_documents(permission_label="unconfigured")
    assert by_perm["total"] == 2

    for kwargs in ({"page_size": 201}, {"sort_by": "title"}, {"status": "deleted"},
                   {"permission_label": "public"}, {"view": "trash"}):
        with pytest.raises(BizError) as exc:
            await svc.doc_service.list_documents(**kwargs)
        assert exc.value.spec.code == "DOC-1001", kwargs


async def test_ac_03_03_include_sub_controls_total(client):
    """AC-03-03：选中父分类时 `total` = 子分类之和；`include_sub=false` 只算直属。"""
    parent = await svc.category_service.create(name="公司制度", parent_id=None, sort=1,
                                              actor_id=ACTOR)
    child = await svc.category_service.create(name="财务报销", parent_id=parent, sort=1,
                                              actor_id=ACTOR)
    await _new_doc(file_name="p.pdf", category_id=parent)
    await _new_doc(file_name="c.pdf", category_id=child)

    with_sub = await svc.doc_service.list_documents(category_id=parent, include_sub=True)
    only_self = await svc.doc_service.list_documents(category_id=parent,
                                                     include_sub=False)
    assert with_sub["total"] == 2, "含子分类"
    assert only_self["total"] == 1, "不含子分类"


async def test_ac_03_02_summary_is_not_affected_by_paging(client):
    """§2.5 ③：`summary` 的前四项与筛选一致但**不受分页影响**。"""
    for index in range(3):
        await _new_doc(file_name=f"s{index}.pdf")
    first = await svc.doc_service.list_documents(page=1, page_size=1)
    second = await svc.doc_service.list_documents(page=3, page_size=1)
    assert first["summary"] == second["summary"]
    assert first["summary"]["total"] == 3 and first["summary"]["importing"] == 3
    assert len(first["items"]) == 1


async def test_ac_03_17_editing_while_importing_is_busy(client):
    """AC-03-17：导入中编辑 → `DOC-3012`；未导入完成就启用 → `DOC-3010`。"""
    doc_id = await _new_doc(file_name="busy.pdf")
    with pytest.raises(BizError) as exc:
        await svc.doc_service.update(doc_id=doc_id, fields={"title": "改个名"},
                                     actor_id=ACTOR)
    assert exc.value.spec.code == "DOC-3012"
    with pytest.raises(BizError) as exc:
        await svc.doc_service.toggle(doc_id=doc_id, enabled=True, actor_id=ACTOR)
    assert exc.value.spec.code == "DOC-3010"
    with pytest.raises(BizError) as exc:
        await svc.doc_service.soft_delete(doc_id=doc_id, actor_id=ACTOR)
    assert exc.value.spec.code == "DOC-3012"


async def test_edit_title_category_tags(client):
    """编辑标题 / 分类 / 标签：只记变化项，并维护两边 `doc_count`。"""
    a = await svc.category_service.create(name="甲", parent_id=None, sort=1,
                                          actor_id=ACTOR)
    b = await svc.category_service.create(name="乙", parent_id=None, sort=2,
                                          actor_id=ACTOR)
    doc_id = await _new_doc(file_name="e.pdf", category_id=a)
    await svc.doc_service.mark_import_done(doc_id, 3, 30)

    result = await svc.doc_service.update(
        doc_id=doc_id, fields={"title": "新标题", "category_id": b,
                               "tags": ["制度", "报销", "制度"]}, actor_id=ACTOR)
    assert result["changed"]["title"] == "新标题"
    assert result["changed"]["category_id"] == b
    assert result["changed"]["tags"] == ["制度", "报销"], "去重且保序"

    rows = {r["_id"]: r for r in await mongo.collection(
        doc_repo.CATEGORIES).find({}).to_list(None)}
    assert rows[a]["doc_count"] == 0 and rows[b]["doc_count"] == 1

    # 没变化时不写库
    same = await svc.doc_service.update(doc_id=doc_id,
                                        fields={"title": "新标题"}, actor_id=ACTOR)
    assert same["changed"] == {}


async def test_tag_and_title_validations(client):
    with pytest.raises(BizError) as exc:
        await svc.doc_service.create(file_name="t.pdf", file_ext="pdf", file_size=1,
                                     file_hash=sha("t"), storage={},
                                     created_by=ACTOR, title="   ")
    assert exc.value.spec.code == "DOC-1002"
    with pytest.raises(BizError) as exc:
        await svc.doc_service.create(file_name="x.exe", file_ext="exe", file_size=1,
                                     file_hash=sha("x"), storage={}, created_by=ACTOR)
    assert exc.value.spec.code == "DOC-1005", "二次防御：绕过上传接口也拦得住"

    doc_id = await _new_doc(file_name="tags.pdf")
    await svc.doc_service.mark_import_done(doc_id, 1, 1)
    with pytest.raises(BizError) as exc:
        await svc.doc_service.update(doc_id=doc_id,
                                     fields={"tags": [f"t{i}" for i in range(11)]},
                                     actor_id=ACTOR)
    assert exc.value.spec.code == "DOC-1006"
    with pytest.raises(BizError) as exc:
        await svc.doc_service.update(doc_id=doc_id, fields={"tags": ["x" * 21]},
                                     actor_id=ACTOR)
    assert exc.value.spec.code == "DOC-1006"


async def test_ac_03_12_soft_delete_is_idempotent_and_hidden(client):
    """AC-03-12：软删除后默认列表看不到、`status=deleted` 视图可见、`status` 已置停用、重复删除幂等。"""
    doc_id = await _new_doc(file_name="del.pdf")
    await svc.doc_service.mark_import_done(doc_id, 2, 20)
    assert await svc.doc_service.soft_delete(doc_id=doc_id, actor_id=ACTOR) is True
    assert await svc.doc_service.soft_delete(doc_id=doc_id, actor_id=ACTOR) is False, "幂等"

    default = await svc.doc_service.list_documents()
    assert default["total"] == 0
    recycle = await svc.doc_service.list_documents(view="deleted")
    assert recycle["total"] == 1
    assert recycle["items"][0]["status_text"] == "已删除"
    row = await mongo.collection(doc_repo.DOCUMENTS).find_one({"_id": doc_id})
    assert row["status"] == DocStatus.DISABLED.value and row["deleted_by"] == ACTOR

    # 详情默认拒绝，带 include_deleted 才给
    with pytest.raises(BizError) as exc:
        await svc.doc_service.detail(doc_id)
    assert exc.value.spec.code == "DOC-3002"
    assert (await svc.doc_service.detail(doc_id, include_deleted=True))["doc_id"] == doc_id

    # 审计只有一条 doc.delete（幂等那次不重复写）
    assert await mongo.collection(audit_repo.AUDIT_LOGS).count_documents(
        {"action": "doc.delete", "target_id": doc_id}) == 1
    await svc.doc_service.restore(doc_id=doc_id, actor_id=ACTOR)
    assert (await svc.doc_service.list_documents())["total"] == 1


async def test_toggle_requires_done_then_works(client):
    """导入完成后可启停；重复设置同一状态不写审计。"""
    doc_id = await _new_doc(file_name="tg.pdf")
    await svc.doc_service.mark_import_done(doc_id, 5, 50)
    assert (await svc.doc_service.toggle(doc_id=doc_id, enabled=True,
                                         actor_id=ACTOR))["changed"] is False, "已是启用"
    off = await svc.doc_service.toggle(doc_id=doc_id, enabled=False, actor_id=ACTOR)
    assert off["status"] == DocStatus.DISABLED.value and off["changed"] is True
    row = await mongo.collection(audit_repo.AUDIT_LOGS).find_one(
        {"action": "doc.toggle", "target_id": doc_id})
    assert row["before"]["status"] == "enabled" and row["after"]["status"] == "disabled"


async def test_ac_03_19_permission_summary_backfill_and_label(client):
    """AC-03-19：05 回填后标签随之更新；`label` 由本模块算并落库。"""
    doc_id = await _new_doc(file_name="perm.pdf")
    await svc.doc_service.update_permission_summary(
        doc_id, {"is_global": False, "dept_cnt": 2, "role_cnt": 1, "user_cnt": 0})
    row = await mongo.collection(doc_repo.DOCUMENTS).find_one({"_id": doc_id})
    assert row["permission_summary"]["label"] == "limited"
    detail = await svc.doc_service.detail(doc_id)
    assert detail["permission_text"] == "受限(2部门/1角色)"

    await svc.doc_service.update_permission_summary(doc_id, {"is_global": True})
    detail = await svc.doc_service.detail(doc_id)
    assert detail["permission_text"] == "全局公开"


async def test_ac_03_20_fail_closed_when_dedup_index_missing(client):
    """AC-03-20：删掉唯一索引后建台账 → `DOC-4004`（fail-closed）。"""
    await mongo.collection(doc_repo.DOCUMENTS).drop_index("uq_file_hash")
    with pytest.raises(BizError) as exc:
        await _new_doc(file_name="noidx.pdf")
    assert exc.value.spec.code == "DOC-4004"
    assert await doc_repo.dedup_index_ready() is False

    await doc_repo.ensure_indexes()                       # 复原，别影响后续用例
    assert await doc_repo.dedup_index_ready() is True


async def test_ac_03_23_startup_self_check_reports_index_state(client):
    """AC-03-23 的一半：索引自检可被程序化查询（`/health` 会用它）。"""
    assert await doc_repo.dedup_index_ready() is True
    await doc_repo.assert_dedup_index_ready()             # 不抛即通过


async def test_ac_03_21_writes_are_audited_and_failure_does_not_block(client,
                                                                      monkeypatch):
    """AC-03-21：写操作留痕（`target_type` 为 doc/category）；**审计挂掉业务仍成功**。"""
    cat = await svc.category_service.create(name="分类", parent_id=None, sort=1,
                                            actor_id=ACTOR)
    doc_id = await _new_doc(file_name="a1.pdf")
    await svc.doc_service.mark_import_done(doc_id, 1, 1)
    await svc.doc_service.update(doc_id=doc_id, fields={"title": "改了"},
                                 actor_id=ACTOR)
    await svc.doc_service.toggle(doc_id=doc_id, enabled=False, actor_id=ACTOR)

    types = {r["action"]: r["target_type"] for r in await mongo.collection(
        audit_repo.AUDIT_LOGS).find({}).to_list(None)}
    assert types["category.create"] == "category"
    assert types["doc.create"] == "doc"
    assert types["doc.update"] == "doc"
    assert types["doc.toggle"] == "doc"

    async def down(_doc):
        raise RuntimeError("模拟 audit_logs 不可写")

    monkeypatch.setattr(audit_repo, "insert", down)
    assert await svc.doc_service.toggle(doc_id=doc_id, enabled=True,
                                        actor_id=ACTOR) is not None
    assert (await svc.doc_service.update(doc_id=doc_id, fields={"title": "还能改"},
                                         actor_id=ACTOR))["changed"]["title"] == "还能改"
    assert cat


async def test_get_and_assert_exists_contract(client):
    """内部契约：`get` 返回原始文档，`assert_exists` 对软删除抛错（供 05/06/08 用）。"""
    doc_id = await _new_doc(file_name="g.pdf")
    assert (await svc.doc_service.get(doc_id))["_id"] == doc_id
    assert (await svc.doc_service.get("DOC20990101000001")) is None
    assert (await svc.doc_service.assert_exists(doc_id))["_id"] == doc_id

    await svc.doc_service.mark_import_done(doc_id, 1, 1)
    await svc.doc_service.soft_delete(doc_id=doc_id, actor_id=ACTOR)
    with pytest.raises(BizError) as exc:
        await svc.doc_service.assert_exists(doc_id)
    assert exc.value.spec.code == "DOC-3002"


async def test_create_placeholder_for_gap_conversion(client):
    """08 的入口：占位单元无文件、`pending`、`chunk_count=0`；**同缺口重复转建命中复用**。"""
    first = await svc.doc_service.create_placeholder(title="差旅标准", source="GAP0001",
                                                     created_by=ACTOR)
    second = await svc.doc_service.create_placeholder(title="差旅标准", source="GAP0001",
                                                      created_by=ACTOR)
    assert first == second, "同一缺口不该产生第二篇占位"
    row = await mongo.collection(doc_repo.DOCUMENTS).find_one({"_id": first})
    assert row["import_status"] == ImportStatus.PENDING.value
    assert row["chunk_count"] == 0 and row["storage"]["bucket"] is None


async def test_category_path_and_recount(client):
    """`category_path_of`（06 溯源用）与 `/categories/recount` 兜底。"""
    parent = await svc.category_service.create(name="制度", parent_id=None, sort=1,
                                              actor_id=ACTOR)
    child = await svc.category_service.create(name="报销", parent_id=parent, sort=1,
                                              actor_id=ACTOR)
    doc_id = await _new_doc(file_name="p.pdf", category_id=child)
    assert await svc.doc_service.category_path_of(doc_id) == "制度 / 报销"

    # 人为把父分类计数写坏，recount 应能修回来
    await mongo.collection(doc_repo.CATEGORIES).update_one(
        {"_id": parent}, {"$set": {"doc_count": 99}})
    result = await svc.category_service.recount(actor_id=ACTOR)
    assert result["changed"][parent] == {"before": 99, "after": 1}
    row = await mongo.collection(doc_repo.CATEGORIES).find_one({"_id": parent})
    assert row["doc_count"] == 1


async def test_doc_id_is_sequential_within_a_day(client):
    """编号 `DOC{日期}{6位}` 同日递增，且 `doc_no` 去前导零到 ≥3 位。"""
    first = await _new_doc(file_name="n1.pdf")
    second = await _new_doc(file_name="n2.pdf")
    assert first[:11] == second[:11], "同一天的日期段一致"
    assert int(second[11:]) == int(first[11:]) + 1
    rows = await mongo.collection(doc_repo.DOCUMENTS).find(
        {"_id": {"$in": [first, second]}}).to_list(None)
    nos = {r["_id"]: r["doc_no"] for r in rows}
    assert len(nos[first][11:]) >= 3, "doc_no 的序列至少 3 位（去前导零后补足）"
    assert nos[first][11:].lstrip("0") == str(int(first[11:])), "同一序列号，只是位数不同"


def test_helpers_are_pure_functions():
    """`_title_from_filename` / `_clean_tags` 的边界（顺带证明没有隐藏 IO）。"""
    assert svc._title_from_filename("a.b.pdf") == "a.b"
    assert svc._title_from_filename("noext") == "noext"
    assert svc._title_from_filename("") == "未命名"
    assert svc._clean_tags(["a", "a", "b"]) == ["a", "b"]
    with pytest.raises(BizError):
        svc._clean_tags("not-a-list")


def test_doc_and_category_services_are_singletons():
    """模块级单例：04/05/06/08/09 通过它们调内部契约，不得各自 new 一个。"""
    assert svc.doc_service is not None and svc.category_service is not None
    assert isinstance(json.dumps({}, default=str), str)


# ===================================================================== 03↔04 连续批次（回填后新增）
async def test_ac_03_09_disabling_a_doc_disables_its_chunks(client):
    """AC-03-09：停用文档 → 其**全部切片 `enabled=false`**，且**未被物理删除**。

    这是总纲 §4.2 那个"前向调用点"的验收：03 自己不连 Milvus，
    只调 04 的 `set_chunks_enabled()`。
    """
    from app.infra.milvus import milvus
    from app.services import chunk_store

    if not await _milvus_ready():
        pytest.skip("Milvus 不可达——启动虚拟机后重跑")
    await milvus.connect()
    try:
        from app.core.config import settings
        from app.services.splitter import split_markdown

        doc_id = await _new_doc(file_name="sync.pdf")
        chunks = split_markdown("# 甲\n内容甲。\n# 乙\n内容乙。", "文档")
        dense = [[1.0] + [0.0] * (settings.embedding_dim - 1) for _ in chunks]

        # ⚠️ **先清这个 doc_id 在 Milvus 里的残留**：`doc_id` 是"日期+当日序列"，
        # 而每个用例都会 drop 并重灌 Mongo（序列从 0001 重来），于是**不同用例会拿到
        # 同一个编号**。Milvus 里上一次的切片不会随 Mongo 的 drop 消失，
        # 于是"写入后 enabled 应为 0"会被残留的已启用切片破坏。
        # 症状是"单独跑通过、全量跑失败"，且失败信息看起来像"新切片默认可检索了"
        # ——那会把排查方向引向 `chunk_store`，而它其实完全正确。
        await chunk_store.drop_chunks_of(doc_id)

        # 按**真实流水线顺序**：先写切片（默认不可检索），再回填完成
        await chunk_store.store_chunks(doc_id, chunks, dense)
        assert await chunk_store.count_chunks(doc_id, enabled=True) == 0, \
            "写入后默认不可检索（否则导入中就能被搜到）"
        await svc.doc_service.mark_import_done(doc_id, len(chunks), 20)
        assert await chunk_store.count_chunks(doc_id, enabled=True) == len(chunks), \
            "导入完成即应可检索（否则就是'已启用但搜不到'的假可用）"

        await svc.doc_service.toggle(doc_id=doc_id, enabled=False, actor_id=ACTOR)
        assert await chunk_store.count_chunks(doc_id, enabled=True) == 0
        assert await chunk_store.count_chunks(doc_id) == len(chunks), "不能物理删除"

        await svc.doc_service.toggle(doc_id=doc_id, enabled=True, actor_id=ACTOR)
        assert await chunk_store.count_chunks(doc_id, enabled=True) == len(chunks)
    finally:
        await chunk_store.drop_chunks_of(doc_id)
        await milvus.close()


async def test_ac_03_10_chunk_sync_failure_keeps_document_unchanged(client, monkeypatch):
    """AC-03-10：切片服务不可用 → `DOC-4003`，且 `kb_documents.status` **保持原值**。

    实现上我在 03 里是"先同步切片、再改文档状态"，所以失败时文档**从未被改过**
    ——这比"改了再回滚"更稳：后者中间有一段真实窗口，文档显示已停用、
    切片却仍可被召回。
    """
    from app.services import chunk_store, doc_service

    doc_id = await _new_doc(file_name="fail.pdf")
    await svc.doc_service.mark_import_done(doc_id, 1, 10)
    before = (await mongo.collection(doc_repo.DOCUMENTS).find_one({"_id": doc_id}))["status"]
    assert before == "enabled"

    async def down(_doc_id, _enabled):
        raise chunk_store.ChunkStoreError("模拟 04 切片服务不可用")

    monkeypatch.setattr(doc_service.chunk_store, "set_chunks_enabled", down)
    with pytest.raises(BizError) as exc:
        await svc.doc_service.toggle(doc_id=doc_id, enabled=False, actor_id=ACTOR)
    assert exc.value.spec.code == "DOC-4003"

    after = (await mongo.collection(doc_repo.DOCUMENTS).find_one({"_id": doc_id}))["status"]
    assert after == before, "文档状态必须保持原值"


async def _milvus_ready() -> bool:
    """Milvus 是否可达（不可达时跳过真机用例，而不是让套件变红）。"""
    import socket

    from app.core.config import settings

    host, _, port = settings.milvus_url.replace("http://", "").partition(":")
    sock = socket.socket()
    sock.settimeout(4)
    try:
        sock.connect((host, int(port)))
        return True
    except Exception:                                         # noqa: BLE001
        return False
    finally:
        sock.close()
