# -*- coding: utf-8 -*-
"""knowledge_manager · 应用装配。

启动：
    .venv\\Scripts\\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8102
    （或直接跑 .\\scripts\\run_server.ps1，它会先清掉占端口的残留进程）
前端：http://127.0.0.1:8102/ui/
接口文档：http://127.0.0.1:8102/docs
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import (routes_audit, routes_auth, routes_docs, routes_faq,
                     routes_gap, routes_health, routes_import, routes_metric,
                     routes_org,
                     routes_perm, routes_qa, routes_system)
from app.api.deps import enforce_perm
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.logging import logger
from app.infra.milvus import milvus
from app.infra.minio import minio_store
from app.infra.mongo import mongo
from app.infra.scheduler import Job, scheduler
from app.middleware import jwt_auth
from app.repositories import (audit_repo, auth_repo, config_repo, faq_repo,
                              gap_repo, metric_repo, org_repo, perm_repo, qa_repo)
from app.services.audit_service import audit_service
from app.services.config_service import config_service
from app.services.import_service import import_service
from app.services.faq_service import faq_service
from app.services.gap_service import gap_service
from app.services.metric_service import metric_service
from app.services.perm_cache import perm_cache
from app.services.qa_service import qa_service


async def _flush_faq_hits() -> None:
    """把 FAQ 命中计数批量落库（模块 07 §3.10 / AC-07-17）。

    周期取 `faq.hit_flush_interval_s`（默认 5s）：命中是高频读路径，
    逐次写库会把它从毫秒拖到十几毫秒，而 5 秒的延迟对"命中次数"这种统计量毫无影响。
    """
    written = await faq_service.flush_hit_counts()
    if written:
        logger.info("FAQ 命中计数已落库：%d 条", written)


async def _scheduled_metric_rollup() -> None:
    """把 1m 桶汇总到 1h、1h 汇总到 1d（模块 09 §4.1）。

    汇总用 $set 覆盖而不是 $inc，所以**重跑幂等**——
    这也是"下一次运行会修复上一次失败"的依据（MET-5002 的语义）。
    """
    try:
        result = await metric_service.rollup()
        if result.get("hourly") or result.get("daily"):
            logger.info("指标桶已汇总：%s", result)
    except Exception as exc:                                # noqa: BLE001
        logger.error("指标桶汇总失败（下一轮幂等重算会修复）：%s", exc)


async def _scheduled_gap_aggregate() -> None:
    """定时缺口聚合（模块 08 §4.3 / AC-08-19）。

    与手动聚合**共用单飞锁**：正在跑时手动触发会得到 `GAP-3005`，反之亦然。
    幂等由三重保证兜住（`$set` 重算覆盖 + `normalized_key` 唯一索引 + 单飞锁），
    所以"多跑几轮"不会让频次翻倍。
    """
    try:
        result = await gap_service.aggregate(actor="scheduler")
        logger.info("定时缺口聚合完成：扫描 %d 日志 → 识别 %d 缺口 → 清理 %d",
                    result.scanned_logs, result.identified, result.removed)
    except Exception as exc:                                # noqa: BLE001
        logger.error("定时缺口聚合失败（下一周期继续）：%s", exc)


async def _scheduled_faq_mine() -> None:
    """定时挖掘（模块 07 §3.4 / AC-07-31）。

    与手动挖掘**共用同一个互斥标志**：`faq_service.mine()` 内部会判 `_mining`，
    所以"定时任务正在跑"时手动触发会得到 `FAQ-3007`，反之亦然。
    """
    try:
        result = await faq_service.mine(actor="scheduler")
        logger.info("定时 FAQ 挖掘完成：扫描 %d 日志 → 新建 %d / 更新 %d",
                    result.scanned_logs, result.candidates_created,
                    result.candidates_updated)
    except Exception as exc:                                # noqa: BLE001
        # 定时任务失败只记日志：下一周期会再试（与审计回放同一条原则）
        logger.error("定时 FAQ 挖掘失败（下一周期继续）：%s", exc)


def _seconds_until_cron(cron: str) -> float:
    """解析「分 时 * * *」形态的每日计划，算出距离下次执行还有多少秒。

    **只支持每日形态**（Spec §2.4 的默认值就是 `0 3 * * *`）。
    支持完整 cron 表达式需要引入依赖或自己写一套解析器，而本项目的需求
    只有"每天凌晨跑一次、错开业务高峰"——所以遇到不认识的形态就返回 0
    （等于"立即执行一次然后每 24 小时一次"），并记一条 WARN。
    """
    import time as _time

    parts = (cron or "").split()
    if len(parts) != 5 or parts[2:] != ["*", "*", "*"]:
        logger.warning("faq.mine_cron=%r 不是「分 时 * * *」形态，"
                       "将退化为「启动后立即执行、之后每 24 小时一次」", cron)
        return 0.0
    try:
        minute, hour = int(parts[0]), int(parts[1])
    except ValueError:
        logger.warning("faq.mine_cron=%r 的分/时不是整数，退化为立即执行", cron)
        return 0.0
    now = _time.localtime()
    target = _time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hour, minute, 0,
                           now.tm_wday, now.tm_yday, -1))
    if target <= _time.time():
        target += 86400
    return max(0.0, target - _time.time())


async def _purge_perm_cache() -> None:
    """定期清掉过期的权限判定缓存（模块 05 §2.2 的兜底）。"""
    purged = perm_cache.purge_expired()
    if purged:
        logger.info("权限判定缓存已清理 %d 条过期记录（当前 %s）", purged,
                    perm_cache.stats())


async def _reap_sse_tasks() -> None:
    """回收已结束的 SSE 任务（模块 06 §4.5：`done`/`error` 后 5 分钟）。

    不回收的后果是内存随问答量线性增长——每个任务都留着队列与绑定信息，
    而这个泄漏**没有任何外部症状**，只在跑几百轮之后表现为"服务越来越慢"。
    """
    reaped = qa_service.reap()
    if reaped:
        logger.info("SSE 任务已回收 %d 条", reaped)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动期一律 fail-fast：连不上库、唯一索引建不起来、预置配置写不进去，就别启动。

    审计（模块 10）是唯一的例外：它的补偿目录不可写**不阻止启动**，
    而是把 `audit_degraded` 暴露到 `/health`——审计可以降级，业务不能停。
    """
    await mongo.connect()
    # Milvus / MinIO 是**导入链路的依赖，不是全局硬依赖**（Spec §7.2）：
    # 连不上时只让"导入"不可用，台账维护与审计照常。所以这里不 fail-fast，
    # 只把失败记成 ERROR——`/health` 会如实反映它们的状态。
    #
    # ⚠️ 这两个 connect 曾经**漏在 lifespan 之外**，症状很隐蔽：
    # `chunk_store.set_chunks_enabled()` 每次都走"Milvus 未连接 → 返回 0 + WARN"，
    # 于是"停用文档"看起来成功了，切片却仍然可召回（最危险的越权形态）；
    # 而模块 04 的启动补偿会直接抛 `MilvusUnavailable`，服务根本起不来。
    try:
        await milvus.connect()
    except Exception as exc:                                  # noqa: BLE001
        logger.error("Milvus 连接失败，导入与切片启停将不可用：%s", exc)
    try:
        await minio_store.connect()
    except Exception as exc:                                  # noqa: BLE001
        logger.error("MinIO 连接失败，导入将降级为本地存储：%s", exc)
    await org_repo.ensure_indexes()          # E08~E11（归属 02）
    await auth_repo.ensure_indexes()         # E12/E13（归属 01）
    await config_repo.ensure_indexes()
    await audit_repo.ensure_indexes()
    # E07 的唯一索引 `doc_id` 是**鉴权正确性的前提**：没有它，
    # 同一文档可能出现两条权限记录，"以哪条为准"就说不清了。
    # 与 03 的 `file_hash` 唯一索引同理，缺了就 fail-fast（不启动）。
    await perm_repo.ensure_indexes()
    # E14/E02/E15（模块 06 的三张表）。`qa_logs.request_id` 是**稀疏唯一索引**：
    # 没有它，幂等键就只是"应用层记着"，重启后同一 request_id 会重复执行
    await qa_repo.ensure_indexes()
    # E16/E17/E18 副本（模块 07）。两个唯一索引各有职责：
    # `cluster_key + window_start` 防同窗重复生成，`question_norm` 防重复发布
    await faq_repo.ensure_indexes()
    # E19（模块 08）：`normalized_key` 唯一索引是"合并计数"的基石
    await gap_repo.ensure_indexes()
    # E20（模块 09）：看板主查询索引 + 1m/1h 的部分 TTL
    await metric_repo.ensure_indexes()
    await config_service.bootstrap()
    await audit_service.startup()
    # 启动即回放一次补偿文件，不必等第一个周期
    await audit_service.replay_spool()
    # 导入链路（模块 04）：建 E06 索引 + 确保 E01 集合存在 + **启动补偿**
    # （把上次进程遗留的 pending/running 任务改判为 failed/IMP-5003）
    startup = await import_service.startup()
    if startup["interrupted_tasks"]:
        logger.warning("导入启动补偿：%d 条中断任务已改判失败，需管理员显式重试",
                       startup["interrupted_tasks"])
    # 模块 07：启动即从 `faqs` 全量重建 FAQ 缓存（AC-07-14）。
    # 重建失败**不阻止启动**（问答仍可用，只是没有 FAQ 直出）
    faq_startup = await faq_service.startup()
    logger.info("FAQ 缓存就绪：%d 条（启动重建成功=%s）", faq_startup["cache_size"],
                faq_startup["rebuilt"])
    await scheduler.start([
        Job("audit_spool_replay", float(settings.audit_replay_interval_seconds),
            audit_service.replay_spool),
        # 导入超时看门狗（Spec §4.5）：心跳超 `import.timeout_min` 的 running 任务
        # 改判 timeout。周期取 60s——它是兜底手段，不需要秒级反应，
        # 而过密的扫描反而会与正常导入的阶段写入抢同一个集合
        Job("import_watchdog", 60.0, import_service.watchdog),
        # 判定缓存的过期清理（模块 05 §2.2）：TTL 只是兜底防内存膨胀，
        # 正确性靠 `version` 自然失效。周期取 300s 与 TTL 同量级——
        # 太密是白扫，太疏则"很久没被访问的文档"会一直占着内存
        Job("perm_cache_purge", 300.0, _purge_perm_cache),
        # SSE 任务回收（模块 06 §4.5）：周期取 60s，比 TTL（300s）密得多，
        # 保证"任务结束后最多 1 分钟就被扫到"，而不是等一个 300s 的周期
        Job("sse_task_reap", 60.0, _reap_sse_tasks),
        # 模块 07：FAQ 命中计数批量落库（AC-07-17）+ 定时挖掘（AC-07-31）。
        # 挖掘用 `delay_first` 对齐到 `faq.mine_cron`（默认每天 03:00），
        # 而不是"服务启动那一刻"——那会在重启频繁的开发机上反复跑全量挖掘
        Job("faq_hit_flush", float(config_service.faq_hit_flush_interval_s),
            _flush_faq_hits),
        Job("faq_mine_job", 86400.0, _scheduled_faq_mine,
            delay_first=_seconds_until_cron(config_service.faq_mine_cron)),
        # 模块 08：缺口聚合（AC-08-19 —— 启动后 60s 首轮，之后按配置周期）。
        # 首轮延时 60s 而不是 0：启动瞬间 Mongo 刚连上、索引刚建，
        # 立刻跑全量聚合会与初始化抢资源
        Job("gap_aggregate", float(config_service.gap_aggregate_interval_min * 60),
            _scheduled_gap_aggregate, delay_first=60.0),
        # 模块 09：指标桶汇总（1m→1h→1d）。每 10 分钟一次，`$set` 覆盖天然幂等，
        # 所以"多跑几轮"不会让数字翻倍（AC-09-16）。
        # `run_on_start=True` 是 Spec §4.2 的"启动时补算近 6 小时"：进程重启后
        # 这段时间的 `1h` 桶还空着，等 10 分钟才补会让刚起的看板显示空图
        Job("metric_rollup", 600.0, _scheduled_metric_rollup, run_on_start=True),
    ])
    logger.info("%s v%s 已就绪（db=%s）", settings.app_name, settings.version, settings.mongo_db)
    try:
        yield
    finally:
        await scheduler.stop()
        # 先停导入（取消在跑任务，让它们走 `failed` 分支）再断库：
        # 反过来的话，流水线会在"库已关"之后继续写进度，
        # 每个阶段都抛异常，日志里一堆噪声却说不清哪条是真问题
        await import_service.shutdown()
        # 先停问答（取消在跑的链路并唤醒订阅者），再断库：
        # 反过来的话前端会永远等不到 done，而日志里只有一堆"库已关"的噪声
        await qa_service.shutdown()
        await mongo.close()
        await milvus.close()
        await minio_store.close()


