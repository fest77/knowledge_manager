# -*- coding: utf-8 -*-
"""模块 02 的服务层：部门树 / 用户 / 角色的业务规则**唯一实现处**。

三条贯穿全文的约束：

| 约束 | 落实方式 |
|---|---|
| **ER-02 单一写入者** | 只经 `org_repo` 写 E08~E11；E13（角色功能权限）的清理必须调 `AuthService` |
| **ER-05 审计只能经 `AuditService`** | 每个写操作在**业务落库成功之后**调 `record()`，
不 `try/except`、不看返回值 |
| **ER-03 判定只有一份** | 本模块**不做**任何 allow/deny 判定（那是 05）；
`path_ids` 只服务于组织架构页的子树展示 |

**`path_ids` 与 G-02 的边界**（Spec §2.1 的注）：G-02 裁定"部门授权**不含子部门**"，
`path_ids` 只用于本页的子树展示与筛选，**不得**被 05 的判定拿去当"含子部门"用。
Spec 为此专门留了 `ORG-1004`：接口上只要出现 `include_sub_dept` 之类的参数就明确拒绝，
免得有人以为"传个参数就能改判定语义"。
"""
from __future__ import annotations

import re
import time
from typing import Any, Mapping, Sequence

from fastapi import Request

from app.core.enums import DeptStatus, UserStatus
from app.core.errors import BizError, Err
from app.core.logging import logger
from app.core.security import hash_password
from app.repositories import org_repo
from app.services.audit_service import audit_service
from app.services.permission_cache import permission_cache

# 账号规则与模块 01 的登录校验保持**同一个正则**（否则会出现"能建出来但登不进"的账号）
USERNAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{2,31}$")
ROLE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{2,31}$")
PASSWORD_MIN_LEN = 8
# 系统管理员角色 code：停用/降权时用它判断"会不会把系统锁死"
SUPER_ADMIN_CODE = "sys_admin"
# 部门树最多 5 层 → level 0~4
MAX_DEPT_LEVEL = org_repo.MAX_DEPT_LEVEL
# 明确拒绝的"子部门语义"参数（G-02）
FORBIDDEN_SUB_DEPT_PARAMS = ("include_sub_dept", "include_sub", "with_sub_dept",
                             "sub_dept", "recursive")


# --------------------------------------------------------------------------- 小工具
def ensure_password_strength(password: str) -> None:
    """密码强度：≥8 位且**同时含字母与数字**（`ORG-1001`）。

    刻意比模块 01 的登录校验（只查长度）更严：登录时要兼容历史口令，
    而**设置新口令**没有历史包袱，此时不提要求就永远提不了。
    """
    text = password or ""
    if len(text) < PASSWORD_MIN_LEN or not (re.search(r"[A-Za-z]", text)
                                            and re.search(r"\d", text)):
        raise BizError(Err.ORG_PWD_WEAK,
                       f"密码至少 {PASSWORD_MIN_LEN} 位，且需同时包含字母与数字")


def ensure_reason(reason: str | None) -> str:
    """G-11 同口径：变更原因必填且 ≥5 字（`ORG-1003`）。"""
    text = (reason or "").strip()
    if len(text) < 5:
        raise BizError(Err.ORG_REASON_REQUIRED, "变更原因必填且不少于 5 字")
    return text


def reject_sub_dept_params(**params: Any) -> None:
    """出现任何"要不要含子部门"的参数就明确拒绝（`ORG-1004`）。

    宁可报错也不静默忽略：静默忽略会让调用方以为"传了 `include_sub_dept=true`
    真的把子部门算进来了"，而 G-02 裁定的是**不含**——这正是最危险的那种误解。
    """
    hit = [name for name in FORBIDDEN_SUB_DEPT_PARAMS
           if params.get(name) not in (None, False, "", "false", "0")]
    if hit:
        raise BizError(Err.ORG_SUB_DEPT_REFUSED,
                       f"部门授权不含子部门（G-02，精确匹配），不接受参数：{hit}")



def _now() -> int:
    return int(time.time())


def _public_dept(row: Mapping[str, Any]) -> dict[str, Any]:
    """把库里存的哨兵 `parent_id` 换回 `None`（对外不暴露哨兵值）。"""
    parent = row.get("parent_id")
    return {
        "dept_id": row["_id"],
        "name": row.get("name", ""),
        "parent_id": None if parent in (None, org_repo.ROOT_SENTINEL) else parent,
        "path": list(row.get("path") or []),
        "path_ids": list(row.get("path_ids") or []),
        "level": int(row.get("level") or 0),
        "sort": int(row.get("sort") or 0),
        "status": row.get("status", DeptStatus.ACTIVE.value),
        "leader_user_id": row.get("leader_user_id"),
    }


