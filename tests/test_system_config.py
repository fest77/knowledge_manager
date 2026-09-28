# -*- coding: utf-8 -*-
"""系统配置（E22）的测试。

覆盖：预置项与默认值、分组顺序、读写、热更新、类型与范围校验、
不可编辑项、批量原子性、bootstrap 不覆盖已有值。
"""
from __future__ import annotations

import pytest

from app.core.enums import ConfigGroup
from app.infra.mongo import mongo
from app.repositories import config_repo
from app.services.config_service import PRESET, config_service
from tests.conftest import DEMO_USERS, token_of

pytestmark = pytest.mark.anyio

CFG = "/api/v1/system/config"


async def _headers(client, account: str = DEMO_USERS["sys_admin"]) -> dict:
    return {"Authorization": f"Bearer {await token_of(client, account)}"}


# ================================================================ 读取
async def test_get_returns_all_presets_in_group_order(client):
    resp = await client.get(CFG, headers=await _headers(client))
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]

    assert len(items) == len(PRESET) == 29
    assert [i["key"] for i in items] == [s.key for s in PRESET]

    groups = [i["group"] for i in items]
    order = [g.value for g in ConfigGroup]
    assert groups == sorted(groups, key=order.index), "应按 ConfigGroup 声明顺序分组"


async def test_get_exposes_type_default_and_bounds_for_frontend(client):
    items = (await client.get(CFG, headers=await _headers(client))).json()["data"]["items"]
    by_key = {i["key"]: i for i in items}

    mult = by_key["retrieval.recall_multiplier"]
    assert mult["value"] == 5 and mult["default_value"] == 5
    assert mult["value_type"] == "int" and mult["minimum"] == 1 and mult["maximum"] == 20

    temp = by_key["llm.temperature"]
    assert temp["value"] == 0.2 and temp["value_type"] == "float"

    assert by_key["llm.model"]["editable"] is False, "模型名须改 .env，不可在界面编辑"


async def test_update_requires_reason(client):
    resp = await client.put(CFG, headers=await _headers(client),
                            json={"values": {"import.concurrency": 3}, "reason": "短"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "SYS-1001"


# ================================================================ 写入与热更新
async def test_put_changes_value_persists_and_hot_reloads(client):
    headers = await _headers(client)
    resp = await client.put(CFG, headers=headers,
                            json={"values": {"import.concurrency": 3,
                                             "gap.score_threshold": 0.8},
                                  "reason": "压测期间调整并发与缺口阈值"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["changed"] == ["gap.score_threshold", "import.concurrency"]
    assert data["unchanged"] == []

    # 落库
    row = await mongo.collection(config_repo.CONFIG).find_one({"_id": "import.concurrency"})
    assert row["value"] == 3
    assert row["version"] == 2, "每次成功修改 version 应 +1"
    assert row["updated_by"] == "U000001"

    # 内存热更新（无需重载）
    assert config_service.import_concurrency == 3
    assert config_service.gap_score_threshold == 0.8
    assert config_service.get_raw("import.concurrency") == 3


async def test_put_unchanged_value_writes_nothing(client):
    headers = await _headers(client)
    resp = await client.put(CFG, headers=headers,
                            json={"values": {"import.concurrency": 2},
                                  "reason": "值本来就是这样"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["changed"] == [] and data["unchanged"] == ["import.concurrency"]

    row = await mongo.collection(config_repo.CONFIG).find_one({"_id": "import.concurrency"})
    assert row["version"] == 1, "值没变就不该写库、也不该 +version"


async def test_put_is_all_or_nothing(client):
    """一批里只要有一项非法，**整批都不写**（先全量校验后写）。"""
    headers = await _headers(client)
    resp = await client.put(CFG, headers=headers,
                            json={"values": {"import.concurrency": 4,
                                             "gap.score_threshold": 9.9},
                                  "reason": "一项合法一项越界"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "SYS-1001"

    row = await mongo.collection(config_repo.CONFIG).find_one({"_id": "import.concurrency"})
    assert row["value"] == 2, "合法项也不能被写进去"
    assert config_service.import_concurrency == 2


# ================================================================ 校验
@pytest.mark.parametrize("values, why", [
    ({"nope.key": 1}, "未知键"),
    ({"llm.model": "gpt-4"}, "不可编辑项（须改 .env）"),
    ({"import.concurrency": "abc"}, "类型错"),
    ({"import.concurrency": 99}, "超上限"),
    ({"import.concurrency": 0}, "低于下限"),
    ({"llm.temperature": 5.0}, "超上限(float)"),
    ({"llm.temperature": True}, "布尔冒充数字"),
])
async def test_put_rejects_invalid_values(client, values, why):
    headers = await _headers(client)
    resp = await client.put(CFG, headers=headers,
                            json={"values": values, "reason": f"测试非法输入：{why}"})
    assert resp.status_code == 400, f"{why} 应被拒"
    assert resp.json()["code"] == "SYS-1001"


async def test_put_rejects_empty_values(client):
    resp = await client.put(CFG, headers=await _headers(client),
                            json={"values": {}, "reason": "什么都不改"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "SYS-1001"


# ================================================================ 生命周期
async def test_bootstrap_does_not_overwrite_existing_values(client):
    """管理员改过的值不能被"重跑种子"或"重启"冲掉。"""
    await client.put(CFG, headers=await _headers(client),
                     json={"values": {"retrieval.recall_multiplier": 9},
                           "reason": "演示前调整召回倍数"})
    inserted = await config_service.bootstrap()
    assert inserted == 0, "预置项都已存在，不应重复插入"
    assert config_service.recall_multiplier == 9, "bootstrap 不能覆盖已有值"


async def test_reset_clears_cache_then_bootstrap_restores(client):
    """`reset()` 后内存为空，`bootstrap()` 能从库里恢复（而不是回到默认值）。"""
    await client.put(CFG, headers=await _headers(client),
                     json={"values": {"import.concurrency": 5},
                           "reason": "验证 reset 不丢库里的值"})
    config_service.reset()
    assert config_service._values == {}                    # noqa: SLF001 — 测试要断言内部状态

    await config_service.bootstrap()
    assert config_service.import_concurrency == 5


async def test_reload_picks_up_direct_db_change(client):
    """直接改库后 `reload()` 应生效——多进程/多实例场景下的兜底。"""
    await mongo.collection(config_repo.CONFIG).update_one(
        {"_id": "faq.cache_sim_threshold"}, {"$set": {"value": 0.95}})
    await config_service.reload()
    assert config_service.faq_cache_sim_threshold == 0.95


async def test_in_memory_getters_never_hit_db(client):
    """`get_raw` 是纯内存读：把库断掉也应照常工作（热更新的意义所在）。"""
    config_service.reset()
    # reset 后回落到预置默认值，不需要库
    assert config_service.recall_multiplier == 5
    assert config_service.import_concurrency == 2
