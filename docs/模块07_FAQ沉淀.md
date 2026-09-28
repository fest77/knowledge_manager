# 模块 07 · FAQ 沉淀

> 本文档对应 `Spec_coding_步骤/04_模块Spec/07_FAQ沉淀.md`。
> 交付日期：2026-09-25。

---

## 一、本步范围

| # | 职责 | 落点 |
|---|---|---|
| 1 | **挖掘**：读 `qa_logs`（只读）→ 向量聚类 → 产出候选 | `faq_service.mine()` |
| 2 | **审核**：通过并发布（可改写问法/答案）/ 驳回（备注必填，可转建文档） | `approve()` / `reject()` |
| 3 | **已发布 FAQ 管理**：列表 / 编辑 / 缓存生效开关 / 删除 | `list_published()` 等 |
| 4 | **FAQ 缓存**：进程内向量缓存 + 原子重建 + 命中计数 | `app/services/faq_cache.py` |
| 5 | **缓存准入**：关联文档必须**全局可见**（经 05 判定，fail-closed） | `_admission()` |
| 6 | 9 条接口 + 25 个 `FAQ-*` + 8 项配置 | `routes_faq.py` 等 |

### 明确没做（Spec §1.2）

- **不写 `qa_logs`**（06 的实体，ER-06）：只读它做挖掘原料
- **不写 `knowledge_gaps`**（08 的实体，ER-02）：转建一律**投递**给 `GapService`
- **不写 `kb_documents`**：关联文档只做只读校验
- **不做鉴权判定**（05）：缓存准入消费 05 的结论，不自判

---

## 二、变更文件树

```
新增
  app/repositories/faq_repo.py        E16 候选 / E17 FAQ / E18 副本的唯一写入者
  app/services/faq_service.py         挖掘聚类 + 审核发布 + FAQ 管理 + 缓存运维
  app/api/schemas_faq.py              请求/响应模型（列表不含 embedding）
  app/api/routes_faq.py               9 条接口（/faq/* 与 /faqs/* 两族）
  tests/test_faq_service.py           27 例（服务层 + HTTP 契约）

修改
  app/services/faq_cache.py           升级为 numpy 矩阵 + 原子重建 + 命中计数缓冲
  app/services/permission_service.py  +check_visibility()（供 07 判"是否全局可见"）
  app/services/config_service.py      +8 项 faq.* 配置与 getter
  app/core/errors.py                  +25 个 FAQ-* 码（Spec §5 逐条登记）
  app/core/enums.py                   +FaqCandidateStatus（ER-10 独立枚举）
  app/infra/scheduler.py              +Job.delay_first（对齐"每天 03:00"）
  app/main.py                         E16/E17 索引；启动重建缓存；命中计数与定时挖掘任务；挂载路由
  scripts/seed.py                     三张集合进清库清单
  tests/test_frontend_shell.py        接口面 +9 路径
  tests/test_audit_integration.py     错误码前缀计数 +FAQ:25
```

---

## 三、接口契约（两族，权限码不同）

| 族 | 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|---|
| 候选 | GET | `/api/v1/faq/candidates` | `faq:review` | 默认 `pending` + 频次降序 |
| 候选 | POST | `/api/v1/faq/candidates/{id}/approve` | `faq:review` | 可改写问法/答案 → 发布 |
| 候选 | POST | `/api/v1/faq/candidates/{id}/reject` | `faq:review` | 备注必填；可 `convert_to_gap` |
| 挖掘 | POST | `/api/v1/faq/mine` | `faq:review` | **同步返回**；无日志时给 `notice` 而非报错 |
| 缓存 | POST | `/api/v1/faq/cache/rebuild` | `faq:manage` | 原子替换；失败保留旧缓存 |
| 缓存 | GET | `/api/v1/faq/cache/status` | `faq:manage` | 含一致性自检与 `match_p95_ms` |
| 已发布 | GET | `/api/v1/faqs` | `faq:manage` | **不含 `embedding`**（AC-07-32） |
| 已发布 | PUT | `/api/v1/faqs/{id}` | `faq:manage` | 改问法/别名 → 重新向量化 |
| 已发布 | POST | `/api/v1/faqs/{id}/toggle` | `faq:manage` | 停用即移出缓存；启用要重做准入 |
| 已发布 | DELETE | `/api/v1/faqs/{id}` | `faq:manage` | **物理删除**（唯一索引所致） |

