# -*- coding: utf-8 -*-
"""模块 01 遗留件 · 角色功能权限分配（E13）的测试。

这是**模块 01 的接口**（E13 的唯一写入者是 01），路径挂在 `/org` 分区下。
它之所以和模块 02 一起交付：原型 `07`/`08` 里"角色 CRUD"与"功能权限矩阵"是同一页，
分开做会让页面只能半亮。

对应验收：模块 01 Spec §3.3/§3.4/§3.5 与 R-01~R-06。
"""
from __future__ import annotations

import pytest

from app.infra.mongo import mongo
from app.repositories import audit_repo, auth_repo
from app.services.permission_cache import permission_cache
from tests.conftest import token_of

pytestmark = pytest.mark.anyio

SYS_ADMIN = "lina"
KB_ADMIN = "zhangwei"
ASKER = "wangqiang"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------- 定义清单
async def test_permission_defs_returns_34_codes(client):
    """`GET /auth/permissions`：34 条定义，字段供矩阵页直接渲染。"""
    token = await token_of(client, SYS_ADMIN)
    resp = await client.get("/api/v1/auth/permissions", headers=auth(token))
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]["items"]
    assert len(items) == 34
    assert {i["code"] for i in items} >= {"qa:use", "doc:read", "audit:read", "role:grant"}
    for item in items:
        assert set(item) == {"permission_id", "code", "name", "type", "parent_id",
                             "menu_path", "sort"}
    assert [i["sort"] for i in items] == sorted(i["sort"] for i in items)


async def test_auth_permissions_requires_role_manage(client):
    token = await token_of(client, KB_ADMIN)
    resp = await client.get("/api/v1/auth/permissions", headers=auth(token))
    assert resp.status_code == 403 and resp.json()["code"] == "AUTH-2004"


# --------------------------------------------------------------------- 角色已有权限
async def test_get_role_permissions_matches_the_seed(client):
    """`kb_admin` 应有 16 条；`asker` 4 条；业务角色 0 条。"""
    token = await token_of(client, SYS_ADMIN)
    for role_id, expected in (("ROLE0001", 4), ("ROLE0002", 16), ("ROLE0003", 22),
                              ("ROLE0004", 0)):
        resp = await client.get(f"/api/v1/org/roles/{role_id}/permissions",
                                headers=auth(token))
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["role_id"] == role_id
        assert len(data["permission_ids"]) == expected, role_id
        assert len(data["codes"]) == expected
        assert data["permission_ids"] == sorted(data["permission_ids"])


async def test_get_role_permissions_unknown_role(client):
    token = await token_of(client, SYS_ADMIN)
    resp = await client.get("/api/v1/org/roles/ROLE9999/permissions", headers=auth(token))
    assert resp.status_code == 404
    assert resp.json()["code"] == "AUTH-3003"


