# -*- coding: utf-8 -*-
"""模块 09 的服务层：**运营看板**（写入时增量聚合 + 读取时降采样）。

## AD-07：看板不能扫 `qa_logs`

这是本模块最重要的一条设计约束。若每次打开看板都去 `qa_logs` 做聚合：

| 后果 | 说明 |
|---|---|
| 慢 | 30 天的日志量在演示环境是**万级**，`$group` 要扫全量 |
| 抢占 | 它和问答链路抢的是同一个 Mongo；看板一刷新，问答就变慢 |
| 不可控 | 数据量增长后 P95 直接失控（AC-09-13 要求 < 500ms） |

所以改成**写入时增量聚合**：06 每完成一轮问答就投递一次
`metric_service.inc(...)`，桶里的数字已经是"累加好的"。
看板查询只读桶（命中 `ix_metric_query`），**AC-09-04 用执行计划验证零集合扫描**。

例外只有两处，都在 Spec 里写明：`ranking` 的高频问题榜（要"问题原文"，
桶里只有计数）与 `latency?mode=exact`（要精确分位）。两者都必须命中索引。

## 两个必须分开的指标：PV 与 UV（AD-12）

- **PV**：每次提问 +1，可加 → 放 `metrics.pv`，`$inc`
- **UV**：去重人数，**不可加** → 放 `uv_set`（`$addToSet`），查询时算集合大小

把 UV 当累加量是本模块最容易犯的错：同一用户 10 分钟问 3 次会被算成 3 个人，
而"日活 3"与"日活 1"在看板上是完全不同的两个结论。
区间 UV 也不能把各日桶相加——必须**跨天再求一次并集**（`_range_uv`）。

## 数据降采样（AC-09-12）

| 查询跨度 | 读哪个粒度 | 条数上限 |
|---|---|---|
| ≤ 1 天 | `1m` | 1440 |
| 1 天 ~ 7 天 | `1h` | 168 |
| > 7 天 | `1d` | ≤ 366 |

**永远不读 `1m` 去画 30 天**：那要拉 43200 个桶，比不降采样还慢。
`downsampled` 字段把"这次用了更粗的粒度"如实回显给前端。

## 桶缺失时"慢但正确"优于"快但少算"（§7.2 第 3 项）

`1h` / `1d` 桶由汇总任务产生，任务没跑到时桶就是空的。此时的策略分三种：

| 场景 | 行为 |
|---|---|
| 自动选粒度 + 该粒度无桶 | **回退到更细的桶现算汇总**（读到 `1m` 也只在内存里按目标槽位聚合），回 `degraded:true` 与
实际的 `source_granularity` |
| 客户端**点名** `granularity=1h/1d` 且无桶 | `MET-3002`（409）——点名要粗粒度却静默返回零值就是"少算" |
| 需要 `1m` 桶但已过 30 天 TTL | `MET-3003`（410）——没有更粗的桶可回退，唯一诚实的回答是"数据过期" |

## 缓存（§4.4）

`overview/trend/latency/ranking` 的结果进进程内 LRU（默认 30 秒 TTL、200 个 key）。
**不做主动失效**：高频问答期间"刚填上就被清"会让缓存变成纯开销，
而时序指标 30 秒的偏差对看板无业务意义。导出与详情不进缓存。
"""
from __future__ import annotations

import asyncio
import copy
import csv
import io
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from app.core.enums import MetricBucketType, MetricGranularity
from app.core.errors import BizError, Err
from app.core.logging import logger
from app.repositories import metric_repo
from app.services.config_service import config_service

# 统一按 Asia/Shanghai 切天（Spec §2.3 / AC-09-19）
TZ = ZoneInfo("Asia/Shanghai")

# 粒度常量（少写三处字符串字面量，改名字时只改这里）
M1 = MetricGranularity.M1.value
H1 = MetricGranularity.H1.value
D1 = MetricGranularity.D1.value

# 降采样阈值（天）：Spec §4.2 策略表
MINUTE_MAX_DAYS = 1
HOURLY_MAX_DAYS = 7
# `1m`/`1h` 桶的保留期；超期后只能靠 `1d` 桶回答（否则 MET-3003）
SHORT_TTL_DAYS = metric_repo.SHORT_GRANULARITY_TTL_DAYS
# 区间绝对上限（MET-1004）与未来容忍窗口（MET-1002）
MAX_RANGE_DAYS = 366
FUTURE_TOLERANCE_MS = 5 * 60_000
# `mode=exact` 的跨度上限与样本上限（MET-1007 / MET-3004）
EXACT_MAX_DAYS = 31
EXACT_SAMPLE_LIMIT = 500_000
# 导出上限（MET-3005）与缓存上限（Spec §4.4）
EXPORT_ROW_LIMIT = 100_000
CACHE_MAX_KEYS = 200
# 单次读取的桶数上限：`1m` 满 1 天 = 1440，留足余量
BUCKET_READ_LIMIT = 20_000

# 延时区间（Spec §2.6 的 7 个固定区间 + 代表值）
LATENCY_BUCKETS: tuple[tuple[str, int, int, int], ...] = (
    ("0-500", 0, 500, 250),
    ("500-1000", 500, 1000, 750),
    ("1000-2000", 1000, 2000, 1500),
    ("2000-4000", 2000, 4000, 3000),
    ("4000-8000", 4000, 8000, 6000),
    ("8000-12000", 8000, 12000, 10000),
    ("12000+", 12000, 10 ** 9, 15000),
)
# `12000+` 的上界在响应里必须是 `null`（Spec §3.3 出参）
LATENCY_TAIL = 10 ** 9
# Token 口径必须**显式回显**（G-07）：前端文案与后端数字同源
TOKEN_SCOPE_NOTE = "仅大模型 prompt + completion，不含 embedding（G-07）"

# `series` 元素的展示契约（Spec §3.2 表格）：前端 ECharts 直接拿来用
SERIES_META: dict[str, dict[str, Any]] = {
    "pv": {"name": "访问量 PV", "axis": "left", "type": "line", "unit": "次"},
    "uv": {"name": "独立提问人数 UV", "axis": "right", "type": "line",
           "unit": "人", "exact": True},
    "token_prompt": {"name": "prompt Token", "type": "area-stack"},
    "token_completion": {"name": "completion Token", "type": "area-stack"},
    "denied_chunk_cnt": {"name": "权限拦截切片数", "type": "line", "unit": "个"},
    "rag_cnt": {"name": "RAG 次数", "type": "line", "unit": "次"},
    "no_knowledge_cnt": {"name": "无知识次数", "type": "line", "unit": "次"},
    "faq_hit_cnt": {"name": "FAQ 命中数", "type": "line", "unit": "次"},
}
# 入参 `metrics` → 输出 `series[].key` 的展开表（R-4：token 必须拆两条）
INPUT_METRICS: dict[str, tuple[str, ...]] = {
    "pv": ("pv",),
    "uv": ("uv",),
    "token": ("token_prompt", "token_completion"),
    "token_prompt": ("token_prompt",),
    "token_completion": ("token_completion",),
    "denied": ("denied_chunk_cnt",),
    "denied_chunk_cnt": ("denied_chunk_cnt",),
    "rag": ("rag_cnt",),
    "rag_cnt": ("rag_cnt",),
    "no_knowledge": ("no_knowledge_cnt",),
    "no_knowledge_cnt": ("no_knowledge_cnt",),
    "faq_hit_cnt": ("faq_hit_cnt",),
}
# 看板卡片（Spec §3.1 出参的 5 张卡）
CARD_LABELS = {
    "pv": "访问量 PV",
    "uv": "独立提问人数 UV",
    "doc_total": "知识单元总数",
    "faq_hit_rate": "FAQ 缓存命中率",
    "avg_elapsed_s": "平均问答延时",
}


