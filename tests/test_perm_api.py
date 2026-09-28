# -*- coding: utf-8 -*-
"""模块 05 的接口层测试（`/api/v1/perm/*`）。

三个角色在这里的行为差异是本模块最值得钉住的东西：

| 角色 | GET/PUT `/perm/{id}` | POST `/perm/check` | 代查他人 |
|---|---|---|---|
| `kb_admin`（zhangwei） | ✅ `perm:manage` | ✅ | ❌ `PERM-2001` |
| `sys_admin`（lina） | ✅ | ✅ | ✅（有 `user:manage`） |
| `asker`（wangqiang） | ❌ `AUTH-2004` | ✅（原型 `08` 标注第 5 条） | ❌ |

`asker` **能调判定接口但不能配权限**是刻意的：判定接口只回答"能不能读"，
不泄漏任何知识内容，所以它对所有登录用户开放；而配置权限会改变全公司能看到什么，
必须受 `perm:manage` 保护。这条边界如果写反，普通用户就能给自己开权限。
"""
from __future__ import annotations

import hashlib

import pytest

from app.infra.mongo import mongo
from app.services.doc_service import doc_service
from app.services.perm_cache import perm_cache
from tests.conftest import token_of

pytestmark = pytest.mark.anyio

KB_ADMIN = "zhangwei"
SYS_ADMIN = "lina"
ASKER = "wangqiang"

DEPT_FINANCE = "DEPT0003"
DEPT_REIMBURSE = "DEPT0008"          # 「报销组」，挂在财务部下（子部门）
DEPT_HR = "DEPT0002"
DEPT_TECH = "DEPT0004"               # 王强所在的部门
ROLE_MANAGEMENT = "ROLE0004"
USER_LINA = "U000001"
USER_WANGQIANG = "U000003"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _clean_cache():
    perm_cache.reset()
    yield
    perm_cache.reset()


async def _new_doc(file_name: str = "制度.pdf") -> str:
    return await doc_service.create(
        file_name=file_name, file_ext="pdf", file_size=100,
        file_hash=hashlib.sha256(file_name.encode()).hexdigest(), storage={},
        created_by="U000001")


# ================================================================ 权限边界
async def test_only_perm_manage_can_read_and_write_config(client):
    """AC-05-10 的反面：`asker` 连权限配置都读不到（403 由全局依赖产出）。"""
    doc_id = await _new_doc("guard.pdf")
    asker = await token_of(client, ASKER)

    got = await client.get(f"/api/v1/perm/{doc_id}", headers=auth(asker))
    assert got.status_code == 403
    assert got.json()["code"] == "AUTH-2004"

    put = await client.put(f"/api/v1/perm/{doc_id}", headers=auth(asker),
                           json={"is_global": True, "reason": "试图给自己开权限"})
    assert put.status_code == 403
    assert put.json()["code"] == "AUTH-2004"
    # 关键：越权请求**没有**留下任何权限记录
    assert await mongo.collection("kb_permissions").count_documents(
        {"doc_id": doc_id}) == 0


async def test_every_role_can_call_check_endpoint(client):
    """AC-05-10：三角色都能调 `/perm/check`（它只返回判定结果）。"""
    doc_id = await _new_doc("checkall.pdf")
    for username in (KB_ADMIN, SYS_ADMIN, ASKER):
        token = await token_of(client, username)
        resp = await client.post("/api/v1/perm/check", headers=auth(token),
                                 json={"doc_id": doc_id})
        assert resp.status_code == 200, f"{username} 应能调用判定接口"
        data = resp.json()["data"]
        assert data["allowed"] is False, "无权限记录 = 默认拒绝"
        assert data["reason_code"] == "no_permission_record"


