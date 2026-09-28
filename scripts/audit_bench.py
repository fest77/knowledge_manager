# -*- coding: utf-8 -*-
"""审计性能压测（模块 10 §7.4 / AC-10-24）。

    .venv\\Scripts\\python.exe scripts\\audit_bench.py --db kb001_bench --rows 100000

量三个指标：

| 环节 | 预算 | 说明 |
|---|---|---|
| `record()` 单次 | P95 < 5ms | 1 次 insert + 内存脱敏；这是业务侧的真实代价 |
| `/audit/logs` 四维筛选 | P95 < 200ms | 10 万条量级，依赖 `ts desc` 与 `action + ts desc` |
| 导出行数 / 内存峰值 | 内存峰值 < 100MB | 游标流式 + `batch_size` |

**安全护栏**：库名必须带 `_bench` 后缀（或用 `--allow-prod` 显式覆盖），
否则脚本直接退出——压测会往库里灌 10 万条数据，绝不能在 `kb001` 上误跑。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Windows 控制台默认 GBK，✔/✘ 与中文都会抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8")


def parse_args(argv: list[str]) -> dict:
    """解析 `--db` / `--rows` / `--record-n` / `--allow-prod`。"""
    opts = {"db": "kb001_bench", "rows": 100000, "record_n": 300, "allow_prod": False,
            "clean": False}
    rest = list(argv)
    while rest:
        item = rest.pop(0)
        if item == "--db" and rest:
            opts["db"] = rest.pop(0)
        elif item == "--rows" and rest:
            opts["rows"] = int(rest.pop(0))
        elif item == "--record-n" and rest:
            opts["record_n"] = int(rest.pop(0))
        elif item == "--clean":
            opts["clean"] = True
        elif item == "--allow-prod":
            opts["allow_prod"] = True
    return opts


def percentile(samples: list[float], ratio: float) -> float:
    """取分位数（样本已升序）。"""
    if not samples:
        return 0.0
    index = max(0, min(len(samples) - 1, int(round(len(samples) * ratio)) - 1))
    return samples[index]


async def run(opts: dict) -> None:
    """灌数据 → 量写入 → 量查询 → 量导出。"""
    from app.core.logging import logger
    from app.infra.mongo import mongo
    from app.repositories import audit_repo
    from app.services.audit_service import audit_service

    await mongo.connect()
    db = mongo.require_db()
    coll = db[audit_repo.AUDIT_LOGS]
    if opts["clean"]:
        await coll.delete_many({})
    await audit_repo.ensure_indexes()

    existing = await coll.count_documents({})
    need = max(0, opts["rows"] - existing)
    print(f"[准备] 目标 {opts['rows']} 条，现有 {existing} 条，本次灌入 {need} 条")
    if need:
        await seed_rows(coll, existing, need)

    # 与 lifespan 一致：准备补偿目录 + 把当日编号序列对齐到库里最大值。
    # 少了这一步，重启后的头几条会挨个撞号（不影响正确性，但延迟会翻几十倍）。
    await audit_service.startup()

    print("\n=== 1. record() 写入延迟 ===")
    samples: list[float] = []
    for i in range(opts["record_n"]):
        started = time.perf_counter()
        await audit_service.record("doc.update", actor="U000001", target_type="doc",
                                   target_id=f"DOC{i:06d}",
                                   before={"title": "旧标题"}, after={"title": "新标题"},
                                   reason="压测：写延迟")
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    print(f"  样本 {len(samples)}　P50 {percentile(samples, .5):.2f}ms　"
          f"P95 {percentile(samples, .95):.2f}ms　max {samples[-1]:.2f}ms")
    print(f"  预算 P95 < 5ms → {'达标 ✔' if percentile(samples, .95) < 5 else '超预算 ✘'}")

    print("\n=== 2. 四维筛选查询延迟（10 万条量级）===")
    await measure_queries(audit_service)

    print("\n=== 3. 导出流式与内存峰值 ===")
    await measure_export(audit_service, coll)

    total = await coll.count_documents({})
    print(f"\n[结束] 库中现有 {total} 条审计记录（跑完请自行 drop：db.{audit_repo.AUDIT_LOGS}"
          ".drop()）")
    logger.info("压测完成，库=%s 条数=%d", opts["db"], total)
    await mongo.close()


async def seed_rows(coll, offset: int, count: int) -> None:
    """批量灌数据。

    编号**仍然遵守 `LOG{yyyyMMdd}{4位序列}` 格式**（而不是造一堆假 _id）：
    这样压测数据与真实数据同形，`/audit/logs/{id}` 详情、`_sync_sequence()` 的
    序列对齐逻辑在压测库上也能被顺带验证。为此把时间戳按 10 秒间隔铺开
    （10 万条约 11.6 天，单日 ~8600 条 < 9999），避免 4 位序列溢出。
    """
    actions = ["doc.create", "doc.update", "doc.toggle", "doc.permission_change",
               "role.grant", "user.role_change", "config.update", "auth.login_fail"]
    actors = ["U000001", "U000002", "U000003", "system"]
    batch: list[dict] = []
    now = int(time.time() * 1000)
    counters: dict[str, int] = {}
    for i in range(count):
        ts = now - (count - i) * 10_000
        day = time.strftime("%Y%m%d", time.localtime(ts / 1000))
        counters[day] = counters.get(day, 0) + 1
        batch.append({
            "_id": f"LOG{day}{counters[day]:04d}", "ts": ts,
            "actor": actors[i % len(actors)], "actor_name": f"用户{i % 4}",
            "actor_role": "sys_admin" if i % 4 == 0 else "kb_admin",
            "action": actions[i % len(actions)], "target_type": "doc",
            "target_id": f"DOC{i % 5000:06d}", "target_name": f"制度文件 {i % 500}",
            "before": {"status": "enabled"}, "after": {"status": "disabled"},
            "changed_fields": ["status"], "redacted_keys": [],
            "snapshot_state": "diff", "reason": "压测数据", "outcome": "success",
            "ip": "10.0.0.1", "ua": "bench", "trace_id": f"bench{i:08x}",
        })
        if len(batch) >= 2000:
            await _flush(coll, batch)
            batch.clear()
    if batch:
        await _flush(coll, batch)
    print(f"  已灌入 {count} 条（编号同真：LOG+日期+4位序列）")


async def _flush(coll, batch: list[dict]) -> None:
    """分批写入，忽略主键冲突（重复跑压测时不该因为已有数据而中断）。"""
    try:
        await coll.insert_many(batch, ordered=False)
    except Exception as exc:                                  # noqa: BLE001
        details = getattr(exc, "details", None) or {}
        written = details.get("nInserted", 0)
        print(f"  部分批次未写入（已存在）：成功 {written}/{len(batch)}")


async def measure_queries(audit_service) -> None:
    """量四维筛选：单维 + 组合，各 10 次取 P95。"""
    from app.infra.mongo import mongo
    from app.repositories import audit_repo

    now = int(time.time() * 1000)
    cases = {
        "时间范围（近 1 天）": {"ts": {"$gte": now - 86400 * 1000}},
        "按动作": {"action": "doc.permission_change", "ts": {"$gte": now - 7 * 86400 * 1000}},
        "按操作人": {"actor": "U000002", "ts": {"$gte": now - 7 * 86400 * 1000}},
        "按目标": {"target_type": "doc", "target_id": "DOC000123"},
        "动作+操作人+时间": {"action": {"$in": ["role.grant", "config.update"]},
                            "actor": "U000001", "ts": {"$gte": now - 30 * 86400 * 1000}},
    }
    for label, flt in cases.items():
        samples = []
        for _ in range(10):
            started = time.perf_counter()
            await audit_service.query(flt, 1, 20)
            samples.append((time.perf_counter() - started) * 1000)
        samples.sort()
        p95 = percentile(samples, .95)
        total = await mongo.collection(audit_repo.AUDIT_LOGS).count_documents(flt)
        flag = "达标 ✔" if p95 < 200 else "超预算 ✘"
        print(f"  {label:<18} 命中 {total:>7} 条　P95 {p95:>7.2f}ms　{flag}")


async def measure_export(audit_service, coll) -> None:
    """量导出：流式消费全部行，用 `tracemalloc` 记录内存峰值。"""
    from app.core.config import settings

    filters = {"ts": {"$gte": 0}}
    rows = await coll.count_documents(filters)
    tracemalloc.start()
    started = time.perf_counter()
    produced = 0
    size = 0
    async for chunk in audit_service.export_stream(filters, "csv", rows=rows):
        produced += 1
        size += len(chunk)
    elapsed = time.perf_counter() - started
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    peak_mb = peak / 1024 / 1024
    print(f"  导出行数 {rows}　分片 {produced}　{size / 1024 / 1024:.2f}MB　"
          f"耗时 {elapsed:.2f}s　内存峰值 {peak_mb:.1f}MB")
    limit = settings.audit_export_max_rows
    flag = "达标 ✔" if peak_mb < 100 else "超预算 ✘"
    print(f"  预算 内存峰值 < 100MB → {flag}（导出上限 {limit} 条）")


if __name__ == "__main__":
    options = parse_args(sys.argv[1:])
    if not options["db"].endswith("_bench") and not options["allow_prod"]:
        raise SystemExit(
            f"拒绝在库 {options['db']} 上压测：库名必须以 _bench 结尾，"
            "或显式加 --allow-prod。")
    # 必须在 import app.* 之前设置：config 在导入时就读取环境变量
    os.environ["MONGO_DB_NAME"] = options["db"]
    asyncio.run(run(options))
