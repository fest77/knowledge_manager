# -*- coding: utf-8 -*-
"""模块 02 · 角色（E10）的测试。

对应验收：**AC-02-11**（内置角色不可删 `ORG-2008` / `code` 不可改 `ORG-2007`）、
**AC-02-12**（仍被用户使用的角色不可删 `ORG-2009`）、
**AC-02-13**（`permission_count` 为 4 / 16 / 22，与原型 `07` 一致）。

外加裁定 A 的业务角色规则：`is_system` 恒为 `false`、**默认不含功能权限**、
无用户绑定时可删（并**经 01 清空 E13**，不得自己删——ER-02）。
"""
from __future__ import annotations

import pytest

from app.infra.mongo import mongo
from app.repositories import audit_repo, auth_repo, org_repo
from tests.conftest import token_of

pytestmark = pytest.mark.anyio

SYS_ADMIN = "lina"
KB_ADMIN = "zhangwei"
ASKER = "wangqiang"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _roles(client, token: str) -> list[dict]:
    resp = await client.get("/api/v1/org/roles", headers=auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["items"]


# --------------------------------------------------------------------- 列表
async def test_ac_02_13_permission_counts_match_the_prototype(client):
    """AC-02-13：内置三角色的 `permission_count` 必须是 4 / 16 / 22。"""
    token = await token_of(client, SYS_ADMIN)
    rows = {r["code"]: r for r in await _roles(client, token)}
    assert rows["asker"]["permission_count"] == 4
    assert rows["kb_admin"]["permission_count"] == 16
    assert rows["sys_admin"]["permission_count"] == 22
    assert rows["management"]["permission_count"] == 0, "业务角色默认无功能权限"


async def test_role_list_columns(client):
    """原型 `07` 的 6 列：编码 / 名称 / 类型 / 功能权限数 / 用户数 / 说明。"""
    token = await token_of(client, SYS_ADMIN)
    rows = {r["code"]: r for r in await _roles(client, token)}
    assert set(rows) == {"asker", "kb_admin", "sys_admin", "management"}
    assert rows["asker"]["is_system"] is True
    assert rows["asker"]["role_type"] == "内置"
    assert rows["management"]["role_type"] == "自定义"
    assert rows["management"]["user_count"] == 0
    assert rows["kb_admin"]["user_count"] == 1, "张伟绑定 kb_admin"


async def test_role_endpoints_require_role_manage(client):
    for name in (KB_ADMIN, ASKER):
        token = await token_of(client, name)
        for method, path in (("get", "/api/v1/org/roles"),
                             ("post", "/api/v1/org/roles"),
                             ("put", "/api/v1/org/roles/ROLE0004"),
                             ("delete", "/api/v1/org/roles/ROLE0004"),
                             ("get", "/api/v1/org/roles/ROLE0004/permissions"),
                             ("put", "/api/v1/org/roles/ROLE0004/permissions"),
                             ("get", "/api/v1/auth/permissions")):
            resp = await client.request(method.upper(), path, headers=auth(token), json={})
            assert resp.status_code == 403, f"{name} {method} {path}"
            assert resp.json()["code"] == "AUTH-2004"


# --------------------------------------------------------------------- 新建
async def test_create_business_role_defaults_to_no_permissions(client):
    """裁定 A：业务角色只作数据权限分组标签，**默认 0 个功能权限**。"""
    token = await token_of(client, SYS_ADMIN)
    resp = await client.post("/api/v1/org/roles", headers=auth(token), json={
        "code": "auditor", "name": "审计员", "description": "只读看板"})
    assert resp.status_code == 200, resp.text
    role_id = resp.json()["data"]["role_id"]

    row = await mongo.collection(org_repo.ROLES).find_one({"_id": role_id})
    assert row["is_system"] is False and row["code"] == "auditor"
    assert await auth_repo.permission_ids_of_role(role_id) == []

    rows = {r["code"]: r for r in await _roles(client, token)}
    assert rows["auditor"]["permission_count"] == 0
    assert rows["auditor"]["role_type"] == "自定义"

    audit = await mongo.collection(audit_repo.AUDIT_LOGS).find_one(
        {"action": "role.create", "target_id": role_id})
    assert audit is not None and audit["after"]["is_system"] is False


async def test_create_role_validations(client):
    token = await token_of(client, SYS_ADMIN)
    dup_code = await client.post("/api/v1/org/roles", headers=auth(token),
                                 json={"code": "asker", "name": "重复编码"})
    assert dup_code.status_code == 409
    assert dup_code.json()["code"] == "ORG-2006", "不得与内置角色 code 冲突"

    await client.post("/api/v1/org/roles", headers=auth(token),
                      json={"code": "temp_role", "name": "临时"})
    dup2 = await client.post("/api/v1/org/roles", headers=auth(token),
                             json={"code": "temp_role", "name": "再来一个"})
    assert dup2.json()["code"] == "ORG-2006"

    bad_code = await client.post("/api/v1/org/roles", headers=auth(token),
                                 json={"code": "BadCode", "name": "大写不行"})
    assert bad_code.json()["code"] == "SYS-1001"


# --------------------------------------------------------------------- 编辑
async def test_ac_02_11_builtin_role_code_is_locked_and_role_is_undeletable(client):
    """AC-02-11：内置角色 `code` 不可改（`ORG-2007`）、不可删除（`ORG-2008`）。"""
    token = await token_of(client, SYS_ADMIN)

    renamed = await client.put("/api/v1/org/roles/ROLE0001", headers=auth(token),
                               json={"code": "asker_v2"})
    assert renamed.status_code == 400
    assert renamed.json()["code"] == "ORG-2007"

    # name / description 可改
    ok = await client.put("/api/v1/org/roles/ROLE0001", headers=auth(token),
                          json={"name": "提问者", "description": "只问答"})
    assert ok.status_code == 200
    assert ok.json()["data"]["changed"] == {"name": "提问者", "description": "只问答"}

    for role_id in ("ROLE0001", "ROLE0002", "ROLE0003"):
        deleted = await client.delete(f"/api/v1/org/roles/{role_id}",
                                      headers=auth(token))
        assert deleted.status_code == 403
        assert deleted.json()["code"] == "ORG-2008"

    # 传与现状一致的 code 不算修改
    same = await client.put("/api/v1/org/roles/ROLE0002", headers=auth(token),
                            json={"code": "kb_admin"})
    assert same.status_code == 200 and same.json()["data"]["changed"] == {}


async def test_update_role_audits_before_after(client):
    token = await token_of(client, SYS_ADMIN)
    await client.put("/api/v1/org/roles/ROLE0004", headers=auth(token),
                     json={"description": "管理层（数据权限分组）"})
    row = await mongo.collection(audit_repo.AUDIT_LOGS).find_one(
        {"action": "role.update", "target_id": "ROLE0004"})
    assert row is not None
    assert row["before"]["description"] != row["after"]["description"]


async def test_update_missing_role_reports_3003(client):
    """`AUTH-3003`：角色不存在（E10 属 02，01 只读校验）。"""
    token = await token_of(client, SYS_ADMIN)
    resp = await client.put("/api/v1/org/roles/ROLE9999", headers=auth(token),
                            json={"name": "谁"})
    assert resp.status_code == 404
    assert resp.json()["code"] == "AUTH-3003"


# --------------------------------------------------------------------- 删除
async def test_ac_02_12_role_in_use_cannot_be_deleted(client):
    """AC-02-12：仍被用户绑定的角色不可删（`ORG-2009`）。"""
    token = await token_of(client, SYS_ADMIN)
    resp = await client.delete("/api/v1/org/roles/ROLE0001", headers=auth(token))
    assert resp.status_code == 403 and resp.json()["code"] == "ORG-2008", \
        "ROLE0001 是内置角色，先命中不可删"

    # 造一个业务角色并绑给人，再删
    created = await client.post("/api/v1/org/roles", headers=auth(token),
                                json={"code": "bound_role", "name": "被绑定的角色"})
    role_id = created.json()["data"]["role_id"]
    await client.put("/api/v1/org/users/U000003/roles", headers=auth(token),
                     json={"role_ids": ["ROLE0001", role_id], "reason": "绑定用于删除校验"})
    in_use = await client.delete(f"/api/v1/org/roles/{role_id}", headers=auth(token))
    assert in_use.status_code == 409
    assert in_use.json()["code"] == "ORG-2009"


async def test_delete_business_role_revokes_its_permissions_via_module_01(client):
    """删除业务角色时**必须经 01** 清空 E13（ER-02），且审计留痕。

    这里给业务角色先授一个功能权限，再解绑用户、删角色，
    最后断言 `sys_role_permissions` 里没有残留——否则会留下指向不存在角色的授权。
    """
    token = await token_of(client, SYS_ADMIN)
    created = await client.post("/api/v1/org/roles", headers=auth(token),
                                json={"code": "temp_perm_role", "name": "临时带权限角色"})
    role_id = created.json()["data"]["role_id"]

    grant = await client.put(f"/api/v1/org/roles/{role_id}/permissions",
                             headers=auth(token),
                             json={"permission_ids": ["PERM0001"],
                                   "reason": "给它一个权限以便验证级联清理"})
    assert grant.status_code == 200, grant.text
    assert await auth_repo.permission_ids_of_role(role_id) == ["PERM0001"]

    deleted = await client.delete(f"/api/v1/org/roles/{role_id}", headers=auth(token))
    assert deleted.status_code == 200, deleted.text
    assert await auth_repo.permission_ids_of_role(role_id) == [], "E13 必须被级联清空"
    assert await mongo.collection(org_repo.ROLES).count_documents({"_id": role_id}) == 0

    audit = await mongo.collection(audit_repo.AUDIT_LOGS).find_one(
        {"action": "role.delete", "target_id": role_id})
    assert audit is not None and audit["before"]["code"] == "temp_perm_role"


async def test_delete_missing_role_reports_3003(client):
    token = await token_of(client, SYS_ADMIN)
    resp = await client.delete("/api/v1/org/roles/ROLE9999", headers=auth(token))
    assert resp.status_code == 404
    assert resp.json()["code"] == "AUTH-3003"


# --------------------------------------------------------------------- 降级
async def test_audit_failure_does_not_block_role_operations(client, monkeypatch):
    """AC-02-16 的角色侧：审计挂掉，新建/编辑/删除角色仍然成功。"""
    async def down(_doc):
        raise RuntimeError("模拟 audit_logs 不可写")

    token = await token_of(client, SYS_ADMIN)
    created = await client.post("/api/v1/org/roles", headers=auth(token),
                                json={"code": "resilient_role", "name": "韧性角色"})
    assert created.status_code == 200
    role_id = created.json()["data"]["role_id"]

    monkeypatch.setattr(audit_repo, "insert", down)
    assert (await client.put(f"/api/v1/org/roles/{role_id}", headers=auth(token),
                             json={"name": "改了名"})).status_code == 200
    assert (await client.delete(f"/api/v1/org/roles/{role_id}",
                                headers=auth(token))).status_code == 200
