# -*- coding: utf-8 -*-
"""模块 04 端到端功能测试：**上传 → 六阶段流水线 → 切片可查**。

## 这个文件测的是"能不能真的跑通"，不是"每个分支都对"

按用户的要求（"测试只要功能正常即可"），这里聚焦**主干可运行性**：

1. 上传 → 后台流水线真的跑完 → 台账 `import_status=done` 且**切片确实写进了 Milvus**
   （这条最关键：`chunk_count>0` 与 Milvus 里真的查得到，是两件事——
   前者是台账回填，后者才证明写入成功）
2. 同哈希复用（不重复解析/向量化）
3. 格式与文件名校验（`IMP-1003` / `IMP-1005`）
4. 批量部分成功
5. 协作式取消的状态机约束（终态不可取消）
6. 对象键越界保护（`IMP-2002`）

## 两个必须的替身

| 替身 | 为什么 |
|---|---|
| **`embedding_service.embed`** | 真模型 2.2GB、加载十几秒；单测要的是"向量正确流经链路"。替身返回**单位化**向量：COSINE 下零向量会退化 |
| **MinIO 调用** | 打桩成"不可用"，让 `_persist` / `_put_bytes` 走**本地降级**路径——顺便测了这个分支，也不给共享桶留垃圾 |
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.enums import ImportTaskStage, ImportTaskStatus
from app.infra.milvus import milvus
from app.services import chunk_store
from app.services.embedding_service import EXPECTED_DIM, embedding_service
from app.services.import_service import LOCAL_STORE_DIR, import_service
from tests.conftest import token_of

KB_ADMIN = "zhangwei"
ASKER = "wangqiang"

# 异步测试统一用 anyio 插件（项目不用 pytest-asyncio，见 conftest 说明）
pytestmark = pytest.mark.anyio

# 一份"像制度文件"的 Markdown：带两级标题，能被标题层级切分成多片
SAMPLE_MD = """# 差旅费报销管理办法

## 第一章 总则

第一条 为规范公司差旅费报销流程，明确审批权限，特制定本办法。
本办法适用于公司全体员工的国内出差活动，境外出差另行规定。

## 第二章 报销标准

第二条 住宿费标准：一线城市每晚不超过 500 元，二线城市不超过 350 元。
超出部分由个人承担，特殊情况需经分管副总审批。

第三条 交通费标准：市内交通据实报销，单次超过 200 元需附说明。
高铁二等座据实报销，商务座需提前审批。

## 第三章 报销流程

第四条 员工应在出差结束后 15 个工作日内提交报销单。
报销单需附发票原件、行程单与审批邮件截图。

第五条 财务部应在收到完整材料后 5 个工作日内完成审核并付款。
""".encode("utf-8")


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def fake_embed(monkeypatch):
    """替身：确定性 1024 维**单位化**向量（不加载真模型）。

    用"按文本长度决定第一个分量"的方式让不同文本的向量不同——
    全部相同的话，检索排序会退化成任意顺序，将来接 06 的检索测试就会出现
    "明明两条都命中，却总是返回同一条"的假故障。
    """
    calls: list[list[str]] = []

    def _embed(texts, *, batch_size: int = 16):
        calls.append(list(texts))
        out = []
        for text in texts:
            vector = [0.0] * EXPECTED_DIM
            vector[0] = 1.0
            vector[1] = float(len(text) % 97) / 97.0
            vector[2] = 0.5
            norm = sum(v * v for v in vector) ** 0.5
            out.append([v / norm for v in vector])
        return out

    monkeypatch.setattr(embedding_service, "embed", _embed)
    return calls


class _MinioDown:
    """把 MinIO 打桩成不可用，逼出本地降级路径（见模块头说明）。"""

    @staticmethod
    def install(monkeypatch) -> None:
        from app.infra.minio import minio_store

        def _boom(*_args, **_kwargs):
            raise RuntimeError("测试：MinIO 故意不可用")

        for name in ("put_file", "put_bytes", "get_bytes", "remove"):
            monkeypatch.setattr(minio_store, name, _boom)


@pytest.fixture
async def infra(monkeypatch):
    """连 Milvus + 打桩 MinIO；用完清理该用例产生的切片与本地落盘文件。

    ⚠️ **拆卸顺序是硬要求**：这个 fixture 必须排在 `client` **之后**声明
    （依赖顺序 = 声明顺序，拆卸是反序），这样它的清理先于 `client` 的
    `mongo.close()`。理由：流水线是**后台任务**，测试结束时可能还在跑；
    先断 Mongo 再去取消它，`_finalize_failure` 回填任务状态就会撞上
    "Cannot use AsyncMongoClient after close"——而这个错会被 pytest 记到
    **下一个用例**头上，排查方向完全跑偏（我就在这里踩过一次）。
    """
    _MinioDown.install(monkeypatch)
    await milvus.connect()
    await milvus.ensure_chunks_collection()
    created: list[str] = []
    yield created
    # ① 先取消并等干净所有在跑的导入任务（它们可能正在写 Mongo / Milvus）
    await import_service.shutdown()
    # ② 再清切片与本地产物
    for doc_id in created:
        try:
            await chunk_store.drop_chunks_of(doc_id)
        except Exception:                                   # noqa: BLE001
            pass
        folder = LOCAL_STORE_DIR / doc_id
        if folder.is_dir():
            for item in folder.rglob("*"):
                if item.is_file():
                    item.unlink(missing_ok=True)
    # ③ 并发闸门绑定在**已经结束的那个事件循环**上，必须重置，
    #    否则下一个用例一 await 就报 "attached to a different loop"
    import_service._sem = None
    await milvus.close()


async def _upload(client, token: str, name: str = "差旅费报销管理办法.md",
                  content: bytes = SAMPLE_MD, **form):
    data = {"auto_enable": "true"}
    data.update({k: str(v) for k, v in form.items()})
    return await client.post("/api/v1/import/upload", headers=auth(token),
                             files={"file": (name, content, "text/markdown")},
                             data=data)


async def _wait_terminal(client, token: str, task_id: str, *, timeout: float = 60.0):
    """轮询任务直到终态（前端建议 1s 间隔；测试用 0.2s 加快）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    last = {}
    while asyncio.get_running_loop().time() < deadline:
        resp = await client.get(f"/api/v1/import/tasks/{task_id}",
                                headers=auth(token))
        assert resp.status_code == 200, resp.text
        last = resp.json()["data"]
        if last["status"] in (ImportTaskStatus.SUCCEEDED.value,
                              ImportTaskStatus.FAILED.value,
                              ImportTaskStatus.TIMEOUT.value,
                              ImportTaskStatus.CANCELLED.value):
            return last
        await asyncio.sleep(0.2)
    raise AssertionError(f"任务未在 {timeout}s 内进入终态，最后状态={last}")


