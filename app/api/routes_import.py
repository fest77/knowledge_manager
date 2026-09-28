# -*- coding: utf-8 -*-
"""模块 04 的接口层（`/api/v1/import/*`）。

**路由声明顺序是硬要求**：`/import/tasks`（列表）必须声明在 `/import/tasks/{task_id}`
之前吗？——**不必**，因为 `tasks` 与 `{task_id}` 是不同深度。但
`/import/tasks/{task_id}` 与 `/import/tasks/{task_id}/cancel` 也不冲突（深度不同）。
本模块真正要注意的是 **`/import/files/{object_key:path}` 放在最后**：
`{object_key:path}` 会吞掉它之后的所有路径段，声明在前面会把 `/import/docs/...` 也吃掉。

**审计写入点**（ER-05，全部经 `AuditService`）：
`doc.import.start` / `doc.import.done` / `doc.import.fail` / `doc.import.cancel`
——四个名字**严格取自 Spec §1.4**，不自行扩展（ER-16）。重试记 `doc.import.start`
（它确实重新开始了一次导入），靠 `detail.retry_of` 与原任务区分。

## 关于 `GET /import/files/{object_key:path}`

它**不在 §3.9 的 7 行接口一览里**，但 §2.2 的「越界保护」与 AC-04-24 明确要求
"任何读写对象前校验对象键属于 `{doc_id}/` 前缀，否则拒绝（`IMP-2002`）"——
那就必须有一个**读对象的入口**，否则这条规则没有落点、也没法构造越界请求去验收。
所以本实现补了这一条，并把它写成最小的形态：只读、只按 `{doc_id}/` 前缀鉴权、
复用 `doc:read`（**不新增权限码**，ER-09）。
"""
from __future__ import annotations

from fastapi import (APIRouter, Depends, File, Form, Query, Request, UploadFile)
from fastapi.responses import Response

from app.api.deps import current_user, ensure_permission
from app.api.schemas_import import (BatchResponse, CancelRequest, CancelResponse,
                                    ChunkPreviewResponse, RetryRequest, RetryResponse,
                                    TaskDetailResponse, TaskPageResponse, UploadResponse)
from app.core.errors import Err
from app.core.permissions import require_perm
from app.core.response import ok
from app.services.auth_service import UserContext
from app.services.import_service import import_service

router = APIRouter(prefix="/api/v1/import", tags=["04 文档导入与向量化"])


# --------------------------------------------------------------------------- 上传
@router.post("/upload", summary="单文件上传（含 file_hash 查重复用）",
             response_model=None)
@require_perm("doc:upload")
async def upload(
    request: Request,
    file: UploadFile = File(..., description="单文件，≤ import.max_file_mb（默认 100MB）"),
    category_id: str | None = Form(None, description="目标分类；不传则未分类"),
    title: str | None = Form(None, description="显示标题；默认取文件名（去扩展名）"),
    auto_enable: bool = Form(True, description="导入完成后是否自动启用"),
    user: UserContext = Depends(current_user),
):
    """**请求内完成 `upload` 阶段**（落盘 + 建台账 + 建任务），其余六阶段中的五个丢后台。

    为什么这里不做 `try/except` 把异常转成 400：校验类的失败由服务层抛 `BizError`，
    统一异常处理器会按 `ErrorSpec` 的 HTTP 码转成规范信封；在这里再包一层
    会丢掉 `trace_id` 与原始错误码，而那两样正是排查入口。
    """
    ensure_permission(user, "doc:upload", Err.AUTH_PERM_DENIED)
    item = await import_service.upload_one(
        upload=file, actor_id=user.user_id, category_id=category_id, title=title,
        auto_enable=auto_enable)
    return ok(UploadResponse(
        doc_id=item.doc_id, task_id=item.task_id, reused=item.reused,
        file_hash=item.file_hash, file_ext=item.file_ext, file_size=item.file_size,
        stage=item.stage, progress=item.progress,
        duplicated_of=item.duplicated_of).model_dump())


@router.post("/batch", summary="批量 / 文件夹上传（部分成功返回 200）",
             response_model=None)
@require_perm("doc:upload")
async def upload_batch(
    request: Request,
    files: list[UploadFile] = File(..., description="1~import.max_batch_files 个文件"),
    relative_paths: list[str] | None = Form(
        None, description="文件夹拖拽时的相对路径，必须与 files 一一对应"),
    category_id: str | None = Form(None),
    auto_map_category: bool = Form(
        False, description="按 relative_paths 目录层级自动配分类（G-01，默认关闭）"),
    auto_enable: bool = Form(True),
    user: UserContext = Depends(current_user),
):
    """逐文件独立校验：**部分成功返回 200**，失败项进 `rejected[]` 并各带自己的码。

    前端据此在导入队列里逐行标红，而不是整批报一个"上传失败"——
    拖 50 个文件时后者等于什么都没说。
    """
    ensure_permission(user, "doc:upload", Err.AUTH_PERM_DENIED)
    data = await import_service.upload_batch(
        uploads=files, actor_id=user.user_id, category_id=category_id,
        relative_paths=relative_paths, auto_map_category=auto_map_category,
        auto_enable=auto_enable)
    return ok(BatchResponse(**data).model_dump())


