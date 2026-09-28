# -*- coding: utf-8 -*-
"""模块 01 的**深度边界测试**。

与 `test_auth_integration.py` 的分工：那边测「按 Spec 该有的行为」，这边专门找
「想不到的输入」——并发、注入、令牌伪造、多角色并集、缺部门、缺角色、编码边界。
"""
from __future__ import annotations

import asyncio
import base64
import json
import time

import jwt
import pytest

from app.core.config import settings
from app.core.errors import BizError, Err
from app.core.security import hash_password, verify_password
from app.infra.mongo import Mongo, mongo
from app.repositories import org_repo
from tests.conftest import DEMO_PASSWORD, DEMO_USERS, login, token_of

pytestmark = pytest.mark.anyio


# ================================================================ 一、账号与凭据边界
async def test_username_is_case_insensitive(client):
    """Spec（模块 02 §3.2）要求账号唯一性**大小写不敏感**；登录也应一致。"""
    resp = await login(client, "LINA")
    assert resp.status_code == 200
    assert resp.json()["data"]["user"]["username"] == "lina"


async def test_username_with_surrounding_spaces_is_not_trimmed_by_server(client):
    """服务端**不**做 trim：前端已 trim，服务端保持严格，避免"看起来一样却登不上"。"""
    resp = await login(client, " lina ")
    assert resp.status_code == 400
    assert resp.json()["code"] == "AUTH-1001"


async def test_password_over_72_bytes_is_rejected_not_crashed(client):
    """bcrypt 5.0 对 >72 字节直接抛错；服务端必须先挡住，返回业务码而不是 500。"""
    resp = await login(client, DEMO_USERS["asker"], "A1" + "x" * 80)
    assert resp.status_code == 400
    assert resp.json()["code"] == "AUTH-1001"
    assert "72" in resp.json()["message"]


async def test_password_with_multibyte_chars_round_trips(client):
    """中文密码要能正确哈希与校验（UTF-8 编码边界）。"""
    plain = "中文密码测试一二三"           # 27 字节，在 8~72 区间
    h = hash_password(plain)
    assert verify_password(plain, h) is True
    assert verify_password(plain + "x", h) is False


async def test_same_password_hashes_differently(client):
    """bcrypt 必须加盐：同一密码两次哈希结果不同，且都能校验通过。"""
    h1, h2 = hash_password("SamePass123"), hash_password("SamePass123")
    assert h1 != h2
    assert verify_password("SamePass123", h1) and verify_password("SamePass123", h2)


async def test_corrupted_hash_fails_closed_not_500(client):
    """库里哈希串损坏时，校验应返回 False（→ AUTH-2001），而不是抛异常变 500。"""
    await mongo.collection(org_repo.USERS).update_one(
        {"username": DEMO_USERS["asker"]}, {"$set": {"password_hash": "not-a-bcrypt-hash"}})
    resp = await login(client, DEMO_USERS["asker"])
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH-2001"


# ================================================================ 二、注入与畸形输入
@pytest.mark.parametrize("bad", [
    {"$ne": None},                       # NoSQL 操作符注入
    {"$regex": ".*"},                    # 正则注入
    ["lina"],                            # 类型混淆
    123456,                              # 类型混淆
    None,
])
async def test_nosql_injection_in_username_is_rejected(client, bad):
    """Pydantic 强制 `str`，字典/列表/数字一律在参数层被挡，**绝不落到 Mongo 查询**。"""
    resp = await client.post("/api/v1/auth/login",
                             json={"username": bad, "password": DEMO_PASSWORD})
    assert resp.status_code == 400
    assert resp.json()["code"] == "SYS-1001"


@pytest.mark.parametrize("bad", ["admin' OR 1=1", "a.*", "a$b", "../../etc/passwd", "lina;--"])
async def test_usernames_outside_charset_are_rejected(client, bad):
    """正则注入、路径穿越、SQL 片段都过不了 `^[a-zA-Z][a-zA-Z0-9_]{2,31}$`。"""
    resp = await login(client, bad)
    assert resp.status_code == 400
    assert resp.json()["code"] == "AUTH-1001"


