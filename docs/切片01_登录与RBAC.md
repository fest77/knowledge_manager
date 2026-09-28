# knowledge_manager · 垂直切片 ①：登录与功能权限（RBAC）

> **阶段**：Step 5 第一阶段 —— **最小可运行、可验证的端到端垂直切片**（UI → API → DB → 测试）
> **对应模块**：`04_模块Spec/01_登录与功能权限（RBAC）.md`
> **状态**：✅ **66/66 测试通过**（2026-09-24，含深度边界测试）
> **测试教程**：见同目录 [`测试教程.md`](测试教程.md)（逐步可执行，含期望输出与验收清单）
> **规模**：19 个源文件 + 4 个前端/配置/脚本文件

---

## 一、为什么选这个切片

要求是「选一个最简单但贯穿 前端→后端→数据库→测试 的功能」。登录**不是**最省事的，
但它是**唯一能当第一步的切片**：

| 依据 | 说明 |
|---|---|
| **总纲 ER-08** | 除 `/health` 与 `/api/v1/auth/login` 外，**所有**接口必须过 JWT 中间件 |
| 推论 | 列表查询、新增记录等任何其他切片**都硬依赖本切片**；先做别的会立刻卡住 |
| 附带收益 | 它同时验证了 00 模块的全部基建：配置、Mongo 连接、统一响应、错误码注册、中间件、枚举 |

**本切片包含**：`POST /auth/login`、`GET /auth/me`、`GET /health`、零依赖前端登录页、
种子脚本、**66 个测试**（21 集成 + 44 边界/攻击面 + 1 真实起服务的 E2E）。

**本切片明确不含**（后续切片按模块 Spec 逐步加）：
功能权限拦截装饰器 `@require_perm`（本切片没有需要它的接口）、
权限结果缓存（模块 01 §4.3 的 60s TTL，属性能优化且失效入口在 02 模块）、
部门/用户/角色/文档/FAQ/缺口/看板/审计等一切业务模块。

---

## 二、假设与范围

| # | 假设 / 决策 | 依据 | 若不成立的影响 |
|---|---|---|---|
| A1 | Mongo 地址与库名沿用 `.env` 的 `MONGO_URL` / `MONGO_DB_NAME`（`192.168.6.170:27017` / `kb001`） | 项目既有 `.env` | 改 `.env` 即可，代码无需动 |
| A2 | **测试用独立库 `kb001_test`**，绝不碰 `kb001` | 避免污染演示数据 | conftest 会断言库名以 `_test` 结尾，否则直接报错 |
| A3 | JWT 载荷**只放身份**（`sub`/`jti`/`iss`/`iat`/`exp`），不放角色与权限 | 模块 01 §4.1 的裁定：权限写进 token 会导致改角色必须重新登录 | 这是"权限即时生效"的前提，不建议改 |
| A4 | 未做权限缓存，**每请求查库**解析角色与权限 | 缓存的失效入口在 02 模块（尚未实现） | 每请求多 3 次 Mongo 查询；正确性不受影响 |
| A5 | 密码长度 **8~72 字节** | bcrypt 5.0 对 >72 字节**直接抛错**（实测） | 服务端必须先挡住，不能靠库兜底 |
| A6 | 菜单 = 用户拥有且 `menu_path` 非空的权限项，**按 `path` 去重** | 一条菜单就是一个前端路由（实测到重复渲染缺陷） | 已回写模块 01 §3.2 |
| A7 | 前端令牌存 `sessionStorage` | 零依赖单文件约束；关闭标签页即失效 | 生产应改 HttpOnly Cookie |
| A8 | 演示密码默认 `Demo@12345`，可用环境变量 `SEED_DEMO_PASSWORD` 覆盖 | 种子数据需要一个已知口令 | — |

### ⚠️ 与 Spec 的一处实测偏差（重要）

模块 Spec 写的是 **`PyJWT + passlib[bcrypt]`**，但本机实测：

```
passlib 1.7.4 + bcrypt 5.0.0
  → passlib/handlers/bcrypt.py:620  version = _bcrypt.__about__.__version__
     AttributeError: module 'bcrypt' has no attribute '__about__'
  → ctx.hash("Demo@12345") 抛
     ValueError: password cannot be longer than 72 bytes
```