# ================================================================ 主干：全链路
async def test_upload_runs_six_stages_and_chunks_land_in_milvus(
        client, fake_embed, infra):
    """上传 → 六阶段 → 切片进 Milvus → 台账回填 → 切片预览可读。"""
    token = await token_of(client, KB_ADMIN)
    resp = await _upload(client, token)
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["reused"] is False
    assert body["task_id"], "新文件必须产生任务号，前端要靠它轮询"
    assert body["stage"] == ImportTaskStage.PDF_TO_MD.value
    doc_id, task_id = body["doc_id"], body["task_id"]
    infra.append(doc_id)

    task = await _wait_terminal(client, token, task_id)
    assert task["status"] == ImportTaskStatus.SUCCEEDED.value, task.get("error")
    assert task["progress"] == 100
    # AC-04-05：done_stages 最终含全部 6 项，且阶段顺序与 Spec 一致
    assert task["done_stages"] == [s.value for s in ImportTaskStage]
    assert task["stage_total"] == 6
    assert task["stage"] == ImportTaskStage.MILVUS.value
    # 每个阶段都应有耗时记录（最后一项靠 `flush_stage()` 补写）
    assert set(task["durations"]) == {s.value for s in ImportTaskStage}
    assert task["error"] is None
    assert fake_embed, "向量化必须被真的调用过"

    # ① 台账回填
    doc = (await client.get(f"/api/v1/docs/{doc_id}", headers=auth(token))).json()["data"]
    assert doc["import_status"] == "done"
    assert doc["status"] == "enabled", "auto_enable=true 时导入完成即启用"
    assert doc["chunk_count"] > 1, f"应切出多片，实际 {doc['chunk_count']}"

    # ② **切片真的在 Milvus 里**（台账数字与向量库事实必须一致）
    real = await chunk_store.count_chunks(doc_id)
    assert real == doc["chunk_count"], "台账 chunk_count 必须等于 Milvus 中的真实条数"
    enabled = await chunk_store.count_chunks(doc_id, enabled=True)
    assert enabled == real, "导入完成后所有切片都应可检索"

    # ③ 切片预览：锚点字段齐备（title/parent_title/part 是 §2.6 的显式字段）
    preview = (await client.get(f"/api/v1/import/docs/{doc_id}/chunks",
                                headers=auth(token))).json()["data"]
    assert preview["total"] == real
    assert preview["items"], "切片预览不能为空"
    first = preview["items"][0]
    for field in ("chunk_index", "title", "parent_title", "file_title", "part",
                  "enabled", "char_count", "content"):
        assert field in first, f"切片预览缺字段 {field}"
    assert first["file_title"] == "差旅费报销管理办法"
    assert any(i["title"] for i in preview["items"]), "应至少有一片带标题"
    # AC-04-12：`content == f"{title}\n\n{body}"`，且 `chunk_index` 从 0 连续递增
    assert [i["chunk_index"] for i in preview["items"]] == \
        list(range(len(preview["items"]))), "chunk_index 必须从 0 连续递增"
    for item in preview["items"]:
        assert item["content"].startswith(item["title"] + "\n\n"), \
            "content 必须以 `标题 + 空行` 开头（§2.6 的装配规则）"
    # 默认只给 200 字预览
    assert all(len(i["content"]) <= 200 for i in preview["items"])


