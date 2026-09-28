# -*- coding: utf-8 -*-
"""E07 判定结果的进程内缓存（模块 05 §2.2）。

## 为什么按 `version` 做 key，而不是"主动失效"

```
_items: {doc_id: (version, record)}
```

权限变更必然 `version + 1`，于是旧 key **自然失效** —— 不需要任何"改权限时去清缓存"
的联动代码。这不是省事，而是**去掉了一整类 bug**：只要有一处漏了清缓存，
就会出现"改了权限但还是要等到重启才生效"，而这是本项目最核心的卖点（AD-02）。

## 两个必须写清的边界

- **"缓存里没有" ≠ "没有权限记录"**：只缓存**查到的**记录，
  查不到的 doc 不写缓存。否则"默认拒绝"会被固化成一条缓存的空记录 ——
  更要命的是，**后来给该文档加了权限记录，这条空记录会让它继续被拒绝到 TTL 过期**。
- **TTL 只是兜底**：默认 300s。正确性靠 `version`，TTL 唯一的作用是
  防止"长期不再访问的文档"把内存撑大（另有定时任务清理）。

> ⚠️ 本缓存与模块 01 的 `permission_cache` **不是一回事**：那个缓存的是
> "用户的**功能权限码**与角色"，属于"能不能用某功能"；本缓存的是
> "文档的**四维数据权限记录**"，属于"能不能读某篇知识"。
> 两套权限体系完全独立（原型 `08` 标注第 1 条），缓存也各管各的。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass
class PermCache:
    """按 `(doc_id, version)` 缓存 E07 记录。"""

    ttl: int = 300
    # doc_id -> (version, 过期时刻, 记录)。带上版本号是为了**可断言**：
    # 排查时能一眼看出"缓存里这条是第几版"，而不是只看到"有/没有"
    _items: dict[str, tuple[int, float, dict[str, Any]]] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    # ------------------------------------------------------------------ 读
    def get(self, doc_id: str) -> dict[str, Any] | None:
        """取缓存的权限记录；没有或已过期返回 `None`。

        **返回 `None` 一律表示"需要查库"**，绝不表示"该文档无权限"——
        后者必须由查库结果为空来回答（见模块头部的边界说明）。
        """
        entry = self._items.get(doc_id)
        if entry is None:
            self.misses += 1
            return None
        _, expires_at, record = entry
        if expires_at < time.monotonic():
            # 过期就删掉，避免"每次都命中一个已过期的 entry 再判一次"
            self._items.pop(doc_id, None)
            self.misses += 1
            return None
        self.hits += 1
        return record

    def get_many(self, doc_ids: Sequence[str]) -> tuple[dict[str, dict[str, Any]],
                                                        list[str]]:
        """批量取，返回 `(命中的记录, 需要查库的 doc_id)`。

        一次 `$in` 查询的**前提**是"知道哪些 doc 还没命中"——直接拿全量去查
        会让缓存在高频问答里完全不起作用（每次都查库，只是多了一次缓存写）。
        """
        found: dict[str, dict[str, Any]] = {}
        pending: list[str] = []
        for doc_id in dict.fromkeys(doc_ids):
            if not doc_id:
                continue
            record = self.get(doc_id)
            if record is None:
                pending.append(doc_id)
            else:
                found[doc_id] = record
        return found, pending

    # ------------------------------------------------------------------ 写
    def put(self, record: Mapping[str, Any]) -> None:
        """缓存一条记录（`version` 变了自然覆盖）。"""
        doc_id = str(record.get("doc_id") or "")
        if not doc_id:
            return
        self._items[doc_id] = (int(record.get("version") or 0),
                               time.monotonic() + self.ttl,
                               dict(record))

    def put_many(self, records: Sequence[Mapping[str, Any]]) -> None:
        for record in records:
            self.put(record)

    # ------------------------------------------------------------------ 维护
    def purge_expired(self) -> int:
        """清掉已过期的 entry（由 scheduler 定期调），返回清理条数。"""
        now = time.monotonic()
        stale = [k for k, (_, expires_at, _) in self._items.items()
                 if expires_at < now]
        for key in stale:
            self._items.pop(key, None)
        return len(stale)

    def invalidate(self, doc_id: str) -> None:
        """显式失效一篇文档（测试用；生产路径靠 version 自然失效）。"""
        self._items.pop(doc_id, None)

    def reset(self) -> None:
        """清空（测试隔离用：进程级单例会跨用例存活）。"""
        self._items.clear()
        self.hits = 0
        self.misses = 0

    def stats(self) -> dict[str, Any]:
        """`/health` 用的缓存概况。"""
        total = self.hits + self.misses
        return {"size": len(self._items), "ttl": self.ttl, "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total, 3) if total else 0.0}


perm_cache = PermCache()

__all__ = ["PermCache", "perm_cache"]