# =========================================================================== 部门
async def list_dept_tree(*, with_user_count: bool = True) -> list[dict[str, Any]]:
    """部门树：一次取全部部门 + 一次聚合取用户数，**无 N+1**。

    组树在内存里做（部门量级是百级），比递归查库稳得多，也不会出现
    "子孙查到一半、父节点已被删"的一致性裂缝。
    """
    rows = await org_repo.list_departments()
    counts = await org_repo.count_users_by_dept() if with_user_count else {}
    nodes: dict[str, dict[str, Any]] = {}
    for row in rows:
        node = _public_dept(row)
        node["user_count"] = counts.get(node["dept_id"], 0)
        node["children"] = []
        nodes[node["dept_id"]] = node

    roots: list[dict[str, Any]] = []
    for node in nodes.values():
        parent = nodes.get(node["parent_id"] or "")
        if parent is None:
            roots.append(node)              # 父不存在（或本来就是根）→ 当根处理，不静默丢
        else:
            parent["children"].append(node)
    _sort_tree(roots)
    return roots


def _sort_tree(nodes: list[dict[str, Any]]) -> None:
    nodes.sort(key=lambda n: (n["sort"], n["dept_id"]))
    for node in nodes:
        _sort_tree(node["children"])


async def create_dept(*, name: str, parent_id: str | None, sort: int | None,
                      leader_user_id: str | None, actor_id: str,
                      request: Request | None = None) -> str:
    """新建部门：算物化路径 → 落库 → 写审计（`dept.create`）。"""
    clean_name = (name or "").strip()
    if not clean_name:
        raise BizError(Err.SYS_PARAM_INVALID, "部门名称不能为空")

    parent = await _require_parent(parent_id)
    if await org_repo.find_dept_by_parent_name(parent_id, clean_name) is not None:
        raise BizError(Err.ORG_DEPT_NAME_TAKEN, f"同级已存在同名部门：{clean_name}")

    level = (parent["level"] + 1) if parent else 0
    if level > MAX_DEPT_LEVEL:
        raise BizError(Err.ORG_DEPT_TOO_DEEP,
                       f"部门层级上限 {MAX_DEPT_LEVEL + 1} 层（当前会到第 {level + 1} 层）")
    if leader_user_id:
        await _require_active_user(leader_user_id, "部门负责人")

    dept_id = await org_repo.next_dept_id()
    now = _now()
    path = list(parent["path"]) + [clean_name] if parent else [clean_name]
    path_ids = list(parent["path_ids"]) + [dept_id] if parent else [dept_id]
    doc = {"_id": dept_id, "name": clean_name, "parent_id": parent_id,
           "path": path, "path_ids": path_ids, "level": level,
           "sort": 0 if sort is None else int(sort),
           "status": DeptStatus.ACTIVE.value, "leader_user_id": leader_user_id,
           "created_at": now, "updated_at": now}
    await org_repo.insert_dept(doc)

    await _audit(request, "dept.create", dept_id, clean_name,
                 after={"name": clean_name, "parent_id": parent_id, "path_ids": path_ids})
    logger.info("部门已新建 %s(%s) 层级=%d", clean_name, dept_id, level)
    return dept_id


async def _require_parent(parent_id: str | None) -> dict[str, Any] | None:
    """父部门必须存在且 `active`（`ORG-3002`）；根传 `None`。"""
    if not parent_id:
        return None
    parent = await org_repo.get_dept(parent_id)
    if parent is None or parent.get("status") != DeptStatus.ACTIVE.value:
        raise BizError(Err.ORG_DEPT_NOT_FOUND, f"父部门不存在或已停用：{parent_id}")
    return parent


async def _require_active_user(user_id: str, label: str) -> dict[str, Any]:
    """用户必须存在且 `active`。"""
    user = await org_repo.get_user(user_id)
    if user is None or user.get("status") != UserStatus.ACTIVE.value:
        raise BizError(Err.ORG_DEPT_NOT_FOUND, f"{label}不存在或已停用：{user_id}")
    return user


