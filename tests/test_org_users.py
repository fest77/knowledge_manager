# -*- coding: utf-8 -*-
"""模块 02 · 用户（E09）的测试。

对应验收：**AC-02-06**（密码只以 bcrypt 存在、任何响应不含明文）、
**AC-02-07**（`username` 大小写不敏感唯一）、**AC-02-08**（停用后已签发令牌立即失效）、
**AC-02-09**（停用最后一个 sys_admin / 停用自己都被拒）、**AC-02-14**（列表不返回联系方式）、
**AC-02-15**（写操作都有审计且不含密码）。

外加本模块自己的边界：`username` 不可改、角色绑定差集与"至少一个角色"、
口令强度、调岗后权限缓存立即失效。
"""
from __future__ import annotations

import json

import pytest

from app.core.errors import BizError
from app.infra.mongo import mongo
from app.repositories import audit_repo, org_repo
from app.services import org_service
from app.services.permission_cache import permission_cache
from tests.conftest import token_of

pytestmark = pytest.mark.anyio

SYS_ADMIN = "lina"
KB_ADMIN = "zhangwei"
ASKER = "wangqiang"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _new_user(client, token: str, **overrides) -> dict:
    body = {"username": "newbie", "password": "Passw0rd123", "real_name": "新人",
            "dept_id": "DEPT0002", "role_ids": ["ROLE0001"]}
    body.update(overrides)
    return await client.post("/api/v1/org/users", headers=auth(token), json=body)


# --------------------------------------------------------------------- 列表
async def test_list_users_returns_expected_columns(client):
    """AC-02-14：列表 7 列语义正确，且**不返回手机号/邮箱明文**。"""
    token = await token_of(client, SYS_ADMIN)
    resp = await client.get("/api/v1/org/users", headers=auth(token))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["total"] == 4 and data["page"] == 1 and data["page_size"] == 20

    row = next(i for i in data["items"] if i["username"] == "zhangwei")
    assert row["real_name"] == "张伟"
    assert row["dept_name"] == "人力资源部"
    assert row["roles"] == [{"role_id": "ROLE0002", "code": "kb_admin",
                             "name": "知识管理员"}]
    assert row["status"] == "active"
    assert "phone" not in row and "email" not in row and "password_hash" not in row


async def test_list_users_keyword_matches_name_and_username(client):
    """关键字同时匹配 `real_name` 与 `username`（中文子串也要能搜到）。"""
    token = await token_of(client, SYS_ADMIN)
    by_name = await client.get("/api/v1/org/users?keyword=张", headers=auth(token))
    assert by_name.json()["data"]["total"] == 1
    assert by_name.json()["data"]["items"][0]["username"] == "zhangwei"

    by_username = await client.get("/api/v1/org/users?keyword=wang", headers=auth(token))
    assert by_username.json()["data"]["total"] == 1
    assert by_username.json()["data"]["items"][0]["real_name"] == "王强"

    by_dept = await client.get("/api/v1/org/users?dept_id=DEPT0004", headers=auth(token))
    assert by_dept.json()["data"]["total"] == 1
    by_status = await client.get("/api/v1/org/users?status=disabled", headers=auth(token))
    assert by_status.json()["data"]["total"] == 1, "赵磊是停用账号"


async def test_list_users_pagination(client):
    token = await token_of(client, SYS_ADMIN)
    page = await client.get("/api/v1/org/users?page=2&page_size=3", headers=auth(token))
    data = page.json()["data"]
    assert data["total"] == 4 and len(data["items"]) == 1 and data["page"] == 2


async def test_user_endpoints_require_permission(client):
    asker = await token_of(client, ASKER)
    for method, path in (("get", "/api/v1/org/users"), ("post", "/api/v1/org/users"),
                         ("put", "/api/v1/org/users/U000001"),
                         ("post", "/api/v1/org/users/U000001/status"),
                         ("post", "/api/v1/org/users/U000001/reset-password"),
                         ("put", "/api/v1/org/users/U000001/roles")):
        resp = await client.request(method.upper(), path, headers=auth(asker), json={})
        assert resp.status_code == 403, f"{method} {path}"
        assert resp.json()["code"] == "AUTH-2004"


