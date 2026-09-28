# -*- coding: utf-8 -*-
"""模块 02 · 部门树（E08）的测试。

对应验收：**AC-02-01**（树结构与 `path_ids` 一致）、**AC-02-02**（同级重名）、
**AC-02-03**（成环拒绝）、**AC-02-04**（移动后全部子孙级联重算）、
**AC-02-05**（三项删除前置）、**AC-02-15**（写操作都有审计且不含敏感值）、
**AC-02-16**（审计挂掉不阻断业务）。

外加本模块自己的边界：层级上限、`ORG-1004`（拒收子部门参数）、
"不传 `parent_id` ≠ 传 `null`"这条最容易写错的更新语义。
"""
from __future__ import annotations

import pytest

from app.core.errors import BizError
from app.infra.mongo import mongo
from app.repositories import audit_repo, org_repo
from app.services import org_service
from app.services.audit_service import audit_service
from tests.conftest import token_of

pytestmark = pytest.mark.anyio

SYS_ADMIN = "lina"
KB_ADMIN = "zhangwei"
ASKER = "wangqiang"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _tree(client, token: str) -> list[dict]:
    resp = await client.get("/api/v1/org/departments", headers=auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["items"]


def _flatten(nodes: list[dict]) -> list[dict]:
    out: list[dict] = []
    for node in nodes:
        out.append(node)
        out.extend(_flatten(node["children"]))
    return out


# --------------------------------------------------------------------- AC-02-01
async def test_ac_02_01_tree_matches_seed_and_path_ids_are_consistent(client):
    """AC-02-01：树按 `path` 正确嵌套，且 `path_ids` 与层级一致。"""
    token = await token_of(client, SYS_ADMIN)
    roots = await _tree(client, token)
    assert len(roots) == 1 and roots[0]["name"] == "总部"
    assert roots[0]["level"] == 0 and roots[0]["path_ids"] == ["DEPT0001"]

    names = [c["name"] for c in roots[0]["children"]]
    assert names[:3] == ["人力资源部", "财务部", "技术部"], names
    assert "客户服务部" in names and "市场部" in names and "总经办" in names

    flat = _flatten(roots)
    assert len(flat) == 8, "种子应有 8 个部门"
    for node in flat:
        assert node["level"] == len(node["path_ids"]) - 1, node
        assert len(node["path"]) == len(node["path_ids"]), node
        assert node["path_ids"][-1] == node["dept_id"], node
        assert node["path"][-1] == node["name"], node
        # 父节点的 path_ids 必须是自己的前缀（物化路径的核心不变量）
        parent = next((n for n in flat if n["dept_id"] == node["parent_id"]), None)
        if parent:
            assert node["path_ids"][:parent["level"] + 1] == parent["path_ids"]

    finance = next(n for n in flat if n["name"] == "财务部")
    assert [c["name"] for c in finance["children"]] == ["报销组"]
    assert finance["children"][0]["path"] == ["总部", "财务部", "报销组"]


async def test_user_count_is_returned_and_can_be_skipped(client):
    """`user_count` 供删除前提示；`with_user_count=false` 时不查用户表。"""
    token = await token_of(client, SYS_ADMIN)
    flat = _flatten(await _tree(client, token))
    hr = next(n for n in flat if n["name"] == "人力资源部")
    assert hr["user_count"] == 1, "张伟在人力资源部"

    resp = await client.get("/api/v1/org/departments?with_user_count=false",
                            headers=auth(token))
    flat2 = _flatten(resp.json()["data"]["items"])
    assert all(n["user_count"] == 0 for n in flat2)


# --------------------------------------------------------------------- 权限边界
async def test_permission_boundary_on_department_endpoints(client):
    """四个部门接口各自需要不同的功能权限码。"""
    kb = await token_of(client, KB_ADMIN)          # 16 权限，无 org:manage
    asker = await token_of(client, ASKER)
    for token in (kb, asker):
        for method, path in (("get", "/api/v1/org/departments"),
                             ("post", "/api/v1/org/departments"),
                             ("put", "/api/v1/org/departments/DEPT0002"),
                             ("delete", "/api/v1/org/departments/DEPT0002")):
            resp = await client.request(method.upper(), path, headers=auth(token),
                                        json={} if method in ("post", "put") else None)
            assert resp.status_code == 403, f"{method} {path} 应被拒"
            assert resp.json()["code"] == "AUTH-2004"
    no_token = await client.get("/api/v1/org/departments")
    assert no_token.status_code == 401


def test_service_layer_fallback_guard_exists():
    """R-02 的服务层兜底：绕过 FastAPI 直调时也要拦住。"""
    from app.api.deps import ensure_permission
    from app.core.errors import Err
    from app.services.auth_service import UserContext

    plain = UserContext(user_id="U000003", username="wangqiang", real_name="王强",
                        dept_id="DEPT0004", dept_name="技术部")
    with pytest.raises(BizError) as exc:
        ensure_permission(plain, "dept:create", Err.AUTH_PERM_DENIED)
    assert exc.value.spec.code == "AUTH-2004"


def test_org_1004_rejects_sub_dept_style_params():
    """`ORG-1004`：G-02 裁定部门授权**不含子部门**，故这类参数明确拒绝而非静默忽略。"""
    with pytest.raises(BizError) as exc:
        org_service.reject_sub_dept_params(include_sub_dept="true")
    assert exc.value.spec.code == "ORG-1004"
    with pytest.raises(BizError):
        org_service.reject_sub_dept_params(sub_dept="1")
    # 不传 / 传 false 都放行
    org_service.reject_sub_dept_params(include_sub_dept=None, sub_dept="false")


async def test_org_1004_is_enforced_on_the_endpoint(client):
    token = await token_of(client, SYS_ADMIN)
    resp = await client.get("/api/v1/org/departments?include_sub_dept=true",
                            headers=auth(token))
    assert resp.status_code == 400
    assert resp.json()["code"] == "ORG-1004"


# --------------------------------------------------------------------- 新建
async def test_create_department_computes_path_and_audits(client):
    """新建：物化路径正确 + 写 `dept.create` 审计（带 `after`）。"""
    token = await token_of(client, SYS_ADMIN)
    resp = await client.post("/api/v1/org/departments", headers=auth(token), json={
        "name": "法务部", "parent_id": "DEPT0001", "sort": 90})
    assert resp.status_code == 200, resp.text
    dept_id = resp.json()["data"]["dept_id"]
    assert dept_id.startswith("DEPT")

    row = await mongo.collection(org_repo.DEPARTMENTS).find_one({"_id": dept_id})
    assert row["path"] == ["总部", "法务部"]
    assert row["path_ids"] == ["DEPT0001", dept_id]
    assert row["level"] == 1 and row["status"] == "active"

    audit = await mongo.collection(audit_repo.AUDIT_LOGS).find_one(
        {"action": "dept.create", "target_id": dept_id})
    assert audit is not None
    assert audit["after"]["path_ids"] == ["DEPT0001", dept_id]
    assert audit["target_type"] == "dept"


async def test_create_at_root_uses_none_parent(client):
    """根部门的 `parent_id` 在库里存哨兵、在接口上回 `None`。"""
    token = await token_of(client, SYS_ADMIN)
    resp = await client.post("/api/v1/org/departments", headers=auth(token),
                             json={"name": "董事会"})
    dept_id = resp.json()["data"]["dept_id"]
    row = await mongo.collection(org_repo.DEPARTMENTS).find_one({"_id": dept_id})
    assert row["parent_id"] == org_repo.ROOT_SENTINEL, "库里存哨兵值以便建唯一索引"
    assert row["level"] == 0

    flat = _flatten(await _tree(client, token))
    node = next(n for n in flat if n["dept_id"] == dept_id)
    assert node["parent_id"] is None, "对外不能暴露哨兵值"


async def test_ac_02_02_same_level_duplicate_name_is_rejected(client):
    """AC-02-02：同级重名 → `ORG-3001`。"""
    token = await token_of(client, SYS_ADMIN)
    dup_root = await client.post("/api/v1/org/departments", headers=auth(token),
                                 json={"name": "人力资源部", "parent_id": "DEPT0001"})
    assert dup_root.status_code == 409
    assert dup_root.json()["code"] == "ORG-3001"
    # 不同层级可以同名
    ok_other = await client.post("/api/v1/org/departments", headers=auth(token),
                                 json={"name": "人力资源部", "parent_id": "DEPT0003"})
    assert ok_other.status_code == 200


async def test_parent_must_exist_and_be_active(client):
    """`ORG-3002`：父部门不存在 / 已停用。"""
    token = await token_of(client, SYS_ADMIN)
    missing = await client.post("/api/v1/org/departments", headers=auth(token),
                                json={"name": "X部", "parent_id": "DEPT9999"})
    assert missing.json()["code"] == "ORG-3002"

    await org_repo.update_dept("DEPT0007", {"status": "disabled"})
    disabled = await client.post("/api/v1/org/departments", headers=auth(token),
                                 json={"name": "Y部", "parent_id": "DEPT0007"})
    assert disabled.json()["code"] == "ORG-3002"


async def test_depth_limit_is_five_levels(client):
    """`ORG-3003`：层级上限 5 层（level 0~4）。"""
    dept_id = "DEPT0008"                      # 已在 level 2
    for index in range(2):                    # 造到 level 4
        parent = dept_id
        dept_id = await org_service.create_dept(
            name=f"深{index}级", parent_id=parent, sort=1, leader_user_id=None,
            actor_id="U000001")
    token = await token_of(client, SYS_ADMIN)
    too_deep = await client.post("/api/v1/org/departments", headers=auth(token),
                                 json={"name": "第六层", "parent_id": dept_id})
    assert too_deep.status_code == 400
    assert too_deep.json()["code"] == "ORG-3003"


# --------------------------------------------------------------------- 移动与级联
async def test_ac_02_03_moving_under_own_descendant_is_rejected(client):
    """AC-02-03：把「总部」移到自己的子孙下 → `ORG-3004`。"""
    token = await token_of(client, SYS_ADMIN)
    resp = await client.put("/api/v1/org/departments/DEPT0001", headers=auth(token),
                            json={"parent_id": "DEPT0003"})
    assert resp.status_code == 409
    assert resp.json()["code"] == "ORG-3004"
    # 移到自己下面同样是环
    self_move = await client.put("/api/v1/org/departments/DEPT0003",
                                 headers=auth(token), json={"parent_id": "DEPT0003"})
    assert self_move.json()["code"] == "ORG-3004"


async def test_ac_02_04_move_cascades_to_all_descendants(client):
    """AC-02-04：移动后**所有子孙**的 `path` / `path_ids` / `level` 都被重算。"""
    token = await token_of(client, SYS_ADMIN)
    resp = await client.put("/api/v1/org/departments/DEPT0003", headers=auth(token),
                            json={"parent_id": "DEPT0004"})       # 财务部 → 技术部下
    assert resp.status_code == 200, resp.text

    flat = _flatten(await _tree(client, token))
    finance = next(n for n in flat if n["dept_id"] == "DEPT0003")
    group = next(n for n in flat if n["dept_id"] == "DEPT0008")   # 报销组（原 level 2）

    assert finance["path_ids"] == ["DEPT0001", "DEPT0004", "DEPT0003"]
    assert finance["path"] == ["总部", "技术部", "财务部"]
    assert finance["level"] == 2
    assert group["path_ids"] == ["DEPT0001", "DEPT0004", "DEPT0003", "DEPT0008"]
    assert group["path"] == ["总部", "技术部", "财务部", "报销组"]
    assert group["level"] == 3, "子孙层级必须跟着父级走"

    # 库里也要一致（不能只是接口层算出来的）
    row = await mongo.collection(org_repo.DEPARTMENTS).find_one({"_id": "DEPT0008"})
    assert row["level"] == 3 and row["path"][1] == "技术部"


async def test_move_to_root_and_not_passing_parent_id_are_different(client):
    """最容易写错的更新语义：**不传 `parent_id` = 不动**，传 `null` = 移到根下。"""
    token = await token_of(client, SYS_ADMIN)
    # 只改名：父级必须保持 DEPT0003
    renamed = await client.put("/api/v1/org/departments/DEPT0008", headers=auth(token),
                               json={"name": "报销与结算组"})
    assert renamed.status_code == 200
    row = await mongo.collection(org_repo.DEPARTMENTS).find_one({"_id": "DEPT0008"})
    assert row["parent_id"] == "DEPT0003", "不传 parent_id 不该动它"
    assert row["name"] == "报销与结算组"

    # 移到根下
    moved = await client.put("/api/v1/org/departments/DEPT0008", headers=auth(token),
                             json={"parent_id": None})
    assert moved.status_code == 200, moved.text
    row = await mongo.collection(org_repo.DEPARTMENTS).find_one({"_id": "DEPT0008"})
    assert row["level"] == 0
    assert row["path"] == ["报销与结算组"]


async def test_rename_cascades_to_descendant_paths(client):
    """改名会让子孙的 `path`（名字数组）失效，必须级联修正。"""
    token = await token_of(client, SYS_ADMIN)
    resp = await client.put("/api/v1/org/departments/DEPT0003", headers=auth(token),
                            json={"name": "财务中心"})
    assert resp.status_code == 200
    group = await mongo.collection(org_repo.DEPARTMENTS).find_one({"_id": "DEPT0008"})
    assert group["path"] == ["总部", "财务中心", "报销组"]
    assert group["path_ids"] == ["DEPT0001", "DEPT0003", "DEPT0008"], "改名不该动 ID 路径"


async def test_move_audit_records_before_and_after(client):
    """`dept.update` 审计必须带 `before` / `after`（AC-02-15）。"""
    token = await token_of(client, SYS_ADMIN)
    await client.put("/api/v1/org/departments/DEPT0006", headers=auth(token),
                     json={"parent_id": "DEPT0003"})
    row = await mongo.collection(audit_repo.AUDIT_LOGS).find_one(
        {"action": "dept.update", "target_id": "DEPT0006"})
    assert row is not None
    detail = await audit_service.detail(row["_id"])
    assert detail["before"]["parent_id"] == "DEPT0001"
    assert detail["after"]["parent_id"] == "DEPT0003"


# --------------------------------------------------------------------- 停用与删除
async def test_disable_department_with_active_users_is_rejected(client):
    """`ORG-3005`：部门下仍有在用用户则不能停用。"""
    token = await token_of(client, SYS_ADMIN)
    resp = await client.put("/api/v1/org/departments/DEPT0002", headers=auth(token),
                            json={"status": "disabled"})
    assert resp.status_code == 409
    assert resp.json()["code"] == "ORG-3005"
    # 无在用用户的部门可以停用
    ok = await client.put("/api/v1/org/departments/DEPT0006", headers=auth(token),
                          json={"status": "disabled"})
    assert ok.status_code == 200
    assert ok.json()["data"]["changed"]["status"] == "disabled"


async def test_ac_02_05_delete_preconditions(client):
    """AC-02-05：有子部门 / 有用户 / 被权限引用 → `ORG-3006` / `ORG-3007` / `ORG-3008`。"""
    token = await token_of(client, SYS_ADMIN)

    has_child = await client.delete("/api/v1/org/departments/DEPT0003",
                                    headers=auth(token))
    assert has_child.status_code == 409 and has_child.json()["code"] == "ORG-3006"

    has_user = await client.delete("/api/v1/org/departments/DEPT0002",
                                   headers=auth(token))
    assert has_user.status_code == 409 and has_user.json()["code"] == "ORG-3007"

    # 造一条知识权限引用（E07 属 05，此处直插模拟）
    # 用 upsert 保证用例可重复跑：`kb_permissions` 不属 02，不在 seed 的清库清单里
    await mongo.collection(org_repo.KB_PERMISSIONS).replace_one(
        {"_id": "DOC0001"},
        {"_id": "DOC0001", "doc_id": "DOC0001", "departments": ["DEPT0006"]},
        upsert=True)
    referenced = await client.delete("/api/v1/org/departments/DEPT0006",
                                     headers=auth(token))
    assert referenced.status_code == 409
    assert referenced.json()["code"] == "ORG-3008"

    # 三项全过的部门可以删
    created = await client.post("/api/v1/org/departments", headers=auth(token),
                                json={"name": "临时部门", "parent_id": "DEPT0001"})
    dept_id = created.json()["data"]["dept_id"]
    deleted = await client.delete(f"/api/v1/org/departments/{dept_id}",
                                  headers=auth(token))
    assert deleted.status_code == 200 and deleted.json()["data"]["deleted"] is True
    assert await mongo.collection(org_repo.DEPARTMENTS).count_documents(
        {"_id": dept_id}) == 0
    audit = await mongo.collection(audit_repo.AUDIT_LOGS).find_one(
        {"action": "dept.delete", "target_id": dept_id})
    assert audit is not None and audit["before"]["name"] == "临时部门"


async def test_delete_missing_department_reports_3002(client):
    token = await token_of(client, SYS_ADMIN)
    resp = await client.delete("/api/v1/org/departments/DEPT9999", headers=auth(token))
    assert resp.status_code == 404
    assert resp.json()["code"] == "ORG-3002"


# --------------------------------------------------------------------- 降级
async def test_ac_02_16_audit_failure_does_not_block_business(client, monkeypatch):
    """AC-02-16：审计写入一直失败，部门新建**仍然成功**。"""
    async def down(_doc):
        raise RuntimeError("模拟 audit_logs 不可写")

    monkeypatch.setattr(audit_repo, "insert", down)
    token = await token_of(client, SYS_ADMIN)
    resp = await client.post("/api/v1/org/departments", headers=auth(token),
                             json={"name": "审计故障下新建部", "parent_id": "DEPT0001"})
    assert resp.status_code == 200, resp.text
    dept_id = resp.json()["data"]["dept_id"]
    assert await mongo.collection(org_repo.DEPARTMENTS).count_documents(
        {"_id": dept_id}) == 1


async def test_create_rejects_extra_fields(client):
    """`extra="forbid"`：多传字段直接判参数错，不让前端静默传错字段名。"""
    token = await token_of(client, SYS_ADMIN)
    resp = await client.post("/api/v1/org/departments", headers=auth(token),
                             json={"name": "X", "parent": "DEPT0001"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "SYS-1001"
