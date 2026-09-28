# -*- coding: utf-8 -*-
"""重排序（模块 06 的可选环节）：BGE reranker，本地 CrossEncoder。

## 为什么它是"可选"的

召回是"宽进"，重排是"精排"。没有重排，答案质量会下降（相关的排不到前面），
但**流程仍然完整**——所以模型不可用时**降级跳过**，而不是让整个问答失败
（Spec §5：`QA-4004` 只记 `degraded=true`，不抛给用户）。

这与其他环节的处置不同，值得对照：

| 环节 | 不可用时 | 为什么不同 |
|---|---|---|
| Embedding | **无法检索** → `no_knowledge` 兜底 | 它在前链路上，缺了就什么都召不回来 |
| Milvus | 同上 | 同上 |
| **Rerank** | **跳过，用召回顺序** | 它在后链路上，只是"排得不够好"，不影响"有没有" |
| LLM | **error 事件** | 它是最终产出者，缺了就没有答案 |

## 惰性加载

模型几百 MB，首次加载数秒。放进 `lifespan` 会拖慢启动，而且"连不上库"这类
更要紧的错误会被它淹没（与 `embedding_service` 同一条理由）。
"""
from __future__ import annotations

import threading
from typing import Any, Sequence

from app.core.config import settings
from app.core.logging import logger
from app.core.progress import Progress, active_progress

# 一次最多重排多少条：CrossEncoder 的复杂度是 O(n) 次前向，
# 而召回放大后可能有 50 条；全排会明显拖慢（实测 50 条约几百毫秒）
MAX_RERANK_DOCS = 60


class RerankUnavailable(RuntimeError):
    """重排模型不可用（模型目录缺失 / 加载失败 / 推理异常）→ 调用方**跳过重排**。"""


class RerankService:
    """进程级单例；模型惰性加载并加锁。"""

    def __init__(self) -> None:
        self._model: Any = None
        self._device: str | None = None
        self._degraded_reason: str | None = None
        self._lock = threading.Lock()
        self._failed = False

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def device(self) -> str | None:
        return self._device

    @property
    def degraded_reason(self) -> str | None:
        return self._degraded_reason

    def load(self) -> Any:
        """加载 CrossEncoder（幂等）。失败抛 `RerankUnavailable`。"""
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            path = settings.bge_reranker_path
            if not path.is_dir():
                raise RerankUnavailable(f"reranker 模型目录不存在：{path}")
            device = settings.bge_reranker_device
            with Progress("reranker 加载", hint="首次几秒，之后常驻") as bar:
                try:
                    bar.step("导入 sentence-transformers")
                    from sentence_transformers import CrossEncoder

                    bar.step(f"读取模型权重（{path}）")
                    model = CrossEncoder(str(path), device=device,
                                         max_length=512)
                    bar.step("试跑一次打分（自检）")
                    model.predict([("自检问题", "自检片段")])
                    self._model = model
                    self._device = device
                except Exception as exc:                    # noqa: BLE001
                    raise RerankUnavailable(f"加载 reranker 失败：{exc}") from exc
            logger.info("reranker 已加载：device=%s fp16=%s", device,
                        settings.bge_reranker_fp16)
            return self._model

    def unload(self) -> None:
        """卸载模型（没加载时**立刻返回**，不去 `import torch` 白等几秒）。"""
        if self._model is None and self._device is None:
            return
        self._model = None
        self._device = None
        self._degraded_reason = None

    def health(self) -> dict[str, Any]:
        """`/health` 用：**不触发加载**（含"是否正在加载"）。"""
        loading = active_progress().get("reranker 加载")
        return {"loaded": self.loaded, "device": self._device,
                "degraded": self._failed,
                "reason": self._degraded_reason,
                "loading": loading is not None, "loading_seconds": loading}

    def rerank(self, query: str, documents: Sequence[str], *,
               top_k: int | None = None) -> list[tuple[int, float]]:
        """对 (query, document) 打分并按分数降序返回 `[(原下标, 分数)]`。

        返回**原下标**而不是重排后的文档：调用方要拿它去对齐切片元信息
        （标题、doc_id），返回文档本身会让调用方不得不再反查一次"这段文字是哪一片"。
        """
        items = list(documents)[:MAX_RERANK_DOCS]
        if not items:
            return []
        model = self.load()
        pairs = [(query, text) for text in items]
        try:
            scores = model.predict(pairs, show_progress_bar=False)
        except Exception as exc:                            # noqa: BLE001
            raise RerankUnavailable(f"rerank 推理失败：{exc}") from exc
        ranked = sorted(((index, float(score))
                         for index, score in enumerate(scores)),
                        key=lambda pair: pair[1], reverse=True)
        if top_k is not None:
            ranked = ranked[:top_k]
        # 记一次成功的降级状态：模型可用则清掉原因，避免上次失败的原因一直挂着
        self._failed = False
        self._degraded_reason = None
        return ranked

    def note_skip(self, reason: str) -> None:
        """记录"本次跳过了重排"（供 `qa_logs.degraded` 与 `/health` 观测）。"""
        self._failed = True
        self._degraded_reason = reason
        logger.warning("重排已跳过：%s", reason)


rerank_service = RerankService()

__all__ = ["RerankService", "RerankUnavailable", "rerank_service", "MAX_RERANK_DOCS"]
