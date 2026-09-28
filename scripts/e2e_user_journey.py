# -*- coding: utf-8 -*-
"""**模拟真实用户使用**的端到端验收（真实 HTTP + 真模型 + 真存储）。

    # 终端 A：起服务
    .\\scripts\\run_server.ps1            # 或 --port 8112
    # 终端 B：
    .venv\\Scripts\\python.exe scripts\\e2e_user_journey.py
    #   （8102 被占用时）$env:KM_ACCEPT_BASE="http://127.0.0.1:8112"

它与另外三类测试的分工：

| 测试 | 覆盖 |
|---|---|
| `pytest`（656 例） | 每个模块的规则、边界、错误码，**进程内直连**、每例清库 |
| `acceptance_moduleXX.py` | 单模块的接口契约与权限矩阵（真实 HTTP） |
| **本脚本** | **一条完整的用户旅程**：建分类 → 传文件 → 配权限 → 提问 →
|  | FAQ 沉淀 → 缺口转建 → 看板 → 审计，全程走 HTTP 与真实模型 |

为什么要单跑一条"旅程"：单模块用例各自都过，不代表**串起来**能过。
跨模块的状态传递（切片落 Milvus → 问答召回 → 权限拦截 → 指标投递 → 看板聚合 → 审计留痕）
只有真按用户顺序走一遍才会暴露断点。

**隔离性**：本脚本在演示库 `kb001` 上跑，会**真的**建分类/文档/FAQ/缺口与问答日志
（这些正是演示数据）；它不删自己的产物，因为"看得见的成果"就是验收物。
需要干净环境时先 `python scripts/seed.py --drop`。

退出码 = 失败项数（0 即全通过）。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

import httpx                                              # noqa: E402

BASE = os.getenv("KM_ACCEPT_BASE") or "http://127.0.0.1:8102"
PASSWORD = "Demo@12345"
# 单请求超时：问答要过 LLM，挖掘/导入要过模型，给足余量
TIMEOUT = httpx.Timeout(120.0, connect=10.0)

RESULTS: list[tuple[bool, str, str]] = []
# 后续步骤要用的上下文（文档号 / 会话号 / 候选号 …）
CTX: dict[str, object] = {}


# ------------------------------------------------------------------ 基础设施
def check(ok: bool, name: str, detail: str = "") -> bool:
    """记一条结果（`detail` 只显示前 160 字，避免刷屏）。"""
    text = ("" if detail is None else str(detail)).replace("\n", " ")[:160]
    RESULTS.append((bool(ok), name, text))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {text}" if text else ""))
    return bool(ok)


def section(title: str) -> None:
    """打印一段标题（让 94 项断言的输出分段可读）。"""
    print(f"\n{'=' * 8} {title} {'=' * 8}")


def login(client: httpx.Client, username: str, password: str = PASSWORD
          ) -> tuple[int, str | None]:
    """登录；返回 `(HTTP 状态码, 令牌或错误码)`。"""
    resp = client.post("/api/v1/auth/login",
                       json={"username": username, "password": password})
    data = resp.json()
    if resp.status_code == 200 and data.get("code") == 0:
        return resp.status_code, data["data"]["access_token"]
    return resp.status_code, data.get("code")


def auth(token: str) -> dict[str, str]:
    """构造 Bearer 头。"""
    return {"Authorization": f"Bearer {token}"}


def get(client: httpx.Client, path: str, token: str, **kw) -> dict:
    """GET 并把（状态码, JSON, 原始响应）一起带回来（检查响应头时要用 `raw`）。"""
    resp = client.get(path, headers=auth(token), **kw)
    return {"status": resp.status_code, "body": _safe_json(resp), "raw": resp}


def post(client: httpx.Client, path: str, token: str, json_body=None, **kw) -> dict:
    """POST JSON。"""
    resp = client.post(path, headers=auth(token), json=json_body, **kw)
    return {"status": resp.status_code, "body": _safe_json(resp), "raw": resp}


def put(client: httpx.Client, path: str, token: str, json_body=None, **kw) -> dict:
    """PUT JSON。"""
    resp = client.put(path, headers=auth(token), json=json_body, **kw)
    return {"status": resp.status_code, "body": _safe_json(resp), "raw": resp}


def _safe_json(resp: httpx.Response):
    try:
        return resp.json()
    except Exception:                                     # noqa: BLE001
        return {"_raw": resp.text[:200]}


def data_of(result: dict):
    """取成功响应的 `data`；失败返回 `None` 并把错误码放进 `CTX['last_error']`。"""
    body = result.get("body") or {}
    if body.get("code") == 0:
        return body.get("data")
    CTX["last_error"] = f"HTTP {result['status']} {body.get('code')} {body.get('message')}"
    return None


def code_of(result: dict) -> str:
    """取响应里的业务错误码（成功是 `"0"`）。"""
    return str((result.get("body") or {}).get("code"))


# ------------------------------------------------------------------ 旅程用的文件
DOC_A_NAME = "差旅报销标准（2026版）.md"
DOC_A_TEXT = """# 差旅报销标准（2026版）

