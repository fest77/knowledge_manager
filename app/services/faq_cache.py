# -*- coding: utf-8 -*-
"""FAQ 缓存（E18）：**进程内**向量缓存 + 可选持久化副本。

## 归属（ER-02）

| 角色 | 模块 | 说明 |
|---|---|---|
| **写** | **07** | `upsert` / `remove` / `rebuild` 只由 07 调 |
| **读** | **06** | 只调 `match()`（问答链路） |

06 的 `match()` 契约在模块 06 落地时就冻结了（`FaqEntry` / `FaqHit` /
`size` / `reset`），这里**只做增强，不改契约**——否则 06 的 19 条用例会一起红。

## 为什么按 version/代次做失效，而不是主动清

发布/编辑/停用后**立刻**调 `upsert` / `remove`（发布即生效，AC-07-09/13）。
不需要任何"等到下个周期"的机制：缓存就是数据本身（暴力比对，无索引）。

## 匹配用一次 BLAS 矩阵乘（Spec §2.3 C）

`N=5000, D=1024` 时矩阵乘 ≈ 5.1M 次乘加、单次 1~5ms，远优于 50ms 目标（AC-07-11）。
纯 Python 循环在 N=5000 时是**几十万次解释器迭代**，会直接踩破预算——
这是"必须用 numpy"的唯一理由，不是为了炫技。

## 三个容易写错的点

1. **`enabled=false` 的 FAQ 不进缓存**（集合层面过滤，不是匹配后过滤）：
   匹配后过滤会让"已停用"的条目占据 top1，把本该走 RAG 的问题判成"命中但不可用"。
2. **重建是原子替换**（AC-07-25）：先在旁边构建好新矩阵再整体换引用，
   中间没有"空缓存"窗口——否则重建期间的每一次问答都会漏掉缓存直出。
3. **`hit_count` 在内存累加、批量落库**（AC-07-17）：
   命中是高频读路径，逐次写库会把它从毫秒拖到十几毫秒。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.core.logging import logger
from app.services.config_service import config_service


@dataclass(slots=True)
class FaqEntry:
    """一条已发布 FAQ 在缓存里的形态。"""

    faq_id: str
    question: str
    answer: str
    vector: Sequence[float] = field(default_factory=tuple)
    aliases: Sequence[str] = field(default_factory=tuple)
    version: int = 1
    published_at: int = 0

    def as_row(self) -> dict[str, Any]:
        """缓存副本（E18.B）的一行 —— **不含 `answer`**（真源在 `faqs`）。"""
        return {"_id": self.faq_id, "faq_id": self.faq_id,
                "question": self.question, "aliases": list(self.aliases),
                "embedding": [float(x) for x in self.vector], "enabled": True}


@dataclass(slots=True)
class FaqHit:
    """命中结果（06 直接用这两个字段）。"""

    entry: FaqEntry
    score: float


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度（纯 Python 版，供单测与小规模场景；零向量返回 0 而不是 NaN）。

    返回 NaN 的后果很隐蔽：`NaN >= 0.92` 是 `False`（看起来"没命中"），
    但一旦有人写成 `max(...)` 比较，NaN 会污染整个排序。
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


class FaqCache:
    """进程内 FAQ 缓存（单例 `faq_cache`）。"""

    def __init__(self) -> None:
        self._items: list[FaqEntry] = []
        # (N, D) float32 矩阵；与 `_items` 严格同步（`_rebuild_matrix` 是唯一写点）
        self._matrix: Any = None
        self._norms: Any = None
        self._generation: int = 0
        self._rebuilding: bool = False
        self._lock = asyncio.Lock()
        # faq_id -> 未落库的命中次数（AC-07-17）
        self._hit_buffer: dict[str, int] = {}
        # 观测：最近一次匹配耗时（AC-07-11 的 `match_p95_ms` 用样本近似）
        self.match_samples: list[float] = []

    # ------------------------------------------------------------------ 读（06）
    @property
    def size(self) -> int:
        return len(self._items)

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def rebuilding(self) -> bool:
        """是否正在重建（并发调用 `POST /faq/cache/rebuild` 要返 `FAQ-3008`）。"""
        return self._rebuilding

    @property
    def entries(self) -> list[FaqEntry]:
        return list(self._items)

    def match(self, vector: Sequence[float]) -> FaqHit | None:
        """按问题向量匹配；低于阈值返回 `None`（G-03：宁可不命中也不给错答案）。

        **不做任何 IO**：整个匹配是纯内存计算，所以它能在毫秒级完成。
        """
        if not vector or not self._items:
            return None
        if not config_service.faq_cache_enabled:
            # 全局降级开关（Spec §7.2）：关掉后一律未命中 → 06 走 RAG
            return None
        started = time.perf_counter()
        threshold = config_service.faq_cache_sim_threshold
        if self._matrix is None:
            best = self._match_python(vector, threshold)
        else:
            best = self._match_matrix(vector, threshold)
        self.match_samples.append((time.perf_counter() - started) * 1000)
        if len(self.match_samples) > 500:
            del self.match_samples[:-500]
        if best is not None:
            # 命中计数先在内存累加（见模块头 ③）
            self._hit_buffer[best.entry.faq_id] = \
                self._hit_buffer.get(best.entry.faq_id, 0) + 1
        return best

    def _match_matrix(self, vector: Sequence[float], threshold: float
                      ) -> FaqHit | None:
        """一次 BLAS 矩阵乘算出全部余弦（Spec §2.3 C 的实现）。"""
        import numpy as np

        query = np.asarray(vector, dtype="float32")
        query_norm = float(np.linalg.norm(query))
        if query_norm <= 0:
            return None
        scores = (self._matrix @ query) / (self._norms * query_norm + 1e-12)
        index = int(scores.argmax())
        score = float(scores[index])
        if score < threshold:
            return None
        return FaqHit(entry=self._items[index], score=score)

    def _match_python(self, vector: Sequence[float], threshold: float
                      ) -> FaqHit | None:
        """numpy 不可用时的兜底（规模小时结果一致）。"""
        best: FaqHit | None = None
        for entry in self._items:
            score = cosine(vector, entry.vector)
            if score >= threshold and (best is None or score > best.score):
                best = FaqHit(entry=entry, score=score)
        return best

    def match_p95_ms(self) -> float:
        """最近的匹配耗时 P95（`/faq/cache/status` 上报，AC-07-11）。"""
        if not self.match_samples:
            return 0.0
        ordered = sorted(self.match_samples)
        index = max(0, int(len(ordered) * 0.95) - 1)
        return round(ordered[index], 3)

    # ------------------------------------------------------------------ 写（07）
    def _rebuild_matrix(self) -> None:
        """按 `_items` 重建矩阵与范数（**唯一的矩阵写点**）。"""
        if not self._items:
            self._matrix = None
            self._norms = None
            return
        try:
            import numpy as np

            matrix = np.asarray([list(e.vector) for e in self._items],
                                dtype="float32")
            self._matrix = matrix
            self._norms = np.linalg.norm(matrix, axis=1)
        except Exception as exc:                            # noqa: BLE001
            logger.warning("FAQ 缓存矩阵构建失败，退回纯 Python 匹配：%s", exc)
            self._matrix = None
            self._norms = None

    def _replace_items(self, items: Iterable[FaqEntry]) -> int:
        """**原子替换**条目集合（AC-07-25：中途没有空缓存窗口）。"""
        new_items = list(items)
        self._items = new_items
        self._rebuild_matrix()
        self._generation += 1
        return len(new_items)

    def replace_all(self, entries: Sequence[FaqEntry]) -> int:
        """整批替换（重建 / 07 全量刷新）。"""
        count = self._replace_items(entries)
        logger.info("FAQ 缓存已重建：%d 条（代次 %d）", count, self._generation)
        return count

    def upsert(self, entry: FaqEntry) -> None:
        """新增 / 更新单条（发布或编辑后立刻调用 → 发布即生效）。"""
        items = [e for e in self._items if e.faq_id != entry.faq_id]
        items.append(entry)
        self._replace_items(items)

    def remove(self, faq_id: str) -> bool:
        """移除一条（停用 / 删除 / 准入校验失败 → 立刻从缓存消失，AC-07-13）。"""
        items = [e for e in self._items if e.faq_id != faq_id]
        if len(items) == len(self._items):
            return False
        self._replace_items(items)
        self._hit_buffer.pop(faq_id, None)
        return True

    # ------------------------------------------------------------------ 命中计数
    def drain_hit_counts(self) -> dict[str, int]:
        """取出并清空命中计数缓冲（由 scheduler 定期落库，AC-07-17）。"""
        if not self._hit_buffer:
            return {}
        drained = dict(self._hit_buffer)
        self._hit_buffer.clear()
        return drained

    def pending_hit_counts(self) -> int:
        """未落库的命中次数合计（`/health` 观测用）。"""
        return sum(self._hit_buffer.values())

    # ------------------------------------------------------------------ 重建锁
    def begin_rebuild(self) -> bool:
        """尝试进入重建态；已在重建返回 `False`（→ `FAQ-3008`）。"""
        if self._rebuilding:
            return False
        self._rebuilding = True
        return True

    def end_rebuild(self) -> None:
        self._rebuilding = False

    @property
    def lock(self) -> asyncio.Lock:
        """重建/写入互斥锁（Spec §2.3 的 `_lock`）。"""
        return self._lock

    # ------------------------------------------------------------------ 维护
    def reset(self) -> None:
        """清空（测试隔离用）。"""
        self._items = []
        self._matrix = None
        self._norms = None
        self._generation += 1
        self._hit_buffer.clear()
        self.match_samples.clear()
        self._rebuilding = False

    def stats(self) -> dict[str, Any]:
        """`/health` 用。"""
        return {"size": len(self._items), "generation": self._generation,
                "threshold": config_service.faq_cache_sim_threshold,
                "enabled": config_service.faq_cache_enabled,
                "pending_hits": self.pending_hit_counts(),
                "match_p95_ms": self.match_p95_ms()}


faq_cache = FaqCache()

__all__ = ["FaqCache", "FaqEntry", "FaqHit", "faq_cache", "cosine"]
