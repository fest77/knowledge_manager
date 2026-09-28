# -*- coding: utf-8 -*-
"""模块 01 的数据访问层。

**职责收窄记录（模块 02 落地时）**：切片① 时本文件代管了 E08~E11 的索引，
那只是权宜——E08 部门 / E09 用户 / E10 角色 / E11 用户角色绑定 的归属模块是 **02**
（总纲 §5）。现在：

| 实体 | 写入者 | 读取者 |
|---|---|---|
| E08 / E09 / E10 / E11 | **02 `org_repo`** | 01（登录要读用户、角色、部门） |
| E12 功能权限 / E13 角色功能权限 | **01（本文件）** | 02（角色列表要显示权限数） |

于是本文件对 E08~E11 只剩**只读委派**（`org_repo`）；**连 `last_login_at` 这种
小写入也交回 02**——ER-02 说的是"写必须调用归属模块"，没有"小字段除外"这一说。
"""
from __future__ import annotations

from typing import Any, Sequence

from app.core.logging import logger
from app.infra.mongo import mongo
from app.repositories import org_repo

PERMISSIONS = "sys_permissions"
ROLE_PERMISSIONS = "sys_role_permissions"

# 账号大小写不敏感：定义在 E09 的归属模块里，这里只做**转出**，
# 让老调用方（如 `audit_repo`）不必改 import 路径
USERNAME_COLLATION = org_repo.USERNAME_COLLATION


# --------------------------------------------------------------------------- 索引
async def ensure_indexes() -> None:
    """只建 **E12 / E13** 的索引；E08~E11 归 `org_repo.ensure_indexes()`。

    ⚠️ **唯一索引建不上就直接抛**——否则会出现重复权限码或重复授权，
    表现为"权限矩阵里同一格被算了两次"，比启动失败更难查。
    """
    db = mongo.require_db()
    await db[PERMISSIONS].create_index("code", unique=True, name="uq_code")
    await db[PERMISSIONS].create_index([("type", 1), ("sort", 1)], name="ix_type_sort")
    await db[ROLE_PERMISSIONS].create_index(
        [("role_id", 1), ("permission_id", 1)], unique=True, name="uq_role_perm")
    logger.info("功能权限索引已确保（2 个集合）")


# --------------------------------------------------------------------------- E08~E11 只读委派
async def find_user_by_username(username: str) -> dict[str, Any] | None:
    """按登录账号查找；**大小写不敏感**（委派给 E09 的归属模块）。"""
    return await org_repo.find_user_by_username(username)


async def find_user_by_id(user_id: str) -> dict[str, Any] | None:
    """按用户编号查找（JWT 的 `sub` 就是它）。"""
    return await org_repo.get_user(user_id)


async def list_roles_of_user(user_id: str) -> list[dict[str, Any]]:
    """用户 → 角色（E11 join E10），委派给 02。"""
    return await lists_roles(await org_repo.role_ids_of_user(user_id))


async def lists_roles(role_ids: Sequence[str]) -> list[dict[str, Any]]:
    """按 ID 集合批量取角色（一次 `$in`，无 N+1）。"""
    if not role_ids:
        return []
    cursor = mongo.collection(org_repo.ROLES).find({"_id": {"$in": list(role_ids)}})
    return await cursor.to_list(length=None)


async def get_department_name(dept_id: str) -> str | None:
    """`/auth/me` 要回 `dept_name`（E08 只读，总纲 §5 允许 01 读）。"""
    if not dept_id:
        return None
    row = await mongo.collection(org_repo.DEPARTMENTS).find_one({"_id": dept_id}, {"name": 1})
    return row["name"] if row else None


# --------------------------------------------------------------------------- E12 功能权限
async def list_permissions() -> list[dict[str, Any]]:
    """全部功能权限定义（原型 `08` 矩阵页的行标题与分组）。"""
    cursor = mongo.collection(PERMISSIONS).find({}).sort("sort", 1)
    return await cursor.to_list(length=None)


async def find_permissions_by_ids(permission_ids: Sequence[str]) -> list[dict[str, Any]]:
    """按 ID 集合取权限项（一次 `$in`）。"""
    if not permission_ids:
        return []
    cursor = mongo.collection(PERMISSIONS).find({"_id": {"$in": list(permission_ids)}})
    return await cursor.to_list(length=None)