**passlib 1.7.4 与 bcrypt 5.0 不兼容**（passlib 用已被移除的 `__about__` 探版本，
随后其内部自检 `detect_wrap_bug` 用超长口令触发 bcrypt 5.0 的新硬校验）。

**处置**：**直接使用 `bcrypt` 库**，不动已通过 17/17 导入验证的 venv。

- 同一算法、**同一 `$2b$` 哈希格式**、同一 `password_hash` 字段；
- `bcrypt` 本就是 `passlib[bcrypt]` 声明的依赖，**未引入任何新依赖**；
- 日后若升级/修复 passlib，**历史哈希字符串可直接继续校验**，无需迁移。

---

## 三、文件树

```
knowledge_manager/
├── app/
│   ├── __init__.py
│   ├── main.py                      # 应用装配：lifespan / CORS / 异常处理 / 中间件 / 路由 / 静态前端
│   ├── core/
│   │   ├── config.py                # 配置全来自 .env；缺失即启动期 fail-fast
│   │   ├── errors.py                # 错误码注册表 + BizError + 5 个统一异常处理器
│   │   ├── enums.py                 # 按实体各自定义（ER-10），禁止通用 StatusEnum
│   │   ├── logging.py               # 结构化日志 + trace_id contextvar
│   │   ├── response.py              # 统一响应契约（code/message/data/trace_id）
│   │   └── security.py              # bcrypt 哈希 + JWT 签发/校验
│   ├── infra/
│   │   └── mongo.py                 # AsyncMongoClient 生命周期（免 motor）
│   ├── repositories/
│   │   └── auth_repo.py             # 6 个集合的只读访问 + 幂等建索引
│   ├── services/
│   │   └── auth_service.py          # 登录校验顺序 + UserContext + 上下文装载
│   ├── api/
│   │   ├── deps.py                  # current_user 依赖
│   │   ├── schemas_auth.py          # 请求/响应契约模型
│   │   ├── routes_auth.py           # /api/v1/auth/login · /api/v1/auth/me
│   │   └── routes_health.py         # /health
│   └── middleware/
│       └── jwt_auth.py              # trace_id 注入 + JWT 鉴权 + 白名单
├── web/
│   └── index.html                   # 零依赖单文件前端（登录页 + 当前用户面板）
├── scripts/
│   ├── __init__.py
│   └── seed.py                      # 幂等种子：5 部门 / 4 角色 / 34 权限 / 42 授权 / 4 用户
├── tests/
│   ├── conftest.py                  # 测试库隔离 + ASGI 夹具（anyio，不引 pytest-asyncio）
│   ├── test_auth_integration.py     # 21 个集成测试
│   └── test_e2e_smoke.py            # 1 个 E2E：真起 uvicorn + 真实 HTTP
├── pytest.ini
└── .env                             # 含 JWT_SECRET（≥32 字节，PyJWT 2.14 要求）
```

---

## 四、依赖安装

**无需安装任何新依赖**——全部已在 venv 中（216 包环境）：

| 依赖 | 版本 | 用途 |
|---|---|---|
| fastapi / uvicorn | 0.141.1 / 0.53.0 | Web 框架与 ASGI 服务 |
| pydantic | 2.13.5 | 请求/响应模型 |
| pymongo | 4.17.0 | `AsyncMongoClient`（**免装 motor**） |
| PyJWT | 2.14.0 | JWT |
| bcrypt | 5.0.0 | 密码哈希（替代失效的 passlib，见 §二偏差说明） |
| python-dotenv | — | 读 `.env` |
| httpx | 0.28.1 | 测试客户端 |
| pytest + anyio 插件 | 9.1.1 / anyio 4.15.1 | 测试（**不引 pytest-asyncio**） |

`.env` 需含（已写入）：

```
JWT_SECRET=<自动生成的 48 字节随机串>
JWT_ALGORITHM=HS256
JWT_ISSUER=knowledge_manager
JWT_EXPIRE_SECONDS=43200
BCRYPT_ROUNDS=12
KM_LOG_LEVEL=INFO
MONGO_URL=mongodb://192.168.6.170:27017
MONGO_DB_NAME=kb001
```

