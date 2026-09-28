# -*- coding: utf-8 -*-
"""模块 09 的测试：**指标桶写入 → 降采样读取 → 汇总幂等 → 权限与导出**。

## 替身与真实

| 项 | 说明 |
|---|---|
| **真实** | Mongo（E20 + 部分索引）、06 的 `qa_logs`（只读聚合）、03 的 `DocService`、07 的 `faqs` |
| **替身** | 无外部模型依赖；故障类用例用 `monkeypatch` 精确注入单点异常（**不**替换整个模块） |

## 五条主线（对应 Spec 的重点验收）

1. **UV 口径**（AC-09-01/02）：同人 10 分钟 3 问 → 当日 UV=1、PV=3；**跨天求并集不相加**
2. **AD-07 落地**（AC-09-12）：30 天趋势只读 `1d` 桶，一个 `1m` 桶都不碰
3. **降级可见**（AC-09-17/18）：读失败 → 零值卡片 + `degraded`；趋势读失败 → `MET-4001`
4. **权限边界**（AC-09-14）：`kb_admin` 必须收到 `MET-2003`，而不是 200 空数据
5. **汇总幂等**（AC-09-16）：`1m→1h` 连跑 3 次，数字不变（`$set` 覆盖）

## 两条测试纪律

- **一次只跑一个 pytest 进程**：所有用例共用 `kb001_test`，
  并发进程会互相 `drop_collection`，症状是"随机几个用例集体失败"。
- 时间一律**相对当前**构造（`now - N 小时`），绝不写死日期——
  否则用例会在某一天突然因为"跨天"而失败。
"""
from __future__ import annotations

import hashlib
import time

import pytest

from app.core.enums import MetricBucketType
from app.core.errors import BizError
from app.infra.mongo import mongo
from app.repositories import auth_repo, doc_repo, metric_repo, qa_repo
from app.services import metric_service as ms
from app.services.doc_service import doc_service
from app.services.metric_service import (
    LATENCY_BUCKETS, MetricService, align_ts, latency_bucket_of, metric_service,
)
from tests.conftest import token_of

pytestmark = pytest.mark.anyio

SYS_ADMIN = "lina"          # ROLE0003：metric:read + metric:export
KB_ADMIN = "zhangwei"       # ROLE0002：doc:* 一堆，**没有** metric:*
ASKER = "wangqiang"         # ROLE0001：只有 qa:* / perm:check
U1 = "U000001"
U2 = "U000003"
DEPT = "DEPT0005"
MINUTE = 60_000
HOUR = 3600_000
DAY = 86400_000


@pytest.fixture(autouse=True)
def _reset_metric_singleton():
    """★ 每个用例前后都清空看板单例的**进程内状态**。

    `MetricService` 是模块级单例，缓存与限流计数都活在进程里——
    `client` 夹具 drop 掉的是**集合**，清不掉它们。少了这一步，
    "上一个用例查过的区间"会让下一个用例拿到缓存结果（症状是"数据明明灌进去了却查不到"）。
    """
    metric_service.reset()
    yield
    metric_service.reset()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _bucket_id(bucket_type: str, key: str, ts: int) -> str:
    return metric_repo.bucket_id(bucket_type, key, ts)


async def _ask(ts_ms: int, *, user_id: str = U1, dept_id: str = DEPT,
               faq_hit: bool = False, source: str = "rag", prompt: int = 100,
               completion: int = 50, elapsed: int = 800, denied: int = 0,
               doc_ids: tuple[str, ...] = (), faq_id: str | None = None) -> None:
    """投递一轮问答的指标（等价于 06 在 `done` 之后做的事）。"""
    await metric_service.inc(
        user_id, dept_id, ts_ms=ts_ms, faq_hit=faq_hit, answer_source=source,
        token_prompt=prompt, token_completion=completion, elapsed_ms=elapsed,
        denied_chunk_cnt=denied, doc_ids=list(doc_ids), faq_id=faq_id)


async def _log(question: str, *, asked_at: int, user_id: str = U1,
               source: str = "rag", elapsed_ms: int = 800,
               recalled: list | None = None, allowed: list | None = None,
               denied: list | None = None, **extra) -> str:
    """直接写一条 `qa_logs`（09 只读它；写权限仍是 06 的，ER-06）。"""
    log_id = await qa_repo.next_log_id(asked_at)
    doc = {"_id": log_id, "task_id": f"t{log_id}", "message_id": f"m{log_id}",
           "session_id": "SESS20260101000001", "user_id": user_id,
           "dept_id": DEPT, "role_ids": ["ROLE0003"], "question": question,
           "asked_at": asked_at, "recalled_chunks": recalled or [],
           "allowed_chunks": allowed or [], "denied_chunks": denied or [],
           "faq_hit": False, "faq_id": None, "answer_source": source,
           "token_usage": None, "max_score": 0.5, "degraded": False,
           "feedback": None, "elapsed_ms": elapsed_ms, "retrieval_ms": 120,
           "auth_ms": 8, "rerank_ms": 60, "llm_ms": elapsed_ms - 200}
    doc.update(extra)
    await qa_repo.insert_log(doc)
    return log_id


def chunk(doc_id: str, score: float = 0.5) -> dict:
    return {"chunk_id": 1, "doc_id": doc_id, "score": score}


def _doc_hash(seed: str) -> str:
    """`DocService.create` 只接受 64 位十六进制 SHA256（DOC-1001）。"""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


