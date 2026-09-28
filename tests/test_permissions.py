# -*- coding: utf-8 -*-
"""功能权限拦截的测试（总纲 ER-08 认证 / ER-09 授权）。

覆盖三个层次：
  1. **纯单元**：`require_perm` / `required_perms_of` 的声明与读回
  2. **集成**：真路由上 `AUTH-2004` 拦截是否生效（`/api/v1/system/config`）
  3. **边界**：无角色用户、只读不写、白名单不受影响
"""
from __future__ import annotations

import pytest

from app.api.deps import enforce_perm
from app.core.errors import BizError, Err
from app.core.permissions import require_perm, required_perms_of
from app.infra.mongo import mongo
from app.repositories import auth_repo, org_repo
from tests.conftest import DEMO_USERS, token_of

pytestmark = pytest.mark.anyio


# ================================================================ 一、单元
def test_require_perm_declares_codes():
    """`require_perm` 把权限码挂在函数上，`required_perms_of` 能读回。

    权限码一律取自 34 码字典（此处的 `doc:read` / `doc:edit` 都是真实存在的码）。
    """

    @require_perm("doc:read", "doc:edit")
    def handler():
        return None

    class FakeRoute:
        endpoint = staticmethod(handler)

    assert required_perms_of(FakeRoute) == ("doc:read", "doc:edit")


def test_require_perm_rejects_empty_declaration():
    """空声明是写错了（永不放行的权限），必须当场报错而不是静默通过。"""
    with pytest.raises(ValueError, match="至少要一个权限码"):
        require_perm()


def test_required_perms_of_tolerates_no_route():
    """未命中路由 / 路由未声明权限码 → 空元组（放行），由 404 处理器负责其它情况。"""
    assert required_perms_of(None) == ()

    def plain():
        return None

    class FakeRoute:
        endpoint = staticmethod(plain)

    assert required_perms_of(FakeRoute) == ()


def test_middleware_envelopes_and_dependency_share_one_shape():
    """`fail()` 与 `envelope()` 是信封的唯一定义处，两个出口必须同形。"""
    from app.core.response import envelope, fail

    assert set(envelope(0, "ok", {"a": 1}, "tid")) == {"code", "message", "data", "trace_id"}
    body = fail("AUTH-2004", "功能权限不足", 403, "tid")
    assert body.status_code == 403
    assert set(body.body and __import__("json").loads(body.body)) == {
        "code", "message", "data", "trace_id"}


# ================================================================ 二、真路由上的拦截
@pytest.mark.parametrize("account", [DEMO_USERS["kb_admin"], DEMO_USERS["asker"]])
async def test_perm_denied_returns_AUTH_2004(client, account):
    """`kb_admin` / `asker` 都没有 `model:config` → 读配置应 403 AUTH-2004。"""
    token = await token_of(client, account)
    resp = await client.get("/api/v1/system/config",
                            headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "AUTH-2004"
    assert body["data"] is None
    assert "model:config" in body["message"]


async def test_perm_granted_passes(client):
    """`sys_admin` 有 `model:config` → 读配置 200。"""
    token = await token_of(client, DEMO_USERS["sys_admin"])
    resp = await client.get("/api/v1/system/config",
                            headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["code"] == 0


async def test_write_perm_is_checked_separately_from_read_perm(client):
    """`system:config` 与 `model:config` 是两个码；只有其中一个时行为要分开。

    这里给 `wangqiang` 单独授予 `model:config`（能读配置），但不给 `system:config`（不能写）。
    权限 id 按 code 查库取得——**不硬编码 `PERMxxxx`**，否则预置表一调整测试就假失败。
    """
    perm = await mongo.collection(auth_repo.PERMISSIONS).find_one({"code": "model:config"})
    assert perm is not None, "预置权限字典里必须有 model:config"
    await mongo.collection(auth_repo.ROLE_PERMISSIONS).insert_one(
        {"_id": f"ROLE0001:{perm['_id']}", "role_id": "ROLE0001",
         "permission_id": perm["_id"], "granted_by": "test", "granted_at": 1758000000})

    token = await token_of(client, DEMO_USERS["asker"])
    headers = {"Authorization": f"Bearer {token}"}

    assert (await client.get("/api/v1/system/config", headers=headers)).status_code == 200
    denied = await client.put("/api/v1/system/config", headers=headers,
                              json={"values": {"import.concurrency": 3}, "reason": "压测调整"})
    assert denied.status_code == 403
    assert denied.json()["code"] == "AUTH-2004"
    assert "system:config" in denied.json()["message"]


async def test_user_without_any_role_is_denied_everywhere(client):
    """无角色用户：认证通过（能登录），但任何权限闸门都进不去。"""
    await mongo.collection(org_repo.USER_ROLES).delete_many({"user_id": "U000003"})
    token = await token_of(client, DEMO_USERS["asker"])
    resp = await client.get("/api/v1/system/config",
                            headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "AUTH-2004"


async def test_routes_without_declaration_are_not_gated(client):
    """没声明权限码的路由不受功能权限影响（`/auth/me` 只要求登录）。"""
    token = await token_of(client, DEMO_USERS["asker"])
    assert (await client.get("/api/v1/auth/me",
                             headers={"Authorization": f"Bearer {token}"})).status_code == 200


async def test_whitelist_paths_skip_both_auth_and_perm(client):
    """白名单接口（`/health`、登录）既不过认证也不应被授权拦下。"""
    assert (await client.get("/health")).status_code == 200
    assert (await client.post("/api/v1/auth/login",
                              json={"username": "lina", "password": "Demo@12345"}
                              )).status_code == 200


# ================================================================ 三、依赖函数本身
async def test_enforce_perm_raises_when_route_needs_perm_but_no_user():
    """白名单路由误声明权限码时，应报"未经鉴权"而不是静默放行（防漏网）。"""

    @require_perm("audit:read")
    def handler():
        return None

    class FakeRoute:
        endpoint = staticmethod(handler)

    class FakeState:
        pass

    class FakeRequest:
        scope = {"route": FakeRoute}
        state = FakeState()

    with pytest.raises(BizError) as exc:
        enforce_perm(FakeRequest)
    assert exc.value.spec.code == Err.AUTH_TOKEN_INVALID.code
