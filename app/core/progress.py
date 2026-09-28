# -*- coding: utf-8 -*-
"""长时间阻塞操作的**进度心跳**（模型加载 / 大目录扫描都用它）。

## 为什么必须有它

BGE-M3 的加载是**一次 15~40 秒的同步阻塞调用**（`SentenceTransformer(...)`）。
这中间除了 sentence-transformers 自己打的一行权重进度条，**一行输出都没有**：

- 在终端里看，这和"卡死"没有区别 —— 新人第一反应是 Ctrl+C 重来，
  而重来一次还是停在同一个地方（因为那就是它该花的时间）；
- 在导入流水线里看，是"任务停在 `embedding` 阶段不动"，运维无法区分
  "在加载模型"与"卡住了"；
- 在 `/health` 上看，是 `not_configured` —— 也看不出"正在加载"。

所以这里给这类调用配一个**心跳线程**：每 N 秒打一行"仍在加载（已用 X 秒）"，
阶段切换时立即打点，退出时打一行总耗时。

## 三条设计取舍

| 取舍 | 理由 |
|---|---|
| 心跳线程是 **daemon** | 主流程退出（异常、Ctrl+C）时不能因为它在 `join` 而挂住 |
| 进度显示**吞掉所有异常** | 显示坏了不能让业务跟着坏（与审计补偿同一取向） |
| 同时维护一份**进程内注册表** | `/health` 要能回答"现在是不是在加载、加载了多久"，而它不能去触发加载 |

## 用法

```python
with Progress("BGE-M3 加载", interval=5.0) as bar:
    bar.stage("导入运行时库")
    import torch                                  # 首次导入本身就要好几秒
    bar.stage("构造 SentenceTransformer")
    model = SentenceTransformer(path, device=device)
    bar.stage("首次前向自检")
# 退出时自动打一行「完成（共 X 秒）」

active_progress()        # {"BGE-M3 加载": 12.3}  —— `/health` 用
```
"""
from __future__ import annotations

import threading
import time
from typing import Any

from app.core.logging import logger

#: 正在进行的耗时操作：`label → 起始时刻`（`time.monotonic()`）。
#: `/health` 只读它，**绝不**通过它去触发加载。
_ACTIVE: dict[str, float] = {}
_ACTIVE_LOCK = threading.Lock()


def active_progress() -> dict[str, float]:
    """当前正在进行的耗时操作 `{label: 已用秒数}`（没有则为空字典）。"""
    now = time.monotonic()
    with _ACTIVE_LOCK:
        return {label: round(now - started, 1) for label, started in _ACTIVE.items()}


def is_busy(label_prefix: str = "") -> bool:
    """是否有耗时操作在进行（可按前缀过滤，如 `BGE-M3`）。"""
    with _ACTIVE_LOCK:
        return any(label.startswith(label_prefix) for label in _ACTIVE)


class Progress:
    """一次耗时操作的进度：阶段打点 + 定时心跳。

    `interval=0` 表示**不打心跳**（只保留阶段打点）——单测里用它避免后台线程。
    """

    def __init__(self, label: str, *, interval: float = 5.0,
                 hint: str = "") -> None:
        self.label = label
        self.hint = hint
        self.interval = max(0.0, float(interval))
        self._started = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stage = "启动"
        self._ok = True

    # ------------------------------------------------------------------ 读数
    @property
    def elapsed(self) -> float:
        """已用秒数（保留 1 位）。"""
        return round(time.monotonic() - self._started, 1)

    @property
    def stage(self) -> str:
        """当前阶段名。"""
        return self._stage

    # ------------------------------------------------------------------ 打点
    def stage_named(self, text: str) -> None:
        """切换阶段：立即打一行（带已用秒数），后续心跳也用这个阶段名。"""
        self._stage = text
        self._log(f"{text}（已用 {self.elapsed}s）")

    # 常用别名：`bar.stage("构造模型")` 会覆盖属性名，所以打点用 `step()`
    def step(self, text: str) -> None:
        """切换阶段（等价于 `stage_named`，用起来更顺手的名字）。"""
        self.stage_named(text)

    def note(self, text: str) -> None:
        """补充一行信息（不改变当前阶段）。"""
        self._log(f"{text}（已用 {self.elapsed}s）")

    # ------------------------------------------------------------------ 生命周期
    def __enter__(self) -> "Progress":
        with _ACTIVE_LOCK:
            _ACTIVE[self.label] = self._started
        suffix = f"；{self.hint}" if self.hint else ""
        self._log(f"开始加载{suffix}")
        if self.interval > 0:
            self._thread = threading.Thread(
                target=self._heartbeat, name=f"progress-{self.label}",
                daemon=True)
            self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        with _ACTIVE_LOCK:
            _ACTIVE.pop(self.label, None)
        if exc_type is None:
            self._log(f"完成（共 {self.elapsed}s）")
        else:
            self._ok = False
            # 失败也要有一行"到此为止花了多久"，否则日志里只剩一个异常栈
            self._log(f"失败：{exc_type.__name__}（已用 {self.elapsed}s）")
        return False                                          # 不吞异常

    # ------------------------------------------------------------------ 内部
    def _heartbeat(self) -> None:
        """每 `interval` 秒打一行"仍在进行"。`_stop.wait()` 让退出立即生效。"""
        while not self._stop.wait(self.interval):
            self._log(f"仍在进行：{self._stage}（已用 {self.elapsed}s）")

    def _log(self, text: str) -> None:
        """打一行进度。**任何异常都吞掉**：显示坏了不能让业务跟着坏。"""
        try:
            logger.info("[%s] %s", self.label, text)
        except Exception:                                     # noqa: BLE001
            pass


__all__ = ["Progress", "active_progress", "is_busy"]
