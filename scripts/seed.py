# -*- coding: utf-8 -*-
"""初始化演示数据（幂等，可反复执行）。

用法：
    .venv\\Scripts\\python.exe scripts\\seed.py            # 写入/更新演示数据
    .venv\\Scripts\\python.exe scripts\\seed.py --drop     # 先清掉本切片涉及的 6 个集合再写

数据来源：
  - 部门    ← 原型 07_组织架构与系统配置页.pen
  - 角色    ← 模块 01 §2.3 D 表（3 内置 4/16/22）+ 裁定 A 的 1 个业务角色
  - 权限码  ← 模块 01 §2.3 的 34 个码
  - 用户    ← 原型 07 的用户表（含 1 个已停用账号，用于验证 AUTH-2002）

演示密码从环境变量 SEED_DEMO_PASSWORD 读取；未设置时用文档里写明的默认值。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.enums import (BuiltinRole, DeptStatus, PermissionType,   # noqa: E402
                            UserStatus)
from app.core.logging import logger                        # noqa: E402
from app.core.security import hash_password                # noqa: E402
from app.infra.mongo import mongo                          # noqa: E402
from app.repositories import (audit_repo, auth_repo, config_repo,   # noqa: E402
                              doc_repo, faq_repo, gap_repo, import_repo, metric_repo,
                              org_repo,
                              perm_repo, qa_repo)
from app.services.audit_service import audit_service       # noqa: E402
from app.services.config_service import config_service     # noqa: E402
from app.services.permission_cache import permission_cache  # noqa: E402
from app.services.perm_cache import perm_cache              # noqa: E402
from app.services.faq_cache import faq_cache                 # noqa: E402
from app.infra.sse import sse_hub                            # noqa: E402

DEFAULT_DEMO_PASSWORD = "Demo@12345"
# E08~E11 归 02（org_repo），E12/E13 归 01（auth_repo）——见总纲 §5
# E04/E05（模块 03）也没有种子数据，但必须一起 drop + 建索引：
# 否则用例之间会串知识单元，且 `file_hash` 唯一索引不存在时
# `create()` 会 fail-closed 报 DOC-4004（那是保护，不是 bug）
COLLECTIONS = [org_repo.DEPARTMENTS, org_repo.ROLES, org_repo.USER_ROLES,
               auth_repo.PERMISSIONS, auth_repo.ROLE_PERMISSIONS, org_repo.USERS,
               config_repo.CONFIG, audit_repo.AUDIT_LOGS,
               doc_repo.DOCUMENTS, doc_repo.CATEGORIES,
               import_repo.IMPORT_TASKS, perm_repo.KB_PERMISSIONS,
               qa_repo.SESSIONS, qa_repo.MESSAGES, qa_repo.QA_LOGS,
               faq_repo.CANDIDATES, faq_repo.FAQS, faq_repo.CACHE_COPY,
               gap_repo.GAPS, metric_repo.BUCKETS]

# ---------------------------------------------------------------- 部门（E08）
# 对齐原型 `07`：总部下 6 个部门 + 「报销组」挂在财务部下（**唯一的二级节点**，
# 让层级/级联重算/子树筛选在演示数据里就有真实样本可测）
DEPARTMENTS = [
    ("DEPT0001", "总部", None, ["总部"], ["DEPT0001"], 0, 10),
    ("DEPT0002", "人力资源部", "DEPT0001", ["总部", "人力资源部"], ["DEPT0001", "DEPT0002"], 1, 20),
    ("DEPT0003", "财务部", "DEPT0001", ["总部", "财务部"], ["DEPT0001", "DEPT0003"], 1, 30),
    ("DEPT0004", "技术部", "DEPT0001", ["总部", "技术部"], ["DEPT0001", "DEPT0004"], 1, 40),
    ("DEPT0005", "总经办", "DEPT0001", ["总部", "总经办"], ["DEPT0001", "DEPT0005"], 1, 50),
    ("DEPT0006", "客户服务部", "DEPT0001", ["总部", "客户服务部"],
     ["DEPT0001", "DEPT0006"], 1, 60),
    ("DEPT0007", "市场部", "DEPT0001", ["总部", "市场部"], ["DEPT0001", "DEPT0007"], 1, 70),
    ("DEPT0008", "报销组", "DEPT0003", ["总部", "财务部", "报销组"],
     ["DEPT0001", "DEPT0003", "DEPT0008"], 2, 80),
]

# ---------------------------------------------------------------- 功能权限（E12）：34 个码
# (code, name, type, parent_code, menu_path, sort)
PERMISSIONS = [
    ("qa:use", "AI 智能问答", PermissionType.API, None, "#/qa", 10),
    ("qa:history", "历史会话查看", PermissionType.API, None, None, 11),
    ("perm:check", "鉴权判定接口", PermissionType.API, None, None, 12),
    ("qa:feedback", "答案反馈", PermissionType.BUTTON, None, None, 13),

    ("doc:read", "知识单元台账查看", PermissionType.MENU, None, "#/docs", 20),
    ("doc:upload", "知识单元上传 / 批量导入", PermissionType.BUTTON, "doc:read", None, 21),
    ("doc:edit", "知识单元编辑", PermissionType.BUTTON, "doc:read", None, 22),
    ("doc:toggle", "知识单元启用 / 停用", PermissionType.BUTTON, "doc:read", None, 23),
    ("doc:delete", "知识单元软删除", PermissionType.BUTTON, "doc:read", None, 24),
    ("doc:category", "分类树维护", PermissionType.BUTTON, "doc:read", None, 25),
    ("doc:chunk", "切片查看与调整", PermissionType.API, "doc:read", None, 26),
    ("perm:manage", "四维数据权限配置", PermissionType.BUTTON, "doc:read", None, 27),

    ("faq:review", "FAQ 挖掘候选审核与发布", PermissionType.MENU, None, "#/sediment", 30),
    ("faq:manage", "已发布 FAQ 管理 / 缓存开关", PermissionType.BUTTON, "faq:review", None, 31),
    ("gap:read", "知识缺口查看", PermissionType.MENU, None, "#/sediment", 32),
    ("gap:convert", "知识缺口一键转建", PermissionType.BUTTON, "gap:read", None, 33),

    ("metric:read", "运营看板与数据大盘", PermissionType.MENU, None, "#/dashboard", 40),
    ("metric:export", "看板数据导出", PermissionType.BUTTON, "metric:read", None, 41),

    ("org:manage", "组织架构管理", PermissionType.MENU, None, "#/system", 50),
    ("dept:create", "新建部门", PermissionType.BUTTON, "org:manage", None, 51),
    ("dept:edit", "编辑部门", PermissionType.BUTTON, "org:manage", None, 52),
    ("dept:delete", "删除部门", PermissionType.BUTTON, "org:manage", None, 53),
    ("user:manage", "用户管理", PermissionType.MENU, None, "#/system", 54),
    ("user:create", "新增用户", PermissionType.BUTTON, "user:manage", None, 55),
    ("user:edit", "编辑用户", PermissionType.BUTTON, "user:manage", None, 56),
    ("user:disable", "停用 / 启用用户", PermissionType.BUTTON, "user:manage", None, 57),
    ("user:reset_pwd", "重置密码", PermissionType.BUTTON, "user:manage", None, 58),
    ("role:manage", "角色管理", PermissionType.MENU, None, "#/system", 59),
    ("role:grant", "功能权限分配", PermissionType.BUTTON, "role:manage", None, 60),

    ("model:config", "模型服务参数配置", PermissionType.BUTTON, None, None, 61),
    ("system:config", "系统配置", PermissionType.BUTTON, None, None, 62),
    ("system:seed", "演示数据初始化", PermissionType.BUTTON, None, None, 63),

    ("audit:read", "审计日志查看", PermissionType.MENU, None, "#/audit", 70),
    ("audit:export", "审计日志导出", PermissionType.BUTTON, "audit:read", None, 71),
]

# ---------------------------------------------------------------- 角色（E10）
ASKER = ["qa:use", "qa:history", "perm:check", "qa:feedback"]                       # 4
KB_ADMIN = ASKER + ["doc:read", "doc:upload", "doc:edit", "doc:toggle", "doc:delete",
                    "doc:category", "doc:chunk", "perm:manage",
                    "faq:review", "faq:manage", "gap:read", "gap:convert"]          # 16
SYS_ADMIN = ASKER + ["metric:read", "metric:export",
                     "org:manage", "dept:create", "dept:edit", "dept:delete",
                     "user:manage", "user:create", "user:edit", "user:disable",
                     "user:reset_pwd", "role:manage", "role:grant",
                     "model:config", "system:config", "system:seed",
                     "audit:read", "audit:export"]                                  # 22

ROLES = [
    ("ROLE0001", "asker", "普通用户 / 提问者", True, "AI 问答、历史会话", ASKER),
    ("ROLE0002", "kb_admin", "知识管理员", True,
     "文档增删改、切片调整、四维权限配置、FAQ 审核、缺口处理", KB_ADMIN),
    ("ROLE0003", "sys_admin", "系统管理员", True,
     "组织架构、角色、功能权限、看板、模型参数", SYS_ADMIN),
    # 裁定 A：业务角色，仅作四维数据权限分组标签，默认不含任何功能权限
    ("ROLE0004", "management", "管理层", False,
     "仅作四维数据权限分组（PRD 2.9.9 场景）；默认不含任何功能权限", []),
]

# ---------------------------------------------------------------- 用户（E09）
USERS = [
    ("U000001", "lina", "李娜", "DEPT0005", "ROLE0003", UserStatus.ACTIVE),
    ("U000002", "zhangwei", "张伟", "DEPT0002", "ROLE0002", UserStatus.ACTIVE),
    ("U000003", "wangqiang", "王强", "DEPT0004", "ROLE0001", UserStatus.ACTIVE),
    ("U000004", "zhaolei", "赵磊", "DEPT0003", "ROLE0001", UserStatus.DISABLED),
]


async def write_seed(db, password: str) -> dict[str, int]:
    """把演示数据写进**已连接**的 db（幂等）。返回各集合条数。测试直接复用本函数。"""
    # 内置角色集合必须与枚举一致（PRD 2.9.2 的封闭集合，防止被悄悄改掉）
    builtin = {r[1] for r in ROLES if r[3]}
    assert builtin == set(BuiltinRole), f"内置角色与枚举不一致：{builtin}"

    now = int(time.time())
    await org_repo.ensure_indexes()      # E08~E11（归属 02）
    await auth_repo.ensure_indexes()     # E12/E13（归属 01）

    for _id, name, parent_id, path, path_ids, level, sort in DEPARTMENTS:
        await db[org_repo.DEPARTMENTS].update_one(
            {"_id": _id},
            {"$set": {"name": name, "parent_id": parent_id, "path": path,
                      "path_ids": path_ids, "level": level, "sort": sort,
                      "status": DeptStatus.ACTIVE.value, "updated_at": now},
             "$setOnInsert": {"created_at": now}},
            upsert=True)

    perm_id_by_code: dict[str, str] = {}
    for idx, (code, _name, _ptype, _parent, _menu, _sort) in enumerate(PERMISSIONS, start=1):
        perm_id_by_code[code] = f"PERM{idx:04d}"
    for idx, (code, name, ptype, parent, menu_path, sort) in enumerate(PERMISSIONS, start=1):
        await db[auth_repo.PERMISSIONS].update_one(
            {"_id": f"PERM{idx:04d}"},
            {"$set": {"code": code, "name": name, "type": ptype.value,
                      "parent_id": perm_id_by_code.get(parent) if parent else None,
                      "menu_path": menu_path, "sort": sort},
             "$setOnInsert": {"created_at": now}},
            upsert=True)

    for role_id, code, name, is_system, desc, codes in ROLES:
        await db[org_repo.ROLES].update_one(
            {"_id": role_id},
            {"$set": {"code": code, "name": name, "is_system": is_system,
                      "description": desc}, "$setOnInsert": {"created_at": now}},
            upsert=True)
        await db[auth_repo.ROLE_PERMISSIONS].delete_many({"role_id": role_id})
        if codes:
            await db[auth_repo.ROLE_PERMISSIONS].insert_many([
                {"_id": f"{role_id}:{perm_id_by_code[c]}", "role_id": role_id,
                 "permission_id": perm_id_by_code[c], "granted_by": "seed", "granted_at": now}
                for c in codes])

    pwd_hash = hash_password(password)
    for user_id, username, real_name, dept_id, role_id, status in USERS:
        await db[org_repo.USERS].update_one(
            {"_id": user_id},
            {"$set": {"username": username, "real_name": real_name, "dept_id": dept_id,
                      "status": status.value, "password_hash": pwd_hash, "updated_at": now},
             "$setOnInsert": {"created_at": now, "last_login_at": None,
                              "created_by": "seed"}},
            upsert=True)
        await db[org_repo.USER_ROLES].update_one(
            {"_id": f"{user_id}:{role_id}"},
            {"$set": {"user_id": user_id, "role_id": role_id,
                      "granted_by": "seed", "granted_at": now}},
            upsert=True)

    # 系统配置（E22）：预置项按默认值补齐；已存在的值**不被覆盖**，
    # 所以"管理员改过阈值"不会因为重跑种子而回退
    await config_repo.ensure_indexes()
    inserted = await config_service.bootstrap()
    if inserted:
        logger.info("系统配置新增 %d 项（其余沿用库中已有值）", inserted)

    # 审计集合（E21）：无种子数据，只保证索引（含 spool_id 唯一稀疏）存在。
    # ASGITransport 不会触发 lifespan，所以测试路径必须靠这里建索引。
    await audit_repo.ensure_indexes()
    await doc_repo.ensure_indexes()      # E04/E05（归属 03）
    await import_repo.ensure_indexes()   # E06（归属 04）
    await perm_repo.ensure_indexes()     # E07（归属 05）
    await qa_repo.ensure_indexes()       # E14/E02/E15（归属 06）
    await faq_repo.ensure_indexes()      # E16/E17/E18 副本（归属 07）
    await gap_repo.ensure_indexes()      # E19（归属 08）
    await metric_repo.ensure_indexes()   # E20（归属 09）

    return {name: await db[name].count_documents({}) for name in COLLECTIONS}


async def drop_collections(db) -> None:
    """清空本模块涉及的集合（`--drop` 用）。"""
    for name in COLLECTIONS:
        await db[name].drop()
    # 内存里的配置缓存、审计进程内状态与权限缓存也必须一起失效，
    # 否则清库后仍会读到旧配置、沿用旧的编号序列与熔断状态、或命中已作废的权限快照
    config_service.reset()
    audit_service.reset()
    permission_cache.invalidate_all()
    # 模块 05 的**判定结果缓存**也必须清：它是进程级单例，按 `(doc_id, version)`
    # 缓存 E07 记录。清库后 doc_id 会被复用，而缓存里还留着上一个用例的记录
    # → 新用例收到"凭空多出来的权限"，测试结果取决于执行顺序（最难查的一类失败）
    perm_cache.reset()
    # 模块 06 的 SSE 总线与 FAQ 缓存同理：跨用例存活会让下一个用例
    # 拿到上一个用例的 task_id 绑定（表现为"越权订阅校验突然失效"）
    sse_hub.reset()
    faq_cache.reset()


async def seed(drop: bool) -> None:
    """命令行入口：连接 → （可选清库）→ 写种子 → 断开。"""
    await mongo.connect()
    db = mongo.require_db()
    if drop:
        await drop_collections(db)
        logger.info("已清空 %d 个集合", len(COLLECTIONS))
    counts = await write_seed(db, os.getenv("SEED_DEMO_PASSWORD") or DEFAULT_DEMO_PASSWORD)
    logger.info("种子完成：%s", counts)
    logger.info("演示密码：%s（可用环境变量 SEED_DEMO_PASSWORD 覆盖）",
                "<自定义>" if os.getenv("SEED_DEMO_PASSWORD") else DEFAULT_DEMO_PASSWORD)
    await mongo.close()


if __name__ == "__main__":
    asyncio.run(seed(drop="--drop" in sys.argv))
