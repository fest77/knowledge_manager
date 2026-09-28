# -*- coding: utf-8 -*-
"""密码哈希与 JWT 签发/校验。

⚠️ **与模块 Spec 的一处实测偏差（已在 README 记录）**：
Spec 写的是 `PyJWT + passlib[bcrypt]`，但实测 **passlib 1.7.4 与 bcrypt 5.0.0 不兼容**
（passlib 用 `bcrypt.__about__.__version__` 探版本，bcrypt 5.0 已移除该属性 → `ctx.hash()`
抛 `ValueError: password cannot be longer than 72 bytes`）。
故本切片**直接用 `bcrypt` 库**：同一算法、同一 `$2b$` 哈希格式、同一 `password_hash` 字段，
且 `bcrypt` 本就是 `passlib[bcrypt]` 声明的依赖，**未引入任何新依赖**。
日后若修复 passlib，历史哈希字符串可直接继续校验。
"""
from __future__ import annotations

import uuid

import bcrypt
import jwt

from app.core.config import settings
from app.core.errors import BizError, Err
from app.core.logging import logger


# ---------------------------------------------------------------- 密码
def hash_password(plain: str) -> str:
    """生成 bcrypt 哈希（`$2b$`）。超过 72 字节直接拒——bcrypt 5.0 会抛错，不能靠库兜底。"""
    raw = plain.encode("utf-8")
    if len(raw) > settings.password_max_bytes:
        raise BizError(
            Err.AUTH_PARAM_INVALID,
            f"密码不能超过 {settings.password_max_bytes} 字节（bcrypt 硬上限）",
        )
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=settings.bcrypt_rounds)).decode("ascii")


def verify_password(plain: str, password_hash: str) -> bool:
    """恒定返回 bool；哈希串损坏时按"不匹配"处理，不向上抛异常（避免登录接口 500）。"""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        logger.warning("password_hash 非法或损坏，按校验失败处理")
        return False


# ---------------------------------------------------------------- JWT
def issue_token(user_id: str) -> tuple[str, str, int]:
    """签发访问令牌。

    **载荷只放身份，不放角色/权限**（模块 01 §4.1 的裁定）：
    权限写进 token 会导致「管理员改了角色，用户必须重新登录才生效」，
    与项目「权限即时生效」的主张直接矛盾。
    返回 (token, jti, expires_in)。
    """
    import time

    jti = uuid.uuid4().hex
    now = int(time.time())
    payload = {
        "sub": user_id,
        "jti": jti,
        "iss": settings.jwt_issuer,
        "iat": now,
        "exp": now + settings.jwt_expire_seconds,
    }
    try:
        token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    except Exception as exc:                       # noqa: BLE001 — 统一转业务错误码
        logger.exception("JWT 签发失败 user_id=%s", user_id)
        raise BizError(Err.AUTH_SIGN_FAILED) from exc
    return token, jti, settings.jwt_expire_seconds


def decode_token(token: str) -> dict:
    """校验签名/过期/签发者；任何失败统一抛 AUTH-2003（不区分原因，避免给攻击者信息）。"""
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            options={"require": ["sub", "exp", "iat", "iss"]},
        )
    except jwt.PyJWTError as exc:
        logger.info("令牌校验失败: %s", type(exc).__name__)
        raise BizError(Err.AUTH_TOKEN_INVALID) from exc
