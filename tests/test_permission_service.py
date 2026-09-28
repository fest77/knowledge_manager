# -*- coding: utf-8 -*-
"""模块 05 的判定内核与配置服务测试。

分三层测，各有明确目的：

| 层 | 测什么 | 为什么单独一层 |
|---|---|---|
| **判定内核**（`evaluate_record`，纯函数） | OR 逻辑、精确匹配、默认拒绝 | 不碰数据库 → 能穷举四维组合，跑得飞快。**判定逻辑只有这一处实现（ER-03）** |
| **服务层**（`PermissionService`） | fail-closed、缓存、批量一次 `$in`、保存链路 | 这些是"多组件协作"的正确性，单元测不出 |
| **配置读**（`get_config`） | 无记录返回默认值 + `version=0`、ID 回显 | 前端弹窗直接依赖这个形状 |

`evaluate_record()` 的入参故意设计成"任何带三个字段的对象"（见 `Subject`），
所以这里用一个 3 行的 `Subject` 就能构造用户，不必拉整套登录链路。
"""
from __future__ import annotations

import hashlib

import pytest

from app.core.errors import BizError
from app.infra.mongo import mongo
from app.repositories import perm_repo
from app.services import permission_service as ps
from app.services.doc_service import doc_service
from app.services.perm_cache import perm_cache

pytestmark = pytest.mark.anyio

ACTOR = "U000001"
# 种子里的真实数据（`scripts/seed.py`）：财务部 / 报销组（财务部的子部门）
DEPT_FINANCE = "DEPT0003"
DEPT_REIMBURSE = "DEPT0008"          # 「报销组」，挂在财务部下
DEPT_HR = "DEPT0002"
ROLE_KB_ADMIN = "ROLE0002"            # kb_admin
ROLE_ASKER = "ROLE0001"               # asker
ROLE_MANAGEMENT = "ROLE0004"          # 业务角色「管理层」（无功能权限，专作四维分组）


@pytest.fixture(autouse=True)
def _clean_cache():
    """每个用例前后都清判定缓存：它是进程级单例，会跨用例存活。"""
    perm_cache.reset()
    yield
    perm_cache.reset()


def user(user_id: str = "U000003", dept_id: str = "", roles=()) -> ps.Subject:
    """构造一个判定用的用户快照。"""
    return ps.Subject(user_id=user_id, dept_id=dept_id,
                      role_ids=frozenset(roles))


def record(**overrides) -> dict:
    """构造一条 E07 记录（默认：全员不可读）。"""
    base = {"doc_id": "DOC1", "is_global": False, "departments": [], "roles": [],
            "users": [], "version": 1}
    base.update(overrides)
    return base


async def _new_doc(file_name: str = "制度.pdf") -> str:
    return await doc_service.create(
        file_name=file_name, file_ext="pdf", file_size=100,
        file_hash=hashlib.sha256(file_name.encode()).hexdigest(), storage={},
        created_by=ACTOR)


# ================================================================ 判定内核（纯函数）
def test_or_logic_four_dimensions_each_allow():
    """AC-05-01：四维**各自单独**都能放行（OR 而非 AND）。"""
    assert ps.evaluate_record(user(dept_id=DEPT_HR), record(is_global=True)) == \
        (True, ps.REASON_GLOBAL)
    assert ps.evaluate_record(user(dept_id=DEPT_FINANCE),
                              record(departments=[DEPT_FINANCE])) == \
        (True, ps.REASON_DEPARTMENT)
    assert ps.evaluate_record(user(roles=[ROLE_ASKER]),
                              record(roles=[ROLE_ASKER])) == (True, ps.REASON_ROLE)
    assert ps.evaluate_record(user("U000009"), record(users=["U000009"])) == \
        (True, ps.REASON_USER)


def test_or_logic_combination_allows_on_any_match():
    """AC-05-16：三维同时配，**部门不对但角色对**也要放行。"""
    perm = record(departments=[DEPT_FINANCE], roles=[ROLE_ASKER], users=["U000099"])
    assert ps.evaluate_record(user(dept_id=DEPT_HR, roles=[ROLE_ASKER]), perm)[0] is True


def test_default_deny_when_no_record():
    """AC-05-02：无权限记录 = 不可读（reason 是 `no_permission_record`）。"""
    assert ps.evaluate_record(user(dept_id=DEPT_FINANCE), None) == \
        (False, ps.REASON_NO_RECORD)


