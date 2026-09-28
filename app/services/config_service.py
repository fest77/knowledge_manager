# -*- coding: utf-8 -*-
"""系统配置服务（E22）——模块 00 的服务层。

**语义**：`system_config` 集合存的是**运行期可调的值**，而配置项的**定义**
（键、分组、中文名、类型、默认值、取值范围）集中在下面的 `PRESET` —— 这是
「定义在代码、值在库」的标准做法，避免出现"库里说它是 int、代码当字符串用"的漂移。

**受控集合**：只有 `PRESET` 里的键对外可见/可改；库里多出的键不会出现在接口里
（防止有人手插一行就凭空多出一个开关）。

**持久化策略**：启动时 `bootstrap()` 把缺失的预置项按默认值插入，
**已存在的一律不覆盖** —— 管理员改过的值不会被重启冲掉。

**内存热更新**：全部配置常驻内存，`get_*()` 是纯内存读，无 IO。
写入后立即刷新，不需要重启（原型 `07` 的 `stNote4` 要求"保存后热更新生效"）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.core.enums import ConfigGroup, ConfigValueType
from app.core.errors import BizError, Err
from app.core.logging import logger
from app.repositories import config_repo


@dataclass(frozen=True, slots=True)
class ConfigSpec:
    """一个配置项的**定义**（代码侧的事实源）。"""

    key: str
    group: ConfigGroup
    label: str
    default: Any
    value_type: ConfigValueType
    description: str = ""
    minimum: float | None = None
    maximum: float | None = None
    editable: bool = True


PRESET: tuple[ConfigSpec, ...] = (
    ConfigSpec("llm.model", ConfigGroup.LLM, "对话大模型", "qwen-flash",
               ConfigValueType.STRING, "OpenAI 兼容接口的模型名", editable=False),
    ConfigSpec("llm.temperature", ConfigGroup.LLM, "温度 temperature", 0.2,
               ConfigValueType.FLOAT, "越低越稳定", minimum=0.0, maximum=2.0),
    ConfigSpec("llm.max_tokens", ConfigGroup.LLM, "最大输出长度", 2048,
               ConfigValueType.INT, "单次回答的最大 token 数", minimum=64, maximum=8192),

    ConfigSpec("embedding.model", ConfigGroup.RETRIEVAL, "Embedding 模型",
               "BGE-M3（本地 · CUDA）", ConfigValueType.STRING,
               "向量化模型；本地模型，含 dense + sparse", editable=False),
    ConfigSpec("rerank.model", ConfigGroup.RETRIEVAL, "Rerank 模型",
               "bge-reranker-large", ConfigValueType.STRING,
               "重排序模型；不可用时跳过重排", editable=False),
    ConfigSpec("retrieval.recall_multiplier", ConfigGroup.RETRIEVAL, "召回放大倍数", 5,
               ConfigValueType.INT,
               "补偿应用层鉴权过滤（AD-04）。调小会漏召回、调大会增加开销（G-04）",
               minimum=1, maximum=20),

    ConfigSpec("faq.cache_sim_threshold", ConfigGroup.FAQ, "FAQ 缓存命中阈值", 0.92,
               ConfigValueType.FLOAT, "余弦相似度阈值；宁可不命中也不给错答案（G-03）",
               minimum=0.5, maximum=1.0),
    # ---- 模块 07 FAQ 沉淀（Spec §2.4；键名沿用 00 的 `组.项` 风格）----
    # ⚠️ 这一段**必须紧跟在 `faq.cache_sim_threshold` 之后**：`PRESET` 的声明顺序
    # 要与 `ConfigGroup` 的枚举顺序一致，`all_items()` 才不需要额外排序
    # （而"界面上的分组顺序"与"代码里的声明顺序"不一致，是一条迟早会踩的沟）
    ConfigSpec("faq.mine_window_days", ConfigGroup.FAQ, "挖掘时间窗(天)", 30,
               ConfigValueType.INT, "近 N 天日志；上限对齐 qa_logs 的 180 天 TTL",
               minimum=1, maximum=180),
    ConfigSpec("faq.mine_freq_threshold", ConfigGroup.FAQ, "簇频次阈值", 20,
               ConfigValueType.INT,
               "达到该频次的簇才生成候选（G-05）；调低会让候选暴增",
               minimum=2, maximum=1000),
    ConfigSpec("faq.mine_sim_threshold", ConfigGroup.FAQ, "聚类相似度阈值", 0.85,
               ConfigValueType.FLOAT, "低于此值判为不同簇（G-05）",
               minimum=0.5, maximum=1.0),
    ConfigSpec("faq.cache_enabled", ConfigGroup.FAQ, "FAQ 缓存总开关", True,
               ConfigValueType.BOOL,
               "全局降级开关：关掉后 06 一律走 RAG（演示越权风险时用）"),
    ConfigSpec("faq.hit_flush_interval_s", ConfigGroup.FAQ, "命中计数刷盘周期(秒)", 5,
               ConfigValueType.INT,
               "hit_count 先在内存累加再批量落库：命中是高频读路径，逐次写库会拖慢它",
               minimum=1, maximum=600),
    ConfigSpec("faq.mine_suppress_days", ConfigGroup.FAQ, "驳回簇抑制天数", 30,
               ConfigValueType.INT,
               "已驳回的 cluster_key 在这段时间内不再生成候选（DEC-07-3）",
               minimum=1, maximum=365),
    ConfigSpec("faq.mine_seed", ConfigGroup.FAQ, "聚类随机种子", 20260101,
               ConfigValueType.INT, "同参数二次运行要产出相同候选（可复现）",
               minimum=0, maximum=99999999),
    ConfigSpec("faq.mine_cron", ConfigGroup.FAQ, "定时挖掘计划", "0 3 * * *",
               ConfigValueType.STRING,
               "只支持「分 时 * * *」的每日形态；错开业务高峰", editable=False),

    ConfigSpec("gap.score_threshold", ConfigGroup.GAP, "缺口判定阈值", 0.75,
               ConfigValueType.FLOAT, "最高相似度低于此值判为知识缺口（G-06）",
               minimum=0.0, maximum=1.0),    # 模块 08 的聚合参数（Spec §2.4 / §3.5）
    ConfigSpec("gap.window_days", ConfigGroup.GAP, "缺口聚合窗口(天)", 30,
               ConfigValueType.INT,
               "窗口内重算频次；与 07 的挖掘窗口一致（原型写「近 30 天日志」）",
               minimum=1, maximum=90),
    ConfigSpec("gap.aggregate_interval_min", ConfigGroup.GAP, "聚合周期(分钟)", 30,
               ConfigValueType.INT,
               "定时聚合周期；服务启动后 60s 先跑一轮，之后按此周期",
               minimum=5, maximum=1440),
    ConfigSpec("gap.export_max_rows", ConfigGroup.GAP, "导出条数上限", 5000,
               ConfigValueType.INT, "超出即 GAP-1005，避免一次导出把内存打满",
               minimum=1, maximum=100000),

ConfigSpec("import.concurrency", ConfigGroup.IMPORT, "导入并发上限", 2,
               ConfigValueType.INT, "AD-09：导入是重任务，需与在线问答隔离",
               minimum=1, maximum=8),
    # 模块 04 §8.4 的 5 项运行参数：全部走配置表（而不是写死在代码里），
    # 这样"上传上限从 100MB 调成 20MB"是一次界面操作，不需要改代码重启。
    ConfigSpec("import.max_file_mb", ConfigGroup.IMPORT, "单文件大小上限(MB)", 100,
               ConfigValueType.INT, "超出即 IMP-1002；前端据此提示",
               minimum=1, maximum=2048),
    ConfigSpec("import.max_batch_files", ConfigGroup.IMPORT, "批量文件数上限", 50,
               ConfigValueType.INT, "超出即 IMP-1004", minimum=1, maximum=500),
    ConfigSpec("import.max_batch_mb", ConfigGroup.IMPORT, "批次总量上限(MB)", 500,
               ConfigValueType.INT, "超出即 IMP-1004", minimum=1, maximum=10240),
    ConfigSpec("import.max_queue", ConfigGroup.IMPORT, "队列等待上限", 20,
               ConfigValueType.INT,
               "排队任务数到此值后新上传返回 IMP-3008(429)，保护 GPU 不被灌满",
               minimum=1, maximum=500),
    ConfigSpec("import.max_retry", ConfigGroup.IMPORT, "最大重试次数", 3,
               ConfigValueType.INT, "超出即 IMP-3005；重试用新任务串 retry_of",
               minimum=0, maximum=10),
    ConfigSpec("import.timeout_min", ConfigGroup.IMPORT, "任务超时(分钟)", 30,
               ConfigValueType.INT,
               "watchdog 判据：超时无阶段推进 → timeout（IMP-4006）",
               minimum=1, maximum=1440),
    ConfigSpec("import.embed_batch_size", ConfigGroup.IMPORT, "向量化批大小", 16,
               ConfigValueType.INT,
               "显存相关：一次嵌入多少条切片。调大省时间但可能 OOM",
               minimum=1, maximum=256),

    # 模块 09 的看板参数
    ConfigSpec("metric.cache_ttl_seconds", ConfigGroup.METRIC, "看板缓存(秒)", 30,
               ConfigValueType.INT,
               "同区间重复请求走缓存：看板聚合是重查询，30 秒内不重复打库（AC-09-20）",
               minimum=0, maximum=600),
    ConfigSpec("metric.rate_limit_per_min", ConfigGroup.METRIC, "看板限流(次/分钟)", 60,
               ConfigValueType.INT, "超过即 MET-2004；缓存未命中时尤其容易触发",
               minimum=1, maximum=6000),
)

SPEC_BY_KEY: dict[str, ConfigSpec] = {s.key: s for s in PRESET}

_GROUP_ORDER = {g: i for i, g in enumerate(ConfigGroup)}


def _coerce(spec: ConfigSpec, raw: Any) -> Any:
    """把外部传入的值转成 spec 声明的类型；不合法就抛 AUTH... 不，抛 SYS-1001 参数错。"""
    t = spec.value_type
    try:
        if t is ConfigValueType.INT:
            if isinstance(raw, bool):
                raise ValueError("布尔不能当整数")
            value: Any = int(raw)
        elif t is ConfigValueType.FLOAT:
            if isinstance(raw, bool):
                raise ValueError("布尔不能当浮点数")
            value = float(raw)
        elif t is ConfigValueType.BOOL:
            if isinstance(raw, bool):
                value = raw
            elif str(raw).lower() in ("true", "1", "yes"):
                value = True
            elif str(raw).lower() in ("false", "0", "no"):
                value = False
            else:
                raise ValueError("布尔值只接受 true/false")
        elif t is ConfigValueType.STRING:
            value = str(raw)
        else:                                    # JSON 类型：保持原样
            value = raw
    except (TypeError, ValueError) as exc:
        raise BizError(Err.SYS_PARAM_INVALID,
                       f"配置 {spec.key} 需要 {t.value} 类型：{exc}") from exc

    if spec.minimum is not None and value < spec.minimum:
        raise BizError(Err.SYS_PARAM_INVALID,
                       f"配置 {spec.key} 不能小于 {spec.minimum}")
    if spec.maximum is not None and value > spec.maximum:
        raise BizError(Err.SYS_PARAM_INVALID,
                       f"配置 {spec.key} 不能大于 {spec.maximum}")
    return value


@dataclass
class ConfigService:
    """进程内配置服务。启动时 `bootstrap()`，此后 `get_*()` 全是内存读。"""

    _values: dict[str, Any] = field(default_factory=dict)
    _loaded: bool = False

    # ---------------------------------------------------------------- 生命周期
    async def bootstrap(self) -> int:
        """确保预置项都在库里（缺失则按默认值插入），然后把值载入内存。

        返回本次新插入的条数。**已存在的项不会被覆盖**。
        """
        now = int(time.time())
        inserted = 0
        for spec in PRESET:
            doc = {
                "_id": spec.key,
                "group": spec.group.value,
                "label": spec.label,
                "value": spec.default,
                "value_type": spec.value_type.value,
                "default_value": spec.default,
                "editable": spec.editable,
                "description": spec.description,
                "version": 1,
                "updated_by": "bootstrap",
                "updated_at": now,
            }
            if await config_repo.insert_missing(doc):
                inserted += 1
        await self.reload()
        logger.info("系统配置已就绪：%d 项（本次新插入 %d 项）", len(self._values), inserted)
        return inserted

    async def reload(self) -> None:
        """从库里重载全部配置到内存。"""
        rows = {r["_id"]: r for r in await config_repo.list_all()}
        values: dict[str, Any] = {}
        for spec in PRESET:
            row = rows.get(spec.key)
            values[spec.key] = row["value"] if row else spec.default
        self._values = values
        self._loaded = True

    def reset(self) -> None:
        """清空内存缓存并回到"未加载"状态。

        清库（`seed.py --drop`）与测试用例隔离时必须调用：否则内存里还留着旧值，
        而 `get_raw()` 不会察觉库里已经空了。
        """
        self._values = {}
        self._loaded = False

    # ---------------------------------------------------------------- 读
    def get_raw(self, key: str) -> Any:
        """按配置键取值；**键必须在预置表内**，否则抛错（防止拼错键静默拿默认值）。"""
        if key not in SPEC_BY_KEY:
            raise KeyError(f"未知配置键：{key}")
        return self._values.get(key, SPEC_BY_KEY[key].default)

    def all_items(self) -> list[dict[str, Any]]:
        """按「分组顺序 + 预置表顺序」返回全部配置项（界面渲染顺序）。"""
        items = []
        for spec in PRESET:
            items.append({
                "key": spec.key,
                "group": spec.group.value,
                "label": spec.label,
                "value": self.get_raw(spec.key),
                "value_type": spec.value_type.value,
                "default_value": spec.default,
                "editable": spec.editable,
                "description": spec.description,
                "minimum": spec.minimum,
                "maximum": spec.maximum,
            })
        return sorted(items, key=lambda i: _GROUP_ORDER[ConfigGroup(i["group"])])

    # ---------------------------------------------------------------- 便捷 getter
    @property
    def recall_multiplier(self) -> int:
        """AD-04 / G-04：召回放大倍数。"""
        return int(self.get_raw("retrieval.recall_multiplier"))

    @property
    def faq_cache_sim_threshold(self) -> float:
        """G-03：FAQ 缓存命中阈值。"""
        return float(self.get_raw("faq.cache_sim_threshold"))

    @property
    def gap_score_threshold(self) -> float:
        """G-06：知识缺口判定阈值。"""
        return float(self.get_raw("gap.score_threshold"))

    # 模块 08 的聚合参数（统一经这里读：界面改了下一轮聚合就生效）
    @property
    def gap_window_days(self) -> int:
        """聚合窗口（天）——频次在这个窗口内**重算覆盖**。"""
        return int(self.get_raw("gap.window_days"))

    @property
    def gap_aggregate_interval_min(self) -> int:
        """定时聚合周期（分钟）。"""
        return int(self.get_raw("gap.aggregate_interval_min"))

    @property
    def gap_export_max_rows(self) -> int:
        """导出条数上限（超出即 `GAP-1005`）。"""
        return int(self.get_raw("gap.export_max_rows"))

    @property
    def import_concurrency(self) -> int:
        """AD-09：导入并发上限。"""
        return int(self.get_raw("import.concurrency"))

    # 模块 04 的运行参数：**统一经这里读**，路由与服务都不直接读 PRESET，
    # 这样"界面上改了配置"立刻对下一次上传生效（值在内存里，无需重启）。
    @property
    def import_max_file_mb(self) -> int:
        """单文件大小上限（MB）→ `IMP-1002`。"""
        return int(self.get_raw("import.max_file_mb"))

    @property
    def import_max_batch_files(self) -> int:
        """批量文件数上限 → `IMP-1004`。"""
        return int(self.get_raw("import.max_batch_files"))

    @property
    def import_max_batch_mb(self) -> int:
        """批次总量上限（MB）→ `IMP-1004`。"""
        return int(self.get_raw("import.max_batch_mb"))

    @property
    def import_max_queue(self) -> int:
        """队列等待上限 → `IMP-3008`（429）。"""
        return int(self.get_raw("import.max_queue"))

    @property
    def import_max_retry(self) -> int:
        """最大重试次数 → `IMP-3005`。"""
        return int(self.get_raw("import.max_retry"))

    @property
    def import_timeout_min(self) -> int:
        """任务超时分钟数 → watchdog 判 `timeout`（`IMP-4006`）。"""
        return int(self.get_raw("import.timeout_min"))

    @property
    def import_embed_batch_size(self) -> int:
        """向量化批大小（显存相关）。"""
        return int(self.get_raw("import.embed_batch_size"))

    # 模块 07 的 FAQ 参数（同样统一经这里读，界面改了立即生效）
    @property
    def faq_mine_window_days(self) -> int:
        return int(self.get_raw("faq.mine_window_days"))

    @property
    def faq_mine_freq_threshold(self) -> int:
        return int(self.get_raw("faq.mine_freq_threshold"))

    @property
    def faq_mine_sim_threshold(self) -> float:
        return float(self.get_raw("faq.mine_sim_threshold"))

    @property
    def faq_cache_enabled(self) -> bool:
        """缓存总开关（全局降级开关）。"""
        return bool(self.get_raw("faq.cache_enabled"))

    @property
    def faq_hit_flush_interval_s(self) -> int:
        return int(self.get_raw("faq.hit_flush_interval_s"))

    @property
    def faq_mine_suppress_days(self) -> int:
        return int(self.get_raw("faq.mine_suppress_days"))

    @property
    def faq_mine_seed(self) -> int:
        return int(self.get_raw("faq.mine_seed"))

    @property
    def faq_mine_cron(self) -> str:
        return str(self.get_raw("faq.mine_cron"))

    # ---------------------------------------------------------------- 写
    async def update(self, pairs: dict[str, Any], actor: str) -> dict[str, Any]:
        """批量更新配置值。

        - 未知键 / 不可编辑项 / 类型或范围不合法 → 抛业务错误，**整批不生效**（先校验后写）
        - 值没变化 → 记入 `unchanged`，不写库
        - 返回 `{changed, unchanged, before, after, items}`；`before`/`after` 供路由层
          逐键写 `config.update` 审计（模块 10 §2.2.1）
        """
        unknown = [k for k in pairs if k not in SPEC_BY_KEY]
        if unknown:
            raise BizError(Err.SYS_PARAM_INVALID, f"未知配置键：{sorted(unknown)}")

        locked = [k for k in pairs if not SPEC_BY_KEY[k].editable]
        if locked:
            raise BizError(Err.SYS_PARAM_INVALID,
                           f"这些配置项不可在界面修改（须改 .env）：{sorted(locked)}")

        prepared: dict[str, Any] = {}
        for key, raw in pairs.items():
            value = _coerce(SPEC_BY_KEY[key], raw)
            if value != self.get_raw(key):
                prepared[key] = value

        if not prepared:
            return {"changed": [], "unchanged": sorted(pairs), "before": {}, "after": {},
                    "items": self.all_items()}

        now = int(time.time())
        before: dict[str, Any] = {}
        for key, value in prepared.items():
            old = await config_repo.set_value(key, value, actor, now)
            before[key] = old["value"] if old else None
            self._values[key] = value
        return {
            "changed": sorted(prepared),
            "unchanged": sorted(set(pairs) - set(prepared)),
            "before": before,
            "after": dict(prepared),
            "items": self.all_items(),
        }


config_service = ConfigService()
