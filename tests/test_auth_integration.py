# -*- coding: utf-8 -*-
"""模块 01 集成测试：真连 MongoDB + ASGI 直连应用，逐条对应模块 Spec 的验收标准。

覆盖：AC-01-01 ~ AC-01-07、AC-01-11、AC-01-12（本切片可达部分）。
"""
from __future__ import annotations

import jwt
import pytest

from app.core.config import settings
from tests.conftest import DEMO_PASSWORD, DEMO_USERS, login, token_of

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------- 健康检查
async def test_health_reports_mongo_ok(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["dependencies"]["mongodb"] == "ok"
    assert body["data"]["status"] == "ok"
    # 未接入的依赖要如实说 "not_configured"，不能谎报 unavailable
    assert body["data"]["dependencies"]["milvus"] == "not_configured"


# ---------------------------------------------------------------- AC-01-01
async def test_login_success_returns_token_and_profile(client):
    resp = await login(client, DEMO_USERS["sys_admin"])
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["token_type"] == "Bearer"
    assert data["expires_in"] == settings.jwt_expire_seconds

    user = data["user"]
    assert user["username"] == "lina"
    assert user["real_name"] == "李娜"
    assert user["dept_id"] == "DEPT0005"
    assert user["dept_name"] == "总经办"                 # ← 跨集合读 E08 成功
    assert [r["code"] for r in user["roles"]] == ["sys_admin"]
    assert len(user["permissions"]) == 22
    # 按 path 去重后 sys_admin 有 4 个菜单：#/qa · #/dashboard · #/system · #/audit
    assert len(user["menus"]) == 4
    assert "password_hash" not in str(user)              # AC-01-11：绝不回显


# ---------------------------------------------------------------- AC-01-02
async def test_unknown_user_and_wrong_password_share_one_code(client):
    unknown = await login(client, "nobody_here")
    wrong = await login(client, DEMO_USERS["sys_admin"], "WrongPass123")
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["code"] == wrong.json()["code"] == "AUTH-2001"


# ---------------------------------------------------------------- AC-01-03
async def test_disabled_user_cannot_login(client):
    resp = await login(client, DEMO_USERS["disabled"])
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH-2002"


async def test_disabling_user_invalidates_existing_token(client):
    """账号被停用后，**已签发的令牌立即失效**（每请求校验 status）。"""
    from app.infra.mongo import mongo

    token = await token_of(client, DEMO_USERS["kb_admin"])
    ok = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200

    await mongo.collection("sys_users").update_one(
        {"username": DEMO_USERS["kb_admin"]}, {"$set": {"status": "disabled"}})
    try:
        blocked = await client.get("/api/v1/auth/me",
                                   headers={"Authorization": f"Bearer {token}"})
        assert blocked.status_code == 401
        assert blocked.json()["code"] == "AUTH-2002"
    finally:
        await mongo.collection("sys_users").update_one(
            {"username": DEMO_USERS["kb_admin"]}, {"$set": {"status": "active"}})


# ---------------------------------------------------------------- AC-01-04 / ER-08
@pytest.mark.parametrize("path", ["/api/v1/auth/me", "/api/v1/docs", "/api/v1/metrics/overview"])
async def test_protected_paths_reject_missing_token(client, path):
    resp = await client.get(path)
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH-2003"   # 未接的路径同样先被中间件拦住


async def test_whitelist_paths_need_no_token(client):
    assert (await client.get("/health")).status_code == 200
    assert (await login(client, DEMO_USERS["asker"])).status_code == 200


# ---------------------------------------------------------------- AC-01-06
async def test_me_returns_same_profile_as_login(client):
    token = await token_of(client, DEMO_USERS["asker"])
    resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    me = resp.json()["data"]
    assert me["username"] == "wangqiang"
    assert [r["code"] for r in me["roles"]] == ["asker"]
    assert me["permissions"] == ["perm:check", "qa:feedback", "qa:history", "qa:use"]


async def test_menus_follow_the_permission_matrix(client):
    """三个内置角色看到的菜单面必须与原型 `08_角色-功能权限矩阵.pen` 一致。

    注意 `sys_admin` **没有** `#/docs` 与 `#/sediment`——矩阵里系统管理员对
    「知识单元台账查看」「FAQ 审核」画的是「—」（管平台不管知识）。
    """
    expected = {
        "asker": ["#/qa"],
        "kb_admin": ["#/docs", "#/qa", "#/sediment"],
        "sys_admin": ["#/audit", "#/dashboard", "#/qa", "#/system"],
    }
    for role, want in expected.items():
        token = await token_of(client, DEMO_USERS[role])
        me = (await client.get("/api/v1/auth/me",
                               headers={"Authorization": f"Bearer {token}"})).json()["data"]
        paths = [m["path"] for m in me["menus"]]
        assert all(p.startswith("#/") for p in paths)
        assert sorted(set(paths)) == want, f"{role} 的菜单面应为 {want}"
        # 同一会话里 menus 不得重复
        assert len(paths) == len(set(paths))


# ---------------------------------------------------------------- 权限数对账（模块 01 §2.3 D 表）
async def test_permission_counts_match_spec(client):
    expected = {"asker": 4, "kb_admin": 16, "sys_admin": 22}
    for role, count in expected.items():
        token = await token_of(client, DEMO_USERS[role])
        me = (await client.get("/api/v1/auth/me",
                               headers={"Authorization": f"Bearer {token}"})).json()["data"]
        assert len(me["permissions"]) == count, f"{role} 权限数应为 {count}"


# ---------------------------------------------------------------- 错误处理与契约
async def test_parameter_validation_returns_SYS_1001(client):
    resp = await client.post("/api/v1/auth/login", json={"username": "lina", "password": "short"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "SYS-1001"


async def test_malformed_username_returns_AUTH_1001(client):
    """账号不符合 `^[a-zA-Z][a-zA-Z0-9_]{2,31}$` → 400 AUTH-1001（模块 01 §5）。"""
    resp = await login(client, "1bad name")
    assert resp.status_code == 400
    assert resp.json()["code"] == "AUTH-1001"


async def test_unknown_but_wellformed_username_returns_AUTH_2001(client):
    """格式合法但不存在 → 与密码错同码，防账号枚举。"""
    resp = await login(client, "nobody_here")
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH-2001"


async def test_bad_token_returns_AUTH_2003(client):
    resp = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH-2003"


async def test_token_signed_with_wrong_secret_is_rejected(client):
    forged = jwt.encode({"sub": "U000001", "iss": settings.jwt_issuer,
                         "iat": 1790000000, "exp": 4102444800},
                        "x" * 40, algorithm="HS256")
    resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH-2003"


async def test_envelope_and_trace_id_on_every_response(client):
    ok_resp = await client.get("/health")
    err_resp = await client.get("/api/v1/auth/me")
    for resp in (ok_resp, err_resp):
        assert set(resp.json()) == {"code", "message", "data", "trace_id"}
        assert resp.headers["X-Trace-Id"]
        assert len(resp.headers["X-Trace-Id"]) == 16
    assert err_resp.json()["data"] is None


async def test_unknown_path_returns_SYS_3001(client):
    token = await token_of(client, DEMO_USERS["sys_admin"])
    resp = await client.get("/api/v1/does-not-exist",
                            headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404
    assert resp.json()["code"] == "SYS-3001"


# ---------------------------------------------------------------- 端到端主链路
async def test_full_login_then_me_chain(client):
    """UI 会走的完整链路：登录拿 token → 带 token 取上下文。"""
    token = await token_of(client, DEMO_USERS["kb_admin"])
    me = (await client.get("/api/v1/auth/me",
                           headers={"Authorization": f"Bearer {token}"})).json()["data"]
    assert me["username"] == DEMO_USERS["kb_admin"]
    assert me["dept_name"] == "人力资源部"
    assert "doc:upload" in me["permissions"]
    assert "metric:read" not in me["permissions"]        # kb_admin 无看板权限（原型 08 的「—」）


async def test_password_hash_is_bcrypt_and_verifiable(client):
    from app.core.security import verify_password
    from app.infra.mongo import mongo

    doc = await mongo.collection("sys_users").find_one({"username": DEMO_USERS["asker"]})
    assert doc["password_hash"].startswith("$2b$")
    assert verify_password(DEMO_PASSWORD, doc["password_hash"]) is True
    assert verify_password("WrongPass123", doc["password_hash"]) is False
