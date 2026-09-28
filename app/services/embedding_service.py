# -*- coding: utf-8 -*-
"""BGE-M3 dense 向量化（模块 04 的 `embedding` 阶段）。

**为什么用 `sentence-transformers` 而不是 `FlagEmbedding`**：`FlagEmbedding` 不在
`requirements.txt` 里，而 `sentence-transformers` 在（已装 6.1.0）。项目规则是
"不引入未声明依赖"，所以从声明过的库走。代价是拿不到 BGE-M3 的 sparse / colbert 头
——那两条要等确认允许新增依赖后再补（见进度文档 §3.2.1）。

**四个刻意的设计**：

| 设计 | 为什么 |
|---|---|
| **懒加载** | 模型 2.2GB、首次加载十几秒。放进 lifespan 会拖慢启动，且"连不上库"这类更要紧的错会被它淹没 |
| **`normalize_embeddings=True`** | Milvus 度量是 `COSINE`；归一化后余弦等价于内积，两次嵌入的结果也可比 |
| **设备降级要留痕** | GPU 不可用时自动退回 CPU，但**必须记一条 `gpu_cpu` 降级**——否则"怎么比昨天慢十倍"会变成悬案 |
| **维度断言** | 实测 1024 维。若给出别的维度就**立刻抛错**，比让 Milvus 在流水线第 6 步才插入失败好得多 |
"""
from __future__ import annotations

import threading
from typing import Any, Sequence

from app.core.config import settings
from app.core.logging import logger
from app.core.progress import Progress, active_progress

# 期望维度：与 `.env` 的 EMBEDDING_DIM、Milvus 集合的 dim 必须三者一致
EXPECTED_DIM = 1024
# 单批最多嵌入多少条：显存有限，一次塞 512 条长文本会 OOM；分批是稳定的做法
BATCH_SIZE = 16
# 单条文本最多喂多少字符：BGE-M3 的最大长度是 8192 token，但中文 2000 字已远超
# 典型切片长度；超出只会浪费显存，且相似度会被稀释
MAX_INPUT_CHARS = 4000


class EmbeddingUnavailable(RuntimeError):
    """向量化模型不可用（模型目录缺失 / 依赖缺装 / 加载失败）。"""