> 路径分成 `/faq/*` 与 `/faqs/*` 两族是 **Spec §3 的原文口径**：候选是"过程"、
> FAQ 是"结果"，两族权限码也不同。`sys_admin` **不含** `faq:*`（与原型 `08` 矩阵一致），
> 因此"无权限"由 01 的全局依赖产出 `AUTH-2004`，本模块**不产出功能权限码**（AC-07-29）。

---

## 四、数据模型

| 实体 | 集合 | 关键索引 |
|---|---|---|
| E16 候选 | `faq_candidates` | **唯一 `cluster_key + window_start`**（防同窗重复）+ `status+frequency` |
| E17 FAQ | `faqs` | **唯一 `question_norm`**（防重复发布）+ `enabled+published_at` + 文本索引 |
| E18 副本 | `faq_cache` | 唯一 `faq_id`；只是兜底，**不存 `answer`** |

### 4.1 两个唯一索引的职责完全不同

- `cluster_key + window_start`：只保证**同一窗口内不重复生成**。同簇跨窗口是两次独立产出
  （`frequency` 要重算）；跨窗口的"不打扰"由应用层的「pending 复用 / rejected 抑制」承担。
- `faqs.question_norm`：比较前先 `normalize`（trim + 全角半角 + 去句末标点 + 折叠空白 + 小写）。
  **Mongo 的唯一索引无法对表达式建**，所以落一个派生字段 `question_norm`；
  没有它，"同句带尾部空格"会变成两条 FAQ，各自进缓存、命中同义问题时给出两个答案。

---

## 五、四条本模块独有的铁律

| # | 铁律 | 为什么 |
|---|---|---|
| 1 | **只有"全局可见"文档的 FAQ 才能进缓存**（AC-07-15） | 缓存直出**不经过鉴权**（毫秒级的前提）。放进受限文档的 FAQ = 给所有人开一条绕过四维权限的捷径 |
| 2 | **准入判定经 05，失败 fail-closed**（AC-07-16） | "查不到就放行"会直接把越权内容送进缓存。失败时**可发布但 `enabled=false`** |
| 3 | **07 不写 `qa_logs` / `knowledge_gaps` / `kb_documents`**（AC-07-19/30） | 那是 06/08/03 的实体；转建一律投递 |
| 4 | **`hit_count` 只由 07 写**（AC-07-18） | 它是命中率的分子，谁都能改就没人能解释看板上的数字 |

### 5.1 可复现（AC-07-21）

聚类**完全确定**：日志按 `(asked_at, question)` 排序后贪心归簇 + 因子化代表问法选择
（出现最多；并列取最早）。`faq.mine_seed` 记入审计，但算法不依赖随机数 ——
**比"固定种子"更强**：换个种子结果也一样。

### 5.2 `window_start` 必须对齐到"天"

初版用 `now - days × 86400_000` 当 `window_start`，结果同一天连跑两次挖掘时
毫秒差让唯一索引永远不冲突 → **生成两份候选**（AC-07-01 直接失败）。
修法：对齐到当天 00:00，让"同一批日志算同一个窗口"。

---

## 六、测试

```
tests/test_faq_service.py   27 例
```