def align_ts(now_ms: int, granularity: str) -> int:
    """把毫秒时间戳**向下对齐**到粒度起点（Spec §2.3 的工具函数，06/07/08 也复用）。

    ★ **必须按 Asia/Shanghai 自然日切天**：用 UTC 会让东八区用户的"今天"
    变成 UTC 的 16:00 到次日 16:00，看板柱子整体错位 8 小时，
    且跨零点的一次提问会被切成两天（AC-09-19 专测这一点）。
    """
    moment = datetime.fromtimestamp(now_ms / 1000, tz=TZ)
    if granularity == M1:
        moment = moment.replace(second=0, microsecond=0)
    elif granularity == H1:
        moment = moment.replace(minute=0, second=0, microsecond=0)
    elif granularity == D1:
        moment = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    else:                                                   # pragma: no cover
        raise ValueError(f"未知粒度：{granularity}")
    return int(moment.timestamp())


def latency_bucket_of(elapsed_ms: int) -> str:
    """耗时落在哪个延时区间（固定 7 个，Spec §2.6）。"""
    value = max(0, int(elapsed_ms))
    for label, low, high, _ in LATENCY_BUCKETS:
        if low <= value < high:
            return label
    return LATENCY_BUCKETS[-1][0]                                # pragma: no cover


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float
    payload: Any


