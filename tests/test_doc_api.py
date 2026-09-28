# -*- coding: utf-8 -*-
"""模块 03 接口层的测试（AC-03-05 / 22 与两处路由顺序坑）。"""
from __future__ import annotations

import hashlib

import pytest

from tests.conftest import token_of

pytestmark = pytest.mark.anyio

SYS_ADMIN = "lina"
KB_ADMIN = "zhangwei"
ASKER = "wangqiang"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def _seed_one(client, token: str, file_name: str = "制度.pdf",
                    category_id: str | None = None) -> str:
    """直连服务层造一条台账（04 未落地，没有上传接口可用）。"""
    from app.services.doc_service import doc_service

    return await doc_service.create(
        file_name=file_name, file_ext="pdf", file_size=100,
        file_hash=sha(file_name), storage={}, created_by="U000001",
        category_id=category_id)


# --------------------------------------------------------------------- 权限边界
async def test_ac_03_22_permission_boundary_is_produced_by_module_01(client):
    """AC-03-22：缺 `doc:read` 时 403 **由 01 产出**（`AUTH-2004`），本模块不自造码。

    这里同时钉住一条容易记错的矩阵事实：**`sys_admin` 没有 `doc:*`**。
    原型 `08` 的矩阵在该格是「—」，所以"系统管理员却看不了知识台账"是**设计如此**，
    不是权限配错。要用台账就得用 `kb_admin`。
    """
    asker = await token_of(client, ASKER)
    denied = await client.get("/api/v1/docs", headers=auth(asker))
    assert denied.status_code == 403
    assert denied.json()["code"] == "AUTH-2004", "不是 DOC-2xxx"

    for name in (ASKER, SYS_ADMIN):
        token = await token_of(client, name)
        assert (await client.get("/api/v1/docs", headers=auth(token))).status_code == 403, \
            f"{name} 不该能读知识台账"

    kb = await token_of(client, KB_ADMIN)
    assert (await client.get("/api/v1/docs", headers=auth(kb))).status_code == 200
    assert (await client.post("/api/v1/categories", headers=auth(kb),
                              json={"name": "知识管理员建的分类"})).status_code == 200
    assert (await client.get("/api/v1/org/departments",
                             headers=auth(kb))).status_code == 403, "kb_admin 没有 org:manage"