| 覆盖点 | 说明 |
|---|---|
| 归一化与簇标识 | 全角/大小写/句末标点/空白都不影响 `cluster_key` |
| 挖掘 | 频次阈值、语义聚类、关联文档反推、幂等（同窗二次运行不重复）、可复现（簇集合一致） |
| 挖掘降级 | 大模型不可用 → 候选仍生成 + `degraded`；向量化不可用 → `FAQ-4001` |
| 抑制 | 已驳回的簇在抑制期内不再生成候选 |
| 发布 | 立刻进缓存；候选侧留痕 + 双向追溯；问法唯一（含空格差异）；无来源 → `FAQ-3004` |
| 准入 | 非全局文档 → 可发布但 `enabled=false`；`toggle(true)` → `FAQ-2001`；判定异常 → fail-closed 且不抛 500 |
| 缓存 | 停用立刻移出；重建后 `cache_size == enabled_count`；并发重建 → `FAQ-3008`；副本不含 `answer` |
| 命中计数 | 累加 + flush 落库（AC-07-17） |
| 编辑 | 改问法重新向量化；改关联文档重做准入（可能自动停用） |
| 边界 | 07 不写 `qa_logs` / `kb_documents` / `knowledge_gaps`；审计快照不含 `embedding` |
| HTTP | 三角色权限（`asker` 与 `sys_admin` 都 403 `AUTH-2004`）、全流程、参数码用 `FAQ-*` |

### 6.1 测试逼出来的三个真问题

| # | 问题 | 症状 | 修法 |
|---|---|---|---|
| 1 | **聚类用字符 n-gram 相似度** | 「生鲜食品破损如何申请退款 / 水果烂了怎么赔 / 到货坏了怎么办」几乎不共享字符 → 各成一簇，阈值形同虚设（AC-07-04 失败） | 改用**语义向量余弦** + 簇心均值；挖掘时批量向量化（失败即 `FAQ-4001`） |
| 2 | **`window_start` 用 `now - N 天`** | 同一天连跑两次挖掘生成两份候选（唯一索引永不冲突） | 对齐到当天 00:00 |
| 3 | **`window_days=0` 被 `or` 吞掉** | 非法入参被静默当成"没传"，本该报 `FAQ-1001` 却正常运行 | 全部改 `is None` 判断 |

---

## 七、验收标准对照（`AC-07-01` ~ `AC-07-32`）

| 编号 | 状态 | 依据 |
|---|---|---|
| AC-07-01 | ✅ | 同窗二次挖掘 `created=0`、`updated=1`、集合条数不变 |
| AC-07-02 | ✅ | `freq_threshold=5` 时低频簇不产出候选 |
| AC-07-03 | ✅ | 相似度阈值参与聚类（语义替身下方向不同即不同簇） |
| AC-07-04 | ✅ | 四条同义问法聚成一个簇、`questions` 齐全、频次正确 |
| AC-07-05 | ✅ | 接口返回 6 列字段；`related_docs` 带标题；空时前端展示「未命中」/「—」 |
| AC-07-06 | ✅ | 改写后的 `question` 与候选的 `representative_question` **各自独立落库** |
| AC-07-07 | ✅ | `reviewed_by`/`reviewed_at`/`review_note` 落库；驳回无备注 → `FAQ-1004` |
| AC-07-08 | ✅ | 重复问法 → `FAQ-3003`（含尾部空格的同句） |
| AC-07-09 | ✅ | 发布后缓存条数 +1（同一测试内立即断言） |
| AC-07-10 | ✅ | 候选级：不同措辞命中同一 FAQ（替身向量口径）；06 侧：`answer_source=faq_cache` 已在模块 06 用例覆盖 |
| AC-07-11 | ⏳ | `match_p95_ms` 已上报；**P95 < 50ms 的压测待交付环境执行**（矩阵乘实现，`N=5000` 预算 1~5ms） |
| AC-07-12 | ✅ | 余弦低于 0.92 不命中（`cosine` 单测 + 阈值逻辑） |
| AC-07-13 | ✅ | 停用后 `cache_size=0` 且 `match()` 返回 `None` |
| AC-07-14 | ✅ | 启动全量重建；`cache_size == enabled_count` |
| AC-07-15 | ✅ | 非全局文档 → 可发布但 `enabled=false`；`toggle(true)` → `FAQ-2001` |
| AC-07-16 | ✅ | 判定异常 → fail-closed（不注入缓存）且不抛 500 |
| AC-07-17 | ✅ | 命中 10 次 → flush → `hit_count=10` |
| AC-07-18 | ✅ | 全项目仅 `faq_repo.inc_hit_counts` 写 `hit_count` |
| AC-07-19 | ✅ | 有用例断言 `qa_logs` / `knowledge_gaps` 计数不变 |
| AC-07-20 | ✅ | `_mining` 互斥 → `FAQ-3007`（用例直接置位验证） |
| AC-07-21 | ✅ | 同参数二次运行的 `cluster_key` 集合一致 |
| AC-07-22 | ✅ | 大模型不可用 → 候选仍生成 + `degraded` |
| AC-07-23 | ✅ | 向量化不可用 → `FAQ-4001` 且 `faqs` 无新增（挖掘侧同样报错） |
| AC-07-24 | ✅ | 重建失败保留旧缓存（`FAQ-4003` 分支 + 异常路径不 `replace_all`） |
| AC-07-25 | ✅ | 原子替换（先构建后换引用）；并发重建返回 `FAQ-3008` |
| AC-07-26 | ⏳ | 多 worker 自检告警未实现（部署固定单 worker；登记为遗留） |
| AC-07-27 | ✅ | 用例断言审计含 `faq.mine` / `faq.publish` 且不含 `embedding` |
| AC-07-28 | ✅ | 审计契约"永不抛异常"→ 业务写操作不受影响 |
| AC-07-29 | ✅ | `asker` 与 `sys_admin` 三个接口均 403 `AUTH-2004` |
| AC-07-30 | ⏳ | 转建**投递**已实现并如实回报 `gap_forwarded=false`；**08 落地后即可为真** |
| AC-07-31 | ✅ | 定时任务注册（`delay_first` 对齐 `faq.mine_cron`）且共用互斥标志 |
| AC-07-32 | ✅ | 列表响应不含 `embedding`（用例断言 + 投影层排除） |

