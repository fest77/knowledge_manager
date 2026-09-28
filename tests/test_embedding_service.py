# -*- coding: utf-8 -*-
"""BGE-M3 向量化服务的**真机测试**。

这个文件会真的加载模型（2.2GB、十几秒）并跑一次 GPU 前向。
慢是刻意的：向量化是 04 流水线的第 5 步，它坏了整条链路就只到"切片"为止，
而"模型其实没加载成功"和"代码调错了"在报错上长得很像。

**最要紧的一条断言是维度**：Milvus 集合按 1024 建（`EMBEDDING_DIM`）。
如果模型给出别的维度，插入会在流水线跑到第 6 步时才失败——那时
前 5 步（MinIO 上传、MinerU 解析、切片）全白做，排查成本高得多。
所以这里在**服务层**就把维度钉死，早失败。

模型目录缺失时 `pytest.skip`（不是 fail）：换台机器没同步模型是环境问题，
不该让整个测试套件变红——但跳过信息写明怎么恢复。
"""
from __future__ import annotations

import math
import time

import pytest

from app.core.config import settings
from app.services.embedding_service import (BATCH_SIZE, EXPECTED_DIM,
                                            EmbeddingUnavailable,
                                            embedding_service)

pytestmark = pytest.mark.anyio

MODEL_READY = settings.bge_m3_path.is_dir()

requires_model = pytest.mark.skipif(
    not MODEL_READY,
    reason=f"BGE-M3 模型目录不存在：{settings.bge_m3_path}——"
           "它来自 MODELSCOPE_CACHE，同步模型后重跑本文件")


def test_config_device_string_has_no_trailing_comment():
    """`.env` 里 `BGE_DEVICE=cuda:0 # 老师写的是cpu` 带行尾注释，必须被剥掉。

    不剥的话 `SentenceTransformer(device="cuda:0 # 老师写的是cpu")` 会报一个
    完全看不懂的错——这类"配置里混进了注释"的坑一旦发生，排查方向会跑偏很远。
    """
    assert settings.bge_device in ("cuda:0", "cpu", "cuda")
    assert "#" not in settings.bge_device
    assert not settings.bge_device.endswith(" ")


def test_expected_dim_is_1024_and_matches_config():
    """维度三处必须一致：本模块的 `EXPECTED_DIM`、`.env`、Milvus 集合。"""
    assert EXPECTED_DIM == 1024
    assert settings.embedding_dim == EXPECTED_DIM, \
        "EMBEDDING_DIM 与模型维度不一致，Milvus 写入会在最后一步才失败"


def test_health_does_not_trigger_loading():
    """`/health` 不该触发模型加载——否则第一次探活就要等十几秒。

    顺带钉住**未加载时的 `unload()` 是零成本**的：旧实现无论有没有模型都会
    `import torch` + `torch.cuda.empty_cache()`，首次 `import torch` 实测 8.3 秒，
    于是"对空服务卸一次"白等 8 秒（本文件里就白等了 8.27 秒）。
    """
    embedding_service.unload()
    started = time.monotonic()
    health = embedding_service.health()
    assert health["loaded"] is False
    assert health["dim"] == EXPECTED_DIM
    assert health["loading"] is False, "没在加载时报 loading=True 会让前端一直转圈"
    assert embedding_service.loaded is False
    embedding_service.unload()                     # 再卸一次：同样应当立刻返回
    assert time.monotonic() - started < 2.0, \
        "未加载时的 unload()/health() 不该有秒级开销（旧实现会 import torch）"


async def test_unavailable_when_model_dir_missing(monkeypatch):
    """模型目录不存在时给 `EmbeddingUnavailable`，而不是底层的一串 FileNotFound。

    注意 `settings` 是 **frozen + slots** 的 dataclass，**不能** `setattr` 它的字段
    （会抛 `FrozenInstanceError`）。所以这里替换的是服务模块里对 settings 的**引用**。
    """
    import dataclasses
    from pathlib import Path

    from app.services import embedding_service as es_mod

    fake = dataclasses.replace(settings, bge_m3_path=Path("D:/不存在的模型目录"))
    monkeypatch.setattr(es_mod, "settings", fake)
    embedding_service.unload()
    try:
        with pytest.raises(EmbeddingUnavailable):
            embedding_service.load()
    finally:
        embedding_service.unload()


