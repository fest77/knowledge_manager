# -*- coding: utf-8 -*-
"""权限缓存（模块 01 Spec §4.3）。

一次问答请求会触发多次权限判定（功能权限 1 次 + 数据权限 N 次），每次都查库会把
延迟堆起来。60 秒 TTL + **主动失效**兼顾正确性与性能。

**为什么不用"每次查库"也不做永久缓存**：永久缓存会让调岗/停权在超时前不生效——
而"权限即时生效"是本项目的核心卖点（AD-02）。60 秒是兜底，主动失效才是主路径。

**缓存的是什么**：用户 → `(角色快照, 权限码)`。**绝不缓存 JWT 有效性**：
令牌只在签发时校验一次签名，每请求仍要读 `sys_users.status`（AC-01-03：
停用后已签发的令牌立即失效）——所以 `load_context()` 里的用户与状态查询**不走缓存**。

> **`DEC-01-x`（模块内决策）**：`invalidate_role(role_id)` 采用**全局纪元 +1** 的
> 粗粒度失效，而不是维护"角色 → 用户"反向索引。理由：角色授权变更是低频操作，
> 而反向索引要在用户角色绑定处同步维护，多一处状态就多一处不一致的可能。
> 代价是授权变更后**所有**用户的权限快照各重查一次——这是正确的方向（多查，
> 不会少判），而漏失效会导致**越权**（少查），两者代价不对称。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import logger


@dataclass(frozen=True, slots=True)
class CachedGrants:
    """一个用户的权限快照（缓存值）。

    连 `menus` 一起缓存：菜单是**权限码的纯函数**（`menu_path` 非空的那些码），
    不缓存它就会在每次请求里多一次 `sys_permissions` 查询，
    等于把"功能权限 1 次查库"又变回 2 次。
    """

    roles: tuple[dict[str, Any], ...]
    codes: tuple[str, ...]
    menus: tuple[dict[str, Any], ...] = ()


@dataclass
class _Entry:
    value: CachedGrants
    expire_at: float
    epoch: int


@dataclass
class PermissionCache:
    """进程内 TTL 缓存：`user_id -> (roles, permission_codes)`。"""

    ttl: float = 60.0
    _users: dict[str, _Entry] = field(default_factory=dict)
    _epoch: int = 0
    _hits: int = 0
    _misses: int = 0

    # ------------------------------------------------------------------ 读
    def get(self, user_id: str) -> CachedGrants | None:
        """取缓存；过期、或纪元已变（有角色授权变更）则视为未命中。"""
        entry = self._users.get(user_id)
        if entry is None:
            self._misses += 1
            return None
        if entry.epoch != self._epoch or entry.expire_at <= time.monotonic():
            self._users.pop(user_id, None)
            self._misses += 1
            return None
        self._hits += 1
        return entry.value

    def put(self, user_id: str, value: CachedGrants) -> None:
        """写缓存（带当前纪元与过期时间）。"""
        self._users[user_id] = _Entry(value=value,
                                      expire_at=time.monotonic() + self.ttl,
                                      epoch=self._epoch)

    # ------------------------------------------------------------------ 失效
    def invalidate_user(self, user_id: str) -> bool:
        """失效单个用户（调岗 / 停用 / 角色变更 / 重置密码后调用）。"""
        removed = self._users.pop(user_id, None) is not None
        if removed:
            logger.info("权限缓存已失效 user_id=%s", user_id)
        return removed

    def invalidate_role(self, role_id: str) -> None:
        """失效某角色带来的影响（**粗粒度**：纪元 +1，全部快照作废）。

        参数 `role_id` 只用于日志——见模块头部的决策说明。
        """
        self._epoch += 1
        self._users.clear()
        logger.info("权限缓存已全量失效（角色 %s 的授权发生变更，epoch=%d）",
                    role_id, self._epoch)

    def invalidate_all(self) -> None:
        """全量清空（清库 / 测试隔离 / 演示前复位）。"""
        self._users.clear()
        self._epoch += 1

    # ------------------------------------------------------------------ 观测
    def stats(self) -> dict[str, Any]:
        """给 `/health` 或排障看的统计（命中率能直接反映 TTL 是否合理）。"""
        total = self._hits + self._misses
        return {"size": len(self._users), "hits": self._hits, "misses": self._misses,
                "hit_rate": round(self._hits / total, 3) if total else 0.0,
                "ttl": self.ttl, "epoch": self._epoch}


permission_cache = PermissionCache()

__all__ = ["CachedGrants", "PermissionCache", "permission_cache"]