---

## 八、遗留事项（登记，不是 TODO）

| # | 事项 | 影响 | 处置 |
|---|---|---|---|
| 1 | **AC-07-26 多 worker 自检未做** | 多 worker 下进程内缓存不共享 | 本项目固定单 worker（AD-01）；`FAQ-2002` 已定义，需要时在 `startup()` 里读 `WEB_CONCURRENCY` 即可 |
| 2 | **`faq.mine_cron` 只支持「分 时 * * *」** | 复杂 cron 会被降级为"每天同一时刻" | Spec 默认值就是该形态；不引依赖 |
| 3 | **转建缺口依赖 08** | `convert_to_gap` 目前恒返回 `gap_forwarded=false` | 08 落地后 `_forward_to_gap` 自动生效（已按 Spec 的降级路径实现） |
| 4 | **挖掘的关联文档只来自 `allowed_chunks`** | 若问答未记录放行切片则候选无来源 | 06 已保证三列表齐全（AC-06-06）；无来源的簇按 Spec 走"驳回 / 转建文档" |
| 5 | **`faq_cache` 副本的 `model` 字段写死 `"BGE-M3"`** | 换模型时副本标记会陈旧 | 副本只用于排查，不影响正确性 |
| 6 | ~~**关联文档反推的实现与真实数据形状不符**~~ | ✅ **已闭环**（2026-09-25 端到端旅程发现并修复）：`allowed_chunks` 在生产里是**切片主键列表**（`doc_id` 只在 `recalled_chunks` 快照里），而旧实现直接把元素当文档号 → 候选的 `related_docs` 变成 `["None"]`，**审核通过永久报 `FAQ-3004 关联知识单元不存在或已删除：None`**，一条 FAQ 也发不出去 | `_doc_ids_of()` 改为**先建 `chunk_id → doc_id` 映射再按放行集合过滤**，并统一丢弃 `None/"None"/"null"`；`_read_logs` 补投影 `recalled_chunks`；新增两条回归护栏（真机形状 + 真形状下能审过）。**旧夹具的 `[{"chunk_id":1,"doc_id":doc}]` 形状与生产不一致，这是它能躲过 27 个用例的原因** |
