# -*- coding: utf-8 -*-
"""前端壳（模块 00）的测试。

**能测什么、不能测什么**：没有浏览器，所以这里不测"点击是否跳转"，
而是测**服务端可验证的部分**——静态资源是否真的可取到、内容类型是否正确、
壳与页面模块的契约是否一致、以及"未实现的路由都会落到一个明确说明"这条约定。
真正的人工验收步骤见 `docs/测试教程.md`。
"""
from __future__ import annotations

import pytest

from app.main import app

pytestmark = pytest.mark.anyio

# 前端壳必须提供的模块（与 `web/js/app.js` 的 import 一一对应）
REQUIRED_MODULES = [
    "/ui/js/app.js",
    "/ui/js/api.js",
    "/ui/js/router.js",
    "/ui/js/shell.js",
    "/ui/js/pages/login.js",
    "/ui/js/pages/me.js",
    "/ui/js/pages/system.js",
    "/ui/js/pages/audit.js",
    "/ui/js/pages/system_dept.js",
    "/ui/js/pages/system_user.js",
    "/ui/js/pages/system_role.js",
    "/ui/js/pages/docs.js",
    # 模块 04 / 05 的弹窗模块：它们被 docs.js 静态 import，
    # 少一个就会让整页在浏览器里静默白屏（ES module 的加载失败不会弹提示）
    "/ui/js/pages/import_panel.js",
    "/ui/js/pages/perm_panel.js",
    # 模块 06：问答页（自己解析 SSE —— EventSource 带不上 Authorization 头）
    "/ui/js/pages/qa.js",
    # 模块 07 / 08：知识沉淀页（缺口清单 + FAQ 候选审核）
    "/ui/js/pages/sediment.js",
    # 模块 09：运营看板（自绘 SVG 图表 + CSV 导出）
    "/ui/js/pages/dashboard.js",
]

# 后端会给菜单、但前端此刻还没实现的路径 —— 必须都能落到"尚未开发"的说明
# 模块 09 落地后这个清单**清空**：所有 `sys_permissions.menu_path` 都有页面了
PENDING_PATHS = []