# --------------------------------------------------------------------- 路由顺序
async def test_dedup_check_is_not_swallowed_by_doc_id_route(client):
    """`/docs/dedup-check` 必须声明在 `/docs/{doc_id}` 之前。

    写反了它会命中 `GET /docs/{doc_id}` 的形状——虽然方法不同（POST vs GET），
    但 `/docs/{doc_id}/toggle` 也是 POST，真正会撞的是别的组合；
    这里用"能不能正常返回去重决策"来验证顺序正确。
    """
    token = await token_of(client, KB_ADMIN)
    resp = await client.post("/api/v1/docs/dedup-check", headers=auth(token),
                             json={"items": [{"file_hash": sha("x"),
                                              "file_name": "x.pdf"}]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["items"][0]["decision"] == "new"


async def test_recount_is_not_swallowed_by_category_id_route(client):
    """`/categories/recount` 必须声明在 `/categories/{category_id}` 之前。"""
    token = await token_of(client, KB_ADMIN)
    resp = await client.post("/api/v1/categories/recount", headers=auth(token))
    assert resp.status_code == 200, resp.text
    assert "checked" in resp.json()["data"]


# --------------------------------------------------------------------- 列表与详情
async def test_list_returns_nine_columns_and_summary(client):
    token = await token_of(client, KB_ADMIN)
    await _seed_one(client, token, "a.pdf")
    resp = await client.get("/api/v1/docs", headers=auth(token))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["total"] == 1 and len(data["items"]) == 1
    assert set(data["summary"]) == {"total", "enabled", "disabled", "importing", "deleted"}
    assert data["summary"]["importing"] == 1


async def test_ac_03_05_list_does_not_depend_on_the_caller(client):
    """AC-03-05：两个知识管理员（同为 kb_admin）看到**同样的行**，且响应里没有 allow/deny 字段。"""
    kb_token_seed = await token_of(client, KB_ADMIN)
    await _seed_one(client, kb_token_seed, "b.pdf")
    kb_token = await token_of(client, KB_ADMIN)
    one = await client.get("/api/v1/docs", headers=auth(kb_token))
    two = await client.get("/api/v1/docs", headers=auth(kb_token))
    assert one.json()["data"]["items"] == two.json()["data"]["items"]
    dump = one.text
    for word in ("allow", "denied", "permission_granted"):
        assert word not in dump, f"列表不该出现权限判定字段：{word}"


async def test_param_errors_use_doc_codes(client):
    """AC-03-02：`page_size=201` → `DOC-1001`；`sort_by` 非法 → `DOC-1001`。"""
    token = await token_of(client, KB_ADMIN)
    for qs, want in (("page_size=201", "DOC-1001"), ("sort_by=title", "DOC-1001"),
                     ("status=deleted", "DOC-1001"), ("view=trash", "DOC-1001"),
                     ("permission_label=public", "DOC-1001")):
        resp = await client.get(f"/api/v1/docs?{qs}", headers=auth(token))
        assert resp.status_code == 400, qs
        assert resp.json()["code"] == want, qs
    assert "200" in (await client.get("/api/v1/docs?page_size=201",
                                      headers=auth(token))).json()["message"], \
        "报错信息里要提示上限，而不是静默截断"


async def test_readonly_fields_are_rejected_with_doc_1001(client):
    """`DOC-1001`：提交不可写字段要报**本模块的**参数码，不是 SYS-1001。"""
    token = await token_of(client, KB_ADMIN)
    doc_id = await _seed_one(client, token, "c.pdf")
    for field, value in (("file_hash", sha("y")), ("chunk_count", 99),
                         ("status", "enabled"), ("import_status", "done"),
                         ("permission_summary", {}), ("file_name", "改名.pdf")):
        resp = await client.put(f"/api/v1/docs/{doc_id}", headers=auth(token),
                                json={field: value})
        assert resp.status_code == 400, field
        assert resp.json()["code"] == "DOC-1001", field


async def test_empty_update_body_is_rejected(client):
    token = await token_of(client, KB_ADMIN)
    doc_id = await _seed_one(client, token, "d.pdf")
    resp = await client.put(f"/api/v1/docs/{doc_id}", headers=auth(token), json={})
    assert resp.json()["code"] == "DOC-1001"


async def test_detail_and_soft_delete_flow(client):
    """详情 → 软删除 → 默认列表看不到 → 回收站可见 → 恢复。"""
    token = await token_of(client, KB_ADMIN)
    doc_id = await _seed_one(client, token, "e.pdf")
    from app.services.doc_service import doc_service

    await doc_service.mark_import_done(doc_id, 3, 30)

    detail = await client.get(f"/api/v1/docs/{doc_id}", headers=auth(token))
    assert detail.json()["data"]["doc_no"].startswith("DOC")
    assert detail.json()["data"]["chunk_count"] == 3

    deleted = await client.delete(f"/api/v1/docs/{doc_id}", headers=auth(token))
    assert deleted.json()["data"]["deleted"] is True
    again = await client.delete(f"/api/v1/docs/{doc_id}", headers=auth(token))
    assert again.json()["data"]["deleted"] is False, "幂等"

    assert (await client.get("/api/v1/docs", headers=auth(token))).json()["data"]["total"] == 0
    recycle = await client.get("/api/v1/docs?view=deleted", headers=auth(token))
    assert recycle.json()["data"]["total"] == 1
    assert recycle.json()["data"]["items"][0]["status_text"] == "已删除"

    gone = await client.get(f"/api/v1/docs/{doc_id}", headers=auth(token))
    assert gone.json()["code"] == "DOC-3002"
    with_del = await client.get(f"/api/v1/docs/{doc_id}?include_deleted=true",
                               headers=auth(token))
    assert with_del.status_code == 200

    restored = await client.post(f"/api/v1/docs/{doc_id}/restore", headers=auth(token),
                                 json={"restore_status": "disabled"})
    assert restored.status_code == 200, restored.text
    assert restored.json()["data"]["status"] == "disabled"


async def test_toggle_endpoint(client):
    token = await token_of(client, KB_ADMIN)
    doc_id = await _seed_one(client, token, "f.pdf")
    busy = await client.post(f"/api/v1/docs/{doc_id}/toggle", headers=auth(token),
                             json={"enabled": True})
    assert busy.json()["code"] == "DOC-3010", "导入未完成不能启用"

    from app.services.doc_service import doc_service
    await doc_service.mark_import_done(doc_id, 2, 20)
    assert (await client.post(f"/api/v1/docs/{doc_id}/toggle", headers=auth(token),
                              json={"enabled": True})).status_code == 200
    off = await client.post(f"/api/v1/docs/{doc_id}/toggle", headers=auth(token),
                            json={"enabled": False})
    assert off.json()["data"]["status"] == "disabled"


# --------------------------------------------------------------------- 分类
async def test_category_tree_and_crud_via_api(client):
    token = await token_of(client, KB_ADMIN)
    root = await client.post("/api/v1/categories", headers=auth(token),
                             json={"name": "公司制度", "sort": 1})
    root_id = root.json()["data"]["category_id"]
    child = await client.post("/api/v1/categories", headers=auth(token),
                              json={"name": "财务报销", "parent_id": root_id})
    child_id = child.json()["data"]["category_id"]
    await _seed_one(client, token, "g.pdf", category_id=child_id)

    tree = await client.get("/api/v1/categories", headers=auth(token))
    node = tree.json()["data"]["items"][0]
    assert node["name"] == "公司制度" and node["doc_count"] == 1, "父分类含子分类"
    assert node["children"][0]["name"] == "财务报销"

    dup = await client.post("/api/v1/categories", headers=auth(token),
                            json={"name": "公司制度"})
    assert dup.json()["code"] == "DOC-3007"
    bad_name = await client.post("/api/v1/categories", headers=auth(token),
                                 json={"name": "含/斜杠"})
    assert bad_name.json()["code"] == "DOC-1003"

    moved = await client.put(f"/api/v1/categories/{child_id}", headers=auth(token),
                             json={"name": "报销与结算"})
    assert moved.json()["data"]["changed"]["name"] == "报销与结算"

    blocked = await client.delete(f"/api/v1/categories/{root_id}", headers=auth(token))
    assert blocked.json()["code"] == "DOC-3005", "仍有子分类"


async def test_unknown_category_in_filter_reports_3009(client):
    token = await token_of(client, KB_ADMIN)
    resp = await client.get("/api/v1/docs?category_id=CAT9999", headers=auth(token))
    assert resp.status_code == 404
    assert resp.json()["code"] == "DOC-3009"


async def test_openapi_surface_has_no_write_route_on_categories_recount(client):
    """接口面自检：本模块 8 个路径，且 `recount`/`dedup-check` 各自只有 POST。"""
    paths = (await client.get("/openapi.json")).json()["paths"]
    mine = {p: sorted(v) for p, v in paths.items()
            if p.startswith("/api/v1/docs") or p.startswith("/api/v1/categories")}
    assert len(mine) == 8, sorted(mine)
    assert mine["/api/v1/categories/recount"] == ["post"]
    assert mine["/api/v1/docs/dedup-check"] == ["post"]
    assert mine["/api/v1/docs/{doc_id}"] == ["delete", "get", "put"]
