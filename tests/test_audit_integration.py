# -*- coding: utf-8 -*-
"""模块 10 的集成与"结构性"验收。

这些用例验的不是单个函数，而是**架构约束**：

| 约束 | 靠什么验 |
|---|---|
| AC-10-04 ER-05 唯一写入者 | 全库 AST 静态扫描 + 一条"违规写法"的负例 |
| AC-10-18 / AC-10-19 索引与永久保留 | `list_indexes()`（含"不得有 TTL"） |
| AC-10-05 审计失败不阻断业务 | 注入写入故障后调真实业务接口 |
| ER-01 错误码前缀唯一 | `Err` 表的码进 `AUD-` 且不与其他模块撞码 |
"""
from __future__ import annotations

import ast
import json
import time
from pathlib import Path

import pytest

from app.core.config import settings
from app.core.errors import Err
from app.infra.mongo import mongo
from app.repositories import audit_repo
from app.services.audit_service import audit_service, now_ms, primary_role_of
from tests.conftest import token_of

pytestmark = pytest.mark.anyio

# 能改动 `audit_logs` 的 Mongo 写方法
WRITE_METHODS = frozenset({
    "insert_one", "insert_many", "update_one", "update_many", "delete_one",
    "delete_many", "replace_one", "find_one_and_update", "find_one_and_replace",
    "find_one_and_delete", "bulk_write",
})
# 唯一允许出现这些调用的文件（ER-05：唯一写入者是 AuditService 的仓储）。
# 三条例外都是**非业务路径**，且各自有明确理由：
#   ① `app/repositories/audit_repo.py` —— 唯一写入者本体；
#   ② `scripts/audit_bench.py` —— 压测数据生成器，只往压测库灌假数据；
#   ③ `scripts/audit_storage_guard.py` —— 存储层护栏探针：它的**目的**就是
#      真的试一次 `update_one`，用"被 Mongo 拒绝"来证明权限收敛生效了。
# 路径一律相对**项目根**，这样无论扫描哪一层，豁免匹配都一致。
ALLOWED_WRITERS = (
    "app/repositories/audit_repo.py",
    "scripts/audit_bench.py",
    "scripts/audit_storage_guard.py",
)
AUDIT_MARKERS = ("AUDIT_LOGS", "audit_logs")


def audit_write_violations(root: Path, base: Path | None = None) -> list[str]:
    """扫描代码库，找出在 `ALLOWED_WRITERS` 之外写 `audit_logs` 的调用点。

    实现要点：不仅要认 `mongo.collection(AUDIT_LOGS).insert_one(...)`，
    还要认"先把集合句柄赋给局部变量再写"这种绕法（`coll = mongo.collection(AUDIT_LOGS)`
    之后 `coll.insert_one(...)`）——否则检查器只是看着严格，实际一绕就过。
    """
    base = base or root
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        parts = set(path.parts)
        if "__pycache__" in parts or ".venv" in parts:
            continue
        rel = path.relative_to(base).as_posix()
        if any(rel.endswith(allowed) for allowed in ALLOWED_WRITERS):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                text = ast.unparse(node.value)
                if any(m in text for m in AUDIT_MARKERS):
                    aliases.update(t.id for t in node.targets if isinstance(t, ast.Name))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in WRITE_METHODS:
                continue
            target = ast.unparse(node.func.value)
            if any(m in target for m in AUDIT_MARKERS) or target in aliases:
                violations.append(f"{rel}:{node.lineno} {target}.{node.func.attr}")
    return violations


# --------------------------------------------------------------------- ER-05
def test_ac_10_04_audit_logs_has_exactly_one_writer():
    """AC-10-04：`app/` 与 `scripts/` 里都不存在非法写入点。

    `tests/` 被排除：用例需要直接插入审计记录来"摆场景"（如造一条两年前的记录
    验 TTL），那是**构造数据**而不是业务写入路径。例外清单见 `ALLOWED_WRITERS`，
    每条都写明了理由——豁免必须是**枚举**的，不能是"目录整体放过"。
    """
    root = settings.web_dir.parents[0]
    violations = (audit_write_violations(root / "app", root)
                  + audit_write_violations(root / "scripts", root))
    assert violations == []