async def test_extra_fields_are_rejected(client):
    """`extra="forbid"`：前端多传字段要报错，不能静默忽略。"""
    resp = await client.post("/api/v1/auth/login",
                             json={"username": "lina", "password": DEMO_PASSWORD,
                                   "is_admin": True})
    assert resp.status_code == 400
    assert resp.json()["code"] == "SYS-1001"


async def test_malformed_json_body_returns_envelope_not_html(client):
    """非法 JSON 不能返回 Starlette 默认的 HTML 错误页。"""
    resp = await client.post("/api/v1/auth/login", content="{not json",
                             headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    assert set(resp.json()) == {"code", "message", "data", "trace_id"}


async def test_wrong_method_on_login_returns_SYS_3002(client):
    resp = await client.get("/api/v1/auth/login")
    assert resp.status_code == 405
    assert resp.json()["code"] == "SYS-3002"


async def test_oversized_body_is_rejected_without_crash(client):
    resp = await client.post("/api/v1/auth/login",
                             json={"username": "lina", "password": "x" * 5_000_000})
    assert resp.status_code in (400, 413)
    assert resp.status_code != 500


# ================================================================ 三、令牌伪造与过期
async def _craft(payload: dict, key: str = "", alg: str = "HS256") -> str:
    return jwt.encode(payload, key or settings.jwt_secret, algorithm=alg)


async def test_expired_token_is_rejected(client):
    token = await _craft({"sub": "U000001", "jti": "x", "iss": settings.jwt_issuer,
                          "iat": int(time.time()) - 100, "exp": int(time.time()) - 10})
    resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH-2003"


async def test_token_without_required_claims_is_rejected(client):
    """缺 `iss` / `exp` / `iat` 的令牌必须拒绝（`options.require` 生效）。"""
    for payload in [
        {"sub": "U000001", "exp": int(time.time()) + 600, "iat": int(time.time())},
        {"sub": "U000001", "iss": settings.jwt_issuer, "iat": int(time.time())},
        {"iss": settings.jwt_issuer, "exp": int(time.time()) + 600, "iat": int(time.time())},
    ]:
        token = await _craft(payload)
        resp = await client.get("/api/v1/auth/me",
                                headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401, payload
        assert resp.json()["code"] == "AUTH-2003"


async def test_alg_none_attack_is_rejected(client):
    """经典的 `alg: none` 绕过：手工拼一个无签名令牌，必须被拒。"""
    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    header = b64({"alg": "none", "typ": "JWT"})
    body = b64({"sub": "U000001", "iss": settings.jwt_issuer,
                "iat": 1, "exp": 4102444800})
    forged = f"{header}.{body}."          # 第三段（签名）为空 —— 经典 alg:none 绕过
    resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH-2003"


async def test_token_with_wrong_issuer_is_rejected(client):
    token = await _craft({"sub": "U000001", "jti": "x", "iss": "somebody_else",
                          "iat": int(time.time()), "exp": int(time.time()) + 600})
    resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH-2003"


async def test_token_of_deleted_user_is_rejected(client):
    token = await token_of(client, DEMO_USERS["asker"])
    await mongo.collection(org_repo.USERS).delete_one({"username": DEMO_USERS["asker"]})
    resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH-2003"


@pytest.mark.parametrize("header", ["", "Bearer", "Bearer  ", "Basic abc", "abc", "bearer"])
async def test_malformed_authorization_header_is_rejected(client, header):
    resp = await client.get("/api/v1/auth/me", headers={"Authorization": header})
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH-2003"


async def test_bearer_scheme_is_case_insensitive(client):
    """`bearer` / `BEARER` 都应被接受（RFC 7235 规定 scheme 大小写不敏感）。"""
    token = await token_of(client, DEMO_USERS["asker"])
    for scheme in ("bearer", "BEARER", "BeArEr"):
        resp = await client.get("/api/v1/auth/me",
                                headers={"Authorization": f"{scheme} {token}"})
        assert resp.status_code == 200, scheme


# ================================================================ 四、多角色 / 缺角色 / 缺部门
async def test_multi_role_user_gets_union_of_permissions(client):
    """E11 是多对多；给同一用户挂两个角色时，权限与菜单都应是**并集**。"""
    user_id = "U000003"                       # wangqiang，原只有 asker
    now = int(time.time())
    for role_id in ("ROLE0002", "ROLE0003"):  # kb_admin + sys_admin
        await mongo.collection(org_repo.USER_ROLES).update_one(
            {"_id": f"{user_id}:{role_id}"},
            {"$set": {"user_id": user_id, "role_id": role_id,
                      "granted_by": "test", "granted_at": now}}, upsert=True)

    token = await token_of(client, DEMO_USERS["asker"])
    me = (await client.get("/api/v1/auth/me",
                           headers={"Authorization": f"Bearer {token}"})).json()["data"]
    # asker(4) ∪ kb_admin 的 12 ∪ sys_admin 的 18 = 34
    assert len(me["permissions"]) == 34
    assert [r["code"] for r in me["roles"]] == ["asker", "kb_admin", "sys_admin"] or \
           sorted(r["code"] for r in me["roles"]) == ["asker", "kb_admin", "sys_admin"]
    assert sorted({m["path"] for m in me["menus"]}) == \
        ["#/audit", "#/dashboard", "#/docs", "#/qa", "#/sediment", "#/system"]


async def test_user_without_any_role_has_no_permissions_but_can_still_login(client):
    """无角色用户：能登录（认证与授权是两件事），但权限与菜单都为空。"""
    await mongo.collection(org_repo.USER_ROLES).delete_many({"user_id": "U000003"})
    resp = await login(client, DEMO_USERS["asker"])
    assert resp.status_code == 200
    user = resp.json()["data"]["user"]
    assert user["roles"] == []
    assert user["permissions"] == []
    assert user["menus"] == []


async def test_user_with_missing_department_gets_null_dept_name(client):
    """`dept_id` 指向不存在的部门时，`dept_name` 为 null，但登录不能失败。"""
    await mongo.collection(org_repo.USERS).update_one(
        {"username": DEMO_USERS["asker"]}, {"$set": {"dept_id": "DEPT9999"}})
    resp = await login(client, DEMO_USERS["asker"])
    assert resp.status_code == 200
    assert resp.json()["data"]["user"]["dept_name"] is None


async def test_role_without_any_permission_yields_empty_permission_list(client):
    """业务角色（如 management）默认 0 功能权限；只挂它时应为 0 权限 0 菜单。"""
    await mongo.collection(org_repo.USER_ROLES).delete_many({"user_id": "U000003"})
    await mongo.collection(org_repo.USER_ROLES).insert_one(
        {"_id": "U000003:ROLE0004", "user_id": "U000003", "role_id": "ROLE0004",
         "granted_by": "test", "granted_at": int(time.time())})
    resp = await login(client, DEMO_USERS["asker"])
    user = resp.json()["data"]["user"]
    assert [r["code"] for r in user["roles"]] == ["management"]
    assert user["permissions"] == []


async def test_menus_are_sorted_by_sort_ascending(client):
    token = await token_of(client, DEMO_USERS["sys_admin"])
    me = (await client.get("/api/v1/auth/me",
                           headers={"Authorization": f"Bearer {token}"})).json()["data"]
    sorts = [m["sort"] for m in me["menus"]]
    assert sorts == sorted(sorts)


# ================================================================ 五、并发与可观测
async def test_concurrent_logins_all_succeed(client):
    """10 个并发登录不应出现串号、连接池耗尽或非 200。"""
    results = await asyncio.gather(*[
        login(client, acc) for acc in
        [DEMO_USERS["asker"], DEMO_USERS["kb_admin"], DEMO_USERS["sys_admin"]] * 4
    ])
    assert len(results) == 12
    assert all(r.status_code == 200 for r in results)
    tokens = {r.json()["data"]["access_token"] for r in results}
    assert len(tokens) == 12, "并发登录应各自签发不同令牌"


async def test_trace_id_is_unique_per_request(client):
    ids = set()
    for _ in range(6):
        resp = await client.get("/health")
        ids.add(resp.headers["X-Trace-Id"])
    assert len(ids) == 6
    assert all(len(i) == 16 for i in ids)


async def test_last_login_at_is_backfilled_on_success_only(client):
    before = await mongo.collection(org_repo.USERS).find_one(
        {"username": DEMO_USERS["kb_admin"]})
    assert before.get("last_login_at") is None

    await login(client, DEMO_USERS["kb_admin"], "WrongPass123")     # 失败
    after_fail = await mongo.collection(org_repo.USERS).find_one(
        {"username": DEMO_USERS["kb_admin"]})
    assert after_fail.get("last_login_at") is None

    await login(client, DEMO_USERS["kb_admin"])                     # 成功
    after_ok = await mongo.collection(org_repo.USERS).find_one(
        {"username": DEMO_USERS["kb_admin"]})
    assert isinstance(after_ok.get("last_login_at"), int)


# ================================================================ 六、基础设施兜底
async def test_ensure_indexes_repairs_stale_collation(client):
    """回归测试：正式库若残留「大小写敏感」的旧 `uq_username`，启动时必须自动替换。

    这个 bug 是实测发现的——`create_index` 对同名同键索引幂等，collation 变了也不重建，
    于是「测试库全绿、正式库大小写敏感」。测试库每次 drop 重建，掩盖了它，
    因此这里**故意先造一个错索引**再调 `ensure_indexes()`。
    """
    db = mongo.require_db()
    await db[org_repo.USERS].drop_index("uq_username")
    await db[org_repo.USERS].create_index("username", unique=True, name="uq_username")
    stale = next(i for i in await (await db[org_repo.USERS].list_indexes()).to_list(None)
                 if i["name"] == "uq_username")
    assert stale.get("collation") is None, "前置条件：旧索引应无 collation"

    await org_repo.ensure_indexes()

    fixed = next(i for i in await (await db[org_repo.USERS].list_indexes()).to_list(None)
                 if i["name"] == "uq_username")
    assert fixed.get("collation", {}).get("locale") == "en"
    assert fixed.get("collation", {}).get("strength") == 2
    assert fixed.get("unique") is True
    # 修好之后大小写不敏感必须真的生效
    assert (await login(client, "LiNa")).status_code == 200


async def test_ensure_indexes_is_idempotent_and_does_not_churn(client):
    """幂等性：连续调用两次不应反复删建索引。

    读回的 `collation` 字段（13 个）比 `Collation.document`（2 个）多，
    若整体比较就会永远不等 → 每次启动都删重建。这里把这个坑钉死。
    """
    db = mongo.require_db()

    async def collation_of() -> dict:
        rows = await (await db[org_repo.USERS].list_indexes()).to_list(None)
        return next(i for i in rows if i["name"] == "uq_username").get("collation", {})

    await org_repo.ensure_indexes()
    first = await collation_of()
    await org_repo.ensure_indexes()
    second = await collation_of()
    assert first == second, "两次 ensure_indexes 后 collation 不应变化"
    assert first.get("strength") == 2


async def test_fresh_mongo_instance_reports_down_and_raises_biz_error():
    """未连接时：`ping()` 返回 False、`require_db()` 抛 SYS-4001（不是 AttributeError）。"""
    fresh = Mongo()
    assert await fresh.ping() is False
    with pytest.raises(BizError) as exc:
        fresh.require_db()
    assert exc.value.spec.code == Err.SYS_DB_UNAVAILABLE.code


async def test_health_reports_degraded_when_db_is_gone():
    """库不可达时 /health 必须报 `degraded`，且**不暴露**内部错误细节。

    这里依赖 `client` 夹具的 teardown 已经 `mongo.close()`（close 会把句柄置空），
    因此本用例不申请 `client` 夹具也能拿到"未连接"状态；但为保证顺序无关，
    显式再关一次并断言句柄确实为空。
    """
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    await mongo.close()
    assert mongo.db is None and mongo.client is None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await c.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["status"] == "degraded"
    assert body["data"]["dependencies"]["mongodb"] == "unavailable"
    text = json.dumps(body).lower()
    assert "traceback" not in text and "pymongo" not in text
