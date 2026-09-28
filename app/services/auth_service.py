# -*- coding: utf-8 -*-
"""模块 01 的服务层：登录与用户上下文装载。

登录校验顺序（模块 Spec §3.1，**任一步失败即返回，且不泄漏账号是否存在**）：
  1) 参数非空/长度           → AUTH-1001
  2) 账号存在               → AUTH-2001（与密码错**同码**，防账号枚举）
  3) status == active        → AUTH-2002
  4) bcrypt 校验密码         → AUTH-2001
  5) 签发 JWT               → AUTH-5001
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.api.schemas_auth import LoginResponse, MenuItem, RoleBrief, UserProfile
from app.core.config import settings
from app.core.enums import UserStatus
from app.core.errors import BizError, Err
from app.core.logging import logger
from app.core.security import issue_token, verify_password
from app.repositories import auth_repo, org_repo
from app.services import org_service
from app.services.audit_service import audit_service
from app.services.permission_cache import CachedGrants, permission_cache

USERNAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{2,31}$")


@dataclass(slots=True)
class UserContext:
    """鉴权上下文的**唯一形状**。

    模块 05 的四维数据权限判定只依赖这个快照（`user_id` / `dept_id` / `role_ids`），
    因此用户调岗后**下一次请求立即按新部门判定**——这是"权限即时生效"的落点。
    """

    user_id: str
    username: str
    real_name: str
    dept_id: str
    dept_name: str | None
    roles: list[RoleBrief] = field(default_factory=list)
    permissions: frozenset[str] = frozenset()
    menus: list[MenuItem] = field(default_factory=list)

    @property
    def role_ids(self) -> frozenset[str]:
        return frozenset(r.role_id for r in self.roles)


def _validate_login_input(username: str, password: str) -> None:
    if not USERNAME_RE.match(username or ""):
        raise BizError(Err.AUTH_PARAM_INVALID, "账号格式不合法")
    raw = (password or "").encode("utf-8")
    if len(raw) < settings.password_min_len or len(raw) > settings.password_max_bytes:
        raise BizError(
            Err.AUTH_PARAM_INVALID,
            f"密码长度应在 {settings.password_min_len}~{settings.password_max_bytes} 字节",
        )


def _profile_from(user: dict[str, Any], roles: list[dict[str, Any]],
                  codes: list[str], menus: list[dict[str, Any]],
                  dept_name: str | None) -> UserProfile:
    return UserProfile(
        user_id=user["_id"],
        username=user["username"],
        real_name=user.get("real_name", ""),
        dept_id=user.get("dept_id", ""),
        dept_name=dept_name,
        roles=[RoleBrief(role_id=r["_id"], code=r["code"], name=r["name"],
                         is_system=bool(r.get("is_system"))) for r in roles],
        permissions=codes,
        menus=[MenuItem(**m) for m in menus],
    )


async def _build_profile(user: dict[str, Any]) -> UserProfile:
    """角色 → 权限码 → 菜单。

    前三次库访问走 **60 秒 TTL 缓存**（模块 01 §4.3）：命中时只剩一次部门名查询。
    `dept_name` **刻意不进缓存**——部门改名后应立刻反映，而它只是一次按主键的
    点查，成本极低；把它缓存起来会让"改了部门名却要等 60 秒"成为可感知的怪现象。
    """
    user_id = user["_id"]
    cached = permission_cache.get(user_id)
    if cached is None:
        try:
            roles = await auth_repo.list_roles_of_user(user_id)
            codes = await auth_repo.list_permission_codes_of_roles([r["_id"] for r in roles])
            menus = await auth_repo.list_menus_of_permission_codes(codes)
        except Exception as exc:                      # noqa: BLE001
            logger.exception("装载权限上下文失败 user_id=%s", user_id)
            raise BizError(Err.AUTH_STORE_FAILED) from exc
        cached = CachedGrants(roles=tuple(roles), codes=tuple(codes), menus=tuple(menus))
        permission_cache.put(user_id, cached)

    try:
        dept_name = await auth_repo.get_department_name(user.get("dept_id", ""))
    except Exception as exc:                          # noqa: BLE001
        logger.exception("读取部门名失败 user_id=%s", user_id)
        raise BizError(Err.AUTH_STORE_FAILED) from exc
    return _profile_from(user, list(cached.roles), list(cached.codes),
                         list(cached.menus), dept_name)


async def _fetch_user_by_id(user_id: str) -> dict[str, Any]:
    try:
        user = await auth_repo.find_user_by_id(user_id)
    except Exception as exc:                          # noqa: BLE001
        logger.exception("读取用户失败 user_id=%s", user_id)
        raise BizError(Err.AUTH_STORE_FAILED) from exc
    if user is None:
        raise BizError(Err.AUTH_TOKEN_INVALID, "用户不存在或已删除")
    return user


async def login(username: str, password: str) -> LoginResponse:
    """登录主流程；校验顺序见模块首部注释，任一步失败即返回。"""
    _validate_login_input(username, password)

    try:
        user = await auth_repo.find_user_by_username(username)
    except Exception as exc:                          # noqa: BLE001
        logger.exception("查询账号失败 username=%s", username)
        raise BizError(Err.AUTH_STORE_FAILED) from exc

    # 账号不存在与密码错误返回同一个码，避免账号枚举
    if user is None or not verify_password(password, user.get("password_hash", "")):
        logger.info("登录失败（账号或密码错误）username=%s", username)
        raise BizError(Err.AUTH_CREDENTIALS)

    if user.get("status") != UserStatus.ACTIVE:
        logger.info("登录失败（账号已停用）user_id=%s", user["_id"])
        raise BizError(Err.AUTH_DISABLED)

    token, _jti, expires_in = issue_token(user["_id"])
    # 写 E09 的 last_login_at 经 02 的仓储（ER-02：写必须调用归属模块）
    await org_repo.touch_last_login(user["_id"], int(time.time()))
    profile = await _build_profile(user)
    logger.info("登录成功 user_id=%s roles=%s", user["_id"], [r.code for r in profile.roles])
    return LoginResponse(access_token=token, expires_in=expires_in, user=profile)


async def load_context(user_id: str) -> UserContext:
    """供 JWT 中间件与 `/auth/me` 共用。

    **每请求校验 `status`**：账号被停用后，已签发的令牌**立即失效**（模块 Spec AC-01-03）。
    """
    user = await _fetch_user_by_id(user_id)
    if user.get("status") != UserStatus.ACTIVE:
        raise BizError(Err.AUTH_DISABLED)
    profile = await _build_profile(user)
    return UserContext(
        user_id=profile.user_id,
        username=profile.username,
        real_name=profile.real_name,
        dept_id=profile.dept_id,
        dept_name=profile.dept_name,
        roles=profile.roles,
        permissions=frozenset(profile.permissions),
        menus=profile.menus,
    )


# =========================================================================== 功能权限（模块 01 的遗留件）
async def list_permission_defs() -> list[dict[str, Any]]:
    """功能权限**定义清单**（原型 `08` 矩阵页的行标题与分组）。

    只读 `sys_permissions`（E12 的归属模块就是本模块）。
    """
    rows = await auth_repo.list_permissions()
    return [{"permission_id": r["_id"], "code": r["code"], "name": r.get("name", ""),
             "type": r.get("type", ""), "parent_id": r.get("parent_id"),
             "menu_path": r.get("menu_path"), "sort": r.get("sort", 0)} for r in rows]


async def role_permissions(role_id: str) -> dict[str, Any]:
    """某角色已有的功能权限（原型 `08` 矩阵页的勾选状态）。"""
    if not await org_service.role_exists(role_id):
        raise BizError(Err.AUTH_ROLE_NOT_FOUND, f"角色不存在：{role_id}")
    permission_ids = await auth_repo.permission_ids_of_role(role_id)
    perms = await auth_repo.find_permissions_by_ids(permission_ids)
    return {"role_id": role_id, "permission_ids": permission_ids,
            "codes": sorted(p["code"] for p in perms)}


async def revoke_role_permissions(role_id: str) -> int:
    """清空某角色的全部授权（**E13 的写入者是本模块**）。

    02 删除角色时必须调这里而不是自己删（ER-02）——这样"角色已删但权限残留"
    这件事在结构上不可能发生。
    """
    return await auth_repo.revoke_all_role_permissions(role_id)


async def grant_role_permissions(*, role_id: str, permission_ids: list[str],
                                 reason: str, actor_id: str,
                                 request: "Request | None" = None) -> dict[str, Any]:
    """分配功能权限（01 Spec §3.5）。

    六条服务端规则里最要紧的是 **R-06 防自锁**：不允许把 `sys_admin` 的 `role:grant`
    移除。代码里没有"超级用户"这一说，`role:grant` 一旦被移除，就再没有人能把它加回来。

    > `AUTH-2005`（403）与 `AUTH-3002`（409）在 Spec 里描述的是同一条规则
    > （§3.5 R-06 说返回 `AUTH-3002`，§5 码表又写成 `AUTH-2005`）。
    > 本实现取 **`AUTH-3002`**：§3.5 是调用侧的规则正文，且 409 的"状态冲突"
    > 比 403 的"你没权限"更贴切——调用方**有**权限，只是这次操作会让系统失去授权能力。
    """
    clean_reason = (reason or "").strip()
    if len(clean_reason) < 5:
        raise BizError(Err.AUTH_REASON_REQUIRED, "变更原因必填且不少于 5 字")
    if not await org_service.role_exists(role_id):
        raise BizError(Err.AUTH_ROLE_NOT_FOUND, f"角色不存在：{role_id}")

    wanted = list(dict.fromkeys(permission_ids or []))
    all_perms = await auth_repo.find_permissions_by_ids(wanted) if wanted else []
    if len(all_perms) != len(wanted):
        known = {p["_id"] for p in all_perms}
        missing = [pid for pid in wanted if pid not in known]
        raise BizError(Err.AUTH_PERM_NOT_FOUND, f"权限项不存在：{missing}")

    before_ids = await auth_repo.permission_ids_of_role(role_id)
    await _guard_grant_ability(role_id, before_ids, wanted)

    added, removed = await auth_repo.apply_role_permissions(
        role_id, wanted, [pid for pid in before_ids if pid not in wanted],
        actor_id, int(time.time()))
    if not added and not removed:
        # R-02：无变更时**不写审计**，否则"点一次保存"就留一条空记录
        raise BizError(Err.AUTH_NO_CHANGE, "权限未发生变化")

    permission_cache.invalidate_role(role_id)
    payload = {"target_type": "role", "target_id": role_id,
               "before": {"permission_ids": before_ids},
               "after": {"permission_ids": sorted(wanted)}, "reason": clean_reason}
    if request is not None:
        await audit_service.record_from_request(request, "role.grant", **payload)
    else:
        # 服务层直调（单测 / 后台任务）没有 Request，此时显式传 actor
        await audit_service.record("role.grant", actor=actor_id, **payload)
    logger.info("角色 %s 授权变更：+%s -%s", role_id, added, removed)
    return {"role_id": role_id, "added": added, "removed": removed,
            "permission_ids": sorted(wanted)}


async def _guard_grant_ability(role_id: str, before_ids: list[str],
                               wanted: list[str]) -> None:
    """R-06：系统管理员不能失去 `role:grant`（否则系统再也没人能授权）。"""
    role = await org_repo.get_role(role_id)
    if role is None or role.get("code") != "sys_admin":
        return
    grant_perm = next((p for p in await auth_repo.list_permissions()
                       if p["code"] == "role:grant"), None)
    if grant_perm is None:
        return
    had = grant_perm["_id"] in before_ids
    keeps = grant_perm["_id"] in wanted
    if had and not keeps:
        raise BizError(Err.AUTH_LOCKOUT,
                       "不允许移除系统管理员的权限分配权（role:grant），否则无人能再授权")