def test_purge_script_delegates_deletion_to_the_repository():
    """清理脚本不得自己 `delete_many`：删除入口只能有一个（可被 review 的位置）。"""
    purge = (settings.web_dir.parents[0] / "scripts" / "purge_audit.py")
    text = purge.read_text(encoding="utf-8")
    assert ".delete_many(" not in text
    assert "purge_before(" in text


def test_ac_10_04_checker_catches_a_violating_snippet(tmp_path):
    """AC-10-04 的负例：故意写一段违规代码，检查器必须抓到它。

    没有这条，上面那条"通过"就无法区分"真的干净"和"检查器没在工作"。
    """
    bad = tmp_path / "app" / "oops.py"
    bad.parent.mkdir(parents=True)
    bad.write_text(
        "from app.repositories.audit_repo import AUDIT_LOGS\n"
        "from app.infra.mongo import mongo\n\n\n"
        "async def leak(doc):\n"
        "    coll = mongo.collection(AUDIT_LOGS)\n"
        "    await coll.insert_one(doc)\n"
        "    await mongo.collection('audit_logs').delete_one({'_id': doc['_id']})\n",
        encoding="utf-8")
    found = audit_write_violations(tmp_path)
    assert len(found) == 2, found
    assert all("oops.py" in f for f in found)


def test_repository_has_no_update_or_delete_for_audit_logs():
    """DEC-10-2 的服务层：仓储**不提供** `update` / `delete` 方法（append-only）。"""
    banned = {"update", "delete", "update_one", "delete_one", "delete_many",
              "remove", "replace_one"}
    assert banned & set(dir(audit_repo)) == set()


# --------------------------------------------------------------------- 存储层
async def test_ac_10_19_indexes_are_all_present(client):
    """AC-10-19：`ts desc` / `actor+ts desc` / `target_type+target_id` /
    `action+ts desc` / `spool_id`（唯一稀疏）。"""
    cursor = await mongo.collection(audit_repo.AUDIT_LOGS).list_indexes()
    indexes = {i["name"]: i for i in await cursor.to_list(length=None)}

    # 注意：Mongo 读回的是 SON（dict 子类），不能直接和 `[("ts",-1)]` 比
    def keys(name: str) -> dict[str, int]:
        return dict(indexes[name]["key"])

    assert keys("ix_ts") == {"ts": -1}
    assert keys("ix_actor_ts") == {"actor": 1, "ts": -1}
    assert keys("ix_target") == {"target_type": 1, "target_id": 1}
    assert keys("ix_action_ts") == {"action": 1, "ts": -1}
    spool = indexes["uq_spool_id"]
    assert keys("uq_spool_id") == {"extra.spool_id": 1}
    assert spool.get("unique") is True and spool.get("sparse") is True


async def test_ac_10_18_no_ttl_index(client):
    """AC-10-18：保留策略是**永久**，索引清单里不得出现 `expireAfterSeconds`。"""
    cursor = await mongo.collection(audit_repo.AUDIT_LOGS).list_indexes()
    for index in await cursor.to_list(length=None):
        assert "expireAfterSeconds" not in index, f"{index['name']} 带了 TTL，违反 DEC-10-7"


async def test_ac_10_18_old_records_survive_reconnect(client):
    """AC-10-18：插入两年前的记录，重连后仍可查到（没有 TTL 清理）。"""
    two_years_ago = now_ms() - 730 * 24 * 3600 * 1000
    await mongo.collection(audit_repo.AUDIT_LOGS).insert_one({
        "_id": "LOG202409230001", "ts": two_years_ago, "actor": "system",
        "actor_role": "unknown", "action": "doc.create", "target_type": "doc",
        "target_id": "-", "outcome": "success", "snapshot_state": "full",
        "changed_fields": []})
    await mongo.close()
    await mongo.connect()
    assert await mongo.collection(audit_repo.AUDIT_LOGS).count_documents(
        {"_id": "LOG202409230001"}) == 1