async def list_permission_codes_of_roles(role_ids: list[str]) -> list[str]:
    """角色集合 → 功能权限码（E13 join E12）。

    一次 `$in` 查完（总纲 ER-13：禁止逐个角色查，避免 N+1）。
    """
    if not role_ids:
        return []
    links = await mongo.collection(ROLE_PERMISSIONS).find(
        {"role_id": {"$in": role_ids}}).to_list(length=None)
    if not links:
        return []
    perm_ids = [x["permission_id"] for x in links]
    perms = await mongo.collection(PERMISSIONS).find(
        {"_id": {"$in": perm_ids}}).to_list(length=None)
    return sorted({p["code"] for p in perms})


async def list_menus_of_permission_codes(codes: list[str]) -> list[dict[str, Any]]:
    """权限码 → 菜单项。

    两条口径：
    1. **判定**：用户拥有、且 `menu_path` 非空的权限项即为菜单入口。这样 `type` 保持
       "权限粒度标签"的原义，而"能不能渲染成菜单"由 `menu_path` 一个字段决定。
    2. **去重**：**按 `path` 去重**，同一路由只出现一次（保留 `sort` 最小者）。
       因为一条菜单就是一个前端路由——`faq:review` 与 `gap:read` 都挂在 `#/sediment`，
       不去重会让侧边栏把「知识沉淀」渲染两遍（切片实现时**实测到的缺陷**）。
    """
    if not codes:
        return []
    cursor = mongo.collection(PERMISSIONS).find(
        {"code": {"$in": codes}, "menu_path": {"$nin": [None, ""]}}).sort("sort", 1)
    rows = await cursor.to_list(length=None)

    seen: set[str] = set()
    menus: list[dict[str, Any]] = []
    for r in rows:
        path = r["menu_path"]
        if path in seen:
            continue
        seen.add(path)
        menus.append({"code": r["code"], "name": r["name"], "path": path, "sort": r["sort"]})
    return menus


# --------------------------------------------------------------------------- E13 角色功能权限
async def permission_ids_of_role(role_id: str) -> list[str]:
    """某角色已绑定的权限 ID 清单。"""
    cursor = mongo.collection(ROLE_PERMISSIONS).find({"role_id": role_id},
                                                     {"permission_id": 1})
    rows = await cursor.to_list(length=None)
    return sorted(r["permission_id"] for r in rows)


async def apply_role_permissions(role_id: str, add_ids: Sequence[str],
                                 remove_ids: Sequence[str], actor: str,
                                 ts: int) -> tuple[list[str], list[str]]:
    """**差集**落库，返回 `(实际新增, 实际移除)`。

    **不做全量删除重建**（01 Spec §3.5 R-03）：先删后建会有**瞬时权限真空**，
    正好落在这个窗口里的请求会被误判为无权限。
    """
    added: list[str] = []
    for permission_id in add_ids:
        result = await mongo.collection(ROLE_PERMISSIONS).update_one(
            {"role_id": role_id, "permission_id": permission_id},
            {"$setOnInsert": {"role_id": role_id, "permission_id": permission_id,
                              "granted_by": actor, "granted_at": ts}},
            upsert=True)
        if result.upserted_id is not None:
            added.append(permission_id)

    removed: list[str] = []
    if remove_ids:
        existing = await mongo.collection(ROLE_PERMISSIONS).find(
            {"role_id": role_id, "permission_id": {"$in": list(remove_ids)}}).to_list(None)
        removed = sorted(r["permission_id"] for r in existing)
        if removed:
            await mongo.collection(ROLE_PERMISSIONS).delete_many(
                {"role_id": role_id, "permission_id": {"$in": removed}})
    return sorted(added), removed


async def revoke_all_role_permissions(role_id: str) -> int:
    """清空某角色的全部授权（**只由 01 调用**；02 删除角色时经 `AuthService` 走这里）。"""
    result = await mongo.collection(ROLE_PERMISSIONS).delete_many({"role_id": role_id})
    logger.info("角色 %s 的功能权限已清空（%d 条）", role_id, result.deleted_count)
    return result.deleted_count


__all__ = [
    "PERMISSIONS", "ROLE_PERMISSIONS", "USERNAME_COLLATION",
    "ensure_indexes",
    "find_user_by_username", "find_user_by_id", "list_roles_of_user", "lists_roles",
    "get_department_name",
    "list_permissions", "find_permissions_by_ids", "list_permission_codes_of_roles",
    "list_menus_of_permission_codes",
    "permission_ids_of_role", "apply_role_permissions", "revoke_all_role_permissions",
]