def test_default_deny_when_record_empty():
    """AC-05-03：`is_global=false` 且三维全空 → **任何人都不可读**。

    这里刻意用一个"看起来最有特权"的用户（带全部角色、任意部门）来验证：
    特权身份**不能**绕过数据权限 —— 否则就是给系统留了个后门。
    """
    everybody = user("U000001", dept_id=DEPT_FINANCE,
                     roles=[ROLE_KB_ADMIN, ROLE_ASKER])
    assert ps.evaluate_record(everybody, record()) == (False, ps.REASON_NONE_MATCHED)


def test_department_match_is_exact_not_subtree():
    """AC-05-04 / G-02：授权「财务部」不含「财务部/报销组」（精确匹配）。"""
    perm = record(departments=[DEPT_FINANCE])
    assert ps.evaluate_record(user(dept_id=DEPT_FINANCE), perm)[0] is True
    assert ps.evaluate_record(user(dept_id=DEPT_REIMBURSE), perm) == \
        (False, ps.REASON_NONE_MATCHED), "子部门不该被父部门授权带进来"

    # 反向：授权子部门也不该让父部门的人读到
    perm_child = record(departments=[DEPT_REIMBURSE])
    assert ps.evaluate_record(user(dept_id=DEPT_FINANCE), perm_child)[0] is False


def test_reason_code_priority_is_stable():
    """四维都满足时，`reason_code` 按 global→department→role→user 的顺序取第一个。

    顺序不是随意的：它决定排障时看到的原因。若顺序漂移，
    `reason_code` 会变成"每次不一样"的噪声，失去排障价值。
    """
    perm = record(is_global=True, departments=[DEPT_FINANCE],
                  roles=[ROLE_ASKER], users=["U000003"])
    subject = user("U000003", dept_id=DEPT_FINANCE, roles=[ROLE_ASKER])
    assert ps.evaluate_record(subject, perm)[1] == ps.REASON_GLOBAL


def test_stranger_gets_none_matched():
    """有记录但四维都没匹配上 → `none_matched`（与"没记录"区分开）。"""
    perm = record(departments=[DEPT_HR], roles=[ROLE_KB_ADMIN], users=["U000088"])
    assert ps.evaluate_record(user("U000077", dept_id=DEPT_FINANCE,
                                   roles=[ROLE_ASKER]), perm) == \
        (False, ps.REASON_NONE_MATCHED)


def test_empty_dept_does_not_match_empty_grant():
    """边界：用户无部门 + 授权里有空串，**不能**因为"都是空"就放行。"""
    perm = record(departments=[""])
    assert ps.evaluate_record(user(dept_id=""), perm)[0] is False


# ================================================================ 判定服务
async def test_batch_check_fail_closed_on_query_error(client, monkeypatch):
    """AC-05-08：查库异常 → **全部 deny** + `degraded=true`（ER-04）。

    这是全模块最重要的一条：数据库抖动时放行 = 把私密文档泄漏给全员。
    """
    async def boom(_doc_ids):
        raise RuntimeError("模拟 Mongo 不可用")

    monkeypatch.setattr(ps.perm_repo, "find_by_docs", boom)
    result = await ps.permission_service.batch_check(
        user(dept_id=DEPT_FINANCE), ["DOC1", "DOC2", "DOC3"])
    assert result.allowed == []
    assert sorted(result.denied) == ["DOC1", "DOC2", "DOC3"]
    assert result.degraded is True
    assert all(code == ps.REASON_DEGRADED for code in result.denied_detail.values())
    # 降级时的提示文案必须与"部分受限"不同（否则用户以为只是少了几篇）
    assert "不可用" in ps.permission_service.build_notice(result)


async def test_batch_check_uses_single_in_query(client, monkeypatch):
    """AC-05-07：无论多少 doc，只有 **1 次** `kb_permissions` 查询（ER-13）。"""
    calls: list[list[str]] = []
    original = ps.perm_repo.find_by_docs

    async def counting(doc_ids):
        calls.append(list(doc_ids))
        return await original(doc_ids)

    monkeypatch.setattr(ps.perm_repo, "find_by_docs", counting)
    docs = [f"DOC{i:04d}" for i in range(50)]
    await ps.permission_service.batch_check(user(), docs)
    assert len(calls) == 1, f"应当是 1 次 $in 查询，实际 {len(calls)} 次"
    assert sorted(calls[0]) == sorted(docs), "一次查询要带上全部待判定的 doc_id"


