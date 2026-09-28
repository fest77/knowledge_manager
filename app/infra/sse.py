# -*- coding: utf-8 -*-
"""SSE 任务总线（模块 00 的补全件，模块 06 使用）。

## 它解决的那个真实安全缺陷

存量的 SSE 实现"`task_id` 随机但**不校验订阅者身份**"——任何人拿到
`task_id`（日志、浏览器历史、同事截图）就能**窃听**别人的问答流。
本文件把"`task_id` → `user_id` 的绑定"做进总线，订阅时**先校验再建流**
（ER-14 / 模块 06 §4.5 / AC-06-07）。

```
POST /qa/ask        → hub.register(task_id, user_id)      ← 绑定
GET  /qa/stream/{id}→ hub.owner_of(task_id) != me → 403    ← 建流之前校验
                     → hub.subscribe(task_id) 才真正建流
```

## 为什么用内存而不是 Redis

单应用部署（AD-01）。放 Redis 会引入一个**新的外部依赖**，
而它解决的问题（多实例共享流）在本项目里不存在。
`task_id` 在 `done`/`error` 后保留 `qa_stream_ttl_seconds`（默认 5 分钟）——
这段时间足够前端把流读完，之后回收防止内存泄漏。

## 背压与"慢消费者"

每个订阅者一个 `asyncio.Queue`，`publish()` 是 `put_nowait`：
队列满就**丢弃这条事件并计数**，而不是阻塞生产者。
理由：生产者是问答链路本身，让它因为"某个前端卡住"而变慢是本末倒置；
而 `delta` 事件丢一条只会让那句话少几个字，`done`/`error` 这类关键事件
优先保证（队列容量给得足够大：1024）。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from app.core.logging import logger

# 每个订阅者的队列容量。1024 条 delta ≈ 几十 KB 文本，
# 正常情况下前端来不及消费才会积压到这个量级
QUEUE_SIZE = 1024
# 已发布事件的保留条数（供给迟到的订阅者回放）。见 `_Task.history`
HISTORY_SIZE = 4096


@dataclass
class _Task:
    """一条 SSE 任务的运行态。"""

    task_id: str
    user_id: str
    created_at: float
    subscribers: list[asyncio.Queue[dict[str, Any] | None]] = field(
        default_factory=list)
    # 已发布事件的历史（供"迟到的订阅者"回放，见 `subscribe` 的说明）。
    # 上限 `HISTORY_SIZE`：一轮问答的 delta 通常几十条，1024 足够覆盖完整答案
    history: list[dict[str, Any]] = field(default_factory=list)
    finished: bool = False
    finished_at: float = 0.0
    dropped: int = 0


class SseHub:
    """`task_id` → 订阅者队列 + 归属绑定（进程级单例）。"""

    def __init__(self) -> None:
        self._tasks: dict[str, _Task] = {}

    # ------------------------------------------------------------------ 生命周期
    def register(self, task_id: str, user_id: str) -> None:
        """注册任务并**绑定 `user_id`**（`POST /qa/ask` 时调用）。"""
        self._tasks[task_id] = _Task(task_id=task_id, user_id=user_id,
                                     created_at=time.monotonic())

    def owner_of(self, task_id: str) -> str | None:
        """取任务的所有者；任务不存在返回 `None`。

        ⚠️ 返回 `None` 的语义是"这个任务没了"（从未注册或已回收），
        **不是"任何人可订阅"**。路由层必须把 `None` 也当成拒绝——
        否则"猜一个已回收的 task_id"就成了绕过校验的方式。
        """
        task = self._tasks.get(task_id)
        return task.user_id if task else None

    def exists(self, task_id: str) -> bool:
        return task_id in self._tasks

    # ------------------------------------------------------------------ 生产 / 消费
    def publish(self, task_id: str, event: str, data: dict[str, Any]) -> int:
        """把一条事件推给该任务的所有订阅者，返回成功投递的订阅者数。

        任务已完成时**仍然允许投递**（`done`/`error` 之后可能还有收尾事件），
        但不再接受新的订阅（见 `subscribe`）。
        """
        task = self._tasks.get(task_id)
        if task is None:
            return 0
        payload = {"event": event, "data": data}
        # 同时留一份历史：**订阅者可能比事件晚到**。这不是理论问题——
        # "FAQ 直出"与"大模型立刻报错"两条路径都会在几毫秒内发完所有事件，
        # 而前端要等 `/qa/ask` 的响应回来才知道 `stream_url`，
        # 于是它订阅时事件已经发完了。没有历史回放的话，
        # 用户会看到"流建立了、然后什么也没有"（测试里表现为读到空事件列表）。
        task.history.append(payload)
        if len(task.history) > HISTORY_SIZE:
            del task.history[:len(task.history) - HISTORY_SIZE]
        delivered = 0
        for queue in list(task.subscribers):
            try:
                queue.put_nowait(payload)
                delivered += 1
            except asyncio.QueueFull:
                # 见模块头"背压"：丢事件而不是阻塞问答链路，但要计数留痕
                task.dropped += 1
                logger.warning("SSE 订阅者队列已满，丢弃一条 %s（task=%s 累计丢 %d）",
                               event, task_id, task.dropped)
        return delivered

    async def subscribe(self, task_id: str) -> AsyncIterator[dict[str, Any]]:
        """订阅事件流（`async for` 消费，直到 `done`/`error` 或任务回收）。

        调用方**必须先做过归属校验**（`owner_of`）。这里不做校验，
        因为总线不该知道"当前用户是谁"——那是路由层的上下文。
        """
        task = self._tasks.get(task_id)
        if task is None:
            return
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(QUEUE_SIZE)
        # **注册队列与快照历史长度之间不放任何 `await`**：否则这中间新发布的事件
        # 既进了队列又留在历史里，回放时会重复推给同一个订阅者
        task.subscribers.append(queue)
        replay = list(task.history)
        try:
            for item in replay:
                yield item
            if task.finished:
                # 任务已经结束：**绝不能再去等队列**。结束哨兵只推给"当时在场"的
                # 订阅者，晚到的订阅者等下去就是永久挂起（表现为前端一直转圈、
                # 测试进程卡死）。没历史时补一个 done，有历史时历史里本就带着 done。
                if not replay:
                    yield {"event": "done", "data": {"replayed": True}}
                return
            while True:
                item = await queue.get()
                if item is None:                            # 结束哨兵
                    return
                # 队列里的内容**不会**与回放的重复：注册队列与快照历史之间
                # 没有 await，别的协程插不进来（见上面的说明）
                yield item
                if item["event"] in ("done", "error"):
                    return
        finally:
            if queue in task.subscribers:
                task.subscribers.remove(queue)

    def finish(self, task_id: str) -> None:
        """标记任务结束并**唤醒所有订阅者**（它们会自然退出循环）。"""
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.finished = True
        task.finished_at = time.monotonic()
        for queue in list(task.subscribers):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:                       # pragma: no cover
                pass

    # ------------------------------------------------------------------ 维护
    def reap(self, ttl_seconds: int) -> int:
        """回收"已结束且超过 TTL"的任务，返回回收数（由 scheduler 定期调）。

        未结束的任务**不回收**：它可能是真的还在跑（长文档问答 + 慢模型），
        回收了会让前端拿到 403，而用户看到的却是"回答到一半消失了"。
        """
        now = time.monotonic()
        stale = [tid for tid, task in self._tasks.items()
                 if task.finished and now - task.finished_at > ttl_seconds]
        for task_id in stale:
            self._tasks.pop(task_id, None)
        if stale:
            logger.info("SSE 任务已回收 %d 条（剩余 %d）", len(stale),
                        len(self._tasks))
        return len(stale)

    def reset(self) -> None:
        """清空（测试隔离用）。"""
        self._tasks.clear()

    def stats(self) -> dict[str, Any]:
        """`/health` 用。"""
        return {"tasks": len(self._tasks),
                "subscribers": sum(len(t.subscribers) for t in self._tasks.values()),
                "dropped": sum(t.dropped for t in self._tasks.values())}


def format_sse(event: str, data: dict[str, Any]) -> str:
    """按 SSE 协议编码一帧（模块 06 的 `text/event-stream` 响应体）。

    `ensure_ascii=False` 是刻意的：中文答案走 `\\uXXXX` 转义会让报文膨胀 6 倍，
    而 SSE 本身是 UTF-8 文本协议，直接写中文没有任何问题。
    """
    import json

    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


sse_hub = SseHub()

__all__ = ["SseHub", "sse_hub", "format_sse", "QUEUE_SIZE"]
