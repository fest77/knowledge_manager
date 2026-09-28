# -*- coding: utf-8 -*-
"""模块 04 基础设施的**真机连通测试**（Milvus / MinIO）。

这两个服务是 04 的硬依赖，而"连不上"和"代码写错"在报错上长得很像。
所以这里先证连通性，再证集合/桶可用——**任何一项失败都给出可操作的排查方向**，
而不是只说一句 assert 失败。

跳过策略：服务不可达时 `pytest.skip` 而不是 fail。理由：这两个服务跑在
另一台虚拟机（`192.168.6.170`）上，它关机时不该让整个测试套件变红——
但跳过信息里会写清"跳过的是哪一项、怎么恢复"。
"""
from __future__ import annotations

import socket

import pytest

from app.core.config import settings
from app.infra.milvus import MilvusUnavailable, milvus
from app.infra.minio import MinioUnavailable, minio_store, safe_object_name

pytestmark = pytest.mark.anyio

TEST_COLLECTION_SUFFIX = "_test"


def _reachable(host_port: str, timeout: float = 4.0) -> bool:
    """探测 `host:port` 是否可连（用于决定 skip 还是 fail）。"""
    host, _, port = host_port.partition(":")
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((host, int(port)))
        return True
    except Exception:                                         # noqa: BLE001
        return False
    finally:
        sock.close()


def _milvus_host_port() -> str:
    return settings.milvus_url.replace("http://", "").replace("https://", "")


MILVUS_UP = _reachable(_milvus_host_port())
MINIO_UP = _reachable(settings.minio_endpoint)

requires_milvus = pytest.mark.skipif(
    not MILVUS_UP,
    reason=f"Milvus {settings.milvus_url} 不可达——它是 04 的硬依赖；"
           "启动那台虚拟机后重跑本文件")
requires_minio = pytest.mark.skipif(
    not MINIO_UP,
    reason=f"MinIO {settings.minio_endpoint} 不可达——它是 04 的硬依赖；"
           "启动那台虚拟机后重跑本文件")


# ===================================================================== 配置自检
def test_chunks_collection_is_v2_not_the_old_projects_v1():
    """集合名必须是 `kb_chunks_v2`。

    实测 Milvus 里真实存在旧项目的 `kb_chunks_v1`——如果配置指向它，
    04 会把本项目的切片**写进另一个项目的集合**，而且一切"看起来正常"。
    这是最难发现的一类故障，所以用一条不会跳过的配置断言钉住。
    """
    assert settings.chunks_collection == "kb_chunks_v2"
    assert settings.chunks_collection != "kb_chunks_v1"


def test_embedding_dim_matches_bge_m3():
    """维度必须是 BGE-M3 的 1024，而不是 OpenAI 的 1536。"""
    assert settings.embedding_dim == 1024, \
        "BGE-M3 的 hidden_size 是 1024；写错会让建集合或插入直接失败"


def test_object_key_is_prefixed_by_doc_id():
    """对象键以 `doc_id/` 打头：既能按前缀清理，也让桶里一眼看出归属。"""
    key = minio_store.object_key("DOC20260925000001", 3, "财务 报销制度.pdf")
    assert key.startswith("DOC20260925000001/")
    assert key.split("/")[1].startswith("0003_")
    assert " " not in key and "报" not in key, "非 ASCII 与空格必须被净化"
    assert safe_object_name("a/b\\c:d.pdf") == "a_b_c_d.pdf"
    assert safe_object_name("") == "unnamed"
    assert len(safe_object_name("x" * 200)) <= 80


# ===================================================================== Milvus
@requires_milvus
async def test_milvus_connects_and_lists_collections():
    """连通 + 能看到集合列表（顺带确认旧项目的 v1 确实在那儿）。"""
    await milvus.connect()
    try:
        assert await milvus.ping() is True
        collections = await milvus.require().list_collections()
        assert isinstance(collections, list)
        assert "kb_chunks_v1" in collections, \
            "旧项目的 v1 应仍在——这正是不能把默认值指向它的原因"
    finally:
        await milvus.close()


@requires_milvus
async def test_milvus_ensure_collection_is_idempotent():
    """建表幂等：第二次调用返回 `False`（没重复建）。"""
    await milvus.connect()
    try:
        first = await milvus.ensure_chunks_collection()
        second = await milvus.ensure_chunks_collection()
        assert second is False, "已存在时不应重建"
        info = await milvus.describe_chunks_collection()
        assert info["collection"] == settings.chunks_collection
        assert set(info["fields"]) >= {"chunk_id", "doc_id", "chunk_index", "content",
                                       "dense_vector", "sparse_vector", "enabled"}
        assert first in (True, False)                 # 首次建或本就在
    finally:
        await milvus.close()


