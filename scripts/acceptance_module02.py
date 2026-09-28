# -*- coding: utf-8 -*-
"""模块 02 组织架构 · 真机验收（**真实 HTTP**，不是 ASGI 直连）。

    # 另开一个终端先把服务跑起来
    .\\scripts\\run_server.ps1
    # 再跑本脚本
    .venv\\Scripts\\python.exe scripts\\acceptance_module02.py

覆盖 AC-02-01~16 里凡能用接口验证的部分，外加模块 01 遗留件（角色功能权限）。
**会改动数据**：脚本自己造临时部门/用户/角色并清理；`kb_permissions` 的那条引用
验完即删。退出码 = 失败项数（0 即全通过）。
"""
from __future__ import annotations

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
    """记一条验收结果。"""
    RESULTS.append((bool(ok), name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def login(client: httpx.Client, username: str, password: str = PASSWORD) -> tuple[int, dict]:
    """登录并返回 `(状态码, 响应体)`。"""
    resp = client.post("/api/v1/auth/login",
                       json={"username": username, "password": password})
    return resp.status_code, resp.json()


def auth(token: str) -> dict[str, str]:
    """Bearer 头。"""
    return {"Authorization": f"Bearer {token}"}


def code_of(resp: httpx.Response) -> str:
    """取响应里的业务错误码。"""
    try:
        return str(resp.json().get("code"))
    except Exception:                                         # noqa: BLE001
        return f"HTTP {resp.status_code}"


def main() -> int:                                            # noqa: PLR0915
    """跑完整个验收清单。"""
    with httpx.Client(base_url=BASE, timeout=20.0) as client:
        ok, body = login(client, "lina")
        check(ok == 200, "lina 登录 200")
        token = body["data"]["access_token"]
        heads = auth(token)
        cleanup: dict[str, list[str]] = {"dept": [], "user": [], "role": []}
        try:
            section_dept(client, heads, cleanup)
            section_user(client, heads, cleanup)
            section_role(client, heads, cleanup)
            section_role_permissions(client, heads, cleanup)
            section_permissions(client, heads)
        finally:
            teardown(client, heads, cleanup)
    failed = [r for r in RESULTS if not r[0]]
    print("\n" + "=" * 62)
    print(f"PASS {len(RESULTS) - len(failed)} / FAIL {len(failed)}（共 {len(RESULTS)} 项）")
    for _ok, name, detail in failed:
        print(f"  ✘ {name} — {detail}")
    print("=" * 62)
    return len(failed)


def section_dept(client: httpx.Client, heads: dict, cleanup: dict) -> None:
    """AC-02-01~05：部门树、重名、成环、级联、三项删除前置。"""
    resp = client.get("/api/v1/org/departments", headers=heads)
    roots = resp.json()["data"]["items"]
    flat = flatten(roots)
    check(resp.status_code == 200 and len(flat) == 8, "AC-02-01 部门树 8 个节点",
          f"{len(flat)}")
    check(all(n["level"] == len(n["path_ids"]) - 1 for n in flat),
          "AC-02-01 path_ids 与 level 一致")
    check(all(n["path_ids"][-1] == n["dept_id"] for n in flat), "path_ids 末位是自身")
    group = next((n for n in flat if n["name"] == "报销组"), None)
    check(group is not None and group["path"] == ["总部", "财务部", "报销组"],
          "二级节点报销组路径正确")

    dup = client.post("/api/v1/org/departments", headers=heads,
                      json={"name": "财务部", "parent_id": "DEPT0001"})
    check(code_of(dup) == "ORG-3001", "AC-02-02 同级重名 → ORG-3001")

    cycle = client.put("/api/v1/org/departments/DEPT0001", headers=heads,
                       json={"parent_id": "DEPT0003"})
    check(code_of(cycle) == "ORG-3004", "AC-02-03 移到子孙下 → ORG-3004")

    child = client.post("/api/v1/org/departments", headers=heads,
                        json={"name": "验收子部门", "parent_id": "DEPT0001"})
    child_id = child.json()["data"]["dept_id"]
    cleanup["dept"].append(child_id)
    grand = client.post("/api/v1/org/departments", headers=heads,
                        json={"name": "验收孙部门", "parent_id": child_id})
    grand_id = grand.json()["data"]["dept_id"]
    cleanup["dept"].append(grand_id)
    moved = client.put(f"/api/v1/org/departments/{child_id}", headers=heads,
                       json={"parent_id": "DEPT0003"})
    after = flatten(client.get("/api/v1/org/departments",
                               headers=heads).json()["data"]["items"])
    grand_node = next(n for n in after if n["dept_id"] == grand_id)
    check(moved.status_code == 200
          and grand_node["path_ids"] == ["DEPT0001", "DEPT0003", child_id, grand_id],
          "AC-02-04 移动后子孙级联重算", str(grand_node["path_ids"]))

    has_child = client.delete("/api/v1/org/departments/DEPT0003", headers=heads)
    check(code_of(has_child) == "ORG-3006", "AC-02-05 有子部门 → ORG-3006")
    has_user = client.delete("/api/v1/org/departments/DEPT0002", headers=heads)
    check(code_of(has_user) == "ORG-3007", "AC-02-05 有用户 → ORG-3007")

    forbidden = client.get("/api/v1/org/departments?include_sub_dept=true", headers=heads)
    check(code_of(forbidden) == "ORG-1004", "G-02 拒收子部门参数 → ORG-1004")


def section_user(client: httpx.Client, heads: dict, cleanup: dict) -> None:
    """AC-02-06~09 / 14：建号、大小写唯一、防自锁、停用即时失效、列表列。

    账号名带时间戳后缀：模块 02 **没有删除用户的接口**（Spec 未定义），
    所以脚本必须自己能重复跑——否则第二次执行会撞 `ORG-2001` 而假红。
    """
    uname = f"acc_{int(time.time()) % 100000}"
    created = client.post("/api/v1/org/users", headers=heads, json={
        "username": uname, "password": "Passw0rd123", "real_name": "验收新人",
        "dept_id": "DEPT0002", "role_ids": ["ROLE0001"]})
    check(created.status_code == 200, "新增用户 200", code_of(created))
    user_id = created.json()["data"]["user_id"]
    cleanup["user"].append(user_id)
    check("Passw0rd123" not in created.text, "AC-02-06 响应不含明文密码")

    dup = client.post("/api/v1/org/users", headers=heads, json={
        "username": uname.upper(), "password": "Passw0rd123", "real_name": "大小写",
        "dept_id": "DEPT0002", "role_ids": ["ROLE0001"]})
    check(code_of(dup) == "ORG-2001", "AC-02-07 大小写变体判冲突")

    weak = client.post("/api/v1/org/users", headers=heads, json={
        "username": uname + "w", "password": "abcdefgh", "real_name": "弱口令",
        "dept_id": "DEPT0002", "role_ids": ["ROLE0001"]})
    check(code_of(weak) == "ORG-1001", "弱口令 → ORG-1001（不是 SYS-1001）")

    listing = client.get("/api/v1/org/users?keyword=验收", headers=heads).json()["data"]
    check(listing["total"] >= 1 and "phone" not in listing["items"][0],
          "AC-02-14 列表可搜且不含联系方式")

    # 停用后已签发令牌立即失效
    # 注意：login() 的默认密码是演示口令，新建用户要显式传自己设的密码
    victim = login(client, uname, "Passw0rd123")
    check(victim[0] == 200, "新用户可登录", code_of(victim[1]) if victim[0] != 200 else "")
    victim_token = victim[1]["data"]["access_token"]
    disabled = client.post(f"/api/v1/org/users/{user_id}/status", headers=heads,
                           json={"status": "disabled"})
    dead = client.get("/api/v1/auth/me", headers=auth(victim_token))
    check(disabled.status_code == 200 and dead.status_code == 401
          and code_of(dead) == "AUTH-2002", "AC-02-08 停用后令牌立即失效")

    self_disable = client.post("/api/v1/org/users/U000001/status", headers=heads,
                              json={"status": "disabled"})
    check(code_of(self_disable) == "ORG-2004", "AC-02-09 不能停用自己 → ORG-2004")

    immutable = client.put(f"/api/v1/org/users/{user_id}", headers=heads,
                           json={"username": "renamed"})
    check(code_of(immutable) == "ORG-1002", "登录账号不可改 → ORG-1002")
    missing = client.put("/api/v1/org/users/U999999", headers=heads,
                         json={"real_name": "谁"})
    check(code_of(missing) == "ORG-3009", "用户不存在 → ORG-3009（Step 5 补录）")


def section_role(client: httpx.Client, heads: dict, cleanup: dict) -> None:
    """AC-02-11~13：内置角色保护、业务角色默认无权限、权限数对账。"""
    rows = {r["code"]: r for r in client.get("/api/v1/org/roles",
                                             headers=heads).json()["data"]["items"]}
    counts = {c: rows[c]["permission_count"] for c in ("asker", "kb_admin", "sys_admin")}
    check(counts == {"asker": 4, "kb_admin": 16, "sys_admin": 22},
          "AC-02-13 内置角色权限数 4/16/22", str(counts))
    check(rows["management"]["permission_count"] == 0
          and rows["management"]["is_system"] is False, "业务角色默认 0 权限且非内置")

    locked = client.put("/api/v1/org/roles/ROLE0001", headers=heads,
                        json={"code": "asker_v2"})
    check(code_of(locked) == "ORG-2007", "AC-02-11 内置角色 code 不可改")
    undeletable = client.delete("/api/v1/org/roles/ROLE0001", headers=heads)
    check(code_of(undeletable) == "ORG-2008", "AC-02-11 内置角色不可删 → ORG-2008")

    created = client.post("/api/v1/org/roles", headers=heads,
                          json={"code": "accept_role", "name": "验收角色"})
    check(created.status_code == 200, "新建业务角色 200", code_of(created))
    role_id = created.json()["data"]["role_id"]
    cleanup["role"].append(role_id)

    bound = client.put("/api/v1/org/users/U000003/roles", headers=heads,
                       json={"role_ids": ["ROLE0001", role_id], "reason": "验收绑定角色"})
    check(bound.status_code == 200 and role_id in bound.json()["data"]["role_ids"],
          "绑定角色（差集）成功")
    in_use = client.delete(f"/api/v1/org/roles/{role_id}", headers=heads)
    check(code_of(in_use) == "ORG-2009", "AC-02-12 仍被使用 → ORG-2009")
    client.put("/api/v1/org/users/U000003/roles", headers=heads,
               json={"role_ids": ["ROLE0001"], "reason": "验收解绑恢复原状"})


def section_role_permissions(client: httpx.Client, heads: dict, cleanup: dict) -> None:
    """模块 01 遗留件：矩阵数据源 + 分配 + R-06 防自锁。"""
    defs = client.get("/api/v1/auth/permissions", headers=heads).json()["data"]["items"]
    check(len(defs) == 34, "功能权限定义 34 条", str(len(defs)))

    owned = client.get("/api/v1/org/roles/ROLE0002/permissions",
                       headers=heads).json()["data"]
    check(len(owned["permission_ids"]) == 16, "kb_admin 已有 16 项")

    role_id = cleanup["role"][0]
    granted = client.put(f"/api/v1/org/roles/{role_id}/permissions", headers=heads,
                         json={"permission_ids": ["PERM0001", "PERM0002"],
                               "reason": "验收给业务角色授两个权限"})
    check(granted.status_code == 200 and granted.json()["data"]["added"] ==
          ["PERM0001", "PERM0002"], "分配功能权限（差集）成功", code_of(granted))

    no_change = client.put(f"/api/v1/org/roles/{role_id}/permissions", headers=heads,
                           json={"permission_ids": ["PERM0001", "PERM0002"],
                                 "reason": "原样再提交一次"})
    check(code_of(no_change) == "AUTH-3001", "无变更 → AUTH-3001")

    grant_perm = next(d["permission_id"] for d in defs if d["code"] == "role:grant")
    current = client.get("/api/v1/org/roles/ROLE0003/permissions",
                         headers=heads).json()["data"]["permission_ids"]
    lockout = client.put("/api/v1/org/roles/ROLE0003/permissions", headers=heads,
                         json={"permission_ids": [p for p in current if p != grant_perm],
                               "reason": "试图拿掉系统管理员的授权能力"})
    check(code_of(lockout) == "AUTH-3002", "R-06 防自锁 → AUTH-3002")


def section_permissions(client: httpx.Client, heads: dict) -> None:
    """权限边界与接口面。"""
    for name in ("zhangwei", "wangqiang"):
        token = login(client, name)[1]["data"]["access_token"]
        for path in ("/api/v1/org/departments", "/api/v1/org/users",
                     "/api/v1/org/roles", "/api/v1/auth/permissions"):
            resp = client.get(path, headers=auth(token))
            check(resp.status_code == 403 and code_of(resp) == "AUTH-2004",
                  f"{name} {path} → 403")
    paths = client.get("/openapi.json").json()["paths"]
    org_paths = sorted(p for p in paths if p.startswith("/api/v1/org"))
    check(len(org_paths) == 10, "组织架构接口 10 个路径", str(len(org_paths)))
    check("/api/v1/org/roles/{role_id}/permissions" in org_paths,
          "矩阵接口挂在 /org 分区下（归属模块 01）")


def flatten(nodes: list[dict]) -> list[dict]:
    """把部门树拍平。"""
    out: list[dict] = []
    for node in nodes:
        out.append(node)
        out.extend(flatten(node["children"]))
    return out


def teardown(client: httpx.Client, heads: dict, cleanup: dict) -> None:
    """清掉本次验收造的临时数据，让脚本可以反复跑。"""
    for user_id in cleanup["user"]:
        client.delete(f"/api/v1/org/users/{user_id}", headers=heads)
        # 没有删除用户的接口（Spec 未定义），改回停用并改名以免占账号
        client.put(f"/api/v1/org/users/{user_id}", headers=heads,
                   json={"real_name": "验收残留"})
    for role_id in cleanup["role"]:
        client.delete(f"/api/v1/org/roles/{role_id}", headers=heads)
    for dept_id in sorted(cleanup["dept"], reverse=True):
        client.delete(f"/api/v1/org/departments/{dept_id}", headers=heads)
    time.sleep(0.2)


if __name__ == "__main__":
    raise SystemExit(main())