---

## 五、数据库：迁移与种子

本项目**不用迁移框架**（Mongo 无 schema），改为**幂等建索引 + 幂等 upsert 种子**。

### 5.1 集合与索引（启动时自动确保）

| 集合 | 索引 | 唯一 |
|---|---|:--:|
| `sys_departments` | `parent_id + name`；`status + sort` | ✔ / — |
| `sys_roles` | `code` | ✔ |
| `sys_user_roles` | `user_id + role_id`；`role_id` | ✔ / — |
| `sys_permissions` | `code`；`type + sort` | ✔ / — |
| `sys_role_permissions` | `role_id + permission_id` | ✔ |
| `sys_users` | `username`；`dept_id + status` | ✔ / — |

> 索引建不起来会**直接终止启动**——唯一索引缺失会导致重复账号、重复授权。

### 5.2 种子

```powershell
.venv\Scripts\python.exe scripts\seed.py --drop
```

写入：**5 部门 / 4 角色（3 内置 + 1 业务）/ 34 功能权限 / 42 角色授权 / 4 用户**。

> `42 = 4 + 16 + 22`，与模块 01 §2.3 D 表的权限数对账一致。

**演示账号**（密码默认 `Demo@12345`）：

| 账号 | 姓名 | 部门 | 角色 | 状态 |
|---|---|---|---|---|
| `lina` | 李娜 | 总经办 | `sys_admin` 系统管理员（22 权限） | 启用 |
| `zhangwei` | 张伟 | 人力资源部 | `kb_admin` 知识管理员（16 权限） | 启用 |
| `wangqiang` | 王强 | 技术部 | `asker` 普通用户（4 权限） | 启用 |
| `zhaolei` | 赵磊 | 财务部 | `asker` | **已停用**（用于验证 `AUTH-2002`） |

---

## 六、启动命令

```powershell
cd D:\A_Py_Java\pyFile\knowledge_manager
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8102
```

| 入口 | URL |
|---|---|
| 前端登录页 | http://127.0.0.1:8102/ui/ |
| 交互式接口文档 | http://127.0.0.1:8102/docs |
| 健康检查 | http://127.0.0.1:8102/health |

---

## 七、接口契约

所有响应统一为 `{code, message, data, trace_id}`；成功 `code=0`，失败 `code` 为业务错误码字符串。
每个响应都带 `X-Trace-Id` 头。

### 7.1 `POST /api/v1/auth/login`（白名单，无需令牌）

**请求** `{"username": "lina", "password": "Demo@12345"}`

**成功 200**
```json
{ "code": 0, "message": "ok", "trace_id": "…",
  "data": { "access_token": "eyJ…", "token_type": "Bearer", "expires_in": 43200,
            "user": { "user_id": "U000001", "username": "lina", "real_name": "李娜",
                      "dept_id": "DEPT0005", "dept_name": "总经办",
                      "roles": [{"role_id":"ROLE0003","code":"sys_admin","name":"系统管理员","is_system":true}],
                      "permissions": ["audit:export","audit:read","…共 22 项"],
                      "menus": [{"code":"qa:use","name":"AI 智能问答","path":"#/qa","sort":10}] } } }
```

**错误**

| 码 | HTTP | 触发 |
|---|---|---|
| `SYS-1001` | 400 | Pydantic 参数校验失败（缺字段/长度越界） |
| `AUTH-1001` | 400 | 账号不符合 `^[a-zA-Z][a-zA-Z0-9_]{2,31}$`，或密码长度不在 8~72 字节 |
| `AUTH-2001` | 401 | **账号不存在 或 密码错误**（故意同码，防账号枚举） |
| `AUTH-2002` | 401 | 账号已停用 |
| `AUTH-4001` | 500 | 读取用户/权限数据失败 |
| `AUTH-5001` | 500 | 令牌签发失败 |

### 7.2 `GET /api/v1/auth/me`（需 `Authorization: Bearer <token>`）

**成功 200**：`data` 为与 login 相同的 `user` 对象。

**错误**

