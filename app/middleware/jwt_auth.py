# -*- coding: utf-8 -*-
"""JWT 鉴权中间件 + trace_id 注入（总纲 ER-08：除白名单外**所有**接口必须过 JWT）。

为什么**不**把异常交给 `app.exception_handler`：Starlette 的用户中间件位于
`ExceptionMiddleware` **之外**，在中间件里抛异常不会被已注册的处理器捕获。
因此本中间件直接调用 `core/response.fail()` —— 信封结构只有一份定义，
两个出口（中间件 / 异常处理器）永远同形。
"""
from __future__ import annotations

import time
import uuid

from fastapi import FastAPI, Request, Response

from app.core.errors import BizError, Err
from app.core.logging import logger, trace_id_var
from app.core.response import fail
from app.core.security import decode_token
from app.services import auth_service

# 白名单：请求路径以这些前缀开头时不校验令牌
WHITELIST: tuple[str, ...] = (
    "/health",
    "/api/v1/auth/login",
    "/ui",                 # 前端静态资源（登录页必须在无令牌时可访问）
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
)


def _bearer(request: Request) -> str | None:
    """取 Bearer 令牌；scheme 大小写不敏感（RFC 7235）。"""
    raw = request.headers.get("authorization") or ""
    scheme, _, token = raw.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def _authenticate(request: Request) -> Response | None:
    """通过则返回 None，否则返回统一契约的 401 响应。"""
    trace_id = request.state.trace_id
    token = _bearer(request)
    if token is None:
        return fail(Err.AUTH_TOKEN_INVALID.code, "缺少 Bearer 令牌",
                    Err.AUTH_TOKEN_INVALID.http, trace_id)
    try:
        payload = decode_token(token)
        context = await auth_service.load_context(payload["sub"])
    except BizError as exc:
        return fail(exc.spec.code, exc.detail or exc.spec.message, exc.spec.http, trace_id)
    request.state.user = context
    return None


def install(app: FastAPI) -> None:
    """安装 trace_id 注入 + JWT 鉴权中间件。**必须在 include_router 之前调用。**"""

    @app.middleware("http")
    async def trace_and_auth(request: Request, call_next) -> Response:
        trace_id = uuid.uuid4().hex[:16]
        request.state.trace_id = trace_id
        reset_token = trace_id_var.set(trace_id)
        started = time.perf_counter()
        try:
            path = request.url.path
            if path.startswith(WHITELIST):
                response = await call_next(request)
            else:
                response = await _authenticate(request) or await call_next(request)
            elapsed_ms = (time.perf_counter() - started) * 1000
            response.headers["X-Trace-Id"] = trace_id
            logger.info("%s %s -> %s (%.1f ms)", request.method, path,
                        response.status_code, elapsed_ms)
            return response
        finally:
            trace_id_var.reset(reset_token)
