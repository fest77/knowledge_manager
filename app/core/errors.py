# -*- coding: utf-8 -*-
"""错误码注册与统一异常处理。

错误码格式 `{PREFIX}-{4位}`，前缀**一模块一前缀**（总纲 ER-01）。
段位：1xxx 参数 · 2xxx 鉴权 · 3xxx 资源/状态 · 4xxx 依赖 · 5xxx 内部。

已登记前缀：`SYS`（00 公共基础，清单见总纲 §3.1）、`AUTH`（01 登录与功能权限，
清单见该模块 Spec §5）、`AUD`（10 审计日志，清单见该模块 Spec §5）。
后续模块按总纲 §3 各自追加自己的前缀。
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import ConfigError
from app.core.logging import logger
from app.core.response import fail


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    """一个错误码的完整规范：码 / HTTP 状态 / 默认消息。"""

    code: str
    http: int
    message: str


class Err:
    """错误码常量表（代码里只引用这里，不写裸字符串）。"""

    # ---- 00 公共基础 / SYS ----
    SYS_PARAM_INVALID = ErrorSpec("SYS-1001", 400, "参数校验失败")
    SYS_NOT_FOUND = ErrorSpec("SYS-3001", 404, "接口不存在")
    SYS_METHOD_NOT_ALLOWED = ErrorSpec("SYS-3002", 405, "请求方法不允许")
    SYS_DB_UNAVAILABLE = ErrorSpec("SYS-4001", 503, "数据库不可用")
    SYS_INTERNAL = ErrorSpec("SYS-5001", 500, "服务器内部错误")

    # ---- 01 登录与功能权限 / AUTH ----
    AUTH_PARAM_INVALID = ErrorSpec("AUTH-1001", 400, "账号或密码格式不合法")
    AUTH_GRANT_PARAM = ErrorSpec("AUTH-1002", 400, "授权参数不合法")
    AUTH_REASON_REQUIRED = ErrorSpec("AUTH-1003", 400, "变更原因必填且不少于 5 字")
    AUTH_CREDENTIALS = ErrorSpec("AUTH-2001", 401, "账号或密码错误")
    AUTH_DISABLED = ErrorSpec("AUTH-2002", 401, "账号已停用")
    AUTH_TOKEN_INVALID = ErrorSpec("AUTH-2003", 401, "登录态无效或已过期")
    AUTH_PERM_DENIED = ErrorSpec("AUTH-2004", 403, "功能权限不足")
    AUTH_NO_GRANT_LEFT = ErrorSpec("AUTH-2005", 403, "不允许移除系统管理员的权限分配权")
    AUTH_NO_CHANGE = ErrorSpec("AUTH-3001", 409, "权限未发生变化")
    AUTH_LOCKOUT = ErrorSpec("AUTH-3002", 409, "该操作会使系统管理员失去授权能力")
    AUTH_ROLE_NOT_FOUND = ErrorSpec("AUTH-3003", 404, "角色不存在")
    AUTH_PERM_NOT_FOUND = ErrorSpec("AUTH-3004", 404, "权限项不存在")
    AUTH_STORE_FAILED = ErrorSpec("AUTH-4001", 500, "权限数据读取失败")
    AUTH_SIGN_FAILED = ErrorSpec("AUTH-5001", 500, "令牌签发失败")

    # ---- 02 组织架构 / ORG（清单来源：模块 02 Spec §5）----
    ORG_PWD_WEAK = ErrorSpec("ORG-1001", 400, "密码强度不足")
    ORG_USERNAME_IMMUTABLE = ErrorSpec("ORG-1002", 400, "登录账号不可修改")
    ORG_REASON_REQUIRED = ErrorSpec("ORG-1003", 400, "变更原因必填且不少于 5 字")
    ORG_SUB_DEPT_REFUSED = ErrorSpec("ORG-1004", 400, "不接受子部门参数")
    ORG_USERNAME_TAKEN = ErrorSpec("ORG-2001", 409, "登录账号已存在")
    ORG_ROLE_NOT_FOUND = ErrorSpec("ORG-2002", 400, "角色不存在")
    ORG_LAST_ADMIN = ErrorSpec("ORG-2003", 409, "不能停用最后一个系统管理员")
    ORG_SELF_DISABLE = ErrorSpec("ORG-2004", 409, "不能停用当前登录账号")
    ORG_ROLE_REQUIRED = ErrorSpec("ORG-2005", 400, "用户至少需要保留一个角色")
    ORG_ROLE_CODE_TAKEN = ErrorSpec("ORG-2006", 409, "角色编码已存在或与内置角色冲突")
    ORG_ROLE_CODE_LOCKED = ErrorSpec("ORG-2007", 400, "内置角色的编码不可修改")
    ORG_SYSTEM_ROLE_UNDELETABLE = ErrorSpec("ORG-2008", 403, "内置角色不可删除")
    ORG_ROLE_IN_USE = ErrorSpec("ORG-2009", 409, "该角色仍被用户使用")
    ORG_DEPT_NAME_TAKEN = ErrorSpec("ORG-3001", 409, "同级已存在同名部门")
    ORG_DEPT_NOT_FOUND = ErrorSpec("ORG-3002", 404, "部门不存在或已停用")
    ORG_DEPT_TOO_DEEP = ErrorSpec("ORG-3003", 400, "部门层级超出上限（5 层）")
    ORG_DEPT_CYCLE = ErrorSpec("ORG-3004", 409, "不能把部门移动到自己的子部门下")
    ORG_DEPT_HAS_ACTIVE_USER = ErrorSpec("ORG-3005", 409, "部门下仍有在用用户")
    ORG_DEPT_HAS_CHILD = ErrorSpec("ORG-3006", 409, "该部门下仍有子部门")
    ORG_DEPT_HAS_USER = ErrorSpec("ORG-3007", 409, "该部门下仍有用户")
    ORG_DEPT_REFERENCED = ErrorSpec("ORG-3008", 409, "该部门被知识权限引用")
    # Step 5 开发期补录（Spec §5 初稿漏了"用户不存在"这一表达）：已同步回模块 02 Spec
    ORG_USER_NOT_FOUND = ErrorSpec("ORG-3009", 404, "用户不存在")
    ORG_CASCADE_FAILED = ErrorSpec("ORG-4001", 500, "部门路径级联更新失败")
    ORG_ROLE_CLEANUP_FAILED = ErrorSpec("ORG-4002", 500, "角色权限清理失败")

    # ---- 03 知识单元与分类 / DOC（清单来源：模块 03 Spec §5）----
    DOC_PARAM_INVALID = ErrorSpec("DOC-1001", 400, "请求参数不合法")
    DOC_TITLE_INVALID = ErrorSpec("DOC-1002", 400, "文档标题不合法")
    DOC_CATEGORY_NAME_INVALID = ErrorSpec("DOC-1003", 400, "分类名称不合法")
    DOC_CATEGORY_TOO_DEEP = ErrorSpec("DOC-1004", 400, "分类层级超出上限（5 层）")
    DOC_FILE_EXT_UNSUPPORTED = ErrorSpec("DOC-1005", 400, "不支持的文件格式")
    DOC_TAGS_INVALID = ErrorSpec("DOC-1006", 400, "标签数量或长度超出限制")
    DOC_DEDUP_ITEMS_INVALID = ErrorSpec("DOC-1007", 400, "批量去重检查条目数超限")
    DOC_NO_PRINCIPAL = ErrorSpec("DOC-2001", 401, "登录态无效或缺失")
    DOC_PERM_UNREGISTERED = ErrorSpec("DOC-2002", 403, "功能权限码未注册")
    DOC_NOT_FOUND = ErrorSpec("DOC-3001", 404, "知识单元不存在")
    DOC_SOFT_DELETED = ErrorSpec("DOC-3002", 409, "知识单元已软删除")
    DOC_HASH_IMPORTING = ErrorSpec("DOC-3003", 409, "同哈希文件正在导入中")
    DOC_HASH_SOFT_DELETED = ErrorSpec("DOC-3004", 409, "同哈希文件已软删除，需先恢复")
    DOC_CATEGORY_HAS_CHILD = ErrorSpec("DOC-3005", 409, "该分类下仍有子分类")
    DOC_CATEGORY_HAS_DOC = ErrorSpec("DOC-3006", 409, "该分类下仍有知识单元")
    DOC_CATEGORY_NAME_TAKEN = ErrorSpec("DOC-3007", 409, "同级已存在同名分类")
    DOC_CATEGORY_CYCLE = ErrorSpec("DOC-3008", 409, "不能把分类移动到自己的子分类下")
    DOC_CATEGORY_NOT_FOUND = ErrorSpec("DOC-3009", 404, "知识分类不存在")
    DOC_IMPORT_NOT_DONE = ErrorSpec("DOC-3010", 409, "知识单元仍在导入中，暂不可启用")
    DOC_MOVE_TOO_DEEP = ErrorSpec("DOC-3011", 409, "分类移动后层级超出上限（5 层）")
    DOC_BUSY_IMPORTING = ErrorSpec("DOC-3012", 409, "知识单元正在导入中，暂不可编辑或删除")
    DOC_STORE_FAILED = ErrorSpec("DOC-4001", 500, "知识单元存储访问失败")
    DOC_CATEGORY_STORE_FAILED = ErrorSpec("DOC-4002", 500, "知识分类存储访问失败")
    DOC_CHUNK_SYNC_FAILED = ErrorSpec("DOC-4003", 503, "切片启停同步失败")
    DOC_DEDUP_INDEX_MISSING = ErrorSpec("DOC-4004", 500, "去重索引未就绪")
    DOC_ID_FAILED = ErrorSpec("DOC-5001", 500, "知识单元编号生成失败")
    DOC_CASCADE_FAILED = ErrorSpec("DOC-5002", 500, "分类物化路径级联重算失败")
    DOC_RECOUNT_FAILED = ErrorSpec("DOC-5003", 500, "分类文档计数重算失败")

    # ---- 04 文档导入 / IMP（清单来源：模块 04 Spec §5）----
    # 分段与全局约定一致：1xxx 参数 / 2xxx 越权 / 3xxx 资源与状态 / 4xxx 依赖 / 5xxx 内部
    IMP_FILE_MISSING = ErrorSpec("IMP-1001", 400, "上传文件缺失或为空")
    IMP_FILE_TOO_LARGE = ErrorSpec("IMP-1002", 400, "文件大小超出上限")
    IMP_EXT_UNSUPPORTED = ErrorSpec("IMP-1003", 400, "不支持的文件格式")
    IMP_BATCH_TOO_LARGE = ErrorSpec("IMP-1004", 400, "批量导入数量或总量超出上限")
    IMP_FILENAME_INVALID = ErrorSpec("IMP-1005", 400, "文件名非法")
    IMP_CONTENT_MISMATCH = ErrorSpec("IMP-1006", 400, "文件内容与扩展名不符")
    IMP_QUERY_INVALID = ErrorSpec("IMP-1007", 400, "查询参数不合法")
    IMP_BATCH_MISMATCH = ErrorSpec("IMP-1008", 400, "批量参数不匹配")
    IMP_UPLOAD_INCOMPLETE = ErrorSpec("IMP-1009", 400, "上传数据不完整")
    IMP_TASK_FORBIDDEN = ErrorSpec("IMP-2001", 403, "无权操作他人的导入任务")
    IMP_OBJECT_KEY_MISMATCH = ErrorSpec("IMP-2002", 403, "文件对象与知识单元不匹配")
    IMP_TASK_NOT_FOUND = ErrorSpec("IMP-3001", 404, "导入任务不存在")
    IMP_TASK_STATE_INVALID = ErrorSpec("IMP-3002", 409, "任务当前状态不允许该操作")
    IMP_HASH_IMPORTING = ErrorSpec("IMP-3003", 409, "该文件正在导入中")
    IMP_DOC_UNAVAILABLE = ErrorSpec("IMP-3004", 404, "知识单元不存在或已删除")
    IMP_RETRY_EXHAUSTED = ErrorSpec("IMP-3005", 409, "重试次数已达上限")
    IMP_CHUNKS_NOT_READY = ErrorSpec("IMP-3006", 409, "导入进行中，切片暂不可查看")
    IMP_CATEGORY_NOT_FOUND = ErrorSpec("IMP-3007", 404, "知识分类不存在")
    IMP_QUEUE_FULL = ErrorSpec("IMP-3008", 429, "导入队列已满，请稍后重试")
    IMP_PARSE_FAILED = ErrorSpec("IMP-4001", 500, "PDF 解析失败")
    IMP_STORAGE_FAILED = ErrorSpec("IMP-4002", 500, "文件存储写入失败")
    IMP_EMBEDDING_FAILED = ErrorSpec("IMP-4003", 500, "向量化失败")
    IMP_VECTOR_STORE_FAILED = ErrorSpec("IMP-4004", 500, "向量库写入失败")
    IMP_META_STORE_FAILED = ErrorSpec("IMP-4005", 500, "导入元数据写入失败")
    IMP_TASK_TIMEOUT = ErrorSpec("IMP-4006", 504, "导入任务超时")
    IMP_PREFLIGHT_FAILED = ErrorSpec("IMP-4007", 503, "依赖服务预检失败")
    IMP_INTERNAL = ErrorSpec("IMP-5001", 500, "导入流程内部异常")
    IMP_STATE_MACHINE = ErrorSpec("IMP-5002", 500, "任务状态机非法迁移")
    IMP_RESTART_INTERRUPTED = ErrorSpec("IMP-5003", 500, "任务因服务重启而中断")
    IMP_LEDGER_BACKFILL_FAILED = ErrorSpec("IMP-5004", 500, "切片已写入但台账回填失败")
    IMP_HASH_FAILED = ErrorSpec("IMP-5005", 500, "文件哈希与查重失败")

    # ---- 05 四维数据权限 / PERM（清单来源：模块 05 Spec §5）----
    # 注意 2002 的措辞：它说的是**功能权限**不足。四维数据权限的"不可读"不是错误，
    # 而是 `allowed=false` 的正常判定结果——两者混用会让"无权看这篇知识"
    # 变成 403 报错，而正确行为是"检索里过滤掉它，并给出受限提示"。
    PERM_REASON_REQUIRED = ErrorSpec("PERM-1001", 400, "变更原因必填且不少于 5 字")
    PERM_DEPT_INVALID = ErrorSpec("PERM-1002", 400, "部门不存在或已停用")
    PERM_ROLE_INVALID = ErrorSpec("PERM-1003", 400, "角色不存在")
    PERM_USER_INVALID = ErrorSpec("PERM-1004", 400, "用户不存在")
    PERM_TOO_MANY = ErrorSpec("PERM-1005", 400, "授权项超出数量上限")
    PERM_PARAM_INVALID = ErrorSpec("PERM-1006", 400, "请求参数格式不合法")
    PERM_IMPERSONATE_DENIED = ErrorSpec("PERM-2001", 403, "无权代他人查询鉴权结果")
    PERM_MANAGE_DENIED = ErrorSpec("PERM-2002", 403, "功能权限不足")
    PERM_DOC_NOT_FOUND = ErrorSpec("PERM-3001", 404, "知识单元不存在或已删除")
    PERM_RECORD_NOT_FOUND = ErrorSpec("PERM-3002", 404, "权限记录不存在")
    PERM_SAVE_FAILED = ErrorSpec("PERM-4001", 500, "权限保存失败")
    PERM_SUMMARY_FAILED = ErrorSpec("PERM-4002", 500, "权限摘要刷新失败")
    PERM_ENGINE_UNAVAILABLE = ErrorSpec("PERM-5001", 500, "鉴权判定服务不可用")

    # ---- 06 AI 鉴权问答 / QA（清单来源：模块 06 Spec §5）----
    QA_QUESTION_INVALID = ErrorSpec("QA-1001", 400, "提问内容不合法")
    QA_FEEDBACK_INVALID = ErrorSpec("QA-1002", 400, "反馈参数不合法")
    QA_SESSION_ID_INVALID = ErrorSpec("QA-1003", 400, "会话编号格式不合法")
    QA_SESSION_FORBIDDEN = ErrorSpec("QA-2001", 403, "无权访问该会话")
    # 2002 是 401 而不是 403：它表达的是"根本没拿到登录上下文"，
    # 与 2001/2003 的"登录了但这不是你的"是两件事（标注 1：不允许未登录问答）
    QA_CONTEXT_MISSING = ErrorSpec("QA-2002", 401, "用户上下文缺失")
    QA_STREAM_FORBIDDEN = ErrorSpec("QA-2003", 403, "无权订阅该问答流")
    QA_SESSION_NOT_FOUND = ErrorSpec("QA-3001", 404, "会话不存在")
    QA_MESSAGE_NOT_FOUND = ErrorSpec("QA-3002", 404, "消息不存在")
    QA_SESSION_FULL = ErrorSpec("QA-3003", 409, "会话已超过消息上限")
    QA_MESSAGE_SAVE_FAILED = ErrorSpec("QA-4001", 500, "消息保存失败")
    QA_LLM_FAILED = ErrorSpec("QA-4002", 502, "大模型调用失败，请稍后重试")
    QA_RETRIEVAL_UNAVAILABLE = ErrorSpec("QA-4003", 503, "向量检索不可用")
    QA_RERANK_UNAVAILABLE = ErrorSpec("QA-4004", 503, "Rerank 模型不可用")
    QA_EMBEDDING_UNAVAILABLE = ErrorSpec("QA-4005", 503, "Embedding 模型不可用")
    QA_LOG_WRITE_FAILED = ErrorSpec("QA-4006", 500, "问答日志写入失败")

    # ---- 07 FAQ 沉淀 / FAQ（清单来源：模块 07 Spec §5）----
    FAQ_WINDOW_INVALID = ErrorSpec("FAQ-1001", 400, "挖掘时间窗参数非法")
    FAQ_QUESTION_INVALID = ErrorSpec("FAQ-1002", 400, "标准问法不合法")
    FAQ_ANSWER_INVALID = ErrorSpec("FAQ-1003", 400, "标准答案不合法")
    FAQ_NOTE_REQUIRED = ErrorSpec("FAQ-1004", 400, "备注必填且不少于 5 字")
    FAQ_ALIAS_INVALID = ErrorSpec("FAQ-1005", 400, "同义问法不合法")
    FAQ_THRESHOLD_INVALID = ErrorSpec("FAQ-1006", 400, "挖掘阈值参数非法")
    FAQ_QUERY_INVALID = ErrorSpec("FAQ-1007", 400, "分页或筛选参数非法")
    # 2001 只在本模块**消费** 05 的判定结论（ER-03），不自己判权限
    FAQ_NOT_GLOBAL = ErrorSpec("FAQ-2001", 403, "关联知识单元非全局可见，禁止进入缓存")
    FAQ_CACHE_OWNERSHIP = ErrorSpec("FAQ-2002", 403, "FAQ 缓存所有权校验失败")
    FAQ_CANDIDATE_NOT_FOUND = ErrorSpec("FAQ-3001", 404, "候选不存在")
    FAQ_CANDIDATE_REVIEWED = ErrorSpec("FAQ-3002", 409, "该候选已审核")
    FAQ_QUESTION_TAKEN = ErrorSpec("FAQ-3003", 409, "标准问法已存在")
    FAQ_SOURCE_INVALID = ErrorSpec("FAQ-3004", 409, "关联知识单元缺失或无效，不能发布")
    FAQ_NOT_FOUND = ErrorSpec("FAQ-3005", 404, "已发布 FAQ 不存在")
    FAQ_WINDOW_CONFLICT = ErrorSpec("FAQ-3006", 409, "该时间窗的候选已存在")
    FAQ_MINING_RUNNING = ErrorSpec("FAQ-3007", 409, "已有挖掘任务在运行")
    FAQ_CACHE_REBUILDING = ErrorSpec("FAQ-3008", 409, "缓存正在重建中")
    # 3009 是 **200**：它是业务告警（"这个窗口里没有可用日志"），不是失败
    FAQ_NO_LOGS = ErrorSpec("FAQ-3009", 200, "时间窗内无可用问答日志")
    FAQ_EMBEDDING_FAILED = ErrorSpec("FAQ-4001", 500, "问题向量化失败")
    FAQ_MINE_FAILED = ErrorSpec("FAQ-4002", 500, "挖掘失败：日志读取或聚类异常")
    FAQ_CACHE_REBUILD_FAILED = ErrorSpec("FAQ-4003", 500, "缓存重建失败")
    FAQ_DRAFT_FAILED = ErrorSpec("FAQ-4004", 503, "参考答案草案生成失败")
    FAQ_CANDIDATE_INTERNAL = ErrorSpec("FAQ-5001", 500, "候选生成内部错误")
    FAQ_CACHE_INCONSISTENT = ErrorSpec("FAQ-5002", 500, "缓存结构与 faqs 不一致")
    FAQ_JOB_REGISTER_FAILED = ErrorSpec("FAQ-5003", 500, "定时任务注册失败")

    # ---- 08 知识缺口 / GAP（清单来源：模块 08 Spec §5）----
    GAP_QUERY_INVALID = ErrorSpec("GAP-1001", 400, "查询参数不合法")
    GAP_WINDOW_INVALID = ErrorSpec("GAP-1002", 400, "时间窗口参数不合法")
    GAP_TITLE_INVALID = ErrorSpec("GAP-1003", 400, "转建标题不合法")
    GAP_CATEGORY_INVALID = ErrorSpec("GAP-1004", 400, "指定的分类不存在或已停用")
    GAP_EXPORT_TOO_LARGE = ErrorSpec("GAP-1005", 400, "导出条数超出上限")
    # 2001 由 01 的全局依赖产出（本模块只负责在自己的路由上声明权限码）；
    # 服务层兜底时用它，保证"从哪个模块越权"在日志里一眼可辨（ER-01）
    GAP_PERM_DENIED = ErrorSpec("GAP-2001", 403, "功能权限不足")
    GAP_ALREADY_CONVERTED = ErrorSpec("GAP-3001", 409, "该缺口已转建")
    GAP_NOT_FOUND = ErrorSpec("GAP-3002", 404, "知识缺口不存在")
    GAP_ALREADY_IGNORED = ErrorSpec("GAP-3003", 409, "该缺口已被忽略")
    GAP_STALE = ErrorSpec("GAP-3004", 409, "缺口数据已过期，请刷新清单")
    GAP_AGGREGATING = ErrorSpec("GAP-3005", 409, "缺口聚合任务正在运行中")
    GAP_DOC_CREATE_FAILED = ErrorSpec("GAP-4001", 500, "知识单元占位创建失败")
    GAP_TASK_CREATE_FAILED = ErrorSpec("GAP-4002", 500, "导入任务创建失败")
    GAP_LOGS_UNAVAILABLE = ErrorSpec("GAP-4003", 503, "问答日志不可读")
    GAP_CATEGORY_LOOKUP_FAILED = ErrorSpec("GAP-4004", 503, "建议分类反查不可用")
    GAP_STATE_WRITE_FAILED = ErrorSpec("GAP-5001", 500, "缺口状态回写失败")
    GAP_AGGREGATE_FAILED = ErrorSpec("GAP-5002", 500, "缺口聚合任务内部异常")
    GAP_EXPORT_FAILED = ErrorSpec("GAP-5003", 500, "缺口清单导出失败")

    # ---- 09 运营看板 / MET（清单来源：模块 09 Spec §5）----
    # 注意 2003 的语义：知识管理员**不是**"权限不足"，而是"这个页面对他不开放"
    # （原型 08 矩阵画了「—」）。用独立的码才能让前端给出正确文案，
    # 而不是笼统的"你没有权限"
    MET_RANGE_INVALID = ErrorSpec("MET-1001", 400, "时间范围不合法")
    MET_FUTURE_RANGE = ErrorSpec("MET-1002", 400, "结束时间不能晚于当前时间")
    MET_METRIC_INVALID = ErrorSpec("MET-1003", 400, "指标名不合法")
    MET_RANGE_TOO_LARGE = ErrorSpec("MET-1004", 400, "查询跨度超出上限")
    # MET-1005 与 MET-1010 是**两件事**，不能合并成一个码：
    # 前者是"你要的粒度在这个跨度上代价不可接受"（1m 跨多天 = 43200 个文档），
    # 后者是"这个粒度取值根本不存在"。合并后前端无法判断该改跨度还是改参数。
    MET_MINUTE_RANGE_TOO_LARGE = ErrorSpec("MET-1005", 400, "该跨度不允许分钟级粒度")
    MET_GRANULARITY_INVALID = ErrorSpec("MET-1010", 400, "粒度取值不合法")
    MET_PERCENTILE_UNSUPPORTED = ErrorSpec("MET-1006", 400, "不支持的分位口径")
    MET_EXACT_RANGE_TOO_LARGE = ErrorSpec("MET-1007", 400, "精确分位模式的时间范围过大")
    MET_RANK_LIMIT_INVALID = ErrorSpec("MET-1008", 400, "榜单条数超出上限")
    MET_EXPORT_FORMAT = ErrorSpec("MET-1009", 400, "导出格式不支持")
    MET_PERM_DENIED = ErrorSpec("MET-2002", 403, "功能权限不足")
    MET_KB_ADMIN_DENIED = ErrorSpec("MET-2003", 403, "知识管理员无权访问运营看板")
    MET_RATE_LIMITED = ErrorSpec("MET-2004", 429, "看板请求过于频繁")
    MET_EXPORT_DENIED = ErrorSpec("MET-2005", 403, "无权导出看板数据")
    MET_BUCKET_NOT_FOUND = ErrorSpec("MET-3001", 404, "指标桶不存在")
    MET_ROLLUP_PENDING = ErrorSpec("MET-3002", 409, "该粒度桶尚未汇总")
    MET_DATA_EXPIRED = ErrorSpec("MET-3003", 410, "指标数据已过期")
    MET_EXACT_TOO_MANY_SAMPLES = ErrorSpec("MET-3004", 400, "精确分位模式样本量超限")
    MET_EXPORT_TOO_LARGE = ErrorSpec("MET-3005", 413, "导出数据量超限")
    MET_READ_FAILED = ErrorSpec("MET-4001", 500, "指标桶读取失败")
    MET_LOG_AGG_FAILED = ErrorSpec("MET-4002", 500, "问答日志聚合失败")
    MET_DOC_TITLE_FAILED = ErrorSpec("MET-4003", 500, "文档标题查询失败")
    MET_DOC_COUNT_FAILED = ErrorSpec("MET-4004", 500, "知识单元计数失败")
    MET_EXACT_UNAVAILABLE = ErrorSpec("MET-4005", 500, "延时明细不可用")
    MET_WRITE_FAILED = ErrorSpec("MET-5001", 500, "指标写入失败")
    MET_ROLLUP_FAILED = ErrorSpec("MET-5002", 500, "指标桶汇总任务失败")
    MET_TTL_INDEX_FAILED = ErrorSpec("MET-5003", 500, "TTL 索引创建失败")
    # ---- 10 审计日志 / AUD（清单来源：模块 10 Spec §5）----
    AUD_TIME_RANGE = ErrorSpec("AUD-1001", 400, "时间范围参数不合法")
    AUD_PAGE_INVALID = ErrorSpec("AUD-1002", 400, "分页参数不合法或查询范围过深")
    AUD_ACTION_UNKNOWN = ErrorSpec("AUD-1003", 400, "动作名不在动作字典内")
    AUD_FILTER_INVALID = ErrorSpec("AUD-1004", 400, "筛选条件取值非法")
    AUD_EXPORT_INVALID = ErrorSpec("AUD-1005", 400, "导出参数不合法或导出范围过大")
    AUD_READ_DENIED = ErrorSpec("AUD-2001", 403, "无权查看审计日志")
    AUD_EXPORT_DENIED = ErrorSpec("AUD-2002", 403, "无审计日志导出权限")
    AUD_APPEND_ONLY = ErrorSpec("AUD-2003", 403, "审计记录只读，禁止修改或删除")
    AUD_NOT_FOUND = ErrorSpec("AUD-3001", 404, "审计记录不存在")
    AUD_IMMUTABLE = ErrorSpec("AUD-3002", 409, "已落库的审计记录不可修改或回填")
    AUD_REPLAY_RUNNING = ErrorSpec("AUD-3003", 409, "补偿文件回放正在进行")
    AUD_WRITE_FAILED = ErrorSpec("AUD-4001", 500, "审计写入失败")
    AUD_WRITE_BREAKER = ErrorSpec("AUD-4002", 503, "审计写入已熔断")
    AUD_SPOOL_UNWRITABLE = ErrorSpec("AUD-4003", 500, "审计补偿文件不可写")
    AUD_QUERY_FAILED = ErrorSpec("AUD-4004", 503, "审计查询失败")
    AUD_REDACT_FAILED = ErrorSpec("AUD-5001", 500, "快照脱敏失败")
    AUD_EXPORT_FAILED = ErrorSpec("AUD-5002", 500, "审计导出生成失败")
    AUD_INTERNAL = ErrorSpec("AUD-5003", 500, "审计服务内部异常")


class BizError(Exception):
    """业务异常：携带错误码规范，由统一处理器转成响应契约。"""

    def __init__(self, spec: ErrorSpec, detail: str | None = None) -> None:
        self.spec = spec
        self.detail = detail
        super().__init__(f"{spec.code} {detail or spec.message}")


def _trace(request: Request) -> str:
    return getattr(request.state, "trace_id", "-")


def register_exception_handlers(app: FastAPI) -> None:
    """注册 5 个统一异常处理器，保证**任何**失败都返回同一信封结构。

    信封信封由 `core/response.fail()` 统一构造（与鉴权中间件共用同一份定义）。
    """

    @app.exception_handler(BizError)
    async def _biz(request: Request, exc: BizError) -> JSONResponse:
        return fail(exc.spec.code, exc.detail or exc.spec.message,
                    exc.spec.http, _trace(request))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        msg = f"{Err.SYS_PARAM_INVALID.message}: {loc} {first.get('msg', '')}".strip()
        return fail(Err.SYS_PARAM_INVALID.code, msg,
                    Err.SYS_PARAM_INVALID.http, _trace(request))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        spec = {404: Err.SYS_NOT_FOUND, 405: Err.SYS_METHOD_NOT_ALLOWED}.get(exc.status_code)
        if spec is None:
            spec = ErrorSpec(f"SYS-{exc.status_code:04d}", exc.status_code, str(exc.detail))
        return fail(spec.code, spec.message, exc.status_code, _trace(request))

    @app.exception_handler(ConfigError)
    async def _config(request: Request, exc: ConfigError) -> JSONResponse:
        return fail(Err.SYS_INTERNAL.code, f"配置错误: {exc}", 500, _trace(request))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # 细节只进日志，不回给客户端（避免泄漏栈信息）
        logger.exception("未捕获异常 trace_id=%s", _trace(request))
        return fail(Err.SYS_INTERNAL.code, Err.SYS_INTERNAL.message,
                    Err.SYS_INTERNAL.http, _trace(request))