| 码 | HTTP | 触发 |
|---|---|---|
| `AUTH-2003` | 401 | 缺令牌 / 格式错 / 签名错 / 过期 / 签发者不符 / 用户已删除 |
| `AUTH-2002` | 401 | **令牌有效但账号已被停用**（每请求都校验，令牌立即失效） |
| `AUTH-4001` | 500 | 装载权限上下文失败 |

### 7.3 `GET /health`（白名单）

```json
{ "code": 0, "data": { "status": "ok", "version": "0.1.0", "server_time": 1790000000,
  "dependencies": { "mongodb": "ok", "milvus": "not_configured",
                    "minio": "not_configured", "embedding_model": "not_configured" } } }
```

> 未接入的依赖如实报 `not_configured`，**不伪装成 `unavailable`**。

---

## 八、数据模型

严格对齐 `01_数据实体/数据实体设计.md` §5 的 E08/E09/E10/E11/E12/E13：

| 实体 | 集合 | 本切片用途 |
|---|---|---|
| E09 用户 | `sys_users` | 凭据（`password_hash`）、`status`、`dept_id`、`last_login_at` |
| E10 角色 | `sys_roles` | `code` / `name` / `is_system` |
| E11 用户角色绑定 | `sys_user_roles` | 用户 → 角色（多对多） |
| E12 功能权限 | `sys_permissions` | `code` / `name` / `type` / `parent_id` / `menu_path` / `sort` |
| E13 角色功能权限 | `sys_role_permissions` | 角色 → 权限（4/16/22） |
| E08 部门 | `sys_departments` | **只读**，用于 `/auth/me` 的 `dept_name`（写入者属 02 模块） |

---

## 九、验证步骤（逐条可执行）

### 9.0 跑全部自动化测试

```powershell
cd D:\A_Py_Java\pyFile\knowledge_manager
.venv\Scripts\python.exe -m pytest -v
```
**期望**：`22 passed`（21 集成 + 1 E2E）。

### 9.1 启动服务

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8102
```
**期望**：日志出现 `MongoDB 已连接`、`索引已确保（6 个集合）`、`knowledge_manager v0.1.0 已就绪`。

### 9.2 健康检查

```powershell
curl.exe -s http://127.0.0.1:8102/health
```
**期望**：`"code":0`，`"mongodb":"ok"`，未接入的依赖为 `"not_configured"`。

### 9.3 无令牌被拦（ER-08）

```powershell
curl.exe -s -o NUL -w "%{http_code}`n" http://127.0.0.1:8102/api/v1/auth/me
curl.exe -s http://127.0.0.1:8102/api/v1/auth/me
```
**期望**：`401`，body 为 `{"code":"AUTH-2003","message":"缺少 Bearer 令牌",…}`。

> ⚠️ **PowerShell 下传 JSON 的写法**：`-d "{\"k\":\"v\"}"` 这种反斜杠转义会被 PowerShell 吞掉，
> 服务端收到非法 JSON 而返回 `SYS-1001 JSON decode error`。**请用下面任一种正确写法**：
> - `curl.exe ... --data-raw '{"username":"lina","password":"Demo@12345"}'`（单引号，**无反斜杠**）
> - `Invoke-RestMethod -Method Post -ContentType "application/json" -Body '{"username":"lina",...}'`

### 9.4 登录成功

```powershell
curl.exe -s -X POST http://127.0.0.1:8102/api/v1/auth/login `
  -H "Content-Type: application/json" `
  --data-raw '{"username":"lina","password":"Demo@12345"}'
```
**期望**：`code=0`；`permissions` **恰为 22 项**；`dept_name` 为「总经办」；
`menus` 为 `#/qa` `#/dashboard` `#/system` `#/audit` 四个；响应中**不含** `password_hash`。

### 9.5 密码错误与账号不存在返回同码（防枚举）

```powershell
curl.exe -s -X POST http://127.0.0.1:8102/api/v1/auth/login -H "Content-Type: application/json" --data-raw '{"username":"lina","password":"WrongPass123"}'
curl.exe -s -X POST http://127.0.0.1:8102/api/v1/auth/login -H "Content-Type: application/json" --data-raw '{"username":"nobody_here","password":"Demo@12345"}'
```
**期望**：两条都是 `401` 且 `"code":"AUTH-2001"`（**完全一致**）。

### 9.6 已停用账号