# ================================================================ 判定接口
async def test_check_returns_verdict_without_leaking_content(client):
    """AC-05-10：响应**不含任何文档内容**（否则它就是越权读取通道）。"""
    doc_id = await _new_doc("secret-policy.pdf")
    kb = await token_of(client, KB_ADMIN)
    await client.put(f"/api/v1/perm/{doc_id}", headers=auth(kb),
                     json={"is_global": True, "reason": "全局公开用于演示"})

    asker = await token_of(client, ASKER)
    raw = await client.post("/api/v1/perm/check", headers=auth(asker),
                            json={"doc_id": doc_id})
    assert raw.status_code == 200
    data = raw.json()["data"]
    assert data["allowed"] is True
    assert data["reason_code"] == "global"
    assert data["version"] == 1
    assert data["evaluated_as"]["user_id"], "要回传判定依据的用户快照"
    # 响应体里不能出现标题 / 正文 / 文件名
    body = raw.text
    for leak in ("secret-policy", "制度", "content", "file_name"):
        assert leak not in body, f"判定响应泄漏了 {leak}"


async def test_check_always_returns_200_for_denied(client):
    """`allowed=false` 是**正确答案**，不是错误（不能用 403 表达"不可读"）。"""
    doc_id = await _new_doc("denied.pdf")
    asker = await token_of(client, ASKER)
    resp = await client.post("/api/v1/perm/check", headers=auth(asker),
                             json={"doc_id": doc_id})
    assert resp.status_code == 200
    assert resp.json()["code"] == 0
    assert resp.json()["data"]["allowed"] is False


async def test_check_impersonation_requires_privilege(client):
    """AC-05-11：普通用户传别人的 `user_id` → `PERM-2001`；系统管理员可以。"""
    doc_id = await _new_doc("impersonate.pdf")
    kb = await token_of(client, KB_ADMIN)
    await client.put(f"/api/v1/perm/{doc_id}", headers=auth(kb),
                     json={"users": [USER_WANGQIANG], "reason": "只授权给王强"})

    # 知识管理员没有 `user:manage` → 不许代查
    denied = await client.post("/api/v1/perm/check", headers=auth(kb),
                               json={"doc_id": doc_id, "user_id": USER_WANGQIANG})
    assert denied.status_code == 403
    assert denied.json()["code"] == "PERM-2001"

    # 系统管理员可以代查，并且判定用的是**被查者**的部门/角色
    sys_admin = await token_of(client, SYS_ADMIN)
    ok = await client.post("/api/v1/perm/check", headers=auth(sys_admin),
                           json={"doc_id": doc_id, "user_id": USER_WANGQIANG})
    assert ok.status_code == 200
    data = ok.json()["data"]
    assert data["allowed"] is True and data["reason_code"] == "user"
    assert data["evaluated_as"]["user_id"] == USER_WANGQIANG

    # 代查一个根本不存在的人 → PERM-1004
    ghost = await client.post("/api/v1/perm/check", headers=auth(sys_admin),
                              json={"doc_id": doc_id, "user_id": "U999999"})
    assert ghost.status_code == 400
    assert ghost.json()["code"] == "PERM-1004"


async def test_check_route_is_not_swallowed_by_doc_id_route(client):
    """路由顺序：`POST /perm/check` 不能被 `/perm/{doc_id}` 吃掉。

    写反的话 `check` 会被当成 `doc_id="check"`，而 `{doc_id}` 只支持 GET/PUT
    → 判定接口变成 **405**，前端看到的是"接口不存在"。
    """
    kb = await token_of(client, KB_ADMIN)
    resp = await client.post("/api/v1/perm/check", headers=auth(kb),
                             json={"doc_id": "DOC00000000000001"})
    assert resp.status_code == 200, "判定接口必须命中自己的路由"

    # 而 `/perm/check` 用 GET 应当 405（说明它确实是一个独立路由，不是被当 doc_id）
    get_check = await client.get("/api/v1/perm/check", headers=auth(kb))
    assert get_check.status_code in (404, 405)


# ================================================================ 配置接口
async def test_get_config_returns_defaults_not_404(client):
    doc_id = await _new_doc("defaults.pdf")
    kb = await token_of(client, KB_ADMIN)
    resp = await client.get(f"/api/v1/perm/{doc_id}", headers=auth(kb))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["version"] == 0 and data["is_global"] is False
    assert data["readable_by"] == "nobody"
    assert data["doc_title"]


