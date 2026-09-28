# -*- coding: utf-8 -*-
"""进程内周期任务调度（模块 00 公共基础，模块 10 §4.2 用到）。

**为什么自己写 16 行而不是引 APScheduler**：项目约定"不引入未声明依赖"，
而这里的全部需求就是"每隔 N 秒跑一次协程、退出时干净收尾"。多引一个调度框架
意味着多一套配置、多一处生命周期、多一个演示现场可能出问题的组件。

三个刻意的设计：

| 设计 | 原因 |
|---|---|
| 任务异常**不让循环退出** | 一次 Mongo 抖动不能让审计回放从此停摆（那才是真的丢数据） |
| `run_on_start=True` 时先跑一次再睡 | 服务启动即回放，不必等第一个周期 |
| `stop()` 里 cancel + 吞掉 `CancelledError` | 否则退出时会抛一堆无意义的取消异常刷满日志 |
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from app.core.logging import logger


@dataclass(frozen=True, slots=True)
class Job:
    """一个周期任务的定义。`func` 必须**自己**把异常处理干净或允许被记录。

    `delay_first` 让任务的**第一次执行**晚于"立刻"——模块 07 的定时挖掘要落在
    `faq_mine_cron`（默认每天 03:00）而不是"服务启动那一刻"。
    没有它就只能在业务里再写一个"睡到 3 点"的循环，那等于把调度逻辑抄了第二遍。
    """

    name: str
    interval: float
    func: Callable[[], Awaitable[object]]
    run_on_start: bool = False
    delay_first: float = 0.0


class Scheduler:
    """极简周期任务调度器：一个 Job 一个 asyncio 任务。"""

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []
        self._jobs: list[Job] = []
        self._running = False

    @property
    def jobs(self) -> list[Job]:
        """已登记的任务（供 `/health` 或排障查看）。"""
        return list(self._jobs)

    @property
    def running(self) -> bool:
        """是否已启动。"""
        return self._running

    async def _loop(self, job: Job) -> None:
        if job.run_on_start:
            await self._once(job)
        elif job.delay_first > 0:
            # 首次对齐（如"每天 03:00"）：先等到那个时刻，之后按 interval 循环
            logger.info("周期任务 %s 将在 %.0f 秒后首次执行", job.name, job.delay_first)
            await asyncio.sleep(job.delay_first)
            await self._once(job)
        while True:
            await asyncio.sleep(job.interval)
            await self._once(job)

    async def _once(self, job: Job) -> None:
        try:
            await job.func()
        except asyncio.CancelledError:
            raise
        except Exception:                                  # noqa: BLE001
            logger.exception("周期任务 %s 执行失败（下一周期继续）", job.name)

    async def start(self, jobs: list[Job]) -> None:
        """登记并启动；重复调用只生效一次（幂等，便于测试反复进入 lifespan）。"""
        if self._running:
            return
        self._jobs = list(jobs)
        for job in self._jobs:
            self._tasks.append(asyncio.create_task(self._loop(job), name=f"job:{job.name}"))
        self._running = True
        logger.info("周期任务已启动：%s",
                    ", ".join(f"{j.name}/{j.interval:g}s" for j in self._jobs) or "（无）")

    async def stop(self) -> None:
        """取消全部任务并等待它们真正结束——不 await 会留下"幽灵任务"。"""
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):     # noqa: BLE001
                pass
        self._tasks.clear()
        self._running = False
        logger.info("周期任务已停止")


scheduler = Scheduler()

__all__ = ["Job", "Scheduler", "scheduler"]