```powershell
curl.exe -s -X POST http://127.0.0.1:8102/api/v1/auth/login -H "Content-Type: application/json" --data-raw '{"username":"zhaolei","password":"Demo@12345"}'
```
**期望**：`401` + `"code":"AUTH-2002"`。

### 9.7 带令牌取上下文

```powershell
$t = (Invoke-RestMethod -Uri http://127.0.0.1:8102/api/v1/auth/login -Method Post `
        -ContentType "application/json" `
        -Body '{"username":"zhangwei","password":"Demo@12345"}').data.access_token
$me = Invoke-RestMethod -Uri http://127.0.0.1:8102/api/v1/auth/me -Headers @{Authorization="Bearer $t"}
$me.data.real_name            # 期望：张伟
$me.data.dept_name            # 期望：人力资源部
$me.data.permissions.Count    # 期望：16
$me.data.permissions -contains "doc:upload"    # 期望：True
$me.data.permissions -contains "metric:read"   # 期望：False（原型 08 矩阵里 kb_admin 是「—」）
```

### 9.8 角色权限数对账（4 / 16 / 22）

```powershell
foreach ($acc in @("wangqiang","zhangwei","lina")) {
  $u = (Invoke-RestMethod -Uri http://127.0.0.1:8102/api/v1/auth/login -Method Post `
          -ContentType "application/json" `
          -Body ("{0}{1}{2}" -f '{"username":"', $acc, '","password":"Demo@12345"}')).data.user
  "{0,-10} {1,-10} permissions={2,2}  menus={3}" -f $acc, $u.roles[0].code, $u.permissions.Count,
      (($u.menus | ForEach-Object { $_.path }) -join ' ')
}
```
**期望**：

```
wangqiang  asker      permissions= 4  menus=#/qa
zhangwei   kb_admin   permissions=16  menus=#/qa #/docs #/sediment
lina       sys_admin  permissions=22  menus=#/qa #/dashboard #/system #/audit
```

### 9.9 伪造令牌被拒

```powershell
curl.exe -s http://127.0.0.1:8102/api/v1/auth/me -H "Authorization: Bearer not.a.jwt"
```
**期望**：`401` + `"code":"AUTH-2003"`。

### 9.10 统一契约与 trace_id

```powershell
$r = Invoke-WebRequest -Uri http://127.0.0.1:8102/health -UseBasicParsing
($r.Content | ConvertFrom-Json).PSObject.Properties.Name   # 期望：code message data trace_id
$r.Headers["X-Trace-Id"]                                   # 期望：16 位十六进制
```
**期望**：body 恰好 4 个键 `code/message/data/trace_id`；响应头含 16 位 `X-Trace-Id`。

### 9.11 前端联调（人工）

1. 浏览器打开 http://127.0.0.1:8102/ui/ （`/ui/` 是白名单，无需令牌）
2. 用 `lina` / `Demo@12345` 登录 → 应显示用户编号、账号/姓名、部门（含中文名）、
   角色标签（内置角色为绿色）、菜单标签、**22 个功能权限标签**
3. 用 `wangqiang` 登录 → 权限标签应只有 4 个，菜单只有「AI 问答 → #/qa」
4. 用错误密码 → 页面红色提示条应显示 `⚠ 账号或密码错误（AUTH-2001 · <trace_id>）`
5. 登录后按 F5 刷新 → 应通过 `/auth/me` 自动恢复登录态
6. 点「退出登录」→ 回到登录页，令牌从 `sessionStorage` 清除

### 9.12 停用账号 → 令牌立即失效

1. 用 `wangqiang` 登录（保持页面打开）
2. 把 `kb001.sys_users` 中 `username='wangqiang'` 的 `status` 改为 `disabled`
3. 刷新页面 → 应被登出（`AUTH-2002`）

---

## 十、深度自测记录（2026-09-24）

第一阶段交付后又做了一轮**自我审查 + 深度测试**，方法与发现如下。

### 10.1 用了什么手段