class MetricService:
    """运营看板服务（进程级单例）。"""

    def __init__(self) -> None:
        # LRU：`OrderedDict` + 超过 200 个 key 时弹出最旧的（防长跑内存无界）
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        # 限流计数：`user_id` → (窗口起点秒, 次数)
        self._rate: dict[str, tuple[int, int]] = {}
        self._rollup_lock = asyncio.Lock()
        self._rolling = False

    @property
    def rolling(self) -> bool:
        return self._rolling

    # ================================================================== 写入
    async def inc(self, user_id: str, dept_id: str, *, ts_ms: int | None = None,
                  faq_hit: bool = False, answer_source: str = "rag",
                  token_prompt: int = 0, token_completion: int = 0,
                  elapsed_ms: int = 0, denied_chunk_cnt: int = 0,
                  faq_id: str | None = None,
                  doc_ids: Sequence[str] | None = None) -> None:
        """★ 全平台唯一的指标投递入口（Spec §3.6 / ER-07，签名与 Spec 逐字一致）。

        **异常一律不外抛**（`MET-5001` 的语义）：指标是旁路，
        为了一条统计数字让用户的问答失败是把可用性押在旁路上。

        `dept_id` 目前**只入参不落桶**：Spec §2.5 的 `bucket_type` 只有
        global / faq / latency / doc 四类，按部门下钻属 OQ-09-02 的后续范围；
        保留入参是为了让 06 的调用点不必在开下钻时改签名。
        """
        moment = ts_ms or metric_repo.now_ms()
        base = {
            "pv": 1,
            "question_cnt": 1,
            "faq_hit_cnt": 1 if faq_hit else 0,
            "rag_cnt": 1 if answer_source == "rag" else 0,
            "no_knowledge_cnt": 1 if answer_source == "no_knowledge" else 0,
            "token_prompt": max(0, int(token_prompt or 0)),
            "token_completion": max(0, int(token_completion or 0)),
            "elapsed_sum_ms": max(0, int(elapsed_ms or 0)),
            "denied_chunk_cnt": max(0, int(denied_chunk_cnt or 0)),
        }
        try:
            # ① 全局分钟桶（可加指标）
            await metric_repo.inc_bucket(
                bucket_type=MetricBucketType.GLOBAL.value, bucket_key="all",
                ts=align_ts(moment, M1), granularity=M1, increments=base,
                ts_ms=moment)
            # ② 全局 UV 日桶（**只存 1d**，`$addToSet` 去重）
            if user_id:
                await metric_repo.inc_bucket(
                    bucket_type=MetricBucketType.GLOBAL.value, bucket_key="uv",
                    ts=align_ts(moment, D1), granularity=D1, increments={},
                    add_uv=[user_id], ts_ms=moment)
            else:
                # 取不到 `user_id` 时**宁可不写**：写进去集合里会多一个"匿名用户"，
                # 把 UV 永久抬高到至少 1（Spec §4.3 的显式禁止项）
                logger.warning("指标投递缺少 user_id，已跳过 UV 写入（AVOID 匿名 UV 污染）")
            # ③ 延时区间桶（直方图数据源）
            await metric_repo.inc_bucket(
                bucket_type=MetricBucketType.LATENCY.value,
                bucket_key=latency_bucket_of(elapsed_ms), ts=align_ts(moment, M1),
                granularity=M1, increments={"pv": 1}, ts_ms=moment)
            # ④ FAQ 维度桶（只有命中时才写：未命中的 FAQ 不该出现在它的统计里）
            if faq_hit and faq_id:
                await metric_repo.inc_bucket(
                    bucket_type=MetricBucketType.FAQ.value, bucket_key=faq_id,
                    ts=align_ts(moment, D1), granularity=D1,
                    increments={"pv": 1, "faq_hit_cnt": 1}, ts_ms=moment)
            # ⑤ 文档维度桶（热门知识榜的数据源）
            for doc_id in dict.fromkeys(d for d in (doc_ids or []) if d):
                await metric_repo.inc_bucket(
                    bucket_type=MetricBucketType.DOC.value, bucket_key=doc_id,
                    ts=align_ts(moment, D1), granularity=D1,
                    increments={"pv": 1}, ts_ms=moment)
        except Exception as exc:                            # noqa: BLE001
            # MET-5001：只记 ERROR，**绝不抛给 06**（用户在等答案）
            logger.error("指标投递失败（code=%s，不影响问答）：%s",
                         Err.MET_WRITE_FAILED.code, exc)

    # ================================================================== 汇总
    async def rollup(self, *, ts_ms: int | None = None) -> dict[str, int]:
        """把 `1m` 汇总到 `1h`、`1h` 汇总到 `1d`（**幂等**：`$set` 覆盖，AC-09-16）。"""
        if self._rollup_lock.locked():
            return {"skipped": 1}
        async with self._rollup_lock:
            self._rolling = True
            moment = ts_ms or metric_repo.now_ms()
            try:
                hourly = await self._rollup_level(
                    source=M1, target=H1, span_seconds=3600, moment=moment,
                    windows=6)
                daily = await self._rollup_level(
                    source=H1, target=D1, span_seconds=86400, moment=moment,
                    windows=31, keep_uv=True)
            except Exception as exc:                        # noqa: BLE001
                logger.error("指标桶汇总失败（下一轮幂等重算会修复，code=%s）：%s",
                             Err.MET_ROLLUP_FAILED.code, exc)
                return {"error": 1}
            finally:
                self._rolling = False
            return {"hourly": hourly, "daily": daily}

    async def _rollup_level(self, *, source: str, target: str, span_seconds: int,
                            moment: int, windows: int,
                            keep_uv: bool = False) -> int:
        """一层汇总：读源粒度桶 → 按目标粒度分组求和 → **覆盖**目标桶。

        覆盖而不是累加，是"连跑 3 次数字不变"的实现基础。

        `windows` 决定回看多少个目标窗口：`1m→1h` 回看 6 小时（Spec §4.2
        「每次汇总任务启动时回看近 6 小时」），`1h→1d` 回看 31 天（日桶要覆盖
        看板的最长查询跨度；744 条小时桶的读取成本可以忽略）。
        **包含当前未走完的窗口**：否则"刚刚问的那一分钟"要等到下一个整点
        才会出现在小时桶里，演示时会看到趋势图最后一格永远是 0。
        重复覆盖当前窗口是安全的——`$set` 覆盖天然幂等。
        """
        end = align_ts(moment, target)
        start = end - span_seconds * windows
        rows = await metric_repo.list_buckets(
            bucket_type=MetricBucketType.GLOBAL.value, granularity=source,
            start_ts=start, end_ts=end + span_seconds - 1, limit=BUCKET_READ_LIMIT)
        grouped: dict[int, dict[str, Any]] = {}
        for row in rows:
            slot = align_ts(int(row["bucket_ts"]) * 1000, target)
            target_bucket = grouped.setdefault(slot, {"metrics": {}, "uv": set()})
            for key, value in (row.get("metrics") or {}).items():
                target_bucket["metrics"][key] = \
                    target_bucket["metrics"].get(key, 0) + int(value or 0)
            if keep_uv:
                # 当前 `1h` 桶不存 `uv_set`（OQ-09-10），这一步是**防御性**的：
                # 将来若开了小时级 UV，汇总会自动把集合并上去而不是丢掉
                target_bucket["uv"].update(row.get("uv_set") or [])
        for slot, payload in grouped.items():
            await metric_repo.replace_bucket(
                bucket_type=MetricBucketType.GLOBAL.value, bucket_key="all",
                ts=slot, granularity=target, metrics=payload["metrics"],
                uv_set=[], ts_ms=moment)
        return len(grouped)

    # ================================================================== 缓存
    def _cache_ttl(self) -> int:
        """缓存 TTL（秒）：配置项 `metric.cache_ttl_seconds`，默认 30（Spec §4.4）。"""
        try:
            return max(0, int(config_service.get_raw("metric.cache_ttl_seconds")))
        except Exception:                                   # noqa: BLE001
            return 30

    def _cache_get(self, key: str) -> Any:
        if self._cache_ttl() <= 0:
            # 配置成 0 秒 = "关掉缓存"：必须在**读**的时候也生效，
            # 否则先填进去的条目会继续被命中，症状是"改了配置却不生效"
            self._cache.clear()
            return None
        entry = self._cache.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)                        # LRU：命中即置新
        return copy.deepcopy(entry.payload)

    def _cache_put(self, key: str, payload: Any) -> None:
        ttl = self._cache_ttl()
        if ttl <= 0:
            return
        self._cache[key] = _CacheEntry(expires_at=time.monotonic() + ttl,
                                       payload=copy.deepcopy(payload))
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_MAX_KEYS:
            self._cache.popitem(last=False)

    # ================================================================== 摘要
    async def overview(self, *, days: int = 7, start_ts: int | None = None,
                       end_ts: int | None = None) -> dict[str, Any]:
        """核心指标摘要（Spec §3.1）：5 张卡片 + `totals`。

        **降级返回零值 + `degraded:true`**（`MET-4001` 的语义）：
        看板的"总览"是**第一屏**，它整体 500 会让页面全白；
        而"显示 0 并标注数据不可用"至少让人知道发生了什么。
        （`trend` 走另一条路：零值曲线会被误读成"业务停摆"，那里必须报错。）
        """
        moment = metric_repo.now_ms()
        start_s, end_s = _resolve_range(moment, days, start_ts, end_ts)
        span_days = _span_days(start_s, end_s)
        key = f"overview|{days}|{start_s}|{end_s}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        totals: dict[str, Any] = {}
        uv = 0
        chosen_gran = _pick_granularity(span_days)
        used_gran = chosen_gran
        degraded = False
        try:
            totals, used_gran, fell_back = await self._totals_with_fallback(
                start_s=start_s, end_s=end_s, span_days=span_days,
                allow_expired=True)
            uv = await _range_uv(start_s, end_s)
            degraded = degraded or fell_back
        except Exception as exc:                            # noqa: BLE001
            logger.error("指标桶读取失败，总览降级为零值（code=%s）：%s",
                         Err.MET_READ_FAILED.code, exc)
            totals, uv, degraded = {}, 0, True

        pv = int(totals.get("pv") or 0)
        hit = int(totals.get("faq_hit_cnt") or 0)
        elapsed_sum = int(totals.get("elapsed_sum_ms") or 0)
        doc_card, doc_degraded = await self._doc_total_card(degraded)
        degraded = degraded or doc_degraded
        cards = {
            "pv": {"value": pv, "unit": "次", "label": CARD_LABELS["pv"],
                   "source": "metric_buckets.pv（可加指标求和）"},
            "uv": {"value": int(uv), "unit": "人", "label": CARD_LABELS["uv"],
                   "source": "metric_buckets.uv_set（1d 桶去重集合取 len）",
                   "exact": True},
            "doc_total": doc_card,
            # 命中率用 `faq_hit_cnt / pv`（原型 k4H 的口径）；除零保护：pv=0 → 0.0
            "faq_hit_rate": {"value": round(hit / pv * 100, 1) if pv else 0.0,
                             "unit": "%", "label": CARD_LABELS["faq_hit_rate"],
                             "formula": "faq_hit_cnt / pv"},
            # 平均耗时 = 累加 / 次数 / 1000（Spec §3.1 出参为**秒**，两位小数）
            "avg_elapsed_s": {"value": round(elapsed_sum / pv / 1000, 2) if pv else 0.0,
                              "unit": "秒", "label": CARD_LABELS["avg_elapsed_s"],
                              "formula": "elapsed_sum_ms / pv / 1000",
                              "note": "含检索 + 生成"},
        }
        data = {
            "range": {"start_ts": start_s * 1000, "end_ts": end_s * 1000,
                      "days": days},
            "cards": cards,
            "totals": {
                "faq_hit_cnt": hit,
                "rag_cnt": int(totals.get("rag_cnt") or 0),
                "no_knowledge_cnt": int(totals.get("no_knowledge_cnt") or 0),
                "token_prompt": int(totals.get("token_prompt") or 0),
                "token_completion": int(totals.get("token_completion") or 0),
                "denied_chunk_cnt": int(totals.get("denied_chunk_cnt") or 0),
                # OQ-09-09 的加分项：缺口数是"当前值"而非时序累计，所以实时计数
                "open_gap_cnt": await _open_gap_count(),
            },
            "granularity": chosen_gran,
            "downsampled": chosen_gran != M1,
            # 实际读的粒度（粗桶缺失时可能更细）——与 `trend` 同一套口径
            "source_granularity": used_gran,
            "degraded": degraded,
            "generated_at": moment,
        }
        self._cache_put(key, data)
        return data

    async def _doc_total_card(self, degraded: bool) -> tuple[dict[str, Any], bool]:
        """`doc_total` 卡片：**不来自指标桶**（R-8 / AC-09-22）。

        删一篇文档后它必须**立即**变化——预聚合桶做不到这一点，
        所以这里实时查 `kb_documents`（经 03 的 `DocService`，ER-02）。
        """
        card: dict[str, Any] = {
            "value": None, "unit": "个", "label": CARD_LABELS["doc_total"],
            "source": "kb_documents 实时计数（非指标桶）"}
        try:
            from app.services.doc_service import doc_service

            counts = await doc_service.count_by_status()
            card["value"] = int(counts.get("total") or 0)
            card["sub"] = f"已启用 {int(counts.get('enabled') or 0)}"
            return card, degraded
        except Exception as exc:                            # noqa: BLE001
            # MET-4004：**只有这一张卡**为 null，其余 4 张照常（Spec §7.2 第 6 项）
            logger.error("知识单元计数失败（code=%s）：%s",
                         Err.MET_DOC_COUNT_FAILED.code, exc)
            return card, True

    # ================================================================== 趋势
    async def trend(self, *, days: int = 7,
                    metrics: Sequence[str] = ("pv", "uv"),
                    granularity: str | None = None,
                    start_ts: int | None = None, end_ts: int | None = None
                    ) -> dict[str, Any]:
        """访问量与提问量趋势（Spec §3.2）：`x_axis` 与各 `series[].data` **等长**。"""
        moment = metric_repo.now_ms()
        start_s, end_s = _resolve_range(moment, days, start_ts, end_ts)
        span_days = _span_days(start_s, end_s)
        wanted = _expand_metrics(metrics)
        chosen = _resolve_granularity(granularity, span_days)
        explicit = granularity is not None
        key = (f"trend|{chosen}|{start_s}|{end_s}|"
               f"{','.join(wanted)}|{'E' if explicit else 'A'}")
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        slots = _slots(start_s, end_s, chosen)
        series: list[dict[str, Any]] = []
        degraded = False
        source_granularity = chosen
        try:
            for name in wanted:
                if name == "uv":
                    values, used = await self._uv_series(slots, start_s, end_s)
                else:
                    values, used, fell_back = await self._metric_series(
                        name, slots=slots, start_s=start_s, end_s=end_s,
                        chosen=chosen, span_days=span_days, explicit=explicit)
                    degraded = degraded or fell_back
                    source_granularity = used
                series.append(_series_payload(name, values))
            range_uv = await _range_uv(start_s, end_s) if "uv" in wanted else None
        except BizError:
            raise
        except Exception as exc:                            # noqa: BLE001
            # R-10：趋势图是看板主体，降级成空图会误导 → 直接 MET-4001
            raise BizError(Err.MET_READ_FAILED, f"趋势查询失败：{exc}") from exc

        data = {
            "days": days,
            "start_ts": start_s * 1000,
            "end_ts": end_s * 1000,
            "granularity": chosen,
            "downsampled": chosen != M1,
            # 实际读的粒度可能与 `granularity` 不同（粗桶缺失时回退现算）——
            # 回显它是为了让"为什么标了 degraded"可解释，而不是靠猜
            "source_granularity": source_granularity,
            "x_axis": _slot_labels(slots, chosen),
            "series": series,
            "notes": {"token_scope": TOKEN_SCOPE_NOTE},
            # 区间 UV（各日桶求并集后的去重人数）：AC-09-02 的判定依据。
            # 不能由 `series` 里的 UV 相加得到，故单列一项
            "range_uv": range_uv,
            "degraded": degraded,
        }
        self._cache_put(key, data)
        return data

    async def _metric_series(self, name: str, *, slots: Sequence[int],
                             start_s: int, end_s: int, chosen: str,
                             span_days: int, explicit: bool
                             ) -> tuple[list[int], str, bool]:
        """单指标序列（缺桶补 0 —— AC-09-17：空区间不报错、不出现除零）。

        返回 `(data, 实际读取粒度, 是否降级)`。自动模式下的回退见模块头。
        """
        rows = await _read_global_buckets(chosen, start_s, end_s)
        if not rows and explicit and chosen in (H1, D1):
            # 客户端点名要 1h/1d 的桶，但我们一个都没有：
            # 返回全零序列等于"静默少算"，必须显式告诉它桶还没汇总出来
            raise BizError(Err.MET_ROLLUP_PENDING,
                           f"{chosen} 粒度的桶尚未汇总完成，可先用默认粒度或稍后重试")
        used, degraded = chosen, False
        if not rows:
            # ★ 只在**确实没有粗粒度桶**时才去读更细的桶。
            # 无条件往下读会让"30 天趋势"顺手把 `1h`/`1m` 也拉一遍——
            # 那正是 AC-09-12 要钉死的浪费（也正是"降采样失效"的典型写法）。
            for gran in _finer_chain(chosen):
                if gran == M1 and span_days > SHORT_TTL_DAYS:
                    raise BizError(Err.MET_DATA_EXPIRED,
                                   f"区间 {span_days} 天已超出分钟桶 "
                                   f"{SHORT_TTL_DAYS} 天保留期，且没有可回退的粗粒度桶")
                alt = await _read_global_buckets(gran, start_s, end_s)
                if alt:
                    rows, used, degraded = alt, gran, True
                    break
        if not rows:
            return [0] * len(slots), used, degraded
        aggregated: dict[int, int] = {}
        for row in rows:
            slot = align_ts(int(row["bucket_ts"]) * 1000, chosen)
            value = int((row.get("metrics") or {}).get(name, 0) or 0)
            aggregated[slot] = aggregated.get(slot, 0) + value
        return [aggregated.get(slot, 0) for slot in slots], used, degraded

    async def _uv_series(self, slots: Sequence[int], start_s: int,
                         end_s: int) -> tuple[list[int], str]:
        """UV 序列：**只能来自 `1d` 桶**（R-6 / AD-12）。

        `1h` 槽位的 UV 取"该小时所属自然日的去重人数"——
        绝不去把小时桶的数字相加（那会把跨小时重复的人算多次）。
        """
        rows = await metric_repo.list_buckets(
            bucket_type=MetricBucketType.GLOBAL.value, bucket_key="uv",
            granularity=D1, start_ts=start_s, end_ts=end_s, limit=BUCKET_READ_LIMIT)
        daily = {int(r["bucket_ts"]): len(r.get("uv_set") or []) for r in rows}
        return ([daily.get(align_ts(slot * 1000, D1), 0) for slot in slots], D1)

    async def _totals_with_fallback(self, *, start_s: int, end_s: int,
                                    span_days: int, allow_expired: bool
                                    ) -> tuple[dict[str, Any], str, bool]:
        """按跨度选粒度求和；粗桶缺失时回退到更细的桶现算（返回 `degraded`）。"""
        chosen = _pick_granularity(span_days)
        totals, count = await _sum_global_buckets(chosen, start_s, end_s)
        if count:
            return totals, chosen, False
        for gran in _finer_chain(chosen):
            if gran == M1 and span_days > SHORT_TTL_DAYS:
                if allow_expired:
                    # `overview` 的降级契约是"零值卡片 + degraded:true"，
                    # 所以数据过期对它不是错误（§7.2 第 2 项）
                    return {}, chosen, True
                raise BizError(Err.MET_DATA_EXPIRED,
                               f"区间 {span_days} 天已超出分钟桶保留期，"
                               f"且没有可回退的粗粒度桶")
            totals, count = await _sum_global_buckets(gran, start_s, end_s)
            if count:
                return totals, gran, True
        return {}, chosen, False

    # ================================================================== 延时
    async def latency(self, *, days: int = 7, mode: str = "bucket",
                      group_by: str | None = None,
                      percentiles: Sequence[str] = ("P50", "P95"),
                      start_ts: int | None = None, end_ts: int | None = None
                      ) -> dict[str, Any]:
        """响应延时分布（Spec §3.3）：直方图 + **P50 / P95 双分位**（G-08）。"""
        moment = metric_repo.now_ms()
        start_s, end_s = _resolve_range(moment, days, start_ts, end_ts)
        span_days = _span_days(start_s, end_s)
        for name in percentiles:
            if str(name).upper() not in ("P50", "P95"):
                # G-08 已固定为 P50 + P95：请求别的分位直接拒绝，
                # 而不是"悄悄按 P95 返回"——那会让调用方以为拿到了 P99
                raise BizError(Err.MET_PERCENTILE_UNSUPPORTED,
                               f"只支持 P50 / P95：{name}")
        if mode not in ("bucket", "exact"):
            raise BizError(Err.SYS_PARAM_INVALID, f"不支持的 mode：{mode}")
        if group_by not in (None, "", "source", "stage"):
            raise BizError(Err.SYS_PARAM_INVALID, f"不支持的 group_by：{group_by}")
        if mode == "exact" and span_days > EXACT_MAX_DAYS:
            raise BizError(Err.MET_EXACT_RANGE_TOO_LARGE,
                           f"exact 模式最长 {EXACT_MAX_DAYS} 天，当前 {span_days} 天")
        key = (f"latency|{mode}|{group_by}|{start_s}|{end_s}")
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        degraded = False
        precision = "approximate"
        values: list[int] | None = None
        if mode == "exact":
            try:
                values = await _exact_samples(start_s, end_s)
                if len(values) > EXACT_SAMPLE_LIMIT:
                    # MET-3004：样本超限时"改用 bucket"是给调用方的建议，
                    # 不是我们偷偷换口径——换口径会让"精确"这个词失去意义
                    raise BizError(
                        Err.MET_EXACT_TOO_MANY_SAMPLES,
                        f"区间内样本超过 {EXACT_SAMPLE_LIMIT} 条，请改用 mode=bucket")
            except BizError:
                raise
            except Exception as exc:                        # noqa: BLE001
                # MET-4005：`qa_logs` 不可读 → **自动降级为 bucket**（精度降、口径不变）
                logger.warning("精确分位不可用，自动降级为 bucket 模式（code=%s）：%s",
                               Err.MET_EXACT_UNAVAILABLE.code, exc)
                mode, degraded, values = "bucket", True, None

        if mode == "exact" and values is not None:
            precision = "exact"
            histogram = [0] * len(LATENCY_BUCKETS)
            for value in values:
                for index, (label, _, _, _) in enumerate(LATENCY_BUCKETS):
                    if latency_bucket_of(value) == label:
                        histogram[index] += 1
                        break
            sample_size = len(values)
            ordered = sorted(values)
            p50 = _at(ordered, 0.50) if ordered else None
            p95 = _at(ordered, 0.95) if ordered else None
            avg_ms = int(sum(ordered) / sample_size) if sample_size else 0
        else:
            counts = await self._histogram(start_s, end_s)
            histogram = counts
            sample_size = sum(counts)
            p50 = _weighted_percentile(counts, 0.50) if sample_size else None
            p95 = _weighted_percentile(counts, 0.95) if sample_size else None
            avg_ms = await self._avg_elapsed(start_s, end_s, span_days, counts)

        by_stage = await self._stage_averages(start_s, end_s) if group_by == "stage" \
            else None
        by_source = await self._source_counts(start_s, end_s) if group_by == "source" \
            else None
        if (by_stage and by_stage.get("partial")) or (by_source is None
                                                     and group_by == "source"):
            degraded = True

        bins = [{"label": f"{label}ms",
                 "range": [low, None if high >= LATENCY_TAIL else high],
                 "count": count}
                for (label, low, high, _), count in zip(LATENCY_BUCKETS, histogram)]
        data = {
            "days": days,
            "start_ts": start_s * 1000,
            "end_ts": end_s * 1000,
            "mode": mode,
            "precision": precision,
            "sample_size": sample_size,
            "avg_ms": avg_ms,
            "bins": bins,
            "percentiles": {
                "p50_ms": p50, "p95_ms": p95,
                "p50_s": round(p50 / 1000, 2) if p50 is not None else None,
                "p95_s": round(p95 / 1000, 2) if p95 is not None else None,
            },
            "by_stage": by_stage,
            "by_source": by_source,
            "degraded": degraded,
        }
        self._cache_put(key, data)
        return data

    async def _histogram(self, start_s: int, end_s: int) -> list[int]:
        """7 个固定区间的计数（缺桶补 0，R-5：计数为 0 的也必须返回）。"""
        rows = await metric_repo.list_buckets(
            bucket_type=MetricBucketType.LATENCY.value, granularity=M1,
            start_ts=start_s, end_ts=end_s, limit=BUCKET_READ_LIMIT)
        counts: dict[str, int] = {}
        for row in rows:
            key = str(row.get("bucket_key"))
            counts[key] = counts.get(key, 0) + int(
                (row.get("metrics") or {}).get("pv", 0) or 0)
        if not counts:
            # `1m` 桶缺失（长跨度或刚启动）时回退到 `1h` 桶，
            # 否则"近 30 天的延时分布"会显示成一片空
            rows = await metric_repo.list_buckets(
                bucket_type=MetricBucketType.LATENCY.value, granularity=H1,
                start_ts=start_s, end_ts=end_s, limit=BUCKET_READ_LIMIT)
            for row in rows:
                key = str(row.get("bucket_key"))
                counts[key] = counts.get(key, 0) + int(
                    (row.get("metrics") or {}).get("pv", 0) or 0)
        return [counts.get(label, 0) for label, _, _, _ in LATENCY_BUCKETS]

    async def _avg_elapsed(self, start_s: int, end_s: int, span_days: int,
                           counts: Sequence[int]) -> int:
        """平均耗时：优先用全局桶的 `elapsed_sum_ms / pv`（精确累加），
        全局桶不可用时退回"区间代表值加权平均"（近似但不会凭空为 0）。"""
        try:
            totals, _, _ = await self._totals_with_fallback(
                start_s=start_s, end_s=end_s, span_days=span_days,
                allow_expired=True)
            pv = int(totals.get("pv") or 0)
            if pv:
                return int(int(totals.get("elapsed_sum_ms") or 0) / pv)
        except Exception as exc:                            # noqa: BLE001
            logger.warning("平均耗时读取失败，改用直方图代表值估算：%s", exc)
        total = sum(counts)
        if not total:
            return 0
        weighted = sum(count * bucket[3]
                       for count, bucket in zip(counts, LATENCY_BUCKETS))
        return int(weighted / total)

    async def _stage_averages(self, start_s: int, end_s: int) -> dict[str, Any]:
        """分段耗时均值（R-6：缺失即 `null` + `partial:true`，**不得用 0 冒充**）。"""
        fields = ("retrieval_ms", "rerank_ms", "llm_ms")
        try:
            rows = await _aggregate_qa_logs([
                {"$match": {"asked_at": _qa_match(start_s, end_s)}},
                {"$group": {
                    "_id": None,
                    **{f: {"$avg": f"${f}"} for f in fields},
                    **{f"{f}_n": {"$sum": {"$cond": [
                        {"$eq": [{"$type": f"${f}"}, "missing"]}, 0, 1]}}
                        for f in fields}}},
            ])
        except Exception as exc:                            # noqa: BLE001
            logger.warning("分段耗时聚合失败（code=%s）：%s",
                           Err.MET_LOG_AGG_FAILED.code, exc)
            return {**{f: None for f in fields}, "partial": True}
        if not rows:
            return {**{f: None for f in fields}, "partial": True}
        row = rows[0]
        result: dict[str, Any] = {}
        partial = False
        for name in fields:
            value = row.get(name)
            if value is None or not int(row.get(f"{name}_n") or 0):
                result[name] = None
                partial = True
            else:
                result[name] = int(value)
        result["partial"] = partial
        return result

    async def _source_counts(self, start_s: int, end_s: int
                             ) -> dict[str, int] | None:
        """按 `answer_source` 分组的问答次数（`group_by=source`）。"""
        try:
            rows = await _aggregate_qa_logs([
                {"$match": {"asked_at": _qa_match(start_s, end_s)}},
                {"$group": {"_id": "$answer_source", "count": {"$sum": 1}}},
            ])
        except Exception as exc:                            # noqa: BLE001
            logger.warning("来源分组聚合失败（code=%s）：%s",
                           Err.MET_LOG_AGG_FAILED.code, exc)
            return None
        return {str(r.get("_id") or "unknown"): int(r.get("count") or 0)
                for r in rows}

    # ================================================================== 榜单
    async def ranking(self, *, kind: str = "both", days: int = 7, limit: int = 10,
                      start_ts: int | None = None, end_ts: int | None = None
                      ) -> dict[str, Any]:
        """高频问题榜 / 热门知识榜（Spec §3.4；TOP-N，`rank` 从 1 连续）。"""
        if not 1 <= limit <= 50:
            raise BizError(Err.MET_RANK_LIMIT_INVALID, f"limit 需在 1~50：{limit}")
        if kind not in ("question", "doc", "both"):
            raise BizError(Err.SYS_PARAM_INVALID, f"不支持的榜单类型：{kind}")
        moment = metric_repo.now_ms()
        start_s, end_s = _resolve_range(moment, days, start_ts, end_ts)
        key = f"ranking|{kind}|{limit}|{start_s}|{end_s}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        degraded = False
        data: dict[str, Any] = {
            "range": {"days": days, "start_ts": start_s * 1000,
                      "end_ts": end_s * 1000},
            "degraded": False,
        }
        if kind in ("question", "both"):
            items, fell_back = await self._top_questions(start_s, end_s, limit)
            data["top_questions"] = items
            degraded = degraded or fell_back
        if kind in ("doc", "both"):
            docs, fell_back = await self._top_docs(start_s, end_s, limit)
            data["top_docs"] = docs
            degraded = degraded or fell_back
        data["degraded"] = degraded
        self._cache_put(key, data)
        return data

    async def _top_questions(self, start_s: int, end_s: int, limit: int
                             ) -> tuple[list[dict[str, Any]], bool]:
        """高频问题榜：主路径聚合 `qa_logs.question`，失败降级读 FAQ 候选频次。"""
        try:
            from app.repositories.faq_repo import normalize_question

            items = await self._question_ranking(start_s, end_s, limit)
            faq_ids = await _faq_ids_of([i["question"] for i in items])
            for item in items:
                item["source"] = "qa_logs"
                # 用**归一化键**回填：榜单里的问题是 `qa_logs` 的原文
                # （可能带标点/空白），与 `faqs.question` 的字面值不保证逐字相同
                item["faq_id"] = faq_ids.get(normalize_question(item["question"]))
            return items, False
        except Exception as exc:                            # noqa: BLE001
            # MET-4002 的语义：降级数据源的**语义与榜单不同**（候选频次 ≠ 提问次数），
            # 所以必须同时给出 `degraded:true` 与 `source:"faq"`（Spec §5 的注）
            logger.error("高频问题榜聚合失败（code=%s），降级读 FAQ 候选频次：%s",
                         Err.MET_LOG_AGG_FAILED.code, exc)
            return await self._question_ranking_fallback(limit), True

    async def _question_ranking(self, start_s: int, end_s: int, limit: int
                                ) -> list[dict[str, Any]]:
        """高频问题榜：`qa_logs` 按**归一化问法**聚合（R-1 / AC-09-10）。

        这是 AC-09-04 明确豁免的一处 `qa_logs` 聚合（榜单必须看原文，
        而桶里只存计数）。它命中 `ix_asked_at`，不是集合扫描。

        `$toLower` 只是为了先把"大小写不同"的写法并到一起；
        真正的归一化（去标点 / 去空白 / 同义收口）在 Python 侧用
        **08 模块同一个** `normalize_question()` 完成（R-2：禁止各写一份）。
        """
        rows = await _aggregate_qa_logs([
            {"$match": {"asked_at": _qa_match(start_s, end_s),
                        "question": {"$nin": [None, ""]}}},
            {"$group": {"_id": {"$toLower": "$question"},
                        "count": {"$sum": 1},
                        "first_at": {"$min": "$asked_at"}}},
            {"$sort": {"count": -1, "first_at": 1}},
            {"$limit": limit * 4}])
        from app.repositories.faq_repo import normalize_question

        merged: dict[str, dict[str, Any]] = {}
        for row in rows:
            text = str(row["_id"] or "")
            norm = normalize_question(text)
            target = merged.get(norm)
            if target is None:
                merged[norm] = {"question": text, "count": int(row["count"])}
            else:
                target["count"] += int(row["count"])
        ordered = sorted(merged.values(), key=lambda r: (-r["count"], r["question"]))
        return [{"rank": index, "question": item["question"], "count": item["count"]}
                for index, item in enumerate(ordered[:limit], start=1)]

    async def _question_ranking_fallback(self, limit: int) -> list[dict[str, Any]]:
        """降级路径：读 FAQ 候选的 `frequency`（Spec `MET-4002` / R-4）。"""
        from app.repositories import faq_repo

        rows, _ = await faq_repo.list_candidates(page=1, page_size=limit)
        return [{"rank": index, "question": r.get("representative_question"),
                 "count": int(r.get("frequency") or 0), "source": "faq",
                 "faq_id": r.get("_id")}
                for index, r in enumerate(rows, start=1)]

    async def _top_docs(self, start_s: int, end_s: int, limit: int
                        ) -> tuple[list[dict[str, Any]], bool]:
        """热门知识榜：**`doc` 维度桶**为第一数据源（AD-07），
        退路是 `qa_logs.recalled_chunks[].doc_id`（带 `degraded`）。

        为什么第一数据源不是 `qa_logs.allowed_chunks`（Spec §3.4 的措辞）：
        冻结的 `qa_logs` 里 `allowed_chunks` 只存 `chunk_id`（不存 `doc_id`），
        要反查必须逐条回 Milvus；而 `inc()` 已经把"被引用的 `doc_id`"
        累进了 `doc` 桶（§4.1 第 6 步），一次 `find` 就能拿到计数。
        退路保留，是因为"桶被清过 / 06 没投递"时榜单不该整页空白。
        """
        degraded = False
        rows = await metric_repo.list_buckets(
            bucket_type=MetricBucketType.DOC.value, granularity=D1,
            start_ts=start_s, end_ts=end_s, limit=BUCKET_READ_LIMIT)
        totals: dict[str, int] = {}
        for row in rows:
            key = str(row.get("bucket_key") or "")
            if key:
                totals[key] = totals.get(key, 0) + int(
                    (row.get("metrics") or {}).get("pv", 0) or 0)
        if not totals:
            try:
                totals = await _doc_counts_from_logs(start_s, end_s)
                degraded = bool(totals)
            except Exception as exc:                        # noqa: BLE001
                logger.error("热门知识榜聚合失败（code=%s）：%s",
                             Err.MET_READ_FAILED.code, exc)
                raise BizError(Err.MET_READ_FAILED, f"热门知识榜聚合失败：{exc}") from exc
        top = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        titles, title_degraded = await _doc_details([doc_id for doc_id, _ in top])
        degraded = degraded or title_degraded
        return [{"rank": index, "doc_id": doc_id,
                 "title": titles.get(doc_id, {}).get("title") or doc_id,
                 "cite_count": count,
                 "category_id": titles.get(doc_id, {}).get("category_id"),
                 "deleted": bool(titles.get(doc_id, {}).get("deleted"))}
                for index, (doc_id, count) in enumerate(top, start=1)], degraded

    # ================================================================== 导出
    async def export_csv(self, *, metric: str = "overview", days: int = 7,
                         start_ts: int | None = None, end_ts: int | None = None
                         ) -> tuple[str, str]:
        """导出看板数据（Spec §3.5）：CSV + **UTF-8 BOM**（AC-09-21）。

        同步流式生成、不落临时文件（R-4）：大区间导出落盘会把磁盘打满，
        而"边算边写"对 10 万行的量级完全够用。
        """
        if metric not in ("overview", "trend", "latency", "ranking"):
            raise BizError(Err.SYS_PARAM_INVALID, f"不支持的导出对象：{metric}")
        moment = metric_repo.now_ms()
        start_s, end_s = _resolve_range(moment, days, start_ts, end_ts)
        header, rows = await self._export_rows(metric, days, start_s, end_s)
        if len(rows) > EXPORT_ROW_LIMIT:
            raise BizError(Err.MET_EXPORT_TOO_LARGE,
                           f"导出预计 {len(rows)} 行，超过 {EXPORT_ROW_LIMIT} 行上限，"
                           f"请缩小时间范围")
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(header)
        writer.writerows(rows)
        filename = f"metrics_{metric}_{time.strftime('%Y%m%d_%H%M')}.csv"
        # BOM 必须有：没有它 Excel 打开中文就是乱码（最常见的"导出失败"投诉）
        return filename, "\ufeff" + buffer.getvalue()

    async def _export_rows(self, metric: str, days: int, start_s: int, end_s: int
                           ) -> tuple[list[str], list[list[Any]]]:
        """把四个查询的结果摊平成 CSV 行（表头随对象而变）。"""
        stamp = datetime.fromtimestamp(end_s, tz=TZ).strftime("%Y-%m-%d %H:%M")
        if metric == "overview":
            data = await self.overview(days=days, start_ts=start_s * 1000,
                                       end_ts=end_s * 1000)
            header = ["指标", "值", "单位", "说明"]
            rows: list[list[Any]] = [["统计区间", f"近 {days} 天", "", ""],
                                     ["生成时间", stamp, "", ""]]
            for key, card in (data.get("cards") or {}).items():
                rows.append([card.get("label") or key,
                             "" if card.get("value") is None else card.get("value"),
                             card.get("unit") or "", card.get("source") or ""])
            for key, value in (data.get("totals") or {}).items():
                rows.append([key, "" if value is None else value, "", "总量"])
            return header, rows
        if metric == "trend":
            data = await self.trend(days=days, start_ts=start_s * 1000,
                                    end_ts=end_s * 1000,
                                    metrics=("pv", "uv", "token", "denied",
                                             "rag", "no_knowledge", "faq_hit_cnt"))
            keys = [s["key"] for s in data["series"]]
            rows = [[axis, *[series["data"][index] for series in data["series"]]]
                    for index, axis in enumerate(data["x_axis"])]
            return ["时间", *keys], rows
        if metric == "latency":
            data = await self.latency(days=days, start_ts=start_s * 1000,
                                      end_ts=end_s * 1000)
            rows = [["模式", data["mode"], "", ""],
                    ["精度", data["precision"], "", ""],
                    ["样本量", data["sample_size"], "", ""],
                    ["P50(ms)", data["percentiles"]["p50_ms"], "", ""],
                    ["P95(ms)", data["percentiles"]["p95_ms"], "", ""]]
            rows += [[b["label"], b["count"], "", ""] for b in data["bins"]]
            return ["项", "值", "单位", "说明"], rows
        data = await self.ranking(days=days, start_ts=start_s * 1000,
                                 end_ts=end_s * 1000, kind="both")
        rows = [["高频问题", item.get("rank"), item.get("question"),
                 item.get("count"), item.get("source")]
                for item in data.get("top_questions") or []]
        rows += [["热门知识", item.get("rank"), item.get("title"),
                  item.get("cite_count"), item.get("doc_id")]
                 for item in data.get("top_docs") or []]
        return ["榜单", "排名", "名称", "次数", "来源"], rows

    # ================================================================== 维护
    def check_rate(self, user_id: str, *, now: float | None = None) -> None:
        """看板限流：> `metric.rate_limit_per_min` 次/分钟/用户 → `MET-2004`。"""
        limit = 60
        try:
            limit = int(config_service.get_raw("metric.rate_limit_per_min"))
        except Exception:                                   # noqa: BLE001
            pass
        current = int(now if now is not None else time.time())
        window, count = self._rate.get(user_id, (current, 0))
        if current - window >= 60:
            window, count = current, 0
        count += 1
        self._rate[user_id] = (window, count)
        if count > limit:
            raise BizError(Err.MET_RATE_LIMITED,
                           f"看板请求过于频繁（>{limit} 次/分钟）")

    def reset(self) -> None:
        """清空缓存与限流计数（测试隔离用）。"""
        self._cache.clear()
        self._rate.clear()
        self._rolling = False

    def health(self) -> dict[str, Any]:
        return {"rolling": self._rolling, "cache_size": len(self._cache),
                "rate_users": len(self._rate)}