class EmbeddingService:
    """进程级单例。模型**懒加载**，加载过程加锁（避免并发请求各加载一份）。"""

    def __init__(self) -> None:
        self._model: Any = None
        self._device: str | None = None
        self._degraded_reason: str | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 生命周期
    @property
    def loaded(self) -> bool:
        """模型是否已加载。"""
        return self._model is not None

    @property
    def device(self) -> str | None:
        """实际使用的设备（`cuda:0` / `cpu`）；未加载时为 `None`。"""
        return self._device

    @property
    def degraded_reason(self) -> str | None:
        """若发生 GPU→CPU 降级，这里是原因（供写入 `degraded[]`）。"""
        return self._degraded_reason

    def _resolve_device(self) -> tuple[str, str | None]:
        """决定跑在哪个设备上，返回 `(device, 降级原因)`。

        降级原因非空即表示"配置要 GPU，但只能用 CPU"——调用方据此记 `gpu_cpu`。
        """
        want = settings.bge_device
        if not want.startswith("cuda"):
            return want, None
        try:
            import torch
        except Exception as exc:                              # noqa: BLE001
            return "cpu", f"torch 不可用：{exc}"
        if not torch.cuda.is_available():
            return "cpu", "CUDA 不可用（驱动缺失或显卡被占用）"
        return want, None

    def load(self) -> Any:
        """加载模型（幂等）。失败抛 `EmbeddingUnavailable`，由调用方决定降级。

        ★ **加载过程会打进度**（`Progress` 心跳 + 阶段打点）：这是一次
        15~40 秒的同步阻塞调用，中间一行输出都没有的话，终端里与"卡死"无异。
        阶段划分对着真实的耗时来源：`torch` 首次导入要好几秒、
        `SentenceTransformer(...)` 是主体（含权重加载）、fp16 转换与自检各占一点。
        """
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:                       # 双检：等锁期间别人可能已加载
                return self._model
            path = settings.bge_m3_path
            if not path.is_dir():
                raise EmbeddingUnavailable(f"BGE-M3 模型目录不存在：{path}")
            with Progress("BGE-M3 加载", hint="首次约 15~40 秒，之后常驻显存") as bar:
                bar.step("解析设备")
                device, reason = self._resolve_device()
                try:
                    bar.step("导入 sentence-transformers / torch")
                    from sentence_transformers import SentenceTransformer

                    bar.step(f"读取模型权重（{path}）")
                    model = SentenceTransformer(str(path), device=device)
                    if device.startswith("cuda") and settings.bge_fp16:
                        bar.step("转换为半精度（fp16）")
                        model.half()
                    bar.step("校验向量维度")
                    # sentence-transformers 6.x 把方法名改了；两个名字都试，
                    # 免得一次依赖升级就让向量化整个不可用
                    getter = (getattr(model, "get_embedding_dimension", None)
                              or model.get_sentence_embedding_dimension)
                    dim = int(getter())
                    if dim != EXPECTED_DIM:
                        raise EmbeddingUnavailable(
                            f"BGE-M3 维度异常：期望 {EXPECTED_DIM}，实际 {dim}"
                            "（Milvus 集合按 1024 建，维度不符会在写入时才失败）")
                    bar.step("试跑一次前向（自检）")
                    model.encode(["自检"], normalize_embeddings=True,
                                 show_progress_bar=False, convert_to_numpy=True)
                    self._model = model
                    self._device = device
                    self._degraded_reason = reason
                except EmbeddingUnavailable:
                    raise
                except Exception as exc:                      # noqa: BLE001
                    raise EmbeddingUnavailable(f"加载 BGE-M3 失败：{exc}") from exc

            if reason:
                logger.warning("向量化已降级到 %s：%s", device, reason)
            logger.info("BGE-M3 已加载：device=%s fp16=%s dim=%d",
                        device, settings.bge_fp16, EXPECTED_DIM)
            return self._model

    def unload(self) -> None:
        """卸载模型并释放显存（测试隔离 / 显存吃紧时手工调用）。

        ⚠️ **没加载时立刻返回**：旧实现无论有没有模型都会 `import torch` 并
        调 `torch.cuda.empty_cache()`——首次 `import torch` 本身就要好几秒
        （实测 8.3 秒），于是"对空服务调一次 unload"白等 8 秒。
        """
        if self._model is None and self._device is None:
            return
        self._model = None
        self._device = None
        self._degraded_reason = None
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:                                     # noqa: BLE001
            pass

    def health(self) -> dict[str, Any]:
        """`/health` 用：**不触发加载**，只报告当前状态（含"是否正在加载"）。"""
        loading = active_progress().get("BGE-M3 加载")
        return {"loaded": self.loaded, "device": self._device,
                "degraded": self._degraded_reason is not None,
                "reason": self._degraded_reason, "dim": EXPECTED_DIM,
                # 正在加载时报"加载中 + 已用秒数"，让探针/前端能显示进度
                "loading": loading is not None,
                "loading_seconds": loading}

    # ------------------------------------------------------------------ 嵌入
    def embed(self, texts: Sequence[str], *, batch_size: int = BATCH_SIZE
              ) -> list[list[float]]:
        """把一批文本嵌入成 dense 向量（**保持输入顺序**）。

        `normalize_embeddings=True`：Milvus 用 COSINE，归一化后余弦 == 内积，
        省掉一次除法，也让两次嵌入的结果可比。

        ⚠️ **fp16 下不是逐位可复现**：实测同一段文本两次嵌入的最大绝对差约 `2.4e-4`
        （fp16 的机器精度），因此**任何"两次结果完全相等"的比较都是错的**；
        需要判等时用 `abs=1e-3` 量级的容差。这不是 bug，是半精度的固有性质——
        要逐位可复现就得关 `BGE_FP16`，代价是显存翻倍、速度减半。
        """
        items = [self._prepare(t) for t in texts]
        if not items:
            return []
        model = self.load()
        vectors = model.encode(items, batch_size=batch_size,
                               normalize_embeddings=True, convert_to_numpy=True,
                               show_progress_bar=False)
        return [[float(x) for x in row] for row in vectors]

    def embed_one(self, text: str) -> list[float]:
        """单条嵌入（问答链路用：检索词只嵌一次）。"""
        return self.embed([text])[0]

    @staticmethod
    def _prepare(text: str) -> str:
        """截断过长输入。

        BGE-M3 支持 8192 token，但本项目的切片最长 1000 字（`splitter.MAX_BODY_CHARS`），
        查询词更短。真出现超长输入（比如有人直接拿整篇文档来嵌），截断比让它吃满显存好。
        """
        value = (text or "").strip()
        return value[:MAX_INPUT_CHARS] if len(value) > MAX_INPUT_CHARS else value


embedding_service = EmbeddingService()

__all__ = ["EmbeddingService", "EmbeddingUnavailable", "embedding_service",
           "EXPECTED_DIM", "BATCH_SIZE", "MAX_INPUT_CHARS"]
