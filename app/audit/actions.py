# -*- coding: utf-8 -*-
"""审计动作字典（模块 10 §2.2）——**全平台动作名的唯一定义源**。

为什么字典在代码里而不入库（**DEC-10-3**）：
动作名由 `record()` 的**调用点**决定，调用点就在代码里。把它入库会产生
"库里有、代码不认"的分裂，而它又不像 `sys_permissions` 那样需要被用户编辑。
它和错误码属于同一类东西：**代码契约**，必须与代码同源同版本。

三件事在这里一次说清：

| 关注点 | 落点 |
|---|---|
| 动作名从哪来 | `AUDIT_ACTION_META`（40 个：23 必备 + 17 扩展） |
| 同义动作怎么查 | `ALIAS` / `expand_action_filter()`（§2.2.2 别名规则） |
| 未注册动作怎么办 | `resolve_action()` → `unknown.{原名}`，**不丢弃事件**（§2.2.4） |
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# ---------------------------------------------------------------------------
# §2.2.3 target_type 取值域（12 个）
# ---------------------------------------------------------------------------
TARGET_TYPES: tuple[str, ...] = (
    "doc", "category", "user", "dept", "role", "faq", "candidate",
    "gap", "config", "auth", "audit", "session",
)

# ---------------------------------------------------------------------------
# §2.2.4 动作命名规约（约束**新增**动作；已接纳的扩展动作见 NAMING_EXEMPT）
# ---------------------------------------------------------------------------
DOMAINS: frozenset[str] = frozenset({
    "doc", "category", "user", "dept", "role", "faq", "gap", "config", "auth", "audit",
})

VERBS: frozenset[str] = frozenset({
    "create", "update", "delete", "toggle", "enable", "disable", "restore", "grant",
    "reject", "publish", "convert", "permission_change", "role_change", "reset_pwd",
    "login_fail", "denied", "recount", "rebuild", "replay", "mine",
})

# 10 个"上游已声明、一并接纳"的动作名不满足 §2.2.4 的域/动词白名单
# （多段动词 `mine.empty` / `cache.rebuild` / `spool_replay` / `import.start`，
# 以及名词段 `candidate.approve` / `candidate.reject`，与未列入动词表的 `export`）。
# §2.2.4 的原文是"约束**新增**动作"，这些在各自的模块 Spec 里就已登记，故豁免；
# 但豁免清单**必须与实际违例集合逐字相等**，由单测钉死，
# 防止有人借"豁免"塞进一个没走评审的动作名。
NAMING_EXEMPT: frozenset[str] = frozenset({
    "faq.candidate.approve", "faq.candidate.reject", "faq.mine.empty",
    "faq.cache.rebuild", "audit.export", "audit.spool_replay",
    # 模块 04 §1.4 逐字声明的四条导入动作：`import.*` 是**带阶段语义的复合动词**
    # （`start` / `done` / `fail` / `cancel` 描述的是同一条长流程的四个节点），
    # 拆成 `doc.start_import` 之类反而丢了"它们属于同一条流程"这个信息
    "doc.import.start", "doc.import.done", "doc.import.fail", "doc.import.cancel",
})

# G-11：这些动作**必须**填 reason（≥5 字）；`*.update` 由 `requires_reason` 兜住
REASON_REQUIRED: frozenset[str] = frozenset({
    "doc.permission_change", "role.grant", "user.role_change", "config.update",
})

UNKNOWN_PREFIX = "unknown."


@dataclass(frozen=True, slots=True)
class ActionMeta:
    """一个审计动作的元数据（中文名 / 目标类型 / 快照要求 / 归属模块）。"""

    action: str
    name: str
    target_type: str
    module: str
    requires_snapshot: bool = False
    alias_of: str | None = None

    @property
    def canonical(self) -> str:
        """统一名：别名返回其指向的必备动作名，其余返回自身。"""
        return self.alias_of or self.action

    @property
    def requires_reason(self) -> bool:
        """是否需要变更原因（G-11：权限 / 角色 / 配置类 + 全部 `*.update`）。"""
        return self.action in REASON_REQUIRED or self.action.endswith(".update")


# ---------------------------------------------------------------------------
# §2.2.1 必备动作（23）
# ---------------------------------------------------------------------------
REQUIRED_ACTIONS: tuple[ActionMeta, ...] = (
    ActionMeta("doc.create", "新建知识单元", "doc", "03/04/08"),
    ActionMeta("doc.update", "编辑知识单元", "doc", "03", requires_snapshot=True),
    ActionMeta("doc.delete", "软删除知识单元", "doc", "03"),
    ActionMeta("doc.toggle", "启用 / 停用知识单元", "doc", "03", requires_snapshot=True),
    # ---- 模块 04 的导入四态（Spec §1.4 / §3.1 R-16 / §3.5 R-05 / §4.5）----
    # 命名严格照 Spec 给的四个名字，**不自行扩展**（ER-16）。
    # 分成四条而不是一条 `doc.import`：导入是**长流程**，"开始了"与"跑完了"
    # 之间隔着几分钟，"失败了"和"被取消了"更是完全不同的结论。
    # 合成一条的话审计里只剩时间戳，回溯时无法回答"这次到底是失败了还是没跑起来"。
    ActionMeta("doc.import.start", "开始导入", "doc", "04"),
    ActionMeta("doc.import.done", "导入完成", "doc", "04"),
    ActionMeta("doc.import.fail", "导入失败", "doc", "04"),
    ActionMeta("doc.import.cancel", "取消导入", "doc", "04"),
    ActionMeta("doc.permission_change", "四维数据权限变更", "doc", "05",
               requires_snapshot=True),
    ActionMeta("faq.publish", "FAQ 发布", "faq", "07", requires_snapshot=True),
    ActionMeta("faq.reject", "FAQ 驳回", "faq", "07"),
    ActionMeta("user.create", "新增用户", "user", "02"),
    ActionMeta("user.update", "编辑用户", "user", "02", requires_snapshot=True),
    ActionMeta("user.disable", "停用用户", "user", "02", requires_snapshot=True),
    ActionMeta("user.enable", "启用用户", "user", "02", requires_snapshot=True),
    ActionMeta("user.role_change", "用户角色变更", "user", "02", requires_snapshot=True),
    ActionMeta("user.reset_pwd", "重置密码", "user", "02"),
    ActionMeta("dept.create", "新建部门", "dept", "02"),
    ActionMeta("dept.update", "编辑部门", "dept", "02", requires_snapshot=True),
    ActionMeta("dept.delete", "删除部门", "dept", "02"),
    ActionMeta("role.update", "编辑角色", "role", "02", requires_snapshot=True),
    ActionMeta("role.delete", "删除角色", "role", "02"),
    ActionMeta("role.grant", "角色功能权限分配", "role", "01", requires_snapshot=True),
    ActionMeta("gap.convert", "知识缺口一键转建", "gap", "08"),
    ActionMeta("auth.login_fail", "登录失败", "auth", "01"),
    ActionMeta("auth.denied", "鉴权（数据权限）拒绝", "auth", "06"),
    ActionMeta("config.update", "系统 / 模型参数修改", "config", "00/02",
               requires_snapshot=True),
)

# ---------------------------------------------------------------------------
# §2.2.2 上游模块已声明、一并接纳的扩展动作（17）
# ---------------------------------------------------------------------------
EXTENDED_ACTIONS: tuple[ActionMeta, ...] = (
    ActionMeta("doc.enable", "启用知识单元", "doc", "03",
               requires_snapshot=True, alias_of="doc.toggle"),
    ActionMeta("doc.disable", "停用知识单元", "doc", "03",
               requires_snapshot=True, alias_of="doc.toggle"),
    ActionMeta("doc.restore", "恢复软删除文档", "doc", "03"),
    ActionMeta("category.create", "新建知识分类", "category", "03"),
    ActionMeta("category.update", "编辑知识分类", "category", "03",
               requires_snapshot=True),
    ActionMeta("category.delete", "删除知识分类", "category", "03"),
    ActionMeta("category.recount", "分类计数重算", "category", "03"),
    ActionMeta("faq.candidate.approve", "FAQ 候选审核通过", "candidate", "07",
               requires_snapshot=True, alias_of="faq.publish"),
    ActionMeta("faq.candidate.reject", "FAQ 候选审核驳回", "candidate", "07",
               alias_of="faq.reject"),
    ActionMeta("faq.mine", "FAQ 定时挖掘完成", "candidate", "07"),
    ActionMeta("faq.mine.empty", "FAQ 挖掘窗口内无日志", "candidate", "07"),
    ActionMeta("faq.update", "编辑已发布 FAQ", "faq", "07", requires_snapshot=True),
    ActionMeta("faq.toggle", "启用 / 停用 FAQ", "faq", "07"),
    ActionMeta("faq.delete", "删除 FAQ", "faq", "07"),
    ActionMeta("faq.cache.rebuild", "FAQ 缓存重建", "faq", "07"),
    ActionMeta("audit.export", "审计日志导出", "audit", "10"),
    ActionMeta("audit.spool_replay", "补偿文件回放", "audit", "10"),
)

# ---------------------------------------------------------------------------
# 后续模块按 §2.2.4 的登记流程**追加**的动作
# ---------------------------------------------------------------------------
# §2.2.4 规定："新增流程：在 `AUDIT_ACTION_META` 注册（含中文名与是否要求快照）
# → 才允许被调用"。所以"字典里没有"不等于"不能新增"，而是要**显式登记**，
# 而不是让调用方自己拼一个名字（那会落成 `unknown.xxx`，在界面上露出字典缺口）。
#
# 单独一个元组而不是塞进上面两个：上面两个是**模块 10 Spec §2.2.1/§2.2.2 的原文清单**，
# 数量（23 + 17）本身就是被文档与断言钉住的对账依据；把后来者混进去，
# 会让"Spec 清单是否走样"这件事再也看不出来。
REGISTERED_ACTIONS: tuple[ActionMeta, ...] = (
    # 模块 02 §3.3：新建业务角色。§2.2.1/§2.2.2 两份清单都漏了它（只有
    # role.update / role.delete / role.grant），属**登记式补录**。
    ActionMeta("role.create", "新建角色", "role", "02"),
)

ACTIONS: tuple[ActionMeta, ...] = REQUIRED_ACTIONS + EXTENDED_ACTIONS + REGISTERED_ACTIONS

AUDIT_ACTION_META: dict[str, ActionMeta] = {m.action: m for m in ACTIONS}

# §2.2.2 别名规则：**原样落库**，只在查询时展开（不改调用方，不丢记录）
ALIAS: dict[str, str] = {m.action: m.alias_of for m in ACTIONS if m.alias_of}

# 统一名 → (统一名, 别名...)；查询 `action=doc.toggle` 时按它展开为 `$in`
ALIAS_GROUPS: dict[str, tuple[str, ...]] = {}
for _m in ACTIONS:
    _group = ALIAS_GROUPS.setdefault(_m.canonical, ())
    ALIAS_GROUPS[_m.canonical] = (*_group, _m.action)


def is_registered(action: str) -> bool:
    """动作名是否已在字典内。"""
    return action in AUDIT_ACTION_META


def meta_of(action: str) -> ActionMeta | None:
    """取动作元数据；未注册返回 `None`（由调用方决定如何降级）。"""
    return AUDIT_ACTION_META.get(action)


def resolve_action(action: str) -> str:
    """把调用方传来的动作名规范成**落库名**。

    未注册动作**不丢弃**，改写为 `unknown.{原名}`（§2.2.4）：
    审计的第一原则是不能有洞——宁可留下一条 `unknown.xxx` 让评审看到
    "这里有个未注册动作"，也不能因为名字写错就整条丢掉。
    """
    return action if is_registered(action) else UNKNOWN_PREFIX + action


def unknown_actions(values: Iterable[str]) -> list[str]:
    """返回入参里**不在字典内**的动作名（供 `AUD-1003` 报错时逐一列出）。"""
    return sorted({v for v in values if v and not is_registered(v)})


def expand_action_filter(values: Iterable[str]) -> list[str]:
    """把筛选用的动作名展开为落库名的并集（§2.2.2）。

    只有**统一名**才展开成整组：`doc.toggle` → `{doc.toggle, doc.enable, doc.disable}`，
    因为这句话要表达的是"看所有启停用操作"。
    若调用方点名某个别名（`doc.enable`），就只筛它本身——展开成整组会让
    "我就想看启用"变成"顺带看了停用"，与筛选器的直觉相反。
    """
    names: set[str] = set()
    for value in values:
        meta = AUDIT_ACTION_META.get(value)
        if meta is None:
            continue
        if meta.alias_of is None:
            names.update(ALIAS_GROUPS.get(meta.canonical, (value,)))
        else:
            names.add(value)
    return sorted(names)


def raw_naming_violations() -> list[str]:
    """**全部**违反 §2.2.4 命名规约的动作名（含已豁免的）。"""
    bad: list[str] = []
    for meta in ACTIONS:
        domain, _, verb = meta.action.partition(".")
        if domain not in DOMAINS or verb not in VERBS:
            bad.append(meta.action)
    return sorted(bad)


def naming_violations() -> list[str]:
    """违反命名规约且**不在豁免清单**内的动作名；空列表代表字典合规。"""
    return sorted(set(raw_naming_violations()) - NAMING_EXEMPT)


def list_action_meta() -> list[dict[str, object]]:
    """给 `GET /api/v1/audit/actions` 用的字典快照（字段与 §3.4 出参一一对应）。"""
    return [
        {
            "action": m.action,
            "name": m.name,
            "target_type": m.target_type,
            "requires_snapshot": m.requires_snapshot,
            "module": m.module,
            "alias_of": m.alias_of,
        }
        for m in sorted(ACTIONS, key=lambda x: x.action)
    ]
