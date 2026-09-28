# -*- coding: utf-8 -*-
"""FastAPI 依赖：当前用户上下文 + 功能权限拦截。

**认证与授权的分工**（与 `middleware/jwt_auth.py` 配合）：

| 关注点 | 位置 | 为什么 |
|---|---|---|
| **认证**（你是谁） | JWT 中间件 | 必须早于路由匹配，才能拦住不存在的路径 |
| **授权**（你能不能） | 本模块的 `enforce_perm` 依赖 | 需要 `request.scope["route"]`，它只在路由匹配后存在 |

`enforce_perm` 通过 `FastAPI(dependencies=[Depends(enforce_perm)])` **全局注册**，
于是每个路由自动生效，不需要逐个接口声明——少写一处就少一个漏网之鱼。
"""
from __future__ import annotations

from fastapi import Request

from app.core.errors import BizError, Err, ErrorSpec
from app.core.logging import logger
from app.core.permissions import required_perms_of
from app.services.auth_service import UserContext


def current_user(request: Request) -> UserContext:
    """取中间件已装载的当前用户上下文（受保护路径上必然存在）。"""
    context: UserContext | None = getattr(request.state, "user", None)
    if context is None:
        raise BizError(Err.AUTH_TOKEN_INVALID, "请求未经鉴权中间件处理")
    return context


def ensure_permission(user: UserContext, code: str, spec: ErrorSpec) -> None:
    """**服务层兜底**（各模块 Spec 的 R-02）：权限检查的第二道闸。

    正常路径由全局依赖 `enforce_perm` 拦截；这一层防的是"绕过 FastAPI 直调处理函数"
    ——单测、后台任务、将来的定时任务都可能这样做。少了它，功能权限就只存在于
    HTTP 这一条路径上。

    各模块传入**自己的**错误码（审计用 `AUD-2001`、组织架构用 `AUTH-2004`），
    这样"从哪个模块越权"在日志里一眼可辨（ER-01）。
    """
    if code not in user.permissions:
        raise BizError(spec, f"内部直调缺少权限：{code}")


def enforce_perm(request: Request) -> None:
    """功能权限拦截（总纲 ER-09）。

    - 路由没声明权限码 → 放行（如 `/health`、登录、仅需登录态的 `/auth/me`）
    - 声明了但用户不具备 → `AUTH-2004`（HTTP 403）
    - 声明了但请求没有用户上下文（白名单路由误用）→ 按未认证处理

    **注意**：这是「能不能用某功能」的功能权限；「能不能读某篇知识」是四维数据权限，
    由模块 05 判定（ER-03：判定只有一份实现）。两者独立，都要过。
    """
    codes = required_perms_of(request.scope.get("route"))
    if not codes:
        return
    user: UserContext | None = getattr(request.state, "user", None)
    if user is None:
        raise BizError(Err.AUTH_TOKEN_INVALID, "请求未经鉴权中间件处理")
    if not (user.permissions & set(codes)):
        logger.info("功能权限不足 need=%s have=%d", codes, len(user.permissions))
        raise BizError(Err.AUTH_PERM_DENIED, f"需要权限：{' 或 '.join(codes)}")