async def test_same_hash_is_reused_without_second_task(client, fake_embed, infra):
    """R-09：同哈希且已导完 → 复用，**不建任务、不再解析**。"""
    token = await token_of(client, KB_ADMIN)
    first = (await _upload(client, token)).json()["data"]
    infra.append(first["doc_id"])
    await _wait_terminal(client, token, first["task_id"])

    again = await _upload(client, token, name="另一个名字.md")
    assert again.status_code == 200, again.text
    data = again.json()["data"]
    assert data["reused"] is True
    assert data["task_id"] is None, "复用不产生任务，前端不该去轮询"
    assert data["doc_id"] == first["doc_id"], "复用必须指向同一个知识单元"
    assert data["duplicated_of"], "应回传已存在文件的名字，供前端提示"


# ================================================================ 校验规则
async def test_unsupported_format_rejected_without_side_effects(client, fake_embed,
                                                                infra):
    """AC-04-02：`.pptx` → `IMP-1003`，且**不产生台账、不产生任务**。"""
    token = await token_of(client, KB_ADMIN)
    resp = await _upload(client, token, name="制度.pptx", content=b"PK\x03\x04junk")
    assert resp.status_code == 400
    assert resp.json()["code"] == "IMP-1003"
    assert (await client.get("/api/v1/docs", headers=auth(token))
            ).json()["data"]["total"] == 0, "被拒的文件不该留下台账"
    assert (await client.get("/api/v1/import/tasks", headers=auth(token))
            ).json()["data"]["total"] == 0, "被拒的文件不该留下任务"


async def test_upload_requires_doc_upload_permission(client, fake_embed, infra):
    """AC-04-22：无 `doc:upload` 的账号上传被 01 的全局依赖拒绝（403 / `AUTH-2004`）。

    这里刻意**不**由 04 自己产出"权限不足"：功能权限只有一份实现（ER-09 / ER-03），
    04 只做服务层兜底（防绕过 FastAPI 直调），用的是 01 的码。
    """
    asker = await token_of(client, ASKER)
    resp = await _upload(client, asker, name="偷偷传.md")
    assert resp.status_code == 403
    assert resp.json()["code"] == "AUTH-2004"
    assert (await client.get("/api/v1/docs", headers=auth(asker))
            ).status_code == 403, "asker 连台账列表都不该看到"


async def test_path_traversal_filename_rejected(client, fake_embed, infra):
    """AC-04-24：文件名含 `../` → `IMP-1005`。"""
    token = await token_of(client, KB_ADMIN)
    resp = await _upload(client, token, name="../../etc/passwd.md")
    assert resp.status_code == 400
    assert resp.json()["code"] == "IMP-1005"


async def test_empty_file_rejected(client, fake_embed, infra):
    """R-01：0 字节 → `IMP-1001`。"""
    token = await token_of(client, KB_ADMIN)
    resp = await _upload(client, token, name="空.md", content=b"")
    assert resp.status_code == 400
    assert resp.json()["code"] == "IMP-1001"


async def test_content_mismatch_rejected(client, fake_embed, infra):
    """R-05：`.pdf` 但没有 `%PDF-` 魔数 → `IMP-1006`。"""
    token = await token_of(client, KB_ADMIN)
    resp = await _upload(client, token, name="伪装.pdf", content=b"not a pdf at all")
    assert resp.status_code == 400
    assert resp.json()["code"] == "IMP-1006"


