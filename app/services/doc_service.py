# -*- coding: utf-8 -*-
"""模块 03 的服务层：`DocService`（E04）+ `CategoryService`（E05）。

**本模块是 04/05/06/08/09 的唯一入口**（§3.14 的内部契约）：那些模块要读知识单元、
要建台账、要回填切片数，一律调这里的方法，**不直连 `kb_documents`**（ER-02）。

三条最容易写错的边界（§1.4），代码里逐条守住：

| # | 边界 | 落点 |
|---|---|---|
| ① | `permission_summary` 只是列表标签 | 不是鉴权依据；本文件**无任何 allow/deny 判定**（ER-03） |
| ② | 切片 `enabled` 只能经 04 写 | 本文件不 import milvus、不碰 `kb_chunks_v2` |
| ③ | 列表**不因调用者角色/部门而变** | 查询条件里没有 `dept_id` / `role` 相关的过滤（AC-03-05） |

**03/04 连续批次约定**（总纲 §4.2）：本步**先不写** `ImportService.set_chunks_enabled()`
那一处调用——此时系统里还没有任何切片，该调用是空操作。04 落地后在
`toggle_document()` 里补约 3 行并重跑本模块全部测试。
"""
from __future__ import annotations

import time
from typing import Any, Mapping, Sequence

from fastapi import Request
from pymongo.errors import DuplicateKeyError

from app.core.enums import DocStatus, ImportStatus
from app.core.errors import BizError, Err
from app.core.logging import logger
from app.repositories import doc_repo
from app.services import chunk_store
from app.services.audit_service import audit_service
from app.services.chunk_store import ChunkStoreError

TITLE_MAX = 200
CATEGORY_NAME_MAX = 50
TAGS_MAX = 10
TAG_LEN_MAX = 20
DEDUP_ITEMS_MAX = 200
PAGE_SIZE_MAX = 200
SORT_WHITELIST = ("updated_at", "created_at")
# 分类树层级上限（与 02 模块部门树同值：5 层 → level 0~4）
MAX_LEVEL = 4
# 导入"进行中"的三个状态（决定能否编辑/删除/启用）
_IN_PROGRESS = (ImportStatus.PENDING, ImportStatus.PARSING, ImportStatus.EMBEDDING)


def _now_ms() -> int:
    return int(time.time() * 1000)


# =========================================================================== 派生展示
def permission_label_text(summary: Mapping[str, Any] | None) -> str:
    """权限标签文案（§2.5 ①，逐字对齐原型 `docTblR*C4T`）。

    **这不是鉴权**：它只是把 `permission_summary` 渲染成一句话。
    05 改了 E07 而忘了回填时，这句话会变陈旧——但问答鉴权仍读 E07，不受影响（AC-03-19）。
    """
    data = dict(summary or {})
    if data.get("is_global"):
        return "全局公开"
    parts: list[str] = []
    for key, unit in (("dept_cnt", "部门"), ("role_cnt", "角色"), ("user_cnt", "用户")):
        count = int(data.get(key) or 0)
        if count > 0:
            parts.append(f"{count}{unit}")
    if parts:
        return f"受限({'/'.join(parts)})"
    return "未配置(默认不可读)"


def status_text(doc: Mapping[str, Any], *, deleted_view: bool = False) -> str:
    """状态列文案（§2.5 ②）。

    优先级是刻意的：**已删除 > 导入中/失败 > 启用/停用**。
    否则一篇"导入失败且已删除"的文档会显示成"已删除"，看起来像"正常删掉的"。
    """
    if doc.get("deleted_at") is not None and deleted_view:
        return "已删除"
    state = doc.get("import_status")
    if state == ImportStatus.FAILED.value:
        return "导入失败"
    if state in (s.value for s in _IN_PROGRESS):
        return "导入中"
    return "已启用" if doc.get("status") == DocStatus.ENABLED.value else "已停用"


def to_list_item(doc: Mapping[str, Any], category_path: str | None,
                 *, deleted_view: bool = False) -> dict[str, Any]:
    """台账列表的一行（9 列，对齐 AC-03-01）。

    `deleted_view` 必须由调用方显式传：状态文案的优先级是"已删除 > 导入中 > 启用/停用"，
    默认视图里一篇软删除的文档**不该出现**，所以只有回收站视图才把已删除作为首选文案。
    """
    return {
        "doc_id": doc["_id"],
        "doc_no": doc.get("doc_no") or doc["_id"],
        "title": doc.get("title", ""),
        "file_name": doc.get("file_name", ""),
        "file_ext": doc.get("file_ext", ""),
        "file_size": doc.get("file_size", 0),
        "category_id": doc.get("category_id"),
        "category_path": category_path or "",
        "tags": list(doc.get("tags") or []),
        "chunk_count": int(doc.get("chunk_count") or 0),
        "status": doc.get("status", DocStatus.DISABLED.value),
        "status_text": status_text(doc, deleted_view=deleted_view),
        "import_status": doc.get("import_status", ImportStatus.PENDING.value),
        "permission_label": (doc.get("permission_summary") or {}).get("label", "unconfigured"),
        "permission_text": permission_label_text(doc.get("permission_summary")),
        "updated_at": doc.get("updated_at", 0),
        "created_at": doc.get("created_at", 0),
        "deleted_at": doc.get("deleted_at"),
    }