async def test_get_config_unknown_doc_is_404(client):
    kb = await token_of(client, KB_ADMIN)
    resp = await client.get("/api/v1/perm/DOC99999999999999", headers=auth(kb))
    assert resp.status_code == 404
    assert resp.json()["code"] == "PERM-3001"


async def test_put_config_validates_and_saves(client):
    """`PUT` 的完整往返：非法原因 → `PERM-1001`；合法 → `version=1` 且摘要刷新。"""
    doc_id = await _new_doc("save.pdf")
    kb = await token_of(client, KB_ADMIN)

    bad = await client.put(f"/api/v1/perm/{doc_id}", headers=auth(kb),
                           json={"is_global": False, "reason": "太短"})
    assert bad.status_code == 400
    assert bad.json()["code"] == "PERM-1001"

    ok = await client.put(f"/api/v1/perm/{doc_id}", headers=auth(kb),
                          json={"is_global": False,
                                "departments": [DEPT_FINANCE],
                                "roles": [ROLE_MANAGEMENT],
                                "users": [],
                                "reason": "新增财务部与管理层可读"})
    assert ok.status_code == 200, ok.text
    data = ok.json()["data"]
    assert data["version"] == 1
    assert data["effective_immediately"] is True

    # 台账的展示摘要同步刷新（03 的职责，05 只能经它写）
    doc = await doc_service.get(doc_id)
    assert doc["permission_summary"]["label"] == "limited"
    assert doc["permission_summary"]["dept_cnt"] == 1
    assert doc["permission_summary"]["role_cnt"] == 1

    # 列表接口的权限标签列也要跟着变（前端不需要额外请求）
    listed = await client.get("/api/v1/docs", headers=auth(kb))
    row = next(i for i in listed.json()["data"]["items"] if i["doc_id"] == doc_id)
    assert row["permission_label"] == "limited"
    assert row["permission_text"] == "受限(1部门/1角色)"


