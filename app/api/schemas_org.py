# -*- coding: utf-8 -*-
"""模块 02 的接口契约（请求/响应模型）。

字段与模块 Spec §3.1（部门）/§3.2（用户）/§3.3（角色）逐条对齐。
所有请求模型都是 `extra="forbid"`：多传字段直接判参数错，避免前端**静默传错字段名**
——那类 bug 的表现是"我明明传了 dept_id，后端说没收到"，排查成本极高。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DeptNode(BaseModel):
    """部门树的一个节点。`children` 递归到叶子。"""

    dept_id: str
    name: str
    parent_id: str | None = None
    path: list[str] = Field(default_factory=list)
    path_ids: list[str] = Field(default_factory=list)
    level: int = 0
    sort: int = 0
    status: str = "active"
    leader_user_id: str | None = None
    user_count: int = 0
    children: list["DeptNode"] = Field(default_factory=list)


class DeptTreeResponse(BaseModel):
    """`GET /api/v1/org/departments` 的出参。"""

    items: list[DeptNode] = Field(default_factory=list)


class DeptCreateRequest(BaseModel):
    """新建部门。`parent_id` 传 `null` 表示建在根下。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=32)
    parent_id: str | None = None
    sort: int | None = None
    leader_user_id: str | None = None


class DeptUpdateRequest(BaseModel):
    """编辑部门。**只有显式传了的字段才会被改**（靠 `model_fields_set` 判断）。

    这一点很重要：`parent_id: None` 的语义是"移到根下"，
    与"没传 parent_id（不动）"必须区分开，否则前端一次改名就会把部门挪到根。
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=32)
    parent_id: str | None = None
    sort: int | None = None
    leader_user_id: str | None = None
    status: str | None = None


class DeptCreateResponse(BaseModel):
    """新建部门的出参。"""

    dept_id: str


class DeptUpdateResponse(BaseModel):
    """编辑部门的出参：实际改了哪些字段。"""

    dept_id: str
    changed: dict[str, object] = Field(default_factory=dict)


class DeptDeleteResponse(BaseModel):
    """删除部门的出参。"""

    deleted: bool = True


class RoleBriefOut(BaseModel):
    """用户列表里的角色标签（只到"够显示"的粒度）。"""

    role_id: str
    code: str
    name: str


class UserItem(BaseModel):
    """用户列表/详情的一行。

    **没有 `phone` / `email`**：Spec §3.2 明确要求列表脱敏回显，
    本实现更严——列表与详情都不返回，避免"谁都能导出全员联系方式"。
    """

    user_id: str
    username: str
    real_name: str = ""
    dept_id: str = ""
    dept_name: str | None = None
    roles: list[RoleBriefOut] = Field(default_factory=list)
    status: str = "active"
    last_login_at: int | None = None


class UserPageResponse(BaseModel):
    """`GET /api/v1/org/users` 的出参（分页契约与总纲 §6.3 一致）。"""

    items: list[UserItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20


class UserCreateRequest(BaseModel):
    """新增用户。密码只在**创建这一刻**出现，之后任何接口都不回显。"""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=32)
    # 长度下限**故意留给服务层**：Pydantic 先拦会返回 `SYS-1001`，
    # 而同一条"弱口令"规则在服务层是 `ORG-1001` —— 同一个错误两种码，前端没法处理。
    password: str = Field(min_length=1, max_length=72)
    real_name: str = Field(min_length=1, max_length=32)
    dept_id: str = Field(min_length=1)
    role_ids: list[str] = Field(default_factory=list)
    phone: str | None = Field(default=None, max_length=20)
    email: str | None = Field(default=None, max_length=64)


class UserUpdateRequest(BaseModel):
    """编辑用户。`username` 传了就**必须与现状一致**（否则 `ORG-1002`）。

    把它留在入参里而不是直接删掉，是为了让"我改了账号名"这个误操作**得到明确报错**，
    而不是被 `extra="forbid"` 当成"多传字段"糊过去——后者的提示是
    "参数校验失败: username"，前者才是"登录账号不可修改"。
    """

    model_config = ConfigDict(extra="forbid")

    username: str | None = None
    real_name: str | None = Field(default=None, max_length=32)
    dept_id: str | None = None
    phone: str | None = Field(default=None, max_length=20)
    email: str | None = Field(default=None, max_length=64)


class UserStatusRequest(BaseModel):
    """停用 / 启用。`reason` 可选（Spec §3.2 未强制，但填了会进审计）。"""

    model_config = ConfigDict(extra="forbid")

    status: str
    reason: str | None = Field(default=None, max_length=200)


class UserIdResponse(BaseModel):
    """只回一个用户编号（新增用户）。"""

    user_id: str


class UserUpdateResponse(BaseModel):
    """编辑用户的出参：实际改了哪些字段。"""

    user_id: str
    changed: dict[str, object] = Field(default_factory=dict)


class UserStatusResponse(BaseModel):
    """停用 / 启用的出参。"""

    user_id: str
    status: str


class ResetPasswordRequest(BaseModel):
    """重置密码。"""

    model_config = ConfigDict(extra="forbid")

    # 同 `UserCreateRequest.password`：只限长度上限，强度判定归服务层（`ORG-1001`）
    new_password: str = Field(min_length=1, max_length=72)


class ResetPasswordResponse(BaseModel):
    """重置密码的出参（只回事实，不回密码）。"""

    user_id: str
    reset: bool = True


class UserRolesRequest(BaseModel):
    """绑定角色。`reason` 必填 ≥5 字（G-11）。"""

    model_config = ConfigDict(extra="forbid")

    role_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=5, max_length=200)


class UserRolesResponse(BaseModel):
    """绑定角色的出参：差集结果。"""

    user_id: str
    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    role_ids: list[str] = Field(default_factory=list)


class RoleItem(BaseModel):
    """角色列表的一行（原型 `07` 的 6 列）。"""

    role_id: str
    code: str
    name: str
    is_system: bool = False
    role_type: str = "自定义"
    description: str = ""
    permission_count: int = 0
    user_count: int = 0


class RoleListResponse(BaseModel):
    """`GET /api/v1/org/roles` 的出参。"""

    items: list[RoleItem] = Field(default_factory=list)


class RoleCreateRequest(BaseModel):
    """新建**业务角色**（裁定 A）。`is_system` 不接受传值，恒为 `False`。"""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=3, max_length=32)
    name: str = Field(min_length=1, max_length=32)
    description: str | None = Field(default=None, max_length=200)


class RoleUpdateRequest(BaseModel):
    """编辑角色。`code` 传了就必须与现状一致（否则 `ORG-2007`）。

    保留它在入参里是为了让「我改了角色编码」得到**明确报错**，
    而不是被 `extra="forbid"` 当成多传字段（后者只提示「参数校验失败」）。
    """

    model_config = ConfigDict(extra="forbid")

    code: str | None = None
    name: str | None = Field(default=None, max_length=32)
    description: str | None = Field(default=None, max_length=200)


class RoleIdResponse(BaseModel):
    """新建角色的出参。"""

    role_id: str


class RoleUpdateResponse(BaseModel):
    """编辑角色的出参。"""

    role_id: str
    changed: dict[str, object] = Field(default_factory=dict)


class RoleDeleteResponse(BaseModel):
    """删除角色的出参。"""

    deleted: bool = True


class PermissionItem(BaseModel):
    """一条功能权限定义（原型 `08` 矩阵页的行）。"""

    permission_id: str
    code: str
    name: str = ""
    type: str = ""
    parent_id: str | None = None
    menu_path: str | None = None
    sort: int = 0


class PermissionListResponse(BaseModel):
    """`GET /api/v1/auth/permissions` 的出参（34 条）。"""

    items: list[PermissionItem] = Field(default_factory=list)


class RolePermissionResponse(BaseModel):
    """某角色已绑定的功能权限。"""

    role_id: str
    permission_ids: list[str] = Field(default_factory=list)
    codes: list[str] = Field(default_factory=list)


class GrantPermissionRequest(BaseModel):
    """分配功能权限。`reason` 必填 ≥5 字（R-02）。"""

    model_config = ConfigDict(extra="forbid")

    permission_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=5, max_length=200)


class GrantPermissionResponse(BaseModel):
    """分配功能权限的出参：差集结果。"""

    role_id: str
    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    permission_ids: list[str] = Field(default_factory=list)


__all__ = [
    "DeptNode", "DeptTreeResponse", "DeptCreateRequest", "DeptCreateResponse",
    "DeptUpdateRequest", "DeptUpdateResponse", "DeptDeleteResponse",
    "RoleBriefOut", "UserItem", "UserPageResponse", "UserCreateRequest",
    "UserUpdateRequest", "UserStatusRequest", "UserIdResponse", "UserUpdateResponse",
    "UserStatusResponse", "ResetPasswordRequest", "ResetPasswordResponse",
    "UserRolesRequest", "UserRolesResponse",
    "RoleItem", "RoleListResponse", "RoleCreateRequest", "RoleUpdateRequest",
    "RoleIdResponse", "RoleUpdateResponse", "RoleDeleteResponse",
    "PermissionItem", "PermissionListResponse", "RolePermissionResponse",
    "GrantPermissionRequest", "GrantPermissionResponse",
]
