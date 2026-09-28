# -*- coding: utf-8 -*-
"""模块 10 审计日志 · 真机验收（**真实 HTTP**，不是 ASGI 直连）。

    # 另开一个终端先把服务跑起来
    .\\scripts\\run_server.ps1
    # 再跑本脚本
    .venv\\Scripts\\python.exe scripts\\acceptance_module10.py

为什么要单独一个脚本，而不是只靠 `pytest`：
pytest 用的是 `ASGITransport`（进程内直连），它**绕过**了 uvicorn 的 HTTP 解析、
中间件顺序、静态资源挂载与流式响应真的分块下发。而验收现场看到的这些问题
（比如导出文件缺 BOM、`Content-Disposition` 写错、SSE 不分块）**只有真端口才暴露**。

脚本只做只读检查 + 一次可回滚的配置改写；退出码 = 失败项数（0 即全通过）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

import httpx                                              # noqa: E402

BASE = "http://127.0.0.1:8102"
PASSWORD = "Demo@12345"
RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    """记一条验收结果；返回 `ok` 便于链式断言。"""
    RESULTS.append((bool(ok), name, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def login(client: httpx.Client, username: str) -> tuple[int, dict]:
    """登录并返回 `(状态码, 响应体)`。"""
    resp = client.post("/api/v1/auth/login",
                       json={"username": username, "password": PASSWORD})
    return resp.status_code, resp.json()


def auth(token: str) -> dict[str, str]:
    """构造 Bearer 头。"""
    return {"Authorization": f"Bearer {token}"}


def main() -> int:
    """跑完整个验收清单。"""
    with httpx.Client(base_url=BASE, timeout=20.0) as client:
        health(client)
        token = section_login(client)
        if token is None:
            return report()
        heads = auth(token)
        section_actions(client, heads)
        section_permissions(client)
        section_append_only(client, heads)
        section_config_trace(client, heads)
        section_filters(client, heads)
        section_errors(client, heads)
        section_export(client, heads)
        section_surface(client, heads)
        section_indexes()
    return report()


def health(client: httpx.Client) -> None:
    """① 存活 + 审计段。"""
    resp = client.get("/health")
    data = resp.json().get("data", {})
    audit = data.get("audit", {})
    check(resp.status_code == 200, "health 200")
    check(data.get("dependencies", {}).get("mongodb") == "ok", "mongodb ok")
    check(audit.get("state") == "ok", "audit.state == ok", str(audit))
    check(audit.get("spool_writable") is True, "补偿目录可写")


def section_login(client: httpx.Client) -> str | None:
    """② 系统管理员登录 + 菜单里有审计入口。"""
    status, body = login(client, "lina")
    check(status == 200, "lina 登录 200", f"HTTP {status}")
    user = body.get("data", {}).get("user", {})
    check(len(user.get("permissions", [])) == 22, "lina 22 项功能权限",
          str(len(user.get("permissions", []))))
    paths = [m["path"] for m in user.get("menus", [])]
    check("#/audit" in paths, "菜单含 #/audit（侧边栏可点，不再灰显）", str(paths))
    # 知识管理员不该看到审计入口
    _status, kb = login(client, "zhangwei")
    kb_paths = [m["path"] for m in kb.get("data", {}).get("user", {}).get("menus", [])]
    check("#/audit" not in kb_paths, "kb_admin 菜单无 #/audit")
    return body.get("data", {}).get("access_token")


def section_actions(client: httpx.Client, heads: dict) -> None:
    """③ 动作字典。"""
    resp = client.get("/api/v1/audit/actions", headers=heads)
    items = resp.json()["data"]["items"]
    check(resp.status_code == 200 and len(items) == 41, "动作字典 41 条",
          f"实际 {len(items)}")
    fields = {"action", "name", "target_type", "requires_snapshot", "module", "alias_of"}
    check(all(set(i) == fields for i in items), "每条字段与 Spec §3.4 一一对应")
    check(all(any("\u4e00" <= ch <= "\u9fff" for ch in i["name"]) for i in items),
          "每条都有中文名（前端不硬编码）")
    alias = [i for i in items if i["action"] == "doc.enable"]
    check(alias and alias[0]["alias_of"] == "doc.toggle", "别名指向统一名")


def section_permissions(client: httpx.Client) -> None:
    """④ 权限边界（AC-10-01）。"""
    no_token = client.get("/api/v1/audit/logs")
    check(no_token.status_code == 401 and no_token.json()["code"] == "AUTH-2003",
          "无令牌 → 401 AUTH-2003")
    for name in ("zhangwei", "wangqiang"):
        _status, body = login(client, name)
        token = body["data"]["access_token"]
        resp = client.get("/api/v1/audit/logs", headers=auth(token))
        check(resp.status_code == 403 and resp.json()["code"] == "AUTH-2004",
              f"{name} → 403 AUTH-2004", f"HTTP {resp.status_code}")
    # 导出是独立权限码
    _status, body = login(client, "zhangwei")
    resp = client.get("/api/v1/audit/logs/export",
                      headers=auth(body["data"]["access_token"]))
    check(resp.status_code == 403, "kb_admin 导出同样被拒")


def section_append_only(client: httpx.Client, heads: dict) -> None:
    """⑤ append-only（AC-10-02）：**管理员也不能改**。"""
    for method in ("PUT", "PATCH", "DELETE"):
        for path in ("/api/v1/audit/logs", "/api/v1/audit/logs/LOG202601010001"):
            resp = client.request(method, path, headers=heads)
            code = resp.json().get("code")
            check(resp.status_code == 403 and code == "AUD-2003",
                  f"{method} {path} → AUD-2003", f"HTTP {resp.status_code} {code}")
    resp = client.put("/api/v1/audit/whatever/new", headers=heads)
    check(resp.json().get("code") == "AUD-2003", "任意审计子路径同样被拒（兜底路由）")


def section_config_trace(client: httpx.Client, heads: dict) -> None:
    """⑥ 真实留痕：改配置 + 登录失败（AC-10-10 / 脱敏红线）。"""
    cur = client.get("/api/v1/system/config", headers=heads).json()["data"]["items"]
    origin = next(i for i in cur if i["key"] == "llm.temperature")["value"]
    new_value = round(float(origin) + 0.05, 2)

    resp = client.put("/api/v1/system/config", headers=heads, json={
        "values": {"llm.temperature": new_value}, "reason": "真机验收：审计留痕检查"})
    check(resp.status_code == 200 and resp.json()["data"]["changed"] == ["llm.temperature"],
          "改配置成功（热生效）")

    logs = client.get("/api/v1/audit/logs?action=config.update", headers=heads).json()["data"]
    check(logs["total"] >= 1, "config.update 已留痕", f"total={logs['total']}")
    row = logs["items"][0]
    check(row["action_name"] == "系统 / 模型参数修改", "动作中文名由字典补齐",
          row["action_name"])
    detail = client.get(f"/api/v1/audit/logs/{row['audit_id']}",
                        headers=heads).json()["data"]
    check(detail["before"] == {"llm.temperature": origin}, "before 快照正确",
          json.dumps(detail["before"], ensure_ascii=False))
    check(detail["after"] == {"llm.temperature": new_value}, "after 快照正确")
    check(len(detail["reason"]) >= 5 and detail["snapshot_state"] != "dropped",
          "含变更原因且快照未丢", f"state={detail['snapshot_state']}")

    # 故意输错密码
    client.post("/api/v1/auth/login", json={"username": "lina", "password": "WrongPass@1"})
    fails = client.get("/api/v1/audit/logs?action=auth.login_fail",
                       headers=heads).json()["data"]
    check(fails["total"] >= 1, "登录失败已留痕")
    fail_row = fails["items"][0]
    check(fail_row["outcome"] == "failure", "登录失败的 outcome = failure")
    fail_detail = client.get(f"/api/v1/audit/logs/{fail_row['audit_id']}",
                             headers=heads).json()["data"]
    dump = json.dumps(fail_detail, ensure_ascii=False, default=str)
    check("WrongPass@1" not in dump, "审计里**没有**密码（脱敏红线）")
    check(fail_detail["after"] == {"username": "lina", "fail_reason": "AUTH-2001"},
          "只记 username 与失败原因", json.dumps(fail_detail["after"], ensure_ascii=False))

    # 还原配置
    client.put("/api/v1/system/config", headers=heads, json={
        "values": {"llm.temperature": origin}, "reason": "真机验收：还原配置值"})


def section_filters(client: httpx.Client, heads: dict) -> None:
    """⑦ 四维筛选（AC-10-11 / AC-10-12）。"""
    def total(qs: str) -> tuple[int, bool]:
        data = client.get(f"/api/v1/audit/logs?{qs}", headers=heads).json()["data"]
        return data["total"], data["degraded"]

    all_total, _ = total("")
    check(all_total > 0, "无筛选可查", f"total={all_total}")
    for label, qs in (("动作", "action=config.update"),
                      ("操作人(username)", "actor=lina"),
                      ("目标类型", "target_type=config"),
                      ("时间范围", f"start_ts={int((time.time() - 3600) * 1000)}")):
        got, _ = total(qs)
        check(0 < got <= all_total, f"维度可用：{label}", f"total={got}")

    got, degraded = total("actor=lina")
    check(degraded is False, "username 解析成功时不置 degraded")
    got, degraded = total("actor=ghost_user")
    check(got == 0 and degraded is True, "解析不出 username → degraded=true 且不报 500",
          f"total={got} degraded={degraded}")

    toggle, _ = total("action=doc.toggle")
    check(toggle >= 0, "别名查询不报错（doc.toggle 展开）")


def section_errors(client: httpx.Client, heads: dict) -> None:
    """⑧ 错误码边界（AC-10-13 及 R-03~R-07）。"""
    cases = [
        ("action=doc.modify", "AUD-1003", "未注册动作"),
        ("page_size=201", "AUD-1002", "page_size 上限"),
        ("page=0", "AUD-1002", "page 下界"),
        ("page=10001&page_size=1", "AUD-1002", "深分页"),
        ("start_ts=1790000000000&end_ts=1780000000000", "AUD-1001", "start > end"),
        ("start_ts=1790000000", "AUD-1001", "秒当毫秒"),
        ("target_type=document", "AUD-1004", "target_type 越界"),
        ("outcome=ok", "AUD-1004", "outcome 越界"),
        ("sort=actor desc", "AUD-1004", "sort 白名单"),
    ]
    for qs, want, label in cases:
        resp = client.get(f"/api/v1/audit/logs?{qs}", headers=heads)
        check(resp.json().get("code") == want, f"{label} → {want}",
              f"{resp.status_code} {resp.json().get('code')}")
    resp = client.get("/api/v1/audit/logs/not-a-log-id", headers=heads)
    check(resp.json().get("code") == "AUD-1003", "非法 log_id 格式 → AUD-1003")
    resp = client.get("/api/v1/audit/logs/LOG202601010001", headers=heads)
    check(resp.status_code == 404 and resp.json().get("code") == "AUD-3001",
          "格式合法但不存在 → AUD-3001")
    body = client.get("/api/v1/audit/logs?page=1&page_size=20", headers=heads).json()["data"]
    check(set(body) >= {"items", "total", "page", "page_size", "degraded"},
          "分页契约字段齐全", str(sorted(body)))


def section_export(client: httpx.Client, heads: dict) -> None:
    """⑨ 导出（AC-10-15 / 10-16 / 10-17）。"""
    before = client.get("/api/v1/audit/logs?action=audit.export",
                        headers=heads).json()["data"]["total"]
    csv = client.get("/api/v1/audit/logs/export?format=csv", headers=heads)
    check(csv.status_code == 200, "CSV 导出 200")
    check(csv.content[:3] == b"\xef\xbb\xbf", "CSV 带 UTF-8 BOM（Excel 不乱码）",
          csv.content[:3].hex())
    header = csv.content.decode("utf-8").lstrip("\ufeff").splitlines()[0]
    cols = header.split(",")
    check(len(cols) == 15 and cols[0] == "ts" and cols[-1] == "after",
          "CSV 15 列且列序固定", str(cols))
    check("attachment" in csv.headers.get("content-disposition", ""),
          "Content-Disposition 为附件", csv.headers.get("content-disposition", ""))
    check(csv.headers.get("content-type", "").startswith("text/csv"), "Content-Type text/csv")

    js = client.get("/api/v1/audit/logs/export?format=json", headers=heads)
    rows = js.json()
    check(isinstance(rows, list) and rows, "JSON 导出是数组", f"len={len(rows)}")
    check({"audit_id", "before", "after"} <= set(rows[0]), "JSON 行含快照字段")

    bad = client.get("/api/v1/audit/logs/export?format=xlsx", headers=heads)
    check(bad.json().get("code") == "AUD-1005", "非法 format → AUD-1005")

    time.sleep(0.3)
    after = client.get("/api/v1/audit/logs?action=audit.export",
                       headers=heads).json()["data"]["total"]
    check(after >= before + 2, "每次导出都留痕（audit.export）",
          f"{before} → {after}")


def section_surface(client: httpx.Client, heads: dict) -> None:
    """⑩ 接口面与无哈希链。"""
    paths = client.get("/openapi.json").json()["paths"]
    # 用**前缀白名单**而不是硬编码总数：断言"没有越界的路由前缀"才是本项的真实意图，
    # 硬编码总数会让本脚本随着别的模块加路由而变红（那是别的模块的验收该管的事）
    allowed_prefixes = ("/health", "/api/v1/auth", "/api/v1/system", "/api/v1/audit",
                        "/api/v1/org")
    stray = [p for p in paths if not p.startswith(allowed_prefixes)]
    check(not stray, "没有越界的路由前缀", str(stray))
    audit_paths = {p for p in paths if p.startswith("/api/v1/audit")}
    check(audit_paths == {"/api/v1/audit/logs", "/api/v1/audit/logs/export",
                          "/api/v1/audit/logs/{log_id}", "/api/v1/audit/actions"},
          "审计接口恰好 4 只", str(sorted(audit_paths)))
    for path in ("/api/v1/audit/logs", "/api/v1/audit/logs/export",
                 "/api/v1/audit/logs/{log_id}", "/api/v1/audit/actions"):
        check(set(paths[path]) == {"get"}, f"{path} 只有 GET")
    logs = client.get("/api/v1/audit/logs?page_size=200", headers=heads).json()["data"]
    doc = client.get(f"/api/v1/audit/logs/{logs['items'][0]['audit_id']}",
                     headers=heads).json()["data"]
    check("prev_hash" not in doc and "hash" not in doc, "无哈希链字段（G-10）")


def section_indexes() -> None:
    """⑪ 索引与永久保留（AC-10-18 / 10-19，直连库检查）。"""
    from pymongo import MongoClient

    from app.core.config import settings

    with MongoClient(settings.mongo_url, serverSelectionTimeoutMS=5000) as mongo:
        coll = mongo[settings.mongo_db]["audit_logs"]
        names = {i["name"]: i for i in coll.list_indexes()}
        check({"ix_ts", "ix_actor_ts", "ix_target", "ix_action_ts", "uq_spool_id"}
              <= set(names), "5 条索引齐备", str(sorted(names)))
        check(not any("expireAfterSeconds" in i for i in names.values()),
              "无 TTL 索引（永久保留）")
        spool = names.get("uq_spool_id", {})
        check(spool.get("unique") is True and spool.get("sparse") is True,
              "spool_id 唯一 + 稀疏")


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


if __name__ == "__main__":
    raise SystemExit(main())