async def test_spool_id_unique_index_blocks_duplicate_replay(client):
    """`extra.spool_id` 的唯一索引是回放幂等的**存储层**保障（不是只靠代码约定）。"""
    from pymongo.errors import DuplicateKeyError

    doc = {"_id": "LOG202609230099", "ts": now_ms(), "actor": "system",
           "actor_role": "unknown", "action": "doc.create", "target_type": "doc",
           "target_id": "-", "outcome": "success", "snapshot_state": "full",
           "changed_fields": [], "extra": {"spool_id": "spool-1"}}
    coll = mongo.collection(audit_repo.AUDIT_LOGS)
    await coll.insert_one(dict(doc))
    with pytest.raises(DuplicateKeyError):
        await coll.insert_one({**doc, "_id": "LOG202609230100"})


# --------------------------------------------------------------------- 脱敏红线
async def test_ac_10_09_no_credential_survives_a_full_collection_scan(client):
    """AC-10-09 的**库级**复核：整个 `audit_logs` 里不得出现 bcrypt 串、明文口令或 `sk-` 密钥。

    与 `test_audit_redaction.py` 的对象级用例互补：那边证明"脱敏函数是对的"，
    这边证明"落到库里的东西确实被脱敏了"——中间还隔着 diff 归约、16 KB 截断
    与两次脱敏调用，任何一环漏掉都会在这里暴露。
    """
    await audit_service.record(
        "user.create", actor="U000001", target_type="user", target_id="U000009",
        after={"username": "newbie", "password": "Plain@12345",
               "password_hash": "$2b$12$" + "0" * 53,
               "config": {"minio_secret_key": "minioadmin",
                          "llm_api_key": "sk-abcdefghijklmnop"},
               # 键名没命中黑名单，只能靠**值正则**兜住
               "note": "$2b$12$" + "1" * 53})
    await audit_service.record(
        "config.update", actor="U000001", target_type="config",
        target_id="llm.temperature", before={"llm.temperature": 0.2},
        after={"llm.temperature": 0.5}, reason="脱敏红线复扫")

    docs = await mongo.collection(audit_repo.AUDIT_LOGS).find({}).to_list(length=None)
    dump = json.dumps(docs, ensure_ascii=False, default=str)
    for secret in ("Plain@12345", "$2b$", "minioadmin", "sk-abcdefghijklmnop"):
        assert secret not in dump, f"审计库里残留了敏感值：{secret}"

    row = next(d for d in docs if d["action"] == "user.create")
    assert {"password", "password_hash", "minio_secret_key", "llm_api_key"} <= set(
        row["redacted_keys"]), "脱敏必须自证：命中键名要写进 redacted_keys"
    assert "value_regex:bcrypt" in row["redacted_keys"], "值正则兜底也必须被记录"
    assert row["snapshot_state"] == "redacted"


# --------------------------------------------------------------------- 不阻断业务
async def test_ac_10_05_audit_failure_does_not_block_business(client, monkeypatch):
    """AC-10-05：审计写入持续抛异常，业务接口**仍返回成功**。

    这里用当前已落地的业务写接口（`PUT /system/config`）与登录接口验证；
    `role.grant` / `user.disable` / `doc.permission_change` 三个接口随
    模块 02 / 05 落地后按同一契约接入（见模块 10 §7.2）。
    """
    async def down(_doc):
        raise RuntimeError("模拟 audit_logs 不可写")

    monkeypatch.setattr(audit_repo, "insert", down)
    token = await token_of(client, "lina")

    resp = await client.put("/api/v1/system/config", headers={
        "Authorization": f"Bearer {token}"},
        json={"values": {"llm.temperature": 0.7}, "reason": "审计故障下的业务可用性验证"})
    assert resp.status_code == 200
    assert resp.json()["code"] == 0
    assert resp.json()["data"]["changed"] == ["llm.temperature"]

    from app.services.config_service import config_service
    assert config_service.get_raw("llm.temperature") == 0.7

    # 审计确实降级了（进了补偿文件），而业务无感
    assert audit_service.health()["state"] == "degraded"
    assert audit_service._spool.pending_path.is_file()


async def test_login_still_works_when_audit_is_down(client, monkeypatch):
    """登录链路上挂了 `auth.login_fail` 审计；它挂掉不能把 401 变成 500。"""
    async def down(_doc):
        raise RuntimeError("模拟 audit_logs 不可写")

    monkeypatch.setattr(audit_repo, "insert", down)
    bad = await client.post("/api/v1/auth/login",
                            json={"username": "lina", "password": "WrongPass@1"})
    assert bad.status_code == 401
    assert bad.json()["code"] == "AUTH-2001"

    good = await client.post("/api/v1/auth/login",
                             json={"username": "lina", "password": "Demo@12345"})
    assert good.status_code == 200
    assert good.json()["data"]["access_token"]