# --------------------------------------------------------------------- 分配
async def test_grant_is_diff_based_and_audited(client):
    """差集落库（R-03）+ 写 `role.grant` 审计（R-04）+ 失效角色缓存（R-05）。"""
    token = await token_of(client, SYS_ADMIN)
    # 给业务角色 ROLE0004 授两个权限
    resp = await client.put("/api/v1/org/roles/ROLE0004/permissions", headers=auth(token),
                            json={"permission_ids": ["PERM0001", "PERM0005"],
                                  "reason": "管理层需要看板权限"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["added"] == ["PERM0001", "PERM0005"] and data["removed"] == []
    assert await auth_repo.permission_ids_of_role("ROLE0004") == ["PERM0001", "PERM0005"]

    # 再改：去掉一个、留一个 → removed 必须精确
    again = await client.put("/api/v1/org/roles/ROLE0004/permissions",
                             headers=auth(token),
                             json={"permission_ids": ["PERM0005"],
                                   "reason": "去掉多余的问答权限"})
    assert again.json()["data"]["added"] == []
    assert again.json()["data"]["removed"] == ["PERM0001"]

    row = await mongo.collection(audit_repo.AUDIT_LOGS).find_one(
        {"action": "role.grant", "target_id": "ROLE0004"})
    assert row is not None
    assert row["reason"] == "去掉多余的问答权限"
    assert row["before"]["permission_ids"] == ["PERM0001", "PERM0005"]
    assert row["after"]["permission_ids"] == ["PERM0005"]
    assert row["target_type"] == "role"


async def test_grant_invalidates_role_cache(client):
    """R-05：授权变更后该角色的权限快照必须失效（否则最多 60 秒不生效）。"""
    token = await token_of(client, SYS_ADMIN)
    await token_of(client, KB_ADMIN)                      # 触发一次缓存写入
    assert permission_cache.stats()["size"] >= 1
    await client.put("/api/v1/org/roles/ROLE0002/permissions", headers=auth(token),
                     json={"permission_ids": [], "reason": "清空知识管理员权限"})
    assert permission_cache.stats()["size"] == 0, "角色授权变更后缓存必须全量作废"


async def test_grant_no_change_reports_auth_3001_without_audit(client):
    """R-02：无变更 → `AUTH-3001`，且**不写审计**（否则点一次保存留一条空记录）。"""
    token = await token_of(client, SYS_ADMIN)
    current = (await client.get("/api/v1/org/roles/ROLE0001/permissions",
                                headers=auth(token))).json()["data"]["permission_ids"]
    before = await mongo.collection(audit_repo.AUDIT_LOGS).count_documents(
        {"action": "role.grant", "target_id": "ROLE0001"})
    resp = await client.put("/api/v1/org/roles/ROLE0001/permissions", headers=auth(token),
                            json={"permission_ids": current, "reason": "原样再提交一次"})
    assert resp.status_code == 409
    assert resp.json()["code"] == "AUTH-3001"
    after = await mongo.collection(audit_repo.AUDIT_LOGS).count_documents(
        {"action": "role.grant", "target_id": "ROLE0001"})
    assert after == before, "无变更不该留下审计记录"


async def test_grant_validations(client):
    token = await token_of(client, SYS_ADMIN)
    missing_role = await client.put("/api/v1/org/roles/ROLE9999/permissions",
                                    headers=auth(token),
                                    json={"permission_ids": [], "reason": "角色不存在"})
    assert missing_role.json()["code"] == "AUTH-3003"

    bad_perm = await client.put("/api/v1/org/roles/ROLE0004/permissions",
                                headers=auth(token),
                                json={"permission_ids": ["PERM9999"], "reason": "权限不存在"})
    assert bad_perm.json()["code"] == "AUTH-3004"

    short_reason = await client.put("/api/v1/org/roles/ROLE0004/permissions",
                                    headers=auth(token),
                                    json={"permission_ids": [], "reason": "短"})
    assert short_reason.status_code == 400, "Pydantic 先拦最小长度"


async def test_r06_cannot_remove_sys_admin_grant_ability(client):
    """R-06 防自锁：不允许把 `sys_admin` 的 `role:grant` 移除。

    Spec 里这一条有两个码（§3.5 说 `AUTH-3002`，§5 码表写 `AUTH-2005`）。
    本实现取 `AUTH-3002`（409）——调用方**有**权限，只是这次操作会让系统失去
    授权能力，"状态冲突"比"权限不足"准确。
    """
    token = await token_of(client, SYS_ADMIN)
    grant_perm_id = next(
        i["permission_id"] for i in (await client.get("/api/v1/auth/permissions",
                                                      headers=auth(token))
                                     ).json()["data"]["items"]
        if i["code"] == "role:grant")
    current = (await client.get("/api/v1/org/roles/ROLE0003/permissions",
                                headers=auth(token))).json()["data"]["permission_ids"]
    assert grant_perm_id in current

    resp = await client.put("/api/v1/org/roles/ROLE0003/permissions", headers=auth(token),
                            json={"permission_ids": [p for p in current
                                                     if p != grant_perm_id],
                                  "reason": "试试把授权能力拿掉"})
    assert resp.status_code == 409
    assert resp.json()["code"] == "AUTH-3002"
    assert await auth_repo.permission_ids_of_role("ROLE0003") == current, "不能落库"


async def test_r06_allows_keeping_grant_ability_while_changing_others(client):
    """防自锁只针对 `role:grant` 本身，其他权限照常可增删。"""
    token = await token_of(client, SYS_ADMIN)
    items = (await client.get("/api/v1/auth/permissions",
                              headers=auth(token))).json()["data"]["items"]
    grant_perm_id = next(i["permission_id"] for i in items if i["code"] == "role:grant")
    metric_perm_id = next(i["permission_id"] for i in items if i["code"] == "metric:read")
    current = (await client.get("/api/v1/org/roles/ROLE0003/permissions",
                                headers=auth(token))).json()["data"]["permission_ids"]

    resp = await client.put("/api/v1/org/roles/ROLE0003/permissions", headers=auth(token),
                            json={"permission_ids": [p for p in current
                                                     if p != metric_perm_id],
                                  "reason": "收回看板权限但不影响授权能力"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["removed"] == [metric_perm_id]
    assert grant_perm_id in resp.json()["data"]["permission_ids"]


async def test_grant_requires_role_grant_permission(client):
    """`PUT` 需要 `role:grant`（kb_admin 有 role:* 吗？没有——它只有 16 条业务权限）。"""
    kb = await token_of(client, KB_ADMIN)
    resp = await client.put("/api/v1/org/roles/ROLE0004/permissions", headers=auth(kb),
                            json={"permission_ids": [], "reason": "越权尝试一次"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "AUTH-2004"
    asker = await token_of(client, ASKER)
    assert (await client.get("/api/v1/org/roles/ROLE0004/permissions",
                             headers=auth(asker))).status_code == 403


# --------------------------------------------------------------------- 生效性
async def test_grant_takes_effect_on_the_next_request(client):
    """授了 `audit:read` 之后，被授权角色的人**下一次请求**就能读审计。"""
    token = await token_of(client, SYS_ADMIN)
    items = (await client.get("/api/v1/auth/permissions",
                              headers=auth(token))).json()["data"]["items"]
    audit_read = next(i["permission_id"] for i in items if i["code"] == "audit:read")
    kb_current = (await client.get("/api/v1/org/roles/ROLE0002/permissions",
                                   headers=auth(token))).json()["data"]["permission_ids"]
    assert audit_read not in kb_current

    kb_token = await token_of(client, KB_ADMIN)
    denied = await client.get("/api/v1/audit/logs", headers=auth(kb_token))
    assert denied.status_code == 403, "授权前读不到审计"

    await client.put("/api/v1/org/roles/ROLE0002/permissions", headers=auth(token),
                     json={"permission_ids": kb_current + [audit_read],
                           "reason": "让知识管理员也能查审计"})
    allowed = await client.get("/api/v1/audit/logs", headers=auth(kb_token))
    assert allowed.status_code == 200, "授权后必须立即生效（缓存已失效）"