# =========================================================================== 分类
class CategoryService:
    """E05 的业务规则。分类是**纯组织维度**，所以硬删除（§2.2 的说明）。"""

    async def list_tree(self, *, with_doc_count: bool = True) -> list[dict[str, Any]]:
        """分类树。

        `doc_count` 的语义是**含子分类**的未软删除文档数（§2.2）。实现方式：
        一次聚合拿"每个分类的直属文档数"，再沿 `path_ids` 向下累加——
        逐分类查会变成 N 次查询，而树常有几十个节点。
        """
        rows = await doc_repo.list_categories()
        direct = await doc_repo.doc_counts_by_category() if with_doc_count else {}
        nodes: dict[str, dict[str, Any]] = {}
        for row in rows:
            nodes[row["_id"]] = {
                "category_id": row["_id"], "name": row.get("name", ""),
                "parent_id": None if row.get("parent_id") in
                (None, doc_repo.ROOT_SENTINEL) else row["parent_id"],
                "path": list(row.get("path") or []),
                "path_ids": list(row.get("path_ids") or []),
                "level": int(row.get("level") or 0),
                "sort": int(row.get("sort") or 0),
                "doc_count": 0, "children": [],
            }
        # 累加：把每个分类的直属数加到它自己与**所有祖先**上
        for category_id, count in direct.items():
            node = nodes.get(category_id)
            if node is None:
                continue
            for ancestor_id in node["path_ids"]:
                if ancestor_id in nodes:
                    nodes[ancestor_id]["doc_count"] += count

        roots: list[dict[str, Any]] = []
        for node in nodes.values():
            parent = nodes.get(node["parent_id"] or "")
            (parent["children"] if parent else roots).append(node)
        _sort_tree(roots)
        return roots

    async def create(self, *, name: str, parent_id: str | None, sort: int | None,
                     actor_id: str, request: Request | None = None) -> str:
        """新建分类：名称校验 → 父分类校验 → 层级 → 同级重名 → 落库 → 审计。"""
        clean = _clean_category_name(name)
        parent = await self._require_parent(parent_id)
        level = (parent["level"] + 1) if parent else 0
        if level > MAX_LEVEL:
            raise BizError(Err.DOC_CATEGORY_TOO_DEEP,
                           f"分类层级上限 {MAX_LEVEL + 1} 层（当前会到第 {level + 1} 层）")
        if await doc_repo.find_category_by_parent_name(parent_id, clean) is not None:
            raise BizError(Err.DOC_CATEGORY_NAME_TAKEN, f"同级已存在同名分类：{clean}")

        category_id = await doc_repo.next_category_id()
        now = _now_ms()
        await doc_repo.insert_category({
            "_id": category_id, "name": clean, "parent_id": parent_id,
            "path": list(parent["path"]) + [clean] if parent else [clean],
            "path_ids": list(parent["path_ids"]) + [category_id] if parent
            else [category_id],
            "level": level, "sort": 0 if sort is None else int(sort), "doc_count": 0,
            "created_by": actor_id, "updated_by": actor_id,
            "created_at": now, "updated_at": now})
        await _audit(request, "category.create", "category", category_id, clean,
                     actor=actor_id, after={"name": clean, "parent_id": parent_id})
        logger.info("分类已新建 %s(%s) 层级=%d", clean, category_id, level)
        return category_id

    async def update(self, *, category_id: str, fields: Mapping[str, Any],
                     actor_id: str, request: Request | None = None) -> dict[str, Any]:
        """编辑 / 移动 / 排序。改名与移动都要**级联重算子树的物化路径**。"""
        node = await self._require(category_id)
        before = {k: node.get(k) for k in ("name", "parent_id", "sort")}
        changed: dict[str, Any] = {}

        if fields.get("name") is not None:
            clean = _clean_category_name(str(fields["name"]))
            if clean != node.get("name"):
                if await doc_repo.find_category_by_parent_name(node.get("parent_id"),
                                                              clean):
                    raise BizError(Err.DOC_CATEGORY_NAME_TAKEN, f"同级已有：{clean}")
                changed["name"] = clean
        if "sort" in fields and fields["sort"] is not None:
            changed["sort"] = int(fields["sort"])
        if "parent_id" in fields:
            target = fields["parent_id"]
            current = node.get("parent_id")
            if target != (None if current in (None, doc_repo.ROOT_SENTINEL) else current):
                changed["parent_id"] = target

        if not changed:
            return {"category_id": category_id, "changed": {}}

        if "parent_id" in changed:
            await self._move(node, changed["parent_id"], changed)
        elif "name" in changed:
            await self._rename(node, changed["name"])

        changed["updated_by"] = actor_id
        changed["updated_at"] = _now_ms()
        await doc_repo.update_category(category_id, changed)
        await _audit(request, "category.update", "category", category_id,
                     node.get("name", ""), actor=actor_id, before=before,
                     after={k: v for k, v in changed.items()
                            if k in ("name", "parent_id", "sort")},
                     reason="编辑分类")
        return {"category_id": category_id,
                "changed": {k: v for k, v in changed.items()
                            if k in ("name", "parent_id", "sort")}}

    async def _move(self, node: Mapping[str, Any], new_parent_id: str | None,
                    changed: dict[str, Any]) -> None:
        """移动分类：环检测 → 层级校验 → 自身与**全部子孙**一次批量重算。

        环检测的方向：判断"新父是不是**我的子孙**"，也就是"新父的 `path_ids` 里有没有我"。
        写反了会把正常的上移拒掉，却放行真正的成环（模块 02 的部门树踩过这个坑）。
        """
        if new_parent_id == node["_id"]:
            raise BizError(Err.DOC_CATEGORY_CYCLE, "不能把分类移动到自己下面")
        parent = await self._require_parent(new_parent_id)
        if parent and node["_id"] in (parent.get("path_ids") or []):
            raise BizError(Err.DOC_CATEGORY_CYCLE,
                           f"不能把分类移动到自己的子分类下：{parent['name']}")
        if await doc_repo.find_category_by_parent_name(new_parent_id, node["name"]) \
                and new_parent_id != node.get("parent_id"):
            raise BizError(Err.DOC_CATEGORY_NAME_TAKEN,
                           f"目标层级已存在同名分类：{node['name']}")

        new_path = list(parent["path"]) + [node["name"]] if parent else [node["name"]]
        new_ids = list(parent["path_ids"]) + [node["_id"]] if parent else [node["_id"]]
        old_ids = list(node.get("path_ids") or [])
        old_path = list(node.get("path") or [])

        descendants = await doc_repo.list_category_subtree(node["_id"])
        now = _now_ms()
        updates: list[dict[str, Any]] = [{"category_id": node["_id"], "path": new_path,
                                          "path_ids": new_ids,
                                          "level": len(new_ids) - 1,
                                          "updated_at": now}]
        for child in descendants:
            if child["_id"] == node["_id"]:
                continue
            child_ids = list(child.get("path_ids") or [])
            child_path = list(child.get("path") or [])
            if child_ids[:len(old_ids)] != old_ids:
                raise BizError(Err.DOC_CASCADE_FAILED,
                               f"分类 {child['_id']} 的 path_ids 与父级不连续")
            merged_ids = new_ids + child_ids[len(old_ids):]
            updates.append({"category_id": child["_id"],
                            "path": new_path + child_path[len(old_path):],
                            "path_ids": merged_ids, "level": len(merged_ids) - 1,
                            "updated_at": now})

        deepest = max(u["level"] for u in updates)
        if deepest > MAX_LEVEL:
            raise BizError(Err.DOC_MOVE_TOO_DEEP,
                           f"移动后最深层级会到第 {deepest + 1} 层，超过上限 {MAX_LEVEL + 1}")
        try:
            await doc_repo.apply_category_paths(updates)
        except Exception as exc:                              # noqa: BLE001
            logger.exception("分类路径级联更新失败 category_id=%s", node["_id"])
            raise BizError(Err.DOC_CASCADE_FAILED,
                           f"路径级联失败，请人工核对："
                           f"{[u['category_id'] for u in updates]}") from exc
        changed["path"] = new_path
        changed["path_ids"] = new_ids
        changed["level"] = len(new_ids) - 1

    async def _rename(self, node: Mapping[str, Any], new_name: str) -> None:
        """改名：子孙的 `path`（名字数组）要重写，`path_ids` / `level` 不变。"""
        old_path = list(node.get("path") or [])
        new_path = old_path[:-1] + [new_name]
        now = _now_ms()
        updates: list[dict[str, Any]] = [{"category_id": node["_id"], "path": new_path,
                                          "path_ids": list(node.get("path_ids") or []),
                                          "level": node["level"], "updated_at": now}]
        for child in await doc_repo.list_category_subtree(node["_id"]):
            if child["_id"] == node["_id"]:
                continue
            updates.append({"category_id": child["_id"],
                            "path": new_path + list(child.get("path") or [])[len(old_path):],
                            "path_ids": list(child.get("path_ids") or []),
                            "level": child["level"], "updated_at": now})
        await doc_repo.apply_category_paths(updates)

    async def delete(self, *, category_id: str, actor_id: str,
                     request: Request | None = None) -> int:
        """删除分类（硬删）。

        前置：无子分类（`DOC-3005`）、无**未软删除**的文档（`DOC-3006`）。
        只软删除的文档不算阻塞——把它们置为"未分类"是安全的（回收站里的东西
        本来就不参与检索），AC-03-15 明确要求这种情况**成功且 `category_id` 变 `null`**。
        """
        node = await self._require(category_id)
        children = await doc_repo.count_category_children(category_id)
        if children:
            raise BizError(Err.DOC_CATEGORY_HAS_CHILD, f"该分类下仍有 {children} 个子分类")
        live = await doc_repo.count_docs_in_categories([category_id])
        if live:
            raise BizError(Err.DOC_CATEGORY_HAS_DOC, f"该分类下仍有 {live} 个知识单元")

        detached = await _detach_deleted_docs(category_id, actor_id)
        # 先递归减掉祖先的 doc_count，再删自己（顺序反了会让父节点的计数多算这一支）
        await _shift_ancestor_counts(node, -int(node.get("doc_count") or 0))
        await doc_repo.delete_category(category_id)
        await _audit(request, "category.delete", "category", category_id,
                     node.get("name", ""), actor=actor_id,
                     before={"name": node.get("name"),
                             "path_ids": node.get("path_ids")},
                     reason="删除分类")
        logger.info("分类已删除 %s(%s)，%d 篇回收站文档转为未分类", node.get("name"),
                    category_id, detached)
        return detached

    async def recount(self, *, actor_id: str,
                      request: Request | None = None) -> dict[str, Any]:
        """全量重算 `doc_count`（兜底接口，原型未画但在 Spec §3.12）。

        增量维护总会因为异常路径漂移；有一个"重算并回报差异"的兜底入口，
        排障时就不必手工跑脚本。
        """
        rows = await doc_repo.list_categories()
        direct = await doc_repo.doc_counts_by_category()
        expected: dict[str, int] = {r["_id"]: 0 for r in rows}
        for category_id, count in direct.items():
            node = next((r for r in rows if r["_id"] == category_id), None)
            if node is None:
                continue
            for ancestor_id in (node.get("path_ids") or []):
                if ancestor_id in expected:
                    expected[ancestor_id] += count
        diff = {cid: {"before": int(next(r for r in rows
                                         if r["_id"] == cid).get("doc_count") or 0),
                      "after": value}
                for cid, value in expected.items()
                if int(next(r for r in rows if r["_id"] == cid).get("doc_count") or 0)
                != value}
        try:
            if diff:
                await doc_repo.update_doc_counts(expected, _now_ms())
        except Exception as exc:                              # noqa: BLE001
            logger.exception("分类计数重算失败")
            raise BizError(Err.DOC_RECOUNT_FAILED, f"计数重算失败：{exc}") from exc
        await _audit(request, "category.recount", "category", "all", "全部分类",
                     actor=actor_id, after={"changed": len(diff)},
                     reason="分类文档计数全量重算")
        return {"checked": len(rows), "changed": diff}

    async def _require(self, category_id: str) -> dict[str, Any]:
        node = await doc_repo.get_category(category_id)
        if node is None:
            raise BizError(Err.DOC_CATEGORY_NOT_FOUND, f"分类不存在：{category_id}")
        return node

    async def _require_parent(self, parent_id: str | None) -> dict[str, Any] | None:
        if not parent_id:
            return None
        return await self._require(parent_id)

    async def path_of(self, category_id: str | None) -> str:
        """分类的展示路径（`公司制度 / 财务报销`），供列表与 06 的溯源卡片用。"""
        if not category_id:
            return ""
        node = await doc_repo.get_category(category_id)
        return " / ".join(node.get("path") or []) if node else ""