# --------------------------------------------------------------------------- 队列
@router.get("/tasks", summary="导入队列（文件 / 阶段 / 进度）", response_model=None)
@require_perm("doc:read")
async def list_tasks(
    user: UserContext = Depends(current_user),
    doc_id: str | None = Query(None, description="按知识单元筛选"),
    status: str | None = Query(None, description="pending/running/succeeded/failed/"
                                                 "timeout/cancelled"),
    stage: str | None = Query(None, description="六阶段之一"),
    batch_id: str | None = Query(None, description="按批次筛选"),
    page: int = Query(1, description="页码，从 1 起；≤0 报 IMP-1007"),
    page_size: int = Query(20, le=1000,
                           description="上限 200，超出报 IMP-1007"),
):
    """⚠️ **这里刻意不写 `ge=1`**：Spec §3.4 R-01 要求 `page` / `page_size` 非正整数时
    报 `IMP-1007`，而 `ge=1` 会让 FastAPI 在校验层就拒掉、返回 `SYS-1001`。
    两个码指向两处实现，前端按 Spec 映射文案就会落空。
    所以"范围约束"整体下移到服务层（`_assert_page`），路由只声明 `le=1000`
    给服务层的 200 上限留出判断余地。"""
    ensure_permission(user, "doc:read", Err.AUTH_PERM_DENIED)
    data = await import_service.list_queue(
        doc_id=doc_id, status=status, stage=stage, batch_id=batch_id,
        page=page, page_size=page_size)
    return ok(TaskPageResponse(**data).model_dump())


@router.get("/tasks/{task_id}", summary="任务进度（阶段 + 百分比 + 各阶段耗时）",
            response_model=None)
@require_perm("doc:read")
async def get_task(task_id: str, user: UserContext = Depends(current_user)):
    """轮询友好：单集合按主键查（Spec §3.3 R-03），**不加限流**，前端建议 1s 间隔。"""
    ensure_permission(user, "doc:read", Err.AUTH_PERM_DENIED)
    data = await import_service.get_task_detail(task_id)
    return ok(TaskDetailResponse(**data).model_dump())


# --------------------------------------------------------------------------- 取消 / 重试
@router.post("/tasks/{task_id}/cancel", summary="取消导入（协作式，阶段边界生效）",
             response_model=None)
@require_perm("doc:upload")
async def cancel_task(task_id: str, body: CancelRequest | None = None,
                      user: UserContext = Depends(current_user)):
    """`cancel_pending=true` 是诚实的回答：请求已受理，但工作线程可能正卡在
    MinerU / BGE 推理里（`to_thread` 无法安全强杀），所以**不承诺立即生效**。"""
    ensure_permission(user, "doc:upload", Err.AUTH_PERM_DENIED)
    can_manage_others = "doc:delete" in user.permissions
    data = await import_service.cancel(
        task_id, actor_id=user.user_id,
        reason=(body.reason if body else ""), can_manage_others=can_manage_others)
    return ok(CancelResponse(**data).model_dump())


@router.post("/tasks/{task_id}/retry", summary="重试导入（新建任务，保留失败现场）",
             response_model=None)
@require_perm("doc:upload")
async def retry_task(task_id: str, body: RetryRequest | None = None,
                     user: UserContext = Depends(current_user)):
    """重试 = **新任务** `retry_of` 指向原任务：失败现场（`error` / `durations` /
    `degraded[]`）留在原任务上，用于回答"上次到底为什么失败"。"""
    ensure_permission(user, "doc:upload", Err.AUTH_PERM_DENIED)
    data = await import_service.retry(
        task_id, actor_id=user.user_id, from_stage=(body.from_stage if body else None))
    return ok(RetryResponse(**data).model_dump())


# --------------------------------------------------------------------------- 切片预览
@router.get("/docs/{doc_id}/chunks", summary="切片预览（核对切分质量）",
            response_model=None)
@require_perm("doc:chunk")
async def chunk_preview(
    doc_id: str,
    user: UserContext = Depends(current_user),
    page: int = Query(1, description="页码，从 1 起；≤0 报 IMP-1007"),
    page_size: int = Query(20, le=1000, description="上限 200，超出报 IMP-1007"),
    keyword: str = Query("", max_length=100, description="在 title / content 上包含匹配"),
    full: bool = Query(False, description="false 只给 200 字预览；true 给全文（≤4000 字）"),
):
    """知识管理员核对"切太长 / 切太碎 / 标题错"，也用于答辩演示切片可查看。"""
    ensure_permission(user, "doc:chunk", Err.AUTH_PERM_DENIED)
    data = await import_service.chunk_preview(doc_id, page=page, page_size=page_size,
                                              keyword=keyword, full=full)
    return ok(ChunkPreviewResponse(**data).model_dump())


# --------------------------------------------------------------------------- 对象读取
@router.get("/files/{object_key:path}", summary="读取导入产物（原文件 / MD / 图片）",
            response_model=None)
@require_perm("doc:read")
async def read_file(object_key: str, user: UserContext = Depends(current_user)):
    """按对象键读导入产物，**鉴权 = 前缀归属校验**（Spec §2.2 / AC-04-24）。

    ⚠️ **必须声明在本文件最后**：`{object_key:path}` 会贪婪匹配后续所有路径段，
    若它排在 `/docs/...` 前面，`/import/docs/DOC.../chunks` 会被当成一个对象键。

    越界（对象键不以 `{doc_id}/` 打头）返回 `IMP-2002`：
    这条规则防的是**跨文档越权取文件**——不校验的话，任何人把 doc_id 换成别人的
    就能读到别部门的原文。
    """
    ensure_permission(user, "doc:read", Err.AUTH_PERM_DENIED)
    data, content_type = await import_service.read_object(object_key)
    return Response(content=data, media_type=content_type)


__all__ = ["router"]
