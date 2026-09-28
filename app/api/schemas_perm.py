# -*- coding: utf-8 -*-
"""模块 05 的请求/响应模型（Pydantic 2）。

三条刻意的取舍：

- **维度列表不设 `max_length`**：上限 200 由服务层报 `PERM-1005`。
  用 Pydantic 的 `max_length` 会让越界变成 `SYS-1001`，
  前端按 Spec 映射的文案就落空了。
- **`reason` 不设 `min_length`**：同理，少于 5 字必须报 `PERM-1001`（G-11）。
- **响应体不含任何文档内容**：AC-05-10 要求 —— `/perm/check`
  只回答"能不能读"，不泄漏知识本身。

最后一条尤其重要：判定接口的响应里一旦出现标题或正文，
它就变成了一个**越权读取通道**（只要能猜出 `doc_id` 就能拿到内容）。
所以 `doc_title` 只出现在**配置接口**（`perm:manage` 才能调）的响应里。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

# 单维上限与最小原因长度（与 `permission_service` 的常量保持一致；
# 这里只用于 OpenAPI 文档里的示例说明，**真正的校验在服务层**）
MAX_DIMENSION_ITEMS = 200
MIN_REASON_LEN = 5


class DeptBrief(BaseModel):
    """部门回显项。`exists=False` 表示这条授权指向一个已被删除的部门。"""

    dept_id: str
    name: str = ""
    status: str | None = None
    exists: bool = True


class RoleBrief(BaseModel):
    """角色回显项。"""

    role_id: str
    code: str | None = None
    name: str = ""
    exists: bool = True


class UserBrief(BaseModel):
    """用户回显项（**只含展示字段**，绝不含 `password_hash`）。"""

    user_id: str
    real_name: str = ""
    dept_name: str = ""
    exists: bool = True


class PermConfigResponse(BaseModel):
    """`GET /api/v1/perm/{doc_id}` 出参（Spec §3.1）。

    无权限记录时返回默认值 + `version=0`（表示"尚未配置"），
    **不返回 404** —— 那是合法的初始状态。
    """

    doc_id: str
    doc_title: str | None = None
    is_global: bool = False
    departments: list[DeptBrief] = Field(default_factory=list)
    roles: list[RoleBrief] = Field(default_factory=list)
    users: list[UserBrief] = Field(default_factory=list)
    version: int = 0
    reason: str | None = None
    updated_by: str | None = None
    updated_at: int | None = None
    readable_by: str = "nobody"


class PermSaveRequest(BaseModel):
    """`PUT /api/v1/perm/{doc_id}` 入参。"""

    is_global: bool = False
    departments: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    users: list[str] = Field(default_factory=list)
    reason: str = ""


class PermSaveResponse(BaseModel):
    """保存出参：`effective_immediately` 恒为 `true`（AD-02 的对外声明）。"""

    doc_id: str
    version: int
    effective_immediately: bool = True


class PermCheckRequest(BaseModel):
    """`POST /api/v1/perm/check` 入参（`user_id` 省略时判定当前登录用户）。"""

    doc_id: str
    user_id: str | None = None


class EvaluatedAs(BaseModel):
    """判定所依据的用户快照（**只回传编号，不回传姓名等个人信息**）。"""

    user_id: str
    dept_id: str = ""
    role_ids: list[str] = Field(default_factory=list)


class PermCheckResponse(BaseModel):
    """判定出参（Spec §3.3）。

    HTTP **恒为 200**：判定是成功操作，`allowed=false` 也是正确答案。
    只有参数错（400）与代查越权（403）才是失败。
    """

    doc_id: str
    allowed: bool
    reason_code: str
    evaluated_as: EvaluatedAs
    version: int = 0


__all__ = [
    "MAX_DIMENSION_ITEMS", "MIN_REASON_LEN",
    "DeptBrief", "RoleBrief", "UserBrief", "PermConfigResponse",
    "PermSaveRequest", "PermSaveResponse",
    "PermCheckRequest", "PermCheckResponse", "EvaluatedAs",
]