def _clean_category_name(name: str) -> str:
    """分类名：1~50 字，且**禁止含 `/`**（会与物化路径的分隔符混淆，`DOC-1003`）。"""
    clean = (name or "").strip()
    if not clean or len(clean) > CATEGORY_NAME_MAX or "/" in clean:
        raise BizError(Err.DOC_CATEGORY_NAME_INVALID,
                       f"分类名需 1~{CATEGORY_NAME_MAX} 字且不能含 '/'")
    return clean


def _sort_tree(nodes: list[dict[str, Any]]) -> None:
    nodes.sort(key=lambda n: (n["sort"], n["category_id"]))
    for node in nodes:
        _sort_tree(node["children"])


async def _detach_deleted_docs(category_id: str, actor_id: str) -> int:
    """把该分类下**已软删除**的文档置为未分类（`category_id=null`）。

    为什么是它们而不是全部：未软删除的已经在上面被 `DOC-3006` 挡住了。
    """
    from app.infra.mongo import mongo

    result = await mongo.collection(doc_repo.DOCUMENTS).update_many(
        {"category_id": category_id, "deleted_at": {"$ne": None}},
        {"$set": {"category_id": None, "updated_by": actor_id, "updated_at": _now_ms()}})
    return int(result.modified_count)


async def _shift_ancestor_counts(node: Mapping[str, Any], delta: int) -> None:
    """沿 `path_ids` 给所有祖先（含自己）的 `doc_count` 加 `delta`。"""
    if delta == 0:
        return
    pairs: dict[str, int] = {}
    rows = await doc_repo.list_categories()
    by_id = {r["_id"]: r for r in rows}
    for ancestor_id in (node.get("path_ids") or []):
        ancestor = by_id.get(ancestor_id)
        if ancestor is not None:
            pairs[ancestor_id] = max(0, int(ancestor.get("doc_count") or 0) + delta)
    await doc_repo.update_doc_counts(pairs, _now_ms())


