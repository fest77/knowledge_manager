# -*- coding: utf-8 -*-
"""模块 00 · 系统配置接口（原型 `07_组织架构与系统配置页.pen` 的「模型服务参数」区块）。

权限映射依总纲 §5：读用 `model:config`、写用 `system:config`。

**审计接入（ER-05）**：`PUT` 是写操作，保存成功且内存热生效之后逐条写
`config.update`（原型 `07` 的 `stNote4` 明确要求），并带上 `reason`。
按 §3.5 C-04「业务批量成功时逐条记」的口径，**一个配置键一条审计**——
合并成一条会让"这个阈值是什么时候被谁改的"重新变成一道推理题。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.deps import current_user
from app.api.schemas_system import (ConfigListResponse, ConfigUpdateRequest,
                                    ConfigUpdateResponse)
from app.core.logging import logger
from app.core.permissions import require_perm
from app.core.response import ok
from app.services.audit_service import audit_service
from app.services.auth_service import UserContext
from app.services.config_service import config_service

router = APIRouter(prefix="/api/v1/system", tags=["00 公共基础"])


@router.get("/config", summary="读取系统 / 模型服务参数")
@require_perm("model:config")
async def get_config(user: UserContext = Depends(current_user)):
    """返回全部可配置项（按分组顺序），含类型、默认值与取值范围供前端做输入约束。"""
    payload = ConfigListResponse(items=config_service.all_items())
    return ok(payload.model_dump())


@router.put("/config", summary="保存系统 / 模型服务参数（热生效）")
@require_perm("system:config")
async def put_config(body: ConfigUpdateRequest, request: Request,
                     user: UserContext = Depends(current_user)):
    """批量保存；**先全量校验后写入**，任一项不合法则整批不生效。

    值未变化时进 `unchanged`，不写库——避免"点一次保存就 version+1"的噪声，
    也不产生"什么都没改却留下一条审计"的假痕迹（无变化时不写 `config.update`）。
    """
    result = await config_service.update(body.values, actor=user.user_id)
    if result["changed"]:
        await _write_audit(request, result, body.reason, user)
    payload = ConfigUpdateResponse(
        changed=result["changed"],
        unchanged=result["unchanged"],
        items=config_service.all_items(),
    )
    return ok(payload.model_dump())


async def _write_audit(request: Request, result: dict, reason: str,
                       user: UserContext) -> None:
    """逐条写 `config.update`。

    ★ 调用时机是**业务数据提交成功之后**（§7.2 D-02），且**不 try/except、
    不看返回值**（D-01）——审计失败只记 ERROR，绝不把业务响应变成失败。
    """
    for key in result["changed"]:
        await audit_service.record_from_request(
            request,
            action="config.update",
            target_type="config",
            target_id=key,
            target_name=key,
            before={key: result["before"].get(key)},
            after={key: result["after"].get(key)},
            reason=reason,
            actor=user.user_id,
        )
    logger.info("配置变更已留痕：%s actor=%s", result["changed"], user.user_id)
