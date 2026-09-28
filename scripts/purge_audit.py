# -*- coding: utf-8 -*-
"""审计日志人工清理（模块 10 §2.5 的**唯一**例外通道）。

    .venv\\Scripts\\python.exe scripts\\purge_audit.py --before 2025-01-01 --yes

**为什么需要人工确认**：审计是**责任凭证**，保留策略是"永久"。
自动清理等于自毁证据链，所以这个脚本刻意做了三道刹车：

1. 必须显式给 `--before`（想删多少自己说清楚）；
2. 必须先写一条 `audit.export` 把待删范围**留档**（先留痕、后删除）；
3. 必须显式加 `--yes`，否则只做 dry-run 打印将要删除的条数。

删除动作本身走 `audit_repo.purge_before()`——审计集合的写入者只有那一个仓储，
ER-05 的静态检查因此仍然成立（脚本里没有 `delete_many`）。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import PROJECT_ROOT, settings          # noqa: E402
from app.core.logging import logger                         # noqa: E402
from app.infra.mongo import mongo                           # noqa: E402
from app.repositories import audit_repo                     # noqa: E402
from app.services.audit_service import audit_service        # noqa: E402

ARCHIVE_DIR = PROJECT_ROOT / "var" / "audit_archive"


def parse_args(argv: list[str]) -> dict:
    """极简参数解析：只有两个参数，用手写解析比引 argparse 更直观。"""
    opts = {"before": None, "yes": False}
    rest = list(argv)
    while rest:
        item = rest.pop(0)
        if item == "--before" and rest:
            opts["before"] = rest.pop(0)
        elif item == "--yes":
            opts["yes"] = True
    return opts


def cutoff_ms(before: str) -> int:
    """把 `YYYY-MM-DD` 解析成本地日 00:00 的毫秒时间戳。"""
    day = datetime.strptime(before, "%Y-%m-%d")
    return int(day.timestamp() * 1000)


async def archive(filters: dict) -> tuple[Path, int]:
    """导出待删记录到本地 JSONL 归档，返回 `(路径, 条数)`。

    归档是**删除的前置条件**：先有纸质副本，再销毁原件。
    """
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    target = ARCHIVE_DIR / f"audit_archive_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    cursor = mongo.collection(audit_repo.AUDIT_LOGS).find(filters)
    rows = await cursor.to_list(length=None)
    with target.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str, sort_keys=True))
            handle.write("\n")
    return target, len(rows)


async def purge(before: str, confirmed: bool) -> int:
    """主流程：统计 → 留痕 → 归档 → （确认后）删除。"""
    await mongo.connect()
    cutoff = cutoff_ms(before)
    filters = {"ts": {"$lt": cutoff}}
    total = await audit_repo.count(filters)
    logger.info("数据库：%s；将删除 %s 之前（ts < %d）的审计记录 %d 条",
                settings.mongo_db, before, cutoff, total)
    if total == 0:
        logger.info("没有需要删除的记录")
        await mongo.close()
        return 0

    archive_path, archived = await archive(filters)
    logger.info("已归档 %d 条到 %s", archived, archive_path)

    # 先留痕：把"删了哪些"这件事本身写进审计（R-15 的同源思路）
    await audit_service.record(
        "audit.export", actor="system", target_type="audit", target_id="-",
        after={"reason": "人工清理前的留档", "before": before, "rows": archived,
               "archive": archive_path.name},
        extra={"mode": "purge"})

    if not confirmed:
        logger.warning("未加 --yes，仅演练：什么都没删。确认无误后重跑并加上 --yes")
        await mongo.close()
        return 0

    deleted = await audit_repo.purge_before(cutoff)
    logger.warning("已删除 %d 条审计记录（归档保留在 %s）", deleted, archive_path)
    await mongo.close()
    return deleted


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    if not args["before"]:
        raise SystemExit("用法：python scripts/purge_audit.py --before YYYY-MM-DD [--yes]")
    asyncio.run(purge(args["before"], bool(args["yes"])))