# ---------------------------------------------------------------------- 纯函数
def _resolve_range(now_ms: int, days: int, start_ts: int | None,
                   end_ts: int | None) -> tuple[int, int]:
    """把「近 N 天 / 显式区间」统一成 `(start_s, end_s)` 的 **epoch 秒**（R-1~R-3）。

    `start_ts` / `end_ts` 入参与出参都是**毫秒**（与 Spec §3.1 的 `range` 一致），
    内部计算一律用秒——桶的 `bucket_ts` 就是秒，混用单位是这类模块最常见的事故。
    """
    if start_ts is None and end_ts is None:
        if not 1 <= days <= 365:
            raise BizError(Err.MET_RANGE_INVALID, f"days 需在 1~365：{days}")
        end_slot = align_ts(now_ms, D1)
        start = _shift_days(end_slot, -(days - 1))
        return start, now_ms // 1000
    if start_ts is None or end_ts is None:
        raise BizError(Err.MET_RANGE_INVALID, "start_ts 与 end_ts 必须同时提供")
    if not 1 <= days <= 365:
        raise BizError(Err.MET_RANGE_INVALID, f"days 需在 1~365：{days}")
    if start_ts >= end_ts:
        raise BizError(Err.MET_RANGE_INVALID, "start_ts 必须早于 end_ts")
    if end_ts > now_ms + FUTURE_TOLERANCE_MS:
        # MET-1002：未来区间会刷出一张全零的图，看起来像"系统没数据"
        raise BizError(Err.MET_FUTURE_RANGE, "end_ts 不能晚于当前时间")
    if (end_ts - start_ts) > MAX_RANGE_DAYS * 86400_000:
        raise BizError(Err.MET_RANGE_TOO_LARGE,
                       f"查询跨度不能超过 {MAX_RANGE_DAYS} 天")
    return int(start_ts) // 1000, int(end_ts) // 1000