| 手段 | 工具 | 结果 |
|---|---|---|
| 静态规范检查 | **自建 AST linter**（venv 里没有任何 linter，且不该为切片引新依赖） | 查出 29 项，已全部修复 |
| 深度边界测试 | 新增 `tests/test_auth_edge.py` | 44 个用例 |
| **变异测试**（验证测试是否真有鉴别力） | 故意注入 5 个 bug | **5/5 全部被测试抓到** |
| 代码 ↔ Spec 一致性 | 自建 `_slice_consistency.py` | 发现 1 处 Spec 缺口，已补 |

### 10.2 发现并修复的问题

| # | 问题 | 类型 | 修复 |
|---|---|---|---|
| 1 | **`uq_username` 的 collation 变了不会重建** | **真 bug（线上才暴露）** | 启动时比对现有索引，不符则先 drop 再建；补 2 个回归测试 |
| 2 | 读回的 `collation` 是 13 字段 SON，与期望的 2 字段整体比较**永远不等** | 真 bug | 只比 `locale` + `strength`；补幂等性测试 |
| 3 | `mongo.close()` 未置空句柄 → 关闭后仍能拿到"可用"的库句柄 | 真 bug（延迟失败） | 置空 `client`/`db`，转为立即 `SYS-4001` |
| 4 | 响应信封在 `errors.py` 与 `jwt_auth.py` **各定义一遍** | 可维护性 | 收敛到 `core/response.py` 的 `envelope()`/`fail()` |
| 5 | 5 处死代码（`Ok`/`PageMeta`/`get_db`/`DEPT_LEVEL_MAX`/`Err.all()`） | 规范性 | 删除；`BuiltinRole` 改为被种子脚本用于一致性断言 |
| 6 | 22 处公共函数/类缺 docstring、1 处行宽超限、1 个未使用 import | 规范性 | 全部补齐 |
| 7 | 总纲只给 `00` 模块分配了前缀，**从未列出 `SYS-*` 具体码** | **Spec 缺口** | 总纲补 §3.1 的 6 条 `SYS-*` 码表 |
| 8 | 项目原有 `main.py` 未标注"已被取代"，两个入口易混淆 | 可维护性 | 补 docstring 并明确指向 `app.main:app` |
| 9 | 我自己的 linter 有 2 个 bug（Python 3.12 的 `format_spec` 也是 `JoinedStr`；`ast.unparse` 的装饰器不带 `@`） | 工具可靠性 | 均已修复——**校验器不可信比没有校验器更糟** |

### 10.3 两个方法论教训（值得记下来）

1. **测试库的"完全隔离"会掩盖生产问题**。
   `kb001_test` 每个用例都 drop 重建索引，所以 #1 那个 collation 问题测试永远发现不了；
   它是在**对真实 `kb001` 跑手工冒烟**时才暴露的。
   → 教训：**每次改索引/迁移，必须对"有历史数据的库"验证一次**，不能只信单元测试。

2. **改完必须确认改的是"正在运行的那份代码"**。
   本轮我在工作区暂存副本上修了 15 个文件，却忘了同步到项目盘，
   于是"测试全绿"测的是暂存区、真实服务跑的是旧版（还因此白排查了两轮）。
   → 教训：**多副本开发必须有一步显式的哈希核对**（已加进交付流程）。

### 10.4 最终验证组合（全部通过）

```
自建 linter（项目盘 + 暂存区）      >>> 无问题 ✔
代码 ↔ Spec 一致性                  >>> 通过 ✔
pytest                              66 passed
真实服务手工冒烟（19 项）             PASS 19 / FAIL 0
```

---

## 十、已知限制（不是 TODO，是**本阶段的范围边界**）

| 项 | 说明 | 何时补 |
|---|---|---|
| `@require_perm` 功能权限拦截 | 本切片唯一受保护接口 `/auth/me` **只需登录**，无需特定权限码，故未实现装饰器（避免未测试的死代码） | 下一个切片（第一个业务接口）时一并加 |
| 权限结果缓存（模块 01 §4.3） | 其失效入口在 02 模块，本切片每请求查库 | 02 模块完成后 |
| `jti` 登出黑名单 | 当前"退出登录"只清客户端令牌 | 需要服务端登出时 |
| 菜单标签 | 取入口权限的 `name`（如「知识单元台账查看」）；原型侧边栏用的是更短的产品化标签（「知识维护」），映射属前端展示层 | 建菜单字典时 |