@requires_milvus
async def test_milvus_schema_has_no_permission_fields():
    """ER-12：切片**不得**含任何权限字段（含一个就让"权限即时生效"失效）。"""
    await milvus.connect()
    try:
        fields = (await milvus.describe_chunks_collection())["fields"]
    finally:
        await milvus.close()
    for forbidden in ("dept_id", "role_id", "user_id", "permission", "is_global",
                      "departments", "roles"):
        assert forbidden not in fields, f"切片 schema 不该出现权限字段：{forbidden}"


@requires_milvus
async def test_milvus_requires_connection_before_use():
    """未连接时用 `MilvusUnavailable` 快速失败，而不是抛一个看不懂的底层错误。"""
    await milvus.close()
    with pytest.raises(MilvusUnavailable):
        milvus.require()
    assert await milvus.ping() is False


async def test_dense_search_must_specify_anns_field(monkeypatch):
    """★ 回归护栏：`search()` **必须**显式指定 `anns_field=F_DENSE`。

    为什么这条必须有：E01 的集合里有**两个**向量字段（稠密 + 稀疏占位），
    Milvus 无法自行判断搜哪个，会报 `code=65535 multiple anns_fields exist`。
    而它的症状极具迷惑性——**每一次检索都失败**，问答一律回落 `no_knowledge`、
    看板召回/拦截为 0、缺口识别为 0（降级轮次被正确排除），
    看起来像"知识库是空的"，可入库与切片预览却完全正常（`query()` 不需要 `anns_field`）。
    模块 06 的单测把 Milvus 打桩了，只有真机端到端跑一遍才会暴露，所以在这里钉死。
    """
    from app.core.config import settings as _settings
    from app.infra import milvus as milvus_mod

    captured: dict = {}

    class _FakeClient:
        async def search(self, **kwargs):
            captured.update(kwargs)
            return [[{"entity": {"doc_id": "DOC1", "content": "x"}, "distance": 0.9}]]

    # `require()` 是**同步**方法（返回已连接的客户端），所以替身也必须是同步的
    monkeypatch.setattr(milvus, "require", lambda: _FakeClient())

    hits = await milvus.search(dense=[0.1] * _settings.embedding_dim, limit=3,
                               expr="enabled == true")
    assert captured.get("anns_field") == milvus_mod.F_DENSE, \
        f"检索必须指定稠密字段，实际 {captured.get('anns_field')!r}"
    assert captured.get("collection_name") == _settings.chunks_collection
    assert captured.get("filter") == "enabled == true"
    # ★ 切片主键必须取回来：漏了它，qa_logs 的三列表 `chunk_id` 全为 null，
    #   引用卡片定位不到切片，07 还会把 `str(None)` 当文档号 → 候选永远审不过
    assert milvus_mod.F_CHUNK_ID in (captured.get("output_fields") or []), \
        "检索必须取回 chunk_id（切片主键）"
    assert hits and hits[0]["score"] == 0.9 and hits[0]["doc_id"] == "DOC1"


# ===================================================================== MinIO
@requires_minio
async def test_minio_connects_and_bucket_exists():
    """连通 + 桶存在（实测该桶已由旧项目建好）。"""
    await minio_store.connect()
    try:
        assert await minio_store.ping() is True
    finally:
        await minio_store.close()


@requires_minio
async def test_minio_roundtrip_put_get_remove():
    """上传 → 下载 → 比对 → 删除，走一遍真实对象存储。"""
    await minio_store.connect()
    key = minio_store.object_key("DOCTEST0000000001", 1, "往返测试.txt")
    payload = "知识库管理平台 · MinIO 往返测试".encode()
    try:
        await minio_store.put_bytes(key, payload, content_type="text/plain; charset=utf-8")
        assert await minio_store.get_bytes(key) == payload
    finally:
        await minio_store.remove(key)
        await minio_store.close()


@requires_minio
async def test_minio_requires_connection_before_use():
    """未连接时用 `MinioUnavailable` 快速失败。"""
    await minio_store.close()
    with pytest.raises(MinioUnavailable):
        minio_store.require()
    assert await minio_store.ping() is False


def test_service_reachability_is_reported():
    """把两个服务的可达性作为一条显式断言输出，便于在 CI/演示前一眼确认。"""
    assert isinstance(MILVUS_UP, bool) and isinstance(MINIO_UP, bool)
    if not (MILVUS_UP and MINIO_UP):
        pytest.skip(f"Milvus={MILVUS_UP} MinIO={MINIO_UP}（部分用例已跳过）")