# --------------------------------------------------------------------- 新增
async def test_ac_02_06_password_is_bcrypt_and_never_echoed(client):
    """AC-02-06：库里是 bcrypt 串；**任何响应都不含明文密码**。"""
    token = await token_of(client, SYS_ADMIN)
    resp = await _new_user(client, token)
    assert resp.status_code == 200, resp.text
    user_id = resp.json()["data"]["user_id"]
    assert "Passw0rd123" not in resp.text

    row = await mongo.collection(org_repo.USERS).find_one({"_id": user_id})
    assert row["password_hash"].startswith("$2b$")
    assert "Passw0rd123" not in json.dumps(row, default=str)

    listing = await client.get("/api/v1/org/users?keyword=newbie", headers=auth(token))
    assert "Passw0rd123" not in listing.text
    detail = await client.get("/api/v1/org/users?keyword=newbie", headers=auth(token))
    assert "password_hash" not in detail.text


async def test_create_user_binds_roles_and_lands_audit(client):
    token = await token_of(client, SYS_ADMIN)
    resp = await _new_user(client, token, username="audit_user",
                           role_ids=["ROLE0001", "ROLE0002"])
    user_id = resp.json()["data"]["user_id"]
    assert sorted(await org_repo.role_ids_of_user(user_id)) == ["ROLE0001", "ROLE0002"]

    row = await mongo.collection(audit_repo.AUDIT_LOGS).find_one(
        {"action": "user.create", "target_id": user_id})
    assert row is not None
    assert row["after"]["role_ids"] == ["ROLE0001", "ROLE0002"]
    assert row["after"]["dept_id"] == "DEPT0002"
    assert "password" not in json.dumps(row, ensure_ascii=False, default=str)


async def test_ac_02_07_username_is_unique_case_insensitively(client):
    """AC-02-07：`Knowledge` 与 `knowledge` 视为冲突。"""
    token = await token_of(client, SYS_ADMIN)
    assert (await _new_user(client, token, username="Knowledge")).status_code == 200
    dup = await _new_user(client, token, username="knowledge")
    assert dup.status_code == 409
    assert dup.json()["code"] == "ORG-2001"


async def test_create_user_validations(client):
    token = await token_of(client, SYS_ADMIN)
    weak = await _new_user(client, token, username="weakpwd", password="abcdefgh")
    assert weak.json()["code"] == "ORG-1001", "缺数字"
    weak2 = await _new_user(client, token, username="weakpwd2", password="12345678")
    assert weak2.json()["code"] == "ORG-1001", "缺字母"
    bad_dept = await _new_user(client, token, username="baddept", dept_id="DEPT9999")
    assert bad_dept.json()["code"] == "ORG-3002"
    bad_role = await _new_user(client, token, username="badrole", role_ids=["ROLE9999"])
    assert bad_role.json()["code"] == "ORG-2002"
    bad_name = await _new_user(client, token, username="1abc")
    assert bad_name.json()["code"] == "SYS-1001", "账号必须以字母开头"
    # 停用的部门不能挂人
    await org_repo.update_dept("DEPT0007", {"status": "disabled"})
    disabled_dept = await _new_user(client, token, username="disdept", dept_id="DEPT0007")
    assert disabled_dept.json()["code"] == "ORG-3002"