async def test_batch_check_missing_vs_none_matched(client):
    """`missing_perm_record` 只收"没记录"的，不收"配了但没匹配上"的。"""
    doc_a = await _new_doc("a.pdf")
    doc_b = await _new_doc("b.pdf")
    await perm_repo.upsert(doc_b, is_global=False, departments=[DEPT_HR], roles=[],
                           users=[], reason="只给人力资源部", updated_by=ACTOR,
                           ts_ms=1)
    result = await ps.permission_service.batch_check(
        user(dept_id=DEPT_FINANCE), [doc_a, doc_b])
    assert result.missing_perm_record == [doc_a]
    assert result.denied_detail[doc_b] == ps.REASON_NONE_MATCHED
    assert result.denied_detail[doc_a] == ps.REASON_NO_RECORD


async def test_cache_invalidated_by_version_change(client):
    """缓存按 `(doc_id, version)` 生效：改权限后**同进程内立刻**按新值判定（AD-02）。

    AC-05-05 的"同一秒内再判定"就靠这条：`save()` 会覆盖缓存，
    即使不覆盖，`version+1` 也会让旧 key 失效。
    """
    doc_id = await _new_doc("cache.pdf")
    subject = user(dept_id=DEPT_FINANCE)
    assert await ps.permission_service.check(subject, doc_id) is False

    await ps.permission_service.save(
        doc_id=doc_id, is_global=False, departments=[DEPT_FINANCE], roles=[],
        users=[], reason="新增财务部可读", actor_id=ACTOR)
    # 同一进程、紧接着判定 —— 必须立刻放行
    assert await ps.permission_service.check(subject, doc_id) is True
    assert await ps.permission_service.check(user(dept_id=DEPT_HR), doc_id) is False

    # 再改回去（把财务部去掉，改成全局）→ 立刻对所有人放行
    await ps.permission_service.save(
        doc_id=doc_id, is_global=True, departments=[], roles=[], users=[],
        reason="改为全局公开", actor_id=ACTOR)
    assert await ps.permission_service.check(user(dept_id=DEPT_HR), doc_id) is True


async def test_evaluate_returns_reason_and_version(client):
    doc_id = await _new_doc("ver.pdf")
    decision = await ps.permission_service.evaluate(user(), doc_id)
    assert decision.allowed is False and decision.version == 0

    saved = await ps.permission_service.save(
        doc_id=doc_id, is_global=True, departments=[], roles=[], users=[],
        reason="全局公开方便演示", actor_id=ACTOR)
    assert saved["version"] == 1
    decision = await ps.permission_service.evaluate(user(), doc_id)
    assert decision.allowed is True and decision.version == 1
    assert decision.reason_code == ps.REASON_GLOBAL


# ================================================================ 配置保存
async def test_save_requires_reason_of_at_least_five_chars(client):
    """AC-05-12 / G-11：原因必填且 ≥5 字 → `PERM-1001`。"""
    doc_id = await _new_doc("reason.pdf")
    for bad in ("", "   ", "太短", "四个字啊"):
        with pytest.raises(BizError) as exc:
            await ps.permission_service.save(
                doc_id=doc_id, is_global=True, departments=[], roles=[], users=[],
                reason=bad, actor_id=ACTOR)
        assert exc.value.spec.code == "PERM-1001", bad
    # 确认失败时**没有**产生权限记录（无副作用）
    assert await perm_repo.get_by_doc(doc_id) is None