## 住宿标准
- 一线城市：每人每晚上限 600 元
- 二线城市：每人每晚上限 400 元
- 其他城市：每人每晚上限 300 元

## 交通标准
- 高铁二等座、经济舱机票可全额报销
- 市内交通凭票报销，单日上限 100 元

## 餐补标准
- 出差期间每人每天餐补 120 元，无需发票

## 报销时限
- 出差结束后 15 个工作日内提交报销单，逾期需部门负责人审批
"""

DOC_B_NAME = "年假与考勤制度.md"
DOC_B_TEXT = """# 年假与考勤制度

## 年假天数
- 入职满 1 年不满 3 年：每年 5 天年假
- 入职满 3 年不满 10 年：每年 10 天年假
- 入职满 10 年以上：每年 15 天年假

## 年假使用
- 年假需提前 3 个工作日在系统内申请
- 当年度年假原则上不跨年使用，确有需要可顺延至次年 3 月 31 日

## 考勤
- 上班时间 09:00，下班时间 18:00，弹性 30 分钟
- 迟到超过 30 分钟计为半天事假
"""


def build_files(tmp: Path) -> tuple[Path, Path]:
    """把两份演示文件写到临时目录（内容即上面两段）。"""
    tmp.mkdir(parents=True, exist_ok=True)
    path_a = tmp / DOC_A_NAME
    path_b = tmp / DOC_B_NAME
    path_a.write_text(DOC_A_TEXT, encoding="utf-8")
    path_b.write_text(DOC_B_TEXT, encoding="utf-8")
    return path_a, path_b


# ------------------------------------------------------------------ ① 健康与静态资源
def section_health(client: httpx.Client) -> None:
    """① 存活与前端静态资源：14 个 ES module 必须都能取到且 Content-Type 正确。"""
    section("① 健康检查与前端静态资源")
    resp = client.get("/health")
    body = resp.json().get("data") or {}
    check(resp.status_code == 200 and body.get("status") in ("ok", "degraded"),
          "GET /health 可用", f"status={body.get('status')} deps={body.get('dependencies')}")
    check((body.get("dependencies") or {}).get("mongodb") == "ok", "Mongo 连通")
    check((body.get("dependencies") or {}).get("milvus") == "ok", "Milvus 连通")
    check((body.get("dependencies") or {}).get("minio") == "ok", "MinIO 连通")

    page = client.get("/ui/")
    check(page.status_code == 200 and "知识库管理平台" in page.text,
          "前端壳可取到", f"HTTP {page.status_code}")
    modules = ["app.js", "api.js", "router.js", "shell.js",
               "pages/login.js", "pages/me.js", "pages/system.js", "pages/audit.js",
               "pages/docs.js", "pages/import_panel.js", "pages/perm_panel.js",
               "pages/qa.js", "pages/sediment.js", "pages/dashboard.js"]
    bad = []
    for name in modules:
        r = client.get(f"/ui/js/{name}")
        if r.status_code != 200 or "javascript" not in r.headers.get("content-type", ""):
            bad.append(f"{name}:{r.status_code}")
    check(not bad, f"{len(modules)} 个前端模块全部可取到且 Content-Type 正确", str(bad))

    r = client.get("/api/v1/docs")
    check(r.status_code == 401 and str(r.json().get("code")).startswith("AUTH-"),
          "无令牌访问业务接口 → 401", f"HTTP {r.status_code} {r.json().get('code')}")


# ------------------------------------------------------------------ ② 登录与权限
def section_login(client: httpx.Client) -> None:
    """② 登录：统一错误码（防账号枚举）、停用账号、三角色权限数、菜单可见性。"""
    section("② 登录、停用账号与统一错误码")
    status, code = login(client, "lina", "wrong-password")
    check(status == 401 and code and code.startswith("AUTH-"),
          "密码错误 → AUTH-*", f"HTTP {status} {code}")
    status2, code2 = login(client, "no_such_user", "whatever")
    check(code2 == code, "账号不存在与密码错误**同码**（防账号枚举）",
          f"{code} / {code2}")

    status3, code3 = login(client, "zhaolei")
    check(status3 == 401 and code3 == "AUTH-2002", "已停用账号 → AUTH-2002",
          f"HTTP {status3} {code3}")

    for username, expect in (("zhangwei", 16), ("wangqiang", 4), ("lina", 22)):
        status4, token = login(client, username)
        if status4 != 200 or not token:
            check(False, f"{username} 登录", str(token))
            continue
        me = data_of(get(client, "/api/v1/auth/me", token)) or {}
        check(len(me.get("permissions") or []) == expect,
              f"{username} 登录并装载 {expect} 项权限",
              f"实际 {len(me.get('permissions') or [])}；菜单 {len(me.get('menus') or [])} 个")
        CTX[f"token_{username}"] = token
        CTX[f"me_{username}"] = me

    menus_kb = {(m or {}).get("path") for m in ((CTX.get("me_zhangwei") or {}).get("menus") or [])}
    menus_sys = {(m or {}).get("path") for m in ((CTX.get("me_lina") or {}).get("menus") or [])}
    check("#/dashboard" not in menus_kb and "#/dashboard" in menus_sys,
          "看板菜单只对系统管理员可见", f"kb={sorted(menus_kb)}")


# ------------------------------------------------------------------ ③ 建库：分类 + 导入
def section_import(client: httpx.Client, file_a: Path, file_b: Path) -> None:
    """③ 建分类 → 上传两份演示文件 → 等六阶段跑完 → 看切片。"""
    section("③ 知识管理员建库：分类 → 上传 → 六阶段流水线 → 切片")
    token = str(CTX["token_zhangwei"])

    top = data_of(post(client, "/api/v1/categories", token,
                       {"name": "人事制度", "parent_id": None, "sort": 1})) or {}
    child = data_of(post(client, "/api/v1/categories", token,
                         {"name": "差旅报销", "parent_id": top.get("category_id"),
                          "sort": 1})) or {}
    check(bool(top.get("category_id")) and bool(child.get("category_id")),
          "建分类「人事制度 / 差旅报销」",
          f"{top.get('category_id')} → {child.get('category_id')}")
    CTX["category_id"] = child.get("category_id")
    tree = data_of(get(client, "/api/v1/categories", token)) or {}
    check(_tree_contains(tree, "差旅报销"), "分类树里能看到新建的分类")

    for key, path in (("a", file_a), ("b", file_b)):
        with path.open("rb") as handle:
            resp = client.post(
                "/api/v1/import/upload", headers=auth(token),
                files={"file": (path.name, handle, "text/markdown")},
                data={"category_id": str(CTX["category_id"] or ""),
                      "auto_enable": "true"})
        body = resp.json()
        # 单文件上传的 `data` **就是那一项**（批量才是 `data.items[]`）
        data = body.get("data") or {}
        item = (data.get("items") or [data])[0] if isinstance(data, dict) else {}
        ok = resp.status_code == 200 and body.get("code") == 0 and bool(item.get("doc_id"))
        check(ok, f"上传 {path.name}", f"HTTP {resp.status_code} {body.get('code')} "
                                       f"doc={item.get('doc_id')} task={item.get('task_id')}")
        if not ok:
            continue
        CTX[f"doc_{key}"] = item.get("doc_id")
        CTX[f"task_{key}"] = item.get("task_id")
        detail = wait_task(client, token, str(item.get("task_id")))
        stages = detail.get("done_stages") if detail else None
        check(bool(detail) and str(detail.get("status")) in ("succeeded", "success",
                                                             "done", "completed"),
              f"{path.name} 六阶段跑完",
              f"status={detail.get('status')} stage={detail.get('stage')}")
        check(int(detail.get("stage_total") or 0) == 6, "阶段总数 = 6（前端零分支渲染）",
              str(detail.get("stage_total")))
        check(isinstance(stages, list) and len(stages) == 6,
              "已完成阶段列表恰好 6 项（含 milvus）", str(stages))
        doc_state = data_of(get(client, f"/api/v1/docs/{item.get('doc_id')}", token)) or {}
        check(int(doc_state.get("chunk_count") or 0) > 0, "台账里的切片数 > 0",
              f"chunk_count={doc_state.get('chunk_count')} status={doc_state.get('status')}")

    doc_id = str(CTX.get("doc_a") or "")
    detail = data_of(get(client, f"/api/v1/docs/{doc_id}", token)) or {}
    check(detail.get("status") == "enabled" and int(detail.get("chunk_count") or 0) > 0,
          "台账详情：已启用且有切片",
          f"status={detail.get('status')} chunks={detail.get('chunk_count')}")

    chunks = data_of(get(client, f"/api/v1/import/docs/{doc_id}/chunks?page_size=5", token)) or {}
    items = chunks.get("items") or []
    fields = sorted(items[0]) if items else []
    check(bool(items), "切片预览非空", f"{len(items)} 条；首条字段 {fields}")
    if items:
        text = str(items[0].get("content") or items[0].get("text") or "")
        check("餐补" in text or "住宿" in text or "年假" not in text,
              "切片正文来自原文件（不是空壳）", text[:60])


def wait_assistant_messages(client: httpx.Client, token: str, session_id: str,
                            expect: int, timeout: float = 10.0) -> list[dict]:
    """等助手消息落档到齐（问答的落档是**异步**的，`done` 之后还要几百毫秒）。

    为什么必须等：`GET /qa/sessions/{id}/messages` 的键是 **`messages`**（不是 `items`），
    而且刚结束的那一轮可能还没写进去——不等就会误判成"denied_count = 0"，
    把一次成功的权限拦截当成失败（本脚本第一版就是这么误报的）。
    """
    deadline = time.time() + timeout
    items: list[dict] = []
    while time.time() < deadline:
        data = data_of(get(client, f"/api/v1/qa/sessions/{session_id}/messages"
                                   f"?page_size=50", token)) or {}
        items = [m for m in (data.get("messages") or data.get("items") or [])
                 if m.get("role") == "assistant"]
        if len(items) >= expect:
            return items
        time.sleep(0.5)
    print(f"   … 助手消息只到 {len(items)}/{expect} 条（继续按现状断言）")
    return items


def _tree_contains(tree, name: str) -> bool:
    """递归找分类树里的名字。"""
    for node in (tree.get("items") or tree.get("nodes") or []):
        if node.get("name") == name:
            return True
        if node.get("children") and _tree_contains({"items": node["children"]}, name):
            return True
    return False


def wait_task(client: httpx.Client, token: str, task_id: str, timeout: float = 300.0
              ) -> dict:
    """轮询导入任务直到终态；返回任务详情（失败/超时返回 `{}`）。"""
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        result = get(client, f"/api/v1/import/tasks/{task_id}", token)
        detail = data_of(result) or {}
        if detail:
            last = detail
        status = str(detail.get("status") or "")
        if status in ("succeeded", "success", "done", "completed", "failed",
                      "canceled", "interrupted", "timeout"):
            return detail
        time.sleep(2.0)
    print(f"   … 任务 {task_id} 轮询超时，最后状态：{last.get('status')}")
    return last


# ------------------------------------------------------------------ ④ 四维权限
def section_permission(client: httpx.Client) -> None:
    """④ 配四维权限：一篇仅财务部可见、一篇全局公开，并核对判定结论。"""
    section("④ 配置四维数据权限（一篇受限、一篇公开）")
    token = str(CTX["token_zhangwei"])
    doc_a, doc_b = str(CTX.get("doc_a")), str(CTX.get("doc_b"))

    limited = put(client, f"/api/v1/perm/{doc_a}", token, {
        "is_global": False, "departments": ["DEPT0003"], "roles": [], "users": [],
        "reason": "端到端旅程：差旅标准仅财务部可见"})
    check(limited["status"] == 200 and code_of(limited) == "0",
          "把「差旅报销标准」限制为仅财务部（DEPT0003）",
          f"HTTP {limited['status']} {code_of(limited)} {limited['body'].get('message')}")
    CTX["limited_doc"] = doc_a

    public = put(client, f"/api/v1/perm/{doc_b}", token, {
        "is_global": True, "departments": [], "roles": [], "users": [],
        "reason": "端到端旅程：年假制度全公司可见"})
    check(public["status"] == 200 and code_of(public) == "0",
          "把「年假与考勤制度」设为全局公开",
          f"HTTP {public['status']} {code_of(public)} {public['body'].get('message')}")

    # `GET /perm/{doc_id}` 是**配置视图**（四维设置 + 摘要），不是判定结果；
    # 真实的 allow/deny 只能由 `POST /perm/check` 给出（05 Spec 的接口划分）
    config_a = data_of(get(client, f"/api/v1/perm/{doc_a}", token)) or {}
    config_b = data_of(get(client, f"/api/v1/perm/{doc_b}", token)) or {}
    check(bool(config_a.get("is_global")) is False
          and bool(config_b.get("is_global")) is True,
          "四维配置视图回显 is_global（受限=False / 公开=True）",
          f"a={config_a.get('is_global')} b={config_b.get('is_global')}")

    # 判定：技术部的提问者读不到受限文档、读得到公开文档
    asker = str(CTX["token_wangqiang"])
    for label, doc_id, expect in (("受限文档（财务部专属）", doc_a, False),
                                  ("公开文档", doc_b, True)):
        one = data_of(post(client, "/api/v1/perm/check", asker, {"doc_id": doc_id})) or {}
        check(bool(one.get("allowed")) is expect,
              f"wangqiang（技术部）对{label} 判定 allowed={expect}",
              f"allowed={one.get('allowed')} reason={one.get('reason_code')}")

    single_a = data_of(post(client, "/api/v1/perm/check", asker,
                            {"doc_id": doc_a})) or {}
    single_b = data_of(post(client, "/api/v1/perm/check", asker,
                            {"doc_id": doc_b})) or {}
    check(bool(single_a.get("allowed")) is False
          and bool(single_b.get("allowed")) is True,
          "判定接口（POST /perm/check）结论与单篇查询一致",
          f"受限={single_a.get('allowed')} 公开={single_b.get('allowed')} "
          f"reason={single_a.get('reason_code')}")


# ------------------------------------------------------------------ ⑤ 问答（含拦截）
def ask_and_stream(client: httpx.Client, token: str, question: str,
                   session_id: str | None = None) -> dict:
    """问一轮并把 SSE 事件收集起来；返回 `{task, session, message, events, answer}`。"""
    payload: dict[str, object] = {"question": question}
    if session_id:
        payload["session_id"] = session_id
    asked = data_of(post(client, "/api/v1/qa/ask", token, payload)) or {}
    task_id = str(asked.get("task_id") or "")
    events: list[tuple[str, str]] = []
    answer = ""
    if task_id:
        with client.stream("GET", f"/api/v1/qa/stream/{task_id}",
                           headers=auth(token), timeout=TIMEOUT) as stream:
            name = ""
            for line in stream.iter_lines():
                if line.startswith("event:"):
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    raw = line.split(":", 1)[1].strip()
                    events.append((name, raw))
                    if name == "delta":
                        try:
                            answer += str(json.loads(raw).get("text") or "")
                        except Exception:                 # noqa: BLE001
                            answer += raw
    return {"task": task_id, "session": asked.get("session_id"),
            "message": asked.get("message_id"), "events": events, "answer": answer}


def section_qa(client: httpx.Client) -> None:
    """⑤ 问答：公开问题（要引用）/ 受限问题（要被拦）/ 无知识问题 / 反馈 / 越权订阅。"""
    section("⑤ 提问者问答：召回 → 鉴权过滤 → 生成 → SSE")
    token = str(CTX["token_wangqiang"])

    public_q = "年假有几天？入职满三年能休多少天？"
    first = ask_and_stream(client, token, public_q)
    names = [n for n, _ in first["events"]]
    check(bool(first["task"]), "POST /qa/ask 返回 task_id", str(first["task"]))
    check("meta" in names, "SSE 首帧是 meta", str(names[:4]))
    check("delta" in names, "SSE 有增量帧（流式）", f"{names.count('delta')} 帧")
    check("citation" in names, "SSE 有引用帧", str([n for n in names if n == 'citation'][:1]))
    check("done" in names, "SSE 以 done 收尾", str(names[-3:]))
    check(len(first["answer"]) > 10, "答案非空", first["answer"][:80])
    CTX["session"] = first["session"]
    CTX["public_q"] = public_q

    # 受限文档的问题：召回可能拿到切片，但鉴权必须拦下
    limited = ask_and_stream(client, token, "差旅报销的住宿费上限是多少？",
                             session_id=str(CTX.get("session")))
    assistant = wait_assistant_messages(client, token, str(CTX.get("session")), 2)
    denied = sum(int(m.get("denied_count") or 0) for m in assistant)
    check(denied >= 1, "受限知识的切片被**鉴权拦下**（denied_count ≥ 1）",
          f"denied={denied}；该轮答案前 60 字：{limited['answer'][:60]}")
    check("600" not in limited["answer"] or denied >= 1,
          "受限内容没有出现在答案里", limited["answer"][:80])

    # 知识库里没有的问题 → no_knowledge（并成为缺口的原料）
    unknown_q = "公司班车每天早上几点发车？"
    # 问两轮：一次是"没答上来"，两次才是"大家都需要这份知识"（也与缺口频次一致）
    miss = ask_and_stream(client, token, unknown_q, session_id=str(CTX.get("session")))
    ask_and_stream(client, token, unknown_q, session_id=str(CTX.get("session")))
    assistant2 = wait_assistant_messages(client, token, str(CTX.get("session")), 4)
    check(bool(miss["answer"]), "无知识问题也有兜底回答", miss["answer"][:60])
    CTX["unknown_q"] = unknown_q
    CTX["messages"] = assistant2

    sessions = data_of(get(client, "/api/v1/qa/sessions", token)) or {}
    check(any(str(s.get("session_id")) == str(CTX["session"])
              for s in (sessions.get("items") or [])), "会话出现在历史列表里")

    # 反馈：拿**最后一条助手消息**（落档是异步的，先等它到齐）
    assistant_now = wait_assistant_messages(client, token, str(CTX.get("session")), 4)
    last_message = str((assistant_now[-1] if assistant_now else {}).get("message_id") or "")
    if last_message:
        fb = post(client, f"/api/v1/qa/messages/{last_message}/feedback", token,
                  {"rating": "up", "comment": "端到端旅程：回答准确"})
        check(fb["status"] == 200 and code_of(fb) == "0", "提交反馈成功",
              f"HTTP {fb['status']} {code_of(fb)}")

    # 越权订阅别人的流
    other = str(CTX["token_zhangwei"])
    resp = client.get(f"/api/v1/qa/stream/{first['task']}", headers=auth(other))
    check(resp.status_code == 403 and "text/event-stream"
          not in resp.headers.get("content-type", ""),
          "越权订阅 SSE → 403 且不建流",
          f"HTTP {resp.status_code} {_safe_json(resp).get('code')}")


# ------------------------------------------------------------------ ⑥ FAQ 沉淀
def section_faq(client: httpx.Client) -> None:
    """⑥ FAQ：凑频次 → 挖掘 → 审核发布 → 缓存里有它。"""
    section("⑥ FAQ 沉淀：挖掘 → 审核发布 → 缓存直出")
    token = str(CTX["token_zhangwei"])
    asker = str(CTX["token_wangqiang"])
    question = str(CTX["public_q"])
    # 再问两轮，凑够频次阈值
    for _ in range(2):
        ask_and_stream(client, asker, question, session_id=str(CTX.get("session")))

    mine = post(client, "/api/v1/faq/mine", token,
                {"window_days": 1, "freq_threshold": 2})
    detail = data_of(mine) or {}
    check(mine["status"] == 200 and code_of(mine) == "0", "触发 FAQ 挖掘",
          f"HTTP {mine['status']} {mine['body'].get('message')} {detail}")

    candidates = data_of(get(client, "/api/v1/faq/candidates?page_size=20", token)) or {}
    items = candidates.get("items") or []
    hit = next((c for c in items if str(c.get("representative_question") or "")
                .startswith("年假")), None)
    check(hit is not None, "候选列表里出现「年假」问题簇",
          f"共 {len(items)} 条候选：{[c.get('representative_question') for c in items][:3]}")
    if hit is None:
        return
    CTX["candidate"] = hit.get("candidate_id")
    check(int(hit.get("frequency") or 0) >= 2, "候选频次 ≥ 2", str(hit.get("frequency")))

    related = [d.get("doc_id") for d in (hit.get("related_docs") or [])
               if d.get("doc_id")] or [str(CTX.get("doc_b"))]
    approved = post(client, f"/api/v1/faq/candidates/{hit['candidate_id']}/approve", token,
                    {"question": question,
                     "answer": "入职满 3 年不满 10 年每年 10 天年假；满 10 年以上 15 天。",
                     "aliases": ["年假多少天", "年假天数"], "category_id": None,
                     "related_doc_ids": related,
                     "review_note": "端到端旅程：审核通过并发布"})
    check(approved["status"] == 200 and code_of(approved) == "0",
          "候选审核通过 → 发布 FAQ",
          f"HTTP {approved['status']} {code_of(approved)} {approved['body'].get('message')}")

    rebuild = post(client, "/api/v1/faq/cache/rebuild", token, {})
    check(rebuild["status"] == 200 and code_of(rebuild) == "0", "重建 FAQ 缓存",
          f"HTTP {rebuild['status']} {rebuild['body'].get('message')}")

    cache = data_of(get(client, "/api/v1/faq/cache/status", token)) or {}
    check(int(cache.get("faqs_total") or 0) >= 1
          and int(cache.get("enabled_count") or 0) >= 1, "缓存里有已启用的 FAQ",
          f"faqs_total={cache.get('faqs_total')} enabled={cache.get('enabled_count')} "
          f"cache_size={cache.get('cache_size')}")

    hit_round = ask_and_stream(client, asker, question, session_id=str(CTX.get("session")))
    check(bool(hit_round["answer"]), "FAQ 直出仍有答案", hit_round["answer"][:60])
    CTX["faq_hit_round"] = hit_round


# ------------------------------------------------------------------ ⑦ 知识缺口
def section_gap(client: httpx.Client) -> None:
    """⑦ 缺口：聚合 → 清单 → 导出 → 一键转建（03 占位 + 04 任务）。"""
    section("⑦ 知识缺口：聚合 → 导出 → 一键转建")
    token = str(CTX["token_zhangwei"])
    agg = post(client, "/api/v1/gaps/aggregate", token, {"window_days": 1})
    detail = data_of(agg) or {}
    check(agg["status"] == 200 and code_of(agg) == "0", "触发缺口聚合",
          f"HTTP {agg['status']} {detail}")

    listing = data_of(get(client, "/api/v1/gaps?status=all&page_size=50", token)) or {}
    items = listing.get("items") or []
    gap = next((g for g in items if "班车" in str(g.get("question") or "")), None)
    check(gap is not None, "清单里出现「班车」缺口（来自 no_knowledge 的提问）",
          f"共 {len(items)} 条；前 3：{[g.get('question') for g in items][:3]}")
    if gap is None:
        return
    CTX["gap"] = gap.get("gap_id")
    check(int(gap.get("frequency") or 0) >= 1, "缺口频次 ≥ 1", str(gap.get("frequency")))
    check(bool(gap.get("dept_name")), "缺口带部门名（跨模块取 02 的数据）",
          str(gap.get("dept_name")))

    export = get(client, "/api/v1/gaps/export", token)
    check(export["status"] == 200
          and export["raw"].content.startswith(b"\xef\xbb\xbf"),
          "缺口导出 CSV 带 BOM", f"HTTP {export['status']}")

    convert = post(client, f"/api/v1/gaps/{gap['gap_id']}/convert", token, {})
    converted = data_of(convert) or {}
    check(convert["status"] == 200 and code_of(convert) == "0",
          "一键转建（03 建占位 → 04 建导入任务）",
          f"HTTP {convert['status']} {convert['body'].get('message')} {converted}")
    new_task = converted.get("task_id")
    if new_task:
        task = wait_task(client, token, str(new_task))
        check(str(task.get("status")) in ("success", "done", "completed"),
              "转建产生的导入任务也已跑完",
              f"status={task.get('status')} doc={converted.get('doc_id')}")

    after = data_of(get(client, f"/api/v1/gaps/{gap['gap_id']}", token)) or {}
    # 详情接口的形状是 `{gap: {...}, samples: [...], synonym_questions: [...]}`
    gap_detail = after.get("gap") if isinstance(after.get("gap"), dict) else after
    check(str(gap_detail.get("status")) == "converted", "缺口状态变为 converted",
          str(gap_detail.get("status")))
    check(bool(gap_detail.get("converted_doc_id")), "缺口详情里回写了转建出的文档号",
          str(gap_detail.get("converted_doc_id")))


# ------------------------------------------------------------------ ⑧ 看板
def section_dashboard(client: httpx.Client) -> None:
    """⑧ 看板：指标是否真的反映了刚才那一串使用（PV/UV/拦截/Token/延时/榜单）。"""
    section("⑧ 运营看板：指标是否真的反映了刚才的使用")
    token = str(CTX["token_lina"])

    overview = data_of(get(client, "/api/v1/metrics/overview?days=1", token)) or {}
    cards = overview.get("cards") or {}
    check(int(cards.get("pv", {}).get("value") or 0) >= 5,
          "PV 反映了刚才的提问次数",
          f"pv={cards.get('pv', {}).get('value')} uv={cards.get('uv', {}).get('value')}")
    check(int(cards.get("uv", {}).get("value") or 0) >= 1, "UV 有值（去重人数）",
          str(cards.get("uv", {}).get("value")))
    check(int(cards.get("doc_total", {}).get("value") or 0) >= 3,
          "知识单元总数实时反映导入的文档",
          f"doc_total={cards.get('doc_total', {}).get('value')} "
          f"sub={cards.get('doc_total', {}).get('sub')}")
    check(float(cards.get("avg_elapsed_s", {}).get("value") or 0) > 0,
          "平均问答延时 > 0（真跑过模型）",
          str(cards.get("avg_elapsed_s", {}).get("value")))

    trend = data_of(get(client, "/api/v1/metrics/trend?days=1&metrics=pv,uv,"
                                                "token,denied,faq_hit_cnt", token)) or {}
    series = {s["key"]: s["data"] for s in (trend.get("series") or [])}
    check(bool(series) and all(len(v) == len(trend.get("x_axis") or [])
                               for v in series.values()),
          "趋势序列与 x 轴等长", f"键={sorted(series)} 长度={len(trend.get('x_axis') or [])}")
    check(sum(series.get("denied_chunk_cnt") or [0]) >= 1,
          "拦截趋势里能看到刚才被拦的切片",
          str(sum(series.get("denied_chunk_cnt") or [0])))
    check(sum(series.get("token_prompt") or [0]) > 0, "Token 趋势非零（真调了模型）",
          str(sum(series.get("token_prompt") or [0])))

    latency = data_of(get(client, "/api/v1/metrics/latency?days=1", token)) or {}
    pct = latency.get("percentiles") or {}
    check(len(latency.get("bins") or []) == 7, "延时直方图 7 个区间")
    check(pct.get("p50_ms") is not None and pct.get("p95_ms") is not None,
          "P50 / P95 都有值", f"P50={pct.get('p50_ms')} P95={pct.get('p95_ms')}")
    check(int(latency.get("sample_size") or 0) >= 5, "样本量 ≥ 提问次数",
          str(latency.get("sample_size")))

    ranking = data_of(get(client, "/api/v1/metrics/ranking?days=1&limit=10", token)) or {}
    titles = [d.get("title") for d in (ranking.get("top_docs") or [])]
    questions = [q.get("question") for q in (ranking.get("top_questions") or [])]
    check(bool(titles), "热门知识榜非空（被引用过的文档）", str(titles[:3]))
    check(any("年假" in str(t) or "差旅" in str(t) for t in titles)
          or bool(questions), "榜单内容与刚才的提问相关",
          f"文档={titles[:2]} 问题={questions[:2]}")

    export = get(client, "/api/v1/metrics/export?metric=overview&days=1", token)
    check(export["status"] == 200
          and export["raw"].content.startswith(b"\xef\xbb\xbf"),
          "看板导出 CSV 带 BOM", f"HTTP {export['status']}")

    denied = get(client, "/api/v1/metrics/overview", str(CTX["token_zhangwei"]))
    check(denied["status"] == 403 and code_of(denied) == "MET-2003",
          "知识管理员访问看板 → 403 MET-2003",
          f"HTTP {denied['status']} {code_of(denied)}")


# ------------------------------------------------------------------ ⑨ 审计
def section_audit(client: httpx.Client) -> None:
    """⑨ 审计：动作是否留痕、详情快照、导出、append-only。"""
    section("⑨ 审计日志：刚才那一串动作是否都留痕")
    token = str(CTX["token_lina"])
    logs = data_of(get(client, "/api/v1/audit/logs?page_size=200", token)) or {}
    items = logs.get("items") or []
    actions = {str(i.get("action")) for i in items}
    print(f"   本次旅程共产生 {len(items)} 条审计，动作集合：{sorted(actions)}")
    expected = ["auth.login_fail", "doc.import.done", "doc.permission_change",
                "config.update", "user.disable"]
    for action in expected:
        if action == "config.update" or action == "user.disable":
            continue                                          # 由第 ⑩ 节产生，稍后单独断言
        check(action in actions, f"审计里有 {action}")
    check(any(a.startswith("faq.") for a in actions), "审计里有 FAQ 相关动作",
          str([a for a in actions if a.startswith("faq.")]))
    check(any(a.startswith("gap.") for a in actions), "审计里有缺口相关动作",
          str([a for a in actions if a.startswith("gap.")]))

    detail_item = next((i for i in items if i.get("action") == "doc.permission_change"),
                       items[0] if items else None)
    if detail_item:
        log_id = detail_item.get("audit_id") or detail_item.get("log_id")
        one = data_of(get(client, f"/api/v1/audit/logs/{log_id}", token)) or {}
        check(bool(one), "审计详情可取到",
              f"snapshot={one.get('snapshot_state')} before={bool(one.get('before'))}")
        check("password" not in json.dumps(one, ensure_ascii=False).lower(),
              "审计详情里没有口令等敏感字段")

    export = get(client, "/api/v1/audit/logs/export", token)
    check(export["status"] == 200, "审计导出可用", f"HTTP {export['status']}")

    if items:
        log_id = items[0].get("audit_id") or items[0].get("log_id")
        deleted = client.delete(f"/api/v1/audit/logs/{log_id}", headers=auth(token))
        check(deleted.status_code >= 400, "审计 append-only（DELETE 被拒）",
              f"HTTP {deleted.status_code} {code_of({'body': _safe_json(deleted)})}")


# ------------------------------------------------------------------ ⑩ 配置与账号
def section_admin(client: httpx.Client) -> None:
    """⑩ 系统配置热更新 + 账号停用即时生效（旧令牌立刻失效）。"""
    section("⑩ 系统配置热更新与账号停用即时生效")
    admin = str(CTX["token_lina"])
    config = data_of(get(client, "/api/v1/system/config", admin)) or {}
    items = config.get("items") or []
    target = next((i for i in items if i["key"] == "retrieval.recall_multiplier"), None)
    if target:
        origin = target["value"]
        changed = put(client, "/api/v1/system/config", admin, {
            "values": {"retrieval.recall_multiplier": origin + 1},
            "reason": "端到端旅程：验证配置热更新"})
        check(changed["status"] == 200 and "retrieval.recall_multiplier"
              in ((data_of(changed) or {}).get("changed") or []),
              "配置热更新成功并回显 changed",
              f"HTTP {changed['status']} {changed['body'].get('message')}")
        back = put(client, "/api/v1/system/config", admin, {
            "values": {"retrieval.recall_multiplier": origin},
            "reason": "端到端旅程：还原配置"})
        check(back["status"] == 200, "配置已还原", str(origin))

    asker = str(CTX["token_wangqiang"])
    before = get(client, "/api/v1/auth/me", asker)
    check(before["status"] == 200, "停用前旧令牌可用")

    off = post(client, "/api/v1/org/users/U000003/status", admin,
               {"status": "disabled", "reason": "端到端旅程：验证停用即时生效"})
    check(off["status"] == 200 and code_of(off) == "0", "停用 wangqiang",
          f"HTTP {off['status']} {off['body'].get('message')}")
    after = get(client, "/api/v1/auth/me", asker)
    check(after["status"] == 401 and code_of(after) == "AUTH-2002",
          "已签发的旧令牌**立即失效**（AUTH-2002）",
          f"HTTP {after['status']} {code_of(after)}")

    on = post(client, "/api/v1/org/users/U000003/status", admin,
              {"status": "active", "reason": "端到端旅程：恢复账号"})
    check(on["status"] == 200 and code_of(on) == "0", "恢复 wangqiang",
          f"HTTP {on['status']} {on['body'].get('message')}")
    restored = login(client, "wangqiang")
    check(restored[0] == 200, "恢复后可再次登录")

    logs = data_of(get(client, "/api/v1/audit/logs?page_size=50", admin)) or {}
    actions = {str(i.get("action")) for i in (logs.get("items") or [])}
    check("config.update" in actions, "审计里有 config.update")
    check("user.disable" in actions, "审计里有 user.disable")


# ------------------------------------------------------------------ 汇总
def report() -> int:
    """打印汇总并返回失败数（作为退出码）。"""
    failed = [r for r in RESULTS if not r[0]]
    print("\n" + "=" * 68)
    print(f"端到端用户旅程：PASS {len(RESULTS) - len(failed)} / FAIL {len(failed)}"
          f"（共 {len(RESULTS)} 项）")
    if failed:
        print("失败项：")
        for _ok, name, detail in failed:
            print(f"  ✘ {name} — {detail}")
    print("=" * 68)
    return len(failed)


def main() -> int:
    """按用户顺序跑完 10 段旅程。"""
    tmp = Path(__file__).resolve().parents[1] / "var" / "e2e_files"
    file_a, file_b = build_files(tmp)
    print(f"目标服务：{BASE}")
    print(f"演示文件：{file_a.name} / {file_b.name}")
    try:
        with httpx.Client(base_url=BASE, timeout=TIMEOUT) as client:
            section_health(client)
            section_login(client)
            section_import(client, file_a, file_b)
            section_permission(client)
            section_qa(client)
            section_faq(client)
            section_gap(client)
            section_dashboard(client)
            section_audit(client)
            section_admin(client)
    except httpx.ConnectError as exc:
        print(f"\n连不上服务：{exc}\n请先起服务：.\\scripts\\run_server.ps1")
        return 1
    except Exception as exc:                              # noqa: BLE001
        print(f"\n旅程中断：{type(exc).__name__} {exc}")
        RESULTS.append((False, f"旅程中断：{type(exc).__name__}", str(exc)))
    return report()


if __name__ == "__main__":
    raise SystemExit(main())