# --------------------------------------------------------------------- 错误码
def test_aud_error_codes_follow_the_registry():
    """ER-01 + 段位约定：`AUD-` 前缀、码不重复、HTTP 状态与段位一致。"""
    codes = {spec.code: name for name, spec in vars(Err).items()
             if getattr(spec, "code", "").startswith("AUD-")}
    assert len(codes) == 18, f"模块 10 §5 定义 18 个码，实际 {len(codes)}"

    by_segment = {"1": 400, "2": 403, "5": 500}
    for code, name in codes.items():
        segment = code.split("-")[1][0]
        expected = by_segment.get(segment)
        if expected is not None:
            assert getattr(Err, name).http == expected, f"{code} 的 HTTP 与段位不符"


def test_all_error_codes_are_unique_across_modules():
    """前缀两两互异（ER-01），且**每个前缀的码数**都与该模块 Spec §5 的清单一致。

    按前缀分组计数而不是只比总数：总数对得上但某个模块多一个、另一个少一个时，
    只比总数是发现不了的；而"某个前缀的码数变了"几乎总意味着 Spec 与代码分叉了。
    """
    seen: dict[str, str] = {}
    for name, spec in vars(Err).items():
        if not getattr(spec, "code", ""):
            continue
        assert spec.code not in seen, f"{spec.code} 被 {name} 与 {seen[spec.code]} 共用"
        seen[spec.code] = name

    by_prefix: dict[str, int] = {}
    for code in seen:
        prefix = code.split("-")[0]
        by_prefix[prefix] = by_prefix.get(prefix, 0) + 1

    # ORG 是 24 而不是 23：`ORG-3009`（用户不存在）是 Step 5 开发期按 Spec §5 的
    # 补录项 —— 原表漏了"用户目标不存在"这一表达，已同步回 Spec
    # IMP 是 31：模块 04 Spec §5 的错误码表 1001~1009 / 2001~2002 / 3001~3008 /
    # 4001~4007 / 5001~5005 逐条登记
    # PERM 是 13：模块 05 Spec §5 的错误码表 1001~1006 / 2001~2002 / 3001~3002 /
    # 4001~4002 / 5001 逐条登记
    # QA 是 15：模块 06 Spec §5 的错误码表 1001~1003 / 2001~2003 / 3001~3003 /
    # 4001~4006 逐条登记
    assert by_prefix == {"SYS": 5, "AUTH": 14, "AUD": 18, "ORG": 24,
                         "DOC": 28, "IMP": 31, "PERM": 13, "QA": 15,
                         "FAQ": 25, "GAP": 18, "MET": 27}, by_prefix


# --------------------------------------------------------------------- 性能冒烟
async def test_ac_10_24_record_latency_smoke(client):
    """AC-10-24 的冒烟版：`record()` 的正常路径必须是"毫秒级"。

    严格预算（P95 < 5ms）由 `scripts/audit_bench.py` 在真实服务上量，
    这里只挡"某次改动让审计写入退化到几十毫秒"这种明显回归。
    """
    samples = []
    for i in range(20):
        started = time.perf_counter()
        await _record_one(i)
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    p95 = samples[int(len(samples) * 0.95) - 1]
    assert p95 < 100, f"record() P95 = {p95:.1f}ms，疑似退化（样本={samples[:5]}…）"


async def _record_one(index: int) -> None:
    await audit_service.record("doc.update", actor="U000001", target_type="doc",
                               target_id=f"DOC{index:04d}",
                               before={"title": "旧"}, after={"title": "新"},
                               reason="性能冒烟")


def test_primary_role_helper_is_used_by_the_service():
    """防止"写了个工具函数却没人用"：服务层必须真的用它取角色快照。"""
    source = (Path(settings.web_dir.parents[0]) / "app" / "services"
              / "audit_service.py").read_text(encoding="utf-8")
    assert "primary_role_of(" in source
    assert primary_role_of([]) == "unknown"