@requires_model
def test_loads_on_configured_device_and_reports_dim():
    """真机加载：设备解析正确、维度正确、`health()` 如实反映状态。"""
    embedding_service.unload()
    model = embedding_service.load()
    assert model is not None
    assert embedding_service.loaded is True
    assert embedding_service.device == settings.bge_device
    health = embedding_service.health()
    assert health["loaded"] is True and health["dim"] == EXPECTED_DIM
    # 幂等：第二次 load 不该重新加载
    assert embedding_service.load() is model


@requires_model
def test_embed_produces_normalized_1024_dim_vectors():
    """核心断言：**1024 维、已归一化**（Milvus 用 COSINE，归一化后余弦 == 内积）。"""
    vectors = embedding_service.embed(["财务报销需要三样材料：发票、审批单、银行回单。"])
    assert len(vectors) == 1
    assert len(vectors[0]) == EXPECTED_DIM
    norm = math.sqrt(sum(x * x for x in vectors[0]))
    assert abs(norm - 1.0) < 1e-3, f"向量未归一化，模长={norm}"


@requires_model
def test_embed_preserves_input_order_and_batch_size():
    """顺序必须与输入一致：切片与向量错位是最难发现的一类 bug。

    容差用 `1e-3` 而不是 `1e-6`：fp16 推理下同一文本两次嵌入的最大绝对差实测约
    `2.4e-4`（半精度机器精度）。**"两次结果完全相等"是个错误的期望**——
    要逐位可复现必须关掉 `BGE_FP16`，代价是显存翻倍、速度减半。
    这条容差本身就是"我们用的是 fp16"这个事实的断言。
    """
    texts = [f"第{i}条 制度说明。" for i in range(BATCH_SIZE + 3)]
    vectors = embedding_service.embed(texts)
    assert len(vectors) == len(texts)
    again = embedding_service.embed(texts[:2])
    assert again[0] == pytest.approx(vectors[0], abs=1e-3)
    assert again[1] == pytest.approx(vectors[1], abs=1e-3)
    # 顺序稳定性用"相似度自比最高"再兜一道（不受 fp16 抖动影响）
    assert float(sum(x * y for x, y in zip(again[0], vectors[0]))) > 0.99


@requires_model
def test_similar_text_scores_higher_than_unrelated():
    """语义可用性：相近文本的余弦相似度必须高于无关文本。

    这一条是"向量化真的在工作"的最直接证据——如果模型没正确加载、
    或者用了错的池化策略，相似度会退化成噪声，此处立刻红。
    """
    import numpy as np

    a, b, c = embedding_service.embed([
        "员工出差住宿费上限是多少",
        "差旅住宿标准：一线城市 600 元每晚",
        "公司年会抽奖活动的流程安排"])
    sim_related = float(np.dot(a, b))
    sim_unrelated = float(np.dot(a, c))
    assert sim_related > sim_unrelated, \
        f"相近文本相似度({sim_related:.3f}) 应高于无关文本({sim_unrelated:.3f})"


@requires_model
def test_long_input_is_truncated_not_rejected():
    """超长输入截断而不是抛错：有人直接拿整篇文档来嵌时不该 500。"""
    vectors = embedding_service.embed(["甲" * 5000])
    assert len(vectors[0]) == EXPECTED_DIM


def test_empty_input_returns_empty_without_loading():
    """空输入直接返回空列表，**不触发模型加载**（避免为一个空批次花十几秒）。"""
    embedding_service.unload()
    assert embedding_service.embed([]) == []
    assert embedding_service.loaded is False