# --------------------------------------------------------------------- 编辑
async def test_update_user_changes_fields_and_audits(client):
    token = await token_of(client, SYS_ADMIN)
    resp = await client.put("/api/v1/org/users/U000003", headers=auth(token),
                            json={"real_name": "王强强", "phone": "13800001111"})
    assert resp.status_code == 200, resp.text
    assert set(resp.json()["data"]["changed"]) == {"real_name", "phone"}

    row = await mongo.collection(org_repo.USERS).find_one({"_id": "U000003"})
    assert row["real_name"] == "王强强" and row["phone"] == "13800001111"

    audit = await mongo.collection(audit_repo.AUDIT_LOGS).find_one(
        {"action": "user.update", "target_id": "U000003"})
    assert audit is not None
    # 联系方式**要进审计**：审计要回答"谁把它改成了什么"，而审计只有 audit:read
    # （仅系统管理员）能看。列表接口不返回它是因为列表是广谱读取，两者的边界不同。
    assert audit["after"]["phone"] == "13800001111"
    assert audit["before"]["real_name"] == "王强"


async def test_username_is_immutable(client):
    """`ORG-1002`：登录账号改了会破坏审计可追溯性。"""
    token = await token_of(client, SYS_ADMIN)
    resp = await client.put("/api/v1/org/users/U000003", headers=auth(token),
                            json={"username": "wangqiang2"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "ORG-1002"
    # 传与现状相同的账号名不算改动
    same = await client.put("/api/v1/org/users/U000003", headers=auth(token),
                            json={"username": "wangqiang", "real_name": "王强"})
    assert same.status_code == 200
    assert same.json()["data"]["changed"] == {}


async def test_user_not_found_reports_org_3009(client):
    """`ORG-3009`（Step 5 补录）：Spec §5 初稿漏了"用户不存在"这一表达。"""
    token = await token_of(client, SYS_ADMIN)
    resp = await client.put("/api/v1/org/users/U999999", headers=auth(token),
                            json={"real_name": "谁"})
    assert resp.status_code == 404
    assert resp.json()["code"] == "ORG-3009"


async def test_transfer_invalidates_permission_cache_immediately(client):
    """调岗 → 权限缓存立即失效（下一次请求按新部门判定，AD-02 的落点）。"""
    token = await token_of(client, SYS_ADMIN)
    asker_token = await token_of(client, ASKER)          # 触发一次缓存写入
    assert permission_cache.get("U000003") is not None
    assert asker_token

    resp = await client.put("/api/v1/org/users/U000003", headers=auth(token),
                            json={"dept_id": "DEPT0006"})
    assert resp.status_code == 200
    assert permission_cache.get("U000003") is None, "调岗后缓存必须立即作废"


# --------------------------------------------------------------------- 停用 / 启用
async def test_ac_02_09_cannot_disable_last_admin_or_self(client):
    """AC-02-09：停用最后一个 sys_admin 被拒；停用自己也被拒。"""
    token = await token_of(client, SYS_ADMIN)
    # lina 是唯一的 sys_admin → 既不能停自己，也不能被停
    self_disable = await client.post("/api/v1/org/users/U000001/status",
                                     headers=auth(token), json={"status": "disabled"})
    assert self_disable.status_code == 409
    assert self_disable.json()["code"] == "ORG-2004", "先命中'不能停用自己'"

    # 造第二个管理员后再停用自己 → 这次被"不能停自己"挡住，说明规则独立生效
    created = await _new_user(client, token, username="admin2", role_ids=["ROLE0003"])
    admin2 = created.json()["data"]["user_id"]
    still_self = await client.post("/api/v1/org/users/U000001/status",
                                   headers=auth(token), json={"status": "disabled"})
    assert still_self.json()["code"] == "ORG-2004"

    # 停用 admin2 是允许的（还有 lina 在）
    ok = await client.post(f"/api/v1/org/users/{admin2}/status", headers=auth(token),
                           json={"status": "disabled"})
    assert ok.status_code == 200 and ok.json()["data"]["status"] == "disabled"
    # 再想停用 lina —— 现在只剩她一个 active 管理员，且不是自己（换个管理员来操作不了），
    # 所以直接验证服务层判据：active 管理员数为 1
    assert await org_repo.count_active_users_with_role("ROLE0003") == 1


async def test_last_admin_guard_is_enforced_by_the_service(client):
    """`ORG-2003` 的直接验证：只剩一个 active 管理员时不允许再停用管理员。

    **为什么必须在服务层测**：`user:disable` 只授予 `sys_admin`，所以"操作人"自己就是
    管理员——只要还有第二个 active 管理员，停用其中一个就永远能过（2 降到 1）。
    真正会触发 `ORG-2003` 的只有"目标就是最后一个 active 管理员"，而那时操作人只可能
    是他自己（先撞 `ORG-2004`）。从 HTTP 层构造不出来，只能直调服务层
    —— 这恰好也是"服务层兜底"这类设计存在的理由。
    """
    token = await token_of(client, SYS_ADMIN)
    created = await _new_user(client, token, username="admin3", role_ids=["ROLE0003"])
    assert created.status_code == 200
    await org_repo.update_user("U000001", {"status": "disabled"})   # 只剩 admin3 一个

    assert await org_repo.count_active_users_with_role("ROLE0003") == 1
    with pytest.raises(BizError) as exc:
        await org_service.set_user_status(user_id=created.json()["data"]["user_id"],
                                          status="disabled", reason=None,
                                          actor_id="U000002", request=None)
    assert exc.value.spec.code == "ORG-2003"


async def test_ac_02_08_disabled_user_token_dies_immediately(client):
    """AC-02-08：停用后**已签发的令牌下一跳即失效**。"""
    admin_token = await token_of(client, SYS_ADMIN)
    victim_token = await token_of(client, ASKER)
    ok = await client.get("/api/v1/auth/me", headers=auth(victim_token))
    assert ok.status_code == 200

    disable = await client.post("/api/v1/org/users/U000003/status",
                                headers=auth(admin_token), json={"status": "disabled"})
    assert disable.status_code == 200

    after = await client.get("/api/v1/auth/me", headers=auth(victim_token))
    assert after.status_code == 401
    assert after.json()["code"] == "AUTH-2002"

    # 启用后令牌又能用了
    await client.post("/api/v1/org/users/U000003/status", headers=auth(admin_token),
                      json={"status": "active"})
    again = await client.get("/api/v1/auth/me", headers=auth(victim_token))
    assert again.status_code == 200


async def test_status_change_is_audited_with_before_after(client):
    token = await token_of(client, SYS_ADMIN)
    await client.post("/api/v1/org/users/U000003/status", headers=auth(token),
                      json={"status": "disabled", "reason": "离职交接中"})
    row = await mongo.collection(audit_repo.AUDIT_LOGS).find_one(
        {"action": "user.disable", "target_id": "U000003"})
    assert row is not None and row["before"]["status"] == "active"
    assert row["after"]["status"] == "disabled"
    assert row["reason"] == "离职交接中"


# --------------------------------------------------------------------- 重置密码
async def test_reset_password_stores_new_hash_without_leaking(client):
    token = await token_of(client, SYS_ADMIN)
    before = await mongo.collection(org_repo.USERS).find_one({"_id": "U000003"})
    resp = await client.post("/api/v1/org/users/U000003/reset-password",
                             headers=auth(token), json={"new_password": "Brand@New1"})
    assert resp.status_code == 200, resp.text
    assert "Brand@New1" not in resp.text

    after = await mongo.collection(org_repo.USERS).find_one({"_id": "U000003"})
    assert after["password_hash"] != before["password_hash"]
    assert after["password_hash"].startswith("$2b$")

    # 新密码能登录
    login = await client.post("/api/v1/auth/login",
                              json={"username": "wangqiang", "password": "Brand@New1"})
    assert login.status_code == 200

    audit = await mongo.collection(audit_repo.AUDIT_LOGS).find_one(
        {"action": "user.reset_pwd", "target_id": "U000003"})
    dump = json.dumps(audit, ensure_ascii=False, default=str)
    assert "Brand@New1" not in dump and "password" not in dump.lower().replace(
        "password_hash", ""), "审计里不该出现任何密码字段"

    weak = await client.post("/api/v1/org/users/U000003/reset-password",
                             headers=auth(token), json={"new_password": "abcdefgh"})
    assert weak.json()["code"] == "ORG-1001"


# --------------------------------------------------------------------- 绑定角色
async def test_set_user_roles_is_diff_based_and_audited(client):
    token = await token_of(client, SYS_ADMIN)
    resp = await client.put("/api/v1/org/users/U000003/roles", headers=auth(token),
                            json={"role_ids": ["ROLE0001", "ROLE0002"],
                                  "reason": "临时支援知识库维护"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["added"] == ["ROLE0002"] and data["removed"] == []
    assert data["role_ids"] == ["ROLE0001", "ROLE0002"]

    # 再改一次：移除 ROLE0001、加回…（差集必须精确）
    again = await client.put("/api/v1/org/users/U000003/roles", headers=auth(token),
                             json={"role_ids": ["ROLE0002"], "reason": "支援结束收回权限"})
    assert again.json()["data"]["added"] == []
    assert again.json()["data"]["removed"] == ["ROLE0001"]

    row = await mongo.collection(audit_repo.AUDIT_LOGS).find_one(
        {"action": "user.role_change", "target_id": "U000003"})
    assert row is not None and row["reason"] == "支援结束收回权限"
    assert row["before"]["role_ids"] == ["ROLE0001", "ROLE0002"]
    assert row["after"]["role_ids"] == ["ROLE0002"]


async def test_set_user_roles_validations(client):
    token = await token_of(client, SYS_ADMIN)
    empty = await client.put("/api/v1/org/users/U000003/roles", headers=auth(token),
                             json={"role_ids": [], "reason": "清空试试看"})
    assert empty.status_code == 400

    missing = await client.put("/api/v1/org/users/U000003/roles", headers=auth(token),
                               json={"role_ids": ["ROLE9999"], "reason": "不存在的角色"})
    assert missing.json()["code"] == "ORG-2002"

    short_reason = await client.put("/api/v1/org/users/U000003/roles", headers=auth(token),
                                    json={"role_ids": ["ROLE0001"], "reason": "短"})
    assert short_reason.status_code == 400

    no_user = await client.put("/api/v1/org/users/U999999/roles", headers=auth(token),
                               json={"role_ids": ["ROLE0001"], "reason": "用户不存在"})
    assert no_user.json()["code"] == "ORG-3009"


async def test_role_change_invalidates_cache(client):
    token = await token_of(client, SYS_ADMIN)
    await token_of(client, ASKER)
    assert permission_cache.get("U000003") is not None
    await client.put("/api/v1/org/users/U000003/roles", headers=auth(token),
                     json={"role_ids": ["ROLE0001", "ROLE0002"], "reason": "变更触发失效"})
    assert permission_cache.get("U000003") is None


# --------------------------------------------------------------------- 降级
async def test_audit_failure_does_not_block_user_operations(client, monkeypatch):
    """AC-02-16 的用户侧：审计挂掉，新增/停用/重置密码仍然成功。"""
    async def down(_doc):
        raise RuntimeError("模拟 audit_logs 不可写")

    monkeypatch.setattr(audit_repo, "insert", down)
    token = await token_of(client, SYS_ADMIN)
    created = await _new_user(client, token, username="resilient")
    assert created.status_code == 200
    user_id = created.json()["data"]["user_id"]
    assert (await client.post(f"/api/v1/org/users/{user_id}/status", headers=auth(token),
                              json={"status": "disabled"})).status_code == 200
    assert (await client.post(f"/api/v1/org/users/{user_id}/reset-password",
                              headers=auth(token),
                              json={"new_password": "Resilient1"})).status_code == 200