# ================================================================ 纯函数 / 时间轴
def test_align_ts_cuts_days_in_shanghai_not_utc():
    """AC-09-19：23:50 与次日 00:10 必须落在**两个不同的日桶**。"""
    late = align_ts(_ms("2026-09-23 23:50"), "1d")
    early = align_ts(_ms("2026-09-24 00:10"), "1d")
    assert late != early, "跨零点必须切天"
    assert early - late == 86400, "两桶应正好差一天"
    # 反证：按 UTC 切天这两次提问会落到同一天（16:00~次日16:00）
    utc_late = int(_ms("2026-09-23 23:50") // 1000) // 86400
    utc_early = int(_ms("2026-09-24 00:10") // 1000) // 86400
    assert utc_late == utc_early, "这一步证明'用 UTC 会错'不是空话"


def test_align_ts_minute_and_hour_truncation():
    moment = _ms("2026-09-23 14:07:33")
    assert align_ts(moment, "1m") == _ms("2026-09-23 14:07:00") // 1000
    assert align_ts(moment, "1h") == _ms("2026-09-23 14:00:00") // 1000
    assert align_ts(moment, "1d") == _ms("2026-09-23 00:00:00") // 1000


def test_latency_bucket_boundaries():
    assert latency_bucket_of(0) == "0-500"
    assert latency_bucket_of(499) == "0-500"
    assert latency_bucket_of(500) == "500-1000"
    assert latency_bucket_of(1999) == "1000-2000"
    assert latency_bucket_of(12000) == "12000+"
    assert latency_bucket_of(10 ** 7) == "12000+"
    assert len(LATENCY_BUCKETS) == 7


def test_weighted_percentile_returns_none_without_samples():
    """R-7：空区间 → `null`，不是 0（0ms 会被读成"快得离谱"）。"""
    assert ms._weighted_percentile([0] * 7, 0.5) is None
    counts = [0, 0, 10, 0, 0, 0, 0]
    assert ms._weighted_percentile(counts, 0.50) == 1500
    assert ms._weighted_percentile(counts, 0.95) == 1500


def test_expand_metrics_splits_token_and_rejects_unknown():
    """R-4：token 必须拆成 prompt/completion 两条；未支持的指标直接拒绝。"""
    assert ms._expand_metrics(["token"]) == ["token_prompt", "token_completion"]
    assert ms._expand_metrics(["pv", "denied"]) == ["pv", "denied_chunk_cnt"]
    assert ms._expand_metrics([]) == ["pv", "uv"]
    with pytest.raises(BizError) as err:
        ms._expand_metrics(["pv", "token_total"])
    assert err.value.spec.code == "MET-1003", "含未支持的值要拒绝，不能静默忽略"


def test_pick_granularity_follows_the_spec_table():
    """§4.2 策略表：≤1 天 `1m`；1~7 天 `1h`；>7 天 `1d`。"""
    assert ms._pick_granularity(1) == "1m"
    assert ms._pick_granularity(7) == "1h"
    assert ms._pick_granularity(8) == "1d"
    assert ms._pick_granularity(30) == "1d"


def test_resolve_granularity_guards_minute_and_unknown():
    """MET-1005（1m 跨多天）/ MET-1010（取值不在枚举内）。"""
    assert ms._resolve_granularity(None, 30) == "1d"
    assert ms._resolve_granularity("1h", 3) == "1h"
    with pytest.raises(BizError) as err:
        ms._resolve_granularity("1m", 3)
    assert err.value.spec.code == "MET-1005"
    with pytest.raises(BizError) as err2:
        ms._resolve_granularity("5m", 3)
    assert err2.value.spec.code == "MET-1010"


def test_resolve_range_rejects_illegal_ranges():
    now = int(time.time() * 1000)
    with pytest.raises(BizError) as e1:
        ms._resolve_range(now, 0, None, None)
    assert e1.value.spec.code == "MET-1001"
    with pytest.raises(BizError) as e2:
        ms._resolve_range(now, 7, now - DAY, None)
    assert e2.value.spec.code == "MET-1001", "只给一端也要拒绝"
    with pytest.raises(BizError) as e3:
        ms._resolve_range(now, 7, now - DAY, now - 2 * DAY)
    assert e3.value.spec.code == "MET-1001", "start 必须早于 end"
    with pytest.raises(BizError) as e4:
        ms._resolve_range(now, 7, now - DAY, now + 30 * MINUTE)
    assert e4.value.spec.code == "MET-1002", "未来区间要拒绝（会刷出零值图）"
    with pytest.raises(BizError) as e5:
        ms._resolve_range(now, 7, now - 400 * DAY, now)
    assert e5.value.spec.code == "MET-1004", "跨度上限 366 天"


def test_slots_and_labels_have_equal_length_for_every_granularity():
    """AC-09-23 的前置：`x_axis` 长度只由区间与粒度决定。"""
    end = _ms("2026-09-23 14:07:00") // 1000
    for granularity, expect in (("1m", 8), ("1h", 1), ("1d", 1)):
        slots = ms._slots(end - 7 * 60, end, granularity)
        labels = ms._slot_labels(slots, granularity)
        assert len(slots) == len(labels) == expect, granularity
    slots = ms._slots(end - 3 * 86400, end, "1d")
    assert len(ms._slot_labels(slots, "1d")) == 4


# ================================================================ 写入侧
async def test_inc_writes_minute_bucket_and_deduped_uv_set(client):
    """AC-09-01（本模块的主干验收）：同人 10 分钟内 3 问 → 当日 UV=1、PV=3。"""
    now = qa_repo.now_ms()
    for offset in (0, 2 * MINUTE, 7 * MINUTE):
        await _ask(now + offset)
    day = align_ts(now, "1d")
    uv_doc = await metric_repo.get_bucket("global", "uv", day)
    assert uv_doc is not None, "日桶必须写出来"
    assert uv_doc["uv_set"] == [U1], "同一用户三次提问 → 集合大小仍是 1"
    assert uv_doc["granularity"] == "1d"
    assert "expire_at" not in uv_doc, "1d 桶永久保留，不能带 TTL 字段"
    total = sum(int((r.get("metrics") or {}).get("pv") or 0)
                for r in await _global_minute_buckets(day))
    assert total == 3, "分钟桶的 PV 必须逐次累加（3 次提问 = 3）"


async def test_inc_writes_latency_faq_and_doc_buckets(client):
    now = qa_repo.now_ms()
    await _ask(now, elapsed=1500, faq_hit=True, faq_id="FAQ000001",
               doc_ids=("DOC0001", "DOC0002", "DOC0002"))
    minute = align_ts(now, "1m")
    day = align_ts(now, "1d")
    latency = await metric_repo.get_bucket("latency", "1000-2000", minute)
    assert latency["metrics"]["pv"] == 1, "1500ms 落在 1000-2000 区间"
    faq = await metric_repo.get_bucket("faq", "FAQ000001", day)
    assert faq["metrics"]["faq_hit_cnt"] == 1
    doc1 = await metric_repo.get_bucket("doc", "DOC0001", day)
    doc2 = await metric_repo.get_bucket("doc", "DOC0002", day)
    assert doc1["metrics"]["pv"] == 1 and doc2["metrics"]["pv"] == 1, \
        "重复 doc_id 只算一次（去重在同一轮内完成）"


async def test_inc_does_not_write_faq_bucket_without_hit(client):
    await _ask(qa_repo.now_ms(), faq_hit=False, faq_id="FAQ000001")
    assert await metric_repo.count_buckets() == 1 + 1 + 1, \
        "未命中 → 只有全局分钟桶 / UV 日桶 / 延时桶 3 条"


async def test_inc_skips_uv_when_user_id_missing(client):
    """★ 取不到 `user_id` 时宁可跳过 UV：写 `null` 会让 UV 永久 ≥ 1（Spec §4.3）。"""
    await _ask(qa_repo.now_ms(), user_id="")
    day = align_ts(qa_repo.now_ms(), "1d")
    assert await metric_repo.get_bucket("global", "uv", day) is None
    assert await metric_repo.get_bucket("global", "all", align_ts(qa_repo.now_ms(), "1m"))


async def test_inc_never_raises_to_the_caller(client, monkeypatch):
    """MET-5001：指标是旁路，写库炸了也只能记 ERROR，**不能**影响问答。"""
    async def boom(**_kwargs):
        raise RuntimeError("mongo down")

    monkeypatch.setattr(metric_repo, "inc_bucket", boom)
    await _ask(qa_repo.now_ms())          # 不抛异常即为通过


async def test_inc_sets_ttl_only_on_short_granularity(client):
    """AC-09-15 的写入侧：`1m` 桶带 `expire_at`，`1d` 桶不带。"""
    now = qa_repo.now_ms()
    await _ask(now)
    minute_doc = await metric_repo.get_bucket("global", "all", align_ts(now, "1m"))
    assert minute_doc["expire_at"] is not None
    day_doc = await metric_repo.get_bucket("global", "uv", align_ts(now, "1d"))
    assert "expire_at" not in day_doc


async def test_metric_indexes_include_partial_ttl(client):
    """AC-09-15：TTL 只覆盖 `1m`/`1h`（部分索引），否则日桶会被一起清掉。"""
    await metric_repo.ensure_indexes()
    indexes = await mongo.collection(metric_repo.BUCKETS).index_information()
    assert "ix_metric_query" in indexes and "ix_metric_drill" in indexes
    ttl = indexes["ttl_metric_short"]
    assert ttl.get("expireAfterSeconds") == 0
    assert ttl["partialFilterExpression"]["granularity"]["$in"] == ["1m", "1h"]


# ================================================================ 摘要
async def test_overview_contract_and_uv_union_across_days(client):
    """AC-09-02：区间 UV 是**各日桶并集**，不是相加。"""
    now = qa_repo.now_ms()
    yesterday = now - DAY
    await _ask(yesterday, user_id=U1)                 # 昨天：A
    await _ask(yesterday, user_id=U2)                 # 昨天：B
    await _ask(now, user_id=U1)                       # 今天：A（跨天重复的人）

    data = await metric_service.overview(days=7)
    cards = data["cards"]
    assert cards["pv"]["value"] == 3
    assert cards["uv"]["value"] == 2, "A 两天都提问 → 区间 UV 仍是 2（不是 3）"
    assert cards["uv"]["exact"] is True
    assert set(cards) == {"pv", "uv", "doc_total", "faq_hit_rate", "avg_elapsed_s"}
    assert set(data["totals"]) >= {"faq_hit_cnt", "rag_cnt", "no_knowledge_cnt",
                                   "token_prompt", "token_completion",
                                   "denied_chunk_cnt"}
    assert data["range"]["days"] == 7
    assert data["granularity"] == "1h", "近 7 天按策略表选 1h"
    # 本用例只写了 `1m` 桶（没有跑汇总），所以 1h 桶缺失 → 回退现算
    # 并如实标注 degraded（§7.2 第 3 项：慢但正确，绝不静默少算）
    assert data["degraded"] is True and data["granularity"] == "1h"


async def test_overview_rate_token_and_avg_are_zero_safe(client):
    """R-6 / R-7 / AC-09-17：空库也不能除零。"""
    data = await metric_service.overview(days=1)
    assert data["cards"]["faq_hit_rate"]["value"] == 0.0
    assert data["cards"]["avg_elapsed_s"]["value"] == 0.0
    assert data["cards"]["pv"]["value"] == 0 and data["cards"]["uv"]["value"] == 0


async def test_overview_computes_rate_and_avg_elapsed(client):
    now = qa_repo.now_ms()
    await _ask(now, faq_hit=True, faq_id="FAQ000009", elapsed=2000,
               prompt=120, completion=80)
    await _ask(now, faq_hit=False, elapsed=1000, prompt=100, completion=20)
    cards = (await metric_service.overview(days=1))["cards"]
    assert cards["faq_hit_rate"]["value"] == 50.0, "1/2 命中 = 50.0%"
    assert cards["avg_elapsed_s"]["value"] == 1.5, "平均 1500ms = 1.5 秒"
    totals = (await metric_service.overview(days=1))["totals"]
    assert totals["token_prompt"] == 220 and totals["token_completion"] == 100


async def test_overview_doc_total_is_realtime_not_from_buckets(client, monkeypatch):
    """AC-09-22：删/加文档后卡片**立即**变化（R-8：不查指标桶）。"""
    # 关掉缓存，否则"相隔 1 毫秒的两次调用"会命中同一个缓存条目
    monkeypatch.setattr(metric_service, "_cache_ttl", lambda: 0)
    before = (await metric_service.overview(days=1))["cards"]["doc_total"]["value"]
    await doc_service.create(file_name="a.pdf", file_ext="pdf", file_size=10,
                             file_hash=_doc_hash("a"), storage={}, created_by=U1)
    card = (await metric_service.overview(days=1))["cards"]["doc_total"]
    assert card["value"] == before + 1, "doc_total 必须实时计数"
    assert "已启用" in card["sub"]


async def test_overview_degrades_to_zero_cards_when_bucket_read_fails(
        client, monkeypatch):
    """AC-09-18 / MET-4001：读失败 → 200 + 零值卡片 + `degraded:true`（不是 500）。"""
    async def boom(**_kwargs):
        raise RuntimeError("mongo down")

    monkeypatch.setattr(metric_repo, "aggregate_metrics", boom)
    data = await metric_service.overview(days=7)
    assert data["degraded"] is True
    assert data["cards"]["pv"]["value"] == 0
    assert data["cards"]["uv"]["value"] == 0
    assert data["cards"]["faq_hit_rate"]["value"] == 0.0
    assert data["totals"]["denied_chunk_cnt"] == 0


async def test_overview_doc_total_null_does_not_block_other_cards(
        client, monkeypatch):
    """MET-4004 / §7.2 第 6 项：**只有**这一张卡为 null，其余照常。"""
    now = qa_repo.now_ms()
    await _ask(now)

    async def boom():
        raise RuntimeError("kb_documents down")

    monkeypatch.setattr(doc_service, "count_by_status", boom)
    metric_service.reset()
    data = await metric_service.overview(days=1)
    assert data["cards"]["doc_total"]["value"] is None
    assert data["cards"]["pv"]["value"] == 1, "其余卡片不受影响"
    assert data["degraded"] is True


async def test_overview_cache_hits_once_within_ttl(client, monkeypatch):
    """AC-09-20：TTL 内重复请求只打一次库；TTL 归零后再查。"""
    calls = {"n": 0}
    original = metric_repo.aggregate_metrics

    async def counting(**kwargs):
        calls["n"] += 1
        return await original(**kwargs)

    monkeypatch.setattr(metric_repo, "aggregate_metrics", counting)
    first = await metric_service.overview(days=7)
    after_first = calls["n"]
    assert after_first >= 1
    second = await metric_service.overview(days=7)
    assert calls["n"] == after_first, "第二次必须命中缓存，一次库都不打"
    assert first == second
    monkeypatch.setattr(metric_service, "_cache_ttl", lambda: 0)
    await metric_service.overview(days=7)
    assert calls["n"] > after_first, "TTL=0 时必须重新查库"


# ================================================================ 趋势
async def test_trend_series_contract_and_equal_lengths(client):
    """AC-09-23：`x_axis` 与每条 `series[].data` 等长，且 token 拆两条（R-4）。"""
    now = qa_repo.now_ms()
    for index in range(3):
        await _ask(now - index * 1000, prompt=10, completion=5)
    data = await metric_service.trend(days=1, metrics=["pv", "uv", "token", "denied"])
    keys = [s["key"] for s in data["series"]]
    assert keys == ["pv", "uv", "token_prompt", "token_completion",
                    "denied_chunk_cnt"]
    for series in data["series"]:
        assert len(series["data"]) == len(data["x_axis"])
    pv_series = next(s for s in data["series"] if s["key"] == "pv")
    assert sum(pv_series["data"]) == 3
    assert pv_series["axis"] == "left"
    assert "area-stack" in [s["type"] for s in data["series"]]
    assert data["notes"]["token_scope"], "Token 口径必须显式回显（G-07）"
    assert data["granularity"] == "1m" and data["downsampled"] is False


async def test_trend_auto_granularity_by_span(client):
    now = qa_repo.now_ms()
    await _ask(now)
    one_day = await metric_service.trend(days=1, metrics=["pv"])
    seven = await metric_service.trend(days=7, metrics=["pv"])
    thirty = await metric_service.trend(days=30, metrics=["pv"])
    assert one_day["granularity"] == "1m"
    assert seven["granularity"] == "1h" and seven["downsampled"] is True
    assert thirty["granularity"] == "1d" and thirty["downsampled"] is True
    assert len(thirty["x_axis"]) == 30


async def test_trend_falls_back_to_minute_buckets_with_degraded(client):
    """§7.2 第 3 项：`1h` 桶还没汇总 → 回退读 `1m` 现算，并**如实标注**降级。"""
    now = qa_repo.now_ms()
    await _ask(now, prompt=7, completion=3)
    metric_service.reset()
    data = await metric_service.trend(days=7, metrics=["pv", "token"])
    assert data["granularity"] == "1h", "回显的仍是请求粒度（契约不变）"
    assert data["source_granularity"] == "1m", "实际读的是更细的桶"
    assert data["degraded"] is True
    assert sum(next(s for s in data["series"] if s["key"] == "pv")["data"]) == 1
    assert sum(next(s for s in data["series"]
                   if s["key"] == "token_prompt")["data"]) == 7


async def test_trend_explicit_granularity_without_buckets_reports_3002(client):
    """点名要 `1h` 却没有桶 → `MET-3002`（409），**绝不静默返回零值**。"""
    now = qa_repo.now_ms()
    await _ask(now)
    metric_service.reset()
    with pytest.raises(BizError) as err:
        await metric_service.trend(days=7, metrics=["pv"], granularity="1h")
    assert err.value.spec.code == "MET-3002"


async def test_trend_thirty_days_reads_only_daily_buckets(client, monkeypatch):
    """AC-09-12（性能核心）：30 天趋势一个 `1m` 桶都不碰。"""
    now = qa_repo.now_ms()
    seen: list[str] = []
    original = metric_repo.list_buckets

    async def spy(**kwargs):
        seen.append(kwargs["granularity"])
        return await original(**kwargs)

    monkeypatch.setattr(metric_repo, "list_buckets", spy)
    for day in range(30):
        slot = align_ts(now - day * DAY, "1d")
        await metric_repo.replace_bucket(
            bucket_type=MetricBucketType.GLOBAL.value, bucket_key="all", ts=slot,
            granularity="1d", metrics={"pv": 5, "token_prompt": 50,
                                       "token_completion": 20}, uv_set=[],
            ts_ms=now)
    metric_service.reset()
    data = await metric_service.trend(days=30, metrics=["pv", "uv"])
    assert data["granularity"] == "1d" and data["degraded"] is False
    assert seen and set(seen) == {"1d"}, f"只允许读 1d 桶，实际读了 {set(seen)}"
    pv_series = next(s for s in data["series"] if s["key"] == "pv")
    assert sum(pv_series["data"]) == 150


async def test_trend_uv_series_uses_daily_sets_not_hourly_sum(client):
    """R-6 / AD-12：UV 序列只能来自 `1d` 桶，且区间 UV 是并集大小。"""
    now = qa_repo.now_ms()
    for hours in (0, 1, 2):
        await _ask(now - hours * HOUR, user_id=U1)
    await _ask(now - HOUR, user_id=U2)
    metric_service.reset()
    data = await metric_service.trend(days=1, metrics=["uv"])
    uv_series = next(s for s in data["series"] if s["key"] == "uv")
    assert data["range_uv"] == 2, "区间 UV = 并集大小（2 个人）"
    assert max(uv_series["data"]) <= 2, "小时槽位取所属日桶的去重集合大小"
    assert uv_series["exact"] is True


async def test_trend_empty_range_returns_zero_filled_series(client):
    """R-8 / AC-09-17：无桶区间返回补零序列，不报错（长度 = 区间内槽位数）。"""
    now = qa_repo.now_ms()
    expected = (now // 1000 - align_ts(now, "1d")) // 60 + 1
    data = await metric_service.trend(days=1, metrics=["pv", "uv"])
    pv_series = next(s for s in data["series"] if s["key"] == "pv")
    assert len(pv_series["data"]) == len(data["x_axis"]) == expected
    assert set(pv_series["data"]) == {0}
    assert data["granularity"] == "1m" and data["degraded"] is False


# ================================================================ 延时
async def test_latency_bucket_mode_histogram_and_percentiles(client):
    """AC-09-08：7 个区间全都返回，`p50_ms ≤ p95_ms`，且 `precision=approximate`。"""
    now = qa_repo.now_ms()
    for elapsed in (100, 300, 900, 1500, 2500, 5000, 9000, 13000):
        await _ask(now, elapsed=elapsed)
    data = await metric_service.latency(days=1)
    assert data["mode"] == "bucket" and data["precision"] == "approximate"
    assert data["sample_size"] == 8
    assert len(data["bins"]) == 7
    assert sum(b["count"] for b in data["bins"]) == 8
    assert data["bins"][-1]["range"][1] is None, "12000+ 的上界是 null"
    assert data["percentiles"]["p50_ms"] <= data["percentiles"]["p95_ms"]
    assert data["percentiles"]["p50_s"] == round(data["percentiles"]["p50_ms"] / 1000, 2)
    assert data["avg_ms"] > 0


async def test_latency_exact_mode_reads_qa_logs(client):
    """R-2：`mode=exact` 查 `qa_logs.elapsed_ms` 取精确分位（`precision=exact`）。"""
    now = qa_repo.now_ms()
    for index, elapsed in enumerate((100, 200, 300, 400, 2000, 3000, 4000, 5000,
                                     6000, 7000)):
        await _log(f"问题{index}", asked_at=now - index * 1000, elapsed_ms=elapsed)
    data = await metric_service.latency(days=1, mode="exact")
    assert data["precision"] == "exact" and data["mode"] == "exact"
    assert data["sample_size"] == 10
    assert 4000 <= data["percentiles"]["p95_ms"] <= 7000
    assert data["percentiles"]["p50_ms"] == 2000


async def test_latency_rejects_other_percentiles(client):
    """G-08 / MET-1006：只提供 P50 / P95，别的分位直接拒绝。"""
    with pytest.raises(BizError) as err:
        await metric_service.latency(days=1, percentiles=["P99"])
    assert err.value.spec.code == "MET-1006"


async def test_latency_exact_range_guard(client):
    """MET-1007：`mode=exact` 跨度 > 31 天拒绝（保护 `qa_logs`）。"""
    with pytest.raises(BizError) as err:
        await metric_service.latency(days=60, mode="exact")
    assert err.value.spec.code == "MET-1007"


async def test_latency_exact_degrades_when_logs_unavailable(client, monkeypatch):
    """MET-4005 / R-4：精确分位不可用 → 自动降级为 `bucket` + `degraded:true`。"""
    async def boom(_start, _end):
        raise RuntimeError("asked_at index missing")

    monkeypatch.setattr(ms, "_exact_samples", boom)
    now = qa_repo.now_ms()
    await _ask(now, elapsed=1500)
    metric_service.reset()
    data = await metric_service.latency(days=1, mode="exact")
    assert data["mode"] == "bucket" and data["precision"] == "approximate"
    assert data["degraded"] is True
    assert data["sample_size"] == 1


async def test_latency_stage_grouping_marks_partial(client):
    """R-6：分段耗时缺失 → 该段 `null` + `partial:true`，**不得用 0 冒充**。"""
    now = qa_repo.now_ms()
    log_id = await _log("缺分段的问题", asked_at=now, elapsed_ms=900)
    await mongo.collection(qa_repo.QA_LOGS).update_one(
        {"_id": log_id}, {"$unset": {"llm_ms": ""}})
    data = await metric_service.latency(days=1, group_by="stage")
    assert data["by_stage"]["llm_ms"] is None
    assert data["by_stage"]["partial"] is True
    assert data["by_stage"]["retrieval_ms"] == 120
    assert data["degraded"] is True


async def test_latency_group_by_source_counts(client):
    now = qa_repo.now_ms()
    await _log("a", asked_at=now)
    await _log("b", asked_at=now, source="no_knowledge")
    data = await metric_service.latency(days=1, group_by="source")
    assert data["by_source"] == {"rag": 1, "no_knowledge": 1}


# ================================================================ 榜单
async def test_ranking_questions_are_normalized_and_ranked(client):
    """AC-09-10：带标点/不带标点的同一问法必须合并，`rank` 从 1 连续。"""
    now = qa_repo.now_ms()
    for index, text in enumerate(["差旅报销上限是多少？", "差旅报销上限是多少",
                                  "差旅报销上限是多少?", "年假没休完能不能折现"]):
        await _log(text, asked_at=now + index)
    data = await metric_service.ranking(kind="question", days=7, limit=10)
    items = data["top_questions"]
    assert [i["rank"] for i in items] == list(range(1, len(items) + 1))
    assert items[0]["count"] == 3, "三种写法应合并成一条"
    assert "差旅报销上限" in items[0]["question"]
    assert items[0]["source"] == "qa_logs"
    assert "user_id" not in items[0] and "session_id" not in items[0], \
        "G-12：榜单不得返回提问人明细"


async def test_ranking_docs_use_doc_buckets_and_one_in_query(client, monkeypatch):
    """AC-09-11：热门知识榜取标题只允许**一次 `$in`**（ER-13）。"""
    now = qa_repo.now_ms()
    doc_a = await doc_service.create(file_name="a.pdf", file_ext="pdf", file_size=1,
                                     file_hash=_doc_hash("a"), storage={},
                                     created_by=U1)
    doc_b = await doc_service.create(file_name="b.pdf", file_ext="pdf", file_size=1,
                                     file_hash=_doc_hash("b"), storage={},
                                     created_by=U1)
    for _ in range(3):
        await _ask(now, doc_ids=(doc_a,))
    await _ask(now, doc_ids=(doc_b,))
    calls = {"n": 0}
    original = doc_repo.find_by_ids

    async def counting(ids):
        calls["n"] += 1
        return await original(ids)

    monkeypatch.setattr(doc_repo, "find_by_ids", counting)
    metric_service.reset()
    data = await metric_service.ranking(kind="doc", days=7, limit=10)
    assert calls["n"] == 1, f"取标题必须只查一次，实际 {calls['n']} 次"
    docs = data["top_docs"]
    assert docs[0]["doc_id"] == doc_a and docs[0]["cite_count"] == 3
    # 标题来自 `kb_documents`（不是从文件名拼出来的），所以拿库里那条比
    created = await doc_service.get(doc_a)
    assert docs[0]["title"] == created["title"] and docs[0]["rank"] == 1
    assert len(docs) == 2


async def test_ranking_doc_title_failure_keeps_the_board(client, monkeypatch):
    """MET-4003：取标题失败 → 榜单照常返回，标题占位 + `degraded:true`。"""
    now = qa_repo.now_ms()
    await _ask(now, doc_ids=("DOC0001",))

    async def boom(_ids):
        raise RuntimeError("kb_documents down")

    monkeypatch.setattr(doc_repo, "find_by_ids", boom)
    metric_service.reset()
    data = await metric_service.ranking(kind="doc", days=7)
    assert data["degraded"] is True
    assert data["top_docs"][0]["title"] == "（标题不可用）"
    assert data["top_docs"][0]["cite_count"] == 1


async def test_ranking_question_fallback_reads_faq_candidates(client, monkeypatch):
    """MET-4002 / R-4：`qa_logs` 聚合失败 → 降级读 FAQ 频次并标 `source:"faq"`。"""
    from app.repositories import faq_repo

    await faq_repo.upsert_candidate({
        "_id": "CAND0001", "cluster_key": "k1", "window_start": 0,
        "representative_question": "报销上限",
        "questions": ["报销上限"], "frequency": 9, "status": "pending",
        "created_at": 1, "updated_at": 1, "first_seen_at": 1, "last_seen_at": 1,
        "dept_ids": [], "suggested_category_id": None, "sample_answer": "",
        "hit_doc_ids": []})

    async def boom(_self, _start, _end, _limit):
        raise RuntimeError("qa_logs down")

    monkeypatch.setattr(MetricService, "_question_ranking", boom)
    metric_service.reset()
    data = await metric_service.ranking(kind="question", days=7)
    assert data["degraded"] is True
    assert data["top_questions"][0]["source"] == "faq"
    assert data["top_questions"][0]["count"] == 9


async def test_ranking_fills_faq_id_by_normalized_key(client):
    """出参里的 `faq_id` 用归一化问法回填（榜单原文与 FAQ 字面值不保证一致）。"""
    from app.repositories import faq_repo

    now = qa_repo.now_ms()
    await _log("差旅报销上限是多少？", asked_at=now)
    await faq_repo.insert_faq({
        "_id": "FAQ000017", "question": "差旅报销上限是多少",
        "question_norm": faq_repo.normalize_question("差旅报销上限是多少"),
        "answer": "按职级", "category_id": None, "aliases": [], "enabled": True,
        "hit_count": 0, "created_by": U1, "created_at": 1, "updated_at": 1,
        "published_at": 1, "embedding": None})
    metric_service.reset()
    data = await metric_service.ranking(kind="question", days=7)
    assert data["top_questions"][0]["faq_id"] == "FAQ000017"


async def test_ranking_limit_and_type_guards(client):
    """MET-1008：`limit` 必须落在 [1, 50]（原型固定 TOP10）。"""
    with pytest.raises(BizError) as err:
        await metric_service.ranking(days=7, limit=51)
    assert err.value.spec.code == "MET-1008"
    with pytest.raises(BizError) as err2:
        await metric_service.ranking(days=7, kind="dept")
    assert err2.value.spec.code == "SYS-1001"


async def test_ranking_both_returns_two_boards(client):
    now = qa_repo.now_ms()
    await _log("只有一个问题", asked_at=now)
    await _ask(now, doc_ids=("DOC0001",))
    data = await metric_service.ranking(kind="both", days=7)
    assert "top_questions" in data and "top_docs" in data


# ================================================================ 汇总幂等
async def test_rollup_is_idempotent_and_sums_minute_buckets(client):
    """AC-09-16：连跑 3 次 `1m→1h`，`1h` 桶数字**不变**（`$set` 覆盖而非 `$inc`）。"""
    now = qa_repo.now_ms() - 30 * MINUTE          # 落在上一个完整小时
    hour = align_ts(now, "1h")
    for _ in range(4):
        await _ask(now, prompt=10, completion=5, elapsed=700)
    for _ in range(3):
        await metric_service.rollup(ts_ms=now)
    bucket = await metric_repo.get_bucket("global", "all", hour)
    assert bucket is not None, "汇总必须产出小时桶"
    assert bucket["metrics"]["pv"] == 4, "跑 3 次数字不变（$inc 会变成 12）"
    assert bucket["metrics"]["token_prompt"] == 40
    assert bucket["granularity"] == "1h"
    assert bucket["expire_at"] is not None, "1h 桶跟着 30 天 TTL"


async def test_rollup_builds_daily_buckets_from_hourly(client):
    """`1h→1d`：日桶由小时桶求和（UV 不参与——它在独立 `global:uv:` 桶里）。"""
    now = qa_repo.now_ms() - 2 * HOUR
    await _ask(now)
    await metric_service.rollup(ts_ms=now)
    metric_service.reset()
    day = align_ts(now, "1d")
    bucket = await metric_repo.get_bucket("global", "all", day)
    assert bucket is not None and bucket["metrics"]["pv"] >= 1
    assert bucket["granularity"] == "1d"
    assert "expire_at" not in bucket


async def test_rollup_reports_skipped_when_already_running(client, monkeypatch):
    """单飞锁：正在汇总时再来一次直接返回 `skipped`，不排队堆积。"""
    service = MetricService()
    await service._rollup_lock.acquire()
    try:
        assert await service.rollup() == {"skipped": 1}
    finally:
        service._rollup_lock.release()


# ================================================================ 导出
async def test_export_csv_has_bom_and_rows(client):
    """AC-09-21：CSV 必须带 UTF-8 BOM，否则 Excel 打开中文乱码。"""
    now = qa_repo.now_ms()
    await _ask(now - 1000, faq_hit=True, faq_id="FAQ000002",
               doc_ids=("DOC0001",))
    await _log("报销上限是多少", asked_at=now - 2000)
    for metric in ("overview", "trend", "latency", "ranking"):
        filename, content = await metric_service.export_csv(metric=metric, days=7)
        assert content.startswith("\ufeff"), metric
        assert filename.startswith(f"metrics_{metric}_") and filename.endswith(".csv")
        assert len(content.splitlines()) >= 2, f"{metric} 导出只有表头"


async def test_export_rejects_unknown_metric_and_format(client):
    with pytest.raises(BizError) as err:
        await metric_service.export_csv(metric="unknown", days=7)
    assert err.value.spec.code == "SYS-1001"


# ================================================================ 接口层
async def test_metrics_endpoints_are_read_only_and_enveloped(client):
    token = await token_of(client, SYS_ADMIN)
    resp = await client.get("/api/v1/metrics/overview?days=7", headers=auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0 and body["message"] == "ok" and body["trace_id"]
    assert set(body["data"]["cards"]) == {"pv", "uv", "doc_total", "faq_hit_rate",
                                          "avg_elapsed_s"}
    assert resp.headers["x-trace-id"]


async def test_all_metrics_endpoints_require_jwt(client):
    """ER-08：看板接口一个都不能漏掉认证。"""
    for path in ("/api/v1/metrics/overview", "/api/v1/metrics/trend",
                 "/api/v1/metrics/latency", "/api/v1/metrics/ranking",
                 "/api/v1/metrics/export"):
        resp = await client.get(path)
        assert resp.status_code == 401, path
        assert resp.json()["code"].startswith("AUTH-")


async def test_kb_admin_is_explicitly_denied_with_met_2003(client):
    """AC-09-14（原型 `08` 矩阵的「—」）：`kb_admin` 拿到 403 `MET-2003`。"""
    token = await token_of(client, KB_ADMIN)
    for path in ("/api/v1/metrics/overview", "/api/v1/metrics/trend",
                 "/api/v1/metrics/latency", "/api/v1/metrics/ranking",
                 "/api/v1/metrics/export?metric=overview"):
        resp = await client.get(path, headers=auth(token))
        assert resp.status_code == 403, path
        assert resp.json()["code"] == "MET-2003", path
        assert resp.json()["data"] is None, "必须拒绝，不能返回空数据糊过去"


async def test_asker_gets_met_2002(client):
    token = await token_of(client, ASKER)
    resp = await client.get("/api/v1/metrics/overview", headers=auth(token))
    assert resp.status_code == 403
    assert resp.json()["code"] == "MET-2002"


async def test_export_requires_metric_export(client):
    """MET-2005：有 `metric:read` 但没有 `metric:export` 时，导出单独被拒。

    构造方式：给 `kb_admin` 角色**只补** `metric:read`（ER-09 要求权限码已入库，
    这里用的就是 34 码字典里那条），再以 `zhangwei` 的身份分别调读取与导出。
    读取应当放行、导出应当 403——这正是"两个码不是一个码"的证据。
    """
    permissions = await auth_repo.list_permissions()
    read_id = next(p["_id"] for p in permissions if p["code"] == "metric:read")
    await mongo.collection(auth_repo.ROLE_PERMISSIONS).insert_one({
        "_id": f"ROLE0002:{read_id}", "role_id": "ROLE0002",
        "permission_id": read_id, "granted_by": "test", "granted_at": 0})

    token = await token_of(client, KB_ADMIN)
    ok_resp = await client.get("/api/v1/metrics/overview", headers=auth(token))
    assert ok_resp.status_code == 200, "有 metric:read 就该能看"
    deny = await client.get("/api/v1/metrics/export?metric=overview",
                            headers=auth(token))
    assert deny.status_code == 403
    assert deny.json()["code"] == "MET-2005"


async def test_export_returns_csv_with_bom_and_disposition(client):
    token = await token_of(client, SYS_ADMIN)
    resp = await client.get("/api/v1/metrics/export?metric=trend&days=7",
                            headers=auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.content.startswith(b"\xef\xbb\xbf"), "响应体必须以 BOM 开头"


async def test_export_rejects_non_csv_format(client):
    """MET-1009。"""
    token = await token_of(client, SYS_ADMIN)
    resp = await client.get("/api/v1/metrics/export?format=xlsx",
                            headers=auth(token))
    assert resp.status_code == 400
    assert resp.json()["code"] == "MET-1009"


async def test_check_rate_raises_after_the_configured_limit(client, monkeypatch):
    """R-11 的单元口径：限流上限来自配置 `metric.rate_limit_per_min`。"""
    from app.services.config_service import config_service

    monkeypatch.setattr(config_service, "get_raw",
                        lambda key: 2 if key == "metric.rate_limit_per_min" else 30)
    service = MetricService()
    service.check_rate("U000001")
    service.check_rate("U000001")
    with pytest.raises(BizError) as err:
        service.check_rate("U000001")
    assert err.value.spec.code == "MET-2004"
    service.check_rate("U000099"), "限流按用户隔离，别人不受影响"


async def test_rate_limit_returns_met_2004(client, monkeypatch):
    """R-11 的接口口径：超限的 HTTP 状态是 429，错误码是 `MET-2004`。"""
    from app.services.config_service import config_service

    monkeypatch.setattr(config_service, "get_raw",
                        lambda key: 2 if key == "metric.rate_limit_per_min" else 30)
    metric_service.reset()
    token = await token_of(client, SYS_ADMIN)
    codes = []
    for _ in range(3):
        resp = await client.get("/api/v1/metrics/overview", headers=auth(token))
        codes.append(resp.status_code)
    assert codes[:2] == [200, 200] and codes[2] == 429


async def test_trend_api_echoes_granularity_and_labels(client):
    """R-3 / R-7：粒度与 `downsampled` 必须回显，标签按 Asia/Shanghai 格式化。"""
    token = await token_of(client, SYS_ADMIN)
    resp = await client.get("/api/v1/metrics/trend?days=30&metrics=pv,uv,token",
                            headers=auth(token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["granularity"] == "1d" and data["downsampled"] is True
    assert len(data["x_axis"]) == 30
    assert all(len(s["data"]) == 30 for s in data["series"])
    assert all(len(label) == 10 for label in data["x_axis"]), "1d 标签是 YYYY-MM-DD"


async def test_latency_api_rejects_custom_percentile(client):
    token = await token_of(client, SYS_ADMIN)
    resp = await client.get("/api/v1/metrics/latency?percentiles=P99",
                            headers=auth(token))
    assert resp.status_code == 400
    assert resp.json()["code"] == "MET-1006"


async def test_ranking_api_default_is_both_boards(client):
    token = await token_of(client, SYS_ADMIN)
    resp = await client.get("/api/v1/metrics/ranking", headers=auth(token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["top_questions"] == [] and data["top_docs"] == []
    assert data["degraded"] is False


# ================================================================ 内部小工具
def _ms(text: str) -> int:
    """`"2026-09-23 14:07:33"`（秒可省）→ 毫秒时间戳，按 Asia/Shanghai 解释。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    moment = None
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            moment = datetime.strptime(text, pattern)
            break
        except ValueError:
            continue
    assert moment is not None, f"时间串无法解析：{text}"
    return int(moment.replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)


async def _global_minute_buckets(day: int) -> list[dict]:
    return await metric_repo.list_buckets(
        bucket_type=MetricBucketType.GLOBAL.value, granularity="1m",
        start_ts=day, end_ts=day + 86400, limit=5000)
