# -*- coding: utf-8 -*-
"""`AuditService` 的测试（模块 10 §3.5 / §4.1 / §4.2 / §7.2）。

这是本模块最要紧的一组测试：`record()` 的契约是"**永不抛异常 + 永不阻断业务**"，
而这类契约**只能靠注入故障来验证**——正常路径跑一万次也证明不了降级链是通的。
对应验收：AC-10-06（永不抛）、AC-10-07（重试一次且不重复）、AC-10-08（补偿与回放）、
AC-10-10（快照必填 + G-11 原因）、AC-10-14（查询失败不伪装）、AC-10-20（未注册动作不丢）。
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.core.enums import SnapshotState
from app.core.errors import BizError
from app.infra.mongo import mongo
from app.repositories import audit_repo
from app.services import audit_service as svc
from app.services.audit_service import (AuditService, client_ip, format_ts,
                                        local_date_of, primary_role_of)

pytestmark = pytest.mark.anyio


async def _docs(filters: dict | None = None) -> list[dict]:
    cursor = mongo.collection(audit_repo.AUDIT_LOGS).find(filters or {})
    return await cursor.to_list(length=None)


async def _count(filters: dict | None = None) -> int:
    return await mongo.collection(audit_repo.AUDIT_LOGS).count_documents(filters or {})


class _Role:
    """最小角色替身：`primary_role_of` 只用到 `.code`。"""

    def __init__(self, code: str) -> None:
        self.code = code


# --------------------------------------------------------------------- 正常路径
async def test_record_success_writes_exactly_one_document(client):
    result = await svc.audit_service.record("doc.create", actor="U000001",
                                            target_type="doc", target_id="DOC0001",
                                            after={"title": "报销制度"})
    assert result.written is True
    assert result.degraded is False
    assert result.audit_id and result.audit_id.startswith("LOG")
    docs = await _docs()
    assert len(docs) == 1
    doc = docs[0]
    assert doc["actor"] == "U000001"
    assert doc["action"] == "doc.create"
    assert doc["target_type"] == "doc"
    assert doc["outcome"] == "success"
    assert doc["ip"] == "unknown"
    assert doc["ua"] is None
    assert isinstance(doc["ts"], int)
    assert doc["snapshot_state"] == SnapshotState.FULL.value


async def test_audit_id_date_segment_is_derived_from_ts(client):
    """`_id` 的日期段必须与 `ts` **同源**（防伪造时间、防排序错乱）。"""
    await svc.audit_service.record("doc.create", actor="U000001")
    doc = (await _docs())[0]
    assert doc["_id"] == f"LOG{local_date_of(doc['ts'])}{doc['_id'][-4:]}"
    assert len(doc["_id"]) == 15, "LOG + 8 位日期 + 4 位序列"


async def test_unregistered_action_is_stored_as_unknown(client):
    """AC-10-20：`doc.modify` 未注册 → 落库 `unknown.doc.modify`，事件**不丢**。"""
    await svc.audit_service.record("doc.modify", actor="U000001", target_id="DOC0001")
    doc = (await _docs())[0]
    assert doc["action"] == "unknown.doc.modify"
    assert svc.audit_service.action_name_of(doc["action"]) == "未注册动作：doc.modify"


async def test_action_name_is_resolved_from_dictionary(client):
    """前端不硬编码动作名：中文名由后端字典补齐（含别名取统一名）。"""
    assert svc.audit_service.action_name_of("doc.enable") == "启用知识单元"
    assert svc.audit_service.action_name_of("doc.update") == "编辑知识单元"


async def test_diff_only_reduction_and_changed_fields(client):
    """S-02：只记发生变化的字段；`changed_fields` 是顶层键名差集。"""
    await svc.audit_service.record(
        "doc.update", actor="U000001", target_id="DOC0001",
        before={"title": "旧", "status": "enabled"}, after={"title": "新", "status": "enabled"},
        reason="标题写错了")
    doc = (await _docs())[0]
    assert doc["changed_fields"] == ["title"]
    assert doc["before"] == {"title": "旧"}
    assert doc["after"] == {"title": "新"}
    assert doc["snapshot_state"] == SnapshotState.DIFF.value
    assert doc["reason"] == "标题写错了"


async def test_required_snapshot_missing_drops_snapshot_but_keeps_event(client):
    """S-04 / R-11：必带而缺失 → 记 ERROR + `dropped`，**审计仍然写入**。"""
    result = await svc.audit_service.record(
        "doc.permission_change", actor="U000001", target_id="DOC0001", reason="按 PRD 调整")
    assert result.written is True
    doc = (await _docs())[0]
    assert doc["snapshot_state"] == SnapshotState.DROPPED.value
    assert doc["before"] is None and doc["after"] is None


async def test_reason_missing_drops_snapshot(client):
    """G-11：要求原因的动作，原因不足 5 字同样置 `dropped`（但不拒业务）。"""
    result = await svc.audit_service.record(
        "config.update", actor="U000001", target_type="config", target_id="llm.temperature",
        before={"llm.temperature": 0.2}, after={"llm.temperature": 0.5}, reason="短")
    assert result.written is True
    doc = (await _docs())[0]
    assert doc["snapshot_state"] == SnapshotState.DROPPED.value
    assert doc["before"] is None


async def test_outcome_is_fixed_for_two_auth_actions(client):
    """§3.5：`auth.login_fail` 固定 `failure`，`auth.denied` 固定 `denied`。"""
    await svc.audit_service.record("auth.login_fail", actor="ghost", outcome="success")
    await svc.audit_service.record("auth.denied", actor="U000003", outcome="success")
    rows = {d["action"]: d for d in await _docs()}
    assert rows["auth.login_fail"]["outcome"] == "failure"
    assert rows["auth.denied"]["outcome"] == "denied"


async def test_missing_actor_falls_back_to_system_and_is_flagged(client):
    """§3.5：拿不到操作人 → `actor="system"` + `extra.actor_missing=true`，**照写**。"""
    await svc.audit_service.record("audit.export", target_type="audit", after={"rows": 1})
    doc = (await _docs())[0]
    assert doc["actor"] == "system"
    assert doc["extra"]["actor_missing"] is True


async def test_snapshot_over_limit_is_truncated(client):
    """S-03 / R-10：超 16 KB 的字段换成 `{"__truncated__": true, len, sha256}`。"""
    big = "长" * 20000
    result = await svc.audit_service.record(
        "doc.update", actor="U000001", target_id="DOC0001",
        before={"content": big}, after={"content": big + "改"}, reason="压测快照截断验证")
    assert result.snapshot_state == SnapshotState.TRUNCATED.value
    doc = (await _docs())[0]
    assert doc["before"]["content"]["__truncated__"] is True
    assert doc["after"]["content"]["__truncated__"] is True
    assert len(json.dumps(doc["before"], ensure_ascii=False)) < 16384


async def test_extra_is_redacted_and_truncated_too(client):
    """§3.5：`extra` 与 `before`/`after` **走同一套**脱敏与截断。"""
    await svc.audit_service.record(
        "faq.mine", actor="system", target_type="candidate",
        extra={"window_days": 30, "api_key": "sk-abcdefghijklmnopqrst"})
    doc = (await _docs())[0]
    assert doc["extra"]["window_days"] == 30
    assert doc["extra"]["api_key"] == "***"
    assert "api_key" in doc["redacted_keys"]


# --------------------------------------------------------------------- 故障注入
async def test_record_never_raises_when_insert_fails(client, monkeypatch):
    """AC-10-06：注入 Mongo 异常，`record()` 必须正常返回且**不抛**。"""
    async def boom(_doc):
        raise RuntimeError("模拟 Mongo 不可用")

    monkeypatch.setattr(audit_repo, "insert", boom)
    result = await svc.audit_service.record("doc.create", actor="U000001")
    assert result.written is False
    assert result.degraded is True
    assert result.spool_path is not None
    assert result.error_code == "AUD-4001"
    lines = svc.audit_service._spool.pending_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["action"] == "doc.create"


async def test_record_never_raises_when_redactor_fails(client, monkeypatch):
    """DEC-10-5：脱敏器异常 → **丢弃整个快照**，但审计事件照写。"""
    from app.services import audit_redactor

    def boom(*_a, **_kw):
        raise RuntimeError("模拟脱敏失败")

    monkeypatch.setattr(audit_redactor, "_redact", boom)
    result = await svc.audit_service.record("doc.update", actor="U000001",
                                            before={"a": 1}, after={"a": 2}, reason="改一下")
    assert result.written is True
    assert result.snapshot_state == SnapshotState.DROPPED.value
    doc = (await _docs())[0]
    assert doc["before"] is None and doc["after"] is None


async def test_record_never_raises_on_unexpected_error(client, monkeypatch):
    """兜底：连组装文档都炸了，也只是返回 `AUD-5003`，绝不把异常抛给业务。"""
    async def boom(*_a, **_kw):
        raise RuntimeError("模拟组装阶段的未预期异常")

    monkeypatch.setattr(AuditService, "_build", boom)
    result = await svc.audit_service.record("doc.create", actor="U000001")
    assert result.written is False and result.degraded is True
    assert result.error_code == "AUD-5003"


async def test_retry_once_leaves_exactly_one_record(client, monkeypatch):
    """AC-10-07：注入"第一次失败、第二次成功"，库里**恰好一条**。"""
    calls = {"n": 0}
    real = audit_repo.insert

    async def flaky(doc):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("第一次失败")
        return await real(doc)

    monkeypatch.setattr(audit_repo, "insert", flaky)
    result = await svc.audit_service.record("doc.create", actor="U000001")
    assert result.written is True
    assert calls["n"] == 2
    assert await _count() == 1


async def test_duplicate_id_after_timeout_is_treated_as_success(client, monkeypatch):
    """超时后重试撞 `_id`，且事件内容一致：说明上一次**其实写成功了**，当成功。

    不能留两条，也不能当失败——"一次操作两条审计"与"操作成功却记失败"都不可接受。
    """
    calls = {"n": 0}
    real = audit_repo.insert

    async def half_written(doc):
        calls["n"] += 1
        if calls["n"] == 1:
            await real(doc)                       # 真的写进去了
            raise RuntimeError("但客户端超时了")
        return await real(doc)                    # 第二次必然撞 _id

    monkeypatch.setattr(audit_repo, "insert", half_written)
    result = await svc.audit_service.record("doc.create", actor="U000001")
    assert result.written is True
    assert await _count() == 1


async def test_id_collision_on_first_attempt_resequences(client, monkeypatch):
    """序列回退（进程重启）撞号：第一次尝试要**换号**而不是当成幂等。"""
    from pymongo.errors import DuplicateKeyError

    calls = {"n": 0}
    real = audit_repo.insert

    async def collide_once(doc):
        calls["n"] += 1
        if calls["n"] == 1:
            raise DuplicateKeyError("E11000 duplicate key")
        return await real(doc)

    monkeypatch.setattr(audit_repo, "insert", collide_once)
    result = await svc.audit_service.record("doc.create", actor="U000001")
    assert result.written is True and calls["n"] == 2
    assert await _count() == 1


async def test_id_collision_with_a_foreign_record_never_silently_drops(client, monkeypatch):
    """★ 撞的是**别人的**号时，绝不能把 DuplicateKeyError 当成"已写过"。

    这是压测里实测到的一个真问题：序列严重落后时，若干条记录会一路撞号，
    若把"重试时撞号"一律当成功，这些事件就**静默消失**了（既不进库也不进补偿文件）。
    正确行为是：换号重试直到成功，库里必须真的出现这条记录。
    """
    from pymongo.errors import DuplicateKeyError

    # 先占掉若干号，让前几次尝试必然撞在一个"别人的"记录上
    today = local_date_of(svc.now_ms())
    await mongo.collection(audit_repo.AUDIT_LOGS).insert_many([
        {"_id": f"LOG{today}{i:04d}", "ts": svc.now_ms() - 10_000, "actor": "system",
         "action": "doc.create", "target_type": "doc", "target_id": "-",
         "actor_role": "unknown", "outcome": "success", "snapshot_state": "full",
         "changed_fields": []}
        for i in range(1, 5)])

    real = audit_repo.insert
    seen: list[str] = []

    async def collide_four_times(doc):
        seen.append(doc["_id"])
        if len(seen) <= 4:
            raise DuplicateKeyError("E11000 duplicate key")
        return await real(doc)

    monkeypatch.setattr(audit_repo, "insert", collide_four_times)
    result = await svc.audit_service.record("doc.update", actor="U000001",
                                            before={"a": 1}, after={"a": 2}, reason="换号验证")
    assert result.written is True, "撞号必须换号重试成功，而不是被当成'已写过'"
    assert len(seen) == 5
    assert await _count({"_id": result.audit_id}) == 1
    assert await _count({"action": "doc.update"}) == 1


async def test_spool_then_replay_is_idempotent(client, monkeypatch):
    """AC-10-08：停库期间的记录落补偿文件；恢复后回放**不产生重复**。"""
    real = audit_repo.insert

    async def down(_doc):
        raise RuntimeError("模拟 Mongoa 不可用")

    monkeypatch.setattr(audit_repo, "insert", down)
    result = await svc.audit_service.record("doc.delete", actor="U000001",
                                           target_id="DOC0009")
    assert result.degraded is True
    spool_line = json.loads(
        svc.audit_service._spool.pending_path.read_text(encoding="utf-8").strip())
    assert spool_line["extra"]["spool_id"] == spool_line["_id"], "回放幂等键必须落盘"

    monkeypatch.setattr(audit_repo, "insert", real)
    replay = await svc.audit_service.replay_spool()
    assert replay.claimed is True and replay.replayed == 1 and replay.failed == 0
    assert await _count({"_id": spool_line["_id"]}) == 1
    assert await _count({"action": "audit.spool_replay"}) == 1

    # 再回放一次：待回放文件已被归档，claim 不到东西
    again = await svc.audit_service.replay_spool()
    assert again.claimed is False

    # 人为把同一条再塞回补偿文件：幂等键必须挡住重复入库
    svc.audit_service._spool.append(spool_line)
    third = await svc.audit_service.replay_spool()
    assert third.claimed is True
    assert await _count({"_id": spool_line["_id"]}) == 1, "重复回放不能产生第二条"


async def test_replay_skips_when_lock_is_held(client):
    """AUD-3003：并发触发回放时，后者被拒绝（不抛异常，返回结构化结果）。"""
    async with svc.audit_service._replay_lock:
        result = await svc.audit_service.replay_spool()
    assert result.claimed is False
    assert result.error_code == "AUD-3003"


async def test_breaker_short_circuits_to_spool(client, monkeypatch):
    """§4.2：连续失败达阈值即熔断，期间**直接**落补偿文件（`AUD-4002`）。"""
    service = svc.audit_service
    service._breaker.threshold = 2
    service._breaker.failures = 0

    async def down(_doc):
        raise RuntimeError("模拟 Mongo 不可用")

    monkeypatch.setattr(audit_repo, "insert", down)
    await service.record("doc.create", actor="U000001")
    await service.record("doc.create", actor="U000001")
    assert service._breaker.is_open() is True

    async def up(doc):
        return str(doc["_id"])

    monkeypatch.setattr(audit_repo, "insert", up)
    result = await service.record("doc.create", actor="U000001")
    assert result.written is False
    assert result.error_code == "AUD-4002"
    assert service.health()["state"] == "degraded"


async def test_spool_unwritable_is_reported_not_raised(client, monkeypatch):
    """§4.2 第 3 段：补偿文件也写不进去 → `AUD-4003` + CRITICAL，业务仍不失败。"""
    async def down(_doc):
        raise RuntimeError("模拟 Mongo 不可用")

    def unwritable(_doc):
        raise OSError("磁盘满")

    monkeypatch.setattr(audit_repo, "insert", down)
    monkeypatch.setattr(svc.audit_service._spool, "append", unwritable)
    result = await svc.audit_service.record("doc.create", actor="U000001")
    assert result.written is False and result.degraded is True
    assert result.error_code == "AUD-4003"
    assert svc.audit_service.health()["spool_writable"] is False


async def test_query_failure_never_looks_like_empty_result(client, monkeypatch):
    """AC-10-14 / R-13：查询故障必须报 `AUD-4004`，**不得**返回 200 空列表。"""
    async def down(_filters):
        raise RuntimeError("模拟查询故障")

    monkeypatch.setattr(audit_repo, "count", down)
    with pytest.raises(BizError) as exc:
        await svc.audit_service.query({}, 1, 20)
    assert exc.value.spec.code == "AUD-4004"
    assert exc.value.spec.http == 503


async def test_detail_not_found_raises_3001(client):
    with pytest.raises(BizError) as exc:
        await svc.audit_service.detail("LOG202609230001")
    assert exc.value.spec.code == "AUD-3001"


async def test_dropped_detail_returns_null_snapshots(client):
    """§3.2：`snapshot_state=dropped` 不是错误——200 + `before/after = null`。"""
    await svc.audit_service.record("doc.permission_change", actor="U000001",
                                   target_id="DOC0001", reason="按 PRD 调整")
    doc = (await _docs())[0]
    detail = await svc.audit_service.detail(doc["_id"])
    assert detail["before"] is None and detail["after"] is None
    assert detail["snapshot_state"] == SnapshotState.DROPPED.value


# --------------------------------------------------------------------- 编号与工具函数
async def test_sequence_is_aligned_from_database_on_startup(client):
    """启动钩子把当日序列顶到库里最大值，避免每次重启都从 1 开始撞号。"""
    today = local_date_of(svc.now_ms())
    await mongo.collection(audit_repo.AUDIT_LOGS).insert_one(
        {"_id": f"LOG{today}0007", "ts": svc.now_ms(), "actor": "system",
         "action": "doc.create", "target_type": "doc", "target_id": "-",
         "actor_role": "unknown", "outcome": "success", "snapshot_state": "full",
         "changed_fields": []})
    svc.audit_service.reset()
    await svc.audit_service.startup()
    result = await svc.audit_service.record("doc.create", actor="U000001")
    assert result.audit_id == f"LOG{today}0008"


async def test_daily_sequence_restarts_on_new_day(client):
    """跨天要重新从 0001 开始（`_id` 的日期段与 `ts` 同源）。"""
    service = AuditService()
    first = await service._next_id(svc.now_ms())
    assert first.endswith("0001")
    tomorrow = svc.now_ms() + 24 * 3600 * 1000
    second = await service._next_id(tomorrow)
    assert second.endswith("0001")
    assert local_date_of(tomorrow) in second


async def test_primary_role_prefers_the_most_powerful_role():
    """§2.1 的 `actor_role` 是单值；多角色时取权限最高者（附模块内决策 DEC-10-9）。"""
    assert primary_role_of([_Role("asker"), _Role("sys_admin")]) == "sys_admin"
    assert primary_role_of([_Role("kb_admin"), _Role("asker")]) == "kb_admin"
    assert primary_role_of([_Role("management")]) == "management"
    assert primary_role_of([]) == "unknown"


def test_client_ip_prefers_first_global_hop():
    """内网部署里 `X-Forwarded-For` 常带内网代理，取第一个**公网**地址才有意义。"""
    from starlette.requests import Request

    def build(forwarded: str | None, host: str = "10.0.0.9") -> Request:
        headers = [] if forwarded is None else [(b"x-forwarded-for", forwarded.encode())]
        return Request({"type": "http", "method": "GET", "path": "/", "query_string": b"",
                        "headers": headers, "client": (host, 1234),
                        "server": ("test", 80), "scheme": "http"})

    assert client_ip(build("8.8.8.8, 10.0.0.1")) == "8.8.8.8"
    assert client_ip(build("10.0.0.5, 10.0.0.1")) == "10.0.0.1"
    assert client_ip(build("not-an-ip, 10.0.0.2")) == "10.0.0.2"
    assert client_ip(build(None, host="192.168.1.9")) == "192.168.1.9"


def test_format_ts_is_local_readable():
    """导出列用本地 `YYYY-MM-DD HH:MM:SS`，便于外部脚本按列索引解析。"""
    ts = int(datetime(2026, 9, 23, 10, 20, 0).timestamp() * 1000)
    assert format_ts(ts) == "2026-09-23 10:20:00"
    assert local_date_of(ts) == "20260923"
