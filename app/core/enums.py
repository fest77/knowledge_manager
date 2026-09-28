# -*- coding: utf-8 -*-
"""枚举：**按实体各自定义，禁止共用一个通用 StatusEnum**（总纲 ER-10）。

Step 1 §8 为每个实体各自定义了取值域，此处逐一对齐。
"""
from __future__ import annotations

from enum import StrEnum


class DeptStatus(StrEnum):
    """E08 `sys_departments.status`（枚举组 dept_status）。"""

    ACTIVE = "active"
    DISABLED = "disabled"


class UserStatus(StrEnum):
    """E09 `sys_users.status`（枚举组 user_status）。

    与 `DeptStatus` 取值恰好同名，但**仍各自定义**：两者语义独立，
    日后一方扩展（如部门加 `archived`）时不应牵连另一方。
    """

    ACTIVE = "active"
    DISABLED = "disabled"


class BuiltinRole(StrEnum):
    """E10 内置角色 code（枚举组 builtin_role）。

    PRD 2.9.2 用 `- ` 列表逐条穷举，属**封闭集合**；业务角色（`is_system=False`）
    不在此枚举内。种子脚本用它做一次一致性断言（防止内置角色集合被悄悄改掉）。
    """

    ASKER = "asker"
    KB_ADMIN = "kb_admin"
    SYS_ADMIN = "sys_admin"


class PermissionType(StrEnum):
    """E12 `sys_permissions.type`（枚举组 permission_type）。"""

    MENU = "menu"
    BUTTON = "button"
    API = "api"


class ConfigGroup(StrEnum):
    """E22 `system_config.group`（枚举组 config_group）。

    **声明顺序即界面分组顺序**（`ConfigService.all_items()` 依赖它）。
    """

    LLM = "llm"
    RETRIEVAL = "retrieval"
    FAQ = "faq"
    GAP = "gap"
    IMPORT = "import"
    # 模块 09 的看板参数（缓存与限流）
    METRIC = "metric"


class ConfigValueType(StrEnum):
    """E22 `system_config.value_type`（枚举组 config_value_type）。"""

    STRING = "string"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    JSON = "json"


class AuditOutcome(StrEnum):
    """E21 `audit_logs.outcome`（枚举组 outcome，模块 10 §2.1）。

    `denied` 专指**数据权限**拒绝（四维判定的结果），与 `failure`（执行出错）分开：
    前者是"有权用功能、无权看内容"，后者是"操作本身没成"。混在一起会让
    「是不是越权尝试」这个问题在审计里查不出来。
    """

    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"


class SnapshotState(StrEnum):
    """E21 `audit_logs.snapshot_state`（枚举组 snapshot_state，模块 10 §2.1）。

    **取值即优先级**（模块 10 §2.1：`dropped > truncated > redacted > diff > full`）。
    这个优先级不是装饰：一条记录可能同时"被截断"且"被脱敏"（截断产生 `sha256`
    占位，脱敏替换真值），此时必须报**更严重**的那个，否则前端会低估快照的残缺程度。
    """

    FULL = "full"
    DIFF = "diff"
    REDACTED = "redacted"
    TRUNCATED = "truncated"
    DROPPED = "dropped"

    @property
    def severity(self) -> int:
        """越大越严重；用于多个处置同时发生时的取严。"""
        return _SNAPSHOT_SEVERITY[self]


# 与 `SnapshotState` 的声明顺序无关，显式写死以免有人重排枚举导致语义漂移
_SNAPSHOT_SEVERITY: dict[SnapshotState, int] = {
    SnapshotState.FULL: 0,
    SnapshotState.DIFF: 1,
    SnapshotState.REDACTED: 2,
    SnapshotState.TRUNCATED: 3,
    SnapshotState.DROPPED: 4,
}


def worst_snapshot_state(*states: SnapshotState) -> SnapshotState:
    """取多个处置状态中**最严重**的一个（优先级见模块 10 §2.1）。"""
    return max(states, key=lambda s: s.severity) if states else SnapshotState.FULL


class DocStatus(StrEnum):
    """E04 `kb_documents.status`（枚举组 doc_status，模块 03 §2.4）。

    ⚠️ 取值与 `UserStatus` 恰好同名，但**必须各自定义**（ER-10）：
    `user_status` 说的是"账号能不能登录"，`doc_status` 说的是"知识能不能被检索到"，
    一方扩展（如知识加 `archived`）不该牵连另一方。
    """

    ENABLED = "enabled"
    DISABLED = "disabled"



