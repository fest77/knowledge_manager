# -*- coding: utf-8 -*-
"""模块 10 的接口测试（§3.1~§3.4 / §3.6 / §3.7）。

对应验收：AC-10-01（权限边界）、AC-10-02（append-only 接口层）、AC-10-11（四维筛选）、
AC-10-12（别名展开）、AC-10-13（分页契约）、AC-10-15（导出可用）、AC-10-16（导出防护）、
AC-10-17（导出留痕）、AC-10-22（动作字典接口）、AC-10-23（无哈希链）。
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from app.core.config import settings
from app.core.errors import BizError, Err
from app.infra.mongo import mongo
from app.repositories import audit_repo, auth_repo
from app.services.audit_service import CSV_COLUMNS, audit_service
from app.services.auth_service import UserContext
from tests.conftest import token_of

pytestmark = pytest.mark.anyio

SYS_ADMIN = "lina"
KB_ADMIN = "zhangwei"
ASKER = "wangqiang"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed() -> None:
    """造一批覆盖三个动作域、两个操作人的记录，供筛选类用例比对。"""
    await audit_service.record("doc.create", actor="U000001", target_type="doc",
                               target_id="DOC0001", target_name="报销制度",
                               after={"title": "报销制度"})
    await audit_service.record("doc.permission_change", actor="U000002", target_type="doc",
                               target_id="DOC0001", target_name="报销制度",
                               before={"is_global": True, "departments": [],
                                       "roles": [], "users": [], "version": 1},
                               after={"is_global": False, "departments": ["DEPT0003"],
                                      "roles": [], "users": [], "version": 2},
                               reason="按 PRD 2.9.9 收紧")
    await audit_service.record("doc.enable", actor="U000002", target_type="doc",
                               target_id="DOC0002", before={"status": "disabled"},
                               after={"status": "enabled"})
    await audit_service.record("role.grant", actor="U000001", target_type="role",
                               target_id="ROLE0002", before={"permissions": ["doc:read"]},
                               after={"permissions": ["doc:read", "doc:edit"]},
                               reason="补授编辑权限")


# --------------------------------------------------------------------- 权限边界
async def test_ac_10_01_only_sys_admin_can_read_audit(client):
    """AC-10-01：只有 `sys_admin` 能查审计；未擅自给 `kb_admin` 加 `audit:read`。"""
    await _seed()
    sys_token = await token_of(client, SYS_ADMIN)
    kb_token = await token_of(client, KB_ADMIN)
    asker_token = await token_of(client, ASKER)

    ok_resp = await client.get("/api/v1/audit/logs", headers=auth(sys_token))
    assert ok_resp.status_code == 200
    assert ok_resp.json()["data"]["total"] == 4

    for token in (kb_token, asker_token):
        denied = await client.get("/api/v1/audit/logs", headers=auth(token))
        assert denied.status_code == 403
        assert denied.json()["code"] == "AUTH-2004"

    no_token = await client.get("/api/v1/audit/logs")
    assert no_token.status_code == 401

    # 授权总量仍是 4+16+22，说明没有为审计悄悄扩权
    bindings = await mongo.collection(auth_repo.ROLE_PERMISSIONS).count_documents({})
    assert bindings == 42


async def test_export_permission_is_separate_from_read(client):
    """`audit:export` 是独立权限码；本模块不擅自放宽。"""
    token = await token_of(client, SYS_ADMIN)
    resp = await client.get("/api/v1/audit/logs/export?format=csv", headers=auth(token))
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]


def test_service_layer_fallback_uses_aud_codes():
    """R-02：绕过 FastAPI 直调时的**服务层兜底**用 `AUD-2001` / `AUD-2002`。"""
    from app.api.routes_audit import ensure_permission

    plain = UserContext(user_id="U000003", username="wangqiang", real_name="王强",
                        dept_id="DEPT0004", dept_name="技术部")
    with pytest.raises(BizError) as read_exc:
        ensure_permission(plain, "audit:read", Err.AUD_READ_DENIED)
    assert read_exc.value.spec.code == "AUD-2001"
    with pytest.raises(BizError) as export_exc:
        ensure_permission(plain, "audit:export", Err.AUD_EXPORT_DENIED)
    assert export_exc.value.spec.code == "AUD-2002"

    allowed = dataclasses.replace(plain, permissions=frozenset({"audit:read"}))
    ensure_permission(allowed, "audit:read", Err.AUD_READ_DENIED)


# --------------------------------------------------------------------- append-only
@pytest.mark.parametrize("method", ["put", "patch", "delete"])
@pytest.mark.parametrize("path", ["/api/v1/audit/logs", "/api/v1/audit/logs/LOG202609230001"])
async def test_ac_10_02_write_methods_are_rejected(client, method, path):
    """AC-10-02：任何写方法一律 `AUD-2003`（**即使 `sys_admin`**）。"""
    token = await token_of(client, SYS_ADMIN)
    resp = await client.request(method.upper(), path, headers=auth(token))
    assert resp.status_code == 403
    assert resp.json()["code"] == "AUD-2003"


async def test_write_rejection_also_covers_new_subpaths(client):
    """兜底路由覆盖**任意**审计子路径，防将来新增路由忘了只读。"""
    token = await token_of(client, SYS_ADMIN)
    resp = await client.put("/api/v1/audit/anything/new", headers=auth(token))
    assert resp.json()["code"] == "AUD-2003"


async def test_ac_10_23_no_hash_chain_fields(client):
    """AC-10-23（G-10）：记录中不含 `prev_hash` / `hash`。"""
    await _seed()
    doc = (await mongo.collection(audit_repo.AUDIT_LOGS).find({}).to_list(length=None))[0]
    assert "prev_hash" not in doc and "hash" not in doc


# --------------------------------------------------------------------- 四维筛选
async def test_ac_10_11_four_dimension_filters(client):
    """AC-10-11：四个维度各自单独筛 + 组合筛，结果集与直连库统计一致。"""
    await _seed()
    token = await token_of(client, SYS_ADMIN)
    coll = mongo.collection(audit_repo.AUDIT_LOGS)
    docs = await coll.find({}).to_list(length=None)
    low = min(d["ts"] for d in docs) - 1000
    high = max(d["ts"] for d in docs) + 1000

    cases = [
        ("action=doc.create", {"action": "doc.create"}),
        ("actor=U000002", {"actor": "U000002"}),
        ("target_type=role&target_id=ROLE0002",
         {"target_type": "role", "target_id": "ROLE0002"}),
        (f"start_ts={low}&end_ts={high}", {}),
        # 组合：AND 语义
        ("action=doc.create,doc.enable&actor=U000002",
         {"action": {"$in": ["doc.create", "doc.enable"]}, "actor": "U000002"}),
        ("target_type=doc&outcome=success", {"target_type": "doc", "outcome": "success"}),
        ("actor_role=sys_admin", {"actor_role": "sys_admin"}),
    ]
    for qs, flt in cases:
        resp = await client.get(f"/api/v1/audit/logs?{qs}", headers=auth(token))
        assert resp.status_code == 200, resp.text
        expected = await coll.count_documents(flt)
        assert resp.json()["data"]["total"] == expected, f"{qs} 的结果与直连库不一致"
        assert len(resp.json()["data"]["items"]) == expected


async def test_actor_filter_accepts_username_without_degrading(client):
    """`actor` 支持传 username：能解析时不应置 `degraded`。"""
    await _seed()
    token = await token_of(client, SYS_ADMIN)
    resp = await client.get("/api/v1/audit/logs?actor=lina", headers=auth(token))
    data = resp.json()["data"]
    assert data["degraded"] is False
    assert data["total"] == 2


async def test_actor_filter_degrades_instead_of_500(client):
    """§7.1 降级：解析不出 username 时 `degraded=true`，**不报 500**。"""
    await _seed()
    token = await token_of(client, SYS_ADMIN)
    resp = await client.get("/api/v1/audit/logs?actor=ghost_user", headers=auth(token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["degraded"] is True and data["total"] == 0


async def test_ac_10_12_alias_expansion_in_query(client):
    """AC-10-12：`action=doc.toggle` 必须同时查出 `doc.enable` / `doc.disable`。"""
    await _seed()
    await audit_service.record("doc.disable", actor="U000002", target_type="doc",
                               target_id="DOC0003", before={"status": "enabled"},
                               after={"status": "disabled"})
    token = await token_of(client, SYS_ADMIN)
    resp = await client.get("/api/v1/audit/logs?action=doc.toggle", headers=auth(token))
    assert resp.json()["data"]["total"] == 2

    single = await client.get("/api/v1/audit/logs?action=doc.enable", headers=auth(token))
    assert single.json()["data"]["total"] == 1


async def test_detail_endpoint_returns_snapshots(client):
    """§3.2：详情返回全字段（含 `before`/`after`/`redacted_keys`/`trace_id`）。"""
    await _seed()
    token = await token_of(client, SYS_ADMIN)
    listing = await client.get("/api/v1/audit/logs?action=doc.permission_change",
                               headers=auth(token))
    audit_id = listing.json()["data"]["items"][0]["audit_id"]
    detail = await client.get(f"/api/v1/audit/logs/{audit_id}", headers=auth(token))
    assert detail.status_code == 200
    data = detail.json()["data"]
    assert data["before"]["version"] == 1 and data["after"]["version"] == 2
    assert data["reason"] == "按 PRD 2.9.9 收紧"
    assert set(data) >= {"before", "after", "redacted_keys", "extra", "trace_id", "ua"}


# --------------------------------------------------------------------- 参数校验
async def test_aud_1003_bad_action_and_bad_log_id(client):
    token = await token_of(client, SYS_ADMIN)
    bad_action = await client.get("/api/v1/audit/logs?action=doc.modify", headers=auth(token))
    assert bad_action.json()["code"] == "AUD-1003"
    assert "doc.modify" in bad_action.json()["message"]

    bad_id = await client.get("/api/v1/audit/logs/not-a-log-id", headers=auth(token))
    assert bad_id.status_code == 400
    assert bad_id.json()["code"] == "AUD-1003"


async def test_aud_3001_valid_format_but_missing(client):
    token = await token_of(client, SYS_ADMIN)
    resp = await client.get("/api/v1/audit/logs/LOG202601010001", headers=auth(token))
    assert resp.status_code == 404
    assert resp.json()["code"] == "AUD-3001"


async def test_aud_1001_time_range_rules(client):
    """R-03：`start > end`、跨度 > 366 天、秒级时间戳都报 `AUD-1001`。"""
    token = await token_of(client, SYS_ADMIN)
    cases = [
        "start_ts=1790000000000&end_ts=1780000000000",
        f"start_ts=1000000000000&end_ts={1000000000000 + 400 * 24 * 3600 * 1000}",
        "start_ts=1790000000",                       # 秒而非毫秒
        "start_ts=abc",
    ]
    for qs in cases:
        resp = await client.get(f"/api/v1/audit/logs?{qs}", headers=auth(token))
        assert resp.status_code == 400, qs
        assert resp.json()["code"] == "AUD-1001", qs


async def test_ac_10_13_pagination_contract_and_bounds(client):
    """AC-10-13：`page_size=200` 可用、`201` 报 `AUD-1002`、`page × page_size = 10001` 报错。"""
    await _seed()
    token = await token_of(client, SYS_ADMIN)

    ok200 = await client.get("/api/v1/audit/logs?page=1&page_size=200", headers=auth(token))
    assert ok200.status_code == 200
    body = ok200.json()["data"]
    assert set(body) >= {"items", "total", "page", "page_size"}
    assert body["page"] == 1 and body["page_size"] == 200

    too_big = await client.get("/api/v1/audit/logs?page_size=201", headers=auth(token))
    assert too_big.json()["code"] == "AUD-1002"

    too_deep = await client.get("/api/v1/audit/logs?page=10001&page_size=1",
                                headers=auth(token))
    assert too_deep.json()["code"] == "AUD-1002"

    bad_page = await client.get("/api/v1/audit/logs?page=0", headers=auth(token))
    assert bad_page.json()["code"] == "AUD-1002"


async def test_aud_1004_filter_value_rules(client):
    """R-06 / R-14：`target_type` / `outcome` / `actor_role` / `sort` 取值域。"""
    token = await token_of(client, SYS_ADMIN)
    cases = ["target_type=document", "outcome=ok", "actor_role=ADMIN",
             "sort=actor desc", "sort=ts;drop"]
    for qs in cases:
        resp = await client.get(f"/api/v1/audit/logs?{qs}", headers=auth(token))
        assert resp.status_code == 400, qs
        assert resp.json()["code"] == "AUD-1004", qs


async def test_business_role_is_accepted_as_actor_role(client):
    """裁定 A：业务角色（`management`）也会出现在 `actor_role` 快照里，不能拒。"""
    await audit_service.record("role.grant", actor="U000001", actor_role="management",
                               target_type="role", target_id="ROLE0004",
                               before={"p": []}, after={"p": ["qa:use"]}, reason="业务角色授权")
    token = await token_of(client, SYS_ADMIN)
    resp = await client.get("/api/v1/audit/logs?actor_role=management", headers=auth(token))
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == 1


async def test_sort_ascending_is_allowed(client):
    await _seed()
    token = await token_of(client, SYS_ADMIN)
    resp = await client.get("/api/v1/audit/logs?sort=ts asc", headers=auth(token))
    items = resp.json()["data"]["items"]
    assert [i["ts"] for i in items] == sorted(i["ts"] for i in items)


# --------------------------------------------------------------------- 导出
async def test_ac_10_15_csv_export_has_bom_and_fixed_columns(client):
    """AC-10-15：UTF-8 BOM（Excel 中文不乱码）+ 固定列序。"""
    await _seed()
    token = await token_of(client, SYS_ADMIN)
    resp = await client.get("/api/v1/audit/logs/export?format=csv", headers=auth(token))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "audit_logs_" in resp.headers["content-disposition"]

    text = resp.content.decode("utf-8")
    assert text.startswith("\ufeff"), "缺 BOM 会让 Excel 打开中文列名乱码"
    header = text.lstrip("\ufeff").splitlines()[0]
    assert header.split(",") == list(CSV_COLUMNS)


async def test_ac_10_15_json_export_is_an_array(client):
    await _seed()
    token = await token_of(client, SYS_ADMIN)
    resp = await client.get("/api/v1/audit/logs/export?format=json", headers=auth(token))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    rows = resp.json()
    assert isinstance(rows, list) and len(rows) == 4
    assert {"audit_id", "before", "after"} <= set(rows[0])


async def test_export_matches_list_scope(client):
    """同一筛选口径下，导出的行数与 `/audit/logs` 的 `total` 一致。"""
    await _seed()
    token = await token_of(client, SYS_ADMIN)
    listing = await client.get("/api/v1/audit/logs?target_type=doc", headers=auth(token))
    resp = await client.get("/api/v1/audit/logs/export?format=csv&target_type=doc",
                            headers=auth(token))
    rows = resp.content.decode("utf-8").lstrip("\ufeff").splitlines()
    assert len(rows) - 1 == listing.json()["data"]["total"]


async def test_ac_10_16_csv_formula_injection_is_neutralised(client):
    """AC-10-16：以 `=` `+` `-` `@` 开头的单元格前置单引号（防 CSV injection）。"""
    await audit_service.record("doc.create", actor="U000001", target_type="doc",
                               target_id="DOC0001", target_name="=cmd|' /C calc'!A0",
                               after={"title": "=1+1"})
    token = await token_of(client, SYS_ADMIN)
    resp = await client.get("/api/v1/audit/logs/export?format=csv", headers=auth(token))
    text = resp.content.decode("utf-8")
    assert "'=cmd|' /C calc'!A0" in text or "\"'=cmd|' /C calc'!A0\"" in text
    assert "'=cmd" in text, "以 = 开头的目标名必须被前置单引号"


async def test_ac_10_16_export_over_limit_is_rejected(client, monkeypatch):
    """AC-10-16：命中超过上限时报 `AUD-1005`，提示缩小时间范围。"""
    from app.api import routes_audit

    await _seed()
    monkeypatch.setattr(routes_audit, "settings",
                        dataclasses.replace(settings, audit_export_max_rows=1))
    token = await token_of(client, SYS_ADMIN)
    resp = await client.get("/api/v1/audit/logs/export?format=csv", headers=auth(token))
    assert resp.status_code == 400
    assert resp.json()["code"] == "AUD-1005"
    assert "缩小时间范围" in resp.json()["message"]


async def test_aud_1005_bad_format(client):
    token = await token_of(client, SYS_ADMIN)
    resp = await client.get("/api/v1/audit/logs/export?format=xlsx", headers=auth(token))
    assert resp.status_code == 400
    assert resp.json()["code"] == "AUD-1005"


async def test_ac_10_17_export_leaves_a_trace(client):
    """AC-10-17：每次导出成功后新增一条 `audit.export`，含筛选条件、条数与格式。"""
    await _seed()
    token = await token_of(client, SYS_ADMIN)
    await client.get("/api/v1/audit/logs/export?format=json&target_type=doc",
                     headers=auth(token))
    coll = mongo.collection(audit_repo.AUDIT_LOGS)
    row = await coll.find_one({"action": "audit.export"})
    assert row is not None
    assert row["after"]["format"] == "json"
    assert row["after"]["rows"] == 3
    assert row["after"]["filters"]["target_type"] == "doc"
    assert row["target_type"] == "audit"


# --------------------------------------------------------------------- 动作字典
async def test_ac_10_22_actions_endpoint(client):
    """AC-10-22：返回 ≥38 个动作，每条含中文名与 `requires_snapshot`。"""
    token = await token_of(client, SYS_ADMIN)
    resp = await client.get("/api/v1/audit/actions", headers=auth(token))
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert len(items) >= 38
    for item in items:
        assert any("\u4e00" <= ch <= "\u9fff" for ch in item["name"])
        assert isinstance(item["requires_snapshot"], bool)
    toggle = next(i for i in items if i["action"] == "doc.toggle")
    assert toggle["name"] == "启用 / 停用知识单元" and toggle["requires_snapshot"] is True


async def test_actions_endpoint_denied_for_kb_admin(client):
    token = await token_of(client, KB_ADMIN)
    resp = await client.get("/api/v1/audit/actions", headers=auth(token))
    assert resp.status_code == 403


# --------------------------------------------------------------------- 列表视图与健康
async def test_list_item_exposes_changed_fields_without_snapshots(client):
    """§3.1：列表级**不含** `before`/`after`，但要有 `changed_fields` 供界面直接展示。"""
    await _seed()
    token = await token_of(client, SYS_ADMIN)
    resp = await client.get("/api/v1/audit/logs?action=doc.permission_change",
                            headers=auth(token))
    item = resp.json()["data"]["items"][0]
    assert "before" not in item and "after" not in item
    assert item["changed_fields"] == ["departments", "is_global", "version"]
    assert item["action_name"] == "四维数据权限变更"
    assert item["reason"] == "按 PRD 2.9.9 收紧"


async def test_items_are_sorted_by_ts_desc_by_default(client):
    await _seed()
    token = await token_of(client, SYS_ADMIN)
    resp = await client.get("/api/v1/audit/logs", headers=auth(token))
    stamps = [i["ts"] for i in resp.json()["data"]["items"]]
    assert stamps == sorted(stamps, reverse=True)


async def test_health_exposes_audit_state(client):
    """§4.2：审计降级状态必须在 `/health` 可见，而不是埋在日志里。"""
    resp = await client.get("/health")
    audit = resp.json()["data"]["audit"]
    assert audit["state"] == "ok"
    assert audit["spool_writable"] is True
    assert audit["breaker_open"] is False


async def test_health_shows_degraded_after_a_failed_write(client, monkeypatch):
    async def down(_doc):
        raise RuntimeError("模拟 Mongo 不可用")

    monkeypatch.setattr(audit_repo, "insert", down)
    await audit_service.record("doc.create", actor="U000001")
    resp = await client.get("/health")
    assert resp.json()["data"]["audit"]["state"] == "degraded"


async def test_openapi_documents_the_four_audit_get_endpoints(client):
    paths = (await client.get("/openapi.json")).json()["paths"]
    assert set(paths["/api/v1/audit/logs"]) == {"get"}
    assert set(paths["/api/v1/audit/logs/export"]) == {"get"}
    assert set(paths["/api/v1/audit/logs/{log_id}"]) == {"get"}
    assert set(paths["/api/v1/audit/actions"]) == {"get"}


async def test_export_requires_login(client):
    resp = await client.get("/api/v1/audit/logs/export")
    assert resp.status_code == 401


async def test_login_failure_is_audited_without_password(client):
    """01 ↔ 10 的接线：登录失败留痕，且**绝不记密码**。"""
    ok = await client.post("/api/v1/auth/login",
                           json={"username": "lina", "password": "WrongPass@1"})
    assert ok.status_code == 401
    row = await mongo.collection(audit_repo.AUDIT_LOGS).find_one({"action": "auth.login_fail"})
    assert row is not None
    assert row["outcome"] == "failure"
    assert row["actor"] == "lina"
    assert row["target_id"] == "lina"
    assert "WrongPass@1" not in json.dumps(row, ensure_ascii=False, default=str)
    assert row["after"] == {"username": "lina", "fail_reason": "AUTH-2001"}


async def test_config_update_is_audited_per_key(client):
    """00 ↔ 10 的接线：`config.update` 逐键留痕，含 before/after 与原因。"""
    token = await token_of(client, SYS_ADMIN)
    resp = await client.put("/api/v1/system/config", headers=auth(token), json={
        "values": {"llm.temperature": 0.6, "retrieval.recall_multiplier": 7},
        "reason": "联调期间放大召回倍数",
    })
    assert resp.status_code == 200
    rows = await mongo.collection(audit_repo.AUDIT_LOGS).find(
        {"action": "config.update"}).to_list(length=None)
    assert len(rows) == 2, "按 C-04 逐键一条"
    by_key = {r["target_id"]: r for r in rows}
    assert by_key["llm.temperature"]["before"] == {"llm.temperature": 0.2}
    assert by_key["llm.temperature"]["after"] == {"llm.temperature": 0.6}
    assert by_key["retrieval.recall_multiplier"]["after"] == {"retrieval.recall_multiplier": 7}
    assert all(r["snapshot_state"] != "dropped" for r in rows)
    assert all(r["reason"] == "联调期间放大召回倍数" for r in rows)


async def test_config_update_without_change_writes_no_audit(client):
    """无实际变化时不写审计：避免"什么都没改却留下一条假痕迹"。"""
    token = await token_of(client, SYS_ADMIN)
    resp = await client.put("/api/v1/system/config", headers=auth(token), json={
        "values": {"llm.temperature": 0.2}, "reason": "重复提交同一个值",
    })
    assert resp.json()["data"]["changed"] == []
    count = await mongo.collection(audit_repo.AUDIT_LOGS).count_documents(
        {"action": "config.update"})
    assert count == 0
