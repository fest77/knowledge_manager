# knowledge_manager

知识库管理平台（2.9）。覆盖文档多格式解析切片、全局/部门/角色/个人四维数据权限、
AI 鉴权检索问答、高频 FAQ 聚类沉淀与知识缺口识别。

## 环境
- Python 3.12.8（`.venv`，独立虚拟环境）
- 依赖：`requirements.txt`（PyPI/清华源）+ `requirements-torch.txt`（PyTorch 官方 cu118）
- pip 源：清华（已写入 `.venv\pip.ini`）

## 启动
```
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8102
```
一键启动（会自动清掉占端口的残留进程）：
```
.\scripts\run_server.ps1
```
打开 http://127.0.0.1:8102/ui/ 进入前端，http://127.0.0.1:8102/docs 看接口文档，
http://127.0.0.1:8102/health 看各依赖连通性。

首次启动前需灌演示数据：
```
.venv\Scripts\python.exe scripts\seed.py --drop
```

> **端口被占用时**（开发机上 8102 可能被别的项目占着）：
> `python -m uvicorn app.main:app --host 127.0.0.1 --port 8112`，
> 真机验收脚本可用 `KM_ACCEPT_BASE=http://127.0.0.1:8112` 指向它。
> 看板是"投递式"的：**先提问再看板**，否则看到的是零值。

## 配置
`.env` 原样复制自 `shopkeeper_brain`，与本班其他项目共用同一台虚拟机
`192.168.6.170`（Milvus 19530 / MongoDB 27017 / MinIO 9000）。
本项目自身追加了 `JWT_SECRET` / `BCRYPT_ROUNDS` / `KM_LOG_LEVEL` 等配置段。

> 「模型服务参数」等**运行期可调**的配置存在 Mongo 的 `system_config` 集合里
> （实体 E22），可在界面「系统配置」页修改并热生效，无需重启；
> 密钥、模型路径等**不可在界面改**的项仍只在 `.env`。