async def test_shell_page_is_served(client):
    resp = await client.get("/ui/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "知识库管理平台" in body
    # 壳只加载入口模块；登录表单已移入 pages/login.js
    assert '<script type="module" src="./js/app.js">' in body
    assert 'id="loginView"' in body and 'id="appView"' in body
    assert "loginForm" not in body, "登录表单应已迁到 pages/login.js，不该留在壳里"


@pytest.mark.parametrize("path", REQUIRED_MODULES)
async def test_frontend_modules_are_served_with_js_content_type(client, path):
    resp = await client.get(path)
    assert resp.status_code == 200, f"{path} 取不到"
    ctype = resp.headers["content-type"]
    assert "javascript" in ctype, f"{path} 的 Content-Type 是 {ctype}，ES module 会加载失败"


async def test_entry_module_wires_the_implemented_routes(client):
    """入口模块必须注册 `#/me` / `#/system` / `#/audit` / `#/docs` / `#/qa` /
    `#/sediment` / `#/dashboard`（最后一个是模块 09）。"""
    body = (await client.get("/ui/js/app.js")).text
    assert 'router.register("#/me"' in body
    assert 'router.register("#/system"' in body
    assert 'router.register("#/audit"' in body
    assert 'router.register("#/docs"' in body
    assert 'router.register("#/qa"' in body
    assert 'router.register("#/sediment"' in body
    assert 'router.register("#/dashboard"' in body
    assert body.count("router.register(") == 7, "新增页面时请同步本测试的预期"
    # 所有后端菜单路径都已实现 → `OWNER_BY_PATH` 里不该再有"待开发"归属
    # （只看**键**，注释里提到 `#/dashboard` 是允许的）
    owner_block = body.split("OWNER_BY_PATH")[1].split("CRUMB_BY_PATH")[0]
    assert '"#/' not in owner_block, f"待开发归属清单应为空：{owner_block}"


async def test_pending_routes_have_a_clear_explanation(client):
    """后端给了菜单但前端未实现的路径，必须能说出"属于哪个模块"。

    这条是刻意的：把用户带到一个空白页，比明确告诉他们"还没做"更糟。
    """
    body = (await client.get("/ui/js/app.js")).text
    for path in PENDING_PATHS:
        assert f'"{path}"' in body, f"{path} 没有归属说明"


async def test_shell_marks_unimplemented_menus_as_pending(client):
    """侧边栏的"待开发"标记逻辑存在于 shell.js（灰显 + 不可点）。"""
    body = (await client.get("/ui/js/shell.js")).text
    assert "isRegistered" in body
    assert "pending" in body and "待开发" in body
    assert 'a.removeAttribute("href")' in body, "未实现的菜单必须摘掉 href，不能可点"


async def test_router_is_hash_based_and_rejects_duplicate_registration(client):
    """hash 路由是零依赖静态托管的必要条件（服务端没有 SPA fallback）。"""
    body = (await client.get("/ui/js/router.js")).text
    assert "hashchange" in body
    assert "window.location.hash" in body
    assert "路由重复注册" in body
    assert "路由必须以 #/ 开头" in body


async def test_api_layer_speaks_the_frozen_envelope(client):
    """接口层只看 `code === 0` 判成功，并把业务错误码/ trace_id 带给界面。"""
    body = (await client.get("/ui/js/api.js")).text
    assert "payload.code === 0" in body
    assert "trace_id" in body
    assert "class ApiError" in body and "toDisplay" in body
    assert "无法连接服务" in body, "网络失败要给可操作的提示，而不是抛原始异常"


async def test_system_page_uses_the_declared_permission_codes(client):
    """页面上的权限码必须与后端 `@require_perm` 声明的一致。

    不一致的症状是「菜单点得进去，接口却 403」——前端以为能读、后端说不行。

    注：FastAPI 0.141 用 `_IncludedRouter` 包装子路由，`app.routes` 里**看不到**
    单个 `APIRoute`，所以这里直接从路由模块取被装饰的函数对象，比依赖框架内部结构稳。
    """
    from app.api import routes_system
    from app.core.permissions import required_perms_of

    class _Route:
        """`required_perms_of` 只用到 `.endpoint`，给个最小替身即可。"""

        def __init__(self, endpoint):
            self.endpoint = endpoint

    read_codes = required_perms_of(_Route(routes_system.get_config))
    write_codes = required_perms_of(_Route(routes_system.put_config))
    assert read_codes, "GET /system/config 应声明权限码"
    assert write_codes, "PUT /system/config 应声明权限码"

    js = (await client.get("/ui/js/pages/system.js")).text
    for code in (*read_codes, *write_codes):
        assert f'"{code}"' in js, f"前端 system.js 没引用后端声明的 {code}"


async def test_openapi_exposes_only_expected_endpoints(client):
    """接口面没有越界：本步新增模块 04 的导入接口（8 条）。"""
    paths = (await client.get("/openapi.json")).json()["paths"]
    assert set(paths) == {
        "/health", "/api/v1/auth/login", "/api/v1/auth/me", "/api/v1/system/config",
        "/api/v1/audit/logs", "/api/v1/audit/logs/export",
        "/api/v1/audit/logs/{log_id}", "/api/v1/audit/actions",
        "/api/v1/org/departments", "/api/v1/org/departments/{dept_id}",
        "/api/v1/org/users", "/api/v1/org/users/{user_id}",
        "/api/v1/org/users/{user_id}/status",
        "/api/v1/org/users/{user_id}/reset-password",
        "/api/v1/org/users/{user_id}/roles",
        "/api/v1/org/roles", "/api/v1/org/roles/{role_id}",
        "/api/v1/org/roles/{role_id}/permissions", "/api/v1/auth/permissions",
        "/api/v1/docs", "/api/v1/docs/dedup-check",
        "/api/v1/docs/{doc_id}", "/api/v1/docs/{doc_id}/toggle",
        "/api/v1/docs/{doc_id}/restore", "/api/v1/categories",
        "/api/v1/categories/recount",
        "/api/v1/categories/{category_id}",
        # ---- 模块 04：导入与向量化 ----
        "/api/v1/import/upload", "/api/v1/import/batch",
        "/api/v1/import/tasks", "/api/v1/import/tasks/{task_id}",
        "/api/v1/import/tasks/{task_id}/cancel",
        "/api/v1/import/tasks/{task_id}/retry",
        "/api/v1/import/docs/{doc_id}/chunks",
        "/api/v1/import/files/{object_key}",
        # ---- 模块 05：四维数据权限与鉴权引擎 ----
        "/api/v1/perm/check", "/api/v1/perm/{doc_id}",
        # ---- 模块 06：AI 鉴权问答 ----
        "/api/v1/qa/ask", "/api/v1/qa/stream/{task_id}", "/api/v1/qa/sessions",
        "/api/v1/qa/sessions/{session_id}/messages",
        "/api/v1/qa/messages/{message_id}/feedback",
        # ---- 模块 07：FAQ 沉淀（两族：/faq/* 候选与挖掘、/faqs/* 已发布） ----
        "/api/v1/faq/candidates",
        "/api/v1/faq/candidates/{candidate_id}/approve",
        "/api/v1/faq/candidates/{candidate_id}/reject",
        "/api/v1/faq/mine", "/api/v1/faq/cache/rebuild", "/api/v1/faq/cache/status",
        "/api/v1/faqs", "/api/v1/faqs/{faq_id}", "/api/v1/faqs/{faq_id}/toggle",
        # ---- 模块 08：知识缺口 ----
        "/api/v1/gaps", "/api/v1/gaps/aggregate", "/api/v1/gaps/export",
        "/api/v1/gaps/{gap_id}", "/api/v1/gaps/{gap_id}/convert",
        "/api/v1/gaps/{gap_id}/ignore",
        # ---- 模块 09：运营看板（5 条，全部只读） ----
        "/api/v1/metrics/overview", "/api/v1/metrics/trend",
        "/api/v1/metrics/latency", "/api/v1/metrics/ranking",
        "/api/v1/metrics/export",
    }, f"接口面超出预期：{sorted(paths)}"
    assert set(paths["/api/v1/system/config"]) == {"get", "put"}
    # 看板**只有读接口**：指标桶的写入只发生在 06 的问答链路里（ER-07）
    for path in paths:
        if path.startswith("/api/v1/metrics/"):
            assert set(paths[path]) == {"get"}, f"{path} 不该有写方法"
    # 审计模块**只有读接口**（append-only 的对外表达）
    for path in ("/api/v1/audit/logs", "/api/v1/audit/logs/export",
                 "/api/v1/audit/logs/{log_id}", "/api/v1/audit/actions"):
        assert set(paths[path]) == {"get"}, f"{path} 不该有写方法"


async def test_audit_page_permissions_and_no_hardcoded_actions(client):
    """审计页的三条硬约束（模块 10 §3.4）。

    ① 页面用的权限码与 `@require_perm` 声明一致；
    ② **动作名不硬编码**——选项来自 `GET /api/v1/audit/actions`；
    ③ 必须展示 `snapshot_state`，否则"快照不完整"会被读成"没改过"。
    """
    from app.api import routes_audit
    from app.core.permissions import required_perms_of

    class _Route:
        def __init__(self, endpoint):
            self.endpoint = endpoint

    codes = []
    for fn in (routes_audit.list_logs, routes_audit.get_log,
               routes_audit.list_actions, routes_audit.export_logs):
        assert required_perms_of(_Route(fn)), f"{fn.__name__} 应声明权限码"
        codes.extend(required_perms_of(_Route(fn)))

    js = (await client.get("/ui/js/pages/audit.js")).text
    for code in codes:
        assert f'"{code}"' in js, f"前端 audit.js 没引用后端声明的 {code}"

    assert '"/api/v1/audit/actions"' in js, "动作下拉必须来自字典接口"
    for action in ("doc.toggle", "doc.permission_change", "config.update"):
        assert action not in js, f"前端不应硬编码动作名：{action}"
    assert "snapshot_state" in js, "列表必须标注快照状态"


def test_audit_page_download_uses_the_shared_api_layer():
    """导出走 `api.js` 的 `download()`：`<a href>` 带不上 JWT，会静默 401。"""
    from app.core.config import settings

    api_js = (settings.web_dir / "js" / "api.js").read_text(encoding="utf-8")
    page_js = (settings.web_dir / "js" / "pages" / "audit.js").read_text(encoding="utf-8")
    assert "export async function download(" in api_js
    assert "Authorization" in api_js
    assert "download(" in page_js


def test_es_module_import_graph_resolves():
    """每条 `import ... from "./x.js"` 的目标必须真实存在。

    浏览器加载 ES module 失败时**不会有明显报错**，只是整页脚本不执行——
    这类"静默 404"最难查，所以用静态检查提前挡住。
    """
    import re

    from app.core.config import settings

    web = settings.web_dir
    assert web.is_dir(), f"前端目录不存在：{web}"

    checked = 0
    for js in sorted(web.rglob("*.js")):
        if "__pycache__" in js.parts:
            continue
        for spec in re.findall(r'from\s+"(\.[^"]+)"', js.read_text(encoding="utf-8")):
            target = (js.parent / spec).resolve()
            assert target.is_file(), (
                f"{js.relative_to(web)} 引用了不存在的模块 {spec}（解析为 {target}）")
            checked += 1
    assert checked >= 6, f"只解析到 {checked} 条 import，import 图可能没被扫到"


def test_app_registers_the_global_perm_dependency():
    """功能权限拦截必须是**全局依赖**——挂在 app 上才不会有路由漏加（ER-09）。

    注：FastAPI 0.141 把依赖挂在 `app.router.dependencies` 上；这里直接看
    OpenAPI 里是否所有业务接口都在（配合上面的 `enforce_perm` 单元测试形成闭环）。
    """
    names = [getattr(d.dependency, "__name__", "") for d in app.router.dependencies]
    assert "enforce_perm" in names, "全局依赖里缺少 enforce_perm"


async def test_system_page_sections_reference_backend_declared_permissions(client):
    """原型 `07` 的四块里，三块组织架构区块引用的权限码必须与后端 `@require_perm` 一致。

    不一致的症状依旧是「菜单点得进去、某个区块却全红」——比整页 403 更难发现。
    """
    from app.api import routes_org
    from app.core.permissions import required_perms_of

    class _Route:
        def __init__(self, endpoint):
            self.endpoint = endpoint

    endpoints = [routes_org.list_departments, routes_org.create_department,
                 routes_org.update_department, routes_org.delete_department,
                 routes_org.list_users, routes_org.create_user, routes_org.update_user,
                 routes_org.set_user_status, routes_org.reset_password,
                 routes_org.set_user_roles, routes_org.list_roles,
                 routes_org.create_role, routes_org.update_role, routes_org.delete_role]
    codes = set()
    for fn in endpoints:
        codes.update(required_perms_of(_Route(fn)))
    assert codes, "组织架构接口应声明权限码"

    js = ((await client.get("/ui/js/pages/system_dept.js")).text
          + (await client.get("/ui/js/pages/system_user.js")).text
          + (await client.get("/ui/js/pages/system_role.js")).text)
    for code in codes:
        assert f'"{code}"' in js, f"前端组织架构区块没引用后端声明的 {code}"


async def test_role_matrix_is_a_subview_not_a_new_permission(client):
    """矩阵（原型 `08`）做成 `#/system` 的子视图，**不新增权限码**。

    菜单项来自 `sys_permissions.menu_path`；为矩阵单开路由就必须新增一个权限码
    （ER-09：路由注解的权限码必须已入库），而它需要的权限与角色列表完全一致。
    本用例把这条设计决策钉住：审计/组织架构用到的权限码必须都还在 34 码字典里。
    """
    from app.core.config import settings

    js = (settings.web_dir / "js" / "pages" / "system_role.js").read_text(encoding="utf-8")
    assert '"/api/v1/auth/permissions"' in js, "矩阵必须从接口取权限定义，不硬编码 34 条"
    assert "role:manage" in js and "role:grant" in js
    # 矩阵是子视图：渲染进页面内的容器，而不是注册新路由
    entry = (await client.get("/ui/js/app.js")).text
    assert "role_matrix" not in entry and "#/matrix" not in entry


async def test_dashboard_page_calls_the_five_metrics_interfaces(client):
    """模块 09 的前端约束（原型 `06`）。

    ① 页面引用的权限码与 `routes_metric` 里的常量一致——本模块**刻意不用
       `@require_perm`**（kb_admin 必须拿到 `MET-2003` 而不是 `AUTH-2004`），
       所以这里比对的是路由模块导出的常量，而不是装饰器；
    ② 五个接口路径都得真的被调用（页面是它们唯一的渲染层）；
    ③ **不引公网 CDN**（D-01）：本地 vendor 缺失时用自绘 SVG 顶，
       但绝不允许写 `https://cdn...`——内网环境会白屏；
    ④ 导出走 `api.js` 的 `download()`（`<a href>` 带不上 JWT，会静默 401）；
    ⑤ `degraded` 必须被渲染成可见提示（Spec §7.2「坏得看得见」）。
    """
    from app.api import routes_metric

    js = (await client.get("/ui/js/pages/dashboard.js")).text
    assert f'"{routes_metric.READ}"' in js, "看板页没引用 metric:read"
    assert f'"{routes_metric.EXPORT}"' in js, "看板页没引用 metric:export（导出按钮要按它置灰）"
    for path in ("/api/v1/metrics/overview", "/api/v1/metrics/trend",
                 "/api/v1/metrics/latency", "/api/v1/metrics/ranking",
                 "/api/v1/metrics/export"):
        assert path in js, f"dashboard.js 没有调用 {path}"
    assert "cdn." not in js and "unpkg" not in js and "jsdelivr" not in js, \
        "看板不得依赖公网 CDN（企业内网会白屏）"
    assert "download(" in js, "导出必须走 api.js 的 download()"
    assert "degraded" in js, "降级状态必须可见"
