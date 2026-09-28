# -*- coding: utf-8 -*-
"""模块 05 的数据访问层：**E07 知识单元数据权限 `kb_permissions` 的唯一写入者**（ER-02）。

## 为什么这张表是全系统最关键的一张

它决定"谁能读哪篇知识"。三个设计后果：

| 设计 | 为什么 |
|---|---|
| **`doc_id` 唯一索引**（R-6） | 鉴权主查询是 `doc_id IN [...]`，必须走唯一索引；一条记录对应一篇文档，多了就说不清以谁为准 |
| **文档级而非切片级**（AD-03） | 权限变更只写**一条**记录（< 10ms），不必回写几十万切片 → 这是"即时生效"的物理基础（AD-02） |
| **`version` 每次 +1**（R-7） | ① 缓存按 `(doc_id, version)` 判新旧，旧 key 自然失效，不需要任何主动失效逻辑；② 审计可追溯"改到第几版" |

## 默认拒绝（R-2）落在"查不到记录"这一支

`find_by_docs()` 只返回**查到的**记录，缺失的 doc 由服务层判 deny。
**绝不能**在这个函数里"没查到就补一条默认记录"——那会把"默认拒绝"
偷偷变成"默认有一条空记录"，而空记录的含义是"当前无人可读"，
与"没有记录"在审计与排查上是两件事。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from app.core.logging import logger
from app.infra.mongo import mongo

# E07 集合名（**唯一定义处**）。模块 02 的"部门删除前置校验"（`ORG-3008`）要读它，
# 但读的是**别人家的表**，所以那边是从这里 import 常量，而不是自己再写一份字面量
# ——两处字面量会有"改了这里忘了那里"的风险，而症状是"部门删不掉"这种莫名其妙的报错。
KB_PERMISSIONS = "kb_permissions"
# 编号前缀：`PM{6位序列}`（与 `DOC` / `IMP` 同一套做法）
PERM_ID_PREFIX = "PM"
PERM_ID_WIDTH = 6


async def ensure_indexes() -> None:
    """建 E07 的索引（Spec §2.1）。

    - `doc_id` **唯一**：鉴权主查询，也是"一个文档最多一条权限记录"的强制手段
    - `is_global`：看板统计"公开知识数"
    - `departments` / `roles` / `users`：**多键索引**，用于反查
      "该部门被授权了哪些文档"（部门删除前置校验 `ORG-3008`、管理端排查）

    ⚠️ 数组字段上的普通索引在 Mongo 里就是多键索引（每个元素一条索引项），
    所以 `create_index("departments")` 已经能支撑 `{departments: "DEPT0002"}` 的查询。
    """
    db = mongo.require_db()
    collection = db[KB_PERMISSIONS]
    await collection.create_index("doc_id", name="uq_doc_id", unique=True)
    await collection.create_index("is_global", name="ix_is_global")
    await collection.create_index("departments", name="ix_departments")
    await collection.create_index("roles", name="ix_roles")
    await collection.create_index("users", name="ix_users")
    logger.info("权限索引已确保（%s）", KB_PERMISSIONS)


async def next_perm_id() -> str:
    """生成权限记录编号 `PM{6位序列}`。

    与 `DOC` / `IMP` 不同，这里**不按日期分段**：权限记录的数量级是"文档数"，
    日期段会让编号变长却不增加信息量；而它从不对外展示（原型 `04` 显示的是
    `DOC` 编号），所以只求唯一、不求可读。

    ⚠️ **不能用 `count_documents() + 1` 当序列**：删过记录之后计数会回退，
    于是新编号撞上已存在的 `_id` → `DuplicateKeyError`，而报错点在"保存权限"
    这种低频操作上，很难联想到这里。所以仍然"查当日/当前最大值再自增"。
    """
    row = await mongo.collection(KB_PERMISSIONS).find_one(
        {"_id": {"$regex": f"^{PERM_ID_PREFIX}\\d{{{PERM_ID_WIDTH}}}$"}}, {"_id": 1},
        sort=[("_id", -1)])
    seq = int(str(row["_id"])[len(PERM_ID_PREFIX):]) + 1 if row else 1
    return f"{PERM_ID_PREFIX}{seq:0{PERM_ID_WIDTH}d}"


async def count_records() -> int:
    """记录总数（生成序列号与看板统计用）。"""
    return await mongo.collection(KB_PERMISSIONS).count_documents({})


async def get_by_doc(doc_id: str) -> dict[str, Any] | None:
    """按文档取权限记录（单文档判定 / 配置回显）。"""
    return await mongo.collection(KB_PERMISSIONS).find_one({"doc_id": doc_id})


async def find_by_docs(doc_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """**一次 `$in` 批量取**（ER-13，鉴权链路的热路径）。

    为什么必须是 `$in` 而不是循环单查：一次问答召回 50 个切片（放大 5 倍后），
    逐条查就是 50 次往返 —— 那是 AC-05-07 明确要拦的 N+1，
    而它带来的延迟会直接叠加在用户等待的问答链路上。

    返回 `{doc_id: 记录}`；**缺失的 doc 不在返回值里**，由调用方按"默认拒绝"处理。
    """
    wanted = [d for d in dict.fromkeys(doc_ids) if d]
    if not wanted:
        return {}
    cursor = mongo.collection(KB_PERMISSIONS).find({"doc_id": {"$in": wanted}})
    rows = await cursor.to_list(length=None)
    return {str(row["doc_id"]): row for row in rows}


async def upsert(
    doc_id: str, *,
    is_global: bool,
    departments: Sequence[str],
    roles: Sequence[str],
    users: Sequence[str],
    reason: str,
    updated_by: str,
    ts_ms: int,
) -> dict[str, Any]:
    """写入 / 更新一条权限记录，**返回写入后的完整记录**。

    `version` 的自增在**服务层**算好（它要拿旧值做审计 `before`），
    这里只负责"写"。用 `find_one_and_update` + `upsert=True` + `$setOnInsert`：
    ① 一次往返拿到结果（省掉"写完再查"的第二次 IO）；
    ② `created_at` 只在插入时写，更新时不动它——否则"这条权限从什么时候开始存在"
       就查不出来了，而那是审计里最常被问的一个问题。
    """
    collection = mongo.collection(KB_PERMISSIONS)
    existing = await collection.find_one({"doc_id": doc_id}, {"version": 1})
    version = int((existing or {}).get("version") or 0) + 1
    record_id = existing.get("_id") if existing else await next_perm_id()

    await collection.update_one(
        {"doc_id": doc_id},
        {
            "$set": {
                "is_global": bool(is_global),
                "departments": list(dict.fromkeys(departments)),
                "roles": list(dict.fromkeys(roles)),
                "users": list(dict.fromkeys(users)),
                "version": version,
                "reason": reason,
                "updated_by": updated_by,
                "updated_at": ts_ms,
            },
            "$setOnInsert": {"_id": record_id, "doc_id": doc_id,
                             "created_at": ts_ms},
        },
        upsert=True,
    )
    row = await collection.find_one({"doc_id": doc_id})
    logger.info("权限已保存 %s version=%d（%d部门/%d角色/%d用户 global=%s）",
                doc_id, version, len(departments), len(roles), len(users), is_global)
    return dict(row or {})


async def count_by_dimension(dimension: str, value: str) -> int:
    """反查某一维授权了多少篇文档（管理端排查用，如"财务部能看到哪些知识"）。"""
    if dimension not in ("departments", "roles", "users"):
        raise ValueError(f"未知维度：{dimension}")
    return await mongo.collection(KB_PERMISSIONS).count_documents({dimension: value})


async def drop_collection() -> None:
    """清空权限集合（仅测试路径调用）。"""
    await mongo.require_db().drop_collection(KB_PERMISSIONS)


async def all_doc_ids_with_records() -> Iterable[str]:
    """列出所有已配置权限的 `doc_id`（脚本核对"摘要是否与 E07 一致"用）。"""
    cursor = mongo.collection(KB_PERMISSIONS).find({}, {"doc_id": 1})
    return [str(row["doc_id"]) for row in await cursor.to_list(length=None)]


def summarize(record: Mapping[str, Any]) -> dict[str, Any]:
    """把 E07 记录压成 E04 的 `permission_summary`（§3.2 的计算口径）。

    ⚠️ 带上 `version` 是**刻意的**：03 据此判断摘要是否过期
    （"E07 已经到第 7 版，摘要还写着第 5 版"= 需要刷新）。
    没有它就只能靠 `updated_at` 猜，而时间戳在"同一毫秒内改两次"时不可靠。

    **`label` 不在这里算**：那是 03 的展示职责，由 `DocService` 落库
    （它才知道列表筛选用得到哪些标签）。
    """
    return {
        "is_global": bool(record.get("is_global")),
        "dept_cnt": len(record.get("departments") or []),
        "role_cnt": len(record.get("roles") or []),
        "user_cnt": len(record.get("users") or []),
        "version": int(record.get("version") or 0),
    }


__all__ = [
    "KB_PERMISSIONS", "PERM_ID_PREFIX", "PERM_ID_WIDTH",
    "ensure_indexes", "next_perm_id", "count_records", "get_by_doc", "find_by_docs",
    "upsert", "count_by_dimension", "drop_collection", "all_doc_ids_with_records",
    "summarize",
]