class ImportStatus(StrEnum):
    """E04 `kb_documents.import_status`（枚举组 import_status）。

    取值域与 E06 的**任务**状态**不是同一枚举**：任务状态还有 `timeout` / `cancelled`，
    而文档只有"导到哪一步了"这五个值。本模块对它是**只读**的（04 回填）。
    """

    PENDING = "pending"
    PARSING = "parsing"
    EMBEDDING = "embedding"
    DONE = "done"
    FAILED = "failed"

    @property
    def in_progress(self) -> bool:
        """是否处于"正在导入"（决定能否编辑/删除/启用，见 `DOC-3010`/`DOC-3012`）。"""
        return self in (ImportStatus.PENDING, ImportStatus.PARSING, ImportStatus.EMBEDDING)




class ImportTaskStatus(StrEnum):
    """E06 `kb_import_tasks.status`（枚举组 task_status，模块 04 §2.5）。

    ⚠️ 它与 E04 的 `ImportStatus` **不是同一枚举**：文档只有"导到哪一步了"五个值，
    任务还要表达 `timeout` / `cancelled` 这两种"没跑完就结束"的结局。
    混用会让前端无法区分"导入失败（可以重试）"与"被取消（用户主动）"。
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class ImportTaskStage(StrEnum):
    """E06 `kb_import_tasks.stage`（枚举组 task_stage）。

    **声明顺序即流水线顺序**（`import_repo.STAGES` 直接依赖它）。
    6 槽是固定的：`.txt` 不需要 PDF 转换也会走完这 6 个名字，
    只是对应阶段耗时趋近 0——阶段数随文件类型变化会让进度条失去统一坐标系。
    """

    UPLOAD = "upload"
    PDF_TO_MD = "pdf_to_md"
    MD_IMG = "md_img"
    SPLIT = "split"
    EMBEDDING = "embedding"
    MILVUS = "milvus"


class FaqCandidateStatus(StrEnum):
    """E16 `faq_candidates.status`（枚举组 **faq_status**，模块 07 §2.5）。

    ⚠️ **ER-10：独立定义，不得复用** `doc_status` / `user_status` / `task_status`。
    本项目 5 种 status 的取值域各不相同，共用会把"用文档状态判断候选状态"
    这类串味缺陷直接写进类型系统。

    中文映射（前端展示）：`pending` 待审核 / `approved` 已通过 / `rejected` 已驳回。
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

    @property
    def label(self) -> str:
        return {"pending": "待审核", "approved": "已通过",
                "rejected": "已驳回"}[self.value]


class GapStatus(StrEnum):
    """E19 `knowledge_gaps.status`（枚举组 **gap_status**，模块 08 §2.6）。

    ⚠️ **ER-10：独立定义，不得复用** `doc_status` / `task_status` / `user_status` /
    `faq_status`。本项目 6 种 `status` 的取值域各不相同，共用会把
    "用文档状态判断缺口状态"这类串味缺陷直接写进类型系统。
    收到 `enabled` / `pending` 之类的值一律按参数错处理（`GAP-1001`）。
    """

    OPEN = "open"
    CONVERTED = "converted"
    IGNORED = "ignored"

    @property
    def label(self) -> str:
        return {"open": "待处理", "converted": "已转建",
                "ignored": "已忽略"}[self.value]

class MetricBucketType(StrEnum):
    """E20 `metric_buckets.bucket_type`（枚举组 **bucket_type**，模块 09 §2.5）。

    ⚠️ **ER-10：独立定义**，不复用任何别的枚举类。
    """

    GLOBAL = "global"      # 全局维度：PV / 总量 / Token
    FAQ = "faq"            # FAQ 维度：单个 FAQ 的命中
    LATENCY = "latency"    # 延时维度：7 个固定区间的直方图
    DOC = "doc"            # 文档维度：单篇文档被引用次数


class MetricGranularity(StrEnum):
    """E20 `metric_buckets.granularity`（枚举组 **granularity**，模块 09 §2.5）。

    `1d` 是**唯一允许存 `uv_set` 的粒度**（AD-12）：UV 是去重量，
    放分钟/小时桶会让同一用户在多个桶里各算一次。
    """

    M1 = "1m"
    H1 = "1h"
    D1 = "1d"
