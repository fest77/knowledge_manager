# -*- coding: utf-8 -*-
"""模块 09 运营看板 · 真机验收（**真实 HTTP**，不是 ASGI 直连）。

    # 另开一个终端先把服务跑起来
    .\\scripts\\run_server.ps1
    # 再跑本脚本
    .venv\\Scripts\\python.exe scripts\\acceptance_module09.py

为什么要有这个脚本（pytest 已经覆盖了服务层）：
pytest 走 `ASGITransport`（进程内直连），它**绕过**了 uvicorn 的 HTTP 解析、
中间件顺序与流式响应。而看板这一块最容易被现场打脸的三件事恰好都在那一层——

1. **导出文件的 BOM 与 `Content-Disposition`**：只有真端口才看得到响应头到底长什么样；
2. **`kb_admin` 必须拿到 403 `MET-2003`**：这是"权限在中间件链上真的生效了"的证明，
   而不是"服务层函数返回了正确的异常对象"；
3. **看板真的能读到刚写进去的桶**：ASGI 直连下每个用例都清库重建，
   掩盖了"写完查不到"这一类真实故障（本模块就真踩过一次：桶文档少了索引字段）。

脚本做三件事：
① 只读的接口契约与权限矩阵（真实 HTTP）；
② 经**唯一投递入口** `metric_service.inc()` 造 4 条指标，验证桶形状与查询链路，
   结束后**按 `_id` 精确清理**自己造的数据；
③ 直连 Mongo 核对 E20 的 3 条索引（含 TTL 部分索引）。

退出码 = 失败项数（0 即全通过）。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

import httpx                                              # noqa: E402

# 默认打 8102（`run_server.ps1` 的端口）。开发机上 8102 被别的项目占着时，
# 可用 `KM_ACCEPT_BASE=http://127.0.0.1:8112 ...` 指向本次起的实例
BASE = os.getenv("KM_ACCEPT_BASE") or "http://127.0.0.1:8102"
PASSWORD = "Demo@12345"
RESULTS: list[tuple[bool, str, str]] = []
# 验收自造的数据（结束后精确删除）：`(bucket_type, bucket_key, ts)`
CREATED: list[tuple[str, str, int]] = []
# 自造数据使用的那个时刻（清理 `1h`/`1d` 聚合桶时要靠它反推窗口）
SYNTHETIC_TS: int | None = None


def check(ok: bool, name: str, detail: str = "") -> bool:
    """记一条验收结果；返回 `ok` 便于链式断言。"""
    RESULTS.append((bool(ok), name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def login(client: httpx.Client, username: str) -> str | None:
    """登录并返回令牌（失败返回 `None`）。"""
    resp = client.post("/api/v1/auth/login",
                       json={"username": username, "password": PASSWORD})
    if resp.status_code != 200:
        check(False, f"{username} 登录", f"HTTP {resp.status_code} {resp.text[:80]}")
        return None
    return resp.json()["data"]["access_token"]


def auth(token: str) -> dict[str, str]:
    """构造 Bearer 头。"""
    return {"Authorization": f"Bearer {token}"}


def report() -> int:
    """打印汇总并返回失败数。"""
    failed = [r for r in RESULTS if not r[0]]
    print("\n" + "=" * 62)
    print(f"PASS {len(RESULTS) - len(failed)} / FAIL {len(failed)}（共 {len(RESULTS)} 项）")
    if failed:
        print("失败项：")
        for _ok, name, detail in failed:
            print(f"  ✘ {name} — {detail}")
    print("=" * 62)
    return len(failed)


# ============================================================ ① 健康与接口面
def section_health(client: httpx.Client) -> None:
    """存活与静态页面。"""
    resp = client.get("/health")
    check(resp.status_code == 200, "GET /health", f"HTTP {resp.status_code}")
    body = resp.json().get("data") or {}
    check(body.get("status") in ("ok", "degraded"), "健康状态字段", str(body.get("status")))
    ui = client.get("/ui/js/pages/dashboard.js")
    check(ui.status_code == 200 and "javascript" in ui.headers["content-type"],
          "看板页模块可取到且 Content-Type 正确")
    spec = client.get("/openapi.json").json()["paths"]
    metrics = sorted(p for p in spec if p.startswith("/api/v1/metrics"))
    check(metrics == ["/api/v1/metrics/export", "/api/v1/metrics/latency",
                      "/api/v1/metrics/overview", "/api/v1/metrics/ranking",
                      "/api/v1/metrics/trend"], "接口面恰好 5 条", str(metrics))
    check(all(set(spec[p]) == {"get"} for p in metrics),
          "看板接口全部只读（无 PUT/POST/DELETE）")


# ============================================================ ② 权限矩阵
def section_permissions(client: httpx.Client, admin: str) -> None:
    """AC-09-14 / ER-08：四种身份 × 5 条路径。"""
    reads = ("/api/v1/metrics/overview", "/api/v1/metrics/trend",
             "/api/v1/metrics/latency", "/api/v1/metrics/ranking")
    for path in reads:
        resp = client.get(path, headers=auth(admin))
        check(resp.status_code == 200 and resp.json()["code"] == 0,
              f"sys_admin 可读 {path}", f"HTTP {resp.status_code}")

    # 无令牌 → 401（ER-08：认证由 01 的中间件统一负责，本模块不定义 401 码）
    resp = client.get("/api/v1/metrics/overview")
    check(resp.status_code == 401 and resp.json()["code"].startswith("AUTH-"),
          "无令牌 → 401 AUTH-*", f"HTTP {resp.status_code} {resp.json()['code']}")

    kb = login(client, "zhangwei")
    if kb:
        for path in (*reads, "/api/v1/metrics/export?metric=overview"):
            resp = client.get(path, headers=auth(kb))
            payload = resp.json()
            check(resp.status_code == 403 and payload["code"] == "MET-2003"
                  and payload["data"] is None,
                  f"kb_admin 访问 {path.split('?')[0]} → 403 MET-2003",
                  f"HTTP {resp.status_code} {payload['code']}")

    asker = login(client, "wangqiang")
    if asker:
        resp = client.get("/api/v1/metrics/overview", headers=auth(asker))
        check(resp.status_code == 403 and resp.json()["code"] == "MET-2002",
              "普通用户 → 403 MET-2002", f"HTTP {resp.status_code}")


# ============================================================ ③ 桶形状与投递
def section_buckets() -> None:
    """经唯一入口投递 4 条指标，核对桶形状（AD-12 / AC-09-01 / AC-09-15）。"""
    from pymongo import MongoClient

    from app.core.config import settings
    from app.services.metric_service import (align_ts, latency_bucket_of,
                                             metric_service, metric_repo)

    # 挑一个"三分钟前"的时刻：既不落在当前分钟（便于稳定断言），
    # 也在看板的"近 1 天"窗口内，方便随后用接口验证
    global SYNTHETIC_TS
    now = metric_repo.now_ms() - 3 * 60_000
    SYNTHETIC_TS = now
    day = align_ts(now, "1d")
    minute = align_ts(now, "1m")

    async def deliver() -> None:
        await metric_service.inc("U000001", "DEPT0005", ts_ms=now, elapsed_ms=800,
                                 token_prompt=100, token_completion=40)
        await metric_service.inc("U000001", "DEPT0005", ts_ms=now + 1000,
                                 elapsed_ms=1500, token_prompt=120,
                                 token_completion=60)
        await metric_service.inc("U000001", "DEPT0005", ts_ms=now + 2000,
                                 faq_hit=True, faq_id="FAQ000001",
                                 elapsed_ms=2500, denied_chunk_cnt=2,
                                 doc_ids=["DOC000001", "DOC000001"])
        await metric_service.inc("U000003", "DEPT0004", ts_ms=now + 3000,
                                 elapsed_ms=30000, answer_source="no_knowledge")

    from app.infra.mongo import mongo

    async def run() -> None:
        await mongo.connect()
        try:
            await deliver()
        finally:
            await mongo.close()

    asyncio.run(run())

    # ★ 清理清单必须**逐条由投递内容推导**，不能凭印象列几条：
    # 每次提问都会写一个**自己的延时区间桶**（800/1500/2500/30000ms 落在四个不同区间），
    # 只在清单里写其中两个，剩下两个就会永久留在演示库里（本脚本第一版就是这么漏的）。
    for bucket_type, key, ts in (
        ("global", "all", minute),
        ("global", "uv", day),
        ("faq", "FAQ000001", day),
        ("doc", "DOC000001", day),
        *[("latency", latency_bucket_of(elapsed), minute)
          for elapsed in (800, 1500, 2500, 30000)],
    ):
        CREATED.append((bucket_type, key, ts))

    with MongoClient(settings.mongo_url, serverSelectionTimeoutMS=5000) as mc:
        coll = mc[settings.mongo_db][metric_repo.BUCKETS]
        cursor = coll.find({"_id": {"$in": [metric_repo.bucket_id(t, k, ts)
                                           for t, k, ts in CREATED]}})
        docs = {d["_id"]: d for d in cursor}

    check(len(docs) == len(CREATED), f"{len(CREATED)} 个桶全部写出",
          f"{len(docs)}/{len(CREATED)}")

    minute_doc = docs.get(metric_repo.bucket_id("global", "all", minute), {})
    metrics = minute_doc.get("metrics") or {}
    # 四次提问**都在同一分钟**：三次 RAG/FAQ + 一次 no_knowledge。
    # PV 的语义是"发生了多少次问答"，no_knowledge 也是一次访问（Spec §4.3 对照表）
    check(metrics.get("pv") == 4, "全局分钟桶 PV = 4（含 no_knowledge 那次）",
          str(metrics.get("pv")))
    check(metrics.get("token_prompt") == 220 and metrics.get("token_completion") == 100,
          "Token 口径只累加大模型用量",
          f"{metrics.get('token_prompt')}/{metrics.get('token_completion')}")
    check(metrics.get("denied_chunk_cnt") == 2, "拦截切片数已累加",
          str(metrics.get("denied_chunk_cnt")))
    check(metrics.get("no_knowledge_cnt") == 1, "no_knowledge 分支计数",
          str(metrics.get("no_knowledge_cnt")))

    # ★ AD-12：可加字段与去重集合物理隔离
    check("uv_set" not in minute_doc, "★ 分钟桶不含 uv_set（AD-12）")
    uv_doc = docs.get(metric_repo.bucket_id("global", "uv", day), {})
    check(sorted(uv_doc.get("uv_set") or []) == ["U000001", "U000003"],
          "★ 日桶 uv_set 是去重集合（两个人）", str(uv_doc.get("uv_set")))
    check("expire_at" not in uv_doc, "日桶不带 expire_at（永久保留）")
    check(minute_doc.get("expire_at") is not None, "分钟桶带 expire_at（30 天 TTL）")
    check(minute_doc.get("bucket_ts") == minute
          and minute_doc.get("granularity") == "1m"
          and minute_doc.get("bucket_type") == "global",
          "桶文档带全索引字段（缺了就会'写进去却查不到'）")

    latency_doc = docs.get(metric_repo.bucket_id("latency", "12000+", minute), {})
    check((latency_doc.get("metrics") or {}).get("pv") == 1,
          "30 秒的提问落在 12000+ 区间")
    # 四个耗时落在四个不同区间 → 直方图数据源必须完整（这也是清理清单的完整性证明）
    labels = {latency_bucket_of(e) for e in (800, 1500, 2500, 30000)}
    latency_total = sum(
        int((docs.get(metric_repo.bucket_id("latency", label, minute), {})
             .get("metrics") or {}).get("pv") or 0) for label in labels)
    check(len(labels) == 4 and latency_total == 4,
          "四个延时区间桶各 +1（合计 4）",
          f"{sorted(labels)} 合计 {latency_total}")
    doc_doc = docs.get(metric_repo.bucket_id("doc", "DOC000001", day), {})
    check((doc_doc.get("metrics") or {}).get("pv") == 1,
          "重复 doc_id 只算一次（同轮去重）")
    faq_doc = docs.get(metric_repo.bucket_id("faq", "FAQ000001", day), {})
    check((faq_doc.get("metrics") or {}).get("faq_hit_cnt") == 1, "FAQ 命中桶")


# ============================================================ ④ 四个查询接口
def section_queries(client: httpx.Client, admin: str) -> None:
    """契约完整性：卡片 / 等长序列 / 7 个区间 / 榜单结构 / 时区。"""
    heads = auth(admin)
    data = client.get("/api/v1/metrics/overview?days=1", headers=heads).json()["data"]
    check(set(data["cards"]) == {"pv", "uv", "doc_total", "faq_hit_rate",
                                 "avg_elapsed_s"}, "overview 恰好 5 张卡",
          str(sorted(data["cards"])))
    check(data["cards"]["pv"]["value"] >= 3, "overview 看到了刚投递的 PV",
          str(data["cards"]["pv"]["value"]))
    check(data["cards"]["uv"]["value"] >= 2, "overview 的 UV 是去重人数",
          str(data["cards"]["uv"]["value"]))
    check(isinstance(data["cards"]["doc_total"]["value"], int)
          and "已启用" in (data["cards"]["doc_total"].get("sub") or ""),
          "doc_total 卡片实时计数 + 已启用副标题")
    check("token_prompt" in data["totals"] and "open_gap_cnt" in data["totals"],
          "totals 含 Token 与缺口数")

    trend = client.get("/api/v1/metrics/trend?days=1&metrics=pv,uv,token,denied",
                       headers=heads).json()["data"]
    keys = [s["key"] for s in trend["series"]]
    check(keys == ["pv", "uv", "token_prompt", "token_completion",
                   "denied_chunk_cnt"], "token 拆成 prompt/completion 两条（R-4）",
          str(keys))
    check(all(len(s["data"]) == len(trend["x_axis"]) for s in trend["series"]),
          "x_axis 与各 series 等长（AC-09-23）")
    check(trend["granularity"] in ("1m", "1h", "1d"), "回显 granularity",
          trend["granularity"])
    check(("downsampled" in trend) and ("degraded" in trend)
          and ("source_granularity" in trend), "回显降采样与降级标记")
    check(trend["notes"]["token_scope"].find("embedding") >= 0,
          "Token 口径显式声明不含 embedding（G-07）")
    pv_sum = sum(next(s for s in trend["series"] if s["key"] == "pv")["data"])
    check(pv_sum >= 3, "趋势里能看到刚投递的 PV", str(pv_sum))

    month = client.get("/api/v1/metrics/trend?days=30&metrics=pv,uv",
                       headers=heads).json()["data"]
    check(month["granularity"] == "1d" and month["downsampled"] is True,
          "30 天自动走 1d（AC-09-12）", month["granularity"])
    check(len(month["x_axis"]) == 30 and all(len(x) == 10 for x in month["x_axis"]),
          "30 个 YYYY-MM-DD 标签", str(len(month["x_axis"])))
    check(all(len(s["data"]) == 30 for s in month["series"]), "30 天序列等长")

    latency = client.get("/api/v1/metrics/latency?days=1", headers=heads).json()["data"]
    check(len(latency["bins"]) == 7, "延时直方图恰好 7 个区间",
          str([b["label"] for b in latency["bins"]]))
    check(latency["bins"][-1]["range"][1] is None, "12000+ 区间上界是 null")
    p50, p95 = latency["percentiles"]["p50_ms"], latency["percentiles"]["p95_ms"]
    check(p50 is None or p95 is None or p50 <= p95, "P50 ≤ P95（AC-09-08）",
          f"{p50}/{p95}")
    check(latency["precision"] in ("approximate", "exact"),
          "precision 已回显", latency["precision"])
    exact = client.get("/api/v1/metrics/latency?days=1&mode=exact",
                       headers=heads).json()["data"]
    check(exact["mode"] in ("exact", "bucket"), "exact 模式（不可用会降级并标 degraded）",
          f"mode={exact['mode']} degraded={exact['degraded']}")

    ranking = client.get("/api/v1/metrics/ranking?days=1&limit=10",
                         headers=heads).json()["data"]
    check("top_questions" in ranking and "top_docs" in ranking,
          "榜单默认返回两族（type=both）")
    ranks = [r["rank"] for r in ranking["top_questions"]]
    check(ranks == list(range(1, len(ranks) + 1)), "rank 从 1 连续", str(ranks))
    check(all("user_id" not in r and "session_id" not in r
              for r in ranking["top_questions"]),
          "榜单不返回提问人明细（G-12）")

    # 时区（AC-09-19）
    from app.services.metric_service import align_ts

    late = align_ts(1774367400000, "1d")          # 2026-09-23 23:50 CST
    early = align_ts(1774369800000, "1d")         # 2026-09-24 00:10 CST
    check(early - late == 86400, "跨零点切到两个日桶（Asia/Shanghai）")


# ============================================================ ⑤ 错误码矩阵
def section_errors(client: httpx.Client, admin: str) -> None:
    """11 个参数类错误码在真端口上逐条触发。"""
    heads = auth(admin)
    cases = [
        ("/api/v1/metrics/overview?days=0", "MET-1001"),
        ("/api/v1/metrics/trend?metrics=token_total", "MET-1003"),
        ("/api/v1/metrics/trend?days=400", "MET-1001"),
        ("/api/v1/metrics/trend?days=7&granularity=1m", "MET-1005"),
        ("/api/v1/metrics/trend?days=7&granularity=5m", "MET-1010"),
        ("/api/v1/metrics/latency?percentiles=P99", "MET-1006"),
        ("/api/v1/metrics/latency?days=60&mode=exact", "MET-1007"),
        ("/api/v1/metrics/ranking?limit=51", "MET-1008"),
        ("/api/v1/metrics/export?format=xlsx", "MET-1009"),
    ]
    for path, expected in cases:
        resp = client.get(path, headers=heads)
        code = resp.json().get("code")
        check(code == expected, f"{path} → {expected}", f"HTTP {resp.status_code} {code}")

    # 未来区间（MET-1002）：end_ts 用 1 小时后
    import time

    now_ms = int(time.time() * 1000)
    resp = client.get(f"/api/v1/metrics/overview?start_ts={now_ms - 3600_000}"
                      f"&end_ts={now_ms + 3600_000}", headers=heads)
    check(resp.json().get("code") == "MET-1002", "未来区间 → MET-1002",
          str(resp.json().get("code")))

    # 点名要 1h 但桶可能不存在 → 允许两种诚实结果：有桶就 200，没桶就 409
    resp = client.get("/api/v1/metrics/trend?days=7&metrics=pv&granularity=1h",
                      headers=heads)
    code = resp.json().get("code")
    check(resp.status_code == 200 or code == "MET-3002",
          "点名 1h：200（已汇总）或 409 MET-3002（未汇总），绝不静默返回零值",
          f"HTTP {resp.status_code} {code}")


# ============================================================ ⑥ 导出
def section_export(client: httpx.Client, admin: str) -> None:
    """AC-09-21：BOM + 附件头 + 四类对象都能导。"""
    heads = auth(admin)
    for metric in ("overview", "trend", "latency", "ranking"):
        resp = client.get(f"/api/v1/metrics/export?metric={metric}&days=1",
                          headers=heads)
        ctype = resp.headers.get("content-type", "")
        disp = resp.headers.get("content-disposition", "")
        check(resp.status_code == 200 and ctype.startswith("text/csv"),
              f"导出 {metric}：Content-Type", ctype)
        check("attachment" in disp and f"metrics_{metric}_" in disp,
              f"导出 {metric}：Content-Disposition", disp)
        check(resp.content.startswith(b"\xef\xbb\xbf"),
              f"导出 {metric}：UTF-8 BOM（Excel 中文不乱码）")
        lines = resp.content.decode("utf-8-sig").splitlines()
        check(len(lines) >= 2, f"导出 {metric}：不止表头", f"{len(lines)} 行")


# ============================================================ ⑧ 限流
def relax_rate_limit(client: httpx.Client, admin: str) -> int | None:
    """把看板限流临时放宽（默认 60 → 600），返回原值以便还原。

    **为什么必须放宽**：本脚本单次要打 80+ 个请求，而 R-11 的口径是
    "60 次/分钟/用户"。不放宽的话，**连跑两遍就会撞限流**——
    症状是后面的导出突然变成 429 的 JSON，看起来像"导出功能坏了"。
    （这与模块 10 的验收脚本"一次可回滚的配置改写"是同一套做法。）
    """
    heads = auth(admin)
    items = client.get("/api/v1/system/config", headers=heads).json()["data"]["items"]
    origin = int(next(i["value"] for i in items
                      if i["key"] == "metric.rate_limit_per_min"))
    if origin < 600:
        resp = client.put("/api/v1/system/config", headers=heads, json={
            "values": {"metric.rate_limit_per_min": 600},
            "reason": "模块 09 真机验收：临时放宽看板限流（结束后自动还原）"})
        check(resp.status_code == 200, "临时放宽看板限流 60→600",
              f"HTTP {resp.status_code}")
    return origin


def restore_rate_limit(client: httpx.Client, admin: str, origin: int | None) -> None:
    """还原限流配置（无论验收是否通过都要还原）。"""
    if origin is None:
        return
    resp = client.put("/api/v1/system/config", headers=auth(admin), json={
        "values": {"metric.rate_limit_per_min": origin},
        "reason": "模块 09 真机验收：还原看板限流配置"})
    print(f"[{'PASS' if resp.status_code == 200 else 'FAIL'}] 还原限流配置 → {origin}"
          f" — HTTP {resp.status_code}")


def section_rate_limit(client: httpx.Client, admin: str) -> None:
    """R-11：超限必须是 **429 + MET-2004**（而不是 500 或静默降级）。

    放在"还原限流之后"跑：此刻本分钟的计数已经远超 60，所以很快就能触发，
    不需要额外打 70 次请求。若一次都没触发，说明限流没生效——那也是失败。
    """
    heads = auth(admin)
    for attempt in range(70):
        resp = client.get("/api/v1/metrics/overview?days=1", headers=heads)
        if resp.status_code == 429:
            payload = resp.json()
            check(payload.get("code") == "MET-2004",
                  "超限 → 429 MET-2004",
                  f"第 {attempt + 1} 次请求，code={payload.get('code')}")
            return
    check(False, "超限 → 429 MET-2004", "连打 70 次都没触发限流")


# ============================================================ ⑦ 索引
def section_indexes() -> None:
    """E20 的 3 条索引，含 TTL 部分索引（AC-09-15）。"""
    from pymongo import MongoClient

    from app.core.config import settings
    from app.repositories import metric_repo

    with MongoClient(settings.mongo_url, serverSelectionTimeoutMS=5000) as mc:
        coll = mc[settings.mongo_db][metric_repo.BUCKETS]
        names = {i["name"]: i for i in coll.list_indexes()}
        check({"ix_metric_query", "ix_metric_drill", "ttl_metric_short"} <= set(names),
              "E20 三条索引齐备", str(sorted(names)))
        ttl = names.get("ttl_metric_short", {})
        check(ttl.get("expireAfterSeconds") == 0, "TTL 用 expireAfterSeconds=0")
        partial = (ttl.get("partialFilterExpression") or {}).get("granularity") or {}
        check(partial.get("$in") == ["1m", "1h"],
              "TTL 只覆盖 1m/1h（日桶永久）", str(partial))


# ============================================================ 清理
def cleanup() -> None:
    """删掉验收自造的桶（按 `_id` 精确删除，不动真实数据）。

    ⚠️ 还要清掉**汇总任务可能已经生成**的 `1h`/`1d` 桶：定时汇总（每 10 分钟）
    随时可能把我们那几条分钟桶汇总进 `global:all:{小时}` 与 `global:all:{日}`。
    不清理的话，演示库里会留下几个"没头没尾"的聚合数字。
    万一误删了同小时的真实聚合桶也没关系——下一次汇总会用 `$set` 重算覆盖（幂等）。
    """
    from pymongo import MongoClient

    from app.core.config import settings
    from app.repositories import metric_repo
    from app.services.metric_service import align_ts

    if not CREATED:
        return
    ids = [metric_repo.bucket_id(t, k, ts) for t, k, ts in CREATED]
    if SYNTHETIC_TS is not None:
        ids += [metric_repo.bucket_id("global", "all", align_ts(SYNTHETIC_TS, "1h")),
                metric_repo.bucket_id("global", "all", align_ts(SYNTHETIC_TS, "1d"))]
    with MongoClient(settings.mongo_url, serverSelectionTimeoutMS=5000) as mc:
        result = mc[settings.mongo_db][metric_repo.BUCKETS].delete_many(
            {"_id": {"$in": ids}})
    print(f"\n（已清理验收自造数据：{result.deleted_count} 条桶）")


def main() -> int:
    """跑完整个验收清单。"""
    origin: int | None = None
    try:
        with httpx.Client(base_url=BASE, timeout=20.0) as client:
            section_health(client)
            admin = login(client, "lina")
            if admin is None:
                return report()
            origin = relax_rate_limit(client, admin)
            section_permissions(client, admin)
            section_buckets()
            section_queries(client, admin)
            section_errors(client, admin)
            section_export(client, admin)
            section_indexes()
            restore_rate_limit(client, admin, origin)
            origin = None
            section_rate_limit(client, admin)
        return report()
    finally:
        cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