async def update_dept(*, dept_id: str, fields: Mapping[str, Any], actor_id: str,
                      request: Request | None = None) -> dict[str, Any]:
    """编辑部门：改名 / 移动 / 排序 / 负责人 / 状态。

    移动部门是这里最重的一步：环检测 → 重算自身路径 → **一次批量重算全部子孙**。
    """
    dept = await _require_dept(dept_id)
    before = _public_dept(dept)
    changed: dict[str, Any] = {}

    if "name" in fields and fields["name"] is not None:
        new_name = str(fields["name"]).strip()
        if new_name and new_name != dept["name"]:
            if await org_repo.find_dept_by_parent_name(dept.get("parent_id"), new_name):
                raise BizError(Err.ORG_DEPT_NAME_TAKEN, f"同级已存在同名部门：{new_name}")
            changed["name"] = new_name

    if "status" in fields and fields["status"] is not None:
        new_status = str(fields["status"])
        if new_status not in (DeptStatus.ACTIVE.value, DeptStatus.DISABLED.value):
            raise BizError(Err.SYS_PARAM_INVALID, f"部门状态非法：{new_status}")
        if new_status != dept.get("status"):
            if new_status == DeptStatus.DISABLED.value:
                active = await org_repo.count_dept_users(dept_id, UserStatus.ACTIVE.value)
                if active:
                    raise BizError(Err.ORG_DEPT_HAS_ACTIVE_USER,
                                   f"部门下仍有 {active} 个在用用户，不能停用")
            changed["status"] = new_status

    if "sort" in fields and fields["sort"] is not None:
        changed["sort"] = int(fields["sort"])
    if "leader_user_id" in fields:
        leader = fields["leader_user_id"]
        if leader:
            await _require_active_user(str(leader), "部门负责人")
        changed["leader_user_id"] = leader

    move = "parent_id" in fields and fields["parent_id"] is not None or \
        ("parent_id" in fields and fields["parent_id"] is None
         and dept.get("parent_id") not in (None, org_repo.ROOT_SENTINEL))
    if move:
        changed["parent_id"] = fields["parent_id"]

    if not changed:
        return {"dept_id": dept_id, "changed": {}}

    if "parent_id" in changed:
        await _move_dept(dept, changed["parent_id"], changed)
    elif "name" in changed:
        # 改名会让**所有子孙**的 `path`（含名字数组）失效，必须级联
        await _rename_cascade(dept, changed["name"])

    changed["updated_at"] = _now()
    await org_repo.update_dept(dept_id, changed)
    after = {**before, **{k: v for k, v in changed.items() if k != "updated_at"}}
    await _audit(request, "dept.update", dept_id, dept["name"],
                 before=before, after=_public_dept({**dept, **changed}),
                 reason=f"编辑部门：{sorted(k for k in changed if k != 'updated_at')}")
    logger.info("部门已更新 %s 字段=%s", dept_id, sorted(changed))
    return {"dept_id": dept_id, "changed": {k: v for k, v in changed.items()
                                            if k != "updated_at"}, "_after": after}


async def _move_dept(dept: Mapping[str, Any], new_parent_id: str | None,
                     changed: dict[str, Any]) -> None:
    """移动部门：环检测 + 自身路径重算 + 子孙级联重算（一次批量）。

    **环检测的方向容易写反**：要判断的是"新父是不是**我的子孙**"，
    也就是"新父的 `path_ids` 里有没有我"——而不是"我在不在新父的路径里"
    （那是"新父是我的祖先"，完全合法）。写反的后果是：正常的上移被拒，
    而真正的成环（把总部移到财务部下）反而放行，然后整棵树被写成一个环。
    """
    if new_parent_id == dept["_id"]:
        raise BizError(Err.ORG_DEPT_CYCLE, "不能把部门移动到自己下面")

    new_parent = await _require_parent(new_parent_id)
    if new_parent and dept["_id"] in (new_parent.get("path_ids") or []):
        raise BizError(Err.ORG_DEPT_CYCLE,
                       f"不能把部门移动到自己的子部门下：{new_parent['name']}")
    if await org_repo.find_dept_by_parent_name(new_parent_id, dept["name"]) \
            and new_parent_id != dept.get("parent_id"):
        raise BizError(Err.ORG_DEPT_NAME_TAKEN,
                       f"目标层级已存在同名部门：{dept['name']}")

    new_path = list(new_parent["path"]) + [dept["name"]] if new_parent else [dept["name"]]
    new_ids = list(new_parent["path_ids"]) + [dept["_id"]] if new_parent else [dept["_id"]]
    old_ids = list(dept.get("path_ids") or [])
    old_path = list(dept.get("path") or [])

    descendants = await org_repo.list_descendants(dept["_id"])
    now = _now()
    updates: list[dict[str, Any]] = [{"dept_id": dept["_id"], "path": new_path,
                                      "path_ids": new_ids, "level": len(new_ids) - 1,
                                      "updated_at": now}]
    for child in descendants:
        if child["_id"] == dept["_id"]:
            continue
        child_ids = list(child.get("path_ids") or [])
        child_path = list(child.get("path") or [])
        if child_ids[:len(old_ids)] != old_ids:
            # 理论上不会发生（同一次读一致）；真发生了就宁可报错也不要写坏路径
            raise BizError(Err.ORG_CASCADE_FAILED,
                           f"部门 {child['_id']} 的 path_ids 与父级不连续，需人工核对")
        merged_ids = new_ids + child_ids[len(old_ids):]
        merged_path = new_path + child_path[len(old_path):]
        updates.append({"dept_id": child["_id"], "path": merged_path,
                        "path_ids": merged_ids, "level": len(merged_ids) - 1,
                        "updated_at": now})

    deepest = max(u["level"] for u in updates)
    if deepest > MAX_DEPT_LEVEL:
        raise BizError(Err.ORG_DEPT_TOO_DEEP,
                       f"移动后最深层级会到第 {deepest + 1} 层，超过上限 {MAX_DEPT_LEVEL + 1}")

    try:
        await org_repo.apply_path_updates(updates)
    except Exception as exc:                                  # noqa: BLE001
        # 物化路径半截更新是最难查的坏数据：显式报错并提示人工核对
        logger.exception("部门路径级联更新失败 dept_id=%s", dept["_id"])
        raise BizError(Err.ORG_CASCADE_FAILED,
                       f"路径级联更新失败，请人工核对受影响部门："
                       f"{[u['dept_id'] for u in updates]}") from exc
    changed["path"] = new_path
    changed["path_ids"] = new_ids
    changed["level"] = len(new_ids) - 1
    logger.info("部门 %s 已移动，级联重算 %d 条", dept["_id"], len(updates) - 1)
    # 部门层级不影响数据权限判定（G-02 精确匹配），故**不**失效权限缓存


