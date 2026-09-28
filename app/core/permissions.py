# -*- coding: utf-8 -*-
"""功能权限：权限码声明与校验（总纲 ER-08 / ER-09）。

**设计取舍**：功能权限拦截**不用中间件，改用 FastAPI 依赖**。

原因：Starlette 的中间件在**路由匹配之前**执行，此刻拿不到「本请求命中了哪条路由」，
也就读不到路由上声明的权限码；而依赖在路由匹配之后执行，`request.scope["route"]`
已经就位。

**JWT 认证仍留在中间件**：它必须早于路由（要能拦住不存在的路径），且负责白名单判断。
于是形成「中间件管**认证**、依赖管**授权**」的分工。
"""
from __future__ import annotations

from typing import Callable, TypeVar

F = TypeVar("F", bound=Callable)

_ATTR = "__km_required_perms__"


def require_perm(*codes: str) -> Callable[[F], F]:
    """声明路由所需的功能权限码，**OR 语义**（命中任一即可）。

    用法::

        @router.get("/logs")
        @require_perm("audit:read")
        async def list_logs(...): ...

    权限码必须已在 `sys_permissions` 里有定义（ER-09）。
    """
    if not codes:
        raise ValueError("require_perm 至少要一个权限码")

    def decorator(func: F) -> F:
        setattr(func, _ATTR, tuple(codes))
        return func

    return decorator


def required_perms_of(route: object | None) -> tuple[str, ...]:
    """从已匹配的路由对象上取回声明的权限码；没声明则返回空元组。

    `route` 为 None 时（未命中任何路由）同样返回空元组——那属于 404 处理器的职责。
    """
    endpoint = getattr(route, "endpoint", None)
    return getattr(endpoint, _ATTR, ())