## 与 shopkeeper_brain 的关系
本项目**未复制源代码**，只复制了依赖清单与配置地址。
原项目 venv 是 Python 3.10，与本项目的 3.12 不兼容，因此依赖全部重装。
参考实现位置：`..\shopkeeper_brain\knowledge\`（只读参考，勿改动）。

## 开发进度

设计与 Spec 全部在 `Spec_coding_步骤/`（四步：数据实体 / 概要设计 / 原型图 / 模块 Spec）。

| 阶段 | 内容 | 状态 |
|---|---|---|
| Step 1 | 数据实体设计（22 个实体） | ✅ |
| Step 2 | 概要设计（12 条架构决策） | ✅ |
| Step 3 | 低代码原型（8 个 `.pen`，内嵌中文字体） | ✅ |
| Step 4 | 模块 Spec（总纲 + 11 个模块） | ✅ |
| Step 5-① | 垂直切片：登录与功能权限（RBAC） | ✅ [`docs/切片01_登录与RBAC.md`](docs/切片01_登录与RBAC.md) |
| **Step 5-②** | **模块 00 骨架补全**：前端应用壳 + 系统配置服务（E22）+ `@require_perm` | ✅ [`docs/模块00_骨架补全.md`](docs/模块00_骨架补全.md) |
| **Step 5-③** | **模块 10 审计日志**：动作字典 + `AuditService` + 脱敏 + 补偿回放 + 4 只只读接口 + 审计页 | ✅ [`docs/模块10_审计日志.md`](docs/模块10_审计日志.md)（真机 71/0 · `record()` P95 2.06ms） |
| **Step 5-④** | **模块 02 组织架构管理**：部门树（级联路径）/ 用户 / 角色 + 功能权限矩阵 + 权限缓存 | ✅ 真机 43/0 |
| **Step 5-⑤** | **模块 03 知识单元与分类**：台账 + 分类树 + 生命周期 + 切片同步 | ✅ [`docs/模块03_知识单元与分类.md`](docs/模块03_知识单元与分类.md) |
| **Step 5-⑥** | **模块 04 文档导入与向量化**：六阶段流水线 + 8 条接口 + 导入抽屉 + 31 个 `IMP-*` | ✅ [`docs/模块04_文档导入与向量化.md`](docs/模块04_文档导入与向量化.md)（真机 43/43） |
| **Step 5-⑦** | **模块 05 四维数据权限与鉴权引擎**：判定内核（纯函数）+ 批量判定 + 3 条接口 + 四维弹窗 | ✅ [`docs/模块05_四维数据权限与鉴权引擎.md`](docs/模块05_四维数据权限与鉴权引擎.md) |
| **Step 5-⑧** | **模块 06 AI 鉴权问答**：FAQ→召回→鉴权过滤→重排→SSE 流式 + 归属校验 | ✅ [`docs/模块06_AI鉴权问答.md`](docs/模块06_AI鉴权问答.md) |
| **Step 5-⑨** | **模块 07 FAQ 沉淀**：语义聚类挖掘 → 审核 → 发布 → 缓存直出（fail-closed 准入） | ✅ [`docs/模块07_FAQ沉淀.md`](docs/模块07_FAQ沉淀.md) |
| **Step 5-⑩** | **模块 08 知识缺口**：三口径识别 + 归一化合并（幂等三重保证）+ 一键转建 | ✅ [`docs/模块08_知识缺口.md`](docs/模块08_知识缺口.md) |
| **Step 5-⑪** | **模块 09 运营看板**：06 投递 → E20 指标桶（AD-12）→ 降采样查询（AC-09-12）+ 27 个 `MET-*` + 看板页 | ✅ [`docs/模块09_运营看板.md`](docs/模块09_运营看板.md)（真机 85/85） |
| **Step 6** | **整体测试与交付**：656 例全绿 + 交付文档 + 演示脚本 | ✅ 见 [`docs/开发进度与恢复.md`](docs/开发进度与恢复.md) §2 |

> 开发顺序与理由、4 组循环依赖的破解方式见
> `Spec_coding_步骤/04_模块Spec/00_模块划分与边界.md` §4。
> 每一步的现场与恢复步骤见 [`docs/开发进度与恢复.md`](docs/开发进度与恢复.md)。
> **10 个模块全部完成**：00 / 10 / 02 / 03 / 04 / 05 / 06 / 07 / 08 / 09。

### 当前可用的接口

共 **62 个路径 / 75 个端点**（`/system/config` 一个路径带 GET+PUT；看板 5 条全部只读）：

| 模块 | 路径 | 端点 | 说明 |
|---|---:|---:|---|
| 00 公共基础 | 1 | 1 | `/health`（存活 + 依赖连通性 + 审计降级） |
| 01 登录与权限 | 3 | 3 | `/auth/login`（白名单）、`/auth/me`、`/auth/permissions` |
| 00 系统配置 | 1 | 2 | `/system/config` GET 读 + PUT 热更新（逐键留审计） |
| 02 组织架构 | 10 | 16 | 部门树 / 用户 / 角色 / 状态 / 重置口令 / 角色授权 |
| 03 知识单元 | 5 | 7 | 台账（列表·详情·启停·恢复）、去重预检 |
| 03 分类 | 3 | 5 | 分类树（详情 / 新建 / 编辑 / 删除 / 重算） |
| 04 文档导入 | 8 | 8 | 上传 / 批量 / 任务（列表·详情·取消·重试）/ 切片预览 / 原文件 |
| 05 四维数据权限 | 2 | 3 | `/perm/check`（批量判定）、`/perm/{doc_id}`（四维配置 GET+PUT） |
| 06 AI 鉴权问答 | 5 | 5 | `/qa/ask`、SSE 流、会话列表、消息、反馈 |
| 07 FAQ 沉淀 | 9 | 10 | 候选（列表·通过·驳回）、挖掘、缓存（重建·状态）、已发布（列表·详情·启停） |
| 08 知识缺口 | 6 | 6 | 清单 / 详情 / 导出 / 聚合 / 转建 / 忽略 |
| **09 运营看板** | **5** | **5** | `overview` / `trend` / `latency` / `ranking`（`metric:read`）+ `export`（`metric:export`） |
| 10 审计日志 | 4 | 4 | 流水 / 详情 / 动作字典 / 导出（**全部只读**，append-only） |
| **合计** | **62** | **75** | |

> 完整清单以 `tests/test_frontend_shell.py::test_openapi_exposes_only_expected_endpoints`
> 为准（接口面一旦越界，那条用例会直接失败）。
>
> **认证与授权分两处**：JWT 中间件管认证（早于路由，负责白名单）；
> `enforce_perm` 作为**全局依赖**管授权（需要 `request.scope["route"]`）。
> 路由上用 `@require_perm("audit:read")` 声明，OR 语义。
> **唯一例外是模块 09**：它要区分"没权限"（`MET-2002`）与"这个页面对知识管理员不开放"
> （`MET-2003`），所以把判定收进路由层，刻意不用 `@require_perm`。
>
> 登录账号大小写不敏感（`uq_username` 索引带 `collation(strength=2)`）。
> 演示账号：`lina`（系统管理员/22 权限，含看板）、`zhangwei`（知识管理员/16，**无看板**）、
> `wangqiang`（普通用户/4）、`zhaolei`（**已停用**，用于验证 `AUTH-2002`）；
> 密码默认 `Demo@12345`。

### 测试

```
.venv\Scripts\python.exe -m pytest -q
```
共 **659 个用例**，使用独立测试库 `kb001_test`（每个用例前清库重建，不污染 `kb001`）：

| 模块 | 文件（用例数） |
|---|---|
| 01 / 02 | `test_auth_integration` 21 · `test_auth_edge` 44 · `test_permissions` 12 · `test_org_departments` 21 · `test_org_users` 21 · `test_org_roles` 12 · `test_role_permissions` 12 · `test_system_config` 18 |
| 03 / 04 | `test_doc_service` 33 · `test_doc_api` 13 · `test_import_pipeline` 15 · `test_import_repo` 13 · `test_chunk_store` 9 · `test_splitter` 13 · `test_embedding_service` 10 · `test_infra_module04` 12 · `test_parser` 42 |
| 05 / 06 | `test_permission_service` 24 · `test_perm_api` 15 · `test_qa_service` 19 · `test_qa_api` 9 |
| 07 / 08 | `test_faq_service` 29 · `test_gap_service` 28 |
| **09** | **`test_metric_service` 61**（UV 口径 / 降采样 / 降级 / 权限矩阵 / 汇总幂等） |
| 10 | `test_audit_actions` 23 · `test_audit_redaction` 13 · `test_audit_service` 30 · `test_audit_api` 41 · `test_audit_integration` 15 |
| 前端与端到端 | `test_frontend_shell` 28 · `test_e2e_smoke` 1 |

另有辅助校验器（在 DSH 工作区 `knowledge/03_校验脚本/`）：
`_slice_selflint.py`（AST 静态检查，124 个 `.py` → `无问题 ✔`）、
`_slice_consistency.py`（代码 ↔ Spec 一致性 → `检查通过 ✔`）。

> **逐步测试教程见 [`docs/测试教程.md`](docs/测试教程.md)** —— 含每条命令的期望输出与验收清单
> （§14 是模块 09 的专项验证）。
> **真机验收脚本**：`scripts/acceptance_module02.py`（43/0）、
> `scripts/acceptance_module04.py`（43/43）、`scripts/acceptance_module09.py`（**85/85**）、
> `scripts/acceptance_module10.py`（72/0）；
> **端到端用户旅程**：`scripts/e2e_user_journey.py`（**94/94**，见 `docs/测试教程.md` §15）。
> **审计专项**：`docs/模块10_审计日志.md`、`docs/审计存储层护栏.md`（需 Mongo 开鉴权）。

## 代码结构

```
app/
├── main.py              # 应用装配（全局功能权限依赖 + 各模块索引/周期任务/路由）
├── audit/actions.py     # ★ 动作字典：全平台动作名的唯一定义源（40 个）
├── core/                # 配置 / 错误码（218 个）/ 枚举 / 日志 / 安全 / 统一响应 / 权限声明
├── infra/               # mongo.py · milvus.py · minio.py · llm.py · sse.py
│                        # audit_spool.py（补偿文件）· scheduler.py（周期任务）
├── repositories/        # 集合读写（一集合一归属模块，ER-02）
│                        # metric_repo 是 E20 指标桶的唯一写入者（ER-07）
├── services/            # 业务规则唯一实现处（22 个文件：13 个 *_service.py +
│                        # 归一化 / 切分 / 解析 / 缓存 / 脱敏等 8 个支撑件）
├── api/                 # 路由 / 契约模型 / 依赖（认证后的授权）
└── middleware/          # trace_id 注入 + JWT 鉴权（ER-08）
web/
├── index.html           # 应用壳（侧边栏 + 顶栏 + 内容区）
└── js/                  # ES module：入口 / 接口层 / hash 路由 / 壳 / pages（8 个页面）
scripts/
├── seed.py              # 幂等种子（审计与指标桶无数据，只建索引）
├── run_server.ps1       # 一键启动（8102）
├── purge_audit.py       # 审计人工清理（留痕 → 归档 → 确认后删）
├── audit_bench.py       # 审计压测（写入 / 查询 / 导出内存）
├── acceptance_module02 / 04 / 09 / 10.py   # 单模块真机验收（真实 HTTP）
├── e2e_user_journey.py  # ★ 端到端用户旅程（94 项断言，真模型 + 真存储）
└── audit_storage_guard.py  # append-only 存储层护栏自检
tests/                   # 集成 + 边界 + 前端 + E2E（659 个用例）
```

### 审计目录与补偿文件

审计写不进 Mongo 时会**落盘补偿**（`var/audit_spool/pending.jsonl`），
启动时与每 5 分钟自动回放，`extra.spool_id` 唯一稀疏索引保证幂等。
审计**没有种子数据**，保留策略是**永久**（不建 TTL）。

### 指标桶与看板（模块 09）

`metric_buckets` 只由 09 写（ER-07），06 每完成一轮问答投递一次；
**PV 进 `metrics`（可加）**、**UV 进根级 `uv_set`（`$addToSet` 去重）**——两者物理隔离，
这是 AD-12"把 UV 当累加量"这个最常见错误的根本防线。
`1m`/`1h` 桶 30 天后由 TTL **部分索引**清理，`1d` 桶永久；
查询按跨度自动降采样（≤1 天 `1m` / 1~7 天 `1h` / >7 天 `1d`），
粗桶缺失时回退读细桶现算并标 `degraded:true`——**绝不允许静默少算**。