async def _rename_cascade(dept: Mapping[str, Any], new_name: str) -> None:
    """改名时级联修正子孙的 `path`（`path_ids` / `level` 不变）。"""
    old_ids = list(dept.get("path_ids") or [])
    old_path = list(dept.get("path") or [])
    new_path = old_path[:-1] + [new_name]
    descendants = await org_repo.list_descendants(dept["_id"])
    now = _now()
    updates: list[dict[str, Any]] = [{"dept_id": dept["_id"], "path": new_path,
                                      "path_ids": old_ids, "level": dept["level"],
                                      "updated_at": now}]
    for child in descendants:
        if child["_id"] == dept["_id"]:
            continue
        child_path = list(child.get("path") or [])
        updates.append({"dept_id": child["_id"],
                        "path": new_path + child_path[len(old_path):],
                        "path_ids": list(child.get("path_ids") or []),
                        "level": child["level"], "updated_at": now})
    await org_repo.apply_path_updates(updates)


async def delete_dept(*, dept_id: str, actor_id: str,
                      request: Request | None = None) -> None:
    """删除部门：**三项前置校验全过才删**（Spec §3.1）。"""
    dept = await _require_dept(dept_id)
    children = await org_repo.count_children(dept_id)
    if children:
        raise BizError(Err.ORG_DEPT_HAS_CHILD, f"该部门下仍有 {children} 个子部门")
    users = await org_repo.count_dept_users(dept_id)
    if users:
        raise BizError(Err.ORG_DEPT_HAS_USER, f"该部门下仍有 {users} 个用户（含已停用）")
    if await org_repo.dept_referenced_in_permissions(dept_id):
        raise BizError(Err.ORG_DEPT_REFERENCED,
                       "该部门被知识权限引用，请先调整相关知识的四维权限")

    await org_repo.delete_dept(dept_id)
    await _audit(request, "dept.delete", dept_id, dept["name"], before=_public_dept(dept),
                 reason="删除部门")
    logger.info("部门已删除 %s(%s)", dept["name"], dept_id)


async def _require_dept(dept_id: str) -> dict[str, Any]:
    dept = await org_repo.get_dept(dept_id)
    if dept is None:
        raise BizError(Err.ORG_DEPT_NOT_FOUND, f"部门不存在：{dept_id}")
    return dept


async def get_dept(dept_id: str) -> dict[str, Any]:
    """取单个部门（供 05 做**精确匹配**用；本模块不做判定）。"""
    return _public_dept(await _require_dept(dept_id))


# =========================================================================== 用户
def _public_user(row: Mapping[str, Any], dept_name: str | None,
                 roles: list[dict[str, Any]]) -> dict[str, Any]:
    """用户的**列表/详情视图**。

    `phone` / `email` **刻意不回显**（Spec §3.2「脱敏」）：列表页只需要 7 列，
    把联系方式带出去会让"谁都能导出全员手机号"成为顺手的事。
    `password_hash` 无论何时都不出现在这里。
    """
    return {
        "user_id": row["_id"],
        "username": row.get("username", ""),
        "real_name": row.get("real_name", ""),
        "dept_id": row.get("dept_id", ""),
        "dept_name": dept_name,
        "roles": roles,
        "status": row.get("status", UserStatus.ACTIVE.value),
        "last_login_at": row.get("last_login_at"),
    }


async def _role_briefs(role_ids: list[str], all_roles: list[dict[str, Any]] | None = None
                       ) -> list[dict[str, Any]]:
    """角色 ID → 精简视图（复用一次取回的角色表，避免逐用户查库）。"""
    mapping = {r["_id"]: r for r in (all_roles if all_roles is not None
                                     else await org_repo.list_roles())}
    return [{"role_id": rid, "code": mapping[rid]["code"], "name": mapping[rid]["name"]}
            for rid in role_ids if rid in mapping]


