# -*- coding: utf-8 -*-
"""审计补偿文件（模块 10 §7.2 第 2 段，**DEC-10-4**）。

为什么是**本地 JSONL 文件**而不是内存队列：
审计写失败最常见的诱因恰恰是**进程重启与 Mongo 抖动**——而内存队列在这两种
情况下必丢。落盘才是补偿。文件与"将要入库的文档"**完全同构**，回放时零转换。

并发安全上用"**先改名再读**"而不是"先读再删"：
    1. `pending.jsonl` → `replayed/pending-{ts}.jsonl`（同分区 rename 是原子的）
    2. 读改名后的文件
    3. 回放成功就留着它当账；失败的条目重新追加进新的 `pending.jsonl`
这样在"读"与"清"之间到达的新记录绝不会被顺手删掉。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

from app.core.logging import logger

PENDING_NAME = "pending.jsonl"
REPLAYED_DIR = "replayed"


class AuditSpool:
    """补偿文件的读写门面。**只有 `AuditService` 可以写它**（§7.2 D-04）。"""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self._writable = False

    # ------------------------------------------------------------------ 路径
    @property
    def pending_path(self) -> Path:
        """当前待回放文件。"""
        return self.directory / PENDING_NAME

    @property
    def replayed_dir(self) -> Path:
        """回放成功后的归档目录。"""
        return self.directory / REPLAYED_DIR

    @property
    def writable(self) -> bool:
        """最近一次探测的结论；`/health` 用它在故障前就暴露问题。"""
        return self._writable

    def mark_unwritable(self) -> None:
        """把目录标记成不可写。

        由 `AuditService` 在**真实写失败**时调用：`prepare()` 只在启动时探一次，
        而磁盘写满、挂载被改只读都可能在运行中发生——那时必须让 `/health`
        立刻从 `ok` 变 `degraded`，否则"审计正在丢"会一直没人知道。
        """
        self._writable = False

    # ------------------------------------------------------------------ 准备
    def prepare(self) -> bool:
        """`mkdir -p` + **实际写一次探针**，把"目录在但只读"这类问题在启动期暴露。

        只看 `os.access` 是不够的：Windows 上的只读目录、容器里的只读挂载
        都可能骗过权限位检查。写一个字节再删掉，才是真的验证。
        """
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.replayed_dir.mkdir(parents=True, exist_ok=True)
            probe = self.directory / ".probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            self._writable = True
        except OSError as exc:
            self._writable = False
            logger.critical("审计补偿目录不可写：%s（%s）——审计降级将失去最后一道兜底",
                            self.directory, exc)
        return self._writable

    # ------------------------------------------------------------------ 写
    def append(self, doc: Mapping[str, Any]) -> Path:
        """追加一条 JSONL。**失败向上抛**，由服务层转成 `AUD-4003` 并记 CRITICAL。"""
        line = json.dumps(dict(doc), ensure_ascii=False, default=str, sort_keys=True)
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.pending_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())        # 掉电也不丢：补偿的意义就在这里
        self._writable = True
        return self.pending_path

    # ------------------------------------------------------------------ 读
    def claim(self) -> tuple[Path | None, list[dict[str, Any]]]:
        """原子"取走"待回放文件并解析。

        返回 `(归档路径, 文档列表)`；没有待回放文件时返回 `(None, [])`。
        坏行（人工编辑过 / 磁盘截断）跳过但**保留在归档文件里**，不会静默消失。
        """
        pending = self.pending_path
        if not pending.is_file():
            return None, []
        self.replayed_dir.mkdir(parents=True, exist_ok=True)
        # 用**纳秒**时间戳：秒级时间戳会让"启动回放"与随后的"周期回放"落在同一秒，
        # 第二次 rename 撞上已存在的归档文件（Windows 上直接 FileExistsError），
        # 于是回放被静默跳过——补偿文件堆着不动，看起来"系统正常"。
        target = self.replayed_dir / f"pending-{time.time_ns()}.jsonl"
        try:
            pending.rename(target)
        except OSError as exc:                     # 正被别的进程占用：本次不抢
            logger.warning("补偿文件改名失败，跳过本次回放：%s", exc)
            return None, []

        docs: list[dict[str, Any]] = []
        for lineno, raw in enumerate(target.read_text(encoding="utf-8").splitlines(), 1):
            text = raw.strip()
            if not text:
                continue
            try:
                docs.append(json.loads(text))
            except json.JSONDecodeError:
                logger.error("补偿文件第 %d 行不是合法 JSON，已跳过（文件保留在 %s）",
                             lineno, target)
        return target, docs

    def restore(self, docs: list[dict[str, Any]]) -> int:
        """把回放失败的条目**重新追加**回待回放文件（`spool_id` 保证不重复入库）。"""
        for doc in docs:
            self.append(doc)
        return len(docs)


__all__ = ["AuditSpool", "PENDING_NAME", "REPLAYED_DIR"]
