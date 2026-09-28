# -*- coding: utf-8 -*-
"""结构化日志：单例 logger，格式里带 trace_id（由中间件写入 contextvar）。"""
from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

from app.core.config import settings

trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")


class _TraceFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = trace_id_var.get()
        return True


def _build() -> logging.Logger:
    log = logging.getLogger("knowledge_manager")
    if log.handlers:          # 避免 uvicorn --reload 下重复挂 handler
        return log
    log.setLevel(settings.log_level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s %(levelname)-5s [%(trace_id)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    handler.addFilter(_TraceFilter())
    log.addHandler(handler)
    log.propagate = False
    return log


logger = _build()