async def test_save_validates_dimension_ids(client):
    """三维 ID 有效性分开报码：前端要能把错误定位到具体某一维。"""
    doc_id = await _new_doc("dim.pdf")
    cases = [
        ({"departments": ["DEPT9999"]}, "PERM-1002"),
        ({"departments": [DEPT_HR], "roles": ["ROLE9999"]}, "PERM-1003"),
        ({"users": ["U999999"]}, "PERM-1004"),
    ]
    for overrides, code in cases:
        payload = {"doc_id": doc_id, "is_global": False, "departments": [],
                   "roles": [], "users": [], "reason": "合法原因五字以上",
                   "actor_id": ACTOR}
        payload.update(overrides)
        with pytest.raises(BizError) as exc:
            await ps.permission_service.save(**payload)
        assert exc.value.spec.code == code, overrides
    # 停用的部门也不接受（种子里没有停用部门，这里直接构造一条）
    await mongo.collection("sys_departments").insert_one(
        {"_id": "DEPT9001", "name": "已停用部门", "status": "disabled",
         "parent_id": "__root__", "path": ["已停用部门"], "path_ids": ["DEPT9001"],
         "level": 1, "sort": 999})
    with pytest.raises(BizError) as exc:
        await ps.permission_service.save(
            doc_id=doc_id, is_global=False, departments=["DEPT9001"], roles=[],
            users=[], reason="授权给停用部门", actor_id=ACTOR)
    assert exc.value.spec.code == "PERM-1002"


async def test_save_deduplicates_and_caps_dimensions(client):
    """规则 6：去重 + 单维上限 200 → `PERM-1005`。"""
    doc_id = await _new_doc("dup.pdf")
    await ps.permission_service.save(
        doc_id=doc_id, is_global=False,
        departments=[DEPT_HR, DEPT_HR, DEPT_FINANCE], roles=[], users=[],
        reason="去重测试用例", actor_id=ACTOR)
    row = await perm_repo.get_by_doc(doc_id)
    assert row["departments"] == [DEPT_HR, DEPT_FINANCE]

    too_many = [f"DEPT{i:05d}" for i in range(201)]
    with pytest.raises(BizError) as exc:
        await ps.permission_service.save(
            doc_id=doc_id, is_global=False, departments=too_many, roles=[],
            users=[], reason="超出上限测试", actor_id=ACTOR)
    assert exc.value.spec.code == "PERM-1005"


async def test_save_rejects_unknown_doc(client):
    """`PERM-3001`：文档不存在或已软删除。"""
    with pytest.raises(BizError) as exc:
        await ps.permission_service.save(
            doc_id="DOC99999999999999", is_global=True, departments=[], roles=[],
            users=[], reason="文档不存在测试", actor_id=ACTOR)
    assert exc.value.spec.code == "PERM-3001"


async def test_version_increments_and_summary_is_refreshed(client):
    """AC-05-14：连续保存 `version` 为 1→2→3；摘要按口径回填。"""
    doc_id = await _new_doc("ver2.pdf")
    for expected in (1, 2, 3):
        saved = await ps.permission_service.save(
            doc_id=doc_id, is_global=False, departments=[DEPT_FINANCE],
            roles=[ROLE_ASKER], users=[], reason=f"第 {expected} 次保存",
            actor_id=ACTOR)
        assert saved["version"] == expected

    doc = await doc_service.get(doc_id)
    summary = doc["permission_summary"]
    assert summary["dept_cnt"] == 1 and summary["role_cnt"] == 1
    assert summary["user_cnt"] == 0 and summary["is_global"] is False
    assert summary["version"] == 3, "摘要必须带 version（03 据此判断是否过期）"
    assert summary["label"] == "limited"


async def test_summary_failure_does_not_block_save(client, monkeypatch):
    """`PERM-4002` 的语义：摘要刷新失败**不影响保存**（鉴权只读 E07）。"""
    doc_id = await _new_doc("summary.pdf")

    async def boom(*_args, **_kwargs):
        raise RuntimeError("模拟 E04 不可写")

    from app.services import doc_service as doc_service_module

    monkeypatch.setattr(doc_service_module.doc_service,
                        "refresh_permission_summary", boom)
    saved = await ps.permission_service.save(
        doc_id=doc_id, is_global=True, departments=[], roles=[], users=[],
        reason="摘要失败也要保存成功", actor_id=ACTOR)
    assert saved["version"] == 1
    assert (await perm_repo.get_by_doc(doc_id))["is_global"] is True