async def list_users(*, keyword: str | None, dept_id: str | None, status: str | None,
                     page: int, page_size: int) -> dict[str, Any]:
    """用户列表（分页）。

    三次库访问**与页大小无关**：总数 + 当前页用户 + 一次批量取角色/部门名。
    逐用户去查角色会变成 N+1（一页 20 人就是 40 次查询）。
    """
    total = await org_repo.count_users(keyword, dept_id, status)
    rows = await org_repo.list_users(keyword, dept_id, status,
                                     skip=(page - 1) * page_size, limit=page_size)
    user_ids = [r["_id"] for r in rows]
    role_ids_map = await org_repo.role_ids_of_users(user_ids)
    all_roles = await org_repo.list_roles()
    dept_names = {d["_id"]: d.get("name", "") for d in await org_repo.list_departments()}

    items = [_public_user(row,
                          dept_names.get(row.get("dept_id", "")),
                          await _role_briefs(role_ids_map.get(row["_id"], []), all_roles))
             for row in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


async def create_user(*, username: str, password: str, real_name: str, dept_id: str,
                      role_ids: list[str], phone: str | None = None,
                      email: str | None = None, actor_id: str,
                      request: Request | None = None) -> str:
    """新增用户：账号唯一 → 口令强度 → 部门/角色存在 → 落库 → 写审计。"""
    clean_username = (username or "").strip()
    if not USERNAME_RE.match(clean_username):
        raise BizError(Err.SYS_PARAM_INVALID,
                       "登录账号需以字母开头，仅含字母/数字/下划线，长度 3~32")
    ensure_password_strength(password)
    await _require_active_dept(dept_id)
    roles = await _require_roles(role_ids)
    if await org_repo.find_user_by_username(clean_username) is not None:
        raise BizError(Err.ORG_USERNAME_TAKEN, f"登录账号已存在：{clean_username}")

    user_id = await org_repo.next_user_id()
    now = _now()
    await org_repo.insert_user({
        "_id": user_id, "username": clean_username,
        "password_hash": hash_password(password),
        "real_name": (real_name or "").strip(), "dept_id": dept_id,
        "phone": phone, "email": email,
        "status": UserStatus.ACTIVE.value, "last_login_at": None,
        "created_by": actor_id, "created_at": now, "updated_at": now})
    await org_repo.add_user_roles(user_id, [r["_id"] for r in roles], actor_id, now)

    # `after` 里**不含密码与哈希**（Spec §3.2）；脱敏器是第二道闸，不是唯一一道
    await _audit(request, "user.create", user_id, clean_username, actor=actor_id,
                 after={"username": clean_username, "real_name": real_name,
                        "dept_id": dept_id,
                        "role_ids": sorted(r["_id"] for r in roles)})
    logger.info("用户已新建 %s(%s) 部门=%s 角色=%s", clean_username, user_id, dept_id,
                [r["code"] for r in roles])
    return user_id


async def _require_active_dept(dept_id: str) -> dict[str, Any]:
    """部门必须存在且 `active`（`ORG-3002`）。"""
    dept = await org_repo.get_dept(dept_id or "")
    if dept is None or dept.get("status") != DeptStatus.ACTIVE.value:
        raise BizError(Err.ORG_DEPT_NOT_FOUND, f"部门不存在或已停用：{dept_id}")
    return dept


async def _require_roles(role_ids: list[str] | None) -> list[dict[str, Any]]:
    """角色必须都存在（`ORG-2002`）。"""
    ids = list(role_ids or [])
    if not ids:
        return []
    all_roles = await org_repo.list_roles()
    mapping = {r["_id"]: r for r in all_roles}
    missing = [rid for rid in ids if rid not in mapping]
    if missing:
        raise BizError(Err.ORG_ROLE_NOT_FOUND, f"角色不存在：{missing}")
    return [mapping[rid] for rid in dict.fromkeys(ids)]


async def update_user(*, user_id: str, fields: Mapping[str, Any], actor_id: str,
                      request: Request | None = None) -> dict[str, Any]:
    """编辑用户：姓名 / 部门 / 手机 / 邮箱。

    - `username` **不可修改**（`ORG-1002`）：它是登录凭据，改了会破坏审计可追溯性
      ——历史记录里的 `actor` 指向 `user_id` 没问题，但人肉排查时对不上号。
    - 调岗（`dept_id` 变）后**立即失效该用户的权限缓存**：他下一次请求就按新部门
      判定数据权限（这就是"权限即时生效"，AD-02）。
    """
    user = await _require_user(user_id)
    before = _public_user(user, None, [])

    if "username" in fields and fields["username"] not in (None, user.get("username")):
        raise BizError(Err.ORG_USERNAME_IMMUTABLE, "登录账号不可修改（登录凭据）")

    changed: dict[str, Any] = {}
    if "real_name" in fields and fields["real_name"] is not None:
        candidate = str(fields["real_name"]).strip()
        if candidate != user.get("real_name"):
            changed["real_name"] = candidate
    if "dept_id" in fields and fields["dept_id"] not in (None, user.get("dept_id")):
        await _require_active_dept(str(fields["dept_id"]))
        changed["dept_id"] = str(fields["dept_id"])
    for key in ("phone", "email"):
        if key in fields and fields[key] != user.get(key):
            changed[key] = fields[key]

    if not changed:
        # 值没变就不写库、不写审计：否则会出现"改了又改回来"式的一串噪声记录，
        # 让真正的那次变更淹没在里面（与 config_service 的 `unchanged` 同口径）
        return {"user_id": user_id, "changed": {}}

    changed["updated_at"] = _now()
    await org_repo.update_user(user_id, changed)
    if "dept_id" in changed:
        permission_cache.invalidate_user(user_id)
        logger.info("用户 %s 调岗 %s → %s，权限缓存已失效", user_id, user.get("dept_id"),
                    changed["dept_id"])

    # `phone` / `email` **要进审计**：审计要回答的正是"谁把联系方式改成了什么"。
    # 它们在**列表接口**上不返回（Spec §3.2）是因为列表是广谱读取；
    # 而审计只有 `audit:read`（仅系统管理员）能看到，收窄它反而削弱问责能力。
    await _audit(request, "user.update", user_id, user.get("username", ""), actor=actor_id,
                 before={k: before.get(k) for k in changed if k != "updated_at"},
                 after={k: v for k, v in changed.items() if k != "updated_at"},
                 reason=f"编辑用户：{sorted(k for k in changed if k != 'updated_at')}")
    return {"user_id": user_id, "changed": {k: v for k, v in changed.items()
                                            if k != "updated_at"}}


async def set_user_status(*, user_id: str, status: str, reason: str | None,
                          actor_id: str, request: Request | None = None) -> str:
    """停用 / 启用用户。

    两道防自锁：**不能停用自己**（`ORG-2004`）、**不能停用最后一个 active 的
    系统管理员**（`ORG-2003`）。停用后失效权限缓存 → 已签发的令牌下一跳即 401
    （模块 01 的 AC-01-03）。
    """
    if status not in (UserStatus.ACTIVE.value, UserStatus.DISABLED.value):
        raise BizError(Err.SYS_PARAM_INVALID, f"用户状态非法：{status}")
    user = await _require_user(user_id)

    if status == UserStatus.DISABLED.value:
        if user_id == actor_id:
            raise BizError(Err.ORG_SELF_DISABLE, "不能停用当前登录账号")
        await _guard_last_admin(user_id)

    changed = {"status": status, "updated_at": _now()}
    await org_repo.update_user(user_id, changed)
    permission_cache.invalidate_user(user_id)

    action = "user.disable" if status == UserStatus.DISABLED.value else "user.enable"
    await _audit(request, action, user_id, user.get("username", ""), actor=actor_id,
                 before={"status": user.get("status")}, after={"status": status},
                 reason=reason or ("停用用户" if action == "user.disable" else "启用用户"))
    logger.info("用户 %s 状态 %s → %s", user_id, user.get("status"), status)
    return status


async def _guard_last_admin(user_id: str) -> None:
    """停用前确认"系统里还有别的 active 系统管理员"（`ORG-2003`）。"""
    admin_role = await org_repo.get_role_by_code(SUPER_ADMIN_CODE)
    if admin_role is None:
        return
    if admin_role["_id"] not in await org_repo.role_ids_of_user(user_id):
        return
    remaining = await org_repo.count_active_users_with_role(admin_role["_id"])
    if remaining <= 1:
        raise BizError(Err.ORG_LAST_ADMIN,
                       "不能停用系统内最后一个在用的系统管理员（否则无人能再授权）")


async def reset_password(*, user_id: str, new_password: str, actor_id: str,
                         request: Request | None = None) -> None:
    """重置密码。

    **只记事实、不含任何密码信息**（Spec §3.2）：审计的 `after` 里没有密码字段，
    脱敏器只是第二道闸。重置后失效权限缓存，强制重新登录。
    """
    user = await _require_user(user_id)
    ensure_password_strength(new_password)
    await org_repo.update_password_hash(user_id, hash_password(new_password), _now())
    permission_cache.invalidate_user(user_id)
    await _audit(request, "user.reset_pwd", user_id, user.get("username", ""),
                 actor=actor_id, reason="重置密码")
    logger.info("用户 %s 的密码已重置（审计不含任何密码信息）", user_id)


async def set_user_roles(*, user_id: str, role_ids: list[str], reason: str,
                         actor_id: str, request: Request | None = None) -> dict[str, Any]:
    """绑定角色（**差集落库**）。

    至少保留 1 个角色（`ORG-2005`）：无角色用户既进不去任何菜单，
    也让"他能看什么"变成一句无法回答的话。变更原因必填（G-11，`ORG-1003`）。
    """
    clean_reason = ensure_reason(reason)
    user = await _require_user(user_id)
    ids = list(dict.fromkeys(role_ids or []))
    if not ids:
        raise BizError(Err.ORG_ROLE_REQUIRED, "用户至少需要保留一个角色")
    roles = await _require_roles(ids)

    before_ids = await org_repo.role_ids_of_user(user_id)
    target_ids = sorted(r["_id"] for r in roles)
    added = await org_repo.add_user_roles(user_id, target_ids, actor_id, _now())
    removed = await org_repo.remove_user_roles(
        user_id, [rid for rid in before_ids if rid not in target_ids])
    permission_cache.invalidate_user(user_id)

    await _audit(request, "user.role_change", user_id, user.get("username", ""),
                 actor=actor_id, before={"role_ids": before_ids},
                 after={"role_ids": target_ids}, reason=clean_reason)
    logger.info("用户 %s 角色变更：+%s -%s", user_id, added, removed)
    return {"user_id": user_id, "added": added, "removed": removed,
            "role_ids": target_ids}


async def _require_user(user_id: str) -> dict[str, Any]:
    user = await org_repo.get_user(user_id)
    if user is None:
        raise BizError(Err.ORG_USER_NOT_FOUND, f"用户不存在：{user_id}")
    return user



# =========================================================================== 角色
def _public_role(row: Mapping[str, Any], permission_count: int = 0,
                 user_count: int = 0) -> dict[str, Any]:
    """角色的列表视图。`role_type` 就是原型 `07` 的「类型」列（内置 / 自定义）。"""
    is_system = bool(row.get("is_system"))
    return {
        "role_id": row["_id"],
        "code": row.get("code", ""),
        "name": row.get("name", ""),
        "is_system": is_system,
        "role_type": "内置" if is_system else "自定义",
        "description": row.get("description", ""),
        "permission_count": permission_count,
        "user_count": user_count,
    }


async def list_roles() -> list[dict[str, Any]]:
    """角色列表（原型 `07` 的「角色与功能权限」表 6 列）。

    `permission_count` / `user_count` 各用**一次聚合**取回，与角色数量无关
    （逐个角色数会变成 2N 次查询）。
    """
    rows = await org_repo.list_roles()
    role_ids = [r["_id"] for r in rows]
    perm_counts = await org_repo.permission_counts_by_role(role_ids)
    user_counts = await org_repo.count_role_users(role_ids)
    return [_public_role(r, perm_counts.get(r["_id"], 0), user_counts.get(r["_id"], 0))
            for r in rows]


async def create_role(*, code: str, name: str, description: str | None,
                      actor_id: str, request: Request | None = None) -> str:
    """新建**业务角色**（裁定 A）。

    三条硬约束：
    1. `code` 唯一且不得与内置角色冲突（`ORG-2006`）——内置角色的 code 是权限判定的
       锚点（如 `sys_admin` 的防自锁检查直接按 code 查），被顶掉会静默改变语义；
    2. `is_system` 恒为 `False`：内置角色只能由初始化脚本写入；
    3. **默认不授予任何功能权限**——业务角色的本职是"数据权限分组标签"，
       它的功能权限应当是一道需要显式走一次决策的步骤。
    """
    clean_code = (code or "").strip()
    clean_name = (name or "").strip()
    if not ROLE_CODE_RE.match(clean_code):
        raise BizError(Err.SYS_PARAM_INVALID,
                       "角色编码需以小写字母开头，仅含小写字母/数字/下划线，长度 3~32")
    if not clean_name:
        raise BizError(Err.SYS_PARAM_INVALID, "角色名称不能为空")
    if await org_repo.get_role_by_code(clean_code) is not None:
        raise BizError(Err.ORG_ROLE_CODE_TAKEN, f"角色编码已存在：{clean_code}")

    role_id = await org_repo.next_role_id()
    await org_repo.insert_role({
        "_id": role_id, "code": clean_code, "name": clean_name,
        "is_system": False, "description": description or "", "created_at": _now()})
    await _audit(request, "role.create", role_id, clean_code, actor=actor_id,
                 after={"code": clean_code, "name": clean_name, "is_system": False},
                 reason=f"新建业务角色：{clean_name}")
    logger.info("业务角色已新建 %s(%s)", clean_code, role_id)
    return role_id


async def update_role(*, role_id: str, fields: Mapping[str, Any], actor_id: str,
                      request: Request | None = None) -> dict[str, Any]:
    """编辑角色：`name` / `description` 可改，**`code` 不可改**（`ORG-2007`）。

    `code` 被权限判定与防自锁逻辑按值引用（`sys_admin` / `kb_admin` / `asker`），
    改它等于悄悄换掉一个系统的语义锚点。
    """
    role = await _require_role(role_id)
    if "code" in fields and fields["code"] not in (None, role.get("code")):
        raise BizError(Err.ORG_ROLE_CODE_LOCKED,
                       "角色编码不可修改（功能权限判定与防自锁逻辑按 code 引用）")

    changed: dict[str, Any] = {}
    if "name" in fields and fields["name"] is not None:
        candidate = str(fields["name"]).strip()
        if candidate and candidate != role.get("name"):
            changed["name"] = candidate
    if "description" in fields and fields["description"] != role.get("description"):
        changed["description"] = fields["description"] or ""

    if not changed:
        return {"role_id": role_id, "changed": {}}
    await org_repo.update_role(role_id, changed)
    await _audit(request, "role.update", role_id, role.get("code", ""), actor=actor_id,
                 before={k: role.get(k) for k in changed},
                 after=dict(changed),
                 reason=f"编辑角色：{sorted(changed)}")
    logger.info("角色已更新 %s 字段=%s", role_id, sorted(changed))
    return {"role_id": role_id, "changed": changed}


async def delete_role(*, role_id: str, actor_id: str,
                      request: Request | None = None) -> None:
    """删除角色（仅业务角色）。

    **E13 的清理必须经 01 的 `AuthService`**（ER-02：E13 的写入者是 01）。
    若清理失败就**不删角色**并报 `ORG-4002`——"角色已删但权限残留"会留下
    一批指向不存在角色的授权记录，比删不掉更难收拾。
    """
    role = await _require_role(role_id)
    if role.get("is_system"):
        raise BizError(Err.ORG_SYSTEM_ROLE_UNDELETABLE,
                       f"内置角色不可删除：{role.get('code')}")
    bound = (await org_repo.count_role_users([role_id])).get(role_id, 0)
    if bound:
        raise BizError(Err.ORG_ROLE_IN_USE, f"该角色仍被 {bound} 个用户使用")

    # 延迟导入：01 的 service 会 import 本模块的仓储，模块级导入会成环
    from app.services import auth_service

    try:
        await auth_service.revoke_role_permissions(role_id)
    except Exception as exc:                                  # noqa: BLE001
        logger.exception("角色 %s 的功能权限清理失败，角色不予删除", role_id)
        raise BizError(Err.ORG_ROLE_CLEANUP_FAILED,
                       f"角色权限清理失败，角色未删除：{role_id}") from exc

    await org_repo.delete_role(role_id)
    await _audit(request, "role.delete", role_id, role.get("code", ""), actor=actor_id,
                 before={"code": role.get("code"), "name": role.get("name")},
                 reason=f"删除业务角色：{role.get('name')}")
    logger.info("业务角色已删除 %s(%s)", role.get("code"), role_id)


async def _require_role(role_id: str) -> dict[str, Any]:
    role = await org_repo.get_role(role_id)
    if role is None:
        raise BizError(Err.AUTH_ROLE_NOT_FOUND, f"角色不存在：{role_id}")
    return role


async def role_exists(role_id: str) -> bool:
    """给 01 的模块用的**只读**存在性检查（E10 属 02，01 不直连）。"""
    return await org_repo.get_role(role_id) is not None


# =========================================================================== 审计
async def _audit(request: Request | None, action: str, target_id: str, target_name: str,
                 *, before: Mapping[str, Any] | None = None,
                 after: Mapping[str, Any] | None = None,
                 reason: str | None = None, actor: str | None = None,
                 extra: Mapping[str, Any] | None = None) -> None:
    """统一的审计出口。

    **不 `try/except` 包裹、不看返回值**（模块 10 §7.2 D-01）——`record()` 保证永不抛，
    审计失败只记 ERROR，绝不把业务响应变成失败。所以每个写操作都是
    "业务落库成功之后"调用它（D-02）。
    """
    kwargs: dict[str, Any] = {"target_type": _target_type_of(action), "target_id": target_id,
                              "target_name": target_name, "before": before, "after": after,
                              "reason": reason, "extra": extra}
    if request is not None:
        await audit_service.record(action, request=request, **kwargs)
    else:
        await audit_service.record(action, actor=actor or "system", **kwargs)


def _target_type_of(action: str) -> str:
    """动作名 → 目标类型（字典里已有权威定义，这里只做转发）。"""
    from app.audit import actions as action_dict

    meta = action_dict.meta_of(action)
    return meta.target_type if meta else "auth"


async def real_names_of(user_ids: Sequence[str]) -> dict[str, str]:
    """批量取用户姓名 `{user_id: real_name}`（**一次 `$in`**）。

    模块 10 的审计回填用它：`actor_name` 的正道是**写入时快照**，
    快照缺失（历史数据、或写入时用户已被删）时才回填姓名。
    为什么由 02 提供而不是让 10 直接读 `sys_users`：ER-02 规定"一集合一归属"，
    用户表读写的解释权在 02；而且这里**只返回姓名**，
    绝不把含 `password_hash` 的整条记录递出去（`org_repo.find_users_by_ids`
    返回的是原始记录，裁剪是这一层的责任）。
    """
    wanted = [u for u in dict.fromkeys(user_ids) if u]
    if not wanted:
        return {}
    rows = await org_repo.find_users_by_ids(wanted)
    return {user_id: str((row or {}).get("real_name") or "")
            for user_id, row in rows.items()}
