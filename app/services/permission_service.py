# -*- coding: utf-8 -*-
"""模块 05 的服务层：**四维数据权限配置 + 鉴权判定引擎**。

## 一张图说清这个模块的位置

```
        ┌─────────────── 05 PermissionService ───────────────┐
        │  save()       ← 知识管理员在原型 04 弹窗里配权限      │
        │  evaluate()   ← /perm/check（对外验收入口）           │
        │  batch_check()← 06 问答链路的**每一步**（ER-03）      │
        └───────────────┬───────────────────┬─────────────────┘
                        │                   │
              E07 kb_permissions      DocService（03）
              （本模块独占写）          permission_summary 回填
```

## 五条铁律（每条都有验收项钉着）

| # | 铁律 | 出处 |
|---|---|---|
| 1 | **OR 逻辑**：全局 / 部门 / 角色 / 个人满足任意一项即可读 | R-1 · AC-05-01/16 |
| 2 | **默认拒绝**：无权限记录 = 不可读，**连 `sys_admin` 也不例外** | R-2 · AC-05-02/03 |
| 3 | **部门精确匹配**：授权「财务部」不含「财务部/报销组」 | R-3 · AC-05-04 |
| 4 | **fail-closed**：查库异常 → **全部判 deny** + `degraded=true` | R-4 · AC-05-08 |
| 5 | **不碰 Milvus**：权限只在 E07，检索侧不冗余任何权限字段 | R-5 · ER-11/12 · AC-05-06 |

## 第 2 条最容易被"好心"破坏

"系统管理员应该能看所有文档吧？" —— **不能**。`is_global=false` 且三维全空时，
`sys_admin` 也读不到（AC-05-03 专测这条）。理由：四维权限是**数据权限**，
与"谁能管理系统"（功能权限）是两套体系。让管理员默认绕过，
等于给系统留了一个"任何人都可以用管理员账号看到全部知识"的后门，
而管理员账号在演示与日常使用中出现频率极高。

管理员真要读某篇文档，就**显式把它配成可读**（`is_global=true` 或加自己进 `users`）
—— 留下一条权限记录，而不是靠身份绕过。

## 第 4 条为什么不能是 fail-open

数据库抖动时放行 = 把私密文档泄漏给全员，**这是不可接受的失败模式**；
而拒绝的代价只是"这次没答上来"，用户可以重试。所以异常一律 deny，
并把 `degraded=true` 一路传到 06（它必须在答案里走保守话术，不得假装没有资料）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from app.core.enums import DeptStatus
from app.core.errors import BizError, Err
from app.core.logging import logger
from app.repositories import org_repo, perm_repo
from app.services.audit_service import audit_service
from app.services.doc_service import doc_service
from app.services.perm_cache import perm_cache

# 单维授权数量上限（Spec §3.2 规则 6 / `PERM-1005`）
MAX_DIMENSION_ITEMS = 200
# 变更原因最小长度（G-11：原型 `04` 的 `rsL` 标了 `*`）
MIN_REASON_LEN = 5

# 判定原因码（Spec §3.3 的取值域，**顺序即判定优先级**）
REASON_GLOBAL = "global"
REASON_DEPARTMENT = "department"
REASON_ROLE = "role"
REASON_USER = "user"
REASON_NO_RECORD = "no_permission_record"
REASON_NONE_MATCHED = "none_matched"
REASON_DEGRADED = "engine_degraded"

# 受限提示文案（AC-05-09 要求"文末出现"这句话，06 直接用这个常量，不要自己拼）
RESTRICTED_NOTICE = "部分参考资料因权限受限无法展示。"


@dataclass(frozen=True, slots=True)
class Subject:
    """判定所需的最小用户快照（模块 05 §2.3 的输入契约）。

    与 01 的 `UserContext` 字段名一致（`user_id` / `dept_id` / `role_ids`），
    所以 `evaluate_record()` 对两者都能直接用；区别是本类**只带判定要的三个字段**，
    用于"代查他人"时物化一个快照，不依赖 01 的加载链路。
    """

    user_id: str
    dept_id: str = ""
    role_ids: frozenset[str] = frozenset()


@dataclass(slots=True)
class Decision:
    """单文档判定结果。"""

    doc_id: str
    allowed: bool
    reason_code: str
    version: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"doc_id": self.doc_id, "allowed": self.allowed,
                "reason_code": self.reason_code, "version": self.version}


@dataclass(slots=True)
class BatchResult:
    """批量判定结果（06 问答链路的输入，Spec §3.3）。

    `denied_detail` 与 `missing_perm_record` 分开是有意的：
    "没有权限记录"（默认拒绝）与"有记录但四维都没匹配上"是**两种不同的管理问题**
    —— 前者是"忘了配权限"，后者是"配了但漏了这个人"。管理端排查时看的是不同的东西。
    """

    allowed: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    denied_detail: dict[str, str] = field(default_factory=dict)
    missing_perm_record: list[str] = field(default_factory=list)
    degraded: bool = False

    @property
    def total(self) -> int:
        return len(self.allowed) + len(self.denied)


def _user_id_of(user: Any) -> str:
    """取用户编号（兼容 dataclass 与 dict 两种入参）。"""
    if isinstance(user, Mapping):
        return str(user.get("user_id") or "")
    return str(getattr(user, "user_id", "") or "")


def _dept_id_of(user: Any) -> str:
    """取当前部门编号（单值，OQ-02-04）。"""
    if isinstance(user, Mapping):
        return str(user.get("dept_id") or "")
    return str(getattr(user, "dept_id", "") or "")


def _role_ids_of(user: Any) -> frozenset[str]:
    """取角色编号集合（`UserContext.role_ids` 是 property，这里兼容 list 入参）。"""
    value = user.get("role_ids") if isinstance(user, Mapping) \
        else getattr(user, "role_ids", None)
    if value is None:
        return frozenset()
    if isinstance(value, (set, frozenset)):
        return frozenset(str(v) for v in value)
    if isinstance(value, Mapping):                          # 容错：{role_id: {...}}
        return frozenset(str(k) for k in value)
    return frozenset(str(v) for v in value)


def evaluate_record(user: Any, record: Mapping[str, Any] | None) -> tuple[bool, str]:
    """**判定内核**：纯函数，输入用户快照 + E07 记录，输出 `(allowed, reason_code)`。

    抽成纯函数有三个好处：
    ① 单测可以直接喂各种组合，不必造数据库；
    ② 判定逻辑只有一处（ER-03），06 不可能"顺手自己再判一次"；
    ③ 它是"无 IO"的，批量判定 50 条 < 1ms 的预算才有保障。

    判定顺序严格照 Spec §4.3 的流程图：global → department → role → user → deny。
    **顺序影响 `reason_code`**（多个维度都满足时报第一个命中的），
    而 `reason_code` 是排障入口，所以顺序不能随意调。
    """
    if record is None:
        # R-2 默认拒绝：没有记录 = 没有任何人可读
        return False, REASON_NO_RECORD
    if bool(record.get("is_global")):
        return True, REASON_GLOBAL
    dept_id = _dept_id_of(user)
    if dept_id and dept_id in (record.get("departments") or ()):
        # R-3 精确匹配：`in` 比较的是**当前部门**，不含子部门（G-02）
        return True, REASON_DEPARTMENT
    roles = _role_ids_of(user)
    if roles and roles & set(record.get("roles") or ()):
        return True, REASON_ROLE
    user_id = _user_id_of(user)
    if user_id and user_id in (record.get("users") or ()):
        return True, REASON_USER
    return False, REASON_NONE_MATCHED


class PermissionService:
    """四维数据权限与鉴权引擎（进程级单例）。"""

    # ------------------------------------------------------------------ 判定
    async def _records_of(self, doc_ids: Sequence[str]) -> tuple[dict[str, dict[str, Any]],
                                                                 bool]:
        """取权限记录（先缓存后一次 `$in`），返回 `(记录表, 是否降级)`。

        **fail-closed 的唯一落点**：这里任何异常都转成 `({}, degraded=True)`，
        由调用方把所有 doc 判 deny。绝不让异常往上冒成 500 ——
        500 会让调用方（06）不知道该怎么办，而"全部拒绝"是明确的、安全的答案。
        """
        unique = [d for d in dict.fromkeys(doc_ids) if d]
        if not unique:
            return {}, False
        cached, pending = perm_cache.get_many(unique)
        if not pending:
            return cached, False
        try:
            fetched = await perm_repo.find_by_docs(pending)
        except Exception as exc:                            # noqa: BLE001
            # ER-04：宁可少答，不可越权。这条日志必须是 ERROR 级别——
            # 它是"系统正在因为数据库问题拒绝所有人"的唯一信号。
            logger.error("权限查询失败，已按 fail-closed 全部拒绝（%d 篇）：%s",
                         len(pending), exc)
            return {}, True
        perm_cache.put_many(list(fetched.values()))
        merged = dict(cached)
        merged.update(fetched)
        return merged, False

    async def batch_check(self, user: Any, doc_ids: Sequence[str]) -> BatchResult:
        """**批量判定**（ER-13 / AC-05-07：无论多少切片都只有 1 次 `kb_permissions` 查询）。

        06 问答链路对每个召回的切片都要问一次"这个人能读吗"，
        一次问答可能 50 个 doc —— 逐条查就是 50 次往返。
        """
        unique = [d for d in dict.fromkeys(doc_ids) if d]
        records, degraded = await self._records_of(unique)
        result = BatchResult(degraded=degraded)
        if degraded:
            # 全 deny：连"有记录且 is_global"的也不放行 —— 因为**我们根本没读到记录**，
            # "没读到"和"记录说不允许"在安全上必须同样对待
            result.denied = list(unique)
            result.denied_detail = {d: REASON_DEGRADED for d in unique}
            return result
        for doc_id in unique:
            allowed, reason = evaluate_record(user, records.get(doc_id))
            if allowed:
                result.allowed.append(doc_id)
                continue
            result.denied.append(doc_id)
            result.denied_detail[doc_id] = reason
            if reason == REASON_NO_RECORD:
                result.missing_perm_record.append(doc_id)
        return result

    @staticmethod
    def needs_notice(result: BatchResult) -> bool:
        """是否需要给答案加受限提示（`denied` 非空）。"""
        return bool(result.denied)

    @staticmethod
    def build_notice(result: BatchResult) -> str:
        """生成受限提示文案（AC-05-09）。

        `degraded=true` 时文案**必须不同**：这时不是"部分资料受限"，
        而是"权限系统不可用、这次一条都没敢用"。用同一句话会让用户以为
        "只是少了几篇"，而实际是"这次回答可能完全不完整"。
        """
        if result.degraded:
            return "权限服务暂时不可用，本次回答未使用任何知识库资料，请稍后重试。"
        return RESTRICTED_NOTICE if result.denied else ""

    async def check(self, user: Any, doc_id: str) -> bool:
        """单文档判定（03 单文档操作 / 08 缺口转建用）。"""
        decision = await self.evaluate(user, doc_id)
        return decision.allowed

    async def evaluate(self, user: Any, doc_id: str) -> Decision:
        """单文档判定，**带上原因码与版本**（`/perm/check` 的对外口径）。

        ⚠️ 这里**不校验 doc_id 是否存在**：判定是纯读，`/perm/check` 对
        "文档不存在"同样返回 `allowed=false` + `no_permission_record`。
        只有**配置类**接口（GET/PUT `/perm/{doc_id}`）才需要 `PERM-3001`，
        因为那时用户面对的是"我要配这篇文档"，必须告诉他文档不存在。
        """
        records, degraded = await self._records_of([doc_id])
        if degraded:
            return Decision(doc_id=doc_id, allowed=False, reason_code=REASON_DEGRADED)
        record = records.get(doc_id)
        allowed, reason = evaluate_record(user, record)
        return Decision(doc_id=doc_id, allowed=allowed, reason_code=reason,
                        version=int((record or {}).get("version") or 0))

    # ------------------------------------------------------------------ 配置读
    async def get_config(self, doc_id: str) -> dict[str, Any]:
        """权限配置回显（Spec §3.1）。

        **无记录不返回 404**：`is_global=false` + 三维全空是**合法的初始状态**
        （原型 `04` 的 `gTxtS` 警告文案就是在描述这个状态），
        用 `version=0` 区分"未配置"与"已配置"。
        """
        doc = await self._require_doc(doc_id)
        record = await perm_repo.get_by_doc(doc_id) or {}
        departments = list(record.get("departments") or [])
        roles = list(record.get("roles") or [])
        users = list(record.get("users") or [])
        return {
            "doc_id": doc_id,
            "doc_title": doc.get("title"),
            "is_global": bool(record.get("is_global")),
            "departments": await self._brief_depts(departments),
            "roles": await self._brief_roles(roles),
            "users": await self._brief_users(users),
            "version": int(record.get("version") or 0),
            "reason": record.get("reason"),
            "updated_by": record.get("updated_by"),
            "updated_at": record.get("updated_at"),
            # 前端弹窗要显示"四维全空 = 无人可读"的警告，判定口径由后端给，
            # 免得前端自己推一遍（推错就会出现"界面说没人能看、实际能看"）
            "readable_by": self._readability_hint(record, departments, roles, users),
        }

    @staticmethod
    def _readability_hint(record: Mapping[str, Any], departments: Sequence[str],
                          roles: Sequence[str], users: Sequence[str]) -> str:
        """给前端的"当前谁能读"提示（三态）。"""
        if record.get("is_global"):
            return "all_logged_in_users"
        if departments or roles or users:
            return "limited"
        return "nobody"

    # ------------------------------------------------------------------ 配置写
    async def save(self, *, doc_id: str, is_global: bool,
                   departments: Sequence[str], roles: Sequence[str],
                   users: Sequence[str], reason: str, actor_id: str) -> dict[str, Any]:
        """保存四维权限（Spec §3.2）：校验 → upsert（version+1）→ 刷摘要 → 审计。

        **顺序不能换**：
        ① 先校验（含 `doc_id` 存在性）——无效请求不该留下任何副作用；
        ② 再写 E07 —— 这是"生效"的那一刻，之后鉴权立刻按新值判定（AD-02）；
        ③ 然后刷摘要 —— 只是展示，失败只记 WARN（`PERM-4002` 不给用户报错）；
        ④ 最后写审计（`before`/`after` 都在手里）。
        """
        clean_reason = (reason or "").strip()
        if len(clean_reason) < MIN_REASON_LEN:
            raise BizError(Err.PERM_REASON_REQUIRED,
                           f"变更原因至少 {MIN_REASON_LEN} 个字（G-11）")
        await self._require_doc(doc_id)
        depts, role_ids, user_ids = await self._validate_dimensions(
            departments, roles, users)

        if not is_global and not (depts or role_ids or user_ids):
            # 规则 7：允许配成"全员不可读"，但必须留下痕迹——
            # 这是最常见的"配置事故"（忘了勾任何一维），日志是唯一的线索
            logger.warning("文档 %s 被配成'全员不可读'（is_global=false 且三维全空）",
                           doc_id)

        before = await perm_repo.get_by_doc(doc_id)
        try:
            record = await perm_repo.upsert(
                doc_id, is_global=is_global, departments=depts, roles=role_ids,
                users=user_ids, reason=clean_reason, updated_by=actor_id,
                ts_ms=_now_ms())
        except Exception as exc:                            # noqa: BLE001
            raise BizError(Err.PERM_SAVE_FAILED, f"权限保存失败：{exc}") from exc

        # 缓存里那条**必须立刻失效**：虽然 version 变了天然会让旧 key 失效，
        # 但这里是"刚写完"的同一进程，紧接着的 /perm/check 极可能马上来读——
        # 若读到旧 version 的缓存（例如并发请求先填了缓存），就会答错。
        perm_cache.invalidate(doc_id)
        perm_cache.put(record)

        await self._refresh_summary(doc_id, record)
        await self._audit_change(actor_id, doc_id, before, record, clean_reason)
        return {"doc_id": doc_id, "version": int(record.get("version") or 1),
                "effective_immediately": True}

    async def _refresh_summary(self, doc_id: str,
                               record: Mapping[str, Any]) -> None:
        """刷新 E04 的展示摘要（**必须经 03 的 `DocService`**，ER-02）。

        `PERM-4002` 的语义是"权限已保存成功，只是展示标签没刷新"
        —— **不影响鉴权正确性**（鉴权只读 E07），所以这里只记 WARN 并重试一次，
        **不把错误抛给用户**。这是"摘要不是鉴权依据"这一设计带来的直接好处。
        """
        summary = perm_repo.summarize(record)
        for attempt in (1, 2):
            try:
                await doc_service.refresh_permission_summary(doc_id, summary)
                return
            except Exception as exc:                        # noqa: BLE001
                logger.warning("权限摘要刷新失败（第 %d 次）doc=%s code=%s：%s",
                               attempt, doc_id, Err.PERM_SUMMARY_FAILED.code, exc)
        logger.error("权限摘要刷新最终失败 doc=%s——权限本身已保存，"
                     "鉴权不受影响，标签会在下次保存时修正", doc_id)

    async def _audit_change(self, actor_id: str, doc_id: str,
                            before: Mapping[str, Any] | None,
                            after: Mapping[str, Any],
                            reason: str) -> None:
        """写 `doc.permission_change` 审计（ER-05，含 `before`/`after` + reason）。

        `record()` 的契约是**永不抛异常**，所以这里不 try/except——
        自己再包一层只会掩盖"契约被破坏"这个更严重的问题。
        """
        await audit_service.record(
            action="doc.permission_change", actor=actor_id, actor_name=actor_id,
            target_type="doc", target_id=doc_id,
            target_name=str(after.get("title") or doc_id),
            before=_snapshot(before), after=_snapshot(after), reason=reason,
            outcome="success")

    # ------------------------------------------------------------------ 校验
    async def _require_doc(self, doc_id: str) -> dict[str, Any]:
        """`doc_id` 必须存在且未被软删除（`PERM-3001`）。"""
        doc = await doc_service.get(doc_id)
        if doc is None or doc.get("deleted_at") is not None:
            raise BizError(Err.PERM_DOC_NOT_FOUND,
                           f"知识单元不存在或已删除：{doc_id}")
        return doc

    async def _validate_dimensions(self, departments: Sequence[str],
                                   roles: Sequence[str],
                                   users: Sequence[str]
                                   ) -> tuple[list[str], list[str], list[str]]:
        """校验三维 ID 并去重（Spec §3.2 规则 3~6）。

        三类错误分开报（`PERM-1002/1003/1004`）而不是笼统一个"参数错"：
        前端要能把错误**定位到具体某一维**（弹窗里哪个分组标红），
        否则用户看到"授权项无效"，得把三个分组逐个试一遍。
        """
        depts = _clean_list(departments, Err.PERM_PARAM_INVALID, "departments")
        role_ids = _clean_list(roles, Err.PERM_PARAM_INVALID, "roles")
        user_ids = _clean_list(users, Err.PERM_PARAM_INVALID, "users")

        if depts:
            found = await org_repo.find_depts_by_ids(depts)
            for dept_id in depts:
                row = found.get(dept_id)
                if row is None or row.get("status") != DeptStatus.ACTIVE.value:
                    # 停用的部门不接受授权：授权给一个已停用的部门，
                    # 它的成员谁也读不到（部门停用后用户不再属于活跃组织），
                    # 配置者却以为"财务部能看了"——静默失效比报错更糟
                    raise BizError(Err.PERM_DEPT_INVALID,
                                   f"部门不存在或已停用：{dept_id}")
        if role_ids:
            found_roles = await org_repo.find_roles_by_ids(role_ids)
            for role_id in role_ids:
                if role_id not in found_roles:
                    raise BizError(Err.PERM_ROLE_INVALID, f"角色不存在：{role_id}")
        if user_ids:
            found_users = await org_repo.find_users_by_ids(user_ids)
            for user_id in user_ids:
                if user_id not in found_users:
                    raise BizError(Err.PERM_USER_INVALID, f"用户不存在：{user_id}")
        return depts, role_ids, user_ids

    # ------------------------------------------------------------------ 回显辅助
    async def _brief_depts(self, dept_ids: Sequence[str]) -> list[dict[str, Any]]:
        """部门回显：`[{dept_id, name, status}]`（**按传入顺序**，前端勾选态对照用）。"""
        found = await org_repo.find_depts_by_ids(list(dept_ids))
        out = []
        for dept_id in dept_ids:
            row = found.get(dept_id)
            out.append({"dept_id": dept_id,
                        "name": (row or {}).get("name") or dept_id,
                        "status": (row or {}).get("status"),
                        "exists": row is not None})
        return out

    async def _brief_roles(self, role_ids: Sequence[str]) -> list[dict[str, Any]]:
        """角色回显：`[{role_id, code, name, exists}]`。

        `exists=False` 表示"这条授权指向一个已被删除的角色"——
        必须如实回显而不是静默丢掉：否则管理员会看到"角色列表少了两个"，
        却不知道那两个授权还在 E07 里（**它们仍然不构成放行**，但会造成困惑）。
        """
        found = await org_repo.find_roles_by_ids(list(role_ids))
        out = []
        for role_id in role_ids:
            row = found.get(role_id)
            out.append({"role_id": role_id,
                        "code": (row or {}).get("code"),
                        "name": (row or {}).get("name") or role_id,
                        "exists": row is not None})
        return out

    async def _brief_users(self, user_ids: Sequence[str]) -> list[dict[str, Any]]:
        """用户回显：`[{user_id, real_name, dept_name, exists}]`。

        ⚠️ **只取展示字段**，绝不把仓储返回的整条记录塞进响应：
        用户记录里有 `password_hash`，泄漏一次就够致命。
        """
        found = await org_repo.find_users_by_ids(list(user_ids))
        dept_ids = [str((row or {}).get("dept_id") or "") for row in found.values()]
        dept_map = await org_repo.find_depts_by_ids([d for d in dept_ids if d])
        out = []
        for user_id in user_ids:
            row = found.get(user_id) or {}
            dept_id = str(row.get("dept_id") or "")
            out.append({"user_id": user_id,
                        "real_name": row.get("real_name") or user_id,
                        "dept_name": (dept_map.get(dept_id) or {}).get("name") or "",
                        "exists": user_id in found})
        return out

    # ------------------------------------------------------------------ 对外判定
    async def check_visibility(self, doc_ids: Sequence[str]) -> dict[str, bool]:
        """批量判定"这些文档是否**全局可见**"（模块 07 缓存准入用，Spec §7.1）。

        语义是 `is_global == True`，而不是"某个人能不能读"：
        FAQ 缓存的直出**不经过鉴权**（§1.5），所以只有"对所有登录用户都可见"的文档
        才允许它的答案进缓存。

        **fail-closed（ER-04）**：查库异常 → 全部 `False`（视为非全局）→
        FAQ 可发布但 `enabled=false`、不进缓存。绝不"查不到就放行"。
        """
        unique = [d for d in dict.fromkeys(doc_ids) if d]
        if not unique:
            return {}
        records, degraded = await self._records_of(unique)
        if degraded:
            return {doc_id: False for doc_id in unique}
        return {doc_id: bool((records.get(doc_id) or {}).get("is_global"))
                for doc_id in unique}

    async def _subject_of(self, user_id: str) -> "Subject":
        """把某个用户**物化**成判定所需的最小快照（代查路径）。

        为什么不直接调 `auth_service.load_context()`：
        ① 它会在账号**已停用**时抛 `AUTH-2002` —— 而"一个已停用账号能读哪些知识"
           是个合法的问题（离职交接、审计复核都要问），不该变成 401；
        ② 判定只需要三个字段，拉整个 `UserContext`（含权限码、菜单）是多余的 IO。
        """
        user = await org_repo.get_user(user_id)
        if user is None:
            raise BizError(Err.PERM_USER_INVALID, f"用户不存在：{user_id}")
        return Subject(user_id=user_id, dept_id=str(user.get("dept_id") or ""),
                       role_ids=frozenset(await org_repo.role_ids_of_user(user_id)))

    async def check_for_user(self, *, doc_id: str, current: Any,
                             target_user_id: str | None,
                             can_impersonate: bool) -> dict[str, Any]:
        """`POST /perm/check` 的服务实现（Spec §3.3）。

        `user_id` 省略时判定**当前登录用户**；显式传别人时只有具备 `user:manage`
        的账号可代查（`PERM-2001`）。为什么要限制代查：这个接口会暴露
        "某个人能不能读某篇知识"，属于**权限分布信息**，
        普通用户拿到它就能推断出组织的授权结构。
        """
        if target_user_id and target_user_id != _user_id_of(current):
            if not can_impersonate:
                raise BizError(Err.PERM_IMPERSONATE_DENIED,
                               "只有系统管理员可以代查他人的鉴权结果")
            subject = await self._subject_of(target_user_id)
        else:
            subject = current
        decision = await self.evaluate(subject, doc_id)
        return {
            **decision.as_dict(),
            "evaluated_as": {
                "user_id": _user_id_of(subject),
                "dept_id": _dept_id_of(subject),
                "role_ids": sorted(_role_ids_of(subject)),
            },
        }


def _clean_list(values: Sequence[str], spec: Any, field_name: str) -> list[str]:
    """去重 + 去空 + 上限校验（规则 6）。"""
    if values is None:
        return []
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple, set,
                                                                  frozenset)):
        raise BizError(spec, f"{field_name} 必须是数组")
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        if text not in out:
            out.append(text)
    if len(out) > MAX_DIMENSION_ITEMS:
        raise BizError(Err.PERM_TOO_MANY,
                       f"{field_name} 最多 {MAX_DIMENSION_ITEMS} 项，实际 {len(out)}")
    return out


def _snapshot(record: Mapping[str, Any] | None) -> dict[str, Any]:
    """审计快照：只取权限四维 + 版本，**不带 `_id` 与时间戳**。

    审计的价值在于"能不能一眼看出改了什么"，塞进 Mongo 内部字段只会让
    两条快照的 diff 里多出一堆噪声。`reason` 单独作为审计的 `reason` 字段传，
    所以快照里也不重复。

    ⚠️ **首次配置时 `record is None`，这里返回"全空 + `version=0`"而不是 `None`**：
    `doc.permission_change` 是 `requires_snapshot` 动作，审计服务遇到
    `before=None` 会**把这条记录整条丢掉**（它认为"要求快照却没有快照 = 调用方违约"）。
    而那恰恰是最该留痕的一次变更——"谁把这篇文档第一次配成了可读"。
    `version=0` 就是"之前没有权限记录（默认拒绝）"的准确表达（§3.1 用同一个约定）。
    """
    if not record:
        return {"is_global": False, "departments": [], "roles": [], "users": [],
                "version": 0}
    return {
        "is_global": bool(record.get("is_global")),
        "departments": list(record.get("departments") or []),
        "roles": list(record.get("roles") or []),
        "users": list(record.get("users") or []),
        "version": int(record.get("version") or 0),
    }


def _now_ms() -> int:
    """当前毫秒时间戳。"""
    import time

    return int(time.time() * 1000)


permission_service = PermissionService()

__all__ = [
    "PermissionService", "permission_service", "Decision", "BatchResult", "Subject",
    "evaluate_record", "RESTRICTED_NOTICE", "MAX_DIMENSION_ITEMS", "MIN_REASON_LEN",
    "REASON_GLOBAL", "REASON_DEPARTMENT", "REASON_ROLE", "REASON_USER",
    "REASON_NO_RECORD", "REASON_NONE_MATCHED", "REASON_DEGRADED",
]
