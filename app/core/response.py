# -*- coding: utf-8 -*-
"""统一响应契约（总纲 §6.2）。

**信封结构的唯一定义处**：异常处理器（`core/errors.py`）与鉴权中间件
（`middleware/jwt_auth.py`）都必须调用这里，不得各自拼字典——
否则契约一改就会漏改一处，两个出口的响应形状悄悄分叉。
"""
from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

from app.core.logging import trace_id_var


def envelope(code: str | int, message: str, data: Any = None,
             trace_id: str | None = None) -> dict[str, Any]:
    """构造统一响应体：`{code, message, data, trace_id}`。

    `trace_id` 缺省时从 contextvar 取（中间件已注入）。
    """
    return {
        "code": code,
        "message": message,
        "data": data,
        "trace_id": trace_id if trace_id is not None else trace_id_var.get(),
    }


def ok(data: Any = None) -> JSONResponse:
    """成功响应：HTTP 恒为 200，业务结果放在 `data` 里。"""
    return JSONResponse(status_code=200, content=envelope(0, "ok", data))


def fail(spec_code: str, message: str, http: int,
         trace_id: str | None = None) -> JSONResponse:
    """失败响应。业务码是字符串（如 `AUTH-2001`），HTTP 状态由调用方按错误码规范决定。"""
    return JSONResponse(status_code=http,
                        content=envelope(spec_code, message, None, trace_id))