async def test_put_config_rejects_invalid_dimension(client):
    """维度里的无效 ID → 对应维度的码（前端要能定位到具体分组）。"""
    doc_id = await _new_doc("bad_dim.pdf")
    kb = await token_of(client, KB_ADMIN)
    resp = await client.put(f"/api/v1/perm/{doc_id}", headers=auth(kb),
                            json={"departments": ["DEPT8888"],
                                  "reason": "包含不存在部门"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "PERM-1002"


async def test_put_config_rejects_unknown_doc(client):
    kb = await token_of(client, KB_ADMIN)
    resp = await client.put("/api/v1/perm/DOC99999999999999", headers=auth(kb),
                            json={"is_global": True, "reason": "文档不存在测试"})
    assert resp.status_code == 404
    assert resp.json()["code"] == "PERM-3001"


# ================================================================ 三条最关键的验收
async def test_ac_05_05_permission_takes_effect_immediately(client):
    """AC-05-05：改权限后**紧接着**再判定，结果按新权限（同一秒内）。"""
    doc_id = await _new_doc("instant.pdf")
    kb = await token_of(client, KB_ADMIN)
    asker = await token_of(client, ASKER)

    before = await client.post("/api/v1/perm/check", headers=auth(asker),
                               json={"doc_id": doc_id})
    assert before.json()["data"]["allowed"] is False

    await client.put(f"/api/v1/perm/{doc_id}", headers=auth(kb),
                     json={"is_global": True, "reason": "立刻生效验证用例"})

    after = await client.post("/api/v1/perm/check", headers=auth(asker),
                              json={"doc_id": doc_id})
    assert after.json()["data"]["allowed"] is True, "权限变更必须即时生效（AD-02）"


async def test_ac_05_15_summary_does_not_participate_in_authorization(client):
    """AC-05-15：手工把摘要改成"公开"，**鉴权结果不变**（只读 E07）。"""
    doc_id = await _new_doc("tamper.pdf")
    asker = await token_of(client, ASKER)
    # E07 无记录（默认拒绝），但把 E04 的展示摘要篡改成"全局公开"
    await mongo.collection("kb_documents").update_one(
        {"_id": doc_id},
        {"$set": {"permission_summary": {"is_global": True, "dept_cnt": 0,
                                         "role_cnt": 0, "user_cnt": 0, "version": 99,
                                         "label": "global"}}})
    resp = await client.post("/api/v1/perm/check", headers=auth(asker),
                             json={"doc_id": doc_id})
    assert resp.json()["data"]["allowed"] is False, \
        "摘要只是展示，绝不能当成鉴权依据"
    assert resp.json()["data"]["reason_code"] == "no_permission_record"


async def test_ac_05_04_department_match_is_exact_over_http(client):
    """AC-05-04：授权「财务部」时，**子部门「报销组」的人不可读**（精确匹配，G-02）。

    构造方式：不依赖哪个演示用户恰好在子部门，而是**直接按部门判定**——
    授权财务部后，用一个"在报销组"的临时用户来判断。
    """
    doc_id = await _new_doc("exact.pdf")
    kb = await token_of(client, KB_ADMIN)
    sys_admin = await token_of(client, SYS_ADMIN)

    # 造一个在「报销组」（财务部的子部门）的用户
    await mongo.collection("sys_users").insert_one({
        "_id": "U900001", "username": "child_user", "real_name": "子部门员工",
        "dept_id": DEPT_REIMBURSE, "status": "active", "created_at": 1,
        "updated_at": 1})
    await mongo.collection("sys_user_roles").insert_one({
        "_id": "U900001:ROLE0001", "user_id": "U900001", "role_id": "ROLE0001",
        "granted_by": "test", "granted_at": 1})

    await client.put(f"/api/v1/perm/{doc_id}", headers=auth(kb),
                     json={"departments": [DEPT_FINANCE],
                           "reason": "只授权给财务部本级"})

    # 子部门的人 → 不可读（父部门授权不含子部门）
    child = await client.post("/api/v1/perm/check", headers=auth(sys_admin),
                              json={"doc_id": doc_id, "user_id": "U900001"})
    assert child.status_code == 200, child.text
    assert child.json()["data"]["allowed"] is False
    assert child.json()["data"]["reason_code"] == "none_matched"

    # 把报销组也授上 → 立刻可读
    await client.put(f"/api/v1/perm/{doc_id}", headers=auth(kb),
                     json={"departments": [DEPT_FINANCE, DEPT_REIMBURSE],
                           "reason": "补上报销组授权"})
    after = await client.post("/api/v1/perm/check", headers=auth(sys_admin),
                              json={"doc_id": doc_id, "user_id": "U900001"})
    assert after.json()["data"]["allowed"] is True
    assert after.json()["data"]["reason_code"] == "department"


async def test_ac_05_01_all_four_dimensions_over_http(client):
    """AC-05-01 / 16：四维各自单独放行；维度组合时**任一满足即可**（OR）。"""
    doc_id = await _new_doc("or.pdf")
    kb = await token_of(client, KB_ADMIN)
    sys_admin = await token_of(client, SYS_ADMIN)

    async def verdict(user_id: str, **payload) -> bool:
        await client.put(f"/api/v1/perm/{doc_id}", headers=auth(kb),
                         json={**payload, "reason": "四维判定验证用例"})
        resp = await client.post("/api/v1/perm/check", headers=auth(sys_admin),
                                 json={"doc_id": doc_id, "user_id": user_id})
        assert resp.status_code == 200, resp.text
        return resp.json()["data"]["allowed"]

    # ① 全局：任何人都放行
    assert await verdict(USER_WANGQIANG, is_global=True) is True
    # ② 部门：王强在技术部
    assert await verdict(USER_WANGQIANG, departments=[DEPT_TECH]) is True
    assert await verdict(USER_WANGQIANG, departments=[DEPT_FINANCE]) is False
    # ③ 角色：王强是 asker（ROLE0001）
    assert await verdict(USER_WANGQIANG, roles=["ROLE0001"]) is True
    assert await verdict(USER_WANGQIANG, roles=[ROLE_MANAGEMENT]) is False
    # ④ 个人
    assert await verdict(USER_WANGQIANG, users=[USER_WANGQIANG]) is True
    # OR：部门不对但角色对 → 仍然放行
    assert await verdict(USER_WANGQIANG, departments=[DEPT_FINANCE],
                         roles=["ROLE0001"]) is True