def create_app() -> FastAPI:
    """装配应用：异常处理 → 中间件 → 路由 → 静态前端（顺序不可调换）。

    `dependencies=[Depends(enforce_perm)]` 是**全局功能权限拦截**（ER-09）：
    挂在 FastAPI 上就不必逐个路由声明，「忘了加」这类漏网之鱼从结构上被消除。
    """
    app = FastAPI(
        title="knowledge_manager",
        version=settings.version,
        description="知识库管理平台（2.9）· 导入解析 / 向量检索 / 四维权限 / AI 鉴权问答",
        lifespan=lifespan,
        dependencies=[Depends(enforce_perm)],
    )

    # CORS 仅为本机前端联调；部署时应收紧到具体来源
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:8102", "http://localhost:8102"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["X-Trace-Id"],
    )

    register_exception_handlers(app)
    jwt_auth.install(app)                    # 必须在路由注册前安装

    app.include_router(routes_health.router)
    app.include_router(routes_auth.router)
    app.include_router(routes_system.router)
    app.include_router(routes_docs.router)
    # 模块 04：`/import/*`。**必须在 docs 之后 include**——
    # 它的 `/import/files/{object_key:path}` 是贪婪路径，跨路由不会抢匹配，
    # 但同一 router 内的顺序仍有要求（见 routes_import 头部说明）
    app.include_router(routes_import.router)
    # 模块 05：`/perm/*`（判定 + 四维配置）
    app.include_router(routes_perm.router)
    # 模块 06：`/qa/*`（提问 / SSE 流 / 会话历史 / 反馈）
    app.include_router(routes_qa.router)
    # 模块 07：`/faq/*` 与 `/faqs/*`（候选审核 / 挖掘 / 已发布 FAQ / 缓存运维）
    app.include_router(routes_faq.router)
    # 模块 08：`/gaps/*`（清单 / 详情 / 转建 / 忽略 / 聚合 / 导出）
    app.include_router(routes_gap.router)
    # 模块 09：/metrics/*（看板四查询 + 导出）
    app.include_router(routes_metric.router)
    app.include_router(routes_org.router)
    # 路径在 /org 下、但归属模块是 01（E13 的唯一写入者）
    app.include_router(routes_auth.org_perm_router)
    app.include_router(routes_audit.router)

    if settings.web_dir.is_dir():
        # 挂在 /ui 而不是 /：这样白名单只需放行一个固定前缀，
        # 不会因为放行 "/" 把整个接口面暴露出去
        app.mount("/ui", StaticFiles(directory=settings.web_dir, html=True), name="ui")
    else:
        logger.warning("前端目录不存在：%s（仅接口可用）", settings.web_dir)

    return app


app = create_app()
