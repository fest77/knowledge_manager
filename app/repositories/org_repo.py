# -*- coding: utf-8 -*-
"""模块 02 的数据访问层：**E08 / E09 / E10 / E11 的唯一写入者**（ER-02）。

本文件同时**接管 E08~E11 的索引管理**——这是总纲 §4.1「循环依赖 #1（01 ↔ 02）」
的破解动作：切片① 时 01 代管这些索引只是权宜，现在归位到实体的归属模块。

**E12 / E13（功能权限与角色授权）的写入者是 01**，本文件对它们**只读**
（列表页要显示「功能权限数」）。02 删除角色时**不得**自己删 `sys_role_permissions`，
必须调 `AuthService.revoke_role_permissions()`——否则会出现"角色已删但权限残留"。

一处**刻意的实现偏差**（`DEC-02-3`）：Spec §2.2 要求给 `sys_users.real_name`
建**文本索引**，本实现改用**不区分大小写的正则查询**。原因：MongoDB 的文本索引
按分隔符切词，对中文**不切分**——「张伟」是整条 token，搜「张」永远搜不到，
搜索框会"看起来能用其实不能用"。成员姓名查询是精确 SQL 式的子串匹配需求，
正则才是对的工具。代价：数据量大时全表扫；当前量级（<10 万用户）可接受。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from pymongo.collation import Collation

from app.core.logging import logger
from app.infra.mongo import mongo
from app.repositories import perm_repo

DEPARTMENTS = "sys_departments"
USERS = "sys_users"
ROLES = "sys_roles"
USER_ROLES = "sys_user_roles"
# 只读（写入者是 01）
PERMISSIONS = "sys_permissions"
ROLE_PERMISSIONS = "sys_role_permissions"
# 只读（写入者是 05）；部门删除前要检查是否被引用（ORG-3008）。
# 集合名从**属主**那里取（`perm_repo` 是 E07 的唯一写入者）：两处各写一份字面量，
# 将来改名就会漏改一处，而症状是"部门删不掉"这种看不出原因的报错。
KB_PERMISSIONS = perm_repo.KB_PERMISSIONS

# 同级重名唯一索引需要哨兵：Mongo 的唯一索引把多个 `null` 视为重复，
# 若根部门（`parent_id=null`）有两个就会误判冲突。写入时把根写成 `__root__`。
ROOT_SENTINEL = "__root__"

# 账号大小写不敏感（模块 02 §3.2「username 唯一，大小写不敏感比较」）
USERNAME_COLLATION = Collation(locale="en", strength=2)

MAX_DEPT_LEVEL = 4          # 层级上限 5 层（level 0~4）
DEPT_ID_WIDTH = 4
USER_ID_WIDTH = 6
ROLE_ID_WIDTH = 4


def _parent_key(parent_id: str | None) -> str:
    """把 `parent_id` 归一成索引用的哨兵值。"""
    return parent_id or ROOT_SENTINEL


async def _next_id(collection: str, prefix: str, width: int) -> str:
    """生成 `{prefix}{零填充序号}` 形式的编号。

    取当日/全表**最大编号 + 1**。零填充保证字典序等于数值序，所以
    `sort=[("_id", -1)]` 的 `find_one` 就是"最大号"，不需要额外计数器表——
    少一张表就少一处并发写点（多进程同时建部门时靠唯一索引兜底重试）。
    """
    pattern = f"^{prefix}\\d{{{width}}}$"
    row = await mongo.collection(collection).find_one(
        {"_id": {"$regex": pattern}}, {"_id": 1}, sort=[("_id", -1)])
    current = int(str(row["_id"])[len(prefix):]) if row else 0
    return f"{prefix}{current + 1:0{width}d}"


async def next_dept_id() -> str:
    """下一个部门编号。"""
    return await _next_id(DEPARTMENTS, "DEPT", DEPT_ID_WIDTH)


async def next_user_id() -> str:
    """下一个用户编号（`U` + 6 位）。"""
    return await _next_id(USERS, "U", USER_ID_WIDTH)


async def next_role_id() -> str:
    """下一个角色编号。"""
    return await _next_id(ROLES, "ROLE", ROLE_ID_WIDTH)


# --------------------------------------------------------------------------- 索引
async def _ensure_username_index(db) -> None:
    """建 `username` 唯一索引，并**确保它的 collation 就是大小写不敏感的那个**。

    ⚠️ 两个实测踩到的坑（从切片① 原样迁来，务必保留）：
    1. `create_index()` 对「同名 + 同键」的索引是**幂等**的，**不会**因为 collation
       变了就重建 → 会出现「测试库（每次 drop 重建）大小写不敏感 ✔、
       正式库（保留旧索引）大小写敏感 ✘」。
    2. Mongo 读回的 `collation` 是**含 13 个字段的 SON**，而 `Collation.document`
       只有 `{locale, strength}` → 整体比较永远不相等 → 每次启动都删重建。
    """
    name = "uq_username"
    want = USERNAME_COLLATION.document

    def _matches(spec: dict | None) -> bool:
        if not spec:
            return False
        return (spec.get("locale") == want["locale"]
                and spec.get("strength") == want["strength"])

    cursor = await db[USERS].list_indexes()
    existing = await cursor.to_list(length=None)
    current = next((i for i in existing if i["name"] == name), None)
    if current is not None and not _matches(current.get("collation")):
        logger.warning("索引 %s 的 collation 与期望不符（现有=%s 期望=%s），先删除后重建",
                       name, current.get("collation"), want)
        await db[USERS].drop_index(name)
    await db[USERS].create_index("username", unique=True, name=name,
                                 collation=USERNAME_COLLATION)


async def ensure_indexes() -> None:
    """建 E08~E11 的全部索引（幂等）。唯一索引建不上就直接抛，否则会出现重复账号。"""
    db = mongo.require_db()

    await _ensure_username_index(db)
    await db[USERS].create_index([("dept_id", 1), ("status", 1)], name="ix_dept_status")

    await db[ROLES].create_index("code", unique=True, name="uq_code")

    await db[USER_ROLES].create_index(
        [("user_id", 1), ("role_id", 1)], unique=True, name="uq_user_role")
    await db[USER_ROLES].create_index("role_id", name="ix_role")

    await db[DEPARTMENTS].create_index(
        [("parent_id", 1), ("name", 1)], unique=True, name="uq_parent_name")
    await db[DEPARTMENTS].create_index([("status", 1), ("sort", 1)], name="ix_status_sort")
    await db[DEPARTMENTS].create_index("path_ids", name="ix_path_ids")

    logger.info("组织架构索引已确保（4 个集合）")


# --------------------------------------------------------------------------- 部门
async def list_departments() -> list[dict[str, Any]]:
    """全部部门（按 `path_ids` 字典序即树的先序；由服务层组树）。"""
    cursor = mongo.collection(DEPARTMENTS).find({}).sort([("level", 1), ("sort", 1)])
    return await cursor.to_list(length=None)


# ---------------------------------------------------------------- 批量只读（模块 05 用）
# 模块 05 保存四维权限时要**批量**校验 ID 有效性、回显时要**批量**取名字。
# 逐条 `get_dept()` 在"配 50 个部门"时会变成 50 次往返（ER-13 要拦的 N+1），
# 所以这里给出三个 `$in` 版本。它们**只读**，不改变本模块的写入边界。
async def find_depts_by_ids(dept_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """按编号批量取部门（`{dept_id: 记录}`）。"""
    wanted = [d for d in dict.fromkeys(dept_ids) if d]
    if not wanted:
        return {}
    cursor = mongo.collection(DEPARTMENTS).find({"_id": {"$in": wanted}})
    return {str(row["_id"]): row for row in await cursor.to_list(length=None)}


async def find_roles_by_ids(role_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """按编号批量取角色（`{role_id: 记录}`）。"""
    wanted = [r for r in dict.fromkeys(role_ids) if r]
    if not wanted:
        return {}
    cursor = mongo.collection(ROLES).find({"_id": {"$in": wanted}})
    return {str(row["_id"]): row for row in await cursor.to_list(length=None)}


async def find_users_by_ids(user_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """按编号批量取用户（`{user_id: 记录}`）。

    ⚠️ 返回的是**原始记录**（含 `password_hash`）。调用方**只允许**取
    `real_name` / `dept_id` / `username` 这几个字段用于展示，
    **绝不能把整条记录塞进响应**——`password_hash` 泄漏一次就够致命了。
    不在这一层裁剪字段是为了保持"仓储返回记录"的一致性，
    裁剪责任明确交给服务层的 `_brief_user()`。
    """
    wanted = [u for u in dict.fromkeys(user_ids) if u]
    if not wanted:
        return {}
    cursor = mongo.collection(USERS).find({"_id": {"$in": wanted}})
    return {str(row["_id"]): row for row in await cursor.to_list(length=None)}


async def get_dept(dept_id: str) -> dict[str, Any] | None:
    """按编号取部门。"""
    return await mongo.collection(DEPARTMENTS).find_one({"_id": dept_id})


async def find_dept_by_parent_name(parent_id: str | None, name: str) -> dict[str, Any] | None:
    """同级同名部门（唯一索引的哨兵口径由 `_parent_key` 统一）。"""
    return await mongo.collection(DEPARTMENTS).find_one(
        {"parent_id": _parent_key(parent_id), "name": name})


async def insert_dept(doc: Mapping[str, Any]) -> str:
    """插入部门，返回编号。"""
    payload = dict(doc)
    payload["parent_id"] = _parent_key(payload.get("parent_id"))
    await mongo.collection(DEPARTMENTS).insert_one(payload)
    return str(payload["_id"])


async def update_dept(dept_id: str, fields: Mapping[str, Any]) -> int:
    """更新部门字段（`parent_id` 会自动归一成哨兵值）。"""
    payload = dict(fields)
    if "parent_id" in payload:
        payload["parent_id"] = _parent_key(payload["parent_id"])
    result = await mongo.collection(DEPARTMENTS).update_one({"_id": dept_id},
                                                            {"$set": payload})
    return result.modified_count


async def delete_dept(dept_id: str) -> int:
    """删除部门（前置校验由服务层负责）。"""
    result = await mongo.collection(DEPARTMENTS).delete_one({"_id": dept_id})
    return result.deleted_count


async def list_descendants(path_id: str) -> list[dict[str, Any]]:
    """取某部门的**全部子孙**（含自身），靠 `path_ids` 一次查完（不递归查库）。"""
    cursor = mongo.collection(DEPARTMENTS).find({"path_ids": path_id})
    return await cursor.to_list(length=None)


async def apply_path_updates(updates: Sequence[Mapping[str, Any]]) -> int:
    """批量写入 `path` / `path_ids` / `level`（移动部门后的级联重算）。

    用 `bulk_write` 一次往返；任一条失败即抛，由服务层转成 `ORG-4001`
    并提示"需人工核对 `path_ids`"——半截的物化路径比报错更难查。
    """
    from pymongo import UpdateOne

    if not updates:
        return 0
    ops = [UpdateOne({"_id": u["dept_id"]},
                     {"$set": {"path": u["path"], "path_ids": u["path_ids"],
                               "level": u["level"], "updated_at": u["updated_at"]}})
           for u in updates]
    result = await mongo.collection(DEPARTMENTS).bulk_write(ops, ordered=True)
    return result.modified_count


async def count_children(dept_id: str) -> int:
    """直接子部门数（删除前置校验 ORG-3006）。"""
    return await mongo.collection(DEPARTMENTS).count_documents({"parent_id": dept_id})


async def count_dept_users(dept_id: str, status: str | None = None) -> int:
    """部门下的用户数；传 `status` 则只数该状态（停用校验用 `active`，删除用全部）。"""
    query: dict[str, Any] = {"dept_id": dept_id}
    if status:
        query["status"] = status
    return await mongo.collection(USERS).count_documents(query)


async def count_users_by_dept() -> dict[str, int]:
    """一次聚合出「每个部门的用户数」，供部门树的 `user_count`（避免 N+1）。"""
    pipeline = [{"$group": {"_id": "$dept_id", "n": {"$sum": 1}}}]
    # 注意：AsyncCollection.aggregate() 返回的是 **coroutine**，必须先 await 才有游标
    cursor = await mongo.collection(USERS).aggregate(pipeline)
    rows = await cursor.to_list(length=None)
    return {r["_id"]: r["n"] for r in rows if r["_id"]}


async def dept_referenced_in_permissions(dept_id: str) -> bool:
    """该部门是否被 `kb_permissions.departments` 引用（ORG-3008）。

    `kb_permissions` 的属主是 05；本模块**只读**。集合尚不存在时
    `count_documents` 返回 0，即"未被引用"——这是正确的降级方向：
    05 未落地时不存在悬空引用的可能。
    """
    try:
        found = await mongo.collection(KB_PERMISSIONS).count_documents(
            {"departments": dept_id}, limit=1)
    except Exception as exc:                                  # noqa: BLE001
        logger.warning("检查部门权限引用失败（按未引用处理）：%s", exc)
        return False
    return found > 0


# --------------------------------------------------------------------------- 用户
def _keyword_query(keyword: str) -> dict[str, Any]:
    """姓名/账号的模糊匹配（`DEC-02-3`：中文用正则而非文本索引）。"""
    import re

    pattern = re.escape(keyword.strip())
    return {"$or": [{"real_name": {"$regex": pattern, "$options": "i"}},
                    {"username": {"$regex": pattern, "$options": "i"}}]}


async def count_users(keyword: str | None = None, dept_id: str | None = None,
                      status: str | None = None) -> int:
    """用户列表总数（分页契约要 `total`）。"""
    return await mongo.collection(USERS).count_documents(
        _user_filter(keyword, dept_id, status))


def _user_filter(keyword: str | None, dept_id: str | None,
                 status: str | None) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if keyword and keyword.strip():
        query.update(_keyword_query(keyword))
    if dept_id:
        query["dept_id"] = dept_id
    if status:
        query["status"] = status
    return query


async def list_users(keyword: str | None, dept_id: str | None, status: str | None,
                     *, skip: int, limit: int) -> list[dict[str, Any]]:
    """分页取用户（按编号排序，保证分页稳定）。"""
    cursor = (mongo.collection(USERS)
              .find(_user_filter(keyword, dept_id, status))
              .sort("_id", 1).skip(skip).limit(limit))
    return await cursor.to_list(length=limit)


async def get_user(user_id: str) -> dict[str, Any] | None:
    """按编号取用户（含 `password_hash`，调用方负责不回显）。"""
    return await mongo.collection(USERS).find_one({"_id": user_id})


async def find_user_by_username(username: str) -> dict[str, Any] | None:
    """按登录账号查找；**大小写不敏感**（与唯一索引同一 collation）。"""
    return await mongo.collection(USERS).find_one(
        {"username": username}, collation=USERNAME_COLLATION)


async def insert_user(doc: Mapping[str, Any]) -> str:
    """插入用户，返回编号。"""
    await mongo.collection(USERS).insert_one(dict(doc))
    return str(doc["_id"])


async def update_user(user_id: str, fields: Mapping[str, Any]) -> int:
    """更新用户字段（**不用于密码**，密码走 `update_password_hash`）。"""
    result = await mongo.collection(USERS).update_one({"_id": user_id}, {"$set": dict(fields)})
    return result.modified_count


async def update_password_hash(user_id: str, password_hash: str, ts: int) -> int:
    """写入新的 bcrypt 哈希；**返回值不含任何密码信息**。"""
    result = await mongo.collection(USERS).update_one(
        {"_id": user_id},
        {"$set": {"password_hash": password_hash, "updated_at": ts}})
    return result.modified_count


async def touch_last_login(user_id: str, ts: int) -> None:
    """登录成功后回填 `last_login_at`（E09）。失败**不影响登录**，只记 WARN。

    这个"小写入"也放在 02 的仓储里，是刻意的：ER-02 要求"写必须调用归属模块"，
    没有"只写一个字段就例外"这一说。01 的登录流程调它，而不是自己 update E09。
    """
    try:
        await mongo.collection(USERS).update_one(
            {"_id": user_id}, {"$set": {"last_login_at": ts, "updated_at": ts}})
    except Exception:                                         # noqa: BLE001
        logger.warning("回填 last_login_at 失败 user_id=%s", user_id, exc_info=True)


async def count_active_users_with_role(role_id: str) -> int:
    """持有该角色且**状态为 active** 的用户数（ORG-2003 防自锁的判据）。"""
    links = await mongo.collection(USER_ROLES).find({"role_id": role_id}).to_list(length=None)
    user_ids = [x["user_id"] for x in links]
    if not user_ids:
        return 0
    return await mongo.collection(USERS).count_documents(
        {"_id": {"$in": user_ids}, "status": "active"})


# --------------------------------------------------------------------------- 角色绑定
async def role_ids_of_user(user_id: str) -> list[str]:
    """用户的角色 ID 清单。"""
    cursor = mongo.collection(USER_ROLES).find({"user_id": user_id}, {"role_id": 1})
    rows = await cursor.to_list(length=None)
    return sorted(r["role_id"] for r in rows)


async def role_ids_of_users(user_ids: Sequence[str]) -> dict[str, list[str]]:
    """批量取多个用户的角色 ID（一次 `$in`，避免列表页 N+1；ER-13 同源思路）。"""
    if not user_ids:
        return {}
    cursor = mongo.collection(USER_ROLES).find({"user_id": {"$in": list(user_ids)}})
    rows = await cursor.to_list(length=None)
    grouped: dict[str, list[str]] = {}
    for r in rows:
        grouped.setdefault(r["user_id"], []).append(r["role_id"])
    return {k: sorted(v) for k, v in grouped.items()}


async def add_user_roles(user_id: str, role_ids: Iterable[str], actor: str,
                         ts: int) -> list[str]:
    """**差集新增**：`$addToSet` 语义靠唯一索引 + 逐条 upsert 实现，返回新增的 ID。"""
    added: list[str] = []
    for role_id in role_ids:
        result = await mongo.collection(USER_ROLES).update_one(
            {"user_id": user_id, "role_id": role_id},
            {"$setOnInsert": {"user_id": user_id, "role_id": role_id,
                              "granted_by": actor, "granted_at": ts}},
            upsert=True)
        if result.upserted_id is not None:
            added.append(role_id)
    return sorted(added)


async def remove_user_roles(user_id: str, role_ids: Iterable[str]) -> list[str]:
    """**差集移除**（只删确实存在的），返回被移除的 ID。"""
    targets = list(role_ids)
    if not targets:
        return []
    existing = await mongo.collection(USER_ROLES).find(
        {"user_id": user_id, "role_id": {"$in": targets}}).to_list(length=None)
    removed = sorted(r["role_id"] for r in existing)
    if removed:
        await mongo.collection(USER_ROLES).delete_many(
            {"user_id": user_id, "role_id": {"$in": removed}})
    return removed


async def count_role_users(role_ids: Sequence[str]) -> dict[str, int]:
    """一次聚合出「每个角色的用户数」（角色列表的「用户数」列）。"""
    if not role_ids:
        return {}
    pipeline = [
        {"$match": {"role_id": {"$in": list(role_ids)}}},
        {"$group": {"_id": "$role_id", "n": {"$sum": 1}}},
    ]
    cursor = await mongo.collection(USER_ROLES).aggregate(pipeline)
    rows = await cursor.to_list(length=None)
    return {r["_id"]: r["n"] for r in rows}


# --------------------------------------------------------------------------- 角色
async def list_roles() -> list[dict[str, Any]]:
    """全部角色（内置在前，按编号升序）。"""
    cursor = mongo.collection(ROLES).find({}).sort("_id", 1)
    return await cursor.to_list(length=None)


async def get_role(role_id: str) -> dict[str, Any] | None:
    """按编号取角色。"""
    return await mongo.collection(ROLES).find_one({"_id": role_id})


async def get_role_by_code(code: str) -> dict[str, Any] | None:
    """按编码取角色（`code` 唯一）。"""
    return await mongo.collection(ROLES).find_one({"code": code})


async def insert_role(doc: Mapping[str, Any]) -> str:
    """插入角色，返回编号。"""
    await mongo.collection(ROLES).insert_one(dict(doc))
    return str(doc["_id"])


async def update_role(role_id: str, fields: Mapping[str, Any]) -> int:
    """更新角色字段。"""
    result = await mongo.collection(ROLES).update_one({"_id": role_id}, {"$set": dict(fields)})
    return result.modified_count


async def delete_role(role_id: str) -> int:
    """删除角色（前置校验与 E13 清理由服务层负责）。"""
    result = await mongo.collection(ROLES).delete_one({"_id": role_id})
    return result.deleted_count


async def permission_counts_by_role(role_ids: Sequence[str]) -> dict[str, int]:
    """一次聚合出「每个角色的功能权限数」（E13 只读）。

    Spec 的 `permission_count` 要能对上原型 `07` 的 4 / 16 / 22，
    所以这里数的是**绑定关系条数**，不是去重后的权限码数——两者在本项目等价
    （`uq_role_perm` 唯一索引保证同一角色不会重复绑定同一权限）。
    """
    if not role_ids:
        return {}
    pipeline = [
        {"$match": {"role_id": {"$in": list(role_ids)}}},
        {"$group": {"_id": "$role_id", "n": {"$sum": 1}}},
    ]
    cursor = await mongo.collection(ROLE_PERMISSIONS).aggregate(pipeline)
    rows = await cursor.to_list(length=None)
    return {r["_id"]: r["n"] for r in rows}


__all__ = [
    "DEPARTMENTS", "USERS", "ROLES", "USER_ROLES", "PERMISSIONS", "ROLE_PERMISSIONS",
    "KB_PERMISSIONS", "ROOT_SENTINEL", "USERNAME_COLLATION", "MAX_DEPT_LEVEL",
    "ensure_indexes", "next_dept_id", "next_user_id", "next_role_id",
    "list_departments", "get_dept", "find_dept_by_parent_name", "insert_dept",
    "update_dept", "delete_dept", "list_descendants", "apply_path_updates",
    "count_children", "count_dept_users", "count_users_by_dept",
    "dept_referenced_in_permissions",
    "count_users", "list_users", "get_user", "find_user_by_username", "insert_user",
    "update_user", "update_password_hash", "touch_last_login",
    "count_active_users_with_role",
    "find_depts_by_ids", "find_roles_by_ids", "find_users_by_ids",
    "role_ids_of_user", "role_ids_of_users", "add_user_roles", "remove_user_roles",
    "count_role_users",
    "list_roles", "get_role", "get_role_by_code", "insert_role", "update_role",
    "delete_role", "permission_counts_by_role",
]