async def test_audit_records_before_after_and_reason(client):
    """AC-05-13：每次变更留下 `doc.permission_change`，含 before/after + reason。"""
    doc_id = await _new_doc("audit.pdf")
    await ps.permission_service.save(
        doc_id=doc_id, is_global=False, departments=[DEPT_FINANCE], roles=[],
        users=[], reason="第一次配置财务部", actor_id=ACTOR)
    await ps.permission_service.save(
        doc_id=doc_id, is_global=True, departments=[], roles=[], users=[],
        reason="改为全局公开用于演示", actor_id=ACTOR)

    # 按 `_id`（编号含序列号）排序而不是 `created_at`：审计的时间戳是**秒级**的，
    # 同一秒内的两条记录顺序不稳定 —— 而"哪条在前"正是这个用例要断言的东西
    rows = await mongo.collection("audit_logs").find(
        {"action": "doc.permission_change"}).sort("_id", 1).to_list(length=None)
    assert len(rows) == 2
    assert rows[0]["after"]["departments"] == [DEPT_FINANCE]
    assert rows[0]["before"]["version"] == 0, "首次配置的 before 是'无记录'（version=0）"
    assert rows[1]["before"]["departments"] == [DEPT_FINANCE]
    assert rows[1]["after"]["is_global"] is True
    assert "全局公开" in (rows[1].get("reason") or ""), "变更原因必须进审计（G-11）"


# ================================================================ 配置回显
async def test_get_config_defaults_without_record(client):
    """Spec §3.1：无记录返回默认值 + `version=0`，**不返回 404**。

    并且 `readable_by=nobody` 要明确告诉前端"当前无人可读"——
    前端不必自己推导这个结论（推错就会出现"界面说没人能看、实际能看"）。
    """
    doc_id = await _new_doc("cfg.pdf")
    data = await ps.permission_service.get_config(doc_id)
    assert data["doc_id"] == doc_id
    assert data["is_global"] is False
    assert data["departments"] == [] and data["roles"] == [] and data["users"] == []
    assert data["version"] == 0
    assert data["readable_by"] == "nobody"
    assert data["doc_title"], "弹窗副标题要显示标题"


async def test_get_config_expands_names(client):
    """回显要把 ID 展开成人能看懂的 `{id, name}`（原型 `04` 的四维分组）。"""
    doc_id = await _new_doc("expand.pdf")
    await ps.permission_service.save(
        doc_id=doc_id, is_global=False, departments=[DEPT_FINANCE, DEPT_HR],
        roles=[ROLE_ASKER], users=["U000003"], reason="回显展开测试",
        actor_id=ACTOR)
    data = await ps.permission_service.get_config(doc_id)
    assert [d["name"] for d in data["departments"]] == ["财务部", "人力资源部"]
    assert data["roles"][0]["code"] == "asker"
    assert data["users"][0]["real_name"], "用户要回显姓名"
    assert "password_hash" not in str(data), "响应里绝不能出现密码哈希"
    assert data["readable_by"] == "limited"


async def test_get_config_marks_deleted_principal(client):
    """授权指向一个**已不存在的角色**时如实标记 `exists=False`，不静默丢弃。"""
    doc_id = await _new_doc("ghost.pdf")
    await perm_repo.upsert(doc_id, is_global=False, departments=[],
                           roles=["ROLE9999"], users=[],
                           reason="授权给不存在的角色", updated_by=ACTOR, ts_ms=1)
    data = await ps.permission_service.get_config(doc_id)
    assert data["roles"][0]["exists"] is False
    assert data["roles"][0]["role_id"] == "ROLE9999"


# ================================================================ 变更前后不动 Milvus
async def test_saving_permission_never_touches_milvus(client, monkeypatch):
    """AC-05-06 / ER-11：改权限**不碰 Milvus**（这正是"即时生效"的物理基础）。

    用"任何 Milvus 调用都炸"来验证：如果实现里偷偷回写了切片，这个用例立刻失败。
    """
    called: list[str] = []

    def boom(*_args, **_kwargs):
        called.append("milvus")
        raise AssertionError("权限变更不该触碰 Milvus（ER-11 / AD-03）")

    from app.infra.milvus import milvus

    for name in ("insert_chunks", "set_enabled", "upsert", "delete", "flush"):
        if hasattr(milvus, name):
            monkeypatch.setattr(milvus, name, boom, raising=False)

    doc_id = await _new_doc("nomilvus.pdf")
    await ps.permission_service.save(
        doc_id=doc_id, is_global=True, departments=[], roles=[], users=[],
        reason="不应触碰向量库", actor_id=ACTOR)
    assert called == []