def _shift_days(ts_seconds: int, delta_days: int) -> int:
    moment = datetime.fromtimestamp(ts_seconds, tz=TZ) + timedelta(days=delta_days)
    return int(moment.timestamp())


def _span_days(start_s: int, end_s: int) -> int:
    """区间覆盖的自然日数（用于选粒度：半天也算 1 天）。"""
    return max(1, (int(end_s) - int(start_s) + 86399) // 86400)


def _pick_granularity(span_days: int) -> str:
    """按跨度选粒度（Spec §4.2 策略表）。"""
    if span_days <= MINUTE_MAX_DAYS:
        return M1
    if span_days <= HOURLY_MAX_DAYS:
        return H1
    return D1


def _resolve_granularity(explicit: str | None, span_days: int) -> str:
    """校验客户端点名的粒度；缺省时按跨度自动选（R-2 / R-3 / MET-1005 / MET-1010）。"""
    if explicit is None:
        return _pick_granularity(span_days)
    if explicit == M1:
        if span_days > MINUTE_MAX_DAYS:
            # MET-1005：跨度 > 1 天要分钟桶 = 请求 43200 个文档，
            # 这正是 §4.2 策略表里"绝不用 1m 算 > 1 天"的那条红线
            raise BizError(Err.MET_MINUTE_RANGE_TOO_LARGE,
                           f"跨度 {span_days} 天不允许分钟级粒度（最多 1 天）")
        return M1
    if explicit not in (H1, D1):
        raise BizError(Err.MET_GRANULARITY_INVALID,
                       f"granularity 只支持 1m / 1h / 1d：{explicit}")
    return explicit


def _finer_chain(granularity: str) -> list[str]:
    """比给定粒度更细的粒度（回退现算的顺序：由粗到细）。"""
    if granularity == D1:
        return [H1, M1]
    if granularity == H1:
        return [M1]
    return []


def _slots(start_s: int, end_s: int, granularity: str) -> list[int]:
    """生成对齐后的时间槽序列（`x_axis` 与各 `series` 等长的前提）。"""
    step = {M1: 60, H1: 3600, D1: 86400}[granularity]
    first = align_ts(start_s * 1000, granularity)
    last = align_ts(end_s * 1000, granularity)
    slots: list[int] = []
    current = first
    while current <= last:
        slots.append(current)
        current += step
    return slots


def _slot_labels(slots: Sequence[int], granularity: str) -> list[str]:
    """`x_axis` 标签，**按 Asia/Shanghai 格式化**（R-7：前端直接显示）。"""
    pattern = {M1: "%Y-%m-%d %H:%M", H1: "%Y-%m-%d %H:00", D1: "%Y-%m-%d"}[granularity]
    return [datetime.fromtimestamp(slot, tz=TZ).strftime(pattern) for slot in slots]


def _expand_metrics(metrics: Sequence[str]) -> list[str]:
    """`metrics` 入参 → `series[].key` 列表（R-1：含未支持的值**直接拒绝**）。"""
    if not metrics:
        return ["pv", "uv"]
    out: list[str] = []
    for raw in metrics:
        name = str(raw).strip()
        if not name:
            continue
        keys = INPUT_METRICS.get(name)
        if keys is None:
            raise BizError(Err.MET_METRIC_INVALID, f"不支持的指标：{name}")
        for key in keys:
            if key not in out:
                out.append(key)
    return out or ["pv", "uv"]


def _series_payload(name: str, data: Sequence[int]) -> dict[str, Any]:
    """组装一个 `series` 元素（key / name / axis / type / unit / data）。"""
    meta = SERIES_META.get(name, {"name": name, "type": "line"})
    payload: dict[str, Any] = {"key": name, "name": meta["name"],
                               "type": meta.get("type", "line"),
                               "data": list(data)}
    if meta.get("axis"):
        payload["axis"] = meta["axis"]
    if meta.get("unit"):
        payload["unit"] = meta["unit"]
    if meta.get("exact"):
        payload["exact"] = True
    return payload


def _weighted_percentile(counts: Sequence[int], ratio: float) -> int | None:
    """用区间**代表值**加权估算分位（Spec §2.6 的口径）。

    样本量为 0 时返回 `None`（而不是 0）：`0ms` 与"没有数据"在看板上
    必须能区分开——前者会让人以为"系统快得离谱"。
    """
    total = sum(counts)
    if total <= 0:
        return None
    threshold = total * ratio
    cumulative = 0
    for (_label, _low, _high, representative), count in zip(LATENCY_BUCKETS, counts):
        cumulative += count
        if cumulative >= threshold:
            return int(representative)
    return int(LATENCY_BUCKETS[-1][3])                            # pragma: no cover


def _at(values: Sequence[int], ratio: float) -> int:
    """精确分位（样本已排序）。"""
    if not values:                                              # pragma: no cover
        return 0
    index = min(len(values) - 1, max(0, int(round(ratio * (len(values) - 1)))))
    return int(values[index])


# ---------------------------------------------------------------------- 读辅助
def _qa_match(start_s: int, end_s: int) -> dict[str, Any]:
    """`qa_logs.asked_at` 的区间条件（**毫秒**，右端补齐到整秒）。

    ★ 为什么右端是 `end_s * 1000 + 999`：`_resolve_range` 把区间统一成**秒**
    （桶的 `bucket_ts` 是秒），而 `asked_at` 是**毫秒**。如果直接写 `end_s * 1000`，
    就会被截掉当前这一秒里已经发生的那部分日志——症状是"刚刚问的那一条不在榜单里"、
    "近 1 天的精确分位少一条"，而且越是刚发生的数据越容易丢（看起来像随机丢数据）。
    """
    return {"$gte": start_s * 1000, "$lte": end_s * 1000 + 999}


async def _read_global_buckets(granularity: str, start_s: int, end_s: int
                               ) -> list[dict[str, Any]]:
    return await metric_repo.list_buckets(
        bucket_type=MetricBucketType.GLOBAL.value, granularity=granularity,
        start_ts=start_s, end_ts=end_s, limit=BUCKET_READ_LIMIT)


async def _sum_global_buckets(granularity: str, start_s: int, end_s: int
                              ) -> tuple[dict[str, Any], int]:
    totals = await metric_repo.aggregate_metrics(
        bucket_type=MetricBucketType.GLOBAL.value, granularity=granularity,
        start_ts=start_s, end_ts=end_s)
    return totals, int(totals.get("count") or 0)


async def _range_uv(start_s: int, end_s: int) -> int:
    """区间 UV = 各日桶 `uv_set` 的**并集**大小（AC-09-02：绝不相加）。"""
    return await metric_repo.overview_uv(
        start_ts=align_ts(start_s * 1000, D1), end_ts=end_s)


async def _exact_samples(start_s: int, end_s: int) -> list[int]:
    """从 `qa_logs` 取精确分位样本（只读；`mode=exact` 专用）。"""
    from app.infra.mongo import mongo
    from app.repositories import qa_repo

    cursor = mongo.collection(qa_repo.QA_LOGS).find(
        {"asked_at": _qa_match(start_s, end_s)},
        {"elapsed_ms": 1}).limit(EXACT_SAMPLE_LIMIT + 1)
    rows = await cursor.to_list(length=EXACT_SAMPLE_LIMIT + 1)
    return [int(r.get("elapsed_ms") or 0) for r in rows]


async def _aggregate_qa_logs(pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """在 `qa_logs` 上跑聚合（榜单 / 分组）。**只读**，写权限仍属 06（ER-06）。"""
    from app.infra.mongo import mongo
    from app.repositories import qa_repo

    # ⚠️ 异步 `aggregate()` 返回**协程**，必须先 await 拿到游标再 `to_list()`
    cursor = await mongo.collection(qa_repo.QA_LOGS).aggregate(pipeline)
    return await cursor.to_list(length=None)


async def _doc_counts_from_logs(start_s: int, end_s: int) -> dict[str, int]:
    """退路：从 `qa_logs.recalled_chunks[].doc_id` 反推被引用的文档次数。

    为什么用 `recalled_chunks` 而不是 `allowed_chunks`：后者只存 `chunk_id`，
    没有 `doc_id`，反查要逐条回 Milvus（ER-13 禁止）。
    它统计的是"被召回"而不是"被引用"，所以调用方**必须**带上 `degraded:true`。
    """
    return await _doc_counts_by_field(start_s, end_s, "recalled_chunks.doc_id")


async def _doc_counts_by_field(start_s: int, end_s: int, field: str
                               ) -> dict[str, int]:
    rows = await _aggregate_qa_logs([
        {"$match": {"asked_at": _qa_match(start_s, end_s)}},
        {"$unwind": f"${field.split('.')[0]}"},
        {"$group": {"_id": f"${field}", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 200}])
    return {str(r["_id"]): int(r["count"]) for r in rows if r.get("_id")}


async def _doc_details(doc_ids: Sequence[str]) -> tuple[dict[str, dict[str, Any]], bool]:
    """批量取文档标题 / 分类 / 软删标记（**一次 `$in`**，AC-09-11 / ER-13）。"""
    wanted = [d for d in dict.fromkeys(doc_ids) if d]
    if not wanted:
        return {}, False
    try:
        from app.repositories import doc_repo

        rows = await doc_repo.find_by_ids(wanted)
        return {str(r["_id"]): {"title": str(r.get("title") or r["_id"]),
                                "category_id": r.get("category_id"),
                                "deleted": bool(r.get("deleted_at"))}
                for r in rows}, False
    except Exception as exc:                                # noqa: BLE001
        # MET-4003：榜单**照常返回**，标题用占位符 + `degraded:true`（排名仍可用）
        logger.error("文档标题查询失败（code=%s）：%s",
                     Err.MET_DOC_TITLE_FAILED.code, exc)
        return {d: {"title": "（标题不可用）", "category_id": None, "deleted": False}
                for d in wanted}, True


async def _faq_ids_of(questions: Sequence[str]) -> dict[str, str]:
    """问题文本 → 已发布 FAQ 的编号（一次 `$in`；失败只记 WARN，不影响榜单）。"""
    wanted = [q for q in dict.fromkeys(questions) if q]
    if not wanted:
        return {}
    try:
        from app.repositories import faq_repo

        rows = await faq_repo.find_faqs_by_questions(wanted)
        # 键用 `question_norm`（发布时由 07 写入的归一化问法），
        # 与调用方的 `normalize_question(item["question"])` 同源
        return {str(r.get("question_norm")): str(r["_id"]) for r in rows}
    except Exception as exc:                                # noqa: BLE001
        logger.warning("FAQ 编号回填失败（榜单仍返回，faq_id 置空）：%s", exc)
        return {}


async def _open_gap_count() -> int | None:
    """未处理缺口数（看板加分卡片；08 的表，**只读**，失败返回 `null`）。"""
    try:
        from app.repositories import gap_repo

        return await gap_repo.count_gaps("open")
    except Exception as exc:                                # noqa: BLE001
        logger.warning("缺口计数失败（看板该字段返回 null）：%s", exc)
        return None


metric_service = MetricService()

__all__ = ["MetricService", "metric_service", "align_ts", "latency_bucket_of",
           "LATENCY_BUCKETS", "SERIES_META", "TOKEN_SCOPE_NOTE", "TZ"]