async def test_batch_partial_success(client, fake_embed, infra):
    """§3.2 R-05：一个坏文件不该让整批失败——**部分成功仍返回 200**。"""
    token = await token_of(client, KB_ADMIN)
    resp = await client.post(
        "/api/v1/import/batch", headers=auth(token),
        files=[("files", ("好的.md", SAMPLE_MD, "text/markdown")),
               ("files", ("坏的.pptx", b"PK\x03\x04x", "application/octet-stream"))],
        data={"auto_enable": "true"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["total"] == 2
    assert data["accepted"] == 1
    assert len(data["rejected"]) == 1
    assert data["rejected"][0]["code"] == "IMP-1003"
    assert data["rejected"][0]["file_name"] == "坏的.pptx"
    assert data["items"][0]["task_id"], "受理项必须有任务号"
    infra.append(data["items"][0]["doc_id"])


async def test_batch_all_rejected_returns_error(client, fake_embed, infra):
    """§3.2：**仅当全部被拒才报错**，`code` 取第一条失败项的码。"""
    token = await token_of(client, KB_ADMIN)
    resp = await client.post(
        "/api/v1/import/batch", headers=auth(token),
        files=[("files", ("a.pptx", b"PK\x03\x04x", "application/octet-stream")),
               ("files", ("b.xlsx", b"PK\x03\x04y", "application/octet-stream"))],
        data={})
    assert resp.status_code == 400
    assert resp.json()["code"] == "IMP-1003"


# ================================================================ 队列 / 取消 / 重试
async def test_queue_and_task_not_found(client, fake_embed, infra):
    """队列列表可用；不存在的任务号 → `IMP-3001`。"""
    token = await token_of(client, KB_ADMIN)
    queued = (await client.get("/api/v1/import/tasks", headers=auth(token))).json()
    assert queued["data"]["total"] == 0 and queued["data"]["items"] == []

    missing = await client.get("/api/v1/import/tasks/IMP20260101999999",
                               headers=auth(token))
    assert missing.status_code == 404
    assert missing.json()["code"] == "IMP-3001"


async def test_cancel_after_success_is_rejected(client, fake_embed, infra):
    """§3.5 R-01：终态任务不可取消 → `IMP-3002`；非创建者 → `IMP-2001`。"""
    token = await token_of(client, KB_ADMIN)
    body = (await _upload(client, token)).json()["data"]
    infra.append(body["doc_id"])
    await _wait_terminal(client, token, body["task_id"])

    resp = await client.post(f"/api/v1/import/tasks/{body['task_id']}/cancel",
                             headers=auth(token), json={"reason": "手滑了"})
    assert resp.status_code == 409
    assert resp.json()["code"] == "IMP-3002"

    # 换一个没有 doc:delete 的用户（asker 连 doc:upload 都没有 → 403 功能权限）
    asker = await token_of(client, ASKER)
    denied = await client.post(f"/api/v1/import/tasks/{body['task_id']}/cancel",
                               headers=auth(asker), json={})
    assert denied.status_code == 403
    assert denied.json()["code"] == "AUTH-2004"


async def test_retry_rejected_when_not_failed(client, fake_embed, infra):
    """§3.6 R-01：非失败态不可重试 → `IMP-3002`。"""
    token = await token_of(client, KB_ADMIN)
    body = (await _upload(client, token)).json()["data"]
    infra.append(body["doc_id"])
    await _wait_terminal(client, token, body["task_id"])
    resp = await client.post(f"/api/v1/import/tasks/{body['task_id']}/retry",
                             headers=auth(token), json={})
    assert resp.status_code == 409
    assert resp.json()["code"] == "IMP-3002"


async def test_query_param_validation(client, fake_embed, infra):
    """§3.4 R-01/R-02：非法分页与非法 status → `IMP-1007`。"""
    token = await token_of(client, KB_ADMIN)
    for query in ("?page=0", "?page_size=201", "?status=nonsense"):
        resp = await client.get(f"/api/v1/import/tasks{query}", headers=auth(token))
        assert resp.status_code == 400, f"{query} 应报参数错"
        assert resp.json()["code"] == "IMP-1007", query


# ================================================================ 对象键越界保护
async def test_object_key_prefix_is_guarded(client, fake_embed, infra):
    """AC-04-24 / §2.2：对象键不属于 `{doc_id}/` → `IMP-2002`（403）。"""
    token = await token_of(client, KB_ADMIN)
    for bad in ("etc/passwd", "DOC20260101000001/../other/secret.txt",
                "..%2fetc%2fpasswd"):
        resp = await client.get(f"/api/v1/import/files/{bad}", headers=auth(token))
        assert resp.status_code == 403, f"{bad} 应被拒，实际 {resp.status_code}"
        assert resp.json()["code"] == "IMP-2002", bad

    # 编号合法但文档不存在 → 也是 IMP-2002（不能靠"编号像"就放行）
    ghost = await client.get("/api/v1/import/files/DOC20260101000001/parsed.md",
                             headers=auth(token))
    assert ghost.status_code == 403
    assert ghost.json()["code"] == "IMP-2002"


async def test_object_key_guard_is_pure_function():
    """`assert_object_key` 的白盒约束（不依赖 HTTP，便于将来别的调用方复用）。"""
    ok = import_service.assert_object_key("DOC20260101000001/parsed.md")
    assert ok == "DOC20260101000001"
    from app.core.errors import BizError

    for bad in ("", "abc", "/DOC20260101000001/x", "DOC2026/x", "DOC20260101/x",
                "DOC20260101000001/../../x"):
        with pytest.raises(BizError) as exc:
            import_service.assert_object_key(bad)
        assert exc.value.spec.code == "IMP-2002", bad