# =========================================================================== 知识单元
class DocService:
    """E04 的业务规则 + 对外（04/05/06/08/09）的内部契约。"""

    async def count_by_status(self) -> dict[str, int]:
        """知识单元总数 / 已启用数（模块 09 Spec §3.1 R-8 点名的接口）。

        为什么 09 的看板不查 `metric_buckets` 而要实时计数：
        「总数」是**当前值**而不是时序累计量，删掉一篇文档后它必须**立即**变化
        （AC-09-22）。预聚合桶做不到这一点——它会一直显示删除前的数字。

        两次 `count_documents` 而不是一次 `$facet`：`kb_documents` 的量级是
        "几百到几万"，带索引的 count 各自都是毫秒级，可读性比省一次往返更值。
        """
        total = await doc_repo.count_documents(deleted=False)
        enabled = await doc_repo.count_documents(deleted=False,
                                                 status=DocStatus.ENABLED.value)
        return {"total": int(total), "enabled": int(enabled)}

    # ------------------------------------------------------------------ 列表与详情
    async def list_documents(self, *, page: int = 1, page_size: int = 20,
                             category_id: str | None = None, include_sub: bool = True,
                             status: str | None = None, permission_label: str | None = None,
                             keyword: str | None = None, sort_by: str = "updated_at",
                             view: str = "default") -> dict[str, Any]:
        """台账列表：三个筛选器 + 关键词 + 分页 + 汇总（AC-03-01/02/03）。

        `page_size > 200` **报错而不静默截断**（AC-03-02）：静默截断会让前端显示
        "共 128 条、每页 20"却拿回 200 条，用户无法察觉自己看到的是被改过的分页。
        """
        if page < 1 or not 1 <= page_size <= PAGE_SIZE_MAX:
            raise BizError(Err.DOC_PARAM_INVALID,
                           f"page ≥ 1，page_size ∈ [1,{PAGE_SIZE_MAX}]")
        if sort_by not in SORT_WHITELIST:
            raise BizError(Err.DOC_PARAM_INVALID, f"sort_by 只允许 {list(SORT_WHITELIST)}")
        if view not in ("default", "deleted"):
            raise BizError(Err.DOC_PARAM_INVALID, "view 只允许 default / deleted")
        if status and status not in (DocStatus.ENABLED.value, DocStatus.DISABLED.value):
            raise BizError(Err.DOC_PARAM_INVALID, f"status 非法：{status}")
        if permission_label and permission_label not in ("global", "limited",
                                                         "unconfigured"):
            raise BizError(Err.DOC_PARAM_INVALID, f"permission_label 非法：{permission_label}")

        category_ids: Sequence[str] | None = None
        if category_id:
            node = await doc_repo.get_category(category_id)
            if node is None:
                raise BizError(Err.DOC_CATEGORY_NOT_FOUND, f"分类不存在：{category_id}")
            if include_sub:
                subtree = await doc_repo.list_category_subtree(category_id)
                category_ids = [c["_id"] for c in subtree] or [category_id]
            else:
                category_ids = [category_id]

        deleted = view == "deleted"
        rows = await doc_repo.list_documents(
            deleted=deleted, category_ids=category_ids, status=status,
            permission_label=permission_label, keyword=keyword, sort_by=sort_by,
            skip=(page - 1) * page_size, limit=page_size)
        total = await doc_repo.count_documents(
            deleted=deleted, category_ids=category_ids, status=status,
            permission_label=permission_label, keyword=keyword)
        summary = await doc_repo.summary_counts(
            category_ids=category_ids, status=status,
            permission_label=permission_label, keyword=keyword)
        paths = await self._category_path_map()
        return {
            "items": [to_list_item(r, paths.get(r.get("category_id") or ""),
                                   deleted_view=deleted)
                      for r in rows],
            "total": total, "page": page, "page_size": page_size,
            "summary": summary,
        }

    async def _category_path_map(self) -> dict[str, str]:
        """分类 ID → 展示路径（一次取回，避免逐行查库）。"""
        return {r["_id"]: " / ".join(r.get("path") or [])
                for r in await doc_repo.list_categories()}

    async def get(self, doc_id: str) -> dict[str, Any] | None:
        """按编号取（内部契约：05/06/08/09 用它做只读校验）。"""
        return await doc_repo.get(doc_id)

    async def assert_exists(self, doc_id: str) -> dict[str, Any]:
        """取文档，不存在或已软删除则抛错（内部契约）。"""
        doc = await doc_repo.get(doc_id)
        if doc is None:
            raise BizError(Err.DOC_NOT_FOUND, f"知识单元不存在：{doc_id}")
        if doc.get("deleted_at") is not None:
            raise BizError(Err.DOC_SOFT_DELETED, f"知识单元已软删除：{doc_id}")
        return doc

    async def detail(self, doc_id: str, *, include_deleted: bool = False
                     ) -> dict[str, Any]:
        """详情。已软删除的默认拒绝（`DOC-3002`），带 `include_deleted` 才给。"""
        doc = await doc_repo.get(doc_id)
        if doc is None:
            raise BizError(Err.DOC_NOT_FOUND, f"知识单元不存在：{doc_id}")
        if doc.get("deleted_at") is not None and not include_deleted:
            raise BizError(Err.DOC_SOFT_DELETED, "知识单元已软删除（回收站可见）")
        paths = await self._category_path_map()
        item = to_list_item(doc, paths.get(doc.get("category_id") or ""),
                            deleted_view=doc.get("deleted_at") is not None)
        item.update({"tags": list(doc.get("tags") or []),
                     "permission_summary": doc.get("permission_summary") or {},
                     "import_error": doc.get("import_error"),
                     "deleted_by": doc.get("deleted_by")})
        return item

    # ------------------------------------------------------------------ 写
    async def update(self, *, doc_id: str, fields: Mapping[str, Any], actor_id: str,
                     request: Request | None = None) -> dict[str, Any]:
        """编辑标题 / 分类 / 标签（**不可写字段由路由层挡下**，这里再兜一次）。"""
        doc = await self.assert_exists(doc_id)
        if doc.get("import_status") in (s.value for s in _IN_PROGRESS):
            raise BizError(Err.DOC_BUSY_IMPORTING,
                           "知识单元正在导入中，暂不可编辑（04 正在回填，会覆盖丢失）")

        changed: dict[str, Any] = {}
        if "title" in fields and fields["title"] is not None:
            title = _clean_title(str(fields["title"]))
            if title != doc.get("title"):
                changed["title"] = title
        if "category_id" in fields:
            target = fields["category_id"]
            if target:
                node = await doc_repo.get_category(str(target))
                if node is None:
                    raise BizError(Err.DOC_CATEGORY_NOT_FOUND, f"分类不存在：{target}")
            if target != doc.get("category_id"):
                changed["category_id"] = target
        if "tags" in fields and fields["tags"] is not None:
            tags = _clean_tags(fields["tags"])
            if tags != list(doc.get("tags") or []):
                changed["tags"] = tags

        if not changed:
            return {"doc_id": doc_id, "changed": {}}
        changed.update({"updated_by": actor_id, "updated_at": _now_ms()})
        await doc_repo.update(doc_id, changed)
        await self._after_category_change(doc.get("category_id"), changed.get("category_id"))
        await _audit(request, "doc.update", "doc", doc_id, doc.get("title", ""),
                     actor=actor_id,
                     before={k: doc.get(k) for k in ("title", "category_id", "tags")},
                     after={k: v for k, v in changed.items()
                            if k in ("title", "category_id", "tags")},
                     reason="编辑知识单元")
        return {"doc_id": doc_id,
                "changed": {k: v for k, v in changed.items()
                            if k in ("title", "category_id", "tags")}}

    async def _after_category_change(self, old_category: str | None,
                                     new_category: str | None) -> None:
        """改分类后维护两边的 `doc_count`（增量，避免每次全量重算）。"""
        now = _now_ms()
        pairs: dict[str, int] = {}
        rows = await doc_repo.list_categories()
        by_id = {r["_id"]: r for r in rows}
        for category_id, delta in ((old_category, -1), (new_category, 1)):
            if not category_id:
                continue
            node = by_id.get(category_id)
            if node is None:
                continue
            for ancestor_id in (node.get("path_ids") or []):
                ancestor = by_id.get(ancestor_id)
                if ancestor is not None:
                    base = pairs.get(ancestor_id,
                                     int(ancestor.get("doc_count") or 0))
                    pairs[ancestor_id] = max(0, base + delta)
        await doc_repo.update_doc_counts(pairs, now)

    async def toggle(self, *, doc_id: str, enabled: bool, actor_id: str,
                     request: Request | None = None) -> dict[str, Any]:
        """启用 / 停用。

        **启用前必须确认导入已完成**（`DOC-3010`）：否则会出现"已启用但零切片"的
        假可用状态——列表上它是绿的，问答里却永远搜不到它。

        ⚠️ **03/04 连续批次**：04 落地后要在本方法里补一行
        `await ImportService.set_chunks_enabled(doc_id, enabled)`，并按其返回
        决定是否回滚；失败时转 `DOC-4003`（AC-03-10 要求"文档状态回滚为原值"）。
        现在系统里还没有任何切片，该调用是空操作，故先不写。
        """
        doc = await self.assert_exists(doc_id)
        target = DocStatus.ENABLED.value if enabled else DocStatus.DISABLED.value
        if doc.get("status") == target:
            return {"doc_id": doc_id, "status": target, "changed": False}
        if enabled and doc.get("import_status") != ImportStatus.DONE.value:
            raise BizError(Err.DOC_IMPORT_NOT_DONE,
                           f"导入未完成（{doc.get('import_status')}），暂不可启用")

        # ★ 03/04 连续批次约定的回填点（总纲 §4.2）：切片启停**只经 04**（ER-02/ER-12）
        await _sync_chunks(doc_id, enabled, action="启停")
        now = _now_ms()
        await doc_repo.update(doc_id, {"status": target, "updated_by": actor_id,
                                       "updated_at": now})
        await _audit(request, "doc.toggle", "doc", doc_id, doc.get("title", ""),
                     actor=actor_id, before={"status": doc.get("status")},
                     after={"status": target, "chunk_count": doc.get("chunk_count")},
                     reason="启用知识单元" if enabled else "停用知识单元")
        return {"doc_id": doc_id, "status": target, "changed": True}

    async def soft_delete(self, *, doc_id: str, actor_id: str,
                          request: Request | None = None) -> bool:
        """软删除（`deleted_at` + `status=disabled`）。

        **幂等**（AC-03-12）：重复删除不报错、不重复写审计——第二次调用时
        `deleted_at` 已是非空，直接返回 `False`（表示"这次没有改动"）。
        停用 `status` 是必要的：否则回收站里的文档在按 `status` 筛选时仍算"已启用"。
        """
        doc = await doc_repo.get(doc_id)
        if doc is None:
            raise BizError(Err.DOC_NOT_FOUND, f"知识单元不存在：{doc_id}")
        if doc.get("deleted_at") is not None:
            return False
        if doc.get("import_status") in (s.value for s in _IN_PROGRESS):
            raise BizError(Err.DOC_BUSY_IMPORTING, "正在导入中，暂不可删除")

        # 切片先停（ER-15）：先标删除再停切片会留下一个"已进回收站但仍可被召回"的窗口
        await _sync_chunks(doc_id, False, action="软删除")
        now = _now_ms()
        await doc_repo.soft_delete(doc_id, actor_id, now)
        await doc_repo.update(doc_id, {"status": DocStatus.DISABLED.value})
        await _shift_ancestor_counts_for_doc(doc, -1)
        await _audit(request, "doc.delete", "doc", doc_id, doc.get("title", ""),
                     actor=actor_id,
                     before={"status": doc.get("status"),
                             "deleted_at": None},
                     reason="软删除知识单元")
        return True

    async def restore(self, *, doc_id: str, actor_id: str, restore_status: str = "disabled",
                      request: Request | None = None) -> dict[str, Any]:
        """从回收站恢复。

        `restore_status=enabled` 时**要求导入已完成**（`DOC-3010`）——恢复一篇
        还没导入完的文档并直接启用，同样会产生"已启用但零切片"。
        """
        doc = await doc_repo.get(doc_id)
        if doc is None:
            raise BizError(Err.DOC_NOT_FOUND, f"知识单元不存在：{doc_id}")
        if doc.get("deleted_at") is None:
            raise BizError(Err.DOC_PARAM_INVALID, "该知识单元不在回收站里")
        if restore_status not in (DocStatus.ENABLED.value, DocStatus.DISABLED.value):
            raise BizError(Err.DOC_PARAM_INVALID, f"restore_status 非法：{restore_status}")
        if restore_status == DocStatus.ENABLED.value \
                and doc.get("import_status") != ImportStatus.DONE.value:
            raise BizError(Err.DOC_IMPORT_NOT_DONE, "导入未完成，恢复后只能保持停用")

        await _sync_chunks(doc_id, restore_status == DocStatus.ENABLED.value,
                           action="恢复")
        now = _now_ms()
        await doc_repo.restore(doc_id, actor_id, now)
        await doc_repo.update(doc_id, {"status": restore_status})
        await _shift_ancestor_counts_for_doc(doc, 1)
        await _audit(request, "doc.restore", "doc", doc_id, doc.get("title", ""),
                     actor=actor_id, before={"deleted_at": doc.get("deleted_at")},
                     after={"status": restore_status}, reason="从回收站恢复")
        return {"doc_id": doc_id, "status": restore_status}

    # ------------------------------------------------------------------ 去重
    async def resolve_by_hash(self, file_hash: str) -> dict[str, Any]:
        """单个哈希的去重决策（`ImportService` 与 `dedup-check` 共用）。

        五种决策（§3.7）：

        | decision | 条件 | 调用方该做什么 |
        |---|---|---|
        | `reuse` | 已存在且已导入完成、未软删除 | 直接复用 `doc_id`，不新增记录 |
        | `conflict` | 已存在且**正在导入** | 报 `DOC-3003`，**不投递第二个任务** |
        | `soft_deleted` | 已存在但已软删除 | 报 `DOC-3004`，提示先恢复 |
        | `retry` | 已存在且导入失败 | 复用 `doc_id` 重试，`doc_id` 不变 |
        | `new` | 不存在 | 建台账 |
        """
        _assert_hash(file_hash)
        doc = await doc_repo.find_by_hash(file_hash)
        if doc is None:
            return {"decision": "new", "doc_id": None, "title": None,
                    "name_conflict": False}
        state = doc.get("import_status")
        if doc.get("deleted_at") is not None:
            return {"decision": "soft_deleted", "doc_id": doc["_id"],
                    "title": doc.get("title"), "name_conflict": True}
        if state in (s.value for s in _IN_PROGRESS):
            return {"decision": "conflict", "doc_id": doc["_id"],
                    "title": doc.get("title"), "name_conflict": True}
        if state == ImportStatus.FAILED.value:
            return {"decision": "retry", "doc_id": doc["_id"],
                    "title": doc.get("title"), "name_conflict": True}
        return {"decision": "reuse", "doc_id": doc["_id"], "title": doc.get("title"),
                "name_conflict": True}

    async def dedup_check(self, items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """批量去重预检（AC-03-06/07/08）。

        上限 200 条（`DOC-1007`）：这个接口是 04 上传前的"预检"，前端一次拖 500 个文件
        会让请求体与响应都很大；超过就报错而不是截断——**截断会让第 201 个文件
        静默地被当成新文件上传**。
        """
        if not 1 <= len(items) <= DEDUP_ITEMS_MAX:
            raise BizError(Err.DOC_DEDUP_ITEMS_INVALID,
                           f"items 长度需在 1~{DEDUP_ITEMS_MAX}")
        hashes = []
        for item in items:
            _assert_hash(str(item.get("file_hash") or ""))
            hashes.append(str(item["file_hash"]))
        found = await doc_repo.find_by_hashes(hashes)
        results: list[dict[str, Any]] = []
        for item in items:
            file_hash = str(item["file_hash"])
            file_name = str(item.get("file_name") or "")
            doc = found.get(file_hash)
            if doc is None:
                results.append({"file_hash": file_hash, "file_name": file_name,
                                "decision": "new", "doc_id": None, "title": None,
                                "name_conflict": False})
                continue
            # **同名不同哈希 = new 且 name_conflict**（AC-03-07）：标题会撞，
            # 前端要提示"已有同名文件，是否仍要新建为独立知识单元"
            one = await self.resolve_by_hash(file_hash)
            one.update({"file_hash": file_hash, "file_name": file_name})
            results.append(one)
        return results

    # ------------------------------------------------------------------ 内部契约（04/05/08）
    async def create(self, *, file_name: str, file_ext: str, file_size: int,
                     file_hash: str, storage: Mapping[str, Any], created_by: str,
                     category_id: str | None = None,
                     title: str | None = None,
                     doc_id: str | None = None,
                     doc_no: str | None = None) -> str:
        """建台账（**04 的唯一建台账入口**）。

        落库即 `status=disabled` / `import_status=pending` / `chunk_count=0`：
        台账先存在、再导入，这样"上传失败"也有记录可查（而不是只留一个报错）。
        同哈希并发创建靠唯一索引兜底——冲突时**改判为复用**并返回已有 `doc_id`。

        `doc_id` / `doc_no` 可**预分配**：模块 04 的落盘顺序是"先落盘再建台账"
        （Spec §3.1 note），而对象键是 `{doc_id}/...`——落盘时就必须知道编号。
        若这里再分配一次，对象会写在 A 号前缀下、台账却记成 B 号，
        结果是**文档永远读不到自己的原文件**（预览/重试/重建全部失败）。
        """
        await doc_repo.assert_dedup_index_ready()
        _assert_hash(file_hash)
        _assert_file_ext(file_ext)
        if category_id:
            node = await doc_repo.get_category(category_id)
            if node is None:
                raise BizError(Err.DOC_CATEGORY_NOT_FOUND, f"分类不存在：{category_id}")

        now = _now_ms()
        if not doc_id or not doc_no:
            doc_id, doc_no = await doc_repo.next_doc_id(now)
        doc = {
            "_id": doc_id, "doc_no": doc_no,
            "title": _clean_title(title or _title_from_filename(file_name)),
            "file_name": file_name, "file_ext": file_ext, "file_size": int(file_size),
            "file_hash": file_hash, "category_id": category_id, "tags": [],
            "storage": dict(storage), "chunk_count": 0, "char_count": 0,
            "status": DocStatus.DISABLED.value,
            "import_status": ImportStatus.PENDING.value,
            "permission_summary": {"is_global": False, "dept_cnt": 0, "role_cnt": 0,
                                   "user_cnt": 0, "label": "unconfigured"},
            "created_by": created_by, "updated_by": created_by,
            "created_at": now, "updated_at": now,
            "deleted_at": None, "deleted_by": None,
        }
        try:
            await doc_repo.insert(doc)
        except DuplicateKeyError:
            existing = await doc_repo.find_by_hash(file_hash)
            if existing is None:                              # pragma: no cover — 极端竞态
                raise BizError(Err.DOC_STORE_FAILED, "同哈希并发创建冲突") from None
            logger.warning("同哈希并发创建 %s，改判为复用已有 %s", file_hash,
                           existing["_id"])
            return str(existing["_id"])
        await self._after_category_change(None, category_id)
        await _audit(None, "doc.create", "doc", doc_id, doc["title"], actor=created_by,
                     after={"file_name": file_name, "file_ext": file_ext,
                            "category_id": category_id})
        logger.info("台账已创建 %s(%s) 导入状态=pending", doc["title"], doc_id)
        return doc_id

    async def create_placeholder(self, *, title: str, source: str, created_by: str,
                                 category_id: str | None = None) -> str:
        """知识缺口一键转建（**08 的入口**）：无文件的待补充单元。

        `file_hash` 用 `placeholder:{doc_id}` 之外的稳定值——**不能用空串**：
        多处占位会互相撞唯一索引。这里用 `sha256("placeholder:" + title + source)`
        的十六进制，既满足"64 位十六进制"的格式约定，又让同一缺口重复转建时
        命中 `reuse` 而不是产生第二篇占位。
        """
        import hashlib

        seed = f"placeholder:{source}:{title}"
        file_hash = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        return await self.create(
            file_name=f"{_clean_title(title)}.pending", file_ext="txt", file_size=0,
            file_hash=file_hash, storage={"bucket": None, "object_key": None,
                                          "md_object_key": None},
            created_by=created_by, category_id=category_id, title=title)

    async def mark_import_done(self, doc_id: str, chunk_count: int,
                               char_count: int | None = None, *,
                               enable: bool = True) -> None:
        """导入完成回填（**04 的唯一入口**，ER-02）。

        做两件事，缺一不可：
        ① 回填 `chunk_count` / `char_count` / `import_status=done`；
        ② **把切片置为可检索**。

        ② 必须在这里做：`store_chunks()` 写入的切片默认 `enabled=false`
        （理由见 `chunk_store.build_rows`），若只改文档状态，结果就是
        **"文档已启用、切片却不可检索"**——界面上它是绿的，问答里永远搜不到它。
        这正是我在别处反复警告的"假可用"，差一点自己踩进去。

        `enable=False`（上传时选了"导入后不自动启用"，Spec §3.1 `auto_enable`）：
        台账写 `import_status=done` 但 `status` **保持 disabled**，切片也**保持
        `enabled=false`**。三个字段必须同时表达"导入完成了，但还没启用"——
        只改其中一个就会出现"状态说停用、切片却能召回"的越权形态。

        ⚠️ **调用顺序要求**：04 必须先 `store_chunks()` 再调本方法；
        反过来的话这里同步的是 0 条切片，仍然会留下"已启用但搜不到"。
        """
        await doc_repo.mark_import_done(doc_id, chunk_count, char_count, _now_ms(),
                                        enable=enable)
        await _sync_chunks(doc_id, enable, action="导入完成" if enable else "导入完成（未启用）")
        logger.info("导入完成回填 %s 切片数=%s auto_enable=%s", doc_id, chunk_count, enable)

    async def mark_import_failed(self, doc_id: str, stage: str, code: str,
                                 message: str) -> None:
        """导入失败回填（**04 的唯一入口**）：台账保留待重试。"""
        await doc_repo.mark_import_failed(doc_id, stage, code, message, _now_ms())
        logger.warning("导入失败回填 %s stage=%s code=%s", doc_id, stage, code)

    async def update_import_status(self, doc_id: str, import_status: str) -> None:
        """推进**过程态**（04 的 `parsing` / `embedding`，Spec §3.8）。

        允许值取自 `ImportStatus` 枚举（ER-10：E04 自己的枚举，不与 E06 任务状态混用）。
        非法值直接抛参数错——放进去会让前端拿到一个它不认识的字符串，
        表现是"状态列空白"，比报错更难查。
        """
        allowed = {s.value for s in ImportStatus}
        if import_status not in allowed:
            raise BizError(Err.DOC_PARAM_INVALID,
                           f"import_status 取值非法：{import_status}")
        await doc_repo.update_import_status(doc_id, import_status, _now_ms())

    async def update_storage(self, doc_id: str, storage: Mapping[str, Any]) -> None:
        """回填对象定位（04 落盘 / 重试后调用，ER-02：只有 03 写 `kb_documents`）。"""
        await doc_repo.update_storage(doc_id, storage, _now_ms())

    async def find_by_hash(self, file_hash: str) -> dict[str, Any] | None:
        """按哈希取台账（**只读**，04 查重与重试复用）。

        与 `resolve_by_hash()` 的区别：那个返回**决策**（reuse/conflict/...），
        这里返回**原始记录**——04 需要读 `storage` 里的对象键来决定"能不能跳过 upload 阶段"。
        """
        _assert_hash(file_hash)
        return await doc_repo.find_by_hash(file_hash)

    async def create_from_gap(self, *, gap_id: str, title: str,
                              category_id: str | None = None) -> str:
        """**08 的转建入口**：为知识缺口建一篇"占位知识单元"（ER-02）。

        占位态是三个字段的组合，缺一不可：`status=disabled`、
        `import_status=pending`、`file_hash` 为"占位哈希"（不是空串——
        空串会让多个占位互相撞 `file_hash` 唯一索引）。

        **幂等键是 `source_gap_id` 的稀疏唯一索引**（AC-08-11 / 跨模块契约）：
        同一个 `gap_id` 重复调用**返回已存在的 `doc_id`**，不新建。
        靠唯一索引而不是"先查再建"：并发两次点击时前者只有一条能成功，
        后者会两条都成功（查的时候都还没插入）。
        """
        import hashlib

        existing = await doc_repo.find_by_source_gap(gap_id)
        if existing is not None:
            logger.info("缺口 %s 已有占位文档 %s，直接复用", gap_id, existing["_id"])
            return str(existing["_id"])
        if category_id:
            node = await doc_repo.get_category(category_id)
            if node is None:
                raise BizError(Err.DOC_CATEGORY_NOT_FOUND, f"分类不存在：{category_id}")

        now = _now_ms()
        doc_id, doc_no = await doc_repo.next_doc_id(now)
        clean_title = _clean_title(title)
        doc = {
            "_id": doc_id, "doc_no": doc_no, "title": clean_title,
            "file_name": f"{clean_title}.pending", "file_ext": "txt", "file_size": 0,
            # 占位哈希：以 `gap:` 开头，与真实文件的 sha256 不可能撞
            "file_hash": hashlib.sha256(f"gap:{gap_id}".encode()).hexdigest(),
            "category_id": category_id, "tags": [],
            "storage": {"bucket": None, "object_key": None, "md_object_key": None},
            "chunk_count": 0, "char_count": 0,
            "status": DocStatus.DISABLED.value,
            "import_status": ImportStatus.PENDING.value,
            "permission_summary": {"is_global": False, "dept_cnt": 0, "role_cnt": 0,
                                   "user_cnt": 0, "version": 0, "label": "unconfigured"},
            "source_gap_id": gap_id,
            "created_by": "gap", "updated_by": "gap",
            "created_at": now, "updated_at": now,
            "deleted_at": None, "deleted_by": None,
        }
        try:
            await doc_repo.insert(doc)
        except DuplicateKeyError:
            # 并发转建：另一条路径刚插进去 → 复用它的
            found = await doc_repo.find_by_source_gap(gap_id)
            if found is None:                              # pragma: no cover
                raise BizError(Err.DOC_STORE_FAILED, "缺口占位并发创建冲突") from None
            return str(found["_id"])
        await _audit(None, "doc.create", "doc", doc_id, clean_title, actor="gap",
                     after={"source_gap_id": gap_id, "category_id": category_id})
        logger.info("缺口 %s 的占位文档已创建 %s（等待上传原文件）", gap_id, doc_id)
        return doc_id

    async def rollback_gap_placeholder(self, doc_id: str) -> bool:
        """**补偿**：撤销缺口转建留下的占位文档（Spec §3.3 R-09）。

        **只在"仍是占位态"时才允许撤销**：已经有切片、或已被上传过原文件的文档
        说明它已经是一条真实知识，删掉它就不是"补偿失败"而是"丢数据"了。
        这种情况返回 `False`，交给人工处理（Spec 明确要求"转人工"）。
        """
        doc = await doc_repo.get(doc_id)
        if doc is None:
            return True                                     # 已被删掉视作撤销成功
        if int(doc.get("chunk_count") or 0) > 0 or doc.get("storage", {}).get("object_key"):
            logger.error("占位文档 %s 已有内容（chunks=%s），拒绝撤销，需人工处理",
                         doc_id, doc.get("chunk_count"))
            return False
        await doc_repo.hard_delete(doc_id)
        logger.warning("占位文档 %s 已撤销（缺口转建失败的补偿）", doc_id)
        return True

    async def update_permission_summary(self, doc_id: str, summary: Mapping[str, Any],
                                        actor_id: str = "system") -> None:
        """05 写完 E07 后回填展示摘要（**05 不写 `kb_documents`**，ER-02）。

        `label` 由**本模块**计算后落库：这样列表筛选（索引 4 用 `label`）不必
        在查询时逐行推导。丢失这次调用只让标签陈旧，**不影响鉴权**（AC-03-19）。

        `version` 原样带上（模块 05 §3.2 的口径）：03 据此判断摘要是否过期。
        没有它就只能靠 `updated_at` 猜，而"同一毫秒内改两次"时时间戳不可靠。
        """
        payload = {
            "is_global": bool(summary.get("is_global")),
            "dept_cnt": int(summary.get("dept_cnt") or 0),
            "role_cnt": int(summary.get("role_cnt") or 0),
            "user_cnt": int(summary.get("user_cnt") or 0),
            "version": int(summary.get("version") or 0),
        }
        payload["label"] = ("global" if payload["is_global"] else
                            "limited" if (payload["dept_cnt"] or payload["role_cnt"]
                                          or payload["user_cnt"]) else "unconfigured")
        await doc_repo.update_permission_summary(doc_id, payload, _now_ms())
        logger.info("权限摘要已回填 %s label=%s version=%d", doc_id, payload["label"],
                    payload["version"])

    async def refresh_permission_summary(self, doc_id: str,
                                         summary: Mapping[str, Any]) -> None:
        """模块 05 Spec §1.2 / §3.2 规则 9 用的名字。

        与本类的 `update_permission_summary` 是**同一个行为**，两个名字并存是因为
        两份 Spec 各写了一个：03 的 §3.8 叫 `update_permission_summary`，
        05 的 §3.2 叫 `refresh_permission_summary`。与其改一份 Spec 让另一份失准，
        不如在这里做一层薄别名——**调用方按各自 Spec 写代码都能跑**，
        而实现只有一处（不存在"两个方法行为漂移"的风险）。
        """
        await self.update_permission_summary(doc_id, summary)

    async def category_path_of(self, doc_id: str) -> str:
        """06 的溯源卡片要用 `title` + `category_path`（一次查两份集合）。"""
        doc = await doc_repo.get(doc_id)
        if doc is None:
            return ""
        node = await doc_repo.get_category(doc.get("category_id") or "")
        return " / ".join(node.get("path") or []) if node else ""


def _clean_title(title: str) -> str:
    """标题：1~200 字，去空白后不能为空（`DOC-1002`）。"""
    clean = (title or "").strip()
    if not clean or len(clean) > TITLE_MAX:
        raise BizError(Err.DOC_TITLE_INVALID, f"标题需 1~{TITLE_MAX} 字")
    return clean


def _assert_file_ext(file_ext: str) -> None:
    """文件格式白名单（`DOC-1005`）。

    04 已校验过一次，这里是**二次防御**：防绕过上传接口直接调 `create()` 建台账。
    """
    if file_ext not in ("pdf", "md", "docx", "txt"):
        raise BizError(Err.DOC_FILE_EXT_UNSUPPORTED,
                       f"不支持的文件格式：{file_ext}（只允许 pdf/md/docx/txt）")


def _assert_hash(file_hash: str) -> None:
    """SHA256 必须是 64 位十六进制（`DOC-1001`）。"""
    import re

    if not re.fullmatch(r"[0-9a-f]{64}", file_hash or ""):
        raise BizError(Err.DOC_PARAM_INVALID, "file_hash 必须是 64 位十六进制 SHA256")


def _clean_tags(tags: Any) -> list[str]:
    """标签：≤10 个、每个 1~20 字（`DOC-1006`）。

    **去重并保序**：同一标签写两遍没有意义，而"顺序"是用户自己排的。
    """
    if not isinstance(tags, (list, tuple)):
        raise BizError(Err.DOC_TAGS_INVALID, "tags 必须是数组")
    seen: list[str] = []
    for raw in tags:
        text = str(raw).strip()
        if not text or len(text) > TAG_LEN_MAX:
            raise BizError(Err.DOC_TAGS_INVALID,
                           f"单个标签需 1~{TAG_LEN_MAX} 字：{raw!r}")
        if text not in seen:
            seen.append(text)
    if len(seen) > TAGS_MAX:
        raise BizError(Err.DOC_TAGS_INVALID, f"标签最多 {TAGS_MAX} 个")
    return seen


def _title_from_filename(file_name: str) -> str:
    """默认标题 = 文件名去扩展名（§2.1）；空文件名退化为"未命名"。"""
    name = (file_name or "").strip()
    if not name:
        return "未命名"
    return name.rsplit(".", 1)[0] if "." in name else name


async def _shift_ancestor_counts_for_doc(doc: Mapping[str, Any], delta: int) -> None:
    """文档进/出回收站时，沿分类 `path_ids` 调整 `doc_count`。"""
    category_id = doc.get("category_id")
    if not category_id:
        return
    node = await doc_repo.get_category(category_id)
    if node is None:
        return
    await _shift_ancestor_counts(node, delta)


async def _sync_chunks(doc_id: str, enabled: bool, *, action: str) -> int:
    """把切片 `enabled` 同步给 04，**失败即整体不生效**（`DOC-4003`）。

    **顺序是刻意的：先同步切片，再改文档状态。** 反过来（先改文档、失败再回滚）
    会留下一个真实窗口——那段时间里文档显示"已停用"、切片却仍能被召回，
    也就是用户以为停用了、内容其实还在答案里。先改切片则失败时文档**从未被改过**，
    "回滚"是天然的而不是补救的。

    这一处就是总纲 §4.2 约定的"前向调用点"：03 **不连 Milvus**（AC-03-11），
    切片 `enabled` 的唯一写入口是 04 的 `set_chunks_enabled()`。
    """
    try:
        affected = await chunk_store.set_chunks_enabled(doc_id, enabled)
    except ChunkStoreError as exc:
        logger.error("切片同步失败，%s操作整体不生效 doc_id=%s：%s", action, doc_id, exc)
        raise BizError(Err.DOC_CHUNK_SYNC_FAILED,
                       f"{action}失败：切片启停未同步（文档状态未改动）") from exc
    logger.info("%s：文档 %s 的 %d 条切片已置 enabled=%s", action, doc_id, affected,
                enabled)
    return affected


async def _audit(request: Request | None, action: str, target_type: str, target_id: str,
                 target_name: str, *, before: Mapping[str, Any] | None = None,
                 after: Mapping[str, Any] | None = None, reason: str | None = None,
                 actor: str | None = None) -> None:
    """统一的审计出口（ER-05）。

    不 `try/except`、不看返回值：`record()` 保证永不抛，审计失败只记 ERROR，
    **绝不把业务响应变成失败**（AC-03-21 的后半句）。
    """
    payload = {"target_type": target_type, "target_id": target_id,
               "target_name": target_name, "before": before, "after": after,
               "reason": reason}
    if request is not None:
        await audit_service.record_from_request(request, action, **payload)
    else:
        await audit_service.record(action, actor=actor or "system", **payload)


doc_service = DocService()
category_service = CategoryService()

__all__ = [
    "DocService", "CategoryService", "doc_service", "category_service",
    "permission_label_text", "status_text", "to_list_item", "MAX_LEVEL",
    "TITLE_MAX", "CATEGORY_NAME_MAX", "TAGS_MAX", "DEDUP_ITEMS_MAX", "PAGE_SIZE_MAX",
]
